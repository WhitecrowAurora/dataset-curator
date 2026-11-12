"use strict";
/* =============================================================================
   fox-panel v3 · 前端行为（只读面板 + 受控写通道）

   v2 的只读五页逐字保留；v3 新增（全部只用 createElement/textContent 构建）：
   - 写入口：批量多选 + 行内动作 + 抽屉动作 + 仓库启停 + 设置抽屉（密钥）
   - 严格二次确认：影响面清单 + 红色危险区 + 输入目标名/数量 + 勾选不可撤销
   - 服务端拒绝对得上：428 → 重新预览；400 diff → 展示服务端给的实际差异
   - toast 三态队列（成功/失败/进行中，失败可重试、带查看审计）
   - 通知中心（未读角标、跳审计、失败可重试）
   - 审计双 Tab（业务事件 / 写操作日志）+ 筛选 + 分页 + JSONL/CSV 导出
   - 深链接：筛选/排序/分页/主题进 URL，前进后退可恢复

   约束（CSP：script-src 'self' / style-src 'self'）：
   - 无内联脚本、无 style 属性、无内联事件；不直接改元素的行内样式
   - 所有动态文本用 textContent；不拼 innerHTML（防 XSS）
   - 排序/搜索/分页交给后端（/api/packages?sort=&order=&q=&page=&size=）
   - 审计双 Tab 用普通 button + aria-pressed（不用 role="tab*"，v2 静态测试禁用该词）
   ============================================================================= */
(function () {

/* ------------------------------------------------------------------ 文案表 */
/* 全部界面文案集中在这里（SPEC 附录 B / §9.14）。只提供中文；换语言只改这张表。 */
var T = {
  app: {
    title: "fox-label 面板",
    brand: "fox-label 面板",
    brandSub: "只读管理面板",
    brandSubWrite: "管理面板",
    skip: "跳到主内容",
    navLabel: "主导航",
    footerBrand: "fox-panel",
    footerNote: "只读面板",
    footerNoteWrite: "可写面板",
    footerUpstream: "数据来自 {up}",
    footerUpdated: "更新于 {ts}",
    readonlyHint: "只读面板：打回/释放/踢设备请用服务端 CLI",
    loading: "加载中…"
  },
  nav: {
    overview: "概览",
    packages: "包列表",
    devices: "设备与租约",
    anomalies: "异常体检",
    audit: "审计",
    overdue: "逾期未申报"
  },
  status: {
    available: "未认领",
    claimed: "处理中",
    completed: "已完成",
    prelock: "预锁",
    active: "处理中",
    unknown: "未知",
    open: "进行中",
    released: "已释放",
    closed: "已关闭"
  },
  col: {
    repo: "仓库", id: "包", size: "大小", status: "状态", owner: "认领人",
    downloaded: "已下载", updated: "更新时间",
    device: "设备", user: "用户", tier: "档位", speed: "速度(张/秒)",
    lastSeen: "最近心跳", ver: "版本",
    gpu: "GPU 型号", vram: "显存", firstSeen: "首次接入", pkgs: "本地包数",
    mode: "模式", hwReport: "硬件上报",
    kind: "类型", lease: "租约", ttl: "已持有时长", progress: "进度", beat: "心跳",
    time: "时间", action: "动作", actor: "操作者", detail: "详情",
    enabled: "启用", type: "类型", total: "总数",
    repoAvailable: "未认领", repoClaimed: "处理中", repoCompleted: "已完成",
    capacity: "容量",
    select: "选择", actions: "操作", target: "目标", result: "结果",
    impact: "影响数", ms: "耗时", batch: "批次 / 令牌", role: "角色",
    grants: "授权仓库", lastSeenShort: "最近使用", packages: "包数",
    openLeases: "open 租约", claimedCount: "处理中"
  },
  stat: {
    packages: "总包数", available: "未认领", claimed: "处理中", completed: "已完成",
    active_leases: "active 租约", prelock_leases: "预锁", devices_online: "在线设备",
    free_for_assign: "可分配", bytes_total: "总容量",
    stuck: "死包", orphan: "孤儿租约", ghost: "幽灵租约",
    reposHint: "{n} 个仓库",
    devicesHint: "{n} 台设备",
    leasesHint: "{n} 条租约"
  },
  card: {
    repos: "仓库",
    devices: "设备",
    leases: "进行中的租约",
    stuck: "死包（claimed 但无 active 租约）",
    orphan: "孤儿租约（包已不存在）",
    ghost: "幽灵租约（设备已不存在）",
    overdue: "逾期未申报（领取后超 48h 未申报完成的包，逐包提示）",
    audit: "审计记录",
    writeLog: "写操作日志",
    settingsRepos: "仓库",
    settingsKeys: "密钥"
  },
  field: { repo: "仓库", status: "状态", q: "搜索", size: "每页", audit: "过滤",
           overdueFirst: "逾期优先" },
  opt: { allRepos: "全部仓库", allStatuses: "全部状态" },
  ph: { q: "包名 / 认领人", audit: "动作 / 包 / 操作者", auditWrites: "目标 / 错误文案" },
  btn: {
    refresh: "刷新", retry: "重试", export: "导出 CSV", exportCount: "导出 CSV · {n}",
    prev: "上一页", next: "下一页",
    copySha: "复制 SHA-256", clearFilter: "清除筛选",
    themeToLight: "亮色", themeToDark: "暗色",
    autoRefresh: "自动刷新", logout: "登出", help: "快捷键说明",
    notifications: "通知", settings: "设置"
  },
  empty: {
    none: "还没有数据",
    filtered: "没有匹配的包",
    audit: "未配置审计文件（--audit-file）",
    anomalyOk: "体检通过：没有发现异常",
    repoNone: "还没有仓库",
    devicesNone: "还没有设备",
    leasesNone: "当前没有租约",
    stuckNone: "没有死包",
    orphanNone: "没有孤儿租约",
    ghostNone: "没有幽灵租约",
    overdueNone: "没有逾期未申报的包",
    auditNone: "没有匹配的审计记录",
    pkg404: "包不存在或已被删除",
    hintFiltered: "试试清空搜索词或切换仓库",
    hintNone: "上游暂无数据",
    hint404: "可能已被删除，或该仓库已停用",
    hintAudit: "启动面板时加 --audit-file 即可开启审计",
    writeLog: "还没有写操作记录",
    writeLogHint: "完成一次写操作后会出现在这里",
    writeFiltered: "没有匹配的写操作",
    writeNotCreated: "写日志尚未创建",
    notif: "暂无通知",
    impactEmpty: "影响面为空，仍可执行"
  },
  err: {
    upstream: "上游不可用",
    failed: "出错了",
    network: "网络不可达，请检查面板服务",
    badjson: "响应不是合法 JSON",
    http: "HTTP {code}",
    session: "会话已过期，请重新登录",
    shaMissing: "该包没有 SHA-256",
    copyFailed: "已选中文本，请按 Ctrl+C 复制",
    copyUnavailable: "复制失败，请手动选择文本",
    retry: "重试",
    writeDisabled: "面板处于只读模式，写入口已收起"
  },
  up: { ok: "上游正常 · {ms}ms" },
  toast: {
    copied: "已复制",
    copiedSha: "已复制 SHA-256",
    exported: "已导出 {n} 行",
    truncated: "结果过多，只导出了前 {n} 行",
    exportConfirm: "当前筛选结果 {n} 行，确定导出全部？",
    stale: "数据可能过期"
  },
  live: {
    updated: "已更新 {time}",
    failed: "刷新失败：{error}",
    exported: "已导出 {n} 行",
    wrote: "已提交 {n} 项写操作"
  },
  pager: {
    labelPackages: "包列表分页",
    labelDevices: "设备分页",
    labelLeases: "租约分页",
    labelStuck: "死包分页",
    labelOrphan: "孤儿租约分页",
    labelGhost: "幽灵租约分页",
    labelOverdue: "逾期未申报分页",
    labelAudit: "审计分页",
    info: "第 {p}/{pages} 页 · 共 {n} 个",
    simple: "共 {n} 个",
    empty: "共 0 个",
    auditCount: "{shown} / {total}",
    writesCount: "{shown} / {total}",
    writesPage: "第 {p}/{pages} 页 · 共 {n} 条"
  },
  drawer: {
    loading: "加载中…",
    lease: "租约",
    noLease: "无租约",
    downloaded: "已下载",
    f: {
      repo: "仓库", status: "状态", size: "大小", sha: "SHA-256",
      owner: "认领人", claimedAt: "认领时间", completedAt: "完成时间",
      downloadedAt: "下载时间", updatedAt: "更新时间", note: "备注",
      kind: "租约类型", device: "租约设备", user: "租约用户",
      ttl: "已持有时长", progress: "租约进度", beat: "最近心跳"
    }
  },
  help: {
    title: "键盘快捷键",
    close: "关闭快捷键说明",
    rows: [
      { k: "/", d: "聚焦搜索框" },
      { k: "r", d: "立即刷新当前页" },
      { k: "n", d: "打开通知中心" },
      { k: "g", d: "概览（或 1）" },
      { k: "p", d: "包列表（或 2）" },
      { k: "d", d: "设备与租约（或 3）" },
      { k: "a", d: "异常体检（或 4）" },
      { k: "u", d: "审计（或 5）" },
      { k: "?", d: "打开本说明" },
      { k: "Esc", d: "关闭弹窗 / 抽屉 / 通知" }
    ]
  },
  misc: {
    dash: "-",
    na: "—",
    seconds: "{n}s",
    expired: "已过期",
    justNow: "刚刚",
    ago: "{n} 前",
    asc: "升序",
    desc: "降序",
    caption: "{name}（{sort} {order}）",
    details: "技术详情",
    closeToast: "关闭提示",
    closeDrawer: "关闭详情",
    enabled: "启用",
    disabled: "停用",
    online: "在线",
    idle: "空闲",
    offline: "离线",
    archived: "已归档",
    devFilter: "状态筛选",
    devFilterAll: "全部",
    devFilterOnline: "在线",
    devFilterIdle: "空闲",
    devFilterOffline: "离线",
    expandRow: "展开行详情",
    collapseRow: "收起行详情",
    morePkgs: "…共 {n} 个",
    devEventNone: "暂无事件",
    devDetail: "设备详情",
    devEvents: "最近事件（≤10 条）",
    devPkgs: "本地包（≤20 条）",
    devLease: "当前租约",
    devNoLease: "无当前租约",
    devBack: "返回设备列表",
    rawStatus: "静态状态（排障用）",
    noGpu: "-",
    expandError: "展开数据加载失败",
    truncatedNote: "结果超过 200,000 行，仅对前 200,000 行排序与筛选",
    tooManyRepos: "仓库过多（{n} 个），请到包列表按仓库筛选",
    pkgDetail: "{name} 详情",
    actions: "行内动作",
    heldFor: "已持有 {t}",
    heldFuse: "已持有 {t}（超过 30 天保险丝，请管理员处理）",
    overdueBadge: "逾期 {t}",
    fuseBadge: "超保险丝",
    rawJson: "原始 JSON",
    settingsClose: "关闭设置",
    readonly: "只读",
    overdueNote: "「死包」= claimed 包没有对应 active 租约（数据不一致）；" +
                 "「逾期未申报」= 领取后超过 48h 还没有 finish 申报的 open 租约（健康包也可能逾期）。" +
                 "两者相互独立，都只提示、不自动回收。",
    viewPkg: "查看包详情",
    releasePkg: "释放该包",
    overdueSince: "领取 {t}",
    overdueResolved: "已解决",
    overdueLive: "未解决",
    overdueReminds: "已提醒 {n} 次"
  },

  /* ---- v3 写通道文案（SPEC §9.14 / §4.8） ---- */
  write: {
    cancel: "取消",
    close: "关闭确认对话框",
    ack: "我了解此操作不可撤销",
    busy: "执行中…",
    previewing: "正在计算影响面…",
    previewFailed: "无法计算影响面",
    expired: "确认已过期，正在刷新影响面…",
    expiredTwice: "确认已连续过期，请关闭后重新发起",
    replay: "该操作已提交过（幂等重放），未重复执行",
    loggedFalse: "操作成功，但写日志失败（logged=false），请检查服务端磁盘",
    retry: "重试",
    retryFailed: "重试失败项",
    viewAudit: "查看审计",
    readonly: "面板处于只读模式",
    readonlyLong: "面板处于只读模式：写入口已收起",
    roPill: "只读模式",
    toLabel: "打回后归入",
    act: {
      reopen: "打回", reopenN: "打回",
      release: "释放", releaseN: "释放",
      lease: "释放租约", leaseN: "释放租约",
      unassign: "解绑设备",
      forget: "强制解绑", forgetUser: "清空该用户",
      enable: "启用", disable: "停用", revoke: "吊销",
      missing: "该动作在当前服务端不受支持"
    },
    bulk: {
      region: "批量操作",
      regionLease: "租约批量操作",
      selectAll: "选择本页全部",
      row: "选择 {label}",
      selected: "已选 {n} 项",
      clear: "清除选择",
      crossPage: "跨页不保留选择",
      pickable: "可处理 {n} 项",
      tooMany: "单次最多 {n} 项",
      mobile: "移动端暂不支持批量，请用行内/抽屉操作"
    },
    need: {
      name: "还需输入目标名",
      count: "还需输入目标数量",
      ack: "还需勾选「{ack}」",
      ok: ""
    },
    title: {
      reopen: "确认打回包？",
      reopenBatch: "确认打回 {n} 个包？",
      release: "确认释放包？",
      releaseBatch: "确认释放 {n} 个包？",
      lease: "确认释放租约？",
      leaseBatch: "确认释放 {n} 条租约？",
      unassign: "确认解绑设备 {device}？",
      forget: "确认强制解绑设备 {device}？",
      forgetUser: "确认清空用户 {user} 的调度身份？",
      repoEnable: "确认启用仓库 {repo}？",
      repoDisable: "确认停用仓库 {repo}？",
      keyRevoke: "确认吊销密钥 {name}？",
      confirm: "确认执行此操作？"
    },
    dangerTitle: {
      reopen: "此操作会撤销交付结果",
      reopenBatch: "此操作会撤销 {n} 个包的交付结果",
      release: "此操作会让包回到公共池",
      lease: "此操作会中断设备当前工作",
      unassign: "此操作会作废该设备全部租约",
      forget: "此操作会删除该设备的用户绑定",
      forgetUser: "此操作会删掉这个用户名下的全部设备",
      repoDisable: "停用后客户端将无法认领该仓库",
      repoEnable: "启用后客户端可重新认领",
      keyRevoke: "吊销立即生效且不可撤销"
    },
    dangerBody: {
      reopen: "此操作会把包从「已完成」退回「{to}」，并清除完成时间与下载标记。" +
              "已下载的产物不会被删除，但服务端会把它视为未完成，原认领人需要重新处理。此操作不可撤销。",
      release: "此操作会清除认领人、认领时间与下载标记，包立即回到公共池，其他设备可能马上领走。" +
               "不会自动回收：原持有设备将在下次 finish 申报时收到拒绝。此操作不可撤销。",
      lease: "此操作会由管理员手动回收该租约，包回到公共池，其他设备可能马上领走；该设备当前进度会丢失" +
             "（客户端本地产物不会被删除）。不会自动回收：设备端将在下次 finish 申报时收到拒绝。此操作不可撤销。",
      unassign: "此操作会由管理员手动作废该设备全部 {n} 条租约，其中 {m} 个包退回公共池，并把设备状态置为离线。" +
                "不会自动回收：该设备将在下次 finish 申报时收到拒绝；其上未交付的工作将丢失。此操作不可撤销。",
      forget: "此操作会作废该设备全部 {n} 条正在跑的租约（{m} 个包退回公共池），并删除它在调度中心里与用户的绑定。" +
              "删除后这台机器下次握手会按新填的 user_id 重新登记，等于一次干净的重装；" +
              "历史租约与审计不会被删。该设备上未交付的工作会丢失，此操作不可撤销。",
      forgetUser: "此操作会清空该用户的整个调度身份：作废其名下全部在跑租约（包退回公共池）、删除其 {n} 台设备、" +
                  "并删掉用户行。之后任何人用这个 user_id 握手都会当成新用户，secret 以那台机器本地的为准。" +
                  "历史租约与审计不会被删。若只是想换机，用「强制解绑」即可，不必清空身份。此操作不可撤销。",
      repoDisable: "停用后：① 客户端无法再认领该仓库的包（返回 403「仓库已停用」）；② 调度器不再向任何设备派发该仓库的包；" +
                   "③ 已打开的 {n} 条租约继续有效，可正常完成；④ 面板仍可查看历史。此操作可撤销（重新启用），" +
                   "但停用期间的派工损失不可恢复。",
      repoEnable: "启用后，客户端可以重新认领该仓库的包，调度器恢复派工。",
      keyRevoke: "吊销立即生效：持有该密钥的客户端/脚本下一次请求即 401，SSE 连接会被断开。" +
                 "不可撤销，只能重新签发新密钥。{last}"
    },
    batchNote: "本次将一次性影响 {n} 项，服务端逐条执行：失败项不会回滚已成功项，" +
               "请在清单中确认每一条；确认后不可整体撤销。",
    to: { claimed: "保留原认领人（处理中）", available: "退回公共池（未认领）" },
    input: {
      pkg: "请输入包名 {id} 以确认",
      lease: "请输入租约 {id} 以确认",
      device: "请输入设备 {device} 以确认",
      repo: "请输入仓库 {repo} 以确认",
      key: "请输入密钥名 {name} 以确认",
      user: "请输入用户名 {user} 以确认",
      count: "请输入目标数量 {n} 以确认",
      countLeases: "请输入将释放的租约数 {n} 以确认",
      mutations: "请输入影响数量 {n} 以确认"
    },
    btn: {
      reopen: "确认打回", reopenN: "确认打回 {n} 个",
      release: "确认释放", releaseN: "确认释放 {n} 个",
      lease: "确认释放", leaseN: "确认释放 {n} 条",
      unassign: "确认解绑",
      forget: "确认强制解绑", forgetUser: "确认清空该用户",
      enable: "确认启用", disable: "确认停用", revoke: "确认吊销",
      confirm: "确认"
    },
    impact: {
      title: "将被影响的 {n} 个{obj}（{action}）",
      more: "仅显示前 200 项，共 {n} 项",
      summary: "共 {total} 项：{parts}",
      list: "影响面清单",
      empty: "该目标当前没有进行中的对象，仍可执行。",
      warnings: "执行前的提醒",
      obj: { package: "包", lease: "租约", device: "设备", repo: "仓库", key: "密钥", user: "用户" },
      count: { packages: "包", leases: "租约", devices: "设备", repos: "仓库", keys: "密钥", users: "用户" }
    },
    diff: {
      title: "影响面已变化",
      targetTitle: "目标与确认时不一致",
      paramsTitle: "参数与确认时不一致",
      batchTitle: "需要数量确认",
      targetBody: "提交的目标与确认时不是同一组，已按当前目标重新计算影响面，请重新确认。",
      paramsBody: "提交的参数与确认时不一致（确认令牌绑定参数），已按当前选择重新计算影响面，请重新确认。",
      batchBody: "本次将影响 {n} 项，超过逐条确认阈值，请再次输入影响数量后提交。",
      empty: "目标集合已变化（数量未变），请重新预览后再确认。",
      body: "确认时 {confirmed} 项，现在 {actual} 项。请核对下面的差异后重新勾选确认。",
      added: "新增 {n} 项",
      removed: "减少 {n} 项",
      more: "还有 {n} 项未显示",
      header: "服务端实际影响面"
    },
    toast: {
      ok: "{action}成功：{n} 项",
      partial: "{action}完成：成功 {ok}，失败 {fail}",
      fail: "{action}失败：{error}",
      readonly: "面板处于只读模式，写入口已收起",
      csrf: "CSRF 校验失败，已刷新令牌，请重试"
    },
    result: {
      title: "{action}结果",
      summary: "成功 {ok} · 失败 {fail}（共 {n} 项）",
      ok: "成功",
      fail: "失败",
      close: "关闭结果",
      loggedFalse: "写日志落盘失败（logged=false）",
      more: "只显示前 {n} 条"
    },
    notif: {
      title: "通知",
      empty: "暂无通知",
      allRead: "全部已读",
      all: "查看全部写操作",
      unread: "通知（{n} 条未读）",
      none: "通知",
      close: "关闭通知",
      nothingToRetry: "该批次没有可重试的失败项",
      counts: "成功 {ok} · 失败 {fail}",
      running: "正在执行 {n} 项…",
      result: { ok: "成功", partial: "部分失败", error: "失败", running: "进行中" }
    },
    audit: {
      tabEvents: "业务事件",
      tabWrites: "写操作日志",
      eventsLabel: "过滤",
      writesLabel: "关键字",
      empty: "还没有写操作记录",
      emptyHint: "完成一次写操作后会出现在这里",
      filtered: "没有匹配的写操作",
      notCreated: "写日志尚未创建",
      exportJsonl: "导出 JSONL",
      exportCsv: "导出 CSV",
      detail: "完整记录",
      detailHint: "点击展开批次与确认令牌",
      actionAll: "全部动作",
      resultAll: "全部结果",
      resultOk: "成功",
      resultError: "失败",
      actorPh: "操作者账号",
      rangeLabel: "时间范围",
      tabsLabel: "审计视图",
      rangeAll: "全部时间",
      rangeToday: "今天",
      range7: "近 7 天",
      range30: "近 30 天",
      stale: "写日志刷新失败，下面仍是上次成功加载的数据",
      tabDenied: "拒绝留痕",
      deniedLabel: "拒绝轨按原因/路径筛选",
      reasonLabel: "原因",
      reasonAll: "全部原因",
      reasonPh: "原因或状态码，如 rate_limited / 429",
      pathLabel: "路径",
      pathAll: "全部路径",
      deniedTitle: "被拒绝的写请求（append-only）",
      deniedEmpty: "还没有被拒绝的写请求",
      deniedEmptyHint: "面板拒绝的写请求（400/403/413/415/429）会留在这里",
      deniedFiltered: "没有匹配的拒绝记录",
      deniedCount: "共 {n} 条拒绝",
      deniedStale: "拒绝留痕刷新失败，下面仍是上次成功加载的数据",
      deniedNote: "另有 {n} 条写请求被拒绝",
      deniedOpen: "查看拒绝留痕",
      deniedCol: {
        time: "时间",
        reason: "原因",
        path: "路径",
        actor: "操作者",
        ip: "来源 IP",
        code: "结果码",
        detail: "详情"
      },
      deniedDetail: "展开"
    },
    settings: {
      title: "设置",
      close: "关闭设置",
      reposHint: "含已停用仓库：停用的仓库仍可启用",
      keysHint: "列表已脱敏（不含 hash）；最后一个管理员不可吊销",
      revoke: "吊销",
      revokeLast: "最后一个管理员密钥不可吊销",
      revoked: "已吊销",
      noKeys: "没有可管理的密钥，或服务端未提供密钥接口",
      empty: "没有仓库"
    }
  }
};

/* ------------------------------------------------------------------ 图标 */
var SVGNS = "http://www.w3.org/2000/svg";
var ICONS = {
  overview: ["M4 4h7v7H4z", "M13 4h7v7h-7z", "M4 13h7v7H4z", "M13 13h7v7h-7z"],
  packages: ["M12 3l8 4.5v9L12 21l-8-4.5v-9z", "M4 7.5l8 4.5 8-4.5", "M12 12v9"],
  devices: ["M3 5h18v11H3z", "M8 20h8", "M12 16v4"],
  anomalies: ["M3 12h4l2.5-6 4 12 2.5-6h5"],
  audit: ["M8 6h12", "M8 12h12", "M8 18h12", "M4 6h.01", "M4 12h.01", "M4 18h.01"],
  refresh: ["M20 12a8 8 0 1 1-2.34-5.66", "M20 4v5h-5"],
  sun: ["M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10z", "M12 2v2", "M12 20v2",
        "M4.9 4.9l1.4 1.4", "M17.7 17.7l1.4 1.4", "M2 12h2", "M20 12h2",
        "M4.9 19.1l1.4-1.4", "M17.7 6.3l1.4-1.4"],
  moon: ["M20 14.5A8 8 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z"],
  close: ["M6 6l12 12", "M18 6L6 18"],
  prev: ["M15 6l-6 6 6 6"],
  next: ["M9 6l6 6-6 6"],
  sort: ["M12 5l6 7H6z"],
  inbox: ["M3 13h5l1.5 2h5L16 13h5", "M5 13l2-8h10l2 8v6H5z"],
  alert: ["M12 4l9 16H3z", "M12 10v4", "M12 17h.01"],
  copy: ["M9 9h11v11H9z", "M5 15V5h10"],
  logout: ["M15 4h4v16h-4", "M3 12h13", "M8 7l-5 5 5 5"],
  help: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
         "M9.6 9.4a2.5 2.5 0 1 1 3.6 2.4c-.9.5-1.2 1-1.2 1.9", "M12 17h.01"],
  bell: ["M12 4a6 6 0 0 0-6 6v4l-2 3h16l-2-3v-4a6 6 0 0 0-6-6z", "M10 20a2 2 0 0 0 4 0"],
  gear: ["M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
         "M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3" +
         " 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3" +
         "l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4" +
         "h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0" +
         " 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3" +
         "l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4" +
         "h-.1a1.6 1.6 0 0 0-1.5 1z"],
  check: ["M20 6L9 17l-5-5"],
  bolt: ["M13 2L4 14h7l-1 8 9-12h-7z"],
  spin: ["M12 3a9 9 0 1 0 9 9"],
  undo: ["M3 12h13", "M8 7l-5 5 5 5"]
};

/* ------------------------------------------------------------------ 小工具 */
function $(sel, root) { return (root || document).querySelector(sel); }

function el(tag, attrs, kids) {
  var node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = String(v);
      else if (k === "dataset") {
        Object.keys(v).forEach(function (dk) { node.dataset[dk] = String(v[dk]); });
      } else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, String(v));
    });
  }
  if (kids) {
    [].concat(kids).forEach(function (kid) {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    });
  }
  return node;
}

function icon(name, size, cls) {
  var svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size || 20));
  svg.setAttribute("height", String(size || 20));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", "fp-ic" + (cls ? " " + cls : ""));
  (ICONS[name] || []).forEach(function (d) {
    var p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", d);
    svg.appendChild(p);
  });
  return svg;
}

function tpl(str, vars) {
  return String(str).replace(/\{(\w+)\}/g, function (m, k) {
    return vars && Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m;
  });
}

function pad2(n) { return n < 10 ? "0" + n : String(n); }
function clockNow() {
  var d = new Date();
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
}

function fmtNum(v) {
  var n = Number(v);
  if (v === null || v === undefined || v === "" || !isFinite(n)) return "0";
  try { return n.toLocaleString("zh-CN"); } catch (e) { return String(n); }
}

var BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
function fmtBytes(v) {
  if (v === null || v === undefined || v === "") return null;
  var n = Number(v);
  if (!isFinite(n) || n < 0) return null;
  if (n < 1024) return n + " B";
  var i = 0;
  while (n >= 1024 && i < BYTE_UNITS.length - 1) { n = n / 1024; i++; }
  var digits = n >= 100 ? 0 : (n >= 10 ? 1 : 2);
  return n.toFixed(digits) + " " + BYTE_UNITS[i];
}

var DATE_FMT = {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
};

function fmtTime(v) {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    var sec = Number(v);
    if (!isFinite(sec) || sec <= 0) return null;
    return new Date(sec * 1000).toLocaleString("zh-CN", DATE_FMT);
  }
  var s = String(v);
  var d = new Date(s.indexOf("T") >= 0 ? s : s.replace(" ", "T"));
  if (isNaN(d.getTime())) return s;
  return d.toLocaleString("zh-CN", DATE_FMT);
}

function relativeTime(v) {
  if (v === null || v === undefined || v === "") return null;
  var sec = Number(v);
  if (!isFinite(sec) || sec <= 0) {
    var d = new Date(String(v).indexOf("T") >= 0 ? String(v) : String(v).replace(" ", "T"));
    if (isNaN(d.getTime())) return null;
    sec = d.getTime() / 1000;
  }
  var diff = Math.floor(Date.now() / 1000 - sec);
  if (diff < 0) diff = 0;
  if (diff < 5) return T.misc.justNow;
  var text;
  if (diff < 60) text = diff + " 秒";
  else if (diff < 3600) text = Math.floor(diff / 60) + " 分钟";
  else if (diff < 86400) text = Math.floor(diff / 3600) + " 小时";
  else text = Math.floor(diff / 86400) + " 天";
  return tpl(T.misc.ago, { n: text });
}

