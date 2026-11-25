"""原离线租约归属下的预览与回收，不新派工、不覆盖管理员决议。"""
import hashlib
import json
import secrets
import shutil
import time
import threading

from assignments import offline
from delivery.reconciliation import accept_rows
from .archive import Archive, MAX_UPLOAD

UPLOAD_LOCK = threading.Lock()


def root(store):
    path = store.dir / 'offline-imports'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def metadata(store, token, actor):
    if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('导入编号不正确')
    path = root(store) / (token + '.json')
    if not path.is_file():raise ValueError('导入不存在，请重新上传')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data['actor'] != actor or time.time() - data['created_at'] > 86400:
        raise ValueError('导入预览已过期或不属于当前管理员，请重新上传')
    return path, data


def receive(store, stream, length, ws_id, actor):
    with UPLOAD_LOCK:
        return _receive(store, stream, length, ws_id, actor)


def _receive(store, stream, length, ws_id, actor):
    if not 0 < length <= MAX_UPLOAD:raise ValueError('网页回收包不能超过 64 MiB')
    directory = root(store)
    # 只清理由本模块产生、已过一天的临时上传；正式产物不参与。
    for path in directory.iterdir():
        if path.suffix in ('.zip', '.json', '.part') and time.time() - path.stat().st_mtime > 86400:
            path.unlink(missing_ok=True)
    if sum(p.stat().st_size for p in directory.iterdir()) + length > 256 * 1024 * 1024:
        raise ValueError('待确认回收包已达到 256 MiB，请先确认或取消已有导入')
    if shutil.disk_usage(directory).free < length + 512 * 1024 * 1024:
        raise ValueError('服务器剩余空间不足，无法暂存回收包')
    token = secrets.token_hex(16)
    path = directory / (token + '.zip')
    try:
        with path.open('xb') as output:
            path.chmod(0o600)
            remaining = length
            while remaining:
                block = stream.read(min(64 * 1024, remaining))
                if not block:raise ValueError('上传未完成，请重新上传')
                output.write(block);remaining -= len(block)
        data = {'actor': actor, 'created_at': time.time(), 'ws_id': ws_id}
        with (directory / (token + '.json')).open('x', encoding='utf-8') as output:
            json.dump(data, output)
        (directory / (token + '.json')).chmod(0o600)
        return preview(store, token, actor)
    except Exception:
        path.unlink(missing_ok=True)
        path.with_suffix('.json').unlink(missing_ok=True)
        raise


