"""训练输入图的元数据清除：容器级重写，像素一个比特都不动。

## 为什么是"重写容器"而不是"解码再保存"

重编码（decode → save）会同时做三件不受欢迎的事：JPEG 再压一道（伪影、
体积漂移）、PNG 换掉压缩参数（体积漂移）、以及把解码器对源文件的色彩/位深
解释固化成新文件。而这里要达成的只有一件事——把容器里的元数据段拿掉。
所以按段/块解析容器，丢掉元数据段，其余字节（量化表、霍夫曼表、熵编码数据
流、IDAT 压缩流）原样搬运：解码出来的像素与源**逐像素相同**，这也是
`strip_image()` 能断言"最大像素差 = 0"的原因。

要清掉的东西是两类，都带隐私与来源痕迹：

- JPEG 的 APP1（EXIF：GPS、设备与相机序列号；XMP：编辑历史、生成参数）、
  APP2（ICC 色彩配置、MPF 多图索引）、APP13（Photoshop 的 IPTC）、COM。
- PNG 的 tEXt / zTXt / iTXt（ComfyUI 把完整生成参数塞在这里）、eXIf、tIME，
  以及其他一切非白名单块（C2PA 的 caBX、各类私有块）。

保留 APP0（仅限 JFIF 载荷）与 APP14（Adobe 色彩变换标记）是有意的：它们不是
个人信息，删掉会让解码器解错颜色或丢掉像素纵横比，属于"清了元数据但弄坏了图"。

APP0 只在载荷以 `JFIF\x00` 开头时保留。同一个段号还承载 **JFXX** 扩展载荷，
而 JFXX 可以内嵌一张完整的缩略 JPEG——那张缩略图自己又能带 EXIF，于是"保留
APP0"等于把一条元数据链整段搬运过去。所以 JFXX 整段丢弃，其他不认识的 APP0
载荷也一律丢弃：这里宁可少保留一个提示段，也不给元数据留后门。
PNG 同理保留 IHDR/PLTE/IDAT/IEND/tRNS 与色彩管理块（gAMA/sRGB/cHRM/iCCP）。

## 两道独立的残留检查

1. `residual_metadata()`：复用清除函数自己的解析器——它能证明"清除逻辑认为
   干净"，但解析器的盲区会同时骗过清除与自检（JFXX 那次就是这样漏的）。
2. `find_dirty_markers()`：**不碰解析器**，直接在产物字节里搜 EXIF / XMP /
   IPTC / JFXX 的特征串，命中即 raise。这是 fail-closed 的兜底：宁可让一张
   极端文件（例如压缩数据里恰好出现同名字节串）报错待人看，也不要悄悄放过去。

删 EXIF 会连 Orientation 一起删，这是有意的：训练用的是原始栅格，不是某个
查看器按 EXIF 转正之后的显示结果。`strip_image()` 会把这个值读出来放在
`StripResult.exif_orientation` 里，批处理遇到非 1 的图会提示一句——要不要先
转正属于数据侧决定，工具不偷偷旋转像素（一转就不是"像素不变"了）。

## 原始文件不可逆删除的防线

唯一的一份原始数据集不能被这个工具改写或覆盖，因此：

1. 只读 `--src`，任何产出都写 `--out` / `--originals`；
2. `--out` 与 `--originals` 都被拒绝落在 `--src` 里面（配置错了直接报错退出，
   而不是"跑完才发现原始目录被覆盖"）；
3. 每张图在 `--originals` 留一份副本（去掉写权限位，作为只读区），落盘后
   重新算 sha256 与源文件比对，不一致即报错；
4. manifest 同时记录源与产物的 sha256，事后可以逐条复核"这份原始图确实还是
   原来那一份"。

## CLI

    python -m tagger.metadata_strip --src DIR --out DIR --originals DIR

逐文件输出到 `<out>/manifest.jsonl`，一行一条 JSON，字段固定为：

    {"src": ..., "dst": ..., "src_sha256": ..., "dst_sha256": ..., "stripped_keys": [...]}

`src` 是相对 `--src` 根的 POSIX 路径，`dst` 是相对 `--out` 根的 POSIX 路径
（两者文件名与目录结构一致，`--originals` 按同一相对路径镜像）。用相对路径
而不是绝对路径，是为了 manifest 能连同产物目录一起搬走、跨机器比对。
`stripped_keys` 列出实际删掉的段/块（如 `jpeg:APP1:Exif`、`png:tEXt:Comment`），
空列表表示这张图本来就是干净的；同一类段被删多次只记一次（多条 COM 就是一条
`jpeg:COM`），否则 manifest 里数不出"清掉了几类东西"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

JPEG_MAGIC = b"\xff\xd8"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 认得出容器、但本工具不处理（或不做容器级重写）的格式。列在这里是为了
# 报出具体格式名，而不是笼统地说"不支持"——不同的格式有不同的补法。
_UNSUPPORTED_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"RIFF", "WebP/AVI（RIFF 容器）"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"\x00\x00\x00\x0cJXL \r\n\x87\n", "JPEG XL"),
    (b"\x00\x00\x00\x1cftypavif", "AVIF"),
    (b"\x00\x00\x00\x18ftypheic", "HEIC"),
)

# 与解析器无关的 fail-closed 兜底：产物字节里出现这些标记就说明清除漏了。
# 纯 bytes 搜索，刻意不调用段解析——残留复检若复用清除函数自己的解析器，
# 解析器的盲区会同时骗过清除与自检（APP0/JFXX 内嵌 EXIF 缩略图正是这样漏的）。
_DIRTY_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"Exif\x00\x00", "EXIF"),
    (b"http://ns.adobe.com/xap", "XMP"),
    (b"Photoshop 3.0", "IPTC"),
    (b"JFXX", "JFXX"),
)


def find_dirty_markers(data: bytes) -> list[str]:
    """纯字节扫描产物：返回仍然出现的元数据标记名（空 = 干净）。"""
    return [name for marker, name in _DIRTY_MARKERS if marker in data]


# PNG 白名单：解码必需 + 色彩管理必需。白名单而不是黑名单，因为 PNG 允许
# 注册与私有块，黑名单永远漏一个。
_PNG_KEEP = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS",
    b"gAMA", b"sRGB", b"cHRM", b"iCCP", b"pHYs", b"bKGD", b"sBIT", b"hIST",
})


class MetadataStripError(Exception):
    """容器结构不允许安全清除，或清除后的自检没通过。"""


class UnsupportedImageFormat(MetadataStripError):
    """不是本工具能处理的容器格式。"""


@dataclass(frozen=True, slots=True)
class StripResult:
    """一张图的清除结果，字段与 manifest 一一对应（另带体积便于统计）。"""

    src: Path
    dst: Path
    src_sha256: str
    dst_sha256: str
    stripped_keys: tuple[str, ...]
    src_bytes: int
    dst_bytes: int
    max_pixel_diff: int
    exif_orientation: int | None = None


# ---------------------------------------------------------------- 哈希与文件


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    """先写同目录临时文件再 replace：中断留下的半截文件不会冒充成品。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- JPEG 段重写


