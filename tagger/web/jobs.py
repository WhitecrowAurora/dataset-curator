"""任务簿：一个进程内的本地打标任务。

四个决定，都是为了让"单机单人用"这件事简单而不是炫技：

1. **任务跑在一条线程里，不搞进程池。** 打标本身是 GPU 串行，多进程只会
   把模型权重在显存里复制几份。线程还有个好处：`Ctrl+C` 与"停止"按钮
   走的是同一条路径。
2. **同一时刻只允许一个任务在跑。** 单卡上两个任务抢显存的结果是"两个都慢
   一半、日志互相穿插"，没有任何收益。后来的请求直接 409，让人等。
3. **事件是"能从头读的列表"，不是队列。** 前端断线重连只要带上 cursor
   就能补齐；队列在这里是负资产（重连即丢）。代价是内存里留一份环形缓冲，
   上限 5000 条——够看，也不会把一次百万张的任务撑爆。
4. **中止是协作式的。** 停止按钮只置一个 flag，`local_tag` 在块边界检查。
   正在算的那一块会算完（几十毫秒），但绝不会留下写了一半的产物。

两阶段
------
一个任务最多两个阶段：**打标 → 封包**。

- 打标阶段把每张图的 tag 落成 `<out>/tags/<image_id>-tag.json`，这是本地的
  中间产物，也是断点续跑的依据。
- 封包阶段在打标完整跑完之后才启动，读中间产物 + 原图，按配置封成
  parquet / tar / zip。它**不重新推理**，所以改质量/采样/格式只重封一遍，
  几十分钟的 GPU 时间不用再付。

为什么要分两阶段而不是边打边封：封包参数是"事后才知道要调"的东西，而且
tar/zip 都不支持追加——边打边封意味着改一个参数就要把模型重跑一次。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..artist import ArtistRule
from ..cpu_tagger import TaggerUnavailable, build_backend, resolve_device
from ..guard import ResourceGuard
from ..local_tag import TagItem, TagStats, read_artifact, tag_items
from ..nl_api import default_base as api_default_base
from ..nl_api import normalize_provider
from ..pack import PackOptions
from ..pipeline import (MemberReader, PackReport, PackSpaceError, Plan,
                        estimate_job_bytes, existing_ancestor, free_bytes,
                        pack_items, plan_inputs, plan_stats, space_error)
from .vllm_service import FINGERPRINT_PATH

# 环形缓冲上限。超出就丢最老的：UI 只关心最近的，补齐靠产物文件而不是事件。
MAX_EVENTS = 5000

# 「上次没跑完的任务」落盘位置。暂停或停止时写，正常跑完/失败时删。
# 为什么只存一条：单卡一次只跑一个任务，存列表只会让页面多一个"该继续哪个"
# 的选择题。产物目录 + 指纹才是真正的进度，这里只记"用哪份参数去续"。
#
# 环境变量是给测试用的：pytest 里的任务也会被 stop，而它们写的是临时目录。
# 不隔离的话，跑一次测试就会把用户页面上「继续上次任务」的提示顶成一条
# 指向 pytest 临时目录的假记录。用环境变量而不是 monkeypatch，是因为它
# 对子进程同样有效。
_resume_env = os.environ.get("TAGGER_RESUME_PATH", "").strip()
RESUME_PATH = Path(_resume_env) if _resume_env else Path("run") / "resume.json"

# ---- 质检分桶统计（统计页数据源）----
# 逐项数据不留存，事后改不了桶宽，所以累加取最细档（每 100 张一桶），
# 查询时按需合并成 500/1000/5000——都是 100 的倍数，合并是纯加法。
STAT_FINE = 100
# 质检分直方图：1.0~5.0 每 0.1 一格。低分/高分的阈值在前端可调，
# 后端只存原始分布，占比由前端现算，改阈值立刻全曲线生效。
SCORE_BINS = 41

_ROW_COLS = ("settled", "done_first", "done_retry",
             "skipped", "blacklisted", "failed", "score_sum")
_ROW_LEN = len(_ROW_COLS) + SCORE_BINS


def merge_buckets(rows: list[list[int]], bucket: int) -> list[dict]:
    """细桶行合并成 bucket 宽的粗桶字典。纯函数，方便单测。

    行布局见 _ROW_COLS：6 个计数器 + 累计质检分（×100 存整）+ 分数直方图。
    end 按真实结算数收口（尾桶常常不满），tooltip 里"第 N–M 张"不说大话。
    """
    step = max(1, bucket // STAT_FINE)
    out: list[dict] = []
    for i in range(0, len(rows), step):
        acc = [0] * _ROW_LEN
        for row in rows[i:i + step]:
            for j, v in enumerate(row):
                acc[j] += v
        start = i * STAT_FINE
        settled = acc[0]
        hist = acc[len(_ROW_COLS):]
        out.append({
            "start": start,
            "end": start + settled - 1,
            "settled": settled,
            "done_first": acc[1],
            "done_retry": acc[2],
            "skipped": acc[3],
            "blacklisted": acc[4],
            "failed": acc[5],
            "score_sum": acc[6] / 100.0,
            "score_hist": hist,
            "score_n": sum(hist),
        })
    return out


# VLM 引擎的三种产出模式。tag 模式是给"只要 tag、不想要 caption"的团队用的：
# 只出 tag 时 token 预算小得多，同样的卡能快一截。
VLM_MODES = ("nl", "tag", "both")


def vlm_backend_for(output_mode: str, backend: str) -> str:
    """VLM 模式下本地 tag 后端到底装不装。

    - nl  ：VLM 只出 NL。后端在场的话 run_client 仍会把它算出的 tag 写进产物
            （need_tags = wants_tag or 后端在场），等于用 CPU 偷偷多产一份没人
            要的 tag 文件——所以必须关。
    - tag ：tag 只能由 VLM 出。CPU 后端在场时 run_client 直接拒绝（它没有 NL
            可做，VLM 也轮不到出 tag），所以这里也强制 none。
    - both：用户选的后端说了算；空串 / "vlm" 表示 VLM 自己出 tag。
    """
    mode = str(output_mode or "both").strip().lower()
    name = (backend or "").strip().lower()
    if mode in ("nl", "tag"):
        return "none"
    return "none" if name in ("", "vlm") else name


# ---- 硬件保护的可调阈值 ----------------------------------------------------

# 白名单 = 允许表单覆盖的那几项，键名与 guard.GuardPolicy 的字段同名。
# 磁盘两项**不在**这里：它们默认归「盘位预算」管，再开一条侧门只会让
# "明明关了还在拦"变成一个查不出来的幽灵。
# 闭环参数（kp/ki/死区/最低占空比/最长休眠）本来是给团队内部调参用的，
# 现在也放上页面——团队自己的机器，改坏了改回来就行，藏起来反而要翻配置文件。
GUARD_KNOBS: dict[str, tuple[float, float]] = {
    # 占空比：1.0 = 全速，0.7 = 每忙 7 份睡 3 份
    "duty_cycle": (0.05, 1.0),
    # 温度闭环目标：0 = 不闭环，只靠下面几条硬线
    "target_temp_c": (0.0, 95.0),
    # PID 闭环的细调：kp 决定压得多快，ki 负责消掉稳态误差。
    # 两者都为 0 时闭环退化成"只在超出死区时按最低占空比跑"。
    "kp": (0.0, 1.0),
    "ki": (0.0, 0.5),
    # 死区：目标温度上下这么多度内不调整，避免占空比来回跳
    "temp_deadband_c": (0.5, 10.0),
    # 闭环最多把占空比压到哪；压到 0 等于停跑，留一条底线
    "min_duty": (0.02, 1.0),
    # 单次让步最长睡多久；太大时温度反应会过冲
    "max_sleep_s": (1.0, 120.0),
    "temp_warn_c": (30.0, 100.0),
    "temp_cutoff_c": (40.0, 110.0),
    "temp_resume_c": (30.0, 105.0),
    # 连续跑多久强制休息；0 = 不限
    "continuous_minutes": (0.0, 1440.0),
    "rest_minutes": (0.0, 240.0),
}


def normalize_guard_policy(raw: dict[str, float] | None) -> dict[str, float]:
    """表单里的阈值覆盖 → 干净字典。白名单外的丢掉，区间外的夹回来。

    夹而不是报错：滑条/数字框的正常输入本来就在区间里，夹一下顺手吸收
    "多打一个 0"这类手误。但**逻辑**关系不合法必须抛——恢复温度不低于熔断
    温度时，跑起来的表现是"一熔断就再也恢复不了"，在页面上长得和卡死一样。
    """
    out: dict[str, float] = {}
    for key, value in (raw or {}).items():
        name = str(key)
        if name not in GUARD_KNOBS:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"硬件保护参数 {name} 不是数字：{value!r}") from None
        if num != num or num in (float("inf"), float("-inf")):   # NaN / ±Inf
            raise ValueError(f"硬件保护参数 {name} 不是有限数：{value!r}")
        low, high = GUARD_KNOBS[name]
        out[name] = min(max(num, low), high)
    return out


def guard_policy_for(spec: "JobSpec"):
    """配置里的 limits 段 + 表单覆盖 → 实际生效的 GuardPolicy。

    配置读不到会抛 ConfigError：create() 里当成"不拦"，_make_guard() 里降级
    成"本次不启用保护"——两种情况都不该让一张本该打完的批次卡在 400 上。
    """
    from ..config import DEFAULT_TIERS, load_ops, load_tier_table
    from ..guard import GuardPolicy

    base = load_ops(load_tier_table(DEFAULT_TIERS)).guard_policy()
    overrides = normalize_guard_policy(spec.guard_policy)
    policy = replace(base, **overrides) if overrides else base
    if policy.temp_resume_c >= policy.temp_cutoff_c:
        raise ValueError(
            f"硬件保护：恢复温度（{policy.temp_resume_c:.0f}°C）必须低于熔断温度"
            f"（{policy.temp_cutoff_c:.0f}°C），否则一熔断就再也恢复不了")
    return policy


def describe_guard(policy) -> str:
    """一行摘要，进任务日志。页面上滑条显示的是"请求值"，这行是"生效值"——
    两者不一致时（比如笔记本自动收紧）以这行为准。"""
    parts = [f"占空比 {policy.duty_cycle:.2f}"]
    parts.append(f"闭环 {policy.target_temp_c:.0f}°C" if policy.target_temp_c
                 else "闭环 关")
    parts.append(f"熔断 {policy.temp_cutoff_c:.0f}°C/恢复 {policy.temp_resume_c:.0f}°C")
    if policy.continuous_minutes:
        parts.append(f"每 {policy.continuous_minutes:.0f} 分钟休 {policy.rest_minutes:.0f} 分钟")
    return " · ".join(parts)


def _hf_proxy(explicit: str) -> str:
    """任务没填就用设置里的全局代理。

    全局代理与下载页、模型下载是同一份（模型覆盖清单里的 `proxy`）：
    代理是"这台机器怎么上网"，不该按页面各配一份——三处不一致时，
    用户会以为"代理坏了"。
    """
    text = (explicit or "").strip()
    if text:
        return text
    try:
        from .models import proxy_url

        return proxy_url()
    except Exception:                    # noqa: BLE001 - 读不到就按直连
        return ""


def _effective_char_bounds(spec: "JobSpec") -> tuple[float, float]:
    """表单覆盖 + 配置默认 → 实际生效的 (最短, 最长) 字符数。

    只在 create() 里做区间校验用：最短 > 最长不会报错，只会让每条样本都被
    判"过长"而全部落进重试/丢弃，页面上只看到通过率 0。
    """
    lo, hi = 140.0, 1200.0
    try:
        from ..config import DEFAULT_TIERS, load_ops, load_tier_table

        p = load_ops(load_tier_table(DEFAULT_TIERS)).quality_policy()
        lo, hi = float(p.min_chars), float(p.max_chars)
    except Exception:                 # noqa: BLE001 - 配置坏了交给 guard 那条路降级
        pass
    if spec.quality_min_chars is not None:
        lo = float(spec.quality_min_chars)
    if spec.quality_max_chars is not None:
        hi = float(spec.quality_max_chars)
    return lo, hi


def _redact_url(text: str) -> str:
    """把 `scheme://user:pass@host:port` 里的口令抹掉，其余保留。

    代理地址里带账号密码很常见，而这个快照会随事件流广播给浏览器。
    """
    if not text or "@" not in text:
        return text
    head, _, tail = text.rpartition("@")
    scheme, sep, rest = head.partition("://")
    if not sep:
        return f"***@{tail}"
    if ":" in rest:
        rest = rest.split(":", 1)[0] + ":***"
    return f"{scheme}{sep}{rest}@{tail}"


def _redacted_spec(spec: "JobSpec") -> dict:
    """spec 快照。密钥类字段会随事件流广播给浏览器，绝不能原样出门。

    `api_key` 与 `hf_token` 同理：这个快照进了 job_started 事件，浏览器
    历史、日志面板、甚至排查时贴出来的截图都会把它带出去。
    """
    d = asdict(spec)
    if d.get("hf_token"):
        d["hf_token"] = "***"
    if d.get("api_key"):
        d["api_key"] = "***"
    if d.get("hf_proxy"):
        d["hf_proxy"] = _redact_url(str(d["hf_proxy"]))
    return d


def spec_provider(spec: "JobSpec") -> str:
    """本次任务走哪个推理来源：""（本机）/ openai / anthropic。

    认不出来的写法直接抛：静默退回本机会让人以为在用 API，产出却带着本机
    引擎指纹——这是这类配置里代价最高的一种误解。
    """
    return normalize_provider(spec.vlm_provider)


def spec_api_base(spec: "JobSpec") -> str:
    """API 地址。留空按来源取默认域名，页面上不必逼用户背地址。"""
    provider = spec_provider(spec)
    if not provider:
        return ""
    return str(spec.api_base or "").strip() or api_default_base(provider)


@dataclass(slots=True)
class JobSpec:
    """一次任务的全部参数。字段与表单一一对应，别在这里加"衍生值"。"""

    # vlm 引擎走 HF 仓库输入时可为空；本地引擎由 create() 显式校验
    input_dir: str = ""
    # 只打这些本地分片（绝对路径）。空 = 按 input_dir 整个目录走。
    # 离线包面板勾选后走这里：input_dir 仍是包目录（算产物目录、界面回显用），
    # 真正喂给 run_client 的输入清单以本字段为准。
    input_shards: list[str] = field(default_factory=list)
    out_dir: str = ""
    backend: str = "wd-eva02"
    model_dir: str = ""
    device: str = "auto"
    precision: str = "bf16"
    batch: int = 4
    # pil = CPU 缩放/归一化；tensor = 缩放/归一化上卡（解码仍在 CPU）。
    # 只有 wd-eva02 认这个参数，其余后端收了也不生效。
    preprocess: str = "pil"
    # None = 用配置里的阈值（configs/tiers.yaml 的 tagging.thresholds）
    threshold: float | None = None
    limit: int = 0
    force: bool = False
    recursive: bool = True

    # ---- 输入/命名

    # 从素材目录第一层取画师名：进 image_id、进归档 meta。
    artist: bool = True
    artist_depth: int = 0
    # 画师名写进 NL 正文（caption）开头，而不只是 JSON 字段。
    artist_inject: bool = True
    # 画师名写进 TAG 串开头。与上一项分开：NL 与 TAG 是两条训练输入，
    # 有人只要其一，绑成一个开关就表达不了。
    artist_inject_tags: bool = True
    # 画师对照表。空 = 跟配置（tiers.yaml 的 artist.table）走。
    # 三种写法：单个 parquet / 分片目录 / hf://datasets/<repo>/<文件或目录>。
    # 做成任务级开关而不是只认配置：同一台机器上经常要对着不同批次用不同的表
    # （换数据集、试在线版），改 yaml 会让上一个任务的口径跟着变。
    artist_table: str = ""
    # flat = 画师-000123.jpg（全部拍平）；mirror = 保留原目录结构
    layout: str = "flat"
    seq_width: int = 6
    # 包内成员（.zip / .tar / .tar.gz）是否算输入；散图是否算输入。默认都算。
    # 字段名沿用 include_zips（表单、历史任务快照里都是这个名字），语义是"素材包"。
    include_zips: bool = True
    include_loose: bool = True

    # ---- 封包（pack=True 时生效）

    pack: bool = False
    formats: list[str] = field(default_factory=lambda: ["parquet"])
    jpeg_quality: int = 95
    # 0 = 4:4:4（默认口径），1 = 4:2:2，2 = 4:2:0
    jpeg_subsampling: int = 0
    # always / non_jpeg / never
    reencode: str = "always"
    compress: str = "stored"
    max_records: int = 5000
    max_bytes: int = 0

    # ---- 引擎。local = 本地 tag 后端；vlm = vLLM 出 NL（tag 可由本地后端并行出）。

    engine: str = "local"

    # ---- VLM 服务（engine="vlm" 时生效）

    # 推理来源：local = 本机/自建服务（有环境指纹）；openai = OpenAI 兼容 API；
    # anthropic = Anthropic Messages API。API 模式不加载本地权重、不读指纹。
    vlm_provider: str = "local"
    # API 模式的服务地址与密钥。地址留空按来源取默认域名；密钥只在本次任务
    # 内存里走一趟，快照里会被抹成 ***（见 _redacted_spec）。
    api_base: str = ""
    api_key: str = ""
    url: str = "http://127.0.0.1:8000"
    vlm_model: str = "tagger"        # 对应 serve 的 --served-model-name
    # both = NL + tag；nl = 只要 NL；tag = 只要 tag（都由 VLM 自己出，后端强制 none）
    output_mode: str = "both"
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 0              # 0 = 用配置默认（run_client 里会按规格兜底抬高）
    timeout_s: int = 90
    # 在途请求数。0 = 跟服务端的并发走：vLLM 是 max_num_seqs，llama.cpp 是
    # -np 槽位（见 client.resolve_concurrency）。
    # 30M 量级下把它填成 1 等于让机器闲置着跑。
    concurrency: int = 0
    # 多卡（数据并行）。开着的时候每张卡各起一个推理服务，打标侧把它们
    # 当作 N 个并发槽轮询使用，产物写同一个目录——不需要事后合并。
    # 关掉时走原来的单服务路径，行为一个字不变。
    multi_gpu: bool = False
    # 参与打标的物理卡号（nvidia-smi 的 index）。空 = 用全部卡。
    multi_gpus: list[int] = field(default_factory=list)
    # 每张卡开几个 slot（= 该卡服务的 max_num_seqs / -np）。
    # 0 = 跟分档表与显存推算。填了就按填的来，只影响吞吐不影响产出。
    per_card_seqs: int = 0
    max_pixels: int = 0              # 0 = 用配置/指纹的默认预算
    # 描述长度引导（字符区间）。None = 按 token 区间派生（400-1024）。
    # 与 max_tokens 不同：max_tokens 是生成硬上限（写不下就截断），
    # 这个只告诉模型"往多长写"，不动合格线。改它会写进 provenance。
    caption_chars: list[int] | None = None
    # 中文 NL 的比例（0~1）。按 (seed, image_id) 抽签，抽中的图用中文写 caption；
    # tag 段永远英文。默认 0.3 = 主创定的口径（tiers.yaml 的 output 段同值）。
    # 0 = 全英文退回；1 = 全中文。
    caption_zh_ratio: float = 0.3
    # NL 提示词配置文件的名字（prompt/<名字>.json 去掉后缀）。空 = 内置那份，
    # 按 caption_chars / 比例生成。文件内容整段替换内置提示词，provenance 里
    # 记 prompt 文本的 sha，所以改名不影响"这批数据同不同源"。
    prompt_id: str = ""
    seed: int | None = None
    # 同一张图跨运行累计失败多少次后跳过。0 = 不限制。
    # 这个数不复用会出问题：一张必然失败的图每次重跑都要重试 3 次，
    # 30M 量级下这类样本的浪费是小时级的。
    max_failures: int = 3
    # 质检判定线与重打次数。0 = 用配置（pass 3.4 / drop 1.8 / 重打 2 次）。
    # 每次重打都是一次完整的推理，30M 量级下这几个数直接决定要跑几天。
    quality_pass_score: float = 0.0
    quality_drop_score: float = 0.0
    quality_max_attempts: int = 0
    # 重试阶梯：温度每轮上调多少 / 封顶，像素预算乘几倍 / 封顶。
    # 0 = 用配置。抬分辨率要求服务端 max_pixels 留了余量（见 run_client 的警告）。
    quality_retry_temp_step: float = 0.0
    quality_retry_temp_cap: float = 0.0
    quality_retry_pixels_scale: float = 0.0
    quality_retry_pixels_cap: int = 0
    # L2 语义打分（图文 CLIP）。开启后质检能抓"句子通顺但跑题"，
    # 代价是必须装 open_clip_torch、吃一份显存，且并发会被摁到 1。
    quality_enable_clip: bool = False
    quality_clip_min_score: float = 0.0
    # CLIP 权重与预训练来源。空 = 跟配置（ViT-L-14 / openai）。
    # 换它们等于换打分口径：两批数据的分数不能直接比。
    quality_clip_model: str = ""
    quality_clip_pretrained: str = ""
    # L1 规则层阈值。None = 跟配置（最短 140 / 最长 1200 / 覆盖率 0.60 / 重复率 0.35）。
    # 这里**不能**用 0 当"跟配置"的哨兵：0 是合法值——最短 0 字 = 不设下限，
    # 重复率 0 = 一个 4-gram 都不许重复。用 0 表示"没填"会让这两项设不了。
    quality_min_chars: int | None = None
    quality_max_chars: int | None = None
    quality_min_tag_coverage: float | None = None
    quality_max_repeat_ratio: float | None = None
    # 重试升级：重试用尽仍不合格时打到第二个服务（另一套更大的权重）。
    # vLLM 一个进程只服务一套权重，所以只能是另一个地址；留空 = 不升级。
    escalate_url: str = ""
    escalate_model: str = ""
    # file:// 传图。要求服务端用 --allowed-local-media-path 放行产物目录，
    # 否则每张图都是 400。只在客户端与服务端同机同路径时有意义。
    use_file_url: bool = False

    # ---- HF 输入（engine="vlm" 时 hf_repo 优先；两者都没有才报错）

    hf_repo: str = ""
    hf_token: str = ""
    hf_revision: str = "main"
    # 只处理这些文件名（精确匹配）；空 = 仓库里全部分片
    hf_files: list[str] = field(default_factory=list)
    # auto：仓库里有 parquet 就流式读（一行不落盘），否则按 tar 分片下载。
    # 两种模式的磁盘行为完全不同，所以让用户能看见并显式选。
    hf_format: str = "auto"
    # tar 模式：预取几片（下载与打标并行，0=严格串行）
    hf_prefetch: int = 1
    # 单片用几条并发连接（国内单条 TCP 常被限速，多连接才跑得满）
    hf_conns: int = 4
    # HF 请求走的代理。空 = 用设置里的全局代理（与下载页共用一份）
    hf_proxy: str = ""
    # 给产物留的磁盘余量（GB）；0 = 自动（最大分片的 30%，不少于 2 GB）
    hf_reserve_gb: float = 0.0

    # 结果缓存：同图同参直接复用上次产出（重跑/换输出目录才划算，默认关）
    cache: bool = False
    cache_max_gb: float = 8.0

    # ---- 产物形态。本地默认 jsonl：一包一文件，回传时一人发自己的那份。

    artifact_format: str = "jsonl"
    parquet_rows: int = 2000

    # ---- tag 后端补充（两种引擎通用）

    # 空 = 用配置的 keep_categories（DEFAULT_KEEP）
    keep_categories: list[str] = field(default_factory=list)
    # 类别 → 阈值；空 = 用配置。与 threshold 滑条合并（滑条覆盖 default）
    thresholds: dict[str, float] = field(default_factory=dict)
    # 覆盖后端的 HF 仓库名；空 = 用后端默认
    model_repo: str = ""

    # ---- 硬件保护（温度闭环 / 占空比 / 磁盘熔断 / 连续运行休息）

    guard: bool = True
    # 阈值覆盖：键名与 guard.GuardPolicy 字段同名（白名单见 GUARD_KNOBS）。
    # 空 = 全跟 configs/tiers.yaml 的 limits 段走。表单默认值就是从那段读的，
    # 所以"没动过的滑条"不会把配置改掉。
    guard_policy: dict[str, float] = field(default_factory=dict)
    # 开跑前/封包中按"预计产出 + 20% 冗余"判盘够不够。
    # 与 guard 的固定阈值（剩 30G 就停）是两件事：后者对小批量太严、
    # 对大批量太松，所以磁盘这一项默认改走估算，guard 只管温度/占空比。
    check_space: bool = True

    def resolved_out_dir(self) -> Path:
        if self.out_dir:
            return Path(self.out_dir)
        if self.engine == "vlm" and not self.input_dir and not self.input_shards:
            # HF 输入没有本地素材目录可参照，落 CLI client 的默认
            return Path("work/out")
        if not self.input_dir and self.input_shards:
            # 只勾了分片、没给包目录：按这些片所在的目录起名。拿 `shard-000005`
            # 当产物目录名会让勾十片散出十个目录，之后自己都找不回来。
            src = Path(self.input_shards[0]).resolve().parent
            return src / f"{src.name}-tags"
        # 默认落在素材目录**旁边**：往素材目录里写产物会让下次扫描把它自己
        # 也扫进去（rename 成 .json 之前还是 .png），也容易误删。
        src = Path(self.input_dir).resolve()
        # 输入是单个 zip 时用去掉扩展名的名字：`pack.zip` → `pack-tags`
        base = src.name if src.is_dir() else (src.stem or src.name)
        return src.parent / f"{base}-tags"

    def backend_thresholds(self) -> dict[str, float] | None:
        """滑条（threshold）与按类别阈值（thresholds）合并成 build_backend 的入参。"""
        merged = {str(k): float(v) for k, v in (self.thresholds or {}).items()}
        if self.threshold is not None:
            merged["default"] = float(self.threshold)
        return merged or None

    def pack_options(self) -> PackOptions | None:
        """把封包字段折成 PackOptions。校验失败抛 ConfigError。"""
        if not self.pack:
            return None
        return PackOptions.from_dict({
            "formats": list(self.formats),
            "compress": self.compress,
            "jpeg_quality": self.jpeg_quality,
            "jpeg_subsampling": self.jpeg_subsampling,
            "reencode": self.reencode,
            "output_max_records": self.max_records,
            "output_max_bytes": self.max_bytes,
        })

    def naming_fingerprint(self) -> dict:
        """会改变产出内容/命名的口径。进封包指纹，避免旧记忆被当成"这批已封"。

        - artist / depth / seq_width：改的是 id 空间（同一张图换个名字）；
        - artist_inject：改的是 .tag/.nl 文本内容（同 id 不同内容）。
        两类都会让旧分片"看起来已完成、实际对不上"，所以一起进指纹。
        """
        return {
            "artist": bool(self.artist),
            "artist_depth": int(self.artist_depth or 0),
            "seq_width": int(self.seq_width),
            "artist_inject": bool(self.artist and self.artist_inject),
        }

    def artist_rule(self, *, prefix: str = "@", aliases_path: str | None = None,
                    table: str | None = None,
                    inject_caption: bool | None = None,
                    inject_tags: bool | None = None) -> ArtistRule:
        """画师规则。enabled/depth/注入 由本任务的开关决定，其余口径跟配置走。

        为什么不让配置的 artist.enabled 直接决定：封包任务需要画师名进
        image_id（`画师-000123`），而配置里那个开关的语义是"要不要注入
        caption"。两件事被一个开关管，用户在界面上就永远说不清自己选了
        什么。注入开关单独由 artist_inject 管，传了 inject_caption 就以它为准
        （VLM 路径要用配置里的同一个值）。
        """
        return ArtistRule(
            enabled=bool(self.artist),
            depth=max(0, int(self.artist_depth or 0)),
            prefix=prefix,
            inject_caption=(bool(self.artist_inject) if inject_caption is None
                            else bool(inject_caption)),
            inject_tags=(bool(self.artist_inject_tags) if inject_tags is None
                         else bool(inject_tags)),
            aliases_path=aliases_path,
            # 画师对照表跟配置走：任务表单只管"要不要注入"，不管"名字从哪来"。
            table=table,
        )


# VLM 任务的结果不在 Job.items 里（那个列表只有本地引擎会填），而是落在
# 产物目录。结果页每次都要一个 total，而 jsonl 动辄几十万行，所以按「产物
# 文件签名」缓存一次行数；文件一追加，size/mtime 变了自然失效。
_ARTIFACT_TOTAL_CACHE: dict[tuple, int] = {}
# 失败账去重后的条数，同样按文件签名缓存——结果页每次请求都要它。
_FAILURE_TOTAL_CACHE: dict[tuple, int] = {}


def _count_lines(path: Path) -> int:
    n = 0
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
    except OSError:
        return 0
    return n


def _append_fine_row(job_id: str, row: list[int]) -> None:
    """把一个满桶追加到 run/history/<id>.stats.jsonl。"""
    from . import history as history_store

    history_store.append_stats(job_id, row)


class Job:
    """一个任务的全部状态。字段读写都在锁里，因为线程在跑、HTTP 在读。"""

    def __init__(self, job_id: str, spec: JobSpec, plan: Plan | None,
                 *, options: PackOptions | None = None,
                 reader: MemberReader | None = None):
        self.id = job_id
        self.spec = spec
        self.plan = plan
        items = plan.items if plan is not None else []
        self.items: list[TagItem] = items
        self.by_id: dict[str, TagItem] = {it.image_id: it for it in items}
        self.out_dir = spec.resolved_out_dir()
        self.options = options
        self.reader = reader or MemberReader()
        # 本任务的 ResourceGuard（_run 里构造；None = 用户关了保护）
        self.guard = None
        # vlm 引擎的实时计数（hook 每次都带同一个 ClientStats 实例，存引用即可）
        self.client_stats = None
        # pending | running | paused | done | stopped | failed
        self.status = "pending"
        # "" | tag | pack
        self.phase = ""
        self.error = ""
        self.created = time.time()
        self.started = 0.0
        self.ended = 0.0
        # 第一张真正产出时刻：速率分母从这里起算，跳过模型加载那几秒。
        self.first_settled = 0.0
        self.pack_started = 0.0
        self.pack_first = 0.0
        self.stats = TagStats(total=len(self.items))
        # 封包进度原地更新的计数（pack_items 的 report 在结束时才完整，
        # 但界面要边跑边看，所以这些字段单独维护）。
        self.packed = 0
        self.pack_skipped = 0
        self.pack_missing = 0
        self.pack_failed = 0
        self.pack_bytes = 0
        self.pack_report: PackReport | None = None
        self.packs: list[dict] = []
        self.model_id = ""
        self.current = ""
        self.seq = 0
        self._events: list[dict] = []
        # 分桶统计的细桶行（布局见 _ROW_COLS）；note_item 在锁里追加
        self._fine: list[list[int]] = []
        # 已经落盘的桶数：满桶立刻追加，最后不满的一桶等终态再补（见 _flush_fine）
        self._fine_saved = 0
        self._lock = threading.Lock()
        self.stop_flag = threading.Event()
        # 暂停：与 stop_flag 分开两个标志，因为语义完全不同——stop 是"收工"，
        # pause 是"原地不动，等我回来"。合成一个的话，恢复时无法区分
        # "用户想继续"和"用户只是没按停止"。
        self.pause_flag = threading.Event()

    # ---- 事件流

    def push(self, kind: str, **fields) -> None:
        with self._lock:
            self.seq += 1
            event = {"seq": self.seq, "ts": time.time(), "kind": kind, **fields}
            self._events.append(event)
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]

    def events_since(self, after: int) -> list[dict]:
        """取 seq > after 的事件。前端断线重连靠它补齐。"""
        with self._lock:
            if not self._events:
                return []
            first = self._events[0]["seq"]
            if after < first - 1:
                after = first - 1          # 缓冲已经滚掉了，只能从最早的开始
            return self._events[max(0, after - first + 1):]

    # ---- 分桶统计

    def note_item(self, kind: str, *, attempts: int = 1,
                  score: float | None = None) -> None:
        """逐项统计入口，两个引擎的结算 hook 都要调。

        kind ∈ done/failed/skipped/blacklisted；attempts 用来区分首过与
        重试过（本地引擎没有重试，恒为 1）；score 是质检分（1~5，仅 VLM）。
        """
        with self._lock:
            if not self._fine or self._fine[-1][0] >= STAT_FINE:
                if self._fine:
                    # 上一桶满了：立刻落盘。100 张才写一次 1KB，持着锁做个追加
                    # 不亏；这样进程被硬杀也只丢最后不满的那一桶。
                    _append_fine_row(self.id, self._fine[-1])
                    self._fine_saved = len(self._fine)
                self._fine.append([0] * _ROW_LEN)
            row = self._fine[-1]
            row[0] += 1
            if kind == "done":
                row[1 if attempts <= 1 else 2] += 1
            elif kind == "skipped":
                row[3] += 1
            elif kind == "blacklisted":
                row[4] += 1
            else:
                row[5] += 1
            if score is not None:
                row[6] += int(round(min(5.0, max(1.0, float(score))) * 100))
                # 越界截到边上：直方图只用于看占比，不用于复原原值
                bin_ = int(round((min(5.0, max(1.0, float(score))) - 1.0) * 10))
                row[len(_ROW_COLS) + bin_] += 1

    def stats_buckets(self, bucket: int = 1000) -> dict:
        """统计页数据：完成顺序分桶 + 全程汇总。

        名字避开实例属性 self.stats（TagStats）。百分比不在这里算——
        低分/高分阈值是前端可调的，直方图原样出门。
        """
        with self._lock:
            rows = [list(r) for r in self._fine]
        snap = self.snapshot()
        counts = snap["counts"]
        fallback = False
        if rows:
            buckets = merge_buckets(rows, bucket)
        elif counts["settled"]:
            # 统计功能上线前的旧任务没有逐项数据：用当前计数合成单桶，
            # 图上是一个点，前端会提示"只有整体汇总"。
            fallback = True
            buckets = [{
                "start": 0, "end": counts["settled"] - 1,
                "settled": counts["settled"],
                "done_first": counts["done"], "done_retry": 0,
                "skipped": counts["skipped"],
                "blacklisted": counts["blacklisted"],
                "failed": counts["failed"],
                "score_sum": 0.0, "score_hist": [0] * SCORE_BINS, "score_n": 0,
            }]
        else:
            buckets = []
        # 直方图全局截尾：分数集中在 2~4.5，尾部全零格不值得出门
        cut = 0
        for b in buckets:
            for i in range(len(b["score_hist"]) - 1, -1, -1):
                if b["score_hist"][i]:
                    cut = max(cut, i)
                    break
        for b in buckets:
            b["score_hist"] = b["score_hist"][:cut + 1]
        keys = ("settled", "done_first", "done_retry", "skipped",
                "blacklisted", "failed", "score_sum", "score_n")
        totals = {k: sum(b[k] for b in buckets) for k in keys}
        totals["score_hist"] = [
            sum(b["score_hist"][i] for b in buckets if i < len(b["score_hist"]))
            for i in range(cut + 1)
        ]
        return {
            "id": self.id, "status": snap["status"],
            # 老快照没有 counts（或早期 VLM 版本没记 total）：总数不该小于已
            # 结算数，否则页面上会出现"0 张里完成了 250 张"。
            "total": max(counts["total"], totals["settled"]),
            # settled 用桶汇总：与图上的数字同源，页面内部永远自洽
            "settled": totals["settled"],
            "bucket": bucket, "buckets": buckets, "totals": totals,
            # true = 旧任务的合成单桶，没有真正的分桶趋势
            "fallback": fallback,
        }

    # ---- 快照

    def snapshot(self) -> dict:
        with self._lock:
            stats = self.stats
            seq = self.seq
            events = len(self._events)
            packed = self.packed
            pack_skipped = self.pack_skipped
            pack_missing = self.pack_missing
            pack_failed = self.pack_failed
            pack_bytes = self.pack_bytes
            packs = list(self.packs)
            client_stats = self.client_stats

        # vlm 引擎的实时计数在 ClientStats 里（流式任务的 total 逐张累加）；
        # local 引擎用 TagStats。两边的字段名刚好同形。
        if self.spec.engine == "vlm" and client_stats is not None:
            cs = client_stats
            done, skipped, failed, total = cs.done, cs.skipped, cs.failed, cs.total
            blacklisted = cs.blacklisted
            busy = cs.busy
            by_reason = dict(cs.by_reason)
            usage = {"prompt_tokens": cs.prompt_tokens, "completion_tokens": cs.tokens,
                     "rate_limited": cs.rate_limited, "cost": cs.cost}
        else:
            done, skipped, failed, total = (stats.done, stats.skipped,
                                            stats.failed, stats.total)
            blacklisted = 0
            busy = stats.busy
            by_reason = dict(stats.by_reason)
            usage = {"prompt_tokens": 0, "completion_tokens": 0,
                     "rate_limited": 0, "cost": None}
        settled = done + skipped + failed + blacklisted
        elapsed = (self.ended or time.time()) - self.started if self.started else 0.0
        # 速率只按"真正打过"的算：跳过的图不花时间，算进去会把 ETA 说得太好听。
        # 分母从**第一张产出**起算，而不是 started：后者含模型加载（本机实测
        # 6~8s），头几秒 rate 会被压到 0.2 张/s，界面就把 ETA 报成"还剩 1 小时"。
        # 也不改用纯前向耗时——那不含写盘，会反过来偏乐观。首张那段算进分子
        # 不算进分母，800 张量级下误差 < 0.2%，可以忽略。
        run_started = self.first_settled or self.started
        run_elapsed = (self.ended or time.time()) - run_started if run_started else 0.0
        rate = done / run_elapsed if run_elapsed > 0 and done else 0.0
        remaining = max(0, total - settled)
        # 流式任务（HF parquet）总量事先未知：eta/progress 都给 None，
        # 前端按"计数 + 速率"显示，不硬造百分比。
        eta = remaining / rate if rate > 0 and total else None

        pack_total = total if (self.options is not None and total) else 0
        pack_settled = packed + pack_skipped + pack_missing + pack_failed
        if self.phase == "pack" and pack_total:
            progress: float | None = min(1.0, pack_settled / pack_total)
        elif total:
            progress = min(1.0, settled / total)
        else:
            progress = None

        pack_rate = 0.0
        if self.phase == "pack":
            p_start = self.pack_first or self.pack_started
            p_elapsed = (self.ended or time.time()) - p_start if p_start else 0.0
            pack_rate = packed / p_elapsed if p_elapsed > 0 and packed else 0.0

        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "error": self.error,
            "spec": _redacted_spec(self.spec),
            "out_dir": str(self.out_dir),
            "counts": {
                "total": total,
                "done": done,
                "skipped": skipped,
                "failed": failed,
                "blacklisted": blacklisted,
                "busy": busy,
                "settled": settled,
            },
            "progress": progress,
            "elapsed_s": round(elapsed, 1),
            "rate_per_s": round(rate, 2),
            "eta_s": round(eta, 1) if eta is not None else None,
            "timings_ms": {"decode": stats.decode_ms, "forward": stats.forward_ms},
            # 云端按量计费：token 与撞限流次数要能在运行中看到，不能等日志。
            # cost 为 None = 没配价目（configs/nl_api.json 的 prices 段）。
            "usage": usage,
            "by_reason": by_reason,
            "pack": {
                "enabled": self.options is not None and pack_total > 0,
                "total": pack_total,
                "packed": packed,
                "skipped": pack_skipped,
                "missing": pack_missing,
                "failed": pack_failed,
                "settled": pack_settled,
                "bytes": pack_bytes,
                "rate_per_s": round(pack_rate, 2),
                "formats": list(self.options.formats) if self.options else [],
                "layout": self.spec.layout,
                "quality": self.spec.jpeg_quality,
                "subsampling": self.spec.jpeg_subsampling,
                "packs": packs[-200:],
            },
            "plan": plan_stats(self.plan) if self.plan is not None else None,
            "current": self.current,
            "seq": seq,
            "queued_events": events,
            "created": self.created,
            "started": self.started,
            "ended": self.ended,
        }

    # ---- 结果

    def _result_of(self, item, payload: dict | None) -> dict:
        """把一份产物折成 API 行。payload 为 None 表示没有产物（失败/未跑到）。"""
        base = {
            "image_id": item.image_id,
            "rel": item.rel,
            "name": item.out_name or item.image_id,
            "artist": item.artist,
            "in_zip": item.in_zip,
        }
        if payload is None:
            # 没有产物 = 失败或还没跑到。两种都列出来，具体原因看事件流，
            # 这里不去猜（产物里本来就没有原因，编一个只会误导）。
            return {**base, "ok": False, "tags": [], "tag": "",
                    "rating": {}, "tag_backend": ""}
        tags = [[str(t), float(p)] for t, p in (payload.get("tags") or [])]
        return {
            **base,
            "ok": bool(payload.get("ok")),
            "tag": str(payload.get("tag") or ""),
            "tags": tags,
            "rating": payload.get("rating") or {},
            "tag_backend": payload.get("tag_backend") or "",
            "ts": payload.get("ts"),
        }

    # ---- VLM 结果：数据源是产物，不是 Job.items
    #
    # 本地引擎在 create() 里扫出 plan，Job.items 就是"这一批有哪些图"；
    # VLM 引擎走 run_client 流式读分片，压根没有 plan，items 恒空。于是结果页
    # 对 NL 任务一直是"还没有产物"，哪怕产物一个不少地躺在磁盘上。
    # 下面这组方法把产物当成唯一真相：三种落盘形态各读一种。

    @staticmethod
    def _json_stem(path: Path) -> str:
        """`<id>-tag.json` / `<id>-nl.json` → `<id>`。"""
        for suffix in ("-tag.json", "-nl.json"):
            if path.name.endswith(suffix):
                return path.name[: -len(suffix)]
        return path.stem

    def _artifact_files(self) -> tuple[list[Path], list[Path], list[Path]]:
        out = Path(self.out_dir)
        if not out.is_dir():
            return [], [], []
        # VLM 的逐图 json 落在 out 根下；本地引擎那套落在 tags/ 子目录。
        # **两个后缀都要收**：纯 NL 模式（backend=none）只写 <id>-nl.json，
        # tag 文件根本不存在；只认 -tag.json 会让「NL 打标 + json 产物」的
        # 结果页继续空着——这正是最常用的一种组合。
        # 同一 stem 在根目录和 tags/ 下都出现时只留一份，按 stem 去重。
        jsons: dict[str, Path] = {}
        for base in (out, out / "tags"):
            if not base.is_dir():
                continue
            for pattern in ("*-tag.json", "*-nl.json"):
                for p in sorted(base.glob(pattern)):
                    if p.is_file():
                        jsons[self._json_stem(p)] = p
        return (
            sorted(out.glob("artifacts-*.jsonl")),
            sorted(out.glob("artifacts-*.parquet")),
            list(jsons.values()),
        )

    @staticmethod
    def _parse_artifact_row(raw: dict) -> dict | None:
        """jsonl / parquet 的一行 → {image_id, tag, nl, ts}。tag / nl 是 JSON 串。"""
        image_id = str(raw.get("image_id") or "").strip()
        if not image_id:
            return None

        def pick(value):
            if isinstance(value, dict):
                return value
            if not value:
                return None
            try:
                obj = json.loads(value)
            except (TypeError, ValueError):
                return None
            return obj if isinstance(obj, dict) else None

        return {"image_id": image_id, "tag": pick(raw.get("tag")),
                "nl": pick(raw.get("nl")), "ts": raw.get("ts")}

    def _iter_artifact_rows(self):
        """按产物顺序产出 {image_id, tag, nl, ts}。

        同时存在多种形态时只认优先级最高的那种：同一批图在两份产物里各有一行，
        混着读会让结果页出现重复卡片。
        """
        jsonls, parquets, jsons = self._artifact_files()
        if jsonls:
            for path in jsonls:
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                raw = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(raw, dict):
                                row = self._parse_artifact_row(raw)
                                if row is not None:
                                    yield row
                except OSError:
                    continue
            return
        if parquets:
            try:
                import pyarrow.parquet as pq
            except ImportError:
                return
            for path in parquets:
                try:
                    table = pq.read_table(
                        str(path), columns=["image_id", "tag", "nl", "ts"])
                except Exception:      # noqa: BLE001 - 半截/坏 footer 跳过这一片
                    continue
                for raw in table.to_pylist():
                    row = self._parse_artifact_row(raw)
                    if row is not None:
                        yield row
            return
        for path in jsons:
            stem = self._json_stem(path)
            # 两个文件都试，不假设手里这份是哪一种：纯 NL 模式下就只有 nl。
            tag_obj = self._read_json(path.with_name(f"{stem}-tag.json"))
            nl_obj = self._read_json(path.with_name(f"{stem}-nl.json"))
            if tag_obj is None and nl_obj is None:
                continue
            yield {"image_id": stem, "tag": tag_obj, "nl": nl_obj,
                   "ts": (tag_obj or {}).get("ts") or (nl_obj or {}).get("ts")}

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    def _artifact_total(self) -> int:
        jsonls, parquets, jsons = self._artifact_files()
        files = [*jsonls, *parquets, *jsons]
        try:
            sig = tuple(
                f"{p}:{p.stat().st_size}:{int(p.stat().st_mtime)}" for p in files)
        except OSError:
            sig = ()
        if sig:
            hit = _ARTIFACT_TOTAL_CACHE.get(sig)
            if hit is not None:
                return hit
        if jsonls:
            n = sum(_count_lines(p) for p in jsonls)
        elif parquets:
            n = 0
            try:
                import pyarrow.parquet as pq
                for p in parquets:
                    try:
                        n += int(pq.ParquetFile(str(p)).metadata.num_rows)
                    except Exception:      # noqa: BLE001
                        continue
            except ImportError:
                pass
        else:
            n = len(jsons)
        if sig:
            _ARTIFACT_TOTAL_CACHE[sig] = n
        return n

    def _artifact_item(self, row: dict) -> dict:
        tag = row.get("tag") or {}
        nl = row.get("nl") or {}
        tags = [[str(t), float(p)] for t, p in (tag.get("tags") or [])]
        return {
            "image_id": row["image_id"],
            "rel": str(tag.get("image_path") or nl.get("image_path") or ""),
            "name": row["image_id"],
            "artist": str(tag.get("artist") or nl.get("artist") or ""),
            "in_zip": False,
            "ok": bool(tag.get("ok") or nl.get("ok")),
            "tag": str(tag.get("tag") or ""),
            "tags": tags,
            "rating": tag.get("rating") or {},
            "tag_backend": str(tag.get("tag_backend") or ("vlm" if nl else "")),
            # NL 文案与质检分：本地引擎这两项没有，前端按缺省处理。
            "caption": str(nl.get("caption") or ""),
            "score": nl.get("score"),
            # VLM 是流式读分片，没有"原图索引"，缩略图接口拿不到图——与其让
            # 前端请求一串 404，不如明确告诉它这次不出缩略图。
            "thumb": False,
            "ts": row.get("ts") or tag.get("ts") or nl.get("ts"),
        }

    def _failure_index(self) -> dict[str, dict]:
        """failures.jsonl → {image_id: 最后一条}。

        失败账跨运行累计（同一 id 每次失败追加一行），取最后一条才是当前状态。
        """
        path = Path(self.out_dir) / "failures.jsonl"
        latest: dict[str, dict] = {}
        if not path.is_file():
            return latest
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    image_id = str(obj.get("image_id") or "").strip()
                    if image_id:
                        latest[image_id] = obj
        except OSError:
            return {}
        return latest

    def _failure_total(self) -> int:
        """失败图数量（去重后）。按文件签名缓存，翻页不会反复读失败账。"""
        path = Path(self.out_dir) / "failures.jsonl"
        try:
            st = path.stat()
        except OSError:
            return 0
        sig = (str(path), st.st_size, int(st.st_mtime))
        hit = _FAILURE_TOTAL_CACHE.get(sig)
        if hit is not None:
            return hit
        n = len(self._failure_index())
        _FAILURE_TOTAL_CACHE[sig] = n
        return n

    def _failure_results(self, *, offset: int, limit: int, query: str) -> dict:
        """只看失败：数据源是 failures.jsonl，不是产物。

        产物只写成功行，失败的根本进不去——所以「只看失败」在产物里永远是
        空表，用户会以为"一张都没失败"。
        """
        latest = self._failure_index()
        if query:
            latest = {k: v for k, v in latest.items() if query in k.lower()}
        items: list[dict] = []
        for idx, (image_id, obj) in enumerate(latest.items()):
            if idx < offset:
                continue
            if len(items) >= limit:
                break
            items.append({
                "image_id": image_id,
                "rel": str(obj.get("file") or ""),
                "name": image_id,
                "artist": "",
                "in_zip": False,
                "ok": False,
                "tag": "",
                "tags": [],
                "rating": {},
                "tag_backend": "",
                "error": str(obj.get("error") or ""),
                "thumb": False,
                "ts": obj.get("ts"),
            })
        return {"total": len(latest), "offset": offset, "limit": limit,
                "items": items, "failed_total": len(latest)}

    def _artifact_results(self, *, offset: int, limit: int, query: str,
                          only_failed: bool) -> dict:
        q = query.strip().lower()
        if only_failed:
            # 失败不在产物里，走失败账（见 _failure_results 的说明）。
            return self._failure_results(offset=offset, limit=limit, query=q)
        if not q:
            items: list[dict] = []
            for idx, row in enumerate(self._iter_artifact_rows()):
                if idx < offset:
                    continue
                if len(items) >= limit:
                    break
                items.append(self._artifact_item(row))
            return {"total": self._artifact_total(), "offset": offset,
                    "limit": limit, "items": items,
                    "failed_total": self._failure_total()}
        # 带筛选只能全扫：总数不数完就不准。
        matched: list[dict] = []
        total = 0
        for row in self._iter_artifact_rows():
            if q and q not in row["image_id"].lower():
                continue
            item = self._artifact_item(row)
            if offset <= total < offset + limit:
                matched.append(item)
            total += 1
        return {"total": total, "offset": offset, "limit": limit,
                "items": matched, "failed_total": self._failure_total()}

    def results(self, *, offset: int = 0, limit: int = 40, query: str = "",
                only_failed: bool = False) -> dict:
        """从产物文件现读，不缓存。

        为什么不缓存：产物是唯一真相（用户可能在别的终端手删/手改），
        缓存一份只会让页面与磁盘打架。接口有分页，一次最多几十个文件。

        无筛选时先切页再读文件：整目录 30 万张，每页只该付 40 次读盘，
        以前是把所有产物读完才分页，翻一页扫全库。
        """
        # items 为空 = 这台任务没有 plan：VLM 引擎本来就没有 plan，历史任务
        # （按快照重建出来的 Job）也没有。这两种都只能从产物读（见上面那组
        # _artifact_*）。本地活任务 items 非空，照旧走 items。
        if not self.items:
            return self._artifact_results(offset=offset, limit=limit,
                                     query=query, only_failed=only_failed)
        q = query.strip().lower()
        if not q and not only_failed:
            page = self.items[offset:offset + limit]
            items = [self._result_of(it, read_artifact(self.out_dir, it))
                     for it in page]
            return {"total": len(self.items), "offset": offset,
                    "limit": limit, "items": items}

        matched: list[dict] = []
        for item in self.items:
            if q and q not in item.image_id.lower():
                continue
            payload = read_artifact(self.out_dir, item)
            if payload is None:
                if not only_failed:
                    continue
                matched.append(self._result_of(item, None))
                continue
            if only_failed:
                continue
            matched.append(self._result_of(item, payload))
        total = len(matched)
        page = matched[offset:offset + limit]
        return {"total": total, "offset": offset, "limit": limit, "items": page}

    def item(self, image_id: str) -> TagItem | None:
        return self.by_id.get(image_id)

    def image_path(self, image_id: str) -> Path | None:
        item = self.by_id.get(image_id)
        if item is None or item.in_zip:
            return None
        return item.path

    def image_bytes(self, image_id: str) -> bytes | None:
        item = self.by_id.get(image_id)
        if item is None:
            return None
        try:
            return self.reader.read(item)
        except (OSError, KeyError):
            return None


class JobManager:
    """进程内唯一的一份任务表。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # 盘上的历史：进程没了之后还能把列表画出来。启动读一次进内存，
        # 之后每次落盘顺带更新——否则每个列表请求都要扫一遍目录。
        from . import history as history_store

        self._history: dict[str, dict] = {
            str(h["id"]): h for h in history_store.load_all()
        }

    # ---- 查询

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created, reverse=True)

    def all_snapshots(self, limit: int = 20) -> list[dict]:
        """列表用：内存里的活任务 + 盘上的历史，同 id 以内存为准。

        历史条目只有快照，没有 Job 对象——它们跑不了、也不能被"暂停/停止"，
        但列表、详情、结果页都还能用。
        """
        with self._lock:
            live = [j.snapshot() for j in self._jobs.values()]
            # 历史表会被任务线程写（_persist）。不持锁直接迭代 items() 会撞上
            # "dictionary changed size during iteration"——任务刚建好（新 id 入表）
            # 的同时正好有人在拉列表，就是 500。
            hist = list(self._history.items())
        seen = {str(s.get("id") or "") for s in live}
        # 历史条目带 historical 标记：页面据此把「进度/结果」置灰，
        # 因为那两页要读 Job.reader，而进程重启后早就没了。
        merged = live + [dict(h, historical=True)
                         for k, h in hist if k not in seen]
        merged.sort(key=lambda s: float(s.get("created") or 0), reverse=True)
        return merged[:limit]

    def snapshot_of(self, job_id: str) -> dict | None:
        """单条详情。内存里没有就去历史里找——历史页点开旧任务不能 404。"""
        job = self.get(job_id)
        if job is not None:
            return job.snapshot()
        with self._lock:
            return self._history.get(job_id)

    def build_readonly(self, job_id: str) -> Job | None:
        """按历史快照重建一个**只用来读结果**的 Job。

        进程重启后 Job 对象就没了，但产物还躺在磁盘上。结果页要的东西只有
        out_dir —— 与其在路由里另拼一套读产物的逻辑，不如把快照里的 spec
        还原成 Job，让读路径只存在一份。

        注意：凭据本来就不落历史，这个对象不能拿去续跑/停止，只给读。
        """
        with self._lock:
            snap = self._history.get(job_id)
        if not snap:
            return None
        saved = snap.get("spec") or {}
        known = set(JobSpec.__dataclass_fields__)
        try:
            spec = JobSpec(**{k: v for k, v in saved.items() if k in known})
        except TypeError:
            return None
        try:
            job = Job(job_id, spec, None)
        except Exception:                  # noqa: BLE001 - 快照残缺就当没有
            return None
        # 快照里的 out_dir 是任务当时真正用的那个；重算一遍可能因为相对路径、
        # cwd 变化而偏掉，所以优先用快照。
        out = str(snap.get("out_dir") or "").strip()
        if out:
            job.out_dir = Path(out)
        # 把状态与计数灌回去：重建出来的 Job 默认是 pending / 全零，直接拿去
        # 渲染统计页会显示"等待中、已完成 0"，而桶里明明有几百条记录。
        job.status = str(snap.get("status") or "pending")
        job.phase = str(snap.get("phase") or "")
        job.error = str(snap.get("error") or "")
        job.created = float(snap.get("created") or 0) or job.created
        job.started = float(snap.get("started") or 0) or 0.0
        job.ended = float(snap.get("ended") or 0) or 0.0
        counts = snap.get("counts") or {}
        if counts:
            job.stats.total = int(counts.get("total") or 0)
            job.stats.done = int(counts.get("done") or 0)
            job.stats.skipped = int(counts.get("skipped") or 0)
            job.stats.failed = int(counts.get("failed") or 0)
        # 细桶统计：内存那份随进程没了，读回落盘的分桶。列数按当前版本补齐
        # （老版本行更短），多的截掉，保证 merge_buckets 读到的是等长行。
        from . import history as history_store

        rows = history_store.load_stats(job_id)
        if rows:
            fixed: list[list[int]] = []
            for row in rows:
                if len(row) < _ROW_LEN:
                    row = row + [0] * (_ROW_LEN - len(row))
                fixed.append(row[:_ROW_LEN])
            job._fine = fixed
            job._fine_saved = len(fixed)
        return job

    def _flush_fine(self, job: Job) -> None:
        """补写还没落盘的桶（终态调一次）。

        中途不补：不满的那桶还在继续累加，写了下次还得再覆盖一遍，容易落成
        "同一桶两行"。代价是进程被硬杀时丢最后不到 100 张的统计——比统计
        错位好接受。
        """
        with job._lock:
            pending = [list(row) for row in job._fine[job._fine_saved:]]
            job._fine_saved = len(job._fine)
        for row in pending:
            _append_fine_row(job.id, row)

    def _persist(self, job: Job) -> None:
        """把当前快照写进历史。写不进去只影响"重启后还能看到"，
        不该让任务本身失败，所以异常全吞。
        """
        from . import history as history_store

        if job.status in ("done", "stopped", "failed"):
            self._flush_fine(job)
        try:
            snap = job.snapshot()
        except Exception:                  # noqa: BLE001 - 快照坏了也不该炸任务
            return
        with self._lock:
            self._history[str(snap.get("id") or "")] = snap
        # 磁盘写在锁外：别让一次慢 IO 卡住所有列表请求。
        history_store.save(snap)

    def running(self) -> Job | None:
        # paused 也算"占着位子"：它随时可能被唤醒接着往同一个 out_dir 写，
        # 这时再开一个任务，两边会同时往一批产物里塞行。
        for job in self.all():
            if job.status in ("pending", "running", "paused"):
                return job
        return None

    # ---- 创建

    def create(self, spec: JobSpec) -> Job:
        """扫输入、建任务。**不启动**——启动与创建分开，HTTP 层才能先回 202。"""
        if self.running() is not None:
            raise RuntimeError("已有任务在跑。单卡上并行只会两个都慢，等它结束或先停止。")

        options = spec.pack_options()          # 先校验封包参数再扫盘，错得便宜
        if spec.guard and spec.guard_policy:
            # 阈值不合法要在建任务时就 400。跑进线程才抛的话，HTTP 已经回了
            # 202，页面上表现为"任务失败"，实际是表单填错（与 output_mode 同理）。
            from ..config import ConfigError

            try:
                guard_policy_for(spec)
            except ConfigError:
                # 配置坏了不拦任务：_make_guard 里会降级成"本次不启用保护"
                pass
        if spec.engine == "vlm":
            # VLM 引擎吃的是 HF 分片或 split 分发包，没有本地素材目录可扫；
            # 总量流式统计，plan 留空。
            # 本地这一侧有两个入口：整目录（input_dir）与勾选清单（input_shards）。
            local_any = bool(spec.input_dir) or bool(spec.input_shards)
            if not (spec.hf_repo or local_any):
                raise ValueError(
                    "VLM 任务要么填 HF 仓库（hf_repo），"
                    "要么给本地分片（input_dir：.parquet / .zip / .tar）。")
            if spec.hf_repo and local_any:
                raise ValueError("hf_repo 与本地分片二选一：一边是仓库拉取，一边是本地分片。")
            # 格式决定磁盘行为（parquet 流式 vs tar 落盘），非法值不能等到
            # 跑进 run_client 才被当成"默认 tar"——那样会静默下几个 GB。
            fmt = str(spec.hf_format or "auto").strip().lower()
            if fmt not in ("auto", "parquet", "webdataset"):
                raise ValueError(
                    f"hf_format 只能是 auto / parquet / webdataset，收到 {spec.hf_format!r}")
            if int(spec.hf_prefetch or 0) < 0:
                raise ValueError("hf_prefetch 不能是负数")
            # 模式在这里就校验：跑进线程才在 run_client 里抛 ConfigError 的话，
            # HTTP 已经回了 202，页面上表现为"任务失败"，而实际上是表单填错了。
            mode = str(spec.output_mode or "both").strip().lower()
            if mode not in VLM_MODES:
                raise ValueError(f"output_mode 只能是 nl / tag / both，收到 {spec.output_mode!r}")
            # 推理来源同样要在这里校验：写错值等到线程里才抛，页面只会显示
            # "任务失败"，而真实原因是表单里那个词（"gpt" / "claude" 之类）。
            provider = spec_provider(spec)
            if provider:
                # API 模式没有本地服务来兜底模型名：`tagger` 这种占位名会 404。
                if not str(spec.vlm_model or "").strip():
                    raise ValueError(
                        f"{provider} 模式必须填模型名（vlm_model）："
                        f"那是 API 侧的模型 id，不是本地服务的路由名。")
                if spec.use_file_url:
                    # 云端收不到本机路径。这里直接拒，而不是跑起来每张图 400。
                    raise ValueError(
                        "API 模式不能用 file:// 传图（云端读不到本机路径）；"
                        "请关掉 use_file_url，改走 base64。")
            # 质检的区间关系同理：最短 > 最长时每条都会被判过长，
            # 跑起来只是"通过率 0"，看不出是参数填反了。
            lo, hi = _effective_char_bounds(spec)
            if lo > hi:
                raise ValueError(
                    f"质检：最短字符数（{lo}）不能大于最长字符数（{hi}）")
            # 选中的提示词文件必须真的在：跑进线程才报的话，页面已经显示
            # "运行中"再变成"失败"，人会以为是自己模型/服务的问题。
            pid = str(spec.prompt_id or "").strip()
            if pid:
                from .. import prompts as prompt_files

                try:
                    prompt_files.load(pid)
                except prompt_files.PromptError as exc:
                    raise ValueError(str(exc)) from exc
            plan = None
        else:
            if not spec.input_dir:
                raise ValueError("本地任务必须填输入目录（input_dir）；HF 仓库输入请选 VLM 引擎")
            rule, aliases = _artist_context(spec)
            plan = plan_inputs(
                spec.input_dir,
                recursive=spec.recursive,
                include_zips=spec.include_zips,
                include_loose=spec.include_loose,
                limit=spec.limit,
                # 封包必须用全局序号；只打标沿用相对路径，兼容历史产物。
                assign_ids=spec.pack,
                layout=spec.layout if spec.pack else "flat",
                seq_width=spec.seq_width,
                artist_rule=rule,
                aliases=aliases,
            )
            if not plan.items:
                raise ValueError(f"{spec.input_dir} 里没有找到图片（支持 "
                                 f"png/jpg/webp/bmp/gif/tif，zip 包内的也算）")

        job = Job(uuid.uuid4().hex[:12], spec, plan, options=options)
        if plan is not None and spec.check_space:
            # 开跑前判一次盘位：估算偏保守时最多误拦一次（可关），
            # 但不判的话要等写到一半才发现装不下——那时已经花掉几小时 GPU。
            need = estimate_job_bytes(plan.items, options)
            free = free_bytes(existing_ancestor(job.out_dir))
            if need > free:
                raise ValueError(space_error(need, free, job.out_dir))
        job.out_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if any(j.status in ("pending", "running", "paused")
                   for j in self._jobs.values()):
                raise RuntimeError("已有任务在跑。请等它结束或先停止。")
            self._jobs[job.id] = job
        self._persist(job)
        return job

    def start(self, job: Job) -> None:
        thread = threading.Thread(target=self._run, args=(job,),
                                  name=f"tag-job-{job.id}", daemon=True)
        thread.start()

    def stop(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status not in ("pending", "running", "paused"):
            return False
        job.stop_flag.set()
        # 暂停中按停止：必须先把挂起解除，否则 worker 还卡在等恢复，
        # 而它等到的条件里 stop 优先——这里显式清一下，收尾更直接。
        job.pause_flag.clear()
        where = "封包" if job.phase == "pack" else "打标"
        job.push("log", message=f"收到停止请求，等当前这一块{where}完就收尾")
        self._save_resume(job, "stopped")
        self._persist(job)
        return True

    # ---- 暂停 / 继续

    def pause(self, job_id: str) -> bool:
        """暂停：在途的几张打完就挂起，不再发新任务。

        只允许打标阶段。封包是纯本地 IO，暂停它除了让 tar 半成品停在盘上
        没有任何好处——而那正是"半批数据进下游"的入口。

        暂停时顺手写一份 resume.json：用户很可能按了暂停就去关程序，
        下次打开要能接着做。断点本身在产物里（+ 指纹），这里只记参数。
        """
        job = self.get(job_id)
        if job is None or job.status != "running":
            return False
        if job.phase == "pack":
            raise ValueError("封包阶段不能暂停：tar/zip 不支持追加，停下只会留下半成品")
        if job.spec.engine != "vlm":
            # 本地分类头是整批喂进 GPU 的，块内没有"停在两张之间"的位置；
            # 假装暂停只会让状态卡在 paused 而进度还在涨。
            raise ValueError(
                "当前引擎不支持暂停（本地分类头按块推理，停下来等同于重跑）。"
                "要中断请用停止——已完成的图同样不会重打。")
        job.pause_flag.set()
        job.status = "paused"
        job.push("log", message="已暂停：在途的几张打完就停，产物都在盘上")
        self._save_resume(job, "paused")
        self._persist(job)
        return True

    def resume(self, job_id: str) -> bool:
        """原地继续——进程还在、worker 只是挂着。

        程序已经关掉的情形不归这里管，那条路走 `resume_last()`：
        进程没了就没有 Job 对象可以"继续"，只能拿参数重建一个。
        """
        job = self.get(job_id)
        if job is None or job.status != "paused":
            return False
        job.pause_flag.clear()
        job.status = "running"
        job.push("log", message="已继续")
        self._persist(job)
        return True

    def restart(self, job_id: str) -> Job:
        """拿旧任务的参数重开一批。用于「已停止 / 失败」的任务。

        为什么不是"原地继续"：这些任务的 worker 线程已经跑完 run_batch 退出了，
        没有"接着那个循环"的说法。重建一个任务，已打完的图靠产物 + 指纹判定
        跳过——对用户来说就是续做，只是历史里会多一条记录。
        """
        old = self.get(job_id)
        if old is None:
            # 进程重启后 Job 对象没了，但历史里有它的参数——「继续」对历史任务
            # 同样该能用。凭据本来就不落历史（与 resume.json 同口径），要 key
            # 的远程来源得让用户重填，这里不假装还记得。
            with self._lock:
                snap = self._history.get(job_id)
            if snap is None:
                raise ValueError("任务不存在")
            saved = snap.get("spec") or {}
            known = set(JobSpec.__dataclass_fields__)
            # 只取认识的键：旧版本留下的记录可能多出已删字段，直接
            # JobSpec(**) 会 TypeError，把"继续"变成一句看不懂的报错。
            try:
                spec = JobSpec(**{k: v for k, v in saved.items() if k in known})
            except TypeError as exc:
                raise ValueError(f"这条历史记录与当前版本不兼容：{exc}") from exc
            job = self.create(spec)
            self.start(job)
            return job
        if old.status in ("pending", "running", "paused"):
            raise RuntimeError("这个任务还在跑，直接在运行页点「继续」就行，不用重开")
        job = self.create(replace(old.spec))     # create 内部会再判一次"已有任务在跑"
        self.start(job)
        return job

    # ---- 跨重启的续做（resume.json）

    def _save_resume(self, job: Job, why: str) -> None:
        """记下"用哪份参数、写到哪、做到哪一步"。

        已完成哪些图由产物文件判定。凭证只留在内存或用户明确保存的
        密钥配置中，不复制到续做文件。
        """
        try:
            spec = asdict(job.spec)
            spec["api_key"] = ""
            spec["hf_token"] = ""
            from ..storage.resume import save
            payload = {
                "job_id": job.id,
                "why": why,
                "saved_at": time.time(),
                "out_dir": str(job.out_dir),
                "spec": spec,
                "counts": job.snapshot().get("counts") or {},
            }
            save(RESUME_PATH, payload)
        except (OSError, TypeError, ValueError) as exc:   # 写不了不该影响暂停本身
            job.push("log", message=f"（续做记录没写成功：{type(exc).__name__}: {exc}）")

    def _clear_resume(self, job_id: str | None = None) -> None:
        """删掉续做记录。带 job_id 时只在它确实是这条任务时才删——
        用户可能已经开了新任务，别把别人的记录误删。"""
        try:
            from ..storage.resume import clear
            clear(RESUME_PATH, job_id)
        except (OSError, ValueError, TypeError):
            pass

    def wanted_resume(self) -> dict | None:
        """上次被暂停/停止、还没跑完的任务。页面打开时问一次。"""
        from ..storage.resume import load
        data = load(RESUME_PATH)
        if not isinstance(data, dict) or not isinstance(data.get("spec"), dict):
            return None
        out = str(data.get("out_dir") or "")
        if out and not Path(out).exists():
            # 产物目录都没了，续跑等于从头来——那就别用"继续"误导人，
            # 让用户自己新建任务更清楚。
            return None
        return data

    def resume_last(self) -> Job:
        """拿 resume.json 重建任务并立刻开跑。

        没有"接着上次那个线程"这回事——进程重启后线程早没了。重建出来的
        是新任务，已经打完的图靠有效产物跳过，所以效果仍是续做。
        """
        data = self.wanted_resume()
        if data is None:
            raise ValueError("没有可继续的任务（run/resume.json 不存在或产物目录已删）")
        try:
            spec = JobSpec(**data["spec"])
        except TypeError as exc:
            raise ValueError(f"续做记录与当前版本不兼容：{exc}") from exc
        previous = self.get(str(data.get("job_id") or ""))
        if previous is not None:
            spec = replace(spec, api_key=previous.spec.api_key,
                           hf_token=previous.spec.hf_token)
        job = self.create(spec)          # 里面会重扫输入、算 plan、校验空间
        self._save_resume(job, "stopped")
        self._clear_resume(str(data.get("job_id") or ""))
        self.start(job)
        return job

    # ---- 执行

    def _run(self, job: Job) -> None:
        spec = job.spec
        from ..storage.resume import running_lock
        active_resume = running_lock(RESUME_PATH, job.id)
        job.status = "running"
        job.started = time.time()
        self._persist(job)
        job.push("job_started",
                 total=len(job.items),
                 out_dir=str(job.out_dir), spec=_redacted_spec(spec),
                 plan=plan_stats(job.plan) if job.plan is not None else None)
        backend = None
        guard = None
        try:
            active_resume.acquire()
            self._save_resume(job, "running")
            guard = self._make_guard(job)
            job.guard = guard
            if spec.engine == "vlm":
                # VLM 引擎的整条主流程在 run_client 里（重试升级/黑名单/
                # 断点续做/HF 流式读），guard 的 start/stop 也归它管。
                self._run_vlm(job, guard)
                return
            job.phase = "tag"
            job.push("phase", phase="tag", message="开始打标")
            backend = _build_backend(spec)
            job.push("log", message=f"加载后端 {backend.name}（{spec.precision}"
                                     f"，batch={spec.batch}"
                                     f"，{getattr(backend, 'preprocess', 'pil')} 预处理）…")
            backend.load()
            job.model_id = str(getattr(backend, "model_id", "") or "")
            job.push("log", message=f"后端就绪：{len(job.items)} 张待处理")

            # 把 job.stats 直接交给引擎原地累加：HTTP 线程随时能读到实时计数，
            # 不必等任务结束才有一份"结账版"。
            tag_items(
                backend, job.items, out_dir=job.out_dir, force=spec.force,
                on_progress=lambda kind, item, extra, st: self._on_item(job, kind, item, extra),
                should_stop=job.stop_flag.is_set, stats=job.stats,
                read_bytes=job.reader.read, guard=guard,
            )

            stopped = job.stats.stopped or job.stop_flag.is_set() or bool(job.stats.busy)
            if stopped:
                job.status = "stopped"
                self._save_resume(job, "stopped")
                job.push("log", message="打标已中止：不封包（半批数据不该进下游）")
            else:
                # 先封包再落 "done"：轮询方一看到 done 就该能读到完整分片，
                # 否则会在 .tmp 还没 rename 的时候拿到空的 packs。
                if job.options is not None:
                    self._pack_phase(job)
                if job.status == "running":
                    job.status = "done"

        except TaggerUnavailable as exc:
            job.status = "failed"
            job.error = f"tag 后端不可用：{exc}"
        except Exception as exc:           # noqa: BLE001 - 任何异常都要落到页面上
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.current = ""
            job.ended = time.time()
            job.push("job_finished", status=job.status, error=job.error,
                     counts=job.snapshot()["counts"])
            # 终态落盘：历史列表要显示"已完成/失败"和最终计数。
            self._persist(job)
            # 后端持有几百 MB 权重与 CUDA 上下文，任务结束就放手，
            # 否则连跑三个任务会占着三份显存不放。
            # 跑完/失败的记录没有"继续"的必要，删掉；暂停与停止的留着。
            # 失败也删：同一份参数再跑一次大概率还是同样的错，让页面反复
            # 提示"继续"只会把人往同一个坑里带。
            if job.status in ("done", "failed"):
                self._clear_resume(job.id)
            active_resume.close()
            del backend
            job.reader.close()
            if guard is not None:
                # vlm 路径里 run_client 已经停过；stop 幂等，这里兜底。
                guard.stop()

    def _make_guard(self, job: Job) -> ResourceGuard | None:
        """两种引擎共用一个 guard。打标在块边界、封包在循环头各挂一次
        before_unit；级别变化经 on_state 推成 guard 事件，页面上直接能
        看到"温度熔断/磁盘告急"这类状态，而不是闷头卡住。"""
        if not job.spec.guard:
            job.push("log", message="硬件保护已关闭（guard=false）")
            return None
        try:
            # 配置的 limits 段打底，表单的覆盖叠在上面。create() 已经校验过一次，
            # 这里再算一次是因为它是"真正生效的那份"，顺便把结果写进日志。
            policy = guard_policy_for(job.spec)
        except Exception as exc:       # noqa: BLE001 - 配置坏了不拦任务，只降级
            job.push("log", message=f"硬件保护启用失败（{exc}），本次不启用")
            return None
        if job.spec.check_space:
            # 磁盘改由"预计产出 + 20% 冗余"判（见 pipeline.space_check）。
            # guard 的固定阈值留着会先斩后奏：剩 12G 的盘跑 3 张图也会被拦。
            policy.disk_min_free_gb = 0.0
            policy.disk_min_free_pct = 0.0
        job.push("log", message=f"硬件保护生效：{describe_guard(policy)}")
        return ResourceGuard(
            policy, data_dir=job.out_dir, verbose=False,
            on_state=lambda st: job.push("guard", level=st.level,
                                         message=st.describe()),
        )

    def _run_vlm(self, job: Job, guard: ResourceGuard | None) -> None:
        """VLM 引擎：整体复用 CLI 的 run_client 主流程，web 只做三件事——
        组 argparse.Namespace、把 ProgressHook 桥成事件、把 ClientStats
        接进快照。重试升级/失败黑名单/断点续做/guard 熔断全在主流程里。"""
        import argparse

        from ..client import run_client
        from ..config import DEFAULT_TIERS, load_ops, load_tier_table

        spec = job.spec
        job.phase = "tag"
        job.push("phase", phase="tag", message="开始 VLM 打标")

        # 多卡：地址来自"已经就绪的那组服务"。这里不主动起服务——
        # 加载模型要几分钟，那是服务面板上的独立动作；任务只管用。
        multi_urls: list[str] = []
        if spec.multi_gpu:
            from .multi_gpu import MULTI

            multi_urls = MULTI.urls()
            if not multi_urls:
                raise ValueError(
                    "多卡模式已开启，但没有就绪的多卡服务。"
                    "先在「推理服务」面板启动多卡，再回来打标。")
            if len(multi_urls) < 2:
                raise ValueError(
                    f"多卡服务只有 {len(multi_urls)} 张卡就绪，"
                    "至少需要 2 张才能并行；或关掉多卡模式。")
            job.push("phase", phase="tag",
                     message=f"多卡并行：{len(multi_urls)} 个推理服务，"
                             f"每卡并发 {spec.per_card_seqs or '按分档'}")

        table = load_tier_table(DEFAULT_TIERS)
        ops = load_ops(table)
        # run_client 的 keep/阈值只从配置读，命令行没有对应开关；web 把
        # 表单里的覆盖注进 tagging 段，等价于"替用户改一份临时配置"。
        if spec.keep_categories or spec.thresholds:
            tagging = dict(ops.tagging or {})
            if spec.keep_categories:
                tagging["keep_categories"] = list(spec.keep_categories)
            if spec.thresholds:
                tagging["thresholds"] = {str(k): float(v)
                                         for k, v in spec.thresholds.items()}
            ops = replace(ops, tagging=tagging)

        # 画师名在 VLM 路径里来自数据本身（parquet 的 artist 列 / webdataset
        # 的目录名），不像本地引擎那样从路径反推。配置里的 enabled 是给
        # 命令行兜底的默认值，而"这次要不要把 @画师 写进 .nl/.tag 文本"
        # 是用户当次的选择——不覆盖的话，页面上那个开关就是摆设。
        artist_cfg = dict(ops.artist or {})
        artist_cfg["enabled"] = bool(spec.artist)
        artist_cfg["inject_caption"] = bool(spec.artist and spec.artist_inject)
        # NL 与 TAG 是两个开关、两条训练输入，只搬其中一个等于另一个永远是默认值。
        artist_cfg["inject_tags"] = bool(spec.artist and spec.artist_inject_tags)
        ops = replace(ops, artist=artist_cfg)

        # 质检线同理：配置里是一套保守默认，页面上调过就以表单为准。
        # 这三项都直接影响"每张图打几次"，是 30M 量级下最贵的旋钮之一。
        quality_cfg = dict(ops.quality or {})
        changed = False
        if spec.quality_pass_score > 0:
            quality_cfg["pass_score"] = float(spec.quality_pass_score)
            changed = True
        if spec.quality_drop_score > 0:
            quality_cfg["drop_score"] = float(spec.quality_drop_score)
            changed = True
        if spec.quality_max_attempts > 0:
            quality_cfg["max_attempts"] = int(spec.quality_max_attempts)
            changed = True
        # 重试阶梯同理：抬温度 / 抬分辨率是"救一张图"的两条路，调错只会
        # 拉长单张耗时而不报错，所以默认留空跟配置，改了就显式覆盖。
        if spec.quality_retry_temp_step > 0:
            quality_cfg["retry_temp_step"] = float(spec.quality_retry_temp_step)
            changed = True
        if spec.quality_retry_temp_cap > 0:
            quality_cfg["retry_temp_cap"] = float(spec.quality_retry_temp_cap)
            changed = True
        if spec.quality_retry_pixels_scale > 0:
            quality_cfg["retry_pixels_scale"] = float(spec.quality_retry_pixels_scale)
            changed = True
        if spec.quality_retry_pixels_cap > 0:
            quality_cfg["retry_pixels_cap"] = int(spec.quality_retry_pixels_cap)
            changed = True
        # L2：只要用户打开了就写进去（false 也是显式意图，不能靠"没设"表示）。
        # escalate_model 是配套开关——质检的 RetryPlan 只有在它开着时才会
        # 给出 model_hint="escalate"，没它就算填了升级地址也不会触发。
        if spec.quality_enable_clip:
            quality_cfg["enable_clip"] = True
            changed = True
        if spec.quality_clip_min_score > 0:
            quality_cfg["clip_min_score"] = float(spec.quality_clip_min_score)
            changed = True
        # CLIP 权重来源：空串 = 跟配置。换权重等于换打分口径，必须显式覆盖。
        if spec.quality_clip_model.strip():
            quality_cfg["clip_model"] = spec.quality_clip_model.strip()
            changed = True
        if spec.quality_clip_pretrained.strip():
            quality_cfg["clip_pretrained"] = spec.quality_clip_pretrained.strip()
            changed = True
        # L1 规则层：None = 跟配置。0 是合法值，不能拿它当"没填"。
        if spec.quality_min_chars is not None:
            quality_cfg["min_chars"] = max(0, int(spec.quality_min_chars))
            changed = True
        if spec.quality_max_chars is not None:
            quality_cfg["max_chars"] = max(0, int(spec.quality_max_chars))
            changed = True
        if spec.quality_min_tag_coverage is not None:
            quality_cfg["min_tag_coverage"] = min(
                1.0, max(0.0, float(spec.quality_min_tag_coverage)))
            changed = True
        if spec.quality_max_repeat_ratio is not None:
            quality_cfg["max_repeat_ratio"] = min(
                1.0, max(0.0, float(spec.quality_max_repeat_ratio)))
            changed = True
        if spec.escalate_model.strip() and spec.escalate_url.strip():
            quality_cfg["escalate_model"] = True
            changed = True
        if changed:
            # 兜底复检：create() 已经拦过一次，这里防的是绕过 create 直接跑
            # （测试、脚本）。最短 > 最长不会报错，只会让每条样本都被判过长。
            lo = quality_cfg.get("min_chars")
            hi = quality_cfg.get("max_chars")
            if lo is not None and hi is not None and float(lo) > float(hi):
                raise ValueError(
                    f"质检：最短字符数（{lo}）不能大于最长字符数（{hi}）")
            ops = replace(ops, quality=quality_cfg)

        # mode 映射：run_client 在 tag 后端在场时会把 VLM 收窄成只出 NL
        # （tag 由后端出），所以"both + 本地后端"必须以 both 进门；
        # 单模式（nl / tag）则连后端一起关掉——具体理由见 vlm_backend_for。
        mode = str(spec.output_mode or "both").strip().lower()
        backend_name = vlm_backend_for(mode, spec.backend)
        provider = spec_provider(spec)

        # 提示词文件在真正开跑时现读一次：从建任务到线程启动之间用户可能刚
        # 编辑过，页面显示的和实际发出去的对不上是最难查的那类问题。
        prompt_doc = None
        if str(spec.prompt_id or "").strip():
            from .. import prompts as prompt_files

            prompt_doc = prompt_files.load(spec.prompt_id)

        args = argparse.Namespace(
            # HF 输入与本地分片二选一（create 已校验）；shard=None 时
            # run_client 走 HF 分支。本地必须给**列表**：collect_shards /
            # classify_local_inputs 都按序列遍历，传单个字符串会逐字符解析，
            # 报出来的是"分片不存在: D"。
            # input_shards 优先：勾了几片就只跑这几片。classify_local_inputs
            # 本来就按序列认（文件、目录可以混着给），所以直接给列表即可。
            shard=None if spec.hf_repo else (list(spec.input_shards) or [spec.input_dir]),
            out_dir=str(job.out_dir),
            # 多卡时 url 是第一个槽，urls 是其余槽；按空闲槽补发。
            url=multi_urls[0] if multi_urls else spec.url,
            urls=multi_urls[1:],
            # 留空交给 run_client 解析保存的地址、密钥及默认值。
            api_provider=provider,
            api_base=str(spec.api_base or ""),
            api_key=str(spec.api_key or ""),
            model_name=spec.vlm_model,
            mode=mode,
            temperature=spec.temperature,
            top_p=spec.top_p,
            max_tokens=int(spec.max_tokens or 0) or None,
            timeout=int(spec.timeout_s or 90),
            # 多卡时这个数是"每卡并发"：run_batch 每个槽各开这么多。
            concurrency=(int(spec.per_card_seqs or 0)
                         or int(spec.concurrency or 0)) or None,
            limit=int(spec.limit or 0) or None,
            force=bool(spec.force),
            layout=spec.layout,
            # 与 WebUI 启动的服务写的是同一个文件；cli 的默认值也是这个相对路径。
            # API 模式传空：没有 serve 写过指纹，传路径只会刷一句误导性的警告。
            fingerprint="" if provider else str(FINGERPRINT_PATH),
            model_id="",
            max_pixels=int(spec.max_pixels or 0) or None,
            # None 时不传，run_client 会按 token 区间派生（保持旧行为）
            caption_chars=(list(spec.caption_chars)
                           if getattr(spec, "caption_chars", None) else None),
            # 0 也照传：它不是"没设置"，而是"明确全英文"。
            caption_zh_ratio=float(getattr(spec, "caption_zh_ratio", 0.0) or 0.0),
            # 提示词文件：空串 = 用内置生成的那份。中文/系统提示词一起带上，
            # 三个都空时 run_client 的行为与改造前完全一致。
            prompt_text=prompt_doc.prompt if prompt_doc else "",
            prompt_zh_text=prompt_doc.prompt_zh if prompt_doc else "",
            system_text=prompt_doc.system if prompt_doc else "",
            prompt_name=prompt_doc.id if prompt_doc else "",
            tier="", variant="", seed=spec.seed,
            quiet=True, use_file_url=bool(spec.use_file_url),
            failures=None, stats_json=None,
            max_failures=int(spec.max_failures or 0),
            no_guard=True,                 # guard 从外面传入，避免双 watchdog
            escalate_url=str(spec.escalate_url or ""),
            escalate_model=str(spec.escalate_model or ""),
            tag_backend=backend_name,
            tag_model=spec.model_repo or None,
            tag_model_dir=spec.model_dir or None,
            tag_device=spec.device or "auto",
            tag_precision=spec.precision,
            tag_batch=max(1, int(spec.batch or 1)),
            tag_preprocess=str(spec.preprocess or "pil"),
            artifact_format=str(spec.artifact_format or "jsonl"),
            parquet_rows=int(spec.parquet_rows or 2000),
            hf_repo=spec.hf_repo or "",
            hf_token=spec.hf_token or "",
            hf_revision=spec.hf_revision or "main",
            hf_files=list(spec.hf_files or []),
            # 空串 = 按实际格式挑后缀。写死 "*.parquet" 时，只有 tar 的仓库
            # 会被过滤成 0 个分片，报出来是"没有可处理的 .tar"，很难对上号。
            hf_pattern="",
            hf_cache=None, hf_shards=0, hf_keep=False,
            hf_format=str(spec.hf_format or "auto"),
            hf_prefetch=max(0, int(spec.hf_prefetch or 0)),
            hf_conns=max(1, min(16, int(spec.hf_conns or 1))),
            hf_proxy=_hf_proxy(spec.hf_proxy),
            hf_reserve_gb=float(spec.hf_reserve_gb or 0.0),
            hf_image_column="image", hf_path_template="{image_id}.jpg",
            cache=bool(spec.cache), cache_db="",
            cache_max_gb=float(spec.cache_max_gb or 8.0),
        )

        def hook(kind: str, task, outcome, stats) -> None:
            job.client_stats = stats
            job.current = task.image_id
            if kind in ("done", "failed"):
                if not job.first_settled:
                    job.first_settled = time.time()
                # 多卡时把这一张记到它实际跑的那张卡上，面板上才有"各卡
                # 分担了多少"。单卡模式不记：MULTI.workers 为空，写了也无处去。
                if spec.multi_gpu:
                    value = getattr(outcome, "slot", -1)
                    slot = int(value) if value is not None else -1
                    if slot >= 0:
                        from .multi_gpu import MULTI

                        MULTI.note_item(slot, kind == "done")
            attempts = getattr(outcome, "attempts", 0) or 1
            score = None
            if kind == "done":
                payload = (outcome.tag if outcome is not None else None) or {}
                names = [str(t) for t, _ in (payload.get("tags") or [])]
                # 质检分在 nl 产物里（tag-only 模式没有）：统计页要它画趋势
                nl = getattr(outcome, "nl", None) or {}
                score = nl.get("score")
                extras = {"score": score} if score is not None else {}
                job.push("item", image_id=task.image_id, ok=True,
                         tag_count=len(names), tags=names[:10], **extras)
            elif kind == "skipped":
                job.push("item", image_id=task.image_id, ok=True, skipped=True,
                         tag_count=0, tags=[])
            elif kind == "blacklisted":
                job.push("item", image_id=task.image_id, ok=False,
                         error="连续失败达到上限，本批拉黑跳过", tags=[])
            elif kind == "busy":
                job.push("item", image_id=task.image_id, ok=False,
                         error="其他进程正在处理，续做时会重新核对", tags=[])
            else:
                job.push("item", image_id=task.image_id, ok=False,
                         error=str(getattr(outcome, "error", "") or "")[:200],
                         tags=[])
            job.note_item(kind, attempts=attempts, score=score)

        job.push("log", message=(
            f"推理来源 {provider or 'local'}"
            f"  {spec_api_base(spec) if provider else spec.url}"
            f"  模型 {spec.vlm_model}  产物 {spec.artifact_format}"
            + (f"  HF 仓库 {spec.hf_repo}" if spec.hf_repo else "  本地分片输入")))
        code = run_client(args, ops, table, on_progress=hook,
                          should_stop=job.stop_flag.is_set,
                          paused=job.pause_flag.is_set,
                          on_log=lambda message: job.push("log", message=message),
                          guard=guard)
        if job.stop_flag.is_set():
            job.status = "stopped"
            job.push("log", message="已停止：产物与断点都在盘上，重跑可续做")
        elif job.client_stats is not None and job.client_stats.busy:
            job.status = "stopped"
            self._save_resume(job, "stopped")
            job.push("log", message="部分图片由其他进程处理；已保留续做记录，下次继续会重新核对")
        elif code == 0:
            if job.status == "running":
                job.status = "done"
        else:
            job.status = "failed"
            job.error = "打标未完整跑完（详见日志与失败统计）；已写盘的部分可续做"

    def _pack_phase(self, job: Job) -> None:
        spec = job.spec
        options = job.options
        assert options is not None
        job.phase = "pack"
        job.pack_started = time.time()
        job.push("phase", phase="pack",
                 message=f"开始封包（{'/'.join(options.formats)}）")
        try:
            report = pack_items(
                job.items, job.out_dir, options,
                layout=spec.layout,
                reader=job.reader,
                model_id=job.model_id,
                artist_prefix=_artist_context(spec)[0].prefix,
                inject_artist=bool(spec.artist and spec.artist_inject),
                naming=spec.naming_fingerprint(),
                on_sample=lambda item: self._on_pack_sample(job, item),
                on_skip=lambda item, why: self._on_pack_skip(job, item, why),
                on_seal=lambda row: self._on_pack_seal(job, row),
                on_log=lambda message: job.push("log", message=message),
                should_stop=job.stop_flag.is_set,
                guard=job.guard,
                check_space=spec.check_space,
            )
        except PackSpaceError as exc:
            # 盘不够是"可以换个盘再跑"的输入问题，不是任务故障：
            # 一条都不写、不留半成品，重跑代价为零。
            job.pack_report = None
            job.status = "failed"
            job.error = f"磁盘不够：{exc}"
            job.push("log", message=f"封包未开始：{exc}")
            return
        except Exception as exc:           # noqa: BLE001 - 封包失败要看得见
            job.pack_report = None
            job.status = "failed"
            job.error = f"封包失败：{type(exc).__name__}: {exc}"
            return
        job.pack_report = report
        job.pack_failed = report.failed
        job.packed = report.packed
        job.pack_skipped = report.skipped_done
        job.pack_missing = report.skipped_missing
        job.push("log", message=(
            f"封包完成：写入 {report.packed} 条，跳过 {report.skipped_done} 条，"
            f"缺产物 {report.skipped_missing} 条，失败 {report.failed} 条；"
            f"产出 {len(report.sealed)} 个分片"))
        if report.stopped:
            # 硬件保护熔断不算"完成"：0 条写入 + 状态 done 会让下游以为
            # 产物齐了。落成 stopped，页面看得到原因。
            job.status = "stopped"
            job.push("log", message=f"封包未完成：{report.stopped}")
        elif job.stop_flag.is_set():
            job.status = "stopped"

    # ---- 回调

    def _on_item(self, job: Job, kind: str, item: TagItem, extra) -> None:
        job.current = item.image_id
        if not job.first_settled:
            # 只有产出线程会写这里；无锁读 float 在 CPython 下是原子的。
            job.first_settled = time.time()
        if kind == "done":
            payload = extra if isinstance(extra, dict) else {}
            names = [str(t) for t, _ in (payload.get("tags") or [])]
            job.push("item", image_id=item.image_id, ok=True,
                     tag_count=len(names), tags=names[:10])
        elif kind == "skipped":
            job.push("item", image_id=item.image_id, ok=True, skipped=True,
                     tag_count=0, tags=[])
        elif kind == "busy":
            job.push("item", image_id=item.image_id, ok=False,
                     error="其他进程正在处理，续做时会重新核对", tags=[])
        else:
            job.push("item", image_id=item.image_id, ok=False,
                     error=str(extra)[:200], tags=[])
        # 本地引擎没有重试也没有质检分：attempts 恒为 1，全落"首过"
        job.note_item(kind)

    def _on_pack_sample(self, job: Job, item: TagItem) -> None:
        job.current = item.image_id
        if not job.pack_first:
            job.pack_first = time.time()
        job.packed += 1
        job.push("pack_item", image_id=item.image_id, ok=True)

    def _on_pack_skip(self, job: Job, item: TagItem, why: str) -> None:
        """封包阶段被跳过的条目。三种原因分开计数：界面上"跳过"的含义
        必须能区分"上次已封过"和"这张根本没打出标"。"""
        job.current = item.image_id
        if not job.pack_first:
            job.pack_first = time.time()
        if why == "done":
            job.pack_skipped += 1
            job.push("pack_item", image_id=item.image_id, ok=True, skipped=True)
        elif why == "missing":
            job.pack_missing += 1
            job.push("pack_item", image_id=item.image_id, ok=False, missing=True)
        else:
            job.pack_failed += 1
            job.push("pack_item", image_id=item.image_id, ok=False)

    def _on_pack_seal(self, job: Job, row: dict) -> None:
        job.packs.append(row)
        job.pack_bytes += int(row.get("bytes") or 0)
        job.push("pack_sealed", name=row.get("name", ""),
                 count=int(row.get("count") or 0),
                 bytes=int(row.get("bytes") or 0))


def _artist_context(spec: JobSpec) -> tuple[ArtistRule, dict[str, str]]:
    """画师规则 + 翻译表。读不到配置就用保守兜底，绝不因此让任务建不起来。

    前缀与别名表的口径跟配置走；**是否注入 caption 跟任务表单走**
    （`artist_inject`）——它是用户这次的选择，不该被配置里的默认值盖掉。
    """
    prefix, aliases_path, table = "@", None, None
    try:
        from ..config import DEFAULT_TIERS, load_ops, load_tier_table

        ops = load_ops(load_tier_table(DEFAULT_TIERS))
        configured = ops.artist_rule()
        prefix = configured.prefix
        aliases_path = configured.aliases_path
        table = configured.table
    except Exception:                     # noqa: BLE001 - 配置文件坏了不该连任务都建不了
        pass

    # 表单填了就以表单为准；空才回落到配置。任务级覆盖是刻意的：
    # 换数据集要换表，改 yaml 会让上一个任务的口径跟着变。
    table = (spec.artist_table or "").strip() or table
    rule = spec.artist_rule(prefix=prefix, aliases_path=aliases_path, table=table)
    aliases: dict[str, str] = {}
    if aliases_path:
        try:
            from ..artist import AliasTable

            aliases = AliasTable(aliases_path).as_dict()
        except Exception:                 # noqa: BLE001
            aliases = {}
    return rule, aliases


def _build_backend(spec: JobSpec):
    backend = build_backend(
        spec.backend,
        model=spec.model_repo or None,
        model_dir=spec.model_dir or None,
        device=resolve_device(spec.device or "auto"),
        precision=spec.precision,
        batch_size=max(1, int(spec.batch or 1)),
        preprocess=spec.preprocess or "pil",
        keep=list(spec.keep_categories) if spec.keep_categories else None,
        thresholds=spec.backend_thresholds(),
        verbose=False,
    )
    if backend is None:
        raise ValueError("本地打标必须选一个 tag 后端（不能是 none）")
    return backend


# 进程级单例。多 worker 跑 uvicorn 时这里会各存一份——本地单用户场景下
# 我们只用单 worker（见 cli.py 的 web 命令），所以不需要外部分布式状态。
MANAGER = JobManager()
