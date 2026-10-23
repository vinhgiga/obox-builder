(() => {
  "use strict";
  const form = document.querySelector("#list-query");
  const config = document.querySelector("#list-query-config");
  if (!form || !config) return;
  const state = JSON.parse(config.textContent);
  const fields = state.fields;
  const field = (name) => fields.find((item) => item.name === name);
  const input = (name) => form.elements.namedItem(name);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const button = (label, action) => {
    const node = element("button", "btn btn-sm", label);
    node.type = "button";
    node.addEventListener("click", () => {
      action();
      render();
    });
    return node;
  };
  const select = (label, options, value, update) => {
    const node = element("select", "form-select w-auto");
    node.setAttribute("aria-label", label);
    options.forEach(([key, text]) => {
      const option = new Option(text, key);
      option.selected = String(key) === String(value);
      node.add(option);
    });
    node.addEventListener("change", () => {
      const position = [...form.querySelectorAll("select")].indexOf(node);
      update(node.value);
      render();
      form.querySelectorAll("select")[position]?.focus();
    });
    return node;
  };
  const serialize = () => {
    input("filter").value = JSON.stringify(state.filter);
    for (const kind of ["sort", "group"]) {
      input(kind).value = state[kind]
        .map((term) => `${term.descending ? "-" : ""}${term.field}`)
        .join(",");
    }
  };
  const condition = (term, remove) => {
    const row = element("div", "d-flex flex-wrap align-items-center gap-2 mb-2");
    row.append(
      select(
        "Filter field",
        fields.filter((item) => item.operators.length).map((item) => [item.name, item.label]),
        term.field,
        (name) => {
          term.field = name;
          term.op = field(name).operators[0].name;
          term.value = "";
        },
      ),
    );
    const spec = field(term.field);
    row.append(
      select(
        "Filter operator",
        spec.operators.map((op) => [op.name, op.label]),
        term.op,
        (op) => {
          term.op = op;
          term.value = "";
        },
      ),
    );
    if (term.op === "isnull") {
      row.append(
        select(
          "Empty value",
          [
            ["true", "Is empty"],
            ["false", "Is not empty"],
          ],
          term.value === "" ? "true" : String(term.value),
          (value) => {
            term.value = value;
          },
        ),
      );
      if (term.value === "") term.value = "true";
    } else if (term.op !== "in" && (spec.type === "checkbox" || spec.choices.length)) {
      const choices =
        spec.type === "checkbox"
          ? [
              ["true", "Yes"],
              ["false", "No"],
            ]
          : spec.choices;
      row.append(
        select("Filter value", [["", "Choose a value"], ...choices], term.value, (value) => {
          term.value = value;
        }),
      );
    } else {
      const value = element("input", "form-control w-auto");
      value.type =
        term.op === "in"
          ? "text"
          : { date: "date", time: "time", integer: "number", decimal: "number" }[spec.type] ||
            "text";
      if (value.type === "number") value.step = spec.type === "integer" ? "1" : "any";
      value.value = Array.isArray(term.value) ? term.value.join(",") : term.value;
      value.placeholder =
        term.op === "in"
          ? "Values, separated by commas"
          : spec.type === "datetime"
            ? "2026-10-23T09:30:00+07:00"
            : "Value";
      value.setAttribute("aria-label", "Filter value");
      value.addEventListener("input", () => {
        term.value = value.value;
        serialize();
      });
      row.append(value);
    }
    row.append(button("Remove condition", remove));
    return row;
  };
  const group = (node, depth, remove) => {
    const box = element("div", "border rounded p-2 mb-2");
    const toolbar = element("div", "d-flex flex-wrap gap-2 mb-2");
    toolbar.append(
      select(
        "Match conditions",
        [
          ["and", "All conditions (AND)"],
          ["or", "Any condition (OR)"],
        ],
        node.conjunction,
        (value) => {
          node.conjunction = value;
        },
      ),
    );
    const available = fields.find((item) => item.operators.length);
    if (available)
      toolbar.append(
        button("Add condition", () => {
          node.children.push({
            field: available.name,
            op: available.operators[0].name,
            value: "",
          });
        }),
      );
    if (depth < 3)
      toolbar.append(
        button("Add group", () => {
          node.children.push({ conjunction: "and", children: [] });
        }),
      );
    if (remove) toolbar.append(button("Remove group", remove));
    box.append(toolbar);
    node.children.forEach((child, index) => {
      const removeChild = () => {
        node.children.splice(index, 1);
      };
      box.append(
        child.children ? group(child, depth + 1, removeChild) : condition(child, removeChild),
      );
    });
    return box;
  };
  const ordering = (kind, max) => {
    const box = form.querySelector(`[data-query-${kind}]`);
    box.replaceChildren();
    const terms = state[kind];
    const available = fields.filter((item) => item.sortable);
    terms.forEach((term, index) => {
      const row = element("div", "d-flex flex-wrap gap-2 mb-2");
      row.append(
        select(
          `${kind} field ${index + 1}`,
          available
            .filter(
              (item) =>
                item.name === term.field || !terms.some((other) => other.field === item.name),
            )
            .map((item) => [item.name, item.label]),
          term.field,
          (value) => {
            term.field = value;
          },
        ),
      );
      row.append(
        select(
          `${kind} direction ${index + 1}`,
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
      if (index)
        row.append(
          button("Move up", () => {
            [terms[index - 1], terms[index]] = [term, terms[index - 1]];
          }),
        );
      if (index < terms.length - 1)
        row.append(
          button("Move down", () => {
            [terms[index + 1], terms[index]] = [term, terms[index + 1]];
          }),
        );
      row.append(
        button("Remove", () => {
          terms.splice(index, 1);
        }),
      );
      box.append(row);
    });
    const next = available.find((item) => !terms.some((term) => term.field === item.name));
    if (terms.length < max && next)
      box.append(
        button(`Add ${kind}`, () => {
          terms.push({ field: next.name, descending: false });
        }),
      );
  };
  const conditionCount = (node) =>
    node.children.reduce((total, child) => total + (child.children ? conditionCount(child) : 1), 0);
  const render = () => {
    form.querySelector('[data-query-panel="filter"] summary').textContent =
      `Filter (${conditionCount(state.filter)})`;
    for (const kind of ["sort", "group"]) {
      form.querySelector(`[data-query-panel="${kind}"] summary`).textContent =
        `${kind === "sort" ? "Sort" : "Group"} (${state[kind].length})`;
    }
    form.querySelector("[data-query-filters]").replaceChildren(group(state.filter, 1));
    ordering("sort", 5);
    ordering("group", 3);
    serialize();
  };
  render();
  form.addEventListener("submit", serialize);
  document.querySelector("[data-save-view]")?.addEventListener("submit", (event) => {
    serialize();
    const url = new URL(event.currentTarget.action);
    url.search = new URLSearchParams(new FormData(form)).toString();
    event.currentTarget.action = url.toString();
  });

  // A header inherits its parent path; records inherit the complete group path.
  const path = [];
  let counter = 0;
  const groupedRows = [];
  const collapsed = new Set();
  document.querySelectorAll("[data-group-header], [data-group-row]").forEach((row) => {
    if (row.hasAttribute("data-group-header")) {
      const level = Number(row.dataset.groupHeader);
      path.length = level - 1;
      row.groupParents = [...path];
      const id = ++counter;
      path.push(id);
      row.querySelector("[data-group-toggle]").addEventListener("click", (event) => {
        if (collapsed.has(id)) collapsed.delete(id);
        else collapsed.add(id);
        event.currentTarget.setAttribute("aria-expanded", String(!collapsed.has(id)));
        groupedRows.forEach((item) => {
          item.hidden = item.groupParents.some((key) => collapsed.has(key));
        });
      });
    } else row.groupParents = [...path];
    groupedRows.push(row);
  });
})();
