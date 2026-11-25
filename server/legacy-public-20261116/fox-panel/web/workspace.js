/* 工作区分包列表与设备队列，复用登录会话和 CSRF。 */
(function(){
  "use strict";
  function node(tag,text){var n=document.createElement(tag);if(text)n.textContent=text;return n;}
  function signature(ws,all,writable){return JSON.stringify([ws,(all.devices||[]).map(function(d){return [d.device_id,d.user_id,d.workspace_id,d.effective_workspace_id,d.status];}),(all.users||[]).map(function(u){return [u.user_id,u.workspace_id,u.effective_workspace_id,u.status];}),writable()]);}
  function csrf(){var m=document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);return m?decodeURIComponent(m[1]):"";}
  async function post(path,body){
    var r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf()},body:JSON.stringify(body)});
    var data=await r.json();if(!r.ok||data.ok===false)throw new Error(data.error||"请求失败");return data;
  }
  window.FoxWorkspace={render:function(card,ws,api,writable,all){
    var previous=card.querySelector("[data-workspace-packages]");
    if(previous&&previous.dataset.ws===ws.ws_id&&previous.refreshUsers)previous.refreshUsers(all);
    if(previous&&previous.dataset.ws===ws.ws_id&&writable()&&(previous.dataset.editing==='1'||previous.dataset.busy==='1'||previous.querySelector('[data-editing="1"],[data-busy="1"]')))return;
    var currentSignature=signature(ws,all,writable);
    if(previous&&previous.dataset.signature===currentSignature)return;
    var initial=previous&&previous.dataset.ws===ws.ws_id?previous.dataset.activeView:'overview';
    if(previous){previous.detach();previous.remove();}
    var box=node("section"),message=node("p"),list=node("div"),devices=node("div"),more=node("button","加载更多"),send=node("button","下发选中分包");
    var advanced=node('details'),diagnostics=node('div'),loaded=new Map(),packagePicker,userPicker,devicePicker;
    function group(parent,title){var section=node('details');section.className='fp-workspace-group';section.append(node('summary',title));parent.append(section);return section;}
    box.dataset.workspacePackages="";box.dataset.ws=ws.ws_id;box.className="fp-workspace-tools";
    box.dataset.signature=currentSignature;
    more.type=send.type="button";more.className=send.className="fp-btn fp-btn--sm";
    var selected=new Map(),chosen=new Set(),selectedUsers=new Set(),offset=0,total=0,busy=false,loading=false,loadId=0,configDirty=false;
    function edited(){box.dataset.editing=selected.size||chosen.size||selectedUsers.size||configDirty?'1':'0';}
    var sections=window.FoxWorkspaceSections.mount(box,card,ws,initial,function(view){if(selection){if(view==='results')resultSelection.append(selection);else manual.querySelector('summary').after(selection);}if(packagePicker)updatePackageItems();});
    box.detach=sections.detach;
    var dispatch=sections.panels.dispatch,results=sections.panels.results,settings=sections.panels.settings;
    (ws.legacy_repository_overlaps||[]).forEach(function(other){settings.append(node('p','检测到旧仓库重复归属：'+other.repos.join('、')+' 同时位于 '+(other.name||other.workspace_id)+'。请先合并旧工作区；仓库只允许属于一个工作区。'));});
    dispatch.append(node('h3','在线派单'),node('p','按成员预派分包；已保存配置自动带入，成员上线托管后领取。'));
    var assistant=window.FoxDispatchAssistant.mount(dispatch,ws,all,api,post,writable,async function(){rememberOptions();reset();await load();},box);
    var offlineLink=node('div'),offlineOpen=node('a','打开独立离线工单');offlineLink.className='fp-workspace-offline';offlineOpen.href='/offline-workorders?ws='+encodeURIComponent(ws.ws_id);offlineOpen.className='fp-btn fp-btn--sm';
    offlineLink.append(node('p','成员无需连接中心时，在独立离线工单页编辑、生成和下载任务。'),offlineOpen);dispatch.append(offlineLink);
    var manual=group(dispatch,'高级派单：手动选择、指定设备与分项分配'),selection=node('div');selection.className='fp-workspace-selection';selection.append(list,more);manual.append(selection,devices,send);
    manual.open=false;advanced.className='fp-workspace-group';advanced.append(node('summary','指定设备与 TAG / NL 分项分配'));manual.append(advanced);
    settings.append(node('h3','派单与仓库设置'));
    window.FoxDispatchTemplate.mount(settings,ws,post,writable,function(){rememberOptions();assistant.syncDefaults();if(!selected.size&&!selectedUsers.size){dispatchMode.value=(ws.preassignment_template||{}).mode||'both';artist.value='template';executionSource.value='template';}},box);
    var resultSelection=node('div');results.append(node('h3','结果与核验'),node('p','选择分包后下载结果、查看逐图核验或处理冲突。离线成员交回的 ZIP 在下方导入。'),resultSelection);
    message.className='fp-workspace-status';message.setAttribute('role','status');box.append(message);
    sections.show(initial||'overview');
    card.appendChild(box);
    packagePicker=window.FoxSelection.mount(list,{title:'分片',max:500,disabled:function(){return busy||loading||!writable();},onChange:function(ids){
      selected.clear();ids.forEach(function(id){var p=loaded.get(id);if(p)selected.set(id,{repo:p.repo,id:p.id,claimable:p.claimable});});update();
    }});
    var directory=window.FoxUsers.catalog(all,ws.ws_id),eligible=directory.devices,userDevices=directory.groups;
    var userSignature=directory.signature;
    function userItems(data){return window.FoxUsers.catalog(data,ws.ws_id).items.map(function(item){var u=(data.users||[]).find(function(user){return user.user_id===item.id;});var workspace=u&&(u.effective_workspace_id||u.workspace_id);return Object.assign({},item,{disabled:!u||u.status!=='active'||workspace!==ws.ws_id,detail:workspace===ws.ws_id?'可预派；成员无需当前在线':item.detail});});}
    userPicker=window.FoxUsers.mount(devices,{title:'用户',items:userItems(all),disabled:function(){return busy||!writable();},onAction:function(item,trigger){
      document.dispatchEvent(new CustomEvent('fox-assign-user',{detail:{user_id:item.id,ws:ws,all:all,trigger:trigger}}));
    },onChange:function(ids){
      selectedUsers=new Set(ids);
      userDevices.forEach(function(devs,user){var count=devs.filter(function(did){return chosen.has(did);}).length;
        if(!ids.includes(user))devs.forEach(function(did){chosen.delete(did);});
        else if(!count)devs.forEach(function(did){chosen.add(did);});
      });
      devicePicker.setValues(Array.from(chosen));update();
    }});
    devicePicker=window.FoxSelection.mount(advanced,{title:'处理设备',items:eligible.map(function(d){return {id:d.device_id,label:d.user_id+' · '+d.device_id};}),disabled:function(){return busy||!writable();},onChange:function(ids){
      chosen=new Set(ids);selectedUsers=new Set(Array.from(userDevices.keys()).filter(function(user){return userDevices.get(user).some(function(did){return chosen.has(did);});}));userPicker.setValues(Array.from(selectedUsers));update();
    }});
    var targetLabel=node('label','分配方式 '),dispatchTarget=node('select');dispatchTarget.setAttribute('aria-label','分配方式');
    [['user','按用户预派（推荐）'],['device','指定已登记设备']].forEach(function(v){var o=node('option',v[1]);o.value=v[0];dispatchTarget.append(o);});targetLabel.append(dispatchTarget);devices.prepend(targetLabel);
    dispatchTarget.onchange=function(){if(start)start.parentElement.hidden=dispatchTarget.value==='user';update();};
    if(window.FoxAllocation) window.FoxAllocation.mount(group(settings,'仓库成员许可与自动分配'),ws,all,writable,async function(){var data=await api('/api/workspaces'),current=data.workspaces.find(function(w){return w.ws_id===ws.ws_id;});if(current){ws.allocation_mode=current.allocation_mode;ws.repo_devices=current.repo_devices;ws.repo_users=current.repo_users;}box.dataset.signature=signature(ws,all,writable);reset();await load();});
    if(window.FoxSourceCatalog) window.FoxSourceCatalog.mount(group(settings,'源清单与核验设置'),ws.repos||[],writable);
    if(window.FoxArtist)window.FoxArtist.mount(group(settings,'画师注入与仓库分表'),ws.repos||[],writable);
    if(window.FoxResultExports)window.FoxResultExports.mount(results,selected,post);
    var review=group(results,'逐图核验、冲突与已有结果修复'),reviewActions=node('div');reviewActions.className='fp-workspace-actions';review.append(reviewActions,diagnostics);
    if(window.FoxSourceCatalog) window.FoxSourceCatalog.actions(reviewActions,selected,post,async function(){reset();await load();},message,writable,box);
    if(window.FoxArtist)window.FoxArtist.actions(reviewActions,selected,post,async function(){reset();await load();},message,writable,box);
    if(window.FoxDispatchPlans)window.FoxDispatchPlans.mount(group(dispatch,'导出已分配派工表'),ws,writable,post);
    function update(){edited();send.disabled=busy||loading||!selected.size||!(dispatchTarget&&dispatchTarget.value==='device'?chosen.size:selectedUsers.size)||!writable()||Array.from(selected.values()).some(function(p){return p.claimable===false;});if(cancelPreassigned)cancelPreassigned.disabled=busy||loading||!selected.size||!writable();if(artist)artist.disabled=busy||!writable();if(packagePicker)packagePicker.refresh();if(userPicker)userPicker.refresh();if(devicePicker)devicePicker.refresh();advanced.dispatchEvent(new Event('change'));reviewActions.dispatchEvent(new Event('change'));results.dispatchEvent(new Event('change'));}
    function check(parent,text,onChange){var label=node("label"),input=node("input");input.type="checkbox";label.className="fp-check";label.append(input,node("span",text));input.addEventListener("change",function(){onChange(input.checked);update();});parent.appendChild(label);return input;}
    if(window.FoxScheduling) window.FoxScheduling.assign(box,selected,all,async function(text,saved){if(saved){reset();await load();}message.textContent=text;},function(){return writable()&&!busy;},advanced);
    var start=check(devices,"同时启动",function(){});start.checked=true;
    start.parentElement.hidden=true;
    devices.append(node('p','按用户预派：成员上线托管后自动领取；已主动暂停的设备保持暂停。'));
    var last=ws.preassignment_last||{},partsLabel=node('label','预派打标内容 '),dispatchMode=node('select');dispatchMode.setAttribute('aria-label','分片预派打标内容');[['both','TAG + NL'],['nl','只出 NL'],['tag','只出 TAG']].forEach(function(v){var o=node('option',v[1]);o.value=v[0];dispatchMode.append(o);});dispatchMode.value=last.mode||(ws.preassignment_template||{}).mode||'both';partsLabel.append(dispatchMode);devices.append(partsLabel);
    var executionLabel=node('label','预派执行配置 '),executionSource=node('select');executionSource.setAttribute('aria-label','分片预派执行配置');[['last','沿用上次成功派单配置'],['template','使用工作区默认模板'],['inherit','沿用成员本机配置']].forEach(function(v){var o=node('option',v[1]);o.value=v[0];o.disabled=v[0]==='last'&&!last.mode;executionSource.append(o);});executionSource.value=last.mode?'last':'template';executionLabel.append(executionSource);devices.append(executionLabel,node('p','按工作区记住成功派单的配置；需要调整模型或并发时使用快速助手。'));
    var cancelPreassigned=node('button','取消选中分包的用户预派');cancelPreassigned.type='button';cancelPreassigned.className='fp-btn fp-btn--sm';advanced.append(cancelPreassigned);
    cancelPreassigned.onclick=async function(){if(busy||!writable())return;busy=true;box.dataset.busy='1';update();try{var r=await post('/api/workspace-dispatch',{action:'cancel_preassignment',ws_id:ws.ws_id,items:Array.from(selected.values()),mode:dispatchMode.value});reset();await load();message.textContent='已取消 '+r.canceled+' 个尚未领取的用户分项';}catch(e){message.textContent=e.message;}finally{busy=false;box.dataset.busy='0';update();}};
    var artistLabel=node('label','画师注入 '),artist=node('select');
    artist.setAttribute('aria-label','派单画师注入');
    [['last','沿用上次派单画师设置'],['template','预派单使用默认模板'],['inherit','沿用仓库设置（未配置时沿用客户端）'],['off','关闭注入'],['nl','仅 NL 描述'],['tag','仅 TAG 标签'],['both','NL 与 TAG 都注入']].forEach(function(v){var option=node('option',v[1]);option.value=v[0];option.disabled=v[0]==='last'&&!last.mode;artist.append(option);});
    artistLabel.append(artist);devices.append(artistLabel,node('p','应用于本次所选分包，已有产物不自动重打。仅注入已有来源的画师。指定设备时沿用仓库画师设置，或选择明确的开关。'));
    artist.value=last.mode?'last':(ws.preassignment_template||{}).mode?'template':'inherit';
    function rememberOptions(){var has=!!(ws.preassignment_last||{}).mode;[executionSource,artist].forEach(function(select){select.querySelector('option[value="last"]').disabled=!has;if(!has&&select.value==='last')select.value='template';});}
    artist.disabled=!writable();
    devices.addEventListener('change',function(){configDirty=true;update();});
    function annotation(){return ['inherit','template','last'].includes(artist.value)?{}:{artist_inject:artist.value==='nl'||artist.value==='both',artist_inject_tags:artist.value==='tag'||artist.value==='both'};}
    function reset(){++loadId;selected.clear();loaded.clear();packagePicker.setValues([]);packagePicker.setItems([]);diagnostics.replaceChildren();offset=0;}
    function updatePackageItems(){packagePicker.setItems(Array.from(loaded,function(entry){var p=entry[1];return {id:entry[0],label:p.id,
      detail:p.repo+' · '+(p.missing_parts.length?'缺 '+p.missing_parts.join(' / ').toUpperCase():'已完成')+(p.pending_results||[]).map(function(r){return ' · '+r.device_id+' 本地完成、待回传';}).join('')+(p.working_device?' · 处理中':(p.assignments||[]).map(function(a){return ' · '+a.target_id+'：'+(a.state||'待领取');}).join('')),disabled:box.dataset.activeView!=='results'&&!p.claimable&&!p.source_submitted};}),total);}
    async function load(){if(loading)return;var id=++loadId;loading=true;more.disabled=true;update();try{
      var page=await api("/api/workspace-packages?ws_id="+encodeURIComponent(ws.ws_id)+"&offset="+offset);
      if(id!==loadId||!box.isConnected)return;
      total=page.total;
      message.textContent=page.total?"共 "+page.total+" 个分片":page.empty_reason||"暂无分片";
      page.items.forEach(function(p){
        var key=p.repo+'/'+p.id;loaded.set(key,p);
        var detail=node('details');detail.append(node('summary',key));
        if(window.FoxScheduling)detail.append(node('p',window.FoxScheduling.counts(p.verification)));
        if(window.FoxSourceCatalog){detail.append(node('p',window.FoxSourceCatalog.status(p.source_verification)));window.FoxSourceCatalog.detail(detail,p,writable,async function(notice){reset();await load();var reopened=Array.from(diagnostics.querySelectorAll('[data-source-package]')).find(function(e){return e.dataset.sourcePackage===p.id&&e.dataset.sourceRepo===p.repo;});if(reopened){reopened.dataset.notice=notice||'';reopened.parentElement.open=true;reopened.open=true;}});}
        detail.append(node('p',(p.leases||[]).map(function(l){return l.part.toUpperCase()+' · '+l.device_id+' · '+(l.kind==='prelock'?'预锁':'执行');}).join('；')));
        detail.append(node('p',(p.assignments||[]).map(function(a){return a.part.toUpperCase()+' · '+(a.target_type==='user'?'用户':'设备')+' · '+a.target_id+' · '+(a.state||'待领取');}).join('；')));
        diagnostics.append(detail);
      });
      updatePackageItems();
      more.hidden=page.next_offset===null;offset=page.next_offset||0;
    }catch(e){if(id===loadId)message.textContent="读取失败："+e.message+"，可重试";}finally{loading=false;if(id===loadId){more.disabled=false;update();}}}
    var refresh=node("button","刷新分片");refresh.type="button";refresh.className="fp-btn fp-btn--sm";
    refresh.addEventListener("click",function(){if(busy||loading)return;reset();load();});selection.append(refresh);
    more.addEventListener("click",load);send.addEventListener("click",async function(){if(busy||box.dataset.busy==='1'||!writable()||!selected.size)return;busy=true;box.dataset.busy='1';update();try{
      var body={ws_id:ws.ws_id,items:Array.from(selected.values()),start:start.checked,annotation:annotation()};
      if(dispatchTarget.value==='device')body.devices=Array.from(chosen);else{body.users=Array.from(selectedUsers);body.mode=dispatchMode.value;if(artist.value==='template')delete body.annotation;else if(artist.value==='last')body.annotation=(ws.preassignment_last||{}).annotation||{};if(executionSource.value==='last'){body.config=(ws.preassignment_last||{}).config||{};body.config_source=(ws.preassignment_last||{}).config_source||'inherit';}else if(executionSource.value==='inherit'){body.config={};body.config_source='inherit';}}
      var result=await post("/api/workspace-dispatch",body);
      if(body.users){ws.preassignment_last=result.preassignment_last;rememberOptions();}
      reset();await load();
      message.textContent="已分配 "+result.assigned.length+" 个分项"+(body.users?'；等待成员上线托管后领取':'')+(result.starts||[]).map(function(s){return "；"+s.device_id+"："+(s.status==="queued"?"开始指令已下发":s.status==="running"?"正在执行":s.message);}).join("");
    }catch(e){message.textContent="下发失败："+e.message;}finally{busy=false;box.dataset.busy='0';update();}});
    var jobsHost=group(results,'已有离线任务：下载、取消或删除工单'),jobs=node('div');jobs.className='fp-dispatch-jobs';jobsHost.append(jobs);
    box.refreshUsers=function(next){
      if(busy||box.dataset.busy==='1')return;
      var updated=window.FoxUsers.catalog(next,ws.ws_id),nextSignature=updated.signature;
      if(nextSignature===userSignature)return;
      userSignature=nextSignature;all=next;eligible=updated.devices;userDevices=updated.groups;
      var valid=new Set(eligible.map(function(d){return d.device_id;}));
      chosen.forEach(function(id){if(!valid.has(id))chosen.delete(id);});
      var users=userItems(next),validUsers=new Set(users.filter(function(item){return !item.disabled;}).map(function(item){return item.id;}));
      selectedUsers.forEach(function(id){if(!validUsers.has(id))selectedUsers.delete(id);});userPicker.setItems(users);userPicker.setValues(Array.from(selectedUsers));
      devicePicker.setItems(eligible.map(function(d){return {id:d.device_id,label:d.user_id+' · '+d.device_id};}));
      devicePicker.setValues(Array.from(chosen));
      assistant.refreshUsers(next);update();
    };
    function save(data){var blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=data.id+".json";a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);}
    async function loadJobs(jobOffset){try{var data=await api("/api/offline-tasks?ws_id="+encodeURIComponent(ws.ws_id)+"&offset="+(jobOffset||0));if(!jobOffset)jobs.replaceChildren();data.items.forEach(function(job){
      var names={ready:"等待离线处理",completed:"已回传完成",failed:"任务已释放或失败",canceled:"已取消"};
      var row=node("div",job.user_id+" · "+job.id+" · "+(names[job.status]||job.status));row.className='fp-dispatch-job';
      window.FoxOfflineJobs.mount(row,job,function(path,body){return body?post(path,body):api(path);},writable,async function(){await loadJobs();reset();await load();},message,save);jobs.appendChild(row);
    });if(data.next_offset!==null&&data.next_offset!==undefined){var next=node("button","更多离线任务");next.type="button";next.addEventListener("click",function(){next.remove();loadJobs(data.next_offset);});jobs.append(next);}}catch(e){jobs.textContent="离线任务读取失败："+e.message;}}
    if(window.FoxOfflineImports)window.FoxOfflineImports.mount(results,ws,post,writable,async function(){await loadJobs();reset();await load();});
    loadJobs();
    update();load();
  }};
})();
