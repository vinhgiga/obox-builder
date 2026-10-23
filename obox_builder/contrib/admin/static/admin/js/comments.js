(function () {
  "use strict";

  var MENTION_QUERY = /(?:^|\s)@([^@\[\]\n]{2,40})$/;
  var menu = null;
  var menuItems = [];
  var activeIndex = -1;
  var activeTextarea = null;
  var activeStart = 0;
  var activeEnd = 0;
  var debounceTimer = null;
  var requestController = null;

  function closeMenu() {
    if (menu) {
      menu.remove();
    }
    menu = null;
    menuItems = [];
    activeIndex = -1;
  }

  function buildMenu() {
    var element = document.createElement("div");
    element.className = "dropdown-menu show comment-mention-menu";
    element.setAttribute("role", "listbox");
    document.body.appendChild(element);
    return element;
  }

  function positionMenu(textarea) {
    var rect = textarea.getBoundingClientRect();
    menu.style.left = rect.left + window.scrollX + "px";
    menu.style.top = rect.bottom + window.scrollY + 4 + "px";
    menu.style.minWidth = Math.min(rect.width, 320) + "px";
  }

  function renderMenu(textarea, results, query) {
    closeMenu();
    if (!results.length) {
      return;
    }
    activeTextarea = textarea;
    menu = buildMenu();
    menuItems = results;
    activeIndex = 0;
    results.forEach(function (result, index) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "dropdown-item" + (index === 0 ? " active" : "");
      item.setAttribute("role", "option");
      item.textContent = result.label;
      item.addEventListener("mousedown", function (event) {
        event.preventDefault();
        choose(index);
      });
      menu.appendChild(item);
    });
    positionMenu(textarea);
  }

  function highlight() {
    if (!menu) {
      return;
    }
    Array.prototype.forEach.call(menu.children, function (item, index) {
      item.classList.toggle("active", index === activeIndex);
    });
  }

  function choose(index) {
    var result = menuItems[index];
    if (!result || !activeTextarea) {
      closeMenu();
      return;
    }
    var textarea = activeTextarea;
    var value = textarea.value;
    var token = "@[u:" + result.id + "] ";
    textarea.value = value.slice(0, activeStart) + token + value.slice(activeEnd);
    var caret = activeStart + token.length;
    textarea.setSelectionRange(caret, caret);
    textarea.focus();
    closeMenu();
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function queryMentions(textarea) {
    var value = textarea.value;
    var caret = textarea.selectionStart || 0;
    var before = value.slice(0, caret);
    var match = before.match(MENTION_QUERY);
    if (!match) {
      closeMenu();
      return;
    }
    var query = match[1].trim();
    if (query.length < 2) {
      closeMenu();
      return;
    }
    var start = caret - match[1].length - 1;
    var end = caret;
    var url = textarea.dataset.mentionUrl;
    var model = textarea.dataset.mentionModel;
    if (!url || !model) {
      return;
    }
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(function () {
      if (requestController) {
        requestController.abort();
      }
      requestController = new AbortController();
      var target =
        url +
        "?model=" +
        encodeURIComponent(model) +
        "&q=" +
        encodeURIComponent(query);
      fetch(target, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: requestController.signal,
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("mention lookup failed");
          }
          return response.json();
        })
        .then(function (payload) {
          if (textarea !== document.activeElement) {
            return;
          }
          activeStart = start;
          activeEnd = end;
          renderMenu(textarea, payload.results || [], query);
        })
        .catch(function () {
          closeMenu();
        });
    }, 150);
  }

  function submitOnCtrlEnter(textarea) {
    textarea.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) {
        return;
      }
      event.preventDefault();
      var form = textarea.closest("form");
      if (form) {
        form.requestSubmit();
      }
    });
  }

  function initComposer(textarea) {
    submitOnCtrlEnter(textarea);
    textarea.addEventListener("input", function () {
      queryMentions(textarea);
    });
    textarea.addEventListener("click", closeMenu);
    textarea.addEventListener("blur", function () {
      window.setTimeout(closeMenu, 120);
    });
    textarea.addEventListener("keydown", function (event) {
      if (!menu) {
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        activeIndex = Math.min(activeIndex + 1, menuItems.length - 1);
        highlight();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        activeIndex = Math.max(activeIndex - 1, 0);
        highlight();
      } else if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        choose(activeIndex);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeMenu();
      }
    });
  }

  function initPage() {
    document.querySelectorAll("[data-comment-composer]").forEach(initComposer);
  }
  document.addEventListener("admin:load", initPage);
  document.addEventListener("admin:leave", function () {
    clearTimeout(debounceTimer);
    if (requestController) requestController.abort();
    closeMenu();
  });
  initPage();

  document.addEventListener("click", function (event) {
    if (menu && !menu.contains(event.target)) {
      closeMenu();
    }
    var editToggle = event.target.closest("[data-comment-edit-toggle]");
    if (editToggle) {
      var editForm = document.querySelector(
        '[data-comment-edit-form="' + editToggle.dataset.commentEditToggle + '"]'
      );
      if (editForm) {
        editForm.classList.remove("d-none");
        var editor = editForm.querySelector("textarea");
        if (editor) {
          editor.focus();
          editor.setSelectionRange(editor.value.length, editor.value.length);
        }
      }
      return;
    }
    var editCancel = event.target.closest("[data-comment-edit-cancel]");
    if (editCancel) {
      var form = editCancel.closest("form");
      if (form) {
        form.classList.add("d-none");
      }
      return;
    }
    var replyToggle = event.target.closest("[data-comment-reply-toggle]");
    if (replyToggle) {
      var replyForm = document.querySelector(
        '[data-comment-reply-form="' + replyToggle.dataset.commentReplyToggle + '"]'
      );
      if (replyForm) {
        replyForm.classList.remove("d-none");
        var textarea = replyForm.querySelector("textarea");
        if (textarea) {
          textarea.focus();
        }
      }
      return;
    }
    var replyCancel = event.target.closest("[data-comment-reply-cancel]");
    if (replyCancel) {
      var formToHide = replyCancel.closest("form");
      if (formToHide) {
        formToHide.classList.add("d-none");
      }
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeMenu();
    }
  });

  window.addEventListener("scroll", closeMenu, true);
  window.addEventListener("resize", closeMenu);
})();
