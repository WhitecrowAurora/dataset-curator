(function(){
  'use strict';
  function node(tag,text){var n=document.createElement(tag);if(text)n.textContent=text;return n;}
  function link(id,token){return location.origin+(token?'/share/results/':'/api/result-exports/')+id+(token?'?token='+encodeURIComponent(token):'');}
  window.FoxResultExports={mount:function(parent,selected,post){
    var box=node('fieldset'),message=node('p'),list=node('div'),create=node('button','导出选中结果／创建分享'),days=node('input');
    box.className='fp-result-exports';message.className='fp-result-export-message';list.className='fp-result-export-list';
    box.append(node('legend','已回收结果下载与分享'),node('p','选择已有回传的分片。导出保留所有冲突候选，不会替管理员采用正文。'),days,create,message,list);parent.append(box);
    days.type='number';days.min='1';days.max='30';days.value='7';days.setAttribute('aria-label','分享有效天数');
    create.type='button';create.className='fp-btn fp-btn--sm';var busy=false;
    async function load(){if(!box.isConnected)return;try{var r=await fetch('/api/result-exports'),d=await r.json();if(!r.ok)throw Error(d.error||'读取导出记录失败');list.replaceChildren();
      (d.items||[]).forEach(function(item){var row=node('div'),labels={queued:'排队中',building:'生成中',ready:'可下载',failed:'失败'};row.className='fp-result-export-row';row.append(node('span',new Date(item.created*1000).toLocaleString()+' · '+item.packages+' 个分片 · '+(labels[item.state]||item.state)+' '+item.progress+'/'+item.packages+' '));
        if(item.state==='ready'){var a=node('a','下载结果');a.href=link(item.id);a.className='fp-btn fp-btn--sm';row.append(a);}
        if(item.error)row.append(node('span',item.error));
        if(!item.revoked){var revoke=node('button','撤销分享');revoke.type='button';revoke.className='fp-btn fp-btn--sm';revoke.onclick=async function(){revoke.disabled=true;try{await post('/api/source-catalog',{action:'revoke_export',repo:'result-export',id:item.id});await load();}catch(e){message.textContent=e.message;revoke.disabled=false;}};row.append(revoke);}else row.append(node('span','分享已撤销'));
        list.append(row);
      });}catch(e){message.textContent=e.message;}}
    create.onclick=async function(){if(busy||!selected.size)return;busy=true;create.disabled=true;message.textContent='正在创建…';
      try{var groups={};selected.forEach(function(p){(groups[p.repo]||(groups[p.repo]=[])).push(p.id);});message.replaceChildren();
        for(var repo of Object.keys(groups)){var result=await post('/api/source-catalog',{action:'export_results',repo:repo,packages:groups[repo],days:Number(days.value)}),input=node('input');input.readOnly=true;input.value=link(result.id,result.token);input.setAttribute('aria-label','外部结果链接');input.style.width='100%';message.append(node('span',repo+'：链接已创建，文件准备好后可导入。有效期 '+days.value+' 天。'),input);}
        await load();}catch(e){message.textContent=e.message;}finally{busy=false;create.disabled=false;}};
    load();var timer=setInterval(function(){if(!box.isConnected){clearInterval(timer);return;}load();},5000);
  },package:function(parent,p){var selected=new Map([[p.id,p]]);this.mount(parent,selected,async function(url,body){var m=document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':m?decodeURIComponent(m[1]):''},body:JSON.stringify(body)}),d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||'导出失败');return d;});}
  };
})();
