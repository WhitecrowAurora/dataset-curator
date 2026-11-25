"""已有 NL 的训练正文预算；使用中心固定的 Qwen tokenizer。"""
import re
from text_encoding.tokenizer import counter, REPO, REVISION, SHA256


def fit(text, prefix, value, tags=''):
    metadata = value.get('training_text') or {}
    spec = metadata.get('settings') or {}
    if spec.get('chat_template') or spec.get('revision', REVISION) != REVISION:
        raise ValueError('该产物使用自定义训练 tokenizer／聊天模板，请在原客户端补注入')
    template = spec.get('template', '{caption}')
    limit = min(int(spec.get('max_length', 512)), 512)
    tokenizer = counter()
    def count(caption):
        return len(tokenizer.encode(template.format(caption=caption, tag=tags),
                                    add_special_tokens=spec.get('add_special_tokens', True)).ids)
    if count(prefix) > limit:
        raise ValueError('画师前缀与文本模板已超过训练 token 上限')
    if count(text) > limit:
        cut = next((end for end in reversed([m.end() for m in re.finditer(r'[。！？；;]|[.!?](?:\s|$)', text)])
                    if end >= len(prefix) and count(text[:end].rstrip()) <= limit), None)
        if cut is not None:
            text = text[:cut].rstrip()
        else:
            lo, hi = len(prefix), len(text)
            while lo < hi:
                middle = (lo + hi + 1) // 2
                if count(text[:middle]) <= limit: lo = middle
                else: hi = middle - 1
            text = text[:lo].rstrip()
            while count(text) > limit: text = text[:-1].rstrip()
    value['training_text'] = {**metadata, 'tokens': count(text), 'limit': limit, 'target': 384,
                              'over_target': count(text) > 384,
                              'artist_reinject_tokenizer': dict(repo=REPO, revision=REVISION, sha256=SHA256)}
    return text
