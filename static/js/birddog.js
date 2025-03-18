





// global app state
var current_page = null;

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

// page loader
async function load_page(archive_id, fond_id=null, opus_id=null, case_id=null, translate=false, compare=null) {
    try {
        // Default to an empty string if any parameter is null or undefined
        url = `/page?` + 
            `archive=${encodeURIComponent(archive_id ?? '')}&` + 
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
    load_page(current_page.archive, current_page.fond, current_page.opus, current_page.case, translate=true)
}

async function download_page() {
    try {
        // Default to an empty string if any parameter is null or undefined
        url = `/download?` + 
            `archive=${encodeURIComponent(current_page.archive ?? '')}&` + 
            `fond=${encodeURIComponent(current_page.fond ?? '')}&` + 
            `opus=${encodeURIComponent(current_page.opus ?? '')}&` + 
            `case=${encodeURIComponent(current_page.case ?? '')}`;

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

// handle table row click
function on_row_click(page_data, index) {
    console.log(`click on:  ${get_text(page_data.title)}[${index}]`);
    archive_id = page_data.archive;
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

    load_page(archive_id, fond_id, opus_id, case_id);
}

// render a data page
function render_page_data(data) {
    const is_comparison = 'refmod' in data;
    
    const title_elem = document.getElementById('page-title');
    title_elem.textContent = get_text(data.title);
    
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
    lastmod.textContent = data.lastmod;
    
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

    render_breadcrumbs(data);
}

function render_history(data) {

    const selector = document.getElementById('version-select');

    // Clear existing options
    select_header = 'refmod' in data? 'Clear Comparing' : 'Select Version';
    selector.innerHTML = `<option value="" selected>${select_header}</option>`;
    selector.disabled = false;

    // Add new options dynamically
    //console.log('adding history: ', data.history.length);
    //const history_slice = data.history.slice(1, 20);
    data.history.forEach(item => {
        if (item.modified != data.lastmod) {
            const option = document.createElement('option');
            option.value = item.modified;   // Set the value
            option.textContent = item.modified; // Display text
            //console.log('adding select item: ', item.modified);
            selector.appendChild(option);
        }
    });

    if ('refmod' in data) {
        selector.value = data.refmod;
    }
}

function handle_breadcrumb_click(parts, index) {
    archive_id = parts[0];
    fond_id = index >= 1? parts[1] : '';
    opus_id = index >= 2? parts[2] : '';
    case_id = index >= 3? parts[3] : '';
    load_page(archive_id, fond_id, opus_id, case_id);
}

function render_breadcrumbs(data) {
    const breadcrumbContainer = document.getElementById('breadcrumb');
    breadcrumbContainer.innerHTML = ''; // Clear existing content

    parts = [ data.archive ];
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


function bd_on_loaded() {
    console.log('bd_on_loaded triggered!');
    
    // set up event listeners
    const selector = document.getElementById('version-select');
    selector.addEventListener('change', (event) => {
        const version = event.target.value;
        console.log(`Comparing to: ${version}`);
        load_page(
            current_page.archive, 
            current_page.fond, 
            current_page.opus, 
            current_page.case, 
            translate=false,
            compare=version);
        //alert(`Comparing to version ${selectedVersion}`)
    });
    load_page("DAZHO");
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('DOM fully loaded and parsed');
    bd_on_loaded();
});

