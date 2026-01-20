# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

from time import sleep
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote
from pathlib import PurePosixPath
import mwparserfromhell

from birddog.database import Database
from birddog.wiki import (
    API_URL,
    canonicalize_title,
    classify_page,
    page_name,
    )
from birddog.utility import (
    fetch_url, 
    new_id, 
    json_size, 
    transliterate,
    )
from birddog.task import TaskManager

from birddog.log import get_logger
_logger = get_logger()

# ----------------------------------------------------------------------
# UTILITY FUNCTIONS

# clear all birddog alerts from given table
def _clear_alerts(db, table_name, alert = "birddog_alert"):
    key_field = db.key_field_name(table_name)
    where = (alert, "is", True)
    update = []
    cursor = None
    while True:
        batch, cursor = db.scan(table_name, cursor=cursor, where=where)
        for record in batch:
            if record.get(alert):
                update.append({
                    key_field: record[key_field],
                    alert: False,
                })
        if not cursor:
            break
    if update:
        db.write(table_name, update)

def _format_date(date):
    d = date.split(",")
    return f"{d[0]}-{d[1]}-{d[2]} {d[3]}:00+00:00"

def _normalize_date_string(s):
    return str(datetime.fromisoformat(s.replace("Z", "+00:00")))

def _normalize_title(title):
    return title.replace("Архів:", "").replace(' ', '_')

def _page_title(page):
    return _normalize_title(page.page["title"]["uk"])

def _edit_links(db, table_name, link_field, source_record, target_records, replace=True):
    if not isinstance(target_records, (list, tuple)):
        target_records = [ target_records ]
    target_set = set(target_records)
    target_records = list(target_set)
    #_logger.info(f"_edit_links: {link_field}, {source_record}, {target_records}, replace={replace}")
    existing_targets = db.get_links(table_name, link_field, source_record)
    existing_set = set(existing_targets)
    if existing_set == target_set or not replace and target_set.issubset(existing_set):
        # no change
        return False
    if replace:
        db.delete_links(table_name, link_field, source_record, existing_targets)
    db.create_links(table_name, link_field, source_record, target_records)
    return True

def _replace_links(db, table_name, link_field, source_record, target_records):
    return _edit_links(db, table_name, link_field, source_record, target_records, replace=True)

def _create_links(db, table_name, link_field, source_record, target_records):
    return _edit_links(db, table_name, link_field, source_record, target_records, replace=False)

def _detect_changes(db, table_name, records):
    if isinstance(records, dict):
        records = [ records ]
        singleton = True
    else:
        singleton = False
    key = db.key_field_name(table_name)
    id_map = db.lookup(table_name, {record[key] for record in records})
    current_records = db.read(table_name, list(id_map.values()))
    current_record_dict = {
        rec["Id"]: rec 
        for rec in db.read(table_name, list(id_map.values()))
        }
    update = []
    update_fields = []
    for record in records:
        rec_id = id_map.get(record[key])
        if rec_id:
            changed_fields = set()
            current_rec = current_record_dict[rec_id]
            for k, v in record.items():
                if not k in current_rec or current_rec[k] != v:
                    changed_fields.add(k)
            if changed_fields:
                update.append(record)
                update_fields.append(changed_fields)
        else:
            update.append(record)
    #for record in update:
    #    record["birddog_alert"] = True
    if singleton:
        return update[0] if update else None
    return update, update_fields

# ----------------------------------------------------------------------
# PAGE RECORD UPDATES

def _get_links(title):
    params = {
        "action": "parse",
        "prop": "links|iwlinks|externallinks|wikitext|revid",
        "format": "json",
        "page": canonicalize_title(title),
    }
    return fetch_url(API_URL, params=params, return_json=True)

_DOCUMENT_SUFFIXES = {
    "pdf", "djvu", "djv",
    "tif", "tiff", "jp2",
    "zip", "cbz", "cbr",
}

_IMAGE_SUFFIXES = {
    "jpg", "jpeg", "png", "gif", "bmp", "webp"
}

def _sniff_suffix(url_or_title: str) -> str | None:
    """
    Returns one of:
      - "document"
      - "image"
      - None (unknown / webpage)
    """
    # Remove query / fragment
    parsed = urlparse(url_or_title)
    path = parsed.path or url_or_title

    suffix = PurePosixPath(path).suffix.lower().lstrip(".")
    if not suffix:
        return None

    if suffix in _DOCUMENT_SUFFIXES:
        return "document"

    if suffix in _IMAGE_SUFFIXES:
        return "image"

    return None

