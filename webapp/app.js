// Book Shelf mini app frontend. Vanilla JS, no build step -- this is a
// small enough app that a framework would add more ceremony than value,
// and it keeps the whole thing servable as static files by webapp_api.py.

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
}
// Outside Telegram (e.g. opening this URL directly in a normal browser for
// a quick look) initData will be empty and every API call will get a clean
// 401 from webapp_auth -- that's correct: this app only works signed-in
// through Telegram.
//
// On some Telegram clients (observed on Telegram Desktop and a few older
// mobile WebView builds), `window.Telegram.WebApp` exists the instant this
// script runs, but `tg.initData` itself is populated by the native app a
// beat later rather than being ready synchronously. Reading it exactly
// once at parse time can race that and capture "" permanently even for a
// perfectly legitimate launch through the 📚 Book Shelf button -- which
// surfaces as a confusing "Missing Telegram sign-in data" error on the
// very first load. `let` (not `const`) plus a short retry below covers
// that without changing behavior for clients that populate it instantly.
let INIT_DATA = tg ? tg.initData : "";

// Polls tg.initData for up to ~2s before giving up. Resolves immediately,
// with no delay, if it was already present on the first read.
function waitForInitData() {
  return new Promise((resolve) => {
    if (!tg || tg.initData) return resolve(tg ? tg.initData : "");
    let tries = 0;
    const timer = setInterval(() => {
      tries++;
      if (tg.initData || tries >= 20) {
        clearInterval(timer);
        resolve(tg.initData || "");
      }
    }, 100);
  });
}

const MAX_QUIZ_QUESTIONS = 30;

// ---------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------

async function api(path, opts = {}) {
  const headers = Object.assign({ "X-Telegram-Init-Data": INIT_DATA }, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const message = (data && data.detail) || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return data;
}

function alertMsg(msg) {
  if (tg && tg.showAlert) tg.showAlert(msg);
  else window.alert(msg);
}

// ---------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------

const views = ["view-shelf", "view-bookmarks", "view-upload", "view-book", "view-reader"];
let viewStack = ["view-shelf"];

function showView(id, { pushHistory = true } = {}) {
  views.forEach((v) => (document.getElementById(v).hidden = v !== id));
  if (pushHistory) {
    if (id === "view-shelf") viewStack = ["view-shelf"];
    else viewStack.push(id);
  }
  updateBackButton();
}

function goBack() {
  if (viewStack.length > 1) {
    viewStack.pop();
    const prev = viewStack[viewStack.length - 1];
    showView(prev, { pushHistory: false });
    if (prev === "view-shelf") loadShelf();
  }
}

function updateBackButton() {
  if (!tg || !tg.BackButton) return;
  if (viewStack.length > 1) {
    tg.BackButton.show();
    tg.BackButton.onClick(goBack);
  } else {
    tg.BackButton.hide();
  }
}

// ---------------------------------------------------------------------
// Shelf
// ---------------------------------------------------------------------

const SHELF_COLS = 4;

async function loadShelf() {
  const grid = document.getElementById("shelf-grid");
  const empty = document.getElementById("shelf-empty");
  grid.innerHTML = "";
  try {
    const { books } = await api("/api/books");
    empty.hidden = books.length > 0;
    renderShelf(grid, books);
  } catch (e) {
    // TEMPORARY diagnostics appended to the error itself -- this specific
    // failure (401 "missing sign-in data") has resisted two rounds of
    // guessing based on symptoms alone, so instead of shipping a third
    // blind fix, surface exactly what the Telegram JS bridge looked like
    // on the device that hit it. Safe to remove once this is root-caused.
    const diag = tg
      ? `tg=yes platform=${tg.platform} ver=${tg.version} initDataLen=${INIT_DATA.length} unsafeUserPresent=${!!(tg.initDataUnsafe && tg.initDataUnsafe.user)}`
      : "tg=no (window.Telegram.WebApp was never defined -- telegram-web-app.js did not load or this wasn't opened as a Web App)";
    alertMsg("Couldn't load your shelf: " + e.message + "\n\n[debug] " + diag);
  }
}

// Builds the shelf as explicit rows of SHELF_COLS books, with a wood
// "shelf-ledge" div after each row -- see style.css's .shelf-row/.shelf-ledge
// comment for why this is done in JS rather than a CSS background trick.
function renderShelf(grid, books) {
  for (let i = 0; i < books.length; i += SHELF_COLS) {
    const row = document.createElement("div");
    row.className = "shelf-row";
    books.slice(i, i + SHELF_COLS).forEach((book) => row.appendChild(bookTile(book)));
    grid.appendChild(row);
    const ledge = document.createElement("div");
    ledge.className = "shelf-ledge";
    grid.appendChild(ledge);
  }
}

function bookTile(book) {
  const wrap = document.createElement("div");
  wrap.className = "book-tile-wrap";

  const btn = document.createElement("button");
  btn.className = "book-tile";
  const dot = book.qa_indexed ? '<span class="status-dot" title="Indexed for AI Q&A"></span>' : "";
  const coverStyle = book.cover_color ? ` style="background:${book.cover_color}"` : "";
  btn.innerHTML = `
    <div class="book-cover"${coverStyle}>📘${dot}</div>
    <div class="book-title-label">${escapeHtml(book.title)}</div>
  `;
  btn.addEventListener("click", () => openBook(book.book_id));

  const menuBtn = document.createElement("button");
  menuBtn.className = "book-menu-btn";
  menuBtn.textContent = "⋮";
  menuBtn.title = "Book options";
  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openBookMenu(book);
  });

  wrap.appendChild(btn);
  wrap.appendChild(menuBtn);
  return wrap;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// ---------------------------------------------------------------------
