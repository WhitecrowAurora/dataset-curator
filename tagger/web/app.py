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
import os
import platform
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import llama_runtime, nl_api
from ..config import ConfigError
from ..local_tag import EXTS
from ..pipeline import plan_inputs, plan_stats
from ..quantize import QUANT
from ..gguf_convert import CONVERTER as GGUF_CONVERT
from ..gguf_convert import snapshot as gguf_snapshot
from .jobs import GUARD_KNOBS, MANAGER, STAT_FINE, Job, JobSpec
from .models import MODELS
from .uploads import UPLOADS, shard_rows
from .dedup import DEDUPS
from .hf_downloads import HfDownloads
from .packaging import DATAJOBS
from .hf_downloads import DOWNLOADS
from .vllm_service import VLLM
from .center import CENTER, CenterError
from .multi_gpu import MULTI
from .selfcheck import SELFCHECK
from .deps import DEPS

UI_DIR = Path(__file__).resolve().parent / "ui"
DIST_DIR = UI_DIR / "dist"

# 产物目录里那些 .jsonl 不是产物：失败账与清单。回传页列文件时排掉，
# 否则用户会以为"这些也该传"，而传上去的对账侧读不出任何记录。
_NON_ARTIFACT_NAMES = {"failures.jsonl", "manifest.jsonl", "state.jsonl"}


class JobRequest(BaseModel):
    # 与 jobs.JobSpec 一一镜像（create 里 JobSpec(**req.model_dump())）。
    # 加字段必须两边同改，漏一边就是"表单发了、任务收不到"的静默丢参。
    # ---- 输入。vlm 引擎走 HF 仓库时可为空，本地引擎必填（create 校验）
    input_dir: str = ""
    # 只打这些本地分片（离线包面板勾出来的绝对路径）；空 = 按 input_dir 整包
    input_shards: list[str] = []
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
    # pil = CPU 缩放/归一化（现状）；tensor = 缩放/归一化上卡（解码仍在 CPU）
    preprocess: str = "pil"
    threshold: float | None = None
    # tag 后端补充（两种引擎通用）
    keep_categories: list[str] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)
    model_repo: str = ""
    guard: bool = True
    # 硬件保护阈值覆盖（键名同 guard.GuardPolicy，白名单见 jobs.GUARD_KNOBS）。
    # 空 = 用 configs/tiers.yaml 的 limits 段；页面默认值就是从那段读来的。
    guard_policy: dict[str, float] = Field(default_factory=dict)
    # 开跑前/封包中按"预计产出 + 20% 冗余"判盘够不够（取代 guard 的固定阈值）
    check_space: bool = True

    # ---- VLM 服务（engine="vlm"）
    # 推理来源：local = 本机/自建服务；openai = OpenAI 兼容 API；
    # anthropic = Anthropic Messages API。后两者不加载本地权重、不读指纹。
    vlm_provider: str = "local"
    # API 模式地址与密钥。地址留空按来源取默认域名；密钥不进快照（会被抹成 ***）。
    api_base: str = ""
    api_key: str = ""
    url: str = "http://127.0.0.1:8000"
    vlm_model: str = "tagger"
    # both = NL + tag；nl = 只要 NL；tag = 只要 tag（单模式都由 VLM 出，本地后端强制 none）
    output_mode: str = "both"
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 0
    # 描述长度引导（字符区间），例如 [250, 500]。空 = 按 token 区间派生。
    # 它与 max_tokens 是两件事：max_tokens 管"写不下就截断"，这个管"往多长写"。
    # 实测 400-1024 → 250-500：字数 −33%、延迟 −36%、要素覆盖只掉 2~3pp。
    caption_chars: list[int] | None = None
    # 中文 NL 的比例（0~1）。按 (seed, image_id) 抽签，抽中的图用中文写 caption；
    # tag 段永远英文。0 = 全英文退回（是合法值，不能当"没填"吞掉）。
    caption_zh_ratio: float = 0.3
    # 用哪份 NL 提示词：prompt/<名字>.json 的名字。空 = 内置那份（按上面的
    # 长度/比例参数生成）。文件里写的是整段指令，会替换内置提示词。
    prompt_id: str = ""
    timeout_s: int = 90
    concurrency: int = 2
    # 多卡（数据并行）。multi_gpu 打开时忽略 url，改用多卡服务组的地址；
    # per_card_seqs 是"每张卡同时跑几条"（0 = 按分档表/显存推算）。
    multi_gpu: bool = False
    multi_gpus: list[int] = []
    per_card_seqs: int = 0
    max_pixels: int = 0
    seed: int | None = None
    # 同一张图累计失败多少次后跳过（0 = 不限制）
    max_failures: int = 3
    # 质检判定线与重打次数（0 = 用配置：pass 3.4 / drop 1.8 / 重打 2 次）
    quality_pass_score: float = 0.0
    quality_drop_score: float = 0.0
    quality_max_attempts: int = 0
    # 重试阶梯：每次重打温度上调多少、封顶多少，像素预算乘几倍、封顶多少。
    # 0 = 用配置。它们直接决定"救一张图要花几次推理"，30M 量级下是硬成本。
    quality_retry_temp_step: float = 0.0
    quality_retry_temp_cap: float = 0.0
    quality_retry_pixels_scale: float = 0.0
    quality_retry_pixels_cap: int = 0
    # L2 语义打分（图文 CLIP）。开启后客户端会把图片也送进质检，
    # 抓"句子通顺但完全跑题"。代价是必须装 open_clip_torch、吃一份显存，
    # 且打分模型不是线程安全的——并发会被摁到 1。
    quality_enable_clip: bool = False
    quality_clip_min_score: float = 0.0
    # CLIP 权重与预训练来源（空 = 跟配置 ViT-L-14 / openai）。
    # 换它们等于换打分口径，两批数据的分数不能直接比。
    quality_clip_model: str = ""
    quality_clip_pretrained: str = ""
    # L1 规则层阈值。None = 跟配置。这里不能拿 0 当"没填"：
    # 最短 0 字 / 重复率 0 都是合法意图（后者 = 一个 4-gram 都不许重复）。
    quality_min_chars: int | None = None
    quality_max_chars: int | None = None
    quality_min_tag_coverage: float | None = None
    quality_max_repeat_ratio: float | None = None
    # 重试升级：重试用尽仍不合格时打到第二个服务（另一套更大的权重）。
    # vLLM 一个进程只服务一套权重，所以只能是另一个地址。
    escalate_url: str = ""
    escalate_model: str = ""
    # 用 file:// 传图（服务端必须用 --allowed-local-media-path 放行产物目录）。
    # 省掉 base64 的 33% 体积与编码 CPU，但要求客户端与服务端同机同路径。
    use_file_url: bool = False

    # ---- HF 输入（engine="vlm"）
    hf_repo: str = ""
    hf_token: str = ""
    hf_revision: str = "main"
    hf_files: list[str] = Field(default_factory=list)
    # auto / parquet / webdataset。parquet 是逐行流式（不落盘），
    # tar 分片要先下到本地再打——磁盘策略完全不同，所以必须能让用户看见。
    hf_format: str = "auto"
    # tar 模式：预取几片（下载与打标并行）；0 = 严格串行
    hf_prefetch: int = 1
    # 单片用几条并发连接（1 = 单连接；文件 < 16 MB 时自动单连接）
    hf_conns: int = 4
    # HF 请求的代理。空 = 用设置里的全局代理
    hf_proxy: str = ""
    # 给产物留的磁盘余量（GB）；0 = 自动（最大分片的 30%，不少于 2 GB）
    hf_reserve_gb: float = 0.0

    # 结果缓存：同图同参复用上次产出。默认关（首轮命中率为 0）
    cache: bool = False
    cache_max_gb: float = 8.0

    # ---- 产物形态（本地默认 jsonl：一包一文件，回传时一人发自己那份）
    artifact_format: str = "jsonl"
    parquet_rows: int = 2000

    # ---- 命名
    artist: bool = True
    artist_depth: int = 0
    # 画师名写进 .nl 正文开头（false = 只留在 JSON 字段里）
    artist_inject: bool = True
    # 画师名写进 .tag 串开头。与上一项分开：NL 与 TAG 是两条训练输入，
    # 只想要其一的场景绑成一个开关就没法表达。
    artist_inject_tags: bool = True
    # 画师对照表：单 parquet / 分片目录 / hf://datasets/<repo>/<文件或目录>。
    # 空 = 跟 tiers.yaml 的 artist.table 走。
    artist_table: str = ""
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


class ModelsRequest(BaseModel):
    """模型管理页整表提交：vlm = NL 模型，tag = tag 分类头。

    只认这几个字段，多余的键由 web/models.py 丢掉——不认识的字段进了配置
    就是"看起来生效、实际没人读"。
    """

    vlm: list[dict[str, Any]] = Field(default_factory=list)
    tag: list[dict[str, Any]] = Field(default_factory=list)


class ModelTarget(BaseModel):
    """模型管理页对单个模型的动作目标（下载 / 取消 / 删文件 / 开目录）。"""

    kind: str = "vlm"          # vlm | tag
    key: str
    # 只对下载有意义：q4/q5/q6/q8 时只下这一档 + 配套 mmproj，空 = 按清单下。
    precision: str | None = None


class ProxyRequest(BaseModel):
    """下载代理地址。空串 = 直连；支持 http:// 与 socks5://。"""

    url: str = ""


class EndpointRequest(BaseModel):
    """下载源前缀（镜像站）。空串 = 跟随 HF_ENDPOINT / 官方。"""

    url: str = ""


class ConnsRequest(BaseModel):
    """大文件下载连接数。0 = 用默认（4）。"""

    conns: int = 0


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
    # 画师表（空 = 跟配置）。预览要说"识别到几个画师"，不查表就会少报一大截。
    artist_table: str = ""


class HfFilesRequest(BaseModel):
    """列仓库分片。

    token 走请求体而不是 query string：query 会原样进 uvicorn 的 access log，
    私有仓库的凭证留在日志里比留在内存里危险得多。
    `format` 为空/auto 时由后端探测（有 parquet 按 parquet，否则按 tar），
    这样"这个仓库到底是什么形态"不用用户猜。
    """
    repo: str
    token: str = ""
    revision: str = "main"
    format: str = "auto"
    proxy: str = ""


class DownloadsStartRequest(BaseModel):
    """把选中的分片下到本地缓存（不排队等 GPU）。"""
    repo: str
    files: list[str] = Field(default_factory=list)
    # 表单读到的大小表：用它算"这一片放不放得下"，省掉后端再列一次
    sizes: dict[str, int] = Field(default_factory=dict)
    token: str = ""
    revision: str = "main"
    conns: int = 4
    proxy: str = ""
    # 给产物留的余量（GB）。0 = 用管理器当前的设置
    reserve_gb: float = 0.0


