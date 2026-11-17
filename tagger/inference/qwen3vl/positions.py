"""Single-image MRoPE positions, including left padding and decode offsets."""
from __future__ import annotations

import torch


def image_positions(input_ids, valid, grid_thw, image_token_id: int, merge_size: int):
    positions = torch.zeros(3, *input_ids.shape, dtype=torch.long, device=input_ids.device)
    deltas = []
    for row in range(input_ids.shape[0]):
        indices = valid[row].nonzero(as_tuple=True)[0]
        tokens = input_ids[row, indices]
        image_indices = (tokens == image_token_id).nonzero(as_tuple=True)[0]
        start = int(image_indices[0])
        t, h, w = (int(v) for v in grid_thw[row])
        h, w = h // merge_size, w // merge_size
        count = t * h * w
        if image_indices.numel() != count or not torch.equal(image_indices, torch.arange(start, start + count, device=tokens.device)):
            raise ValueError("Expected exactly one contiguous image token block per caption")
        before = torch.arange(start, device=tokens.device).expand(3, -1)
        axes = torch.meshgrid(torch.arange(t, device=tokens.device), torch.arange(h, device=tokens.device),
                              torch.arange(w, device=tokens.device), indexing="ij")
        image = torch.stack(axes).reshape(3, -1) + start
        next_position = start + max(t, h, w)
        after = torch.arange(tokens.numel() - start - count, device=tokens.device).expand(3, -1) + next_position
        positions[:, row, indices] = torch.cat((before, image, after), dim=1)
        deltas.append(next_position + after.shape[1] - tokens.numel())
    return positions, torch.tensor(deltas, device=input_ids.device, dtype=torch.long)