// Book ⋮ menu (rename / cover color / delete) -- a generic bottom action
// sheet, reused for all three so there's one popup pattern in the app
// rather than three different ones.
// ---------------------------------------------------------------------

const COVER_COLORS = ["#8a5a3c", "#c0392b", "#2678b6", "#2e8b57", "#8e44ad", "#e67e22", "#34495e", "#d4af37"];

function showActionSheet(html) {
  document.getElementById("action-sheet").innerHTML = html;
  document.getElementById("action-sheet-backdrop").hidden = false;
}

function hideActionSheet() {
  document.getElementById("action-sheet-backdrop").hidden = true;
  document.getElementById("action-sheet").innerHTML = "";
}

document.getElementById("action-sheet-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "action-sheet-backdrop") hideActionSheet();
});

function openBookMenu(book) {
  showActionSheet(`
    <div class="sheet-title">${escapeHtml(book.title)}</div>
    <button id="sheet-edit-name">✏️ Edit name</button>
    <button id="sheet-change-cover">🎨 Change cover color</button>
    <button id="sheet-delete" class="danger">🗑 Delete book</button>
  `);
  document.getElementById("sheet-edit-name").addEventListener("click", () => promptRenameFromShelf(book));
  document.getElementById("sheet-change-cover").addEventListener("click", () => openCoverPicker(book));
  document.getElementById("sheet-delete").addEventListener("click", () => confirmDeleteBook(book));
}