function storeGet(key, fallback) {
  try {
    var v = window.localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch (e) { return fallback; }
}
function storeSet(key, value) {
  try { window.localStorage.setItem(key, String(value)); } catch (e) { /* 隐私模式忽略 */ }
}

function uid(prefix) {
  return String(prefix || "u") + Date.now().toString(36) +
         Math.floor(Math.random() * 1e9).toString(36);
}

/* 幂等键：8–64 位 [A-Za-z0-9_-]（与面板/服务端正则一致） */
function newIdemKey() { return "idem-" + uid(""); }

function num(v, dflt) {
  var n = Number(v);
  return isFinite(n) ? n : (dflt === undefined ? 0 : dflt);
}

/* ------------------------------------------------------------------ 状态 */
var ROUTES = [
  { id: "overview", hash: "#/overview", icon: "overview" },
  { id: "packages", hash: "#/packages", icon: "packages" },
  { id: "devices", hash: "#/devices", icon: "devices" },
  { id: "anomalies", hash: "#/anomalies", icon: "anomalies" },
  { id: "overdue", hash: "#/overdue", icon: "alert" },
  { id: "audit", hash: "#/audit", icon: "audit" }
];

var WRITE_DEFAULT_SIZE = 25;

var state = {
  route: "overview",
  meta: null,
  refreshMs: 15000,
  auto: true,
  timer: null,
  theme: "dark",
  silentHash: "",
  cache: { summary: null, anomalies: null, audit: null, overdue: null },
  /* 申报制（SPEC-declare §3.5）：逾期中心数据与请求序号 */
  ovdSeq: 0,
  pkg: { repo: "", status: "", q: "", sort: "updated", order: "desc", page: 1, size: 50,
         overdueFirst: false },
  pkgData: null,
  pkgSeq: 0,
  pages: { dev: 1, lease: 1, stuck: 1, orphan: 1, ghost: 1, audit: 1 },
  /* SPEC-link §4：设备页新增状态（面板侧）
     devFilter：online/idle/offline/all（默认 all）；
     devShowArchived：默认 false（默认隐藏 archived 设备）；
     devExpanded：当前展开行的 device_id；
     devDetailId：#/devices/<id> 详情页深链接；
     devEvents：行展开用的 /api/audit 事件缓存（按 device_id 过滤）。 */
  devFilter: "all",
  devShowArchived: false,
  devExpanded: "",
  devDetailId: "",
  devEvents: null,
  auditQ: "",
  focusSearchPending: false,

  /* v3 */
  write: false,
  writeReason: "",
  writeForcedOff: false,
  sel: { packages: {}, leases: {} },
  rows: { packages: [], leases: [] },
  auditTab: "events",
  auditFocus: "",
  wq: {
    action: "", actor: "", result: "", q: "", batch_id: "", write_id: "",
    order: "desc", page: 1, size: WRITE_DEFAULT_SIZE, range: ""
  },
  wdata: null,
  ddata: null,
  dq: { page: 1, size: 25, actor: "", reason: "", path: "", write_id: "" },
  dseq: 0,
  wseq: 0,
  notif: {
    items: [], unread: 0, latest: null, open: false, loading: false,
    timer: null, seq: 0, seen: { ts_epoch: 0, write_id: "" }, running: null,
    denied: 0,
    /* 申报制（SPEC-declare §3.5）：逾期未申报提醒（逐包，服务端每日去重） */
    overdue: [], overdueHours: null
  },
  settingsOpen: false
};

var drawerState = { open: false, repo: "", id: "", trigger: null, seq: 0 };
var helpOpen = false;

/* ------------------------------------------------------------------ 请求 */
function ApiError(message, status, url, body) {
  this.name = "ApiError";
  this.message = message;
  this.status = status || 0;
  this.url = url || "";
  this.body = body || null;
}
ApiError.prototype = Object.create(Error.prototype);

function markUpstream(ok, message, ms) {
  var text = ok ? tpl(T.up.ok, { ms: (ms === null || ms === undefined ? 0 : ms) })
                : T.err.upstream;
  var cls = "fp-pill " + (ok ? "fp-pill--ok" : "fp-pill--bad");
  ["#up-pill", "#up-pill-side"].forEach(function (sel) {
    var node = $(sel);
    if (!node) return;
    node.className = cls;
    node.textContent = text;
    node.title = ok ? "" : (message || "");
  });
}

function api(path) {
  var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
  return fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Accept": "application/json" }
  }).then(function (resp) {
    var ms = Math.round(((window.performance && performance.now) ? performance.now() : Date.now()) - t0);
    var ctype = resp.headers.get("content-type") || "";
    var parsed = null;
    var readBody = ctype.indexOf("application/json") === 0
      ? resp.json().catch(function () { return null; })
      : Promise.resolve(null);
    return readBody.then(function (data) {
      parsed = data;
      if (!resp.ok) {
        var msg = (parsed && parsed.error) ? String(parsed.error)
                                          : tpl(T.err.http, { code: resp.status });
        if (resp.status === 401) toast("danger", T.err.session);
        if (resp.status >= 500) markUpstream(false, msg);
        throw new ApiError(msg, resp.status, path, parsed);
      }
      if (!parsed) {
        markUpstream(false, T.err.badjson);
        throw new ApiError(T.err.badjson, resp.status, path);
      }
      markUpstream(true, null, ms);
      return parsed;
    });
  }).catch(function (err) {
    if (err instanceof ApiError) throw err;
    var e = new ApiError(T.err.network, 0, path);
    markUpstream(false, e.message);
    throw e;
  });
}

/* ---- CSRF（双提交：cookie fp_csrf + 头 X-CSRF-Token） ---- */
var csrfState = { token: "", pending: null };

function csrfToken(force) {
  if (!force && csrfState.token) return Promise.resolve(csrfState.token);
  if (csrfState.pending) return csrfState.pending;
  csrfState.pending = api("/api/csrf").then(function (d) {
    csrfState.token = (d && d.csrf) ? String(d.csrf) : "";
    csrfState.pending = null;
    return csrfState.token;
  }, function (err) {
    csrfState.pending = null;
    throw err;
  });
  return csrfState.pending;
}

/* 写请求：永不 throw（428/400/409 是业务语义，不是异常），返回统一信封 */
function writeRequest(path, body) {
  var idem = (body && body.__idem) || newIdemKey();
  var payload = {};
  Object.keys(body || {}).forEach(function (k) {
    if (k !== "__idem") payload[k] = body[k];
  });
  return csrfToken().then(function (tok) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": tok,
        "Idempotency-Key": idem
      },
      body: JSON.stringify(payload)
    });
  }).then(function (resp) {
    var replay = (resp.headers.get("Idempotent-Replay") || "") === "1";
    return resp.json().catch(function () { return null; }).then(function (data) {
      if (resp.status === 403 && data && data.code === "csrf_failed") {
        /* 令牌过期：刷新后由调用方决定是否重试（避免静默重复提交） */
        csrfToken(true).catch(function () {});
      }
      if (resp.status === 403 && data && data.code === "readonly") {
        onReadonlyRejected(data);
      }
      return {
        status: resp.status,
        ok: resp.ok,
        replay: replay,
        body: data || {},
        idem: idem
      };
    });
  }, function (err) {
    return {
      status: 0, ok: false, replay: false, idem: idem,
      body: { error: (err && err.message) ? String(err.message) : T.err.network,
              code: "network_error" }
    };
  });
}

function onReadonlyRejected() {
  /* 配置热加载把写开关关掉时，服务端会 403 readonly：前端立刻收起全部写入口
     （fail-closed）。写开关重新打开需要刷新页面，避免"刚被拒绝又亮起来"。 */
  state.writeForcedOff = true;
  state.write = false;
  state.writeReason = T.write.readonly;
  var d = $("#write-modal");
  if (d && !d.hidden) closeWrite(true);
  loadMeta().then(function () { applyWriteMode(true); });
  toast("info", T.write.toast.readonly);
}

/* ------------------------------------------------------------------ 写通道元信息 */
function writeMeta() { return (state.meta && state.meta.write) || null; }
function writeOn() {
  return !!(writeMeta() && writeMeta().enabled === true) && !state.writeForcedOff;
}
function writeMax() {
  var m = writeMeta();
  return (m && num(m.max_batch, 100)) || 100;
}
function writeThreshold() {
  var m = writeMeta();
  return (m && num(m.batch_threshold, 5)) || 5;
}
function writeActions() {
  var m = writeMeta();
  return (m && m.actions) || [];
}
function actionOf(id) {
  var list = writeActions();
  for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
  return null;
}
/* P2 动作（user.rekey）只在 API 目录里，不渲染入口（SPEC §2.7 红线） */
function actionVisible(id) {
  var spec = actionOf(id);
  if (!spec) return false;
  return String(spec.priority || "P0").toUpperCase() !== "P2";
}

/* ------------------------------------------------------------------ 语义块 */
function statusBadge(status, label) {
  var map = {
    available: { cls: "fp-badge--available", text: T.status.available },
    claimed: { cls: "fp-badge--claimed", text: T.status.claimed },
    completed: { cls: "fp-badge--completed", text: T.status.completed }
  };
  var hit = map[status];
  /* 映射表优先：列表页行内没有 status_label，抽屉里有；两处必须同词，
     所以固定用面板映射（未认领/处理中/已完成），label 只对未知状态兜底。 */
  var text = hit ? hit.text : (label || status || T.status.unknown);
  var node = el("span", { class: "fp-badge " + (hit ? hit.cls : "fp-badge--available") });
  node.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
  node.appendChild(el("span", { text: text }));
  return node;
}

function leaseBadge(kind) {
  var isPre = kind === "prelock";
  var node = el("span", {
    class: "fp-badge " + (isPre ? "fp-badge--claimed" : "fp-badge--info")
  });
  node.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
  node.appendChild(el("span", {
    text: isPre ? T.status.prelock : T.status.active
  }));
  return node;
}

/* SPEC-link §4.2：三态状态（服务端派生，前端不算时间）。
   online=🟢（completed 绿）、idle=🟡（warning 黄）、offline=⚪（info 灰）；
   未知值沿用 offline 兜底，但文字保留原样。 */
function devTriBadge(status) {
  var s = String(status || "offline");
  var map = {
    online:  { cls: "fp-badge--completed", text: T.misc.online },
    idle:    { cls: "fp-badge--warning",   text: T.misc.idle },
    offline: { cls: "fp-badge--info",      text: T.misc.offline }
  };
  var hit = map[s] || map.offline;
  var node = el("span", {
    class: "fp-badge fp-tristate " + hit.cls,
    dataset: { tristate: s }
  });
  node.appendChild(el("span", { class: "fp-tristate__ic", "data-tri": s,
                                "aria-hidden": "true" }));
  node.appendChild(el("span", { text: hit.text }));
  return node;
}

/* SPEC-link §4.1：显存 MB→GB 显示（≥1024 显示 GB，1 位小数），否则 MB；非法为 "-" */
function fmtVram(mb) {
  var n = Number(mb);
  if (mb === null || mb === undefined || mb === "" || !isFinite(n) || n <= 0) {
    return T.misc.noGpu;
  }
  if (n >= 1024) {
    var gb = n / 1024;
    return (gb >= 100 ? gb.toFixed(0) : gb.toFixed(1)) + " GB";
  }
  return fmtNum(Math.round(n)) + " MB";
}

/* epoch 秒 → 本地时间文本（captain 口径：unix 时间戳秒） */
function fmtEpoch(sec) {
  var n = Number(sec);
  if (sec === null || sec === undefined || sec === "" || !isFinite(n) || n <= 0) return null;
  return new Date(n * 1000).toLocaleString("zh-CN", DATE_FMT);
}

/* 行展开后的明细行（<tr class="fp-expandrow">）：本地包 ≤20 + hw_report_at +
   最近 10 条事件（/api/audit 按 device_id 过滤，不自造接口） */
function buildExpandRow(device) {
  var tr = el("tr", { class: "fp-expandrow", dataset: { expandfor: String(device.device_id || "") } });
  var cell = el("td", { colspan: "8" });
  cell.appendChild(buildExpandBody(device));
  tr.appendChild(cell);
  return tr;
}

function buildExpandBody(device) {
  var box = el("div", { class: "fp-expand" });

  var pkgsAll = Array.isArray(device.local_pkgs) ? device.local_pkgs : [];
  var pkgs = pkgsAll.slice(0, 20);
  var pkgBox = el("div", { class: "fp-expand__sec" });
  pkgBox.appendChild(el("h3", { class: "fp-expand__t", text: T.misc.devPkgs }));
  var pkgList = el("ul", { class: "fp-expand__list" });
  if (!pkgs.length) {
    pkgList.appendChild(el("li", { class: "fp-expand__none is-muted", text: T.misc.dash }));
  } else {
    pkgs.forEach(function (p) {
      pkgList.appendChild(el("li", { class: "is-mono", text: String(p) }));
    });
    if (pkgsAll.length > 20) {
      pkgList.appendChild(el("li", {
        class: "fp-expand__more",
        text: tpl(T.misc.morePkgs, { n: device.local_pkgs_count || pkgsAll.length })
      }));
    }
  }
  pkgBox.appendChild(pkgList);
  box.appendChild(pkgBox);

  var metaBox = el("div", { class: "fp-expand__sec" });
  metaBox.appendChild(el("h3", { class: "fp-expand__t", text: T.col.hwReport }));
  metaBox.appendChild(el("p", {
    class: "fp-expand__meta",
    text: fmtEpoch(device.hw_report_at) || T.misc.dash
  }));
  box.appendChild(metaBox);

  var evBox = el("div", { class: "fp-expand__sec" });
  evBox.appendChild(el("h3", { class: "fp-expand__t", text: T.misc.devEvents }));
  var evList = el("ul", { class: "fp-expand__list" });
  var evs = devEventsFor(String(device.device_id || ""));
  if (!evs.length) {
    evList.appendChild(el("li", { class: "fp-expand__none is-muted", text: T.misc.devEventNone }));
  } else {
    evs.forEach(function (e) {
      evList.appendChild(el("li", {
        class: "fp-expand__ev",
        text: (fmtTime(e.ts) || T.misc.dash) + " · " + String(e.kind || T.misc.dash) +
              (e.pkg ? " · " + String(e.pkg) : "")
      }));
    });
  }
  evBox.appendChild(evList);
  box.appendChild(evBox);
  return box;
}

/* 事件源：/api/audit（panel 已聚合的事件流），按 device_id 过滤取最近 10 条 */
function devEventsFor(deviceId) {
  var cache = state.devEvents;
  if (!cache || !Array.isArray(cache.lines)) return [];
  var needle = String(deviceId).toLowerCase();
  var out = [];
  for (var i = cache.lines.length - 1; i >= 0 && out.length < 10; i--) {
    var e = cache.lines[i] || {};
    var hay = [e.device_id, e.pkg, e.detail, e.actor].map(function (v) {
      return v === null || v === undefined ? "" : String(v).toLowerCase();
    });
    if (hay.indexOf(needle) < 0) continue;
    out.push(e);
  }
  return out;
}

function ensureDevEvents() {
  if (state.devEvents) return Promise.resolve(state.devEvents);
  return api("/api/audit").then(function (d) {
    state.devEvents = d;
    return d;
  }).catch(function () {
    state.devEvents = { ok: false, lines: [] };
    return state.devEvents;
  });
}

function toggleDevExpand(deviceId, btn) {
  var box = $("#dev-wrap");
  if (!box) return;
  var row = box.querySelector('tr[data-rowkey="' + String(deviceId).replace(/"/g, "") + '"]');
  if (!row) return;
  var open = state.devExpanded === String(deviceId);
  box.querySelectorAll("tr.fp-expandrow").forEach(function (n) { n.remove(); });
  var buttons = box.querySelectorAll("button[data-expand]");
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].setAttribute("aria-expanded", "false");
    buttons[i].title = T.misc.expandRow;
  }
  if (open) {
    state.devExpanded = "";
    if (btn) btn.title = T.misc.expandRow;
    return;
  }
  state.devExpanded = String(deviceId);
  if (btn) {
    btn.setAttribute("aria-expanded", "true");
    btn.title = T.misc.collapseRow;
  }
  ensureDevEvents().then(function () {
    var cur = box.querySelector('tr[data-rowkey="' + String(deviceId).replace(/"/g, "") + '"]');
    if (!cur || state.devExpanded !== String(deviceId)) return;
    var after = cur.nextSibling;
    /* 展开行插在设备行后：先移除旧展开行避免重复 */
    box.querySelectorAll("tr.fp-expandrow").forEach(function (n) { n.remove(); });
    var summary = (state.cache.summary && (state.cache.summary.devices || []))
      .filter(function (d) { return String(d.device_id) === String(deviceId); })[0];
    if (!summary) return;
    var expandTr = buildExpandRow(summary);
    if (after && after.parentNode === box) box.insertBefore(expandTr, after);
    else cur.parentNode.insertBefore(expandTr, cur.nextSibling);
  });
}

/* SPEC-link §4.4：设备详情页体（当前租约/历史事件/本地包/解绑） */
function buildDeviceDetailBody(device, leases) {
  var box = el("div", { class: "fp-devdetail" });
  var head = el("div", { class: "fp-devdetail__hd" });
  var back = el("button", { class: "fp-btn fp-btn--sm", type: "button" });
  back.appendChild(el("span", { text: T.misc.devBack }));
  back.addEventListener("click", function () { state.devDetailId = ""; go("devices"); });
  head.appendChild(back);
  head.appendChild(el("h2", { class: "fp-devdetail__t is-mono", text: device.device_id || T.misc.dash }));
  head.appendChild(devTriBadge(device.status));
  box.appendChild(head);

  var grid = el("dl", { class: "fp-devdetail__grid" });
  [
    [T.col.user, device.user_id || T.misc.dash],
    [T.col.tier, device.tier || T.misc.dash],
    [T.col.gpu, device.gpu_name || T.misc.dash],
    [T.col.vram, fmtVram(device.gpu_vram_mb)],
    [T.col.mode, device.mode || T.misc.dash],
    [T.col.firstSeen, fmtEpoch(device.first_seen) || T.misc.dash],
    [T.col.hwReport, fmtEpoch(device.hw_report_at) || T.misc.dash],
    [T.col.pkgs, fmtNum(device.local_pkgs_count) || T.misc.dash],
    [T.col.ver, device.client_ver || T.misc.dash],
    [T.col.speed, fmtNum(device.speed)]
  ].forEach(function (kv) {
    grid.appendChild(el("dt", { text: kv[0] }));
    grid.appendChild(el("dd", { text: String(kv[1]) }));
  });
  box.appendChild(grid);

  /* 当前租约（进度/速度/TTL） */
  var sec1 = el("section", { class: "fp-devdetail__sec" });
  sec1.appendChild(el("h3", { class: "fp-devdetail__st", text: T.misc.devLease }));
  var mine = (leases || []).filter(function (l) {
    return String(l.device_id || "") === String(device.device_id || "");
  });
  if (!mine.length) {
    sec1.appendChild(el("p", { class: "is-muted", text: T.misc.devNoLease }));
  } else {
    var ul = el("ul", { class: "fp-devdetail__list" });
    mine.forEach(function (l) {
      var li = el("li");
      li.appendChild(el("span", { class: "is-mono", text: String(l.package || l.lease_id || T.misc.dash) }));
      var bits = [" · " + (parseProgress(l.progress).idle
        ? T.misc.dash : parseProgress(l.progress).pct + "%"),
        " · " + (fmtHeld(l.held_seconds) || T.misc.dash)];
      var ob = overdueBadge(l);
      if (ob) li.appendChild(ob);
      li.appendChild(el("span", { text: bits.join("") }));
      /* 设备详情页：单包释放入口（走 package.release 严格确认） */
      if (writeOn() && actionVisible("package.release") && l.repo && l.package) {
        var relBtn = rowActionBtn(T.misc.releasePkg, "danger", (function (repo, pkg) {
          return function () {
            startWrite("package.release", [{ repo: repo, id: pkg }]);
          };
        })(l.repo, l.package), T.misc.releasePkg);
        relBtn.classList.add("fp-devlease-rel");
        li.appendChild(relBtn);
      }
      ul.appendChild(li);
    });
    sec1.appendChild(ul);
  }
  box.appendChild(sec1);

  /* 本地包列表 */
  var sec2 = el("section", { class: "fp-devdetail__sec" });
  sec2.appendChild(el("h3", { class: "fp-devdetail__st", text: T.misc.devPkgs }));
  var pkgList = el("ul", { class: "fp-devdetail__list" });
  var pkgs = Array.isArray(device.local_pkgs) ? device.local_pkgs : [];
  if (!pkgs.length) pkgList.appendChild(el("li", { class: "is-muted", text: T.misc.dash }));
  pkgs.slice(0, 20).forEach(function (p) {
    pkgList.appendChild(el("li", { class: "is-mono", text: String(p) }));
  });
  if (pkgs.length > 20) {
    pkgList.appendChild(el("li", {
      class: "fp-expand__more",
      text: tpl(T.misc.morePkgs, { n: device.local_pkgs_count || pkgs.length })
    }));
  }
  sec2.appendChild(pkgList);
  box.appendChild(sec2);

  /* 历史事件（/api/audit 按 device_id 过滤，≤10） */
  var sec3 = el("section", { class: "fp-devdetail__sec" });
  sec3.appendChild(el("h3", { class: "fp-devdetail__st", text: T.misc.devEvents }));
  var evList = el("ul", { class: "fp-devdetail__list" });
  var evs = devEventsFor(String(device.device_id || ""));
  if (!evs.length) evList.appendChild(el("li", { class: "is-muted", text: T.misc.devEventNone }));
  evs.forEach(function (e) {
    evList.appendChild(el("li", {
      text: (fmtTime(e.ts) || T.misc.dash) + " · " + String(e.kind || T.misc.dash)
    }));
  });
  sec3.appendChild(evList);
  box.appendChild(sec3);

  /* 解绑按钮：沿用现有严格确认弹窗（device.unassign impact.mutations 口径） */
  if (writeOn()) {
    var acts = el("div", { class: "fp-devdetail__acts" });
    if (actionVisible("device.unassign")) {
      acts.appendChild(rowActionBtn(T.write.act.unassign, "danger", function () {
        startWrite("device.unassign", [{ device_id: device.device_id }]);
      }));
    }
    /* 强制解绑：连 user_id 绑定一起删。设备卡在"已绑定到别的用户"时用这条 */
    if (actionVisible("device.forget")) {
      acts.appendChild(rowActionBtn(T.write.act.forget, "danger", function () {
        startWrite("device.forget", [{ device_id: device.device_id }]);
      }));
    }
    /* 清空用户：这台设备顺带的整条身份都不要了（换人换机、测试号回收） */
    if (device.user_id && actionVisible("user.forget")) {
      acts.appendChild(rowActionBtn(T.write.act.forgetUser, "danger", function () {
        startWrite("user.forget", [{ user_id: device.user_id }]);
      }));
    }
    if (acts.childNodes.length) box.appendChild(acts);
  }
  return box;
}

function renderDeviceDetail() {
  var devBox = $("#dev-wrap");
  var leaseBox = $("#lease-wrap");
  var d = state.cache.summary;
  $("#dev-title").textContent = T.misc.devDetail;
  var hint = $("#dev-hint");
  if (hint) hint.textContent = "";
  devBox.setAttribute("aria-busy", "false");
  leaseBox.setAttribute("aria-busy", "false");
  if (!d) {
    devBox.replaceChildren(skeletonTable(11, 3));
    return;
  }
  var device = (d.devices || []).filter(function (x) {
    return String(x.device_id) === String(state.devDetailId);
  })[0];
  if (!device) {
    devBox.replaceChildren(emptyState(T.empty.devicesNone, T.empty.hint404, null));
    return;
  }
  devBox.replaceChildren(buildDeviceDetailBody(device, d.leases || []));
}

function devStatusBadge(status) {
  var s = String(status || "");
  if (!s) return el("span", { class: "fp-badge fp-badge--available", text: T.misc.dash });
  if (s === "online" || s === "idle" || s === "offline") return devTriBadge(s);
  if (s === "archived") {
    return el("span", { class: "fp-badge fp-badge--available", text: T.misc.archived });
  }
  return el("span", { class: "fp-badge fp-badge--info", text: s });
}

/* 写结果徽章：成功 / 失败 / 部分失败 / 进行中（颜色不是唯一信号：都带文字） */
function resultBadge(result) {
  var map = {
    ok: { cls: "fp-badge--completed", text: T.write.notif.result.ok },
    partial: { cls: "fp-badge--warning", text: T.write.notif.result.partial },
    error: { cls: "fp-badge--danger", text: T.write.notif.result.error },
    running: { cls: "fp-badge--info", text: T.write.notif.result.running }
  };
  var hit = map[result] || map.error;
  var node = el("span", { class: "fp-badge " + hit.cls });
  node.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
  node.appendChild(el("span", { text: hit.text }));
  return node;
}

function parseProgress(v) {
  if (!v || typeof v !== "object") return { idle: true };
  if (typeof v.percent === "number" && isFinite(v.percent)) {
    var pct = Math.max(0, Math.min(100, Math.round(v.percent)));
    return { idle: false, done: pct, total: 100, pct: pct };
  }
  var done = Number(v.done);
  var total = Number(v.total);
  if (!isFinite(done) || !isFinite(total) || total <= 0) return { idle: true };
  var p = Math.max(0, Math.min(100, Math.round(done / total * 100)));
  return { idle: false, done: done, total: total, pct: p };
}

function progressCell(progress) {
  var box = el("span", { class: "fp-progresscell" });
  var info = parseProgress(progress);
  var bar = el("progress", { class: "fp-progress", "aria-label": T.col.progress });
  if (info.idle) {
    bar.classList.add("is-idle");
  } else {
    bar.setAttribute("max", String(info.total));
    bar.setAttribute("value", String(info.done));
    if (info.pct >= 100) bar.classList.add("is-full");
    if (info.pct <= 0) bar.classList.add("is-empty");
  }
  box.appendChild(bar);
  box.appendChild(el("span", {
    class: "fp-progresscell__pct",
    text: info.idle ? T.misc.dash : info.pct + "%"
  }));
  return box;
}

function fmtHeld(seconds) {
  /* 已持有时长 → 相对时间（“2 天 3 小时”）。数据是服务端 held_seconds
     （SPEC-declare §3.2），前端只做展示格式化，不判定逾期。 */
  var n = Number(seconds);
  if (!isFinite(n) || n < 0) return null;
  var s = Math.round(n);
  if (s < 60) return s + " 秒";
  if (s < 3600) {
    var m = Math.floor(s / 60);
    return m + " 分钟";
  }
  if (s < 86400) {
    var h = Math.floor(s / 3600);
    var rm = Math.floor((s % 3600) / 60);
    return rm ? h + " 小时 " + rm + " 分钟" : h + " 小时";
  }
  var d = Math.floor(s / 86400);
  var rh = Math.floor((s % 86400) / 3600);
  return rh ? d + " 天 " + rh + " 小时" : d + " 天";
}

function heldNode(lease) {
  /* 「已持有时长」节点：优先 held_seconds（服务端），本地每秒插值（claim_at + t）。
     超过 30 天保险丝（fuse_exceeded，服务端标记）给红色特殊提示。 */
  var held = Number(lease && lease.held_seconds);
  var claim = Number(lease && lease.claim_at);
  if (!isFinite(held) || held < 0) {
    if (isFinite(claim) && claim > 0) {
      held = Math.max(0, (Date.now() / 1000) - claim);
    } else {
      return el("span", { class: "fp-ttl", text: T.misc.dash });
    }
  }
  var fuse = !!(lease && lease.fuse_exceeded);
  var node = el("span", {
    class: "fp-ttl" + (fuse ? " is-danger" : ""),
    dataset: { held: String(Math.round(held)), heldAt: String(Date.now()),
               heldFuse: fuse ? "1" : "" },
    text: fmtHeld(held) || T.misc.dash
  });
  if (fuse) node.title = T.misc.heldFuse.replace("{t}", node.textContent);
  return node;
}

function overdueBadge(lease) {
  /* 逾期/保险丝红色徽标：判定完全来自服务端 overdue/fuse_exceeded 字段，
     前端不自算时间（SPEC-declare §3.1）。 */
  if (!lease) return null;
  if (lease.fuse_exceeded) {
    return el("span", { class: "fp-badge fp-badge--danger fp-overdue-badge",
                        text: T.misc.fuseBadge });
  }
  if (lease.overdue) {
    var t = fmtHeld(lease.overdue_seconds);
    return el("span", {
      class: "fp-badge fp-badge--danger fp-overdue-badge",
      text: T.misc.overdueBadge.replace("{t}", t || "")
    });
  }
  return null;
}

function td(label, value, opts) {
  opts = opts || {};
  var cell = el("td", { dataset: { label: label } });
  if (opts.cls) cell.className = opts.cls;
  if (value === null || value === undefined || value === "") {
    cell.textContent = T.misc.dash;
    cell.className = (cell.className ? cell.className + " " : "") + "is-muted";
  } else if (value instanceof Node) {
    cell.appendChild(value);
  } else {
    cell.textContent = String(value);
  }
  if (opts.title) cell.title = String(opts.title);
  return cell;
}

function thCell(col) {
  if (col.header) {
    var custom = el("th", { scope: "col" });
    if (col.cls) {
      String(col.cls).split(/\s+/).forEach(function (tk) {
        if (tk) custom.classList.add(tk);
      });
    }
    custom.appendChild(col.header());
    return custom;
  }
  var th = el("th", { scope: "col" });
  if (col.cls) th.className = col.cls;
  if (!col.sort) {
    th.textContent = col.label;
    return th;
  }
  th.setAttribute("aria-sort", "none");
  var btn = el("button", { class: "fp-sort", type: "button", dataset: { sort: col.sort } });
  btn.appendChild(el("span", { text: col.label }));
  var ico = el("span", { class: "fp-sort__icon", "aria-hidden": "true" });
  ico.appendChild(icon("sort", 12));
  btn.appendChild(ico);
  btn.addEventListener("click", function () { onSort(col.sort); });
  th.appendChild(btn);
  return th;
}

