import { create } from 'zustand'
import { api, type JobEvent, type Snapshot } from './api'

const MAX_EVENTS = 400
const finished = (snap: Snapshot | null) =>
  Boolean(snap && ['done', 'failed', 'stopped'].includes(snap.status))

export type LiveStore = {
  jobId: string | null
  snap: Snapshot | null
  events: JobEvent[]
  connected: boolean
  polling: boolean
  error: string | null
  attach: (id: string) => void
  detach: () => void
  stop: () => Promise<void>
  pause: () => Promise<void>
  resume: () => Promise<void>
}

// 每个驻留任务页持有自己的 store；连接只能影响创建它的订阅。
export function createLiveStore() {
  let dispose: (() => void) | null = null
  let cursor = 0
  let generation = 0
  return create<LiveStore>((set, get) => ({
    jobId: null, snap: null, events: [], connected: false, polling: false, error: null,

    detach() {
      dispose?.()
      dispose = null
      set({ connected: false, polling: false })
    },

    attach(id) {
      dispose?.()
      generation += 1
      if (get().jobId !== id) {
        cursor = 0
        set({ jobId: id, snap: null, events: [], error: null })
      }
      set({ connected: false, polling: false, error: null })
      let active = true
      let socket: WebSocket | null = null
      let timer: number | null = null
      const close = () => {
        if (!active) return
        active = false
        generation += 1
        if (timer !== null) window.clearTimeout(timer)
        timer = null
        if (socket) {
          socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null
          socket.close()
          socket = null
        }
      }
      dispose = close

      const push = (incoming: JobEvent[] | undefined, snap: Snapshot) => {
        if (!active || snap.id !== id) return
        const fresh = (incoming ?? []).filter((event) => event.seq > cursor)
        if (fresh.length) {
          cursor = Math.max(cursor, ...fresh.map((event) => event.seq))
          set({ snap, events: [...get().events, ...fresh].slice(-MAX_EVENTS), error: null })
        } else set({ snap, error: null })
      }
      const settle = () => {
        close()
        set({ connected: false, polling: false })
      }
      const tick = async () => {
        timer = null
        if (!active) return
        try {
          const data = await api.events(id, cursor)
          if (!active) return
          push(data.events, data.snapshot)
          if (finished(data.snapshot)) {
            settle()
            return
          }
        } catch {
          if (!active) return
          set({ error: '与后端断开，正在重连；显示最后一次状态' })
        }
        if (active) timer = window.setTimeout(() => void tick(), 1000)
      }
      const poll = () => {
        if (!active || timer !== null || get().polling) return
        set({ connected: false, polling: true })
        void tick()
      }
      const disconnected = async () => {
        if (!active) return
        set({ connected: false })
        if (get().snap) {
          if (finished(get().snap)) settle()
          else poll()
          return
        }
        try {
          const snap = await api.job(id)
          if (!active) return
          push(undefined, snap)
          if (finished(snap)) settle()
          else poll()
        } catch {
          if (active) poll()
        }
      }

      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      try {
        const ws = new WebSocket(`${proto}://${location.host}/ws/events?job_id=${encodeURIComponent(id)}`)
        socket = ws
        ws.onopen = () => {
          if (active) set({ connected: true, polling: false, error: null })
        }
        ws.onmessage = (raw) => {
          if (!active) return
          const msg = JSON.parse(raw.data as string) as {
            type: string; events?: JobEvent[]; snapshot?: Snapshot; message?: string
          }
          if (msg.type === 'error') {
            set({ error: msg.message ?? '任务不存在' })
            settle()
            return
          }
          if (msg.snapshot) push(msg.events, msg.snapshot)
          if (msg.type === 'done') settle()
        }
        ws.onclose = () => void disconnected()
      } catch {
        void disconnected()
      }
    },

    async stop() {
      const id = get().jobId
      const subscription = generation
      if (!id) return
      try { await api.stop(id) }
      catch (err) {
        if (generation === subscription) set({ error: err instanceof Error ? err.message : String(err) })
      }
    },
    async pause() {
      const id = get().jobId
      const subscription = generation
      if (!id) return
      try {
        const res = await api.pause(id)
        if (generation === subscription && res.snapshot) set({ snap: res.snapshot })
      } catch (err) {
        if (generation === subscription) set({ error: err instanceof Error ? err.message : String(err) })
      }
    },
    async resume() {
      const id = get().jobId
      const subscription = generation
      if (!id) return
      try {
        const res = await api.resume(id)
        if (generation === subscription && res.snapshot) set({ snap: res.snapshot })
      } catch (err) {
        if (generation === subscription) set({ error: err instanceof Error ? err.message : String(err) })
      }
    },
  }))
}

export const useLive = createLiveStore()