function promptRenameFromShelf(book) {
  showActionSheet(`
    <div class="sheet-title">Rename book</div>
    <div class="name-editor" style="padding: 0 20px 18px;">
      <input id="sheet-rename-input" type="text" maxlength="200" value="${escapeHtml(book.title)}" />
      <button id="sheet-rename-save" class="btn">Save</button>
    </div>
  `);
  const input = document.getElementById("sheet-rename-input");
  input.focus();
  input.select();
  const save = async () => {
    const newTitle = input.value.trim();
    if (!newTitle) {
      alertMsg("Title can't be empty.");
      return;
    }
    try {
      await api(`/api/books/${book.book_id}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: newTitle }),
      });
    } catch (e) {
      alertMsg("Couldn't rename this book: " + e.message);
      return;
    }
    hideActionSheet();
    // Keep the open book detail view (if this is the currently-open book)
    // in sync too, since renaming now happens from the shelf, not from
    // inside the book itself.
    if (currentBook && currentBook.book_id === book.book_id) {
      currentBook.title = newTitle;
      renderBookHeader();
    }
    loadShelf();
  };
  document.getElementById("sheet-rename-save").addEventListener("click", save);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") save();
  });
}

function openCoverPicker(book) {
  const swatches = COVER_COLORS
    .map(
      (c) =>
        `<button class="color-swatch ${book.cover_color === c ? "selected" : ""}" data-color="${c}" style="background:${c}" title="${c}"></button>`
    )
    .join("");
  showActionSheet(`
    <div class="sheet-title">Cover color</div>
    <div class="color-swatch-row">${swatches}</div>
  `);
  document.querySelectorAll(".color-swatch").forEach((sw) => {
    sw.addEventListener("click", async () => {
      try {
        await api(`/api/books/${book.book_id}/cover`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ color: sw.dataset.color }),
        });
      } catch (e) {
        alertMsg("Couldn't change the cover color: " + e.message);
        return;
      }
      hideActionSheet();
      loadShelf();
    });
  });
}

function confirmDeleteBook(book) {
  const doDelete = async () => {
    try {
      await api(`/api/books/${book.book_id}`, { method: "DELETE" });
    } catch (e) {
      alertMsg("Couldn't delete this book: " + e.message);
      return;
    }
    if (currentBook && currentBook.book_id === book.book_id) {
      showView("view-shelf", { pushHistory: false });
      viewStack = ["view-shelf"];
    }
    loadShelf();
  };
  const message = `Delete "${book.title}"? This can't be undone.`;
  hideActionSheet();
  if (tg && tg.showConfirm) {
    tg.showConfirm(message, (ok) => {
      if (ok) doDelete();
    });
  } else if (window.confirm(message)) {
    doDelete();
  }
}

// ---------------------------------------------------------------------
// Bookmarks (main shelf page): one row per book that currently has a
// bookmark set, tapping jumps straight into that book's reader at that
// exact page.
// ---------------------------------------------------------------------

document.getElementById("btn-bookmarks").addEventListener("click", () => {
  showView("view-bookmarks");
  loadBookmarks();
});

async function loadBookmarks() {
  const list = document.getElementById("bookmarks-list");
  const empty = document.getElementById("bookmarks-empty");
  list.innerHTML = "";
  let books;
  try {
    ({ books } = await api("/api/books"));
  } catch (e) {
    alertMsg("Couldn't load bookmarks: " + e.message);
    return;
  }
  const bookmarked = books.filter((b) => b.bookmark_page);
  empty.hidden = bookmarked.length > 0;
  bookmarked.forEach((book) => {
    const row = document.createElement("button");
    row.className = "bookmark-row";
    const coverStyle = book.cover_color ? ` style="background:${book.cover_color}"` : "";
    row.innerHTML = `
      <span class="bookmark-cover"${coverStyle}>📘</span>
      <span class="bookmark-info">
        <span class="bookmark-title">${escapeHtml(book.title)}</span>
        <span class="muted">Page ${book.bookmark_page}</span>
      </span>
      <span class="bookmark-chevron">›</span>
    `;
    row.addEventListener("click", () => openBookAtPage(book.book_id, book.bookmark_page));
    list.appendChild(row);
  });
}

async function openBookAtPage(bookId, page) {
  await openBook(bookId);
  await openReader(page);
}

document.getElementById("btn-add-book").addEventListener("click", () => {
  document.getElementById("file-input").click();
});

document.getElementById("file-input").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) uploadFile(file);
});

function uploadFile(file) {
  const MAX_BYTES = 200 * 1024 * 1024;
  if (file.size > MAX_BYTES) {
    alertMsg(`That file is over the 200MB limit (${(file.size / 1024 / 1024).toFixed(1)}MB).`);
    return;
  }
  showView("view-upload");
  const bar = document.getElementById("upload-bar");
  const status = document.getElementById("upload-status");
  bar.style.width = "0%";
  status.textContent = "Starting upload…";

  const form = new FormData();
  form.append("file", file, file.name);
  form.append("title", file.name.replace(/\.pdf$/i, ""));

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.setRequestHeader("X-Telegram-Init-Data", INIT_DATA);

  xhr.upload.addEventListener("progress", (evt) => {
    if (evt.lengthComputable) {
      const pct = Math.round((evt.loaded / evt.total) * 100);
      bar.style.width = pct + "%";
      status.textContent = pct < 100 ? `Uploading… ${pct}%` : "Processing…";
    }
  });

  xhr.onload = () => {
    let data = null;
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
    if (xhr.status >= 200 && xhr.status < 300 && data) {
      showView("view-shelf", { pushHistory: false });
      viewStack = ["view-shelf"];
      loadShelf().then(() => openBook(data.book_id));
    } else {
      const msg = (data && data.detail) || `Upload failed (${xhr.status})`;
      alertMsg(msg);
      showView("view-shelf", { pushHistory: false });
      viewStack = ["view-shelf"];
    }
  };
  xhr.onerror = () => {
    alertMsg("Upload failed -- check your connection and try again.");
    showView("view-shelf", { pushHistory: false });
    viewStack = ["view-shelf"];
  };
  xhr.send(form);
}

// ---------------------------------------------------------------------
// Book detail
// ---------------------------------------------------------------------

let currentBook = null;

async function openBook(bookId) {
  try {
    currentBook = await api(`/api/books/${bookId}`);
  } catch (e) {
    alertMsg("Couldn't open that book: " + e.message);
    return;
  }
  renderBookHeader();
  document.getElementById("book-panel").hidden = true;
  document.getElementById("book-panel").innerHTML = "";
  showView("view-book");
}

