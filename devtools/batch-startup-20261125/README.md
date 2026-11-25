# Windows 启动脚本乱码命令修复

2026-11-25，`H:\tagger`。

截图中的三条“不是内部或外部命令”已在本机重现：UTF-8 编码的 `tagger.cmd` 含中文注释，cmd.exe 在中文代码页下会错误解析部分注释并执行文字片段。`run-web.cmd` 则使用 GBK，两个批处理入口编码不一致。截图末尾已打印本地 UI 地址，说明这组批处理错误未阻止 Python Web 服务启动；不能据此判断模型或 GPU 失败。

两个入口改为纯 ASCII、CRLF，不改变 Python 选择、目录切换、CLI 转发、浏览器打开和 Web 端口逻辑；中文 UI 与 Python 日志保持原样。原脚本备份在 `H:\tagger\.delete\batch-startup-20261125\before`。

验证使用真实 cmd.exe 隐藏控制台，分别设置代码页 936 和 65001：两组 CLI `--help` 均正常、stderr 为零；两组 `run-web.cmd` 启动实际 Web 服务，`/api/health` 和主页均 200，无乱码命令错误。测试前本地调度为不自动恢复；测试没有启动模型、领取分片或调整服务端。测试服务均已退出。

小补丁 `dist/tagger-20261125-startup-encoding-patch.zip` 只包含 `tagger.cmd` 与 `run-web.cmd`。关闭该命令窗口后解压到原程序根目录覆盖，再运行 `run-web.cmd`；无需重装环境。现有累计压缩包仍需配合此小补丁，未改写旧归档。

验证证据见 `validation.json`、`package.json`。临时脚手架和测试日志归档在 `H:\lulynx-trainer\.delete\tagger-batch-startup-20261125\backend\smoke\tagger-batch-startup-20261125`。生产不加入测试代码。`jcodemunch_guide` 不在当前工具列表，本次使用脚本原始字节和真实 cmd.exe 验证。
