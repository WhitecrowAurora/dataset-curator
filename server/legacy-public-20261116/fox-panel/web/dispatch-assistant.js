/* 工作区快速配置：按用户预派，最后一次提交保存；不占用执行租约。 */
(function () {
  'use strict';
  function node(tag,text){var e=document.createElement(tag);if(text)e.textContent=text;return e;}
  function button(text){var e=node('button',text);e.type='button';e.className='fp-btn fp-btn--sm';return e;}
  function choice(parent,title,values){var label=node('label'),select=node('select');label.className='fp-dispatch-sort';select.setAttribute('aria-label',title);label.append(node('span',title),select);values.forEach(function(v){var o=node('option',v[1]);o.value=v[0];select.append(o);});parent.append(label);return select;}
  window.FoxDispatchAssistant={mount:function(parent,ws,all,api,post,writable,refresh,owner){
    owner=owner||parent;
    var open=button('快速配置与预派单'),box=node('section'),title=node('h3','快速配置与预派单');
    box.className='fp-dispatch-panel fp-assistant';box.setAttribute('aria-label','快速配置与预派单');open.classList.add('fp-btn--primary');open.disabled=!writable();open.hidden=true;parent.append(open,box);
    var steps=node('ol'),panels=[],step=0,busy=false,loading=false,offset=0,total=0,loaded=new Map();
    steps.className='fp-assistant-steps';['选择成员','选择分包','核对预派单'].forEach(function(text,index){var li=node('li',text);steps.append(li);var panel=node('div');panel.dataset.step=index;box.append(panel);panels.push(panel);});
    box.prepend(title,node('p','已保存配置自动带入。选成员 → 选包 → 确认；成员上线托管后领取。'),steps);
    function memberItems(data){return (data.users||[]).map(function(u){var workspace=u.effective_workspace_id||u.workspace_id;return {id:u.user_id,label:u.user_id,
      meta:(data.devices||[]).filter(function(d){return d.user_id===u.user_id&&!d.device_id.startsWith('offline-');}).length+' 台登记设备',
      detail:!workspace?'保存时加入当前工作区':workspace===ws.ws_id?'可预派；无需当前在线':'属于其他工作区',
      disabled:u.status!=='active'||!!workspace&&workspace!==ws.ws_id};});}
    var members=window.FoxUsers.mount(panels[0],{title:'预派成员',max:100,items:memberItems(all),disabled:function(){return busy||!writable();},onChange:changed});
    panels[0].append(node('p','支持尚未登记设备的成员；只连接服务器还不会开跑，成员需要开始托管或自动接单。未分配工作区的成员将在保存时加入本工作区。'));
    var configPanel=node('details'),configSummary=node('summary'),configText=node('span');configPanel.className='fp-workspace-group fp-assistant-config';configSummary.append(node('strong','本次打标配置'),configText);configPanel.append(configSummary);panels[0].append(configPanel);
    var configSource=choice(configPanel,'执行配置',[['inherit','沿用成员本机配置（推荐）'],['specified','设置预派并发与高级要求']]);
    var mode=choice(configPanel,'预派打标内容',[['nl','只出 NL'],['both','TAG + NL'],['tag','只出 TAG']]);
    var fields=node('div');configPanel.append(fields);
    var form=window.FoxExecutionForm.mount(fields,{prefix:'预派',tag:'pixai',engine:'native',precision:'q6',parallel:true,advancedModels:true,writable:writable});
    var artist=choice(configPanel,'预派画师注入',[['inherit','沿用仓库设置'],['nl','仅 NL 注入'],['tag','仅 TAG 注入'],['both','TAG 与 NL 都注入'],['off','关闭注入']]);
    var configNote=node('p'),useTemplate=button('使用工作区默认模板');configPanel.append(configNote,useTemplate);
    configPanel.append(node('p','预派并发优先于远程调整，成员须勾选允许服务器调整。0 不限制并发；旧指定模型要求保留在高级设置。成员暂停时保持暂停。'));
    function updateFields(){fields.hidden=configSource.value==='inherit';var groups=fields.querySelectorAll('.fp-dispatch-form-group');groups[0].hidden=mode.value==='nl';groups[1].hidden=mode.value==='tag';configText.textContent=mode.selectedOptions[0].textContent+' · '+configurationText()+' · 画师：'+artist.selectedOptions[0].textContent;}
    configSource.onchange=mode.onchange=function(){updateFields();changed();};
    function applyTemplate(templateOnly){var last=ws.preassignment_last||{},value=!templateOnly&&last.mode?last:ws.preassignment_template||{};mode.value=value.mode||'nl';configSource.value=value.config_source||'inherit';artist.value=window.FoxDispatchTemplate.artistValue(value.annotation||{});form.setValue(value.config||{});configNote.textContent=value===last?'来源：上次成功派单；优先于工作区模板。本次可单独修改。':value.mode?'来源：工作区默认模板；派单成功后记住本次配置。':'来源：成员本机配置；派单成功后记住本次配置。';updateFields();}
    useTemplate.onclick=function(){applyTemplate(true);changed();};
    applyTemplate();
    var packages=window.FoxSelection.mount(panels[1],{title:'预派分包',max:500,disabled:function(){return busy||loading||!writable();},onChange:changed});
    var more=button('加载更多分包'),packageMessage=node('p');panels[1].append(more,packageMessage);
    var review=node('div');review.className='fp-assistant-review';panels[2].append(review,node('p','多位成员按已分配数量均衡安排。已分配、正在执行或已完成的分项不会覆盖。'));
    var actions=node('div'),previous=button('上一步'),next=button('下一步'),submit=button('保存预派单'),close=button('关闭助手'),message=node('p');
    submit.classList.add('fp-btn--primary');actions.className='fp-dispatch-actions';actions.append(previous,next,submit,close);message.setAttribute('role','status');box.append(actions,message);
    function changed(){box.dataset.editing='1';if(previous)render();}
    function selected(){return packages.values().map(function(key){return loaded.get(key);}).filter(Boolean);}
    function configurationText(){if(configSource.value==='inherit')return '沿用成员本机配置';var v=form.value(),text=[];if(mode.value!=='nl')text.push('TAG：'+(v.tag_backend||'本机模型')+'，批大小 '+(v.tag_batch||'本机值'));if(mode.value!=='tag')text.push('NL：'+(v.nl_engine||'本机引擎')+' / '+(v.nl_model||'本机模型')+' / '+(v.nl_precision||'本机精度').toUpperCase()+'，并发 '+(v.concurrency||'本机值'));return text.join('；');}
    function render(){panels.forEach(function(panel,index){panel.hidden=index!==step;steps.children[index].setAttribute('aria-current',index===step?'step':'false');});previous.hidden=step===0;next.hidden=step===2;submit.hidden=step!==2;
      previous.disabled=close.disabled=busy;next.disabled=busy||(step===1&&loading);submit.disabled=useTemplate.disabled=busy||!writable();members.refresh();packages.refresh();
      configPanel.querySelectorAll('input,select').forEach(function(input){input.disabled=busy||!writable();});updateFields();
      if(step===2){review.replaceChildren();[['工作区',ws.name||ws.ws_id],['成员',members.values().join('、')],['打标内容',mode.selectedOptions[0].textContent],['执行配置',configurationText()],['画师注入',artist.selectedOptions[0].textContent],['分包数量',selected().length],['上线后','上线托管后自动领取；主动暂停时保持暂停']].forEach(function(row){var p=node('p');p.append(node('strong',row[0]+'：'),node('span',String(row[1])));review.append(p);});}}
    function available(p){var wanted=(p.missing_parts||[]).filter(function(part){return mode.value==='both'||part===mode.value;}),free=p.unassigned_parts||p.claimable_parts||[];return wanted.length>0&&wanted.every(function(part){return free.includes(part);});}
    function updatePackages(){packages.setItems(Array.from(loaded,function(entry){var p=entry[1];return {id:entry[0],label:p.id,detail:p.repo+' · '+((p.missing_parts||[]).join(' / ').toUpperCase()||'已完成'),disabled:!available(p)};}),total);}
    async function load(){if(loading)return;loading=true;render();more.disabled=true;try{var data=await api('/api/workspace-packages?ws_id='+encodeURIComponent(ws.ws_id)+'&offset='+offset);data.items.forEach(function(p){loaded.set(p.repo+'/'+p.id,p);});total=data.total;offset=data.next_offset;more.hidden=offset===null;packageMessage.textContent='共 '+total+' 个分包；可选当前输出内容尚未完成的分项。';updatePackages();}catch(e){packageMessage.textContent='读取失败：'+e.message;}finally{loading=false;more.disabled=false;render();}}
    open.onclick=function(){if(box.dataset.editing!=='1')applyTemplate();box.hidden=false;open.hidden=true;render();if(!loaded.size)load();};close.onclick=function(){box.hidden=true;open.hidden=false;};more.onclick=load;
    previous.onclick=function(){step--;render();};next.onclick=function(){message.textContent='';if(step===0&&!members.values().length){message.textContent='请至少选择一位成员';return;}if(step===1&&!selected().length){message.textContent='请至少选择一个分包';return;}if(step===0)updatePackages();step++;render();if(step===1&&!loaded.size)load();};
    submit.onclick=async function(){if(busy||owner.dataset.busy==='1'||!writable())return;var value=artist.value,annotation=value==='inherit'?{}:{artist_inject:value==='nl'||value==='both',artist_inject_tags:value==='tag'||value==='both'};
      var body={ws_id:ws.ws_id,users:members.values(),items:selected().map(function(p){return {repo:p.repo,id:p.id};}),mode:mode.value,start:true,annotation:annotation,
        config:configSource.value==='inherit'?{}:form.value(),config_source:configSource.value,assign_workspace:true,grant_repos:ws.allocation_mode==='manual'};
      busy=true;box.dataset.busy=owner.dataset.busy='1';render();message.textContent='正在保存预派单…';
      try{var result=await post('/api/workspace-dispatch',body);ws.preassignment_last=result.preassignment_last;box.dataset.editing='0';message.textContent='已保存 '+result.assigned.length+' 个分项，等待成员上线托管后领取；已记住本次配置。';packages.setValues([]);await refresh();}
      catch(e){message.textContent='保存失败：'+e.message;}finally{busy=false;box.dataset.busy=owner.dataset.busy='0';render();}
    };
    box.addEventListener('change',function(){box.dataset.editing='1';});render();
    return {element:box,refreshUsers:function(data){members.setItems(memberItems(data));},syncDefaults:function(){if(box.dataset.editing!=='1')applyTemplate();}};
  }};
})();
