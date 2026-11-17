/* 分组、优先级、分项分配与逐图计数。 */
(function () {
  "use strict";
  function n(tag, text) { var e = document.createElement(tag); if (text) e.textContent = text; return e; }
  function field(parent, text, input) { var label = n("label", text); label.className = "fp-field"; label.append(input); parent.append(label); return input; }
  function options(s, values) { s.replaceChildren(); values.forEach(function (v) { var o = n("option", v[1]); o.value = v[0]; s.append(o); }); return s; }
  function button(parent, text, fn) { var b = n("button", text); b.type = "button"; b.className = "fp-btn fp-btn--sm"; b.onclick = fn; parent.append(b); return b; }
  async function get(path) { var r = await fetch(path), d = await r.json(); if (!r.ok || d.ok === false) throw Error(d.error || "读取失败"); return d; }
  async function post(body) {
    var m = document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);
    var r = await fetch("/api/scheduling", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": m ? decodeURIComponent(m[1]) : "" }, body: JSON.stringify(body) });
    var d = await r.json(); if (!r.ok || d.ok === false) throw Error(d.error || "保存失败"); return d;
  }
  function lines(input) { return input.value.split(/\r?\n/).map(function (x) { return x.trim(); }).filter(Boolean); }
  var host, data, message, groups, target, order, effective, scope, name, gid, grace;
  var advanced, quickBody, workspacePicker, batchPicker, selectPickers = [], workspaceData;
  var editing = false, busy = false, requestId = 0, userRequestId = 0, editId = 0, writable = function () { return false; };
  function controls() {
    if (!host) return;
    advanced.querySelectorAll('input,textarea,select,button').forEach(function (input) { input.disabled = busy || (input.textContent !== '刷新' && (!data || !writable())); });
    selectPickers.forEach(function (p) { p.refresh(); });
    if (batchPicker) batchPicker.disable(busy || !data || !writable());
  }
  function showTarget() {
    var type = target.value.split(":")[0], id = target.value.slice(type.length + 1);
    var row = (type === "user" ? data.users : data.devices).find(function (x) { return x[type + "_id"] === id; });
    order.value = row ? row.repo_order.join("\n") : "";
    if (type === "user") groups.value = row && row.group_id || "";
    if (selectPickers[1]) selectPickers[1].sync();
    effective.textContent = type === "device" && row ? "生效顺序：" + row.effective_repos.join(" → ") + "；" + row.leases.map(function (l) { return (l.kind === "prelock" ? "预锁" : "执行") + "：" + l.package + " · " + l.parts.join(" / "); }).join("；") : "";
  }
  function show() {
    var old = groups.value; options(groups, [["", "无分组"]].concat(data.groups.map(function (g) { return [g.group_id, g.name]; }))); groups.value = old;
    old = target.value;
    options(target, data.users.map(function (u) { return ["user:" + u.user_id, u.user_id]; }).concat(data.devices.map(function (d) { return ["device:" + d.device_id, d.user_id + " · " + d.device_id]; })));
    if (old && Array.from(target.options).some(function (o) { return o.value === old; })) target.value = old; grace.value = data.offline_grace; showTarget();
    selectPickers.forEach(function (p) { p.sync(); });
    batchPicker.setItems(window.FoxUsers.catalog(workspaceData).items.map(function(item){return Object.assign({},item,{disabled:false});}));
  }
  async function save(body) {
    if (busy || !data || !writable()) return;
    busy = true; ++requestId; controls(); message.textContent = "保存中…";
    try { data = await post(body); editing = false; message.textContent = "已保存"; show(); }
    catch (e) { message.textContent = e.message; }
    finally { busy = false; controls(); }
  }
  function mount() {
    host = n("section"); host.className = "fp-card"; host.append(n("h2", "调度配置"));
    host.addEventListener('input', function () { editing = true; ++editId; });
    host.addEventListener('change', function () { editing = true; ++editId; });
    message = n("p"); message.setAttribute("role", "status"); host.append(message);
    workspacePicker = window.FoxSelection.mount(host, {title:'工作区',single:true,onChange:function () { showWorkspace(); }});
    quickBody = n('div'); host.append(quickBody);
    advanced = n('details'); advanced.className = 'fp-scheduling-advanced';
    advanced.append(n('summary','分组与优先级设置')); host.append(advanced);
    grace = field(advanced, "离线宽限（秒）", n("input")); grace.type = "number"; grace.min = "30"; grace.max = "86400";
    button(advanced, "保存宽限", function () { save({ action: "settings", offline_grace: Number(grace.value) }); });
    var box = n("fieldset"); box.append(n("legend", "分组")); advanced.append(box);
    gid = field(box, "分组 ID", n("input")); name = field(box, "名称", n("input")); scope = field(box, "工作区", n("select"));
    selectPickers.push(window.FoxSelection.fromSelect(scope,'分组工作区'));
    var groupOrder = field(box, "仓库优先级", n("textarea"));
    button(box, "保存分组", function () { save({ action: "group", group_id: gid.value.trim(), name: name.value.trim(), workspace_id: scope.value, repos: lines(groupOrder) }); });
    groups = field(advanced, "分组", n("select"));
    selectPickers.push(window.FoxSelection.fromSelect(groups,'分组'));
    button(advanced, "编辑分组", function () { var g = data.groups.find(function (x) { return x.group_id === groups.value; }); if (g) { gid.value = g.group_id; name.value = g.name; scope.value = g.workspace_id || ""; groupOrder.value = g.repos.join("\n"); selectPickers[0].sync(); editing = true; ++editId; } });
    button(advanced, "删除分组", function () { if (groups.value) save({ action: "delete_group", group_id: groups.value }); });
    target = field(advanced, "用户／设备", n("select")); target.onchange = showTarget;
    selectPickers.push(window.FoxSelection.fromSelect(target,'配置用户／设备'));
    order = field(advanced, "仓库优先级", n("textarea")); effective = n("p"); advanced.append(effective);
    button(advanced, "保存配置", function () { var type = target.value.split(":")[0], id = target.value.slice(type.length + 1); if(id)save({ action: type, ids: [id], group_id: groups.value, repos: lines(order) }); });
    batchPicker = window.FoxUsers.mount(advanced,{title:'批量配置用户'});
    button(advanced, "批量分组", function () { if(batchPicker.values().length)save({ action: "user", ids: batchPicker.values(), group_id: groups.value, repos: lines(order) }); });
    button(advanced, "刷新", function () { load(true); }); document.querySelector("#page-devices").append(host); controls();
  }
  function showWorkspace() {
    if (!workspaceData) return;
    var id = workspacePicker.values()[0], ws = workspaceData.workspaces.find(function (w) { return w.ws_id === id; });
    var previous = quickBody.querySelector('[data-workspace-packages]');
    if (!previous || previous.dataset.ws !== id) quickBody.replaceChildren();
    if (ws) window.FoxWorkspace.render(quickBody,ws,get,writable,workspaceData);
    else quickBody.replaceChildren(n('p','请选择工作区，再勾选分片与用户'));
  }
  async function load(force, canWrite) {
    if (canWrite) writable = canWrite;
    if (busy || editing && force !== true) { controls(); return; }
    var id = ++requestId, revision = editId;
    try {
      if (!host) mount();
      var values = await Promise.all([get("/api/scheduling"), get("/api/workspaces")]);
      if (id !== requestId || revision !== editId || busy) return;
      editing = false; data = values[0]; workspaceData = values[1];
      workspacePicker.setItems(workspaceData.workspaces.map(function (w) { return {id:w.ws_id,label:w.name||w.ws_id,detail:(w.repos||[]).join(' · ')}; }));
      if (!workspacePicker.values().length && workspaceData.workspaces.length === 1) workspacePicker.setValues([workspaceData.workspaces[0].ws_id]);
      showWorkspace();
      var old = scope.value; options(scope, [["", "未分配"]].concat(values[1].workspaces.map(function (w) { return [w.ws_id, w.name || w.ws_id]; }))); scope.value = old; show();
    } catch (e) { if (id === requestId && message) message.textContent = e.message; }
    finally { controls(); }
  }
  window.FoxScheduling = {
    load: load,
    refreshUsers: async function () {
      if(!host||busy||!editing)return;
      var id=++userRequestId,revision=requestId;
      try {
        var latest=await get('/api/workspaces');
        if(id!==userRequestId||revision!==requestId||busy)return;
        workspaceData=latest;showWorkspace();
      } catch(e) { if(id===userRequestId&&revision===requestId)message.textContent=e.message; }
    },
    counts: function (report) {
      if (!report) return "未验收";
      var pending = report.source_verification && (report.source_verification.state === "pending" || report.source_verification.state === "failed");
      var conflicts=report.conflict_count===undefined?report.conflicts.length:report.conflict_count;
      return ["tag", "nl"].map(function (part) { var c = report.parts[part]; return part.toUpperCase() + "：成功 " + c.success + "，排除 " + c.excluded + "，失败 " + c.failed + "，漏项 " + c.missing + " · " + (pending ? "待核验" : report.complete_parts.indexOf(part) >= 0 ? "完成" : "未完成"); }).join("；") + (conflicts ? "；冲突 " + conflicts : "");
    },
    assign: function (parent, selected, all, done, canWrite, holder) {
      var types = {}, ids = {}, pickers = {}, saving = false;
      holder = holder || parent;
      canWrite = canWrite || function () { return false; };
      ["tag", "nl"].forEach(function (part) {
        types[part] = field(holder, part.toUpperCase() + " 分配", options(n("select"), [["auto", "自动"], ["user", "用户"], ["device", "设备"]]));
        ids[part] = field(holder, part.toUpperCase() + " 目标", n("select"));
        pickers[part] = window.FoxSelection.fromSelect(ids[part],part.toUpperCase()+' 目标');
        types[part].onchange = function () {
          var type = types[part].value; ids[part].hidden = type === "auto";
          var available = (type === "user" ? all.users : all.devices) || [];
          options(ids[part], available.filter(function (item) { return type === "user" ? item.status === "active" : !['blocked','archived'].includes(item.status) && !item.device_id.startsWith('offline-'); }).map(function (item) { var id = item[type + "_id"]; return [id, id]; }));
          pickers[part].sync(); pickers[part].element.hidden = type === 'auto';
          if (save) { parent.dataset.editing = '1'; update(); }
        };
        types[part].onchange();
      });
      var save = button(holder, "保存分配", async function () {
        if (saving || parent.dataset.busy === '1' || !selected.size || !canWrite()) return;
        saving = true; parent.dataset.busy = '1'; update();
        try { var body = { action: "assign", items: Array.from(selected.values()) }; ["tag", "nl"].forEach(function (part) { body[part] = { type: types[part].value, id: ids[part].value }; }); await post(body); await done("已保存分项分配", true); }
        catch (e) { done(e.message); }
        finally { saving = false; parent.dataset.busy = '0'; update(); }
      });
      function update() {
        var locked = saving || !canWrite();
        ['tag','nl'].forEach(function (part) { types[part].disabled = locked; ids[part].disabled = locked; pickers[part].refresh(); });
        if (save) save.disabled = locked || !selected.size || ['tag','nl'].some(function (part) { return types[part].value !== 'auto' && !ids[part].value; });
      }
      parent.addEventListener('change',function (event) { if (['tag','nl'].some(function (part) { return event.target === ids[part]; })) parent.dataset.editing = '1'; update(); }); update();
      if(holder!==parent)holder.addEventListener('change',update);
    }
  };
})();
