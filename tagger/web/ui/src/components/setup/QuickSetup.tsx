import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ArrowLeft, ArrowRight, CheckCircle2, Loader2, RotateCcw, Wand2, X } from 'lucide-react'
import { api, type ModelCatalog, type SetupState } from '../../lib/api'
import { loadSetup, saveSetup, setupError, SETUP_STEPS, PRECISION_LABELS, type SetupData, type SetupDraft } from '../../lib/setup'
import { ErrorBox, ToggleRow } from '../ui/controls'
import { Field, SetupFields } from './SetupFields'
import { ModelPreparation } from './ModelPreparation'
import { ServiceConnection } from './ServiceConnection'
import { ModelFields, SetupDevices } from './ModelFields'
import { useModelPreparation } from './useModelPreparation'
import { preparationKey, weightsReady } from './preparation'

export function QuickSetup({ onSaved, onOpen, onOpenChange }: {
  onSaved: (form: Record<string, unknown>) => void; onOpen: (path: string) => void;
  onOpenChange: (open: boolean) => void;
}) {
  const [state, setState] = useState<SetupState | null>(null)
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<SetupData | null>(null)
  const [draft, setDraft] = useState<SetupDraft | null>(null)
  const [step, setStep] = useState(0)
  const [editing, setEditing] = useState(true)
  const [token, setToken] = useState('')
  const [secret, setSecret] = useState('')
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)
  const [error, setError] = useState('')
  const dialog = useRef<HTMLDivElement>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  const cancelPreparation = useRef<() => void>(() => undefined)

  useEffect(() => { onOpenChange(open) }, [open, onOpenChange])

  useEffect(() => {
    let alive = true
    api.setup().then((value) => {
      if (!alive) return
      setState(value)
      if (!value.completed && !value.dismissed) setOpen(true)
    }).catch(() => { if (alive) setError('无法读取快速配置，请确认后端已启动') })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!open) return
    let alive = true
    setLoading(true); setError(''); setData(null); setDraft(null)
    loadSetup().then((value) => {
      if (!alive) return
      setState(value.state); setData(value); setDraft(value.draft)
      setEditing(!value.state.completed); setStep(0)
    }).catch((e) => { if (alive) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, reload])

  const close = useCallback(() => {
    cancelPreparation.current()
    setOpen(false); setToken(''); setSecret('')
    if (state && !state.completed && !state.dismissed) {
      const next = { ...state, dismissed: true }
      setState(next)
      api.saveSetup(next).catch(() => setError('暂时无法保存关闭状态，下次打开可能再次提示'))
    }
  }, [state])

  useEffect(() => {
    if (!open) return
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    dialog.current?.focus()
    const key = (e: KeyboardEvent) => {
      // 目录选择与量化弹窗有自己的键盘处理，打开时把控制权交给它们。
      if (document.querySelector('[role="dialog"][aria-modal="true"]:not([data-quick-setup])')) return
      if (e.key === 'Escape' && !busy) { e.preventDefault(); close() }
      if (e.key !== 'Tab') return
      const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]') ?? [])
        .filter((v) => v.getClientRects().length)
      const first = controls[0], last = controls[controls.length - 1]
      if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
        e.preventDefault(); last?.focus()
      } else if (!e.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) {
        e.preventDefault(); first?.focus()
      }
    }
    window.addEventListener('keydown', key)
    return () => { window.removeEventListener('keydown', key); returnFocus.current?.focus() }
  }, [open, busy, close])

  const onCatalog = useCallback((catalog: ModelCatalog) => setData((v) => v ? { ...v, catalog } : v), [])
  const saveDownloadSettings = async () => {
    if (!draft || !data) return
    if (token.trim() && remember) {
      await api.saveHfToken({ token: token.trim(), repo: '', remember: true, scope: 'global' })
      setToken(''); setData((v) => v ? { ...v, tokenSet: true } : v)
    }
    if (draft.endpoint !== data.catalog.endpoint) await api.saveEndpoint(draft.endpoint.trim())
    if (draft.proxy !== data.catalog.proxy) await api.saveProxy(draft.proxy.trim())
  }
  const preparation = useModelPreparation(open ? draft : null, saveDownloadSettings, onCatalog, token)
  cancelPreparation.current = preparation.cancel
  const prepared = Boolean(data && draft && !preparation.error && weightsReady(draft, data.catalog)
    && (preparation.ready || (data.state.completed && preparationKey(draft) === preparationKey(data.draft))))
  const finish = async () => {
    if (!data || !draft) return
    setBusy(true); setError('')
    try {
      await saveDownloadSettings()
      const result = await saveSetup(data, draft, secret, remember ? '' : token)
      setState(result.state); setData({ ...data, draft, state: result.state, tokenSet: data.tokenSet || (remember && Boolean(token.trim())) })
      setEditing(false); if (remember) setToken(''); setSecret(''); onSaved(result.form)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  const next = async () => {
    if (!draft) return
    const message = setupError(draft, step)
    if (message) { setError(message); dialog.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(); return }
    setError('')
    if (step === 0) {
      setBusy(true)
      try { await saveDownloadSettings(); setStep(1) }
      catch (e) { setError(e instanceof Error ? e.message : String(e)) }
      finally { setBusy(false) }
    } else setStep(step + 1)
  }
  const updateDraft = (value: SetupDraft) => {
    if (value.engine === 'llamacpp' && value.precision === 'q8'
      && !data?.catalog.vlm.find((m) => m.key === value.model)?.gguf_ready?.includes('q8')) {
      value = { ...value, precision: 'q6' }
    }
    setDraft(value)
    setError('')
  }
  const summary = draft ? [
    data?.tokenSet ? 'Token 已保存' : '未保存 Token · 公开资源可直接访问',
    `${draft.mode === 'tag' ? '只出 TAG' : draft.mode === 'nl' ? '只出自然语言' : 'TAG + 自然语言'} · ${draft.mode === 'tag' ? draft.backend : draft.engine === 'existing' ? '已有服务' : draft.engine === 'native' ? 'lulynx 专用' : draft.engine === 'llamacpp' ? 'llama.cpp' : 'vLLM'}`,
    [draft.mode !== 'nl' ? `${draft.backend} · ${draft.tagPrecision.toUpperCase()}` : '',
      draft.mode !== 'tag' ? draft.engine === 'existing' ? '已有服务' : `${draft.model} · ${PRECISION_LABELS[draft.precision] ?? draft.precision}` : ''].filter(Boolean).join(' / '),
    draft.usage === 'center' ? `调度中心 · ${draft.userId}` : `本地打标与打包 · ${draft.packFormat}`,
  ] : []

  return <>
    <button type="button" onClick={() => setOpen(true)}
      className="flex w-full items-center gap-2 rounded-lx-sm border border-lx-accent/25 bg-lx-accent-subtle px-3 py-2.5 text-left transition-colors hover:border-lx-accent"
      aria-label="打开快速配置向导">
      <Wand2 size={16} className="shrink-0 text-lx-accent" />
      <span className="min-w-0 flex-1"><span className="block text-xs font-medium text-lx">快速配置向导</span>
        <span className="mt-0.5 block text-[10px] text-lx-muted">{state?.completed ? '已配置 · 点击查看' : '几步完成基础设置'}</span></span>
      {state?.completed && <CheckCircle2 size={14} className="shrink-0 text-lx-success" />}
    </button>
    {!open && error && <p className="mt-1 text-[10px] text-lx-warning">{error}</p>}
    {open && createPortal(<div className="fixed inset-0 z-[9000] flex items-center justify-center bg-black/55 p-3 backdrop-blur-sm sm:p-6">
      <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="quick-setup-title" tabIndex={-1}
        data-quick-setup className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-lx-lg border border-lx bg-lx-surface shadow-2xl outline-none">
        <header className="flex shrink-0 items-center justify-between gap-4 border-b border-lx px-5 py-4 sm:px-7">
          <div><h2 id="quick-setup-title" className="flex items-center gap-2 text-lg font-semibold text-lx"><Wand2 size={20} className="text-lx-accent" />快速配置向导</h2>
            <p className="mt-1 text-xs text-lx-muted">{editing ? '完成基础设置，让助手准备好开始工作。' : '基础配置已保存，可查看当前设置或重新配置。'}</p></div>
          <button className="lx-btn-ghost-icon" type="button" aria-label="关闭快速配置向导" disabled={busy} onClick={close}><X size={18} /></button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-7">
          {loading ? <p className="flex items-center gap-2 text-sm text-lx-muted"><Loader2 size={16} className="animate-spin" />读取当前配置…</p> : <>
            {error && <div className="mb-4"><ErrorBox error={error} /></div>}
            {!draft && <button type="button" className="lx-btn-ghost" onClick={() => setReload((v) => v + 1)}>重新读取</button>}
            {draft && data && <>
              {editing ? <>
                <ol className="mb-6 grid grid-cols-4 gap-2" aria-label="配置步骤">{SETUP_STEPS.map((label, i) =>
                  <li key={label} className={`border-t-2 pt-2 text-[11px] ${i <= step ? 'border-lx-accent text-lx' : 'border-lx text-lx-dim'}`}>
                    <span className="mb-1 block font-mono">{i < step ? '✓' : `0${i + 1}`}</span>{label}</li>)}</ol>
                <h3 className="mb-5 text-base font-semibold text-lx">{SETUP_STEPS[step]}</h3>
                {step === 0 && <div className="mb-6 space-y-3">
                  <Field label="Hugging Face Token（可选）" hint="私有或受限模型、分片需要；公开资源可以留空。">
                    <input className="lx-input font-mono" aria-label="Hugging Face Token" type="password" autoComplete="off" disabled={busy}
                      placeholder={data.tokenSet ? '已配置，留空沿用' : 'hf_…'} value={token} onChange={(e) => setToken(e.target.value)} />
                  </Field>
                  <ToggleRow checked={remember} onChange={setRemember} disabled={busy} label="在本机记住 Token"
                    hint="供模型下载与 HF 分片共用；只向所选 Hugging Face 下载源发送，保存后不回显。" />
                  {data.tokenSet && <button type="button" className="lx-btn-ghost text-xs" disabled={busy} onClick={async () => {
                    setBusy(true)
                    try { await api.saveHfToken({ token: '', repo: '', remember: false }); setData({ ...data, tokenSet: false }) }
                    catch (e) { setError(String(e)) } finally { setBusy(false) }
                  }}>清除已保存 Token</button>}
                </div>}
                {step === 2 ? <>
                  <fieldset disabled={busy || preparation.running} className="min-w-0"><ModelFields data={data} value={draft} onChange={updateDraft} /></fieldset>
                  <ModelPreparation preparation={preparation} />
                  <fieldset disabled={busy || preparation.running} className="min-w-0"><SetupDevices data={data} value={draft} onChange={updateDraft} /></fieldset>
                </> : <fieldset disabled={busy} className="min-w-0"><SetupFields step={step} data={data} value={draft}
                  onChange={updateDraft} secret={secret} onSecret={setSecret} /></fieldset>}
              </> : <>
                <div className="mb-5 flex items-center gap-3 rounded-lx-sm bg-lx-success/10 p-4 text-lx-success">
                  <CheckCircle2 size={22} /><div><p className="text-sm font-semibold">基础配置已完成</p>
                    <p className="mt-1 text-xs">下面的设置会在任务与调度页面中直接生效。</p></div>
                </div>
                <ul className="divide-y divide-lx">{SETUP_STEPS.map((label, i) => <li key={label} className="flex items-center gap-3 py-3">
                  <CheckCircle2 size={16} className="shrink-0 text-lx-success" /><div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-lx">{label}</p><p className="mt-1 break-all text-xs text-lx-muted">{summary[i]}</p></div>
                  <span className="shrink-0 text-xs text-lx-success">已配置</span></li>)}</ul>
                <p className="mt-4 break-all text-xs text-lx-muted">产物目录：{draft.output}</p>
                <button type="button" className="lx-btn-ghost mt-5 inline-flex items-center gap-2"
                  onClick={() => { setEditing(true); setStep(0); setError('') }}><RotateCcw size={14} />重新配置</button>
                <ServiceConnection draft={draft} />
              </>}
            </>}
          </>}
        </div>
        <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-lx px-5 py-4 sm:px-7">
          <button type="button" className="lx-btn-ghost inline-flex items-center gap-2" disabled={busy || preparation.running}
            onClick={() => editing && step > 0 ? (setStep(step - 1), setError('')) : close()}>
            {editing && step > 0 ? <><ArrowLeft size={14} />上一步</> : '关闭'}</button>
          {!loading && draft && (editing ? <button type="button" className="lx-btn-primary inline-flex items-center gap-2"
            disabled={busy || (step === 2 && (!prepared || preparation.running))} onClick={() => void (step === 3 ? finish() : next())}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <ArrowRight size={14} />}
            {step === 3 ? '保存基础配置' : '下一步'}</button> :
            <button type="button" className="lx-btn-primary inline-flex items-center gap-2" onClick={() => {
              close(); onOpen(draft.usage === 'center' ? '/center' : '/')
            }}>前往{draft.usage === 'center' ? '调度中心' : '新建任务'}<ArrowRight size={14} /></button>)}
        </footer>
      </div>
    </div>, document.body)}
  </>
}
