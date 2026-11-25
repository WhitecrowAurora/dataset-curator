"""历史分片候选、检查任务及幂等完成登记。"""
import json
import re
import secrets
import time
from contextlib import closing

from managed.contracts import ManagedError
from managed import store as managed


def ensure_schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS history_candidates (
      device_id TEXT NOT NULL, candidate_id TEXT NOT NULL, repo TEXT NOT NULL,
      package TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
      last_seen REAL NOT NULL, PRIMARY KEY(device_id,candidate_id));
    CREATE TABLE IF NOT EXISTS history_checks (
      check_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, user_id TEXT NOT NULL,
      session_id TEXT NOT NULL, candidate_id TEXT NOT NULL, repo TEXT NOT NULL,
      package TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_bytes INTEGER NOT NULL,
      status TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
      updated_at REAL NOT NULL);
    """)
    c.execute('CREATE INDEX IF NOT EXISTS idx_history_package ON history_checks(repo,package,source_sha256,status)')
    if 'annotation_json' not in {r[1] for r in c.execute('PRAGMA table_info(history_checks)')}:
        c.execute("ALTER TABLE history_checks ADD COLUMN annotation_json TEXT NOT NULL DEFAULT '{}'")
    if 'reinject_mode' not in {r[1] for r in c.execute('PRAGMA table_info(history_checks)')}:
        c.execute("ALTER TABLE history_checks ADD COLUMN reinject_mode TEXT NOT NULL DEFAULT ''")


def inventory(c, device_id, items, now):
    if not isinstance(items, list) or len(items) > 500:
        raise ManagedError("历史分片清单最多 500 项")
    c.execute("DELETE FROM history_candidates WHERE device_id=?", (device_id,))
    for item in items:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k] for k in ("id", "repo", "package", "fingerprint", "status")):
            raise ManagedError("历史分片清单格式不正确")
        c.execute("INSERT INTO history_candidates VALUES(?,?,?,?,?,?,?)",
                  (device_id, item["id"], item["repo"], item["package"], item["fingerprint"], item["status"], now))


def view(store):
    with closing(store.conn()) as c:
        candidates = [dict(r) for r in c.execute("SELECT h.*,d.user_id FROM history_candidates h JOIN devices d ON d.device_id=h.device_id ORDER BY h.last_seen DESC")]
        checks = [dict(r) for r in c.execute("SELECT * FROM history_checks ORDER BY created_at DESC LIMIT 500")]
    for item in checks:
        item["report"] = json.loads(item["report"])
    return {"ok": True, "candidates": candidates, "checks": checks}


def request(store, dispatch, body, actor, *, local=False):
    did, candidate = body.get("device_id"), body.get("candidate_id")
    reinject = body.get('reinject','')
    if reinject not in ('','missing','force'):
        raise ManagedError('画师补注入模式不正确')
    annotation = {}
    with closing(store.conn()) as c:
        row = c.execute("SELECT * FROM history_candidates WHERE device_id=? AND candidate_id=?", (did, candidate)).fetchone()
        current = managed.current(c, did)
        if local:
            from .client import authenticate
            authenticate(c, dispatch, body)
        if not row or row["status"] != "ready" or not current or current["released"]:
            raise ManagedError("设备没有可检查的本地历史分片", 409)
        state = json.loads(current["state"])
        if state.get("running") or (state.get("history_running") and not local):
            raise ManagedError("请先停止当前任务，等待检查结束", 409)
        if c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open'", (row["repo"], row["package"])).fetchone():
            raise ManagedError("源分片仍在执行或预锁中，请先释放任务", 409)
        from scheduling.leases import held
        if held(c,exclude_pending_device=did).get((row['repo'], row['package'])):
            raise ManagedError('源分片仍在等待中心核验', 409)
        if c.execute("SELECT 1 FROM history_checks WHERE device_id=? AND session_id=? AND status='pending'", (did, current["session_id"])).fetchone():
            raise ManagedError("设备已有待处理的历史检查", 409)
        package = c.execute("SELECT p.*,r.repo_type FROM packages p JOIN repos r ON r.repo=p.repo WHERE p.repo=? AND p.id=?", (row["repo"], row["package"])).fetchone()
        if not package:
            raise ManagedError("源分片尚未登记到中心，请先同步仓库", 409)
        sha = str(package["sha256"] or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ManagedError("源分片缺少 SHA256，先同步来源元数据", 409)
        device = dispatch._row(c, "SELECT * FROM devices WHERE device_id=?", (did,))
        workspace = dispatch.workspace_of(c, device)
        if workspace is not None and row["repo"] not in workspace["repos"]:
            raise ManagedError("历史分片不属于设备的工作区", 409)
        session = current["session_id"]
        user = current["user_id"]
        source_bytes = package["size"]
        if reinject:
            from assignments.annotation import for_package
            from annotation.store import config as artist_config
            original_repo_config = artist_config(c, row['repo'])
            annotation = for_package(c,dict(package),['tag','nl'])
    if reinject:
        from annotation.discovery import for_history
        original_annotation = dict(annotation)
        annotation = for_history(store, row['repo'], row['package'], annotation)
    check_id = "H" + secrets.token_hex(12)
    task = {"check_id": check_id, "candidate_id": candidate, "repo": row["repo"],
            "package": row["package"], "size": source_bytes, "sha256": sha}
    if reinject:
        task.update(annotation=annotation,reinject=reinject)
    now = time.time()
    with managed.transaction(store) as c:
        active = managed.current(c, did)
        if local:
            authenticate(c, dispatch, body)
            if c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND status IN ('queued','accepted')", (session,)).fetchone():
                raise ManagedError("中心已有待执行指令，请等当前操作结束后再核对", 409)
        if not active or active["session_id"] != session or active["released"]:
            raise ManagedError("托管会话已变化，请刷新", 409)
        if reinject:
            from assignments.annotation import for_package
            original = for_package(c, dict(package), ['tag','nl'])
            if original != original_annotation or artist_config(c, row['repo']) != original_repo_config:
                raise ManagedError('画师注入配置已变化，请刷新重试',409)
            source = c.execute('SELECT sha256,size FROM packages WHERE repo=? AND id=?',(row['repo'],row['package'])).fetchone()
            if not source or source['sha256'] != sha or source['size'] != source_bytes:
                raise ManagedError('源分片已变化，请重新核对',409)
        if c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open'", (row["repo"], row["package"])).fetchone():
            raise ManagedError("源分片已被派发，请等待任务结束", 409)
        from scheduling.leases import held
        if held(c,exclude_pending_device=did).get((row['repo'], row['package'])):
            raise ManagedError('源分片仍在等待中心核验', 409)
        if c.execute("SELECT 1 FROM history_checks WHERE device_id=? AND session_id=? AND status='pending'", (did, session)).fetchone():
            raise ManagedError("设备已有待处理的历史检查", 409)
        c.execute("INSERT INTO history_checks(check_id,device_id,user_id,session_id,candidate_id,repo,package,source_sha256,source_bytes,status,report,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'pending','{}',?,?)",
                  (check_id, did, user, session, candidate, row["repo"], row["package"], sha, source_bytes, now, now))
        c.execute('UPDATE history_checks SET annotation_json=?,reinject_mode=? WHERE check_id=?',(json.dumps(annotation),reinject,check_id))
        if local:
            store.event(c, 'history_requested', row['repo'], row['package'], user, 'check=' + check_id)
    if local:
        return {"ok": True, "check_id": check_id, "task": task}
    try:
        result = managed.enqueue(store, {"id": check_id, "device_id": did, "session_id": session,
                                        "action": "check", "config": task}, actor)
    except Exception:
        with managed.transaction(store) as c:
            c.execute("DELETE FROM history_checks WHERE check_id=?", (check_id,))
        raise
    return {"ok": True, "check_id": check_id, **result}


def identity(c, dispatch, user, did, secret, check_id, session):
    owner = c.execute("SELECT user_id FROM devices WHERE device_id=?", (did,)).fetchone()
    if not owner or owner["user_id"] != user:
        raise dispatch.AuthError("设备与用户不匹配")
    device = dispatch._require_device(c, user, did, secret)
    account = c.execute("SELECT status FROM users WHERE user_id=?", (user,)).fetchone()
    check = c.execute("SELECT * FROM history_checks WHERE check_id=?", (check_id,)).fetchone()
    current = managed.current(c, did)
    if (not account or account["status"] != "active" or device["status"] in ("blocked", "archived")
            or not check or check["device_id"] != did or check["user_id"] != user
            or check["status"] in ("failed", "canceled")
            or check["session_id"] != session or not current or current["session_id"] != session or current["released"]
            or not isinstance(secret, str) or not secrets.compare_digest(dispatch.hash_secret(secret), current["secret_hash"])):
        raise dispatch.AuthError("历史检查身份或托管会话不匹配")
    return dict(check), device


def finish(store, dispatch, body):
    from verification.history import read_rows, reconcile
    with managed.transaction(store) as c:
        check, device = identity(c, dispatch, body.get("user_id"), body.get("device_id"),
                                 body.get("secret"), body.get("check_id"), body.get("session_id"))
        if check["status"] in ("registered", "incomplete", "verifying"):
            return {"ok": True, "replay": True, "verification_pending": check["status"] == "verifying",
                    "registered_parts": json.loads(check["report"]).get("registered_parts", []),
                    'status': check['status'], 'report': json.loads(check['report'])}
        uploads = store.delivery.files(check["repo"], check["package"])
        files = {r["name"].rsplit("/", 1)[-1]: r for r in uploads if r["lease_id"] == check["check_id"]}
        if not {"artifacts-history.jsonl", "proof.json"} <= files.keys():
            raise ManagedError("历史产物和检查报告尚未回传完整", 409)
        from .uploads import local_file
        proof = json.loads(local_file(store, files["proof.json"]).read_text(encoding="utf-8"))
        if proof.get("source_sha256") != check["source_sha256"] or proof.get("source_bytes") != check["source_bytes"]:
            raise ManagedError("历史检查来源不匹配", 409)
        artifact_rows = list(read_rows([local_file(store, files["artifacts-history.jsonl"])]))
        _, report = reconcile(proof.get("expected_ids", []), artifact_rows)
        client = proof.get("report", {})
        if any(client.get(k) != report[k] for k in ("expected", "ids_sha256", "parts", "complete_parts")):
            raise ManagedError("客户端和中心逐图对账结果不一致", 409)
        from delivery.reconciliation import merge
        if check.get('reinject_mode'):
            from annotation.tables import for_task
            from annotation.maintenance import normalize
            from annotation.artist_reinject import rows
            from annotation.text import fit
            from annotation.review import require_review
            table = for_task(store,{**check,'pkg':check['package']},cached=True)
            if table is None:
                raise ManagedError('画师表缓存缺失，请先重新获取画师表再上报',409)
            annotation = json.loads(check['annotation_json'])
            force = check['reinject_mode'] == 'force'
            issues = []
            tags = {r['image_id']: r['tag'] for r in artifact_rows if r.get('tag')}
            if annotation.get('artist_inject_tags'):
                from annotation.artist_reinject import apply, id_of
                tags = {iid: apply(value, table['mapping'].get(id_of(iid), []), 'tag',
                                   source=table['source'], force=force)[0] for iid, value in tags.items()}
            artifact_rows, _ = rows(artifact_rows, table, annotation, force=force, fit=fit,
                                    tags_by_id=tags, issues=issues)
            require_review(table, issues)
            normalize(c,check['repo'],check['package'],check['source_sha256'],table,
                      annotation,force=force,review=True)
        report = merge(c, check["repo"], check["package"], check["source_sha256"], proof["expected_ids"], artifact_rows, check['created_at'],
                       artist_normalized=bool(check.get('reinject_mode')),
                       annotation=json.loads(check['annotation_json']) if check.get('reinject_mode') else None)
        # 当前旧调度仍独占整片；不能覆盖正在执行或预锁中的其它任务。
        if c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open'", (check["repo"], check["package"])).fetchone():
            raise ManagedError("该分片仍有有效任务，请先停止并释放任务，再重试补登记", 409)
        package = dispatch._row(c, "SELECT * FROM packages WHERE repo=? AND id=?", (check["repo"], check["package"]))
        if not package or package["sha256"] != check["source_sha256"]:
            raise ManagedError("中心源分片已变化，请重新检查", 409)
        if report.get('source_verification', {}).get('state') in ('pending', 'failed'):
            report['registered_parts'] = []
            c.execute("UPDATE history_checks SET status='verifying',report=?,updated_at=? WHERE check_id=?",
                      (json.dumps(report, ensure_ascii=False), time.time(), check['check_id']))
            return {'ok': True, 'verification_pending': True, 'registered_parts': [], 'report': report}
        registered = []
        for part in report["complete_parts"]:
            if part in dispatch.missing_parts(package):
                c.execute(f"UPDATE packages SET {part}_status='done', {part}_by=?,updated_at=? WHERE repo=? AND id=?",
                          (device["device_id"], time.strftime("%Y-%m-%dT%H:%M:%S%z"), check["repo"], check["package"]))
                registered.append(part)
        package = dispatch._row(c, "SELECT * FROM packages WHERE repo=? AND id=?", (check["repo"], check["package"]))
        if not dispatch.missing_parts(package):
            c.execute("UPDATE packages SET status='completed',completed_at=? WHERE repo=? AND id=?",
                      (time.strftime("%Y-%m-%dT%H:%M:%S%z"), check["repo"], check["package"]))
        report["registered_parts"] = registered
        from scheduling.pending_results import finalize
        finalize(c,check['repo'],check['package'],check['source_sha256'],report,device['device_id'])
        status = "registered" if not dispatch.missing_parts(package) else "incomplete"
        c.execute("UPDATE history_checks SET status=?,report=?,updated_at=? WHERE check_id=?",
                  (status, json.dumps(report, ensure_ascii=False), time.time(), check["check_id"]))
        store.event(c, "history_registered", check["repo"], check["package"], check["user_id"], "parts=" + ",".join(registered))
    return {"ok": True, "registered_parts": registered, "report": report, "status": status}
