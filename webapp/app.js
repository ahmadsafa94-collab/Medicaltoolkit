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
const MAX_FLASHCARD_CARDS = 50;

// ---------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------

let sessionExpiredShown = false;

// Icons: Lucide line icons (ISC licence) in the sprite at the top of index.html,
// drawn in the text colour so they follow the theme.
function ic(name, extra = "") {
  return `<svg class="ic${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "X-Telegram-Init-Data": INIT_DATA }, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    // `detail` is usually a string, but the drug endpoints answer a name
    // miss with {message, suggestions} so the client can offer the
    // alternatives as buttons. Both shapes are handled here so no caller
    // has to, and the raw detail is attached for the ones that want it.
    const detail = data && data.detail;
    const message =
      (typeof detail === "string" && detail) ||
      (detail && detail.message) ||
      `Request failed (${res.status})`;
    const err = new Error(message);
    err.status = res.status;
    err.detail = detail;
    if (res.status === 401 && INIT_DATA && !sessionExpiredShown) {
      sessionExpiredShown = true;
      err.message = "Your session has expired. Close the app and open it again from the bot.";
    }
    throw err;
  }
  return data;
}

function alertMsg(msg) {
  const m = String(msg || "Something went wrong.").slice(0, 250);
  try {
    if (tg && tg.showAlert) return tg.showAlert(m);
  } catch (_) { /* popup already open, or an old client */ }
  try { window.alert(m); } catch (_) {}
}

// tg.showConfirm, but it can't throw and never leaves the caller hanging.
function confirmMsg(msg) {
  const m = String(msg || "Are you sure?").slice(0, 250);
  return new Promise((resolve) => {
    try {
      if (tg && tg.showConfirm) return tg.showConfirm(m, (ok) => resolve(!!ok));
    } catch (_) {}
    try { resolve(window.confirm(m)); } catch (_) { resolve(false); }
  });
}

// ---------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------

const views = [
  "view-shelf", "view-ask", "view-drugs", "view-bookmarks", "view-request-book",
  "view-upload", "view-book", "view-reader", "view-web", "view-accounts",
];

// The tab bar's destinations. A root view resets the navigation stack
// rather than being pushed onto it, so the Telegram BackButton is hidden
// there -- there is nothing above a tab to go back to, and switching tabs
// is not a step you reverse.
const ROOT_VIEWS = ["view-shelf", "view-ask", "view-drugs"];
let viewStack = ["view-shelf"];

function showView(id, { pushHistory = true } = {}) {
  views.forEach((v) => (document.getElementById(v).hidden = v !== id));
  if (pushHistory) {
    if (ROOT_VIEWS.includes(id)) viewStack = [id];
    else if (viewStack[viewStack.length - 1] !== id) viewStack.push(id);
  }
  syncTabBar(id);
  updateBackButton();
}

// The bar is only up on the root views: anything you navigated INTO is
// depth that the BackButton owns, and a sideways jump out of the reader
// would both fight that and eat the bottom of the page being read.
function syncTabBar(activeId) {
  const bar = document.getElementById("tabbar");
  if (!bar) return;
  const isRoot = ROOT_VIEWS.includes(activeId);
  bar.hidden = !isRoot;
  document.body.classList.toggle("has-tabbar", isRoot);
  bar.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("is-active", t.dataset.tab === activeId);
  });
}

function goBack() {
  if (viewStack.length > 1) {
    if (viewStack[viewStack.length - 1] === "view-web") webClose();
    viewStack.pop();
    const prev = viewStack[viewStack.length - 1];
    showView(prev, { pushHistory: false });
    if (prev === "view-shelf") setShelfTab(shelfTab);
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

// Free-plan shelf cap, refreshed on every shelf load. Only so the "+" button
// can explain itself before a file is picked -- the server enforces the real
// limit in upload_book(). null = unlimited (Premium).
let shelfLimit = null;
let shelfCount = 0;
// The server's upload limit, sent with every shelf/ask-library load so this
// pre-check can't drift from what the server enforces.
let maxUploadMb = 500;

let shelfLoadSeq = 0;

// Book title search: case and accents ignored, every typed word must appear.
function bookQueryWords(q) {
  return (q || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().split(/\s+/).filter(Boolean);
}
function titleMatches(title, words) {
  if (!words.length) return true;
  const t = (title || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
  return words.every((w) => t.includes(w));
}

let shelfBooks = [];   // the user's own books, as last loaded (the search filters these)
let shelfQuery = "";

async function loadShelf() {
  const grid = document.getElementById("shelf-grid");
  const empty = document.getElementById("shelf-empty");
  const seq = ++shelfLoadSeq;
  try {
    const { books, shelf_limit, shelf_used, max_upload_mb } = await api("/api/books");
    if (seq !== shelfLoadSeq) return;  // a newer load is on its way
    grid.innerHTML = "";
    shelfLimit = shelf_limit ?? null;
    if (max_upload_mb) maxUploadMb = max_upload_mb;
    // From the server, not books.length: paid 📚 Requested Books sit on the
    // shelf but don't count toward the free cap.
    shelfCount = shelf_used ?? books.length;
    shelfBooks = books;
    renderMyShelf();
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
function shelfView() {
  try { return localStorage.getItem("shelfView") === "list" ? "list" : "grid"; } catch (_) { return "grid"; }
}

function renderShelf(grid, books, { shared = false } = {}) {
  grid.classList.toggle("is-list", shelfView() === "list");
  // A specialty book added to My books is still the library's (its cover
  // and file come from there).
  const isShared = (book) => shared || book.source === "shared";
  if (shelfView() === "list") {
    books.forEach((book) => grid.appendChild(bookTile(book, { shared: isShared(book), list: true })));
    loadCoverThumbs(grid);
    return;
  }
  for (let i = 0; i < books.length; i += SHELF_COLS) {
    const row = document.createElement("div");
    row.className = "shelf-row";
    books.slice(i, i + SHELF_COLS).forEach((book) => row.appendChild(bookTile(book, { shared: isShared(book) })));
    grid.appendChild(row);
    const ledge = document.createElement("div");
    ledge.className = "shelf-ledge";
    grid.appendChild(ledge);
  }
  loadCoverThumbs(grid);
}

// --- Shelf tabs: 📚 My books | 🏥 Specialty books ------------------------
// Specialty books open exactly like an uploaded book (reader, chapters,
// summaries, Ask AI, quizzes, flashcards, notes) -- the server keeps a
// per-user reference to the shared copy, see webapp_api._book_or_404.

let shelfTab = "mine";
let shelfSpecialty = null;
let shelfCatalog = null;

function setShelfTab(tab) {
  shelfTab = tab;
  document.querySelectorAll("#view-shelf .subtab").forEach((t) => t.classList.toggle("is-active", t.dataset.shelftab === tab));
  document.getElementById("shelf-pane-mine").hidden = tab !== "mine";
  document.getElementById("shelf-pane-spec").hidden = tab !== "spec";
  document.getElementById("btn-add-book").hidden = tab !== "mine";
  if (tab === "spec") loadSpecialtyShelf();
  else loadShelf();
}

let shelfInLibrary = new Set();  // specialty books this user added to My books

async function toggleInLibrary(book, on, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`/api/shared/books/${encodeURIComponent(book.book_id)}/library`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    });
  } catch (e) {
    if (btn) btn.disabled = false;
    return alertMsg(e.message);
  }
  if (on) shelfInLibrary.add(book.book_id);
  else shelfInLibrary.delete(book.book_id);
  shelfToast(on ? `“${book.title}” added to My books` : `Removed from My books (still in its specialty)`);
  if (shelfTab === "spec") renderSpecialtyShelf();
  else loadShelf();
}

let shelfToastTimer = null;
function shelfToast(text) {
  const t = document.getElementById("reader-toast");
  if (t.parentElement !== document.body) document.body.appendChild(t);  // shown outside the reader too
  t.textContent = text;
  t.hidden = false;
  clearTimeout(shelfToastTimer);
  shelfToastTimer = setTimeout(() => { t.hidden = true; }, 2400);
}

async function loadSpecialtyShelf() {
  try {
    const lib = await api("/api/ask/library");
    shelfCatalog = lib.specialties;
    shelfInLibrary = new Set(lib.in_library || []);
  } catch (e) {
    return alertMsg("Couldn't load the specialty books: " + e.message);
  }
  const select = document.getElementById("shelf-specialty");
  const names = shelfCatalog.map((x) => x.name);
  if (!names.includes(shelfSpecialty)) shelfSpecialty = askSpecialty && names.includes(askSpecialty) ? askSpecialty : names[0] || null;
  select.hidden = !names.length;
  select.innerHTML = shelfCatalog
    .map((x) => `<option value="${escapeHtml(x.name)}" ${x.name === shelfSpecialty ? "selected" : ""}>${escapeHtml(x.name)} (${x.books.length})</option>`)
    .join("");
  renderSpecialtyShelf();
}

function renderMyShelf() {
  const grid = document.getElementById("shelf-grid");
  const empty = document.getElementById("shelf-empty");
  const words = bookQueryWords(shelfQuery);
  const books = shelfBooks.filter((b) => titleMatches(b.title, words));
  grid.innerHTML = "";
  empty.hidden = books.length > 0;
  empty.innerHTML = shelfBooks.length && words.length
    ? `<p>No book on your shelf matches “${escapeHtml(shelfQuery)}”.</p>`
    : `<p>No books yet.</p><p class="muted">Tap + to upload your first PDF.</p>`;
  renderShelf(grid, books);
}

function renderSpecialtyShelf() {
  const grid = document.getElementById("shelf-spec-grid");
  const empty = document.getElementById("shelf-spec-empty");
  grid.innerHTML = "";
  const words = bookQueryWords(shelfQuery);
  let books;
  if (words.length) {
    // A search looks through every specialty, not just the one picked.
    const seen = new Set();
    books = (shelfCatalog || []).flatMap((x) => x.books).filter((b) => {
      if (seen.has(b.book_id) || !titleMatches(b.title, words)) return false;
      seen.add(b.book_id);
      return true;
    });
  } else {
    const spec = (shelfCatalog || []).find((x) => x.name === shelfSpecialty);
    books = spec ? spec.books : [];
  }
  document.getElementById("shelf-specialty").disabled = words.length > 0;
  empty.hidden = books.length > 0;
  empty.textContent = words.length
    ? `No specialty book matches “${shelfQuery}”.`
    : shelfCatalog && shelfCatalog.length ? "No books in this specialty yet." : "No specialty libraries yet.";
  renderShelf(grid, books, { shared: true });
}

{
  let t = null;
  document.getElementById("shelf-search").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(() => {
      shelfQuery = e.target.value.trim();
      if (shelfTab === "spec") renderSpecialtyShelf();
      else renderMyShelf();
    }, 120);
  });
}

function bookTile(book, { shared = false, list = false } = {}) {
  const wrap = document.createElement("div");
  wrap.className = list ? "book-row" : "book-tile-wrap";

  const btn = document.createElement("button");
  btn.className = "book-tile";
  btn.setAttribute("aria-label", `Open ${book.title}`);
  const dot = book.qa_indexed ? '<span class="status-dot" title="Indexed for AI Q&A"></span>' : "";
  const coverStyle = book.cover_color ? ` style="background:${book.cover_color}"` : "";
  // The real cover (page 1) replaces the 📘 placeholder once it loads --
  // see loadCoverThumbs. A chosen cover colour still shows until then.
  btn.innerHTML = `<div class="book-cover"${coverStyle} data-cover="${book.book_id}" data-shared="${shared ? 1 : 0}">${ic("book", " cover-ic")}${dot}</div>`;
  btn.addEventListener("click", () => openBook(book.book_id));

  // The title is its own control: in the grid a long one is cut to two
  // lines, and a tap shows it whole (a tap on the cover opens the book).
  const title = document.createElement(list ? "button" : "div");
  title.className = "book-title-label";
  title.textContent = book.title;
  title.title = book.title;
  if (list) {
    title.classList.add("book-row-title");
    const pages = book.page_count ? `<span class="book-row-meta">${Number(book.page_count)} pages</span>` : "";
    title.innerHTML = `<span>${escapeHtml(book.title)}</span>${pages}`;
    title.addEventListener("click", () => openBook(book.book_id));
  } else {
    title.setAttribute("role", "button");
    title.tabIndex = 0;
    title.addEventListener("click", () => title.classList.toggle("is-expanded"));
  }
  wrap.appendChild(btn);
  wrap.appendChild(title);
  if (shared) {
    // Specialty books are the library's: no rename/colour/delete menu, but a
    // toggle that shows the book in My books as well (or takes it off).
    const on = book.source === "shared" ? !!book.in_library : shelfInLibrary.has(book.book_id);
    const toggle = document.createElement("button");
    toggle.className = "lib-toggle" + (on ? " is-on" : "");
    toggle.setAttribute("aria-pressed", on ? "true" : "false");
    toggle.innerHTML = on ? `${ic("check")}<span>In my library</span>` : `${ic("plus")}<span>Add to my library</span>`;
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleInLibrary(book, !on, toggle);
    });
    wrap.appendChild(toggle);
    return wrap;
  }

  const menuBtn = document.createElement("button");
  menuBtn.className = "book-menu-btn";
  menuBtn.textContent = "⋮";
  menuBtn.title = "Book options";
  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openBookMenu(book);
  });

  wrap.appendChild(menuBtn);
  return wrap;
}

function renderShelfViewToggle() {
  const view = shelfView();
  document.querySelectorAll("#view-shelf .view-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.view === view));
}
document.querySelectorAll("#view-shelf .view-btn").forEach((b) =>
  b.addEventListener("click", () => {
    try { localStorage.setItem("shelfView", b.dataset.view); } catch (_) {}
    renderShelfViewToggle();
    if (shelfTab === "spec") renderSpecialtyShelf();
    else renderMyShelf();
  })
);
renderShelfViewToggle();

// Safe for text AND for double- or single-quoted attribute values: titles
// come from uploaded file names, and a '"' there used to end the attribute.
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// ---------------------------------------------------------------------
// Rich text for AI answers: right-to-left lines, bold, bullets, citations
// ---------------------------------------------------------------------
// A Persian/Arabic answer is full of English terms (SSRI, CBT, ESRD), and
// a whole bubble given one direction lays such lines out backwards and
// aligned to the wrong side. Each line is therefore its own block whose
// direction follows the line's DOMINANT script (counting letters, not just
// the first one -- "- SSRI ها خط اول..." is a right-to-left line).
// Markdown the model writes (**bold**, "- " bullets, # headings) is
// rendered instead of shown as raw asterisks.

const RTL_CHARS = /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/g;
const LTR_CHARS = /[A-Za-z\u00C0-\u024F\u0400-\u04FF]/g;

function lineDir(line, fallback) {
  const rtl = (line.match(RTL_CHARS) || []).length;
  const ltr = (line.match(LTR_CHARS) || []).length;
  if (!rtl && !ltr) return fallback;
  return rtl >= ltr ? "rtl" : "ltr";
}

function inlineMarkdown(escapedLine) {
  return escapedLine
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*\*(?=\s|$)/g, "$1")       // a dangling ** left over by the model
    .replace(/\[(\d+)\]/g, (m, n) => `<span class="cite-marker" data-n="${n}">[${n}]</span>`);
}

function richText(text) {
  const lines = String(text || "").replace(/\r/g, "").split("\n");
  const whole = lineDir(text || "", "ltr");
  let prev = whole;
  const out = lines.map((raw) => {
    let line = raw.replace(/\s+$/, "");
    if (!line.trim()) return `<div class="gap"></div>`;
    const dir = lineDir(line, prev);
    prev = dir;
    let html;
    const heading = line.match(/^\s*#{1,4}\s+(.*)$/);
    const bullet = line.match(/^(\s*)[-*•]\s+(.*)$/);
    if (heading) html = `<strong>${inlineMarkdown(escapeHtml(heading[1]))}</strong>`;
    else if (bullet) html = `${bullet[1] ? "&nbsp;&nbsp;" : ""}• ${inlineMarkdown(escapeHtml(bullet[2]))}`;
    else html = inlineMarkdown(escapeHtml(line));
    return `<div dir="${dir}">${html}</div>`;
  });
  return `<div class="rich" dir="${whole}">${out.join("")}</div>`;
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
    <button id="sheet-edit-name">${ic("pencil")} Edit name</button>
    <button id="sheet-delete" class="danger">${ic("trash-2")} Delete book</button>
  `);
  document.getElementById("sheet-edit-name").addEventListener("click", () => promptRenameFromShelf(book));
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
    if (currentBook && currentBook.book_id === book.book_id) showView("view-shelf");
    loadShelf();
  };
  // Long titles are shortened: Telegram refuses a dialog over 256 characters,
  // which used to make such a book impossible to delete.
  const shortTitle = book.title.length > 120 ? book.title.slice(0, 117) + "…" : book.title;
  hideActionSheet();
  confirmMsg(`Delete "${shortTitle}"? This can't be undone.`).then((ok) => { if (ok) doDelete(); });
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

let bookmarksLoadSeq = 0;

async function loadBookmarks() {
  const list = document.getElementById("bookmarks-list");
  const empty = document.getElementById("bookmarks-empty");
  const seq = ++bookmarksLoadSeq;
  let books;
  try {
    ({ books } = await api("/api/books?all=1"));
  } catch (e) {
    alertMsg("Couldn't load bookmarks: " + e.message);
    return;
  }
  if (seq !== bookmarksLoadSeq) return;
  list.innerHTML = "";
  const bookmarked = books.filter((b) => b.bookmark_page);
  empty.hidden = bookmarked.length > 0;
  bookmarked.forEach((book) => {
    const row = document.createElement("button");
    row.className = "bookmark-row";
    const coverStyle = book.cover_color ? ` style="background:${book.cover_color}"` : "";
    row.innerHTML = `
      <span class="bookmark-cover"${coverStyle}>${ic("book", " cover-ic")}</span>
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
  if (await openBook(bookId)) await openReader(page);
}

// A citation tapped in an answer: open that book at the cited page, show
// the quoted passage above the page, and highlight it on the page itself.
// Works for specialty books too (the server opens them on first use).
let readerSource = null;   // {page, text, n} while a cited passage is being shown

async function openSource(bookId, page, text, n) {
  readerSource = { page, text: text || "", n };
  if (!currentBook || currentBook.book_id !== bookId) {
    try {
      currentBook = await api(`/api/books/${bookId}`);
    } catch (e) {
      readerSource = null;
      return alertMsg("Couldn't open that book: " + e.message);
    }
    renderBookHeader();
  }
  await openReader(page);
}

function showReaderSource() {
  const box = document.getElementById("reader-source");
  if (!readerSource || readerSource.page !== readerState.pageNum) {
    box.hidden = true;
    return;
  }
  document.getElementById("reader-source-label").innerHTML =
    `${ic("pin")} Source${readerSource.n ? ` [${Number(readerSource.n)}]` : ""} · page ${Number(readerSource.page)}`;
  document.getElementById("reader-source-text").innerHTML = richText(readerSource.text);
  box.hidden = false;
}

// Best effort: mark the text-layer pieces of this page that belong to the
// quoted passage. Pieces are matched on normalised words, so line breaks
// and hyphenation in the PDF don't stop the match.
function highlightSourceOnPage() {
  if (!readerSource || readerSource.page !== readerState.pageNum) return;
  const norm = (t) => t.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  const excerpt = " " + norm(readerSource.text.replace(/…$/, "")) + " ";
  if (excerpt.trim().length < 12) return;
  document.querySelectorAll("#reader-text-layer span").forEach((span) => {
    const t = norm(span.textContent || "");
    if (t.length >= 4 && excerpt.includes(" " + t + " ")) span.classList.add("src-hl");
  });
  const first = document.querySelector("#reader-text-layer span.src-hl");
  if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
}

// ---------------------------------------------------------------------
// Request a Book: send a URL to the admin from the shelf itself (the
// customer doesn't have this book yet, so it can't live under one book's
// own action grid). Everything past this submit step -- the admin's price
// quote, payment, delivery -- happens as chat messages, not in this mini
// app, since the customer needs a live notification when the admin
// responds and a screen left open here can't reliably deliver that.
// ---------------------------------------------------------------------

document.getElementById("btn-request-book").addEventListener("click", () => {
  document.getElementById("request-book-url").value = "";
  document.getElementById("request-book-note").value = "";
  document.getElementById("request-book-result").innerHTML = "";
  showView("view-request-book");
});

document.getElementById("request-book-send").addEventListener("click", async () => {
  const url = document.getElementById("request-book-url").value.trim();
  const note = document.getElementById("request-book-note").value.trim();
  const result = document.getElementById("request-book-result");
  if (!url) {
    result.innerHTML = `<p class="error-text">Please enter the book's URL.</p>`;
    return;
  }
  const btn = document.getElementById("request-book-send");
  btn.disabled = true;
  result.innerHTML = `<p class="spinner-line">${ic("hourglass")} Sending…</p>`;
  try {
    await api("/api/request-book", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, note }),
    });
    result.innerHTML = `<p class="muted">${ic("circle-check")} Sent! Check your Telegram chat with this bot for the admin's reply.</p>`;
    document.getElementById("request-book-url").value = "";
    document.getElementById("request-book-note").value = "";
  } catch (e) {
    result.innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("btn-add-book").addEventListener("click", () => {
  if (shelfLimit !== null && shelfCount >= shelfLimit) {
    alertMsg(
      `Your free plan holds ${shelfLimit} books on the shelf at a time, and you have ${shelfCount}. ` +
        "Delete a book you're done with to free up a slot, or upgrade to Premium (⭐ My Plan in the bot chat) " +
        "for an unlimited shelf."
    );
    return;
  }
  document.getElementById("file-input").click();
});

document.getElementById("file-input").addEventListener("change", (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = "";
  if (files.length) uploadFiles(files);
});

// ---------------------------------------------------------------------
// Chunked upload (shelf books, and specialty books for the admin)
// ---------------------------------------------------------------------
// The file goes up in small parts (the server sets the size, 1 MB),
// UPLOAD_PARALLEL at a time, each written straight into place on the
// server. Several streams use a slow or far-away connection much better
// than one, and a part that fails is retried on its own instead of the
// whole book starting over. The server keeps an upload's progress on disk,
// so a restart (every deploy is one) only pauses it: parts are retried for
// about two minutes, which outlasts a restart, and carry on where they were.

const UPLOAD_PARALLEL = 4;
const UPLOAD_RETRIES = 16;
const uploadRetryDelay = (attempt) => Math.min(2000 * attempt, 10000);

// --- stay on the latest deployed version -------------------------------
// Telegram keeps a mini app's page alive between opens, so a user can run
// an old app.js for hours after a deploy. The page compares its own
// version (the ?v= it was loaded with) with the server's and reloads when
// it's behind -- checked when the app comes back to the foreground and
// right before an upload starts.
const APP_VERSION = (() => {
  const src = (document.currentScript && document.currentScript.src) || "";
  const m = src.match(/[?&]v=(\d+)/);
  return m ? parseInt(m[1], 10) : 0;
})();

// Something the user would lose on a reload: an upload or a question in
// flight, or text typed into a question box.
function userIsBusy() {
  if (uploadsInFlight > 0 || askBusy || drugsBusy || (typeof lexiBusy !== "undefined" && lexiBusy)) return true;
  if (!document.getElementById("view-web").hidden) return true;  // a reload would drop the live browser
  return ["ask-question", "lookup-question", "lexi-question", "web-live"].some((id) => {
    const el = document.getElementById(id);
    return el && el.value.trim();
  });
}

async function reloadIfOutdated() {
  if (userIsBusy()) return false;  // try again next time the app comes back
  try {
    const { version } = await api("/api/version");
    if (version && APP_VERSION && version > APP_VERSION) {
      window.location.reload();
      return true;
    }
  } catch (_) { /* offline or server busy: keep going on this version */ }
  return false;
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") reloadIfOutdated();
});

// "12.4 / 85.0 MB · 1.8 MB/s · ~41 s left" -- so a slow upload shows
// whether it's moving, and how fast the connection really is.
function uploadProgressText(fraction, bytesDone, total, startedAt) {
  const mb = (b) => (b / 1048576).toFixed(1);
  const secs = (Date.now() - startedAt) / 1000;
  if (fraction >= 1) return "Checking the file…";
  if (secs < 1.5 || bytesDone <= 0) return `Uploading… ${mb(bytesDone)} / ${mb(total)} MB`;
  const rate = bytesDone / secs;
  const left = Math.max(0, (total - bytesDone) / rate);
  const eta = left > 90 ? `~${Math.round(left / 60)} min left` : `~${Math.round(left)} s left`;
  return `Uploading… ${mb(bytesDone)} / ${mb(total)} MB · ${(rate / 1048576).toFixed(1)} MB/s · ${eta}`;
}

function putChunk(url, blob, onBytes) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("X-Telegram-Init-Data", INIT_DATA);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.addEventListener("progress", (e) => { if (e.lengthComputable) onBytes(e.loaded); });
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let detail = null;
      try { detail = JSON.parse(xhr.responseText).detail; } catch (_) { /* ignore */ }
      const err = new Error((typeof detail === "string" && detail) || `Upload failed (${xhr.status})`);
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () => reject(new Error("Network error"));
    xhr.send(blob);
  });
}

let uploadsInFlight = 0;

async function chunkedUpload(file, opts) {
  if (await reloadIfOutdated()) throw new Error("The app was just updated -- please start the upload again.");
  uploadsInFlight++;
  try {
    return await _chunkedUploadAwake(file, opts);
  } finally {
    uploadsInFlight--;
  }
}

async function _chunkedUploadAwake(file, { target = "shelf", title, specialty, series, volume, onProgress }) {
  // Keep the screen on: Android pauses a backgrounded WebView, and a
  // sleeping screen is the usual reason a long upload "stops". (This used
  // to share its name with _chunkedUpload below, which replaced it, so the
  // screen was never actually kept on.)
  let wakeLock = null;
  try { if (navigator.wakeLock) wakeLock = await navigator.wakeLock.request("screen"); } catch (_) { /* not supported */ }
  try {
    return await _chunkedUpload(file, { target, title, specialty, series, volume, onProgress });
  } finally {
    try { if (wakeLock) await wakeLock.release(); } catch (_) { /* already released */ }
  }
}

