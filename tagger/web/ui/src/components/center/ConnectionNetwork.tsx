import { ChoiceGrid } from '../ui/controls'
import { SettingRow } from '../ui/launcher'

export type ProxyMode = 'auto' | 'direct' | 'custom'

export function ConnectionNetwork({ mode, address, onMode, onAddress, disabled, notice }: {
  mode: ProxyMode; address: string; onMode: (mode: ProxyMode) => void
  onAddress: (address: string) => void; disabled: boolean; notice: string
}) {
  return <>
    <SettingRow label="调度连接方式">
      <ChoiceGrid options={['auto', 'direct', 'custom'] as const} value={mode} onChange={onMode}
        disabled={disabled} size="sm" className="grid-cols-3" ariaLabel="调度连接方式"
        renderLabel={(value) => ({ auto: '自动（系统代理）', direct: '直连', custom: '指定代理' })[value]} />
    </SettingRow>
    {mode === 'custom' && <SettingRow label="代理地址" required notice={notice}
      help="HTTP / mixed 端口">
      <input className="lx-input font-mono text-xs" aria-label="调度代理地址"
        value={address} onChange={(e) => onAddress(e.target.value)} disabled={disabled}
        placeholder="http://127.0.0.1:47826" spellCheck={false} aria-invalid={Boolean(notice)} />
    </SettingRow>}
  </>
}
