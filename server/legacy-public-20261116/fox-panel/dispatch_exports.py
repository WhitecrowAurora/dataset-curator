"""分派清单的可读 HTML 与按用户打包，内容来自服务端快照。"""
import io
import json
import html
import zipfile

LABELS = dict(inference_source='推理来源',mode='产出模式',tag_backend='TAG 模型',tag_device='TAG 设备',
              tag_precision='TAG 精度',tag_batch='TAG 批大小',nl_engine='NL 引擎',nl_model='NL 模型',
              nl_device='NL 设备',nl_precision='NL 精度',concurrency='NL 并发',gpu_mem='显存比例',
              served_name='服务模型名',auto_download='自动下载模型',auto_quantize='自动量化',stop_service='完成后停止模型',
              artist_table='画师表',artist_inject_tags='TAG 画师注入',artist_inject='NL 画师注入')


def description(value):
    def text(item):
        if type(item) is bool:return '开启' if item else '关闭'
        return '自动' if item in ('',0,'auto') else str(item)
    return '<br>'.join(html.escape(LABELS.get(k,k)+'：'+text(v)) for k,v in value.items())


def readable(plan):
    escape = lambda value: html.escape(str(value))
    settings = ''.join('<section><h2>'+escape(r['repo'])+'</h2><h3>画师设置</h3>'+description(
        dict(画师表启用=r['artist']['enabled'],画师分表目录=r['artist']['directory'],固定画师表版本=r['artist']['revision'],
             artist_inject_tags=r['artist']['artist_inject_tags'],artist_inject=r['artist']['artist_inject']))+
        '<h3>图片总表设置</h3>'+description(dict(总表核验启用=r['source']['enabled'],总表来源=r['source']['source'],
        总表路径=r['source']['path'],图片ID字段=r['source']['id_field'],分包字段=r['source']['package_field'],
        包内图片字段=r['source']['member_field'],固定版本=r['source']['revision']))+'</section>' for r in plan['repositories'])
    tasks = list(plan['tasks'])
    key = (lambda r:(r['user_id'],r['repo'],r['package'],r['part'])) if plan['sort']=='user' else (lambda r:(r['package'],r['repo'],r['user_id'],r['part']))
    tasks.sort(key=key)
    columns = [('user_id','用户'),('device_id','设备（空为该用户任意设备）'),('repo','仓库'),('package','分包'),('part','分项'),('state','导出时状态')]
    rows = ''.join('<tr>'+''.join('<td>'+escape(r.get(k,'') or '')+'</td>' for k,_ in columns)+
                   '<td>'+description(r['config'])+'</td><td>'+description(r['annotation'])+'</td></tr>' for r in tasks)
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>lulynx 分派表</title><style>body{font:14px/1.6 system-ui;margin:24px;color:#222}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px;text-align:left;vertical-align:top;word-break:break-word}pre{white-space:pre-wrap;background:#f5f5f5;padding:12px}section{break-inside:avoid}thead{display:table-header-group}td:nth-last-child(-n+2){font-size:12px;min-width:160px}</style>'
            '<h1>lulynx 分派表 · '+escape(plan['workspace']['name'] or plan['workspace']['ws_id'])+'</h1><p>分派编号 '+escape(plan['id'])+
            ' · '+str(len(tasks))+' 个分项 · 排序：'+('用户' if plan['sort']=='user' else '包名')+'</p><p>这是导出时快照；导入客户端后需确认应用，实际归属和进度以中心为准。</p>'+
            settings+'<h2>具体分派</h2><table><thead><tr>'+''.join('<th>'+escape(label)+'</th>' for _,label in columns)+
            '<th>执行配置</th><th>画师注入</th></tr></thead><tbody>'+rows+'</tbody></table></html>').encode('utf-8')


def content(plan, kind):
    if kind == 'html':
        return readable(plan), 'text/html; charset=utf-8', plan['id']+'.html'
    if kind == 'json':
        return json.dumps(plan, ensure_ascii=False, indent=2).encode('utf-8'), 'application/json; charset=utf-8', plan['id']+'.json'
    if kind != 'zip':
        raise ValueError('导出格式只支持 HTML、JSON 或按用户 ZIP')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('分派总表.html', readable(plan))
        archive.writestr('说明.txt', '每个用户目录内的 分派任务.json 可在客户端调度中心导入，或直接导入此 ZIP 并选择用户。导入后确认联网／离线应用，不自动开始打标。\n')
        for index, user in enumerate(sorted({r['user_id'] for r in plan['tasks']}), 1):
            # 文件名使用序号，用户 ID 保留在表内，避免同名、Windows 非法字符及路径穿越。
            subset = {**plan, 'recipient':user, 'tasks':[r for r in plan['tasks'] if r['user_id']==user]}
            prefix = f'用户-{index:03d}/'
            archive.writestr(prefix+'分派表.html', readable(subset))
            archive.writestr(prefix+'分派任务.json', json.dumps(subset, ensure_ascii=False, indent=2))
    return stream.getvalue(), 'application/zip', plan['id']+'-users.zip'


def download(handler, query):
    from urllib.parse import urlencode
    plan_id = (query.get('id') or [''])[0]
    plan = handler.up.get('/admin/v1/dispatch-plan?'+urlencode({'id':plan_id}), cache=False)
    data, mime, filename = content(plan, (query.get('format') or ['html'])[0])
    return handler._send(200, data, mime, {'Content-Disposition':'attachment; filename="'+filename+'"','Cache-Control':'no-store'})
