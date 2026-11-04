"""AB 变体：同一批图跑几套 prompt，然后按变体比。

要解决的问题
------------
"哪个 prompt 更好"不能靠读 prompt 猜。同一批图、同一个模型、同一个
分辨率，只有指令不同，产出的 tag 密度、描述长度、质检通过率才有可比性。
一旦换了图或换了模型，差异就归因不清了。

三个必须守住的规则
------------------
1. **同一批输入**。变体共用 --shard / --limit，谁也别偷偷多跑几张。
2. **分目录落盘**。产物写进 <out-dir>/<variant>/，否则重跑时后一个变体
   会被前一个的"-tag.json 已存在"当成已完成而整批跳过——这种"成功但
   什么都没做"最难发现。
3. **只比数值，不比感觉**。对比表给的是条数、tag 密度、字符数、质检分、
   标记率；最后选哪套仍然要人看抽检清单，但至少先排除掉明显崩掉的那套。

变体怎么配
----------
configs/tiers.yaml 的 variants 段：

    variants:
      base: {}
      detail:
        prompt_note: |
          Additionally describe the dominant colour palette ...
        temperature: 0.35
      raw:
        prompt_file: prompts/raw.txt      # 整段替换

prompt_note 是追加（保留自动生成的长度/数量约束），prompt_file 是替换
（连约束一起替换，写错就会产出不达标的数据，只建议在调 prompt 时用）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ConfigError

# 对比表里展示的列。顺序即 markdown 列顺序。
COMPARE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("avg_tag_count", "平均 tag 数"),
    ("avg_chars", "平均字符数"),
    ("avg_nl_tokens", "平均 token"),
    ("avg_score", "平均质检分"),
    ("complete_rate", "完整率"),
    ("flag_rate", "标记率"),
)

# 与基线比较时给出相对差（百分比）的指标。
DELTA_KEYS = ("avg_tag_count", "avg_chars", "avg_score", "complete_rate")


@dataclass(slots=True)
class Variant:
    """一套指令。空 note + 空 file = 就是默认 prompt。"""

    name: str
    prompt_note: str = ""
    prompt_file: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    note: str = ""

    @property
    def overrides_prompt(self) -> bool:
        return bool(self.prompt_note or self.prompt_file)

    def render_prompt(self, base: str) -> str:
        """把变体套到自动生成的 prompt 上。

        prompt_file 走整段替换：调 prompt 时最常见的诉求就是把整套指令
        换掉重试，此时再往里拼自动段落只会得到两份互相矛盾的约束。
        """
        if self.prompt_file:
            p = Path(self.prompt_file)
            if not p.is_file():
                raise ConfigError(f"变体 {self.name} 的 prompt_file 不存在: {p}")
            return p.read_text(encoding="utf-8").strip()
        if self.prompt_note:
            return f"{base}\n\n{self.prompt_note.strip()}"
        return base


def parse_variants(raw: Mapping[str, Any] | Sequence[Any] | None) -> list[Variant]:
    """解析配置。支持 dict（名字作键）与 list（元素带 name）。"""
    if not raw:
        return []

    items: list[tuple[str, Any]] = []
    if isinstance(raw, Mapping):
        items = [(str(k), v or {}) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, str):
                items.append((entry, {}))
            elif isinstance(entry, Mapping):
                name = str(entry.get("name") or "").strip()
                if not name:
                    raise ConfigError("variants 列表里有条目缺 name")
                items.append((name, entry))
            else:
                raise ConfigError(f"variants 条目的格式不认识: {entry!r}")
    else:
        raise ConfigError("variants 必须是映射或列表")

    out: list[Variant] = []
    seen: set[str] = set()
    for name, cfg in items:
        if not name:
            raise ConfigError("variants 里有一个空名字")
        if name in seen:
            raise ConfigError(f"variants 里有重名: {name}")
        seen.add(name)
        if not isinstance(cfg, Mapping):
            raise ConfigError(f"variants.{name} 必须是一个映射")
        temp = cfg.get("temperature")
        max_tokens = cfg.get("max_tokens")
        out.append(Variant(
            name=name,
            prompt_note=str(cfg.get("prompt_note") or ""),
            prompt_file=str(cfg.get("prompt_file") or ""),
            temperature=float(temp) if temp is not None else None,
            max_tokens=int(max_tokens) if max_tokens is not None else None,
            note=str(cfg.get("note") or ""),
        ))
    return out


def resolve_variants(raw, names: Sequence[str] | None) -> list[Variant]:
    """按命令行给的名字挑变体。不给名字 = 全部。

    名字写错时要把可用的名字列出来：成员在命令行上敲错一个字母，
    得到的如果是"0 个变体"，他会以为配置没生效。
    """
    known = parse_variants(raw)
    if not known:
        raise ConfigError(
            "配置里没有 variants 段。先在 configs/tiers.yaml 里定义变体，例如：\n"
            "  variants:\n"
            "    base: {}\n"
            "    detail:\n"
            "      prompt_note: \"Also describe the colour palette.\""
        )
    if not names:
        return known

    by_name = {v.name: v for v in known}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise ConfigError(
            f"未知变体: {', '.join(missing)}。可用：{', '.join(by_name)}")
    # 保序去重：命令行写了两遍同一个变体，不该跑两遍覆盖同一份产物。
    picked: list[Variant] = []
    seen: set[str] = set()
    for n in names:
        if n not in seen:
            seen.add(n)
            picked.append(by_name[n])
    return picked


def pick_variant(raw, name: str) -> Variant:
    if not name:
        raise ConfigError("变体名为空")
    for v in parse_variants(raw):
        if v.name == name:
            return v
    raise ConfigError(f"未知变体 {name!r}。可用：{', '.join(v.name for v in parse_variants(raw)) or '（无）'}")


# ------------------------------------------------------------------ 对比


def _variant_metrics(points) -> dict:
    from .report import build_report

    report = build_report(points, batch_size=max(1, len(points)), order="id")
    totals = report["totals"]
    return {
        "samples": totals["samples"],
        "avg_tag_count": totals["avg_tag_count"],
        "avg_chars": totals["avg_chars"],
        "avg_nl_tokens": totals["avg_nl_tokens"],
        "avg_score": totals["avg_score"],
        "complete_rate": totals["complete_rate"],
        "flag_rate": totals["flag_rate"],
    }


def compare_variants(
    root: str | Path,
    names: Sequence[str],
    *,
    unreadable: list[str] | None = None,
) -> dict:
    """逐个变体目录统计，算出与基线（第一个变体）的相对差。

    目录缺失不报错而记 0 条：AB 跑到一半被打断是正常情况，报告应当
    如实显示"这个变体没产出"，而不是让对比命令本身失败。
    """
    from .report import collect_points

    base_dir = Path(root)
    bad = unreadable if unreadable is not None else []
    rows: list[dict] = []

    for name in names:
        d = base_dir / name
        if not d.is_dir():
            bad.append(f"{d}: 变体目录不存在")
            points = []
        else:
            points = collect_points([d], unreadable=bad)
        rows.append({"variant": name, **_variant_metrics(points)})

    if rows:
        base = rows[0]
        for row in rows[1:]:
            # 任一侧没有样本就不算相对差：0 条 vs 0 条算出来的 +0%
            # 会被当成"两个变体一样好"，而实际是"一个都没跑出来"。
            if not row.get("samples") or not base.get("samples"):
                continue
            delta: dict[str, float] = {}
            for key in DELTA_KEYS:
                b = float(base.get(key) or 0.0)
                c = float(row.get(key) or 0.0)
                # 基线为 0 时给 0 而不是 inf：除零算出来的数在表里没有意义。
                delta[key] = round((c - b) / b, 4) if b else 0.0
            row["delta_vs_base"] = delta

    return {
        "schema": 1,
        "root": str(base_dir),
        "baseline": rows[0]["variant"] if rows else "",
        # 列定义随数据一起下发，前端不必硬编码指标名与顺序。
        "columns": [{"key": k, "label": v} for k, v in COMPARE_COLUMNS],
        "variants": rows,
        "unreadable": bad,
    }


def render_compare_markdown(result: Mapping[str, Any]) -> str:
    rows = result.get("variants") or []
    lines = [
        f"# 变体对比（基线：`{result.get('baseline') or '-'}`）",
        "",
        "同一批输入、同一模型，只有指令不同。相对差以基线为 100%，"
        "只用于筛掉明显跑崩的变体；最终取舍仍看抽检清单。",
        "",
        "| 变体 | 条数 | tag | 字符 | token | 分 | 完整率 | 标记率 | 相对基线（tag/字符/分/完整率） |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        delta = row.get("delta_vs_base") or {}
        if delta:
            d = "/".join(f"{delta.get(k, 0.0):+.0%}" for k in DELTA_KEYS)
        else:
            d = "—"
        cells = [
            f"`{row['variant']}`",
            str(row.get("samples", 0)),
            f"{row.get('avg_tag_count', 0):.1f}",
            f"{row.get('avg_chars', 0):.0f}",
            f"{row.get('avg_nl_tokens', 0):.0f}",
            f"{row.get('avg_score', 0):.2f}",
            f"{row.get('complete_rate', 0):.1%}",
            f"{row.get('flag_rate', 0):.1%}",
            d,
        ]
        lines.append("| " + " | ".join(cells) + " |")

    bad = result.get("unreadable") or []
    if bad:
        lines.extend(["", f"读不动 {len(bad)} 项：", ""])
        lines.extend(f"- {b}" for b in bad[:20])
        if len(bad) > 20:
            lines.append(f"- … 其余 {len(bad) - 20} 项略")
    return "\n".join(lines) + "\n"


def write_compare(result: Mapping[str, Any], *,
                  json_out: str | Path | None = None,
                  md_out: str | Path | None = None) -> list[Path]:
    written: list[Path] = []
    if json_out:
        p = Path(json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        written.append(p)
    if md_out:
        p = Path(md_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_compare_markdown(result), encoding="utf-8")
        written.append(p)
    return written


def summary_lines(result: Mapping[str, Any]) -> list[str]:
    rows = result.get("variants") or []
    if not rows:
        return ["（没有变体产出）"]
    lines = [f"基线 : {result.get('baseline') or '-'}", ""]
    for row in rows:
        lines.append(
            f"  {row['variant']:<12} {row.get('samples', 0):>7} 条  "
            f"tag {row.get('avg_tag_count', 0):>5.1f}  "
            f"字符 {row.get('avg_chars', 0):>6.0f}  "
            f"分 {row.get('avg_score', 0):>4.2f}  "
            f"完整 {row.get('complete_rate', 0):>6.1%}"
        )
    return lines
