"""调度管理沿用原管理员身份和写通道。"""
from . import leases, policies


def read(store, dispatch):
    with store._write_lock:
        c = store.conn()
        try:
            with dispatch._Tx(c):
                leases.release_offline(c)
                return policies.view(c, dispatch)
        finally:
            c.close()


def write(store, dispatch, body, actor):
    with store._write_lock:
        c = store.conn()
        try:
            result = policies.update(c, dispatch, body, actor)
            store.event(c, 'scheduling', None, None, actor, 'action=' + str(body.get('action')))
            return result
        finally:
            c.close()