function buildTable(caption, cols, rows, rowFn) {
  var table = el("table", { class: "fp-table" });
  table.appendChild(el("caption", { class: "fp-sr", text: caption }));
  var thead = el("thead");
  var htr = el("tr");
  cols.forEach(function (c) {
    var th = thCell(c);
    /* thCell 已用 className 写入 col.cls；这里按 token 逐个补，避免
       "fp-col-first fp-align-right" 这种多类字符串传给 classList.add 时
       抛 DOMTokenList 错误把整张表渲染打断 */
    if (c.cls) {
      String(c.cls).split(/\s+/).forEach(function (tk) {
        if (tk && !th.classList.contains(tk)) th.classList.add(tk);
      });
    }
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);
  var tbody = el("tbody");
  rows.forEach(function (r) { tbody.appendChild(rowFn(r)); });
  table.appendChild(tbody);
  return table;
}

/* 只有表头没有数据行的空表（空态/错误态的行内占位用） */
function tableShell(caption, cols, bodyRows) {
  var table = el("table", { class: "fp-table" });
  table.appendChild(el("caption", { class: "fp-sr", text: caption }));
  var thead = el("thead");
  var htr = el("tr");
  cols.forEach(function (c) { htr.appendChild(thCell(c)); });
  thead.appendChild(htr);
  table.appendChild(thead);
  var tbody = el("tbody");
  (bodyRows || []).forEach(function (r) { tbody.appendChild(r); });
  table.appendChild(tbody);
  return table;
}

function emptyRow(colspan, node) {
  var tr = el("tr", { class: "fp-emptyrow" });
  var cell = el("td", { colspan: String(colspan) });
  cell.appendChild(node);
  tr.appendChild(cell);
  return tr;
}

function skeletonTable(cols, rows) {
  var table = el("table", { class: "fp-table" });
  table.appendChild(el("caption", { class: "fp-sr", text: T.app.loading }));
  var thead = el("thead");
  var htr = el("tr");
  for (var c = 0; c < cols; c++) htr.appendChild(el("th", { scope: "col" }));
  thead.appendChild(htr);
  table.appendChild(thead);
  var tbody = el("tbody");
  for (var r = 0; r < rows; r++) {
    var tr = el("tr");
    for (var i = 0; i < cols; i++) {
      var cell = el("td");
      cell.appendChild(el("span", {
        class: "fp-skeleton fp-skeleton--text",
        "aria-hidden": "true"
      }));
      tr.appendChild(cell);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function emptyState(title, hint, action) {
  var box = el("div", { class: "fp-empty" });
  box.appendChild(icon("inbox", 32, "fp-empty__ic"));
  box.appendChild(el("p", { class: "fp-empty__t", text: title }));
  if (hint) box.appendChild(el("p", { class: "fp-empty__h", text: hint }));
  if (action) box.appendChild(action);
  return box;
}

function errorState(err, retry, inline) {
  var box = el("div", { class: "fp-error" + (inline ? " fp-error--inline" : ""), role: "alert" });
  box.appendChild(icon("alert", inline ? 16 : 20, "fp-error__ic"));
  var body = el("div", { class: "fp-error__body" });
  var isUpstream = err && String(err.message || "").indexOf("上游") === 0;
  body.appendChild(el("p", {
    class: "fp-error__t",
    text: isUpstream ? T.err.upstream : T.err.failed
  }));
  body.appendChild(el("p", {
    class: "fp-error__h",
    text: err && err.message ? String(err.message) : T.err.failed
  }));
  if (retry) {
    var btn = el("button", { class: "fp-btn fp-btn--sm", type: "button" });
    btn.appendChild(icon("refresh", 16));
    btn.appendChild(el("span", { text: T.err.retry }));
    btn.addEventListener("click", function () { retry(); });
    body.appendChild(btn);
  }
  if (!inline) {
    var det = el("details", { class: "fp-error__d" });
    det.appendChild(el("summary", { text: T.misc.details }));
    det.appendChild(el("pre", {
      text: "GET " + ((err && err.url) || "?") + " → " +
            String((err && err.status) || 0)
    }));
    body.appendChild(det);
  }
  box.appendChild(body);
  return box;
}

/* ------------------------------------------------------------------ 导航 */
function buildNav() {
  var side = $("#nav-side-list");
  var tabs = $("#tabs");
  var tabbar = $("#tabbar");
  side.replaceChildren();
  tabs.replaceChildren();
  tabbar.replaceChildren();

  ROUTES.forEach(function (route) {
    var li = el("li");
    var a = el("a", {
      href: route.hash,
      class: "fp-navitem",
      dataset: { route: route.id }
    });
    a.appendChild(icon(route.icon, 20));
    a.appendChild(el("span", { text: T.nav[route.id] }));
    li.appendChild(a);
    side.appendChild(li);

    var tab = el("a", { href: route.hash, class: "fp-tab", dataset: { route: route.id } });
    tab.appendChild(icon(route.icon, 18));
    tab.appendChild(el("span", { text: T.nav[route.id] }));
    tabs.appendChild(tab);

    var item = el("a", {
      href: route.hash,
      class: "fp-tabbar__item",
      dataset: { route: route.id }
    });
    item.appendChild(icon(route.icon, 20));
    item.appendChild(el("span", { text: T.nav[route.id] }));
    tabbar.appendChild(item);
  });
}

function setActiveNav(id) {
  var nodes = document.querySelectorAll("[data-route]");
  for (var i = 0; i < nodes.length; i++) {
    var node = nodes[i];
    if (node.dataset.route === id) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  }
}

/* ------------------------------------------------------------------ 路由（URL 即状态） */
function parseHash() {
  var raw = location.hash || "#/overview";
  var body = raw.replace(/^#/, "");
  var parts = body.split("?");
  var path = (parts[0] || "/overview").replace(/^\//, "") || "overview";
  var params = {};
  if (parts[1]) {
    var sp = new URLSearchParams(parts[1]);
    sp.forEach(function (v, k) { params[k] = v; });
  }
  var known = ROUTES.some(function (r) { return r.id === path; });
  /* 设备详情深链接：#/devices/<device_id>（SPEC-link §4.4） */
  var devDetail = null;
  var mDev = /^devices\/(.+)$/.exec(path);
  if (mDev) {
    devDetail = decodeURIComponent(mDev[1]);
    path = "devices";
  }
  var known2 = known || path === "devices";
  return { id: known2 ? path : "overview", params: params, deepPackage: path === "package",
           devDetail: devDetail };
}

function applyRoute(fromHash) {
  var parsed = parseHash();
  var id = parsed.deepPackage ? "packages" : parsed.id;
  if (parsed.devDetail) state.devDetailId = parsed.devDetail;
  else if (parsed.id === "devices") state.devDetailId = "";
  if (parsed.params.theme === "light" || parsed.params.theme === "dark") {
    setTheme(parsed.params.theme, true);
    state.themePinned = true;
  }
  if (parsed.deepPackage && parsed.params.id) {
    openDrawer(parsed.params.repo || "", parsed.params.id, null);
  }
  if (fromHash) {
    if (id === "packages") applyPackageParams(parsed.params);
    else if (id === "devices") applyDeviceParams(parsed.params);
    else if (id === "anomalies") applyAnomalyParams(parsed.params);
    else if (id === "audit") applyAuditParams(parsed.params);
  }

  if (state.route !== id && id === "overdue") {
    /* 重进逾期中心时重新拉取：resolve 状态要新鲜（SPEC-declare §3.4） */
    state.cache.overdue = null;
  }
  state.route = id;
  ROUTES.forEach(function (r) {
    var sec = document.getElementById("page-" + r.id);
    if (sec) sec.hidden = (r.id !== id);
  });
  setActiveNav(id);
  var title = T.nav[id];
  $("#page-title").textContent = title;
  document.title = title + " · " + T.app.title;
  if (state.focusSearchPending) {
    state.focusSearchPending = false;
    var target = (id === "audit") ? $("#f-audit") : $("#f-q");
    if (target) { target.focus(); target.select(); }
  }
  loadRoute();
}

function applyPackageParams(params) {
  var s = state.pkg;
  if (typeof params.repo === "string") s.repo = params.repo;
  if (typeof params.status === "string") s.status = params.status;
  if (typeof params.q === "string") s.q = params.q;
  if (params.sort === "updated" || params.sort === "size" || params.sort === "id") s.sort = params.sort;
  if (params.order === "asc" || params.order === "desc") s.order = params.order;
  s.overdueFirst = params.overdue_first === "1";
  var page = parseInt(params.page, 10);
  s.page = isFinite(page) && page > 0 ? page : 1;
  var size = parseInt(params.size, 10);
  var sizes = (state.meta && state.meta.page_sizes) || [25, 50, 100, 200];
  if (isFinite(size) && sizes.indexOf(size) >= 0) {
    s.size = size;
    storeSet("fp.size", size);
  }
  syncPackageControls();
}

function applyDeviceParams(params) {
  var tab = params.tab === "leases" ? "leases" : "devices";
  state.devTab = tab;
  if (params.device) state.devFocus = { kind: "device", id: String(params.device) };
  else if (params.lease) state.devFocus = { kind: "lease", id: String(params.lease) };
  /* SPEC-link §4.5：筛选状态可入 URL（deep link 可复现） */
  if (["online", "idle", "offline", "all"].indexOf(params.filter) >= 0) {
    state.devFilter = params.filter;
  }
  state.devShowArchived = params.archived === "1";
  var sel = $("#f-dev-status");
  if (sel) sel.value = state.devFilter;
  var arch = $("#f-dev-archived");
  if (arch) arch.checked = state.devShowArchived;
}

function applyAnomalyParams(params) {
  var tab = (params.tab === "stuck" || params.tab === "orphan" || params.tab === "ghost")
    ? params.tab : "";
  state.anFocus = tab;
}

function applyAuditParams(params) {
  var tab = params.tab === "writes" ? "writes"
    : (params.tab === "denied" ? "denied" : "events");
  state.auditTab = tab;
  var w = state.wq;
  if (typeof params.action === "string") w.action = params.action;
  if (typeof params.actor === "string") w.actor = params.actor;
  if (params.result === "ok" || params.result === "error") w.result = params.result;
  else if (params.result === "") w.result = "";
  if (typeof params.q === "string") w.q = params.q;
  if (typeof params.batch === "string") w.batch_id = params.batch;
  if (typeof params.write === "string") w.write_id = params.write;
  if (params.order === "asc" || params.order === "desc") w.order = params.order;
  var page = parseInt(params.page, 10);
  w.page = isFinite(page) && page > 0 ? page : 1;
  var size = parseInt(params.size, 10);
  w.size = [25, 50, 100].indexOf(size) >= 0 ? size : WRITE_DEFAULT_SIZE;
  if (typeof params.range === "string") w.range = params.range;
  if (typeof params.q === "string") state.auditQ = params.q;
  var d = state.dq;
  if (typeof params.reason === "string") d.reason = params.reason;
  if (typeof params.path === "string") d.path = params.path;
  if (typeof params.write === "string") d.write_id = params.write;
  if (typeof params.actor === "string") d.actor = params.actor;
  d.page = w.page;
  d.size = w.size;
  syncAuditControls();
}

/* URL 组装：筛选/排序/分页/主题都写进 hash（replaceState，不产生历史条目）；
   切页用 location.hash（产生历史条目）——见 SPEC §9.8 */
function currentQuery() {
  var sp = new URLSearchParams();
  var theme = state.themePinned ? state.theme : "";
  if (state.route !== "overview" && theme) sp.set("theme", theme);
  if (state.route === "packages") {
    var s = state.pkg;
    if (s.repo) sp.set("repo", s.repo);
    if (s.status) sp.set("status", s.status);
    if (s.q) sp.set("q", s.q);
    if (s.sort !== "updated") sp.set("sort", s.sort);
    if (s.order !== "desc") sp.set("order", s.order);
    if (s.page > 1) sp.set("page", String(s.page));
    if (s.size !== 50) sp.set("size", String(s.size));
    if (s.overdueFirst) sp.set("overdue_first", "1");
  } else if (state.route === "devices") {
    if (state.devTab === "leases") sp.set("tab", "leases");
    if (state.devFilter !== "all") sp.set("filter", state.devFilter);
    if (state.devShowArchived) sp.set("archived", "1");
    var f = state.devFocus;
    if (f && f.kind === "device") sp.set("device", f.id);
    else if (f && f.kind === "lease") sp.set("lease", f.id);
  } else if (state.route === "anomalies") {
    if (state.anFocus) sp.set("tab", state.anFocus);
  } else if (state.route === "audit") {
    var w = state.wq;
    if (state.auditTab === "writes") sp.set("tab", "writes");
    if (state.auditTab === "denied") {
      var dq = state.dq;
      sp.set("tab", "denied");
      if (dq.reason) sp.set("reason", dq.reason);
      if (dq.path) sp.set("path", dq.path);
      if (dq.actor) sp.set("actor", dq.actor);
      if (dq.write_id) sp.set("write", dq.write_id);
      if (dq.page > 1) sp.set("page", String(dq.page));
      if (dq.size !== WRITE_DEFAULT_SIZE) sp.set("size", String(dq.size));
      return sp.toString();
    }
    if (w.action) sp.set("action", w.action);
    if (w.actor) sp.set("actor", w.actor);
    if (w.result) sp.set("result", w.result);
    if (w.q) sp.set("q", w.q);
    if (w.batch_id) sp.set("batch", w.batch_id);
    if (w.write_id) sp.set("write", w.write_id);
    if (w.range) sp.set("range", w.range);
    if (w.page > 1) sp.set("page", String(w.page));
    if (w.size !== WRITE_DEFAULT_SIZE) sp.set("size", String(w.size));
  }
  return sp.toString();
}

function syncUrl() {
  var qs = currentQuery();
  var hash = "#/" + state.route + (qs ? "?" + qs : "");
  if (hash === location.hash) return;
  state.silentHash = hash;
  try { history.replaceState(null, "", hash); } catch (e) { location.hash = hash; }
}

function go(id) {
  var route = ROUTES.filter(function (r) { return r.id === id; })[0];
  if (!route) return;
  var qs = "";
  if (id === state.route) qs = currentQuery();
  var hash = qs ? (route.hash + "?" + qs) : route.hash;
  if (location.hash === hash) { loadRoute(); return; }
  location.hash = hash;
}

/* 设备详情深链接：#/devices/<id>（SPEC-link §4.4，URL 即状态） */
function goDeviceDetail(deviceId) {
  if (!deviceId) return;
  var hash = "#/devices/" + encodeURIComponent(String(deviceId));
  if (location.hash === hash) { loadRoute(); return; }
  location.hash = hash;
}

function loadRoute() {
  switch (state.route) {
    case "overview": return loadOverview();
    case "packages": return loadPackages();
    case "devices": return loadDevices();
    case "anomalies": return loadAnomalies();
    case "overdue": return loadOverdue();
    case "audit": return loadAudit();
    default: return loadOverview();
  }
}

/* ------------------------------------------------------------------ 逾期未申报中心（申报制 SPEC-declare §3.5） */
function loadOverdue() {
  var box = $("#ovd-list");
  var seq = ++state.ovdSeq;
  if (!state.cache.overdue) {
    box.setAttribute("aria-busy", "true");
    box.replaceChildren(skeletonTable(1, 3));
  }
  $("#ovd-title").textContent = T.card.overdue;
  $("#ovd-note").textContent = T.misc.overdueNote;
  $("#ovd-hint").textContent = "";
  return api("/api/overdue").then(function (d) {
    if (seq !== state.ovdSeq) return;
    state.cache.overdue = d;
    renderOverdue(d);
  }).catch(function () {
    if (seq !== state.ovdSeq) return;
    box.setAttribute("aria-busy", "false");
    box.replaceChildren(emptyState(T.empty.overdueNone, null, null));
    $("#ovd-info").textContent = T.pager.empty;
  });
}

function renderOverdue(d) {
  var box = $("#ovd-list");
  box.setAttribute("aria-busy", "false");
  var items = (d && d.items || []).slice();
  var declareH = num(d && d.declare_hours, 48);
  $("#ovd-hint").textContent = items.length
    ? tpl(T.stat.leasesHint, { n: fmtNum(items.length) })
    : "";
  if (!items.length) {
    box.replaceChildren(emptyState(T.empty.overdueNone, null, null));
    $("#ovd-info").textContent = T.pager.empty;
    return;
  }
  /* 服务端已按 (repo,pkg,本地日期) 每日去重：前端直接逐包渲染，不再聚合；
     清单通常很小（每日去重 × 未解决），不分页——保持全站 7 个 fp-pager 红线 */
  var canWrite = writeOn();
  var frag = document.createDocumentFragment();
  items.forEach(function (it) {
    frag.appendChild(overdueCard(it, declareH, canWrite));
  });
  box.replaceChildren(frag);
  $("#ovd-info").textContent = tpl(T.pager.simple, { n: fmtNum(items.length) });
}

function overdueCard(it, declareH, canWrite) {
  var resolved = !it.still_open;
  var card = el("article", {
    class: "fp-ovd-card" + (resolved ? " is-resolved" : " is-overdue"),
    dataset: { overdue: String(it.repo || "") + "/" + String(it.pkg || "") }
  });
  var top = el("div", { class: "fp-ovd-card__top" });
  top.appendChild(el("span", { class: "fp-ovd-card__pkg is-mono",
                               text: String(it.repo || "") + "/" + String(it.pkg || "") }));
  if (resolved) {
    top.appendChild(el("span", { class: "fp-badge fp-badge--info",
                                 text: T.misc.overdueResolved }));
  } else if (it.fuse_exceeded) {
    top.appendChild(el("span", { class: "fp-badge fp-badge--danger",
                                 text: T.misc.fuseBadge }));
  } else {
    top.appendChild(el("span", { class: "fp-badge fp-badge--danger",
                                 text: T.misc.overdueBadge
                                   .replace("{t}", fmtHeld(it.overdue_seconds) || "") }));
  }
  card.appendChild(top);
  var meta = el("div", { class: "fp-ovd-card__meta" });
  meta.appendChild(el("span", { class: "is-mono",
                                text: String(it.device_id || T.misc.dash) }));
  meta.appendChild(el("span", { class: "fp-notif__sep", text: "·" }));
  meta.appendChild(el("span", {
    text: T.misc.heldFor.replace("{t}", fmtHeld(it.held_seconds) || T.misc.dash)
  }));
  var reminds = num(it.remind_count, 0);
  if (reminds > 0) {
    meta.appendChild(el("span", { class: "fp-notif__sep", text: "·" }));
    meta.appendChild(el("span", {
      text: T.misc.overdueReminds.replace("{n}", fmtNum(reminds))
    }));
  }
  card.appendChild(meta);
  var acts = el("div", { class: "fp-notif__acts-row" });
  var view = el("a", {
    class: "fp-btn fp-btn--sm",
    href: "#/package?repo=" + encodeURIComponent(String(it.repo || "")) +
          "&id=" + encodeURIComponent(String(it.pkg || ""))
  });
  view.appendChild(el("span", { text: T.misc.viewPkg }));
  acts.appendChild(view);
  if (canWrite && !resolved && it.repo && it.pkg &&
      actionVisible("package.release")) {
    var rel = el("button", {
      class: "fp-btn fp-btn--sm fp-btn--danger fp-write-action", type: "button"
    });
    rel.appendChild(el("span", { text: T.misc.releasePkg }));
    rel.addEventListener("click", function () {
      startWrite("package.release", [{ repo: it.repo, id: it.pkg }]);
    });
    acts.appendChild(rel);
  }
  card.appendChild(acts);
  return card;
}

/* ------------------------------------------------------------------ 概览 */
function renderStatSkeleton(container, count) {
  container.setAttribute("aria-busy", "true");
  var items = [];
  for (var i = 0; i < count; i++) {
    items.push(el("div", { class: "fp-stat is-loading", "aria-hidden": "true" }, [
      el("span", { class: "fp-skeleton fp-skeleton--text fp-w-50" }),
      el("span", { class: "fp-skeleton fp-skeleton--stat" })
    ]));
  }
  container.replaceChildren.apply(container, items);
}

function buildStat(item) {
  var node = item.link
    ? el("a", { class: "fp-stat is-link" + (item.cls ? " " + item.cls : ""), href: item.link })
    : el("div", { class: "fp-stat" + (item.cls ? " " + item.cls : "") });
  node.appendChild(el("div", { class: "fp-stat__k", text: item.k }));
  node.appendChild(el("div", { class: "fp-stat__v", text: item.v }));
  if (item.h) node.appendChild(el("div", { class: "fp-stat__h", text: item.h }));
  return node;
}

function loadOverview() {
  var stats = $("#ov-stats");
  var repoBox = $("#ov-repo");
  if (!state.cache.summary) {
    renderStatSkeleton(stats, 9);
    repoBox.setAttribute("aria-busy", "true");
    repoBox.replaceChildren(skeletonTable(9, 3));
  }
  return api("/api/summary").then(function (d) {
    state.cache.summary = d;
    renderOverview(d);
    $("#ft-updated").textContent = tpl(T.app.footerUpdated, { ts: d.ts || clockNow() });
    setLive(tpl(T.live.updated, { time: clockNow() }));
  }).catch(function (err) {
    setLive(tpl(T.live.failed, { error: err.message }));
    if (state.cache.summary) {
      toast("danger", tpl(T.live.failed, { error: err.message }));
    } else {
      stats.setAttribute("aria-busy", "false");
      stats.replaceChildren(errorState(err, loadOverview));
      repoBox.setAttribute("aria-busy", "false");
      repoBox.replaceChildren();
    }
  });
}

function renderOverview(d) {
  var t = d.totals || {};
  var stats = $("#ov-stats");
  stats.setAttribute("aria-busy", "false");
  var items = [
    { k: T.stat.packages, v: fmtNum(t.packages),
      h: tpl(T.stat.reposHint, { n: fmtNum(t.repos) }), link: "#/packages" },
    { k: T.stat.available, v: fmtNum(t.available) },
    { k: T.stat.claimed, v: fmtNum(t.claimed) },
    { k: T.stat.completed, v: fmtNum(t.completed) },
    { k: T.stat.active_leases, v: fmtNum(t.active_leases),
      cls: Number(t.active_leases) > 0 ? "is-good" : "" },
    { k: T.stat.prelock_leases, v: fmtNum(t.prelock_leases) },
    { k: T.stat.devices_online, v: fmtNum(t.devices_online),
      cls: Number(t.devices_online) > 0 ? "is-good" : "" },
    { k: T.stat.free_for_assign, v: fmtNum(t.free_for_assign) },
    { k: T.stat.bytes_total, v: fmtBytes(t.bytes_total) || T.misc.dash,
      cls: "is-num" }
  ];
  stats.replaceChildren.apply(stats, items.map(buildStat));
  renderRepoTable(d.repos || []);
}

function renderRepoTable(repos) {
  var box = $("#ov-repo");
  box.setAttribute("aria-busy", "false");
  $("#ov-repo-title").textContent = T.card.repos;
  $("#ov-repo-hint").textContent = tpl(T.stat.reposHint, { n: fmtNum(repos.length) });
  var note = $("#ov-note");
  if (repos.length > 200) {
    note.hidden = false;
    note.textContent = tpl(T.misc.tooManyRepos, { n: fmtNum(repos.length) });
  } else {
    note.hidden = true;
    note.textContent = "";
  }
  if (!repos.length) {
    box.replaceChildren(emptyState(T.empty.repoNone, T.empty.hintNone, null));
    return;
  }
  var canWrite = writeOn();
  var cols = [
    { label: T.col.repo, cls: "fp-col-first" },
    { label: T.col.type },
    { label: T.col.enabled },
    { label: T.col.total, cls: "fp-align-right" },
    { label: T.col.repoAvailable, cls: "fp-align-right" },
    { label: T.col.repoClaimed, cls: "fp-align-right" },
    { label: T.col.repoCompleted, cls: "fp-align-right" },
    { label: T.col.capacity, cls: "fp-align-right" }
  ];
  if (canWrite) cols.push({ label: T.col.actions, cls: "fp-align-right" });
  var rows = repos.slice().sort(function (a, b) {
    return String(a.repo || "").localeCompare(String(b.repo || ""));
  });
  var caption = tpl(T.misc.caption, { name: T.card.repos, sort: T.col.repo, order: T.misc.asc });
  box.replaceChildren(buildTable(caption, cols, rows, function (r) {
    var s = r.summary || {};
    var tr = el("tr");
    tr.appendChild(td(T.col.repo, r.repo || null, { cls: "fp-col-first is-mono" }));
    tr.appendChild(td(T.col.type, r.type || null));
    var enabledCell = el("td", { dataset: { label: T.col.enabled } });
    if (r.enabled) {
      var badge = el("span", { class: "fp-badge fp-badge--completed" });
      badge.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
      badge.appendChild(el("span", { text: T.misc.enabled }));
      enabledCell.appendChild(badge);
    } else {
      var off = el("span", { class: "fp-badge fp-badge--available" });
      off.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
      off.appendChild(el("span", { text: T.misc.disabled }));
      enabledCell.appendChild(off);
    }
    tr.appendChild(enabledCell);
    tr.appendChild(td(T.col.total, fmtNum(s.total || 0), { cls: "fp-align-right is-num" }));
    tr.appendChild(td(T.col.repoAvailable, fmtNum(s.available || 0), { cls: "fp-align-right is-num" }));
    tr.appendChild(td(T.col.repoClaimed, fmtNum(s.claimed || 0), { cls: "fp-align-right is-num" }));
    tr.appendChild(td(T.col.repoCompleted, fmtNum(s.completed || 0), { cls: "fp-align-right is-num" }));
    tr.appendChild(td(T.col.capacity, fmtBytes(s.bytes_total) || T.misc.dash, { cls: "fp-align-right is-num" }));
    if (canWrite) {
      var cell = el("td", {
        dataset: { label: T.col.actions },
        class: "fp-align-right fp-cell-actions"
      });
      if (r.enabled && actionVisible("repo.disable")) {
        cell.appendChild(rowActionBtn(T.write.act.disable, "danger", function () {
          startWrite("repo.disable", [{ repo: r.repo }]);
        }));
      }
      tr.appendChild(cell);
    }
    return tr;
  }));
}

/* ------------------------------------------------------------------ 包列表 */
function packagesQuery(withPage) {
  var s = state.pkg;
  var sp = new URLSearchParams();
  sp.set("sort", s.sort);
  sp.set("order", s.order);
  if (s.repo) sp.set("repo", s.repo);
  if (s.status) sp.set("status", s.status);
  if (s.q) sp.set("q", s.q);
  /* 申报制（SPEC-declare §3.5）：逾期优先（可选，默认关） */
  if (s.overdueFirst) sp.set("overdue_first", "1");
  if (withPage) {
    sp.set("page", String(s.page));
    sp.set("size", String(s.size));
  }
  return sp.toString();
}

function onSort(key) {
  var s = state.pkg;
  if (s.sort === key) {
    s.order = s.order === "desc" ? "asc" : "desc";
  } else {
    s.sort = key;
    s.order = defaultOrderOf(key);
  }
  s.page = 1;
  clearSel("packages");
  syncUrl();
  loadPackages();
}

function defaultOrderOf(key) {
  var sorts = (state.meta && state.meta.sorts) || [];
  for (var i = 0; i < sorts.length; i++) {
    if (sorts[i].value === key) return sorts[i].default_order || "desc";
  }
  return key === "id" ? "asc" : "desc";
}

function loadPackages() {
  var seq = ++state.pkgSeq;
  var box = $("#pkg-wrap");
  var toolbar = $("#pkg-toolbar");
  toolbar.classList.add("is-busy");
  box.setAttribute("aria-busy", "true");
  if (!box.querySelector("table")) box.replaceChildren(skeletonTable(pkgColCount(), 5));

  return api("/api/packages?" + packagesQuery(true)).then(function (d) {
    if (seq !== state.pkgSeq) return;
    state.pkgData = d;
    toolbar.classList.remove("is-busy");
    renderPackages(d);
    setStale(false);
    setLive(tpl(T.live.updated, { time: clockNow() }));
  }).catch(function (err) {
    if (seq !== state.pkgSeq) return;
    toolbar.classList.remove("is-busy");
    box.setAttribute("aria-busy", "false");
    if (state.pkgData && state.pkgData.packages) {
      setStale(true);
      toast("danger", tpl(T.live.failed, { error: err.message }));
    } else {
      box.replaceChildren(errorState(err, loadPackages));
      $("#pkg-info").textContent = T.pager.empty;
    }
    setLive(tpl(T.live.failed, { error: err.message }));
  });
}

function pkgColCount() {
  return 7 + (writeOn() ? 2 : 0);
}

function pkgKey(p) { return String(p.repo || "") + "\u0000" + String(p.id || ""); }

function renderPackages(d) {
  var box = $("#pkg-wrap");
  box.setAttribute("aria-busy", "false");
  var rows = d.packages || [];
  var page = d.page || {};
  var s = state.pkg;
  var sortLabel = sortLabelOf(s.sort);
  var orderLabel = s.order === "desc" ? T.misc.desc : T.misc.asc;
  var canWrite = writeOn();

  state.rows.packages = rows;
  pruneSel("packages", rows.map(pkgKey));

  var note = $("#pkg-note");
  if (page.truncated) {
    note.hidden = false;
    note.textContent = T.misc.truncatedNote;
  } else {
    note.hidden = true;
    note.textContent = "";
  }

  var cols = [];
  if (canWrite) {
    cols.push({
      label: T.col.select, cls: "fp-col-select",
      header: function () { return selHeader("packages", rows, pkgKey); }
    });
  }
  cols.push({ label: T.col.repo, cls: "fp-col-first" });
  cols.push({ label: T.col.id, sort: "id", cls: "fp-cell-wrap" });
  cols.push({ label: T.col.size, sort: "size", cls: "fp-align-right" });
  cols.push({ label: T.col.status });
  cols.push({ label: T.col.owner });
  cols.push({ label: T.col.downloaded, cls: "fp-col-md" });
  cols.push({ label: T.col.updated, sort: "updated", cls: "fp-align-right" });
  if (canWrite) cols.push({ label: T.col.actions, cls: "fp-align-right" });
  var caption = tpl(T.misc.caption, { name: T.nav.packages, sort: sortLabel, order: orderLabel });

  var filtered = !!(s.repo || s.status || s.q);
  if (!rows.length) {
    var action = null;
    if (filtered) {
      action = el("button", { class: "fp-btn fp-btn--sm", type: "button" });
      action.textContent = T.btn.clearFilter;
      action.addEventListener("click", clearFilters);
    }
    box.replaceChildren(tableShell(caption, cols, [emptyRow(cols.length, emptyState(
      filtered ? T.empty.filtered : T.empty.none,
      filtered ? T.empty.hintFiltered : T.empty.hintNone,
      action
    ))]));
  } else {
    box.replaceChildren(buildTable(caption, cols, rows, rowPackage));
  }

  /* 排序指示：aria-sort 落在 th 上 */
  var ths = box.querySelectorAll("thead th");
  for (var i = 0; i < ths.length; i++) {
    var btn = ths[i].querySelector(".fp-sort");
    if (!btn) continue;
    if (btn.dataset.sort === s.sort) {
      ths[i].setAttribute("aria-sort", s.order === "desc" ? "descending" : "ascending");
      btn.dataset.active = "true";
    } else {
      ths[i].setAttribute("aria-sort", "none");
      delete btn.dataset.active;
    }
  }

  var total = Number(page.total || 0);
  var pages = Math.max(1, Number(page.pages || 1));
  var current = Number(page.page || 1);
  if (!rows.length && total > 0 && current > pages) {
    s.page = pages;
    syncUrl();
    loadPackages();
    return;
  }
  $("#pkg-info").textContent = pages > 1
    ? tpl(T.pager.info, { p: current, pages: pages, n: fmtNum(total) })
    : (total ? tpl(T.pager.simple, { n: fmtNum(total) }) : T.pager.empty);
  $("#pkg-prev").disabled = current <= 1;
  $("#pkg-next").disabled = current >= pages;

  var exportLabel = total > 5000
    ? tpl(T.btn.exportCount, { n: fmtNum(total) })
    : T.btn.export;
  $("#pkg-export-label").textContent = exportLabel;
  var table = box.querySelector(".fp-table");
  if (table) table.setAttribute("data-page", String(current));
  updateBulkBar("packages");
}

function sortLabelOf(key) {
  var sorts = (state.meta && state.meta.sorts) || [];
  for (var i = 0; i < sorts.length; i++) {
    if (sorts[i].value === key) return sorts[i].label;
  }
  return key;
}

function rowPackage(p) {
  var tr = el("tr");
  if (writeOn()) {
    tr.appendChild(selCell("packages", pkgKey(p), (p.repo || "") + "/" + (p.id || "")));
  }
  tr.appendChild(td(T.col.repo, p.repo || null, { cls: "fp-col-first is-mono" }));

  var idCell = el("td", {
    dataset: { label: T.col.id },
    class: "fp-cell-wrap fp-cell-title"
  });
  var btn = el("button", {
    class: "fp-linkbtn",
    type: "button",
    title: p.id || "",
    "aria-label": tpl(T.misc.pkgDetail, { name: p.id || "" })
  });
  btn.textContent = p.id || T.misc.dash;
  btn.addEventListener("click", function () { openDrawer(p.repo || "", p.id, btn); });
  idCell.appendChild(btn);
  tr.appendChild(idCell);

  tr.appendChild(td(T.col.size, fmtBytes(p.size), { cls: "fp-align-right is-num" }));

  var statusCell = el("td", {
    dataset: { label: T.col.status },
    class: "fp-cell-status"
  });
  statusCell.appendChild(statusBadge(p.status));
  /* 申报制（SPEC-declare §3.5）：逾期包行内红色徽标（服务端 overdue 字段） */
  var pOb = p.overdue ? overdueBadge({ overdue: true }) : null;
  if (pOb) statusCell.appendChild(pOb);
  tr.appendChild(statusCell);

  tr.appendChild(td(T.col.owner, p.owner || null));
  tr.appendChild(td(T.col.downloaded, downloadBadge(p.downloaded_at), { cls: "fp-col-md" }));
  tr.appendChild(td(T.col.updated, fmtTime(p.updated_at), {
    cls: "fp-align-right is-num",
    title: p.updated_at || ""
  }));
  if (writeOn()) tr.appendChild(pkgActionCell(p));
  return tr;
}

function pkgActionCell(p) {
  var cell = el("td", {
    dataset: { label: T.col.actions },
    class: "fp-align-right fp-cell-actions"
  });
  var target = { repo: p.repo, id: p.id };
  if (p.status === "completed" && actionVisible("package.reopen")) {
    cell.appendChild(rowActionBtn(T.write.act.reopen, "danger", function () {
      startWrite("package.reopen", [target]);
    }));
  } else if (p.status === "claimed" && actionVisible("package.release")) {
    cell.appendChild(rowActionBtn(T.write.act.release, "danger", function () {
      startWrite("package.release", [target]);
    }));
  }
  return cell;
}

function downloadBadge(at) {
  if (!at) return null;
  var node = el("span", { class: "fp-badge fp-badge--info" });
  node.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
  node.appendChild(el("span", { text: T.drawer.downloaded }));
  node.title = fmtTime(at) || "";
  return node;
}

function clearFilters() {
  var s = state.pkg;
  s.repo = "";
  s.status = "";
  s.q = "";
  s.page = 1;
  clearSel("packages");
  syncPackageControls();
  syncUrl();
  loadPackages();
}

function setStale(stale) {
  var badge = $("#pkg-stale");
  badge.hidden = !stale;
  badge.textContent = stale ? T.toast.stale : "";
  $("#pkg-toolbar").classList.toggle("is-stale", !!stale);
}

/* ------------------------------------------------------------------ 多选与批量 */
function selCount(kind) {
  return Object.keys(state.sel[kind] || {}).length;
}
function isSel(kind, key) {
  return !!(state.sel[kind] && state.sel[kind][key]);
}
function setSel(kind, key, on) {
  if (!state.sel[kind]) state.sel[kind] = {};
  if (on) state.sel[kind][key] = true;
  else delete state.sel[kind][key];
}
function clearSel(kind) {
  state.sel[kind] = {};
  updateBulkBar(kind);
  syncMaster(kind);
}
/* 换筛选/排序/翻页时清空（跨页不保留选择，SPEC §9.2） */
function pruneSel(kind, keys) {
  var keep = {};
  keys.forEach(function (k) { if (isSel(kind, k)) keep[k] = true; });
  state.sel[kind] = keep;
}

function selHeader(kind, rows, keyOf) {
  var wrap = el("span", { class: "fp-selectwrap" });
  var input = el("input", {
    type: "checkbox",
    class: "fp-rowcheck",
    id: "sel-all-" + kind,
    "aria-label": T.write.bulk.selectAll
  });
  var keys = rows.map(keyOf);
  var picked = keys.filter(function (k) { return isSel(kind, k); }).length;
  input.checked = keys.length > 0 && picked === keys.length;
  input.indeterminate = picked > 0 && picked < keys.length;
  input.addEventListener("change", function () {
    keys.forEach(function (k) { setSel(kind, k, input.checked); });
    updateBulkBar(kind);
    /* 行内复选框同步（不重绘整张表，避免丢焦点） */
    var boxes = document.querySelectorAll('.fp-rowcheck[data-bulkkind="' + kind + '"]');
    for (var i = 0; i < boxes.length; i++) boxes[i].checked = input.checked;
  });
  if (!state.master) state.master = {};
  state.master[kind] = { input: input, rows: keys };
  wrap.appendChild(input);
  return wrap;
}

function syncMaster(kind) {
  var m = state.master && state.master[kind];
  if (!m || !m.input) return;
  var picked = m.rows.filter(function (k) { return isSel(kind, k); }).length;
  m.input.checked = m.rows.length > 0 && picked === m.rows.length;
  m.input.indeterminate = picked > 0 && picked < m.rows.length;
}

function selCell(kind, key, label) {
  var cell = el("td", { dataset: { label: T.col.select }, class: "fp-cell-select" });
  var input = el("input", {
    type: "checkbox",
    class: "fp-rowcheck",
    dataset: { bulkkind: kind },
    "aria-label": tpl(T.write.bulk.row, { label: label })
  });
  input.checked = isSel(kind, key);
  input.addEventListener("change", function () {
    setSel(kind, key, input.checked);
    updateBulkBar(kind);
    syncMaster(kind);
  });
  cell.appendChild(input);
  return cell;
}

function rowActionBtn(label, tone, fn, title) {
  var btn = el("button", {
    class: "fp-btn fp-btn--sm fp-rowact fp-write-action" +
           (tone === "danger" ? " fp-btn--danger" : ""),
    type: "button"
  });
  if (title) btn.title = title;
  btn.appendChild(el("span", { text: label }));
  btn.addEventListener("click", fn);
  return btn;
}

function bulkBarFor(kind) {
  return document.getElementById(kind === "packages" ? "bulkbar-pkg" : "bulkbar-lease");
}

function updateBulkBar(kind) {
  var bar = bulkBarFor(kind);
  if (!bar) return;
  if (!writeOn()) { bar.hidden = true; bar.replaceChildren(); return; }
  var n = selCount(kind);
  var max = writeMax();
  bar.hidden = n === 0;
  if (!n) { bar.replaceChildren(); return; }

  var tooMany = n > max;
  var kids = [];
  kids.push(el("span", { class: "fp-bulkbar__n", "aria-live": "polite",
                         text: tpl(T.write.bulk.selected, { n: n }) }));
  kids.push(el("span", { class: "fp-bulkbar__hint", text: T.write.bulk.crossPage }));

  var acts = el("div", { class: "fp-bulkbar__actions" });
  if (kind === "packages") {
    var completed = 0;
    var claimed = 0;
    var seen = {};
    state.rows.packages.forEach(function (p) {
      var k = pkgKey(p);
      if (seen[k] || !isSel("packages", k)) return;
      seen[k] = true;
      if (p.status === "completed") completed += 1;
      else if (p.status === "claimed") claimed += 1;
    });
    acts.appendChild(bulkBtn(T.write.act.reopenN, "danger", "reopen",
      tpl(T.write.bulk.pickable, { n: completed }), completed, tooMany, function () {
        startWrite("package.reopen", selectedPkgTargets("completed"));
      }));
    acts.appendChild(bulkBtn(T.write.act.releaseN, "danger", "release",
      tpl(T.write.bulk.pickable, { n: claimed }), claimed, tooMany, function () {
        startWrite("package.release", selectedPkgTargets("claimed"));
      }));
  } else {
    acts.appendChild(bulkBtn(T.write.act.leaseN, "danger", "lease-release",
      tpl(T.write.bulk.pickable, { n: n }), n, tooMany, function () {
        startWrite("lease.release", selectedLeaseTargets());
      }));
  }
  kids.push(acts);
  if (tooMany) {
    kids.push(el("span", { class: "fp-note", text: tpl(T.write.bulk.tooMany, { n: max }) }));
  }
  var clear = el("button", { class: "fp-btn fp-btn--sm", type: "button", id: "bulk-clear-" + kind });
  clear.appendChild(el("span", { text: T.write.bulk.clear }));
  clear.addEventListener("click", function () { clearSel(kind); });
  kids.push(clear);
  bar.replaceChildren.apply(bar, kids);
}

function bulkBtn(label, tone, key, hint, eligible, tooMany, fn) {
  var btn = el("button", {
    class: "fp-btn fp-btn--sm fp-write-action" + (tone === "danger" ? " fp-btn--danger" : ""),
    type: "button",
    dataset: { bulk: key },
    title: hint
  });
  btn.appendChild(el("span", { text: label }));
  btn.disabled = eligible === 0 || tooMany;
  btn.addEventListener("click", fn);
  return btn;
}

function selectedPkgTargets(status) {
  var out = [];
  var seen = {};
  state.rows.packages.forEach(function (p) {
    var k = pkgKey(p);
    if (seen[k] || !isSel("packages", k)) return;
    seen[k] = true;
    if (p.status === status) out.push({ repo: p.repo, id: p.id });
  });
  return out;
}

function selectedLeaseTargets() {
  var out = [];
  var seen = {};
  state.rows.leases.forEach(function (l) {
    var k = String(l.lease_id || "");
    if (!k || seen[k] || !isSel("leases", k)) return;
    seen[k] = true;
    out.push({ lease_id: l.lease_id });
  });
  return out;
}

/* ------------------------------------------------------------------ 分页小工具 */
function clampPage(page, total, size) {
  var pages = Math.max(1, Math.ceil(total / size));
  var p = Number(page) || 1;
  return Math.min(Math.max(1, p), pages);
}

function slicePage(rows, page, size) {
  var total = rows.length;
  var pages = Math.max(1, Math.ceil(total / size));
  var p = Math.min(Math.max(1, Number(page) || 1), pages);
  return { rows: rows.slice((p - 1) * size, p * size), page: p, pages: pages, total: total };
}

function setPager(prefix, slice) {
  var info = document.getElementById(prefix + "-info");
  var prev = document.getElementById(prefix + "-prev");
  var next = document.getElementById(prefix + "-next");
  if (info) {
    info.textContent = slice.pages > 1
      ? tpl(T.pager.info, { p: slice.page, pages: slice.pages, n: fmtNum(slice.total) })
      : (slice.total ? tpl(T.pager.simple, { n: fmtNum(slice.total) }) : T.pager.empty);
  }
  if (prev) prev.disabled = slice.page <= 1;
  if (next) next.disabled = slice.page >= slice.pages;
}

/* ------------------------------------------------------------------ 设备 */
function loadDevices() {
  var devBox = $("#dev-wrap");
  var leaseBox = $("#lease-wrap");
  if (!state.cache.summary) {
    devBox.replaceChildren(skeletonTable(writeOn() ? 12 : 11, 3));
    leaseBox.replaceChildren(skeletonTable(writeOn() ? 9 : 7, 3));
  }
  devBox.setAttribute("aria-busy", "true");
  leaseBox.setAttribute("aria-busy", "true");
  return api("/api/summary").then(function (d) {
    state.cache.summary = d;
    /* 详情深链接：#/devices/<id> 时渲染单设备详情（SPEC-link §4.4） */
    if (state.devDetailId) {
      renderDeviceDetail();
    } else {
      renderDevices(d);
    }
    $("#dev-ts").textContent = tpl(T.app.footerUpdated, { ts: d.ts || clockNow() });
    setLive(tpl(T.live.updated, { time: clockNow() }));
  }).catch(function (err) {
    devBox.setAttribute("aria-busy", "false");
    leaseBox.setAttribute("aria-busy", "false");
    devBox.replaceChildren(errorState(err, loadDevices));
    leaseBox.replaceChildren();
    setLive(tpl(T.live.failed, { error: err.message }));
  });
}

function renderDevices(d) {
  var devBox = $("#dev-wrap");
  var leaseBox = $("#lease-wrap");
  devBox.setAttribute("aria-busy", "false");
  leaseBox.setAttribute("aria-busy", "false");
  var canWrite = writeOn();

  /* SPEC-link §4.5：默认隐藏 archived；筛选下拉 online/idle/offline/all。
     排序建议（§4.2）：online → idle → offline，组内 last_seen 倒序（非硬项）。 */
  var rank = { online: 0, idle: 1, offline: 2 };
  var devices = (d.devices || []).filter(function (r) {
    if (!state.devShowArchived && String(r.status) === "archived") return false;
    if (state.devFilter !== "all" && String(r.status) !== state.devFilter) return false;
    return true;
  }).sort(function (a, b) {
    var ra = rank[String(a.status)];
    var rb = rank[String(b.status)];
    if (ra !== rb) return (ra === undefined ? 9 : ra) - (rb === undefined ? 9 : rb);
    return Number(b.last_seen || 0) - Number(a.last_seen || 0);
  });
  $("#dev-title").textContent = T.card.devices;
  $("#dev-hint").textContent = tpl(T.stat.devicesHint, { n: fmtNum(devices.length) });

  var devCols = [
    { label: T.col.device, cls: "fp-col-first" },
    { label: T.col.gpu },
    { label: T.col.vram, cls: "fp-align-right" },
    { label: T.col.user },
    { label: T.col.tier },
    { label: T.col.speed, cls: "fp-align-right" },
    { label: T.col.firstSeen, cls: "fp-align-right" },
    { label: T.col.pkgs, cls: "fp-align-right" },
    { label: T.col.status },
    { label: T.col.lastSeen },
    { label: T.col.ver }
  ];
  if (canWrite) devCols.push({ label: T.col.actions, cls: "fp-align-right" });
  var devPage = clampPage(state.pages.dev, devices.length, 50);
  state.pages.dev = devPage;
  var devSlice = slicePage(devices, devPage, 50);
  if (!devices.length) {
    devBox.replaceChildren(emptyState(T.empty.devicesNone, T.empty.hintNone, null));
    $("#dev-info").textContent = T.pager.empty;
    $("#dev-prev").disabled = true;
    $("#dev-next").disabled = true;
  } else {
    devBox.replaceChildren(buildTable(T.card.devices, devCols, devSlice.rows, function (r) {
      var tr = el("tr", { dataset: { rowkey: String(r.device_id || "") } });
      var devCell = el("td", {
        dataset: { label: T.col.device },
        class: "fp-col-first is-mono fp-cell-title"
      });
      /* 点设备名 → 详情页深链接 #/devices/<id>（SPEC-link §4.4） */
      var devBtn = el("button", {
        class: "fp-linkbtn",
        type: "button",
        title: tpl(T.misc.pkgDetail, { name: r.device_id || "" })
      });
      devBtn.textContent = r.device_id || T.misc.dash;
      devBtn.addEventListener("click", function () {
        goDeviceDetail(r.device_id);
      });
      devCell.appendChild(devBtn);
      tr.appendChild(devCell);
      tr.appendChild(td(T.col.gpu, r.gpu_name || null, {
        cls: "fp-cell-wrap", title: r.gpu_name || ""
      }));
      tr.appendChild(td(T.col.vram, fmtVram(r.gpu_vram_mb), { cls: "fp-align-right is-num" }));
      tr.appendChild(td(T.col.user, r.user_id || null));
      tr.appendChild(td(T.col.tier, r.tier || null));
      tr.appendChild(td(T.col.speed, fmtNum(r.speed), { cls: "fp-align-right is-num" }));
      tr.appendChild(td(T.col.firstSeen, fmtEpoch(r.first_seen) || relativeTime(r.first_seen), {
        cls: "fp-align-right is-num", title: fmtEpoch(r.first_seen) || ""
      }));
      tr.appendChild(td(T.col.pkgs, (r.local_pkgs_count === null ||
                                     r.local_pkgs_count === undefined)
                                    ? null : fmtNum(r.local_pkgs_count),
                        { cls: "fp-align-right is-num" }));
      var sc = el("td", { dataset: { label: T.col.status }, class: "fp-cell-status" });
      sc.appendChild(devStatusBadge(r.status));
      tr.appendChild(sc);
      /* 行展开按钮（SPEC-link §4.3）：aria-expanded + 最小 44px 触控 */
      var exCell = el("td", { dataset: { label: T.misc.expandRow },
                              class: "fp-align-right fp-cell-actions" });
      var exBtn = el("button", {
        class: "fp-iconbtn fp-expandbtn", type: "button",
        dataset: { expand: String(r.device_id || "") },
        "aria-expanded": "false",
        "aria-label": T.misc.expandRow,
        title: T.misc.expandRow
      });
      exBtn.textContent = "▾";
      exBtn.addEventListener("click", function () {
        toggleDevExpand(r.device_id, exBtn);
      });
      exCell.appendChild(exBtn);
      tr.appendChild(exCell);
      tr.appendChild(td(T.col.lastSeen, relativeTime(r.last_seen), {
        cls: "fp-align-right is-num", title: fmtTime(r.last_seen) || ""
      }));
      tr.appendChild(td(T.col.ver, r.client_ver || null, { cls: "is-mono" }));
      if (canWrite) {
        var cell = el("td", {
          dataset: { label: T.col.actions },
          class: "fp-align-right fp-cell-actions"
        });
        if (actionVisible("device.unassign")) {
          cell.appendChild(rowActionBtn(T.write.act.unassign, "danger", function () {
            startWrite("device.unassign", [{ device_id: r.device_id }]);
          }));
        }
        if (actionVisible("device.forget")) {
          cell.appendChild(rowActionBtn(T.write.act.forget, "danger", function () {
            startWrite("device.forget", [{ device_id: r.device_id }]);
          }));
        }
        tr.appendChild(cell);
      }
      return tr;
    }));
    setPager("dev", devSlice);
    markFocus(devBox, state.devFocus && state.devFocus.kind === "device" ? state.devFocus.id : "");
  }

  var leases = (d.leases || []).slice().sort(function (a, b) {
    /* 申报制：不再有 TTL 可排。逾期（服务端标记）优先，其余按已持时长倒序
       （越久越前，与原 TTL 列的“最紧急在前”语义对齐）。 */
    var ax = a.overdue ? 1 : 0;
    var bx = b.overdue ? 1 : 0;
    if (ax !== bx) return bx - ax;
    ax = Number(a.held_seconds);
    bx = Number(b.held_seconds);
    if (!isFinite(ax)) ax = -1;
    if (!isFinite(bx)) bx = -1;
    return bx - ax;
  });
  $("#lease-title").textContent = T.card.leases;
  $("#lease-hint").textContent = tpl(T.stat.leasesHint, { n: fmtNum(leases.length) });
  var leaseCols = [];
  if (canWrite) {
    leaseCols.push({
      label: T.col.select, cls: "fp-col-select",
      header: function () { return selHeader("leases", leaseSlice.rows, leaseKey); }
    });
  }
  leaseCols.push({ label: T.col.kind, cls: "fp-col-first" });
  leaseCols.push({ label: T.col.id });
  leaseCols.push({ label: T.col.user });
  leaseCols.push({ label: T.col.device });
  leaseCols.push({ label: T.col.ttl, cls: "fp-align-right" });
  leaseCols.push({ label: T.col.progress });
  leaseCols.push({ label: T.col.beat });
  if (canWrite) leaseCols.push({ label: T.col.actions, cls: "fp-align-right" });
  var leasePage = clampPage(state.pages.lease, leases.length, 50);
  state.pages.lease = leasePage;
  var leaseSlice = slicePage(leases, leasePage, 50);
  state.rows.leases = leaseSlice.rows;
  pruneSel("leases", leaseSlice.rows.map(leaseKey));
  if (!leases.length) {
    leaseBox.replaceChildren(emptyState(T.empty.leasesNone, T.empty.hintNone, null));
    $("#lease-info").textContent = T.pager.empty;
    $("#lease-prev").disabled = true;
    $("#lease-next").disabled = true;
  } else {
    leaseBox.replaceChildren(buildTable(T.card.leases, leaseCols, leaseSlice.rows, function (l) {
      var tr = el("tr", { dataset: { rowkey: String(l.lease_id || "") } });
      if (canWrite) tr.appendChild(selCell("leases", leaseKey(l), l.lease_id || ""));
      var kindCell = el("td", {
        dataset: { label: T.col.kind },
        class: "fp-col-first fp-cell-status"
      });
      kindCell.appendChild(leaseBadge(l.kind));
      tr.appendChild(kindCell);
      var pkgCell = el("td", {
        dataset: { label: T.col.id },
        class: "fp-cell-wrap fp-cell-title"
      });
      var btn = el("button", {
        class: "fp-linkbtn",
        type: "button",
        title: l.package || "",
        "aria-label": tpl(T.misc.pkgDetail, { name: l.package || "" })
      });
      btn.textContent = l.package || T.misc.dash;
      btn.addEventListener("click", function () { openDrawer(l.repo || "", l.package, btn); });
      pkgCell.appendChild(btn);
      tr.appendChild(pkgCell);
      tr.appendChild(td(T.col.user, l.user_id || null));
      tr.appendChild(td(T.col.device, l.device_id || null, { cls: "is-mono" }));
      var heldCell = el("td", {
        dataset: { label: T.col.ttl },
        class: "fp-align-right is-num"
      });
      var ob = overdueBadge(l);
      if (ob) heldCell.appendChild(ob);
      heldCell.appendChild(heldNode(l));
      tr.appendChild(heldCell);
      tr.appendChild(td(T.col.progress, progressCell(l.progress)));
      tr.appendChild(td(T.col.beat, relativeTime(l.last_beat_at) || relativeTime(l.last_beat_ago)));
      if (canWrite) {
        var cell = el("td", {
          dataset: { label: T.col.actions },
          class: "fp-align-right fp-cell-actions"
        });
        if (actionVisible("lease.release")) {
          cell.appendChild(rowActionBtn(T.write.act.release, "danger", function () {
            startWrite("lease.release", [{ lease_id: l.lease_id }]);
          }));
        }
        tr.appendChild(cell);
      }
      return tr;
    }));
    setPager("lease", leaseSlice);
    markFocus(leaseBox, state.devFocus && state.devFocus.kind === "lease" ? state.devFocus.id : "");
    updateBulkBar("leases");
    syncMaster("leases");
  }
}

function leaseKey(l) { return String(l.lease_id || ""); }

/* 深链接定位：给行加高亮并滚到可视区（不改变任何数据） */
function markFocus(box, key) {
  if (!key || !box) return;
  var rows = box.querySelectorAll("tr[data-rowkey]");
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].dataset.rowkey === String(key)) {
      rows[i].classList.add("is-focus");
      if (rows[i].scrollIntoView) {
        try { rows[i].scrollIntoView({ block: "center" }); } catch (e) { /* 忽略 */ }
      }
      return;
    }
  }
}

/* ------------------------------------------------------------------ 异常 */
function loadAnomalies() {
  var box = $("#an-stuck");
  ["an-stuck", "an-orphan", "an-ghost"].forEach(function (id) {
    var node = document.getElementById(id);
    node.setAttribute("aria-busy", "true");
    if (!state.cache.anomalies) node.replaceChildren(skeletonTable(writeOn() ? 4 : 3, 3));
  });
  if (!state.cache.anomalies) renderStatSkeleton($("#an-stats"), 3);
  return api("/api/anomalies").then(function (d) {
    state.cache.anomalies = d;
    renderAnomalies(d);
    setLive(tpl(T.live.updated, { time: clockNow() }));
  }).catch(function (err) {
    ["an-stuck", "an-orphan", "an-ghost"].forEach(function (id) {
      document.getElementById(id).setAttribute("aria-busy", "false");
    });
    box.replaceChildren(errorState(err, loadAnomalies));
    $("#an-stats").setAttribute("aria-busy", "false");
    $("#an-stats").replaceChildren(errorState(err, loadAnomalies));
    setLive(tpl(T.live.failed, { error: err.message }));
  });
}

function renderAnomalies(d) {
  var stuck = d.stuck || [];
  var orphan = d.orphan || [];
  var ghost = d.ghost || [];
  var all = stuck.length + orphan.length + ghost.length;

  var stats = $("#an-stats");
  stats.setAttribute("aria-busy", "false");
  stats.replaceChildren.apply(stats, [
    { k: T.stat.stuck, v: fmtNum(stuck.length) },
    { k: T.stat.orphan, v: fmtNum(orphan.length) },
    { k: T.stat.ghost, v: fmtNum(ghost.length) }
  ].map(function (it) {
    return buildStat({ k: it.k, v: it.v, cls: Number(it.v.replace(/,/g, "")) > 0 ? "is-alert" : "" });
  }));

  var okNote = $("#an-ok");
  okNote.hidden = all !== 0;
  okNote.textContent = all === 0 ? T.empty.anomalyOk : "";

  /* 死包 */
  var stuckRows = stuck.slice().sort(function (a, b) {
    return (String(a.repo || "") + "/" + String(a.package || ""))
      .localeCompare(String(b.repo || "") + "/" + String(b.package || ""));
  });
  renderAnomalyTable("stuck", T.card.stuck, stuckRows, [
    { label: T.col.repo, cls: "fp-col-first" },
    { label: T.col.id },
    { label: T.col.owner }
  ], function (r) {
    var tr = el("tr");
    tr.appendChild(td(T.col.repo, r.repo || null, { cls: "fp-col-first is-mono" }));
    var cell = el("td", {
      dataset: { label: T.col.id },
      class: "fp-cell-wrap fp-cell-title"
    });
    var btn = el("button", {
      class: "fp-linkbtn",
      type: "button",
      title: r.package || "",
      "aria-label": tpl(T.misc.pkgDetail, { name: r.package || "" })
    });
    btn.textContent = r.package || T.misc.dash;
    btn.addEventListener("click", function () { openDrawer(r.repo || "", r.package, btn); });
    cell.appendChild(btn);
    tr.appendChild(cell);
    tr.appendChild(td(T.col.owner, r.owner || null));
    return tr;
  }, T.empty.stuckNone, function (r) {
    return actionVisible("package.release")
      ? rowActionBtn(T.write.act.release, "danger", function () {
          startWrite("package.release", [{ repo: r.repo, id: r.package }]);
        })
      : null;
  });

  /* 孤儿租约 */
  var orphanRows = orphan.slice().sort(function (a, b) {
    return String(a.lease_id || "").localeCompare(String(b.lease_id || ""));
  });
  renderAnomalyTable("orphan", T.card.orphan, orphanRows, [
    { label: T.col.lease, cls: "fp-col-first" },
    { label: T.col.id },
    { label: T.col.device }
  ], function (r) {
    var tr = el("tr");
    tr.appendChild(td(T.col.lease, r.lease_id || null, { cls: "fp-col-first is-mono" }));
    tr.appendChild(td(T.col.id, r.package || null,
                      { cls: "fp-cell-wrap is-mono fp-cell-title" }));
    tr.appendChild(td(T.col.device, r.device_id || null, { cls: "is-mono" }));
    return tr;
  }, T.empty.orphanNone, function (r) {
    return actionVisible("lease.release")
      ? rowActionBtn(T.write.act.release, "danger", function () {
          startWrite("lease.release", [{ lease_id: r.lease_id }]);
        })
      : null;
  });

  /* 幽灵租约 */
  var ghostRows = ghost.slice().sort(function (a, b) {
    return String(a.lease_id || "").localeCompare(String(b.lease_id || ""));
  });
  renderAnomalyTable("ghost", T.card.ghost, ghostRows, [
    { label: T.col.lease, cls: "fp-col-first" },
    { label: T.col.device }
  ], function (r) {
    var tr = el("tr");
    tr.appendChild(td(T.col.lease, r.lease_id || null,
                      { cls: "fp-col-first is-mono fp-cell-title" }));
    tr.appendChild(td(T.col.device, r.device_id || null, { cls: "is-mono" }));
    return tr;
  }, T.empty.ghostNone, function (r) {
    return actionVisible("lease.release")
      ? rowActionBtn(T.write.act.release, "danger", function () {
          startWrite("lease.release", [{ lease_id: r.lease_id }]);
        })
      : null;
  });

  if (state.anFocus) {
    var card = document.getElementById("an-" + state.anFocus + "-card");
    if (card) {
      card.classList.add("is-focus");
      if (card.scrollIntoView) {
        try { card.scrollIntoView({ block: "start" }); } catch (e) { /* 忽略 */ }
      }
    }
  }
}

function renderAnomalyTable(prefix, title, rows, cols, rowFn, emptyText, actionFn) {
  var box = document.getElementById("an-" + prefix);
  var page = clampPage(state.pages[prefix], rows.length, 50);
  state.pages[prefix] = page;
  box.setAttribute("aria-busy", "false");
  document.getElementById("an-" + prefix + "-title").textContent = title;
  document.getElementById("an-" + prefix + "-hint").textContent = tpl(T.pager.simple, { n: fmtNum(rows.length) });
  var slice = slicePage(rows, page, 50);
  var useActions = writeOn() && !!actionFn;
  var allCols = cols.slice();
  if (useActions) allCols.push({ label: T.col.actions, cls: "fp-align-right" });
  if (!rows.length) {
    box.replaceChildren(emptyState(emptyText, null, null));
    setPager("an-" + prefix, { page: 1, pages: 1, total: 0 });
    return;
  }
  box.replaceChildren(buildTable(title, allCols, slice.rows, function (r) {
    var tr = rowFn(r);
    if (useActions) {
      var cell = el("td", {
        dataset: { label: T.col.actions },
        class: "fp-align-right fp-cell-actions"
      });
      var btn = actionFn(r);
      if (btn) cell.appendChild(btn);
      tr.appendChild(cell);
    }
    return tr;
  }));
  setPager("an-" + prefix, slice);
}

/* ------------------------------------------------------------------ 审计（双 Tab） */
function auditSize() {
  return (state.meta && Number(state.meta.audit_page_size)) || 25;
}

function loadAudit() {
  renderAuditTabs();
  if (state.auditTab === "writes") return loadAuditWrites();
  if (state.auditTab === "denied") return loadAuditDenied();
  return loadAuditEvents();
}

function renderAuditTabs() {
  var tab = state.auditTab;
  var isWrites = tab === "writes";
  var isDenied = tab === "denied";
  var isEvents = !isWrites && !isDenied;
  $("#au-tab-events").setAttribute("aria-pressed", isEvents ? "true" : "false");
  $("#au-tab-writes").setAttribute("aria-pressed", isWrites ? "true" : "false");
  $("#au-tab-denied").setAttribute("aria-pressed", isDenied ? "true" : "false");
  $("#au-filters").hidden = isEvents;
  $("#au-export-jsonl").hidden = !isWrites;
  $("#au-export-csv").hidden = !isWrites;
  /* 拒绝轨没有服务端关键字/动作/结果/时间范围：换成原因 + 路径，
     关键字位用等高静态说明占位，保证工具栏高度三个 Tab 完全一致（吸顶几何依赖它） */
  $("#au-f-action").hidden = isDenied;
  $("#au-f-result").hidden = isDenied;
  $("#au-f-range").hidden = isDenied;
  $("#au-f-reason").hidden = !isDenied;
  $("#au-f-path").hidden = !isDenied;
  $("#au-q-wrap").hidden = isDenied;
  $("#au-q-note").hidden = !isDenied;
  if (!isDenied) {
    $("#lbl-audit").textContent = isWrites ? T.write.audit.writesLabel : T.write.audit.eventsLabel;
    $("#f-audit").placeholder = isWrites ? T.ph.auditWrites : T.ph.audit;
  }
  if (isEvents) {
    $("#au-stale").hidden = true;
    $("#au-count").textContent = "";
  } else {
    syncAuditControls();
  }
}

function switchAuditTab(tab) {
  if (state.auditTab === tab) return;
  state.auditTab = tab;
  if (tab === "writes") {
    /* 事件 Tab 的关键字带过去当写日志关键字（同一输入框） */
    state.wq.q = state.auditQ;
    state.wq.page = 1;
  } else if (tab === "denied") {
    state.auditQ = state.wq.q;
    state.dq.page = 1;
  } else {
    state.auditQ = state.wq.q;
  }
  syncUrl();
  loadAudit();
}

function loadAuditEvents() {
  var box = $("#au-wrap");
  box.setAttribute("aria-busy", "true");
  if (!state.cache.audit) box.replaceChildren(skeletonTable(6, 5));
  return api("/api/audit").then(function (d) {
    state.cache.audit = d;
    renderAuditEvents(d);
    setLive(tpl(T.live.updated, { time: clockNow() }));
  }).catch(function (err) {
    box.setAttribute("aria-busy", "false");
    if (state.cache.audit && state.cache.audit.lines) {
      $("#au-stale").hidden = false;
      $("#au-stale").replaceChildren(errorState(err, loadAuditEvents, true));
      toast("danger", tpl(T.live.failed, { error: err.message }));
    } else {
      box.replaceChildren(errorState(err, loadAuditEvents));
    }
    setLive(tpl(T.live.failed, { error: err.message }));
  });
}

function renderAuditEvents(d) {
  var box = $("#au-wrap");
  box.setAttribute("aria-busy", "false");
  var pager = $("#au-pager");
  $("#au-stale").hidden = true;
  if (!d.enabled) {
    pager.hidden = true;
    $("#au-count").textContent = "";
    $("#au-info").textContent = "";
    box.replaceChildren(emptyState(T.empty.audit, T.empty.hintAudit, null));
    return;
  }
  pager.hidden = false;

  var rows = (d.lines || []).slice().reverse();  /* 最新在上：后端按文件顺序给出 */
  var needle = state.auditQ.trim().toLowerCase();
  var shown = needle ? rows.filter(function (r) {
    return [r.ts, r.kind, r.repo, r.pkg, r.actor, r.detail].some(function (v) {
      return v !== null && v !== undefined && String(v).toLowerCase().indexOf(needle) >= 0;
    });
  }) : rows;

  $("#au-count").textContent = tpl(T.pager.auditCount, {
    shown: fmtNum(shown.length), total: fmtNum(rows.length)
  });

  var size = auditSize();
  var page = clampPage(state.pages.audit, shown.length, size);
  state.pages.audit = page;
  var slice = slicePage(shown, page, size);

  var cols = [
    { label: T.col.time, cls: "fp-col-first fp-align-right" },
    { label: T.col.action },
    { label: T.col.repo },
    { label: T.col.id },
    { label: T.col.actor },
    { label: T.col.detail, cls: "fp-cell-wrap" }
  ];
  if (!shown.length) {
    var act = null;
    if (needle) {
      act = el("button", { class: "fp-btn fp-btn--sm", type: "button", text: T.btn.clearFilter });
      act.addEventListener("click", function () {
        state.auditQ = "";
        $("#f-audit").value = "";
        renderAuditEvents(state.cache.audit);
      });
    }
    box.replaceChildren(tableShell(T.card.audit, cols,
      [emptyRow(cols.length, emptyState(T.empty.auditNone, T.empty.hintFiltered, act))]));
    setPager("au", { page: 1, pages: 1, total: 0 });
    return;
  }
  box.replaceChildren(buildTable(T.card.audit, cols, slice.rows, function (r) {
    var tr = el("tr");
    tr.appendChild(td(T.col.time, fmtTime(r.ts), {
      cls: "fp-col-first fp-align-right is-num fp-cell-title",
      title: r.ts || ""
    }));
    tr.appendChild(td(T.col.action, r.kind || null));
    tr.appendChild(td(T.col.repo, r.repo || null, { cls: "is-mono" }));
    tr.appendChild(td(T.col.id, r.pkg || null, { cls: "is-mono" }));
    tr.appendChild(td(T.col.actor, r.actor || null));
    var detail = r.detail === null || r.detail === undefined ? null : String(r.detail);
    var shownText = detail && detail.length > 60 ? detail.slice(0, 60) + "…" : detail;
    tr.appendChild(td(T.col.detail, shownText, {
      cls: "fp-cell-wrap",
      title: detail || ""
    }));
    return tr;
  }));
  setPager("au", slice);
}

/* ---- 写操作日志 Tab ---- */
function rangeQuery(range) {
  if (!range) return "";
  var now = Math.floor(Date.now() / 1000);
  if (range === "today") {
    var d = new Date();
    d.setHours(0, 0, 0, 0);
    return String(Math.floor(d.getTime() / 1000));
  }
  if (range === "7d") return String(now - 7 * 86400);
  if (range === "30d") return String(now - 30 * 86400);
  return "";
}

function writesQuery() {
  var w = state.wq;
  var sp = new URLSearchParams();
  sp.set("limit", String(w.size));
  sp.set("offset", String((Math.max(1, w.page) - 1) * w.size));
  /* 服务端默认把"拒绝留痕"并进业务视图，而合并视图只取前 200 行再切片：
     offset>=200 会翻出空白页。这里显式只要业务行；拒绝留痕走独立 Tab
     （/api/write/denied，真正的 limit/offset 分页）。 */
  sp.set("include_denied", "0");
  if (w.action) sp.set("action", w.action);
  if (w.actor) sp.set("actor", w.actor);
  if (w.result) sp.set("result", w.result);
  if (w.q) sp.set("q", w.q);
  if (w.batch_id) sp.set("batch_id", w.batch_id);
  if (w.write_id) sp.set("write_id", w.write_id);
  sp.set("order", w.order);
  var from = rangeQuery(w.range);
  if (from) sp.set("from", from);
  return sp.toString();
}

function writesFiltered() {
  var w = state.wq;
  return !!(w.action || w.actor || w.result || w.q || w.batch_id || w.write_id || w.range);
}

function loadAuditWrites() {
  var seq = ++state.wseq;
  var box = $("#au-wrap");
  box.setAttribute("aria-busy", "true");
  if (!state.wdata) box.replaceChildren(skeletonTable(8, 5));
  return api("/api/write/history?" + writesQuery()).then(function (d) {
    if (seq !== state.wseq) return;
    state.wdata = d;
    renderAuditWrites(d);
    setLive(tpl(T.live.updated, { time: clockNow() }));
  }).catch(function (err) {
    if (seq !== state.wseq) return;
    box.setAttribute("aria-busy", "false");
    var stale = $("#au-stale");
    if (state.wdata && state.wdata.items) {
      /* 503 不清空已有数据：保留表格 + 顶部"数据可能过期" + 重试 */
      stale.hidden = false;
      stale.replaceChildren(errorState(err, loadAuditWrites, true));
      stale.appendChild(el("span", { class: "fp-badge fp-badge--danger", text: T.toast.stale }));
      toast("danger", tpl(T.live.failed, { error: err.message }));
    } else {
      stale.hidden = true;
      box.replaceChildren(errorState(err, loadAuditWrites));
    }
    setLive(tpl(T.live.failed, { error: err.message }));
  });
}

function actionLabelOf(id) {
  var spec = actionOf(id);
  return (spec && spec.label) ? spec.label : (id || T.misc.dash);
}

function targetTextOf(target) {
  if (!target || typeof target !== "object") return null;
  var kind = target.kind || "";
  var prefix = T.write.impact.obj[kind] || "";
  var body = "";
  if (kind === "package") body = (target.repo || "") + "/" + (target.id || "");
  else if (kind === "lease") body = target.lease_id || "";
  else if (kind === "device") body = target.device_id || "";
  else if (kind === "repo") body = target.repo || "";
  else if (kind === "key") body = target.name || "";
  else if (kind === "user") body = target.user_id || "";
  if (!body) return null;
  return prefix ? prefix + " " + body : body;
}

function renderAuditWrites(d) {
  var box = $("#au-wrap");
  box.setAttribute("aria-busy", "false");
  $("#au-stale").hidden = true;
  var pager = $("#au-pager");
  pager.hidden = false;
  var items = d.items || [];
  var total = Number(d.total || 0);
  var size = state.wq.size;
  var pages = Math.max(1, Math.ceil(total / size));
  var page = Math.min(Math.max(1, state.wq.page), pages);

  $("#au-count").textContent = tpl(T.pager.writesCount, {
    shown: fmtNum(items.length), total: fmtNum(total)
  });

  var cols = [
    { label: T.col.time, cls: "fp-col-first fp-align-right" },
    { label: T.col.action },
    { label: T.col.target, cls: "fp-cell-wrap" },
    { label: T.col.actor },
    { label: T.col.result },
    { label: T.col.impact, cls: "fp-align-right" },
    { label: T.col.ms, cls: "fp-align-right" },
    { label: T.col.batch, cls: "fp-cell-wrap" }
  ];

  if (!items.length) {
    var action = null;
    if (writesFiltered()) {
      action = el("button", { class: "fp-btn fp-btn--sm", type: "button", text: T.btn.clearFilter });
      action.addEventListener("click", clearWritesFilter);
    }
    var title = writesFiltered() ? T.write.audit.filtered : (d.note ? T.write.audit.notCreated
                                                                   : T.write.audit.empty);
    var hint = writesFiltered() ? T.empty.hintFiltered : T.write.audit.emptyHint;
    box.replaceChildren(tableShell(T.card.writeLog, cols, [emptyRow(cols.length, emptyState(title, hint, action))]));
    setPager("au", { page: 1, pages: 1, total: 0 });
    return;
  }

  box.replaceChildren(buildTable(T.card.writeLog, cols, items, function (rec) {
    var tr = el("tr", { dataset: { write: rec.write_id || "" } });
    if (state.wq.write_id && rec.write_id === state.wq.write_id) {
      tr.classList.add("is-focus");
    }
    tr.appendChild(td(T.col.time, fmtTime(rec.ts), {
      cls: "fp-col-first fp-align-right is-num fp-cell-title",
      title: rec.ts || ""
    }));
    var actCell = el("td", { dataset: { label: T.col.action } });
    actCell.appendChild(el("span", { text: actionLabelOf(rec.action) }));
    actCell.appendChild(el("span", { class: "fp-write__id", text: rec.action || "" }));
    tr.appendChild(actCell);
    tr.appendChild(td(T.col.target, targetTextOf(rec.target), {
      cls: "fp-cell-wrap is-mono", title: JSON.stringify(rec.target || {})
    }));
    tr.appendChild(td(T.col.actor, rec.actor || null));
    var resCell = el("td", { dataset: { label: T.col.result }, class: "fp-cell-result" });
    resCell.appendChild(resultBadge(rec.result === "ok" ? "ok" : "error"));
    if (rec.result !== "ok") {
      var codeText = rec.code === null || rec.code === undefined ? "" : String(rec.code);
      resCell.appendChild(el("span", { class: "fp-write__code", text: codeText }));
    }
    tr.appendChild(resCell);
    tr.appendChild(td(T.col.impact, fmtNum(rec.impact_count), { cls: "fp-align-right is-num" }));
    tr.appendChild(td(T.col.ms, rec.ms === null || rec.ms === undefined ? null : fmtNum(rec.ms),
                      { cls: "fp-align-right is-num" }));
    tr.appendChild(batchCell(rec));
    return tr;
  }));

  $("#au-info").textContent = pages > 1
    ? tpl(T.pager.writesPage, { p: page, pages: pages, n: fmtNum(total) })
    : (total ? tpl(T.pager.simple, { n: fmtNum(total) }) : T.pager.empty);
  $("#au-prev").disabled = page <= 1;
  $("#au-next").disabled = page >= pages;

  if (state.wq.write_id) {
    var node = box.querySelector('tr[data-write="' + cssEscape(state.wq.write_id) + '"]');
    if (node && node.scrollIntoView) {
      try { node.scrollIntoView({ block: "center" }); } catch (e) { /* 忽略 */ }
    }
  }
}

/* 批次/令牌 + 完整 JSON（<details>，只读） */
function batchCell(rec) {
  var cell = el("td", { dataset: { label: T.col.batch }, class: "fp-cell-wrap" });
  var det = el("details", { class: "fp-write__detail" });
  var sum = el("summary", { title: T.write.audit.detailHint });
  sum.appendChild(el("span", { class: "fp-write__code", text: rec.batch_id || T.misc.dash }));
  sum.appendChild(document.createTextNode(" / "));
  sum.appendChild(el("span", { class: "fp-write__code", text: rec.confirm_token_id || T.misc.dash }));
  det.appendChild(sum);
  det.appendChild(el("pre", { class: "fp-write__json", text: JSON.stringify(rec, null, 2) }));
  cell.appendChild(det);
  return cell;
}

function clearAuditFilter() {
  if (state.auditTab === "denied") {
    var d = state.dq;
    d.actor = "";
    d.reason = "";
    d.path = "";
    d.write_id = "";
    d.page = 1;
    var reasonInput = $("#f-au-reason");
    if (reasonInput) reasonInput.value = "";
    var pathSel = $("#f-au-path");
    if (pathSel) pathSel.value = "";
    syncAuditControls();
    syncUrl();
    loadAuditDenied();
    return;
  }
  clearWritesFilter();
}

function clearWritesFilter() {
  var w = state.wq;
  w.action = "";
  w.actor = "";
  w.result = "";
  w.q = "";
  w.batch_id = "";
  w.write_id = "";
  w.range = "";
  w.page = 1;
  state.auditQ = "";
  var input = $("#f-audit");
  if (input) input.value = "";
  syncAuditControls();
  syncUrl();
  loadAuditWrites();
}

function deniedQuery() {
  var d = state.dq;
  var sp = new URLSearchParams();
  sp.set("limit", String(d.size));
  sp.set("offset", String((Math.max(1, d.page) - 1) * d.size));
  if (d.actor) sp.set("actor", d.actor);
  if (d.reason) sp.set("reason", d.reason);
  if (d.path) sp.set("path", d.path);
  sp.set("order", "desc");
  return sp.toString();
}

function deniedFiltered() {
  var d = state.dq;
  return !!(d.actor || d.reason || d.path);
}

/* 拒绝留痕：面板自己拒绝的写请求（400/403/413/415/429），append-only，只读 */
function loadAuditDenied() {
  var seq = ++state.dseq;
  var box = $("#au-wrap");
  box.setAttribute("aria-busy", "true");
  if (!state.ddata) box.replaceChildren(skeletonTable(7, 5));
  return api("/api/write/denied?" + deniedQuery()).then(function (d) {
    if (seq !== state.dseq) return;
    state.ddata = d;
    renderAuditDenied(d);
    setLive(tpl(T.live.updated, { time: clockNow() }));
  }).catch(function (err) {
    if (seq !== state.dseq) return;
    box.setAttribute("aria-busy", "false");
    var stale = $("#au-stale");
    if (state.ddata && state.ddata.items) {
      stale.hidden = false;
      stale.replaceChildren(errorState(err, loadAuditDenied, true));
      stale.appendChild(el("span", { class: "fp-badge fp-badge--danger", text: T.toast.stale }));
      toast("danger", tpl(T.live.failed, { error: err.message }));
    } else {
      stale.hidden = true;
      box.replaceChildren(errorState(err, loadAuditDenied));
    }
    setLive(tpl(T.live.failed, { error: err.message }));
  });
}

function deniedRow(rec) {
  var tr = el("tr", { "data-write": String(rec.write_id || "") });
  var ts = num(rec.ts_epoch, 0);
  tr.appendChild(td(T.write.audit.deniedCol.time, rec.ts || null, {
    cls: "fp-col-first fp-align-right",
    title: ts ? new Date(ts * 1000).toLocaleString() : ""
  }));
  var reasonCell = el("td", { dataset: { label: T.write.audit.deniedCol.reason } });
  reasonCell.appendChild(el("span", {
    class: "fp-badge fp-badge--warning", text: String(rec.reason || T.misc.dash)
  }));
  tr.appendChild(reasonCell);
  tr.appendChild(td(T.write.audit.deniedCol.path, rec.path || null,
                    { cls: "fp-cell-wrap is-mono" }));
  tr.appendChild(td(T.write.audit.deniedCol.actor, rec.actor || null));
  tr.appendChild(td(T.write.audit.deniedCol.ip, rec.remote_ip || null, { cls: "is-mono" }));
  var codeCell = el("td", { dataset: { label: T.write.audit.deniedCol.code } });
  codeCell.appendChild(el("span", {
    class: "fp-write__code",
    text: rec.code === null || rec.code === undefined ? T.misc.dash : String(rec.code)
  }));
  if (rec.error) {
    codeCell.appendChild(el("span", { class: "fp-write__reason", text: " " + String(rec.error) }));
  }
  tr.appendChild(codeCell);
  var detailCell = el("td", {
    dataset: { label: T.write.audit.deniedCol.detail }, class: "fp-cell-wrap"
  });
  var det = el("details", { class: "fp-write__detail" });
  det.appendChild(el("summary", { title: T.write.audit.deniedTitle,
                                  text: T.write.audit.deniedDetail }));
  det.appendChild(el("pre", { class: "fp-write__json",
                              text: JSON.stringify(rec, null, 2) }));
  detailCell.appendChild(det);
  tr.appendChild(detailCell);
  return tr;
}

function renderAuditDenied(d) {
  var box = $("#au-wrap");
  box.setAttribute("aria-busy", "false");
  $("#au-stale").hidden = true;
  var pager = $("#au-pager");
  pager.hidden = false;
  var items = d.items || [];
  var total = Number(d.total || 0);
  var size = state.dq.size;
  var pages = Math.max(1, Math.ceil(total / size));
  state.dq.page = Math.min(Math.max(1, state.dq.page), pages);
  setPager("au", { page: state.dq.page, pages: pages, total: total });
  $("#au-count").textContent = total ? tpl(T.write.audit.deniedCount, { n: fmtNum(total) }) : "";

  if (!items.length) {
    var filtered = deniedFiltered();
    box.replaceChildren(tableShell(T.write.audit.deniedTitle, 7,
      emptyRow(7, emptyState(
        filtered ? T.write.audit.deniedFiltered : T.write.audit.deniedEmpty,
        filtered ? "" : T.write.audit.deniedEmptyHint
      ))));
    return;
  }
  var cols = [
    { label: T.write.audit.deniedCol.time, nowrap: true },
    { label: T.write.audit.deniedCol.reason, nowrap: true },
    { label: T.write.audit.deniedCol.path, nowrap: true },
    { label: T.write.audit.deniedCol.actor, nowrap: true },
    { label: T.write.audit.deniedCol.ip, nowrap: true },
    { label: T.write.audit.deniedCol.code },
    { label: T.write.audit.deniedCol.detail }
  ];
  box.replaceChildren(buildTable(T.write.audit.deniedTitle, cols, items, deniedRow));
  if (state.dq.write_id) {
    var node = box.querySelector('tr[data-write="' + cssEscape(state.dq.write_id) + '"]');
    if (node) {
      node.classList.add("is-focus");
      if (node.scrollIntoView) {
        try { node.scrollIntoView({ block: "center" }); } catch (e) { /* 忽略 */ }
      }
    }
  }
}

function syncAuditControls() {
  var w = state.wq;
  var d = state.dq;
  var isDenied = state.auditTab === "denied";
  var action = $("#f-au-action");
  var actor = $("#f-au-actor");
  var result = $("#f-au-result");
  var range = $("#f-au-range");
  var size = $("#f-au-size");
  var reason = $("#f-au-reason");
  var path = $("#f-au-path");
  if (isDenied) {
    if (reason && reason.value !== d.reason) reason.value = d.reason;
    if (path && path.value !== d.path) path.value = d.path;
    if (actor && actor.value !== d.actor) actor.value = d.actor;
    if (size && size.value !== String(d.size)) size.value = String(d.size);
    return;
  }
  if (action && action.value !== w.action) action.value = w.action;
  if (actor && actor.value !== w.actor) actor.value = w.actor;
  if (result && result.value !== w.result) result.value = w.result;
  if (range && range.value !== w.range) range.value = w.range;
  if (size && size.value !== String(w.size)) size.value = String(w.size);
  var input = $("#f-audit");
  var want = state.auditTab === "writes" ? w.q : state.auditQ;
  if (input && input.value !== want) input.value = want;
}

function exportWrites(fmt) {
  var url = "/api/write/export?" + writesQuery() + "&format=" + fmt;
  var btn = fmt === "csv" ? $("#au-export-csv") : $("#au-export-jsonl");
  if (btn) btn.disabled = true;
  fetch(url, { credentials: "same-origin", cache: "no-store" })
    .then(function (resp) {
      var ctype = resp.headers.get("content-type") || "";
      if (!resp.ok || ctype.indexOf("application/json") === 0) {
        return resp.json().catch(function () { return null; }).then(function (data) {
          var msg = (data && data.error) ? String(data.error)
                                         : tpl(T.err.http, { code: resp.status });
          throw new ApiError(msg, resp.status, url);
        });
      }
      var name = filenameOf(resp.headers.get("Content-Disposition")) ||
                 ("fox-writes." + fmt);
      var rows = Number(resp.headers.get("X-Total-Rows") || 0);
      var truncated = resp.headers.get("X-Truncated") === "1";
      return resp.blob().then(function (blob) {
        downloadBlob(blob, name);
        toast("success", tpl(T.toast.exported, { n: fmtNum(rows) }));
        if (truncated) toast("info", tpl(T.toast.truncated, { n: fmtNum(rows) }));
      });
    })
    .catch(function (err) {
      toast("danger", err && err.message ? String(err.message) : T.err.upstream);
    })
    .then(function () { if (btn) btn.disabled = false; });
}

/* ------------------------------------------------------------------ 包列表 CSV 导出 */
function exportCsv() {
  var total = state.pkgData && state.pkgData.page ? Number(state.pkgData.page.total || 0) : 0;
  if (total > 5000 && !window.confirm(tpl(T.toast.exportConfirm, { n: fmtNum(total) }))) return;
  var btn = $("#pkg-export");
  btn.disabled = true;
  var url = "/api/export.csv?" + packagesQuery(false);
  fetch(url, { credentials: "same-origin", cache: "no-store" })
    .then(function (resp) {
      var ctype = resp.headers.get("content-type") || "";
      if (!resp.ok || ctype.indexOf("application/json") === 0) {
        return resp.json().catch(function () { return null; }).then(function (data) {
          var msg = (data && data.error) ? String(data.error)
                                         : tpl(T.err.http, { code: resp.status });
          throw new ApiError(msg, resp.status, url);
        });
      }
      var name = filenameOf(resp.headers.get("Content-Disposition")) || "fox-packages.csv";
      var rows = Number(resp.headers.get("X-Total-Rows") || 0);
      var truncated = resp.headers.get("X-Truncated") === "1";
      return resp.blob().then(function (blob) {
        downloadBlob(blob, name);
        toast("success", tpl(T.toast.exported, { n: fmtNum(rows) }));
        if (truncated) toast("info", tpl(T.toast.truncated, { n: fmtNum(rows) }));
        setLive(tpl(T.live.exported, { n: fmtNum(rows) }));
      });
    })
    .catch(function (err) {
      toast("danger", err && err.message ? String(err.message) : T.err.upstream);
    })
    .then(function () { btn.disabled = false; });
}

function filenameOf(disposition) {
  if (!disposition) return null;
  var m = /filename="?([^";]+)"?/i.exec(disposition);
  return m ? m[1] : null;
}

function downloadBlob(blob, name) {
  var objUrl = URL.createObjectURL(blob);
  var a = el("a", { href: objUrl, download: name, class: "fp-aux" });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function () { URL.revokeObjectURL(objUrl); }, 2000);
}

