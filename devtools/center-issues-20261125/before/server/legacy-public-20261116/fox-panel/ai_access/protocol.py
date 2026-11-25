"""MCP JSON-RPC；采用无状态 Streamable HTTP 的 JSON 响应模式。"""
import json

from .tools import TOOLS, WRITE, call

VERSIONS=('2025-06-18','2025-03-26')
INSTRUCTIONS=('lulynx 调度管理员接口。先查询设备、工作区和可派分项，再操作；'
              '完整调度配置用center_scheduling；撤销用户预派用center_cancel_preassignment，仅取消尚未领取的分项。'
              '撤销预派不暂停自动派单，若不希望再派新包，先暂停总派单。预锁或执行租约另走释放，不与撤销预派混用。'
              '启停请求的 request_id 要唯一，重试沿用。强制继续不会跳过租约、冲突或并发授权。'
              '在线自动派单由管理员启停，暂停保留当前包。离线 stop 会在重连后执行；释放租约先预览，再确认影响与实际数量。'
              '离线工单删除会撤销未完成占用，请先告知成员；不能假定已离线的成员马上停止。')


def error(request_id, code, message):
    return dict(jsonrpc='2.0',id=request_id,error=dict(code=code,message=message))


def handle(handler, store, identity, message):
    if (not isinstance(message,dict) or message.get('jsonrpc')!='2.0'
            or not isinstance(message.get('method'),str)):
        return error(None,-32600,'无效的 JSON-RPC 请求')
    if 'id' not in message:
        return None
    rid=message['id']
    if type(rid) not in (str,int) and rid is not None:
        return error(None,-32600,'请求编号不正确')
    method=message['method'];params=message.get('params',{})
    if not isinstance(params,dict):return error(rid,-32602,'params 必须为对象')
    if method=='initialize':
        version=params.get('protocolVersion')
        result=dict(protocolVersion=version if version in VERSIONS else VERSIONS[0],
                    serverInfo=dict(name='lulynx-center',version='1.1.0'),
                    capabilities=dict(tools=dict(listChanged=False)),instructions=INSTRUCTIONS)
    elif method=='ping':result={}
    elif method=='tools/list':
        result=dict(tools=[t for t in TOOLS if identity['role']=='operate' or t['name'] not in WRITE])
    elif method=='tools/call':
        name=params.get('name');args=params.get('arguments',{})
        if not isinstance(name,str) or not isinstance(args,dict):return error(rid,-32602,'工具参数格式不正确')
        success=False
        try:
            data=call(handler,identity,name,args)
            result=dict(content=[dict(type='text',text=json.dumps(data,ensure_ascii=False))],
                        structuredContent=data,isError=False)
            success=True
        except (ValueError,PermissionError) as exc:
            result=dict(content=[dict(type='text',text=str(exc))],isError=True)
        except Exception:
            result=dict(content=[dict(type='text',text='调度服务暂时不可用，请查询当前状态后重试')],isError=True)
        store.audit(identity['id'],name,args,success)
    else:return error(rid,-32601,'未知 MCP 方法')
    return dict(jsonrpc='2.0',id=rid,result=result)
