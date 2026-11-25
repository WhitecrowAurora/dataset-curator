(function () {
  'use strict';
  function node(tag, text) { var e = document.createElement(tag); if (text) e.textContent = text; return e; }
  function field(parent, text, input) { var label = node('label', text + ' '); label.append(input); parent.append(label); return input; }
  window.FoxArtist = {
    mount: function (parent, repos, writable, drafts) {
      var box = node('fieldset'); box.className = 'fp-artist-settings'; box.append(node('legend', '画师表与任务注入')); parent.append(box);
      var repo = field(box, '仓库', node('select'));
      repo.setAttribute('aria-label', '仓库');
      repos.forEach(function (r) { var o = node('option', r); o.value = r; repo.append(o); });
      var enabled = field(box, '启用画师表', node('input')); enabled.type = 'checkbox';
      var directory = field(box, '仓库内画师表目录', node('input')); directory.value = 'artist';
      var revision = field(box, '固定版本（留空获取当前版本）', node('input'));
      var tag = field(box, 'TAG 开头注入 @画师', node('input')); tag.type = 'checkbox';
      var nl = field(box, 'NL 开头注入画师风格句', node('input')); nl.type = 'checkbox';
      box.append(node('p', 'TAG：@画师, 标签；英文 NL：In the style of @画师, 描述；中文 NL：使用 @画师 的风格，描述。'));
      box.append(node('p', '保存时验证表文件，中心缓存并随任务配发。已有任务按领取时的配置执行；旧结果用“补注入选中”处理。'));
      var message = node('p'); message.setAttribute('role', 'status'); box.append(message);
      var detected = node('div'); detected.setAttribute('aria-live', 'polite'); box.append(detected);
      var busy = false, rows = [], dirty = false, current = repo.value, generation = 0;
      drafts = drafts || new Map();
      function config() { return {enabled:enabled.checked,directory:directory.value.trim(),revision:revision.value.trim(),artist_inject_tags:tag.checked,artist_inject:nl.checked}; }
      function mark() { dirty = true; drafts.set(repo.value, config()); box.dataset.editing = '1'; }
      function controls() { box.querySelectorAll('input,select,button').forEach(function (e) { e.disabled = busy || !writable() || !repo.value || e.dataset.needsChoice === '1' && !detected.querySelector('select').value; }); }
      function show() { var row = rows.find(function (r) { return r.repo === repo.value; }); if (!row) return;
        var cfg = drafts.get(repo.value) || row.artist || {}; enabled.checked = Boolean(cfg.enabled); directory.value = cfg.directory || 'artist'; revision.value = cfg.revision || ''; tag.checked = cfg.artist_inject_tags !== false; nl.checked = cfg.artist_inject !== false;
        dirty = drafts.has(repo.value); box.dataset.editing = drafts.size ? '1' : '0';
      }
      box.addEventListener('input', function (event) { if (event.target !== repo && !detected.contains(event.target)) mark(); });
      box.addEventListener('change', function (event) { if (event.target !== repo && !detected.contains(event.target)) mark(); });
      repo.onchange = function () { current = repo.value; ++generation; detected.replaceChildren(); message.textContent = ''; dirty = drafts.has(current); show(); controls(); };
      async function request(body) {
        var m = document.cookie.match(/(?:^|;\s*)fp_csrf=([^;]+)/);
        var response = await fetch('/api/source-catalog', body ? {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':m?decodeURIComponent(m[1]):''},body:JSON.stringify(body)} : {});
        var data = await response.json(); if (!response.ok || data.ok === false) throw Error(data.error || '画师配置读取失败');
        return data;
      }
      async function load(body) {
        if (busy || body && !writable()) return;
        busy = true; box.dataset.busy = '1'; controls();
        try {
          var data = await request(body); if (!box.isConnected) return;
          rows = data.items; if (body) { drafts.delete(body.repo); dirty = drafts.has(repo.value); box.dataset.editing = drafts.size ? '1' : '0'; }
          if (!dirty) show(); message.textContent = body ? '已保存；新任务将使用此画师表。' : '';
        } catch (e) { message.textContent = e.message; }
        finally { busy = false; box.dataset.busy = '0'; controls(); }
      }
      var save = node('button', '保存画师配置'); save.type = 'button'; save.className = 'fp-btn fp-btn--sm';
      save.onclick = function () { if (writable()) load({action:'configure_artist',repo:repo.value,config:config()}); }; box.append(save);
      var detect = node('button', '识别仓库画师分表'); detect.type = 'button'; detect.className = 'fp-btn fp-btn--sm'; box.append(detect);
      detect.onclick = async function () {
        if (busy || !repo.value || !writable()) return;
        var selectedRepo = repo.value, id = generation; busy = true; box.dataset.busy = '1'; controls(); message.textContent = '正在读取仓库文件树并校验分表样本…';
        try {
          var result = await request({action:'detect_artist',repo:selectedRepo,revision:revision.value.trim()});
          if (!box.isConnected || id !== generation || selectedRepo !== repo.value) return;
          detected.replaceChildren(); detected.dataset.artistDetection = '1';
          message.textContent = result.message + ' · 已登记 ' + result.total_packages + ' 个分片' + (result.root_files ? ' · ' + result.root_files + ' 张根目录分表不符合目录配置格式' : '');
          if (!result.candidates.length) return;
          var choices = node('select'); choices.setAttribute('aria-label', '识别到的画师分表目录');
          choices.append(node('option', '请选择识别结果'));
          result.candidates.forEach(function (item, index) {
            var option = node('option', item.directory + ' · 匹配 ' + item.matched + '/' + result.total_packages + ' · 缺表 ' + item.missing + ' · 同名歧义 ' + item.ambiguous);
            option.value = String(index); option.disabled = Boolean(item.error); choices.append(option);
            if (item.error) detected.append(node('p', item.directory + '：' + item.error));
          });
          choices.options[0].value = ''; detected.append(choices);
          var detail = node('p'), use = node('button', '使用所选目录与版本'); use.type = 'button'; use.className = 'fp-btn fp-btn--sm'; use.dataset.needsChoice = '1'; use.disabled = true; detected.append(detail, use);
          choices.onchange = function () {
            var item = choices.value === '' ? null : result.candidates[Number(choices.value)]; use.disabled = !item || Boolean(item.error);
            detail.textContent = item ? item.sample_path + ' · ' + (item.validated ? '样本校验通过，' + item.sample_rows + ' 条，' + item.sample_with_artist + ' 条有画师' : item.error || '保存时校验样本') + (item.missing_samples.length ? ' · 缺表例：' + item.missing_samples.join('、') : '') : '';
          };
          use.onclick = function () {
            if (!writable() || busy || choices.value === '') return;
            directory.value = result.candidates[Number(choices.value)].directory; revision.value = result.revision; mark();
            message.textContent = '已填入目录与固定版本；请确认启用及 TAG / NL 开关，再保存画师配置。';
          };
        } catch (e) { if (id === generation) message.textContent = e.message; }
        finally { busy = false; box.dataset.busy = '0'; controls(); }
      };
      var refresh = node('button','刷新'); refresh.type='button'; refresh.className='fp-btn fp-btn--sm'; refresh.onclick=function(){load();}; box.append(refresh); controls(); load();
      return {setRepos:function (values) {
        var old = repo.value; ++generation; repo.replaceChildren(); values.forEach(function (value) { var o = node('option', value); o.value = value; repo.append(o); });
        if (values.includes(old)) repo.value = old; current = repo.value; dirty = drafts.has(current); detected.replaceChildren(); show(); controls();
      }};
    },
    open: function (repos, writable, trigger) {
      var wrap = node('div'); wrap.className = 'fp-dialogwrap'; var dialog = node('div'); dialog.className = 'fp-dialog'; dialog.setAttribute('role','dialog'); dialog.setAttribute('aria-modal','true'); dialog.setAttribute('aria-label','仓库画师注入与分表');
      var head = node('header'); head.className = 'fp-dialog__hd'; var close = node('button','关闭'); close.type = 'button'; close.className = 'fp-btn fp-btn--sm'; head.append(node('h2','仓库画师注入与分表'),close);
      var body = node('div'); body.className = 'fp-dialog__bd'; dialog.append(head,body); wrap.append(dialog); document.body.append(wrap); this.mount(body,repos,writable);
      function finish() { wrap.remove(); if (trigger && trigger.isConnected) trigger.focus(); }
      close.onclick = finish; wrap.onkeydown = function(event) {
        if(event.key === 'Escape'){event.stopPropagation();finish();}
        if(event.key === 'Tab') {
          var fields = Array.from(wrap.querySelectorAll('button,input,select,a[href]')).filter(function(e){return !e.disabled;});
          if(event.shiftKey && document.activeElement === fields[0]){event.preventDefault();fields[fields.length-1].focus();}
          else if(!event.shiftKey && document.activeElement === fields[fields.length-1]){event.preventDefault();fields[0].focus();}
        }
      };
      close.focus();
    },
    actions: function (parent, selected, post, refresh, message, writable, owner) {
      owner = owner || parent;
      var busy = false, buttons = [], pending = {};
      function update() { buttons.forEach(function(b){b.disabled=busy||!selected.size||!writable();}); }
      [['补注入选中',false],['强制重新注入选中',true]].forEach(function(v){
        var b=node('button',v[0]); b.type='button';b.className='fp-btn fp-btn--sm';buttons.push(b);parent.append(b);
        b.onclick=async function(){if(busy||owner.dataset.busy==='1'||!selected.size||!writable())return;
          busy=true;owner.dataset.busy='1';update();message.textContent='按画师表处理已有产物…';
          try { var grouped={};selected.forEach(function(p){(grouped[p.repo]||(grouped[p.repo]=[])).push(p.id);}); var totalTag=0,totalNl=0,skipped=0;
            for(var repo of Object.keys(grouped))for(var pkg of grouped[repo].sort()){var packages=[pkg],key=JSON.stringify([repo,packages,v[1]]),body=pending[key]||(pending[key]={action:'reinject_artist',repo:repo,packages:packages,force:v[1],request_id:crypto.randomUUID()});
              message.textContent='处理 '+pkg+'；已补注入 TAG '+totalTag+' / NL '+totalNl;
              var result=await post('/api/source-catalog',body);delete pending[key];(result.items||[]).forEach(function(r){totalTag+=r.changed_tag||0;totalNl+=r.changed_nl||0;if(r.skipped)skipped++;});}
            await refresh();message.textContent='已补注入 TAG '+totalTag+' / NL '+totalNl+'；跳过 '+skipped+' 个正在执行或暂无产物的分片。';
          }catch(e){message.textContent=e.message;}finally{busy=false;owner.dataset.busy='0';update();}
        };
      });parent.addEventListener('change',update);update();
    }
  };
})();
