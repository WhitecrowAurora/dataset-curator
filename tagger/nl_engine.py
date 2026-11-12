"""NL 推理引擎的第二种实现：llama.cpp（GGUF）。

为什么需要它
------------
vLLM 只在 Linux 上可用，Windows 成员机只能走 WSL2 或连远程。2026-11-04 在
RTX 4070 Ti SUPER 上实测过三个候选，结论是 Windows 原生能跑 Qwen3-VL 且真的
用上 GPU 的只有 llama.cpp：

    llama.cpp（CUDA 官方预编译）  1.27 张/秒（8B-Q4_K_M，并发 4）
    mistral.rs                   有 Windows 构建，但只有 CPU 版（4.86 tok/s）
    vLLM 官方 / SGLang / TRT-LLM  无 Windows 版

所以这里不做"通用引擎抽象"——那会把两边的差异抹平成谁也不满意的一层。
只做一件事：把同一份 RunProfile **翻译**成 llama-server 的命令行。

参数怎么对得上（改动前务必读懂）
--------------------------------
1. **max_model_len 是每序列，`-c` 是总池**。llama.cpp 的 `-c` 按 `-np` 均分给
   各 slot，所以 `-c = max_model_len × slots`。照搬 vLLM 的数字会让每个 slot
   的上下文缩到 1/slots，症状是长 caption 被腰斩——而截断不会报错。
2. **`-np` 就是 max_num_seqs**。实测 4070 Ti SUPER 上 4 个 slot 就到顶，
   再加只把 p95 从 3.4s 拉到 6.3s（吞吐只涨 5%）。
3. **served_name 用 `-a/--alias`**。客户端请求里的 model 字段必须与它一字不差，
   否则 llama-server 回 404，而报错里只写着 "model not found"。
4. **像素预算翻译成 `--image-max-tokens`**。两边口径不同：vLLM 收的是
   max_pixels，llama.cpp 收的是视觉 token 上限，换算按 Qwen3-VL 的
   32×32 像素/token（589824 → 576）。同一张图在两个引擎下才是同一个输入，
   provenance 里的 max_pixels 也才能跨引擎比较。
5. **不传 file://**。llama-server 没有 `--allowed-local-media-path`，
   指纹里 local_media_root 留空，客户端据此退回 base64。

GGUF 从哪来
-----------
权重路径写在 tiers.yaml 的 `models:` 段（`gguf:` / `mmproj:` 两个字段），
相对仓库根。不自动下载：5.8GB 的权重不该在"点一下启动"里悄悄拉，
而且 GGUF 的量化档位（Q4_K_M / Q8_0）是口径的一部分，必须显式写在配置里。
下载命令（国内直连 HF 不通时走 ModelScope）见 missing_weights_hint()。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .config import GGUF_PRECISIONS, ConfigError, RunProfile
from .netutil import trust_env_for

ROOT = Path(__file__).resolve().parents[1]

ENGINES = ("vllm", "llamacpp")

# Qwen3-VL 的视觉 token 换算：patch 16 × merge 2 = 32×32 像素一个 token。
# 换家族就不成立，所以换算只在这个家族里启用（见 image_tokens_for）。
_PIXELS_PER_VISION_TOKEN = 32 * 32


def normalize_engine(value: str | None) -> str:
    """归一化引擎名。空 = 默认 llama.cpp。"""
    key = str(value or "").strip().lower()
    if not key:
        # 默认 llama.cpp：它是唯一在 Windows 原生能上 GPU 的引擎（vLLM 官方
        # 只有 Linux 版，Windows 要 WSL2），而这个项目的主要使用场景就是
        # Windows + 单卡。默认选一个"多数机器开箱能跑"的，比默认选一个
        # "多数机器一上来就报错"的强。
        # 想用 vLLM 就显式写 engine=vllm——引擎决定产出算什么口径，所以
        # 指纹里会记下来，两种引擎的数据不会混批。
        return "llamacpp"
    if key in ("llamacpp", "llama.cpp", "llama-cpp", "llama", "gguf"):
        return "llamacpp"
    if key in ("vllm", "v-llm"):
        return "vllm"
    raise ConfigError(f"未知 NL 引擎 {value!r}。可选：vllm / llamacpp。")


def image_tokens_for(profile: RunProfile) -> int:
    """把 max_pixels 换算成视觉 token 上限。只对 Qwen3-VL 家族成立。"""
    if not profile.model.family.startswith("qwen3vl"):
        raise ConfigError(
            f"family={profile.model.family} 不是 Qwen3-VL，"
            f"max_pixels={profile.max_pixels} 无法按 32×32/token 换算。"
            f"请先用 `tagger smoke` 实测单图 token 数再填 --image-max-tokens。"
        )
    return max(64, int(profile.max_pixels) // _PIXELS_PER_VISION_TOKEN)


def find_server_binary() -> str | None:
    """找 llama-server。环境变量 > runtime/llama.cpp（本程序装的）> PATH。

    具体查找顺序在 llama_runtime.find_binary 里——那个模块还负责"没有就
    装一个"，查找与安装共用一套落点，否则装完这里仍找不到。
    """
    from .llama_runtime import find_binary

    return find_binary()


def missing_weights_hint(model_key: str) -> str:
    return (
        f"模型 {model_key} 没有配 GGUF 权重。在 configs/tiers.yaml 的 models 段补两行：\n"
        f"    gguf: models/nl/{model_key}/<权重>.gguf\n"
        f"    mmproj: models/nl/{model_key}/mmproj-<权重>.gguf\n"
        f"官方 GGUF 在 Qwen/Qwen3-VL-8B-Instruct-GGUF（含 mmproj）。"
        f"国内直连 huggingface.co 常不通，可改用 modelscope.cn 同名仓库，"
        f"或设 HTTPS_PROXY 后重试。"
    )


def resolve_weights(profile: RunProfile) -> tuple[Path, Path]:
    """把 profile 里的 gguf / mmproj 解析成两个真实文件，缺一个就当场报清楚。

    选了 GGUF 量化档（q4/q6…）就按档取路径：同一份权重不同压缩比是三个
    文件，不是三个模型。没配对应档时回落到 `gguf:`，并说明缺哪一档，
    而不是让人以为"选了 Q6、其实还是 Q4 在跑"。
    """
    model = profile.model
    quants = dict(getattr(model, "gguf_quants", None) or {})
    want = str(profile.precision or "").strip().lower()
    # 选了 GGUF 档但模型没配这一档时**必须报错**。以前是静默回落到 `gguf:`，
    # 结果是"界面上选 Q6，实际一直跑 Q4"——用户以为换了一代权重，产出差异
    # 却全被算到 prompt 或数据头上。只有"没指定 GGUF 档"（int4/bf16 这类
    # vLLM 口径，或为空）才回落到默认那档。
    if want in GGUF_PRECISIONS and quants and want not in quants:
        raise ConfigError(
            f"模型 {model.key} 没有 {want} 这一档 GGUF，现有哪些："
            f"{' / '.join(sorted(quants)) or '（无）'}。\n"
            f"在「模型管理」页下它（或用量化功能生成），或改用上面的档位。")
    gguf_raw = str(quants.get(want) or getattr(model, "gguf", "") or "").strip()
    mmproj_raw = str(getattr(model, "mmproj", "") or "").strip()
    if not gguf_raw or not mmproj_raw:
        raise ConfigError(missing_weights_hint(model.key))

    def _abs(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (ROOT / p)

    gguf, mmproj = _abs(gguf_raw), _abs(mmproj_raw)
    if not gguf.is_file():
        extra = ""
        if want in quants:
            extra = (f"\n当前精度是 {want}，配的文件是 {gguf_raw}——这一档还没下载。"
                     f"在「模型管理」页下它，或把精度改回 Q4。")
        raise ConfigError(
            f"GGUF 权重不存在：{gguf}{extra}\n{missing_weights_hint(model.key)}")
    if not mmproj.is_file():
        raise ConfigError(
            f"视觉投影文件（mmproj）不存在：{mmproj}\n"
            f"少了它 llama-server 能起来，但每张图都会被当成纯文本请求处理——"
            f"表现是模型开始「看图说话」地编造内容，而不是报错。\n"
            f"{missing_weights_hint(model.key)}")
    return gguf, mmproj


def check_engine(engine: str) -> None:
    """能不能在本机拉起这个引擎。不能时给一句人能读的原因。"""
    if engine != "llamacpp":
        from .model_loader import check_environment

        check_environment()
        return
    binary = find_server_binary()
    if binary is None:
        # 装过、但目录里翻不到 exe：这时的关键信息不是"再装一次"，而是
        # "装到哪了"。自查一下再说话，否则用户只会反复重装。
        from .llama_runtime import BINARY_NAME, INSTALL_ROOT, _local_binary

        if INSTALL_ROOT.is_dir() and any(INSTALL_ROOT.iterdir()):
            found = _local_binary()
            raise ConfigError(
                f"llama.cpp 目录里有东西，但没找到 {BINARY_NAME}：{INSTALL_ROOT}\n"
                f"（本次查找结果：{found or '无'}）\n"
                f"大概是解压不完整或被杀软删了。去网页点「下载并安装 llama.cpp」"
                f"重装一次；也可以用「从本地压缩包安装」装一份官方包，或在命令行 "
                f"`tagger llama install --zip <路径>`。")
        raise ConfigError(
            f"找不到 {BINARY_NAME}。三条路：\n"
            "  1) 网页上点「下载并安装 llama.cpp」（推荐，装到 runtime/llama.cpp）；\n"
            "  2) 命令行 `tagger llama install`（可用 --zip 指定本地包或镜像地址）；\n"
            "  3) 已经有现成构建：设环境变量 TAGGER_LLAMA_SERVER 指向 "
            f"{BINARY_NAME} 的完整路径。\n"
            "GitHub 直连不通时先设 HTTPS_PROXY。"
        )


# --------------------------------------------------------------------- 槽位预算
#
# 为什么不照抄 vLLM 的 max_num_seqs × max_model_len
# ------------------------------------------------
# 那套数字对 llama.cpp 是**双倍浪费**：
#   1. max_model_len 是按"最坏情况的长上下文"给的（8192），而 NL 打标一个请求
#      实际只吃 image_tokens(576) + prompt(约 630) + 输出(约 200) ≈ 1400 token。
#   2. 上下文按 8192 乘 12 个 slot = 98K，16GB 卡直接吃满显存，
#      decode 崩到 1 tok/s（实测）。
#
# 实测（4070 Ti SUPER 16G，8B-Q4_K_M，真实素材，输出约 200 token）：
#   slot 数  每 slot 上下文   KV   张/秒   p50     GPU util
#      4         8192      f16   0.91   3.80s     58%
#      8         4096      f16   1.00   7.21s     79%
#     12         2816      q8_0  1.22   8.37s     82%   ← 拐点
#     16         2560      q8_0  1.18  11.88s     79%
# 12 之后收益归零（compute 顶到了），所以上限就停在 12。
# 延迟随 slot 数上升，但打标是批处理，看的是张/秒不是 p50。
SLOT_TIERS: tuple[tuple[float, int], ...] = (
    (14.0, 12), (9.0, 8), (6.0, 4), (0.0, 2))
CTX_PER_SLOT = 3072      # 覆盖 576 视觉 + 630 prompt + 400 输出，还留了余量
KV_TYPE = "q8_0"         # 只换显存，不换产出分布
MIN_CTX_HEADROOM = 1024  # prompt + 输出至少要这么多，低于它就必然截断


def slot_plan(profile: RunProfile, vram_gb: float | None = None,
              override: int | None = None) -> tuple[int, int, str]:
    """按显存给 (slot 数, 每 slot 上下文, KV 类型)。

    vram 未知时给最保守的 2 slot —— 这个值只影响吞吐，不影响产出，
    所以按"肯定起得来"选，不猜。

    取值顺序：override > profile.slots > 显存推算。
      * override 是页面上填的"每卡并发"（多卡时每张卡一份服务，各按这个数
        开 slot），多卡那条路专用；
      * profile.slots 来自 resolve_profile（分档表 slots 或 --max-num-seqs），
        单卡命令行/页面启动走这条；
      * 都没有才按显存推。
    它只改吞吐不改产出，所以允许覆盖显存推算值；但每 slot 上下文仍取
    CTX_PER_SLOT，总上下文随之线性增长——显存不够时会 OOM，这是用户显式
    选择的结果，不做二次猜测。
    """
    if override:
        slots = max(1, min(64, int(override)))
    else:
        # profile.slots 是"用户/分档表给的值"（resolve_profile 里已把
        # --max-num-seqs 折进来）。它非零就别再按显存推——Bug 1 就是这条
        # 路断了：用户传了 12，build_command 只认 override，profile 上那个
        # 数从来没被读过，日志里永远是按显存算出来的 2。
        want = int(getattr(profile, "slots", 0) or 0)
        if want < 0:
            # 负槽位不可能有意义，且不同路径的结果不一致（这里会被判成"按
            # 显存取 12"，override 路径会被 max(1,..) 变成 1）。直接拒绝。
            raise ConfigError(f"槽位数不能是负数，收到 {want}")
        if want > 0:
            slots = max(1, min(64, want))
        else:
            slots = SLOT_TIERS[-1][1]
            for floor, n in SLOT_TIERS:
                if (vram_gb or 0.0) >= floor:
                    slots = n
                    break
    # 模型自带的最长上下文是硬上限，不能超过
    ctx_per_slot = min(int(profile.max_model_len), CTX_PER_SLOT)
    need = image_tokens_for(profile) + MIN_CTX_HEADROOM
    if ctx_per_slot < need:
        raise ConfigError(
            f"每 slot 上下文 {ctx_per_slot} 装不下一个请求：视觉 "
            f"{image_tokens_for(profile)} token + prompt/输出至少 "
            f"{MIN_CTX_HEADROOM}。把 max_model_len 调大，或降低 max_pixels。")
    return slots, ctx_per_slot, KV_TYPE


def kv_bytes_per_slot(profile: RunProfile, ctx_per_slot: int = CTX_PER_SLOT,
                      kv_type: str = KV_TYPE) -> int:
    """一个 slot 的 KV 缓存大约占多少字节。

    为什么要给页面一个数：用户想"打标的时候顺便玩把游戏"，需要知道把 slot
    从 12 降到 4 到底能腾出多少显存。现在这个关系完全看不见，只能靠试。

    口径是粗估而非精确：层数 × 2（K/V）× 每 slot 上下文 × KV 头维度 ×
    每元素字节。拿不到层数和头维度时按 8B 档的 36 层 / 8 个 KV 头 / 128
    维算——宁可估大一点（估小了用户会真 OOM）。
    """
    per_elem = 1.0 if str(kv_type).lower() in ("q8_0", "q8") else 2.0
    layers = 36
    kv_heads = 8
    head_dim = 128
    return int(layers * 2 * ctx_per_slot * kv_heads * head_dim * per_elem)


def slot_plan_memory(profile: RunProfile, slots: int,
                     ctx_per_slot: int = CTX_PER_SLOT,
                     kv_type: str = KV_TYPE) -> dict:
    """槽位方案对应的 KV 显存估算（GiB），给页面显示"降 slot 能省多少"。"""
    per_slot = kv_bytes_per_slot(profile, ctx_per_slot, kv_type)
    total = per_slot * max(1, slots)
    gib = 1024 ** 3
    return {
        "slots": slots,
        "ctx_per_slot": ctx_per_slot,
        "ctx_total": ctx_per_slot * max(1, slots),
        "kv_type": kv_type,
        "kv_per_slot_gib": round(per_slot / gib, 2),
        "kv_total_gib": round(total / gib, 2),
    }


def build_command(
    profile: RunProfile,
    *,
    port: int,
    served_name: str,
    gguf: Path,
    mmproj: Path,
    vram_gb: float | None = None,
    slots_override: int | None = None,
) -> list[str]:
    """拼 llama-server 命令行。每个参数都对应一个会让产出漂移的变量。"""
    binary = find_server_binary()
    if binary is None:                      # check_engine 已经拦过，这里兜底
        raise ConfigError("找不到 llama-server")
    slots, ctx_per_slot, kv_type = slot_plan(profile, vram_gb, slots_override)
    ctx_total = slots * ctx_per_slot
    cmd = [
        binary,
        "-m", str(gguf),
        "--mmproj", str(mmproj),
        "-a", served_name,
        "--host", "127.0.0.1",
        "--port", str(port),
        "-c", str(ctx_total),
        "-np", str(slots),
        # KV 缓存量化：只影响显存，不改产出分布；省下来的一半显存用来换 slot。
        # ★这也是"switching 成本"：它改了运行参数，指纹要记，否则两批数据
        #   的差异会被错误归因到模型身上。
        "-ctk", kv_type,
        "-ctv", kv_type,
        # 全部层上卡。16GB 卡上 8B-Q4 加视觉塔还剩得下 32K 上下文；
        # 留一部分在内存反而会因 PCIe 往返把 decode 拖慢一个数量级。
        "-ngl", "99",
        "--image-max-tokens", str(image_tokens_for(profile)),
        "--metrics",
    ]
    return cmd


def probe_version(base_url: str) -> str | None:
    """llama-server 没有 /version，构建号在 /props 的 build_info 里。"""
    try:
        r = httpx.get(f"{base_url}/props", timeout=5,
                      trust_env=trust_env_for(base_url))
        if r.status_code != 200:
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    for key in ("build_info", "version", "system_fingerprint"):
        val = data.get(key)
        if val:
            return str(val)
    return None


def readiness(base_url: str) -> tuple[bool, str]:
    """探一次就绪。llama-server 的 /health 返回 {"status":"ok"}。"""
    try:
        r = httpx.get(f"{base_url}/health", timeout=5,
                      trust_env=trust_env_for(base_url))
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        body = r.json()
    except ValueError:
        return True, ""                    # 有些版本回纯文本，200 就算就绪
    status = str(body.get("status") or "").lower()
    if status and status not in ("ok", "ready"):
        return False, status
    return True, ""


def describe(profile: RunProfile, slots_override: int | None = None) -> dict:
    """给页面看的引擎摘要：换引擎等于换一代数据，这些字段要能一眼看到。

    槽位三项（生效值、来源、KV 显存）也放这里：它们是同一次启动里互相
    牵制的数，分两处显示只会让人以为可以各自调。
    """
    info: dict = {
        "engine": "llamacpp",
        "served_name": "",
        "gguf": str(getattr(profile.model, "gguf", "") or ""),
        "mmproj": str(getattr(profile.model, "mmproj", "") or ""),
    }
    try:
        info["image_max_tokens"] = image_tokens_for(profile)
    except ConfigError as exc:
        info["image_max_tokens"] = None
        info["error"] = str(exc)
    try:
        slots, ctx_per_slot, kv_type = slot_plan(
            profile, getattr(profile, "vram_gb", None), slots_override)
        info["slots"] = slots
        # 留空时这个是 False：页面据此显示"按显存自动"，而不是假装用户选过。
        info["slots_manual"] = bool(slots_override)
        info.update(slot_plan_memory(profile, slots, ctx_per_slot, kv_type))
        # 不填时该档的默认值，方便页面把"自动会给你 12"写出来。
        info["slots_default"] = slot_plan(
            profile, getattr(profile, "vram_gb", None), None)[0]
    except ConfigError as exc:
        info["slots_error"] = str(exc)
    return info
