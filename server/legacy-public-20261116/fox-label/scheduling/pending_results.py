"""本地完成但未回传的分项仍占用；上传并验收后才登记完成。"""
import json
import time

SCHEMA="""
CREATE TABLE IF NOT EXISTS pending_local_results (
 lease_id TEXT PRIMARY KEY, repo TEXT NOT NULL, pkg TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 device_id TEXT NOT NULL, parts_json TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def register(c,dispatch,lease,body):
    if lease['kind'] != 'active':
        raise dispatch.LeaseError('仅当前在线执行租约可登记本地完成')
    package=c.execute('SELECT sha256 FROM packages WHERE repo=? AND id=?',(lease['repo'],lease['pkg'])).fetchone()
    if not package:
        raise dispatch.LeaseError('源分片不存在')
    report = body.get('local_report') or {}
    assigned = json.loads(lease['parts_json'])
    if (not isinstance(report, dict) or report.get('source_sha256') != package['sha256']
            or type(report.get('expected')) is not int or report['expected'] <= 0
            or not isinstance(report.get('complete_parts'), list)
            or any(part not in ('tag','nl') for part in report['complete_parts'])
            or not isinstance(report.get('parts'), dict)
            or not set(assigned).issubset(report.get('complete_parts', []))):
        raise dispatch.LeaseError('本地逐图检查不完整，不能登记待回传')
    for part in assigned:
        counts = report.get('parts', {}).get(part, {})
        if (not isinstance(counts, dict)
                or any(type(counts.get(k)) is not int or counts[k] < 0 for k in ('success','excluded','failed','missing'))
                or counts['failed'] or counts['missing']
                or counts['success'] + counts['excluded'] != report['expected']):
            raise dispatch.LeaseError('本地逐图检查仍有失败或漏项，不能登记待回传')
    c.execute('INSERT OR IGNORE INTO pending_local_results VALUES(?,?,?,?,?,?,?)',
              (lease['lease_id'],lease['repo'],lease['pkg'],package['sha256'],lease['device_id'],lease['parts_json'],time.time()))
    c.execute("UPDATE leases SET status='local_done',closed_reason='awaiting_upload',progress_json=? WHERE lease_id=?",
              (json.dumps(body.get('progress') or {}),lease['lease_id']))
    from managed.limits import completed
    completed(c,dict(lease))
    from .leases import refresh
    refresh(c,lease['repo'],lease['pkg'])


def clear(c,lease_id):
    c.execute('DELETE FROM pending_local_results WHERE lease_id=?',(lease_id,))


def finalize(c,repo,pkg,sha,report,device_id=None):
    if report.get('source_verification',{}).get('state') in ('pending','failed'):
        return
    rows=c.execute('SELECT * FROM pending_local_results WHERE repo=? AND pkg=? AND source_sha256=?'
                   + (' AND device_id=?' if device_id else ''),
                   (repo,pkg,sha,device_id) if device_id else (repo,pkg,sha)).fetchall()
    complete=set(report.get('complete_parts',[]))
    for row in rows:
        full=set(json.loads(row['parts_json'])).issubset(complete)
        if not full and device_id is None:
            continue
        for part in set(json.loads(row['parts_json'])) & complete:
            c.execute(f"UPDATE packages SET {part}_status='done',{part}_by=? WHERE repo=? AND id=?",
                      (row['device_id'],repo,pkg))
            c.execute('DELETE FROM part_assignments WHERE repo=? AND pkg=? AND part=?', (repo,pkg,part))
        c.execute("UPDATE leases SET status=?,closed_reason=? WHERE lease_id=? AND status='local_done'",
                  ('done' if full else 'failed','uploaded' if full else 'upload_incomplete',row['lease_id']))
        clear(c,row['lease_id'])


def view(c,device_id):
    return [{**dict(r), 'parts': json.loads(r['parts_json'])} for r in c.execute('SELECT p.*,l.user_id FROM pending_local_results p JOIN leases l USING(lease_id)'
                                      ' JOIN packages source ON source.repo=p.repo AND source.id=p.pkg'
                                      ' AND source.sha256=p.source_sha256 WHERE p.device_id=? ORDER BY p.created_at', (device_id,))]
