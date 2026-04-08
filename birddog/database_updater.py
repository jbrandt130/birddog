# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse, unquote
from pathlib import PurePosixPath
import json
import threading
import mwparserfromhell

from birddog.database import Database
from birddog.wiki import (
    API_URL,
    canonicalize_title,
    page_label,
    parent_title,
    sequential_page_label,
    page_kind,
    page_url_from_title,
    )
from birddog.utility import (
    new_id,
    )
from birddog.task import TaskManager
from birddog.fetch import fetch_url
from birddog.store import KeyValueStore

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

def _normalize_date_string(s):
    return str(datetime.fromisoformat(s.replace("Z", "+00:00")))

def _normalize_title(title):
    # ensure no spaces in title, and remove namespace qualifier
    return canonicalize_title(title, include_namespace=False)

def _edit_links(db, table_name, link_field, source_record, target_records, replace=True):
    if not isinstance(target_records, (list, tuple)):
        target_records = [ target_records ]
    target_set = set(target_records)
    target_records = list(target_set)
    #_logger.info(f"_edit_links: {link_field}, {source_record}, target len={len(target_records)}, replace={replace}")
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

def _page_urls_from_titles(titles):
    if isinstance(titles, set):
        return {page_url_from_title(title) for title in titles}
    if isinstance(titles, (list, tuple)):
        return [page_url_from_title(title) for title in titles]
    if isinstance(titles, str):
        return page_url_from_title(titles)
    raise TypeError(f"_page_urls_from_titles: invalid type: {type(titles)}")

def _lookup_pages(db, titles):
    return db.lookup("Pages", _page_urls_from_titles(titles))

def get_child_titles(db, titles):
    if not isinstance(titles, (list, tuple)):
        titles = [titles]

    parent_ids = _lookup_pages(db, titles)

    result = []
    cursor = None

    while True:
        page, cursor = scan_child_titles_by_id(
            db,
            parent_ids,
            limit=100,   # reasonable default batch size
            cursor=cursor,
        )
        result.extend(page)
        if not cursor:
            break

    # preserve legacy behavior: deduplicate
    return list(set(result))

def scan_child_titles_by_id(db, parent_ids, limit=100, cursor=None):
    if not isinstance(parent_ids, (list, tuple)):
        parent_ids = [parent_ids]

    if limit <= 0:
        return [], cursor

    if cursor is None:
        parent_index = 0
        link_cursor = None
    else:
        try:
            parent_index, link_cursor = cursor
        except Exception:
            raise InvalidRecordId(f"Invalid cursor value: {cursor}")

    child_ids = []

    while parent_index < len(parent_ids) and len(child_ids) < limit:
        parent_id = parent_ids[parent_index]

        if not parent_id:
            parent_index += 1
            link_cursor = None
            continue

        remaining = limit - len(child_ids)
        page_ids, next_link_cursor = db.scan_links(
            "Pages",
            "children",
            parent_id,
            limit=remaining,
            cursor=link_cursor,
        )
        child_ids.extend(page_ids)

        if next_link_cursor is None:
            parent_index += 1
            link_cursor = None
        else:
            link_cursor = next_link_cursor

    # optimized read: only fetch "title"
    if child_ids:
        records = db.read("Pages", child_ids, fields="title")
        child_titles = [rec.get("title") for rec in records if rec]
    else:
        child_titles = []

    if parent_index >= len(parent_ids) and link_cursor is None:
        next_cursor = None
    else:
        next_cursor = (parent_index, link_cursor)

    return child_titles, next_cursor

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
    title = _normalize_title(title)
    label = page_label(title)
    return {
        "title": title,
        "url": page_url_from_title(title),
        "label": label,
        "seq_label": sequential_page_label(label),
        "source_type": "wiki",
        "availability": "linked",
    }

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

def _safe_parent_title(title):
    try:
        return parent_title(title)
    except ValueError:
        return None

def _extract_links_from_wiki_parse(title, parse):
    internal_links = []
    category_links = []
    children = []
    parent = None
    parent_title_name = _safe_parent_title(title)
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
            elif parent_title_name == canonical_title:
                parent = item
            elif _is_category_link(other_title):
                category_links.append(item)
            elif _safe_parent_title(item["title"]) == title:
                children.append(item)
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
                "url": url,
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
    record["url"] = page_url_from_title(title)
    error = raw.get("error")
    if error:
        if error.get("code") == "missingtitle":
            info["missing"] = True
        info["error"] = error
    else:
        record["level"] = page_kind(title, info["links"].get("children"))
        record["label"] = page_label(title)
        record["seq_label"] = sequential_page_label(record["label"])
        revid = parse.get("revid", "")
        record["timestamp"] = _get_latest_mod_date(revid)
        record["availability"] = "linked"
        record["source_type"] = "wiki"
    return info

