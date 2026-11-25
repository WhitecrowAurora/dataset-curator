"""预派吞吐建议与实际配置；模型要求单独匹配。"""
import json

FIELDS={'tag':'tag_batch','nl':'concurrency'}


def permissions(caps):
    return caps.get('remote_concurrency',{})


def sources(c,session,device,package):
    cfg=json.loads(session['config'])
    resolved=json.loads(session['state']).get('resolved', {})
    expected=required(c,device,package)
    result={}
    for part,key in FIELDS.items():
        if cfg['mode'] not in ('both',part) or part=='tag' and cfg['tag_backend']=='none':
            continue
        row=c.execute("SELECT actor FROM managed_commands WHERE session_id=? AND action='configure'"
                      " AND status='applied' AND json_type(config,?) IS NOT NULL ORDER BY seq DESC LIMIT 1",
                      (session['session_id'],'$.'+key)).fetchone()
        result[key]={'value':cfg[key], 'source':'预派单' if key in expected else
                     ('预派单（上次调整）' if row['actor']=='preassignment' else '管理员远程调整') if row else '本机配置'}
        warnings = []
        if key in expected and expected[key] != cfg[key]:
            result[key]['source'] = '本机配置（预派建议尚未应用）'
            warnings.append(f'预派建议 {expected[key]}，当前配置 {cfg[key] or "自动"}；不影响接单')
        actual = resolved.get(key)
        if cfg[key] and actual is not None and actual != cfg[key]:
            warnings.append(f'配置 {cfg[key]}，本机实际使用 {actual}；按本机能力执行')
        result[key]['warning'] = '；'.join(warnings)
    return result


def required(c,device,package):
    if not package:
        return {}
    result={}
    for row in c.execute('SELECT * FROM part_assignments WHERE repo=? AND pkg=?',
                         (package['repo'],package.get('pkg',package.get('id')))):
        who=device['device_id'] if row['target_type']=='device' else device['user_id']
        if row['target_id'] != who or row['part'] not in package.get('dispatch_parts',json.loads(package.get('parts_json','["tag","nl"]'))):
            continue
        value=json.loads(row['config'] or '{}').get(FIELDS[row['part']])
        if value:
            result[FIELDS[row['part']]]=value
    return result


def next_package(c,dispatch,device):
    package=current_package(c,device['device_id'])
    if package and package['kind']=='prelock':
        from scheduling.selection import valid_prelock
        if not valid_prelock(c,dispatch,device,package,allow_tuning=True):
            c.execute("UPDATE leases SET status='released',closed_reason='policy_changed' WHERE lease_id=?",(package['lease_id'],))
            package=None
    return package or dispatch._pick_package(c,device,dispatch.params(),allow_tuning=True)


def current_package(c,device_id):
    row=c.execute("SELECT * FROM leases WHERE device_id=? AND status='open' ORDER BY kind='active' DESC LIMIT 1",
                  (device_id,)).fetchone()
    return dict(row) if row else None
