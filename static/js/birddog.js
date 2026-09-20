// (c) 2025 Jonathan Brandt
// Licensed under the MIT License. See LICENSE file in the project root.

// ---------------------------------------------------------------------------
// APP GLOBALS
var current_page            = null;
var current_user            = null;
var archives                = null;
var watchlist               = null;
var unresolved_updates      = {};
var hide_insignificant      = false; // set from hide_insignificant_pref in on_loaded()
// watch titles currently folded to a single group-header row in the alerts
// table -- session-only UI state, deliberately not a persisted preference
// (same treatment the old tree view gave its expanded/collapsed nodes)
var collapsed_watches       = new Set();
// issue #138 Stage 4: paths checked for bulk resolve. A checkbox's checked
// state never changes just because its row stops being rendered (hidden by
// the minor-changes toggle, or its watch collapsing) -- it persists here
// across re-renders, same as the old tree's checked-independent-of-hidden
// behavior confirmed during design. rendered_paths is rebuilt on every
// render_unresolved_items() call and is exactly path_to_node's key set at
// that moment; kept separately so update_bulk_bar() doesn't need to re-walk
// unresolved_updates just to know what's currently on screen.
var checked_paths           = new Set();
var rendered_paths          = new Set();
var last_checked_path       = null;

const months                = [
    'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
    'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'
    ];

const closed_icon = "bi-plus-circle-fill";
const open_icon = "bi-dash-circle-fill";

const ROOT_HUB_TITLE = "Архів:Архіви";
const ROOT_HUB_LABEL = "HOME";

// ---------------------------------------------------------------------------
// HELPER FUNCTIONS

// return translated text if present, otherwise original
function get_text(item) {
    return "en" in item? item.en : item.uk;
}

function empty(item) {
    return item == null || item == '';
}

// check if valid link
function is_linked(item) {
    return item != null && 
        item.link != null && 
        item.exists && 
        !item.link.includes("redlink") &&
        item.link.startsWith("/wiki/");
}

function format_date(mod_date, strip_time=false) {
    if (!mod_date)
        return '';
    // UTC ISO8601, e.g. "2025-10-09T10:36:46Z" -> "2025-10-09 10:36:46"
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(mod_date)) {
        const result = mod_date.slice(0, -1).replace('T', ' ');
        if (strip_time)
            return result.slice(0, 10);
        return result.replace(/ 00:00:00$/, '');
    }
    // legacy "YYYY,MM,DD,HH:MM"
    const parsed = mod_date.split(',');
    if (parsed.length <= 1)
        return mod_date;
    let result = `${parsed[2]} ${months[Number(parsed[1])-1]} ${parsed[0]}`;
    if (parsed.length > 3 && !strip_time)
         result += ` ${parsed[3]}`;
    return result;
}

function show(elem_id) {
    document.getElementById(elem_id).classList.remove('d-none');
}

function hide(elem_id) {
    document.getElementById(elem_id).classList.add('d-none');
}

function show_if(elem_id, visible)
{
    if (visible)
        show(elem_id);
    else
        hide(elem_id);
}

function enable_if(elem_id, enabled)
{
    if (enabled)
        document.getElementById(elem_id).classList.remove('disabled');
    else
        document.getElementById(elem_id).classList.add('disabled');
}

function show_tab(tab_id) {
    console.log(`showing tab: ${tab_id}`);
    const tab = new bootstrap.Tab(document.getElementById(tab_id));
    tab.show();
}

const _TRANSLATE_POLL_INTERVAL_MS = 1000;
const _TRANSLATE_NO_PROGRESS_TIMEOUT_MS = 15 * 1000; // give up if progress stalls for 15s
let _translate_last_progress = null;
let _translate_last_progress_at = null;

async function update_translation_progress(data) {
    console.log('translate result:', data);

    hide('progress-container');
    hide('translating-badge');

    if (!data.available) {
        alert("Translation service is temporarily unavailable. Please try again later.");
        load_page_by_title(current_page.title, compare=current_page.refmod ?? null);
        return;
    }

    const translations = data.translations || [];
    let current_progress = null;
    for (const item of translations) {
        //console.log(item.page_name, current_page.name);
        if (item.title == current_page.title) {
            current_progress = item.progress;
            const progress_bar = document.getElementById("progress-bar");
            if (progress_bar) {
                const percent = (100. * item.progress / item.total).toFixed(1);
                progress_bar.style.width = `${percent}%`;
                progress_bar.setAttribute("aria-valuenow", percent);
                progress_bar.textContent = ''; //`${percent}%`;
            }
            show('progress-container');
            show('translating-badge');
            enable_if("translate-btn", false);
            break;
        }
    }

    if (translations.length > 0) {
        // Track progress changes to detect a stuck task.
        const now = Date.now();
        if (current_progress !== _translate_last_progress) {
            _translate_last_progress = current_progress;
            _translate_last_progress_at = now;
        }
        const stalled_ms = now - (_translate_last_progress_at ?? now);
        if (stalled_ms >= _TRANSLATE_NO_PROGRESS_TIMEOUT_MS) {
            console.warn(`Translation progress stalled for ${stalled_ms}ms — task may be stuck. Reloading.`);
            _translate_last_progress = null;
            _translate_last_progress_at = null;
            hide('progress-container');
            hide('translating-badge');
            enable_if("translate-btn", true);
            const reload = confirm(
                "Translation seems to be taking longer than expected and may be stuck.\n\n" +
                "Click OK to reload the page (translation may already be complete), " +
                "or Cancel to leave the page as-is."
            );
            if (reload) {
                load_page_by_title(current_page.title, compare=current_page.refmod ?? null);
            }
            return;
        }

        // Continue polling after 1 second
        setTimeout(async () => {
            try {
                console.log('checking translation progress...')
                const response = await fetch('/translate');
                if (!response.ok) {
                    if (response.status === 404) {
                        alert('Your session may have expired. Please log in again.');
                        location.reload();
                        return;
                    }
                    throw new Error(`Polling failed: ${response.statusText}`);
                }
                const new_data = await response.json();
                update_translation_progress(new_data);
            } catch (err) {
                console.error("Polling error:", err);
            }
        }, _TRANSLATE_POLL_INTERVAL_MS);
    }
    else {
        _translate_last_progress = null;
        _translate_last_progress_at = null;
        // reload in case we're on the translated page
        // FIXME: don't do this if not on a translated page
        load_page_by_title(current_page.title, compare=current_page.refmod ?? null);
    }
}

function get_resolve_info(page_title) {
  const updates = window.unresolved_updates;
  for (const prefix in updates) {
    if (page_title.startsWith(prefix)) {
      const entries = updates[prefix];
      for (const [title, obj] of entries) {
        if (title === page_title) {
          return obj;
        }
      }
    }
  }
  return null;
}

function is_navigable_unresolved(obj) {
    // a real entry (not a synthesized tree-path placeholder -- see
    // is_real_entry()) that isn't currently hidden by the issue #138
    // "hide minor changes" toggle
    return obj.hasOwnProperty("modified") && !(hide_insignificant && obj.insignificant);
}

function get_next_unresolved_item(page_title) {
    const updates = window.unresolved_updates;
    // archive order must match render_unresolved_items()'s on-screen order
    // (sorted by displayed label), not object key insertion order -- which
    // is actually nondeterministic here, since check_all_watchlists() fires
    // one fetch per watch concurrently and each writes unresolved_updates[title]
    // whenever its own request happens to resolve -- nor raw-title
    // lexicographic order, which doesn't match the label sort either
    const sorted_prefixes = Object.keys(updates).sort(
        (a, b) => label_for_watch_title(a).localeCompare(label_for_watch_title(b))
    );

    // first pass: look within current archive. entries is already in the
    // server's correct tree order (unresolved_tree()/_sort_keys() in
    // watcher.py -- the same numeric-aware, prefix-grouped order the tree
    // view renders in), so walk it by position instead of re-deriving
    // "next" via plain lexicographic comparison on full title paths, which
    // ignored that ordering entirely. If page_title isn't itself a node in
    // the list (e.g. browsing a page with no unresolved status), fall back
    // to the first unresolved entry in this archive.
    let current_prefix_index = -1;
    for (let p = 0; p < sorted_prefixes.length; p++) {
        const prefix = sorted_prefixes[p];
        if (page_title.startsWith(prefix)) {
            current_prefix_index = p;
            const entries = updates[prefix];
            const current_index = entries.findIndex(([title]) => title === page_title);
            const start = current_index === -1 ? 0 : current_index + 1;
            for (let i = start; i < entries.length; i++) {
                const [, obj] = entries[i];
                if (is_navigable_unresolved(obj)) {
                    return obj;
                }
            }
            break;
        }
    }

    // second pass: look to the next archive in display order
    const start_p = current_prefix_index === -1 ? 0 : current_prefix_index + 1;
    for (let p = start_p; p < sorted_prefixes.length; p++) {
        const entries = updates[sorted_prefixes[p]];
        for (const [title, obj] of entries) {
            if (is_navigable_unresolved(obj)) {
                return obj;
            }
        }
    }

    // third pass: wrap around to the first unresolved item overall
    for (const prefix of sorted_prefixes) {
        const entries = updates[prefix];
        for (const [title, obj] of entries) {
            if (is_navigable_unresolved(obj)) {
                return obj;
            }
        }
    }

    return null;
}

