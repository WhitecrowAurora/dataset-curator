# 2026-11-23 冲突批量采用与 native 便携环境修复

## 管理员入口

服务器网页 → 工作区分片 →「设备、分项分配与核验详情」→ `shard-000015.tar` →「分片详情」。刷新网页即可使用，不要求操作员更新客户端。

支持单条／跨页勾选、本页全选、前 X 条待处理及全部待处理。全部处理在服务器查询，不受每页 100 条限制。
规则：较新／较旧产物生成时间、较长／较短正文字符数、token 较多／较少正文。
正文来自 TAG 的 tag 或 NL 的 caption/nl，忽略 JSON 元数据。token 计数使用与训练客户端一致的 Qwen/Qwen3.5-0.8B tokenizer，固定 revision `2fc06364715b967f1860aea9cf38778875588b17`；不计图片、聊天模板或特殊 token，不截断。
时间或长度不代表准确性；并列、信息缺失、正在执行或核验的项目跳过。原始回传、候选结果和决议记录保留。

复用管理员清单接口和 CSRF，不新增入口。批量操作一次事务、一次整包归并，持久 request_id 防止响应丢失后“前 X 条”重复重试选到下一批。

## 梦斋 15 分片现场验证

仓库 `MatchaCuratio/Anime-2024Dru768-8M`，分片 `shard-000015.tar`，8678 张图片，8614 条待处理 NL 冲突。
原包还有 3 条 NL 失败。此前只派 NL，TAG missing 8678 不等于本次 NL 漏做。

仅在服务器 SQLite 副本上批量采用并逐项核验；正式服务器未替管理员采用冲突，部署后仍有 8614 条待处理。

| 规则 | 采用 | 并列跳过 | 副本操作耗时 |
| --- | ---: | ---: | ---: |
| 较新时间 | 8614 | 0 | 6.50 秒 |
| 较旧时间 | 8614 | 0 | 10.88 秒 |
| 较长字符正文 | 8497 | 117 | 11.27 秒 |
| 较短字符正文 | 8497 | 117 | 12.72 秒 |
| token 较多正文 | 8373 | 241 | 26.44 秒 |
| token 较少正文 | 8373 | 241 | 30.04 秒 |

上述为当时服务器副本验证耗时，不承诺现场每次相同。全部候选保持原样，选定正文与规范快照一致，3 条失败保留且未误标整侧完成。
管理员选择后处理剩余并列，再补失败图片；不要为此全包重打。

部署备份：`/tagger/.delete/conflict-batch-20261123`、`/tagger/.delete/conflict-tokens-20261123`。
label/panel 均 active，health 200，页面脚本哈希和正式详情接口 token 数验证通过；首条两候选为 58 / 50 token。
部署证据见 deployment.json、tokens-deployment.json；数据副本验证见 remote-validation.json、tokens-remote-validation.json。

## native code=3

本次日志的直接原因为 `include file 'Python.h' not found`，并有 `Failed to find Python libs`。
便携 Python 缺少编译头文件与 Python 导入库，Triton tcc 无法编译 cuda_utils，应用启动失败后返回 code=3。
code=3 本身不是具体根因；这次不应建议用户更换显卡或缩短上下文。

新增 runtime/python_dev：使用随包官方 Python 3.12.9 x64 Include/libs，缺失时原子补齐，不覆盖已存在文件。
native 启动检查、相关依赖安装与 install.bat 环境准备均覆盖，UI 对缺 Python.h／开发库给出明确恢复提示。
资源来源为官方 Python NuGet；源码 URL、下载 SHA256 和 bundle SHA256 见 python-dev-source.json，Python 授权文件随包。
tokenizer 资源授权见服务器 text_encoding/assets/LICENSE，来源见 assets/source.json。

实际使用 Triton tcc 编译 Python 扩展：补齐前复现 Python.h not found，补齐后编译成功，并由项目便携 Python 载入，返回 42。
这是编译链验证；尚未在故障用户机器上复测整套 Qwen 模型启动，不宣称其现场已恢复。

## 验证与客户端交付

- 批量冲突原测试 26 项通过；加入 token 规则后完整 35 项通过，其中包含原 26 项。
- 依赖／冲突／便携开发文件相关回归 78 项通过，其中包含 8 项开发文件验证；与前一项合计 113 项，不重复计数。
- 后台 Edge 真实页面、现有 panel 接口、临时 SQLite 验证：登录、CSRF、分页、跨页选取、前 X 条、全选、取消确认、token 两规则、已采用状态、768px 布局，0 pageerror。
- 内置浏览器连接受工具 `missing field sandboxPolicy` 错误阻碍，使用后台 Edge 完成页面验证。浏览器关闭时单次取消后台 metadata 请求造成测试服务连接中止，不影响验证结果。
- UI build 通过，git diff --check 通过。jcodemunch_guide 当前工具未提供，没有声称调用。

客户端包：`H:/tagger/dist/tagger-20261123-conflicts-native.zip`

- 大小：77,001,499 bytes。
- SHA256：`a61e6be46ab2b52e30d89b52cd1eecd53fa959960a7bea22d79c21c2012bf17b`。
- ZIP 完整性、修复源码／构建 UI／Python 开发资源入包核验通过；本机 models/work/run/tmp、个人配置和已知凭据排除。
- 包含前轮调度代理功能。服务器冲突页面独立部署，客户端包无需携带服务器 tokenizer。

旧客户端升级：关闭程序，将新版程序内容更新到原程序目录，保留原 work、个人 configs、models、run 和 env/python；新版 env/initialization 必须完整带上。重新启动 native 会自动补齐开发文件。不要删除身份、租约或本地产物。
全新解压运行 install.bat 安装依赖后再启动。此包不含完整 env/python。

变化记录：changes.patch 来自实时服务器旧源码；client-changes.patch 来自上轮 proxy ZIP，不使用 Git 回退。
临时测试脚手架完成后归档到 `H:/lulynx-trainer/.delete/tagger-conflicts-20261123/backend/smoke/tagger-conflicts-20261123`，生产依赖和本机补齐后的 Include/libs 保留。
