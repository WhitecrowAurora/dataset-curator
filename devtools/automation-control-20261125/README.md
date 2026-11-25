# 自动派单控制、离线停工与训练 tokenizer

2026-11-25，工作区 `H:\tagger`。本轮服务器修复已部署，按用户明确选择首次设为暂停；客户端更新包含在本轮完整包和覆盖补丁中。

## 调查结论

本轮只读调查时 874 个分片中 869 个 available、3 个 claimed、2 个 completed；重复 open 分项租约为 0。`package_queue` 为空，queued／accepted 托管指令为空，两个工作区均采用 auto 分配。未找到所谓“双队列”或重复 open 租约的证据。

Draco_Echo 的 6／7 分别是当前包和预锁。N叔在此前停止后出现 `N叔:start` 策略更新，并领取 10／294。这确认了在线启停控制的缺口，但不能把截图猜测直接当成重复派发的确诊。294 在本轮真正部署之前已经不再 open，本轮没有代释放；其关闭原因未在本轮追查。

## 最终行为

- 网页顶部常驻“自动派单：已暂停／运行中”和“启动派单／暂停派单”，5 秒刷新真实服务器状态。读取失败明确显示，不假装暂停。开关写入带 CSRF 和审计；刷新、服务重启保留管理员选择。首次迁移默认暂停。
- 暂停阻止在线新租约、预取包下发及预锁升级；已有 active 租约可继续心跳、回传和登记。DB 中已有预锁保留但不交给 worker。离线工单和保存预派仍可用。启动总开关不清除单台设备的暂停。
- 离线设备也可下发 stop，服务器保存停工意图；原会话或新的重连会话继续执行。保持身份校验，停用设备不可操作；停止不释放租约。
- MCP 保留旧工具，新增 `center_dispatch_state`、`center_dispatch_control`、`center_release_preview` 和 `center_release`，共 13 个。释放复用现有预览／数量确认／影响校验／幂等机制；保留已回传结果，暂停原设备。工具列表不推送变更，AI 客户端须重连刷新。
- 客户端模型管理增加 Qwen3.5-0.8B tokenizer 下载／补齐、刷新和训练正文 token 检查，按已保存训练模板计算 caption 和 tag，软目标 384、硬上限 512。固定 `Qwen/Qwen3.5-0.8B` revision `2fc06364715b967f1860aea9cf38778875588b17`；仅下载 tokenizer 与模板，沿用 HF 下载源和代理，不覆盖保存的训练配置。
- 顶部控制区及表格在桌面、平板和手机正常显示；修正设备表表头错位和空执行限额显示 undefined。新 automation.js 已加入 panel 静态路由，实际 HTTP 检查通过。

## 验证证据

测试使用 `H:\lulynx-trainer\backend\env\python-flashattention\python.exe`，仅在临时 trainer `backend/smoke/tagger-automation-20261125` 创建验证代码。

| 验证 | 结果与范围 |
| --- | --- |
| `regression.xml` | 105 项通过，0 失败／错误／跳过，覆盖自动接单、恢复、离线 stop、释放、配额、客户端控制器及旧设计兼容 |
| `acceptance.xml` | 4 项通过，实际 panel 静态资源与 CSRF 启停、MCP 批量释放、确认过期／影响变化、离线 stop 后换会话重连 |
| `browser.json` | Edge headless 访问构建后客户端及真实隔离 panel；官方固定 revision 全新 tokenizer 下载、计数、超 512 提示、刷新、训练配置保留和启停均通过。该文件的布局值为修复前记录，最终布局以后一项为准 |
| `browser-layout.json` | 1440／1280／1024／768／390 宽度下页面无横向溢出；开关位于顶部，离线可停止、停用设备不可停止，无页面异常。手机工具行允许行内横向滚动 |
| `linux-validation.json` | Linux 隔离 DB、label／panel 的 8 项检查通过，覆盖暂停、恢复同一预锁、心跳／finish、离线 stop、MCP 释放和离线工单。第 8 项用错误直接分配路由测试请求不改变数据；真正直接分配暂停前不替换预锁的行为由专项回归覆盖，不将第 8 项当成正确路由的完整验收 |
| `deploy.json` | 21 个部署文件与候选 hash 相符，两服务 active；874 包、23 份离线工单历史、1 份分派表、2 个工作区和部署当时 4 条 open 租约保持一致，重复分项 0，真实租约释放操作 0 |
| `external.json`、`live-final.json` | 部署后的外网 HTTP／nginx、顶部静态脚本和 MCP 查询检查；不操作真实成员派单、启停或释放，临时接口密钥撤销 |
| `package.json` | 完整包／覆盖补丁 CRC、当前源码、29 份 worker 内嵌源码、构建 UI、自带运行时的窗口程序及私人配置排除均通过 |

Browser 技能已读取，in-app browser 因 `sandboxPolicy` 元数据缺失无法启动，使用本机 Edge／Playwright headless 验证。`control.xml`、`overflow.json` 保留早期调试证据，不作为最终验收；确认重复使用等测试脚本问题修正后已在最终回归通过。静态文本响应按 panel 既有换行规范化后比较，部署源文件另按字节 hash 校验。

没有运行真人 GPU 打标或隔夜测试。已发送给客户端的请求、accepted 开始指令和完全离线客户端无法被总开关瞬间撤回；现有包崩溃时处于暂停状态不会自动看门狗恢复，等管理员启动。显式新 start／force 与历史请求跨会话的所有竞态未做无限时序验收，不能承诺零 bug。

## 部署与交付

服务器 `119.28.16.93`，备份根 `/tagger/.delete/automation-control-20261125`。SQLite 固定读取快照后 backup，未恢复旧整库或替换新回传；依次重启 label、明确暂停、重启 panel。线上总开关当前状态以 `live-final.json` 为准，管理员在顶部检查后点“启动派单”。

2026-11-25 01:33 最后核对：总开关仍暂停，3 个 active、1 个 prelock、pending start 为 0，暂停后新在线租约为 0。874 个分片中整包完成 2，Dru768／Fox768Test 均存在，重复 open 分项为 0。已通过外网直接 HTTP 和 nginx 检查；线上仅查询密钥返回 6 个查询工具，操作权限密钥返回完整 13 个工具，两种临时密钥随后均已撤销，没有对真实成员执行操作。

- 完整客户端：`H:\tagger\dist\tagger-20261125-automation-control-fix.zip`。
- 累计覆盖补丁：`H:\tagger\dist\tagger-20261125-automation-control-patch.zip`，包含当前客户端程序、UI、独立 worker 和自带 .NET 的 Shell，保留前几轮回传／移动目录／Torii 兼容修复。
- 已安装用户先停止打标并关闭客户端和 native 服务，再覆盖补丁；保留环境、模型、个人配置和产物。新安装用户用完整包，先运行 `install.bat`。SHA256 和字节数见 `package.json`。

临时脚本和隔离数据完成后移至 `H:\lulynx-trainer\.delete\tagger-automation-20261125\backend\smoke\tagger-automation-20261125`，保留原目录结构；正式报告目录只保存说明、修改前备份和验证结果。未执行 Git reset／stash／push，未从 Git 恢复文件。当前工具列表没有 `jcodemunch_guide`，结论基于当前源码、请求与运行记录。