function find_archive(title) {
    return archives.find(a => a.title === title) || null;
}

function label_for_watch_title(title) {
    const watch_entry = watchlist && watchlist.find(w => w.title === title);
    if (watch_entry) return watch_entry.label;
    const archive = find_archive(title);
    if (archive) return archive.label;
    return title;
}

// Helper to safely escape values for HTML attributes
function escape_attr(value) {
    if (value === null || value === undefined) {
        return '';
    }
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// ---------------------------------------------------------------------------
// Browse panel scroll position

// Save/restore scroll positions for the page_table across pages

function get_scroll_position() {
    return window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;
}

function set_scroll_position(pos) {
    window.scrollTo({
      top: pos,
      behavior: 'smooth'
    });
}

// per page scroll memory (keyed on page name)
const scroll_positions = {};

function save_scroll_position() {
    const page_name = current_page?.name;
    if (!page_name) return;
    const position = get_scroll_position();
    scroll_positions[page_name] = position;
    console.log(`Saved scroll position: ${page_name}: ${position}`);
}

function restore_scroll_position() {
    const page_name = current_page?.name;
    if (page_name && page_name in scroll_positions) {
        position = scroll_positions[page_name];
        set_scroll_position(position);
        console.log(`Restored scroll position: ${page_name}: ${position}`);
    }
}

// Save/restore scroll position per top-level nav tab (Alerts, Watchlists,
// Browse, ...) across tab switches, same mechanism as the browse-page memory
// above. Bootstrap's tab component already fires hide.bs.tab (on the tab
// being left, right before the new one shows) and shown.bs.tab (on the tab
// now active, right after it shows) and both bubble, so one pair of
// listeners on the nav container covers every tab with no per-tab wiring.
const tab_scroll_positions = {};

function init_tab_scroll_persistence() {
    const nav = document.getElementById('nav-brand-tab');
    if (!nav) return;
    nav.addEventListener('hide.bs.tab', e => {
        tab_scroll_positions[e.target.getAttribute('href')] = get_scroll_position();
    });
    nav.addEventListener('shown.bs.tab', e => {
        set_scroll_position(tab_scroll_positions[e.target.getAttribute('href')] ?? 0);
    });
}

// ---------------------------------------------------------------------------
// BIRDDOG SERVICE CALLS

// page loader
async function load_page_by_title(page_title, compare=null) {
    try {
        save_scroll_position();
        var url = `/page?title=${page_title}`;
        if (compare != null)
            url += `&compare=${compare}`
        console.log(`Fetching data from: ${url}`);

        // Show the spinner
        show('browse-spinner');
        hide('browse-page-content');

        // Make the GET request
        const response = await fetch(url, {
            method: 'GET',
            headers: {
                'Accept': 'application/json'
            }
        });

        if (!response.ok) {
            hide('browse-spinner');
            show('browse-page-content');
            if (response.status === 404) {
                alert('Your session may have expired. Please log in again.');
                location.reload();
                return;
            }
            throw new Error(`HTTP error! Status: ${response.status}`);
        }

        // Parse the JSON response
        const data = await response.json();
        console.log('Data loaded:', data);

        current_page = data;

        // Process and display the data
        render_page_data(data);

        // Populate the history dropdown
        render_history(data)

        // after delay to get the page populated, update scroll
        setTimeout(restore_scroll_position, 100);

        // Hide the spinner after loading
        hide('browse-spinner');
        show('browse-page-content');
    } catch (error) {
        console.error('Error loading page:', error.message);
        alert(`Failed to load data: ${error.message}`);
    }
}

async function translate_page() {
    const page_title = current_page.title;
    console.log('translating:', page_title);
    show('translating-badge');
    enable_if("translate-btn", false);
    const response = await fetch(`/translate?title=${page_title}`);
    if (!response.ok) {
        if (response.status === 404) {
            alert('Your session may have expired. Please log in again.');
            location.reload();
            return;
        }
        throw new Error(`Failed during translation progress check: ${response.statusText}`);
    }
    const data = await response.json();
    console.log('translate_page:', data)
    update_translation_progress(data);
}

function update_database() {
  // Display current page title in the modal (use whatever your current selected title variable is)
  const title = document.getElementById("page-title")?.textContent?.trim() || "-";
  document.getElementById("db-update-page-title").textContent = title;

  // Default deep update off (or preserve prior state if you prefer)
  document.getElementById("db-update-deep-checkbox").checked = false;

  const modal_el = document.getElementById("dbUpdateConfirmModal");
  const modal = bootstrap.Modal.getOrCreateInstance(modal_el);
  modal.show();
}

async function do_database_update(deep=false) {
    const page_title = current_page.title;
    console.log('database update:', page_title);
    
    const response = await fetch(`/database_update?title=${page_title}&deep=${deep}`);
    if (!response.ok) {
        if (response.status === 404) {
            alert('Your session may have expired. Please log in again.');
            location.reload();
            return;
        }
        throw new Error(`Failed during database update request: ${response.statusText}`);
    }
    const data = await response.json();
    console.log('update_database:', data);
}

function sanitize_id(column_name) {
  return 'export_' + column_name.toLowerCase().replace(/\s+/g, '_');
}

function _labelize(s) {
  // Display-only: keep original in `value`
  return String(s).replaceAll(",", "\,");
}

function render_export_column_assignments(table_name, column_classes, column_headers, column_header_map) {
  const column_container = document.getElementById("export-columns-container");
  column_container.innerHTML = "";
  const tagify_map = {};

  column_classes.forEach(column_class => {
    const column_id = sanitize_id(column_class);
    const div = document.createElement("div");
    div.className = "mb-3";
    div.innerHTML = `
      <label class="form-label">Export Column: ${column_class}</label>
      <input class="form-control tag-input" id="${column_id}" placeholder="Select source columns...">
    `;
    column_container.appendChild(div);
  });

  const headers = column_headers[table_name] || [];
  const whitelist = headers.map(h => ({ value: h, label: _labelize(h) }));

  column_classes.forEach(column_class => {
    const input_id = sanitize_id(column_class);
    const input = document.getElementById(input_id);
    if (!input) return;

    const tagify = new Tagify(input, {
      whitelist,
      tagTextProp: "label", // show label on the tag
      dropdown: {
        enabled: 0,
        fuzzySearch: true,
        position: "auto",
        searchKeys: ["value", "label"]
      },
      enforceWhitelist: true,
      duplicates: true
    });

    tagify_map[column_class] = tagify;

    const header_indices = column_header_map?.[table_name]?.[column_class];
    if (header_indices !== undefined) {
      tagify.removeAllTags();
      header_indices.forEach(i => {
        const h = headers[i];
        if (h) tagify.addTags([{ value: h, label: _labelize(h) }]);
      });
    }
  });

  return tagify_map;
}

async function open_export_modal() {
  try {
    const page_title = current_page.title;
    const response = await fetch(`/export?title=${encodeURIComponent(page_title)}`);
    if (!response.ok) {
      if (response.status === 404) {
        alert('Your session may have expired. Please log in again.');
        location.reload();
        return;
      }
      throw new Error(`Failed to load export config: ${response.status}`);
    }

    const data = await response.json();
    console.log("export data:", data);

    const {
      column_headers,
      column_classes,
      column_header_map,
      default_template,
      default_table,
      templates,
      title: export_page_title
    } = data;

    const num_tables = Object.keys(column_headers).length;

    // Set modal title
    document.getElementById("exportModalLabel").textContent = `Download: ${export_page_title}`;

    // Populate the template dropdown
    const template_select = document.getElementById("templateSelect");
    template_select.innerHTML = templates.map(template => {
      const display_name = template
        .replace(/\.xlsx$/i, '')
        .replace(/_/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase());
      return `<option value="${template}">${display_name}</option>`;
    }).join("");
    template_select.value = default_template;

    // populate the table selection dropdown (if needed)
    if (num_tables > 1) {
        // more than one table
        const table_select = document.getElementById("tableSelect");
        table_select.innerHTML = Object.keys(column_headers).map(table_name => {
            return `<option value="${table_name}">${table_name}</option>`;
        }).join("");
        table_select.value = default_table;

        // Attach event listener to update column assignments on change
        table_select.addEventListener("change", (event) => {
            const selected_table = event.target.value;
            const new_tagify_map = render_export_column_assignments(
                selected_table,
                column_classes,
                column_headers,
                column_header_map
            );
            window._export_tagify_map = new_tagify_map;
        });

        show("table-select-container");
    }
    else {
        hide("table-select-container");
    }

    // Render column assignment inputs
    let tagify_map = {};
    if (num_tables > 0) {
        tagify_map = render_export_column_assignments(default_table, column_classes, column_headers, column_header_map);
        show("export-columns-container");
    }
    else {
        hide("export-columns-container");
        document.getElementById("tableSelect").value = "";
    }

    // Save for use on submit
    window._export_tagify_map = tagify_map;
    window._export_column_classes = column_classes;
    window._export_page_title = export_page_title;
    window._export_compare = current_page.refmod ?? null;
    window._export_column_headers = column_headers;
    window._export_num_tables = num_tables;

    // Show the modal
    const export_modal = new bootstrap.Modal(document.getElementById("exportModal"));
    export_modal.show();

  } catch (err) {
    console.error("Failed to open export modal:", err);
    alert("Unable to load export configuration.");
  }
}


// ---------------------------------------------------------------------------
// UI RENDERING AND HANDLERS

// handle table row click
function on_row_click(table_data, index) {
    //console.log(`click on:  ${page_data.title}[${index}]`);
    const child_title = table_data.children[index][0].link.replace("/wiki/","");
    console.log(`on_row_click: child title = ${child_title}`)
    load_page_by_title(child_title);
}

// insert a table
function render_table(table_element, table_data, is_comparison) {
    const children = table_data.children;
    const header = table_data.header;
    const header_elem = table_element.querySelector('thead');
    var row = '<tr>';
    header.forEach((item, index) => {
        row += `<th>${get_text(item) || ''}</th>`;
    });
    row += '</tr>';
    header_elem.innerHTML = row;

    const body_elem = table_element.querySelector('tbody');
    body_elem.innerHTML = ''; // Clear existing content

    var row_added = false;
    var any_edit = false;
    children.forEach((child, index) => {
        var row_edited = false;
        const row_elem = document.createElement('tr');
        child.forEach((item, index) => {
            const cell_elem = document.createElement('td');
            var cell_content = '';
            cell_content += get_text(item.text) || '';
            if (is_comparison && 'edit' in item) {
                switch (item.edit) {
                case 'added':
                    cell_elem.classList.add('table-success');
                    row_edited = true;
                    break;
                case 'changed':
                    cell_elem.classList.add('table-warning');
                    row_edited = true;
                    break;
                default:
                    break;
                }
            }
            if (is_comparison && 'link_edit' in item) {
                switch (item.link_edit) {
                case 'added':
                    //console.log('link added:', cell_content);
                    cell_content =
                        `<button class="btn btn-success btn-sm" style="opacity: 0.5;">
                            <i class="bi bi-link-45deg"></i>
                        </button> &nbsp;` + cell_content;
                    row_edited = true;
                    break;
                case 'changed':
                    //console.log('link changed:', cell_content);
                    cell_content =
                        `<button class="btn btn-warning btn-sm" style="opacity: 0.5;">
                            <i class="bi bi-link-45deg"></i>
                        </button> &nbsp;` + cell_content;
                    row_edited = true;
                    break;
                default:
                    break;
                }
            }
            cell_elem.innerHTML = cell_content;
            row_elem.appendChild(cell_elem)
        });
        if (row_edited)
            any_edit = true;
        if (!is_comparison || row_edited) {
            // only add the row if not doing comparison or there is a change to show
            if (is_linked(child[0])) {
                // Add click event listener
                row_elem.addEventListener('click', () => on_row_click(table_data, index));
            }
            else {
                row_elem.classList.add('table-secondary', 'disabled');
                row_elem.style.pointerEvents = 'none';
                row_elem.style.opacity = '0.4'; // Dim for better visibility
            }

            body_elem.appendChild(row_elem);
            row_added = true;
        }
    });
    return any_edit;
}

// render a data page
function render_page_data(data) {
    const false_comparison = 'refmod' in data && data.refmod >= data.lastmod
    // a comparison happened whenever compare() was invoked at all (refmod is
    // set) -- NOT only when the reference turns out to differ from lastmod.
    // refmod === lastmod is exactly the "compared, found zero differences"
    // case the no-differences-badge exists to report; excluding it here used
    // to suppress that badge (and all edit-flag rendering) for precisely the
    // page state it's meant to surface (found investigating issue #138).
    var is_comparison = 'refmod' in data && !!data.refmod;
    var resolve_enable = needs_resolve(data);

    if (resolve_enable && data.lastmod == "") {
        // not an actual update - offer to simply resolve it
        if (confirm("This page no longer exists. It is safe to clear this page's unresolved status. Click OK to clear it.")) {
            resolve_page_update(watch_title_for_page(data), data.title, deep=false);
            data.refmod = null;
            is_comparison = false;
            resolve_enable = false;
        }
    }
    if (resolve_enable && false_comparison) {
        // check for false alarm in page update
        const resolve_info = get_resolve_info(data.title);
        if (resolve_info && resolve_info.last_resolved > data.lastmod) {
            // not an actual update - offer to simply resolve it
            if (confirm("The latest modification of this page precedes the comparison date due to a false change detection. It is safe to clear this page's unresolved status. Click OK to clear it.")) {
                resolve_page_update(watch_title_for_page(data), data.title, deep=false);
                data.refmod = null;
                is_comparison = false;
                resolve_enable = false;
            }
        }
    }

    const title_elem = document.getElementById('page-title');
    title_elem.textContent = data.name.replace("-_","");

    var any_edit = false;
    const desc_elem = document.getElementById('page-description');
    desc_elem.textContent = get_text(data.description);
    desc_elem.classList.remove('bg-warning', 'bg-success');
    if (is_comparison && 'edit' in data.description) {
        switch (data.description.edit) {
            case 'added':
                desc_elem.classList.add('bg-success');
                any_edit = true;
                break;
            case 'changed':
                desc_elem.classList.add('bg-warning');
                any_edit = true;
                break;
            default:
                break;
        }
    }

    const dates_elem = document.getElementById('page-dates');
    dates_elem.textContent = get_text(data.dates);
    dates_elem.classList.remove('bg-warning', 'bg-success');
    if (is_comparison && 'edit' in data.dates) {
        switch (data.dates.edit) {
            case 'added':
                dates_elem.classList.add('bg-success');
                any_edit = true;
                break;
            case 'changed':
                dates_elem.classList.add('bg-warning');
                any_edit = true;
                break;
            default:
                break;
        }
    }

    const doc_link_elem = document.getElementById('page-doc-link');
    const doc_url = data.doc_link;
    doc_link_elem.textContent = doc_url;
    doc_link_elem.classList.remove('bg-warning', 'bg-success');
    if (is_comparison && 'doc_link_edit' in data) {
        switch (data.doc_link_edit) {
            case 'added':
                doc_link_elem.classList.add('bg-success');
                any_edit = true;
                break;
            case 'changed':
                doc_link_elem.classList.add('bg-warning');
                any_edit = true;
                break;
            default:
                break;
        }
    }
    if (doc_url.length > 0) {
        doc_link_elem.setAttribute('href', doc_url);
        show('page-doc-link')
    }
    else {
        hide('page-doc-link')
    }

    const lastmod = document.getElementById('last-modified');
    lastmod.textContent = format_date(data.lastmod);

    const source_link_elem = document.getElementById('source-link');
    source_link_elem.setAttribute('href', data.link);

    // render all tables
    const container = document.getElementById('page-table-container');
    container.innerHTML = '';  // Clear existing content
    let any_children = false;

    if (data.tables.length === 1) {
        const table_data = data.tables[0];
        if (table_data.children.length > 0) {
            any_children = true;
        }

        const table = document.createElement('table');
        table.className = 'table table-striped table-hover';
        table.innerHTML = '<thead class="table-light position-sticky top-0" style="z-index: 1;"></thead><tbody></tbody>';

        container.appendChild(table);
        if (render_table(table, table_data, is_comparison)) {
            any_edit = true;
        }
    } else {
        data.tables.forEach((table_data, i) => {
            const section = document.createElement('div');
            section.className = 'mb-3';

            const headerId = `tableHeading${i}`;
            const collapseId = `tableCollapse${i}`;
            const is_first = i === 0;

            section.innerHTML = `
              <div class="d-flex align-items-center mb-2">
                <button class="btn btn-link d-flex align-items-center" data-bs-toggle="collapse" data-bs-target="#${collapseId}" aria-expanded="${is_first}" aria-controls="${collapseId}">
                  <i class="bi ${is_first ? open_icon : closed_icon} me-2" id="icon-${i}"></i>
                  ${table_data.name}
                </button>
              </div>
              <div id="${collapseId}" class="collapse ${is_first ? 'show' : ''}">
                <table class="table table-striped table-hover">
                  <thead class="table-light position-sticky top-0" style="z-index: 1;"></thead>
                  <tbody></tbody>
                </table>
              </div>
            `;

            container.appendChild(section);

            // Attach event listener to update icon
            const collapseEl = section.querySelector(`#${collapseId}`);
            collapseEl.addEventListener('show.bs.collapse', () => {
                const icon = section.querySelector(`#icon-${i}`);
                icon.classList.remove(closed_icon);
                icon.classList.add(open_icon);
            });
            collapseEl.addEventListener('hide.bs.collapse', () => {
                const icon = section.querySelector(`#icon-${i}`);
                icon.classList.remove(open_icon);
                icon.classList.add(closed_icon);
            });

            const table = section.querySelector('table');
            if (table_data.children.length > 0) {
                any_children = true;
            }
            if (render_table(table, table_data, is_comparison)) {
                any_edit = true;
            }
        });
    }

    show_if('comparing-badge', is_comparison);
    show_if('no-differences-badge', is_comparison && !any_edit);
    show_if('empty-page-badge', !any_children);
    show_if('needs-resolve-badge', resolve_enable);

    // set button enables
    enable_if("resolve-btn", resolve_enable);
    enable_if("next-unresolved-btn", get_next_unresolved_item(current_page.title) != null);
    enable_if("translate-btn", data.needs_translation);

    render_breadcrumbs(data);
    update_archive_select();
}

function render_history(data) {
    // "NEW PAGE" and "does a version dropdown make sense" are separate
    // questions. NEW PAGE: either literally only one revision ever exists,
    // or (issue #138) the entire history postdates whatever date was
    // requested via ?compare= -- either way the user has never seen an
    // earlier state via that automatic comparison. The dropdown itself only
    // needs real history to exist (more than one revision) -- even when the
    // automatic cutoff-based comparison found nothing, the user can still
    // manually pick one of those real revisions to compare against.
    const has_real_history = data.history.length > 1;
    show_if('new-page-badge', !has_real_history || !!data.no_earlier_version);

    if (!has_real_history) {
        hide('history-selection-box');
    } else {
        show('history-selection-box');

        const selector = document.getElementById('version-select');
        const select_header = 'refmod' in data && data.refmod != null? 'Stop Comparing' : 'Select Version';
        selector.innerHTML = `<option value="" selected>${select_header}</option>`;
        selector.disabled = false;

        // Create a sorted list of eligible history items (excluding current lastmod)
        const eligible_history = data.history
            .filter(item => item.modified !== data.lastmod)
            .sort((a, b) => b.modified.localeCompare(a.modified)); // descending

        // Add options to the dropdown
        eligible_history.forEach(item => {
            const option = document.createElement('option');
            option.value = item.modified;
            option.textContent = format_date(item.modified);
            selector.appendChild(option);
        });

        if ('refmod' in data && data.refmod) {
            const exact_match = eligible_history.find(item => item.modified === data.refmod);
            if (exact_match) {
                selector.value = exact_match.modified;
            } else {
                // refmod doesn't match any distinct revision in history (e.g.
                // it equals the current lastmod, or some other chain-gap
                // anomaly -- see issue #138). Silently substituting the
                // nearest older real revision would hide that mismatch, so
                // surface the true value as its own flagged option instead
                // of approximating it away.
                console.warn(`render_history: refmod ${data.refmod} for ${data.title} does not match a distinct revision in history -- showing it as a flagged option instead of approximating`);
                const anomaly_option = document.createElement('option');
                anomaly_option.value = data.refmod;
                anomaly_option.textContent = `${format_date(data.refmod)} *`;
                anomaly_option.title = 'This reference date does not correspond to a distinct revision in this page\'s history.';
                const insert_before = Array.from(selector.options).find(
                    opt => opt.value && opt.value < data.refmod
                );
                selector.insertBefore(anomaly_option, insert_before || null);
                selector.value = data.refmod;
            }
        }
    }
}

function render_breadcrumbs(data) {
    const breadcrumbContainer = document.getElementById('breadcrumb');
    breadcrumbContainer.innerHTML = ''; // Clear existing content

    // data.lineage/data.lineage_labels are leaf-to-root; render root-to-leaf.
    // The server always terminates lineage at ROOT_HUB_TITLE, so the root
    // crumb (rendered as a house icon) is always present -- no client-side
    // special-casing needed here.
    const levels = data.lineage.map((title, i) => ({ title, label: data.lineage_labels[i] })).reverse();

    levels.forEach((level, index) => {
        const li = document.createElement('li');
        li.classList.add('breadcrumb-item');

        const content = level.title === ROOT_HUB_TITLE
            ? '<i class="bi bi-house-fill"></i>'
            : level.label;

        if (index === levels.length - 1) {
            // Final part - make it non-clickable (active)
            li.classList.add('active');
            li.setAttribute('aria-current', 'page');
            li.innerHTML = content;
        } else {
            // Intermediate parts are clickable
            const link = document.createElement('a');
            link.href = '#'; // Optional: Use an actual URL if needed
            link.innerHTML = content;
            link.addEventListener('click', (event) => {
                event.preventDefault();
                load_page_by_title(level.title);
            });
            li.appendChild(link);
        }

        breadcrumbContainer.appendChild(li);
    });
}

function watch_title_for_page(page) {
    // the (coarsened) watchlist entry a page falls under is the level just
    // below the permanent hub root in its lineage (lineage is leaf-to-root);
    // if there's no such level (the page IS the hub), fall back to the
    // page's own title -- which then simply won't match anything watchable
    const lineage = page.lineage || [];
    return lineage.length > 1 ? lineage[lineage.length - 2] : page.title;
}

function update_archive_select() {
    const archive_root_title = watch_title_for_page(current_page);
    console.log('update_archive_select:', archive_root_title);
    document.getElementById('archiveSelect').value = -1;
    archives.forEach((archive, index) => {
        if (archive.title === archive_root_title) {
            console.log('archive select:', index)
            document.getElementById('archiveSelect').value = index;
        }

    });
}

async function populate_archive_select() {
    console.log("populate_archive_select");
    const archive_select_btn = document.getElementById('archive-select-btn');
    const archive_select_modal = new bootstrap.Modal(document.getElementById('archiveSelectModal'));
    const archive_select = document.getElementById('archiveSelect');
    const confirm_selection_btn = document.getElementById('confirmSelectionBtn');

    confirm_selection_btn.disabled = true;

    async function fetch_archives() {
        try {
            console.log("fetching /archives");
            const response = await fetch('/archives');
            if (!response.ok) {
                if (response.status === 404) {
                    alert('Your session may have expired. Please log in again.');
                    location.reload();
                    return null;
                }
                throw new Error(`Failed to fetch archives: ${response.statusText}`);
            }
            // /archives already returns entries sorted by label
            return await response.json();
        } catch (error) {
            console.error('Error fetching archives:', error);
            alert('Failed to load archives. Please try again.');
            return null;
        }
    }

    function populate_archive_select_dropdown(archive_list) {
        archive_select.innerHTML = '<option value="-1" selected>Select an archive...</option>';
        archive_list.forEach((archive, index) => {
            const option = document.createElement('option');
            option.value = index;
            option.textContent = archive.label;
            archive_select.appendChild(option);
        });
    }

    archives = await fetch_archives();
    if (!archives || archives.length === 0) return;

    populate_archive_select_dropdown(archives);
    populate_watchlist_archive_select(archives);
    confirm_selection_btn.disabled = false;

    confirm_selection_btn.onclick = () => {
        const archive_index = parseInt(archive_select.value, 10);
        if (isNaN(archive_index) || archive_index < 0 || archive_index >= archives.length) {
            alert('Please select an archive.');
            return;
        }
        const selected_archive = archives[archive_index];
        console.log(`Selected Archive: ${selected_archive.label} (title=${selected_archive.title})`);
        load_page_by_title(selected_archive.title)
        archive_select_modal.hide();
    };
}

// ---------------------------------------------------------------------------
// ARCHIVES TAB (title/label/description table)

function render_archives_table() {
    const table_body = document.getElementById('archives-table-body');
    table_body.innerHTML = '';

    const can_edit = current_user && current_user.role === 'admin';
    const sorted_archives = [...archives].sort((a, b) => a.label.localeCompare(b.label));

    sorted_archives.forEach(archive => {
        const label = archive.label;
        const description = archive.description || '';
        const label_cell = can_edit
            ? `<input type="text" class="form-control form-control-sm" data-original="${escape_attr(label)}" value="${escape_attr(label)}" onclick="event.stopPropagation()" oninput="update_archive_save_state(this)">`
            : escape_attr(label);
        const description_cell = can_edit
            ? `<input type="text" class="form-control form-control-sm" data-original="${escape_attr(description)}" value="${escape_attr(description)}" onclick="event.stopPropagation()" oninput="update_archive_save_state(this)">`
            : escape_attr(description);
        const save_cell = can_edit
            ? `<button class="btn btn-primary btn-sm" title="Save Changes" onclick="event.stopPropagation(); save_archive_row(this)" disabled>
                   <i class="bi bi-arrow-up-square"></i>
               </button>`
            : '';
        const row = `
            <tr data-title="${escape_attr(archive.title)}" style="cursor: pointer;" onclick="open_archive_row(this)">
                <td style="word-break: break-word;">${escape_attr(archive.title)}</td>
                <td style="word-break: break-word;">${label_cell}</td>
                <td style="word-break: break-word;">${description_cell}</td>
                <td>
                    <a href="${escape_attr(archive.url)}" target="birddog_archive_source" class="btn btn-primary btn-sm" title="View Source" onclick="event.stopPropagation()">
                        <i class="bi bi-box-arrow-up-right"></i>
                    </a>
                </td>
                <td>${save_cell}</td>
            </tr>
        `;
        table_body.innerHTML += row;
    });
}

function open_archive_row(row) {
    const title = row.dataset.title;
    load_page_by_title(title);
    show_tab('nav-browse-tab');
}

function update_archive_save_state(input) {
    const row = input.closest('tr');
    const inputs = row.querySelectorAll('input');
    const dirty = Array.from(inputs).some(inp => inp.value !== inp.dataset.original);
    row.querySelector('button').disabled = !dirty;
}

async function save_archive_row(button) {
    const row = button.closest('tr');
    const title = row.dataset.title;
    const inputs = row.querySelectorAll('input');
    const label = inputs[0].value.trim();
    const description = inputs[1].value.trim();

    try {
        const response = await fetch('/archives', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title, label, description }),
        });
        if (!response.ok) {
            const data = await response.json().catch(() => null);
            throw new Error(data?.error || `Failed to save: ${response.statusText}`);
        }
        const archive = find_archive(title);
        if (archive) {
            archive.label = label;
            archive.description = description;
        }
        inputs[0].value = label;
        inputs[1].value = description;
        inputs[0].dataset.original = label;
        inputs[1].dataset.original = description;
        button.disabled = true;
    } catch (error) {
        console.error('Error saving archive metadata:', error);
        alert(`Failed to save archive details: ${error.message}`);
    }
}

