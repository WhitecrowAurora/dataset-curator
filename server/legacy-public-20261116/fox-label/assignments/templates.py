"""工作区预派单默认值；派单时取快照，不影响客户端或既有任务。"""
import json

from . import annotation
from .preassignment import requirements


def normalize(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'config_source', 'config', 'annotation'}:
        raise ValueError('预派单模板格式不正确')
    mode, source = value.get('mode', 'nl'), value.get('config_source', 'inherit')
    if mode not in ('tag', 'nl', 'both') or source not in ('inherit', 'specified'):
        raise ValueError('模板打标内容或执行配置不正确')
    configuration = requirements(value.get('config', {}), mode)
    return dict(mode=mode, config_source=source,
                config=configuration if source == 'specified' else {},
                annotation=annotation.options(value.get('annotation', {})))


def load(c, ws_id):
    row = c.execute('SELECT preassignment_template FROM workspaces WHERE ws_id=?', (ws_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(c, dispatch, body):
    value = {} if body['action'] == 'clear_preassignment_template' else normalize(body.get('template'))
    with dispatch._Tx(c):
        ws = dispatch.ws_get(c, body.get('ws_id'))
        c.execute("UPDATE workspaces SET preassignment_template=?,preassignment_last='{}' WHERE ws_id=?",
                  (json.dumps(value, ensure_ascii=False), ws['ws_id']))
    return dict(ok=True, template=value)


def defaults(c, ws_id, body):
    template = load(c, ws_id)
    result = {**{key: template[key] for key in ('mode', 'config', 'annotation') if key in template}, **body}
    if 'config_source' not in result:
        result['config_source'] = (template.get('config_source', 'inherit') if 'config' not in body
                                   else 'specified' if result.get('config') else 'inherit')
    return result


def remember(c, ws_id, mode, source, configuration, options):
    value = dict(mode=mode, config_source=source, config=configuration, annotation=options or {})
    c.execute('UPDATE workspaces SET preassignment_last=? WHERE ws_id=?',
              (json.dumps(value, ensure_ascii=False), ws_id))
    return value
