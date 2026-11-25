import { ChoiceGrid } from '../ui/controls'
import { SettingRow } from '../ui/launcher'
import type { CenterConfig } from '../../lib/api'

export function DownloadNetwork({ mode, address, onMode, onAddress, disabled, notice }: {
  mode: CenterConfig['download_proxy_mode']; address: string
  onMode: (mode: CenterConfig['download_proxy_mode']) => void; onAddress: (address: string) => void
  disabled: boolean; notice: string
}) {
  return <>
    <SettingRow label="分片下载连接">
      <ChoiceGrid options={['auto', 'direct', 'custom', 'center'] as const} value={mode} onChange={onMode}
        disabled={disabled} size="sm" className="grid-cols-2" ariaLabel="分片下载连接方式"
        renderLabel={(value) => ({ auto: '自动（系统代理）', direct: '直连', custom: '指定代理', center: '跟随调度连接' })[value]} />
    </SettingRow>
    {mode === 'custom' && <SettingRow label="下载代理地址" required notice={notice}
      help="HTTP / mixed 端口">
      <input className="lx-input font-mono text-xs" aria-label="下载代理地址" value={address}
        onChange={(event) => onAddress(event.target.value)} disabled={disabled}
        placeholder="http://127.0.0.1:47826" spellCheck={false} aria-invalid={Boolean(notice)} />
    </SettingRow>}
  </>
}