def _jpeg_segment_decision(marker: int, payload: bytes) -> tuple[bool, str]:
    """(保留?, 删除时记进 manifest 的键)。

    APP0 只保留 JFIF 载荷（像素密度/色彩空间提示）；JFXX 与其他载荷丢掉——
    JFXX 内嵌的缩略 JPEG 能带 EXIF，保留它等于放一条元数据绕过路径。
    APP14（Adobe）保留：它是 CMYK/YCCK 的变换标记，删掉只是换来"看起来更
    干净"但解错的颜色。
    """
    if marker == 0xE0:
        if payload.startswith(b"JFIF\x00"):
            return True, ""
        if payload.startswith(b"JFXX\x00"):
            # JFXX 可以内嵌整张缩略 JPEG，缩略图自己还能带 EXIF：整段丢掉。
            return False, "jpeg:APP0:JFXX"
        return False, "jpeg:APP0"
    if marker == 0xEE:
        return True, ""
    if marker == 0xE1:
        if payload.startswith(b"Exif\x00\x00"):
            return False, "jpeg:APP1:Exif"
        if payload.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
            return False, "jpeg:APP1:XMP"
        if payload.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
            return False, "jpeg:APP1:XMP_EXT"
        return False, "jpeg:APP1"
    if marker == 0xE2:
        if payload.startswith(b"ICC_PROFILE\x00"):
            return False, "jpeg:APP2:ICC_PROFILE"
        if payload.startswith(b"MPF\x00"):
            return False, "jpeg:APP2:MPF"
        return False, "jpeg:APP2"
    if marker == 0xED:
        if payload.startswith(b"Photoshop 3.0\x00"):
            return False, "jpeg:APP13:Photoshop"
        return False, "jpeg:APP13"
    if 0xE3 <= marker <= 0xEF and marker not in (0xED, 0xEE):
        return False, f"jpeg:APP{marker - 0xE0}"
    if marker == 0xFE:
        return False, "jpeg:COM"
    return True, ""


def _skip_entropy_data(data: bytes, i: int) -> int:
    """跳过 SOS 之后的熵编码数据，返回下一个真正的标记位置。

    扫描段里的 0xFF 只有两种合法出现：填充（FF 00）与重启标记（FF D0-D7），
    其余 0xFF xx 都是下一个标记段。progressive JPEG 会有多个 SOS，所以不能
    像"遇到第一个 SOS 就把尾巴整体带走"那样提前收工——后续 SOS 之前仍可能
    挂着 DHT/DNL 之外的段。
    """
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1
            continue
        j = i + 1
        while j < n and data[j] == 0xFF:
            j += 1
        if j >= n:
            return n
        marker = data[j]
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            i = j + 1
            continue
        return i
    return n


def _exif_orientation(payload: bytes) -> int | None:
    """从 APP1 的 EXIF 里取 Orientation（0x0112），取不到返回 None。

    删 EXIF 会连 Orientation 一起删：像素是原始栅格，而按 EXIF 旋转显示的
    查看器会看到"方向没了"。这里读出来只是为了让批处理提示一句（由调用方
    决定要不要先做方向校正），本工具不改像素。
    """
    if not payload.startswith(b"Exif\x00\x00"):
        return None
    tiff = payload[6:]
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "little"
    elif tiff[:2] == b"MM":
        endian = "big"
    else:
        return None
    if int.from_bytes(tiff[2:4], endian) != 42:
        return None
    ifd = int.from_bytes(tiff[4:8], endian)
    if ifd + 2 > len(tiff):
        return None
    count = int.from_bytes(tiff[ifd:ifd + 2], endian)
    pos = ifd + 2
    for _ in range(count):
        entry = tiff[pos:pos + 12]
        if len(entry) < 12:
            return None
        if int.from_bytes(entry[0:2], endian) == 0x0112:
            etype = int.from_bytes(entry[2:4], endian)
            ecount = int.from_bytes(entry[4:8], endian)
            if etype == 3 and ecount >= 1:      # SHORT
                return int.from_bytes(entry[8:10], endian)
            return None
        pos += 12
    return None


