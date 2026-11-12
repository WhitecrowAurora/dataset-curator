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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

import httpx

HF_OFFICIAL = "https://huggingface.co"


def _download_bases() -> list[str]:
    """下载源候选：配置的镜像优先，官方兜底。

    默认走 hf-mirror.com（见 config.DEFAULT_ENDPOINT），国内直连、不吃梯子
    流量。但镜像**不代理登录态**，私有/门禁仓库在那边很可能直接 401——这时
    回退官方，否则表现成"仓库明明存在却一直拒绝访问"，很难查。
    """
    from .config import download_endpoint

    out: list[str] = []
    for base in (download_endpoint(), HF_OFFICIAL):
        base = str(base or "").rstrip("/")
        if base and base not in out:
            out.append(base)
    return out


def api_url(repo: str, *, revision: str = "main", base: str = "") -> str:
    base = (base or _download_bases()[0]).rstrip("/")
    return f"{base}/api/datasets/{repo}/tree/{revision}"

# 分片缓存根目录（相对工作目录）。跨任务共享：下载页提前下好的分片，
# 打标任务要能直接命中，所以不能放在某个任务的 out_dir 里。
HF_CACHE_ROOT = Path("run") / "hf-cache"


def cache_dir_for(repo: str, root: str | Path | None = None) -> Path:
    """某个仓库的分片缓存目录。

    必须按仓库分目录：不同仓库常有同名的 `shard-0000.tar`，平铺在一个目录里
    会互相覆盖，而且覆盖后"文件存在=下载完整"的判定会让它静默用错数据。
    `org/name` 里的斜杠换成 `--`，避免在缓存目录里再套一层。
    """
    base = Path(root) if root else HF_CACHE_ROOT
    return base / (repo.replace("/", "--") or "_")


# 分段下载的门槛与上限。
# 单段小于 8 MB 就不值得多开一条连接：TCP 慢启动还没爬上来就结束了，
# 而每多一条连接就多一次 TLS 握手与一次 HF 的 302 跳转。
SEG_MIN_BYTES = 8 << 20
SEG_MAX_CONNS = 16


