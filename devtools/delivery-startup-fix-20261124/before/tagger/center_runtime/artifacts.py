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
    result = {"X-Fox-User": quote(str(cfg["user_id"]), safe=""), "X-Fox-Device": cfg["device_id"],
              "X-Fox-Secret": cfg.get("secret") or ""}
    if lease.get('check_id'):
        result.update({'X-Fox-History': lease['check_id'], 'X-Fox-Session': lease['session_id']})
    else:
        result['X-Fox-Lease'] = lease['lease_id']
    return result


def request(cfg, lease, action, body=None, timeout=120):
    url = cfg["server"].rstrip("/") + "/api/blob/" + action
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers(cfg, lease), 'Content-Type': 'application/json'})
    from .network import opener
    with opener(url, cfg).open(req, timeout=timeout) as response:
        return json.load(response)


def resume(cfg, lease, output, source):
    if not lease.get("resume_supported"):
        return
    data = request(cfg, lease, "resume")
    if data.get("source_sha256") != lease.get("sha256"):
        raise ValueError("续作产物源分片不匹配")
    rows = data.get("rows", [])
    from .snapshot import NAME, state
    previous = state(output).get('resolutions', [])
    prior = {(r['image_id'], r['part'], r['updated_at']): r for r in previous}
    decisions = data.get('resolutions', [])
    received_at = time.time()
    for decision in decisions:
        saved = prior.get((decision['image_id'], decision['part'], decision['updated_at']), {})
        decision['received_at'] = saved.get('received_at', received_at)
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    if (rows or data.get('sha256')) and hashlib.sha256(encoded.encode()).hexdigest() != data.get("sha256"):
        raise ValueError("续作产物 SHA256 不匹配")
    from ..verification.source import source_ids
    normalized, report = reconcile(source_ids(source), rows)
    if report["conflicts"]:
        raise ValueError("续作产物有内容冲突")
    assigned = lease.get('assigned_parts') or lease.get('missing_parts') or ['tag', 'nl']
    if any(': ' + part + ' 内容冲突' in problem for part in assigned
           for problem in (data.get('report') or {}).get('conflicts', [])):
        raise ValueError('中心产物有内容冲突，请管理员处理')
    done = {part: [r['image_id'] for r in normalized if r[part] or
                  r['outcomes'].get(part, {}).get('status') == 'excluded'] for part in ('tag', 'nl')}
    # 校验成功后一次替换两侧正文、决议和完成清单；空快照也覆盖旧快照。
    write_json(output / NAME, {'rows': normalized, 'resolutions': decisions, 'done': done})


def inspect(lease, output, source=None):
    from ..verification.source import source_ids
    from ..fox_worker import _skip_rows
    source = Path(source) if source is not None else output / lease["package"]
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    if digest.hexdigest() != lease.get("sha256") or source.stat().st_size != lease.get("size"):
        raise ValueError("原始分片与租约的 SHA256／大小不匹配")
    expected = source_ids(source)
    known = set(expected)
    from ..verification.local import paths, rows as local_rows
    rows = list(local_rows(paths(output, lease['package'], isolated=True), expected))
    rows.extend({'image_id': r['image_id'], 'ts': r.get('ts', 0), 'outcomes': {p: {'status': 'excluded', 'reason': r['reason']}
                 for p in r['parts'] if p in ('tag','nl')}} for r in _skip_rows(output, lease['package'], per_part=True) if r['image_id'] in known)
    from ..verification.local import failure_rows
    rows.extend(failure_rows(output / 'failures.jsonl', lease['package'], expected))
    # 管理员决议优先于该设备旧产物；重做只接受决议之后新生成的记录。
    from .snapshot import state
    resolutions = state(output).get('resolutions', [])
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
    return source, digest.hexdigest(), expected, rows, normalized, report


def prepare(lease, output, source=None):
    source, digest, expected, rows, normalized, report = inspect(lease, output, source)
    stem = shard_stem(lease['package'])
    # 固定文件完整覆盖本次快照；原业务产物保留。
    path = output / f"artifacts-{stem}-delivery.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (rows or normalized)), encoding="utf-8")
    proof = output / "proof.json"
    write_json(proof, {"source_sha256": digest, "source_bytes": source.stat().st_size, "expected_ids": expected})
    return [path, proof]
