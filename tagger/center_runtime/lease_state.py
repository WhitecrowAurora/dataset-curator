"""本机执行心跳的观测记录；只在请求成功后更新成功时刻。"""
import json
import os
import time
from pathlib import Path

from ..storage.atomic import write_json


def publish(leases, value):
    root = os.environ.get('FOXLABEL_HEARTBEAT_DIR')
    if not root or not any(l['kind'] == 'active' for l in leases):
        return
    for lease in leases:
        try:
            write_json(Path(root) / (lease['lease_id'] + '.json'), value)
        except OSError:
            # 观测记录写失败不能中断实际租约上报。
            pass


def snapshot(center, leases):
    now = time.time()
    items = {}
    for lease in leases:
        try:
            value = json.loads((center.dir / 'heartbeats' / (lease['lease_id'] + '.json')).read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                continue
            items[lease['lease_id']] = {**value,
                'last_success_ago': max(0, now - value['last_success_at']) if value.get('last_success_at') else None,
                'next_in': max(0, value['next_at'] - now) if center.running and value.get('next_at') else None,
                'running': center.running}
        except (OSError, ValueError, TypeError, KeyError):
            continue
    session = center.runtime.session
    if center.runtime.active and session and not session.closed:
        for lease in leases:
            if lease['kind'] not in ('active', 'prelock'):
                continue
            items[lease['lease_id']] = {
                'last_success_ago': max(0, now - session.last_success_at) if session.last_success_at else None,
                'next_in': max(0, session.next_poll - now), 'running': True,
                'in_flight': center.runtime._polling, 'error': session.error}
    return {'sampled_at': now, 'leases': items}