// One part straight to the bucket (a presigned URL: no sign-in header, which
// the bucket wouldn't accept). Resolves with the part's ETag.
function putPart(url, blob, onBytes) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.upload.addEventListener("progress", (e) => { if (e.lengthComputable) onBytes(e.loaded); });
    xhr.onload = () => {
      const etag = xhr.getResponseHeader("ETag");
      if (xhr.status >= 200 && xhr.status < 300 && etag) return resolve(etag);
      const err = new Error(`Upload failed (${xhr.status}${etag ? "" : ", no ETag"})`);
      err.status = xhr.status || 0;
      reject(err);
    };
    xhr.onerror = () => { const err = new Error("Network error"); err.status = 0; reject(err); };
    xhr.send(blob);
  });
}

// The phone sends the parts to the bucket itself (book_store.py), skipping the
// server's long route. Returns null if the bucket can't be reached at all, so
// the caller falls back to uploading through the server.
async function _directUpload(file, start, onProgress, startedAt) {
  const { upload_id: id, part_size: size, urls } = start;
  const loaded = new Array(urls.length).fill(0);
  const etags = new Array(urls.length).fill(null);
  let anyDone = false;
  const report = () => {
    if (!onProgress) return;
    const done = loaded.reduce((a, b) => a + b, 0);
    onProgress(done / file.size, uploadProgressText(done / file.size, done, file.size, startedAt));
  };
  let next = 0;
  const worker = async () => {
    while (next < urls.length) {
      const i = next++;
      const blob = file.slice(i * size, Math.min(file.size, (i + 1) * size));
      for (let attempt = 1; ; attempt++) {
        try {
          etags[i] = await putPart(urls[i], blob, (b) => { loaded[i] = b; report(); });
          loaded[i] = blob.size;
          anyDone = true;
          report();
          break;
        } catch (e) {
          // Never reached the bucket even once (blocked, CORS): give up on
          // the direct route quickly and let the server take the file.
          if (!anyDone && attempt >= 2) throw Object.assign(e, { unreachable: true });
          if (attempt >= UPLOAD_RETRIES) throw e;
          loaded[i] = 0;
          report();
          await new Promise((r) => setTimeout(r, uploadRetryDelay(attempt)));
        }
      }
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, urls.length) }, worker));
  } catch (e) {
    api(`/api/uploads/${id}`, { method: "DELETE" }).catch(() => {});
    if (e.unreachable) return null;
    throw e;
  }
  if (onProgress) onProgress(1, "Checking the file…");
  for (let attempt = 1; ; attempt++) {
    try {
      return await api(`/api/uploads/${id}/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parts: etags.map((etag, i) => ({ n: i + 1, etag })) }),
      });
    } catch (e) {
      const unreachable = !e.status || e.status === 502 || e.status === 503;
      if (!unreachable || attempt >= 8) throw e;
      await new Promise((r) => setTimeout(r, uploadRetryDelay(attempt)));
    }
  }
}

let directUploadBroken = false;  // the bucket couldn't be reached once: use the server for the rest of this visit

async function _chunkedUpload(file, { target, title, specialty, series, volume, onProgress }) {
  const startedAt = Date.now();
  const startBody = { filename: file.name, size: file.size, title, target, specialty, series, volume };
  let start = await api("/api/uploads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...startBody, direct: !directUploadBroken }),
  });
  if (start.mode === "direct") {
    const done = await _directUpload(file, start, onProgress, startedAt);
    if (done) return done;
    directUploadBroken = true;
    if (onProgress) onProgress(0, "Uploading through the server…");
    start = await api("/api/uploads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(startBody),
    });
  }
  const { upload_id: id, chunk_size: size, chunks } = start;
  const loaded = new Array(chunks).fill(0);
  const report = () => {
    if (!onProgress) return;
    const done = loaded.reduce((a, b) => a + b, 0);
    onProgress(done / file.size, uploadProgressText(done / file.size, done, file.size, startedAt));
  };

  let next = 0;
  const worker = async () => {
    while (next < chunks) {
      const i = next++;
      const blob = file.slice(i * size, Math.min(file.size, (i + 1) * size));
      for (let attempt = 1; ; attempt++) {
        try {
          await putChunk(`/api/uploads/${id}/chunk?index=${i}`, blob, (b) => { loaded[i] = b; report(); });
          loaded[i] = blob.size;
          report();
          break;
        } catch (e) {
          // 4xx other than a cut-off chunk is not going to improve on retry.
          const permanent = e.status && e.status !== 400 && e.status < 500;
          if (permanent || attempt >= UPLOAD_RETRIES) throw e;
          loaded[i] = 0;
          report();
          await new Promise((r) => setTimeout(r, uploadRetryDelay(attempt)));
        }
      }
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, chunks) }, worker));
  } catch (e) {
    api(`/api/uploads/${id}`, { method: "DELETE" }).catch(() => {});
    throw e;
  }
  if (onProgress) onProgress(1, "Checking the file…");
  for (let attempt = 1; ; attempt++) {
    try {
      return await api(`/api/uploads/${id}/finish`, { method: "POST" });
    } catch (e) {
      // Only while the server is unreachable (a restart): anything it
      // actually answered is final.
      const unreachable = !e.status || e.status === 502 || e.status === 503;
      if (!unreachable || attempt >= 8) throw e;
      await new Promise((r) => setTimeout(r, uploadRetryDelay(attempt)));
    }
  }
}

// Shelf upload: the sheet asks for the title (and the volume numbers when
// it is a multi-volume book), then the files go up one after another.
async function uploadFiles(files) {
  const items = await askVolumes(files);
  if (!items) return;
  showView("view-upload");
  const bar = document.getElementById("upload-bar");
  const status = document.getElementById("upload-status");
  let lastId = null;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const label = items.length > 1 ? `${it.label} (${i + 1} of ${items.length}): ` : "";
    bar.style.width = "0%";
    status.textContent = `${label}Starting upload…`;
    try {
      const data = await chunkedUpload(it.file, {
        target: "shelf",
        title: it.title,
        series: it.series,
        volume: it.volume,
        onProgress: (f, text) => {
          bar.style.width = Math.round(f * 100) + "%";
          status.textContent = label + text;
        },
      });
      lastId = data.book_id;
    } catch (e) {
      showView("view-shelf");
      await loadShelf();
      const msg = e.message === "Network error" ? "Upload failed -- check your connection and try again." : e.message;
      return alertMsg(items.length > 1 ? `${it.label}: ${msg}` : msg);
    }
  }
  showView("view-shelf");
  // One book opens straight away; several volumes stay on the shelf, together.
  loadShelf().then(() => { if (items.length === 1 && lastId) openBook(lastId); });
}

// ---------------------------------------------------------------------
// Several volumes of one book
// ---------------------------------------------------------------------
// A multi-volume book is uploaded as one file per volume, all under the
// book's own title; each is stored as "<title> — Volume N" and the Ask
// picker shows them together (tick the title for every volume, or one).

const VOLUME_RE = /(?:^|[\s._\-(\[])(?:vol(?:ume)?|v|part|pt|tome|book)[\s._\-]*0*(\d{1,2})(?=$|[\s._\-)\]])/i;

function cleanFileTitle(name) {
  // "Harrison_s_Principles" -> "Harrison's Principles" (an apostrophe can't be in many shared file names)
  return name.replace(/\.pdf$/i, "").replace(/([A-Za-z])_s(?=_|$)/g, "$1's").replace(/[_]+/g, " ").replace(/\s+/g, " ").trim();
}

// "Harrison_s_Principles_Vol_2_22e.pdf" -> {base: "Harrison s Principles 22e", volume: 2}
function detectVolume(name) {
  const raw = name.replace(/\.pdf$/i, "");
  const m = raw.match(VOLUME_RE);
  if (!m) return { base: cleanFileTitle(name), volume: null };
  const base = cleanFileTitle((raw.slice(0, m.index) + " " + raw.slice(m.index + m[0].length)).replace(/[\s._\-]+$/, ""));
  return { base: base.replace(/[\s\-–—,(\[]+$/, "").trim(), volume: parseInt(m[1], 10) };
}

let volSheetResolve = null;

// Resolves with [{file, title, series, volume, label}] in upload order, or null if cancelled.
// `volumes`: numbers this book already has (new files are numbered after
// them); `multi`: it is known to be a multi-volume book.
function askVolumes(files, { title = "", head = "Add a book", volumes = [], multi: knownMulti = false } = {}) {
  files = Array.from(files || []).filter((f) => f && f.size);
  if (!files.length) return Promise.resolve(null);
  for (const f of files) {
    if (f.size > maxUploadMb * 1024 * 1024) {
      alertMsg(`"${f.name}" is over the ${maxUploadMb}MB limit (${(f.size / 1024 / 1024).toFixed(1)}MB).`);
      return Promise.resolve(null);
    }
  }
  const found = files.map((f) => ({ file: f, ...detectVolume(f.name) }));
  // Volumes found in the names: in that order; otherwise as picked.
  if (found.every((x) => x.volume)) found.sort((a, b) => a.volume - b.volume);
  const multi = knownMulti || files.length > 1 || found[0].volume != null;
  document.getElementById("vol-head").textContent = files.length > 1 ? `Add ${files.length} files` : head;
  document.getElementById("vol-title").value = title || found[0].base || cleanFileTitle(files[0].name);
  document.getElementById("vol-multi").checked = multi;
  document.getElementById("vol-error").hidden = true;
  volSheetExisting = new Set(volumes);
  const used = new Set(volumes);
  document.getElementById("vol-files").innerHTML = found
    .map((x, i) => {
      let n = x.volume && !used.has(x.volume) ? x.volume : null;
      if (!n) { n = 1; while (used.has(n)) n++; }
      used.add(n);
      return `<div class="vol-file" data-i="${i}">${ic("file-text")}<span class="vol-name">${escapeHtml(x.file.name)}</span>` +
        `<label class="vol-num">Volume <input type="number" min="1" max="99" inputmode="numeric" value="${n}" data-vol="${i}" /></label></div>`;
    })
    .join("");
  volSheetFiles = found.map((x) => x.file);
  // A volume added later joins its book by the same title: offer the titles
  // of the multi-volume books already here.
  const known = new Set();
  const addSeries = (b) => { if (b && b.series) known.add(b.series); };
  shelfBooks.forEach(addSeries);
  if (askLib) {
    (askLib.my_books || []).forEach(addSeries);
    (askLib.specialties || []).forEach((sp) => sp.books.forEach(addSeries));
  }
  document.getElementById("vol-series-list").innerHTML = [...known].map((t) => `<option value="${escapeHtml(t)}"></option>`).join("");
  syncVolSheet();
  document.getElementById("vol-sheet").hidden = false;
  return new Promise((resolve) => { volSheetResolve = resolve; });
}

let volSheetFiles = [];
let volSheetExisting = new Set();  // volumes the book already has (adding to a multi-volume book)

function syncVolSheet() {
  const multi = document.getElementById("vol-multi").checked;
  const several = volSheetFiles.length > 1;
  document.getElementById("vol-hint").hidden = !multi;
  document.querySelectorAll("#vol-files .vol-num").forEach((el) => { el.hidden = !multi; });
  // Several files that aren't volumes of one book are separate books, each under its own name.
  const label = document.querySelector("#vol-sheet .vol-label");
  label.textContent = multi ? "Title of the book (all volumes)" : "Title";
  label.hidden = several && !multi;
  document.getElementById("vol-title").hidden = several && !multi;
}

function closeVolSheet(result) {
  document.getElementById("vol-sheet").hidden = true;
  const resolve = volSheetResolve;
  volSheetResolve = null;
  if (resolve) resolve(result);
}

function submitVolSheet() {
  const err = document.getElementById("vol-error");
  const fail = (msg) => { err.textContent = msg; err.hidden = false; };
  const multi = document.getElementById("vol-multi").checked;
  const title = document.getElementById("vol-title").value.replace(/\s+/g, " ").trim();
  if (!multi) {
    if (volSheetFiles.length > 1) {
      return closeVolSheet(volSheetFiles.map((f) => ({ file: f, title: cleanFileTitle(f.name), label: cleanFileTitle(f.name) })));
    }
    if (!title) return fail("Type the book's title.");
    return closeVolSheet([{ file: volSheetFiles[0], title, label: title }]);
  }
  if (!title) return fail("Type the book's title: every volume is kept under it.");
  const items = [];
  const seen = new Set();
  for (const input of document.querySelectorAll("#vol-files input[data-vol]")) {
    const n = parseInt(input.value, 10);
    if (!(n >= 1 && n <= 99)) return fail("Each volume needs a number from 1 to 99.");
    if (seen.has(n)) return fail(`Volume ${n} is listed twice.`);
    if (volSheetExisting.has(n)) return fail(`Volume ${n} is already in the library.`);
    seen.add(n);
    const file = volSheetFiles[parseInt(input.dataset.vol, 10)];
    items.push({ file, title: `${title} — Volume ${n}`, series: title, volume: n, label: `Volume ${n}` });
  }
  items.sort((a, b) => a.volume - b.volume);
  closeVolSheet(items);
}

document.getElementById("vol-multi").addEventListener("change", syncVolSheet);
document.getElementById("vol-go").addEventListener("click", submitVolSheet);
document.getElementById("vol-cancel").addEventListener("click", () => closeVolSheet(null));
document.getElementById("vol-close").addEventListener("click", () => closeVolSheet(null));
document.getElementById("vol-sheet").addEventListener("click", (e) => {
  if (e.target.id === "vol-sheet") closeVolSheet(null);
});

// ---------------------------------------------------------------------
// Book detail
// ---------------------------------------------------------------------

let currentBook = null;

async function openBook(bookId) {
  let book;
  try {
    book = await api(`/api/books/${bookId}`);
  } catch (e) {
    alertMsg("Couldn't open that book: " + e.message);
    return false;
  }
  currentBook = book;
  panelSeq++;
  flashcardChapterFilter = null;  // a chapter index from a previously-open book wouldn't mean anything here
  renderBookHeader();
  document.getElementById("book-panel").hidden = true;
  document.getElementById("book-panel").innerHTML = "";
  showView("view-book");
  return true;
}

function isSpecialtyBook(book) {
  return !!book && book.source === "shared";
}

// Specialty books are divided into chapters for everyone when the admin
// adds them, so they have no "Divide into chapters" step -- if chapters
// aren't there yet, they're still being prepared.
function noChaptersText(what) {
  return isSpecialtyBook(currentBook)
    ? `This book's chapters are still being prepared. Please check back in a few minutes to ${what}.`
    : `Divide this book into chapters first, then come back to ${what}.`;
}

// "Ask questions using AI" inside a book goes to the 💬 Ask tab with only
// this book ticked, ready for a question -- one place to ask, whether it's
// one book or several. A book that isn't searchable yet is made searchable
// first and ticked when that finishes.
async function askAboutCurrentBook() {
  const book = currentBook;
  showView("view-ask");
  await loadAskLibrary();
  askSelected.clear();
  if (isSpecialtyBook(book)) {
    const spec = askLib.specialties.find((x) => x.books.some((b) => b.book_id === book.book_id));
    if (spec) askSpecialty = spec.name;
  }
  if (book.qa_indexed) {
    askSelected.add(book.book_id);
  } else if (!isSpecialtyBook(book)) {
    askIndexOwn(book.book_id);  // ticks it itself once searchable
  }
  askStartTopic();
  renderAskLibrary();
  askShowPage("chat");
  const q = document.getElementById("ask-question");
  q.focus();
  q.scrollIntoView({ block: "center" });
}

function renderBookHeader() {
  // Specialty books come divided into chapters; the button would only confuse.
  document.querySelector('.action-btn[data-action="chapters"]').hidden = isSpecialtyBook(currentBook);
  document.getElementById("book-title").textContent = currentBook.title;
  const chStatus = currentBook.chapters_status;
  document.getElementById("book-meta").textContent =
    `${currentBook.page_count} pages` + (currentBook.qa_indexed ? " · AI Q&A ready" : "");

  const badge = document.getElementById("chapters-badge");
  badge.className = "badge";
  if (chStatus === "done") { badge.innerHTML = `${ic("check")} done`; badge.classList.add("ok"); }
  else if (chStatus === "pending") { badge.textContent = "working…"; }
  else if (chStatus === "error") { badge.textContent = "failed"; badge.classList.add("err"); }
  else { badge.textContent = ""; }
}

let panelSeq = 0;  // bumped whenever the book panel or the open book changes

function panel(html) {
  panelSeq++;
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
    else if (action === "ask") askAboutCurrentBook();
    else if (action === "quiz") handleQuizMenu();
    else if (action === "flashcards") handleFlashcardsMenu();
  });
});

// ---------------------------------------------------------------------
// Generic job polling
// ---------------------------------------------------------------------