function renderBookHeader() {
  document.getElementById("book-title").textContent = currentBook.title;
  const chStatus = currentBook.chapters_status;
  document.getElementById("book-meta").textContent =
    `${currentBook.page_count} pages` + (currentBook.qa_indexed ? " · AI Q&A ready" : "");

  const badge = document.getElementById("chapters-badge");
  badge.className = "badge";
  if (chStatus === "done") { badge.textContent = "✓ done"; badge.classList.add("ok"); }
  else if (chStatus === "pending") { badge.textContent = "working…"; }
  else if (chStatus === "error") { badge.textContent = "failed"; badge.classList.add("err"); }
  else { badge.textContent = ""; }
}

function panel(html) {
  const p = document.getElementById("book-panel");
  p.hidden = false;
  p.innerHTML = html;
  return p;
}

document.querySelectorAll(".action-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const action = btn.dataset.action;
    if (action === "chapters") handleChapters();
    else if (action === "read") openReader();
    else if (action === "summarize") handleSummarizeMenu();
    else if (action === "ask") handleAskMenu();
    else if (action === "quiz") handleQuizMenu();
  });
});

// ---------------------------------------------------------------------
// Generic job polling
// ---------------------------------------------------------------------

function pollJob(bookId, jobType, { onProgress, onDone, onError }) {
  const tick = async () => {
    let job;
    try {
      job = await api(`/api/books/${bookId}/jobs/${jobType}`);
    } catch (e) {
      onError(e.message);
      return;
    }
    if (job.status === "running") {
      if (onProgress) onProgress(job.progress);
      setTimeout(tick, 1500);
    } else if (job.status === "done") {
      onDone(job.result);
    } else if (job.status === "error") {
      onError(job.error || "Something went wrong.");
    } else {
      onError("No job in progress.");
    }
  };
  tick();
}

// ---------------------------------------------------------------------
// 1. Divide into chapters
// ---------------------------------------------------------------------

async function handleChapters() {
  if (currentBook.chapters_status === "done") {
    renderChapterList();
    return;
  }
  panel(`<p class="spinner-line">⏳ <span id="chapters-progress">Starting…</span></p>`);
  try {
    await api(`/api/books/${currentBook.book_id}/chapters`, { method: "POST" });
  } catch (e) {
    panel(`<p class="error-text">${escapeHtml(e.message)}</p>`);
    return;
  }
  pollJob(currentBook.book_id, "chapters", {
    onProgress: (p) => {
      const el = document.getElementById("chapters-progress");
      if (el) el.textContent = p || "Working…";
    },
    onDone: async () => {
      currentBook = await api(`/api/books/${currentBook.book_id}`);
      renderBookHeader();
      renderChapterList();
    },
    onError: (msg) => {
      panel(`<p class="error-text">Couldn't divide this book: ${escapeHtml(msg)}</p>
             <button class="btn" id="retry-chapters">Try again</button>`);
      document.getElementById("retry-chapters").addEventListener("click", handleChapters);
    },
  });
}

function renderChapterList() {
  const chapters = currentBook.chapters || [];
  const items = chapters
    .map((c) => `<li>${escapeHtml(c.title)} <span class="muted">(p.${c.start_page}-${c.end_page})</span></li>`)
    .join("");
  panel(`
    <ul class="chapter-list">${items}</ul>
    <button class="btn secondary" id="redo-chapters">Re-divide</button>
  `);
  document.getElementById("redo-chapters").addEventListener("click", () => {
    currentBook.chapters_status = "none";
    handleChapters();
  });
}

// ---------------------------------------------------------------------
// 2. Reader (pdf.js)
// ---------------------------------------------------------------------

// zoomFactor is a multiplier applied on top of the auto-fit-to-width scale
// (1 = fit width, 1.4 = 40% zoomed in past fit-width, etc.) so zoom keeps
// working sensibly across pages/devices with different fit-width scales.
let readerState = { pdf: null, pageNum: 1, rendering: false, zoomFactor: 1 };
const ZOOM_STEP = 0.2;
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;

async function openReader(targetPage) {
  showView("view-reader");
  document.getElementById("reader-page-label").textContent = "Loading…";
  document.getElementById("reader-goto-row").hidden = true;
  try {
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
    const loadingTask = pdfjsLib.getDocument({
      url: `/api/books/${currentBook.book_id}/file`,
      httpHeaders: { "X-Telegram-Init-Data": INIT_DATA },
    });
    readerState.pdf = await loadingTask.promise;
    // targetPage (from a bookmark tap) wins; otherwise resume from the
    // book's own bookmark if one is set, otherwise page 1.
    readerState.pageNum = targetPage || currentBook.bookmark_page || 1;
    readerState.zoomFactor = 1;
    updateZoomLabel();
    renderReaderPage();
  } catch (e) {
    alertMsg("Couldn't open the reader: " + e.message);
    goBack();
  }
}