// ---------------------------------------------------------------------------
// WATCHLIST MANAGEMENT

async function load_watchlist(check_all=false, initial_load=false) {
    const response = await fetch('/watchlist');
    if (response.status === 404) {
        alert('Your session may have expired. Please log in again.');
        location.reload();
        return;
    }
    const data = await response.json();
    console.log('watchlist loaded:', data)
    watchlist = data;

    // Check if the watchlist is empty on initial load
    if (initial_load && watchlist.length == 0) {
        open_add_to_watchlist_dialog();
        return;
    }

    if (check_all)
        check_all_watchlists();
    render_watchlist();
}

function open_add_to_watchlist_dialog() {
    // show the modal
    var add_watchlist_modal = new bootstrap.Modal(document.getElementById('addWatchlistModal'));
    add_watchlist_modal.show();
}

function render_watchlist() {
    const table_body = document.getElementById('watchlist-body');
    table_body.innerHTML = '';

    const sorted_watchlist = [...watchlist].sort((a, b) => a.label.localeCompare(b.label));

    sorted_watchlist.forEach(item => {
        // titles can contain characters (quotes, etc.) that are unsafe to
        // splice into an inline onclick="fn('...')" string -- the row's own
        // data-title (already escape_attr()'d) is the safe source of truth,
        // read back via the delegated click handler below instead
        const row = `
            <tr data-title="${escape_attr(item.title)}">
                <td>${item.label}</td>
                <td>${format_date(item.last_checked_date)}</td>
                <td>${format_date(item.cutoff_date)}</td>
                <td>
                    <button class="btn btn-primary check-watchlist-btn" title="Check for Updates">
                        <i class="bi bi-arrow-clockwise"></i>
                    </button>
                </td>
                <td>
                    <button class="btn btn-primary remove-watchlist-btn" title="Remove from Watchlist">
                        <i class="bi bi-x-square"></i>
                    </button>
                </td>
            </tr>
        `;
        table_body.innerHTML += row;
    });

    // scroll to top automatically
    document.getElementById('nav-watchlists').scrollTo({ top: 0, behavior: 'smooth' });
}