/* ------------------------------------------------------------------ 背景 inert 与复制 */
function setBackgroundInert(on) {
  [".fp-app", "#tabbar"].forEach(function (sel) {
    var node = $(sel);
    if (!node) return;
    if (on) node.setAttribute("inert", "");
    else node.removeAttribute("inert");
  });
}

function selectNodeText(node) {
  if (!node) return false;
  try {
    var range = document.createRange();
    range.selectNodeContents(node);
    var sel = window.getSelection();
    if (!sel) return false;
    sel.removeAllRanges();
    sel.addRange(range);
    return true;
  } catch (e) { return false; }
}

function copyText(text, okMessage, fallbackNode) {
  if (!text) { toast("info", T.err.shaMissing); return; }
  var done = function () { toast("success", okMessage || T.toast.copied); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, function () {
      if (selectNodeText(fallbackNode)) toast("info", T.err.copyFailed);
      else toast("danger", T.err.copyFailed);
    });
    return;
  }
  if (selectNodeText(fallbackNode)) toast("info", T.err.copyFailed);
  else toast("danger", T.err.copyUnavailable);
}

/* ------------------------------------------------------------------ 抽屉（包详情） */
function openDrawer(repo, id, trigger) {
  if (!id) return;
  drawerState.open = true;
  drawerState.repo = repo || "";
  drawerState.id = id;
  drawerState.trigger = trigger || document.activeElement;
  $("#drawer-title").textContent = id;
  $("#drawer-ft").hidden = true;
  renderDrawerSkeleton();
  var scrim = $("#scrim");
  var drawer = $("#drawer");
  scrim.hidden = false;
  drawer.hidden = false;
  window.requestAnimationFrame(function () {
    drawer.classList.add("is-open");
    scrim.classList.add("is-open");
  });
  document.body.classList.add("fp-noscroll");
  setBackgroundInert(true);
  var close = $("#drawer-close");
  if (close) close.focus();
  loadPackage(repo, id);
}

