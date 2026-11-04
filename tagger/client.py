"""成员端打标客户端：分发包 → vLLM 推理 → 回收产物。

流程位置
--------
    总机 split 打包 → **本模块（成员机上跑一遍）** → 交回 → 总机 merge

进来的东西是一个 zip 分发包，里面只有两样：图片（平铺，名字就是 image_id）
和 manifest.jsonl（这一包负责哪些 image_id）。出去的东西是
`<image_id>-tag.json` 与 `<image_id>-nl.json` 两个文件——注意是**两个独立文件**，
不是一个总表，理由见 merge.py：文件存在性就是完成信号，没有歧义。

四个不显然的取舍
----------------
1. **一次请求出两段，不是两次请求。**
   tag 和 caption 看的是同一张图、同一个视觉编码，分两次请求等于把 prefill
   付两遍钱。所以 prompt 要求模型按 `<TAGS>...</TAGS>` 与
   `<CAPTION>...</CAPTION>` 两个块输出，一次拿全。

2. **用块标记而不是 JSON。**
   JSON 一旦被 max_tokens 截断就是废的，解析不出任何东西——连已经说完的 tag
   都一起丢。块标记的截断是局部的：`<TAGS>` 段完整、`<CAPTION>` 段断在半句，
   至少能拿到前一半，而且断在哪儿一眼可见（能进重试，而不是静默变脏数据）。
   代价是解析要写正则，也要容忍模型少写闭合标签。

3. **客户端不算 sha256，也不重新编号。**
   image_id 由分发环节一次性分配（见 naming.py），本地再生成一次就会撞号。
   哈希同理：包内图片已经用 image_id 定位，再算一遍 sha256 是纯 CPU 浪费，
   而它唯一的作用是"同一张图出现两次"——这件事由分发/manifest 负责。

4. **跳过判定看「产物存在且非空」，不看进度记录。**
   进度文件可能比产物新（写完记录才崩、或产物被误删），
   反过来产物也可能是上一版模型打的。所以两者都要看：文件在、内容非空，
   才跳过；`--force` 时才无条件重打。

和资源守护的关系
----------------
每完成一个单元（一张图）调一次 guard 的让步（`after_unit`），这是 guard 里
验证过的做法：在任务边界 sleep(busy*(1-duty)/duty)，不碰驱动、不干扰 vLLM
自己的连续批处理。并发数 > 1 时"单元"变成"一批在途请求"，让步按批结算——
仍贴着 duty，只是粒度变粗。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tarfile
import threading
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import httpx

from .artist import ArtistRule, format_tag, inject_into_caption
from .config import ConfigError, OpsConfig, OutputSpec
from .cpu_tagger import (TagBackend, TagPrediction, TaggerUnavailable, build_backend,
                         resolve_device)
from .distribute import read_shard_manifest
from .guard import ResourceGuard
from .naming import annotation_name, normalize_ext
from .netutil import trust_env_for
from .quality import QualityGate, QualityPolicy
from .schema import Annotation, QC

# prompt 一改，版本号必须跟着改。provenance 靠它回答"这批数据是哪套指令产的"，
# 不清不楚的话，两个 prompt 混出来的数据集永远查不出差异来自哪。
# v2：长度与 tag 数量改为按配置生成（v1 是写死的 15-40 tag / 480-1000 字符）。
PROMPT_VERSION = "v2"

SYSTEM_PROMPT = (
    "You annotate images for diffusion-model training data. "
    "You are precise and literal: you describe only what is visible in the image. "
    "You never guess the artist, the character, or the series."
)

_TAG_RULES = """<TAGS> rules:
- {lo} to {hi} tags, lowercase, words joined by underscores (danbooru style: long_hair, looking_at_viewer, depth_of_field).
- Order from most to least salient; separate with commas only.
- Most images land around {anchor_lo}-{anchor_hi} tags; go higher only when the image really is that dense.
- Only tag what is clearly visible: subject count and type, hair, eyes, clothing, pose, action, setting, props, camera angle, framing, lighting, colour palette, art style.
- Do not output quality or score words (masterpiece, best quality, 8k, highres, ultra detailed).
- Do not output the artist name, the character name, or the series name: you cannot know them and guessing poisons the label."""

_CAPTION_RULES = """<CAPTION> rules:
- One paragraph of {char_lo}-{char_hi} characters. Longer is fine; empty is a failure.
- Order the content: main subject; its appearance (colour, material, texture); setting and background; composition and camera angle; lighting; any visible text, transcribed verbatim inside double quotes.
- Describe only what is visible. Do not speculate about anything outside the frame.
- English only, plain prose: no markdown, no bullet points, no headings, no "Caption:" prefix."""


def _prompt_for(output: OutputSpec) -> str:
    """按配置里的预算生成 prompt。

    ★这里的长度引导与质检硬线刻意不是一个数：
      硬线是 32~256 token（容忍"图简单、话少"，不因此判废），
      引导是 400~1000 字符（≈90~230 token）。
      把引导值也降到硬线，等于告诉模型"写 32 个 token 就行"，
      那一批数据会整体变贫——而贫是没法在质检里被抓到的。
    """
    char_lo = max(400, output.nl_min_tokens * 4)
    char_hi = max(char_lo + 200, min(1200, output.nl_max_tokens * 4))

    lo, hi = output.tag_min_count, output.tag_max_count
    anchor_lo = min(max(30, lo), hi)
    anchor_hi = min(max(60, anchor_lo), hi)

    tag_rules = _TAG_RULES.format(lo=lo, hi=hi,
                                  anchor_lo=anchor_lo, anchor_hi=anchor_hi)
    cap_rules = _CAPTION_RULES.format(char_lo=char_lo, char_hi=char_hi)

    mode = output.mode
    if mode == "tag":
        return ("Produce one block about the image, in exactly this format:\n\n"
                "<TAGS>\ntag_one, tag_two, tag_three\n</TAGS>\n\n"
                f"{tag_rules}\n\nOutput the TAGS block and nothing else.")
    if mode == "nl":
        return ("Produce one block about the image, in exactly this format:\n\n"
                "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
                f"{cap_rules}\n\nOutput the CAPTION block and nothing else.")
    return ("Produce two blocks about the image, in exactly this format:\n\n"
            "<TAGS>\ntag_one, tag_two, tag_three\n</TAGS>\n"
            "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
            f"{tag_rules}\n\n{cap_rules}")


# 两道正则都写成「闭合标签可有可无」：模型偶尔只写开标记就往下走，
# 那种输出仍然有值，硬要求闭标记会把它整条判废。
# 但"没有闭标记"不等于"一直到文末"：如果后面紧跟着另一段的开标记，
# 必须在那儿截断，否则 `<CAPTION>` 的正文会被当成 tag 洗进词表
# （实测会产出 captiona_woman_with 这种鬼东西）。
_TAG_BLOCK = re.compile(r"<TAGS>(.*?)(?:</TAGS>|(?=<CAPTION>)|\Z)", re.S | re.I)
_CAPTION_BLOCK = re.compile(r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|\Z)", re.S | re.I)

_TAG_SPLIT = re.compile(r"[,;\n、]+")
_TAG_BAD = re.compile(r"[^a-z0-9_\-.:()]+")

# 只砍两类词，别的都留：
#   质量词   —— 提示词模板的残留（"masterpiece, best quality"），不是画面事实
#   身份词   —— 模型在猜作者/角色/系列，那是它不该产出的东西，混进 tag 词汇表
#              之后训练侧的"画师绑定"就被污染了
_TAG_DROP = frozenset({
    "masterpiece", "best_quality", "high_quality", "normal_quality", "good_quality",
    "highres", "absurdres", "lowres", "ultra_detailed", "extremely_detailed",
    "detailed", "8k", "4k", "uhd", "hd",
    "score_9", "score_8_up", "score_7_up",
    "safe", "sensitive", "nsfw", "explicit", "questionable",
    "artist_name", "character_name", "series_name", "copyright_name",
    "signature", "artist_logo",
})

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}

TAG_SUFFIX = "-tag.json"
NL_SUFFIX = "-nl.json"

# 质检只看最显著的前 N 个 tag。
# 这是 tag 上限提到 160 之后的必要配套：quality 的覆盖率门槛是"60% 的
# 强 tag 要被 caption 覆盖"，而 caption 只有 256 token。要求一张群像图的
# 160 个 tag 里有 96 个出现在两百来个词里，是不可能的——结果是高密度图
# 成批进重试再被判废，而那恰恰是最有价值的样本。
# 前 20 个是 prompt 里明确要求"按重要性降序"的那一段，它们必须在 caption
# 里出现，否则就是 tag 与描述各说各话。
_QC_TAG_HEAD = 20


class InferenceError(Exception):
    """单次请求失败。可重试的（超时/5xx）与不可重试的（404 模型名错）要分开。"""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Stopped(Exception):
    """守护判定为 stop（磁盘满等）。继续写只会产生坏产物，直接收工。"""


# ------------------------------------------------------------------ 解析


def normalize_tags(raw: str) -> list[str]:
    """把模型吐的 tag 段洗成可入训练集的形态。

    规则刻意保守：只做「必须做」的转换（小写、空白转下划线、去非法字符），
    不做同义词归并、不做单复数归一——那些属于训练侧的词表，客户端擅自归一
    会让同一张图在两台机器上产出不同的 tag。
    """
    out: list[str] = []
    seen: set[str] = set()
    for part in _TAG_SPLIT.split(raw):
        t = part.strip().lower().replace(" ", "_")
        if not t:
            continue
        t = _TAG_BAD.sub("", t)
        t = re.sub(r"_{2,}", "_", t).strip("_")
        if not t or len(t) < 2 or len(t) > 64:
            continue
        if t in _TAG_DROP or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def normalize_caption(raw: str) -> str:
    """压成一段。保留标点与引号，只去 markdown 装饰与多余空白。"""
    text = raw.strip()
    text = re.sub(r"^\s*(caption|description)\s*[:：]\s*", "", text, flags=re.I)
    text = text.replace("**", "").replace("##", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_output(text: str, mode: str) -> tuple[list[str], str]:
    """从模型输出里取出 (tags, caption)。

    没有块标记时按模式兜底：tag/nl 单模式把整段当结果；both 模式直接判空——
    在两个块的输出里靠猜切分，等于批量制造脏数据，宁可让它进重试。
    """
    body = (text or "").strip()
    if not body:
        return [], ""

    have_marker = bool(_TAG_BLOCK.search(body) or _CAPTION_BLOCK.search(body))
    if not have_marker:
        if mode == "tag":
            return normalize_tags(body), ""
        if mode == "nl":
            return [], normalize_caption(body)
        return [], ""

    tags: list[str] = []
    caption = ""
    if mode in ("tag", "both"):
        m = _TAG_BLOCK.search(body)
        tags = normalize_tags(m.group(1)) if m else []
    if mode in ("nl", "both"):
        m = _CAPTION_BLOCK.search(body)
        caption = normalize_caption(m.group(1)) if m else ""
    return tags, caption


# ------------------------------------------------------------------ 任务


@dataclass(slots=True)
class TaskRow:
    """一行待处理的活。shard 记着来源，读图时要靠它定位 zip。"""

    shard: str
    image_id: str
    file: str
    artist: str = ""
    rel_path: str = ""
    # HF 模式下才有值：`hf://datasets/<repo>/<shard>#<member>`。
    # 会原样写进产物，主机拿它拼总表的 image_path。
    image_path: str = ""

    @property
    def ext(self) -> str:
        return normalize_ext(self.file)


@dataclass(slots=True)
class Outcome:
    """一张图的处理结果。失败也带原因，便于按原因聚合。"""

    image_id: str
    ok: bool
    tag: dict | None = None
    nl: dict | None = None
    error: str = ""
    wall_ms: int = 0
    attempts: int = 0
    tokens: int = 0


# 进度回调。kind ∈ {"done", "failed", "skipped", "blacklisted"}：
#   done / failed 带 outcome（failed 里含写盘失败，那种同样没有产物）；
#   skipped / blacklisted 没有 outcome——这两张根本没去推理，编一个出来只会骗人。
# 订阅方（Web UI 或任何前端）靠它推实时进度，CLI 不传就等于没有。
ProgressHook = Callable[[str, TaskRow, "Outcome | None", "ClientStats"], None]


class StageTimer:
    """分段计时：read / tag / b64 / http / parse / gate / write。

    为什么不用 cProfile：它按函数调用切分，一次请求会被切成上千个片段，
    加上 GIL 切换和 I/O 等待，结果反而看不出哪一段是瓶颈。这里只记七个
    粗粒度阶段，够回答"时间到底花在哪"——而这正是决定"要不要优化"的唯一依据。

    注意：各阶段不是互斥的。tag（CPU 反推）与 http（等 GPU）是并行跑的，
    所以各阶段的 total 加起来会超过墙钟时间，别把它当占比看；看 avg_ms
    （每次调用平均多久）和 count（发生了几次）才有意义。
    """

    STAGES = ("read", "tag", "b64", "http", "parse", "gate", "write")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total: dict[str, float] = {}
        self._count: dict[str, int] = {}

    def add(self, stage: str, seconds: float) -> None:
        with self._lock:
            self._total[stage] = self._total.get(stage, 0.0) + seconds
            self._count[stage] = self._count.get(stage, 0) + 1

    @contextmanager
    def track(self, stage: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add(stage, time.perf_counter() - t0)

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            items = list(self._total.items())
            counts = dict(self._count)
        out: dict[str, dict[str, float]] = {}
        for stage, total in items:
            n = counts.get(stage, 0)
            out[stage] = {
                "total_ms": round(total * 1000.0, 1),
                "count": n,
                "avg_ms": round(total * 1000.0 / n, 2) if n else 0.0,
            }
        return out


@contextmanager
def _null_track():
    """占位用的空计时。没有计时器时不必在每个调用点写 if 判断。"""
    yield


@dataclass(slots=True)
class ClientStats:
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    # 累计失败过多次、这次直接不理的。单列出来，否则"跳过"里混着
    # "已完成"和"永远做不成"，看报告的人会以为进度正常。
    blacklisted: int = 0
    tokens: int = 0
    wall_ms: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        lines = [
            f"总共       : {self.total}",
            f"完成       : {self.done}",
            f"跳过（已有）: {self.skipped}",
            f"跳过（拉黑）: {self.blacklisted}",
            f"失败       : {self.failed}",
            f"生成 token : {self.tokens}",
        ]
        if self.done or self.failed:
            n = self.done + self.failed
            avg = self.wall_ms / n / 1000 if n else 0.0
            lines.append(f"平均耗时   : {avg:.3f}s/张")
        for reason, n in sorted(self.by_reason.items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"  {reason:<28} {n}")
        return lines


# ------------------------------------------------------------------ 读图


class ShardReader:
    """按 manifest 从包里取图。zip 与 tar 都认。

    句柄缓存 + 一把锁：`ZipFile.read` / `TarFile.extractfile` 内部都会 seek，
    多线程共用一个句柄而不加锁会读到别人的字节（错位、偶发、极难复现）。
    锁的代价在这里可以忽略——真正的瓶颈是 GPU 推理，不是这点读盘。

    为什么 tar 也走随机读而不是顺序流：本地只放一个 shard 的时候，
    顺序读会要求主流程严格按 tar 内顺序消费，而这跟"跳过已完成、并发乱序
    返回"是冲突的。tarfile 的 "r:" 模式支持按成员名取，代价只是多一次 seek。
    """

    def __init__(self, shards: Iterable[str]):
        self._handles: dict[str, object] = {}
        self._kinds: dict[str, str] = {}
        self._lock = threading.Lock()
        for s in shards:
            if s.lower().endswith((".tar", ".tar.gz", ".tgz")):
                self._handles[s] = tarfile.open(s, "r:*")
                self._kinds[s] = "tar"
            else:
                self._handles[s] = zipfile.ZipFile(s, "r")
                self._kinds[s] = "zip"

    def read(self, shard: str, member: str) -> bytes:
        handle = self._handles.get(shard)
        if handle is None:
            raise KeyError(f"分发包未打开: {shard}")
        with self._lock:
            if self._kinds[shard] == "tar":
                fh = handle.extractfile(member)       # type: ignore[union-attr]
                if fh is None:
                    raise KeyError(f"{Path(shard).name} 里没有成员 {member}")
                try:
                    return fh.read()
                finally:
                    fh.close()
            return handle.read(member)                # type: ignore[union-attr]

    def close(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()                        # type: ignore[union-attr]
            except OSError:
                pass
        self._handles.clear()


def _mime_of(name: str) -> str:
    return _MIME.get(Path(name).suffix.lower(), "image/jpeg")


# ------------------------------------------------------------------ 落盘


def _write_json_atomic(path: Path, payload: dict) -> None:
    """tmp + fsync + replace。

    直接 open(w) 写的话，断电/崩溃会留下一个能被 json.loads 读出一半的文件，
    merge 那边只看"文件在不在"，这条就会被当成有效产出收进总表。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _existing_ok(path: Path, key: str) -> bool:
    """产物已存在且该字段非空才算完成。与 merge 的判空口径保持一致。"""
    if not path.is_file():
        return False
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(obj, dict):
        return False
    return bool(str(obj.get(key) or "").strip())


