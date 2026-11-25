"""复用现有 HF 下载链，只准备固定版本的 tokenizer 文件。"""
from __future__ import annotations

from .settings import REPO, REVISION, ROOT


def prepare(*, proxy="", base=""):
    from ..hf_source import download_shard, resolve_urls
    from ..storage.atomic import write_json
    from .tokenizer import TrainingText
    from .settings import TextEncoding

    directory = ROOT / "models/text/qwen3.5-0.8b"
    files = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja")
    for name in files:
        urls = ([f"{base.rstrip('/')}/{REPO}/resolve/{REVISION}/{name}"] if base else
                [url.replace("/datasets/", "/", 1) for url in resolve_urls(REPO, name, revision=REVISION)])
        for index, url in enumerate(urls):
            try:
                download_shard(REPO, name, directory, revision=REVISION, proxy=proxy,
                               timeout_s=30, max_attempts=1, verbose=False, conns=1, url=url + "?download=true")
                break
            except RuntimeError:
                if index == len(urls) - 1:
                    raise
    spec = TextEncoding()
    counter = TrainingText(spec)
    import hashlib
    write_json(directory / "source.json", {"repo": REPO, "revision": REVISION,
               "sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files}})
    return {"ready": True, "path": str(directory), "revision": REVISION,
            "example_tokens": counter.count("A blue-haired person in a red coat.")}
