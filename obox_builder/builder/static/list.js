(() => {
  const form = document.querySelector("[data-bulk-form]");
  const table = document.querySelector("[data-list-table]");
  const status = document.querySelector("[data-selection-status]");
  const controls = document.querySelector("[data-bulk-controls]");
  const action = document.querySelector("[data-bulk-action]");
  const submit = document.querySelector("[data-bulk-submit]");
  const selectAll = document.querySelector("[data-select-all]");
  const selectAcross = document.querySelector("[data-select-across]");
  const clearSelection = document.querySelector("[data-clear-selection]");
  const acrossInput = form?.querySelector("[data-select-across-input]");
  if (
    !form ||
    !table ||
    !status ||
    !controls ||
    !action ||
    !submit ||
    !selectAll ||
    !selectAcross ||
    !clearSelection ||
    !acrossInput
  )
    return;

  const rows = [...table.querySelectorAll("[data-row-select]")];
  let allSelected = false;
  const update = () => {
    const selectable = rows.filter((input) => !input.disabled);
    const count = rows.filter((input) => input.checked).length;
    const pageCount = Number(status.dataset.pageCount);
    const totalCount = Number(status.dataset.totalCount);
    status.textContent = allSelected
      ? status.dataset.allSelected
      : `${count || pageCount} ${
          count
            ? count === 1
              ? status.dataset.selectedOne
              : status.dataset.selectedMany
            : pageCount === 1
              ? status.dataset.pageOne
              : status.dataset.pageMany
        }`;
    controls.hidden = !count;
    if (!count) action.value = "";
    submit.disabled = !count || !action.value;
    selectAll.disabled = !selectable.length;
    selectAll.checked = selectable.length > 0 && selectable.every((input) => input.checked);
    selectAll.indeterminate = count > 0 && !selectAll.checked;
    selectAcross.hidden = allSelected || !selectAll.checked || totalCount <= pageCount;
    clearSelection.hidden = !allSelected;
    acrossInput.value = String(Number(allSelected));
    rows.forEach((input) => {
      input.closest("tr").dataset.selected = String(input.checked);
    });
  };

  rows.forEach((input) => {
    input.addEventListener("change", () => {
      allSelected = false;
      update();
    });
  });
  selectAll.addEventListener("change", () => {
    allSelected = false;
    rows.forEach((input) => {
      if (!input.disabled) input.checked = selectAll.checked;
    });
    update();
  });
  selectAcross.addEventListener("click", () => {
    allSelected = true;
    update();
  });
  clearSelection.addEventListener("click", () => {
    allSelected = false;
    rows.forEach((input) => {
      input.checked = false;
    });
    update();
  });
  action.addEventListener("change", update);
  form.addEventListener("submit", (event) => {
    const count = allSelected
      ? Number(status.dataset.totalCount)
      : rows.filter((input) => input.checked).length;
    if (!count || action.value !== "delete") {
      event.preventDefault();
      return;
    }
    if (!window.confirm(count === 1 ? form.dataset.confirmOne : form.dataset.confirmMany)) {
      event.preventDefault();
    }
  });
  update();
})();

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
          ...readState(),
          hidden: toggles
            .filter((input) => !input.checked)
            .map((input) => input.dataset.fieldToggle),
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
    toggles.forEach((input) => {
      input.checked = false;
    });
    apply();
  });
  menu.querySelector("[data-show-all]")?.addEventListener("click", () => {
    toggles.forEach((input) => {
      input.checked = true;
    });
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
(() => {
  const menu = document.querySelector("[data-filter-menu]");
  const configNode = document.querySelector("[data-list-filter-config]");
  if (!menu || !configNode) return;
  const config = JSON.parse(configNode.textContent);
  const fields = config.fields;
  const byName = new Map(fields.map((field) => [field.name, field]));
  const list = menu.querySelector("[data-filter-conditions]");
  const count = menu.querySelector("[data-filter-count]");
  const output = menu.querySelector("[data-filter-input]");
  const emptyOps = new Set(["is_empty", "is_not_empty"]);
  const labels = {
    contains: "contains",
    does_not_contain: "does not contain",
    is: "is",
    is_not: "is not",
    is_empty: "is empty",
    is_not_empty: "is not empty",
  };
  const state = config.filter || { conjunction: "and", children: [] };
  const ids = new WeakMap();
  let nextId = 0;

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const idOf = (term) => {
    if (!ids.has(term)) ids.set(term, `filter-${++nextId}`);
    return ids.get(term);
  };
  const defaultOp = (spec) =>
    spec.operators.includes("contains") ? "contains" : spec.operators[0];
  const control = (label, options, value, className) => {
    const node = document.createElement("select");
    node.className = className;
    node.setAttribute("aria-label", label);
    options.forEach(([key, text]) => {
      node.add(new Option(text, key));
    });
    node.value = value;
    return node;
  };
  const svgIcon = (shapes) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.5");
    svg.setAttribute("aria-hidden", "true");
    shapes.forEach(([tag, attributes]) => {
      const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
      Object.entries(attributes).forEach(([name, value]) => {
        shape.setAttribute(name, value);
      });
      svg.append(shape);
    });
    return svg;
  };
  const trashIcon = () => svgIcon([["path", { d: "M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13" }]]);
  const plusIcon = () => svgIcon([["path", { d: "M12 5v14M5 12h14" }]]);
  const closeAddMenus = () => {
    document.querySelectorAll("[data-filter-add-menu]:not([hidden])").forEach((popover) => {
      popover.hidden = true;
      popover.parentElement
        ?.querySelector("[data-filter-add-toggle]")
        ?.setAttribute("aria-expanded", "false");
    });
  };
  const saved = (node) => ({
    conjunction: node.conjunction,
    children: node.children.flatMap((child) => {
      if (child.children) {
        const group = saved(child);
        return group.children.length ? [group] : [];
      }
      return child.draft ? [] : [{ field: child.field, op: child.op, value: child.value }];
    }),
  });
  const countTerms = (node) =>
    node.children.reduce(
      (total, child) => total + (child.children ? countTerms(child) : child.draft ? 0 : 1),
      0,
    );
  const serialize = () => {
    const total = countTerms(state);
    if (output) {
      output.value = total ? JSON.stringify(saved(state)) : "";
      output.disabled = total === 0;
    }
    if (count) {
      count.textContent = String(total);
      count.hidden = total === 0;
    }
  };

  if (!state.children.length && fields.length) {
    state.children.push({
      field: fields[0].name,
      op: defaultOp(fields[0]),
      value: "",
      draft: true,
    });
  }

  const addTerm = (node) => {
    const spec = fields[0];
    if (!spec) return;
    const term = { field: spec.name, op: defaultOp(spec), value: "", draft: true };
    node.children.push(term);
    render();
    list.querySelector(`[data-filter-term="${idOf(term)}"] select[data-filter-field]`)?.focus();
  };
  const addGroup = (node) => {
    const child = { conjunction: "and", children: [] };
    node.children.push(child);
    render();
    list.querySelector(`[data-filter-group-id="${idOf(child)}"] [data-filter-add-toggle]`)?.focus();
  };

  const group = (node, depth, parent, index) => {
    const box = make("div", depth === 1 ? "filter-group" : "filter-group filter-group-nested");
    box.dataset.filterGroup = "";
    const groupId = idOf(node);
    box.dataset.filterGroupId = groupId;
    if (depth > 1) {
      const header = make("div", "filter-group-header");
      header.append(
        make(
          "p",
          "filter-group-heading",
          node.conjunction === "and"
            ? "All of the following are true..."
            : "Any of the following are true...",
        ),
      );
      const tools = make("div", "filter-group-tools");
      const add = make("div", "filter-add");
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "filter-add-icon";
      toggle.dataset.filterAddToggle = "";
      toggle.setAttribute("aria-label", "Add to group");
      toggle.setAttribute("aria-expanded", "false");
      toggle.append(plusIcon());
      const addMenu = make("div", "filter-add-menu");
      addMenu.dataset.filterAddMenu = "";
      addMenu.hidden = true;
      const item = (label, action, disabled = false) => {
        const button = make("button", "filter-add-menu-item", label);
        button.type = "button";
        button.disabled = disabled;
        button.addEventListener("click", () => {
          closeAddMenus();
          action();
        });
        return button;
      };
      addMenu.append(
        item("Add condition", () => addTerm(node)),
        item("Add condition group", () => addGroup(node), depth >= 3),
      );
      toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        const open = addMenu.hidden;
        closeAddMenus();
        addMenu.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
      });
      add.append(toggle, addMenu);
      tools.append(add);
      if (parent) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "filter-remove";
        remove.setAttribute("aria-label", "Delete group");
        remove.append(trashIcon());
        remove.addEventListener("click", () => {
          parent.children.splice(parent.children.indexOf(node), 1);
          render();
          list.querySelector("[data-filter-add]")?.focus();
        });
        tools.append(remove);
      }
      header.append(tools);
      box.append(header);
    }
    const children = make("div", "filter-conditions");
    node.children.forEach((child, childIndex) => {
      children.append(
        child.children
          ? group(child, depth + 1, node, childIndex)
          : condition(child, node, childIndex),
      );
    });
    if (!node.children.length) children.append(make("p", "filter-empty", "No conditions yet."));
    box.append(children);

    if (depth === 1) {
      const actions = make("div", "filter-group-actions");
      const addCondition = make("button", "filter-add", "＋ Add condition");
      addCondition.type = "button";
      addCondition.dataset.filterAdd = "";
      addCondition.addEventListener("click", () => addTerm(node));
      const addGroupButton = make("button", "filter-add", "＋ Add group");
      addGroupButton.type = "button";
      addGroupButton.addEventListener("click", () => addGroup(node));
      actions.append(addCondition, addGroupButton);
      box.append(actions);
    }

    if (parent && index > 0) {
      const row = make("div", "filter-group-row");
      if (index > 1) {
        row.append(make("span", "filter-join filter-join-static", parent.conjunction));
      } else {
        const join = control(
          "Group join",
          [
            ["and", "and"],
            ["or", "or"],
          ],
          parent.conjunction,
          "filter-join",
        );
        join.dataset.filterGroupJoin = groupId;
        join.addEventListener("change", () => {
          parent.conjunction = join.value;
          render();
          list.querySelector(`[data-filter-group-join="${groupId}"]`)?.focus();
        });
        row.append(join);
      }
      row.append(box);
      return row;
    }
    return box;
  };

  const condition = (term, parent, index) => {
    const id = idOf(term);
    const spec = byName.get(term.field);
    if (!spec) return make("p", "filter-empty", "Unknown field.");
    const row = make("div", "filter-condition");
    row.dataset.filterTerm = id;

    if (index === 0) {
      row.append(make("span", "filter-join", "Where"));
    } else if (index > 1) {
      row.append(make("span", "filter-join filter-join-static", parent.conjunction));
    } else {
      const join = control(
        "Condition join",
        [
          ["and", "and"],
          ["or", "or"],
        ],
        parent.conjunction,
        "filter-join",
      );
      join.addEventListener("change", () => {
        parent.conjunction = join.value;
        render();
        list
          .querySelector(`[data-filter-term="${id}"] select[aria-label="Condition join"]`)
          ?.focus();
      });
      row.append(join);
    }

    const field = document.createElement("select");
    field.dataset.filterField = term.field;
    field.setAttribute("aria-label", "Filter field");
    fields.forEach((option) => {
      field.add(new Option(option.label, option.name));
    });
    field.value = term.field;
    field.addEventListener("change", () => {
      term.field = field.value;
      const next = byName.get(term.field);
      term.op = defaultOp(next);
      term.value = "";
      term.draft = true;
      render();
      list.querySelector(`[data-filter-term="${id}"] select[data-filter-field]`)?.focus();
    });
    row.append(field);

    const op = document.createElement("select");
    op.dataset.filterOp = term.field;
    op.setAttribute("aria-label", "Filter operator");
    spec.operators.forEach((name) => {
      op.add(new Option(labels[name], name));
    });
    op.value = term.op;
    op.addEventListener("change", () => {
      term.op = op.value;
      term.value = "";
      term.draft = !emptyOps.has(term.op);
      render();
      list.querySelector(`[data-filter-term="${id}"] select[data-filter-op]`)?.focus();
    });
    row.append(op);

    if (!emptyOps.has(term.op)) {
      let value;
      if (spec.kind === "choice" || spec.kind === "boolean") {
        value = document.createElement("select");
        value.add(new Option("", ""));
        const choices =
          spec.kind === "boolean"
            ? [
                ["true", "Yes"],
                ["false", "No"],
              ]
            : spec.choices.map((item) => [item, item]);
        choices.forEach(([key, label]) => {
          value.add(new Option(label, key));
        });
        value.value = term.value ?? "";
        value.addEventListener("change", () => {
          term.value = value.value;
          term.draft = value.value === "";
          serialize();
        });
      } else {
        value = document.createElement("input");
        value.type = { number: "number", date: "date", time: "time" }[spec.kind] || "text";
        if (spec.kind === "number") value.step = "any";
        value.value = term.value ?? "";
        value.placeholder = spec.kind === "datetime" ? "YYYY-MM-DDTHH:MM:SS" : "Enter a value";
        value.addEventListener("input", () => {
          term.value = value.value;
          term.draft = value.value === "";
          serialize();
        });
      }
      value.dataset.filterValueControl = "";
      value.setAttribute("aria-label", "Filter value");
      row.append(value);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "filter-remove";
    remove.setAttribute("aria-label", "Delete condition");
    remove.append(trashIcon());
    remove.addEventListener("click", () => {
      parent.children.splice(parent.children.indexOf(term), 1);
      render();
      list.querySelector("[data-filter-add]")?.focus();
    });
    row.append(remove);
    return row;
  };

  const render = () => {
    list.replaceChildren(group(state, 1, null));
    serialize();
  };

  document.addEventListener("click", (event) => {
    closeAddMenus();
    // The target may be detached after a re-render; the captured path still includes the menu.
    if (menu.open && !event.composedPath().includes(menu)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeAddMenus();
    if (!menu.open) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });
  render();
})();
(() => {
  const menu = document.querySelector("[data-sort-menu]");
  const configNode = document.querySelector("[data-list-sort-config]");
  if (!menu || !configNode) return;
  const config = JSON.parse(configNode.textContent);
  const specs = new Map(config.fields.map((spec) => [spec.name, spec]));
  const rows = menu.querySelector("[data-sort-rows]");
  const picker = menu.querySelector("[data-sort-picker]");
  const search = menu.querySelector("[data-sort-search]");
  const fields = menu.querySelector("[data-sort-fields]");
  const add = menu.querySelector("[data-sort-add]");
  const input = menu.querySelector("[data-sort-input]");
  const cancel = menu.querySelector("[data-sort-cancel]");
  const footer = menu.querySelector(".sort-footer");
  const applied = () => config.sort.map((term) => ({ ...term }));
  let terms = applied();
  let picking = !terms.length;
  let query = "";

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const svgIcon = (shapes, fill = "none") => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", fill);
    svg.setAttribute("stroke", fill === "none" ? "currentColor" : "none");
    svg.setAttribute("stroke-width", "1.5");
    svg.setAttribute("aria-hidden", "true");
    for (const [tag, attributes] of shapes) {
      const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, value);
      svg.append(shape);
    }
    return svg;
  };
  const xIcon = () => svgIcon([["path", { d: "m6 6 12 12M18 6 6 18" }]]);
  const gripIcon = () =>
    svgIcon(
      [
        ["circle", { cx: "9", cy: "6", r: "1.5" }],
        ["circle", { cx: "15", cy: "6", r: "1.5" }],
        ["circle", { cx: "9", cy: "12", r: "1.5" }],
        ["circle", { cx: "15", cy: "12", r: "1.5" }],
        ["circle", { cx: "9", cy: "18", r: "1.5" }],
        ["circle", { cx: "15", cy: "18", r: "1.5" }],
      ],
      "currentColor",
    );
  const available = () =>
    [...specs.keys()].filter((name) => !terms.some((term) => term.field === name));
  const directions = (kind) =>
    ({
      number: ["1 → 9", "9 → 1"],
      date: ["Earliest → Latest", "Latest → Earliest"],
      datetime: ["Earliest → Latest", "Latest → Earliest"],
      time: ["Earliest → Latest", "Latest → Earliest"],
      boolean: ["Unchecked → Checked", "Checked → Unchecked"],
      choice: ["First → Last", "Last → First"],
    })[kind] || ["A → Z", "Z → A"];

  const renderPicker = () => {
    picker.hidden = !picking;
    if (!picking) return;
    fields.replaceChildren();
    const matches = available().filter((name) =>
      specs.get(name).label.toLowerCase().includes(query),
    );
    matches.forEach((name) => {
      const button = make("button", "sort-field", specs.get(name).label);
      button.type = "button";
      button.addEventListener("click", () => {
        terms.push({ field: name, descending: false });
        picking = false;
        query = "";
        search.value = "";
        render();
        rows.querySelector(`[data-sort-index="${terms.length - 1}"] select`)?.focus();
      });
      fields.append(button);
    });
    if (!matches.length) fields.append(make("p", "sort-empty", "No fields found."));
  };

  const reset = () => {
    terms = applied();
    picking = !terms.length;
    query = "";
    search.value = "";
    render();
  };
  const serialize = () => {
    input.value = terms.map((term) => `${term.descending ? "-" : ""}${term.field}`).join(",");
    input.disabled = terms.length === 0;
  };
  const move = (from, to) => {
    if (from < 0 || to < 0 || from === to || to >= terms.length) return;
    const [term] = terms.splice(from, 1);
    terms.splice(to, 0, term);
    render();
    rows.querySelector(`[data-sort-index="${to}"] .sort-grip`)?.focus();
  };

  const render = () => {
    rows.replaceChildren();
    terms.forEach((term, index) => {
      const spec = specs.get(term.field);
      if (!spec) return;
      const row = make("div", "sort-row");
      row.dataset.sortIndex = index;

      const field = document.createElement("select");
      field.setAttribute("aria-label", `Sort field ${index + 1}`);
      specs.forEach((option) => {
        if (
          option.name !== term.field &&
          terms.some((other, otherIndex) => otherIndex !== index && other.field === option.name)
        ) {
          return;
        }
        field.add(new Option(option.label, option.name));
      });
      field.value = term.field;
      field.addEventListener("change", () => {
        term.field = field.value;
        term.descending = false;
        render();
        rows.querySelector(`[data-sort-index="${index}"] select`)?.focus();
      });
      row.append(field);

      const direction = document.createElement("select");
      direction.setAttribute("aria-label", `Sort direction for ${spec.label}`);
      const labels = directions(spec.kind);
      direction.add(new Option(labels[0], "asc"));
      direction.add(new Option(labels[1], "desc"));
      direction.value = term.descending ? "desc" : "asc";
      direction.addEventListener("change", () => {
        term.descending = direction.value === "desc";
        serialize();
      });
      row.append(direction);

      const remove = make("button", "sort-icon");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${spec.label} sort`);
      remove.append(xIcon());
      remove.addEventListener("click", () => {
        terms.splice(index, 1);
        picking = !terms.length;
        render();
        (picking ? search : add).focus();
      });
      row.append(remove);

      const grip = make("button", "sort-icon sort-grip");
      grip.type = "button";
      grip.setAttribute("aria-label", `Reorder ${spec.label} sort`);
      grip.title = "Drag or use Alt+Up/Down";
      grip.append(gripIcon());
      grip.draggable = true;
      grip.addEventListener("keydown", (event) => {
        if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        move(index, index + (event.key === "ArrowUp" ? -1 : 1));
      });
      grip.addEventListener("dragstart", (event) => {
        event.dataTransfer.setData("text/plain", String(index));
        event.dataTransfer.effectAllowed = "move";
      });
      row.append(grip);
      row.addEventListener("dragover", (event) => event.preventDefault());
      row.addEventListener("drop", (event) => {
        event.preventDefault();
        move(Number(event.dataTransfer.getData("text/plain")), index);
      });
      rows.append(row);
    });

    add.hidden = picking || terms.length >= 5 || !available().length;
    footer.hidden = !terms.length && !config.sort.length;
    renderPicker();
    serialize();
  };

  add.addEventListener("click", () => {
    picking = true;
    query = "";
    search.value = "";
    render();
    search.focus();
  });
  search.addEventListener("input", () => {
    query = search.value.trim().toLowerCase();
    renderPicker();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    fields.querySelector("button")?.click();
  });
  cancel.addEventListener("click", () => {
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });

  menu.addEventListener("toggle", () => {
    if (!menu.open) return reset();
    if (picking) search.focus();
  });

  document.addEventListener("click", (event) => {
    if (menu.open && !event.composedPath().includes(menu)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });
  render();
})();
(() => {
  const menu = document.querySelector("[data-group-menu]");
  const configNode = document.querySelector("[data-list-group-config]");
  if (!menu || !configNode) return;
  const form = menu.closest("form");
  const config = JSON.parse(configNode.textContent);
  const specs = new Map(config.fields.map((spec) => [spec.name, spec]));
  const rows = menu.querySelector("[data-group-rows]");
  const picker = menu.querySelector("[data-group-picker]");
  const search = menu.querySelector("[data-group-search]");
  const fields = menu.querySelector("[data-group-fields]");
  const add = menu.querySelector("[data-group-add]");
  const input = menu.querySelector("[data-group-input]");
  const count = menu.querySelector("[data-group-count]");
  const terms = config.group.map((term) => ({ ...term }));
  let picking = !terms.length;
  let query = "";

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const svgIcon = (shapes, fill = "none") => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", fill);
    svg.setAttribute("stroke", fill === "none" ? "currentColor" : "none");
    svg.setAttribute("stroke-width", "1.5");
    svg.setAttribute("aria-hidden", "true");
    for (const [tag, attributes] of shapes) {
      const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, value);
      svg.append(shape);
    }
    return svg;
  };
  const xIcon = () => svgIcon([["path", { d: "m6 6 12 12M18 6 6 18" }]]);
  const gripIcon = () =>
    svgIcon(
      [
        ["circle", { cx: "9", cy: "6", r: "1.5" }],
        ["circle", { cx: "15", cy: "6", r: "1.5" }],
        ["circle", { cx: "9", cy: "12", r: "1.5" }],
        ["circle", { cx: "15", cy: "12", r: "1.5" }],
        ["circle", { cx: "9", cy: "18", r: "1.5" }],
        ["circle", { cx: "15", cy: "18", r: "1.5" }],
      ],
      "currentColor",
    );
  const available = () =>
    [...specs.keys()].filter((name) => !terms.some((term) => term.field === name));
  const directions = (kind) =>
    ({
      number: ["1 → 9", "9 → 1"],
      date: ["Earliest → Latest", "Latest → Earliest"],
      datetime: ["Earliest → Latest", "Latest → Earliest"],
      time: ["Earliest → Latest", "Latest → Earliest"],
      boolean: ["Unchecked → Checked", "Checked → Unchecked"],
      choice: ["First → Last", "Last → First"],
    })[kind] || ["A → Z", "Z → A"];

  const serialize = () => {
    input.value = terms.map((term) => `${term.descending ? "-" : ""}${term.field}`).join(",");
    input.disabled = !terms.length;
    count.textContent = terms.length;
    count.hidden = !terms.length;
  };
  const submit = () => {
    try {
      sessionStorage.setItem("obox-group-open", "1");
    } catch {}
    form.requestSubmit();
  };
  const renderPicker = () => {
    picker.hidden = !picking;
    if (!picking) return;
    fields.replaceChildren();
    const matches = available().filter((name) =>
      specs.get(name).label.toLowerCase().includes(query),
    );
    matches.forEach((name) => {
      const button = make("button", "sort-field", specs.get(name).label);
      button.type = "button";
      button.addEventListener("click", () => {
        terms.push({ field: name, descending: false });
        picking = false;
        query = "";
        search.value = "";
        render();
        submit();
      });
      fields.append(button);
    });
    if (!matches.length) fields.append(make("p", "sort-empty", "No fields found."));
  };
  const move = (from, to) => {
    if (from < 0 || to < 0 || from === to || to >= terms.length) return;
    const [term] = terms.splice(from, 1);
    terms.splice(to, 0, term);
    render();
    submit();
  };

  const render = () => {
    rows.replaceChildren();
    terms.forEach((term, index) => {
      const spec = specs.get(term.field);
      if (!spec) return;
      const row = make("div", "sort-row");
      row.dataset.groupIndex = index;

      const field = document.createElement("select");
      field.setAttribute("aria-label", `Group field ${index + 1}`);
      specs.forEach((option) => {
        if (
          option.name !== term.field &&
          terms.some((other, otherIndex) => otherIndex !== index && other.field === option.name)
        ) {
          return;
        }
        field.add(new Option(option.label, option.name));
      });
      field.value = term.field;
      field.addEventListener("change", () => {
        term.field = field.value;
        term.descending = false;
        render();
        submit();
      });
      row.append(field);

      const direction = document.createElement("select");
      direction.setAttribute("aria-label", `Group direction for ${spec.label}`);
      const labels = directions(spec.kind);
      direction.add(new Option(labels[0], "asc"));
      direction.add(new Option(labels[1], "desc"));
      direction.value = term.descending ? "desc" : "asc";
      direction.addEventListener("change", () => {
        term.descending = direction.value === "desc";
        serialize();
        submit();
      });
      row.append(direction);

      const remove = make("button", "sort-icon");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${spec.label} grouping`);
      remove.append(xIcon());
      remove.addEventListener("click", () => {
        terms.splice(index, 1);
        picking = !terms.length;
        render();
        submit();
      });
      row.append(remove);

      const grip = make("button", "sort-icon sort-grip");
      grip.type = "button";
      grip.setAttribute("aria-label", `Reorder ${spec.label} grouping`);
      grip.title = "Drag or use Alt+Up/Down";
      grip.append(gripIcon());
      grip.draggable = true;
      grip.addEventListener("keydown", (event) => {
        if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        move(index, index + (event.key === "ArrowUp" ? -1 : 1));
      });
      grip.addEventListener("dragstart", (event) => {
        event.dataTransfer.setData("text/plain", String(index));
        event.dataTransfer.effectAllowed = "move";
      });
      row.append(grip);
      row.addEventListener("dragover", (event) => event.preventDefault());
      row.addEventListener("drop", (event) => {
        event.preventDefault();
        move(Number(event.dataTransfer.getData("text/plain")), index);
      });
      rows.append(row);
    });

    add.hidden = picking || terms.length >= 3 || !available().length;
    renderPicker();
    serialize();
  };

  add.addEventListener("click", () => {
    picking = true;
    query = "";
    search.value = "";
    render();
    search.focus();
  });
  search.addEventListener("input", () => {
    query = search.value.trim().toLowerCase();
    renderPicker();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    fields.querySelector("button")?.click();
  });
  menu.addEventListener("toggle", () => {
    if (menu.open && picking) search.focus();
  });
  document.addEventListener("click", (event) => {
    if (menu.open && !event.composedPath().includes(menu)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });

  render();
  try {
    if (sessionStorage.getItem("obox-group-open") === "1") {
      sessionStorage.removeItem("obox-group-open");
      menu.open = true;
      if (picking) requestAnimationFrame(() => search.focus());
    }
  } catch {}

  const table = document.querySelector("[data-list-table]");
  if (!table) return;
  const collapsed = new Set();
  const sync = () => {
    const path = [];
    for (const row of table.tBodies[0]?.rows || []) {
      if (!row.dataset.groupHeader) {
        row.hidden = path.some((id) => collapsed.has(id));
        continue;
      }
      path.length = Number(row.dataset.groupHeader) - 1;
      row.hidden = path.some((id) => collapsed.has(id));
      row
        .querySelector("[data-group-toggle]")
        ?.setAttribute("aria-expanded", String(!collapsed.has(row.dataset.groupId)));
      path.push(row.dataset.groupId);
    }
  };
  table.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-group-toggle]");
    if (!toggle) return;
    const id = toggle.closest("tr").dataset.groupId;
    if (collapsed.has(id)) collapsed.delete(id);
    else collapsed.add(id);
    sync();
  });
  sync();
})();
(() => {
  const menu = document.querySelector("[data-row-height-panel]");
  const table = document.querySelector("[data-list-table]");
  if (!menu || !table) return;
  const storageKey = `obox-list:${table.dataset.listKey || window.location.pathname}`;
  const choices = [...menu.querySelectorAll("[data-row-height-choice]")];
  const wrap = menu.querySelector("[data-wrap-headers]");

  const readState = () => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (Array.isArray(value)) return { hidden: value, order: [] };
      return value && typeof value === "object" ? value : {};
    } catch {
      return {};
    }
  };
  const saveState = () => {
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({
          ...readState(),
          rowHeight: table.dataset.rowHeight,
          wrapHeaders: table.dataset.wrapHeaders === "true",
        }),
      );
    } catch {
      // Keep the controls usable when browser storage is unavailable.
    }
  };
  const setHeight = (height) => {
    if (!choices.some((button) => button.dataset.rowHeightChoice === height)) height = "medium";
    table.dataset.rowHeight = height;
    choices.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.rowHeightChoice === height));
    });
  };
  const setWrapHeaders = (wrapHeaders) => {
    table.dataset.wrapHeaders = String(wrapHeaders);
    wrap?.setAttribute("aria-pressed", String(wrapHeaders));
  };

  const state = readState();
  setHeight(state.rowHeight || "medium");
  setWrapHeaders(state.wrapHeaders === true);
  choices.forEach((button) => {
    button.addEventListener("click", () => {
      setHeight(button.dataset.rowHeightChoice);
      saveState();
    });
  });
  wrap?.addEventListener("click", () => {
    setWrapHeaders(table.dataset.wrapHeaders !== "true");
    saveState();
  });
  document.addEventListener("click", (event) => {
    if (menu.open && !event.composedPath().includes(menu)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary")?.focus();
  });
})();
(() => {
  const table = document.querySelector("[data-list-table]");
  const toggle = document.querySelector("[data-search-toggle]");
  const panel = document.querySelector("[data-search-panel]");
  if (!table || !toggle || !panel) return;

  const input = panel.querySelector("[data-search-input]");
  const count = panel.querySelector("[data-search-count]");
  const previous = panel.querySelector("[data-search-prev]");
  const next = panel.querySelector("[data-search-next]");
  const close = panel.querySelector("[data-search-close]");
  const emptyLabel = panel.dataset.searchEmpty || "No matches";
  const ofLabel = panel.dataset.searchOf || "of";
  let matches = [];
  let active = -1;

  const fold = (text) => {
    let folded = "";
    const positions = [];
    for (let index = 0; index < text.length; index += 1) {
      const part = text[index]
        .normalize("NFD")
        .replace(/\p{M}/gu, "")
        .toLocaleLowerCase()
        .replace(/đ/g, "d");
      folded += part;
      for (let offset = 0; offset < part.length; offset += 1) positions.push(index);
    }
    return { folded, positions };
  };

  const clear = () => {
    for (const mark of table.querySelectorAll("mark[data-search-mark]")) {
      const value = mark.closest(".cell-value");
      if (value) value.replaceChildren(value.textContent);
    }
    table.querySelectorAll("[data-search-current]").forEach((cell) => {
      cell.removeAttribute("data-search-current");
    });
    matches = [];
    active = -1;
  };

  const highlight = (cell, needle) => {
    const value = cell.querySelector(".cell-value");
    if (!value) return false;
    const text = value.textContent || "";
    const { folded, positions } = fold(text);
    const ranges = [];
    let index = folded.indexOf(needle);
    while (index !== -1) {
      const end = index + needle.length;
      let to = positions[end - 1] + 1;
      while (to < text.length && /\p{M}/u.test(text[to])) to += 1;
      ranges.push([positions[index], to]);
      index = folded.indexOf(needle, end);
    }
    if (!ranges.length) return false;

    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const [from, to] of ranges) {
      if (from > cursor) fragment.append(text.slice(cursor, from));
      const mark = document.createElement("mark");
      mark.dataset.searchMark = "";
      mark.textContent = text.slice(from, to);
      fragment.append(mark);
      cursor = to;
    }
    if (cursor < text.length) fragment.append(text.slice(cursor));
    value.replaceChildren(fragment);
    return true;
  };

  const show = (index, scroll = false) => {
    active = matches.length ? (index + matches.length) % matches.length : -1;
    table.querySelectorAll("[data-search-current]").forEach((cell) => {
      cell.removeAttribute("data-search-current");
    });
    const cell = matches[active];
    if (cell) {
      cell.dataset.searchCurrent = "";
      if (scroll) cell.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    if (!input.value.trim()) count.textContent = `0 ${ofLabel} 0`;
    else if (!matches.length) count.textContent = emptyLabel;
    else count.textContent = `${active + 1} ${ofLabel} ${matches.length}`;
    previous.disabled = !matches.length;
    next.disabled = !matches.length;
  };

  const run = () => {
    const needle = fold(input.value.trim()).folded;
    clear();
    if (needle) {
      for (const row of table.querySelectorAll("tr[data-group-row]:not([hidden])")) {
        for (const cell of row.querySelectorAll("td[data-list-column]:not([hidden])")) {
          if (highlight(cell, needle)) matches.push(cell);
        }
      }
    }
    show(0, Boolean(needle));
  };

  const open = () => {
    panel.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    input.focus();
    input.select();
    if (input.value.trim()) run();
  };

  const hide = () => {
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    input.value = "";
    run();
    toggle.focus();
  };

  toggle.addEventListener("click", () => (panel.hidden ? open() : hide()));
  close.addEventListener("click", hide);
  previous.addEventListener("click", () => show(active - 1, true));
  next.addEventListener("click", () => show(active + 1, true));
  input.addEventListener("input", (event) => {
    if (!event.isComposing) run();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      hide();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (matches.length) show(active + (event.shiftKey ? -1 : 1), true);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || panel.hidden) return;
    event.preventDefault();
    hide();
  });

  const fieldMenu = document.querySelector("[data-field-menu]");
  fieldMenu?.addEventListener("change", () => {
    if (!panel.hidden && input.value.trim()) run();
  });
  fieldMenu?.addEventListener("click", (event) => {
    if (event.target.closest("[data-hide-all], [data-show-all]") && !panel.hidden) run();
  });
  table.addEventListener("click", (event) => {
    if (event.target.closest("[data-group-toggle]") && !panel.hidden) run();
  });
})();
