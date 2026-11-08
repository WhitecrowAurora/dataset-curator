"""NL 两条产出的配对 A/B：同一批图、同一份 prompt，只换模型/量化档位。

为什么必须配对
--------------
图片难度是最大的方差来源——一张图 30 个 tag、另一张 8 个，这个差异比
"Q4 还是 Q8"大得多。配对（同一张图跑两次）把这个方差抵掉大半，
所以同样本量下能测出更小的差异：n ≈ 8/d²，d=0.3 时 89 张、d=0.5 时 32 张。

三个指标各自回答一个问题
------------------------
1. **硬失败率**：空输出、缺 `<CAPTION>` 块、`<TAGS>` 泄漏进正文、乱码。
   这是"能不能用"，发生率 >5% 时几十张就能撞见。
2. **字符数 / tag 数**：产出是不是整体缩水了。缩水不报错，但会让下游
   检索与训练数据分布整个偏移。
3. **字符 3-gram Jaccard**：两次描述是不是在说同一件事。语言无关，
   比词重叠稳（中文没有空格）。低于 0.45 基本等于"重新写了一遍"。

用法
----
    # 采一条（服务先起好）
    python devtools/nl_ab.py collect --images H:\\素材 --url http://127.0.0.1:8080 ^
        --out run/ab-q4.jsonl

    # 比两条
    python devtools/nl_ab.py compare --a run/ab-q4.jsonl --b run/ab-q8.jsonl ^
        --label-a Q4_K_M --label-b Q8_0
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import httpx                          # noqa: E402

from tagger.client import (SYSTEM_PROMPT, _prompt_for,  # noqa: E402
                           _resize_to_budget, _sentences_for)
from tagger.config import OutputSpec  # noqa: E402

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

CAP_RE = re.compile(r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|\Z)", re.S | re.I)
TAGS_RE = re.compile(r"<TAGS>(.*?)(?:</TAGS>|\Z)", re.S | re.I)
SPLIT_RE = re.compile(r"[,，、;；\n]+")


# ------------------------------------------------------------------ 采集


def collect_images(paths: list[str], limit: int) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(sorted(q for q in p.iterdir()
                                if q.is_file() and q.suffix.lower() in IMG_EXT))
        elif p.is_file():
            found.append(p)
    if not found:
        raise SystemExit("没有找到图片")
    return found[:limit] if limit > 0 else found


def sample_identity(blob: bytes) -> str:
    """用内容哈希配对，而不是文件名——素材目录重命名过也不会错配。"""
    return hashlib.sha1(blob).hexdigest()[:16]


def cmd_collect(args: argparse.Namespace) -> int:
    files = collect_images(args.images, args.limit)
    if args.prompt_file:
        # 比"两版 prompt"时用：旧版指令直接从文件读，不必为了跑一次旧版
        # 就把工作区 checkout 回去——那样中途切分支/丢改动太容易出事。
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        if args.caption_range:
            raise SystemExit("--prompt-file 与 --caption-range 不能同时用："
                             "文件里的长度引导已经是定死的")
    else:
        prompt = _prompt_for(OutputSpec(mode=args.mode))
    if args.caption_range:
        # 只改"写多长"这一条，别的条款一个字不动 —— 否则测出来的差异
        # 归因不清（可能是换了内容要求，而不是换了长度）。
        lo, hi = args.caption_range
        prompt = re.sub(r"\d+-\d+ characters", f"{lo}-{hi} characters", prompt)
        if f"{lo}-{hi} characters" not in prompt:
            raise SystemExit("prompt 里没找到长度引导，--caption-range 无效")
        # 句数口径要跟着一起换：只改字符区间会留下"250-500 字符（约 4-9 句）"
        # 这种自相矛盾的指令，测出来的差异就不是"只换了长度"了。
        s_lo, s_hi = _sentences_for(lo, hi)
        prompt = re.sub(r"roughly \d+-\d+ sentences",
                        f"roughly {s_lo}-{s_hi} sentences", prompt)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 每条都带上 prompt 指纹：两份 jsonl 混着用时，能一眼看出哪条是哪套指令产的。
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    print(f"  prompt sha={prompt_sha}  {len(prompt)} 字符", flush=True)

    client = httpx.Client(timeout=args.timeout,
                          headers={"User-Agent": "tagger-ab"})

    def run_one(path: Path) -> dict:
        """一张图 = 一次请求。并发与串行走同一个函数，否则两条路会悄悄分叉。"""
        blob = path.read_bytes()
        small = (_resize_to_budget(blob, args.max_pixels)
                 if args.max_pixels else blob)
        b64 = base64.b64encode(small).decode("ascii")
        payload = {
            "model": args.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt}]},
            ],
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_tokens": args.max_tokens,
        }
        t0 = time.perf_counter()
        try:
            r = client.post(f"{args.url}/v1/chat/completions", json=payload)
            r.raise_for_status()
            body = r.json()
            text = body["choices"][0]["message"]["content"] or ""
            usage = body.get("usage") or {}
            return {"id": sample_identity(blob), "file": path.name,
                    "text": text,
                    "prompt_sha": prompt_sha,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "latency": round(time.perf_counter() - t0, 3),
                    "error": None}
        except Exception as exc:          # noqa: BLE001
            return {"id": sample_identity(blob), "file": path.name,
                    "text": "", "prompt_sha": prompt_sha,
                    "prompt_tokens": None,
                    "completion_tokens": None, "latency": None,
                    "error": f"{type(exc).__name__}: {exc}"[:200]}

    done = 0
    t_start = time.perf_counter()
    with out.open("w", encoding="utf-8") as fh:
        if args.concurrency <= 1:
            # 串行：延迟就是"一张图的真实墙钟"，A/B 比的是这个数，所以默认仍是 1。
            for i, path in enumerate(files, 1):
                rec = run_one(path)
                if not rec["error"]:
                    done += 1
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if i % 10 == 0 or i == len(files):
                    el = time.perf_counter() - t_start
                    print(f"  {i}/{len(files)}  {el:.0f}s  "
                          f"({i / el:.2f} 张/秒)", flush=True)
        else:
            # 并发：用于回答"这台机器一秒钟到底能出几张"。吞吐和单张延迟是
            # 两回事——4 路并发下每张可能要 3 秒，但合起来是 1.2 张/秒。
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                for i, rec in enumerate(pool.map(run_one, files), 1):
                    if not rec["error"]:
                        done += 1
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if i % 10 == 0 or i == len(files):
                        el = time.perf_counter() - t_start
                        print(f"  {i}/{len(files)}  {el:.0f}s  "
                              f"({i / el:.2f} 张/秒, 并发 {args.concurrency})",
                              flush=True)
    client.close()
    print(f"写入 {out}（{len(files)} 条，成功 {done}）")
    return 0


# ------------------------------------------------------------------ 判读


def parse(text: str) -> dict:
    cap = CAP_RE.search(text)
    body = (cap.group(1).strip() if cap else "")
    tags_m = TAGS_RE.search(text)
    tags = []
    if tags_m:
        tags = [t.strip() for t in SPLIT_RE.split(tags_m.group(1)) if t.strip()]
    shown = text[:0]
    # `<TAGS>` 出现在 caption 正文里，说明模型没按格式收口；
    # 出现在自己那块里是正常的，所以要基于剥离后的正文判断。
    leak = "<TAGS>" in body.upper()
    return {"body": body, "tags": tags, "leak": bool(leak),
            "has_caption": cap is not None, "raw": shown}


def trigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def load(path: str) -> dict[str, dict]:
    rows = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        rows[rec["id"]] = rec
    return rows


def effect_size(diffs: list[float]) -> float:
    """配对效应量 d = 均值 / 标准差。

    d 的读法（配对设计）：0.2 小、0.5 中、0.8 大。
    这里关心的是"差得够不够小"，所以看 |d| 是否 < 0.2。
    """
    if len(diffs) < 2:
        return 0.0
    sd = statistics.stdev(diffs)
    return (statistics.mean(diffs) / sd) if sd else 0.0


def cmd_compare(args: argparse.Namespace) -> int:
    a, b = load(args.a), load(args.b)
    common = sorted(set(a) & set(b))
    if not common:
        raise SystemExit("两份结果没有可配对的样本（内容哈希对不上）")

    rows = []
    for sid in common:
        pa, pb = parse(a[sid]["text"]), parse(b[sid]["text"])
        rows.append({
            "id": sid,
            "file": a[sid]["file"],
            "fail_a": not pa["body"] or pa["leak"] or bool(a[sid]["error"]),
            "fail_b": not pb["body"] or pb["leak"] or bool(b[sid]["error"]),
            "chars_a": len(pa["body"]), "chars_b": len(pb["body"]),
            "tags_a": len(pa["tags"]), "tags_b": len(pb["tags"]),
            "jac_tags": jaccard(set(pa["tags"]), set(pb["tags"])),
            "jac_tri": jaccard(trigrams(pa["body"]), trigrams(pb["body"])),
            "lat_a": a[sid]["latency"] or 0.0, "lat_b": b[sid]["latency"] or 0.0,
            "tok_a": a[sid]["completion_tokens"] or 0,
            "tok_b": b[sid]["completion_tokens"] or 0,
        })

    n = len(rows)
    la, lb = args.label_a, args.label_b

    diffs_chars = [r["chars_b"] - r["chars_a"] for r in rows]
    diffs_tags = [r["tags_b"] - r["tags_a"] for r in rows]
    diffs_tok = [r["tok_b"] - r["tok_a"] for r in rows]
    diffs_lat = [r["lat_b"] - r["lat_a"] for r in rows]

    fail_a = sum(r["fail_a"] for r in rows) / n
    fail_b = sum(r["fail_b"] for r in rows) / n

    print("=" * 74)
    print(f" 配对 A/B：{la}  vs  {lb}    n={n} 张（按内容哈希配对）")
    print("=" * 74)
    print(f" {'指标':<18}{la:>14}{lb:>14}{'差':>12}{'d':>8}")
    print("-" * 74)
    for label, key, diffs in (
            ("硬失败率", "fail", None),
            ("caption 字符", "chars", diffs_chars),
            ("tag 个数", "tags", diffs_tags),
            ("completion tok", "tok", diffs_tok),
            ("延迟 s", "lat", diffs_lat)):
        if key == "fail":
            print(f" {label:<18}{fail_a * 100:>13.1f}%{fail_b * 100:>13.1f}%"
                  f"{(fail_b - fail_a) * 100:>11.1f}%{'—':>8}")
            continue
        ma = statistics.mean(r[f"{key}_a"] for r in rows)
        mb = statistics.mean(r[f"{key}_b"] for r in rows)
        print(f" {label:<18}{ma:>14.2f}{mb:>14.2f}"
              f"{statistics.mean(diffs):>12.2f}{effect_size(diffs):>8.2f}")

    print("-" * 74)
    print(f" 同图一致性：tag Jaccard {statistics.mean(r['jac_tags'] for r in rows):.3f}"
          f"    字符 3-gram Jaccard {statistics.mean(r['jac_tri'] for r in rows):.3f}")
    print()

    # 判决：差得够小才算"等价"。阈值是事先定的，不是看数说话。
    d_chars = abs(effect_size(diffs_chars))
    d_tags = abs(effect_size(diffs_tags))
    verdict = []
    if max(fail_a, fail_b) > 0.02:
        verdict.append(f"硬失败率偏高（{max(fail_a, fail_b) * 100:.1f}%），先查数据")
    if d_chars >= 0.2:
        verdict.append(f"描述长度差异显著（|d|={d_chars:.2f}）")
    if d_tags >= 0.2:
        verdict.append(f"tag 数差异显著（|d|={d_tags:.2f}）")
    if statistics.mean(r["jac_tri"] for r in rows) < 0.45:
        verdict.append("两份描述重合度低（3-gram Jaccard < 0.45）")

    if verdict:
        print(" 结论：两条产出不等价——" + "；".join(verdict))
    else:
        print(" 结论：两条产出在事先定的阈值内等价"
              f"（|d|<0.2，Jaccard≥0.45，失败率≤2%），低档位可以放量。")
    print("=" * 74)

    out = Path(args.out) if args.out else Path(args.a).with_suffix(".compare.md")
    out.write_text(
        "\n".join([f"# {la} vs {lb}（n={n}）", "",
                   f"| 指标 | {la} | {lb} | 差 | d |",
                   "|---|---:|---:|---:|---:|",
                   f"| 硬失败率 | {fail_a * 100:.1f}% | {fail_b * 100:.1f}% | "
                   f"{(fail_b - fail_a) * 100:.1f}% | — |",
                   f"| caption 字符 | {statistics.mean(r['chars_a'] for r in rows):.1f} | "
                   f"{statistics.mean(r['chars_b'] for r in rows):.1f} | "
                   f"{statistics.mean(diffs_chars):.1f} | {effect_size(diffs_chars):.2f} |",
                   f"| tag 个数 | {statistics.mean(r['tags_a'] for r in rows):.2f} | "
                   f"{statistics.mean(r['tags_b'] for r in rows):.2f} | "
                   f"{statistics.mean(diffs_tags):.2f} | {effect_size(diffs_tags):.2f} |",
                   f"| 延迟 s | {statistics.mean(r['lat_a'] for r in rows):.2f} | "
                   f"{statistics.mean(r['lat_b'] for r in rows):.2f} | "
                   f"{statistics.mean(diffs_lat):.2f} | {effect_size(diffs_lat):.2f} |",
                   "",
                   f"tag Jaccard {statistics.mean(r['jac_tags'] for r in rows):.3f}"
                   f" · 字符 3-gram Jaccard "
                   f"{statistics.mean(r['jac_tri'] for r in rows):.3f}",
                   "", "判决：" + ("；".join(verdict) if verdict else "等价")]),
        encoding="utf-8")
    print(f" 报告写入 {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NL 产出配对 A/B")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="跑一批图，落 jsonl")
    c.add_argument("--images", nargs="+", required=True)
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--url", default="http://127.0.0.1:8080")
    c.add_argument("--model", default="tagger")
    c.add_argument("--mode", default="nl", choices=["nl", "tag", "both"])
    c.add_argument("--caption-range", type=int, nargs=2, default=None,
                   dest="caption_range", metavar=("LO", "HI"),
                   help="覆盖 prompt 里的长度引导，例如 --caption-range 250 500")
    c.add_argument("--prompt-file", default="", dest="prompt_file",
                   help="整段替换 prompt（比两版指令时用，不必 checkout 旧代码）")
    c.add_argument("--concurrency", type=int, default=1,
                   help="同时在飞的请求数。默认 1：A/B 比的是单张墙钟；"
                        "调大只影响吞吐（每张反而更慢一点）")
    c.add_argument("--max-pixels", type=int, default=589824, dest="max_pixels")
    c.add_argument("--max-tokens", type=int, default=800, dest="max_tokens")
    c.add_argument("--temperature", type=float, default=0.2)
    c.add_argument("--top-p", type=float, default=0.9, dest="top_p")
    c.add_argument("--timeout", type=float, default=180.0)
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_collect)

    m = sub.add_parser("compare", help="比两条 jsonl")
    m.add_argument("--a", required=True)
    m.add_argument("--b", required=True)
    m.add_argument("--label-a", default="A", dest="label_a")
    m.add_argument("--label-b", default="B", dest="label_b")
    m.add_argument("--out", default="")
    m.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
