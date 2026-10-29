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
    if (!viewport || !window.AdminVirtual) return;
    const card = viewport.closest(".admin-list-card");
    const body = viewport.querySelector("tbody");
    const head = viewport.querySelector("thead");
    const status = card.querySelector("[data-list-status]");
    const retry = card.querySelector("[data-list-retry]");
    const rows = [...body.rows];
    const itemKeys = new WeakMap();
    let nextKey = 0;
    let rowHeight = 56;
    let visible = rows.filter((row) => !row.hidden);
    let nextURL = viewport.dataset.nextUrl;
    let loading = false;
    let stopped = false;
    let failed = false;
    let frame = 0;
    const controller = new AbortController();
    const {Virtualizer, observeElementRect, observeElementOffset, elementScroll} = window.AdminVirtual;
    viewport.adminList = {
      rows,
      queryAll: (selector) => rows.flatMap((row) => [
        ...(row.matches(selector) ? [row] : []), ...row.querySelectorAll(selector),
      ]),
    };

    function spacer(height) {
      const row = document.createElement("tr");
      row.dataset.virtualSpacer = "";
      row.setAttribute("aria-hidden", "true");
      const cell = row.insertCell();
      cell.colSpan = head.rows[0].cells.length;
      cell.style.height = `${Math.max(0, height)}px`;
      return row;
    }

    function schedule() {
      if (!stopped && !frame) frame = requestAnimationFrame(render);
    }

    const virtualizer = new Virtualizer({
      count: visible.length,
      getScrollElement: () => viewport,
      estimateSize: (index) => visible[index].hasAttribute("data-group-header") ? 44 : rowHeight,
      getItemKey: (index) => {
        const row = visible[index];
        if (!itemKeys.has(row)) itemKeys.set(row, ++nextKey);
        return itemKeys.get(row);
      },
      observeElementRect,
      observeElementOffset,
      scrollToFn: elementScroll,
      scrollMargin: head.offsetHeight,
      overscan: 8,
      onChange: schedule,
    });
    const unmount = virtualizer._didMount();

    function layout() {
      if (stopped) return;
      card.style.height = `${Math.max(160, window.innerHeight - card.getBoundingClientRect().top - 12)}px`;
      visible = rows.filter((row) => !row.hidden);
      rowHeight = parseFloat(getComputedStyle(viewport).getPropertyValue("--admin-row-height")) *
        parseFloat(getComputedStyle(document.documentElement).fontSize);
      virtualizer.setOptions({...virtualizer.options, count: visible.length, scrollMargin: head.offsetHeight});
      virtualizer.measure();
      virtualizer._willUpdate();
      schedule();
    }

    function render() {
      frame = 0;
      if (stopped) return;
      const items = [...virtualizer.getVirtualItems()];
      const focusedRow = document.activeElement?.closest("tr");
      const focusedIndex = visible.indexOf(focusedRow);
      if (focusedIndex >= 0 && !items.some((item) => item.index === focusedIndex)) {
        const measurement = virtualizer.getMeasurements()[focusedIndex];
        if (measurement) items.push(measurement);
        items.sort((a, b) => a.index - b.index);
      }
      const desired = [];
      let end = 0;
      for (const item of items) {
        const start = item.start - virtualizer.options.scrollMargin;
        if (start > end) desired.push(spacer(start - end));
        const row = visible[item.index];
        row.dataset.index = item.index;
        row.setAttribute("aria-rowindex", row.dataset.rowKey
          ? Number(row.querySelector(".admin-row-number").textContent) + 1 : item.index + 2);
        desired.push(row);
        end = item.end - virtualizer.options.scrollMargin;
      }
      const remaining = virtualizer.getTotalSize() - end;
      if (remaining > 0) desired.push(spacer(remaining));
      const keep = new Set(desired);
      [...body.children].forEach((row) => { if (!keep.has(row)) row.remove(); });
      desired.forEach((row, index) => {
        if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null);
      });
      desired.filter((row) => !row.hasAttribute("data-virtual-spacer"))
        .forEach((row) => virtualizer.measureElement(row));
      if ((visible.length === rows.length || viewport.scrollTop > 0) &&
          viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < viewport.clientHeight) {
        loadNext();
      }
    }

    async function loadNext() {
      if (!nextURL || loading || stopped || failed) return;
      loading = true;
      viewport.setAttribute("aria-busy", "true");
      status.textContent = gettext("Loading records…");
      try {
        const response = await fetch(window.localizeAdminURL(nextURL), {
          headers: {"X-Admin-Rows": "true"}, signal: controller.signal,
        });
        if (!response.ok) throw new Error("Load failed");
        const document = new DOMParser().parseFromString(await response.text(), "text/html");
        const page = document.querySelector("[data-list-page]");
        if (!page) throw new Error("Invalid page");
        const incoming = [...page.querySelector("tbody").rows].filter((row) =>
          row.matches("[data-group-header], [data-group-row]"));
        // Consecutive pages can begin inside the same group.
        const path = [];
        rows.forEach((row) => {
          if (row.dataset.groupHeader) {
            path.length = Number(row.dataset.groupHeader);
            path[path.length - 1] = row.dataset.groupKey;
          }
        });
        let firstRecord = true;
        const keys = new Set(rows.filter((row) => row.dataset.rowKey).map((row) => row.dataset.rowKey));
        incoming.forEach((row) => {
          if (firstRecord && row.dataset.groupHeader) {
            const level = Number(row.dataset.groupHeader) - 1;
            if (path[level] === row.dataset.groupKey) return;
            path.length = level;
          } else firstRecord = false;
          if (row.dataset.rowKey && keys.has(row.dataset.rowKey)) return;
          rows.push(row);
          body.append(row);
        });
        nextURL = page.dataset.nextUrl;
        viewport.dataset.nextUrl = nextURL;
        viewport.dispatchEvent(new Event("admin:rows", {bubbles: true}));
        if (!viewport.dataset.total) {
          card.querySelector("[data-list-total]").textContent = gettext("%(count)s records loaded", {count: rows.filter((row) => row.dataset.rowKey).length});
        }
        if (window.htmx) htmx.process(body);
        layout();
        status.textContent = nextURL ? "" : gettext("All records loaded");
      } catch (error) {
        if (!controller.signal.aborted) {
          failed = true;
          status.textContent = gettext("Could not load records.");
          retry.classList.remove("d-none");
        }
      } finally {
        loading = false;
        viewport.removeAttribute("aria-busy");
        schedule();
      }
    }

    retry.addEventListener("click", () => {
      failed = false;
      retry.classList.add("d-none");
      loadNext();
    });
    // Detached rows still participate in native and boosted form submissions.
    function attachRows() { body.replaceChildren(...rows); }
    function beforeSubmit(event) {
      if (["inline-edit", "bulk-form"].includes(event.target.id)) {
        attachRows();
        schedule();
      }
    }
    document.addEventListener("submit", beforeSubmit, true);
    viewport.addEventListener("admin:group", layout);
    viewport.addEventListener("admin:columns", layout);
    const attributes = new MutationObserver(layout);
    attributes.observe(viewport, {attributes: true, attributeFilter: ["data-row-height", "data-wrap-headers"]});
    const resize = new ResizeObserver(layout);
    resize.observe(card.querySelector(".admin-list-toolbar"));
    resize.observe(document.querySelector(".admin-list-header"));
    window.addEventListener("resize", layout);
    card.classList.add("is-virtual");
    dispose = () => {
      stopped = true;
      controller.abort();
      cancelAnimationFrame(frame);
      attributes.disconnect();
      resize.disconnect();
      unmount();
      window.removeEventListener("resize", layout);
      document.removeEventListener("submit", beforeSubmit, true);
      attachRows();
      delete viewport.adminList;
      card.classList.remove("is-virtual");
      dispose = () => {};
    };
    layout();
  }
  init();
})();
