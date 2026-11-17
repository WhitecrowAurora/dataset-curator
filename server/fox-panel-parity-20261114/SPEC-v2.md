# fox-panel v2 设计与接口规格

> 状态：**可施工**（round 1 定稿）。本文档是后端与前端并行实现的唯一契约来源。
> 版本：v2.0 · 适用：`fox-panel/panel.py` v2、`fox-panel/web/{index.html,app.css,app.js}`
>
> 施工红线（任何一条不满足即视为未完成）
> 1. **纯标准库**：不新增第三方依赖，不引入构建步骤，不引 CDN 字体/图标/脚本。
> 2. **CSP 不变**：`default-src 'self'; script-src 'self'; style-src 'self'`，**无 `unsafe-inline`**。
>    ⇒ 无内联 `<script>`、无内联 `style="…"` 属性、无 `onclick=` 等内联事件处理器。
> 3. **零密钥外泄**：admin key / 会话签名密钥 / 密码哈希不得出现在 HTML、CSS、JS、日志、错误体里。
> 4. **既有 39 项测试全绿**：`python3 tests/test_panel.py` 必须仍然 39/39 通过（见 §6.6 兼容红线）。
> 5. 只读：没有任何写操作入口。

---

## 0. 施工边界与文件布局

| 位置 | 归属 | 内容 |
|---|---|---|
| `fox-panel/web/index.html` | 前端（结构） | 5 个页面骨架 + 抽屉 + toast 容器 + 占位符 `{{VERSION}}` / `{{UPSTREAM}}` |
| `fox-panel/web/login.html` | 前端（结构） | 登录页（从 `LOGIN_PAGE` 迁出），占位符 `{{ERROR}}` |
| `fox-panel/web/app.css` | 前端（样式） | §1 token + §2 组件 + §4 断点 |
| `fox-panel/web/app.js` | 前端（行为） | 路由、渲染、fetch、快捷键、无障碍 |
| `fox-panel/panel.py` | 后端 | 读 `web/` 目录下发静态文件；新增 `/api/package`、`/api/export.csv`、`/api/meta`；`/api/packages` 加 `sort/order` |
| `fox-panel/tests/test_panel.py` | 后端 | 保持 39 项通过；新增用例另开文件 `tests/test_v2.py`（不改动旧断言） |

服务端静态资源契约（后端实现，前端依赖）：

- 路径映射：`GET /static/app.css → web/app.css`，`GET /static/app.js → web/app.js`；
  `GET / → web/index.html`（替换 `{{VERSION}}`、`{{UPSTREAM}}`），`GET /login → web/login.html`。
- `Content-Type`：`.html → text/html; charset=utf-8`，`.css → text/css; charset=utf-8`，
  `.js → application/javascript; charset=utf-8`，`.svg → image/svg+xml`。
- 缓存：`Cache-Control: no-cache` + `ETag: "<mtime_ns>-<size>"`；命中 `If-None-Match` 返回 `304`（空体）。
- 读取失败（文件缺失/IO 错误）→ `503 {"error": "静态资源缺失：app.css"}`，不抛栈、不返回半截文件。
- `web/` 目录相对 `panel.py` 所在目录解析（测试是在原地跑 `panel.py`，不复制目录）。
- `HEAD` 对 `/`、`/login`、`/static/app.css`、`/static/app.js` 返回 200（保持 v1 行为）。

---

## 1. 视觉 token

### 1.1 主题与切换约定

- 主题挂在 `<html data-theme="dark|light">`，默认 `dark`；`localStorage` 键 `fp.theme`。
- `<meta name="color-scheme" content="dark light">`，两套 token 均声明 `color-scheme`，让滚动条/表单控件跟随。
- 切换按钮在页头右侧，`aria-pressed` 反映当前是否为亮色，文案 `亮色` / `暗色`。
- **所有颜色只能通过 token 取用**，组件 CSS 中不允许出现裸 hex（token 定义块除外）。
- 图标一律内联 SVG（`stroke="currentColor"`、`stroke-width="1.5"`、尺寸 16 或 20），不引外部图标字体。

### 1.2 暗色（默认）

```css
:root[data-theme="dark"] {
  color-scheme: dark;

  /* 背景 / 表面 */
  --fp-bg:              #0f1115;   /* 页面底色 */
  --fp-bg-subtle:       #12161d;   /* 表头、工具栏、页脚 */
  --fp-surface:         #161a22;   /* 卡片、表格、输入框 */
  --fp-surface-2:       #1b2130;   /* 次级块、进度条轨道、骨架屏 */
  --fp-surface-hover:   #1d2532;   /* 行/卡片悬停 */
  --fp-overlay:         rgba(0, 0, 0, .55);

  /* 边框 */
  --fp-border:          #242a36;   /* 分隔线（装饰性，不承载状态） */
  --fp-border-strong:   #2b3240;   /* 卡片悬停、分区块边界 */
  --fp-border-field:    #65728c;   /* 输入框/下拉/按钮描边（≥3:1，可判定控件边界） */

  /* 文本三级 */
  --fp-text-1:          #e6e9ef;   /* 正文、数值、表头 */
  --fp-text-2:          #b6c0cf;   /* 次要说明、次级列 */
  --fp-text-3:          #8b96a8;   /* 弱化说明、单位、时间戳 */

  /* 主色 */
  --fp-primary:         #4c8dff;
  --fp-primary-hover:   #6ba1ff;
  --fp-primary-fg:      #0b1220;   /* 主按钮上的文字 */
  --fp-primary-soft:    #16273f;   /* 选中导航项底色 */

  /* 语义四色 + 中性 */
  --fp-success:         #5fd08a;  --fp-success-bg: #12301f;
  --fp-warning:         #e3b341;  --fp-warning-bg: #2a2110;
  --fp-danger:          #ff7b72;  --fp-danger-bg:  #3a1618;
  --fp-info:            #58b6f0;  --fp-info-bg:    #12283a;
  --fp-neutral:         #b6c0cf;  --fp-neutral-bg: #1d232d;

  /* 焦点 */
  --fp-focus:           #4c8dff;

  /* 阴影 */
  --fp-shadow-1: 0 1px 2px rgba(0, 0, 0, .40);
  --fp-shadow-2: 0 4px 12px rgba(0, 0, 0, .45);
  --fp-shadow-3: -8px 0 24px rgba(0, 0, 0, .55);
}
```

### 1.3 亮色

```css
:root[data-theme="light"] {
  color-scheme: light;

  --fp-bg:              #f6f7f9;
  --fp-bg-subtle:       #eef1f5;
  --fp-surface:         #ffffff;
  --fp-surface-2:       #f0f2f6;
  --fp-surface-hover:   #eef1f6;
  --fp-overlay:         rgba(16, 24, 40, .40);

  --fp-border:          #dfe3ea;
  --fp-border-strong:   #c6ccd8;
  --fp-border-field:    #767f8f;

  --fp-text-1:          #16181d;
  --fp-text-2:          #424a59;
  --fp-text-3:          #646c7c;

  --fp-primary:         #1f6feb;
  --fp-primary-hover:   #1a5fd0;
  --fp-primary-fg:      #ffffff;
  --fp-primary-soft:    #e5f0ff;

  --fp-success:         #146c31;  --fp-success-bg: #e6f4ea;
  --fp-warning:         #8a5a00;  --fp-warning-bg: #fff4d6;
  --fp-danger:          #cf222e;  --fp-danger-bg:  #ffe9e9;
  --fp-info:            #0a5fc4;  --fp-info-bg:    #e5f0ff;
  --fp-neutral:         #424a59;  --fp-neutral-bg: #eef0f4;

  --fp-focus:           #1f6feb;

  --fp-shadow-1: 0 1px 2px rgba(16, 24, 40, .06);
  --fp-shadow-2: 0 4px 12px rgba(16, 24, 40, .10);
  --fp-shadow-3: -8px 0 24px rgba(16, 24, 40, .14);
}
```

### 1.4 字号阶梯 / 行高 / 字重

字体族（两套主题共用）：

```css
:root {
  --fp-font: -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
             "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  --fp-font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo,
                  Consolas, monospace;
}
```

| token | 字号 | 行高 | 字重 | 用途 |
|---|---|---|---|---|
| `--fp-fs-11` | 11px | 16px | 500 | 徽章文字、底部 tab 标签、`<th>` 小字 |
| `--fp-fs-12` | 12px | 18px | 400/600 | 辅助说明、单位、表格单元格（紧凑）、页脚 |
| `--fp-fs-13` | 13px | 20px | 400 | 表格正文默认、按钮、输入框 |
| `--fp-fs-14` | 14px | 22px | 400 | 页面正文、卡片标题、抽屉正文 |
| `--fp-fs-16` | 16px | 24px | 600 | 区块标题（h2）、抽屉标题 |
| `--fp-fs-20` | 20px | 28px | 600 | 页面标题（h1，仅在内容区顶部）、抽屉内大标题 |
| `--fp-fs-28` | 28px | 36px | 600 | 统计块数值（`font-variant-numeric: tabular-nums`） |

直接可抄的声明（含两套主题共用的尺度 token）：

```css
:root {
  --fp-fs-11: 11px; --fp-lh-11: 16px;
  --fp-fs-12: 12px; --fp-lh-12: 18px;
  --fp-fs-13: 13px; --fp-lh-13: 20px;
  --fp-fs-14: 14px; --fp-lh-14: 22px;
  --fp-fs-16: 16px; --fp-lh-16: 24px;
  --fp-fs-20: 20px; --fp-lh-20: 28px;
  --fp-fs-28: 28px; --fp-lh-28: 36px;

  --fp-sp-1: 4px;  --fp-sp-2: 8px;  --fp-sp-3: 12px; --fp-sp-4: 16px;
  --fp-sp-5: 20px; --fp-sp-6: 24px; --fp-sp-8: 32px; --fp-sp-10: 40px;
  --fp-sp-12: 48px;

  --fp-radius-sm: 4px; --fp-radius-md: 6px; --fp-radius-lg: 8px;
  --fp-radius-xl: 12px; --fp-radius-pill: 999px;

  --fp-dur-fast: 120ms; --fp-dur-base: 150ms; --fp-dur-panel: 240ms;
  --fp-ease-out: ease-out;
  --fp-ease-panel: cubic-bezier(.2, .7, .3, 1);

  --fp-header-h: 56px;      /* <768 覆盖为 48px */
  --fp-sidebar-w: 224px;
  --fp-tabbar-h: 56px;      /* 仅 <768 的底部 tab */
  --fp-tabs-h: 44px;        /* 仅 768–1199 的顶部页签 */
  --fp-drawer-w: 480px;     /* 768–1199 覆盖为 420px */

  /* 吸顶栈：第一层 = 页头（+页签），第二层 = 工具栏；表头贴在第二层下沿 */
  --fp-sticky-top: var(--fp-header-h);   /* 768–1199 覆盖为 calc(var(--fp-header-h) + var(--fp-tabs-h)) = 100px */
  --fp-toolbar-h: calc(var(--fp-sp-2) + var(--fp-lh-11) + 2px + var(--fp-field-h)
                       + var(--fp-sp-2) + 1px);   /* 单行工具栏；768–1199 两行覆盖为 125px；<768 卡片模式用不到 */

  --fp-z-header: 10; --fp-z-toolbar: 9; --fp-z-scrim: 20;
  --fp-z-drawer: 30; --fp-z-toast: 40; --fp-z-dialog: 50;
}
```

