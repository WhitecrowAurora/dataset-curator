"""离线清单只携带已分配任务，素材由客户端从仓库下载。"""
import json
import secrets
import time


def ensure_schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS offline_jobs (id TEXT PRIMARY KEY,user_id TEXT NOT NULL,"
              "device_id TEXT NOT NULL,secret_hash TEXT NOT NULL,status TEXT NOT NULL,"
              "error TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS offline_leases (lease_id TEXT PRIMARY KEY,job_id TEXT NOT NULL)")
    if 'deleted_at' not in {r['name'] for r in c.execute('PRAGMA table_info(offline_jobs)')}:
        c.execute('ALTER TABLE offline_jobs ADD COLUMN deleted_at REAL')


def authenticate(c, dispatch, lease, secret):
    row = c.execute("SELECT j.secret_hash,j.status FROM offline_jobs j JOIN offline_leases l ON l.job_id=j.id"
                    " WHERE l.lease_id=?", (lease["lease_id"],)).fetchone()
    if not row or row[1]=="canceled" or not isinstance(secret,str) or not secrets.compare_digest(row[0],dispatch.hash_secret(secret)):
        raise dispatch.AuthError("离线任务身份不匹配")
    account=c.execute("SELECT u.status,d.status FROM users u JOIN devices d ON d.user_id=u.user_id WHERE d.device_id=? AND u.user_id=?",
                      (lease["device_id"],lease["user_id"])).fetchone()
    if not account or account[0]!="active" or account[1] in ("blocked","archived"):
        raise dispatch.AuthError("离线任务用户或设备已停用")


def refresh(c, device):
    if c.execute("SELECT 1 FROM leases WHERE device_id=? AND status IN ('open','submitted')",(device,)).fetchone():return
    failed=c.execute("SELECT 1 FROM leases WHERE device_id=? AND status<>'done'",(device,)).fetchone()
    c.execute("UPDATE offline_jobs SET status=? WHERE device_id=? AND status<>'canceled'",
              ("failed" if failed else "completed",device))


def status(c, dispatch, body):
    lease=dispatch._row(c,"SELECT * FROM leases WHERE lease_id=?",(dispatch._sid(body.get("lease_id"),"lease_id"),))
    if not lease or lease["kind"]!="offline" or lease["device_id"]!=body.get("device_id") or lease["user_id"]!=body.get("user_id"):
        raise dispatch.AuthError("离线任务身份不匹配")
    authenticate(c,dispatch,lease,body.get("secret"))
    return {"ok":True,"status":lease["status"]}


def view(store, ws_id=None, offset=0):
    try:offset=int(offset)
    except (ValueError,TypeError) as exc:raise ValueError("分页位置不正确") from exc
    if offset<0:raise ValueError("分页位置不正确")
    c=store.conn()
    try:
        query=" FROM offline_jobs j LEFT JOIN devices d ON d.device_id=j.device_id WHERE j.deleted_at IS NULL"
        params=[]
        if ws_id:query+=" AND d.workspace_id=?";params.append(ws_id)
        total=c.execute("SELECT COUNT(*)"+query,params).fetchone()[0]
        rows=[dict(r) for r in c.execute("SELECT j.id,j.user_id,j.device_id,j.status,j.error,j.created_at"+query+
                                        " ORDER BY j.created_at DESC,j.id DESC LIMIT 100 OFFSET ?",[*params,offset])]
        end=offset+len(rows)
        return {"ok":True,"items":rows,"total":total,"next_offset":end if end<total else None}
    finally:c.close()


