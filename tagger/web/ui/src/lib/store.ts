import { create } from 'zustand'
import { api, type JobEvent, type Snapshot } from './api'

// 一个页面只需要订阅一个任务，所以这里不做任务表——路由决定看哪个，
// 换路由就 detach 重连。任务表由 /api/jobs 提供（首页用）。

const MAX_EVENTS = 400

async function stopJob(id: string) {
  await api.stop(id)
}

export type LiveStore = {
  jobId: string | null
  snap: Snapshot | null
  events: JobEvent[]
  connected: boolean
  /** ws 断开后是否已退回轮询——页面上要能看出"数据可能慢一拍" */
  polling: boolean
  error: string | null
  attach: (id: string) => void
  detach: () => void
  stop: () => Promise<void>
}

let socket: WebSocket | null = null
let timer: number | null = null
let cursor = 0

function closeAll() {
  if (socket) {
    socket.onclose = null
    socket.close()
    socket = null
  }
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

export const useLive = create<LiveStore>((set, get) => ({
  jobId: null,
  snap: null,
  events: [],
  connected: false,
  polling: false,
  error: null,

  detach() {
    closeAll()
    cursor = 0
    set({ jobId: null, snap: null, events: [], connected: false, polling: false })
  },

  attach(id) {
    closeAll()
    cursor = 0
    set({ jobId: id, snap: null, events: [], connected: false, polling: false })

    const push = (incoming: JobEvent[] | undefined, snap: Snapshot) => {
      if (!incoming?.length) {
        set({ snap })
        return
      }
      cursor = Math.max(cursor, incoming[incoming.length - 1].seq)
      const merged = [...get().events, ...incoming]
      set({
        snap,
        events: merged.length > MAX_EVENTS ? merged.slice(merged.length - MAX_EVENTS) : merged,
      })
    }

    const poll = () => {
      if (timer !== null) return
      set({ polling: true })
      timer = window.setInterval(async () => {
        try {
          const data = await api.events(id, cursor)
          push(data.events, data.snapshot)
          if (['done', 'failed', 'stopped'].includes(data.snapshot.status)) {
            closeAll()
            set({ polling: false })
          }
        } catch {
          // 后端没了（关服务/换端口）：停轮询，页面上留最后一次快照
          closeAll()
          set({ polling: false, error: '与后端断开，正在显示最后一次状态' })
        }
      }, 1000)
    }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws/events?job_id=${id}`)
    socket = ws

    ws.onopen = () => set({ connected: true, polling: false })
    ws.onmessage = (raw) => {
      const msg = JSON.parse(raw.data as string) as {
        type: string
        events?: JobEvent[]
        snapshot?: Snapshot
        message?: string
      }
      if (msg.type === 'error') {
        set({ error: msg.message ?? '任务不存在' })
        closeAll()
        return
      }
      if (msg.snapshot) push(msg.events, msg.snapshot)
      if (msg.type === 'done') {
        closeAll()
        set({ connected: false })
      }
    }
    ws.onclose = () => {
      if (get().jobId !== id) return
      set({ connected: false })
      const status = get().snap?.status
      // 还在跑就退回轮询：本地服务断线极罕见，但"页面不动了"最难自证，
      // 留个兜底比事后解释便宜。
      if (status && !['done', 'failed', 'stopped'].includes(status)) poll()
      else if (!status) {
        api
          .job(id)
          .then((snap) => set({ snap }))
          .catch(() => set({ error: '连不上后端' }))
      }
    }
  },

  async stop() {
    const id = get().jobId
    if (!id) return
    try {
      await stopJob(id)
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) })
    }
  },
}))