规则：
- 数字一律 `font-variant-numeric: tabular-nums`（统计值、大小、TTL、时间、分页计数）。
- 等宽字体只用于 `sha256`、`lease_id`、`device_id`、`repo/id` 组合。
- 正文最小 12px；11px 仅用于徽章与 tab 标签，且必须满足 §1.6 对比度。
- **吸顶栈三个 token 的推导**（只用它们算吸顶位置，不要写死 px）：
  - `--fp-sticky-top` = 吸顶栈第一层（页头，768–1199 再加 44px 页签）的底边 = **工具栏的吸顶位置**；
  - `--fp-toolbar-h` = 工具栏自身高度，随断点与行数变：**≥1200 单行 = 8+16+2+32+8+1 = 67px**，
    **768–1199 两行 = 2×(16+2+32)+8+16+1 = 125px**，审计页（右侧只有计数文字）另矮一档；
  - **表头吸顶位置 = `calc(var(--fp-sticky-top) + var(--fp-toolbar-h))`**（≥1200 为 123px，768–1199 为 225px）。
    `<768` 走卡片模式，两者都不参与吸顶。

### 1.5 间距 / 圆角 / 阴影 / 动效 / 层级

**间距阶梯（全部 4 的倍数）**：`4, 8, 12, 16, 20, 24, 32, 40, 48` px。

| 场景 | 值 |
|---|---|
| 图标与文字 | 8px |
| 徽章内边距 | 2px 8px |
| 按钮内边距 | 6px 12px（sm）/ 8px 16px（md） |
| 卡片内边距 | 16px |
| 表格单元格内边距 | 8px 12px（td）/ 8px 12px（th） |
| 表格行高 | th 36px、td 40px（紧凑档 td 36px） |
| 页面内容左右留白 | ≥1200: 32px；768–1199: 20px；<768: 12px |
| 页面区块之间 | 24px |
| 工具栏与表格之间 | 12px |
| 卡片网格间距 | 12px |

**圆角**：`--fp-radius-sm: 4px`（徽章内小元素、tag）、`--fp-radius-md: 6px`（按钮、输入框、下拉）、
`--fp-radius-lg: 8px`（表格容器）、`--fp-radius-xl: 12px`（卡片、抽屉内侧区块）、
`--fp-radius-pill: 999px`（徽章、状态 pill）。

**阴影**：仅卡片（`--fp-shadow-1`）、toast（`--fp-shadow-2`）、抽屉（`--fp-shadow-3`）。表格/工具栏不用阴影，用边框。

**动效**：

| 场景 | 时长 | 缓动 |
|---|---|---|
| 颜色/背景/描边悬停 | 120ms | `ease-out` |
| 导航选中条、徽章色变化 | 150ms | `ease-out` |
| 抽屉滑入/滑出、toast 进出 | 240ms | `cubic-bezier(.2, .7, .3, 1)` |
| 骨架屏微光 | 1400ms | `linear`（`infinite`） |
| 进度条数值变化 | 200ms | `ease-out` |

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .01ms !important;
    scroll-behavior: auto !important;
  }
  .fp-skeleton { animation: none; background: var(--fp-surface-2); }
}
```

**层级**：页头 `10`、工具栏 `9`、抽屉遮罩 `20`、抽屉 `30`、toast `40`、快捷键面板 `50`。

### 1.6 对比度实测（WCAG 2.1，前景/背景）

| # | 组合（前景 / 背景） | 暗色 | 亮色 | 门槛 | 用到的地方 |
|---|---|---|---|---|---|
| 1 | text-1 / surface | 14.33:1 | 17.76:1 | ≥4.5 | 正文、表格单元格、数值 |
| 2 | text-2 / surface | 9.49:1 | 8.92:1 | ≥4.5 | 次要说明、错误说明 |
| 3 | text-3 / surface | 5.83:1 | 5.28:1 | ≥4.5 | 弱化说明、统计块提示（11px） |
| 4 | text-1 / bg-subtle | 14.91:1 | 15.68:1 | ≥4.5 | 表头文字、页签、工具栏、**底部 tab 激活文字（现实现取法）** |
| 5 | text-2 / bg-subtle | 9.87:1 | 7.87:1 | ≥4.5 | 自动刷新开关、页脚说明 |
| 6 | text-3 / bg-subtle | 6.07:1 | 4.66:1 | ≥4.5 | 表格 `th`、底部 tab 未激活、字段标签 |
| 7 | text-3 / bg | 6.32:1 | 4.92:1 | ≥4.5 | 页面级弱化文字 |
| 8 | text-2 / bg | 10.28:1 | 8.32:1 | ≥4.5 | 分页信息、抽屉兜底值 |
| 9 | text-2 / surface-2 | 8.75:1 | 7.95:1 | ≥4.5 | 状态 pill |
| 10 | text-3 / surface-2 | 5.38:1 | 4.71:1 | ≥4.5 | 次级块内弱化文字 |
| 11 | text-2 / surface-hover | 8.39:1 | 7.87:1 | ≥4.5 | 悬停态说明文字 |
| 12 | text-1 / surface-hover | 12.67:1 | 15.69:1 | ≥4.5 | 悬停态行文本 |
| 13 | text-1 / primary-soft | 12.37:1 | 15.42:1 | ≥4.5 | 侧栏激活项文字 |
| 14 | primary / surface | 5.45:1 | 4.63:1 | ≥4.5 | 链接、包名按钮 |
| 15 | primary-hover / surface | 6.78:1 | 5.85:1 | ≥4.5 | 链接/包名悬停 |
| 16 | **primary-hover / bg-subtle** | 7.05:1 | **5.16:1** | ≥4.5 | **底部 tab 激活文字（11px 小字号；与第 4 行 text-1 二选一，禁用 primary）** |
| 17 | primary-fg / primary | 5.85:1 | 4.63:1 | ≥4.5 | 主按钮文字 |
| 18 | primary-fg / primary-hover | 7.28:1 | 5.85:1 | ≥4.5 | 主按钮悬停文字 |
| 19 | success / success-bg | 7.41:1 | 5.74:1 | ≥4.5 | 已完成徽章 |
| 20 | warning / warning-bg | 8.15:1 | 5.41:1 | ≥4.5 | 处理中/预锁徽章 |
| 21 | danger / danger-bg | 6.37:1 | 4.61:1 | ≥4.5 | 危险徽章、过期提示 |
| 22 | info / info-bg | 6.74:1 | 5.29:1 | ≥4.5 | 已下载/活跃租约徽章 |
| 23 | neutral / neutral-bg | 8.59:1 | 7.81:1 | ≥4.5 | 未认领徽章、中性 pill |
| 24 | success / surface | 9.03:1 | 6.52:1 | ≥4.5 | 统计块"正常"值 |
| 25 | warning / surface | 8.96:1 | 5.93:1 | ≥4.5 | 租约 TTL <300s |
| 26 | danger / surface | 6.91:1 | 5.36:1 | ≥4.5 | TTL <60s、异常统计值、错误图标旁文字 |
| 27 | warning / bg | 9.71:1 | 5.53:1 | ≥4.5 | `.fp-note` 提示条（截断/过期/过多仓库） |
| 28 | border-field / surface | 3.60:1 | 4.04:1 | ≥3.0 | 输入框/下拉/按钮描边（非文本） |
| 29 | focus / surface | 5.45:1 | 4.63:1 | ≥3.0 | 焦点框（非文本） |
| 30 | focus / bg | 5.90:1 | 4.32:1 | ≥3.0 | 焦点框贴页面底色（非文本） |
| 31 | primary / primary-soft | 4.70:1 | 4.02:1 | ≥3.0 | 侧栏激活项图标（**仅图标**，不得当文字用） |
| ✗ | primary / bg-subtle | 5.66:1 | **4.09:1** | 禁用 | **亮色不达标**：主色文字压 `--fp-bg-subtle` 的任何场景都不得使用 |

规则：

- 第 1–27 行是**文本对**，门槛 4.5:1，11px/12px 小字同样按 4.5 判；第 28–31 行是**非文本对**（控件边界、
  焦点框、纯图标），门槛 3:1。
- **亮色的 `primary / bg-subtle` 只有 4.09:1，是已知不合格组合**（暗色 5.66:1 合格，但两套主题请统一）。
  因此「主色文字压在 `--fp-bg-subtle` 上」一律改用 `--fp-primary-hover`（亮色 5.16:1）或 `--fp-text-1`
  （15.68:1），并只在**图标或 2px 上边框**上直接用 `--fp-primary`（非文本，4.02:1 ≥ 3:1）。
  典型落点：底部 tab 激活态（<768）、顶部页签激活态（768–1199）、表头内的排序强调。
- `--fp-border`（1.21:1 / 1.29:1）**仅作装饰分隔线**，不得单独承担"这里有控件/可点区域"的信息——
  控件边界一律用 `--fp-border-field`。
- 新增组合必须先加进这张表并跑 §7.4 第 4 条脚本；脚本读的就是 `web/app.css` 里的 token，表与实现同源。
- 脚本额外做一条**用法断言**：`.fp-tabbar__item[aria-current="page"]` 自身的 `color` 不得是 `var(--fp-primary)`
  （只允许 `var(--fp-primary-hover)` 或 `var(--fp-text-1)`）；子选择器 `… .fp-ic { color: var(--fp-primary) }`
  属于图标（非文本），不在限制内。这条断言就是为「亮色 4.09 字」这类回归设的闸。

---

## 2. 组件清单

命名规则：`fp-` 前缀 + 短横线；状态类用 `.is-*`，变体用 `.fp-x--y`。全部样式在 `web/app.css`，
结构由 `app.js` 用 `createElement`/`textContent` 生成（禁止拼 HTML 字符串，避免 XSS）。

### 2.1 卡片 `.fp-card`

```html
<section class="fp-card">
  <div class="fp-card__hd"><h2>仓库</h2><span class="fp-card__hint">共 3 个</span></div>
  <div class="fp-card__bd">…</div>
</section>
```

- 结构：`__hd`（可选，标题 16px/600 + 右侧 hint 12px/text-3）→ `__bd`。
- 尺寸：背景 `--fp-surface`，边框 `1px solid --fp-border`，圆角 12px，内边距 16px，阴影 `--fp-shadow-1`。
- 状态：`default`；`.is-interactive`（可点：hover 背景 `--fp-surface-hover` + 边框 `--fp-border-strong` + `translateY(-1px)`，120ms，光标 pointer，需有 `tabindex="0"` 与 Enter/Space 触发）；`.is-loading`（`__bd` 换成骨架块）；`.is-error`（边框/`__hd` 图标用 `--fp-danger`）。

### 2.2 统计块 `.fp-stat`

```html
<div class="fp-stat">
  <div class="fp-stat__k">总包数</div>
  <div class="fp-stat__v">12,480</div>
  <div class="fp-stat__h">3 个仓库</div>