async function remove_from_watchlist(title) {
    await fetch(`/watchlist/${encodeURIComponent(title)}`, { method: 'DELETE' });
    load_watchlist(); // Refresh after deletion
    if (title in unresolved_updates) {
        delete unresolved_updates[title];
        render_unresolved_items();
    }
}

async function check_watchlist(title, quiet=false, render=true) {
    console.log(`Checking ${title}...`);
    try {
        // Show the spinner
        show('unresolved-updates-loading-spinner');
        hide('unresolved-updates-container');

        const response = await fetch(`/watchlist/${encodeURIComponent(title)}/check?tree`);

        if (response.status === 404) {
            if (!find_archive(title)) {
                // the specified archive doesn't exists
                alert(`The archive ${title} is unrecognized. Please remove it from your watchlist.`);
                show('unresolved-updates-container');
                hide('unresolved-updates-loading-spinner');
                return;
            }
            alert('Your session may have expired. Please log in again.');
            location.reload();
            return;
        }

        if (!response.ok) {
            // Hide the spinner
            show('unresolved-updates-container');
            hide('unresolved-updates-loading-spinner');
            throw new Error(`Failed to check updates: ${response.statusText}`);
        }
        const data = await response.json();
        console.log('check_watchlist:', data);

        unresolved_updates[title] = data.unresolved;
        if (render) {
            // Hide the spinner
            show('unresolved-updates-container');
            hide('unresolved-updates-loading-spinner');
            render_unresolved_items();
        }
        if (!quiet && data.unresolved.length == 0)
            alert(`No new updates for ${title}.`);
        watchlist = data.watchlist;
        render_watchlist();
    } catch (error) {
        // Hide the spinner
        show('unresolved-updates-container');
        hide('unresolved-updates-loading-spinner');
        console.error('Error checking updates:', error);
        if (!quiet)
            alert('Failed to check updates.');
    }
}

