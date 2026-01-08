# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

#from birddog.database import Database
#from birddog.runtime import Runtime
from birddog.wiki import (
    WIKI_NAMESPACE,
    _expand_link_target,
    )
from birddog.core import Page
from birddog.utility import fetch_url

from birddog.log import get_logger
_logger = get_logger()

# ----------------------------------------------------------------------
# Helpers

def _format_date(date):
    d = date.split(",")
    return f"{d[0]}-{d[1]}-{d[2]} {d[3]}:00+00:00"

def _normalize_date_string(s):
    return str(datetime.fromisoformat(s.replace("Z", "+00:00")))

def _page_title(page):
    return page.page["title"]["uk"]

def _availability(cell):
    return "linked" if cell.get("exists", False) else "redlinked"

_DOC_LINK_BLOCKLIST = [
    "FSMosaicTreeLogo",
    "familysearch.org",
]

def _allowed_doc_link(link):
    return all([item not in link for item in _DOC_LINK_BLOCKLIST])

def _replace_links(db, table_name, link_field, source_record, target_records):
    existing_targets = db.get_links(table_name, link_field, source_record)
    if set(existing_targets) == set(target_records):
        # no change
        return False
    db.delete_links(table_name, link_field, source_record, existing_targets)
    db.create_links(table_name, link_field, source_record, target_records)
    return True

# ----------------------------------------------------------------------
# Pages table record updates

def _form_page_record(page):
    page_data = page.page
    try:
        result = { "title": _page_title(page) }
        result["description_uk"] = page_data["description"]["uk"]
        desc = page_data["description"].get("en")
        if desc:
            result["description"] = desc
        result["availability"] = "linked"
        result["level"] = page.kind
        result["reference_date"] = _format_date(page_data["lastmod"])
        result["label"] = page.display_name if page.kind == "archive" else page.id
        dates = page_data.get("dates")
        if dates:
            result["years"] = dates["uk"]
        result["source_type"] = "wiki"
        
        return result
    except Exception as e:
        _logger.error(f"error in _form_page_record: {page.title}")
        raise e

def _child_titles(page):
    result = []
    prefix = f"/wiki/{WIKI_NAMESPACE}:"
    for child in page.children:
        if child:
            for cell in child:
                link = cell.get("link")
                if link and link.startswith(prefix):
                    result.append({
                        "title": link.replace(prefix, ""),
                        "availability": _availability(cell),
                    })
                    break
    # remove duplicate records
    result_dict = { rec.get("title"): rec for rec in result }
    return list(result_dict.values())

def _detect_changes(db, table_name, records, key="title"):
    if isinstance(records, dict):
        records = [ records ]
        singleton = True
    else:
        singleton = False
    id_map = db.lookup(table_name, {record[key] for record in records})
    current_records = db.read(table_name, list(id_map.values()))
    current_record_dict = {
        rec["Id"]: rec 
        for rec in db.read(table_name, list(id_map.values()))
        }
    update = []
    for record in records:
        rec_id = id_map.get(record[key])
        if rec_id:
            current_rec = current_record_dict[rec_id]
            for k, v in record.items():
                if not k in current_rec or current_rec[k] != v:
                    update.append(record)
                    break
        else:
            update.append(record)
    for record in update:
        record["birddog_alert"] = True
    if singleton:
        return update[0] if update else None
    return update

def _link_children(db, page):
    parent_title = _page_title(page)
    child_records = _child_titles(page)

    # locate the parent record
    parent_id = db.lookup("Pages", parent_title)
    if not parent_id:
        raise ValueError(f"cannot find record for parent (title={parent_title})")

    # ensure all child records exist
    child_ids = db.lookup("Pages", [child["title"] for child in child_records])
    new_children = []
    for child_id, child_rec in zip(child_ids, child_records):
        if child_id is None:
            new_children.append(child_rec)
        else:
            child_rec["Id"] = child_id

    if new_children:
        # some child titles were not found: create records for them and 
        # update the child id list to include the new titles
        new_child_ids = db.write("Pages", new_children)
        for child_id, child_rec in zip(new_child_ids, new_children):
            child_rec["Id"] = child_id
        child_ids = [child_rec["Id"] for child_rec in child_records]

    # patch the child links (replacing existing), returns True if there was a change
    if _replace_links(db, "Pages", "children", parent_id, child_ids):
        _logger.info(f"Child links for {parent_title} updated ({len(child_ids)} children)")
        return True
    # otherwise, no change in links
    return False

def _link_documents(db, page, urls):
    # locate the parent record
    page_id = db.lookup("Pages", _page_title(page))
    if not page_id:
        raise ValueError(f"_link_documents: cannot find record for parent (title={_page_title(page)})")

    doc_ids = db.lookup("Documents", urls)
    if not all(doc_ids):
        missing_urls = [url for url, did in zip(urls, doc_ids) if not did]
        for url in missing_urls:
            _logger.info(f"doc url missing: {url}")
        raise ValueError(f"_link_documents: unable to locate all referenced documents")

    if _replace_links(db, "Pages", "doc_links", page_id, doc_ids):
        _logger.info(f"Doc links for {_page_title(page)} updated ({len(doc_ids)} doc(s))")
        return True
    # otherwise, no change in links
    return False