async function renderReaderPage() {
  if (!readerState.pdf || readerState.rendering) return;
  readerState.rendering = true;
  const page = await readerState.pdf.getPage(readerState.pageNum);
  const canvas = document.getElementById("reader-canvas");
  const ctx = canvas.getContext("2d");
  const containerWidth = document.getElementById("reader-canvas-wrap").clientWidth - 16;
  const baseViewport = page.getViewport({ scale: 1 });
  const fitScale = Math.max(0.5, containerWidth / baseViewport.width);
  const viewport = page.getViewport({ scale: fitScale * readerState.zoomFactor });
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  await page.render({ canvasContext: ctx, viewport }).promise;

  // Text layer: an invisible but selectable text overlay positioned over
  // the canvas, matching every glyph pdf.js just drew as pixels -- this is
  // what makes "select and copy text from the book" actually work, since
  // a <canvas> render on its own is just an image with no selectable text.
  const textLayerDiv = document.getElementById("reader-text-layer");
  textLayerDiv.innerHTML = "";
  textLayerDiv.style.width = `${viewport.width}px`;
  textLayerDiv.style.height = `${viewport.height}px`;
  if (typeof pdfjsLib.renderTextLayer === "function") {
    try {
      const textContent = await page.getTextContent();
      await pdfjsLib.renderTextLayer({ textContentSource: textContent, container: textLayerDiv, viewport }).promise;
    } catch (_) {
      // Non-fatal -- the page still displays fine, it just won't be
      // selectable for this one page (e.g. a scanned/image-only page with
      // no extractable text at all).
    }
  }

  document.getElementById("reader-page-label").textContent =
    `Page ${readerState.pageNum} / ${readerState.pdf.numPages}`;
  updateBookmarkButton();
  readerState.rendering = false;
}

function updateZoomLabel() {
  document.getElementById("reader-zoom-label").textContent = Math.round(readerState.zoomFactor * 100) + "%";
}

function updateBookmarkButton() {
  const btn = document.getElementById("reader-bookmark");
  const isBookmarked = currentBook.bookmark_page === readerState.pageNum;
  btn.textContent = isBookmarked ? "🔖 Bookmarked (tap to remove)" : "🔖 Bookmark this page";
}

document.getElementById("reader-prev").addEventListener("click", () => {
  if (readerState.pageNum > 1) { readerState.pageNum -= 1; renderReaderPage(); }
});
document.getElementById("reader-next").addEventListener("click", () => {
  if (readerState.pdf && readerState.pageNum < readerState.pdf.numPages) {
    readerState.pageNum += 1;
    renderReaderPage();
  }
});

document.getElementById("reader-zoom-in").addEventListener("click", () => {
  readerState.zoomFactor = Math.min(ZOOM_MAX, +(readerState.zoomFactor + ZOOM_STEP).toFixed(2));
  updateZoomLabel();
  renderReaderPage();
});
document.getElementById("reader-zoom-out").addEventListener("click", () => {
  readerState.zoomFactor = Math.max(ZOOM_MIN, +(readerState.zoomFactor - ZOOM_STEP).toFixed(2));
  updateZoomLabel();
  renderReaderPage();
});

// ---------------------------------------------------------------------
// Pinch to zoom (two-finger touch)
// ---------------------------------------------------------------------
//
// Re-rendering the actual PDF page (page.render()) on every touchmove tick
// would be far too slow to track a finger smoothly, so during the pinch we
// just apply a cheap CSS transform: scale() to the already-rendered canvas
// for instant visual feedback, and only do the real, crisp pdf.js
// re-render once (on touchend) at the final zoom level.
let pinchState = null;

function touchDistance(touches) {
  const dx = touches[0].clientX - touches[1].clientX;
  const dy = touches[0].clientY - touches[1].clientY;
  return Math.hypot(dx, dy);
}

const readerCanvasWrap = document.getElementById("reader-canvas-wrap");

readerCanvasWrap.addEventListener(
  "touchstart",
  (e) => {
    if (e.touches.length === 2) {
      pinchState = { startDist: touchDistance(e.touches), startZoom: readerState.zoomFactor, liveZoom: null };
    }
  },
  { passive: true }
);

readerCanvasWrap.addEventListener(
  "touchmove",
  (e) => {
    if (!pinchState || e.touches.length !== 2) return;
    e.preventDefault();
    const dist = touchDistance(e.touches);
    const liveZoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, pinchState.startZoom * (dist / pinchState.startDist)));
    pinchState.liveZoom = liveZoom;
    // Scale the whole page container (canvas + text layer together) so the
    // selectable text overlay stays visually aligned with the image during
    // the live gesture, not just the canvas underneath it.
    document.getElementById("reader-page-container").style.transform = `scale(${liveZoom / readerState.zoomFactor})`;
  },
  { passive: false }
);

