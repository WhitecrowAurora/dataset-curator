"""管理员批量冲突决议：一事务采用候选，一次归并整包。"""
import json
import time
from collections import Counter

from verification.payload import payload_done
from . import conflicts, details


def candidate(variants, part, strategy):
    if len(variants) < 2:
        return None, '缺少候选结果'
    if not all(payload_done(part, v) for v in variants):
        return None, '候选结果不完整'
    key = 'timestamp' if strategy in ('newest', 'oldest') else 'length'
    values = [conflicts.variant_info(v, part)[key] for v in variants]
    if any(v is None for v in values):
        return None, '缺少生成时间' if key == 'timestamp' else '缺少有效正文'
    target = (max if strategy in ('newest', 'longest') else min)(values)
    winners = [i for i, value in enumerate(values) if value == target]
    if len(winners) != 1:
        return None, '生成时间相同' if key == 'timestamp' else '正文长度相同'
    return winners[0], None


def resolve(c, dispatch, body, actor):
    repo, pkg = body.get('repo'), body.get('package')
    strategy, scope = body.get('strategy'), body.get('scope')
    if strategy not in ('newest', 'oldest', 'longest', 'shortest'):
        raise ValueError('请选择较新、较旧、较长或较短结果')
    if scope not in ('selected', 'first', 'all'):
        raise ValueError('请选择批量处理范围')
    package = c.execute('SELECT * FROM packages WHERE repo=? AND id=?', (repo, pkg)).fetchone()
    if not package:
        raise ValueError('分片不存在')
    sha = package['sha256']
    if body.get('source_sha256') != sha:
        raise ValueError('分片来源已变化，请刷新详情后重新选择')
    artifact = c.execute('SELECT * FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?', (repo, pkg, sha)).fetchone()
    if not artifact:
        raise ValueError('分片没有回传结果')
    selected = set()
    if scope == 'selected':
        items = body.get('items')
        if not isinstance(items, list) or not items:
            raise ValueError('请勾选待处理冲突')
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('image_id'), str) or not item['image_id'] or item.get('part') not in ('tag', 'nl'):
                raise ValueError('冲突列表格式不正确')
            selected.add((item['image_id'], item['part']))
        if len(selected) != len(items):
            raise ValueError('不能重复选择冲突')
    limit = body.get('count') if scope == 'first' else None
    if scope == 'first' and (type(limit) is not int or limit <= 0):
        raise ValueError('选择数量必须是正整数')
    from scheduling.leases import held
    busy = held(c).get((repo, pkg), set())
    skipped, changed, updates = Counter(), set(), []
    total = 0
    for record in c.execute("SELECT * FROM artifact_conflicts WHERE repo=? AND pkg=? AND source_sha256=? AND state='pending' ORDER BY image_id,part", (repo, pkg, sha)):
        key = (record['image_id'], record['part'])
        if scope == 'selected' and key not in selected:
            continue
        if limit is not None and total >= limit:
            break
        total += 1
        if record['part'] in busy:
            skipped['正在执行或核验'] += 1
            continue
        chosen, reason = candidate(json.loads(record['variants_json']), record['part'], strategy)
        if reason:
            skipped[reason] += 1
            continue
        changed.add(key)
        updates.append((chosen, actor, time.time(), repo, pkg, sha, *key))
    if scope == 'selected' and total < len(selected):
        skipped['不存在或已处理'] += len(selected) - total
        total = len(selected)
    c.executemany("UPDATE artifact_conflicts SET state='resolved',chosen=?,actor=?,updated_at=? WHERE repo=? AND pkg=? AND source_sha256=? AND image_id=? AND part=? AND state='pending'", updates)
    if changed:
        result = details.finish(c, dispatch, package, artifact, changed, actor)
    else:
        result = details.view(c, repo, pkg)
    result['batch'] = dict(strategy=strategy, scope=scope, requested=total, applied=len(changed),
                           skipped=sum(skipped.values()), reasons=dict(skipped))
    return result
