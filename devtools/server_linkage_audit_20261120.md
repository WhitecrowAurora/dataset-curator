# 调度中心与本机联动检查（2026-11-20）

当前结论：下述 B1、B2、B3 均已修复；2026-11-20 04:03（服务器时间）已部署并重启调度服务，生产复查正常。修复验收共 52 项检查通过。以下原始检查保留为修复前证据；本机直连 HF 的环境阻塞未在此次改动范围内。

## 三项修复与验收

- B1：只有成功申报才进入待核验分支。pending/failed 报告下的失败申报会正常记为 failed、保存失败原因并释放分项；覆盖 active 与 offline 租约。成功申报仍进入 submitted，漏项的完成申报仍被拒绝；部分失败保留已验收通过的一侧。
- B2：新增 `tagger/center_runtime/download_segments.py`，按源哈希、文件大小和原始分段数记录下载布局，连接数只控制并发。4→2、2→4、4→1、4→4 均通过真实 curl 中断及续传，HTTP 请求从已下载偏移继续，最终 SHA256 一致，并发未超过设定值。来源变化重新使用独立缓存；旧版无区间记录的分段不混用。
- B3：`status` 使用现有 `derived_status(last_seen, now)`，与设备列表共用在线状态口径。30 秒、180 秒、900 秒、3 天及空时间均验证通过；查询不会更新 last_seen 或数据库管理状态。
- 下载验证使用上轮取得的真实分片字节，由本地 HTTP Range 服务提供；本地源码与服务器独立 worker 内嵌源码各通过 7 项检查。独立 worker 的 CLI 加载也通过。此次没有声称重新完成 HF 端到端联网下载或真实 GPU 打标。
- 同一 Linux 服务器独立数据库的 23 项回归检查通过；本机到隔离调度服务的下载、回传、归并、续作和完成申报 15 项联动检查再次全部通过。
- 服务器仅替换 `dispatch.py`、`worker.py`、`worker-sources.json`，独立 worker 只修改原内嵌下载模块并新增下载布局模块。保留其余旧部署模块，避免混入本地其他开发改动。`server.py`、面板、生产配置未修改。
- 部署后 `lulynx-label.service` 为 active，MainPID 32391，启动日志正常；公网 `/health` 为 200，2 仓库、874 片。现有 lulu 身份通过只读 status 认证并显示 offline，0 租约。生产包数仍为 873 available、1 completed，open 租约 0。
- 部署 SHA256：dispatch `b025c1d4446b0304b86953a9daa76dcb70020e22ca22c03f7a73bb99ad77c3d5`；worker `c9558a4093017aba616ad78a5814779f138ff499f028dcd0a5bc5733def28afa`；worker-sources `407947b2d2ac74b03c37139819adab8e958faf3354065c9e687a38e27d58a322`。本地与远端逐文件一致。
- 服务器改前备份：`/tagger/.delete/server-linkage-fix-20261120/before/tagger/source/fox-label/`。修复测试及结果归档：本地 `.delete/server-linkage-fix-20261120/backend/smoke/server-linkage-fix-20261120/`，远端 `/tagger/.delete/tmp/lulynx-linkage-fix-20261120/`。临时实例与 SSH 隧道已关闭；未 commit/push。

## 证据范围

- 真实服务器：119.28.16.93，SSH 28882；公网 80 的 `/api/v1/`、`/api/blob/`、`/health` 转至 127.0.0.1:31831，其他路径转至面板 31832。
- 公网及 SSH 隧道 `/health` 均为 200：2 仓库、874 分片；本机已保存的 lulu 身份通过 `/api/v1/status` 认证。
- 本机未启动打标服务，工作目录为空；没有运行真实 GPU 打标。
- 服务器原进程 2026-11-16 16:30:23 启动；server.py 和 dispatch.py 的修改时间均早于进程启动。复制的部署文件与远端 SHA256 一致。
- server.py SHA256：`0cb71bcfe8fcdac585d239849b84a30d61c5fbfdd933faf3b22aa8a9bfa0db83`。
- dispatch.py SHA256：`dd0fac5ec28510c6cfaac3dd255963721ecbf2aefc33e75406357b928fa8c245`。

## 确认问题