def create(store, dispatch, contracts, body, actor, *, dispatch_plan=None, online_identity=None):
    from .store import selection
    from .annotation import FIELDS, options as annotation_options
    items=selection(body)
    annotation = annotation_options(body["annotation"]) if "annotation" in body else None
    user=dispatch._sid(body.get("user_id"),"user_id")
    cfg=contracts.config(body.get("config",{}))
    job="O"+secrets.token_hex(8); device="offline-"+secrets.token_hex(8)
    credential=secrets.token_urlsafe(32)
    server=body.get("server", "")
    from urllib.parse import urlsplit
    parsed=urlsplit(server)
    if parsed.scheme not in ("https","http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请填写回传的调度中心 HTTP 地址")
    root=store.dir/"offline"/job
    leases=[]
    from . import offline_document
    with offline_document.artist_tables(store, items) as artist_tables, store._write_lock:
        c=store.conn()
        try:
            with dispatch._Tx(c):
                ws=dispatch.ws_get(c,body.get("ws_id"))
                allocated = {}
                if dispatch_plan is not None:
                    from .plans import selected, rows, outstanding_offline
                    from .store import identity
                    online_device = identity(c, dispatch, online_identity, update_parts=False)
                    prior = c.execute('SELECT * FROM dispatch_plan_applications WHERE plan_id=? AND device_id=?', (dispatch_plan['id'], online_device['device_id'])).fetchone()
                    if prior:
                        if prior['mode'] != 'offline': raise ValueError('此清单已经联网应用')
                        return dict(ok=True, manifest=manifest(store, prior['job_id']), replay=True)
                    if outstanding_offline(c,online_device['device_id']):
                        raise ValueError('本机仍持有离线分派，请先补传或在中心取消原分配')
                    if c.execute("SELECT 1 FROM leases WHERE device_id=? AND status IN ('open','submitted')", (online_device['device_id'],)).fetchone():
                        raise ValueError('本机仍持有任务，请先完成或释放')
                    current = {(r['repo'],r['package'],r['part']):r for r in rows(c, dispatch, ws)}
                    mine = selected(c, dispatch, online_device, dispatch_plan)
                    from scheduling.policies import repo_order
                    permitted = repo_order(c, online_device, dispatch.workspace_of(c, online_device))
                    for task in mine:
                        key = task['repo'],task['package']
                        if key not in items: continue
                        now = current.get((*key,task['part']))
                        if now is None:
                            p = dispatch._row(c,'SELECT * FROM packages WHERE repo=? AND id=?',key)
                            if p and task['part'] not in dispatch.missing_parts(p): continue
                        if not now or key[0] not in permitted or any(now[k] != task[k] for k in ('user_id','device_id','sha256','size','repo_type','annotation')):
                            raise ValueError('派工或画师配置已变化，请重新导入：'+key[1])
                        allocated.setdefault(key, []).append(task)
                    if set(allocated) != set(items): raise ValueError('任务已完成或改派，请刷新分派')
                for repo,pkg in items:
                    row=dispatch._row(c,"SELECT p.*,r.enabled FROM packages p JOIN repos r ON r.repo=p.repo WHERE p.repo=? AND p.id=?",(repo,pkg))
                    if not row or not row["enabled"] or row["status"] not in (('available','claimed') if dispatch_plan else ('available',)) or repo not in ws["repos"]:
                        raise dispatch.LeaseError(f"分包不能导出：{repo}/{pkg}")
                    if dispatch_plan is None and c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open'",(repo,pkg)).fetchone():
                        raise dispatch.LeaseError(f"分包已有租约：{pkg}")
                    from scheduling.leases import held
                    requested = {r['part'] for r in allocated.get((repo,pkg), [])}
                    occupied = held(c).get((repo,pkg), set())
                    if (occupied.intersection(requested) if dispatch_plan else occupied):
                        raise dispatch.LeaseError(f"分包仍在核验：{pkg}")
                    for table in ("package_queue","package_targets"):
                        owner=c.execute(f"SELECT user_id FROM {table} WHERE repo=? AND pkg=?",(repo,pkg)).fetchone()
                        if owner and owner[0]!=user:raise dispatch.LeaseError(f"分包已分配给其他 ID：{pkg}")
                account=dispatch._row(c,"SELECT status FROM users WHERE user_id=?",(user,))
                if account and account["status"]!="active":raise dispatch.AuthError("用户已停用")
                if not account:
                    c.execute("INSERT INTO users(user_id,secret,created_at,workspace_id) VALUES(?,?,?,?)",(user,dispatch.hash_secret(credential),time.time(),ws["ws_id"]))
                c.execute("INSERT INTO devices(device_id,user_id,first_seen,status,workspace_id,parts) VALUES(?,?,?,'offline',?,?)",(device,user,time.time(),ws["ws_id"],cfg["mode"]))
                c.execute("INSERT INTO offline_jobs(id,user_id,device_id,secret_hash,status,created_at) VALUES(?,?,?,?,'ready',?)",(job,user,device,dispatch.hash_secret(credential),time.time()))
                for repo,pkg in items:
                    package=dispatch._row(c,"SELECT * FROM packages WHERE repo=? AND id=?",(repo,pkg))
                    requested = [r['part'] for r in allocated[(repo,pkg)]] if dispatch_plan else [p for p in dispatch.missing_parts(package) if cfg['mode'] == 'both' or p == cfg['mode']]
                    if not requested:
                        raise dispatch.LeaseError('所选模式不能处理分包缺项')
                    for target in c.execute('SELECT * FROM part_assignments WHERE repo=? AND pkg=?', (repo, pkg)):
                        target_matches = target['target_id'] == (online_device['device_id'] if dispatch_plan and target['target_type'] == 'device' else user) and (dispatch_plan or target['target_type'] == 'user')
                        if target['part'] in requested and not target_matches:
                            raise dispatch.LeaseError('分项已分配给其他用户或设备')
                    package['dispatch_parts'] = requested
                    attempt=c.execute("SELECT COUNT(*) FROM leases WHERE repo=? AND pkg=?",(repo,pkg)).fetchone()[0]+1
                    lease=dispatch._open_lease(c,device,package,"offline",attempt)
                    if annotation is not None:
                        selected = {**json.loads(lease.get('annotation_json') or '{}'),
                                    **{FIELDS[p]: annotation[FIELDS[p]] for p in requested if FIELDS[p] in annotation}}
                        lease["annotation_json"] = json.dumps(selected)
                        c.execute("UPDATE leases SET annotation_json=? WHERE lease_id=?",
                                  (lease["annotation_json"], lease["lease_id"]))
                    c.execute("UPDATE packages SET status='claimed',owner=? WHERE repo=? AND id=?",(user,repo,pkg))
                    c.execute("DELETE FROM package_queue WHERE repo=? AND pkg=?",(repo,pkg))
                    c.execute("INSERT INTO offline_leases VALUES(?,?)",(lease["lease_id"],job))
                    entry=dispatch._lease_view(c,lease)
                    if entry.get('annotation',{}).get('artist_table'):
                        table=artist_tables[(repo,pkg)]
                        if not table or table['source']!=entry['annotation']['artist_table']:
                            raise ValueError('画师表配置已变化，请重新导出')
                    if dispatch_plan:
                        task_config = contracts.config(allocated[(repo,pkg)][0]['config'])
                        # 单个分片两侧来自不同执行环境时分别导出，不能静默套用第一侧。
                        if any(contracts.config(r['config']) != task_config for r in allocated[(repo,pkg)]):
                            raise ValueError('同一分片的 TAG／NL 配置不同，请分别应用对应设备分派')
                        task_config['mode'] = 'both' if len(requested)==2 else requested[0]
                        entry['config'] = contracts.config(task_config)
                    entry["missing_parts"]=[part for part in entry["missing_parts"] if part in requested]
                    if not entry["missing_parts"]:raise dispatch.LeaseError("所选模式不能处理分包缺项")
                    leases.append(entry)
                store.event(c,"offline_export",None,None,actor,f"job={job};user={user};packages={len(leases)}")
                data={"format":"lulynx-offline-task","version":1,"id":job,"server":server.rstrip("/"),
                      "identity":{"user_id":user,"device_id":device,"secret":credential},"config":cfg,"tasks":leases}
                if dispatch_plan:
                    data['dispatch_plan_id'] = dispatch_plan['id']
                    c.execute('DELETE FROM device_dispatch_plans WHERE device_id=?', (online_device['device_id'],))
                    c.execute('INSERT INTO dispatch_plan_applications VALUES(?,?,?,?)', (dispatch_plan['id'],online_device['device_id'],'offline',job))
                # 写盘失败会回滚租约，无法重新下载的清单不占用分包。
                import os
                root.mkdir(parents=True,exist_ok=False)
                offline_document.write(root / 'task.json', data, artist_tables)
        finally:c.close()
    return {"ok":True,"id":job,"manifest":offline_document.read(root / 'task.json')}


