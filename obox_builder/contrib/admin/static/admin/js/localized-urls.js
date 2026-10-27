(function () {
  "use strict";

  function localizeAdminURL(value) {
    var language = document.documentElement.lang.toLowerCase();
    var prefix = language ? "/" + language : "";
    if (!prefix || !(location.pathname === prefix + "/admin" || location.pathname.startsWith(prefix + "/admin/"))) {
      return value;
    }
    var url = new URL(value, location.href);
    if (url.origin !== location.origin || !/^\/admin(?:\/|$)/.test(url.pathname)) return value;
    return prefix + url.pathname + url.search + url.hash;
  }

  function localizeAdminURLs() {
    document.querySelectorAll("a[href], form[action]").forEach(function (element) {
      var attribute = element.tagName === "FORM" ? "action" : "href";
      element.setAttribute(attribute, localizeAdminURL(element.getAttribute(attribute)));
    });
  }

  window.localizeAdminURL = localizeAdminURL;

  window.localizeAdminURLs = localizeAdminURLs;
  document.addEventListener("DOMContentLoaded", localizeAdminURLs);
  document.addEventListener("htmx:after:process", localizeAdminURLs);
  if (document.readyState !== "loading") localizeAdminURLs();
})();
