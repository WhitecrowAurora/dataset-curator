"""bf16 / tf32 的掉落验证：换精度之前必须跑这一遍。

为什么必须有这一步
------------------
bench 测出 bf16 在 batch=4 上是 3.75×，但 bf16 的尾数只有 8 位（fp32 是 24 位）。
对分类头来说，影响不是"结果变差"，而是**阈值 0.35 附近的选择变了**：
某个 tag 从 0.349 变成 0.351 就多出这一个 tag，从 0.351 变成 0.349 就少一个。
这类变化在损失函数上几乎为零，在数据集里却是成片的差异，而且事后无法归因。

所以判定标准不是"平均误差小"，而是**每张图的 tag 集合是否一致**：
    IOU = |A ∩ B| / |A ∪ B|（同一张图，fp32 的集合 A 与 bf16 的集合 B）

但只看 IOU 会判错。一张图只有 27 个 tag 时，**一个** 0.3499 的 tag 翻成
0.3501，IOU 立刻掉到 0.93——而这跟"精度掉没掉"没关系：换 batch、换卡、
换驱动同样会翻。所以这里把翻面拆成两类：

    贴阈值（基线概率在 阈值±margin 内） → 边界抖动，任何扰动都有，不算改判
    脱离边界带                        → 真改判，这才是不能上的理由

判定：
    · 全部图 IOU ≥ 0.98                      → 可以无条件上
    · 有图 IOU < 0.98，但脱离边界带的翻面为 0 → 可以用，但要知道 tag 集合会变
    · 脱离边界带的翻面 > 0                    → 别上

用法
----
    # 用真权重（推荐）：本地目录里要有 config.json + model.safetensors
    env\\python\\python.exe devtools\\verify_dtype.py --checkpoint <目录>

    # 快速自检（会下一份 timm 预训练权重，验证方法与流程）
    env\\python\\python.exe devtools\\verify_dtype.py --arch vit_base_patch16_224 \\
        --pretrained vit_base_patch16_224.augreg2_in21k_ft_in1k --images 16

    # 有真实素材时用真实素材，比合成图更能代表线上分布
    env\\python\\python.exe devtools\\verify_dtype.py --checkpoint <目录> --images-dir <素材目录>

⚠️ 用随机权重跑出来的 IOU **没有意义**（概率普遍接近 0，阈值下几乎没有 tag）。
本脚本默认拒绝在随机权重下给结论，只打印"流程已跑通"。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import torch

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


# ------------------------------------------------------------------ 造图


def _synthetic_images(count: int, size: int) -> list:
    """造一批有结构的图（渐变 + 色块 + 噪声）。

    纯噪声会让模型输出接近均匀分布，看不出阈值边界上的摇摆；
    有结构的图更接近真实素材，测出来的 IOU 才有参考价值。
    但有真实素材时优先用 --images-dir。
    """
    import random

    from PIL import Image, ImageDraw

    rng = random.Random(20261102)
    out = []
    for i in range(count):
        im = Image.new("RGB", (size, size), (rng.randrange(60, 200),
                                             rng.randrange(60, 200),
                                             rng.randrange(60, 200)))
        draw = ImageDraw.Draw(im)
        for _ in range(rng.randrange(4, 10)):
            x0, y0 = rng.randrange(size), rng.randrange(size)
            x1 = min(size - 1, x0 + rng.randrange(size // 8, size // 2))
            y1 = min(size - 1, y0 + rng.randrange(size // 8, size // 2))
            draw.ellipse([x0, y0, x1, y1], fill=(rng.randrange(256),
                                                 rng.randrange(256),
                                                 rng.randrange(256)))
        out.append(im)
    return out


def _load_images(images_dir: str | None, count: int, size: int) -> list:
    from PIL import Image

    if not images_dir:
        return _synthetic_images(count, size)

    root = Path(images_dir)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    files = [p for p in sorted(root.rglob("*")) if p.suffix.lower() in exts]
    if not files:
        raise SystemExit(f"{root} 下没有图片")
    out = []
    for p in files[:count]:
        im = Image.open(p).convert("RGB")
        if im.size != (size, size):
            im = im.resize((size, size), Image.BILINEAR)
        out.append(im)
    return out


# ------------------------------------------------------------------ 模型


def _build_model(args, device: str, dtype):
    import timm

    ckpt = args.checkpoint
    if ckpt:
        root = Path(ckpt)
        cfg_path = root / "config.json"
        if not cfg_path.is_file():
            raise SystemExit(f"{root} 里没有 config.json，确认是 WD 那套导出")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        arch = cfg.get("architecture") or args.arch
        classes = int(cfg.get("num_classes") or args.classes)
        model = timm.create_model(arch, pretrained=False, num_classes=classes,
                                  img_size=args.size)
        from safetensors.torch import load_file

        weights = load_file(str(root / "model.safetensors"))
        missing, unexpected = model.load_state_dict(weights, strict=False)
        non_head = [k for k in missing if not k.startswith("head")]
        if non_head or len(unexpected) > 8:
            raise SystemExit(
                f"权重与结构对不上：缺 {len(non_head)} 个非 head 键、"
                f"多 {len(unexpected)} 个键。确认 config.json 与权重同一次导出。")
        data_cfg = timm.data.resolve_data_config({}, model=model)
        transform = timm.data.create_transform(**data_cfg, is_training=False)
        return model.eval().to(device=device, dtype=dtype), transform, False

    # num_classes 与预训练权重一致时，整模型都是训过的，测出来的漂移才有意义；
    # 不一致时 timm 会重置 head，head 随机 → 概率近似常数 → IOU 恒为 1，
    # 那是假象（所以 --pretrained 自检时请把 --classes 设成权重的类数）。
    model = timm.create_model(args.arch, pretrained=bool(args.pretrained),
                              num_classes=args.classes, img_size=args.size)
    data_cfg = timm.data.resolve_data_config({}, model=model)
    transform = timm.data.create_transform(**data_cfg, is_training=False)
    return model.eval().to(device=device, dtype=dtype), transform, not bool(args.pretrained)


# ------------------------------------------------------------------ 主流程



def main() -> int:
    ap = argparse.ArgumentParser(description="fp32 vs bf16 掉落验证")
    ap.add_argument("--checkpoint", default=None, help="WD 权重目录（含 config.json）")
    ap.add_argument("--arch", default="eva02_large_patch14_448")
    ap.add_argument("--pretrained", default="", help="timm 预训练标签（自检用）")
    ap.add_argument("--classes", type=int, default=16473)
    ap.add_argument("--size", type=int, default=448)
    ap.add_argument("--images", type=int, default=16)
    ap.add_argument("--images-dir", default=None, dest="images_dir")
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument("--margin", type=float, default=0.01,
                    help="阈值边界带宽度：基线概率落在 阈值±margin 内的 tag "
                         "本来就不稳定，单独统计（判据见文件头）")
    ap.add_argument("--batch", type=int, default=1)
    args = ap.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("需要 CUDA（CPU 上 bf16 是模拟的，测出来的漂移不代表 GPU 行为）")

    device = "cuda"
    images = _load_images(args.images_dir, args.images, args.size)
    print(f"图片 {len(images)} 张（{args.images_dir or '合成图'}），阈值 {args.threshold}")
    print(f"设备 {torch.cuda.get_device_name(0)}  torch {torch.__version__}")

    results: dict[str, list[torch.Tensor]] = {}
    random_init = False

    for label, dtype, tf32 in (("fp32", torch.float32, False),
                               ("tf32", torch.float32, True),
                               ("bf16", torch.bfloat16, True)):
        torch.backends.cuda.matmul.allow_tf32 = tf32
        torch.backends.cudnn.allow_tf32 = tf32
        try:
            model, transform, is_random = _build_model(args, device, dtype)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {label}: {type(exc).__name__}: {exc}")
            continue
        random_init = random_init or is_random

        probs_all: list[torch.Tensor] = []
        batch: list = []
        with torch.no_grad():
            for im in images:
                batch.append(transform(im))
                if len(batch) < args.batch:
                    continue
                x = torch.stack(batch).to(device=device, dtype=dtype)
                out = torch.sigmoid(model(x)).float().cpu()
                probs_all.extend(list(out))
                batch = []
            if batch:
                x = torch.stack(batch).to(device=device, dtype=dtype)
                out = torch.sigmoid(model(x)).float().cpu()
                probs_all.extend(list(out))

        results[label] = probs_all
        print(f"  {label:<5} 完成 {len(probs_all)} 张")
        del model
        torch.cuda.empty_cache()

    torch.backends.cuda.matmul.allow_tf32 = False

    if "fp32" not in results:
        raise SystemExit("fp32 基线没跑成，无法比较")

    base = results["fp32"]
    print()
    print(f"{'对照':<6}{'平均 IOU':>10}{'最低 IOU':>10}{'IOU<0.98 的图':>14}"
          f"{'翻转 tag':>10}{'其中贴阈值':>12}{'边界带内 IOU':>14}")
    verdicts: list[tuple[str, str]] = []
    for label, probs in results.items():
        if label == "fp32":
            continue
        ious: list[float] = []
        stable: list[float] = []
        flips = near = 0
        for a, b in zip(base, probs):
            at, bt = a.tolist(), b.tolist()
            sa = {i for i, v in enumerate(at) if v >= args.threshold}
            sb = {i for i, v in enumerate(bt) if v >= args.threshold}
            union = sa | sb
            ious.append(len(sa & sb) / len(union) if union else 1.0)
            # 贴阈值的 tag 本来就不稳定：任何数值扰动都会让它翻面，
            # 换 batch、换卡、换驱动同样会翻。所以单独把边界带剔掉再算一次 IOU，
            # 否则"一个 0.3499 的 tag"就能把整张图判成 0.93。
            keep = {i for i, v in enumerate(at)
                    if abs(v - args.threshold) > args.margin}
            sa2, sb2 = sa & keep, sb & keep
            u2 = sa2 | sb2
            stable.append(len(sa2 & sb2) / len(u2) if u2 else 1.0)
            diff = sa ^ sb
            flips += len(diff)
            near += sum(1 for i in diff if abs(at[i] - args.threshold) <= args.margin)
        bad = sum(1 for v in ious if v < 0.98)
        avg = sum(ious) / len(ious)
        hard = flips - near
        print(f"{label:<6}{avg:>10.4f}{min(ious):>10.4f}{bad:>14}"
              f"{flips:>10}{near:>12}{sum(stable) / len(stable):>14.4f}")
        if bad == 0:
            verdicts.append((label, "可以无条件上（所有图 IOU ≥ 0.98）"))
        elif hard == 0:
            verdicts.append((label, f"可以用：{flips} 个翻面全在阈值 ±{args.margin} "
                                    f"内（边界带内 IOU 最低 "
                                    f"{min(stable):.4f}），但会改变 tag 集合"))
        else:
            verdicts.append((label, f"别上：{hard} 个翻面脱离阈值边界带，"
                                    f"那是真改判"))

    print()
    if random_init:
        print("⚠️ 本次用的是**随机权重**，上面的 IOU 只能证明流程跑通，"
              "不能作为上生产的依据。")
        print("   要结论请用真权重：--checkpoint <WD 目录>。")
    else:
        for label, text in verdicts:
            print(f"  {label:<5} {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
