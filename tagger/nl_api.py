"""外部推理 API 适配层：OpenAI 兼容接口 与 Anthropic Messages 接口。

为什么要有这一层
----------------
本机 vLLM / llama.cpp 都提供 **OpenAI 兼容**的 `/v1/chat/completions`，客户端
一直按那个形状发请求（见 client._infer_at）。换成云端 API 时只有两种方言：

* **OpenAI 兼容**：请求/响应形状与本机完全一样，只多一个 `Authorization` 头。
  唯一的坑是 base_url 常写成 `https://api.openai.com/v1`——照老代码再拼
  `/v1/chat/completions` 就成了 `/v1/v1/...`，所以端点必须归一化。
  本地跑 LM Studio / one-api / vLLM 的也走这一支（key 可以留空）。

* **Anthropic Messages**：系统提示是顶层 `system` 字段，图片是
  `{"type":"image","source":{...base64...}}`，响应是 `content` 数组而不是
  `choices[0].message.content`，token 数叫 `output_tokens`，而且**不认 seed**。
  照抄 OpenAI 的 body 会被 400 拒绝（unknown field），报错信息还只说参数非法。

把这些差异全部收在这个模块里，client.py 只关心"给（图 + prompt）拿（文本 + token）"。
多出来的方言往这里加，不要去改 client 的推理主循环。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

# "" = 本机/自建（OpenAI 兼容，但由我们自己起服务、有环境指纹）
LOCAL = ""
# 页面下拉用的顺序与文案，前端从 /api/defaults 的 vlm_providers 读同一份。
PROVIDERS = ("", "openai", "anthropic")
LABELS = {
    "": "本机 / 自建服务",
    "openai": "OpenAI 兼容 API",
    "anthropic": "Anthropic API",
}
# 页面上「地址」一栏的默认值。留空时 run_client 会用它，用户不必背域名。
DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
# key 没填时去哪个环境变量找。API 模式密钥不该逼着人写进命令行历史。
ENV_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
}

ANTHROPIC_VERSION = "2023-06-01"

# Anthropic 只收这四种位图。bmp / tiff 会被 400 拒绝，客户端先转 PNG。
ANTHROPIC_IMAGE_MIMES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})

# ------------------------------------------------------------------ 失败分类
#
# 本机服务几乎只会"成功或超时"，云端才会成片地返回 429 / 529 / 401。
# 把这几类混成"HTTP 错误，重试"，后果完全不同：
#   * 429 当永久失败 → 批量任务在限流时整批变废；
#   * 401/403/404 当可重试 → 密钥或模型名写错时，每张图都重试三次，
#     30 万张要跑到天亮才告诉你"key 无效"。
# 所以分三档：retry（退避再来）、fatal（整批中止）、fail（这张不要了）。
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
# 401 密钥错、403 无权、404 模型名/端点错——都是配置问题，跟具体哪张图无关。
FATAL_STATUSES = frozenset({401, 403, 404})


def classify_status(status: int) -> tuple[bool, bool]:
    """→ (可重试, 属于配置性错误)。两者互斥，且只有 5xx/4xx 才会进这里。"""
    if status in FATAL_STATUSES:
        return False, True
    if status in RETRY_STATUSES or status >= 500:
        return True, False
    return False, False


def retry_after_seconds(headers) -> float | None:
    """读 `Retry-After`（秒数形式）。拿不到就 None，由调用方用指数退避。

    大小写都认：httpx 的 Headers 本身不区分，但测试替身与部分代理给的是
    普通 dict，只查小写会静默丢掉"服务端要求的等待时间"。
    """
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        items = getattr(headers, "items", None)
        if callable(items):
            for key, value in items():
                if str(key).lower() == "retry-after":
                    raw = value
                    break
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        # 也可能是 HTTP-date。解析收益不大，交给指数退避即可。
        return None


# ------------------------------------------------------------------ 各来源的默认值

# 云端模型的推荐输入尺寸与本地 serve 的钳制不是一回事：
#   * Anthropic 建议长边 ≈1568px（再大只是多付 token，模型看不到更多细节）；
#   * OpenAI 的 detail=high 会把图缩进 2048×2048 再切块。
# 沿用本地的 1024×1024 基线等于白付像素钱还降质，所以按来源各给一档。
DEFAULT_MAX_PIXELS = {
    "openai": 2048 * 2048 // 2,
    "anthropic": 1568 * 1176,
}
# 保守的在途请求数：云端跟本机不同，撞到限流是一整批的事。
# 本机走指纹里的 max_num_seqs，这里只给 API 模式的起点。
DEFAULT_CONCURRENCY = {
    "openai": 4,
    "anthropic": 2,
}
# 页面「模型名」的建议值。只是 <datalist> 提示，可以自由填写别的 id。
# 不放价格的同一条理由：这张表会过时，价格比模型 id 变得更快。
MODEL_HINTS = {
    "openai": ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"),
    "anthropic": ("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"),
}

# 价目（每 100 万 token，币种随账单）。**故意默认为空**：
# 价格按月变，写死在代码里过两周就开始说谎，而"成本估算"这件事一旦算错
# 比不算更糟。要用就在 configs/nl_api.json 里按 model_id 填 [输入, 输出]。
SAVED_CONFIG = "configs/nl_api.json"


def default_max_pixels(provider: str) -> int:
    return int(DEFAULT_MAX_PIXELS.get(provider, 0))


def default_concurrency(provider: str) -> int:
    return int(DEFAULT_CONCURRENCY.get(provider, 0))


def model_hints(provider: str) -> list[str]:
    return list(MODEL_HINTS.get(provider, ()))

# 用户可能填到"完整端点"，也可能只填域名。两种都得认。
_FULL_ENDPOINT = re.compile(r"/(chat/completions|messages)$", re.I)
_VERSIONED = re.compile(r"/v\d+(?:beta)?$", re.I)


@dataclass(slots=True)
class ImageBlob:
    """一张待发送的图。三种形态并存是刻意的：

    * `data_url`：OpenAI 兼容接口要的就是它（可以是 `file://`）
    * `b64`     ：Anthropic 只收裸 base64 + media_type
    * `path`    ：`file://` 直传时落盘的临时文件，产物收尾要删
    """

    mime: str
    data_url: str
    b64: str = ""
    path: Path | None = None


def normalize_provider(value: str | None) -> str:
    """把表单/命令行里五花八门的写法收敛成三选一。

    认不出来直接抛（带可选值），不静默退回本地——"以为在用 API、实际在打
    本机服务"是这类配置里代价最高的误解。
    """
    text = str(value or "").strip().lower()
    if text in ("", "local", "none", "off", "vllm", "llamacpp"):
        return LOCAL
    if text in ("openai", "openai-compatible", "openai_compatible", "compat"):
        return "openai"
    if text in ("anthropic", "claude", "anthropic-messages"):
        return "anthropic"
    raise ValueError(
        f"未知的推理来源 {value!r}：只能是 local / openai / anthropic "
        f"（openai 也覆盖 LM Studio、one-api 这类 OpenAI 兼容服务）")


def default_base(provider: str) -> str:
    return DEFAULT_BASES.get(provider, "")


def env_api_key(provider: str) -> str:
    """密钥的兜底来源。命令行走环境变量比写进 shell 历史安全。"""
    import os

    for name in ENV_KEYS.get(provider, ()):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def auth_headers(provider: str, api_key: str) -> dict[str, str]:
    """鉴权头。挂成 httpx.Client 的默认头，`post()` 的调用形状就不必改。"""
    key = (api_key or "").strip()
    if provider == "anthropic":
        # anthropic-version 是必需的，不传会被 400（且提示不指向版本头）。
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if key:
            headers["x-api-key"] = key
        return headers
    if provider == "openai" and key:
        return {"Authorization": f"Bearer {key}"}
    return {}


def resolve_credentials(provider: str, base: str = "", key: str = "") -> tuple[str, str]:
    """显式值优先；保存的密钥只用于保存时绑定的服务地址。"""
    name = normalize_provider(provider)
    saved = saved_for(name)
    saved_base = str(saved.get("base") or default_base(name)).strip()
    resolved_base = str(base or "").strip() or saved_base
    same_base = bool(name) and endpoint(resolved_base, name) == endpoint(saved_base, name)
    resolved_key = (str(key or "").strip()
                    or (str(saved.get("key") or "").strip() if same_base else "")
                    or env_api_key(name))
    return resolved_base, resolved_key


def endpoint(base_url: str, provider: str) -> str:
    """拼出真正要 POST 的地址。

    四种输入都要能跑，因为用户是复制粘贴过来的：
      `http://127.0.0.1:8000`            → /v1/chat/completions
      `https://api.openai.com/v1`        → /v1/chat/completions（不重复加 v1）
      `https://x/openai/v1`              → 同上
      `https://x/v1/chat/completions`    → 原样用
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("推理服务的地址是空的")
    path = "messages" if provider == "anthropic" else "chat/completions"
    if _FULL_ENDPOINT.search(base):
        return base
    if _VERSIONED.search(base):
        return f"{base}/{path}"
    return f"{base}/v1/{path}"