# ------------------------------------------------------------------ 客户端


class _BatchTagExecutor:
    """单线程 + 攒批的 tag 执行器。

    为什么不用 ThreadPoolExecutor：它是"一次交一个任务"，模型永远只见到 batch=1。
    GPU 上前向的固定开销（kernel 发射 1600 次、CPU 侧 dispatch）跟 batch 无关，
    所以只有把图凑进同一次前向才拿得到收益（实测 79 张：fp32 57.8 → bf16+b4 17.3
    ms/张）。

    为什么**不等**：攒批最容易写错的地方是"等一小会儿凑更多"。这里只取队列里
    已经排着的（get_nowait），永不为了凑数阻塞——tag 与 GPU 侧的 NL 是并行跑的，
    人为等待会直接把 NL 的完成时间拖后。队列空着时它就和逐张跑一模一样。
    """

    def __init__(self, fn, *, max_batch: int = 1, stage=None):
        import queue

        self._queue = queue.Queue()
        self._fn = fn
        self._max = max(1, int(max_batch))
        self._stage = stage
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="tag-batch",
                                        daemon=True)
        self._thread.start()

    def submit(self, item):
        fut: Future = Future()
        if self._closed:
            fut.set_exception(RuntimeError("tag 执行器已关闭"))
            return fut
        self._queue.put((item, fut))
        return fut

    def _loop(self) -> None:
        import queue

        while True:
            first = self._queue.get()
            if first is None:
                return
            batch = [first]
            while len(batch) < self._max:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._queue.put(None)
                    break
                batch.append(nxt)
            items = [it for it, _ in batch]
            try:
                outs = self._fn(items)
                if len(outs) != len(items):
                    raise RuntimeError(
                        f"tag 后端返回 {len(outs)} 条，提交了 {len(items)} 张")
            except BaseException as exc:  # noqa: BLE001 - 必须原样转给等待方
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(exc)
                continue
            for (_, fut), out in zip(batch, outs):
                if not fut.done():
                    fut.set_result(out)

    def shutdown(self, wait: bool = False) -> None:
        self._closed = True
        self._queue.put(None)
        if wait:
            self._thread.join(timeout=5.0)