// The callbacks run only while the same book and the same panel are still
// on screen: a job finishing after the user moved on (another book, a quiz
// started meanwhile) must not redraw -- or act on -- what is there now.
function pollJob(bookId, jobType, { onProgress, onDone, onError, screenBound = true }) {
  const myPanel = panelSeq;
  const current = () => !screenBound || (currentBook && currentBook.book_id === bookId && panelSeq === myPanel);
  const safely = (fn, arg) => {
    if (!fn || !current()) return;
    Promise.resolve()
      .then(() => fn(arg))
      .catch((e) => alertMsg(e.message));
  };
  let failures = 0;
  const tick = async () => {
    let job;
    try {
      job = await api(`/api/books/${bookId}/jobs/${jobType}`);
      failures = 0;
    } catch (e) {
      // One dropped request isn't a failed job: the server keeps working.
      if (++failures < 4 && e.status !== 401 && e.status !== 404) {
        setTimeout(tick, 2500);
      } else {
        safely(onError, e.message);
      }
      return;
    }
    if (!current() && job.status === "running") {
      setTimeout(tick, 1500);  // keep following quietly; nothing is drawn while away
      return;
    }
    if (job.status === "running") {
      safely(onProgress, job.progress);
      setTimeout(tick, 1500);
    } else if (job.status === "done") {
      safely(onDone, job.result);
    } else if (job.status === "error") {
      safely(onError, job.error || "Something went wrong.");
    } else {
      safely(onError, "No job in progress.");
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
  panel(`<p class="spinner-line">${ic("hourglass")} <span id="chapters-progress">Starting…</span></p>`);
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
    <button class="btn" id="send-chapters">${ic("share")} Send chapter files to chat</button>
    <p id="send-chapters-progress" class="muted"></p>
    <button class="btn secondary" id="redo-chapters">Re-divide</button>
  `);
  document.getElementById("redo-chapters").addEventListener("click", () => {
    currentBook.chapters_status = "none";
    handleChapters();
  });
  document.getElementById("send-chapters").addEventListener("click", sendChapterFilesToChat);
}

// Physically splits the book into one PDF per chapter and delivers each as
// a Telegram document straight to the user's chat with the bot -- same as
// what a chat-uploaded PDF gets automatically, just triggered on demand
// from the Book Shelf mini app for a book that was uploaded/divided there.
async function sendChapterFilesToChat() {
  const btn = document.getElementById("send-chapters");
  const progressEl = document.getElementById("send-chapters-progress");
  btn.disabled = true;
  progressEl.textContent = "Splitting and sending…";
  try {
    await api(`/api/books/${currentBook.book_id}/chapters/send`, { method: "POST" });
  } catch (e) {
    progressEl.textContent = e.message;
    progressEl.classList.add("error-text");
    btn.disabled = false;
    return;
  }
  pollJob(currentBook.book_id, "send_chapters", {
    onDone: (result) => {
      progressEl.classList.remove("error-text");
      progressEl.textContent =
        result.sent === result.total
          ? `Sent all ${result.total} chapter file(s) to your Telegram chat.`
          : `Sent ${result.sent} of ${result.total} chapter file(s) -- the rest failed (they may be too large for Telegram).`;
      btn.disabled = false;
    },
    onError: (msg) => {
      progressEl.classList.add("error-text");
      progressEl.textContent = msg;
      btn.disabled = false;
    },
  });
}

// ---------------------------------------------------------------------
// 2. Reader (pdf.js)
// ---------------------------------------------------------------------

// zoomFactor is a multiplier applied on top of the auto-fit-to-width scale
// (1 = fit width, 1.4 = 40% zoomed in past fit-width, etc.) so zoom keeps
// working sensibly across pages/devices with different fit-width scales.
let readerState = { pdf: null, pageNum: 1, rendering: false, zoomFactor: 1, notes: [], highlights: [] };
const ZOOM_STEP = 0.2;
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;

// Large books (1,000 pages, 100+ MB) on mobile: pdf.js by default keeps
// downloading the WHOLE file in small pieces in the background, so any
// dropped connection -- or a server restart during a deploy -- failed the
// reader with "Failed to fetch". Now it fetches only the pages being read
// (disableAutoFetch), in 1 MB pieces, and a failed open is retried.
async function openPdfWithRetry(url, attempts = 3) {
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try {
      const task = pdfjsLib.getDocument({
        url,
        httpHeaders: { "X-Telegram-Init-Data": INIT_DATA },
        disableAutoFetch: true,
        disableStream: true,
        rangeChunkSize: 1024 * 1024,
        // A crafted font in a PDF could otherwise run code (CVE-2024-4367).
        isEvalSupported: false,
      });
      return await task.promise;
    } catch (e) {
      lastErr = e;
      if (e && e.name === "InvalidPDFException") break;  // a broken file won't get better
      document.getElementById("reader-page-label").textContent = "Reconnecting…";
      await new Promise((r) => setTimeout(r, 1500 * (i + 1)));
    }
  }
  throw lastErr;
}

let readerSeq = 0;

async function openReader(targetPage) {
  const seq = ++readerSeq;
  const bookId = currentBook.book_id;
  // The previous book's document is released now, not kept driving the
  // pager (and holding memory) while the new one loads.
  const old = readerState.pdf;
  readerState.pdf = null;
  readerState.pending = false;
  if (old) old.destroy();
  if (readerSource && readerSource.page !== targetPage) readerSource = null;
  document.getElementById("reader-source").hidden = true;
  showView("view-reader");
  document.getElementById("reader-page-label").textContent = "Loading…";
  document.getElementById("reader-goto-row").hidden = true;
  try {
    pdfjsLib.GlobalWorkerOptions.workerSrc = "vendor/pdf.worker.min.js";
    const pdf = await openPdfWithRetry(`/api/books/${bookId}/file`);
    if (seq !== readerSeq) {  // another book was opened meanwhile
      pdf.destroy();
      return;
    }
    readerState.pdf = pdf;
    // targetPage (from a bookmark tap) wins; otherwise resume from the
    // book's own bookmark if one is set, otherwise page 1.
    readerState.pageNum = targetPage || currentBook.bookmark_page || 1;
    readerState.zoomFactor = 1;
    updateZoomLabel();
    document.getElementById("reader-notes-panel").hidden = true;
    try {
      const result = await api(`/api/books/${bookId}/notes`);
      readerState.notes = result.notes;
    } catch (_) {
      readerState.notes = [];  // non-fatal -- the reader still works without notes loaded
    }
    try {
      readerState.highlights = (await api(`/api/books/${bookId}/highlights`)).highlights || [];
    } catch (_) {
      readerState.highlights = [];
    }
    readerSearchTerm = "";
    document.getElementById("reader-search-results").innerHTML = "";
    document.getElementById("reader-search-status").textContent = "";
    hideSelBar();
    readerClosePanels();
    readerApplyNight();
    if (seq !== readerSeq) return;
    renderReaderPage();
  } catch (e) {
    if (seq !== readerSeq) return;
    alertMsg("Couldn't open the reader: " + e.message);
    goBack();
  }
}

// A flip or zoom while a page is still drawing is remembered and drawn
// right after, so the page on screen always ends up matching the label,
// the bookmark button and the notes.
async function renderReaderPage() {
  if (!readerState.pdf) return;
  if (readerState.rendering) {
    readerState.pending = true;
    return;
  }
  readerState.rendering = true;
  try {
    do {
      readerState.pending = false;
      const pdf = readerState.pdf;
      if (!pdf) break;
      await _renderReaderPage(pdf, readerState.pageNum);
    } while (readerState.pending);
  } catch (e) {
    // A failed page fetch used to leave `rendering` stuck at true, so the
    // reader froze on "Loading…" with a blank page for good.
    document.getElementById("reader-page-label").innerHTML = `${ic("triangle-alert")} Couldn't load -- tap here to retry`;
  } finally {
    readerState.rendering = false;
  }
}

async function getPageWithRetry(pdf, num, attempts = 3) {
  for (let i = 0; ; i++) {
    try {
      return await pdf.getPage(num);
    } catch (e) {
      if (i >= attempts - 1) throw e;
      document.getElementById("reader-page-label").textContent = "Reconnecting…";
      await new Promise((r) => setTimeout(r, 1200 * (i + 1)));
    }
  }
}

async function _renderReaderPage(pdf, num) {
  const page = await getPageWithRetry(pdf, num);
  const canvas = document.getElementById("reader-canvas");
  const ctx = canvas.getContext("2d");
  const containerWidth = document.getElementById("reader-canvas-wrap").clientWidth - 16;
  const baseViewport = page.getViewport({ scale: 1 });
  const fitScale = Math.max(0.5, containerWidth / baseViewport.width);
  const displayScale = fitScale * readerState.zoomFactor;
  // This is the CSS-pixel-sized viewport: used for the canvas's ON-SCREEN
  // size and for the text layer, which is positioned in CSS pixels.
  const viewport = page.getViewport({ scale: displayScale });

  // A <canvas>'s backing-store resolution defaults to CSS pixels, capped
  // at whatever size it's drawn on screen -- on a phone with a 2x-3x pixel
  // density (nearly all of them), that means every page was rendered at
  // roughly a THIRD of the screen's real resolution, which is exactly why
  // text looked soft until zooming in forced more actual pixels to be
  // drawn. Rendering at devicePixelRatio instead (capped at 2x, so a
  // zoomed-in page on a high-DPI phone can't balloon into a huge canvas
  // and risk running out of memory) fixes that at the default zoom level
  // too, not just when zoomed in.
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const renderViewport = page.getViewport({ scale: displayScale * dpr });
  canvas.width = Math.floor(renderViewport.width);
  canvas.height = Math.floor(renderViewport.height);
  canvas.style.width = `${viewport.width}px`;
  canvas.style.height = `${viewport.height}px`;

  await page.render({ canvasContext: ctx, viewport: renderViewport }).promise;

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

  showReaderSource();
  highlightSourceOnPage();
  applyReaderMarks();
  document.getElementById("reader-page-label").textContent = `Page ${num} / ${pdf.numPages}`;
  updateBookmarkButton();
  updateNotesButton();
  if (!document.getElementById("reader-notes-panel").hidden) renderNotesPanel();
}

function updateZoomLabel() {
  document.getElementById("reader-zoom-label").textContent = Math.round(readerState.zoomFactor * 100) + "%";
}

function updateBookmarkButton() {
  const btn = document.getElementById("reader-bookmark");
  const isBookmarked = currentBook.bookmark_page === readerState.pageNum;
  btn.innerHTML = `${ic("bookmark", isBookmarked ? " ic-fill" : "")} ${isBookmarked ? "Bookmarked (tap to remove)" : "Bookmark this page"}`;
}

// ---------------------------------------------------------------------
// Reader notes -- 📝 Notes button, tied to the page currently on screen.
// Shares the exact same notes.py-backed store as the chat-side
// 🧠 Study Tools -> 📓 My Notes, so a note added here shows up there too.
// ---------------------------------------------------------------------

function notesForCurrentPage() {
  return readerState.notes.filter((n) => n.page === readerState.pageNum);
}

function updateNotesButton() {
  const btn = document.getElementById("reader-notes");
  const count = notesForCurrentPage().length;
  btn.innerHTML = `${ic("notebook-pen")} Notes${count ? ` (${count})` : ""}`;
}

function renderNotesPanel() {
  const list = document.getElementById("reader-notes-list");
  const pageNotes = notesForCurrentPage();
  if (!pageNotes.length) {
    list.innerHTML = `<p class="muted">No notes on this page yet.</p>`;
  } else {
    list.innerHTML = "";
    pageNotes.forEach((n) => {
      const row = document.createElement("div");
      row.className = "reader-note-row";
      const text = document.createElement("div");
      text.className = "reader-note-text";
      text.textContent = n.text;
      const del = document.createElement("button");
      del.className = "link-btn";
      del.innerHTML = ic("trash-2");
      del.setAttribute("aria-label", "Delete note");
      del.addEventListener("click", () => deleteReaderNote(n.id));
      row.appendChild(text);
      row.appendChild(del);
      list.appendChild(row);
    });
  }
  document.getElementById("reader-note-input").value = "";
}

document.getElementById("reader-notes").addEventListener("click", () => {
  const panel = document.getElementById("reader-notes-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) renderNotesPanel();
});

document.getElementById("reader-notes-close").addEventListener("click", () => {
  document.getElementById("reader-notes-panel").hidden = true;
});

document.getElementById("reader-note-save").addEventListener("click", async () => {
  const input = document.getElementById("reader-note-input");
  const text = input.value.trim();
  if (!text) return;
  const saveBtn = document.getElementById("reader-note-save");
  saveBtn.disabled = true;
  try {
    const note = await api(`/api/books/${currentBook.book_id}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page: readerState.pageNum, text }),
    });
    readerState.notes.push(note);
    updateNotesButton();
    renderNotesPanel();
  } catch (e) {
    alertMsg("Couldn't save that note: " + e.message);
  } finally {
    saveBtn.disabled = false;
  }
});

async function deleteReaderNote(noteId) {
  try {
    await api(`/api/books/${currentBook.book_id}/notes/${noteId}`, { method: "DELETE" });
  } catch (e) {
    alertMsg("Couldn't delete that note: " + e.message);
    return;
  }
  readerState.notes = readerState.notes.filter((n) => n.id !== noteId);
  updateNotesButton();
  renderNotesPanel();
}

// Shared by the ‹ › buttons and the swipe gesture below. Scroll is reset so
// a new page always opens at its top-left: without that, flipping while
// scrolled down lands you in the middle of the next page, and a zoomed page
// would start already scrolled away from the edge the swipe logic tests.
function flipPage(delta) {
  if (!readerState.pdf) return false;
  const target = readerState.pageNum + delta;
  if (target < 1 || target > readerState.pdf.numPages) return false;
  readerState.pageNum = target;
  readerCanvasWrap.scrollTop = 0;
  readerCanvasWrap.scrollLeft = 0;
  renderReaderPage();
  return true;
}

document.getElementById("reader-page-label").addEventListener("click", () => renderReaderPage());
document.getElementById("reader-prev").addEventListener("click", () => flipPage(-1));
document.getElementById("reader-next").addEventListener("click", () => flipPage(1));

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

// ---------------------------------------------------------------------
// Swipe to flip pages (one-finger horizontal drag)
// ---------------------------------------------------------------------
//
// Edge-aware on purpose. #reader-canvas-wrap scrolls -- that's what makes a
// zoomed-in page pannable (see style.css) -- so a horizontal drag is only
// treated as a page flip when there is no more page left to pan toward in
// that direction. Without that check, zooming in would make a page
// impossible to pan sideways: every pan would jump to the next page.
const SWIPE_MIN_PX = 60;      // shorter than this is a tap or jitter, not a flip
const SWIPE_MAX_MS = 800;     // a slow drag is a pan or a text selection, not a flip
const SWIPE_H_RATIO = 1.5;    // must be clearly horizontal, not a diagonal scroll
const SCROLL_EDGE_SLACK = 2;  // px of rounding slack when testing "already at the edge"

let swipeState = null;

readerCanvasWrap.addEventListener(
  "touchstart",
  (e) => {
    // Anything but a single finger (i.e. the start of a pinch) cancels the
    // swipe outright, so lifting out of a pinch can't register as one.
    if (e.touches.length !== 1) {
      swipeState = null;
      return;
    }
    swipeState = {
      x: e.touches[0].clientX,
      y: e.touches[0].clientY,
      startedAt: Date.now(),
      atLeftEdge: readerCanvasWrap.scrollLeft <= SCROLL_EDGE_SLACK,
      atRightEdge:
        readerCanvasWrap.scrollLeft + readerCanvasWrap.clientWidth >=
        readerCanvasWrap.scrollWidth - SCROLL_EDGE_SLACK,
    };
  },
  { passive: true }
);

readerCanvasWrap.addEventListener(
  "touchend",
  (e) => {
    const start = swipeState;
    swipeState = null;
    // e.touches is what's STILL down: >0 means a finger remains (lifting out
    // of a pinch one finger at a time), which is not the end of a swipe.
    if (!start || e.touches.length > 0 || e.changedTouches.length !== 1) return;
    if (Date.now() - start.startedAt > SWIPE_MAX_MS) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;  // dragging a text selection, not flipping

    const dx = e.changedTouches[0].clientX - start.x;
    const dy = e.changedTouches[0].clientY - start.y;
    if (Math.abs(dx) < SWIPE_MIN_PX || Math.abs(dx) < Math.abs(dy) * SWIPE_H_RATIO) return;

    // Swipe left = pull the next page in from the right, as in any reader.
    if (dx < 0 && start.atRightEdge) flipPage(1);
    else if (dx > 0 && start.atLeftEdge) flipPage(-1);
  },
  { passive: true }
);

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
    panel(`<p>${noChaptersText("summarize it")}</p>`);
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
  // Title for the exported PDF's cover/filename -- the book's own title for
  // a whole-book summary, "Book Title — Chapter Title" for a single chapter.
  const summaryTitle =
    body.scope === "book"
      ? currentBook.title
      : `${currentBook.title} — ${(currentBook.chapters || [])[body.chapter_index]?.title || "Chapter"}`;

  const result = document.getElementById("summarize-result");
  result.innerHTML = `<p class="spinner-line">${ic("hourglass")} <span id="summarize-progress">Starting…</span></p>`;
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
      result.innerHTML = `
        <div style="margin-top:10px;">${richText(text)}</div>
        <button class="btn secondary" id="summarize-export" style="margin-top:12px;">${ic("file-text")} Export as PDF</button>
      `;
      document.getElementById("summarize-export").addEventListener("click", (e) =>
        exportSummaryAsPdf(e.target, summaryTitle, text)
      );
    },
    onError: (msg) => {
      result.innerHTML = `<p class="error-text">${escapeHtml(msg)}</p>`;
    },
  });
}

// Same reasoning as Ask AI's exportAnswerAsPdf: a browser download doesn't
// reliably land anywhere findable inside Telegram's in-app WebView, so the
// backend sends the PDF as a Telegram document to the user's own chat with
// the bot instead of streaming it back over HTTP.
async function exportSummaryAsPdf(btn, title, text) {
  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    await api(`/api/books/${currentBook.book_id}/summarize/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, text }),
    });
    alertMsg("Sent! Check your Telegram chat with this bot to download the PDF.");
  } catch (e) {
    alertMsg("Couldn't export: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalLabel;
  }
}

// ---------------------------------------------------------------------
// 4. Ask AI
// ---------------------------------------------------------------------

let qaHistory = [];
let qaMode = "single"; // "single" | "conversational"
let qaSessionId = null; // client-generated thread id -- every turn asked under it lands in the same history entry

// A question box that grows with what's typed (up to 40% of the screen);
// Ctrl/Cmd+Enter sends.
function autoGrowComposer(id, send) {
  const q = document.getElementById(id);
  const grow = () => { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight + 2, window.innerHeight * 0.4) + "px"; };
  q.addEventListener("input", grow);
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
  });
}

// crypto.randomUUID() isn't guaranteed in every WebView this mini app might
// run in, so fall back to a plain timestamp+random id -- it only needs to be
// unique enough to tell two conversation threads apart, not cryptographic.
function genId() {
  if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function handleAskMenu() {
  qaHistory = [];
  qaMode = "single";
  qaSessionId = genId();
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
    <div class="qa-top-row">
      <button class="link-btn" id="qa-new">🆕 New chat</button>
      <button class="link-btn" id="qa-history">${ic("history")} History</button>
    </div>
    <div class="qa-mode-row">
      <button class="qa-mode-btn ${qaMode === "single" ? "selected" : ""}" data-mode="single">${ic("message-circle")} Single answers</button>
      <button class="qa-mode-btn ${qaMode === "conversational" ? "selected" : ""}" data-mode="conversational">${ic("link")} Conversational</button>
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
  document.getElementById("qa-new").addEventListener("click", () => {
    qaHistory = [];
    qaMode = "single";
    qaSessionId = genId();
    renderAskUI();
  });
  document.getElementById("qa-history").addEventListener("click", showQaHistoryList);
  document.querySelectorAll(".qa-mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      qaMode = btn.dataset.mode;
      // Switching modes mid-conversation would mix "continuous" answers
      // with a fresh no-memory mode confusingly, so start the thread over
      // (a new history entry, not a continuation of the old one).
      qaHistory = [];
      qaSessionId = genId();
      renderAskUI();
    });
  });
}

// "🕘 History": lists past Ask AI conversations for this book (each ask
// turn is saved server-side under its session_id -- see sendQuestion()) so
// the user can reopen and continue one instead of it vanishing the moment
// they leave this panel.
async function showQaHistoryList() {
  panel(`
    <button class="link-btn" id="qa-hist-back">← Back</button>
    <div id="qa-hist-list" class="bookmarks-list"><p class="muted">Loading…</p></div>
  `);
  document.getElementById("qa-hist-back").addEventListener("click", renderAskUI);

  let sessions;
  try {
    const result = await api(`/api/books/${currentBook.book_id}/qa/sessions`);
    sessions = result.sessions || [];
  } catch (e) {
    document.getElementById("qa-hist-list").innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
    return;
  }

  const list = document.getElementById("qa-hist-list");
  if (!sessions.length) {
    list.innerHTML = `<p class="muted">No past conversations yet -- ask a question to start one.</p>`;
    return;
  }
  list.innerHTML = sessions
    .map(
      (s) => `
      <button class="bookmark-row" data-session-id="${s.session_id}">
        <span class="bookmark-info">
          <strong>${escapeHtml(s.preview || "(empty)")}</strong>
          <span class="muted">${s.turn_count} message${s.turn_count === 1 ? "" : "s"} · ${
            s.mode === "conversational" ? "Conversational" : "Single answers"
          } · ${new Date(s.updated_at * 1000).toLocaleString()}</span>
        </span>
        <span class="bookmark-chevron">›</span>
      </button>`
    )
    .join("");
  list.querySelectorAll(".bookmark-row").forEach((btn) => {
    btn.addEventListener("click", () => openQaSession(btn.dataset.sessionId));
  });
}

async function openQaSession(sessionId) {
  let session;
  try {
    session = await api(`/api/books/${currentBook.book_id}/qa/sessions/${sessionId}`);
  } catch (e) {
    alertMsg("Couldn't load that conversation: " + e.message);
    return;
  }
  qaSessionId = session.session_id;
  qaMode = session.mode === "conversational" ? "conversational" : "single";
  qaHistory = [];
  (session.turns || []).forEach((t) => {
    qaHistory.push({ role: "q", text: t.question });
    qaHistory.push({ role: "a", text: t.answer, sources: t.sources || [] });
  });
  renderAskUI();
}

// Turns literal "[1]", "[2]" markers Claude wrote inline into small
// superscript citation numbers, and appends a numbered source list at the
// bottom of the bubble -- exactly the footnote-style citation the mini
// app's Ask AI panel is meant to show.
// Source lines under an answer show a short preview; tapping one opens the
// book at that page with the full passage shown and highlighted.
function sourcePreview(text) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  return t.length > 100 ? t.slice(0, 100).replace(/\s\S*$/, "") + "…" : t;
}

function renderAnswerHtml(text, sources) {
  const body = richText(text);
  if (!sources || !sources.length) return body;
  const lines = sources
    .map((s) => `<span class="src-line" data-n="${s.n}"><span class="n">[${s.n}]</span><span>p.${s.page} — ${escapeHtml(sourcePreview(s.text))}</span></span>`)
    .join("");
  return `${body}<span class="qa-sources">${lines}</span>`;
}

function renderQaLog() {
  const log = document.getElementById("qa-log");
  if (!log) return;
  log.innerHTML = qaHistory
    .map((m, i) => {
      if (m.role === "q") return `<div class="qa-msg q" dir="auto">${escapeHtml(m.text)}</div>`;
      // m.sources is only set once a real answer has come back (it's absent
      // on the "…thinking…" placeholder and on an "Error: ..." bubble), so
      // it doubles as the signal that there's a finished answer to export.
      const exportBtn =
        m.sources !== undefined
          ? `<button class="qa-export-btn" data-msg-index="${i}">${ic("file-text")} Export as PDF</button>`
          : "";
      return `<div class="qa-msg a" data-msg-index="${i}">${renderAnswerHtml(m.text, m.sources)}${exportBtn}</div>`;
    })
    .join("");
  log.scrollTop = log.scrollHeight;
}

// Tapping a small citation number re-shows that excerpt. Delegated on the
// static #view-book container (rather than #qa-log, which is rebuilt from
// scratch by panel()/renderAskUI() and wouldn't keep a directly-attached
// listener) since bubbles are re-rendered wholesale on every turn.
document.getElementById("view-book").addEventListener("click", (e) => {
  const marker = e.target.closest(".cite-marker, .src-line");
  if (!marker) return;
  const bubble = marker.closest(".qa-msg");
  if (!bubble) return;
  const msg = qaHistory[parseInt(bubble.dataset.msgIndex, 10)];
  const source = msg && msg.sources && msg.sources.find((s) => String(s.n) === marker.dataset.n);
  // Opens the book at the cited page with the quoted passage shown and
  // highlighted, instead of an alert with a 150-character snippet.
  if (source) openSource(currentBook.book_id, source.page, source.text, source.n);
});

// Same delegation pattern as the citation-marker handler above, for the
// per-answer "Export as PDF" button.
document.getElementById("view-book").addEventListener("click", (e) => {
  const btn = e.target.closest(".qa-export-btn");
  if (!btn) return;
  const msgIndex = parseInt(btn.dataset.msgIndex, 10);
  const answerMsg = qaHistory[msgIndex];
  const questionMsg = qaHistory[msgIndex - 1];
  if (!answerMsg || !questionMsg) return;
  exportAnswerAsPdf(btn, questionMsg.text, answerMsg.text, answerMsg.sources || []);
});

// A browser-download trick (object URL + hidden <a download>) doesn't
// reliably produce a file the user can find again inside Telegram's in-app
// WebView -- confirmed broken in practice. The backend now sends the PDF as
// a Telegram document straight to the user's own chat with the bot instead
// (every mini-app user already has one, since that's how they got here), so
// this just posts the exchange and reports whether it was sent.
async function exportAnswerAsPdf(btn, question, answer, sources) {
  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    await api(`/api/books/${currentBook.book_id}/ask/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, answer, sources }),
    });
    alertMsg("Sent! Check your Telegram chat with this bot to download the PDF.");
  } catch (e) {
    alertMsg("Couldn't export: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalLabel;
  }
}

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
      // session_id ties every turn asked in this thread to the same saved
      // conversation server-side (see library.append_qa_turn), which is
      // what "🕘 History" reopens later.
      body: JSON.stringify({ question, history: priorTurns, session_id: qaSessionId, mode: qaMode }),
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
// State for the quiz CURRENTLY on screen -- needed by "🏁 End Test" to build
// a summary and save the attempt without re-fetching anything.
let currentQuizQuestions = [];
let currentQuizAnswers = [];   // parallel to currentQuizQuestions: chosen option index, or null if unanswered
let currentQuizMeta = { difficulty: "medium", chapterTitles: [] };

function handleQuizMenu() {
  const chapters = currentBook.chapters || [];
  if (!chapters.length) {
    panel(`<p>${noChaptersText("create a quiz")}</p>`);
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
    <button class="link-btn" id="quiz-history">${ic("history")} Quiz History</button>
    <p><strong>Select chapters:</strong></p>
    <div class="quiz-select-row">
      <button class="link-btn" id="quiz-select-all">${ic("square-check")} Select all</button>
      <button class="link-btn" id="quiz-select-none">⬜ Unselect all</button>
    </div>
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
  document.getElementById("quiz-history").addEventListener("click", showQuizHistoryList);
  document.getElementById("quiz-select-all").addEventListener("click", () => {
    document.querySelectorAll(".quiz-chapter-cb").forEach((cb) => { cb.checked = true; });
  });
  document.getElementById("quiz-select-none").addEventListener("click", () => {
    document.querySelectorAll(".quiz-chapter-cb").forEach((cb) => { cb.checked = false; });
  });
}

async function runQuizGeneration() {
  const checkedBoxes = Array.from(document.querySelectorAll(".quiz-chapter-cb:checked"));
  const chapterIndices = checkedBoxes.map((cb) => parseInt(cb.value, 10));
  let numQuestions = parseInt(document.getElementById("quiz-count").value, 10) || 10;
  numQuestions = Math.max(1, Math.min(MAX_QUIZ_QUESTIONS, numQuestions));

  if (!chapterIndices.length) {
    alertMsg("Select at least one chapter.");
    return;
  }

  const chapters = currentBook.chapters || [];
  currentQuizMeta = {
    difficulty: quizDifficulty,
    chapterTitles: chapterIndices.map((i) => chapters[i] && chapters[i].title).filter(Boolean),
  };

  const result = document.getElementById("quiz-result");
  result.innerHTML = `<p class="spinner-line">${ic("hourglass")} Generating ${numQuestions} questions…</p>`;
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
  currentQuizQuestions = questions;
  currentQuizAnswers = new Array(questions.length).fill(null);

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
        currentQuizAnswers[qi] = oi;
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

  const endBtn = document.createElement("button");
  endBtn.className = "btn";
  endBtn.id = "quiz-end-test";
  endBtn.innerHTML = `${ic("flag")} End Test`;
  endBtn.addEventListener("click", endQuizTest);
  result.appendChild(endBtn);
}

// "🏁 End Test": can be tapped at any point, whether every question has been
// answered or not (unanswered ones just count against the score, same as a
// real exam) -- shows a summary and saves the attempt to Quiz History.
async function endQuizTest() {
  const questions = currentQuizQuestions;
  const answers = currentQuizAnswers;
  const total = questions.length;
  let correct = 0;
  let incorrect = 0;
  let unanswered = 0;
  questions.forEach((q, qi) => {
    if (answers[qi] === null) unanswered += 1;
    else if (answers[qi] === q.correct_index) correct += 1;
    else incorrect += 1;
  });
  const percentage = total ? Math.round((1000 * correct) / total) / 10 : 0;

  const result = document.getElementById("quiz-result");
  result.innerHTML = `
    <div class="quiz-summary">
      <p class="quiz-summary-pct">${percentage}%</p>
      <p class="quiz-summary-row">${ic("circle-check")} Correct: ${correct}</p>
      <p class="quiz-summary-row">${ic("circle-x")} Incorrect: ${incorrect}</p>
      <p class="quiz-summary-row">${ic("circle-minus")} Unanswered: ${unanswered}</p>
      <p class="muted">${correct} / ${total} questions correct</p>
      <p id="quiz-save-status" class="muted">Saving to history…</p>
      <button class="btn secondary" id="quiz-retake">Back to quiz setup</button>
    </div>
  `;
  document.getElementById("quiz-retake").addEventListener("click", handleQuizMenu);

  try {
    await api(`/api/books/${currentBook.book_id}/quiz/attempts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        questions,
        answers,
        difficulty: currentQuizMeta.difficulty,
        chapter_titles: currentQuizMeta.chapterTitles,
      }),
    });
    const statusEl = document.getElementById("quiz-save-status");
    if (statusEl) statusEl.textContent = "Saved to Quiz History.";
  } catch (e) {
    const statusEl = document.getElementById("quiz-save-status");
    if (statusEl) {
      statusEl.textContent = "Couldn't save this attempt to history: " + e.message;
      statusEl.classList.add("error-text");
    }
  }
}

// "🕘 Quiz History": lists past finished/ended attempts for this book.
async function showQuizHistoryList() {
  panel(`
    <button class="link-btn" id="quiz-hist-back">← Back</button>
    <div id="quiz-hist-list" class="bookmarks-list"><p class="muted">Loading…</p></div>
  `);
  document.getElementById("quiz-hist-back").addEventListener("click", handleQuizMenu);

  let attempts;
  try {
    const result = await api(`/api/books/${currentBook.book_id}/quiz/attempts`);
    attempts = result.attempts || [];
  } catch (e) {
    document.getElementById("quiz-hist-list").innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
    return;
  }

  const list = document.getElementById("quiz-hist-list");
  if (!attempts.length) {
    list.innerHTML = `<p class="muted">No past quiz attempts yet.</p>`;
    return;
  }
  list.innerHTML = attempts
    .map(
      (a) => `
      <button class="bookmark-row" data-attempt-id="${a.attempt_id}">
        <span class="bookmark-info">
          <strong>${a.percentage}% (${a.correct_count}/${a.total}) · ${escapeHtml(a.difficulty || "medium")}</strong>
          <span class="muted">${escapeHtml((a.chapter_titles || []).join(", ") || "All chapters")} · ${new Date(
            a.created_at * 1000
          ).toLocaleString()}</span>
        </span>
        <span class="bookmark-chevron">›</span>
      </button>`
    )
    .join("");
  list.querySelectorAll(".bookmark-row").forEach((btn) => {
    btn.addEventListener("click", () => openQuizAttempt(btn.dataset.attemptId));
  });
}

async function openQuizAttempt(attemptId) {
  let attempt;
  try {
    attempt = await api(`/api/books/${currentBook.book_id}/quiz/attempts/${attemptId}`);
  } catch (e) {
    alertMsg("Couldn't load that quiz attempt: " + e.message);
    return;
  }
  renderQuizReview(attempt);
}

// Read-only replay of a past attempt: each question shows the correct
// answer and the user's own pick (if any) already marked, no further
// interaction -- this is a review, not a retake.
function renderQuizReview(attempt) {
  panel(`
    <button class="link-btn" id="quiz-review-back">← Back to history</button>
    <div class="quiz-summary">
      <p class="quiz-summary-pct">${attempt.percentage}%</p>
      <p class="muted">${attempt.correct_count} / ${attempt.total} questions correct</p>
    </div>
    <div id="quiz-review-list"></div>
  `);
  document.getElementById("quiz-review-back").addEventListener("click", showQuizHistoryList);

  const list = document.getElementById("quiz-review-list");
  attempt.questions.forEach((q, qi) => {
    const chosen = attempt.answers[qi];
    const block = document.createElement("div");
    block.className = "quiz-q";
    block.innerHTML = `<div class="q-text">${qi + 1}. ${escapeHtml(q.question)}</div>`;
    q.options.forEach((opt, oi) => {
      const optBtn = document.createElement("button");
      optBtn.className = "quiz-opt";
      optBtn.disabled = true;
      optBtn.textContent = opt;
      if (oi === q.correct_index) optBtn.classList.add("correct");
      else if (oi === chosen) optBtn.classList.add("wrong");
      block.appendChild(optBtn);
    });
    if (chosen === null) {
      const skipped = document.createElement("div");
      skipped.className = "muted";
      skipped.textContent = "(left unanswered)";
      block.appendChild(skipped);
    }
    if (q.explanation) {
      const exp = document.createElement("div");
      exp.className = "quiz-explain";
      exp.textContent = q.explanation;
      block.appendChild(exp);
    }
    list.appendChild(block);
  });
}

