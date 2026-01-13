# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Wiki API access functions
"""

import time
import json
import re
import os
from datetime import datetime
from urllib.parse import quote, unquote, urlparse
from itertools import islice

import requests
import mwparserfromhell
from cachetools import LRUCache
from bs4 import BeautifulSoup

from birddog.utility import (
    from_utc_format,
    to_utc_format,
    equal_text,
    fetch_url,
    form_text_item,
    format_date,
    get_text,
    is_linked,
    )
from birddog.translate import translate_structure

from birddog.log import get_logger
_logger = get_logger()

# INITIALIZATION --------------------------------------------------------------

# global constants

ARCHIVE_BASE    = 'https://uk.wikisource.org'
WIKI_NAMESPACE  = 'Архів'
WIKI_NAMESPACE_ID = '116' # use lookup_namespace_id() to find this out
ARCHIVES        = None
API_URL         = f"{ARCHIVE_BASE}/w/api.php"

# load static data resources

_ARCHIVE_MASTER_PATH = 'resources/archives_master.json'
_NONEXISTENT_PAGE_PATH = 'resources/nonexistent_page.json'

with open(_ARCHIVE_MASTER_PATH, encoding="utf8") as f:
    _archive_data = json.load(f)
    ARCHIVES = _archive_data['archives']

def _inventory_subarchives(archives):
    subarchives = {}
    for arc in archives.values():
        for sub in arc.values():
            subarchives[sub['subarchive']['uk']] = sub['subarchive']
    return list(subarchives.values())

SUBARCHIVES = _inventory_subarchives(ARCHIVES)

def canonicalize_title(title):
    if not title.startswith(f"{WIKI_NAMESPACE}:"):
        title = f"{WIKI_NAMESPACE}:{title}"
    return title.replace(" ", "_")

def _archives_init():
    archives_by_root = {}
    archive_by_title = {}
    archive_by_address = {}
    for archive_key in ARCHIVES.keys():
        archive_entries = ARCHIVES[archive_key].values()
        for entry in archive_entries:
            subarchive_key = entry["subarchive"]["en"]
            archive_title = canonicalize_title(entry["title"]["uk"])
            address = (archive_key, subarchive_key)
            archive_by_title[archive_title] = address
            archive_by_address[address] = archive_title
            if len(archive_entries) == 1 or subarchive_key in "D_":
                # add subarchive defaults
                archive_by_address[(address[0], "")] = archive_title
                archive_by_address[(address[0], None)] = archive_title
            root = archive_title.split("/")[0]
            if root in archives_by_root:
                archives_by_root[root].append(address)
            else:
                archives_by_root[root] = [address]
    return archives_by_root, archive_by_title, archive_by_address

ARCHIVES_BY_ROOT, ARCHIVE_BY_TITLE, ARCHIVE_BY_ADDRESS = _archives_init()

def archive_root(archive, subarchive):
    return ARCHIVE_BY_ADDRESS[(archive, subarchive)].split("/", 1)[0]

def classify_page(title):
    title = canonicalize_title(title)
    if title in ARCHIVE_BY_TITLE:
        return "archive"
    title_split = title.split("/")
    if len(title_split) <= 2:
        return "fond"
    if len(title_split) == 3:
        return "opus"
    return "case"

def parent_title(title):
    title = canonicalize_title(title)
    if title in ARCHIVE_BY_TITLE:
        # top level page for an archive
        return None
    title_split = title.split("/")
    if title_split[0] not in ARCHIVES_BY_ROOT:
        raise ValueError(f"Unrecognized archive root: {title}")
    if len(title_split) > 2:
        return title.rsplit("/", 1)[0]
    # hard case: locate parent of a fond
    if len(title_split) < 2:
        raise ValueError(f"Unrecognized title: {title}")
    archives = ARCHIVES_BY_ROOT[title_split[0]]
    fond_id = title_split[1]
    if len(archives) == 1:
        # unambiguous - return the one archive
        return ARCHIVE_BY_ADDRESS[archives[0]]
    archive_spec = ARCHIVES[archives[0][0]]
    default_title = None
    for sub in archive_spec.values():
        # look for subarchive string like "P" that is in the fond name
        if sub["subarchive"]["uk"] in fond_id:
            return canonicalize_title(sub["title"]["uk"])
        # subarchives "D" and "_" are the backup if no match is found
        if sub["subarchive"]["en"] in "D_":
            default_title = canonicalize_title(sub["title"]["uk"])
    if default_title:
        return default_title
    raise RuntimeError(f"Unable to find parent of {title} (searched {archives[0][0]})")

def lineage(title):
    title = canonicalize_title(title)
    #_logger.info(f"lineage({title})")
    result = []
    while title:
        result.append(title)
        title = parent_title(title)
    return result

def page_address(title):
    title = canonicalize_title(title)
    hierarchy = lineage(title)
    if not hierarchy:
        raise ValueError(f"Cannot compute address for title {title}")
    archive_title = hierarchy[-1].split("/")
    if len(hierarchy) > 1:
        tail = title.split("/")[1:]
        if tail and tail[0] == archive_title[-1]:
            tail.pop(0)
        tail.extend((3 - len(tail)) * [""])
    else:
        tail = 3 * [""]
    result = ( *ARCHIVE_BY_TITLE[hierarchy[-1]] , *tail)
    #_logger.info(f"page_address({title}) -> {result}")
    return result

def page_name(title):
    address = page_address(title)
    return f"{address[0]}-{address[1]}/{'/'.join(address[2:])}".rstrip("/")

def is_archive(title):
    return canonicalize_title(title) in ARCHIVE_BY_TITLE

def page_title_from_address(address):
    if isinstance(address, list):
        address = tuple(address)
    tail = "/".join(address[2:]).rstrip("/")
    if not tail:
        return ARCHIVE_BY_ADDRESS[address[:2]]
    return "/".join([archive_root(*address[:2]), tail])

# -------------------------------------------------------------------------------
# namespace id lookup (utility)

def _api_url(base=ARCHIVE_BASE):
    return f"{base}/w/api.php"

def lookup_namespace_id(name, base=ARCHIVE_BASE):
    params = {
        "action": "query",
        "format": "json",
        "meta": "siteinfo",
        "siprop": "namespaces|namespacealiases"
    }
    data = fetch_url(_api_url(base), params=params, json=True)
    data = data["query"]
    #_logger.info(data)
    target = name.lower()
    # Check official namespaces
    for ns_id, ns in data["namespaces"].items():
        if ns.get("canonical","").lower() == target or ns.get("*","").lower() == target:
            return int(ns_id)
    # Check aliases
    for alias in data.get("namespacealiases", []):
        if alias["alias"].lower() == target:
            return int(alias["id"])
    return None

# -------------------------------------------------------------------------------
# return list of all pages in given namespace with given prefix (or all if prefix is None)

def get_all_pages(namespace=WIKI_NAMESPACE_ID, prefix=None, limit=500):
    titles = []
    params = {
        "action": "query",
        "format": "json",
        "list": "allpages",
        "apnamespace": namespace,
        "aplimit": limit,
    }
    if prefix:
        params["apprefix"] = prefix.replace(f"{WIKI_NAMESPACE}:", "")

    cont = {}
    while True:
        if cont:
            params.update(cont)
        data = fetch_url(API_URL, params=params, json=True)
        titles.extend([p["title"] for p in data["query"]["allpages"]])
        if "continue" in data:
            cont = data["continue"]
            #time.sleep(5)
        else:
            break
    return titles

# -------------------------------------------------------------------------------
# subarchive sniffer

def sniff_subarchives(archive):
    url = f'{ARCHIVE_BASE}/wiki/{archive}'
    page = mw_read_page(url)
    result = {}
    for table in page["tables"]:
        for child in table["children"]:
            entry = child[0]
            #_logger.info(entry)
            if entry["exists"] and archive in entry["link"]:
                subarchive = entry["text"]
                if subarchive["uk"] != "видання":
                    _logger.info(f'found subarchive: {archive}-{subarchive["uk"]}')
                    result[subarchive["uk"]] = {
                        'title': form_text_item(entry["link"].replace("/wiki/","")),
                        'archive': form_text_item(archive),
                        'subarchive': subarchive,
                        'link': entry["link"],
                        }

    return result

def _comment_string():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"This file was generated by update_master_archive_list() on {timestamp}. Do not edit manually."

def update_master_archive_list():
    with open('resources/archives.json', encoding="utf8") as f:
        manifest = json.load(f)

    archives = {}
    for archive_name, archive in manifest["archives"].items():
        time.sleep(1)
        _logger.info(f"Searching {archive_name} ({archive})")
        archives[archive_name] = sniff_subarchives(archive)
        translate_structure(archives[archive_name])
        for sub, value in archives[archive_name].items():
            if sub == "Р":  # make sure Cyrillic Р maps to Latin R
                _logger.info("Mapping Cyrillic Р to Latin R")
                value["subarchive"]["en"] = "R"
            elif sub == "А": # make sure Cyrillic А maps to Latin A
                _logger.info("Mapping Cyrillic А to Latin A")
                value["subarchive"]["en"] = "A"

    for fond_name, fond_title in manifest["fonds"].items():
        fond_name = fond_name.split('-')
        if len(fond_name) == 1:
            fond_name.append('_')
        item = {
            "title": form_text_item(fond_title),
            "subarchive": form_text_item(fond_name[1])
        }
        translate_structure(item)
        if not archives.get(fond_name[0]):
            archives[fond_name[0]] = {}
        archives[fond_name[0]][fond_name[1]] = item

    _logger.info(f"generate_master_archive_list: updating {_ARCHIVE_MASTER_PATH}")
    with open(_ARCHIVE_MASTER_PATH, "w", encoding="utf8") as file:
        file.write(json.dumps({
            'comment':  _comment_string(),
            'archives': archives
            }, indent=4))

def all_archives():
    #_logger.info(ARCHIVES)
    return [
        [arc, sub['subarchive']['en'], sub['title']['uk']] 
        for arc, archive in ARCHIVES.items() 
        for sub in archive.values()]

def _select_subarchive(archive, subarchive):
    for key, value in archive.items():
        if subarchive is None or key == subarchive or value["subarchive"]["en"] == subarchive:
            return value
    raise ValueError("Unrecognized subarchive key")

def find_archive(archive_tag, subarchive=None):
    archive = ARCHIVES[archive_tag]
    sub = _select_subarchive(archive, subarchive)
    return { "title": sub["title"], "subarchive": sub["subarchive"] }

def batch_page_exists(titles, batch_size=50):
    """
    Check if a list of Wikimedia page titles exist using the MediaWiki API.

    Args:
        titles (list of str): Page titles to check.
        batch_size (int): Max titles per request (50 for normal users).

    Returns:
        dict: Mapping of title -> True (exists) or False (missing)
    """
    results = {}

    for i in range(0, len(titles), batch_size):
        batch = titles[i:i + batch_size]
        params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(batch)
        }

        try:
            response = fetch_url(API_URL, params=params, json=True, method="POST")
            pages = response.get("query", {}).get("pages", {})
            for page in pages.values():
                title = canonicalize_title(page.get("title"))
                results[title] = "missing" not in page
        except Exception as e:
            for title in batch:
                results[canonicalize_title(title)] = False
            _logger.error(f"Error checking titles {batch}: {e}")

    return results

def page_exists(title):
    return batch_page_exists([title])[title]

# -------------------------------------------------------------------------------
# WikiSource MediaWiki API page download

def get_title(url, include_namespace=True):
    """Extract title from a wiki URL. Optionally include or strip the namespace."""
    result = url.replace(ARCHIVE_BASE, '')
    result = result.replace('/wiki/', '')
    result = unquote(result)

    ns_prefix = f"{WIKI_NAMESPACE}:"

    if include_namespace:
        if not result.startswith(ns_prefix):
            result = ns_prefix + result
    else:
        if result.startswith(ns_prefix):
            result = result[len(ns_prefix):]

    return result

def _nonexistent_page(page_title):
    _logger.info(f"Nonexistent page: {page_title}")
    with open(_NONEXISTENT_PAGE_PATH, encoding="utf8") as f:
        page = json.load(f)
        page["title"]["uk"] = page_title.replace(f"{WIKI_NAMESPACE}:", "")
        page["link"] = f"{ARCHIVE_BASE}/wiki/{page_title}"
        return page

def _is_table(tag):
    return tag.tag == "table" and [entry for entry in tag.attributes if "wikitable" in entry] != []

def _check_page_existence_chunked(page_links, chunk_size=50):
    exists_map = {}
    title_map = {get_title(link): link for link in page_links}
    titles = list(title_map.keys())
    title_map = {key.replace(" ", "_").lower(): link for key, link in title_map.items()}

    for i in range(0, len(titles), chunk_size):
        title_batch = "|".join(titles[i:i+chunk_size])
        params = {
            'action': 'query',
            'prop': 'info',
            'titles': title_batch,
            'format': 'json'
        }
        data = fetch_url(API_URL, params=params, json=True, method="POST")
        for page_data in data['query']['pages'].values():
            title = page_data['title'].replace(" ", "_").lower()
            # If invalid or missing, mark as False
            exists = not ('missing' in page_data or 'invalid' in page_data)
            if title not in title_map:
                _logger.error(f"_check_page_existence_chunked: ignoring unrecognized title: {title}")
            else:
                exists_map[title_map[title]] = exists
    return exists_map

def _is_category_link(title):
    return title.startswith("Категорія:")

def _is_commons_url(title):
    return title.lower().startswith("c:")

def _map_commons_url(title):
    if title.lower().startswith("c:"):
        return f"https://commons.wikimedia.org/wiki/{title[2:].replace(' ', '_')}"
    return title

def _is_relative_link_target(link_target):
    return re.match(r'^(\.\./|\.\/|/)', link_target) is not None

def _is_familysearch_url(link):
    return link.startswith("https://www.familysearch.org")

def expand_link_target(link_target, page_title):
    # remove leading and trailing spaces
    link_target = link_target.strip()

    # check for absolute target reference
    if not link_target.startswith(('.', '/')):
        return f"{ARCHIVE_BASE}/wiki/{link_target.replace(' ', '_')}"

    # collapse multiple slashes to single slashes
    link_target = re.sub(r'//+', '/', link_target)

    # split the page_title into components
    base_parts = page_title.strip("/").split("/")
    target_parts = link_target.strip("/").split("/")

    # get rid of stray spaces in path components
    target_parts = [part.strip() for part in target_parts]

    # build absolute path from relative path spec
    resolved_parts = []
    for part in target_parts:
        if part == "..":
            if base_parts:
                base_parts.pop()
        elif part in [".", ""]:
            continue
        else:
            resolved_parts.append(part)

    final_parts = base_parts + resolved_parts
    full_path = "/".join(final_parts).replace(" ", "_")

    return f"{ARCHIVE_BASE}/wiki/{full_path}"

def _split_list(lst, *predicates):
    buckets = [[] for _ in range(len(predicates) + 1)]  # One extra for "rest"
    for item in lst:
        for i, pred in enumerate(predicates):
            if pred(item):
                buckets[i].append(item)
                break
        else:
            buckets[-1].append(item)  # No predicate matched
    return tuple(buckets)

def _safe_remove(lst, item):
    try:
        lst.remove(item)
    except ValueError:
        pass

def _subtract_links(links, delta):
    for key, link_list in delta.items():
        if key in links.keys():
            for delta_link in link_list:
                _safe_remove(links[key], delta_link)

# Detect bare File:... or c:File:... patterns in template params
_doc_file_pattern = re.compile(r'((?:c:)?File:[^<>\[\]\n]+)', re.IGNORECASE)

def _extract_doc_links_from_template(template):
    """
    Scan all parameters of a template for things that look like document links.

    Returns a dict in the same "shape" as _extract_links:
        {
            "commons_links": [...],
            "external_links": [...],
        }
    but only including keys that actually have values.
    """
    doc_links = {
        "commons_links": [],
        "external_links": [],
    }

    for param in template.params:
        value = param.value
        value_text = str(value).strip()
        if not value_text:
            continue

        # 1) Use the normal link extractor on the parameter value
        param_links = _extract_links(value)

        # Commons-style links (e.g. c:File:..., or already-URL-mapped)
        for link in param_links.get("commons_links", []):
            if _included_link(link):
                mapped = _map_commons_url(link)
                if mapped not in doc_links["commons_links"]:
                    doc_links["commons_links"].append(mapped)

        # External links (e.g. https://commons.wikimedia.org/..., pdfs, etc.)
        for link in param_links.get("external_links", []):
            if _included_link(link) and link not in doc_links["external_links"]:
                doc_links["external_links"].append(link)

        # 2) Catch bare File:... text with no [[...]] or [http...] wrapper
        for m in _doc_file_pattern.finditer(value_text):
            file_title = m.group(1).strip()

            if file_title.lower().startswith("c:"):
                mapped = _map_commons_url(file_title)
                if mapped not in doc_links["commons_links"]:
                    doc_links["commons_links"].append(mapped)
            else:
                # Assume bare File:... refers to a Commons file by default
                file_title_norm = file_title.replace(" ", "_")
                url = f"https://commons.wikimedia.org/wiki/{file_title_norm}"
                if url not in doc_links["commons_links"]:
                    doc_links["commons_links"].append(url)

    # Strip empty keys so _subtract_links works cleanly
    return {k: v for k, v in doc_links.items() if v}

def _extract_links(wikitext):
    # parse if necessary
    if not isinstance(wikitext, mwparserfromhell.wikicode.Wikicode):
        wikitext = mwparserfromhell.parse(str(wikitext))

    # Internal wiki link targets
    links = [str(link.title).strip() for link in wikitext.filter_wikilinks()]
    commons_links, category_links, int_links = _split_list(links, _is_commons_url, _is_category_link)
    commons_links = [_map_commons_url(title) for title in commons_links]

    # External link URLs
    ext_links = [str(link.url).strip() for link in wikitext.filter_external_links()]

    return {
        "commons_links": commons_links,
        "category_links": category_links,
        "internal_links": int_links,
        "external_links": ext_links,
    }

def _read_wiki_text(page_title, oldid=None):
    params = {
        'action': 'parse',
        'prop': 'wikitext|revid',
        'format': 'json'
    }
    if oldid:
        params['oldid'] = oldid
    elif page_title:
        params['page'] = page_title
    else:
        raise ValueError("Must provide either page_title or oldid")

    data = fetch_url(API_URL, params=params, json=True)

    if 'error' in data:
        raise RuntimeError(f"API error: {data['error']}")

    return (
        data['parse']['wikitext']['*'],
        data['parse']['revid'],
        data['parse']['title'].replace(f'{WIKI_NAMESPACE}:', ''),
    )

_colspan_re = re.compile(r'\bcolspan\s*=\s*["\']?(\d+)["\']?', flags=re.IGNORECASE)

def _extract_colspan(text):
    """
    Extracts colspan=N from text.

    Returns:
        (n, cleaned_text):
            n (int): colspan value (0 if absent),
            cleaned_text (str): text with the colspan directive removed
    """
    match = _colspan_re.search(text)
    if not match:
        return 0
    return int(match.group(1))

def _expand_colspan(cells):
    expanded = []
    for cell in cells:
        expanded.append(cell.get("text", ""))
        colspan = cell.get("colspan", 0)
        if colspan > 1:
            expanded.extend([""] * (colspan - 1))
    return expanded

_table_cell_token_re = re.compile(r'''
    (\[\[[^\[\]]+?\]\])      |  # group 1: wikilink, non-greedy
    (\[https?:[^\[\]]+?\])   |  # group 2: external link (optional)
    (?<!\\)(\|)              |  # group 3: unescaped pipe
    ([^|\[\]\\]+|\\\|)          # group 4: text (including escaped pipe)
    ''', re.VERBOSE)

_table_cell_token_re = re.compile(r'''
    (?P<wikilink>\[\[[^\[\]]+?\]\])                  |  # [[wikilink]]
    (?P<externallink>\[https?:[^\[\]]+?\])           |  # [http://...]
    (?P<pipe>(?<!\\)\|)                              |  # unescaped |
    (?P<quoted_directive>\b\w+\s*=\s*"[^"]*")        |  # key="..."
    (?P<single_quoted_directive>\b\w+\s*=\s*'[^']*') |  # key='...'
    (?P<unquoted_directive>\b\w+\s*=\s*[^\s|]+)      |  # key=value (no quotes)
    (?P<text>[^\s|[\]\\]+(?:\s+[^\s|[\]\\]+)*)       |  # text chunks, avoiding pipes and brackets
    (?P<whitespace>\s+)                                 # separate whitespace
    ''', re.VERBOSE)

def _tokenize_wikitext_table_cell(text):
    text = text.strip()
    colspan = 0
    result_text = ""
    after_pipe = True

    for match in _table_cell_token_re.finditer(text):
        token_type = match.lastgroup
        value = match.group(token_type)

        if token_type in ["quoted_directive", "single_quoted_directive", "unquoted_directive"]:
            after_pipe = False # everything before pipe is a directive
            if "colspan" in value:
                colspan = _extract_colspan(value)
        elif token_type == "pipe":
            after_pipe = True
            result_text = ""
        elif token_type == "text" and after_pipe:
            result_text += value
        elif token_type == "wikilink" and after_pipe:
            result_text += value
        elif token_type == "whitespace" and after_pipe:
            result_text += " "

    return result_text.strip(), colspan

_table_line_token_re = re.compile(r'''
    (?P<wikilink>\[\[.*?\]\])       |  # [[wikilink]]
    (?P<sep_double>\|\||\!\!)       |  # double pipe || or double bang !!
    (?P<sep_single>\||\!)           |  # single pipe | or single bang !
    (?P<text>[^|\[\]!]+)               # everything else (non-token text)
    ''', re.VERBOSE)

def _tokenize_wikitext_table_line(text):

    # tokenize the table text line
    text = text.strip()
    cells = [m.group(0) for m in _table_line_token_re.finditer(text)]
    def _format_cell(cell_text):
        cell_text, colspan = _tokenize_wikitext_table_cell(cell_text)
        return { "text": cell_text, "colspan": colspan }

    # group cells between separators
    result = []
    current_cell = ""
    for cell in cells:
        if cell not in ["!!", "||"]:
            current_cell += cell
        else:
            result.append(_format_cell(current_cell))
            current_cell = ""
    if current_cell:
        result.append(_format_cell(current_cell))
    result = _expand_colspan(result)

    return result

def _parse_wikitext_table_lines(wikitext):
    """
    Parse stripped Wikitext table content line-by-line.

    :param wikitext: A string containing the contents of a table, minus the outer {| and |}
    :return: List of rows, each row is a list of cells
    """
    rows = []
    current_row = []
    is_header = False

    lines = wikitext.strip().splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith('|-'):
            if current_row:
                rows.append(current_row)
                current_row = []
            is_header = False  # reset after row break
            continue

        if line.startswith('|+'):
            continue  # ignore caption lines

        if line.startswith('!'):
            if not is_header and current_row:
                rows.append(current_row)
                current_row = []
            is_header = True
            line_content = line[1:]

            # Use both !! and || as possible header separators
            cells = _tokenize_wikitext_table_line(line_content)
            current_row.extend(cells)
            continue

        if line.startswith('|'):
            if is_header:
                # terminate header
                rows.append(current_row)
                current_row = []
                is_header = False
            line_content = line[1:]
            cells = _tokenize_wikitext_table_line(line_content)
            current_row.extend(cells)
            continue

    if current_row:
        rows.append(current_row)

    return rows

def _parse_wikitext_table(text):

    #with open("table_code.txt", "w") as file:
    #    file.write(str(text))

    # Split rows using the row separator "|-", allowing optional leading/trailing whitespace
    rows = re.split(r"\s*\|[-—]\s*", str(text))
    rows = [row.strip("\n ") for row in rows]
    rows = [row for row in rows if row] # get rid of empty rows

    header = []
    body = _parse_wikitext_table_lines(text)
    if body:
        # take the first row as the header
        header = body.pop(0)

    return header, body

def _included_link(link):
    return not any([
        _is_relative_link_target(link),
        _is_familysearch_url(link),
        re.search(r".(png|jpg)$", link, re.IGNORECASE),
        ])

def mw_page_doc_url(page):
    links = page["other_links"].get("internal_links", [])
    links = [link for link in links if _included_link(link)]
    if links:
        return expand_link_target(links[0], page["title"]["uk"])
    links = page["notes"].get("commons_links", [])
    links = [link for link in links if _included_link(link)]
    if links:
        return links[0]
    links = page["other_links"].get("external_links", [])
    links = [link for link in links if _included_link(link)]
    if links:
        return links[0]
    return None


def _extract_table(table_code, page_title, page_links, all_page_links):
    header, rows = _parse_wikitext_table(table_code)

    # format header
    header = [form_text_item(cell.strip()) for cell in header]

    # process rows
    children = []
    for cells in rows:
        row_data = []
        for cell_text in cells:
            #_logger.info(f"cell: {cell_text}")
            cell_wikicode = mwparserfromhell.parse(cell_text)
            # Extract internal links
            links = cell_wikicode.filter_wikilinks()
            link = None
            if links:
                link_target = str(links[0].title).strip()
                _safe_remove(page_links["internal_links"], link_target)
                if not link_target.startswith("#"):
                    link = expand_link_target(link_target, page_title)
                    all_page_links.add(link)
            else:
                # External links as fallback
                ext_links = cell_wikicode.filter_external_links()
                if ext_links:
                    link = str(ext_links[0].url).strip()

            # Clean text (strip wikitext markup)
            text = cell_wikicode.strip_code().strip('./ ')
            row_data.append({'text': form_text_item(text), 'link': link})
        children.append(row_data)

    return {
        "header": header,
        "children": children
    }

def _check_table_link_existence(tables, all_page_links):
    # Collect unique linked page titles (relative titles like '/1/' etc.)
    link_existence = _check_page_existence_chunked(all_page_links)
    for table in tables:
        for row in table["children"]:
            for cell in row:
                if cell['link']:
                    cell['exists'] = link_existence.get(cell["link"], True)  # True by default
                    # ARCHIVE_BASE is implicit for child links that are within the media wiki
                    cell["link"] = cell["link"].replace(ARCHIVE_BASE, "")

def _normalize_child_link_positions(tables):
    for table in tables:
        for child in table.get("children", []):
            if len(child) > 1 and not child[0].get("link"):
                for pos in range(1, len(child)):
                    link = child[pos].get("link")
                    if link and link.startswith("/wiki/"):
                        child[0]["link"] = link
                        child[0]["exists"] = child[pos].get("exists", False)
                        del child[pos]["link"]
                        del child[pos]["exists"]
                        break

def _parse_wiki_text(wikitext, page_title, title, revid=None):
    """
    Core parser that turns wikitext into a `page` dict.

    - `page_title`: canonical title used for resolving relative links (e.g. 'Архів:...').
    - `title`: display title (usually without namespace, or whatever you want to show).
    - `revid`: optional, stored in the page dict if provided.
    """
    wikicode = mwparserfromhell.parse(wikitext)

    # get and organize all the links on the page
    page_links = _extract_links(wikicode)

    # Title and description
    desc = None
    dates = None
    notes = {}
    template_name = None

    for template in wikicode.filter_templates():
        if template.name.startswith("Архіви") or template.name.startswith("заголовок"):
            template_name = template.name.strip(' \n')

            if template.has("назва"):
                desc = template.get("назва").value.strip_code().strip(" ./\n")

            if template.has("секція") and not desc:
                desc = template.get("секція").value.strip_code().strip()

            if template.has("рік"):
                dates = template.get("рік").value.strip_code().strip()

            # 1) Explicit примітки field, as before
            if template.has("примітки"):
                prim_notes = _extract_links(template.get("примітки"))
                # Merge into notes
                for key, vals in prim_notes.items():
                    if not vals:
                        continue
                    notes.setdefault(key, [])
                    for v in vals:
                        if v not in notes[key]:
                            notes[key].append(v)
                _subtract_links(page_links, prim_notes)

            # 2) Generalized document links from all parameters
            doc_notes = _extract_doc_links_from_template(template)
            if doc_notes:
                for key, vals in doc_notes.items():
                    notes.setdefault(key, [])
                    for v in vals:
                        if v not in notes[key]:
                            notes[key].append(v)
                _subtract_links(page_links, doc_notes)

            break

    page = {
        "title": form_text_item(title),
        "template": form_text_item(template_name),
        "revid": revid,
        "description": form_text_item(desc),
        "dates": form_text_item(dates),
        "notes": notes,
        "other_links": page_links,
        # We'll fill "tables", "link", "doc_link" later
    }

    # Table extraction
    wiki_tables = [t for t in wikicode.filter_tags() if _is_table(t)]
    all_page_links = set()
    tables = [
        _extract_table(table.contents, page_title, page_links, all_page_links)
        for table in wiki_tables
    ]
    for i, table in enumerate(tables):
        table["name"] = f"Table {i+1}"

    if not tables:
        # try to populate a "table" if there is either a list of subpages or commons links
        children = []
        sub_pages = [link for link in page_links["internal_links"] if link.startswith("/")]
        if sub_pages:
            # synthesize a table from list of links to subpages
            for link_target in sub_pages:
                link = expand_link_target(link_target, page_title)
                all_page_links.add(link)
                _safe_remove(page_links["internal_links"], link_target)
                text = form_text_item(link_target.strip("./ "))
                children.append([{'text': text, 'link': link}])
        else:
            sub_pages = list(page_links["commons_links"])
            if len(sub_pages) > 1:
                # synthesize a table from list of links commons files
                for link in sub_pages:
                    _safe_remove(page_links["commons_links"], link)
                    text = link.replace("https://commons.wikimedia.org/wiki/", "")
                    text = text.replace("File:", "")
                    text = text.replace("_", " ")
                    text = form_text_item(text)
                    children.append([{'text': text, 'link': link, 'exists': True}])
        if children:
            tables.append({
                "name": "Linked Pages",
                "header": [form_text_item("Linked Pages")],
                "children": children,
            })

    # determine if linked items in tables are to existing pages
    _check_table_link_existence(tables, all_page_links)

    _normalize_child_link_positions(tables)

    page["tables"] = tables

    # Fill basic link and doc_link (but not lastmod — that's not wikitext parsing)
    page["link"] = f"{ARCHIVE_BASE}/wiki/{page_title}"
    doc_url = mw_page_doc_url(page)
    page["doc_link"] = doc_url if doc_url is not None else ""

    return page

def mw_read_page(page_title, oldid=None):
    # extract title from url if necessary
    page_title = get_title(page_title)
    # _logger.info(f"mw_read_page: {page_title}")

    # get the wikitext and parse
    try:
        wikitext, revid, title = _read_wiki_text(page_title, oldid)
    except RuntimeError as e:
        # unable to read page - test for existence
        if page_exists(page_title):
            # page exists - raise the exception
            raise e
        # nonexistent page - return placeholder
        return _nonexistent_page(page_title)

    # shared parsing logic
    page = _parse_wiki_text(wikitext, page_title=page_title, title=title, revid=revid)

    # Last modified date via API `revisions` (for this oldid)
    params_rev = {
        'action': 'query',
        'prop': 'revisions',
        'revids': revid,
        'rvprop': 'timestamp',
        'format': 'json',
    }
    rev_data = fetch_url(API_URL, params=params_rev, json=True)

    pages = rev_data['query']['pages']
    page_id = next(iter(pages))
    page["lastmod"] = from_utc_format(pages[page_id]['revisions'][0]['timestamp'])

    return page

# -------------------------------------------------------------------------------
# WikiSource change detection

def do_search(query_string, limit=10, offset=0):
    """
    Search archive site for matching entries, sorted on last modification date.
    For each hit, return dict with item with keys: title, link, and lastmod.
    """
    _logger.info(f'do_search({query_string}, limit={limit}, offset={offset})')
    query_string = quote(query_string, safe='', encoding=None, errors=None)
    url = f'{ARCHIVE_BASE}/w/index.php?limit={limit}&offset={offset}'
    url += f'&ns0=1&sort=last_edit_desc&search={query_string}'
    #_logger.info(f'search url={url}')
    soup = BeautifulSoup(fetch_url(url), 'lxml')
    results = []
    for result in soup.find_all('li', attrs = {'class': 'mw-search-result'}):
        div = result.find('div', attrs = {'class': 'mw-search-result-heading'})
        data = result.find('div', attrs = {'class': 'mw-search-result-data'})
        data = data.text.strip()
        pos = data.find('-')
        data = format_date(data[(pos + 1):].strip())
        item = {
            'title': div.a['title'],
            'link': div.a['href'],
            'lastmod': data
        }
        results.append(item)
    return results

def report_page_changes(page):
    """
    Print a report of changes detected in check_page_changes().
    """
    if not isinstance(page, dict):
        page = page.page
    if 'refmod' not in page:
        _logger.info("No changes to report. Run check_page_changes first.")
        return
    _logger.info(
        f'Change report for {get_text(page["title"])},' +
        f' lastmod={page["lastmod"]}, refmod={page["refmod"]}')
    for key in ['title', 'description']:
        if page[key]['edit'] is not None:
            _logger.info(f'{key}: {page[key]["edit"]}')
    for table in page["tables"]:
        for child in table["children"]:
            index = get_text(child[0]['text'])
            for i, item in enumerate(child):
                if 'edit' in item and item['edit'] is not None:
                    _logger.info(f'{index}[{i}] ({item["edit"]}): {get_text(item["text"])}')
                if 'link_edit' in item and item['link_edit'] is not None:
                    _logger.info(f'{index}[{i}] (link {item["link_edit"]}): {item["link"]}')

def _check_table_changes(table, ref_table):
    ref_children = dict((c[0]['text']['uk'], c) for c in ref_table['children'])
    #_logger.info(f"_check_table_changes: {table['name']} vs {ref_table['name']}")
    for child in table['children']:
        index = child[0]['text']['uk']
        if index in ref_children:
            ref_child = ref_children[index]
            #_logger.info(f"comparing: {child} to {ref_child}")
            for item, ref_item in zip(child, ref_child):
                changed = item['text']['uk'] != ref_item['text']['uk']
                item['edit'] = 'changed' if changed else None
                if is_linked(item):
                    if is_linked(ref_item):
                        item['link_edit'] = 'changed' if item['link'] != ref_item['link'] else None
                    else:
                        item['link_edit'] = 'added'
        else:
            for item in child:
                item['edit'] = 'added'

def check_page_changes(page, reference, report=False):
    """
    Compare a given page to a prior version of the same page and return any detected changes.
    """
    if not isinstance(page, dict):
        page = page.page
    if not isinstance(reference, dict):
        reference = reference.page
    page['refmod'] = reference['lastmod']
    for key in ['title', 'description']:
        changed = not equal_text(page[key], reference[key])
        page[key]['edit'] = 'changed' if changed else None
    if 'doc_link' in page:
        if 'doc_link' in reference:
            if page['doc_link'] != reference['doc_link']:
                page['doc_link_edit'] = 'changed'
        else:
            page['doc_link_edit'] = 'added'

    for table in page["tables"]:
        found_match = False
        for ref_table in reference["tables"]:
            if table["name"] == ref_table["name"]:
                _check_table_changes(table, ref_table)
                found_match = True
                break
        if not found_match:
            for child in table['children']:
                for item in child:
                    item["edit"] = "added"

    if report:
        report_page_changes(page)

def _page_update_summary(archive, change_list):
    #assert isinstance(archive, Archive)
    archive_prefix = archive.url[:archive.url.rfind('/')]
    archive_prefix = archive_prefix.replace(ARCHIVE_BASE, '')
    archive_prefix = archive_prefix.replace('%3A', ':')
    # Form list of fonds belonging to this archive
    fond_list={get_text(c[0]['text']) for c in archive.children}
    result = {}
    for item in change_list:
        page_spec = item["title"].split('/')
        address = archive.address[:2]
        address += tuple(entry for entry in page_spec[1:])
        address = (address + ("",) * 3)[:5]
        fond = address[2]
        address = ','.join(address)
        mod_date = item["lastmod"]
        # Confirm that the item belongs to the selected archive
        if fond in fond_list and item["link"].startswith(archive_prefix):
            if address in result:
                result[address] = max(mod_date, result["address"])
            else:
                result[address] = mod_date
    return result

def check_page_updates(archive, cutoff_date):
    #assert isinstance(archive, Archive)
    change_list = []
    batch_size = 50
    offset = 0
    while True:
        _logger.info(f'check_page_updates: {archive.name}, {batch_size}, {offset}')
        changes = archive.latest_changes(limit=batch_size, offset=offset)
        change_list += changes
        if not changes or changes[-1]["lastmod"] < cutoff_date:
            break
        offset += batch_size
        batch_size *= 2 # search geometrically longer history
    change_list = [item for item in change_list if item["lastmod"] >= cutoff_date]
    _logger.info(f"check_page_updates, {len(change_list)}, changes found")
    return _page_update_summary(archive, change_list)

# -------------------------------------------------------------------------------
# Get most recent page modification dates within given namespace

def get_recent_changes(namespace=WIKI_NAMESPACE_ID, cutoff_date=None, limit=500, sleep_time=0.1, base=ARCHIVE_BASE):
    """
    Collects the latest modification timestamp for each page in a given namespace,
    going back to the specified cutoff date.

    Args:
        namespace (int): Namespace number (e.g. 0 = main, 6 = file, etc.)
        cutoff_date (str): timestamp
        limit (int): Max results per request (max is 500 for users, 5000 for bots)
        sleep_time (float): Seconds to sleep between requests to avoid throttling

    Returns:
        dict: Mapping from page title to latest modification timestamp (ISO8601)
    """
    if not cutoff_date:
        cutoff_date = "2025"

    latest_mods = {}
    params = {
        "action": "query",
        "format": "json",
        "list": "recentchanges",
        "rcnamespace": namespace,
        "rcprop": "title|timestamp|user",
        "rclimit": limit,
        "rcend": to_utc_format(cutoff_date),
        "rcdir": "older",  # go backward in time
        "rcshow": "!redirect",
    }

    seen_pages = set()
    cont = {}

    while True:
        if cont:
            params.update(cont)
        data = fetch_url(_api_url(base), params=params, json=True)
        for rc in data.get("query", {}).get("recentchanges", []):
            title = rc["title"]
            timestamp = rc["timestamp"]
            user = rc["user"]
            if title not in seen_pages:
                seen_pages.add(title)
                latest_mods[title] = { "timestamp": from_utc_format(timestamp), "user": user }

        if "continue" in data:
            cont = data["continue"]
            time.sleep(sleep_time)
        else:
            break

    return latest_mods

# -------------------------------------------------------------------------------
# Get most recent page modification dates within given namespace

def get_last_mod(titles, api_delay=0):
    """
    Return a dict of {page_title: last_content_modified_datetime} using 'prop=revisions'.
    This avoids inflated 'touched' timestamps from template updates, purges, or file usage.

    Input: str or list of str (page titles)
    Output: dict {title: datetime or None}
    """
    if isinstance(titles, str):
        titles = [titles]

    result = {}

    for title in titles:
        params = {
            "action": "query",
            "format": "json",
            "prop": "revisions",
            "rvlimit": 1,
            "rvprop": "timestamp",
            "titles": title,
        }

        response = fetch_url(API_URL, params=params, json=True)
        query = response.get("query")
        if query:
            title_mapping = {}
            normalized = query.get("normalized", {})
            for item in normalized:
                title_mapping[item["to"]] = item["from"]

            pages = query.get("pages", {})
            for page in pages.values():
                revs = page.get("revisions")
                actual_title = page.get("title")
                original_title = title_mapping.get(actual_title, actual_title)

                if revs:
                    result[original_title] = from_utc_format(revs[0]["timestamp"])
                else:
                    result[original_title] = None
        else:
            result[title] = None

        if api_delay > 0:
            time.sleep(api_delay)

    return result

# -------------------------------------------------------------------------------
# Page revision history handling (using wiki API)

def history_url(page_title, limit=1):
    return ('https://uk.wikisource.org/w/api.php?action=query&format=json'
            '&prop=revisions&rvprop=ids|timestamp'
            f'&rvlimit={limit}&titles={canonicalize_title(page_title)}')

def page_revision_url(page_title, revid):
    return ('https://uk.wikisource.org/w/index.php?'
            f'title={canonicalize_title(page_title)}&oldid={revid}')

def get_page_history(page_title, limit=10):
    result = fetch_url(history_url(page_title, limit=limit), json=True)
    query = result.get('query')
    #_logger.info(f'get_page_history({page_title}, limit={limit}): result={query}')

    if not query:
        _logger.error(f'get_page_history({page_title}, limit={limit}): no result returned')
        return []
    pages = query.get('pages')
    if not pages:
        _logger.error(f'get_page_history({page_title}, limit={limit}): empty result returned')
        return []
    if '-1' in pages:
        _logger.error(f'get_page_history({page_title}, limit={limit}): unrecognized page name')
        return []
    # assume only one page is returned (in future, pass multiple to reduce api calls)
    for page in pages.values():
        history = [ {
            'revid': rev['revid'],
            'modified': from_utc_format(rev['timestamp']),
            'link': page_revision_url(page_title, rev['revid'])
        } for rev in page.get('revisions') ]
        return history
    _logger.error(f'get_page_history({page_title}, limit={limit}): unexpected result returned')
    return []

def get_page_history_from_cutoff(page_title, cutoff_date):
    # search increasingly for cutoff date  because
    # api does not allow for paging through search results
    last_result_length = 0
    attempt = 50
    while True:
        result = get_page_history(page_title, limit=attempt)
        if not result:
            _logger.error(f'get_page_history({page_title}, cutoff_date={cutoff_date}): empty history')
            return []
        if len(result) == last_result_length:
            result[-1]['created'] = True
            return result # no more history to be had
        if result[-1]['modified'] <= cutoff_date:
            for index, item in enumerate(result):
                if item['modified'] <= cutoff_date:
                    return result[:(index+1)]
            return result
        # increase limit length and try again
        last_result_length = len(result)
        attempt *= 2


# -------------------------------------------------------------------------------
# History LRU

class HistoryLRU:
    def __init__(self, maxsize=100, reset_limit=5 * 60):
        self._reset_limit = reset_limit  # seconds
        self._timer_start = time.time()
        self._lru = LRUCache(maxsize=maxsize)

    def _flush_if_needed(self):
        if time.time() - self._timer_start >= self._reset_limit:
            #_logger.info("HistoryLRU: flushing all entries")
            self._lru.clear()
            self._timer_start = time.time()

    def _filter_with_fallback(self, history, cutoff_date):
        split = next((i for i, h in enumerate(history) if h['modified'] <= cutoff_date), len(history))
        return history[:split + 1]

    def lookup(self, page_title, limit=10):
        self._flush_if_needed()
        try:
            history = self._lru[page_title]
            #_logger.info(f"HistoryLRU.lookup({page_title}): cache hit")
            if len(history) >= limit:
                return history[:limit]
            #_logger.info(f"HistoryLRU.lookup({page_title}): cache too short, refreshing")
        except KeyError:
            pass
            #_logger.info(f"HistoryLRU.lookup({page_title}): cache miss")
        # Refresh
        history = get_page_history(page_title, limit=limit)
        self._lru[page_title] = history
        return history[:limit]

    def lookup_by_cutoff(self, page_title, cutoff_date):
        self._flush_if_needed()
        try:
            history = self._lru[page_title]
            #_logger.info(f"HistoryLRU.lookup_by_cutoff({page_title}): cache hit")

            if history:
                oldest = history[-1]
                if oldest.get('created') or oldest['modified'] < cutoff_date:
                    # We have enough
                    return self._filter_with_fallback(history, cutoff_date)
                #_logger.info(f"HistoryLRU.lookup_by_cutoff({page_title}): cache incomplete, refreshing")
        except KeyError:
            pass
            #_logger.info(f"HistoryLRU.lookup_by_cutoff({page_title}): cache miss")

        # Refresh and filter
        history = get_page_history_from_cutoff(page_title, cutoff_date=cutoff_date)
        self._lru[page_title] = history
        return self._filter_with_fallback(history, cutoff_date)

# -------------------------------------------------------------------------------
# Document link extraction from wikitext

def _wiki_content_url(titles):
    batch_titles = '|'.join([quote(t) for t in titles])
    return (f'{ARCHIVE_BASE}/w/api.php?'
            'action=query&format=json&prop=revisions&'
            'rvprop=content&rvslots=main&'
            f'titles={batch_titles}'
           )

def _normalize_mediawiki_title(title):
    title = title.replace(' ', '_')         # Normalize space to underscore
    return title

def _file_link_to_url(link):
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.lower().startswith("file:"):
        filename = _normalize_mediawiki_title(link[5:])
        return f"/wiki/File:{filename}"
    return None

def _deduplicate_links(links):
    return list(dict.fromkeys(links))

def _chunked(iterable, size):
    """Yield successive chunks from iterable."""
    it = iter(iterable)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            break
        yield chunk

def _collect_doc_links_from_page(page):
    """
    Given a parsed `page` dict (as returned by _parse_wiki_text),
    collect all plausible document links.

    Returns a list of URLs or titles depending on what _parse_wiki_text produced.
    """
    links = []

    # 1) Primary doc_link if present
    doc_link = page.get("doc_link")
    if doc_link:
        links.append(doc_link)

    # 2) Any commons_links from notes
    notes = page.get("notes") or {}
    links.extend(notes.get("commons_links", []))

    # 3) External links that are included
    other_links = page.get("other_links") or {}
    for link in other_links.get("external_links", []):
        if _included_link(link):
            links.append(link)

    # Deduplicate
    return _deduplicate_links(links)

def batch_fetch_document_links(titles, map_to_url=True, chunk_size=20):
    if not isinstance(titles, (list, tuple)):
        titles = [titles]
    titles = [canonicalize_title(title) for title in titles]
    result = {}

    #_logger.info(f"batch_fetch_document_links: {titles}")
    for chunk in _chunked(titles, chunk_size):
        data = fetch_url(_wiki_content_url(chunk), json=True)
        if 'query' not in data:
            _logger.error(f'batch_fetch_document_links returned:\n    {data}')
            continue

        for page in data['query']['pages'].values():
            title = page['title']
            try:
                wikitext = page['revisions'][0]['slots']['main']['*']

                # Reuse shared parser.
                # We don't have/need revid here, so pass None.
                parsed = _parse_wiki_text(
                    wikitext=wikitext,
                    page_title=title,   # good enough for relative links
                    title=title,
                    revid=None,
                )

                links = _collect_doc_links_from_page(parsed)
                if map_to_url:
                    # Make _file_link_to_url a no-op for already-URLs:
                    links = [_file_link_to_url(link) for link in links]

                result[title] = _deduplicate_links(links)

            except (KeyError, IndexError):
                result[title] = []

    return result


# -------------------------------------------------------------------------------
# Document thumbnail download

def _api_and_default_ns_for_host(host: str) -> tuple[str, str]:
    """Return (api_url, default_namespace_prefix) for a given wiki host."""
    if _is_commons_host(host):
        return "https://commons.wikimedia.org/w/api.php", "File:"
    # Any language wikisource subdomain (e.g., uk.wikisource.org)
    if host.endswith(".wikisource.org"):
        return f"https://{host}/w/api.php", "Файл:"  # Ukrainian default
    # Generic MediaWiki fallback (rare)
    return f"https://{host}/w/api.php", "File:"

def _is_commons_host(host: str) -> bool:
    return host == "commons.wikimedia.org"

def _query_imageinfo(api_url: str, title: str, width: int, page: int) -> dict:
    params = {
        "action": "query",
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": str(width),
        "redirects": "1",
        "format": "json",
        "titles": title,
        "origin": "*",
        "iiurlparam": f"page={int(page) if page and page > 0 else 1}",
    }
    return fetch_url(api_url, params=params, json=True)

def _first_imageinfo(api_json: dict):
    pages = api_json.get("query", {}).get("pages", {})
    if not pages:
        return None
    page_block = next(iter(pages.values()))
    return page_block.get("imageinfo") or None

def _strip_ns(full_title: str) -> str:
    if full_title.startswith("Файл:"):
        return full_title[len("Файл:"):]
    if full_title.startswith("File:"):
        return full_title[len("File:"):]
    return full_title

def _special_filepath_url(host: str, title_no_ns: str, width: int, page: int) -> str:
    # Build Special:FilePath on the same host if possible; for Commons links this is ideal.
    # Works on Commons and most Wikimedia projects.
    base = f"https://{host}/wiki/Special:FilePath/{quote(title_no_ns)}?width={width}"
    if page and page > 1:
        base += f"&page={int(page)}"
    return base

def _safe_base_from_title(title: str, page: int, width: int) -> str:
    base = title.replace("Файл:", "").replace("File:", "").replace("/", "_")
    if page and page >= 1:
        base = f"{base}.p{page}"
    base = f"{base}.w{width}"
    # keep filename plain printable
    return "".join(c if c.isprintable() else "_" for c in base)

def _ext_from_url(url: str) -> str:
    import os as _os
    path = urlparse(url).path
    fname = _os.path.basename(path)
    root, ext = _os.path.splitext(fname)
    ext = ext.lower()
    if ext and 2 <= len(ext) <= 5 and ext[1:].isalnum():
        return _normalize_ext(ext)
    return ""

def _normalize_ext(ext: str) -> str:
    mapping = {".jpe": ".jpg", ".jpeg": ".jpg", ".tiff": ".tif", ".svgz": ".svg", ".htm": ".html"}
    ext = (ext or "").lower()
    return mapping.get(ext, ext)

def download_thumbnail(
    file_page_url: str,
    size: str | int = "medium",
    page: int = 1,
    out_dir: str = ".",
    use_special_filepath_fallback: bool = True,
) -> dict:
    width = {"small": 240, "medium": 640}.get(str(size).lower(), 640) if not isinstance(size, int) \
            else max(32, min(4096, size))

    parsed = urlparse(file_page_url)
    host = parsed.netloc.lower()
    path = unquote(parsed.path)

    if "/wiki/" not in path:
        raise RuntimeError("This does not look like a /wiki/ URL.")

    # Title from the URL
    title = path.split("/wiki/", 1)[1]

    # Decide default namespace and API endpoint from host
    api_url, default_ns = _api_and_default_ns_for_host(host)

    # Ensure title has a namespace (use site-appropriate default)
    if not (title.startswith("Файл:") or title.startswith("File:")):
        title = default_ns + title

    # Query imageinfo on the site from the URL first
    data = _query_imageinfo(api_url, title, width, page)

    # If no imageinfo and we are NOT on Commons, retry on Commons API (some files live there)
    infos = _first_imageinfo(data)
    if not infos and not _is_commons_host(host):
        commons_api, commons_ns = _api_and_default_ns_for_host("commons.wikimedia.org")
        commons_title = title
        # ensure English File: for Commons
        if commons_title.startswith("Файл:"):
            commons_title = "File:" + commons_title[len("Файл:"):]
        data = _query_imageinfo(commons_api, commons_title, width, page)
        title = commons_title  # track the title actually used
        api_url = commons_api   # and API used
        infos = _first_imageinfo(data)

    if not infos:
        # Still nothing—surface a helpful error
        raise RuntimeError(f"No imageinfo found. Response: {json.dumps(data, ensure_ascii=False)[:800]}")

    info = infos[0]
    thumb_url = info.get("thumburl") or info.get("url")
    orig_url = info.get("url")
    api_mime = info.get("mime") or ""

    if not thumb_url:
        raise RuntimeError("Thumbnail URL not available for this file.")

    # Try the thumbnail URL first
    try:
        content = fetch_url(thumb_url, content=True)
    except Exception as e:
        # Optional fallback via Special:FilePath (often works through CDN edge cases)
        if not use_special_filepath_fallback:
            raise
        title_no_ns = _strip_ns(title)
        fallback = _special_filepath_url(host, title_no_ns, width, page)
        content = fetch_url(fallback, content=True)
        thumb_url = fallback  # record what actually worked

    # Choose extension: URL path ext → API MIME guess → default .jpg
    ext = _ext_from_url(thumb_url)
    if not ext:
        ext = _normalize_ext(mimetypes.guess_extension(api_mime) or "")
    if not ext:
        ext = ".jpg"

    # Save
    os.makedirs(out_dir, exist_ok=True)
    base = _safe_base_from_title(title, page, width)
    saved_path = os.path.join(out_dir, base + ext)
    with open(saved_path, "wb") as f:
        f.write(content)

    return {
        "saved_path": saved_path,
        "thumb_url": thumb_url,
        "orig_url": orig_url,
        "mime": api_mime,
        "width": width,
        "api_response": data,
        "api_used": api_url,
    }
