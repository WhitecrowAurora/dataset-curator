import { Settings2 } from 'lucide-react'
import { ChoiceGrid } from '../components/ui/controls'
import { PageHeader, SectionPanel, SettingRow } from '../components/ui/launcher'
import { PROGRESS_INTERVALS } from '../lib/prefs'

/**
 * 设置页。目前只有一个偏好，但独立成页是刻意的：界面偏好以后会越攒越多
 * （表格密度、默认分页、动效开关），塞进任何业务页里都会变成"找得到算运气"。
 */
export function Settings({
  progressInterval,
  onProgressInterval,
}: {
  progressInterval: number
  onProgressInterval: (ms: number) => void
}) {
  // 选项和选中值都按**秒**给：底层存毫秒，但这里必须先把 1000 折掉，
  // 否则 options 是 ['1000','2000',...] 而 value 是 '5'，既显示成 1000s，
  // 也永远匹配不到任何一档（等于没有选中态）。0 = 暂停。
  const options = PROGRESS_INTERVALS.map((ms) => String(ms / 1000))
  const value = String(progressInterval / 1000)

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-5">
      <PageHeader
        icon={<Settings2 size={18} />}
        eyebrow="本机偏好"
        title="设置"
        description="只存在当前浏览器里"
      />

      <SectionPanel title="界面">
        <SettingRow
          label="底部进度条刷新频率"
          help="调快更跟手，但会多打后端"
        >
          <ChoiceGrid
            options={options}
            value={value}
            onChange={(v) => onProgressInterval(Number(v) * 1000)}
            className="grid-cols-5"
            size="sm"
            mono
            ariaLabel="底部进度条刷新频率"
            renderLabel={(v) => (v === '0' ? '暂停' : `${v}s`)}
          />
        </SettingRow>
        <p className="px-1 pt-2 text-[10px] leading-relaxed text-lx-dim">
          默认 5 秒；「暂停」只停止轮询，任务照跑
        </p>
      </SectionPanel>
    </div>
  )
}