// ---------------------------------------------------------------------
// 6. Flashcards -- shares the exact same per-book deck (flashcards.py on
// the server) as the chat-side 🧠 Study Tools -> 🗂 Flashcards flow, so
// cards generated or reviewed in either place show up in both.
// ---------------------------------------------------------------------

let flashcardDeck = null;   // last-fetched {cards, due_count, all_count, hard_count}
let flashcardQueue = [];    // current review session's card queue
let flashcardIndex = 0;
let flashcardRevealed = false;
let flashcardChapterFilter = null;  // null = every chapter; otherwise a chapter index -- scopes the due/all/hard counts and review queue below

// {chapter_index: {title, count}} across the current deck, built from
// currentBook.chapters + each card's own chapter_indices (a card generated
// from several chapters at once counts toward each of them) -- the exact
// same "which chapters actually have cards" data flashcard_flow.py's
// flashcards.chapter_breakdown() computes server-side for the chat flow.
function flashcardChapterBreakdown() {
  const chapters = currentBook.chapters || [];
  const breakdown = {};
  (flashcardDeck.cards || []).forEach((c) => {
    (c.chapter_indices || []).forEach((idx) => {
      if (!breakdown[idx]) breakdown[idx] = { title: (chapters[idx] && chapters[idx].title) || `Chapter ${idx + 1}`, count: 0 };
      breakdown[idx].count += 1;
    });
  });
  return breakdown;
}

function cardsForFilter() {
  const cards = flashcardDeck.cards || [];
  if (flashcardChapterFilter === null) return cards;
  return cards.filter((c) => (c.chapter_indices || []).includes(flashcardChapterFilter));
}

async function handleFlashcardsMenu() {
  panel(`<p class="spinner-line">${ic("hourglass")} Loading…</p>`);
  try {
    flashcardDeck = await api(`/api/books/${currentBook.book_id}/flashcards`);
  } catch (e) {
    panel(`<p class="error-text">${escapeHtml(e.message)}</p>`);
    return;
  }
  renderFlashcardsMenu();
}

function renderFlashcardsMenu() {
  const deck = flashcardDeck;
  const chapters = currentBook.chapters || [];
  const breakdown = flashcardChapterBreakdown();
  const breakdownEntries = Object.entries(breakdown).sort((a, b) => a[0] - b[0]);

  // Scoped to flashcardChapterFilter -- computed client-side from the
  // already-fetched cards array rather than re-hitting the server, same
  // pattern the due/all/hard split itself already uses.
  const scoped = cardsForFilter();
  const now = Date.now() / 1000;
  const dueCount = scoped.filter((c) => c.due_ts <= now).length;
  const hardCount = scoped.filter((c) => (c.ease_factor || 2.5) < 2.5).length;
  const allCount = scoped.length;

  const chapterFilterSection = breakdownEntries.length > 1
    ? `
      <p style="margin-top:12px;"><strong>Filter by chapter:</strong></p>
      <select id="flash-chapter-filter" style="width:100%;padding:8px;border-radius:8px;">
        <option value="all"${flashcardChapterFilter === null ? " selected" : ""}>All chapters (${deck.cards.length})</option>
        ${breakdownEntries
          .map(([idx, info]) => `<option value="${idx}"${flashcardChapterFilter === Number(idx) ? " selected" : ""}>${escapeHtml(info.title)} (${info.count})</option>`)
          .join("")}
      </select>
    `
    : "";

  const reviewButtons = [];
  if (dueCount) reviewButtons.push(`<button class="btn flash-mode-btn" data-mode="due">${ic("repeat")} Review due (${dueCount})</button>`);
  if (allCount) reviewButtons.push(`<button class="btn secondary flash-mode-btn" data-mode="all">${ic("library-big")} Review all (${allCount})</button>`);
  if (hardCount) reviewButtons.push(`<button class="btn secondary flash-mode-btn" data-mode="hard">${ic("circle-alert")} Review hard (${hardCount})</button>`);

  const genSection = !chapters.length
    ? `<p class="muted">${noChaptersText("generate flashcards")}</p>`
    : `
      <p style="margin-top:16px;"><strong>Generate cards from chapters:</strong></p>
      <div class="quiz-select-row">
        <button class="link-btn" id="flash-select-all">${ic("square-check")} Select all</button>
        <button class="link-btn" id="flash-select-none">⬜ Unselect all</button>
      </div>
      <ul class="chapter-list">
        ${chapters
          .map((c, i) => `<li><label><input type="checkbox" class="flash-chapter-cb" value="${i}" /> ${escapeHtml(c.title)}</label></li>`)
          .join("")}
      </ul>
      <p><strong>Number of cards (max ${MAX_FLASHCARD_CARDS}):</strong></p>
      <input id="flash-count" type="number" min="1" max="${MAX_FLASHCARD_CARDS}" value="20" style="width:80px;padding:8px;" />
      <div style="margin-top:12px;">
        <button class="btn" id="flash-generate">Generate flashcards</button>
      </div>
      <div id="flash-gen-result"></div>
    `;

  panel(`
    <p>Cards in deck: ${deck.cards.length}</p>
    ${chapterFilterSection}
    ${reviewButtons.length
      ? `<div class="flash-review-row">${reviewButtons.join("")}</div>`
      : `<p class="muted">${deck.cards.length ? "Nothing to review in this chapter right now." : "No cards yet -- generate some below."}</p>`}
    ${deck.cards.length ? `
      <div class="flash-review-row">
        <button class="btn secondary" id="flash-export">${ic("share")} Export to Anki</button>
        <button class="btn secondary" id="flash-delete">${ic("trash-2")} Delete deck</button>
      </div>` : ""}
    ${genSection}
  `);

  const chapterFilterSelect = document.getElementById("flash-chapter-filter");
  if (chapterFilterSelect) {
    chapterFilterSelect.addEventListener("change", () => {
      const val = chapterFilterSelect.value;
      flashcardChapterFilter = val === "all" ? null : Number(val);
      renderFlashcardsMenu();
    });
  }

  document.querySelectorAll(".flash-mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => startFlashcardReview(btn.dataset.mode));
  });

  const exportBtn = document.getElementById("flash-export");
  if (exportBtn) exportBtn.addEventListener("click", () => exportFlashcards(exportBtn));

  const deleteBtn = document.getElementById("flash-delete");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (await confirmMsg("Delete this whole flashcard deck?")) deleteFlashcardDeck();
    });
  }

  if (chapters.length) {
    document.getElementById("flash-generate").addEventListener("click", runFlashcardGeneration);
    document.getElementById("flash-select-all").addEventListener("click", () => {
      document.querySelectorAll(".flash-chapter-cb").forEach((cb) => { cb.checked = true; });
    });
    document.getElementById("flash-select-none").addEventListener("click", () => {
      document.querySelectorAll(".flash-chapter-cb").forEach((cb) => { cb.checked = false; });
    });
  }
}

async function runFlashcardGeneration() {
  const checkedBoxes = Array.from(document.querySelectorAll(".flash-chapter-cb:checked"));
  const chapterIndices = checkedBoxes.map((cb) => parseInt(cb.value, 10));
  if (!chapterIndices.length) {
    alertMsg("Select at least one chapter.");
    return;
  }
  let numCards = parseInt(document.getElementById("flash-count").value, 10) || 20;
  numCards = Math.max(1, Math.min(MAX_FLASHCARD_CARDS, numCards));

  const result = document.getElementById("flash-gen-result");
  result.innerHTML = `<p class="spinner-line">${ic("hourglass")} Generating ${numCards} flashcard(s)…</p>`;
  try {
    await api(`/api/books/${currentBook.book_id}/flashcards/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chapter_indices: chapterIndices, num_cards: numCards }),
    });
  } catch (e) {
    result.innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
    return;
  }
  pollJob(currentBook.book_id, "flashcards", {
    onDone: (summary) => {
      flashcardDeck = summary;
      renderFlashcardsMenu();
    },
    onError: (msg) => { result.innerHTML = `<p class="error-text">${escapeHtml(msg)}</p>`; },
  });
}

async function deleteFlashcardDeck() {
  try {
    await api(`/api/books/${currentBook.book_id}/flashcards`, { method: "DELETE" });
  } catch (e) {
    alertMsg("Couldn't delete: " + e.message);
    return;
  }
  handleFlashcardsMenu();
}

// Same "send to Telegram chat" pattern as exportSummaryAsPdf/exportAnswerAsPdf
// above -- Telegram's in-app WebView has no reliable browser-download path.
async function exportFlashcards(btn) {
  btn.disabled = true;
  try {
    await api(`/api/books/${currentBook.book_id}/flashcards/export`, { method: "POST" });
    alertMsg("Sent! Check your Telegram chat with this bot to download the .apkg file.");
  } catch (e) {
    alertMsg("Couldn't export: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

function startFlashcardReview(mode) {
  const scoped = cardsForFilter();  // respects flashcardChapterFilter
  let queue;
  if (mode === "due") queue = scoped.filter((c) => c.due_ts <= Date.now() / 1000);
  else if (mode === "hard") queue = scoped.filter((c) => (c.ease_factor || 2.5) < 2.5).sort((a, b) => (a.ease_factor || 2.5) - (b.ease_factor || 2.5));
  else queue = scoped.slice();

  if (!queue.length) {
    alertMsg("Nothing to review in that mode right now.");
    return;
  }
  flashcardQueue = queue;
  flashcardIndex = 0;
  flashcardRevealed = false;
  renderFlashcardReview();
}

function renderFlashcardReview() {
  if (flashcardIndex >= flashcardQueue.length) {
    panel(`
      <p class="quiz-summary-pct">${ic("party-popper", " big-ic")}</p>
      <p class="quiz-summary-row">All done for now!</p>
      <button class="btn secondary" id="flash-review-back">Back to Flashcards</button>
    `);
    document.getElementById("flash-review-back").addEventListener("click", handleFlashcardsMenu);
    return;
  }

  const card = flashcardQueue[flashcardIndex];
  const progress = `${flashcardIndex + 1} / ${flashcardQueue.length}`;

  if (!flashcardRevealed) {
    panel(`
      <p class="muted">${progress}</p>
      <div class="flash-card">${escapeHtml(card.front)}</div>
      <button class="btn" id="flash-reveal">${ic("search")} Show answer</button>
    `);
    document.getElementById("flash-reveal").addEventListener("click", () => {
      flashcardRevealed = true;
      renderFlashcardReview();
    });
  } else {
    panel(`
      <p class="muted">${progress}</p>
      <div class="flash-card">${escapeHtml(card.front)}</div>
      <div class="flash-card flash-card-back">${escapeHtml(card.back)}</div>
      <div class="flash-rate-row">
        <button class="flash-rate-btn again" data-q="0">${ic("circle-x")} Again</button>
        <button class="flash-rate-btn hard" data-q="3">${ic("frown")} Hard</button>
        <button class="flash-rate-btn good" data-q="4">${ic("smile")} Good</button>
        <button class="flash-rate-btn easy" data-q="5">${ic("laugh")} Easy</button>
      </div>
    `);
    document.querySelectorAll(".flash-rate-btn").forEach((btn) => {
      btn.addEventListener("click", () => rateFlashcard(card, parseInt(btn.dataset.q, 10)));
    });
  }
}

async function rateFlashcard(card, quality) {
  try {
    await api(`/api/books/${currentBook.book_id}/flashcards/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_id: card.id, quality }),
    });
  } catch (e) {
    alertMsg("Couldn't record that rating: " + e.message);
    return;
  }
  flashcardIndex += 1;
  flashcardRevealed = false;
  renderFlashcardReview();
}


// ---------------------------------------------------------------------
// Drugs tab: interactions + label Q&A
// ---------------------------------------------------------------------
//
// Both tools exist in the bot's chat menu already. The point of having
// them here is that neither is really a turn-by-turn conversation: an
// interaction check is a SET of drugs you assemble and then run, which in
// chat means typing one name per message and re-reading the list from
// scrollback, and a drug Q&A is a thread against one label, which in chat
// has no visible "which drug am I asking about" state. Chips you can
// remove, and a thread with the drug pinned above it, are what those two
// shapes actually want.
//
// Neither reimplements anything: both POST to endpoints that call the same
// interaction_ai / drug_qa functions the chat flows call.

const DRUG_SUGGEST_MIN_CHARS = 2;
const DRUG_SUGGEST_DEBOUNCE_MS = 250;

let interactionDrugs = [];
let lookupDrug = null;
let lookupThread = [];   // [{q, a}] -- sent back as history so follow-ups keep context
let lookupTopicId = null;      // saved-topic id for this drug thread (kind "drug")
let lookupWebSites = [];       // [{id, title}] websites Drug LookUp can also search (web_sources.py)
const lookupWebOff = new Set(); // the ones this user unticked
let interactionTopicId = null; // saved-topic id of the check on screen (kind "interactions")
let drugsBusy = false;

function drugsTab(name) {
  document.querySelectorAll("#view-drugs .subtab").forEach((t) => {
    t.classList.toggle("is-active", t.dataset.drugtab === name);
  });
  document.getElementById("drugs-pane-interactions").hidden = name !== "interactions";
  document.getElementById("drugs-pane-lookup").hidden = name !== "lookup";
  document.getElementById("drugs-pane-lexi").hidden = name !== "lexi";
  document.getElementById("drugs-pane-web").hidden = name !== "web";
  if (name === "lexi") checkLexiStatus();
  if (name === "web") loadWebSites();
}

// --- shared name autocomplete ----------------------------------------
// One debounced fetch per input. Suggestions are a convenience, so a
// failed or slow lookup just leaves the list closed rather than surfacing
// an error over something the user is still typing.
function wireDrugSuggest(inputId, boxId, onPick) {
  const input = document.getElementById(inputId);
  const box = document.getElementById(boxId);
  let timer = null;
  let seq = 0;

  function close() {
    // Also cancel a pending or in-flight lookup, so its late reply can't
    // reopen the list after the name was already set.
    clearTimeout(timer);
    seq++;
    box.hidden = true;
    box.innerHTML = "";
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < DRUG_SUGGEST_MIN_CHARS) return close();
    timer = setTimeout(async () => {
      const mine = ++seq;
      let names = [];
      try {
        ({ names } = await api(`/api/drugs/search?q=${encodeURIComponent(q)}`));
      } catch (_) {
        return close();
      }
      // A slower earlier request must not overwrite a newer one's results.
      if (mine !== seq) return;
      if (!names || !names.length) return close();
      box.innerHTML = names
        .map((n) => `<button class="suggestion" data-name="${escapeHtml(n)}">${escapeHtml(n)}</button>`)
        .join("");
      box.hidden = false;
    }, DRUG_SUGGEST_DEBOUNCE_MS);
  });

  box.addEventListener("click", (e) => {
    const btn = e.target.closest(".suggestion");
    if (!btn) return;
    input.value = btn.dataset.name;
    close();
    onPick(btn.dataset.name);
  });

  return { close };
}

// A name miss comes back as 404 + suggestions; offer them instead of
// making the user guess the spelling again.
function renderNameSuggestions(container, err, onPick) {
  const suggestions = (err.detail && err.detail.suggestions) || [];
  if (!suggestions.length) {
    container.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }
  container.innerHTML =
    `<p class="muted">${escapeHtml(err.message)} Did you mean:</p>` +
    `<div class="suggestions is-inline">` +
    suggestions.map((n) => `<button class="suggestion" data-name="${escapeHtml(n)}">${escapeHtml(n)}</button>`).join("") +
    `</div>`;
  container.querySelectorAll(".suggestion").forEach((b) => {
    b.addEventListener("click", () => onPick(b.dataset.name));
  });
}

// --- interactions -----------------------------------------------------

function renderInteractionChips() {
  const box = document.getElementById("interaction-chips");
  box.innerHTML = interactionDrugs
    .map((n, i) => `<span class="chip">${escapeHtml(n)}<button class="chip-x" data-i="${i}">×</button></span>`)
    .join("");
  document.getElementById("interaction-check").disabled = interactionDrugs.length < 2 || drugsBusy;
  document.getElementById("interaction-clear").hidden = interactionDrugs.length === 0;
}

// Each name is confirmed against openFDA as it is added, not when the
// check runs. A typo is then a one-second "did you mean", instead of a
// long multi-drug check that fails at the end on something that was
// knowable immediately -- which is exactly what "Sertralin" did.
async function addInteractionDrug(name) {
  const clean = (name || "").trim();
  if (!clean || drugsBusy) return;
  if (interactionDrugs.some((d) => d.toLowerCase() === clean.toLowerCase())) {
    alertMsg(`${clean} is already on the list.`);
    return;
  }

  const input = document.getElementById("interaction-input");
  const result = document.getElementById("interaction-result");
  drugsBusy = true;
  renderInteractionChips();
  input.disabled = true;
  try {
    const { name: resolved } = await api("/api/drugs/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: clean }),
    });
    // Guard again post-await: the label's own spelling may collide with a
    // chip that is already there ("Coumadin" resolving onto "Warfarin").
    if (interactionDrugs.some((d) => d.toLowerCase() === resolved.toLowerCase())) {
      alertMsg(`${resolved} is already on the list.`);
    } else {
      interactionDrugs.push(resolved);
    }
    input.value = "";
    result.hidden = true;
  } catch (e) {
    result.hidden = false;
    if (e.status === 404) {
      renderNameSuggestions(result, e, (picked) => {
        result.hidden = true;
        addInteractionDrug(picked);
      });
    } else {
      result.innerHTML = `<p class="muted">${escapeHtml(e.message)}</p>`;
    }
  } finally {
    input.disabled = false;
    drugsBusy = false;
    renderInteractionChips();
  }
}

async function checkInteractions() {
  if (interactionDrugs.length < 2 || drugsBusy) return;
  const result = document.getElementById("interaction-result");
  drugsBusy = true;
  renderInteractionChips();
  result.hidden = false;
  result.innerHTML = `<p class="muted">Checking interactions…</p>`;
  const myCheck = genId();
  try {
    const { answer, drugs } = await api("/api/drugs/interactions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: interactionDrugs, topic_id: (interactionTopicId = myCheck) }),
    });
    // Another saved check was opened (or the list cleared) meanwhile.
    if (interactionTopicId !== myCheck) return;
    // The server resolves each name to the label's own spelling; show that
    // back so it is clear which products were actually read.
    if (Array.isArray(drugs) && drugs.length) {
      interactionDrugs = drugs;
      renderInteractionChips();
    }
    renderInteractionResult(interactionDrugs, answer);
  } catch (e) {
    if (interactionTopicId !== myCheck) return;  // the screen has moved on to another check
    if (e.status === 404) {
      // Every chip was confirmed when it was added, so reaching here means
      // a label that existed a moment ago no longer resolves. Drop the one
      // that failed and let them re-add it, rather than guessing a fix.
      const failed = interactionDrugs.find((d) => e.message.includes(d));
      renderNameSuggestions(result, e, (name) => {
        if (failed) interactionDrugs = interactionDrugs.filter((d) => d !== failed);
        result.hidden = true;
        addInteractionDrug(name);
      });
    } else {
      result.innerHTML = `<p class="muted">${escapeHtml(e.message)}</p>`;
    }
  } finally {
    drugsBusy = false;
    renderInteractionChips();
  }
}

function renderInteractionResult(drugs, answer) {
  const result = document.getElementById("interaction-result");
  result.hidden = false;
  result.innerHTML =
    `<p class="muted">${escapeHtml(drugs.join(" · "))}</p>` +
    `<div class="qa-msg a">${richText(answer)}` +
    (interactionTopicId
      ? `<div class="ask-turn-tools"><button class="mini-btn" data-export-interaction="1">${ic("file-text")} Save as PDF</button></div>`
      : "") +
    `</div>`;
}

// --- saved Drugs-tab topics (same store as Ask your books, by kind) ----

async function showDrugTopics(panelId, kind, emptyText) {
  const box = document.getElementById(panelId);
  const list = box.querySelector(".topics-list");
  box.hidden = false;
  list.innerHTML = `<p class="muted">Loading…</p>`;
  let topics;
  try {
    ({ topics } = await api(`/api/ask/topics?kind=${kind}`));
  } catch (e) {
    list.innerHTML = `<p class="muted">${escapeHtml(e.message)}</p>`;
    return;
  }
  list.innerHTML = topics.length
    ? topics
        .map((t) => {
          const when = new Date(t.updated * 1000).toLocaleDateString();
          const meta = kind === "interactions" ? when : `${t.turns} answer${t.turns === 1 ? "" : "s"} · ${when}`;
          return (
            `<div class="topic-row"><button class="topic-main" data-topic="${t.topic_id}">` +
            `<span class="topic-title" dir="auto">${escapeHtml(t.title)}</span>` +
            `<span class="topic-meta">${meta}</span>` +
            `</button><button class="mini-btn" data-del-topic="${t.topic_id}" title="Delete">${ic("trash-2")}</button></div>`
          );
        })
        .join("")
    : `<p class="muted">${emptyText}</p>`;
  box.scrollIntoView({ block: "start", behavior: "smooth" });
}

function wireDrugTopicsPanel(panelId, kind, emptyText, onOpen, onDeleted) {
  const box = document.getElementById(panelId);
  box.addEventListener("click", async (e) => {
    if (e.target.closest(".topics-close")) {
      box.hidden = true;
      return;
    }
    const open = e.target.closest("button.topic-main");
    if (open) {
      try {
        const topic = await api(`/api/ask/topics/${open.dataset.topic}`);
        box.hidden = true;
        onOpen(open.dataset.topic, topic);
      } catch (err) {
        alertMsg(err.message);
      }
      return;
    }
    const del = e.target.closest("button[data-del-topic]");
    if (del) {
      try {
        await api(`/api/ask/topics/${del.dataset.delTopic}`, { method: "DELETE" });
        onDeleted(del.dataset.delTopic);
        showDrugTopics(panelId, kind, emptyText);
      } catch (err) {
        alertMsg(err.message);
      }
    }
  });
}

// --- drug lookup (Q&A against one label) ------------------------------

// The websites an answer can also draw on, and (for the admin) the
// 🌐 Websites tab where they are added. Refreshed after the admin changes one.
async function loadDrugWebsites() {
  let data;
  try {
    data = await api("/api/drugs/websites");
  } catch (_) {
    return;  // the label alone still answers
  }
  lookupWebSites = data.sites || [];
  document.querySelector('#view-drugs .subtab[data-drugtab="web"]').hidden = !data.is_admin;
  renderLookupWebsites();
}

function renderLookupWebsites() {
  document.getElementById("lookup-web").hidden = lookupWebSites.length === 0;
  document.getElementById("lookup-web-list").innerHTML = lookupWebSites
    .map(
      (w) =>
        `<label class="check-row"><input type="checkbox" data-web-id="${escapeHtml(w.id)}" ${lookupWebOff.has(w.id) ? "" : "checked"} />` +
        `<span class="web-row-icon">${ic("globe")}</span><span class="check-title">${escapeHtml(w.title)}</span></label>`
    )
    .join("");
}

function lookupWebIds() {
  return lookupWebSites.filter((w) => !lookupWebOff.has(w.id)).map((w) => w.id).slice(0, 5);
}

function renderLookupState() {
  const label = document.getElementById("lookup-current");
  const askPanel = document.getElementById("lookup-ask-panel");
  if (lookupDrug) {
    label.hidden = false;
    label.textContent = `Asking about: ${lookupDrug}`;
    askPanel.hidden = false;
  } else {
    label.hidden = true;
    askPanel.hidden = true;
  }
  renderLookupThread();
}

function renderLookupThread() {
  const box = document.getElementById("lookup-thread");
  box.innerHTML = lookupThread
    .map(
      (t, i) =>
        `<div class="qa-msg q" dir="auto">${escapeHtml(t.q)}</div>` +
        `<div class="qa-msg a" data-turn="${i}">${t.sources && t.sources.length ? askAnswerHtml(t.a, t.sources) : richText(t.a)}` +
        (t.done && lookupTopicId
          ? `<div class="ask-turn-tools"><button class="mini-btn" data-export-turn="${i}">${ic("file-text")} Save as PDF</button></div>`
          : "") +
        `</div>`
    )
    .join("");
  box.scrollTop = box.scrollHeight;
  const answered = lookupThread.some((t) => t.done);
  document.getElementById("lookup-topic-note").hidden = !answered;
  document.getElementById("lookup-topic-pdf").hidden = !(answered && lookupTopicId);
}

function lookupStartTopic() {
  lookupDrug = null;
  lookupThread = [];
  lookupTopicId = null;
  document.getElementById("lookup-drug-input").value = "";
  renderLookupState();
}

async function setLookupDrug(name) {
  const clean = (name || "").trim();
  if (!clean || drugsBusy) return;

  const input = document.getElementById("lookup-drug-input");
  const thread = document.getElementById("lookup-thread");
  drugsBusy = true;
  input.disabled = true;
  try {
    // Confirmed before the first question rather than on it, so a typo
    // costs a second here instead of a full answer round-trip.
    const { name: resolved } = await api("/api/drugs/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: clean }),
    });
    // A different drug means a different label, so the thread cannot carry
    // over -- its history would be answered against the wrong document.
    if (lookupDrug && resolved.toLowerCase() !== lookupDrug.toLowerCase()) {
      lookupThread = [];
      lookupTopicId = null;
    }
    lookupDrug = resolved;
    input.value = resolved;
    renderLookupState();
  } catch (e) {
    if (e.status === 404) {
      renderNameSuggestions(thread, e, (picked) => setLookupDrug(picked));
    } else {
      alertMsg(e.message);
    }
  } finally {
    input.disabled = false;
    drugsBusy = false;
  }
}