def _is_category_link(title):
    return title.startswith("Категорія:")

_DOC_LINK_BLOCKLIST = [
    "FSMosaicTreeLogo",
    "familysearch.org",
]

def _allowed_doc_link(link):
    return all([item not in link for item in _DOC_LINK_BLOCKLIST])

def _form_simple_page_record(title):
    return {
        "title": title,
        "source_type": "wiki",
        "availability": "linked",
    }

def _kind(title, record):
    result = classify_page(title)
    if result == "case" and record.get("children"):
        return "opus"
    return result

def _label(title):
    try:
        return transliterate(page_name(title))
    except ValueError as err:
        return None
        
def _get_latest_mod_date(revid):
    # Last modified date via API `revisions` (for this oldid)
    params = {
        'action': 'query',
        'prop': 'revisions',
        'revids': revid,
        'rvprop': 'timestamp',
        'format': 'json',
    }
    raw = fetch_url(API_URL, params=params, return_json=True)
    query = raw.get("query")
    if not query:
        return None
    pages = query.get("pages")
    if not pages:
        return None
    for result in pages.values():
        revisions = result.get("revisions", [])
        if revisions:
            date_string = revisions[0].get("timestamp", None)
            if date_string:
                date_string = _normalize_date_string(date_string)
            return date_string
        return None
    
def _parse_wiki_templates(parse):
    wikitext = parse.get("wikitext", {}).get("*", "")
    wikicode = mwparserfromhell.parse(wikitext)
    desc = ""
    dates = ""
    for template in wikicode.filter_templates():
        if template.name.startswith("Архіви") or template.name.startswith("заголовок"):
            if template.has("назва"):
                desc = template.get("назва").value.strip_code().strip(" ./\n")
            if template.has("секція") and not desc:
                desc = template.get("секція").value.strip_code().strip()
            if template.has("рік"):
                dates = template.get("рік").value.strip_code().strip()
            break
    return {
        "description_uk": desc.strip(),
        "years": dates.strip(),
    }

def _extract_links_from_wiki_parse(title, parse):
    internal_links = []
    category_links = []
    children = []
    parent = None
    for link in parse.get("links", []):
        other_title = link.get("*")
        if other_title:
            canonical_title = canonicalize_title(other_title)
            item = {
                "title": canonical_title,
                "exists": "exists" in link
            }
            if canonical_title.startswith(title):
                children.append(item)
            elif title.rsplit("/", 1)[0] == canonical_title:
                parent = item
            elif _is_category_link(other_title):
                category_links.append(item)
            else:
                item["doc_type"] = _sniff_suffix(canonical_title)
                internal_links.append(item)

    interwiki_links = []
    commons_links = []
    for link in parse.get("iwlinks", []):
        other_title = link.get("*")
        url = link.get("url")
        if other_title:
            item = {
                "title": other_title,
                "url": unquote(url),
                "doc_type": _sniff_suffix(url),
            }
            if url.startswith("https://commons.wikimedia.org"):
                commons_links.append(item)
            else:
                interwiki_links.append(item)

    return {
        "title": title,
        "pageid": parse.get("pageid"),
        "parent": parent,
        "children": children,
        "category_links": category_links,
        "internal_links": internal_links,
        "commons_links": commons_links,
        "interwiki_links": interwiki_links,
        "external_links": parse.get("externallinks", []),
    }

def _form_page_info_from_title(title):
    title = canonicalize_title(title)
    raw = _get_links(title)
    parse = raw.get("parse", {})
    info = {
        "title": _normalize_title(title),
        "record": _parse_wiki_templates(parse),
        "links": _extract_links_from_wiki_parse(title, parse),
    }
    record = info["record"]
    record["title"] = _normalize_title(title)
    error = raw.get("error")
    if error:
        if error.get("code") == "missingtitle":
            info["missing"] = True
        info["error"] = error
    else:
        record["level"] = _kind(title, record)
        record["label"] = _label(title)
        revid = parse.get("revid", "")
        record["reference_date"] = _get_latest_mod_date(revid)
        record["availability"] = "linked"
        record["source_type"] = "wiki"
    return info

# ----------------------------------------------------------------------
# DOCUMENT RECORD UPDATES

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
    _logger.info(f"_form_document_record: {url}")
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    # Must be a /wiki/ URL to extract a title
    if not path.startswith("/wiki/"):
        if _sniff_suffix(url):
            title = path.rsplit("/", 1)[-1]
        else:
            title = url
        return {
            "title": title,
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
        "title": title,
        "source": "other",
        "link": url,
    }

