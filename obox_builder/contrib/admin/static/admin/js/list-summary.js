(function () {
  "use strict";
  const gettext = window.oboxGettext;
  let dispose = () => {};
  document.addEventListener("admin:leave", () => dispose());
  window.addEventListener("pagehide", () => dispose());
  document.addEventListener("admin:load", init);

  function init() {
    dispose();
    const viewport = document.querySelector("[data-list-table]");
    const footer = document.querySelector("[data-list-summary]");
    if (!viewport || !footer) return;
    const menus = [...footer.querySelectorAll("[data-summary-field]")];
    const storageKey = `admin-summary:${viewport.dataset.listKey}`;
    let preferences = {};
    let stopped = false;
    let frame = 0;
    const requests = new Map();
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "{}");
      if (value && typeof value === "object" && !Array.isArray(value)) preferences = value;
    } catch {}

    function align() {
      frame = 0;
      const headers = [...viewport.querySelectorAll("thead th")];
      footer.style.width = `${viewport.clientWidth}px`;
      footer.querySelector("table").style.width = `${viewport.querySelector("table").getBoundingClientRect().width}px`;
      footer.querySelector("[data-summary-status]").style.width = `${headers.slice(0, 2).reduce((sum, cell) => sum + cell.getBoundingClientRect().width, 0)}px`;
      headers.filter((cell) => cell.dataset.listColumn).forEach((header) => {
        const cell = [...footer.querySelectorAll("td[data-list-column]")]
          .find((item) => item.dataset.listColumn === header.dataset.listColumn);
        cell.style.width = `${header.getBoundingClientRect().width}px`;
      });
      footer.scrollLeft = viewport.scrollLeft;
      menus.filter((menu) => menu.open).forEach(position);
    }
    function scheduleAlign() { if (!frame) frame = requestAnimationFrame(align); }
    const resize = new ResizeObserver(scheduleAlign);
    viewport.querySelectorAll("thead th").forEach((cell) => resize.observe(cell));
    viewport.addEventListener("admin:columns", scheduleAlign);
    viewport.addEventListener("scroll", () => { footer.scrollLeft = viewport.scrollLeft; }, {passive: true});
    footer.addEventListener("scroll", () => { viewport.scrollLeft = footer.scrollLeft; }, {passive: true});

    async function choose(menu, operation, persist = true) {
      const field = menu.dataset.summaryField;
      const buttons = [...menu.querySelectorAll("[data-summary-operation]")];
      const selected = buttons.find((button) => button.dataset.summaryOperation === operation) || buttons[0];
      operation = selected.dataset.summaryOperation;
      requests.get(field)?.abort();
      const output = menu.querySelector("[data-summary-value]");
      menu.open = false;
      buttons.forEach((button) => button.setAttribute("aria-checked", String(button === selected)));
      if (persist) {
        preferences[field] = operation;
        try { localStorage.setItem(storageKey, JSON.stringify(preferences)); } catch {}
      }
      if (operation === "none") {
        output.textContent = gettext("Summary");
        menu.removeAttribute("aria-busy");
        return;
      }
      const controller = new AbortController();
      requests.set(field, controller);
      menu.setAttribute("aria-busy", "true");
      output.textContent = gettext("Loading…");
      const url = new URL(window.localizeAdminURL(`${viewport.dataset.listKey}/summary`), location.origin);
      url.search = location.search;
      url.searchParams.delete("page");
      url.searchParams.set("field", field);
      url.searchParams.set("operation", operation);
      try {
        const response = await fetch(url, {signal: controller.signal});
        if (!response.ok) throw new Error("Summary failed");
        const result = await response.json();
        if (!stopped && !controller.signal.aborted) output.textContent = `${selected.textContent} ${result.text}`;
      } catch {
        if (!stopped && !controller.signal.aborted) output.textContent = gettext("Unavailable · retry");
      } finally {
        if (requests.get(field) === controller) {
          requests.delete(field);
          menu.removeAttribute("aria-busy");
        }
      }
    }

    function closeMenus(event) {
      menus.forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; });
    }
    function keydown(event) {
      const menu = menus.find((item) => item.open);
      if (!menu) return;
      if (event.key === "Escape") {
        event.preventDefault();
        menu.open = false;
        menu.querySelector("summary").focus();
      } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const buttons = [...menu.querySelectorAll("button")];
        const index = buttons.indexOf(document.activeElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
          : (index + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length;
        buttons[next].focus();
      }
    }
    function position(menu) {
      if (!menu.open) return;
      menus.forEach((other) => { if (other !== menu) other.open = false; });
      const bounds = menu.querySelector("summary").getBoundingClientRect();
      const panel = menu.querySelector("[role=menu]");
      panel.style.left = `${Math.max(8, Math.min(bounds.left, innerWidth - panel.offsetWidth - 8))}px`;
      panel.style.bottom = `${innerHeight - bounds.top + 4}px`;
      panel.style.maxHeight = `${Math.min(352, bounds.top - 8)}px`;
    }
    menus.forEach((menu) => {
      menu.addEventListener("toggle", () => position(menu));
      menu.querySelector("summary").addEventListener("click", () => queueMicrotask(() => position(menu)));
      menu.addEventListener("click", (event) => {
        const button = event.target.closest("[data-summary-operation]");
        if (!button) return;
        choose(menu, button.dataset.summaryOperation);
        menu.querySelector("summary").focus();
      });
      choose(menu, preferences[menu.dataset.summaryField] || "none", false);
    });
    document.addEventListener("click", closeMenus);
    document.addEventListener("keydown", keydown);
    window.addEventListener("resize", scheduleAlign);
    dispose = () => {
      stopped = true;
      requests.forEach((controller) => controller.abort());
      cancelAnimationFrame(frame);
      resize.disconnect();
      document.removeEventListener("click", closeMenus);
      document.removeEventListener("keydown", keydown);
      window.removeEventListener("resize", scheduleAlign);
      dispose = () => {};
    };
    align();
  }
  init();
})();
