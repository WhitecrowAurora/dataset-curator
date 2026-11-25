# lulynx 调度中心 AI 管理接口

管理员在服务器网页顶部点击「AI 接口」，创建独立密钥。可以选择仅查询或查询及操作，设置1～365天有效期，随时撤销。密钥只显示一次，不使用 SSH 密码或网页口令。

支持远程 MCP Streamable HTTP（JSON 响应，无常驻 SSE）。当前部署地址为 `http://119.28.16.93/mcp`，配置请求头 `Authorization: Bearer <独立密钥>`。客户端须发送 `Accept: application/json, text/event-stream`，协议版本支持2025-06-18与2025-03-26；部分客户端仅允许 HTTPS，需要自行按该客户端的连接要求配置。

支持查询总速度、工作区、设备、分片占用、离线工单；操作工具支持用户预派单、开始／强制继续／停止、TAG批大小／NL并发调整、并发探测、离线工单生成与删除。服务器继续检查租约、冲突、预派要求和本机远程并发授权。AI不获得任意HTTP路径、系统命令、上游密钥或离线成员凭据。

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

删除离线工单会撤销未完成占用，已回传结果与审计仍保留。成员完全离线时不会立即收到撤销，管理员应先通知他停止，避免继续做已重新分配的图片。
