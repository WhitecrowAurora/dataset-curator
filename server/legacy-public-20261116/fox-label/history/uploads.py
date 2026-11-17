"""复用分块通道接收历史检查产物，中心校验后再补登记。"""
import json
import os
import shutil
from pathlib import Path


def local_file(store, row):
    path = Path(row["local_path"]) if row["local_path"] else store.delivery.blobs.files / row["name"]
    if not path.is_file():
        raise ValueError("验收产物不存在，请重新回传")
    return path


def publish(service, lease, device, body, publication):
    filename = str(body.get("name") or "")
    if filename not in ("artifacts-history.jsonl", "proof.json"):
        raise ValueError("历史检查只接收规范化产物及 proof.json")
    rel = f"history/{device['device_id']}/{lease['lease_id']}/{filename}"
    with service.publish_lock:
        done = service.blobs.commit(rel, body.get("chunks") or [], total_bytes=int(body.get("bytes") or 0))
        source = service.blobs.files / rel
        if filename == "proof.json":
            proof = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(proof, dict) or not isinstance(proof.get("expected_ids"), list):
                raise ValueError("历史检查报告缺少图片清单")
        else:
            from verification.history import read_rows
            if not sum(1 for _ in read_rows([source])):
                raise ValueError("历史产物为空")
        cfg = service.config(True)
        dest = Path(service.default_dir) / rel
        if cfg["mode"] == "local":
            dest = Path(cfg["local_dir"]) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        scratch = dest.with_name(dest.name + ".part")
        shutil.copyfile(source, scratch)
        hf_repo, hf_path = "", ""
        with publication():
            os.replace(scratch, dest)
        if cfg["mode"] == "hf":
            hf_repo = cfg["hf_repo"]
            hf_path = "/".join(x for x in (cfg["hf_prefix"], rel) if x)
            service.hf(cfg["hf_token"]).upload_file(path_or_fileobj=str(dest), path_in_repo=hf_path,
                                                    repo_id=hf_repo, repo_type=cfg["hf_type"])
            with publication():
                pass
        with publication(), service.connect() as c:
            c.execute("INSERT OR REPLACE INTO files(name,repo,package,user_id,device_id,lease_id,sha256,size,local_path,hf_repo,hf_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (rel,lease["repo"],lease["pkg"],device["user_id"],device["device_id"],lease["lease_id"],done["sha256"],done["bytes"],str(dest),hf_repo,hf_path))
        source.unlink(missing_ok=True)
        source.with_name(source.name + ".chunks").unlink(missing_ok=True)
        done["verify"] = {"ok": True}
        return done