</div>
```

- 结构：键（12px/text-3）→ 值（28px/600/text-1，tabular-nums）→ 提示（12px/text-3，可省略）。
- 尺寸：最小高 88px，内边距 16px，背景 `--fp-surface`，边框/圆角同卡片。
- 状态：`default`；`.is-loading`（值位置显示 64×28 骨架条）；`.is-alert`（值用 `--fp-danger`，用于"死包 > 0"）；`.is-good`（值用 `--fp-success`，用于"上游正常"类）；`.is-link`（整块可点，跳转到对应页，hover 同 `.fp-card.is-interactive`）。
- 验收：概览页必须恰好渲染 9 个 `.fp-stat`（总包数/未认领/处理中/已完成/active 租约/预锁/在线设备/可分配/总容量）。

### 2.3 徽章 `.fp-badge`（状态四色 + 信息色）

```html
<span class="fp-badge fp-badge--claimed"><span class="fp-badge__dot"></span>处理中</span>
```

- 结构：6px 圆点（`currentColor`）+ 文案。可选纯文本变体（无点）。
- 尺寸：字体 11px/16px，字重 500，内边距 `2px 8px`，圆角 999px，`white-space: nowrap`。
- 变体（背景 / 文字 / 用途）：
  | 类 | 暗色 | 亮色 | 用途 |
  |---|---|---|---|
  | `--available` | `--fp-neutral-bg` / `--fp-neutral` | 同左 | 包状态"未认领" |
  | `--claimed` | `--fp-warning-bg` / `--fp-warning` | 同左 | 包状态"处理中"、预锁租约 |
  | `--completed` | `--fp-success-bg` / `--fp-success` | 同左 | 包状态"已完成"、上游正常 |
  | `--danger` | `--fp-danger-bg` / `--fp-danger` | 同左 | 上游不可用、异常计数 >0 |
  | `--info` | `--fp-info-bg` / `--fp-info` | 同左 | active 租约、中性提示 |
- 状态：**只读、不可点击**（不可点的不做成徽章；可点请用 `.fp-btn--sm`）。无 hover、无 focus。
- 状态映射固定：`available→未认领/neutral`、`claimed→处理中/warning`、`completed→已完成/success`；未知状态值原样显示并用 neutral。

### 2.4 进度条 `.fp-progress`

```html
<progress class="fp-progress" value="42" max="100" aria-label="下载进度">42%</progress>
<progress class="fp-progress is-full" value="100" max="100" aria-label="下载进度">100%</progress>
<progress class="fp-progress is-idle" aria-label="下载进度（未知）"></progress>
```

- 用**原生 `<progress>`**，`appearance: none`；高度 6px，轨道 `--fp-surface-2`，圆角 999px；
  填充 `::-webkit-progress-value` / `::-moz-progress-bar` 用 `--fp-primary`。
- 状态：`default`（有值）；`.is-full`（100% → 填充 `--fp-success`）；`.is-idle`（无 `value` →
  不确定态，条纹动画，`prefers-reduced-motion` 下静止）；`.is-empty`（0% → 只显示轨道）。
- 旁边固定显示百分比文字（12px/text-2，tabular-nums）。
- **禁止**用 `style.width` 或 `style="width:42%"` 画进度（CSP 不允许内联样式；`<progress value>` 是唯一实现）。

### 2.5 工具栏 `.fp-toolbar`

```html
<div class="fp-toolbar" role="search">
  <div class="fp-toolbar__l">
    <label class="fp-field"><span>仓库</span><select id="f-repo">…</select></label>
    <label class="fp-field"><span>状态</span><select id="f-status">…</select></label>
    <label class="fp-field fp-field--grow"><span>搜索</span>
      <input id="f-q" type="search" placeholder="包名 / 认领人" maxlength="200"></label>
  </div>
  <div class="fp-toolbar__r">
    <button class="fp-btn fp-btn--sm" id="export">导出 CSV</button>
    <label class="fp-check"><input type="checkbox" id="auto" checked> 自动刷新</label>
    <button class="fp-btn fp-btn--primary" id="refresh">刷新</button>
  </div>
</div>
```

- 布局：`display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap`；
  吸顶（`position:sticky; top:var(--fp-sticky-top)`，**不是 `var(--fp-header-h)`**：768–1199 还要让出 44px 页签），
  背景 `--fp-bg-subtle`，`border-bottom: 1px solid --fp-border`，`z-index: 9`；**禁止用 `top:0` 顶替**（见 §2.6 吸顶约束）。
- 状态：`default`；`.is-busy`（按钮 `disabled`，`aria-busy="true"`，刷新按钮图标旋转）；`.is-stale`（上次刷新失败 → 左侧出现 `.fp-badge--danger` "数据可能过期"）。
- 输入框：高 32px（移动 40px）、边框 `1px solid --fp-border-field`、圆角 6px、背景 `--fp-surface`、
  `:focus-visible` 外描边 2px `--fp-focus` + 偏移 2px。

### 2.6 表格 `.fp-table`

```html
<div class="fp-tablewrap">
  <table class="fp-table">
    <caption class="fp-sr">包列表（按更新时间倒序）</caption>
    <thead><tr>
      <th scope="col" aria-sort="none">
        <button class="fp-sort" data-sort="id">包<span class="fp-sort__icon" aria-hidden="true"></span></button>
      </th>
      <th scope="col" aria-sort="descending">
        <button class="fp-sort" data-sort="updated" data-active="true">更新时间<span class="fp-sort__icon" aria-hidden="true"></span></button>
      </th>
    </tr></thead>
    <tbody>…</tbody>
  </table>
</div>
```

- 尺寸：`width:100%; border-collapse: separate; border-spacing:0`；外框 `1px solid --fp-border`、
  圆角 8px、`overflow:hidden`；`th` 高 36px、背景 `--fp-bg-subtle`、字体 11px/600、颜色 `--fp-text-3`、
  大写不强制（中文无大小写），`position: sticky; top: calc(var(--fp-sticky-top) + var(--fp-toolbar-h)); z-index: 2`
  （= 页头[+页签] + 工具栏；≥1200 为 123px，768–1199 为 225px）。
- **吸顶实现约束（两条，违反即为缺陷）**：
  1. **滚动容器必须是页面（`body`）**：表格容器保持 `overflow: visible`，不得 `overflow: auto/hidden`，
     更不得用 `max-height` 造内嵌滚动——那样会出现双滚动条，且容器一旦成为滚动口，横向滚动一出现表头吸顶就失效。
     *例外*：页签条自身的 `overflow-x: auto`（`.fp-tabs` 内部横向滚动）**不算**内嵌滚动容器，不要误判。
  2. **不得用 `top: 0` 顶替吸顶栈**：页头 `z-index: 10` 高于工具栏 `9`，`top:0` 会让工具栏滑到页头下面被盖住。
     工具栏必须是 `top: var(--fp-sticky-top)`，表头必须是 `calc(var(--fp-sticky-top) + var(--fp-toolbar-h))`。
     `<768` 走卡片模式，两者都不吸顶（无需该变量）。
- `td`：高 40px、13px/20px、`--fp-text-1`、`border-bottom: 1px solid --fp-border`；
  `tbody tr:hover td { background: var(--fp-surface-hover) }`；最后一列右对齐（数值列 `--align-right`）。
- 排序指示：`th[aria-sort="ascending|descending"]` 时 `.fp-sort__icon` 显示 `▲/▼`（SVG，12px，
  颜色 `--fp-primary`；非激活态用 `--fp-text-3` 且 40% 透明）；点击同一列在 `desc → asc → desc` 间切换
  （`updated/size` 默认 desc，`id` 默认 asc）。
- 允许的状态：
  - `default` 有数据；
  - `loading`：`tbody` 渲染 5 行 `.fp-skeleton` 行（每行高度 40px，各列一条骨架条）；
  - `empty`（整表无数据）：一行 `<tr class="fp-emptyrow"><td colspan="N">` 内放 `.fp-empty`，文案"还没有数据"+ 说明；
  - `empty-filtered`（筛选无结果）：同结构，文案"没有匹配的包"+ "清除筛选"按钮；
  - `error`：一行内放 `.fp-error` 紧凑版（文案 + 重试按钮）；
  - `truncated`：表格上方 `.fp-note`（12px/`--fp-warning`）"结果超过 200,000 行，仅对前 200,000 行排序"。
- 单元格内容规则：`null/undefined` → `-`（`--fp-text-3`）；超长文本 `max-width` + `text-overflow: ellipsis`
  且 `title` 属性给全文；`sha256` 显示前 12 位 + `…`，等宽字体，点击复制（P1）。
- 排序必须走服务端（`/api/packages?sort=&order=`），前端不得只排当前页。

### 2.7 右侧抽屉 `.fp-drawer`（单包详情）

```html
<div class="fp-scrim" id="scrim" hidden></div>
<aside class="fp-drawer" id="drawer" role="dialog" aria-modal="true"
       aria-labelledby="drawer-title" hidden>
  <header class="fp-drawer__hd">
    <h2 id="drawer-title">shard-00042.tar</h2>
    <button class="fp-iconbtn" id="drawer-close" aria-label="关闭详情">
      <svg …>…</svg>
    </button>
  </header>
  <div class="fp-drawer__bd">
    <dl class="fp-dl">
      <dt>仓库</dt><dd>t/one</dd>
      <dt>状态</dt><dd><span class="fp-badge fp-badge--claimed">处理中</span></dd>
      <dt>大小</dt><dd>1.2 GiB</dd>
      <dt>SHA-256</dt><dd class="fp-mono">9f2c1d4e8ab7…</dd>
    </dl>
  </div>
  <footer class="fp-drawer__ft">
    <button class="fp-btn">复制 SHA-256</button>
  </footer>
