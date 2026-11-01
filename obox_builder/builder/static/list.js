(() => {
  const menu = document.querySelector("[data-field-menu]");
  const table = document.querySelector("[data-list-table]");
  if (!menu || !table) return;

  const toggles = [...menu.querySelectorAll("[data-field-toggle]")];
  const fieldList = menu.querySelector(".field-menu-list");
  const storageKey = `obox-list:${table.dataset.listKey || window.location.pathname}`;
  const fieldNames = toggles.map((input) => input.dataset.fieldToggle);
  const fieldOptions = new Map(
    toggles.map((input) => [input.dataset.fieldToggle, input.closest("[data-field-option]")]),
  );

  const readState = () => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (Array.isArray(value)) return { hidden: value, order: [] };
      return value && typeof value === "object" ? value : {};
    } catch {
      return {};
    }
  };

  const state = readState();
  const hidden = new Set(Array.isArray(state.hidden) ? state.hidden : []);
  const storedOrder = Array.isArray(state.order) ? state.order : [];
  let order = [
    ...storedOrder.filter(
      (name, index) => fieldOptions.has(name) && storedOrder.indexOf(name) === index,
    ),
    ...fieldNames.filter((name) => !storedOrder.includes(name)),
  ];

  const saveState = () => {
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({
          hidden: toggles.filter((input) => !input.checked).map((input) => input.dataset.fieldToggle),
          order,
        }),
      );
    } catch {
      // Keep the controls usable when browser storage is unavailable.
    }
  };

  const applyOrder = () => {
    for (const row of table.querySelectorAll("tr")) {
      const cells = [...row.querySelectorAll(":scope > [data-list-column]")];
      if (!cells.length) continue;
      const byName = new Map(cells.map((cell) => [cell.dataset.listColumn, cell]));
      const marker = document.createComment("");
      cells[0].before(marker);
      for (const name of [...order, ...cells.map((cell) => cell.dataset.listColumn)]) {
        const cell = byName.get(name);
        if (!cell) continue;
        marker.before(cell);
        byName.delete(name);
      }
      marker.remove();
    }
  };

  const renderOrder = () => {
    const empty = fieldList.querySelector("[data-field-empty]");
    for (const name of order) {
      const option = fieldOptions.get(name);
      if (option) fieldList.insertBefore(option, empty);
    }
  };

  const apply = () => {
    applyOrder();
    const hiddenNow = new Set(
      toggles.filter((input) => !input.checked).map((input) => input.dataset.fieldToggle),
    );
    table.querySelectorAll("[data-list-column]").forEach((cell) => {
      cell.hidden = hiddenNow.has(cell.dataset.listColumn);
    });
    saveState();
  };

  toggles.forEach((input) => {
    input.checked = !hidden.has(input.dataset.fieldToggle);
    input.addEventListener("change", apply);
  });
  renderOrder();
  apply();

  menu.querySelector("[data-hide-all]")?.addEventListener("click", () => {
    toggles.forEach((input) => { input.checked = false; });
    apply();
  });
  menu.querySelector("[data-show-all]")?.addEventListener("click", () => {
    toggles.forEach((input) => { input.checked = true; });
    apply();
  });

  const search = menu.querySelector("[data-field-search]");
  const empty = menu.querySelector("[data-field-empty]");
  search?.addEventListener("input", () => {
    const query = search.value.trim().toLocaleLowerCase();
    const options = [...menu.querySelectorAll("[data-field-option]")];
    options.forEach((option) => {
      option.hidden = !option.dataset.fieldLabel.includes(query);
    });
    if (empty) empty.hidden = options.some((option) => !option.hidden);
  });

  let dragging = null;
  let dragOrder = null;

  const stopDragging = () => {
    if (!dragging) return;
    dragging.option.classList.remove("is-dragging");
    document.body.classList.remove("field-menu-dragging");
    document.removeEventListener("pointermove", moveField);
    document.removeEventListener("pointerup", finishField);
    document.removeEventListener("pointercancel", cancelField);
    dragging = null;
  };

  const moveField = (event) => {
    if (!dragging || event.pointerId !== dragging.pointerId) return;
    const target = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest("[data-field-option]");
    if (!target || target === dragging.option) return;
    const rect = target.getBoundingClientRect();
    fieldList.insertBefore(
      dragging.option,
      event.clientY > rect.top + rect.height / 2 ? target.nextSibling : target,
    );
    order = [...fieldList.querySelectorAll("[data-field-option]")].map(
      (option) => option.dataset.fieldOption,
    );
  };

  const finishField = (event) => {
    if (!dragging || event.pointerId !== dragging.pointerId) return;
    stopDragging();
    apply();
  };

  const cancelField = (event) => {
    if (!dragging || event.pointerId !== dragging.pointerId) return;
    order = [...dragOrder];
    stopDragging();
    renderOrder();
    apply();
  };

  toggles.forEach((input) => {
    const name = input.dataset.fieldToggle;
    const option = fieldOptions.get(name);
    const handle = option?.querySelector("[data-field-reorder]");
    if (!option || !handle) return;
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.isPrimary === false) return;
      event.preventDefault();
      handle.focus();
      dragging = { option, pointerId: event.pointerId };
      dragOrder = [...order];
      option.classList.add("is-dragging");
      document.body.classList.add("field-menu-dragging");
      document.addEventListener("pointermove", moveField);
      document.addEventListener("pointerup", finishField);
      document.addEventListener("pointercancel", cancelField);
    });
    handle.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const index = order.indexOf(name);
      const targetIndex = index + (event.key === "ArrowUp" ? -1 : 1);
      if (targetIndex < 0 || targetIndex >= order.length) return;
      [order[index], order[targetIndex]] = [order[targetIndex], order[index]];
      renderOrder();
      apply();
      handle.focus();
    });
  });

  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });
})();