def _update_pages(db, pages):
    if not isinstance(pages, (list, tuple)):
        if not isinstance(pages, Page):
            raise ValueError("must be list/tuple of Page objects or singleton Page object")
        singleton = True
        pages = [ pages ]
    else:
        singleton = False
    page_records = [_form_page_record(page) for page in pages]
    update = _detect_changes(db, "Pages", page_records)
    if update:
        result = db.write("Pages", update)
        if singleton:
            return result[0]
        return result
    return None

def _extract_page_links(page, strict=True):
    result = set()
    page_data = page.page
    for key in ["notes", "other_links"]:
        for k, v in page_data.get(key, {}).items():
            if k == "category_links":
                continue
            for item in v:
                # remove trailing label
                item = item.split("|")[0]
                if not item.startswith("http"):
                    # seems to be a link target, expand it
                    item = _expand_link_target(item, page.title)
                result.add(item)
    doc_link = page_data.get("doc_link")
    if doc_link:
        result.add(doc_link)
    result = list(result)
    if strict:
        result = [link for link in result if _allowed_doc_link(link)]
    return result

# ----------------------------------------------------------------------
# Documents table record updates

def _form_document_record(url):
    """
    Parse a MediaWiki file URL and return:
      {
        "title": "File:...",
        "source": "commons" | "wikisource" | "other",
        "link": canonical_link_or_original
      }

    If source == "other", title will be None and link is the original URL.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    # Must be a /wiki/ URL to extract a title
    if not path.startswith("/wiki/"):
        return {
            "title": None,
            "source": "other",
            "link": url,
        }

    # Extract and decode title
    title = unquote(path[len("/wiki/"):])

    # Wikimedia Commons
    if host == "commons.wikimedia.org":
        return {
            "title": title,
            "source": "commons",
            "link": f"https://commons.wikimedia.org/wiki/{title}",
        }

    # Wikisource (any language subdomain)
    if host.endswith(".wikisource.org"):
        return {
            "title": title,
            "source": "wikisource",
            "link": f"https://{host}/wiki/{title}",
        }

    # Anything else
    return {
        "title": None,
        "source": "other",
        "link": url,
    }

def _fetch_mediawiki_file_metadata_chunk(titles, source, thumbnail_width = 300):
    # Resolve API endpoint
    if source == "commons":
        api = "https://commons.wikimedia.org/w/api.php"
    elif source == "wikisource":
        api = "https://uk.wikisource.org/w/api.php"
    else:
        raise ValueError(f"Unsupported source: {source}")

    # Normalize input to list (preserve caller strings for keys)
    if isinstance(titles, str):
        requested_titles: List[str] = [titles.strip()]
    else:
        requested_titles = [t.strip() for t in titles if t and t.strip()]

    if not requested_titles:
        return {}

    params = {
        "action": "query",
        "format": "json",
        "prop": "imageinfo",
        "titles": "|".join(requested_titles),
        "iiprop": "timestamp|size|mime|mediatype|url|sha1",
        "iiurlwidth": thumbnail_width,
        "iilimit": 1,
    }
    data = fetch_url(api, params=params, json=True)
    query = data.get("query", {})

    # 1) Build mapping: input title -> normalized title (API "to")
    # If a title is not normalized by the API, map to itself.
    normalized_map = {t: t for t in requested_titles}
    for item in query.get("normalized", []):
        frm = item.get("from")
        to = item.get("to")
        if frm and to:
            normalized_map[frm] = to

    # 2) Build reverse index: api_title -> page object
    pages = query.get("pages", {})
    pages_by_title = {}
    for _pageid, page in pages.items():
        t = page.get("title")
        if t:
            pages_by_title[t] = page

    # 3) For each requested title, resolve to the API-normalized title, then to page
    results = {}
    for req_title in requested_titles:
        api_lookup_title = normalized_map.get(req_title, req_title)
        page = pages_by_title.get(api_lookup_title)

        # page may be missing even if present in pages dict
        if not page or page.get("missing") or not page.get("imageinfo"):
            results[req_title] = None
            continue

        ii = page["imageinfo"][0]
        record = {
            "title": page.get("title"),
            "source": source,
            "timestamp": _normalize_date_string(ii.get("timestamp")),
            "byte_size": ii.get("size"),
            "mimetype": ii.get("mime"),
            "mediatype": ii.get("mediatype"),
            "width": ii.get("width"),
            "height": ii.get("height"),
            "page_count": ii.get("pagecount"),
            "sha1_hash": ii.get("sha1"),
            "description_url": unquote(ii.get("descriptionurl")),
        }
        if "thumburl" in ii:
            record["thumb_url"] = unquote(ii.get("thumburl"))
            record["thumb_width"] = ii.get("thumbwidth")
            record["thumb_height"] = ii.get("thumbheight")
        results[req_title] = record

    return results

def _fetch_mediawiki_file_metadata(titles, source, thumbnail_width = 300):
    """
    Batch-fetch MediaWiki file metadata for one or more File: titles.

    Returns a dict keyed by the *caller-supplied* titles (trimmed), with metadata
    containing both:
      - "requested_title": as supplied by the caller (after trimming)
      - "api_title": the title returned by the API for the page (canonical/normalized)

    The MediaWiki API may normalize titles (underscores -> spaces, punctuation, etc.).
    This function reads query.normalized[] and uses it to preserve a stable mapping
    back to the caller's input.

    Parameters
    ----------
    titles : str | list[str]
        One title (e.g. "Файл:…pdf" or "File:…pdf") or a list of titles.
    source : str
        "commons" or "wikisource".
    thumbnail_width : int
        Requested thumbnail width in pixels.

    Returns
    -------
    dict[str, dict]
        Mapping of requested_title -> metadata dict.
    """
    if isinstance(titles, str):
        titles = [ titles ]
    if not all([isinstance(title, str) for title in titles]):
        raise ValueError("fetch_mediawiki_file_metadata_batch: titles must be str or list of str")
    CHUNK_SIZE = 25
    result = {}
    for i in range(0, len(titles), CHUNK_SIZE):
        chunk = titles[i:(i + CHUNK_SIZE)]
        chunk_result = _fetch_mediawiki_file_metadata_chunk(chunk, source, thumbnail_width)
        for k, v in chunk_result.items():
            result[k] = v
    return result

# ----------------------------------------------------------------------
# Documents table record updates

class Updater:
    def __init__(self, db, runtime):
        self._db = db
        self._runtime = runtime

    def _get_pages(self, page_titles):
        if isinstance(page_titles, str):
            page_titles = [ page_titles ]

        if not all(isinstance(title, str) for title in page_titles):
            raise ValueError("Updater._get_pages: page_titles must be str or list of str")

        pages = [self._runtime.lookup_by_title(title) for title in page_titles]
        if not all([isinstance(page, Page) for page in pages]):
            raise ValueError("Updater._get_pages: not all pages were found")

        result = []
        for page in pages:
            if not page.lastmod:
                _logger.info(f"Updater: ignoring nonexistent page: {_page_title(page)}")
            else:
                result.append(page)
        return result

    def update_page_records(self, page_titles, update_child_links=True):
        pages = self._get_pages(page_titles)
        if not all([isinstance(page, Page) for page in pages]):
            raise ValueError("Updater.update_page_records: not all pages were found")
        updated_page_ids = _update_pages(self._db, pages)

        if update_child_links:
            child_links_changed = False
            for page in pages:
                if page.children:
                    _logger.info(f"checking child links for {_page_title(page)} ({len(page.children)} children)")
                    child_links_changed = _link_children(self._db, page) or child_links_changed

        if updated_page_ids or child_links_changed:
            _logger.info("Updater.update_page_records: database updated")
            return True

        _logger.info("Updater.update_page_records: no changes")
        return False

    def update_linked_documents(self, page_titles, update_links=True):
        pages = self._get_pages(page_titles)
        page_doc_urls = { _page_title(page): _extract_page_links(page) for page in pages }
        doc_urls = set([url for docs in page_doc_urls.values() for url in docs])
        doc_records = { url: _form_document_record(url) for url in doc_urls}

        # collect meta data for known sources
        _KNOWN_SOURCES = ("commons", "wikisource")
        missing = []
        for source in _KNOWN_SOURCES:
            subset = [record for record in doc_records.values() if record["source"] == source]
            subset_titles = [rec["title"] for rec in subset]
            metadata_records = _fetch_mediawiki_file_metadata(subset_titles, source)
            #_logger.info(f"metadata_records: {metadata_records}")
            for record in subset:
                metadata_record = metadata_records.get(record["title"])
                if metadata_record:
                    for k, v in metadata_record.items():
                        record[k] = v
                else:
                    missing.append(record["link"])

        # remove urls from known sources that have no metadata (not a true document)
        for url in missing:
            del doc_records[url]

        # detect any record changes and update those
        update = _detect_changes(self._db, "Documents", doc_records.values(), key="link")
        doc_ids = self._db.write("Documents", update) if update else []

        doc_links_changed = False
        if update_links:
            # update page doc links
            for page in pages:
                # get doc links for this page
                urls = page_doc_urls.get(_page_title(page), [])
                # ignore links that are missing or not allowed
                urls = [url for url in urls if url in doc_records.keys()]
                if urls:
                    doc_links_changed = _link_documents(self._db, page, urls) or doc_links_changed

        if update or doc_links_changed:
            _logger.info("Updater.update_linked_documents: database updated")
            return True

        _logger.info("Updater.update_linked_documents: no changes")
        return False

