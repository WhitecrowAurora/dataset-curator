/* 待派记录直接取消；复用原有预览、执行和审计通道。 */
(function () {
  "use strict";
  var busy = false, action = "package.cancel_reservation";

  async function run(targets, spec, access, retryPayload) {
    if (busy || !access.writable()) return;
    busy = true;
    var payload = retryPayload, trigger = access.trigger;
    var disabled = trigger && trigger.disabled;
    if (trigger) trigger.disabled = true;
    access.running(true);
    try {
      if (!payload) {
        var response = await access.write("/api/write/preview", { action: action, targets: targets, params: {} });
        var preview = response.body && response.body.preview;
        if (response.status !== 200 || !preview || !preview.confirm_token) {
          access.failed(response.body.error || "取消待派失败", function () { return run(targets, spec, access); });
          return;
        }
        payload = {
          action: action, targets: preview.targets, params: preview.params,
          confirm_token: preview.confirm_token, __idem: access.newIdem(),
          batch_confirm: {
            mutations: preview.impact.mutations,
            target_hash: preview.target_hash, impact_hash: preview.impact.hash
          }
        };
      }
      if (!access.writable()) return;
      var result = await access.write(spec.path, payload);
      if (result.status === 200) {
        access.done(action, result.body.results || [], {
          batch_id: result.body.batch_id, logged: result.body.logged,
          replay: result.replay, total: result.body.total,
          succeeded: result.body.succeeded, failed: result.body.failed
        });
      } else if ((result.status === 404 || result.status === 409) && result.body.write_id) {
        access.done(action, [{ target: targets[0], ok: false, code: result.status, error: result.body.error }], {
          batch_id: result.body.batch_id, logged: result.body.logged, total: 1, succeeded: 0, failed: 1
        });
      } else {
        var unknown = result.status === 0 || result.status >= 500;
        access.failed(result.body.error || "取消待派失败", function () {
          return run(targets, spec, access, unknown ? payload : null);
        });
      }
    } catch (error) {
      access.failed(error.message || "取消待派失败", function () { return run(targets, spec, access, payload); });
    } finally {
      busy = false;
      if (trigger && trigger.isConnected !== false) trigger.disabled = disabled;
      access.running(false);
    }
  }

  window.FoxPendingActions = { run: run, busy: function () { return busy; } };
})();
