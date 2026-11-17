"""Dense, scaled W8A16, native W8A8 and packed W4A16 checkpoints."""
from __future__ import annotations

import torch
from torch import nn

from .checkpoints import Checkpoint
from .decoder import CaptionDecoder
from .matrices.packed import PackedMatrix, unpack_int32
from .matrices.scaled import ScaledMatrix
from .quantization.activations import (
    DTYPES,
    ActivationQuantization,
    checkpoint_activation,
)


def checkpoint_matrix(checkpoint: Checkpoint, module: str, *, device, dtype, optimized=False,
                      native_precision=None, embedding=False):
    if native_precision not in (None, "fp8", "int8"):
        raise ValueError("native_precision must be 'fp8', 'int8', or None")
    quant = checkpoint.config.get("quantization_config") or {}
    scheme = checkpoint.scheme(module)
    activation_args = (scheme or {}).get("input_activations") if not embedding else None
    args = (scheme or {}).get("weights") or {}
    group_size = int(args.get("group_size") or 0)
    group_size = max(group_size, 0)
    scale = checkpoint.optional(module + ".weight_scale")
    zero = checkpoint.optional(module + ".weight_zero_point")
    groups = checkpoint.optional(module + ".weight_g_idx")
    if module + ".weight_packed" in checkpoint.files:
        if activation_args is not None or native_precision is not None:
            raise ValueError("Packed W4/W8 checkpoints currently execute A16")
        if quant.get("quant_method") != "compressed-tensors" or not args:
            raise ValueError("Packed weights require a compressed-tensors scheme")
        bits = int(args["num_bits"])
        shape = tuple(map(int, checkpoint.tensor(module + ".weight_shape").tolist()))
        if scale is None:
            raise ValueError(f"Missing packed weight scale: {module}")
        scale = scale.reshape(shape[0], -1) if scale.numel() != 1 else scale
        if not args.get("symmetric", True):
            if zero is None:
                raise ValueError(f"Missing asymmetric zero points: {module}")
            if zero.dtype == torch.int32:
                zero = unpack_int32(zero.T.contiguous(), shape[0], bits).T.contiguous()
            zero = zero.reshape(scale.shape)
        return PackedMatrix(checkpoint.tensor(module + ".weight_packed"), shape, scale,
                            group_size=group_size, zero_point=zero, group_index=groups,
                            bits=bits, optimized=optimized).to(device)
    weight = checkpoint.tensor(module + ".weight")
    inv_scale = checkpoint.optional(module + ".weight_scale_inv")
    if weight.dtype in (torch.float8_e4m3fn, torch.float8_e5m2, torch.int8):
        if inv_scale is not None:
            if inv_scale.dtype == torch.uint8:
                inv_scale = (inv_scale.float() - 127).exp2()
            scale = inv_scale
            block = tuple(quant.get("weight_block_size") or (128, 128))
        else:
            block = tuple(args["block_structure"]) if args.get("block_structure") else None
        if scale is None:
            raise ValueError(f"Missing FP8/INT8 weight scale: {module}")
        if scale.numel() > 1 and not block:
            scale = scale.reshape(weight.shape[0], -1)
        if zero is not None:
            zero = zero.reshape(scale.shape)
        options = {"group_size": group_size, "block_shape": block, "zero_point": zero,
                   "group_index": groups, "optimized": optimized}
        if activation_args is not None or native_precision is not None:
            from .matrices.native import NativeMatrix

            if native_precision is not None and DTYPES[native_precision] != weight.dtype:
                raise ValueError("native_precision does not match checkpoint weights")
            if activation_args is not None:
                activation, input_scale = checkpoint_activation(
                    activation_args, weight.dtype, checkpoint.optional(module + ".input_scale"),
                    checkpoint.optional(module + ".input_zero_point"))
            else:
                activation, input_scale = ActivationQuantization(weight.dtype), None
            return NativeMatrix(weight, scale, activation=activation, input_scale=input_scale, **options).to(device)
        return ScaledMatrix(weight, scale, **options).to(device)
    if not weight.is_floating_point():
        raise ValueError(f"Unsupported checkpoint matrix dtype: {weight.dtype}")
    if activation_args is not None:
        raise ValueError("A8 schemes require stored 8-bit weights and weight scales")
    if native_precision is not None:
        from .quantization.weights import quantize_matrix

        return quantize_matrix(weight, native_precision, optimized=optimized).to(device)
    layer = nn.Linear(weight.shape[1], weight.shape[0], bias=False, device="meta")
    layer.weight = nn.Parameter(weight.to(device=device, dtype=dtype), requires_grad=False)
    return layer


def load_hf_decoder(path, config, *, device, dtype, optimized=False, native_precision=None):
    if native_precision is not None and native_precision not in ("fp8", "int8"):
        raise ValueError("native_precision must be 'fp8', 'int8', or None")
    checkpoint = Checkpoint(path)
    with torch.device("meta"):
        decoder = CaptionDecoder(config, optimized=optimized)
    from .decoder import Rotary

    decoder.rotary_emb = Rotary(config).to(device)
    prefix = checkpoint.resolve("model.language_model.embed_tokens.weight", "model.embed_tokens.weight").rsplit(".", 2)[0]
    embedding = checkpoint_matrix(checkpoint, prefix + ".embed_tokens", device=device, dtype=dtype, embedding=True)
    decoder.embed_tokens = nn.Embedding.from_pretrained(embedding.weight, freeze=True) if isinstance(embedding, nn.Linear) else embedding
    for i, layer in enumerate(decoder.layers):
        for name, child in list(layer.named_modules()):
            if isinstance(child, nn.Linear):
                parent, _, attr = name.rpartition(".")
                setattr(layer.get_submodule(parent), attr, checkpoint_matrix(
                    checkpoint, f"{prefix}.layers.{i}.{name}", device=device, dtype=dtype,
                    optimized=optimized, native_precision=native_precision))
            elif hasattr(child, "eps") and hasattr(child, "weight"):
                child.weight = nn.Parameter(checkpoint.tensor(f"{prefix}.layers.{i}.{name}.weight").to(device=device, dtype=dtype), requires_grad=False)
    decoder.norm.weight = nn.Parameter(checkpoint.tensor(prefix + ".norm.weight").to(device=device, dtype=dtype), requires_grad=False)
    if "lm_head.weight" in checkpoint.files or "lm_head.weight_packed" in checkpoint.files:
        output = checkpoint_matrix(checkpoint, "lm_head", device=device, dtype=dtype,
                                   optimized=optimized, native_precision=native_precision)
    else:
        output = embedding
    return decoder.eval(), output.eval()


def load_hf_vision(path, config, *, device, dtype):
    from transformers.models.qwen3_vl.modeling_qwen3_vl import (
        Qwen3VLVisionModel,
    )

    from .vision_rotary import load_rotary

    checkpoint = Checkpoint(path)
    config._attn_implementation = "sdpa"
    with torch.device("meta"):
        vision = Qwen3VLVisionModel(config)
    vision.rotary_pos_emb = load_rotary(config, device=device)
    prefix = "model.visual." if "model.visual.patch_embed.proj.weight" in checkpoint.files else "visual."
    state = {}
    for key, expected in vision.state_dict().items():
        tensor = checkpoint.tensor(prefix + key)
        if tensor.shape != expected.shape or tensor.element_size() < 2:
            raise ValueError("The prototype requires an unquantized vision tower")
        state[key] = tensor.to(device=device, dtype=dtype)
    vision.load_state_dict(state, strict=True, assign=True)
    return vision.eval()
