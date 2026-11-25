(function(){
  'use strict';
  function node(tag,text){var e=document.createElement(tag);if(text)e.textContent=text;return e;}
  window.FoxDispatchPlans={mount:function(parent,ws,writable,post){
    var box=node('fieldset'),sort=node('select'),message=node('p'),button=node('button','导出已分配派工'),links=node('div');
    box.className='fp-dispatch-panel';box.append(node('legend','分派表导出与客户端应用'),node('p','导出工作区全部已分配的待处理分项。表格顶部附仓库设置；按用户 ZIP 内附可导入任务清单。导出不开始执行、不领取租约。'));
    [['user','按用户排序'],['package','按包名排序']].forEach(function(v){var o=node('option',v[1]);o.value=v[0];sort.append(o);});sort.setAttribute('aria-label','分派表排序');
    var defaults=node('details');defaults.append(node('summary','未托管设备的默认执行配置'));
    var form=window.FoxExecutionForm.mount(defaults,{prefix:'分派默认',tag:'pixai',engine:'native',precision:'q6',writable:writable});
    defaults.append(node('p','有托管配置的设备使用其已登记配置；其他设备使用这里的默认配置。设备路径与凭据仍由本机提供。'));
    var sortField=node('label');sortField.className='fp-dispatch-sort';sortField.append(node('span','分派表排序'),sort);
    var actions=node('div');actions.className=links.className='fp-dispatch-actions';button.type='button';button.className='fp-btn fp-btn--primary';actions.append(button);box.append(sortField,defaults,actions,message,links);parent.append(box);
    button.disabled=!writable();var busy=false;
    button.onclick=async function(){if(busy||!writable())return;busy=true;box.dataset.busy='1';button.disabled=true;message.textContent='正在生成分派快照…';
      try{var data=await post('/api/scheduling',{action:'export_plan',ws_id:ws.ws_id,server:location.origin,sort:sort.value,config:form.value()});
        links.replaceChildren();[['html','易读总表 HTML'],['json','可导入总清单 JSON'],['zip','按用户分表 ZIP']].forEach(function(v){var a=node('a',v[1]);a.href='/api/dispatch-plan?id='+encodeURIComponent(data.manifest.id)+'&format='+v[0];a.className='fp-btn fp-btn--sm';links.append(a);});
        message.textContent='已导出 '+data.manifest.tasks.length+' 个分项；本机用户可在调度中心查看分派通知，确认后应用。';
      }catch(e){message.textContent=e.message;}finally{busy=false;box.dataset.busy='0';button.disabled=!writable();}
    };
  }};
})();
