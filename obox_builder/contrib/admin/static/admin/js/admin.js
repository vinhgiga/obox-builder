(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
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
})();