class VllmStartRequest(BaseModel):
    """本机拉起 vLLM。端口改成非默认值时，任务表单里的服务地址要一起改。

    model 选权重（None = 按显存落档）；served_name 是接口路由名，必须与任务
    表单里的「模型名」一致。并发三项留空 = 用分档表的值。它们只管服务端批
    调度；任务侧的 `concurrency` 是另一回事（客户端在途请求数），两者要一起调。
    """
    port: int = 8000
    timeout_s: int = 900
    # 引擎：vllm | llamacpp。留空 = 按平台选（Windows 只有 llamacpp 能用，
    # 见 nl_engine.normalize_engine）。换引擎等于换一代数据，指纹里会记下来。
    engine: str | None = None
    model: str | None = None
    tier: str | None = None
    # 权重精度：bf16（默认）| fp8 | w4a16。留空 = 用分档表的值。
    # fp8 需要 Ada 及以上；w4a16 需要预量化的 AWQ/GPTQ 权重，缺权重会回退 bf16
    # 并在 plan.warnings 里说明——不静默降级。
    precision: str | None = None
    served_name: str | None = None
    # 自定义分档时自己填的上下文长度；不填就按分档表
    max_model_len: int | None = None
    max_num_seqs: int | None = None
    max_num_batched_tokens: int | None = None
    gpu_mem: float | None = None
    # 非空 = 加 --allowed-local-media-path。客户端才能用 file:// 传图，
    # 省掉 base64 的 33% 体积；放行范围必须覆盖任务的产物目录。
    local_media_root: str | None = None
    # llama.cpp 的槽位数（-np）。留空 = 按显存自动。
    # 这是**唯一**能让出显存的旋钮：KV 在启动时按槽位数预留，任务并发调小
    # 只是让 slot 空着。"边打标边玩游戏"就是把它从 12 降到 4。
    slots: int | None = None


class LlamaInstallRequest(BaseModel):
    """装 llama.cpp 运行时（llama-server）。

    url 非空 = 直接下这个 zip：离线包、公司内网镜像、或 GitHub 被墙时
    手动下的那一份。留空才去查 GitHub 的 latest。
    """
    url: str = ""
    # 已经装过也重装：换构建（比如从 CPU 包换成 CUDA 包）要用。
    force: bool = False


class NlApiProbeRequest(BaseModel):
    """探测外部推理 API：地址通不通、模型认不认、图看不看得见。

    本机模式下有「启动 → 就绪」这个红灯，API 模式没有进程可看；没有这个
    接口的话，密钥/模型名写错要等任务跑起来、第一条产物失败才暴露。
    key 可以留空——免鉴权的自建网关很常见，留空时走环境变量。
    """
    provider: str = "openai"
    base: str = ""
    model: str = ""
    key: str = ""
    timeout_s: float = 30.0


class NlApiKeyRequest(BaseModel):
    """记住 / 忘记一把密钥。key 传空 = 删除该来源的存档。

    remember=false 时把刚存的删掉：页面上的勾选框取消要能立刻生效，不能
    留下一个"看不见但还在盘上"的密钥。
    """
    provider: str = "openai"
    base: str = ""
    key: str = ""
    remember: bool = True


class MultiStartRequest(BaseModel):
    """多卡：每张卡各起一个单卡服务。

    gpus 是 nvidia-smi 的物理卡号，空 = 用全部卡。base_port 是第 0 个
    worker 的端口，其余按卡顺序 +1、+2……所以选卡时端口会跳号，
    页面照 gpus 里的顺序显示真实端口，不要自己算。

    per_card_seqs 是每卡开几个 slot（= 该卡服务的 max_num_seqs / -np）。
    它决定"每张卡同时处理几条"，与任务表单里的并发是同一个数——
    填了任务侧就不用再填。
    """
    engine: str | None = None
    gpus: list[int] = []
    base_port: int = 8010
    model: str | None = None
    tier: str | None = None
    precision: str | None = None
    served_name: str | None = None
    per_card_seqs: int | None = None
    timeout_s: int = 1800


class SelfCheckRequest(BaseModel):
    """部署自检：给一个装图片的文件夹，逐项验一遍这台机器能不能干活。

    url 留空时自动找多卡服务组的第一个；都没起就跳过"服务/推理"两项——
    skip 不算失败，装机验证不该因为"还没开那个可选功能"整片飘红。
    """
    input_dir: str
    recursive: bool = True
    sample: int = 3
    url: str = ""


class QuantStartRequest(BaseModel):
    """页内量化：把当前模型压成 W4A16。

    model 留空 = 按显存自动落档（与启动服务同一口径）；out_dir 留空 =
    models/nl/<key>-w4a16。method 只影响算法（gptq / awq），产物格式相同。
    """
    model: str | None = None
    out_dir: str | None = None
    method: str = "gptq"


class GgufConvertRequest(BaseModel):
    """本地量化成 GGUF：原始 safetensors → F16 → Q4_K_M / Q6_K。

    target 是量化档位（q4_k_m / q6_k）。产物登记回覆盖文件，之后在新任务页
    把权重精度选成同名档就会用它。
    """
    model: str
    target: str = "q6_k"


class CenterLoginRequest(BaseModel):
    """调度中心的身份。secret 是管理员一次性签发的，留空表示服务端免签。"""
    server: str
    user_id: str
    secret: str = ""


class CenterRunRequest(BaseModel):
    """托管 fox worker 的运行参数。全部可省略 = 沿用上次保存的配置。"""
    tag_url: str | None = None
    mode: str | None = None
    out_dir: str | None = None
    interval: int | None = None
    extra_args: str | None = None
    cmd_template: str | None = None
    # 长驻等待：开了就是"挂在线上等人派活"，关掉是"手上的干完就停"
    wait: bool | None = None
    idle_timeout: float | None = None    # 配 wait：连续多少秒没活就自己收工（0 = 一直等）
    max_packages: int | None = None      # 本次最多交付多少片（0 = 不限）


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


