(function () {
  "use strict";
  const gettext = window.oboxGettext;
  var pending = new Set();
  var initialized = new WeakSet();

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = gettext(text);
    return node;
  }

  function initPicker(root) {
    if (initialized.has(root)) return;
    initialized.add(root);
    root.querySelectorAll("[data-relation-ui]").forEach(function (node) { node.remove(); });
    var select = root.querySelector("select");
    if (!select || select.disabled) return;
    root.classList.add("position-relative");
    var input = element("input", "form-control");
    input.type = "search";
    input.id = select.id + "-search";
    input.autocomplete = "off";
    input.placeholder = gettext("Search records…");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", select.id + "-results");
    if (!select.dataset.relationRequired) select.dataset.relationRequired = String(select.required);
    input.setAttribute("aria-required", select.dataset.relationRequired);
    Array.from(select.labels).forEach(function (label) { label.htmlFor = input.id; });
    var selected = element("div", "d-flex flex-wrap gap-1 mb-1");
    var menu = element("div", "border rounded bg-body p-1 mt-1 position-absolute start-0 end-0 shadow");
    menu.style.top = "100%";
    menu.style.zIndex = "1000";
    menu.style.maxHeight = "260px";
    menu.style.overflowY = "auto";
    menu.addEventListener("mousedown", function (event) { event.preventDefault(); });
    menu.hidden = true;
    var list = element("div");
    list.id = select.id + "-results";
    list.setAttribute("role", "listbox");
    var status = element("div", "text-secondary small px-2");
    status.setAttribute("role", "status");
    var more = element("button", "btn btn-sm mt-1", "Load more");
    more.type = "button";
    more.hidden = true;
    menu.append(list, status, more);
    [selected, input, menu].forEach(function (node) { node.setAttribute("data-relation-ui", ""); });
    root.append(selected, input, menu);
    select.hidden = true;
    var required = select.dataset.relationRequired === "true";
    select.required = false;
    var page = 1, timer, controller, active = -1;

    function close() {
      menu.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }
    function renderSelected() {
      selected.replaceChildren();
      Array.from(select.selectedOptions).filter(function (option) { return option.value; }).forEach(function (option) {
        var chip = element("span", "badge bg-blue-lt d-inline-flex align-items-center gap-1");
        chip.append(document.createTextNode(option.textContent));
        if (select.multiple || !required) {
          var remove = element("button", "btn-close", "");
          remove.type = "button";
          remove.setAttribute("aria-label", gettext("Remove %(name)s", {name: option.textContent}));
          remove.addEventListener("click", function () {
            option.selected = false;
            option.removeAttribute("selected");
            if (!select.multiple) {
              var empty = Array.from(select.options).find(function (item) { return item.value === ""; });
              if (!empty) { empty = new Option("", ""); select.add(empty); }
              empty.selected = true;
              empty.setAttribute("selected", "");
            }
            select.dispatchEvent(new Event("change", {bubbles: true}));
            renderSelected();
            input.focus();
          });
          chip.append(remove);
        }
        selected.append(chip);
      });
    }
    function choose(result) {
      var option = Array.from(select.options).find(function (item) { return item.value === result.id; });
      if (!option) { option = new Option(result.text, result.id); select.add(option); }
      if (!select.multiple) Array.from(select.options).forEach(function (item) {
        item.selected = false;
        item.removeAttribute("selected");
      });
      option.selected = true;
      option.setAttribute("selected", "");
      select.dispatchEvent(new Event("change", {bubbles: true}));
      renderSelected();
      input.value = "";
      close();
      input.focus();
    }
    async function search(append) {
      if (!root.isConnected) return;
      if (controller) { controller.abort(); pending.delete(controller); }
      controller = new AbortController();
      var request = controller;
      pending.add(request);
      var term = input.value;
      if (!append) { page = 1; list.replaceChildren(); active = -1; }
      menu.hidden = false;
      input.setAttribute("aria-expanded", "true");
      status.textContent = gettext("Loading…");
      if (!append) more.hidden = true;
      else more.textContent = gettext("Loading…");
      var url = new URL(root.dataset.url, window.location.origin);
      url.searchParams.set("q", term);
      url.searchParams.set("page", page);
      try {
        var response = await fetch(url, {signal: request.signal, headers: {Accept: "application/json"}});
        if (!response.ok) throw new Error("Search unavailable");
        var data = await response.json();
        if (request.signal.aborted || term !== input.value || !root.isConnected) return;
        data.results.forEach(function (result) {
          var button = element("button", "btn btn-ghost-secondary w-100 text-start", result.text);
          button.type = "button";
          button.id = list.id + "-" + list.children.length;
          button.setAttribute("role", "option");
          button.setAttribute("aria-selected", "false");
          button.addEventListener("click", function () { choose(result); });
          list.append(button);
        });
        status.textContent = list.children.length ? "" : "No matching records.";
        if (!data.pagination.more && document.activeElement === more && list.lastElementChild) {
          list.lastElementChild.focus();
        }
        more.textContent = gettext("Load more");
        more.hidden = !data.pagination.more;
      } catch (error) {
        if (error.name !== "AbortError") status.textContent = gettext("Search failed. Try again.");
      } finally { pending.delete(request); }
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(function () { search(false); }, 200); });
    input.addEventListener("focus", function () { search(false); });
    input.addEventListener("keydown", function (event) {
      var buttons = Array.from(list.children);
      if (event.key === "Escape") close();
      if ((event.key === "ArrowDown" || event.key === "ArrowUp") && buttons.length) {
        event.preventDefault();
        active = (active + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        buttons.forEach(function (button, index) { button.setAttribute("aria-selected", String(index === active)); });
        input.setAttribute("aria-activedescendant", buttons[active].id);
        buttons[active].scrollIntoView({block: "nearest"});
      }
      if (event.key === "Enter" && !menu.hidden) {
        event.preventDefault();
        if (buttons.length) buttons[active < 0 ? 0 : active].click();
      }
      if (event.key === "Tab") close();
    });
    root.addEventListener("focusout", function (event) { if (!root.contains(event.relatedTarget)) close(); });
    more.addEventListener("click", function () {
      if (controller && pending.has(controller)) return;
      page += 1;
      search(true);
    });
    renderSelected();
  }

  function initFormset(root) {
    if (initialized.has(root)) return;
    initialized.add(root);
    var total = root.querySelector("[data-inline-total]");
    var rows = root.querySelector("[data-inline-rows]");
    var add = root.querySelector("[data-inline-add]");
    function update() {
      if (!add) return;
      var count = Array.from(rows.children).filter(function (row) {
        var deleted = row.querySelector('input[name$=":_delete"]');
        return !row.hidden && !(deleted && (deleted.checked || deleted.value === "1" && deleted.type === "hidden"));
      }).length;
      add.disabled = count >= Number(root.dataset.max) || Number(total.value) >= 1000;
    }
    if (add) add.addEventListener("click", function () {
      var html = root.querySelector("[data-inline-empty]").innerHTML.replaceAll("__index__", total.value);
      rows.insertAdjacentHTML("beforeend", html);
      total.value = Number(total.value) + 1;
      rows.lastElementChild.querySelectorAll("[data-relation-picker]").forEach(initPicker);
      update();
    });
    root.addEventListener("click", function (event) {
      if (!event.target.closest("[data-inline-remove]")) return;
      var row = event.target.closest("[data-inline-row]");
      row.querySelectorAll("input, select, textarea").forEach(function (input) { input.disabled = true; });
      var deleted = row.querySelector('input[name$=":_delete"]');
      deleted.disabled = false;
      deleted.value = "1";
      row.hidden = true;
      update();
    });
    root.addEventListener("change", update);
    update();
  }
  function init() {
    document.querySelectorAll("[data-relation-picker]").forEach(initPicker);
    document.querySelectorAll("[data-inline-formset]").forEach(initFormset);
  }
  document.addEventListener("admin:load", init);
  document.addEventListener("admin:leave", function () { pending.forEach(function (controller) { controller.abort(); }); pending.clear(); });
  init();
})();
