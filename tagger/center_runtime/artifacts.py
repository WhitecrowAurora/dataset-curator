"""按源隔离目录、下载已归并产物、生成逐图回传。"""
import hashlib
import json
import time
from pathlib import Path

import urllib.request

from ..artifacts import shard_stem
from ..verification.history import read_rows, reconcile
from ..storage.atomic import write_json


def directory(root, lease):
    key = hashlib.sha256((lease["repo"] + "\n" + lease["package"] + "\n" + str(lease.get("sha256") or "")).encode()).hexdigest()
    path = Path(root) / "sources" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def headers(cfg, lease):
    from urllib.parse import quote
    return {"X-Fox-User": quote(str(cfg["user_id"]), safe=""), "X-Fox-Device": cfg["device_id"],
            "X-Fox-Secret": cfg.get("secret") or "", "X-Fox-Lease": lease["lease_id"]}


def request(cfg, lease, action, body=None, timeout=120):
    url = cfg["server"].rstrip("/") + "/api/blob/" + action
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers(cfg, lease), 'Content-Type': 'application/json'})
    # 回传身份不能经重定向发送到其它主机。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None
    with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
        return json.load(response)


def resume(cfg, lease, output, source):
    if not lease.get("resume_supported"):
        return
    data = request(cfg, lease, "resume")
    if data.get("source_sha256") != lease.get("sha256"):
        raise ValueError("续作产物源分片不匹配")
    rows = data.get("rows", [])
    decision_path = output / '.center-resolutions.json'
    previous = json.loads(decision_path.read_text(encoding='utf-8')) if decision_path.is_file() else []
    prior = {(r['image_id'], r['part'], r['updated_at']): r for r in previous}
    decisions = data.get('resolutions', [])
    received_at = time.time()
    for decision in decisions:
        saved = prior.get((decision['image_id'], decision['part'], decision['updated_at']), {})
        decision['received_at'] = saved.get('received_at', received_at)
    write_json(decision_path, decisions)
    if not rows:
        write_json(output / '.center-resume.json', {'tag': [], 'nl': []})
        return
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    if hashlib.sha256(encoded.encode()).hexdigest() != data.get("sha256"):
        raise ValueError("续作产物 SHA256 不匹配")
    from ..verification.source import source_ids
    normalized, report = reconcile(source_ids(source), rows)
    if report["conflicts"]:
        raise ValueError("续作产物有内容冲突")
    assigned = lease.get('assigned_parts') or lease.get('missing_parts') or ['tag', 'nl']
    if any(': ' + part + ' 内容冲突' in problem for part in assigned
           for problem in (data.get('report') or {}).get('conflicts', [])):
        raise ValueError('中心产物有内容冲突，请管理员处理')
    # 两侧分别保存，单侧继续时只跳过本侧已完成图片。
    stem = shard_stem(lease["package"])
    for part in ("tag", "nl"):
        path = output / f"artifacts-{stem}-resume-{part}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for row in normalized:
                item = {"image_id": row["image_id"], part: row[part]}
                outcome = row["outcomes"].get(part, {})
                if outcome.get("status") == "excluded":
                    item["outcomes"] = {part: outcome}
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    done = {part: [r['image_id'] for r in normalized if r[part] or
                  r['outcomes'].get(part, {}).get('status') == 'excluded'] for part in ('tag', 'nl')}
    write_json(output / '.center-resume.json', done)


def prepare(lease, output):
    from ..verification.source import source_ids
    from ..fox_worker import _skip_rows
    source = output / lease["package"]
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    if digest.hexdigest() != lease.get("sha256") or source.stat().st_size != lease.get("size"):
        raise ValueError("原始分片与租约的 SHA256／大小不匹配")
    expected = source_ids(source)
    known = set(expected)
    stem = shard_stem(lease["package"])
    from ..verification.local import paths, rows as local_rows
    rows = list(local_rows(paths(output, lease['package'], isolated=True), expected))
    rows.extend({'image_id': r['image_id'], 'ts': r.get('ts', 0), 'outcomes': {p: {'status': 'excluded', 'reason': r['reason']}
                 for p in r['parts'] if p in ('tag','nl')}} for r in _skip_rows(output, lease['package'], per_part=True) if r['image_id'] in known)
    from ..verification.local import failure_rows
    rows.extend(failure_rows(output / 'failures.jsonl', lease['package'], expected))
    # 管理员决议优先于该设备旧产物；重做只接受决议之后新生成的记录。
    resolution_file = output / '.center-resolutions.json'
    resolutions = json.loads(resolution_file.read_text(encoding='utf-8')) if resolution_file.is_file() else []
    decisions = {(r['image_id'], r['part']): r for r in resolutions}
    for index, row in enumerate(rows):
        for part in ('tag', 'nl'):
            decision = decisions.get((row['image_id'], part))
            if decision and (decision['state'] == 'resolved' or float(row.get('ts') or 0) <= decision.get('received_at', decision['updated_at'])):
                row = dict(row)
                row[part] = decision['value'] if decision['state'] == 'resolved' else ''
                row['outcomes'] = {k: v for k, v in (row.get('outcomes') or {}).items() if k != part}
        rows[index] = row
    normalized, report = reconcile(expected, rows)
    # 固定文件完整覆盖本次快照；原业务产物保留。
    path = output / f"artifacts-{stem}-delivery.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (rows or normalized)), encoding="utf-8")
    proof = output / "proof.json"
    write_json(proof, {"source_sha256": digest.hexdigest(), "source_bytes": source.stat().st_size, "expected_ids": expected})
    return [path, proof]
