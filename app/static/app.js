"use strict";

/* Firm PDF Workspace — vanilla JS, no build step.
   Flow: pick a tool → upload files (stored server-side so thumbnails work) →
   set options → run a job → poll/advance it → download the outputs. */

const state = {
  tools: [],
  groups: [],
  tool: null,
  batch: null,
  job: null,
  files: [],
  pages: new Set(),
  pagesByDoc: {},
  rotations: {},
  gridDoc: null,
  pageOrder: [],
  preview: null,
  totalPages: 0,
  library: [],
  stats: {},
  health: null,
  openBatches: new Set(),
  timer: null,
  running: false,
  unlockPassword: "",
  passwordRetry: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ helpers */
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (_error) {
    const dropped = new Error(
      "The connection dropped before the server answered, so nothing was saved. Try again."
    );
    dropped.network = true;
    throw dropped;
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  if (!response.ok) {
    const failure = new Error(payload.error || fallbackMessage(response.status));
    failure.status = response.status;
    failure.code = payload.code || "";
    throw failure;
  }
  return payload;
}

/* A host can answer with a plain-text error page, so give those a real meaning
   instead of a bare status code. */
function fallbackMessage(status) {
  const limitMb = state.health && state.health.limits ? state.health.limits.upload_mb : 4;
  if (status === 413) {
    return `That upload is bigger than this deployment accepts (${limitMb} MB per request). Send fewer files at a time.`;
  }
  if (status === 502 || status === 503 || status === 504) {
    return "The server is not answering yet (a new version may be deploying). Wait a few seconds and try again.";
  }
  return `Request failed (${status})`;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function bytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function setStatus(message, tone) {
  const node = $("status");
  node.textContent = message || "";
  node.className = `status${tone ? ` is-${tone}` : ""}`;
}

function toolLabel(id) {
  const found = state.tools.find((tool) => tool.id === id);
  return found ? found.label : id;
}

function isImage(name) {
  return /\.(png|jpe?g|jfif|gif|webp|bmp|tiff?)$/i.test(name || "");
}

/* Phones name their pictures whatever they like (and send HEIC), so the file
   type decides as much as the extension. */
function acceptsImage(file) {
  return isImage(file.name) || (file.type || "").startsWith("image/");
}

function compressRanges(pages) {
  const numbers = [...pages].sort((a, b) => a - b);
  if (!numbers.length) return "";
  const parts = [];
  let start = numbers[0];
  let previous = numbers[0];
  for (const page of numbers.slice(1)) {
    if (page === previous + 1) {
      previous = page;
      continue;
    }
    parts.push(start === previous ? `${start}` : `${start}-${previous}`);
    start = page;
    previous = page;
  }
  parts.push(start === previous ? `${start}` : `${start}-${previous}`);
  return parts.join(",");
}

function filePagesSpec(docId) {
  const set = state.pagesByDoc[docId];
  return set && set.size ? compressRanges(set) : "";
}

/* Options are remembered per tool, so a repeated run does not start over. */
function savedOptions(toolId) {
  try {
    return JSON.parse(window.localStorage.getItem(`firmpdf.options.${toolId}`) || "{}") || {};
  } catch (_error) {
    return {};
  }
}

function rememberOptions(toolId, options) {
  const keep = {};
  for (const [key, value] of Object.entries(options || {})) {
    if (key === "password" || key === "owner_password") continue;
    keep[key] = value;
  }
  try {
    window.localStorage.setItem(`firmpdf.options.${toolId}`, JSON.stringify(keep));
  } catch (_error) {
    /* storage blocked or full */
  }
}

/* --------------------------------------------------------------------- boot */
document.addEventListener("DOMContentLoaded", boot);

async function boot() {
  bindShell();
  try {
    const [toolsPayload, health, library] = await Promise.all([
      api("/api/tools"),
      api("/api/health"),
      api("/api/library"),
    ]);
    state.tools = toolsPayload.tools;
    state.groups = toolsPayload.groups;
    state.health = health;
    state.library = library.batches;
    state.stats = library.stats;
    $("lookup-panel").hidden = !health.exa;
    renderToolrail();
    renderLibrary();
    renderHealth();
    selectTool(state.tools[0] ? state.tools[0].id : null);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function renderHealth() {
  const health = state.health;
  if (!health) return;
  const bits = [health.database, `${health.storage} storage`, `${health.jobs} jobs`];
  if (!health.ocr) bits.push("OCR unavailable");
  setStatus(bits.join(" · "), health.ocr ? "ok" : "");
}

function bindShell() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  });

  const dropzone = $("dropzone");
  const input = $("file-input");
  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
  ["dragenter", "dragover"].forEach((type) =>
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragging");
    })
  );
  ["dragleave", "drop"].forEach((type) =>
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
    })
  );
  dropzone.addEventListener("drop", (event) => {
    if (event.dataTransfer && event.dataTransfer.files) handleFiles(event.dataTransfer.files);
  });
  input.addEventListener("change", () => {
    handleFiles(input.files);
    input.value = "";
  });

  $("clear-files").addEventListener("click", resetUpload);
  $("start-over").addEventListener("click", () => {
    resetUpload();
    hideResults();
  });
  $("run").addEventListener("click", startRun);
  $("refresh-library").addEventListener("click", refreshLibrary);

  document.querySelectorAll("[data-pages]").forEach((button) => {
    button.addEventListener("click", () => pageAction(button.dataset.pages));
  });

  // Turn a whole file at once, next to the page-selection helpers.
  const gridActions = document.querySelector(".pagegrid-actions");
  if (gridActions && !gridActions.dataset.turns) {
    gridActions.dataset.turns = "1";
    [
      ["-90", "Rotate all ↺", "Turn every page in this file to the left"],
      ["90", "Rotate all ↻", "Turn every page in this file to the right"],
    ].forEach(([step, label, title]) =>
      gridActions.appendChild(
        el("button", { class: "chip", type: "button", title, text: label, onclick: () => rotateAll(Number(step)) })
      )
    );
  }

  $("lookup-form").addEventListener("submit", runLookup);
  $("password-form").addEventListener("submit", usePassword);
  $("password-cancel").addEventListener("click", () => {
    state.unlockPassword = "";
    closePassword();
    setStatus("Password cleared.");
  });
}

