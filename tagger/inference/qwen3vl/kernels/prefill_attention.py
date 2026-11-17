"""Optional cuDNN prefill with PyTorch math fallback on unsupported devices."""
from __future__ import annotations

from inspect import signature

from torch.nn import functional as F
from torch.nn.attention import SDPBackend, sdpa_kernel

_PRIORITY_ARGS = {"set_priority": True} if "set_priority" in signature(sdpa_kernel).parameters else {}


def cudnn_attention(q, k, v, mask):
    # Allow the dispatcher to retain math SDPA for unsupported dtype/shape/GPU.
    # This changes floating-point reduction order only when cuDNN is selected.
    with sdpa_kernel([SDPBackend.CUDNN_ATTENTION, SDPBackend.MATH], **_PRIORITY_ARGS):
        return F.scaled_dot_product_attention(q, k, v, attn_mask=mask, enable_gqa=True)
