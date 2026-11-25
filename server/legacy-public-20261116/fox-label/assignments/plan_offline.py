"""把已确认的分派转为离线租约，沿用原离线产物回传。"""
from .offline import create


def apply(store, dispatch, body, plan, device, tasks):
    from managed import contracts
    parts={task['part'] for task in tasks}
    config={**tasks[0]['config'], 'mode':'both' if len(parts)==2 else next(iter(parts))}
    result = create(store, dispatch, contracts,
                    dict(ws_id=plan['workspace']['ws_id'], user_id=device['user_id'], server=plan['server'],
                         config=config, items=[dict(repo=repo, id=pkg) for repo, pkg in
                         dict.fromkeys((r['repo'], r['package']) for r in tasks)]),
                    device['user_id'], dispatch_plan=plan, online_identity=body)
    return dict(ok=True, mode='offline', manifest=result['manifest'], replay=result.get('replay', False))
