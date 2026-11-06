const token = (box) => box.closest("form")?.querySelector('[name="csrf_token"]')?.value || "";
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const pending = new WeakMap();
const initialized = new WeakSet();
const metadata = new WeakMap();
const previewTypes = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
  "image/avif",
  "video/mp4",
  "video/webm",
]);

async function json(url, options) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const result = await response.json();
  if (!response.ok) {
    throw Object.assign(new Error(result.detail || `HTTP ${response.status}`), {
      status: response.status,
    });
  }
  return result;
}

function patch(url, blob, offset, csrf, progress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PATCH", url);
    xhr.timeout = 120000;
    xhr.setRequestHeader("Upload-Offset", String(offset));
    xhr.setRequestHeader("Content-Type", "application/offset+octet-stream");
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (event) => progress(offset + event.loaded);
    xhr.onload = () => {
      try {
        if (xhr.status < 200 || xhr.status >= 300) {
          throw Object.assign(new Error(`HTTP ${xhr.status}`), { status: xhr.status });
        }
        resolve(JSON.parse(xhr.responseText));
      } catch (error) {
        reject(error);
      }
    };
    xhr.onerror = xhr.ontimeout = () => reject(new Error("Upload interrupted."));
    xhr.send(blob);
  });
}

async function upload(file, box, progress) {
  const csrf = token(box);
  const meta = await json(box.dataset.url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify({
      filename: file.name,
      size: file.size,
      field: box.dataset.field,
      owner_id: box.dataset.owner || null,
    }),
  });
  const url = `${box.dataset.url}/${meta.id}`;
  let offset = 0;
  let failures = 0;
  let resync = false;
  for (;;) {
    try {
      if (resync) {
        const state = await json(`${url}/state`);
        if (state.status === "ready") return state;
        if (state.status === "failed")
          throw Object.assign(new Error("Invalid file."), { status: 422 });
        offset = state.offset;
        resync = false;
      }
      const result = await patch(
        url,
        file.slice(offset, Math.min(offset + meta.chunk_size, file.size)),
        offset,
        csrf,
        progress,
      );
      if (result.status === "ready") return result;
      offset = result.offset;
      failures = 0;
    } catch (error) {
      if ((error.status >= 400 && error.status < 500 && error.status !== 409) || ++failures > 5)
        throw error;
      resync = true;
      await wait(500 * 2 ** failures);
    }
  }
}

function ids(box) {
  const value = JSON.parse(box.querySelector("[data-files-ids]").value || "[]");
  return Array.isArray(value) ? value : [];
}

function save(box) {
  const input = box.querySelector("[data-files-ids]");
  input.value = JSON.stringify(
    [...box.querySelectorAll("[data-file-id]")].map((card) => card.dataset.fileId),
  );
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

const reorders = new WeakMap();
const removed = new WeakSet();
const FLIP_DURATION = 180;
const FLIP_EASING = "cubic-bezier(0.2, 0, 0, 1)";
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function persistOrder(box) {
  if (!box.dataset.owner || removed.has(box)) return;
  const attached = [...box.querySelectorAll('[data-file-id][data-attached="true"]')].map(
    (card) => card.dataset.fileId,
  );
  if (!attached.length) return;
  const body = JSON.stringify({
    field: box.dataset.field,
    ids: attached,
  });
  const errorBox = box.querySelector("[data-files-error]");
  const request = async () => {
    try {
      await json(`${box.dataset.url}/${encodeURIComponent(box.dataset.owner)}/order`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token(box) },
        body,
      });
      if (errorBox.dataset.orderError) {
        errorBox.textContent = "";
        delete errorBox.dataset.orderError;
      }
    } catch (error) {
      errorBox.dataset.orderError = "";
      errorBox.textContent = `Could not save order: ${error.message}`;
    }
  };
  reorders.set(box, (reorders.get(box) || Promise.resolve()).then(request, request));
}

function stopFlips(cards) {
  for (const card of cards) {
    for (const animation of card.getAnimations()) {
      if (animation.id === "obox-file-flip") animation.cancel();
    }
  }
}

