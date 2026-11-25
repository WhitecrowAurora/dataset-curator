# 客户端与调度中心实操验收 · 2026-11-25

主要联动流程通过。本轮使用真实页面、真实请求和本机 GPU 推理；生产服务器只读核对，写操作在服务器上的隔离调度实例完成。本轮没有修改生产源码、重新部署生产服务或生成新发布包。

## 实际环境与样本

- Windows，RTX 4070 Ti SUPER 16 GB。
- TAG：已有 WD-EVA02，BF16、批大小 2。NL：Qwen3-VL 8B abliterated，native、Q6、上下文 8192、服务槽数 2。
- 原图取自本机已有的两张含文字食品照片，重复放入小测试包。3 个测试包最终完整完成，共 10 个图片 ID；其中一个包的 TAG 在线执行、NL 离线执行，再由网页合并回收。
- 这验证执行和交付流程，不构成动漫打标准确率、OCR 质量或大规模吞吐率评估。PixAI／Torii、本次未加载。
- 页面操作使用 Edge；另外实际启动了发布目录中的 Tagger.Shell，直接在其 WebView2 中打开新建任务、调度中心、模型管理页面。

## 已通过

| 功能 | 实际结果 | 证据 |
| --- | --- | --- |
| 客户端页面 | 14 个主要页面可打开，无捕获到的页面脚本异常；仅页面展示验收 | `browser-pages.json` |
| 桌面启动 | Python 服务与 WebView2 正常启动；3 个主要页面显示；关窗后对应服务退出 | `desktop-webview.json`、`desktop-webview-center.png` |
| 生产管理员页面 | 概览加载出两个仓库；工作区、包列表、设备、体检、审计可打开 | `live-panel-final.json`、`server-live-overview.png` |
| 总派单控制 | 暂停时不领取新包；网页启动后实际领取；强制继续遵守总暂停 | `online-connected.json`、`parallel-release.json` |
| 在线打标与回传 | 实际 GPU 产出、文件回传、逐图核验、分项完成、整包完成 | `dispatch-run.json`、`final-integrity.json` |
| 分片上限 | 上限 1 完成后暂停，未继续领取下一包 | `dispatch-run.json`、`online-resume.json` |
| 暂停与续做 | 退出托管保留同一租约，约 48 小时有效期；恢复后 TAG 内容保持一致，补完 NL 并回收 | `online-resume.json`、`final-integrity.json` |
| 远程并发授权 | 未授权拒绝调整；勾选后服务器并发 1 实际应用到客户端 | `parallel-release.json` |
| 停止并释放 | 第一次点击仅进入确认；取消保留租约；确认后停工并释放，本地产物保留 | `parallel-release.json` |
| 离线工单 | 独立网页登记、下载、客户端导入、真实 NL 推理、导出回收 ZIP | `offline-generate.json`、`offline-real-execution.json` |
| 网页离线回收 | 上传、核对、勾选明确确认后接受；与此前在线 TAG 合并为整包完成 | `offline-panel-import.json`、`final-integrity.json` |
| 离线工单删除 | 第二次点击才删除；删除后的已回收 TAG／NL 和整包完成状态保留 | `mcp-delete.json`、`final-integrity.json` |
| 结果下载与分享 | 管理员 ZIP 下载通过 CRC；免登录分享下载与管理员结果一致 | `result-download.json` |
| 客户端结果核验 | 分享链接实际导入 2 条；复用本地测试原包后显示原图；人工通过和核验记录导出成功 | `result-review.json`、`client-result-review.png` |
| 预派与配置记忆 | 为隔离成员保存预派；刷新页面后并发 3、画师关闭配置保留；NL 模式隐藏 TAG 批大小 | `preassignment-memory.json` |
| 画师目录发现 | 真实 Dru 仓库识别 844 张 artist 分表；样本表 9472 条，9195 条有画师；保存固定版本及两侧开关成功 | `artist-discovery.json`、`server-artist-discovery.png` |
| 本地画师补注入 | 对隔离样本注入 TAG 与 NL；重复调用不叠加；前缀冲突提示核对；强制模式替换错误前缀，原文件备份 | `artist-reinject.json` |
| tokenizer 与上限 | Qwen3.5 0.8B tokenizer，384 软目标／512 硬上限；1209 token 长文本裁到 512 且保留画师前缀 | `token-check.json` |
| 日志持久化 | 重启测试客户端后旧调度日志仍在、界面恢复旧尾部；native 日志路径与内容可重新读取，未自动开跑 | `log-restart.json` |
| 管理员 MCP | initialize、工具列表、真实概览查询成功；撤销密钥后请求 401 | `mcp-delete.json` |

