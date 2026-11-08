import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  CheckCheck,
  FolderOpen,
  GitCompareArrows,
  Loader2,
  RefreshCw,
  ScanSearch,
  X,
} from 'lucide-react'
import { api, ApiError, type ConflictItem, type ConflictsState } from '../lib/api'
import { PageHeader, SectionPanel, SettingRow, StatusChip, type StatusTone } from '../components/ui/launcher'
import { ChoiceGrid, ErrorBox } from '../components/ui/controls'
import { FolderPicker } from '../components/FolderPicker'

// 冲突与决策：同一个 image_id 被打了两遍、内容还不一样（换模型 / 换 prompt /
// 新旧两代产物混进同一目录）。合并遇到这种会报"内容冲突"并只取第一份，
// 所以要把两边摊开给人看，谁留谁舍由人点头——或者按策略一键建议。
//
// 决策按内容的哈希存，不按下标：重扫一次候选顺序就可能变（多了个文件、
// 路径排序变了），按下标会把决定悄悄安到另一份内容上。

const STRATEGIES = ['manual', 'newest', 'longest'] as const
const STRATEGY_LABEL: Record<(typeof STRATEGIES)[number], string> = {
  manual: '人工决策',
  newest: '保留最新',
  longest: '保留最长',
}

const PAGE_SIZE = 50

