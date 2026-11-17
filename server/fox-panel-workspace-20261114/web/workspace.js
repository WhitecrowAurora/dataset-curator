/* 工作区分包列表与设备队列，复用登录会话和 CSRF。 */
(function(){
  "use strict";
  function node(tag,text){var n=document.createElement(tag);if(text)n.textContent=text;return n;}
  function csrf(){var m=document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);return m?decodeURIComponent(m[1]):"";}
  async function post(path,body){
    var r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf()},body:JSON.stringify(body)});
    var data=await r.json();if(!r.ok||data.ok===false)throw new Error(data.error||"请求失败");return data;
  }
  window.FoxWorkspace={render:function(card,ws,api,writable,all){
    var previous=card.querySelector("[data-workspace-packages]");
    var signature=JSON.stringify([ws,(all.devices||[]).map(function(d){return [d.device_id,d.workspace_id,d.status];}),(all.users||[]).map(function(u){return [u.user_id,u.workspace_id];}),writable()]);
    if(previous&&previous.dataset.signature===signature)return;
    if(previous)previous.remove();
    var box=node("section"),message=node("p"),list=node("div"),devices=node("div"),more=node("button","加载更多"),send=node("button","下发选中分包"),offline=node("fieldset");
    box.dataset.workspacePackages="";box.dataset.ws=ws.ws_id;box.className="fp-card";
    box.dataset.signature=signature;
    more.type=send.type="button";more.className=send.className="fp-btn fp-btn--sm";
    var selected=new Map(),chosen=new Set(),offset=0,busy=false;
    box.append(node("h3","工作区待处理分包"),node("p",ws.allow_manual_claim?"允许客户端手动领取":"客户端只处理管理员分配的分包"),message,list,more,devices,send,offline);
    card.appendChild(box);
    function update(){devices.hidden=!selected.size;send.disabled=busy||!selected.size||!chosen.size||!writable();}
    function check(parent,text,onChange){var label=node("label"),input=node("input");input.type="checkbox";label.className="fp-check";label.append(input,node("span",text));input.addEventListener("change",function(){onChange(input.checked);update();});parent.appendChild(label);return input;}
    (all.devices||[]).filter(function(d){return d.workspace_id===ws.ws_id||(!d.workspace_id&&(all.users||[]).some(function(u){return u.user_id===d.user_id&&u.workspace_id===ws.ws_id;}));}).forEach(function(d){
      var input=check(devices,d.user_id+" · "+d.device_id+(d.status==="blocked"||d.status==="archived"?" · 已停用":""),function(on){if(on)chosen.add(d.device_id);else chosen.delete(d.device_id);});
      input.disabled=d.status==="blocked"||d.status==="archived"||d.device_id.indexOf("offline-")===0;
    });
    var start=check(devices,"同时启动在线的被动托管设备",function(){});start.checked=true;
    async function load(){more.disabled=true;try{
      var page=await api("/api/workspace-packages?ws_id="+encodeURIComponent(ws.ws_id)+"&offset="+offset);
      message.textContent=page.total?"待处理 "+page.total+" 个分包":page.empty_reason||"暂无待处理分包";
      page.items.forEach(function(p){var key=p.repo+"/"+p.id,input=check(list,key+" · 缺 "+p.missing_parts.join(" / ")+(p.assigned_device?" · 已分配："+p.assigned_device:p.working_device?" · 处理中":""),function(on){if(on)selected.set(key,{repo:p.repo,id:p.id});else selected.delete(key);});input.disabled=!p.claimable;});
      more.hidden=page.next_offset===null;offset=page.next_offset||0;
    }catch(e){message.textContent="读取失败："+e.message+"，可重试";}finally{more.disabled=false;update();}}
    more.addEventListener("click",load);send.addEventListener("click",async function(){busy=true;update();try{
      var result=await post("/api/workspace-dispatch",{ws_id:ws.ws_id,items:Array.from(selected.values()),devices:Array.from(chosen),start:start.checked});
      selected.clear();list.replaceChildren();offset=0;await load();
      message.textContent="已分配 "+result.assigned.length+" 个分包"+(result.starts||[]).map(function(s){return "；"+s.device_id+"："+(s.status==="queued"?"开始指令已下发":s.status==="running"?"正在执行":s.message);}).join("");
    }catch(e){message.textContent="下发失败："+e.message;}finally{busy=false;update();}});
    var user=node("input"),mode=node("select"),engine=node("select"),model=node("input"),precision=node("select"),tag=node("select"),exportButton=node("button","导出选中分包的离线任务清单"),jobs=node("div");
    tag.setAttribute("aria-label","离线 TAG 模型");["wd-eva02","pixai","cl-tagger-v2","onnx"].forEach(function(v){var o=node("option",v);o.value=v;tag.append(o);});
    user.placeholder="分配给用户 ID";model.placeholder="NL 模型名（留空自动选择）";
    user.setAttribute("aria-label","离线任务用户 ID");model.setAttribute("aria-label","离线 NL 模型");
    mode.setAttribute("aria-label","离线打标模式");engine.setAttribute("aria-label","离线 NL 引擎");precision.setAttribute("aria-label","离线 NL 精度");
    [["both","TAG + NL"],["tag","只出 TAG"],["nl","只出 NL"]].forEach(function(v){var o=node("option",v[1]);o.value=v[0];mode.appendChild(o);});
    [["llamacpp","llama.cpp"],["vllm","vLLM"]].forEach(function(v){var o=node("option",v[1]);o.value=v[0];engine.appendChild(o);});
    function precisions(){precision.replaceChildren();(engine.value==="llamacpp"?["f16","q4","q6","q8"]:["bf16","fp8","w4a16"]).forEach(function(v){var o=node("option",v);o.value=v;precision.appendChild(o);});}
    engine.addEventListener("change",precisions);precisions();exportButton.type="button";exportButton.className="fp-btn fp-btn--sm";
    offline.append(node("legend","离线任务"),node("p","导出小配置清单。客户端从对应 HF 仓库下载分片，不需要连接调度中心；可在本机调整设备与模型配置。"),user,mode,tag,engine,model,precision,exportButton,jobs);
    function save(data){var blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=data.id+".json";a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);}
    async function loadJobs(jobOffset){try{var data=await api("/api/offline-tasks?ws_id="+encodeURIComponent(ws.ws_id)+"&offset="+(jobOffset||0));if(!jobOffset)jobs.replaceChildren();data.items.forEach(function(job){
      var names={ready:"等待离线处理",completed:"已回传完成",failed:"任务已释放或失败",canceled:"已取消"};
      var row=node("div",job.user_id+" · "+job.id+" · "+(names[job.status]||job.status)),download=node("button","重新下载清单"),cancel=node("button","取消分配");download.type=cancel.type="button";
      download.disabled=job.status==="canceled";
      download.addEventListener("click",async function(){try{save(await api("/api/offline-manifest?id="+encodeURIComponent(job.id)));}catch(e){message.textContent=e.message;}});
      cancel.disabled=job.status==="canceled"||job.status==="completed"||!writable();
      cancel.addEventListener("click",async function(){try{await post("/api/offline-cancel",{id:job.id});await loadJobs();list.replaceChildren();offset=0;await load();}catch(e){message.textContent=e.message;}});
      row.append(download,cancel);jobs.appendChild(row);
    });if(data.next_offset!==null&&data.next_offset!==undefined){var next=node("button","更多离线任务");next.type="button";next.addEventListener("click",function(){next.remove();loadJobs(data.next_offset);});jobs.append(next);}}catch(e){jobs.textContent="离线任务读取失败："+e.message;}}
    exportButton.addEventListener("click",async function(){
      if(!writable()||busy)return;
      if(!user.value.trim()||!selected.size){message.textContent="请填写用户 ID 并勾选分包";return;}
      busy=true;exportButton.disabled=true;update();try{
        var result=await post("/api/offline-export",{ws_id:ws.ws_id,user_id:user.value.trim(),server:location.origin,items:Array.from(selected.values()),
          config:{mode:mode.value,tag_backend:tag.value,nl_engine:engine.value,nl_model:model.value.trim(),nl_precision:precision.value}});
        save(result.manifest);selected.clear();list.replaceChildren();offset=0;await load();await loadJobs();message.textContent="离线清单已导出，分包已保留给 "+user.value.trim();
      }catch(e){message.textContent="导出失败："+e.message;}finally{busy=false;exportButton.disabled=false;update();}
    });
    loadJobs();
    update();load();
  }};
})();