| 编号 | 级别 | 位置 | 触发及影响 | 证据 |
|---|---|---|---|---|
| B1 | 高 | 服务器 `/tagger/source/fox-label/dispatch.py:1513` | 已有逐图验收报告处于 pending/failed 时调用失败申报，也会进入 defer，变为 submitted/source_pending，失败原因没有按失败路径落账，分项继续占用并等待核验。 | 部署代码隔离实例中，先确认漏项的 done 被拒，再以 `ok=False`、`reason=isolated command failure` 申报，返回 `verification_pending=true`；数据库为 submitted，source_submissions 为 pending。分支缺少对 ok 的判断。 |
| B2 | 中 | 本机 `tagger/fox_worker.py:715`、`:724`、`:745` | 分段文件只用段序号命名，没有保存原始区间/连接数。中途把 4 连接改成 2 连接，第二段会被当成新范围的前缀继续追加，最终校验失败并清除分段缓存。 | 用本轮真实下载分片的有效原始字节构造旧 4 连接的两段缓存，再调用真实下载器使用 2 连接；下载响应和范围正确，但得到 SHA256 `374d6875cd4ce6fc6c6aac8bee470c9a561b34f73c16e1c357e62ca3c7770cf2`，校验失败后剩余分段为 0。哈希校验阻止了坏分片进入打标。 |
| B3 | 低 | 服务器 `/tagger/source/fox-label/dispatch.py:1790` | status 返回静态设备状态；设备多天没有活动仍返回 online，与设备列表的派生状态不一致。 | 真实 lulu 状态返回 online，但 last_seen_ago 约 356737 秒；隔离实例将 last_seen 设为 3 天前，status 返回 online，device_view 和 derived_status 均为 offline。此项影响状态显示，不阻止任务协议。 |

建议修复方向：B1 只对成功提交进入待核验分支，失败走原有释放/失败记录路径；B2 给续传分段保存源哈希及绝对区间，连接数改变时按原区间续传或显式重新分段；B3 在展示接口统一在线状态语义，保留归档/封禁等管理状态。

## 实际下载与联动验证

1. 本机直连 `https://huggingface.co`：requests 连接重置，curl 退出 35；服务器访问该站为 200。没有配置代理的当前默认下载路径因此不可用。这是本次确认的环境阻塞，不计入上述 3 个代码 bug。
2. 通过临时 SSH SOCKS 隧道，使用本机现有 HF token 请求官方受限仓库，返回 206，证明该 token 对测试分片有访问权限。没有把 token 发给镜像站。
3. 用本机真实 worker 下载 `MatchaCuratio/Anime-2024Dru768-8M/shard-000843.tar`，大小 94238720 字节，4 连接，约 28.3 秒；SHA256 与服务器登记值 `0b91012766c0d5939063ee3e9ff42d3ae4fe0a8eb92a4c2751ea2fd1f4b0b417` 完全一致。只在测试进程里启用代理并关闭 CDN 探测，没有改本机持久配置，也没有领取该生产分片。
4. 在同一台 Linux 服务器上，以当前部署源码启动单独的 loopback 测试实例和独立数据库。本机通过 SSH 隧道与它交互；隔离数据是两张图片 ID 的测试 tar、预置的独立权威源清单，以及人工构造的打标 JSONL，不代表真实模型质量验收。
5. 15 项检查通过：仅登记不领片、派工元数据、4 路下载哈希、缓存零请求、分段续传哈希与绝对续传位置、单流续传、不支持 Range 的回退、源图片 ID、回传归并、重复回传秒传、续作快照哈希、完成落账、重复 done 幂等、归还多派租约。
6. 另确认不完整逐图验收的成功申报会被拒绝。

## 不计为此次业务 bug 的观察

- 8999 的 502 属于另一套 nginx 代理，当前 worker 配置使用公网 80，不能据此判断调度回传失败。
- SSH 新连接偶尔在认证前被远端关闭；根因未查明，没有更改 SSH 设置。
- 8M 仓库的中心独立源清单核验尚未启用；400k 仓库已启用，当前缓存了 6/30 片的源清单，剩余可按需导入。前者是现有配置选择，应在大规模生产前确认验收口径，不算协议错配。

## 原始检查收尾（修复前）

- 生产服务、配置和源码未变，未领取/完成任何生产任务，未 commit/push。
- 收尾时生产状态仍为 873 available、1 completed，open 租约为 0，与检查前一致。
- Linux 隔离服务已停止，31833 已无监听；测试资料归档至服务器 `/tagger/.delete/tmp/lulynx-linkage-20261120`。
- 本机临时测试、源码快照、下载分片及结果移至 `.delete/server-linkage-20261120/backend/smoke/server-linkage-20261120/`；`backend/smoke` 不保留本轮脚手架。
- 临时 SSH 连接及代理隧道关闭；默认网络路径没有被自动修复。
