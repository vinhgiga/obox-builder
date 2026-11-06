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
  const dialog = document.createElement("dialog");
  dialog.className = "obox-file-lightbox";
  dialog.tabIndex = -1;
  dialog.setAttribute("aria-label", "File preview");

  const counter = document.createElement("span");
  counter.className = "obox-file-lightbox-counter";
  const download = document.createElement("a");
  download.className = "obox-file-lightbox-download";
  download.textContent = "Download";
  const actions = document.createElement("div");
  actions.className = "obox-file-lightbox-actions";
  actions.append(
    download,
    button("Close preview", "×", () => dialog.close()),
  );
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

  const previous = button("Previous file", "‹", () => move(-1));
  const next = button("Next file", "›", () => move(1));
  previous.className = "obox-file-lightbox-nav is-previous";
  next.className = "obox-file-lightbox-nav is-next";

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

  let index = start;
  function render(selected) {
    const value = metadata.get(cards[selected]);
    if (!value) return;
    index = selected;
    const url = `${box.dataset.url}/${encodeURIComponent(value.id)}`;
    const content = media(value, url);
    if (content?.tagName === "VIDEO") {
      content.controls = true;
      content.muted = false;
    }
    newMedia.replaceChildren(
      content || document.createTextNode("Preview is unavailable for this file type."),
    );
    title.textContent = value.filename;
    details.textContent = fileDetails(value);
    details.hidden = !details.textContent;
    counter.textContent = cards.length > 1 ? `${selected + 1} / ${cards.length}` : "";
    download.href = `${url}?download=true`;
    dialog.setAttribute("aria-label", value.filename);
    previous.hidden = next.hidden = thumbs.hidden = cards.length < 2;
    thumbButtons.forEach((thumb, position) => {
      thumb.classList.toggle("is-active", position === selected);
      if (position === selected) thumb.setAttribute("aria-current", "true");
      else thumb.removeAttribute("aria-current");
    });
  }
  function move(step) {
    if (cards.length < 2) return;
    render((index + step + cards.length) % cards.length);
  }

  dialog.append(toolbar, previous, figure, next, thumbs);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
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
