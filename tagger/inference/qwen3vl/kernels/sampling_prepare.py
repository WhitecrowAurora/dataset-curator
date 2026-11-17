"""Prepare sampled logits with the reference FP32 scalar operations."""
from __future__ import annotations

import torch
import triton
import triton.language as tl

from .sampling import _mark


@triton.jit(do_not_specialize=["EPOCH"])
def _prepare(LOGITS, MARKS, FACTORS, SCORES, WIDTH: tl.constexpr,
             STRIDE: tl.constexpr, EPOCH, BLOCK: tl.constexpr):
    row, tile = tl.program_id(0), tl.program_id(1)
    ids = tile * BLOCK + tl.arange(0, BLOCK)
    value = tl.load(LOGITS + row * STRIDE + ids, ids < WIDTH, 0).to(tl.float32)
    mark = tl.load(MARKS + row * WIDTH + ids, ids < WIDTH, 0)
    reciprocal = tl.load(FACTORS + row * 3)
    penalty = tl.load(FACTORS + row * 3 + 1)
    temperature = tl.load(FACTORS + row * 3 + 2)
    penalized = tl.where(value > 0, value * reciprocal, value * penalty)
    value = tl.where(mark == EPOCH, penalized, value)
    tl.store(SCORES + row * WIDTH + ids, value * temperature, ids < WIDTH)


class SamplingPreparation:
    def __init__(self, logits):
        self.marks = torch.zeros(logits.shape, device=logits.device, dtype=torch.int32)
        self.epoch = 0

    def __call__(self, logits, histories, options, active):
        batch, width = logits.shape
        tails = [history[-option.repeat_last_n:] if enabled and option.repeat_penalty != 1
                 and option.repeat_last_n else []
                 for history, option, enabled in zip(histories, options, active)]
        count = max(map(len, tails), default=0)
        ids = torch.tensor([tail + [-1] * (count - len(tail)) for tail in tails],
                           device=logits.device, dtype=torch.long) if count else None
        factors = torch.tensor([[1.0 / option.repeat_penalty, option.repeat_penalty,
                                1.0 / option.temperature if enabled and option.temperature else 1.0]
                                for option, enabled in zip(options, active)],
                               device=logits.device, dtype=torch.float32)
        scores = torch.empty(logits.shape, device=logits.device, dtype=torch.float32)
        self.epoch += 1
        if self.epoch == 2147483647:
            self.marks.zero_()
            self.epoch = 1
        with torch.cuda.device(logits.device):
            if count:
                _mark[(batch,)](ids, self.marks, width, count, self.epoch,
                                triton.next_power_of_2(count), num_warps=4)
            _prepare[(batch, triton.cdiv(width, 1024))](
                logits, self.marks, factors, scores, width, logits.stride(0), self.epoch, 1024,
                num_warps=4, enable_fp_fusion=False)
        return scores
