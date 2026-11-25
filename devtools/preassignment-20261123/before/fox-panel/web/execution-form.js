/* 派单与分派表使用同一组有标签、按输出模式分类的执行选项。 */
(function () {
  'use strict';
  function node(tag, text) { var e=document.createElement(tag);if(text)e.textContent=text;return e; }
  function select(title, values, initial, prefix) {
    var input=node('select');input.setAttribute('aria-label',prefix+' '+title);
    values.forEach(function(v){var o=node('option',Array.isArray(v)?v[1]:v);o.value=Array.isArray(v)?v[0]:v;input.append(o);});
    if(initial)input.value=initial;return input;
  }
  function field(parent, title, input) {var label=node('label');label.append(node('span',title),input);parent.append(label);}
  function group(parent, title) {var box=node('div');box.className='fp-dispatch-form-group';box.append(node('h4',title));parent.append(box);return box;}
  window.FoxExecutionForm={mount:function(parent,options){
    var box=node('div'),prefix=options.prefix;box.className='fp-dispatch-form';parent.append(box);
    var mode=options.mode?select('打标模式',[['both','TAG + NL'],['tag','只出 TAG'],['nl','只出 NL']],options.mode,prefix):null;
    if(mode){var output=group(box,'输出内容');field(output,'打标模式',mode);}
    var tagGroup=group(box,'TAG 识别'),tag=select('TAG 模型',['wd-eva02','pixai','cl-tagger-v2','onnx'],options.tag||'wd-eva02',prefix);
    field(tagGroup,'TAG 模型',tag);
    var nlGroup=group(box,'NL 描述');
    var engine=select('NL 引擎',[['native','native（专用引擎）'],['llamacpp','llama.cpp'],['vllm','vLLM']],options.engine||'llamacpp',prefix);
    var precision=node('select'),model=node('input');precision.setAttribute('aria-label',prefix+' NL 精度');model.setAttribute('aria-label',prefix+' NL 模型');
    model.placeholder='留空自动选择';field(nlGroup,'NL 引擎',engine);field(nlGroup,'NL 模型',model);field(nlGroup,'NL 精度',precision);
    function precisions(){
      var previous=precision.value||options.precision;precision.replaceChildren();
      var values=engine.value==='native'?['q6','q8','q4','q5','f16','fp16','bf16','fp8','int8','w4a16']:engine.value==='llamacpp'?['f16','q4','q6','q8']:['bf16','fp8','w4a16'];
      values.forEach(function(v){var o=node('option',v.toUpperCase());o.value=v;precision.append(o);});
      if(values.includes(previous))precision.value=previous;
    }
    function update(){tagGroup.hidden=!!mode&&mode.value==='nl';nlGroup.hidden=!!mode&&mode.value==='tag';}
    engine.addEventListener('change',precisions);if(mode)mode.addEventListener('change',update);precisions();update();
    if(options.writable&&!options.writable())box.querySelectorAll('input,select').forEach(function(input){input.disabled=true;});
    return {element:box,value:function(){return Object.assign(mode?{mode:mode.value}:{},
      {tag_backend:tag.value,nl_engine:engine.value,nl_model:model.value.trim(),nl_precision:precision.value});}};
  }};
})();
