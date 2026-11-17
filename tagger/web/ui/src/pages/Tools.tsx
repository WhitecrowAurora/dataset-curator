import { useEffect, useState } from 'react'
import { FileJson, FolderOpen, Loader2, Play, Wrench } from 'lucide-react'
import { toolsApi, type JsonlCleanJob } from '../lib/tools'
import { ApiError } from '../lib/api'
import { fmtCount } from '../lib/format'
import { useFolderPicker } from '../components/FolderPicker'
import { ErrorBox, ToggleRow } from '../components/ui/controls'
import { PageHeader, SectionPanel, SettingRow, StatusChip } from '../components/ui/launcher'
import { ProgressBar } from '../components/ui/ProgressBar'

const LAST_JOB = 'tagger-jsonl-clean-job'

export function Tools() {
  const [path, setPath] = useState('')
  const [recursive, setRecursive] = useState(false)
  const [job, setJob] = useState<JsonlCleanJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const picker = useFolderPicker()
  const running = busy || job?.status === 'running'

  useEffect(() => {
    let alive = true
    try {
      const id = localStorage.getItem(LAST_JOB)
      if (id) toolsApi.jsonlJob(id).then((result) => {
        if (alive) setJob(result)
      }).catch(() => undefined)
    } catch { /* 本机存储不可用时仍能清洗。 */ }
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!job || job.status !== 'running') return
    let alive = true
    let pending = false
    const timer = window.setInterval(async () => {
      if (pending) return
      pending = true
      try {
        const result = await toolsApi.jsonlJob(job.id)
        if (alive) { setJob(result); setError('') }
      } catch (err) {
        if (alive) {
          setError(err instanceof Error ? err.message : String(err))
          if (err instanceof ApiError && err.status === 404) {
            setJob(null)
            try { localStorage.removeItem(LAST_JOB) } catch { /* 可选记录。 */ }
          }
        }
      } finally { pending = false }
    }, 1000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [job?.id, job?.status])

  async function start() {
    setBusy(true)
    setError('')
    try {
      const result = await toolsApi.cleanJsonl(path.trim(), recursive)
      setJob(result)
      try { localStorage.setItem(LAST_JOB, result.id) } catch { /* 可选记录。 */ }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally { setBusy(false) }
  }

  return (
    <div className="lx-page-enter mx-auto flex max-w-5xl flex-col gap-4">
      <PageHeader icon={<Wrench size={17} />} title="工具" description="整理已经打好的标注产物" />
      <SectionPanel title="JSONL 标签置信度清洗" description="将 tags 中的 [标签, 置信度] 转为纯标签，保留标签顺序和其他字段。">
        <div className="space-y-4">
          <SettingRow label="已打好的 JSONL" icon={<FileJson size={14} />} required>
            <div className="flex w-full gap-2">
              <input id="jsonl-clean-path" aria-label="已打好的 JSONL 文件或文件夹" className="lx-input flex-1" value={path} disabled={running}
                onChange={(e) => setPath(e.target.value)} placeholder="JSONL 文件路径，或存放 JSONL 的文件夹" />
              <button type="button" className="lx-btn-ghost" disabled={running}
                onClick={() => picker.pick(path || undefined, setPath, ['jsonl'])}>
                <FolderOpen size={14} /> 选择
              </button>
            </div>
          </SettingRow>
          <ToggleRow checked={recursive} onChange={setRecursive} disabled={running} label="递归子文件夹"
            hint="输入为文件夹时，也处理全部子文件夹中的 JSONL；选择单个文件时只处理该文件。" />
          <p className="text-xs text-lx-muted">修正版保存到旁边的新文件夹，保留原文件名和子目录结构。原文件保留；坏行原样保留并列出行号。</p>
          <button type="button" className="lx-btn-primary inline-flex items-center gap-2" disabled={running || !path.trim()} onClick={() => void start()}>
            {running ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} {running ? '正在清洗' : '开始清洗'}
          </button>
        </div>
      </SectionPanel>
      {error && <ErrorBox error={error} />}
      {job && (
        <SectionPanel title="处理结果" trailing={<StatusChip tone={job.status === 'running' ? 'accent' : job.status === 'done' ? 'success' : 'warning'}>
          {job.status === 'running' ? '进行中' : job.status === 'done' ? '完成' : '完成，部分需要检查'}
        </StatusChip>}>
          <div className="space-y-3" aria-live="polite">
            <ProgressBar percent={job.total_files ? job.processed_files / job.total_files * 100 : 0} />
            <p className="text-sm text-lx-secondary">文件 {fmtCount(job.processed_files)} / {fmtCount(job.total_files)} · 处理 {fmtCount(job.rows)} 行 · 修改 {fmtCount(job.changed_rows)} 行 · 移除 {fmtCount(job.removed_scores)} 个置信度</p>
            <p className="break-all text-xs text-lx-muted">输出目录：{job.output_dir}</p>
            {job.current && <p className="break-all text-xs text-lx-muted">正在处理：{job.current}</p>}
            {(job.failed_files > 0 || job.invalid_rows > 0) && <ErrorBox tone="warning" title="需要检查" error={`${job.failed_files} 个文件失败，${job.invalid_rows} 行无法解析；请查看下面的明细。`} />}
            <div className="max-h-80 space-y-2 overflow-y-auto">
              {job.results.map((result) => (
                <div key={result.source} className="rounded-lx-sm bg-lx-surface-raised p-3 text-xs">
                  <p className="break-all text-lx-secondary">{result.source}</p>
                  {result.error ? <p className="mt-1 text-lx-danger">{result.error}</p> : <>
                    <p className="mt-1 text-lx-muted">修改 {fmtCount(result.changed_rows ?? 0)} 行，移除 {fmtCount(result.removed_scores ?? 0)} 个置信度</p>
                    <p className="mt-1 break-all text-lx-muted">{result.output}</p>
                    {!!result.invalid_rows && <p className="mt-1 text-lx-warning">坏行 {result.invalid_rows} 条，前 10 个行号：{result.invalid_lines?.join('、')}</p>}
                  </>}
                </div>
              ))}
            </div>
          </div>
        </SectionPanel>
      )}
      {picker.dialog}
    </div>
  )
}