async function askLookup() {
  if (drugsBusy) return;
  const input = document.getElementById("lookup-question");
  const question = input.value.trim();
  if (!lookupDrug) return alertMsg("Pick a drug first.");
  if (!question) return;

  drugsBusy = true;
  const askBtn = document.getElementById("lookup-ask");
  askBtn.disabled = true;
  const thread = lookupThread;
  const topicId = lookupTopicId || (lookupTopicId = genId());
  const askedDrug = lookupDrug;
  const webIds = lookupWebIds();
  const turn = { q: question, a: webIds.length ? "…reading the label and searching the websites…" : "…reading the label…" };
  const history = thread.map((t) => ({ question: t.q, answer: t.a }));
  thread.push(turn);
  input.value = "";
  renderLookupThread();
  let suggestionsShown = false;

  try {
    const { answer, drug, sources, skipped } = await api("/api/drugs/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        drug: askedDrug,
        question,
        history,  // everything answered so far in this thread
        topic_id: topicId,
        web_ids: webIds,
      }),
    });
    const note = skipped && skipped.length ? `\n\n(Couldn't search: ${skipped.join(", ")}.)` : "";
    Object.assign(turn, { a: answer + note, sources: sources || [], done: true });
    if (thread === lookupThread && drug && drug !== lookupDrug) {
      lookupDrug = drug;
      renderLookupState();
    }
  } catch (e) {
    // The failed turn is dropped rather than left in the thread: it would
    // otherwise be replayed as history on the next question, teaching the
    // model that an error message is a valid answer.
    const i = thread.indexOf(turn);
    if (i >= 0) thread.splice(i, 1);
    if (e.status === 404 && thread === lookupThread) {
      const panel = document.getElementById("lookup-thread");
      renderNameSuggestions(panel, e, (name) => {
        setLookupDrug(name);
        document.getElementById("lookup-question").value = question;
      });
      suggestionsShown = true;
    } else {
      alertMsg(e.message);
    }
  } finally {
    drugsBusy = false;
    askBtn.disabled = false;
    if (!suggestionsShown) renderLookupThread();
  }
}

// --- Lexicomp / Lexidrug (read off the cloud phone) -------------------
//
// Same thread shape as Drug LookUp, but the source document is the
// Lexidrug monograph the server reads off the VMOS cloud phone rather than
// an FDA label. Opening a drug is its own step because it is the slow one
// (the phone has to search, open and scroll the monograph); the server
// caches the result, so every question after that is a normal Claude call.
// There is no autocomplete or "did you mean" here: the app's own search is
// the authority on names, and the server reports back the title Lexidrug
// actually opened so a wrong match is visible before anyone asks.

let lexiDrug = null;
let lexiThread = [];
let lexiBusy = false;
let lexiStatusChecked = false;

async function checkLexiStatus() {
  if (lexiStatusChecked) return;
  lexiStatusChecked = true;
  try {
    const { enabled } = await api("/api/lexidrug/status");
    document.getElementById("lexi-offline").hidden = !!enabled;
  } catch (_) {
    lexiStatusChecked = false; // try again next time the tab is opened
  }
}

function renderLexiState() {
  const label = document.getElementById("lexi-current");
  if (lexiDrug) {
    label.hidden = false;
    label.textContent = `Lexidrug monograph: ${lexiDrug}`;
  } else {
    label.hidden = true;
  }
  document.getElementById("lexi-ask-panel").hidden = !lexiDrug;
  const box = document.getElementById("lexi-thread");
  box.innerHTML = lexiThread
    .map((t) => `<div class="qa-msg q" dir="auto">${escapeHtml(t.q)}</div><div class="qa-msg a">${richText(t.a)}</div>`)
    .join("");
  box.scrollTop = box.scrollHeight;
}

async function openLexiDrug(name) {
  const clean = (name || "").trim();
  if (!clean || lexiBusy) return;
  const input = document.getElementById("lexi-drug-input");
  const setBtn = document.getElementById("lexi-drug-set");
  const label = document.getElementById("lexi-current");
  lexiBusy = true;
  input.disabled = true;
  setBtn.disabled = true;
  label.hidden = false;
  label.textContent = `Opening "${clean}" in Lexidrug on the cloud phone…`;
  try {
    const { drug } = await api("/api/lexidrug/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drug: clean }),
    });
    if (!lexiDrug || drug.toLowerCase() !== lexiDrug.toLowerCase()) lexiThread = [];
    lexiDrug = drug;
    input.value = clean;
  } catch (e) {
    alertMsg(e.message);
  } finally {
    input.disabled = false;
    setBtn.disabled = false;
    lexiBusy = false;
    renderLexiState();
  }
}

async function askLexi() {
  if (lexiBusy) return;
  const input = document.getElementById("lexi-question");
  const question = input.value.trim();
  if (!lexiDrug) return alertMsg("Open a drug first.");
  if (!question) return;

  lexiBusy = true;
  const askBtn = document.getElementById("lexi-ask");
  askBtn.disabled = true;
  const thread = lexiThread;
  const turn = { q: question, a: "…reading the Lexidrug monograph…" };
  const history = thread.map((t) => ({ question: t.q, answer: t.a }));
  thread.push(turn);
  input.value = "";
  renderLexiState();
  try {
    const { answer } = await api("/api/lexidrug/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drug: lexiDrug, question, history }),
    });
    turn.a = answer;
  } catch (e) {
    const i = thread.indexOf(turn);
    if (i >= 0) thread.splice(i, 1); // never replay an error as history
    if (thread === lexiThread && !input.value) input.value = question;
    alertMsg(e.message);
  } finally {
    lexiBusy = false;
    askBtn.disabled = false;
    renderLexiState();
  }
}

function wireLexiPane() {
  document.getElementById("lexi-drug-set").addEventListener("click", () =>
    openLexiDrug(document.getElementById("lexi-drug-input").value)
  );
  document.getElementById("lexi-drug-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      openLexiDrug(e.target.value);
    }
  });
  document.getElementById("lexi-ask").addEventListener("click", askLexi);
  document.getElementById("lexi-reset").addEventListener("click", () => {
    lexiDrug = null;
    lexiThread = [];
    document.getElementById("lexi-drug-input").value = "";
    renderLexiState();
  });
  renderLexiState();
}

function wireDrugsTab() {
  document.querySelectorAll("#tabbar .tab").forEach((t) => {
    t.addEventListener("click", () => {
      showView(t.dataset.tab);
      if (t.dataset.tab === "view-shelf") setShelfTab(shelfTab);
      if (t.dataset.tab === "view-ask") loadAskLibrary();
    });
  });
  document.querySelectorAll("#view-drugs .subtab").forEach((t) => {
    t.addEventListener("click", () => drugsTab(t.dataset.drugtab));
  });

  const interactionSuggest = wireDrugSuggest("interaction-input", "interaction-suggestions", addInteractionDrug);
  document.getElementById("interaction-add").addEventListener("click", () => {
    interactionSuggest.close();
    addInteractionDrug(document.getElementById("interaction-input").value);
  });
  document.getElementById("interaction-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      interactionSuggest.close();
      addInteractionDrug(e.target.value);
    }
  });
  document.getElementById("interaction-chips").addEventListener("click", (e) => {
    const x = e.target.closest(".chip-x");
    if (!x) return;
    interactionDrugs.splice(Number(x.dataset.i), 1);
    renderInteractionChips();
  });
  document.getElementById("interaction-check").addEventListener("click", checkInteractions);
  document.getElementById("interaction-clear").addEventListener("click", () => {
    interactionDrugs = [];
    interactionTopicId = null;
    document.getElementById("interaction-result").hidden = true;
    renderInteractionChips();
  });

  const lookupSuggest = wireDrugSuggest("lookup-drug-input", "lookup-suggestions", setLookupDrug);
  document.getElementById("lookup-drug-set").addEventListener("click", () => {
    lookupSuggest.close();
    setLookupDrug(document.getElementById("lookup-drug-input").value);
  });
  document.getElementById("lookup-drug-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      lookupSuggest.close();
      setLookupDrug(e.target.value);
    }
  });
  document.getElementById("lookup-ask").addEventListener("click", askLookup);
  document.getElementById("lookup-reset").addEventListener("click", lookupStartTopic);
  document.getElementById("lookup-topic-pdf").addEventListener("click", (e) => exportTopic(e.currentTarget, lookupTopicId));
  document.getElementById("lookup-thread").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-export-turn]");
    if (btn) return exportTopic(btn, lookupTopicId, parseInt(btn.dataset.exportTurn, 10));
    // a [n] marker or source line: the website page it came from
    const cite = e.target.closest(".cite-marker, .src-line");
    const msg = cite && cite.closest(".qa-msg");
    const t = msg && lookupThread[parseInt(msg.dataset.turn, 10)];
    const src = t && t.sources && t.sources.find((x) => String(x.n) === cite.dataset.n);
    if (src && src.url) openExternal(src.url);
  });
  document.getElementById("lookup-web-list").addEventListener("change", (e) => {
    const box = e.target.closest("input[data-web-id]");
    if (!box) return;
    if (box.checked) lookupWebOff.delete(box.dataset.webId);
    else lookupWebOff.add(box.dataset.webId);
  });
  loadDrugWebsites();
  const lookupEmpty = "No saved drug questions yet. Every answer here is saved automatically.";
  document.getElementById("lookup-history").addEventListener("click", () =>
    showDrugTopics("lookup-topics", "drug", lookupEmpty)
  );
  wireDrugTopicsPanel(
    "lookup-topics", "drug", lookupEmpty,
    (topicId, topic) => {
      lookupTopicId = topicId;
      lookupDrug = (topic.books && topic.books[0] && topic.books[0].title) || null;
      lookupThread = (topic.turns || []).map((t) => ({ q: t.q, a: t.a, sources: t.sources || [], done: true }));
      document.getElementById("lookup-drug-input").value = lookupDrug || "";
      renderLookupState();
      document.getElementById("lookup-thread").scrollIntoView({ block: "start", behavior: "smooth" });
    },
    (deleted) => { if (lookupTopicId === deleted) lookupStartTopic(); }
  );

  document.getElementById("interaction-result").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-export-interaction]");
    if (btn) exportTopic(btn, interactionTopicId);
  });
  const interactionEmpty = "No saved interaction checks yet. Every check is saved automatically.";
  document.getElementById("interaction-history").addEventListener("click", () =>
    showDrugTopics("interaction-topics", "interactions", interactionEmpty)
  );
  wireDrugTopicsPanel(
    "interaction-topics", "interactions", interactionEmpty,
    (topicId, topic) => {
      const turn = (topic.turns || [])[0];
      if (!turn) return;
      interactionTopicId = topicId;
      interactionDrugs = (topic.books || []).map((b) => b.title);
      renderInteractionChips();
      renderInteractionResult(interactionDrugs, turn.a);
      document.getElementById("interaction-result").scrollIntoView({ block: "start", behavior: "smooth" });
    },
    (deleted) => {
      if (interactionTopicId === deleted) {
        interactionTopicId = null;
        document.getElementById("interaction-result").hidden = true;
      }
    }
  );

  renderInteractionChips();
  renderLookupState();
  wireLexiPane();
  // The Lexicomp tab only exists for users while the cloud phone is
  // connected; otherwise it would be a tab that can only say "offline".
  api("/api/lexidrug/status")
    .then(({ enabled }) => {
      document.querySelector('#view-drugs .subtab[data-drugtab="lexi"]').hidden = !enabled;
    })
    .catch(() => {});
}


// ---------------------------------------------------------------------
// Ask tab: one question across several books + specialty libraries
// ---------------------------------------------------------------------
//
// A book's own "Ask questions using AI" panel answers from that one book.
// This tab is the other shape of the same need: tick any mix of your own
// indexed books and the admin-provided books of a specialty (Psychiatry,
// Cardiology, ...), and ask once across all of them. The ticks survive
// switching specialty, so books from two specialties plus your own shelf
// can be combined in one question.
//
// Shared books are answer sources only -- there is no way to open or
// download one from here, by design (see shared_library.py).

let askLib = null;            // last /api/ask/library response
let askSelected = new Set();  // ticked book ids (own and shared)
let askSpecialty = null;      // name of the specialty being shown
let askThread = [];           // [{q, a, sources}] -- sent back as history
let askTopicId = null;        // server-side id of the saved topic these turns belong to
let askBusy = false;
let askPollTimer = null;
let askIndexingOwn = new Set();  // own books whose index job this tab started

function askAllBooks() {
  if (!askLib) return [];
  return askLib.my_books.concat(...askLib.specialties.map((s) => s.books));
}

async function loadAskLibrary({ quiet = false } = {}) {
  try {
    askLib = await api("/api/ask/library");
  } catch (e) {
    if (!quiet) alertMsg(e.message);
    return;
  }
  if (askLib.max_upload_mb) maxUploadMb = askLib.max_upload_mb;
  // A tick on a book that has since been removed (or lost its index) would
  // make the next question fail, so it is dropped here.
  const askable = new Set(askAllBooks().filter((b) => b.qa_indexed).map((b) => b.book_id));
  askSelected.forEach((id) => { if (!askable.has(id)) askSelected.delete(id); });
  const names = askLib.specialties.map((s) => s.name);
  if (!names.includes(askSpecialty)) askSpecialty = names[0] || null;
  // A background refresh must not rebuild the list under a title being typed.
  if (!(quiet && askEditingId)) renderAskLibrary();

  // Keep refreshing while anything is still being indexed, so a book
  // becomes tickable by itself the moment it is ready.
  clearTimeout(askPollTimer);
  const pending = askLib.specialties.some((s) => s.books.some((b) => b.indexing)) || askIndexingOwn.size > 0;
  if (pending && !document.getElementById("view-ask").hidden) {
    askPollTimer = setTimeout(() => loadAskLibrary({ quiet: true }), 3000);
  }
}

function askRowHtml(b, { shared, label }) {
  const shown = escapeHtml(label || b.title);
  const pages = b.page_count ? `${b.page_count} p.` : "";
  const admin = shared && askLib.is_admin;
  const del = admin ? `<button class="mini-btn" data-del="${b.book_id}" title="Remove from library">${ic("trash-2")}</button>` : "";
  const edit = admin ? `<button class="mini-btn" data-rename="${b.book_id}" title="Edit title">${ic("pencil")}</button>` : "";
  const open = `<button class="mini-btn" data-open="${b.book_id}" title="Open the book">${ic("book-open")}</button>`;
  const cover = coverButtonHtml(b.book_id, shared);
  if (admin && askEditingId === b.book_id) {
    // Inline title editor (window.prompt is unreliable inside Telegram).
    return (
      `<div class="check-row">${cover}` +
      `<input class="rename-input" type="text" maxlength="200" value="${escapeHtml(b.title)}" data-rename-input="${b.book_id}" />` +
      `<button class="mini-btn" data-rename-save="${b.book_id}">Save</button>` +
      `<button class="mini-btn" data-rename-cancel="1">${ic("x")}</button></div>`
    );
  }
  if (b.qa_indexed) {
    const checked = askSelected.has(b.book_id) ? "checked" : "";
    return (
      `<label class="check-row"><input type="checkbox" data-id="${b.book_id}" ${checked} />${cover}` +
      `<span class="check-title">${shown}</span><span class="check-meta">${pages}</span>${open}${edit}${del}</label>`
    );
  }
  // Not answerable yet: say why (under the title, so a long progress line
  // can't squeeze the title into a one-letter column), and offer the one
  // thing that fixes it.
  let meta, action = "";
  if (b.indexing || askIndexingOwn.has(b.book_id)) {
    meta = `<span class="check-sub">${ic("hourglass")} ${escapeHtml(b.progress || "Preparing…")}</span>`;
  } else if (shared) {
    meta = b.index_error
      ? `<span class="check-sub is-error">${escapeHtml(b.index_error)}</span>`
      : `<span class="check-sub">Not ready yet</span>`;
    if (admin) action = `<button class="mini-btn" data-reindex="${b.book_id}">Retry</button>`;
  } else {
    meta = "";
    action = `<button class="mini-btn" data-index="${b.book_id}">Make searchable</button>`;
  }
  return (
    `<div class="check-row is-disabled"><input type="checkbox" disabled />${cover}` +
    `<span class="check-title">${shown}${meta}</span>${action}${open}${edit}${del}</div>`
  );
}

// The dropdown shows how many books are ticked inside each specialty, so
// ticks made in one are still visible after switching to another.
function renderAskSpecialtyOptions() {
  document.getElementById("ask-specialty").innerHTML = askLib.specialties
    .map((s) => {
      const ticked = s.books.filter((b) => askSelected.has(b.book_id)).length;
      const label = `${s.name} (${s.books.length})${ticked ? ` — ${ticked} ticked` : ""}`;
      return `<option value="${escapeHtml(s.name)}" ${s.name === askSpecialty ? "selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");
}

let askQuery = "";  // the picker's search box

// The rows of one list, with the volumes of a multi-volume book together
// under one title: its tick picks every ready volume, or tick one alone.
function askListHtml(books, { shared }) {
  const groups = new Map();
  const order = [];
  books.forEach((b) => {
    if (b.series && b.volume) {
      const key = b.series.toLowerCase();
      if (!groups.has(key)) { groups.set(key, []); order.push({ key }); }
      groups.get(key).push(b);
    } else {
      order.push({ book: b });
    }
  });
  return order
    .map((o) => {
      if (o.book) return askRowHtml(o.book, { shared });
      const vols = groups.get(o.key).slice().sort((a, b) => a.volume - b.volume);
      const ready = vols.filter((b) => b.qa_indexed).map((b) => b.book_id);
      const n = vols.length;
      const sub = `${n} volume${n === 1 ? "" : "s"}` + (ready.length < n ? ` · ${ready.length} ready` : "");
      const head =
        `<label class="check-row vol-head${ready.length ? "" : " is-disabled"}">` +
        `<input type="checkbox" data-group="${ready.join(",")}" ${ready.length ? "" : "disabled"} />` +
        `<span class="vol-icon">${ic("layers")}</span>` +
        `<span class="check-title">${escapeHtml(vols[0].series)}<span class="check-sub">${sub}</span></span></label>`;
      const rows = vols.map((b) => askRowHtml(b, { shared, label: `Volume ${b.volume}` })).join("");
      return `<div class="vol-group">${head}<div class="vol-children">${rows}</div></div>`;
    })
    .join("");
}

// A title's tick shows all (ticked), some (a dash) or none of its volumes.
function syncAskGroupTicks(root) {
  root.querySelectorAll('input[type="checkbox"][data-group]').forEach((box) => {
    const ids = box.dataset.group ? box.dataset.group.split(",") : [];
    const on = ids.filter((id) => askSelected.has(id)).length;
    box.checked = ids.length > 0 && on === ids.length;
    box.indeterminate = on > 0 && on < ids.length;
  });
}

function renderAskLibrary() {
  if (!askLib) return;
  const select = document.getElementById("ask-specialty");
  const sharedBox = document.getElementById("ask-shared-books");
  const mineBox = document.getElementById("ask-my-books");

  const words = bookQueryWords(askQuery);
  if (askLib.specialties.length) {
    select.hidden = false;
    select.disabled = words.length > 0;
    renderAskSpecialtyOptions();
    if (words.length) {
      // A search looks through every specialty, not just the one picked.
      const seen = new Set();
      const hits = askLib.specialties.flatMap((s) => s.books).filter((b) => {
        if (seen.has(b.book_id) || !(titleMatches(b.title, words) || (b.series && titleMatches(b.series, words)))) return false;
        seen.add(b.book_id);
        return true;
      });
      sharedBox.innerHTML = hits.length
        ? askListHtml(hits, { shared: true })
        : `<p class="muted">No specialty book matches “${escapeHtml(askQuery)}”.</p>`;
    } else {
      const spec = askLib.specialties.find((s) => s.name === askSpecialty);
      sharedBox.innerHTML = spec && spec.books.length
        ? askListHtml(spec.books, { shared: true })
        : `<p class="muted">No books in this specialty yet.</p>`;
    }
  } else {
    select.hidden = true;
    sharedBox.innerHTML = `<p class="muted">No specialty libraries yet.</p>`;
  }

  const mine = askLib.my_books.filter((b) => titleMatches(b.title, words) || (b.series && titleMatches(b.series, words)));
  mineBox.innerHTML = mine.length
    ? askListHtml(mine, { shared: false })
    : askLib.my_books.length
      ? `<p class="muted">None of your books matches “${escapeHtml(askQuery)}”.</p>`
      : `<p class="muted">No books on your shelf yet. Add one from the ${ic("library-big")} Shelf tab.</p>`;
  loadCoverThumbs(document.getElementById("view-ask"));
  syncAskGroupTicks(document.getElementById("view-ask"));

  const admin = document.getElementById("ask-admin");
  admin.hidden = !askLib.is_admin;
  if (askLib.is_admin) renderRecommended();
  if (askLib.is_admin) {
    document.getElementById("ask-admin-target").textContent = askSpecialty
      ? `Books you add go into: ${askSpecialty}`
      : "Add a specialty first, then add books to it.";
    ["ask-admin-upload", "ask-admin-publish", "ask-admin-del-spec"].forEach((id) => {
      document.getElementById(id).disabled = !askSpecialty;
    });
  }
  renderAskSelection();
}

function renderAskSpecActions() {
  const spec = askLib && askLib.specialties.find((x) => x.name === askSpecialty);
  const ready = spec ? spec.books.filter((b) => b.qa_indexed) : [];
  const btn = document.getElementById("ask-tick-all");
  btn.hidden = ready.length === 0 || bookQueryWords(askQuery).length > 0;  // ticks one specialty: not while searching
  btn.innerHTML = ready.length && ready.every((b) => askSelected.has(b.book_id)) ? `${ic("square")} Untick all` : `${ic("square-check")} Tick all`;
  document.getElementById("ask-suggest").hidden = !askLib;
}

function renderAskSelection() {
  renderAskSpecActions();
  const n = askSelected.size;
  const titles = askAllBooks().filter((b) => askSelected.has(b.book_id)).map((b) => b.title);
  document.getElementById("ask-res-list").innerHTML = n
    ? `<b>Answering from ${n} resource${n === 1 ? "" : "s"}:</b> ${titles.map(escapeHtml).join(" · ")}`
    : "No resources chosen yet.";
  const hint = document.getElementById("ask-selected");
  hint.hidden = n > 0;
  hint.textContent = "Tap “Choose resources” above to pick the books to answer from.";
  document.getElementById("ask-choose").classList.toggle("secondary", n > 0);
  document.getElementById("ask-send").disabled = n === 0 || askBusy;
  document.getElementById("ask-clear").hidden = n === 0;
  const done = document.getElementById("ask-pick-done");
  done.innerHTML = `${ic("circle-check")} Done${n ? ` — ${n} selected` : ""}`;
}

// The Ask tab is two pages: choosing resources, and the questions and
// answers themselves (with the chosen resources listed in small print).
function askShowPage(page) {
  const picking = page === "pick";
  document.getElementById("ask-pick").hidden = !picking;
  document.getElementById("ask-chat").hidden = picking;
  window.scrollTo(0, 0);
}

function askAnswerHtml(text, sources) {
  const body = richText(text);
  if (!sources || !sources.length) return body;
  const lines = sources
    .map(
      (s) =>
        `<span class="src-line" data-n="${s.n}"><span class="n">[${s.n}]</span>` +
        (s.url
          ? `<span>${ic("globe")} <strong>${escapeHtml(s.title)}</strong> <span class="muted">(${escapeHtml(hostOf(s.url))})</span> — ${escapeHtml(sourcePreview(s.text))}</span></span>`
          : `<span><strong>${escapeHtml(s.title)}</strong>, p.${s.page} — ${escapeHtml(sourcePreview(s.text))}</span></span>`)
    )
    .join("");
  return `${body}<span class="qa-sources">${lines}</span>`;
}

function renderAskThread() {
  const box = document.getElementById("ask-thread");
  box.innerHTML = askThread
    .map(
      (t, i) =>
        `<div class="qa-msg q" dir="auto">${escapeHtml(t.q)}</div>` +
        `<div class="qa-msg a" data-turn="${i}">${t.sources ? askAnswerHtml(t.a, t.sources) : escapeHtml(t.a)}` +
        (t.sources ? `<div class="ask-turn-tools"><button class="mini-btn" data-export-turn="${i}">${ic("file-text")} Save as PDF</button></div>` : "") +
        `</div>`
    )
    .join("");
  box.scrollTop = box.scrollHeight;
  const answered = askThread.some((t) => t.sources);
  document.getElementById("ask-topic-note").hidden = !answered;
  document.getElementById("ask-topic-pdf").hidden = !answered;
}

function askStartTopic() {
  askThread = [];
  askTopicId = null;
  renderAskThread();
}

// Answers go to the user's Telegram chat as a PDF: the in-app browser
// can't save files itself. One answer, or the whole topic.
function askExport(btn, turn) {
  return exportTopic(btn, askTopicId, turn);
}

async function exportTopic(btn, topicId, turn) {
  if (!topicId) return;
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    await api("/api/ask/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(turn === undefined ? { topic_id: topicId } : { topic_id: topicId, turn }),
    });
    btn.innerHTML = `${ic("circle-check")} Sent to your chat`;
    setTimeout(() => { btn.innerHTML = label; btn.disabled = false; }, 2500);
  } catch (e) {
    btn.innerHTML = label;
    btn.disabled = false;
    alertMsg(e.message);
  }
}

async function askShowTopics() {
  const box = document.getElementById("ask-topics");
  const list = document.getElementById("ask-topics-list");
  box.hidden = false;
  list.innerHTML = `<p class="muted">Loading…</p>`;
  let topics;
  try {
    ({ topics } = await api("/api/ask/topics"));
  } catch (e) {
    list.innerHTML = `<p class="muted">${escapeHtml(e.message)}</p>`;
    return;
  }
  list.innerHTML = topics.length
    ? topics
        .map((t) => {
          const when = new Date(t.updated * 1000).toLocaleDateString();
          return (
            `<div class="topic-row"><button class="topic-main" data-topic="${t.topic_id}">` +
            `<span class="topic-title">${escapeHtml(t.title)}</span>` +
            `<span class="topic-meta">${t.turns} answer${t.turns === 1 ? "" : "s"} · ${when} · ${escapeHtml(t.books.join(", "))}</span>` +
            `</button><button class="mini-btn" data-del-topic="${t.topic_id}" title="Delete">${ic("trash-2")}</button></div>`
          );
        })
        .join("")
    : `<p class="muted">No saved topics yet. Every question you ask here is saved automatically.</p>`;
  box.scrollIntoView({ block: "start", behavior: "smooth" });
}

// Reopen a saved topic: its answers come back, its books are ticked again
// (those still available), and new questions continue it.
async function askOpenTopic(topicId) {
  let topic;
  try {
    topic = await api(`/api/ask/topics/${topicId}`);
  } catch (e) {
    return alertMsg(e.message);
  }
  askTopicId = topicId;
  askThread = (topic.turns || []).map((t) => ({ q: t.q, a: t.a, sources: t.sources || [] }));
  const available = new Set(askAllBooks().filter((b) => b.qa_indexed).map((b) => b.book_id));
  askSelected.clear();
  (topic.books || []).forEach((b) => { if (available.has(b.book_id)) askSelected.add(b.book_id); });
  document.getElementById("ask-topics").hidden = true;
  renderAskLibrary();
  askShowPage("chat");
  renderAskThread();
  document.getElementById("ask-thread").scrollIntoView({ block: "start", behavior: "smooth" });
}

async function askSend() {
  if (askBusy) return;
  const input = document.getElementById("ask-question");
  const question = input.value.trim();
  if (!askSelected.size) return alertMsg("Tick at least one book first.");
  if (!question) return;

  askBusy = true;
  const n = askSelected.size;
  // The answer belongs to this thread and this turn, even if the user opens
  // another topic or starts a new one while it is on its way.
  const thread = askThread;
  const turn = { q: question, a: `…searching ${n} book${n === 1 ? "" : "s"}…` };
  const history = thread.map((t) => ({ question: t.q, answer: t.a }));
  thread.push(turn);
  input.value = "";
  renderAskThread();
  renderAskSelection();
  try {
    const { answer, sources, skipped } = await api("/api/ask/multi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        book_ids: Array.from(askSelected),
        question,
        topic_id: askTopicId || (askTopicId = genId()),
        history,
      }),
    });
    const note = skipped && skipped.length ? `\n\n(Couldn't search: ${skipped.join(", ")}.)` : "";
    Object.assign(turn, { a: answer + note, sources: sources || [] });
  } catch (e) {
    const i = thread.indexOf(turn);
    if (i >= 0) thread.splice(i, 1);  // never replay an error as history
    if (thread === askThread && !input.value) input.value = question;
    alertMsg(e.message);
    if (e.status === 404 || e.status === 409) loadAskLibrary({ quiet: true });
  } finally {
    askBusy = false;
    renderAskThread();
    renderAskSelection();
  }
}

// Index one of the user's own books from here, so they don't have to leave
// the tab, open the book, and come back just to be able to tick it.
async function askIndexOwn(bookId) {
  askIndexingOwn.add(bookId);
  renderAskLibrary();
  const finish = (err) => {
    askIndexingOwn.delete(bookId);
    if (err) alertMsg(err);
    loadAskLibrary({ quiet: true });
  };
  try {
    await api(`/api/books/${bookId}/index`, { method: "POST" });
  } catch (e) {
    return finish(e.message);
  }
  pollJob(bookId, "index", {
    screenBound: false,  // runs from the Ask tab, whatever book was last opened
    onProgress: (p) => {
      const b = askLib && askLib.my_books.find((x) => x.book_id === bookId);
      // Not while the admin is typing a new title: a redraw would wipe it.
      if (b) { b.progress = p; if (!askEditingId) renderAskLibrary(); }
    },
    onDone: () => { askSelected.add(bookId); finish(); },
    onError: (msg) => finish(msg),
  });
}

// --- admin: manage the specialty libraries ----------------------------

// window.confirm is unreliable inside Telegram's WebView (it can silently
// return false), so Telegram's own dialog is used when there is one.
function askConfirm(message) {
  return confirmMsg(message);
}

async function askAdminCall(path, opts, { confirmText } = {}) {
  if (confirmText && !(await askConfirm(confirmText))) return;
  try {
    const data = await api(path, opts);
    if (data && data.name) askSpecialty = data.name;
  } catch (e) {
    alertMsg(e.message);
  }
  loadAskLibrary({ quiet: true });
}

// Into the specialty library (admin): the same title/volumes sheet as the
// shelf, then the files go up one after another.
async function askAdminUpload(files, { specialty = askSpecialty, title = "", volumes = [], multi = false } = {}) {
  if (!specialty) return;
  const items = await askVolumes(files, { title, head: `Add a book to ${specialty}`, volumes, multi });
  if (!items) return;
  const wrap = document.getElementById("ask-admin-progress");
  const bar = document.getElementById("ask-admin-bar");
  const status = document.getElementById("ask-admin-status");
  const uploadBtn = document.getElementById("ask-admin-upload");
  wrap.hidden = false;
  uploadBtn.disabled = true;
  wrap.scrollIntoView({ block: "nearest", behavior: "smooth" });
  let error = null;
  for (let i = 0; i < items.length && !error; i++) {
    const it = items[i];
    const label = items.length > 1 ? `${it.label} (${i + 1} of ${items.length}): ` : "";
    bar.style.width = "0%";
    status.textContent = `${label}Uploading "${it.title}" to ${specialty}…`;
    try {
      await chunkedUpload(it.file, {
        target: "shared",
        specialty,
        title: it.title,
        series: it.series,
        volume: it.volume,
        onProgress: (f, text) => {
          bar.style.width = Math.round(f * 100) + "%";
          status.textContent = label + text;
        },
      });
    } catch (e) {
      const msg = e.message === "Network error" ? "Upload failed -- check your connection and try again." : e.message;
      error = items.length > 1 ? `${it.label}: ${msg}` : msg;
    }
    loadAskLibrary({ quiet: true });  // each volume shows up (and starts indexing) as soon as it's in
  }
  wrap.hidden = true;
  uploadBtn.disabled = false;
  if (error) alertMsg(error);
  loadAskLibrary({ quiet: true });
}

// --- recommended books (admin): the standard references per specialty ---

let recSpecialty = null;
let recPending = null;  // {specialty, title} waiting for its PDF

// What the specialty already has of a recommended book: a single book, or
// the volumes of a multi-volume one (more volumes can still be added).
function recBookInLibrary(specialty, rec) {
  const spec = (askLib.specialties || []).find((s) => s.name.toLowerCase() === specialty.toLowerCase());
  if (!spec) return null;
  const words = rec.match.map((w) => w.toLowerCase());
  const hits = spec.books.filter((b) => {
    const t = (b.series || b.title || "").toLowerCase().replace(/[_-]+/g, " ");
    return words.every((w) => t.includes(w));
  });
  if (!hits.length) return null;
  const vols = hits.filter((b) => b.series && b.volume);
  if (!vols.length) return { single: true };
  return { series: vols[0].series, volumes: vols.map((b) => b.volume).sort((x, y) => x - y) };
}

function renderRecommended() {
  const rec = askLib && askLib.recommended;
  const box = document.getElementById("rec-list");
  const select = document.getElementById("rec-specialty");
  if (!rec) return;
  const names = Object.keys(rec);
  if (!names.includes(recSpecialty)) {
    const current = names.find((n) => askSpecialty && n.toLowerCase() === askSpecialty.toLowerCase());
    recSpecialty = current || names[0];
  }
  select.innerHTML = names
    .map((n) => {
      const have = rec[n].filter((r) => recBookInLibrary(n, r)).length;
      return `<option value="${escapeHtml(n)}" ${n === recSpecialty ? "selected" : ""}>${escapeHtml(n)} (${have}/${rec[n].length} added)</option>`;
    })
    .join("");
  box.innerHTML = rec[recSpecialty]
    .map((r, i) => {
      const have = recBookInLibrary(recSpecialty, r);
      let status;
      if (!have) {
        status = `<button class="mini-btn" data-rec="${i}">${ic("upload")} Add PDF</button>`;
      } else if (have.single) {
        status = `<span class="rec-have">${ic("circle-check")} In library</span>`;
      } else {
        // A multi-volume book: which volumes are in, and a button for the rest.
        const v = have.volumes;
        status =
          `<span class="rec-have">${ic("layers")} Vol. ${v.join(", ")}</span>` +
          `<button class="mini-btn" data-rec="${i}">${ic("plus")} Volume</button>`;
      }
      return `<div class="check-row rec-row"><span class="rec-num">${i + 1}</span>` +
        `<span class="check-title"><strong>${escapeHtml(r.title)}</strong><br><span class="muted">${escapeHtml(r.by)}</span></span>` +
        status + `</div>`;
    })
    .join("");
}

async function recUpload(files) {
  const pending = recPending;
  recPending = null;
  if (!pending) return;
  // The specialty is created first if the library doesn't have it yet.
  const existing = (askLib.specialties || []).find((s) => s.name.toLowerCase() === pending.specialty.toLowerCase());
  let specialty = existing && existing.name;
  if (!specialty) {
    try {
      const data = await api("/api/shared/specialties", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: pending.specialty }),
      });
      specialty = (data && data.name) || pending.specialty;
    } catch (e) {
      return alertMsg(e.message);
    }
  }
  askSpecialty = specialty;
  askAdminUpload(files, { specialty, title: pending.title, volumes: pending.volumes, multi: pending.multi });
}

