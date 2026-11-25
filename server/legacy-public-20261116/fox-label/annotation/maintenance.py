"""补注入规范产物与全部冲突候选；保留正文差异和管理员决议。"""
import json
import time
from .artist_reinject import rows, apply, id_of, payload, ArtistPrefixConflict
from .review import require_review
from .text import fit


def normalize(c, repo, pkg, sha, table, options, *, force=True, review=False):
    item = c.execute('SELECT * FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?', (repo,pkg,sha)).fetchone()
    if not item:
        return dict(changed_tag=0, changed_nl=0, matched=0, missing=0, variants=0)
    original = json.loads(item['rows_json'])
    tags_by_id={r['image_id']:r.get('tag','') for r in original if r.get('tag')}
    issues = []
    converted, stats = rows(original, table, options, force=force, fit=fit,tags_by_id=tags_by_id,issues=issues,review=review)
    conflicts = [dict(r) for r in c.execute('SELECT * FROM artifact_conflicts WHERE repo=? AND pkg=? AND source_sha256=?', (repo,pkg,sha))]
    changes = []
    tag_by_id = {r['image_id']: payload(r.get('tag')) for r in converted}
    # TAG 原本可能位于另一个逐侧行，NL 预算要使用补注入后的完整标签。
    for record in conflicts:
        part = record['part']
        if not options.get('artist_inject_tags' if part == 'tag' else 'artist_inject'):
            continue
        tag = tag_by_id.get(record['image_id'])
        tag = tag.get('tag', '') if isinstance(tag, dict) else tag or ''
        values, changed = [], 0
        for value in json.loads(record['variants_json']):
            names = table['mapping'].get(id_of(record['image_id']), [])
            try:
                value, modified = apply(value, names, part, source=table['source'], force=force,
                                        fit=lambda text,prefix,data: fit(text,prefix,data,tag), review=review)
            except ArtistPrefixConflict as exc:
                issues.append(dict(image_id=record['image_id'], part=part, artists=names,
                                   existing_prefix=exc.prefix, reason=str(exc)))
                modified = False
            values.append(value);changed += modified
        if changed:
            changes.append((json.dumps(values, ensure_ascii=False),record['image_id'],part))
    require_review(table, issues)
    stats['variants'] = len(changes)
    stats['changed'] = converted != original or bool(changes)
    if converted != original or changes:
        c.execute('INSERT INTO artist_backups VALUES(?,?,?,?,?,?)',
                  (repo,pkg,sha,time.time(),item['rows_json'],json.dumps(conflicts, ensure_ascii=False)))
        c.execute('UPDATE package_artifacts SET rows_json=? WHERE repo=? AND pkg=? AND source_sha256=?',
                  (json.dumps(converted,ensure_ascii=False,separators=(',',':')),repo,pkg,sha))
        for values,iid,part in changes:
            c.execute('UPDATE artifact_conflicts SET variants_json=? WHERE repo=? AND pkg=? AND source_sha256=? AND image_id=? AND part=?',
                      (values,repo,pkg,sha,iid,part))
    return stats


def cached(c, repo, pkg, sha, incoming, *, annotation=None):
    from .tables import prepare, for_task
    from .store import config
    from assignments.annotation import for_package
    from pathlib import Path
    cfg = config(c, repo)
    if annotation is None and not cfg['enabled']:
        return incoming
    database = c.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        return incoming
    # 只读已验证缓存，归并事务不发网络请求。
    class Cache:
        dir = Path(database).parent
    table = (for_task(Cache(),dict(repo=repo,pkg=pkg,annotation_json=json.dumps(annotation)),cached=True)
             if annotation is not None and annotation.get('artist_table') else
             prepare(Cache(), repo, pkg, cfg, cached=True) if annotation is None else None)
    if table is None:
        return incoming
    package = dict(c.execute('SELECT * FROM packages WHERE repo=? AND id=?',(repo,pkg)).fetchone())
    options = annotation if annotation is not None else for_package(c, package, ['tag','nl'])
    normalize(c,repo,pkg,sha,table,options,force=False)
    saved = c.execute('SELECT rows_json FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?',(repo,pkg,sha)).fetchone()
    tags = {r['image_id']:r['tag'] for r in json.loads(saved[0]) if r.get('tag')} if saved else {}
    incoming_tags={r['image_id']:r['tag'] for r in incoming if r.get('tag')}
    tags.update(incoming_tags)
    if options.get('artist_inject_tags'):
        tags={iid:apply(tag,table['mapping'].get(id_of(iid),[]),'tag',source=table['source'])[0] for iid,tag in tags.items()}
    issues = []
    result, _ = rows(incoming,table,options,force=False,fit=fit,tags_by_id=tags,issues=issues,review=False)
    require_review(table, issues)
    return result
