# MCP 撤销预派与查询补齐

2026-11-25，工作区 `H:\tagger`。已部署服务器，MCP 版本 1.1.0。

## 功能

- 新增 `center_cancel_preassignment`：复用网页 `/admin/v1/workspace-dispatch` 的 `cancel_preassignment` 操作，按 TAG、NL 或两项撤销尚未领取的用户分项预派。参数为 `ws_id`、`items`（仓库与分片 ID）、`mode`，单批最多 500 个分片。
- 任一选中分项正在执行或预锁时，整批撤销失败且不修改预派。保留已回传结果、未选中的另一项、画师设置、模板、设备指定、设备队列、离线工单和仓库权限。已撤销且未重新预派的项重复提交返回取消数量 0。
- 撤销不停止设备或暂停自动派单；总派单运行时，分片仍可能再次分配。需要阻止重新派单时先暂停总派单；执行／预锁包使用已有租约预览和释放工具，离线工单使用已有删除工具。
- 新增只读 `center_scheduling`，查询成员、分组、设备仓库范围、用户／设备分项指定、租约和派单状态。它不返回设备定向队列的完整明细。
- `center_overview` 补上分包总统计，保留原 health 和 speed 字段，使用现有 15 秒统计超时。
- 现有 13 个工具增至 15 个，查询密钥可见 7 个。连接地址和现有密钥不变；AI 客户端需重新连接刷新工具列表。网页说明及可下载桥接说明同步更新，stdio 桥接代码逻辑不变。

仍未开放到 MCP 的网页功能包括工作区／仓库增删、默认模板编辑、设备定向队列清理、结果下载分享和冲突批量处理。本轮未增加这些操作，也未开放任意 HTTP 路径或系统命令。

## 验证

- 本轮实际 HTTP 专项 10 项通过；完整回归 158 项通过，0 失败、0 错误、0 跳过。
- 覆盖 TAG／NL／两项撤销、另一侧分项和产物保留、重复取消、批次事务回滚、执行／预锁／离线占用拒绝、工作区匹配、队列和模板保留、只读密钥及只读面板权限、参数校验、MCP 审计和总统计。
- Linux 候选代码在独立数据库验证 15／7 工具列表、NL 撤销、预锁批次回滚、权限、审计、统计以及派单和租约保持。隔离服务已关闭，测试密钥已撤销。
- 生产外网 `http://119.28.16.93/mcp` 的工具列表、调度查询和总览均返回 200；总览统计 874 个分片。网页说明和桥接 ZIP 下载验证通过。
- 仅重启 `lulynx-panel`；label PID 保持，两个服务均 active。部署前后 open 租约、包状态、工作区、分项预派、设备队列和总派单设置完全一致。总派单仍暂停，未对生产成员调用撤销、派单或释放。临时验证密钥已撤销。

## 文件与备份

服务端修改 `fox-panel/ai_access/tools.py`、`ai_access/protocol.py`、`web/ai-access.html`、`web/center-mcp.zip`。服务器备份在 `/tagger/.delete/mcp-preassignment-20261125/before`；本地原桥接 ZIP 保存在 `H:\tagger\.delete\mcp-preassignment-20261125\before`。

验证证据见同目录 `linux.json`、`deploy.json` 和 `validation.json`。临时测试代码和数据库归档在 `H:\lulynx-trainer\.delete\tagger-mcp-preassignment-20261125\backend\smoke\tagger-mcp-preassignment-20261125`，不留在活跃测试目录或生产入口。

本轮是服务器 MCP 更新，未生成新客户端整包。当前工具目录没有 `jcodemunch_guide`，源码和实际 API 验证用于本次检查。
