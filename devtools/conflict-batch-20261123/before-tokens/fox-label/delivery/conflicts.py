"""分侧冲突证据与管理员决议，原始回传文件保持不变。"""
import json
import math
import time

from verification.history import _body
from verification.payload import payload_done


def variant_info(value, part):
    """只比较标注正文；生成时间必须来自产物本身。"""
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        pass
    stamp = value.get('ts') if isinstance(value, dict) else None
    try:
        stamp = float(stamp) if not isinstance(stamp, bool) else None
    except (TypeError, ValueError, OverflowError):
        stamp = None
    if stamp is not None and (not math.isfinite(stamp) or stamp <= 0):
        stamp = None
    text = value.get('tag') if part == 'tag' and isinstance(value, dict) else value
    if part == 'nl' and isinstance(value, dict):
        text = value.get('caption') or value.get('nl')
    return dict(timestamp=stamp, length=len(text.strip()) if isinstance(text, str) else None)


def ensure_schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS artifact_conflicts (repo TEXT NOT NULL,pkg TEXT NOT NULL,"
              "source_sha256 TEXT NOT NULL,image_id TEXT NOT NULL,part TEXT NOT NULL,variants_json TEXT NOT NULL,"
              "state TEXT NOT NULL,chosen INTEGER,actor TEXT NOT NULL DEFAULT '',updated_at REAL NOT NULL,"
              "PRIMARY KEY(repo,pkg,source_sha256,image_id,part))")


def records(c, repo, pkg, sha):
    return [dict(r) for r in c.execute("SELECT * FROM artifact_conflicts WHERE repo=? AND pkg=? AND source_sha256=?"
                                     " ORDER BY image_id,part", (repo, pkg, sha))]


def blocked(c):
    result = {}
    for row in c.execute("SELECT a.repo,a.pkg,a.part FROM artifact_conflicts a JOIN packages p"
                         " ON p.repo=a.repo AND p.id=a.pkg AND p.sha256=a.source_sha256 WHERE a.state='pending'"):
        result.setdefault((row['repo'], row['pkg']), set()).add(row['part'])
    for row in c.execute("SELECT a.repo,a.pkg FROM source_rejections a JOIN packages p"
                         " ON p.repo=a.repo AND p.id=a.pkg AND p.sha256=a.source_sha256 WHERE a.rows_json<>'[]'"):
        result.setdefault((row['repo'], row['pkg']), set()).update(('tag', 'nl'))
    for row in c.execute("SELECT a.repo,a.pkg,a.report_json FROM package_artifacts a JOIN packages p"
                         " ON p.repo=a.repo AND p.id=a.pkg AND p.sha256=a.source_sha256"):
        problems = json.loads(row['report_json']).get('conflicts', [])
        parts = {p for p in ('tag', 'nl') if any(': ' + p + ' 内容冲突' in message for message in problems)}
        if parts:
            result.setdefault((row['repo'], row['pkg']), set()).update(parts)
    return result


def prepare(c, repo, pkg, sha, old_rows, incoming, expected, submitted_at=None):
    """保留所有不同成功值；决议已覆盖的旧值再次上报保持幂等。"""
    known_ids = set(expected)
    evidence = {(r['image_id'], r['part']): r for r in records(c, repo, pkg, sha) if r['image_id'] in known_ids}
    values = {}
    combined = [*old_rows, *incoming]
    for row in combined:
        for part in ('tag', 'nl'):
            if payload_done(part, row.get(part)):
                key = (row['image_id'], part)
                variants = values.setdefault(key, [])
                if not any(_body(v, part) == _body(row[part], part) for v in variants):
                    variants.append(row[part])
    for key, variants in values.items():
        saved = evidence.get(key)
        known = json.loads(saved['variants_json']) if saved else []
        additions = [v for v in variants if not any(_body(v, key[1]) == _body(k, key[1]) for k in known)]
        if not saved and len(variants) < 2:
            continue
        state, chosen = (saved['state'], saved['chosen']) if saved else ('pending', 0)
        if saved and state == 'redone':
            fresh = [r[key[1]] for r in incoming if submitted_at is not None and submitted_at > saved['updated_at']
                     and r.get('image_id') == key[0] and payload_done(key[1], r.get(key[1]))]
            if fresh:
                for value in fresh:
                    if not any(_body(value, key[1]) == _body(k, key[1]) for k in known):
                        known.append(value)
                chosen = next(i for i, k in enumerate(known) if _body(k, key[1]) == _body(fresh[0], key[1]))
                state = 'resolved' if all(_body(v, key[1]) == _body(fresh[0], key[1]) for v in fresh) else 'pending'
        else:
            known.extend(additions)
            if additions and saved:
                state = 'pending'
        c.execute("INSERT INTO artifact_conflicts VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repo,pkg,source_sha256,image_id,part)"
                  " DO UPDATE SET variants_json=excluded.variants_json,state=excluded.state,chosen=excluded.chosen,"
                  " updated_at=CASE WHEN artifact_conflicts.state<>excluded.state OR artifact_conflicts.variants_json<>excluded.variants_json"
                  " THEN excluded.updated_at ELSE artifact_conflicts.updated_at END",
                  (repo, pkg, sha, *key, json.dumps(known, ensure_ascii=False), state, chosen, saved['actor'] if saved else '', time.time()))
        evidence[key] = dict(image_id=key[0], part=key[1], variants_json=json.dumps(known), state=state, chosen=chosen)
    # 把每项统一成选定值后再调用原有逐图统计；冲突单独阻止该侧完成。
    for row in combined:
        for part in ('tag', 'nl'):
            record = evidence.get((row['image_id'], part))
            if record:
                row = dict(row)
                row[part] = ''
                row['outcomes'] = {k: v for k, v in (row.get('outcomes') or {}).items() if k != part}
        yield row
    for (iid, part), record in evidence.items():
        if record['state'] == 'redone':
            continue
        variants = json.loads(record['variants_json'])
        if variants:
            yield {'image_id': iid, part: variants[record['chosen'] or 0]}


def apply(c, repo, pkg, sha, body, actor):
    iid, part, action = body.get('image_id'), body.get('part'), body.get('resolution')
    if not isinstance(iid, str) or not iid:
        raise ValueError('图片 ID 不正确')
    if part not in ('tag', 'nl'):
        raise ValueError('请选择 TAG 或 NL')
    from scheduling.leases import held
    if part in held(c).get((repo, pkg), set()):
        raise ValueError('该分项正在执行或检查中')
    record = c.execute("SELECT * FROM artifact_conflicts WHERE repo=? AND pkg=? AND source_sha256=? AND image_id=? AND part=?",
                       (repo, pkg, sha, iid, part)).fetchone()
    if not record or record['state'] != 'pending':
        raise ValueError('冲突不存在或已处理')
    variants = json.loads(record['variants_json'])
    chosen = body.get('variant')
    if action == 'choose':
        if len(variants) < 2:
            raise ValueError('旧冲突未保留其他结果，请重做该项')
        if type(chosen) is not int or not 0 <= chosen < len(variants):
            raise ValueError('请选择有效结果')
        state = 'resolved'
    elif action == 'redo':
        chosen, state = None, 'redone'
    else:
        raise ValueError('未知冲突操作')
    c.execute("UPDATE artifact_conflicts SET state=?,chosen=?,actor=?,updated_at=? WHERE repo=? AND pkg=? AND source_sha256=? AND image_id=? AND part=?",
              (state, chosen, actor, time.time(), repo, pkg, sha, iid, part))
    return part