画师补注入用真实分表中的两个 ID 配合已有推理文本，文件位于隔离测试目录；并未声称这些照片对应真实画师。这一项验证格式、重复执行和强制替换。注入后训练文本为 133／201 token。

客户端结果核验的原图关联使用真实本地 tar，执行现有的原图索引逻辑；虚构测试仓库没有 HF 地址，因此没有把它的原包网络下载标成通过。

## 观察到的问题与限制

1. **本地磁盘保护会停止开工。** H 盘约剩 15.6 GiB、3.3%；E 盘约剩 370 GiB，但比例仅 9.9%。当前策略要求同时满足至少 30 GiB 和 15%，所以两盘均触发停止。切换到 D 盘后真实打标通过。未删除用户数据或调低保护阈值。建议以后在模型加载前显示该原因，并评估大盘是否适合按剩余比例停止。
2. **生产统计接口曾短暂返回 503。** 03:48:28 出现 `/api/summary` 503，03:48:41 同接口自动刷新恢复 200；随后重新操作页面的接口均成功。没有获得首次失败响应中的底层错误详情，服务日志没有对应 traceback，原因未定，不能归结为某位用户网络或已经修复。后续值得完善统计请求的错误详情与重试体验。
3. **不能据此宣布所有场景无 Bug。** 尚未覆盖 4090／5090／V100 的长期高并发、完整并发探测、48 小时实际离线等待、弱网长时重连、几 GB 原图包网络断点下载、全部模型安装／量化／上传，以及管理员 MCP 的每个写工具。
4. **本次真实推理用 WD＋Qwen。** PixAI＋Qwen／Torii 组合和动漫样本质量需另行验证。本次两张照片的 OCR 文字存在猜测，不能当成文字质量验收。
5. **桌面权限回退未制造故障重测。** 当前账户的 WebView2 缓存可写，正常启动通过；其他用户 `Administrator` 的目录权限拒绝和覆盖安装场景不在本次实操覆盖范围。

前期失败中，测试 tar 缓存路径、隔离服务未配置产物目录、测试环境未配置 HF 凭据以及页面定位器不匹配，都已在测试准备中纠正。`offline-panel-import.json` 中辅助接口预览因脚本未传 Origin 被拒绝；最终网页预览与明确确认流程通过，并由数据库和重新导出的结果验证，不能将这个辅助请求误报为产品回收失败。

## 生产状态与收尾

- 前后数据库对照一致：2 个工作区、874 个分片；基础状态 869 available、3 claimed、2 completed；4 条现有 open 租约保持一致。
- 总派单仍为暂停，未对真实成员派单、释放其租约或重置管理员密码。
- 面板按完整性细分显示未认领 856、未完结 13、处理中 3、已完成 2；不与基础 available 869 矛盾。
- 服务器两项生产服务 active，label 健康接口和已认证的面板 `/api/meta` 均返回 200；服务器剩余空间约 2.3 GiB。
- 临时测试 MCP 密钥已撤销，测试分享已撤销，隔离派单暂停，无测试 open 租约。隔离服务、本机 native、测试客户端及桌面窗口均关闭。
- 临时脚本和本机测试数据归档到 `H:\lulynx-trainer\.delete\tagger-live-20261125`，保留源路径层次，文件数量与总字节数核对一致（`local-cleanup.json`）。本目录只保留验收证据，未把测试脚手架放入生产代码。
- 当前环境没有可调用的 `jcodemunch_guide`；检查依据为新鲜源码、页面、日志与数据库。

主要证据：`final-integrity.json`、`live-before.json`、`live-after.json`。测试报告中的 `ok` 只适用于对应测试范围，不能累加为全功能数量。