function flip(cards, update, skip) {
  if (reducedMotion()) {
    update();
    return;
  }
  const before = new Map(cards.map((card) => [card, card.getBoundingClientRect()]));
  update();
  stopFlips(cards);
  for (const card of cards) {
    if (card === skip || !card.isConnected) continue;
    const start = before.get(card);
    const end = card.getBoundingClientRect();
    const dx = start.left - end.left;
    const dy = start.top - end.top;
    if (!dx && !dy) continue;
    const animation = card.animate(
      [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "translate(0, 0)" }],
      { duration: FLIP_DURATION, easing: FLIP_EASING },
    );
    animation.id = "obox-file-flip";
  }
}

function reorderCard(box, card, target, after) {
  const cards = [...box.querySelectorAll("[data-file-id]")];
  const others = cards.filter((item) => item !== card);
  const index = others.indexOf(target) + (after ? 1 : 0);
  if (index === cards.indexOf(card)) return false;
  flip(
    cards,
    () => {
      if (index < others.length) others[index].before(card);
      else if (others.length) others[others.length - 1].after(card);
      else card.parentElement.append(card);
    },
    card,
  );
  return true;
}

function targetAt(box, card, x, y) {
  let nearest = null;
  let distance = Infinity;
  for (const item of box.querySelectorAll("[data-file-id]")) {
    if (item === card) continue;
    const rect = item.getBoundingClientRect();
    const dx = Math.max(rect.left - x, 0, x - rect.right);
    const dy = Math.max(rect.top - y, 0, y - rect.bottom);
    const square = dx * dx + dy * dy;
    if (square < distance) {
      nearest = { item, rect };
      distance = square;
    }
  }
  if (!nearest) return null;
  const { item, rect } = nearest;
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const across = Math.abs(x - centerX) / Math.max(rect.width / 2, 1);
  const down = Math.abs(y - centerY) / Math.max(rect.height / 2, 1);
  return { target: item, after: across >= down ? x >= centerX : y >= centerY };
}

function sortable(box, card) {
  let suppress = false;
  card.addEventListener(
    "click",
    (event) => {
      if (!suppress) return;
      suppress = false;
      event.preventDefault();
      event.stopImmediatePropagation();
    },
    true,
  );
  card.addEventListener("dragstart", (event) => event.preventDefault());

  card.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.isPrimary === false) return;
    const control = event.target.closest("button, a, input, select, textarea");
    const handle = control?.classList.contains("obox-file-grip");
    const preview = control?.classList.contains("obox-file-preview");
    if (control && !handle && !preview) return;
    if (event.pointerType !== "mouse" && !handle) return;
    const origin = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    let drag = null;

    const move = (current) => {
      if (current.pointerId !== origin.pointerId) return;
      if (!drag) {
        if (Math.hypot(current.clientX - origin.x, current.clientY - origin.y) < 5) return;
        drag = beginDrag(box, card, origin);
        if (!drag) return;
        try {
          card.setPointerCapture(current.pointerId);
        } catch {
          /* Synthetic pointers cannot always be captured. */
        }
      }
      current.preventDefault();
      drag.move(current);
    };

    const end = (current) => {
      if (current.pointerId !== origin.pointerId) return;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      if (!drag) return;
      drag.end(current);
      suppress = true;
      setTimeout(() => {
        suppress = false;
      }, 0);
    };

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  });
}

