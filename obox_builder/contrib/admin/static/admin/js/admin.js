(function () {
  "use strict";

  var confirmDialog = null;
  var pendingForm = null;

  function buildConfirmDialog() {
    var dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    dialog.setAttribute("aria-labelledby", "admin-confirm-title");
    dialog.innerHTML = [
      '<div class="confirm-dialog-status"></div>',
      '<div class="confirm-dialog-body">',
      '  <svg xmlns="http://www.w3.org/2000/svg" class="icon text-danger icon-lg mb-2" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M12 9v4"/><path d="M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0z"/><path d="M12 16h.01"/></svg>',
      '  <h3 class="mb-2" id="admin-confirm-title">Are you sure?</h3>',
      '  <div class="text-secondary" data-confirm-message></div>',
      '</div>',
      '<div class="confirm-dialog-footer">',
      '  <button type="button" class="btn" data-confirm-cancel autofocus>Cancel</button>',
      '  <button type="button" class="btn btn-danger" data-confirm-ok>Delete</button>',
      '</div>',
    ].join("\n");
    dialog.querySelector("[data-confirm-cancel]").addEventListener("click", function () {
      dialog.close();
    });
    dialog.querySelector("[data-confirm-ok]").addEventListener("click", function () {
      var form = pendingForm;
      dialog.close();
      if (form) {
        form.dataset.confirmed = "true";
        form.requestSubmit();
      }
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) {
        dialog.close();
      }
    });
    dialog.addEventListener("close", function () {
      pendingForm = null;
    });
    document.body.appendChild(dialog);
    return dialog;
  }

  function openConfirmDialog(form, message) {
    if (!confirmDialog) {
      confirmDialog = buildConfirmDialog();
    }
    pendingForm = form;
    confirmDialog.querySelector("[data-confirm-message]").textContent = message;
    confirmDialog.querySelector("[data-confirm-ok]").textContent =
      form.getAttribute("data-confirm-label") || "Delete";
    confirmDialog.showModal();
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    var message = form.getAttribute("data-confirm");
    if (!message) {
      return;
    }
    if (form.dataset.confirmed === "true") {
      delete form.dataset.confirmed;
      return;
    }
    event.preventDefault();
    openConfirmDialog(form, message);
  });

  var contextMenu = null;
  var contextCell = null;

  function copyText(text) {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(function () {});
      return;
    }
    var input = document.createElement("textarea");
    input.value = text;
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }

  function closeContextMenu() {
    if (contextMenu) {
      contextMenu.classList.remove("show");
    }
    contextCell = null;
  }

  function buildContextMenu() {
    var menu = document.createElement("div");
    menu.className = "dropdown-menu context-menu";
    menu.innerHTML = [
      '<button type="button" class="dropdown-item" data-context-copy>',
      '  <i class="ti ti-copy me-2"></i>Copy',
      '</button>',
      '<button type="button" class="dropdown-item text-danger" data-context-delete>',
      '  <i class="ti ti-trash me-2"></i>Delete row',
      '</button>',
    ].join("\n");
    menu.querySelector("[data-context-copy]").addEventListener("click", function () {
      var cell = contextCell;
      closeContextMenu();
      if (cell) {
        var editor = cell.querySelector("input, select, textarea");
        copyText(editor ? editor.value : cell.innerText.trim());
      }
    });
    menu.querySelector("[data-context-delete]").addEventListener("click", function () {
      var cell = contextCell;
      closeContextMenu();
      var form = cell && cell.closest("tr").querySelector("form[data-confirm]");
      if (form) {
        form.requestSubmit();
      }
    });
    document.body.appendChild(menu);
    return menu;
  }

  function openContextMenu(cell, x, y) {
    if (!contextMenu) {
      contextMenu = buildContextMenu();
    }
    contextCell = cell;
    var row = cell.closest("tr");
    var deletable = Boolean(row.querySelector("form[data-confirm]"));
    contextMenu
      .querySelector("[data-context-delete]")
      .classList.toggle("d-none", !deletable);
    contextMenu.classList.add("show");
    var left = Math.max(8, Math.min(x, window.innerWidth - contextMenu.offsetWidth - 8));
    var top = Math.max(8, Math.min(y, window.innerHeight - contextMenu.offsetHeight - 8));
    contextMenu.style.left = left + "px";
    contextMenu.style.top = top + "px";
  }

  function isDataCell(cell) {
    return cell.cellIndex !== 0 && !cell.querySelector("form, button, a.btn");
  }

  document.addEventListener("contextmenu", function (event) {
    var cell = event.target.closest("table tbody td");
    if (!cell || !isDataCell(cell)) {
      return;
    }
    event.preventDefault();
    openContextMenu(cell, event.clientX, event.clientY);
  });

  document.addEventListener("click", function (event) {
    if (contextMenu && !contextMenu.contains(event.target)) {
      closeContextMenu();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeContextMenu();
    }
  });

  window.addEventListener("scroll", closeContextMenu, true);
  window.addEventListener("resize", closeContextMenu);

  function setAllPermissions(checked) {
    document
      .querySelectorAll('input[name="permission_ids"]')
      .forEach(function (input) {
        input.checked = checked;
      });
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-perm-all], [data-perm-none]");
    if (!target) {
      return;
    }
    setAllPermissions(target.hasAttribute("data-perm-all"));
  });

  var SEARCH_DEBOUNCE_MS = 400;

  function initDebouncedSearch() {
    document
      .querySelectorAll('form input[name="q"]')
      .forEach(function (input) {
        var form = input.form;
        if (!form) {
          return;
        }
        var lastValue = input.value;
        var timer = null;
        input.addEventListener("input", function (event) {
          if (event.isComposing) {
            return;
          }
          window.clearTimeout(timer);
          timer = window.setTimeout(function () {
            if (input.value === lastValue) {
              return;
            }
            lastValue = input.value;
            form.submit();
          }, SEARCH_DEBOUNCE_MS);
        });
        form.addEventListener("submit", function () {
          window.clearTimeout(timer);
        });
      });
  }

  function restoreSearchFocus() {
    var query = new URLSearchParams(window.location.search).get("q");
    if (!query) {
      return;
    }
    document
      .querySelectorAll('form input[name="q"]')
      .forEach(function (input) {
        if (input.value !== query) {
          return;
        }
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
  }

  function wrapMatches(node, needle) {
    var text = node.nodeValue;
    var lower = text.toLowerCase();
    var index = lower.indexOf(needle);
    if (index === -1) {
      return;
    }
    var fragment = document.createDocumentFragment();
    var position = 0;
    while (index !== -1) {
      if (index > position) {
        fragment.appendChild(document.createTextNode(text.slice(position, index)));
      }
      var mark = document.createElement("mark");
      mark.textContent = text.slice(index, index + needle.length);
      fragment.appendChild(mark);
      position = index + needle.length;
      index = lower.indexOf(needle, position);
    }
    if (position < text.length) {
      fragment.appendChild(document.createTextNode(text.slice(position)));
    }
    node.parentNode.replaceChild(fragment, node);
  }

  function highlightCell(cell, needle) {
    var walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue.trim()) {
          return NodeFilter.FILTER_REJECT;
        }
        if (
          node.parentElement &&
          node.parentElement.closest("button, mark")
        ) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    var nodes = [];
    while (walker.nextNode()) {
      nodes.push(walker.currentNode);
    }
    nodes.forEach(function (node) {
      wrapMatches(node, needle);
    });
  }

  function highlightSearchMatches() {
    var query = new URLSearchParams(window.location.search).get("q");
    if (!query) {
      return;
    }
    var needle = query.toLowerCase();
    document.querySelectorAll("table tbody td").forEach(function (cell) {
      if (!isDataCell(cell) || cell.querySelector("input, select, textarea")) {
        return;
      }
      highlightCell(cell, needle);
    });
  }

  function initInlineEditors() {
    var form = document.getElementById("inline-edit");
    if (!form) {
      return;
    }
    var editors = Array.prototype.slice.call(
      document.querySelectorAll("[data-inline-editor]")
    );
    if (!editors.length) {
      return;
    }
    var saveButton = document.querySelector("[data-inline-save]");
    var countBadge = document.querySelector("[data-inline-count]");
    var revertButton = document.querySelector("[data-inline-revert]");

    function refresh() {
      var count = 0;
      editors.forEach(function (editor) {
        var modified = editor.value !== editor.dataset.original;
        if (modified) {
          count += 1;
        }
        var cell = editor.closest("td");
        if (cell) {
          cell.classList.toggle("cell-modified", modified);
        }
      });
      if (countBadge) {
        countBadge.textContent = String(count);
        countBadge.classList.toggle("d-none", count === 0);
      }
      if (saveButton) {
        saveButton.disabled = count === 0;
      }
      if (revertButton) {
        revertButton.classList.toggle("d-none", count === 0);
      }
    }

    function focusNextRow(editor) {
      var cell = editor.closest("td");
      var row = editor.closest("tr");
      if (!cell || !row) {
        return false;
      }
      var next = row.nextElementSibling;
      while (next) {
        if (next.cells && next.cells.length > cell.cellIndex) {
          var target = next.cells[cell.cellIndex].querySelector("[data-inline-editor]");
          if (target) {
            target.focus();
            if (typeof target.select === "function") {
              target.select();
            }
            return true;
          }
        }
        next = next.nextElementSibling;
      }
      return false;
    }

    editors.forEach(function (editor) {
      editor.dataset.committed = editor.value;
      editor.addEventListener("input", refresh);
      editor.addEventListener("change", function () {
        editor.dataset.committed = editor.value;
        refresh();
      });
      editor.addEventListener("blur", function () {
        editor.dataset.committed = editor.value;
      });
      editor.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          editor.dataset.committed = editor.value;
          refresh();
          if (!focusNextRow(editor)) {
            editor.blur();
          }
        } else if (event.key === "Escape") {
          event.preventDefault();
          editor.value = editor.dataset.committed;
          refresh();
        }
      });
      var cell = editor.closest("td");
      if (cell) {
        cell.addEventListener("click", function (event) {
          if (event.target !== editor) {
            editor.focus();
          }
        });
      }
    });
    if (revertButton) {
      revertButton.addEventListener("click", function () {
        editors.forEach(function (editor) {
          editor.value = editor.dataset.original;
          editor.dataset.committed = editor.dataset.original;
        });
        refresh();
      });
    }
    refresh();
  }

  initInlineEditors();
  initDebouncedSearch();
  highlightSearchMatches();
  restoreSearchFocus();
})();
