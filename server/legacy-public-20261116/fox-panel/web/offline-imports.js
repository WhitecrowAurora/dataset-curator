/* 离线回收：上传预览与明确确认分开，沿用工作区权限。 */
(function(){
  'use strict';
  function node(tag,text){var e=document.createElement(tag);if(text)e.textContent=text;return e;}
  window.FoxOfflineImports={mount:function(parent,ws,post,writable,refresh){
    var box=node('fieldset'),file=node('input'),upload=node('button','上传并核对'),message=node('p'),list=node('div'),
        confirm=node('button','确认回收所选分片'),cancel=node('button','取消本次导入'),recheck=node('button','重新核对'),agree=node('input'),selected=new Set(),data=null,busy=false,storageKey='lulynx-offline-import:'+ws.ws_id;
    box.className='fp-dispatch-panel';file.type='file';file.accept='.zip';file.setAttribute('aria-label','离线网页回收包');
    upload.type=confirm.type=cancel.type=recheck.type='button';upload.className='fp-btn';confirm.className='fp-btn fp-btn--primary';cancel.className=recheck.className='fp-btn';
    agree.type='checkbox';var consent=node('label','已核对成员、分片与完整性，确认按原分配回收 ');consent.prepend(agree);
    var actions=node('div');actions.className='fp-dispatch-actions';actions.append(confirm,recheck,cancel);
    box.append(node('legend','导入离线结果并确认回收'),node('p','成员在客户端“离线已分配任务”中导出网页回收包，再交给管理员上传。上传只核对；确认后登记。单次最多 64 MiB，不含原始图片。'),file,upload,message,list,consent,actions);
    parent.append(box);
    function update(){file.disabled=busy||!writable();upload.disabled=busy||!writable()||!file.files.length;
      confirm.disabled=busy||!writable()||!data||!selected.size||!agree.checked;cancel.disabled=recheck.disabled=busy||!data;
      box.dataset.busy=busy?'1':'0';box.dataset.editing=data||file.files.length?'1':'0';}
    function remember(token){try{if(token)localStorage.setItem(storageKey,token);else localStorage.removeItem(storageKey);}catch(e){}}
    function display(next){data=next;remember(data.token);selected.clear();agree.checked=false;list.replaceChildren();
      message.textContent='原分配成员：'+data.user_id+' · 任务 '+data.job_id+' · '+data.items.length+' 个分片';
      var all=node('input');all.type='checkbox';var allLabel=node('label',' 全选可回收分片');allLabel.prepend(all);list.append(allLabel);
      var checks=[];data.items.forEach(function(item){var row=node('div'),check=node('input');check.type='checkbox';check.disabled=!item.eligible;
        check.setAttribute('aria-label','回收 '+item.package);var label=node('label',item.repo+' / '+item.package+' · '+item.parts.join(' + ').toUpperCase()+' · '+item.message);label.prepend(check);row.append(label);
        if(item.report){var report=item.report,details=node('details');details.append(node('summary','逐图检查：'+report.expected+' 张'));
          item.parts.forEach(function(part){var n=report.parts[part];details.append(node('p',part.toUpperCase()+'：成功 '+n.success+'，排除 '+n.excluded+'，缺项 '+n.missing+'，失败 '+n.failed));});
          (report.conflicts||[]).slice(0,10).forEach(function(text){details.append(node('p',text));});
          if(report.source_verification&&['pending','failed'].includes(report.source_verification.state))details.append(node('p','源图片总表尚未核验通过；确认后进入待核验，不会直接标成完成。'));
          row.append(details);}
        check.onchange=function(){if(check.checked)selected.add(item.lease_id);else selected.delete(item.lease_id);all.checked=checks.length>0&&checks.every(function(c){return c.checked;});update();};
        if(item.eligible){check.dataset.lease=item.lease_id;checks.push(check);}list.append(row);});
      all.disabled=!checks.length;all.onchange=function(){checks.forEach(function(check){check.checked=all.checked;if(all.checked)selected.add(check.dataset.lease);else selected.delete(check.dataset.lease);});update();};update();}
    file.onchange=function(){agree.checked=false;update();};agree.onchange=update;
    upload.onclick=async function(){if(busy||!writable()||!file.files.length)return;var chosen=file.files[0];
      if(chosen.size>64*1024*1024){message.textContent='回收包超过 64 MiB，请在客户端分批导出';return;}
      busy=true;update();message.textContent='正在上传并核对，请稍候…';try{
        if(data)await post('/api/offline-import',{action:'cancel',token:data.token});data=null;remember('');list.replaceChildren();
        var match=document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);
        var response=await fetch('/api/offline-import-upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':match?decodeURIComponent(match[1]):'','X-Workspace-Id':ws.ws_id},body:chosen});
        var result=await response.json();if(!response.ok||result.ok===false)throw new Error(result.error||'上传失败');display(result);
      }catch(e){message.textContent=e.message;}finally{busy=false;update();}};
    confirm.onclick=async function(){if(busy||!writable()||!data||!selected.size||!agree.checked)return;busy=true;update();
      try{var result=await post('/api/offline-import',{action:'confirm',token:data.token,revision:data.revision,lease_ids:Array.from(selected)});display(result);
        message.textContent=result.stale?result.error:'已接收 '+result.confirmed.length+' 个分片；最终完成以逐图和源清单核验为准';await refresh();
      }catch(e){message.textContent=e.message;}finally{busy=false;update();}};
    recheck.onclick=async function(){if(busy||!data)return;busy=true;update();try{display(await post('/api/offline-import',{action:'preview',token:data.token}));}catch(e){message.textContent=e.message;}finally{busy=false;update();}};
    cancel.onclick=async function(){if(busy||!data)return;busy=true;update();try{await post('/api/offline-import',{action:'cancel',token:data.token});data=null;remember('');selected.clear();list.replaceChildren();file.value='';agree.checked=false;message.textContent='已取消导入，原分配和本地产物保留';}catch(e){message.textContent=e.message;}finally{busy=false;update();}};
    update();
    var previous='';try{previous=localStorage.getItem(storageKey)||'';}catch(e){}
    if(previous){busy=true;update();post('/api/offline-import',{action:'preview',token:previous}).then(display).catch(function(e){remember('');message.textContent=e.message;}).finally(function(){busy=false;update();});}
  }};
})();