function beginDrag(box, card, origin) {
  const rect = card.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const overlay = card.cloneNode(true);
  overlay.removeAttribute("data-file-id");
  overlay.removeAttribute("role");
  overlay.classList.add("obox-file-drag-overlay");
  overlay.classList.remove("is-dragging");
  overlay.setAttribute("aria-hidden", "true");
  overlay.inert = true;
  Object.assign(overlay.style, {
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
  });
  document.body.append(overlay);
  card.classList.add("is-dragging");
  box.classList.add("is-sorting");
  document.body.classList.add("obox-file-sorting");
  let dx = 0;
  let dy = 0;

  function move(event) {
    dx = event.clientX - origin.x;
    dy = event.clientY - origin.y;
    overlay.style.transform = `translate3d(${dx}px, ${dy}px, 0) rotate(1.5deg) scale(1.03)`;
    const target = targetAt(
      box,
      card,
      rect.left + rect.width / 2 + dx,
      rect.top + rect.height / 2 + dy,
    );
    if (target) reorderCard(box, card, target.target, target.after);
  }

  function end(event) {
    if (card.hasPointerCapture?.(event.pointerId)) card.releasePointerCapture(event.pointerId);
    save(box);
    persistOrder(box);
    const finish = () => {
      overlay.remove();
      card.classList.remove("is-dragging");
      box.classList.remove("is-sorting");
      document.body.classList.remove("obox-file-sorting");
    };
    if (reducedMotion()) {
      finish();
      return;
    }
    const target = card.getBoundingClientRect();
    const animation = overlay.animate(
      [
        {
          transform: `translate3d(${dx}px, ${dy}px, 0) rotate(1.5deg) scale(1.03)`,
        },
        {
          transform: `translate3d(${target.left - rect.left}px, ${target.top - rect.top}px, 0) rotate(0deg) scale(1)`,
        },
      ],
      { duration: FLIP_DURATION, easing: FLIP_EASING, fill: "forwards" },
    );
    animation.finished.catch(() => {}).then(finish);
  }

  return { move, end };
}

function button(label, text, action) {
  const item = document.createElement("button");
  item.type = "button";
  item.textContent = text;
  item.title = label;
  item.setAttribute("aria-label", label);
  item.onclick = action;
  return item;
}

function actionButton(label, name, action) {
  const item = button(label, "", action);
  item.className = "obox-file-action";
  item.append(icon(name));
  return item;
}

const iconShapes = {
  zoomIn: [
    ["circle", { cx: "11", cy: "11", r: "7" }],
    ["path", { d: "m20 20-4.5-4.5M11 8v6M8 11h6" }],
  ],
  zoomOut: [
    ["circle", { cx: "11", cy: "11", r: "7" }],
    ["path", { d: "m20 20-4.5-4.5M8 11h6" }],
  ],
  external: [
    ["path", { d: "M15 4h5v5" }],
    ["path", { d: "M10 14 20 4" }],
    ["path", { d: "M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5" }],
  ],
  expand: [
    ["path", { d: "M9 4H6a2 2 0 0 0-2 2v3" }],
    ["path", { d: "M20 9V6a2 2 0 0 0-2-2h-3" }],
    ["path", { d: "M4 15v3a2 2 0 0 0 2 2h3" }],
    ["path", { d: "M15 20h3a2 2 0 0 0 2-2v-3" }],
  ],
  compress: [
    ["path", { d: "M9 4v3a2 2 0 0 1-2 2H4" }],
    ["path", { d: "M20 9h-3a2 2 0 0 1-2-2V4" }],
    ["path", { d: "M4 15h3a2 2 0 0 1 2 2v3" }],
    ["path", { d: "M15 20v-3a2 2 0 0 1 2-2h3" }],
  ],
  play: [["path", { d: "m7 4 12 8-12 8Z" }]],
  pause: [["path", { d: "M8 4v16M16 4v16" }]],
  download: [
    ["path", { d: "M20 14v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-5" }],
    ["path", { d: "m7 9 5 5 5-5M12 14V4" }],
  ],
  file: [
    ["path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" }],
    ["path", { d: "M14 3v5h5" }],
    ["path", { d: "M9 13h6M9 17h4" }],
  ],
  edit: [["path", { d: "M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" }]],
  trash: [
    ["path", { d: "M3 6h18" }],
    ["path", { d: "M8 6V4h8v2" }],
    ["path", { d: "M19 6v13a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" }],
    ["path", { d: "M10 11v6M14 11v6" }],
  ],
  grip: [5, 12, 19].flatMap((cy) =>
    [9, 15].map((cx) => [
      "circle",
      { cx: String(cx), cy: String(cy), r: "1.15", fill: "currentColor", stroke: "none" },
    ]),
  ),
  grid: [
    ["rect", { x: "4", y: "4", width: "6", height: "6", rx: "1" }],
    ["rect", { x: "14", y: "4", width: "6", height: "6", rx: "1" }],
    ["rect", { x: "14", y: "14", width: "6", height: "6", rx: "1" }],
    ["rect", { x: "4", y: "14", width: "6", height: "6", rx: "1" }],
  ],
  chevronLeft: [["path", { d: "m15 19-7-7 7-7" }]],
  chevronRight: [["path", { d: "m9 19 7-7-7-7" }]],
  close: [["path", { d: "m5 5 14 14M19 5 5 19" }]],
};

