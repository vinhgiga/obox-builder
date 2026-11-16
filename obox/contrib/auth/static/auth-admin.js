(() => {
  const boxes = (root, level) =>
    Array.from(root.querySelectorAll(`[data-tree-level="${level}"]`));

  const setState = (input, state) => {
    input.checked = state === "all";
    input.indeterminate = state === "some";
  };

  const stateOf = (actions) => {
    const checked = actions.filter((action) => action.checked).length;
    if (!actions.length || checked === 0) return "none";
    return checked === actions.length ? "all" : "some";
  };

  for (const form of document.querySelectorAll("[data-permission-form]")) {
    const sync = () => {
      for (const resource of boxes(form, "resource")) {
        const details = resource.closest("details");
        if (!details) continue;
        setState(resource, stateOf(boxes(details, "action")));
      }
      for (const service of boxes(form, "service")) {
        const details = service.closest("details");
        if (!details) continue;
        setState(service, stateOf(boxes(details, "action")));
      }
    };

    sync();
    form.addEventListener("change", (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.matches("[data-tree-node]")) {
        return;
      }
      if (input.dataset.treeLevel !== "action") {
        const details = input.closest("details");
        if (details) {
          for (const action of boxes(details, "action")) action.checked = input.checked;
        }
      }
      sync();
    });
    for (const input of form.querySelectorAll("summary [data-tree-node]")) {
      input.addEventListener("click", (event) => event.stopPropagation());
    }
  }

  for (const select of document.querySelectorAll("[data-role-select]")) {
    const chips = select.querySelector("[data-role-select-chips]");
    const placeholder = select.querySelector("[data-role-select-placeholder]");
    const options = Array.from(select.querySelectorAll("[data-role-option]"));
    const removeLabel = select.dataset.removeLabel || "Remove";
    if (!chips) continue;

    const chipFor = (value) =>
      Array.from(chips.querySelectorAll("[data-role-chip]")).find(
        (chip) => chip.dataset.roleChip === value,
      );

    const addChip = (option) => {
      const name = option.dataset.roleName || option.value;
      const chip = document.createElement("span");
      chip.className = "role-select-chip";
      chip.dataset.roleChip = option.value;
      chip.append(name);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "role-select-remove";
      remove.dataset.roleSelectRemove = option.value;
      remove.setAttribute("aria-label", `${removeLabel} ${name}`);
      remove.textContent = "×";
      chip.append(remove);
      chips.append(chip);
    };

    const sync = () => {
      const selected = new Set(
        options.filter((option) => option.checked).map((option) => option.value),
      );
      for (const chip of chips.querySelectorAll("[data-role-chip]")) {
        if (!selected.has(chip.dataset.roleChip)) chip.remove();
      }
      for (const option of options) {
        if (option.checked && !chipFor(option.value)) addChip(option);
      }
      if (placeholder) placeholder.hidden = selected.size > 0;
    };

    const close = () => select.removeAttribute("open");

    select.setAttribute("data-enhanced", "");
    sync();

    select.addEventListener("change", (event) => {
      if (event.target instanceof HTMLInputElement && event.target.matches("[data-role-option]")) {
        sync();
      }
    });

    select.addEventListener("click", (event) => {
      const target =
        event.target instanceof Element
          ? event.target.closest("[data-role-select-remove]")
          : null;
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      const option = options.find((item) => item.value === target.dataset.roleSelectRemove);
      if (option) {
        option.checked = false;
        option.dispatchEvent(new Event("change", { bubbles: true }));
        sync();
      }
    });

    select.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && select.open) {
        close();
        select.querySelector("summary")?.focus();
      }
    });

    document.addEventListener("click", (event) => {
      if (event.target instanceof Node && !select.contains(event.target)) close();
    });
  }

  for (const preview of document.querySelectorAll("[data-permission-preview]")) {
    const form = preview.closest("form");
    if (!form) continue;

    const sync = () => {
      const selected = new Set(
        Array.from(form.querySelectorAll("[data-role-option]:checked")).map(
          (option) => option.value,
        ),
      );
      let granted = false;
      for (const permission of preview.querySelectorAll("[data-preview-permission]")) {
        const visible = (permission.dataset.previewRoles || "")
          .split(/\s+/)
          .some((role) => role && selected.has(role));
        permission.hidden = !visible;
        granted ||= visible;
      }
      for (const resource of preview.querySelectorAll("[data-preview-resource]")) {
        resource.hidden = !Array.from(
          resource.querySelectorAll("[data-preview-permission]"),
        ).some((permission) => !permission.hidden);
      }
      for (const service of preview.querySelectorAll("[data-preview-service]")) {
        service.hidden = !Array.from(
          service.querySelectorAll("[data-preview-resource]"),
        ).some((resource) => !resource.hidden);
      }
      const empty = preview.querySelector("[data-preview-empty]");
      if (empty) empty.hidden = granted;
    };

    sync();
    form.addEventListener("change", sync);
  }

  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.getAttribute("data-confirm") || "")) {
        event.preventDefault();
      }
    });
  }
})();