function switchView(view) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === view));
  document.querySelectorAll(".view").forEach((section) =>
    section.classList.toggle("is-active", section.id === `view-${view}`)
  );
  if (view === "library") refreshLibrary();
}

/* ---------------------------------------------------------------- tool rail */
function renderToolrail() {
  const rail = $("toolrail");
  rail.innerHTML = "";
  for (const group of state.groups) {
    rail.appendChild(el("div", { class: "group-label", text: group }));
    for (const tool of state.tools.filter((item) => item.group === group)) {
      rail.appendChild(
        el("button", {
          class: "tool",
          type: "button",
          "data-tool": tool.id,
          text: tool.label,
          onclick: () => selectTool(tool.id),
        })
      );
    }
  }
  markActiveTool();
}

function markActiveTool() {
  document.querySelectorAll(".tool").forEach((button) =>
    button.classList.toggle("is-active", state.tool && button.dataset.tool === state.tool.id)
  );
}

function selectTool(id) {
  if (!id) return;
  const tool = state.tools.find((item) => item.id === id);
  if (!tool) return;
  if (state.tool && state.tool.id === id) {
    markActiveTool();
    return;
  }
  discardDraft();
  state.tool = tool;
  state.files = [];
  state.batch = null;
  state.job = null;
  state.pages = new Set();
  state.pagesByDoc = {};
  state.rotations = {};
  state.gridDoc = null;
  state.pageOrder = [];
  state.preview = null;
  state.totalPages = 0;
  state.unlockPassword = "";
  state.passwordRetry = null;
  $("password-panel").hidden = true;

  $("tool-group").textContent = tool.group;
  $("tool-title").textContent = tool.label;
  $("tool-summary").textContent = tool.summary;
  $("tool-badge").textContent = tool.accepts === "images" ? "Images" : "PDF";
  state.dropHint =
    tool.accepts === "images"
      ? `PNG, JPG or WebP up to ${state.health ? state.health.limits.upload_mb : 40} MB each.`
      : `PDF files up to ${state.health ? state.health.limits.upload_mb : 40} MB each, ${state.health ? state.health.limits.pages : 150} pages max per file.`;
  $("drop-hint").textContent = state.dropHint;
  $("file-input").accept = tool.accepts === "images" ? "image/*" : ".pdf,application/pdf";

  const runButton = $("run");
  runButton.textContent = tool.id === "pdf-to-excel" ? "Convert to Excel" : "Run";
  runButton.disabled = true;

  renderOptions();
  renderFiles();
  hideProgress();
  hideResults();
  markActiveTool();
  updatePanels();
}

function updatePanels() {
  const hasFiles = state.files.length > 0;
  // The drop area stays at the top of the workspace, so the convert button,
  // the progress and the download all sit below the place files arrive and the
  // next statement can be dropped without scrolling past the results.
  $("dropzone").hidden = false;
  $("dropzone").classList.toggle("is-compact", hasFiles);
  $("drop-hint").textContent = hasFiles
    ? "Drop files here to replace this set, or browse."
    : state.dropHint || "";
  $("files-panel").hidden = !hasFiles;
  $("options-panel").hidden = !state.tool || !state.tool.options.length;
  $("runbar").hidden = !hasFiles;
  $("run").disabled = !hasFiles || state.running;
  const picked = Object.values(state.pagesByDoc).filter((set) => set && set.size).length;
  const rangeNote = state.tool && state.tool.page_ranges && !state.tool.multiple
    ? " · pages chosen below apply to the run"
    : "";
  $("run-meta").textContent = hasFiles
    ? `${state.files.length} file(s) ready${picked ? ` · pages chosen for ${picked} file(s)` : ""}${rangeNote}`
    : "";
}

