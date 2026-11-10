"""输入发现与归档打包。

一次完整的搬运是：
    指向一个目录（里面可能有 zip 分包、可能有散图、可能有嵌套子目录）
    → 递归发现所有图片
    → 平铺命名（训练侧不需要关心原始目录结构）
    → 打标
    → 按条数或字节数封口归档
    → 记住"上次做到哪"，中断了能接着做

归档支持两种格式，选哪个看下游吃什么：

    zip        自家用。中央目录自带索引，可随机取单条，Windows 直接能打开。
               STORED 不压缩，因为图片已是 JPEG，二次压缩省不到 5%
               却让每次读取都变成解压流。

    webdataset HuggingFace 生态的标准格式：tar 分片，每个样本是
               同 basename 的 `00000000.jpg` + `00000000.json`。
               HF datasets 的 streaming、webdataset 库、以及各类训练框架
               都能直接顺序流式读取，不用整包下载。

两种都不支持"追加"（索引在文件头），所以一律是"写满就封口 + 原子 rename"。
记忆功能解决的是续做问题：重启后跳过已完成的部分，而不是往旧包里塞。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import time
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .archive import IMAGE_SUFFIXES, sha256_of
from .storage.discovery import check_stopped, sorted_files

WORKDONE_NAME = ".packstate.jsonl"

# 能当输入容器的后缀。tar 是必须的：HF 上的数据集（包括我们自己下载的
# webdataset 分包）几乎都是 `.tar`，只认 zip 的话"读取包内图片"对它们等于
# 没打开——页面上看到的是"扫到 0 张"。`.tar.gz` / `.tgz` 也认，但随机取成员
# 要重新解压，比未压缩的 `.tar` 慢一个数量级。
CONTAINER_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")


def container_kind(name: str) -> str:
    """`"zip"` / `"tar"` / `""`（不是容器）。按后缀判断，不看文件头。"""
    low = name.lower()
    if low.endswith(".zip"):
        return "zip"
    if low.endswith((".tar", ".tar.gz", ".tgz")):
        return "tar"
    return ""


def container_stem(container: str) -> str:
    """去掉容器的后缀，给 id / 展示用。`.tar.gz` 要整体去掉，不能只去掉 `.gz`。"""
    low = container.lower()
    for suffix in (".tar.gz", ".tgz", ".tar", ".zip"):
        if low.endswith(suffix):
            return container[: -len(suffix)]
    return container


# ------------------------------------------------------------------ 发现


@dataclass(slots=True)
class InputItem:
    kind: str                 # zip_member | tar_member | loose_file
    source: str               # 容器（zip/tar）的路径，或散图的路径
    member: str = ""          # 容器内的成员名
    rel_path: str = ""        # 原始相对路径，保留在 meta 里供追溯
    flat_name: str = ""       # 归档后使用的名字（平铺）
    size: int = 0
    key: str = ""             # 幂等键，记忆用

    def __post_init__(self) -> None:
        if not self.key:
            self.key = f"{self.source}::{self.member}" if self.member else self.source

    @property
    def is_member(self) -> bool:
        """这张图在容器里（zip 或 tar），不是盘上的散图。

        ★ 判断"是不是包内成员"只能用这个属性。以前各处直接写
        `kind == "zip_member"`，一旦多出一种容器就得逐处改，漏一处就是
        "tar 包里的图画师名/分组/id 全部按散图处理"——错得很安静。
        """
        return self.kind in ("zip_member", "tar_member")

    @property
    def logical_path(self) -> str:
        """用于提取画师名的路径。

        包内成员必须看**包内**路径（member），不能看 rel_path——后者形如
        `subdir/shard-0003.zip::artist/0001.jpg`，第一层是容器文件名，
        拿来当画师名会得到一堆 "shard-0003"。
        散图则用相对路径，第一层就是素材整理时建的画师目录。
        """
        return self.member if self.is_member else self.rel_path


def _is_image(name: str) -> bool:
    return name.lower().endswith(IMAGE_SUFFIXES)


def _skip_member(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base.startswith(".") or base in ("meta.jsonl", "index.json")


class FlatNamer:
    """把任意深度的路径压成一层，冲突时加内容哈希前缀。

    为什么平铺：训练侧是按 basename 组样本的，嵌套目录只会带来
    路径解析的额外分支；而且不同来源的目录名经常撞车（多个 shard 都有 `0001/`）。
    """

    def __init__(self) -> None:
        self._used: set[str] = set()

    def make(self, rel_path: str, *, digest: str = "") -> str:
        base = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
        stem, dot, ext = base.rpartition(".")
        if not dot:
            stem, ext = base, ""
        cand = base
        if cand.lower() in self._used:
            suffix = (digest or hashlib.sha256(rel_path.encode("utf-8")).hexdigest())[:8]
            cand = f"{stem}_{suffix}{dot}{ext}"
            n = 2
            while cand.lower() in self._used:
                cand = f"{stem}_{suffix}_{n}{dot}{ext}"
                n += 1
        self._used.add(cand.lower())
        return cand


def _iter_zip_members(path: Path, *, rel_prefix: str,
                      namer: FlatNamer, should_stop=None) -> Iterator[InputItem]:
    """枚举一个 zip 内的图片成员。

    rel_prefix 只用于展示与追溯（`shard-0003.zip::artist/0001.jpg`）；
    取画师名走 InputItem.logical_path，即包内路径。
    """
    try:
        check_stopped(should_stop)
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                check_stopped(should_stop)
                if info.is_dir() or not _is_image(info.filename):
                    continue
                if _skip_member(info.filename):
                    continue
                yield InputItem(
                    kind="zip_member",
                    source=str(path),
                    member=info.filename,
                    rel_path=f"{rel_prefix}{info.filename}",
                    flat_name=namer.make(info.filename),
                    size=info.file_size,
                )
    except (zipfile.BadZipFile, OSError):
        # 坏包 / 权限不足：跳过它，不要把整批素材卡死在这里。
        return


def _iter_tar_members(path: Path, *, rel_prefix: str,
                      namer: FlatNamer, should_stop=None) -> Iterator[InputItem]:
    """枚举一个 tar 内的图片成员（webdataset 分片就是 tar）。

    用**可 seek** 的 `r:*` 而不是流式 `r|*`：流式模式跳过成员是把数据整段读掉，
    实测 200MB 的包枚举一遍 149ms、可 seek 只要 3ms——对一个几十 GB 的数据集，
    那等于"扫描阶段先把全部图片读一遍"。可 seek 模式按成员大小直接跳到下一个
    头部，代价是把成员表放进内存（一个分片几千条，可以忽略）。

    `.tar.gz` 在下没有便宜的路：seek 要靠解压，整个包会被顺序解一遍。
    webdataset 的成员名是 `000123.jpg` / `000123.json`，非图片成员在这里滤掉。
    """
    try:
        check_stopped(should_stop)
        with tarfile.open(path, "r:*") as tf:
            for info in tf:
                check_stopped(should_stop)
                if not info.isfile() or not _is_image(info.name):
                    continue
                if _skip_member(info.name):
                    continue
                yield InputItem(
                    kind="tar_member",
                    source=str(path),
                    member=info.name,
                    rel_path=f"{rel_prefix}{info.name}",
                    flat_name=namer.make(info.name),
                    size=info.size,
                )
    except (tarfile.TarError, OSError):
        # 坏包 / 没传完的分片：跳过它，不要让整批素材卡在这里。
        return


def _iter_discover(root: Path, *, recursive: bool, include_zips: bool,
                   include_loose: bool, namer: FlatNamer,
                   limit: int | None, should_stop=None) -> Iterator[InputItem]:
    """discover_inputs 的流式内核。

    单独拆出来是因为 build_manifest 只需要按 source 分组计数，不需要把
    几十万条 InputItem 同时攥在手里。公开 API 维持返回 list——cmd_split
    要先全量枚举才能全局分配 id，那是设计使然，不是可以流化的部分。
    """
    found = 0
    for entry in sorted_files(root, recursive=recursive, should_stop=should_stop):
        path = Path(entry.path)
        rel = str(path.relative_to(root)).replace("\\", "/")

        kind = container_kind(path.name)
        if include_zips and kind:
            iter_members = _iter_zip_members if kind == "zip" else _iter_tar_members
            for item in iter_members(path, rel_prefix=f"{rel}::", namer=namer,
                                     should_stop=should_stop):
                yield item
                found += 1
                if limit and found >= limit:
                    return
            continue

        if include_loose and _is_image(path.name) and not path.name.startswith("."):
            yield InputItem(
                kind="loose_file",
                source=str(path),
                rel_path=rel,
                flat_name=namer.make(rel),
                size=entry.stat().st_size,
            )
            found += 1
            if limit and found >= limit:
                return


def discover_inputs(
    root: str | Path,
    *,
    recursive: bool = True,
    include_zips: bool = True,
    include_loose: bool = True,
    namer: FlatNamer | None = None,
    limit: int | None = None,
    should_stop=None,
) -> list[InputItem]:
    """递归发现 root 下的所有图片，含 zip / tar 包内部成员。

    root 也可以直接是一个包文件（`.zip` / `.tar` / `.tar.gz` / `.tgz`）——
    4TB 级素材经常就是单个大包，要求用户先解压再指目录是多余的一步。
    单图路径同样接受。
    """
    check_stopped(should_stop)
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"输入路径不存在: {root}")

    namer = namer or FlatNamer()
    if root.is_file():
        kind = container_kind(root.name)
        if include_zips and kind:
            from itertools import islice
            iter_members = _iter_zip_members if kind == "zip" else _iter_tar_members
            items = iter_members(root, rel_prefix=f"{root.name}::",
                                 namer=namer, should_stop=should_stop)
            try:
                return list(islice(items, limit)) if limit and limit > 0 else list(items)
            finally:
                items.close()
        if include_loose and _is_image(root.name):
            return [InputItem(
                kind="loose_file",
                source=str(root),
                rel_path=root.name,
                flat_name=namer.make(root.name),
                size=root.stat().st_size,
            )]
        raise ValueError(
            f"输入是个文件，但不是包（{'/'.join(CONTAINER_SUFFIXES)}）也不是图片: {root}")

    return list(_iter_discover(root, recursive=recursive, include_zips=include_zips,
                               include_loose=include_loose, namer=namer, limit=limit,
                               should_stop=should_stop))


class ContainerReader:
    """包内成员读取：句柄缓存 + 一把锁。

    zip 每次 open 只读中央目录，很便宜；**tar 的 open 要把整个包扫一遍建成员
    索引**，每张图重开一次的话，一个 8000 张的分片就是 8000 次全扫——比推理
    本身还慢。所以句柄必须留住。

    而 zipfile/tarfile 的读操作内部都会 seek，多线程共用一个句柄不加锁会读到
    别人的字节（错位、偶发、极难复现）。真正的瓶颈是 GPU，锁的开销可以忽略。
    """

    def __init__(self, keep: int = 8) -> None:
        self._keep = max(1, keep)
        self._handles: OrderedDict[str, tuple[str, object]] = OrderedDict()
        self._lock = threading.Lock()

    def _open(self, source: str) -> tuple[str, object]:
        hit = self._handles.get(source)
        if hit is not None:
            self._handles.move_to_end(source)
            return hit
        kind = container_kind(source) or "zip"
        handle = (tarfile.open(source, "r:*") if kind == "tar"
                  else zipfile.ZipFile(source, "r"))
        self._handles[source] = (kind, handle)
        while len(self._handles) > self._keep:
            _, (_, old) = self._handles.popitem(last=False)
            try:
                old.close()                       # type: ignore[union-attr]
            except OSError:
                pass
        return self._handles[source]

    def read(self, source: str, member: str) -> bytes:
        with self._lock:
            kind, handle = self._open(source)
            if kind == "tar":
                fh = handle.extractfile(member)   # type: ignore[union-attr]
                if fh is None:
                    raise KeyError(f"{Path(source).name} 里没有成员 {member}")
                try:
                    return fh.read()
                finally:
                    fh.close()
            return handle.read(member)            # type: ignore[union-attr]

    def close(self) -> None:
        with self._lock:
            for _, handle in self._handles.values():
                try:
                    handle.close()                # type: ignore[union-attr]
                except OSError:
                    pass
            self._handles.clear()


_READERS = ContainerReader()


def read_item_bytes(item: InputItem) -> bytes:
    if item.is_member:
        return _READERS.read(item.source, item.member)
    return Path(item.source).read_bytes()


def close_readers() -> None:
    """收掉缓存里的句柄。进程退出与测试清场时用。"""
    _READERS.close()


# ------------------------------------------------------------------ 记忆


class PackState:
    """记录已完成条目，支持断点续做。

    用追加式 jsonl 而不是整体重写：中断时最多丢最后一行，
    而重写整个大文件在断电时会直接损坏状态。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._done: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._done.add(json.loads(line)["key"])
                except (ValueError, KeyError):
                    continue

    def done(self, key: str) -> bool:
        return key in self._done

    def mark(self, key: str, **extra) -> None:
        if key in self._done:
            return
        self._done.add(key)
        rec = {"key": key, "ts": time.time(), **extra}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @property
    def count(self) -> int:
        return len(self._done)


