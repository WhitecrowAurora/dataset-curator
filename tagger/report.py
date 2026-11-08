"""批次报告：把一堆产出折成"每 N 条一格"的时间序列。

为什么要分批
------------
三千万条的总体均值没有信息量：它把所有变化都摊平了。真正要看的是
**趋势**——第 3 批还正常，第 40 批 tag 数突然掉到 8 个，那多半是服务端
换了模型、显存吃紧降了分辨率、或者有人在半路改了 prompt。

所以这里不输出"一个总数"，而是把产出按处理顺序切成等长批次，每批给
一组指标。批次大小是配置项（report.batch_size），因为它是**尺度**问题：
    - 1000 条/批 → 3000 万条约 3 万格，适合看全局漂移，单个作者的影响被平均
    - 100 条/批  → 30 万格，能定位到具体某几批，适合试跑与排查
格越小越灵敏，也越吵。选多大的判断标准是"你希望多快发现变化"。

输出两份，各有各的用处
----------------------
    report.json  给 UI。列式（series + metrics）而不是行式：
                 前端画折线图时直接 x = series[i].batch、y = metrics[key].values[i]，
                 不需要先做一次 transpose。
    report.md    给人。批次多了 markdown 会很长，所以只列前 top 批与异常批。

数据来源两种
------------
    artifacts  成员产物目录（<id>-tag.json / <id>-nl.json）。字段最全，
               有 score / flags / nl_tokens，是首选。
    archives   封口后的 .tar / .zip。只有文本成员，指标退化成"条数对不对"，
               但它是**最终交付物**，验收时以它为准。

两种来源都不做"修复"：读不动就跳过并计数。报告的价值在于如实反映，
不在于把缺口补平。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .artifacts import find_artifacts, iter_artifact_rows, parse_payload

TAG_SUFFIX = "-tag.json"
NL_SUFFIX = "-nl.json"

# 完整率判定口径。必须和 merge 的 require 对齐（都源自 output.mode）：
# both 模式下"只有 tag 没 NL"是不完整，而 any 口径会把它算成完整，
# 同一批数据在报告与合并对账里就会出现两个打架的完整率。
REQUIRE_MODES = ("both", "nl", "tag", "any")

# 报告里认这些指标。顺序即 markdown 列顺序，也是 UI 图例顺序。
METRICS: tuple[tuple[str, str], ...] = (
    ("avg_tag_count", "平均 tag 数"),
    ("avg_chars", "平均字符数"),
    ("avg_nl_tokens", "平均 NL token"),
    ("avg_score", "平均质检分"),
    ("complete_rate", "完整率"),
    ("flag_rate", "带标记比例"),
)


@dataclass(slots=True)
class SamplePoint:
    """一条产出的可统计面。

    刻意只留数值与布尔，不留文本：报告要能扫过百万条而不吃内存，
    留 caption 进来只会让报告变成第二份数据集。
    """

    image_id: str
    tag_count: int = 0
    chars: int = 0
    nl_tokens: int = 0
    score: float = 0.0
    flags: tuple[str, ...] = ()
    ok: bool = True
    ts: float = 0.0

    @property
    def flagged(self) -> bool:
        return bool(self.flags)


# ------------------------------------------------------------------ 读产物


def _id_from_name(name: str) -> str:
    for suffix in (TAG_SUFFIX, NL_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return ""


def _read_json(path: Path) -> tuple[dict | None, str]:
    """返回 (对象, 纯文本)。坏文件返回 (None, "")，由调用方计入 unreadable。"""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None, ""
    if not raw:
        return None, ""
    try:
        obj = json.loads(raw)
    except ValueError:
        return None, raw
    if isinstance(obj, str):
        return None, obj
    return (obj if isinstance(obj, dict) else None), ""


def _count_tags_tag_file(obj: dict | None) -> int:
    """tag 数取 `tags` 数组长度；没有数组时退回数逗号。

    为什么不直接用 `tag_count` 字段（那是 NL 文件里的）：它记的是"当时
    一共用了几个 tag"，而 tag 文件才是真正落盘的那一份。两者理论上
    相等，实际会因为截断/过滤而不等——报告应当反映落盘结果。
    """
    if not obj:
        return 0
    tags = obj.get("tags")
    if isinstance(tags, list):
        return len(tags)
    text = str(obj.get("tag") or "")
    return len([t for t in text.split(",") if t.strip()])


def _complete_of(p: "SamplePoint", require: str) -> bool:
    """按 require 口径判一条产出是否完整。

    require 是**配置声明该产什么**：both = tag 与 NL 都得有内容。
    以前 json 路径把"扫到的文件必须都非空"AND 起来、归档路径却用 OR，
    同一批数据两种来源给出两个完整率；而且 json 路径对"少了一整个
    文件"的样本反而算完整（没扫到就不参与 AND）。
    """
    if require == "tag":
        return p.tag_count > 0
    if require == "nl":
        return p.chars > 0
    if require == "both":
        return p.tag_count > 0 and p.chars > 0
    return p.tag_count > 0 or p.chars > 0


def collect_points(roots: Iterable[str | Path], *,
                   unreadable: list[str] | None = None,
                   require: str = "any") -> list[SamplePoint]:
    """扫成员产物目录。同一 image_id 的 tag/nl 两份合成一个点。

    require 决定完整率口径（见 _complete_of）：调用方应传 output.mode。
    """
    if require not in REQUIRE_MODES:
        raise ValueError(f"require 只能是 {' / '.join(REQUIRE_MODES)}，收到 {require!r}")
    points: dict[str, SamplePoint] = {}
    bad = unreadable if unreadable is not None else []

    for root in roots:
        base = Path(root)
        if not base.exists():
            bad.append(f"{base}: 目录不存在")
            continue
        for path in sorted(base.rglob("*.json")):
            name = path.name
            if name.endswith(TAG_SUFFIX):
                kind = "tag"
            elif name.endswith(NL_SUFFIX):
                kind = "nl"
            else:
                continue

            obj, text = _read_json(path)
            if obj is None and not text:
                bad.append(f"{path}: 空文件或读不动")
                continue
            image_id = str((obj or {}).get("image_id") or _id_from_name(name))
            if not image_id:
                bad.append(f"{path}: 取不到 image_id")
                continue

            p = points.get(image_id)
            if p is None:
                p = SamplePoint(image_id=image_id)
                points[image_id] = p
            p.ts = max(p.ts, float((obj or {}).get("ts") or 0.0))

            if kind == "tag":
                if obj is None:
                    # merge 能收裸文本产物（成员手改过 / 工具直接写），
                    # 报告也要认，否则这类文件会被算成"0 个 tag"。
                    count = len([t for t in text.split(",") if t.strip()])
                else:
                    count = _count_tags_tag_file(obj)
                p.tag_count = max(p.tag_count, count)
            else:
                # chars 优先取落盘时记的字段；没有就数 caption 本身。
                # 只退到 len(text) 没有意义——obj 解析成功时 text 恒为空串。
                chars = (int((obj or {}).get("chars") or 0)
                         or len(str((obj or {}).get("caption") or ""))
                         or len(text))
                p.chars = max(p.chars, chars)
                p.nl_tokens = max(p.nl_tokens, int((obj or {}).get("nl_tokens") or 0))
                p.score = max(p.score, float((obj or {}).get("score") or 0.0))
                flags = (obj or {}).get("flags")
                if isinstance(flags, list):
                    p.flags = tuple(str(f) for f in flags)

        # 分片形态（members 直接交 artifacts-*.parquet / *.jsonl）。几种形态可能
        # 同时存在：换了 --artifact-format 重跑时会这样，报告按"取最大"合并即可。
        for path in find_artifacts(base):
            try:
                for row in iter_artifact_rows(path):
                    image_id = str(row.get("image_id") or "")
                    if not image_id:
                        continue
                    p = points.get(image_id)
                    if p is None:
                        p = SamplePoint(image_id=image_id)
                        points[image_id] = p
                    p.ts = max(p.ts, float(row.get("ts") or 0.0))
                    tag_obj = parse_payload("tag", str(row.get("tag") or ""))
                    if tag_obj is not None:
                        p.tag_count = max(p.tag_count, _count_tags_tag_file(tag_obj))
                    nl_obj = parse_payload("nl", str(row.get("nl") or ""))
                    if nl_obj is not None:
                        chars = (int(nl_obj.get("chars") or 0)
                                 or len(str(nl_obj.get("caption") or "")))
                        p.chars = max(p.chars, chars)
                        p.nl_tokens = max(p.nl_tokens,
                                          int(nl_obj.get("nl_tokens") or 0))
                        p.score = max(p.score, float(nl_obj.get("score") or 0.0))
                        flags = nl_obj.get("flags")
                        if isinstance(flags, list):
                            p.flags = tuple(str(f) for f in flags)
            except Exception as exc:      # noqa: BLE001 - 坏片记下来，别中断报告
                bad.append(f"{path}: {exc}")

    # 完整率在全部来源合并完之后统一判定，而不是扫文件时顺手写：
    # 同一个点的两份文件可能分属不同目录，扫到第一份时还下不了结论。
    for p in points.values():
        p.ok = _complete_of(p, require)
    return list(points.values())


def collect_points_from_archives(out_dir: str | Path, *,
                                 unreadable: list[str] | None = None,
                                 require: str = "any") -> list[SamplePoint]:
    """扫封口后的 .tar / .zip。没有 score/flags，指标会少两列。"""
    from .verify import iter_samples

    if require not in REQUIRE_MODES:
        raise ValueError(f"require 只能是 {' / '.join(REQUIRE_MODES)}，收到 {require!r}")
    bad = unreadable if unreadable is not None else []
    points: list[SamplePoint] = []
    for stem, members in iter_samples(out_dir):
        tag_text = members.get(".tag", b"").decode("utf-8", "replace").strip()
        nl_text = members.get(".nl", b"").decode("utf-8", "replace").strip()
        meta: dict = {}
        raw = members.get(".json")
        if raw:
            try:
                obj = json.loads(raw.decode("utf-8"))
                if isinstance(obj, dict):
                    meta = obj
            except (UnicodeDecodeError, ValueError):
                bad.append(f"{stem}: .json 无法解析")
        image_id = str(meta.get("name") or stem)
        if not image_id:
            bad.append(f"{stem}: 取不到样本名")
            continue
        p = SamplePoint(
            image_id=image_id,
            tag_count=len([t for t in tag_text.split(",") if t.strip()]),
            chars=len(nl_text),
            ts=float(meta.get("ts") or 0.0),
        )
        # 与 collect_points 同一个口径函数：归档与产物目录不许出现
        # 两个完整率（以前这里是 OR，那边是 AND）。
        p.ok = _complete_of(p, require)
        points.append(p)
    return points


# ------------------------------------------------------------------ 统计


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _metric_row(points: Sequence[SamplePoint]) -> dict[str, float]:
    n = len(points)
    if not n:
        return {key: 0.0 for key, _ in METRICS} | {"count": 0.0}
    scored = [p.score for p in points if p.score > 0]
    return {
        "avg_tag_count": _mean([p.tag_count for p in points]),
        "avg_chars": _mean([p.chars for p in points]),
        "avg_nl_tokens": _mean([p.nl_tokens for p in points]),
        "avg_score": _mean(scored),
        "complete_rate": round(sum(1 for p in points if p.ok) / n, 4),
        "flag_rate": round(sum(1 for p in points if p.flagged) / n, 4),
    }


def sort_points(points: Sequence[SamplePoint], order: str = "id") -> list[SamplePoint]:
    """排序口径决定"批"的含义，必须显式选。

    id   —— 按 image_id。可复现，且同画师的图连在一起，适合比对批次间的
            内容差异（这批是不是某个作者）。
    time —— 按产出写入时间。反映服务状态的变化，排查"跑到一半变差"用它。
    """
    if order == "time":
        return sorted(points, key=lambda p: (p.ts or 0.0, p.image_id))
    return sorted(points, key=lambda p: p.image_id)


def split_batches(points: Sequence[SamplePoint],
                  batch_size: int) -> list[list[SamplePoint]]:
    if batch_size <= 0:
        raise ValueError("batch_size 必须为正整数")
    return [list(points[i:i + batch_size])
            for i in range(0, len(points), batch_size)]


def build_report(
    points: Sequence[SamplePoint],
    *,
    batch_size: int = 1000,
    order: str = "id",
    source: str = "",
    generated_at: float | None = None,
    extra: dict | None = None,
) -> dict:
    """折成给 UI 的结构。

    series 与 metrics 是同一份数据的两种摆法：前者按行（便于逐批读），
    后者按列（便于直接画线）。冗余几十 KB，换前端零加工。
    """
    ordered = sort_points(points, order)
    batches = split_batches(ordered, batch_size)

    series: list[dict] = []
    columns: dict[str, list[float]] = {key: [] for key, _ in METRICS}
    for i, batch in enumerate(batches, 1):
        row = {
            "batch": i,
            "start_id": batch[0].image_id,
            "end_id": batch[-1].image_id,
            "count": len(batch),
            **_metric_row(batch),
        }
        series.append(row)
        for key, _ in METRICS:
            columns[key].append(row[key])

    report = {
        "schema": 1,
        "generated_at": generated_at if generated_at is not None else time.time(),
        "source": source,
        "order": order,
        "batch_size": batch_size,
        "batch_count": len(batches),
        "totals": {
            "samples": len(ordered),
            **_metric_row(ordered),
        },
        "series": series,
        "metrics": {
            key: {"label": label, "values": columns[key]}
            for key, label in METRICS
        },
    }
    if extra:
        report["extra"] = extra
    return report


def render_markdown(report: dict, *, top: int = 20) -> str:
    """人读版。批次多时只列前 top 批 + 最好/最差批。"""
    totals = report.get("totals") or {}
    lines = [
        f"# 批次报告（每 {report.get('batch_size')} 条一批，"
        f"共 {report.get('batch_count')} 批）",
        "",
        f"- 来源：`{report.get('source') or '-'}`",
        f"- 排序：{report.get('order')}",
        f"- 样本：{totals.get('samples', 0)}",
        f"- 整体完整率：{totals.get('complete_rate', 0):.2%}",
        f"- 平均 tag 数：{totals.get('avg_tag_count', 0):.1f}",
        f"- 平均字符数：{totals.get('avg_chars', 0):.0f}",
        f"- 平均质检分：{totals.get('avg_score', 0):.2f}",
        "",
        "| 批 | 起止 | 条数 | tag | 字符 | token | 分 | 完整率 | 标记率 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    series = report.get("series") or []
    shown = series[:top]
    for row in shown:
        lines.append(
            f"| {row['batch']} | `{row['start_id']}` ~ `{row['end_id']}` | "
            f"{row['count']} | {row['avg_tag_count']:.1f} | {row['avg_chars']:.0f} | "
            f"{row['avg_nl_tokens']:.0f} | {row['avg_score']:.2f} | "
            f"{row['complete_rate']:.1%} | {row['flag_rate']:.1%} |"
        )
    if len(series) > top:
        lines.append(f"| … | 其余 {len(series) - top} 批略 | | | | | | | |")
    return "\n".join(lines) + "\n"


def write_report(report: dict, *, json_out: str | Path | None = None,
                 md_out: str | Path | None = None, top: int = 20) -> list[Path]:
    """落盘。两个路径都可省，返回实际写出的文件。"""
    written: list[Path] = []
    if json_out:
        p = Path(json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        written.append(p)
    if md_out:
        p = Path(md_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_markdown(report, top=top), encoding="utf-8")
        written.append(p)
    return written


def summary_lines(report: dict, *, top: int = 8) -> list[str]:
    totals = report.get("totals") or {}
    lines = [
        f"样本       : {totals.get('samples', 0)}  "
        f"（{report.get('batch_count', 0)} 批，每批 {report.get('batch_size')} 条）",
        f"完整率     : {totals.get('complete_rate', 0):.2%}",
        f"平均 tag 数: {totals.get('avg_tag_count', 0):.1f}",
        f"平均字符数 : {totals.get('avg_chars', 0):.0f}",
        f"平均质检分 : {totals.get('avg_score', 0):.2f}",
    ]
    series = report.get("series") or []
    if series:
        # 极值批单独点出来：趋势图看斜率，人眼先要知道"哪个批最差"。
        worst = min(series, key=lambda r: r["complete_rate"])
        lines.append(f"最差批     : #{worst['batch']} 完整率 {worst['complete_rate']:.1%}"
                     f"（{worst['start_id']} ~ {worst['end_id']}）")
    return lines
