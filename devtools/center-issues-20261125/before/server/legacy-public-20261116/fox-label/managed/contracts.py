"""与打标器共用的托管配置契约。"""
import math
import re


class ManagedError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

    def body(self):
        return {"ok": False, "error": str(self), "code": "managed_error"}


DEFAULTS = dict(inference_source="local", mode="both", tag_backend="wd-eva02",
                tag_device="auto", tag_precision="tf32", tag_batch=0,
                nl_engine="llamacpp", nl_model="", nl_device="auto", nl_precision="f16",
                served_name="tagger", concurrency=0, gpu_mem=0,
                auto_download=True, auto_quantize=False, stop_service=True)
ENUMS = dict(inference_source=("local", "existing"), mode=("both", "tag", "nl"),
             tag_backend=("wd-eva02", "pixai", "cl-tagger-v2", "onnx", "none"),
             tag_precision=("tf32", "fp32", "bf16", "fp16"), nl_engine=("llamacpp", "vllm", "native"))
TERMINAL = ("applied", "failed", "canceled")


def identifier(value, label):
    if (not isinstance(value, str) or not value or len(value) > 128
            or any(ord(ch) < 32 for ch in value)):
        raise ManagedError(f"{label} 格式不正确")
    return value


def config(changes, base=None, capabilities=None):
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ManagedError("配置包含不支持的字段")
    merged = {**DEFAULTS, **(base or {}), **changes}
    for key, values in ENUMS.items():
        if merged[key] not in values:
            raise ManagedError(f"{key} 选项不正确")
    for key in ("auto_download", "auto_quantize", "stop_service"):
        if type(merged[key]) is not bool:
            raise ManagedError(f"{key} 必须为布尔值")
    for key, limit in (("tag_batch", 128), ("concurrency", 64)):
        if type(merged[key]) is not int or not 0 <= merged[key] <= limit:
            raise ManagedError(f"{key} 必须为 0～{limit} 的整数")
    mem = merged["gpu_mem"]
    if (type(mem) not in (int, float) or not math.isfinite(mem)
            or (mem != 0 and not 0.1 <= mem <= 0.98)):
        raise ManagedError("显存比例必须为 0（自动）或 0.1～0.98")
    for key in ("tag_device", "nl_device"):
        value = merged[key]
        if not isinstance(value, str) or not re.fullmatch(r"auto|cpu|cuda:[0-9]+", value):
            raise ManagedError(f"{key} 设备格式不正确")
        if key == "nl_device" and value == "cpu" and merged["nl_engine"] == "vllm":
            raise ManagedError("只有 llama.cpp 支持 CPU；vLLM 请选择自动或 CUDA 设备")
    for key in ("nl_model", "served_name"):
        value = merged[key]
        if not isinstance(value, str) or len(value) > 256 or any(ord(ch) < 32 for ch in value):
            raise ManagedError(f"{key} 格式不正确")
        merged[key] = value.strip()
    if not merged["served_name"]:
        raise ManagedError("服务模型名不能为空")
    prec = ("f16", "q4", "q6", "q8") if merged["nl_engine"] == "llamacpp" else ("bf16", "fp8", "w4a16")
    if merged["nl_engine"] == "native":
        prec = ("fp16", "bf16", "fp8", "int8", "w4a16", "f16", "q4", "q5", "q6", "q8")
    if merged["nl_precision"] not in prec:
        raise ManagedError("NL 精度与引擎不匹配")
    if merged["mode"] == "tag" and merged["tag_backend"] == "none":
        raise ManagedError("只出 TAG 时请选择分类模型")
    if merged["mode"] != "tag" and merged["inference_source"] == "local" and merged["nl_model"] == "torii-gate-0.5":
        limit = 4 if merged["nl_engine"] == "native" else 1
        if merged["nl_engine"] not in ("native", "llamacpp") or merged["nl_precision"] != "q6" or merged["concurrency"] > limit:
            raise ManagedError(f"Torii 需要 native／llama.cpp、Q6；当前引擎并发上限 {limit}")
    if capabilities is not None:
        devices = {x["value"] for x in capabilities.get("devices", [])}
        for key in ("tag_device", "nl_device"):
            if merged[key].startswith("cuda:") and merged[key] not in devices:
                raise ManagedError(f"设备没有 {merged[key]}")
        if merged["mode"] != "nl" and merged["tag_backend"] != "none":
            if merged["tag_backend"] not in {x["backend"] for x in capabilities.get("tag_models", [])}:
                raise ManagedError("设备未报告这个 TAG 模型")
        if merged["mode"] != "tag" and merged["inference_source"] == "local":
            if merged["nl_engine"] not in capabilities.get("engines", []):
                reason = capabilities.get("engine_status", {}).get(merged["nl_engine"], {}).get("reason", "")
                raise ManagedError("设备当前无法启动这个 NL 引擎" + ("：" + reason if reason else ""))
            if merged["nl_model"] and merged["nl_model"] not in {x["key"] for x in capabilities.get("nl_models", [])}:
                raise ManagedError("设备未报告这个 NL 模型")
    return merged


