"""固定调度工具只适配已有管理员 API，不开放任意路径或命令。"""
import json
import re
from urllib.parse import urlencode


def schema(properties=None, required=()):
    return dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False)


TEXT = dict(type='string', minLength=1, maxLength=128)
ITEMS = dict(type='array', minItems=1, maxItems=500,
             items=schema(dict(repo=TEXT, id=TEXT), ('repo', 'id')))
CONFIG = schema(dict(concurrency=dict(type='integer', minimum=0, maximum=256),
                     tag_batch=dict(type='integer', minimum=0, maximum=128)))
EXECUTION = schema({**CONFIG['properties'],
    'tag_backend': dict(type='string', enum=['wd-eva02', 'pixai', 'cl-tagger-v2', 'onnx', 'none']),
    'tag_precision': dict(type='string', enum=['tf32', 'fp32', 'bf16', 'fp16']),
    'inference_source': dict(type='string', enum=['local', 'existing']),
    'nl_engine': dict(type='string', enum=['llamacpp', 'native', 'vllm']),
    'nl_model': dict(type='string', maxLength=256),
    'nl_precision': dict(type='string', enum=['f16', 'fp16', 'bf16', 'fp8', 'int8', 'w4a16', 'q4', 'q5', 'q6', 'q8'])})
ANNOTATION = schema(dict(artist_inject=dict(type='boolean'), artist_inject_tags=dict(type='boolean')))
PARTS = dict(type='string', enum=['tag', 'nl', 'both'])
TEMPLATE = schema(dict(mode=PARTS, config_source=dict(type='string', enum=['inherit', 'specified']),
                       config=EXECUTION, annotation=ANNOTATION), ('mode', 'config_source'))
LEASE_IDS = dict(type='array', minItems=1, maxItems=100, items=dict(type='string', minLength=1, maxLength=64))
REASON = dict(type='string', minLength=1, maxLength=200)
WRITE = {'center_preassign', 'center_device_command', 'center_offline_export', 'center_offline_delete',
         'center_dispatch_control', 'center_release_preview', 'center_release', 'center_cancel_preassignment',
         'center_workspace_template'}


def tool(name, description, input_schema):
    return dict(name=name, description=description, inputSchema=input_schema,
                annotations=dict(readOnlyHint=name not in WRITE or name=='center_release_preview',
                                 destructiveHint=name in ('center_offline_delete','center_release'),
                                 idempotentHint=name not in WRITE or name in ('center_device_command','center_offline_delete','center_release','center_workspace_template'),
                                 openWorldHint=False))