# ------------------------------------------------------------------ 归档


@dataclass(slots=True)
class PackOptions:
    max_records: int = 5000          # 每包最多多少条
    max_bytes: int = 0               # 每包最多多少字节，0 = 不限
    # 默认 parquet：素材要传到 HF 私有仓库，那边按 datasets 的 parquet 分片
    # 存（客户端用 `load_dataset(..., streaming=True)` 按 row group 拉数据）。
    # tar 分片仍保留，写 ["parquet", "webdataset"] 可以两种都出。
    formats: tuple[str, ...] = ("parquet",)
    compress: str = "stored"

    # 重编码质量（1-100）。默认 95 而不是 92：
    # 92 -> 95 的体积涨幅通常不到 10%，而这是要拿去训模型的图，
    # 省那点盘换画质掉一档不划算。
    jpeg_quality: int = 95
    # 色度下采样：0 = 4:4:4（默认），1 = 4:2:2，2 = 4:2:0。
    #
    # 默认 4:4:4 是项目口径：这批素材要训色感敏感的生成模型，4:2:0 把色度
    # 分辨率砍半，饱和色/细线/文字边缘会出现可见的色晕。体积代价是真实的
    # （实测 1024² 有细节的图，q95 下 4:4:4 约 1427 KB、4:2:0 约 688 KB），
    # 所以它必须是一个**可选项**，不是硬编码——想省体积的人自己在界面选 4:2:0。
    jpeg_subsampling: int = 0
    # 重编码策略：always / non_jpeg / never
    #
    # 默认 always —— 所有图（含已经是 JPEG 的）都按 jpeg_quality 重压一遍。
    # 这是项目主创的决定：宁可吃一次有损，也要把体积统一压到可控范围。
    #
    # 代价必须写在这（实测 1024² 真实照片）：
    #   源 = 已经是 q92 的 JPEG：再压 q92 → PSNR 58.22 / 348KB
    #                            压 q95  → PSNR 43.68 / 446KB (+28%)
    # 即"从 q92 重压到 q95"体积涨 28%、保真度掉 14.5 dB——信息不会因为
    # 编码质量更高而回来，换量化表只是把旧伪影重新抖一遍。
    #
    # 真要减体积，正确方向是下调 jpeg_quality（q90 约 -11%、q85 约 -34%），
    # 而不是把 q 调高。想保留原始字节就设成 non_jpeg / never。
    reencode_mode: str = "always"

    # 清除图片元数据（规范 v0.2 §5 最后一条："任何处理的图像内包含元数据必须清除"）。
    #
    # 重编码的路径本来就不带元数据（Pillow 只写像素），所以这个开关只为
    # **不重编码**的那部分工作：`non_jpeg` 时原样搬运的 JPEG、`never` 时的全部。
    # 关掉它没有正当理由——留着 EXIF 的 GPS/设备号、ComfyUI 的生成参数，
    # 交付出去就是隐私和"这批图是 AI 生成的"双重泄漏。留着开关只为出问题
    # 时能一键排除（比如某批图删掉 ICC 后下游报颜色不对）。
    strip_metadata: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "PackOptions":
        from .config import ConfigError

        d = d or {}
        fmts = d.get("formats") or ["parquet"]
        # yaml 里写 formats: parquet（标量）是很容易犯的笔误。tuple("parquet")
        # 会拆成 ('p','a','r',...)，"parquet"/"webdataset" 的判断永远不命中，
        # 于是什么都不输出、也不报错。这里把标量当单元素列表收下。
        if isinstance(fmts, str):
            fmts = [fmts]
        fmts = tuple(dict.fromkeys(str(f).strip().lower() for f in fmts))
        bad = [f for f in fmts if f not in ("parquet", "webdataset", "zip")]
        if bad:
            raise ConfigError(
                f"archive.formats 只能写 parquet / webdataset / zip，收到 {bad}。"
                f"这三种都不支持追加写，所以一次可以同时产出多份。")
        if not fmts:
            raise ConfigError("archive.formats 不能为空（否则打完整批什么都没有）")

        quality = int(d.get("jpeg_quality", 95))
        if not 90 <= quality <= 100:
            raise ConfigError(
                f"archive.jpeg_quality 只能是 90~100，收到 {quality}。"
                f"低于 90 的二次压缩会在细节纹理上留可见块效应，"
                f"而这批图是要训生成模型的。")

        subsampling = int(d.get("jpeg_subsampling", 0))
        if subsampling not in (0, 1, 2):
            raise ConfigError(
                f"archive.jpeg_subsampling 只能是 0（4:4:4）/ 1（4:2:2）/ "
                f"2（4:2:0），收到 {subsampling}")

        compress = str(d.get("compress", "stored")).lower()
        if compress not in ("stored", "deflate"):
            raise ConfigError(
                f"archive.compress 只能是 stored / deflate，收到 {compress!r}")

        max_records = int(d.get("output_max_records", 5000))
        max_bytes = int(d.get("output_max_bytes", 0))
        if max_records < 0 or max_bytes < 0:
            raise ConfigError("archive.output_max_records / output_max_bytes 不能为负")

        raw_mode = d.get("reencode", "always")
        if isinstance(raw_mode, bool):          # 兼容旧的 true/false
            mode = "always" if raw_mode else "never"
        else:
            mode = str(raw_mode).lower()
        if mode not in ("always", "non_jpeg", "never"):
            raise ConfigError(
                f"archive.reencode 只能是 always / non_jpeg / never（或 true/false），"
                f"收到 {raw_mode!r}")
        return cls(
            max_records=max_records,
            max_bytes=max_bytes,
            formats=fmts,
            compress=compress,
            jpeg_quality=quality,
            jpeg_subsampling=subsampling,
            reencode_mode=mode,
            strip_metadata=_as_flag(d.get("strip_metadata"), True),
        )

    @property
    def suffixes(self) -> tuple[str, ...]:
        """本配置会产出哪些后缀的分片，上传/统计都靠它枚举。"""
        out: list[str] = []
        if "parquet" in self.formats:
            out.append(".parquet")
        if "webdataset" in self.formats:
            out.append(".tar")
        if "zip" in self.formats:
            out.append(".zip")
        return tuple(out)


