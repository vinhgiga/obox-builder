(function () {
  "use strict";
  let dispose = () => {};
  document.addEventListener("admin:leave", () => dispose());
  document.addEventListener("admin:load", init);

  function init() {
    dispose();
    const card = document.querySelector(".admin-list-card");
    const menu = card?.querySelector("[data-view-menu]");
    if (!menu) return;
    const toggle = card.querySelector("[data-views-toggle]");
    const sidebar = card.querySelector("[data-views-sidebar]");
    const table = card.querySelector("[data-list-table]");
    const query = card.querySelector("#list-query");
    const dialog = card.querySelector("[data-view-name-dialog]");
    const storageKey = `admin-views-open:${table.dataset.listKey}`;
    function show(open) {
      sidebar.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (!open && sidebar.contains(document.activeElement)) toggle.focus();
      try { localStorage.setItem(storageKey, String(open)); } catch {}
      table.dispatchEvent(new Event("admin:columns"));
    }
    try { show(localStorage.getItem(storageKey) === "true"); } catch { show(false); }
    toggle.addEventListener("click", () => show(sidebar.hidden));
    sidebar.querySelector("[data-view-search]").addEventListener("input", (event) => {
      const term = event.target.value.trim().toLocaleLowerCase();
      const links = [...sidebar.querySelectorAll("[data-saved-view]")];
      links.forEach((link) => { link.hidden = !link.textContent.toLocaleLowerCase().includes(term); });
      sidebar.querySelector("[data-views-empty]").hidden = links.some((link) => !link.hidden);
    });
    menu.querySelector("[data-view-rename]").addEventListener("click", () => {
      menu.open = false;
      dialog.showModal();
      dialog.querySelector('[name="name"]').select();
    });
    dialog.querySelector("[data-view-name-cancel]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => menu.querySelector("summary").focus());
    menu.querySelector("[data-view-csv]").addEventListener("click", (event) => {
      query.dispatchEvent(new Event("admin:serialize-query"));
      const headers = [...table.querySelectorAll("thead [data-list-column]")].filter((cell) => !cell.hidden);
      if (!headers.length) { event.preventDefault(); return; }
      const url = new URL(event.currentTarget.href);
      url.search = new URLSearchParams(new FormData(query)).toString();
      url.searchParams.delete("page");
      url.searchParams.set("columns", JSON.stringify(headers.map((cell) => cell.dataset.listColumn)));
      event.currentTarget.href = url.toString();
      menu.open = false;
    });
    function outside(event) {
      if (!menu.contains(event.target)) menu.open = false;
    }
    function keydown(event) {
      if (event.key === "Escape" && menu.open) {
        event.preventDefault();
        menu.open = false;
        menu.querySelector("summary").focus();
      } else if (event.key === "Escape" && sidebar.contains(event.target)) {
        event.preventDefault();
        show(false);
      } else if (menu.contains(event.target) && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        menu.open = true;
        const items = [...menu.querySelectorAll('[role="menuitem"]:not([disabled])')];
        const index = items.indexOf(document.activeElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (index + (event.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
        items[next].focus();
      }
    }
    document.addEventListener("click", outside);
    document.addEventListener("keydown", keydown);
    dispose = () => {
      dialog.close();
      document.removeEventListener("click", outside);
      document.removeEventListener("keydown", keydown);
      dispose = () => {};
    };
  }
  init();
})();
