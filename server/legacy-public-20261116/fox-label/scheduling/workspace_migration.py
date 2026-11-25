"""合并相同仓库和策略的旧工作区，保留分片与租约身份。"""
from .transitions import atomic
import json


@atomic
def merge(c, dispatch, source_id, target_id):
    source_id, target_id = dispatch._ws_id(source_id), dispatch._ws_id(target_id)
    if source_id == target_id:
        raise ValueError('请选择两个不同的工作区')
    source, target = (dispatch.ws_get(c, key) for key in (source_id, target_id))
    if not source['repos'] or source['repos'] != target['repos']:
        raise ValueError('只能直接合并仓库范围和排序相同的旧工作区')
    for field in ('allocation_mode', 'allow_manual_claim', 'preassignment_template', 'preassignment_last'):
        if source[field] != target[field]:
            raise ValueError('工作区派单设置不同，请先明确保留哪份配置：' + field)
    # 已导出分派表的工作区参与指纹；离线导入预览也按原工作区核验。
    # 这些记录需要单独迁移，不能通过改 JSON 或改设备归属使其静默失效。
    for row in c.execute('SELECT manifest FROM dispatch_plans'):
        if json.loads(row['manifest'])['workspace']['ws_id'] == source_id:
            raise ValueError('来源工作区仍有已导出分派表，需先迁移其应用记录')
    if c.execute('SELECT 1 FROM offline_jobs j JOIN devices d USING(device_id)'
                 ' WHERE d.workspace_id=? LIMIT 1', (source_id,)).fetchone():
        raise ValueError('来源工作区仍有离线工单，需先迁移其回收记录')

    moved = {}
    for table in ('users', 'devices', 'user_groups'):
        moved[table] = c.execute('UPDATE ' + table + ' SET workspace_id=? WHERE workspace_id=?',
                                 (target_id, source_id)).rowcount
    for table, member in (('repo_devices', 'device_id'), ('repo_users', 'user_id')):
        moved[table] = c.execute('INSERT OR IGNORE INTO ' + table + '(ws_id,repo,' + member + ')'
                                 ' SELECT ?,repo,' + member + ' FROM ' + table + ' WHERE ws_id=?',
                                 (target_id, source_id)).rowcount
        c.execute('DELETE FROM ' + table + ' WHERE ws_id=?', (source_id,))
    # 继承自动接单次序，保留原创建时间；合并后不会改派到较新的测试仓库。
    priority = max(row.get('auto_assign_at') or row['created_at'] for row in (source, target))
    c.execute('UPDATE workspaces SET auto_assign_at=? WHERE ws_id=?', (priority, target_id))
    c.execute('DELETE FROM workspaces WHERE ws_id=?', (source_id,))
    # 仓库范围、策略和许可等价，不运行全局 reconcile 以免清理无关预约。
    return dict(source=source, target_id=target_id, moved=moved, auto_assign_at=priority)
