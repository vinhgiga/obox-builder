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
  const { fields, filter } = JSON.parse(document.querySelector("[data-list-config]").textContent);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const filterSpecs = new Map(fields.map((field) => [field.name, field]));
  const setQueryParams = (updates) => {
    const url = new URL(window.location.href);
    url.searchParams.delete("page");
    for (const [name, value] of Object.entries(updates)) url.searchParams.set(name, value);
    window.location.assign(url.href);
  };

  for (const form of document.querySelectorAll("[data-order-query]")) {
    const fields = [...form.querySelectorAll("[data-order-field]")];
    const directions = [...form.querySelectorAll("[data-order-direction]")];
    const updateOptions = () => {
      const selected = new Set(fields.map((field) => field.value));
      for (const field of fields) {
        for (const option of field.options) {
          option.disabled = Boolean(
            option.value && option.value !== field.value && selected.has(option.value),
          );
        }
      }
    };
    form.addEventListener("change", updateOptions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const terms = fields.flatMap((field, index) =>
        field.value ? [directions[index].value + field.value] : [],
      );
      setQueryParams({ [form.dataset.orderQuery]: terms.join(",") });
    });
    form.querySelector("[data-order-clear]").addEventListener("click", () => {
      setQueryParams({ [form.dataset.orderQuery]: "" });
    });
    updateOptions();
  }

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
  const parseFilterArray = (value) => {
    try {
      const items = Array.isArray(value) ? value : JSON.parse(value);
      return Array.isArray(items) &&
        items.length &&
        items.length <= 100 &&
        items.every((item) => ["string", "number", "boolean"].includes(typeof item))
        ? items
        : null;
    } catch {
      return null;
    }
  };
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
      if (op === "in") {
        const values = parseFilterArray(child.value);
        return values ? [{ field: child.field, op, value: values }] : [];
      }
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
    in: "is one of",
    is_empty: "is empty",
    is_not_empty: "is not empty",
  };
  const configureFilterValue = (row, spec, op, currentValue) => {
    let value = row.querySelector("[data-filter-value]");
    const wantsSelect = op !== "in" && (spec.kind === "choice" || spec.kind === "boolean");
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
    if (op === "in") {
      value.type = "text";
      value.placeholder = 'JSON array, e.g. ["A","B"]';
    }
    value.value = Array.isArray(currentValue) ? JSON.stringify(currentValue) : (currentValue ?? "");
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
    setQueryParams({ filter: JSON.stringify(nextFilter) });
  });
  filterReset?.addEventListener("click", () => {
    setQueryParams({ filter: "" });
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
    document.querySelector(`[data-group-${attribute}-all]`)?.addEventListener("click", () => {
      for (const button of headings) toggleGroup(button, expanded);
      updateGroups();
    });
  }
  renderFilter();
})();