function renderAskAdminShelf() {
  const box = document.getElementById("ask-admin-shelf");
  if (!askLib.my_books.length) {
    box.innerHTML = `<p class="muted">Your own shelf is empty.</p>`;
    return;
  }
  box.innerHTML = askLib.my_books
    .map(
      (b) =>
        `<div class="check-row"><span class="check-title">${escapeHtml(b.title)}</span>` +
        `<button class="mini-btn" data-publish="${b.book_id}">Add to ${escapeHtml(askSpecialty)}</button></div>`
    )
    .join("");
}

// --- book covers --------------------------------------------------------
// Page 1 of each PDF, rendered by the server. Fetched with the Telegram
// sign-in header (an <img src> can't send it) and kept as object URLs, so
// the frequent list re-renders don't fetch the same image again.

const coverUrls = new Map();   // "s:<id>" / "m:<id>" -> object URL, or null if it failed
const coverQueue = [];
let coverActive = 0;
const COVER_PARALLEL = 4;

function coverKey(id, shared) { return (shared ? "s:" : "m:") + id; }

function coverButtonHtml(id, shared) {
  const book = askAllBooks().find((b) => b.book_id === id);
  const title = book ? escapeHtml(book.title) : "";
  const url = coverUrls.get(coverKey(id, shared));
  const inner = url ? `<img src="${url}" alt="" />` : `<span class="cover-ph">${ic("book", " cover-ic")}</span>`;
  return `<button type="button" class="cover-btn" data-cover="${id}" data-shared="${shared ? 1 : 0}" data-title="${title}" title="Show cover">${inner}</button>`;
}

function coverPath(id, shared, size) {
  return `${shared ? "/api/shared/books" : "/api/books"}/${id}/cover?size=${size}`;
}

async function fetchImage(path) {
  const res = await fetch(path, { headers: { "X-Telegram-Init-Data": INIT_DATA } });
  if (!res.ok) throw new Error(`cover ${res.status}`);
  return URL.createObjectURL(await res.blob());
}

function setCoverImage(el, url) {
  if (el.classList.contains("book-cover")) {
    const dot = el.querySelector(".status-dot");
    el.classList.add("has-img");
    el.innerHTML = `<img src="${url}" alt="" />`;
    if (dot) el.appendChild(dot);
  } else {
    el.innerHTML = `<img src="${url}" alt="" />`;
  }
}

function loadCoverThumbs(root) {
  root.querySelectorAll("[data-cover]").forEach((btn) => {
    const cached = coverUrls.get(coverKey(btn.dataset.cover, btn.dataset.shared === "1"));
    if (cached) setCoverImage(btn, cached);
  });
  root.querySelectorAll("[data-cover]").forEach((btn) => {
    const key = coverKey(btn.dataset.cover, btn.dataset.shared === "1");
    if (coverUrls.has(key) || coverQueue.some((q) => q.key === key)) return;
    coverQueue.push({ key, id: btn.dataset.cover, shared: btn.dataset.shared === "1" });
  });
  pumpCovers();
}

function pumpCovers() {
  while (coverActive < COVER_PARALLEL && coverQueue.length) {
    const job = coverQueue.shift();
    coverActive++;
    fetchImage(coverPath(job.id, job.shared, "thumb"))
      .then((url) => {
        coverUrls.set(job.key, url);
        document.querySelectorAll(`[data-cover="${job.id}"][data-shared="${job.shared ? 1 : 0}"]`)
          .forEach((b) => setCoverImage(b, url));
      })
      .catch(() => coverUrls.set(job.key, null))  // keep the 📘 placeholder; don't retry every render
      .finally(() => { coverActive--; pumpCovers(); });
  }
}

async function showCover(id, shared, title) {
  const box = document.getElementById("cover-viewer");
  const img = document.getElementById("cover-viewer-img");
  document.getElementById("cover-viewer-title").textContent = title || "";
  img.hidden = true;
  document.getElementById("cover-viewer-status").textContent = "Loading cover…";
  box.hidden = false;
  try {
    const url = await fetchImage(coverPath(id, shared, "full"));
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    img.dataset.url = url;
    img.src = url;
    img.hidden = false;
    document.getElementById("cover-viewer-status").textContent = "";
  } catch (_) {
    document.getElementById("cover-viewer-status").textContent = "This book's cover couldn't be shown.";
  }
}

let askEditingId = null;  // shared book whose title is being edited inline (admin)

function askSaveRename(id) {
  const input = document.querySelector(`input[data-rename-input="${id}"]`);
  const title = input ? input.value.trim() : "";
  const book = askAllBooks().find((b) => b.book_id === id);
  askEditingId = null;
  if (!title || (book && title === book.title)) return renderAskLibrary();
  askAdminCall(`/api/shared/books/${id}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

// Tick every searchable book of the specialty on screen (up to the limit).
function askTickAll() {
  const spec = askLib && askLib.specialties.find((x) => x.name === askSpecialty);
  if (!spec) return;
  const ready = spec.books.filter((b) => b.qa_indexed).map((b) => b.book_id);
  const allTicked = ready.length && ready.every((id) => askSelected.has(id));
  if (allTicked) {
    ready.forEach((id) => askSelected.delete(id));  // second tap unticks them again
  } else {
    for (const id of ready) {
      if (askSelected.size >= askLib.max_books) {
        alertMsg(`Ticked as many as fit -- up to ${askLib.max_books} books can be asked at once.`);
        break;
      }
      askSelected.add(id);
    }
  }
  renderAskLibrary();
}

async function askSendSuggestion() {
  const title = document.getElementById("ask-suggest-title").value.trim();
  const note = document.getElementById("ask-suggest-note").value.trim();
  if (!title) return alertMsg("Type the book's title (and author, if you know it).");
  const btn = document.getElementById("ask-suggest-send");
  btn.disabled = true;
  try {
    await api("/api/suggest-book", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ specialty: askSpecialty, title, note }),
    });
    document.getElementById("ask-suggest-title").value = "";
    document.getElementById("ask-suggest-note").value = "";
    document.getElementById("ask-suggest-form").hidden = true;
    alertMsg("Thanks! Your suggestion was sent to the admin.");
  } catch (e) {
    alertMsg(e.message);
  } finally {
    btn.disabled = false;
  }
}

function wireAskTab() {
  const view = document.getElementById("view-ask");

  document.getElementById("ask-specialty").addEventListener("change", (e) => {
    askSpecialty = e.target.value;
    document.getElementById("ask-admin-shelf").hidden = true;
    renderAskLibrary();
  });

  // One delegated listener for both checklists and every row button.
  view.addEventListener("change", (e) => {
    const group = e.target.closest('.check-row input[type="checkbox"][data-group]');
    if (group) {
      // The title of a multi-volume book: every ready volume, or none.
      const ids = group.dataset.group ? group.dataset.group.split(",") : [];
      if (group.checked) {
        const adding = ids.filter((id) => !askSelected.has(id));
        if (askLib && askSelected.size + adding.length > askLib.max_books) {
          syncAskGroupTicks(view);
          return alertMsg(`That's more than ${askLib.max_books} books at once. Untick a few, or tick single volumes.`);
        }
        adding.forEach((id) => askSelected.add(id));
      } else {
        ids.forEach((id) => askSelected.delete(id));
      }
      view.querySelectorAll('.check-row input[type="checkbox"][data-id]').forEach((b) => {
        if (ids.includes(b.dataset.id)) b.checked = askSelected.has(b.dataset.id);
      });
      syncAskGroupTicks(view);
      renderAskSpecialtyOptions();
      renderAskSelection();
      return;
    }
    const box = e.target.closest('.check-row input[type="checkbox"][data-id]');
    if (!box) return;
    if (box.checked) {
      if (askLib && askSelected.size >= askLib.max_books) {
        box.checked = false;
        return alertMsg(`You can ask up to ${askLib.max_books} books at once. Untick one first.`);
      }
      askSelected.add(box.dataset.id);
    } else {
      askSelected.delete(box.dataset.id);
    }
    // Only the counters are refreshed: rebuilding the lists on every tick
    // would replace the checkbox under the user's finger.
    syncAskGroupTicks(view);
    renderAskSpecialtyOptions();
    renderAskSelection();
  });
  view.addEventListener("click", (e) => {
    const coverBtn = e.target.closest("button.cover-btn");
    if (coverBtn) {
      // Inside a <label>: without this the tap would also tick the book.
      e.preventDefault();
      return showCover(coverBtn.dataset.cover, coverBtn.dataset.shared === "1", coverBtn.dataset.title);
    }
    const cite = e.target.closest("#ask-thread .cite-marker, #ask-thread .src-line");
    if (cite) {
      const turn = askThread[parseInt(cite.closest(".qa-msg").dataset.turn, 10)];
      const src = turn && turn.sources && turn.sources.find((x) => String(x.n) === cite.dataset.n);
      if (src && src.url) openExternal(src.url);
      else if (src) openSource(src.book_id, src.page, src.text, src.n);
      return;
    }
    const topicBtn = e.target.closest("button.topic-main");
    if (topicBtn) return askOpenTopic(topicBtn.dataset.topic);
    const btn = e.target.closest("button.mini-btn");
    if (!btn) return;
    e.preventDefault();
    if (btn.dataset.exportTurn !== undefined) return askExport(btn, parseInt(btn.dataset.exportTurn, 10));
    if (btn.dataset.delTopic) {
      return api(`/api/ask/topics/${btn.dataset.delTopic}`, { method: "DELETE" })
        .then(() => {
          if (askTopicId === btn.dataset.delTopic) askStartTopic();
          askShowTopics();
        })
        .catch((err) => alertMsg(err.message));
    }
    if (btn.dataset.open) return openBook(btn.dataset.open);
    if (btn.id === "ask-tick-all") return askTickAll();
    if (btn.id === "ask-suggest") {
      const form = document.getElementById("ask-suggest-form");
      form.hidden = !form.hidden;
      if (!form.hidden) document.getElementById("ask-suggest-title").focus();
      return;
    }
    if (btn.dataset.rename) {
      askEditingId = btn.dataset.rename;
      renderAskLibrary();
      const input = document.querySelector(`input[data-rename-input="${askEditingId}"]`);
      if (input) { input.focus(); input.select(); }
      return;
    }
    if (btn.dataset.renameSave) return askSaveRename(btn.dataset.renameSave);
    if (btn.dataset.renameCancel) { askEditingId = null; return renderAskLibrary(); }
    if (btn.dataset.index) return askIndexOwn(btn.dataset.index);
    if (btn.dataset.reindex) {
      return askAdminCall(`/api/shared/books/${btn.dataset.reindex}/index`, { method: "POST" });
    }
    if (btn.dataset.del) {
      return askAdminCall(`/api/shared/books/${btn.dataset.del}`, { method: "DELETE" }, {
        confirmText: "Remove this book from the specialty library for everyone?",
      });
    }
    if (btn.dataset.publish) {
      btn.disabled = true;
      btn.textContent = "Adding…";
      document.getElementById("ask-admin-shelf").hidden = true;
      return askAdminCall("/api/shared/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ book_id: btn.dataset.publish, specialty: askSpecialty }),
      });
    }
  });

  document.getElementById("ask-send").addEventListener("click", askSend);
  document.getElementById("ask-suggest-send").addEventListener("click", askSendSuggestion);
  document.getElementById("ask-suggest-cancel").addEventListener("click", () => {
    document.getElementById("ask-suggest-form").hidden = true;
  });
  document.getElementById("reader-source-close").addEventListener("click", () => {
    readerSource = null;
    document.getElementById("reader-source").hidden = true;
    document.querySelectorAll("#reader-text-layer span.src-hl").forEach((x) => x.classList.remove("src-hl"));
  });
  view.addEventListener("keydown", (e) => {
    const input = e.target.closest("input[data-rename-input]");
    if (!input) return;
    if (e.key === "Enter") { e.preventDefault(); askSaveRename(input.dataset.renameInput); }
    if (e.key === "Escape") { askEditingId = null; renderAskLibrary(); }
  });
  const viewer = document.getElementById("cover-viewer");
  viewer.addEventListener("click", () => { viewer.hidden = true; });
  document.getElementById("ask-new").addEventListener("click", () => {
    askStartTopic();
    document.getElementById("ask-question").focus();
  });
  document.getElementById("ask-history").addEventListener("click", askShowTopics);
  document.getElementById("ask-topics-close").addEventListener("click", () => {
    document.getElementById("ask-topics").hidden = true;
  });
  document.getElementById("ask-topic-pdf").addEventListener("click", (e) => askExport(e.currentTarget));
  // The question box grows with what's typed (up to 40% of the screen).
  autoGrowComposer("ask-question", askSend);
  autoGrowComposer("lookup-question", askLookup);
  document.getElementById("ask-clear").addEventListener("click", () => {
    askSelected.clear();
    renderAskLibrary();
  });
  document.getElementById("ask-choose").addEventListener("click", () => askShowPage("pick"));
  document.getElementById("ask-pick-done").addEventListener("click", () => {
    askShowPage("chat");
    if (askSelected.size) document.getElementById("ask-question").focus();
  });

  const addSpec = () => {
    const input = document.getElementById("ask-admin-new-spec");
    const name = input.value.trim();
    if (!name) return;
    input.value = "";
    askAdminCall("/api/shared/specialties", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  };
  document.getElementById("ask-admin-add-spec").addEventListener("click", addSpec);
  document.getElementById("ask-admin-new-spec").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addSpec(); }
  });
  document.getElementById("ask-admin-del-spec").addEventListener("click", () => {
    if (!askSpecialty) return;
    askAdminCall("/api/shared/specialties/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: askSpecialty }),
    }, { confirmText: `Remove the "${askSpecialty}" specialty? (Only works once its books are removed.)` });
  });
  document.getElementById("ask-admin-upload").addEventListener("click", () => {
    document.getElementById("ask-admin-file").click();
  });
  document.getElementById("ask-admin-file").addEventListener("change", (e) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (files.length) askAdminUpload(files);
  });
  document.getElementById("rec-specialty").addEventListener("change", (e) => {
    recSpecialty = e.target.value;
    renderRecommended();
  });
  document.getElementById("rec-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-rec]");
    if (!b) return;
    const r = askLib.recommended[recSpecialty][parseInt(b.dataset.rec, 10)];
    const have = recBookInLibrary(recSpecialty, r);
    recPending = have && have.series
      ? { specialty: recSpecialty, title: have.series, volumes: have.volumes, multi: true }
      : { specialty: recSpecialty, title: r.title };
    document.getElementById("rec-file").click();
  });
  document.getElementById("rec-file").addEventListener("change", (e) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (files.length) recUpload(files);
    else recPending = null;
  });
  document.getElementById("ask-admin-publish").addEventListener("click", () => {
    const box = document.getElementById("ask-admin-shelf");
    box.hidden = !box.hidden;
    if (!box.hidden) renderAskAdminShelf();
  });

  renderAskSelection();
}

// ---------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------

(async () => {
  if (!INIT_DATA) {
    INIT_DATA = await waitForInitData();
  }
  wireDrugsTab();
  wireAskTab();
  wireWebSources();
  setTimeout(() => { if (!me) loadMe({ prompt: true }); }, 0);  // if the sign-in data arrived late
  document.querySelectorAll("#view-shelf .subtab").forEach((t) =>
    t.addEventListener("click", () => setShelfTab(t.dataset.shelftab))
  );
  document.getElementById("shelf-specialty").addEventListener("change", (e) => {
    shelfSpecialty = e.target.value;
    renderSpecialtyShelf();
  });
  // "?tab=ask" is how the chat's "Ask several books" button lands here.
  if (new URLSearchParams(window.location.search).get("tab") === "ask") {
    showView("view-ask");
    loadAskLibrary();
  } else {
    syncTabBar("view-shelf");
    loadShelf();
  }
})();


// ---------------------------------------------------------------------
// 🌐 Web sources (admin): log in to a website in the bot's own browser,
// then tell the bot how to search it. The page is a live picture of the
// server's browser; a tap on it is a click at the same spot.
// ---------------------------------------------------------------------

let webSites = [];
let webSitesLoaded = false;
let webOpenSettings = null;   // site_id whose settings form is open
let webFrame = null;          // last frame: {url, width, height, steps, site_id}
let webBusy = false;
let webRefreshTimer = null;

function openExternal(url) {
  let u;
  try { u = new URL(url); } catch (_) { return; }
  if (u.protocol !== "https:" && u.protocol !== "http:") return;
  try {
    if (tg && tg.openLink) return tg.openLink(u.href);
  } catch (_) {}
  window.open(u.href, "_blank", "noopener");
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch (_) { return ""; }
}

async function loadWebSites() {
  webSitesLoaded = true;
  try {
    ({ sites: webSites } = await api("/api/admin/web/sites"));
  } catch (e) {
    webSitesLoaded = false;
    return;
  }
  renderWebSites();
  loadDrugWebsites();  // a site switched on or off changes what Drug LookUp can search
}

