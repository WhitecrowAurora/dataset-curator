"""本地打标 UI 的 HTTP / WebSocket 层。

三个刻意的选择：

1. **进度走 WebSocket，同时留 REST 轮询。** 前端先用 `connect()` 开 WS，
   断开就退回每秒拉一次 `/events?after=` ——同一份数据、同一条 cursor。
   本地服务其实很少断，但"页面卡住不刷新"是最难自证的故障，留个兜底能省掉
   一次"是不是你网不好"的扯皮。
2. **不缓存产物。** 结果页每次都从 `<out_dir>/tags/*.json` 现读。产物是唯一
   真相（用户可能在别的终端手删/手改），缓存一份只会让页面与磁盘互相打脸。
   分页一次最多几十个文件，代价可以忽略。
3. **缩略图在后端缩。** 原图 1024² 约 1 MB，一页 40 张就是 40 MB，浏览器
   还要各自解码。这里按 `?w=` 缩成 JPEG 再发，并按 (路径, 宽度, mtime)
   缓存在内存里——第一次滚动稍慢，之后是本地数据库级别的响应。
"""

from __future__ import annotations

import contextlib
import io
import platform
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import ConfigError
from ..local_tag import EXTS
from ..pipeline import plan_inputs, plan_stats
from .jobs import MANAGER, STAT_FINE, Job, JobSpec
from .uploads import UPLOADS, parquet_rows
from .dedup import DEDUPS
from .packaging import DATAJOBS
from .vllm_service import VLLM

UI_DIR = Path(__file__).resolve().parent / "ui"
DIST_DIR = UI_DIR / "dist"


class JobRequest(BaseModel):
    # 与 jobs.JobSpec 一一镜像（create 里 JobSpec(**req.model_dump())）。
    # 加字段必须两边同改，漏一边就是"表单发了、任务收不到"的静默丢参。
    # ---- 输入。vlm 引擎走 HF 仓库时可为空，本地引擎必填（create 校验）
    input_dir: str = ""
    out_dir: str = ""
    recursive: bool = True
    include_zips: bool = True
    include_loose: bool = True
    limit: int = 0
    force: bool = False

    # ---- 引擎。local = 本地 tag 后端；vlm = vLLM 出 NL（tag 可由本地后端并行出）
    engine: str = "local"

    # ---- 打标
    backend: str = "wd-eva02"
    model_dir: str = ""
    device: str = "auto"
    precision: str = "bf16"
    batch: int = 4
    threshold: float | None = None
    # tag 后端补充（两种引擎通用）
    keep_categories: list[str] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)
    model_repo: str = ""
    guard: bool = True
    # 开跑前/封包中按"预计产出 + 20% 冗余"判盘够不够（取代 guard 的固定阈值）
    check_space: bool = True

    # ---- VLM 服务（engine="vlm"）
    url: str = "http://127.0.0.1:8000"
    vlm_model: str = "tagger"
    # both = NL + tag；nl = 只要 NL；tag = 只要 tag（单模式都由 VLM 出，本地后端强制 none）
    output_mode: str = "both"
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 0
    timeout_s: int = 90
    concurrency: int = 2
    max_pixels: int = 0
    seed: int | None = None
    # 同一张图累计失败多少次后跳过（0 = 不限制）
    max_failures: int = 3
    # 质检判定线与重打次数（0 = 用配置：pass 3.4 / drop 1.8 / 重打 2 次）
    quality_pass_score: float = 0.0
    quality_drop_score: float = 0.0
    quality_max_attempts: int = 0

    # ---- HF 输入（engine="vlm"）
    hf_repo: str = ""
    hf_token: str = ""
    hf_revision: str = "main"
    hf_files: list[str] = Field(default_factory=list)

    # ---- 产物形态（vlm 引擎：parquet 回传给汇总机）
    artifact_format: str = "parquet"
    parquet_rows: int = 2000

    # ---- 命名
    artist: bool = True
    artist_depth: int = 0
    # 画师名写进 .tag/.nl 文本开头（false = 只留在 JSON 字段里）
    artist_inject: bool = True
    layout: str = "flat"
    seq_width: int = 6

    # ---- 封包
    pack: bool = False
    formats: list[str] = Field(default_factory=lambda: ["parquet"])
    jpeg_quality: int = 95
    jpeg_subsampling: int = 0
    reencode: str = "always"
    compress: str = "stored"
    max_records: int = 5000
    max_bytes: int = 0


class FtpRequest(BaseModel):
    """回传页右侧面板。密码只在内存里走一趟（建 UploadJob），不落盘不进事件。"""
    host: str
    port: int = 21
    user: str = "anonymous"
    password: str = ""
    remote_dir: str = "/"
    passive: bool = True


