import { useCallback, useEffect, useState } from 'react'
import { Settings2 } from 'lucide-react'
import { api } from '../lib/api'
import { ENDPOINT_PRESETS, endpointPresetOf } from '../lib/endpoint'
import { ChoiceGrid } from '../components/ui/controls'
import { PageHeader, SectionPanel, SettingRow } from '../components/ui/launcher'
import { PROGRESS_INTERVALS } from '../lib/prefs'
import { DiskProtection } from '../components/settings/DiskProtection'

/**
 * 设置页。两个来源刻意分开摆：界面偏好只存当前浏览器，下载源是服务端配置
 * （configs/models.json，与模型页共用同一份），混在一张卡里会让人以为
 * 换台浏览器镜像就变了。
 */
export function Settings({
  progressInterval,
  onProgressInterval,
}: {
  progressInterval: number
  onProgressInterval: (ms: number) => void
}) {
  // 选项和选中值都按**秒**给：底层存毫秒，但这里必须先把 1000 折掉，
  // 否则 options 是 ['1000','2000',...] 而 value 是 '5'，既显示成 1000s，
  // 也永远匹配不到任何一档（等于没有选中态）。0 = 暂停。
  const options = PROGRESS_INTERVALS.map((ms) => String(ms / 1000))
  const value = String(progressInterval / 1000)

  // 下载源。effective 是后端算完的最终值（页面设置 > HF_ENDPOINT > 默认），
  // 前端不重复推导优先级，只把它显示出来。
  const [pick, setPick] = useState('')
  const [draft, setDraft] = useState('')
  const [now, setNow] = useState('')
  const [envNow, setEnvNow] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api
      .models()
      .then((d) => {
        setPick(endpointPresetOf(d.endpoint))
        setDraft(d.endpoint)
        setNow(d.effective_endpoint)
        setEnvNow(d.env_endpoint)
      })
      .catch(() => undefined)
  }, [])

  const save = useCallback(async (url: string) => {
    setBusy(true)
    setMsg('')
    try {
      const res = await api.saveEndpoint(url)
      setNow(res.effective)
      setPick(endpointPresetOf(url))
      setDraft(url)
      setMsg('已保存')
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [])

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-5">
      <PageHeader
        icon={<Settings2 size={18} />}
        eyebrow="本机偏好"
        title="设置"
        description="界面、磁盘保护和下载偏好"
      />

      <SectionPanel title="界面">
        <SettingRow
          label="底部进度条刷新频率"
          help="调快更跟手，但会多打后端"
        >
          <ChoiceGrid
            options={options}
            value={value}
            onChange={(v) => onProgressInterval(Number(v) * 1000)}
            className="grid-cols-5"
            size="sm"
            mono
            ariaLabel="底部进度条刷新频率"
            renderLabel={(v) => (v === '0' ? '暂停' : `${v}s`)}
          />
        </SettingRow>
        <p className="px-1 pt-2 text-[10px] leading-relaxed text-lx-dim">
          默认 5 秒；「暂停」只停止轮询，任务照跑
        </p>
      </SectionPanel>

      <DiskProtection />

      <SectionPanel title="下载" description="写进服务端配置，任务与模型下载共用">
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex min-w-0 flex-1 flex-col gap-1">
            <span className="text-[11px] text-lx-muted">下载源（镜像）</span>
            <select
              className="lx-select text-xs"
              value={pick}
              onChange={(e) => {
                const v = e.target.value
                setPick(v)
                setMsg('')
                if (v === 'custom') setDraft(draft || 'https://')
              }}
            >
              {ENDPOINT_PRESETS.map((p) => (
                <option key={p.key || 'default'} value={p.key}>
                  {p.label}
                </option>
              ))}
              <option value="custom">自定义…</option>
            </select>
          </label>
          {pick === 'custom' && (
            <label className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="text-[11px] text-lx-muted">地址</span>
              <input
                className="lx-input font-mono text-xs"
                spellCheck={false}
                value={draft}
                placeholder="https://hf-mirror.com"
                onChange={(e) => {
                  setDraft(e.target.value)
                  setMsg('')
                }}
              />
            </label>
          )}
          <button
            type="button"
            className="lx-btn-ghost shrink-0 px-3 py-1.5 text-[11px]"
            disabled={busy}
            onClick={() => void save((pick === 'custom' ? draft : pick).trim())}
          >
            {busy ? '保存中…' : '保存'}
          </button>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-lx-dim">
          当前生效：<span className="font-mono">{now || '—'}</span>
          {envNow && '（环境变量 HF_ENDPOINT 已设；这里的选择优先）'}
        </p>
        {msg && <p className="mt-1 text-[11px] text-lx-muted">{msg}</p>}
      </SectionPanel>
    </div>
  )
}
