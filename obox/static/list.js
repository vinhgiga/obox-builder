(() => {
  const table = document.querySelector("[data-list-table]");
  if (!table) return;

  const menus = [...document.querySelectorAll(".grid-tools details.field-menu")];
  const closeMenus = () =>
    menus.forEach((menu) => {
      menu.open = false;
    });
  menus.forEach((menu) => {
    menu.addEventListener("toggle", () => {
      if (menu.open)
        menus.forEach((other) => {
          if (other !== menu) other.open = false;
        });
    });
  });
  document.addEventListener("click", (event) => {
    const insideToolMenu = event
      .composedPath()
      .some((node) => node instanceof Element && node.matches(".grid-tools .field-menu"));
    if (!insideToolMenu) closeMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenus();
  });

  const body = table.tBodies[0];
  const originalRows = [...body.querySelectorAll("tr[data-grid-row]")];
  const originalEmptyRow = body.querySelector(".grid-empty")?.closest("tr") || null;
  const selectionStatus = document.querySelector("[data-selection-status]");
  const bulkControls = document.querySelector("[data-bulk-controls]");
  const bulkAction = document.querySelector("[data-bulk-action]");
  const bulkSubmit = document.querySelector("[data-bulk-submit]");
  const selectAll = table.querySelector("[data-select-all]");
  const clearSelection = document.querySelector("[data-clear-selection]");
  const rowChecks = [...table.querySelectorAll("[data-row-select]")];
  const storageKey = `obox-list:${table.dataset.listKey || window.location.pathname}`;
  const readState = () => {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (Array.isArray(stored)) return { hidden: stored, order: [] };
      return stored && typeof stored === "object" ? stored : {};
    } catch {
      return {};
    }
  };
  const initialState = readState();
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const readConfig = (selector, fallback) => {
    const node = document.querySelector(selector);
    try {
      return node ? JSON.parse(node.textContent) : fallback;
    } catch {
      return fallback;
    }
  };
  const filterConfig = readConfig("[data-list-filter-config]", {
    fields: [],
    filter: { conjunction: "and", children: [] },
  });
  const sortConfig = readConfig("[data-list-sort-config]", { fields: [], sort: [] });
  const groupConfig = readConfig("[data-list-group-config]", { fields: [], group: [] });
  if (filterConfig.fallback) {
    filterConfig.fields.forEach((field) => {
      const values = [
        ...table.querySelectorAll(
          `tbody tr[data-grid-row] [data-list-column="${CSS.escape(field.name)}"]`,
        ),
      ]
        .map((cell) => cell.innerText.trim())
        .filter(Boolean);
      if (!values.length) return;
      const lower = values.map((value) => value.toLocaleLowerCase());
      const choices = [...new Set(values)];
      if (lower.every((value) => ["true", "false", "yes", "no"].includes(value)))
        field.kind = "boolean";
      else if (
        values.every(
          (value) => Number.isFinite(Number(value.replace(/[$,]/g, ""))) && /\d/.test(value),
        )
      )
        field.kind = "number";
      else if (
        values.every(
          (value) =>
            /^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}/.test(value) && Number.isFinite(Date.parse(value)),
        )
      )
        field.kind = "date";
      else if (choices.length > 1 && choices.length <= 12 && choices.length < values.length) {
        field.kind = "choice";
        field.choices = choices;
      }
      if (field.kind === "number" || field.kind === "date")
        field.operators = ["is", "is_not", "gt", "gte", "lt", "lte", "is_empty", "is_not_empty"];
      else if (field.kind === "boolean")
        field.operators = ["is", "is_not", "is_empty", "is_not_empty"];
      else if (field.kind === "choice")
        field.operators = ["is", "is_not", "is_empty", "is_not_empty"];
    });
    if (sortConfig.fallback) sortConfig.fields = filterConfig.fields;
    if (groupConfig.fallback) groupConfig.fields = filterConfig.fields;
  }
  const urlParams = new URL(window.location.href).searchParams;
  const parseUrlTerms = (value) =>
    value
      .split(",")
      .filter(Boolean)
      .map((term) => ({ field: term.replace(/^-/, ""), descending: term.startsWith("-") }));
  if (!filterConfig.filter?.children?.length && urlParams.has("filter")) {
    try {
      filterConfig.filter = JSON.parse(urlParams.get("filter"));
    } catch {
      /* ignore malformed view state */
    }
  }
  if (!sortConfig.sort?.length && urlParams.has("ordering"))
    sortConfig.sort = parseUrlTerms(urlParams.get("ordering") || "");
  if (!groupConfig.group?.length && urlParams.has("group"))
    groupConfig.group = parseUrlTerms(urlParams.get("group") || "");
  const filterSpecs = new Map((filterConfig.fields || []).map((field) => [field.name, field]));
  const fieldOptions = [...document.querySelectorAll("[data-field-option]")];
  const fieldNames = fieldOptions.map((option) => option.dataset.fieldKey);
  const fieldLabels = new Map(
    fieldOptions.map((option) => [
      option.dataset.fieldKey,
      option.dataset.fieldDisplay || option.dataset.fieldLabel,
    ]),
  );
  const fieldIconFor = (kind) =>
    ({ date: "▦", datetime: "▦", time: "▦", number: "#", choice: "⊙", boolean: "⊙" })[kind] || "≡";
  fieldOptions.forEach((option) => {
    const kind =
      filterSpecs.get(option.dataset.fieldKey)?.kind || option.dataset.fieldKind || "text";
    option.dataset.fieldKind = kind;
    const icon = option.querySelector(".field-type-icon");
    if (icon) icon.textContent = fieldIconFor(kind);
  });
  document.querySelectorAll("[data-grid-field-kind]").forEach((option) => {
    const kind =
      filterSpecs.get(option.dataset.gridSortChoice || option.dataset.gridGroupChoice)?.kind ||
      option.dataset.gridFieldKind ||
      "text";
    option.dataset.gridFieldKind = kind;
    const icon = option.querySelector(".toolbar-icon");
    if (icon) icon.textContent = fieldIconFor(kind);
  });
  const sortButtons = [...document.querySelectorAll("[data-grid-sort-choice]")];
  const groupButtons = [...document.querySelectorAll("[data-grid-group-choice]")];
  const allowedFields = new Set(fieldNames);
  const validTerms = (terms) =>
    Array.isArray(terms) ? terms.filter((term) => term && allowedFields.has(term.field)) : [];
  const viewState = {
    hidden: Array.isArray(initialState.hidden)
      ? initialState.hidden.filter((field) => allowedFields.has(field))
      : [],
    order: Array.isArray(initialState.order)
      ? initialState.order.filter(
          (field, index, list) => allowedFields.has(field) && list.indexOf(field) === index,
        )
      : [],
    filter:
      filterConfig.filter && typeof filterConfig.filter === "object"
        ? filterConfig.filter
        : { conjunction: "and", children: [] },
    sort: validTerms(sortConfig.sort),
    group: validTerms(groupConfig.group),
  };
  const saveState = () => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(viewState));
    } catch {
      /* storage is optional */
    }
  };
  const labelFor = (field) => fieldLabels.get(field) || field.replaceAll("_", " ");
  const setQueryParams = (updates, removals = []) => {
    const url = new URL(window.location.href);
    url.searchParams.delete("page");
    removals.forEach((name) => {
      url.searchParams.delete(name);
    });
    Object.entries(updates).forEach(([name, value]) => {
      if (value === "" || value == null) url.searchParams.delete(name);
      else url.searchParams.set(name, value);
    });
    window.location.assign(url.href);
  };
  const cellValue = (row, field) => {
    const cell = [...row.querySelectorAll("[data-list-column]")].find(
      (item) => item.dataset.listColumn === field,
    );
    return cell ? cell.innerText.trim() : "";
  };
  const compareValues = (left, right) => {
    const a = left.trim();
    const b = right.trim();
    if (!a && !b) return 0;
    if (!a) return -1;
    if (!b) return 1;
    const numericA = Number(a.replace(/[$,]/g, ""));
    const numericB = Number(b.replace(/[$,]/g, ""));
    if (Number.isFinite(numericA) && Number.isFinite(numericB) && /\d/.test(a) && /\d/.test(b)) {
      return numericA - numericB;
    }
    const parsedDateA = Date.parse(a);
    const parsedDateB = Date.parse(b);
    if (
      Number.isFinite(parsedDateA) &&
      Number.isFinite(parsedDateB) &&
      /\d/.test(a) &&
      /\d/.test(b)
    )
      return parsedDateA - parsedDateB;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  };

  // Hide fields: restore visibility and order per list, and keep the table and picker in sync.
  const fieldList = document.querySelector(".field-menu-list");
  const fieldToggles = [...document.querySelectorAll("[data-field-toggle]")];
  const fieldOrder = [
    ...viewState.order,
    ...fieldNames.filter((field) => !viewState.order.includes(field)),
  ];
  const renderFieldOrder = () => {
    const empty = fieldList?.querySelector("[data-field-empty]");
    fieldOrder.forEach((field) => {
      const option = fieldOptions.find((item) => item.dataset.fieldKey === field);
      if (option && fieldList) fieldList.insertBefore(option, empty || null);
    });
  };
  const applyFieldOrder = () => {
    const rows = [
      table.tHead?.rows[0],
      ...[...table.tBodies].flatMap((section) => [...section.rows]),
    ].filter(Boolean);
    rows.forEach((row) => {
      const cells = [...row.querySelectorAll(":scope > [data-list-column]")];
      if (!cells.length) return;
      const byName = new Map(cells.map((cell) => [cell.dataset.listColumn, cell]));
      const marker = document.createComment("");
      cells[0].before(marker);
      for (const field of [...fieldOrder, ...cells.map((cell) => cell.dataset.listColumn)]) {
        const cell = byName.get(field);
        if (!cell) continue;
        marker.before(cell);
        byName.delete(field);
      }
      marker.remove();
    });
  };
  const applyFieldVisibility = () => {
    const hidden = new Set(
      fieldToggles.filter((input) => !input.checked).map((input) => input.dataset.fieldToggle),
    );
    table.querySelectorAll("[data-list-column]").forEach((cell) => {
      cell.hidden = hidden.has(cell.dataset.listColumn);
    });
  };
  const saveFieldState = () => {
    viewState.hidden = fieldToggles
      .filter((input) => !input.checked)
      .map((input) => input.dataset.fieldToggle);
    viewState.order = [...fieldOrder];
    saveState();
  };
  fieldToggles.forEach((input) => {
    input.checked = !viewState.hidden.includes(input.dataset.fieldToggle);
    input.addEventListener("change", () => {
      applyFieldVisibility();
      saveFieldState();
    });
  });
  renderFieldOrder();
  applyFieldOrder();
  applyFieldVisibility();
  document.querySelector("[data-hide-all]")?.addEventListener("click", () => {
    fieldToggles.forEach((input) => {
      input.checked = false;
    });
    applyFieldVisibility();
    saveFieldState();
  });
  document.querySelector("[data-show-all]")?.addEventListener("click", () => {
    fieldToggles.forEach((input) => {
      input.checked = true;
    });
    applyFieldVisibility();
    saveFieldState();
  });
  const fieldSearch = document.querySelector("[data-field-search]");
  fieldSearch?.addEventListener("input", () => {
    const query = fieldSearch.value.trim().toLocaleLowerCase();
    let visible = 0;
    fieldOptions.forEach((option) => {
      option.hidden = !option.dataset.fieldLabel.includes(query);
      if (!option.hidden) visible += 1;
    });
    const empty = document.querySelector("[data-field-empty]");
    if (empty) empty.hidden = visible > 0;
  });
  let dragField = null;
  let dragPointer = null;
  const stopFieldDrag = () => {
    document.body.classList.remove("field-menu-dragging");
    fieldOptions.forEach((option) => {
      option.classList.remove("is-dragging");
    });
    document.removeEventListener("pointermove", moveField);
    document.removeEventListener("pointerup", finishField);
    document.removeEventListener("pointercancel", cancelField);
    dragField = dragPointer = null;
  };
  const moveField = (event) => {
    if (!dragField || event.pointerId !== dragPointer) return;
    const target = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest("[data-field-option]");
    if (!target || target === dragField) return;
    const rect = target.getBoundingClientRect();
    fieldList.insertBefore(
      dragField,
      event.clientY > rect.top + rect.height / 2 ? target.nextSibling : target,
    );
  };
  const finishField = (event) => {
    if (event.pointerId !== dragPointer) return;
    fieldOrder.splice(
      0,
      fieldOrder.length,
      ...[...fieldList.querySelectorAll("[data-field-option]")].map(
        (option) => option.dataset.fieldKey,
      ),
    );
    stopFieldDrag();
    applyFieldOrder();
    saveFieldState();
  };
  const cancelField = (event) => {
    if (event.pointerId !== dragPointer) return;
    stopFieldDrag();
    renderFieldOrder();
  };
  document.querySelectorAll("[data-field-reorder]").forEach((handle) => {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.isPrimary === false) return;
      event.preventDefault();
      dragField = handle.closest("[data-field-option]");
      dragPointer = event.pointerId;
      dragField?.classList.add("is-dragging");
      document.body.classList.add("field-menu-dragging");
      document.addEventListener("pointermove", moveField);
      document.addEventListener("pointerup", finishField);
      document.addEventListener("pointercancel", cancelField);
    });
    handle.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const index = fieldOrder.indexOf(handle.dataset.fieldReorder);
      const next = index + (event.key === "ArrowUp" ? -1 : 1);
      if (next < 0 || next >= fieldOrder.length) return;
      [fieldOrder[index], fieldOrder[next]] = [fieldOrder[next], fieldOrder[index]];
      renderFieldOrder();
      applyFieldOrder();
      saveFieldState();
      handle.focus();
    });
  });

  // Filter: type-aware conditions are encoded into the list URL and applied before pagination.
  const filterMenu = document.querySelector("[data-filter-menu]");
  const filterRows = filterMenu?.querySelector("[data-filter-conditions]");
  const filterTemplate =
    filterMenu?.querySelector("[data-filter-row-template]") ||
    document.querySelector("[data-filter-row-template]");
  const filterJoin = filterMenu?.querySelector("[data-filter-conjunction]");
  const filterCount = filterMenu?.querySelector("[data-filter-count]");
  const filterApply = filterMenu?.querySelector("[data-filter-apply]");
  const filterReset = filterMenu?.querySelector("[data-filter-reset]");
  const serverFilterFields = [...document.querySelectorAll("[data-filter-server-field]")].map(
    (node) => node.dataset.filterServerField,
  );
  const exactFilterParams = serverFilterFields.flatMap((field) => [field, `${field}__exact`]);
  const cleanFilterGroup = (group) => ({
    conjunction: group?.conjunction === "or" ? "or" : "and",
    children: (Array.isArray(group?.children) ? group.children : []).flatMap((child) => {
      if (child?.children) {
        const nested = cleanFilterGroup(child);
        return nested.children.length ? [nested] : [];
      }
      if (!child || !filterSpecs.has(child.field)) return [];
      const spec = filterSpecs.get(child.field);
      const op = child.op || (spec.operators.includes("contains") ? "contains" : spec.operators[0]);
      if (!spec.operators.includes(op)) return [];
      if (!["is_empty", "is_not_empty"].includes(op) && !String(child.value ?? "").trim())
        return [];
      return [{ field: child.field, op, value: String(child.value ?? "") }];
    }),
  });
  let appliedFilter = cleanFilterGroup(viewState.filter);
  if (!appliedFilter.children.length && serverFilterFields.length) {
    appliedFilter.children = serverFilterFields.flatMap((field) => {
      const node = document.querySelector(`[data-filter-server-field="${CSS.escape(field)}"]`);
      const value = node?.dataset.filterServerValue || "";
      return value && allowedFields.has(field) ? [{ field, op: "is", value }] : [];
    });
  }
  let draftFilter = clone(appliedFilter);
  const filterTermCount = (group) =>
    group.children.reduce(
      (count, child) => count + (child.children ? filterTermCount(child) : 1),
      0,
    );
  const filterIsValid = (term) =>
    ["is_empty", "is_not_empty"].includes(term.op) || Boolean(String(term.value ?? "").trim());
  const renderFilterCount = () => {
    const count = filterTermCount(appliedFilter);
    if (filterCount) {
      filterCount.hidden = !count;
      filterCount.textContent = String(count);
    }
  };
  const filterMatches = (row, group) => {
    const checks = group.children
      .map((child) => {
        if (child.children) return filterMatches(row, child);
        if (!filterIsValid(child)) return null;
        const actual = cellValue(row, child.field);
        const expected = String(child.value ?? "").trim();
        const a = actual.toLocaleLowerCase();
        const b = expected.toLocaleLowerCase();
        switch (child.op) {
          case "contains":
            return a.includes(b);
          case "does_not_contain":
            return !a.includes(b);
          case "is":
            return compareValues(actual, expected) === 0;
          case "is_not":
            return compareValues(actual, expected) !== 0;
          case "is_empty":
            return !actual;
          case "is_not_empty":
            return Boolean(actual);
          case "gt":
            return compareValues(actual, expected) > 0;
          case "gte":
            return compareValues(actual, expected) >= 0;
          case "lt":
            return compareValues(actual, expected) < 0;
          case "lte":
            return compareValues(actual, expected) <= 0;
          default:
            return true;
        }
      })
      .filter((result) => result !== null);
    if (!checks.length) return true;
    return group.conjunction === "or" ? checks.some(Boolean) : checks.every(Boolean);
  };
  const makeJoin = (group, onChange, label) => {
    const select = document.createElement("select");
    select.className = "form-select form-select-sm filter-join";
    select.setAttribute("aria-label", label);
    select.add(new Option("and", "and"));
    select.add(new Option("or", "or"));
    select.value = group.conjunction;
    select.addEventListener("change", () => {
      group.conjunction = select.value;
      onChange();
    });
    return select;
  };
  const makeRemoveButton = (label, action) => {
    const button = document.createElement("button");
    button.className = "filter-remove";
    button.type = "button";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M5 12h14" /></svg>';
    button.addEventListener("click", action);
    return button;
  };
  const addFilterTerm = (group) => {
    const firstField =
      serverFilterFields.find((field) => filterSpecs.has(field)) || filterSpecs.keys().next().value;
    if (!firstField || !filterTemplate) return;
    const spec = filterSpecs.get(firstField);
    group.children.push({
      field: firstField,
      op: spec.operators.includes("contains") ? "contains" : spec.operators[0],
      value: "",
      draft: true,
    });
    renderFilter();
    filterRows?.querySelector("[data-filter-row]:last-child [data-filter-value]")?.focus();
  };
  const filterOperatorLabels = {
    contains: "contains",
    does_not_contain: "does not contain",
    is: "is",
    is_not: "is not",
    gt: "is greater than",
    gte: "is greater than or equal to",
    lt: "is less than",
    lte: "is less than or equal to",
    is_empty: "is empty",
    is_not_empty: "is not empty",
  };
  const configureFilterValue = (row, spec, op, currentValue) => {
    let value = row.querySelector("[data-filter-value]");
    const wantsSelect = spec.kind === "choice" || spec.kind === "boolean";
    if (wantsSelect !== (value.tagName === "SELECT")) {
      const replacement = document.createElement(wantsSelect ? "select" : "input");
      replacement.className = "form-control form-control-sm";
      replacement.dataset.filterValue = "";
      replacement.setAttribute("aria-label", "Filter value");
      value.replaceWith(replacement);
      value = replacement;
    }
    value.className = "form-control form-control-sm";
    value.dataset.filterValue = "";
    value.setAttribute("aria-label", "Filter value");
    if (value.tagName === "SELECT") {
      value.replaceChildren(new Option("Choose a value", ""));
      const choices =
        spec.kind === "boolean"
          ? [
              ["true", "Yes"],
              ["false", "No"],
            ]
          : (spec.choices || []).map((choice) => [choice, choice]);
      choices.forEach(([key, label]) => {
        value.add(new Option(label, key));
      });
    } else {
      value.type =
        { number: "number", date: "date", datetime: "datetime-local", time: "time" }[spec.kind] ||
        "text";
      if (spec.kind === "number") value.step = "any";
      value.placeholder = spec.kind === "datetime" ? "YYYY-MM-DDTHH:MM" : "Enter a value";
    }
    value.value = currentValue ?? "";
    const noValue = ["is_empty", "is_not_empty"].includes(op);
    value.hidden = noValue;
    value.disabled = noValue;
    return value;
  };
  const renderFilterNode = (group, root = false) => {
    const container = document.createElement("div");
    container.className = root ? "filter-group" : "filter-group filter-group-nested";
    if (!root) {
      const heading = document.createElement("div");
      heading.className = "filter-group-header";
      const title = document.createElement("p");
      title.className = "filter-group-heading";
      title.textContent =
        group.conjunction === "and"
          ? "All of the following are true"
          : "Any of the following are true";
      const tools = document.createElement("div");
      tools.className = "filter-group-tools";
      tools.append(makeJoin(group, renderFilter, "Condition group match"));
      const add = document.createElement("button");
      add.type = "button";
      add.className = "filter-add-icon";
      add.setAttribute("aria-label", "Add condition to group");
      add.textContent = "+";
      add.addEventListener("click", () => addFilterTerm(group));
      tools.append(add);
      tools.append(
        makeRemoveButton("Remove condition group", () => {
          const removeChild = (parent, target) => {
            const index = parent.children.indexOf(target);
            if (index >= 0) parent.children.splice(index, 1);
            else
              parent.children.forEach((child) => {
                if (child.children) removeChild(child, target);
              });
          };
          removeChild(draftFilter, group);
          renderFilter();
        }),
      );
      heading.append(title, tools);
      container.append(heading);
    }
    const children = document.createElement("div");
    children.className = "filter-conditions";
    group.children.forEach((child) => {
      if (child.children) {
        children.append(renderFilterNode(child));
        return;
      }
      const fragment = filterTemplate.content.cloneNode(true);
      const row = fragment.querySelector("[data-filter-row]");
      const field = row.querySelector("[data-filter-field]");
      const op = row.querySelector("[data-filter-op]");
      const spec = filterSpecs.get(child.field) || filterConfig.fields[0];
      if (!spec) return;
      field.replaceChildren(
        ...filterConfig.fields.map((item) => new Option(item.label, item.name)),
      );
      field.value = child.field;
      const selectedOp = spec.operators.includes(child.op)
        ? child.op
        : spec.operators.includes("contains")
          ? "contains"
          : spec.operators[0];
      op.replaceChildren(
        ...spec.operators.map((name) => new Option(filterOperatorLabels[name] || name, name)),
      );
      op.value = selectedOp;
      let value = configureFilterValue(row, spec, selectedOp, child.value);
      const sync = () => {
        child.field = field.value;
        child.op = op.value;
        child.value = value.value;
        child.draft = !filterIsValid(child);
        renderFilterCount();
      };
      field.addEventListener("change", () => {
        const next = filterSpecs.get(field.value);
        child.field = field.value;
        child.op = next.operators.includes("contains") ? "contains" : next.operators[0];
        child.value = "";
        child.draft = true;
        renderFilter();
      });
      op.addEventListener("change", () => {
        child.op = op.value;
        child.value = "";
        child.draft = !["is_empty", "is_not_empty"].includes(op.value);
        renderFilter();
      });
      value.addEventListener(value.tagName === "SELECT" ? "change" : "input", sync);
      value.addEventListener(value.tagName === "SELECT" ? "change" : "input", () => {
        if (filterApply)
          filterApply.disabled =
            filterTermCount(cleanFilterGroup(draftFilter)) === 0 &&
            filterTermCount(appliedFilter) === 0;
      });
      const updateApplyState = () => {
        if (filterApply)
          filterApply.disabled =
            filterTermCount(cleanFilterGroup(draftFilter)) === 0 &&
            filterTermCount(appliedFilter) === 0;
      };
      row.querySelector("[data-filter-remove]")?.addEventListener("click", () => {
        group.children.splice(group.children.indexOf(child), 1);
        renderFilter();
      });
      children.append(fragment);
    });
    if (!group.children.length) {
      const empty = document.createElement("p");
      empty.className = "filter-empty";
      empty.textContent = root
        ? "No filter conditions are applied."
        : "No conditions in this group.";
      children.append(empty);
    }
    container.append(children);
    if (root) {
      const actions = document.createElement("div");
      actions.className = "filter-group-actions";
      const addCondition = document.createElement("button");
      addCondition.className = "filter-add";
      addCondition.type = "button";
      addCondition.dataset.filterAdd = "";
      addCondition.textContent = "＋ Add condition";
      addCondition.addEventListener("click", () => addFilterTerm(group));
      const addGroup = document.createElement("button");
      addGroup.className = "filter-add";
      addGroup.type = "button";
      addGroup.dataset.filterAddGroup = "";
      addGroup.textContent = "＋ Add condition group";
      addGroup.addEventListener("click", () => {
        const nested = { conjunction: "and", children: [] };
        group.children.push(nested);
        addFilterTerm(nested);
      });
      actions.append(addCondition, addGroup);
      container.append(actions);
    }
    return container;
  };
  function renderFilter() {
    if (!filterRows) return;
    filterJoin.value = draftFilter.conjunction;
    filterRows.replaceChildren(renderFilterNode(draftFilter, true));
    renderFilterCount();
    if (filterApply)
      filterApply.disabled =
        filterTermCount(cleanFilterGroup(draftFilter)) === 0 &&
        filterTermCount(appliedFilter) === 0;
  }
  filterJoin?.addEventListener("change", () => {
    draftFilter.conjunction = filterJoin.value;
  });
  filterApply?.addEventListener("click", () => {
    const nextFilter = cleanFilterGroup(draftFilter);
    setQueryParams(
      { filter: nextFilter.children.length ? JSON.stringify(nextFilter) : "" },
      exactFilterParams,
    );
  });
  filterReset?.addEventListener("click", () => {
    setQueryParams({ filter: "" }, exactFilterParams);
  });
  filterMenu?.addEventListener("toggle", () => {
    if (!filterMenu.open) return;
    draftFilter = clone(appliedFilter);
    if (!draftFilter.children.length && filterConfig.fields.length) {
      const first =
        serverFilterFields.find((name) => filterSpecs.has(name)) || filterConfig.fields[0].name;
      const spec = filterSpecs.get(first);
      draftFilter.children.push({
        field: first,
        op: spec.operators.includes("contains") ? "contains" : spec.operators[0],
        value: "",
        draft: true,
      });
    }
    renderFilter();
  });

  // Sort: draft multiple ordered fields; Apply and Cancel make edits predictable.
  const sortMenu = document.querySelector("[data-sort-menu]");
  const sortRows = sortMenu?.querySelector("[data-sort-rows]");
  const sortFields = sortMenu?.querySelector("[data-sort-fields]");
  const sortSearch = sortMenu?.querySelector("[data-sort-search]");
  const sortPicker = sortMenu?.querySelector("[data-sort-picker]");
  const sortAdd = sortMenu?.querySelector("[data-sort-add]");
  const sortApply = sortMenu?.querySelector("[data-sort-apply]");
  const sortCancel = sortMenu?.querySelector("[data-sort-cancel]");
  const sortCount = sortMenu?.querySelector("[data-sort-count]");
  const sortSpecs = new Map((sortConfig.fields || []).map((field) => [field.name, field]));
  const directionLabels = (field) => {
    const kind = sortSpecs.get(field)?.kind || "text";
    return (
      {
        number: ["1 → 9", "9 → 1"],
        date: ["Earliest → Latest", "Latest → Earliest"],
        datetime: ["Earliest → Latest", "Latest → Earliest"],
        time: ["Earliest → Latest", "Latest → Earliest"],
        boolean: ["Unchecked → Checked", "Checked → Unchecked"],
        choice: ["First → Last", "Last → First"],
      }[kind] || ["A → Z", "Z → A"]
    );
  };
  let appliedSort = viewState.sort
    .slice(0, 5)
    .map((term) => ({ field: term.field, descending: Boolean(term.descending) }));
  let draftSort = clone(appliedSort);
  let pickingSort = !draftSort.length;
  let sortQuery = "";
  const renderSortFields = () => {
    const selected = new Set(draftSort.map((term) => term.field));
    const buttons = sortFields?.querySelectorAll("[data-grid-sort-choice]") || [];
    let shown = 0;
    buttons.forEach((button) => {
      const visible =
        !selected.has(button.dataset.gridSortChoice) &&
        button.dataset.gridOptionLabel.includes(sortQuery);
      button.hidden = !visible;
      if (visible) shown += 1;
    });
    let empty = sortFields?.querySelector("[data-sort-empty]");
    if (sortFields && !empty) {
      empty = document.createElement("p");
      empty.className = "sort-empty";
      empty.dataset.sortEmpty = "";
      sortFields.append(empty);
    }
    if (empty) {
      empty.textContent = shown
        ? ""
        : sortQuery
          ? "No fields found."
          : "All fields are already sorted.";
      empty.hidden = shown > 0;
    }
  };
  const renderSort = () => {
    if (!sortRows || !sortPicker) return;
    sortRows.replaceChildren();
    draftSort.forEach((term, index) => {
      const row = document.createElement("div");
      row.className = "sort-row";
      row.dataset.sortIndex = String(index);
      const field = document.createElement("select");
      field.className = "form-select form-select-sm";
      field.setAttribute("aria-label", `Sort field ${index + 1}`);
      [...sortSpecs.keys()].forEach((name) => {
        if (
          name !== term.field &&
          draftSort.some((other, otherIndex) => otherIndex !== index && other.field === name)
        )
          return;
        field.add(new Option(labelFor(name), name));
      });
      field.value = term.field;
      field.addEventListener("change", () => {
        term.field = field.value;
        term.descending = false;
        renderSort();
      });
      const direction = document.createElement("select");
      direction.className = "form-select form-select-sm sort-direction";
      direction.setAttribute("aria-label", `Sort direction for ${labelFor(term.field)}`);
      const directionText = directionLabels(term.field);
      direction.add(new Option(directionText[0], "asc"));
      direction.add(new Option(directionText[1], "desc"));
      direction.value = term.descending ? "desc" : "asc";
      direction.addEventListener("change", () => {
        term.descending = direction.value === "desc";
      });
      const remove = makeRemoveButton(`Remove ${labelFor(term.field)} sort`, () => {
        draftSort.splice(index, 1);
        pickingSort = !draftSort.length;
        renderSort();
      });
      const grip = document.createElement("button");
      grip.type = "button";
      grip.className = "sort-icon sort-grip";
      grip.draggable = true;
      grip.title = "Drag or use Alt+Up/Down";
      grip.setAttribute("aria-label", `Reorder ${labelFor(term.field)} sort`);
      grip.textContent = "⠿";
      grip.addEventListener("keydown", (event) => {
        if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        moveCriterion(draftSort, index, index + (event.key === "ArrowUp" ? -1 : 1), renderSort);
      });
      grip.addEventListener("dragstart", (event) => {
        event.dataTransfer?.setData("text/plain", String(index));
      });
      row.addEventListener("dragover", (event) => event.preventDefault());
      row.addEventListener("drop", (event) => {
        event.preventDefault();
        moveCriterion(
          draftSort,
          Number(event.dataTransfer?.getData("text/plain")),
          index,
          renderSort,
        );
      });
      row.append(field, direction, remove, grip);
      sortRows.append(row);
    });
    sortPicker.hidden = !pickingSort;
    sortAdd.hidden =
      !draftSort.length || draftSort.length >= 5 || draftSort.length >= fieldNames.length;
    renderSortFields();
    if (sortCount) {
      sortCount.hidden = !appliedSort.length;
      sortCount.textContent = String(appliedSort.length);
    }
    sortApply.disabled = draftSort.length === 0 && appliedSort.length === 0;
  };
  sortSearch?.addEventListener("input", () => {
    sortQuery = sortSearch.value.trim().toLocaleLowerCase();
    renderSortFields();
  });
  sortAdd?.addEventListener("click", () => {
    pickingSort = true;
    sortQuery = "";
    sortSearch.value = "";
    renderSort();
    sortSearch.focus();
  });
  sortButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (
        draftSort.length >= 5 ||
        draftSort.some((term) => term.field === button.dataset.gridSortChoice)
      )
        return;
      draftSort.push({ field: button.dataset.gridSortChoice, descending: false });
      pickingSort = false;
      sortQuery = "";
      sortSearch.value = "";
      renderSort();
    });
  });
  sortApply?.addEventListener("click", () => {
    const ordering = draftSort
      .map((term) => `${term.descending ? "-" : ""}${term.field}`)
      .join(",");
    setQueryParams({ ordering });
  });
  sortCancel?.addEventListener("click", () => {
    draftSort = clone(appliedSort);
    pickingSort = !draftSort.length;
    renderSort();
    closeMenus();
  });
  sortMenu?.addEventListener("toggle", () => {
    if (sortMenu.open) {
      draftSort = clone(appliedSort);
      pickingSort = !draftSort.length;
      sortQuery = "";
      sortSearch.value = "";
      renderSort();
    }
  });

  // Group: keep up to three nested grouping levels and render collapsible group headers.
  const groupMenu = document.querySelector("[data-group-menu]");
  const groupRows = groupMenu?.querySelector("[data-group-rows]");
  const groupFields = groupMenu?.querySelector("[data-group-fields]");
  const groupSearch = groupMenu?.querySelector("[data-group-search]");
  const groupPicker = groupMenu?.querySelector("[data-group-picker]");
  const groupAdd = groupMenu?.querySelector("[data-group-add]");
  const groupApply = groupMenu?.querySelector("[data-group-apply]");
  const groupCancel = groupMenu?.querySelector("[data-group-cancel]");
  const groupCount = groupMenu?.querySelector("[data-group-count]");
  const groupBulkControls = groupMenu?.querySelector("[data-group-bulk-controls]");
  const groupSpecs = new Map((groupConfig.fields || []).map((field) => [field.name, field]));
  let appliedGroup = viewState.group
    .slice(0, 3)
    .map((term) => ({ field: term.field, descending: Boolean(term.descending) }));
  let draftGroup = clone(appliedGroup);
  let pickingGroup = !draftGroup.length;
  let groupQuery = "";
  const renderGroupFields = () => {
    const selected = new Set(draftGroup.map((term) => term.field));
    const buttons = groupFields?.querySelectorAll("[data-grid-group-choice]") || [];
    let shown = 0;
    buttons.forEach((button) => {
      const visible =
        !selected.has(button.dataset.gridGroupChoice) &&
        button.dataset.gridOptionLabel.includes(groupQuery);
      button.hidden = !visible;
      if (visible) shown += 1;
    });
    let empty = groupFields?.querySelector("[data-group-empty]");
    if (groupFields && !empty) {
      empty = document.createElement("p");
      empty.className = "sort-empty";
      empty.dataset.groupEmpty = "";
      groupFields.append(empty);
    }
    if (empty) {
      empty.textContent = shown
        ? ""
        : groupQuery
          ? "No fields found."
          : "All fields are already grouped.";
      empty.hidden = shown > 0;
    }
  };
  const renderGroup = () => {
    if (!groupRows || !groupPicker) return;
    groupRows.replaceChildren();
    draftGroup.forEach((term, index) => {
      const row = document.createElement("div");
      row.className = "sort-row";
      row.dataset.groupIndex = String(index);
      const field = document.createElement("select");
      field.className = "form-select form-select-sm";
      field.setAttribute("aria-label", `Group field ${index + 1}`);
      [...groupSpecs.keys()].forEach((name) => {
        if (
          name !== term.field &&
          draftGroup.some((other, otherIndex) => otherIndex !== index && other.field === name)
        )
          return;
        field.add(new Option(labelFor(name), name));
      });
      field.value = term.field;
      field.addEventListener("change", () => {
        term.field = field.value;
        term.descending = false;
        renderGroup();
      });
      const direction = document.createElement("select");
      direction.className = "form-select form-select-sm sort-direction";
      direction.setAttribute("aria-label", `Group direction for ${labelFor(term.field)}`);
      const groupDirectionText = directionLabels(term.field);
      direction.add(new Option(groupDirectionText[0], "asc"));
      direction.add(new Option(groupDirectionText[1], "desc"));
      direction.value = term.descending ? "desc" : "asc";
      direction.addEventListener("change", () => {
        term.descending = direction.value === "desc";
      });
      const remove = makeRemoveButton(`Remove ${labelFor(term.field)} grouping`, () => {
        draftGroup.splice(index, 1);
        pickingGroup = !draftGroup.length;
        renderGroup();
      });
      const grip = document.createElement("button");
      grip.type = "button";
      grip.className = "sort-icon sort-grip";
      grip.draggable = true;
      grip.title = "Drag or use Alt+Up/Down";
      grip.setAttribute("aria-label", `Reorder ${labelFor(term.field)} grouping`);
      grip.textContent = "⠿";
      grip.addEventListener("keydown", (event) => {
        if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        moveCriterion(draftGroup, index, index + (event.key === "ArrowUp" ? -1 : 1), renderGroup);
      });
      grip.addEventListener("dragstart", (event) => {
        event.dataTransfer?.setData("text/plain", String(index));
      });
      row.addEventListener("dragover", (event) => event.preventDefault());
      row.addEventListener("drop", (event) => {
        event.preventDefault();
        moveCriterion(
          draftGroup,
          Number(event.dataTransfer?.getData("text/plain")),
          index,
          renderGroup,
        );
      });
      row.append(field, direction, remove, grip);
      groupRows.append(row);
    });
    groupPicker.hidden = !pickingGroup;
    groupAdd.hidden =
      !draftGroup.length || draftGroup.length >= 3 || draftGroup.length >= fieldNames.length;
    if (groupBulkControls) groupBulkControls.hidden = appliedGroup.length === 0;
    renderGroupFields();
    if (groupCount) {
      groupCount.hidden = !appliedGroup.length;
      groupCount.textContent = String(appliedGroup.length);
    }
    groupApply.disabled = draftGroup.length === 0 && appliedGroup.length === 0;
  };
  groupSearch?.addEventListener("input", () => {
    groupQuery = groupSearch.value.trim().toLocaleLowerCase();
    renderGroupFields();
  });
  groupAdd?.addEventListener("click", () => {
    pickingGroup = true;
    groupQuery = "";
    groupSearch.value = "";
    renderGroup();
    groupSearch.focus();
  });
  groupButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (
        draftGroup.length >= 3 ||
        draftGroup.some((term) => term.field === button.dataset.gridGroupChoice)
      )
        return;
      draftGroup.push({ field: button.dataset.gridGroupChoice, descending: false });
      pickingGroup = false;
      groupQuery = "";
      groupSearch.value = "";
      renderGroup();
    });
  });
  groupApply?.addEventListener("click", () => {
    const grouping = draftGroup
      .map((term) => `${term.descending ? "-" : ""}${term.field}`)
      .join(",");
    setQueryParams({ group: grouping });
  });
  groupCancel?.addEventListener("click", () => {
    draftGroup = clone(appliedGroup);
    pickingGroup = !draftGroup.length;
    renderGroup();
    closeMenus();
  });
  groupMenu?.addEventListener("toggle", () => {
    if (groupMenu.open) {
      draftGroup = clone(appliedGroup);
      pickingGroup = !draftGroup.length;
      groupQuery = "";
      groupSearch.value = "";
      renderGroup();
    }
  });

  function moveCriterion(list, from, to, render) {
    if (from < 0 || to < 0 || from === to || to >= list.length) return;
    const [term] = list.splice(from, 1);
    list.splice(to, 0, term);
    render();
  }

  const collapsedGroups = new Set();
  const activeFilterCount = () => filterTermCount(appliedFilter);
  const activeRowCount = () =>
    originalRows.filter((row) => filterMatches(row, appliedFilter)).length;
  const updateSelection = () => {
    const matchingChecks = rowChecks.filter(
      (input) => input.closest("tr")?.dataset.filterMatch === "true",
    );
    const selected = matchingChecks.filter((input) => input.checked);
    const selectable = matchingChecks.filter((input) => !input.disabled);
    if (selectionStatus) {
      const totalCount = Number(selectionStatus.dataset.totalCount || originalRows.length);
      const filteredCount =
        table.dataset.serverFiltering === "true" ? totalCount : activeRowCount();
      const count = selected.length || (activeFilterCount() ? filteredCount : totalCount);
      selectionStatus.textContent = `${count} ${count === 1 ? "record" : "records"}${selected.length ? " selected" : ""}`;
    }
    if (bulkControls) bulkControls.hidden = selected.length === 0;
    if (clearSelection) clearSelection.hidden = selected.length === 0;
    if (selectAll) {
      selectAll.checked = selectable.length > 0 && selected.length === selectable.length;
      selectAll.indeterminate = selected.length > 0 && !selectAll.checked;
    }
    if (bulkSubmit) bulkSubmit.disabled = selected.length === 0 || !bulkAction?.value;
    rowChecks.forEach((input) => {
      input.closest("tr")?.toggleAttribute("data-selected", input.checked);
    });
    if (!selected.length && bulkAction) bulkAction.value = "";
  };
  selectAll?.addEventListener("change", () => {
    rowChecks.forEach((input) => {
      if (!input.disabled && input.closest("tr")?.dataset.filterMatch === "true")
        input.checked = selectAll.checked;
    });
    updateSelection();
  });
  rowChecks.forEach((input) => {
    input.addEventListener("change", updateSelection);
  });
  bulkAction?.addEventListener("change", updateSelection);
  clearSelection?.addEventListener("click", () => {
    rowChecks.forEach((input) => {
      input.checked = false;
    });
    updateSelection();
  });
  document.querySelector("[data-bulk-form]")?.addEventListener("submit", (event) => {
    if (
      !rowChecks.some((input) => input.checked && !input.closest("tr")?.hidden) ||
      !bulkAction?.value
    )
      event.preventDefault();
  });

  function refreshGrid() {
    body.querySelectorAll("[data-client-group]").forEach((row) => {
      row.remove();
    });
    originalEmptyRow?.remove();
    const matching = originalRows.filter((row) => filterMatches(row, appliedFilter));
    const nonmatching = originalRows.filter((row) => !filterMatches(row, appliedFilter));
    const groupTerms = appliedGroup.filter((term) => allowedFields.has(term.field));
    const sortTerms = [
      ...groupTerms,
      ...appliedSort.filter((term) => allowedFields.has(term.field)),
    ];
    const sortRowsByTerms = (rows) =>
      rows.sort((left, right) => {
        for (const term of sortTerms) {
          const result = compareValues(cellValue(left, term.field), cellValue(right, term.field));
          if (result) return term.descending ? -result : result;
        }
        return originalRows.indexOf(left) - originalRows.indexOf(right);
      });
    const ordered = sortRowsByTerms([...matching]);
    const rowOffset = Number(table.dataset.pageOffset || 0);
    ordered.forEach((row, index) => {
      const rowNumber = row.querySelector(".row-index");
      if (rowNumber) rowNumber.textContent = String(rowOffset + index + 1);
    });
    const hiddenFields = new Set(
      fieldToggles.filter((input) => !input.checked).map((input) => input.dataset.fieldToggle),
    );
    table.querySelectorAll("[data-list-column]").forEach((cell) => {
      cell.hidden = hiddenFields.has(cell.dataset.listColumn);
    });
    let previousKeys = [];
    const groupCounts = new Map();
    if (groupTerms.length) {
      ordered.forEach((row) => {
        const values = groupTerms.map((term) => cellValue(row, term.field));
        values.forEach((_, index) => {
          const key = JSON.stringify(
            groupTerms
              .slice(0, index + 1)
              .map((term, termIndex) => [term.field, values[termIndex]]),
          );
          groupCounts.set(key, (groupCounts.get(key) || 0) + 1);
        });
      });
    }
    body.replaceChildren();
    for (const row of ordered) {
      const ancestors = [];
      if (groupTerms.length) {
        const values = groupTerms.map((term) => cellValue(row, term.field));
        values.forEach((value, index) => {
          const key = JSON.stringify(
            groupTerms
              .slice(0, index + 1)
              .map((term, termIndex) => [term.field, values[termIndex]]),
          );
          if (previousKeys[index] !== key) {
            const heading = document.createElement("tr");
            heading.className = "group-header";
            heading.dataset.clientGroup = "";
            heading.dataset.groupKey = key;
            heading.dataset.groupAncestors = JSON.stringify(ancestors);
            const cell = document.createElement("td");
            cell.colSpan = table.tHead.rows[0].cells.length;
            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = "group-toggle";
            toggle.dataset.groupToggle = "";
            toggle.setAttribute("aria-expanded", String(!collapsedGroups.has(key)));
            const caret = document.createElement("span");
            caret.className = "group-caret";
            caret.setAttribute("aria-hidden", "true");
            caret.textContent = collapsedGroups.has(key) ? "▸" : "▾";
            const name = document.createElement("span");
            name.className = "group-name";
            name.textContent = labelFor(groupTerms[index].field);
            const groupValue = document.createElement("span");
            groupValue.className = "group-value";
            groupValue.textContent = value || "Empty";
            const count = document.createElement("span");
            count.className = "group-count";
            const amount = groupCounts.get(key) || 0;
            count.textContent = `${amount} ${amount === 1 ? "record" : "records"}`;
            toggle.append(caret, name, groupValue, count);
            cell.append(toggle);
            heading.append(cell);
            body.append(heading);
          }
          ancestors.push(key);
          previousKeys[index] = key;
        });
        previousKeys.length = groupTerms.length;
      }
      row.hidden = false;
      row.dataset.filterMatch = "true";
      row.dataset.groupAncestors = JSON.stringify(ancestors);
      body.append(row);
    }
    nonmatching.forEach((row) => {
      row.hidden = true;
      row.dataset.filterMatch = "false";
      row.dataset.groupAncestors = "[]";
      body.append(row);
    });
    if (collapsedGroups.size) {
      body.querySelectorAll("[data-client-group], [data-grid-row]").forEach((row) => {
        let ancestors = [];
        try {
          ancestors = JSON.parse(row.dataset.groupAncestors || "[]");
        } catch {
          /* ignore stale markup */
        }
        const collapsed = ancestors.find((key) => collapsedGroups.has(key));
        row.hidden = Boolean(collapsed);
        if (row.dataset.groupKey) {
          const ownCollapsed = collapsedGroups.has(row.dataset.groupKey);
          row
            .querySelector("[data-group-toggle]")
            ?.setAttribute("aria-expanded", String(!ownCollapsed));
          const caret = row.querySelector(".group-caret");
          if (caret) caret.textContent = ownCollapsed ? "▸" : "▾";
        }
      });
    }
    if (!ordered.length && originalEmptyRow) {
      const row = originalEmptyRow.cloneNode(true);
      row.querySelector(".grid-empty").textContent = originalRows.length
        ? "No matching records."
        : "No records.";
      body.append(row);
    }
    updateSelection();
  }

  body.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-group-toggle]");
    if (!toggle) return;
    const heading = toggle.closest("[data-client-group]");
    const key = heading?.dataset.groupKey;
    if (!key) return;
    if (collapsedGroups.has(key)) collapsedGroups.delete(key);
    else collapsedGroups.add(key);
    refreshGrid();
  });
  groupMenu?.querySelector("[data-group-collapse-all]")?.addEventListener("click", () => {
    body.querySelectorAll("[data-client-group][data-group-key]").forEach((row) => {
      collapsedGroups.add(row.dataset.groupKey);
    });
    refreshGrid();
  });
  groupMenu?.querySelector("[data-group-expand-all]")?.addEventListener("click", () => {
    collapsedGroups.clear();
    refreshGrid();
  });
  renderSort();
  renderGroup();
  renderFilter();
  renderFilterCount();
  refreshGrid();

  const searchPanel = document.querySelector("[data-search-panel]");
  const searchToggle = document.querySelector("[data-search-toggle]");
  const searchInput = document.querySelector("[data-search-input]");
  const closeSearch = () => {
    if (searchPanel) searchPanel.hidden = true;
    searchToggle?.setAttribute("aria-expanded", "false");
  };
  searchToggle?.addEventListener("click", () => {
    if (!searchPanel) return;
    searchPanel.hidden = !searchPanel.hidden;
    searchToggle.setAttribute("aria-expanded", String(!searchPanel.hidden));
    if (!searchPanel.hidden) searchInput?.focus();
  });
  document.querySelector("[data-search-close]")?.addEventListener("click", closeSearch);

  document.querySelectorAll("[data-row-height-choice]").forEach((button) => {
    button.addEventListener("click", () => {
      table.dataset.rowHeight = button.dataset.rowHeightChoice;
      document.querySelectorAll("[data-row-height-choice]").forEach((item) => {
        item.setAttribute("aria-pressed", String(item === button));
      });
      closeMenus();
    });
  });
  document.querySelector("[data-wrap-headers]")?.addEventListener("click", (event) => {
    const button = event.currentTarget;
    const wrap = button.getAttribute("aria-pressed") !== "true";
    button.setAttribute("aria-pressed", String(wrap));
    table.dataset.wrapHeaders = String(wrap);
    closeMenus();
  });
  document.querySelectorAll("[data-grid-color]").forEach((button) => {
    button.addEventListener("click", () => {
      const color = button.dataset.gridColor;
      if (color === "default") delete table.dataset.rowColor;
      else table.dataset.rowColor = color;
      closeMenus();
    });
  });
  document.querySelector("[data-share-grid]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const label = button.querySelector("[data-share-label]");
    try {
      await navigator.clipboard.writeText(window.location.href);
      if (label) label.textContent = "Copied";
      window.setTimeout(() => {
        if (label) label.textContent = "Copy view link";
      }, 1500);
    } catch {
      button.title = "Copy this page URL from the address bar";
    }
  });
})();
