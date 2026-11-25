import { useEffect, useState } from 'react'
import { json } from '../../lib/api'
import { SectionPanel } from '../ui/launcher'
import { FolderPicker } from '../FolderPicker'

type Package = {repo:string;pkg:string;count:number;reviewed:number;tag:number;nl:number;failed:number;conflicts:number;size:number;cached:boolean;status:string;message:string;done:number;total:number}
type Session = {id:string;status:string;message:string;records:number;packages:Package[]}
type Item = {image_id:string;payload:Record<string,unknown>;review:string;edited:{tag?:string;nl?:string}|null;image_available:boolean}
function text(raw:unknown,part:'tag'|'nl') {try{const value=typeof raw==='string'?JSON.parse(raw):raw;return typeof value==='object'&&value?String((value as Record<string,unknown>)[part==='tag'?'tag':'caption']??(value as Record<string,unknown>).nl??''):String(raw??'')}catch{return String(raw??'')}}
function size(n:number){return n?(n/1024**3).toFixed(2)+' GB':'大小待查询'}
const base='/api/result-review'

export function ResultReviewPanel(){
  const [sessions,setSessions]=useState<{id:string;created:number}[]>([]),[sid,setSid]=useState(''),[state,setState]=useState<Session|null>(null)
  const [source,setSource]=useState(''),[repo,setRepo]=useState(''),[index,setIndex]=useState(''),[idField,setIdField]=useState('image_id'),[packageField,setPackageField]=useState('shard'),[memberField,setMemberField]=useState('file')
  const [selected,setSelected]=useState<Set<string>>(new Set()),[active,setActive]=useState<Package|null>(null),[items,setItems]=useState<Item[]>([]),[offset,setOffset]=useState(0),[total,setTotal]=useState(0)
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[picker,setPicker]=useState<'source'|'index'|null>(null)
  useEffect(()=>{if(!active||!sid)return;let live=true;const timer=setInterval(()=>{void json<{items:Item[];total:number}>(base+'/'+sid+'/records?'+new URLSearchParams({repo:active.repo,pkg:active.pkg,offset:String(offset)})).then(value=>{if(live)setItems(value.items)}).catch(e=>{if(live)setError(String(e))})},3000);return()=>{live=false;clearInterval(timer)}},[sid,active?.repo,active?.pkg,offset])
  const key=(p:Package)=>JSON.stringify([p.repo,p.pkg])
  async function load(){setSessions((await json<{items:{id:string;created:number}[]}>(base)).items)}
  async function act(fn:()=>Promise<unknown>){setBusy(true);setError('');try{await fn()}catch(e){setError(String(e))}finally{setBusy(false)}}
  useEffect(()=>{void load().catch(e=>setError(String(e)))},[])
  useEffect(()=>{if(!sid)return;setSelected(new Set());setActive(null);let live=true;async function refresh(){try{const value=await json<Session>(base+'/'+sid);if(live)setState(value)}catch(e){if(live)setError(String(e))}}void refresh();const timer=setInterval(()=>void refresh(),3000);return()=>{live=false;clearInterval(timer)}},[sid])
  async function page(p:Package,next=0){const value=await json<{items:Item[];total:number}>(base+'/'+sid+'/records?'+new URLSearchParams({repo:p.repo,pkg:p.pkg,offset:String(next)}));setActive(p);setItems(value.items);setOffset(next);setTotal(value.total)}
  async function decide(item:Item,review:string,edited?:{tag:string;nl:string}){await json(base+'/'+sid+'/decision',{method:'POST',body:JSON.stringify({repo:active?.repo,pkg:active?.pkg,image_id:item.image_id,review,edited})});if(active)await page(active,offset)}
  const packages=state?.packages??[],chosen=packages.filter(p=>selected.has(key(p)))
  return <SectionPanel title="导入中心结果与图片核验">
    <p className="mb-3 text-xs text-lx-muted">导入管理员分享链接或结果文件，先查看分包分布，再勾选下载原图包。读取和核验不会启动模型，原结果保留。</p>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="text-xs text-lx-muted">结果链接／本地文件<input className="lx-input mt-1" value={source} onChange={e=>setSource(e.target.value)} placeholder="粘贴外部结果链接，或选择 ZIP / JSONL / Parquet" /></label>
      <label className="text-xs text-lx-muted">源仓库（中心 ZIP 可留空）<input className="lx-input mt-1" value={repo} onChange={e=>setRepo(e.target.value)} placeholder="org/dataset" /></label>
    </div>
    <div className="my-3 flex flex-wrap gap-2"><button className="lx-btn-ghost" onClick={()=>setPicker('source')}>选择结果文件</button><button className="lx-btn-primary" disabled={busy||!source} onClick={()=>void act(async()=>{const value=await json<Session>(base,{method:'POST',body:JSON.stringify({source,repo})});setSid(value.id);setState(value);await load()})}>导入结果</button>
      <select className="lx-input max-w-sm" aria-label="已有核验任务" value={sid} onChange={e=>setSid(e.target.value)}><option value="">选择已有核验任务</option>{sessions.map(s=><option key={s.id} value={s.id}>{new Date(s.created*1000).toLocaleString()} · {s.id.slice(0,8)}</option>)}</select></div>
    {error&&<p role="alert" className="text-xs text-lx-warning">{error}</p>}
    {state&&<><p role="status" className="mb-3 text-xs text-lx-muted">{state.message} · {state.records??0} 条记录</p>
      <details className="mb-3"><summary className="cursor-pointer text-xs text-lx-muted">总表定位／校验分包关联</summary><p className="my-2 text-xs text-lx-muted">中心结果自带分包来源。无分包信息时，用总表匹配；无法唯一匹配的结果保留在“未定位”中。</p>
        <div className="grid gap-2 sm:grid-cols-2"><input aria-label="总表位置" className="lx-input" value={index} onChange={e=>setIndex(e.target.value)} placeholder="总表路径或 hf://datasets/org/repo/manifest.parquet" />
          <input aria-label="总表仓库" className="lx-input" value={repo} onChange={e=>setRepo(e.target.value)} placeholder="源仓库 org/dataset" />
          <input aria-label="图片 ID 字段" className="lx-input" value={idField} onChange={e=>setIdField(e.target.value)} placeholder="图片 ID 字段"/><input aria-label="分包字段" className="lx-input" value={packageField} onChange={e=>setPackageField(e.target.value)} placeholder="分包字段"/><input aria-label="成员路径字段" className="lx-input" value={memberField} onChange={e=>setMemberField(e.target.value)} placeholder="成员路径字段"/></div>
        <div className="mt-2 flex gap-2"><button className="lx-btn-ghost" onClick={()=>setPicker('index')}>选择总表</button><button className="lx-btn-ghost" disabled={busy||!repo} onClick={()=>void act(()=>json(base+'/'+sid+'/index',{method:'POST',body:JSON.stringify({path:index,repo,id_field:idField,package_field:packageField,member_field:memberField})}))}>索引并定位（留空自动找总表）</button></div>
      </details>
      <div className="my-3 flex flex-wrap items-center gap-2"><button className="lx-btn-ghost" onClick={()=>setSelected(new Set(packages.filter(p=>p.pkg).map(key)))}>全选已定位分包</button><button className="lx-btn-ghost" onClick={()=>setSelected(new Set())}>清空选择</button>
        <button className="lx-btn-ghost" disabled={busy} onClick={()=>void act(()=>json(base+'/'+sid+'/prepare',{method:'POST'}))}>查询分包大小与版本</button>
        <span className="text-xs text-lx-muted">已选 {chosen.length} 包 · 已知待下载 {size(chosen.filter(p=>!p.cached).reduce((n,p)=>n+p.size,0))}</span>
        <button className="lx-btn-primary" disabled={busy||!chosen.length} onClick={()=>void act(()=>json(base+'/'+sid+'/download',{method:'POST',body:JSON.stringify({packages:chosen.map(p=>({repo:p.repo,pkg:p.pkg}))})}))}>下载选中分包供核验</button>
        <button className="lx-btn-ghost" onClick={()=>void act(()=>json(base+'/'+sid+'/cancel',{method:'POST'}))}>停止下载</button><a className="lx-btn-ghost" href={base+'/'+sid+'/export'}>导出核验记录</a></div>
      <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr><th>选择／分包</th><th>结果数</th><th>TAG / NL</th><th>冲突 / 失败</th><th>大小</th><th>原图与核验</th></tr></thead><tbody>{packages.map(p=><tr key={key(p)} className="border-t border-lx"><td className="py-2"><label><input type="checkbox" disabled={!p.pkg} checked={selected.has(key(p))} onChange={e=>setSelected(old=>{const next=new Set(old);if(e.target.checked)next.add(key(p));else next.delete(key(p));return next})}/><span className="ml-2">{p.pkg||'未定位'}</span><small className="block text-lx-muted">{p.repo}</small></label></td><td>{p.count}</td><td>{p.tag} / {p.nl}</td><td>{p.conflicts} / {p.failed}</td><td>{size(p.size)}</td><td><p>{p.cached?'已缓存':p.status==='downloading'?'下载中':p.status==='failed'?'失败':'未下载'} · 已核验 {p.reviewed}</p><p className="text-lx-muted">{p.message}{p.total>0?' '+Math.round(p.done/p.total*100)+'%':''}</p><button className="lx-btn-ghost" onClick={()=>void act(()=>page(p))}>查看结果</button></td></tr>)}</tbody></table></div>
      {active&&<div className="mt-4 space-y-3"><p className="text-xs">{active.pkg||'未定位'} · {total} 张</p>{items.map(item=><ReviewCard key={key(active)+item.image_id} item={item} image={base+'/'+sid+'/image?'+new URLSearchParams({repo:active.repo,pkg:active.pkg,image_id:item.image_id})} decide={decide}/>)}<div className="flex items-center gap-3"><button className="lx-btn-ghost" disabled={offset===0} onClick={()=>void act(()=>page(active,Math.max(0,offset-20)))}>上一页</button><span className="text-xs">第 {offset/20+1} 页</span><button className="lx-btn-ghost" disabled={offset+20>=total} onClick={()=>void act(()=>page(active,offset+20))}>下一页</button></div></div>}
    </>}
    <FolderPicker open={picker!==null} files={['zip','jsonl','parquet','json']} onClose={()=>setPicker(null)} onPick={path=>{if(picker==='source')setSource(path);else setIndex(path);setPicker(null)}}/>
  </SectionPanel>
}