def inspect(store, dispatch, c, archive, ws_id):
    manifest = archive.manifest
    identity = manifest.get('identity')
    if not isinstance(identity, dict):raise ValueError('回收包缺少离线身份')
    job = dispatch._row(c, 'SELECT * FROM offline_jobs WHERE id=?', (manifest.get('id'),))
    if not job or job['user_id'] != identity.get('user_id') or job['device_id'] != identity.get('device_id'):
        raise ValueError('回收包不是本中心签发的离线任务')
    device = dispatch._row(c, 'SELECT * FROM devices WHERE device_id=?', (job['device_id'],))
    if not device or device.get('workspace_id') != ws_id:
        raise ValueError('离线任务不属于当前工作区')
    summaries, fingerprint = [], []
    for task in manifest['tasks']:
        lid = task['lease_id']
        lease = dispatch._row(c, 'SELECT * FROM leases WHERE lease_id=?', (lid,))
        linked = c.execute('SELECT 1 FROM offline_leases WHERE lease_id=? AND job_id=?', (lid, job['id'])).fetchone()
        if (not lease or not linked or lease['kind'] != 'offline' or lease['user_id'] != job['user_id']
                or lease['device_id'] != job['device_id'] or lease['repo'] != task.get('repo')
                or lease['pkg'] != task.get('package')):
            raise ValueError('分片与原离线分配不匹配：' + lid)
        offline.authenticate(c, dispatch, lease, identity.get('secret'))
        fingerprint.append(lease)
        item = {'lease_id': lid, 'repo': lease['repo'], 'package': lease['pkg'],
                'parts': json.loads(lease['parts_json']), 'status': lease['status'], 'eligible': False}
        if lease['status'] in ('done', 'submitted'):
            item.update(state='received', message='中心已接收，跳过重复回收')
        elif lease['status'] != 'open':
            item.update(state='invalid', message='租约已失效：' + lease['status'])
        else:
            files = archive.files(lid)
            c.execute('SAVEPOINT offline_preview')
            try:
                from delivery.routes import validate_lease
                validate_lease(c, lease)
                for name in files:store.delivery.validate_filename(lease, name)
                proof = json.loads(files['proof.json'])
                rows = [json.loads(line) for name, content in files.items() if name.endswith('.jsonl')
                        for line in content.decode('utf-8').splitlines() if line.strip()]
                saved = c.execute('SELECT rows_json,report_json,updated_at FROM package_artifacts WHERE repo=? AND pkg=?',
                                  (lease['repo'], lease['pkg'])).fetchall()
                digest = hashlib.sha256()
                for record in saved:
                    digest.update(json.dumps(dict(record), sort_keys=True, ensure_ascii=False).encode())
                for record in c.execute('SELECT * FROM artifact_conflicts WHERE repo=? AND pkg=? ORDER BY image_id,part',
                                        (lease['repo'], lease['pkg'])):
                    digest.update(json.dumps(dict(record), sort_keys=True, ensure_ascii=False).encode())
                fingerprint.append(digest.hexdigest())
                report = accept_rows(dispatch, c, lease, proof, rows)['report']
                parts = report.get('provisional_complete_parts', report['complete_parts'])
                eligible = set(item['parts']).issubset(parts)
                item.update(report=report, eligible=eligible, state='ready' if eligible else 'incomplete',
                            message='可确认回收' if eligible else '缺项、失败或内容冲突，请修正后重新上传')
            except ValueError as exc:
                item.update(state='invalid', message=str(exc))
            finally:
                c.execute('ROLLBACK TO offline_preview');c.execute('RELEASE offline_preview')
        summaries.append(item)
    revision = hashlib.sha256(json.dumps([summaries, fingerprint], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'ok': True, 'job_id': job['id'], 'user_id': job['user_id'], 'items': summaries, 'revision': revision}


def preview(store, token, actor):
    import dispatch
    _, data = metadata(store, token, actor)
    archive = Archive(root(store) / (token + '.zip'))
    c = store.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        result = inspect(store, dispatch, c, archive, data['ws_id'])
        return {**result, 'token': token}
    finally:
        c.rollback();c.close();archive.close()


def publish(delivery, archive, lease, device):
    for name, data in archive.files(lease['lease_id']).items():
        chunks = []
        for start in range(0, len(data), 8 * 1024 * 1024):
            block = data[start:start + 8 * 1024 * 1024]
            sha = hashlib.sha256(block).hexdigest()
            delivery.blobs.put_chunk(sha, block);chunks.append(sha)
        delivery.publish(lease, device, {'name': name, 'chunks': chunks, 'bytes': len(data)})


def confirm(store, dispatch, body, actor):
    token = body.get('token')
    _, data = metadata(store, token, actor)
    selected = body.get('lease_ids')
    if (not isinstance(selected, list) or not selected or any(not isinstance(lid, str) for lid in selected)
            or len(selected) != len(set(selected))):
        raise ValueError('请勾选需要回收的分片')
    if not store.delivery:raise ValueError('服务器未启用产物接收')
    archive = Archive(root(store) / (token + '.zip'))
    c = store.conn()
    try:
        with store.delivery.publish_lock, store._write_lock, dispatch._Tx(c):
            result = inspect(store, dispatch, c, archive, data['ws_id'])
            items = {i['lease_id']: i for i in result['items']}
            if any(lid not in items for lid in selected):raise ValueError('所选分片不在回收包中')
            if all(items[lid]['state'] == 'received' for lid in selected):
                return {**result, 'token': token, 'replay': True, 'confirmed': selected}
            if result['revision'] != body.get('revision'):
                return {**result, 'token': token, 'stale': True, 'error': '中心记录已变化，请核对新的预览后再次确认'}
            if any(not items[lid]['eligible'] and items[lid]['state'] != 'received' for lid in selected):
                raise ValueError('所选分片仍有缺项、冲突或租约失效，不能确认回收')
            for lid in selected:
                if items[lid]['state'] == 'received':continue
                lease = dispatch._row(c, 'SELECT * FROM leases WHERE lease_id=?', (lid,))
                device = dispatch._row(c, 'SELECT * FROM devices WHERE device_id=?', (lease['device_id'],))
                publish(store.delivery, archive, lease, device)
                files = archive.files(lid)
                rows = [json.loads(line) for name, content in files.items() if name.endswith('.jsonl')
                        for line in content.decode('utf-8').splitlines() if line.strip()]
                accept_rows(dispatch, c, lease, json.loads(files['proof.json']), rows)
                dispatch._finish_impl(c, dispatch.params(), {**archive.manifest['identity'], 'lease_id': lid,
                                      'parts': {p: 'done' for p in items[lid]['parts']}}, True)
                store.event(c, 'offline_import', lease['repo'], lease['pkg'], actor,
                            'job=' + result['job_id'] + '; lease=' + lid + '; user=' + lease['user_id'])
        return {**preview(store, token, actor), 'confirmed': selected}
    finally:c.close();archive.close()


def action(store, dispatch, body, actor):
    kind = body.get('action')
    if kind == 'preview':return preview(store, body.get('token'), actor)
    if kind == 'confirm':return confirm(store, dispatch, body, actor)
    if kind == 'cancel':
        path, _ = metadata(store, body.get('token'), actor)
        path.with_suffix('.zip').unlink(missing_ok=True);path.unlink(missing_ok=True)
        return {'ok': True}
    raise ValueError('未知离线导入操作')