def public(value):
    """展示字段移除凭据及目录；错误文本中的 HF token 同样脱敏。"""
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items()
                if not any(s in k.lower() for s in ("token", "secret", "password", "path", "directory"))}
    if isinstance(value, list):
        return [public(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"hf_[A-Za-z0-9_]+", "[已隐藏]", value)
    return value


def report(body):
    if not isinstance(body, dict) or type(body.get("protocol_version")) is not int or body["protocol_version"] != 1:
        raise ManagedError("托管协议版本不匹配")
    session = identifier(body.get("session_id"), "session_id")
    seq = body.get("last_seq")
    if type(seq) is not int or seq < 0 or type(body.get("released", False)) is not bool:
        raise ManagedError("托管序号或退出标志格式不正确")
    state, caps, results = body.get("state", {}), body.get("capabilities", {}), body.get("results", [])
    if not isinstance(state, dict) or not isinstance(caps, dict) or not isinstance(results, list) or len(results) > 64:
        raise ManagedError("托管状态、能力或结果格式不正确")
    fields = dict(devices=("value", "physical_index", "name", "vram_gb"),
                  tag_models=("backend", "ready"),
                  nl_models=("key", "raw_ready", "ready", "gguf_ready", "quantized"))
    clean = {}
    for key, keys in fields.items():
        rows = caps.get(key, [])
        if not isinstance(rows, list) or len(rows) > 256 or any(not isinstance(x, dict) for x in rows):
            raise ManagedError(f"{key} 能力格式不正确")
        primary = keys[0]
        clean[key] = [public({k: x[k] for k in keys if k in x}) for x in rows
                      if isinstance(x.get(primary), str)]
    engines = caps.get("engines", [])
    if not isinstance(engines, list):
        raise ManagedError("引擎能力格式不正确")
    clean["engines"] = [x for x in engines if x in ("llamacpp", "vllm", "native")]
    clean["actions"] = [x for x in caps.get("actions", []) if x in ("configure", "probe", "force_continue")]
    if 'remote_concurrency' in caps:
        value=caps['remote_concurrency']
        if not isinstance(value,dict) or set(value)-{'concurrency','tag_batch'} or any(type(v) is not bool for v in value.values()):
            raise ManagedError('远程并发授权格式不正确')
        clean['remote_concurrency']=value
    status = caps.get("engine_status", {})
    if not isinstance(status, dict):
        raise ManagedError("引擎状态格式不正确")
    clean["engine_status"] = {engine: {"available": value.get("available") is True,
                                     "reason": public(str(value.get("reason", ""))[:2000])}
                              for engine, value in status.items()
                              if engine in ("llamacpp", "vllm", "native") and isinstance(value, dict)}
    return session, seq, public({k: state[k] for k in
        ("status", "running", "preparing", "message", "error", "last_rc", "progress", "telemetry", "resolved", "probe", "pending_action", "history_running", "history_status", "history_message", "history_only", "execution_limit") if k in state}), clean, results
