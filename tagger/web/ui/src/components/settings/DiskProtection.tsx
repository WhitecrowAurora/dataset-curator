import { useEffect, useState } from 'react'
import { api } from '../../lib/api'
import { SectionPanel } from '../ui/launcher'

export function DiskProtection() {
  const [gb, setGb] = useState('')
  const [percent, setPercent] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    api.diskSettings().then((settings) => {
      setGb(String(settings.disk_min_free_gb))
      setPercent(String(Number((settings.disk_min_free_pct * 100).toFixed(6))))
      setLoaded(true)
    }).catch((error) => setMessage(error instanceof Error ? error.message : String(error)))
  }, [])

  async function save() {
    const freeGb = Number(gb)
    const freePercent = Number(percent)
    if (!gb.trim() || !percent.trim() || !Number.isFinite(freeGb) ||
        !Number.isFinite(freePercent) || freeGb < 0 || freeGb > 1048576 ||
        freePercent < 0 || freePercent > 100) {
      setMessage('请填写有效数字：剩余空间至少 0 GB，剩余比例为 0～100%')
      return
    }
    setBusy(true)
    setMessage('')
    try {
      await api.saveDiskSettings({ disk_min_free_gb: freeGb, disk_min_free_pct: freePercent / 100 })
      setMessage('已保存，下一次开工生效')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <SectionPanel title="磁盘保护" description="保存在本机，调度中心与本地任务共用">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-[11px] text-lx-muted">最低剩余空间（GB）</span>
          <input className="lx-input text-xs" type="number" min="0" max="1048576" step="0.1"
            disabled={!loaded || busy} value={gb} onChange={(event) => { setGb(event.target.value); setMessage('') }} />
        </label>
        <label className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-[11px] text-lx-muted">最低剩余比例（%）</span>
          <input className="lx-input text-xs" type="number" min="0" max="100" step="0.1"
            disabled={!loaded || busy} value={percent} onChange={(event) => { setPercent(event.target.value); setMessage('') }} />
        </label>
        <button className="lx-btn-ghost px-3 py-1.5 text-[11px]" type="button"
          disabled={!loaded || busy} onClick={() => void save()}>{busy ? '保存中…' : '保存磁盘设置'}</button>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-lx-dim">
        默认 30 GB／15%；任一项低于下限就停止开工。填 0 可关闭对应限制，大容量盘可只保留 GB 下限。
        调度任务在加载模型前检查输出盘；本地任务开启「盘位预算」时，沿用预计产出量检查。
      </p>
      {message && <p role="status" className="mt-2 text-[11px] text-lx-muted">{message}</p>}
    </SectionPanel>
  )
}
