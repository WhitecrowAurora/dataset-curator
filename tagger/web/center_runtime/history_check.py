"""中心下发检查后的本地逐图对账和回传，不加载模型。"""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from ...blob_client import BlobClient
from ...storage.atomic import write_json
from ...verification.history import reconcile


from ...verification.source import source_ids


def execute(history, command, session, cancel):
    task = command["config"]
    row = history.inventory.local(task["candidate_id"])
    if row["repo"] != task["repo"] or row["package"] != task["package"]:
        raise ValueError("中心检查任务与本地来源关联不匹配")
    source = Path(row["source"])
    source_before = (source.stat().st_size, source.stat().st_mtime_ns)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while block := stream.read(1 << 20):
            if cancel.is_set():
                raise ValueError("历史分片检查已取消")
            digest.update(block)
    if source.stat().st_size != task["size"] or digest.hexdigest() != task["sha256"]:
        raise ValueError("本地原始分片与中心的大小／SHA256 不一致，请核对源仓库")
    expected = source_ids(source)
    local = next(r for r in history.inventory.scan() if r["id"] == row["id"])
    files = local["files"]
    ledgers = [str(Path(row["out_dir"]) / name) for name in ("failures.jsonl", "skipped.jsonl", "excused.jsonl")
               if (Path(row["out_dir"]) / name).is_file()]
    tracked = files + ledgers
    before = [(p, Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p in tracked]
    from ...verification.local import rows as local_rows
    rows = list(local_rows([Path(p) for p in files], expected))
    # 老产物的剔除原因可能只在本机账本中，归并进本次验收产物。
    from ...fox_worker import _skip_rows
    known = set(expected)
    for skipped in _skip_rows(Path(row["out_dir"]), row["package"], per_part=True):
        if skipped["image_id"] in known:
            rows.append({'image_id': skipped['image_id'], 'outcomes': {part: {'status': 'excluded', 'reason': skipped['reason']}
                         for part in skipped['parts'] if part in ('tag','nl')}})
    from ...verification.local import failure_rows
    rows.extend(failure_rows(Path(row['out_dir']) / 'failures.jsonl', row['package'], expected))
    normalized, report = reconcile(expected, rows)
    after = [(p, Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p in tracked]
    if before != after:
        raise ValueError("检查期间产物被修改，请等本地打标完成后重试")
    if source_before != (source.stat().st_size, source.stat().st_mtime_ns):
        raise ValueError("检查期间源分片被修改，请重试")
    report.update(source_sha256=digest.hexdigest(), source_bytes=source.stat().st_size)
    history.report(row["id"], report, "checked", local['fingerprint'])
    if cancel.is_set() or session.closed:
        raise ValueError("已退出托管，取消历史产物回传")
    folder = history.center.dir / "history-transfer" / task["check_id"]
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "artifacts-history.jsonl"
    output.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (rows or normalized)), encoding="utf-8")
    proof = {"expected_ids": expected, "source_sha256": digest.hexdigest(),
             "source_bytes": source.stat().st_size, "report": report}
    write_json(folder / "proof.json", proof)
    headers = {"X-Fox-User": quote(session.identity["user_id"], safe=""),
               "X-Fox-Device": session.identity["device_id"], "X-Fox-Secret": session.identity["secret"],
               "X-Fox-History": task["check_id"], "X-Fox-Session": session.id}
    client = BlobClient(session.identity["server"], headers=headers, timeout=300)
    history.phase("uploading", "回传历史产物与逐图检查报告")
    for path in (output, folder / "proof.json"):
        if cancel.is_set():
            raise ValueError("历史产物回传已取消")
        result = client.upload(path)
        if not result.ok:
            raise ValueError(result.error)
    if cancel.is_set() or session.closed:
        raise ValueError("已退出托管，取消历史产物登记")
    from ...fox_worker import api
    result = api(session.identity, "/api/v1/history/finish", {"check_id": task["check_id"], "session_id": session.id}, timeout=60, retries=2)
    final_report = result.get('report') or report
    history.report(row["id"], {**final_report, 'check_id': task['check_id'], 'session_id': session.id,
                             "registered_parts": result["registered_parts"]},
                   "verifying" if result.get('verification_pending') else
                   result.get('status') or ("registered" if final_report.get('ok') else "incomplete"), local['fingerprint'])
    for path in (output, folder / "proof.json"):
        path.unlink(missing_ok=True)
    folder.rmdir()
