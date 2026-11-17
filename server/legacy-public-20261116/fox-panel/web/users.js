/* 用户目录和登记设备计数；操作资格由各列表的工作区决定。 */
(function () {
  'use strict';
  function catalog(all, workspaceId) {
    var users = all.users || [], accounts = new Map(users.map(function (u) { return [u.user_id, u]; }));
    var counts = new Map(), devices = [], groups = new Map();
    (all.devices || []).forEach(function (d) {
      if (d.device_id.startsWith('offline-')) return;
      counts.set(d.user_id, (counts.get(d.user_id) || 0) + 1);
      var u = accounts.get(d.user_id);
      if (!u || u.status !== 'active' || ['blocked', 'archived'].includes(d.status) ||
          (workspaceId !== undefined && d.effective_workspace_id !== workspaceId)) return;
      devices.push(d);
      if (!groups.has(d.user_id)) groups.set(d.user_id, []);
      groups.get(d.user_id).push(d.device_id);
    });
    var names = new Map((all.workspaces || []).map(function (w) { return [w.ws_id, w.name || w.ws_id]; }));
    var items = users.map(function (u) {
      var workspace = u.effective_workspace_id || u.workspace_id, ids = groups.get(u.user_id) || [];
      var inactive = !!u.status && u.status !== 'active', detail = [];
      if (inactive) detail.push('已停用');
      if (!workspace) detail.push('未分配工作区');
      else if (workspace !== workspaceId) detail.push('工作区：' + (names.get(workspace) || workspace));
      if (workspaceId !== undefined) detail.push(ids.length ? '当前工作区可用设备 ' + ids.length + ' 台' : '当前工作区无可用设备');
      return {id: u.user_id, label: u.user_id, meta: '设备 ' + (counts.get(u.user_id) || 0) + ' 台',
        detail: detail.join(' · '), disabled: inactive || (workspaceId !== undefined && !ids.length),
        action: workspaceId !== undefined && !workspace && !inactive ? '指派到此工作区' : ''};
    });
    return {items: items, devices: devices, groups: groups,
      signature: JSON.stringify([items, devices.map(function(d) { return [d.device_id, d.user_id]; })])};
  }
  function mount(parent, config) {
    return window.FoxSelection.mount(parent, Object.assign({searchMode: 'prefix',
      placeholder: '输入用户名开头筛选', empty: '暂无注册用户', noMatch: '没有匹配的用户名'}, config));
  }
  window.FoxUsers = {catalog: catalog, mount: mount};
})();