# ----------------------------------------------------------------------
# DOCUMENT RECORD UPDATES

def form_document_record(url):
    """
    Parse a MediaWiki file URL and return:
      {
        "title": "File:...",
        "source": "commons" | "wikisource" | "other",
        "link": canonical_link_or_original
      }

    If source == "other", title will be None and link is the original URL.
    """
    url = url.strip()
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
    quoted_title = path[len("/wiki/"):]
    title = unquote(quoted_title)

    # Wikimedia Commons
    if host == "commons.wikimedia.org":
        return {
            "title": title,
            "source": "commons",
            "link": f"https://commons.wikimedia.org/wiki/{quoted_title}",
        }

    # Wikisource (any language subdomain)
    if host.endswith(".wikisource.org"):
        return {
            "title": title,
            "source": "wikisource",
            "link": f"https://{host}/wiki/{quoted_title}",
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
        requested_titles: list[str] = [titles.strip()]
    else:
        requested_titles = [t.strip() for t in titles if t and t.strip()]

    if not requested_titles:
        return {}

    #_logger.info(f"_fetch_mediawiki_file_metadata_chunk: {requested_titles}")
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
    #_logger.info(f"fetching meta data. request size = {json_size(params)}")
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
            _logger.info(f"missing title: {req_title}, {api_lookup_title}, {page}")
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
            "description_url": ii.get("descriptionurl"),
        }
        if "thumburl" in ii:
            record["thumb_url"] = ii.get("thumburl")
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

    CHUNK_LIMIT = 800
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
                _logger.error(f"Error loading Wiki page {title}: {info.get('error')}")
            else:
                result.append(info)
        return result

    def _update_page_records_with_cache(self, page_records, id_map, existing_records, set_alert=True):
        """Update page records using pre-fetched lookup and existing records."""
        if not page_records:
            return None

        update = []
        update_fields = []

        for record in page_records:
            record_url = record["url"]
            rec_id = id_map.get(record_url)
            if not rec_id:
                #_logger.info(f"NO ID MATCH: record title={record.get('title')} url={record_url}")
                #_logger.info(f"id_map sample keys: {list(id_map.keys())[:3]}")
                update.append(record)
                update_fields.append(set())
                continue

            current_rec = existing_records.get(rec_id, {})
            changed_fields = set()
            for k, v in record.items():
                if k not in current_rec or current_rec[k] != v:
                    changed_fields.add(k)
            if changed_fields:
                #_logger.info(
                #    f"CHANGED: title={record.get('title')} fields={sorted(changed_fields)} "
                #    f"new={[record.get(k) for k in sorted(changed_fields)]} "
                #    f"old={[current_rec.get(k) for k in sorted(changed_fields)]}"
                #)
                update.append(record)
                update_fields.append(changed_fields)

        if update:
            for i, changed_fields in enumerate(update_fields):
                if "description_uk" in changed_fields:
                    update[i]["description"] = ""
            if set_alert:
                for rec in update:
                    rec["birddog_alert"] = True
            _logger.info(f"_update_page_records_with_cache: write {len(update)} records")
            return self._db.write("Pages", update)

        return None

    def update_page_records(self, page_titles):
        if isinstance(page_titles, str):
            page_titles = [page_titles]
        if not all(isinstance(title, str) for title in page_titles):
            raise ValueError("Updater.update_page_records: page_titles must be str or sequence of str")

        tid = threading.get_ident()
        _logger.info(f"Updater {tid}: starting update_page_records: {len(page_titles)} titles")
        page_info = self._get_page_info(page_titles)
        #_logger.info(f"page_info: {page_info}")

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
                _logger.error(
                    f"second parent for child {child}: {parent}, {parent_link_updates.get(child)}"
                )
            parent_link_updates[child] = parent

        def _add_doc_link(title, doc_url):
            updates = doc_link_updates.get(title, set())
            updates.add(doc_url)
            doc_link_updates[title] = updates
            doc_urls.add(doc_url)

        _logger.info(f"Updater {tid}: analyzing page links")
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

            for source in ("commons_links", "interwiki_links"):
                for link in page_links.get(source, []):
                    if link.get("doc_type"):
                        url = link.get("url")
                        url = url.replace(" ", "_").replace("file:", "File:")
                        _add_doc_link(title, url)

            for url in page_links.get("external_links", []):
                if _allowed_doc_link(url):
                    _add_doc_link(title, url)

        page_records = [info["record"] for info in page_info]

        # Build page lookup from the exact URLs used in page records, plus linked placeholder URLs.
        requested_page_urls = {record["url"] for record in page_records}
        linked_page_urls = {page_url_from_title(rec["title"]) for rec in linked_page_updates}
        all_page_titles = title_set | {record["title"] for record in page_records}
        all_page_urls = requested_page_urls | linked_page_urls

        _logger.info(f"Updater {tid}: looking up page ids {len(all_page_urls)}")
        page_id_by_url = self._db.lookup("Pages", all_page_urls)
        existing_page_ids = [rid for rid in page_id_by_url.values() if rid]

        _logger.info(f"Updater {tid}: loading existing page records {len(existing_page_ids)}")
        existing_pages = (
            {rec["Id"]: rec for rec in self._db.read("Pages", existing_page_ids)}
            if existing_page_ids else {}
        )

        updated_pages = bool(
            self._update_page_records_with_cache(
                page_records, page_id_by_url, existing_pages, set_alert=True
            )
        )

        # Refresh cache before linked placeholder updates so newly written pages
        # are not treated as missing.
        if updated_pages:
            page_id_by_url = self._db.lookup("Pages", all_page_urls)
            existing_page_ids = [rid for rid in page_id_by_url.values() if rid]
            _logger.info(f"Updater {tid}: refresh existing page records {len(existing_page_ids)}")
            existing_pages = (
                {rec["Id"]: rec for rec in self._db.read("Pages", existing_page_ids)}
                if existing_page_ids else {}
            )

        linked_records_changed = bool(
            self._update_page_records_with_cache(
                linked_page_updates, page_id_by_url, existing_pages, set_alert=False
            )
        )

        # Refresh once more if linked placeholders were inserted.
        if linked_records_changed:
            _logger.info(f"Updater {tid}: refresh page ids {len(all_page_urls)}")
            page_id_by_url = self._db.lookup("Pages", all_page_urls)

        # Derive title -> page_id mapping explicitly for link operations.
        page_id_by_title = {
            title: page_id_by_url.get(page_url_from_title(title))
            for title in all_page_titles
        }

        links_changed = False
        _logger.info(f"Updater {tid}: linking child pages: child_link_updates: {len(child_link_updates)}, parent_link_updates: {len(parent_link_updates)}")

        try:
            for parent_title, child_titles in child_link_updates.items():
                parent_id = page_id_by_title.get(parent_title)
                if not parent_id:
                    continue

                child_ids = [
                    page_id_by_title[child_title]
                    for child_title in child_titles
                    if page_id_by_title.get(child_title)
                ]
                #_logger.info(f"start _replace_links {tid}: {len(child_ids)}")
                if _replace_links(self._db, "Pages", "children", parent_id, child_ids):
                    #_logger.info(f"Child links for {parent_title} updated ({len(child_ids)} children)")
                    links_changed = True
                #_logger.info(f"finish _replace_links {tid}")

            for child_title, parent_title in parent_link_updates.items():
                child_id = page_id_by_title.get(child_title)
                parent_id = page_id_by_title.get(parent_title)
                if not child_id or not parent_id:
                    continue

                #_logger.info(f"start _create_links {tid}")
                if _create_links(self._db, "Pages", "children", parent_id, child_id):
                    #_logger.info(f"Parent link for {child_title} updated ({parent_title})")
                    links_changed = True
                #_logger.info(f"finish _create_links {tid}")

        except Exception as e:
            _logger.error(f"exception during page linking {tid}: {e}. Aborting.")
            return False

        _logger.info(f"Updater {tid}: finished linking child pages")

        doc_records_changed = False
        doc_links_changed = False
        if doc_urls:
            doc_records_changed = self.update_doc_records(doc_urls)

            # One lookup for all document IDs, then reuse locally.
            doc_id_map = self._db.lookup("Documents", doc_urls)

            _logger.info(f"Updater {tid}: updating document links")
            for page_title, page_doc_urls in doc_link_updates.items():
                page_id = page_id_by_title.get(page_title)
                if not page_id:
                    continue

                doc_ids_for_page = [doc_id_map.get(url) for url in page_doc_urls]
                doc_ids_for_page = [doc_id for doc_id in doc_ids_for_page if doc_id]

                if _replace_links(self._db, "Pages", "doc_links", page_id, doc_ids_for_page):
                    #_logger.info(f"Doc links for {page_title} updated ({len(doc_ids_for_page)} docs)")
                    doc_links_changed = True

        _logger.info(f"Updater {tid}: finished update")

        return any([
            updated_pages,
            linked_records_changed,
            links_changed,
            doc_records_changed,
            doc_links_changed,
        ])

    def update_doc_records(self, doc_urls):
        doc_records_changed = False
        if isinstance(doc_urls, str):
            doc_urls = { doc_urls }
        elif isinstance(doc_urls, (list, tuple)):
            doc_urls = set(doc_urls)
        if not isinstance(doc_urls, set):
            raise ValueError("doc_urls must be str, squence of str, or set of str")

        tid = threading.get_ident()

        if doc_urls:
            _logger.info(f"Updater.update_doc_records {tid}: accessing linked document metadata")
            doc_records = { url: form_document_record(url) for url in doc_urls}

            # collect meta data for known sources
            _KNOWN_SOURCES = ("commons", "wikisource")
            for source in _KNOWN_SOURCES:
                subset = [record for record in doc_records.values() if record["source"] == source]
                subset_titles = [rec["title"] for rec in subset]
                if subset_titles:
                    metadata_records = _fetch_mediawiki_file_metadata(subset_titles, source)
                    for record in subset:
                        metadata_record = metadata_records.get(record["title"])
                        if metadata_record:
                            record.update(metadata_record)
                            record["availability"] = "linked"
                        else:
                            _logger.info(f"marking doc url as unlinked: {record['title']}")
                            record["availability"] = "unlinked"

            # Single lookup for all document URLs
            doc_id_map = self._db.lookup("Documents", doc_urls)
            existing_doc_ids = [did for did in doc_id_map.values() if did]
            existing_docs = {
                rec["Id"]: rec
                for rec in self._db.read("Documents", existing_doc_ids)
            } if existing_doc_ids else {}

            # Detect changes using pre-fetched data
            doc_update = []
            for record in doc_records.values():
                rec_id = doc_id_map.get(record["link"])
                if rec_id:
                    current_rec = existing_docs[rec_id]
                    changed = any(
                        k not in current_rec or current_rec[k] != v
                        for k, v in record.items()
                    )
                    if changed:
                        record["birddog_alert"] = True
                        doc_update.append(record)
                else:
                    record["birddog_alert"] = True
                    doc_update.append(record)

            doc_ids = self._db.write("Documents", doc_update) if doc_update else []
            doc_records_changed = bool(doc_ids)
            _logger.info(f"Updater.update_doc_records {tid}: finished")

        return doc_records_changed

    # -------------------------------------------------------------------------
    # TRANSLATION SUPPORT

    # collect all untranslated descriptions from Pages table. Depends on predefined view "Untranslated Pages"
    def _collect_translations(self):
        table_name = "Pages"
        view_name = "Untranslated Pages"
        key_field = self._db.key_field_name(table_name)
        fields = [key_field, "description", "description_uk"]
        translations = []
        cursor = None
        while True:
            batch, cursor = self._db.scan(table_name, cursor=cursor, fields=fields, view_name=view_name)
            _logger.info(f"collect translation batch: {len(batch)}")
            for record in batch:
                ukrainian_description = record.get("description_uk")
                if ukrainian_description and not record.get("description"):
                    translations.append({
                        key_field: record[key_field],
                        "description_uk": ukrainian_description,
                    })
            if not cursor:
                break
        return translations

    def start_translation(self):
        translations = self._collect_translations()
        _logger.info(f"Updater: collecting needed translations (length={len(translations)})")
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
                result = self._db.write("Pages", update)