async function check_all_watchlists() {
    show('unresolved-updates-loading-spinner');
    hide('unresolved-updates-container');

    const promises = watchlist.map(item =>
        check_watchlist(item.title, true, false)
            .catch(err => console.error(`Error in ${item.title}:`, err))
    );

    await Promise.all(promises);

    show('unresolved-updates-container');
    hide('unresolved-updates-loading-spinner');
    console.log('check_all_watchlists: render_unresolved');
    render_unresolved_items();
}


// Populate the archive select dropdown
async function populate_watchlist_archive_select(archives) {
    const archive_select = document.getElementById('watchlistArchiveSelect');
    archive_select.innerHTML = '<option value="" selected>Select an archive...</option>';

    try {
        // the hub page itself is a valid, if narrow, watch target (it only
        // ever flags edits to the hub index page itself, since no other
        // page's title is prefixed by it) -- offered here, not in the
        // general archive browse dropdown, since it's already reachable
        // there via the permanent breadcrumb home icon
        const hub_option = document.createElement('option');
        hub_option.value = ROOT_HUB_TITLE;
        hub_option.textContent = ROOT_HUB_LABEL;
        archive_select.appendChild(hub_option);

        archives.forEach(archive => {
            const option = document.createElement('option');
            option.value = archive.title;
            option.textContent = archive.label;
            archive_select.appendChild(option);
        });
    } catch (error) {
        console.error('Error fetching archives:', error);
        alert('Failed to load archives.');
    }
}

// Confirm adding to the watchlist
async function confirm_add_to_watchlist() {
    const title = document.getElementById('watchlistArchiveSelect').value;
    const cutoff_input = document.getElementById('watchlistCutoffDate').value;
    const cutoff_date = cutoff_input ? `${cutoff_input}T00:00:00Z` : '';
    console.log(title, cutoff_date);
    if (!title || !cutoff_date) {
        alert('All fields are required.');
        return;
    }

    // Close the modal using Bootstrap API
    const modal = bootstrap.Modal.getInstance(document.getElementById('addWatchlistModal'));
    modal.hide();

    // Show the spinner
    show('watchlist-loading-spinner');
    hide('watchlist-container');

    const response = await fetch('/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            title: title,
            cutoff_date: cutoff_date
        })
    });

    if (response.status === 404) {
        alert('Your session may have expired. Please log in again.');
        location.reload();
        return;
    }

    hide('watchlist-loading-spinner');
    show('watchlist-container');

    const data = await response.json();
    console.log('watchlist:', data)
    watchlist = data;
    render_watchlist();

    // Refresh table
    //load_watchlist(check_all=true);
}

// ---------------------------------------------------------------------------
// UNRESOLVED UPDATES NAVIGATOR

// path (full page title) -> the flattened row {path, watch_title, meta,
// full_label} rendered for it, for browse-page lookups (needs_resolve(),
// resolve_page()); populated by render_unresolved_items(). Only real,
// currently-visible (not hidden-minor) entries are present -- consistent
// with the tree view this replaced, which never gave a hidden-minor node a
// resolve affordance either.
var path_to_node = null;

function page_path(page) {
    return page.title;
}

function needs_resolve(page) {
    if (!path_to_node)
        return false;
    const path = page_path(page);
    //console.log('needs_resolve:', path);
    return path in path_to_node;
}

function view_changes(page_title, modified, last_resolved) {
    console.log("Viewing changes for", page_title, last_resolved);
    compare = last_resolved ?? null;
    load_page_by_title(page_title, compare=compare);
    // Switch to the browse tab
    show_tab('nav-browse-tab');
}


async function resolve_page_update(title, item_title, deep=false) {
    try {
        console.log('Resolving:', title, item_title, "deep=", deep);
        const params = new URLSearchParams({ title: title, item: item_title, tree: '1' });
        if (deep)
            params.set('deep', '1');
        const response = await fetch(`/resolve?${params.toString()}`);
        if (!response.ok) {
            if (response.status === 404) {
                alert('Your session may have expired. Please log in again.');
                location.reload();
                return;
            }
            throw new Error(`Failed to resolve: ${response.statusText}`);
        }

        // update unresolved item table
        const data = await response.json();
        console.log('resolve result:', data);
        unresolved_updates[title] = data.unresolved;

        render_unresolved_items();
    } catch (error) {
        console.error('Error during resolve:', error);
        alert('Failed to resolve.');
    }
}

function mark_resolved(watch_title, full_path, label) {
    // the table shows one row per real unresolved item -- a row's own
    // Resolve button now only ever resolves that one row (deep=false).
    // Sweeping up a whole subtree in one action is Stage 4's job (bulk
    // select + a per-row subtree quick-select), not an implicit side effect
    // of resolving the row at the top of that subtree.
    if (!confirm(`Resolve ${label}?`)) {
        console.log('resolve cancelled by user');
        return false;
    }
    console.log("Marking resolved:", watch_title, full_path);
    resolve_page_update(watch_title, full_path, deep=false);
    return true;
}

// called from resolve button on browse page
function resolve_page() {
    const path = page_path(current_page);
    if (!path_to_node)
        return false;
    const row = path_to_node[path];
    if (!row) return false;
    if (mark_resolved(row.watch_title, row.path, row.full_label)) {
        enable_if("resolve-btn", false);
        //show("next-unresolved-btn");
        hide('needs-resolve-badge');
    }
}

