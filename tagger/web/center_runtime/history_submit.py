"""一个按钮顺序核对已有产物；只创建核验会话，不启动打标。"""
from .managed import ManagedSession


def execute(history, *, submit, reinject='', candidate_id=''):
    from ...fox_worker import api
    from .history_check import execute as check
    from .history_discovery import discover
    center = history.center
    session = None
    owned = False
    state = {'running': False, 'history_running': True, 'history_only': True, 'status': 'checking'}
    errors = []
    try:
        history.phase('discovering', '自动识别本地历史产物')
        discover(history.inventory)
        if history.cancel.is_set():
            history.phase('idle', '历史核对已停止，已完成结果保留')
            return
        if not submit:
            history.phase('idle', '本地历史产物识别完成')
            return
        items = [r for r in history.snapshot()['items'] if r['local_status'] == 'ready'
                 and (not candidate_id or r['id']==candidate_id)
                 and (reinject or r['status'] not in ('registered', 'verifying', 'incomplete'))]
        if not items:
            history.phase('idle', '没有新增待上报产物；来源不明的结果可在下方补充关联')
            return
        session = center.runtime.session
        owned = session is None or session.closed
        if owned:
            session = ManagedSession(center, history_only=True)
        session.poll(state, {}, inventory=[])
        for index, item in enumerate(items, 1):
            if history.cancel.is_set() or session.closed:
                break
            task = None
            try:
                history.phase('checking', f'核对并上报 {index}/{len(items)}：{item["package"]}')
                session.poll(state, {}, inventory=[{k: item[k] for k in ('id','repo','package','fingerprint')}
                                                   | {'status': item['local_status']}])
                result = api(session.identity, '/api/v1/history/begin',
                             {'session_id': session.id, 'candidate_id': item['id'],'reinject':reinject}, timeout=120 if reinject else 20, retries=0)
                task = result['task']
                check(history, {'config': task}, session, history.cancel)
            except Exception as exc:
                message = center.credentials.redact(str(exc))
                current = next((r for r in history.inventory.scan() if r['id']==item['id']), item)
                history.report(item['id'], {**getattr(exc,'report',{}), 'error': message}, 'failed', current['fingerprint'])
                errors.append(item['package'] + '：' + message)
                if task:
                    try:
                        api(session.identity, '/api/v1/history/cancel',
                            {'session_id': session.id, 'check_id': task['check_id']}, timeout=10, retries=1)
                    except Exception:
                        pass
        if history.cancel.is_set() or session.closed:
            history.phase('idle', '历史核对已停止，已完成结果保留')
            return
        session.poll(state, {}, inventory=[])
        history.phase('failed' if errors else 'completed',
                      '；'.join(errors) if errors else '核对并上报完成，缺项分片已保留成功结果并等待续做')
    except Exception as exc:
        history.phase('failed', center.credentials.redact(str(exc)))
    finally:
        if owned and session:
            try:
                with session.lock:
                    session.closed = True
                    session._persist()
                session.poll(state, {}, released=True, inventory=[])
            except Exception:
                pass
        history.running = False
