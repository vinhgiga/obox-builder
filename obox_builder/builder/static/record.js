const drafts = new Map();
let pending = false;

function remember() {
  for (const form of document.querySelectorAll("[data-record-form]")) {
    const body = form.querySelector('textarea[name="body"]');
    if (body) drafts.set(form.action, body.value);
  }
}

async function replace(url, form = null, discard = null) {
  if (pending) return;
  pending = true;
  const buttons = form?.querySelectorAll('button[type="submit"]') || [];
  for (const button of buttons) button.disabled = true;
  remember();
  try {
    const response = await fetch(url, {
      method: form ? "POST" : "GET",
      headers: {
        Accept: "text/html",
        "X-Obox-Fragment": "true",
        ...(form ? { "Content-Type": "application/x-www-form-urlencoded" } : {}),
      },
      body: form ? new URLSearchParams(new FormData(form)) : undefined,
      credentials: "same-origin",
    });
    const doc = new DOMParser().parseFromString(await response.text(), "text/html");
    const root = doc.body.firstElementChild;
    const target = root?.id ? document.getElementById(root.id) : null;
    if (!target) {
      const error = document.createElement("p");
      error.role = "alert";
      error.className = "alert alert-danger";
      error.textContent = root?.textContent || response.statusText;
      (form || document.getElementById("record-content"))?.prepend(error);
      return;
    }
    if (response.ok && discard) drafts.delete(discard);
    if (response.ok && form?.querySelector('textarea[name="body"]')) {
      drafts.delete(form.action);
    }
    target.replaceWith(root);
    for (const next of root.querySelectorAll("[data-record-form]")) {
      const body = next.querySelector('textarea[name="body"]');
      if (body && drafts.has(next.action)) body.value = drafts.get(next.action);
    }
    root.querySelector("[role=alert], input:not([type=hidden]), textarea")?.focus();
  } catch (error) {
    const message = document.createElement("p");
    message.role = "alert";
    message.textContent = error.message;
    (form || document.getElementById("record-content"))?.prepend(message);
  } finally {
    for (const button of buttons) button.disabled = false;
    pending = false;
  }
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-record-link]");
  if (
    !link ||
    event.button !== 0 ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    event.altKey
  ) {
    return;
  }
  event.preventDefault();
  const editor = document.querySelector('[id^="record-field-"] form');
  if (link.hasAttribute("data-field-edit") && editor) {
    editor.querySelector("input:not([type=hidden]), textarea, select")?.focus();
    return;
  }
  replace(
    link.href,
    null,
    link.hasAttribute("data-discard-draft") ? link.closest("form")?.action : null,
  );
});

document.addEventListener("submit", (event) => {
  if (event.defaultPrevented) return;
  const form = event.target.closest("[data-record-form]");
  if (!form) return;
  event.preventDefault();
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
  replace(form.action, form);
});

const selectors = new WeakMap();
async function options(input, more = false) {
  const form = input.closest("form");
  selectors.get(input)?.abort();
  const controller = new AbortController();
  selectors.set(input, controller);
  const button = form.querySelector("[data-options-more]");
  const select = form.querySelector('select[name="value"]');
  const error = form.querySelector("[data-options-error]");
  const url = new URL(more ? button.dataset.nextUrl : input.dataset.optionsUrl, location.href);
  if (!more) {
    url.searchParams.set("limit", "20");
    if (input.value) url.searchParams.set("search", input.value);
  }
  try {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || response.statusText);
    error.textContent = "";
    if (!more) {
      for (const option of [...select.options]) {
        if (!option.selected && option.value) option.remove();
      }
    }
    for (const item of data.items) {
      if (![...select.options].some((option) => option.value === item.id)) {
        select.add(new Option(item.label, item.id));
      }
    }
    button.hidden = !data.page.hasMore;
    if (data.page.hasMore) {
      if (data.page.nextCursor) {
        url.searchParams.delete("offset");
        url.searchParams.set("cursor", data.page.nextCursor);
      } else {
        url.searchParams.delete("cursor");
        url.searchParams.set("offset", String(data.page.offset + data.page.limit));
      }
      button.dataset.nextUrl = url.href;
    }
  } catch (failure) {
    if (failure.name !== "AbortError") error.textContent = failure.message;
  }
}

document.addEventListener("input", (event) => {
  if (event.target.matches("[data-options-search]")) options(event.target);
});
document.addEventListener("focusin", (event) => {
  if (event.target.matches("[data-options-search]")) options(event.target);
});
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-options-more]");
  if (button) options(button.closest("form").querySelector("[data-options-search]"), true);
});
