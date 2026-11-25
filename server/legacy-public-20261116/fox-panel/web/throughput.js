(function () {
  'use strict';
  window.FoxThroughput = {mount:function (api) {
    var box=document.querySelector('#total-throughput'),value=box.querySelector('[data-speed]'),details=box.querySelector('[data-devices]');
    var latest=null,received=0,loading=false;
    function render() {
      var old=!latest || Date.now()-received>20000;
      value.textContent=old?'总速度 —':('总速度 '+latest.rate_per_s.toFixed(2)+' 张/秒');
      details.textContent=old?'等待更新':((latest.rate_per_s*60).toFixed(1)+' 张/分钟 · '+latest.reporting_devices+'/'+latest.active_devices+' 台上报'+(latest.unreported_devices?' · '+latest.unreported_devices+' 台待速度':''));
      box.dataset.stale=old?'1':'0';
      box.title=old?'读取暂不可用；显示 —，恢复连接后自动刷新':latest.basis+'；TAG '+latest.tag_rate_per_s.toFixed(2)+' / NL '+latest.nl_rate_per_s.toFixed(2)+' 张/秒；'+latest.stale_devices+' 台进度超过 '+latest.progress_timeout_s+' 秒';
    }
    function load() {
      if (loading || document.hidden)return;
      loading=true;
      api('/api/managed/devices?summary=throughput').then(function (data) {latest=data;received=Date.now();})
        .catch(function () {latest=null;}).finally(function () {loading=false;render();});
    }
    function tick() {render();load();setTimeout(tick,5000);}
    document.addEventListener('visibilitychange',function () {if (!document.hidden)load();});
    document.querySelector('#refresh').addEventListener('click',load);
    tick();
  }};
})();
