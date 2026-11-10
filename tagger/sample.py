"""按配额从候选集里选样（标注规范 v0.2 §1–§3）。

这一层只管"从候选里选哪些 id"，不读图、不打标、不去 HF 拉数据。规范要求的
是**先给每个来源建候选 manifest**，再在 manifest 上做筛选——所以输入的每一行
是一条约候选记录，输出是选中的那些行（原样带回，再加两列记账）。

三件事刻意分成三个纯函数：
    resolve_quotas  配额规划：quota_original / quota_final，缺额跨来源补齐
    dedupe_clusters 近重复去重：同簇只留分数最高的一条
    pick            分层选样：60% 质量加权 / 25% 长尾保底 / 15% 均匀随机

为什么不把它们做成一遍流式：
    "同簇留最高分"需要看到整批才知道谁最高；"按配额补齐"需要先知道每个来源
    实际可用多少。两遍读 manifest 是规范自己的流程（先建 manifest 再筛选），
    比在流里近似要诚实得多。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

# 候选行里被"规范化"出来的键。加下划线前缀是为了不和源列撞名——
# MONET 的列里有 `id`、`source`、`aesthetic_jasperai`，直接用它们当内部键
# 会在"同一份 manifest 换一列当分数"时互相覆盖。
_ID = "_id"
_SOURCE = "_source"
_SCORE = "_score"
_CLUSTER = "_cluster"
_STRATUM = "_stratum"

DEFAULT_COLUMNS = {
    "id": "id",
    "source": "source",
    "score": "score",
    "cluster": "",
    "stratum": "",
}

# 分层配比。规范 §2 写的是"60% 质量加权 / 25% 稀有题材保底 / 15% 均匀随机"，
# 并注明这是"初始可测试策略，需抽样审计后定稿"——所以它必须是配置项。
DEFAULT_MIX = (0.60, 0.25, 0.15)


class SampleError(ValueError):
    """配置或输入有问题。带"怎么改"，不要只抛栈。"""


@dataclass(slots=True)
class SampleSpec:
    """一次选样的参数。"""

    # 来源 → 计划条数。来源名就是 manifest 里 source 列的取值。
    quotas: dict[str, int] = field(default_factory=dict)
    # 配额的合计（如 MONET 3000 万）。给了就校验，不等只警告不拦——
    # 规范 §8 要求合计"必须"准确，但拦死会让人改不了单个来源试跑。
    quota_total: int = 0
    mix: tuple[float, float, float] = DEFAULT_MIX
    seed: int = 42
    # 合格线：低于它的候选不参与选样（规范 §2"所有分支都必须通过最低质量过滤"）。
    min_score: float = 0.0
    dedupe: bool = True
    # 缺额是否允许跨来源补齐（规范 §2 允许，但要记 quota_original/final）。
    allow_borrow: bool = True
    columns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLUMNS))

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None,
                  *, profile: str | None = None) -> "SampleSpec":
        d = d or {}
        # 多组配额：MONET 3000 万与 Fine-T2I 100 万是两个独立交付物，配额表
        # 不能混在一个 quotas 里（合计算谁的都是错的）。所以允许分 profile，
        # 顶层放公共项（mix/seed/columns），profile 里放 quota_total + quotas。
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
                # columns 做深合并：profile 通常只想改 score/cluster 两列，
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

        mix_raw = d.get("mix") or {}
        if isinstance(mix_raw, dict):
            mix = (float(mix_raw.get("quality", DEFAULT_MIX[0])),
                   float(mix_raw.get("tail", DEFAULT_MIX[1])),
                   float(mix_raw.get("random", DEFAULT_MIX[2])))
        else:
            seq = list(mix_raw) or list(DEFAULT_MIX)
            if len(seq) != 3:
                raise SampleError("sample.mix 要么是 {quality,tail,random}，"
                                  "要么是三个数（质量/长尾/随机）")
            mix = tuple(float(x) for x in seq)  # type: ignore[assignment]
        if any(x < 0 for x in mix) or abs(sum(mix) - 1.0) > 1e-6:
            raise SampleError(f"sample.mix 三项必须非负且合计为 1，收到 {mix}")

        columns = dict(DEFAULT_COLUMNS)
        if isinstance(d.get("columns"), dict):
            columns.update({str(k): str(v) for k, v in d["columns"].items()})
        if not columns.get("id"):
            raise SampleError("sample.columns.id 不能为空：没有 id 就无法记账")

        return cls(
            quotas=quotas,
            quota_total=max(0, int(d.get("quota_total", 0) or 0)),
            mix=mix,
            seed=int(d.get("seed", 42)),
            min_score=float(d.get("min_score", 0.0) or 0.0),
            dedupe=_flag(d.get("dedupe"), True),
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
    """读一份候选 manifest（jsonl / parquet），把配置的列规范化成内部键。

    只保留"内部键 + 原始行"：输出时原样带回，是为了让下游拿得到 url/宽高/
    许可这些筛选依据，而不是只剩一个 id 又要回去查一次。

    `source` 是兜底来源名：manifest 里没有 source 列（很多导出就是没有）时
    用它，否则所有行会落进同一个空字符串分组，配额表里的来源一个都对不上。
    """
    p = Path(path)
    if not p.is_file():
        raise SampleError(f"候选 manifest 不存在: {p}")
    cols = spec.columns
    wanted = [cols[k] for k in ("id", "source", "score", "cluster", "stratum")
              if cols.get(k)]

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
        table = pq.read_table(str(p), columns=list(dict.fromkeys(wanted)))
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

    def pick(key: str) -> str:
        name = cols.get(key) or ""
        value = row.get(name) if name else ""
        return "" if value is None else str(value)

    ident = pick("id").strip()
    if not ident:
        return None                      # 没有 id 的行无法记账，也无法去重
    rec = dict(row)
    rec[_ID] = ident
    rec[_SOURCE] = pick("source").strip()
    try:
        rec[_SCORE] = float(row.get(cols.get("score") or "") or 0.0)
    except (TypeError, ValueError):
        rec[_SCORE] = 0.0
    rec[_CLUSTER] = pick("cluster").strip()
    rec[_STRATUM] = pick("stratum").strip()
    return rec


# ------------------------------------------------------------------ 配额


def resolve_quotas(available: dict[str, int], quotas: dict[str, int],
                   *, allow_borrow: bool = True,
                   ) -> tuple[dict[str, tuple[int, int]], dict[str, int], list[str]]:
    """定每个来源的计划数与实际目标数。

    返回 (每来源 (quota_original, quota_final), 借入量, 警告)。
    规则（规范 §2）：
      - 可用不足就按可用削顶，差额记 shortfall；
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
            f"{len(unknown)} 个来源在 manifest 里有数据但没配额（不会被选中）："
            f"{', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''}")

    return {s: (original[s], final[s]) for s in all_sources}, borrowed, warnings


