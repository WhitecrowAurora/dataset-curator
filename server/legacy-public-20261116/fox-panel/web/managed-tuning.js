(function () {
  "use strict";
  var drafts = {};
  function node(tag, text) {var n = document.createElement(tag); if (text) n.textContent = text; return n;}
  function metric(parent, text) {parent.appendChild(node("p", text));}
  window.foxManagedTuning = function (parent, d, writable, write, commandId) {
    var s = d.state || {}, p = s.running ? s.progress || {} : {}, cfg = d.config || {}, actual = s.resolved || {};
    parent.appendChild(node("h3", "实时执行与并发"));
    metric(parent, d.waiting_reason || s.message || "等待客户端上报");
    metric(parent, "本机已交付 " + (d.completed_packages || 0) + " 个分片（包括等待中心核验的提交）");
    if(d.execution_limit && typeof d.execution_limit.completed === 'number') metric(parent,'本轮已完成 '+d.execution_limit.completed+' 片 · 上限 '+(d.execution_limit.max_packages||'不限'));
    (d.pending_results||[]).forEach(function(r){metric(parent,'本地已完成、待回传：'+r.pkg+' · '+r.parts.join(' / ').toUpperCase());});
    Object.keys(d.parallel||{}).forEach(function(key){var v=d.parallel[key];metric(parent,(key==='concurrency'?'NL 并发':'TAG 批大小')+'：'+(v.value||'自动')+' · 来源：'+v.source);if(v.warning)metric(parent,v.warning);});
    (d.leases || []).forEach(function (lease) {
      metric(parent, (lease.kind === "active" ? "执行租约：" : "预取租约：") + lease.package + " · " + Math.round(lease.beat_ago_s) + " 秒前收到存活心跳"
        + (lease.kind === "active" && lease.beat_ago_s > 120 ? " · 执行心跳已中断，需确认设备是否仍在运行；租约保留以便续做" : ""));
    });
    if (p.package) metric(parent, "当前分片：" + p.package + (p.package_index ? " · 本轮第 " + p.package_index + " 片" : "") + " · " + (p.phase || "等待进度上报"));
    if (!s.running) metric(parent, "当前没有执行分片；已保留的租约见上方。");
    if (typeof p.done === "number") {
      metric(parent, "已处理 " + p.done + " 张 · " + (p.total_known ? "总计 " : "已读取 ") + (p.total || 0)
        + (p.total_known ? "" : "，总量待确定") + " · 成功 " + (p.successful === undefined ? "待上报" : p.successful)
        + " · 失败 " + (p.failed === undefined ? "待上报" : p.failed)
        + " · 有效速度 " + (typeof p.rate_per_s === "number" ? (p.rate_per_s * 60).toFixed(1) + " 张/分钟" : "等待升级客户端"));
      if (p.updated_ago_s !== undefined) metric(parent, Math.round(p.updated_ago_s + d.last_seen_ago) + " 秒前更新逐图进度");
      if (s.running && p.updated_ago_s + d.last_seen_ago >= 120) metric(parent, "超过 2 分钟没有新进度，请检查设备日志与实际模型请求");
    }
    ((s.telemetry || {}).gpus || []).forEach(function (g) {
      metric(parent, g.device + " · " + g.name + " · GPU " + g.util + "% · 显存 "
        + (g.mem_used_mb / 1024).toFixed(1) + " / " + (g.mem_total_mb / 1024).toFixed(1) + " GB");
    });
    if(cfg.mode!=='tag'&&actual.concurrency!==undefined)metric(parent,'实际 NL 并发 '+actual.concurrency);
    if(cfg.mode!=='nl'&&cfg.tag_backend!=='none'&&actual.tag_batch!==undefined)metric(parent,'实际 TAG 批大小 '+actual.tag_batch);
    var probe = s.probe || {};
    if (probe.error) metric(parent, "探测失败：" + probe.error);
    if (probe.recommended) metric(parent, "探测建议（" + probe.sample_count + " 张）："
      + Object.keys(probe.recommended).map(function (k) {return (k === "concurrency" ? "NL 并发 " : "TAG 批大小 ") + probe.recommended[k];}).join(" · "));
    (probe.rows || []).forEach(function (r) {
      metric(parent, (r.field === "concurrency" ? "NL 并发 " : "TAG 批大小 ") + r.requested
        + (r.error ? " · " + r.error : " · 实际 " + r.actual + " · " + (r.rate_per_s * 60).toFixed(1) + " 张/分钟"));
    });
    var actions = (d.capabilities || {}).actions || [];
    if (actions.indexOf("configure") < 0) {metric(parent, "并发下发与探测需要更新客户端。当前客户端仍可正常领取分片。"); return;}
    var key = d.session_id, draft = drafts[key] || (drafts[key] = {});
    var grid = node("div"); grid.className = "fp-managed-actions"; parent.appendChild(grid);
    var inputs = {};
    ["concurrency", "tag_batch"].forEach(function (k) {
      if (k === "concurrency" && cfg.mode === "tag" || k === "tag_batch" && (cfg.mode === "nl" || cfg.tag_backend === "none")) return;
      var permissions=d.capabilities.remote_concurrency;
      if(permissions&&!permissions[k]) {metric(parent,(k==='concurrency'?'NL 并发':'TAG 批大小')+'：成员未允许服务器调整');return;}
      var label = node("label"); label.className = "fp-field";
      label.appendChild(node("span", k === "concurrency" ? "NL 并发（0 自动）" : "TAG 批大小（0 自动）"));
      var input = node("input"); input.type = "number"; input.min = "0";
      input.max = k === "tag_batch" ? "128" : "64";
      input.value = draft[k] === undefined ? cfg[k] : draft[k]; input.disabled = !writable;
      input.setAttribute("aria-label", k === "concurrency" ? "NL 并发" : "TAG 批大小");
      input.addEventListener("input", function () {draft[k] = input.value;});
      inputs[k] = input; label.appendChild(input); grid.appendChild(label);
    });
    function submit(action, changes) {write("/api/managed/commands", {id: commandId(), device_id: d.device_id,
      session_id: d.session_id, action: action, config: changes});}
    var pending = (d.commands || []).some(function (c) {return c.status === "queued" || c.status === "accepted";});
    var apply = node("button", "下发并发配置"); apply.type = "button"; apply.className = "fp-btn fp-btn--sm";
    apply.disabled = !writable || pending || !Object.keys(inputs).length; grid.appendChild(apply);
    apply.addEventListener("click", function () {
      var changes = {};
      Object.keys(inputs).forEach(function (k) {var n = Number(inputs[k].value);
        if (!inputs[k].reportValidity() || inputs[k].value === "" || !Number.isInteger(n)) return;
        changes[k] = n;});
      if (Object.keys(changes).length !== Object.keys(inputs).length) return;
      submit("configure", changes);
    });
    if (actions.indexOf("probe") >= 0) {
      var button = node("button", "探测并发"); button.type = "button"; button.className = "fp-btn fp-btn--sm";
      button.disabled = !writable || pending; button.addEventListener("click", function () {submit("probe", {});}); grid.appendChild(button);
    }
    metric(parent, "运行时下发会在当前分片交付后生效；探测使用本地小样本，建议值需确认下发，并继续观察正式任务。");
  };
})();