class UploadRequest(BaseModel):
    dir: str
    files: list[str] = Field(default_factory=list)
    ftp: FtpRequest


class DedupRequest(BaseModel):
    """去重页表单。素材只读，产出落在 <输入名>-dedup/（或显式 out_dir）。"""
    input_dir: str
    out_dir: str = ""
    mode: str = "similar"
    threshold: int = 8
    keep: str = "first"
    recursive: bool = True
    workers: int = 8


class DataJobStartRequest(BaseModel):
    """数据打包（分包/拼接/封包）表单。params 字段按 kind 各自校验。"""
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class DataPreviewRequest(BaseModel):
    """分包预览：只扫路径结构，不读图片字节。"""
    kind: str = "split"
    input_dir: str
    exclude: str = ""
    recursive: bool = True
    limit: int = 0


class VllmStartRequest(BaseModel):
    """本机拉起 vLLM。端口改成非默认值时，任务表单里的服务地址要一起改。

    model 选权重（None = 按显存落档）；served_name 是接口路由名，必须与任务
    表单里的「模型名」一致。并发三项留空 = 用分档表的值。它们只管服务端批
    调度；任务侧的 `concurrency` 是另一回事（客户端在途请求数），两者要一起调。
    """
    port: int = 8000
    timeout_s: int = 900
    model: str | None = None
    tier: str | None = None
    served_name: str | None = None
    max_num_seqs: int | None = None
    max_num_batched_tokens: int | None = None
    gpu_mem: float | None = None


# ---------------------------------------------------------------- 环境信息

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _gpu_lines() -> list[str]:
    """显卡信息。查不到就返回空表——**不报错**：这个接口是给页面顶部
    "有没有卡"用的，不是启动门槛。"""
    try:
        from ..hardware import describe, query_gpus

        return [describe(g) for g in query_gpus()]
    except Exception:                 # noqa: BLE001 - nvidia-smi 不在 PATH 之类
        return []