</aside>
```

- 尺寸：宽度 `var(--fp-drawer-w)`（≥1200 为 480px，768–1199 覆盖为 420px）；<768 为 `100vw` 且高度 `100dvh`（全屏，
  关闭按钮固定在右上，安全区 `padding-bottom: env(safe-area-inset-bottom)`）。
  高度 100dvh，`position: fixed; inset: 0 0 0 auto; z-index: 30; background: var(--fp-surface);`
  左边框 `1px solid --fp-border`，阴影 `--fp-shadow-3`。
- 结构：`__hd`（标题 16px/600 + 关闭按钮 32×32）→ `__bd`（`<dl>` 两列：标签 12px/text-3 宽 96px，
  值 13px/text-1）→ `__ft`（按钮组，可选）。
- 字段顺序（固定）：仓库 → 状态（徽章）→ 大小 → SHA-256 → 认领人 → 认领时间 → 完成时间 →
  下载时间（下载时间非空时徽章"已下载"）→ 更新时间 → 备注（`note`，多行，`white-space: pre-wrap`）→
  租约（有则显示：类型/设备/用户/剩余 TTL/进度条/最近心跳）。
- 允许的状态：`hidden`；`open`（进入动画 240ms，`transform: translateX(100%) → 0`）；
  `loading`（`__bd` 内 6 行骨架，标题区显示"加载中…"）；`error`（`__bd` 内 `.fp-error` 紧凑版 +
  "重试"按钮）；`404`（文案"包不存在或已被删除"，无重试）。
- 交互：点遮罩关闭、`Esc` 关闭、打开时 `main`/`nav`/`header` 加 `inert`、焦点移入抽屉首个可聚焦元素、
  关闭后焦点回到触发按钮；`document.body` 加 `overflow: hidden`（`<768` 时无滚动穿透）。
- **不允许**出现任何写操作按钮（打回/释放/踢设备/改备注）。

### 2.8 骨架屏 `.fp-skeleton`

- 结构：`<span class="fp-skeleton" aria-hidden="true"></span>` 或 `--text` / `--stat` / `--row` 变体。
- 尺寸：文本条高 12px、宽 100%；统计条 64×28px；表格行条高 12px（每列宽度按列 `%` 分配）。
- 样式：背景 `--fp-surface-2`、圆角 4px、`@keyframes fp-shimmer`（渐变 `--fp-surface-2` →
  `--fp-surface` → `--fp-surface-2`，`background-size: 200% 100%`，位移 `-100% → 100%`，1400ms linear infinite）。
- 状态：仅 `default`；`prefers-reduced-motion` 下静态。
- 规则：骨架块数 = 目标内容的行数（表格 5 行、统计块 1 条、抽屉 6 行），且必须带
  `aria-busy="true"` 在容器上，骨架本身 `aria-hidden="true"`。

### 2.9 空状态 `.fp-empty`

```html
<div class="fp-empty">
  <svg class="fp-empty__ic" …>…</svg>
  <p class="fp-empty__t">没有匹配的包</p>
  <p class="fp-empty__h">试试清空搜索词或切换仓库</p>
  <button class="fp-btn fp-btn--sm">清除筛选</button>
</div>
```

- 结构：图标（32px，`--fp-text-3`）→ 标题（14px/600）→ 说明（12px/text-3）→ 可选操作按钮。
- 尺寸：垂直内边距 40px，水平居中，最大宽 360px。
- 状态：`default`；`.is-filtered`（有筛选条件时的文案"没有匹配的…"+ 清除筛选）。
- 两种文案必须区分："还没有数据"（首次/空库）与"没有匹配的包"（筛选后为空），验收靠 DOM 文本。

### 2.10 错误态 `.fp-error`

```html
<div class="fp-error" role="alert">
  <svg class="fp-error__ic" …>…</svg>
  <div>
    <p class="fp-error__t">上游不可用</p>
    <p class="fp-error__h">连接 fox-label 失败：上游不可达 [Errno 111]</p>
    <button class="fp-btn fp-btn--sm">重试</button>
    <details class="fp-error__d"><summary>技术详情</summary><pre>GET /api/summary → 503</pre></details>
  </div>
</div>
```

- 结构：图标（20px danger）→ 标题（14px/600）→ 说明（12px/text-2，来自 `error` 字段原文）→ 重试按钮 → 折叠详情。
- 尺寸：内边距 16px，边框 `1px solid --fp-danger-bg`，圆角 8px，背景 `--fp-danger-bg` 的 40% 混合色
  （实现：`background: color-mix(in srgb, var(--fp-danger-bg) 40%, var(--fp-surface))`，不支持时退化为 `--fp-surface`）。
- 状态：`default`（区块内）；`.fp-error--inline`（表格行内，无图标、无折叠详情）。
- 说明文字**必须原样展示服务端 `{"error": "..."}` 的字符串**，不得只显示"出错了"。

### 2.11 Toast `.fp-toast`

```html
<div class="fp-toasts" id="toasts" aria-live="polite" aria-atomic="false"></div>
<div class="fp-toast fp-toast--success" role="status">
  <span class="fp-toast__dot"></span><span class="fp-toast__msg">已复制 SHA-256</span>
  <button class="fp-iconbtn fp-toast__x" aria-label="关闭提示">×</button>
</div>
```

- 位置：≥768 右下（`right:24px; bottom:24px`），<768 底部 tab 上方（`bottom: calc(56px + 12px)`），
  宽 320px（移动 `calc(100vw - 24px)`），背景 `--fp-surface`、边框 `--fp-border`、阴影 `--fp-shadow-2`。
- 变体：`--info`（`--fp-info` 点）、`--success`（`--fp-success` 点）、`--danger`（`--fp-danger` 点，`role="alert"`）。
- 状态：`entering`（240ms 上滑+淡入）、`visible`、`leaving`（180ms 淡出）。自动消失：info/success 4000ms，
  danger 8000ms；鼠标悬停或聚焦时暂停计时；最多同时 3 条（超出丢弃最旧）。

---

## 3. 信息架构与导航

### 3.1 导航形态

| 断点 | 形态 | 细节 |
|---|---|---|
| ≥1200 | **左侧边栏**（固定 224px 宽，`position: sticky; top:0; height:100dvh`） | 品牌区（"fox-label 面板"，高 56px）+ 5 个导航项（高 40px，图标 20px + 14px 文案）+ 底部版本号与上游状态 pill |
| 768–1199 | **顶部横向页签**（页头下方，高 44px） | 5 项等距左对齐，激活项 `border-bottom: 2px solid --fp-primary` |
| <768 | **底部 tab 栏**（高 56px + 安全区） | 5 项等分，图标 20px + 11px 文案；顶部不显示页签 |

- 导航项固定顺序与 hash：`#/overview` 概览、`#/packages` 包列表、`#/devices` 注册设备表、
  `#/anomalies` 异常体检、`#/audit` 审计。
- 激活项：`aria-current="page"`，文字 `--fp-text-1`，背景 `--fp-primary-soft`（侧栏）/ 下边框主色（顶部）/
  图标+文字主色（底部）；非激活文字 `--fp-text-3`。
- 页头（所有断点）：左侧页面标题（20px/600）+ 右侧"上游状态 pill + 主题切换 + 自动刷新开关 + 刷新 + 登出"。
  高度 56px，`position: sticky; top:0; z-index:10`，背景 `--fp-bg`，底边框 `--fp-border`。
- 首个可聚焦元素是"跳到主内容"链接（`.fp-skip`，聚焦时可见）。

### 3.2 五个页面

#### (1) 概览 `#/overview`

- 统计块 9 个（2.2 节）：总包数 / 未认领 / 处理中 / 已完成 / active 租约 / 预锁 / 在线设备 / 可分配 / 总容量。
  字段来源：`/api/summary` 的 `totals.{packages,available,claimed,completed,active_leases,prelock_leases,devices_online,free_for_assign,bytes_total}`。
- 仓库表：`仓库 | 类型 | 启用 | 总数 | 未认领 | 处理中 | 已完成 | 容量`。
  来源 `summary.repos[]`：`repo`、`type`、`enabled`、`summary.{total,available,claimed,completed,bytes_total}`。
- 默认排序：`repo` 升序（客户端排序）。分页：不分页（仓库数 ≤200 全展示，>200 提示"仓库过多，请到包列表筛选"）。
- 页脚：`fox-panel v{{VERSION}} · 只读面板 · 数据来自 {{UPSTREAM}} · 更新时间 <ts>`。

#### (2) 包列表 `#/packages`

- 列（顺序固定）：`仓库 | 包 | 大小 | 状态 | 认领人 | 已下载 | 更新时间`。
  字段：`repo,id,size,status,owner,downloaded_at,updated_at`。
- 工具栏：仓库下拉（来自 `/api/meta.repos`）、状态下拉（`/api/meta.statuses`）、搜索框（匹配 `id` 或 `owner`，
  大小写不敏感，面板侧过滤）、分页信息（"第 2/13 页 · 共 631 个"）、上一页/下一页、导出 CSV。
- 默认排序：`sort=updated&order=desc`。可排序列：`updated`（更新时间）、`size`（大小）、`id`（包名）。
- 默认分页大小 **50**；可选 `25 / 50 / 100 / 200`（下拉），选择记入 `localStorage["fp.size"]`。
- 交互：点包名 → 打开单包详情抽屉。

#### (3) 注册设备表 `#/devices`

- 设备表：`设备 | 用户 | 档位 | 速度(张/秒) | 状态 | 最近心跳 | 版本`。
  字段：`device_id,user_id,tier,speed,status,last_seen,client_ver`（来自 `/api/summary.devices[]`）。
  默认排序：`last_seen` 降序（客户端）。分页：>50 行时客户端分页，默认 50。
- 租约表：`类型 | 包 | 用户 | 设备 | 剩余 TTL | 进度 | 心跳`。
  字段：`leases[].{kind,repo,package,user_id,device_id,expires_in,progress,last_beat_ago}`；
  `kind=active` → "处理中"徽章（info），`kind=prelock` → "预锁"徽章（warning）。
  默认排序：`expires_in` 升序（最紧急在前）。分页：>50 行时客户端分页，默认 50。
- 进度：`progress` 是对象（如 `{"done":12,"total":100}`），渲染为 `<progress value=done max=total>` +
  `"12%"`；若对象里是 `{"percent": 42}` 则直接用；无法解析时显示 `-`。空对象 → `.is-idle`。
- 剩余 TTL：`<60s` 用 `--fp-danger` 色，`<300s` 用 `--fp-warning`，其余 `--fp-text-2`。

#### (4) 异常体检 `#/anomalies`

- 三个统计块：死包 / 孤儿租约 / 幽灵租约；>0 时 `.is-alert`。
- 三张表：
  - 死包：`仓库 | 包 | 认领人`（`stuck[].{repo,package,owner}`），默认 `repo` 升序；
  - 孤儿租约：`租约 | 包 | 设备`（`orphan[].{lease_id,repo,package}`），默认 `lease_id` 升序；
  - 幽灵租约：`租约 | 设备`（`ghost[].{lease_id,device_id}`），默认 `lease_id` 升序。
- 分页：每表客户端分页，默认 50；三个统计块显示总数。
- 交互（P1）：点死包行 → 打开对应包的抽屉。
- 全部为 0 时显示 `.fp-empty` "体检通过：没有发现异常"。

#### (5) 审计 `#/audit`

- 列：`时间 | 动作 | 仓库 | 包 | 操作者 | 详情`（`ts,kind,repo,pkg,actor,detail`）。
- 默认排序：`ts` 降序（最新在上——注意 v1 后端返回的是文件顺序，前端 `.reverse()` 后渲染）。
- 默认分页大小 **25**（后端 `tail=25`）；P1 支持 `?tail=50|100`。
- 未启用审计（`enabled:false`）→ `.fp-empty` "未配置审计文件（--audit-file）"，不显示表格。
- `detail` 超 60 字符截断 + `title` 全文（P1 支持点击展开）。

### 3.3 单包详情抽屉

- 触发：包列表的包名按钮、异常体检的死包行（P1）、URL `#/package?repo=<repo>&id=<id>`（P1 深链）。
- 数据：`GET /api/package?repo=&id=`（§6.2）。打开先渲染骨架，成功替换；失败按错误态处理。
- 字段与顺序见 §2.7。抽屉不涉及任何写操作。

### 3.4 状态保持

- `localStorage` 键：`fp.theme`（`dark|light`）、`fp.auto`（`1|0`，默认 `1`）、`fp.size`（默认 `50`）、
  `fp.cols`（列显隐，P1）。任何键读取失败（隐私模式）时用默认值，不报错。
