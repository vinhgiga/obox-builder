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

function button(label, text, action) {
  const item = document.createElement("button");
  item.type = "button";
  item.textContent = text;
  item.title = label;
  item.setAttribute("aria-label", label);
  item.onclick = action;
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

function entry(box, id, meta) {
  const card = document.createElement("div");
  card.className = "obox-file";
  card.dataset.fileId = id;
  card.setAttribute("role", "listitem");
  const url = `${box.dataset.url}/${encodeURIComponent(id)}`;
  const thumb = button("Preview file", "File", () => preview(box, card));
  thumb.className = "obox-file-preview";
  const name = document.createElement("span");
  name.className = "obox-file-name";
  const actions = document.createElement("div");
  actions.className = "obox-file-actions";
  const download = document.createElement("a");
  download.href = `${url}?download=true`;
  download.textContent = "↓";
  download.title = "Download";
  download.setAttribute("aria-label", "Download");
  actions.append(download);
  function update(value) {
    metadata.set(card, value);
    name.textContent = name.title = value.filename;
    thumb.setAttribute("aria-label", `Preview ${value.filename}`);
    const image = media(value, url);
    thumb.replaceChildren(image || document.createTextNode("File"));
  }
  if (box.dataset.readonly === "false") {
    const move = (step) => {
      const sibling = step < 0 ? card.previousElementSibling : card.nextElementSibling;
      if (!sibling?.dataset.fileId) return;
      if (step < 0) sibling.before(card);
      else sibling.after(card);
      save(box);
    };
    actions.append(
      button("Rename file", "✎", async () => {
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
      button("Move file left", "←", () => move(-1)),
      button("Move file right", "→", () => move(1)),
      button("Remove file", "×", () => {
        card.remove();
        save(box);
      }),
    );
    card.draggable = true;
    card.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("application/x-obox-file", id);
      event.dataTransfer.effectAllowed = "move";
    });
    card.addEventListener("drop", (event) => {
      const sourceId = event.dataTransfer.getData("application/x-obox-file");
      const source = [...box.querySelectorAll("[data-file-id]")].find(
        (item) => item.dataset.fileId === sourceId,
      );
      if (source && source !== card) {
        event.preventDefault();
        card.before(source);
        save(box);
      }
    });
  }
  card.append(thumb, name, actions);
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
      row.className = "obox-file";
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
