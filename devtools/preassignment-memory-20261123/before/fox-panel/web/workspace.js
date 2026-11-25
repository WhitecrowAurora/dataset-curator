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
    if(previous)previous.remove();
    var box=node("section"),message=node("p"),list=node("div"),devices=node("div"),more=node("button","加载更多"),send=node("button","下发选中分包"),offline=node("fieldset");
    var advanced=node('details'),diagnostics=node('div'),loaded=new Map(),packagePicker,userPicker,devicePicker,offlineUser;
    advanced.className='fp-workspace-advanced';advanced.append(node('summary','设备、分项分配、画师表与核验详情'));
    box.dataset.workspacePackages="";box.dataset.ws=ws.ws_id;box.className="fp-card";
    box.dataset.signature=currentSignature;
    more.type=send.type="button";more.className=send.className="fp-btn fp-btn--sm";
    var selected=new Map(),chosen=new Set(),selectedUsers=new Set(),offset=0,busy=false,loading=false,loadId=0,offlineDirty=false;
    function edited(){box.dataset.editing=selected.size||chosen.size||selectedUsers.size||offlineDirty?'1':'0';}
    offline.addEventListener('input',function(){offlineDirty=true;edited();});
    offline.addEventListener('change',function(){offlineDirty=true;edited();});
    box.append(node("h3","工作区分片"),node("p","按用户预派分包，成员上线托管后领取。首次配置可使用快速助手。"));
    var assistant=window.FoxDispatchAssistant.mount(box,ws,all,api,post,writable,async function(){reset();await load();});
    window.FoxDispatchTemplate.mount(box,ws,post,writable,function(){if(!selected.size&&!selectedUsers.size){dispatchMode.value=(ws.preassignment_template||{}).mode||'both';artist.value='template';}});
    box.append(message,list,more,devices,send,advanced,offline);
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
    if(window.FoxAllocation) window.FoxAllocation.mount(advanced,ws,all,writable,async function(){var data=await api('/api/workspaces'),current=data.workspaces.find(function(w){return w.ws_id===ws.ws_id;});if(current){ws.allocation_mode=current.allocation_mode;ws.repo_devices=current.repo_devices;ws.repo_users=current.repo_users;}box.dataset.signature=signature(ws,all,writable);reset();await load();});
    if(window.FoxSourceCatalog) window.FoxSourceCatalog.mount(advanced,ws.repos||[],writable);
    if(window.FoxSourceCatalog) window.FoxSourceCatalog.actions(box,selected,post,async function(){reset();await load();},message,writable);
    if(window.FoxArtist){window.FoxArtist.mount(advanced,ws.repos||[],writable);window.FoxArtist.actions(box,selected,post,async function(){reset();await load();},message,writable);}
    if(window.FoxResultExports)window.FoxResultExports.mount(box,selected,post);
    if(window.FoxDispatchPlans)window.FoxDispatchPlans.mount(box,ws,writable,post);
    advanced.append(diagnostics);
    function update(){edited();send.disabled=busy||loading||!selected.size||!(dispatchTarget&&dispatchTarget.value==='device'?chosen.size:selectedUsers.size)||!writable()||Array.from(selected.values()).some(function(p){return p.claimable===false;});if(cancelPreassigned)cancelPreassigned.disabled=busy||loading||!selected.size||!writable();if(artist)artist.disabled=busy||!writable();if(exportButton)exportButton.disabled=busy||loading||!writable()||!selected.size;if(packagePicker)packagePicker.refresh();if(userPicker)userPicker.refresh();if(devicePicker)devicePicker.refresh();if(offlineUser)offlineUser.disable(busy||!writable());advanced.dispatchEvent(new Event('change'));}
    function check(parent,text,onChange){var label=node("label"),input=node("input");input.type="checkbox";label.className="fp-check";label.append(input,node("span",text));input.addEventListener("change",function(){onChange(input.checked);update();});parent.appendChild(label);return input;}
    if(window.FoxScheduling) window.FoxScheduling.assign(box,selected,all,async function(text,saved){if(saved){reset();await load();}message.textContent=text;},function(){return writable()&&!busy;},advanced);
    var start=check(devices,"同时启动",function(){});start.checked=true;
    start.parentElement.hidden=true;
    devices.append(node('p','按用户预派：成员上线托管后自动领取；已主动暂停的设备保持暂停。'));
    var partsLabel=node('label','预派打标内容 '),dispatchMode=node('select');dispatchMode.setAttribute('aria-label','分片预派打标内容');[['both','TAG + NL'],['nl','只出 NL'],['tag','只出 TAG']].forEach(function(v){var o=node('option',v[1]);o.value=v[0];dispatchMode.append(o);});dispatchMode.value=(ws.preassignment_template||{}).mode||'both';partsLabel.append(dispatchMode);devices.append(partsLabel,node('p','执行配置默认使用工作区预派单模板；需要本次单独指定时使用快速助手。'));
    var cancelPreassigned=node('button','取消选中分包的用户预派');cancelPreassigned.type='button';cancelPreassigned.className='fp-btn fp-btn--sm';advanced.append(cancelPreassigned);
    cancelPreassigned.onclick=async function(){if(busy||!writable())return;busy=true;box.dataset.busy='1';update();try{var r=await post('/api/workspace-dispatch',{action:'cancel_preassignment',ws_id:ws.ws_id,items:Array.from(selected.values()),mode:dispatchMode.value});reset();await load();message.textContent='已取消 '+r.canceled+' 个尚未领取的用户分项';}catch(e){message.textContent=e.message;}finally{busy=false;box.dataset.busy='0';update();}};
    var artistLabel=node('label','画师注入 '),artist=node('select');
    artist.setAttribute('aria-label','派单画师注入');
    [['template','预派单使用默认模板（设备／离线沿用仓库）'],['inherit','沿用仓库设置（未配置时沿用客户端）'],['off','关闭注入'],['nl','仅 NL 描述'],['tag','仅 TAG 标签'],['both','NL 与 TAG 都注入']].forEach(function(v){var option=node('option',v[1]);option.value=v[0];artist.append(option);});
    artistLabel.append(artist);devices.append(artistLabel,node('p','应用于本次所选分包（含离线清单），已有产物不自动重打。仅注入已有来源的画师；客户端需更新至支持派单画师选项的版本。'));
    artist.value=(ws.preassignment_template||{}).mode?'template':'inherit';
    artist.disabled=!writable();
    artist.addEventListener('change',function(){offlineDirty=true;update();});
    function annotation(){return artist.value==='inherit'||artist.value==='template'?{}:{artist_inject:artist.value==='nl'||artist.value==='both',artist_inject_tags:artist.value==='tag'||artist.value==='both'};}
    function reset(){++loadId;selected.clear();loaded.clear();packagePicker.setValues([]);packagePicker.setItems([]);diagnostics.replaceChildren();offset=0;}
    async function load(){if(loading)return;var id=++loadId;loading=true;more.disabled=true;update();try{
      var page=await api("/api/workspace-packages?ws_id="+encodeURIComponent(ws.ws_id)+"&offset="+offset);
      if(id!==loadId||!box.isConnected)return;
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
      packagePicker.setItems(Array.from(loaded,function(entry){var p=entry[1];return {id:entry[0],label:p.id,
        detail:p.repo+' · '+(p.missing_parts.length?'缺 '+p.missing_parts.join(' / ').toUpperCase():'已完成')+(p.working_device?' · 处理中':(p.assignments||[]).map(function(a){return ' · '+a.target_id+'：'+(a.state||'待领取');}).join('')),disabled:!p.claimable&&!p.source_submitted};}),page.total);
      more.hidden=page.next_offset===null;offset=page.next_offset||0;
    }catch(e){if(id===loadId)message.textContent="读取失败："+e.message+"，可重试";}finally{loading=false;if(id===loadId){more.disabled=false;update();}}}
    var refresh=node("button","刷新分片");refresh.type="button";refresh.className="fp-btn fp-btn--sm";
    refresh.addEventListener("click",function(){if(busy||loading)return;box.dataset.editing='0';reset();load();});box.append(refresh);
    more.addEventListener("click",load);send.addEventListener("click",async function(){if(busy||box.dataset.busy==='1'||!writable()||!selected.size)return;busy=true;box.dataset.busy='1';update();try{
      var body={ws_id:ws.ws_id,items:Array.from(selected.values()),start:start.checked,annotation:annotation()};
      if(dispatchTarget.value==='device')body.devices=Array.from(chosen);else{body.users=Array.from(selectedUsers);body.mode=dispatchMode.value;if(artist.value==='template')delete body.annotation;}
      var result=await post("/api/workspace-dispatch",body);
      reset();await load();
      message.textContent="已分配 "+result.assigned.length+" 个分项"+(body.users?'；等待成员上线托管后领取':'')+(result.starts||[]).map(function(s){return "；"+s.device_id+"："+(s.status==="queued"?"开始指令已下发":s.status==="running"?"正在执行":s.message);}).join("");
    }catch(e){message.textContent="下发失败："+e.message;}finally{busy=false;box.dataset.busy='0';update();}});
    var user=node("input"),exportButton=node("button","导出选中分包的离线任务清单"),jobs=node("div");
    user.placeholder="分配给用户 ID";user.setAttribute("aria-label","离线任务用户 ID");
    exportButton.type="button";exportButton.className="fp-btn fp-btn--primary";offline.className='fp-dispatch-panel';jobs.className='fp-dispatch-jobs';
    offline.append(node("legend","离线任务"),node("p","选择用户和输出内容后导出。清单保留选中的分包，客户端可以下载处理，再联网回传。"),user);
    var offlineForm=window.FoxExecutionForm.mount(offline,{prefix:'离线',mode:'both',engine:'llamacpp',precision:'f16',writable:writable});
    var offlineActions=node('div');offlineActions.className='fp-dispatch-actions';offlineActions.append(exportButton);offline.append(offlineActions,jobs);
    user.hidden=true;
    offlineUser=window.FoxUsers.mount(offline,{title:'离线任务用户',single:true,items:window.FoxUsers.catalog(all).items,onChange:function(ids){user.value=ids[0]||'';offlineDirty=true;update();}});
    user.after(offlineUser.element);
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
      var offlineItems=window.FoxUsers.catalog(next).items;
      if(!offlineItems.some(function(item){return item.id===user.value&&!item.disabled;})){user.value='';offlineUser.setValues([]);}
      offlineUser.setItems(offlineItems);assistant.refreshUsers(next);update();
    };
    function save(data){var blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=data.id+".json";a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);}
    async function loadJobs(jobOffset){try{var data=await api("/api/offline-tasks?ws_id="+encodeURIComponent(ws.ws_id)+"&offset="+(jobOffset||0));if(!jobOffset)jobs.replaceChildren();data.items.forEach(function(job){
      var names={ready:"等待离线处理",completed:"已回传完成",failed:"任务已释放或失败",canceled:"已取消"};
      var row=node("div",job.user_id+" · "+job.id+" · "+(names[job.status]||job.status)),download=node("button","重新下载清单"),cancel=node("button","取消分配");download.type=cancel.type="button";
      row.className='fp-dispatch-job';download.className=cancel.className='fp-btn fp-btn--sm';
      download.disabled=job.status==="canceled";
      download.addEventListener("click",async function(){try{save(await api("/api/offline-manifest?id="+encodeURIComponent(job.id)));}catch(e){message.textContent=e.message;}});
      cancel.disabled=job.status==="canceled"||job.status==="completed"||!writable();
      cancel.addEventListener("click",async function(){try{await post("/api/offline-cancel",{id:job.id});await loadJobs();reset();await load();}catch(e){message.textContent=e.message;}});
      row.append(download,cancel);jobs.appendChild(row);
    });if(data.next_offset!==null&&data.next_offset!==undefined){var next=node("button","更多离线任务");next.type="button";next.addEventListener("click",function(){next.remove();loadJobs(data.next_offset);});jobs.append(next);}}catch(e){jobs.textContent="离线任务读取失败："+e.message;}}
    exportButton.addEventListener("click",async function(){
      if(!writable()||busy||box.dataset.busy==='1')return;
      if(!user.value.trim()||!selected.size){message.textContent="请选择用户并勾选分包";return;}
      busy=true;box.dataset.busy='1';exportButton.disabled=true;update();try{
        var result=await post("/api/offline-export",{ws_id:ws.ws_id,user_id:user.value.trim(),server:location.origin,items:Array.from(selected.values()),
          annotation:annotation(),config:offlineForm.value()});
        save(result.manifest);reset();await load();await loadJobs();message.textContent="离线清单已导出，分包已保留给 "+user.value.trim();
      }catch(e){message.textContent="导出失败："+e.message;}finally{busy=false;box.dataset.busy='0';exportButton.disabled=!writable();update();}
    });
    loadJobs();
    update();load();
  }};
})();