- URL hash 保存 `repo/status/q/sort/order/page`（P1 深链）：格式 `#/packages?repo=t%2Fone&status=claimed&page=2`。
- 切页不清空筛选（每页各自记忆，属 P2 可选）。

---

## 4. 响应式断点

三个档位：**≥1200（桌面）**、**768–1199（紧凑）**、**<768（移动）**。
断点用 `min-width` 写（CSS 从移动优先向上叠加），与上表一致。

| 项目 | ≥1200 | 768–1199 | <768 |
|---|---|---|---|
| 根字号/正文 | 14px | 14px | 13px |
| 导航 | 左侧边栏 `--fp-sidebar-w: 224px` 固定，内容区左偏移 224px | 顶部横向页签 44px，页头下吸顶 | 底部 tab 56px，`main` 底部留白 `calc(56px + 16px + env(safe-area-inset-bottom))` |
| 页头 | `--fp-header-h: 56px`，标题 + 全部动作 | 56px，隐藏"主题切换"文案只留图标 | `--fp-header-h: 48px`，只留标题 + 刷新/更多（`⋯` 菜单放主题/登出） |
| 统计块网格 | `repeat(auto-fill, minmax(176px, 1fr))`，单行 4–6 个 | `repeat(auto-fill, minmax(150px, 1fr))` | `repeat(2, 1fr)`，高 80px |
| 表格 | 展示全部列；表头 sticky；`td` 40px | 隐藏低优先列（`.fp-col-md`，表格下方"列"按钮可开），保留关键 5 列；**不退回横向滚动**（表格容器 `overflow: visible`，理由见 §2.6 吸顶约束） | **转卡片列表**：每行一张 `.fp-card`，内部 `<dl>` 2 列（标签 88px 左、值右对齐），包名当卡片标题、状态徽章在右上；不出现横向滚动 |
| 工具栏 | 单行，左筛选右动作 | 两行：筛选一行、动作一行右对齐 | 筛选控件各占整行；"导出 CSV"收进 `⋯` 菜单 |
| 抽屉 | `--fp-drawer-w: 480px` 右侧 | 420px 右侧 | **全屏**（100vw × 100dvh），关闭按钮固定右上，底部按钮吸底 |
| 卡片内边距 | 16px | 16px | 12px |
| 页面留白 | 32px | 20px | 12px |
| Toast | 右下 320px | 右下 320px | 底部 tab 上方，宽 `calc(100vw - 24px)` |
| 可点目标 | ≥32×32px | ≥32×32px | ≥44×44px |
| 表格转卡片时隐藏的顺序 | — | 先 `已下载` → `更新时间` → `大小` | 卡片内全部保留，字段名简写 |

表格转卡片的实现约束：同一份 DOM 数据渲染两次代价高 ⇒ 用 CSS 实现（`.fp-table` 在 `<768` 时
`thead{display:none}`、`tr{display:block}`、`td{display:flex; justify-content:space-between}` 并用
`td::before { content: attr(data-label) }` 显示字段名）——`data-label` 由 `app.js` 渲染时写入，
**不用内联样式**。若实现成本过高，允许在 `<768` 用 JS 渲染成 `<ul class="fp-cardlist">`，但必须保证
两种断点下字段一致、且切换断点时（`resize` 监听 + `matchMedia`）不丢失当前筛选。

---

## 5. 功能分级（P0 / P1 / P2）

P0 = 本轮必交付；P1 = 本轮尽量交付，不做不影响 P0 验收；P2 = 明确推迟。
每条都给"能被别人用命令或肉眼判定通过/不通过"的验收口径。

### P0（必须）

| 编号 | 功能 | 验收口径 |
|---|---|---|
| P0-1 | 五页导航（侧栏/顶部页签/底部 tab 三形态） | 浏览器宽 1400px 时侧栏可见且顶部无页签；宽 390px 时底部 tab 可见；依次点 5 项，`location.hash` 分别为 `#/overview`…`#/audit`，且对应项 `aria-current="page"`。 |
| P0-2 | 概览 9 个统计块 + 仓库表 | 假上游（6 个包）下"总包数"显示 `6`、"未认领"显示 `5`，页面恰好 9 个 `.fp-stat`，仓库表有 1 行且"未认领"列 = 5。 |
| P0-3 | 包列表筛选（repo/status/q）+ 分页 | 选仓库 `t/one` 后请求 URL 含 `repo=t%2Fone`，表格每行仓库列均为 `t/one`；搜索 `bob` 后只剩认领人含 bob 的行且分页信息总数变为查询结果数；点"下一页"请求 `page=2` 且表格内容变化。 |
| P0-4 | 服务端排序（`sort`/`order`） | 点"大小"表头后该 `th` 的 `aria-sort="descending"`，首行 size 等于当前结果集最大值；再点一次变 `ascending` 且首行 size 为最小值；刷新页面后排序回默认（`updated desc`）。 |
| P0-5 | 单包详情抽屉 | 点包名后抽屉可见且 `role="dialog"`，内部文本包含该包 `sha256` 与拥有者 `owner`；按 `Esc` 关闭且焦点回到刚才点击的包名按钮；抽屉内不存在任何"打回/释放/踢设备"按钮。 |
| P0-6 | `/api/meta` 驱动筛选器与页脚 | `curl -b cookie /api/meta` 返回 200 且 `statuses` 长度 3、`sorts` 含 `updated|size|id`；页面仓库下拉项数 = `meta.repos` 长度；页脚版本号与 `meta.version` 一致。 |
| P0-7 | CSV 导出 | 点"导出 CSV"下载文件名匹配 `^fox-packages-\d{8}-\d{6}\.csv$`；`head -c3` 输出 BOM；`head -1` 恰为 `repo,id,size,sha256,status,owner,claimed_at,completed_at,downloaded_at,updated_at`（10 列、顺序固定）；导出内容与当前筛选一致（行数 = 分页信息中的总数）。 |
| P0-8 | 暗/亮主题切换并持久化 | 点主题按钮后 `<html data-theme="light">`，`localStorage.fp.theme === "light"`；刷新后仍是亮色；用 §7.4 脚本检查两套主题所有文本对比度 ≥4.5:1。 |
| P0-9 | 自动刷新 + 手动刷新 + 状态播报 | 勾选"自动刷新"后 15 秒内出现第二次 `/api/packages` 请求；每次成功刷新后 `#fp-live` 文本更新为"已更新 HH:MM:SS"且当前焦点不跳走；取消勾选后 30 秒内无新请求。 |
| P0-10 | 骨架屏 / 空态 / 错误态 | 首次进入包列表时先出现 5 行骨架（`aria-busy="true"`），数据到达后消失；搜索一个不存在的词后显示"没有匹配的包"；停掉上游后页面显示"上游不可用"（含服务端 error 原文）与"重试"按钮，点击重试后仍失败也不崩。 |
| P0-11 | 键盘可达 + focus-visible | 只用键盘：`Tab` 到包名 → `Enter` 打开抽屉 → `Tab` 到"复制 SHA-256" → `Enter` → `Esc` 关闭，全程无需鼠标；每一步焦点元素有 2px 主色描边（截图像素或 CSS 检查 `:focus-visible` 规则存在）。 |
| P0-12 | 无内联脚本/样式，CSP 不变 | `grep -n 'style="' web/*.html web/*.css web/*.js` 无输出；`grep -n 'on[a-z]*=' web/index.html` 只有 `input` 的 `type=`/`name=`（无 `onclick/onchange`）；`curl -sI http://127.0.0.1:31832/` 的 CSP 含 `script-src 'self'` 且不含 `unsafe-inline`。 |

### P1（本轮尽量）

| 编号 | 功能 | 验收口径 |
|---|---|---|
| P1-1 | 租约 TTL 本地插值 | 停在"注册设备表"页，某租约显示 `58s`，60 秒后（未刷新）变成约 `-1s`→显示 `0s` 或 `已过期`，且期间 Network 面板无 `/api/summary` 请求。 |
| P1-2 | 异常行 → 抽屉 | 点击死包表行，抽屉打开且标题等于该行"包"列文本，内部"仓库"值等于该行"仓库"列。 |
| P1-3 | 审计关键字过滤 | 审计页输入 `release` 后表格只剩 kind/detail 含该词的行，且表头显示 `3 / 25`；清空后恢复 25 行。 |
| P1-4 | 复制 SHA-256 / 复制深链 | 点"复制 SHA-256"后 toast 显示"已复制"，`navigator.clipboard.readText()` 等于抽屉里的完整 sha256；非安全上下文（http）下退化为选中文本 + toast "已选中，请按 Ctrl+C"。 |
| P1-5 | 主题跟随系统 | 清空 `localStorage.fp.theme` 后，把系统外观从暗切到亮，页面在 1 秒内变为亮色（`matchMedia` 监听），无需刷新。 |
| P1-6 | URL 深链 | 打开 `#/packages?repo=t%2Fone&status=claimed&sort=size&order=asc&page=1` 后，筛选器与排序指示与 URL 一致；改筛选后 URL 同步更新；刷新页面状态不丢。 |
| P1-7 | 快捷键 | 焦点在正文（非输入框）时按 `r` 触发一次刷新、按 `/` 聚焦搜索框、按 `1`–`5` 切页、按 `?` 打开快捷键面板（`role="dialog"`，`Esc` 关闭）；焦点在输入框内按上述键不触发。 |
| P1-8 | 列显隐记忆 | 隐藏"已下载"列后刷新页面仍隐藏（`localStorage.fp.cols`）；导出 CSV 不受列显隐影响（始终 10 列）。 |
| P1-9 | 导出前提示行数 | 当前筛选结果 >5000 行时，"导出 CSV"按钮显示行数（如"导出 CSV · 12,480"），点击先弹确认框，确认后才开始下载。 |
| P1-10 | 上游状态 pill 含耗时 | 页头 pill 文本匹配 `^上游正常 · \d+ms$`；上游挂掉后 15 秒内变 `上游不可用`（danger 色）且工具栏出现"数据可能过期"。 |

### P2（本轮不做）

| 编号 | 功能 | 说明 |
|---|---|---|
| P2-1 | 虚拟滚动 | >1000 行时性能优化，当前 200000 行上限下先只做提示 |
| P2-2 | 列宽拖拽 | 需持久化布局，价值低 |
| P2-3 | 容量/趋势图表 | 无历史数据源，需服务端新表，另开一轮 |
| P2-4 | `prefers-contrast` 高对比主题 | 两套主题已满足 AA |
| P2-5 | 打印样式表 | 面板不面向打印 |
| P2-6 | 表格内 roving tabindex / 行内单元格导航 | P0/P1 用按钮与抽屉已可键盘完成主要任务 |

---

## 6. 只读接口契约

### 6.0 通用约定

- 全部为 `GET`（除既有 `/login`、`/logout` 的 POST）。`/api/*` 一律要求会话 Cookie `fp_sid`；
  未登录 → `401 {"error": "未登录"}`（JSON，**不是** 302）。页面路径未登录 → 302 到 `/login`（保持 v1）。
