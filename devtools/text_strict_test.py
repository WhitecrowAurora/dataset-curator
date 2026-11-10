"""严格版文字测试：除了"原文是否保留"，还查三件事。

1. 引号后有没有加括注（读音/拼音/译文）——规则明令禁止。
2. 该出的地方有没有出 `text partially illegible`——认不准时不许编。
3. caption 里有没有字体风格描述——新加的规则要求。

用法：
    python devtools/text_strict_test.py --url http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
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

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
CAP_RE = re.compile(r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|\Z)", re.S | re.I)

# 引号后紧跟括号：`"欢迎光临" (huānyíng guānglín)` / `"ライブ2D" (Live2D)`。
GLOSS_RE = re.compile(r'["“][^"”]{1,60}["”]\s*\(([^)]{1,80})\)')

# 字体风格词表。命中任意一个就算提到了字体。
TYPE_WORDS = (
    "font", "typeface", "serif", "sans-serif", "italic", "handwritten", "hand-written",
    "script", "bold", "uppercase", "lowercase", "lettering", "typography",
    "watermark", "outlined", "stroke", "monospace", "print", "block letter",
    "字体", "手写", "衬线", "无衬线", "斜体", "加粗", "描边", "水印", "印刷体",
)

# 每张图的期待：必须出现的原文、是否允许出现 illegible 标记、是否该有字体描述。
CASES = {
    "zh_sign": {"expect": ["欢迎光临"], "type_font": True},
    "en_sign": {"expect": ["KEEP OUT", "DO NOT ENTER"], "type_font": True},
    "ja_ramen": {"expect": ["ラーメン", "営業中"], "type_font": True},
    "ko_greeting": {"expect": ["안녕하세요", "커피숍"], "type_font": True},
    "mixed_sign": {"expect": ["WELCOME", "欢迎光临", "ようこそ", "환영합니다"],
                   "type_font": True},
    "small_multiline": {"expect": ["TERMS AND CONDITIONS", "No refunds"], "type_font": True},
    "italic_watermark": {"expect": ["Sample Text", "DRAFT ONLY"], "type_font": True},
}


def has_font_word(caption: str) -> str:
    low = caption.lower()
    for w in TYPE_WORDS:
        if w in low:
            return w
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", default="qwen3vl-8b")
    ap.add_argument("--images", default=str(REPO / "work/_text_test"))
    ap.add_argument("--out", default=str(REPO / "run/text_strict.jsonl"))
    args = ap.parse_args()

    prompt = _prompt_for(OutputSpec(mode="nl"))
    rows: list[dict] = []
    n_pass = 0

    with httpx.Client(timeout=300) as http:
        for path in sorted(Path(args.images).iterdir()):
            if path.suffix.lower() not in IMG_EXT or path.stem not in CASES:
                continue
            case = CASES[path.stem]
            blob = base64.b64encode(path.read_bytes()).decode()
            t0 = time.perf_counter()
            r = http.post(f"{args.url.rstrip('/')}/v1/chat/completions", json={
                "model": args.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{blob}"}},
                        {"type": "text", "text": prompt}]},
                ],
                "temperature": 0.2, "max_tokens": 800,
            })
            dt = time.perf_counter() - t0
            if r.status_code >= 400:
                print(f"[ERROR] {path.name}: HTTP {r.status_code}")
                continue
            text = r.json()["choices"][0]["message"]["content"]
            m = CAP_RE.search(text)
            caption = m.group(1).strip() if m else ""

            miss = [s for s in case["expect"] if s.lower() not in caption.lower()]
            gloss = [g.group(1) for g in GLOSS_RE.finditer(caption)]
            font = has_font_word(caption)
            illegible = "partially illegible" in caption.lower()

            problems = []
            if not caption:
                problems.append("空 caption")
            if miss:
                problems.append(f"缺失原文 {miss}")
            if gloss:
                problems.append(f"括注 {gloss}")
            if case.get("type_font") and not font:
                problems.append("没描述字体风格")

            ok = not problems
            n_pass += ok
            print(f"\n[{'PASS' if ok else 'FAIL'}] {path.name}  ({dt:.1f}s)")
            if problems:
                for p in problems:
                    print(f"    × {p}")
            print(f"    字体词: {font or '无'}    illegible 标记: {illegible}")
            print(f"    {caption[:300]}")
            rows.append({"image": path.name, "ok": ok, "problems": problems,
                         "miss": miss, "gloss": gloss, "font": font,
                         "illegible": illegible, "caption": caption})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows),
                   encoding="utf-8")
    print(f"\n=== 通过 {n_pass}/{len(rows)} ===")
    return 0 if n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
