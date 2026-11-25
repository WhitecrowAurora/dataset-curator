"""从 PixAI 原前向取少量属性分数，不重复执行模型。"""
from __future__ import annotations


def capture_scores(backend):
    from .batch import is_hair_tag
    pipe = backend._pipe
    tags = list(pipe.tags)
    wanted = [(i, tag) for i, tag in enumerate(tags) if is_hair_tag(tag)
              or tag in ("1girl", "1boy", "2girls", "2boys", "3girls", "3boys", "4girls", "4boys",
                         "5girls", "5boys", "6+girls", "6+boys")]

    def capture(_model, _args, logits):
        values = logits[:, [i for i, _ in wanted]].sigmoid().detach().float().cpu().tolist()
        backend._last_hair_scores.extend({tag: score for (_, tag), score in zip(wanted, row)} for row in values)

    return pipe.model.register_forward_hook(capture)
