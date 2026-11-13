document.querySelectorAll("[data-tab-bar-color]").forEach((bar) => {
  const color = bar.getAttribute("data-tab-bar-color");
  if (color) bar.style.setProperty("--obox-app-tab-bar-color", color);
});

document.querySelectorAll("[data-app-tab-menu]").forEach((menu) => {
  const search = menu.querySelector("[data-app-tab-search]");
  const items = [...menu.querySelectorAll("[data-app-tab-item]")];
  const empty = menu.querySelector("[data-app-tab-empty]");
  const toggle = menu.querySelector("summary");

  if (!search || !empty || !toggle) return;

  const normalize = (value) =>
    value
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLocaleLowerCase();
  const filter = () => {
    const query = normalize(search.value.trim());
    let matches = 0;
    items.forEach((item) => {
      const visible = normalize(item.textContent ?? "").includes(query);
      item.hidden = !visible;
      if (visible) matches += 1;
    });
    empty.hidden = matches > 0;
  };

  search.addEventListener("input", filter);
  menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    search.value = "";
    filter();
    search.focus();
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    toggle.focus();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
});
