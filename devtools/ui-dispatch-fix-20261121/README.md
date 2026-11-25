# 新建任务空白与服务器派单画师选项修复

2026-11-21 完成。本机前端已构建，服务器修复已部署，新客户端包已生成。

## 修改

- 新建任务选择“只出 NL”并启用动漫 Torii / 真人 Qwen 分流时，普通描述模型面板被隐藏，但原页面没有分流设置面板，导致“模型与规则”内容为空。现在按动漫、真人、混合类型显示对应模型目录、引擎和已有服务配置。
- 复用发色复核模型设置，保存前合并最新配置与本页实际修改的字段，避免常驻新建任务页用旧副本覆盖另一页刚保存的配置。保存期间禁用编辑；刷新保留未保存的修改。
- 服务器工作区派单加入“沿用客户端 / 关闭 / 仅 NL / 仅 TAG / NL 与 TAG”。选择按分包、分项及源哈希保存，领取时固定在租约中。active、prelock、重连、离线清单均携带选项。
- 客户端在线 worker 与离线执行子进程传递选项，现有 CLI 入口将其适配为画师规则。不改变客户端模型路径、硬件配置、画师表与别名来源。每个分包单独设置环境，防止串入上一包选项。
- 服务器独立 worker 以部署前版本为基础，只补 run_task 环境传递和 annotation 模块，保留其他内嵌源码。客户端发行包的独立 worker 由当前客户端源码生成。

## 验证

- 前端 TypeScript / Vite 构建通过；仅有原有大 chunk 提示。
- Edge 实际浏览器验证：动漫、真人、混合分流；普通 NL、TAG、TAG+NL；390px 页面；真实设置 API 保存并重读；另一页面修改 Qwen 路径后，本页只改 Torii 不覆盖 Qwen。无页面 JavaScript 异常。截图里的健康状态被测试桩隔离，不是生产服务状态。
- 服务器面板实际 JS 浏览器验证：5 种选项的派单请求、轮询保留草稿、离线清单请求与下载。请求均发往隔离测试地址，未向生产派单。
- 107 项既有回归通过：test_cli、test_client、test_artist_table、test_center_watchdog、test_worker_retry。
- 21 项专项测试通过：迁移幂等、批量事务回滚、分项设备分配、预锁、重连选项固定、源哈希变化、默认继承与关闭、无画师时不猜测、来源保留、在线真实子进程环境、CLI 消费、离线导入到执行子进程。
- Linux 同机隔离数据库 45 项检查通过，使用真实部署源加本次修复；独立 worker CLI 加载通过。本机 Windows SQLite 测试未调用 Linux 文件锁，Linux 测试使用真实模块。
- 服务器独立 worker 内嵌 annotation 模块与 run_task 接线验证通过；发行 zip 内 6 个关键文件逐字节匹配当前源码/前端产物，包含 text_encoding 与新 worker 模块，未夹带 work、模型权重、个人 ui-form 或 nl_api 配置。
- 本轮没有启动 GPU 模型；验证的是界面、派单选项、持久化与执行适配，不是新一轮模型质量评估。

## 部署与状态

- SSH 直连曾间歇在认证前断开；HTTP CONNECT / SOCKS5 代理未解决。随后直连成功，保留同一已认证连接完成部署，没有修改 SSH 或代理配置。
- 修改前读取并比较生产文件：除本次修复外与本地快照一致。8 个部署文件及前后 SHA256 见 deployment.json。
- 先备份源码和数据库，部署后重启 lulynx-label.service；面板静态脚本直接读取更新文件，无需重启面板。两个服务均 active。
- 备份：`/tagger/.delete/ui-dispatch-fix-20261121/`。
- 本机读取公网 `/health`、`/static/workspace.js` 均 HTTP 200；公网脚本与本地新脚本逐字节一致。服务器直接检查 31831 /health、31832 /static/workspace.js 同样通过。
- 数据库迁移成功，原有状态未变：873 available、1 completed；租约 2 done、52 failed、227 released，无 open/submitted；未新增生产派单。
- 首次用服务器 loopback 的 nginx 默认站点检查得到 404，随后使用实际后端端口与公网域址重新确认通过；不是调度服务故障。

## 交付与使用

- 新客户端：`H:\tagger\dist\tagger-20261121-ui-artist-fix.zip`，76,497,750 bytes。
- SHA256：`4782493e8cf2d40e61a79d316003e942b8d781efeeb70a9042c06f8bccd8343e`。
- 客户端包含本次修复及之前 384 软目标 / 512 硬上限代码。该包不含 Python 环境、模型权重或本机状态；新机器需按 install.bat 安装依赖并准备模型/tokenizer。已有安装请退出客户端后更新，保留自己的模型和配置。
- 网页面板刷新即可出现画师选项；执行电脑必须同步更新客户端，旧客户端不会执行新增的派单选项。
- 注入仅使用可信元数据、已有画师表或目录来源；不会猜画师，不自动重打已有产物。选项只作用于本次成功分配的分项，正在执行的租约保持领取时的设置。
- 临时本机 UI 服务已关闭；测试脚手架归档至 `H:\lulynx-trainer\.delete\tagger-ui-dispatch-20261121\backend\smoke\tagger-ui-dispatch-20261121`。服务器临时测试归档至 `/tagger/.delete/tmp/lulynx-ui-dispatch-20261121/`。未 commit / push。
