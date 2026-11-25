"""配置目标、发布已校验分片和记录回传位置。"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .blobs import BlobStore


class Delivery:
    def __init__(self, data_dir, artifact_dir):
        self.config_path = Path(data_dir) / "delivery.json"
        self.db_path = Path(data_dir) / "delivery.sqlite3"
        self.default_dir = str(Path(artifact_dir) / "received")
        self.blobs = BlobStore(Path(artifact_dir) / "transfer")
        self.lock = threading.RLock()
        self.publish_lock = threading.RLock()
        self.next_cleanup = 0.0
        Path(self.default_dir).mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, repo TEXT,"
                      "package TEXT, user_id TEXT, device_id TEXT, lease_id TEXT,"
                      "sha256 TEXT, size INTEGER, local_path TEXT, hf_repo TEXT, hf_path TEXT,"
                      "received_at REAL DEFAULT (unixepoch()))")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def config(self, private=False):
        cfg = {"mode": "local", "local_dir": self.default_dir, "hf_token": "",
               "hf_repo": "", "hf_type": "dataset", "hf_prefix": ""}
        try:
            cfg.update(json.loads(self.config_path.read_text("utf8")))
        except (OSError, ValueError):
            pass
        if not private:
            cfg["has_token"] = bool(cfg.pop("hf_token", ""))
        return cfg

    def save(self, body):
        with self.lock:
            cfg = self.config(True)
            cfg.update({k: body[k] for k in cfg if k in body and k != "hf_token"})
            if body.get("hf_token"):
                cfg["hf_token"] = str(body["hf_token"]).strip()
            if cfg["mode"] not in ("local", "hf"):
                raise ValueError("储存方式必须为本地或 Hugging Face")
            if cfg["mode"] == "local":
                target = Path(str(cfg["local_dir"])).expanduser()
                if not target.is_absolute() or not target.is_dir():
                    raise ValueError("请选择存在的服务器绝对目录")
                cfg["local_dir"] = str(target.resolve())
                forbidden = (self.config_path.parent.resolve(), self.blobs.root.resolve())
                if any(target.resolve() == p or target.resolve() in p.parents
                       or p in target.resolve().parents for p in forbidden):
                    raise ValueError("产物目录不能包含调度数据或中转仓库")
            else:
                if not cfg["hf_token"] or cfg["hf_type"] not in ("dataset", "model"):
                    raise ValueError("请填写 HF token 并选择仓库类型")
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", cfg["hf_repo"]):
                    raise ValueError("请选择有效的 HF 仓库")
                prefix = str(cfg["hf_prefix"]).strip("/")
                if prefix:
                    self.blobs._safe_rel(prefix)
                cfg["hf_prefix"] = prefix
                self.hf(cfg["hf_token"]).repo_info(cfg["hf_repo"], repo_type=cfg["hf_type"])
            tmp = self.config_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf8") as f:
                json.dump(cfg, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_path)
        return self.config()

    @staticmethod
    def browse(path):
        base = Path(path or "/").expanduser().resolve()
        if not base.is_dir():
            raise ValueError("服务器目录不存在")
        dirs = []
        for p in base.iterdir():
            try:
                if p.is_dir():
                    dirs.append({"name": p.name, "path": str(p.resolve())})
            except OSError:
                continue
        return {"path": str(base), "parent": str(base.parent),
                "directories": sorted(dirs, key=lambda x: x["name"]) }

    @staticmethod
    def hf(token):
        from huggingface_hub import HfApi

        return HfApi(token=token)

    def repos(self, token):
        api = self.hf(token or self.config(True)["hf_token"])
        who = api.whoami()
        owners = [who["name"]] + [o["name"] for o in who.get("orgs", [])]
        rows = []
        for owner in owners:
            for kind, fn in (("dataset", api.list_datasets), ("model", api.list_models)):
                rows.extend({"id": r.id, "type": kind, "private": bool(r.private)}
                            for r in fn(author=owner))
        return {"repositories": sorted(rows, key=lambda r: (r["type"], r["id"]))}

    @staticmethod
    def validate_filename(lease, filename):
        if Path(filename).name != filename or (not filename.endswith(".jsonl") and filename != "proof.json"):
            raise ValueError("调度回传只接收本包 JSONL 分片")
        stem = Path(lease["pkg"]).stem
        stem = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 or ch.isspace()
                       else ch for ch in stem).strip("._ ")[:60].rstrip("._ ") or "multi"
        if not (filename == "proof.json" or filename == f"artifacts-{stem}.jsonl" or filename.startswith(f"artifacts-{stem}-")):
            raise ValueError("产物文件名与当前租约分片不匹配")

    def publish(self, lease, device, body, publication=nullcontext):
        filename = str(body.get("name") or "")
        self.validate_filename(lease, filename)
        # 用户/仓库路径用哈希隔离；文件名、租约和设备另有路径校验。
        namespace = hashlib.sha256(lease["repo"].encode()).hexdigest()[:16]
        rel = f"{namespace}/{device['device_id']}/{lease['lease_id']}/{filename}"
        self.blobs._safe_rel(rel)
        with self.publish_lock:
            done = self.blobs.commit(rel, body.get("chunks") or [],
                                     total_bytes=int(body.get("bytes") or 0))
            source = self.blobs.files / rel
            # 验证结构后再发布，失败保留块供补传。
            count = 0
            with source.open(encoding="utf8") as f:
                if filename == "proof.json":
                    proof = json.load(f)
                    if not isinstance(proof, dict) or not isinstance(proof.get("expected_ids"), list):
                        raise ValueError("检查报告缺少图片清单")
                    count = 1
                for line in f:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not row.get("image_id"):
                        raise ValueError("产物有缺少 image_id 的记录")
                    count += 1
            if not count:
                raise ValueError("产物为空")
            with self.lock:
                cfg = self.config(True)
            local_path, hf_repo, hf_path = "", "", ""
            if cfg["mode"] == "local":
                dest = Path(cfg["local_dir"]) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.resolve().is_relative_to(Path(cfg["local_dir"]).resolve()) is False:
                    raise ValueError("保存位置超出所选目录")
                scratch = dest.with_name(dest.name + ".part")
                with source.open("rb") as r, scratch.open("wb") as w:
                    shutil.copyfileobj(r, w, 1 << 20)
                    w.flush()
                    os.fsync(w.fileno())
                with publication():
                    os.replace(scratch, dest)
                local_path = str(dest)
            else:
                hf_repo = cfg["hf_repo"]
                hf_path = "/".join(x for x in (cfg["hf_prefix"], rel) if x)
                with publication():
                    pass
                self.hf(cfg["hf_token"]).upload_file(
                    path_or_fileobj=str(source), path_in_repo=hf_path, repo_id=hf_repo,
                    repo_type=cfg["hf_type"], commit_message=f"Receive {lease['pkg']}")
                dest = Path(self.default_dir) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
                local_path = str(dest)
            with publication(), self.connect() as c:
                c.execute("INSERT OR REPLACE INTO files(name,repo,package,user_id,device_id,"
                          "lease_id,sha256,size,local_path,hf_repo,hf_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (rel, lease["repo"], lease["pkg"], device["user_id"], device["device_id"],
                           lease["lease_id"], done["sha256"], done["bytes"],local_path,hf_repo,hf_path))
            # 已经发布；清掉中转成品，内容块保留给断点续传。不会触碰最终文件。
            source.unlink(missing_ok=True)
            source.with_name(source.name + ".chunks").unlink(missing_ok=True)
            # 中转缓存只服务续传；最终产物不参与清理。成功发布后每小时清一次
            # 超过一天仍未被复用的孤立块，避免在线储存仍永久占用服务器磁盘。
            if time.monotonic() >= self.next_cleanup:
                self.next_cleanup = time.monotonic() + 3600
                try:
                    self.blobs.gc(dry_run=False, linger_hours=24)
                except OSError:
                    pass
            done["name"] = filename
            done["verify"] = {"ok": True, "rows": count}
            return done

    def files(self, repo, package):
        with self.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM files WHERE repo=? AND package=? ORDER BY received_at", (repo,package))]
