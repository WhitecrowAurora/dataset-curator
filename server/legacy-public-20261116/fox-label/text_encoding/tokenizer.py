"""固定训练 tokenizer 的正文计数；不截断、不套用聊天模板。"""
import hashlib
from functools import lru_cache
from pathlib import Path

REPO = 'Qwen/Qwen3.5-0.8B'
REVISION = '2fc06364715b967f1860aea9cf38778875588b17'
SHA256 = '5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42'
ASSET = Path(__file__).parent / 'assets' / 'tokenizer.json'


@lru_cache(maxsize=1)
def counter():
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise ValueError('正文 token 计数依赖未安装，请安装 text_encoding/requirements.txt') from exc
    if not ASSET.is_file() or hashlib.sha256(ASSET.read_bytes()).hexdigest() != SHA256:
        raise ValueError('Qwen3.5-0.8B tokenizer 文件缺失或不完整，请重新部署计数文件')
    tokenizer = Tokenizer.from_file(str(ASSET))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


@lru_cache(maxsize=4096)
def count(text):
    return len(counter().encode(text, add_special_tokens=False).ids)


def status():
    result = dict(repo=REPO, revision=REVISION, sha256=SHA256, add_special_tokens=False,
                  chat_template=False, ready=False)
    try:
        counter()
        result['ready'] = True
    except (ValueError, OSError) as exc:
        result['error'] = str(exc)
    return result
