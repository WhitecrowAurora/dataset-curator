"""实测：图中文字是否按原本语言写进 caption 正文（主创点名要验的一项）。

前提：本地推理服务已经起来（vLLM / llama.cpp 的 OpenAI 兼容端口）。

用法：
    python devtools/text_in_caption_test.py --url http://127.0.0.1:8000
    python devtools/text_in_caption_test.py --url http://127.0.0.1:8000 \
        --images work/_text_test --out run/text_test.jsonl
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
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
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
# 引号后紧跟括号读音/拼音，例如 `"欢迎光临" (huānyíng guānglín)`。
# 规则明令禁止：原文照抄，不加音译。命中即判失败。
PINYIN_RE = re.compile(r'[""][^""]{1,40}[""]\s*\(([^)]{1,60})\)')

# 每张图期待在 caption 里"原样出现"的文字。
# 判定是子串匹配（大小写不敏感）——正文可能把它放进更长的句子里。
EXPECT = {
    "zh_sign": ["欢迎光临"],
    "en_sign": ["KEEP OUT", "DO NOT ENTER"],
    "ja_ramen": ["ラーメン", "営業中"],
    "ko_greeting": ["안녕하세요", "커피숍"],
    "mixed_sign": ["WELCOME", "欢迎光临", "ようこそ", "환영합니다"],
    "small_multiline": ["TERMS AND CONDITIONS", "No refunds", "Keep your receipt"],
    "italic_watermark": ["Sample Text", "DRAFT ONLY", "Copyright 2026"],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="OpenAI 兼容服务地址")
    ap.add_argument("--model", default="qwen3vl-8b")
    ap.add_argument("--images", default=str(REPO / "work/_text_test"))
    ap.add_argument("--out", default=str(REPO / "run/text_test.jsonl"))
    args = ap.parse_args()

    prompt = _prompt_for(OutputSpec(mode="nl"))
    rows: list[dict] = []
    ok_all = True

    with httpx.Client(timeout=120) as http:
        for path in sorted(Path(args.images).iterdir()):
            if path.suffix.lower() not in IMG_EXT:
                continue
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
                "temperature": 0.2,
                "max_tokens": 800,
            })
            dt = time.perf_counter() - t0
            if r.status_code >= 400:
                print(f"[失败] {path.name}: HTTP {r.status_code} {r.text[:200]}")
                ok_all = False
                continue
            text = r.json()["choices"][0]["message"]["content"]
            m = CAP_RE.search(text)
            caption = m.group(1).strip() if m else ""
            expect = EXPECT.get(path.stem, [])
            hits = [s for s in expect if s.lower() in caption.lower()]
            miss = [s for s in expect if s not in hits]
            # 规则禁止的：引号后面加读音括注。
            pinyin = [m.group(1) for m in PINYIN_RE.finditer(caption)]
            ok = bool(caption) and not miss and not pinyin
            ok_all = ok_all and ok
            mark = "PASS" if ok else "FAIL"
            print(f"\n[{mark}] {path.name}  ({dt:.1f}s)")
            print(f"  期待原文: {expect}")
            print(f"  命中: {hits or '无'}  缺失: {miss or '无'}")
            if pinyin:
                print(f"  [违规] 加了读音括注: {pinyin}")
            print(f"  caption: {caption[:300]}")
            rows.append({"image": path.name, "expect": expect,
                         "hit": hits, "miss": miss, "pinyin": pinyin,
                         "caption": caption, "raw": text, "ok": ok})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows),
                   encoding="utf-8")
    print(f"\n=== 结果：{'全部通过' if ok_all else '有未命中的'} ===")
    print(f"明细已写入 {out}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
