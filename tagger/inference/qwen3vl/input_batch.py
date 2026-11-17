"""Combine independently prepared CPU images into the original left-padded batch."""
from __future__ import annotations

import torch


def collate(inputs, pad_token_id):
    width = max(value["input_ids"].shape[1] for value in inputs)
    ids = inputs[0]["input_ids"].new_full((len(inputs), width), pad_token_id)
    valid = inputs[0]["attention_mask"].new_zeros(len(inputs), width)
    for row, value in enumerate(inputs):
        count = value["input_ids"].shape[1]
        ids[row, -count:] = value["input_ids"][0]
        valid[row, -count:] = value["attention_mask"][0]
    return {"input_ids": ids, "attention_mask": valid,
            "image_grid_thw": torch.cat([value["image_grid_thw"] for value in inputs]),
            "pixel_values": torch.cat([value["pixel_values"] for value in inputs])}