function closeDrawer() {
  if (!drawerState.open) return;
  drawerState.open = false;
  drawerState.seq++;
  var drawer = $("#drawer");
  var scrim = $("#scrim");
  drawer.classList.remove("is-open");
  scrim.classList.remove("is-open");
  var done = false;
  var finish = function () {
    if (done || drawerState.open) return;
    done = true;
    scrim.hidden = true;
    drawer.hidden = true;
  };
  setTimeout(finish, 260);
  document.body.classList.remove("fp-noscroll");
  setBackgroundInert(false);
  var trigger = drawerState.trigger;
  if (trigger && trigger.focus) trigger.focus();
}

function renderDrawerSkeleton() {
  var body = $("#drawer-bd");
  var box = el("div", { "aria-busy": "true" });
  for (var i = 0; i < 6; i++) {
    box.appendChild(el("div", { class: "fp-dl" }, [
      el("span", { class: "fp-skeleton fp-skeleton--text fp-w-40" }),
      el("span", { class: "fp-skeleton fp-skeleton--text fp-w-75" })
    ]));
  }
  body.replaceChildren(box);
}

function loadPackage(repo, id) {
  var seq = ++drawerState.seq;
  var sp = new URLSearchParams();
  if (repo) sp.set("repo", repo);
  sp.set("id", id);
  return api("/api/package?" + sp.toString()).then(function (d) {
    if (seq !== drawerState.seq || !drawerState.open) return;
    renderDrawerPackage(d.package || {});
  }).catch(function (err) {
    if (seq !== drawerState.seq || !drawerState.open) return;
    var body = $("#drawer-bd");
    if (err.status === 404) {
      body.replaceChildren(emptyState(T.empty.pkg404, T.empty.hint404, null));
      $("#drawer-ft").hidden = true;
    } else {
      body.replaceChildren(errorState(err, function () { loadPackage(repo, id); }));
    }
  });
}

function renderDrawerPackage(pkg) {
  var body = $("#drawer-bd");
  $("#drawer-title").textContent = pkg.id || T.misc.na;
  var dl = el("dl", { class: "fp-dl" });

  function row(label, value, cls) {
    dl.appendChild(el("dt", { text: label }));
    var dd = el("dd", cls ? { class: cls } : null);
    if (value === null || value === undefined || value === "") {
      dd.textContent = T.misc.dash;
      dd.classList.add("is-muted");
    } else if (value instanceof Node) {
      dd.appendChild(value);
    } else {
      dd.textContent = String(value);
    }
    dl.appendChild(dd);
    return dd;
  }

  row(T.drawer.f.repo, pkg.repo, "is-mono");
  row(T.drawer.f.status, statusBadge(pkg.status, pkg.status_label));
  row(T.drawer.f.size, fmtBytes(pkg.size) || T.misc.dash, "is-num");
  var shaDd = row(T.drawer.f.sha, pkg.sha256, "is-mono");
  row(T.drawer.f.owner, pkg.owner);
  row(T.drawer.f.claimedAt, fmtTime(pkg.claimed_at), "is-num");
  row(T.drawer.f.completedAt, fmtTime(pkg.completed_at), "is-num");

  var downDd = el("dd", { class: "is-num" });
  if (pkg.downloaded_at) {
    downDd.appendChild(el("span", { text: fmtTime(pkg.downloaded_at) || T.misc.dash }));
    downDd.appendChild(document.createTextNode(" "));
    downDd.appendChild(downloadBadge(pkg.downloaded_at));
  } else {
    downDd.textContent = T.misc.dash;
    downDd.classList.add("is-muted");
  }
  dl.appendChild(el("dt", { text: T.drawer.f.downloadedAt }));
  dl.appendChild(downDd);

  row(T.drawer.f.updatedAt, fmtTime(pkg.updated_at), "is-num");
  row(T.drawer.f.note, pkg.note, "is-pre");

  dl.appendChild(el("div", { class: "fp-dl__sep" }));
  dl.appendChild(el("div", { class: "fp-dl__hd", text: T.drawer.lease }));
  var lease = pkg.lease;
  if (lease) {
    row(T.drawer.f.kind, leaseBadge(lease.kind));
    row(T.drawer.f.device, lease.device_id, "is-mono");
    row(T.drawer.f.user, lease.user_id);
    var ttlDd = el("dd", { class: "is-num" });
    var dOb = overdueBadge(lease);
    if (dOb) ttlDd.appendChild(dOb);
    ttlDd.appendChild(heldNode(lease));
    dl.appendChild(el("dt", { text: T.drawer.f.ttl }));
    dl.appendChild(ttlDd);
    row(T.drawer.f.progress, progressCell(lease.progress));
    row(T.drawer.f.beat, fmtTime(lease.last_beat_at), "is-num");
  } else {
    row(T.drawer.f.kind, T.drawer.noLease);
  }
  body.replaceChildren(dl);

  var ft = $("#drawer-ft");
  ft.replaceChildren();
  var copyBtn = el("button", { class: "fp-btn fp-btn--sm", type: "button" });
  copyBtn.appendChild(icon("copy", 16));
  copyBtn.appendChild(el("span", { text: T.btn.copySha }));
  copyBtn.disabled = !pkg.sha256;
  copyBtn.addEventListener("click", function () {
    copyText(pkg.sha256, T.toast.copiedSha, shaDd);
  });
  ft.appendChild(copyBtn);

  if (writeOn() && pkg.repo && pkg.id) {
    var target = [{ repo: pkg.repo, id: pkg.id }];
    if (pkg.status === "completed" && actionVisible("package.reopen")) {
      var reopen = el("button", {
        class: "fp-btn fp-btn--sm fp-btn--danger fp-write-action", type: "button"
      });
      reopen.appendChild(icon("undo", 16));
      reopen.appendChild(el("span", { text: T.write.act.reopen }));
      reopen.addEventListener("click", function () { startWrite("package.reopen", target); });
      ft.appendChild(reopen);
    } else if (pkg.status === "claimed" && actionVisible("package.release")) {
      var releaseBtn = el("button", {
        class: "fp-btn fp-btn--sm fp-btn--danger fp-write-action", type: "button"
      });
      releaseBtn.appendChild(icon("bolt", 16));
      releaseBtn.appendChild(el("span", { text: T.write.act.release }));
      releaseBtn.addEventListener("click", function () {
        startWrite("package.release", target);
      });
      ft.appendChild(releaseBtn);
    }
  }
  ft.hidden = false;
}