def _strip_jpeg(data: bytes) -> tuple[bytes, list[str], int | None]:
    """按段重写 JPEG：丢掉 APPn（白名单外）与 COM，其余字节原样搬运。

    额外返回源 EXIF 里的 Orientation（没写就是 None），供调用方提示方向信息
    已随 EXIF 一起被清掉。
    """
    if not data.startswith(JPEG_MAGIC):
        raise MetadataStripError("不是 JPEG（缺少 SOI 标记）")
    out = bytearray(data[:2])
    keys: list[str] = []
    orientation: int | None = None
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            # 段结构与预期不符（坏文件/非标准封装）：剩余字节原样带走。
            # 宁可不猜着重同步——猜错会吐出比原文件更坏的字节。
            out += data[i:]
            break
        start = i
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            out += data[start:]
            break
        marker = data[i]
        i += 1
        if marker == 0x00:
            # 段边界位置出现 FF 00：结构已错位，停止解析并保留尾巴。
            out += data[start:]
            break
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += data[start:i]          # 无长度字段
            continue
        if marker == 0xD9:                # EOI
            out += data[start:i]
            break
        if i + 2 > n:
            out += data[start:]
            break
        seg_len = int.from_bytes(data[i:i + 2], "big")
        if seg_len < 2:
            out += data[start:]
            break
        end = min(n, i + seg_len)
        payload = data[i + 2:end]
        keep, key = _jpeg_segment_decision(marker, payload)
        if keep:
            out += data[start:end]
        elif key:
            # 同一类段只记一次：三条 COM 记成一条，manifest 才数得清"清了几类"。
            if key not in keys:
                keys.append(key)
            if key == "jpeg:APP1:Exif" and orientation is None:
                orientation = _exif_orientation(payload)
        i = end
        if marker == 0xDA:                # SOS
            scan_end = _skip_entropy_data(data, i)
            out += data[i:scan_end]
            i = scan_end
    return bytes(out), keys, orientation


# ---------------------------------------------------------------- PNG 块重写


def _chunk_key(ctype: bytes, payload: bytes) -> str:
    name = ctype.decode("ascii", "replace")
    if ctype in (b"tEXt", b"zTXt", b"iTXt"):
        keyword, _, _rest = payload.partition(b"\x00")
        if keyword:
            return f"png:{name}:{keyword.decode('latin-1')}"
    return f"png:{name}"


def _strip_png(data: bytes) -> tuple[bytes, list[str]]:
    """按块重写 PNG：只留白名单块，其余整块丢弃。"""
    if not data.startswith(PNG_MAGIC):
        raise MetadataStripError("不是 PNG（缺少签名）")
    out = bytearray(PNG_MAGIC)
    keys: list[str] = []
    i, n = 8, len(data)
    seen_iend = False
    seen_ihdr = False
    while i + 8 <= n:
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            raise MetadataStripError(f"PNG 块 {ctype!r} 长度越界，文件已损坏")
        if not seen_ihdr:
            if ctype != b"IHDR":
                raise MetadataStripError("PNG 首个块不是 IHDR，无法安全重写")
            seen_ihdr = True
        if ctype in _PNG_KEEP:
            out += data[i:end]
        elif ctype[:1].isupper():
            # 关键块（首字母大写）不认识就不能丢：丢掉等于弄坏图，报错交给人看。
            raise MetadataStripError(
                f"PNG 含未知关键块 {ctype.decode('ascii', 'replace')}，拒绝猜测性清除")
        else:
            chunk_key = _chunk_key(ctype, data[i + 8:i + 8 + length])
            if chunk_key not in keys:
                keys.append(chunk_key)
        i = end
        if ctype == b"IEND":
            seen_iend = True
            break
    if not seen_iend:
        raise MetadataStripError("PNG 缺少 IEND，文件已截断")
    if i < n:
        keys.append("png:trailing-bytes")
    return bytes(out), keys


# ---------------------------------------------------------------- 像素与残留


def _decode(data: bytes):
    from PIL import Image

    return Image.open(BytesIO(data))


def _normalized(image):
    """把两张图拉到同一个可比较的色彩空间，避免模式差异冒充像素差异。"""
    mode = image.mode
    info_alpha = "transparency" in getattr(image, "info", {})
    if mode in ("I", "I;16", "I;16B", "I;16L", "F"):
        return image.convert("I")
    if mode in ("RGBA", "LA", "PA", "RGBa", "La") or info_alpha:
        return image.convert("RGBA")
    return image.convert("RGB")


def _max_channel_diff(left, right) -> int:
    """最大逐通道差。

    ImageChops 只认 8 位模式，16 位/浮点 PNG 会抛 "image has wrong mode"，
    所以高位深单独走一次逐像素比较。正常路径（容器级重写、IDAT 原样搬运）
    两边字节完全相同，逐像素那一步退化成一次 bytes 比较，不会真的慢。
    """
    from PIL import ImageChops

    try:
        extrema = ImageChops.difference(left, right).getextrema()
    except ValueError:
        return _max_diff_wide(left, right)
    if extrema and isinstance(extrema[0], tuple):
        return max(pair[1] for pair in extrema)
    return int(extrema[1])


def _max_diff_wide(left, right) -> int:
    import array
    import sys

    a, b = left.tobytes(), right.tobytes()
    if a == b:
        return 0
    typecode = "f" if left.mode == "F" else "i"
    arr_a, arr_b = array.array(typecode), array.array(typecode)
    arr_a.frombytes(a)
    arr_b.frombytes(b)
    if sys.byteorder == "big":
        arr_a.byteswap()
        arr_b.byteswap()
    worst = 0
    for x, y in zip(arr_a, arr_b):
        d = abs(x - y)
        if d > worst:
            worst = d
    return int(worst)


def pixel_diff(src_data: bytes, dst_data: bytes) -> tuple[tuple[int, int], int]:
    """返回 ((宽, 高), 最大逐通道像素差)。

    尺寸不同直接抛错（那不是"差一点"，是换了一张图）。差异本身不在这一层
    抛错，阈值由调用方决定：容器级重写的调用方要的是 0，将来如果有人拿它
    校验别的路径，也能自己定容忍度。
    """
    with _decode(src_data) as a, _decode(dst_data) as b:
        if a.size != b.size:
            raise MetadataStripError(f"尺寸变了：{a.size} → {b.size}")
        size = a.size
        left, right = _normalized(a), _normalized(b)
    return size, _max_channel_diff(left, right)


def residual_metadata(data: bytes) -> list[str]:
    """重新扫一遍产物：返回还残留的元数据键。空列表 = 干净。

    直接复用清除函数本身（它返回"如果现在清会删掉什么"），避免第二套解析器
    与清除逻辑漂移。但要清楚它只是第二层：解析器视角的"干净"不等于字节干净，
    字节层面由 `find_dirty_markers()` 独立兜底。
    """
    if data.startswith(JPEG_MAGIC):
        return _strip_jpeg(data)[1]
    if data.startswith(PNG_MAGIC):
        return _strip_png(data)[1]
    return ["unsupported"]