function svgIcon(shapes) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.75");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attributes] of shapes) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, value);
    svg.append(shape);
  }
  return svg;
}

function icon(name, className) {
  const svg = svgIcon(iconShapes[name]);
  if (className) svg.setAttribute("class", className);
  return svg;
}

function setIcon(item, name) {
  item.querySelector(".obox-file-lightbox-tool-icon")?.replaceChildren(svgIcon(iconShapes[name]));
}

function setLabel(item, text) {
  item.title = text;
  item.setAttribute("aria-label", text);
}

function tool(name, label, action) {
  const item = button(label, "", action);
  item.className = "obox-file-lightbox-tool";
  const icon = document.createElement("span");
  icon.className = "obox-file-lightbox-tool-icon";
  icon.append(svgIcon(iconShapes[name]));
  item.append(icon);
  return item;
}

const fullscreenEvents = ["fullscreenchange", "webkitfullscreenchange", "MSFullscreenChange"];

function fullscreenElement() {
  return (
    document.fullscreenElement || document.webkitFullscreenElement || document.msFullscreenElement
  );
}

function requestFullscreen(element) {
  const request =
    element.requestFullscreen || element.webkitRequestFullscreen || element.msRequestFullscreen;
  if (!request) return Promise.reject(new Error("Fullscreen is unavailable."));
  try {
    return Promise.resolve(request.call(element));
  } catch (error) {
    return Promise.reject(error);
  }
}

function exitFullscreen() {
  const exit =
    document.exitFullscreen || document.webkitExitFullscreen || document.msExitFullscreen;
  if (!exit) return Promise.reject(new Error("Fullscreen is unavailable."));
  try {
    return Promise.resolve(exit.call(document));
  } catch (error) {
    return Promise.reject(error);
  }
}

function media(meta, url) {
  if (!previewTypes.has(meta.content_type)) return null;
  const item = document.createElement(meta.content_type.startsWith("image/") ? "img" : "video");
  item.src = url;
  if (item.tagName === "IMG") {
    item.alt = meta.filename;
    item.loading = "lazy";
  } else {
    item.preload = "metadata";
    item.muted = true;
  }
  return item;
}

function fileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function fileDetails(meta) {
  const type = (meta.content_type.split("/").pop() || "").toUpperCase();
  return [type, fileSize(meta.size)].filter(Boolean).join(" · ");
}