TOOLS = [
    tool('center_overview', '查询总速度、在线设备数量和仓库分包统计。', schema()),
    tool('center_workspaces', '查询工作区、成员归属、仓库统计及 preassignment_template 模型与量化默认要求，不含登录密钥。', schema()),
    tool('center_workspace_template', '保存或清除工作区预派单默认模板；先用 center_workspaces 读取。save 用 template 完整替换，指定模型、引擎、量化、并发建议和画师注入；inherit 沿用成员配置。仅影响后续预派单，不改已派任务或本机配置。clear 不带 template。',
         schema(dict(ws_id=TEXT, action=dict(type='string', enum=['save', 'clear']), template=TEMPLATE), ('ws_id', 'action'))),
    tool('center_devices', '查询设备运行、并发权限、速度、显存、租约及等待原因。', schema(dict(device_id=TEXT))),
    tool('center_packages', '分页查询工作区分片，查看缺项、占用、预派和可派分项。',
         schema(dict(ws_id=TEXT, offset=dict(type='integer',minimum=0)), ('ws_id',))),
    tool('center_offline_jobs', '分页查询已有离线工单，不返回工单身份密钥。',
         schema(dict(ws_id=TEXT,offset=dict(type='integer',minimum=0)))),
    tool('center_dispatch_state', '查询在线自动派单运行状态及当前包、预锁、离线租约数量。', schema()),
    tool('center_scheduling', '查询完整调度配置：成员、分组、设备仓库范围、用户／设备分项指定、租约和自动派单状态。不返回登录密钥。', schema()),
    tool('center_dispatch_control', '启动或暂停在线自动派单。暂停阻止新包、预取及预锁升级，保留当前包和离线工单；启动保留个别设备暂停。',
         schema(dict(running=dict(type='boolean')), ('running',))),
    tool('center_preassign', '按用户预派未完成分项；items 必须来自最新可派列表。config 支持模型、引擎、量化及并发建议，annotation 设置画师注入；省略时沿用工作区默认模板，显式空对象取消对应模板设置。并发不同不阻断接单，模型与量化仍须匹配。',
         schema(dict(ws_id=TEXT, users=dict(type='array',minItems=1,maxItems=100,items=TEXT), items=ITEMS,
                     mode=PARTS,config=EXECUTION,annotation=ANNOTATION,assign_workspace=dict(type='boolean')),
                ('ws_id','users','items','mode'))),
    tool('center_cancel_preassignment', '撤销所选分片尚未领取的用户预派，可按 TAG／NL／两项取消，复用网页撤销规则。先查询最新 center_packages；正在执行或预锁的分项不能撤销，应走租约释放。撤销不暂停自动派单、不清除设备队列、离线工单或仓库权限；自动派单运行时可再次分配。',
         schema(dict(ws_id=TEXT, items=ITEMS, mode=PARTS), ('ws_id','items','mode'))),
    tool('center_device_command', '向设备下发开始／强制继续／停止／并发调整／探测。stop支持离线设备并保存停工意图，重连后执行，保留租约；其它操作须在线。先查询当前 session_id；request_id 用唯一编号，重试沿用。configure 须本机授权，模型与量化由本机管理。',
         schema(dict(device_id=TEXT,session_id=TEXT,request_id=TEXT,
                     action=dict(type='string',enum=['start','stop','configure','probe']),config=CONFIG,
                     force_continue=dict(type='boolean')),('device_id','session_id','request_id','action'))),
    tool('center_offline_export', '为成员登记并占用离线工单，返回工单ID。管理员在网页下载清单，AI不接触离线凭据。',
         schema(dict(ws_id=TEXT,user_id=TEXT,items=ITEMS,mode=PARTS,config=EXECUTION,annotation=ANNOTATION),
                ('ws_id','user_id','items','mode'))),
    tool('center_offline_delete', '删除指定已有离线工单并撤销未完成的占用；已回传结果和审计保留。删除前告知成员，离线客户端无法实时收到撤销。',
         schema(dict(id=TEXT),('id',))),
    tool('center_release_preview', '预览指定租约释放影响，支持离线设备。返回confirmation和实际数量，向管理员说明后再调用center_release。',
         schema(dict(lease_ids=LEASE_IDS, reason=REASON), ('lease_ids',))),
    tool('center_release', '按已确认的预览释放租约并暂停原设备；保留已回传结果。使用相同lease_ids/reason及confirmation、confirmed_count；唯一request_id重试沿用。',
         schema(dict(lease_ids=LEASE_IDS, reason=REASON, confirmation=dict(type='string',minLength=1,maxLength=4096),
                     confirmed_count=dict(type='integer',minimum=0,maximum=100), request_id=TEXT),
                ('lease_ids','confirmation','confirmed_count','request_id'))),
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
        return dict(health=up.get('/health',cache=False),speed=up.get('/admin/v1/managed/devices?summary=throughput',cache=False),
                    packages=up.summary().get('summary',{}))
    if name=='center_workspaces':return clean(up.get('/admin/v1/workspaces',cache=False))
    if name=='center_devices':return clean(up.get('/admin/v1/managed/devices?'+urlencode(args),cache=False))
    if name=='center_packages':return clean(up.get('/admin/v1/workspace-packages?'+urlencode(args),cache=False))
    if name=='center_offline_jobs':return clean(up.get('/admin/v1/offline-tasks?'+urlencode(args),cache=False))
    if name=='center_dispatch_state':return clean(up.get('/admin/v1/scheduling?summary=automation',cache=False))
    if name=='center_scheduling':return clean(up.get('/admin/v1/scheduling',cache=False))
    headers={'X-Fox-Actor':f"{identity['owner']}:mcp:{identity['id']}",
             'X-Fox-Session':'mcp-'+identity['id'],'X-Fox-Client-IP':handler._ip(),'X-Fox-Panel-Version':VERSION}
    if name in ('center_release_preview','center_release'):
        if len(set(args['lease_ids'])) != len(args['lease_ids']):raise ValueError('租约列表不能重复')
        from .releases import call as release
        return release(up, identity, name, args, headers)
    body=dict(args)
    if name=='center_dispatch_control':
        body['action']='automation'
        path='scheduling'
    elif name=='center_workspace_template':
        if (body['action'] == 'save') != ('template' in body):
            raise ValueError('save 必须携带 template，clear 不能携带 template')
        body['action'] = 'save_preassignment_template' if body['action'] == 'save' else 'clear_preassignment_template'
        path = 'workspace-dispatch'
    elif name=='center_device_command':
        body['id']='mcp-'+identity['id']+'-'+body.pop('request_id')
        if len(body['id'])>128:raise ValueError('request_id 最多允许106字')
        path='managed/commands'
    elif name=='center_preassign':
        path='workspace-dispatch'
        body['start']=False
    elif name=='center_cancel_preassignment':
        path='workspace-dispatch'
        body['action']='cancel_preassignment'
    elif name=='center_offline_delete':path='offline-delete'
    else:
        path='offline-export'
        cfg = body.get('config', {})
        engine = cfg.get('nl_engine', 'native')
        precision = 'q6' if engine == 'native' or cfg.get('nl_model') == 'torii-gate-0.5' else 'f16' if engine == 'llamacpp' else 'bf16'
        body['config']={'tag_backend':'pixai','nl_engine':engine,'nl_precision':precision,**cfg,'mode':body.pop('mode')}
        body['server']=handler._expected_origin()
    status,raw,_=up.post('/admin/v1/'+path,body,headers,timeout=180 if path=='offline-export' else 30)
    result=json.loads(raw)
    if status>=400 or result.get('ok') is False:
        raise ValueError(result.get('error') or f'调度服务返回 {status}')
    up.invalidate()
    if name=='center_offline_export':
        return dict(ok=True,id=result['id'],note='已登记，管理员可在离线工单网页下载清单')
    if name=='center_cancel_preassignment':
        return {**clean(result), 'note':'已撤销尚未领取的用户预派；自动派单状态保持不变，分片仍可能按现有规则自动分配。'}
    return clean(result)
