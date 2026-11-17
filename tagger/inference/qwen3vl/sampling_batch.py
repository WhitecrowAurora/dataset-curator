"""Batch probability preparation while keeping each request's scan and RNG."""
from __future__ import annotations

import torch


def sample_batch(logits, histories, options, generators, active, prepare, graph=None):
    scores = prepare(logits, histories, options, active)
    if graph is not None:
        graph(scores)
        return graph.sample(generators, active)
    for i, (option, enabled) in enumerate(zip(options, active)):
        if enabled and option.temperature > 0 and option.top_k and option.top_k < scores.shape[1]:
            scores[i].masked_fill_(scores[i] < scores[i].topk(option.top_k).values[-1], -torch.inf)
    probs = scores.softmax(-1)
    for i, (option, enabled) in enumerate(zip(options, active)):
        if enabled and option.min_p:
            probs[i].masked_fill_(probs[i] < probs[i].max() * option.min_p, 0)
    if any(enabled and option.temperature > 0 and option.top_p < 1
           for option, enabled in zip(options, active)):
        values, order = probs.sort(descending=True)
    result = []
    for i, (option, generator, enabled) in enumerate(zip(options, generators, active)):
        if not enabled:
            result.append(logits.new_zeros((), dtype=torch.long))
        elif option.temperature == 0:
            result.append(scores[i].argmax())
        elif option.top_p < 1:
            row = values[i]
            row.masked_fill_(row.cumsum(-1) - row >= option.top_p * row.sum(), 0)
            result.append(order[i, torch.multinomial(row, 1, generator=generator)[0]])
        else:
            result.append(torch.multinomial(probs[i], 1, generator=generator)[0])
    return torch.stack(result)