function preview(box, card) {
  const cards = [...box.querySelectorAll("[data-file-id]")];
  const start = cards.indexOf(card);
  if (start < 0) return;
  const many = cards.length > 1;
  const dialog = document.createElement("dialog");
  dialog.className = "obox-file-lightbox";
  dialog.tabIndex = -1;
  dialog.setAttribute("aria-label", "File preview");

  const counter = document.createElement("span");
  counter.className = "obox-file-lightbox-counter";

  const slideshowDelay = 5000;
  let playing = false;
  let slideshowStart = 0;
  let slideshowFrame = null;
  let thumbsOpen = true;
  let expanded = false;
  let index = start;
  let current = null;
  let scale = 1;
  let offsetX = 0;
  let offsetY = 0;
  let drag = null;
  let moved = false;

  const zoom = tool("zoomIn", "Zoom", () => toggleZoom());
  const openTab = document.createElement("a");
  openTab.className = "obox-file-lightbox-tool";
  openTab.target = "_blank";
  openTab.rel = "noopener";
  setLabel(openTab, "Open in new tab");
  const openTabIcon = document.createElement("span");
  openTabIcon.className = "obox-file-lightbox-tool-icon";
  openTabIcon.append(svgIcon(iconShapes.external));
  openTab.append(openTabIcon);
  const fullscreen = tool("expand", "Fullscreen", () => toggleFullscreen());
  const slideshow = tool("play", "Start slideshow", () => toggleSlideshow());
  const slideshowProgress = document.createElement("span");
  slideshowProgress.className = "obox-file-lightbox-tool-progress";
  const slideshowFill = document.createElement("span");
  slideshowFill.className = "obox-file-lightbox-tool-progress-fill";
  slideshowProgress.append(slideshowFill);
  slideshow.append(slideshowProgress);
  const download = document.createElement("a");
  download.className = "obox-file-lightbox-tool";
  setLabel(download, "Download");
  const downloadIcon = document.createElement("span");
  downloadIcon.className = "obox-file-lightbox-tool-icon";
  downloadIcon.append(svgIcon(iconShapes.download));
  download.append(downloadIcon);
  const thumbToggle = tool("grid", "Hide thumbnails", () => toggleThumbs());
  const close = tool("close", "Close preview", () => dialog.close());
  zoom.setAttribute("aria-pressed", "false");
  fullscreen.setAttribute("aria-pressed", "false");
  slideshow.setAttribute("aria-pressed", "false");
  thumbToggle.setAttribute("aria-pressed", "true");
  slideshow.hidden = thumbToggle.hidden = !many;
  if (
    !document.documentElement.requestFullscreen &&
    !document.documentElement.webkitRequestFullscreen
  ) {
    fullscreen.hidden = true;
  }

  const actions = document.createElement("div");
  actions.className = "obox-file-lightbox-actions";
  actions.append(zoom, openTab, fullscreen, slideshow, download, thumbToggle, close);
  const toolbar = document.createElement("div");
  toolbar.className = "obox-file-lightbox-toolbar";
  toolbar.append(counter, actions);

  const newMedia = document.createElement("div");
  newMedia.className = "obox-file-lightbox-media";
  const title = document.createElement("strong");
  title.className = "obox-file-lightbox-title";
  const details = document.createElement("span");
  details.className = "obox-file-lightbox-details";
  const caption = document.createElement("figcaption");
  caption.className = "obox-file-lightbox-caption";
  caption.append(title, details);
  const figure = document.createElement("figure");
  figure.className = "obox-file-lightbox-figure";
  figure.append(newMedia, caption);

  const previous = tool("chevronLeft", "Previous file", () => move(-1));
  const next = tool("chevronRight", "Next file", () => move(1));
  previous.className = "obox-file-lightbox-nav is-previous";
  next.className = "obox-file-lightbox-nav is-next";
  previous.hidden = next.hidden = !many;

  const thumbs = document.createElement("div");
  thumbs.className = "obox-file-lightbox-thumbs";
  thumbs.setAttribute("role", "group");
  thumbs.setAttribute("aria-label", "Attachments");
  const thumbButtons = cards.map((item, index) => {
    const value = metadata.get(item);
    const thumb = document.createElement("button");
    thumb.type = "button";
    thumb.className = "obox-file-lightbox-thumb";
    thumb.setAttribute("aria-label", `Preview ${value?.filename || "file"}`);
    thumb.onclick = () => render(index);
    const image = value ? media(value, `${box.dataset.url}/${encodeURIComponent(value.id)}`) : null;
    if (image) {
      if (image.tagName === "VIDEO") image.muted = true;
      thumb.append(image);
    } else {
      thumb.textContent = "File";
    }
    thumbs.append(thumb);
    return thumb;
  });

  function applyZoom() {
    dialog.classList.toggle("is-zoomed", scale > 1);
    if (!current) return;
    if (scale > 1) {
      const maxX = Math.max(0, (current.offsetWidth * scale - newMedia.clientWidth) / 2);
      const maxY = Math.max(0, (current.offsetHeight * scale - newMedia.clientHeight) / 2);
      offsetX = Math.min(maxX, Math.max(-maxX, offsetX));
      offsetY = Math.min(maxY, Math.max(-maxY, offsetY));
    }
    current.style.transform =
      scale > 1 ? `translate(${offsetX}px, ${offsetY}px) scale(${scale})` : "";
  }

  function resetZoom() {
    scale = 1;
    offsetX = 0;
    offsetY = 0;
    drag = null;
    setIcon(zoom, "zoomIn");
    setLabel(zoom, "Zoom");
    zoom.setAttribute("aria-pressed", "false");
    applyZoom();
  }

  function zoomScale() {
    const natural = current?.naturalWidth || 0;
    const shown = current?.clientWidth || 0;
    if (!natural || !shown) return 2;
    return Math.max(natural / shown, 1.1);
  }

  function toggleZoom() {
    if (current?.tagName !== "IMG") return;
    if (scale > 1) {
      resetZoom();
      return;
    }
    scale = zoomScale();
    setIcon(zoom, "zoomOut");
    setLabel(zoom, "Zoom out");
    zoom.setAttribute("aria-pressed", "true");
    applyZoom();
  }

  function updateFullscreen() {
    dialog.classList.toggle("is-expanded", expanded);
    setIcon(fullscreen, expanded ? "compress" : "expand");
    setLabel(fullscreen, expanded ? "Exit fullscreen" : "Fullscreen");
    fullscreen.setAttribute("aria-pressed", String(expanded));
  }

  function toggleFullscreen() {
    expanded = !expanded;
    updateFullscreen();
    if (expanded) {
      requestFullscreen(document.documentElement).catch(() => {});
    } else if (fullscreenElement()) {
      exitFullscreen().catch(() => {});
    }
  }

  function syncFullscreen() {
    if (!fullscreenElement() && expanded) {
      expanded = false;
      updateFullscreen();
    }
  }

  function tickSlideshow(now) {
    if (!playing) return;
    const elapsed = Math.min((now - slideshowStart) / slideshowDelay, 1);
    slideshowFill.style.transform = `scaleX(${elapsed})`;
    if (elapsed >= 1) move(1);
    slideshowFrame = requestAnimationFrame(tickSlideshow);
  }

  function stopSlideshow() {
    if (!playing) return;
    playing = false;
    cancelAnimationFrame(slideshowFrame);
    slideshowFrame = null;
    slideshow.classList.remove("is-progress");
    slideshowFill.style.transform = "scaleX(0)";
    setIcon(slideshow, "play");
    setLabel(slideshow, "Start slideshow");
    slideshow.setAttribute("aria-pressed", "false");
  }

  function toggleSlideshow() {
    if (playing) {
      stopSlideshow();
      return;
    }
    if (!many) return;
    playing = true;
    slideshowStart = performance.now();
    slideshow.classList.add("is-progress");
    slideshowFill.style.transform = "scaleX(0)";
    setIcon(slideshow, "pause");
    setLabel(slideshow, "Pause slideshow");
    slideshow.setAttribute("aria-pressed", "true");
    slideshowFrame = requestAnimationFrame(tickSlideshow);
  }

  function toggleThumbs() {
    thumbsOpen = !thumbsOpen;
    thumbs.hidden = !many || !thumbsOpen;
    setLabel(thumbToggle, thumbsOpen ? "Hide thumbnails" : "Show thumbnails");
    thumbToggle.setAttribute("aria-pressed", String(thumbsOpen));
  }

  function render(selected) {
    const value = metadata.get(cards[selected]);
    if (!value) return;
    index = selected;
    if (playing) slideshowStart = performance.now();
    const url = `${box.dataset.url}/${encodeURIComponent(value.id)}`;
    const content = media(value, url);
    if (content?.tagName === "VIDEO") {
      content.controls = true;
      content.muted = false;
    }
    newMedia.replaceChildren(
      content || document.createTextNode("Preview is unavailable for this file type."),
    );
    current = content;
    resetZoom();
    zoom.hidden = content?.tagName !== "IMG";
    title.textContent = value.filename;
    details.textContent = fileDetails(value);
    details.hidden = !details.textContent;
    counter.textContent = many ? `${selected + 1} / ${cards.length}` : "";
    download.href = `${url}?download=true`;
    openTab.href = url;
    dialog.setAttribute("aria-label", value.filename);
    thumbs.hidden = !many || !thumbsOpen;
    thumbButtons.forEach((thumb, position) => {
      thumb.classList.toggle("is-active", position === selected);
      if (position === selected) thumb.setAttribute("aria-current", "true");
      else thumb.removeAttribute("aria-current");
    });
  }

  function move(step) {
    if (!many) return;
    render((index + step + cards.length) % cards.length);
  }

  newMedia.addEventListener("pointerdown", (event) => {
    moved = false;
    if (event.button !== 0 || scale === 1) return;
    drag = { x: event.clientX - offsetX, y: event.clientY - offsetY };
    try {
      newMedia.setPointerCapture(event.pointerId);
    } catch {
      /* Synthetic pointers cannot always be captured. */
    }
  });
  newMedia.addEventListener("pointermove", (event) => {
    if (!drag) return;
    moved = true;
    offsetX = event.clientX - drag.x;
    offsetY = event.clientY - drag.y;
    applyZoom();
  });
  const endDrag = (event) => {
    if (!drag) return;
    drag = null;
    if (newMedia.hasPointerCapture(event.pointerId)) {
      newMedia.releasePointerCapture(event.pointerId);
    }
  };
  newMedia.addEventListener("pointerup", endDrag);
  newMedia.addEventListener("pointercancel", endDrag);
  newMedia.addEventListener("click", (event) => {
    if (moved) {
      moved = false;
      return;
    }
    if (current?.tagName !== "IMG") return;
    const rect = current.getBoundingClientRect();
    const inside =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    if (event.target === current || inside) toggleZoom();
  });
  newMedia.addEventListener("contextmenu", (event) => {
    if (current?.tagName !== "IMG" || scale === 1) return;
    event.preventDefault();
    resetZoom();
  });

  dialog.append(toolbar, previous, figure, next, thumbs);
  for (const name of fullscreenEvents) document.addEventListener(name, syncFullscreen);
  dialog.addEventListener(
    "close",
    () => {
      stopSlideshow();
      for (const name of fullscreenEvents) document.removeEventListener(name, syncFullscreen);
      if (fullscreenElement() === document.documentElement) {
        exitFullscreen().catch(() => {});
      }
      dialog.remove();
    },
    { once: true },
  );
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      move(1);
    }
  });
  document.body.append(dialog);
  render(start);
  dialog.showModal();
  dialog.focus();
}

