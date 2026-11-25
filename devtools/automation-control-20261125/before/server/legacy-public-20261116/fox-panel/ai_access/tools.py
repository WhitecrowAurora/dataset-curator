"""固定调度工具只适配已有管理员 API，不开放任意路径或命令。"""
import json
import re
from urllib.parse import urlencode


def schema(properties=None, required=()):
    return dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False)


TEXT = dict(type='string', minLength=1, maxLength=128)
ITEMS = dict(type='array', minItems=1, maxItems=500,
             items=schema(dict(repo=TEXT, id=TEXT), ('repo', 'id')))
CONFIG = schema(dict(concurrency=dict(type='integer', minimum=0, maximum=64),
                     tag_batch=dict(type='integer', minimum=0, maximum=128)))
ANNOTATION = schema(dict(artist_inject=dict(type='boolean'), artist_inject_tags=dict(type='boolean')))
PARTS = dict(type='string', enum=['tag', 'nl', 'both'])
WRITE = {'center_preassign', 'center_device_command', 'center_offline_export', 'center_offline_delete'}


def tool(name, description, input_schema):
    return dict(name=name, description=description, inputSchema=input_schema,
                annotations=dict(readOnlyHint=name not in WRITE, destructiveHint=name=='center_offline_delete',
                                 idempotentHint=name not in WRITE or name in ('center_device_command','center_offline_delete'),
                                 openWorldHint=False))


TOOLS = [
    tool('center_overview', '查询总速度、在线设备数量和仓库分包统计。', schema()),
    tool('center_workspaces', '查询工作区、成员归属和仓库统计，不含登录密钥。', schema()),
    tool('center_devices', '查询设备运行、并发权限、速度、显存、租约及等待原因。', schema(dict(device_id=TEXT))),
    tool('center_packages', '分页查询工作区分片，查看缺项、占用、预派和可派分项。',
         schema(dict(ws_id=TEXT, offset=dict(type='integer',minimum=0)), ('ws_id',))),
    tool('center_offline_jobs', '分页查询已有离线工单，不返回工单身份密钥。',
         schema(dict(ws_id=TEXT,offset=dict(type='integer',minimum=0)))),
    tool('center_preassign', '按用户预派未完成分项；items 必须来自最新可派列表。config仅支持预派并发，annotation设置画师注入；省略时沿用工作区默认模板，显式空对象取消对应模板设置。',
         schema(dict(ws_id=TEXT, users=dict(type='array',minItems=1,maxItems=100,items=TEXT), items=ITEMS,
                     mode=PARTS,config=CONFIG,annotation=ANNOTATION,assign_workspace=dict(type='boolean')),
                ('ws_id','users','items','mode'))),
    tool('center_device_command', '向在线设备下发开始／强制继续／停止／并发调整／探测。先查询当前 session_id；request_id 用唯一编号，重试沿用。configure须本机授权且预派并发优先。',
         schema(dict(device_id=TEXT,session_id=TEXT,request_id=TEXT,
                     action=dict(type='string',enum=['start','stop','configure','probe']),config=CONFIG,
                     force_continue=dict(type='boolean')),('device_id','session_id','request_id','action'))),
    tool('center_offline_export', '为成员登记并占用离线工单，返回工单ID。管理员在网页下载清单，AI不接触离线凭据。',
         schema(dict(ws_id=TEXT,user_id=TEXT,items=ITEMS,mode=PARTS,config=CONFIG,annotation=ANNOTATION),
                ('ws_id','user_id','items','mode'))),
    tool('center_offline_delete', '删除指定已有离线工单并撤销未完成的占用；已回传结果和审计保留。删除前告知成员，离线客户端无法实时收到撤销。',
         schema(dict(id=TEXT),('id',))),
]


