"""MCP 释放操作复用管理员预览、确认和幂等执行通道。"""
import hashlib
import json


def call(up, identity, name, args, headers):
    body = dict(action='lease.release', targets=[dict(lease_id=value) for value in args['lease_ids']],
                params=dict(reason=args.get('reason') or 'admin_release'))
    if name == 'center_release_preview':
        status, raw, _ = up.post('/admin/v1/preview', body, headers)
        result = json.loads(raw)
        if status >= 400 or result.get('ok') is False:
            raise ValueError(result.get('error') or f'调度服务返回 {status}')
        preview = result['preview']
        from .tools import clean
        return dict(ok=True, lease_ids=args['lease_ids'], reason=preview['params']['reason'],
                    impact=clean(preview['impact']), confirmation=preview['confirm_token'],
                    expires_at=preview['expires_at'], confirmed_count=preview['impact']['mutations'],
                    note='确认影响和数量后调用 center_release；释放会暂停原设备并保留已回传结果')
    request = identity['id'] + ':' + args['request_id']
    headers = {**headers, 'Idempotency-Key': 'mcp-release-' + hashlib.sha256(request.encode()).hexdigest()[:32]}
    body.update(confirm_token=args['confirmation'], batch_confirm=dict(mutations=args['confirmed_count']))
    status, raw, _ = up.post('/admin/v1/execute', body, headers)
    result = json.loads(raw)
    if status >= 400 or result.get('ok') is False:
        raise ValueError(result.get('error') or f'调度服务返回 {status}')
    up.invalidate()
    from .tools import clean
    return clean(result)
