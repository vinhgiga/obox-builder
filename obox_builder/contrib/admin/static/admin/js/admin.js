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
      if (cell.cellIndex === 0) {
        return;
      }
      highlightCell(cell, needle);
    });
  }

  initDebouncedSearch();
  highlightSearchMatches();
  restoreSearchFocus();
})();