function entry(box, id, meta, attached = false) {
  const card = document.createElement("div");
  card.className = "obox-file";
  card.dataset.fileId = id;
  card.dataset.attached = String(attached);
  card.setAttribute("role", "listitem");
  const url = `${box.dataset.url}/${encodeURIComponent(id)}`;
  const thumb = button("Preview file", "", () => preview(box, card));
  thumb.className = "obox-file-preview";
  thumb.append(icon("file", "obox-file-icon"));
  const name = document.createElement("span");
  name.className = "obox-file-name";
  const actions = document.createElement("div");
  actions.className = "obox-file-actions";
  const download = document.createElement("a");
  download.className = "obox-file-action";
  download.href = `${url}?download=true`;
  download.title = "Download";
  download.setAttribute("aria-label", "Download");
  download.append(icon("download"));
  function update(value) {
    metadata.set(card, value);
    name.textContent = name.title = value.filename;
    thumb.setAttribute("aria-label", `Preview ${value.filename}`);
    const image = media(value, url);
    image?.addEventListener("error", () => thumb.replaceChildren(icon("file", "obox-file-icon")), {
      once: true,
    });
    thumb.replaceChildren(image || icon("file", "obox-file-icon"));
  }
  if (box.dataset.readonly === "false") {
    const move = (step) => {
      const cards = [...box.querySelectorAll("[data-file-id]")];
      const target = cards[cards.indexOf(card) + step];
      if (!target) return;
      if (reorderCard(box, card, target, step > 0)) {
        save(box);
        persistOrder(box);
      }
    };
    const grip = actionButton("Drag or use arrow keys to reorder", "grip", () => {});
    grip.classList.add("obox-file-grip");
    grip.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      move(event.key === "ArrowLeft" ? -1 : 1);
      grip.focus();
    });
    actions.append(
      actionButton("Rename file", "edit", async () => {
        const filename = window.prompt("File name", metadata.get(card)?.filename || id);
        if (!filename) return;
        try {
          update(
            await json(url, {
              method: "PUT",
              headers: { "Content-Type": "application/json", "X-CSRF-Token": token(box) },
              body: JSON.stringify({ filename }),
            }),
          );
        } catch (error) {
          box.querySelector("[data-files-error]").textContent = error.message;
        }
      }),
      download,
      actionButton("Remove file", "trash", () => {
        removed.add(box);
        card.remove();
        save(box);
      }),
      grip,
    );
    sortable(box, card);
  } else {
    actions.append(download);
  }
  const footer = document.createElement("div");
  footer.className = "obox-file-footer";
  footer.append(name, actions);
  card.append(thumb, footer);
  box.querySelector("[data-files-list]").append(card);
  if (meta) update(meta);
  else {
    name.textContent = id;
    json(`${url}/state`)
      .then(update)
      .catch((error) => {
        box.querySelector("[data-files-error]").textContent = error.message;
      });
  }
}

