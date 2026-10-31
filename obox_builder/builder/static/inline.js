(() => {
  for (const section of document.querySelectorAll("[data-inline]")) {
    const form = section.closest("form");
    const rows = section.querySelector("[data-inline-rows]");
    const group = section.dataset.inlineGroup;
    if (!form || !rows) continue;

    section.addEventListener("click", (event) => {
      if (event.target.closest("[data-inline-add]")) {
        const template = section.querySelector("template[data-inline-template]");
        if (!template) return;
        const row = template.content.querySelector("tr[data-inline-row]").cloneNode(true);
        rows.append(row);
        row.querySelector("input, select, textarea")?.focus();
      } else if (event.target.closest("[data-inline-remove]")) {
        event.target.closest("[data-inline-row]").remove();
      }
    });

    section.addEventListener("change", (event) => {
      const checkbox = event.target.closest("[data-inline-delete]");
      if (checkbox) {
        checkbox.closest("[data-inline-row]").style.opacity = checkbox.checked ? "0.5" : "";
      }
    });

    form.addEventListener("submit", () => {
      const pattern = new RegExp(`^inline:${group}:[^:]+:`);
      const prefix = `inline:${group}:`;
      [...rows.querySelectorAll("[data-inline-row]")].forEach((row, index) => {
        for (const element of row.querySelectorAll("[name], [id], [for]")) {
          for (const attribute of ["name", "id", "for"]) {
            const value = element.getAttribute(attribute);
            if (value) {
              element.setAttribute(
                attribute,
                value.replace(pattern, `${prefix}${index}:`),
              );
            }
          }
        }
      });
      const total = section.querySelector("[data-inline-total]");
      if (total) total.value = rows.querySelectorAll("[data-inline-row]").length;
    });
  }
})();
