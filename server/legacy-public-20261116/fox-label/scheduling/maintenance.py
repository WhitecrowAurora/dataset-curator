"""定时释放离线预约，不回收执行任务。"""
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
                            store.event(c, 'prelock_offline', None, None, 'auto', 'released=' + ','.join(released))
                except Exception:
                    logging.exception('释放离线预锁失败')
                finally:
                    c.close()
    threading.Thread(target=run, daemon=True, name='prelock-maintenance').start()
    return stop