def _fetch_mediawiki_file_metadata_chunk(titles, source, thumbnails=False, thumbnail_width=300):
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
    }
    if thumbnails:
        params["iiurlwidth"] = thumbnail_width
        params["iilimit"] = 1
    data = fetch_url(api, params=params, return_json=True)

    error = data.get("error")
    if error:
        _logger.error(f"_fetch_mediawiki_file_metadata_chunk error: {error}")
    query = data.get("query", {})

    # 1) Build mapping: input title -> normalized title (API "to")
    # If a title is not normalized by the API, map to itself.
    normalized_map = {t: t for t in requested_titles}
    for item in query.get("normalized", []):
        frm = item.get("from")
        to = item.get("to")
        if frm and to:
            #_logger.info(f"title map: {frm}->{to}")
            normalized_map[frm] = to

    # 2) Build reverse index: api_title -> page object
    pages = query.get("pages", {})
    pages_by_title = {}
    for _pageid, page in pages.items():
        #_logger.info(f"page info: {page}")
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
            _logger.info(f"skipping missing title: {req_title}, {api_lookup_title}, {page}")
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

def _fetch_mediawiki_file_metadata(titles, source, thumbnails=False, thumbnail_width=300):
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

    #_logger.info(f"_fetch_mediawiki_file_metadata: processing {len(titles)} titles")

    def _do_chunk(pos, cursor):
        chunk = titles[pos:cursor]
        chunk_result = _fetch_mediawiki_file_metadata_chunk(
            chunk, source, thumbnails=thumbnails, thumbnail_width=thumbnail_width)
        for k, v in chunk_result.items():
            if not v:
                _logger.info(f"_fetch_mediawiki_file_metadata_chunk: .... empty metadata: {k}, {v}")
        result.update(chunk_result)

    CHUNK_LIMIT = 1000
    result = {}

    n = len(titles)
    pos = 0
    cursor = 0
    chunk_length = 0

    while cursor < n:
        next_len = len(titles[cursor]) + 1  # +1 for separator

        # If adding the next title would exceed the limit, flush current chunk first.
        # (But ensure forward progress if chunk is empty.)
        if chunk_length > 0 and (chunk_length + next_len) >= CHUNK_LIMIT:
            _do_chunk(pos, cursor)
            pos = cursor
            chunk_length = 0
            continue

        # Add the next title
        chunk_length += next_len
        cursor += 1

    # Flush the final chunk
    if pos < n:
        _do_chunk(pos, cursor)

    for title in titles:
        if not result.get(title):
            _logger.info(f"_fetch_mediawiki_file_metadata: missing title: {title}")
    return result

# ----------------------------------------------------------------------
# DATABASE UPDATER

