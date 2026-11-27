(() => {
  const table = document.querySelector("[data-list-table]");
  if (!table) return;

  const menus = [...document.querySelectorAll(".grid-toolbar details.field-menu")];
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
      .some((node) => node instanceof Element && node.matches(".grid-toolbar .field-menu"));
    if (!insideToolMenu) closeMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenus();
  });

  const body = table.tBodies[0];
  const selectionStatus = document.querySelector("[data-selection-status]");
  const bulkControls = document.querySelector("[data-bulk-controls]");
  const bulkAction = document.querySelector("[data-bulk-action]");
  const bulkSubmit = document.querySelector("[data-bulk-submit]");
  const selectAll = table.querySelector("[data-select-all]");
  const clearSelection = document.querySelector("[data-clear-selection]");
  const rowChecks = [...table.querySelectorAll("[data-row-select]")];
  const { query, layout } = JSON.parse(document.querySelector("[data-list-config]").textContent);
  const { fields, filter, sort, group } = query;
  const storageKey = `obox-list:${window.location.pathname}`;
  const readState = () => {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (Array.isArray(stored)) return { hidden: stored, order: [] };
      return stored && typeof stored === "object" ? stored : {};
    } catch {
      return {};
    }
  };
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const filterSpecs = new Map(fields.map((field) => [field.name, field]));
  const fieldOptions = [...document.querySelectorAll("[data-field-option]")];
  const sortButtons = [...document.querySelectorAll("[data-grid-sort-choice]")];
  const groupButtons = [...document.querySelectorAll("[data-grid-group-choice]")];
  const fieldNames = fieldOptions.map((option) => option.dataset.fieldKey);
  const primaryField = fieldNames[0];
  const displayFields = new Set(fieldNames);
  const stored = readState();
  const viewState = {
    hidden: (Array.isArray(stored.hidden) ? stored.hidden : layout.hidden).filter(
      (field) => field !== primaryField && displayFields.has(field),
    ),
    order: [...new Set(Array.isArray(stored.order) ? stored.order : layout.order)].filter(
      (field) => field !== primaryField && displayFields.has(field),
    ),
    row_height: ["short", "medium", "tall", "extra"].includes(stored.row_height)
      ? stored.row_height
      : layout.row_height,
    row_color: ["blue", "green", "yellow", "red"].includes(stored.row_color)
      ? stored.row_color
      : "",
  };
  const saveState = () => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(viewState));
    } catch {
      /* storage is optional */
    }
  };
  const labelFor = (field) => filterSpecs.get(field)?.label || field.replaceAll("_", " ");
  const setQueryParams = (updates, removals = []) => {
    const url = new URL(window.location.href);
    url.searchParams.delete("page");
    for (const name of removals) url.searchParams.delete(name);
    for (const [name, value] of Object.entries(updates)) url.searchParams.set(name, value);
    window.location.assign(url.href);
  };
  const fieldList = document.querySelector(".field-menu-list");
  const fieldToggles = [...document.querySelectorAll("[data-field-toggle]")];
  const fieldOrder = [
    ...fieldNames.slice(0, 1),
    ...viewState.order,
    ...fieldNames.slice(1).filter((field) => !viewState.order.includes(field)),
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
    viewState.order = fieldOrder.slice(1);
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
      if (!input.disabled) input.checked = false;
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
    if (!target || target === dragField || target.dataset.fieldKey === primaryField) return;
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
      if (next < 1 || next >= fieldOrder.length) return;
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
  const reservedParams = new Set(["page", "filter", "ordering", "group", "q"]);
  const exactFilterParams = [...new URL(window.location.href).searchParams.keys()].filter(
    (name) => !reservedParams.has(name),
  );
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
  const appliedFilter = cleanFilterGroup(filter);
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
    const firstField = fields[0]?.name;
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
      const spec = filterSpecs.get(child.field) || fields[0];
      if (!spec) return;
      field.replaceChildren(...fields.map((item) => new Option(item.label, item.name)));
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
      const value = configureFilterValue(row, spec, selectedOp, child.value);
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
    setQueryParams({ filter: JSON.stringify(nextFilter) }, exactFilterParams);
  });
  filterReset?.addEventListener("click", () => {
    setQueryParams(
      { filter: JSON.stringify({ conjunction: "and", children: [] }) },
      exactFilterParams,
    );
  });
  filterMenu?.addEventListener("toggle", () => {
    if (!filterMenu.open) return;
    draftFilter = clone(appliedFilter);
    if (!draftFilter.children.length && fields.length) {
      const first = fields[0].name;
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
  const sortSpecs = filterSpecs;
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
  const appliedSort = sort
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
  const groupSpecs = filterSpecs;
  const appliedGroup = group
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

  const updateSelection = () => {
    const selected = rowChecks.filter((input) => input.checked);
    const selectable = rowChecks.filter((input) => !input.disabled);
    if (selectionStatus) {
      const total = Number(selectionStatus.dataset.totalCount);
      selectionStatus.textContent = selected.length
        ? `${selected.length} selected`
        : `${total} ${total === 1 ? "record" : "records"}`;
    }
    if (clearSelection) clearSelection.hidden = selected.length === 0;
    if (selectAll) {
      selectAll.checked = selectable.length > 0 && selected.length === selectable.length;
      selectAll.indeterminate = selected.length > 0 && !selectAll.checked;
    }
    if (bulkSubmit) bulkSubmit.disabled = selected.length === 0 || !bulkAction?.value;
    rowChecks.forEach((input) => {
      input.closest("tr")?.setAttribute("data-selected", String(input.checked));
    });
    if (!selected.length && bulkAction) bulkAction.value = "";
  };
  selectAll?.addEventListener("change", () => {
    rowChecks.forEach((input) => {
      if (!input.disabled) input.checked = selectAll.checked;
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
    if (!rowChecks.some((input) => input.checked) || !bulkAction?.value) event.preventDefault();
  });

  const headings = [...body.querySelectorAll("[data-group-toggle]")];
  const toggleGroup = (button, expanded) => {
    button.setAttribute("aria-expanded", String(expanded));
    button.querySelector(".group-caret").textContent = expanded ? "▾" : "▸";
  };
  const updateGroups = () => {
    let collapsedDepth = 0;
    for (const row of body.rows) {
      const depth = Number(row.dataset.groupDepth);
      if (depth && depth <= collapsedDepth) collapsedDepth = 0;
      row.hidden = collapsedDepth > 0;
      if (
        depth &&
        !row.hidden &&
        row.querySelector("[data-group-toggle]").getAttribute("aria-expanded") === "false"
      ) {
        collapsedDepth = depth;
      }
    }
  };
  headings.forEach((button) => {
    button.addEventListener("click", () => {
      toggleGroup(button, button.getAttribute("aria-expanded") === "false");
      updateGroups();
    });
  });
  for (const [attribute, expanded] of [
    ["collapse", false],
    ["expand", true],
  ]) {
    groupMenu?.querySelector(`[data-group-${attribute}-all]`)?.addEventListener("click", () => {
      for (const button of headings) toggleGroup(button, expanded);
      updateGroups();
    });
  }
  renderSort();
  renderGroup();
  renderFilter();
  renderFilterCount();
  updateSelection();

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

  table.dataset.rowHeight = viewState.row_height;
  if (viewState.row_color) table.dataset.rowColor = viewState.row_color;
  document.querySelectorAll("[data-row-height-choice]").forEach((button) => {
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.rowHeightChoice === viewState.row_height),
    );
    button.addEventListener("click", () => {
      viewState.row_height = table.dataset.rowHeight = button.dataset.rowHeightChoice;
      saveState();
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
      viewState.row_color = table.dataset.rowColor || "";
      saveState();
      closeMenus();
    });
  });
})();