/* ------------------------------------------------------------------ 设置抽屉（仓库 / 密钥） */
function ensureSettings() {
  if (document.getElementById("settings")) return;
  var scrim = el("div", { class: "fp-scrim", id: "settings-scrim", hidden: true });
  var drawer = el("aside", {
    class: "fp-drawer fp-settings fp-write-action",
    id: "settings", role: "dialog", "aria-modal": "true",
    "aria-labelledby": "settings-title", hidden: true
  });
  var hd = el("header", { class: "fp-drawer__hd" });
  hd.appendChild(el("h2", { id: "settings-title", text: T.write.settings.title }));
  var close = el("button", {
    class: "fp-iconbtn", id: "settings-close", type: "button",
    "aria-label": T.write.settings.close
  });
  close.appendChild(icon("close", 20));
  hd.appendChild(close);
  drawer.appendChild(hd);

  var bd = el("div", { class: "fp-drawer__bd" });
  var repoCard = el("section", { class: "fp-card" });
  var repoHd = el("div", { class: "fp-card__hd" });
  repoHd.appendChild(el("h2", { text: T.card.settingsRepos }));
  repoHd.appendChild(el("span", { class: "fp-card__hint", text: T.write.settings.reposHint }));
  repoCard.appendChild(repoHd);
  repoCard.appendChild(el("div", { class: "fp-tablewrap", id: "settings-repos",
                                   "aria-busy": "false" }));
  bd.appendChild(repoCard);

  var keyCard = el("section", { class: "fp-card" });
  var keyHd = el("div", { class: "fp-card__hd" });
  keyHd.appendChild(el("h2", { text: T.card.settingsKeys }));
  keyHd.appendChild(el("span", { class: "fp-card__hint", text: T.write.settings.keysHint }));
  keyCard.appendChild(keyHd);
  keyCard.appendChild(el("div", { class: "fp-tablewrap", id: "settings-keys",
                                  "aria-busy": "false" }));
  bd.appendChild(keyCard);
  drawer.appendChild(bd);

  var ft = el("footer", { class: "fp-drawer__ft" });
  var done = el("button", { class: "fp-btn fp-btn--sm", type: "button" });
  done.appendChild(el("span", { text: T.write.settings.close }));
  done.addEventListener("click", closeSettings);
  ft.appendChild(done);
  drawer.appendChild(ft);

  scrim.appendChild(document.createComment("settings-scrim"));
  document.body.appendChild(scrim);
  document.body.appendChild(drawer);
  $("#settings-close").addEventListener("click", closeSettings);
  scrim.addEventListener("click", closeSettings);
}

function openSettings(trigger) {
  ensureSettings();
  state.settingsOpen = true;
  state.settingsTrigger = trigger || document.activeElement;
  $("#settings-scrim").hidden = false;
  $("#settings").hidden = false;
  window.requestAnimationFrame(function () {
    $("#settings").classList.add("is-open");
    $("#settings-scrim").classList.add("is-open");
  });
  document.body.classList.add("fp-noscroll");
  setBackgroundInert(true);
  var close = $("#settings-close");
  if (close) close.focus();
  loadSettingsRepos();
  if (actionVisible("key.revoke")) loadSettingsKeys();
}

function closeSettings() {
  if (!state.settingsOpen) return;
  state.settingsOpen = false;
  var drawer = $("#settings");
  var scrim = $("#settings-scrim");
  if (!drawer || !scrim) return;
  drawer.classList.remove("is-open");
  scrim.classList.remove("is-open");
  setTimeout(function () {
    if (state.settingsOpen) return;
    drawer.hidden = true;
    scrim.hidden = true;
  }, 260);
  document.body.classList.remove("fp-noscroll");
  setBackgroundInert(false);
  var trigger = state.settingsTrigger;
  if (trigger && trigger.focus) trigger.focus();
}

function loadSettingsRepos() {
  var box = $("#settings-repos");
  if (!box) return;
  box.setAttribute("aria-busy", "true");
  box.replaceChildren(skeletonTable(6, 3));
  api("/api/repos").then(function (d) {
    renderSettingsRepos(d);
  }).catch(function (err) {
    box.setAttribute("aria-busy", "false");
    box.replaceChildren(errorState(err, loadSettingsRepos, true));
  });
}

function renderSettingsRepos(d) {
  var box = $("#settings-repos");
  if (!box) return;
  box.setAttribute("aria-busy", "false");
  var rows = d.repos || [];
  if (!rows.length) {
    box.replaceChildren(emptyState(T.write.settings.empty, T.empty.hintNone, null));
    return;
  }
  var cols = [
    { label: T.col.repo, cls: "fp-col-first" },
    { label: T.col.type },
    { label: T.col.enabled },
    { label: T.col.packages, cls: "fp-align-right" },
    { label: T.col.claimedCount, cls: "fp-align-right" },
    { label: T.col.openLeases, cls: "fp-align-right" },
    { label: T.col.actions, cls: "fp-align-right" }
  ];
  box.replaceChildren(buildTable(T.card.settingsRepos, cols, rows, function (r) {
    var tr = el("tr");
    tr.appendChild(td(T.col.repo, r.repo || null, { cls: "fp-col-first is-mono" }));
    tr.appendChild(td(T.col.type, r.repo_type || null));
    var cell = el("td", { dataset: { label: T.col.enabled } });
    var badge = el("span", {
      class: "fp-badge " + (r.enabled ? "fp-badge--completed" : "fp-badge--available")
    });
    badge.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
    badge.appendChild(el("span", { text: r.enabled ? T.misc.enabled : T.misc.disabled }));
    cell.appendChild(badge);
    tr.appendChild(cell);
    tr.appendChild(td(T.col.packages, fmtNum(r.packages), { cls: "fp-align-right is-num" }));
    tr.appendChild(td(T.col.claimedCount, fmtNum(r.claimed), { cls: "fp-align-right is-num" }));
    tr.appendChild(td(T.col.openLeases, fmtNum(r.open_leases), { cls: "fp-align-right is-num" }));
    var act = el("td", { dataset: { label: T.col.actions }, class: "fp-align-right fp-cell-actions" });
    var id = r.enabled ? "repo.disable" : "repo.enable";
    if (actionVisible(id)) {
      act.appendChild(rowActionBtn(
        r.enabled ? T.write.act.disable : T.write.act.enable,
        r.enabled ? "danger" : "",
        function () { startWrite(id, [{ repo: r.repo }]); }
      ));
    }
    tr.appendChild(act);
    return tr;
  }));
}

function loadSettingsKeys() {
  var box = $("#settings-keys");
  if (!box) return;
  box.setAttribute("aria-busy", "true");
  box.replaceChildren(skeletonTable(5, 3));
  api("/api/keys").then(function (d) {
    renderSettingsKeys(d);
  }).catch(function (err) {
    box.setAttribute("aria-busy", "false");
    box.replaceChildren(errorState(err, loadSettingsKeys, true));
  });
}

function renderSettingsKeys(d) {
  var box = $("#settings-keys");
  if (!box) return;
  box.setAttribute("aria-busy", "false");
  var rows = d.keys || [];
  if (!rows.length) {
    box.replaceChildren(emptyState(T.write.settings.noKeys, T.empty.hintNone, null));
    return;
  }
  var cols = [
    { label: T.col.action, cls: "fp-col-first" },
    { label: T.col.role },
    { label: T.col.status },
    { label: T.col.grants, cls: "fp-cell-wrap" },
    { label: T.col.lastSeenShort },
    { label: T.col.actions, cls: "fp-align-right" }
  ];
  box.replaceChildren(buildTable(T.card.settingsKeys, cols, rows, function (r) {
    var tr = el("tr");
    tr.appendChild(td(T.col.action, r.name || null, { cls: "fp-col-first is-mono" }));
    tr.appendChild(td(T.col.role, r.role || null));
    var cell = el("td", { dataset: { label: T.col.status } });
    var badge = el("span", {
      class: "fp-badge " + (r.revoked ? "fp-badge--danger" : "fp-badge--completed")
    });
    badge.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
    badge.appendChild(el("span", {
      text: r.revoked ? T.write.settings.revoked : T.misc.enabled
    }));
    cell.appendChild(badge);
    tr.appendChild(cell);
    tr.appendChild(td(T.col.grants, (r.grants && r.grants.length) ? r.grants.join(", ") : null,
                      { cls: "fp-cell-wrap is-mono" }));
    tr.appendChild(td(T.col.lastSeenShort, fmtTime(r.last_seen), { cls: "fp-align-right is-num" }));
    var act = el("td", { dataset: { label: T.col.actions }, class: "fp-align-right fp-cell-actions" });
    if (actionVisible("key.revoke")) {
      var btn = rowActionBtn(T.write.settings.revoke, "danger", function () {
        startWrite("key.revoke", [{ name: r.name }]);
      }, r.is_last_admin ? T.write.settings.revokeLast :
         (r.revoked ? T.write.settings.revoked : ""));
      btn.disabled = !!r.revoked || !!r.is_last_admin;
      act.appendChild(btn);
    }
    tr.appendChild(act);
    return tr;
  }));
}

/* ------------------------------------------------------------------ 写弹窗（严格二次确认） */
var wm = {
  open: false,
  phase: "idle",        /* idle | previewing | confirming | submitting | error | done */
  action: "",
  targets: [],
  params: {},
  preview: null,
  impact: null,
  token: "",
  tokenId: "",
  idem: "",
  trigger: null,
  retried: 0,
  diff: null,
  banner: "",
  bannerKind: "",
  errorMessage: "",
  reuseIdem: false
};

function ensureWriteModal() {
  if (document.getElementById("wm-wrap")) return;
  var scrim = el("div", { class: "fp-scrim", id: "wm-scrim", hidden: true });
  var wrap = el("div", { class: "fp-modalwrap", id: "wm-wrap", hidden: true });
  var modal = el("div", {
    class: "fp-modal", id: "write-modal", role: "alertdialog",
    "aria-modal": "true", "aria-labelledby": "wm-title", "aria-describedby": "wm-desc"
  });
  var hd = el("header", { class: "fp-modal__hd" });
  hd.appendChild(el("h2", { id: "wm-title" }));
  var close = el("button", {
    class: "fp-iconbtn", id: "wm-close", type: "button",
    "aria-label": T.write.close
  });
  close.appendChild(icon("close", 20));
  hd.appendChild(close);
  modal.appendChild(hd);
  modal.appendChild(el("div", { class: "fp-modal__bd", id: "wm-bd" }));
  var ft = el("footer", { class: "fp-modal__ft" });
  var cancel = el("button", { class: "fp-btn", id: "wm-cancel", type: "button" });
  cancel.appendChild(el("span", { text: T.write.cancel }));
  ft.appendChild(cancel);
  var submit = el("button", {
    class: "fp-btn fp-btn--danger fp-write-action", id: "wm-submit",
    type: "button", disabled: true
  });
  ft.appendChild(submit);
  modal.appendChild(ft);
  wrap.appendChild(modal);
  document.body.appendChild(scrim);
  document.body.appendChild(wrap);

  $("#wm-close").addEventListener("click", function () { closeWrite(false); });
  $("#wm-cancel").addEventListener("click", function () { closeWrite(false); });
  $("#wm-submit").addEventListener("click", function () { submitWrite(false); });
  wrap.addEventListener("click", function (e) {
    if (e.target === wrap) closeWrite(false);
  });
}

function showWriteModal() {
  var wrap = $("#wm-wrap");
  var scrim = $("#wm-scrim");
  if (!wrap || !scrim) return;
  scrim.hidden = false;
  scrim.classList.add("is-open");
  wrap.hidden = false;
  document.body.classList.add("fp-noscroll");
  setBackgroundInert(true);
  if (notifOpen()) closeNotif();
  window.requestAnimationFrame(function () {
    var modal = $("#write-modal");
    if (modal) modal.classList.add("is-open");
  });
}

function closeWrite(instant) {
  if (!wm.open) return;
  wm.open = false;
  wm.phase = "idle";
  var wrap = $("#wm-wrap");
  var scrim = $("#wm-scrim");
  var modal = $("#write-modal");
  if (modal) modal.classList.remove("is-open");
  if (scrim) scrim.classList.remove("is-open");
  var finish = function () {
    if (wm.open) return;
    if (wrap) wrap.hidden = true;
    if (scrim) scrim.hidden = true;
  };
  if (instant) finish(); else setTimeout(finish, 200);
  document.body.classList.remove("fp-noscroll");
  setBackgroundInert(false);
  var trigger = wm.trigger;
  wm.trigger = null;
  if (trigger && trigger.focus && trigger.isConnected !== false) {
    try { trigger.focus(); } catch (e) { /* 忽略 */ }
  }
}

function startWrite(action, targets, opts) {
  opts = opts || {};
  if (!writeOn()) { toast("info", T.write.readonly); return; }
  var spec = actionOf(action);
  if (!spec || !actionVisible(action)) {
    toast("danger", T.write.act.missing);
    return;
  }
  var list = (targets || []).filter(Boolean);
  if (!list.length) return;
  var max = writeMax();
  if (list.length > max) {
    toast("danger", tpl(T.write.bulk.tooMany, { n: max }));
    return;
  }
  if (wm.open) closeWrite(true);
  wm.open = true;
  wm.phase = "previewing";
  wm.action = action;
  wm.targets = list;
  wm.params = opts.params || defaultParams(action);
  wm.preview = null;
  wm.impact = null;
  wm.token = "";
  wm.tokenId = "";
  wm.idem = newIdemKey();
  wm.trigger = opts.trigger || document.activeElement;
  wm.retried = 0;
  wm.diff = null;
  wm.banner = "";
  wm.bannerKind = "";
  wm.errorMessage = "";
  wm.reuseIdem = false;
  wm._name = "";
  wm._count = "";
  wm._ack = false;
  wm.paramsJson = "";
  ensureWriteModal();
  showWriteModal();
  previewNow();
}

function defaultParams(action) {
  if (action === "package.reopen") return { to: "claimed" };
  return {};
}

function writeErrorText(res) {
  var body = res && res.body ? res.body : {};
  if (body.error) return String(body.error);
  if (res && res.status) return tpl(T.err.http, { code: res.status });
  return T.err.network;
}

function previewNow() {
  if (!wm.open) return;
  wm.phase = "previewing";
  renderWriteModal();
  /* 快照：preview 发出去的就是这一份，execute 逐字复用（服务端把 params 绑进 token） */
  var paramsJson = JSON.stringify(wm.params || {});
  var payload = { action: wm.action, targets: wm.targets, params: JSON.parse(paramsJson) };
  wm.paramsJson = "";
  return writeRequest("/api/write/preview", payload).then(function (res) {
    if (!wm.open) return;
    if (res.status === 200 && res.body && res.body.preview) {
      var prev = res.body.preview;
      wm.preview = prev;
      wm.impact = prev.impact || { mutations: 0, counts: {}, items: [] };
      wm.token = prev.confirm_token || "";
      wm.tokenId = prev.confirm_token_id || "";
      /* 以服务端回显的 params 为准（它已被 _norm_params 规范化并绑定进 token）：
         执行时原样送回去，避免"前端默认值"与"服务端默认值"打架。 */
      wm.params = (prev.params && typeof prev.params === "object") ? prev.params : wm.params;
      wm.paramsJson = JSON.stringify(wm.params || {});
      wm.phase = "confirming";
      wm.errorMessage = "";
      renderWriteModal();
      return;
    }
    wm.phase = "error";
    wm.errorMessage = writeErrorText(res);
    renderWriteModal();
  });
}

function needBatchConfirm() {
  var impact = wm.impact;
  if (!impact) return false;
  return wm.targets.length > 1 || num(impact.mutations) > writeThreshold();
}

/* 前端比服务端更严：批量（≥2 目标）一律要求输入数量（SPEC §4.8.3） */
function modalRules() {
  var spec = actionOf(wm.action) || {};
  var level = spec.level || "danger";
  var impact = wm.impact || {};
  var mutations = num(impact.mutations, wm.targets.length);
  var normal = level === "normal";
  var needName = !normal && wm.targets.length === 1;
  var needCount = !normal && (wm.targets.length > 1 ||
    (wm.targets.length === 1 && mutations > writeThreshold()));
  return {
    normal: normal,
    needName: needName,
    needCount: needCount,
    needAck: !normal,
    mutations: mutations,
    nameLabel: needName ? namePrompt(wm.targets[0]) : "",
    countLabel: needCount ? countPrompt() : ""
  };
}

function namePrompt(target) {
  var spec = actionOf(wm.action) || {};
  var kind = spec.target || "";
  if (kind === "package") return tpl(T.write.input.pkg, { id: (target && target.id) || "" });
  if (kind === "lease") return tpl(T.write.input.lease, { id: (target && target.lease_id) || "" });
  if (kind === "device") return tpl(T.write.input.device, { device: (target && target.device_id) || "" });
  if (kind === "repo") return tpl(T.write.input.repo, { repo: (target && target.repo) || "" });
  if (kind === "key") return tpl(T.write.input.key, { name: (target && target.name) || "" });
  if (kind === "user") return tpl(T.write.input.user, { user: (target && target.user_id) || "" });
  return tpl(T.write.input.pkg, { id: "" });
}

function expectedName() {
  var spec = actionOf(wm.action) || {};
  var target = wm.targets[0] || {};
  var kind = spec.target || "";
  if (kind === "package") return String(target.id || "");
  if (kind === "lease") return String(target.lease_id || "");
  if (kind === "device") return String(target.device_id || "");
  if (kind === "repo") return String(target.repo || "");
  if (kind === "key") return String(target.name || "");
  if (kind === "user") return String(target.user_id || "");
  return "";
}

function countPrompt() {
  var spec = actionOf(wm.action) || {};
  var mutations = num(wm.impact && wm.impact.mutations, wm.targets.length);
  if (spec.target === "device" && wm.targets.length === 1) {
    return tpl(T.write.input.countLeases, { n: mutations });
  }
  if (wm.targets.length > 1) return tpl(T.write.input.count, { n: mutations });
  return tpl(T.write.input.mutations, { n: mutations });
}

function validateModal() {
  var rules = modalRules();
  var nameInput = $("#wm-type");
  var countInput = $("#wm-count");
  var ack = $("#wm-ack");
  if (rules.normal) return { ok: true, need: "" };
  var nameOk = !rules.needName || (nameInput && nameInput.value.trim() === expectedName());
  var countOk = !rules.needCount ||
    (countInput && /^\d+$/.test(countInput.value.trim()) &&
     Number(countInput.value.trim()) === rules.mutations);
  var ackOk = !rules.needAck || !!(ack && ack.checked);
  var need = "";
  if (!nameOk) need = T.write.need.name;
  else if (!countOk) need = T.write.need.count;
  else if (!ackOk) need = tpl(T.write.need.ack, { ack: T.write.ack });
  return { ok: nameOk && countOk && ackOk, need: need };
}

function submitWrite(reuseIdem) {
  if (!wm.open || wm.phase !== "confirming") return;
  var check = validateModal();
  if (!check.ok) return;
  if (!wm.token) { previewNow(); return; }
  /* N6：params 严格绑定——execute 必须逐字复用 preview 那一份。
     若 UI 改了参数却没重新预览，先重新预览，绝不带着旧 token 发新参数。 */
  if (wm.paramsJson && JSON.stringify(wm.params) !== wm.paramsJson) {
    previewNow();
    return;
  }
  if (!reuseIdem || !wm.idem) wm.idem = newIdemKey();
  wm.phase = "submitting";
  wm.banner = "";
  renderWriteModal();
  toast("running", tpl(T.write.notif.running, { n: wm.targets.length }),
        { id: "write-running", sticky: true });
  state.notif.running = { n: wm.targets.length, action: actionLabelOf(wm.action) };
  if (state.notif.open) renderNotifPanel();
  var payload = {
    action: wm.action,
    targets: wm.targets,
    params: wm.paramsJson ? JSON.parse(wm.paramsJson) : wm.params,
    confirm_token: wm.token,
    __idem: wm.idem
  };
  if (needBatchConfirm()) {
    payload.batch_confirm = {
      mutations: num(wm.impact && wm.impact.mutations, wm.targets.length),
      target_hash: (wm.preview && wm.preview.target_hash) || "",
      impact_hash: (wm.impact && wm.impact.hash) || ""
    };
  }
  var spec = actionOf(wm.action) || {};
  var path = spec.path || "/api/write/preview";
  writeRequest(path, payload).then(function (res) {
    if (!wm.open) return;
    handleWriteResponse(res);
  });
}

/* 这些 400 表示"确认令牌与请求对不上"，都要回到确认步骤而不是关弹窗 */
var CONFIRM_CODES = {
  impact_mismatch: true,
  target_mismatch: true,
  params_mismatch: true,
  batch_confirm_required: true
};

function confirmBanner(code, body) {
  if (code === "params_mismatch") return T.write.diff.paramsTitle + "：" + T.write.diff.paramsBody;
  if (code === "target_mismatch") return T.write.diff.targetTitle + "：" + T.write.diff.targetBody;
  if (code === "batch_confirm_required") {
    return tpl(T.write.diff.batchBody, { n: num(body && body.actual, 0) });
  }
  return T.write.diff.title;
}

function handleWriteResponse(res) {
  clearToast("write-running");
  state.notif.running = null;
  var body = res.body || {};
  if (res.status === 200) {
    var results = body.results || [];
    if (res.replay) toast("info", T.write.replay);
    afterWrite(wm.action, results, {
      batch_id: body.batch_id,
      logged: body.logged,
      replay: res.replay,
      total: body.total,
      succeeded: body.succeeded,
      failed: body.failed
    });
    wm.phase = "done";
    closeWrite(false);
    return;
  }
  var code = String(body.code || "");
  if (res.status === 428) {
    wm.banner = T.write.expired;
    wm.bannerKind = "warn";
    wm.retried += 1;
    if (wm.retried <= 1) {
      /* 确认过期/重放：自动重新预览一次（保留用户输入） */
      wm.token = "";
      previewNow();
      return;
    }
    wm.phase = "error";
    wm.errorMessage = body.error ? String(body.error) : T.write.expiredTwice;
    wm.banner = "";
    renderWriteModal();
    return;
  }
  if (res.status === 400 && CONFIRM_CODES[code]) {
    /* 执行前的"对不上"档：token 已在服务端消费，必须重新预览拿新 token，
       并展示服务端给出的实际差异（impact/target/params 三种 diff） */
    wm.diff = body.diff || wm.diff || null;
    wm.banner = confirmBanner(code, body);
    wm.bannerKind = "danger";
    wm.errorMessage = body.error ? String(body.error) : wm.banner;
    wm.token = "";
    wm.phase = "confirming";
    wm._ack = false;
    renderWriteModal();
    var ack = $("#wm-ack");
    if (ack) ack.checked = false;
    previewNow().then(function () {
      if (!wm.open) return;
      renderWriteModal();
      var box = $("#wm-ack");
      if (box) box.checked = false;
      updateModalSubmit();
    });
    return;
  }
  if (res.status === 403) {
    wm.phase = "error";
    wm.errorMessage = writeErrorText(res);
    renderWriteModal();
    return;
  }
  if (res.status === 429 || res.status >= 500 || res.status === 0) {
    wm.phase = "error";
    wm.errorMessage = writeErrorText(res);
    wm.reuseIdem = (res.status >= 500 || res.status === 0);
    renderWriteModal();
    return;
  }
  /* 其余 4xx（409/404/403 单条）：已执行但该条失败 → 关闭并出结果面板 */
  afterWrite(wm.action, [{
    target: wm.targets[0], ok: false, code: res.status,
    error: body.error ? String(body.error) : tpl(T.err.http, { code: res.status })
  }], { batch_id: body.batch_id, logged: body.logged, total: 1, succeeded: 0, failed: 1 });
  wm.phase = "done";
  closeWrite(false);
}

function renderWriteModal() {
  var modal = $("#write-modal");
  if (!modal) return;
  var bd = $("#wm-bd");
  var title = $("#wm-title");
  var submit = $("#wm-submit");
  var cancel = $("#wm-cancel");
  if (!bd) return;
  var spec = actionOf(wm.action) || {};
  var level = spec.level || "danger";
  var n = wm.targets.length;
  var impact = wm.impact;
  var busy = wm.phase === "submitting";

  title.textContent = modalTitle();
  submit.replaceChildren(el("span", {
    text: busy ? T.write.busy : (submitLabel() || "确认")
  }));
  submit.disabled = busy || wm.phase !== "confirming" || !validateModal().ok;
  if (busy) submit.setAttribute("aria-busy", "true");
  else submit.removeAttribute("aria-busy");
  if (cancel) cancel.disabled = busy;
  var close = $("#wm-close");
  if (close) close.disabled = busy;

  var kids = [];
  if (wm.banner) {
    kids.push(el("p", {
      class: "fp-modal__banner is-" + (wm.bannerKind || "warn"),
      role: "status", text: wm.banner
    }));
  }
  if (wm.diff) kids.push(renderDiff(wm.diff));
  if (wm.phase === "previewing") {
    kids.push(renderPreviewSkeleton());
  } else if (wm.phase === "error") {
    kids.push(errorState(new ApiError(wm.errorMessage || T.err.failed, 0, ""),
                         function () { previewNow(); }, true));
  } else if (impact) {
    if (level !== "normal") kids.push(renderDanger(spec, level, n, impact));
    kids.push(renderImpact(spec, impact));
    var rules = modalRules();
    if (rules.needName) {
      var nameWrap = el("label", { class: "fp-field fp-field--wide" });
      nameWrap.appendChild(el("span", { id: "wm-type-label", text: rules.nameLabel }));
      var nameInput = el("input", {
        id: "wm-type", type: "text", autocomplete: "off", spellcheck: "false",
        value: wm._name || ""
      });
      nameInput.addEventListener("input", function (e) {
        wm._name = e.target.value;
        updateModalSubmit();
      });
      nameInput.addEventListener("keydown", onModalEnter);
      nameWrap.appendChild(nameInput);
      kids.push(nameWrap);
    }
    if (rules.needCount) {
      var countWrap = el("label", { class: "fp-field fp-field--wide" });
      countWrap.appendChild(el("span", { id: "wm-count-label", text: rules.countLabel }));
      var countInput = el("input", {
        id: "wm-count", type: "text", inputmode: "numeric",
        autocomplete: "off", spellcheck: "false", value: wm._count || ""
      });
      countInput.addEventListener("input", function (e) {
        wm._count = e.target.value;
        updateModalSubmit();
      });
      countInput.addEventListener("keydown", onModalEnter);
      countWrap.appendChild(countInput);
      kids.push(countWrap);
    }
    if (wm.action === "package.reopen" && level !== "normal") {
      var toWrap = el("label", { class: "fp-field fp-field--wide" });
      toWrap.appendChild(el("span", { text: T.write.toLabel }));
      var sel = el("select", { id: "wm-to" });
      [["claimed", T.write.to.claimed], ["available", T.write.to.available]]
        .forEach(function (pair) {
          var opt = el("option", { value: pair[0], text: pair[1] });
          if ((wm.params.to || "claimed") === pair[0]) opt.setAttribute("selected", "");
          sel.appendChild(opt);
        });
      sel.disabled = busy;
      sel.addEventListener("change", function (e) {
        wm.params.to = e.target.value;
        /* to 参与 impact 的 summary/哈希：改了就重新预览 */
        previewNow();
      });
      toWrap.appendChild(sel);
      kids.push(toWrap);
    }
    if (rules.needAck) {
      var ackWrap = el("label", { class: "fp-check fp-check--ack" });
      var ack = el("input", { type: "checkbox", id: "wm-ack" });
      ack.checked = !!wm._ack;
      ack.disabled = busy;
      ack.addEventListener("change", function (e) {
        wm._ack = e.target.checked;
        updateModalSubmit();
      });
      ackWrap.appendChild(ack);
      ackWrap.appendChild(el("span", { text: T.write.ack }));
      kids.push(ackWrap);
    }
    var need = el("p", { class: "fp-hint", id: "wm-need", "aria-live": "polite" });
    kids.push(need);
    kids.push(el("p", { class: "fp-error fp-error--inline fp-write__errmsg",
                        id: "wm-error", role: "alert", hidden: true }));
  }
  bd.replaceChildren.apply(bd, kids);
  if (busy) bd.setAttribute("aria-busy", "true");
  else bd.removeAttribute("aria-busy");
  /* 危险档优先把焦点放到输入框（SPEC §9.5）；重绘后焦点掉出弹窗也要补回来 */
  var first = $("#wm-type") || $("#wm-count") || $("#wm-ack") ||
              $("#wm-submit") || $("#wm-close");
  if (first && wm.open) {
    var active = document.activeElement;
    if (!active || active === document.body || !modal.contains(active)) {
      try { first.focus(); } catch (e) { /* 忽略 */ }
    }
  }
  updateModalSubmit();
}

function onModalEnter(e) {
  if (e.key !== "Enter") return;
  e.preventDefault();
  if (!e.target || e.target.tagName !== "INPUT") return;
  var check = validateModal();
  if (check.ok && wm.phase === "confirming") submitWrite(false);
}

function updateModalSubmit() {
  var submit = $("#wm-submit");
  if (!submit) return;
  var busy = wm.phase === "submitting";
  var check = validateModal();
  submit.disabled = busy || wm.phase !== "confirming" || !check.ok;
  var need = $("#wm-need");
  if (need) need.textContent = busy ? T.write.busy : check.need;
  var errBox = $("#wm-error");
  if (errBox) {
    var text = (wm.phase === "error" && wm.errorMessage) ? wm.errorMessage : "";
    errBox.hidden = !text;
    errBox.textContent = text;
  }
  if (wm.phase === "submitting") {
    submit.replaceChildren(el("span", { text: T.write.busy }));
  } else {
    submit.replaceChildren(el("span", { text: submitLabel() || "确认" }));
  }
}

