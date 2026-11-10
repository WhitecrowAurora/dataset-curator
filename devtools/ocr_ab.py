"""OCR 对比实验：同一张图，Qwen-VL / Manga-OCR 各出什么字。

判定的是**逐字准确率**，不是"意思对不对"。所以期待值按字符比较，
一处错字就算这一条错——这正是当前 Qwen-VL 方案最薄弱的地方
（认不出就编一个看起来合理的词）。

Manga-OCR 装在 `work/_ocr_env`（--target 隔离安装），不污染主环境。
它只做"识别"，不做"检测"：整张图丢进去它会当成一页漫画去找文字，
所以这里同时支持 --crop 手工框选，用来测"框对了之后它认得准不准"。

用法：
    python devtools/ocr_ab.py --image work/_text_test/ja_ramen.jpg
    python devtools/ocr_ab.py --image work/_hand.png --crop 0,0,900,220
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

REPO = Path(__file__).resolve().parent.parent
# manga_ocr 的依赖目录：只放它自己 + 主环境没有的几个小包（fire/fugashi/
# unidic_lite/jaconv 等）。torch、transformers、PIL 一律复用主环境——
# 装一整套会带进另一个版本的 torchvision，报 `torchvision::nms does not exist`。
OCR_ENV = REPO / "work" / "_ocr_lite"
if OCR_ENV.is_dir() and str(OCR_ENV) not in sys.path:
    sys.path.insert(0, str(OCR_ENV))

# 期待原文：(整图, 裁切框) 里的真实文字。字符级比较。
EXPECT = {
    "zh_sign": ["欢迎光临"],
    "en_sign": ["KEEP OUT", "DO NOT ENTER"],
    "ja_ramen": ["ラーメン", "営業中"],
    "ko_greeting": ["안녕하세요", "커피숍"],
    "mixed_sign": ["WELCOME", "欢迎光临", "ようこそ", "환영합니다"],
    "small_multiline": ["TERMS AND CONDITIONS", "No refunds after 30 days.",
                        "Keep your receipt.", "Prices may change.",
                        "Please ask staff for help."],
    "italic_watermark": ["Sample Text", "DRAFT ONLY", "Copyright 2026"],
}


def _norm(s: str) -> str:
    """比较用归一化：去掉空白与常见标点差异，其余一律照比。"""
    keep = []
    for ch in s:
        if ch.isspace():
            continue
        keep.append(ch)
    return "".join(keep)


def char_accuracy(got: str, want: str) -> float:
    """逐字准确率：最长公共子序列 / 期望长度。抄多抄少都扣分。"""
    a, b = _norm(got), _norm(want)
    if not b:
        return 0.0
    prev = [0] * (len(a) + 1)
    for i in range(1, len(b) + 1):
        cur = [0] * (len(a) + 1)
        for j in range(1, len(a) + 1):
            cur[j] = (prev[j - 1] + 1 if b[i - 1] == a[j - 1]
                      else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[len(a)] / len(b)


def run_manga(image: Path, crop: tuple[int, int, int, int] | None) -> str:
    from manga_ocr import MangaOcr
    from PIL import Image

    im = Image.open(image).convert("RGB")
    if crop:
        im = im.crop(crop)
    ocr = MangaOcr()
    return ocr(im)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--crop", default="", help="x1,y1,x2,y2")
    ap.add_argument("--expect", default="", help="手动指定期待原文，逗号分隔")
    args = ap.parse_args()

    path = Path(args.image)
    crop = None
    if args.crop:
        crop = tuple(int(v) for v in args.crop.split(","))  # type: ignore[assignment]

    expect = ([e.strip() for e in args.expect.split("|") if e.strip()]
              or EXPECT.get(path.stem, []))

    print(f"图: {path.name}  裁切: {crop or '整图'}")
    text = run_manga(path, crop)
    print(f"\nManga-OCR 输出:\n  {text!r}\n")

    if not expect:
        print("（没有期待值，只看输出）")
        return 0

    print(f"{'期望':<28} {'逐字准确率':>8}")
    print("-" * 42)
    for want in expect:
        acc = char_accuracy(text, want)
        flag = "OK " if acc >= 0.95 else "错"
        print(f"{want:<28} {acc:>7.0%}  {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