/* ------------------------------------------------------------------ uploads */
async function handleFiles(fileList) {
  const files = Array.from(fileList || []).filter((file) => file.size > 0);
  if (!files.length || !state.tool) return;
  const tool = state.tool;

  const accepted = tool.accepts === "images" ? files.filter(acceptsImage) : files.filter((file) => /\.pdf$/i.test(file.name));
  if (accepted.length !== files.length) {
    const skipped = files
      .filter((file) => !accepted.includes(file))
      .map((file) => file.name)
      .slice(0, 3)
      .join(", ");
    setStatus(
      `Skipped ${skipped || "some files"}: ${tool.label} needs ${tool.accepts === "images" ? "images" : "PDFs"}.`,
      "error"
    );
  }
  if (!accepted.length) return;
  if (!tool.multiple && accepted.length > 1) {
    setStatus(`${tool.label} works on one file at a time.`, "error");
    return;
  }

  const limitMb = state.health && state.health.limits ? state.health.limits.upload_mb : 40;
  let ready = accepted;
  if (tool.accepts === "images") {
    setStatus("Preparing pictures…");
    ready = await prepareImages(accepted, limitMb * 1024 * 1024 * 0.7);
  }
  const totalMb = ready.reduce((sum, file) => sum + file.size, 0) / (1024 * 1024);
  if (totalMb > limitMb) {
    setStatus(
      `Those files total ${totalMb.toFixed(1)} MB and this deployment accepts ${limitMb} MB per upload. Send them in smaller groups.`,
      "error"
    );
    return;
  }

  discardDraft();
  setStatus("Uploading…");
  const form = new FormData();
  ready.forEach((file) => form.append("files", file));
  try {
    const payload = await uploadWithRetry(form);
    state.batch = payload.batch;
    state.files = payload.batch.documents || [];
    state.pages = new Set();
    state.pagesByDoc = {};
    state.rotations = {};
    state.gridDoc = null;
    state.pageOrder = [];
    renderFiles();
    updatePanels();
    setStatus("Uploaded. Set the options and run.", "ok");
    if (tool.page_ranges) await loadPageGrid();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

/* Nothing is committed server-side until the 201 comes back, so a dropped
   connection (cold start, deploy swap, a flaky link) is worth exactly one retry
   with an explanation rather than a bare browser error. */
async function uploadWithRetry(form) {
  try {
    return await api("/api/uploads", { method: "POST", body: form });
  } catch (error) {
    if (!error.network) throw error;
    setStatus("The upload was interrupted — trying once more…");
    await new Promise((resolve) => setTimeout(resolve, 1500));
    return api("/api/uploads", { method: "POST", body: form });
  }
}

function renderFiles() {
  const list = $("file-list");
  list.innerHTML = "";
  state.files.forEach((file) => {
    const pages = file.meta && file.meta.pages ? `${file.meta.pages} page(s) · ` : "";
    const perFile = Boolean(state.tool && state.tool.multiple && /\.pdf$/i.test(file.filename));
    const spec = filePagesSpec(file.id);
    const children = [
      el("span", { class: "file-icon", text: isImage(file.filename) ? "IMG" : "PDF" }),
      el("div", { class: "file-main" }, [
        el("div", { class: "file-name", text: file.filename }),
        el("div", { class: "file-meta", text: `${pages}${bytes(file.size)}${spec ? ` · pages ${spec}` : ""}` }),
      ]),
    ];
    if (perFile) {
      children.push(
        el("button", {
          class: `chip${spec ? " is-on" : ""}`,
          type: "button",
          title: "Choose pages for this file",
          text: spec ? `Pages ${spec}` : "Pages",
          onclick: () => openPageGrid(file.id),
        })
      );
    }
    children.push(
      el("button", {
        class: "icon-button",
        type: "button",
        title: "Remove file",
        text: "✕",
        onclick: () => removeFile(file.id),
      })
    );
    list.appendChild(el("div", { class: "file" }, children));
  });
}

async function removeFile(id) {
  try {
    await api(`/api/documents/${id}`, { method: "DELETE" });
  } catch (_error) {
    /* already gone */
  }
  state.files = state.files.filter((file) => file.id !== id);
  delete state.pagesByDoc[id];
  delete state.rotations[id];
  if (state.gridDoc === id) {
    state.gridDoc = null;
    state.totalPages = 0;
    state.pages = new Set();
    $("pagegrid").hidden = true;
    $("pagegrid-body").innerHTML = "";
  }
  if (!state.files.length) {
    resetUpload();
    return;
  }
  renderFiles();
  updatePanels();
  if (state.tool.page_ranges) await loadPageGrid();
}

async function resetUpload() {
  const batch = state.batch;
  state.files = [];
  state.batch = null;
  state.pages = new Set();
  state.pagesByDoc = {};
  state.rotations = {};
  state.gridDoc = null;
  state.pageOrder = [];
  state.preview = null;
  state.totalPages = 0;
  $("pagegrid").hidden = true;
  $("pagegrid-body").innerHTML = "";
  renderFiles();
  updatePanels();
  if (batch && !state.job) {
    try {
      await api(`/api/batches/${batch.id}`, { method: "DELETE" });
      refreshLibrary();
    } catch (_error) {
      /* ignore */
    }
  }
}

function discardDraft() {
  if (state.batch && !state.job) {
    api(`/api/batches/${state.batch.id}`, { method: "DELETE" }).catch(() => {});
  }
}

/* ---------------------------------------------------------------- page grid */
async function loadPageGrid(docId) {
  const pdf = docId
    ? state.files.find((file) => file.id === docId)
    : state.files.find((file) => /\.pdf$/i.test(file.filename));
  if (!pdf) {
    $("pagegrid").hidden = true;
    return;
  }
  if (!docId && state.gridDoc === pdf.id && state.totalPages) return;
  try {
    const payload = await api(`/api/documents/${pdf.id}/pages`);
    state.preview = pdf.id;
    state.gridDoc = pdf.id;
    state.totalPages = payload.pages;
    state.pageOrder = [];
    state.pages = new Set(state.pagesByDoc[pdf.id] || []);
    $("pagegrid-title").textContent = `Pages · ${pdf.filename}`;
    renderPageGrid();
    $("pagegrid").hidden = false;
    paintSelection();
    syncPageField();
  } catch (error) {
    $("pagegrid").hidden = true;
    if (error.code === "password_required") {
      askPassword(error.message, () => loadPageGrid(docId));
      return;
    }
    setStatus(error.message, "error");
  }
}

function openPageGrid(docId) {
  loadPageGrid(docId);
}

function pageAngle(page) {
  const map = state.gridDoc ? state.rotations[state.gridDoc] : null;
  return (map && Number(map[page])) || 0;
}

function renderPageGrid() {
  if (!state.preview) return;
  const body = $("pagegrid-body");
  body.innerHTML = "";
  const angle = Number((state.options && state.options.angle) || 0);
  for (let page = 1; page <= state.totalPages; page += 1) {
    const turned = pageAngle(page);
    const thumb = el("img", {
      src: `/api/documents/${state.preview}/thumb/${page}${passwordQuery()}`,
      alt: `Page ${page}`,
      loading: "lazy",
    });
    if (state.tool && state.tool.id === "rotate-pages" && angle) {
      thumb.style.transform = `rotate(${angle}deg)`;
    } else if (turned) {
      thumb.style.transform = `rotate(${turned}deg)`;
    }
    const button = el("button", { class: "page", type: "button", "data-page": page }, [thumb, el("span", { text: `Page ${page}` })]);
    button.addEventListener("click", () => togglePage(page));
    const controls = el("div", { class: "page-rotate" }, [
      el("button", {
        class: "rotate",
        type: "button",
        title: `Rotate page ${page} left`,
        "data-rotate": "left",
        text: "↺",
        onclick: (event) => {
          event.stopPropagation();
          rotatePage(page, -90);
        },
      }),
      el("button", {
        class: "rotate",
        type: "button",
        title: `Rotate page ${page} right`,
        "data-rotate": "right",
        text: "↻",
        onclick: (event) => {
          event.stopPropagation();
          rotatePage(page, 90);
        },
      }),
    ]);
    body.appendChild(el("div", { class: `page-cell${turned ? " is-rotated" : ""}` }, [button, controls]));
  }
  paintSelection();
}

/* Per-page rotation: the thumbnail turns immediately, and the angles travel
   with the run, so the converted output matches what the grid shows. */
function rotatePage(page, delta) {
  const doc = state.gridDoc;
  if (!doc) return;
  const map = { ...(state.rotations[doc] || {}) };
  const next = ((Number(map[page]) || 0) + delta + 360) % 360;
  if (next) map[page] = next;
  else delete map[page];
  if (Object.keys(map).length) state.rotations[doc] = map;
  else delete state.rotations[doc];
  renderPageGrid();
  const count = Object.keys(map).length;
  setStatus(count ? `${count} page(s) turned in this file.` : "Rotation cleared for this file.");
}

function rotateAll(delta) {
  const doc = state.gridDoc;
  if (!doc || !state.totalPages) return;
  const map = { ...(state.rotations[doc] || {}) };
  for (let page = 1; page <= state.totalPages; page += 1) {
    const next = ((Number(map[page]) || 0) + delta + 360) % 360;
    if (next) map[page] = next;
    else delete map[page];
  }
  if (Object.keys(map).length) state.rotations[doc] = map;
  else delete state.rotations[doc];
  renderPageGrid();
  setStatus(delta > 0 ? "Every page turned right." : "Every page turned left.");
}

function togglePage(page) {
  if (state.tool.id === "reorder-pages") {
    const index = state.pageOrder.indexOf(page);
    if (index === -1) state.pageOrder.push(page);
    else state.pageOrder.splice(index, 1);
    paintSelection();
    syncPageField();
    return;
  }
  if (!state.gridDoc) return;
  const set = new Set(state.pagesByDoc[state.gridDoc] || []);
  if (set.has(page)) set.delete(page);
  else set.add(page);
  if (set.size) state.pagesByDoc[state.gridDoc] = set;
  else delete state.pagesByDoc[state.gridDoc];
  state.pages = set;
  paintSelection();
  syncPageField();
  renderFiles();
  updatePanels();
}

function paintSelection() {
  document.querySelectorAll(".page").forEach((button) => {
    const page = Number(button.dataset.page);
    const order = state.tool && state.tool.id === "reorder-pages" ? state.pageOrder.indexOf(page) + 1 : 0;
    const selected = order > 0 || state.pages.has(page);
    button.classList.toggle("is-selected", selected);
    const badge = button.querySelector(".order");
    if (badge) badge.remove();
    if (order > 0) button.appendChild(el("span", { class: "order", text: String(order) }));
  });
  const count = state.tool && state.tool.id === "reorder-pages" ? state.pageOrder.length : state.pages.size;
  $("page-selection").textContent = count
    ? `${count} page(s) selected`
    : state.gridDoc && state.tool && state.tool.multiple
      ? "Nothing selected — this file is used in full"
      : "Nothing selected — the whole file is used";
}

function pageAction(action) {
  if (!state.totalPages) return;
  if (state.tool.id === "reorder-pages") {
    if (action === "all") state.pageOrder = Array.from({ length: state.totalPages }, (_value, index) => index + 1);
    else if (action === "invert") {
      const current = new Set(state.pageOrder);
      state.pageOrder = Array.from({ length: state.totalPages }, (_value, index) => index + 1).filter((page) => !current.has(page));
    } else state.pageOrder = [];
  } else if (action === "all") {
    state.pages = new Set(Array.from({ length: state.totalPages }, (_value, index) => index + 1));
  } else if (action === "invert") {
    const next = new Set();
    for (let page = 1; page <= state.totalPages; page += 1) if (!state.pages.has(page)) next.add(page);
    state.pages = next;
  } else {
    state.pages = new Set();
  }
  if (state.tool.id !== "reorder-pages" && state.gridDoc) {
    if (state.pages.size) state.pagesByDoc[state.gridDoc] = state.pages;
    else delete state.pagesByDoc[state.gridDoc];
  }
  paintSelection();
  syncPageField();
  if (state.tool.id !== "reorder-pages") {
    renderFiles();
    updatePanels();
  }
}

function syncPageField() {
  if (!state.tool) return;
  if (state.tool.multiple) return;  // per-file pages travel separately
  const name = state.tool.id === "reorder-pages" ? "order" : "pages";
  const input = document.querySelector(`[data-option="${name}"]`);
  if (!input) return;
  input.value = state.tool.id === "reorder-pages" ? state.pageOrder.join(",") : compressRanges(state.pages);
  if (state.options) state.options[name] = input.value;
}

/* ----------------------------------------------------------------- options */
function renderOptions() {
  const body = $("options-body");
  body.innerHTML = "";
  state.options = {};
  const tool = state.tool;
  const saved = savedOptions(tool.id);
  $("options-note").textContent = tool.page_ranges
    ? "Choose pages in the grid above, or type a range here."
    : tool.multiple
      ? "Use “Pages” next to a file to convert only part of it."
      : "";
  for (const option of tool.options || []) {
    const initial = Object.prototype.hasOwnProperty.call(saved, option.name) ? saved[option.name] : option.default;
    state.options[option.name] = initial;
    if (option.type === "bool") {
      const input = el("input", { type: "checkbox", "data-option": option.name });
      input.checked = Boolean(initial);
      input.addEventListener("change", () => {
        state.options[option.name] = input.checked;
        rememberOptions(tool.id, state.options);
        if (option.name === "angle") renderPageGrid();
      });
      body.appendChild(el("label", { class: "option switch" }, [input, el("label", { text: option.label })]));
      continue;
    }

    let input;
    if (option.type === "select") {
      input = el("select", { "data-option": option.name });
      for (const choice of option.choices || []) {
        const node = el("option", { value: choice, text: choice });
        if (String(choice) === String(initial)) node.selected = true;
        input.appendChild(node);
      }
    } else {
      const type = option.type === "number" ? "number" : option.type === "password" ? "password" : "text";
      input = el("input", {
        type,
        "data-option": option.name,
        value: initial === undefined || initial === null ? "" : String(initial),
      });
      if (option.min !== undefined && option.min !== null) input.min = option.min;
      if (option.max !== undefined && option.max !== null) input.max = option.max;
    }
    input.addEventListener("change", () => {
      state.options[option.name] = option.type === "number" ? Number(input.value) : input.value;
      rememberOptions(tool.id, state.options);
      if (option.name === "angle") renderPageGrid();
      if (option.name === "pages" && input.value.trim() === "") {
        state.pages = new Set();
        paintSelection();
      }
    });
    body.appendChild(
      el("div", { class: "option" }, [
        el("label", { for: `opt-${option.name}`, text: option.label }),
        input,
        option.help ? el("span", { class: "help", text: option.help }) : null,
      ])
    );
    input.id = `opt-${option.name}`;
  }
}

function collectOptions() {
  const options = {};
  for (const option of state.tool.options || []) {
    const node = document.querySelector(`[data-option="${option.name}"]`);
    if (!node) continue;
    if (option.type === "bool") options[option.name] = node.checked;
    else if (option.type === "number") options[option.name] = Number(node.value || option.default || 0);
    else options[option.name] = node.value;
  }
  if (state.tool.page_ranges) {
    if (state.tool.id === "reorder-pages") options.order = state.pageOrder.join(",");
    else if (state.pages.size) options.pages = compressRanges(state.pages);
  }
  for (const key of ["pages", "order"]) {
    if (options[key] !== undefined) options[key] = String(options[key] || "").trim();
  }
  if (state.tool.multiple) {
    const byDoc = {};
    for (const [docId, set] of Object.entries(state.pagesByDoc)) {
      if (set && set.size) byDoc[docId] = compressRanges(set);
    }
    if (Object.keys(byDoc).length) options.pages_by_doc = byDoc;
  }
  const turns = {};
  for (const [docId, pages] of Object.entries(state.rotations)) {
    if (pages && Object.keys(pages).length) turns[docId] = { ...pages };
  }
  if (Object.keys(turns).length) options.rotate_by_doc = turns;
  if (state.unlockPassword) options.unlock_password = state.unlockPassword;
  return options;
}

/* ----------------------------------------------------------------- pictures */
const IMAGE_SAFE_EXTENSION = /\.(png|jpe?g|jfif|webp|bmp|tiff?)$/i;
const IMAGE_MAX_SIDE = 2600;

/* A phone photo is 4–12 MB and often HEIC, which this server cannot decode. The
   browser decodes it, fits it to a sensible resolution and hands back a JPEG
   inside the upload limit — so “Images → PDF” works with camera pictures. */
async function decodeImage(file) {
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(file);
    } catch (_error) {
      /* fall back to an <img> below */
    }
  }
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error(`could not read ${file.name}`));
    };
    image.src = url;
  });
}