class TaggerClient:
    """单张图的完整处理：取图 → 推理（含重试）→ 质检 → 组织产物。

    这一层不做 IO 之外的决策：写文件、统计、让步都在 run() 里，
    这样并发与单线程走的是同一条提交路径，不会有两套口径。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        output: OutputSpec,
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_s: int,
        max_attempts: int,
        gate: QualityGate,
        prov_base: dict,
        artist_rule: ArtistRule,
        use_file_url: bool = False,
        spool_dir: Path | None = None,
        seed: int | None = None,
        tag_backend: TagBackend | None = None,
        need_tags: bool = True,
        prompt_text: str | None = None,
        variant: str = "",
        stage: StageTimer | None = None,
        pixel_cap: int = 0,
        concurrency: int = 1,
        escalate_url: str = "",
        escalate_model: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.output = output
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        self.gate = gate
        self.prov_base = prov_base
        self.artist_rule = artist_rule
        self.use_file_url = use_file_url
        self.spool_dir = spool_dir
        self.seed = seed
        # 服务端 mm-processor-kwargs 的钳制上限（来自指纹）。0 = 未知。
        # 重试提分辨率最多抬到这里：再往上服务端还是会钳回去。
        self.pixel_cap = int(pixel_cap or 0)
        # AB 变体：prompt_text 是变体渲染后的完整指令（可能是自动 prompt
        # 追加一段，也可能整段替换）。为空就用配置生成的那份。
        self.prompt = prompt_text or _prompt_for(output)
        self.variant = variant
        # 分段计时器。默认自己建一个，好让单独调用 TaggerClient 的代码
        # （测试、基准脚本）不用关心它；正式流程会把它传进来以便汇总输出。
        self.stage = stage or StageTimer()
        # tag_backend 非空时，output 已经被收窄成"VLM 只出 NL"；
        # need_tags 记的是"本次要不要写 tag 文件"，两个概念不能混。
        self.tag_backend = tag_backend
        self.need_tags = need_tags and (output.wants_tag or tag_backend is not None)
        self._tag_pool: _BatchTagExecutor | None = None
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            # 连接池要跟并发同步放大。写死 32 的话，并发调到 64 时后一半请求
            # 在客户端排队，看起来"并发没生效"，而服务端其实还有空位。
            limits=httpx.Limits(
                max_connections=max(32, int(concurrency) * 2),
                max_keepalive_connections=max(16, int(concurrency)),
            ),
            # 本机地址不吃系统代理。打标机常开着 Clash 一类的代理，
            # 它的规则未必放过 127.0.0.1：轻则每张图的 base64 多绕一圈
            # （吞吐直接砍半），重则请求根本到不了 vLLM，报出来的却是
            # "服务端超时"。远程地址维持默认，那边可能真需要代理。
            trust_env=trust_env_for(self.base_url),
        )
        # 重试升级：质检判定"重试用尽仍不合格"时可以换一个更大的模型再打一次。
        # vLLM 一个进程只服务一套权重，所以升级目标只能是**另一个地址**上的
        # 第二份服务。地址/模型名留空 = 不启用（默认），此时 model_hint 被忽略。
        # 独立连接池：两个服务可能在不同机器，超时与代理策略都该各算各的。
        self.escalate_model = (escalate_model or "").strip()
        self._escalate_http: httpx.Client | None = None
        self._escalate_base = (escalate_url or "").strip().rstrip("/")
        if self.escalate_model and self._escalate_base:
            self._escalate_http = httpx.Client(
                timeout=httpx.Timeout(timeout_s, connect=10.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                trust_env=trust_env_for(self._escalate_base),
            )
        else:
            # 只配了一半要报出来：静默不生效比报错更难查。
            if self.escalate_model or self._escalate_base:
                print("[client][警告] 重试升级需要同时给地址与模型名，"
                      "当前只有一半，升级不会生效。", file=sys.stderr)
            self.escalate_model = ""

    # -------------------------------------------------------------- 推理

    def close(self) -> None:
        self._http.close()
        if self._escalate_http is not None:
            self._escalate_http.close()
            self._escalate_http = None
        if self._tag_pool is not None:
            self._tag_pool.shutdown(wait=False)
            self._tag_pool = None

    def _tag_executor(self) -> _BatchTagExecutor:
        """CPU tag 专用执行器，固定 1 个 worker。

        CPU 侧并行没有收益：同一个模型多线程只是互相抢核，而 tagger 的瓶颈
        是单张前向。1 个 worker 也天然限制住"GPU 已经跑完、CPU 还在追"的
        堆积量——不会攒出几百张待算的图。

        批处理顺带在这里解决：worker 每次开工时把队列里**已经排着**的图一起
        取走（上限 = 后端的 batch_size）。不额外等待，所以 batch=1 时行为与
        逐张跑完全一致，攒批也没引入任何人为延迟。
        """
        if self._tag_pool is None:
            self._tag_pool = _BatchTagExecutor(
                self._cpu_tags_batch,
                max_batch=getattr(self.tag_backend, "batch_size", 1) or 1,
                stage=self.stage,
            )
        return self._tag_pool

    def _cpu_tags_batch(self, batch: list[bytes]) -> list[TagPrediction]:
        """在 tag 线程里计时。必须在这里测，不能在外面测 .result() 的等待——
        那个等待里混着 GPU 的时间，量出来的是"谁先完成"而不是"CPU 花了多久"。

        这里**不再自己解码**：原始字节直接交给后端。原先客户端先
        `Image.open().convert("RGB")` 一遍、后端 `_open_rgb` 再 convert 一遍，
        1024² 的图每张白付一次全尺寸拷贝（实测 3~5 ms）。解码只该有一次，
        放在最懂图片格式的那一层（后端可能换 torchvision 或 GPU 解码）。

        返回的是完整的 TagPrediction 而不是光秃秃的 tag 列表：rating 那一栏
        要跟着走到产物里（它不属于 tag，但分类头已经算出来了）。
        """
        with self.stage.track("tag"):
            preds = self.tag_backend.predict_batch(batch)
            # 后端会把上一次的耗时拆成 解码 / 前向 报上来（tag 执行器是单
            # worker，读到的就是本次）。没有拆分的后端这里两个都是 0，
            # 那就只有 "tag" 一段，不编数字。
            split = getattr(self.tag_backend, "last_timings", None) or {}
        if split.get("decode"):
            self.stage.add("tag_decode", split["decode"] / 1000.0)
        if split.get("forward"):
            self.stage.add("tag_fwd", split["forward"] / 1000.0)
        return preds

    def _budget_data(self, data: bytes | None, budget: int) -> bytes | None:
        """需要客户端出手的场合才缩图。

        预算 ≥ 服务端钳制时，服务端 smart_resize 做的是同一件事，客户端
        再缩一道只是白付一次解码，还把像素口径从"服务端算法"换成"本地
        算法"。只有当预算低于钳制（serve 留了余量、或没有指纹可依）时，
        基线才必须由客户端自己执行。
        """
        if not budget or not data:
            return data
        if self.pixel_cap and budget >= self.pixel_cap:
            return data
        return _resize_to_budget(data, budget)

    def _image_part(self, data: bytes, mime: str, file_name: str) -> tuple[dict, Path | None]:
        """返回 (content part, 临时文件路径)。

        默认走 base64：图片本来就在 zip 里，用 file:// 必须先落盘，
        落盘意味着「磁盘要够、服务端要放行该目录、用完要删」三件额外的事。
        只有 --use-file-url 且显式开了服务端白名单时才值得省那 33% 体积。
        """
        if self.use_file_url and self.spool_dir is not None:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            # 名字用 image_id + 原扩展名：vLLM 按扩展名选解码器，丢了会被当 jpeg
            path = self.spool_dir / f"{Path(file_name).stem}{normalize_ext(file_name)}"
            path.write_bytes(data)
            part = {"type": "image_url",
                    "image_url": {"url": path.resolve().as_uri()}}
            return part, path
        b64 = base64.b64encode(data).decode("ascii")
        return ({"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}}, None)

    def _infer(self, image_part: dict, temperature: float) -> tuple[str, int]:
        """一次推理（主服务）。"""
        return self._infer_at(image_part, temperature, escalate=False)

    def _infer_at(self, image_part: dict, temperature: float, *,
                  escalate: bool = False) -> tuple[str, int]:
        """一次推理，可指名打升级服务。

        拆成两个入口是刻意的：`_infer` 是"正常路径"的唯一形状，测试与基准
        脚本替换它就能接管全部推理；只有真的发生了升级才会走 `_infer_at`。
        合成一个带默认参数的方法会让每个调用方都要跟着改。
        """
        http, base_url, model_name = self._target(escalate)
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [image_part, {"type": "text", "text": self.prompt}]},
            ],
            "temperature": temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        if self.seed is not None:
            payload["seed"] = self.seed
        try:
            r = http.post(f"{base_url}/v1/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise InferenceError(f"超时: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise InferenceError(f"连接失败: {type(exc).__name__}: {exc}") from exc

        if r.status_code >= 500:
            raise InferenceError(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            # 4xx 基本是配置问题（模型名错、图片被拒），重试没有意义
            raise InferenceError(f"HTTP {r.status_code}: {r.text[:200]}", retryable=False)

        try:
            body = r.json()
            text = body["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise InferenceError(f"响应结构不对: {type(exc).__name__}") from exc
        usage = body.get("usage") or {}
        return text, int(usage.get("completion_tokens") or 0)

    def _target(self, escalate: bool) -> tuple[httpx.Client, str, str]:
        """这一轮该打哪个服务。升级没配置时永远回主服务。"""
        if escalate and self._escalate_http is not None and self.escalate_model:
            return self._escalate_http, self._escalate_base, self.escalate_model
        return self._http, self.base_url, self.model_name

    # -------------------------------------------------------------- 溯源

    def _provenance(self, temperature: float, *, tag: bool = False,
                    escalated: bool = False) -> dict:
        base = dict(self.prov_base)
        # 升级重试用的是另一套权重，必须如实记下来：否则事后按 model_id 分组
        # 会把两套模型混成一批，而这批里只有少数样本来自大模型——那是最难
        # 发现的一类污染。`escalated` 让清洗侧一行就能把这类批次挑出来。
        if escalated:
            base["escalated"] = True
            base["escalated_model"] = self.escalate_model
        base["temperature"] = round(temperature, 3)
        base["prompt_ver"] = PROMPT_VERSION
        # prompt 指纹。AB 变体之间常常只差一句话，prompt_ver 分不出来；
        # 事后想回答"这批的 12 个 tag 是哪套指令逼出来的"，只能靠它。
        base["prompt_sha"] = hashlib.sha256(
            self.prompt.encode("utf-8")).hexdigest()[:12]
        if self.variant:
            base["variant"] = self.variant
        base["seed"] = self.seed
        if tag:
            # 只写进 tag 产物：这两个字段描述的是 tagger，NL 那侧没有这个概念。
            # 为什么非写不可：fp32 / tf32 / bf16 出的 tag 集合不一样（实测
            # bf16 每 79 张会有 2 个 tag 真改判），事后想回答"这批为什么
            # 多出几个 tag"，没有这个字段就只能靠猜。
            # 注意与 base["precision"] 区分：那个是 VLM 的精度，两者会打架。
            base["tag_backend"] = self.tag_backend.name if self.tag_backend else "vlm"
            base["tag_precision"] = (self.tag_backend.precision
                                     if self.tag_backend else "vlm")
        return base

    # -------------------------------------------------------------- 单张

    def process(self, task: TaskRow, data: bytes | None = None,
                reader: ShardReader | None = None) -> Outcome:
        """取图 → 重试循环 → 组织两个 payload。异常不外抛，全部折成失败原因。

        `data` 由调用方给（parquet 流式模式下图片已经在内存里），
        给不了才回去问 reader 要（zip/tar 模式按需读，省内存）。
        """
        started = time.perf_counter()
        if data is None:
            if reader is None:
                return Outcome(task.image_id, False,
                               error="没有图片数据，也没有可用的读取器")
            try:
                with self.stage.track("read"):
                    data = reader.read(task.shard, task.file)
            except (KeyError, OSError, zipfile.BadZipFile, tarfile.TarError,
                    RuntimeError) as exc:
                return Outcome(task.image_id, False,
                               error=f"读图失败: {type(exc).__name__}: {exc}")

        mime = _mime_of(task.file)
        # 基线像素预算（attempt 0 的口径）。重试时按质检给出的 RetryPlan
        # 抬高（见 _run），这里先把基线执行掉。缩过的字节只进 image_part，
        # `data` 必须保持原始字节：重试提分辨率要从原始字节重组，从缩过的
        # 字节出发的话，抬再高也回不去。
        with self.stage.track("b64"):
            image_part, spool = self._image_part(
                self._budget_data(data, int(self.prov_base.get("max_pixels") or 0)),
                mime, task.file)

        # CPU tag 与 GPU 推理同时起跑。GPU 每张 1~3 秒，CPU 这段时间正好把
        # tag 算完；串行做的话总耗时 = 两者相加，白等一半。
        # 只在"tag 由 CPU 出"时提交，VLM 自己吐 tag 的模式下没有这一步。
        # 提交原始字节：tag 后端有自己的输入口径（如 wd-eva02 的 448²），
        # VLM 的像素预算不该改变 tag 的输入。
        tag_future = None
        if self.tag_backend is not None and self.need_tags:
            tag_future = self._tag_executor().submit(data)

        try:
            return self._run(task, image_part, started, tag_future, data=data)
        finally:
            # spool 文件只在 --use-file-url 时存在。必须每张都删：
            # 一万张图的副本能吃掉几十 GB，而它们全都已经躺在 zip 里了。
            if spool is not None:
                try:
                    spool.unlink()
                except OSError:
                    pass

    def _run(self, task: TaskRow, image_part: dict, started: float,
             tag_future: Future | None = None, *,
             data: bytes | None = None) -> Outcome:
        """重试循环。拆出来是为了让 spool 清理只在 process 的 finally 里写一次。

        `data` 是原始图片字节：重试提分辨率时要按抬高的预算重新组装
        image_part，从原始字节出发（而不是从上一轮缩过的字节出发）。
        """
        want_tag = self.output.wants_tag      # VLM 是否产出 tag（CPU 模式下恒 False）
        want_nl = self.output.wants_nl
        temperature = self.temperature
        attempts = 0
        reasons: list[str] = []
        tokens_total = 0
        # 当前生效的像素预算。attempt 0 是基线，重试按 RetryPlan 抬。
        budget = int(self.prov_base.get("max_pixels") or 0)
        # 这一条最终是否走了升级服务。一旦升上去就不再退回：重试的语义是
        # "小模型救不回来"，退回去只会重复同一份失败。
        escalated = False
        # 质检的 CLIP 需要一张 PIL 图。只在真的启用 CLIP 时才解码——正常
        # 打标路径一张图只解一次（在后端），这里多解一次是实打实的成本。
        qc_image = None
        qc_image_ready = False

        while True:
            attempts += 1
            try:
                with self.stage.track("http"):
                    if escalated:
                        text, tokens = self._infer_at(image_part, temperature,
                                                      escalate=True)
                    else:
                        text, tokens = self._infer(image_part, temperature)
            except InferenceError as exc:
                reasons = [str(exc)]
                if not exc.retryable or attempts > self.max_attempts:
                    break
                time.sleep(min(2.0 * attempts, 10.0))   # 退避，别把服务打成雪崩
                continue

            tokens_total += tokens
            with self.stage.track("parse"):
                tags, caption = parse_output(text, self.output.mode)

            # 显式空产出检查必须放在质检之前：质量门在 tags 为空时
            # tag_coverage 返回 1.0（"没有强 tag，视为全覆盖"），
            # 于是"模型一个 tag 都没给"会被判成合格。这个洞只能在这里补。
            reasons = []
            if want_tag and not tags:
                reasons.append("没有产出 tag")
            if want_nl and not caption:
                reasons.append("没有产出 caption")

            # tag 数量也是 prompt 里的硬约定，但质量门不管它（那是文本质量维度）。
            # 多给的按重要性顺序截断——prompt 要求降序输出，截掉的正好最不重要，
            # 比整条判废合理；少给的只能重试，那是模型敷衍。
            if want_tag and tags:
                if len(tags) < self.output.tag_min_count:
                    reasons.append(
                        f"tag 过少（{len(tags)} < {self.output.tag_min_count}）")
                elif len(tags) > self.output.tag_max_count:
                    tags = tags[: self.output.tag_max_count]

            if reasons:
                if attempts > self.max_attempts:
                    break
                continue

            head = tags[:_QC_TAG_HEAD]
            ann = Annotation(
                image_sha256="",          # 客户端不算哈希，见模块头说明
                image_path=task.file,
                tags=[(t, 1.0) for t in head],
                caption_dense=caption,
                tag_line=", ".join(head),
                qc=QC(caption_chars=len(caption), nl_tokens=tokens),
            )
            with self.stage.track("gate"):
                # CLIP 打分要的是图本身。解码失败就当没有图（跳过 L2），
                # 不能因为"打分打不了"把一条已经合格的样本判废。
                if not qc_image_ready:
                    qc_image_ready = True
                    # getattr：测试与基准脚本会塞只实现 evaluate 的假质检门，
                    # 不能因为缺少 clip 属性把整条打标路径带崩。
                    if getattr(self.gate, "clip", None) is not None and data is not None:
                        try:
                            from io import BytesIO

                            from PIL import Image

                            with Image.open(BytesIO(data)) as im:
                                qc_image = im.convert("RGB")
                        except Exception:         # noqa: BLE001
                            qc_image = None
                verdict = self.gate.evaluate(
                    ann, attempt=attempts - 1, temperature=temperature,
                    max_pixels=budget, image=qc_image,
                )
            if verdict.level == "pass":
                tags_with_prob: list[tuple[str, float]] = [(t, 1.0) for t in tags]
                # 评级分数（只有 wd-eva02 这类分级头后端会给）。它不进 tag 串，
                # 只作为独立字段落进产物，供数据集清洗/剔除误标用。
                rating: dict[str, float] = {}

                if tag_future is not None:
                    try:
                        pred = tag_future.result()
                        tags_with_prob, rating = pred.tags, pred.rating
                    except Exception as exc:      # noqa: BLE001 - 后端异常花样多
                        return Outcome(
                            task.image_id, False,
                            error=f"CPU tag 反推失败: {type(exc).__name__}: {exc}",
                            wall_ms=int((time.perf_counter() - started) * 1000),
                            attempts=attempts, tokens=tokens_total,
                        )
                    # 分类头是确定性的：同一张图再算一次结果不变，
                    # 数量不够就重试是纯浪费。直接判失败，让人去看阈值。
                    if len(tags_with_prob) < self.output.tag_min_count:
                        return Outcome(
                            task.image_id, False,
                            error=(f"tag 过少（{len(tags_with_prob)} < "
                                   f"{self.output.tag_min_count}），"
                                   f"后端 {self.tag_backend.name if self.tag_backend else '?'}"
                                   f" 的阈值偏低"),
                            wall_ms=int((time.perf_counter() - started) * 1000),
                            attempts=attempts, tokens=tokens_total,
                        )
                    tags_with_prob = tags_with_prob[: self.output.tag_max_count]

                # 只写本模式该产的文件：tag 模式下多写一个 -nl.json，
                # 会让 merge 在 require=tag 时多找一条"本不该存在"的记录，
                # 更糟的是那份 caption 没经过任何按模式裁剪的约束。
                return Outcome(
                    task.image_id, True,
                    tag=(self._tag_payload(task, tags_with_prob, temperature,
                                           tokens, attempts, rating=rating,
                                           escalated=escalated)
                         if self.need_tags else None),
                    nl=(self._nl_payload(task, caption, len(tags_with_prob),
                                         temperature, tokens, attempts, verdict,
                                         escalated=escalated)
                        if want_nl else None),
                    wall_ms=int((time.perf_counter() - started) * 1000),
                    attempts=attempts, tokens=tokens_total,
                )
            reasons = verdict.reasons or [f"质检未通过（{verdict.score:.2f}）"]
            if verdict.level == "drop" or attempts > self.max_attempts:
                break
            if verdict.retry is not None:
                temperature = verdict.retry.temperature
                # 升级目标只在质检点名要升级、且确实配了第二个服务时才切。
                # 切了之后重试不再退回小模型（见上面的 escalated 说明）。
                if (getattr(verdict.retry, "model_hint", "same") == "escalate"
                        and self._escalate_http is not None and not escalated):
                    escalated = True
                # 重试提分辨率：质检按基线×scale^k 给出更高预算，客户端
                # 从原始字节按新预算重组图片。服务端最多钳到 pixel_cap，
                # 抬过头没有意义；预算没超过当前值就不动（温度照常变）。
                nxt = int(getattr(verdict.retry, "max_pixels", 0) or 0)
                if nxt > budget and data is not None:
                    if self.pixel_cap and nxt > self.pixel_cap:
                        nxt = self.pixel_cap
                    if nxt > budget:
                        budget = nxt
                        with self.stage.track("b64"):
                            # use_file_url 模式下 _image_part 落的是同名
                            # spool 文件，process 的 finally 会统一清掉。
                            image_part, _ = self._image_part(
                                self._budget_data(data, budget),
                                _mime_of(task.file), task.file)

        return Outcome(
            task.image_id, False,
            error="; ".join(reasons)[:400],
            wall_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts, tokens=tokens_total,
        )

    # -------------------------------------------------------------- 产物

    def _artist_tag_of(self, artist: str) -> str:
        """注入形态的画师标签。空串表示"不注入"。

        `artist` 字段（json 里）与 `artist_tag`（caption 里）是两件事：
        前者是分发时记录的事实，写进产物只为了追溯，永远照写；
        后者会改变 caption 的文本分布，属于数据集口径，只在配置启用时才给。
        manifest 里一定有画师名（split 阶段强制提取），若拿"有名字"当"该注入"，
        artist.enabled: false 就形同虚设了。
        """
        if not artist or not self.artist_rule.enabled:
            return ""
        return format_tag(artist, self.artist_rule)

    def _tag_payload(self, task: TaskRow, tags: Sequence[tuple[str, float]],
                     temperature: float, tokens: int, attempts: int,
                     *, rating: dict[str, float] | None = None,
                     escalated: bool = False) -> dict:
        names = [t for t, _ in tags]
        payload = {
            "image_id": task.image_id,
            # merge 读的就是这个字段，必须是逗号分隔的字符串
            "tag": ", ".join(names),
            # prob 是有真值的：CPU tagger（分类头）会给分数。
            # VLM 出 tag 时只能填 1.0，那时 tag_backend="vlm"，
            # 下游看到这个字段就知道概率该不该当真。
            "tags": [[t, round(float(p), 4)] for t, p in tags],
            "tag_backend": self.tag_backend.name if self.tag_backend else "vlm",
            "artist": task.artist,
            "artist_tag": self._artist_tag_of(task.artist),
            "ok": True,
            "attempts": attempts,
            "completion_tokens": tokens,
            "provenance": self._provenance(temperature, tag=True,
                                           escalated=escalated),
            "ts": time.time(),
        }
        # 分级分数单列。它**不能**并进 tag 串（描述的是审核等级，不是画面内容），
        # 但丢掉又太可惜——分类头每张图都算过了。空字典不写字段，
        # 老产物的形态保持不变。
        if rating:
            payload["rating"] = {k: round(float(v), 4) for k, v in rating.items()}
        # 只有 HF 模式才有显式路径（本地分发靠 image_id 就能回指）。
        # 写进产物而不是让 merge 现拼：拼装需要知道仓库名和分片名，
        # 而那两样只有产出方知道。
        if task.image_path:
            payload["image_path"] = task.image_path
        return payload

    def _nl_payload(self, task: TaskRow, caption: str, tag_count: int,
                    temperature: float, tokens: int, attempts: int, verdict,
                    *, escalated: bool = False) -> dict:
        artist_tag = self._artist_tag_of(task.artist)
        text = caption
        if artist_tag:
            # 画师标签只进 caption 与 json 字段，不并进 tag 串：
            # `@name` 不是 danbooru 词汇，混进 tag 会把词表撕出一个洞。
            text = inject_into_caption(text, artist_tag, self.artist_rule)
        payload = {
            "image_id": task.image_id,
            "caption": text,
            "artist": task.artist,
            "artist_tag": artist_tag,
            "tag_count": tag_count,
            "nl_tokens": tokens,
            "chars": len(text),
            "score": verdict.score,
            "flags": list(verdict.reasons),
            "ok": True,
            "attempts": attempts,
            "provenance": self._provenance(temperature, escalated=escalated),
            "ts": time.time(),
        }
        if task.image_path:
            payload["image_path"] = task.image_path
        return payload


# ------------------------------------------------------------------ 批处理


def _rel_for_layout(task: TaskRow) -> str:
    """mirror 布局用的相对路径。

    manifest 的 rel_path 形如 `dist-0003.zip::artist/0001.jpg`，把 zip 名当目录
    会产出一个谁都不认识的层级；member（zip 内原始路径）才是镜像该走的那条。
    """
    return task.rel_path or task.file


def _artifact_paths(out_dir: Path, task: TaskRow, layout: str) -> tuple[Path, Path]:
    rel = _rel_for_layout(task)
    tag = out_dir / annotation_name(task.image_id, "tag", layout=layout,
                                    rel_path=rel, style="dash")
    nl = out_dir / annotation_name(task.image_id, "nl", layout=layout,
                                   rel_path=rel, style="dash")
    return tag, nl


def _hf_task(sample: dict, shard: str) -> TaskRow:
    """parquet 的一行 → 任务。shard 里存的是分片名（只用于日志/失败明细）。"""
    path = str(sample.get("image_path") or f"{sample['image_id']}.jpg")
    return TaskRow(
        shard=shard,
        image_id=str(sample["image_id"]),
        file=path,
        artist=str(sample.get("artist") or ""),
        rel_path=path,
        image_path=path,
    )


def load_failure_counts(path: Path | None) -> dict[str, int]:
    """failures.jsonl 里每个 image_id 累计失败过几次。

    这个数是**跨运行累计**的：每次失败都追加一行，重跑时先读旧账。
    没有它的话，一张必然失败的图（坏文件、模型对它稳定吐空）会在每次
    重跑时都重试 3 次再失败——3M 规模下这类样本的浪费是小时级的。
    """
    counts: dict[str, int] = {}
    if path is None or not path.is_file():
        return counts
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return counts
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        iid = str(obj.get("image_id") or "")
        if iid:
            counts[iid] = counts.get(iid, 0) + 1
    return counts


def _already_done(client: TaggerClient, task: TaskRow,
                  out_dir: Path, layout: str) -> bool:
    """两个必产文件都在且内容非空 = 这张已经做完。

    只看"文件存在"不够：崩溃留下的半截 json、被手删过的空文件都会骗过它。
    """
    tag_path, nl_path = _artifact_paths(out_dir, task, layout)
    need_tag = client.need_tags
    need_nl = client.output.wants_nl
    return ((not need_tag or _existing_ok(tag_path, "tag"))
            and (not need_nl or _existing_ok(nl_path, "caption")))


def run_batch(
    client: TaggerClient,
    items: Iterable,
    reader: ShardReader | None,
    *,
    out_dir: Path,
    layout: str,
    force: bool,
    concurrency: int,
    guard: ResourceGuard | None,
    stats: ClientStats,
    failures: Path | None,
    verbose: bool,
    fail_counts: dict[str, int] | None = None,
    max_failures: int = 0,
    stage_timer: StageTimer | None = None,
    sink=None,
    done_ids: set[str] | None = None,
    on_progress: ProgressHook | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ClientStats:
    """跑完一批。单写者：文件只在主线程写，worker 只算。

    这么分是因为"目录里多一个文件"与"两个线程同时 rename"这两件事凑在一起
    调试成本极高，而写两个小 json 根本不值得并行。

    `on_progress` 在**主线程**上逐张回调（跳过/拉黑也算），所以订阅方可以直接
    改自己的状态，不必加锁；传 None 就是 CLI 的老行为，一个字都不变。

    `should_stop` 是协作式停止（Web 的停止按钮）：串行路径当场抛 `Stopped`；
    并发路径停补新任务、等在途请求收尾后正常返回——在途的几张不写产物，
    重跑会被重做，与 guard 熔断同一套语义。
    """
    # 注意：这里不再 len(tasks)。parquet 模式的任务是流式生成的，
    # 长度事先根本不知道（要先读完分片才能数清），所以 total 在迭代时累加。
    done_ids = done_ids or set()

    # 惰性打开：一次都没失败时不该在产物目录里留一个空的 failures.jsonl，
    # 那看起来像"有失败明细而内容被吞了"。
    fail_holder: list = []

    def fail_fh():
        if failures is None:
            return None
        if not fail_holder:
            failures.parent.mkdir(parents=True, exist_ok=True)
            fail_holder.append(failures.open("a", encoding="utf-8", buffering=1))
        return fail_holder[0]

    processed = 0
    warned: list = []

    def emit(kind: str, task: TaskRow, outcome: Outcome | None) -> None:
        """通知订阅方。**故意吞异常**：前端一个 KeyError 不该让三百万张的批
        停在第 3 张；但完全不吭声又会让人以为"进度不动了"，所以只报第一次。
        放在 stats 更新之后调用，订阅方拿到的一定是已结账的数。
        """
        if on_progress is None:
            return
        try:
            on_progress(kind, task, outcome, stats)
        except Exception as exc:
            if not warned:
                warned.append(1)
                print(f"[client][on_progress 异常] {type(exc).__name__}: {exc}"
                      "（后续同类异常不再提示）", file=sys.stderr, flush=True)

    def commit(task: TaskRow, outcome: Outcome) -> None:
        nonlocal processed
        processed += 1
        if not outcome.ok:
            stats.failed += 1
            stats.wall_ms += outcome.wall_ms
            for r in (outcome.error or "未知原因").split("; ")[:3]:
                key = r.split(":")[0].strip()[:60]
                stats.by_reason[key] = stats.by_reason.get(key, 0) + 1
            line = json.dumps({
                "image_id": task.image_id, "file": task.file,
                "shard": Path(task.shard).name, "error": outcome.error,
                "attempts": outcome.attempts, "ts": time.time(),
            }, ensure_ascii=False)
            fh = fail_fh()
            if fh is not None:
                fh.write(line + "\n")
            print(f"[client][失败] {task.image_id}  {outcome.error}", file=sys.stderr, flush=True)
            emit("failed", task, outcome)
            return

        tag_path, nl_path = _artifact_paths(out_dir, task, layout)
        try:
            with (stage_timer.track("write") if stage_timer is not None
                  else _null_track()):
                if sink is not None:
                    sink.add(task.image_id, outcome.tag, outcome.nl)
                else:
                    if outcome.tag is not None:
                        _write_json_atomic(tag_path, outcome.tag)
                    if outcome.nl is not None:
                        _write_json_atomic(nl_path, outcome.nl)
        except OSError as exc:
            stats.failed += 1
            print(f"[client][写入失败] {task.image_id}: {exc}", file=sys.stderr, flush=True)
            # 算完成后挂了写盘，结果同样是"这张没有产物"，对订阅方而言就是 failed。
            emit("failed", task, outcome)
            return

        stats.done += 1
        stats.wall_ms += outcome.wall_ms
        stats.tokens += outcome.tokens
        if verbose:
            n = stats.skipped + processed
            print(f"  [{n}/{stats.total}] {task.image_id}  "
                  f"{outcome.wall_ms / 1000:.2f}s  {outcome.tokens} tok  "
                  f"{outcome.attempts} 次", flush=True)
        elif processed % 100 == 0:
            print(f"[client] {stats.skipped + processed}/{stats.total}  "
                  f"完成 {stats.done}  失败 {stats.failed}", flush=True)
        emit("done", task, outcome)

    def pending_items():
        """惰性生成待处理项。

        **不能先收集成 list**：parquet 是流式的，一个分片上百万行的话，
        先攒 list 等于把整个数据集解码进内存。跳过判定放在这里，
        逐项吐、逐项做。
        """
        for item in items:
            if isinstance(item, tuple):
                task, data = item
            else:
                task, data = item, None
            stats.total += 1
            # 先查黑名单：字典查找比读两个 json 便宜，也更能说明问题
            if (max_failures and fail_counts
                    and fail_counts.get(task.image_id, 0) >= max_failures):
                stats.blacklisted += 1
                if verbose:
                    print(f"  [拉黑] {task.image_id}"
                          f"（累计失败 {fail_counts[task.image_id]} 次）", flush=True)
                emit("blacklisted", task, None)
                continue
            # parquet 模式下"文件存在"这条判定不成立（一个分片里有很多行），
            # 改成查启动时建好的 id 集合。两者语义相同：这张已经做完了。
            if sink is not None:
                if not force and task.image_id in done_ids:
                    stats.skipped += 1
                    if verbose:
                        print(f"  [跳过] {task.image_id}（产物里已有）", flush=True)
                    emit("skipped", task, None)
                    continue
            elif not force and _already_done(client, task, out_dir, layout):
                stats.skipped += 1
                if verbose:
                    print(f"  [跳过] {task.image_id}（产物已存在）", flush=True)
                emit("skipped", task, None)
                continue
            yield task, data

    try:
        if concurrency <= 1:
            for task, data in pending_items():
                if should_stop is not None and should_stop():
                    raise Stopped("收到停止请求")
                if guard is not None:
                    _check_guard(guard)
                outcome = client.process(task, data, reader)
                commit(task, outcome)
                if guard is not None:
                    guard.after_unit()
            return stats

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pending: dict[Future, TaskRow] = {}
            it = pending_items()

            def submit_next() -> bool:
                if should_stop is not None and should_stop():
                    return False      # 停补新任务；在途的等它们自然收尾
                try:
                    task, data = next(it)
                except StopIteration:
                    return False
                if guard is not None:
                    _check_guard(guard)
                pending[pool.submit(client.process, task, data, reader)] = task
                return True

            for _ in range(concurrency):
                if not submit_next():
                    break

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    task = pending.pop(fut)
                    try:
                        outcome = fut.result()
                    except Exception as exc:      # 兜底：worker 里的 bug 不该中断整批
                        outcome = Outcome(task.image_id, False,
                                          error=f"内部异常: {type(exc).__name__}: {exc}")
                    commit(task, outcome)
                    # 让步按批结算：一个 worker 回来 ≈ 一个批次的边界
                    if guard is not None:
                        guard.after_unit()
                    submit_next()
        return stats
    finally:
        for fh in fail_holder:
            fh.close()


def _check_guard(guard: ResourceGuard) -> None:
    st = guard.before_unit()
    if not st.can_run:
        raise Stopped(st.describe())


# ------------------------------------------------------------------ 组装


def resolve_concurrency(value: int | None, fp: dict) -> int:
    """在途请求数。不填时跟服务端的 max_num_seqs 走。

    为什么默认不再是 1：vLLM 的吞吐全在连续批处理上，客户端一次只送一条时
    每次 batch 都只有一条，30M 量级下等于把机器闲置着跑。而盯着服务端的
    max_num_seqs 是唯一不需要人拍脑袋的锚点——它就是这个服务愿意同时处理
    多少条请求。

    上限压到 32：再多只是让请求在服务端排队，吞吐不变而延迟与内存都涨
    （每张图的原始字节还压在客户端手里）。真正的数用 `tagger bench` 量。
    """
    if value is not None:
        return max(1, int(value))
    try:
        seqs = int(fp.get("max_num_seqs") or 0)
    except (TypeError, ValueError):
        seqs = 0
    if seqs <= 0:
        # 指纹缺失（没跑过 serve，或跨机读取不到）：4 是保守值，
        # 但比 1 强得多，而且单卡 4B 上也确实装得下。
        return 4
    return max(1, min(seqs, 32))


def load_fingerprint(path: str | Path | None) -> dict:
    """读 `tagger serve` 写下的环境指纹。

    缺了也能跑，但 provenance 会残——而残缺的 provenance 意味着这批数据
    事后无法回答"在什么环境下产的"。所以缺的时候要显式提示一次，
    不能静默空着。
    """
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        print(f"[client][警告] 没找到环境指纹 {p}。"
              f"provenance 将不完整（建议先在起服务的那台机器上跑 `tagger serve`）。",
              file=sys.stderr)
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[client][警告] 指纹读不动（{exc}），provenance 将不完整。", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _prov_base(fp: dict, *, model_id: str, max_pixels: int, tier: str) -> dict:
    """Provenance 的固定部分。每张都带一份，几百字节换可追溯性，值。"""
    return {
        "model_id": model_id or str(fp.get("model_id") or "unknown"),
        "model_path": str(fp.get("model_path") or "unknown"),
        "model_revision": fp.get("model_revision"),
        "precision": str(fp.get("precision") or "unknown"),
        "max_pixels": int(max_pixels or fp.get("max_pixels") or 0),
        "vllm_version": fp.get("vllm_version"),
        "tier": tier or "",
        "device": str(fp.get("device") or ""),
    }


def _resize_to_budget(data: bytes, max_pixels: int) -> bytes:
    """把超过像素预算的图片等比缩小。不超预算原样返回（字节不动）。

    为什么客户端要做这件事：重试提分辨率（quality.retry_pixels_scale）的
    前提是"基线预算 < 服务端上限"，这时服务端只钳到上限，基线只能靠
    客户端自己执行。缩放算法与 pack 同一套口径：等比、alpha 铺白底、
    JPEG q95——落进包里的本来就该是这个形态。
    """
    from io import BytesIO

    from PIL import Image

    try:
        im = Image.open(BytesIO(data))
        im.load()
    except Exception:          # noqa: BLE001 - 解不开的图交给服务端去报错
        return data
    w, h = im.size
    if w <= 0 or h <= 0 or w * h <= max_pixels:
        return data
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        im = flat
    else:
        im = im.convert("RGB")
    scale = (max_pixels / (w * h)) ** 0.5
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    buf = BytesIO()
    im.resize((nw, nh), Image.LANCZOS).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _as_paths(paths) -> list:
    """统一成路径列表。

    CLI 的 --shard 是 nargs+（列表），而 Web 侧 JobSpec.input_dir 是一个字符串。
    直接 `for raw in "<路径>"` 会逐字符遍历，报出来的是"分发包不存在: D"——
    这个坑在这里堵掉，别指望每个调用方都记得套一层 list。
    """
    if isinstance(paths, (str, Path)):
        return [paths]
    return list(paths or [])


# 本项目 Packer 产出的 webdataset 后缀；tar.gz / tgz 也要认。
_WEBDS_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")


def _is_webdataset(p: Path) -> bool:
    return p.name.lower().endswith(_WEBDS_SUFFIXES)


def classify_local_inputs(paths) -> tuple[list[Path], list[Path]]:
    """把本地输入分成 (parquet 分片, webdataset 包)。

    目录按内容判：有 .parquet 就当 parquet 批，有 .zip/.tar 就当分发包。
    两者混在一起直接报错——它们的 image_id 口径和"整片完成"标记都不通用，
    拼一起只会得到一份对不上账的产物。
    """
    parquet: list[Path] = []
    shards: list[Path] = []
    for raw in _as_paths(paths):
        p = Path(raw)
        if p.is_dir():
            parquet.extend(sorted(p.glob("*.parquet")))
            shards.extend(sorted(q for q in p.iterdir()
                                 if q.is_file() and _is_webdataset(q)))
        elif p.is_file():
            if p.name.lower().endswith(".parquet"):
                parquet.append(p)
            elif _is_webdataset(p):
                shards.append(p)
            else:
                raise ConfigError(
                    f"不认识的分片格式: {p.name}"
                    f"（支持 .parquet / .zip / .tar / .tar.gz / .tgz）")
        else:
            raise ConfigError(f"分片不存在: {p}")
    if parquet and shards:
        raise ConfigError(
            "本地输入里同时有 parquet 和 zip/tar：两种格式的编号口径不同，"
            "请分两次跑（parquet 走流式，zip/tar 走解包）。")
    if not parquet and not shards:
        raise ConfigError(
            f"{paths} 下没有 .parquet / .zip / .tar 分片。"
            f"散图目录要先在「数据打包」页封包。")
    return parquet, shards


def collect_shards(paths: Sequence[str] | str) -> list[Path]:
    """展开 --shard：可以给包，也可以给装包的目录。"""
    out: list[Path] = []
    for raw in _as_paths(paths):
        p = Path(raw)
        if p.is_dir():
            found = sorted(p.glob("*.zip"))
            if not found:
                raise ConfigError(f"{p} 里没有 .zip 分发包")
            out.extend(found)
        elif p.is_file():
            out.append(p)
        else:
            raise ConfigError(f"分发包不存在: {p}")
    # 同一个包被写两遍（目录 + 具体文件）时去重，避免整包重打
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def load_tasks(shards: Sequence[Path], *, hf_repo: str = "") -> list[TaskRow]:
    """按 manifest 建任务表。

    同一个 image_id 出现在两个包里是分发错误（不是"两个人抢活"）：
    merge 会把它判成内容冲突。这里先发现、先报出来，别等交回去才炸。

    tar（webdataset）没有 manifest.jsonl，成员名本身就是清单：
    按「同 stem 不同后缀」归组，取图片成员当 file（见 hf_source.read_tar_samples）。
    """
    from . import hf_source

    tasks: list[TaskRow] = []
    seen: dict[str, str] = {}
    dups: list[str] = []

    for shard in shards:
        try:
            if str(shard).lower().endswith((".tar", ".tgz", ".tar.gz")):
                rows = hf_source.read_tar_samples(shard)
                if hf_repo:
                    rows = hf_source.merge_manifests(rows, repo=hf_repo,
                                                     shard=shard.name)
            else:
                rows = read_shard_manifest(shard)
        except (KeyError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            print(f"[client][警告] 读不动 {shard.name}: {exc}", file=sys.stderr)
            continue
        for row in rows:
            image_id = str(row.get("image_id") or "").strip()
            if not image_id:
                print(f"[client][警告] {shard.name} 有 manifest 行缺 image_id，已跳过",
                      file=sys.stderr)
                continue
            if image_id in seen:
                dups.append(f"{image_id}（{Path(seen[image_id]).name} / {shard.name}）")
                continue
            seen[image_id] = str(shard)
            tasks.append(TaskRow(
                shard=str(shard),
                image_id=image_id,
                file=str(row.get("file") or f"{image_id}{normalize_ext(image_id)}"),
                artist=str(row.get("artist") or ""),
                # rel_path 里存"镜像布局该走的路径"：优先 zip 内的原始 member，
                # 它才是训练侧认识的层级；散图没有 member 时退回 rel_path。
                rel_path=str(row.get("member") or row.get("rel_path") or ""),
                image_path=str(row.get("image_path") or ""),
            ))

    if dups:
        print(f"[client][警告] {len(dups)} 个 image_id 在多个包里重复出现，"
              f"只处理第一份（其余会导致 merge 判成内容冲突）：", file=sys.stderr)
        for d in dups[:10]:
            print(f"    {d}", file=sys.stderr)
        if len(dups) > 10:
            print(f"    … 其余 {len(dups) - 10} 个略", file=sys.stderr)

    # 按 image_id 排序：分包表本身就是这个序，顺序处理好让日志与产出一致，
    # 中断重跑时人的直觉也对得上（"我做到 leilei-003200 了"）。
    tasks.sort(key=lambda t: t.image_id)
    return tasks


def _filter_shards(names: list[str], wanted) -> list[str]:
    """按文件名挑分片（成员"只处理某个文件"的流程）。wanted 空 = 全部。

    精确匹配而不是 glob：文件名里的点在 glob 里是"任意字符"，`shard-1.parquet`
    会顺带命中 `shard-1xparquet` 这种名字。挑了但仓库里没有的，点名警告——
    静默少跑一片，对账时就是一片"整包没交"。
    """
    want = {str(w).strip() for w in (wanted or []) if str(w).strip()}
    if not want:
        return names
    hit = [n for n in names if n in want]
    missing = sorted(want - set(hit))
    if missing:
        print(f"[client][警告] 请求的分片里有 {len(missing)} 个在仓库中不存在："
              f"{', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}",
              file=sys.stderr)
    return hit


def run_client(args, ops: OpsConfig, table: dict,
               on_progress: ProgressHook | None = None, *,
               should_stop: Callable[[], bool] | None = None,
               on_log=None, guard: "ResourceGuard | None" = None) -> int:
    """CLI 入口的实体。参数解析留在 cli.py，这里只做组装与执行。

    `on_progress` 原样透传给 `run_batch`（Web 层靠它推实时进度）；
    CLI 不传，行为与以前完全一致。

    `should_stop`：协作式停止，原样透传给 `run_batch`；触发后 run_client
    返回码非 0，Web 层结合自己的停止标志区分"失败"与"已停止"。

    `on_log`：关键横幅/汇总同时回调给订阅方（Web 的日志事件流）。
    print 保持不变——终端里跑还是老样子，回调只是"多一份"。

    `guard`：外部预构造的 ResourceGuard（Web 侧带 on_state 事件接线）。
    传了就以它为准、由本函数负责 start/stop；不传且 args.no_guard 为假
    时按老路径自建。两个来源只会存在一个 watchdog。
    """

    def _log(msg: str) -> None:
        print(msg, flush=True)
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass      # 日志回调不该比任务本身更娇气

    defaults = table.get("defaults") or {}
    output = ops.output_spec()
    mode = str(args.mode or output.mode).lower()
    if mode not in ("nl", "tag", "both"):
        raise ConfigError(f"--mode 只能是 nl / tag / both，收到 {mode!r}")
    output = OutputSpec(mode=mode, nl_min_tokens=output.nl_min_tokens,
                        nl_max_tokens=output.nl_max_tokens,
                        tag_min_count=output.tag_min_count,
                        tag_max_count=output.tag_max_count)

    quality: QualityPolicy = ops.quality_policy()
    artist_rule: ArtistRule = ops.artist_rule()

    # AB 变体。这里只做"取值"，渲染放到 output 收窄之后——CPU tag 模式会把
    # output 改成 nl，而 prompt 必须按收窄后的规格生成，先渲染会得到一份
    # 要求模型吐 tag 的指令，白费 token 还污染产物。
    from .abtest import pick_variant

    variant_name = str(getattr(args, "variant", "") or "")
    variant = pick_variant(table.get("variants") or {}, variant_name) if variant_name else None

    # ---- 双线：CPU 出 tag，GPU 出 NL
    tagging = ops.tagging or {}
    backend_name = str(args.tag_backend or tagging.get("backend") or "pixai")
    need_tags = output.wants_tag
    # 模型名必须跟后端绑定：配置里只有一个 `model` 字段时，切后端就会把
    # pixai 的仓库名喂给 wd-eva02（实测直接报"不像 WD 仓库"）。
    # 所以优先查按后端分组的 models 表，单个 model 字段只作为老配置的兜底。
    models_map = {str(k): str(v) for k, v in (tagging.get("models") or {}).items()}
    model_for_backend = (args.tag_model
                         or models_map.get(backend_name)
                         or tagging.get("model"))
    # 设备：auto 走探测（有卡就 cuda:0），UI 上做成下拉框即可。
    tag_device = resolve_device(args.tag_device or tagging.get("device"))
    # 精度默认 tf32：79 张真实素材上实测 0 个 tag 翻面、概率最大漂移 0.0027，
    # 前向 1.33×。要逐位复现老口径就显式 --tag-precision fp32。
    tag_precision = str(getattr(args, "tag_precision", None)
                        or tagging.get("precision") or "tf32")
    tag_batch = int(getattr(args, "tag_batch", None) or tagging.get("batch") or 1)
    tag_backend = build_backend(
        backend_name,
        model=model_for_backend,
        model_dir=args.tag_model_dir,
        device=tag_device,
        keep=tagging.get("keep_categories"),
        thresholds={str(k): float(v) for k, v in (tagging.get("thresholds") or {}).items()},
        max_tags=output.tag_max_count,
        token=args.hf_token or os.environ.get("HF_TOKEN"),
        verbose=not args.quiet,
        precision=tag_precision,
        batch_size=tag_batch,
    )
    if tag_backend is not None:
        if not output.wants_nl:
            raise ConfigError(
                "--mode tag 不能配 CPU tag 后端：tag 由后端产出，VLM 就没有活干了。\n"
                "          用 --mode nl 或 both，或者 --tag-backend none 让 VLM 出 tag。")
        # 预加载：缺依赖、下不到模型、权重对不上，现在就说，
        # 别等跑到第 500 张才发现整批 tag 全是空的。
        try:
            tag_backend.load()
        except TaggerUnavailable as exc:
            raise ConfigError(f"tag 后端加载失败：{exc}") from exc

        # 实测（RTX 4090 主机 + 消费级 CPU）：pixai 1008²/486M 在 CPU 上
        # 约 16 s/张，wd-eva02 448²/eva02-large 约 2.3 s/张。而 GPU 上的
        # VLM 通常 1~3 s/张——用 pixai 跑 CPU，整条流水线会被它拖慢 5~8 倍，
        # "双线并行"退化成"CPU 串行 + GPU 空转"。
        if tag_backend.name == "pixai" and tag_device == "cpu":
            print("[client][警告] pixai 在 CPU 上实测约 16 秒/张，会是整条流水线的瓶颈。"
                  "\n          要跟上 GPU（1~3 秒/张）请换 --tag-backend wd-eva02"
                  "（约 2.3 秒/张，16473 词表）。",
                  file=sys.stderr)
        # VLM 收窄成只出 NL；tag_min/max 保留下来给数量校验用
        output = OutputSpec(mode="nl",
                            nl_min_tokens=output.nl_min_tokens,
                            nl_max_tokens=output.nl_max_tokens,
                            tag_min_count=output.tag_min_count,
                            tag_max_count=output.tag_max_count)

    # 变体可以带自己的生成上限与温度；命令行显式给了就以命令行为准。
    variant_max_tokens = variant.max_tokens if variant is not None else None
    max_tokens = int(args.max_tokens or variant_max_tokens
                     or defaults.get("max_tokens", 800))
    # 生成上限必须放得下"NL 全量 + tag 全量 + 两段标记"。放不下时的表现是
    # `<CAPTION>` 说到一半断掉或后面几十个 tag 直接消失——两种都不会报错，
    # 只会静默产出一条短数据。所以这里按最坏情况估一次，不够就抬。
    need_tokens = 64 + (output.nl_max_tokens if output.wants_nl else 0)
    if output.wants_tag:
        need_tokens += output.tag_max_count * 3      # 一个 danbooru tag ≈ 2~3 token
    if max_tokens < need_tokens:
        print(f"[client][警告] max_tokens={max_tokens} 放不下 "
              f"{output.tag_max_count} 个 tag + {output.nl_max_tokens} token 的 NL，"
              f"本次抬到 {need_tokens}（根治请改 configs/tiers.yaml 的 "
              f"defaults.max_tokens）。", file=sys.stderr)
        max_tokens = need_tokens

    temperature = float(args.temperature if args.temperature is not None
                        else (variant.temperature if variant is not None
                              and variant.temperature is not None
                              else defaults.get("temperature", 0.2)))

    # 变体渲染必须发生在调用 TaggerClient 之前、且在 output 收窄之后：
    # prompt 里的长度/数量约束是从 output 生成的，渲染得早会用错规格。
    prompt_text = variant.render_prompt(_prompt_for(output)) if variant is not None else None

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    fp = load_fingerprint(args.fingerprint)
    model_id = str(args.model_id or fp.get("model_id") or "")
    # 基线预算的取值顺序：命令行 > 配置 > 指纹。配置优先于指纹是刻意的：
    # "serve 时用更大的 --max-pixels 留出余量、客户端维持配置里的基线"
    # 正是重试提分辨率的打开方式——若指纹优先，基线永远等于服务端上限，
    # 重试就没有可抬的空间。
    base_pixels = int(args.max_pixels
                      or defaults.get("max_pixels", 0)
                      or fp.get("max_pixels") or 0)
    prov_base = _prov_base(
        fp, model_id=model_id,
        max_pixels=base_pixels,
        tier=str(args.tier or ""),
    )
    server_cap = int(fp.get("max_pixels") or 0)
    if (quality.retry_pixels_scale > 1 and server_cap
            and base_pixels and base_pixels >= server_cap):
        print(f"[client][警告] 基线预算 {base_pixels} ≥ 服务端上限 {server_cap}，"
              f"重试提分辨率不会生效（全被服务端钳回基线）。\n"
              f"          要启用：起服务时用更大的 --max-pixels 留余量"
              f"（如 {quality.retry_pixels_cap}），客户端维持配置默认。",
              file=sys.stderr)
    if variant_name:
        prov_base["variant"] = variant_name

    concurrency = resolve_concurrency(args.concurrency, fp)
    gate = QualityGate(quality, verbose=False)
    if quality.enable_clip:
        # 链路已经接通（图片在 _run 里解码后传给 gate），但打分模型是同一个
        # 实例上的前向，并发会互相踩权重与显存——锁只能保证不崩。所以并发
        # 摁到 1，并把代价说清楚：30M 量级下这是吞吐的生死线。
        if not gate.clip or not gate.clip.available:
            print(f"[client][警告] quality.enable_clip 已开启，但 "
                  f"{gate.clip.reason if gate.clip else 'CLIP 未初始化'}。"
                  f"本次将只跑 L1 规则层。", file=sys.stderr)
        elif concurrency > 1:
            print("[client][警告] CLIP 打分不是线程安全的，并发被强制改成 1。"
                  "启用 L2 会让整条流水线慢一个数量级，建议只用小批量抽检。",
                  file=sys.stderr)
            concurrency = 1

    stage = StageTimer()
    client = TaggerClient(
        base_url=args.url,
        model_name=args.model_name,
        output=output,
        temperature=temperature,
        top_p=float(args.top_p if args.top_p is not None else defaults.get("top_p", 0.9)),
        max_tokens=max_tokens,
        timeout_s=int(args.timeout or defaults.get("request_timeout_s", 90)),
        max_attempts=int(quality.max_attempts),
        gate=gate,
        prov_base=prov_base,
        artist_rule=artist_rule,
        use_file_url=bool(args.use_file_url),
        spool_dir=out_dir / ".spool",
        seed=args.seed,
        tag_backend=tag_backend,
        need_tags=need_tags,
        prompt_text=prompt_text,
        variant=variant_name,
        stage=stage,
        pixel_cap=server_cap,
        concurrency=concurrency,
        escalate_url=str(getattr(args, "escalate_url", "") or ""),
        escalate_model=str(getattr(args, "escalate_model", "") or ""),
    )

    stats = ClientStats()
    failures = Path(args.failures) if args.failures else out_dir / "failures.jsonl"
    layout = str(args.layout or (ops.distribute or {}).get("layout", "flat"))
    remaining = int(args.limit) if args.limit else 0

    # 产物形态。json = 每图两文件（默认，完成信号最直接）；
    # parquet = 攒成片（千万级时文件数降 4 个数量级，见 artifacts.py 的说明）。
    artifact_format = str(getattr(args, "artifact_format", "json") or "json").lower()
    sink = None
    done_ids: set[str] = set()
    if artifact_format == "parquet":
        from .artifacts import ParquetSink, load_done_ids

        sink = ParquetSink(out_dir, rows_per_file=int(
            getattr(args, "parquet_rows", 2000) or 2000))
        if not args.force:
            try:
                done_ids = load_done_ids(out_dir)
            except RuntimeError as exc:
                raise ConfigError(str(exc)) from exc
    elif artifact_format != "json":
        raise ConfigError(
            f"--artifact-format 只能是 json / parquet，收到 {artifact_format!r}")
    if sink is not None:
        extra = f"，已有 {len(done_ids)} 张将自动跳过" if done_ids else ""
        _log(f"[client] 产物形态 : parquet 分片，每 {sink.rows_per_file} 行一片{extra}")

    # 跨运行累计的失败账。同一张图失败到 max_failures 次就不再碰它，
    # 否则每次重跑都要为同一批坏样本重烧 3 次推理。
    max_failures = int(getattr(args, "max_failures", 3) or 0)
    fail_counts = load_failure_counts(failures)
    if fail_counts:
        black = sum(1 for n in fail_counts.values() if max_failures and n >= max_failures)
        _log(f"[client] 历史失败 : {len(fail_counts)} 个 id，"
             f"其中 {black} 个已达上限（本次跳过）")

    # 分片级完成标记：HF 模式下重跑时整片跳过。
    # 存在单独的 .hf-done 目录而不是产物目录根下，免得被 merge 的扫描扫到。
    done_dir = out_dir / ".hf-done"

    def shard_done(name: str) -> bool:
        return (done_dir / f"{Path(name).name}.done").is_file()

    def mark_shard_done(name: str) -> None:
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / f"{Path(name).name}.done").write_text(
            f"{name}\t{time.time():.0f}\n", encoding="utf-8")

    # guard 的磁盘检查盯产物目录：满盘时最先坏的是正在写的 json。
    # Web 传入的 guard 优先——它带 on_state 事件接线，自建会出现两个
    # watchdog 各自采样、各自熔断的鬼畜局面。
    if guard is None and not args.no_guard:
        guard = ResourceGuard(
            ops.guard_policy(),
            data_dir=out_dir,
            device_name=str(fp.get("device") or ""),
            verbose=True,
        )

    def run_group(group: list[Path], *, hf_repo_name: str = "") -> tuple[int, bool]:
        """处理一组包。本地模式一组，HF 模式一个 tar 一组（下完就删）。

        返回 (处理数, 是否被 --limit 截断)。截断的整片**不能**标记完成，
        否则片尾没跑的任务下次被整片跳过——丢片不报错，是最贵的那种 bug。
        """
        nonlocal remaining
        limit_total = int(getattr(args, "limit", 0) or 0)
        try:
            reader = ShardReader([str(s) for s in group])
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise ConfigError(
                f"打不开 {group[0].name}（{exc}）：文件可能没传完，重新下一份再跑。") from exc
        try:
            tasks = load_tasks(group, hf_repo=hf_repo_name)
            full_count = len(tasks)
            if remaining:
                tasks = tasks[:remaining]
                remaining -= len(tasks)
            if not tasks:
                return 0, bool(limit_total) and full_count > 0
            print(f"[client] 本批 : {group[0].name}  任务 {len(tasks)} 张", flush=True)
            run_batch(
                client, ((t, None) for t in tasks), reader,
                out_dir=out_dir,
                layout=layout,
                force=bool(args.force),
                concurrency=concurrency,
                guard=guard,
                stats=stats,
                failures=failures,
                verbose=not args.quiet,
                fail_counts=fail_counts,
                max_failures=max_failures,
                stage_timer=stage,
                sink=sink,
                done_ids=done_ids,
                on_progress=on_progress,
                should_stop=should_stop,
            )
            return len(tasks), bool(limit_total) and full_count > len(tasks)
        finally:
            reader.close()

    def _run_parquet_entries(entries: list[tuple[str, str]], *,
                             token: str | None = None,
                             shard_mark: bool = True) -> None:
        """流式打一批 parquet。entries = [(日志显示名, 数据源)]。

        数据源既可以是 `hf://datasets/<repo>/<file>`，也可以是本地绝对路径——
        `iter_parquet_samples` 两种都认，本地文件同样按 row group 流式读，
        不解包、不整表落盘。所以"本地 parquet"与"HF parquet"共用这一条路，
        跳过/失败账/产物写法全都一样。

        shard_mark：只有 HF 需要"整片完成"标记（省掉重复下载）。本地靠产物里的
        done_ids 逐行跳过就够；而且各目录的 pack-00000.parquet 会重名，
        拿文件名当标记键会让两批数据互相跳过。
        """
        nonlocal remaining
        img_col = str(getattr(args, "hf_image_column", "image") or "image")
        tpl = str(getattr(args, "hf_path_template", "{image_id}.jpg") or "{image_id}.jpg")
        _log(f"[client] 分片数 : {len(entries)}   (流式读取，不落盘)")
        _log(f"[client] 图片列 : {img_col}    image_path 模板 {tpl}")
        _log("")

        for idx, (name, uri) in enumerate(entries, 1):
            if remaining == 0 and getattr(args, "limit", None):
                break
            if shard_mark and not args.force and shard_done(name) and not args.limit:
                _log(f"[client] [{idx}/{len(entries)}] {name}  已完成，整片跳过")
                continue
            _log(f"[client] [{idx}/{len(entries)}] {name}")
            before_failed = stats.failed
            samples = hf_source.iter_parquet_samples(
                uri,
                image_column=img_col, token=token, repo="",
                shard=name, path_template=tpl,
            )
            counter = {"n": 0}
            truncated = {"v": False}

            def gen():
                for sample in samples:
                    if remaining and counter["n"] >= remaining:
                        # --limit 在这里把流掐断：这片没跑完，
                        # 调用方不能标记整片完成。
                        truncated["v"] = True
                        return
                    counter["n"] += 1
                    yield _hf_task(sample, name), sample["data"]

            run_batch(
                client, gen(), None,
                out_dir=out_dir,
                layout=layout,
                force=bool(args.force),
                concurrency=concurrency,
                guard=guard,
                stats=stats,
                failures=failures,
                verbose=not args.quiet,
                fail_counts=fail_counts,
                max_failures=max_failures,
                stage_timer=stage,
                sink=sink,
                done_ids=done_ids,
                on_progress=on_progress,
                should_stop=should_stop,
            )
            if remaining:
                remaining = max(0, remaining - counter["n"])
            # 只有"整片跑完且零失败"才标记。被 --limit 截断的片不标，
            # 否则片尾没跑的样本下次被整片跳过，永远静默丢失。
            if shard_mark and stats.failed == before_failed and not truncated["v"]:
                mark_shard_done(name)

    def run_parquet(repo: str, revision: str, token: str | None) -> None:
        """HF 仓库里的 parquet 分片：列文件、过滤，然后交给上面的流式入口。"""
        files = hf_source.list_parquet(repo, revision=revision, token=token)
        files = _filter_shards(files, getattr(args, "hf_files", None))
        if not files:
            raise ConfigError(
                f"{repo} 里没有可处理的 .parquet（仓库为空，或请求的文件名都不存在）。")
        max_shards = int(getattr(args, "hf_shards", 0) or 0)
        if max_shards:
            files = files[:max_shards]
        _run_parquet_entries(
            [(name, f"hf://datasets/{repo}/{name}") for name in files], token=token)

    _log(f"[client] 目标服务 : {client.base_url}  模型名 {args.model_name}")
    spec_note: list[str] = []
    if output.wants_nl:
        spec_note.append(f"NL {output.nl_min_tokens}~{output.nl_max_tokens} token")
    if need_tags:
        spec_note.append(f"tag {output.tag_min_count}~{output.tag_max_count} 个"
                         f"（{tag_backend.name if tag_backend else 'VLM'}）")
    _log(f"[client] 产出模式 : {output.mode}  " + "  ".join(spec_note))
    if variant is not None:
        note = "整段替换" if variant.prompt_file else ("追加说明" if variant.prompt_note else "默认指令")
        _log(f"[client] AB 变体  : {variant.name}（{note}）")
        if variant.note:
            _log(f"           {variant.note}")
    _log(f"[client] 产物目录 : {out_dir.resolve()}")
    _log(f"[client] 并发     : {concurrency}    "
         f"model_id {prov_base['model_id'] or '(未知)'}")
    # 两条线的设备分开显示：NL 那侧由服务端的 vLLM 决定（客户端改不了），
    # tag 这侧才是本机参数。混在一起时，用户会以为改 --tag-device 能挪动 LLM。
    _log(f"[client] NL 设备  : {fp.get('device') or '(由服务端 vLLM 决定)'}")
    if tag_backend:
        _log(f"[client] tag 设备 : {tag_device}（{tag_backend.precision}"
             f"{f'，batch={tag_backend.batch_size}' if tag_backend.batch_size > 1 else ''}）")
    else:
        _log("[client] tag 设备 : 未启用（tag 由 VLM 出）")
    _log("")

    from . import hf_source

    hf_repo = str(getattr(args, "hf_repo", "") or "")
    hf_cache = Path(getattr(args, "hf_cache", "") or (out_dir / ".hf-cache"))

    code = 0
    started = time.time()
    try:
        if guard is not None:
            guard.start()
            # 守护线程的首轮采样是异步的，不等它的话，开跑后前几秒读到的还是
            # 初始的 ok。真实原因是"磁盘已经不足却照样开始写"——这类问题越早
            # 拦住越便宜，所以这里同步采一次，读数还能顺手打给人看。
            pre = guard.watchdog.sample_once()
            print(f"[guard] {pre.describe()}")
            if pre.level == "stop":
                raise Stopped(pre.describe())

        if hf_repo:
            token = args.hf_token or os.environ.get("HF_TOKEN")
            revision = str(getattr(args, "hf_revision", "main") or "main")
            want_fmt = str(getattr(args, "hf_format", "auto") or "auto").lower()
            fmt = want_fmt
            if fmt == "auto":
                fmt = hf_source.detect_format(hf_repo, revision=revision, token=token)
                if not fmt:
                    raise ConfigError(
                        f"{hf_repo} 里既没有 *.parquet 也没有 *.tar。"
                        f"确认仓库类型是 dataset，且文件已经上传完。")
            _log(f"[client] HF 仓库 : {hf_repo}   格式 {fmt}")

            if fmt == "parquet":
                run_parquet(hf_repo, revision, token)
            else:
                names = hf_source.list_shards(
                    hf_repo, revision=revision,
                    pattern=str(getattr(args, "hf_pattern", "*.tar") or "*.tar"),
                    token=token,
                )
                names = _filter_shards(names, getattr(args, "hf_files", None))
                if not names:
                    raise ConfigError(
                        f"{hf_repo} 里没有可处理的 .tar 分片"
                        f"（仓库为空，或请求的文件名都不存在）。")
                max_shards = int(getattr(args, "hf_shards", 0) or 0)
                if max_shards:
                    names = names[:max_shards]

                _log(f"[client] 分片数 : {len(names)}")
                _log(f"[client] 临时目录: {hf_cache}  "
                     f"（一次只落一个分片，处理完即删）")
                _log("")

                for idx, name in enumerate(names, 1):
                    if not args.force and shard_done(name) and not args.limit:
                        _log(f"[client] [{idx}/{len(names)}] {name}  已完成，整片跳过")
                        continue
                    _log(f"[client] [{idx}/{len(names)}] {name}")
                    shard = hf_source.download_shard(
                        hf_repo, name, hf_cache, token=token, revision=revision,
                        verbose=not args.quiet,
                    )
                    before_failed = stats.failed
                    try:
                        _, truncated = run_group([shard.path], hf_repo_name=hf_repo)
                    finally:
                        hf_source.cleanup(shard,
                                          keep=bool(getattr(args, "hf_keep", False)))
                    # 只有整片跑完且零失败才标记。有失败别标（坏样本不能被
                    # 藏起来）；被 --limit 截断也别标（片尾还没跑）。
                    if stats.failed == before_failed and not truncated:
                        mark_shard_done(name)
                    if remaining == 0 and getattr(args, "limit", None):
                        break
        else:
            if not args.shard:
                raise ConfigError(
                    "要么给 --shard <分片>（本地 parquet / zip / tar），"
                    "要么给 --hf-repo <org/name>（仓库）。")
            # 本地两种格式走两条路：parquet 流式读、zip/tar 解包读。
            # 判定交给 classify_local_inputs（同时把"混着装"拦下来）。
            parquet_files, shard_files = classify_local_inputs(args.shard)
            if parquet_files:
                _log(f"[client] 本地 parquet : {len(parquet_files)} 片")
                _run_parquet_entries(
                    [(p.name, str(p.resolve())) for p in parquet_files],
                    shard_mark=False,
                )
                if not stats.total:
                    raise ConfigError(
                        "这些 parquet 里没有可处理的行：确认有 image 列（图片字节）"
                        "与 image_id 列。")
            else:
                total, _ = run_group(shard_files)
                if not total:
                    print("[client] 分发包里没有任务。检查 --shard 是否指对了包。")
                    code = 1

    except hf_source.HFError as exc:
        print(f"\n[client] HF 访问失败：{exc}", file=sys.stderr)
        code = 1
    except Stopped as exc:
        print(f"\n[client] 已停止：{exc}", file=sys.stderr)
        print("[client] 阈值在 configs/tiers.yaml 的 limits 段"
              "（disk_min_free_gb / temp_cutoff_c）。", file=sys.stderr)
        print("[client] 产物已写盘，修好之后直接重跑同一条命令即可续做"
              "（已完成的会自动跳过；在途的几张没写产物，会被重做）。", file=sys.stderr)
        code = 1
    finally:
        if guard is not None:
            guard.stop()
        # sink 必须在这里最后 flush 一次：末片可能不满 rows_per_file，
        # 不 flush 就等于把最后几百张悄悄丢了（而且"没报错"）。
        if sink is not None:
            try:
                sink.close()
            except Exception as exc:      # noqa: BLE001 - 落盘失败要显式报出来
                print(f"[client][写入失败] 最后一片 parquet 没落成: {exc}",
                      file=sys.stderr)
                code = 1
        client.close()

    elapsed = time.time() - started
    _log("")
    for line in stats.summary_lines():
        _log("  " + line)
    _log(f"  总耗时     : {elapsed / 60:.1f} 分钟")
    if stats.done and elapsed > 0:
        _log(f"  吞吐       : {stats.done / elapsed:.2f} 张/秒")
    # ---- 分段计时。默认不落盘，只在 --stats-json 时写。
    # 为什么不总是打印：这些数字对调优有用，对日常跑批只是噪音；
    # 而"顺手多打几行"的习惯会让真正的告警被淹没。
    stage_snapshot = stage.snapshot()
    if stage_snapshot:
        print()
        print("  分段计时   :")
        for name in StageTimer.STAGES:
            row = stage_snapshot.get(name)
            if row:
                print(f"    {name:<6} {row['avg_ms']:>8.2f} ms/次  "
                      f"共 {row['count']:>6} 次  累计 {row['total_ms'] / 1000:>7.2f}s")
        print("    （tag 与 http 是并行跑的，各项累计之和会超过墙钟）")

    if args.stats_json:
        stats_path = Path(args.stats_json)
        try:
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            stats_path.write_text(json.dumps({
                "schema": 1,
                "ts": time.time(),
                "elapsed_s": round(elapsed, 2),
                "mode": output.mode,
                "variant": variant_name,
                "concurrency": concurrency,
                "model_id": prov_base.get("model_id"),
                "tag_backend": tag_backend.name if tag_backend else None,
                "tag_precision": tag_backend.precision if tag_backend else None,
                "tag_batch": tag_backend.batch_size if tag_backend else None,
                "tag_graph": bool(getattr(tag_backend, "_graph", None)) if tag_backend else None,
                "counts": {
                    "total": stats.total, "done": stats.done,
                    "failed": stats.failed, "skipped": stats.skipped,
                    "blacklisted": stats.blacklisted,
                },
                "throughput_per_s": round(stats.done / elapsed, 3) if elapsed else 0.0,
                "stages": stage_snapshot,
                "note": "各阶段不互斥（tag 与 http 并行），total_ms 相加会超过墙钟；"
                        "看 avg_ms 与 count 判断该优化哪一段",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[client] 计时写入 {stats_path}")
        except OSError as exc:
            print(f"[client][警告] 计时文件写不了 {stats_path}: {exc}", file=sys.stderr)

    if stats.failed:
        print()
        print(f"[client] {stats.failed} 张失败，明细在 {failures}")
        print("         失败行不会写产物文件，所以重跑时会被自动重做——"
              "先把原因修掉再跑，别让它们一直循环。")
    if code == 0 and stats.failed:
        code = 1

    if code == 0:
        print()
        print("[client] 下一步：把整个产物目录交回总机，总机跑 "
              "`tagger merge --roots <目录> --shard-dir <分发包目录>` 对账合并。")
    return code
