"""调度管理沿用原管理员身份和写通道。"""
from . import leases, policies


def read(store, dispatch, summary=None):
    if summary == 'automation':
        from .control import overview
        c = store.conn()
        try:
            return overview(c)
        finally:
            c.close()
    with store._write_lock:
        c = store.conn()
        try:
            with dispatch._Tx(c):
                leases.release_offline(c)
                return policies.view(c, dispatch)
        finally:
            c.close()


def write(store, dispatch, body, actor):
    if isinstance(body, dict) and body.get('action') == 'export_plan':
        from assignments.plans import create
        return create(store, dispatch, body, actor)
    with store._write_lock:
        c = store.conn()
        try:
            if isinstance(body, dict) and body.get('action') == 'automation':
                from .control import update, overview
                with dispatch._Tx(c):
                    result = update(c, body, actor)
                    store.event(c, 'scheduling', None, None, actor,
                                'action=automation; running=' + str(body['running']))
                    return {**overview(c), **result}
            result = policies.update(c, dispatch, body, actor)
            detail = 'action=' + str(body.get('action'))
            if body.get('action') == 'automation':
                detail += '; running=' + str(body['running'])
            store.event(c, 'scheduling', None, None, actor, detail)
            return result
        finally:
            c.close()
