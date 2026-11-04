import { useEffect, useMemo, useState } from 'react'
import {
  CheckCircle2,
  FileJson,
  FolderSearch,
  Merge,
  ScanSearch,
  TriangleAlert,
} from 'lucide-react'
import {
  api,
  ApiError,
  type TagCheckResult,
  type TagFileRow,
  type TagMergeReport,
  type TagMergeTask,
  type TagScanResult,
} from '../lib/api'
import { fmtBytes } from '../lib/format'
import {
  PageHeader,
  SectionPanel,
  SettingRow,
  StatusChip,
  type StatusTone,
} from '../components/ui/launcher'
import { ErrorBox } from '../components/ui/controls'

// 打标管理：把一堆 json 整理成**一个单体 json**。
//
// 打标产物是一张图两个文件（`<id>-tag.json` / `<id>-nl.json`），训练侧要的却是
// 一条记录含全部字段。这个页面做的就是那一步合并，并且把"做了什么"摊开给
// 人看：读了几条、去重几条、哪些字段冲突、写出多大。
//
// 为什么预览走真合并逻辑而不是"读几行"：用户要看的是**合并后**的形态，
// 随便抽一行只会看到只有 tag 或只有 caption 的半条记录，反而误导。

const KIND_LABEL: Record<TagFileRow['kind'], string> = {
  tag: 'tag',
  nl: 'nl',
  parquet: 'pq',
  other: 'json',
}

