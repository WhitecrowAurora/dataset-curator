import { useState } from 'react'
import { StatusChip } from '../ui/launcher'
import { HairColorSample } from './HairColorSample'
import { HairCrop } from './HairCrop'
import { hairApi, probabilityText, type HairColor, type HairReview } from './hairApi'

const MODEL_NAMES: Record<string, string> = { pixai: 'PixAI', mobilenetv3_v0_dist: 'MobileNet', caformer_s36_v0: 'CAFormer', qwen3vl: 'Qwen3-VL', toriigate: 'ToriiGate' }
const STATUS_NAMES = { pending: '等待中', running: '复核中', accepted: '自动通过', review: '待人工确认', decided: '人工已确认', failed: '复核失败', skipped: '无可见头发' }

export function HairReviewCard({ item, colors, onChange }: { item: HairReview; colors: HairColor[]; onChange: () => void }) {
  const [selected, setSelected] = useState<string[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [cropping, setCropping] = useState(false)
  const [box, setBox] = useState<number[] | null>(null)
  const manual = !['pending', 'running'].includes(item.status)
  const label = (color: string) => colors.find((c) => c.color === color)?.label ?? color

  async function decide(picks: string[], noHair = false) {
    setBusy(true)
    setError('')
    try { const result = await hairApi.decide(item.id, picks, noHair); if (result.writeback_error) setError(`决定已保存，标签回写失败：${result.writeback_error}`); onChange() }
    catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setBusy(false) }
  }

  return (
    <article className="rounded-lx-sm border border-lx-subtle bg-lx-surface p-3">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="font-semibold text-lx-primary">{item.image_name}</span>
        {item.parent_id && <span className="text-lx-muted">角色裁图</span>}
        <StatusChip tone={item.status === 'review' ? 'warning' : item.status === 'failed' ? 'danger' : item.selected.length ? 'success' : 'neutral'}>{STATUS_NAMES[item.status]}</StatusChip>
        {item.selected.length > 0 && <span className="text-lx-success">{item.selected.map(label).join(' + ')}</span>}
        <span className="text-lx-muted">{item.reason}</span>
        {item.elapsed_ms !== undefined && <span className="ml-auto text-lx-muted">{(item.elapsed_ms / 1000).toFixed(2)} 秒</span>}
      </div>
      <div className="grid gap-3 md:grid-cols-[minmax(180px,260px)_1fr]">
        <div>
          <img src={hairApi.image(item.id)} alt={`${item.image_name} 原图`} className="max-h-72 w-full rounded object-contain bg-lx-surface-raised" />
          {item.crop && <img src={hairApi.image(item.id, true)} alt="角色头部裁图" className="mt-2 max-h-40 w-full rounded object-contain bg-lx-surface-raised" />}
          <button disabled={busy || ['pending', 'running'].includes(item.status)} className="lx-btn-ghost mt-2 text-xs" onClick={() => setCropping(!cropping)}>框选角色重新复核</button>
          {cropping && <div className="mt-2"><HairCrop src={hairApi.image(item.id)} onChange={setBox} /><button disabled={!box || busy} className="lx-btn-primary mt-2 px-2 text-xs" onClick={() => { if (box) { setBusy(true); void hairApi.crop(item.id, box).then(() => { setCropping(false); onChange() }).catch((e) => setError(String(e))).finally(() => setBusy(false)) } }}>复核此角色</button></div>}
        </div>
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            {item.observations.map((o) => (
              <div key={o.model} className="rounded border border-lx-subtle px-2 py-1 text-xs text-lx-secondary" title={o.reason || o.error}>
                <span className="text-lx-muted">{MODEL_NAMES[o.model] ?? o.model}：</span>
                {o.error ? '不可用' : o.color ? label(o.color) : '不明确'}
                {o.probability !== undefined && ` ${probabilityText(o.probability)}`}
                {o.source === 'existing_result' && '（已有结果）'}
              </div>
            ))}
          </div>
          {item.observations.flatMap((o) => o.characters ?? []).map((role, i) => <details key={i} className="text-xs text-lx-secondary"><summary className="cursor-pointer">{role.name} · {role.colors.map(label).join(' / ') || '发色不明确'}{role.nonhuman ? ' · 非人物' : ''}</summary><p className="mt-1 whitespace-pre-wrap">{role.description}</p></details>)}
          {(item.status === 'failed' || item.status === 'review' && item.observations.some((o) => o.error)) && <button className="lx-btn-primary w-fit px-3 text-xs" disabled={busy} onClick={() => { setBusy(true); void hairApi.retry(item.id).then(onChange).catch((e) => setError(String(e))).finally(() => setBusy(false)) }}>重试复核</button>}
          {!item.parent_id && manual && Boolean(item.role_count) && <button className="lx-btn-ghost w-fit text-xs" disabled={busy || item.settled_roles !== item.role_count} onClick={() => { setBusy(true); void hairApi.mergeRoles(item.id).then(onChange).catch((e) => setError(String(e))).finally(() => setBusy(false)) }}>合并已确认角色的发色到原图</button>}
          {item.candidates.length > 0 && <div>
            <p className="mb-2 text-xs text-lx-muted">{item.observations.some((o) => o.model === 'caformer_s36_v0') ? 'CAFormer' : 'MobileNet'} 前三候选 · 概率为该模型输出</p>
            <div className="grid gap-2 sm:grid-cols-3">
              {[...item.candidates].sort((a, b) => (b.preference ?? 0) - (a.preference ?? 0)).map((c) => (
                <button key={c.color} disabled={!manual || busy || c.color === 'light'} onClick={() => void decide([c.color])}
                  className="flex items-center gap-2 rounded-lx-sm border border-lx-subtle bg-lx-surface-raised p-2 text-left enabled:hover:border-lx-accent disabled:cursor-default">
                  <HairColorSample color={c.color} swatch={c.swatch} />
                  <div className="text-xs"><div className="text-lx-primary">{c.label}</div><div className="mt-1 font-mono text-lx-secondary">{probabilityText(c.probability)}</div>
                    {c.preference !== undefined && <div className="mt-1 text-lx-muted" title="根据人工确认学习的相对偏好，不是发色准确率">偏好 {(c.preference * 100).toFixed(1)}%</div>}
                    <span className="mt-1 inline-block h-2 w-8 rounded border border-lx-subtle" style={{ backgroundColor: c.swatch }} />
                  </div>
                </button>
              ))}
            </div>
            {item.candidates.some((c) => c.color === 'light') && <p className="mt-1 text-[11px] text-lx-muted">浅色组包含多种浅色，请在下方选择具体颜色；示意不代表严格色值边界。</p>}
          </div>}
          {manual && <div className="flex flex-wrap items-center gap-2">
            {colors.filter((c) => c.color !== 'light' && (['silver', 'grey', 'white', 'blonde'].includes(c.color) || item.extra_colors?.includes(c.color))).map((c) => <button key={c.color} disabled={busy} className="flex items-center gap-1 rounded border border-lx-subtle p-1 text-xs" onClick={() => void decide([c.color])}><HairColorSample color={c.color} swatch={c.swatch} /><span>{c.label}</span></button>)}
            <select aria-label="人工选择发色" className="lx-input w-auto text-xs" value="" disabled={busy} onChange={(e) => setSelected((prev) => prev.includes(e.target.value) ? prev : [...prev, e.target.value].slice(-2))}>
              <option value="">其他颜色 / 双色</option>
              {colors.filter((c) => c.color !== 'light').map((c) => <option key={c.color} value={c.color}>{c.label}</option>)}
            </select>
            {selected.map((color) => <button key={color} className="lx-btn-ghost text-xs" onClick={() => setSelected((prev) => prev.filter((c) => c !== color))}>{label(color)} ×</button>)}
            <button className="lx-btn-primary px-3 text-xs" disabled={busy || !selected.length} onClick={() => void decide(selected)}>确认选择</button>
            <button className="lx-btn-ghost text-xs" disabled={busy} onClick={() => void decide([], true)}>无可见头发</button>
            {['decided', 'accepted', 'skipped'].includes(item.status) && <button className="lx-btn-ghost text-xs" disabled={busy} onClick={() => void decide([])}>撤销决定</button>}
          </div>}
          {(error || item.writeback_error) && <p className="text-xs text-lx-danger">{error || `标签回写失败：${item.writeback_error}`}</p>}
        </div>
      </div>
    </article>
  )
}