function webStatusLine(s) {
  const parts = [];
  parts.push(s.has_session ? `${ic("circle-check")} Login saved` : "No login saved");
  if (s.last_login) {
    const when = new Date(s.last_login * 1000).toLocaleString();
    parts.push(s.last_login_ok === false ? `<span class="bad">${ic("triangle-alert")} Needs you to log in again (${when}): ${escapeHtml(s.last_error || "")}</span>` : `logged in ${when}`);
  }
  parts.push(s.enabled ? `${ic("circle", " ic-on")} Searched in Drug LookUp` : `${ic("circle")} Not searched in Drug LookUp`);
  return parts.join(" · ");
}

function renderWebSites() {
  const box = document.getElementById("web-sites");
  if (!webSites.length) {
    box.innerHTML = `<p class="muted">No websites yet. Type an address above and tap Open.</p>`;
    return;
  }
  box.innerHTML = webSites
    .map((s) => {
      const form =
        webOpenSettings === s.site_id
          ? `<div class="web-form" data-form="${s.site_id}">
              <label>Name<input class="drug-input" data-f="name" maxlength="80" value="${escapeHtml(s.name || "")}" /></label>
              <label>Search address <span class="hint">— the page that shows search results, with {query} where the words go. Search the site once in the browser and copy the address it lands on.</span>
                <input class="drug-input" data-f="search_url" placeholder="https://pubmed.ncbi.nlm.nih.gov/?term={query}" value="${escapeHtml(s.search_url)}" /></label>
              <label>Result links contain <span class="hint">— text (or a regular expression) found in the address of each result to read. Empty = read the results page itself.</span>
                <input class="drug-input" data-f="link_pattern" placeholder="pubmed.ncbi.nlm.nih.gov/\\d+" value="${escapeHtml(s.link_pattern)}" /></label>
              <label>Results to read
                <select class="ask-select" data-f="max_results">${[1, 2, 3, 4, 5]
                  .map((n) => `<option ${n === s.max_results ? "selected" : ""}>${n}</option>`)
                  .join("")}</select></label>
              <label class="web-check"><input type="checkbox" data-f="enabled" ${s.enabled ? "checked" : ""} /> Search it in Drug LookUp</label>
              <button class="btn" data-web-save="${s.site_id}">Save settings</button>
              <div class="drug-input-row"><input class="drug-input" data-f="test" placeholder="Test search, e.g. lithium bipolar" />
                <button class="btn secondary" data-web-test="${s.site_id}">Test</button></div>
              <div data-test-out="${s.site_id}"></div>
            </div>`
          : "";
      return (
        `<div class="web-site"><div class="web-site-head"><strong>${escapeHtml(s.name || "")}</strong>` +
        `<span class="muted">${escapeHtml(new URL(s.start_url).hostname)}</span></div>` +
        `<div class="web-site-status">${webStatusLine(s)}</div>` +
        `<div class="web-site-actions">` +
        `<button class="mini-btn" data-web-open="${s.site_id}">${ic("key-round")} Open &amp; log in</button>` +
        `<button class="mini-btn" data-web-settings="${s.site_id}">${ic("settings")} Where to search</button>` +
        `<button class="mini-btn" data-web-del="${s.site_id}">${ic("trash-2")}</button></div>${form}</div>`
      );
    })
    .join("");
}

function webFormValues(siteId) {
  const form = document.querySelector(`[data-form="${siteId}"]`);
  const val = (f) => form.querySelector(`[data-f="${f}"]`);
  return {
    name: val("name").value.trim(),
    search_url: val("search_url").value.trim(),
    link_pattern: val("link_pattern").value.trim(),
    max_results: parseInt(val("max_results").value, 10),
    enabled: val("enabled").checked,
  };
}

async function webSiteClick(e) {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.webOpen) return webOpen(null, b.dataset.webOpen);
  if (b.dataset.webSettings) {
    webOpenSettings = webOpenSettings === b.dataset.webSettings ? null : b.dataset.webSettings;
    return renderWebSites();
  }
  const id = b.dataset.webSave || b.dataset.webTest || b.dataset.webRelogin || b.dataset.webDel;
  if (!id) return;
  const label = b.innerHTML;
  b.disabled = true;
  try {
    if (b.dataset.webSave) {
      await api(`/api/admin/web/sites/${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(webFormValues(id)),
      });
      webOpenSettings = null;
      await loadWebSites();
      loadAskLibrary({ quiet: true });  // the source may have just appeared under Websites
    } else if (b.dataset.webTest) {
      const out = document.querySelector(`[data-test-out="${id}"]`);
      const query = document.querySelector(`[data-form="${id}"] [data-f="test"]`).value.trim();
      b.textContent = "Searching…";
      out.innerHTML = "";
      const { results } = await api(`/api/admin/web/sites/${id}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      out.innerHTML = results.length
        ? results
            .map(
              (r) =>
                `<div class="web-test-result"><strong>${escapeHtml(r.title || r.url)}</strong> (${r.chars} characters)<br>` +
                `<span class="muted">${escapeHtml(r.url)}</span><br>${escapeHtml(r.preview)}…</div>`
            )
            .join("")
        : `<p class="muted">Nothing found. Check the search address and the result-link text.</p>`;
    } else if (b.dataset.webRelogin) {
      b.textContent = "Logging in…";
      await api(`/api/admin/web/sites/${id}/relogin`, { method: "POST" });
      await loadWebSites();
      alertMsg("Logged in again and saved.");
    } else if (b.dataset.webDel) {
      const ok = await confirmMsg("Delete this website and its saved login?");
      if (!ok) return;
      await api(`/api/admin/web/sites/${id}`, { method: "DELETE" });
      await loadWebSites();
      loadAskLibrary({ quiet: true });
    }
  } catch (err) {
    alertMsg(err.message);
    if (b.dataset.webRelogin) loadWebSites();
  } finally {
    b.disabled = false;
    if (document.body.contains(b)) b.innerHTML = label;
  }
}

// --- the browser view -------------------------------------------------

function webSetBusy(on) {
  webBusy = on;
  document.getElementById("web-busy").hidden = !on;
}

function webShow(frame) {
  webFrame = frame;
  const shot = document.getElementById("web-shot");
  shot.src = `data:image/jpeg;base64,${frame.image}`;
  shot.style.transform = "";  // a swipe's preview offset, now replaced by the real picture
  webSetFocus(frame.focus || {});
  const url = document.getElementById("web-url");
  if (document.activeElement !== url) url.value = frame.url;
  renderWebTabs(frame.tabs || []);
  const notice = document.getElementById("web-notice");
  notice.hidden = !frame.notice;
  notice.textContent = frame.notice || "";
}

// A ring where the finger landed, so a tap visibly registered.
function webTapMark(clientX, clientY) {
  const screen = document.querySelector("#view-web .web-screen");
  const r = screen.getBoundingClientRect();
  const dot = document.createElement("span");
  dot.className = "web-tap";
  dot.style.left = `${clientX - r.left}px`;
  dot.style.top = `${clientY - r.top}px`;
  screen.appendChild(dot);
  setTimeout(() => dot.remove(), 650);
}

// The browser's tabs: shown once a site opens a second one. A new tab never
// takes over the view by itself; it gets a dot until the admin opens it.
function renderWebTabs(tabs) {
  const bar = document.getElementById("web-tabs");
  bar.hidden = tabs.length < 2;
  bar.replaceChildren(...tabs.map((t) => {
    const tab = document.createElement("div");
    tab.className = "web-tab" + (t.active ? " is-active" : "") + (t.new ? " is-new" : "");
    const open = document.createElement("button");
    open.className = "web-tab-open";
    open.dataset.tab = t.i;
    open.title = t.title;
    open.textContent = t.title;
    const close = document.createElement("button");
    close.className = "web-tab-close";
    close.dataset.closeTab = t.i;
    close.title = "Close this tab";
    close.setAttribute("aria-label", `Close ${t.title}`);
    close.innerHTML = ic("x");
    tab.append(open, close);
    return tab;
  }));
  const active = bar.querySelector(".is-active");
  if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
}

async function webCall(path, body) {
  if (webBusy) return;
  webSetBusy(true);
  clearTimeout(webRefreshTimer);
  try {
    const frame = await api(path, body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    webShow(frame);
    // Pages often keep drawing after they report "loaded": take one more
    // picture a moment later.
    webRefreshTimer = setTimeout(webRefresh, 1800);
  } catch (e) {
    alertMsg(e.message);
  } finally {
    webSetBusy(false);
  }
}

// A quiet second picture: no busy overlay, and dropped if an action has
// started meanwhile (that action brings its own picture).
async function webRefresh() {
  clearTimeout(webRefreshTimer);
  if (document.getElementById("view-web").hidden || document.visibilityState !== "visible") return;
  if (!webBusy && !webDrag && !webTypeTimer && !webTyping) {
    try {
      const frame = await api("/api/admin/web/browser/frame");
      if (!webBusy && !webDrag) webShow(frame);
    } catch (_) { /* the next action will show the state */ }
  }
  // Keep the picture live: pages load, pop-ups open, timers tick.
  if (!document.getElementById("view-web").hidden) webRefreshTimer = setTimeout(webRefresh, 2500);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !document.getElementById("view-web").hidden) webRefresh();
});

async function webOpen(url, siteId) {
  const site = siteId && webSites.find((s) => s.site_id === siteId);
  document.getElementById("web-save-name").value = site ? site.name : "";
  document.getElementById("web-shot").removeAttribute("src");
  document.getElementById("web-url").value = url || (site && site.start_url) || "";
  showView("view-web");
  await webCall("/api/admin/web/browser/open", siteId ? { site_id: siteId } : { url });
}

function webClose() {
  clearTimeout(webRefreshTimer);
  clearTimeout(webTypeTimer);
  webTypeTimer = null;
  webResetTyping();
  api("/api/admin/web/browser/close", { method: "POST" }).catch(() => {});
}

function webAction(body) {
  return webCall("/api/admin/web/browser/action", body);
}

// --- typing straight into the page ---------------------------------------
// The bar under the picture mirrors the focused field: what is typed there is
// sent a few letters at a time (corrections as backspaces), so the phone's own
// keyboard writes into the website.

let webSent = "";          // what the bar has already sent to the page
let webTypeTimer = null;   // pending send (a short pause after the last key)
let webTyping = 0;         // sends in flight
let webTypeChain = Promise.resolve();
let webFocusAfterTap = false;

function webResetTyping() {
  const input = document.getElementById("web-live");
  input.value = "";
  webSent = "";
}

function webSetFocus(focus) {
  const input = document.getElementById("web-live");
  const bar = input.closest(".web-kbd");
  bar.classList.toggle("is-on", !!focus.editable);
  bar.classList.toggle("is-secret", !!(focus.editable && focus.password));
  // Switch to a hidden field only between words, never mid-typing.
  const type = focus.editable && focus.password ? "password" : "text";
  if (input.type !== type && !input.value) input.type = type;
  input.placeholder = !focus.editable
    ? "Tap a field on the page, then type here"
    : focus.password
      ? "Password: goes straight into the page (hidden)"
      : `Typing into ${focus.label ? `“${focus.label}”` : "the selected field"}`;
  if (webFocusAfterTap) {
    webFocusAfterTap = false;
    webResetTyping();
    input.type = type;
    if (focus.editable) input.focus({ preventScroll: true });  // opens the phone's keyboard
  }
}

// Send what changed since the last send; returns once it has been typed.
function webFlushTyping() {
  clearTimeout(webTypeTimer);
  webTypeTimer = null;
  const input = document.getElementById("web-live");
  const value = input.value;
  let k = 0;
  while (k < value.length && k < webSent.length && value[k] === webSent[k]) k++;
  const backspaces = webSent.length - k;
  const text = value.slice(k);
  if (!backspaces && !text) return webTypeChain;
  webSent = value;
  const secret = input.type === "password";
  const pieces = [];
  for (let i = 0; i < text.length; i += 400) pieces.push(text.slice(i, i + 400));
  if (!pieces.length) pieces.push("");
  pieces.forEach((piece, i) => {
    const body = { type: "edit", backspaces: i === 0 ? Math.min(backspaces, 500) : 0, text: piece, secret };
    webTypeChain = webTypeChain.then(async () => {
      webTyping++;
      try {
        const frame = await api("/api/admin/web/browser/action", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!webDrag) webShow(frame);
      } catch (e) {
        alertMsg(e.message);
      } finally {
        webTyping--;
      }
    });
  });
  return webTypeChain;
}

async function webKey(key) {
  await webFlushTyping();
  webResetTyping();
  webFocusAfterTap = true;  // Tab moves to the next field: follow it
  await webAction({ type: "key", key });
}

// --- tapping and swiping on the picture -----------------------------------

let webDrag = null;

function wireWebScreen() {
  const shot = document.getElementById("web-shot");
  const end = async (e, cancelled) => {
    const d = webDrag;
    webDrag = null;
    if (!d || !webFrame) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    const sx = webFrame.width / d.rect.width, sy = webFrame.height / d.rect.height;
    const px = (d.x - d.rect.left) * sx, py = (d.y - d.rect.top) * sy;
    if (cancelled || webBusy) { shot.style.transform = ""; return; }
    if (!d.moved) {
      if (Date.now() - d.t > 900) return;  // a long press: nothing
      webTapMark(d.x, d.y);
      await webFlushTyping();
      webFocusAfterTap = true;
      await webAction({ type: "click", x: px, y: py });
      return;
    }
    const horizontal = Math.abs(dx) > Math.abs(dy);
    await webAction({
      type: "scroll", x: px, y: py,
      dx: horizontal ? Math.round(-dx * sx) : 0,
      dy: horizontal ? 0 : Math.round(-dy * sy),
    });
    shot.style.transform = "";
  };
  shot.addEventListener("pointerdown", (e) => {
    if (!webFrame || e.button > 0) return;
    shot.setPointerCapture(e.pointerId);
    webDrag = { x: e.clientX, y: e.clientY, t: Date.now(), moved: false, rect: shot.getBoundingClientRect() };
  });
  shot.addEventListener("pointermove", (e) => {
    if (!webDrag) return;
    const dx = e.clientX - webDrag.x, dy = e.clientY - webDrag.y;
    if (!webDrag.moved && Math.hypot(dx, dy) > 10) webDrag.moved = true;
    if (webDrag.moved) {
      shot.style.transform = Math.abs(dx) > Math.abs(dy) ? `translateX(${dx}px)` : `translateY(${dy}px)`;
    }
  });
  shot.addEventListener("pointerup", (e) => end(e, false));
  shot.addEventListener("pointercancel", (e) => end(e, true));

  const input = document.getElementById("web-live");
  input.addEventListener("input", () => {
    clearTimeout(webTypeTimer);
    webTypeTimer = setTimeout(webFlushTyping, 300);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); webKey("Enter"); }
    // Backspace on an empty bar still deletes in the page (text that was already there).
    else if (e.key === "Backspace" && !input.value) {
      e.preventDefault();
      webTypeChain = webTypeChain.then(() => webAction({ type: "key", key: "Backspace" }));
    }
  });
}

async function webSave() {
  if (webBusy) return;
  const name = document.getElementById("web-save-name").value.trim();
  await webFlushTyping();  // letters still waiting to be sent belong to the login
  webSetBusy(true);
  clearTimeout(webRefreshTimer);
  try {
    const { site } = await api("/api/admin/web/browser/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    webOpenSettings = site.site_id;  // next step: tell the bot where to search
    await loadWebSites();
    goBack();  // closes the browser too
    alertMsg(`Saved "${site.name}". Now set where the bot should search it.`);
  } catch (e) {
    alertMsg(e.message);
  } finally {
    webSetBusy(false);
  }
}

function wireWebSources() {
  document.getElementById("web-sites").addEventListener("click", webSiteClick);
  const openNew = () => {
    const url = document.getElementById("web-new-url").value.trim();
    if (!url) return alertMsg("Type the website's address first.");
    webOpen(url, null);
  };
  document.getElementById("web-new-open").addEventListener("click", openNew);
  document.getElementById("web-new-url").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); openNew(); }
  });

  document.getElementById("web-tabs").addEventListener("click", (e) => {
    const close = e.target.closest("[data-close-tab]");
    const open = e.target.closest("[data-tab]");
    if (close) webAction({ type: "close_tab", i: Number(close.dataset.closeTab) });
    else if (open && !open.closest(".is-active")) webAction({ type: "tab", i: Number(open.dataset.tab) });
  });
  wireWebScreen();
  const go = () => {
    const url = document.getElementById("web-url").value.trim();
    if (url) webAction({ type: "goto", url });
  };
  document.getElementById("web-go").addEventListener("click", go);
  document.getElementById("web-url").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); go(); }
  });
  document.querySelector("#view-web .web-tools").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-web]");
    if (!b) return;
    const what = b.dataset.web;
    if (what === "back") webAction({ type: "back" });
    else if (what === "reload") webAction({ type: "reload" });
    else if (what === "up") webAction({ type: "scroll", dy: -500 });
    else if (what === "down") webAction({ type: "scroll", dy: 500 });
    else webCall("/api/admin/web/browser/frame");
  });
  document.querySelectorAll("#view-web [data-key]").forEach((b) => {
    // keep the keyboard open: the tap must not take focus from the typing bar
    b.addEventListener("pointerdown", (e) => e.preventDefault());
    b.addEventListener("click", () => webKey(b.dataset.key));
  });
  document.getElementById("web-save").addEventListener("click", webSave);
}


// ---------------------------------------------------------------------
// Reader tools: highlights, things to do with selected text, search in the
// book, the chapter list and night reading (reader_tools.py on the server).
//
// A highlight is anchored to the page's text layer -- the index of its first
// and last text piece and the character offsets in them -- so it lands on the
// same words at any zoom. Scanned pages have no text layer, so nothing can be
// selected or highlighted on them.
// ---------------------------------------------------------------------

const HL_COLORS = ["yellow", "green", "blue", "pink"];
let readerSel = null;       // {page, text, anchor} of the selection the bar acts on
let readerSelEdit = null;   // a highlight tapped on the page (bar in edit mode)
let readerSearchTerm = "";  // marked on the page after a search result is opened

function textLayerSpans() {
  const layer = document.getElementById("reader-text-layer");
  return Array.from(layer.children).filter((el) => el.tagName === "SPAN");
}

// Wrap characters [a, b) of a text piece in a <mark>, across the text nodes
// earlier marks may already have split it into.
function markRange(span, a, b, cls, id) {
  if (b <= a) return;
  const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
  let pos = 0;
  for (const node of nodes) {
    const len = node.data.length;
    const from = Math.max(a, pos), to = Math.min(b, pos + len);
    if (from < to) {
      let target = node;
      if (from > pos) target = target.splitText(from - pos);
      if (to < pos + len) target.splitText(to - from);
      const mark = document.createElement("mark");
      mark.className = cls;
      if (id) mark.dataset.hl = id;
      target.parentNode.insertBefore(mark, target);
      mark.appendChild(target);
    }
    pos += len;
  }
}

function clearReaderMarks() {
  const layer = document.getElementById("reader-text-layer");
  layer.querySelectorAll("mark").forEach((m) => m.replaceWith(...m.childNodes));
  layer.querySelectorAll(":scope > span").forEach((s) => s.normalize());
}

function applyReaderMarks() {
  clearReaderMarks();
  const spans = textLayerSpans();
  if (!spans.length) return;
  for (const h of readerState.highlights || []) {
    if (h.page !== readerState.pageNum || !h.anchor) continue;
    const { s, so, e, eo } = h.anchor;
    for (let i = s; i <= e && i < spans.length; i++) {
      const len = spans[i].textContent.length;
      markRange(spans[i], i === s ? so : 0, i === e ? Math.min(eo, len) : len, `hl hl-${h.color}`, h.id);
    }
  }
  if (readerSearchTerm) {
    const term = readerSearchTerm.toLowerCase();
    let first = null;
    spans.forEach((span) => {
      const t = span.textContent.toLowerCase();
      const hits = [];
      for (let i = t.indexOf(term); i >= 0; i = t.indexOf(term, i + term.length)) hits.push(i);
      hits.reverse().forEach((i) => markRange(span, i, i + term.length, "search-hl"));
      if (hits.length && !first) first = span;
    });
    if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

// The current text selection as a highlight anchor, or null when it isn't
// a selection inside this page's text.
function currentSelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  const layer = document.getElementById("reader-text-layer");
  if (!layer.contains(range.commonAncestorContainer)) return null;
  const spans = textLayerSpans();
  const pieceOf = (node) => {
    while (node && node.parentNode !== layer) node = node.parentNode;
    return node;
  };
  const offsetIn = (span, node, off) => {
    const r = document.createRange();
    r.setStart(span, 0);
    r.setEnd(node, off);
    return r.toString().length;
  };
  let sNode = range.startContainer, sOff = range.startOffset;
  let eNode = range.endContainer, eOff = range.endOffset;
  if (sNode === layer) { sNode = layer.childNodes[sOff] || layer.lastChild; sOff = 0; }
  if (eNode === layer) {
    const prev = layer.childNodes[eOff - 1] || layer.firstChild;
    eNode = prev; eOff = prev.childNodes.length;
  }
  const sSpan = pieceOf(sNode), eSpan = pieceOf(eNode);
  let s = spans.indexOf(sSpan), e = spans.indexOf(eSpan);
  if (s < 0 || e < 0) return null;
  let so = sNode === sSpan && sOff === 0 ? 0 : offsetIn(sSpan, sNode, sOff);
  let eo = offsetIn(eSpan, eNode, eOff);
  if (s > e || (s === e && so >= eo)) return null;
  const text = range.toString().replace(/\s+/g, " ").trim();
  if (!text) return null;
  return { page: readerState.pageNum, text, anchor: { s, so, e, eo } };
}

function showSelBar(editId) {
  const bar = document.getElementById("reader-selbar");
  readerSelEdit = editId || null;
  bar.querySelector('[data-sel="delete"]').hidden = !editId;
  const h = editId && (readerState.highlights || []).find((x) => x.id === editId);
  bar.querySelectorAll(".selbar-color").forEach((b) => b.classList.toggle("is-current", !!h && b.dataset.color === h.color));
  bar.hidden = false;
}

function hideSelBar() {
  document.getElementById("reader-selbar").hidden = true;
  readerSel = null;
  readerSelEdit = null;
}

let selTimer = null;
document.addEventListener("selectionchange", () => {
  clearTimeout(selTimer);
  selTimer = setTimeout(() => {
    if (document.getElementById("view-reader").hidden) return;
    const sel = currentSelection();
    if (sel) {
      readerSel = sel;
      showSelBar(null);
    } else if (!readerSelEdit && !document.getElementById("reader-selbar").matches(":hover")) {
      // Collapsed: give a tap on the bar itself time to land first.
      setTimeout(() => { if (!currentSelection() && !readerSelEdit) hideSelBar(); }, 350);
    }
  }, 220);
});

// A tap on a highlight: change its colour, remove it, or use its text.
document.getElementById("reader-text-layer").addEventListener("click", (e) => {
  const mark = e.target.closest("mark[data-hl]");
  const sel = window.getSelection();
  if (!mark || (sel && !sel.isCollapsed)) return;
  const h = (readerState.highlights || []).find((x) => x.id === mark.dataset.hl);
  if (!h) return;
  readerSel = { page: h.page, text: h.text, anchor: h.anchor };
  showSelBar(h.id);
});

let toastTimer = null;
function readerToast(text) {
  const t = document.getElementById("reader-toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

async function addHighlight(color) {
  const sel = readerSel;
  if (!sel) return;
  try {
    const h = await api(`/api/books/${currentBook.book_id}/highlights`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page: sel.page, text: sel.text, color, anchor: sel.anchor }),
    });
    readerState.highlights.push(h);
    window.getSelection().removeAllRanges();
    hideSelBar();
    applyReaderMarks();
    readerToast("Highlighted");
  } catch (e) {
    alertMsg(e.message);
  }
}

