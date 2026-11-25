import { json } from './api'

export type BenchmarkConfig = { model: string; engine: string; precision: string }
export type BenchmarkRequest = {
  configurations: BenchmarkConfig[]; device: string;
  source: string; output_dir: string; levels: number[]; sample_count: number
}
export type BenchmarkOptions = {
  models: { key: string; title: string; variants: { engine: string; precisions: string[] }[] }[]
  devices: { value: string; name: string; vram_gb: number }[]
  recent_source: string
}
export type BenchmarkRow = {
  series_id?: string; requested: number; actual?: number; status: string; successful: number; failed: number
  rate_per_s?: number; peak_vram_mb?: number; elapsed_s?: number; prepare_s?: number; warmup_s?: number
  warning?: string; error?: string; log_path?: string
}
export type BenchmarkState = {
  running: boolean; status: string; phase?: string; message: string; error?: string
  request?: BenchmarkRequest; sample_count?: number; recommended?: number; current?: number
  rows: BenchmarkRow[]; report_path?: string; progress?: { done?: number; total?: number }
  series?: BenchmarkSeries[]
}
export type BenchmarkSeries = BenchmarkConfig & { id: string; recommended?: number; peak_rate_per_s?: number }
export const SERIES_COLORS = ['var(--lx-accent)', '#36a9a1', '#9c87df', '#d9a342']

export function configurations(request?: BenchmarkRequest): BenchmarkConfig[] {
  if (!request) return []
  if (request.configurations) return request.configurations
  const old = request as BenchmarkRequest & BenchmarkConfig
  return old.model ? [{ model: old.model, engine: old.engine, precision: old.precision }] : []
}

export function resultSeries(state: BenchmarkState | null): BenchmarkSeries[] {
  return state?.series ?? configurations(state?.request).map((c, i) => ({ ...c, id: `config-${i}`, recommended: state?.recommended }))
}

export function readyConfig(options: BenchmarkOptions | null, value?: Partial<BenchmarkConfig>): BenchmarkConfig {
  const model = options?.models.find(m => m.key === value?.model) ?? options?.models[0]
  const variant = model?.variants.find(v => v.engine === value?.engine) ?? model?.variants[0]
  return { model: model?.key ?? '', engine: variant?.engine ?? '',
    precision: variant?.precisions.includes(value?.precision ?? '') ? value!.precision! : variant?.precisions[0] ?? '' }
}

export function nextConfig(options: BenchmarkOptions | null, selected: BenchmarkConfig[]): BenchmarkConfig {
  const all = options?.models.flatMap(m => m.variants.flatMap(v => v.precisions.map(p => ({ model: m.key, engine: v.engine, precision: p })))) ?? []
  const available = all.filter(c => !selected.some(s => s.model === c.model && s.engine === c.engine && s.precision === c.precision))
  return available.find(c => c.model === selected[0]?.model && c.engine !== selected[0]?.engine && c.precision === selected[0]?.precision)
    ?? available.find(c => c.model !== selected[0]?.model) ?? available[0] ?? readyConfig(options)
}

async function request<T>(path = '', body?: unknown): Promise<T> {
  return json<T>('/api/benchmark' + path, body === undefined ? undefined : {
    method: 'POST', body: JSON.stringify(body),
  })
}

export const benchmark = {
  options: () => request<BenchmarkOptions>('/options'),
  status: () => request<BenchmarkState>(),
  start: (value: BenchmarkRequest) => request<BenchmarkState>('/start', value),
  stop: () => request<BenchmarkState>('/stop', {}),
}

export function parseLevels(text: string): number[] {
  const tokens = text.trim().split(/[\s,，;；]+/).filter(Boolean)
  if (!tokens.length || tokens.length > 32 || tokens.some(t => !/^\d+$/.test(t) || Number(t) < 1 || Number(t) > 256)) {
    throw new Error('请输入 1～256 的整数，最多 32 档，用逗号或空格分隔')
  }
  return [...new Set(tokens.map(Number))].sort((a, b) => a - b)
}
