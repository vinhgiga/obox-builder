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

function preview(box, card) {
  const meta = metadata.get(card);
  if (!meta) return;
  const url = `${box.dataset.url}/${encodeURIComponent(meta.id)}`;
  const dialog = document.createElement("dialog");
  dialog.className = "obox-file-dialog";
  dialog.setAttribute("aria-label", meta.filename);
  const header = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = meta.filename;
  const cards = [...box.querySelectorAll("[data-file-id]")];
  const index = cards.indexOf(card);
  const move = (step) => {
    dialog.close();
    preview(box, cards[(index + step + cards.length) % cards.length]);
  };
  header.append(title);
  if (cards.length > 1)
    header.append(
      button("Previous file", "←", () => move(-1)),
      button("Next file", "→", () => move(1)),
    );
  header.append(button("Close preview", "×", () => dialog.close()));
  const content = media(meta, url);
  if (content?.tagName === "VIDEO") {
    content.controls = true;
    content.muted = false;
  }
  const download = document.createElement("a");
  download.href = `${url}?download=true`;
  download.textContent = "Download";
  download.className = "btn btn-outline-secondary btn-sm";
  header.append(download);
  dialog.append(
    header,
    content || document.createTextNode("Preview is unavailable for this file type."),
  );
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        dialog.close();
    }
  });
  document.body.append(dialog);
  dialog.showModal();
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
