import { useEffect, useRef, useState } from 'react'
import { api, type CenterOfflineState, type CenterExecution } from '../../lib/api'
import { useFolderPicker } from '../FolderPicker'
import { ErrorBox } from '../ui/controls'
import { SectionPanel, SettingRow } from '../ui/launcher'
import { ExecutionSettings } from './ExecutionSettings'

export function OfflineTasks() {
  const [path, setPath] = useState('')
  const [shards, setShards] = useState('work/offline/shards')
  const [out, setOut] = useState('work/offline/results')
  const [state, setState] = useState<CenterOfflineState | null>(null)
  const [error, setError] = useState('')
  const [exported, setExported] = useState('')
  const [exportPackage, setExportPackage] = useState('')
  const [busy, setBusy] = useState(false)
  const [config, setConfig] = useState<CenterExecution | null>(null)
  const [token, setToken] = useState('')
  const [remember, setRemember] = useState(true)
  const [tokenSet, setTokenSet] = useState(false)
  const loadedId = useRef<string | undefined>(undefined)
  useEffect(() => { api.center().then((s) => setTokenSet(s.credentials.hf_token_set)).catch(() => {}) }, [])
  const { pick: openPicker, dialog: picker } = useFolderPicker()
  useEffect(() => {
    let closed = false
    const pull = () => api.centerOffline().then((data) => { if (!closed) {
      if (data.config && loadedId.current !== data.id) setConfig(data.config)
      if (loadedId.current !== data.id) { setExportPackage(''); setExported('') }
      loadedId.current = data.id; setState(data)
      if (data.out_dir) setOut(data.out_dir)
    } }).catch((e) => { if (!closed) setError(String(e)) })
    void pull(); const timer = setInterval(pull, 2000)
    return () => { closed = true; clearInterval(timer) }
  }, [])
  const act = async (action: () => Promise<CenterOfflineState>) => {
    setBusy(true); setError('')
    try {
      const data = await action(); if (loadedId.current !== data.id) { setExportPackage(''); setExported('') }; loadedId.current = data.id; setState(data)
      if (data.config) setConfig(data.config)
      if (data.out_dir) setOut(data.out_dir)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  return <SectionPanel title="离线已分配任务">
    {(error || state?.error) && <ErrorBox error={error || state?.error || ''} />}
    <SettingRow label="离线任务清单">
      <div className="flex gap-2"><input className="lx-input text-xs" aria-label="离线清单路径" value={path} disabled={!!state?.running} onChange={(e) => setPath(e.target.value)} />
        <button type="button" className="lx-btn-ghost" disabled={!!state?.running} onClick={() => openPicker(path, setPath, ['json'])}>浏览</button>
        <button type="button" className="lx-btn-primary" disabled={busy || !!state?.running || !path.trim()}
          onClick={() => void act(() => api.centerOfflineImport(path))}>导入</button></div>
    </SettingRow>
    {state?.id && <>
      <p className="text-xs text-lx-muted">任务 {state.id} · {state.tasks.length} 个分包 · {state.message}</p>
      {config && <details className="my-3"><summary className="cursor-pointer text-xs">本机执行配置</summary>
        <ExecutionSettings offline value={config} onChange={setConfig} mode={state.config?.mode || 'both'}
          disabled={state.running || busy} token={token} onToken={setToken} remember={remember} onRemember={setRemember}
          tokenSet={tokenSet} onClear={() => { setToken(''); setTokenSet(false); void act(() => api.centerOfflineConfigure({ ...config, hf_token: '' })) }} />
      </details>}
      <SettingRow label="分片素材目录" help="已有分片放在这里；缺少的分片自动从 HF 下载。私有仓库使用本机 HF token。">
        <div className="flex gap-2"><input className="lx-input text-xs" aria-label="离线素材目录" value={shards} disabled={state.running} onChange={(e) => setShards(e.target.value)} />
          <button type="button" className="lx-btn-ghost" disabled={state.running} onClick={() => openPicker(shards, setShards)}>浏览</button></div>
      </SettingRow>
      <SettingRow label="产物目录">
        <div className="flex gap-2"><input className="lx-input text-xs" aria-label="离线产物目录" value={out} disabled={state.running} onChange={(e) => setOut(e.target.value)} />
          <button type="button" className="lx-btn-ghost" disabled={state.running} onClick={() => openPicker(out, setOut)}>浏览</button></div>
      </SettingRow>
      <SettingRow label="网页回收包范围" help="默认导出全部已完成分片；超过 64 MiB 时可选择单个分片导出。">
        <select className="lx-input text-xs" aria-label="网页回收包分片" value={exportPackage} disabled={busy || state.running} onChange={(e) => setExportPackage(e.target.value)}>
          <option value="">全部已完成分片</option>
          {state.tasks.map((task) => <option key={`${task.repo}/${task.package}`} value={`${task.repo}/${task.package}`}>{task.package}</option>)}
        </select>
      </SettingRow>
      <div className="mt-3 flex gap-2">
        <button type="button" className="lx-btn-primary" disabled={busy || state.running || !shards.trim() || !out.trim()}
          onClick={() => void act(async () => {
            if (config) await api.centerOfflineConfigure({ ...config, ...(token ? { hf_token: token, remember_hf: remember } : {}) })
            setToken(''); if (token) setTokenSet(true)
            return api.centerOfflineStart(shards, out)
          })}>开始离线打标</button>
        <button type="button" className="lx-btn-ghost" disabled={busy || state.running || !out.trim()}
          onClick={() => void act(() => api.centerOfflineStart(shards, out, true))}>联网后补传</button>
        <button type="button" className="lx-btn-ghost" disabled={busy || state.running || !out.trim()}
          onClick={async () => {
            setBusy(true); setError(''); setExported('')
            try { const result = await api.centerOfflineExport(shards, out, exportPackage); setExported(`已导出 ${result.tasks} 个分片：${result.path}`) }
            catch (e) { setError(String(e)) } finally { setBusy(false) }
          }}>导出网页回收包</button>
        <button type="button" className="lx-btn-ghost" disabled={busy || !state.running} onClick={() => void act(api.centerOfflineStop)}>停止</button>
      </div>
      <p className="mt-2 text-xs text-lx-muted">换机器补传时，复制整个产物目录并导入里面的任务清单。</p>
      <p className="mt-2 text-xs text-lx-muted">始终连不上中心时，导出网页回收包交给管理员上传并确认。不包含原始图片，无需启动模型。</p>
      {exported && <p role="status" className="mt-2 break-all text-xs">{exported}</p>}
    </>}
    {picker}
  </SectionPanel>
}
