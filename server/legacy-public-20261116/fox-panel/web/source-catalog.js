/* 仓库清单配置与后台同步状态。 */
(function () {
  "use strict";
  function node(tag, text) { var e = document.createElement(tag); if (text) e.textContent = text; return e; }
  function field(parent, title, input) { var label = node("label", title); label.className = "fp-field"; label.append(input); parent.append(label); return input; }
  function options(select, values) { values.forEach(function (v) { var o = node("option", v[1]); o.value = v[0]; select.append(o); }); return select; }
  async function request(body) {
    var m = document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/), headers = { "Content-Type": "application/json" };
    if (body) headers["X-CSRF-Token"] = m ? decodeURIComponent(m[1]) : "";
    var r = await fetch("/api/source-catalog", body ? { method: "POST", headers: headers, body: JSON.stringify(body) } : {});
    var data = await r.json(); if (!r.ok || data.ok === false) throw Error(data.error || "清单读取失败"); return data;
  }
  var names = { disabled: "未启用", pending: "待核验", queued: "排队中", running: "核验中", ready: "已核验", failed: "核验失败" };
  window.FoxSourceCatalog = {
    actions: function(parent, selected, post, refresh, message, writable, owner) {
      owner = owner || parent;
      var busy = false, buttons = [];
      function update() { buttons.forEach(function (button) { button.disabled = busy || !selected.size || !writable(); }); }
      [["sync", "核验选中"], ["cancel", "取消核验"]].forEach(function(v){
        var b=node("button",v[1]);b.type="button";b.className="fp-btn fp-btn--sm";buttons.push(b);
        b.onclick=async function(){
          if(busy||owner.dataset.busy==='1'||!selected.size||!writable())return;
          busy=true;owner.dataset.busy='1';update();message.textContent='提交中…';
          try{var grouped={};selected.forEach(function(p){(grouped[p.repo]||(grouped[p.repo]=[])).push(p.id);});for(var repo of Object.keys(grouped))await post("/api/source-catalog",{action:v[0],repo:repo,packages:grouped[repo]});await refresh();message.textContent='已提交';}
          catch(e){message.textContent=e.message;}
          finally{busy=false;owner.dataset.busy='0';update();}
        };parent.append(b);
      });
      parent.addEventListener('change',update);update();
    },
    status: function (source) { return "源清单：" + (names[source && source.state] || "未启用") + (source && source.images ? " · " + source.images + " 张" : "") + (source && source.error ? " · " + source.error : ""); },
    detail: function(parent, p, writable, refresh) {
      var download=node('a','下载当前核验结果（含补注入）');
      download.href='/api/source-catalog?format=jsonl&repo='+encodeURIComponent(p.repo)+'&package='+encodeURIComponent(p.id);
      download.className='fp-btn fp-btn--sm';parent.append(download);
      window.FoxSourceConflicts.detail(parent, p, writable, refresh);
    },
    mount: function (parent, repos, writable) {
      var box = node("fieldset"); box.append(node("legend", "清单核验")); parent.append(box);
      var repo = field(box, "仓库", options(node("select"), repos.map(function (r) { return [r, r]; }))), enabled = field(box, "启用", node("input")); enabled.type = "checkbox";
      var source = field(box, "清单来源", options(node("select"), [["auto", "自动"], ["manifest", "总表"], ["index", "分片索引"], ["archive", "原片"]]));
      var fields = {}; [["path", "清单路径"], ["id_field", "图片 ID 字段"], ["package_field", "分片字段"], ["member_field", "成员字段"], ["rows_key", "记录字段"], ["revision", "仓库版本"]].forEach(function (v) { fields[v[0]] = field(box, v[1], node("input")); });
      var message = node("p"), items = [], dirty = false, busy = false, requestId = 0, editId = 0, drafts = new Map(); message.setAttribute("role", "status"); box.append(message);
      function mark() { dirty = true; ++editId; box.dataset.editing = '1'; }
      box.addEventListener("input", mark);
      box.addEventListener("change", function (event) { if (event.target !== repo) mark(); });
      function config() { var cfg = { enabled: enabled.checked, source: source.value }; Object.keys(fields).forEach(function (k) { cfg[k] = fields[k].value.trim(); }); return cfg; }
      function controls() { box.querySelectorAll('input,select,button').forEach(function (input) { input.disabled = busy || (input !== repo && input.textContent !== '刷新' && !writable()); }); }
      var current = repo.value;
      function show() {
        var row = items.find(function (r) { return r.repo === repo.value; }); if (!row) return;
        var cfg = drafts.get(repo.value) || row.config;
        enabled.checked = cfg.enabled; source.value = cfg.source;
        Object.keys(fields).forEach(function (key) { fields[key].value = cfg[key] || ''; });
        var job = row.job; message.textContent = "已核验 " + row.verified_packages + " 个分片" + (job ? " · " + (names[job.state] || job.state) + (job.total ? " · " + (100 * job.downloaded / job.total).toFixed(1) + "%" : "") + (job.error ? " · " + job.error : "") : "");
        dirty = drafts.has(repo.value); box.dataset.editing = dirty || drafts.size ? '1' : '0';
      }
      async function load(body) {
        if (busy || body && !writable()) return;
        var id = ++requestId, revision = editId;
          if (body) { busy = true; box.dataset.busy = '1'; message.textContent = '提交中…'; controls(); }
        try {
          var result = await request(body);
          if (id !== requestId || revision !== editId || !box.isConnected) return;
          items = result.items;
          if (body && body.action === 'configure') { drafts.delete(body.repo); dirty = false; }
          if (!dirty || body && body.action === 'configure') show();
          if (body) message.textContent = body.action === 'configure' ? '已保存' : '已提交';
        } catch (e) { if (id === requestId) message.textContent = e.message; }
        finally { if (body) { busy = false; box.dataset.busy = '0'; } controls(); }
      }
      function button(text, fn) { var b = node("button", text); b.type = "button"; b.className = "fp-btn fp-btn--sm"; b.disabled = !writable(); b.onclick = fn; box.append(b); }
      button("保存", function () { load({ action: "configure", repo: repo.value, config: config() }); });
      button("同步", function () { load({ action: "sync", repo: repo.value }); });
      button("重试", function () { load({ action: "retry", repo: repo.value }); });
      var refresh = node("button", "刷新"); refresh.type = "button"; refresh.className = "fp-btn fp-btn--sm"; refresh.onclick = function () { load(); }; box.append(refresh);
      repo.onchange = function () { if (dirty) drafts.set(current,config()); current = repo.value; ++editId; show(); }; controls(); load();
      var timer = setInterval(function () { if (!box.isConnected) { clearInterval(timer); return; } if (!dirty && !busy) load(); }, 5000);
    }
  };
})();
