/* 调度产物目标：复用面板会话及 CSRF，不在浏览器存 token。 */
window.foxDeliverySettings = function () {
  var host = document.querySelector("#delivery-settings");
  if (!host) {
    host = document.createElement("section");
    host.id = "delivery-settings";
    host.className = "fp-card";
    document.querySelector("#settings .fp-drawer__bd").prepend(host);
  }
  if (host.dataset.ready) return;
  host.textContent = "读取产物储存设置…";
  fetch("/api/delivery/settings", {credentials: "same-origin"}).then(function (r) {
    if (!r.ok) throw Error("读取储存设置失败");
    return r.json();
  }).then(function (cfg) {
    host.replaceChildren();
    host.dataset.ready = "1";
    var title = document.createElement("h2");
    title.textContent = "产物储存";
    host.appendChild(title);
    function field(label, type, value) {
      var wrap = document.createElement("label");
      wrap.style.cssText = "display:block;margin:12px 0";
      wrap.appendChild(document.createTextNode(label + " "));
      var input = document.createElement(type === "select" ? "select" : "input");
      if (type !== "select") input.type = type;
      input.value = value || "";
      input.className = "fp-input";
      input.style.width = "100%";
      wrap.appendChild(input);
      var notice = document.createElement("small");
      notice.setAttribute("role", "status"); notice.hidden = true;
      notice.style.cssText = "display:none;margin-top:6px;padding:8px 10px;border-left:2px solid #d99c32;background:rgba(217,156,50,.1);line-height:1.5";
      wrap.appendChild(notice); input.deliveryNotice = notice;
      host.appendChild(wrap);
      return input;
    }
    function option(select, value, label) {
      var o = document.createElement("option");
      o.value = value; o.textContent = label; select.appendChild(o);
    }
    var mode = field("储存方式", "select");
    option(mode, "local", "本地储存（服务器目录）");
    option(mode, "hf", "在线储存（Hugging Face）");
    mode.value = cfg.mode;
    var local = field("服务器目录", "text", cfg.local_dir);
    var browse = document.createElement("button");
    browse.type = "button"; browse.className = "fp-btn"; browse.textContent = "浏览服务器目录";
    host.appendChild(browse);
    var dirs = document.createElement("div"); host.appendChild(dirs);
    var token = field("HF token", "password");
    token.autocomplete = "off";
    token.placeholder = cfg.has_token ? "已保存，留空沿用" : "hf_…（需要目标仓库写权限）";
    var list = document.createElement("button");
    list.type = "button"; list.className = "fp-btn"; list.textContent = "获取仓库";
    host.appendChild(list);
    var repo = field("目标仓库", "select");
    option(repo, "", "请选择目标仓库");
    if (cfg.hf_repo) option(repo, cfg.hf_type + ":" + cfg.hf_repo, cfg.hf_repo + " (" + cfg.hf_type + ")");
    if (cfg.hf_repo) repo.value = cfg.hf_type + ":" + cfg.hf_repo;
    var prefix = field("仓库子目录（可留空）", "text", cfg.hf_prefix);
    var msg = document.createElement("p"); msg.setAttribute("role", "status"); host.appendChild(msg);
    function notice(input, text) {
      input.deliveryNotice.textContent = text;
      input.deliveryNotice.hidden = !text;
      input.deliveryNotice.style.display = text ? "block" : "none";
      input.setAttribute("aria-invalid", text ? "true" : "false");
    }
    function validate() {
      notice(local, mode.value === "local" && !local.value.trim() ? "请选择服务器上的产物目录，可点击下面的浏览按钮。" : "");
      notice(token, mode.value === "hf" && !cfg.has_token && !token.value.trim()
        ? "请填写有目标仓库写权限的 HF Token，再点击获取仓库。" : "");
      notice(repo, mode.value === "hf" && !repo.value ? "请获取仓库并选择一个产物存放仓库。" : "");
    }
    [local, token, repo].forEach(function (input) {
      input.addEventListener("input", validate); input.addEventListener("change", validate);
    });
    function toggle() {
      [local.parentElement, browse, dirs].forEach(function (x) {x.hidden = mode.value !== "local";});
      [token.parentElement, list, repo.parentElement, prefix.parentElement].forEach(function (x) {x.hidden = mode.value !== "hf";});
      validate();
    }
    mode.addEventListener("change", toggle); toggle();
    function browseAt(path) {
      return fetch("/api/delivery/browse?path=" + encodeURIComponent(path), {credentials: "same-origin"})
        .then(function (r) {if (!r.ok) throw Error("目录不可访问"); return r.json();})
        .then(function (d) {
          local.value = d.path; validate(); dirs.replaceChildren();
          [{name: "↑ 上级目录", path: d.parent}].concat(d.directories).forEach(function (item) {
            var b = document.createElement("button"); b.type = "button";
            b.className = "fp-btn fp-btn--sm"; b.textContent = item.name;
            b.addEventListener("click", function () {browseAt(item.path).catch(function(e){notice(local,e.message + "；请换一个可访问的服务器目录。");});});
            dirs.appendChild(b);
          });
        });
    }
    browse.addEventListener("click", function () {browseAt(local.value || "/").catch(function(e){notice(local,e.message + "；请换一个可访问的服务器目录。");});});
    list.addEventListener("click", function () {
      list.disabled = true; msg.textContent = "获取个人及组织仓库…";
      writeRequest("/api/delivery/repos", {hf_token: token.value}).then(function (r) {
        if (!r.ok) throw Error(r.body.error || "获取仓库失败");
        repo.replaceChildren();
        option(repo, "", "请选择目标仓库");
        r.body.repositories.forEach(function (x) {option(repo, x.type + ":" + x.id, x.id + " (" + x.type + ")");});
        var previous = cfg.hf_type + ":" + cfg.hf_repo;
        if (Array.from(repo.options).some(function (x) { return x.value === previous; })) repo.value = previous;
        validate();
        msg.textContent = "已获取 " + r.body.repositories.length + " 个仓库；请确认目标仓库有写权限";
      }).catch(function(e){notice(token,e.message + "；请检查 Token 和网络，确认账号有仓库访问权限后重新获取。");}).finally(function(){list.disabled=false;});
    });
    var save = document.createElement("button"); save.type = "button";
    save.className = "fp-btn"; save.textContent = "保存储存设置"; host.appendChild(save);
    save.addEventListener("click", function () {
      var choice = repo.value.split(":");
      save.disabled = true;
      writeRequest("/api/delivery/settings", {mode: mode.value, local_dir: local.value,
        hf_token: token.value, hf_type: choice[0] || "dataset", hf_repo: choice[1] || "",
        hf_prefix: prefix.value}).then(function (r) {
          if (!r.ok) throw Error(r.body.error || "保存失败");
          cfg = r.body;
          token.value = ""; token.placeholder = r.body.has_token ? "已保存，留空沿用" : "hf_…";
          validate();
          msg.textContent = "已保存；后续回传使用此位置";
        }).catch(function(e){
          if (mode.value === "local") notice(local,e.message + "；请检查目录是否可写，或选择其他目录。");
          else msg.textContent = e.message + "；请检查所选仓库、Token 写权限与子目录设置后重新保存。";
        }).finally(function(){save.disabled=false;});
    });
  }).catch(function(e){host.textContent=e.message;});
};
