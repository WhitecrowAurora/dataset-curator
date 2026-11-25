# lulynx 调度中心 AI 管理接口

管理员在服务器网页顶部点击「AI 接口」，创建独立密钥。可以选择仅查询或查询及操作，设置1～365天有效期，随时撤销。密钥只显示一次，不使用 SSH 密码或网页口令。

支持远程 MCP Streamable HTTP（JSON 响应，无常驻 SSE）。当前部署地址为 `http://119.28.16.93/mcp`，配置请求头 `Authorization: Bearer <独立密钥>`。客户端须发送 `Accept: application/json, text/event-stream`，协议版本支持2025-06-18与2025-03-26；部分客户端仅允许 HTTPS，需要自行按该客户端的连接要求配置。

支持查询总速度、分包总统计、工作区、完整调度配置、设备、分片占用、离线工单与自动派单状态；操作工具支持自动派单启停、用户预派单与撤销、开始／强制继续／停止、TAG批大小／NL并发调整、并发探测、租约释放、离线工单生成与删除。服务器继续检查租约、冲突、预派要求和本机远程并发授权。AI不获得任意HTTP路径、系统命令、上游密钥或离线成员凭据。

当前共 16 个工具，仅查询密钥可见 7 个查询工具。更新后重新连接 AI 客户端刷新列表；连接地址和现有密钥保持有效。

只支持 stdio 的 AI 客户端可使用 `bridge.py`，Python 3.10+，无需额外安装依赖：

```json
{
  "mcpServers": {
    "lulynx_center": {
      "command": "python",
      "args": ["C:/你的目录/mcp/center/bridge.py"],
      "env": {
        "LULYNX_CENTER_MCP_URL": "http://119.28.16.93/mcp",
        "LULYNX_CENTER_MCP_KEY": "填写网页生成的独立密钥"
      }
    }
  }
}
```

创建预派前先查询 `center_packages` 的可派分项。启停和调整调用 `center_device_command` 时，使用最新设备会话ID，给 `request_id` 一个唯一编号，网络重试沿用该编号。生成离线工单后管理员在网页下载，AI只返回工单ID。

`center_scheduling` 查询用户、分组、设备仓库范围、分项指定和租约，帮助区分用户预派、设备指定和离线占用。

撤销尚未领取的用户预派调用 `center_cancel_preassignment`，参数例子：

```json
{
  "ws_id": "Dru768",
  "items": [{"repo": "MatchaCuratio/Anime-2024Dru768-8M", "id": "shard-000027.tar"}],
  "mode": "nl"
}
```

`mode` 可为 `tag`、`nl`、`both`。一个批次最多 500 个分片，任一所选分项已在执行或预锁，整批不修改。已撤销项重复提交返回取消数量 0。撤销保留已回传产物、画师设置、另一侧分项、设备指定、原设备队列、离线工单、仓库权限和工作区默认模板。它不停止设备或暂停总派单；若不希望这些分片再被自动领取，先用 `center_dispatch_control` 设置 `running=false`。

正在执行或预锁的分项走下面的租约释放工具；离线占用走 `center_offline_delete`。目前未开放工作区／仓库增删、设备定向队列清理、结果下载分享和冲突批量处理等网页操作，不能用撤销用户预派代替这些动作。

工作区默认模型与量化要求先用 `center_workspaces` 读取 `preassignment_template`，再调用 `center_workspace_template` 保存完整模板，例如：

```json
{
  "ws_id": "Dru768",
  "action": "save",
  "template": {
    "mode": "nl",
    "config_source": "specified",
    "config": {"nl_model": "torii-gate-0.5", "nl_engine": "llamacpp", "nl_precision": "q6", "concurrency": 4},
    "annotation": {}
  }
}
```

`action: "clear"` 不携带 `template`，清除默认模板及上次派单记忆。模板仅影响后续预派单，已有任务和客户端配置不会被改写。`center_preassign` 与 `center_offline_export` 的 `config` 也支持 TAG 模型/精度、NL 模型/引擎/量化和吞吐参数。设备 `configure` 仍仅支持已授权的并发调整。

模型、量化要求不匹配时等待用户调整。并发及 TAG 批大小是吞吐建议：未授权远程调整时沿用本机值并提示，不阻断接单；已授权时通过原有顺序指令调整。超出本机引擎能力时由客户端按实际能力执行，显示配置值和实际值，不因此中断托管轮询。

`center_dispatch_state` 查询在线派单总状态；`center_dispatch_control` 的 `running` 控制启动／暂停。新版本首次上线默认暂停，管理员在网页顶部点击「启动派单」。暂停保留当前包和原预锁记录，阻止新包与预取包下发、预锁升级；已有离线工单可以继续，保存预派也不受影响。启动不取消某台设备的人工暂停。

设备离线时也可下发 `stop`，服务器保存停工意图，重连后执行；停止设备保留其租约，回收租约另行操作。释放前调用 `center_release_preview`，确认返回的租约列表和 `impact`；再用相同 `lease_ids`、`reason` 调用 `center_release`，带上 `confirmation`、实际 `confirmed_count` 和唯一 `request_id`。确认过期或租约变化需要重新预览，重试沿用原 `request_id`。释放保留已回传产物，并暂停原设备接单。

删除离线工单会撤销未完成占用，已回传结果与审计仍保留。成员完全离线时不会立即收到撤销，管理员应先通知他停止，避免继续做已重新分配的图片。
