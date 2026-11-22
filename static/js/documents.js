// Documents tab: curate Documents records (templates/index.html #nav-documents).
// Server routes: GET /documents (list page), GET /documents/<id> (detail),
// POST /documents (edits + actions), GET/POST /documents/preferences.
// Workflow rules live in birddog/documents.py; the server sends each row's
// allowed actions and the user's visible process codes, and re-checks every
// change. Comments only ever append.

(function () {
"use strict";

// ---------- schema options (Documents multi-selects, as stored) ----------
const DOC_TYPES = ["Vitals", "Residence & Identity", "Persecution", "Military", "Taxes & Voting", "Legal & Land",
                   "Police", "Education", "Organizations", "Indexes", "Bibliographical", "Other"];
const DOC_STRUCTURES = ["Template", "Narrative"];
const DOC_SCOPES = ["Individual", "Group"];
const LANGUAGES = ["Czech", "English", "French", "German", "Hebrew", "Hungarian", "Latin", "Polish", "Romanian",
                   "Russian", "Slovak", "Yiddish", "Ukranian"];
const MULTI = {doc_type: DOC_TYPES, doc_structure: DOC_STRUCTURES, doc_scope: DOC_SCOPES, language: LANGUAGES};
const CURATED = ["doc_type", "doc_structure", "doc_scope", "language", "filename"];

const PROCESS_CODES = {
  new:       {stage: "entry", d: "Entry level: newly imported; awaiting initial review"},
  duplicate: {stage: "entry", d: "Entry level or end state: already retained elsewhere"},
  check:     {stage: "mid",   d: "Midstream: basic check of automatic data (type/scope/location/etc.)"},
  review:    {stage: "mid",   d: "Midstream: requires skilled language/content review"},
  escalate:  {stage: "mid",   d: "Midstream: requires specialized review"},
  reject:    {stage: "off",   d: "End state: no Jewish content"},
  defer:     {stage: "off",   d: "End state: defer indefinitely; mark for check in distant future"},
  save:      {stage: "end",   d: "End state: save to server"},
};
const LOCATION_STATUS_TEXT = {pending: "Not yet inferred", none: "None found", failed: "Inference failed"};

// optional grid columns (label is fixed); w = default width in px
const COLUMN_DEFS = [
  {key: "process_code",       title: "Process code", w: 110},
  {key: "page_native_description", title: "Page native description", cls: "dt-wrap", w: 300},
  {key: "page_description",   title: "Page description",   cls: "dt-wrap", w: 300},
  {key: "native_description", title: "Native description", cls: "dt-wrap", w: 300},
  {key: "description",        title: "Description",        cls: "dt-wrap", w: 300},
  {key: "years",              title: "Years", w: 90},
  {key: "page_count",         title: "Pages", num: true, w: 70},
  {key: "byte_size",          title: "Size",  num: true, w: 80},
  {key: "doc_type",           title: "Doc type",  cls: "dt-tags", w: 180},
  {key: "doc_structure",      title: "Structure", cls: "dt-tags", w: 120},
  {key: "doc_scope",          title: "Scope",     cls: "dt-tags", w: 110},
  {key: "language",           title: "Language",  cls: "dt-tags", w: 130},
  {key: "filename",           title: "File name", w: 190},
  {key: "anomaly_comment",    title: "Anomaly comment", clip: true, w: 200},
  {key: "comments",           title: "Comments",  clip: true, w: 200},
  {key: "last_updated",       title: "Last Updated", w: 140},
];
const FIXED_COLS = [{key: "_sel", w: 34}, {key: "label", title: "Label", w: 190}];
const DEFAULT_COLUMNS = ["page_native_description", "page_description", "years", "page_count", "byte_size", "doc_type", "last_updated"];
const MIN_COL_W = 50;
const THUMB_WIDTHS = [120, 250, 330, 500, 960, 1280, 1920];   // standard Wikimedia widths

// ---------- state ----------
const prefs = {           // saved per user (Birddog Users.doc_preferences)
  columns: [...DEFAULT_COLUMNS],
  colWidths: {},
  sort: {key: "label", dir: 1},
  pageSize: 50,
  prefix: "",
  pcFilter: null,
  anomalyOnly: false,
};
const state = {
  started: false,
  codes: [],              // process codes this user can see ("" = blank)
  seesAnomalies: false,
  rows: [],               // current page
  total: null,
  cursors: [null],        // cursors[i] fetches page i
  page: 0,
  nextCursor: null,
  loading: false,
  known: new Map(),       // id -> latest row seen (pending edits can span pages)
  selected: new Set(),
  focus: null,
  pending: new Map(),     // id -> {field: value}
  batch: null,
  detail: new Map(),      // id -> GET /documents/<id> result (locations, owning pages)
};

// ---------- helpers ----------
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const same = (a, b) => Array.isArray(a) || Array.isArray(b)
  ? [...(a || [])].sort().join("|") === [...(b || [])].sort().join("|")
  : (a ?? "") === (b ?? "");
const cap = t => t.charAt(0).toUpperCase() + t.slice(1);
const codeName = c => c || "(blank)";
function fmtBytes(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}
const fmtTime = t => t ? String(t).replace("T", " ").slice(0, 16) : "";
// matches the importer for now: DAHMO/5/1/7 -> DAHMO-5-1-7
const defaultFilename = label => (label || "").replace(/^\/+/, "").replace(/\//g, "-");
function eff(d, f) { const p = state.pending.get(d.id); return p && f in p ? p[f] : d[f]; }
function pcBadge(code) {
  if (!code) return `<span class="badge dt-pc dt-pc-blank">blank</span>`;
  const st = (PROCESS_CODES[code] || {}).stage || "off";
  return `<span class="badge dt-pc dt-pc-${st}">${esc(code)}</span>`;
}
function setField(d, f, v) {
  const p = {...(state.pending.get(d.id) || {})};
  if (same(v, d[f])) delete p[f]; else p[f] = Array.isArray(v) ? [...v] : v;
  Object.keys(p).length ? state.pending.set(d.id, p) : state.pending.delete(d.id);
}
const docById = id => state.known.get(id);
// some documents (e.g. new finding aids) have no label yet: show the file name
const docName = d => d.label || (d.commons_file || (d.title || "").replace(/^[^:]*:/, "") || `#${d.id}`).replace(/_/g, " ");
const docNameHtml = d => d.label ? esc(d.label) : `<span class="fst-italic text-body-secondary" title="No label yet">${esc(docName(d))}</span>`;
const currentUser = () => { try { return JSON.parse($("user-data").dataset.user) || {}; } catch (e) { return {}; } };

function toast(msg) {
  $("dt-toast-body").textContent = msg;
  bootstrap.Toast.getOrCreateInstance($("dt-toast"), {delay: 3000}).show();
}
function showError(msg) {
  $("dt-error").textContent = msg || "";
  $("dt-error").classList.toggle("d-none", !msg);
}

async function api(method, url, body) {
  const opts = {method, headers: {}};
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const resp = await fetch(url, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* empty body */ }
  if (!resp.ok) {
    const err = new Error((data && data.error) || `${resp.status} ${resp.statusText}`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

// ---------- preferences ----------
let prefsTimer = null;
function savePrefs() {
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(() => api("POST", "/documents/preferences", prefs).catch(e => console.warn("documents prefs:", e)), 800);
}
async function loadPrefs() {
  try {
    const saved = await api("GET", "/documents/preferences");
    if (saved && typeof saved === "object") {
      for (const k of Object.keys(prefs)) if (k in saved) prefs[k] = saved[k];
      prefs.columns = (prefs.columns || []).filter(k => COLUMN_DEFS.some(c => c.key === k));
      if (![25, 50, 100].includes(+prefs.pageSize)) prefs.pageSize = 50;
    }
  } catch (e) { console.warn("documents prefs:", e); }
}

// ---------- loading ----------
function filterParams() {
  const q = new URLSearchParams();
  if (prefs.pcFilter !== null && prefs.pcFilter !== undefined) q.set("process_code", prefs.pcFilter === "" ? "(blank)" : prefs.pcFilter);
  const prefix = normPrefix();
  if (prefix) q.set("prefix", prefix);
  if (prefs.anomalyOnly && state.seesAnomalies) q.set("anomalies", "1");
  q.set("sort", prefs.sort.key);
  q.set("dir", prefs.sort.dir > 0 ? "asc" : "desc");
  q.set("limit", prefs.pageSize);
  return q;
}
const normPrefix = () => (prefs.prefix || "").trim().replace(/^\/+|\/+$/g, "");

let loadSeq = 0;
// load page `page` (cursors[page] must be known); withTotal re-counts
async function loadPage(page, {withTotal = false, focus = "keep"} = {}) {
  const seq = ++loadSeq;
  state.loading = true;
  renderGrid();
  const q = filterParams();
  const cursor = state.cursors[page];
  if (cursor !== null && cursor !== undefined) q.set("cursor", cursor);
  if (withTotal || page === 0) q.set("total", "1");
  try {
    const data = await api("GET", "/documents?" + q.toString());
    if (seq !== loadSeq) return;            // a newer load replaced this one
    showError("");
    state.codes = data.process_codes || [];
    state.seesAnomalies = !!data.sees_anomalies;
    if (prefs.pcFilter === null && state.codes.length > 1) {
      // first visit: the filter shows one process code at a time
      prefs.pcFilter = state.codes[0];
      state.loading = false;
      return reload();
    }
    state.rows = data.documents || [];
    state.rows.forEach(d => state.known.set(d.id, d));
    if (data.total !== null && data.total !== undefined) state.total = data.total;
    state.page = page;
    state.nextCursor = data.next_cursor;
    state.cursors[page + 1] = data.next_cursor;
    state.cursors.length = page + 2;
    if (focus === "first") state.focus = state.rows.length ? state.rows[0].id : null;
    else if (focus === "last") state.focus = state.rows.length ? state.rows[state.rows.length - 1].id : null;
  } catch (e) {
    if (seq !== loadSeq) return;
    if (e.status === 400 && /process code not available/.test(e.message)) {
      prefs.pcFilter = null;                 // a saved filter the user can no longer see
      state.loading = false;
      return reload();
    }
    showError(e.status === 503 ? "The database is unavailable right now. Try again later." : `Couldn't load documents: ${e.message}`);
    state.rows = [];
  } finally {
    if (seq === loadSeq) { state.loading = false; render(); }
  }
}
// new filter or sort: back to the first page, recount
function reload(focus = "first") {
  state.cursors = [null];
  state.selected.clear();
  state.batch = null;
  return loadPage(0, {withTotal: true, focus});
}

// ---------- filters ----------
function renderFilters() {
  const codes = state.codes;
  const showCodes = codes.length > 1;
  $("dt-pc-label").hidden = $("dt-pc-filter").hidden = !showCodes;
  $("dt-pc-filter").innerHTML = codes.map(c => `
    <div class="form-check form-check-inline mb-0 me-0" title="${esc((PROCESS_CODES[c] || {}).d || "No process code")}">
      <input class="form-check-input" type="radio" name="dt-pcf" id="dt-pcf-${esc(c || "blank")}" value="${esc(c)}" ${prefs.pcFilter === c ? "checked" : ""}>
      <label class="form-check-label small" for="dt-pcf-${esc(c || "blank")}">${esc(codeName(c))}</label>
    </div>`).join("");
  document.querySelectorAll('input[name="dt-pcf"]').forEach(r => r.onchange = () => {
    prefs.pcFilter = r.value; savePrefs(); reload();
  });
  $("dt-anomaly-wrap").hidden = !state.seesAnomalies;
  $("dt-anomaly-only").checked = !!prefs.anomalyOnly;
  $("dt-filter-row").classList.toggle("d-none", !showCodes && !state.seesAnomalies);
}

function matchesFilter(d) {
  // does a changed row still belong in this list? (prefix never changes)
  const code = d.process_code || "";
  if (!state.codes.includes(code)) return false;
  if (prefs.pcFilter !== null && code !== prefs.pcFilter) return false;
  if (!state.seesAnomalies && d.anomaly) return false;
  if (prefs.anomalyOnly && state.seesAnomalies && !d.anomaly) return false;
  return true;
}

// ---------- grid ----------
const colW = c => prefs.colWidths[c.key] || c.w;
const visibleCols = () => [...FIXED_COLS, ...prefs.columns.map(k => COLUMN_DEFS.find(c => c.key === k)).filter(Boolean)];
function applyTableWidth() { $("dt-table").style.width = visibleCols().reduce((a, c) => a + colW(c), 0) + "px"; }

let justResized = false;
function renderHead() {
  const cols = visibleCols();
  $("dt-cols").innerHTML = cols.map(c => `<col data-col="${c.key}" style="width:${colW(c)}px">`).join("");
  applyTableWidth();
  $("dt-head").innerHTML = cols.map(c => {
    if (c.key === "_sel") return `<th class="dt-nosort dt-frz dt-frz-1"><input type="checkbox" class="form-check-input" id="dt-select-all" aria-label="Select all on this page"></th>`;
    const ind = prefs.sort.key === c.key ? (prefs.sort.dir > 0 ? "\u25B2" : "\u25BC") : "\u2195";
    const frz = c.key === "label" ? "dt-frz dt-frz-2" : "";
    return `<th data-sort="${c.key}" class="${c.num ? "dt-num" : ""} ${frz}" title="${esc(c.title)}">${esc(c.title)}<span class="dt-sort-ind">${ind}</span><span class="dt-rs" data-rs="${c.key}" title="Drag to resize, double-click to reset"></span></th>`;
  }).join("");
  document.querySelectorAll("#dt-head .dt-rs").forEach(h => {
    const key = h.dataset.rs, c = cols.find(x => x.key === key), colEl = document.querySelector(`#dt-cols col[data-col="${key}"]`);
    h.onclick = e => e.stopPropagation();
    h.ondblclick = e => { e.stopPropagation(); delete prefs.colWidths[key]; savePrefs(); renderGrid(); };
    h.onpointerdown = e => {
      e.preventDefault(); e.stopPropagation();
      const x0 = e.clientX, w0 = colW(c);
      h.setPointerCapture(e.pointerId); h.classList.add("active"); document.body.classList.add("dt-resizing");
      h.onpointermove = ev => {
        prefs.colWidths[key] = Math.max(MIN_COL_W, Math.round(w0 + ev.clientX - x0));
        colEl.style.width = prefs.colWidths[key] + "px"; applyTableWidth();
      };
      h.onpointerup = () => {
        h.onpointermove = h.onpointerup = null; h.classList.remove("active"); document.body.classList.remove("dt-resizing");
        justResized = true; setTimeout(() => { justResized = false; }, 0);
        savePrefs();
      };
    };
  });
  document.querySelectorAll("#dt-head th[data-sort]").forEach(th => th.onclick = () => {
    if (justResized) return;
    const k = th.dataset.sort;
    prefs.sort = {key: k, dir: prefs.sort.key === k ? -prefs.sort.dir : 1};
    savePrefs(); reload();
  });
  $("dt-select-all").onchange = e => {
    state.rows.forEach(d => e.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id));
    selectionChanged();
  };
}

function cell(d, key) {
  const def = COLUMN_DEFS.find(c => c.key === key), v = eff(d, key);
  if (key === "process_code") return `<td>${pcBadge(v)}</td>`;
  if (key === "byte_size") return `<td class="dt-num">${fmtBytes(v)}</td>`;
  if (def.num) return `<td class="dt-num">${esc(v)}</td>`;
  if (def.cls === "dt-tags") return `<td class="dt-tags">${(v || []).map(t => `<span class="badge text-bg-light border dt-tag">${esc(t)}</span>`).join("")}</td>`;
  if (def.clip) return `<td class="small text-body-secondary" title="${esc(v)}">${esc(v)}</td>`;
  if (key === "last_updated") return `<td class="small" title="Created ${esc(fmtTime(d.created_at))}${d.updated_at ? ", updated " + esc(fmtTime(d.updated_at)) : ""} (UTC)">${esc(fmtTime(v))}</td>`;
  if (key === "filename") return `<td class="dt-mono small">${esc(v)}</td>`;
  return `<td class="${def.cls || ""}">${esc(v)}</td>`;
}

function renderGrid() {
  renderHead();
  const rows = state.rows;
  $("dt-body").innerHTML = rows.length ? rows.map(d => {
    const cls = [state.pending.has(d.id) && "dt-dirty", state.selected.has(d.id) && "dt-sel", isFocused(d.id) && "dt-focus"].filter(Boolean).join(" ");
    const anom = eff(d, "anomaly") ? ` <i class="bi bi-exclamation-triangle-fill text-danger" title="Anomaly: ${esc(eff(d, "anomaly_comment"))}"></i>` : "";
    return `<tr data-id="${d.id}" class="${cls}">
      <td class="dt-frz dt-frz-1"><input type="checkbox" class="form-check-input dt-row-sel" aria-label="Select ${esc(docName(d))}" ${state.selected.has(d.id) ? "checked" : ""}></td>
      <td class="dt-frz dt-frz-2"><a class="dt-label" href="${esc(d.url)}" target="_blank" rel="noopener" title="Open document in a new tab">${docNameHtml(d)} <i class="bi bi-box-arrow-up-right small"></i></a>${anom}</td>
      ${prefs.columns.filter(k => COLUMN_DEFS.some(c => c.key === k)).map(k => cell(d, k)).join("")}
    </tr>`;
  }).join("") : "";
  // no rows: a centered message across the card, and no paging row (unless
  // a later page came up empty, where Previous is still needed)
  // while loading, the spinner takes the table's place
  const empty = !rows.length, loading = state.loading;
  $("dt-empty").innerHTML = loading ? `<span class="spinner-border spinner-border-sm me-1"></span> Loading...` : empty ? emptyText() : "";
  $("dt-empty").classList.toggle("d-none", !loading && !empty);
  $("dt-scroll").classList.toggle("d-none", loading);
  $("dt-editor").classList.toggle("d-none", loading);
  $("dt-footer").classList.toggle("d-none", loading || (empty && state.page === 0));

  document.querySelectorAll("#dt-body tr[data-id]").forEach(tr => {
    const id = +tr.dataset.id;
    tr.onclick = e => {
      if (e.target.closest("a, select, input, button")) return;
      state.selected.clear(); state.focus = id; selectionChanged();
    };
    tr.querySelector(".dt-row-sel").onchange = e => { e.target.checked ? state.selected.add(id) : state.selected.delete(id); selectionChanged(); };
  });
  $("dt-select-all").checked = rows.length > 0 && rows.every(d => state.selected.has(d.id));
  renderSummary();
}
function emptyText() {
  const where = normPrefix() ? ` under <code>${esc(normPrefix())}</code>` : "";
  const code = prefs.pcFilter !== null ? ` with process code <code>${esc(codeName(prefs.pcFilter))}</code>` : "";
  return `No documents${where}${code}${prefs.anomalyOnly && state.seesAnomalies ? " and an anomaly flag" : ""}.`;
}
function renderSummary() {
  const total = state.total;
  const where = normPrefix() ? ` under <code>${esc(normPrefix())}</code>` : "";
  $("dt-summary").innerHTML = state.loading ? `<span class="spinner-border spinner-border-sm me-1"></span> Loading...`
    : total === null ? "" : `<strong>${total}</strong> document${total === 1 ? "" : "s"}${where}`;
  const start = state.rows.length ? state.page * prefs.pageSize + 1 : 0;
  $("dt-page-summary").textContent = state.rows.length
    ? `Showing ${start}-${start + state.rows.length - 1}${total !== null ? ` of ${total}` : ""}` : "";
  $("dt-page-prev").disabled = state.loading || state.page === 0;
  $("dt-page-next").disabled = state.loading || !state.nextCursor;
  $("dt-page-size").value = String(prefs.pageSize);
}
const isFocused = id => state.selected.size ? false : state.focus === id;

function renderBars() {
  $("dt-pending-count").textContent = state.pending.size;
  $("dt-pending-bar").classList.toggle("d-none", !state.pending.size);
  resetDiscard();
}
function render() { renderFilters(); renderGrid(); renderBars(); renderEditor(); }

function selectionChanged() {
  state.batch = state.selected.size > 1 ? newBatchDraft() : null;
  if (state.selected.size === 1) state.focus = [...state.selected][0];
  render();
}

// ---------- editor pieces ----------
function pillsHtml(field, values, current) {
  const all = [...new Set([...values, ...(current || [])])];
  return `<div class="dt-pills" data-field="${field}">${all.map(v => `<button type="button" class="btn btn-sm rounded-pill ${(current || []).includes(v) ? "btn-secondary" : "btn-outline-secondary"}" data-v="${esc(v)}">${esc(v)}</button>`).join("")}</div>`;
}
function fld(field, label, body, hint = "") {
  return `<div class="dt-fld d-flex flex-column gap-1" data-fld="${field}"><div class="small fw-semibold">${label}</div>${body}${hint ? `<div class="form-text mt-0">${hint}</div>` : ""}</div>`;
}
const fieldTitle = f => ({doc_type: "Doc type", doc_structure: "Structure", doc_scope: "Scope", language: "Language", filename: "File name"})[f] || f;

// buttons for a set of documents: the actions every one of them allows
function actionButtons(docs) {
  const common = docs.map(d => d.actions || []).reduce((a, b) => a.filter(x => b.includes(x)));
  const procActs = common.filter(a => !["anomaly", "clear_anomaly"].includes(a));
  const flagged = docs.filter(d => d.anomaly).length;
  const next = procActs.find(a => ["check", "review", "escalate"].includes(a));
  const btn = (act, cls, html, title) => `<button type="button" class="btn btn-sm ${cls}" data-act="${act}" title="${esc(title)}">${html}</button>`;
  const parts = [];
  procActs.forEach(a => {
    if (a === next) parts.push(btn(a, "btn-outline-primary", cap(a), (PROCESS_CODES[a] || {}).d || ""));
    else if (a === "check") parts.push(btn(a, "btn-outline-secondary", `<i class="bi bi-chevron-left"></i> Back to check`, "Send back to check"));
    else parts.push(btn(a, a === "save" ? "btn-outline-success" : a === "reject" ? "btn-outline-warning dt-btn-reject" : "btn-outline-secondary", cap(a), (PROCESS_CODES[a] || {}).d || ""));
  });
  if (flagged < docs.length) parts.push(btn("anomaly", "btn-outline-danger", `<i class="bi bi-exclamation-triangle"></i> Flag anomaly`, "Flag as an anomaly and say why"));
  if (flagged) parts.push(btn("clear_anomaly", "btn-outline-secondary", `Clear anomaly${docs.length > 1 ? ` (${flagged})` : ""}`, "Clear the anomaly flag and anomaly comment"));
  return `<div class="dt-actions">${parts.join("")}</div>`;
}

// ---------- page thumbnail + viewer (Commons files only) ----------
function pageThumb(d, page, width) {
  if (!d.commons_file || !d.commons_hash) return null;
  const n = encodeURIComponent(d.commons_file);
  return `https://upload.wikimedia.org/wikipedia/commons/thumb/${d.commons_hash}/${n}/page${page}-${width}px-${n}.jpg`;
}
const insp = {doc: null, page: 1, aspect: 0.7};
function inspWidth() {
  const st = document.querySelector(".dt-insp-stage");
  const need = Math.min(st.clientWidth - 32, (st.clientHeight - 16) * insp.aspect) * (window.devicePixelRatio || 1);
  return THUMB_WIDTHS.find(x => x >= need) || THUMB_WIDTHS[THUMB_WIDTHS.length - 1];
}
function inspShow() {
  const d = insp.doc, n = d.page_count || 1, p = insp.page;
  $("dt-insp-title").textContent = docName(d);
  $("dt-insp-count").textContent = n;
  $("dt-insp-page").max = n; $("dt-insp-page").value = p;
  $("dt-insp-prev").disabled = p <= 1; $("dt-insp-next").disabled = p >= n;
  $("dt-insp-open").href = d.url + (p > 1 ? `?page=${p}` : "");
  const img = $("dt-insp-img"), w = inspWidth();
  $("dt-insp-msg").hidden = false; $("dt-insp-msg").textContent = `Loading page ${p}...`;
  img.style.opacity = .35;
  img.onload = () => {
    if (img.naturalWidth) insp.aspect = img.naturalWidth / img.naturalHeight;
    img.style.opacity = 1; $("dt-insp-msg").hidden = true;
  };
  img.onerror = () => { img.style.opacity = 0; $("dt-insp-msg").textContent = "Couldn't load this page from Wikimedia."; };
  img.alt = `${docName(d)}, page ${p}`;
  img.src = pageThumb(d, p, w);
  if (p < n) new Image().src = pageThumb(d, p + 1, w);   // warm the next page
}
function openInspector(d, page = 1) { insp.doc = d; insp.page = page; $("dt-insp").hidden = false; inspShow(); $("dt-insp-close").focus(); }
function closeInspector() { $("dt-insp").hidden = true; $("dt-insp-img").removeAttribute("src"); insp.doc = null; }
function inspGo(p) { if (!insp.doc) return; insp.page = Math.min(Math.max(1, p), insp.doc.page_count || 1); inspShow(); }

// ---------- single-document editor ----------
async function loadDetail(id) {
  if (state.detail.has(id)) return;
  state.detail.set(id, null);   // loading
  try {
    state.detail.set(id, await api("GET", `/documents/${id}`));
  } catch (e) {
    state.detail.set(id, {error: e.message});
  }
  if (!state.selected.size && state.focus === id) renderEditor();
}
function detailHtml(d) {
  const det = state.detail.get(d.id);
  if (det === undefined || det === null) return `<div class="small text-body-secondary"><span class="spinner-border spinner-border-sm me-1"></span> Loading locations...</div>`;
  if (det.error) return `<div class="small text-danger">Couldn't load locations: ${esc(det.error)}</div>`;
  const locs = det.locations || [];
  const locHtml = locs.length
    ? locs.map(l => `<span class="badge rounded-pill text-bg-light border fw-normal me-1" title="${esc([l.name, l.region, l.country].filter(Boolean).join(", "))}">${esc(l.name)} <span class="text-body-secondary">${esc([l.region, l.country].filter(Boolean).join(", "))}</span></span>`).join("")
    : `<span class="fst-italic text-body-secondary small">${esc(LOCATION_STATUS_TEXT[d.location_status] || "None")}</span>`;
  const pages = (det.owning_pages || []).map(p =>
    `<a href="#" class="dt-open-page small me-2" data-title="${esc(p.title)}" title="Open ${esc(p.title)} in Browse"><i class="bi bi-file-earmark-text"></i> ${esc(p.label || p.title)}</a>`).join("");
  return `<div class="border-top pt-2"><span class="dt-ro-k">Locations</span>${locHtml}</div>
    ${pages ? `<div class="border-top pt-2"><span class="dt-ro-k">Page in Browse</span>${pages}</div>` : ""}`;
}

function renderEditor() {
  const ed = $("dt-editor");
  const n = state.selected.size;
  if (n > 1) return renderBatchEditor(ed);
  const id = n === 1 ? [...state.selected][0] : state.focus;
  const d = id && docById(id);
  if (!d) {
    ed.innerHTML = `<div class="card-body text-center text-body-secondary small py-5">Click a row to edit that document.<br>Tick rows to edit several at once.</div>`;
    return;
  }
  loadDetail(d.id);
  const idx = state.rows.findIndex(x => x.id === d.id);
  const pos = idx >= 0 ? state.page * prefs.pageSize + idx + 1 : null;
  const thumb = pageThumb(d, 1, 120);
  ed.innerHTML = `
    <div class="dt-ed-actions">
      <div class="d-flex gap-2 align-items-center mb-2">
        <button class="btn btn-sm btn-outline-secondary" id="dt-ed-revert" ${state.pending.has(d.id) ? "" : "disabled"}>Revert</button>
        <div class="ms-auto d-flex gap-2">
          <button class="btn btn-sm btn-outline-secondary" id="dt-ed-prev" ${idx > 0 || (idx === 0 && state.page > 0) ? "" : "disabled"} aria-label="Previous document" title="Previous document"><i class="bi bi-chevron-left"></i></button>
          <button class="btn btn-sm btn-outline-primary" id="dt-ed-next" ${idx >= 0 && (idx < state.rows.length - 1 || state.nextCursor) ? "" : "disabled"} aria-label="Next document" title="Next document"><i class="bi bi-chevron-right"></i></button>
        </div>
      </div>
      ${actionButtons([d])}
    </div>
    <div class="card-header bg-body d-flex flex-column gap-1">
      <div class="d-flex align-items-center gap-2">
        ${thumb ? `<button type="button" class="dt-thumb" id="dt-ed-thumb" title="View pages" aria-label="View pages of ${esc(docName(d))}"><img src="${thumb}" alt=""></button>` : ""}
        <a class="dt-label fw-semibold" href="${esc(d.url)}" target="_blank" rel="noopener">${docNameHtml(d)} <i class="bi bi-box-arrow-up-right small"></i></a>
        <span class="ms-auto">${pcBadge(eff(d, "process_code"))}</span>
      </div>
      <div class="small text-body-secondary">${pos ? `${pos}${state.total !== null ? ` of ${state.total}` : ""} in this list` : "Not in the current list"}</div>
    </div>
    <div class="card-body">
      <div class="bg-body-tertiary rounded p-2 small d-flex flex-column gap-1">
        <div>${esc(d.description)}</div>
        <div class="text-body-secondary">${esc(d.native_description)}</div>
        <div class="d-flex gap-3 flex-wrap text-body-secondary">${d.years ? `<span>${esc(d.years)}</span>` : ""}${d.page_count ? `<span>${d.page_count} pages</span>` : ""}<span>${fmtBytes(d.byte_size)}</span></div>
        ${detailHtml(d)}
        ${d.comments ? `<div class="border-top pt-2 dt-ro-comments"><span class="dt-ro-k">Comments</span>${esc(d.comments)}</div>` : ""}
      </div>
      <div class="dt-curated"><div class="dt-curated-title">Curated information</div>
        ${fld("doc_type", "Doc type", pillsHtml("doc_type", DOC_TYPES, eff(d, "doc_type")))}
        ${fld("doc_structure", "Structure", pillsHtml("doc_structure", DOC_STRUCTURES, eff(d, "doc_structure")))}
        ${fld("doc_scope", "Scope", pillsHtml("doc_scope", DOC_SCOPES, eff(d, "doc_scope")))}
        ${fld("language", "Language", pillsHtml("language", LANGUAGES, eff(d, "language")))}
        ${fld("filename", "File name", `<input class="form-control form-control-sm dt-mono" id="dt-ed-filename" value="${esc(eff(d, "filename"))}">`, d.label ? `Default: ${esc(defaultFilename(d.label))}` : "")}
      </div>
      ${eff(d, "anomaly") ? `<div class="alert alert-warning py-2 mb-0 small"><i class="bi bi-exclamation-triangle-fill"></i> <strong>Anomaly:</strong> ${esc(eff(d, "anomaly_comment")) || "(no comment)"}</div>` : ""}
    </div>`;
  markChanged(d);

  const after = () => { markChanged(d); renderGrid(); renderBars(); $("dt-ed-revert").disabled = !state.pending.has(d.id); };
  ed.querySelectorAll("[data-act]").forEach(b => b.onclick = () => openAction([d], b.dataset.act));
  const th = $("dt-ed-thumb");
  if (th) {
    th.onclick = () => openInspector(d);
    th.querySelector("img").onerror = () => th.remove();
  }
  ed.querySelectorAll(".dt-pills[data-field] .btn").forEach(b => b.onclick = () => {
    const f = b.parentElement.dataset.field, v = b.dataset.v, cur = eff(d, f) || [];
    setField(d, f, cur.includes(v) ? cur.filter(x => x !== v) : [...cur, v]);
    b.classList.toggle("btn-secondary"); b.classList.toggle("btn-outline-secondary");
    after();
  });
  $("dt-ed-filename").oninput = e => { setField(d, "filename", e.target.value); after(); };
  $("dt-ed-revert").onclick = () => { state.pending.delete(d.id); render(); };
  ed.querySelectorAll(".dt-open-page").forEach(a => a.onclick = e => {
    e.preventDefault();
    if (typeof load_page_by_title === "function") { load_page_by_title(a.dataset.title); show_tab("nav-browse-tab"); }
  });
  $("dt-ed-prev").onclick = () => step(d, -1);
  $("dt-ed-next").onclick = () => step(d, 1);
}
async function step(d, dlt) {
  const cur = state.rows.findIndex(x => x.id === d.id);
  const ni = cur + dlt;
  state.selected.clear();
  if (ni >= 0 && ni < state.rows.length) {
    state.focus = state.rows[ni].id;
    render();
    const tr = document.querySelector(`#dt-body tr[data-id="${state.focus}"]`); if (tr) tr.scrollIntoView({block: "nearest"});
  } else if (ni >= state.rows.length && state.nextCursor) {
    await loadPage(state.page + 1, {focus: "first"});
  } else if (ni < 0 && state.page > 0) {
    await loadPage(state.page - 1, {focus: "last"});
  }
}
function markChanged(d) {
  const p = state.pending.get(d.id) || {};
  document.querySelectorAll("#dt-editor .dt-fld").forEach(el => el.classList.toggle("dt-changed", el.dataset.fld in p));
}

// ---------- batch editor ----------
const newBatchDraft = () => ({multi: {doc_type: {}, doc_structure: {}, doc_scope: {}, language: {}}});
function applyBatch(docs) {
  // apply the drafted add/remove per value to each document's pending edits
  const b = state.batch;
  docs.forEach(d => Object.entries(b.multi).forEach(([f, m]) => {
    if (!Object.keys(m).length) return;
    let cur = [...(eff(d, f) || [])];
    Object.entries(m).forEach(([v, act]) => { cur = act === "add" ? (cur.includes(v) ? cur : [...cur, v]) : cur.filter(x => x !== v); });
    setField(d, f, cur);
  }));
}
function renderBatchEditor(ed) {
  const docs = [...state.selected].map(docById).filter(Boolean), n = docs.length, b = state.batch || (state.batch = newBatchDraft());
  const dist = {};
  docs.forEach(d => { const c = codeName(eff(d, "process_code")); dist[c] = (dist[c] || 0) + 1; });
  const multiField = (f, values) => {
    const all = [...new Set([...values, ...docs.flatMap(d => eff(d, f) || [])])];
    return `<div class="dt-pills" data-bfield="${f}">${all.map(v => {
      const have = docs.filter(d => (eff(d, f) || []).includes(v)).length, act = b.multi[f][v];
      const cls = act === "add" ? "btn-secondary" : act === "remove" ? "btn-outline-danger dt-rm" : have === n ? "btn-secondary" : have ? "btn-outline-secondary dt-mixed" : "btn-outline-secondary";
      return `<button type="button" class="btn btn-sm rounded-pill ${cls}" data-v="${esc(v)}" title="${have} of ${n} have it. Click to add to all, again to remove from all, again to leave as is.">${act === "add" ? "+ " : ""}${esc(v)}${have && have < n && !act ? ` <span class="small opacity-75">${have}/${n}</span>` : ""}</button>`;
    }).join("")}</div>`;
  };
  const anyChange = Object.values(b.multi).some(m => Object.keys(m).length);
  const codes = Object.keys(dist);
  ed.innerHTML = `
    <div class="dt-ed-actions">
      <div class="d-flex gap-2 align-items-center mb-2">
        <button class="btn btn-sm btn-outline-secondary" id="dt-b-clear">Clear selection</button>
        <button class="btn btn-sm btn-outline-primary ms-auto" id="dt-b-apply" ${anyChange ? "" : "disabled"}>Apply to ${n} documents</button>
      </div>
      ${actionButtons(docs)}
      ${codes.length > 1 ? `<div class="form-text mt-1">These documents have different process codes (${Object.entries(dist).map(([k, v]) => `${esc(k)} ${v}`).join(", ")}), so only the steps they share are offered.</div>` : ""}
    </div>
    <div class="card-header bg-body">
      <div class="fw-semibold">${n} documents selected</div>
      <div class="small text-body-secondary">Fields you don't touch stay as they are on each document.</div>
    </div>
    <div class="card-body">
      <div class="dt-curated"><div class="dt-curated-title">Curated information</div>
        ${fld("doc_type", "Doc type", multiField("doc_type", DOC_TYPES), "Striped = some have it. Click to add to all, again to remove from all.")}
        ${fld("doc_structure", "Structure", multiField("doc_structure", DOC_STRUCTURES))}
        ${fld("doc_scope", "Scope", multiField("doc_scope", DOC_SCOPES))}
        ${fld("language", "Language", multiField("language", LANGUAGES))}
      </div>
    </div>`;
  Object.keys(b.multi).forEach(f => ed.querySelector(`[data-fld="${f}"]`).classList.toggle("dt-changed", Object.keys(b.multi[f]).length > 0));
  // an action also commits the drafted curated changes
  ed.querySelectorAll("[data-act]").forEach(x => x.onclick = () => openAction(docs, x.dataset.act));
  ed.querySelectorAll("[data-bfield] .btn").forEach(x => x.onclick = () => {
    const m = b.multi[x.parentElement.dataset.bfield], v = x.dataset.v;
    m[v] = m[v] === undefined ? "add" : m[v] === "add" ? "remove" : undefined;
    if (m[v] === undefined) delete m[v];
    renderEditor();
  });
  $("dt-b-clear").onclick = () => { state.selected.clear(); selectionChanged(); };
  $("dt-b-apply").onclick = () => {
    applyBatch(docs);
    toast(`Changes staged on ${n} documents. Save them from the yellow bar.`);
    state.batch = newBatchDraft();
    render();
  };
}

// ---------- saving ----------
// POST one request per group of documents whose pending curated edits are
// identical; the action (if any) rides along when every document shares one
// group, otherwise it follows as its own request.
async function commit(docs, actionBody) {
  const groups = new Map();
  docs.forEach(d => {
    const p = state.pending.get(d.id) || {};
    const curated = Object.fromEntries(CURATED.filter(f => f in p).map(f => [f, p[f]]));
    const key = JSON.stringify(curated);
    if (!groups.has(key)) groups.set(key, {curated, ids: []});
    groups.get(key).ids.push(d.id);
  });
  const changed = [];
  const post = async body => { const r = await api("POST", "/documents", body); changed.push(...(r.documents || [])); };
  const list = [...groups.values()];
  if (actionBody && list.length === 1) {
    const g = list[0];
    await post({ids: g.ids, ...actionBody, ...(Object.keys(g.curated).length ? {curated: g.curated} : {})});
  } else {
    for (const g of list) if (Object.keys(g.curated).length) await post({ids: g.ids, curated: g.curated});
    if (actionBody) await post({ids: docs.map(d => d.id), ...actionBody});
  }
  applyChanged(changed, docs.map(d => d.id));
  return changed;
}
// patch the changed rows in place; rows that left this list's filter drop out
function applyChanged(changed, ids) {
  ids.forEach(id => state.pending.delete(id));
  let removed = 0;
  changed.forEach(d => {
    state.known.set(d.id, d);
    state.detail.delete(d.id);
    const i = state.rows.findIndex(x => x.id === d.id);
    if (i < 0) return;
    if (matchesFilter(d)) state.rows[i] = d;
    else { state.rows.splice(i, 1); removed++; state.selected.delete(d.id); }
  });
  if (removed && state.total !== null) state.total = Math.max(0, state.total - removed);
  return removed;
}

// ---------- action dialog ----------
let action = null;
function stamp(from, to) {
  const user = currentUser();
  const day = new Date().toISOString().slice(0, 10);
  const who = (user.name || "").trim();
  let head = `[${day}${who ? " " + who : ""}]`;
  if (to) head += ` ${from === undefined ? "" : codeName(from) + " -> "}${to}`;
  return head + ": ";
}
function openAction(docs, act) {
  const isAnom = act === "anomaly", isClear = act === "clear_anomaly";
  if (isClear) docs = docs.filter(d => eff(d, "anomaly"));
  if (isAnom) docs = docs.filter(d => !eff(d, "anomaly"));
  const n = docs.length, one = n === 1 ? docs[0] : null;
  const from = [...new Set(docs.map(d => eff(d, "process_code") || ""))];
  const who = one ? `<span class="dt-mono">${esc(docName(one))}</span>` : `${n} documents`;
  $("dt-action-title").innerHTML = isClear ? "Clear anomaly" : isAnom ? `<i class="bi bi-exclamation-triangle"></i> Flag anomaly`
    : `Set process code to ${pcBadge(act)}`;
  $("dt-action-summary").innerHTML = isClear
    ? `Clear the anomaly flag and anomaly comment on ${who}.` + (one ? `<div class="small text-body-secondary mt-1">Current comment: ${esc(eff(one, "anomaly_comment")) || "(none)"}</div>` : "")
    : isAnom ? `Flag ${who} as an anomaly. The process code stays ${from.map(pcBadge).join(", ")}.`
    : `Move ${who} from ${from.map(pcBadge).join(", ")} to ${pcBadge(act)}.`;
  // pending curated edits are saved with the action
  if (state.batch && docs.length > 1) applyBatch(docs);
  const curated = docs.flatMap(d => {
    const p = state.pending.get(d.id) || {};
    return CURATED.filter(f => f in p).map(f => `${one ? "" : esc(docName(d)) + " "}${fieldTitle(f)}: ${esc(Array.isArray(p[f]) ? (p[f].join(", ") || "(none)") : p[f])}`);
  });
  $("dt-action-changes").innerHTML = curated.length ? `<li class="list-unstyled">Curated changes saved with it:</li>` + curated.map(c => `<li>${c}</li>`).join("") : "";
  $("dt-action-changes").hidden = !curated.length;
  $("dt-action-text-label").textContent = isAnom ? "Anomaly comment" : "Comment";
  const t = $("dt-action-text");
  if (isAnom) {
    t.value = one ? (eff(one, "anomaly_comment") || "") : "";
    $("dt-action-text-hint").textContent = one ? "What is wrong with this document?" : `Sets the anomaly comment on all ${n}.`;
  } else {
    // the stamp is a suggestion: the user can edit or clear it
    t.value = isClear ? stamp() : stamp(from.length === 1 ? from[0] : undefined, act);
    $("dt-action-text-hint").textContent = `Added on a new line after ${one ? "the document's" : "each document's"} current comments. Clear it to add no comment.`;
  }
  $("dt-action-text-row").hidden = isClear;   // as in the mockup: clearing takes no text
  $("dt-action-extract-row").hidden = act !== "save";
  $("dt-action-extract").checked = true;
  $("dt-action-error").classList.add("d-none");
  $("dt-action-confirm").className = "btn " + (act === "reject" ? "btn-warning" : isAnom ? "btn-danger" : "btn-primary");
  $("dt-action-confirm").textContent = isClear ? "Clear anomaly" : isAnom ? "Flag anomaly" : `Confirm ${act}`;
  $("dt-action-confirm").disabled = false;
  action = {docs, act};
  const modal = bootstrap.Modal.getOrCreateInstance($("dt-action-modal"));
  modal.show();
  $("dt-action-modal").addEventListener("shown.bs.modal", () => t.focus(), {once: true});
}
async function confirmAction() {
  const {docs, act} = action;
  const text = $("dt-action-text").value;
  const isAnom = act === "anomaly";
  if (isAnom && !text.trim()) { $("dt-action-text").focus(); $("dt-action-text-hint").textContent = "Please say what the anomaly is."; return; }
  // a comment that is only the untouched stamp still records who did what
  const comment = !isAnom && act !== "clear_anomaly" && text.trim() ? text.trim() : undefined;
  const body = isAnom ? {action: act, anomaly_comment: text.trim()}
    : {action: act, ...(comment ? {comment} : {}), ...(act === "save" ? {extract: $("dt-action-extract").checked} : {})};
  $("dt-action-confirm").disabled = true;
  try {
    await commit(docs, body);
    bootstrap.Modal.getInstance($("dt-action-modal")).hide();
    const n = docs.length;
    toast(act === "clear_anomaly" ? `Cleared the anomaly on ${n} document(s)` : isAnom ? `Flagged ${n} document(s) as anomalies`
      : `Moved ${n} document(s) to ${act}`);
    afterSave(docs);
  } catch (e) {
    $("dt-action-error").textContent = e.status === 503 ? "The database is unavailable right now; nothing was saved." : e.message;
    $("dt-action-error").classList.remove("d-none");
    $("dt-action-confirm").disabled = false;
  }
}
// keep working where the user was: the same document if it is still listed,
// else the one that moved into its place
function afterSave(docs) {
  const d0 = docs[0];
  const wasBatch = state.selected.size > 1;
  if (wasBatch) { state.selected.clear(); state.batch = null; }
  if (!wasBatch && d0 && !state.rows.some(x => x.id === d0.id)) {
    const idx = Math.min(state.rows.length - 1, Math.max(0, lastIndexOf(d0.id)));
    state.focus = state.rows[idx] ? state.rows[idx].id : null;
  }
  render();
  // rows left the list: the page is now short and later offsets have moved,
  // so refill this page and recount quietly
  if (state.rows.length < prefs.pageSize && (state.nextCursor || state.page > 0)) {
    loadPage(state.page, {withTotal: true});
  }
}
let lastIdx = {};
const lastIndexOf = id => lastIdx[id] ?? 0;

// ---------- columns dialog ----------
let draftCols = [];
function renderColumnsList() {
  const order = [...draftCols, ...COLUMN_DEFS.map(c => c.key).filter(k => !draftCols.includes(k))];
  $("dt-columns-list").innerHTML = order.map(k => {
    const c = COLUMN_DEFS.find(x => x.key === k), on = draftCols.includes(k);
    return `<li class="list-group-item d-flex align-items-center gap-2 py-1 ${on ? "" : "text-body-secondary"}">
      <input class="form-check-input mt-0" type="checkbox" id="dt-col-${k}" data-col="${k}" ${on ? "checked" : ""}>
      <label class="form-check-label" for="dt-col-${k}">${esc(c.title)}</label>
      <span class="ms-auto" ${on ? "" : "hidden"}>
        <button class="btn btn-sm btn-link py-0" data-up="${k}" aria-label="Move ${esc(c.title)} up"><i class="bi bi-arrow-up"></i></button>
        <button class="btn btn-sm btn-link py-0" data-down="${k}" aria-label="Move ${esc(c.title)} down"><i class="bi bi-arrow-down"></i></button>
      </span></li>`;
  }).join("");
  document.querySelectorAll("#dt-columns-list [data-col]").forEach(cb => cb.onchange = () => {
    const k = cb.dataset.col;
    draftCols = cb.checked ? [...draftCols, k] : draftCols.filter(x => x !== k);
    renderColumnsList();
  });
  const move = (k, dlt) => {
    const i = draftCols.indexOf(k), j = i + dlt;
    if (j < 0 || j >= draftCols.length) return;
    [draftCols[i], draftCols[j]] = [draftCols[j], draftCols[i]];
    renderColumnsList();
  };
  document.querySelectorAll("#dt-columns-list [data-up]").forEach(b => b.onclick = () => move(b.dataset.up, -1));
  document.querySelectorAll("#dt-columns-list [data-down]").forEach(b => b.onclick = () => move(b.dataset.down, 1));
}

// ---------- pending / review & save ----------
let discardArmed = false;
function resetDiscard() { discardArmed = false; $("dt-pending-discard").textContent = "Discard all"; }
const show = v => Array.isArray(v) ? (v.length ? v.join(", ") : "(none)") : (v === "" || v === null || v === undefined ? "(empty)" : v);

// ---------- wiring ----------
function wire() {
  $("dt-anomaly-only").onchange = e => { prefs.anomalyOnly = e.target.checked; savePrefs(); reload(); };
  let prefixTimer;
  $("dt-prefix").oninput = e => {
    clearTimeout(prefixTimer);
    prefixTimer = setTimeout(() => { prefs.prefix = e.target.value; savePrefs(); reload(); }, 400);
  };
  $("dt-prefix-clear").onclick = () => { $("dt-prefix").value = ""; prefs.prefix = ""; savePrefs(); reload(); };
  $("dt-refresh").onclick = () => { state.detail.clear(); loadPage(state.page, {withTotal: true}); };
  $("dt-page-size").onchange = e => { prefs.pageSize = +e.target.value; savePrefs(); reload(); };
  $("dt-page-prev").onclick = () => { if (state.page > 0) loadPage(state.page - 1, {focus: "first"}); };
  $("dt-page-next").onclick = () => { if (state.nextCursor) loadPage(state.page + 1, {focus: "first"}); };

  $("dt-open-columns").onclick = () => { draftCols = [...prefs.columns]; renderColumnsList(); bootstrap.Modal.getOrCreateInstance($("dt-columns-modal")).show(); };
  $("dt-columns-reset").onclick = () => { draftCols = [...DEFAULT_COLUMNS]; renderColumnsList(); };
  $("dt-columns-apply").onclick = () => {
    prefs.columns = [...draftCols]; savePrefs();
    bootstrap.Modal.getInstance($("dt-columns-modal")).hide();
    renderGrid();
  };

  $("dt-action-confirm").onclick = confirmAction;

  $("dt-pending-discard").onclick = () => {
    if (!discardArmed) { discardArmed = true; $("dt-pending-discard").textContent = `Discard changes on ${state.pending.size} document(s)?`; return; }
    state.pending.clear(); render(); toast("Changes discarded");
  };
  $("dt-pending-review").onclick = () => {
    $("dt-review-body").innerHTML = [...state.pending.entries()].map(([id, p]) => {
      const d = docById(id), fields = CURATED.filter(f => f in p);
      return fields.map((f, i) => `<tr>
        ${i === 0 ? `<td class="dt-mono" rowspan="${fields.length}">${docNameHtml(d)}</td>` : ""}
        <td class="dt-mono text-body-secondary">${f}</td>
        <td><span class="text-body-secondary text-decoration-line-through">${esc(show(d[f]))}</span> &rarr; ${esc(show(p[f]))}</td></tr>`).join("");
    }).join("");
    $("dt-review-error").classList.add("d-none");
    $("dt-review-save").disabled = false;
    bootstrap.Modal.getOrCreateInstance($("dt-review-modal")).show();
  };
  $("dt-review-save").onclick = async () => {
    const docs = [...state.pending.keys()].map(docById).filter(Boolean);
    $("dt-review-save").disabled = true;
    try {
      await commit(docs, null);
      bootstrap.Modal.getInstance($("dt-review-modal")).hide();
      toast(`Saved ${docs.length} document(s)`);
      render();
    } catch (e) {
      $("dt-review-error").textContent = e.status === 503 ? "The database is unavailable right now; nothing more was saved." : e.message;
      $("dt-review-error").classList.remove("d-none");
      $("dt-review-save").disabled = false;
      render();   // groups saved before the failure are already applied
    }
  };

  $("dt-insp-close").onclick = closeInspector;
  $("dt-insp-prev").onclick = () => inspGo(insp.page - 1);
  $("dt-insp-next").onclick = () => inspGo(insp.page + 1);
  $("dt-insp-page").onchange = e => inspGo(parseInt(e.target.value, 10) || 1);
  document.addEventListener("keydown", e => {
    if ($("dt-insp").hidden) return;
    if (e.key === "Escape") { closeInspector(); e.stopImmediatePropagation(); }
    else if (e.target.id !== "dt-insp-page" && (e.key === "ArrowRight" || e.key === "PageDown")) { inspGo(insp.page + 1); e.preventDefault(); }
    else if (e.target.id !== "dt-insp-page" && (e.key === "ArrowLeft" || e.key === "PageUp")) { inspGo(insp.page - 1); e.preventDefault(); }
  }, true);

  // remember where each row sat, so afterSave can pick its neighbour
  $("dt-body").addEventListener("click", () => { lastIdx = Object.fromEntries(state.rows.map((d, i) => [d.id, i])); }, true);
  $("dt-editor").addEventListener("click", () => { lastIdx = Object.fromEntries(state.rows.map((d, i) => [d.id, i])); }, true);
}

function fillPrefixSuggestions() {
  // archive labels, when the Archives tab has loaded them
  try {
    if (typeof archives !== "undefined" && Array.isArray(archives)) {
      $("dt-prefix-list").innerHTML = archives.map(a => `<option value="${esc(a.label)}"></option>`).join("");
    }
  } catch (e) { /* not loaded yet */ }
}

async function start(opts = {}) {
  fillPrefixSuggestions();
  if (!state.started) {
    state.started = true;
    wire();
    await loadPrefs();
  }
  if (opts.prefix !== undefined) prefs.prefix = opts.prefix;
  if (opts.process_code !== undefined) prefs.pcFilter = opts.process_code;
  if (opts.anomalies_only !== undefined) prefs.anomalyOnly = !!opts.anomalies_only;
  $("dt-prefix").value = prefs.prefix || "";
  await reload();
}

// Entry point for other tabs, e.g. open_documents({prefix: "DAHMO/5/1"}).
// Options: prefix, process_code ("" = blank), anomalies_only.
window.open_documents = function (opts = {}) {
  const tab = $("nav-documents-tab");
  if (!tab) return false;
  bootstrap.Tab.getOrCreateInstance(tab).show();
  start(opts);
  return true;
};

document.addEventListener("DOMContentLoaded", () => {
  const tab = $("nav-documents-tab");
  if (!tab) return;
  tab.addEventListener("shown.bs.tab", () => { if (!state.started) start(); });
});
})();
