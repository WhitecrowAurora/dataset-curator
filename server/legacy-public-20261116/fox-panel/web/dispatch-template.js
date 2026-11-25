/* 管理员预派单默认模板；只保存工作区默认值，不创建派工或客户端配置。 */
(function () {
  'use strict';
  function node(tag,text){var e=document.createElement(tag);if(text)e.textContent=text;return e;}
  function choice(parent,title,values){var label=node('label'),select=node('select');label.className='fp-dispatch-sort';select.setAttribute('aria-label',title);label.append(node('span',title),select);values.forEach(function(v){var o=node('option',v[1]);o.value=v[0];select.append(o);});parent.append(label);return select;}
  function artistValue(value){return value.artist_inject===true?(value.artist_inject_tags===true?'both':'nl'):value.artist_inject_tags===true?'tag':value.artist_inject===false&&value.artist_inject_tags===false?'off':'inherit';}
  function annotation(value){return value==='inherit'?{}:{artist_inject:value==='nl'||value==='both',artist_inject_tags:value==='tag'||value==='both'};}
  window.FoxDispatchTemplate={artistValue:artistValue,annotation:annotation,mount:function(parent,ws,post,writable,saved,owner){
    owner=owner||parent;
    var box=node('details'),message=node('p'),busy=false;box.className='fp-dispatch-panel fp-assistant';box.append(node('summary','预派单默认模板'));parent.append(box);
    box.append(node('p','未单独设置的预派单默认使用此模板，适用于后续新成员。保存模板不会改动已经派出的任务，客户端默认值保持原样。'));
    var mode=choice(box,'模板打标内容',[['nl','只出 NL'],['both','TAG + NL'],['tag','只出 TAG']]);
    var source=choice(box,'模板执行配置',[['inherit','沿用成员本机配置'],['specified','设置预派并发与高级要求']]);
    var fields=node('div');box.append(fields);
    var form=window.FoxExecutionForm.mount(fields,{prefix:'模板',tag:'pixai',engine:'native',precision:'q6',parallel:true,advancedModels:true,writable:writable});
    var artist=choice(box,'模板画师注入',[['inherit','沿用仓库设置'],['nl','仅 NL 注入'],['tag','仅 TAG 注入'],['both','TAG 与 NL 都注入'],['off','关闭注入']]);
    var actions=node('div'),save=node('button','保存默认模板'),clear=node('button','清除默认模板');actions.className='fp-dispatch-actions';
    [save,clear].forEach(function(b){b.type='button';b.className='fp-btn fp-btn--sm';actions.append(b);});save.classList.add('fp-btn--primary');message.setAttribute('role','status');box.append(actions,message);
    function update(){fields.hidden=source.value==='inherit';var groups=fields.querySelectorAll('.fp-dispatch-form-group');groups[0].hidden=mode.value==='nl';groups[1].hidden=mode.value==='tag';box.querySelectorAll('input,select,button').forEach(function(e){e.disabled=busy||!writable();});}
    function load(){var value=ws.preassignment_template||{};mode.value=value.mode||'nl';source.value=value.config_source||'inherit';artist.value=artistValue(value.annotation||{});form.setValue(value.config||{});update();message.textContent=value.mode?'已配置工作区默认模板':'尚未配置模板，预派单沿用原默认值';}
    box.addEventListener('input',function(){box.dataset.editing='1';});box.addEventListener('change',function(){box.dataset.editing='1';update();});
    async function write(remove){if(busy||owner.dataset.busy==='1'||!writable())return;busy=true;box.dataset.busy=owner.dataset.busy='1';update();message.textContent='正在保存…';
      try{var result=await post('/api/workspace-dispatch',{action:remove?'clear_preassignment_template':'save_preassignment_template',ws_id:ws.ws_id,template:{mode:mode.value,config_source:source.value,config:source.value==='specified'?form.value():{},annotation:annotation(artist.value)}});ws.preassignment_template=result.template;ws.preassignment_last={};box.dataset.editing='0';load();message.textContent=remove?'已清除默认模板和上次派单记忆，后续派单使用原默认值':'默认模板已保存，上次派单记忆已重置；后续派单带入此模板';saved();}
      catch(e){message.textContent='保存失败：'+e.message;}finally{busy=false;box.dataset.busy=owner.dataset.busy='0';update();}
    }
    save.onclick=function(){write(false);};clear.onclick=function(){write(true);};load();return {element:box};
  }};
})();
