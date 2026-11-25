"""本机执行配置；托管可顺序调整并发和批大小。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hosting_mode: Literal["active", "passive"] = "passive"
    allow_remote_tag_batch: bool = False
    allow_remote_concurrency: bool = False
    inference_source: Literal["local", "existing"] = "local"
    mode: Literal["both", "tag", "nl"] = "both"
    tag_backend: Literal["wd-eva02", "pixai", "cl-tagger-v2", "onnx", "none"] = "wd-eva02"
    tag_device: str = "auto"
    tag_precision: Literal["tf32", "fp32", "bf16", "fp16"] = "tf32"
    tag_batch: int = Field(0, ge=0, le=128)
    nl_engine: Literal["llamacpp", "vllm", "native"] = "llamacpp"
    nl_model: str = ""
    nl_device: str = "auto"
    nl_precision: str = "f16"
    served_name: str = "tagger"
    concurrency: int = Field(0, ge=0, le=256)
    max_model_len: int = Field(0, ge=0, le=262144)
    gpu_mem: float = Field(0, ge=0, le=0.98)
    auto_download: bool = True
    auto_quantize: bool = False
    stop_service: bool = True
    resume_on_startup: bool = False

    @model_validator(mode="after")
    def validate_execution(self):
        from ...config import ConfigError, normalize_quant_precision

        try:
            self.nl_precision = normalize_quant_precision(self.nl_precision, engine=self.nl_engine) or "f16"
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        if self.nl_engine == "native":
            from ...inference.qwen3vl.runtime import PRECISIONS

            allowed = PRECISIONS
        else:
            allowed = ("f16", "q4", "q6", "q8") if self.nl_engine == "llamacpp" else ("bf16", "fp8", "w4a16")
        if self.nl_precision not in allowed:
            raise ValueError(f"{self.nl_engine} 的精度只能选择 {' / '.join(allowed)}")
        if self.mode == "tag" and self.tag_backend == "none":
            raise ValueError("只出 TAG 时请选择本地分类模型")
        for name in ("tag_device", "nl_device"):
            value = getattr(self, name).strip()
            valid = value in ("auto", "cpu") or (value.startswith("cuda:") and value[5:].isdigit())
            if not valid or (name == "nl_device" and value == "cpu" and self.nl_engine == "vllm"):
                raise ValueError(f"{name} 请选择自动或可用的 CUDA 设备" + (" / CPU" if name == "tag_device" else ""))
            setattr(self, name, value)
        self.nl_model = self.nl_model.strip()
        self.served_name = self.served_name.strip()
        if self.mode != "tag" and self.inference_source == "local" and self.nl_model == "torii-gate-0.5":
            from ..torii_service import plan
            resolved = plan(self.nl_engine, self.nl_precision, self.concurrency)
            if not resolved["ok"]:
                raise ValueError(resolved["error"])
            if self.mode == "both" and self.tag_backend == "none":
                raise ValueError("Torii 描述模式请配合 PixAI 等分类模型生成标签")
        if not self.served_name:
            raise ValueError("服务模型名不能为空")
        if self.gpu_mem and self.gpu_mem < 0.1:
            raise ValueError("显存比例应为 0（自动）或 0.1～0.98")
        if self.max_model_len and self.max_model_len < 512:
            raise ValueError('模型上下文应为 0（自动）或 512～262144')
        return self


EXECUTION_DEFAULTS = ExecutionConfig().model_dump()
# 目录、身份、凭据和命令模板始终由本机管理。
REPORT_FIELDS = set(EXECUTION_DEFAULTS) - {"hosting_mode", "resume_on_startup", "max_model_len", "allow_remote_tag_batch", "allow_remote_concurrency"}
# 模型、精度、目录和凭据由本机管理；服务端可调整吞吐参数。
REMOTE_FIELDS = {"concurrency", "tag_batch"}


def execution_config(cfg: dict) -> dict:
    return ExecutionConfig(**{k: v for k, v in cfg.items() if k in EXECUTION_DEFAULTS}).model_dump()
