"""HuggingFace 私有仓库的 webdataset 输入（一次只落一个 shard）。

为什么不是"边下边读"
--------------------
理论上可以做 HTTP range + tar 流式解析，把内存占用压到一个图片大小。但代价是：
  - tar 没有中央目录，必须从头顺序走，一旦某个 shard 里有个坏块，整段就废；
  - 不能随机重试单张图（网络抖一下就丢了那个样本，且没法回头补）；
  - 断点续做只能靠"读到第几个成员"，而这个位置在分片重传后会变。

折中方案是**按 shard 为单位**落盘：一次只下载一个 tar（通常 0.5~2 GB），
处理完立刻删掉，再下一个。成员 SSD 上只需要腾出一个 shard 的空间，
而不是整个数据集（4~8 TB）。相比真流式，代价只是"当前 shard 的临时占用"。

这也让下游逻辑完全不变：解出来的 tar 就是普通的本地包，Reader 照旧读、
产物照旧写、失败照旧重试——不需要为流式单独维护一条代码路径。

与 sucai 的关系
---------------
H:\\sucai 那边用的是 `datasets.load_dataset(..., streaming=True)`，走的是
HF datasets 的 parquet/arrow 格式，不是 tar 分片。两者不能混用：
parquet 数据集要按列解码，webdataset 是 tar + 同 stem 多后缀。这里实现的是
后者（本项目的 Packer 产出的就是 webdataset 格式），API 只用到 HF 的
tree 列表接口和 resolve 直链，不引额外依赖。
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import httpx

HF_API = "https://huggingface.co/api/datasets"
HF_RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}"

# webdataset 里同一 stem 的各种成员。图片成员按这些后缀识别。
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")


class HFError(RuntimeError):
    """网络/权限/格式问题。信息里要带"下一步怎么查"，不要只抛状态码。"""


@dataclass(slots=True)
class HFShard:
    """一个待处理的 tar。path 是本地临时文件，处理完必须删。"""

    repo: str
    filename: str
    path: Path
    bytes: int = 0
    temporary: bool = True


def list_shards(
    repo: str,
    *,
    revision: str = "main",
    pattern: str = "*.tar",
    token: str | None = None,
    timeout_s: int = 60,
) -> list[str]:
    """列出仓库里的分片文件名（递归）。

    用 tree 接口而不是 list_repo_files：前者能一次拿到大小，便于按 shard 排错；
    而且不依赖 huggingface_hub（少一个必装依赖）。

    必须跟随 Link: rel="next" 头翻页：单页超过 1000 条时 HF 只回第一页，
    不翻页的话千万级仓库会静默丢掉后面所有分片——数量不对但没有任何报错。
    """
    url = f"{HF_API}/{repo}/tree/{revision}"
    headers = _auth(token)
    params: dict | None = {"recursive": "true"}
    names: list[str] = []
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as c:
            while url:
                r = c.get(url, headers=headers, params=params)
                if r.status_code == 401:
                    raise HFError(
                        f"{repo} 需要鉴权（401）。私有仓库请先 `huggingface-cli login`，"
                        f"或设环境变量 HF_TOKEN。")
                if r.status_code == 404:
                    raise HFError(
                        f"{repo} 不存在（404）。确认是 datasets 仓库，且名字拼写对"
                        f"（形如 org/name，不要带 https:// 前缀）。")
                r.raise_for_status()
                entries = r.json()
                if not isinstance(entries, list):
                    raise HFError(
                        f"{repo} 的 tree 返回结构不认识：{type(entries).__name__}")
                names.extend(e.get("path", "") for e in entries
                             if e.get("type") == "file")
                # httpx 已把 Link 头解析成 {rel: {"url": ...}}；next 指向
                # 同一资源就继续翻，没有 next（或指向别处）就收手。
                nxt = (r.links.get("next", {}).get("url") if r.links else None)
                if nxt and nxt.split("?")[0] == url.split("?")[0]:
                    url, params = nxt, None
                else:
                    url, params = None, None
    except httpx.HTTPError as exc:
        raise HFError(f"列分片失败：{type(exc).__name__}: {exc}") from exc

    glob_re = re.compile(re.escape(pattern).replace(r"\*", ".*") + r"$")
    out = [n for n in names if glob_re.match(n)]
    return sorted(out)


def resolve_url(repo: str, filename: str, *, revision: str = "main") -> str:
    return HF_RESOLVE.format(repo=repo, revision=revision, path=filename)


def download_shard(
    repo: str,
    filename: str,
    dest_dir: str | Path,
    *,
    revision: str = "main",
    token: str | None = None,
    timeout_s: int = 600,
    max_attempts: int = 3,
    verbose: bool = True,
) -> HFShard:
    """下载一个 tar 到 dest_dir，原子落盘。

    先写 `.part` 再 rename：中途断网留下半截文件的话，tarfile 会读到一半
    才报错（甚至读到"看起来完整"的截断包），而原子 rename 让"文件存在"
    等于"下载完整"。
    """
    name = Path(filename).name or "shard.tar"
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / name

    if final.is_file() and final.stat().st_size > 0:
        return HFShard(repo=repo, filename=filename, path=final,
                       bytes=final.stat().st_size, temporary=False)

    url = resolve_url(repo, filename, revision=revision)
    part = final.with_name(final.name + ".part")
    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            t0 = time.time()
            with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=20.0),
                              follow_redirects=True) as c:
                with c.stream("GET", url, headers=_auth(token)) as r:
                    if r.status_code in (401, 403):
                        raise HFError(
                            f"{repo}/{filename} 需要鉴权（HTTP {r.status_code}）。"
                            f"私有仓库要带 HF_TOKEN。")
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    written = 0
                    with part.open("wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                            written += len(chunk)
                    f_size = part.stat().st_size
            if total and f_size != total:
                raise HFError(f"下载不完整：{f_size}/{total} 字节")
            part.replace(final)
            if verbose:
                mb = final.stat().st_size / 1048576
                print(f"[hf] {name}  {mb:.1f} MB  {time.time() - t0:.1f}s", flush=True)
            return HFShard(repo=repo, filename=filename, path=final,
                           bytes=final.stat().st_size)
        except HFError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            last_err = exc
            if attempt >= max_attempts:
                break
            wait = min(5.0 * attempt, 30.0)
            print(f"[hf][警告] {name} 第 {attempt} 次失败（{type(exc).__name__}），"
                  f"{wait:.0f}s 后重试", flush=True)
            time.sleep(wait)

    if part.exists():
        try:
            part.unlink()
        except OSError:
            pass
    raise HFError(
        f"{name} 下载失败（已重试 {max_attempts} 次）：{last_err}。"
        f"检查网络/磁盘空间，或先用浏览器下一份放到 --hf-cache 目录下再跑。")


def _auth(token: str | None) -> dict[str, str]:
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ------------------------------------------------------------------ parquet


def iter_parquet_samples(
    source: str,
    *,
    image_column: str = "image",
    token: str | None = None,
    repo: str = "",
    shard: str = "",
    path_template: str = "{image_id}.jpg",
    id_columns: Sequence[str] = ("image_id", "id", "file_name", "path", "name", "key"),
    artist_column: str = "artist",
    skip: int = 0,
) -> Iterator[dict]:
    """流式读一个 parquet 分片，逐行产出图片字节。

    `source` 可以是本地路径、https 直链，或 `hf://datasets/<repo>/<file>`。
    用 datasets 的 streaming：它按 row group 拉数据，不会把整个分片落盘，
    所以成员机器只需要一点缓存空间——这正是"SSF 太贵"要的效果。

    和 sucai 那边是同一套机制（`load_dataset(..., streaming=True)`），
    区别只在它是给训练喂样本，这里是把样本变成打标队列。
    """
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise HFError(
            "读 parquet 需要 datasets：pip install datasets pyarrow"
        ) from exc

    uri = source
    if source.startswith("hf://") or Path(source).exists():
        uri = source
    elif repo:
        uri = f"hf://datasets/{repo}/{source}"

    try:
        ds = load_dataset("parquet", data_files={"train": uri}, split="train",
                          streaming=True, token=token)
    except Exception as exc:  # noqa: BLE001 - datasets 的异常类型太杂
        raise HFError(
            f"打开 {uri} 失败：{type(exc).__name__}: {exc}\n"
            f"私有仓库记得带 token；本地文件就给绝对路径。") from exc

    # Image feature 默认会把每张图解码成 PIL 再交出来——而我们要的就是
    # 原始字节（下游自己按扩展名解码、该转码的地方自有统一转码）。
    # PIL 中转一遍等于每张图付一次解码 + 有损重编码（quality=95 的 JPEG），
    # PNG 源会被悄悄变成 JPEG。decode=False 拿回 {"bytes": ...} 原始字节；
    # 列类型不是 Image feature 时 cast 失败，按老路径走。
    try:
        from datasets import Image as _HFImage

        ds = ds.cast_column(image_column, _HFImage(decode=False))
    except Exception:        # noqa: BLE001 - 列不存在/已是 bytes 时回退
        pass

    stem = Path(shard or source).stem
    for idx, row in enumerate(ds):
        if idx < skip:
            continue
        raw = _image_bytes(row.get(image_column), source=uri, row_index=idx)
        image_id = _row_image_id(row, id_columns, fallback=f"{stem}-{idx:06d}")
        artist = str(row.get(artist_column) or "")
        yield {
            "image_id": image_id,
            "artist": artist,
            "data": raw,
            "image_path": path_template.format(image_id=image_id, index=idx,
                                               shard=stem, repo=repo),
            "row_index": idx,
        }


def _row_image_id(row: dict, columns: Sequence[str], *, fallback: str) -> str:
    """从行的 id 类列里取 image_id；取不到就用 `分片名-行号`。

    行号兜底是稳定的：parquet 的行序在文件不变时固定，重跑能对上，
    而随机 id 会让"断点续做"永远对不上已完成产物。
    """
    for col in columns:
        val = row.get(col)
        if val in (None, ""):
            continue
        text = str(val).strip()
        if not text:
            continue
        from .naming import sanitize

        stem = Path(text).stem or text
        cleaned = sanitize(stem)
        if cleaned:
            return cleaned
    return fallback


def _image_bytes(value, *, source: str = "", row_index: int = -1) -> bytes:
    """把 parquet 里的一行图片值变成字节。

    四种形态都要认（取决于写 parquet 时用的是 Image feature 还是裸 bytes）：
        PIL.Image        datasets 解码 Image feature 的结果
        {"bytes": ...}   Image feature 未解码 / save_to_disk 的形态
        {"path": ...}    指向仓库内文件的引用
        bytes            裸二进制列
    """
    from io import BytesIO

    from PIL import Image

    if value is None:
        raise HFError(f"{source} 第 {row_index} 行没有图片列（列名对不对？）")

    if isinstance(value, (bytes, bytearray)):
        return bytes(value)

    if isinstance(value, Image.Image):
        buf = BytesIO()
        value.convert("RGB").save(buf, "JPEG", quality=95)
        return buf.getvalue()

    if isinstance(value, dict):
        raw = value.get("bytes")
        if raw:
            return bytes(raw)
        path = value.get("path")
        if path and Path(path).is_file():
            return Path(path).read_bytes()
        raise HFError(f"{source} 第 {row_index} 行的图片既没有 bytes 也没有可读的 path")

    if isinstance(value, str) and Path(value).is_file():
        return Path(value).read_bytes()

    raise HFError(f"{source} 第 {row_index} 行的图片类型不认识：{type(value).__name__}")


def list_parquet(repo: str, *, revision: str = "main", token: str | None = None,
                 pattern: str = "*.parquet") -> list[str]:
    return list_shards(repo, revision=revision, pattern=pattern, token=token)


def detect_format(repo: str, *, revision: str = "main", token: str | None = None) -> str:
    """看仓库里是 parquet 还是 tar 分片。两者都要能处理，别让用户猜。"""
    try:
        if list_parquet(repo, revision=revision, token=token):
            return "parquet"
    except HFError:
        raise
    if list_shards(repo, revision=revision, token=token):
        return "webdataset"
    return ""


def cleanup(shard: HFShard, *, keep: bool) -> None:
    if keep or not shard.temporary:
        return
    try:
        shard.path.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ 清单


def read_tar_samples(path: str | Path) -> list[dict]:
    """把一个 tar 里的 member 按 webdataset 约定归组，产出 manifest 行。

    webdataset 的约定是「同一个 stem + 不同后缀属于同一个样本」：
        000123.jpg  000123.json  000123.nl  000123.tag
    所以这里按 `去掉后缀的路径` 分组，图片成员名作为 file。

    返回的行与 `distribute.read_shard_manifest` 同构，下游完全可以复用：
        {"image_id", "file", "artist", "member", "rel_path"}
    """
    from .naming import sanitize

    path = Path(path)
    rows: list[dict] = []
    with tarfile.open(path, "r:*") as tf:
        for info in tf:
            if not info.isfile():
                continue
            name = info.name
            if not name.lower().endswith(IMAGE_EXTS):
                continue
            stem = name.rsplit(".", 1)[0]
            artist = _artist_of(name)
            # image_id 用 tar 内的 stem：它本来就是人类可读的唯一路径。
            # 清洗掉非法字符（Windows 文件名 + 总表里的逗号），但保留层级感。
            image_id = sanitize(stem.replace("/", "-")) or sanitize(Path(name).stem)
            rows.append({
                "image_id": image_id,
                "file": name,               # tar 内成员名，Reader 直接用它取
                "artist": artist,
                "member": name,
                "rel_path": name,
                "bytes": info.size,
            })
    return rows


def _artist_of(member: str) -> str:
    """webdataset 里常见的 `artist/000123.jpg`：第一段目录就是画师。

    取不到就给空串——宁可空着，也不要拿文件名去凑（见 artist.py 的说明）。
    """
    parts = [p for p in member.replace("\\", "/").split("/") if p]
    return parts[0] if len(parts) >= 2 else ""


def merge_manifests(rows: Sequence[dict], *, repo: str, shard: str) -> list[dict]:
    """补上 HF 侧的溯源字段。

    image_path 写成 `hf://datasets/<repo>/<shard>#<member>`：主机拿到总表后
    要靠它回指到仓库里的位置。不用 https 直链，是因为它带 revision，
    一旦仓库改分支历史就会失效；而 hf:// 的语义是"仓库 + 分片 + 成员"，
    只要仓库在就一直能用。
    """
    out: list[dict] = []
    for row in rows:
        r = dict(row)
        r["hf_repo"] = repo
        r["hf_shard"] = shard
        r["image_path"] = f"hf://datasets/{repo}/{shard}#{r['member']}"
        out.append(r)
    return out


def cache_size(path: str | Path) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.glob("*") if f.is_file())


def clear_cache(path: str | Path) -> None:
    p = Path(path)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)


def dumps_rows(rows: Sequence[dict]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"


def iter_repo_tars(repo: str, *, pattern: str, revision: str,
                   token: str | None) -> Iterator[str]:
    yield from list_shards(repo, revision=revision, pattern=pattern, token=token)
