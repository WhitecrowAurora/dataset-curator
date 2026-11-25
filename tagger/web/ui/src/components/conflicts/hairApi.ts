import { json } from '../../lib/api'

export type HairColor = { color: string; label: string; swatch: string }
export type HairCandidate = HairColor & { probability: number; preference?: number }
export type HairReview = {
  id: string
  image_name: string
  status: 'pending' | 'running' | 'accepted' | 'review' | 'decided' | 'failed' | 'skipped'
  selected: string[]
  reason: string
  crop: number[] | null
  candidates: HairCandidate[]
  observations: { model: string; color: string; probability?: number; error?: string; reason?: string; source?: string; characters?: { name: string; description: string; colors: string[]; nonhuman: boolean }[] }[]
  elapsed_ms?: number
  media?: string
  job_id?: string
  writeback_error?: string
  extra_colors?: string[]
  no_hair?: boolean
  parent_id?: string
  role_count?: number
  settled_roles?: number
}
export type HairSettings = {
  enabled: boolean; media: 'anime' | 'photo' | 'unknown'; min_confidence: number
  cache_enabled: boolean; cache_max_gb: number; memory_reserve_gb: number
  torii_dir: string; qwen_dir: string; torii_url: string; torii_model: string; qwen_url: string; qwen_model: string
  torii_engine: 'llamacpp' | 'native'
}
export type HairRuntime = { settings: HairSettings; cache: {
  total_gb: number; available_gb: number; reserve_gb: number; budget_gb: number; retained_gb: number
  models: { name: string; kind: string; size_gb: number; active: boolean }[]
} }
export type HairState = { items: HairReview[]; total: number; active: number; colors: HairColor[] }
export function probabilityText(value: number) {
  const percent = value * 100
  return percent > 0 && percent < 0.1 ? '<0.1%' : `${percent.toFixed(1)}%`
}
const BASE = '/api/conflicts/hair'
export const hairApi = {
  list: (offset = 0, status = 'all', jobId = '') => json<HairState>(`${BASE}?offset=${offset}&limit=20&status=${encodeURIComponent(status)}&job_id=${encodeURIComponent(jobId)}`),
  start: (body: { image_base64: string; image_name: string; pixai_color: string; crop: number[] | null; media: string; visible_hair: string }) =>
    json<HairReview>(BASE, { method: 'POST', body: JSON.stringify(body) }),
  decide: (id: string, colors: string[], noHair = false) => json<HairReview>(`${BASE}/${id}/decide`, { method: 'POST', body: JSON.stringify({ colors, no_hair: noHair }) }),
  settings: () => json<HairRuntime>(`${BASE}/settings`),
  configure: (body: HairSettings) => json<HairRuntime>(`${BASE}/settings`, { method: 'PUT', body: JSON.stringify(body) }),
  release: () => json<HairRuntime>(`${BASE}/cache/release`, { method: 'POST' }),
  crop: (id: string, crop: number[]) => json<HairReview>(`${BASE}/${id}/crop`, { method: 'POST', body: JSON.stringify({ crop }) }),
  retry: (id: string) => json<HairReview>(`${BASE}/${id}/retry`, { method: 'POST' }),
  roles: (id: string) => json<HairReview[]>(`${BASE}/${id}/roles`),
  mergeRoles: (id: string) => json<HairReview>(`${BASE}/${id}/merge-roles`, { method: 'POST' }),
  image: (id: string, crop = false) => `${BASE}/${id}/image${crop ? '?cropped=true' : ''}`,
}
