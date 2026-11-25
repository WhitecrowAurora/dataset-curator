import { useEffect, useRef, useState } from 'react'
import { SettingRow, SectionPanel } from '../ui/launcher'
import { ToggleRow } from '../ui/controls'
import { FolderPicker } from '../FolderPicker'
import { hairApi, type HairRuntime, type HairSettings } from './hairApi'

export function HairSettingsPanel({ captionMedia }: { captionMedia?: HairSettings['media'] }) {
  const [runtime, setRuntime] = useState<HairRuntime | null>(null)
  const [settings, setSettings] = useState<HairSettings | null>(null)
  const [picker, setPicker] = useState<'torii_dir' | 'qwen_dir' | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const changes = useRef<Partial<HairSettings>>({})
  const load = () => hairApi.settings().then((value) => { setRuntime(value); setSettings({ ...value.settings, ...changes.current }) }).catch((e) => setError(String(e)))
  useEffect(() => { void load() }, [])
  const put = <K extends keyof HairSettings>(key: K, value: HairSettings[K]) => {
    changes.current[key] = value
    setSettings((current) => current && { ...current, [key]: value })
  }
  async function save() {
    if (!settings) return
    setBusy(true); setError(''); setNotice('')
    try {
      const latest = await hairApi.settings()
      const saved = await hairApi.configure({ ...latest.settings, ...changes.current })
      changes.current = {}
      setRuntime(saved); setSettings(saved.settings)
      setNotice('已保存，后续描述与发色复核使用这些模型设置')
    }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  const kinds = captionMedia === 'anime' ? ['torii'] as const : captionMedia === 'photo' ? ['qwen'] as const : ['torii', 'qwen'] as const
  return <SectionPanel title={captionMedia ? '分流描述模型' : '发色复核设置'} description={captionMedia
    ? '动漫使用 Torii，真人使用 Qwen；混合数据逐图选择，无法确定类型时使用 Qwen。模型设置与发色复核共用，修改后请保存。'
    : '动漫由 Torii 补充判断，真人与未知来源由 Qwen 判断；冲突保留给人工确认'}>
    {settings && <fieldset disabled={busy} className="flex min-w-0 flex-col gap-2">
      {!captionMedia && <>
      <SettingRow label="批量任务默认开启"><ToggleRow checked={settings.enabled} onChange={(v) => put('enabled', v)} label="新任务默认复核发色" /></SettingRow>
      <SettingRow label="默认数据类型"><select aria-label="默认数据类型" className="lx-input text-xs" value={settings.media} onChange={(e) => put('media', e.target.value as HairSettings['media'])}><option value="anime">动漫 · Torii</option><option value="photo">真人 · Qwen</option><option value="unknown">混合 / 未知 · 自动判定</option></select></SettingRow>
      <SettingRow label="分类器复核阈值" help="低于此分数继续复核；高分冲突仍需复核，70% 不代表正确率">
        <input aria-label="分类器复核阈值" className="lx-input w-24" type="number" min={0} max={100} value={Math.round(settings.min_confidence * 100)} onChange={(e) => put('min_confidence', Math.min(100, Math.max(0, Number(e.target.value))) / 100)} />
      </SettingRow>
      </>}
      <SettingRow label="内存缓存"><ToggleRow checked={settings.cache_enabled} onChange={(v) => put('cache_enabled', v)} label="按可用内存自动保留模型" /></SettingRow>
      <SettingRow label="缓存上限 / 系统余量" help="单位 GB；0 为自动。内存压力增大时释放空闲模型，正在推理的模型保留">
        <div className="flex flex-wrap gap-2">{(['cache_max_gb', 'memory_reserve_gb'] as const).map((key, i) => <input key={key} aria-label={i ? '系统内存余量' : '模型缓存上限'} className="lx-input w-28" type="number" min={0} value={settings[key]} onChange={(e) => put(key, Math.max(0, Number(e.target.value)))} />)}</div>
      </SettingRow>
      {kinds.map((kind) => `${kind}_dir` as const).map((key) => <SettingRow key={key} label={key === 'torii_dir' ? 'Torii 模型目录' : 'Qwen 模型目录'} help="可选目录或 GGUF 文件；有多个量化版本时直接选权重，对应 mmproj 放在同目录">
        <div className="flex min-w-0 gap-2"><input aria-label={key === 'torii_dir' ? 'Torii 模型目录' : 'Qwen 模型目录'} className="lx-input min-w-0 flex-1 text-xs" value={settings[key]} onChange={(e) => put(key, e.target.value)} /><button className="lx-btn-ghost text-xs" onClick={() => setPicker(key)}>浏览</button></div>
      </SettingRow>)}
      {captionMedia !== 'photo' && <SettingRow label="Torii 引擎" help="默认使用 llama.cpp；native 为实验选项，需要专用推理依赖组，首次补齐官方处理器文件。CUDA 保留量化权重；可选加速依赖减少推理耗时，首图可能编译内核。">
        <select aria-label="Torii 引擎" className="lx-input text-xs" value={settings.torii_engine ?? 'llamacpp'} onChange={(e) => put('torii_engine', e.target.value as HairSettings['torii_engine'])}><option value="llamacpp">llama.cpp（默认）</option><option value="native">native（实验）</option></select>
      </SettingRow>}
      <details className="text-xs text-lx-muted"><summary className="cursor-pointer">使用已有服务</summary><div className="mt-2 flex flex-col gap-2">
        {kinds.map((kind) => <SettingRow key={kind} label={`${kind === 'torii' ? 'Torii' : 'Qwen'} 服务`} help="留空使用本机模型目录；已有服务的显存由其宿主管理">
          <div className="flex min-w-0 flex-wrap gap-2"><input aria-label={`${kind} 服务地址`} className="lx-input min-w-0 flex-1" value={settings[`${kind}_url`]} onChange={(e) => put(`${kind}_url`, e.target.value)} placeholder="http://127.0.0.1:8000" /><input aria-label={`${kind} 服务模型名`} className="lx-input w-36" value={settings[`${kind}_model`]} onChange={(e) => put(`${kind}_model`, e.target.value)} /></div>
        </SettingRow>)}
      </div></details>
      <div className="flex flex-wrap gap-2"><button disabled={busy} className="lx-btn-primary px-3 text-xs" onClick={() => void save()}>保存设置</button><button className="lx-btn-ghost text-xs" onClick={() => void hairApi.release().then(setRuntime).catch((e) => setError(String(e)))}>释放空闲缓存</button><button className="lx-btn-ghost text-xs" onClick={() => void load()}>刷新内存</button></div>
    </fieldset>}
    {!settings && !error && <p className="text-xs text-lx-muted">正在读取模型设置…</p>}
    {runtime && <div className="mt-3 text-xs text-lx-secondary">
      <p>可用内存 {runtime.cache.available_gb} / {runtime.cache.total_gb} GB · 系统余量 {runtime.cache.reserve_gb} GB · 缓存预算 {runtime.cache.budget_gb} GB · 已保留 {runtime.cache.models.length} 个模型</p>
      <div className="mt-2 flex flex-wrap gap-2">{runtime.cache.models.map((m) => <span key={m.name} className="rounded border border-lx-subtle px-2 py-1" title={m.name}>{m.name.split(':').at(-1)} · {m.kind === 'mapped_weights' ? '权重文件缓存' : '内存模型'} · {m.size_gb} GB{m.active ? ' · 使用中' : ''}</span>)}</div>
      <p className="mt-2 text-lx-muted">权重文件缓存仍需重新初始化引擎与传入显存；显示的大小为预算估计，文件页面可由系统回收。</p>
    </div>}
    {error && <p className="mt-2 text-xs text-lx-danger">{error}</p>}{notice && <p className="mt-2 text-xs text-lx-success">{notice}</p>}
    <FolderPicker files={['gguf']} open={picker !== null} start={picker && settings ? settings[picker] : ''} onClose={() => setPicker(null)} onPick={(path) => { if (picker) put(picker, path); setPicker(null) }} />
  </SectionPanel>
}
