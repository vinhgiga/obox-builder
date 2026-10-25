(function () {
  "use strict";

  function localizeAdminURLs() {
    var language = document.documentElement.lang.toLowerCase();
    var prefix = language ? "/" + language : "";
    if (!prefix || !(location.pathname === prefix + "/admin" || location.pathname.startsWith(prefix + "/admin/"))) {
      return;
    }
    document.querySelectorAll("a[href], form[action]").forEach(function (element) {
      var attribute = element.tagName === "FORM" ? "action" : "href";
      var url = new URL(element.getAttribute(attribute), location.href);
      if (url.origin !== location.origin || !/^\/admin(?:\/|$)/.test(url.pathname)) return;
      url.pathname = prefix + url.pathname;
      element.setAttribute(attribute, url.pathname + url.search + url.hash);
    });
  }

  window.localizeAdminURLs = localizeAdminURLs;
  document.addEventListener("DOMContentLoaded", localizeAdminURLs);
  document.addEventListener("htmx:after:process", localizeAdminURLs);
  if (document.readyState !== "loading") localizeAdminURLs();
})();
