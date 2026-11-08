"""NL 产出的要素覆盖与长度分布：一份 jsonl 单独看，两份配对看。

**它回答的是"指令有没有被执行"，nl_ab.py compare 回答的是"产出等不等价"。**
两件事要分开：一条 prompt 可以让产出等价，却把某类要素整体漏掉（v2 就是
这样——结构齐全、材质提得少）。

口径上的两个刻意选择：
1. **颜色只算有彩色**。white/black/grey 算进去的话几乎每张插画都命中（"白
   衬衫"、"黑发"），指标直接饱和，看不出差别。
2. **除了"有没有"，还报"平均有几个"**。v3 的材质条款要求的是"每个物体都给
   材质"，它影响密度而不只是有无；只看命中率会把改进压成 0pp。
3. **引号认弯引号**。模型输出 “RITZ” 的频率很高，只认 ASCII 双引号会把
   "照抄了但用了弯引号"误判成"没照抄"（第一版就踩了这个坑，虚报了 −5pp）。

用法：
    python devtools/nl_cover.py run\\ab\\v2.jsonl run\\ab\\v3.jsonl
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

CAP = re.compile(r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|\Z)", re.S | re.I)
QUOTED = re.compile(r'["“”「」『』][^"“”「」『』]{2,}["“”「」『』]')

CHROMATIC = """red blue green yellow orange purple pink brown gold golden silver
blonde crimson scarlet teal cyan magenta violet indigo beige cream ivory tan navy
maroon olive turquoise amber peach lavender rose copper bronze multicoloured
multicolored pastel neon""".split()

MATERIALS = """material texture matte glossy shiny metallic metal wooden wood
plastic glass leather silk satin cotton fabric cloth knit wool denim velvet
rubber ceramic porcelain stone concrete marble paper canvas steel iron brass
chrome translucent reflective fur feather scale glassy polished rough soft wet
dry""".split()

LIGHTING = """light lit lighting glow glowing shadow shadows sunlight sunlit
backlight backlit rim lamp illuminate illuminated neon candle moonlight
overcast bright dim dark dusk dawn chiaroscuro""".split()

COMPOSITION = """angle framing framed close-up closeup wide shot portrait
front view side view profile above overhead top-down low angle high angle
centered centred off-center off-centre bokeh depth of field foreground diagonal
symmetrical perspective fisheye cropped""".split()

SETTING = """background behind room wall walls outdoors indoors street forest
sky field window floor ceiling city interior exterior surroundings""".split()

NAMES = ["具名颜色", "材质/质地", "光线", "构图/机位", "背景/环境", "画面引文"]
TABLES = [CHROMATIC, MATERIALS, LIGHTING, COMPOSITION, SETTING]


def body_of(rec: dict) -> str:
    m = CAP.search(rec.get("text") or "")
    text = m.group(1) if m else (rec.get("text") or "")
    return " ".join(text.split())


def tokens_of(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z\-']*", text.lower())


def load(path: str) -> dict[str, dict]:
    rows = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            rows[rec["id"]] = rec
    return rows


def length_stats(path: str, rows: dict[str, dict]) -> None:
    lens = [len(body_of(rec)) for rec in rows.values()]
    over = sum(1 for x in lens if x > 1024)
    print(f" {Path(path).name:<16} n={len(lens):<4} 均值={statistics.mean(lens):>6.0f} "
          f"中位={statistics.median(lens):>6.0f} 最大={max(lens):>5} "
          f">1024={over}({100 * over / len(lens):.1f}%) "
          f"<400={sum(1 for x in lens if x < 400)}")


def main(argv: list[str]) -> int:
    arms = argv[1:]
    if not arms:
        raise SystemExit("用法：nl_cover.py A.jsonl [B.jsonl]")
    data = [load(p) for p in arms]

    print("长度分布：")
    for path, rows in zip(arms, data):
        length_stats(path, rows)

    if len(data) < 2:
        return 0
    common = sorted(set(data[0]) & set(data[1]))
    if not common:
        raise SystemExit("两份没有可配对样本")

    hit = [{k: 0 for k in range(len(NAMES))} for _ in range(2)]
    c_density = [[], []]
    m_density = [[], []]
    for sid in common:
        for arm in range(2):
            body = body_of(data[arm][sid])
            toks = tokens_of(body)
            for k, table in enumerate(TABLES):
                hit[arm][k] += 1 if any(t in table for t in toks) else 0
            hit[arm][5] += 1 if QUOTED.search(body) else 0
            c_density[arm].append(sum(1 for t in toks if t in CHROMATIC))
            m_density[arm].append(sum(1 for t in toks if t in MATERIALS))

    n = len(common)
    a_name, b_name = Path(arms[0]).stem, Path(arms[1]).stem
    print(f"\n要素覆盖（配对 n={n}）：")
    print(f" {'要素':<10}{a_name:>10}{b_name:>10}{'差':>9}")
    for k, name in enumerate(NAMES):
        a, b = hit[0][k] / n, hit[1][k] / n
        print(f" {name:<10}{a * 100:>9.0f}%{b * 100:>9.0f}%{(b - a) * 100:>+8.0f}pp")
    print()
    for label, a, b, fmt in (
            ("颜色词/张", statistics.mean(c_density[0]),
             statistics.mean(c_density[1]), "{:.2f}"),
            ("材质词/张", statistics.mean(m_density[0]),
             statistics.mean(m_density[1]), "{:.2f}")):
        print(f" {label:<10}{fmt.format(a):>10}{fmt.format(b):>10}{b - a:>+9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
