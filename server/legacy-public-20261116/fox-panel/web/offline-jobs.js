/* 共享已有离线工单操作，删除只在第二次点击后提交。 */
(function(){
  'use strict';
  window.FoxOfflineJobs={mount:function(row,job,request,writable,refresh,message,download){
    var canceled=job.status==='canceled',busy=false,armed=false;
    var get=document.createElement('button'),cancel=document.createElement('button'),remove=document.createElement('button'),note=document.createElement('span');
    get.textContent='下载工单';cancel.textContent='取消分配';remove.textContent='删除工单';
    [get,cancel,remove].forEach(function(b){b.type='button';b.className='fp-btn fp-btn--sm';});remove.classList.add('fp-btn--danger');
    row.dataset.offlineJob='1';note.setAttribute('role','status');row.append(get,cancel,remove,note);
    function update(){get.disabled=busy||canceled;cancel.disabled=busy||!writable()||canceled||job.status==='completed';remove.disabled=busy||!writable();}
    row.addEventListener('offline-jobs-update',update);
    async function write(path){busy=true;update();try{await request(path,{id:job.id});message.textContent=path==='/api/offline-delete'?'工单已删除；未完成占用已撤销，已回传结果保留':'已取消离线分配';await refresh();}catch(e){message.textContent=e.message;}finally{busy=false;update();}}
    get.onclick=async function(){if(busy)return;try{download(await request('/api/offline-manifest?id='+encodeURIComponent(job.id)));}catch(e){message.textContent=e.message;}};
    cancel.onclick=function(){if(!busy&&writable())void write('/api/offline-cancel');};
    remove.onclick=function(){if(busy||!writable())return;if(!armed){armed=true;remove.textContent='再次点击确认删除';note.textContent='将撤销未完成占用。请先告知成员停止离线任务，已回传结果保留。';return;}void write('/api/offline-delete');};
    update();
  }};
})();
