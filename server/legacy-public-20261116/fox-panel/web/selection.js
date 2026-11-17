/* 调度面板共用的列表选择：选择保留在组件中，搜索不改变选择。 */
(function () {
  'use strict';
  function node(tag, text, cls) {
    var e = document.createElement(tag);
    if (text) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }
  function mount(parent, config) {
    config = config || {};
    var box = node('fieldset', '', 'fp-selection'), toolbar = node('div', '', 'fp-selection-toolbar');
    var search = node('input'), count = node('span'), list = node('div', '', 'fp-selection-list');
    var empty = node('p', config.empty || '暂无可选项', 'fp-note'), selected = new Set(config.values || []);
    var rows = new Map(), items = [], total, locked = false;
    search.type = 'search'; search.placeholder = config.placeholder || '搜索';
    search.setAttribute('aria-label', (config.title || '列表') + '搜索');
    count.setAttribute('role', 'status');
    box.append(node('legend', config.title || '选择'), toolbar, list);
    toolbar.append(search, count); parent.append(box);
    function blocked(item) { return locked || !!item.disabled || !!(config.disabled && config.disabled()); }
    function refresh() {
      var query = search.value.trim().toLowerCase(), visible = 0;
      rows.forEach(function (row, id) {
        row.element.hidden = !!query && (config.searchMode === 'prefix' ? !row.text.startsWith(query) : row.text.indexOf(query) < 0);
        row.label.hidden = row.element.hidden;
        if (!row.label.hidden) visible++;
        row.input.checked = selected.has(id); row.input.disabled = blocked(row.item);
        if (row.action) row.action.disabled = locked || !!(config.disabled && config.disabled());
        row.label.classList.toggle('is-selected', selected.has(id));
      });
      empty.hidden = !!visible;
      empty.textContent = query ? (config.noMatch || '没有匹配项') : (config.empty || '暂无可选项');
      count.textContent = '已选 ' + selected.size + ' / 共 ' + (total == null ? items.length : total) + ' 个';
      if (total != null && total > items.length) count.textContent += ' · 已加载 ' + items.length;
      if (query) count.textContent += ' · 筛选 ' + visible + ' 个';
      if (all) all.disabled = locked || !!(config.disabled && config.disabled()) || !visible;
      clear.disabled = locked || !!(config.disabled && config.disabled()) || !selected.size;
    }
    function changed() {
      refresh();
      if (config.onChange) config.onChange(Array.from(selected));
      box.dispatchEvent(new Event('change', {bubbles:true}));
    }
    function choose(id, checked) {
      if (!checked) selected.delete(id);
      else if (!config.max || selected.size < config.max || selected.has(id)) {
        if (config.single) selected.clear();
        selected.add(id);
      }
    }
    var all;
    if (!config.single) {
      all = node('button', '选择筛选结果', 'fp-btn fp-btn--sm'); all.type = 'button';
      all.onclick = function () {
        rows.forEach(function (row, id) { if (!row.label.hidden && !blocked(row.item)) choose(id, true); });
        changed();
      };
      toolbar.append(all);
    }
    var clear = node('button', '清空', 'fp-btn fp-btn--sm'); clear.type = 'button';
    clear.onclick = function () { selected.clear(); changed(); }; toolbar.append(clear);
    search.addEventListener('input', refresh);
    function setItems(next, nextTotal) {
      items = next; total = nextTotal; rows.clear(); list.replaceChildren();
      var fragment = document.createDocumentFragment();
      items.forEach(function (item) {
        var label = node('label', '', 'fp-selection-row'), input = node('input'), content = node('span', '', 'fp-selection-content');
        var heading = node('span', '', 'fp-selection-heading'), element = label, action;
        input.type = 'checkbox'; input.value = item.id;
        heading.append(node('span', item.label || item.id));
        if (item.meta) heading.append(node('span', item.meta, 'fp-selection-meta'));
        content.append(heading);
        if (item.detail) content.append(node('small', item.detail));
        label.append(input, content);
        if (item.action && config.onAction) {
          element = node('div', '', 'fp-selection-entry');
          action = node('button', item.action, 'fp-btn fp-btn--sm'); action.type = 'button';
          action.onclick = function () { config.onAction(item, action); };
          element.append(label, action);
        }
        fragment.append(element);
        input.onchange = function () { if (!blocked(item)) { choose(item.id, input.checked); changed(); } };
        rows.set(item.id, {element: element, label: label, input: input, item: item, action: action,
          text: (config.searchMode === 'prefix' ? (item.search || item.label || item.id) : ((item.label || item.id) + ' ' + (item.detail || ''))).toLowerCase()});
      });
      list.append(fragment, empty); refresh();
    }
    setItems(config.items || [], config.total);
    return {element: box, values: function () { return Array.from(selected); },
      setValues: function (values) { selected = new Set(values); refresh(); },
      setItems: setItems, refresh: refresh, disable: function (value) { locked = value; refresh(); }};
  }
  function fromSelect(select, title, config) {
    var holder = node('div'), label = select.parentElement;
    select.hidden = true;
    if (label.tagName === 'LABEL') { label.hidden = true; label.after(holder); }
    else select.after(holder);
    var picker = mount(holder, Object.assign({}, config, {title: title, single: true,
      disabled: function () { return select.disabled; },
      onChange: function (values) { select.value = values[0] || ''; select.dispatchEvent(new Event('change', {bubbles: true})); }}));
    picker.sync = function () {
      picker.setItems(Array.from(select.options).filter(function (o) { return o.value; })
        .map(function (o) { return {id: o.value, label: o.textContent, disabled: o.disabled}; }));
      picker.setValues(select.value ? [select.value] : []);
    };
    picker.sync(); return picker;
  }
  window.FoxSelection = {mount: mount, fromSelect: fromSelect};
})();