def coerce_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Anthropic 只认四种位图；其它格式先转 PNG。

    不转的后果是每张 bmp/tiff 都 400，而错误只说"media_type 不支持"，
    批量任务里看起来像是随机失败。
    """
    if mime in ANTHROPIC_IMAGE_MIMES:
        return data, mime
    from PIL import Image

    with Image.open(BytesIO(data)) as im:
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = BytesIO()
        im.save(buf, "PNG")
    return buf.getvalue(), "image/png"


def blob_from_bytes(data: bytes, mime: str, *, provider: str = "") -> ImageBlob:
    """把图片字节装成一份可发送的 blob（总是 base64）。"""
    if provider == "anthropic":
        data, mime = coerce_image(data, mime)
    b64 = base64.b64encode(data).decode("ascii")
    return ImageBlob(mime=mime, data_url=f"data:{mime};base64,{b64}", b64=b64)


def blob_from_path(path: Path, mime: str) -> ImageBlob:
    """`file://` 直传用：不编码，省掉 33% 体积与一次 base64。

    只有本机引擎认这个 URL；Anthropic / 远程 API 一律走 blob_from_bytes。
    """
    return ImageBlob(mime=mime, data_url=path.resolve().as_uri(), path=path)


def image_block(provider: str, image: ImageBlob) -> dict:
    if provider == "anthropic":
        return {"type": "image",
                "source": {"type": "base64", "media_type": image.mime,
                           "data": image.b64}}
    return {"type": "image_url", "image_url": {"url": image.data_url}}


def build_payload(provider: str, *, model: str, system: str, prompt: str,
                  image: ImageBlob, temperature: float, top_p: float,
                  max_tokens: int, seed: int | None,
                  repeat_penalty: float = 0.0) -> dict:
    """按方言组装请求体。不要在这里写鉴权——那是 client 的默认头。"""
    if provider == "anthropic":
        payload = {
            "model": model,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "top_p": float(top_p),
            # system 是顶层字段，塞进 messages 会被当成用户发言。
            "system": system,
            "messages": [{"role": "user",
                          "content": [image_block(provider, image),
                                      {"type": "text", "text": prompt}]}],
        }
        # Anthropic 不认 seed：带上它是 400 invalid_request_error。
        # 少一个可复现维度是真的，但那是接口能力边界，不是这里能补的。
        return payload
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [image_block(provider, image),
                                         {"type": "text", "text": prompt}]},
        ],
        "temperature": float(temperature),
        "top_p": float(top_p),
        "max_tokens": int(max_tokens),
    }
    if seed is not None:
        payload["seed"] = seed
    # 重复惩罚是 llama.cpp 的扩展字段，默认不发（vLLM 形状必须逐字不变，
    # 多一个字段老服务会 400）。只有本地引擎显式配了才带上。
    #
    # 为什么需要它：文字密集的图上模型会陷进 "xxx_text_composition,
    # xxx_text_layout, xxx_text_color, xxx_text_font, xxx_text_style" 的
    # 死循环，把 max_tokens 吃干、caption 一个字都不出（实测 h5 关闭时 4/6 失败）。
    if repeat_penalty and repeat_penalty > 0 and not provider:
        payload["repeat_penalty"] = float(repeat_penalty)
    return payload


def parse_response(provider: str, body: dict) -> tuple[str, int, int]:
    """取 (文本, 输入 token, 输出 token)。

    两种方言的空产出都返回空串——交给 client 的"没有 caption / tag"判废，
    不要在这里抛：那会让一次成功的请求被当成网络故障重试三遍。

    输入 token 必须一起取：图片走 base64 时 prompt 动辄几千 token，云端按量
    计费时它才是账单的大头，只统计输出等于把成本算少一个数量级。
    """
    if provider == "anthropic":
        blocks = body.get("content") or []
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type", "text") == "text")
        usage = body.get("usage") or {}
        return (text, int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0))

    choices = body.get("choices") or []
    if not choices or not isinstance(choices, list):
        raise ValueError("响应里没有 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # 少数网关（Gemini 系、部分代理）把 content 返成 parts 数组。
        text = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict))
    else:
        text = str(content or "")
    usage = body.get("usage") or {}
    return (text, int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0))


# ------------------------------------------------------------------ 成本


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  prices: dict | None = None) -> float | None:
    """按 model_id 查价目算钱。查不到返回 None（= 只报 token，不编数字）。

    匹配用前缀：模型名后面常带日期后缀（`gpt-4o-2024-08-06`），
    精确匹配会让价目表天天失效。
    """
    table = prices or {}
    if not table:
        return None
    name = str(model or "").strip()
    hit = table.get(name)
    if hit is None:
        for key, value in table.items():
            if name.startswith(str(key)):
                hit = value
                break
    if not isinstance(hit, (list, tuple)) or len(hit) < 2:
        return None
    try:
        p_in, p_out = float(hit[0]), float(hit[1])
    except (TypeError, ValueError):
        return None
    return (prompt_tokens / 1_000_000.0) * p_in + \
           (completion_tokens / 1_000_000.0) * p_out


# ------------------------------------------------------------------ 本机存档
#
# 密钥默认只活在这个请求里。要记住就落 configs/nl_api.json（与 models.json
# 同级，便于整目录备份/清理），文件里是明文——所以页面上写清楚"仅本机"，
# 并且回显一律脱敏，浏览器永远拿不到完整 key。


def saved_path() -> Path:
    """运行时读常量，测试才好指到临时目录。"""
    return Path(SAVED_CONFIG)


def load_saved() -> dict:
    """→ {"providers": {...}, "prices": {...}, "hf_token": str, "hf_repo": str}

    `hf_token` / `hf_repo` 是后加的：以前这个文件只放 API 密钥，HF 令牌
    哪里都不存（`ui_form` 的注释说"有专门的存档"，实际并没有），于是每次
    新建 HF 任务都得重新粘一次。见 `store_hf_token`。
    """
    empty: dict = {"providers": {}, "prices": {}, "hf_token": "", "hf_repo": ""}
    p = saved_path()
    if not p.is_file():
        return empty
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    providers = raw.get("providers")
    prices = raw.get("prices")
    return {
        "providers": providers if isinstance(providers, dict) else {},
        "prices": prices if isinstance(prices, dict) else {},
        # 老文件没有这两个键：空串 = 没存过，与"存了个空字符串"同义。
        "hf_token": str(raw.get("hf_token") or "").strip(),
        "hf_repo": str(raw.get("hf_repo") or "").strip(),
    }


def save_saved(data: dict) -> None:
    """写回存档。先写 tmp 再 replace：半截 json 会让下次启动读不出任何密钥。"""
    p = saved_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(p)
    try:
        # 明文密钥文件：至少在类 Unix 上别让同机其他用户直接读。
        os.chmod(p, 0o600)
    except OSError:
        pass


def mask_key(key: str) -> str:
    """只留头尾，够人认出"用的是哪把"，不够拿去用。"""
    text = str(key or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]}"


def store_key(provider: str, base: str, key: str) -> dict:
    """记住 / 忘记一把密钥。key 传空 = 删除该来源的存档。"""
    data = load_saved()
    providers = dict(data.get("providers") or {})
    name = normalize_provider(provider)
    if not name:
        raise ValueError("本机模式没有 API 密钥可存")
    text = str(key or "").strip()
    if text:
        providers[name] = {"base": str(base or "").strip(), "key": text}
    else:
        providers.pop(name, None)
    data["providers"] = providers
    save_saved(data)
    return data


def saved_for(provider: str) -> dict:
    """没有存档就返回空 dict——不要回一份空壳，那会让页面以为"已记住"。"""
    name = normalize_provider(provider)
    if not name:
        return {}
    entry = (load_saved().get("providers") or {}).get(name) or {}
    if not isinstance(entry, dict) or not entry:
        return {}
    return {"base": str(entry.get("base") or ""),
            "key": str(entry.get("key") or "")}


# --------------------------------------------------------------- HF 令牌存档
#
# 与 API 密钥同一个文件、同一套落盘形态（明文 + chmod 600），因为理由一样：
# 用户已经接受"密钥明文存本机"，HF 令牌没有理由更保守。之前它是唯一不存的
# 一个，代价是每个私有仓库都要重新粘一次。
#
# ★存的时候连**仓库名**一起存，取的时候要求仓库对得上。这一条不是洁癖：
#   私有仓库 A 的 read 令牌被自动带进公共仓库 B 的请求里，就是把令牌交给了
#   一个与它无关的仓库方。API 密钥那边早就有同样的约束（见
#   `resolve_credentials` 里的 `same_base`），这里照抄。


def saved_hf() -> dict:
    """→ {"repo": str, "token": str}；没存过就是空 dict（与 saved_for 同口径）。"""
    data = load_saved()
    token = str(data.get("hf_token") or "").strip()
    if not token:
        return {}
    return {"repo": str(data.get("hf_repo") or "").strip(), "token": token}


def mask_hf(hf: dict | None = None) -> str:
    """脱敏值，页面只拿得到这个。"""
    entry = saved_hf() if hf is None else hf
    return mask_key(str(entry.get("token") or ""))


def store_hf_token(token: str, repo: str = "") -> dict:
    """记住 / 忘记 HF 令牌。token 传空 = 删掉存档（连仓库名一起删）。

    **只记仓库名、不记分支**：分支（main / 某个 revision）不是凭据，它已经
    存在 `ui-form.json` 里，跟着表单恢复就够；把它也塞进这里，会出现"存档
    里的分支压过用户刚改的分支"这种看不出原因的行为。
    """
    data = load_saved()
    text = str(token or "").strip()
    data["hf_token"] = text
    data["hf_repo"] = str(repo or "").strip() if text else ""
    save_saved(data)
    return data


def resolve_hf_token(repo: str, token: str = "") -> str:
    """显式给的优先；否则用存档，**且只在仓库对得上时**用。

    仓库对不上就返回空串，让请求以匿名身份发——私有仓库会得到 401/403，
    那个报错至少指向"没有读权限"，比"拿了别人的令牌去问"清楚得多。
    """
    given = str(token or "").strip()
    if given:
        return given
    saved = saved_hf()
    if not saved:
        return ""
    saved_repo = str(saved.get("repo") or "").strip()
    want = str(repo or "").strip()
    # 存档里没记仓库（老文件/手工改过）时**不用**：宁可让用户重贴一次，
    # 也不能把一把来历不明的令牌发出去。
    if not saved_repo or not want:
        return ""
    if saved_repo.lower() != want.lower():
        return ""
    return str(saved.get("token") or "").strip()


# ------------------------------------------------------------------ 连通性探测
#
# 本机模式有「启动 → 就绪」这个红灯，API 模式没有进程可看。没有预检的话，
# 密钥/模型名/地址写错要等任务跑起来、第一条产物失败才暴露——而那时已经
# 可能烧掉一批调用。这里发一张 8×8 的小图走真实请求，一次拿全三件事：
# 地址通不通、模型认不认、图能不能被看见（视觉能力）。

_PROBE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc"
    "0000001549444154789c63fcffff3f0336c0845574d04a000056d4030d37eccca3"
    "0000000049454e44ae426082"
)
PROBE_PROMPT = "Reply with the single word: ok"


def probe(*, provider: str, base: str, model: str, api_key: str = "",
          timeout_s: float = 30.0) -> dict:
    """跑一次最小请求。永远返回 dict（失败也带 error），不往外抛。"""
    import httpx

    started = time.perf_counter()
    name = normalize_provider(provider)
    out: dict = {"ok": False, "provider": name, "base": base, "model": model,
                 "latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "text": "", "error": ""}
    try:
        url = endpoint(base or default_base(name), name)
    except ValueError as exc:
        out["error"] = str(exc)
        return out
    blob = blob_from_bytes(_PROBE_PNG, "image/png", provider=name)
    payload = build_payload(name, model=str(model or "").strip() or "probe",
                            system="You are a helpful assistant.",
                            prompt=PROBE_PROMPT, image=blob,
                            temperature=0.0, top_p=1.0, max_tokens=16,
                            seed=None)
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0),
                          headers=auth_headers(name, api_key),
                          trust_env=True) as http:
            resp = http.post(url, json=payload)
        out["latency_ms"] = int((time.perf_counter() - started) * 1000)
        out["status"] = resp.status_code
        if resp.status_code >= 400:
            out["error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
            return out
        text, p_tok, c_tok = parse_response(name, resp.json())
        out.update(ok=True, text=text.strip()[:200],
                   prompt_tokens=p_tok, completion_tokens=c_tok)
        return out
    except httpx.TimeoutException:
        out["error"] = "超时（地址不可达或被墙）"
    except httpx.HTTPError as exc:
        out["error"] = f"连接失败: {type(exc).__name__}: {exc}"
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        out["error"] = f"响应结构不对: {type(exc).__name__}: {exc}"
    out["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return out
