import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { SectionPanel, SettingRow } from '../ui/launcher'
import { HairReviewCard } from './HairReviewCard'
import { HairCrop } from './HairCrop'
import { HairSettingsPanel } from './HairSettingsPanel'
import { hairApi, type HairState } from './hairApi'

async function encode(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('图片读取失败'))
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1])
    reader.readAsDataURL(file)
  })
}

export function HairReviewPanel() {
  const [state, setState] = useState<HairState | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [pixai, setPixai] = useState('')
  const [crop, setCrop] = useState<number[] | null>(null)
  const [preview, setPreview] = useState('')
  const [media, setMedia] = useState('anime')
  const [visible, setVisible] = useState('unknown')
  const [filter, setFilter] = useState('todo')
  const [jobId, setJobId] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [offset, setOffset] = useState(0)
  const sequence = useRef(0)
  const load = useCallback(async () => {
    const request = ++sequence.current
    try { const next = await hairApi.list(offset, filter, jobId); if (request === sequence.current) setState(next) }
    catch (err) { if (request === sequence.current) setError(err instanceof Error ? err.message : String(err)) }
  }, [offset, filter, jobId])
  useEffect(() => { void load(); return () => { sequence.current++ } }, [load])
  useEffect(() => {
    setCrop(null)
    if (!file) { setPreview(''); return }
    const url = URL.createObjectURL(file); setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file])
  const active = Boolean(state?.active)
  useEffect(() => {
    const timer = window.setInterval(() => void load(), active ? 1500 : 5000)
    return () => window.clearInterval(timer)
  }, [active, load])

  async function start() {
    if (!file) return
    setBusy(true)
    setError('')
    try {
      await hairApi.start({ image_base64: await encode(file), image_name: file.name, pixai_color: pixai,
        crop, media, visible_hair: visible })
      setFilter('all'); setJobId(''); setOffset(0)
      if (filter === 'all' && offset === 0 && !jobId) await load()
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setBusy(false) }
  }

  return (
    <div className="flex flex-col gap-3"><details className="text-sm text-lx-secondary"><summary className="cursor-pointer">发色复核设置</summary><div className="mt-2"><HairSettingsPanel /></div></details>
    <SectionPanel title="发色复核" description="PixAI → MobileNet → CAFormer → 动漫 Torii / 真人 Qwen；仍有争议时人工确认">
      <details className="mb-4 text-xs text-lx-secondary"><summary className="cursor-pointer">单图测试 / 框选头部</summary><div className="mt-3 flex flex-col gap-2">
        <SettingRow label="测试图片" help="多人图可在预览中框选头部，逐个角色处理">
          <input type="file" accept="image/*" aria-label="测试图片" className="lx-input text-xs" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </SettingRow>
        <SettingRow label="PixAI 发色" help="已有结果可直接复用；留空自动运行 PixAI">
          <select aria-label="PixAI 发色" className="lx-input text-xs" value={pixai} onChange={(e) => setPixai(e.target.value)}>
            <option value="">自动识别</option>
            {state?.colors.map((c) => <option key={c.color} value={c.color}>{c.label}</option>)}
          </select>
        </SettingRow>
        <SettingRow label="图片类型">
          <select aria-label="图片类型" className="lx-input text-xs" value={media} onChange={(e) => setMedia(e.target.value)}><option value="anime">动漫 · Torii</option><option value="photo">真人 · Qwen</option><option value="unknown">混合 / 未知 · Qwen</option></select>
        </SettingRow>
        <SettingRow label="可见头发" help="确认有头发后才允许两个分类器直接通过；未知时交给 VLM 复核">
          <select aria-label="可见头发" className="lx-input text-xs" value={visible} onChange={(e) => setVisible(e.target.value)}><option value="unknown">由模型复核</option><option value="yes">确认有头发</option><option value="no">没有头发</option></select>
        </SettingRow>
        {preview && <HairCrop key={preview} src={preview} onChange={setCrop} />}
        <div className="flex flex-wrap items-center gap-2">
          <button className="lx-btn-primary inline-flex items-center gap-1 px-3 text-xs" disabled={!file || busy || active} onClick={() => void start()}>
            {busy && <Loader2 size={12} className="animate-spin" />}开始发色复核
          </button>
          <button className="lx-btn-ghost inline-flex items-center gap-1 text-xs" onClick={() => void load()}><RefreshCw size={12} />刷新</button>
          <span className="text-xs text-lx-muted">批量记录的人工选择会写回标签产物；单图记录独立保存。</span>
        </div>
        {error && <p className="text-xs text-lx-danger">{error}</p>}
      </div></details>
      <div className="mb-3 flex flex-wrap items-center gap-2"><select aria-label="复核状态筛选" className="lx-input w-auto text-xs" value={filter} onChange={(e) => { setFilter(e.target.value); setOffset(0) }}><option value="todo">待处理（分歧 / 失败）</option><option value="all">全部记录</option><option value="review">待人工确认</option><option value="decided">人工已确认</option><option value="accepted">自动通过</option><option value="failed">失败</option><option value="skipped">无可见头发</option></select>
      <input aria-label="按任务编号筛选" placeholder="任务编号（留空查看所有任务）" className="lx-input min-w-0 text-xs sm:w-64" value={jobId} onChange={(e) => { setJobId(e.target.value.trim()); setOffset(0) }} />
      <button className="lx-btn-ghost text-xs" onClick={() => void load()}>刷新记录</button>
      {active && <span className="text-xs text-lx-muted">模型正在处理 {state?.active} 项，可先确认已完成的分歧。</span>}</div>
      {state?.total === 0 && <p className="py-3 text-xs text-lx-muted">{filter === 'todo' ? '当前没有需要人工处理的发色记录。' : '没有符合筛选条件的记录。'}</p>}
      <div className="flex flex-col gap-3">{state?.items.map((item) => <HairReviewCard key={item.id} item={item} colors={state.colors} onChange={() => void load()} />)}</div>
      {state && state.total > 20 && <div className="mt-3 flex items-center justify-end gap-2 text-xs text-lx-muted">
        <span>共 {state.total} 条</span><button className="lx-btn-ghost" disabled={offset === 0} onClick={() => setOffset(offset - 20)}>上一页</button>
        <button className="lx-btn-ghost" disabled={offset + 20 >= state.total} onClick={() => setOffset(offset + 20)}>下一页</button>
      </div>}
    </SectionPanel></div>
  )
}
