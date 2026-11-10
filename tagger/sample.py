"""按配额从候选集里挑 id（标注规范 v0.2 §1–§3）。

**这一层只做一件事：按表里的配额，从每个来源挑够数。**

    MONET 要 3000 万，表里写好了：laion 1030 万、coyo 660 万、
    commoncatalog-cc-by 500 万、megalith10m 430 万、cc12m 378.72 万、
    diffusion-aesthetic-4k 1.28 万。每个来源的候选清单给进来，
    各自挑够自己的定额，完事。

挑哪些？**稳定随机**：`hash(seed, source, id)` 排序取前 N。同一份候选、
同一个 seed，跑几次都是同一批；换个 seed 就是另一批。不按文件顺序取。

可选开关（默认全关，都是"初始可测试策略"级别的东西，不是必需）：

    min_score   质量线，低于它的候选不参与。默认 0 = 不过滤。
    mix         60/25/15 分层。默认关 = 均匀随机。
    dedupe      同 sscd_cluster_id 只留最高分。默认关。

★为什么默认全关：这三条在规范里写明是"初始可测试策略，需抽样审计后定稿"，
  而它们都会改变样本分布（质量线砍掉尾部、分层改变题材构成、去重让某些
  来源在不知不觉中少一截）。默认开启等于替主创决定了分布——想用就显式开。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

# 候选行里被"规范化"出来的键。加下划线前缀是为了不和源列撞名——
# MONET 的列里有 `id`、`source`、`aesthetic_jasperai`，直接用它们当内部键
# 会在"同一份 manifest 换一列当分数"时互相覆盖。
_ID = "_id"
_SOURCE = "_source"
_SCORE = "_score"
_CLUSTER = "_cluster"
_STRATUM = "_stratum"

# 一份清单里至少要能找到的两列。其余（分数/近重复簇/题材）只在开了对应的
# 可选开关时才需要——不开就不读，候选清单里没有这些列也照样能跑。
DEFAULT_COLUMNS = {
    "id": "id",
    "source": "source",
    "score": "",
    "cluster": "",
    "stratum": "",
}

# 分层配比：60% 按分数取头部 / 25% 稀有题材保底 / 15% 均匀随机。
# 只在 `mix` 开着时用；关着的时候是纯均匀随机。
DEFAULT_MIX = (0.60, 0.25, 0.15)


class SampleError(ValueError):
    """配置或输入有问题。带"怎么改"，不要只抛栈。"""


@dataclass(slots=True)
class SampleSpec:
    """一次选样的参数。"""

    # 来源 → 计划条数。来源名就是 manifest 里 source 列的取值。
    quotas: dict[str, int] = field(default_factory=dict)
    # 配额的合计（如 MONET 3000 万）。给了就校验；只警告不拦——
    # 拦死会让人没法单独试跑一个来源。
    quota_total: int = 0
    seed: int = 42
    # ---- 以下四个是可选开关，默认全关 ----
    min_score: float = 0.0
    mix: bool = False
    mix_ratio: tuple[float, float, float] = DEFAULT_MIX
    dedupe: bool = False
    # 缺额是否允许跨来源补齐（规范 §2 允许，但要记 quota_original/final）。
    allow_borrow: bool = True
    columns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLUMNS))

    @property
    def needs_score(self) -> bool:
        """这次要不要读分数列。"""
        return bool(self.min_score) or self.mix

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None,
                  *, profile: str | None = None) -> "SampleSpec":
        d = d or {}
        # 多组配额：MONET 3000 万与 Fine-T2I 100 万是两个独立交付物，配额表
        # 不能混在一个 quotas 里（合计算谁的都是错的）。所以允许分 profile，
        # 顶层放公共项，profile 里放 quota_total + quotas。
        profiles = d.get("profiles")
        if isinstance(profiles, dict) and profiles:
            name = (profile or str(d.get("active") or "")).strip()
            if not name:
                raise SampleError(
                    f"sample.profiles 有 {len(profiles)} 组，必须用 --profile 指定"
                    f"（可选：{', '.join(sorted(profiles))}），或在配置里写 active")
            if name not in profiles:
                raise SampleError(
                    f"未知的抽样 profile {name!r}，可选：{', '.join(sorted(profiles))}")
            merged = {k: v for k, v in d.items() if k != "profiles"}
            over = dict(profiles[name] or {})
            if isinstance(over.get("columns"), dict):
                # columns 做深合并：profile 通常只想改一两列，
                # 整段替换会把 id/source 一起丢掉（那份清单就直接不能用了）。
                over["columns"] = {**(d.get("columns") or {}), **over["columns"]}
            merged.update(over)
            d = merged

        quotas: dict[str, int] = {}
        raw = d.get("quotas") or {}
        if not isinstance(raw, dict):
            raise SampleError("sample.quotas 必须是 {来源: 条数} 的映射")
        for key, value in raw.items():
            try:
                quotas[str(key)] = max(0, int(value))
            except (TypeError, ValueError) as exc:
                raise SampleError(
                    f"sample.quotas[{key!r}] 不是整数：{value!r}") from exc

        # mix 可以写 true/false（开关）或 {quality,tail,random}（开了并给比例）。
        mix_raw = d.get("mix", False)
        if isinstance(mix_raw, dict):
            mix_on = True
            ratio = (float(mix_raw.get("quality", DEFAULT_MIX[0])),
                     float(mix_raw.get("tail", DEFAULT_MIX[1])),
                     float(mix_raw.get("random", DEFAULT_MIX[2])))
        elif isinstance(mix_raw, (list, tuple)):
            mix_on = True
            seq = list(mix_raw)
            if len(seq) != 3:
                raise SampleError("sample.mix 给三个数时是 质量/长尾/随机")
            ratio = tuple(float(x) for x in seq)  # type: ignore[assignment]
        else:
            mix_on = _flag(mix_raw, False)
            ratio = DEFAULT_MIX
        if any(x < 0 for x in ratio) or abs(sum(ratio) - 1.0) > 1e-6:
            raise SampleError(f"sample.mix 三项必须非负且合计为 1，收到 {ratio}")

        columns = dict(DEFAULT_COLUMNS)
        if isinstance(d.get("columns"), dict):
            columns.update({str(k): str(v) for k, v in d["columns"].items()})
        if not columns.get("id"):
            raise SampleError("sample.columns.id 不能为空：没有 id 就无法记账")

        return cls(
            quotas=quotas,
            quota_total=max(0, int(d.get("quota_total", 0) or 0)),
            seed=int(d.get("seed", 42)),
            min_score=float(d.get("min_score", 0.0) or 0.0),
            mix=mix_on,
            mix_ratio=ratio,  # type: ignore[arg-type]
            dedupe=_flag(d.get("dedupe"), False),
            allow_borrow=_flag(d.get("allow_borrow"), True),
            columns=columns,
        )


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "none")


@dataclass(slots=True)
class SourceReport:
    """一个来源的记账。规范 §2 要求同时记 quota_original 与 quota_final。"""

    source: str
    available: int = 0        # manifest 里出现多少条
    qualified: int = 0        # 过了 min_score 的
    selected: int = 0         # 最终选中
    quota_original: int = 0   # 计划配额
    quota_final: int = 0      # 补齐/削顶之后的实际目标
    duplicates: int = 0       # 被近重复去重剔掉的
    shortfall: int = 0        # quota_original - selected
    borrowed: int = 0         # 从别的来源补进来的条数

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "available": self.available,
            "qualified": self.qualified,
            "selected": self.selected,
            "quota_original": self.quota_original,
            "quota_final": self.quota_final,
            "duplicates": self.duplicates,
            "shortfall": self.shortfall,
            "borrowed": self.borrowed,
        }


@dataclass(slots=True)
class SampleResult:
    rows: list[dict] = field(default_factory=list)
    report: list[SourceReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def quota_original(self) -> int:
        return sum(r.quota_original for r in self.report)

    @property
    def quota_final(self) -> int:
        return sum(r.quota_final for r in self.report)

    def summary(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "quota_original": self.quota_original,
            "quota_final": self.quota_final,
            "sources": [r.as_dict() for r in self.report],
            "warnings": list(self.warnings),
        }


# ------------------------------------------------------------------ 读候选


def read_candidates(path: str | Path, spec: SampleSpec,
                    source: str = "") -> list[dict]:
    """读一份候选清单（jsonl / parquet），只读实际用得到的列。

    parquet 走 `columns=` 只拉需要的几列：MONET 的元数据 parquet 里还有图片
    缩略图和好几个 Caption 列，整表读进来是几十倍的浪费。

    `source` 是兜底来源名：清单里没有 source 列（很多导出就是没有）时用它，
    否则所有行会落进同一个空字符串分组，配额表里的来源一个都对不上。
    """
    p = Path(path)
    if not p.is_file():
        raise SampleError(f"候选清单不存在: {p}")
    cols = spec.columns
    wanted = [cols[k] for k in ("id", "source") if cols.get(k)]
    if spec.needs_score and cols.get("score"):
        wanted.append(cols["score"])
    if spec.dedupe and cols.get("cluster"):
        wanted.append(cols["cluster"])
    if spec.mix and cols.get("stratum"):
        wanted.append(cols["stratum"])
    wanted = list(dict.fromkeys(wanted))

    if p.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise SampleError("读 parquet 候选需要 pyarrow") from exc
        names = pq.ParquetFile(str(p)).schema_arrow.names
        missing = [c for c in wanted if c not in names]
        if missing:
            raise SampleError(
                f"{p} 缺少列 {missing}；当前 sample.columns 指到这里没有的列。"
                f"实际列：{names[:12]}{'…' if len(names) > 12 else ''}")
        table = pq.read_table(str(p), columns=wanted)
        raw_rows: Iterable[dict] = table.to_pylist()
    else:
        def _iter_jsonl() -> Iterator[dict]:
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            yield obj
        raw_rows = _iter_jsonl()

    out: list[dict] = []
    for row in raw_rows:
        rec = _normalize(row, spec)
        if rec is None:
            continue
        if not rec[_SOURCE] and source:
            rec[_SOURCE] = source
        out.append(rec)
    return out


def _normalize(row: dict, spec: SampleSpec) -> dict | None:
    cols = spec.columns

    def take(key: str) -> str:
        name = cols.get(key) or ""
        value = row.get(name) if name else ""
        return "" if value is None else str(value)

    ident = take("id").strip()
    if not ident:
        return None                      # 没有 id 的行无法记账
    rec = dict(row)
    rec[_ID] = ident
    rec[_SOURCE] = take("source").strip()
    rec[_SCORE] = 0.0
    if spec.needs_score:
        try:
            rec[_SCORE] = float(row.get(cols.get("score") or "") or 0.0)
        except (TypeError, ValueError):
            rec[_SCORE] = 0.0
    rec[_CLUSTER] = take("cluster").strip() if spec.dedupe else ""
    rec[_STRATUM] = take("stratum").strip() if spec.mix else ""
    return rec


# ------------------------------------------------------------------ 配额


def resolve_quotas(available: dict[str, int], quotas: dict[str, int],
                   *, allow_borrow: bool = True,
                   ) -> tuple[dict[str, tuple[int, int]], dict[str, int], list[str]]:
    """定每个来源的计划数与实际目标数。

    返回 (每来源 (quota_original, quota_final), 借入量, 警告)。

    规则就是规范 §2 那一句：
      - 可用不足就按可用削顶；
      - 缺额可跨来源补齐，按各来源的**余量比例**分，余数给余量最大的；
      - 补进来的量记在 borrowed 上，quota_original 不动——这样事后能分清
        "计划就是这个数"和"这个数是凑出来的"。
    """
    all_sources = sorted(set(available) | set(quotas))
    original = {s: max(0, int(quotas.get(s, 0))) for s in all_sources}
    final = {s: min(original[s], max(0, int(available.get(s, 0))))
             for s in all_sources}
    borrowed = {s: 0 for s in all_sources}
    warnings: list[str] = []

    short = {s: original[s] - final[s] for s in all_sources
             if original[s] > final[s]}
    if short and allow_borrow:
        demand = sum(short.values())
        spare = {s: max(0, int(available.get(s, 0)) - final[s])
                 for s in all_sources if s not in short}
        total_spare = sum(spare.values())
        given = 0
        if total_spare:
            for s in sorted(spare, key=lambda k: (-spare[k], k)):
                if given >= demand:
                    break
                add = min(spare[s], demand - given,
                          max(1, round(demand * spare[s] / total_spare)))
                if add <= 0:
                    continue
                final[s] += add
                borrowed[s] += add
                given += add
            # 比例取整会剩几条，按余量从大到小补到补不动为止。
            for s in sorted(spare, key=lambda k: (-spare[k], k)):
                if given >= demand:
                    break
                add = min(spare[s], demand - given)
                if add > 0:
                    final[s] += add
                    borrowed[s] += add
                    given += add
        if given < demand:
            warnings.append(
                f"缺额 {demand - given} 条无处可补：所有来源的候选都已用尽")

    for s in short:
        warnings.append(
            f"来源 {s} 候选不足：计划 {original[s]}、可用 {available.get(s, 0)}，"
            f"缺 {short[s]}")

    unknown = [s for s in all_sources
               if s and s not in quotas and available.get(s, 0) > 0]
    if unknown:
        warnings.append(
            f"{len(unknown)} 个来源在清单里有数据但没配额（不会被选中）："
            f"{', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''}")

    return {s: (original[s], final[s]) for s in all_sources}, borrowed, warnings


# ------------------------------------------------------------------ 可选：去重


def dedupe_clusters(rows: Sequence[dict]) -> tuple[list[dict], int]:
    """同一近重复簇只留分数最高的一条（跨来源）。**默认不启用。**

    对应规范 §2 的"同一 sscd_cluster_id 原则上不重复入选"。开了就要有分数列，
    否则"最高分"退化成按 id 取字典序，留哪条是随机的。
    """
    best: dict[str, dict] = {}
    for row in rows:
        cluster = row.get(_CLUSTER) or ""
        if not cluster:
            continue
        prev = best.get(cluster)
        if prev is None or (row[_SCORE], row[_ID]) > (prev[_SCORE], prev[_ID]):
            best[cluster] = row
    winners = {(r[_SOURCE], r[_ID]) for r in best.values()}
    kept: list[dict] = []
    removed = 0
    for row in rows:
        cluster = row.get(_CLUSTER) or ""
        if cluster and (row[_SOURCE], row[_ID]) not in winners:
            removed += 1
            continue
        kept.append(row)
    return kept, removed


# ------------------------------------------------------------------ 选样


def _stable_key(spec: SampleSpec, row: dict) -> bytes:
    """(seed, source, id) 的稳定哈希。加 source 的理由与语言抽签相同：
    某个来源 id 连号时，只按 id 排序会让整段成片入选。"""
    raw = f"{spec.seed}:{row[_SOURCE]}:{row[_ID]}"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def pick(rows: Sequence[dict], n: int, spec: SampleSpec) -> list[dict]:
    """从一组候选里挑 n 条。

    默认（均匀随机）：按 `hash(seed, source, id)` 排序取前 n。可复现、
    不依赖输入顺序、不需要分数列——这就是"从每个文件夹里抽够数"。
    开了 `mix` 才走 60/25/15 三段。
    """
    if n <= 0 or not rows:
        return []
    if len(rows) <= n:
        return list(rows)

    if not spec.mix:
        ranked = sorted(rows, key=lambda r: _stable_key(spec, r))
        return list(ranked[:n])

    q_share, t_share, _ = spec.mix_ratio
    n_q = min(n, int(round(n * q_share)))
    n_t = min(n - n_q, int(round(n * t_share)))

    ranked = sorted(rows, key=lambda r: (-r[_SCORE], _stable_key(spec, r)))
    # ★键必须是 (source, id) 而不是 id：不同来源会出现同一个 id（跨库同图），
    #   用 id 当键会让后到的把先到的覆盖掉，选中数凭空少一截。
    chosen: dict[tuple[str, str], dict] = {}

    def take(row: dict) -> bool:
        key = (row[_SOURCE], row[_ID])
        if key in chosen:
            return False
        chosen[key] = row
        return True

    # 1) 质量段：分数头部
    for row in ranked:
        if len(chosen) >= n_q:
            break
        take(row)

    # 2) 长尾段：按 stratum 频次升序轮转。稀有桶每轮都能出一张，
    #    直到配额填满——这样"只有 30 张的题材"不会一条都进不来。
    if n_t > 0:
        buckets: dict[str, list[dict]] = {}
        for row in ranked:
            buckets.setdefault(row[_STRATUM] or row[_SOURCE] or "-", []).append(row)
        order = sorted(buckets, key=lambda k: (len(buckets[k]), k))
        start = len(chosen)
        while len(chosen) - start < n_t:
            progressed = False
            for key in order:
                bucket = buckets[key]
                while bucket:
                    row = bucket.pop(0)
                    if take(row):
                        progressed = True
                        break
                if len(chosen) - start >= n_t:
                    break
            if not progressed:
                break

    # 3) 随机段：剩下的按稳定哈希排序取。
    if len(chosen) < n:
        rest = [r for r in rows if (r[_SOURCE], r[_ID]) not in chosen]
        rest.sort(key=lambda r: _stable_key(spec, r))
        for row in rest:
            if len(chosen) >= n:
                break
            take(row)

    return sorted(chosen.values(), key=lambda r: (r[_SOURCE], r[_ID]))


# ------------------------------------------------------------------ 总流程


def select(by_source: Mapping[str, Sequence[dict]], spec: SampleSpec,
           *, dedupe: bool | None = None) -> SampleResult:
    """按来源分组选样。by_source 是 `{source: 候选行}`。"""
    use_dedupe = spec.dedupe if dedupe is None else dedupe

    # 过滤合格线 + 收集可用量
    qualified: dict[str, list[dict]] = {}
    available: dict[str, int] = {}
    for source, rows in by_source.items():
        available[source] = len(rows)
        qualified[source] = [r for r in rows if r[_SCORE] >= spec.min_score]

    quotas, borrowed, warnings = resolve_quotas(available, spec.quotas,
                                                allow_borrow=spec.allow_borrow)

    # 去重要在选样**之前**：先选后去重会把某个来源的配额悄悄打掉一截，
    # 报告里的 selected 却还是原数——对不上账。
    removed_by_source = {s: 0 for s in qualified}
    if use_dedupe:
        flat = [r for rows in qualified.values() for r in rows]
        kept, _ = dedupe_clusters(flat)
        keep_keys = {(r[_SOURCE], r[_ID]) for r in kept}
        for source, rows in qualified.items():
            new_rows = [r for r in rows if (r[_SOURCE], r[_ID]) in keep_keys]
            removed_by_source[source] = len(rows) - len(new_rows)
            qualified[source] = new_rows

    report: list[SourceReport] = []
    picked: list[dict] = []
    # 报告要覆盖配额表里的每一个来源：某个来源这次一条候选都没有（清单
    # 缺文件/路径写错），必须显示成 0 而不是从表里消失——"消失"看起来像
    # 这个来源不在计划里，而它其实欠着几百万条。
    for source in sorted(set(available) | set(spec.quotas)):
        original, final = quotas.get(source, (0, 0))
        rows = qualified.get(source, [])
        chosen = pick(rows, final, spec) if final > 0 else []
        # 可用比目标少时，pick 会全给出来——那是缺额，不是 bug。
        if 0 < final and len(chosen) < final:
            warnings.append(
                f"来源 {source} 只选出 {len(chosen)}/{final}：合格候选不足")
        report.append(SourceReport(
            source=source,
            available=available[source],
            qualified=len(rows),
            selected=len(chosen),
            quota_original=original,
            quota_final=final,
            duplicates=removed_by_source.get(source, 0),
            shortfall=max(0, original - len(chosen)),
            borrowed=borrowed.get(source, 0),
        ))
        picked.extend(chosen)

    if spec.quota_total:
        planned = sum(orig for orig, _ in quotas.values())
        if planned != spec.quota_total:
            warnings.append(
                f"配额合计 {planned} ≠ quota_total {spec.quota_total}"
                f"（差 {planned - spec.quota_total:+d}）")
    actual = len(picked)
    if spec.quota_total and actual != spec.quota_total:
        warnings.append(
            f"实际选中 {actual} 条，与 quota_total {spec.quota_total} 差 "
            f"{actual - spec.quota_total:+d} 条（候选不足或质量过滤所致）")

    return SampleResult(rows=picked, report=report, warnings=warnings)


# ------------------------------------------------------------------ 输出


def write_selection(result: SampleResult, out_path: str | Path) -> tuple[Path, int]:
    """把选中行写成 jsonl。内部键不写出（下划线开头的一律剥掉），
    只加两列可审计的记账：`sample_source` 与 `sample_quota`。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    quota_of = {r.source: r for r in result.report}
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in result.rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            rec = quota_of.get(row[_SOURCE])
            clean["sample_source"] = row[_SOURCE]
            clean["sample_quota"] = (rec.quota_final if rec else 0)
            fh.write(json.dumps(clean, ensure_ascii=False) + "\n")
    tmp.replace(out)
    return out, len(result.rows)


def write_report(result: SampleResult, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.summary(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out
