import { api, type DepState, type ModelCatalog } from '../../lib/api'
import type { SetupDraft } from '../../lib/setup'
import { GGUF_TARGETS as targets } from '../../lib/nlEngines'

export type PreparationProgress = { stage: string; percent: number; logs: string[] }
export const preparationKey = (d: SetupDraft) => [d.mode, d.engine, d.model, d.backend, d.precision].join('|')
const usesGguf = (d: SetupDraft) => d.engine === 'llamacpp' || (d.engine === 'native' && d.precision in targets)
const active = (status?: string) => status === 'queued' || status === 'running'
const finished = (status?: string) => status === 'done' || status === 'ready'

export function weightsReady(d: SetupDraft, catalog: ModelCatalog) {
  const tag = d.mode === 'nl' || d.backend === 'none' || catalog.tag.some((m) => m.backend === d.backend && m.ready)
  const m = catalog.vlm.find((m) => m.key === d.model)
  const nl = d.mode === 'tag' || d.engine === 'existing' || (usesGguf(d)
    ? m?.gguf_ready?.includes(d.precision) : d.precision === 'w4a16' ? m?.quantized : m?.raw_ready)
  return Boolean(tag && nl)
}

export async function prepareModels(d: SetupDraft, token: string, signal: AbortSignal,
  report: (value: PreparationProgress) => void, onCatalog: (value: ModelCatalog) => void) {
  let percent = 0, stage = '准备开始', logs: string[] = []
  let cancelOwned: (() => Promise<unknown>) | undefined
  const check = () => { if (signal.aborted) throw new DOMException('已停止准备', 'AbortError') }
  const emit = (label: string, value: number, lines: string[] = []) => {
    check(); stage = label; percent = Math.max(percent, Math.min(100, value))
    if (lines.length) {
      let overlap = Math.min(logs.length, lines.length)
      while (overlap && !logs.slice(-overlap).every((v, i) => v === lines[i])) overlap--
      logs = [...logs, ...lines.slice(overlap)].slice(-240)
    }
    report({ stage, percent, logs })
  }
  const pause = () => new Promise<void>((resolve, reject) => {
    check()
    const stop = () => { window.clearTimeout(timer); reject(new DOMException('已停止准备', 'AbortError')) }
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', stop); resolve() }, 1200)
    signal.addEventListener('abort', stop, { once: true })
  })
  const refresh = async () => { check(); const c = await api.models(); check(); onCatalog(c); return c }
  const waitDeps = async (initial: DepState, label: string) => {
    let state = initial
    while (state.job?.status === 'running') {
      emit(label, 50, state.job.log); await pause(); state = await api.deps()
    }
    emit(label, 55, state.job?.log)
    if (state.job?.status === 'failed' || state.job?.status === 'cancelled') throw new Error(`${label}失败，请查看控制台日志`)
    return state
  }
  try {
    emit('检查现有权重', 0, ['开始准备：下载模型 → 安装所需依赖 → 转换与量化 → 准备推理运行时'])
    let catalog = await refresh()
    const localNl = d.mode !== 'tag' && d.engine !== 'existing'
    const selected = catalog.vlm.find((m) => m.key === d.model)
    const nlReady = localNl && (usesGguf(d) ? selected?.gguf_ready?.includes(d.precision)
      : d.precision === 'w4a16' ? selected?.quantized : selected?.raw_ready)
    const downloads = [
      ...(localNl && !nlReady && !selected?.raw_ready ? [{ kind: 'vlm', key: d.model }] : []),
      ...(d.mode !== 'nl' && d.backend !== 'none' && !catalog.tag.some((m) => m.backend === d.backend && m.ready)
        ? [{ kind: 'tag', key: d.backend }] : []),
    ]
    for (let i = 0; i < downloads.length; i++) {
      const { kind, key } = downloads[i]
      check()
      const prior = kind === 'tag' ? catalog.tag.find((m) => m.backend === key)?.download : catalog.vlm.find((m) => m.key === key)?.download
      emit(`下载 ${key}`, i / downloads.length * 45, [`下载 ${key}`])
      let task = active(prior?.status) ? prior! : await api.startDownload(kind, key, undefined, token || undefined)
      if (!active(prior?.status)) cancelOwned = () => api.cancelDownload(kind, key)
      while (active(task.status)) {
        emit(`下载 ${key}`, (i + task.percent / 100) / downloads.length * 45,
          [task.current ? `${key}：${task.current}` : task.message].filter(Boolean))
        await pause(); catalog = await refresh()
        const next = kind === 'tag' ? catalog.tag.find((m) => m.backend === key)?.download : catalog.vlm.find((m) => m.key === key)?.download
        if (!next) throw new Error(`${key} 的下载状态不可用，请重试`)
        task = next
      }
      cancelOwned = undefined
      if (!finished(task.status)) throw new Error(task.message || `${key} 下载失败`)
      emit(`下载 ${key} 完成`, (i + 1) / downloads.length * 45, [`${key} 下载完成`])
    }
    catalog = await refresh()
    emit('检查运行依赖', 45, ['检查所选模型需要的依赖'])
    let deps = await api.deps()
    if (deps.job?.status === 'running') deps = await waitDeps(deps, '等待当前依赖安装')
    const m = catalog.vlm.find((v) => v.key === d.model)
    const convert = localNl && usesGguf(d) && !m?.gguf_ready?.includes(d.precision)
    const quantize = localNl && !usesGguf(d) && d.precision === 'w4a16' && !m?.quantized
    const extras = [
      'core',
      ...(d.mode !== 'nl' && d.backend !== 'none' ? ['tag'] : []),
      ...(convert ? ['convert'] : []), ...(localNl && d.engine === 'vllm' ? ['serve'] : []),
      ...(localNl && d.engine === 'native' ? ['native-nl'] : []),
      ...(quantize ? ['quantize'] : []),
      'parquet',
    ]
    for (const extra of extras) {
      check()
      const group = deps.groups.find((g) => (g.extra ?? g.key) === extra)
      if (!group) throw new Error(`找不到 ${extra} 依赖组`)
      if (!group.items.some((v) => v.status === 'missing')) continue
      if (group.blocked) throw new Error(`${group.title} 不支持当前平台`)
      emit(`安装 ${group.title}`, 50, [`安装 ${group.title}`])
      if (group.extra) deps = await waitDeps(await api.depInstallExtra(group.extra), `安装 ${group.title}`)
      else for (const item of group.items.filter((v) => v.status === 'missing')) {
        check(); deps = await waitDeps(await api.depAction(item.name, 'install'), `安装 ${item.name}`)
      }
      if (deps.groups.find((g) => (g.extra ?? g.key) === extra)?.items.some((v) => v.status === 'missing'))
        throw new Error(`${group.title} 仍缺少依赖，请查看安装日志`)
    }
    emit('依赖已准备', 60, ['所需依赖已准备'])
    if (convert || quantize) {
      let task
      if (convert) {
        const state = await api.ggufState(d.model)
        if (!state.available) throw new Error(state.reason)
        task = state.task
        if (!active(task?.status)) { check(); task = await api.ggufStart({ model: d.model, target: targets[d.precision] }); cancelOwned = api.ggufCancel }
        if (task.model !== d.model || task.target !== targets[d.precision]) throw new Error('另一个模型正在转换，请完成后重试')
      } else {
        const state = await api.quantize(d.model, true)
        if (!state.available) throw new Error(state.reason)
        task = state.task
        if (!active(task?.status)) { check(); task = await api.quantizeStart({ model: d.model, method: 'gptq' }); cancelOwned = api.quantizeCancel }
        if (task.model !== d.model) throw new Error('另一个模型正在量化，请完成后重试')
      }
      while (task) {
        emit(task.stage || '量化模型', 60 + task.percent * 0.3, task.log_tail)
        if (!active(task.status)) break
        await pause()
        task = convert ? (await api.ggufState(d.model)).task : (await api.quantize(d.model)).task
      }
      cancelOwned = undefined
      if (!task || !finished(task.status)) throw new Error(task?.message || '量化失败，请查看控制台日志')
      emit('量化完成', 90, [`${d.model} · ${d.precision.toUpperCase()} 权重已准备`])
    }
    if (localNl && d.engine === 'llamacpp') {
      let state = await api.llamaStatus()
      if (!state.installed || state.smoke?.ok === false) {
        check(); const result = await api.installLlama({ force: state.smoke?.ok === false })
        state = result
        if (result.started) cancelOwned = api.cancelLlamaInstall
      }
      while (state.busy) {
        emit('准备 llama.cpp 运行时', 90 + (state.total ? state.done / state.total : 0) * 9,
          [state.message].filter(Boolean))
        await pause(); state = await api.llamaStatus()
      }
      cancelOwned = undefined
      if (!state.installed || state.smoke?.ok === false) throw new Error(state.error || '推理运行时未能安装，请重试')
    }
    catalog = await refresh()
    if (!weightsReady(d, catalog)) throw new Error('所选权重尚未就绪，请查看日志后重试')
    emit('准备完成，可以进入下一步', 100, ['模型、所选精度和运行依赖已准备完成'])
  } catch (error) {
    if (signal.aborted && cancelOwned) await cancelOwned().catch(() => undefined)
    throw error
  }
}
