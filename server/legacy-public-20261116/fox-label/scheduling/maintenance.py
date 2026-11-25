"""定时回收连续 48 小时未成功上报的在线租约。"""
import logging
import threading

from .leases import release_offline


def start(store, dispatch):
    stop = threading.Event()
    def run():
        while not stop.wait(30):
            with store._write_lock:
                c = store.conn()
                try:
                    with dispatch._Tx(c):
                        released = release_offline(c)
                        if released:
                            store.event(c, 'lease_expired', None, None, 'auto', 'released=' + ','.join(released))
                except Exception:
                    logging.exception('回收过期在线租约失败')
                finally:
                    c.close()
    threading.Thread(target=run, daemon=True, name='lease-maintenance').start()
    return stop