def validate(value, contract, label='arguments'):
    kind=contract.get('type')
    types={'object':dict,'array':list,'string':str,'integer':int,'boolean':bool}
    if kind and type(value) is not types[kind]:
        raise ValueError(label+' 格式不正确')
    if 'enum' in contract and value not in contract['enum']:
        raise ValueError(label+' 选项不正确')
    if kind=='object':
        properties=contract['properties']
        if set(value)-set(properties) or set(contract['required'])-set(value):
            raise ValueError(label+' 缺少必填项或包含未支持字段')
        for key,item in value.items():validate(item,properties[key],label+'.'+key)
    elif kind=='array':
        if not contract.get('minItems',0)<=len(value)<=contract.get('maxItems',1000):
            raise ValueError(label+' 数量不正确')
        for item in value:validate(item,contract['items'],label+'[]')
    elif kind=='string':
        if not contract.get('minLength',0)<=len(value)<=contract.get('maxLength',1000) or any(ord(ch)<32 for ch in value):
            raise ValueError(label+' 长度或字符不正确')
    elif kind=='integer' and not contract.get('minimum',0)<=value<=contract.get('maximum',1000000000):
        raise ValueError(label+' 数值不正确')


def clean(value):
    if isinstance(value, dict):
        return {k:clean(v) for k,v in value.items()
                if not any(word in k.lower() for word in ('secret','password','token','credential','path','directory','cmd_template','extra_args'))}
    if isinstance(value, list):return [clean(v) for v in value]
    if isinstance(value, str):return re.sub(r'hf_[A-Za-z0-9_]+','[已隐藏]',value)
    return value


def call(handler, identity, name, args):
    definition=next((t for t in TOOLS if t['name']==name),None)
    if definition is None:raise ValueError('未知调度工具')
    validate(args,definition['inputSchema'])
    if name in WRITE and identity['role']!='operate':raise PermissionError('此密钥仅允许查询')
    from panel import write_enabled, VERSION
    if name in WRITE and not write_enabled(handler.cfg):raise PermissionError('管理员面板处于只读模式')
    up=handler.up
    if name=='center_overview':
        return dict(health=up.get('/health',cache=False),speed=up.get('/admin/v1/managed/devices?summary=throughput',cache=False))
    if name=='center_workspaces':return clean(up.get('/admin/v1/workspaces',cache=False))
    if name=='center_devices':return clean(up.get('/admin/v1/managed/devices?'+urlencode(args),cache=False))
    if name=='center_packages':return clean(up.get('/admin/v1/workspace-packages?'+urlencode(args),cache=False))
    if name=='center_offline_jobs':return clean(up.get('/admin/v1/offline-tasks?'+urlencode(args),cache=False))
    body=dict(args)
    if name=='center_device_command':
        body['id']='mcp-'+identity['id']+'-'+body.pop('request_id')
        if len(body['id'])>128:raise ValueError('request_id 最多允许106字')
        path='managed/commands'
    elif name=='center_preassign':
        path='workspace-dispatch'
        body['start']=False
    elif name=='center_offline_delete':path='offline-delete'
    else:
        path='offline-export'
        body['config']={**body.get('config',{}),'mode':body.pop('mode'),'tag_backend':'pixai','nl_engine':'native','nl_precision':'q6'}
        body['server']=handler._expected_origin()
    headers={'X-Fox-Actor':f"{identity['owner']}:mcp:{identity['id']}",
             'X-Fox-Session':'mcp-'+identity['id'],'X-Fox-Client-IP':handler._ip(),'X-Fox-Panel-Version':VERSION}
    status,raw,_=up.post('/admin/v1/'+path,body,headers,timeout=180 if path=='offline-export' else 30)
    result=json.loads(raw)
    if status>=400 or result.get('ok') is False:
        raise ValueError(result.get('error') or f'调度服务返回 {status}')
    up.invalidate()
    if name=='center_offline_export':
        return dict(ok=True,id=result['id'],note='已登记，管理员可在离线工单网页下载清单')
    return clean(result)