// called from next unresolved button on browse page
function next_unresolved() {
    const next_unresolved = get_next_unresolved_item(current_page.title);
    console.log("next unresolved: ", next_unresolved);
    if (next_unresolved != null) {
        view_changes(next_unresolved.title, next_unresolved.modified, next_unresolved.last_resolved);
    }
}

function is_real_entry(meta) {
    // unresolved_tree() (watcher.py) gives every node -- leaf or synthesized
    // path placeholder -- a non-null meta with at least {title, label}, so
    // meta truthiness alone can't tell them apart. A genuine unresolved item
    // always carries 'modified'; a placeholder never does.
    return !!(meta && meta.modified);
}

function get_watch_cutoff(watch_title) {
    const entry = watchlist && watchlist.find(w => w.title === watch_title);
    return entry ? entry.cutoff_date : '';
}

function group_counts(data_list) {
    // counts every real unresolved entry for a watch regardless of the
    // hide-minor toggle or collapse state -- a stable summary a collapsed
    // group's header can show without needing to be rebuilt when either
    // toggles
    let total = 0, new_count = 0, minor_count = 0;
    for (const [, meta] of data_list) {
        if (!is_real_entry(meta)) continue;
        total++;
        if (meta.new_page) new_count++;
        if (meta.insignificant) minor_count++;
    }
    return { total, new_count, minor_count };
}

function group_header_html(watch_title, counts) {
    const collapsed = collapsed_watches.has(watch_title);
    const summary_parts = [`${counts.total} unresolved`];
    if (counts.new_count) summary_parts.push(`${counts.new_count} new`);
    if (counts.minor_count) summary_parts.push(`${counts.minor_count} minor`);

    return `
        <tr class="table-light">
          <td></td>
          <td colspan="5">
            <button
                type="button"
                class="btn btn-sm btn-link text-decoration-none text-start p-0 w-100 d-flex align-items-center gap-2 group-toggle-btn"
                data-watch-title="${escape_attr(watch_title)}"
            >
              <i class="bi ${collapsed ? 'bi-chevron-right' : 'bi-chevron-down'}"></i>
              <strong>${escape_attr(label_for_watch_title(watch_title))}</strong>
              <span class="text-muted small">${escape_attr(summary_parts.join(' · '))}</span>
            </button>
          </td>
          <td class="text-nowrap">
            <button
                type="button"
                class="btn btn-sm btn-outline-secondary alert-subtree-select-btn"
                title="Select all of ${escape_attr(label_for_watch_title(watch_title))}"
                data-select-prefix="${escape_attr(watch_title)}"
            >
              <i class="bi bi-check2-square"></i>
            </button>
          </td>
        </tr>
    `;
}

function flatten_alert_rows(data_list, watch_title) {
    // unresolved_tree() (watcher.py) emits one record per path level, parent
    // before its children -- every real item's ancestor placeholders (each
    // carrying its own latinized 'label' for that single path segment, see
    // _gen_title() in watcher.py) therefore always appear earlier in
    // data_list, so a first pass can build a path -> segment-label lookup
    // that a second pass uses to assemble each real item's full breadcrumb
    // label (e.g. "DAKO / 132 / 4a") without a tree structure at all.
    const label_by_path = {};
    for (const [path, meta] of data_list) {
        label_by_path[path] = meta && meta.label;
    }

    const rows = [];
    for (const [path, meta] of data_list) {
        if (!is_real_entry(meta)) continue;
        // hidden-minor entries are excluded here, at the source, the same as
        // the tree view this replaced -- they never got a resolve
        // affordance from the browse page either (see path_to_node)
        if (hide_insignificant && meta.insignificant) continue;
        let acc = '';
        const segments = [];
        for (const part of path.split('/')) {
            acc = acc ? `${acc}/${part}` : part;
            segments.push(label_by_path[acc] ?? part);
        }
        rows.push({ path, watch_title, meta, full_label: segments.join(' / ') });
    }
    return rows;
}

function alert_row_html(row) {
    const { path, watch_title, meta, full_label } = row;
    const modified = meta.modified || '';
    const last_resolved = meta.last_resolved || '';
    const cutoff = get_watch_cutoff(watch_title);
    const checked = checked_paths.has(path);

    let badges = '';
    if (meta.new_page) badges += ' <span class="badge bg-success">NEW</span>';
    if (meta.insignificant) badges += ' <span class="badge bg-secondary">MINOR</span>';

    // a "moved_to" entry (issue #136) means this watch's own scope was
    // moved on the wiki, not an ordinary content change -- resolving it
    // retires the whole watch, so it needs to read very differently from a
    // normal alert row
    const label_html = meta.moved_to
        ? `${escape_attr(full_label)} <span class="text-danger small">(archive moved to "${escape_attr(meta.moved_to)}" -- resolving removes it from your watchlist)</span>`
        : escape_attr(full_label);

    // a subtree quick-select button only makes sense when this row has a
    // parent within its own watch to select alongside it (a bare watch-root
    // row's equivalent is the group header's own select-all-of-archive
    // button, see group_header_html())
    const parent_path = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : null;
    const subtree_btn = parent_path
        ? `<button
               type="button"
               class="btn btn-sm btn-link p-0 ms-1 alert-subtree-select-btn"
               title="Select this and everything under ${escape_attr(parent_path)}"
               data-select-prefix="${escape_attr(parent_path)}"
           >
             <i class="bi bi-diagram-3"></i>
           </button>`
        : '';

    return `
        <tr class="${checked ? 'table-active' : ''}" data-path="${escape_attr(path)}" data-modified="${escape_attr(modified)}" data-last-resolved="${escape_attr(last_resolved)}">
          <td class="select-col"><input type="checkbox" class="form-check-input row-select-checkbox" data-path="${escape_attr(path)}" ${checked ? 'checked' : ''}></td>
          <td>${label_html}${badges}${subtree_btn}</td>
          <td>${modified ? format_date(modified, false) : ''}</td>
          <td>${escape_attr(meta.user || '')}</td>
          <td>${last_resolved ? format_date(last_resolved, false) : ''}</td>
          <td>${cutoff ? format_date(cutoff, false) : ''}</td>
          <td class="text-nowrap">
            <button
                class="btn btn-sm btn-primary mark-resolved-btn"
                title="Mark Resolved"
                data-watch-title="${escape_attr(watch_title)}"
                data-path="${escape_attr(path)}"
                data-label="${escape_attr(full_label)}"
            >
              <i class="bi bi-check-square"></i>
            </button>
          </td>
        </tr>
    `;
}

function render_unresolved_items() {
    const tbody = document.getElementById('alerts-table-body');
    path_to_node = {};
    rendered_paths = new Set();
    tbody.innerHTML = '';

    // Sort by each entry's displayed (latinized) label, not the raw
    // Cyrillic watch title, so the on-screen order matches what's shown
    const sorted_keys = Object.keys(unresolved_updates).sort(
        (a, b) => label_for_watch_title(a).localeCompare(label_for_watch_title(b))
    );
    console.log(`render_unresolved_items: sorted_keys=${sorted_keys}`);

    const rows_html = [];
    sorted_keys.forEach(key => {
        const data_list = unresolved_updates[key];
        // flatten_alert_rows() already excludes hidden-minor entries (see its
        // own comment) -- path_to_node stays scoped to exactly what's
        // currently visible-or-collapsed, same as before groups existed. A
        // watch left with nothing visible (e.g. every item is minor and the
        // toggle is hiding them) renders no header at all, matching the old
        // tree view's pruning of an all-hidden branch.
        const rows = flatten_alert_rows(data_list, key);
        rows.forEach(row => { path_to_node[row.path] = row; });
        if (rows.length === 0) return;

        // the header's own summary counts every real entry regardless of the
        // hide-minor toggle, so it stays a stable, informative total even
        // while some of this watch's rows are filtered out below
        rows_html.push(group_header_html(key, group_counts(data_list)));
        if (!collapsed_watches.has(key)) {
            rows.forEach(row => {
                rendered_paths.add(row.path);
                rows_html.push(alert_row_html(row));
            });
        }
    });
    tbody.innerHTML = rows_html.join('');

    // drop any checked path that no longer exists anywhere (resolved via
    // another path -- a single-row resolve, the Browse-tab button, or a
    // previous bulk resolve) -- but keep one that's merely hidden right now
    // by the minor-changes toggle or a collapsed watch, matching the
    // checked-state-persists-independent-of-hidden decision
    const all_real_paths = new Set();
    for (const key in unresolved_updates) {
        for (const [path, meta] of unresolved_updates[key]) {
            if (is_real_entry(meta)) all_real_paths.add(path);
        }
    }
    checked_paths.forEach(p => { if (!all_real_paths.has(p)) checked_paths.delete(p); });

    update_hidden_count_badge();
    update_bulk_bar();
}

// issue #138: count every unresolved item across all watchlists currently
// flagged insignificant by the background sweep (birddog/significance.py)
function count_hidden_items() {
    let count = 0;
    for (const key in unresolved_updates) {
        for (const [, meta] of unresolved_updates[key]) {
            if (meta && meta.insignificant) count++;
        }
    }
    return count;
}