function endPinch() {
  if (!pinchState) return;
  document.getElementById("reader-page-container").style.transform = "";
  if (pinchState.liveZoom != null && pinchState.liveZoom !== readerState.zoomFactor) {
    readerState.zoomFactor = +pinchState.liveZoom.toFixed(2);
    updateZoomLabel();
    renderReaderPage();
  }
  pinchState = null;
}
readerCanvasWrap.addEventListener("touchend", endPinch);
readerCanvasWrap.addEventListener("touchcancel", endPinch);

document.getElementById("reader-goto").addEventListener("click", () => {
  if (!readerState.pdf) return;
  const row = document.getElementById("reader-goto-row");
  const input = document.getElementById("reader-goto-input");
  input.max = readerState.pdf.numPages;
  input.placeholder = `Page 1-${readerState.pdf.numPages}`;
  row.hidden = false;
  input.focus();
});
document.getElementById("reader-goto-cancel").addEventListener("click", () => {
  document.getElementById("reader-goto-row").hidden = true;
});
function jumpToPage() {
  const input = document.getElementById("reader-goto-input");
  const n = parseInt(input.value, 10);
  if (readerState.pdf && n >= 1 && n <= readerState.pdf.numPages) {
    readerState.pageNum = n;
    document.getElementById("reader-goto-row").hidden = true;
    input.value = "";
    renderReaderPage();
  } else {
    alertMsg(`Enter a page between 1 and ${readerState.pdf ? readerState.pdf.numPages : "?"}.`);
  }
}
document.getElementById("reader-goto-go").addEventListener("click", jumpToPage);
document.getElementById("reader-goto-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") jumpToPage();
});

document.getElementById("reader-bookmark").addEventListener("click", async () => {
  const alreadyBookmarked = currentBook.bookmark_page === readerState.pageNum;
  const newPage = alreadyBookmarked ? null : readerState.pageNum;
  try {
    currentBook = await api(`/api/books/${currentBook.book_id}/bookmark`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page: newPage }),
    });
  } catch (e) {
    alertMsg("Couldn't update the bookmark: " + e.message);
    return;
  }
  updateBookmarkButton();
});

// ---------------------------------------------------------------------
// 3. Summarize
// ---------------------------------------------------------------------

function handleSummarizeMenu() {
  const chapters = currentBook.chapters || [];
  if (!chapters.length) {
    panel(`<p>Divide this book into chapters first, then come back to summarize it.</p>`);
    return;
  }
  const options = chapters
    .map((c, i) => `<option value="${i}">${escapeHtml(c.title)}</option>`)
    .join("");
  panel(`
    <button class="btn" id="summarize-book">Summarize whole book</button>
    <div style="margin-top:12px;">
      <select id="summarize-chapter-select">${options}</select>
      <button class="btn secondary" id="summarize-chapter">Summarize this chapter</button>
    </div>
    <div id="summarize-result"></div>
  `);
  document.getElementById("summarize-book").addEventListener("click", () => runSummarize({ scope: "book" }));
  document.getElementById("summarize-chapter").addEventListener("click", () => {
    const idx = parseInt(document.getElementById("summarize-chapter-select").value, 10);
    runSummarize({ scope: "chapter", chapter_index: idx });
  });
}

