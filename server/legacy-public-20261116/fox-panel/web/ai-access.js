(function(){
  'use strict';
  var $=function(id){return document.getElementById('ai-'+id);},busy=false,writable=false;
  function node(tag,text){var e=document.createElement(tag);e.textContent=text;return e;}
  function date(value){return new Date(value*1000).toLocaleString();}
  function update(){$('create').disabled=busy||!writable;$('refresh').disabled=busy;$('keys').querySelectorAll('button').forEach(function(b){b.disabled=busy||!writable||b.dataset.revoked==='1';});}
  async function request(body){var m=document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/),response=await fetch('/api/ai-access',body?{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':m?decodeURIComponent(m[1]):''},body:JSON.stringify(body)}:{}),data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败');return data;}
  async function refresh(){var data=await request();writable=data.write_enabled;$('keys').replaceChildren();$('audit').replaceChildren();
    data.keys.forEach(function(key){var row=node('tr',''),revoke=node('button','撤销密钥'),state=key.revoked_at?'已撤销':key.expires_at*1000<Date.now()?'已过期':'可用';revoke.type='button';revoke.dataset.revoked=key.revoked_at?'1':'0';revoke.disabled=busy||!writable||!!key.revoked_at;
      revoke.onclick=async function(){if(busy||!writable)return;busy=true;update();revoke.disabled=true;try{await request({action:'revoke',id:key.id});$('message').textContent='密钥已撤销，后续 AI 请求会被拒绝';await refresh();}catch(e){$('message').textContent=e.message;}finally{busy=false;update();}};
      row.append(node('td',key.name),node('td',key.role==='operate'?'查询和操作':'仅查询'),node('td',date(key.expires_at)),node('td',state));var cell=node('td','');cell.append(revoke);row.append(cell);$('keys').append(row);});
    data.audit.forEach(function(item){var row=node('tr','');row.append(node('td',date(item.created_at)),node('td',item.name),node('td',item.tool),node('td',item.status==='ok'?'成功':'失败'));$('audit').append(row);});update();}
  $('url').value=location.origin+'/mcp';
  $('create').onclick=async function(){if(busy||!writable)return;busy=true;update();try{var data=await request({action:'create',name:$('name').value,role:$('role').value,days:Number($('days').value)});$('key').value=data.key;$('new').hidden=false;
    $('example').textContent=JSON.stringify({mcpServers:{lulynx_center:{url:location.origin+'/mcp',headers:{Authorization:'Bearer <这里填新密钥>'}}}},null,2);$('message').textContent='密钥已创建，请保存；之后列表不会再次显示正文';await refresh();
    }catch(e){$('message').textContent=e.message;}finally{busy=false;update();}};
  $('hide').onclick=function(){$('key').value='';$('new').hidden=true;};
  $('copy').onclick=async function(){try{await navigator.clipboard.writeText($('key').value);$('message').textContent='已复制';}catch(e){$('key').select();$('message').textContent='请按 Ctrl+C 复制密钥';}};
  $('refresh').onclick=function(){refresh().catch(function(e){$('message').textContent=e.message;});};
  refresh().catch(function(e){$('message').textContent=e.message;});
})();
