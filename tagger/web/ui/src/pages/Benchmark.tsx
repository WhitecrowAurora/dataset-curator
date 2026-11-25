import { useEffect, useRef, useState } from 'react'
import { FolderOpen, Gauge, Play, RefreshCw, SlidersHorizontal, Square } from 'lucide-react'
import { benchmark, configurations, nextConfig, readyConfig, type BenchmarkConfig, type BenchmarkOptions, type BenchmarkState } from '../lib/benchmark'
import { parseLevels } from '../lib/benchmark'
import { useFolderPicker } from '../components/FolderPicker'
import { BenchmarkConfigs } from '../components/benchmark/BenchmarkConfigs'
import { BenchmarkResults } from '../components/benchmark/BenchmarkResults'
import '../components/benchmark/benchmark.css'

export function Benchmark() {
  const [options, setOptions] = useState<BenchmarkOptions | null>(null)
  const [state, setState] = useState<BenchmarkState | null>(null)
  const [configs, setConfigs] = useState<BenchmarkConfig[]>([])
  const [device, setDevice] = useState('auto')
  const [source, setSource] = useState('')
  const [output, setOutput] = useState('')
  const [levels, setLevels] = useState('1, 2, 4, 6, 8')
  const [count, setCount] = useState(24)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const seeded = useRef(false)
  const folder = useFolderPicker()
  const locked = busy || !loaded || Boolean(state?.running)
  const usesVllm = configs.some(c => c.engine === 'vllm')
  const refresh = async () => {
    setBusy(true)
    try { setOptions(await benchmark.options()); setError('') }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  useEffect(() => { void refresh() }, [])
  useEffect(() => { if (options && !options.recent_source) setSettingsOpen(true) }, [options])
  useEffect(() => {
    let alive = true
    let timer = 0
    const poll = async () => {
      try {
        const next = await benchmark.status()
        if (!alive) return
        setState(next); setPollError(''); setLoaded(true)
        if (!seeded.current) {
          seeded.current = true
          if (next.request) {
            setConfigs(configurations(next.request))
            setDevice(next.request.device); setSource(next.request.source); setLevels(next.request.levels.join(', ')); setCount(next.request.sample_count)
            setOutput(next.request.output_dir ?? '')
          }
        }
      } catch (e) { if (alive) setPollError(String(e)) }
      if (alive) timer = window.setTimeout(poll, 1200)
    }
    void poll()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [])
  useEffect(() => {
    if (!options || !loaded || state?.running) return
    setConfigs(current => {
      if (current.length) return current.map(c => readyConfig(options, c))
      const first = readyConfig(options)
      const second = nextConfig(options, [first])
      return first.model && JSON.stringify(first) !== JSON.stringify(second) ? [first, second] : [first]
    })
  }, [options, loaded, state?.running])
  useEffect(() => { if (usesVllm && device === 'cpu') setDevice('auto') }, [usesVllm, device])
  const act = async (stop = false) => {
    setBusy(true); setError('')
    try {
      if (!stop && (!Number.isInteger(count) || count < 4 || count > 1024)) throw new Error('样本数量请选择 4～1024')
      setState(stop ? await benchmark.stop() : await benchmark.start({ configurations: configs, device, source, output_dir: output, levels: parseLevels(levels), sample_count: count }))
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  let requested: number[] = []
  let levelError = ''
  try { requested = parseLevels(levels) } catch (e) { levelError = (e as Error).message }
  const duplicates = new Set(configs.map(c => JSON.stringify(c))).size !== configs.length
  const invalid = !configs.length || configs.some(c => !c.model || !c.precision) || duplicates || Boolean(levelError) || (!source && !options?.recent_source)
  const done = state?.rows.filter(r => ['completed', 'failed', 'skipped', 'canceled'].includes(r.status)).length ?? 0
  const total = state?.request ? configurations(state.request).length * state.request.levels.length : 0
  return <div className="bn-page">
    <header className="bn-page-head"><div><div className="bn-eyebrow">LULYNX / BENCHMARK</div><h1><Gauge size={25} />并发测试</h1></div>
      <button className="lx-btn-ghost" disabled={locked} onClick={refresh}><RefreshCw size={14} />刷新模型</button>
    </header>
    {(error || pollError || state?.error) && <div role="alert" className="bn-alert">{error || pollError || state?.error}</div>}
    <BenchmarkConfigs options={options} values={configs} locked={locked} onChange={setConfigs} />
    {(!options?.models.length && options) && <p className="text-sm text-lx-warning">请在模型管理准备权重，并在依赖检测中检查推理引擎。</p>}
    {duplicates && <p role="alert" className="text-sm text-lx-warning">对比配置重复，请选择不同模型、引擎或精度。</p>}
    <section className="bn-controls">
      <div className="bn-run-row"><label className="bn-field bn-levels"><span>并发档位</span><input className="lx-input font-mono" aria-label="测试档位" disabled={locked} value={levels} onChange={e => setLevels(e.target.value)} /></label>
        <div className="bn-presets"><button disabled={locked} onClick={() => setLevels('1, 2, 4, 6, 8')}>默认档位</button><button disabled={locked} onClick={() => setLevels('1, 2, 4, 6, 8, 10, 12, 14, 16, 20, 24')}>扩展到 24</button></div>
        {state?.running ? <button className="lx-btn-ghost bn-run" disabled={busy} onClick={() => act(true)}><Square size={15} />停止测试</button>
          : <button className="lx-btn-primary bn-run" disabled={locked || invalid} onClick={() => act()}><Play size={15} />{configs.length > 1 ? '开始对比' : '开始测试'}</button>}
      </div>
      {levelError && <p className="bn-footnote text-lx-warning">{levelError}</p>}
      <div className="bn-plan"><span>{configs.length} 组配置 × {requested.length} 档并发</span><span>每组共用 {Math.max(count || 0, ...requested)} 张样本</span><span>自动切换引擎</span></div>
      <details className="bn-settings" open={settingsOpen} onToggle={e => setSettingsOpen(e.currentTarget.open)}><summary><SlidersHorizontal size={14} />样本与测试设置<span>{source || options?.recent_source || '请选择样本目录'}</span></summary>
        <div className="bn-settings-grid">
          <label className="bn-field"><span>样本目录或分片</span><div className="bn-input-action"><input className="lx-input" aria-label="样本路径" placeholder="留空使用最近下载分片" value={source} disabled={locked} onChange={e => setSource(e.target.value)} /><button className="lx-btn-ghost" disabled={locked} aria-label="浏览样本" onClick={() => folder.pick(source, setSource, ['parquet'])}><FolderOpen size={15} /></button></div></label>
          <label className="bn-field"><span>测试结果目录</span><div className="bn-input-action"><input className="lx-input" aria-label="测试结果目录" placeholder="默认测试目录" value={output} disabled={locked} onChange={e => setOutput(e.target.value)} /><button className="lx-btn-ghost" disabled={locked} aria-label="浏览结果目录" onClick={() => folder.pick(output, setOutput)}><FolderOpen size={15} /></button></div></label>
          <label className="bn-field"><span>测试设备</span><select className="lx-select" aria-label="测试设备" disabled={locked} value={device} onChange={e => setDevice(e.target.value)}><option value="auto">自动</option>{!usesVllm && <option value="cpu">CPU</option>}{options?.devices.map(d => <option key={d.value} value={d.value}>{d.value} · {d.name} · {d.vram_gb} GB</option>)}</select></label>
          <label className="bn-field"><span>样本数量</span><input type="number" className="lx-input" aria-label="样本数量" min={4} max={1024} value={count} disabled={locked} onChange={e => setCount(Number(e.target.value))} /></label>
        </div>
        <p className="bn-footnote">支持图片目录、TAR、ZIP 和 Parquet。采样数至少覆盖最高并发；所有配置复用相同图片。测试前请结束打标、退出托管并停止已有模型服务。</p>
      </details>
    </section>
    {state?.message && <div className="bn-progress"><div><span className={state.running ? 'bn-live' : 'bn-dot'} /><span>{state.message}</span><small>{done} / {total} 档{state.running && state.progress?.done !== undefined ? ` · ${state.progress.done} / ${state.sample_count} 张` : ''}</small></div>{state.running && <div className="bn-progress-track"><span style={{ width: `${total ? done / total * 100 : 0}%` }} /></div>}</div>}
    <BenchmarkResults state={state} />
    {folder.dialog}
  </div>
}