def manifest(store,job):
    if not isinstance(job,str) or not job.startswith("O") or len(job)!=17 or any(ch not in "0123456789abcdef" for ch in job[1:]):
        raise ValueError("离线任务 ID 不正确")
    c=store.conn()
    try:row=c.execute("SELECT status FROM offline_jobs WHERE id=?",(job,)).fetchone()
    finally:c.close()
    if not row or row[0]=="canceled":raise ValueError("离线任务不存在或已取消")
    path=store.dir/"offline"/job/"task.json"
    from .offline_document import read
    return read(path)


def cancel(store,dispatch,job,actor):
    if not isinstance(job,str):raise ValueError("离线任务 ID 不正确")
    with store._write_lock:
        c=store.conn()
        try:
            with dispatch._Tx(c):
                row=c.execute("SELECT device_id FROM offline_jobs WHERE id=?",(job,)).fetchone()
                if not row:raise ValueError("离线任务不存在")
                for lease in c.execute("SELECT * FROM leases WHERE device_id=? AND status='open'",(row[0],)).fetchall():
                    c.execute("UPDATE leases SET status='released',closed_reason='offline_canceled' WHERE lease_id=?",(lease["lease_id"],))
                    c.execute("UPDATE packages SET status='available',owner=NULL WHERE repo=? AND id=?",(lease["repo"],lease["pkg"]))
                c.execute("UPDATE offline_jobs SET status='canceled' WHERE id=?",(job,))
                store.event(c,"offline_cancel",None,None,actor,f"job={job}")
        finally:c.close()
    return {"ok":True}