def _as_flag(value: object, default: bool) -> bool:
    """yaml/表单里的布尔开关。None = 默认值。

    `bool("false")` 是 True——直接套 bool() 会让"显式关掉"变成"开着"，
    而这种错只在跑完一批后看产物才现形。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "none")


def _is_jpeg(data: bytes) -> bool:
    """靠 SOI 标记判断，不看扩展名。

    素材从各处下载而来，扩展名和实际编码对不上的情况很常见
    （`.jpg` 里装着 PNG、无扩展名的 WebP）。按魔数判断才不会把一张
    本来无损的 PNG 当成 JPEG 直接搬过去。
    """
    return len(data) >= 3 and data[:3] == b"\xff\xd8\xff"


def transcode_to_jpeg(data: bytes, quality: int, subsampling: int = 0) -> bytes:
    """把任意格式重编码成 JPEG。

    两个必须处理的细节：
    - **alpha 必须铺白底**：JPEG 没有 alpha 通道，Pillow 遇到 RGBA 直接抛
      `cannot write mode RGBA as JPEG`；而直接丢 alpha 会得到黑底图，
      训练数据里一片黑比"多一层白底"伤害大得多。

    - **subsampling 默认 0（4:4:4）**。这是项目口径：4:2:0 把色度分辨率
      砍半，饱和色/细线/文字边缘会有可见色晕，而这批图要训色感敏感的
      生成模型。体积代价是真实的（1024² 有细节的图，原图 q92/4:2:0 = 540 KB）：

          q95 / 4:4:4  → 1427 KB  (+164%)
          q95 / 4:2:0  →  688 KB  (+28%)
          q90 / 4:2:0  →  481 KB  (-11%)
          q85 / 4:2:0  →  358 KB  (-34%)

      想换体积的人把 subsampling 设成 1（4:2:2）或 2（4:2:0）——它是显式
      选项，不是隐藏默认。别把默认改回 2：那就等于悄悄降质。
    """
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            out = flat
        else:
            out = im.convert("RGB")
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=quality, subsampling=subsampling,
                 optimize=False)
    return buf.getvalue()


def strip_metadata(data: bytes) -> bytes:
    """去掉图片容器里的元数据段，像素不动（规范 v0.2 §5 最后一条）。

    ★为什么"重编码就已经清掉了"不够：`reencode_mode` 可以是 `non_jpeg` 或
      `never`，那两档是**原字节直搬**，于是 EXIF（GPS、设备序列号）、PNG 的
      tEXt/iTXt（ComfyUI 的完整生成参数）、XMP（编辑历史）会原样进交付包。
      而这些正是最容易带出隐私和"这张图是 AI 生成的"痕迹的部分。

    ★为什么不走 Pillow 的 save：重新编码一次等于再压一道（体积、伪影），
      而这里要的只是删掉几个段。JPEG 按段重写，PNG/WebP 走容器级处理。

    认不出的格式原样返回：宁可没清掉，也不要为了一条规则把图弄坏——
    调用方会在归档检查里报出来（见 metadata_report）。
    """
    if len(data) < 4:
        return data
    if data[:2] == b"\xff\xd8":
        return _strip_jpeg(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _strip_png(data)
    return data


def _strip_jpeg(data: bytes) -> bytes:
    """JPEG：丢掉 APPn（除 APP0/JFIF 与 APP14/Adobe）和 COM 段。

    保留 APP0 与 APP14 是有意的：前者承载 JFIF 的密度信息，后者是 CMYK 图的
    色彩变换标记，删掉会让解码器把颜色解错（图上不会有提示，只是颜色不对）。
    APP1 是 EXIF/XMP，APP2 是 ICC 与 MPF，APP13 是 Photoshop 的 IPTC——
    这些才是要清的。ICC 一起丢：交付的是 sRGB 语义的 JPEG，留着别人的色彩
    配置文件只会让下游对同一张图算出两种颜色。
    """
    out = bytearray(data[:2])
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            # 段结构与预期不符（坏文件/非标准）：从这里到结尾原样带走，
            # 不做"猜着重同步"——那会吐出比原文件更坏的字节。
            out += data[i:]
            break
        marker = data[i + 1]
        # 填充字节（0xFF 0xFF…）与 SOI/EOI/TEM 没有长度字段
        if marker in (0xFF, 0xD8, 0x01):
            out += data[i:i + 2]
            i += 2
            continue
        if marker == 0xD9:
            out += data[i:i + 2]
            i += 2
            break
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            out += data[i:]
            break
        end = min(n, i + 2 + seg_len)
        keep = not (
            # APP1..APP15，但 APP14（Adobe）要留：它标记 CMYK 的变换方式
            (0xE1 <= marker <= 0xEF and marker != 0xEE)
            or marker == 0xFE
        )
        if keep:
            out += data[i:end]
        i = end
        if marker == 0xDA:          # SOS：之后是熵编码数据，遇到就整体带走
            out += data[i:]
            break
    return bytes(out)


def _strip_png(data: bytes) -> bytes:
    """PNG：只留必要块，去掉 tEXt/zTXt/iTXt/eXIf/tIME 等文本与元数据块。

    白名单而不是黑名单：PNG 的块类型是私有的、可扩展的（C2PA 的 caBX、
    ComfyUI 的私有块都往里塞），黑名单永远漏。留下的是解码必需（IHDR/PLTE/
    IDAT/IEND/tRNS）与色彩管理必需（gAMA/sRGB/cHRM/iCCP/pHYs）的那几类。
    """
    keep = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"sRGB",
            b"cHRM", b"iCCP", b"pHYs", b"bKGD", b"sBIT", b"hIST"}
    out = bytearray(data[:8])
    i = 8
    n = len(data)
    while i + 8 <= n:
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            break
        if ctype in keep:
            out += data[i:end]
        if ctype == b"IEND":
            break
        i = end
    return bytes(out)


def metadata_report(data: bytes) -> str:
    """这张图里还留着什么元数据。空串 = 干净。

    ★返回**具体是哪种**而不是 bool：`strip_metadata` 认不出的格式会原样
    返回，如果这里只回 False，那种情况看起来和"清干净了"一模一样——而它们
    正好是两个相反的事实。抽检报"unknown"时该去补那一条分支。

    只看 JPEG 的 EXIF/XMP/IPTC/ICC 与 PNG 的文本块：这两类占了实际遇到的
    绝大多数，而 WebP/AVIF 的元数据要到容器层解析，判错的代价（误报整批
    不合格）比漏报大。
    """
    if data[:2] == b"\xff\xd8":
        head = data[: 64 * 1024]     # EXIF/XMP 段都在文件头，不必扫全图
        for marker, name in ((b"Exif\x00\x00", "exif"),
                             (b"http://ns.adobe.com/xap", "xmp"),
                             (b"Photoshop 3.0", "iptc"),
                             (b"ICC_PROFILE", "icc")):
            if marker in head:
                return name
        return ""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        i, n = 8, min(len(data), 256 * 1024)
        while i + 8 <= n:
            length = int.from_bytes(data[i:i + 4], "big")
            ctype = data[i + 4:i + 8]
            if ctype in (b"tEXt", b"zTXt", b"iTXt", b"eXIf"):
                return f"png:{ctype.decode('ascii')}"
            if ctype == b"IEND":
                return ""
            i += 12 + length
        return ""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        # WebP 的元数据在 RIFF 的 EXIF/XMP 块里，块头就是四字节标识。
        return "webp:exif" if b"EXIF" in data[:4096] else ""
    return "unknown"


@dataclass(slots=True)
class SealedPack:
    name: str
    count: int
    bytes: int
    sha256: str


@dataclass(slots=True)
class PreparedSample:
    """转码完、序列化完，就等着写进归档的一条样本。

    把"准备"和"写"拆开是并行的前提：准备阶段（解码 + JPEG 编码 + JSON）
    是纯 CPU、可以扔进线程池；写阶段要碰 tarfile/zipfile 的共享句柄，
    必须串行。两者混在一个函数里，就只能整体串行。
    """

    key: str
    flat_name: str
    image_bytes: bytes
    payload: bytes
    nl: str
    tag_text: str
    meta: dict
    width: int
    height: int


class Packer:
    """按条数/字节数自动封口，同时可出 zip 和 webdataset 两种归档。"""

    def __init__(self, out_dir: str | Path, options: PackOptions, *,
                 worker: str = "w", model_id: str = "", verbose: bool = True,
                 buckets=None, max_pixels: int = 0):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.options = options
        self.worker = worker
        self.model_id = model_id
        self.verbose = verbose
        # 分桶表。传了就自动给每条样本标注 bucket，调用方不必自己算。
        self._buckets = list(buckets) if buckets else []
        self._max_pixels = max_pixels

        self._seq = self._next_seq()
        self._count = 0
        self._bytes = 0
        self._zip: zipfile.ZipFile | None = None
        self._zip_path: Path | None = None
        self._zip_count = 0
        self._tar: tarfile.TarFile | None = None
        self._tar_path: Path | None = None
        self._tar_count = 0
        self._index: list[dict] = []
        # parquet 的行缓冲。tar/zip 能边写边落盘，parquet 不行——
        # 它的 footer 在文件末尾，必须整表写完才能关文件。
        self._pq_rows: list[dict] = []
        self._closed: list[SealedPack] = []

    def _next_seq(self) -> int:
        existing = [p for p in self.out_dir.iterdir()
                    if p.suffix in (".zip", ".tar", ".parquet") and self.worker in p.name]
        if not existing:
            return 0
        nums = []
        for p in existing:
            tail = p.stem.rsplit("-", 1)[-1]
            if tail.isdigit():
                nums.append(int(tail))
        return max(nums) + 1 if nums else 0

    def _base(self) -> str:
        return f"pack-{self.worker}-{self._seq:05d}"

    # ------------------------------------------------------ 打开写句柄

    def _open_zip(self) -> zipfile.ZipFile:
        if self._zip is not None:
            return self._zip
        path = self.out_dir / f"{self._base()}.zip"
        tmp = path.with_suffix(".zip.tmp")
        comp = zipfile.ZIP_STORED if self.options.compress == "stored" else zipfile.ZIP_DEFLATED
        self._zip = zipfile.ZipFile(tmp, "w", compression=comp, allowZip64=True)
        self._zip_path = tmp
        self._zip_count = 0
        self._index = []
        return self._zip

    def _open_tar(self) -> tarfile.TarFile:
        if self._tar is not None:
            return self._tar
        path = self.out_dir / f"{self._base()}.tar"
        tmp = path.with_suffix(".tar.tmp")
        self._tar = tarfile.open(tmp, mode="w")
        self._tar_path = tmp
        self._tar_count = 0
        return self._tar

    # ------------------------------------------------------ 写入

    def add(self, *, flat_name: str, image_bytes: bytes, meta: dict,
            nl: str = "", tag_text: str = "",
            annotation: str = "") -> SealedPack | None:
        """写入一条样本，返回封口结果或 None。

        每个样本在归档里落成三个文件，同一个 basename：
            <key>.jpg   原图
            <key>.json  元信息（宽高、桶、画师、provenance）
            <key>.nl    NL 描述      —— 仅在产出了 NL 时写
            <key>.tag   tag 串       —— 仅在产出了 tag 时写

        为什么 NL 和 tag 分成两个文件而不是塞进 json：
        训练侧读 NL 和读 tag 是两条不同的路径（一个当 prompt 文本，一个做
        检索/分桶），分成独立成员后各自可以流式直读，不必解析 json 再取字段。
        用 .nl / .tag 这种自定义扩展名是 WebDataset 允许的，它按后缀分组，
        不限定具体名字。

        annotation 是旧参数，等价于 nl，保留是为了不破坏既有调用。

        批量场景请用 prepare() + write()（或模块级 pack_stream），
        把可以并行的转码和必须串行的归档写入分开。
        """
        if annotation and not nl:
            nl = annotation
        prepared = self.prepare(flat_name=flat_name, image_bytes=image_bytes,
                                meta=meta, nl=nl, tag_text=tag_text)
        return self.write(prepared)

    def prepare(self, *, flat_name: str, image_bytes: bytes, meta: dict,
                nl: str = "", tag_text: str = "") -> PreparedSample:
        """转码 + 组 payload。纯函数，不碰任何共享句柄，可以在线程池里跑。

        图片转码是整条 pack 里最贵的一步（1024² 约 15ms/张），而它只吃 CPU
        和自己那份数据——这正是能并行的部分。实测 8 线程 1.96×（受 GIL 与
        内存带宽限制，不是核数倍）。
        """
        # 统一转码。扩展名跟着改，否则包内会出现"叫 .png 的 JPEG"，
        # 而下游是按扩展名选解码器的（webdataset 尤其严格）。
        mode = self.options.reencode_mode
        # 带 alpha 的图也照转，透明区域铺白底（transcode_to_jpeg 里处理）。
        # 这是明确的流程约定：下游训练管线不支持 alpha，留着透明通道反而
        # 会让"这张图到底长什么样"取决于训练侧用什么解码器——
        # 统一压平成白底，是唯一能跨工具一致的选择。
        if mode == "always" or (mode == "non_jpeg" and not _is_jpeg(image_bytes)):
            image_bytes = transcode_to_jpeg(image_bytes, self.options.jpeg_quality,
                                            self.options.jpeg_subsampling)
            # 只换扩展名，**不能**用 Path().stem——mirror 布局下路径里带目录，
            # stem 会把 `alice/sub/1.png` 压成 `1.jpg`，目录结构静默丢失。
            posix = flat_name.replace("\\", "/")
            head, _, tail = posix.rpartition("/")
            base = (Path(tail).stem or tail) + ".jpg"
            flat_name = f"{head}/{base}" if head else base
        elif self.options.strip_metadata:
            # 不重编码的条目（non_jpeg 的原 JPEG、never 的全部）原字节直搬，
            # 元数据会跟着走。这里只删段、不动像素，见 strip_metadata。
            image_bytes = strip_metadata(image_bytes)

        key = flat_name.rsplit(".", 1)[0]
        w = int(meta.get("width") or 0)
        h = int(meta.get("height") or 0)

        # 强制带上样本标识。复核时要拿它跟输入清单对账，
        # 缺了这个字段就只能靠 tar 里的序号猜，而序号在分片之间是不唯一的。
        # 调用方已经给了就不覆盖。
        meta = {**meta}
        meta.setdefault("name", flat_name)
        meta.setdefault("key", key)

        # 分桶标注。放在写 json 之前，因为 bucket 要进 payload。
        # 用副本，避免改动调用方传进来的 dict。
        if self._buckets and w and h and not meta.get("bucket"):
            from .bucket import assign_bucket

            b = assign_bucket(w, h, self._buckets, max_pixels=self._max_pixels or 0)
            if b is not None:
                meta = {**meta, "bucket": b.name,
                        "bucket_size": [b.width, b.height]}

        return PreparedSample(
            key=key,
            flat_name=flat_name,
            image_bytes=image_bytes,
            payload=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
            nl=(nl or "").strip(),
            tag_text=(tag_text or "").strip(),
            meta=meta,
            width=w,
            height=h,
        )

    def write(self, sample: PreparedSample) -> SealedPack | None:
        """把准备好的样本写进归档。**必须串行**：tarfile/zipfile 的句柄
        内部会 seek，多线程共用会写串字节（错位、偶发、极难复现）。"""
        sealed: list[SealedPack] = []
        key = sample.key
        flat_name = sample.flat_name
        image_bytes = sample.image_bytes
        payload = sample.payload
        nl = sample.nl
        tag_text = sample.tag_text
        meta = sample.meta
        w, h = sample.width, sample.height

        if "parquet" in self.options.formats:
            # 图片以裸 bytes 存（pyarrow binary 列），不做 Image feature 编码：
            # 客户端要的正是原始字节，多一层 HF Image 解码只会让流式读更慢，
            # 而且 Image feature 会在 parquet 里额外记录解码元数据。
            self._pq_rows.append({
                "image_id": str(meta.get("image_id") or key),
                "image": image_bytes,
                "artist": str(meta.get("artist") or ""),
                # table = 按 post id 查表得来；path = 目录名。下游靠它决定
                # "包内这个值能不能压过查表结果"，没有就按 path 保守处理。
                "artist_source": str(meta.get("artist_source") or ""),
                "caption": nl,
                "tag": tag_text,
            })

        if "zip" in self.options.formats:
            zf = self._open_zip()
            zf.writestr(flat_name, image_bytes)
            zf.writestr(f"{key}.json", payload)
            if nl:
                zf.writestr(f"{key}.nl", nl)
            if tag_text:
                zf.writestr(f"{key}.tag", tag_text)
            self._index.append({
                "name": flat_name,
                "key": key,
                "size": len(image_bytes),
                "wh": [w, h] if w and h else [],
                "bucket": meta.get("bucket", ""),
                "has_nl": bool(nl),
                "has_tag": bool(tag_text),
            })
            self._zip_count += 1

        if "webdataset" in self.options.formats:
            tf = self._open_tar()
            # 扩展名必须与实际字节一致，WebDataset 的下游按扩展名选解码器
            ext = Path(flat_name).suffix.lower() or ".jpg"
            members: list[tuple[str, bytes]] = [
                (ext, image_bytes),
                (".json", payload),
            ]
            if nl:
                members.append((".nl", nl.encode("utf-8")))
            if tag_text:
                members.append((".tag", tag_text.encode("utf-8")))

            # 成员名用样本标识，不用 00000000 这种顺序号。
            # 顺序号解包后会变成一堆无意义的数字文件，而我们要的是解出来就是
            # `画师-000123.jpg`——能不能对上人是靠名字，不是靠 tar 里的位次。
            # WebDataset 本身只要求同名不同后缀，并不限定 stem 的形态。
            # mirror 布局下 key 里带目录，tar 内自然形成层级。
            stem = key.replace("\\", "/")
            for suffix, data in members:
                info = tarfile.TarInfo(f"{stem}{suffix}")
                info.size = len(data)
                info.mtime = int(time.time())
                tf.addfile(info, io.BytesIO(data))
            self._tar_count += 1

        self._count += 1
        self._bytes += len(image_bytes)

        if self._should_seal():
            s = self.seal()
            if s:
                sealed.append(s)

        return sealed[0] if sealed else None

    def _should_seal(self) -> bool:
        o = self.options
        if o.max_records and self._count >= o.max_records:
            return True
        if o.max_bytes and self._bytes >= o.max_bytes:
            return True
        return False

    # ------------------------------------------------------ 封口

    def _finalize_zip(self) -> SealedPack | None:
        if self._zip is None or self._zip_path is None:
            return None
        zf, tmp = self._zip, self._zip_path
        final = tmp.with_suffix("")  # 去掉 .tmp
        index = {
            "schema": 1, "worker": self.worker, "model_id": self.model_id,
            "count": self._zip_count, "ts": time.time(), "entries": self._index,
        }
        zf.writestr("index.json", json.dumps(index, ensure_ascii=False))
        zf.close()
        with tmp.open("rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, final)
        pack = SealedPack(name=final.name, count=self._zip_count,
                          bytes=final.stat().st_size, sha256=sha256_of_file(final))
        self._zip = None
        self._zip_path = None
        self._zip_count = 0
        self._index = []
        return pack

    def _finalize_tar(self) -> SealedPack | None:
        if self._tar is None or self._tar_path is None:
            return None
        tf, tmp = self._tar, self._tar_path
        tf.close()
        final = tmp.with_suffix("")
        with tmp.open("rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, final)
        pack = SealedPack(name=final.name, count=self._tar_count,
                          bytes=final.stat().st_size, sha256=sha256_of_file(final))
        self._tar = None
        self._tar_path = None
        self._tar_count = 0
        return pack

    def _finalize_parquet(self) -> SealedPack | None:
        """写一个 parquet 分片（一次 seal = 一个分片）。

        compression="zstd"：图片已经是 JPEG（不可再压），但 caption/tag 是文本，
        zstd 能把这一列压掉一半左右，而解码开销可以忽略。
        """
        if not self._pq_rows:
            return None
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError(
                "写 parquet 需要 pyarrow：pip install pyarrow（或用 formats=[webdataset]）"
            ) from exc

        rows = self._pq_rows
        table = pa.table({
            "image_id": pa.array([r["image_id"] for r in rows], type=pa.string()),
            "image": pa.array([r["image"] for r in rows], type=pa.binary()),
            "artist": pa.array([r["artist"] for r in rows], type=pa.string()),
            "artist_source": pa.array([r["artist_source"] for r in rows],
                                      type=pa.string()),
            "caption": pa.array([r["caption"] for r in rows], type=pa.string()),
            "tag": pa.array([r["tag"] for r in rows], type=pa.string()),
        })
        path = self.out_dir / f"{self._base()}.parquet"
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp, compression="zstd")
        with tmp.open("rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
        pack = SealedPack(name=path.name, count=len(rows),
                          bytes=path.stat().st_size, sha256=sha256_of_file(path))
        self._pq_rows = []
        return pack

    def seal(self) -> SealedPack | None:
        packs = [p for p in (self._finalize_zip(), self._finalize_tar(),
                             self._finalize_parquet()) if p]
        if not packs:
            return None
        self._closed.extend(packs)
        self._count = 0
        self._bytes = 0
        self._seq += 1
        if self.verbose:
            for p in packs:
                print(f"[pack] 封口 {p.name}  {p.count} 条  {p.bytes / 1048576:.1f} MB")
        return packs[0]

    def close(self) -> list[SealedPack]:
        if self._count > 0:
            self.seal()
        return self._closed

    @property
    def sealed(self) -> list[SealedPack]:
        """本次会话已经封口的所有分片（跨格式）。

        write() 只返回第一个封口结果（兼容旧调用），同时出 parquet+tar 时
        调用方需要看到全部，用它取。
        """
        return list(self._closed)

    def __enter__(self) -> "Packer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def sha256_of_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _flush_window(pool: ThreadPoolExecutor, packer: Packer,
                  window: list[dict]) -> Iterator[SealedPack]:
    prepared = list(pool.map(lambda kw: packer.prepare(**kw), window))
    for p in prepared:
        sealed = packer.write(p)
        if sealed:
            yield sealed


def pack_stream(packer: Packer, samples: Iterable[dict], *, workers: int = 1,
                window: int | None = None) -> Iterator[SealedPack]:
    """批量打包：转码在线程池里跑，归档写入留在主线程。

    samples 是 `prepare()` 的参数字典序列（flat_name / image_bytes / meta /
    nl / tag_text）。产出顺序与输入一致——顺序一变，"第 3 包和第 40 包差多少"
    这种对比就没法做了，而顺序号是写进包名的，事后对不上会非常难查。

    window 控制一次预取多少条。默认 workers*4：太小等于没并行（线程池一直
    在等主线程消费），太大则是把转码后的图片堆在内存里（每张几 MB，
    几万条就是几十 GB）。

    实测（24 核 / 1024² q95）：8 线程 1.96×。瓶颈在 Pillow 只在部分阶段
    释放 GIL，以及内存带宽——不是核数倍，别按核数派活。
    """
    if workers <= 1:
        for kw in samples:
            sealed = packer.add(**kw)
            if sealed:
                yield sealed
        return

    batch = max(2, window or workers * 4)
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="pack") as pool:
        buf: list[dict] = []
        for kw in samples:
            buf.append(kw)
            if len(buf) >= batch:
                yield from _flush_window(pool, packer, buf)
                buf = []
        if buf:
            yield from _flush_window(pool, packer, buf)


def build_manifest(root: str | Path, out: str | Path | None = None, *,
                   recursive: bool = True) -> Path:
    """为输入目录（含 zip）生成全局清单，成员可据此只领自己那份。

    走流式内核逐条聚合：输入几十万张时不把 InputItem 全量攥在内存里。
    """
    root_path = Path(root)
    out = Path(out) if out else root_path / "manifest.jsonl"
    agg: dict[str, dict] = {}
    for it in _iter_discover(root_path, recursive=recursive, include_zips=True,
                             include_loose=True, namer=FlatNamer(), limit=None):
        row = agg.setdefault(it.source, {
            "source": it.source, "kind": it.kind, "count": 0, "bytes": 0,
        })
        row["count"] += 1
        row["bytes"] += it.size

    with out.open("w", encoding="utf-8") as f:
        for source in sorted(agg):
            row = agg[source]
            row["digest"] = sha256_of(source.encode("utf-8"))[:16]
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out
