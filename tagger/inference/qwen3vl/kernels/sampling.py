"""Batched greedy selection with the reference FP32 repetition penalty."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit(do_not_specialize=["EPOCH"])
def _mark(IDS, MARKS, WIDTH: tl.constexpr, COUNT: tl.constexpr,
          EPOCH, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    cols = tl.arange(0, BLOCK)
    ids = tl.load(IDS + row * COUNT + cols, cols < COUNT, -1)
    tl.store(MARKS + row * WIDTH + ids, EPOCH, (cols < COUNT) & (ids >= 0))


@triton.jit(do_not_specialize=["EPOCH"])
def _candidates(LOGITS, MARKS, PENALTIES, ACTIVE, VALUES, INDICES,
                WIDTH: tl.constexpr, STRIDE: tl.constexpr,
                TILES: tl.constexpr, EPOCH, BLOCK: tl.constexpr):
    row, tile = tl.program_id(0), tl.program_id(1)
    ids = tile * BLOCK + tl.arange(0, BLOCK)
    scores = tl.load(LOGITS + row * STRIDE + ids, ids < WIDTH, -float("inf")).to(tl.float32)
    marks = tl.load(MARKS + row * WIDTH + ids, ids < WIDTH, 0)
    reciprocal = tl.load(PENALTIES + row * 2)
    penalty = tl.load(PENALTIES + row * 2 + 1)
    # PyTorch scalar division uses the FP32 reciprocal of the Python scalar.
    penalized = tl.where(scores > 0, scores * reciprocal, scores * penalty)
    scores = tl.where(marks == EPOCH, penalized, scores)
    is_nan = scores != scores  # noqa: PLR0124 - IEEE NaN comparison in Triton
    nan = tl.sum((is_nan & (ids < WIDTH)).to(tl.int32), 0) > 0
    maximum = tl.max(scores, 0)
    match = tl.where(nan, is_nan, scores == maximum)
    index = tl.min(tl.where(match & (ids < WIDTH), ids, 2147483647), 0)
    enabled = tl.load(ACTIVE + row)
    offset = row * TILES + tile
    tl.store(VALUES + offset, tl.where(nan, float("nan"), maximum))
    tl.store(INDICES + offset, tl.where(enabled, index, 0))


@triton.jit
def _select(VALUES, INDICES, ACTIVE, SELECTED, TILES: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    tiles = tl.arange(0, BLOCK)
    values = tl.load(VALUES + row * TILES + tiles, tiles < TILES, -float("inf"))
    indices = tl.load(INDICES + row * TILES + tiles, tiles < TILES, 2147483647)
    is_nan = values != values  # noqa: PLR0124 - IEEE NaN comparison in Triton
    nan = tl.sum((is_nan & (tiles < TILES)).to(tl.int32), 0) > 0
    maximum = tl.max(values, 0)
    match = tl.where(nan, is_nan, values == maximum)
    token = tl.min(tl.where(match, indices, 2147483647), 0)
    enabled = tl.load(ACTIVE + row)
    tl.store(SELECTED + row, tl.where(enabled, token, 0))


class GreedySampler:
    def __init__(self, logits):
        batch, width = logits.shape
        self.marks = torch.zeros(batch, width, device=logits.device, dtype=torch.int32)
        self.epoch = 0

    def __call__(self, logits, histories, options, active):
        batch, width = logits.shape
        tails = [history[-option.repeat_last_n:] if enabled and option.repeat_penalty != 1
                 and option.repeat_last_n else []
                 for history, option, enabled in zip(histories, options, active)]
        count = max(map(len, tails), default=0)
        ids = torch.tensor([tail + [-1] * (count - len(tail)) for tail in tails],
                           device=logits.device, dtype=torch.long) if count else None
        penalties = torch.tensor([[1.0 / option.repeat_penalty, option.repeat_penalty] for option in options],
                                 device=logits.device, dtype=torch.float32)
        enabled = torch.tensor(active, device=logits.device, dtype=torch.bool)
        tiles = triton.cdiv(width, 1024)
        values = torch.empty(batch, tiles, device=logits.device, dtype=torch.float32)
        indices = torch.empty(batch, tiles, device=logits.device, dtype=torch.int32)
        selected = torch.empty(batch, device=logits.device, dtype=torch.long)
        self.epoch += 1
        if self.epoch == 2147483647:
            self.marks.zero_()
            self.epoch = 1
        with torch.cuda.device(logits.device):
            if count:
                _mark[(batch,)](ids, self.marks, width, count, self.epoch,
                                triton.next_power_of_2(count), num_warps=4)
            _candidates[(batch, tiles)](
                logits, self.marks, penalties, enabled, values, indices,
                width, logits.stride(0), tiles, self.epoch, 1024,
                num_warps=4, enable_fp_fusion=False)
            _select[(batch,)](values, indices, enabled, selected,
                             tiles, triton.next_power_of_2(tiles), num_warps=4)
        return selected
