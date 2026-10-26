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
  const fieldList = toolbar.querySelector(".admin-field-list");
  const fieldNames = toggles.map((input) => input.dataset.fieldToggle);
  const fieldOptions = new Map(
    toggles.map((input) => [input.dataset.fieldToggle, input.closest("[data-field-option]")]),
  );
  const storedOrder = Array.isArray(state.order) ? state.order : [];
  const order = [
    ...storedOrder.filter((name, index) => fieldOptions.has(name) && storedOrder.indexOf(name) === index),
    ...fieldNames.filter((name) => !storedOrder.includes(name)),
  ];
  const animateFieldItems = (update) => {
    if (!fieldList) return update();
    const items = [...fieldList.children].filter(
      (item) => item.matches("[data-field-option], .admin-field-placeholder"),
    );
    const previousPositions = new Map(items.map((item) => [item, item.getBoundingClientRect().top]));
    update();
    const moved = [];
    previousPositions.forEach((top, item) => {
      const offset = top - item.getBoundingClientRect().top;
      if (Math.abs(offset) < 1) return;
      item.style.transition = "none";
      item.style.transform = `translateY(${offset}px)`;
      moved.push(item);
    });
    if (moved.length) {
      fieldList.getBoundingClientRect();
      requestAnimationFrame(() => {
        moved.forEach((item) => {
          item.style.transition = "";
          item.style.transform = "";
        });
      });
    }
  };
  const renderFieldOrder = (animate = false) => {
    if (!fieldList) return;
    const update = () => {
      const empty = fieldList.querySelector("[data-field-empty]");
      order.forEach((name) => {
        const option = fieldOptions.get(name);
        if (option) fieldList.insertBefore(option, empty);
      });
    };
    if (animate) animateFieldItems(update);
    else update();
  };
  const saveState = () => {
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({
          hidden: toggles.filter((input) => !input.checked).map((input) => input.dataset.fieldToggle),
          order,
          rowHeight: table.dataset.rowHeight,
        }),
      );
    } catch {
      // Keep the controls usable when browser storage is unavailable.
    }
  };
  const applyFieldOrder = () => {
    table.querySelectorAll("tr").forEach((row) => {
      const cells = [...row.children].filter((cell) => cell.hasAttribute("data-list-column"));
      if (!cells.length) return;
      const byName = new Map(cells.map((cell) => [cell.dataset.listColumn, cell]));
      const cellOrder = [
        ...order,
        ...cells.map((cell) => cell.dataset.listColumn).filter((name) => !order.includes(name)),
      ];
      cellOrder.forEach((name) => {
        const cell = byName.get(name);
        if (cell) row.append(cell);
      });
    });
  };
  const applyColumns = () => {
    applyFieldOrder();
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
  renderFieldOrder();
  applyColumns();

  const placeField = (name, targetName, after = false) => {
    const from = order.indexOf(name);
    const target = order.indexOf(targetName);
    if (from < 0 || target < 0 || from === target) return false;
    let destination = target + (after ? 1 : 0);
    if (from < destination) destination -= 1;
    if (from === destination) return false;
    order.splice(from, 1);
    order.splice(destination, 0, name);
    renderFieldOrder(true);
    applyColumns();
    saveState();
    return true;
  };
  let draggedField = null;
  let draggedOption = null;
  let dragPlaceholder = null;
  let dragStartOrder = null;
  let pendingFieldDrag = null;
  const readPlaceholderOrder = () => {
    const nextOrder = [...fieldList.children].flatMap((item) => {
      if (item === dragPlaceholder) return [draggedField];
      return item.matches("[data-field-option]") && item !== draggedOption
        ? [item.dataset.fieldOption]
        : [];
    });
    order.splice(0, order.length, ...nextOrder);
  };
  const movePlaceholderTo = (centerY) => {
    const options = [...fieldList.children].filter(
      (item) => item.matches("[data-field-option]") && item !== draggedOption,
    );
    const next = options.find((option) => {
      const rect = option.getBoundingClientRect();
      return centerY < rect.top + rect.height / 2;
    }) || fieldList.querySelector("[data-field-empty]");
    if (dragPlaceholder.nextElementSibling === next) return;
    animateFieldItems(() => fieldList.insertBefore(dragPlaceholder, next || null));
  };
  const finishFieldDrag = (commit) => {
    if (!pendingFieldDrag) return;
    if (draggedOption && dragPlaceholder) {
      if (commit) readPlaceholderOrder();
      dragPlaceholder.replaceWith(draggedOption);
      draggedOption.classList.remove("is-dragging");
      draggedOption.style.position = "";
      draggedOption.style.top = "";
      draggedOption.style.left = "";
      draggedOption.style.width = "";
      if (commit) {
        applyColumns();
        saveState();
      } else if (dragStartOrder) {
        order.splice(0, order.length, ...dragStartOrder);
        renderFieldOrder(true);
      }
    }
    document.body.classList.remove("admin-field-dragging");
    draggedField = null;
    draggedOption = null;
    dragPlaceholder = null;
    dragStartOrder = null;
    pendingFieldDrag = null;
  };
  const startFieldDrag = (event) => {
    const {name, option} = pendingFieldDrag;
    const rect = option.getBoundingClientRect();
    option.setPointerCapture(event.pointerId);
    draggedField = name;
    draggedOption = option;
    dragStartOrder = [...order];
    dragPlaceholder = document.createElement("div");
    dragPlaceholder.className = "admin-field-placeholder";
    dragPlaceholder.setAttribute("aria-hidden", "true");
    dragPlaceholder.style.height = `${rect.height}px`;
    option.before(dragPlaceholder);
    option.style.position = "fixed";
    option.style.top = `${rect.top}px`;
    option.style.left = `${rect.left}px`;
    option.style.width = `${rect.width}px`;
    option.classList.add("is-dragging");
    document.body.classList.add("admin-field-dragging");
    pendingFieldDrag.offsetY = event.clientY - rect.top;
    pendingFieldDrag.height = rect.height;
  };
  document.addEventListener("pointermove", (event) => {
    if (!pendingFieldDrag || event.pointerId !== pendingFieldDrag.pointerId) return;
    const {startX, startY, option} = pendingFieldDrag;
    if (!draggedField && Math.hypot(event.clientX - startX, event.clientY - startY) < 6) return;
    event.preventDefault();
    if (!draggedField) startFieldDrag(event);
    option.style.top = `${event.clientY - pendingFieldDrag.offsetY}px`;
    movePlaceholderTo(event.clientY - pendingFieldDrag.offsetY + pendingFieldDrag.height / 2);
  });
  document.addEventListener("pointerup", (event) => {
    if (pendingFieldDrag?.pointerId === event.pointerId) finishFieldDrag(Boolean(draggedField));
  });
  document.addEventListener("pointercancel", (event) => {
    if (pendingFieldDrag?.pointerId === event.pointerId) finishFieldDrag(false);
  });
  toggles.forEach((input) => {
    const name = input.dataset.fieldToggle;
    const option = fieldOptions.get(name);
    const handle = option?.querySelector("[data-field-reorder]");
    if (!option || !handle) return;
    handle.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const index = order.indexOf(name);
      const targetIndex = index + (event.key === "ArrowUp" ? -1 : 1);
      const target = order[targetIndex];
      if (target) placeField(name, target, event.key === "ArrowDown");
      handle.focus();
    });
    option.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.isPrimary === false) return;
      pendingFieldDrag = {
        name,
        option,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
      };
    });
  });

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
    if (popover.dataset.queryPanel === "group") {
      panel.style.maxHeight = `${Math.max(0, window.innerHeight - panel.getBoundingClientRect().top - 8)}px`;
    }
  };
  window.addEventListener("resize", () => {
    popovers.filter((popover) => popover.open).forEach(positionPanel);
  });
  popovers.forEach((popover) => {
    popover.querySelector(":scope > summary")?.addEventListener("click", () => {
      queueMicrotask(() => { if (popover.open) positionPanel(popover); });
    });
    popover.addEventListener("toggle", () => {
      if (popover.open) {
        positionPanel(popover);
        popovers.forEach((other) => {
          if (other !== popover) other.open = false;
        });
      } else {
        const panel = popover.querySelector(":scope > .admin-toolbar-panel");
        if (panel) {
          panel.style.maxWidth = "";
          panel.style.maxHeight = "";
        }
        popover.querySelectorAll("details[open]").forEach((nested) => { nested.open = false; });
      }
    });
  });
})();
