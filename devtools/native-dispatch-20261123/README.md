# native 缓存与调度派单修复（2026-11-23）

## 修复内容

1. Windows native 启动：用户日志中的 `WinError 5` 发生在 Triton 写入用户目录缓存时，属于本机文件权限问题；code=3 是服务启动失败的退出码。现在在导入模型／Triton 前选择可写缓存，默认使用程序 `run/cache/triton`。可写的自定义 `TRITON_CACHE_DIR` 仍然保留；默认位置不可写时依次尝试 LOCALAPPDATA 与临时目录中的程序专属路径。不会删除旧缓存或修改 ACL。
2. 管理员离线派单：增加 native 引擎，复用同一执行表单用于离线清单与分派表。按输出内容、TAG 识别、NL 描述分类，有字段标签、引擎对应精度与手机布局。只出 NL 隐藏 TAG，只出 TAG 隐藏 NL。原默认配置与派单协议保留。
3. 客户端实时执行：根据当前分包实际模式显示并发／批大小；NL 任务只显示 NL 并发。后台保存的 TAG 批大小仍保留，方便以后切回 TAG，不意味着当前会执行 TAG。NL-only 准备链路不会检查或加载 TAG 模型。

## 验证

- `tests.xml`：11 项全部通过，包括缓存权限／路径回退、父环境不变、磁盘不足报告、native 子进程启动顺序、NL-only 不触发 TAG，以及真实 Triton C 扩展编译、缓存写入与再次加载。
- 前端 TypeScript 与 Vite 构建通过。构建资源为 `index-BQ5hxcb1.js`、`index-7Ebx__7j.css`。
- `browser.json`：隔离的真实 HTTP 服务与 Edge 验证 native/Q6/NL 清单导出，任务仅包含 NL；分派表导出与 NL/TAG/双输出状态显示通过，页面错误为空。
- `candidate-validation.json`：服务器现有数据库副本完整性通过，18 条现有分派分别按用户／包名导出 HTML、JSON、ZIP，租约未变化。
- `live-browser.json`：生产页面暗色模式、390px 手机宽度、三种输出模式字段显隐、native 选项、顶部速度显示通过，无横向溢出或页面错误。仅浏览与切换表单，没有向真实用户导出／应用任务。
- `deployment-verified.json`：六个生产文件与候选 SHA256 相符，四个公网静态资源与源码相符，公网 health 正常。仅重启 panel；label PID 26437 保持不变，panel 新 PID 30496。
- `live-validation.json`：生产接口与数据库结构核验。
- `package.json`：ZIP CRC、最新源码／界面／同源 worker、凭据扫描和用户状态排除通过。

GPU 检查时本机显卡已有高负载，未启动模型占用 GPU。本轮真实 Triton 编译测试覆盖用户失败的编译／缓存写入链路；没有在报错用户的 5090 上完成整个模型启动验收。缓存初始化后若被安全软件动态阻断，需要依据新日志继续定位。

## 部署和发行

服务器备份与候选目录：`/tagger/.delete/native-dispatch-20261123`，包含精确文件备份、baseline 与 SQLite 在线备份。本轮只更新六个 panel 文件，未恢复生产数据库或变更真实派工。

部署后的首次静态检查碰到 panel 重启窗口的短暂 502，记录保留于 `deployment.json`。随后源码、公网静态资源、健康接口与生产浏览器均验证正常，结果见 `deployment-verified.json` 与 `live-browser.json`。

客户端包：`H:\tagger\dist\tagger-20261123-native-dispatch-fix.zip`

- 大小：77,060,005 字节。
- SHA256：`1c7adb70d750022fa34e841b4260b8c5b97101107fe9ad475f474597c9963f88`。
- 不含模型、env/python、work、run、用户配置、服务器源码和密钥；包含初始化与 Python 开发文件补齐资源。

管理员按 Ctrl+F5 刷新网页。客户端升级时先停止任务并关闭客户端，再将新版程序覆盖到原目录，保留 env、models、work、run 和原有个人配置，重启后生效。首次安装按包内说明运行 install.bat；native 需要完整推理依赖。

native 日志会打印 `[native] Triton 缓存：...`，用于确认新路径。此次修复不需要删除旧 `.triton` 或默认以管理员权限启动。

临时测试与部署脚本测试完成后归档到 `H:\lulynx-trainer\.delete\tagger-native-dispatch-20261123\backend\smoke\tagger-native-dispatch-20261123`，不留在源码或发行包。