# ------------------------------------------------------------------ 去重


def dedupe_clusters(rows: Sequence[dict]) -> tuple[list[dict], int]:
    """同一近重复簇只留分数最高的一条（跨来源）。

    `sscd_cluster_id` 这类簇是"同一张图的变体"的代理。留最高分不是"最好的
    那张图"（簇内可能是不同裁剪），而是"最像我们要的那张"——规范 §2 写的是
    "只保留质量更高且来源可追踪的版本"，质量分是唯一可比的量。
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
    """从一组候选里选 n 条：质量/长尾/随机三段配额。

    ★质量段用"按分数排序取前 n_q"，不是按分数做加权随机。理由：
      加权随机要两遍扫描 + 累加分布（3000 万条上很贵），而它的收益是
      "次优样本也有机会"——这件事已经由随机段的 15% 承担了。排序给的是
      确定、可复现、能一句话解释的结果。
    """
    if n <= 0 or not rows:
        return []
    if len(rows) <= n:
        return list(rows)

    q_share, t_share, _ = spec.mix
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

    # 3) 随机段：剩下的按稳定哈希排序取。可复现，且不依赖输入顺序。
    if len(chosen) < n:
        rest = [r for r in rows if (r[_SOURCE], r[_ID]) not in chosen]
        rest.sort(key=lambda r: _stable_key(spec, r))
        for row in rest:
            if len(chosen) >= n:
                break
            take(row)

    return sorted(chosen.values(), key=lambda r: (r[_SOURCE], r[_ID]))


# ------------------------------------------------------------------ 总流程


def select(by_source: dict[str, Sequence[dict]], spec: SampleSpec,
           *, dedupe: bool | None = None) -> SampleResult:
    """按来源分组选样。by_source 是 `{source: 候选行}`。

    ★去重放在选样**之前**：先选后去重会把某个来源的配额悄悄打掉一截，
    报告里的 selected 却还是原数——对不上账，且比例会被去重强度带偏。
    """
    use_dedupe = spec.dedupe if dedupe is None else dedupe
    errors: list[str] = []

    # 过滤合格线 + 收集可用量
    qualified: dict[str, list[dict]] = {}
    available: dict[str, int] = {}
    for source, rows in by_source.items():
        available[source] = len(rows)
        ok = [r for r in rows if r[_SCORE] >= spec.min_score]
        qualified[source] = ok

    quotas, borrowed, warnings = resolve_quotas(available, spec.quotas,
                                                allow_borrow=spec.allow_borrow)

    # 全局去重（跨来源），逐来源统计被剔掉多少
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
    # 报告要覆盖配额表里的每一个来源：某个来源这次一条候选都没有（manifest
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

    for err in errors:
        warnings.append(err)
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
