"""F1 回归夹具：APP0/JFXX 内嵌 EXIF 缩略图必须被清除。

背景
----
JPEG 的 APP0 段除了常见的 JFIF 标识，还允许放 **JFXX** 扩展段——它可以
内嵌一张完整的缩略 JPEG，而那张缩略图自己又能带 EXIF。清除实现若"无条件
保留 APP0"，就会把这条元数据链整段搬过去；更糟的是残留复检若复用同一个
解析器，解析器的盲区会同时骗过清除与自检，检查结果看起来完全干净。

所以这个夹具只做一件事：造出这种嵌套，跑一遍清除，然后在**字节层面**
确认标记消失。它不依赖被测算法的任何内部函数。

用法
----
    python devtools/probe_jfxx.py            # 通过退出 0，失败退出 1
    python devtools/probe_jfxx.py -v         # 打印中间细节
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MARKER = "PROBE-JFXX-EXIF-T10"
# 解析器无关的“脏”字节：产物里出现任何一种都说明元数据没清干净。
DIRTY = (b"Exif\x00\x00", b"http://ns.adobe.com/xap", b"Photoshop 3.0", b"JFXX")


def build_sample(dst_dir: Path) -> Path:
    from PIL import Image

    # 1) 内嵌缩略图：8x8 JPEG，带 EXIF(0x010F = 我们的标记)
    thumb = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = MARKER
    Image.new("RGB", (8, 8), (7, 7, 7)).save(thumb, "JPEG", exif=exif)
    thumb_bytes = thumb.getvalue()
    assert MARKER.encode() in thumb_bytes, "夹具自身没把标记写进缩略图"

    # 2) 主图：64x48 JPEG，手工插入 APP0/JFXX(extension code 0x10 + 缩略图)
    main = io.BytesIO()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(main, "JPEG", quality=90)
    raw = main.getvalue()
    payload = b"JFXX\x00\x10" + thumb_bytes
    seg = b"\xff\xe0" + (len(payload) + 2).to_bytes(2, "big") + payload
    src = dst_dir / "f1.jpg"
    src.write_bytes(raw[:2] + seg + raw[2:])
    return src


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    from tagger.metadata_strip import strip_image

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = build_sample(tmp)
        src_bytes = src.read_bytes()
        if MARKER.encode() not in src_bytes:
            print("夹具构造失败：源文件里没有标记")
            return 1

        dst = tmp / "f1_strip.jpg"
        result = strip_image(src, dst)
        out = dst.read_bytes()

        checks: list[tuple[str, bool, str]] = [
            ("产物不再含 EXIF 标记", MARKER.encode() not in out,
             f"标记 {'仍在' if MARKER.encode() in out else '已消失'}"),
            ("stripped_keys 记录了被清段", bool(result.stripped_keys),
             f"stripped_keys={result.stripped_keys}"),
        ]
        for dirty in DIRTY:
            checks.append((f"产物不含 {dirty!r}", dirty not in out,
                           "干净" if dirty not in out else "残留"))

        # 像素必须没被动过：JFXX 只是附加段，丢掉它不该影响主图
        try:
            from PIL import Image
            a = Image.open(src).convert("RGB")
            b = Image.open(dst).convert("RGB")
            same = a.size == b.size and a.tobytes() == b.tobytes()
        except Exception as exc:  # noqa: BLE001
            same = False
            if verbose:
                print(f"  像素比对失败: {type(exc).__name__}: {exc}")
        checks.append(("主图像素未变", same, "一致" if same else "不一致"))

    failed = [c for c in checks if not c[1]]
    if verbose:
        for name, ok, detail in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}  —— {detail}")
    if failed:
        print(f"[probe_jfxx] 失败 {len(failed)} 项：{[c[0] for c in failed]}")
        return 1
    print(f"[probe_jfxx] 通过：{len(checks)} 项（JFXX 内嵌 EXIF 已被清除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
