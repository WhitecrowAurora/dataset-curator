import { create } from 'zustand'
import { api, type JobSpec, type PresetState } from './api'

/**
 * 顶栏「配置方案」：具名快照，可保存 / 切换 / 删除，也可以开自动保存。
 *
 * 为什么放在 store 而不是页面里
 * ---------------------------
 * 控件在顶栏（AppShell），表单却在新建任务页里（NewJob）。两边不是父子，
 * 所以由 NewJob 往这里注册两个钩子：`readForm`（现读当前快照）和
 * `apply`（把一份快照灌回表单）。这样顶栏不需要知道表单长什么样，
 * 加字段也不用改这里。
 *
 * 自动保存
 * -------
 * 节流 1.2s 往上写一次，名字由后端生成（「自动保存 + 时间戳」，开启那一刻定死，
 * 之后原地覆盖）。关闭只是不再写，已有快照留着，随时能切回去。
 */

type Hooks = {
  readForm: () => Record<string, unknown> | null
  apply: (form: Record<string, unknown>, spec: Partial<JobSpec>) => void
}

type PresetsStore = {
  state: PresetState | null
  busy: boolean
  /** 一次性的短提示（已保存 / 已切换），页面顶部小字用 */
  note: string | null
  error: string | null
  register: (hooks: Hooks) => void
  unregister: () => void
  refresh: () => Promise<void>
  saveAs: (name: string) => Promise<boolean>
  switchTo: (name: string) => Promise<boolean>
  remove: (name: string) => Promise<void>
  setAuto: (on: boolean) => Promise<void>
  /** 表单变了：开着自动保存就节流写一次 */
  notifyChanged: (form: Record<string, unknown>, spec?: Partial<JobSpec>) => void
  clearNote: () => void
}

let hooks: Hooks | null = null
let timer: number | null = null
let queued: { form: Record<string, unknown>; spec?: Partial<JobSpec> } | null = null

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export const usePresets = create<PresetsStore>((set, get) => ({
  state: null,
  busy: false,
  note: null,
  error: null,

  register(h) {
    hooks = h
  },

  unregister() {
    hooks = null
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
    queued = null
  },

  async refresh() {
    try {
      set({ state: await api.presets(), error: null })
    } catch (err) {
      set({ error: errText(err) })
    }
  },

  async saveAs(name) {
    const trimmed = name.trim()
    const form = hooks?.readForm()
    if (!trimmed || !form) return false
    set({ busy: true })
    try {
      const state = await api.savePreset({ name: trimmed, form })
      set({ state, busy: false, note: `已保存「${trimmed}」`, error: null })
      return true
    } catch (err) {
      set({ busy: false, error: errText(err) })
      return false
    }
  },

  async switchTo(name) {
    if (!hooks) return false
    set({ busy: true })
    try {
      const got = await api.applyPreset(name)
      hooks.apply(got.form, got.spec)
      set({ state: got.state, busy: false, note: `已切换到「${name}」`, error: null })
      return true
    } catch (err) {
      set({ busy: false, error: errText(err) })
      return false
    }
  },

  async remove(name) {
    set({ busy: true })
    try {
      // 删除给一句回执：删除按钮是常驻的（以前悬停才显形，没人发现），
      // 点完没有任何反馈的话，用户会怀疑是不是没点中而不停重试。
      set({
        state: await api.deletePreset(name),
        busy: false,
        error: null,
        note: `已删除「${name}」`,
      })
    } catch (err) {
      set({ busy: false, error: errText(err) })
    }
  },

  async setAuto(on) {
    const form = on ? hooks?.readForm() ?? undefined : undefined
    set({ busy: true })
    try {
      const state = await api.autoPreset(on ? { enabled: true, form } : { enabled: false })
      set({
        state,
        busy: false,
        error: null,
        note: on ? `自动保存已开启：${state.auto_name ?? ''}` : '自动保存已关闭',
      })
    } catch (err) {
      set({ busy: false, error: errText(err) })
    }
  },

  notifyChanged(form, spec) {
    if (!get().state?.auto) return
    queued = { form, spec }
    if (timer !== null) return
    timer = window.setTimeout(async () => {
      timer = null
      const payload = queued
      queued = null
      if (!payload) return
      try {
        const state = await api.autoPreset(payload)
        usePresets.setState({ state })
      } catch {
        // 自动保存失败不出声：它只是兜底，喊出来反而打断输入
      }
    }, 1200)
  },

  clearNote() {
    set({ note: null })
  },
}))
