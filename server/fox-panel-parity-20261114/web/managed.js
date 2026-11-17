/* 托管设备控制，沿用面板的请求、会话和写通道。 */
(function () {
  "use strict";
  var host, picker, status, detail, history, message, refresh, form, controls = {};
  var callbacks, rows = [], selected = "", session = "", busy = false, loading = false;
  var buttons = {}, groups = {};
  var statusNames = {managed_idle: "托管待命", connecting: "连接中", preparing: "准备模型",
    downloading: "下载权重", quantizing: "生成量化权重", starting: "启动服务",
    running: "打标中", stopping: "停止中", stopped: "已停止", failed: "执行失败",
    completed: "任务已完成", ready: "服务就绪", starting_service: "启动模型服务",
    checking_service: "连接已有服务", loading_classifier: "检查分类模型", recovering: "恢复模型服务"};
  var resultNames = {queued: "已下发", accepted: "已接收", applied: "已执行", failed: "失败", canceled: "已取消"};
  var actions = {configure: "保存配置", start: "开始打标", stop: "停止打标"};

  function node(tag, text, cls) {
    var n = document.createElement(tag);
    if (text) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function option(select, value, label) {
    var o = node("option", label); o.value = value; select.appendChild(o);
  }
  function choose(key, values) {
    var input = controls[key], saved = input.value;
    input.replaceChildren();
    values.forEach(function (v) {option(input, v[0], v[1]);});
    if (values.some(function (v) {return v[0] === saved;})) input.value = saved;
  }
  function field(parent, key, label, values, max, step) {
    var wrap = node("label", "", "fp-field"), title = node("span", label);
    var input = node(values ? "select" : "input");
    input.setAttribute("aria-label", label); input.name = key;
    if (values) values.forEach(function (v) {option(input, v[0], v[1]);});
    else if (max !== undefined) {input.type = "number"; input.min = "0"; input.max = String(max); input.step = String(step || 1);}
    else input.type = "text";
    input.className = "fp-input";
    wrap.append(title, input); parent.appendChild(wrap); controls[key] = input;
    input.addEventListener("change", toggle);
    return input;
  }
  function group(name, label) {
    var box = node("fieldset"), legend = node("legend", label);
    box.appendChild(legend); form.appendChild(box);
    var grid = node("div", "", "fp-managed-grid"); box.appendChild(grid);
    groups[name] = box; return grid;
  }
  function button(label, fn, parent) {
    var b = node("button", label, "fp-btn fp-btn--sm"); b.type = "button";
    b.addEventListener("click", fn); parent.appendChild(b); return b;
  }
  function mount() {
    host = node("section", "", "fp-card fp-managed"); host.id = "managed-devices";
    var hd = node("div", "", "fp-card__hd"); hd.appendChild(node("h2", "被动托管"));
    refresh = button("刷新状态", load, hd); host.appendChild(hd);
    host.appendChild(node("p", "在客户端启动被动模式后，这里可以配置设备并下发启停。停止后设备仍托管待命，本机可随时退出。", "fp-note"));
    var label = node("label", "", "fp-field"); label.appendChild(node("span", "托管设备"));
    picker = node("select"); picker.setAttribute("aria-label", "托管设备");
    label.appendChild(picker); host.appendChild(label);
    picker.addEventListener("change", function () {selected = picker.value; session = ""; update();});
    status = node("p"); status.setAttribute("role", "status"); host.appendChild(status);
    detail = node("div"); host.appendChild(detail);
    form = node("form"); form.addEventListener("submit", function (e) {e.preventDefault();});
    detail.appendChild(form);
    var general = group("general", "执行方式");
    field(general, "mode", "打标模式", [["both", "TAG + NL"], ["tag", "只出 TAG"], ["nl", "只出 NL"]]);
    field(general, "inference_source", "NL 推理方式", [["local", "本机自动启动"], ["existing", "本机已配置的服务"]]);
    var tag = group("tag", "TAG 分类");
    field(tag, "tag_backend", "TAG 模型", []);
    field(tag, "tag_device", "TAG 设备", []);
    field(tag, "tag_precision", "TAG 计算精度", ["tf32", "fp32", "bf16", "fp16"].map(function (v) {return [v, v.toUpperCase()];}));
    field(tag, "tag_batch", "TAG 批大小（0 自动）", null, 128);
    var nl = group("nl", "NL 模型服务");
    field(nl, "nl_engine", "NL 引擎", [["llamacpp", "llama.cpp"], ["vllm", "vLLM"]]);
    field(nl, "nl_model", "NL 模型", []);
    field(nl, "nl_device", "NL 设备", []);
    field(nl, "nl_precision", "NL 权重量化精度", []);
    field(nl, "gpu_mem", "NL 显存比例（0 自动）", null, 0.98, 0.01);
    var service = group("service", "请求参数");
    field(service, "served_name", "服务模型名");
    field(service, "concurrency", "推理并发（0 自动）", null, 64);
    var prep = group("prep", "自动准备");
    [["auto_download", "下载缺少的原始权重"], ["auto_quantize", "生成缺少的量化权重"],
      ["stop_service", "停止时关闭本次自动启动的服务"]].forEach(function (v) {
        var wrap = node("label", "", "fp-check"), input = node("input");
        input.type = "checkbox"; input.name = v[0]; input.setAttribute("aria-label", v[1]);
        wrap.append(input, node("span", v[1])); prep.appendChild(wrap); controls[v[0]] = input;
      });
    form.appendChild(node("p", "HF Token 与已有服务地址由客户端保存。量化完成、模型加载成功后才领取分片。", "fp-note"));
    var bar = node("div", "", "fp-managed-actions"); form.appendChild(bar);
    Object.keys(actions).forEach(function (action) {
      buttons[action] = button(actions[action], function () {send(action);}, bar);
    });
    buttons.reload = button("读取设备当前配置", function () {session = ""; update();}, bar);
    message = node("p"); message.setAttribute("role", "status"); detail.appendChild(message);
    detail.appendChild(node("h3", "最近指令")); history = node("ul", "", "fp-managed-history"); detail.appendChild(history);
    document.querySelector("#page-devices").prepend(host);
  }
  function device() {return rows.find(function (r) {return r.device_id === selected;});}
  function fill(d) {
    var caps = d.capabilities || {}, cfg = d.config || {};
    var devices = [["auto", "自动选择"]].concat((caps.devices || []).map(function (v) {
      return [v.value, v.value + " · " + v.name + " · " + v.vram_gb + " GB"];
    }));
    choose("tag_device", devices.concat([["cpu", "CPU"]]));
    choose("nl_device", cfg.nl_engine === "llamacpp" ? devices.concat([["cpu", "CPU"]]) : devices);
    choose("tag_backend", (caps.tag_models || []).map(function (v) {return [v.backend, v.backend + (v.ready ? " · 就绪" : " · 待准备")];}).concat([["none", "由 NL 模型出 TAG"]]));
    choose("nl_model", [["", "按显存自动选择"]].concat((caps.nl_models || []).map(function (v) {
      return [v.key, v.key + (v.ready ? " · 就绪" : v.raw_ready ? " · 已有原始权重" : " · 待准备")];
    })));
    Object.keys(controls).forEach(function (key) {
      if (key === "nl_precision") return;
      if (controls[key].type === "checkbox") controls[key].checked = !!cfg[key];
      else controls[key].value = cfg[key] === undefined ? "" : String(cfg[key]);
    });
    toggle(); controls.nl_precision.value = cfg.nl_precision || controls.nl_precision.value;
    session = d.session_id;
  }
  function toggle() {
    var d = device(), caps = d && d.capabilities || {};
    var devices = [["auto", "自动选择"]].concat((caps.devices || []).map(function (v) {
      return [v.value, v.value + " · " + v.name + " · " + v.vram_gb + " GB"];
    }));
    choose("nl_device", controls.nl_engine.value === "llamacpp" ? devices.concat([["cpu", "CPU"]]) : devices);
    var prec = controls.nl_engine.value === "vllm" ? [["bf16", "BF16"], ["fp8", "FP8"], ["w4a16", "W4A16"]]
      : [["f16", "F16 完整"], ["q4", "Q4_K_M"], ["q6", "Q6_K"], ["q8", "Q8_0"]];
    choose("nl_precision", prec);
    groups.tag.hidden = controls.mode.value === "nl";
    groups.nl.hidden = controls.mode.value === "tag" || controls.inference_source.value === "existing";
    groups.service.hidden = controls.mode.value === "tag";
    if (controls.mode.value === "tag" && controls.tag_backend.value === "none") {
      var first = Array.from(controls.tag_backend.options).find(function (o) {return o.value !== "none";});
      if (first) controls.tag_backend.value = first.value;
    }
  }
  function update() {
    var wanted = selected, options = rows.map(function (d) {
      return [d.device_id, d.user_id + " · " + d.device_id + (d.released ? " · 已退出" : d.online ? " · 在线" : " · 离线")];
    });
    picker.replaceChildren(); options.forEach(function (v) {option(picker, v[0], v[1]);});
    selected = options.some(function (v) {return v[0] === wanted;}) ? wanted : options.length ? options[0][0] : "";
    picker.value = selected;
    var d = device(); detail.hidden = !d;
    if (!d) {status.textContent = "暂无托管设备。请在客户端连接中心并启动被动模式。"; return;}
    if (session !== d.session_id) {fill(d); message.textContent = "已读取设备当前配置";}
    var s = d.state || {}, progress = s.progress || {};
    status.textContent = (d.released ? "已退出托管" : d.online ? (statusNames[s.status] || s.status || "在线") : "离线")
      + " · " + d.last_seen_ago + " 秒前上报" + (s.message ? " · " + s.message : "")
      + (s.error ? " · " + s.error : "") + (progress.total ? " · " + (progress.done || 0) + "/" + progress.total : "");
    var writable = d.online && !d.released && callbacks.writable();
    var working = s.running || ["preparing", "downloading", "quantizing", "starting", "running", "stopping", "ready",
      "starting_service", "checking_service", "loading_classifier", "recovering"].indexOf(s.status) >= 0;
    Object.keys(controls).forEach(function (k) {controls[k].disabled = !writable || busy || working;});
    buttons.configure.disabled = !writable || busy || working;
    buttons.start.disabled = !writable || busy || working;
    buttons.stop.disabled = !writable || busy;
    buttons.reload.disabled = busy;
    picker.disabled = busy;
    history.replaceChildren();
    (d.commands || []).slice(0, 8).forEach(function (c) {
      history.appendChild(node("li", "#" + c.seq + " " + (actions[c.action] || c.action) + " · "
        + (resultNames[c.status] || c.status) + (c.error ? " · " + c.error : "")));
    });
    if (!(d.commands || []).length) history.appendChild(node("li", "尚无指令"));
  }
  function send(action) {
    var d = device(); if (!d || busy) return;
    if (action !== "stop" && !form.reportValidity()) return;
    var cfg = {};
    if (action !== "stop") Object.keys(controls).forEach(function (key) {
      var input = controls[key]; cfg[key] = input.type === "checkbox" ? input.checked
        : input.type === "number" ? Number(input.value) : input.value;
    });
    busy = true; update(); message.textContent = "下发指令…";
    callbacks.write("/api/managed/commands", {id: crypto.randomUUID(), device_id: d.device_id,
      session_id: d.session_id, action: action, config: cfg}).then(function (r) {
      if (!r.ok) throw Error(r.body.error || "下发失败");
      message.textContent = "已下发 #" + r.body.command.seq + "，等待设备接收及执行";
      return load();
    }).catch(function (e) {message.textContent = e.message;}).finally(function () {busy = false; update();});
  }
  function load() {
    if (!callbacks || loading) return Promise.resolve();
    loading = true; refresh.disabled = true;
    return callbacks.api("/api/managed/devices").then(function (d) {
      rows = d.devices || []; update();
    }).catch(function (e) {status.textContent = "读取托管状态失败：" + e.message;}).finally(function () {
      loading = false; refresh.disabled = false;
    });
  }
  window.foxManagedDevices = function (id, api) {
    callbacks = api; if (!host) mount();
    if (id && selected !== id) {selected = id; session = "";}
    return load();
  };
  setInterval(function () {
    if (host && !document.hidden && !document.querySelector("#page-devices").hidden) load();
  }, 5000);
})();
