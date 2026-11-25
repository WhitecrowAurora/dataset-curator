# 管理员新派单与离线工单的占用筛选

2026-11-24 已部署。

执行层原本统一读取 lease_parts 的开放占用，并检查预派指定、手动队列和整包指定。idx_part_open 确保同一仓库、分片、TAG/NL 分项只有一个开放租约。离线导出成功即创建 offline 租约；草稿编辑不占包。未成功生成清单的事务回滚，已正式导出的离线任务不会因 48 小时在线超时释放。

本轮确认并修复两处配置页缺陷：

1. 管理员列表的 claimable_parts 未扣除已预派但尚未领取的指定。新增 unassigned_parts 和 reserved_user，在快速配置的新派单中只选择未指定的可用分项。原领取判定及高级指定操作保留。
2. 离线页仅按 status/leases 判断，且查询结束的统一 update 又重新启用了包复选框。现同时检查预派指定、整包指定、设备队列、租约和待核验，并在更新时保持已占用包禁选。

整包离线导出对已占用的包保守禁选；在线分项调度仍允许 NL 和 TAG 由不同设备分别完成。例如 NL 已离线派出时，自动领取可以领取空闲 TAG，不能重复领取 NL。

新派单列表不会自动覆盖已指定任务。高级修改原分配仍按原服务端校验执行。同一批次包含已失效任务时后端可能拒绝本次请求，管理员刷新后重新选择；不静默改变工单范围。

上线文件：fox-label/scheduling/packages.py、fox-panel/web/dispatch-assistant.js、fox-panel/web/offline-workorders.js。

备份：`/tagger/.delete/allocation-selection-20261124/before`。

验证：auto-workspace 报告中的 36 项测试与管理页实际 HTTP/浏览器验证通过，包括离线 NL 占用、自动 TAG 分项选择、重复离线导出拒绝、手动分项/整包/设备队列/active/prelock 的列表筛选，以及查询后禁选状态保持。

`live.json` 为上线后只读检查：新增字段存在、生产开放分项重复 0、两项服务 active、源码哈希匹配。

管理员刷新网页即可使用。原任务、离线工单和执行租约未取消或重新分配。
