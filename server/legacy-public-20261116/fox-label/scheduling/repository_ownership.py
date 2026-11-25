"""仓库只属于一个工作区，编辑现有工作区也遵循同一规则。"""
import json


def validate(c, ws_id, repos):
    for owner in overlaps(c, ws_id, repos):
        raise ValueError('仓库已绑定工作区 ' + (owner['name'] or owner['workspace_id'])
                         + '：' + ', '.join(owner['repos']))


def overlaps(c, ws_id, repos):
    wanted = set(repos)
    result = []
    if wanted:
        for row in c.execute('SELECT ws_id,name,repos FROM workspaces WHERE ws_id<>?', (ws_id,)):
            shared = wanted.intersection(json.loads(row['repos']))
            if shared:
                result.append(dict(workspace_id=row['ws_id'], name=row['name'], repos=sorted(shared)))
    return result