async function addFiles(box, selected) {
  const input = box.querySelector('input[type="file"]');
  if (!input || input.disabled || !selected.length) return;
  const errorBox = box.querySelector("[data-files-error]");
  const max = box.dataset.maxCount;
  if (max !== "" && ids(box).length + selected.length > Number(max)) {
    errorBox.textContent = `Choose at most ${max} files.`;
    input.value = "";
    return;
  }
  const form = box.closest("form");
  pending.set(form, (pending.get(form) || 0) + 1);
  for (const item of form.querySelectorAll('[type="submit"]')) {
    if (!item.disabled) {
      item.dataset.filesDisabled = "";
      item.disabled = true;
    }
  }
  input.disabled = true;
  errorBox.textContent = "";
  try {
    for (const file of selected) {
      const row = document.createElement("div");
      row.className = "obox-file obox-file-upload";
      const name = document.createElement("span");
      name.className = "obox-file-name";
      name.textContent = file.name;
      const progress = document.createElement("progress");
      progress.max = file.size;
      progress.value = 0;
      progress.setAttribute("aria-label", `Uploading ${file.name}`);
      row.append(name, progress);
      box.querySelector("[data-files-list]").append(row);
      try {
        const meta = await upload(file, box, (bytes) => {
          progress.value = bytes;
        });
        row.remove();
        entry(box, meta.id, meta);
        save(box);
      } catch (error) {
        row.remove();
        errorBox.textContent = `${file.name}: ${error.message}. Select the file to retry.`;
      }
    }
  } finally {
    input.value = "";
    input.disabled = false;
    pending.set(form, pending.get(form) - 1);
    if (!pending.get(form)) {
      for (const item of form.querySelectorAll("[data-files-disabled]")) {
        item.disabled = false;
        delete item.dataset.filesDisabled;
      }
    }
  }
}