def _weight_state(key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """这套权重能不能直接跑（给新建任务页做"缺文件"提示）。

    口径与模型管理页一致：llama.cpp 要的 gguf / mmproj 都在盘上才算好，
    只看目录非空是不够的——下载只下原始权重（safetensors），那两个文件得等
    「本地量化」压出来；没压之前目录再满也跑不起来。
    """
    from . import downloads as dl
    from .models import weight_summary

    try:
        dest = dl.resolve_dir("vlm", key, None)
        exists = dest.is_dir() and any(dest.iterdir())
    except OSError:
        dest, exists = None, False
    summary = weight_summary(spec, dest)
    return {
        "exists": exists,
        # 目录里空着但文件在别处（path 写了绝对路径）也算能用
        "ready": not summary["missing_files"],
        **summary,
        # gguf_ready：哪几档 GGUF 真有文件（页面的精度选项据此置灰）
        # raw_ready ：原始权重在不在（不在就只能用已有的档，压不出新的）
        # quantized ：vLLM 的 W4A16 要有预量化权重
    }


def _defaults() -> dict:
    """表单默认值。能读配置就读，读不到用代码里的兜底，绝不因此报错。"""
    tagging: dict[str, Any] = {}
    archive: dict[str, Any] = {}
    artist: dict[str, Any] = {}
    # 硬件保护的滑条初值。只取 jobs.GUARD_KNOBS 白名单那几项，
    # 页面能调的和后端认的必须是同一份名单——两处各写一份就会出
    # "页面上能调、后端默默丢掉"的假开关。
    guard: dict[str, float] = {}
    notes: dict[str, str] = {}
    raw_models: dict[str, Any] = {}
    raw_tiers: list[dict[str, Any]] = []
    # 质检默认值。页面上的输入框要显示"0 = 用配置的 3.4"这种话，那个 3.4
    # 必须来自配置本身：写死在 JSX 里的话，改了 tiers.yaml 页面就在骗人。
    quality: dict[str, Any] = {}
    try:
        from ..config import DEFAULT_TIERS, load_model_overlay, load_ops, load_tier_table

        # 之前传的是 None：Path(None) 抛 TypeError 被 except 吞掉，
        # 页面上的默认值从来没读到过配置，一直显示代码兜底。
        table = load_tier_table(DEFAULT_TIERS)
        ops = load_ops(table)
        tagging = dict(ops.tagging or {})
        archive = dict(ops.archive or {})
        artist = dict(ops.artist or {})
        policy = ops.guard_policy()
        guard = {k: float(getattr(policy, k)) for k in GUARD_KNOBS}
        quality = dict(ops.quality or {})
        notes = dict(load_model_overlay().get("notes") or {})
        raw_models = dict(table.get("models") or {})
        raw_tiers = list(table.get("tiers") or [])
    except Exception:                 # noqa: BLE001 - 配置文件坏了不该连页面都打不开
        tagging = {}
        archive = {}
        artist = {}
        quality = {}
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
    # 权重在不在盘上，在这里一并算好给页面：配置里有这号模型 ≠ 权重已经下好。
    # 少这一项，用户选了 Q6 却只下了 Q4 时，页面上一点提示都没有，直到点启动
    # 才看到"文件不存在"——而那时候他已经在等任务跑了。
    vllm_models = [
        {
            "key": str(key),
            "path": str(spec.get("path") or ""),
            "precision": str(spec.get("precision") or "int4"),
            "family": str(spec.get("family") or ""),
            "tier": tier_of.get(str(key), ""),
            # 模型管理页填的备注，下拉里当副标题用（"8B 量化版"这类）。
            "note": str(notes.get(f"vlm:{key}") or ""),
            **_weight_state(str(key), spec),
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
        # 配置里没写就默认 pil：tensor 只有大 PNG 才真赚（见 perf-baseline §12）
        "preprocess": str(tagging.get("preprocess") or "pil"),
        "threshold": float(((tagging.get("thresholds") or {}).get("default")) or 0.35),
        "keep_categories": list(tagging.get("keep_categories") or []),
        # 各 tag 后端在配置里绑定的 HF 仓库（按后端分组，切换后端时才不会拿错）。
        "tag_models": {str(k): str(v) for k, v in (tagging.get("models") or {}).items()},
        "vllm_models": vllm_models,
        "vllm_tiers": vllm_tiers,
        # 多卡组默认值。基端口故意和单卡服务的 8000 错开：两个面板的默认
        # 值撞在一起时，先起的那个会让后起的报"端口被占用"，而人第一反应
        # 是去查进程而不是改端口。
        "multi": {"base_port": 8010, "per_card_seqs": 0},
        # 推理来源下拉。默认地址与"密钥去哪个环境变量找"都由后端给，
        # 前端只负责显示——把 api.openai.com 写进 JSX 的话，改一次要发两处。
        "vlm_providers": [
            {
                # 后端的本机用空串表示，页面上一律写 "local"（少一层空串判断）。
                "value": value or "local",
                "label": nl_api.LABELS[value],
                "base": nl_api.DEFAULT_BASES.get(value, ""),
                "env_key": (nl_api.ENV_KEYS.get(value) or [""])[0],
                # 免鉴权的自建网关很常见（LM Studio / one-api 默认不开 key），
                # 所以不能把"必须填密钥"写死成通用规则。
                "requires_key": False,
                # 云端要的是"每分钟请求数"，与本机显存无关，默认值各给一档。
                "concurrency": nl_api.default_concurrency(value),
                # 推荐输入尺寸（总像素）。云端看不到更多细节时，多给的像素
                # 只是多付钱，所以基线由来源决定而不是沿用本地预算。
                "max_pixels": nl_api.default_max_pixels(value),
                # 模型名建议（<datalist>）：只是提示，可以填任何 id。
                "models": nl_api.model_hints(value),
                # 记住过密钥就回一份脱敏值，页面知道"这里有一份存档"，
                # 但拿不到明文——浏览器不该能捞出密钥。
                "saved": ({"base": entry.get("base", ""),
                           "key_masked": nl_api.mask_key(entry.get("key", ""))}
                          if (entry := nl_api.saved_for(value)) else None),
            }
            for value in nl_api.PROVIDERS
        ],
        "guard": guard,
        # 质检的配置默认值。0 在这套表单里是"不覆盖"的哨兵值，所以页面
        # 必须能读到真实数字才能把占位说明写对。
        "quality": {
            "pass_score": float(quality.get("pass_score") or 3.4),
            "drop_score": float(quality.get("drop_score") or 1.8),
            "max_attempts": int(quality.get("max_attempts") or 2),
            "retry_temp_step": float(quality.get("retry_temp_step") or 0.15),
            "retry_temp_cap": float(quality.get("retry_temp_cap") or 0.7),
            "retry_pixels_scale": float(quality.get("retry_pixels_scale") or 1.5),
            "retry_pixels_cap": int(quality.get("retry_pixels_cap") or 1048576),
            "enable_clip": bool(quality.get("enable_clip", False)),
            "clip_min_score": float(quality.get("clip_min_score") or 0.22),
            "clip_model": str(quality.get("clip_model") or "ViT-L-14"),
            "clip_pretrained": str(quality.get("clip_pretrained") or "openai"),
            # L1 规则层。0 是合法值（最短 0 = 不设下限），所以这里原样透传，
            # 只有键缺失才用代码兜底。
            "min_chars": int(quality.get("min_chars", 140)),
            "max_chars": int(quality.get("max_chars", 1200)),
            "min_tag_coverage": float(quality.get("min_tag_coverage", 0.60)),
            "max_repeat_ratio": float(quality.get("max_repeat_ratio", 0.35)),
        },
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
        # 注入默认跟配置走；NL 与 TAG 两个开关分开（各管一条训练输入）
        "artist_inject": bool(artist.get("inject_caption", True)),
        "artist_inject_tags": bool(artist.get("inject_tags", True)),
        "artist_prefix": str(artist.get("prefix") or "@"),
        # 配置里配过就预填，用户可以在表单里改成别的表；清空 = 回落到配置。
        "artist_table": str(artist.get("table") or ""),
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

def _volume_label(root: str) -> str:
    """盘符的卷标，读不到就是空串。

    空卷标很常见（新分区、内存盘、刚格式化的盘），那不是错误——前端会退化
    成只显示盘符。所以这里把失败也归到空串，不抛。
    """
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(261)
        gvi = ctypes.windll.kernel32.GetVolumeInformationW   # type: ignore[attr-defined]
        # 不设 argtypes 的话，64 位下指针参数会被按 32 位截断，调用直接崩。
        gvi.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_wchar_p, ctypes.c_uint32,
        ]
        gvi.restype = ctypes.c_int
        # 卷标最长 32 字符，261（MAX_PATH）绰绰有余
        ok = gvi(root, buf, 261, None, None, None, None, 0)
        return buf.value if ok else ""
    except (AttributeError, OSError):
        return ""


def _probe_drives() -> list[dict[str, str]]:
    """枚举"点进去确实能读"的盘符，带卷标。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        mask = kernel32.GetLogicalDrives()
        get_type = kernel32.GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint

        # 2=可移动 3=固定 6=内存盘：类型本身就说明它在那，直接列。
        #
        # 4=网络盘必须再 exists 一次。`GetLogicalDrives` 只说明"映射还挂在
        # 注册表里"——`net use` 连过的 NAS 早断了，位图照样亮着，类型也照样
        # 报 4。这类鬼影盘符点进去就是一次 TCP 超时，列表里混进一排更糟。
        #
        # 5=光驱不列：空盘探测会一直等到驱动超时，把整个浏览拖死。
        local = {2, 3, 6}
        network = 4

        out: list[dict[str, str]] = []
        for i in range(26):
            if not (mask >> i) & 1:
                continue
            root = f"{chr(ord('A') + i)}:\\"
            kind = get_type(root)
            if kind in local or (kind == network and os.path.exists(root)):
                out.append({"path": root, "label": _volume_label(root)})
        return out
    except (AttributeError, OSError):
        # 拿不到就返回空：前端会退化成"只有上一级"，不该因此让浏览失败。
        return []


# 探测断开的网络映射要等 TCP 超时，而每点一次"浏览"都会请求一次盘符列表。
# 盘符的变化粒度是"插拔 U 盘"，缓存一分钟看不出来。
_DRIVES_TTL = 60.0
_drives_cache: tuple[float, list[dict[str, str]]] | None = None


def _drives() -> list[dict[str, str]]:
    """可以切换的盘符（Windows）/ 挂载根（其他系统）。

    "只能选训练器所在的那块盘"是目录浏览器最常被抱怨的一点：往上翻到
    `C:\\` 之后 `parent` 就是自己，再没有出口。这里把**真能用**的盘列出来，
    带上卷标（大家都会给盘起名，光有字母根本分不清哪块是哪个），
    前端做成一行可点的切换。
    """
    if os.name != "nt":
        # Linux / macOS 从 `/` 一路往下就能走到任何挂载点，不需要额外入口。
        return []

    global _drives_cache
    now = time.monotonic()
    if _drives_cache is not None and now - _drives_cache[0] < _DRIVES_TTL:
        return [dict(d) for d in _drives_cache[1]]

    out = _probe_drives()
    _drives_cache = (now, out)
    return [dict(d) for d in out]


def _list_dirs(path: Path) -> dict:
    """给"选输入"用的极简浏览器。子目录 + 当前层图片/素材包数量。

    素材包单独计数并单独列出：4TB 级素材常见形态就是"一个目录里几个大包"，
    只让用户选目录、却把包当成不存在，等于让他自己猜路径。

    包**不只 zip**：HF 上下的数据集（含我们自己发的 webdataset）都是 `.tar`，
    这里只认 zip 的话，选择器里那几个 tar 根本不出现，用户以为文件没下好。
    """
    from ..pack import container_kind

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
                                 if container_kind(p.name))
            except OSError:
                inner_zips = 0
            dirs.append({"name": entry.name, "path": str(entry), "images": images,
                         "zips": inner_zips})
        elif entry.is_file() and container_kind(entry.name):
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
        # 每次都给：盘符是这台机器的固定信息，让前端多记一份状态反而容易过期。
        "drives": _drives(),
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


# ---------------------------------------------------------------- 下载管理

# 下载队列是懒建的：模块导入时就起后台线程，会让每个测试进程都多一个
# 跑着的线程，也让 `tagger` 命令行在不需要下载时白占一份状态文件。
_downloads: HfDownloads | None = None
_downloads_lock = threading.Lock()


def downloads() -> HfDownloads:
    global _downloads
    with _downloads_lock:
        if _downloads is None:
            _downloads = HfDownloads(
                state_path=Path("run") / "hf-downloads.json")
        return _downloads


# ---------------------------------------------------------------- 计划任务

# 同样懒建：计划任务要起一个盯任务的线程，导入即启动会让测试进程凭空多一条。
_plan: "PlanWatcher | None" = None
_plan_lock = threading.Lock()


def plans() -> "PlanWatcher":
    global _plan
    from .plan import PlanWatcher

    with _plan_lock:
        if _plan is None:
            _plan = PlanWatcher(state_path=Path("run") / "plan.json")
        return _plan


class TagPreviewRequest(BaseModel):
    files: list[str] = Field(default_factory=list)
    limit: int = 5
    key: str = "image_id"
    # 空 = 保留产物自带的 image_path
    image_root: str = ""
    image_ext: str = ".jpg"


class PlanRequest(BaseModel):
    """跑到 N 张之后：停任务 / 关机。"""

    after_images: int = 0
    action: str = "stop"      # stop | shutdown
    enabled: bool = True


class TagMergeRequest(BaseModel):
    files: list[str] = Field(default_factory=list)
    out_path: str = ""
    # full = 保留字段并集（便于核对）；train = 只留四项（训练侧口径）
    format: str = "full"
    as_jsonl: bool = False
    key: str = "image_id"
    # first = 先读到的胜出；last = 后读到的覆盖
    on_conflict: str = "first"
    require: str = "any"
    indent: int = 2
    # 图片路径前缀：填了就覆盖成 <root>[/目录]/<image_id><ext>，留空保留原值
    image_root: str = ""
    image_ext: str = ".jpg"
    # 自检时把 image_path 的前缀换成这台机器上的实际目录
    local_root: str = ""
    # 0 = 自动（估算量大时自动分桶）；>0 = 强制分桶，每桶约这么多条
    batch_size: int = 0


class ConflictScanRequest(BaseModel):
    """扫一遍产物找冲突。roots 支持给多个目录（也支持只给父目录，递归扫）。"""

    roots: list[str] = Field(default_factory=list)
    limit: int = 100


class ConflictDecideRequest(BaseModel):
    image_id: str = ""
    kind: str = ""            # tag | nl
    # 选中那份内容的哈希；空串 = 撤销这条决定
    pick: str = ""


class ConflictAutoRequest(BaseModel):
    # manual = 什么都不自动做（默认）。newest / longest 是批量建议。
    strategy: str = "manual"
    # 已经人工批过的要不要被覆盖？默认不覆盖——人拍过的板不该被一键刷掉。
    overwrite: bool = False


class DownloadRequest(BaseModel):
    repo: str = ""
    token: str = ""
    revision: str = "main"
    files: list[str] = Field(default_factory=list)
    # 文件名 → 字节数。列表接口顺手带过来的 size 能省掉一次 HEAD。
    sizes: dict[str, int] = Field(default_factory=dict)
    # 给产物留的余量（GB）。0 = 沿用上一次设置
    reserve_gb: float = 0.0
    # 单个分片用几条并发连接。1 = 单连接；上限 16
    conns: int = 4


def _artist_context_default(enabled: bool, depth: int, inject: bool = True,
                            table: str = ""):
    """预览接口用的画师上下文。与任务执行时同一份配置口径。

    `table` 必须一起传：预览是"命名长什么样"的唯一防线，如果预览按目录名
    算、实际跑却查表，用户看到的 `alice-000001.jpg` 会变成 `ban_syou-000001.jpg`，
    而这正是预览存在的意义所在。
    """
    from .jobs import _artist_context

    spec = JobSpec(input_dir="", artist=enabled, artist_depth=depth,
                   artist_inject=inject, artist_table=table)
    return _artist_context(spec)


# ---------------------------------------------------------------- 应用

def create_app(*, ui_dist: Path | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 计划任务是懒建的，但服务一启动就得把线程拉起来：否则用户设了
        # "跑完关机"、关掉页面再重启服务，计划就再也不会触发了。
        try:
            plans()
        except Exception:               # noqa: BLE001 - 计划坏了不该让服务起不来
            pass
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
        # 多卡组的进程同样要收：留着的话每张卡都被占住，下次启动直接 OOM。
        try:
            if MULTI.snapshot()["status"] != "stopped":
                MULTI.stop()
        except Exception:               # noqa: BLE001
            pass
        # 量化的子进程同样要收：它比 vLLM 更贪显存，留着会让下一次启动直接 OOM。
        try:
            QUANT.cancel()
        except Exception:               # noqa: BLE001
            pass
        # 调度中心的 worker 子进程也要收：它手上可能还握着租约，进程留着不放，
        # 包会一直显示"有人在跑"，而那个"人"已经不是本机了。
        try:
            CENTER.shutdown()
        except Exception:               # noqa: BLE001
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

    # ---- 模型管理（NL 模型 / tag 分类头清单）

    @app.get("/api/models")
    def get_models() -> dict:
        """当前生效的清单 = configs/tiers.yaml + configs/models.json 覆盖。"""
        return MODELS.catalog()

    @app.put("/api/models")
    def put_models(req: ModelsRequest) -> dict:
        try:
            return MODELS.save(req.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"写不进覆盖文件：{exc}") from exc

    # ---- 模型权重的下载 / 删除 / 开目录
    # 下载是后台线程，前端靠轮询 GET /api/models 里的 download 字段画进度条。

    @app.post("/api/models/download")
    def start_model_download(req: ModelTarget) -> dict:
        try:
            # precision 可选：传了就只下这一档 + 配套 mmproj；不传按条目清单下。
            return MODELS.start_download(req.kind, req.key, req.precision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/models/download/cancel")
    def cancel_model_download(req: ModelTarget) -> dict:
        return MODELS.cancel_download(req.kind, req.key)

    @app.post("/api/models/localfiles/delete")
    def delete_model_files(req: ModelTarget) -> dict:
        try:
            return MODELS.delete_files(req.kind, req.key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"删不掉：{exc}") from exc

    @app.post("/api/models/reveal")
    def reveal_model_dir(req: ModelTarget) -> dict:
        try:
            return MODELS.reveal_files(req.kind, req.key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---- 下载代理

    @app.put("/api/models/proxy")
    def put_proxy(req: ProxyRequest) -> dict:
        try:
            return MODELS.set_proxy(req.url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"写不进覆盖文件：{exc}") from exc

    @app.post("/api/models/proxy/test")
    def test_proxy(req: ProxyRequest) -> dict:
        """探一次 HF：通不通、延迟多少。失败是正常结果，返回 ok=false 不抛。"""
        return MODELS.test_proxy(req.url)

    # ---- 下载源（镜像站）

    @app.put("/api/models/endpoint")
    def put_endpoint(req: EndpointRequest) -> dict:
        """空串 = 跟随 HF_ENDPOINT / 官方；只存前缀，不在这里探连通。"""
        try:
            return MODELS.set_endpoint(req.url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"写不进覆盖文件：{exc}") from exc

    @app.put("/api/models/conns")
    def put_conns(req: ConnsRequest) -> dict:
        """大文件下载连接数。0 = 用默认；下次下载生效（在跑的沿用旧值）。"""
        try:
            return MODELS.set_conns(req.conns)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"写不进覆盖文件：{exc}") from exc

    @app.get("/api/fs")
    def fs(path: str = "") -> dict:
        return _list_dirs(_start_path(path))

    @app.get("/api/preview")
    def preview(path: str = "", layout: str = "flat", artist: bool = True,
                artist_depth: int = 0, artist_table: str = "", recursive: bool = True,
                include_zips: bool = True, include_loose: bool = True,
                limit: int = 0, seq_width: int = 6, pack: bool = True, start_seq: int = 1,
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
            from ..artist import ArtistTable

            rule, aliases = _artist_context_default(artist, artist_depth,
                                                    table=artist_table)
            plan = plan_inputs(
                path, recursive=recursive, include_zips=include_zips,
                include_loose=include_loose, limit=max(0, limit),
                # 与 JobManager.create 同一口径：不封包就走旧 id
                assign_ids=pack, layout=layout if pack else "flat",
                seq_width=seq_width, start_seq=start_seq, artist_rule=rule, aliases=aliases,
                # 预览也要查表：否则"画师名会变成什么"与实际跑出来的不一样。
                artist_table=ArtistTable(rule.table),
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

    # ---- 新建任务页的参数记忆（上次填的那套，关掉程序再开还在）

    @app.get("/api/job-form")
    def get_job_form() -> dict:
        from . import ui_form

        return ui_form.load()

    @app.post("/api/job-form")
    def save_job_form(payload: dict) -> dict:
        """填一项存一项的入口：`form` 是页面快照，`spec` 只在提交成功时来。"""
        from . import ui_form

        form = payload.get("form") if isinstance(payload, dict) else None
        spec = payload.get("spec") if isinstance(payload, dict) else None
        if form is None and spec is None:
            raise HTTPException(400, "form / spec 至少给一个")
        if form is not None and not isinstance(form, dict):
            raise HTTPException(400, "form 必须是一个对象")
        if spec is not None and not isinstance(spec, dict):
            raise HTTPException(400, "spec 必须是一个对象")
        return ui_form.save(form=form, spec=spec)

    # ---- NL 提示词（仓库根 prompt/*.json）

    # 名字走 query 而不是路径：提示词名就是文件名，中文、空格、括号都合法，
    # 塞进 path 得先编码一遍，反而多一处出错的地方（与 presets 同一取舍）。

    @app.get("/api/prompts")
    def list_prompts() -> dict:
        """下拉框清单。目录不存在时返回空清单——那是"还没建"，不是错误。"""
        from .. import prompts as prompt_files

        return prompt_files.info()

    @app.get("/api/prompts/template")
    def prompt_template() -> dict:
        """新建时的初始内容：内置那份在当前配置下长什么样。

        对着空白框从零写一套指令不现实，从这里改才有意义。
        """
        from ..config import DEFAULT_TIERS, OutputSpec, load_ops, load_tier_table
        from ..client import SYSTEM_PROMPT, _prompt_for

        try:
            output = load_ops(load_tier_table(DEFAULT_TIERS)).output_spec()
        except Exception:                    # noqa: BLE001 - 配置坏了不该挡住这个入口
            output = OutputSpec()
        return {
            "prompt": _prompt_for(output),
            "prompt_zh": _prompt_for(output, zh=True),
            "system": SYSTEM_PROMPT,
        }

    @app.get("/api/prompts/one")
    def get_prompt(id: str) -> dict:          # noqa: A002 - 与前端字段同名
        from .. import prompts as prompt_files

        try:
            return prompt_files.detail(id)
        except prompt_files.PromptError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/prompts/one")
    def put_prompt(payload: dict, id: str) -> dict:   # noqa: A002
        from .. import prompts as prompt_files

        try:
            return prompt_files.save(id, payload)
        except prompt_files.PromptError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"写不进提示词文件：{exc}") from exc

    @app.post("/api/prompts", status_code=201)
    def create_prompt(payload: dict) -> dict:
        from .. import prompts as prompt_files

        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str):
            raise HTTPException(400, "新建提示词要给一个名字（name）")
        try:
            return prompt_files.create(name, payload)
        except prompt_files.PromptError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"写不进提示词文件：{exc}") from exc

    @app.post("/api/prompts/reveal")
    def reveal_prompts() -> dict:
        """在系统文件管理器里打开 prompt 目录（不存在就先建出来）。"""
        from .. import prompts as prompt_files

        try:
            return {"path": prompt_files.reveal()}
        except OSError as exc:
            raise HTTPException(500, f"打不开目录：{exc}") from exc

    # ---- 配置方案（顶栏下拉）：具名快照，可保存 / 切换 / 删除 / 自动保存
    # 名字用 query 传而不是放路径里：中文、空格、冒号都是合法方案名，
    # 塞进 path 就得先编码一遍，反而多一处出错的地方。

    @app.get("/api/presets")
    def list_presets() -> dict:
        from . import ui_presets

        return ui_presets.meta()

    @app.get("/api/presets/one")
    def get_preset(name: str) -> dict:
        from . import ui_presets

        got = ui_presets.payload(name)
        if got is None:
            raise HTTPException(404, "没有这个方案")
        return got

    @app.post("/api/presets")
    def save_preset(payload: dict) -> dict:
        from . import ui_presets

        name = payload.get("name") if isinstance(payload, dict) else None
        form = payload.get("form") if isinstance(payload, dict) else None
        spec = payload.get("spec") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "name 必填")
        if form is not None and not isinstance(form, dict):
            raise HTTPException(400, "form 必须是一个对象")
        if spec is not None and not isinstance(spec, dict):
            raise HTTPException(400, "spec 必须是一个对象")
        try:
            return ui_presets.save(name, form or {},
                                   spec if isinstance(spec, dict) else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/presets/apply")
    def apply_preset(payload: dict) -> dict:
        from . import ui_presets

        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "name 必填")
        got = ui_presets.apply(name)
        if got is None:
            raise HTTPException(404, "没有这个方案")
        return got

    @app.delete("/api/presets")
    def delete_preset(name: str) -> dict:
        from . import ui_presets

        return ui_presets.remove(name)

    @app.post("/api/presets/auto")
    def auto_preset(payload: dict) -> dict:
        """`enabled` 在就是开关自动保存；只带 `form` 就是"写一次当前配置"。"""
        from . import ui_presets

        body = payload if isinstance(payload, dict) else {}
        form = body.get("form")
        spec = body.get("spec")
        if form is not None and not isinstance(form, dict):
            raise HTTPException(400, "form 必须是一个对象")
        if "enabled" in body:
            return ui_presets.set_auto(bool(body.get("enabled")), form=form,
                                       spec=spec if isinstance(spec, dict) else None)
        got = ui_presets.autosave(form, spec if isinstance(spec, dict) else None)
        return got if got is not None else ui_presets.meta()

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        # 内存里的活任务 + 盘上的历史（重启后列表还在）。同 id 以内存为准。
        return {"jobs": MANAGER.all_snapshots()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        snap = MANAGER.snapshot_of(job_id)
        if snap is None:
            raise HTTPException(404, "没有这个任务")
        return snap

    @app.get("/api/jobs/{job_id}/events")
    def get_events(job_id: str, after: int = 0) -> dict:
        """WS 的兜底：同一份数据、同一条 cursor。

        历史任务（进程重启前的）没有事件流，回空表 + 快照：运行页据此渲染
        成"只读的已完成任务"，而不是一个 404 让人以为记录丢了。
        """
        job = MANAGER.get(job_id)
        if job is None:
            snap = MANAGER.snapshot_of(job_id)
            if snap is None:
                raise HTTPException(404, "没有这个任务")
            return {"events": [], "snapshot": snap}
        return {"events": job.events_since(after), "snapshot": job.snapshot()}

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str) -> dict:
        if not MANAGER.stop(job_id):
            raise HTTPException(409, "这个任务已经结束或不存在")
        job = MANAGER.get(job_id)
        return {"ok": True, "snapshot": job.snapshot() if job else None}

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str) -> dict:
        """暂停：在途的几张打完就挂起，产物照常落盘。"""
        try:
            if not MANAGER.pause(job_id):
                raise HTTPException(409, "这个任务没有在跑，无法暂停")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        job = MANAGER.get(job_id)
        return {"ok": True, "snapshot": job.snapshot() if job else None}

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict:
        """原地继续：进程还在，worker 只是挂着。"""
        if not MANAGER.resume(job_id):
            raise HTTPException(409, "这个任务不是暂停状态")
        job = MANAGER.get(job_id)
        return {"ok": True, "snapshot": job.snapshot() if job else None}

    @app.post("/api/jobs/{job_id}/restart", status_code=202)
    def restart_job(job_id: str) -> dict:
        """续跑一个已经停下的任务：同一套参数重开，已打完的图自动跳过。

        与 `/resume` 的分工：`resume` 叫醒还活着的 worker，`restart` 是
        线程已经退出、只能按原参数重来一个。两者对用户都叫"继续"。
        """
        try:
            return MANAGER.restart(job_id).snapshot()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/resume")
    def resume_state() -> dict:
        """上次没跑完的任务（暂停/停止后把程序关了）。页面打开时问一次。

        只回摘要，不回完整 spec：那份里有 API 密钥，而页面上要看的只是
        "哪个目录、做到第几张"。
        """
        data = MANAGER.wanted_resume()
        if data is None:
            return {"ok": False, "resume": None}
        spec = data.get("spec") or {}
        return {
            "ok": True,
            "resume": {
                "job_id": data.get("job_id"),
                "why": data.get("why"),
                "saved_at": data.get("saved_at"),
                "out_dir": data.get("out_dir"),
                "counts": data.get("counts") or {},
                "engine": spec.get("engine"),
                "input_dir": spec.get("input_dir"),
                "vlm_model": spec.get("vlm_model"),
                "output_mode": spec.get("output_mode"),
            },
        }

    @app.delete("/api/resume")
    def resume_dismiss() -> dict:
        """不再提示（只删记录，产物一张不动）。给"这个活儿我不打算继续了"用。"""
        MANAGER._clear_resume()
        return {"ok": True}

    @app.post("/api/resume", status_code=202)
    def resume_start() -> dict:
        """用 resume.json 重建任务并开跑。已打完的图由产物判定自动跳过。"""
        try:
            return MANAGER.resume_last().snapshot()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/results")
    def job_results(job_id: str, offset: int = 0, limit: int = 40,
                    q: str = "", only_failed: bool = False) -> dict:
        job = MANAGER.get(job_id)
        if job is None:
            # 历史任务：进程重启后 Job 没了，但产物还在盘上。结果读取只依赖
            # out_dir，按快照重建一个只读 Job 即可，不必另写一套路由。
            job = MANAGER.build_readonly(job_id)
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
            # 历史任务：细桶计数只在内存里，进程重启后就没了。这里仍然返回
            # 结构完整的空统计，而不是 404——统计页对"没有数据"和"没有这个
            # 任务"是两种文案，混起来会被当成链接坏了。
            job = MANAGER.build_readonly(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        return job.stats_buckets(bucket)

    # ---- 回传（FTP 上传）与 HF 文件清单

    @app.get("/api/artifacts")
    def artifacts(path: str = "") -> dict:
        """列出目录下可以回传的东西：产物分片（*.parquet / *.jsonl）与合并总表。

        rows 就是"这片里有多少张处理好的图"——sink 只写成功行，行数即成品数。
        成员的实际流程是"本地合并成自己的总表 → 发给服务器再合一次"，所以
        `merged.jsonl` 这种文件必须能选到，不能只认 artifacts-* 前缀。
        `failures.jsonl` 是失败账不是产物，排除。
        读不动的文件 rows=None，页面显示 "-"，不挡列表。"""
        if not path:
            raise HTTPException(400, "缺少 path")
        base = Path(path).expanduser()
        if not base.is_dir():
            raise HTTPException(404, f"目录不存在: {path}")
        files = []
        try:
            entries = sorted(
                [p for p in base.glob("*.parquet") if p.is_file()]
                + [p for p in base.glob("*.jsonl")
                   if p.is_file() and p.name not in _NON_ARTIFACT_NAMES],
                key=lambda p: p.name.lower())
            for p in entries:
                try:
                    st = p.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                files.append({"name": p.name, "bytes": size, "mtime": mtime,
                              "rows": shard_rows(p)})
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
        rule.table = (req.artist_table or "").strip() or rule.table
        aliases = AliasTable(rule.aliases_path).as_dict() if rule.aliases_path else {}
        from ..artist import ArtistTable

        reader = ArtistTable(rule.table)
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
            # 与 run_split 同口径：表优先、目录名兜底（分片表在分包阶段查不了，
            # 会静默退成目录名——run_split 那边有一条日志说明这件事）。
            got = reader.lookup(it.logical_path) if reader.active else []
            name = got[0] if got else extract_artist(it.logical_path, rule, aliases).tag
            if name:
                artists[name] = artists.get(name, 0) + 1
            else:
                no_artist += 1
            # 来源分布按"包 / 画师目录"聚合，而不是逐文件——散图语料
            # 一路扫下来每个文件都是自己的 source，逐文件列表没有信息量。
            src_key = it.source if it.is_member \
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
        except (ValueError, ConfigError) as exc:
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

    @app.get("/api/disk")
    def disk(path: str = "") -> dict:
        """目标路径所在卷的剩余空间。

        填预取片数 / 磁盘余量时要看的就是这个数。路径可能还不存在
        （用户正在敲），所以沿父目录往上找到第一个存在的位置再问。
        """
        import shutil

        raw = (path or "").strip() or "."
        probe = Path(raw).expanduser()
        try:
            probe = probe.resolve()
        except OSError:
            probe = Path(raw)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(str(probe))
        except OSError as exc:
            raise HTTPException(400, f"读不到 {probe} 的磁盘信息：{exc}") from exc
        return {
            "path": str(probe),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
        }

    @app.post("/api/hf/files")
    def hf_files(req: HfFilesRequest) -> dict:
        """仓库里的分片清单，带每个分片的大小。

        为什么带大小：下载页要靠它算"这一片放不放得下"、表单要靠它显示
        "全选会占多少 GB"。只给文件名的话，这两件事都得等下完才知道。

        token 只在本次请求内使用：不落盘、不进日志、不进任何任务状态。
        走 POST body 而不是 query，正是为了不让它进 access log。
        """
        from .. import hf_source

        repo = (req.repo or "").strip()
        if not repo:
            raise HTTPException(400, "缺少仓库名（形如 org/name）")
        token = (req.token or "").strip() or None
        revision = (req.revision or "main").strip() or "main"
        # 表单没填代理就用全局那份：代理是"这台机器怎么上网"，不该按页面各配一套
        proxy = (req.proxy or "").strip() or DOWNLOADS.snapshot().get("proxy", "")
        want = (req.format or "auto").strip().lower()
        if want not in ("auto", "parquet", "webdataset"):
            raise HTTPException(
                400, f"format 只能是 auto / parquet / webdataset，收到 {req.format!r}")
        try:
            if want == "auto":
                fmt = hf_source.detect_format(repo, revision=revision,
                                              token=token, proxy=proxy)
                if not fmt:
                    raise hf_source.HFError(
                        f"{repo} 里既没有 *.parquet 也没有 *.tar。确认它是 "
                        f"**dataset** 仓库（不是 model），且文件已经上传完。")
            else:
                fmt = want
            pattern = "*.parquet" if fmt == "parquet" else "*.tar"
            sized = hf_source.list_shards_sized(
                repo, revision=revision, pattern=pattern,
                token=token, proxy=proxy)
        except hf_source.HFError as exc:
            raise HTTPException(400, f"HF 访问失败：{exc}") from exc
        except Exception as exc:          # noqa: BLE001 - 网络/权限统一翻 400
            raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
        return {
            "repo": repo,
            "revision": revision,
            "format": fmt,
            "files": [name for name, _ in sized],
            "sizes": {name: size for name, size in sized},
            "total_bytes": sum(size for _, size in sized),
        }

    # ---- 打标管理（多个 json → 一个单体 json）

    @app.get("/api/tag-manage/scan")
    def tag_manage_scan(dir: str = "", recursive: bool = True) -> dict:
        """扫目录下的 json。只按文件名分类，不读内容——几万个文件也不卡。"""
        from .. import tag_manage as tm

        root = (dir or "").strip()
        if not root:
            raise HTTPException(400, "缺少目录")
        try:
            files = tm.scan(root, recursive=recursive)
        except tm.TagManageError as exc:
            raise HTTPException(400, str(exc)) from exc
        counts = {"tag": 0, "nl": 0, "parquet": 0, "other": 0,
                  "empty": 0, "bytes": 0}
        for row in files:
            # 用 setdefault：以后再加一种产物形态，这里不该再变成 KeyError
            counts[str(row["kind"])] = counts.get(str(row["kind"]), 0) + 1
            counts["bytes"] += int(row["size"])
            if row["empty"]:
                counts["empty"] += 1
        return {"root": root, "files": files, "counts": counts}

    @app.post("/api/tag-manage/preview")
    def tag_manage_preview(req: "TagPreviewRequest") -> dict:
        """合并前预览前几条。预览走的是真合并逻辑，不是随便读几行。"""
        from .. import tag_manage as tm

        if not req.files:
            raise HTTPException(400, "没有选中文件")
        rows = tm.preview(req.files, limit=max(1, min(20, req.limit)),
                          key=req.key or "image_id",
                          image_root=req.image_root,
                          image_ext=req.image_ext or ".jpg")
        fields: list[str] = []
        for row in rows:
            for name in row:
                if name not in fields:
                    fields.append(name)
        return {"rows": rows, "fields": fields}

    @app.post("/api/tag-manage/check", status_code=202)
    def tag_manage_check(req: "TagMergeRequest") -> dict:
        """合并后逐条核对 image_path 指向的图在不在本地。

        路径填错是**静默失败**：合并照样成功，直到训练侧读第一批才炸。
        所以宁可多花几秒先查一遍。

        几十万条在网络盘上是分钟级，挂在一个 HTTP 请求里就是"页面转圈、
        切页丢掉、超时算失败"——和合并同一个道理，所以也走后台 + 轮询。
        """
        from .. import tag_manage as tm
        from . import tag_merge_tasks

        if not req.files:
            raise HTTPException(400, "没有选中文件")

        def run(tick):
            return tm.check_paths(
                req.files, key=req.key or "image_id",
                image_root=req.image_root,
                image_ext=req.image_ext or ".jpg",
                local_root=req.local_root,
                on_progress=tick)

        return {"task_id": tag_merge_tasks.start(run, kind="check").id}

    @app.get("/api/tag-manage/check/{task_id}")
    def tag_manage_check_status(task_id: str) -> dict:
        from . import tag_merge_tasks

        task = tag_merge_tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "没有这个自检任务（服务重启过？）")
        return task.as_dict()

    @app.get("/api/activities")
    def activities() -> dict:
        """底部进度条的唯一数据源：所有"正在跑"的活儿，一种形态。

        刻意只返回进行中的条目：底部条要么有东西显示、要么整条不占位。
        打标/数据打包各自维护状态，这里只做归一化，不复制它们的语义。
        """
        from . import tag_merge_tasks

        items: list[dict] = []

        for job in MANAGER.all():
            if job.status not in ("pending", "running"):
                continue
            s = job.snapshot()
            counts = s.get("counts") or {}
            done = int(counts.get("done") or 0) + int(counts.get("skipped") or 0) \
                + int(counts.get("failed") or 0)
            total = int(counts.get("total") or 0)
            spec = s.get("spec") or {}
            items.append({
                "kind": "tag",
                "id": s["id"],
                "title": "打标",
                "detail": str(spec.get("input_dir") or ""),
                "phase": str(s.get("phase") or ""),
                "done": done,
                "total": total,
                "progress": s.get("progress"),
                "rate": s.get("rate") or 0,
                "eta_s": s.get("eta_s"),
                "href": f"/run/{s['id']}",
            })

        kind_label = {"split": "分包", "merge": "拼接", "pack": "封包"}
        for job in DATAJOBS.all():
            if job.status not in ("pending", "running"):
                continue
            s = job.snapshot()
            items.append({
                "kind": "data",
                "id": s["id"],
                "title": kind_label.get(s.get("kind", ""), "数据任务"),
                "detail": str(s.get("current") or ""),
                "phase": str(s.get("kind") or ""),
                "done": int(s.get("done") or 0),
                "total": int(s.get("total") or 0),
                "progress": None,
                "rate": s.get("rate") or 0,
                "eta_s": s.get("eta_s"),
                "href": "/packaging",
            })

        for task in tag_merge_tasks.running():
            items.append({
                "kind": task.kind,
                "id": task.id,
                "title": "自检" if task.kind == "check" else "合并",
                "detail": task.stage,
                "phase": task.stage,
                "done": task.done,
                "total": task.total,
                "progress": None,
                "rate": 0,
                "eta_s": None,
                "href": "/tag-manage",
            })

        # 量化与依赖安装也是"要等几分钟"的活，以前它们只在自己那个页面里有进度，
        # 底栏一片空白——用户切走后完全不知道还在不在跑。
        finished: list[dict] = []
        quant = QUANT.activity()
        if quant:
            running = quant["status"] == "running"
            entry = {
                "kind": "quantize",
                "id": quant["id"],
                "title": "量化",
                "detail": quant["model"],
                "phase": quant["stage"],
                # percent 当分子用：底栏三段里"45/100"读起来就是 45%，
                # 比显示 "--/--" 有用。
                "done": round(quant["percent"]),
                "total": 100 if running else 0,
                "progress": quant["percent"] / 100,
                "rate": 0,
                "eta_s": None,
                "href": "/models",
            }
            if running:
                items.append(entry)
            elif quant.get("finished_at"):
                finished.append({**entry, "status": quant["status"],
                                 "ok": quant["status"] == "done",
                                 "elapsed_s": quant["elapsed_s"],
                                 "error": "",
                                 "log_tail": quant.get("log_tail") or []})

        # 本地量化（safetensors → GGUF）同理：十几分钟起步，切走就看不见了。
        conv = GGUF_CONVERT.activity()
        if conv:
            running = conv["status"] == "running"
            entry = {
                "kind": "convert",
                "id": conv["id"],
                "title": "本地量化",
                "detail": conv["detail"],
                "phase": conv["stage"],
                "done": round(conv["percent"]),
                "total": 100 if running else 0,
                "progress": conv["percent"] / 100,
                "rate": 0,
                "eta_s": None,
                "href": "/models",
            }
            if running:
                items.append(entry)
            elif conv.get("finished_at"):
                finished.append({**entry, "status": conv["status"],
                                 "ok": conv["status"] == "done",
                                 "elapsed_s": conv["elapsed_s"],
                                 "error": "",
                                 "log_tail": conv.get("log_tail") or []})

        deps = DEPS.activity()
        if deps:
            running = deps["status"] == "running"
            entry = {
                "kind": "deps",
                "id": deps["id"],
                "title": "依赖安装",
                "detail": deps["detail"],
                "phase": "",
                "done": deps["done"],
                "total": deps["total"],
                "progress": None,
                "rate": 0,
                "eta_s": None,
                "href": "/deps",
            }
            if running:
                items.append(entry)
            elif deps.get("finished_at"):
                finished.append({**entry,
                                 "status": "ok" if deps["status"] == "done" else deps["status"],
                                 "ok": deps["status"] == "done",
                                 "elapsed_s": deps["elapsed_s"],
                                 "error": deps.get("error") or "",
                                 "log_tail": deps.get("log_tail") or []})

        return {"items": items, "finished": finished}

    @app.get("/api/sysinfo")
    def sysinfo() -> dict:
        """底栏右侧的四个数：CPU 占用/温度、GPU 占用/温度。

        读数在 sysinfo 模块里做了缓存（GPU 子进程 2s、CPU 用累计差），
        所以这里被 1 秒轮询一次也不会把机器压出额外开销。
        """
        from . import sysinfo as si

        return si.snapshot()

    @app.get("/api/plan")
    def get_plan() -> dict:
        return plans().snapshot()

    @app.put("/api/plan")
    def put_plan(req: PlanRequest) -> dict:
        """设置"跑到 N 张后停止/关机"。关机不可逆，页面必须二次确认。"""
        try:
            return plans().configure(after_images=req.after_images,
                                     action=req.action, enabled=req.enabled)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/plan")
    def delete_plan() -> dict:
        return plans().cancel()

    @app.post("/api/tag-manage/merge", status_code=202)
    def tag_manage_merge(req: "TagMergeRequest") -> dict:
        """把选中的 json 合并成一个大单体 json（后台跑，前台轮询进度）。

        校验留在同步这一段：参数错了要立刻 400，而不是让用户先拿到一个
        task_id、轮询两秒才发现"输出路径没填"。
        """
        from .. import tag_manage as tm

        from . import tag_merge_tasks

        if not req.files:
            raise HTTPException(400, "没有选中文件")
        out_path = (req.out_path or "").strip()
        if not out_path:
            raise HTTPException(400, "缺少输出文件路径")
        if not out_path.lower().endswith((".json", ".jsonl")):
            raise HTTPException(400, "输出文件请用 .json 或 .jsonl")
        batch = max(0, int(req.batch_size))

        def run(tick):
            return tm.merge_files(
                req.files, out_path,
                fmt=req.format, as_jsonl=req.as_jsonl,
                key=req.key or "image_id",
                on_conflict=req.on_conflict,
                require=req.require,
                indent=max(0, min(8, int(req.indent))),
                image_root=req.image_root,
                image_ext=req.image_ext or ".jpg",
                batch_size=batch,
                on_progress=tick)

        return {"task_id": tag_merge_tasks.start(run).id}

    @app.get("/api/tag-manage/merge/{task_id}")
    def tag_manage_merge_status(task_id: str) -> dict:
        from . import tag_merge_tasks

        task = tag_merge_tasks.get(task_id)
        if task is None:
            # 服务重启过、或 id 拼错。页面据此停止轮询。
            raise HTTPException(404, "没有这个合并任务（服务重启过？）")
        return task.as_dict()

    # ---- 冲突与决策：同一个 image_id 两份不同内容，逐条比、逐条批

    @app.get("/api/conflicts")
    def conflicts_state(offset: int = 0, limit: int = 100) -> dict:
        """读上次扫描的结果。没扫过返回 index=False，页面显示"先扫一遍"。"""
        from .. import conflicts as cf

        res = cf.load_index()
        if res is None:
            return {"index": False, "total": 0, "items": [],
                    "decisions": 0}
        payload = res.as_dict(offset=max(0, offset), limit=max(1, min(500, limit)))
        payload["index"] = True
        payload["decisions"] = res.decided_count
        return payload

    @app.post("/api/conflicts/scan")
    def conflicts_scan(req: "ConflictScanRequest") -> dict:
        """扫一遍产物，把"同 id 不同内容"的组合列出来。

        同步执行：冲突是**罕见**的（幂等重复不算），正常几秒就完。
        真遇到几十万条冲突的极端情况会慢，但那时候人也看不过来，
        不如先在命令行用 `tagger conflicts` 筛。
        """
        from .. import conflicts as cf

        roots = [r for r in (req.roots or []) if str(r).strip()]
        if not roots:
            raise HTTPException(400, "至少给一个产物目录")
        decided = cf.picks_for_merge()
        result = cf.scan(roots, decisions=decided)
        path = cf.save_index(result)
        payload = result.as_dict(offset=0, limit=max(1, min(500, req.limit)))
        payload["index"] = True
        payload["index_path"] = str(path)
        payload["decisions"] = result.decided_count
        return payload

    @app.post("/api/conflicts/decide")
    def conflicts_decide(req: "ConflictDecideRequest") -> dict:
        """批准某一条：记下选中的那份（哈希，不是下标）。"""
        from .. import conflicts as cf

        if not req.image_id or req.kind not in ("tag", "nl"):
            raise HTTPException(400, "需要 image_id 与 kind（tag / nl）")
        cf.decide(req.image_id, req.kind, req.pick or "")
        result = cf.load_index()
        return {"ok": True, "decisions": result.decided_count if result else 0}

    @app.post("/api/conflicts/auto")
    def conflicts_auto(req: "ConflictAutoRequest") -> dict:
        """按策略批量批准。manual 什么都不做——它只是"不自动"。"""
        from .. import conflicts as cf

        if req.strategy not in ("manual", "newest", "longest"):
            raise HTTPException(400, "strategy 只能是 manual / newest / longest")
        result = cf.load_index()
        if result is None:
            raise HTTPException(400, "还没有扫描结果，先扫一遍")
        out = cf.auto_decide(result, req.strategy,
                             only_undecided=not req.overwrite)
        out["decisions"] = result.decided_count
        return out

    # ---- 分片下载队列（下载页；与打标任务解耦，共用 run/hf-cache/）

    @app.get("/api/downloads")
    def downloads_state() -> dict:
        """队列快照 + 缓存目录 + 剩余空间 + 当前全局代理。前端每秒拉一次。"""
        from .models import proxy_url

        snap = DOWNLOADS.snapshot()
        # 代理是"这台机器怎么上网"的全局设置（与模型下载共用一份），
        # 下载页显示它，避免"以为设了、其实这里没走"。
        snap["proxy"] = proxy_url()
        return snap

    @app.get("/api/downloads/packs")
    def downloads_packs() -> dict:
        """本地已下载好的"离线包"清单，供打标页直接选用。

        每个 pack 就是缓存里的一个仓库目录：选了它，任务按**本地分片**跑，
        不再走网络——这正是"先下好、再开跑"的用法。
        """
        return {
            "cache_root": str(DOWNLOADS.cache_root),
            "packs": DOWNLOADS.packs(),
            "free_bytes": DOWNLOADS.snapshot().get("free_bytes", 0),
        }

    @app.get("/api/packs/shards")
    def packs_shards(path: str = "", out: str = "") -> dict:
        """离线包里的分片清单，附带每片已经产出多少行。

        打标页的分片面板靠它渲染状态：产物按包名定名（artifacts-<包名>-…），
        反解回去就知道哪片打过、打了多少。
        """
        from .pack_shards import scan

        key = (path or "").strip()
        if not key:
            raise HTTPException(400, "缺少离线包目录")
        try:
            return scan(key, (out or "").strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/downloads/cached")
    def downloads_cached(repo: str = "") -> dict:
        """这个仓库已经下到本地的分片（不含 .part 半截文件）。"""
        key = (repo or "").strip()
        if not key:
            raise HTTPException(400, "缺少仓库名")
        return {"repo": key, "files": DOWNLOADS.cached(key)}

    @app.post("/api/downloads/start", status_code=202)
    def downloads_start(req: DownloadsStartRequest) -> dict:
        if req.reserve_gb > 0:
            DOWNLOADS.set_reserve(int(req.reserve_gb * (1 << 30)))
        try:
            created = DOWNLOADS.submit(
                req.repo, req.files, sizes=req.sizes, token=req.token,
                revision=req.revision, conns=req.conns, proxy=req.proxy)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"created": created, "snapshot": DOWNLOADS.snapshot()}

    @app.post("/api/downloads/{item_id}/cancel")
    def downloads_cancel(item_id: str) -> dict:
        """取消/暂停一片。正在下的会保留 .part，再点一次从断点续传。"""
        if not DOWNLOADS.cancel(item_id):
            raise HTTPException(404, "没有这条下载")
        return {"ok": True, "snapshot": DOWNLOADS.snapshot()}

    @app.post("/api/downloads/cancel_all")
    def downloads_cancel_all() -> dict:
        DOWNLOADS.cancel_all()
        return {"ok": True, "snapshot": DOWNLOADS.snapshot()}

    @app.post("/api/downloads/clear")
    def downloads_clear() -> dict:
        """清掉已结束的条目。**不删磁盘上的分片**——那是打标任务的输入。"""
        return {"removed": DOWNLOADS.clear_finished(),
                "snapshot": DOWNLOADS.snapshot()}

    # ---- vLLM 服务（NL 模式的前置）

    @app.get("/api/vllm")
    def vllm_status() -> dict:
        """本机服务的状态 + 指纹 + 日志尾巴。页面每 4 秒拉一次。"""
        return VLLM.snapshot()

    @app.get("/api/vllm/capability")
    def vllm_capability(engine: str = "llamacpp") -> dict:
        """某个引擎在这台机器上能不能用。

        单独一个接口是因为"还没启动"时快照里的 local_ok 只能按当前引擎算，
        而页面上的引擎下拉一改，结论就该跟着变。塞进 /api/vllm 会让那个
        每 4 秒轮询的接口去调 nvidia-smi 两次。
        """
        try:
            ok, reason = VLLM.local_capability(engine)
        except ConfigError as exc:
            return {"engine": engine, "ok": False, "reason": str(exc)}
        return {"engine": engine, "ok": ok, "reason": reason}

    @app.post("/api/vllm/start", status_code=202)
    def vllm_start(req: VllmStartRequest) -> dict:
        try:
            return VLLM.start(port=req.port, timeout_s=req.timeout_s,
                              engine=req.engine,
                              model=req.model, tier=req.tier,
                              precision=req.precision,
                              served_name=req.served_name,
                              max_model_len=req.max_model_len,
                              max_num_seqs=req.max_num_seqs,
                              max_num_batched_tokens=req.max_num_batched_tokens,
                              gpu_mem=req.gpu_mem,
                              local_media_root=req.local_media_root,
                              slots=req.slots)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/vllm/plan")
    def vllm_plan(model: str = "", tier: str = "", precision: str = "",
                  engine: str = "",
                  max_model_len: int | None = None,
                  max_num_seqs: int | None = None,
                  max_num_batched_tokens: int | None = None,
                  gpu_mem: float | None = None,
                  slots: int | None = None) -> dict:
        """这组参数会解析成哪个分档 / 哪套权重。点启动之前就要能看见。

        参数与 /start 同名同义，只是不真的起进程。解析失败（模型 key 写错、
        卡不支持该精度）也在这里变成一句话，而不是几分钟后的一段 traceback。

        `precision` 必须透传：漏了它，页面选 int4 也会得到一份 bf16 的分档与
        显存预估，而那正是"选了 4bit 却还是 16G 卡 OOM"的来源。
        """
        return VLLM.plan(engine=engine or None,
                         model=model or None, tier=tier or None,
                         precision=precision or None,
                         max_model_len=max_model_len,
                         max_num_seqs=max_num_seqs,
                         max_num_batched_tokens=max_num_batched_tokens,
                         gpu_mem=gpu_mem,
                         slots_override=slots)

    @app.post("/api/vllm/stop")
    def vllm_stop() -> dict:
        return VLLM.stop()

    # ---- 页内量化（W4A16）
    #
    # 单独一组而不是塞进 /api/vllm/start：量化是几十分钟的长任务，与"起服务"
    # 的生命周期、取消语义、失败原因都不同。混在一起会让"启动失败"既可能
    # 是显存不够，也可能是量化还没跑完。

    @app.get("/api/vllm/quantize")
    def quantize_state(model: str = "", refresh: bool = False) -> dict:
        """能不能量化、有没有现成 4bit 权重、默认落哪个目录、当前进度。

        `refresh=1` 清掉可用性缓存：装完依赖回来要立刻看到"能了"，否则要等
        60 秒 TTL，用户只会以为安装没生效。
        """
        if refresh:
            QUANT.refresh_capability()
        return QUANT.snapshot(model or None)

    @app.post("/api/vllm/quantize/start", status_code=202)
    def quantize_start(req: QuantStartRequest) -> dict:
        try:
            return QUANT.start(model_key=req.model, out_dir=req.out_dir,
                               method=req.method)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/vllm/quantize/cancel")
    def quantize_cancel() -> dict:
        """取消：杀掉子进程组。半成品目录保留，下次会覆盖写。"""
        return QUANT.cancel() or {}

    # ---- 本地量化成 GGUF（原始权重 → F16 → Q4/Q6）
    #
    # 与上面那组并列而不是合并：那边产出 W4A16（给 vLLM），这边产出 GGUF
    # （给默认引擎 llama.cpp）。输入、依赖、耗时、登记方式全不一样，
    # 混成一个 "quantize" 只会让报错说不清是哪条路。

    @app.get("/api/gguf")
    def gguf_state(model: str = "") -> dict:
        return gguf_snapshot(model)

    @app.post("/api/gguf/convert", status_code=202)
    def gguf_convert_start(req: GgufConvertRequest) -> dict:
        try:
            return GGUF_CONVERT.start(req.model, req.target)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/gguf/cancel")
    def gguf_convert_cancel() -> dict:
        return GGUF_CONVERT.cancel() or {}

    @app.get("/api/vllm/probe")
    def vllm_probe(url: str = "") -> dict:
        """远程服务只能探：/health 说明活着，/v1/models 说明模型名对不对。"""
        return VLLM.probe(url)

    # ---- llama.cpp 运行时（默认引擎，llama-server 不在 pip 里）
    #
    # 页面上的「启动服务」失败时最常见的原因就是找不到 llama-server，而它
    # 不是一条 pip install 能解决的：官方只发 GitHub release 的 zip。这三条
    # 接口把那段折腾收进网页，与 `tagger llama install` 共用实现。

    @app.get("/api/llama")
    def llama_status() -> dict:
        """装没装、装在哪、正在装的话到哪一步了。

        刻意不联网：页面每几秒轮询一次，摸 GitHub 会让面板跟着卡。
        """
        snap = llama_runtime.status()
        # 装好了就顺手让能力判定失效：否则那条"找不到 llama-server"的缓存
        # 还在 TTL 里，「启动服务」会继续按不能用来显示。
        if snap["installed"]:
            VLLM.invalidate_capability()
        return snap

    @app.post("/api/llama/install")
    def llama_install(req: LlamaInstallRequest) -> dict:
        return llama_runtime.install(url=req.url.strip(), force=req.force)

    @app.post("/api/llama/install/cancel")
    def llama_install_cancel() -> dict:
        """取消。已下到一半的包会留成 .part，下次安装覆盖写。"""
        return llama_runtime.cancel()

    # ---- 外部推理 API（NL 打标的 openai / anthropic 来源）
    #
    # 这两条只服务"不走本地权重"的用法，所以与 /api/vllm/* 并列：那个是
    # "进程怎么起"，这个是"对方接口通不通"。

    @app.post("/api/nl-api/probe")
    def nl_api_probe(req: NlApiProbeRequest) -> dict:
        """发一张 8×8 的小图走一次真实请求：一次拿全地址/密钥/模型/视觉能力。"""
        try:
            provider = nl_api.normalize_provider(req.provider)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not provider:
            raise HTTPException(400, "本机模式不需要探测；请用 /api/vllm/probe")
        base, key = nl_api.resolve_credentials(provider, req.base, req.key)
        return nl_api.probe(
            provider=provider,
            base=base,
            model=req.model.strip(),
            api_key=key,
            timeout_s=max(5.0, min(120.0, float(req.timeout_s or 30.0))),
        )

    @app.get("/api/nl-api/keys")
    def nl_api_keys() -> dict:
        """已记住的密钥（脱敏）+ 价目是否存在。明文永远不出后端。"""
        saved = nl_api.load_saved()
        providers = {}
        for name in nl_api.PROVIDERS:
            if not name:
                continue
            entry = nl_api.saved_for(name)
            providers[name] = ({"base": entry.get("base", ""),
                                "key_masked": nl_api.mask_key(entry.get("key", ""))}
                               if entry else None)
        return {"providers": providers,
                "prices": sorted((saved.get("prices") or {}).keys())}

    @app.post("/api/nl-api/keys")
    def nl_api_keys_save(req: NlApiKeyRequest) -> dict:
        try:
            provider = nl_api.normalize_provider(req.provider)
            if not provider:
                raise ValueError("本机模式没有 API 密钥可存")
            if req.remember and req.key.strip():
                nl_api.store_key(provider, req.base, req.key)
            elif not req.remember:
                nl_api.store_key(provider, "", "")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"写不进 {nl_api.SAVED_CONFIG}：{exc}") from exc
        entry = nl_api.saved_for(provider) if provider else {}
        return {"provider": provider,
                "saved": ({"base": entry.get("base", ""),
                           "key_masked": nl_api.mask_key(entry.get("key", ""))}
                          if entry else None)}

    # ---- 多卡（数据并行）
    #
    # 与 /api/vllm/* 并列而不是塞进它：单卡服务是一个进程一份状态，
    # 多卡是一组进程一张表，字段结构完全不同。混在一起的话每个消费方
    # 都要先判断"这是哪一种"，页面上也会出现一堆对当前模式无意义的字段。

    @app.get("/api/multi")
    def multi_status() -> dict:
        """每张卡的状态、端口、已处理数、显存与利用率。页面每 4 秒拉一次。"""
        return MULTI.snapshot()

    @app.get("/api/multi/capability")
    def multi_capability(engine: str = "llamacpp") -> dict:
        """组多卡的前提：引擎装好、卡至少两张。顺带把卡列表给页面选。"""
        ok, reason, gpus = MULTI.capability(engine)
        return {
            "engine": engine,
            "ok": ok,
            "reason": reason,
            "gpus": [{"index": g.index, "name": g.name,
                      "vram_gb": g.vram_gb} for g in gpus],
        }

    @app.post("/api/multi/start", status_code=202)
    def multi_start(req: MultiStartRequest) -> dict:
        # 单卡服务占着的卡不能再给 worker：两个进程抢同一张卡时，谁先 OOM
        # 取决于加载顺序，而报错会指向模型，看不出是冲突。
        if VLLM.snapshot()["status"] in ("starting", "ready"):
            raise HTTPException(
                409, "单卡服务正在运行，会和多卡抢显存；先停掉单卡服务再启动多卡。")
        try:
            return MULTI.start(
                # 空值时给 llamacpp：这是 Windows 原生唯一能上 GPU 的引擎，
                # 也是页面默认值；Linux 上页面会显式传 vllm。
                engine=req.engine or "llamacpp",
                gpus=req.gpus,
                base_port=req.base_port,
                model=req.model, tier=req.tier, precision=req.precision,
                served_name=req.served_name or "tagger",
                per_card_seqs=req.per_card_seqs,
                timeout_s=req.timeout_s,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/multi/stop")
    def multi_stop() -> dict:
        return MULTI.stop()

    # ---- 部署自检
    #
    # 装完机器点一下，把"能不能干活"逐项跑一遍。与 /api/health 的分工：
    # health 回答"服务活着吗"，这里回答"它现在能不能真的打出一张图"。

    @app.get("/api/selfcheck")
    def selfcheck_state() -> dict:
        """进度快照。页面在弹窗里每 0.4 秒拉一次，所以这里必须无副作用。"""
        return SELFCHECK.snapshot()

    @app.post("/api/selfcheck/start", status_code=202)
    def selfcheck_start(req: SelfCheckRequest) -> dict:
        try:
            return SELFCHECK.start(input_dir=req.input_dir,
                                   recursive=req.recursive,
                                   sample=req.sample, url=req.url)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/selfcheck/stop")
    def selfcheck_stop() -> dict:
        return SELFCHECK.stop()

    # ---- 依赖检测
    #
    # 与「部署自检」的分工：自检回答"这些包能不能 import"，这里回答
    # "装了没、版本对不对、要不要现在动它"，并且能真的装/卸。

    @app.get("/api/deps")
    def deps_state() -> dict:
        return DEPS.snapshot()

    def _deps_guard() -> None:
        """装卸依赖会改 site-packages。正在跑的任务每隔几秒就要 import 一次
        （图片解码、pyarrow 写盘），装到一半的东西会让它以"找不到模块"崩掉，
        而那种报错看起来像代码 bug。所以直接拦住。"""
        if MANAGER.running() is not None:
            raise HTTPException(
                409, "有任务正在跑，装卸依赖会让它中途崩溃。先停下任务再来。")

    def _deps_call(fn, *args) -> dict:
        try:
            return fn(*args)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/deps/{name}/{action}", status_code=202)
    def deps_action(name: str, action: str) -> dict:
        if action not in ("install", "uninstall", "reinstall"):
            raise HTTPException(404, f"不认识的操作：{action}")
        _deps_guard()
        return _deps_call({"install": DEPS.install,
                           "uninstall": DEPS.uninstall,
                           "reinstall": DEPS.reinstall}[action], name)

    # ---- 批量
    #
    # 三个按钮共用一条串行队列：pip 互斥是硬约束（同时改 site-packages 会
    # 各自读到对方写了一半的目录），批量只是把多个包排进同一个循环。

    @app.post("/api/deps/install-missing", status_code=202)
    def deps_install_missing() -> dict:
        """装上所有「缺失」的包。不动版本不符的——那等于降级。"""
        _deps_guard()
        return _deps_call(DEPS.install_missing)

    @app.post("/api/deps/install-extra", status_code=202)
    def deps_install_extra(extra: str = "") -> dict:
        """装 pyproject 里声明的附加依赖（quantize / serve / parquet）。

        为什么要有这条：量化不可用时后端只会说"pip install 'tagger[quantize]'"，
        而那要求用户离开页面、找到环境里的解释器、在命令行敲——实际结果是
        这个功能对多数人等于不存在。附加依赖本来就在 pyproject 里写着，
        没理由不让人在页面上点一下。

        ★extra 走 query 而不是路径段：`/api/deps/{name}/{action}` 那条通用路由
        也占两段，写 `.../install-extra/quantize` 会被它先匹配走，然后报
        "不认识的操作"。路由顺序不该是这类 bug 的隐性依赖。
        """
        if not extra:
            raise HTTPException(400, "要指定 extra，例如 ?extra=quantize")
        _deps_guard()
        return _deps_call(DEPS.install_extra, extra)

    @app.post("/api/deps/uninstall-all", status_code=202)
    def deps_uninstall_all() -> dict:
        """卸掉所有已装的包（只限清单内的）。页面那边要求二次确认。"""
        _deps_guard()
        return _deps_call(DEPS.uninstall_all)

    @app.post("/api/deps/verify", status_code=202)
    def deps_verify() -> dict:
        """逐个真 import 一遍。只读，所以不拦正在跑的任务——
        它改的是内存里的检查结果，不碰 site-packages。"""
        return _deps_call(DEPS.verify_all)

    @app.post("/api/deps/cancel")
    def deps_cancel() -> dict:
        return DEPS.cancel()

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

    # ---- 调度中心（托管 fox-label v3 worker 子进程）

    @app.get("/api/center")
    def center_state() -> dict:
        return CENTER.snapshot()

    @app.get("/api/center/status")
    def center_brief() -> dict:
        # 顶栏的灯用这个：只读本地身份，不去问远端（见 Center.brief 的注释）。
        return CENTER.brief()

    @app.post("/api/center/login")
    def center_login(req: CenterLoginRequest) -> dict:
        try:
            CENTER.login(req.server, req.user_id, req.secret)
        except CenterError as exc:
            raise HTTPException(400, str(exc)) from exc
        return CENTER.snapshot()

    @app.post("/api/center/start", status_code=202)
    def center_start(req: CenterRunRequest) -> dict:
        try:
            return CENTER.start(**req.model_dump(exclude_none=True))
        except CenterError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/center/logout")
    def center_logout() -> dict:
        try:
            return CENTER.logout()
        except CenterError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/center/stop")
    def center_stop() -> dict:
        return CENTER.stop()

    # ---- 静态前端

    # 前端是客户端路由（/runjob、/downloads 这些路径磁盘上都没有对应文件），
    # 直接刷新或从外链进来会 404。除 /api 与 /ws 外的 404 一律交还 index.html，
    # 由前端自己解析路径。
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _spa_fallback(request, exc):       # noqa: ANN001 - Starlette 签名
        path = request.url.path
        if exc.status_code != 404 or path.startswith(("/api", "/ws")):
            # headers 必须原样带上：比如 405 会带 Allow，丢了浏览器和 curl 都看不出
            # "这个路径存在但方法不对"，只会以为它不存在。
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "界面还没构建：先跑 npm run build"},
                            status_code=404)

    @app.exception_handler(RequestValidationError)
    async def _bad_body(request, exc):           # noqa: ANN001 - Starlette 签名
        """请求体校验失败说人话，别提 ctx/input。

        FastAPI 默认把 `! model_dump 里的 ValueError(...)` 原样塞进 422 的 ctx，
        前端写错一个字段看到的是 `Value error, 数字不能是 NaN [type=value_error,
        input_value=nan, input_type=float]` —— 而前端只显示 detail，于是整页只剩
        一行看不懂的英文。这里摘出我们自己抛的那句，其余转成字段名+原因。
        """
        problems = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
            msg = err.get("msg") or "格式不对"
            # "Value error, 真正的说明" → 只留后半句
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            if err.get("type") == "json_invalid":
                problems.append("请求体不是合法的 JSON")
            elif not loc:
                problems.append(msg)
            else:
                problems.append(f"{loc}：{msg}")
        return JSONResponse({"detail": "；".join(problems) or "请求格式不对"},
                            status_code=422)

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
