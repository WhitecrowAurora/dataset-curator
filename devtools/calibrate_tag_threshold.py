"""阈值标定：0.35 这个数是拍出来的，这个脚本用来把它变成"选出来的"。

为什么需要单独一个脚本
----------------------
阈值是这份数据里**唯一一个直接决定 tag 集合、却从来没有证据支撑**的参数。
它的影响不小：79 张真实素材实测，0.35 → 0.45 会砍掉约 17% 的 tag
（3002 → 2479 个），而每一步 0.05 的变化都会动约 10%。

两个模式
--------
1) **没有标注**（默认）：只报结构 —— 每个阈值下每张图吐多少 tag、
   有多少张图"一个都没有"。够用来排除明显不合理的阈值，但**不足以定标**：
   没有对错信号，"38 个 tag/张"到底是多还是少，只能由人看着判断。

2) **有人工标注**（`--labels`）：直接算每个候选阈值下的 precision / recall，
   按 `--target-precision` 或 F1 推荐一个数。这是唯一能给结论的模式。

标注格式（jsonl，一行一张图）：

    {"image": "102.png", "tags": ["1girl", "solo", "long_hair"]}

`image` 用文件名即可（不带目录）。标注不要求全：
只有被标注到的图参与计算，标注里的 tag 视为"该有的全集"。

用法
----
    # 看结构（无标注）
    env\\python\\python.exe devtools\\calibrate_tag_threshold.py ^
        --checkpoint models\\wd-eva02 --images-dir <素材目录>

    # 定标（有标注）
    env\\python\\python.exe devtools\\calibrate_tag_threshold.py ^
        --checkpoint models\\wd-eva02 --images-dir <素材目录> ^
        --labels work/labels.jsonl --target-precision 0.9

结果怎么用
----------
打印出的阈值写进配置即可，按类别分：

    tagging:
      thresholds: {default: 0.35, character: 0.6}
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _images(root: Path, limit: int) -> list[Path]:
    files = [p for p in sorted(root.rglob("*")) if p.suffix.lower() in EXTS]
    if not files:
        raise SystemExit(f"{root} 下没有图片")
    return files[:limit] if limit else files


def _load_labels(path: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        key = str(obj.get("image") or obj.get("image_id") or "").strip()
        if not key:
            continue
        out[Path(key).name] = {str(t).strip().lower() for t in (obj.get("tags") or [])
                               if str(t).strip()}
    if not out:
        raise SystemExit(f"{path} 里没有读到任何标注")
    return out


def _by_category(backend) -> dict[str, str]:
    return {n: c for n, c in zip(backend._names, backend._categories)}


def _sweep_structure(rows: list[dict[str, float]], thresholds: list[float]) -> None:
    print(f"\n{'阈值':>6}{'tag/张(中位)':>14}{'总 tag':>9}{'空图':>7}{'<20 个':>8}")
    for thr in thresholds:
        counts = [sum(1 for v in row.values() if v >= thr) for row in rows]
        print(f"{thr:>6.2f}{statistics.median(counts):>14.0f}{sum(counts):>9}"
              f"{sum(1 for c in counts if c == 0):>7}{sum(1 for c in counts if c < 20):>8}")


def _sweep_labels(rows: dict[str, dict[str, float]],
                  labels: dict[str, set[str]], cat_of: dict[str, str],
                  thresholds: list[float], target_precision: float) -> None:
    """有人工标注时：按类别算 precision / recall，给推荐值。"""
    used = sum(1 for k in labels if k in rows)
    print(f"\n参与计算的标注图：{used} / {len(labels)}")
    if not used:
        print("  一张都对不上：标注里的 image 要用文件名（不带目录）。")
        return

    cats = ["__all__"] + sorted({cat_of.get(t, "general")
                                 for tags in labels.values() for t in tags})
    for cat in cats:
        print(f"\n[{cat}]")
        print(f"{'阈值':>6}{'precision':>11}{'recall':>9}{'F1':>7}{'命中数':>8}")
        best_f1 = (0.0, 0.0)
        best_prec = None
        for thr in thresholds:
            hit = want_hit = 0
            got_all = want_all = 0
            for fname, want_tags in labels.items():
                row = rows.get(fname)
                if row is None:
                    continue
                # "__all__" 是不分类的汇总：不能拿它去比对类别名，
                # 否则每一类都被筛掉，precision 会假成 1.000（命中数却是 0）。
                want = {t for t in want_tags
                        if cat == "__all__" or cat_of.get(t, "general") == cat}
                got = {t for t, v in row.items()
                       if v >= thr and (cat == "__all__"
                                        or cat_of.get(t, "general") == cat)}
                hit += len(got & want)
                want_hit += len(want)
                got_all += len(got)
                want_all += len(want)
            prec = hit / got_all if got_all else 1.0
            rec = hit / want_all if want_all else 1.0
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
            print(f"{thr:>6.2f}{prec:>11.3f}{rec:>9.3f}{f1:>7.3f}{got_all:>8}")
            if f1 > best_f1[1]:
                best_f1 = (thr, f1)
            if best_prec is None and prec >= target_precision:
                best_prec = thr
        if best_f1[1]:
            print(f"  → F1 最优：{best_f1[0]:.2f}（F1={best_f1[1]:.3f}）")
        if best_prec is not None:
            print(f"  → precision ≥ {target_precision:.2f} 的最低阈值：{best_prec:.2f}")
    print("\n注意：precision/recall 是**按 tag 计**，不是按图；"
          "图内 tag 数量多时，precision 会被大量正确但没标注到的 tag 拖低——"
          "标注若只标了「核心 tag」，把 --target-precision 调松一点再看。")


def main() -> int:
    ap = argparse.ArgumentParser(description="tag 阈值标定")
    ap.add_argument("--checkpoint", default="models/wd-eva02")
    ap.add_argument("--images-dir", default=None, dest="images_dir",
                    help="素材目录（不给就只打印默认曲线）")
    ap.add_argument("--labels", default=None, help="标注 jsonl（有它才算 precision/recall）")
    ap.add_argument("--thresholds", type=float, nargs="+",
                    default=[0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60])
    ap.add_argument("--target-precision", type=float, default=0.90,
                    dest="target_precision")
    ap.add_argument("--limit", type=int, default=0, help="最多用多少张（0=全部）")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--precision", default="tf32")
    ap.add_argument("--batch", type=int, default=4)
    args = ap.parse_args()

    if not args.images_dir:
        print("只打印默认阈值曲线（不给 --images-dir 就没法算真实产出）：")
        print("  0.30  0.35  0.40  0.45  0.50  0.55  0.60")
        print("要从数据上定标，请给 --images-dir（有标注再加 --labels）。")
        return 0

    from tagger.cpu_tagger import build_backend, resolve_device

    backend = build_backend("wd-eva02", model_dir=args.checkpoint,
                            device=resolve_device(args.device),
                            precision=args.precision, batch_size=args.batch,
                            verbose=True)
    backend.load()
    files = _images(Path(args.images_dir), args.limit)
    print(f"\n用 {len(files)} 张图取原始分数（阈值在这里不生效）")
    raws = backend.predict_raw([str(p) for p in files])
    rows = {p.name: raw for p, raw in zip(files, raws)}

    _sweep_structure(raws, args.thresholds)

    if args.labels:
        labels = _load_labels(Path(args.labels))
        _sweep_labels(rows, labels, _by_category(backend),
                      args.thresholds, args.target_precision)
    else:
        print("\n没有标注：上面只是结构。要定标必须给 --labels —— "
              "没有对错信号，任何阈值都能自称合理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
