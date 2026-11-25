/* 独立工单编辑页面：草稿在浏览器，真实占用仍由服务器原离线分配事务完成。 */
(function(){
  'use strict';
  var $=function(id){return document.getElementById('wo-'+id);},workspaces=[],users=[],loaded=new Map(),chosen=new Set(),draft=new Map(),configs={},next=null,busy=false,writable=false,jobNext=null;
  function node(tag,text){var e=document.createElement(tag);if(text)e.textContent=text;return e;}
  function csrf(){var m=document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);return m?decodeURIComponent(m[1]):'';}
  async function request(path,body){var response=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)}:{}),data=await response.json();if(!response.ok||data.ok===false)throw new Error(data.error||'请求失败');return data;}
  function ws(){return workspaces.find(function(w){return w.ws_id===$('workspace').value;});}
  function key(p){return p.repo+'/'+(p.id||p.package);}
  function available(p){return p.claimable!==false&&p.status==='available'&&!(p.leases||[]).length&&!(p.assignments||[]).length&&!p.assigned_device&&!p.reserved_user&&!p.source_submitted;}
  function save(){try{localStorage.setItem('lulynx-offline-draft:'+ws().ws_id,JSON.stringify({items:Array.from(draft.values()),configs:configs}));}catch(e){}}
  function changed(){save();$('agree').checked=false;renderDraft();update();}
  function update(){document.querySelectorAll('button').forEach(function(b){if(!b.closest('[data-offline-job]'))b.disabled=busy;});document.querySelectorAll('[data-offline-job]').forEach(function(row){row.dispatchEvent(new Event('offline-jobs-update'));});$('generate').disabled=busy||!writable||!draft.size||!$('agree').checked;
    ['add','clear','save-config'].forEach(function(id){$(id).disabled=busy||!writable;});$('more').disabled=busy||next===null;$('jobs-more').disabled=busy;
    document.querySelectorAll('input,select').forEach(function(e){e.disabled=busy||(e.dataset.package!==undefined&&!available(loaded.get(e.dataset.package)));});}
  function defaults(){var value=(ws()||{}).preassignment_template||{};return {config:Object.assign({mode:value.mode||'nl',tag_backend:'pixai',nl_engine:'native',nl_precision:'q6'},value.config||{}),annotation:value.annotation||{}};}
  var form=window.FoxExecutionForm.mount($('config'),{prefix:'工单',mode:'nl',engine:'native',precision:'q6',tag:'pixai',parallel:true});
  function currentConfig(){return {config:form.value(),annotation:window.FoxDispatchTemplate.annotation($('artist').value)};}
  function showConfig(){var value=configs[$('user').value]||defaults();form.setValue(value.config);$('artist').value=window.FoxDispatchTemplate.artistValue(value.annotation);}
  function renderPackages(){var term=$('search').value.toLowerCase(),rows=Array.from(loaded.values()).filter(function(p){return key(p).toLowerCase().includes(term);});$('packages').replaceChildren();
    rows.forEach(function(p){var row=node('tr'),check=node('input');check.type='checkbox';check.dataset.package=key(p);check.checked=chosen.has(key(p));check.disabled=busy||!available(p);check.setAttribute('aria-label','选择 '+p.id);
      check.onchange=function(){if(check.checked)chosen.add(key(p));else chosen.delete(key(p));};var cell=node('td');cell.append(check);row.append(cell,node('td',key(p)),node('td',(p.missing_parts||[]).join(' + ').toUpperCase()),node('td',available(p)?'可生成工单':p.status==='completed'?'已完成':'已占用或待核验'));$('packages').append(row);});
    $('count').textContent='已加载 '+loaded.size+' 个分包；查询仅获取状态，尚未占用。';}
  function options(select,selected){users.filter(function(u){return u.status==='active';}).forEach(function(u){var o=node('option',u.user_id);o.value=u.user_id;select.append(o);});select.value=selected||'';}
  function renderDraft(){var rows=Array.from(draft.values()),sort=$('sort').value;rows.sort(function(a,b){return (sort==='user'?a.user_id.localeCompare(b.user_id):a.id.localeCompare(b.id))||key(a).localeCompare(key(b));});$('draft').replaceChildren();
    rows.forEach(function(p){var row=node('tr'),select=node('select');options(select,p.user_id);select.setAttribute('aria-label','修改 '+p.id+' 的工单成员');select.onchange=function(){p.user_id=select.value;if(!configs[p.user_id])configs[p.user_id]=defaults();changed();};var member=node('td');member.append(select);var remove=node('button','移除');remove.type='button';remove.onclick=function(){draft.delete(key(p));changed();};var action=node('td');action.append(remove);row.append(member,node('td',key(p)),action);$('draft').append(row);});
    var counts={};rows.forEach(function(p){counts[p.user_id]=(counts[p.user_id]||0)+1;});$('summary').textContent=rows.length?Object.keys(counts).sort().map(function(id){var value=configs[id]||defaults(),c=value.config;return id+'：'+counts[id]+' 个分包 · '+c.mode.toUpperCase()+' · TAG '+c.tag_backend+' · NL '+c.nl_engine+' / '+(c.nl_model||'自动模型')+' / '+c.nl_precision+' · 画师 '+window.FoxDispatchTemplate.artistValue(value.annotation);}).join('\n'):'尚未添加分包。';}
  async function query(more){if(!ws())return;busy=true;update();try{var page=await request('/api/workspace-packages?ws_id='+encodeURIComponent(ws().ws_id)+'&offset='+(more?next:0));if(!more){loaded.clear();chosen.clear();}page.items.forEach(function(p){loaded.set(key(p),p);});next=page.next_offset===undefined?null:page.next_offset;renderPackages();$('message').textContent='分包状态已更新；现有草稿保留。';}catch(e){$('message').textContent=e.message;}finally{busy=false;update();}}
  function download(data,name){var url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),a=node('a');a.href=url;a.download=name;a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);}
  function ready(manifest){var row=node('div',manifest.identity.user_id+' · '+manifest.tasks.length+' 个分包 · '+manifest.id),button=node('button','下载成员工单');button.type='button';button.onclick=function(){download(manifest,manifest.id+'.json');};row.append(button);$('downloads').append(row);}
  async function jobs(more){if(!ws())return;try{var result=await request('/api/offline-tasks?ws_id='+encodeURIComponent(ws().ws_id)+'&offset='+(more?jobNext:0));if(!more)$('jobs').replaceChildren();result.items.forEach(function(job){var row=node('div',job.user_id+' · '+job.id+' · '+job.status);
    window.FoxOfflineJobs.mount(row,job,request,function(){return writable&&!busy;},async function(){await jobs(false);await query(false);},$('message'),function(data){download(data,data.id+'.json');});$('jobs').append(row);});jobNext=result.next_offset;$('jobs-more').hidden=jobNext===null||jobNext===undefined;}catch(e){$('message').textContent=e.message;}}
  $('workspace').onchange=function(){loaded.clear();chosen.clear();next=null;draft.clear();configs={};$('downloads').replaceChildren();
    try{var stored=JSON.parse(localStorage.getItem('lulynx-offline-draft:'+ws().ws_id)||'{}');configs=stored.configs||{};(stored.items||[]).forEach(function(p){draft.set(key(p),p);});}catch(e){}
    showConfig();renderPackages();renderDraft();update();void jobs(false);};
  $('user').onchange=showConfig;$('query').onclick=function(){void query(false);};$('more').onclick=function(){void query(true);};$('search').oninput=renderPackages;$('sort').onchange=renderDraft;
  $('all').onchange=function(){loaded.forEach(function(p){if(available(p)&&key(p).toLowerCase().includes($('search').value.toLowerCase())){if($('all').checked)chosen.add(key(p));else chosen.delete(key(p));}});renderPackages();};
  $('save-config').onclick=function(){if(!$('user').value)return;configs[$('user').value]=currentConfig();changed();$('message').textContent='当前成员配置已保存到草稿。';};
  $('add').onclick=function(){if(!writable||!$('user').value||!chosen.size){$('message').textContent='请选择成员并勾选分包。';return;}if(new Set([].concat(Array.from(draft.keys()),Array.from(chosen))).size>500){$('message').textContent='每次草稿最多 500 个分包。';return;}
    configs[$('user').value]=currentConfig();chosen.forEach(function(k){var p=loaded.get(k);draft.set(k,{repo:p.repo,id:p.id,user_id:$('user').value});});chosen.clear();renderPackages();changed();};
  $('clear').onclick=function(){draft.clear();changed();};$('agree').onchange=update;$('jobs-refresh').onclick=function(){void jobs(false);};$('jobs-more').onclick=function(){void jobs(true);};
  $('generate').onclick=async function(){if(busy||!writable||!draft.size||!$('agree').checked)return;busy=true;update();var groups={};draft.forEach(function(p){(groups[p.user_id]||(groups[p.user_id]=[])).push(p);});var completed=0;
    try{for(var user of Object.keys(groups).sort()){var value=configs[user]||defaults();$('message').textContent='正在登记 '+user+' 的工单…';var result=await request('/api/offline-export',{ws_id:ws().ws_id,user_id:user,server:location.origin,items:groups[user].map(function(p){return {repo:p.repo,id:p.id};}),config:value.config,annotation:value.annotation});ready(result.manifest);groups[user].forEach(function(p){draft.delete(key(p));});save();completed++;}
      $('message').textContent='已生成 '+completed+' 位成员的工单，请分别下载。客户端导入后可离线执行。';
    }catch(e){$('message').textContent='已生成 '+completed+' 位成员的工单；剩余草稿保留。停止原因：'+e.message;}finally{busy=false;$('agree').checked=false;renderDraft();update();void jobs(false);}};
  async function boot(){busy=true;update();try{var result=await Promise.all([request('/api/workspaces'),request('/api/users'),request('/api/meta')]);workspaces=result[0].workspaces||[];users=result[1].users||[];writable=!!(result[2].write&&result[2].write.enabled);
      workspaces.forEach(function(w){var o=node('option',w.name||w.ws_id);o.value=w.ws_id;$('workspace').append(o);});var active=users.filter(function(u){return u.status==='active';});options($('user'),active.length?active[0].user_id:'');
      var wanted=new URLSearchParams(location.search).get('ws');if(workspaces.some(function(w){return w.ws_id===wanted;}))$('workspace').value=wanted;
      if(workspaces.length)$('workspace').onchange();else $('message').textContent='请先在调度中心创建工作区。';
    }catch(e){$('message').textContent=e.message;}finally{busy=false;update();}}
  void boot();
})();