function fmtTime(ts: number) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export function Conflicts() {
  const [roots, setRoots] = useState<string[]>([''])
  const [picker, setPicker] = useState<number | null>(null)
  const [state, setState] = useState<ConflictsState | null>(null)
  const [strategy, setStrategy] = useState<(typeof STRATEGIES)[number]>('manual')
  const [busy, setBusy] = useState<'scan' | 'auto' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)

  const load = useCallback(async (next = 0) => {
    try {
      const st = await api.conflicts(next, PAGE_SIZE)
      setState(st)
      setOffset(next)
      // 目录框空着就沿用上次扫的那几个，省得每次重新填。
      if (st.index && st.roots.length && roots.every((r) => !r.trim())) {
        setRoots(st.roots)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
    // roots 刻意不进依赖：这里只在挂载时读一次，目录框以后的编辑不该触发重扫
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    void load(0)
  }, [load])

  async function scan() {
    const list = roots.map((r) => r.trim()).filter(Boolean)
    if (!list.length) {
      setError('至少填一个产物目录')
      return
    }
    setBusy('scan')
    setError(null)
    setNotice(null)
    try {
      const st = await api.conflictsScan(list, PAGE_SIZE)
      setState(st)
      setOffset(0)
      setNotice(
        st.unreadable > 0
          ? `有 ${st.unreadable} 项读取失败；当前结果不完整，请检查目录或产物后重新扫描。`
          : st.total === 0
          ? `比较了 ${st.scanned} 条，没有冲突。`
          : `比较了 ${st.scanned} 条，找到 ${st.total} 条冲突（内容一致的重复 ${st.idempotent} 次，已自动取一份）。`,
      )
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  async function decide(item: ConflictItem, pick: string) {
    setError(null)
    try {
      await api.conflictDecide(item.image_id, item.kind, pick)
      await load(offset)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  async function auto() {
    setBusy('auto')
    setError(null)
    setNotice(null)
    try {
      const res = await api.conflictsAuto(strategy)
      if (res.reason) setNotice(res.reason)
      else setNotice(`按「${STRATEGY_LABEL[strategy]}」批准 ${res.applied} 条，跳过 ${res.skipped} 条。`)
      await load(offset)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const items = state?.items ?? []
  const total = state?.total ?? 0
  const undecided = total - (state?.decisions ?? 0)
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const page = Math.floor(offset / PAGE_SIZE)

  const tone: StatusTone = !state?.index
    ? 'neutral'
    : state.unreadable > 0
      ? 'warning'
      : total === 0
      ? 'success'
      : undecided > 0
        ? 'warning'
        : 'success'

  return (
    <div className="mx-auto lx-page-enter flex max-w-6xl flex-col gap-3.5">
      <PageHeader
        icon={<GitCompareArrows size={17} />}
        eyebrow="冲突与决策"
        title="冲突审批"
        description="同一 image_id 有多份不同产物时，在这里逐条定留哪份；合并只取你批准的那份。"
        badges={
          <StatusChip tone={tone}>
            {!state?.index
              ? '还没扫过'
              : state.unreadable > 0
                ? `${state.unreadable} 项读取失败`
                : total === 0
                ? '没有冲突'
                : `${total} 条冲突 · ${state.decisions} 条已批`}
          </StatusChip>
        }
        actions={
          <button
            type="button"
            className="lx-btn-primary lx-launch-glow inline-flex items-center gap-1.5"
            disabled={busy !== null}
            onClick={() => void scan()}
          >
            {busy === 'scan' ? <Loader2 size={14} className="animate-spin" /> : <ScanSearch size={14} />}
            {busy === 'scan' ? '扫描中…' : '扫描冲突'}
          </button>
        }
      />

      {error && <ErrorBox error={error} title="操作失败" />}
      {notice && (
        <div className="rounded-lx-sm border border-lx bg-lx-surface-raised px-3 py-2 text-xs text-lx-secondary">
          {notice}
        </div>
      )}

      <SectionPanel
        title="产物目录"
        description="只填一个共同父目录也行（会递归扫）；合并时给的目录要与这里一致。"
      >
        {roots.map((value, i) => (
          <SettingRow key={i} label={`目录 ${i + 1}`}>
            <div className="flex gap-2">
              <input
                className="lx-input font-mono text-xs"
                value={value}
                onChange={(e) => setRoots((prev) => prev.map((v, k) => (k === i ? e.target.value : v)))}
                placeholder="D:\tagger\out 或 \\server\share\member-07"
                spellCheck={false}
              />
              <button type="button" className="lx-btn-ghost shrink-0 px-3" onClick={() => setPicker(i)}>
                <FolderOpen size={14} className="mr-1 inline" />
                浏览
              </button>
              {roots.length > 1 && (
                <button
                  type="button"
                  className="lx-btn-ghost shrink-0 px-2"
                  title="删掉这一行"
                  onClick={() => setRoots((prev) => prev.filter((_, k) => k !== i))}
                >
                  <X size={14} />
                </button>
              )}
            </div>
          </SettingRow>
        ))}
        <div className="flex justify-end pt-1">
          <button type="button" className="lx-btn-ghost text-xs" onClick={() => setRoots((prev) => [...prev, ''])}>
            + 再加一个目录
          </button>
        </div>
      </SectionPanel>

      <SectionPanel
        title="自动审批"
        description="策略只预填建议，批准后仍可逐条反悔。"
        trailing={
          <button
            type="button"
            className="lx-btn-ghost inline-flex items-center gap-1.5 text-xs"
            disabled={busy !== null || !state?.index || total === 0 || strategy === 'manual'}
            onClick={() => void auto()}
          >
            {busy === 'auto' ? <Loader2 size={13} className="animate-spin" /> : <CheckCheck size={13} />}
            按策略批准
          </button>
        }
      >
        <SettingRow
          label="审批方案"
          help="保留最新 = 按产物写入时间取新的那份；保留最长 = 正文字符最多（tag 就是串最长）。人工决策 = 不自动批任何一条。"
        >
          <ChoiceGrid
            options={STRATEGIES}
            value={strategy}
            onChange={setStrategy}
            className="grid-cols-3"
            size="sm"
            renderLabel={(v) => STRATEGY_LABEL[v]}
            ariaLabel="审批方案"
          />
        </SettingRow>
      </SectionPanel>

      <SectionPanel
        title="冲突明细"
        description="点「保留这份」即批准，批准过的在合并时生效。"
        trailing={
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="lx-btn-ghost px-2 text-xs"
              disabled={offset <= 0}
              onClick={() => void load(Math.max(0, offset - PAGE_SIZE))}
            >
              上一页
            </button>
            <span className="text-xs tabular-nums text-lx-muted">
              {page + 1}/{pages}
            </span>
            <button
              type="button"
              className="lx-btn-ghost px-2 text-xs"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => void load(offset + PAGE_SIZE)}
            >
              下一页
            </button>
            <button type="button" className="lx-btn-ghost px-2" title="重新读一次" onClick={() => void load(offset)}>
              <RefreshCw size={13} />
            </button>
          </div>
        }
      >
        {!state?.index && (
          <p className="px-1 py-3 text-xs text-lx-muted">还没有扫描结果。填好目录后点右上角「扫描冲突」。</p>
        )}
        {state?.index && state.unreadable > 0 && (
          <p className="px-1 py-3 text-xs text-lx-warning">
            {state.unreadable} 项读取失败，当前冲突清单不完整。请检查目录或产物后重新扫描。
          </p>
        )}
        {state?.index && total === 0 && state.unreadable === 0 && (
          <p className="px-1 py-3 text-xs text-lx-success">
            没有冲突。内容一致的重复 {state.idempotent} 次已自动取一份。
          </p>
        )}
        {items.map((item) => (
          <div key={item.key} className="mb-3 rounded-lx-sm border border-lx-subtle bg-lx-surface p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <AlertTriangle size={13} className={item.decided ? 'text-lx-success' : 'text-lx-warning'} />
              <span className="font-mono text-xs font-semibold text-lx-primary">{item.image_id}</span>
              <StatusChip tone="neutral">{item.kind === 'tag' ? 'tag' : 'NL'}</StatusChip>
              <StatusChip tone={item.decided ? 'success' : 'warning'}>
                {item.decided ? '已批准' : `${item.candidates.length} 份待选`}
              </StatusChip>
              {item.decided && (
                <button
                  type="button"
                  className="ml-auto text-xs text-lx-muted hover:text-lx-danger"
                  onClick={() => void decide(item, '')}
                >
                  撤销
                </button>
              )}
            </div>
            <div className="flex flex-col gap-2">
              {item.candidates.map((cand) => {
                const chosen = item.decided === cand.digest
                return (
                  <div
                    key={cand.digest}
                    className={`rounded-lx-sm border p-2.5 ${
                      chosen ? 'border-lx-success bg-lx-success-subtle' : 'border-lx-subtle bg-lx-surface-raised'
                    }`}
                  >
                    <div className="mb-1.5 flex flex-wrap items-center gap-2 text-[11px] text-lx-muted">
                      <span className="font-mono">{cand.digest}</span>
                      <span>{cand.chars} 字</span>
                      <span>{fmtTime(cand.ts)}</span>
                      {cand.model && <span className="truncate">模型 {cand.model}</span>}
                      <span className="truncate font-mono opacity-70">{cand.source}</span>
                      <button
                        type="button"
                        className={`ml-auto shrink-0 ${chosen ? 'text-lx-success' : 'lx-btn-ghost px-2 py-0.5 text-xs'}`}
                        disabled={chosen}
                        onClick={() => void decide(item, cand.digest)}
                      >
                        {chosen ? '已选这份' : '保留这份'}
                      </button>
                    </div>
                    <pre className="m-0 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-lx-secondary">
                      {cand.value}
                    </pre>
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </SectionPanel>

      <FolderPicker
        open={picker !== null}
        start={picker !== null ? roots[picker] : ''}
        onClose={() => setPicker(null)}
        onPick={(path) => {
          setRoots((prev) => prev.map((v, k) => (k === picker ? path : v)))
          setPicker(null)
        }}
      />
    </div>
  )
}