def detect_format(data: bytes) -> str:
    if data.startswith(JPEG_MAGIC):
        return "jpeg"
    if data.startswith(PNG_MAGIC):
        return "png"
    for magic, name in _UNSUPPORTED_MAGIC:
        if data.startswith(magic):
            raise UnsupportedImageFormat(f"不支持的图片格式：{name}")
    raise UnsupportedImageFormat("不支持的图片格式：无法识别的容器（只支持 JPEG / PNG）")


# 魔数认不出的图片后缀：批处理里要报成"不支持"，而不是当成说明文件跳过。
_UNSUPPORTED_SUFFIXES = frozenset({
    ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif",
    ".jxl", ".jp2", ".ico", ".ppm", ".pgm", ".pnm", ".tga", ".dds", ".exr",
})


def unsupported_format(head: bytes, suffix: str) -> str:
    """"是图片但清不了"就返回格式名；与图片无关的文件（说明、标注表）返回空串。

    两路判断缺一不可：魔数认得出容器，就报具体格式；认不出但后缀是图片格式，
    仍然要报——否则一张 .webp 会被静默跳过，看起来像"处理完了"。
    """
    for magic, name in _UNSUPPORTED_MAGIC:
        if head.startswith(magic):
            return name
    lower = suffix.lower()
    if lower in _UNSUPPORTED_SUFFIXES:
        return f"后缀 {lower} 不在 JPEG / PNG 之内"
    return ""


# ---------------------------------------------------------------- 主入口


def strip_image(src: str | Path, dst: str | Path, *,
                max_pixel_diff: int = 0) -> StripResult:
    """把 `src` 的元数据清掉写到 `dst`；源文件只读不写。

    `max_pixel_diff` 默认 0：容器级重写不会碰像素，任何差异都说明解析错了，
    应该报错而不是放过。

    写盘前过三道关：像素差 → 字节级标记扫描（与解析器无关）→ 解析器复检。
    任一不过都 raise，不产出文件——宁可让这一张停下来待人看，也不写出一份
    "看起来清干净了"的产物。
    """
    src_path, dst_path = Path(src), Path(dst)
    src_data = src_path.read_bytes()
    fmt = detect_format(src_data)     # 不支持的格式在这里就给出明确错误

    dst_suffix = dst_path.suffix.lower()
    if fmt == "jpeg" and dst_suffix not in (".jpg", ".jpeg", ".jpe", ".jfif"):
        raise MetadataStripError(
            f"产物后缀与源格式不符：源是 JPEG，dst 是 {dst_path.name}")
    if fmt == "png" and dst_suffix != ".png":
        raise MetadataStripError(
            f"产物后缀与源格式不符：源是 PNG，dst 是 {dst_path.name}")

    orientation: int | None = None
    if fmt == "jpeg":
        stripped, keys, orientation = _strip_jpeg(src_data)
    else:
        stripped, keys = _strip_png(src_data)

    size, diff = pixel_diff(src_data, stripped)
    if diff > max_pixel_diff:
        raise MetadataStripError(
            f"像素差 {diff} 超过允许的 {max_pixel_diff}：{src_path}")
    # 先做与解析器无关的字节级兜底：解析器认为干净而字节里还有标记，说明
    # 清除路径有盲区，这时必须报错而不是相信自检。
    dirty = find_dirty_markers(stripped)
    if dirty:
        raise MetadataStripError(
            f"字节级兜底检查失败（清除有盲区，产物仍含 {'/'.join(dirty)}）：{src_path}")
    leftover = residual_metadata(stripped)
    if leftover:
        raise MetadataStripError(
            f"清除后仍残留元数据 {leftover}：{src_path}")
    if size[0] <= 0 or size[1] <= 0:
        raise MetadataStripError(f"解码得到空图像：{src_path}")

    _write_atomic(dst_path, stripped)
    written = dst_path.read_bytes()
    if written != stripped:
        raise MetadataStripError(f"写入后内容不一致：{dst_path}")

    return StripResult(
        src=src_path, dst=dst_path,
        src_sha256=sha256_bytes(src_data),
        dst_sha256=sha256_bytes(written),
        stripped_keys=tuple(keys),
        src_bytes=len(src_data), dst_bytes=len(written),
        max_pixel_diff=diff,
        exif_orientation=orientation,
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _iter_candidates(root: Path) -> list[Path]:
    return sorted((p for p in root.rglob("*") if p.is_file()),
                  key=lambda p: p.relative_to(root).as_posix())


def run(src_dir: Path, out_dir: Path, originals_dir: Path, *,
        manifest_path: Path | None = None, quiet: bool = False) -> int:
    """遍历 `src_dir` 清一遍元数据。返回进程退出码（0 = 全部成功）。

    逐文件容错：一张图坏了不该让整批停下，但坏的那些要进 stderr 并且最终
    退出码非 0——否则批量脚本会把"一半没处理"当成成功。
    """
    src_dir = src_dir.resolve()
    out_dir = out_dir.resolve()
    originals_dir = originals_dir.resolve()
    if not src_dir.is_dir():
        raise MetadataStripError(f"--src 不是目录：{src_dir}")
    # 防线：产出目录绝不能与原始目录重叠，否则"清一遍"就等于对唯一的原始
    # 数据集做不可逆改写。
    if _is_within(out_dir, src_dir):
        raise MetadataStripError(f"--out 不能位于 --src 之内：{out_dir}")
    if _is_within(originals_dir, src_dir):
        raise MetadataStripError(f"--originals 不能位于 --src 之内：{originals_dir}")
    if out_dir == originals_dir:
        raise MetadataStripError("--out 与 --originals 不能是同一个目录")
    if _is_within(out_dir, originals_dir) or _is_within(originals_dir, out_dir):
        raise MetadataStripError("--out 与 --originals 不能互相嵌套")

    manifest_path = manifest_path or (out_dir / "manifest.jsonl")
    out_dir.mkdir(parents=True, exist_ok=True)
    originals_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    errors: list[tuple[str, str]] = []
    done = skipped = 0
    src_bytes = dst_bytes = 0
    for path in _iter_candidates(src_dir):
        rel = path.relative_to(src_dir)
        with open(path, "rb") as handle:
            head = handle.read(16)
        if not (head.startswith(JPEG_MAGIC) or head.startswith(PNG_MAGIC)):
            # 与图片无关的文件（说明文件、标注表）跳过；是图片但格式不支持的
            # 记一条错误，让退出码反映"有一批没处理"。
            name = unsupported_format(head, path.suffix)
            if not name:
                skipped += 1
                continue
            message = f"不支持的图片格式：{name}"
            errors.append((rel.as_posix(), message))
            print(f"[strip:error] {rel.as_posix()}: {message}",
                  file=sys.stderr, flush=True)
            continue

        dst_path = out_dir / rel
        original_path = originals_dir / rel
        try:
            src_sha = sha256_file(path)
            if original_path.exists():
                # 副本已存在：内容不同就意味着两条不同的源文件撞到同一个
                # 相对路径，宁可报错也不覆盖——那是唯一原始数据的备份。
                if original_path.is_file() and sha256_file(original_path) == src_sha:
                    os.chmod(original_path, 0o444)
                else:
                    raise MetadataStripError(
                        f"原始副本已存在且内容不同，拒绝覆盖：{original_path}")
            else:
                original_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, original_path)
                os.chmod(original_path, 0o444)
                if sha256_file(original_path) != src_sha:
                    raise MetadataStripError(
                        f"原始副本哈希与源不一致：{original_path}")

            result = strip_image(path, dst_path)
            if result.src_sha256 != src_sha:
                raise MetadataStripError(f"处理期间源文件被改动：{path}")
            if sha256_file(original_path) != src_sha:
                raise MetadataStripError(f"原始副本被改动：{original_path}")
            lines.append(json.dumps({
                "src": rel.as_posix(),
                "dst": dst_path.relative_to(out_dir).as_posix(),
                "src_sha256": result.src_sha256,
                "dst_sha256": result.dst_sha256,
                "stripped_keys": list(result.stripped_keys),
            }, ensure_ascii=False, sort_keys=False))
            if result.exif_orientation not in (None, 1):
                print(f"[strip:warn] {rel.as_posix()}: 源 EXIF 方向标记 "
                      f"Orientation={result.exif_orientation}，已随 EXIF 清掉；"
                      f"像素是原始栅格，需要按方向校正请先转正再入训",
                      file=sys.stderr, flush=True)
            done += 1
            src_bytes += result.src_bytes
            dst_bytes += result.dst_bytes
            if not quiet:
                keys = ",".join(result.stripped_keys) or "-"
                print(f"[strip] {rel.as_posix()}  {keys}", flush=True)
        except (MetadataStripError, OSError) as exc:
            errors.append((rel.as_posix(), str(exc)))
            print(f"[strip:error] {rel.as_posix()}: {exc}", file=sys.stderr, flush=True)

    _write_atomic(manifest_path, ("\n".join(lines) + "\n").encode() if lines else b"")
    print(f"[strip] 完成 {done} 张，跳过 {skipped} 个非图片，失败 {len(errors)} 个；"
          f"manifest={manifest_path}", flush=True)
    if done:
        print(f"[strip] 体积 {src_bytes} → {dst_bytes} 字节", flush=True)
    return 0 if not errors else 1


