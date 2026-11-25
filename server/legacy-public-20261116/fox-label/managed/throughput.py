"""汇总最近仍在执行的分片速度；只读现有租约和托管进度。"""
import json
import math
import time

from .store import ONLINE_SECONDS

PROGRESS_SECONDS = 120


def number(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None


def summarize(rows, now):
    rates={'rate_per_s':0.0,'tag_rate_per_s':0.0,'nl_rate_per_s':0.0}
    active=reporting=stale=0
    seen=set()
    for row in rows:
        if row['device_id'] in seen:continue
        seen.add(row['device_id'])
        beat=number(row['last_beat_at']) or row['issued_at']
        if now-beat>PROGRESS_SECONDS or row['expires_at']<=now:continue
        state=json.loads(row['state'] or '{}')
        session_current=(row['session_seen'] is not None and now-row['session_seen']<=ONLINE_SECONDS)
        if session_current and (row['released'] or state.get('running') is not True or state.get('preparing') is True):continue
        active+=1
        progress=json.loads(row['progress_json'] or '{}')
        managed=state.get('progress')
        if session_current and isinstance(managed,dict) and managed.get('lease_id')==row['lease_id']:progress=managed
        if progress.get('finished') is True:continue
        age=number(progress.get('updated_ago_s'))
        if session_current and progress is managed and age is not None:age+=max(0,now-row['session_seen'])
        else:
            updated=number(progress.get('updated_at'))
            age=max(0,now-updated) if updated is not None else None
        if age is None or age>PROGRESS_SECONDS:
            stale+=1;continue
        rate=number(progress.get('rate_per_s'))
        if rate is None:continue
        reporting+=1;rates['rate_per_s']+=rate
        for part in ('tag','nl'):
            details=progress.get(part)
            part_rate=number(details.get('rate_per_s')) if isinstance(details,dict) else None
            if part_rate is not None:rates[part+'_rate_per_s']+=part_rate
            elif progress.get('phase')==part:rates[part+'_rate_per_s']+=rate
    return dict(ok=True,**{k:round(v,3) for k,v in rates.items()},active_devices=active,reporting_devices=reporting,
                unreported_devices=active-reporting,stale_devices=stale,sampled_at=now,
                progress_timeout_s=PROGRESS_SECONDS,basis='最近上报的分片内平均有效速度；总速度按图片结算，不重复累加 TAG 与 NL')


def view(store):
    c=store.conn()
    try:
        rows=c.execute("SELECT l.lease_id,l.device_id,l.issued_at,l.last_beat_at,l.expires_at,l.progress_json,"
                       " s.last_seen session_seen,s.released,s.state FROM leases l"
                       " JOIN devices d ON d.device_id=l.device_id JOIN users u ON u.user_id=d.user_id"
                       " LEFT JOIN managed_devices m ON m.device_id=d.device_id"
                       " LEFT JOIN managed_sessions s ON s.session_id=m.session_id AND s.user_id=d.user_id"
                       " WHERE l.kind='active' AND l.status='open' AND u.status='active'"
                       " AND d.status NOT IN ('archived','blocked') ORDER BY l.issued_at DESC")
        return summarize(rows,time.time())
    finally:c.close()