def _defaults() -> dict:
    """表单默认值。能读配置就读，读不到用代码里的兜底，绝不因此报错。"""
    tagging: dict[str, Any] = {}
    archive: dict[str, Any] = {}
    artist: dict[str, Any] = {}
    raw_models: dict[str, Any] = {}
    raw_tiers: list[dict[str, Any]] = []
    try:
        from ..config import DEFAULT_TIERS, load_ops, load_tier_table

        # 之前传的是 None：Path(None) 抛 TypeError 被 except 吞掉，
        # 页面上的默认值从来没读到过配置，一直显示代码兜底。
        table = load_tier_table(DEFAULT_TIERS)
        ops = load_ops(table)
        tagging = dict(ops.tagging or {})
        archive = dict(ops.archive or {})
        artist = dict(ops.artist or {})
        raw_models = dict(table.get("models") or {})
        raw_tiers = list(table.get("tiers") or [])
    except Exception:                 # noqa: BLE001 - 配置文件坏了不该连页面都打不开
        tagging = {}
        archive = {}
        artist = {}
        raw_models = {}
        raw_tiers = []
    models_dir = _repo_root() / "models"
    wd = models_dir / "wd-eva02"
    formats = archive.get("formats") or ["parquet"]
    if isinstance(formats, str):
        formats = [formats]
    # VLM 可选权重：分档表 models 段就是唯一真相源。页面上的下拉必须由它
    # 生成，否则改了配置页面还是那几个写死的名字（之前"换模型只能去命令行"
    # 就是这么来的）。tier 只是给个提示，真正落在哪档由 resolve_profile 定。
    # 一个模型可能被多个档位引用（4B 同时是 low-16g 和 mid-24g）。提示只取
    # 表里出现的第一个（分档表按显存升序写），否则会显示成"4B 是 24G 档"。
    tier_of: dict[str, str] = {}
    for t in raw_tiers:
        if isinstance(t, dict) and t.get("name"):
            tier_of.setdefault(str(t.get("model")), str(t.get("name")))
    vllm_models = [
        {
            "key": str(key),
            "path": str(spec.get("path") or ""),
            "precision": str(spec.get("precision") or "int4"),
            "family": str(spec.get("family") or ""),
            "tier": tier_of.get(str(key), ""),
        }
        for key, spec in raw_models.items()
        if isinstance(spec, dict)
    ]
    # 分档决定 max_model_len / max_num_seqs / 支持的精度，和"用哪套权重"是两个
    # 正交的轴：24G 卡上想跑 4B 也可以强行留在 low-16g（上下文更短、KV 更省）。
    vllm_tiers = [
        {
            "name": str(t.get("name") or ""),
            "model": str(t.get("model") or ""),
            "vram_min": float(t.get("vram_min") or 0),
            "vram_max": float(t.get("vram_max") or 999),
            "max_model_len": int(t.get("max_model_len") or 8192),
            "max_num_seqs": int(t.get("max_num_seqs") or 8),
            "note": str(t.get("note") or ""),
        }
        for t in raw_tiers
        if isinstance(t, dict) and t.get("name")
    ]
    return {
        "backend": str(tagging.get("backend") or "wd-eva02"),
        "model_dir": str(wd) if wd.is_dir() else "",
        "models_dir": str(models_dir) if models_dir.is_dir() else "",
        "device": str(tagging.get("device") or "auto"),
        "precision": "bf16",
        "batch": int(tagging.get("batch") or 4),
        "threshold": float(((tagging.get("thresholds") or {}).get("default")) or 0.35),
        "keep_categories": list(tagging.get("keep_categories") or []),
        # 各 tag 后端在配置里绑定的 HF 仓库（按后端分组，切换后端时才不会拿错）。
        "tag_models": {str(k): str(v) for k, v in (tagging.get("models") or {}).items()},
        "vllm_models": vllm_models,
        "vllm_tiers": vllm_tiers,
        # ---- 封包/命名默认值（代码兜底与 tiers.yaml 的 archive 段同源）
        "pack": True,
        "formats": [str(f) for f in formats],
        "layout": "flat",
        "jpeg_quality": int(archive.get("jpeg_quality") or 95),
        "jpeg_subsampling": int(archive.get("jpeg_subsampling") or 0),
        "reencode": str(archive.get("reencode") or "always"),
        "compress": str(archive.get("compress") or "stored"),
        "max_records": int(archive.get("output_max_records") or 5000),
        "max_bytes": int(archive.get("output_max_bytes") or 0),
        # 默认从目录第一层取画师名：封包命名靠它（`画师-000123`）。
        # 注意这里不看 artist.enabled——那个开关管的是"注入 caption"，
        # 与"是否把画师名放进 id"是两件事，混用会让用户说不清自己选了什么。
        "artist": True,
        "artist_depth": int(artist.get("depth") or 0),
        # 注入默认跟配置走：这是"画师名写不写进 .tag/.nl 文本"的开关
        "artist_inject": bool(artist.get("inject_caption", True)),
        "artist_prefix": str(artist.get("prefix") or "@"),
        "seq_width": 6,
    }


# ---------------------------------------------------------------- 缩略图缓存

