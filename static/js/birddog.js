
// ---------------------------------------------------------------------------
// APP GLOBALS
var current_page = null;
var archives = null;

const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

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

function format_date(mod_date) {
    const parsed = mod_date.split(',');
    if (parsed.length <= 1)
        return mod_date;
    result = `${parsed[2]} ${months[Number(parsed[1])-1]} ${parsed[0]}`;
    if (parsed.length > 3)
         result += ` ${parsed[3]}`;
    return result;
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
        translate=false,
        compare=null) {
    try {
        // Default to an empty string if any parameter is null or undefined
        url = `/page?` + 
            `archive=${encodeURIComponent(archive_id ?? '')}&` + 
            `subarchive=${encodeURIComponent(subarchive_id ?? '')}&` + 
            `fond=${encodeURIComponent(fond_id ?? '')}&` + 
            `opus=${encodeURIComponent(opus_id ?? '')}&` + 
            `case=${encodeURIComponent(case_id ?? '')}`;
        if (translate)
            url += "&translate";
        if (compare != null)
            url += `&compare=${compare}`
        console.log(`Fetching data from: ${url}`);

        // Make the GET request
        const response = await fetch(url, {
            method: 'GET',
            headers: {
                'Accept': 'application/json'
            }
        });

        if (!response.ok) {
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

    } catch (error) {
        console.error('Error loading page:', error.message);
        alert(`Failed to load data: ${error.message}`);
    }
}

function translate_page() {
    load_page(
        current_page.archive, 
        current_page.subarchive, 
        current_page.fond, 
        current_page.opus, 
        current_page.case, 
        translate=true)
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

         // Make the GET request
        const response = await fetch(url, {
            method: 'GET'
        });

        if (!response.ok) {
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

    } catch (error) {
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
    const is_comparison = 'refmod' in data;
    
    const title_elem = document.getElementById('page-title');
    title_elem.textContent = data.name;
    
    const desc_elem = document.getElementById('page-description');
    desc_elem.textContent = get_text(data.description);
    desc_elem.classList.remove('bg-warning', 'bg-success');
    if (is_comparison && 'edit' in data.description) {
        switch (data.description.edit) {
            case 'added':
                desc_elem.classList.add('bg-success');
                break;
            case 'changed':
                desc_elem.classList.add('bg-warning');
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

    children.forEach((child, index) => {
        var any_edit = false;
        const row_elem = document.createElement('tr');
        child.forEach((item, index) => {
            const cell_elem = document.createElement('td')
            cell_elem.textContent = get_text(item.text) || ''
            if (is_comparison && 'edit' in item) {
                switch (item.edit) {
                case 'added':
                    cell_elem.classList.add('table-success');
                    any_edit = true;
                    break;
                case 'changed':
                    cell_elem.classList.add('table-warning');
                    any_edit = true;
                    break;
                default:
                    break;
                }
            }
            row_elem.appendChild(cell_elem)
        });

        if (!is_comparison || any_edit) {
            // only add the row if not doing comparison or there is a change to show
            if (data.kind != 'case' && is_linked(child[0].link)) {
                // Add click event listener
                row_elem.addEventListener('click', () => on_row_click(data, index));
            }
            else {
                row_elem.classList.add('table-secondary', 'disabled');
                row_elem.style.pointerEvents = 'none';
                row_elem.style.opacity = '0.25'; // Dim for better visibility
            }

            body_elem.appendChild(row_elem);
        }
    });

    // watch button is only visible for archive level pages
    const eye_elem = document.getElementById('archive-watch-btn');
    if (data.kind == 'archive') {
        eye_elem.classList.remove('d-none');
    }
    else {
        eye_elem.classList.add('d-none');
    }
    render_breadcrumbs(data);
    update_archive_select();
}

function render_history(data) {

    const selector = document.getElementById('version-select');

    // Clear existing options
    select_header = 'refmod' in data? 'Clear Comparing' : 'Select Version';
    if (data.history.length <= 1)
        select_header = '-';
    selector.innerHTML = `<option value="" selected>${select_header}</option>`;
    selector.disabled = false;

    // Add new options dynamically
    //console.log('adding history: ', data.history.length);
    //const history_slice = data.history.slice(1, 20);
    data.history.forEach(item => {
        if (item.modified != data.lastmod) {
            const option = document.createElement('option');
            option.value = item.modified;   // Set the value
            option.textContent = format_date(item.modified); // Display text
            //console.log('adding select item: ', item.modified, format_date(item.modified));
            selector.appendChild(option);
        }
    });

    if ('refmod' in data) {
        selector.value = data.refmod;
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
    document.getElementById('archiveSelect').value = current_page.archive;
    document.getElementById('subarchiveSelect').value = current_page.subarchive;
}

function populate_archive_select() {
    console.log("populate_archive_select(): ", bootstrap)
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
        archive_select.innerHTML = '<option value="" selected>Select an archive...</option>'; 
        Object.keys(archives).forEach(archive => {
            const option = document.createElement('option');
            option.value = archive;
            option.textContent = archive;
            archive_select.appendChild(option);
        });
    }

    fetch_archives();  // Fetch latest archive list when the modal opens

    // Handle Confirm button click
    confirm_selection_btn.addEventListener('click', () => {
        const selected_archive = archive_select.value;
        const selected_subarchive = document.getElementById('subarchiveSelect').value;

        if (!selected_archive) {
            alert('Please select an archive.');
            return;
        }

        console.log(`Selected Archive: ${selected_archive}`);
        console.log(`Selected Subarchive: ${selected_subarchive}`);
        if (selected_archive in archives) {
            load_page(
                archive_id=selected_archive, 
                subarchive_id=selected_subarchive)
        }
        archive_select_modal.hide();
    });
}

// ---------------------------------------------------------------------------
// WATCHLIST MANAGEMENT

async function load_watchlist() {
    const response = await fetch('/watchlist');
    const data = await response.json();

    const table_body = document.getElementById('watchlist-body');
    table_body.innerHTML = '';

    data.forEach(item => {
        const row = `
            <tr data-archive="${item.archive}" data-subarchive="${item.subarchive}">
                <td>${item.archive}</td>
                <td>${item.subarchive}</td>
                <td>${format_date(item.last_checked_date)}</td>
                <td>${format_date(item.cutoff_date)}</td>
                <td>
                    <button class="btn btn-primary" onclick="check_watchlist('${item.archive}', '${item.subarchive}')">
                        <i class="bi bi-arrow-clockwise"></i>
                    </button>
                </td>
                <td>
                    <button class="btn btn-primary" onclick="remove_from_watchlist('${item.archive}', '${item.subarchive}')">
                        <i class="bi bi-x-square"></i>
                    </button>
                </td>
            </tr>
        `;
        table_body.innerHTML += row;
    });
}

async function remove_from_watchlist(archive, subarchive) {
    await fetch(`/watchlist/${archive}/${subarchive}`, { method: 'DELETE' });
    load_watchlist(); // Refresh after deletion
}

function resolve_page_update(page_name) {
    console.log('resolve:', page_name)
}

async function check_watchlist(archive, subarchive) {
    console.log(`Checking ${archive}-${subarchive}...`);
    try {
        const response = await fetch(`/watchlist/${archive}/${subarchive}/check`);
        if (!response.ok) {
            throw new Error(`Failed to check updates: ${response.statusText}`);
        }

        const data = await response.json();

        const table_body = document.getElementById('unresolved-updates-body');
        table_body.innerHTML = '';

        console.log(data.unresolved);
        data.unresolved.forEach(item => {
            let name = item.name.split(',');
            console.log(name);
            const row = `
                <tr data-page-id="${item.name}" data-last-resolved="${item.last_resolved}">
                    <td>${name[0]}-${name[1]}/${name[2]}/${name[3]}/${name[4]}</td>
                    <td>${format_date(item.modified)}</td>
                    <td>${format_date(item.last_resolved)}</td>
                    <td>
                        <button class="btn btn-primary" onclick="event.stopPropagation(); resolve_page_update('${item.name}')">
                            <i class="bi bi-check-square"></i>
                        </button>
                    </td>
                </tr>
            `;
            table_body.innerHTML += row;
        });
    } catch (error) {
        console.error('Error checking updates:', error);
        alert('Failed to check updates.');
    }
}

async function add_to_watchlist() {
    const archive = prompt("Enter Archive Name:");
    const subarchive = prompt("Enter Subarchive:");
    const last_checked_date = prompt("Enter Last Checked Date (yyyy,MM,dd,hh:mm):");
    const cut_off_date = prompt("Enter Cutoff Date (yyyy,MM,dd,hh:mm):");

    if (archive && subarchive && last_checked_date && cut_off_date) {
        await fetch('/watchlist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                archive,
                subarchive,
                last_checked_date,
                cut_off_date
            })
        });
        load_watchlist(); // Refresh after adding
    }
}

// Populate the archive select dropdown
async function populate_watchlist_archive_select(archives) {
    const archive_select = document.getElementById('watchlistArchiveSelect');
    archive_select.innerHTML = '<option value="" selected>Select an archive...</option>';

    try {
        Object.keys(archives).forEach(archive => {
            const option = document.createElement('option');
            option.value = archive;
            option.textContent = archive;
            archive_select.appendChild(option);
        });
    } catch (error) {
        console.error('Error fetching archives:', error);
        alert('Failed to load archives.');
    }
}

// Confirm adding to the watchlist
async function confirm_add_to_watchlist() {
    const archive = document.getElementById('watchlistArchiveSelect').value;
    const subarchive = document.getElementById('watchlistSubarchiveSelect').value;
    const cutoff_date = document.getElementById('watchlistCutoffDate').value.replace(/-/g, ',');
    
    console.log(archive, subarchive, cutoff_date);
    if (!archive || !subarchive || !cutoff_date) {
        alert('All fields are required.');
        return;
    }

    await fetch('/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            archive: archive,
            subarchive: subarchive,
            cutoff_date: cutoff_date
        })
    });

    // Close the modal using Bootstrap API
    const modal = bootstrap.Modal.getInstance(document.getElementById('addWatchlistModal'));
    modal.hide();

    // Refresh table
    load_watchlist();
}

// ---------------------------------------------------------------------------
// APP INITIALIZATION

function on_loaded() {
    // set up event listeners
    
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
                translate=false,
                compare=version);
            //alert(`Comparing to version ${selectedVersion}`)
        });

        // Handle Unresolved Updates Row Click
        const unresolved_updates_body = document.getElementById('unresolved-updates-body');
        if (unresolved_updates_body) {
            unresolved_updates_body.addEventListener('click', (event) => {
                const row = event.target.closest('tr');
                if (row) {
                    const page_id = row.dataset.pageId; // Accesses data-page-id
                    const last_resolved = row.dataset.lastResolved; // Accesses data-last-resolved
                    console.log(`Page ID: ${page_id}, Last Resolved: ${last_resolved}`);
            
                    //if (page_id) {
                    //    window.location.href = `/diff/${page_id}`;
                    //}
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
                    //browse_archive(archive, subarchive);
                }
            }
        });

        load_watchlist();

        // archive select listener
        populate_archive_select();

        load_page("DAZHO");
    }
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('DOM fully loaded and parsed');
    on_loaded();
});

