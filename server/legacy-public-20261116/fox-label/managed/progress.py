"""心跳进度仅更新归属正确的执行租约，不参与租约回收。"""
import json
import time


def current(state, leases):
    """实时展示必须对应仍由本设备持有的执行租约，兼容旧客户端残留上报。"""
    raw = state.get('progress')
    if state.get('running') is not True or not isinstance(raw, dict):
        return {}
    return raw if any(lease['kind'] == 'active' and
                      (lease['lease_id'], lease['repo'], lease['package']) ==
                      (raw.get('lease_id'), raw.get('repo'), raw.get('package'))
                      for lease in leases) else {}


def update(c, dispatch, device, lease_id, raw):
    lease = c.execute("SELECT kind,progress_json FROM leases WHERE lease_id=? AND device_id=? AND status='open'",
                      (lease_id, device['device_id'])).fetchone()
    if not lease or lease['kind'] != 'active':
        return
    clean = dispatch.clean_progress(raw)
    for key in ('total_known', 'finished'):
        if type(raw.get(key)) is bool:
            clean[key] = raw[key]
    old = json.loads(lease['progress_json'] or '{}')
    advanced = clean.get('done') != old.get('done') or clean.get('phase') != old.get('phase')
    c.execute("UPDATE leases SET progress_json=?,last_progress_at=CASE WHEN ? THEN ? ELSE last_progress_at END WHERE lease_id=?",
              (json.dumps(clean, ensure_ascii=False), advanced, time.time(), lease_id))
    speed = clean.get('rate_per_s')
    if isinstance(speed, (int, float)) and speed > 0:
        c.execute("UPDATE devices SET speed=?,tier=? WHERE device_id=?", (speed, dispatch._tier_for(speed), device['device_id']))
