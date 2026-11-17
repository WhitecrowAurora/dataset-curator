# fox-panel

fox-label 的**只读 Web 面板**（v2）。独立进程、独立配置，只通过 HTTP 读 fox-label 的只读接口，
不 import 服务端代码、不碰它的数据库。

```
浏览器 ──cookie 会话──▶ fox-panel :31832 ──Bearer(admin key)──▶ fox-label :31831
```

## 快速开始

```bash
python3 panel.py init --user admin            # 交互设置密码（≥8 位）
python3 panel.py serve                        # 默认 127.0.0.1:31832
```

纯标准库，无第三方依赖（Python 3.9+；本机/VPS 实测 3.14 / 3.13）。

## 文件结构（v2 起前端拆成静态资源）

```
fox-panel/
├── panel.py                 # 唯一后端：HTTP 服务 + 静态资源下发 + 只读 API 代理（1600 行）
├── web/                     # 前端产物（纯静态，由 panel.py 按固定文件名下发）
│   ├── index.html           # 单页外壳（只留占位符 {{VERSION}} / {{UPSTREAM}}）
│   ├── login.html           # 登录页（无 JS，{{ERROR}} 由服务端注入）
│   ├── app.css              # 设计系统：token + 11 个组件 + 三档响应式（1407 行）
│   └── app.js               # 五个页面 + 单包抽屉 + 主题/快捷键/轮询（2283 行）
├── tests/
│   ├── test_panel.py        # 39 项：鉴权/会话/限速/反代/聚合/上游故障/安全头
│   └── test_panel_v2.py     # 204 项：新接口/CSV/静态资源/恶意输入/t14 修复核验
├── SPEC-v2.md               # v2 设计规格与接口契约（token/组件/IA/契约/无障碍）
├── DELIVERY-v2.md           # v2 交付记录：验收命令、指纹、回滚、已知取舍
└── fox-panel.service        # systemd 单元示例（本机 --user / VPS system 两用）
```

静态资源**只按固定文件名映射**（`/static/app.css`、`/static/app.js`），不接受用户输入路径，
没有 `../` 穿越面；`web/` 缺失或未交付时回退内置页面（部署中间态不会整页 503）。
css/js 带 `ETag: "<mtime_ns>-<size>"` 与 `Cache-Control: no-cache`，命中 `If-None-Match` 返回 304 空体；
两个 HTML 页面保持 `no-store`。

## 界面（v2 五个页面 + 单包抽屉）

| 页面 | 内容 |
|---|---|
| 概览 | 9 个统计块（总包/未认领/处理中/已完成/active 租约/预锁/在线设备/可分配/总容量）+ 每仓库汇总表 |
| 包列表 | 仓库/状态/关键字筛选 + **服务端排序**（更新时间/大小/包名，升降序）+ 分页（25/50/100/200）+ **导出 CSV** |
| 注册设备表 | 设备档位/速度/心跳/版本；租约类型、剩余 TTL（本地每秒插值，不刷新也不飘）、进度条 |
| 异常体检 | 死包 / 孤儿租约 / 幽灵租约三张表，全 0 时显示「体检通过」 |
| 审计 | 审计 jsonl 尾部（需 `--audit-file`），关键字客户端过滤、时间倒序、详情截断 |
| 单包抽屉 | 点包名 → 右侧抽屉：12 个契约字段 + 当前租约摘要；`Esc` 关闭、焦点归还；无任何写操作按钮 |

交互与无障碍：暗/亮主题（跟随系统 + 手动切换并持久化）、三档响应式导航（≥1200 侧栏 /
768–1199 顶部页签 / <768 底部 tab，<768 表格转卡片）、键盘可达（`/` 搜索、`r` 刷新、`1`–`5` 切页、
`?` 快捷键面板、`Esc` 关闭）、`aria-sort` / `aria-live` 播报、focus-visible 2px、`prefers-reduced-motion`。
CSP 保持 `script-src 'self'` 且无 `unsafe-inline`，前端零内联脚本/样式、无 `innerHTML`。

## 只读接口

页面路径需要会话（未登录 302 到 `/login`）；`/api/*` 未登录一律 `401 {"error": "未登录"}`（JSON）。

| 接口 | 说明 |
|---|---|
| `GET /api/summary` | 概览聚合（totals / repos / devices / leases），v1 语义不变 |
| `GET /api/packages` | 列表 + 分页。参数 `page,size,repo,status,q,sort,order`；`q` 在**面板侧**、分页前过滤；`sort=updated\|size\|id`、`order=asc\|desc`（缺省随 sort 取默认），**非法枚举 → 400**（不静默回落）；返回 `page.{total,pages,matched,limit,offset,truncated}`（`total` 是面板过滤后行数，`matched` 是上游匹配数不含 `q`）与 v1 保留字段 `summary`/`repos` |
| `GET /api/package?repo=&id=` | **新增**：单包详情。`package` 恰好 13 键（12 字段 + `lease`），`status_label` 固定用面板词表（未认领/处理中/已完成，不吃上游文案）；无租约或 work 接口故障时 `lease` 为 null 且详情仍 200；缺 `id` → 400，不存在 → 404，跨仓库同名 → 400（请指定 repo），上游故障 → 503 |
| `GET /api/export.csv?…` | **新增**：按当前筛选导出 CSV。UTF-8 BOM + CRLF + 固定 10 列（`repo,id,size,sha256,status,owner,claimed_at,completed_at,downloaded_at,updated_at`）+ RFC4180 转义与公式注入防护（`= + - @ \t \r` 开头加前导单引号），`X-Total-Rows`/`X-Truncated` 头，导出顺序与列表顺序一致 |
| `GET /api/meta` | **新增**：前端启动配置（版本、上游、刷新间隔、分页档位、状态/排序枚举、仓库、features、审计开关）。上游故障时降级为 `upstream_ok=false` 的 200，不会整页不可用 |