function update_hidden_count_badge() {
    const badge = document.getElementById('hidden-count-badge');
    if (!badge) return;
    const hidden = count_hidden_items();
    if (hide_insignificant && hidden > 0) {
        badge.textContent = `${hidden} minor change${hidden === 1 ? '' : 's'} hidden`;
        badge.classList.remove('d-none');
    } else {
        badge.classList.add('d-none');
    }
}

async function on_hide_insignificant_toggle(checkbox) {
    hide_insignificant = checkbox.checked;
    try {
        const response = await fetch('/preference/hide_insignificant', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: hide_insignificant }),
        });
        if (!response.ok) {
            throw new Error(`Failed to save preference: ${response.statusText}`);
        }
    } catch (error) {
        console.error('Failed to save hide_insignificant preference:', error);
    }
    render_unresolved_items();
}

// ---------------------------------------------------------------------------
// ALERTS TABLE BULK RESOLVE (issue #138 Stage 4)
//
// Selection state (checked_paths) lives independently of what's currently
// rendered -- a checkbox's checked state never changes just because its row
// stops being displayed (minor-hide toggle, or its watch collapsing). The
// bulk bar discloses whenever the checked set includes rows not currently
// on screen, rather than silently including or silently dropping them.

function set_row_checked(path, checked) {
    if (checked) checked_paths.add(path); else checked_paths.delete(path);
    const cb = document.querySelector(`#alerts-table-body .row-select-checkbox[data-path="${CSS.escape(path)}"]`);
    if (cb) {
        cb.checked = checked;
        cb.closest('tr').classList.toggle('table-active', checked);
    }
}

function sync_select_all_checkbox() {
    const select_all_cb = document.getElementById('select-all-alerts');
    if (!select_all_cb) return;
    const boxes = Array.from(document.querySelectorAll('#alerts-table-body .row-select-checkbox'));
    const checked_count = boxes.filter(cb => cb.checked).length;
    select_all_cb.checked = boxes.length > 0 && checked_count === boxes.length;
    select_all_cb.indeterminate = checked_count > 0 && checked_count < boxes.length;
}

function update_bulk_bar() {
    const total = checked_paths.size;
    const hidden = Array.from(checked_paths).filter(p => !rendered_paths.has(p)).length;
    show_if('bulk-resolve-bar', total > 0);
    if (total > 0) {
        document.getElementById('bulk-selected-count').textContent = total;
        document.getElementById('bulk-hidden-note').textContent = hidden > 0 ? ` (${hidden} hidden)` : '';
    }
    sync_select_all_checkbox();
}

function clear_bulk_selection() {
    checked_paths.forEach(path => set_row_checked(path, false));
    checked_paths.clear();
    last_checked_path = null;
    update_bulk_bar();
}

function select_subtree(prefix) {
    // path_to_node covers every currently-visible-per-minor-filter row
    // regardless of collapse state (see render_unresolved_items()), so this
    // reaches into a collapsed watch's rows too -- set_row_checked() only
    // touches the DOM for a path that actually has a rendered checkbox,
    // and otherwise just records the selection for the bulk bar to disclose
    // as hidden. A minor-hidden row still isn't reachable this way, same as
    // everywhere else hidden-minor rows are excluded.
    Object.keys(path_to_node).forEach(path => {
        if (path === prefix || path.startsWith(prefix + '/')) {
            set_row_checked(path, true);
        }
    });
    update_bulk_bar();
}

async function resolve_selected() {
    if (checked_paths.size === 0) return;

    const items = [];
    checked_paths.forEach(path => {
        const row = path_to_node[path];
        if (row) items.push({ title: row.watch_title, item: path });
    });
    if (items.length === 0) return;

    const hidden = Array.from(checked_paths).filter(p => !rendered_paths.has(p)).length;
    const hidden_text = hidden > 0 ? ` (including ${hidden} hidden)` : '';
    if (!confirm(`Resolve ${items.length} selected item(s)${hidden_text}?`)) return;

    try {
        const response = await fetch('/resolve/batch?tree=1', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items }),
        });
        if (!response.ok) {
            if (response.status === 404) {
                alert('Your session may have expired. Please log in again.');
                location.reload();
                return;
            }
            throw new Error(`Failed to resolve: ${response.statusText}`);
        }
        const data = await response.json();
        Object.entries(data.unresolved).forEach(([title, unresolved]) => {
            unresolved_updates[title] = unresolved;
        });
        checked_paths.clear();
        last_checked_path = null;
        render_unresolved_items();
    } catch (error) {
        console.error('Error during batch resolve:', error);
        alert('Failed to resolve selected items.');
    }
}

function toggle_page_desc_icon(btn) {
  const icon = btn.querySelector('i');
  if (btn.getAttribute('aria-expanded') === 'true') {
    icon.classList.remove('bi-plus-circle-fill');
    icon.classList.add('bi-dash-circle-fill');
  } else {
    icon.classList.remove('bi-dash-circle-fill');
    icon.classList.add('bi-plus-circle-fill');
  }
}

// ---------------------------------------------------------------------------
// APP INITIALIZATION

