# 并发测试页面 HTTP 500 修复

2026-11-26，H:/tagger。

## 原因与修复

并发测试页面打开时请求 `/api/benchmark/options`。普通 Qwen 模型的 `gguf_ready` 为精度列表，例如 `['q4', 'q6']`；Torii 固定 Q6 模型包的同名字段是布尔值。目录汇总在进入 Torii 分支之前调用 `list(True)`，只要 Torii 已准备完成就触发 `TypeError: 'bool' object is not iterable`，接口返回 500，与选择哪个模型或是否启动测试无关。

`tagger/web/benchmark/catalog.py` 现在只为普通模型读取精度列表，Torii 继续沿用既有固定 Q6 和引擎检查。没有改变共享模型管理接口、推理实现、提示词或训练文本长度限制。

## 验证

- 修改前使用 E:/dev_model/ToriiGate-0.5 的真实模型文件，仅通过进程内覆盖指定模型路径；正式用户配置未改变。真实路由返回 HTTP 500，异常与行号见 `before.json`。
- 修复后真实应用首页、options、status 均 HTTP 200；模型列表同时保留 Qwen 与 Torii。Torii native/llamacpp Q6 选择校验成功，见 `after.json`。
- 客户端自带 Python 下 36 项通过：既有 33 项并发测试回归，加 Torii 就绪/未就绪布尔字段和真实应用路由检查。见 `tests.xml`。检查未启动 GPU 模型、托管任务或远程派单。
- 这次是后端清单类型修复，没有修改或重建前端。用户机器的日志尚未提供；已复现的是与“打开页面即报错”一致、且 Torii 已下载时确定触发的缺陷。

## 更新

`dist/tagger-20261126-benchmark-http500-patch.zip` 仅含修正后的目录模块及说明。完全退出客户端后，解压到原客户端目录，覆盖同名文件并重启。已有并发测试功能的当前发行包可以直接应用；不需要重装 Python 或重新下载模型。

源码与 ZIP 哈希、CRC 校验结果见 `package.json`。编辑前文件在 `H:/tagger/.delete/benchmark-http500-20261126/before`。临时复现与测试脚本归档到 `H:/lulynx-trainer/.delete/tagger-benchmark-http500-20261126/backend/smoke/tagger-benchmark-http500-20261126`，不进入产品补丁。