function initialize(root) {
  for (const box of root.querySelectorAll("[data-files]")) {
    if (initialized.has(box)) continue;
    initialized.add(box);
    try {
      const records = JSON.parse(box.dataset.filesMeta || "[]");
      for (const id of ids(box))
        entry(
          box,
          id,
          records.find((meta) => meta.id === id),
          Boolean(box.dataset.owner),
        );
    } catch {
      box.querySelector("[data-files-error]").textContent = "Invalid attachment list.";
    }
    if (box.dataset.readonly !== "false") continue;
    box.addEventListener("dragover", (event) => {
      event.preventDefault();
      box.classList.add("obox-files-dragover");
    });
    box.addEventListener("dragleave", () => box.classList.remove("obox-files-dragover"));
    box.addEventListener("drop", (event) => {
      event.preventDefault();
      box.classList.remove("obox-files-dragover");
      if (event.dataTransfer.files.length) addFiles(box, [...event.dataTransfer.files]);
    });
  }
}

initialize(document);
new MutationObserver(() => initialize(document)).observe(document.body, {
  childList: true,
  subtree: true,
});
document.addEventListener("submit", (event) => {
  if (pending.get(event.target)) event.preventDefault();
});
document.addEventListener("change", (event) => {
  const input = event.target.closest('[data-files] input[type="file"]');
  if (input) addFiles(input.closest("[data-files]"), [...input.files]);
});