- 响应头：`Content-Type: application/json; charset=utf-8`、`Cache-Control: no-store`、CSP 等安全头（§7.1）。
- 字段命名 `snake_case`，与上游一致；时间戳是上游原样的本地 ISO 8601 字符串（如 `2026-11-10T20:15:14+08:00`）；
  例外：`devices[].last_seen`、`leases[].last_beat_at` 是 Unix 秒（浮点），前端 `fmtTime` 需兼容两种。
- JSON 顶层成功响应含 `"ok": true`。既有接口的**已有字段与语义**不变（`/api/summary` 的 `ok`/`ts`/`totals`/
  `repos`/`devices`/`leases`、`/api/anomalies` 的三类数组、`/api/audit` 的 `enabled`/`lines`）；
  `/api/packages` 在保留 `packages[]` 上游字段的前提下**新增** `ok`/`generated_at`/`sort`/`page.{size,total,pages,truncated}`
  （只做加法，不改已有键的含义）。
- 所有错误响应体**只有一个键** `{"error": "<人类可读字符串>"}`，不加 `code`/`detail`/`trace`；
  字符串里不得包含 admin key、文件绝对路径中的用户名、Python 栈信息。
- 参数解析失败（`int()` 失败、枚举越界）→ `400`；上游故障（`UpstreamError`）→ `503`，
  文案必须以"上游"开头（如 `上游不可达：…`、`上游返回 500`），保证既有测试断言可读。
- 数字参数一律 clamp：`page = max(1, n)`、`size = min(200, max(1, n))`；非法枚举（`sort/order`）→ 400 而不是静默回退。

### 6.1 `GET /api/meta` —— 前端启动所需的一切配置

请求参数：无。

成功 `200`：

```json
{
  "ok": true,
  "version": "2.0.0",
  "upstream": "http://127.0.0.1:31831",
  "upstream_ok": true,
  "upstream_error": null,
  "generated_at": "2026-11-10T20:15:14+08:00",
  "refresh_ms": 15000,
  "default_page_size": 50,
  "page_sizes": [25, 50, 100, 200],
  "audit_page_size": 25,
  "export_max_rows": 100000,
  "statuses": [
    {"value": "available", "label": "未认领"},
    {"value": "claimed",   "label": "处理中"},
    {"value": "completed", "label": "已完成"}
  ],
  "sorts": [
    {"value": "updated", "label": "更新时间", "default_order": "desc"},
    {"value": "size",    "label": "大小",     "default_order": "desc"},
    {"value": "id",      "label": "包名",     "default_order": "asc"}
  ],
  "repos": [{"repo": "t/one", "type": "dataset", "enabled": true}],
  "features": {"drawer": true, "export": true, "sort": true, "theme": true},
  "audit": {"enabled": false}
}
```

字段语义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | str | 面板版本，等于 `panel.py` 的 `VERSION`；页脚显示它 |
| `upstream` | str | 上游 base URL（**不含密钥**），页脚展示 |
| `upstream_ok` | bool | 本次拉 `/repos` 是否成功；失败不影响 200 |
| `upstream_error` | str\|null | `upstream_ok=false` 时的错误原文（"上游…"开头） |
| `generated_at` | str | 生成时刻，ISO 8601 带偏移 |
| `refresh_ms` | int | 自动刷新间隔（15000），前端不得硬编码 |
| `page_sizes` | int[] | 包列表可选分页大小，升序 |
| `default_page_size` | int | 50 |
| `audit_page_size` | int | 25 |
| `export_max_rows` | int | 100000，CSV 行数上限 |
| `statuses` | obj[] | `value` 用于 `?status=`，`label` 用于下拉文案 |
| `sorts` | obj[] | `value` 用于 `?sort=`，`default_order` 决定首次点击方向 |
| `repos` | obj[] | 启用中的仓库；`enabled` 恒为 true，保留字段供未来停用展示 |
| `features` | obj | 前端据此隐藏不可用按钮（本轮全 true） |
| `audit` | obj | `enabled` 反映是否配置了审计文件 |

错误：`401`（未登录）、`503`（**不会**出现：上游失败时降级为 `upstream_ok=false` 的 200，避免整页不可用）。

### 6.2 `GET /api/package?repo=&id=` —— 单包详情

请求参数：

| 参数 | 必填 | 类型 | 规则 |
|---|---|---|---|
| `id` | 是 | str | 包名，1–512 字符；空或缺失 → `400 {"error": "缺少参数 id"}`；超过 512 字符 → `400 {"error": "参数 id 过长（最多 512 字符）"}` |
| `repo` | 否 | str | 仓库名。缺省时由上游按 `id` 反查；命中多个仓库 → 见下方 400 |

成功后端行为：`GET {upstream}/packages/<urlencode(id)>?repo=<urlencode(repo)>`，
带 `Authorization: Bearer <admin key>`（key 只在进程内使用）。

成功 `200`：

```json
{
  "ok": true,
  "generated_at": "2026-11-10T20:15:14+08:00",
  "package": {
    "repo": "t/one",
    "id": "shard-00042.tar",
    "size": 1288490188,
    "sha256": "9f2c1d4e8ab7c0f5a1b2c3d4e5f60718293a4b5c6d7e8f9012345678abcdef01",
    "status": "claimed",
    "status_label": "处理中",
    "owner": "bob",
    "claimed_at": "2026-11-10T10:00:00+08:00",
    "completed_at": null,
    "downloaded_at": null,
    "note": null,
    "updated_at": "2026-11-10T10:00:00+08:00",
    "lease": null
  }
}
```

`package` 对象**恰好 13 个键**：上面 12 个固定字段 + `lease`，不多不少（上游行里多出的列
（`lease_device`、`prelock_*`、`progress` 等）**不得**外泄，实现按白名单收敛）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `package.repo` / `package.id` | str | 主键 |
| `package.size` | int | 字节数；0 表示未知/空文件 |
| `package.sha256` | str\|null | 64 位小写 hex；`null` 时前端显示 `-` |
| `package.status` | str | `available\|claimed\|completed` |
| `package.status_label` | str | 中文标签，固定用 §2.3 词表（`未认领`/`处理中`/`已完成`），**不采用上游 `/packages/<id>` 的文案**（上游把 claimed 写成"已认领"，与本面板不一致） |
| `package.owner` | str\|null | 认领人 |
| `package.claimed_at` / `completed_at` / `downloaded_at` / `updated_at` | str\|null | ISO 8601 |
| `package.note` | str\|null | 备注，可能含换行 |
| `package.lease` | obj\|null | 该包当前租约摘要；**无租约时 `null`**。子字段：`lease_id`(str)、`kind`(`"active"`\|`"prelock"`)、`device_id`(str)、`user_id`(str\|null)、`expires_in`(int，剩余秒数，可为负)、`last_beat_at`(number\|null，Unix 秒)、`progress`(obj，可能是 `{}`)。上游 `/api/v1/work` 取不到时同样返回 `null`，**详情不因此降级为 503**。 |

错误：

| 状态码 | 触发条件 | 响应体 |
|---|---|---|
| `400` | `id` 为空；或省略 `repo` 且同名包存在于多个仓库（上游 409，或 5xx 经扫描回退还原） | `{"error": "缺少参数 id"}` / `{"error": "包名在多个仓库存在，请指定 repo：a/x, b/x"}` |
| `401` | 无有效会话 | `{"error": "未登录"}` |
| `404` | 上游 404（包不存在）；或上游 403（仓库停用/无权）也归为 404 以免暴露仓库存在性 | `{"error": "包不存在：t/one/shard-00042.tar"}` |
| `503` | 上游不可达 / 超时；**指定了 `repo`** 时上游返回 5xx/401；密钥读取失败 | `{"error": "上游不可达：…"}` |
| `404` | 契约外路径 | `{"error": "not found"}` |

**未指定 `repo` 时上游 5xx 不回 503**（F3 的正式语义）：真实 fox-label 会把"同一包名存在于多个仓库"的
`Conflict` 落成通用 `500`，所以面板对**无 repo 的 5xx** 会退化为全量扫描，把歧义还原成正确状态——
该路径更常见的终态是 `400`（`包名在多个仓库存在，请指定 repo`）或 `404`（`包不存在`）。
`503` 只保留给三种情况：上游整体不可用（连不上/超时）、**指定了 `repo`** 时的上游 5xx/401、密钥读取失败。

### 6.3 `GET /api/export.csv?repo=&status=&q=&sort=&order=` —— CSV 导出

请求参数：与 §6.4 的 `/api/packages` **完全同义**（`repo`、`status`、`q`、`sort`、`order`），
但忽略 `page`/`size`（总是导出整个筛选结果）。`sort`/`order` **缺省**时按 §6.4 的默认值（`updated desc`）；
**显式**传入枚举外的值 → `400 {"error": …}` 的 **JSON** 响应（不是回落默认值，也不是 CSV）。

成功 `200` 响应头：

```
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="fox-packages-20261110-201514.csv"
Cache-Control: no-store
X-Total-Rows: 631
X-Truncated: 0
```

- 文件名：`fox-packages-<YYYYmmdd-HHMMSS>.csv`，时间取服务器本地时区。
- `X-Total-Rows` = 本次实际写出的数据行数（不含表头）；超过 `export_max_rows` 时只写前 100000 行并置
  `X-Truncated: 1`。

正文格式（逐字节约定）：

1. **BOM**：正文前三字节必须是 `EF BB BF`。
2. **表头**（第 1 行，英文键名、小写、顺序固定、恰好 10 列）：
   `repo,id,size,sha256,status,owner,claimed_at,completed_at,downloaded_at,updated_at`
3. **行序**：与同一筛选+排序下的 `/api/packages` 顺序一致（`sort`/`order` 默认 `updated desc`）。
   ⇒ 页面看到的顺序 ≈ 导出文件顺序（用于"导出我看到的这批"）。
4. **换行**：行尾 `CRLF`（`\r\n`），文件最后一行的 `\r\n` 保留。
5. **取值**：
   - `size` 为十进制整数（无千分位）；
   - `sha256` 为 64 位小写 hex 或空；
   - `status` 用英文枚举 `available|claimed|completed`（不写中文标签）；
   - 空值（`null`/缺失）写**空字符串**，不写 `-`、不写 `null`；
   - 时间写上游原值（ISO 8601），不本地化。
6. **转义规则**（RFC 4180 + 公式注入防护）：
   - 若字段值以 `=`、`+`、`-`、`@`、制表符（`\t`）或回车（`\r`）开头，在值前加一个单引号 `'`；
   - 若字段值含 `,`、`"`、`\r` 或 `\n`，用双引号包裹整个字段，字段内的 `"` 写成 `""`；
   - 两者同时满足时先加 `'` 再包裹（顺序：先注入防护、后引号包裹）；
   - 数值列 `size` 不包裹（纯数字无特殊字符）。
7. 编码：UTF-8，不做 BOM 之外的前后缀；不写注释行、不写汇总行。

错误：`400` / `401` / `503` 均为 **JSON**（`Content-Type: application/json`），前端必须按
`response.headers.get("content-type")` 判断，避免把错误体当 CSV 存成文件。

### 6.4 `GET /api/packages` —— 列表 + 新增排序

请求参数：