排序、筛选与分页都在**服务端全集**上做（拉全量 → 过滤 → 稳定排序 → 切片），所以跨页顺序正确、
`updated_at` 相同的行保持上游原始顺序（`page=2&size=2 → shard-00002/00003` 这条 v1 断言仍然成立）。

## 公网部署（反代场景）

放在 Caddy/Nginx 后面时，面板会自动：

- 按 `X-Forwarded-For` 认真实客户端 IP（否则所有访客共用反代那一个限速桶）
- 见到 `X-Forwarded-Proto: https` 就给会话 cookie 加 `Secure`

> 只有在**直连方是本机**（127.0.0.1/::1）时才信任 XFF，否则任何人伪造这个头就能绕过限速。

Caddy 示例（面板 + 成员端 API 共用一个域名；v2 无需改动反代，静态路径仍是 `/static/app.css|app.js`）：

```
label.example.com {
	handle /api/v1/* {
		reverse_proxy 127.0.0.1:31831
	}
	handle /health {
		reverse_proxy 127.0.0.1:31831
	}
	handle {
		reverse_proxy 127.0.0.1:31832
	}
}
```

## 配置

`~/.config/fox-panel/config.json`（600）：

```json
{
  "username": "admin",
  "salt": "<hex>", "pwd_hash": "<pbkdf2-sha256>", "iterations": 200000,
  "secret": "<hex>（会话签名密钥）",
  "upstream": "http://127.0.0.1:31831",
  "admin_key_file": "…/fox-label/state/admin.key",
  "audit_file": null
}
```

改密码（**立即生效**，无需重启服务；旧会话当场失效）：

```bash
python3 panel.py passwd
```

## 鉴权设计

- 密码 **PBKDF2-SHA256**（20 万轮 + 16 字节随机盐），不存明文
- 会话：`HMAC-SHA256` 签名 Cookie（`HttpOnly` + `SameSite=Lax`，12 小时过期），
  HTTPS 下自动加 `Secure`；服务端无状态，改不了也伪造不了
- 登录失败限速：单 IP 5 次后指数退避（上限 5 分钟），全局 30 次锁 1 分钟
- 错误信息不区分"账号错"与"密码错"，避免枚举
- 安全响应头：CSP（`script-src 'self'`，禁内联）、`X-Frame-Options: DENY`、
  `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store`
- admin key 只在面板进程内使用，不出现在任何 HTML/JS/日志中（启动日志只打密钥文件名）
- 配置热加载：`passwd` 后签名密钥立即换新，旧 cookie 全部失效

## 作为常驻服务

本机（user 单元）：

```bash
mkdir -p ~/.config/systemd/user
cp fox-panel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fox-panel.service
```

VPS（system 单元，`WorkingDirectory=/opt/fox-panel`，见 `/etc/systemd/system/fox-panel.service`）：

```bash
systemctl restart fox-panel && systemctl status fox-panel --no-pager
```

## 自检

```bash
python3 tests/test_panel.py       # 39 项：鉴权/会话/限速/反代/聚合/上游故障/安全头
python3 tests/test_panel_v2.py    # 204 项：/api/package、CSV、meta、排序、静态资源、恶意输入、t14 修复核验
```

两套都自包含：自己起假上游 `http.server` + 面板子进程（临时配置），不联网、不依赖真实服务。
`test_panel_v2.py` 里还有一组「字节绑定 + 交付核验」：服务端 `/static/app.css|app.js` 的 sha256
必须等于 `web/` 里的文件，脚本还会断言 CSP、ETag 形状、零内联样式与 t14 的 11 条前端修复。

## 已知限制 / 本轮 non-goals

- **只读**：打回、释放、踢设备仍需跑服务端 CLI（`reopen` / `release` / `unassign` / `doctor --fix`）。
  面板不提供任何写入口，连「危险按钮」都不渲染（抽屉里没有打回/释放/踢设备）。
- **无 SSE**：靠 15 秒轮询（20~30 人规模足够；长连接与反代缓冲要另做一轮）。
- **单账号**：多人分权需要用户表与角色体系，本轮不做（公共密码已被限速与 12 小时会话约束）。
- **多语言**：文案集中在 `app.js` 的 `T` 表，但本轮只做中文。
- **虚拟滚动 / 列宽拖拽 / 容量趋势图**：本轮不做（>10 万行导出用 `X-Truncated` 提示）。

详细契约见 [SPEC-v2.md](SPEC-v2.md)，本次上线记录（验收输出、文件指纹、回滚步骤、已知取舍）见
[DELIVERY-v2.md](DELIVERY-v2.md)。
