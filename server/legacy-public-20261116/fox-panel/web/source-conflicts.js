/* 分片详情与批量采用，沿用清单接口和管理员会话。 */
(function () {
  'use strict';
  function node(tag, text) { var e = document.createElement(tag); if (text) e.textContent = text; return e; }
  function button(text, fn) { var e = node('button', text); e.type = 'button'; e.className = 'fp-btn fp-btn--sm'; e.onclick = fn; return e; }
  function select(label, values) {
    var e = node('select'); e.setAttribute('aria-label', label);
    values.forEach(function (v) { var o = node('option', v[1]); o.value = v[0]; e.append(o); }); return e;
  }
  async function request(body) {
    var m = document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);
    var r = await fetch('/api/source-catalog', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : ''}, body: JSON.stringify(body)});
    var data = await r.json(); if (!r.ok || data.ok === false) throw Error(data.error || '处理失败'); return data;
  }
  function body(value) { try { return typeof value === 'string' ? JSON.parse(value) : value; } catch (_) { return value; } }
  var rules = [['newest', '采用较新结果'], ['oldest', '采用较旧结果'], ['longest', '采用较长正文'], ['shortest', '采用较短正文'], ['most_tokens', '采用 token 较多正文'], ['fewest_tokens', '采用 token 较少正文']];
  window.FoxSourceConflicts = {detail: function (parent, p, writable, refresh) {
    var box = node('details'), message = node('p'), content = node('div'), notice = node('p'), list;
    var offset = 0, busy = false, revision = 0, data = null, selected = new Map(), checks = [], actions = [];
    var filter = select('冲突状态', [['pending', '仅待处理'], ['all', '全部状态']]);
    var scope = select('批量处理范围', [['selected', '勾选的冲突'], ['first', '前 X 条待处理'], ['all', '全部待处理']]);
    var count = node('input'); count.type = 'number'; count.min = '1'; count.step = '1'; count.value = '100'; count.setAttribute('aria-label', '待处理数量');
    var toolbar = node('div'), stats = node('p'), selection = node('div'), execute = node('div');
    toolbar.className = selection.className = execute.className = 'fp-conflict-toolbar';
    box.className = 'fp-source-detail'; box.dataset.sourcePackage = p.id; box.dataset.sourceRepo = p.repo;
    message.setAttribute('role', 'status'); notice.setAttribute('role', 'status'); notice.className = 'fp-conflict-notice';
    box.append(node('summary', '分片详情'), message, notice, toolbar, stats, selection, execute, content); parent.append(box);
    toolbar.append(filter, button('刷新详情', function () { if (!busy) load(); }));
    var pageAll = button('本页全选', function () { scope.value = 'selected'; checks.forEach(function (v) { if (v.eligible) selected.set(v.key, v.item); }); update(); });
    var clear = button('清空选择', function () { selected.clear(); scope.value = 'selected'; update(); });
    var all = button('全选待处理', function () { scope.value = 'all'; update(); });
    selection.append(pageAll, clear, all, scope, count);
    rules.forEach(function (rule) { var b = button(rule[1], function () { apply(rule); }); b.dataset.tokenRule = rule[0].endsWith('_tokens') ? '1' : '0'; actions.push(b); execute.append(b); });
    var tokenNote = node('span'); execute.append(tokenNote);
    function amount() {
      if (!data) return 0;
      if (scope.value === 'all') return data.conflict_pending;
      if (scope.value === 'first') { var n = Number(count.value); return Number.isSafeInteger(n) && n > 0 ? Math.min(n, data.conflict_pending) : 0; }
      return selected.size;
    }
    function update() {
      var allowed = writable() && !busy;
      box.dataset.busy = busy ? '1' : '0'; box.dataset.editing = box.open || selected.size ? '1' : '0';
      count.hidden = scope.value !== 'first'; count.disabled = scope.disabled = !allowed;
      pageAll.disabled = clear.disabled = all.disabled = !allowed; filter.disabled = busy;
      actions.forEach(function (b) { b.disabled = !allowed || !amount() || b.dataset.tokenRule === '1' && !(data && data.token_encoding && data.token_encoding.ready); });
      tokenNote.textContent = '时间取产物生成时间；长度取正文字符数；token 使用 Qwen3.5-0.8B，只计算正文，不含图片和聊天模板。并列、信息缺失及正在执行的项会跳过。' + (data && data.token_encoding && !data.token_encoding.ready ? ' ' + data.token_encoding.error : '');
      checks.forEach(function (v) { v.input.disabled = !allowed || !v.eligible; v.input.checked = scope.value === 'selected' ? selected.has(v.key) : scope.value === 'all' && v.eligible; });
      stats.textContent = data ? '待处理 ' + data.conflict_pending + ' 条 · 当前范围 ' + amount() + ' 条' + (scope.value === 'selected' ? '（可跨页勾选）' : '') : '正在读取…';
      content.querySelectorAll('button,select').forEach(function (b) { b.disabled = busy || b.dataset.unavailable === '1' || !writable() && !b.dataset.navigation; });
    }
    filter.onchange = function () { offset = 0; selected.clear(); load(); };
    scope.onchange = update; count.oninput = update;
    async function apply(rule) {
      if (busy || !writable() || !amount()) return;
      if (!window.confirm('将对「' + p.id + '」的 ' + amount() + ' 条冲突' + rule[1] + '。原始回传和候选结果会保留；无法按此规则选择的项会跳过。继续？')) return;
      var payload = {action: 'resolve_batch', repo: p.repo, package: p.id, source_sha256: data.source_sha256, strategy: rule[0], scope: scope.value};
      if (scope.value === 'selected') payload.items = Array.from(selected.values());
      if (scope.value === 'first') payload.count = Number(count.value);
      var storageKey = 'lulynx-conflict-batch:' + p.repo + ':' + p.id, encoded = JSON.stringify(payload), saved;
      try { saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) {}
      payload.request_id = saved && saved.body === encoded ? saved.id : crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(36).slice(2);
      try { sessionStorage.setItem(storageKey, JSON.stringify({body: encoded, id: payload.request_id})); } catch (_) {}
      busy = true; update(); notice.textContent = '正在批量处理…';
      try {
        var result = await request(payload), batch = result.batch;
        try { sessionStorage.removeItem(storageKey); } catch (_) {}
        var text = '已采用 ' + batch.applied + ' 条，跳过 ' + batch.skipped + ' 条' + Object.keys(batch.reasons).map(function (k) { return '；' + k + ' ' + batch.reasons[k] + ' 条'; }).join('');
        selected.clear(); scope.value = 'selected'; offset = 0; box.dataset.notice = text;
        busy = false; await load(); if (batch.applied) await refresh(text);
      } catch (e) { notice.textContent = e.message; }
      finally { busy = false; update(); }
    }
    function renderConflict(conflict) {
      var wrapper = node('div'), row = node('details'), label = node('label'), check = node('input'), pick = node('select');
      var key = JSON.stringify([conflict.image_id, conflict.part]), eligible = conflict.state === 'pending' && !data.busy_parts.includes(conflict.part);
      wrapper.className = 'fp-conflict-row'; check.type = 'checkbox'; check.setAttribute('aria-label', '选择 ' + conflict.image_id + ' ' + conflict.part.toUpperCase());
      var item = {image_id: conflict.image_id, part: conflict.part}; checks.push({input: check, key: key, item: item, eligible: eligible});
      check.onchange = function () { scope.value = 'selected'; if (check.checked) selected.set(key, item); else selected.delete(key); update(); };
      label.append(check); row.append(node('summary', conflict.image_id + ' · ' + conflict.part.toUpperCase() + ' · ' + ({pending: '冲突', resolved: '已处理', redone: '待重做'}[conflict.state] || conflict.state)));
      conflict.variants.forEach(function (value, i) {
        var meta = (conflict.variant_info || [])[i] || {}, stamp = meta.timestamp ? new Date(meta.timestamp * 1000).toLocaleString() : '时间未知';
        var title = '结果 ' + (i + 1) + ' · ' + stamp + ' · ' + (meta.length == null ? '长度未知' : meta.length + ' 字符') + ' · ' + (meta.tokens == null ? 'token 未计数' : meta.tokens + ' token');
        var option = node('option', title); option.value = i; pick.append(option);
        row.append(node('p', title + (conflict.state === 'resolved' && conflict.chosen === i ? ' · 已采用' : '')), node('pre', JSON.stringify(body(value), null, 2)));
      });
      pick.setAttribute('aria-label', '冲突结果 ' + conflict.image_id); if (conflict.chosen != null) pick.value = conflict.chosen;
      async function single(resolution) {
        if (busy || !writable()) return; busy = true; update();
        try {
          await request({action: 'resolve', repo: p.repo, package: p.id, image_id: conflict.image_id, part: conflict.part, resolution: resolution, variant: Number(pick.value)});
          selected.delete(key); var text = resolution === 'choose' ? '已采用所选结果' : '已标记重做，原产物保留';
          busy = false; box.dataset.notice = text; await load(); await refresh(text);
        } catch (e) { notice.textContent = e.message; }
        finally { busy = false; update(); }
      }
      var choose = button('采用结果', function () { single('choose'); }), redo = button('重做', function () { single('redo'); });
      [pick, choose, redo].forEach(function (b) { b.dataset.unavailable = !writable() || !eligible ? '1' : '0'; });
      if (conflict.variants.length < 2) choose.dataset.unavailable = '1';
      row.append(pick, choose, redo); wrapper.append(label, row); list.append(wrapper);
    }
    async function load() {
      var id = ++revision; busy = true; update();
      try {
        var r = await fetch('/api/source-catalog?repo=' + encodeURIComponent(p.repo) + '&package=' + encodeURIComponent(p.id) + '&offset=' + offset + '&conflict_state=' + filter.value), value = await r.json();
        if (!r.ok || value.ok === false) throw Error(value.error || '读取失败');
        if (id !== revision) return; data = value; content.replaceChildren(); checks = [];
        message.textContent = window.FoxSourceCatalog.status(data.source) + (data.waiting_reason ? ' · ' + data.waiting_reason : ''); notice.textContent = box.dataset.notice || '';
        content.append(node('p', window.FoxScheduling ? window.FoxScheduling.counts(data.report) : ''));
        var retry = button('重试核验', async function () { if (busy) return; busy = true; update(); try { await request({action: 'retry', repo: p.repo, packages: [p.id]}); await load(); } catch (e) { notice.textContent = e.message; } finally { busy = false; update(); } });
        retry.dataset.unavailable = !writable() || data.source.state === 'disabled' ? '1' : '0'; content.append(retry);
        list = node('div'); list.className = 'fp-conflict-list'; content.append(list);
        data.conflicts.forEach(renderConflict);
        var nav = node('div'); nav.className = 'fp-conflict-toolbar';
        var prev = button('上一页', function () { if (!busy) { offset = Math.max(0, offset - 100); load(); } }); prev.dataset.unavailable = offset === 0 ? '1' : '0';
        var next = button('下一页', function () { if (!busy) { offset += 100; load(); } }); next.dataset.unavailable = Math.max(data.issue_total, data.conflict_total) <= offset + 100 ? '1' : '0';
        prev.dataset.navigation = next.dataset.navigation = '1';
        nav.append(prev, node('span', '第 ' + (offset / 100 + 1) + ' 页 · 每页最多 100 条'), next); content.append(nav);
        if (data.issue_total) {
          var issues = node('details'); issues.append(node('summary', '失败、缺失与来源问题 · 共 ' + data.issue_total + ' 项（本页 ' + data.issues.length + ' 项）'));
          data.issues.forEach(function (issue) { issues.append(node('p', issue.image_id + ' · ' + issue.part.toUpperCase() + ' · ' + ({failed: '失败', missing: '缺失', foreign: '来源不符'}[issue.status] || issue.status) + (issue.reason ? ' · ' + issue.reason : ''))); });
          content.append(issues);
        }
      } catch (e) { if (id === revision) notice.textContent = e.message; }
      finally { if (id === revision) { busy = false; update(); } }
    }
    box.addEventListener('toggle', function () { update(); if (box.open) { offset = 0; load(); } });
    update();
  }};
})();
