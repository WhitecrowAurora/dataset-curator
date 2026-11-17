/* 工作区内的仓库设备分配；两种视图共用设备集合。 */
(function () {
  'use strict';
  function node(tag, text) { var e=document.createElement(tag); if(text)e.textContent=text; return e; }
  function field(parent,text,input) { var label=node('label',text); label.className='fp-field'; label.append(input); parent.append(label); return input; }
  function choices(select,items) { items.forEach(function(item){var option=node('option',item[1]);option.value=item[0];select.append(option);}); }
  function button(text,label) { var b=node('button',text);b.type='button';b.className='fp-btn fp-btn--sm';if(label)b.setAttribute('aria-label',label);return b; }
  window.FoxAllocation={mount:function(parent,ws,all,writable,refresh){
    var box=node('fieldset'),toolbar=node('div'),rows=node('div'),message=node('p'),save=button('保存分配'),selections={},views=[],busy=false;
    box.className='fp-allocation';toolbar.className='fp-allocation-toolbar';message.setAttribute('role','status');
    box.append(node('legend','仓库设备分配'),toolbar,rows,message,save);parent.append(box);
    var mode=field(toolbar,'分配模式',node('select')),view=field(toolbar,'配置视图',node('select'));
    choices(mode,[['auto','自动'],['manual','手动']]);choices(view,[['simple','简化'],['detail','详细']]);
    mode.value=ws.allocation_mode||'auto';view.value='simple';
    var directory=window.FoxUsers.catalog(all,ws.ws_id),devices=directory.devices,groups=directory.groups;
    var valid=new Set(devices.map(function(d){return d.device_id;}));
    function locked(){return busy||!writable();}
    function changed(){box.dataset.editing='1';}
    (ws.repos||[]).forEach(function(repo){
      var selected=new Set((ws.repo_devices&&ws.repo_devices[repo]||[]).filter(function(id){return valid.has(id);}));selections[repo]=selected;
      var row=node('fieldset'),simple=node('div'),detail=node('div');
      row.className='fp-allocation-repo';
      row.append(node('legend',repo),node('p','处理设备'),simple,detail);rows.append(row);
      var userList=window.FoxUsers.mount(simple,{title:'用户',items:directory.items,disabled:locked,
        onChange:function(users){groups.forEach(function(ids,user){var count=ids.filter(function(id){return selected.has(id);}).length;
          if(!users.includes(user))ids.forEach(function(id){selected.delete(id);});
          else if(!count)ids.forEach(function(id){selected.add(id);});
        });changed();render();}});
      var deviceList=window.FoxSelection.mount(detail,{title:'设备',items:devices.map(function(d){return {id:d.device_id,label:d.user_id+' · '+d.device_id};}),disabled:locked,
        onChange:function(ids){selected.clear();ids.forEach(function(id){selected.add(id);});changed();render();}});
      function render(){
        userList.setValues(Array.from(groups.keys()).filter(function(user){return groups.get(user).some(function(id){return selected.has(id);});}));userList.refresh();
        deviceList.setValues(Array.from(selected));deviceList.refresh();
      }
      views.push({simple:simple,detail:detail,render:render});render();
    });
    function show(){rows.hidden=mode.value!=='manual';view.disabled=rows.hidden||busy;mode.disabled=locked();save.disabled=locked();views.forEach(function(v){v.simple.hidden=view.value!=='simple';v.detail.hidden=view.value!=='detail';v.render();});}
    mode.onchange=function(){changed();show();};view.onchange=show;show();
    save.onclick=async function(){
      if(locked())return;
      var mapping={};Object.keys(selections).forEach(function(repo){mapping[repo]=Array.from(selections[repo]);});
      var body={action:'allocation',ws_id:ws.ws_id,mode:mode.value,repo_devices:mapping};busy=true;box.dataset.busy='1';show();
      try{
        var csrf=document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);
        var r=await fetch('/api/scheduling',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf?decodeURIComponent(csrf[1]):''},body:JSON.stringify(body)}),data=await r.json();
        if(!r.ok||data.ok===false)throw Error(data.error||'保存失败');box.dataset.editing='0';message.textContent='已保存'+(data.released_prelocks?' · 释放预约 '+data.released_prelocks:'')+(data.cleared_assignments?' · 清理旧指定 '+data.cleared_assignments:'');await refresh();
      }catch(e){message.textContent=e.message;}finally{busy=false;box.dataset.busy='0';show();}
    };
  }};
})();