async function shrinkImage(file, budget) {
  const bitmap = await decodeImage(file);
  const sourceWidth = bitmap.width || bitmap.naturalWidth || 0;
  const sourceHeight = bitmap.height || bitmap.naturalHeight || 0;
  if (!sourceWidth || !sourceHeight) throw new Error(`could not read ${file.name}`);
  const scale = Math.min(1, IMAGE_MAX_SIDE / Math.max(sourceWidth, sourceHeight));
  let width = Math.max(1, Math.round(sourceWidth * scale));
  let height = Math.max(1, Math.round(sourceHeight * scale));
  let quality = 0.9;
  let blob = null;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, width, height);
    context.drawImage(bitmap, 0, 0, width, height);
    blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (blob && blob.size <= budget) break;
    quality = Math.max(0.55, quality - 0.12);
    if (attempt >= 1) {
      width = Math.max(1, Math.round(width * 0.85));
      height = Math.max(1, Math.round(height * 0.85));
    }
  }
  if (typeof bitmap.close === "function") bitmap.close();
  if (!blob) throw new Error(`could not prepare ${file.name}`);
  const stem = file.name.replace(/\.[^.]+$/, "") || "picture";
  return new File([blob], `${stem}.jpg`, { type: "image/jpeg", lastModified: Date.now() });
}