class DatabaseUpdateManager(TaskManager):
    _BATCH_SIZE = 20
    _EXPAND_BATCH_SIZE = 500
    _UPDATE_STATE_NS = "tasks"
    _EXPAND_STATE_NS = "expand"

    def __init__(self, runtime, updater=None):
        self._updater = updater if updater else DatabaseUpdater(runtime)
        self._state_store = KeyValueStore(table_name="bd_db_update")
        self._state_lock = threading.Lock()
        super().__init__("DatabaseUpdateManager")
        # update subtasks: allow approx 10 sec per item in batch
        self._stale_subtask_threshold_ms = self._BATCH_SIZE * 10000

    # ------------------------------------------------------------------
    # state helpers

    def _get_state(self, namespace, key):
        return json.loads(self._state_store.get(namespace, key))

    def _put_state(self, namespace, key, value):
        self._state_store.insert(namespace, key, json.dumps(value))

    def _remove_state(self, namespace, key):
        try:
            self._state_store.remove(namespace, key)
        except KeyError:
            pass

    def _update_progress(self, task_id, increment):
        _logger.info(f"update progress: {task_id}: {increment}")
        with self._state_lock:
            try:
                task = self.lookup_task(task_id)
                task_name = task["name"]
                state = self._get_state(self._UPDATE_STATE_NS, task_name)
                state["completed"] += increment
                self._put_state(self._UPDATE_STATE_NS, task_name, state)
            except KeyError:
                pass

    # ------------------------------------------------------------------
    # subtask execution

    def execute_subtask(self, subtask):
        payload = subtask["payload"]
        kind = payload.get("kind", "update_titles")
        if kind == "update_titles":
            self._execute_update_subtask(subtask)
        elif kind == "expand_children":
            self._execute_expand_subtask(subtask)
        else:
            raise ValueError(f"DatabaseUpdateManager.execute_subtask: unknown kind: {kind}")

    def _execute_update_subtask(self, subtask):
        _logger.info(f"DatabaseUpdateManager: update subtask {subtask['task_id']}.{subtask['index']}")
        batch = subtask["payload"]
        titles = batch["titles"]
        try:
            updated = self._updater.update_page_records(titles)
            subtask["payload"] = {
                "kind": "update_titles",
                "updated": updated,
                "titles": titles,
                "deep": batch["deep"],
            }
        except Exception as err:
            _logger.error(
                f"DatabaseUpdateManager: exception during update subtask execution: {err}, {batch}"
            )
            subtask["payload"] = {
                "kind": "update_titles",
                "titles": titles,
                "deep": batch.get("deep", False),
                "error": str(err),
            }
        self._update_progress(subtask["task_id"], len(titles))

    def _execute_expand_subtask(self, subtask):
        _logger.info(f"DatabaseUpdateManager: expand subtask {subtask['task_id']}.{subtask['index']}")
        payload = subtask["payload"]
        state_key = payload["state_key"]
        try:
            state = self._get_state(self._EXPAND_STATE_NS, state_key)
            parent_ids = state["parent_ids"]
            exclude_titles = set(state.get("exclude_titles", []))
            cursor = state.get("cursor")

            spawn_titles = []
            while True:
                page, cursor = scan_child_titles_by_id(
                    self._updater._db,
                    parent_ids,
                    limit=self._EXPAND_BATCH_SIZE,
                    cursor=cursor,
                )
                if page:
                    spawn_titles.extend(page)
                if cursor is None:
                    break

            # remove immediate self-respawn titles
            if exclude_titles:
                spawn_titles = [title for title in spawn_titles if title not in exclude_titles]

            # dedupe once at the task boundary to avoid redundant updates
            if spawn_titles:
                spawn_titles = list(set(spawn_titles))

            state["cursor"] = None
            state["completed"] = True
            state["spawn_count"] = len(spawn_titles)
            self._put_state(self._EXPAND_STATE_NS, state_key, state)

            subtask["payload"] = {
                "kind": "expand_children",
                "state_key": state_key,
                "spawn_titles": spawn_titles,
                "deep": True,
            }

        except Exception as err:
            _logger.error(
                f"DatabaseUpdateManager: exception during expand subtask execution: {err}, {payload}"
            )
            subtask["payload"] = {
                "kind": "expand_children",
                "state_key": state_key,
                "error": str(err),
            }

    # ------------------------------------------------------------------
    # task completion

    def complete_task(self, task_desc, subtasks, is_cancelled=False):
        _logger.info(f"DatabaseUpdateManager: complete task {task_desc['task_id']}")
        try:
            if is_cancelled:
                return

            if not subtasks:
                return

            payload0 = subtasks[0].get("payload", {})
            kind = payload0.get("kind", "update_titles")

            if kind == "update_titles":
                self._complete_update_task(task_desc, subtasks)
            elif kind == "expand_children":
                self._complete_expand_task(task_desc, subtasks)
            else:
                _logger.error(
                    f"DatabaseUpdateManager.complete_task: unknown task kind: {kind}, task={task_desc}"
                )

        except Exception as err:
            _logger.error(f"DatabaseUpdateManager: exception during task completion: {err}")

        finally:
            # clean up update task progress state if present
            with self._state_lock:
                self._remove_state(self._UPDATE_STATE_NS, task_desc["name"])

    def _complete_update_task(self, task_desc, subtasks):
        _logger.info(f"updater.complete_update_task: {task_desc['task_id']}")

        parent_titles = []
        for subtask in subtasks:
            payload = subtask.get("payload", {})
            if payload.get("kind", "update_titles") != "update_titles":
                continue
            if payload.get("deep"):
                parent_titles.extend(payload.get("titles", []))

        if not parent_titles:
            return

        _logger.info(f"DatabaseUpdateManager: deep update frontier completed: {len(parent_titles)} titles")
        self.start_expand(parent_titles)

    def _complete_expand_task(self, task_desc, subtasks):
        _logger.info(f"updater.complete_expand_task: {task_desc['task_id']}")

        if not subtasks:
            return

        payload = subtasks[0].get("payload", {})
        state_key = payload.get("state_key")
        spawn_titles = payload.get("spawn_titles", [])

        if spawn_titles:
            _logger.info(
                f"DatabaseUpdateManager: deep expansion spawned next update task: {len(spawn_titles)} titles"
            )
            self.start_update(spawn_titles, deep=True)

        if state_key:
            self._remove_state(self._EXPAND_STATE_NS, state_key)

    # ------------------------------------------------------------------
    # public API

    def complete_translation(self, task_name, translation_map):
        self._updater.complete_translation(task_name, translation_map)

    def start_update(self, page_titles, deep=False):
        if isinstance(page_titles, str):
            page_titles = [page_titles]
        if not isinstance(page_titles, (list, tuple)) or not all(
            [isinstance(title, str) for title in page_titles]
        ):
            raise ValueError(
                "DatabaseUpdateManager.start_update: page_titles must be str or sequence of str"
            )

        page_titles = [_normalize_title(title) for title in page_titles]
        total = len(page_titles)
        if total <= 0:
            return None

        task_name = f"DBU_{new_id()}"
        batches = []
        for i in range(0, total, self._BATCH_SIZE):
            batches.append(
                {
                    "kind": "update_titles",
                    "titles": page_titles[i:i + self._BATCH_SIZE],
                    "deep": deep,
                }
            )

        preview = page_titles[:50]
        titles_str = ", ".join(preview) + ("..." if total > 50 else "")
        state = {
            "kind": "update_titles",
            "titles": titles_str,
            "deep": deep,
            "total": total,
            "completed": 0,
        }
        self._put_state(self._UPDATE_STATE_NS, task_name, state)
        self.create(task_name, batches)
        return task_name

    def start_expand(self, parent_titles):
        if isinstance(parent_titles, str):
            parent_titles = [parent_titles]
        if not isinstance(parent_titles, (list, tuple)) or not all(
            [isinstance(title, str) for title in parent_titles]
        ):
            raise ValueError(
                "DatabaseUpdateManager.start_expand: parent_titles must be str or sequence of str"
            )

        parent_titles = [_normalize_title(title) for title in parent_titles]
        parent_ids = _lookup_pages(self._updater._db, parent_titles)
        if not any(parent_ids):
            _logger.info("DatabaseUpdateManager.start_expand: no valid parent ids found")
            return None

        task_name = f"DBX_{new_id()}"
        state_key = task_name
        state = {
            "kind": "expand_children",
            "parent_titles": parent_titles[:50],
            "exclude_titles": parent_titles,
            "parent_ids": parent_ids,
            "cursor": None,
            "completed": False,
            "spawn_count": 0,
        }
        self._put_state(self._EXPAND_STATE_NS, state_key, state)
        self.create(
            task_name,
            [
                {
                    "kind": "expand_children",
                    "state_key": state_key,
                }
            ],
        )
        return task_name

    def status(self):
        result = {}

        for item in self._state_store.get_all(self._UPDATE_STATE_NS):
            result[item[0]] = json.loads(item[1])

        #for item in self._state_store.get_all(self._EXPAND_STATE_NS):
        #    result[item[0]] = json.loads(item[1])

        return result

    def cancel(self, task_name):
        _logger.info(f"DatabaseUpdateManager: cancel task {task_name}")
        for task in self.active_tasks():
            if task.get("name") == task_name:
                _logger.info(f"DatabaseUpdateManager: cancel task {task_name} found!")
                super().cancel(task.get("task_id"))
                break
        self._remove_state(self._UPDATE_STATE_NS, task_name)
        self._remove_state(self._EXPAND_STATE_NS, task_name)