class DatabaseUpdater:
    def __init__(self, runtime, db=None):
        self._runtime = runtime
        self._db = db if db else Database()

    # -------------------------------------------------------------------------
    # UTILITY

    def clear_alerts(self):
        _clear_alerts(self._db, "Pages")
        _clear_alerts(self._db, "Documents")

    # -------------------------------------------------------------------------
    # PAGE AND DOCUMENT TABLE UPDATES

    def _get_page_info(self, page_titles):
        result = []
        _logger.info(f"Updater: accessing wiki page info for {len(page_titles)} pages")
        for title in page_titles:
            info = _form_page_info_from_title(title)
            if info.get("missing"):
                _logger.error(f"Wiki page missing: {title}")
            elif info.get("error"):
                _logger.error(f"Error loading Wiki page {title}: {record.get("error")}")
            else:
                result.append(info)
        return result

    def _update_page_records(self, page_records, set_alert=True):
        update, update_fields = _detect_changes(self._db, "Pages", page_records)
        if update:
            for i, changed_fields in enumerate(update_fields):
                # clear translated fields if description changed
                if "description_uk" in changed_fields:
                    update[i]["description"] = ""
            if set_alert:
                for rec in update:
                    rec["birddog_alert"] = True
            return self._db.write("Pages", update)
        return None

    def _link_documents_with_page_id(self, page_id, urls):
        doc_ids = self._db.lookup("Documents", urls)
        if not all(doc_ids):
            missing_urls = [url for url, did in zip(urls, doc_ids) if not did]
            for url in missing_urls:
                _logger.info(f"doc url missing: {url}. ignoring...")
            doc_ids = [d for d in doc_ids if d]
        if _replace_links(self._db, "Pages", "doc_links", page_id, doc_ids):
            return True
        # otherwise, no change in links
        return False

    def update_records(self, page_titles):
        if isinstance(page_titles, str):
            page_titles = [ page_titles ]
        if not all([isinstance(title, str) for title in page_titles]):
            raise ValueError("Updater.update_records: page_titles must be str or sequence of str")

        page_info = self._get_page_info(page_titles)
        updated_pages = bool(self._update_page_records(
            [info["record"] for info in page_info]))

        linked_page_updates = []
        child_link_updates = {}
        parent_link_updates = {}
        title_set = set()
        doc_urls = set()
        doc_link_updates = {}
        
        def _add_child_link(parent, child):
            updates = child_link_updates.get(parent, set())
            updates.add(child)
            child_link_updates[parent] = updates
        
        def _add_parent_link(child, parent):
            if parent_link_updates.get(child):
                _logger.error(f"second parent for child {child}: {parent}, {parent_link_updates.get(child)}")
            parent_link_updates[child] = parent

        def _add_doc_link(title, doc_url):
            #_logger.info(f"found linked doc: {title} url={doc_url}")
            updates = doc_link_updates.get(title, set())
            updates.add(doc_url)
            doc_link_updates[title] = updates
            doc_urls.add(doc_url)

        _logger.info(f"Updater: analyzing page links")
        for info in page_info:
            page_links = info.get("links", {})
            title = info["title"]
            title_set.add(title)
            parent = page_links.get("parent") or {}
            if parent.get("exists"):
                parent_title = _normalize_title(parent["title"])
                title_set.add(parent_title)
                linked_page_updates.append(_form_simple_page_record(parent_title))
                _add_parent_link(title, parent_title)
            child_links = page_links.get("children") or []
            for child in child_links:
                if child.get("exists"):
                    child_title = _normalize_title(child["title"])
                    title_set.add(child_title)
                    linked_page_updates.append(_form_simple_page_record(child_title))
                    _add_child_link(title, child_title)
            for link in page_links.get("internal_links", []):
                if link.get("doc_type"):
                    doc_title = _normalize_title(link.get("title"))
                    url = f"https://uk.wikisource.org/wiki/{doc_title}"
                    _add_doc_link(title, url)
                #else:
                #    _logger.info(f"ignoring non-doc wiki link: {title} wiki target={link.get('title')}")
            for source in ("commons_links", "interwiki_links"):
                for link in page_links.get(source, []):
                    if link.get("doc_type"):
                        url = link.get("url")
                        url = url.replace(" ", "_").replace("file:", "File:")
                        _add_doc_link(title, url)
                    #else:
                    #    _logger.info(f"ignoring non-doc link: {title} url={link.get('url')}")
            for url in page_links.get("external_links", []):
                if _allowed_doc_link(url):
                    _add_doc_link(title, url)

        linked_records_changed = bool(self._update_page_records(linked_page_updates, set_alert=False))
        record_ids = self._db.lookup("Pages", title_set)

        links_changed = False
        link_children = True
        if link_children:
            _logger.info(f"Updater: linking child pages")
            for parent_title, child_titles in child_link_updates.items():
                parent_id = record_ids[parent_title]
                child_ids = [record_ids[t] for t in child_titles]
                if _replace_links(self._db, "Pages", "children", parent_id, child_ids):
                    _logger.info(f"Child links for {parent_title} updated ({len(child_ids)} children)")
                    links_changed = True
            for child_title, parent_title in parent_link_updates.items():
                child_id = record_ids[child_title]
                parent_id = record_ids[parent_title]
                if _create_links(self._db, "Pages", "children", parent_id, child_id):
                    _logger.info(f"Parent link for {child_title} updated ({parent_title})")
                    links_changed = True

        doc_records_changed = False
        doc_links_changed = False
        if doc_urls:
            _logger.info(f"Updater: accessing linked document metadata")
            doc_records = { url: _form_document_record(url) for url in doc_urls}

            # collect meta data for known sources
            _KNOWN_SOURCES = ("commons", "wikisource")
            missing = []
            for source in _KNOWN_SOURCES:
                subset = [record for record in doc_records.values() if record["source"] == source]
                subset_titles = [rec["title"] for rec in subset]
                if subset_titles:
                    metadata_records = _fetch_mediawiki_file_metadata(subset_titles, source)
                    #_logger.info(f"metadata_records: {metadata_records}")
                    for record in subset:
                        metadata_record = metadata_records.get(record["title"])
                        if metadata_record:
                            record.update(metadata_record)
                        else:
                            _logger.info(f"ignoring missing doc url {record["title"]}")
                            missing.append(record["link"])

            # remove urls from known sources that have no metadata (not a true document)
            for url in missing:
                if url in doc_records:
                    del doc_records[url]

            # detect any record changes and update those
            update, update_fields = _detect_changes(self._db, "Documents", doc_records.values())
            for record in update:
                record["birddog_alert"] = True
            doc_ids = self._db.write("Documents", update) if update else []
            doc_records_changed = bool(doc_ids)

            # update page doc links
            _logger.info(f"Updater: updating document links")
            for page_title, doc_urls in doc_link_updates.items():
                page_id = record_ids[page_title]
                if self._link_documents_with_page_id(page_id, list(doc_urls)):
                    _logger.info(f"Doc links for {page_title} updated ({len(doc_urls)} docs)")
                    doc_links_changed = True

        return any([
            updated_pages, 
            linked_records_changed, 
            links_changed, 
            doc_records_changed, 
            doc_links_changed])

    # -------------------------------------------------------------------------
    # TRANSLATION SUPPORT

    # collect all untranslated descriptions from Pages table
    def _collect_translations(self):
        table_name = "Pages"
        description_uk = "description_uk"
        description = "description"
        where = (description_uk, "isnot", None)
        key_field = self._db.key_field_name(table_name)
        translations = []
        cursor = None
        while True:
            batch, cursor = self._db.scan(table_name, cursor=cursor, where=where)
            for record in batch:
                ukrainian_description = record.get(description_uk)
                if ukrainian_description and not record.get(description):
                    translations.append({
                        key_field: record[key_field],
                        description_uk: ukrainian_description,
                    })
            if not cursor:
                break
        return translations

    def start_translation(self):
        translations = self._collect_translations()
        if translations:
            task_name = f"DBT_{new_id()}"
            translation_items = [t["description_uk"] for t in translations]
            _logger.info(f"Updater: starting translation task (length={len(translation_items)})")
            self._runtime.start_translation(task_name=task_name, items=translation_items)

    def complete_translation(self, task_name, translation_map):
        translations = self._collect_translations()
        if translations:
            update = []
            for record in translations:
                translation = translation_map.get(record["description_uk"])
                if translation:
                    record["description"] = translation
                    update.append(record)
            if update:
                _logger.info(f"Updater: updating translations (length={len(update)})")
                self._db.write("Pages", update)

