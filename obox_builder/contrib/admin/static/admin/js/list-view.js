(function initListView() {
  "use strict";
  let activeToolbar = null;
  let activePopovers = [];
  document.addEventListener("admin:load", initListView);
  document.addEventListener("admin:leave", () => {
    activeToolbar = null;
    activePopovers = [];
  });
  document.addEventListener("click", (event) => {
    if (activeToolbar && !activeToolbar.contains(event.target)) {
      activePopovers.forEach((popover) => { popover.open = false; });
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const openPopover = activePopovers.find((popover) => popover.open);
    if (!openPopover) return;
    event.preventDefault();
    const nested = [...openPopover.querySelectorAll("details[open]")].at(-1);
    if (nested) {
      nested.open = false;
      nested.querySelector("summary")?.focus();
      return;
    }
    openPopover.open = false;
    openPopover.querySelector("summary").focus();
  });
  const toolbar = document.querySelector("[data-list-toolbar]");
  const table = document.querySelector("[data-list-table]");
  if (!toolbar || !table) return;

  const popovers = [...toolbar.querySelectorAll(":scope > details")];
  activeToolbar = toolbar;
  activePopovers = popovers;
  const toggles = [...toolbar.querySelectorAll("[data-field-toggle]")];
  const storageKey = `admin-list:${table.dataset.listKey}`;
  const readState = () => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "null");
      return value && typeof value === "object" ? value : {};
    } catch {
      return {};
    }
  };
  const state = readState();
  const saveState = () => {
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({
          hidden: toggles.filter((input) => !input.checked).map((input) => input.dataset.fieldToggle),
          rowHeight: table.dataset.rowHeight,
        }),
      );
    } catch {
      // Keep the controls usable when browser storage is unavailable.
    }
  };
  const applyColumns = () => {
    const visibleCount = toggles.filter((input) => input.checked).length;
    table.querySelectorAll("[data-list-column]").forEach((cell) => {
      const input = toggles.find((item) => item.dataset.fieldToggle === cell.dataset.listColumn);
      cell.hidden = Boolean(input && !input.checked);
    });
    const fixedColumns = table.querySelectorAll("thead th:not([data-list-column])").length;
    table.querySelectorAll("[data-list-colspan]").forEach((cell) => {
      cell.colSpan = fixedColumns + visibleCount;
    });
  };
  const setHeight = (height) => {
    if (!["compact", "medium", "tall"].includes(height)) return;
    table.dataset.rowHeight = height;
    toolbar.querySelectorAll("[data-row-height-choice]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.rowHeightChoice === height));
    });
  };

  if (Array.isArray(state.hidden)) {
    const hidden = new Set(state.hidden);
    toggles.forEach((input) => {
      input.checked = !hidden.has(input.dataset.fieldToggle);
    });
  }
  setHeight(state.rowHeight || "medium");
  applyColumns();

  toggles.forEach((input) => input.addEventListener("change", () => {
    applyColumns();
    saveState();
  }));
  toolbar.querySelector("[data-hide-all]")?.addEventListener("click", () => {
    toggles.forEach((input) => { input.checked = false; });
    applyColumns();
    saveState();
  });
  toolbar.querySelector("[data-show-all]")?.addEventListener("click", () => {
    toggles.forEach((input) => { input.checked = true; });
    applyColumns();
    saveState();
  });
  toolbar.querySelector("[data-field-search]")?.addEventListener("input", (event) => {
    const query = event.currentTarget.value.trim().toLocaleLowerCase();
    const options = [...toolbar.querySelectorAll("[data-field-option]")];
    options.forEach((option) => {
      option.hidden = !option.dataset.fieldLabel.includes(query);
    });
    const empty = toolbar.querySelector("[data-field-empty]");
    if (empty) empty.classList.toggle("d-none", options.some((option) => !option.hidden));
  });
  toolbar.querySelectorAll("[data-row-height-choice]").forEach((button) => {
    button.addEventListener("click", () => {
      setHeight(button.dataset.rowHeightChoice);
      saveState();
    });
  });
  const positionPanel = (popover) => {
    const summary = popover.querySelector(":scope > summary");
    const panel = popover.querySelector(":scope > .admin-toolbar-panel");
    const bounds = toolbar.closest(".admin-list-card")?.getBoundingClientRect();
    if (!summary || !panel || !bounds) return;
    panel.style.maxWidth = `${Math.max(0, summary.getBoundingClientRect().right - bounds.left - 8)}px`;
  };
  window.addEventListener("resize", () => {
    popovers.filter((popover) => popover.open).forEach(positionPanel);
  });
  popovers.forEach((popover) => popover.addEventListener("toggle", () => {
    if (popover.open) {
      positionPanel(popover);
      popovers.forEach((other) => {
        if (other !== popover) other.open = false;
      });
    } else {
      const panel = popover.querySelector(":scope > .admin-toolbar-panel");
      if (panel) panel.style.maxWidth = "";
      popover.querySelectorAll("details[open]").forEach((nested) => { nested.open = false; });
    }
  }));
})();