async function on_loaded() {
    // Login form submit button
    const login = document.getElementById('loginForm');
    if (login) {
        login.addEventListener('submit', async (event) => {
            event.preventDefault();

            const email = document.getElementById('loginEmail').value;
            const password = document.getElementById('loginPassword').value;

            const response = await fetch('/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ email, password })
            });

            if (response.ok) {
                window.location.reload();  // Reload the page to reflect logged-in state
            } else {
                const data = await response.json();
                document.getElementById('loginError').innerText = data.message;
            }
        });
    }

    // show/hide login password toggle
    document.getElementById('toggleLoginPassword')?.addEventListener('click', () => {
        const input = document.getElementById('loginPassword');
        const icon = document.getElementById('toggleLoginPasswordIcon');
        const showing = input.type === 'text';
        input.type = showing ? 'password' : 'text';
        icon.className = showing ? 'bi bi-eye' : 'bi bi-eye-slash';
    });

    // signup form submit button
    const signup = document.getElementById('signupForm');
    if (signup) {
        signup.addEventListener('submit', async (event) => {
            event.preventDefault();

            const name = document.getElementById('signupName').value;
            const email = document.getElementById('signupEmail').value;
            const password = document.getElementById('signupPassword').value;

            const response = await fetch('/signup', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name, email, password })
            });

            if (response.ok) {
                window.location.reload();  // Refresh page to reflect logged-in state
            } else {
                const data = await response.json();
                document.getElementById('signupError').innerText = data.message;
            }
        });
    }

    // reset password submit
    const reset_password = document.getElementById('resetPasswordModal');
    if (reset_password) {
        reset_password.addEventListener('submit', async (event) => {
            event.preventDefault();

            const email = document.getElementById('resetEmail').value;
            console.log('reset password:', email)

            const response = await fetch('/reset_password', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ email })
            });
            const data = await response.json();
            alert(data.message);
            window.location.reload();  // Refresh page
        });
    }


    // if user is logged in, then start loading the page data
    const user_data_elem = document.getElementById('user-data');
    if (user_data_elem) {
        current_user = JSON.parse(user_data_elem.dataset.user);

        // ------------------ VERSION SELECT HANDLER ------------------
        // version select listener
        const selector = document.getElementById('version-select');
        selector.addEventListener('change', (event) => {
            const version = event.target.value;
            console.log(`Comparing to: ${version}`);
            load_page_by_title(current_page.title, compare=version);
            //alert(`Comparing to version ${selectedVersion}`)
        });

        // ------------------ TABLE BODY CLICK HANDLER ------------------
        // Attach click event to the whole table body
        const watchlist_body = document.getElementById('watchlist-body');
        watchlist_body.addEventListener('click', (event) => {
            const row = event.target.closest('tr');
            if (!row) return;
            const title = row.dataset.title;
            if (!title) return;

            if (event.target.closest('.check-watchlist-btn')) {
                check_watchlist(title);
                return;
            }
            if (event.target.closest('.remove-watchlist-btn')) {
                remove_from_watchlist(title);
                return;
            }
            if (!event.target.closest('button')) {
                console.log(`browse: ${title}`)
                // load the selected page
                load_page_by_title(title);
                // Switch to the browse tab
                show_tab('nav-browse-tab');
            }
        });

        // ------------------ ALERTS TABLE CONTROL HANDLER ------------------
        const alerts_table_body = document.getElementById('alerts-table-body');
        alerts_table_body.addEventListener("click", (e) => {
            if (e.target.classList.contains("row-select-checkbox")) {
                const cb = e.target;
                const path = cb.dataset.path;
                if (e.shiftKey && last_checked_path !== null) {
                    const boxes = Array.from(document.querySelectorAll('#alerts-table-body .row-select-checkbox'));
                    const from_index = boxes.findIndex(b => b.dataset.path === last_checked_path);
                    const to_index = boxes.findIndex(b => b.dataset.path === path);
                    if (from_index !== -1 && to_index !== -1) {
                        const [start, end] = [from_index, to_index].sort((a, b) => a - b);
                        for (let i = start; i <= end; i++) {
                            set_row_checked(boxes[i].dataset.path, cb.checked);
                        }
                    }
                } else {
                    set_row_checked(path, cb.checked);
                }
                last_checked_path = path;
                update_bulk_bar();
                return;
            }

            const subtree_btn = e.target.closest(".alert-subtree-select-btn");
            if (subtree_btn) {
                select_subtree(subtree_btn.dataset.selectPrefix);
                return;
            }

            const group_btn = e.target.closest(".group-toggle-btn");
            if (group_btn) {
                const watch_title = group_btn.dataset.watchTitle;
                if (collapsed_watches.has(watch_title)) collapsed_watches.delete(watch_title);
                else collapsed_watches.add(watch_title);
                render_unresolved_items();
                return;
            }

            const mark_btn = e.target.closest(".mark-resolved-btn");
            if (mark_btn) {
                mark_resolved(mark_btn.dataset.watchTitle, mark_btn.dataset.path, mark_btn.dataset.label);
                return;
            }

            // clicking the row body anywhere else (label text, a badge,
            // blank cell space) views that item's changes -- there's no
            // separate eye button; this is the only way to trigger it.
            // Mirrors the watchlist table's row-click-to-browse fallback.
            // Header rows carry no data-path, so a click on empty header
            // space is correctly a no-op here.
            const row = e.target.closest("tr");
            if (row && row.dataset.path) {
                view_changes(row.dataset.path, row.dataset.modified, row.dataset.lastResolved);
            }
        });

        document.getElementById('select-all-alerts')?.addEventListener('change', function() {
            const checked = this.checked;
            document.querySelectorAll('#alerts-table-body .row-select-checkbox').forEach(cb => {
                set_row_checked(cb.dataset.path, checked);
            });
            last_checked_path = null;
            update_bulk_bar();
        });

        document.getElementById('bulk-clear-btn')?.addEventListener('click', clear_bulk_selection);
        document.getElementById('bulk-resolve-btn')?.addEventListener('click', resolve_selected);

        // ------------------ CHANGE PASSWORD HANDLER ------------------
        // handler for change password form
        document.getElementById('change-password-form')?.addEventListener('submit', async (e) => {
          e.preventDefault();

          const current = document.getElementById('currentPassword').value;
          const newPass = document.getElementById('newPassword').value;
          const confirm = document.getElementById('confirmPassword').value;
          const msgBox = document.getElementById('password-change-message');

          if (newPass !== confirm) {
            msgBox.textContent = 'New passwords do not match.';
            msgBox.className = 'text-danger';
            return;
          }

          try {
            const res = await fetch('/change_password', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ current, new: newPass }),
            });

            const result = await res.json();
            msgBox.textContent = result.message;
            msgBox.className = result.success ? 'text-success' : 'text-danger';
          } catch (err) {
            msgBox.textContent = 'Error changing password.';
            msgBox.className = 'text-danger';
          }
        });

        // ------------------ DATABASE UPDATE HANDLER ------------------
        const btn = document.getElementById("db-update-confirm-btn");
        if (!btn) return;

        btn.addEventListener("click", async () => {
            const deep = document.getElementById("db-update-deep-checkbox").checked;

            // Close modal immediately
            const modal_el = document.getElementById("dbUpdateConfirmModal");
            bootstrap.Modal.getOrCreateInstance(modal_el).hide();

            // Call your existing backend update logic here
            // Replace this stub with your existing implementation.
            await do_database_update(deep);
        });

        // ------------------ EXPORT HANDLER ------------------
        document.getElementById("submitExport").addEventListener("click", () => {
          const selected_template = document.getElementById("templateSelect").value;
          let selected_table = document.getElementById("tableSelect").value;

          if (!Object.keys(window._export_column_headers).includes(selected_table)) {
            if (window._export_num_tables > 0) {
              selected_table = Object.keys(window._export_column_headers)[0];
            } else {
              selected_table = "";
            }
          }
          console.log(`export: template=${selected_template}, table=${selected_table}`);

          // map selected labels to indices
          console.log(window._export_column_headers);
          const column_map = {};
          if (window._export_num_tables > 0) {
            window._export_column_classes.forEach(col => {
              const tagify = window._export_tagify_map[col];
              if (tagify) {
                column_map[col] = tagify.value
                  .map(tag => {
                    const idx = window._export_column_headers[selected_table].indexOf(tag.value);
                    if (idx === -1) console.warn(`Header not found: "${tag.value}"`);
                    return idx;
                  })
                  .filter(i => i !== -1); // ignore unfound labels
              }
            });
          }

          const payload = {
            title: window._export_page_title,
            template: selected_template,
            table: selected_table,
            column_map: column_map,
            compare: window._export_compare
          };

          // UI state: show spinner, hide page
          show('browse-spinner');
          hide('browse-page-content');

          const modal = bootstrap.Modal.getInstance(document.getElementById('exportModal'));
          if (modal) modal.hide();

          // Tunables for polling
          const BASE_DELAY_MS = 500;
          const MAX_DELAY_MS = 3000;
          const MAX_ATTEMPTS = 180; // ~ up to a few minutes depending on backoff

          // Helpers
          const sleep = (ms) => new Promise(res => setTimeout(res, ms));

          function extract_filename(response) {
            const cd = response.headers.get('Content-Disposition');
            if (!cd) return 'download.xlsx';
            const m = /filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/i.exec(cd);
            try {
              if (m?.[1]) return decodeURIComponent(m[1]);
              if (m?.[2]) return m[2];
            } catch (_) {}
            return 'download.xlsx';
          }

          async function download_blob_response(response) {
            const filename = extract_filename(response);
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = filename || 'download.xlsx';
            document.body.appendChild(a);
            a.click();
            a.remove();
            window.URL.revokeObjectURL(url);
          }

          function handle_error_response(response) {
            if (response.status === 404) {
              alert('Your session may have expired. Please log in again.');
              location.reload();
              return true; // handled
            }
            return false; // not handled; caller should throw
          }

          async function poll_download(task_id, attempt = 0) {
            const delay = Math.min(BASE_DELAY_MS * Math.pow(1.5, attempt), MAX_DELAY_MS);
            if (attempt > 0) await sleep(delay);

            let response;
            try {
              response = await fetch(`/download?task_id=${encodeURIComponent(task_id)}&title=${window._export_page_title}`, {
                method: "GET",
                headers: { "Accept": "application/octet-stream,application/json;q=0.9,*/*;q=0.8" }
              });
            } catch (err) {
              throw new Error(`Network error while polling: ${err?.message || err}`);
            }

            // Continue polling
            if (response.status === 202) {
              // optionally inspect JSON: {status:"in-progress"} (ignored here)
              if (attempt + 1 >= MAX_ATTEMPTS) {
                throw new Error("Timed out waiting for export to finish.");
              }
              return poll_download(task_id, attempt + 1);
            }

            if (!response.ok) {
              if (handle_error_response(response)) return; // session expired handled
              // Try to parse server-provided message for 4xx/5xx
              let msg = `Export failed with status ${response.status}`;
              try {
                const data = await response.json();
                if (data?.error) msg += `: ${data.error}`;
              } catch (_) {
                // ignore parse errors
              }
              throw new Error(msg);
            }

            // Success: server returned the file as an attachment (e.g., 200)
            await download_blob_response(response);
          }

          (async () => {
            try {
              // Kick off the export job
              let post_response;
              try {
                post_response = await fetch("/download", {
                  method: "POST",
                  headers: { "Content-Type": "application/json", "Accept": "application/json" },
                  body: JSON.stringify(payload)
                });
              } catch (err) {
                throw new Error(`Network error during export start: ${err?.message || err}`);
              }

              if (post_response.status === 202) {
                // Expected async case: get task_id
                let data;
                try {
                  data = await post_response.json();
                } catch (_) {
                  throw new Error("Malformed 202 response: expected JSON with task_id.");
                }
                const task_id = data?.task_id;
                if (!task_id) {
                  throw new Error("Server did not provide a task_id.");
                }
                await poll_download(task_id);
              } else if (post_response.ok) {
                // Back-compat: if server still returns file immediately
                await download_blob_response(post_response);
              } else {
                if (handle_error_response(post_response)) return;
                let msg = `Export failed with status ${post_response.status}`;
                try {
                  const data = await post_response.json();
                  if (data?.error) msg += `: ${data.error}`;
                } catch (_) {}
                throw new Error(msg);
              }

            } catch (error) {
              console.error("Export error:", error);
              alert("Export failed: " + error.message);
            } finally {
              // Restore UI
              hide('browse-spinner');
              show('browse-page-content');
            }
          })();
        });

        // constrain range for watchlist cutoff date
        const today = new Date().toISOString().split('T')[0];
        const input = document.getElementById('watchlistCutoffDate');
        input.max = today;  // only allow up to today
        input.min = "2000-01-01";  // hard-coded example start date

        // issue #138: "hide minor changes" toggle, persisted server-side
        hide_insignificant = !!hide_insignificant_pref;
        const hide_insignificant_toggle = document.getElementById('hide-insignificant-toggle');
        if (hide_insignificant_toggle) {
            hide_insignificant_toggle.checked = hide_insignificant;
        }

        // Populate the interface
        console.log("loading watchlist")
        load_watchlist(check_all=true, initial_load=true);

        // archive select listener
        console.log("loading archive select");
        await populate_archive_select();
        console.log("archives =", archives);
        render_archives_table();

        // start with a default browse page (FIXME: remember user's last page)
        console.log("loading browse page")
        console.log("start_title=", start_title);
        if (start_title) {
            load_page_by_title(start_title);
        }
        else {
            load_page_by_title("Архів:ДАЖО/Д");
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('DOM fully loaded and parsed');
    init_tab_scroll_persistence();
    on_loaded();
});