async function runSummarize(body) {
  const jobType = body.scope === "book" ? "summary:book" : `summary:chapter:${body.chapter_index}`;
  const result = document.getElementById("summarize-result");
  result.innerHTML = `<p class="spinner-line">⏳ <span id="summarize-progress">Starting…</span></p>`;
  try {
    await api(`/api/books/${currentBook.book_id}/summarize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    result.innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
    return;
  }
  pollJob(currentBook.book_id, jobType, {
    onProgress: (p) => {
      const el = document.getElementById("summarize-progress");
      if (el) el.textContent = p || "Working…";
    },
    onDone: (text) => {
      result.innerHTML = `<div style="white-space:pre-wrap; margin-top:10px;">${escapeHtml(text)}</div>`;
    },
    onError: (msg) => {
      result.innerHTML = `<p class="error-text">${escapeHtml(msg)}</p>`;
    },
  });
}

// ---------------------------------------------------------------------
// 4. Ask AI
// ---------------------------------------------------------------------

let qaHistory = [];
let qaMode = "single"; // "single" | "conversational"

function handleAskMenu() {
  qaHistory = [];
  qaMode = "single";
  if (!currentBook.qa_indexed) {
    panel(`
      <p>This book isn't indexed for AI Q&A yet.</p>
      <button class="btn" id="start-index">Index this book</button>
      <p id="index-progress" class="muted"></p>
    `);
    document.getElementById("start-index").addEventListener("click", startIndexing);
    return;
  }
  renderAskUI();
}

async function startIndexing() {
  const progressEl = document.getElementById("index-progress");
  document.getElementById("start-index").disabled = true;
  try {
    await api(`/api/books/${currentBook.book_id}/index`, { method: "POST" });
  } catch (e) {
    progressEl.textContent = e.message;
    progressEl.classList.add("error-text");
    return;
  }
  pollJob(currentBook.book_id, "index", {
    onProgress: (p) => { progressEl.textContent = p || "Working…"; },
    onDone: async () => {
      currentBook = await api(`/api/books/${currentBook.book_id}`);
      renderBookHeader();
      renderAskUI();
    },
    onError: (msg) => {
      progressEl.textContent = msg;
      document.getElementById("start-index").disabled = false;
    },
  });
}

function renderAskUI() {
  panel(`
    <div class="qa-mode-row">
      <button class="qa-mode-btn ${qaMode === "single" ? "selected" : ""}" data-mode="single">💬 Single answers</button>
      <button class="qa-mode-btn ${qaMode === "conversational" ? "selected" : ""}" data-mode="conversational">🔗 Conversational</button>
    </div>
    <p class="qa-mode-hint">${
      qaMode === "conversational"
        ? "Each answer can build on earlier ones in this chat."
        : "Every question is answered on its own, with no memory of earlier ones."
    }</p>
    <div id="qa-log" class="qa-log"></div>
    <div class="qa-input-row">
      <input id="qa-input" type="text" placeholder="Ask a question about this book…" />
      <button class="btn" id="qa-send">Ask</button>
    </div>
  `);
  renderQaLog();
  document.getElementById("qa-send").addEventListener("click", sendQuestion);
  document.getElementById("qa-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendQuestion();
  });
  document.querySelectorAll(".qa-mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      qaMode = btn.dataset.mode;
      // Switching modes mid-conversation would mix "continuous" answers
      // with a fresh no-memory mode confusingly, so start the thread over.
      qaHistory = [];
      renderAskUI();
    });
  });
}

// Turns literal "[1]", "[2]" markers Claude wrote inline into small
// superscript citation numbers, and appends a numbered source list at the
// bottom of the bubble -- exactly the footnote-style citation the mini
// app's Ask AI panel is meant to show.
function renderAnswerHtml(text, sources) {
  const escaped = escapeHtml(text);
  const withMarkers = escaped.replace(/\[(\d+)\]/g, (m, n) => `<span class="cite-marker" data-n="${n}">[${n}]</span>`);
  if (!sources || !sources.length) return withMarkers;
  const lines = sources
    .map((s) => `<span class="src-line"><span class="n">[${s.n}]</span><span>p.${s.page} — ${escapeHtml(s.text)}</span></span>`)
    .join("");
  return `${withMarkers}<span class="qa-sources">${lines}</span>`;
}

function renderQaLog() {
  const log = document.getElementById("qa-log");
  if (!log) return;
  log.innerHTML = qaHistory
    .map((m, i) =>
      m.role === "q"
        ? `<div class="qa-msg q">${escapeHtml(m.text)}</div>`
        : `<div class="qa-msg a" data-msg-index="${i}">${renderAnswerHtml(m.text, m.sources)}</div>`
    )
    .join("");
  log.scrollTop = log.scrollHeight;
}

// Tapping a small citation number re-shows that excerpt. Delegated on the
// static #view-book container (rather than #qa-log, which is rebuilt from
// scratch by panel()/renderAskUI() and wouldn't keep a directly-attached
// listener) since bubbles are re-rendered wholesale on every turn.
document.getElementById("view-book").addEventListener("click", (e) => {
  const marker = e.target.closest(".cite-marker");
  if (!marker) return;
  const bubble = marker.closest(".qa-msg");
  const msg = qaHistory[parseInt(bubble.dataset.msgIndex, 10)];
  const source = msg && msg.sources && msg.sources.find((s) => String(s.n) === marker.dataset.n);
  if (source) alertMsg(`[${source.n}] Page ${source.page}:\n\n${source.text}`);
});

async function sendQuestion() {
  const input = document.getElementById("qa-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";

  qaHistory.push({ role: "q", text: question });
  qaHistory.push({ role: "a", text: "…thinking…" });
  renderQaLog();

  const priorTurns = [];
  if (qaMode === "conversational") {
    for (let i = 0; i < qaHistory.length - 2; i += 2) {
      priorTurns.push({ question: qaHistory[i].text, answer: qaHistory[i + 1].text });
    }
  }

  try {
    const result = await api(`/api/books/${currentBook.book_id}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: priorTurns }),
    });
    qaHistory[qaHistory.length - 1] = { role: "a", text: result.answer, sources: result.sources || [] };
  } catch (e) {
    qaHistory[qaHistory.length - 1] = { role: "a", text: "Error: " + e.message };
  }
  renderQaLog();
}

