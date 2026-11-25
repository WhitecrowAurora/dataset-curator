import type { CenterState } from '../../lib/api'

/** 实际分片的处理阶段优先于本机配置，避免只跑 NL 时展示 TAG 参数。 */
export function executionMode(state: CenterState | null) {
  const p = state?.running ? state.progress : undefined
  const mode = p?.package && ['tag', 'nl', 'both'].includes(p.phase ?? '')
    ? p.phase : state?.runtime.resolved.mode ?? state?.config.mode
  return {
    showNl: mode !== 'tag',
    showTag: mode !== 'nl' && (state?.runtime.resolved.tag_backend ?? state?.config.tag_backend) !== 'none',
  }
}