async function prepareImages(files, budget) {
  const needsWork = (file) => file.size > budget / 2 || !IMAGE_SAFE_EXTENSION.test(file.name);
  if (!files.some(needsWork)) return files;
  const prepared = [];
  let converted = 0;
  for (const file of files) {
    if (!needsWork(file)) {
      prepared.push(file);
      continue;
    }
    try {
      prepared.push(await shrinkImage(file, budget));
      converted += 1;
    } catch (_error) {
      prepared.push(file); // the server reports the precise reason
    }
  }
  if (converted) setStatus(`Prepared ${converted} picture(s) for upload.`);
  return prepared;
}

/* ---------------------------------------------------------------- passwords */
function passwordQuery() {
  return state.unlockPassword ? `?password=${encodeURIComponent(state.unlockPassword)}` : "";
}

function askPassword(message, retry) {
  state.passwordRetry = typeof retry === "function" ? retry : null;
  $("password-note").textContent = message || "Enter the PDF password to continue.";
  $("password-panel").hidden = false;
  $("password-input").value = "";
  $("password-input").focus();
}

function closePassword() {
  state.passwordRetry = null;
  $("password-panel").hidden = true;
}

async function usePassword(event) {
  event.preventDefault();
  const value = $("password-input").value.trim();
  if (!value) {
    $("password-input").focus();
    return;
  }
  state.unlockPassword = value;
  const retry = state.passwordRetry;
  closePassword();
  setStatus("Password set for this run.");
  if (retry) await retry();
  else startRun();
}

