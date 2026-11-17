"""固定 HF 版本下载；重定向不转发访问凭据。"""
import hashlib
import json
import re
from http.client import IncompleteRead
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import retry


class Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            new.remove_header("Authorization")
        return new


def open_url(url, token, extra_headers=None):
    headers = {"User-Agent": "lulynx-source-catalog"}
    if token:
        headers["Authorization"] = "Bearer " + token
    headers.update(extra_headers or {})
    return build_opener(Redirect()).open(Request(url, headers=headers), timeout=60)


def metadata(repo, kind, revision, token, canceled=None, notify=None):
    plural = "datasets" if kind == "dataset" else "models"
    base = "https://huggingface.co/api/" + plural + "/" + quote(repo, safe="/")
    def read(url):
        def fetch():
            with open_url(url, token) as response:
                return json.load(response), response.headers.get("Link", "")
        return retry.call(fetch, canceled, notify)
    version = read(base + "/revision/" + quote(revision, safe=""))[0]["sha"]
    tree_url = base + "/tree/" + version + "?recursive=true"
    tree = {}
    while tree_url:
        entries, links = read(tree_url)
        tree.update((r["path"], r) for r in entries if r.get("type") == "file")
        tree_url = next((p.split(";", 1)[0].strip().strip("<>") for p in links.split(",") if 'rel="next"' in p), None)
    return version, tree


def download(repo, kind, revision, entry, target, token, progress, canceled):
    if hasattr(progress, 'package'):
        progress.package(entry['path'])
    prefix = "datasets/" if kind == "dataset" else ""
    url = "https://huggingface.co/" + prefix + quote(repo, safe="/") + "/resolve/" + revision + "/" + quote(entry["path"], safe="/")
    digest, size = hashlib.sha256(), 0
    with target.open("wb") as output:
        for attempt in range(1, retry.ATTEMPTS + 1):
            retry.check(canceled)
            try:
                with open_url(url, token, {"Range": f"bytes={size}-"} if size else None) as response:
                    if size:
                        if response.status == 200:
                            output.seek(0)
                            output.truncate()
                            digest, size = hashlib.sha256(), 0
                        elif response.status != 206 or not re.fullmatch(
                                rf"bytes {size}-\d+/{entry['size']}", response.headers.get("Content-Range", "")):
                            raise ValueError("仓库断点响应与文件范围不一致")
                    while True:
                        retry.check(canceled)
                        block = response.read(1 << 20)
                        if not block:
                            break
                        output.write(block)
                        digest.update(block)
                        size += len(block)
                        progress(size, entry["size"])
                    if size < entry["size"]:
                        raise IncompleteRead(b"", entry["size"] - size)
                break
            except Exception as exc:
                retry.pause(exc, attempt, canceled, getattr(progress, "retry", None))
    expected = (entry.get("lfs") or {}).get("oid")
    if size != entry["size"] or expected and digest.hexdigest() != expected:
        raise ValueError("仓库文件大小或 SHA256 不一致")
    return digest.hexdigest()
