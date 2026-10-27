(function () {
  "use strict";

  var motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var positions = new Map();
  var currentURL = location.href;
  var restoring = false;
  var searchFocus = null;
  var importTimer;

  function isPage(ctx) {
    return ctx && (ctx.target === document.body || ctx.target?.id === "admin-main");
  }

  function languagePrefix() {
    var language = document.documentElement.lang.toLowerCase();
    var prefix = language ? "/" + language : "";
    return prefix && (location.pathname === prefix + "/admin" || location.pathname.startsWith(prefix + "/admin/"))
      ? prefix
      : "";
  }

  function isAdminPath(pathname) {
    var prefix = languagePrefix();
    return pathname === "/admin" || pathname.startsWith("/admin/") ||
      (prefix && (pathname === prefix + "/admin" || pathname.startsWith(prefix + "/admin/")));
  }

  function syncNavigation() {
    document.querySelectorAll("#admin-sidebar-shell a.nav-link[href]").forEach(function (link) {
      var linkPath = link.pathname.replace(/\/$/, "");
      var adminHome = /\/admin$/.test(linkPath);
      var active = link.pathname === location.pathname ||
        (!adminHome && location.pathname.startsWith(linkPath + "/"));
      link.closest("li").classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  htmx.config.transitions = !motion.matches;
  motion.addEventListener("change", function () { htmx.config.transitions = !motion.matches; });

  // Keep normal navigation for same-origin URLs outside the admin UI.
  document.addEventListener("htmx:before:init", function (event) {
    var element = event.target;
    if (!element.matches("a, form")) return;
    var url = new URL(element.href || element.action, location.href);
    var languageChange = element.tagName === "FORM" && url.pathname === "/i18n/set-language";
    if (!languageChange && !isAdminPath(url.pathname)) {
      event.preventDefault();
    }
  });

  window.addEventListener("scroll", function () {
    positions.set(currentURL, [window.scrollX, window.scrollY]);
  }, {passive: true});

  document.addEventListener("htmx:before:history:restore", function () { restoring = true; });
  document.addEventListener("htmx:before:request", function (event) {
    var ctx = event.detail.ctx;
    if (!isPage(ctx)) return;
    positions.set(currentURL, [window.scrollX, window.scrollY]);
    var input = document.activeElement;
    searchFocus = input && input.matches('input[name="q"]') ? {
      action: input.form.action,
      start: input.selectionStart,
      end: input.selectionEnd
    } : null;
    document.getElementById("admin-network-error").classList.add("d-none");
  });

  document.addEventListener("htmx:after:request", function (event) {
    var ctx = event.detail.ctx;
    if (!isPage(ctx)) return;
    // History and custom views can return a complete document.
    if (/<!doctype|<html[\s>]/i.test(ctx.text)) {
      var page = new DOMParser().parseFromString(ctx.text, "text/html");
      if (page.querySelector("#admin-main")) ctx.select = "#admin-main";
      else {
        ctx.target = document.body;
        ctx.swap = "outerSync";
      }
    }
  });

  document.addEventListener("htmx:finally:request", function (event) {
    var ctx = event.detail.ctx;
    if (ctx.hx?.location) ctx.keepIndicators = false;
  });

  document.addEventListener("htmx:before:swap", function (event) {
    if (isPage(event.detail.ctx)) {
      clearTimeout(importTimer);
      document.dispatchEvent(new Event("admin:leave"));
      var input = document.activeElement;
      if (searchFocus && input?.matches('input[name="q"]')) {
        searchFocus.value = input.value;
        searchFocus.start = input.selectionStart;
        searchFocus.end = input.selectionEnd;
      }
    }
  });

  document.addEventListener("htmx:finally:swap", function (event) {
    if (!isPage(event.detail.ctx)) return;
    var response = event.detail.ctx.response?.raw;
    var language = response?.headers.get("content-language");
    if (language) document.documentElement.lang = language;
    window.localizeAdminURLs();
    currentURL = location.href;
    syncNavigation();
    document.dispatchEvent(new Event("admin:load"));
    scheduleImportRefresh();
    var main = document.getElementById("admin-main");
    if (!main) return;
    if (restoring) {
      window.scrollTo.apply(window, positions.get(currentURL) || [0, 0]);
    } else if (searchFocus) {
      var input = Array.from(document.querySelectorAll('input[name="q"]')).find(function (item) {
        return item.form.action === searchFocus.action;
      });
      if (input) {
        input.focus({preventScroll: true});
        input.setSelectionRange(searchFocus.start, searchFocus.end);
        if (searchFocus.value !== undefined && input.value !== searchFocus.value) {
          input.value = searchFocus.value;
          input.dispatchEvent(new Event("input"));
        }
      }
    } else {
      var heading = main.querySelector(".page-title, h1, h2") || main;
      heading.setAttribute("tabindex", "-1");
      heading.focus({preventScroll: true});
    }
    restoring = false;
    searchFocus = null;
  });

  document.addEventListener("htmx:after:history:update", window.localizeAdminURLs);

  function scheduleImportRefresh() {
    clearTimeout(importTimer);
    if (document.querySelector("[data-import-refresh]")) {
      importTimer = setTimeout(function () {
        htmx.ajax("GET", location.href, {target: "#admin-main", swap: "outerHTML show:none"});
      }, 3000);
    }
  }
  window.addEventListener("pagehide", function () { clearTimeout(importTimer); });
  document.addEventListener("DOMContentLoaded", scheduleImportRefresh);

  document.addEventListener("htmx:error", function (event) {
    var ctx = event.detail.ctx;
    if (isPage(ctx) && !ctx.request.signal.aborted) {
      document.getElementById("admin-network-error").classList.remove("d-none");
    }
    restoring = false;
  });
})();