def plan_segments(total: int, conns: int) -> list[tuple[int, int]]:
    """把 [0, total) 切成若干闭区间，返回 [(start, end), ...]。

    段大小对齐到 1 MiB：`Range` 边界落在整兆上对 CDN 更友好，续传时记录的
    偏移也是整数，不必保存到字节级的任意值。

    小文件直接单段——国内网络下"4 条连接下 3 MB"比单连接还慢。
    """
    conns = max(1, min(int(conns), SEG_MAX_CONNS))
    if total <= 0:
        return []
    if conns == 1 or total < SEG_MIN_BYTES * 2:
        return [(0, total - 1)]
    seg = max(SEG_MIN_BYTES, -(-total // conns))
    seg = -(-seg // (1 << 20)) * (1 << 20)
    out: list[tuple[int, int]] = []
    start = 0
    while start < total:
        end = min(start + seg, total) - 1
        out.append((start, end))
        start = end + 1
    return out

# webdataset 里同一 stem 的各种成员。图片成员按这些后缀识别。
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")


class HFError(RuntimeError):
    """网络/权限/格式问题。信息里要带"下一步怎么查"，不要只抛状态码。"""


class HFCancelled(Exception):
    """用户主动取消，不是错误。

    故意**不继承 HFError**：下载循环里 `except HFError: raise` 与
    `except (HTTPError, OSError)` 两道都不能吞它，否则取消会被当成
    "可重试的网络错误"，或者半截 `.part` 被删掉（下次就得从头下）。
    """


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
    proxy: str = "",
) -> list[str]:
    """列出仓库里的分片文件名（递归）。

    用 tree 接口而不是 list_repo_files：前者能一次拿到大小，便于按 shard 排错；
    而且不依赖 huggingface_hub（少一个必装依赖）。

    必须跟随 Link: rel="next" 头翻页：单页超过 1000 条时 HF 只回第一页，
    不翻页的话千万级仓库会静默丢掉后面所有分片——数量不对但没有任何报错。
    """
    return sorted(path for path, _ in list_shards_sized(
        repo, revision=revision, pattern=pattern, token=token,
        timeout_s=timeout_s, proxy=proxy))


def list_shards_sized(
    repo: str,
    *,
    revision: str = "main",
    pattern: str = "*.tar",
    token: str | None = None,
    timeout_s: int = 60,
    proxy: str = "",
) -> list[tuple[str, int]]:
    """同 `list_shards`，但带上每个分片的字节数。

    预取要用它算"这一片放不放得下"。`size` 缺失时给 0——那时只能退化成
    "下完再看"，不能拿 0 当"不需要空间"。
    """
    headers = _auth(token)

    def _list_one(base: str) -> list[tuple[str, int]]:
        url = api_url(repo, revision=revision, base=base)
        params: dict | None = {"recursive": "true"}
        got: list[tuple[str, int]] = []
        try:
            with _client(proxy, timeout=timeout_s, follow_redirects=True) as c:
                while url:
                    r = c.get(url, headers=headers, params=params)
                    if r.status_code == 401:
                        # HF 对"不存在"也回 401（不泄露仓库是否存在），所以这条
                        # 不能只写"要 token"：最常见的其实是名字写错或类型选错。
                        raise HFError(
                            f"{repo} 拒绝访问（401）。三种可能，按这个顺序查：\n"
                            f"  1) 仓库名写错——要形如 org/name，不要带 https:// 前缀；\n"
                            f"  2) 类型不对——这里是 **dataset** 仓库，model 仓库要换成对应的接口；\n"
                            f"  3) 确实是私有/门禁仓库——填上读权限的 token"
                            f"（huggingface-cli login，或用有 repo.content.read 的 token）。")
                    if r.status_code == 404:
                        raise HFError(
                            f"{repo} 不存在（404）。确认是 datasets 仓库，且名字拼写对"
                            f"（形如 org/name，不要带 https:// 前缀）。")
                    r.raise_for_status()
                    page = r.json()
                    if not isinstance(page, list):
                        raise HFError(
                            f"{repo} 的 tree 返回结构不认识：{type(page).__name__}")
                    got.extend(
                        (str(e.get("path") or ""), int(e.get("size") or 0))
                        for e in page if e.get("type") == "file")
                    # httpx 已把 Link 头解析成 {rel: {"url": ...}}；next 指向
                    # 同一资源就继续翻，没有 next（或指向别处）就收手。
                    nxt = (r.links.get("next", {}).get("url") if r.links else None)
                    if nxt and nxt.split("?")[0] == url.split("?")[0]:
                        url, params = nxt, None
                    else:
                        url, params = None, None
        except httpx.HTTPError as exc:
            raise HFError(f"列分片失败：{type(exc).__name__}: {exc}") from exc
        return got

    entries: list[tuple[str, int]] = []
    last_err: HFError | None = None
    for base in _download_bases():
        try:
            entries = _list_one(base)
            break
        except HFError as exc:
            last_err = exc
            # 只有"拒绝访问"才换源：名字写错/类型选错，换到官方一样错。
            msg = str(exc)
            if "（401）" not in msg and "403" not in msg:
                raise
    else:
        raise last_err or HFError(f"{repo} 列分片失败")

    glob_re = re.compile(re.escape(pattern).replace(r"\*", ".*") + r"$")
    return sorted(((p, n) for p, n in entries if glob_re.match(p)),
                  key=lambda kv: kv[0])


def resolve_url(repo: str, filename: str, *, revision: str = "main",
                base: str = "") -> str:
    base = (base or _download_bases()[0]).rstrip("/")
    return f"{base}/datasets/{repo}/resolve/{revision}/{filename}"


def resolve_urls(repo: str, filename: str, *, revision: str = "main") -> list[str]:
    """同一个文件的多个下载地址：配置的镜像在前，官方在后。

    下载路径要能换源：镜像不代理登录态，私有仓库在那边会 401；而镜像不通时
    官方又可能被墙。两边都得试，谁通走谁。
    """
    return [resolve_url(repo, filename, revision=revision, base=b)
            for b in _download_bases()]


def _probe_range(
    url: str, headers: dict[str, str], timeout_s: int, proxy: str = "",
    alt: Sequence[str] = (),
) -> tuple[str, int, bool]:
    """探服务端认不认 Range，顺手拿直连地址与总大小。

    用 `Range: bytes=0-0` 而不是 HEAD：HF 的 resolve 会 302 到 CDN，HEAD 在
    某些边缘节点上不回 Content-Length；而 206 的 `Content-Range` 里带着总长，
    一次就够。返回的 `direct` 是重定向后的最终地址——后续每段直接打它，
    省掉每段一次 302。

    `alt` 是备选地址（官方站）。镜像常见两种失效：私有仓库 401、镜像本身
    抽风；两种都换到下一个地址再试，全都不通才报错。
    """
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    last: HFError | None = None
    for candidate in (url, *alt):
        try:
            with _client(proxy, timeout=httpx.Timeout(timeout_s, connect=20.0),
                         follow_redirects=True) as c:
                with c.stream("GET", candidate, headers=h) as r:
                    if r.status_code in (401, 403):
                        last = HFError(
                            f"需要鉴权（HTTP {r.status_code}）。私有仓库要带 HF_TOKEN。")
                        continue
                    r.raise_for_status()
                    direct = str(r.url)
                    if r.status_code == 206:
                        cr = r.headers.get("content-range") or ""
                        m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
                        return direct, int(m.group(1)) if m else 0, True
                    # 服务端忽略了 Range：整片返回，只能走单连接
                    return direct, int(r.headers.get("content-length") or 0), False
        except httpx.HTTPError as exc:
            last = HFError(f"探测下载地址失败：{type(exc).__name__}: {exc}")
            continue
    raise last or HFError("探测下载地址失败：没有可用的下载源")


def _fetch_range(
    url: str,
    headers: dict[str, str],
    path: Path,
    begin: int,
    end: int,
    on_bytes: Callable[[int], None],
    should_stop: "Callable[[], bool] | None",
    timeout_s: int,
    proxy: str = "",
) -> None:
    """把远端 [begin, end] 写进 path 的同一偏移处。

    每个线程用**自己的文件句柄** seek 后写：Windows 没有 `os.pwrite`，但
    多个句柄各写各的区间是允许的——前提是文件长度已经预分配到位。
    """
    h = dict(headers)
    h["Range"] = f"bytes={begin}-{end}"
    try:
        with _client(proxy, timeout=httpx.Timeout(timeout_s, connect=20.0),
                     follow_redirects=True) as c:
            with c.stream("GET", url, headers=h) as r:
                if r.status_code == 416:
                    raise HFError(
                        f"服务端拒绝区间 {begin}-{end}（416）：远端文件可能换了")
                if r.status_code != 206:
                    raise HFError(f"分段请求期望 206，收到 {r.status_code}")
                with path.open("r+b") as f:
                    f.seek(begin)
                    for chunk in r.iter_bytes(1 << 20):
                        if should_stop is not None and should_stop():
                            f.flush()
                            raise HFCancelled("已取消")
                        f.write(chunk)
                        on_bytes(len(chunk))
    except httpx.HTTPError as exc:
        raise HFError(f"分段下载中断：{type(exc).__name__}: {exc}") from exc


def _write_segment_state(path: Path, total: int,
                         plan: Sequence[tuple[int, int]],
                         done: Sequence[int]) -> None:
    """段进度落盘。写不下不算错：最坏只是下次从头下这一段。"""
    try:
        path.write_text(json.dumps({
            "total": total,
            "segments": [[a, b] for a, b in plan],
            "done": list(done),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _download_segmented(
    *,
    repo: str,
    filename: str,
    url: str,
    part: Path,
    final: Path,
    token: str | None,
    timeout_s: int,
    max_attempts: int,
    conns: int,
    proxy: str,
    verbose: bool,
    on_progress: "Callable[[int, int], None] | None",
    should_stop: "Callable[[], bool] | None",
    alt: Sequence[str] = (),
) -> HFShard | None:
    """多连接分段下载。服务端不支持 Range 时返回 None，由调用方退回单连接。

    为什么要"每段一个临时区段"而不是"每段一个临时文件"：后者在拼接时要
    再占一份完整空间（峰值 2 倍），分段越多越容易把盘撑爆。这里预分配一个
    稀疏文件、各段 seek 直写，峰值就是 1 倍。

    代价是 `.part` 的长度一开始就等于总大小——所以**不能**再用"文件存在/
    长度等于 total"判断完成。完成的判据只有一个：状态文件里所有段都写满
    （`done[i] == 段长`），随后 rename。
    """
    state_path = part.with_name(part.name + ".json")
    headers = _auth(token)
    direct, total, ranged = _probe_range(url, headers, timeout_s, proxy, alt)
    if not ranged or total <= 0:
        return None

    plan: list[tuple[int, int]] | None = None
    done: list[int] | None = None
    if state_path.is_file():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = None
        if isinstance(prev, dict) and int(prev.get("total") or 0) == total:
            segs, marks = prev.get("segments"), prev.get("done")
            if (isinstance(segs, list) and isinstance(marks, list)
                    and len(segs) == len(marks) and segs):
                try:
                    plan = [(int(a), int(b)) for a, b in segs]
                    done = [max(0, int(x)) for x in marks]
                except (TypeError, ValueError):
                    plan = None
    if plan is None or done is None:
        # 沿用旧的分段计划（哪怕这次连接数变了）：`.part` 已经被预分配成
        # 总长，重新分段的偏移和已有内容对不上，是最容易"看起来成功、文件是花的"。
        plan = plan_segments(total, conns)
        done = [0] * len(plan)
        with part.open("wb") as f:
            f.truncate(total)
        _write_segment_state(state_path, total, plan, done)

    lock = threading.Lock()
    abort = threading.Event()
    progress = {"done": sum(done)}
    last_save = [time.time()]

    def bump(idx: int, n: int) -> None:
        snapshot = None
        with lock:
            done[idx] += n
            progress["done"] += n
            cur = progress["done"]
            now = time.time()
            # 每 2 秒落一次进度：崩溃最坏丢 2 秒的下载量，
            # 而每次 chunk 都写盘会让下载线程卡在 fsync 上。
            if now - last_save[0] > 2.0:
                last_save[0] = now
                snapshot = (list(plan), list(done))
        if on_progress is not None:
            on_progress(cur, total)
        if snapshot is not None:
            _write_segment_state(state_path, total, *snapshot)

    def stopped() -> bool:
        if abort.is_set():
            return True
        return should_stop is not None and should_stop()

    def worker(idx: int) -> None:
        start, end = plan[idx]
        span = end - start + 1
        attempt = 0
        while done[idx] < span:
            if stopped():
                raise HFCancelled(f"{Path(filename).name} 已取消")
            attempt += 1
            try:
                _fetch_range(direct, headers, part, start + done[idx], end,
                             lambda n: bump(idx, n), stopped, timeout_s, proxy)
                return
            except (HFCancelled, HFError):
                raise
            except Exception as exc:      # noqa: BLE001
                if attempt >= max_attempts:
                    raise HFError(
                        f"{Path(filename).name} 第 {idx + 1} 段失败"
                        f"（已重试 {max_attempts} 次）：{exc}") from exc
                time.sleep(min(2.0 * attempt, 10.0))

    t0 = time.time()
    pool = ThreadPoolExecutor(max_workers=len(plan),
                              thread_name_prefix="hf-seg")
    futures = [pool.submit(worker, i) for i in range(len(plan))]
    try:
        for fut in futures:
            fut.result()
    except BaseException:
        # 一段失败/被取消：让其余段尽快收手，别继续吃带宽
        abort.set()
        for fut in futures:
            fut.cancel()
        raise
    finally:
        pool.shutdown(wait=True)
        # 无论成败都落一次：取消后重启能接着下，而不是从头开始
        with lock:
            snap = (list(plan), list(done))
        _write_segment_state(state_path, total, *snap)

    if sum(done) != total:
        raise HFError(
            f"{Path(filename).name} 分段下载未完成：{sum(done)}/{total} 字节。"
            f"半截文件与进度已保留，再跑一次会续传。")
    try:
        state_path.unlink()
    except OSError:
        pass
    part.replace(final)
    if verbose:
        mb = final.stat().st_size / 1048576
        print(f"[hf] {final.name}  {mb:.1f} MB  {time.time() - t0:.1f}s "
              f"（{len(plan)} 段并发）", flush=True)
    return HFShard(repo=repo, filename=filename, path=final,
                   bytes=final.stat().st_size)


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
    on_progress: "Callable[[int, int], None] | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
    conns: int = 4,
    proxy: str = "",
    url: str | None = None,
) -> HFShard:
    """下载一个分片到 dest_dir，原子落盘。

    先写 `.part` 再 rename：中途断网留下半截文件的话，tarfile 会读到一半
    才报错（甚至读到"看起来完整"的截断包），而原子 rename 让"文件存在"
    等于"下载完整"。

    `.part` 存在时走 HTTP Range 续传（服务端不支持就退回整片重下）。
    下载页把几个 GB 的包下到一半被叫停、再点一次，靠的就是这条。

    `on_progress(已下字节, 总字节)` 每块回调一次；总字节未知时为 0。
    `should_stop()` 为真则抛 `HFCancelled`，**保留 .part** 以便续传。

    `conns > 1` 且服务端支持 Range 时走多连接分段（`_download_segmented`）：
    国内单条 TCP 常被压到几百 KB/s，凭这一点开 4~8 条才是真的快。文件太小
    （< 2×8 MB）或服务端忽略 Range 时自动退回单连接。

    `url` 给了就用它，而不是按数据集拼直链：模型的下载页（`web/downloads.py`）
    走的是 `/models/.../resolve/`，跟这里的 `/datasets/...` 不是同一条路径。
    复用这一份实现的是"分段 + 预分配 + 段级续传 + 重试"，URL 只是入参。
    """
    from .storage.locking import FileLock
    # 同一缓存分片的 .part / 分段状态只能由一个下载者改写。
    cache_lock = FileLock(Path(dest_dir) / (Path(filename).name + ".download.lock"))
    while not cache_lock.acquire(blocking=False):
        if should_stop is not None and should_stop():
            raise HFCancelled(f"{filename} 已取消")
        time.sleep(0.1)
    try:
        dest_dir = Path(dest_dir)
        rel = Path(filename)
        # 仓库里的路径要原样保留层级：parquet 常有 data/part-0.parquet 这种
        # 带目录的名字，压平就会让两批数据互相覆盖。
        if rel.is_absolute() or ".." in rel.parts:
            raise HFError(f"分片名不安全，拒绝落盘：{filename}")
        final = dest_dir / rel
        final.parent.mkdir(parents=True, exist_ok=True)

        if final.is_file() and final.stat().st_size > 0:
            size = final.stat().st_size
            if on_progress is not None:
                on_progress(size, size)
            return HFShard(repo=repo, filename=filename, path=final,
                           bytes=size, temporary=False)

        # 地址候选：配置的镜像在前、官方在后。分段下载会把 alt 一起带进探测，
        # 镜像 401（私有仓库）或抽风时自动换到官方。
        if url:
            alts: list[str] = []
        else:
            _cands = resolve_urls(repo, filename, revision=revision)
            url, alts = _cands[0], _cands[1:]
        part = final.with_name(final.name + ".part")
        state_path = part.with_name(part.name + ".json")

        conns = max(1, min(int(conns), SEG_MAX_CONNS))
        # 有分段状态就必须走分段：`.part` 已被预分配成总长，单连接会把它当成
        # "已经下完了"（offset == total），请求出来是 416 或干脆写花。
        if conns > 1 or state_path.is_file():
            seg = _download_segmented(
                repo=repo, filename=filename, url=url, part=part, final=final,
                token=token, timeout_s=timeout_s, max_attempts=max_attempts,
                conns=conns, proxy=proxy, verbose=verbose, on_progress=on_progress,
                should_stop=should_stop, alt=alts)
            if seg is not None:
                return seg
            # 服务端不认 Range：清掉预分配的半成品，退回单连接从头下
            for path in (state_path, part):
                try:
                    path.unlink()
                except OSError:
                    pass

        last_err: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.time()
                offset = part.stat().st_size if part.is_file() else 0
                headers = _auth(token)
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                with _client(proxy, timeout=httpx.Timeout(timeout_s, connect=20.0),
                             follow_redirects=True) as c:
                    with c.stream("GET", url, headers=headers) as r:
                        if r.status_code in (401, 403):
                            raise HFError(
                                f"{repo}/{filename} 需要鉴权（HTTP {r.status_code}）。"
                                f"私有仓库要带 HF_TOKEN。")
                        r.raise_for_status()
                        if offset and r.status_code == 206:
                            written = offset
                            total = offset + int(r.headers.get("content-length") or 0)
                            mode = "ab"
                        else:
                            # 服务端不认 Range（有的镜像会整片返回）：从头写
                            written = 0
                            total = int(r.headers.get("content-length") or 0)
                            mode = "wb"
                        if on_progress is not None:
                            on_progress(written, total)
                        with part.open(mode) as f:
                            for chunk in r.iter_bytes(1 << 20):
                                if should_stop is not None and should_stop():
                                    f.flush()
                                    raise HFCancelled(f"{filename} 已取消")
                                f.write(chunk)
                                written += len(chunk)
                                if on_progress is not None:
                                    on_progress(written, total)
                        f_size = part.stat().st_size
                if total and f_size != total:
                    raise HFError(f"下载不完整：{f_size}/{total} 字节")
                part.replace(final)
                if verbose:
                    mb = final.stat().st_size / 1048576
                    print(f"[hf] {rel.name}  {mb:.1f} MB  {time.time() - t0:.1f}s",
                          flush=True)
                return HFShard(repo=repo, filename=filename, path=final,
                               bytes=final.stat().st_size)
            except HFCancelled:
                # 不删 .part：下次点"继续下载"从断点接上
                raise
            except HFError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                last_err = exc
                if attempt >= max_attempts:
                    break
                wait = min(5.0 * attempt, 30.0)
                print(f"[hf][警告] {rel.name} 第 {attempt} 次失败"
                      f"（{type(exc).__name__}），{wait:.0f}s 后重试", flush=True)
                time.sleep(wait)

        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        raise HFError(
            f"{rel.name} 下载失败（已重试 {max_attempts} 次）：{last_err}。"
            f"检查网络/磁盘空间，或先用浏览器下一份放到 --hf-cache 目录下再跑。")

    finally:
        cache_lock.close()


def _auth(token: str | None) -> dict[str, str]:
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _client(proxy: str = "", **kwargs):
    """带可选代理的 httpx 客户端。

    给了代理就 `trust_env=False`：显式设置应当压过环境变量，否则
    "页面上填了 A、实际走的是环境变量里的 B"是最难自查的一类问题。
    没给代理就保持 httpx 默认（信任 HTTP_PROXY/HTTPS_PROXY）——很多机器
    本来就靠环境变量出网，这里不该替用户改主意。
    """
    text = (proxy or "").strip()
    if text:
        kwargs["proxy"] = text
        kwargs["trust_env"] = False
    try:
        return httpx.Client(**kwargs)
    except ImportError as exc:      # socks5 但没装 socksio
        raise HFError("socks5 代理需要 socksio，请先 `pip install socksio`") from exc


# ------------------------------------------------------------------ parquet


# 画师列的常见叫法。HF 上 danbooru 派生的数据集没有统一 schema：
# 官方版叫 `artist`，各家魔改版常写 `author` / `creator` / `username` /
# `uploader`（名字来自它们自己的上传后台），列表形态则叫 `artists`。
#
# ★这些必须**兜底**而不是写死一个。写死时的失败是彻底静默的：取不到画师
#   → 产物 `artist` 字段空 → 用户看到"开关全开、画师表也填了，产物里却
#   什么都没有"，而日志一个字都不提。和 `id_columns`、`_ref_tags_of` 是
#   同一类问题，做法也照它们：按顺序找第一个有内容的。
ARTIST_COLUMNS: tuple[str, ...] = (
    "artist", "artists", "author", "creator", "username", "uploader",
)


def _row_artist(row: dict, columns: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """(单值, 全部画师)。都对不上就是 `("", ())`，表示这一行没有画师信息。

    列表形态（`artists`）保留全部：caption 里按设计只注入第一个（连写几个
    `@` 会把绑定稀释掉），但产物 `artists` 字段要用来追溯，这里砍掉就找不
    回来了。

    空值形态不止 `None` / `""`：pandas 空单元格读出来是 `nan`，`str(nan)`
    是字符串 "nan"，直接当名字用会让产物里出现一个叫 nan 的画师。
    """
    for col in columns:
        val = row.get(col)
        if val is None:
            continue
        if isinstance(val, (list, tuple)):
            names = tuple(str(x).strip() for x in val
                          if x is not None and str(x).strip()
                          and str(x).strip().lower() not in ("nan", "none", "null"))
            if names:
                return names[0], names
            continue
        text = str(val).strip()
        if text and text.lower() not in ("nan", "none", "null"):
            return text, ()
    return "", ()


def iter_parquet_samples(
    source: str,
    *,
    image_column: str = "image",
    token: str | None = None,
    repo: str = "",
    shard: str = "",
    path_template: str = "{image_id}.jpg",
    id_columns: Sequence[str] = ("image_id", "id", "file_name", "path", "name", "key"),
    artist_column: str = "",
    artist_columns: Sequence[str] = ARTIST_COLUMNS,
    skip: int = 0,
) -> Iterator[dict]:
    """流式读一个 parquet 分片，逐行产出图片字节。

    `source` 可以是本地路径、https 直链，或 `hf://datasets/<repo>/<file>`。
    用 datasets 的 streaming：它按 row group 拉数据，不会把整个分片落盘，
    所以成员机器只需要一点缓存空间——这正是"SSF 太贵"要的效果。

    和 sucai 那边是同一套机制（`load_dataset(..., streaming=True)`），
    区别只在它是给训练喂样本，这里是把样本变成打标队列。

    画师列走 `artist_columns` 兜底（见 `ARTIST_COLUMNS`）：第三方的
    danbooru 派生数据集列名不统一，写死一个 `artist` 会让画师静默丢失。
    要精确指定就用 `artist_column`，它排在候选表最前面。
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
        # 显式点名的那一列排最前：调用方说"就用 author"时不该被默认顺序盖过。
        cols = ((artist_column,) + tuple(artist_columns)) if artist_column else artist_columns
        artist, artists = _row_artist(row, cols)
        out = {
            "image_id": image_id,
            "artist": artist,
            "data": raw,
            "image_path": path_template.format(image_id=image_id, index=idx,
                                               shard=stem, repo=repo),
            "row_index": idx,
        }
        # 只有真的是多画师才带这个键：单画师时留空，下游 `_hf_task` 的
        # `artists=()` 与"没有这一列"是同一种语义，不必多一个空元组。
        if len(artists) > 1:
            out["artists"] = list(artists)
        yield out


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
                 pattern: str = "*.parquet", proxy: str = "") -> list[str]:
    return list_shards(repo, revision=revision, pattern=pattern, token=token,
                       proxy=proxy)


def detect_format(repo: str, *, revision: str = "main", token: str | None = None,
                  proxy: str = "") -> str:
    """看仓库里是 parquet 还是 tar 分片。两者都要能处理，别让用户猜。"""
    try:
        if list_parquet(repo, revision=revision, token=token, proxy=proxy):
            return "parquet"
    except HFError:
        raise
    if list_shards(repo, revision=revision, token=token, proxy=proxy):
        return "webdataset"
    return ""


def cleanup(shard: HFShard, *, keep: bool) -> None:
    if keep or not shard.temporary:
        return
    try:
        shard.path.unlink()
    except OSError:
        pass


def free_bytes(path: str | Path) -> int:
    """目标目录所在磁盘的剩余字节。目录还不存在就往上找存在的父级。

    不能直接 `os.statvfs(".")`：调用方给的可能是还没建的临时目录，
    那样量到的是工作目录的盘，结果"还剩 500 GB"却写不进去。
    """
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return int(shutil.disk_usage(str(p)).free)
    except OSError:
        return 0


def cache_session(cache_dir: Path, should_stop=None):
    """共享缓存从下载到清理期间保持独占，等待也响应停止。"""
    from contextlib import contextmanager
    from .storage.locking import FileLock

    @contextmanager
    def session():
        lock = FileLock(Path(cache_dir) / ".processing.lock")
        while not lock.acquire(blocking=False):
            if should_stop is not None and should_stop():
                raise HFCancelled("等待缓存使用权时已取消")
            time.sleep(0.1)
        try:
            yield
        finally:
            lock.close()
    return session()


def iter_hf_shards(
    names: Sequence[str],
    *,
    repo: str,
    cache_dir: str | Path,
    revision: str = "main",
    token: str | None = None,
    prefetch: int = 1,
    reserve_bytes: int = 0,
    sizes: Mapping[str, int] | None = None,
    conns: int = 4,
    proxy: str = "",
    keep: bool = False,
    verbose: bool = True,
    on_log=None,
    should_stop=None,
) -> Iterator[HFShard]:
    """按需下载分片：预取 N 片，调用方处理完（并删掉）再要下一片。

    为什么值得预取：一个 tar 是 0.5~2 GB，串行跑的话"下载 30 s、GPU 空转
    30 s"，等于白丢 30~40 张的产能。预取 1 片就能把下载藏进上一片的处理时间里，
    再多就没有收益了（带宽是瓶颈，不是并发数）。

    **空间是硬约束**：每次提交下载前都重新量一次 `cache_dir` 所在盘，
    `剩 < 这一片 + reserve` 就不提交（而不是下到一半写失败）。调用方
    处理完上一片会立刻删掉它，所以下一条 `submit_next` 又能拿到空间——
    这正是"打完一个删一个、按剩余空间再下一个"的语义。

    预取下来但调用方没用上（--limit 截断 / 抛异常）的分片，在 finally 里删掉。
    """
    # 共享缓存会在消费后删除；持锁覆盖下载、读取、清理整个生命周期。
    with cache_session(Path(cache_dir), should_stop):
        from collections import deque
        from concurrent.futures import Future, ThreadPoolExecutor

        def log(msg: str) -> None:
            if on_log is not None:
                on_log(msg)
            else:
                print(msg, flush=True)

        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        abort = threading.Event()
        def cancelled():
            return abort.is_set() or (should_stop is not None and should_stop())
        sizes = sizes or {}
        pending = list(names)
        # 窗口 = 正在处理的 1 片 + 预取的 N 片
        window = 1 + max(0, int(prefetch))
        queue: deque[tuple[str, Future]] = deque()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hf-dl")

        def submit_next() -> bool:
            if cancelled():
                raise HFCancelled("分片处理已取消")
            if not pending or len(queue) >= window:
                return False
            name = pending[0]
            est = int(sizes.get(name) or 0)
            local = cache_dir / Path(name)
            # 上次 --hf-keep 留下的同名片不再占"新增"空间
            if est and not local.is_file():
                # 在下载途中的分片还没落成文件，free_bytes 看不到它；不扣掉就会
                # "同时预取 3 片、每片都通过检查"，然后一起把盘写满。
                inflight = sum(int(sizes.get(n) or 0)
                               for n, f in queue if not f.done())
                free = free_bytes(cache_dir) - inflight
                if free < est + reserve_bytes:
                    log(f"[hf] 空间不够，暂停下载：剩 {free / 1073741824:.1f} GB，"
                        f"{local.name} 需 {est / 1073741824:.1f} GB + 余量 "
                        f"{reserve_bytes / 1073741824:.1f} GB")
                    return False
            pending.pop(0)
            queue.append((name, pool.submit(
                download_shard, repo, name, cache_dir, revision=revision,
                token=token, verbose=verbose, conns=conns, proxy=proxy, should_stop=cancelled)))
            return True

        try:
            while submit_next():
                pass
            while queue or pending:
                if not queue:
                    # 上一片刚被删、空间又够：先补提交，别当成"空间不足"报错
                    if submit_next():
                        continue
                    name = pending[0]
                    est = int(sizes.get(name) or 0)
                    free = free_bytes(cache_dir)
                    raise HFError(
                        f"磁盘空间不足，下载不了 {name}：剩 {free / 1073741824:.1f} GB，"
                        f"需要 {est / 1073741824:.1f} GB + 余量 "
                        f"{reserve_bytes / 1073741824:.1f} GB。\n"
                        f"清一下 {cache_dir}，或把临时目录换到更大的盘"
                        f"（--hf-cache），或关掉预取（--hf-prefetch 0）再跑。")
                name, fut = queue.popleft()
                shard = fut.result()          # 下载失败在这里抛 HFError
                # 落在这个缓存目录里的分片一律当临时文件：上一次提前中断可能留下
                # 一个完整的 tar，download_shard 会以 temporary=False 复用它，
                # 那样 cleanup 就不删了，缓存目录会越积越大（每片 0.5~2 GB）。
                shard = replace(shard, temporary=True)
                submit_next()                 # 当前片处理期间，下一片已经在下载
                yield shard
        finally:
            abort.set()
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except TypeError:                # Python < 3.9
                pool.shutdown(wait=True)
            # 已下载完但没被消费的分片：不删就是"预取把盘吃满"的来源
            for _, fut in queue:
                if not fut.done():
                    continue
                try:
                    cleanup(fut.result(), keep=keep)
                except Exception:            # noqa: BLE001 - 下载失败的分片本来就没落盘
                    continue
            queue.clear()


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