# ---------------------------------------------------------------- selftest


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return (len(payload).to_bytes(4, "big") + ctype + payload
            + zlib.crc32(ctype + payload).to_bytes(4, "big"))


def _pattern_image(width: int = 48, height: int = 32):
    from PIL import Image

    image = Image.new("RGB", (width, height))
    image.putdata([((x * 5) % 256, (y * 7) % 256, (x * 3 + y * 11) % 256)
                   for y in range(height) for x in range(width)])
    return image


def _make_exif_jpeg() -> bytes:
    from PIL import Image

    exif = Image.Exif()
    exif[0x010F] = "StripTestCam"          # Make
    exif[0x0110] = "StripTestBody"         # Model
    exif[0x0132] = "2026:01:01 00:00:00"   # DateTime
    exif[0x010E] = "prompt: a cat"         # ImageDescription
    exif[0x0112] = 6                       # Orientation
    buf = BytesIO()
    _pattern_image().save(buf, "JPEG", quality=95, subsampling=0, exif=exif)
    data = buf.getvalue()
    xmp = (b'http://ns.adobe.com/xap/1.0/\x00'
           b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
           b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
           b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           b'<rdf:Description xmp:CreatorTool="ComfyUI"/></rdf:RDF></x:xmpmeta>'
           b'<?xpacket end="w"?>')
    com = b"generator=comfyui; model=test; prompt=masterpiece"
    # 插在 SOI 之后：APPn/COM 属于段头区，放在这里合法且不会被当成扫描数据。
    return (data[:2] + _jpeg_segment(0xE1, xmp) + _jpeg_segment(0xFE, com)
            + data[2:])