async function recolorHighlight(id, color) {
  try {
    const h = await api(`/api/books/${currentBook.book_id}/highlights/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ color }),
    });
    const i = readerState.highlights.findIndex((x) => x.id === id);
    if (i >= 0) readerState.highlights[i] = h;
    hideSelBar();
    applyReaderMarks();
    renderHighlightsPanel();
  } catch (e) {
    alertMsg(e.message);
  }
}

async function deleteHighlight(id) {
  try {
    await api(`/api/books/${currentBook.book_id}/highlights/${id}`, { method: "DELETE" });
  } catch (e) {
    if (e.status !== 404) return alertMsg(e.message);
  }
  readerState.highlights = readerState.highlights.filter((x) => x.id !== id);
  hideSelBar();
  applyReaderMarks();
  renderHighlightsPanel();
}

function readerClosePanels(except) {
  ["reader-search-panel", "reader-chapters-panel", "reader-hl-panel", "reader-explain", "reader-notes-panel"].forEach((id) => {
    if (id !== except) document.getElementById(id).hidden = true;
  });
}

function readerOpenPanel(id) {
  const panel = document.getElementById(id);
  const opening = panel.hidden;
  readerClosePanels(id);
  panel.hidden = !opening;
  if (opening) panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return opening;
}

function readerGoToPage(n) {
  if (!readerState.pdf || n < 1 || n > readerState.pdf.numPages) return;
  readerState.pageNum = n;
  renderReaderPage();
}

async function explainSelection(sel) {
  const panel = document.getElementById("reader-explain");
  readerClosePanels("reader-explain");
  panel.hidden = false;
  panel.dataset.text = sel.text;
  panel.dataset.page = sel.page;
  document.getElementById("reader-explain-title").innerHTML = `${ic("sparkles")} Explaining page ${Number(sel.page)}`;
  document.getElementById("reader-explain-body").innerHTML =
    `<blockquote>${escapeHtml(sel.text.slice(0, 600))}</blockquote><p class="spinner-line">${ic("hourglass")} Thinking…</p>`;
  document.getElementById("reader-explain-note").hidden = true;
  panel.scrollIntoView({ block: "start", behavior: "smooth" });
  try {
    const { answer } = await api(`/api/books/${currentBook.book_id}/explain`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: sel.text, page: sel.page }),
    });
    panel.dataset.answer = answer;
    document.getElementById("reader-explain-title").innerHTML = `${ic("sparkles")} Explained (page ${Number(sel.page)})`;
    document.getElementById("reader-explain-body").innerHTML =
      `<blockquote>${escapeHtml(sel.text.slice(0, 600))}</blockquote>${richText(answer)}`;
    document.getElementById("reader-explain-note").hidden = false;
  } catch (e) {
    document.getElementById("reader-explain-body").innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
  }
}

async function flashcardFromSelection(sel) {
  readerToast("Making a flashcard…");
  try {
    const { cards } = await api(`/api/books/${currentBook.book_id}/flashcards/from-selection`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: sel.text, page: sel.page }),
    });
    const panel = document.getElementById("reader-explain");
    readerClosePanels("reader-explain");
    panel.hidden = false;
    document.getElementById("reader-explain-title").innerHTML =
      `${ic("layers")} ${cards.length} flashcard${cards.length === 1 ? "" : "s"} added to this book's deck`;
    document.getElementById("reader-explain-body").innerHTML = cards
      .map((c) => `<div class="sel-card"><p><strong>${escapeHtml(c.front)}</strong></p><p class="muted">${escapeHtml(c.back)}</p></div>`)
      .join("");
    document.getElementById("reader-explain-note").hidden = true;
    panel.scrollIntoView({ block: "start", behavior: "smooth" });
  } catch (e) {
    alertMsg(e.message);
  }
}

async function noteFromText(page, text) {
  try {
    const note = await api(`/api/books/${currentBook.book_id}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page, text: text.slice(0, 10000) }),
    });
    readerState.notes.unshift(note);
    updateNotesButton();
    readerToast("Added to this page's notes");
  } catch (e) {
    alertMsg(e.message);
  }
}

// The selection bar: buttons must not take the selection away when tapped.
(() => {
  const bar = document.getElementById("reader-selbar");
  bar.addEventListener("pointerdown", (e) => e.preventDefault());
  bar.addEventListener("mousedown", (e) => e.preventDefault());
  bar.addEventListener("click", (e) => {
    const sel = readerSel;
    const colorBtn = e.target.closest(".selbar-color");
    if (colorBtn) {
      if (readerSelEdit) recolorHighlight(readerSelEdit, colorBtn.dataset.color);
      else addHighlight(colorBtn.dataset.color);
      return;
    }
    const btn = e.target.closest("[data-sel]");
    if (!btn || !sel) return;
    const what = btn.dataset.sel;
    if (what === "delete" && readerSelEdit) return deleteHighlight(readerSelEdit);
    window.getSelection().removeAllRanges();
    hideSelBar();
    if (what === "explain") explainSelection(sel);
    else if (what === "card") flashcardFromSelection(sel);
    else if (what === "note") noteFromText(sel.page, `“${sel.text}”`);
  });
})();

document.getElementById("reader-explain-note").addEventListener("click", () => {
  const panel = document.getElementById("reader-explain");
  const page = parseInt(panel.dataset.page, 10) || readerState.pageNum;
  noteFromText(page, `“${panel.dataset.text || ""}”\n\n${panel.dataset.answer || ""}`);
});

document.querySelectorAll("#view-reader .reader-panel-close").forEach((b) =>
  b.addEventListener("click", () => { b.closest(".reader-panel").hidden = true; })
);

// --- highlights list ---

function renderHighlightsPanel() {
  const box = document.getElementById("reader-hl-list");
  if (!box) return;
  const items = (readerState.highlights || []).slice().sort((a, b) => a.page - b.page);
  document.getElementById("reader-hl-export").hidden = items.length === 0;
  box.innerHTML = items.length
    ? items
        .map(
          (h) =>
            `<div class="reader-list-row"><button class="reader-list-main" data-hl-page="${Number(h.page)}">` +
            `<span class="hl-dot hl-${escapeHtml(h.color)}"></span><span class="reader-list-page">p.${Number(h.page)}</span>` +
            `<span class="reader-list-text">${escapeHtml(h.text.slice(0, 220))}</span></button>` +
            `<button class="mini-btn" data-hl-del="${escapeHtml(h.id)}" aria-label="Delete highlight">${ic("trash-2")}</button></div>`
        )
        .join("")
    : `<p class="muted">No highlights yet. Long-press text on a page to select it, then pick a colour.</p>`;
}

document.getElementById("reader-hl-btn").addEventListener("click", () => {
  if (readerOpenPanel("reader-hl-panel")) renderHighlightsPanel();
});
document.getElementById("reader-hl-list").addEventListener("click", (e) => {
  const del = e.target.closest("[data-hl-del]");
  if (del) return deleteHighlight(del.dataset.hlDel);
  const go = e.target.closest("[data-hl-page]");
  if (go) readerGoToPage(parseInt(go.dataset.hlPage, 10));
});
document.getElementById("reader-hl-export").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    await api(`/api/books/${currentBook.book_id}/highlights/export`, { method: "POST" });
    btn.innerHTML = `${ic("circle-check")} Sent to your chat`;
    setTimeout(() => { btn.innerHTML = label; btn.disabled = false; }, 2500);
  } catch (err) {
    btn.innerHTML = label;
    btn.disabled = false;
    alertMsg(err.message);
  }
});

// --- search in the book ---

async function runBookSearch() {
  const input = document.getElementById("reader-search-input");
  const q = input.value.trim();
  const status = document.getElementById("reader-search-status");
  const box = document.getElementById("reader-search-results");
  if (q.length < 2) return alertMsg("Type at least 2 characters to search.");
  status.textContent = "Searching the book… (the first search in a big book takes a little longer)";
  box.innerHTML = "";
  try {
    const { results, total, pages_with_text } = await api(
      `/api/books/${currentBook.book_id}/search?q=${encodeURIComponent(q)}`
    );
    readerSearchTerm = q;
    if (!pages_with_text) {
      status.textContent = "This book has no searchable text (it looks like scanned images).";
      return;
    }
    status.textContent = total
      ? `${total} match${total === 1 ? "" : "es"}${total > results.length ? ` (showing the first ${results.length})` : ""}`
      : "No matches.";
    box.innerHTML = results
      .map((r) => {
        const s = r.snippet;
        const snip = escapeHtml(s.slice(0, r.start)) + `<mark class="search-hl">${escapeHtml(s.slice(r.start, r.end))}</mark>` + escapeHtml(s.slice(r.end));
        return `<button class="reader-list-main reader-list-row" data-search-page="${Number(r.page)}">` +
          `<span class="reader-list-page">p.${Number(r.page)}</span><span class="reader-list-text">${snip}</span></button>`;
      })
      .join("");
  } catch (e) {
    status.textContent = "";
    alertMsg(e.message);
  }
}
document.getElementById("reader-search-btn").addEventListener("click", () => {
  if (readerOpenPanel("reader-search-panel")) document.getElementById("reader-search-input").focus();
});
document.getElementById("reader-search-go").addEventListener("click", runBookSearch);
document.getElementById("reader-search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runBookSearch(); }
});
document.getElementById("reader-search-results").addEventListener("click", (e) => {
  const r = e.target.closest("[data-search-page]");
  if (!r) return;
  const n = parseInt(r.dataset.searchPage, 10);
  if (n === readerState.pageNum) applyReaderMarks();
  else readerGoToPage(n);
});

// --- chapters ---

function renderChaptersPanel() {
  const box = document.getElementById("reader-chapters-list");
  const chapters = (currentBook && currentBook.chapters) || [];
  if (!chapters.length) {
    box.innerHTML = `<p class="muted">This book hasn't been divided into chapters yet. Go back to the book and tap “Divide into chapters”.</p>`;
    return;
  }
  const here = readerState.pageNum;
  let current = -1;
  chapters.forEach((c, i) => { if ((c.start_page || 0) <= here) current = i; });
  box.innerHTML = chapters
    .map(
      (c, i) =>
        `<button class="reader-list-main reader-list-row${i === current ? " is-current" : ""}" data-chapter-page="${Number(c.start_page)}">` +
        `<span class="reader-list-text">${escapeHtml(c.title || `Chapter ${i + 1}`)}</span><span class="reader-list-page">p.${Number(c.start_page)}</span></button>`
    )
    .join("");
}
document.getElementById("reader-chapters-btn").addEventListener("click", () => {
  if (readerOpenPanel("reader-chapters-panel")) renderChaptersPanel();
});
document.getElementById("reader-chapters-list").addEventListener("click", (e) => {
  const c = e.target.closest("[data-chapter-page]");
  if (!c) return;
  readerGoToPage(parseInt(c.dataset.chapterPage, 10));
  document.getElementById("reader-chapters-panel").hidden = true;
});

// --- night reading ---

function readerNightOn() {
  try { return localStorage.getItem("readerNight") === "1"; } catch (_) { return false; }
}
function readerApplyNight() {
  const on = readerNightOn();
  document.getElementById("view-reader").classList.toggle("is-night", on);
  document.getElementById("reader-night-btn").innerHTML = on ? `${ic("sun")} Day` : `${ic("moon")} Night`;
}
document.getElementById("reader-night-btn").addEventListener("click", () => {
  try { localStorage.setItem("readerNight", readerNightOn() ? "0" : "1"); } catch (_) {}
  readerApplyNight();
});
readerApplyNight();

{
  let t = null;
  document.getElementById("ask-book-search").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(() => {
      askQuery = e.target.value.trim();
      renderAskLibrary();
    }, 120);
  });
}


// ---------------------------------------------------------------------
// Account: log in with a username and password from the admin (linked to
// this Telegram account), or use the app as a guest with limits. The admin
// manages accounts and reads each one's activity log (accounts.py).
// ---------------------------------------------------------------------

let me = null;  // {tier: admin|account|premium|guest, username, is_admin, full_access, guest?}

function choiceKey() { return "guestChoice"; }
function guestChosen() { try { return localStorage.getItem(choiceKey()) === "1"; } catch (_) { return false; } }
function setGuestChosen(on) { try { on ? localStorage.setItem(choiceKey(), "1") : localStorage.removeItem(choiceKey()); } catch (_) {} }

async function loadMe({ prompt = false } = {}) {
  try {
    me = await api("/api/me");
  } catch (_) {
    return;
  }
  renderAccountUI();
  const wantsRegister = new URLSearchParams(window.location.search).get("register") === "1";
  if (me.can_register && (wantsRegister || prompt)) openLoginSheet();
  else if (prompt && me.tier === "guest" && !guestChosen()) openLoginSheet();
}

function renderAccountUI() {
  if (!me) return;
  const btn = document.getElementById("btn-account");
  if (me.can_register) btn.innerHTML = `${ic("user-check")} <span>Create account</span>`;
  else if (me.tier === "guest") btn.innerHTML = `${ic("log-in")} <span>Log in</span>`;
  else if (me.is_admin) btn.innerHTML = `${ic("shield-check")} <span>Admin</span>`;
  else if (me.username) btn.innerHTML = `${ic("user-check")} <span>${escapeHtml(me.username)}</span>`;
  else btn.innerHTML = `${ic("user-check")} <span>Premium</span>`;
  btn.classList.toggle("is-guest", me.tier === "guest" || me.tier === "expired" || !!me.can_register);
  const banner = document.getElementById("guest-banner");
  banner.hidden = me.tier !== "guest" && me.tier !== "expired";
  if (me.tier === "expired") {
    document.getElementById("guest-banner-text").textContent =
      "your Premium has ended, so guest limits apply. Renew it in the bot (⭐ My Plan)";
  }
  if (me.guest) {
    const q = me.guest;
    document.getElementById("guest-banner-text").textContent =
      `${q.questions_left} of ${q.questions_per_day} AI questions left today · no uploads or saving`;
    document.getElementById("guest-limit-q").textContent =
      `${q.questions_per_day} AI questions a day (Ask, Drug LookUp, Explain)`;
  }
}

function openLoginSheet(note) {
  const sheet = document.getElementById("login-sheet");
  const registering = !!(me && me.can_register);
  const loggedIn = !registering && me && me.tier !== "guest";
  document.getElementById("register-pane").hidden = !registering;
  document.getElementById("login-pane").hidden = registering || loggedIn;
  document.getElementById("account-pane").hidden = !loggedIn;
  if (registering) {
    sheet.hidden = false;
    setTimeout(() => document.getElementById("reg-user").focus(), 50);
    return;
  }
  const err = document.getElementById("login-error");
  err.hidden = !note;
  err.textContent = note || "";
  if (loggedIn) {
    document.getElementById("account-title").textContent =
      me.is_admin ? "Admin" : me.username ? `Logged in as ${me.username}` : "Premium";
    document.getElementById("account-sub").textContent = me.is_admin
      ? "You have full access and manage the accounts."
      : me.tier === "expired"
        ? "Your Premium has ended, so guest limits apply for now. Renew it in the bot (⭐ My Plan) for full access."
        : me.username
          ? "Full access. This account is linked to your Telegram account."
          : "Full access through your Premium plan.";
    document.getElementById("account-manage").hidden = !me.is_admin;
    document.getElementById("account-logout").hidden = !me.username;
  }
  sheet.hidden = false;
  if (!loggedIn) setTimeout(() => document.getElementById("login-user").focus(), 50);
}

function closeLoginSheet() {
  document.getElementById("login-sheet").hidden = true;
}

async function doLogin() {
  const user = document.getElementById("login-user").value.trim();
  const pass = document.getElementById("login-pass").value;
  const err = document.getElementById("login-error");
  if (!user || !pass) {
    err.hidden = false;
    err.textContent = "Enter your username and password.";
    return;
  }
  const btn = document.getElementById("login-go");
  btn.disabled = true;
  err.hidden = true;
  try {
    await api("/api/account/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: user, password: pass }),
    });
    document.getElementById("login-pass").value = "";
    setGuestChosen(false);
    await loadMe();
    closeLoginSheet();
    alertMsg(`Welcome, ${me && me.username ? me.username : user}! You now have full access.`);
    loadShelf();
  } catch (e) {
    err.hidden = false;
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("btn-account").addEventListener("click", () => openLoginSheet());
document.querySelectorAll("[data-open-login]").forEach((b) => b.addEventListener("click", () => openLoginSheet()));
document.getElementById("login-go").addEventListener("click", doLogin);
document.getElementById("login-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doLogin(); } });
document.getElementById("login-user").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); document.getElementById("login-pass").focus(); }
});
document.getElementById("login-guest").addEventListener("click", () => { setGuestChosen(true); closeLoginSheet(); });
document.getElementById("login-close").addEventListener("click", () => {
  if (me && me.tier === "guest") setGuestChosen(true);
  closeLoginSheet();
});
document.getElementById("login-sheet").addEventListener("click", (e) => {
  if (e.target.id === "login-sheet") document.getElementById("login-close").click();
});
document.getElementById("account-logout").addEventListener("click", async () => {
  if (!(await askConfirm("Log out? This Telegram account goes back to guest mode until you log in again."))) return;
  try {
    await api("/api/account/logout", { method: "POST" });
  } catch (e) {
    return alertMsg(e.message);
  }
  closeLoginSheet();
  await loadMe();
});
document.getElementById("account-manage").addEventListener("click", () => {
  closeLoginSheet();
  openAccounts();
});

// Guests can't upload: say why before a file is picked, not after.
document.getElementById("btn-add-book").addEventListener("click", (e) => {
  if (me && me.tier === "guest") {
    e.stopImmediatePropagation();
    e.preventDefault();
    openLoginSheet("Uploading your own books needs an account. Log in for full access.");
  }
}, true);

// --- admin: accounts ---------------------------------------------------

let accList = [];

function fmtWhen(ts) {
  if (!ts) return "never";
  const d = new Date(ts * 1000);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString();
}

async function openAccounts() {
  showView("view-accounts");
  document.getElementById("acc-list-pane").hidden = false;
  document.getElementById("acc-log-pane").hidden = true;
  document.getElementById("acc-created").hidden = true;
  await loadAccounts();
}

async function loadAccounts() {
  try {
    accList = (await api("/api/admin/accounts")).accounts;
  } catch (e) {
    return alertMsg(e.message);
  }
  document.getElementById("acc-count").textContent = `${accList.length}`;
  document.getElementById("acc-list").innerHTML = accList.length
    ? accList
        .map((a) => {
          const kind = a.access === "paid" ? `<span class="acc-tag paid">Premium (self-registered)</span>` : "";
          const status = a.disabled
            ? `<span class="acc-tag off">${ic("ban")} Disabled</span>`
            : a.telegram_id
              ? `<span class="acc-tag on">${ic("user-check")} Linked</span>`
              : `<span class="acc-tag">Not logged in yet</span>`;
          const u = escapeHtml(a.username);
          return `<div class="acc-row">
            <div class="acc-head"><strong>${u}</strong>${status}</div>${kind}
            <div class="muted acc-meta">${a.note ? escapeHtml(a.note) + " · " : ""}active ${fmtWhen(a.last_active)} · logged in ${fmtWhen(a.last_login)}</div>
            <div class="acc-actions">
              <button class="mini-btn" data-acc-log="${u}">${ic("activity")} Log &amp; stats</button>
              <button class="mini-btn" data-acc-pass="${u}">${ic("key-round")} New password</button>
              ${a.telegram_id ? `<button class="mini-btn" data-acc-unlink="${u}">${ic("unlink")} Unlink</button>` : ""}
              <button class="mini-btn" data-acc-disable="${u}" data-on="${a.disabled ? 0 : 1}">${ic("ban")} ${a.disabled ? "Enable" : "Disable"}</button>
              <button class="mini-btn" data-acc-del="${u}" aria-label="Delete ${u}">${ic("trash-2")}</button>
            </div>
          </div>`;
        })
        .join("")
    : `<p class="muted">No accounts yet. Create the first one above.</p>`;
}

function showCreated(username, password, label) {
  const box = document.getElementById("acc-created");
  box.hidden = false;
  box.innerHTML = `<p><strong>${escapeHtml(label)}</strong> Send these to the person; the password isn't shown again.</p>
    <div class="acc-cred"><span>Username</span><code>${escapeHtml(username)}</code></div>
    <div class="acc-cred"><span>Password</span><code>${escapeHtml(password)}</code></div>
    <button class="mini-btn" id="acc-copy">${ic("copy")} Copy both</button>`;
  document.getElementById("acc-copy").addEventListener("click", async () => {
    const text = `Medic Toolkit login\nUsername: ${username}\nPassword: ${password}`;
    try { await navigator.clipboard.writeText(text); alertMsg("Copied."); } catch (_) { alertMsg(text); }
  });
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

document.getElementById("acc-create").addEventListener("click", async () => {
  const username = document.getElementById("acc-new-user").value.trim();
  const password = document.getElementById("acc-new-pass").value.trim();
  const note = document.getElementById("acc-new-note").value.trim();
  if (!username) return alertMsg("Type a username first.");
  try {
    const r = await api("/api/admin/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password: password || null, note }),
    });
    ["acc-new-user", "acc-new-pass", "acc-new-note"].forEach((id) => (document.getElementById(id).value = ""));
    showCreated(r.username, r.password, "Account created.");
    loadAccounts();
  } catch (e) {
    alertMsg(e.message);
  }
});

document.getElementById("acc-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  try {
    if (b.dataset.accLog) return openAccountLog(b.dataset.accLog);
    if (b.dataset.accPass) {
      if (!(await askConfirm(`Give ${b.dataset.accPass} a new password? The old one stops working.`))) return;
      const r = await post(`/api/admin/accounts/${encodeURIComponent(b.dataset.accPass)}/password`);
      showCreated(b.dataset.accPass, r.password, "New password set.");
    } else if (b.dataset.accUnlink) {
      if (!(await askConfirm(`Unlink ${b.dataset.accUnlink} from its Telegram account? It can then be logged into again (from any Telegram account).`))) return;
      await post(`/api/admin/accounts/${encodeURIComponent(b.dataset.accUnlink)}/unlink`);
    } else if (b.dataset.accDisable) {
      await post(`/api/admin/accounts/${encodeURIComponent(b.dataset.accDisable)}/disable`, { disabled: b.dataset.on === "1" });
    } else if (b.dataset.accDel) {
      if (!(await askConfirm(`Delete the account ${b.dataset.accDel} and its log? Its Telegram account goes back to guest mode.`))) return;
      await api(`/api/admin/accounts/${encodeURIComponent(b.dataset.accDel)}`, { method: "DELETE" });
    }
    loadAccounts();
  } catch (err) {
    alertMsg(err.message);
  }
});

async function openAccountLog(username) {
  let data;
  try {
    data = await api(`/api/admin/accounts/${encodeURIComponent(username)}/log`);
  } catch (e) {
    return alertMsg(e.message);
  }
  document.getElementById("acc-list-pane").hidden = true;
  document.getElementById("acc-log-pane").hidden = false;
  document.getElementById("acc-log-title").textContent = username;
  const s = data.stats;
  const card = (label, value, warn) => `<div class="stat-card${warn ? " warn" : ""}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value ?? "–"))}</strong></div>`;
  const maxDay = Math.max(1, ...s.per_day.map(([, n]) => n));
  document.getElementById("acc-log-stats").innerHTML =
    `<div class="stat-grid">` +
    card("Requests (24 h)", s.requests_24h) + card("Requests (7 days)", s.requests_7d) + card("All requests", s.requests) +
    card("AI calls", s.ai_calls_total) + card("Errors (7 days)", s.errors_7d, s.errors_7d > 0) + card("Server errors", s.server_errors, s.server_errors > 0) +
    card("Typical response", s.median_ms != null ? `${s.median_ms} ms` : "–") + card("Logins", s.logins) +
    card("Failed logins", s.failed_logins, s.failed_logins > 0) +
    `</div>` +
    (s.per_day.length
      ? `<p class="stat-label">Requests per day (last 14 days)</p><div class="day-bars">` +
        s.per_day.map(([d, n]) => `<div class="day-bar" title="${d}: ${n}"><i style="height:${Math.round((n / maxDay) * 100)}%"></i><span>${d.slice(8)}</span></div>`).join("") +
        `</div>`
      : "") +
    (Object.keys(s.ai_calls).length
      ? `<p class="stat-label">AI use</p><div class="kv">` + Object.entries(s.ai_calls).map(([k, v]) => `<span>${escapeHtml(k)}</span><b>${v}</b>`).join("") + `</div>`
      : "") +
    (s.top_endpoints.length
      ? `<p class="stat-label">Most used</p><div class="kv">` + s.top_endpoints.map(([k, v]) => `<span><code>${escapeHtml(k)}</code></span><b>${v}</b>`).join("") + `</div>`
      : "") +
    `<p class="muted">Logged since ${s.since ? new Date(s.since * 1000).toLocaleString() : "–"}.</p>`;
  const row = (e) => {
    const when = new Date(e.ts * 1000).toLocaleString();
    if (e.kind === "api") {
      const bad = e.s >= 400;
      return `<div class="acc-event${bad ? " bad" : ""}"><span class="ev-when">${when}</span>` +
        `<span class="ev-what"><b>${e.s}</b> ${escapeHtml(e.m)} <code>${escapeHtml(e.p)}</code> · ${e.ms} ms</span>` +
        (e.err ? `<span class="ev-err">${escapeHtml(e.err)}</span>` : "") + `</div>`;
    }
    const labels = { login: "Logged in", logout: "Logged out", login_failed: "Wrong password", login_refused: "Login refused (linked elsewhere)",
      created: "Account created", registered: "Registered after paying", password_reset: "Password reset", unlinked: "Unlinked by admin", disabled: "Disabled", enabled: "Enabled" };
    return `<div class="acc-event${/failed|refused/.test(e.kind) ? " bad" : ""}"><span class="ev-when">${when}</span>` +
      `<span class="ev-what">${escapeHtml(labels[e.kind] || e.kind)}${e.telegram_id ? ` · Telegram ${Number(e.telegram_id)}` : ""}</span></div>`;
  };
  document.getElementById("acc-log-errors").innerHTML = data.errors.length ? data.errors.map(row).join("") : `<p class="muted">No errors.</p>`;
  document.getElementById("acc-log-events").innerHTML = data.events.length ? data.events.slice(0, 150).map(row).join("") : `<p class="muted">Nothing yet.</p>`;
  window.scrollTo(0, 0);
}

document.getElementById("acc-log-back").addEventListener("click", () => {
  document.getElementById("acc-log-pane").hidden = true;
  document.getElementById("acc-list-pane").hidden = false;
  loadAccounts();
});

loadMe({ prompt: true });

// --- registration after a Premium (Stars) payment ---------------------

async function doRegister() {
  const user = document.getElementById("reg-user").value.trim();
  const pass = document.getElementById("reg-pass").value;
  const pass2 = document.getElementById("reg-pass2").value;
  const err = document.getElementById("reg-error");
  const fail = (m) => { err.hidden = false; err.textContent = m; };
  if (!user) return fail("Choose a username.");
  if (pass.length < 8) return fail("Choose a password of at least 8 characters.");
  if (pass !== pass2) return fail("The two passwords don't match.");
  const btn = document.getElementById("reg-go");
  btn.disabled = true;
  err.hidden = true;
  try {
    const { username } = await api("/api/account/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: user, password: pass }),
    });
    ["reg-pass", "reg-pass2"].forEach((id) => (document.getElementById(id).value = ""));
    await loadMe();
    closeLoginSheet();
    alertMsg(`Your account "${username}" is ready and linked to this Telegram account. Keep your password somewhere safe.`);
  } catch (e) {
    fail(e.message);
  } finally {
    btn.disabled = false;
  }
}
document.getElementById("reg-go").addEventListener("click", doRegister);
document.getElementById("reg-pass2").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doRegister(); } });
document.getElementById("reg-later").addEventListener("click", closeLoginSheet);