function modalTitle() {
  var action = wm.action;
  var n = wm.targets.length;
  var t = wm.targets[0] || {};
  if (action === "package.reopen") {
    return n > 1 ? tpl(T.write.title.reopenBatch, { n: n }) : T.write.title.reopen;
  }
  if (action === "package.release") {
    return n > 1 ? tpl(T.write.title.releaseBatch, { n: n }) : T.write.title.release;
  }
  if (action === "lease.release") {
    return n > 1 ? tpl(T.write.title.leaseBatch, { n: n }) : T.write.title.lease;
  }
  if (action === "device.unassign") return tpl(T.write.title.unassign, { device: t.device_id || "" });
  if (action === "device.forget") return tpl(T.write.title.forget, { device: t.device_id || "" });
  if (action === "user.forget") return tpl(T.write.title.forgetUser, { user: t.user_id || "" });
  if (action === "repo.enable") return tpl(T.write.title.repoEnable, { repo: t.repo || "" });
  if (action === "repo.disable") return tpl(T.write.title.repoDisable, { repo: t.repo || "" });
  if (action === "key.revoke") return tpl(T.write.title.keyRevoke, { name: t.name || "" });
  return T.write.title.confirm;
}

function submitLabel() {
  var action = wm.action;
  var n = wm.targets.length;
  if (action === "package.reopen") return n > 1 ? tpl(T.write.btn.reopenN, { n: n }) : T.write.btn.reopen;
  if (action === "package.release") return n > 1 ? tpl(T.write.btn.releaseN, { n: n }) : T.write.btn.release;
  if (action === "lease.release") return n > 1 ? tpl(T.write.btn.leaseN, { n: n }) : T.write.btn.lease;
  if (action === "device.unassign") return T.write.btn.unassign;
  if (action === "device.forget") return T.write.btn.forget;
  if (action === "user.forget") return T.write.btn.forgetUser;
  if (action === "repo.enable") return T.write.btn.enable;
  if (action === "repo.disable") return T.write.btn.disable;
  if (action === "key.revoke") return T.write.btn.revoke;
  return T.write.btn.confirm;
}

function renderDanger(spec, level, n, impact) {
  var action = wm.action;
  var muts = num(impact && impact.mutations, n);
  var box = el("div", { class: "fp-modal__danger", id: "wm-danger" });
  var head = el("p", { class: "fp-modal__danger-t" });
  head.appendChild(icon("alert", 16, "fp-modal__danger-ic"));
  head.appendChild(el("span", { text: dangerTitleOf(action, n) }));
  box.appendChild(head);
  var body = el("p", { id: "wm-desc", text: dangerBodyOf(action, n, muts, impact) });
  box.appendChild(body);
  if (n > 1) {
    box.appendChild(el("p", { class: "fp-modal__danger-note",
                              text: tpl(T.write.batchNote, { n: n }) }));
  }
  return box;
}

function dangerTitleOf(action, n) {
  var d = T.write.dangerTitle;
  if (action === "package.reopen") return n > 1 ? tpl(d.reopenBatch, { n: n }) : d.reopen;
  if (action === "package.release") return d.release;
  if (action === "lease.release") return d.lease;
  if (action === "device.unassign") return d.unassign;
  if (action === "device.forget") return d.forget;
  if (action === "user.forget") return d.forgetUser;
  if (action === "repo.disable") return d.repoDisable;
  if (action === "repo.enable") return d.repoEnable;
  if (action === "key.revoke") return d.keyRevoke;
  return d.reopen;
}

function dangerBodyOf(action, n, muts, impact) {
  var counts = (impact && impact.counts) || {};
  var d = T.write.dangerBody;
  if (action === "package.reopen") {
    return tpl(d.reopen, { to: T.write.to[wm.params.to || "claimed"] || "" });
  }
  if (action === "package.release") return d.release;
  if (action === "lease.release") return d.lease;
  if (action === "device.unassign") {
    return tpl(d.unassign, { n: muts, m: num(counts.packages) });
  }
  if (action === "device.forget") {
    return tpl(d.forget, { n: num(counts.leases), m: num(counts.packages) });
  }
  if (action === "user.forget") {
    return tpl(d.forgetUser, { n: num(counts.devices) });
  }
  if (action === "repo.disable") return tpl(d.repoDisable, { n: num(counts.leases) });
  if (action === "repo.enable") return d.repoEnable;
  if (action === "key.revoke") {
    var last = "";
    var names = wm.targets.map(function (t) { return t.name; });
    if (names.length === 1) last = "";
    return tpl(d.keyRevoke, { last: last });
  }
  return "";
}

function renderImpact(spec, impact) {
  var items = impact.items || [];
  var counts = impact.counts || {};
  var mutationList = [];
  if (wm.targets.length > 1 || spec.target === "device") mutationList = impact.items || [];
  var box = el("section", { class: "fp-impact", id: "wm-impact",
                            "aria-labelledby": "wm-impact-h" });
  var head = el("h3", { id: "wm-impact-h" });
  var objName = T.write.impact.obj[spec.target] || "";
  var n = num(impact.mutations, wm.targets.length);
  head.textContent = tpl(T.write.impact.title, { n: n, obj: objName, action: actionLabelOf(wm.action) });
  box.appendChild(head);

  var list = el("ul", { class: "fp-impact__list", tabindex: "0",
                        "aria-label": T.write.impact.list });
  if (!items.length) {
    list.appendChild(el("li", { class: "fp-impact__empty", text: T.write.impact.empty }));
  } else {
    items.slice(0, 200).forEach(function (it) {
      list.appendChild(impactItem(it));
    });
  }
  box.appendChild(list);

  if (impact.truncated || items.length > 200) {
    box.appendChild(el("p", { class: "fp-impact__more",
      text: tpl(T.write.impact.more, { n: fmtNum(num(impact.total, items.length)) }) }));
  }
  var parts = [];
  ["packages", "leases", "devices", "repos", "keys", "users"].forEach(function (k) {
    var v = num(counts[k]);
    if (v > 0) parts.push((T.write.impact.count[k] || k) + " " + fmtNum(v));
  });
  if (parts.length) {
    box.appendChild(el("p", { class: "fp-impact__sum",
      text: tpl(T.write.impact.summary, { total: fmtNum(num(impact.total, n)), parts: parts.join(" · ") }) }));
  }
  var warns = impact.warnings || [];
  if (warns.length) {
    var wbox = el("div", { class: "fp-impact__warn" });
    wbox.appendChild(el("p", { class: "fp-impact__warn-t", text: T.write.impact.warnings }));
    var ul = el("ul", { class: "fp-impact__warn-list" });
    warns.slice(0, 8).forEach(function (w) { ul.appendChild(el("li", { text: String(w) })); });
    wbox.appendChild(ul);
    box.appendChild(wbox);
  }
  return box;
}

function impactItem(it) {
  var li = el("li", { class: "fp-impact__item" });
  li.appendChild(el("span", { class: "fp-impact__name", text: impactItemName(it) }));
  var badge = impactItemBadge(it);
  if (badge) li.appendChild(badge);
  var meta = impactItemMeta(it);
  if (meta) li.appendChild(el("span", { class: "fp-impact__meta", text: meta }));
  return li;
}

function impactItemName(it) {
  var kind = it.kind || "";
  if (kind === "package") return String(it.repo || "") + "/" + String(it.id || "");
  if (kind === "lease") return T.write.impact.obj.lease + " " + String(it.lease_id || "");
  if (kind === "device") return T.write.impact.obj.device + " " + String(it.device_id || "");
  if (kind === "repo") return T.write.impact.obj.repo + " " + String(it.repo || "");
  if (kind === "key") return T.write.impact.obj.key + " " + String(it.name || "");
  if (kind === "user") return T.write.impact.obj.user + " " + String(it.user_id || "");
  return String(it.label || "");
}

function impactItemBadge(it) {
  if (it.kind === "package") return statusBadge(it.status);
  if (it.kind === "lease") {
    var b = el("span", {
      class: "fp-badge " + (it.status === "open" ? "fp-badge--info" : "fp-badge--available")
    });
    b.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
    b.appendChild(el("span", {
      text: it.status === "open" ? T.status.open : (it.status || T.status.unknown)
    }));
    return b;
  }
  if (it.kind === "device") return devStatusBadge(it.status);
  if (it.kind === "repo") {
    var r = el("span", {
      class: "fp-badge " + (it.enabled ? "fp-badge--completed" : "fp-badge--available")
    });
    r.appendChild(el("span", { class: "fp-badge__dot", "aria-hidden": "true" }));
    r.appendChild(el("span", { text: it.enabled ? T.misc.enabled : T.misc.disabled }));
    return r;
  }
  return null;
}

function impactItemMeta(it) {
  if (it.kind === "package") return it.owner ? (T.col.owner + " " + it.owner) : "";
  if (it.kind === "lease") {
    var bits = [];
    if (it.repo || it.pkg) bits.push(String(it.repo || "") + "/" + String(it.pkg || ""));
    if (it.device_id) bits.push(T.write.impact.obj.device + " " + it.device_id);
    return bits.join(" · ");
  }
  if (it.kind === "device") return it.user_id ? (T.col.user + " " + it.user_id) : "";
  return "";
}

function renderPreviewSkeleton() {
  var box = el("div", { class: "fp-impact fp-impact--loading", "aria-busy": "true" });
  box.appendChild(el("p", { class: "fp-impact__sum", text: T.write.previewing }));
  var list = el("div", { class: "fp-dl" });
  for (var i = 0; i < 3; i++) {
    list.appendChild(el("div", { class: "fp-dl" }, [
      el("span", { class: "fp-skeleton fp-skeleton--text fp-w-40" }),
      el("span", { class: "fp-skeleton fp-skeleton--text fp-w-75" })
    ]));
  }
  box.appendChild(list);
  box.appendChild(el("span", { class: "fp-skeleton fp-skeleton--line" }));
  box.appendChild(el("span", { class: "fp-skeleton fp-skeleton--line" }));
  return box;
}

function renderDiff(diff) {
  diff = diff || {};
  var box = el("div", { class: "fp-modal__diff", role: "alert" });
  box.appendChild(el("p", { class: "fp-modal__diff-t", text: T.write.diff.title }));
  var muts = diff.mutations || {};
  var added = diff.added || [];
  var removed = diff.removed || [];
  var targetDiff = diff.target || null;
  if (muts.confirmed !== undefined || muts.actual !== undefined) {
    box.appendChild(el("p", {
      class: "fp-modal__diff-b",
      text: tpl(T.write.diff.body, {
        confirmed: fmtNum(muts.confirmed), actual: fmtNum(muts.actual)
      })
    }));
  }
  if (targetDiff) {
    box.appendChild(el("p", { class: "fp-modal__diff-b", text: T.write.diff.targetBody }));
  }
  if (added.length) box.appendChild(diffList(T.write.diff.added, added));
  if (removed.length) box.appendChild(diffList(T.write.diff.removed, removed));
  /* F6：数量不变但集合变了时服务端可能给空 added/removed——不能留一个空框 */
  if (!targetDiff && !added.length && !removed.length &&
      muts.confirmed === undefined && muts.actual === undefined) {
    box.appendChild(el("p", { class: "fp-modal__diff-b", text: T.write.diff.empty }));
  } else if (!added.length && !removed.length && !targetDiff &&
             num(muts.confirmed) === num(muts.actual)) {
    box.appendChild(el("p", { class: "fp-modal__diff-b", text: T.write.diff.empty }));
  }
  if (diff.truncated) {
    box.appendChild(el("p", { class: "fp-impact__more", text: tpl(T.write.diff.more, { n: "…" }) }));
  }
  return box;
}

function diffList(label, items) {
  var wrap = el("div", { class: "fp-modal__diff-list" });
  var head = el("p", { class: "fp-modal__diff-h",
                       text: tpl(label, { n: items.length }) });
  wrap.appendChild(head);
  var ul = el("ul", { class: "fp-impact__list" });
  items.slice(0, 10).forEach(function (it) {
    ul.appendChild(el("li", { class: "fp-impact__item" },
      [el("span", { class: "fp-impact__name", text: impactItemName(it) })]));
  });
  wrap.appendChild(ul);
  return wrap;
}

/* ------------------------------------------------------------------ 结果面板 */
function showResultPanel(label, results, meta) {
  var host = $("#main");
  if (!host) return;
  var old = $("#resultpanel");
  if (old) old.remove();
  var okCount = results.filter(function (r) { return r.ok; }).length;
  var failCount = results.length - okCount;
  var panel = el("section", {
    class: "fp-resultpanel", id: "resultpanel", role: "region",
    "aria-label": tpl(T.write.result.title, { action: label })
  });
  var hd = el("header", { class: "fp-resultpanel__hd" });
  hd.appendChild(el("h2", { class: "fp-resultpanel__t",
                            text: tpl(T.write.result.title, { action: label }) }));
  hd.appendChild((okCount && !failCount) ? resultBadge("ok")
    : (okCount ? resultBadge("partial") : resultBadge("error")));
  hd.appendChild(el("span", {
    class: "fp-resultpanel__sum",
    text: tpl(T.write.result.summary, { ok: okCount, fail: failCount, n: results.length })
  }));
  var close = el("button", {
    class: "fp-iconbtn", type: "button", "aria-label": T.write.result.close
  });
  close.appendChild(icon("close", 18));
  close.addEventListener("click", function () { panel.remove(); });
  hd.appendChild(close);
  panel.appendChild(hd);

  if (meta && meta.logged === false) {
    panel.appendChild(el("p", { class: "fp-note", text: T.write.result.loggedFalse }));
  }

  var failedTargets = [];
  var ul = el("ul", { class: "fp-resultpanel__list" });
  results.slice(0, 200).forEach(function (r) {
    var li = el("li", { class: "fp-resultpanel__item" + (r.ok ? " is-ok" : " is-fail") });
    li.appendChild(el("span", { class: "fp-resultpanel__name",
                                text: targetTextOf(r.target) || T.misc.dash }));
    li.appendChild(r.ok ? resultBadge("ok") : resultBadge("error"));
    if (!r.ok) {
      failedTargets.push(r.target);
      li.appendChild(el("span", { class: "fp-resultpanel__why",
        text: (r.error ? String(r.error) : tpl(T.err.http, { code: r.code || 0 })) }));
    }
    ul.appendChild(li);
  });
  panel.appendChild(ul);

  var ft = el("footer", { class: "fp-resultpanel__ft" });
  if (failedTargets.length) {
    var retry = el("button", { class: "fp-btn fp-btn--sm fp-btn--danger fp-write-action",
                               type: "button" });
    retry.appendChild(icon("refresh", 16));
    retry.appendChild(el("span", { text: T.write.retryFailed }));
    retry.addEventListener("click", function () {
      var action = wm.lastAction || (meta && meta.action) || "";
      startWrite(action || "package.reopen", failedTargets);
    });
    ft.appendChild(retry);
  }
  var audit = el("a", { class: "fp-btn fp-btn--sm", href: auditLink(meta) });
  audit.appendChild(el("span", { text: T.write.viewAudit }));
  ft.appendChild(audit);
  panel.appendChild(ft);
  host.insertBefore(panel, host.firstChild);
}

function auditLink(meta) {
  var w = state.wq;
  if (meta && meta.batch_id) return "#/audit?tab=writes&batch=" + encodeURIComponent(meta.batch_id);
  if (meta && meta.write_id) return "#/audit?tab=writes&write=" + encodeURIComponent(meta.write_id);
  return "#/audit?tab=writes";
}

function afterWrite(action, results, meta) {
  wm.lastAction = action;
  var label = actionLabelOf(action);
  meta = meta || {};
  var okCount = results.filter(function (r) { return r.ok; }).length;
  var failCount = results.length - okCount;
  var failedTargets = results.filter(function (r) { return !r.ok; })
                            .map(function (r) { return r.target; })
                            .filter(Boolean);
  var link = auditLink(meta);
  if (failCount === 0) {
    toast("success", tpl(T.write.toast.ok, { action: label, n: okCount }), { link: link });
  } else {
    var firstErr = "";
    results.some(function (r) {
      if (!r.ok) { firstErr = r.error ? String(r.error) : ""; return true; }
      return false;
    });
    var opts = { link: link };
    if (failedTargets.length) {
      opts.retry = function () {
        if (!writeOn()) return;
        startWrite(action, failedTargets);
      };
    }
    if (okCount > 0) {
      toast("danger", tpl(T.write.toast.partial, {
        action: label, ok: okCount, fail: failCount
      }), opts);
    } else {
      toast("danger", tpl(T.write.toast.fail, {
        action: label, error: firstErr || tpl(T.err.http, { code: 0 })
      }), opts);
    }
  }
  if (meta.logged === false) toast("info", T.write.loggedFalse);
  showResultPanel(label, results, meta);
  refreshAfterWrite();
  loadNotifications(true);
}

function refreshAfterWrite() {
  state.cache = { summary: null, anomalies: null, audit: null };
  state.wdata = null;
  if (state.route === "audit" && state.auditTab === "events") state.cache.audit = null;
  loadRoute();
  if (state.settingsOpen) {
    loadSettingsRepos();
    if (actionVisible("key.revoke")) loadSettingsKeys();
  }
}

/* ------------------------------------------------------------------ Toast 队列 */
function toast(kind, message, opts) {
  opts = opts || {};
  var box = $("#toasts");
  if (!box) return null;
  var id = opts.id || "";
  if (id) {
    var old = box.querySelector('[data-toast-id="' + cssEscape(id) + '"]');
    if (old) old.parentNode.removeChild(old);
  }
  while (box.children.length >= 3) box.removeChild(box.firstChild);
  var node = el("div", {
    class: "fp-toast fp-toast--" + (kind || "info"),
    role: (kind === "danger") ? "alert" : "status",
    dataset: { toastId: id }
  });
  node.appendChild(el("span", { class: "fp-toast__dot", "aria-hidden": "true" }));
  node.appendChild(el("span", { class: "fp-toast__msg", text: message }));
  var acts = el("span", { class: "fp-toast__acts" });
  if (opts.link) {
    var link = el("a", { class: "fp-toast__act", href: opts.link,
                         text: opts.linkText || T.write.viewAudit });
    link.addEventListener("click", function () { dismiss(); });
    acts.appendChild(link);
  }
  if (opts.retry) {
    var retry = el("button", { class: "fp-btn fp-btn--sm fp-btn--danger", type: "button" });
    retry.appendChild(el("span", { text: opts.retryText || T.write.retry }));
    retry.addEventListener("click", function () { dismiss(); opts.retry(); });
    acts.appendChild(retry);
  }
  if (acts.children.length) node.appendChild(acts);
  var x = el("button", { class: "fp-iconbtn fp-toast__x", type: "button", "aria-label": T.misc.closeToast });
  x.appendChild(icon("close", 16));
  node.appendChild(x);
  box.appendChild(node);
  window.requestAnimationFrame(function () { node.classList.add("is-visible"); });

  var sticky = kind === "running" || opts.sticky;
  var ttl = sticky ? 0 : (kind === "danger" ? 10000 : 4000);
  var timer = ttl ? setTimeout(dismiss, ttl) : null;
  function dismiss() {
    if (timer) clearTimeout(timer);
    node.classList.remove("is-visible");
    node.classList.add("is-leaving");
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 260);
  }
  /* 悬停或键盘聚焦都暂停计时，离开后按原 ttl 重新计时（同一个 pause/resume） */
  function pause() { if (timer) clearTimeout(timer); }
  function resume() { pause(); if (ttl) timer = setTimeout(dismiss, ttl); }
  x.addEventListener("click", dismiss);
  node.addEventListener("mouseenter", pause);
  node.addEventListener("mouseleave", resume);
  node.addEventListener("focusin", pause);
  node.addEventListener("focusout", resume);
  node.dismiss = dismiss;
  return node;
}

function clearToast(id) {
  var box = $("#toasts");
  if (!box) return;
  var node = box.querySelector('[data-toast-id="' + cssEscape(id) + '"]');
  if (node && node.dismiss) node.dismiss();
}

function setLive(text) {
  var live = $("#fp-live");
  if (live) live.textContent = text;
}

/* ------------------------------------------------------------------ 通知中心 */
function seenLoad() {
  var raw = storeGet("fp.notif.seen", "");
  if (!raw) return { ts_epoch: 0, write_id: "" };
  try {
    var parsed = JSON.parse(raw);
    return {
      ts_epoch: num(parsed.ts_epoch, 0),
      write_id: String(parsed.write_id || "")
    };
  } catch (e) { return { ts_epoch: 0, write_id: "" }; }
}

function seenSave(seen) {
  state.notif.seen = seen;
  storeSet("fp.notif.seen", JSON.stringify(seen));
}

function notifOpen() { return !!state.notif.open; }

function computeUnread() {
  var seen = state.notif.seen;
  var n = 0;
  (state.notif.items || []).forEach(function (it) {
    var ts = num(it.ts_epoch, 0);
    var wid = String(it.write_id || "");
    if (ts > seen.ts_epoch ||
        (ts === seen.ts_epoch && wid > String(seen.write_id || ""))) n += 1;
  });
  /* 申报制（SPEC-declare §3.5）：逾期未解决提醒持续未读，逐包计入角标 */
  (state.notif.overdue || []).forEach(function (it) {
    if (it.still_open) n += 1;
  });
  state.notif.unread = n;
}

function loadNotifications(silent) {
  var n = state.notif;
  var seq = ++n.seq;
  if (!silent) n.loading = true;
  if (n.open && !silent) renderNotifPanel();
  return api("/api/notifications?limit=20").then(function (d) {
    if (seq !== n.seq) return;
    n.items = d.items || [];
    n.latest = d.latest || null;
    n.denied = num(d.denied_total, 0);
    n.upstreamOk = d.upstream_ok !== false;
    n.loading = false;
    /* 逾期提醒走独立聚合端点（服务端每日去重、resolve 回填都在那边），
       这里只拉结果做角标与「逾期未申报」区块渲染 */
    return api("/api/overdue").then(function (od) {
      if (seq !== n.seq) return;
      n.overdue = (od && od.items) || [];
      n.overdueHours = (od && od.declare_hours) || null;
      computeUnread();
      renderNotifBadge();
      if (n.open) renderNotifPanel();
    });
  }).catch(function () {
    /* 通知失败不改变页面状态，只保留上次角标（SPEC §8.6） */
    n.loading = false;
  });
}

function renderNotifBadge() {
  var btn = $("#notif-btn");
  var badge = $("#notif-badge");
  if (!btn || !badge) return;
  var n = state.notif.unread;
  badge.textContent = n > 99 ? "99+" : String(n);
  badge.hidden = n <= 0;
  btn.classList.toggle("has-unread", n > 0);
  var denied = num(state.notif.denied, 0);
  var dot = document.getElementById("notif-dot");
  if (denied > 0) {
    if (!dot) {
      dot = el("span", { class: "fp-notifbtn__dot", id: "notif-dot" });
      btn.appendChild(dot);
    }
    dot.title = tpl(T.write.audit.deniedNote, { n: denied });
  } else if (dot && dot.parentNode) {
    dot.parentNode.removeChild(dot);
  }
  var label = n > 0 ? tpl(T.write.notif.unread, { n: n }) : T.write.notif.none;
  if (denied > 0) label += "；" + tpl(T.write.audit.deniedNote, { n: denied });
  btn.setAttribute("aria-label", label);
  btn.title = label;
}

function toggleNotif() {
  if (notifOpen()) closeNotif(); else openNotif();
}

function openNotif() {
  var panel = $("#notif");
  if (!panel) return;
  state.notif.open = true;
  panel.hidden = false;
  $("#notif-btn").setAttribute("aria-expanded", "true");
  window.requestAnimationFrame(function () { panel.classList.add("is-open"); });
  renderNotifPanel();
  /* 打开即拉一次最新（申报制 resolve 状态要新鲜，SPEC-declare §3.4） */
  loadNotifications(false);
  var close = $("#notif-close");
  if (close) close.focus();
}

function closeNotif() {
  var panel = $("#notif");
  if (!panel) return;
  state.notif.open = false;
  panel.classList.remove("is-open");
  var btn = $("#notif-btn");
  if (btn) {
    btn.setAttribute("aria-expanded", "false");
    try { btn.focus(); } catch (e) { /* 忽略 */ }
  }
  setTimeout(function () {
    if (!state.notif.open) panel.hidden = true;
  }, 180);
}

function renderNotifPanel() {
  var panel = $("#notif");
  var list = $("#notif-list");
  if (!panel || !list || !state.notif.open) return;
  $("#notif-title").textContent = T.write.notif.title;
  $("#notif-readall-t").textContent = T.write.notif.allRead;
  $("#notif-all").textContent = T.write.notif.all;
  var deniedFoot = $("#notif-denied");
  var deniedN = num(state.notif.denied, 0);
  if (deniedN > 0) {
    if (!deniedFoot) {
      deniedFoot = el("a", { class: "fp-notif__denied", id: "notif-denied",
                             href: "#/audit?tab=denied" });
      var allLink = $("#notif-all");
      if (allLink && allLink.parentNode) allLink.parentNode.appendChild(deniedFoot);
    }
    deniedFoot.textContent = tpl(T.write.audit.deniedNote, { n: deniedN }) + " →";
    deniedFoot.href = "#/audit?tab=denied";
    deniedFoot.hidden = false;
  } else if (deniedFoot) {
    deniedFoot.hidden = true;
  }
  var items = state.notif.items || [];
  /* 「逾期未申报」区块：逐包卡片（服务端已每日去重，前端直接渲染）；
     resolve（still_open=false）转灰「已解决」；计入角标在 computeUnread。
     先攒节点，最后与写日志条目一起 replaceChildren，避免被清空。 */
  var ovd = state.notif.overdue || [];
  var ovdSec = null;
  if (ovd.length) {
    ovdSec = el("section", { class: "fp-notif__ovd",
                             "aria-label": T.card.overdue });
    ovdSec.appendChild(el("h3", { class: "fp-notif__ovd-hd",
                                  text: T.card.overdue }));
    var canWrite = writeOn();
    var hours = num(state.notif.overdueHours, 48);
    ovd.forEach(function (it) {
      ovdSec.appendChild(overdueCard(it, hours, canWrite));
    });
  }
  if (state.notif.loading && !items.length && !ovd.length) {
    var sk = el("div", { "aria-busy": "true" });
    for (var i = 0; i < 3; i++) {
      sk.appendChild(el("div", { class: "fp-notif__item" }, [
        el("span", { class: "fp-skeleton fp-skeleton--text fp-w-50" }),
        el("span", { class: "fp-skeleton fp-skeleton--text fp-w-75" })
      ]));
    }
    list.replaceChildren(sk);
    return;
  }
  if (!items.length) {
    if (!ovd.length) {
      list.replaceChildren(emptyState(T.write.notif.empty, null, null));
    }
    return;
  }
  var rendered = items.map(function (it) {
    var node = el("div", {
      class: "fp-notif__item is-" + String(it.result || "ok"),
      role: it.result === "error" ? "alert" : null,
      dataset: { notif: String(it.id || "") }
    });
    var top = el("div", { class: "fp-notif__top" });
    top.appendChild(el("span", { class: "fp-notif__t", text: String(it.title || "") }));
    top.appendChild(resultBadge(it.result === "ok" ? "ok" : (it.result === "partial" ? "partial" : "error")));
    node.appendChild(top);
    var meta = el("div", { class: "fp-notif__meta" });
    meta.appendChild(el("span", { text: fmtTime(it.ts) || "" }));
    if (it.action_label) {
      meta.appendChild(el("span", { class: "fp-notif__sep", text: "·" }));
      meta.appendChild(el("span", { text: String(it.action_label) }));
    }
    if (it.failed_count) {
      meta.appendChild(el("span", { class: "fp-notif__sep", text: "·" }));
      meta.appendChild(el("span", {
        text: tpl(T.write.notif.counts, { ok: num(it.ok_count), fail: num(it.failed_count) })
      }));
    }
    node.appendChild(meta);
    if (it.error) {
      node.appendChild(el("p", { class: "fp-notif__err", text: String(it.error) }));
    }
    var acts = el("div", { class: "fp-notif__acts-row" });
    var link = el("a", {
      class: "fp-btn fp-btn--sm", href: it.audit_link || auditLink({ batch_id: it.batch_id })
    });
    link.appendChild(el("span", { text: T.write.viewAudit }));
    link.addEventListener("click", function () {
      markRead(it);
      closeNotif();
    });
    acts.appendChild(link);
    if (it.result !== "ok" && it.action && actionVisible(it.action) && writeOn()) {
      var retry = el("button", { class: "fp-btn fp-btn--sm fp-btn--danger fp-write-action",
                                 type: "button" });
      retry.appendChild(el("span", { text: T.write.retryFailed }));
      retry.addEventListener("click", function () { notifRetry(it); });
      acts.appendChild(retry);
    }
    node.appendChild(acts);
    return node;
  });
  if (state.notif.running) {
    var run = el("div", { class: "fp-notif__item is-running", role: "status" });
    var runTop = el("div", { class: "fp-notif__top" });
    runTop.appendChild(el("span", { class: "fp-notif__t",
      text: tpl(T.write.notif.running, { n: state.notif.running.n }) }));
    runTop.appendChild(resultBadge("running"));
    run.appendChild(runTop);
    rendered.unshift(run);
  }
  if (ovdSec) rendered.unshift(ovdSec);
  list.replaceChildren.apply(list, rendered);
}

function markRead(item) {
  var seen = state.notif.seen;
  var ts = num(item && item.ts_epoch, 0);
  if (ts > seen.ts_epoch ||
      (ts === seen.ts_epoch && String(item.write_id || "") > String(seen.write_id || ""))) {
    seenSave({ ts_epoch: ts, write_id: String(item.write_id || "") });
    computeUnread();
    renderNotifBadge();
  }
}

function markAllRead() {
  var latest = state.notif.latest;
  if (!latest) {
    var items = state.notif.items || [];
    if (!items.length) return;
    latest = { ts_epoch: items[0].ts_epoch, write_id: items[0].write_id };
  }
  seenSave({ ts_epoch: num(latest.ts_epoch, 0), write_id: String(latest.write_id || "") });
  computeUnread();
  renderNotifBadge();
  renderNotifPanel();
}

function notifRetry(item) {
  if (!writeOn()) { toast("info", T.write.readonly); return; }
  if (!item || !item.action || !actionVisible(item.action)) return;
  var qs = "?limit=200&batch_id=" + encodeURIComponent(item.batch_id || "");
  api("/api/write/history" + qs).then(function (d) {
    var failed = (d.items || [])
      .filter(function (r) { return r.result !== "ok"; })
      .map(function (r) { return r.target; })
      .filter(Boolean);
    if (!failed.length) { toast("info", T.write.notif.nothingToRetry); return; }
    startWrite(item.action, failed.slice(0, writeMax()));
  }).catch(function (err) {
    toast("danger", err && err.message ? String(err.message) : T.err.upstream);
  });
}

