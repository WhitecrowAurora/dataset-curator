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
  const working = locked || busy || Boolean(state?.running)
  return <SectionPanel title="本地已做结果" trailing={<button className="lx-btn-ghost" type="button" disabled={working}
    onClick={() => void act(() => api.centerHistoryDiscover())}>重新识别</button>}>
    <p className="mb-3 text-xs text-lx-muted">自动查找本地产物和原分片。核对 TAG / NL 后上报中心，已成功的图片会在后续处理时跳过。</p>
    <button className="lx-btn-primary mb-3" type="button" disabled={working}
      onClick={() => void act(() => api.centerHistoryCheck())}>{state?.running ? '正在处理…' : '核对并上报'}</button>
    <button className="lx-btn-ghost ml-2" type="button" disabled={working}
      onClick={() => folder.pick('', (path) => void act(() => api.centerHistoryDirectory(path)))}>查找其他目录</button>
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
        {item.report?.error && <p className="text-lx-warning">{item.report.error}</p>}
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
