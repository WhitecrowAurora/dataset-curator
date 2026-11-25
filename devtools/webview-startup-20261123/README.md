# WebView2 启动与缓存恢复 · 2026-11-23

已修复并重新发布 Windows Shell。用户报错为默认 `%LOCALAPPDATA%/Tagger/WebView2` 目录拒绝访问；日志表明 Python 本地网页服务已经启动，错误发生在内嵌浏览器阶段。

## 已确认与限制

- 原代码固定一个用户数据目录，创建失败直接进入启动失败页面，没有备用目录和初始化超时；关窗未显式 Dispose WebView2。
- 原单文件 `dist/desktop-single/Tagger.Shell.exe` 时间为 2026-11-14 12:38:17，打包器一直取此文件。本轮重新编译；ZIP 内 EXE 与此次发布文件 SHA 一致。
- 原 AppLog 在默认日志不可写时吞掉异常，没有备用日志。已修复。
- 用户 AI 报告的孤立进程可能导致缓存占用；本机已真实验证第二个进程遇到缓存占用时恢复。无法据截图确认用户机器上的全部占用情况，也没有根据“无数字签名”断言 SDK/运行时不兼容。
- 没有删除用户旧缓存、模型或任务，也未全局杀 WebView2/Edge 进程。

## 实现

`Services/ShellStorage.cs` 提供默认用户目录、程序 run 目录和按根路径隔离的临时目录。`Services/WebViewSession.cs` 管理控件初始化、每候选目录 15 秒超时、独占浏览器缓存以及正常释放。失败后重新创建控件尝试备用目录；SDK 无法取消的迟到初始化任务会观察异常。

窗口重试复用已启动的 Python 服务并重建浏览器控件；浏览器初始化全部失败时打开系统浏览器继续使用本地服务。关窗取消启动并释放浏览器后停止本地服务，不批量结束其它应用进程。

AppLog 默认日志不可写时写程序 `run/shell.log`，再失败时写临时目录日志；错误面板展示实际日志路径。

## 验证

- `build.log`、`publish.log`：WPF build 和 self-contained 单文件 publish 通过。
- `normal.json`：真实 WPF + WebView2 导航、执行页面 JS、正常 Dispose 后浏览器进程退出。
- `acl.json`：仅测试目录写权限拒绝，自动改用第二目录，导航和进程退出通过，测试 ACL 已撤回。
- `two-processes.json`：不同进程占用第一缓存目录，第二进程成功改用第二目录，导航及正常退出通过。
- `permission.json`：候选目录同名文件冲突，自动改用第二目录通过。
- `log-fallback.json`：默认 shell.log 独占锁定时成功写到测试程序 `run/shell.log`，锁随后释放。
- `shell-real.json`：完整 Shell 使用隔离客户端数据及真实开发 Python 环境，UIAutomation 读到工作台网页 document，正常关窗通过。
- `shell-published-reopen.json`：最终 self-contained 单文件 EXE 两次启动、页面进入、关闭、立即重开均通过。
- `shell-permission-recovery.json`：最终 EXE 的真实默认缓存目录写入被拒绝，程序自动改用客户端 run/WebView2 并载入工作台、正常退出。临时拒绝 ACL 已撤回，没有删旧缓存。
- `package.json`、`patch.json`：ZIP CRC、EXE SHA、关键源码与清单一致、私有状态及凭据排除通过。

本机运行时版本 158.0.4370.33；没有在报错用户机器上实际运行，对方仍需用新 Shell 验证。

`locked.json` 为早期同进程两个 WebView 的探索结果，缓存可被同进程共享，不能当作不同进程占用恢复的证据；正式不同进程证据见 `two-processes.json`。

## 交付

- 完整包：`H:/tagger/dist/tagger-20261123-webview-startup-fix.zip`，76,762,662 bytes，SHA256 `80699efa5807cc6dc2358efc7c0ebff7b97074c2589ff6d9096c2101177a0c2f`。
- 已安装用户补丁：`H:/tagger/dist/tagger-20261123-webview-shell-patch.zip`，60,665,595 bytes，SHA256 `48f4fe735f7800c754c572961807fcd43851050db0cfe44cf663f2eecf4b0ac6`；只替换根目录 Tagger.Shell.exe 和更新说明。
- Shell SHA256 `3ca049c5154b2c8ad4d3a0db6ce0d5a8dd9618f36ee4824142bd1f4bf41df853`。

更新前关闭旧 Tagger 窗口。已有环境覆盖补丁即可，无需重装 Python；完整包同时包含此前依赖修复。

旧发布目录保留在 `H:/tagger/.delete/webview-shell-20261123/dist/desktop-single`。临时测试脚手架及隔离客户端按原结构归档至 `H:/lulynx-trainer/.delete/tagger-webview-startup-20261123/backend/smoke/tagger-webview-startup-20261123`。

参考：Microsoft Learn user-data-folder 文档以及所安装 SDK XML 对 ExclusiveUserDataFolderAccess、BrowserProcessExited 和 BrowserProcessExited 故障恢复的说明，见 `reference.json`。
