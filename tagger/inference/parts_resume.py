"""正式双侧派工分别复用 TAG / NL，普通续跑保留原口径。"""
from copy import copy
from dataclasses import replace
from threading import Lock

from .part_artifacts import completed_part


def requested(value):
    parts = set(str(value or '').strip().lower().split(','))
    if parts in ({'tag', 'nl'}, {'both'}):
        return 'tag,nl'
    return next(iter(parts)) if parts in ({'tag'}, {'nl'}) else ''


class CompletedParts:
    def __init__(self, root, *, paths, resolutions=()):
        self.parts = {}
        self.views = {}
        self.lock = Lock()
        try:
            for part in ('tag', 'nl'):
                self.parts[part] = completed_part(root, part, paths=paths, resolutions=resolutions, include_excluded=True)
        except Exception:
            self.close()
            raise

    def __contains__(self, iid):
        return all(iid in index for index in self.parts.values())

    def close(self):
        for index in self.parts.values():
            index.close()

    def view(self, client, part):
        # 每槽每侧只准备一次，避免逐图重读提示词文件和计算缓存指纹。
        key = (id(client), part)
        with self.lock:
            if key not in self.views:
                self.views[key] = _view(client, part)
            return self.views[key]

    def process(self, client, task, data, reader, *, force=False):
        available = {} if force else {p: index[task.image_id][0] for p, index in self.parts.items() if task.image_id in index}
        if not available:
            return client.process(task, data, reader)
        part = 'nl' if 'tag' in available else 'tag'
        view = self.view(client, part)
        if part == 'nl':
            task = replace(task, training_tags=tuple(available['tag'].get('tags', [])))
        return view.process(task, data, reader)


def _view(client, part):
    view = copy(client)
    view.output = replace(client.output, mode=part)
    view.need_tags = part == 'tag'
    if part == 'nl':
        view.hair_batch = None
    # 默认提示词随输出侧收窄，自定义提示词保持用户原文。
    from ..client import _prompt_for
    for field, custom, zh in (('prompt', '_prompt_is_custom', False), ('prompt_zh', '_prompt_zh_is_custom', True)):
        if hasattr(client, field) and not getattr(client, custom, False):
            setattr(view, field, _prompt_for(view.output, zh=zh))
    for field, zh in (('_dense_prompt', False), ('_dense_prompt_zh', True)):
        if getattr(client, field, ''):
            setattr(view, field, _prompt_for(view.output, zh=zh,
                extra_rules=view.output.dense_retry_rules, tag_hi=view.output.dense_retry_tag_max))
    if getattr(client, 'cache', None) is not None:
        view.cache_prefix = view._cache_fingerprint()
    if hasattr(client, '_tag_executor'):
        view._tag_executor = client._tag_executor
    return view
