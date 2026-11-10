"""手写体识别 A/B：同一张手写图分别发给两个端点，比较识别结果。

量化对手写体的影响通常集中在"认不出的字怎么处理"上：Q6 会编一个
看起来合理的词填上，FP16 更可能承认认不出。所以这个脚本除了打印原文，
还统计三件事：

- 是否出现 `text partially illegible`（承认认不出）
- 引号内文本的数量与长度
- 引号后是否加了括注（读音/译文，规则明令禁止）

用法：
    python devtools/handwriting_ab.py --image <路径> \\
        --a http://127.0.0.1:8081 --a-model f16 \\
        --b http://127.0.0.1:8080 --b-model q6
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from tagger.client import SYSTEM_PROMPT, _prompt_for  # noqa: E402
from tagger.config import OutputSpec  # noqa: E402

CAP_RE = re.compile(r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|\Z)", re.S | re.I)
QUOTE_RE = re.compile(r'["“]([^"”]{1,120})["”]')
GLOSS_RE = re.compile(r'["“][^"”]{1,80}["”]\s*\(([^)]{1,60})\)')


def ask(url: str, model: str, blob: str, mime: str) -> tuple[str, float]:
    import time
    prompt = _prompt_for(OutputSpec(mode="nl", text_rules="图中文字规则"))
    t0 = time.perf_counter()
    r = httpx.post(f"{url.rstrip('/')}/v1/chat/completions", json={
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{blob}"}},
                {"type": "text", "text": prompt}]},
        ],
        "temperature": 0.0,
        "max_tokens": 900,
    }, timeout=900)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    return text, time.perf_counter() - t0


def report(tag: str, raw: str, dt: float) -> dict:
    m = CAP_RE.search(raw)
    cap = m.group(1).strip() if m else raw.strip()
    quotes = [q.strip() for q in QUOTE_RE.findall(cap)]
    gloss = [g for g in GLOSS_RE.findall(cap)]
    illegible = "partially illegible" in cap.lower()

    print(f"\n{'=' * 70}\n[{tag}]  {dt:.1f}s\n{'=' * 70}")
    print(cap)
    print(f"\n  -- 统计 --")
    print(f"  引号原文 {len(quotes)} 处: {quotes if quotes else '无'}")
    print(f"  承认认不出(illegible): {illegible}")
    print(f"  违规括注: {gloss or '无'}")
    return {"tag": tag, "caption": cap, "quotes": quotes,
            "illegible": illegible, "gloss": gloss, "sec": round(dt, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--a", default="http://127.0.0.1:8081")
    ap.add_argument("--a-model", default="f16")
    ap.add_argument("--b", default="")
    ap.add_argument("--b-model", default="q6")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    src = Path(args.image)
    blob = base64.b64encode(src.read_bytes()).decode()
    mime = mimetypes.guess_type(src.name)[0] or "image/png"

    rows = [report(f"A {args.a_model}", *ask(args.a, args.a_model, blob, mime))]
    if args.b:
        rows.append(report(f"B {args.b_model}", *ask(args.b, args.b_model, blob, mime)))

    if len(rows) == 2:
        a, b = rows
        print(f"\n{'=' * 70}\n对比\n{'=' * 70}")
        print(f"  引号数      : {len(a['quotes'])} vs {len(b['quotes'])}")
        print(f"  illegible   : {a['illegible']} vs {b['illegible']}")
        print(f"  违规括注    : {len(a['gloss'])} vs {len(b['gloss'])}")
        print(f"  唯一原文 A  : {sorted(set(a['quotes']) - set(b['quotes']))}")
        print(f"  唯一原文 B  : {sorted(set(b['quotes']) - set(a['quotes']))}")

    if args.out:
        import json
        Path(args.out).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
