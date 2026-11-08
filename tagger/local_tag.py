"""本地打标：一个图片目录 → 每张一份 tag 产物。整条链路不碰 vLLM。

为什么单独一层
--------------
`client.py` 那条路是**分发包 + VLM 服务**的形态：进来必须是 `split` 产出的
zip（里面才有 manifest），出去的 tag 是"VLM 出 NL、CPU 顺带出 tag"的副产物。
可本仓库最常见的用法恰恰相反——**手上只有一堆图，就想知道每张是什么 tag**，
没有 vLLM，也不需要 caption。硬套 client 会要求先 split 再连服务，两头都
是白费（client 里那句"`--mode tag` 不能配 CPU tag 后端"就是这条缝）。

所以这里只做一件事：扫目录 → 按批推理 → 落 `<image_id>-tag.json`。
产物字段与 `client._tag_payload` 对齐（`image_id` / `tag` / `tags` /
`tag_backend` / `rating` / `provenance`），这样 merge、report、verify
那一套将来仍能直接吃这批产物。

三个不显然的口径
----------------
1. **image_id 用相对路径去掉扩展名**（`/` 分隔）。用文件名会撞号（子目录里
   两个 `001.png` 很常见），用绝对路径会绑死机器。相对路径既可复现又可读。
2. **跳过判定看 `ok: True`，不看 `tag` 是否非空。** client 那边要求"字段
   非空"，因为 VLM 吐不出 tag 就是失败的信号；这里不同——阈值 0.35 下一张
   纯色图**合理地**一个 tag 都没有，那是一份有效结果，不该每次重跑都重算。
3. **整块推理失败要逐张重试一次。** 一张坏图会让 `predict_batch` 整块抛，
   直接记全块失败等于一张坏图毁掉 8 张。逐张重试能把账算到真正的那张头上。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# 与 client / distribute 认的扩展名保持一致；多一种就多一处漏判。
EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def config_fingerprint(backend) -> str:
    """记录影响 tag 输出的配置，供追溯；默认续做仍按图片跳过。"""
    raw = json.dumps({
        "backend": str(getattr(backend, "name", "") or ""),
        "model_id": str(getattr(backend, "model_id", "") or ""),
        "precision": str(getattr(backend, "precision", "") or ""),
        # 重采样实现影响 tag，需记录实际使用的口径。
        "preprocess": str(getattr(backend, "preprocess", "") or ""),
        "thresholds": sorted((str(k), float(v))
                             for k, v in (getattr(backend, "thresholds", None) or {}).items()),
        "keep": list(getattr(backend, "keep", None) or ()),
        "max_tags": int(getattr(backend, "max_tags", 0) or 0),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class TagItem:
    """一张待打标的图。image_id 同时是产物文件名。

    除了散图，也承载 zip 内的成员（source + member）。打标后端只认
    "路径或字节"，所以读取统一走 read_bytes()/外部 reader；不要在调用方
    假定 item.path 一定存在——zip 成员的 path 是包文件本身。
    """

    image_id: str
    path: Path
    rel: str
    # zip 成员：source 是包路径，member 是包内成员名。散图两者都为空/自身。
    source: str = ""
    member: str = ""
    # 归档里要用的名字（flat 布局下是 画师-000001.jpg；mirror 下是原路径）。
    # 为空表示沿用 rel / 平铺名，由封包阶段补。
    out_name: str = ""
    # 路径里取到的画师名（原始拼写）。写进 meta，也用于命名。
    artist: str = ""
    # 原始文件字节数。封包前"盘够不够写"的估算全靠它：不需要读图就能拿到，
    # 比"转到一半才发现写不下"便宜得多。
    size: int = 0

    @property
    def name(self) -> str:
        if self.member:
            return self.member.replace("\\", "/").rsplit("/", 1)[-1]
        return self.path.name

    @property
    def in_zip(self) -> bool:
        return bool(self.member)

    def read_bytes(self) -> bytes:
        """读原始字节。zip 成员每次重开包，适合小批量/CLI。

        大批量请传 TagItem 池化 reader 给 tag_items（见 pipeline.MemberReader）：
        每读一个成员都解析一次 zip 中央目录，几十万成员时是 O(n²)。
        """
        if self.member:
            with zipfile.ZipFile(self.source, "r") as zf:
                return zf.read(self.member)
        return self.path.read_bytes()


@dataclass(slots=True)
class TagStats:
    total: int = 0
    done: int = 0
    skipped: int = 0
    busy: int = 0
    failed: int = 0
    stopped: bool = False
    wall_ms: int = 0
    # 后端报上来的累计耗时（毫秒）。只有 wd-eva02 这类会拆的才有值，
    # 其余后端留 0——宁可空着，也不编一个"解码占 40%"出来。
    decode_ms: int = 0
    forward_ms: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def finished(self) -> int:
        return self.done + self.failed

    def summary_lines(self) -> list[str]:
        lines = [
            f"总共       : {self.total}",
            f"完成       : {self.done}",
            f"跳过（已有）: {self.skipped}",
            f"失败       : {self.failed}",
        ]
        if self.stopped:
            lines.append("状态       : 已中止（剩余未处理）")
        if self.finished and self.wall_ms:
            lines.append(f"平均耗时   : {self.wall_ms / self.finished / 1000:.3f}s/张")
        for reason, n in sorted(self.by_reason.items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"  {reason:<28} {n}")
        return lines


# 进度回调：kind ∈ {"skipped", "done", "failed"}；第三位是产物 dict（done 时）
# 或错误原因字符串（failed 时），另外两个是 None。
TagHook = Callable[[str, TagItem, object, TagStats], None]
# 中止信号：返回 True 就在下一个块边界停下（不是硬杀，正在跑的块会跑完）。
StopFlag = Callable[[], bool]


# ------------------------------------------------------------------ 扫描

def scan_images(root: str | Path, *, recursive: bool = True,
                limit: int = 0) -> list[TagItem]:
    """把一个目录扫成任务列表。顺序按相对路径排序，保证两次跑结果一致。"""
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"不是目录：{base}")
    it = base.rglob("*") if recursive else base.glob("*")
    files = [p for p in it if p.is_file() and p.suffix.lower() in EXTS]
    files.sort(key=lambda p: str(p.relative_to(base)).replace("\\", "/").lower())
    if limit > 0:
        files = files[:limit]
    items: list[TagItem] = []
    for p in files:
        rel = str(p.relative_to(base)).replace("\\", "/")
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        items.append(TagItem(image_id=rel[: -len(p.suffix)] if p.suffix else rel,
                             path=p, rel=rel, source=str(p), size=size))
    return items


def artifact_path(out_dir: str | Path, item: TagItem) -> Path:
    """产物落点：`<out_dir>/tags/<image_id>-tag.json`，子目录按原层级铺开。"""
    return Path(out_dir) / "tags" / f"{item.image_id}-tag.json"


def read_artifact(out_dir: str | Path, item: TagItem) -> dict | None:
    """读回一份产物。读不出来（半截 / 不是 dict）就当没有。"""
    path = artifact_path(out_dir, item)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _done_ok(out_dir: str | Path, item: TagItem, fingerprint: str) -> bool:
    """有效产物默认复用；配置变化只有在强制重做时才重新计算。"""
    obj = read_artifact(out_dir, item)
    if not obj or obj.get("ok") is not True:
        return False
    return isinstance(obj.get("tag"), str)


# ------------------------------------------------------------------ 产物

def _payload(item: TagItem, pred, backend, *, precision: str,
             fingerprint: str = "") -> dict:
    names = [t for t, _ in pred.tags]
    payload = {
        "image_id": item.image_id,
        # merge/report 认的是 image_path，本地这条就是相对路径
        "image_path": item.rel,
        # merge 读的就是这个字段，必须是逗号分隔的字符串
        "tag": ", ".join(names),
        "tags": [[t, round(float(p), 4)] for t, p in pred.tags],
        "tag_backend": getattr(backend, "name", "") or pred.backend or "unknown",
        "ok": True,
        "provenance": {
            "mode": "local-tag",
            "tag_backend": getattr(backend, "name", "") or "unknown",
            # 精度必须落盘：fp32/tf32/bf16 出的 tag 集合不一样，事后想回答
            # "这批为什么多出几个 tag"，没有这个字段只能靠猜。
            "tag_precision": precision,
            # 预处理口径：pil / tensor 的重采样不是同一套核，tag 集合会有差异，
            # 事后混批时要能分辨是"口径变了"还是"模型变了"。
            "tag_preprocess": str(getattr(backend, "preprocess", "pil") or "pil"),
            "batch": int(getattr(backend, "batch_size", 1) or 1),
        },
        "ts": time.time(),
    }
    # 配置指纹保留用于追溯，不改变默认跳过已有图片的行为。
    if fingerprint:
        payload["config_fingerprint"] = fingerprint
    # 分级分数单列，不并进 tag 串（描述审核等级，不是画面内容）。
    rating = getattr(pred, "rating", None) or {}
    if rating:
        payload["rating"] = {k: round(float(v), 4) for k, v in rating.items()}
    return payload


def _write_json(path: Path, payload: dict) -> None:
    """tmp + fsync + replace：半截 json 会被当成"已完成"，这是最贵的错。"""
    from .storage.atomic import write_json
    write_json(path, payload)


# ------------------------------------------------------------------ 执行

def _chunk_size(backend) -> int:
    """一次交给后端的图片数。

    必须**大于**后端的 batch_size，否则 `_predict_many` 只切出一块，
    解码预取（前向时解下一批）就被这条边界关掉了——实测那是 1.75× 的差距，
    而且它不会报错，只是慢，属于最难发现的那种退化。
    """
    return max(1, int(getattr(backend, "batch_size", 1) or 1)) * 8


def _reason_of(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:120]


def tag_items(backend, items: Sequence[TagItem], *, out_dir: str | Path,
              force: bool = False, on_progress: TagHook | None = None,
              should_stop: StopFlag | None = None,
              stats: TagStats | None = None,
              read_bytes: Callable[[TagItem], bytes] | None = None,
              guard=None) -> TagStats:
    from .storage.work import WorkIndex
    with WorkIndex(Path(out_dir)) as work:
        work.refresh()
        return _tag_items(backend, items, out_dir=out_dir, force=force,
                          on_progress=on_progress, should_stop=should_stop, stats=stats,
                          read_bytes=read_bytes, guard=guard, work=work)


def _tag_items(backend, items, *, out_dir, force, on_progress, should_stop,
               stats, read_bytes, guard, work):
    """按批打标并落盘。返回结账过的 TagStats。

    `should_stop` 是**协作式**中止：只在块边界检查，正在算的那一块会算完
    （一张 448² 前向几十毫秒，硬杀反而留下写了一半的状态）。

    `stats` 可以传一个进来**原地累加**（Web 层就这么用：它要一边跑一边让
    另一个线程读计数）。不传就自己新建一个。

    `read_bytes` 只在输入不是散图路径时有用（zip 成员）：给了就按它取字节，
    后端直接吃 bytes；不给就走路径。池化 reader 能把 zip 中央目录的解析
    从"每个成员一次"降到"每个包一次"。

    `guard` 是可选的 ResourceGuard：块开始前问一次"让不让跑"（温度/磁盘
    超限就等，等待中随时响应 should_stop），块完成后结算一次占空比让步。
    单元粒度 = 一个块（batch×8 张），与 guard 的"任务边界插空闲"语义对齐。
    """
    out_dir = Path(out_dir)
    stats = stats if stats is not None else TagStats()
    stats.total = len(items)
    precision = str(getattr(backend, "precision", "") or "")
    fingerprint = config_fingerprint(backend)
    started = time.perf_counter()

    read_bytes = read_bytes or (lambda item: item.read_bytes())

    def wait_for_guard() -> bool:
        """阻塞到 guard 放行。返回 False = 不该继续（stop 级别或收到停止）。

        不能直接 before_unit(wait=True)：那个等待不响应 should_stop，
        用户点"停止"时页面会卡在"暂停中"直到温度自己降下来。
        """
        st = guard.before_unit(wait=False)
        while not st.can_run and st.level != "stop":
            if should_stop is not None and should_stop():
                return False
            time.sleep(1.0)
            st = guard.watchdog.state
        return st.can_run

    def inputs_of(chunk: Sequence[TagItem]) -> list:
        # 散图优先传路径：后端自己解码时不再多一次"读进内存再包 BytesIO"。
        # zip 成员没有独立路径，只能给字节。
        return [read_bytes(it) if it.in_zip else str(it.path) for it in chunk]

    def emit(kind: str, item: TagItem, extra: object) -> None:
        if on_progress is None:
            return
        try:
            on_progress(kind, item, extra, stats)
        except Exception:        # noqa: BLE001 - 订阅方（UI）的 bug 不该打断整批
            pass

    def fail(item: TagItem, reason: str) -> None:
        work.release(item.image_id)
        stats.failed += 1
        key = reason.split(":")[0].strip()[:60]
        stats.by_reason[key] = stats.by_reason.get(key, 0) + 1
        emit("failed", item, reason)

    def pending_items():
        for item in items:
            if not force and _done_ok(out_dir, item, fingerprint):
                stats.skipped += 1
                emit("skipped", item, None)
                continue
            status = work.claim(item.image_id, force=force)
            if status != "claimed":
                if status == "done":
                    stats.skipped += 1
                    emit("skipped", item, None)
                else:
                    stats.busy += 1
                    emit("busy", item, None)
                continue
            if not force and _done_ok(out_dir, item, fingerprint):
                work.release(item.image_id)
                stats.skipped += 1
                emit("skipped", item, None)
                continue
            yield item

    def commit(item: TagItem, pred) -> None:
        if not work.owns(item.image_id):
            raise RuntimeError(f"图片领取已失效：{item.image_id}")
        payload = _payload(item, pred, backend, precision=precision,
                           fingerprint=fingerprint)
        try:
            _write_json(artifact_path(out_dir, item), payload)
        except OSError as exc:
            fail(item, f"写盘失败: {exc}")
            return
        stats.done += 1
        work.release(item.image_id)
        emit("done", item, payload)

    def account_timings() -> None:
        """把后端报的上一次拆分耗时累进来。没有拆分的后端两个都是 0。"""
        split = getattr(backend, "last_timings", None) or {}
        stats.decode_ms += int(split.get("decode") or 0)
        stats.forward_ms += int(split.get("forward") or 0)

    step = _chunk_size(backend)
    from itertools import islice
    pending = pending_items()
    while True:
        if should_stop is not None and should_stop():
            stats.stopped = True
            break
        if guard is not None and not wait_for_guard():
            stats.stopped = True
            break
        chunk = list(islice(pending, step))
        if not chunk:
            break
        try:
            preds = backend.predict_batch(inputs_of(chunk))
        except Exception as exc:      # noqa: BLE001 - 一块坏了不能连坐
            first_error: Exception | None = exc
        else:
            first_error = None
            account_timings()
            for item, pred in zip(chunk, preds):
                commit(item, pred)
            if guard is not None:
                guard.after_unit()
            continue

        # 这一块整体炸了：逐张重试一次，把账算到真正坏的那张头上。
        for item in chunk:
            try:
                one = backend.predict_batch(inputs_of([item]))[0]
            except Exception as exc:  # noqa: BLE001
                fail(item, _reason_of(exc) or _reason_of(first_error))
                continue
            account_timings()
            commit(item, one)
        if guard is not None:
            guard.after_unit()

    stats.wall_ms = int((time.perf_counter() - started) * 1000)
    return stats
