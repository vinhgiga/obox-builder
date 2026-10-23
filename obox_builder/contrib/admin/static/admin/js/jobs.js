(function () {
  "use strict";
  var cleanup = function () {};
  function initPage() {
    cleanup();
    var monitor = document.querySelector("[data-jobs-monitor]");
    if (!monitor) return;
    var toggle = monitor.querySelector("[data-auto-refresh]");
    var status = monitor.querySelector("[data-refresh-status]");
    var busy = false;
    var controller = new AbortController();
    var timer;

    function schedule() {
      clearTimeout(timer);
      if (monitor.isConnected && !controller.signal.aborted && toggle.checked) timer = setTimeout(function () { refresh(false); }, 5000);
    }

    async function refresh(manual) {
      var content = monitor.querySelector("[data-jobs-content]");
      var inspecting = document.querySelector("dialog[open], details[open]") ||
        window.getSelection().toString() ||
        (document.activeElement && document.activeElement.closest("form, pre"));
      if (busy || (!manual && (document.hidden || inspecting || content.dataset.terminal === "true"))) {
        schedule();
        return;
      }
      busy = true;
      try {
        var response = await fetch(window.location.href, {
          signal: controller.signal, credentials: "same-origin", cache: "no-store", headers: {Accept: "text/html"}
        });
        var parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        var replacement = parsed.querySelector("[data-jobs-content]");
        if (!response.ok || response.redirected || !replacement) throw new Error("Refresh unavailable");
        // Do not interrupt inspection started while the request was in flight.
        if (monitor.isConnected && !controller.signal.aborted && !document.querySelector("dialog[open]") && !window.getSelection().toString() &&
            (manual || !document.querySelector("details[open]"))) {
          content.replaceWith(document.importNode(replacement, true));
          if (window.htmx) htmx.process(monitor.querySelector("[data-jobs-content]"));
          status.textContent = "Updated " + new Date().toISOString().slice(11, 19) + " UTC";
        }
      } catch (_) {
        status.textContent = "Update failed · use Refresh to retry";
      } finally {
        busy = false;
        schedule();
      }
    }

    toggle.addEventListener("change", function () {
      status.textContent = toggle.checked ? "Live updates enabled" : "Live updates paused";
      schedule();
    });
    monitor.querySelector("[data-refresh-now]").addEventListener("click", function (event) {
      event.preventDefault();
      refresh(true);
    });
    cleanup = function () {
      clearTimeout(timer);
      controller.abort();
      window.removeEventListener("pageshow", schedule);
    };
    window.addEventListener("pageshow", schedule);
    schedule();
  }
  window.addEventListener("pagehide", function () { cleanup(); });
  document.addEventListener("admin:leave", function () { cleanup(); });
  document.addEventListener("admin:load", initPage);
  initPage();
})();
