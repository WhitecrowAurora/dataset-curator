/* 分片明细分页；正文使用 textContent，原因中出现 HTML 时仍作为文本显示。 */
(function () {
  "use strict";
  function node(tag, text) {
    var n = document.createElement(tag); if (text) n.textContent = text; return n;
  }
  window.FoxSkips = {render: function (dl, pkg, row, api) {
    var data = pkg.skips, box = node("div"), list = node("div"), status = node("p");
    var more = node("button", "加载更多"); more.type = "button"; more.className = "fp-btn fp-btn--sm";
    var names = {skipped: "跳过", excused: "重试后剔除", excluded: "过滤"};
    function append(page) {
      status.textContent = "共 " + page.total + " 条记录（当前或已交付租约）";
      (page.items || []).forEach(function (item) {
        var entry = node("details"), title = node("summary", item.image_id + " · " + (names[item.kind] || item.kind));
        entry.append(title, node("p", item.reason || "未提供原因"),
                     node("small", "设备：" + item.device_id + " · 租约：" + item.lease_id));
        list.appendChild(entry);
      });
      more.hidden = page.next_offset === null || page.next_offset === undefined;
      more.dataset.offset = String(page.next_offset || 0);
    }
    box.append(status, list, more); row("跳过／剔除明细", box);
    if (!data || data.error) {
      status.textContent = data && data.error || "请刷新详情读取跳过明细"; more.hidden = true; return;
    }
    append(data);
    more.addEventListener("click", function () {
      more.disabled = true;
      api("/api/skips?repo=" + encodeURIComponent(pkg.repo) + "&package=" + encodeURIComponent(pkg.id)
          + "&offset=" + more.dataset.offset).then(append).catch(function (error) {
        status.textContent = "读取下一页失败：" + error.message + "。请重试。";
      }).finally(function () {more.disabled = false;});
    });
  }};
})();
