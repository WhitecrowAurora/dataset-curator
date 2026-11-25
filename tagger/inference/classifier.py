"""分类模型的单侧产出，沿用客户端的批处理、产物与断点机制。"""
from __future__ import annotations

import time


def finish_tags(client, data, future, tags, cap):
    """沿用数量补救与接受策略，重筛已有分数而不重新执行前向。"""
    output = client.output
    rescored = 0
    if len(tags) < output.tag_min_count:
        tags = client._rescore_at(data, future, output.tag_fallback_threshold,
                                  with_prob=True) or tags
        rescored = 1
    elif (len(tags) > cap and output.tag_retry_threshold
          and len(tags) > max(cap, output.tag_overflow_count or 0)):
        tags = client._rescore_at(data, future, output.tag_retry_threshold,
                                  with_prob=True) or tags
        rescored = 1
    tags = tags[:cap]
    low = len(tags) < output.tag_min_count
    if low and not (output.tag_min_accept and len(tags) >= output.tag_min_accept):
        raise ValueError(f"tag 过少（{len(tags)} < {output.tag_min_count}），"
                         f"后端 {client.tag_backend.name} 的阈值偏低")
    return tags, rescored, low


def classify(client, task, data, started, cache_key):
    from ..client import Outcome, TAG_FLAG_LOW_COUNT

    try:
        future = client._tag_executor().submit(data)
        prediction = future.result()
        tags, rescored, low = finish_tags(client, data, future, prediction.tags,
                                          client._tag_cap(False))
        flags = [TAG_FLAG_LOW_COUNT] if low else []
        payload = client._tag_payload(task, tags, 0.0, 0, 1,
                                      rating=prediction.rating, flags=flags,
                                      tag_rescored=rescored)
        if client.cache is not None and cache_key is not None:
            client.cache.put(cache_key, {"temperature": 0.0, "attempts": 1,
                                        "tokens": 0, "tag": {"tags": tags, "flags": flags,
                                                             "rescored": rescored,
                                                             "rating": prediction.rating}})
        if getattr(client, "hair_batch", None) is not None:
            payload["_hair_scores"] = prediction.hair_scores
            client.hair_batch.capture(data, payload, task.image_id, task.file, media=task.media)
        return Outcome(task.image_id, True, tag=payload, attempts=1,
                       wall_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:
        return Outcome(task.image_id, False,
                       error=f"分类推理失败: {type(exc).__name__}: {exc}",
                       wall_ms=int((time.perf_counter() - started) * 1000))