class DatabaseUpdateManager(TaskManager):
    _BATCH_SIZE = 100

    def __init__(self, runtime, updater=None):
        self._updater = updater if updater else DatabaseUpdater(runtime)
        super().__init__("DatabaseUpdateManager")
        # adjust subtask timeout to allow for approx 1 sec per item in batch
        self._stale_subtask_threshold_ms = self._BATCH_SIZE * 1000

    def execute_subtask(self, subtask):
        title_batch = subtask["payload"]
        try:
            subtask["payload"] = {"updated": self._updater.update_records(title_batch)}
        except Exception as err:
            _logger.error(f"DatabaseUpdateManager: exception during subtask execution: {err}")
            subtask["payload"] = {"error": str(err)}

    def complete_task(self, task_desc, subtasks):
        try:
            if any([subtask["payload"].get("updated") for subtask in subtasks]):
                _logger.info("Update task completed. Some records changed. Starting translations")
                # kick off translation on any new records
                self._updater.start_translation()
        except Exception as err:
            _logger.error(f"DatabaseUpdateManager: exception during task completion: {err}")

    def complete_translation(self, task_name, translation_map):
        self._updater.complete_translation(task_name, translation_map)

    def start_update(self, page_titles):
        if isinstance(page_titles, str):
            page_titles = [ page_titles ]
        if not isinstance(page_titles, (list, tuple)) or not all([isinstance(title, str) for title in page_titles]):
            raise ValueError("DatabaseUpdateManager.start_update_task: page_titles must be str or sequence of str")
        total = len(page_titles)
        if total > 0:
            task_name = f"DBU_{new_id()}"
            batches = []
            for i in range(0, total, self._BATCH_SIZE):
                batches.append(page_titles[i:i+self._BATCH_SIZE])
            self.create(task_name, batches)






