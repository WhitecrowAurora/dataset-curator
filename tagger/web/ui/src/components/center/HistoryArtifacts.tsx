import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type CenterHistory } from '../../lib/api'
import { SectionPanel } from '../ui/launcher'
import { useFolderPicker } from '../FolderPicker'

const STATUS: Record<string, string> = {
  ready: '待核对', missing_local_files: '文件缺失', checked: '已核对，待上报', registered: '已上报完成',
  checking: '检查中', uploading: '回传中', completed: '已完成', failed: '失败', idle: '待命',
  discovering: '识别中', verifying: '等待中心核验', incomplete: '未完结，等待分发续做',
}

export function HistoryArtifacts({ locked }: { locked: boolean }) {
  const [state, setState] = useState<CenterHistory | null>(null)
  const [draft, setDraft] = useState({ repo: '', package: '', source: '', out_dir: '' })
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [busy, setBusy] = useState(false)
  const discovered = useRef(false)
  const folder = useFolderPicker()
  const refresh = useCallback(async () => {
    try { setState(await api.centerHistory()); setPollError('') }
    catch (e) { setPollError(String(e)) }
  }, [])
  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(timer)
  }, [refresh])
  useEffect(() => {
    if (locked || discovered.current) return
    discovered.current = true
    void api.centerHistoryDiscover().then(setState).catch((e) => setError(String(e)))
  }, [locked])
  async function act(fn: () => Promise<CenterHistory>) {
    setBusy(true); setError('')
    try { setState(await fn()) }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  const working = locked || busy || Boolean(state?.running) || Boolean(state?.blocked_reason)
  return <SectionPanel title="本地已做结果" trailing={<button className="lx-btn-ghost" type="button" disabled={working}
    onClick={() => void act(() => api.centerHistoryDiscover())}>重新识别</button>}>
    <p className="mb-3 text-xs text-lx-muted">自动查找本地产物和原分片。核对 TAG / NL 后上报中心，已成功的图片会在后续处理时跳过。</p>
    {(state?.blocked_reason || locked) && <p role="status" className="mb-3 text-xs text-lx-warning">{state?.blocked_reason || '请先停止当前任务，再核对或补注入历史产物。'}</p>}
    <button className="lx-btn-primary mb-3" type="button" disabled={working}
      onClick={() => void act(() => api.centerHistoryCheck())}>{state?.running ? '正在处理…' : '核对并上报'}</button>
    <button className="lx-btn-ghost ml-2" type="button" disabled={working}
      onClick={() => folder.pick('', (path) => void act(() => api.centerHistoryDirectory(path)))}>查找其他目录</button>
    <p className="mt-2 mb-3 text-xs text-lx-muted">补注入会按图片 ID 核对仓库画师表；未配置目录时自动查找唯一匹配的分表，不运行模型。重复前缀自动合并，画师不一致时列出待核对项；强制模式按表替换完整前缀，原文件保留在 .delete 中。</p>
    {(error || pollError) && <p role="alert" className="text-xs text-lx-warning">{error || pollError}</p>}
    {state?.message && <p role="status" className="mb-3 text-xs text-lx-muted">{state.message}</p>}
    <details>
    <summary className="cursor-pointer text-xs text-lx-muted">未识别到？补充关联</summary>
    <form className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); void act(() => api.centerHistoryBind(draft)) }}>
      {([['repo', '源仓库'], ['package', '源分片'], ['source', '原始分片路径'], ['out_dir', '产物目录']] as const).map(([key, label]) =>
        <label key={key} className="space-y-1 text-xs text-lx-muted">{label}
          <input className="lx-input" aria-label={label} required value={draft[key]} disabled={working}
            onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} />
        </label>)}
      <button className="lx-btn-primary justify-self-start" disabled={working}>关联</button>
    </form>
    </details>
    <div className="mt-4 space-y-3">
      {state?.items.map((item) => <div key={item.id} className="rounded-lx-sm border border-lx p-3 text-xs">
        <div className="flex items-center justify-between gap-3">
          <span className="break-all">{item.repo} / {item.package}</span>
          <button type="button" className="lx-btn-ghost shrink-0" disabled={working}
            onClick={() => void act(() => api.centerHistoryRemove(item.id))}>解除关联</button>
        </div>
        <p className="mt-1 text-lx-muted">{STATUS[item.status] ?? item.status} · {item.files.length} 个文件</p>
        <div className="mt-2 flex flex-wrap gap-2">
          <button type="button" className="lx-btn-ghost" disabled={working || item.local_status !== 'ready'}
            onClick={() => void act(() => api.centerHistoryCheck({reinject: 'missing', candidate_id: item.id}))}>补注入画师并上报</button>
          <button type="button" className="lx-btn-ghost" disabled={working || item.local_status !== 'ready'}
            onClick={() => void act(() => api.centerHistoryCheck({reinject: 'force', candidate_id: item.id}))}>强制重新注入并上报</button>
        </div>
        {item.report?.error && <p className="text-lx-warning">{item.report.error}</p>}
        {item.report?.artist_reinject && <div className="mt-2 space-y-1 text-lx-muted">
          <p className="break-all">画师表：{item.report.artist_reinject.source}</p>
          <p>{item.report.artist_reinject.origin === 'center'
            ? `中心发现 ${item.report.artist_reinject.review_count} 条旧画师前缀需核对，尚未完成登记；核对后可强制按表替换。`
            : `已补注入 TAG ${item.report.artist_reinject.changed_tag} 条、NL ${item.report.artist_reinject.changed_nl} 条；待核对 ${item.report.artist_reinject.review_count} 条，尚未回传。`}</p>
          {item.report.artist_reinject.review_samples.map((r) => <p key={`${r.image_id}/${r.part}`} className="break-all text-lx-warning">
            图片 {r.image_id} · {r.part.toUpperCase()} · 原前缀：{r.existing_prefix} · 表中画师：{r.artists.join('、')} · {r.reason}
          </p>)}
        </div>}
        {item.report?.expected != null && <>
          <p className="mt-2">图片：{item.report.expected} · 已登记：{item.report.registered_parts?.map((p) => p.toUpperCase()).join('、') || '无'}</p>
          {Object.entries(item.report.parts ?? {}).map(([part, n]) => <p key={part} className="mt-1 text-lx-muted">
            {part.toUpperCase()} · 成功 {n.success} · 排除 {n.excluded} · 失败 {n.failed} · 缺失 {n.missing}
          </p>)}
          {(item.report.conflicts ?? []).map((c) => <p key={c} className="text-lx-warning">{c}</p>)}
        </>}
      </div>)}
      {!!state?.unlinked.length && <div className="text-xs text-lx-muted">需要补充关联：
        {state.unlinked.map((r, index) => <p key={r.path + index} className="break-all" title={r.path}>{r.name} · {r.reason}</p>)}
      </div>}
    </div>
    {folder.dialog}
  </SectionPanel>
}