/* --------------------------------------------------------------------- run */
async function startRun() {
  if (!state.batch || state.running) return;
  state.running = true;
  $("run").disabled = true;
  hideResults();
  showProgress(0, "Starting…");
  try {
    const payload = await api(`/api/batches/${state.batch.id}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool: state.tool.id, options: collectOptions() }),
    });
    state.batch = payload.batch;
    state.job = payload.job;
    setStatus("Working…");
    watchJob();
  } catch (error) {
    state.running = false;
    $("run").disabled = false;
    hideProgress();
    setStatus(error.message, "error");
  }
}

async function watchJob() {
  clearTimeout(state.timer);
  try {
    let job = state.job;
    if (!job) return;
    if (job.mode === "poll" && (job.status === "queued" || job.status === "running")) {
      const payload = await api(`/api/jobs/${job.id}/advance`, { method: "POST" });
      job = payload.job;
      if (payload.batch) state.batch = payload.batch;
    } else {
      const payload = await api(`/api/jobs/${job.id}`);
      job = payload.job;
    }
    state.job = job;
    showProgress(job.progress, job.message || job.status);
    if (job.status === "done" || job.status === "failed") {
      finishJob(job);
      return;
    }
    state.timer = setTimeout(watchJob, 700);
  } catch (error) {
    state.running = false;
    $("run").disabled = false;
    setStatus(error.message, "error");
  }
}

async function finishJob(job) {
  state.running = false;
  $("run").disabled = false;
  clearTimeout(state.timer);
  showProgress(100, job.message || job.status);
  try {
    const payload = await api(`/api/batches/${job.batch_id}`);
    state.batch = payload.batch;
    renderResults(payload.batch, job);
  } catch (error) {
    setStatus(error.message, "error");
  }
  if (job.password_required) {
    setStatus("That PDF is locked — enter its password.", "error");
    askPassword(job.password_message || "This PDF is password protected.", () => startRun());
  } else {
    setStatus(job.status === "done" ? "Done." : "Finished with problems.", job.status === "done" ? "ok" : "error");
  }
  refreshLibrary();
}

function showProgress(percent, message) {
  $("progress-panel").hidden = false;
  $("progress-fill").style.width = `${Math.max(0, Math.min(percent, 100))}%`;
  $("progress-percent").textContent = `${Math.max(0, Math.min(percent, 100))}%`;
  $("progress-message").textContent = message || "";
}

function hideProgress() {
  $("progress-panel").hidden = true;
  $("progress-fill").style.width = "0%";
}

/* ----------------------------------------------------------------- results */
function renderResults(batch, job) {
  $("results-panel").hidden = false;
  const body = $("results-body");
  body.innerHTML = "";
  const outputs = (batch.documents || []).filter((doc) => doc.kind === "output");
  const inputs = (batch.documents || []).filter((doc) => doc.kind === "input");

  if (!outputs.length) {
    body.appendChild(el("p", { class: "muted", text: "No files were produced." }));
  }

  for (const doc of outputs) {
    const meta = doc.meta || {};
    const stats = el("div", { class: "result-stats" });
    if (meta.rows !== undefined) stats.appendChild(el("span", { class: "tag", text: `${meta.rows} rows` }));
    if (meta.pages !== undefined) stats.appendChild(el("span", { class: "tag", text: `${meta.pages} pages` }));
    if (meta.doc_label) stats.appendChild(el("span", { class: "tag", text: meta.doc_label }));
    if (meta.words !== undefined) stats.appendChild(el("span", { class: "tag", text: `${meta.words} words` }));
    if (meta.checks) {
      const checks = meta.checks;
      if (checks.Error) stats.appendChild(el("span", { class: "tag bad", text: `${checks.Error} error` }));
      if (checks.Warning) stats.appendChild(el("span", { class: "tag warn", text: `${checks.Warning} warning` }));
      if (checks.Passed) stats.appendChild(el("span", { class: "tag ok", text: `${checks.Passed} passed` }));
    }

    body.appendChild(
      el("div", { class: "result" }, [
        el("span", { class: "file-icon", text: (doc.filename.split(".").pop() || "FILE").slice(0, 3).toUpperCase() }),
        el("div", { class: "file-main" }, [
          el("div", { class: "file-name", text: doc.filename }),
          el("div", { class: "file-meta", text: bytes(doc.size) }),
          stats,
        ]),
        el("a", {
          class: "secondary download",
          href: `${doc.download_url}`,
          text: "Download",
          download: doc.filename,
        }),
      ])
    );
  }

  if (outputs.length > 1) {
    body.appendChild(
      el("div", { class: "zip-row" }, [
        el("a", {
          class: "primary download",
          href: `/api/batches/${batch.id}/download.zip`,
          text: `Download all ${outputs.length} as one ZIP`,
          download: `${batch.tool || "results"}.zip`,
        }),
      ])
    );
  }

  const inputsNote = el("p", {
    class: "muted",
    text: `${inputs.length} source file(s) kept in the library · ${batch.title}`,
  });
  body.appendChild(inputsNote);

  const errors = (job && job.errors) || [];
  if (errors.length) {
    const box = el("div", { class: "errors" });
    for (const error of errors) {
      box.appendChild(el("div", { class: "error-row", text: error.message || String(error) }));
    }
    body.appendChild(box);
  }
}

function hideResults() {
  $("results-panel").hidden = true;
  $("results-body").innerHTML = "";
}

/* ----------------------------------------------------------------- library */
async function refreshLibrary() {
  try {
    const payload = await api("/api/library");
    state.library = payload.batches;
    state.stats = payload.stats;
    $("lib-count").textContent = String(payload.batches.length);
    renderLibrary();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function renderLibrary() {
  $("lib-count").textContent = String(state.library.length);
  const stats = $("library-stats");
  stats.innerHTML = "";
  const items = [
    ["Batches", state.stats.batches || 0],
    ["Documents", state.stats.documents || 0],
    ["Outputs", state.stats.outputs || 0],
    ["Stored", bytes(state.stats.bytes || 0)],
  ];
  for (const [label, value] of items) {
    stats.appendChild(el("div", { class: "stat" }, [el("strong", { text: String(value) }), el("span", { text: label })]));
  }

  const list = $("library-list");
  list.innerHTML = "";
  if (!state.library.length) {
    list.appendChild(el("p", { class: "muted", text: "Nothing here yet. Convert or organise a file and it will appear." }));
    return;
  }

  for (const batch of state.library) {
    const counts = batch.counts || {};
    const inputs = counts.input || 0;
    const outputs = counts.output || 0;
    const head = el("div", { class: "batch-head" }, [
      el("div", { class: "file-main" }, [
        el("div", { class: "batch-title", text: batch.title }),
        el("div", { class: "batch-meta", text: `${toolLabel(batch.tool)} · ${batch.created_at.replace("T", " ").replace("Z", "")} · ${inputs} in / ${outputs} out` }),
      ]),
      el("div", { class: "batch-actions" }, [
        outputs > 1
          ? el("a", {
              class: "chip",
              href: `/api/batches/${batch.id}/download.zip`,
              text: "ZIP",
              title: "Download every result in this batch as a ZIP",
              download: `${batch.tool || "results"}.zip`,
            })
          : null,
        el("button", {
          class: "chip",
          type: "button",
          text: state.openBatches.has(batch.id) ? "Hide" : "Open",
          onclick: () => toggleBatch(batch.id),
        }),
        el("button", {
          class: "chip",
          type: "button",
          text: "Delete",
          onclick: () => deleteBatch(batch.id),
        }),
      ]),
    ]);
    const holder = el("div", { class: "batch" }, [head]);
    if (state.openBatches.has(batch.id) && batch.documents) {
      const body = el("div", { class: "batch-body" });
      for (const doc of batch.documents) {
        body.appendChild(
          el("div", { class: "doc-row" }, [
            el("span", { class: "kind", text: doc.kind }),
            el("span", { text: doc.filename }),
            el("span", { class: "muted", text: bytes(doc.size) }),
            el("a", { href: doc.download_url, text: "Download", download: doc.filename }),
          ])
        );
      }
      holder.appendChild(body);
    }
    list.appendChild(holder);
  }
}

async function toggleBatch(id) {
  if (state.openBatches.has(id)) {
    state.openBatches.delete(id);
    renderLibrary();
    return;
  }
  try {
    const payload = await api(`/api/batches/${id}`);
    state.openBatches.add(id);
    const index = state.library.findIndex((batch) => batch.id === id);
    if (index >= 0) state.library[index] = payload.batch;
    renderLibrary();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function deleteBatch(id) {
  if (!window.confirm("Delete this batch and every file in it?")) return;
  try {
    await api(`/api/batches/${id}`, { method: "DELETE" });
    state.openBatches.delete(id);
    if (state.batch && state.batch.id === id) {
      state.batch = null;
      state.files = [];
      state.job = null;
      hideResults();
      updatePanels();
    }
    refreshLibrary();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

/* ------------------------------------------------------------------ lookup */
async function runLookup(event) {
  event.preventDefault();
  const query = $("lookup-query").value.trim();
  if (!query) return;
  const results = $("lookup-results");
  results.innerHTML = "";
  results.appendChild(el("p", { class: "muted", text: "Searching…" }));
  try {
    const payload = await api(`/api/search?q=${encodeURIComponent(query)}`);
    results.innerHTML = "";
    if (!payload.results || !payload.results.length) {
      results.appendChild(el("p", { class: "muted", text: payload.error || "No results." }));
      return;
    }
    for (const item of payload.results) {
      results.appendChild(
        el("article", {}, [
          el("h4", {}, [el("a", { href: item.url, target: "_blank", rel: "noopener", text: item.title || item.url })]),
          el("p", { class: "muted", text: item.summary || "" }),
        ])
      );
    }
  } catch (error) {
    results.innerHTML = "";
    results.appendChild(el("p", { class: "muted", text: error.message }));
  }
}
