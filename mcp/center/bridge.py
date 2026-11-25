"""把仅支持 stdio 的 AI 客户端连接到远程调度中心 MCP，无第三方依赖。"""
import json
import os
import sys
import urllib.error
import urllib.request


def main():
    url=os.environ.get('LULYNX_CENTER_MCP_URL','').rstrip('/')
    key=os.environ.get('LULYNX_CENTER_MCP_KEY','')
    if not url or not key:
        raise SystemExit('请设置 LULYNX_CENTER_MCP_URL 和 LULYNX_CENTER_MCP_KEY')
    version='2025-06-18'
    for line in sys.stdin.buffer:
        message=None
        try:
            message=json.loads(line.decode('utf-8-sig'))
            req=urllib.request.Request(url,data=json.dumps(message).encode(),headers={
                'Authorization':'Bearer '+key,'Content-Type':'application/json',
                'Accept':'application/json, text/event-stream','MCP-Protocol-Version':version})
            with urllib.request.urlopen(req,timeout=180) as response:
                raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:
                raise ValueError('响应过大')
            if raw:
                result=json.loads(raw)
                if message.get('method')=='initialize':version=result.get('result',{}).get('protocolVersion',version)
                sys.stdout.write(json.dumps(result,ensure_ascii=True)+'\n');sys.stdout.flush()
        except Exception:
            if isinstance(message,dict) and 'id' in message:
                sys.stdout.write(json.dumps(dict(jsonrpc='2.0',id=message['id'],error=dict(code=-32000,message='调度中心连接失败，请检查地址、密钥和网络')))+'\n');sys.stdout.flush()


if __name__=='__main__':
    main()
