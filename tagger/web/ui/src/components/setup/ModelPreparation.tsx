import { useEffect, useRef } from 'react'
import { Loader2, Play, Square } from 'lucide-react'
import { ProgressBar } from '../ui/ProgressBar'
import { ErrorBox } from '../ui/controls'
import { failureAdvice } from '../../lib/guidance'
import type { useModelPreparation } from './useModelPreparation'

export function ModelPreparation({ preparation: p }: {
  preparation: ReturnType<typeof useModelPreparation>;
}) {
  const consoleRef = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const el = consoleRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [p.progress])
  return <div className="mt-5 space-y-3 border-t border-lx pt-5">
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" className="lx-btn-primary inline-flex items-center gap-2" disabled={p.running} onClick={() => void p.start()}>
        {p.running ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
        {p.running ? '正在准备…' : p.ready ? '重新检查' : p.error ? '重试' : '开始'}</button>
      {p.running && <button type="button" className="lx-btn-ghost inline-flex items-center gap-2" onClick={p.cancel}><Square size={13} />停止准备</button>}
      <p className="text-xs text-lx-muted">下载模型、安装依赖和量化会依次自动完成。</p>
    </div>
    {p.error && <ErrorBox error={p.error} nextStep={failureAdvice(p.error, 'download')} />}
    {p.progress && <>
      <div className="flex justify-between gap-3 text-xs text-lx-muted"><span>{p.progress.stage}</span><span>{Math.round(p.progress.percent)}%</span></div>
      <ProgressBar percent={p.progress.percent} tone={p.ready ? 'success' : 'accent'} heightClass="h-1.5" />
      <pre ref={consoleRef} role="log" aria-label="模型准备控制台" aria-live="polite"
        className="max-h-52 min-h-28 overflow-auto whitespace-pre-wrap break-all rounded-lx-sm border border-lx bg-lx-bg p-3 font-mono text-[11px] leading-5 text-lx-secondary">
        {p.progress.logs.join('\n') || '等待任务日志…'}</pre>
    </>}
  </div>
}