function ReviewCard({item,image,decide}:{item:Item;image:string;decide:(item:Item,status:string,edited?:{tag:string;nl:string})=>Promise<void>}){
  const [tag,setTag]=useState(item.edited?.tag??text(item.payload.tag,'tag')),[nl,setNl]=useState(item.edited?.nl??text(item.payload.nl,'nl')),[error,setError]=useState(''),[busy,setBusy]=useState(false)
  useEffect(()=>{setTag(item.edited?.tag??text(item.payload.tag,'tag'));setNl(item.edited?.nl??text(item.payload.nl,'nl'))},[item.review,item.edited?.tag,item.edited?.nl])
  async function save(status:string,edited?:{tag:string;nl:string}){setBusy(true);setError('');try{await decide(item,status,edited)}catch(e){setError(String(e))}finally{setBusy(false)}}
  return <div className="grid gap-3 rounded-lx-sm border border-lx p-3 md:grid-cols-[minmax(180px,1fr)_2fr]"><div>{item.image_available?<img src={image} loading="lazy" className="max-h-96 w-full object-contain" alt={item.image_id}/>:<p className="text-xs text-lx-muted">原图未下载／无法唯一定位</p>}<p className="mt-2 text-xs font-mono">{item.image_id} · {({pass:'通过',modify:'需要修改',redo:'重做'} as Record<string,string>)[item.review]??'待核验'}</p></div><div className="space-y-2"><label className="block text-xs text-lx-muted">TAG<textarea className="lx-input mt-1 min-h-20" value={tag} onChange={e=>setTag(e.target.value)}/></label><label className="block text-xs text-lx-muted">NL<textarea className="lx-input mt-1 min-h-32" value={nl} onChange={e=>setNl(e.target.value)}/></label>
    {!!item.payload.variants&&<details><summary className="cursor-pointer text-xs text-lx-warning">查看冲突候选</summary><pre className="overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(item.payload.variants,null,2)}</pre></details>}
    <div className="flex flex-wrap gap-2"><button disabled={busy} className="lx-btn-primary" onClick={()=>void save('pass',(tag!==text(item.payload.tag,'tag')||nl!==text(item.payload.nl,'nl')||item.edited)?{tag,nl}:undefined)}>通过</button><button disabled={busy} className="lx-btn-ghost" onClick={()=>void save('modify',{tag,nl})}>保存修改</button><button disabled={busy} className="lx-btn-ghost" onClick={()=>void save('redo',item.edited?{tag,nl}:undefined)}>标记重做</button><button disabled={busy} className="lx-btn-ghost" onClick={()=>void save('')}>取消核验</button></div>{error&&<p role="alert" className="text-xs text-lx-warning">{error}</p>}</div></div>
}