function startNotifTimer() {
  if (state.notif.timer) clearInterval(state.notif.timer);
  state.notif.timer = setInterval(function () {
    if (document.hidden) return;
    loadNotifications(true);
  }, 30000);
}

/* ------------------------------------------------------------------ 主题 */
function systemTheme() {
  try {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch (e) { return "dark"; }
}

function applyTheme(theme) {
  state.theme = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", state.theme);
  var btn = $("#theme");
  if (!btn) return;
  var isLight = state.theme === "light";
  var label = isLight ? T.btn.themeToDark : T.btn.themeToLight;
  btn.setAttribute("aria-pressed", isLight ? "true" : "false");
  btn.setAttribute("aria-label", label);
  btn.title = label;
  btn.replaceChildren(icon(isLight ? "moon" : "sun", 20),
                      el("span", { class: "fp-sr", text: label }));
}

function setTheme(theme, persist) {
  applyTheme(theme);
  if (persist) {
    storeSet("fp.theme", state.theme);
    state.themePinned = true;
  }
}

/* ------------------------------------------------------------------ 帮助 */
function buildHelp() {
  $("#help-title").textContent = T.help.title;
  var list = el("ul", { class: "fp-keys" });
  T.help.rows.forEach(function (row) {
    var li = el("li");
    li.appendChild(el("kbd", { text: row.k }));
    li.appendChild(el("span", { text: row.d }));
    list.appendChild(li);
  });
  $("#help-bd").replaceChildren(list);
  $("#help-close").setAttribute("aria-label", T.help.close);
  $("#help-close").replaceChildren(icon("close", 18));
}

function openHelp() {
  if (drawerState.open) closeDrawer();
  if (notifOpen()) closeNotif();
  helpOpen = true;
  $("#help-wrap").hidden = false;
  setBackgroundInert(true);
  $("#help-close").focus();
}

function closeHelp() {
  if (!helpOpen) return;
  helpOpen = false;
  $("#help-wrap").hidden = true;
  setBackgroundInert(drawerState.open);
  var helpBtn = $("#help-btn");
  if (helpBtn) helpBtn.focus();
}

function toggleHelp() { if (helpOpen) closeHelp(); else openHelp(); }

/* ------------------------------------------------------------------ 键盘 */
function isTyping(target) {
  if (!target) return false;
  var tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

function focusSearch() {
  if (state.route !== "packages" && state.route !== "audit") {
    /* 切页由 hashchange 驱动，焦点在 applyRoute 里补，避免和 hashchange 抢时序 */
    state.focusSearchPending = true;
    go("packages");
    return;
  }
  var input = state.route === "audit" ? $("#f-audit") : $("#f-q");
  if (input) { input.focus(); input.select(); }
}

function refreshNow() {
  var btn = $("#refresh");
  if (btn) btn.classList.add("is-busy");
  var promise = loadRoute();
  var done = function () { if (btn) btn.classList.remove("is-busy"); };
  if (promise && promise.then) promise.then(done, done);
  else done();
}

function onKeydown(e) {
  if (e.defaultPrevented) return;
  var key = e.key;

  if (key === "Escape") {
    if (wm.open) {
      if (wm.phase === "submitting") { e.preventDefault(); return; }
      e.preventDefault();
      closeWrite(false);
      return;
    }
    if (state.settingsOpen) { e.preventDefault(); closeSettings(); return; }
    if (notifOpen()) { e.preventDefault(); closeNotif(); return; }
    if (drawerState.open) { e.preventDefault(); closeDrawer(); return; }
    if (helpOpen) { e.preventDefault(); closeHelp(); return; }
    if (isTyping(e.target) && e.target.tagName === "INPUT") { e.target.blur(); return; }
    return;
  }

  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (isTyping(e.target)) return;

  if (key === "/") { e.preventDefault(); focusSearch(); return; }
  if (key === "?") { e.preventDefault(); toggleHelp(); return; }

  var lower = key.length === 1 ? key.toLowerCase() : key;
  var map = {
    r: refreshNow,
    n: toggleNotif,
    g: function () { go("overview"); },
    p: function () { go("packages"); },
    d: function () { go("devices"); },
    a: function () { go("anomalies"); },
    u: function () { go("audit"); },
    "1": function () { go("overview"); },
    "2": function () { go("packages"); },
    "3": function () { go("devices"); },
    "4": function () { go("anomalies"); },
    "5": function () { go("audit"); }
  };
  var fn = map[lower];
  if (fn) { e.preventDefault(); fn(); }
}

function focusableList(root) {
  var sel = "a[href], button:not([disabled]), input:not([disabled]), " +
            "select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
  var nodes = root.querySelectorAll(sel);
  var out = [];
  for (var i = 0; i < nodes.length; i++) {
    var node = nodes[i];
    if (node.hasAttribute("hidden")) continue;
    if (node.closest("[hidden]")) continue;
    out.push(node);
  }
  return out;
}

function trapRoot() {
  if (wm.open) return $("#write-modal");
  if (state.settingsOpen) return $("#settings");
  if (drawerState.open) return $("#drawer");
  if (helpOpen) return $("#help");
  return null;
}

function onTrapTab(e) {
  if (e.key !== "Tab") return;
  var root = trapRoot();
  if (!root) return;
  var list = focusableList(root);
  if (!list.length) return;
  var first = list[0];
  var last = list[list.length - 1];
  if (!root.contains(document.activeElement)) {
    e.preventDefault();
    first.focus();
    return;
  }
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

/* ------------------------------------------------------------------ 绑定 */
function fillSelect(select, items, allLabel) {
  select.replaceChildren();
  if (allLabel) {
    select.appendChild(el("option", { value: "", text: allLabel }));
  }
  items.forEach(function (it) {
    select.appendChild(el("option", { value: it.value, text: it.label }));
  });
}

function fillMetaControls(meta) {
  var repos = (meta && meta.repos) || [];
  fillSelect($("#f-repo"), repos.map(function (r) {
    return { value: r.repo, label: r.repo };
  }), T.opt.allRepos);

  var statuses = (meta && meta.statuses) || [
    { value: "available", label: T.status.available },
    { value: "claimed", label: T.status.claimed },
    { value: "completed", label: T.status.completed }
  ];
  fillSelect($("#f-status"), statuses.map(function (s) {
    return { value: s.value, label: s.label };
  }), T.opt.allStatuses);

  var sizes = (meta && meta.page_sizes) || [25, 50, 100, 200];
  fillSelect($("#pkg-size"), sizes.map(function (n) {
    return { value: String(n), label: String(n) };
  }), null);

  var saved = Number(storeGet("fp.size", ""));
  if (sizes.indexOf(saved) >= 0) state.pkg.size = saved;
  else if (sizes.indexOf(50) >= 0) state.pkg.size = 50;
  fillAuditControls();
  syncPackageControls();
}

function fillAuditControls() {
  var pathOpts = [];
  writeActions().forEach(function (a) {
    if (a.path) pathOpts.push({ value: a.path, label: (a.label || "") + " " + a.path });
  });
  fillSelect($("#f-au-path"), pathOpts, T.write.audit.pathAll);
  var actions = writeActions();
  var opts = [];
  actions.forEach(function (a) {
    opts.push({ value: a.id, label: a.label || a.id });
  });
  fillSelect($("#f-au-action"), opts, T.write.audit.actionAll);
  fillSelect($("#f-au-result"), [
    { value: "ok", label: T.write.audit.resultOk },
    { value: "error", label: T.write.audit.resultError }
  ], T.write.audit.resultAll);
  fillSelect($("#f-au-range"), [
    { value: "today", label: T.write.audit.rangeToday },
    { value: "7d", label: T.write.audit.range7 },
    { value: "30d", label: T.write.audit.range30 }
  ], T.write.audit.rangeAll);
  fillSelect($("#f-au-size"), [25, 50, 100].map(function (n) {
    return { value: String(n), label: String(n) };
  }), null);
}

function syncPackageControls() {
  var s = state.pkg;
  var repo = $("#f-repo");
  var status = $("#f-status");
  var q = $("#f-q");
  var size = $("#pkg-size");
  if (repo && repo.querySelector("option[value='" + cssEscape(s.repo) + "']")) repo.value = s.repo;
  else if (repo) repo.value = "";
  if (status && status.querySelector("option[value='" + cssEscape(s.status) + "']")) status.value = s.status;
  else if (status) status.value = "";
  if (q && q.value !== s.q) q.value = s.q;
  if (size) size.value = String(s.size);
  var ovd = $("#pkg-ovd-first");
  if (ovd) ovd.checked = !!s.overdueFirst;
}

function cssEscape(v) {
  return String(v || "").replace(/["\\]/g, "\\$&");
}

function wireEvents() {
  $("#refresh").addEventListener("click", refreshNow);
  $("#theme").addEventListener("click", function () {
    setTheme(state.theme === "light" ? "dark" : "light", true);
    syncUrl();
  });
  $("#help-btn").addEventListener("click", toggleHelp);
  $("#auto").addEventListener("change", function (e) {
    setAuto(e.target.checked, true);
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  $("#help-close").addEventListener("click", closeHelp);
  $("#help-wrap").addEventListener("click", function (e) {
    if (e.target === $("#help-wrap")) closeHelp();
  });

  $("#notif-btn").addEventListener("click", toggleNotif);
  $("#notif-close").addEventListener("click", closeNotif);
  $("#notif-readall").addEventListener("click", markAllRead);

  $("#f-repo").addEventListener("change", function (e) {
    state.pkg.repo = e.target.value;
    state.pkg.page = 1;
    clearSel("packages");
    syncUrl();
    loadPackages();
  });
  /* SPEC-link §4.5：设备筛选（online/idle/offline/all）+ archived 开关 */
  $("#f-dev-status").addEventListener("change", function (e) {
    var v = String(e.target.value || "all");
    state.devFilter = ["online", "idle", "offline", "all"].indexOf(v) >= 0 ? v : "all";
    state.pages.dev = 1;
    syncUrl();
    if (state.cache.summary) renderDevices(state.cache.summary);
  });
  $("#f-dev-archived").addEventListener("change", function (e) {
    state.devShowArchived = !!e.target.checked;
    state.pages.dev = 1;
    syncUrl();
    if (state.cache.summary) renderDevices(state.cache.summary);
  });
  $("#f-status").addEventListener("change", function (e) {
    state.pkg.status = e.target.value;
    state.pkg.page = 1;
    clearSel("packages");
    syncUrl();
    loadPackages();
  });
  var qTimer = null;
  $("#f-q").addEventListener("input", function (e) {
    clearTimeout(qTimer);
    var value = e.target.value.trim();
    qTimer = setTimeout(function () {
      state.pkg.q = value;
      state.pkg.page = 1;
      clearSel("packages");
      syncUrl();
      loadPackages();
    }, 280);
  });
  $("#pkg-size").addEventListener("change", function (e) {
    var n = parseInt(e.target.value, 10);
    if (!isFinite(n)) return;
    state.pkg.size = n;
    state.pkg.page = 1;
    storeSet("fp.size", n);
    syncUrl();
    loadPackages();
  });
  /* 申报制（SPEC-declare §3.5）：逾期优先排序开关 */
  var ovdFirst = $("#pkg-ovd-first");
  if (ovdFirst) {
    ovdFirst.addEventListener("change", function (e) {
      state.pkg.overdueFirst = !!e.target.checked;
      state.pkg.page = 1;
      clearSel("packages");
      syncUrl();
      loadPackages();
    });
  }
  $("#pkg-prev").addEventListener("click", function () {
    if (state.pkg.page > 1) {
      state.pkg.page -= 1;
      clearSel("packages");
      syncUrl();
      loadPackages();
    }
  });
  $("#pkg-next").addEventListener("click", function () {
    var page = state.pkgData && state.pkgData.page ? state.pkgData.page : {};
    if (state.pkg.page < Number(page.pages || 1)) {
      state.pkg.page += 1;
      clearSel("packages");
      syncUrl();
      loadPackages();
    }
  });
  $("#pkg-export").addEventListener("click", exportCsv);

  /* 审计：Tab 切换（普通 button + aria-pressed） */
  $("#au-tab-events").addEventListener("click", function () { switchAuditTab("events"); });
  $("#au-tab-writes").addEventListener("click", function () { switchAuditTab("writes"); });
  $("#au-tab-denied").addEventListener("click", function () { switchAuditTab("denied"); });

  var auditTimer = null;
  $("#f-audit").addEventListener("input", function (e) {
    clearTimeout(auditTimer);
    var value = e.target.value;
    auditTimer = setTimeout(function () {
      if (state.auditTab === "writes") {
        state.wq.q = value.trim();
        state.wq.page = 1;
        syncUrl();
        loadAuditWrites();
      } else {
        state.auditQ = value;
        state.pages.audit = 1;
        if (state.cache.audit) renderAuditEvents(state.cache.audit);
      }
    }, 240);
  });

  function auditFilterChange(fn) {
    return function (e) {
      fn(e.target.value);
      state.wq.page = 1;
      syncAuditControls();
      syncUrl();
      if (state.auditTab === "denied") loadAuditDenied();
      else loadAuditWrites();
    };
  }
  $("#f-au-action").addEventListener("change", auditFilterChange(function (v) {
    state.wq.action = v;
  }));
  $("#f-au-result").addEventListener("change", auditFilterChange(function (v) {
    state.wq.result = v;
  }));
  $("#f-au-range").addEventListener("change", auditFilterChange(function (v) {
    state.wq.range = v;
  }));
  $("#f-au-size").addEventListener("change", auditFilterChange(function (v) {
    var n = parseInt(v, 10);
    if (isFinite(n)) state.wq.size = n;
  }));
  var actorTimer = null;
  $("#f-au-actor").addEventListener("input", function (e) {
    clearTimeout(actorTimer);
    var value = e.target.value.trim();
    actorTimer = setTimeout(function () {
      if (state.auditTab === "denied") {
        state.dq.actor = value;
        state.dq.page = 1;
        syncUrl();
        loadAuditDenied();
        return;
      }
      state.wq.actor = value;
      state.wq.page = 1;
      syncUrl();
      loadAuditWrites();
    }, 240);
  });
  var reasonTimer = null;
  $("#f-au-reason").addEventListener("input", function (e) {
    clearTimeout(reasonTimer);
    var value = e.target.value.trim();
    reasonTimer = setTimeout(function () {
      state.dq.reason = value;
      state.dq.page = 1;
      syncUrl();
      loadAuditDenied();
    }, 240);
  });
  $("#f-au-path").addEventListener("change", function (e) {
    state.dq.path = e.target.value;
    state.dq.page = 1;
    syncUrl();
    loadAuditDenied();
  });
  $("#au-clear").addEventListener("click", clearAuditFilter);
  $("#au-export-jsonl").addEventListener("click", function () { exportWrites("jsonl"); });
  $("#au-export-csv").addEventListener("click", function () { exportWrites("csv"); });

  /* 客户端分页：DOM 前缀 → state.pages 键 → 重渲染入口 */
  [
    { dom: "dev", key: "dev", target: "devices" },
    { dom: "lease", key: "lease", target: "devices" },
    { dom: "an-stuck", key: "stuck", target: "anomalies" },
    { dom: "an-orphan", key: "orphan", target: "anomalies" },
    { dom: "an-ghost", key: "ghost", target: "anomalies" },
    { dom: "au", key: "audit", target: "audit" }
  ].forEach(function (spec) {
    var step = function (delta) {
      return function () {
        if (spec.target === "audit" && state.auditTab === "writes") {
          var pages = Math.max(1, Math.ceil(num(state.wdata && state.wdata.total) / state.wq.size));
          state.wq.page = Math.min(Math.max(1, state.wq.page + delta), pages);
          syncUrl();
          loadAuditWrites();
          return;
        }
        if (spec.target === "audit" && state.auditTab === "denied") {
          var dpages = Math.max(1,
            Math.ceil(num(state.ddata && state.ddata.total) / state.dq.size));
          state.dq.page = Math.min(Math.max(1, state.dq.page + delta), dpages);
          syncUrl();
          loadAuditDenied();
          return;
        }
        state.pages[spec.key] = Math.max(1, (state.pages[spec.key] || 1) + delta);
        if (spec.target === "audit") {
          if (state.cache.audit) renderAuditEvents(state.cache.audit);
        } else if (spec.target === "devices") {
          if (state.cache.summary) {
            if (state.devDetailId) renderDeviceDetail();
            else renderDevices(state.cache.summary);
          }
        } else if (state.cache.anomalies) {
          renderAnomalies(state.cache.anomalies);
        }
      };
    };
    var prev = document.getElementById(spec.dom + "-prev");
    var next = document.getElementById(spec.dom + "-next");
    if (prev) prev.addEventListener("click", step(-1));
    if (next) next.addEventListener("click", step(1));
  });

  document.addEventListener("keydown", onKeydown);
  document.addEventListener("keydown", onTrapTab);

  /* 点通知面板外部关闭（popover 语义） */
  document.addEventListener("click", function (e) {
    if (!notifOpen()) return;
    var panel = $("#notif");
    var btn = $("#notif-btn");
    if (panel && panel.contains(e.target)) return;
    if (btn && btn.contains(e.target)) return;
    closeNotif();
  });

  window.addEventListener("hashchange", function () {
    if (state.silentHash && location.hash === state.silentHash) {
      state.silentHash = "";
      return;
    }
    /* 「跳到主内容」等非路由 hash 不当作换页 */
    if (location.hash.indexOf("#/") !== 0) {
      if (location.hash === "#main") {
        var main = $("#main");
        if (main) main.focus();
      }
      return;
    }
    applyRoute(true);
  });

  var skip = $(".fp-skip");
  if (skip) {
    skip.addEventListener("click", function (e) {
      e.preventDefault();
      var main = $("#main");
      if (main) main.focus();
    });
  }

  /* 已持有时长本地插值：不刷新接口，每秒重算（申报制 SPEC-declare §2.3，
     原 TTL 插值改为 now - claim_at 口径的已持时长插值，同样是纯本地插值） */
  setInterval(function () {
    if (state.route !== "devices") return;
    var nodes = document.querySelectorAll("[data-held]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var base = Number(node.dataset.held);
      var at = Number(node.dataset.heldAt);
      if (!isFinite(base) || !isFinite(at)) continue;
      var held = base + (Date.now() - at) / 1000;
      var text = fmtHeld(held);
      node.textContent = text || T.misc.dash;
      if (node.dataset.heldFuse === "1") {
        node.title = T.misc.heldFuse.replace("{t}", text || "");
      }
    }
  }, 1000);

  /* 系统主题变化：只在用户没手动选过时跟随 */
  try {
    var mq = window.matchMedia("(prefers-color-scheme: light)");
    var onChange = function () {
      if (!storeGet("fp.theme", null)) applyTheme(systemTheme());
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  } catch (e) { /* 忽略 */ }
}

function setAuto(on, persist) {
  state.auto = !!on;
  var box = $("#auto");
  if (box) box.checked = !!on;
  if (persist) storeSet("fp.auto", on ? "1" : "0");
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (on) {
    state.timer = setInterval(function () {
      if (document.hidden) return;
      loadRoute();
    }, state.refreshMs);
  }
}

/* ------------------------------------------------------------------ 写模式装配 */
function ensureHeaderWrite() {
  if ($("#settings-btn")) return;
  var btn = el("button", {
    class: "fp-iconbtn fp-settings fp-write-action", id: "settings-btn", type: "button"
  });
  btn.appendChild(icon("gear", 20));
  btn.addEventListener("click", function () { openSettings(btn); });
  var theme = $("#theme");
  if (theme && theme.parentNode) theme.parentNode.insertBefore(btn, theme);
}

function ensureBulkBars() {
  if (!writeOn()) return;
  if (!$("#bulkbar-pkg")) {
    var pkgBar = el("div", {
      class: "fp-bulkbar fp-write-action", id: "bulkbar-pkg", hidden: true,
      role: "region", "aria-label": T.write.bulk.region
    });
    var pkgWrap = $("#pkg-wrap");
    pkgWrap.parentNode.insertBefore(pkgBar, pkgWrap);
  }
  if (!$("#bulkbar-lease")) {
    var leaseBar = el("div", {
      class: "fp-bulkbar fp-write-action", id: "bulkbar-lease", hidden: true,
      role: "region", "aria-label": T.write.bulk.regionLease
    });
    var leaseWrap = $("#lease-wrap");
    leaseWrap.parentNode.insertBefore(leaseBar, leaseWrap);
  }
  if (!$("#pkg-mobile-hint")) {
    var hint = el("p", {
      class: "fp-mobile-hint fp-write-action", id: "pkg-mobile-hint",
      text: T.write.bulk.mobile
    });
    var bar = $("#bulkbar-pkg");
    bar.parentNode.insertBefore(hint, bar);
  }
  updateBulkBar("packages");
  updateBulkBar("leases");
}

function removeWriteChrome() {
  ["#wm-wrap", "#wm-scrim", "#settings", "#settings-scrim", "#settings-btn",
   "#bulkbar-pkg", "#bulkbar-lease", "#pkg-mobile-hint", "#resultpanel"].forEach(function (sel) {
    var node = $(sel);
    if (node && node.parentNode) node.parentNode.removeChild(node);
  });
  /* 其它页面的 DOM 可能还留着上一轮的写按钮（只在切到该页时才重绘）：
     关写时整体清空，保证"只读模式下写入口节点数 0"在任何时刻都成立。 */
  ["ov-repo", "pkg-wrap", "dev-wrap", "lease-wrap", "an-stuck", "an-orphan",
   "an-ghost", "au-wrap"].forEach(function (id) {
    var node = document.getElementById(id);
    if (node) node.replaceChildren();
  });
  state.sel = { packages: {}, leases: {} };
  state.settingsOpen = false;
  wm.open = false;
  wm.phase = "idle";
  document.body.classList.remove("fp-noscroll");
  setBackgroundInert(false);
}

function applyWriteMode(rerender) {
  var on = writeOn();
  state.write = on;
  document.body.classList.toggle("fp-write", on);
  var sub = $("#brand-sub");
  if (sub) sub.textContent = on ? T.app.brandSubWrite : T.app.brandSub;
  var note = $("#ft-note");
  if (note) note.textContent = on ? T.app.footerNoteWrite : T.app.footerNote;
  var sideVer = $("#side-ver");
  if (sideVer && state.meta && state.meta.version) {
    sideVer.textContent = "v" + state.meta.version + " · " +
      (on ? T.app.footerNoteWrite : T.app.footerNote);
  }
  /* 只读时在页头挂一颗常驻提示（≥768；<768 由页脚给出原因），
     写模式恢复后立即摘掉，避免出现"看起来能写"的误导 */
  var roPill = document.getElementById("ro-pill");
  if (!on) {
    if (!roPill) {
      roPill = el("span", { class: "fp-pill fp-pill--idle", id: "ro-pill", role: "note" });
      var upPill = $("#up-pill");
      if (upPill && upPill.parentNode) upPill.parentNode.insertBefore(roPill, upPill);
      else $("#header").appendChild(roPill);
    }
    roPill.textContent = T.write.roPill;
    roPill.title = state.writeReason || T.write.readonlyLong;
  } else if (roPill && roPill.parentNode) {
    roPill.parentNode.removeChild(roPill);
  }
  if (on) {
    ensureHeaderWrite();
    ensureWriteModal();
    ensureBulkBars();
    var btn = $("#settings-btn");
    if (btn) {
      btn.setAttribute("aria-label", T.btn.settings);
      btn.title = T.btn.settings;
    }
  } else {
    removeWriteChrome();
  }
  if (rerender) loadRoute();
}

/* ------------------------------------------------------------------ 启动 */
function buildChrome() {
  document.title = T.nav.overview + " · " + T.app.title;
  $("#brand-name").textContent = T.app.brand;
  $("#brand-sub").textContent = T.app.brandSub;
  $("#nav-side").setAttribute("aria-label", T.app.navLabel);
  $("#tabs").setAttribute("aria-label", T.app.navLabel);
  $("#tabbar").setAttribute("aria-label", T.app.navLabel);
  [
    ["#pkg-pager", T.pager.labelPackages],
    ["#dev-pager", T.pager.labelDevices],
    ["#lease-pager", T.pager.labelLeases],
    ["#an-stuck-pager", T.pager.labelStuck],
    ["#an-orphan-pager", T.pager.labelOrphan],
    ["#an-ghost-pager", T.pager.labelGhost],
    ["#au-pager", T.pager.labelAudit]
  ].forEach(function (pair) {
    var node = $(pair[0]);
    if (node) node.setAttribute("aria-label", pair[1]);
  });

  var skip = el("a", { class: "fp-skip", href: "#main", text: T.app.skip });
  document.body.insertBefore(skip, document.body.firstChild);

  $("#refresh-label").textContent = T.btn.refresh;
  $("#refresh").setAttribute("aria-label", T.btn.refresh);
  $("#refresh").insertBefore(icon("refresh", 16), $("#refresh-label"));
  $("#help-btn").setAttribute("aria-label", T.btn.help);
  $("#help-btn").title = T.btn.help;
  $("#help-btn").replaceChildren(icon("help", 20), el("span", { class: "fp-sr", text: T.btn.help }));
  $("#logout").setAttribute("aria-label", T.btn.logout);
  $("#logout").replaceChildren(icon("logout", 16),
      el("span", { class: "fp-logout-t", text: T.btn.logout }));
  $("#drawer-close").setAttribute("aria-label", T.misc.closeDrawer);
  $("#drawer-close").replaceChildren(icon("close", 20));
  $("#auto").setAttribute("aria-label", T.btn.autoRefresh);
  $("#auto-label").textContent = T.btn.autoRefresh;

  $("#notif-btn").replaceChildren(icon("bell", 20),
                                   el("span", { class: "fp-sr", text: T.btn.notifications }),
                                   $("#notif-badge"));
  $("#notif-close").setAttribute("aria-label", T.write.notif.close);
  $("#notif-close").replaceChildren(icon("close", 20));
  $("#notif-title").textContent = T.write.notif.title;

  $("#lbl-repo").textContent = T.field.repo;
  $("#lbl-status").textContent = T.field.status;
  $("#lbl-q").textContent = T.field.q;
  $("#lbl-size").textContent = T.field.size;
  var lblOvd = $("#lbl-pkg-ovd");
  if (lblOvd) lblOvd.textContent = T.field.overdueFirst;
  $("#lbl-audit").textContent = T.field.audit;
  $("#f-q").placeholder = T.ph.q;
  $("#f-audit").placeholder = T.ph.audit;

  $("#au-tab-events-t").textContent = T.write.audit.tabEvents;
  $("#au-tab-writes-t").textContent = T.write.audit.tabWrites;
  $("#au-tab-denied-t").textContent = T.write.audit.tabDenied;
  $("#au-tabs").setAttribute("aria-label", T.write.audit.tabsLabel);
  $("#lbl-audit-note").textContent = T.field.audit;
  $("#au-q-note-t").textContent = T.write.audit.deniedLabel;
  $("#lbl-au-reason").textContent = T.write.audit.reasonLabel;
  $("#f-au-reason").placeholder = T.write.audit.reasonPh;
  $("#lbl-au-path").textContent = T.write.audit.pathLabel;
  $("#au-export-jsonl-t").textContent = T.write.audit.exportJsonl;
  $("#au-export-csv-t").textContent = T.write.audit.exportCsv;
  $("#au-clear-t").textContent = T.btn.clearFilter;
  $("#lbl-au-action").textContent = T.col.action;
  $("#lbl-au-actor").textContent = T.col.actor;
  $("#lbl-au-result").textContent = T.col.result;
  $("#lbl-au-range").textContent = T.write.audit.rangeLabel;
  $("#lbl-au-size").textContent = T.field.size;
  $("#f-au-actor").placeholder = T.write.audit.actorPh;

  $("#pkg-export-label").textContent = T.btn.export;
  $("#pkg-prev").setAttribute("aria-label", T.btn.prev);
  $("#pkg-prev").replaceChildren(icon("prev", 16));
  $("#pkg-next").setAttribute("aria-label", T.btn.next);
  $("#pkg-next").replaceChildren(icon("next", 16));
  ["dev", "lease", "stuck", "orphan", "ghost", "au"].forEach(function (prefix) {
    var prev = document.getElementById(prefix + "-prev");
    var next = document.getElementById(prefix + "-next");
    if (prev) {
      prev.setAttribute("aria-label", T.btn.prev);
      prev.replaceChildren(icon("prev", 16));
    }
    if (next) {
      next.setAttribute("aria-label", T.btn.next);
      next.replaceChildren(icon("next", 16));
    }
  });

  $("#ov-repo-title").textContent = T.card.repos;
  $("#dev-title").textContent = T.card.devices;
  $("#lease-title").textContent = T.card.leases;
  $("#an-stuck-title").textContent = T.card.stuck;
  $("#an-orphan-title").textContent = T.card.orphan;
  $("#an-ghost-title").textContent = T.card.ghost;
  $("#ovd-title").textContent = T.card.overdue;
  $("#ovd-note").textContent = T.misc.overdueNote;

  $("#ft-brand").textContent = T.app.footerBrand;
  $("#ft-note").textContent = T.app.footerNote;

  buildNav();
  buildHelp();
  state.themePinned = !!storeGet("fp.theme", null);
  applyTheme(storeGet("fp.theme", null) || systemTheme());
  renderNotifBadge();
}

function loadMeta() {
  return api("/api/meta").then(function (meta) {
    state.meta = meta;
    state.refreshMs = Number(meta.refresh_ms) || 15000;
    if (meta.version) {
      $("#ft-version").textContent = "v" + meta.version;
      $("#side-ver").textContent = "v" + meta.version + " · " + T.app.footerNote;
    }
    if (meta.upstream) $("#ft-upstream").textContent = tpl(T.app.footerUpstream, { up: meta.upstream });
    fillMetaControls(meta);
    /* 刷新间隔由 /api/meta 决定（不硬编码）：拿到后重建定时器 */
    if (state.auto) setAuto(true, false);
    if (meta.write && meta.write.readonly_reason) state.writeReason = meta.write.readonly_reason;
  }).catch(function () {
    fillMetaControls(null);
  });
}

function init() {
  buildChrome();
  wireEvents();
  if (!location.hash) {
    try { history.replaceState(null, "", "#/overview"); } catch (e) { /* 忽略 */ }
  }
  setAuto(storeGet("fp.auto", "1") === "1", false);
  state.notif.seen = seenLoad();
  loadMeta().then(function () {
    applyWriteMode(false);
    applyRoute(true);
    loadNotifications(true);
    startNotifTimer();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

})();