export function TagManage() {
  const [dir, setDir] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [scan, setScan] = useState<TagScanResult | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [outPath, setOutPath] = useState('run/merged.json')
  const [format, setFormat] = useState<'full' | 'train'>('full')
  // 图片路径前缀：空 = 不动产物自带的值；填了才覆盖成 <前缀>/<id><扩展名>。
  // 默认留空是刻意的——接手一批已经能跑的数据时，默认行为必须是什么都不改。
  const [imageRoot, setImageRoot] = useState('')
  const [imageExt, setImageExt] = useState('.jpg')
  const [asJsonl, setAsJsonl] = useState(false)
  const [key, setKey] = useState('image_id')
  const [onConflict, setOnConflict] = useState<'first' | 'last'>('first')
  const [require, setRequire] = useState<'any' | 'both' | 'tag' | 'nl'>('any')

  // 0 = 自动：估算量大时后端自己分桶。填了才强制分批。
  const [batchSize, setBatchSize] = useState(0)
  // 自检时把 image_path 的前缀换成本机实际目录（容器路径 / 网络路径需要）
  const [localRoot, setLocalRoot] = useState('')
  const [check, setCheck] = useState<TagCheckResult | null>(null)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<TagMergeReport | null>(null)
  const [preview, setPreview] = useState<Record<string, unknown>[]>([])
  // 合并跑在后台：这里存 task_id，进度由下面的 effect 轮询。
  // 用一个 id 而不是直接存 task：切页/刷新后还能接着看，也不会被上一次的
  // 定时器串台。
  const [taskId, setTaskId] = useState('')
  const [task, setTask] = useState<TagMergeTask | null>(null)
  // 自检同样后台跑：扫几十万张在网络盘上是分钟级
  const [checkTaskId, setCheckTaskId] = useState('')
  const [checkTask, setCheckTask] = useState<TagMergeTask | null>(null)

  // 输出扩展名跟着 json/jsonl 走：两者不一致时后端会拒绝，提前对齐省一次报错
  useEffect(() => {
    setOutPath((prev) => {
      const base = prev.replace(/\.jsonl?$/i, '') || 'run/merged'
      return `${base}${asJsonl ? '.jsonl' : '.json'}`
    })
  }, [asJsonl])

  const files = scan?.files ?? []
  const selBytes = useMemo(
    () => files.filter((f) => selected.has(f.path)).reduce((n, f) => n + f.size, 0),
    [files, selected],
  )

  async function doScan() {
    const target = dir.trim()
    if (!target) {
      setError('先填一个目录')
      return
    }
    setBusy(true)
    setError(null)
    setReport(null)
    try {
      const res = await api.tagScan(target, recursive)
      setScan(res)
      // 默认全选"非空、且是本项目产物或普通 json"的文件：空文件是截断的，
      // 让它默认不进合并，但列表里仍然显示（用户可能想先去看一眼）
      setSelected(new Set(res.files.filter((f) => !f.empty).map((f) => f.path)))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function doPreview() {
    if (!selected.size) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.tagPreview({
        files: [...selected],
        limit: 5,
        key: key.trim() || 'image_id',
        image_root: imageRoot.trim(),
        image_ext: imageExt.trim() || '.jpg',
      })
      setPreview(res.rows)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function doMerge() {
    if (!selected.size) return
    setBusy(true)
    setError(null)
    setReport(null)
    setCheck(null)
    try {
      const res = await api.tagMerge({
        files: [...selected],
        out_path: outPath.trim(),
        format,
        as_jsonl: asJsonl,
        key: key.trim() || 'image_id',
        on_conflict: onConflict,
        require,
        indent: 2,
        image_root: imageRoot.trim(),
        image_ext: imageExt.trim() || '.jpg',
        batch_size: Math.max(0, batchSize || 0),
      })
      setPreview([])
      setTaskId(res.task_id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setBusy(false)
    }
  }

  // 轮询合并进度。任务结束后自己停：不靠 setInterval 长跑，避免页面切走后
  // 还在打后端。
  useEffect(() => {
    if (!taskId) return
    let alive = true
    let timer = 0
    const tick = async () => {
      try {
        const t = await api.tagMergeStatus(taskId)
        if (!alive) return
        setTask(t)
        if (t.status === 'running') {
          timer = window.setTimeout(() => void tick(), 400)
          return
        }
        setTaskId('')
        setBusy(false)
        // report 字段两种任务共用；合并这条分支拿到的必是 TagMergeReport
        if (t.status === 'done' && t.report) setReport(t.report as TagMergeReport)
        else setError(t.error || '合并失败')
      } catch (err) {
        if (!alive) return
        setTaskId('')
        setBusy(false)
        setError(err instanceof ApiError ? err.message : String(err))
      }
    }
    timer = window.setTimeout(() => void tick(), 250)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [taskId])

  async function doCheck() {
    if (!selected.size) return
    setBusy(true)
    setError(null)
    setCheck(null)
    try {
      const res = await api.tagCheck({
        files: [...selected],
        key: key.trim() || 'image_id',
        image_root: imageRoot.trim(),
        image_ext: imageExt.trim() || '.jpg',
        local_root: localRoot.trim(),
      })
      setCheckTaskId(res.task_id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setBusy(false)
    }
  }

  // 自检进度：与合并同一个套路，只是阶段名读起来不一样
  useEffect(() => {
    if (!checkTaskId) return
    let alive = true
    let timer = 0
    const tick = async () => {
      try {
        const t = await api.tagCheckStatus(checkTaskId)
        if (!alive) return
        setCheckTask(t)
        if (t.status === 'running') {
          timer = window.setTimeout(() => void tick(), 500)
          return
        }
        setCheckTaskId('')
        setBusy(false)
        // report 字段两边共用：这里是自检的统计，不是合并报告
        if (t.status === 'done' && t.report) setCheck(t.report as TagCheckResult)
        else setError(t.error || '自检失败')
      } catch (err) {
        if (!alive) return
        setCheckTaskId('')
        setBusy(false)
        setError(err instanceof ApiError ? err.message : String(err))
      }
    }
    timer = window.setTimeout(() => void tick(), 250)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [checkTaskId])

  const toggle = (path: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  const tone: StatusTone = report ? (report.bad_files.length ? 'warning' : 'success') : 'neutral'

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        icon={<Merge size={17} />}
        eyebrow="打标管理"
        title="把多个 json 合成一个单体 json"
        description="扫描目录里的打标产物（每图两个 json：tag 与 nl），按 image_id 合成一条记录，字段取并集。也可以合并任意 json 数组 / JSONL。写出是流式的，几百万条也不会把内存打满。"
        actions={
          <button
            type="button"
            className="lx-btn-primary lx-launch-glow inline-flex items-center gap-1.5"
            disabled={!selected.size || busy}
            onClick={() => void doMerge()}
          >
            <Merge size={14} />
            {busy ? '处理中…' : `合并 ${selected.size} 个文件`}
          </button>
        }
      />

      {error && <ErrorBox error={error} />}

      <div className="grid gap-3.5 lg:grid-cols-[1.15fr_1fr]">
        <SectionPanel
          title="输入"
          trailing={
            <div className="flex items-center gap-1.5">
              {scan && (
                <StatusChip tone="neutral">
                  {files.length} 个 json · {fmtBytes(scan.counts.bytes)}
                </StatusChip>
              )}
              <button
                type="button"
                className="lx-btn-ghost px-2.5 py-1 text-[11px]"
                disabled={busy}
                onClick={() => void doScan()}
              >
                <ScanSearch size={12} className="mr-1 inline" />
                扫描
              </button>
            </div>
          }
        >
          <SettingRow
            label="目录"
            help="打标产物目录（含 *.json）。也可以直接填一个 json 文件"
          >
            <div className="flex items-center gap-2">
              <input
                className="lx-input min-w-0 flex-1 font-mono text-xs"
                value={dir}
                onChange={(e) => setDir(e.target.value)}
                placeholder="H:\out\tags"
                spellCheck={false}
              />
              <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-[11px] text-lx-dim">
                <input
                  type="checkbox"
                  className="accent-lx-accent"
                  checked={recursive}
                  onChange={(e) => setRecursive(e.target.checked)}
                />
                递归
              </label>
            </div>
          </SettingRow>

          {scan && scan.counts.empty > 0 && (
            <p className="flex items-start gap-1.5 px-1 text-[11px] leading-relaxed text-lx-warning">
              <TriangleAlert size={12} className="mt-0.5 shrink-0" />
              有 {scan.counts.empty} 个空文件，默认没有勾选——空 json 多是下载/
              写入被截断，合进去只会让条数看着对、内容是空的。
            </p>
          )}

          {scan && (
            <>
              <div className="flex items-center justify-between px-1 text-[11px] text-lx-dim">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="lx-btn-ghost px-2 py-0.5 text-[11px]"
                    onClick={() =>
                      setSelected(new Set(files.filter((f) => !f.empty).map((f) => f.path)))
                    }
                  >
                    全选
                  </button>
                  <button
                    type="button"
                    className="lx-btn-ghost px-2 py-0.5 text-[11px]"
                    onClick={() => setSelected(new Set())}
                  >
                    清空
                  </button>
                  <button
                    type="button"
                    className="lx-btn-ghost px-2 py-0.5 text-[11px]"
                    onClick={() =>
                      setSelected(
                        new Set(files.filter((f) => f.kind !== 'other' && !f.empty).map((f) => f.path)),
                      )
                    }
                  >
                    仅产物
                  </button>
                </div>
                <span className="font-mono tabular-nums">
                  已选 {selected.size} · {fmtBytes(selBytes)}
                </span>
              </div>

              <div className="mt-1 max-h-[48vh] overflow-auto rounded-lg border border-lx-line/60">
                {files.length === 0 ? (
                  <p className="px-3 py-3 text-xs text-lx-dim">这个目录里没有 json。</p>
                ) : (
                  files.map((f) => (
                    <div
                      key={f.path}
                      className="flex items-center gap-2 border-b border-lx-line/40 px-2.5 py-1.5 last:border-b-0 hover:bg-lx-raised/40"
                    >
                      <input
                        type="checkbox"
                        className="accent-lx-accent"
                        checked={selected.has(f.path)}
                        onChange={() => toggle(f.path)}
                      />
                      {f.empty ? (
                        <TriangleAlert size={12} className="shrink-0 text-lx-warning" />
                      ) : selected.has(f.path) ? (
                        <CheckCircle2 size={12} className="shrink-0 text-lx-success" />
                      ) : (
                        <FileJson size={12} className="shrink-0 text-lx-dim" />
                      )}
                      <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={f.path}>
                        {f.name}
                      </span>
                      <span className="shrink-0 rounded border border-lx-line/60 px-1 text-[9px] uppercase text-lx-dim">
                        {KIND_LABEL[f.kind]}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] tabular-nums text-lx-dim">
                        {f.empty ? '空' : fmtBytes(f.size)}
                      </span>
                    </div>
                  ))
                )}
              </div>
            </>
          )}
          {!scan && (
            <p className="flex items-center gap-1.5 px-1 text-[11px] text-lx-dim">
              <FolderSearch size={12} />
              填目录后点右上角「扫描」。
            </p>
          )}
        </SectionPanel>

        <div className="flex flex-col gap-3.5">
          <SectionPanel
            title="合并设置"
            trailing={
              <button
                type="button"
                className="lx-btn-ghost px-2.5 py-1 text-[11px]"
                disabled={!selected.size || busy}
                onClick={() => void doPreview()}
              >
                预览前 5 条
              </button>
            }
          >
            <SettingRow label="输出文件" help="相对路径按仓库根算。写出是流式的，不先物化整表">
              <input
                className="lx-input font-mono text-xs"
                value={outPath}
                onChange={(e) => setOutPath(e.target.value)}
                spellCheck={false}
              />
            </SettingRow>
            <SettingRow
              label="输出格式"
              help="full = 保留全部字段（便于核对/二次加工）；train = 只留 image_id / image_path / caption / tag"
            >
              <select
                className="lx-input"
                value={format}
                onChange={(e) => setFormat(e.target.value as 'full' | 'train')}
              >
                <option value="full">完整字段</option>
                <option value="train">训练四项</option>
              </select>
            </SettingRow>
            <SettingRow label="文件形态" help="单体数组便于人看，JSONL 便于下游逐行流式读">
              <select
                className="lx-input"
                value={asJsonl ? 'jsonl' : 'json'}
                onChange={(e) => setAsJsonl(e.target.value === 'jsonl')}
              >
                <option value="json">单个 JSON 数组</option>
                <option value="jsonl">JSONL（每行一条）</option>
              </select>
            </SettingRow>
            <SettingRow
              label="图片路径前缀"
              help="填了就覆盖成「前缀/图片ID+扩展名」，例如 /images/fanbox；留空 = 保留产物自带的值（HF 模式是 hf://datasets/…，本地分发的产物没有这个字段）"
            >
              <div className="flex items-center gap-2">
                <input
                  className="lx-input min-w-0 flex-1 font-mono text-xs"
                  value={imageRoot}
                  onChange={(e) => setImageRoot(e.target.value)}
                  placeholder="/images/fanbox"
                  spellCheck={false}
                />
                <input
                  className="lx-input w-20 shrink-0 font-mono text-xs"
                  value={imageExt}
                  onChange={(e) => setImageExt(e.target.value)}
                  placeholder=".jpg"
                  spellCheck={false}
                />
              </div>
            </SettingRow>
            <SettingRow
              label="路径自检"
              help="按上面的前缀逐条核对图在不在本地。训练机路径与本机不同时，右边填本机目录做映射；hf:// 之类远端路径不计入缺图"
            >
              <div className="flex items-center gap-2">
                <input
                  className="lx-input min-w-0 flex-1 font-mono text-xs"
                  value={localRoot}
                  onChange={(e) => setLocalRoot(e.target.value)}
                  placeholder="本机根（可选）D:\ds\fanbox"
                  spellCheck={false}
                />
                <button
                  type="button"
                  className="lx-btn-ghost shrink-0 px-2.5 py-1 text-[11px]"
                  disabled={!selected.size || busy}
                  onClick={() => void doCheck()}
                >
                  检查
                </button>
              </div>
            </SettingRow>
            <SettingRow
              label="分批大小"
              help="0 = 自动（估算超过 30 万条才分桶）。分桶把内存上限降到一个桶；代价是输出按桶序排列，不再全局按 image_id 排序"
            >
              <input
                type="number"
                className="lx-input font-mono tabular-nums"
                min={0}
                step={50000}
                value={batchSize}
                onChange={(e) => setBatchSize(Math.max(0, Number(e.target.value) || 0))}
              />
            </SettingRow>
            <SettingRow label="去重键" help="同键的多个文件会合并成一条（tag 与 nl 就靠这个配对）">
              <input
                className="lx-input font-mono text-xs"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                spellCheck={false}
              />
            </SettingRow>
            <SettingRow label="字段冲突" help="artist / provenance / ts 这类字段两边都有，值不同时取哪个">
              <select
                className="lx-input"
                value={onConflict}
                onChange={(e) => setOnConflict(e.target.value as 'first' | 'last')}
              >
                <option value="first">先读到的胜出</option>
                <option value="last">后读到的覆盖</option>
              </select>
            </SettingRow>
            <SettingRow label="完整性过滤" help="both = 缺 caption 或缺 tag 的整条丢掉，避免训练侧读到空字段">
              <select
                className="lx-input"
                value={require}
                onChange={(e) =>
                  setRequire(e.target.value as 'any' | 'both' | 'tag' | 'nl')
                }
              >
                <option value="any">不丢（有 key 就留）</option>
                <option value="both">两者都要有</option>
                <option value="tag">只要有 tag</option>
                <option value="nl">只要有 caption</option>
              </select>
            </SettingRow>
          </SectionPanel>

          <SectionPanel
            title="结果"
            trailing={
              <StatusChip tone={tone}>
                {report ? `写出 ${report.records_out} 条` : '尚未合并'}
              </StatusChip>
            }
          >
            {/* 进度：合并跑在后台线程里，不显示这个的话页面就像卡住了 */}
            {task?.status === 'running' && (
              <div className="mb-2 flex flex-col gap-1.5 px-1">
                <div className="flex items-center justify-between text-[11px] text-lx-secondary">
                  <span>
                    {task.stage === 'read'
                      ? `读入中 ${task.done}/${task.total} 个文件`
                      : task.total > 0
                        ? `写出中 ${task.done}/${task.total} 条`
                        : `写出中 ${task.done} 条`}
                  </span>
                  <span className="font-mono tabular-nums text-lx-dim">
                    {task.elapsed_s}s
                  </span>
                </div>
                <div className="h-1 overflow-hidden rounded-full bg-lx-raised">
                  <div
                    className="h-full bg-lx-accent transition-[width] duration-300"
                    style={{
                      width: `${
                        task.total > 0
                          ? Math.min(100, (task.done / task.total) * 100)
                          : task.stage === 'read'
                            ? 0
                            : 50
                      }%`,
                    }}
                  />
                </div>
              </div>
            )}

            {/* 自检进度：同样是后台任务，几十万条时要让人看到它在动 */}
            {checkTask?.status === 'running' && (
              <div className="mb-2 flex flex-col gap-1.5 px-1">
                <div className="flex items-center justify-between text-[11px] text-lx-secondary">
                  <span>
                    {checkTask.stage === 'read'
                      ? `读入中 ${checkTask.done}/${checkTask.total} 个文件`
                      : checkTask.total > 0
                        ? `核对中 ${checkTask.done}/${checkTask.total} 条`
                        : `核对中 ${checkTask.done} 条`}
                  </span>
                  <span className="font-mono tabular-nums text-lx-dim">
                    {checkTask.elapsed_s}s
                  </span>
                </div>
                <div className="h-1 overflow-hidden rounded-full bg-lx-raised">
                  <div
                    className="h-full bg-lx-accent transition-[width] duration-300"
                    style={{
                      width: `${
                        checkTask.total > 0
                          ? Math.min(100, (checkTask.done / checkTask.total) * 100)
                          : 0
                      }%`,
                    }}
                  />
                </div>
              </div>
            )}

            {/* 自检结果单独一块：它回答的是"路径对不对"，与合并统计无关 */}
            {check && (
              <div className="mb-3 flex flex-col gap-1.5 rounded-lg border border-lx-line/60 px-2.5 py-2 text-[11px]">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="text-lx-dim">
                    自检 {check.total} 条
                  </span>
                  <span className="text-lx-success">在 {check.ok}</span>
                  <span className={check.missing ? 'text-lx-danger' : 'text-lx-dim'}>
                    缺 {check.missing}
                  </span>
                  <span className="text-lx-dim">远端 {check.remote}</span>
                  <span className="text-lx-dim">无路径 {check.empty}</span>
                </div>
                {check.missing > 0 && (
                  <div className="text-[10px] leading-relaxed text-lx-danger">
                    前 {check.samples.length} 个缺失：
                    {check.samples.map((s) => (
                      <div key={s.image_id} className="truncate font-mono">
                        {s.image_path}
                      </div>
                    ))}
                  </div>
                )}
                {check.missing === 0 && check.ok > 0 && (
                  <p className="text-[10px] text-lx-success">
                    路径全部对得上，可以合并了。
                  </p>
                )}
              </div>
            )}

            {report ? (
              <div className="flex flex-col gap-2 text-[11px]">
                <div className="grid grid-cols-2 gap-1.5 font-mono tabular-nums">
                  <span className="text-lx-dim">读入文件</span>
                  <span className="text-right">{report.inputs}</span>
                  <span className="text-lx-dim">读入记录</span>
                  <span className="text-right">{report.records_read}</span>
                  <span className="text-lx-dim">去重合并</span>
                  <span className="text-right">{report.records_out}</span>
                  <span className="text-lx-dim">同键重复</span>
                  <span className="text-right">{report.duplicates}</span>
                  <span className="text-lx-dim">字段冲突</span>
                  <span className="text-right">{report.conflicts}</span>
                  <span className="text-lx-dim">跳过</span>
                  <span className="text-right">{report.skipped}</span>
                  <span className="text-lx-dim">文件大小</span>
                  <span className="text-right">{fmtBytes(report.bytes)}</span>
                  <span className="text-lx-dim">耗时</span>
                  <span className="text-right">{report.elapsed_s}s</span>
                </div>
                <p className="break-all font-mono text-[10px] text-lx-dim">
                  {report.out_path}
                </p>
                <p className="break-all text-[10px] leading-relaxed text-lx-dim">
                  图片路径：{' '}
                  {report.image_root ? (
                    <span className="font-mono text-lx-secondary">
                      {report.image_root}/&lt;image_id&gt;{imageExt || '.jpg'}
                    </span>
                  ) : (
                    '沿用产物自带的值（未覆盖）'
                  )}
                </p>
                {report.bad_files.length > 0 && (
                  <div className="rounded border border-lx-warning/40 px-2 py-1.5 text-[10px] leading-relaxed text-lx-warning">
                    {report.bad_files.length} 个文件读不了：
                    {report.bad_files.slice(0, 3).map((b) => (
                      <div key={b} className="truncate font-mono">
                        {b}
                      </div>
                    ))}
                  </div>
                )}
                <p className="text-[10px] leading-relaxed text-lx-dim">
                  字段并集：<span className="font-mono">{report.fields.join(', ')}</span>
                </p>
                {report.batched && (
                  <p className="text-[10px] leading-relaxed text-lx-warning">
                    走了分桶（{report.buckets} 桶）：内存上限降到一个桶，代价是输出按桶序
                    排列，不再全局按 image_id 排序。
                  </p>
                )}
              </div>
            ) : (
              <p className="px-1 text-[11px] leading-relaxed text-lx-dim">
                合并完这里会显示读入/去重/冲突计数与字段并集。先点「预览前 5 条」看一眼
                记录形态，确认无误再合并。
              </p>
            )}

            {preview.length > 0 && (
              <div className="mt-2">
                <p className="mb-1 px-1 text-[10px] uppercase tracking-wide text-lx-dim">
                  合并后的记录（前 {preview.length} 条）
                </p>
                <pre className="max-h-[34vh] overflow-auto rounded-lg border border-lx-line/60 bg-lx-raised/40 p-2.5 text-[10px] leading-relaxed">
                  {JSON.stringify(preview, null, 2)}
                </pre>
              </div>
            )}
          </SectionPanel>
        </div>
      </div>
    </div>
  )
}
