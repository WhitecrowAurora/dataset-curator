# 调度中心并发、下载状态与 MCP 模板修复

2026-11-25，工作区 H:\tagger。

## 结果

- Torii 的模型/引擎/量化兼容性继续校验；并发不再使用 Torii 引擎上限拒绝托管轮询。服务器通用整数范围保持当前工作区的 0～256（来自并行容量工作）。
- 预派的 NL 并发、TAG 批大小作为吞吐建议，不参与模型匹配。不允许远程调整或不支持调整的客户端可按本机配置接单，页面显示建议与实际值；已授权客户端继续通过顺序指令调整。
- Torii llama.cpp 页面可输入大于 1 的值，提示实际使用 1；运行时沿用引擎能力解析并记录配置值与实际值，不因差异拒绝托管。
- 下载失败原先会因 resume_supported 自动进入回传，读取不存在的 tar 并覆盖原始 curl 错误。现在下载失败及下载中断不回传，状态区分 downloading / download_failed / processing；有效源上的打标失败仍可回传成功部分。
- HTTP 400 等托管拒绝显示服务器具体理由，保留 HTTPStatusError 供原重试/退出逻辑使用，并脱敏。
- MCP 新增 center_workspace_template，复用网页已有工作区模板 API，支持保存/清除后续预派的模型、引擎、精度、吞吐建议与画师设置。已有任务不随模板改写。center_preassign 与 center_offline_export 同时开放模型/精度字段，离线导出保留显式配置。设备 configure 仍只调整已授权吞吐参数。

## 验证

- tests.xml：最终 44 项通过，0 失败、0 错误、0 跳过。覆盖轮询/接单、量化拒绝、未授权并发差异、授权顺序调整、真实本机 HTTP 的 MCP 模板保存/继承/清除及只读权限、离线配置、下载失败/中断和部分回传。
- 真实 curl 对隔离 HTTP Range 服务：HTTP 403 可留下零字节分段；恢复后续传结果与源字节完全一致。403 只是测试场景，不代表用户的真实下载原因。
- TypeScript/Vite 构建成功；仅已有大 chunk 提示。内置浏览器因 sandboxPolicy 元数据错误不可用，使用本机 Edge headless 验证。
- browser.json 对最终补丁中的页面构建验证：并发输入 16 保留并显示实际 1 警告；下载失败及原始错误显示；1440/1024/768/390 宽度无横向溢出，0 页面异常。截图保留在本目录。

## 服务器

已部署至 119.28.16.93 的 /tagger/source，共 11 个相关文件。server-plan.json 记录候选和上线前 SHA256，before/ 与 *.diff 保存审阅依据；远端备份在 /tagger/.delete/center-issues-20261125/before。

deploy.json：lulynx-label 与 lulynx-panel 重启后均 active，前后 8 条 open 租约完全一致，工作区模板未变化，无重复 open 分项。没有主动启停真人设备或修改模板。

server-verification.json：健康接口成功，面板 HTTP 200，MCP 未授权请求 HTTP 401；部署模块实际接受 Torii llama.cpp 并发 256，拒绝不兼容量化；MCP 1.2.0，操作角色 16 工具、只读角色 7 工具，11 文件 SHA256 匹配。MCP 模板的真实认证 HTTP 写入测试在本机隔离实例完成，未对生产模板试写。

## 客户端交付

dist/tagger-20261125-center-issues-patch.zip 为定向补丁（约 541 KiB），适用于 2026-11-25 当前客户端及 torii-throughput 版本。停止打标并退出客户端/模型服务后覆盖原安装根目录，再重启。无需重装环境或模型。

补丁包含 fox_worker.py、center_runtime/inference.py、center_runtime/managed.py 与本轮已验证的前端构建；不含个人配置、凭据、模型、产物。前端采用并行 native 容量工作之前的已验证构建，当前工作区新构建保持原样，另一会话的容量/投影/内核改动未纳入此补丁。package.json 包含逐文件 SHA256、整包 SHA256 与 ZIP CRC 验证结果。

管理员重新连接 MCP 客户端刷新工具列表，地址与现有密钥不变；用 center_workspaces 查询后，通过 center_workspace_template 设置后续预派默认模型/引擎/量化。

用户实际下载失败的网络原因仍未知，需其原始下载日志；本轮修正的是错误归类与误回传流程，未宣称已经修好其网络。未进行本轮 GPU 性能或高并发显存验收。

测试、浏览器与 SSH 脚手架归档至 H:\lulynx-trainer\.delete\tagger-center-issues-20261125\backend\smoke\tagger-center-issues-20261125；不进入产品。没有 Git push/reset/stash 操作。工具目录不存在 jcodemunch_guide，使用直接源码与运行证据核对。
