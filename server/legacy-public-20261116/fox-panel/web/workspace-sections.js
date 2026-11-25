/* 工作区展示分区；只切换视图，不提交任务或修改配置。 */
(function () {
  'use strict';
  function node(tag, text) { var e = document.createElement(tag); if (text) e.textContent = text; return e; }
  var nextId = 0;
  window.FoxWorkspaceSections = {mount: function (root, card, ws, initial, changed) {
    var prefix = 'workspace-view-' + (++nextId), tabs = node('div'), panels = {}, buttons = {}, originals = [];
    var labels = {overview: '概况', dispatch: '派单', results: '结果与核验', settings: '设置'};
    tabs.className = 'fp-workspace-tabs'; tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', '工作区功能'); root.append(tabs);
    Object.keys(labels).forEach(function (id) {
      var button = node('button', labels[id]), panel = node('section');
      button.type = 'button'; button.id = prefix + '-' + id + '-tab'; button.setAttribute('role', 'tab');
      button.setAttribute('aria-controls', prefix + '-' + id); panel.id = prefix + '-' + id;
      panel.className = 'fp-workspace-pane'; panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', button.id);
      buttons[id] = button; panels[id] = panel; tabs.append(button); root.append(panel);
      button.onclick = function () { show(id); };
      button.onkeydown = function (event) {
        var ids = Object.keys(labels), index = ids.indexOf(id), target;
        if (event.key === 'ArrowRight') target = ids[(index + 1) % ids.length];
        if (event.key === 'ArrowLeft') target = ids[(index + ids.length - 1) % ids.length];
        if (event.key === 'Home') target = ids[0]; if (event.key === 'End') target = ids[ids.length - 1];
        if (target) { event.preventDefault(); show(target); buttons[target].focus(); }
      };
    });
    [['#ws-overview-content', 'overview'], ['#ws-settings-content', 'settings']].forEach(function (entry) {
      var content = card.querySelector(entry[0]); if (content) { originals.push(content); panels[entry[1]].append(content); }
    });
    if (!panels.overview.children.length) panels.overview.append(node('h3', ws.name || ws.ws_id), node('p', '包含 ' + (ws.repos || []).length + ' 个仓库；切换到派单查看可分配的分包。'));
    function show(id) {
      root.dataset.activeView = id;
      Object.keys(labels).forEach(function (key) { panels[key].hidden = key !== id; buttons[key].setAttribute('aria-selected', String(key === id)); buttons[key].tabIndex = key === id ? 0 : -1; });
      if (changed) changed(id, panels);
    }
    show(initial || 'overview');
    return {panels: panels, show: show, detach: function () { originals.forEach(function (content) { card.append(content); }); }};
  }};
})();
