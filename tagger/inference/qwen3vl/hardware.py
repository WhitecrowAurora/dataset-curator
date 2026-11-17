"""Explicit device planning; constructing a profile never probes CUDA."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    name: str
    capability: tuple[int, int] | None = None

    @property
    def fp8_tensor_core(self):
        return self.capability is not None and self.capability >= (8, 9)

    @property
    def bf16_tensor_core(self):
        return self.capability is not None and self.capability >= (8, 0)

    @property
    def triton_candidate(self):
        # Current Triton toolchains do not cover Volta; use the portable path.
        return self.capability is not None and self.capability >= (8, 0)


DEVICE_FAMILIES = (
    DeviceProfile("V100", (7, 0)), DeviceProfile("Turing / RTX 20", (7, 5)),
    DeviceProfile("Ampere / RTX 30", (8, 6)), DeviceProfile("Ada / RTX 40", (8, 9)),
    DeviceProfile("Hopper", (9, 0)), DeviceProfile("Blackwell / RTX 50", (12, 0)),
)


@dataclass(frozen=True, slots=True)
class KernelPlan:
    storage: str
    activation: str
    accumulation: str
    linear: str
    attention: str
    native_low_precision: bool


def plan_kernels(profile: DeviceProfile, storage: str, *, optimized: bool = False,
                 native: bool = False) -> KernelPlan:
    """Plan default A16 or explicitly requested A8 execution without probing CUDA."""
    storage = storage.lower()
    gguf = storage in ("q4_0", "q4_1", "q5_0", "q8_0", "q4_k", "q5_k", "q6_k")
    low = storage in ("fp8", "int8", "w4a16", "w8a16")
    if native and storage in ("fp8", "int8"):
        supported = profile.fp8_tensor_core if storage == "fp8" else profile.triton_candidate
        use_native = optimized and supported
        return KernelPlan(storage, storage, "int32_then_fp32" if storage == "int8" else "fp32",
                          "triton_w8a8" if use_native else "a8_reference_dequant_gemm",
                          "torch_sdpa", use_native)
    fused = (optimized and profile.triton_candidate and (gguf or low)
             and (storage != "fp8" or profile.fp8_tensor_core))
    return KernelPlan(storage, "fp16" if profile.capability else "fp32", "fp32",
                      ("triton_gguf" if gguf else "triton_scaled") if fused else "tiled_dequant_gemm",
                      "torch_sdpa", False)

