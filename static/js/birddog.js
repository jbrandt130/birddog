// (c) 2025 Jonathan Brandt
// Licensed under the MIT License. See LICENSE file in the project root.

// ---------------------------------------------------------------------------
// APP GLOBALS
var current_page            = null;
var archives                = null;
var watchlist               = null;
var unresolved_updates      = {};

const months                = [
    'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
    'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'
    ];

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
    return item != null && !item.includes("redlink");
}

function format_date(mod_date, strip_time=false) {
    if (!mod_date)
        return '';
    const parsed = mod_date.split(',');
    if (parsed.length <= 1)
        return mod_date;
    result = `${parsed[2]} ${months[Number(parsed[1])-1]} ${parsed[0]}`;
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

async function update_translation_progress(data) {
    //console.log('translate result:', data);
    const translations = data.translations || [];

    hide('progress-container');
    hide('translating-badge');

    for (const item of translations) {
        //console.log(item.page_name, current_page.name);
        if (item.page_name == current_page.name) {
            const progress_bar = document.getElementById("progress-bar");
            if (progress_bar) {
                const percent = (item.progress / item.total * 100).toFixed(1);
                //console.log(`Updating progress for ${item.page_name}: ${percent}%`);
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
        // Continue polling after 1 second
        setTimeout(async () => {
            try {
                const response = await fetch('/translate');
                if (!response.ok) {
                    throw new Error(`Polling failed: ${response.statusText}`);
                }
                const new_data = await response.json();
                update_translation_progress(new_data);
            } catch (err) {
                console.error("Polling error:", err);
            }
        }, 1000);
    }
    else {
        // reload in case we're on the translated page
        // FIXME: don't do this if not on a translated page
        load_page(
            current_page.archive,
            current_page.subarchive,
            current_page.fond,
            current_page.opus,
            current_page.case,
            compare=current_page.refmod ?? null);
    }
}

// ---------------------------------------------------------------------------
// BIRDDOG SERVICE CALLS

// page loader
async function load_page(
        archive_id,
        subarchive_id=null,
        fond_id=null,
        opus_id=null,
        case_id=null,
        compare=null) {
    try {
        // Default to an empty string if any parameter is null or undefined
        url = `/page?` +
            `archive=${encodeURIComponent(archive_id ?? '')}&` +
            `subarchive=${encodeURIComponent(subarchive_id ?? '')}&` +
            `fond=${encodeURIComponent(fond_id ?? '')}&` +
            `opus=${encodeURIComponent(opus_id ?? '')}&` +
            `case=${encodeURIComponent(case_id ?? '')}`;
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

        // Hide the spinner after loading
        hide('browse-spinner');
        show('browse-page-content');
    } catch (error) {
        console.error('Error loading page:', error.message);
        alert(`Failed to load data: ${error.message}`);
    }
}

async function translate_page() {
    //console.log('translate', current_page.name);
    const path = [
        current_page.archive,
        current_page.subarchive,
        current_page.fond || '',
        current_page.opus || '',
        current_page.case || '']
        .join('/').replace(/\/+$/, '');
    console.log('translating:', path);
    const response = await fetch(`/translate/${path}`);
    if (!response.ok) {
        throw new Error(`Failed to resolve: ${response.statusText}`);
    }
    const data = await response.json();
    console.log('translate_page:', data)
    update_translation_progress(data);
}

async function download_page() {
    try {
        // Default to an empty string if any parameter is null or undefined
        url = `/download?` +
            `archive=${encodeURIComponent(current_page.archive ?? '')}&` +
            `subarchive=${encodeURIComponent(current_page.subarchive ?? '')}&` +
            `fond=${encodeURIComponent(current_page.fond ?? '')}&` +
            `opus=${encodeURIComponent(current_page.opus ?? '')}&` +
            `case=${encodeURIComponent(current_page.case ?? '')}`;

        if ("refmod" in current_page) {
            url += `&compare=${current_page.refmod}`
        }
        console.log(`Fetching data from: ${url}`);

        // Show the spinner
        show('browse-spinner');
        hide('browse-page-content');

        // Make the GET request
        const response = await fetch(url, {
            method: 'GET'
        });

        if (!response.ok) {
            // Hide the spinner after loading
            hide('browse-spinner');
            show('browse-page-content');
            throw new Error(`HTTP error! Status: ${response.status}`);
        }

        // Convert the response to a Blob
        const blob = await response.blob();

        // Create an object URL for the Blob
        const blobUrl = window.URL.createObjectURL(blob);

        // Extract filename from Content-Disposition header (if available)
        const contentDisposition = response.headers.get('Content-Disposition');
        const filename = contentDisposition
            ? contentDisposition.split('filename=')[1]?.replace(/['"]/g, '')
            : 'download.xlsx';

        // Create a hidden <a> element to trigger the download
        const link = document.createElement('a');
        link.href = blobUrl;
        link.setAttribute('download', filename);
        document.body.appendChild(link);

        // Trigger download
        link.click();

        // Clean up
        document.body.removeChild(link);
        window.URL.revokeObjectURL(blobUrl);

        // Hide the spinner after loading
        hide('browse-spinner');
        show('browse-page-content');

    } catch (error) {
        // Hide the spinner after loading
        hide('browse-spinner');
        show('browse-page-content');

        console.error('Error loading page:', error.message);
        alert(`Failed to load data: ${error.message}`);
    }
}

// ---------------------------------------------------------------------------
// UI RENDERING AND HANDLERS

// handle table row click
function on_row_click(page_data, index) {
    console.log(`click on:  ${get_text(page_data.title)}[${index}]`);
    archive_id = page_data.archive;
    subarchive_id = page_data.subarchive;
    fond_id = page_data.fond;
    opus_id = page_data.opus;
    case_id = page_data.case;

    child_id = get_text(page_data.children[index][0].text);
    console.log(child_id);
    switch (page_data.kind) {
        case 'archive':
            fond_id = child_id;
            opus_id = null;
            case_id = null;
            break;

        case 'fond':
            opus_id = child_id;
            case_id = null;
            break;

        case 'opus':
            case_id = child_id;
            break;

        case 'case':
            console.log('Ignoring case expansion click...');
            return;

        default:
            return;
    }

    load_page(archive_id, subarchive_id, fond_id, opus_id, case_id);
}

// render a data page
function render_page_data(data) {
    const is_comparison = 'refmod' in data && data.refmod != data.lastmod;

    const title_elem = document.getElementById('page-title');
    title_elem.textContent = data.name;

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

    const lastmod = document.getElementById('last-modified');
    lastmod.textContent = format_date(data.lastmod);

    const source_link_elem = document.getElementById('source-link');
    source_link_elem.setAttribute('href', data.link);

    const children = data.children;
    const header = data.header;
    const header_elem = document.getElementById('page_table').querySelector('thead');
    var row = '<tr>';
    header.forEach((item, index) => {
        row += `<th>${get_text(item) || ''}</th>`;
    });
    row += '</tr>';
    header_elem.innerHTML = row;

    const body_elem = document.getElementById('page_table').querySelector('tbody');
    body_elem.innerHTML = ''; // Clear existing content

    var row_added = false;
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
                    console.log('link added:', cell_content);
                    cell_content = 
                        `<button class="btn btn-success btn-sm" style="opacity: 0.5;">
                            <i class="bi bi-link-45deg"></i>
                        </button> &nbsp;` + cell_content;
                    row_edited = true;
                    break;
                case 'changed':
                    console.log('link changed:', cell_content);
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
            if (data.kind != 'case' && is_linked(child[0].link)) {
                // Add click event listener
                row_elem.addEventListener('click', () => on_row_click(data, index));
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

    // watch button is only visible for archive level pages
    show_if('archive-watch-btn', data.kind == 'archive')

    show_if('comparing-badge', is_comparison);
    show_if('no-differences-badge', is_comparison && !any_edit);
    show_if('empty-page-badge', !row_added);
    show_if('needs-resolve-badge', needs_resolve(data));
    //show_if('translating-badge', data.translating ?? false);
    //show_if('progress-container', data.translating ?? false);



    // set button enables
    enable_if("resolve-btn", needs_resolve(data));
    enable_if("translate-btn", data.needs_translation);

    render_breadcrumbs(data);
    update_archive_select();
}

function render_history(data) {
    if (data.history.length <= 1) {
        hide('history-selection-box');
        show('new-page-badge');
    } else {
        show('history-selection-box');
        hide('new-page-badge');

        const selector = document.getElementById('version-select');
        const select_header = 'refmod' in data ? 'Stop Comparing' : 'Select Version';
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

        if ('refmod' in data) {
            // Find the latest item <= refmod
            const best_match = eligible_history.find(item => item.modified <= data.refmod);
            if (best_match) {
                selector.value = best_match.modified;
            }
        }
    }
}

function handle_breadcrumb_click(parts, index) {
    archive_id = parts[0].split('-');
    subarchive_id = archive_id[1];
    archive_id = archive_id[0];
    fond_id = index >= 1? parts[1] : '';
    opus_id = index >= 2? parts[2] : '';
    case_id = index >= 3? parts[3] : '';
    load_page(archive_id, subarchive_id, fond_id, opus_id, case_id);
}

function render_breadcrumbs(data) {
    const breadcrumbContainer = document.getElementById('breadcrumb');
    breadcrumbContainer.innerHTML = ''; // Clear existing content

    parts = [ `${data.archive}-${data.subarchive}` ];
    if (!empty(data.fond)) {
        parts.push(data.fond);
        if (!empty(data.opus)) {
            parts.push(data.opus);
            if (!empty(data.case)) {
                parts.push(data.case);
            }
        }
    }

    //console.log('parts = ', parts);
    if (parts.length == 1) {
        // no need for breadcrumbs
        return;
    }

    parts.forEach((part, index) => {
        const li = document.createElement('li');
        li.classList.add('breadcrumb-item');

        if (index === parts.length - 1) {
            // Final part - make it non-clickable (active)
            li.classList.add('active');
            li.setAttribute('aria-current', 'page');
            li.textContent = part;
        } else {
            // Intermediate parts - make them clickable
            const link = document.createElement('a');
            link.href = '#'; // Optional: Use an actual URL if needed
            link.textContent = part;
            link.addEventListener('click', (event) => {
                event.preventDefault();
                handle_breadcrumb_click(parts, index);
            });
            li.appendChild(link);
        }

        breadcrumbContainer.appendChild(li);
    });
}

function update_archive_select() {
    console.log('update_archive_select:', current_page.archive, current_page.subarchive);
    document.getElementById('archiveSelect').value = -1;
    archives.forEach((archive, index) => {
        if (archive[0] == current_page.archive && archive[1] == current_page.subarchive) {
            console.log('archive select:', index)
            document.getElementById('archiveSelect').value = index;
        }

    });
}

function populate_archive_select() {
    //console.log("populate_archive_select(): ", bootstrap)
    const archive_select_btn = document.getElementById('archive-select-btn');
    const archive_select_modal = new bootstrap.Modal(document.getElementById('archiveSelectModal'));
    const archive_select = document.getElementById('archiveSelect');
    const confirm_selection_btn = document.getElementById('confirmSelectionBtn');

    // Fetch archives from the server
    async function fetch_archives() {
        try {
            const response = await fetch('/archives');
            if (!response.ok) {
                throw new Error(`Failed to fetch archives: ${response.statusText}`);
            }
            archives = await response.json();
            console.log('archive list loaded:', archives);
            populate_archive_select_dropdown(archives);
            populate_watchlist_archive_select(archives);
        } catch (error) {
            console.error('Error fetching archives:', error);
            alert('Failed to load archives. Please try again.');
        }
    }

    // Populate the archive dropdown
    function populate_archive_select_dropdown(archives) {
        archive_select.innerHTML = '<option value="-1" selected>Select an archive...</option>';
        archives.forEach((archive, index) => {
            //console.log(archive);
            const option = document.createElement('option');
            value = `${archive[0]}-${archive[1]}`
            option.value = index;
            option.textContent = value;
            archive_select.appendChild(option);
        });
    }

    fetch_archives();  // Fetch latest archive list when the modal opens

    // Handle Confirm button click
    confirm_selection_btn.addEventListener('click', () => {
        const archive_index = archive_select.value;
        if (!archive_index || archive_index < 0 || archive_index >= archives.length) {
            alert('Please select an archive.');
            return;
        }
        const selected_archive = archives[archive_index];
        console.log(`Selected Archive: ${selected_archive[0]}-${selected_archive[1]}`);
        load_page(
            archive_id=selected_archive[0],
            subarchive_id=selected_archive[1])
        archive_select_modal.hide();
    });
}

// ---------------------------------------------------------------------------
// WATCHLIST MANAGEMENT

async function load_watchlist(check_all=false, initial_load=false) {
    const response = await fetch('/watchlist');
    const data = await response.json();
    console.log('watchlist:', data)
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

    // Sort by archive, then subarchive
    const sorted_watchlist = [...watchlist].sort((a, b) => {
        const archive_cmp = a.archive.localeCompare(b.archive);
        return archive_cmp !== 0 ? archive_cmp : a.subarchive.localeCompare(b.subarchive);
    });

    sorted_watchlist.forEach(item => {
        const row = `
            <tr data-archive="${item.archive}" data-subarchive="${item.subarchive}">
                <td>${item.archive}</td>
                <td>${item.subarchive}</td>
                <td>${format_date(item.last_checked_date)}</td>
                <td>${format_date(item.cutoff_date)}</td>
                <td>
                    <button class="btn btn-primary" title="Check for Updates" onclick="check_watchlist('${item.archive}', '${item.subarchive}')">
                        <i class="bi bi-arrow-clockwise"></i>
                    </button>
                </td>
                <td>
                    <button class="btn btn-primary" title="Remove from Watchlist" onclick="remove_from_watchlist('${item.archive}', '${item.subarchive}')">
                        <i class="bi bi-x-square"></i>
                    </button>
                </td>
            </tr>
        `;
        table_body.innerHTML += row;
    });

    // scroll to top automatically
    document.getElementById('nav-home').scrollTo({ top: 0, behavior: 'smooth' });
}

async function remove_from_watchlist(archive, subarchive) {
    await fetch(`/watchlist/${archive}/${subarchive}`, { method: 'DELETE' });
    load_watchlist(); // Refresh after deletion
    const key = `${archive}-${subarchive}`
    if (key in unresolved_updates) {
        delete unresolved_updates[key];
        render_unresolved_items();
    }
}

async function check_watchlist(archive, subarchive, quiet=false, render=true) {
    console.log(`Checking ${archive}-${subarchive}...`);
    try {
        // Show the spinner
        show('unresolved-updates-loading-spinner');
        hide('unresolved-updates-container');

        const response = await fetch(`/watchlist/${archive}/${subarchive}/check?tree`);
        if (!response.ok) {
            // Hide the spinner
            show('unresolved-updates-container');
            hide('unresolved-updates-loading-spinner');
            throw new Error(`Failed to check updates: ${response.statusText}`);
        }
        const data = await response.json();

        console.log(`Checking ${archive}-${subarchive}: unresolved items: ${data}`);
        unresolved_updates[`${archive}-${subarchive}`] = data.unresolved;
        if (render) {
            // Hide the spinner
            show('unresolved-updates-container');
            hide('unresolved-updates-loading-spinner');       
            render_unresolved_items();
        }
        if (!quiet && data.unresolved.length == 0)
            alert(`No new updates for ${archive}-${subarchive}.`);
    } catch (error) {
        // Hide the spinner
        show('unresolved-updates-container');
        hide('unresolved-updates-loading-spinner');
        console.error('Error checking updates:', error);
        alert('Failed to check updates.');
    }
}

async function check_all_watchlists() {
    show('unresolved-updates-loading-spinner');
    hide('unresolved-updates-container');

    const promises = watchlist.map(item =>
        check_watchlist(item.archive, item.subarchive, true, false)
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
        archives.forEach(archive => {
            const option = document.createElement('option');
            value = `${archive[0]}-${archive[1]}`
            option.value = value;
            option.textContent = value;
            archive_select.appendChild(option);
        });
    } catch (error) {
        console.error('Error fetching archives:', error);
        alert('Failed to load archives.');
    }
}

// Confirm adding to the watchlist
async function confirm_add_to_watchlist() {
    var archive = document.getElementById('watchlistArchiveSelect').value.split('-');
    const cutoff_date = document.getElementById('watchlistCutoffDate').value.replace(/-/g, ',');
    const subarchive = archive[1];
    archive = archive[0];
    console.log(archive, subarchive, cutoff_date);
    if (!archive || !subarchive || !cutoff_date) {
        alert('All fields are required.');
        return;
    }

    // Close the modal using Bootstrap API
    const modal = bootstrap.Modal.getInstance(document.getElementById('addWatchlistModal'));
    modal.hide();

    // Show the spinner
    show('watchlist-loading-spinner');
    hide('watchlist-container');

    await fetch('/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            archive: archive,
            subarchive: subarchive,
            cutoff_date: cutoff_date
        })
    });

    hide('watchlist-loading-spinner');
    show('watchlist-container');

    // Refresh table
    load_watchlist(check_all=true);
}

// ---------------------------------------------------------------------------
// UNRESOLVED UPDATES TREE NAVIGATOR

var node_map = null;
var path_to_node = null;

function page_path(page) {
    const archive = `${page.archive}-${page.subarchive}`;
    return [archive, page.fond, page.opus, page.case]
        .filter(Boolean)
        .join('/');
}

function needs_resolve(page) {
    if (!path_to_node) 
        return false;
    const path = page_path(page);
    //console.log('needs_resolve:', path);
    return path in path_to_node;
}

function build_tree(data_list) {
    const root = {};
    for (const [path, meta] of data_list) {
        const parts = path.split('/');
        let current = root;
        for (const part of parts) {
            if (!current[part]) current[part] = {};
                current = current[part];
        }
        current._meta = meta;
        current._full_path = path;
    }
    return root;
}

function view_changes(full_path, modified, last_resolved) {
    console.log("Viewing changes for", full_path, last_resolved);
    const path = full_path.split('/');
    const archive = path[0].split('-');
    compare = last_resolved ?? null;
    load_page(
        archive[0],
        subarchive_id=archive[1],
        fond_id=path.length > 1? path[1] : null,
        opus_id=path.length > 2? path[2] : null,
        case_id=path.length > 3? path[3] : null,
        compare=compare);
    // Switch to the browse tab
    show_tab('nav-browse-tab');
}


async function resolve_page_update(page_name, deep=false) {
    try {
        const path = page_name.replace(/,+$/, '').replace(/,/g, '/');
        console.log('Resolving:', path);
        var url = `/resolve/${path}?tree=1`;
        if (deep)
            url += '&deep=1';
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`Failed to resolve: ${response.statusText}`);
        }

        // update unresolved item table
        const data = await response.json();
        const parsed_path = path.split('/');
        console.log('resolve result:', data);
        unresolved_updates[`${parsed_path[0]}-${parsed_path[1]}`] = data.unresolved;

        render_unresolved_items();
    } catch (error) {
        console.error('Error during resolve:', error);
        alert('Failed to resolve.');
    }
}

function mark_resolved(node_id) {
    // full_path.replace(/'/g, "\\'")
    //console.log(full_path)
    const node = node_map[node_id];
    const has_children = Object.keys(node).some(key => !key.startsWith('_'));
    const full_path = node._full_path || name;
    var deep = false;
    const confirm_message = has_children?
        `${full_path} has unresolved subsidiary pages. Resolve all subsidiaries?` : 
        `Resolve ${full_path}?`; 
    if (confirm(confirm_message)) {
        console.log('resolving all children');
    }
    else {
        console.log('resolve cancelled by user');
        return false;
    }
    const path = full_path.split('/');
    const archive = path[0].split('-');
    const new_path = archive.concat(path.slice(1)).join(',')
    console.log("Marking resolved:", new_path);
    resolve_page_update(new_path, deep=has_children);
    return true;
}

// called from resolve button on browse page
function resolve_page() {
    const path = page_path(current_page);
    if (!path_to_node) 
        return false;
    const node = path_to_node[path];
    if (mark_resolved(node._id)) {
        enable_if("resolve-btn", false);
        hide('needs-resolve-badge');
    }
}

const closed_icon = "bi-plus-circle-fill";
const open_icon = "bi-dash-circle-fill";

function render_node(name, node) {
    const node_id = 'id_' + Math.random().toString(36).substring(2, 10);
    node._id = node_id;
    node_map[node_id] = node;
    const has_children = Object.keys(node).some(key => !key.startsWith('_'));
    const meta = node._meta;
    const full_path = node._full_path || name;
    path_to_node[full_path] = node;
    const modified = meta? meta.modified : '';
    const last_resolved = meta? meta.last_resolved : '';
    //console.log(last_resolved);

    const update_text = meta ? `Latest Update: ${format_date(meta.modified, true)}` : '';
    const resolved_text = meta ? `Last Resolved: ${format_date(meta.last_resolved, true)}` : '';

    const button_html =
    `<button class="btn btn-sm btn-primary" title="View Changes" onclick="view_changes(
        '${full_path.replace(/'/g, "\\'")}', '${modified}', '${last_resolved}')">
          <i class="bi bi-eye"></i>
        </button>
        <button class="btn btn-sm btn-primary" title="Mark Resolved" onclick="mark_resolved('${node_id}')">
          <i class="bi bi-check-square"></i>
    </button>`;


    const name_html = has_children
    ? `<a data-bs-toggle="collapse" href="#${node_id}" role="button" aria-expanded="false" aria-controls="${node_id}">
            <i class="bi ${closed_icon} arrow" data-arrow="closed"></i>
            <span class="tree-label ms-1" data-path="${full_path}">${name}</span>
    </a>`
    : `<span class="tree-label" data-path="${full_path}">${name}</span>`;

    const meta_html = meta
    ? `<div class="text-muted small">
             <div>${update_text}</div>
             <div>${resolved_text}</div>
    </div>`
    : '';

    const row_layout = `
    <div class="d-flex align-items-center justify-content-between">
      <div class="d-flex flex-column flex-grow-1">
        ${name_html}
        ${meta_html}
      </div>
      <div class="ms-3">${button_html}</div>
    </div>
    `;

    if (!has_children) {
        return `<li class="list-group-item">${row_layout}</li>`;
    }

    const children_html = Object.entries(node)
        .filter(([key]) => !key.startsWith('_'))
        .map(([child_name, child_node]) => render_node(child_name, child_node))
        .join('');

    return `
    <li class="list-group-item">
      ${row_layout}
      <div class="collapse ms-3 mt-1" id="${node_id}">
        <ul class="list-group">
          ${children_html}
        </ul>
      </div>
    </li>
    `;
}

function render_tree(tree) {
    const top_level = Object.entries(tree)
        .map(([name, node]) => render_node(name, node))
        .join('');
    return `<ul class="list-group">${top_level}</ul>`;
}

function render_tree_to_dom(data_list, container_id) {
    const tree = build_tree(data_list);
    const html = render_tree(tree);

    const container = document.getElementById(container_id);
    const wrapper = document.createElement('div'); // Optional: separates each tree visually
    //wrapper.classList.add('mb-3');
    wrapper.innerHTML = html;
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.appendChild(wrapper);
    //td.innerHTML = html;
    tr.appendChild(td);
    container.appendChild(tr);

    // Attach arrow toggles and label click handlers as before...
    wrapper.querySelectorAll('.collapse').forEach(collapse => {
        collapse.addEventListener('show.bs.collapse', e => {
            const arrow = wrapper.querySelector(`a[href="#${collapse.id}"] .arrow`);
            if (arrow) {
                arrow.classList.remove(closed_icon);
                arrow.classList.add(open_icon);
            }
        });
        collapse.addEventListener('hide.bs.collapse', e => {
            const arrow = wrapper.querySelector(`a[href="#${collapse.id}"] .arrow`);
            if (arrow) {
                arrow.classList.remove(open_icon);
                arrow.classList.add(closed_icon);
            }
        });
    });

    wrapper.querySelectorAll('.tree-label').forEach(label => {
        label.addEventListener('click', e => {
          const path = label.getAttribute('data-path');
          console.log('Node clicked:', path);
          wrapper.querySelectorAll('.tree-label').forEach(el => el.classList.remove('selected'));
          label.classList.add('selected');
      });
    });
}

function get_expanded_nodes(container_id) {
    const expanded = [];
    document.querySelectorAll(`#${container_id} .collapse.show`).forEach(el => {
        const trigger = document.querySelector(`a[href="#${el.id}"] .tree-label`);
        if (trigger) {
            expanded.push(trigger.getAttribute('data-path'));
        }
    });
    return expanded;
}

function restore_expanded_nodes(container_id, expanded_paths) {
    const container = document.getElementById(container_id);
    if (!container) return;

    expanded_paths.forEach(path => {
        const label = container.querySelector(`.tree-label[data-path="${path}"]`);
        if (label) {
            const link = label.closest('a');
            if (link && link.getAttribute('href')?.startsWith('#')) {
                const collapse_id = link.getAttribute('href').slice(1);
                const collapse_el = document.getElementById(collapse_id);
                if (collapse_el) {
                    const collapse = new bootstrap.Collapse(collapse_el, { toggle: false });
                    collapse.show();
                }
            }
        }
    });
}

function render_unresolved_items() {
    const container_id = 'tree-container';
    const expanded_paths = get_expanded_nodes(container_id);

    node_map = {};
    path_to_node = {};
    document.getElementById(container_id).innerHTML = ''; 

    // Sort by keys in unresolved_updates
    const sorted_keys = Object.keys(unresolved_updates).sort(); // alphabetical sort

    sorted_keys.forEach(key => {
        const item = unresolved_updates[key];
        render_tree_to_dom(item, 'tree-container');
    });

    restore_expanded_nodes(container_id, expanded_paths);
}

// ---------------------------------------------------------------------------
// APP INITIALIZATION

function on_loaded() {
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
        // version select listener
        const selector = document.getElementById('version-select');
        selector.addEventListener('change', (event) => {
            const version = event.target.value;
            console.log(`Comparing to: ${version}`);
            load_page(
                current_page.archive,
                current_page.subarchive,
                current_page.fond,
                current_page.opus,
                current_page.case,
                compare=version);
            //alert(`Comparing to version ${selectedVersion}`)
        });

        // Handle Unresolved Updates Row Click
        const unresolved_updates_body = document.getElementById('unresolved-updates-body');
        if (unresolved_updates_body) {
            unresolved_updates_body.addEventListener('click', (event) => {
                const row = event.target.closest('tr');
                if (row) {
                    const page_id = row.dataset.pageId.split(','); // Accesses data-page-id
                    const last_resolved = row.dataset.lastResolved; // Accesses data-last-resolved
                    console.log(`Page ID: ${page_id}, Last Resolved: ${last_resolved}`);
                    try {
                        // load the selected page
                        load_page(page_id[0], page_id[1], page_id[2], page_id[3], page_id[4],
                            compare=last_resolved);
                        // Switch to the browse tab
                        show_tab('nav-browse-tab');
                    } catch (error) {
                        console.error('Error fetching archives:', error);
                        alert('Failed to load', page_id);
                    }
                }
            });
        }

        // Attach click event to the whole table body
        const watchlist_body = document.getElementById('watchlist-body');
        watchlist_body.addEventListener('click', (event) => {
            const row = event.target.closest('tr');
            if (row && !event.target.closest('button')) {
                const archive = row.dataset.archive;
                const subarchive = row.dataset.subarchive;
                if (archive && subarchive) {
                    console.log(`browse: ${archive}-${subarchive}`)
                    // load the selected page
                    load_page(archive, subarchive, '', '');
                    // Switch to the browse tab
                    show_tab('nav-browse-tab');
                }
            }
        });

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


        // Populate the interface
        load_watchlist(check_all=true, initial_load=true);

        // archive select listener
        populate_archive_select();

        // start with a default browse page (FIXME: remember user's last page)
        load_page("DAZHO");
    }
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('DOM fully loaded and parsed');
    on_loaded();
});