| 参数 | 默认 | 合法值 | 说明 |
|---|---|---|---|
| `page` | `1` | ≥1 整数 | 页码，从 1 开始 |
| `size` | `50` | 1–200 整数 | 每页行数 |
| `repo` | `""` | 仓库名或空 | 空 = 全部仓库 |
| `status` | `""` | `available\|claimed\|completed\|""` | 空 = 全部状态 |
| `q` | `""` | ≤200 字符 | 面板侧过滤：`id` 或 `owner` 的大小写不敏感子串匹配；在分页**之前**应用 |
| `sort` | `updated` | `updated\|size\|id` | 排序键（新增） |
| `order` | 见下 | `asc\|desc` | 排序方向（新增） |

默认值与校验的边界必须分清：

- **缺省**（参数不传或传空串）→ `sort=updated`；`order` 随 `sort` 取默认：`updated→desc`、`size→desc`、`id→asc`。
- **显式**传了值就按值校验：枚举外的值一律 `400 {"error": …}`，**绝不静默回落默认**——
  `?sort=foo` → `400`（不是 200 + `updated desc`），`?order=down` → `400`（不是 200 + 默认方向）。
  理由：静默回落会让"点了表头但排序没变"变成不可诊断的静默错误，前端也无法把 URL 与表格状态对齐。

精确错误文案（`400`）：`sort` 越界 → `{"error": "sort 只能是 updated、size、id 之一"}`；
`order` 越界 → `{"error": "order 只能是 asc 或 desc"}`；`page`/`size` 非整数 → `{"error": "参数不合法"}`。
（`page`/`size` 越界不报错，按 §6.0 clamp。）

成功 `200`：

```json
{
  "ok": true,
  "repo_filter": "",
  "generated_at": "2026-11-10T20:15:14+08:00",
  "sort": {"by": "updated", "order": "desc"},
  "page": {"page": 2, "size": 50, "total": 631, "pages": 13,
           "matched": 631, "limit": 50, "offset": 50, "truncated": false},
  "summary": {"repos": 1, "total": 631, "available": 480, "claimed": 151,
              "completed": 0, "downloaded": 12, "bytes_total": 734003200},
  "repos": [{"repo": "t/one", "repo_type": "dataset", "manifest": "manifest.jsonl",
             "manifest_sha256": "9f2c…ef01", "manifest_size": 20480}],
  "packages": [
    {"repo": "t/one", "id": "shard-00042.tar", "size": 1288490188,
     "sha256": "9f2c…ef01", "status": "claimed", "owner": "bob",
     "claimed_at": "…", "completed_at": null, "downloaded_at": null,
     "note": null, "updated_at": "2026-11-10T10:00:00+08:00"}
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `packages[]` | obj[] | 每项必含 `repo,id,size,status,owner,updated_at`；**可含**（新增）`sha256,claimed_at,completed_at,downloaded_at,note` |
| `page.page` | int | 当前页（回显，已 clamp） |
| `page.size` | int | 每页行数（回显，已 clamp） |
| `page.total` | int | **筛选后**总行数（不是上游全库行数） |
| `page.pages` | int | `ceil(total/size)`，最小 1 |
| `page.matched` | int | 上游按 `repo`/`status` 匹配到的行数，**不含 `q` 过滤**；上游没给时回落为 `total` |
| `page.limit` / `page.offset` | int | 本次切片的上游同义参数（`limit=size`、`offset=(page-1)*size`），保留 v1 兼容字段 |
| `page.truncated` | bool | 排序/过滤时超过 200000 行上限则为 true |
| `summary` | obj | 上游 `/packages` 首块的 `summary` 原样透传（`repos/total/available/claimed/completed/downloaded/bytes_total`），v1 兼容字段 |
| `repos` | obj[] | 上游仓库数组原样透传（`repo/repo_type/manifest/manifest_sha256/manifest_size`），v1 兼容字段 |
| `sort.by` / `sort.order` | str | 生效的排序 |

`page.total` 与 `page.matched` **语义不同，不能混用**：`total` = 面板过滤（`repo`+`status`+`q`）之后的真实行数，
分页与导出都以它为准；`matched` = 上游对 `repo`/`status` 的匹配数，**不扣 `q`**。两者相等只说明这次没传 `q`。

实现约束（后端）：

- 排序在**筛选后的全集**上做（拉全量→过滤→稳定排序→切片），不得只排当前页；
  全量拉取沿用 `_all_packages()` 的 `limit=1000` 分页循环与 200000 行上限。
- **稳定排序**：`updated_at` 相同的行保持上游返回顺序。这保证既有测试
  `page=2&size=2 → shard-00002.tar, shard-00003.tar` 的断言不变（假上游 6 个包 `updated_at` 全相同）。
- `sha256` 等新增字段直接透传上游；上游未提供时为 `null`。
- `page.matched` 只作信息字段透传；分页、`pages`、导出条数一律用面板计算的 `total`。

错误：`400`（参数非法）、`401`、`503`（上游故障）、`404`（未知 `/api/` 路径）。

### 6.5 既有接口（不改变语义，兼容红线）

| 接口 | 必须保持 |
|---|---|
| `GET /api/summary` | 键 `ok`、`ts`、`totals.{packages,available,claimed,completed,bytes_total,repos,devices_online,active_leases,prelock_leases,free_for_assign}`、`repos[].{repo,type,enabled,summary}`、`devices[].{device_id,user_id,tier,speed,status,last_seen,client_ver}`、`leases[].{lease_id,kind,repo,package,device_id,user_id,expires_in,progress}`。允许**新增**字段（如 `generated_at`、`upstream_ms`）。 |
| `GET /api/anomalies` | `ok`、`stuck[]`（`repo,package,owner`）、`orphan[]`（`lease_id,repo,package`）、`ghost[]`（`lease_id,device_id`）。允许新增 `counts`。 |
| `GET /api/audit` | `ok`、`enabled`、`lines[]`（`ts,kind,repo,pkg,actor,detail`）、未启用时 `lines: []`。 |
| `GET /login`、`POST /login`、`/logout` | 行为与 302/Cookie 属性不变；登录失败限速 429 保持。 |
| 安全头 | `Content-Security-Policy`、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store` 全部保留，**CSP 不得出现 `unsafe-inline`**。 |
| 静态资源 | `/static/app.css`、`/static/app.js` 路径保持（可加 ETag，不可改成别的 URL）。 |

### 6.6 错误码总表

| 码 | 触发条件 | 响应体 | Content-Type |
|---|---|---|---|
| `400` | 参数非法：`sort`/`order` 越界、`page`/`size` 非整数、缺 `id`、`repo` 歧义、`tail` 越界 | `{"error": "…"}` | json |
| `401` | 无会话 / 会话签名不对 / 会话过期（调 `/api/*`） | `{"error": "未登录"}` | json |
| `404` | 包不存在；未知 `/api/` 路径 | `{"error": "包不存在：repo/id"}` / `{"error": "not found"}` | json |
| `413` | `POST` 请求体 > 64 KiB（既有） | 纯文本（既有行为，保持） | text |
| `429` | 登录失败限速（既有） | 登录页 HTML | html |
| `503` | 上游不可达/超时；指定 `repo` 时的上游 5xx/401；admin key 读取失败；静态资源缺失（**无 `repo` 的 5xx 走 §6.2 的扫描回退，终态多为 400/404**） | `{"error": "上游…"}` | json |

错误文案规范：`400` 指出"哪个参数、允许什么"；`503` 以"上游"开头；不泄露内部路径。

---

## 7. 无障碍与 CSP

### 7.1 CSP 红线（不得放宽）

响应头必须**恰好**包含（与 v1 一致，不接受新增来源）：

```
default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
form-action 'self'; frame-ancestors 'none'
```

由此产生的硬性实现约束（前端必须照做）：

1. **无内联 `<script>`**：所有 JS 在 `web/app.js`；`<script src="/static/app.js" defer>`。
2. **无内联 `style="…"` 属性**：状态变化用 class 切换；**进度用原生 `<progress value max>`**；
   宽度/高度用预定义 class（`.fp-w-25/50/75/100` 之类）；JS 里**不**写
   `el.style.*`/`el.setAttribute("style", …)`（CSSOM 写入虽然多数浏览器不拦，但会破坏"零内联样式"这条可 grep 的自检）。
3. **无内联事件处理器**：禁止 `onclick=`/`onchange=`/`onload=`；一律 `addEventListener`。
4. `img-src 'self' data:` 只允许同源图片与 `data:`（内联 SVG 用 `<svg>` 元素，不算 img）。
5. 不得为图省事加 `'unsafe-inline'` 或 `'unsafe-eval'`；不得引 CDN、不得用 `innerHTML` 拼装含用户数据的字符串
   （一律 `textContent`，需要结构时 `createElement`）。
6. 不允许把 `{{UPSTREAM}}`、`{{ERROR}}` 之外的内容插进模板；占位符替换必须 `html.escape`（上游 URL、
   错误文本都可能含特殊字符）。

### 7.2 键盘可达

| 键 | 行为 |
|---|---|
| `Tab` / `Shift+Tab` | 按 DOM 顺序遍历：跳转链接 → 页头动作 → 导航 → 工具栏 → 表格内包名按钮 → 分页 → 抽屉按钮 |
| `Enter` / `Space` | 激活当前按钮/链接；表格行本身不可聚焦，只有行内按钮可聚焦 |
| `Esc` | 关闭抽屉 → 关闭快捷键面板 → 清空搜索框焦点（按顺序只处理最上层） |
| `/` | 焦点跳到搜索框（不在输入框内时） |
| `r` | 立即刷新（不在输入框内时） |
| `1`–`5` | 切到第 N 个页面（不在输入框内时） |
| `?` | 打开快捷键说明面板（P1） |

抽屉打开时：焦点陷阱（`Tab` 在抽屉内循环）、`aria-modal="true"`、背景 `inert`、关闭后焦点回到触发元素。

### 7.3 焦点、语义与动效

- **focus-visible**：所有可聚焦元素统一
  `outline: 2px solid var(--fp-focus); outline-offset: 2px; border-radius: 4px`；
  不用 `outline: none` 除非同时给出等价可见替代（此处不允许）。
- **语义**：`header`/`nav`/`main`/`footer` 齐全；每页一个 `h1`（可视觉隐藏给屏幕阅读器，用 `.fp-sr`）；
  表格用 `caption`（视觉隐藏）+ `th scope="col"`；表单控件用 `<label>` 包裹（文字可为 `.fp-sr`）；
  纯图标按钮必须有 `aria-label`。
- **aria-live**：顶部隐藏区域
  `<p id="fp-live" class="fp-sr" role="status" aria-live="polite" aria-atomic="true">`。
  内容：刷新成功 "已更新 20:31:05"；刷新失败 "刷新失败：上游不可达"；导出完成 "已导出 631 行"。
  不打断当前焦点（只更新文本）。
- **busy 状态**：加载中给 `main` 或表格容器 `aria-busy="true"`，完成后移除。
- **对比度**：§1.6 的 27 个文本对全部 ≥4.5:1（表末 ✗ 行是唯一禁用组合：亮色 `primary/bg-subtle` 4.09:1）；
  非文本（控件边界/焦点框/纯图标）≥3:1；状态不只靠颜色（徽章带文字、异常同时显示数字）。
