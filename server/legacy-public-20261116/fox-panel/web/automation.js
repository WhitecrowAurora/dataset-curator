/* 顶部在线派单总开关；运行状态来自服务器，不与页面自动刷新混用。 */
(function () {
  'use strict';
  window.FoxAutomation = {mount: function (callbacks) {
    var box = document.querySelector('#dispatch-control'), state = box.querySelector('[data-state]'),
      action = box.querySelector('button'), latest = null, busy = false, loading = false, error = '', generation = 0;
    function render() {
      state.textContent = !latest ? error ? '自动派单：读取失败' : '自动派单：读取中' : latest.dispatch.running ? '自动派单：运行中' : '自动派单：已暂停';
      action.textContent = latest && latest.dispatch.running ? '暂停派单' : '启动派单';
      action.disabled = busy || !latest || !callbacks.writable();
      box.dataset.running = latest ? String(latest.dispatch.running) : 'unknown';
      box.title = error || (latest ? '当前包 ' + latest.active_leases + ' · 预锁 ' + latest.prelocks
        + '；暂停保留当前包，阻止新包与预取；停止设备和释放租约另行操作。' : '等待服务器状态');
    }
    async function load() {
      if (loading || busy || document.hidden) return;
      loading = true;
      var current = generation;
      try {
        var response = await callbacks.api('/api/scheduling?summary=automation');
        if (current === generation) { latest = response; error = ''; }
      }
      catch (e) { if (current === generation) { latest = null; error = e.message; } }
      finally { loading = false; render(); }
    }
    action.onclick = async function () {
      if (busy || !latest || !callbacks.writable()) return;
      var running = !latest.dispatch.running;
      generation++; busy = true; render();
      try {
        var response = await callbacks.write('/api/scheduling', {action: 'automation', running: running});
        if (!response.ok) throw Error(response.body.error || '更新失败');
        latest = response.body; error = '';
      } catch (e) { error = e.message; box.querySelector('[data-message]').textContent = error; }
      finally { busy = false; render(); }
      if (!error) box.querySelector('[data-message]').textContent = running ? '已启动' : '已暂停，当前包保留';
    };
    document.querySelector('#refresh').addEventListener('click', load);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
    load(); setInterval(load, 5000);
  }};
})();
