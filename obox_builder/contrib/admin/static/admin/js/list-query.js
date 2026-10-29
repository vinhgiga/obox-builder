(function initListQuery() {
  "use strict";
  const gettext = window.oboxGettext;
  document.addEventListener("admin:load", initListQuery);
  const form = document.querySelector("#list-query");
  const config = document.querySelector("#list-query-config");
  if (!form || !config) return;
  const state = JSON.parse(config.textContent);
  const appliedSortFields = new Set(state.sort.map((term) => term.field));
  const initiallyGrouped = state.group.length > 0;
  const fields = state.fields;
  const field = (name) => fields.find((item) => item.name === name);
  const input = (name) => form.elements.namedItem(name);
  const sortPanel = form.querySelector('[data-query-panel="sort"]');
  const sortPreferenceKey = `admin-sort-auto:${form.action}:${input("view")?.value || ""}`;
  let automaticSort = true;
  try {
    automaticSort = localStorage.getItem(sortPreferenceKey) !== "false";
  } catch {}
  const initialField =
    fields.find((item) => item.type === "text" && item.operators.length) ||
    fields.find((item) => item.operators.length);
  const defaultOperator = (spec) =>
    spec.operators.find((operator) => operator.name === "contains") || spec.operators[0];
  if (initialField && !state.filter.children.length) {
    state.filter.children.push({
      field: initialField.name,
      op: defaultOperator(initialField).name,
      value: "",
      draft: true,
    });
  }
  const termIds = new WeakMap();
  let nextTermId = 0;
  let activePicker = null;
  let showAllGroupFields = false;
  let groupFieldSearch = "";
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = gettext(text);
    return node;
  };
  const button = (label, action) => {
    const node = element("button", "btn btn-sm", label);
    node.type = "button";
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      action();
      render();
    });
    return node;
  };
  const iconButton = (icon, label, action, className = "") => {
    const node = element("button", `btn btn-sm admin-filter-icon-button ${className}`.trim());
    node.type = "button";
    node.setAttribute("aria-label", gettext(label));
    node.title = gettext(label);
    const glyph = element("i", `ti ti-${icon}`);
    glyph.setAttribute("aria-hidden", "true");
    node.append(glyph);
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      action();
      render();
    });
    return node;
  };
  const select = (label, options, value, update) => {
    const node = element("select", "form-select w-auto");
    node.setAttribute("aria-label", gettext(label));
    options.forEach(([key, text]) => {
      const option = new Option(gettext(text), key);
      option.selected = String(key) === String(value);
      node.add(option);
    });
    node.addEventListener("change", (event) => {
      event.stopPropagation();
      const position = [...form.querySelectorAll("select")].indexOf(node);
      update(node.value);
      render();
      form.querySelectorAll("select")[position]?.focus();
    });
    return node;
  };
  const serialize = () => {
    const persisted = (node) => ({
      conjunction: node.conjunction,
      children: node.children.flatMap((child) => {
        if (!child.children) {
          return child.draft ? [] : [{field: child.field, op: child.op, value: child.value}];
        }
        const group = persisted(child);
        return group.children.length ? [group] : [];
      }),
    });
    input("filter").value = JSON.stringify(persisted(state.filter));
    for (const kind of ["sort", "group"]) {
      input(kind).value = state[kind]
        .map((term) => `${term.descending ? "-" : ""}${term.field}`)
        .join(",");
    }
  };
  const termId = (term) => {
    if (!termIds.has(term)) termIds.set(term, `filter-${++nextTermId}`);
    return termIds.get(term);
  };
  const closePicker = (picker, returnFocus = false) => {
    if (!picker) return;
    picker.open = false;
    if (activePicker === picker) activePicker = null;
    if (returnFocus) picker.querySelector("summary")?.focus();
  };
  const positionPicker = (picker) => {
    if (!picker?.open) return;
    const summary = picker.querySelector("summary");
    const menu = picker.querySelector(".admin-filter-field-menu");
    if (!summary || !menu) return;
    const rect = summary.getBoundingClientRect();
    const width = Math.min(picker.classList.contains("admin-sort-field-picker") ? 420 : 360, window.innerWidth - 16);
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    const below = Math.max(0, window.innerHeight - rect.bottom - 12);
    const above = Math.max(0, rect.top - 12);
    const openBelow = below >= 180 || below >= above;
    const height = Math.min(340, openBelow ? below : above);
    menu.style.width = `${width}px`;
    menu.style.maxHeight = `${height}px`;
    const top = openBelow ? rect.bottom + 4
      : Math.max(8, rect.top - menu.getBoundingClientRect().height - 4);
    menu.style.inset = `${top}px auto auto ${left}px`;
  };
  const pickerChanged = (picker) => {
    if (picker.open) {
      if (activePicker && activePicker !== picker) closePicker(activePicker);
      activePicker = picker;
      positionPicker(picker);
      requestAnimationFrame(() => picker.querySelector("input")?.focus());
    } else if (activePicker === picker) {
      activePicker = null;
    }
    picker.querySelector("summary")?.setAttribute("aria-expanded", String(picker.open));
  };
  document.addEventListener("click", (event) => {
    if (activePicker && !activePicker.contains(event.target)) closePicker(activePicker);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && activePicker) {
      event.preventDefault();
      event.stopPropagation();
      closePicker(activePicker, true);
    }
  });
  window.addEventListener("resize", () => positionPicker(activePicker));
  document.addEventListener("scroll", () => positionPicker(activePicker), true);
  const fieldIcon = (type) => ({
    text: "letter-a",
    long_text: "letter-a",
    email: "mail",
    url: "link",
    phone: "phone",
    checkbox: "checkbox",
    integer: "number-123",
    decimal: "number-123",
    single_select: "list-details",
    date: "calendar",
    datetime: "calendar-time",
    time: "clock",
    json: "code",
  })[type] || "abc";
  const fieldPicker = (term) => {
    const id = termId(term);
    const picker = element("details", "admin-filter-field-picker");
    picker.dataset.fieldPicker = id;
    const summary = element("summary", "admin-filter-field-trigger");
    summary.setAttribute("aria-controls", `${id}-options`);
    summary.setAttribute("aria-expanded", "false");
    summary.setAttribute("aria-label", gettext("Filter by %(field)s", {field: field(term.field).label}));
    const glyph = element("i", `ti ti-${fieldIcon(field(term.field).type)}`);
    glyph.setAttribute("aria-hidden", "true");
    summary.append(glyph, element("span", "admin-filter-field-label", field(term.field).label));
    const chevron = element("i", "ti ti-chevron-down");
    chevron.setAttribute("aria-hidden", "true");
    summary.append(chevron);

    const menu = element("div", "admin-filter-field-menu");
    const search = element("input", "form-control form-control-sm");
    search.type = "search";
    search.placeholder = gettext("Find a field");
    search.setAttribute("aria-label", gettext("Find a field"));
    search.autocomplete = "off";
    const options = element("div", "admin-filter-field-options");
    options.id = `${id}-options`;
    options.setAttribute("role", "group");
    options.setAttribute("aria-label", gettext("Filter fields"));
    const available = fields.filter((item) => item.operators.length);
    const optionButtons = available.map((spec) => {
      const option = element("button", "admin-filter-field-option");
      option.type = "button";
      option.setAttribute("aria-pressed", String(spec.name === term.field));
      option.dataset.fieldLabel = spec.label.toLocaleLowerCase();
      const optionIcon = element("i", `ti ti-${fieldIcon(spec.type)}`);
      optionIcon.setAttribute("aria-hidden", "true");
      option.append(optionIcon, element("span", "", spec.label));
      option.addEventListener("click", (event) => {
        event.stopPropagation();
        closePicker(picker);
        term.field = spec.name;
        term.op = defaultOperator(spec).name;
        term.value = "";
        render();
        form.querySelector(`[data-field-picker="${id}"] > summary`)?.focus();
      });
      options.append(option);
      return option;
    });
    const empty = element("p", "text-secondary small mb-0 d-none", "No fields found.");
    search.addEventListener("input", () => {
      const query = search.value.trim().toLocaleLowerCase();
      optionButtons.forEach((option) => {
        option.hidden = !option.dataset.fieldLabel.includes(query);
      });
      empty.classList.toggle("d-none", optionButtons.some((option) => !option.hidden));
    });
    menu.append(search, options, empty);
    picker.append(summary, menu);
    picker.addEventListener("toggle", () => pickerChanged(picker));
    return picker;
  };
  const condition = (term, siblings, index, conjunction) => {
    const id = termId(term);
    const row = element("div", "admin-filter-row");
    row.dataset.filterTerm = id;
    const join = index === 0
      ? element("span", "admin-filter-join-label", "Where")
      : select("Condition join", [["and", "And"], ["or", "Or"]], conjunction, (value) => {
        const parent = row.closest("[data-filter-group]");
        if (parent) parent.filterGroup.conjunction = value;
      });
    join.classList.add("admin-filter-join-control");
    row.append(join);
    row.append(fieldPicker(term));
    const spec = field(term.field);
    const operator = select(
        "Filter operator",
        spec.operators.map((op) => [op.name, op.label]),
        term.op,
        (op) => {
          term.op = op;
          term.value = op === "isnull" ? "true" : "";
          if (op === "isnull") term.draft = false;
        },
      );
    operator.classList.add("admin-filter-operator");
    row.append(operator);
    if (term.op === "isnull") {
      const value = select(
          "Empty value",
          [
            ["true", "Is empty"],
            ["false", "Is not empty"],
          ],
          term.value === "" ? "true" : String(term.value),
          (value) => {
            term.value = value;
            if (value !== "") term.draft = false;
          },
        );
      value.classList.add("admin-filter-value");
      row.append(value);
      if (term.value === "") term.value = "true";
    } else if (term.op !== "in" && (spec.type === "checkbox" || spec.choices.length)) {
      const choices =
        spec.type === "checkbox"
          ? [
              ["true", "Yes"],
              ["false", "No"],
            ]
          : spec.choices;
      const value = select("Filter value", [["", "Choose a value"], ...choices], term.value, (value) => {
          term.value = value;
          if (value !== "") term.draft = false;
        });
      value.classList.add("admin-filter-value");
      row.append(value);
    } else {
      const value = element("input", "form-control w-auto");
      value.classList.add("admin-filter-value");
      value.type =
        term.op === "in"
          ? "text"
          : { date: "date", time: "time", integer: "number", decimal: "number" }[spec.type] ||
            "text";
      if (value.type === "number") value.step = spec.type === "integer" ? "1" : "any";
      value.value = Array.isArray(term.value) ? term.value.join(",") : term.value;
      value.placeholder =
        term.op === "in"
          ? gettext("Values, separated by commas")
          : spec.type === "datetime"
            ? "2026-10-23T09:30:00+07:00"
            : gettext("Value");
      value.setAttribute("aria-label", gettext("Filter value"));
      value.addEventListener("input", () => {
        term.value = value.value;
        if (value.value !== "") term.draft = false;
        serialize();
        form.querySelector('[data-query-panel="filter"] [data-query-count]').textContent =
          filterCount();
      });
      row.append(value);
    }
    const handle = element("button", "btn btn-sm admin-filter-icon-button admin-filter-grip");
    handle.type = "button";
    handle.setAttribute("aria-label", gettext("Move condition with Alt+Up or Alt+Down"));
    handle.title = gettext("Drag to reorder; use Alt+Up or Alt+Down");
    const gripIcon = element("i", "ti ti-grip-vertical");
    gripIcon.setAttribute("aria-hidden", "true");
    handle.append(gripIcon);
    handle.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const next = index + (event.key === "ArrowUp" ? -1 : 1);
      if (next < 0 || next >= siblings.length) return;
      [siblings[index], siblings[next]] = [siblings[next], siblings[index]];
      render();
      form.querySelector(`[data-filter-term="${id}"] .admin-filter-grip`)?.focus();
    });
    handle.draggable = true;
    handle.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", id);
      event.dataTransfer.effectAllowed = "move";
      row.classList.add("is-dragging");
    });
    row.addEventListener("dragover", (event) => event.preventDefault());
    row.addEventListener("dragend", () => row.classList.remove("is-dragging"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      const from = siblings.findIndex((item) => termId(item) === event.dataTransfer.getData("text/plain"));
      if (from < 0 || from === index) return;
      const [moved] = siblings.splice(from, 1);
      siblings.splice(index, 0, moved);
      render();
      form.querySelector(`[data-filter-term="${id}"] .admin-filter-grip`)?.focus();
    });
    row.append(iconButton("trash", "Delete condition", () => {
      const position = siblings.indexOf(term);
      if (position >= 0) siblings.splice(position, 1);
    }, "admin-filter-delete"), handle);
    return row;
  };
  const group = (node, depth, remove) => {
    const root = depth === 1;
    const box = element("div", root ? "admin-filter-group" : "admin-filter-group admin-filter-nested-group");
    box.filterGroup = node;
    box.dataset.filterGroup = "";
    if (root) box.append(element("p", "admin-filter-heading", "In this view, show records"));
    else box.append(element("p", "admin-filter-group-heading", "Group conditions"));
    const children = element("div", "admin-filter-conditions");
    const available = initialField;
    node.children.forEach((child, index) => {
      if (child.children) children.append(group(child, depth + 1, () => node.children.splice(index, 1)));
      else children.append(condition(child, node.children, index, node.conjunction));
    });
    if (!node.children.length) children.append(element("p", "admin-filter-empty", "No conditions yet."));
    box.append(children);
    const actions = element("div", "admin-filter-actions");
    if (available) {
      const addCondition = button("+ Add condition", () => {
          node.children.push({
            field: available.name,
            op: defaultOperator(available).name,
            value: "",
            draft: true,
          });
        });
      addCondition.dataset.queryAction = "add-condition";
      actions.append(addCondition);
    }
    if (depth < 3) {
      const addGroup = button("+ Add group", () => {
          node.children.push({ conjunction: "and", children: [] });
        });
      addGroup.dataset.queryAction = "add-group";
      actions.append(addGroup);
    }
    if (remove) actions.append(iconButton("trash", "Delete group", remove, "admin-filter-delete"));
    box.append(actions);
    return box;
  };
  const applySort = () => {
    serialize();
    document.body.dataset.sortMenu = form.action;
    form.requestSubmit();
  };
  const sortChanged = () => {
    render();
    if (automaticSort) applySort();
    else sortPanel.querySelector(".admin-sort-field-picker > summary, .admin-sort-search")?.focus();
  };
  const sortFields = (current, choose) => {
    const box = element("div", "admin-sort-fields");
    const searchRow = element("div", "admin-sort-search-row");
    const glyph = element("i", "ti ti-search");
    glyph.setAttribute("aria-hidden", "true");
    const search = element("input", "form-control form-control-sm admin-sort-search");
    search.type = "search";
    search.placeholder = gettext("Find a field");
    search.setAttribute("aria-label", gettext("Find a sort field"));
    search.autocomplete = "off";
    const list = element("div", "admin-sort-field-list");
    list.id = `sort-field-options-${current || "new"}`;
    search.setAttribute("aria-controls", list.id);
    list.setAttribute("role", "group");
    list.setAttribute("aria-label", gettext("Fields available for sorting"));
    fields.filter((spec) => spec.sortable &&
      (spec.name === current || !state.sort.some((term) => term.field === spec.name))
    ).forEach((spec) => {
      const option = element("button", "admin-sort-field-option", spec.label);
      option.type = "button";
      const icon = element("i", `ti ti-${queryFieldIcon(spec)}`);
      icon.setAttribute("aria-hidden", "true");
      option.prepend(icon);
      option.addEventListener("click", (event) => {
        event.stopPropagation();
        choose(spec.name);
        sortChanged();
      });
      list.append(option);
    });
    const empty = element("p", "admin-group-empty", "No fields found.");
    empty.setAttribute("role", "status");
    const filter = () => {
      const query = search.value.trim().toLocaleLowerCase();
      [...list.children].forEach((option) => {
        option.classList.toggle("d-none", !option.textContent.toLocaleLowerCase().includes(query));
      });
      empty.classList.toggle("d-none", [...list.children].some((option) => !option.classList.contains("d-none")));
    };
    search.addEventListener("input", filter);
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault();
    });
    filter();
    searchRow.append(glyph, search);
    box.append(searchRow, list, empty);
    return box;
  };
  const sortPicker = (label, current, choose, add = false) => {
    const picker = element("details", "admin-filter-field-picker admin-sort-field-picker");
    const summary = element("summary", add ? "admin-sort-add" : "admin-filter-field-trigger");
    summary.setAttribute("aria-label", gettext(label));
    summary.setAttribute("aria-expanded", "false");
    if (add) {
      const plus = element("i", "ti ti-plus");
      plus.setAttribute("aria-hidden", "true");
      summary.append(plus);
    }
    summary.append(element("span", "admin-filter-field-label", label));
    if (!add) {
      const chevron = element("i", "ti ti-chevron-down");
      chevron.setAttribute("aria-hidden", "true");
      summary.append(chevron);
    }
    const menu = sortFields(current, choose);
    menu.id = `sort-field-picker-${current || "new"}`;
    summary.setAttribute("aria-controls", menu.id);
    menu.classList.add("admin-filter-field-menu");
    picker.append(summary, menu);
    picker.addEventListener("toggle", () => pickerChanged(picker));
    return picker;
  };
  const sorting = () => {
    const box = form.querySelector("[data-query-sort]");
    box.replaceChildren();
    const available = fields.filter((spec) => spec.sortable);
    document.querySelectorAll("[data-list-column]").forEach((cell) => {
      cell.classList.toggle("is-sorted", automaticSort && appliedSortFields.has(cell.dataset.listColumn));
    });
    sortPanel.classList.toggle("is-sorted", state.sort.length > 0);
    sortPanel.querySelector("[data-sort-label]").textContent = state.sort.length
      ? gettext(state.sort.length === 1 ? "Sorted by %(count)s field" : "Sorted by %(count)s fields", {count: state.sort.length}) : gettext("Sort");
    if (!state.sort.length) {
      box.append(available.length
        ? sortFields(null, (name) => state.sort.push({field: name, descending: false}))
        : element("p", "admin-group-empty", "No fields can be sorted."));
      if (automaticSort || !appliedSortFields.size) return;
    }
    state.sort.forEach((term, index) => {
      const spec = field(term.field);
      const row = element("div", "admin-sort-row");
      row.dataset.sortField = term.field;
      row.append(sortPicker(spec.label, term.field, (name) => { term.field = name; }));
      const labels = ["integer", "decimal"].includes(spec.type) ? ["1 → 9", "9 → 1"]
        : ["date", "datetime", "time"].includes(spec.type) ? ["Earliest → Latest", "Latest → Earliest"]
          : spec.type === "checkbox" ? ["Unchecked → Checked", "Checked → Unchecked"] : ["A → Z", "Z → A"];
      const direction = select(gettext("Sort direction for %(field)s", {field: spec.label}),
        [["asc", labels[0]], ["desc", labels[1]]], term.descending ? "desc" : "asc",
        (value) => { term.descending = value === "desc"; });
      direction.addEventListener("change", () => { if (automaticSort) applySort(); });
      row.append(direction);
      const move = (next) => {
        if (next < 0 || next >= state.sort.length || next === index) return;
        const [moved] = state.sort.splice(index, 1);
        state.sort.splice(next, 0, moved);
        sortChanged();
        [...sortPanel.querySelectorAll("[data-sort-field]")]
          .find((item) => item.dataset.sortField === term.field)?.querySelector(".admin-sort-grip")?.focus();
      };
      const grip = element("button", "admin-sort-icon admin-sort-grip");
      grip.type = "button";
      grip.setAttribute("aria-label", gettext("Move %(field)s sort with Alt+Up or Alt+Down", {field: spec.label}));
      grip.title = gettext("Drag to reorder; use Alt+Up or Alt+Down");
      const glyph = element("i", "ti ti-grip-vertical");
      glyph.setAttribute("aria-hidden", "true");
      grip.append(glyph);
      grip.draggable = true;
      grip.addEventListener("keydown", (event) => {
        if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        move(index + (event.key === "ArrowUp" ? -1 : 1));
      });
      grip.addEventListener("dragstart", (event) => {
        event.dataTransfer.setData("text/plain", term.field);
        event.dataTransfer.effectAllowed = "move";
      });
      row.addEventListener("dragover", (event) => event.preventDefault());
      row.addEventListener("drop", (event) => {
        event.preventDefault();
        const from = state.sort.findIndex((item) => item.field === event.dataTransfer.getData("text/plain"));
        if (from < 0 || from === index) return;
        const [moved] = state.sort.splice(from, 1);
        state.sort.splice(index, 0, moved);
        sortChanged();
      });
      const remove = element("button", "admin-sort-icon");
      remove.type = "button";
      remove.setAttribute("aria-label", gettext("Remove %(field)s sort", {field: spec.label}));
      const cross = element("i", "ti ti-x");
      cross.setAttribute("aria-hidden", "true");
      remove.append(cross);
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        state.sort.splice(index, 1);
        sortChanged();
      });
      row.append(remove, grip);
      box.append(row);
    });
    if (state.sort.length && state.sort.length < 5 && available.some((spec) => !state.sort.some((term) => term.field === spec.name))) {
      box.append(sortPicker("Add another sort", null,
        (name) => state.sort.push({field: name, descending: false}), true));
    }
    const footer = element("div", "admin-sort-footer");
    const label = element("label", "form-check form-switch mb-0");
    const automatic = element("input", "form-check-input");
    automatic.type = "checkbox";
    automatic.setAttribute("role", "switch");
    automatic.checked = automaticSort;
    automatic.addEventListener("change", () => {
      automaticSort = automatic.checked;
      try { localStorage.setItem(sortPreferenceKey, String(automaticSort)); } catch {}
      sorting();
      if (automaticSort) applySort();
      else sortPanel.querySelector('[role="switch"]')?.focus();
    });
    label.append(automatic, element("span", "form-check-label", "Automatically sort records"));
    footer.append(label);
    if (!automaticSort) {
      const apply = element("button", "btn btn-sm btn-primary", "Sort");
      apply.type = "button";
      apply.addEventListener("click", applySort);
      footer.append(apply);
    }
    box.append(footer);
  };
  const queryFieldIcon = (spec) =>
    spec.choices.length || spec.type === "single_select"
      ? "circle-chevron-down"
      : spec.name.endsWith("_id")
        ? "relation-one-to-many"
        : fieldIcon(spec.type);
  const grouping = () => {
    const box = form.querySelector("[data-query-group]");
    box.replaceChildren();
    const available = fields.filter((item) => item.sortable);
    const selected = new Set(state.group.map((term) => term.field));
    const preferred = available.filter(
      (item) =>
        item.choices.length ||
        item.type === "single_select" ||
        item.name.endsWith("_id"),
    );
    const secondary = available.filter(
      (item) => ["checkbox", "date", "datetime"].includes(item.type),
    );
    const suggestions = [...new Set([...preferred, ...secondary, ...available])].slice(0, 4);
    if (!showAllGroupFields) {
      box.append(element("p", "admin-group-subtitle", "Pick a field to group by"));
    }

    if (state.group.length) {
      box.append(element("p", "admin-group-selected-heading", "Grouped by"));
      state.group.forEach((term, index) => {
        const spec = field(term.field);
        if (!spec) return;
        const row = element("div", "admin-group-selected-row");
        const glyph = element("i", `ti ti-${queryFieldIcon(spec)}`);
        glyph.setAttribute("aria-hidden", "true");
        row.append(glyph, element("span", "admin-group-selected-label", spec.label));
        row.append(
          select(
            gettext("Group direction for %(field)s", {field: spec.label}),
            [
              ["asc", "Ascending"],
              ["desc", "Descending"],
            ],
            term.descending ? "desc" : "asc",
            (value) => {
              term.descending = value === "desc";
            },
          ),
        );
        if (index > 0) {
          row.append(iconButton("chevron-up", gettext("Move %(field)s group up", {field: spec.label}), () => {
            [state.group[index - 1], state.group[index]] = [term, state.group[index - 1]];
          }, "admin-group-icon-button"));
        }
        if (index < state.group.length - 1) {
          row.append(iconButton("chevron-down", gettext("Move %(field)s group down", {field: spec.label}), () => {
            [state.group[index + 1], state.group[index]] = [term, state.group[index + 1]];
          }, "admin-group-icon-button"));
        }
        row.append(iconButton("trash", gettext("Remove %(field)s grouping", {field: spec.label}), () => {
          state.group.splice(index, 1);
        }, "admin-group-icon-button admin-filter-delete"));
        box.append(row);
      });
    }

    const visible = (showAllGroupFields ? available : suggestions).filter(
      (spec) => !selected.has(spec.name),
    );
    const list = element("div", "admin-group-field-list");
    list.id = "group-field-options";
    list.setAttribute("role", "group");
    list.setAttribute("aria-label", gettext("Fields available for grouping"));
    visible.forEach((spec) => {
      const option = element("button", "admin-group-field-option", spec.label);
      option.type = "button";
      option.disabled = state.group.length >= 3;
      const glyph = element("i", `ti ti-${queryFieldIcon(spec)}`);
      glyph.setAttribute("aria-hidden", "true");
      option.prepend(glyph);
      option.addEventListener("click", (event) => {
        event.stopPropagation();
        if (state.group.length >= 3 || selected.has(spec.name)) return;
        state.group.push({ field: spec.name, descending: false });
        render();
        form.querySelectorAll(".admin-group-selected-row select")[state.group.length - 1]?.focus();
      });
      list.append(option);
    });
    box.append(list);
    if (showAllGroupFields) {
      const searchRow = element("div", "admin-group-search-row");
      const searchIcon = element("i", "ti ti-search");
      searchIcon.setAttribute("aria-hidden", "true");
      const search = element("input", "form-control form-control-sm admin-group-search");
      search.type = "search";
      search.placeholder = gettext("Find a field");
      search.setAttribute("aria-label", gettext("Find a grouping field"));
      search.setAttribute("aria-controls", list.id);
      search.autocomplete = "off";
      search.value = groupFieldSearch;
      const empty = element("p", "admin-group-empty d-none", "No fields found.");
      empty.setAttribute("role", "status");
      const filterFields = () => {
        groupFieldSearch = search.value;
        const query = groupFieldSearch.trim().toLocaleLowerCase();
        const options = [...list.children];
        options.forEach((option) => {
          option.classList.toggle("d-none", !option.textContent.toLocaleLowerCase().includes(query));
        });
        empty.classList.toggle("d-none", options.some((option) => !option.classList.contains("d-none")));
      };
      search.addEventListener("input", filterFields);
      search.addEventListener("keydown", (event) => {
        if (event.key === "Enter") event.preventDefault();
      });
      searchRow.append(searchIcon, search);
      box.prepend(searchRow);
      list.after(empty);
      filterFields();
    }
    if (!available.length) box.append(element("p", "admin-group-empty", "No fields can be grouped."));
    else if (state.group.length >= 3) {
      box.append(element("p", "admin-group-empty", "You can group by up to 3 fields."));
    } else if (available.every((spec) => selected.has(spec.name))) {
      box.append(element("p", "admin-group-empty", "All available fields are grouped."));
    }

    const hasMore = available.some((spec) => !suggestions.includes(spec));
    if (hasMore) {
      const toggle = element("button", "admin-group-see-all");
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", String(showAllGroupFields));
      toggle.setAttribute("aria-controls", list.id);
      toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        showAllGroupFields = !showAllGroupFields;
        groupFieldSearch = "";
        grouping();
        box.querySelector(showAllGroupFields ? ".admin-group-search" : ".admin-group-see-all")?.focus();
      });
      const glyph = element("i", `ti ti-chevron-${showAllGroupFields ? "up" : "down"}`);
      glyph.setAttribute("aria-hidden", "true");
      toggle.append(glyph, element("span", "", showAllGroupFields ? "Show fewer fields" : "See all fields"));
      box.append(toggle);
    }
    form.querySelector('[data-query-panel="group"] .admin-query-panel-actions')
      .classList.toggle("d-none", !state.group.length && !initiallyGrouped);
  };
  const conditionCount = (node) =>
    node.children.reduce(
      (total, child) => total + (child.children ? conditionCount(child) : child.draft ? 0 : 1),
      0,
    );
  const filterCount = () =>
    conditionCount(state.filter) +
    [...form.querySelectorAll(".admin-simple-filters select")].filter((select) => select.value)
      .length;
  const render = () => {
    closePicker(activePicker);
    form.querySelector('[data-query-panel="filter"] [data-query-count]').textContent =
      filterCount();
    for (const kind of ["sort", "group"]) {
      form.querySelector(`[data-query-panel="${kind}"] [data-query-count]`).textContent =
        state[kind].length;
    }
    form.querySelector("[data-query-filters]").replaceChildren(group(state.filter, 1));
    sorting();
    grouping();
    serialize();
  };
  form.querySelector('[data-query-panel="group"]').addEventListener("toggle", () => {
    showAllGroupFields = false;
    groupFieldSearch = "";
    grouping();
  });
  render();
  sortPanel.addEventListener("toggle", () => {
    if (sortPanel.open && !state.sort.length) sortPanel.querySelector(".admin-sort-search")?.focus();
  });
  if (document.body.dataset.sortMenu === form.action) {
    delete document.body.dataset.sortMenu;
    sortPanel.open = true;
    requestAnimationFrame(() => sortPanel.querySelector(".admin-sort-field-picker > summary, .admin-sort-search")?.focus());
  } else {
    delete document.body.dataset.sortMenu;
  }
  form.querySelectorAll(".admin-simple-filters select").forEach((select) => {
    select.addEventListener("change", render);
  });
  form.addEventListener("submit", serialize);
  form.addEventListener("admin:serialize-query", serialize);
  document.querySelectorAll("[data-save-query]").forEach((target) => target.addEventListener("submit", (event) => {
    serialize();
    const url = new URL(event.currentTarget.action);
    url.search = new URLSearchParams(new FormData(form)).toString();
    event.currentTarget.action = url.toString();
  }));

  const table = document.querySelector("[data-list-table]");
  const collapsed = new Set();
  let groupedRows = [];
  function groupRows() {
    const path = [];
    groupedRows = table.adminList?.rows || [...table.querySelectorAll("tbody tr")];
    groupedRows.forEach((row) => {
      if (row.hasAttribute("data-group-header")) {
        path.length = Number(row.dataset.groupHeader) - 1;
        row.groupParents = [...path];
        path.push(JSON.stringify([...path, row.dataset.groupKey]));
        row.groupID = path.at(-1);
        row.querySelector("[data-group-toggle]").setAttribute("aria-expanded", String(!collapsed.has(row.groupID)));
      } else row.groupParents = [...path];
      row.hidden = row.groupParents.some((key) => collapsed.has(key));
    });
    table.dispatchEvent(new Event("admin:group"));
  }
  table.addEventListener("admin:rows", groupRows);
  table.addEventListener("click", (event) => {
    const button = event.target.closest("[data-group-toggle]");
    if (!button) return;
    const id = button.closest("tr").groupID;
    if (collapsed.has(id)) collapsed.delete(id);
    else collapsed.add(id);
    groupRows();
  });
  groupRows();
})();