// ---------------------------------------------------------------------
// 5. Quiz
// ---------------------------------------------------------------------

let quizDifficulty = "medium";

function handleQuizMenu() {
  const chapters = currentBook.chapters || [];
  if (!chapters.length) {
    panel(`<p>Divide this book into chapters first, then come back to create a quiz.</p>`);
    return;
  }
  quizDifficulty = "medium";
  const items = chapters
    .map(
      (c, i) => `
      <li>
        <label><input type="checkbox" class="quiz-chapter-cb" value="${i}" checked /> ${escapeHtml(c.title)}</label>
      </li>`
    )
    .join("");
  panel(`
    <p><strong>Select chapters:</strong></p>
    <ul class="chapter-list">${items}</ul>
    <p><strong>Difficulty:</strong></p>
    <div class="diff-row">
      <button class="diff-btn" data-diff="easy">Easy</button>
      <button class="diff-btn selected" data-diff="medium">Medium</button>
      <button class="diff-btn" data-diff="difficult">Difficult</button>
    </div>
    <p><strong>Number of questions (max ${MAX_QUIZ_QUESTIONS}):</strong></p>
    <input id="quiz-count" type="number" min="1" max="${MAX_QUIZ_QUESTIONS}" value="10" style="width:80px;padding:8px;" />
    <div style="margin-top:12px;">
      <button class="btn" id="quiz-generate">Generate quiz</button>
    </div>
    <div id="quiz-result"></div>
  `);

  document.querySelectorAll(".diff-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      quizDifficulty = btn.dataset.diff;
      document.querySelectorAll(".diff-btn").forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
    });
  });

  document.getElementById("quiz-generate").addEventListener("click", runQuizGeneration);
}

async function runQuizGeneration() {
  const chapterIndices = Array.from(document.querySelectorAll(".quiz-chapter-cb:checked")).map((cb) =>
    parseInt(cb.value, 10)
  );
  let numQuestions = parseInt(document.getElementById("quiz-count").value, 10) || 10;
  numQuestions = Math.max(1, Math.min(MAX_QUIZ_QUESTIONS, numQuestions));

  if (!chapterIndices.length) {
    alertMsg("Select at least one chapter.");
    return;
  }

  const result = document.getElementById("quiz-result");
  result.innerHTML = `<p class="spinner-line">⏳ Generating ${numQuestions} questions…</p>`;
  try {
    await api(`/api/books/${currentBook.book_id}/quiz`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chapter_indices: chapterIndices,
        difficulty: quizDifficulty,
        num_questions: numQuestions,
      }),
    });
  } catch (e) {
    result.innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
    return;
  }
  pollJob(currentBook.book_id, "quiz", {
    onDone: (questions) => renderQuiz(questions),
    onError: (msg) => { result.innerHTML = `<p class="error-text">${escapeHtml(msg)}</p>`; },
  });
}

function renderQuiz(questions) {
  const result = document.getElementById("quiz-result");
  const score = { correct: 0, answered: 0 };
  result.innerHTML = `<p class="quiz-score" id="quiz-score">Score: 0 / ${questions.length}</p>`;

  questions.forEach((q, qi) => {
    const block = document.createElement("div");
    block.className = "quiz-q";
    block.innerHTML = `<div class="q-text">${qi + 1}. ${escapeHtml(q.question)}</div>`;
    q.options.forEach((opt, oi) => {
      const optBtn = document.createElement("button");
      optBtn.className = "quiz-opt";
      optBtn.textContent = opt;
      optBtn.addEventListener("click", () => {
        if (block.dataset.answered) return;
        block.dataset.answered = "1";
        score.answered += 1;
        const buttons = block.querySelectorAll(".quiz-opt");
        buttons[q.correct_index].classList.add("correct");
        if (oi !== q.correct_index) optBtn.classList.add("wrong");
        else score.correct += 1;
        if (q.explanation) {
          const exp = document.createElement("div");
          exp.className = "quiz-explain";
          exp.textContent = q.explanation;
          block.appendChild(exp);
        }
        document.getElementById("quiz-score").textContent = `Score: ${score.correct} / ${questions.length}`;
      });
      block.appendChild(optBtn);
    });
    result.appendChild(block);
  });
}

// ---------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------

(async () => {
  if (!INIT_DATA) {
    INIT_DATA = await waitForInitData();
  }
  loadShelf();
})();