class _ThumbCache:
    """(路径, 宽度, mtime) → JPEG 字节。上限 400 张，约 8~16 MB。"""

    def __init__(self, limit: int = 400) -> None:
        self._limit = limit
        self._data: OrderedDict[tuple, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            data = self._data.get(key)
            if data is not None:
                self._data.move_to_end(key)
            return data

    def put(self, key: tuple, data: bytes) -> None:
        with self._lock:
            self._data[key] = data
            self._data.move_to_end(key)
            while len(self._data) > self._limit:
                self._data.popitem(last=False)


THUMBS = _ThumbCache()


def _thumbnail(path: Path, width: int) -> bytes:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > width:
            height = max(1, round(im.height * width / im.width))
            im = im.resize((width, height), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()


def _thumbnail_bytes(data: bytes, width: int) -> bytes:
    """与 _thumbnail 同一套口径，只是源是内存里的字节（zip 成员）。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        if im.width > width:
            height = max(1, round(im.height * width / im.width))
            im = im.resize((width, height), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()


# ---------------------------------------------------------------- 目录浏览

def _list_dirs(path: Path) -> dict:
    """给"选输入"用的极简浏览器。子目录 + 当前层图片/zip 数量。

    zip 单独计数并单独列出：4TB 级素材常见形态就是"一个目录里几个大包"，
    只让用户选目录、却把包当成不存在，等于让他自己猜路径。
    """
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except PermissionError as exc:
        raise HTTPException(403, f"没有权限读 {path}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"路径不存在: {path}") from exc
    dirs = []
    zips = []
    for entry in entries:
        if entry.name.startswith((".", "$")):
            continue
        if entry.is_dir():
            images = 0
            try:
                images = sum(1 for p in entry.glob("*") if p.suffix.lower() in EXTS)
            except OSError:
                images = 0
            inner_zips = 0
            try:
                inner_zips = sum(1 for p in entry.glob("*")
                                 if p.suffix.lower() == ".zip")
            except OSError:
                inner_zips = 0
            dirs.append({"name": entry.name, "path": str(entry), "images": images,
                         "zips": inner_zips})
        elif entry.is_file() and entry.suffix.lower() == ".zip":
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            zips.append({"name": entry.name, "path": str(entry), "bytes": size})
    own = 0
    try:
        own = sum(1 for p in path.glob("*") if p.suffix.lower() in EXTS)
    except OSError:
        own = 0
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else "",
        "images": own,
        "zips": zips,
        "dirs": dirs,
    }


def _start_path(raw: str) -> Path:
    """把前端传来的路径解释成一个**目录**。

    输入本身可以是文件（.zip / 单张图），浏览时落在它所在的目录——用户
    想看的是"包里还有个什么"，不是"这个包叫什么"。
    """
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
        if p.exists() or p.suffix:
            return p.parent if p.parent != p else p
        return p
    for candidate in (Path.home() / "Pictures", Path.home(), _repo_root()):
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def _artist_context_default(enabled: bool, depth: int, inject: bool = True):
    """预览接口用的画师上下文。与任务执行时同一份配置口径。"""
    from .jobs import _artist_context

    spec = JobSpec(input_dir="", artist=enabled, artist_depth=depth,
                   artist_inject=inject)
    return _artist_context(spec)


# ---------------------------------------------------------------- 应用

def create_app(*, ui_dist: Path | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Web 进程退出时收掉自己拉起的 vllm 子进程。不收的话它会继续占着
        # 显存（子进程在自己的进程组里，Ctrl+C 打不到），下次启动报显存不足，
        # 而人只会觉得"这机器有问题"。只收本进程拉起的：命令行 `tagger serve`
        # 起的服务不受影响。
        try:
            if VLLM.snapshot()["status"] != "stopped":
                VLLM.stop()
        except Exception:               # noqa: BLE001 - 退出路径上不值得再报错
            pass

    app = FastAPI(title="Tagger UI", version="0.1.0", docs_url="/api/docs",
                  lifespan=lifespan)
    dist = Path(ui_dist) if ui_dist else DIST_DIR

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "version": "0.1.0",
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.release()}",
            "gpus": _gpu_lines(),
            "ui_built": (dist / "index.html").is_file(),
            "running_job": (MANAGER.running().id if MANAGER.running() else None),
            "ts": time.time(),
        }

    @app.get("/api/defaults")
    def defaults() -> dict:
        return _defaults()

    @app.get("/api/fs")
    def fs(path: str = "") -> dict:
        return _list_dirs(_start_path(path))

    @app.get("/api/preview")
    def preview(path: str = "", layout: str = "flat", artist: bool = True,
                artist_depth: int = 0, recursive: bool = True,
                include_zips: bool = True, include_loose: bool = True,
                limit: int = 0, seq_width: int = 6, pack: bool = True,
                out_dir: str = "", formats: str = "", jpeg_quality: int = 0,
                jpeg_subsampling: int = -1, reencode: str = "") -> dict:
        """按当前参数预演"会发现什么、叫什么名、要写多少盘"。

        这是新建任务页最要紧的一块：命名规则改一个字符、画师开关拨一下，
        用户就该立刻看到 `artist-000017.jpg` 变成 `subdir/0001.png`。等到
        跑完几百万张才发现命名不对，代价是整批重来。盘位同理——写到爆盘
        才发现装不下，损失的是已经花掉的那几个小时 GPU。

        `pack` 必须传：只打标的任务沿用相对路径当 id（历史产物兼容），
        命名口径和封包任务不同；预览若按封包口径演，展示的名字就与实际
        产物对不上。

        `formats` 是逗号分隔的格式列表（如 `parquet,zip`）；不传就只按
        打标产物的体量估算。只扫不读图，也不加载模型。
        """
        if not path:
            raise HTTPException(400, "缺少 path")
        options = None
        if formats:
            try:
                from ..pack import PackOptions

                defaults = _defaults()
                options = PackOptions.from_dict({
                    "formats": [f.strip() for f in formats.split(",") if f.strip()],
                    "jpeg_quality": jpeg_quality or defaults["jpeg_quality"],
                    "jpeg_subsampling": (defaults["jpeg_subsampling"]
                                         if jpeg_subsampling < 0 else jpeg_subsampling),
                    "reencode": reencode or defaults["reencode"],
                })
            except ConfigError as exc:
                raise HTTPException(400, str(exc)) from exc
        try:
            rule, aliases = _artist_context_default(artist, artist_depth)
            plan = plan_inputs(
                path, recursive=recursive, include_zips=include_zips,
                include_loose=include_loose, limit=max(0, limit),
                # 与 JobManager.create 同一口径：不封包就走旧 id
                assign_ids=pack, layout=layout if pack else "flat",
                seq_width=seq_width, artist_rule=rule, aliases=aliases,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (ValueError, NotADirectoryError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:          # noqa: BLE001 - 坏包/权限都翻成 400
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        return plan_stats(plan, options=options, out_dir=out_dir or None)

    # ---- 任务

    @app.post("/api/jobs", status_code=202)
    def create_job(req: JobRequest) -> dict:
        spec = JobSpec(**req.model_dump())
        try:
            job = MANAGER.create(spec)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ConfigError as exc:
            # 封包参数非法（质量越界、格式拼错）是用户输入问题，不是服务器故障
            raise HTTPException(400, str(exc)) from exc
        except (ValueError, NotADirectoryError) as exc:
            raise HTTPException(400, str(exc)) from exc
        MANAGER.start(job)
        return job.snapshot()

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": [j.snapshot() for j in MANAGER.all()[:20]]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = MANAGER.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/events")
    def get_events(job_id: str, after: int = 0) -> dict:
        """WS 的兜底：同一份数据、同一条 cursor。"""
        job = MANAGER.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        return {"events": job.events_since(after), "snapshot": job.snapshot()}

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str) -> dict:
        if not MANAGER.stop(job_id):
            raise HTTPException(409, "这个任务已经结束或不存在")
        job = MANAGER.get(job_id)
        return {"ok": True, "snapshot": job.snapshot() if job else None}

    @app.get("/api/jobs/{job_id}/results")
    def job_results(job_id: str, offset: int = 0, limit: int = 40,
                    q: str = "", only_failed: bool = False) -> dict:
        job = MANAGER.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        return job.results(offset=offset, limit=min(200, max(1, limit)),
                           query=q, only_failed=only_failed)

    @app.get("/api/jobs/{job_id}/stats")
    def job_stats(job_id: str, bucket: int = 1000) -> dict:
        """统计页：完成顺序分桶的通过/失败/质检分趋势。

        bucket 必须是细桶宽（100）的倍数——累加只有这一档，其余靠合并。
        """
        if bucket < STAT_FINE or bucket > 50000 or bucket % STAT_FINE:
            raise HTTPException(400, f"bucket 必须是 {STAT_FINE}~50000 之间 {STAT_FINE} 的倍数")
        job = MANAGER.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        return job.stats_buckets(bucket)

    # ---- 回传（FTP 上传）与 HF 文件清单

    @app.get("/api/artifacts")
    def artifacts(path: str = "") -> dict:
        """列出目录下的 parquet 分片。rows 从 footer 元数据读（O(1)），
        就是"这片里有多少张处理好的图"——ParquetSink 只写成功行，
        行数即成品数。读不动的文件 rows=None，页面显示 "-"，不挡列表。"""
        if not path:
            raise HTTPException(400, "缺少 path")
        base = Path(path).expanduser()
        if not base.is_dir():
            raise HTTPException(404, f"目录不存在: {path}")
        files = []
        try:
            entries = sorted(base.glob("*.parquet"), key=lambda p: p.name.lower())
            for p in entries:
                try:
                    st = p.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                files.append({"name": p.name, "bytes": size, "mtime": mtime,
                              "rows": parquet_rows(p)})
        except OSError as exc:
            raise HTTPException(403, f"读不了 {path}: {exc}") from exc
        return {"path": str(base), "files": files}

    @app.post("/api/upload/start", status_code=202)
    def upload_start(req: UploadRequest) -> dict:
        try:
            job = UPLOADS.create(req.dir, req.files, host=req.ftp.host,
                                 port=req.ftp.port, user=req.ftp.user,
                                 password=req.ftp.password,
                                 remote_dir=req.ftp.remote_dir,
                                 passive=req.ftp.passive)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.snapshot()

    @app.get("/api/upload/{job_id}")
    def upload_status(job_id: str) -> dict:
        job = UPLOADS.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这次回传")
        return job.snapshot()

    @app.post("/api/upload/{job_id}/stop")
    def upload_stop(job_id: str) -> dict:
        if not UPLOADS.stop(job_id):
            raise HTTPException(409, "这次回传已结束或不存在")
        job = UPLOADS.get(job_id)
        return {"ok": True, "snapshot": job.snapshot() if job else None}

    @app.post("/api/dedup/start", status_code=202)
    def dedup_start(req: DedupRequest) -> dict:
        try:
            job = DEDUPS.create(
                input_dir=req.input_dir, out_dir=req.out_dir, mode=req.mode,
                threshold=req.threshold, keep=req.keep,
                recursive=req.recursive, workers=req.workers)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.snapshot()

    @app.get("/api/dedup/{job_id}")
    def dedup_status(job_id: str) -> dict:
        job = DEDUPS.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这次去重")
        return job.snapshot()

    @app.post("/api/dedup/{job_id}/stop")
    def dedup_stop(job_id: str) -> dict:
        if not DEDUPS.stop(job_id):
            raise HTTPException(409, "这次去重已结束或不存在")
        job = DEDUPS.get(job_id)
        return {"ok": True, "snapshot": job.snapshot() if job else None}

    # ---- 数据打包（分包 / 拼接 / 封包）---------------------------------
    # 与打标/去重/回传互不排斥（纯磁盘 IO），但三条链路彼此一次只跑一个。

    @app.post("/api/datajob/preview")
    def datajob_preview(req: DataPreviewRequest) -> dict:
        """分包前的便宜预览：发现张数、去重排除数、画师覆盖、来源分布。
        只有分包做预览——封包复用 /api/preview 的磁盘估算，拼接的表单是
        静态的，对账看板本身就是它的"预览"。"""
        if req.kind != "split":
            raise HTTPException(400, "只有分包有预览")
        if not req.input_dir.strip():
            raise HTTPException(400, "必须给素材目录")
        if not Path(req.input_dir).exists():
            raise HTTPException(400, f"素材路径不存在: {req.input_dir}")

        from ..artist import AliasTable, extract_artist
        from ..config import DEFAULT_TIERS, load_ops, load_tier_table
        from ..dedup import load_exclude
        from ..pack import discover_inputs

        table = load_tier_table(DEFAULT_TIERS)
        ops = load_ops(table)
        rule = ops.artist_rule()
        rule.enabled = True          # 与 run_split 同口径
        aliases = AliasTable(rule.aliases_path).as_dict() if rule.aliases_path else {}
        try:
            items = discover_inputs(req.input_dir, recursive=req.recursive,
                                    limit=req.limit or None)
        except OSError as exc:
            raise HTTPException(400, f"扫不动输入: {exc}") from exc
        excluded = 0
        if req.exclude:
            # 预览不因排除清单写错路径而炸：清单缺失按"排除 0 张"处理，
            # 前端靠这个用猜的默认路径试探去重清单在不在。
            try:
                keys = load_exclude(req.exclude)
            except OSError:
                keys = set()
            excluded = sum(1 for it in items if it.key in keys)
        total = len(items)
        artists: dict[str, int] = {}
        sources: dict[str, int] = {}
        no_artist = 0
        for it in items:
            a = extract_artist(it.logical_path, rule, aliases)
            if a.tag:
                artists[a.tag] = artists.get(a.tag, 0) + 1
            else:
                no_artist += 1
            # 来源分布按"包 / 画师目录"聚合，而不是逐文件——散图语料
            # 一路扫下来每个文件都是自己的 source，逐文件列表没有信息量。
            src_key = it.source if it.kind == "zip_member" \
                else str(Path(it.source).parent)
            sources[src_key] = sources.get(src_key, 0) + 1
        top = sorted(sources.items(), key=lambda kv: -kv[1])[:10]
        return {
            "total": total,
            "bytes": sum(it.size for it in items),
            "excluded": excluded,
            "artists": {"count": len(artists), "covered": total - no_artist,
                        "missing": no_artist},
            "sources": [{"name": name, "count": count} for name, count in top],
        }

    @app.post("/api/datajob/start", status_code=202)
    def datajob_start(req: DataJobStartRequest) -> dict:
        try:
            job = DATAJOBS.create(req.kind, req.params)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.snapshot()

    @app.get("/api/datajob")
    def datajob_list() -> dict:
        return {"jobs": [j.snapshot() for j in DATAJOBS.all()]}

    @app.get("/api/datajob/{job_id}")
    def datajob_status(job_id: str) -> dict:
        job = DATAJOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个数据任务")
        return job.snapshot()

    @app.post("/api/datajob/{job_id}/stop")
    def datajob_stop(job_id: str) -> dict:
        if not DATAJOBS.stop(job_id):
            raise HTTPException(409, "这个数据任务已结束或不存在")
        job = DATAJOBS.get(job_id)
        return {"ok": True, "snapshot": job.snapshot() if job else None}

    @app.get("/api/hf/files")
    def hf_files(repo: str = "", token: str = "", revision: str = "main") -> dict:
        """仓库里的分片清单，供表单"查看文件列表"。token 只在本次请求内
        使用：不落盘、不进日志、不进任何任务状态。"""
        from .. import hf_source

        if not repo.strip():
            raise HTTPException(400, "缺少 repo")
        try:
            names = hf_source.list_parquet(repo.strip(), revision=revision,
                                           token=token or None)
        except hf_source.HFError as exc:
            raise HTTPException(400, f"HF 访问失败：{exc}") from exc
        except Exception as exc:          # noqa: BLE001 - 网络/权限统一翻 400
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        return {"repo": repo.strip(), "revision": revision, "files": names}

    # ---- vLLM 服务（NL 模式的前置）

    @app.get("/api/vllm")
    def vllm_status() -> dict:
        """本机服务的状态 + 指纹 + 日志尾巴。页面每 4 秒拉一次。"""
        return VLLM.snapshot()

    @app.post("/api/vllm/start", status_code=202)
    def vllm_start(req: VllmStartRequest) -> dict:
        try:
            return VLLM.start(port=req.port, timeout_s=req.timeout_s,
                              model=req.model, tier=req.tier,
                              served_name=req.served_name,
                              max_num_seqs=req.max_num_seqs,
                              max_num_batched_tokens=req.max_num_batched_tokens,
                              gpu_mem=req.gpu_mem)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/vllm/plan")
    def vllm_plan(model: str = "", tier: str = "",
                  max_num_seqs: int | None = None,
                  max_num_batched_tokens: int | None = None,
                  gpu_mem: float | None = None) -> dict:
        """这组参数会解析成哪个分档 / 哪套权重。点启动之前就要能看见。

        参数与 /start 同名同义，只是不真的起进程。解析失败（模型 key 写错、
        卡不支持该精度）也在这里变成一句话，而不是几分钟后的一段 traceback。
        """
        return VLLM.plan(model=model or None, tier=tier or None,
                         max_num_seqs=max_num_seqs,
                         max_num_batched_tokens=max_num_batched_tokens,
                         gpu_mem=gpu_mem)

    @app.post("/api/vllm/stop")
    def vllm_stop() -> dict:
        return VLLM.stop()

    @app.get("/api/vllm/probe")
    def vllm_probe(url: str = "") -> dict:
        """远程服务只能探：/health 说明活着，/v1/models 说明模型名对不对。"""
        return VLLM.probe(url)

    # ---- 图片

    @app.get("/api/jobs/{job_id}/thumb/{image_id:path}")
    def thumb(job_id: str, image_id: str, w: int = 360) -> Response:
        """缩略图。散图直接读盘，zip 成员从包内取——两条路走同一个缓存，
        key 里带包路径 + 成员名，不能只带 image_id（跨任务会命中错图）。"""
        job = MANAGER.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        item = job.item(image_id)
        if item is None:
            raise HTTPException(404, "找不到这张图")
        width = max(64, min(1024, w))
        if item.in_zip:
            try:
                mtime = Path(item.source).stat().st_mtime_ns
            except OSError as exc:
                raise HTTPException(404, "素材包已被移走") from exc
            key = (item.source, item.member, width, mtime)
            cached = THUMBS.get(key)
            if cached is None:
                try:
                    data = job.image_bytes(image_id)
                    if data is None:
                        raise FileNotFoundError(item.member)
                    cached = _thumbnail_bytes(data, width)
                except Exception as exc:  # noqa: BLE001 - 坏图不该 500
                    raise HTTPException(
                        415, f"这张图解不开：{type(exc).__name__}") from exc
                THUMBS.put(key, cached)
            return Response(cached, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

        path = job.image_path(image_id)
        if path is None or not path.is_file():
            raise HTTPException(404, "找不到这张图")
        try:
            mtime = path.stat().st_mtime_ns
        except OSError as exc:
            raise HTTPException(404, "图片已被移走") from exc
        key = (str(path), width, mtime)
        cached = THUMBS.get(key)
        if cached is None:
            try:
                cached = _thumbnail(path, width)
            except Exception as exc:      # noqa: BLE001 - 坏图不该 500
                raise HTTPException(415, f"这张图解不开：{type(exc).__name__}") from exc
            THUMBS.put(key, cached)
        return Response(cached, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    # ---- WebSocket 进度

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket, job_id: str = "", cursor: int = 0) -> None:
        await ws.accept()
        job = MANAGER.get(job_id)
        if job is None:
            await ws.send_json({"type": "error", "message": "没有这个任务"})
            await ws.close()
            return
        seq = max(0, cursor)
        last_snapshot = 0.0
        try:
            while True:
                events = job.events_since(seq)
                if events:
                    seq = events[-1]["seq"]
                now = time.time()
                # 没事件也要周期性推快照：ETA、速率、elapsed 是随时间变的，
                # 只在有事件时推，页面上的秒表会停。
                if events or now - last_snapshot > 1.0:
                    last_snapshot = now
                    await ws.send_json({"type": "tick", "seq": seq,
                                        "events": events,
                                        "snapshot": job.snapshot()})
                if job.status in ("done", "failed", "stopped") and not events:
                    await ws.send_json({"type": "done", "seq": seq,
                                        "snapshot": job.snapshot()})
                    break
                await asyncio_sleep(0.15)
        except WebSocketDisconnect:
            return
        except RuntimeError:
            # 服务端关闭或客户端拔线：这里没有可做的补救，安静退出即可。
            return
        await ws.close()

    # ---- 静态前端

    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return _NOT_BUILT_HTML

    return app


async def asyncio_sleep(seconds: float) -> None:
    """薄封装：让上面的循环不必 import asyncio（也方便测试里 monkeypatch）。"""
    import asyncio

    await asyncio.sleep(seconds)


_NOT_BUILT_HTML = """<!doctype html><meta charset="utf-8">
<title>Tagger UI</title>
<style>
 body{margin:0;height:100vh;display:grid;place-items:center;
      background:#111418;color:#e6e9ef;
      font:15px/1.7 "Segoe UI",system-ui,sans-serif}
 code{background:#1b2027;padding:2px 6px;border-radius:4px;color:#ee4c2c}
 pre{background:#1b2027;padding:16px 20px;border-radius:8px;line-height:1.9}
 .card{max-width:640px;padding:32px 36px;border:1px solid #252b34;border-radius:12px}
 h1{margin:0 0 12px;font-size:20px}
 p{color:#9aa4b2;margin:8px 0}
</style>
<div class=card>
  <h1>前端还没构建</h1>
  <p>后端是活的（<code>/api/health</code> 能通），但 <code>tagger/web/ui/dist</code>
     里没有 <code>index.html</code>。</p>
  <p>构建一次（需要 Node 18+）：</p>
  <pre>cd tagger/web/ui
npm install
npm run build</pre>
  <p>开发时更省事：<code>npm run dev</code> 起在 3010，已把 <code>/api</code>
     与 <code>/ws</code> 代理到本服务。</p>
</div>
"""

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return _NOT_BUILT_HTML

    return app


async def asyncio_sleep(seconds: float) -> None:
    """薄封装：让上面的循环不必 import asyncio（也方便测试里 monkeypatch）。"""
    import asyncio

    await asyncio.sleep(seconds)


_NOT_BUILT_HTML = """<!doctype html><meta charset="utf-8">
<title>Tagger UI</title>
<style>
 body{margin:0;height:100vh;display:grid;place-items:center;
      background:#111418;color:#e6e9ef;
      font:15px/1.7 "Segoe UI",system-ui,sans-serif}
 code{background:#1b2027;padding:2px 6px;border-radius:4px;color:#ee4c2c}
 pre{background:#1b2027;padding:16px 20px;border-radius:8px;line-height:1.9}
 .card{max-width:640px;padding:32px 36px;border:1px solid #252b34;border-radius:12px}
 h1{margin:0 0 12px;font-size:20px}
 p{color:#9aa4b2;margin:8px 0}
</style>
<div class=card>
  <h1>前端还没构建</h1>
  <p>后端是活的（<code>/api/health</code> 能通），但 <code>tagger/web/ui/dist</code>
     里没有 <code>index.html</code>。</p>
  <p>构建一次（需要 Node 18+）：</p>
  <pre>cd tagger/web/ui
npm install
npm run build</pre>
  <p>开发时更省事：<code>npm run dev</code> 起在 3010，已把 <code>/api</code>
     与 <code>/ws</code> 代理到本服务。</p>
</div>
"""

 .card{max-width:640px;padding:32px 36px;border:1px solid #252b34;border-radius:12px}
 h1{margin:0 0 12px;font-size:20px}
 p{color:#9aa4b2;margin:8px 0}
</style>
<div class=card>
  <h1>前端还没构建</h1>
  <p>后端是活的（<code>/api/health</code> 能通），但 <code>tagger/web/ui/dist</code>
     里没有 <code>index.html</code>。</p>
  <p>构建一次（需要 Node 18+）：</p>
  <pre>cd tagger/web/ui
npm install
npm run build</pre>
  <p>开发时更省事：<code>npm run dev</code> 起在 3010，已把 <code>/api</code>
     与 <code>/ws</code> 代理到本服务。</p>
</div>
"""
