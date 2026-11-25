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
    var tagModels=options.advancedModels?node('details'):tagGroup;
    if(options.advancedModels){tagModels.append(node('summary','高级：指定 TAG 模型'));tagGroup.append(tagModels);}
    field(tagModels,'TAG 模型',tag);
    function number(title,max){var input=node('input');input.type='number';input.min=0;input.max=max;input.step=1;input.value='0';input.setAttribute('aria-label',prefix+' '+title);return input;}
    var tagBatch=options.parallel?number('TAG 批大小',128):null;
    if(tagBatch)field(tagGroup,'TAG 批大小（0 = 自动）',tagBatch);
    var nlGroup=group(box,'NL 描述');
    var engine=select('NL 引擎',[['native','native（专用引擎）'],['llamacpp','llama.cpp'],['vllm','vLLM']],options.engine||'llamacpp',prefix);
    var precision=node('select'),model=node('input');precision.setAttribute('aria-label',prefix+' NL 精度');model.setAttribute('aria-label',prefix+' NL 模型');
    var nlModels=options.advancedModels?node('details'):nlGroup;
    if(options.advancedModels){nlModels.append(node('summary','高级：指定 NL 模型与引擎'));nlGroup.append(nlModels);}
    model.placeholder='留空自动选择';field(nlModels,'NL 引擎',engine);field(nlModels,'NL 模型',model);field(nlModels,'NL 精度',precision);
    var concurrency=options.parallel?number('NL 并发',256):null;
    if(concurrency)field(nlGroup,'NL 并发（0 = 自动）',concurrency);
    var specified=new Set();
    [[tag,'tag_backend'],[engine,'nl_engine'],[model,'nl_model'],[precision,'nl_precision']].forEach(function(item){item[0].addEventListener('change',function(){specified.add(item[1]);});});
    if(options.advancedModels)box.append(node('p','指定模型仅在编辑高级选项后生效；既有要求会原样保留。'));
    function precisions(){
      var previous=precision.value||options.precision;precision.replaceChildren();
      var values=engine.value==='native'?['q6','q8','q4','q5','f16','fp16','bf16','fp8','int8','w4a16']:engine.value==='llamacpp'?['f16','q4','q6','q8']:['bf16','fp8','w4a16'];
      values.forEach(function(v){var o=node('option',v.toUpperCase());o.value=v;precision.append(o);});
      if(values.includes(previous))precision.value=previous;
    }
    function update(){tagGroup.hidden=!!mode&&mode.value==='nl';nlGroup.hidden=!!mode&&mode.value==='tag';}
    engine.addEventListener('change',precisions);if(mode)mode.addEventListener('change',update);precisions();update();
    if(options.writable&&!options.writable())box.querySelectorAll('input,select').forEach(function(input){input.disabled=true;});
    return {element:box,setValue:function(value){
      specified=new Set(Object.keys(value));
      if(mode&&value.mode)mode.value=value.mode;
      if(value.tag_backend)tag.value=value.tag_backend;
      if(value.nl_engine)engine.value=value.nl_engine;
      precisions();if(value.nl_precision)precision.value=value.nl_precision;
      model.value=value.nl_model||'';
      if(tagBatch){tagBatch.value=value.tag_batch||0;concurrency.value=value.concurrency||0;}
      update();
    },value:function(){var models={tag_backend:tag.value,nl_engine:engine.value,nl_model:model.value.trim(),nl_precision:precision.value};
      if(options.advancedModels)Object.keys(models).forEach(function(key){if(!specified.has(key))delete models[key];});
      return Object.assign(mode?{mode:mode.value}:{},models,
      tagBatch?{tag_batch:Number(tagBatch.value),concurrency:Number(concurrency.value)}:{});}};
  }};
})();
