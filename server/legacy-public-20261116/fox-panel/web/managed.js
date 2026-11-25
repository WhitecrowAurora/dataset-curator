(function () {
  "use strict";
  var callbacks, host, picker, status, detail, message, refresh, rows = [], inventory = {}, selected = "", busy = false, loading = false;
  var actions = {start: "开始打标", stop: "停止打标", check: "检查分片"};
  var results = {queued: "已下发", accepted: "已接收", applied: "已执行", failed: "失败", canceled: "已取消"};
  var states = {paused: "暂停", managed_idle: "托管待命", running: "打标中", preparing: "准备中", failed: "失败", stopped: "已停止", checking: "检查中", uploading: "回传中", completed: "已完成"};
  var labels = {mode: "产出模式", tag_backend: "TAG 模型", tag_device: "TAG 设备", tag_precision: "TAG 精度", tag_batch: "TAG 批大小",
    inference_source: "NL 服务", nl_engine: "NL 引擎", nl_model: "NL 模型", nl_device: "NL 设备", nl_precision: "NL 精度",
    gpu_mem: "NL 显存比例", concurrency: "推理并发", served_name: "服务模型名"};
  function node(tag, text, cls) {
    var n = document.createElement(tag); if (text) n.textContent = text; if (cls) n.className = cls; return n;
  }
  function button(label, fn, parent, disabled) {
    var b = node("button", label, "fp-btn fp-btn--sm"); b.type = "button"; b.disabled = !!disabled;
    b.addEventListener("click", fn); parent.appendChild(b); return b;
  }
  function device() {return rows.find(function (d) {return d.device_id === selected;});}
  function commandId() {return Array.from(crypto.getRandomValues(new Uint8Array(16)), function (n) {return n.toString(16).padStart(2, "0");}).join("");}
  function mount() {
    host = node("section", "", "fp-card fp-managed"); host.id = "managed-devices";
    var hd = node("div", "", "fp-card__hd"); hd.appendChild(node("h2", "自动托管"));
    refresh = button("刷新", load, hd); host.appendChild(hd);
    var label = node("label", "", "fp-field"); label.appendChild(node("span", "托管设备"));
    picker = node("select"); picker.setAttribute("aria-label", "托管设备"); label.appendChild(picker); host.appendChild(label);
    picker.addEventListener("change", function () {selected = picker.value; update();});
    status = node("p"); status.setAttribute("role", "status"); host.appendChild(status);
    detail = node("div"); host.appendChild(detail);
    message = node("p"); message.setAttribute("role", "status"); host.appendChild(message);
    document.querySelector("#page-devices").prepend(host);
  }
  function write(path, body) {
    if (busy) return; busy = true; update(); message.textContent = "下发中…";
    callbacks.write(path, body).then(function (r) {
      if (!r.ok) throw Error(r.body.error || "下发失败");
      message.textContent = r.body.pending_reconnect ? "停工意图已保存，设备重连后执行" : "已下发，等待设备执行"; return load();
    }).catch(function (e) {message.textContent = e.message;}).finally(function () {busy = false; update();});
  }
  function update() {
    var wanted = selected; picker.replaceChildren();
    rows.forEach(function (d) {
      var o = node("option", d.user_id + " · " + d.device_id + (d.released ? " · 已退出" : d.online ? " · 在线" : " · 离线"));
      o.value = d.device_id; picker.appendChild(o);
    });
    selected = rows.some(function (d) {return d.device_id === wanted;}) ? wanted : rows.length ? rows[0].device_id : "";
    picker.value = selected; picker.disabled = busy;
    var d = device(); detail.replaceChildren(); detail.hidden = !d;
    if (!d) {status.textContent = "暂无托管设备"; return;}
    var s = d.state || {}, cfg = d.config || {};
    if(d.desired === "paused" && !s.running) s = Object.assign({},s,{status:"paused"});
      status.textContent = (d.released ? "已退出" : d.online ? states[s.status] || s.status : "离线")
        + " · " + d.last_seen_ago + " 秒前" + ((s.error || d.automation_error) ? " · " + (s.error || d.automation_error) : "");
    var working = s.running || s.history_running || ["preparing", "starting", "downloading", "quantizing", "starting_service", "checking_service", "loading_classifier", "recovering", "stopping"].indexOf(s.status) >= 0;
    var enabled = d.enabled !== false && callbacks.writable() && !busy;
    var writable = d.online && !d.released && enabled;
    var grid = node("dl", "", "fp-managed-grid"); detail.appendChild(grid);
    if (window.foxManagedTuning) window.foxManagedTuning(detail, d, writable, write, commandId);
    Object.keys(labels).forEach(function (k) {
      if(k.indexOf('tag_')===0&&cfg.mode==='nl'||(k.indexOf('nl_')===0||k==='concurrency'||k==='served_name')&&cfg.mode==='tag')return;
      var cell = node("div"); cell.append(node("dt", labels[k]), node("dd", String(cfg[k] === undefined || cfg[k] === "" ? "自动" : cfg[k]))); grid.appendChild(cell);
    });
    var bar = node("div", "", "fp-managed-actions"); detail.appendChild(bar);
    ["start", "stop"].forEach(function (action) {
      button(actions[action], function () {
        write("/api/managed/commands", {id: commandId(), device_id: d.device_id, session_id: d.session_id, action: action, config: {}});
      }, bar, action === 'stop' ? !enabled : !writable || working);
    });
    button("强制继续", function () {
      write("/api/managed/commands", {id: commandId(), device_id: d.device_id, session_id: d.session_id,
        action: "start", config: {}, force_continue: true});
    }, bar, !writable || working || (d.capabilities.actions || []).indexOf('force_continue') < 0);
    if ((d.capabilities.actions || []).indexOf('force_continue') < 0)
      detail.appendChild(node("p", "升级客户端后可使用强制继续；当前可使用开始打标。"));
    if (d.changed_at) detail.appendChild(node("p", "接单状态更新：" + new Date(d.changed_at * 1000).toLocaleString()
      + " · " + (d.changed_by || "来源未记录")));
    if (d.waiting_reason) detail.appendChild(node("p", d.waiting_reason));
    detail.appendChild(node("h3", "历史分片"));
    (inventory.candidates || []).filter(function (r) {return r.device_id === selected;}).forEach(function (r) {
      var box = node("div", "", "fp-managed-actions"); box.appendChild(node("span", r.repo + " / " + r.package));
      var pending = (inventory.checks || []).some(function (c) {return c.device_id === selected && c.session_id === d.session_id && c.status === "pending";});
      button("检查", function () {write("/api/history/check", {device_id: d.device_id, candidate_id: r.candidate_id});}, box, !writable || working || pending || r.status !== "ready");
      button("补注入画师", function () {write("/api/history/check", {device_id: d.device_id, candidate_id: r.candidate_id, reinject: 'missing'});}, box, !writable || working || pending || r.status !== "ready");
      button("强制重新注入", function () {write("/api/history/check", {device_id: d.device_id, candidate_id: r.candidate_id, reinject: 'force'});}, box, !writable || working || pending || r.status !== "ready");
      detail.appendChild(box);
    });
    (inventory.checks || []).filter(function (r) {return r.device_id === selected;}).slice(0, 8).forEach(function (r) {
      var report = r.report || {}, parts = report.parts || {};
      var box = node("div"); box.appendChild(node("p", r.package + " · " + ({pending: "待检查", registered: "已登记", incomplete: "待补全", failed: "失败", canceled: "已取消"}[r.status] || r.status)));
      Object.keys(parts).forEach(function (p) {var n = parts[p]; box.appendChild(node("p", p.toUpperCase() + " · 成功 " + n.success + " · 排除 " + n.excluded + " · 失败 " + n.failed + " · 缺失 " + n.missing));});
      detail.appendChild(box);
    });
    detail.appendChild(node("h3", "最近指令")); var list = node("ul", "", "fp-managed-history"); detail.appendChild(list);
    (d.commands || []).slice(0, 8).forEach(function (c) {
      list.appendChild(node("li", "#" + c.seq + " " + (actions[c.action] || c.action) + " · " + (results[c.status] || c.status) + (c.error ? " · " + c.error : "")));
    });
  }
  function load() {
    if (!callbacks || loading) return Promise.resolve(); loading = true; refresh.disabled = true;
    return Promise.all([callbacks.api("/api/managed/devices"), callbacks.api("/api/history")]).then(function (values) {
      rows = values[0].devices || []; inventory = values[1]; update();
    }).catch(function (e) {status.textContent = "读取失败：" + e.message;}).finally(function () {loading = false; refresh.disabled = false;});
  }
  window.foxManagedDevices = function (id, api) {callbacks = api; if (!host) mount(); if (id) selected = id; if(window.FoxScheduling) window.FoxScheduling.load(false, callbacks.writable); return load();};
  setInterval(function () {if (host && !document.hidden && !document.querySelector("#page-devices").hidden) load();}, 5000);
})();