- **缩放**：1440×900 下浏览器缩放 200% 不出现横向滚动（表格容器保持 `overflow: visible`，宽表靠隐藏低优先列解决；
  页签条自身的横向滚动不在此列，见 §2.6 吸顶约束）。
- **`prefers-reduced-motion`**：关闭骨架微光、抽屉滑入改淡入、toast 不位移（§1.5 已给 CSS）。

### 7.4 可执行的验收命令

```bash
# 1) CSP：必须是既有值，且不含 unsafe-inline
curl -sI http://127.0.0.1:31832/ | grep -i content-security-policy

# 2) 零内联样式 / 零内联事件（两个 grep 都必须无输出）
grep -n 'style="'  fox-panel/web/index.html fox-panel/web/login.html fox-panel/web/app.css fox-panel/web/app.js
grep -nE ' on[a-z]+=' fox-panel/web/index.html

# 3) 页面不含管理员密钥（沿用 v1 测试思路；key 从文件读，不打印）
python3 - <<'PY'
import pathlib
k = pathlib.Path.home().joinpath(
    "文档/deepseek-harness/default-workspace/fox-label/state/admin.key")
key = k.read_text().strip() if k.exists() else "no-key"
for f in ("index.html", "login.html", "app.js", "app.css"):
    t = pathlib.Path("fox-panel/web", f).read_text(encoding="utf-8")
    assert key not in t, f"{f} 泄露了 admin key"
print("no key leak")
PY

# 4) 主题对比度（§1.6 的 31 个组合 × 两套主题；直接读 web/app.css 的 token，表与实现同源）
python3 - <<'PY'
import pathlib, re, sys

css = pathlib.Path("fox-panel/web/app.css").read_text(encoding="utf-8")

def block(theme):
    i = css.index(':root[data-theme="%s"]' % theme)
    j = css.index("}", i)
    return dict(re.findall(r"(--fp-[a-z0-9-]+)\s*:\s*([^;]+);", css[i:j]))

T = {k: block(k) for k in ("dark", "light")}

def lum(h):
    h = h.strip().lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda v: v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])

def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)

# 与 §1.6 表逐行对应：(前景, 背景, 门槛, 用途)
PAIRS = [
    ("--fp-text-1", "--fp-surface", 4.5, "正文/单元格/数值"),
    ("--fp-text-2", "--fp-surface", 4.5, "次要说明"),
    ("--fp-text-3", "--fp-surface", 4.5, "弱化说明 11px"),
    ("--fp-text-1", "--fp-bg-subtle", 4.5, "表头/页签/工具栏"),
    ("--fp-text-2", "--fp-bg-subtle", 4.5, "自动刷新开关/页脚"),
    ("--fp-text-3", "--fp-bg-subtle", 4.5, "th/底部 tab 未激活"),
    ("--fp-text-3", "--fp-bg", 4.5, "页面级弱化文字"),
    ("--fp-text-2", "--fp-bg", 4.5, "分页信息"),
    ("--fp-text-2", "--fp-surface-2", 4.5, "状态 pill"),
    ("--fp-text-3", "--fp-surface-2", 4.5, "次级块弱化"),
    ("--fp-text-2", "--fp-surface-hover", 4.5, "悬停说明"),
    ("--fp-text-1", "--fp-surface-hover", 4.5, "悬停行文本"),
    ("--fp-text-1", "--fp-primary-soft", 4.5, "侧栏激活文字"),
    ("--fp-primary", "--fp-surface", 4.5, "链接/包名按钮"),
    ("--fp-primary-hover", "--fp-surface", 4.5, "链接悬停"),
    ("--fp-primary-hover", "--fp-bg-subtle", 4.5, "底部 tab 激活文字"),
    ("--fp-primary-fg", "--fp-primary", 4.5, "主按钮文字"),
    ("--fp-primary-fg", "--fp-primary-hover", 4.5, "主按钮悬停"),
    ("--fp-success", "--fp-success-bg", 4.5, "已完成徽章"),
    ("--fp-warning", "--fp-warning-bg", 4.5, "处理中/预锁徽章"),
    ("--fp-danger", "--fp-danger-bg", 4.5, "危险徽章"),
    ("--fp-info", "--fp-info-bg", 4.5, "已下载/活跃租约徽章"),
    ("--fp-neutral", "--fp-neutral-bg", 4.5, "未认领徽章"),
    ("--fp-success", "--fp-surface", 4.5, "统计块正常值"),
    ("--fp-warning", "--fp-surface", 4.5, "TTL <300s"),
    ("--fp-danger", "--fp-surface", 4.5, "TTL <60s/异常值"),
    ("--fp-warning", "--fp-bg", 4.5, "提示条 fp-note"),
    # 非文本：控件边界 / 焦点框 / 纯图标，门槛 3:1
    ("--fp-border-field", "--fp-surface", 3.0, "控件边界（非文本）"),
    ("--fp-focus", "--fp-surface", 3.0, "焦点框（非文本）"),
    ("--fp-focus", "--fp-bg", 3.0, "焦点框贴底（非文本）"),
    ("--fp-primary", "--fp-primary-soft", 3.0, "激活图标（非文本）"),
]

bad = []
for fg, bg, need, use in PAIRS:
    for theme in ("dark", "light"):
        if fg not in T[theme] or bg not in T[theme]:
            bad.append((theme, use, "token 缺失"))
            continue
        r = ratio(T[theme][fg], T[theme][bg])
        if r < need:
            bad.append((theme, use, round(r, 2), "need>=%s" % need))
# 已知禁用组合：亮色 primary/bg-subtle 必须保持 <4.5，否则 §1.6 的告警该更新了
banned = ratio(T["light"]["--fp-primary"], T["light"]["--fp-bg-subtle"])
print("banned primary/bg-subtle(light) = %.2f:1（应 <4.5，文字禁用；用 primary-hover 或 text-1）" % banned)

# 用法断言：底部 tab 激活态的文字色不得是 primary（亮色只有 4.09）；图标子选择器不在此列
m = re.search(r'\.fp-tabbar__item\[aria-current[^{]*\{([^}]*)\}', css)
warn = []
if not m:
    warn.append("未找到 .fp-tabbar__item[aria-current] 规则，跳过用法断言")
else:
    color = (re.search(r'color:\s*([^;]+);', m.group(1)) or [None, ""])[1].strip()
    if color not in ("var(--fp-primary-hover)", "var(--fp-text-1)"):
        bad.append(("text", "tabbar 激活文字", color, "只允许 primary-hover/text-1"))

print("FAIL", bad) if bad else print("contrast ok (%d 对 × 2 主题)" % len(PAIRS))
if warn:
    print("WARN", warn)
sys.exit(1 if (bad or banned >= 4.5) else 0)
PY
```

---

## 8. nonGoals

本轮**明确不做**以下四件事，理由是它们都需要动轮次之外的东西（服务端写接口、协议、数据模型），
现在做会引入风险且无法验收：

1. **不做任何写操作**（打回 `reopen`、释放 `release`、踢设备 `unassign`、`doctor --fix`、改备注）。
   - 原因：面板目前**没有 CSRF 防护面**，正因为"零写操作"才不需要 CSRF token。加写操作就必须先在
     fox-label 服务端补一套管理端 HTTP 写接口（带鉴权、幂等、审计事件、并发冲突语义），再在面板侧做
     CSRF token/双重提交 Cookie、`Origin` 校验、二次确认 UI。这是一整套独立工作，必须单独一轮做，
     且必须配套新的测试（当前 39 项测试完全不覆盖写路径）。
   - 本轮替代方案：页面上给出 CLI 提示文案（"需要打回/释放请执行 `python3 server.py reopen …`"），
     只读文本，不提供按钮。
2. **不做 SSE / WebSocket 推送**。
   - 原因：纯标准库下 `ThreadingHTTPServer` 跑长连接需要自己处理连接泄漏、代理缓冲、断线重连、
     心跳与优雅停机；当前 20–30 人规模 + 15 秒轮询足够（v1 已验证）。SSE 另开一轮，且要先在
     Caddy 侧确认 `proxy_buffering` 与超时配置。
3. **不做多语言（i18n）**。
   - 原因：面板只有中文单语用户；引入 i18n 会把每个字符串都变成查表调用，本轮的工作量应花在
     设计系统与接口上。约定：所有文案集中写在 `app.js` 顶部的 `T` 对象里（便于以后替换），
     但只提供中文。
4. **不做多账号 / 权限分级**。
   - 原因：v1 的设计是"单账号 = 管理员"，配置里只有一组 `username/pwd_hash`，服务端签名会话也
     不带角色。多账号需要用户表、角色、审计归属、密码重置流程，是服务端数据模型改动，另开一轮。
   - 本轮替代方案：不改鉴权代码，保持单账号；页面上不出现"用户管理"入口。

补充 nonGoal：**不做服务端分页以外的聚合新接口**（如按用户统计、趋势历史），因为上游没有对应端点，
面板侧计算会把 40 万包的仓库拖成分钟级。本轮所有新接口都只做"单包查询 / 导出 / 元数据"这类
能靠一次上游请求或一次线性扫描完成的事。

---

## 附录 A · 施工检查单（交接用）

后端（`panel.py`）：

- [ ] `web/` 静态资源下发（含 ETag、Content-Type、缺失时 503 JSON）
- [ ] `GET /api/meta`：字段与 §6.1 一致，上游失败降级 200
- [ ] `GET /api/package`：参数校验、404/400 映射、`sha256` 透传
- [ ] `GET /api/export.csv`：BOM + CRLF + 10 列 + 转义 + 公式注入防护 + `X-Truncated`
- [ ] `GET /api/packages`：`sort`/`order` 校验与稳定排序、`page.{size,total,pages,truncated}`、`q` 面板侧过滤
- [ ] `python3 tests/test_panel.py` → 39/39 通过
- [ ] 新增 `tests/test_v2.py`（meta/package/export/sort 各 ≥3 例，含 400 与 503）

前端（`web/`）：

- [ ] 5 页 + 抽屉，三档断点各截一张图
- [ ] 两套主题，全部组件出现一次
- [ ] 全部交互只用 `addEventListener`，无 `innerHTML` 拼接用户数据
- [ ] 键盘走查（§7.2）与 `#fp-live` 播报
- [ ] §7.4 的 4 条命令全部通过

## 附录 B · 文案表（中文，集中在 `app.js` 的 `T`）

| key | 文案 |
|---|---|
| `nav.overview` / `packages` / `devices` / `anomalies` / `audit` | 概览 / 包列表 / 注册设备表 / 异常体检 / 审计 |
| `status.available` / `claimed` / `completed` | 未认领 / 处理中 / 已完成 |
| `empty.none` / `empty.filtered` | 还没有数据 / 没有匹配的包 |
| `empty.audit` | 未配置审计文件（`--audit-file`） |
| `err.upstream` | 上游不可用 |
| `err.retry` | 重试 |
| `toast.copied` | 已复制 |
| `toast.exported` | 已导出 {n} 行 |
| `live.updated` | 已更新 {time} |
| `live.failed` | 刷新失败：{error} |
| `readonly.hint` | 只读面板：打回/释放/踢设备请用服务端 CLI |
