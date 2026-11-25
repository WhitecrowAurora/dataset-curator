import { useEffect, useState } from 'react'
import { Download, FolderOpen, X } from 'lucide-react'
import { api, type ReviewModelEntry } from '../../lib/api'
import { hairApi } from '../conflicts/hairApi'
import { SectionPanel, StatusChip } from '../ui/launcher'

const running = (row: ReviewModelEntry) =>
  row.download?.status === 'queued' || row.download?.status === 'running'

export function ReviewModels() {
  const [rows, setRows] = useState<ReviewModelEntry[]>([])
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const refresh = async () => setRows((await api.models()).review ?? [])
  const act = async (key: string, action: () => Promise<unknown>) => {
    setBusy(key)
    setMessage('')
    try {
      await action()
      await refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy('')
    }
  }
  useEffect(() => { void refresh().catch(error => setMessage(String(error))) }, [])
  const downloading = rows.some(running)
  useEffect(() => {
    if (!downloading) return
    const timer = setInterval(() => { void refresh().catch(() => undefined) }, 1200)
    return () => clearInterval(timer)
  }, [downloading])

  return (
    <SectionPanel title="发色与动漫复核" description="下载后由现有流程自动调用；Torii 使用模型管理中的 llama.cpp 运行时。">
      <div className="space-y-3">
        {rows.map(row => (
          <div key={row.key} className="rounded-xl border border-lx p-4" data-review-model={row.key}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{row.title}</span>
                  <StatusChip tone={row.ready ? 'success' : 'neutral'}>{row.ready ? '文件已就绪' : '待下载'}</StatusChip>
                </div>
                <p className="mt-1 text-xs text-lx-dim">{row.note}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <button type="button" className="lx-btn-ghost" disabled={!!busy}
                  onClick={() => void act(row.key, async () => {
                    const result = await api.revealLocalDir('review', row.key)
                    if (!result.opened) setMessage(result.path)
                  })}><FolderOpen size={14} className="mr-1 inline" />打开位置</button>
                {running(row) ? (
                  <button type="button" className="lx-btn-ghost" disabled={!!busy}
                    onClick={() => void act(row.key, () => api.cancelDownload('review', row.key))}><X size={14} className="mr-1 inline" />取消下载</button>
                ) : (
                  <button type="button" className="lx-btn-primary" disabled={!!busy}
                    onClick={() => void act(row.key, () => api.startDownload('review', row.key))}>
                    <Download size={14} className="mr-1 inline" />{busy === row.key ? '处理中…' : row.ready ? '补齐文件' : '下载模型'}</button>
                )}
                {row.use_path && row.ready && !row.in_use && (
                  <button type="button" className="lx-btn-ghost" disabled={!!busy}
                    onClick={() => void act(row.key, async () => {
                      const current = await hairApi.settings()
                      await hairApi.configure({ ...current.settings, torii_dir: row.use_path! })
                      setMessage('Torii 路径已保存，后续任务自动使用。')
                    })}>使用已下载模型</button>
                )}
              </div>
            </div>
            <p className="mt-2 break-all text-xs text-lx-dim">下载位置：{row.local_dir}</p>
            {row.configured_path && <p className="mt-1 break-all text-xs text-lx-dim">当前 Torii 路径：{row.configured_path}</p>}
            {running(row) && <div className="mt-3">
              <progress className="h-2 w-full" max={100} value={row.download?.percent ?? 0} />
              <p className="break-all text-xs text-lx-dim">{row.download?.percent.toFixed(1)}% · {row.download?.current || '连接中…'}</p>
            </div>}
            {row.download?.status === 'error' && <p className="mt-2 whitespace-pre-wrap text-xs text-lx-warning">{row.download.message}</p>}
            {row.missing_files.length > 0 && <p className="mt-1 break-all text-xs text-lx-dim">缺少或不完整：{row.missing_files.join('、')}</p>}
            <details className="mt-2 text-xs text-lx-dim"><summary className="cursor-pointer">模型来源</summary>
              <p className="mt-1 break-all">{row.download_repo} · {row.revision}</p>
              {row.use_path && <p className="mt-1">Minthy 官方模型的社区 GGUF，使用已验证的 Q6_K 版本。</p>}
            </details>
          </div>
        ))}
        {message && <p role="status" className="break-all text-xs text-lx-warning">{message}</p>}
      </div>
    </SectionPanel>
  )
}
