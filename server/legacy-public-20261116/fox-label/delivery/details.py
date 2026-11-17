"""现有分片详情：核验等待、漏项与分侧冲突处理。"""
import json
import time

from verification.payload import payload_done
from source_catalog import store as catalogs
from . import conflicts, reconciliation


def view(c, repo, pkg, offset=0):
    package = c.execute('SELECT * FROM packages WHERE repo=? AND id=?', (repo, pkg)).fetchone()
    if not package:
        raise ValueError('分片不存在')
    sha = package['sha256']
    snapshot = reconciliation.snapshot(c, repo, pkg, sha)
    source = catalogs.status(c, repo, pkg, sha, package['size'])
    job = c.execute('SELECT * FROM source_jobs WHERE repo=?', (repo,)).fetchone()
    reason = source.get('error', '')
    if source['state'] == 'pending':
        reason = (job['error'] or ('正在准备 ' + job['package'] if job['state'] == 'running' else '等待清单任务')) if job else '尚未准备清单'
    issues = []
    for row in snapshot['rows']:
        for part in ('tag', 'nl'):
            if not payload_done(part, row.get(part)):
                outcome = (row.get('outcomes') or {}).get(part, {})
                if outcome.get('status') != 'excluded':
                    issues.append(dict(image_id=row['image_id'], part=part, status=outcome.get('status', 'missing'), reason=outcome.get('reason', '')))
    evidence = [{**{k: r[k] for k in ('image_id', 'part', 'state', 'chosen', 'actor')}, 'variants': json.loads(r['variants_json'])}
                for r in conflicts.records(c, repo, pkg, sha)]
    rejection = c.execute('SELECT rows_json FROM source_rejections WHERE repo=? AND pkg=? AND source_sha256=?', (repo, pkg, sha)).fetchone()
    if rejection:
        foreign = json.loads(rejection['rows_json'])
        if foreign:
            reason = '产物含原片之外的图片，请检查分片与产物目录的关联'
            issues.extend(dict(image_id=r['image_id'], part='tag/nl', status='foreign', reason='图片不在中心原片清单') for r in foreign)
    from scheduling.leases import held as held_parts
    held = held_parts(c).get((repo, pkg), set())
    page = max(0, int(offset))
    return dict(ok=True, repo=repo, package=pkg, source=source, waiting_reason=reason, job=dict(job) if job else None,
                report=snapshot['report'], issues=issues[page:page + 100], issue_total=len(issues),
                next_offset=page + 100 if page + 100 < len(issues) else None,
                conflicts=evidence[page:page + 100], conflict_total=len(evidence), busy_parts=sorted(held))


def resolve(c, dispatch, body, actor):
    repo, pkg = body.get('repo'), body.get('package')
    package = c.execute('SELECT * FROM packages WHERE repo=? AND id=?', (repo, pkg)).fetchone()
    if not package:
        raise ValueError('分片不存在')
    sha = package['sha256']
    part = conflicts.apply(c, repo, pkg, sha, body, actor)
    row = c.execute('SELECT * FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?', (repo, pkg, sha)).fetchone()
    if not row:
        raise ValueError('分片没有回传结果')
    rows = json.loads(row['rows_json'])
    # 清掉规范快照中的冲突项；prepare 会注入选择值，重做保持空白。
    for item in rows:
        if item['image_id'] == body['image_id']:
            item[part] = ''
            item['outcomes'] = {k: v for k, v in (item.get('outcomes') or {}).items() if k != part}
    c.execute('UPDATE package_artifacts SET rows_json=? WHERE repo=? AND pkg=? AND source_sha256=?',
              (json.dumps(rows, ensure_ascii=False), repo, pkg, sha))
    report = reconciliation.merge(c, repo, pkg, sha, json.loads(row['expected_json']), [])
    from source_catalog.revalidation import refresh
    refresh(c, dispatch, repo, dict(package))
    for p in (part,):
        if p in report['complete_parts']:
            c.execute(f"UPDATE packages SET {p}_status='done',{p}_by=COALESCE({p}_by,?),updated_at=? WHERE repo=? AND id=?",
                      (actor, time.strftime('%Y-%m-%dT%H:%M:%S%z'), repo, pkg))
            c.execute('DELETE FROM part_assignments WHERE repo=? AND pkg=? AND part=?', (repo, pkg, p))
    from scheduling.leases import refresh as refresh_package
    refresh_package(c, repo, pkg)
    return view(c, repo, pkg)