def _make_text_png() -> bytes:
    buf = BytesIO()
    _pattern_image().save(buf, "PNG")
    data = buf.getvalue()
    chunks = [
        _png_chunk(b"tEXt", b"Comment\x00generated by comfyui"),
        _png_chunk(b"tEXt", b"parameters\x00masterpiece, 1girl, steps: 30"),
        _png_chunk(b"zTXt", b"Software\x00\x00" + zlib.compress(b"comfyui")),
        _png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00"
                            b"<x:xmpmeta><rdf:RDF/></x:xmpmeta>"),
        _png_chunk(b"tIME", bytes([0x07, 0xEA, 1, 1, 0, 0, 0])),
        _png_chunk(b"eXIf", b"II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00"),
    ]
    # IHDR 是紧跟在 8 字节签名后的第一块，总长 8 + (12 + 13) = 33。
    return data[:33] + b"".join(chunks) + data[33:]


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def _selftest() -> int:
    import tempfile

    from PIL import Image

    from . import tmpdir

    with tempfile.TemporaryDirectory(prefix="metadata-strip-",
                                     dir=tmpdir.ensure()) as tmp:
        root = Path(tmp)

        # ---- 1. JPEG：EXIF + XMP + COM 全部清掉，像素不变
        jpeg_src = _make_exif_jpeg()
        jpeg_in = root / "case" / "shot.jpg"
        jpeg_in.parent.mkdir(parents=True, exist_ok=True)
        jpeg_in.write_bytes(jpeg_src)
        _check(b"Exif\x00\x00" in jpeg_src, "构造的 JPEG 没有 EXIF，用例无效")
        _check(b"http://ns.adobe.com/xap/1.0/" in jpeg_src, "构造的 JPEG 没有 XMP")

        jpeg_out = root / "case-out" / "shot.jpg"
        res = strip_image(jpeg_in, jpeg_out)
        stripped = jpeg_out.read_bytes()
        _check(b"Exif\x00\x00" not in stripped, "JPEG 仍残留 EXIF")
        _check(b"http://ns.adobe.com/xap" not in stripped, "JPEG 仍残留 XMP")
        _check(b"generator=comfyui" not in stripped, "JPEG 仍残留 COM")
        _check("jpeg:APP1:Exif" in res.stripped_keys, "manifest 未记录 EXIF 删除")
        _check("jpeg:APP1:XMP" in res.stripped_keys, "manifest 未记录 XMP 删除")
        _check("jpeg:COM" in res.stripped_keys, "manifest 未记录 COM 删除")
        _check(residual_metadata(stripped) == [], "JPEG 残留检测不干净")
        _check(res.src_sha256 == sha256_bytes(jpeg_src), "JPEG 源哈希记录不正确")
        _check(res.dst_sha256 == sha256_bytes(stripped), "JPEG 产物哈希记录不正确")
        _check(res.max_pixel_diff == 0, "JPEG 容器重写却改了像素")
        _check(res.exif_orientation == 6,
               f"未读出源 EXIF 的 Orientation：{res.exif_orientation}")
        with Image.open(jpeg_in) as a, Image.open(jpeg_out) as b:
            _check(a.size == b.size, "JPEG 尺寸变了")
            _check(a.size == _pattern_image().size, "JPEG 尺寸与源图不符")
        _check("StripTestCam" not in stripped.decode("latin-1"), "JPEG 仍残留相机型号")
        # 正常 JFIF 的 APP0 必须留着：它是像素密度提示，删了图没坏但信息丢了。
        _check(b"JFIF\x00" in stripped, "正常 JFIF APP0 被误删")
        _check(not any(k.startswith("jpeg:APP0") for k in res.stripped_keys),
               f"正常 JFIF APP0 被记成删除：{res.stripped_keys}")

        # ---- 1b. APP0 的 JFXX 载荷内嵌带 EXIF 的缩略 JPEG：整段丢弃
        exif_thumb = Image.Exif()
        exif_thumb[0x010F] = "PROBE-JFXX-EXIF-T10"
        buf_thumb = BytesIO()
        _pattern_image(8, 8).save(buf_thumb, "JPEG", exif=exif_thumb)
        thumb_bytes = buf_thumb.getvalue()
        _check(b"PROBE-JFXX-EXIF-T10" in thumb_bytes, "缩略图夹具没把标记写进去")
        buf_main = BytesIO()
        _pattern_image().save(buf_main, "JPEG", quality=90)
        main_bytes = buf_main.getvalue()
        jfxx_src = (main_bytes[:2]
                    + _jpeg_segment(0xE0, b"JFXX\x00\x10" + thumb_bytes)
                    + main_bytes[2:])
        _check(b"JFXX" in jfxx_src, "夹具里没有 JFXX 段")
        _check(b"Exif\x00\x00" in jfxx_src, "夹具的缩略图里没有 EXIF")
        jfxx_in = root / "case" / "jfxx.jpg"
        jfxx_in.write_bytes(jfxx_src)
        jfxx_out = root / "case-out" / "jfxx.jpg"
        res_jfxx = strip_image(jfxx_in, jfxx_out)
        jfxx_stripped = jfxx_out.read_bytes()
        _check(b"PROBE-JFXX-EXIF-T10" not in jfxx_stripped, "JFXX 内嵌的 EXIF 标记仍在")
        _check(b"JFXX" not in jfxx_stripped, "JFXX 段仍在")
        _check(b"Exif\x00\x00" not in jfxx_stripped, "产物仍含 EXIF 标记")
        _check("jpeg:APP0:JFXX" in res_jfxx.stripped_keys,
               f"未记录 JFXX 删除：{res_jfxx.stripped_keys}")
        _check(res_jfxx.stripped_keys, "JFXX 用例的 stripped_keys 为空")
        _check(res_jfxx.max_pixel_diff == 0, "丢 JFXX 却改了像素")
        _check(b"JFIF\x00" in jfxx_stripped, "JFIF 段被 JFXX 连带删掉")

        # 非 JFIF、非 JFXX 的 APP0 载荷也不留：不认识的段没有保留价值。
        unknown_src = (main_bytes[:2]
                       + _jpeg_segment(0xE0, b"UNKNOWNAPP0\x00payload")
                       + main_bytes[2:])
        unknown_in = root / "case" / "unknown_app0.jpg"
        unknown_in.write_bytes(unknown_src)
        unknown_out_path = root / "case-out" / "unknown_app0.jpg"
        res_unknown = strip_image(unknown_in, unknown_out_path)
        unknown_out = unknown_out_path.read_bytes()
        _check(b"UNKNOWNAPP0" not in unknown_out, "未知 APP0 载荷仍在")
        _check("jpeg:APP0" in res_unknown.stripped_keys,
               f"未记录未知 APP0 删除：{res_unknown.stripped_keys}")
        _check(b"JFIF\x00" in unknown_out, "删未知 APP0 时连带删掉了 JFIF")
        _check(res_unknown.max_pixel_diff == 0, "删未知 APP0 改了像素")

        # ---- 1c. 多条 COM：stripped_keys 不得出现重复项
        multi_com = (jpeg_src[:2] + _jpeg_segment(0xFE, b"comment one")
                     + _jpeg_segment(0xFE, b"comment two") + jpeg_src[2:])
        _check(multi_com.count(b"\xff\xfe") >= 3, "多条 COM 夹具构造失败")
        multi_in = root / "case" / "multi_com.jpg"
        multi_in.write_bytes(multi_com)
        res_multi = strip_image(multi_in, root / "case-out" / "multi_com.jpg")
        _check(list(res_multi.stripped_keys).count("jpeg:COM") == 1,
               f"COM 键重复：{res_multi.stripped_keys}")
        _check(len(set(res_multi.stripped_keys)) == len(res_multi.stripped_keys),
               f"stripped_keys 有重复项：{res_multi.stripped_keys}")
        _check(res_multi.max_pixel_diff == 0, "多条 COM 用例改了像素")

        # ---- 1d. 字节级 fail-closed：解析器说干净、字节里还有标记时必须报错
        _check(find_dirty_markers(b"xxJFXXyy") == ["JFXX"], "字节级扫描没认出 JFXX")
        _check(find_dirty_markers(b"clean bytes") == [], "字节级扫描误报")
        _check(find_dirty_markers(b"<x:xmpmeta xmlns:x=\"x\"/>",
                                 ) == [], "字节级扫描对普通 xmpmeta 误报")
        buf_keep = BytesIO()
        _pattern_image().save(buf_keep, "JPEG", quality=90)
        keep_bytes = buf_keep.getvalue()
        # APP14（Adobe）是保留段：把脏字节塞进它的载荷，段解析视角完全干净，
        # 只有字节级扫描能拦住——这正是 JFXX 那次漏检的形状。
        sneaky = (keep_bytes[:2]
                  + _jpeg_segment(0xEE, b"Adobe" + b"\x00\x64\x00\x00\x00\x00"
                                  + b"Exif\x00\x00")
                  + keep_bytes[2:])
        sneaky_in = root / "case" / "sneaky.jpg"
        sneaky_in.write_bytes(sneaky)
        try:
            strip_image(sneaky_in, root / "case-out" / "sneaky.jpg")
        except MetadataStripError as exc:
            _check("字节级" in str(exc), f"报错信息没点明字节级检查：{exc}")
        else:
            raise AssertionError("保留段携带 EXIF 字节时字节级兜底没拦住")

        # ---- 2. PNG：tEXt/zTXt/iTXt/tIME/eXIf 全部清掉，像素不变
        png_src = _make_text_png()
        png_in = root / "case" / "chip.png"
        png_in.write_bytes(png_src)
        for probe in (b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"):
            _check(probe in png_src, f"构造的 PNG 缺少 {probe!r}，用例无效")
        png_out = root / "case-out" / "chip.png"
        res_png = strip_image(png_in, png_out)
        stripped_png = png_out.read_bytes()
        for probe in (b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"):
            _check(probe not in stripped_png, f"PNG 仍残留 {probe!r}")
        _check(b"masterpiece" not in stripped_png, "PNG 仍残留生成参数")
        _check(res_png.stripped_keys[:2] == ("png:tEXt:Comment", "png:tEXt:parameters"),
               f"PNG 删除键记录不正确：{res_png.stripped_keys}")
        _check("png:iTXt:XML:com.adobe.xmp" in res_png.stripped_keys,
               "PNG 未记录 iTXt 删除")
        _check("png:eXIf" in res_png.stripped_keys, "PNG 未记录 eXIf 删除")
        _check(residual_metadata(stripped_png) == [], "PNG 残留检测不干净")
        _check(res_png.src_sha256 == sha256_bytes(png_src), "PNG 源哈希记录不正确")
        _check(res_png.dst_sha256 == sha256_bytes(stripped_png), "PNG 产物哈希记录不正确")
        _check(res_png.max_pixel_diff == 0, "PNG 容器重写却改了像素")
        with Image.open(png_in) as a, Image.open(png_out) as b:
            _check(a.size == b.size, "PNG 尺寸变了")
            _check(dict(a.text) != {}, "构造的 PNG 文本块没被 Pillow 读到，用例无效")
            _check(dict(b.text) == {}, "PNG 产物仍有文本块元数据")

        # ---- 2b. 高位深 PNG，以及"像素差检测有没有牙"
        gray16 = Image.new("I;16", (32, 24))
        gray16.putdata([(x * 300 + y * 7) % 65535 for y in range(24) for x in range(32)])
        buf16 = BytesIO()
        gray16.save(buf16, "PNG")
        body16 = buf16.getvalue()
        png16_in = root / "case" / "depth16.png"
        png16_in.write_bytes(body16[:33]
                             + _png_chunk(b"tEXt", b"Software\x00comfyui")
                             + body16[33:])
        png16_out = root / "case-out" / "depth16.png"
        res16 = strip_image(png16_in, png16_out)
        _check(res16.stripped_keys == ("png:tEXt:Software",),
               f"16 位 PNG 删除键记录不正确：{res16.stripped_keys}")
        _check(res16.max_pixel_diff == 0, "16 位 PNG 容器重写却改了像素")
        _check(residual_metadata(png16_out.read_bytes()) == [], "16 位 PNG 残留检测不干净")

        # 负向对照：像素真被改了必须被检出来，否则上面那些 diff == 0 什么也没证明。
        differing = root / "case" / "differing.png"
        patched = _pattern_image()
        patched.putpixel((0, 0), (255, 0, 0))
        patched.save(differing, "PNG")
        _check(pixel_diff(png_in.read_bytes(), differing.read_bytes())[1] > 0,
               "像素差检测对真实差异失效")

        # ---- 3. 不支持的格式：明确报错，不静默放过
        webp = root / "case" / "shot.webp"
        _pattern_image().save(webp, "WEBP")
        try:
            strip_image(webp, root / "case-out" / "shot.webp")
        except UnsupportedImageFormat as exc:
            _check("不支持" in str(exc) and "WebP" in str(exc),
                   f"不支持格式的报错不够明确：{exc}")
        else:
            raise AssertionError("WebP 未被拒绝")
        junk = root / "case" / "notes.txt"
        junk.write_bytes(b"not an image at all")
        try:
            strip_image(junk, root / "case-out" / "notes.txt")
        except UnsupportedImageFormat as exc:
            _check("不支持" in str(exc), f"杂文件报错不够明确：{exc}")
        else:
            raise AssertionError("非图片文件未被拒绝")

        # ---- 4. CLI：目录树、manifest、originals 只读副本
        src_dir = root / "src"
        nested = src_dir / "sub"
        nested.mkdir(parents=True)
        (src_dir / "a.jpg").write_bytes(jpeg_src)
        (nested / "b.png").write_bytes(png_src)
        (src_dir / "readme.txt").write_text("说明文件，应被跳过", encoding="utf-8")
        before = {p: sha256_file(p) for p in (src_dir / "a.jpg", nested / "b.png")}

        out_dir = root / "out"
        orig_dir = root / "originals"
        rc = run(src_dir, out_dir, orig_dir, quiet=True)
        _check(rc == 0, f"CLI 在全部成功时退出码应为 0，实际 {rc}")

        manifest = out_dir / "manifest.jsonl"
        _check(manifest.is_file(), "未生成 manifest.jsonl")
        rows = [json.loads(line) for line in manifest.read_text("utf-8").splitlines()]
        _check(len(rows) == 2, f"manifest 条数应为 2，实际 {len(rows)}")
        _check([r["src"] for r in rows] == ["a.jpg", "sub/b.png"],
               f"manifest 源路径不正确：{[r['src'] for r in rows]}")
        expected_keys = {"src", "dst", "src_sha256", "dst_sha256", "stripped_keys"}
        for row in rows:
            _check(set(row) == expected_keys, f"manifest 字段不符约定：{sorted(row)}")
            _check(row["dst"] == row["src"], "manifest dst 路径未按同名镜像")
            _check(row["src_sha256"] == before[src_dir / row["src"]],
                   f"manifest 源哈希与文件不一致：{row['src']}")
            _check(row["dst_sha256"] == sha256_file(out_dir / row["dst"]),
                   f"manifest 产物哈希与文件不一致：{row['dst']}")
            _check(row["stripped_keys"], f"manifest 未记录删除键：{row['src']}")
            _check(row["src_sha256"] != row["dst_sha256"], "源与产物哈希相同，元数据没清掉")

        for rel in ("a.jpg", "sub/b.png"):
            original = orig_dir / rel
            _check(original.is_file(), f"originals 缺少副本：{rel}")
            _check(sha256_file(original) == before[src_dir / rel],
                   f"originals 副本哈希与源不一致：{rel}")
            _check(not (os.stat(original).st_mode & 0o222),
                   f"originals 副本不是只读：{rel}")
        _check((out_dir / "a.jpg").is_file(), "产物 a.jpg 缺失")
        _check((out_dir / "sub" / "b.png").is_file(), "产物 sub/b.png 缺失")
        _check(not (out_dir / "readme.txt").exists(), "非图片文件被误处理")
        _check(before == {p: sha256_file(p) for p in before}, "源目录被改动")

        # 重跑一次：幂等，且已存在的只读原始副本不被覆盖
        rc2 = run(src_dir, out_dir, orig_dir, quiet=True)
        _check(rc2 == 0, f"重跑退出码应为 0，实际 {rc2}")
        _check(len(manifest.read_text("utf-8").splitlines()) == 2, "重跑后 manifest 条数变了")

        # ---- 5. 不支持格式在批处理里：不中断整批，退出码非 0
        bad_src = root / "src-bad"
        bad_src.mkdir()
        (bad_src / "ok.jpg").write_bytes(jpeg_src)
        (bad_src / "bad.webp").write_bytes(webp.read_bytes())
        rc3 = run(bad_src, root / "out-bad", root / "orig-bad", quiet=True)
        _check(rc3 == 1, f"存在不支持格式时退出码应为 1，实际 {rc3}")
        _check((root / "out-bad" / "ok.jpg").is_file(), "整批被一个坏文件拖停")

        # ---- 6. 产出目录落在原始目录内：直接拒绝
        try:
            run(src_dir, src_dir / "out", orig_dir, quiet=True)
        except MetadataStripError as exc:
            _check("--out" in str(exc), f"重叠目录报错不清楚：{exc}")
        else:
            raise AssertionError("--out 落在 --src 内未被拒绝")

    print("[selftest] metadata_strip: 全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tagger.metadata_strip",
        description="训练输入图重写容器以清除 EXIF/XMP/PNG 文本块，原始文件保留在只读区并记录 sha256")
    parser.add_argument("--src", help="原始图片目录（只读，不会被改写）")
    parser.add_argument("--out", help="清除后的产物目录，manifest.jsonl 默认也写在这里")
    parser.add_argument("--originals", help="原始文件副本目录（副本去写权限位，作只读区）")
    parser.add_argument("--manifest", help="manifest.jsonl 路径，默认 <out>/manifest.jsonl")
    parser.add_argument("--quiet", action="store_true", help="只在出错时输出")
    parser.add_argument("--selftest", action="store_true", help="构造带元数据的样本自检")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    missing = [name for name in ("src", "out", "originals")
               if not getattr(args, name)]
    if missing:
        parser.error("缺少参数：" + " / ".join("--" + m for m in missing))
    try:
        return run(Path(args.src), Path(args.out), Path(args.originals),
                   manifest_path=Path(args.manifest) if args.manifest else None,
                   quiet=args.quiet)
    except (MetadataStripError, OSError) as exc:
        print(f"[strip:error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
