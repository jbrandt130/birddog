# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

import os
import re
from time import sleep
import requests
from datetime import datetime
#from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlparse, parse_qs

from birddog.wiki import (
    WIKI_NAMESPACE,
    get_title,
    )

from birddog.logging import get_logger
_logger = get_logger()

_NOCODB_V3_API_ROOT     = "https://app.nocodb.com/api/v3"
_NOCODB_V2_API_ROOT     = "https://app.nocodb.com/api/v2"
_NOCODB_API_TOKEN       = os.environ["NOCODB_API_TOKEN"]
_NOCODB_API_DELAY       = .25

_BASE_ID = "pljzqjmv8a5nvku"
_TABLE_ID = {
    "pages": "mtrj7h4scl3s15b",
    "documents": "m676bm10pfq3hwk",
}
_FIELD_ID = {
    "doc_links": "c42iq5cns4oof05",
    "parent": "coy5kak8p66e1kv",
    "children": "cuj7mxq51rot9kt",
    "owning_page": "cbrvqbad1g0mdiv",
}

# ------------------------------------------------------------
# sheet data extraction

def get_page_title_from_link(cell):
    if not cell.hyperlink:
        return None
    url = cell.hyperlink.target
    if "index.php" in url:
        query = urlparse(url).query
        params = parse_qs(query)
        if "title" in params:
            result = params["title"][0]
            ns_prefix = f"{WIKI_NAMESPACE}:"
            if result.startswith(ns_prefix):
                result = result[len(ns_prefix):]
            return result
    return get_title(unquote(url), include_namespace=False)

def get_cell_link(cell):
    return unquote(cell.hyperlink.target) if cell.hyperlink else ""

def get_cell_value(cell):
    value = cell.value
    if value is None:
        return value
    if isinstance(value, float):
        value = int(value)
    if not isinstance(value, str):
        value = str(value)
    return value

def get_cell_int_value(cell):
    value = get_cell_value(cell)
    if value is None:
        return 0
    if isinstance(value, (str, int, float)):
        return int(value)
    raise TypeError(f"get_cell_int_value: unrecognized type: {type(value)}")

def combine_cell_value(cell1, cell2):
    result = ",".join(filter(
        lambda x: x is not None, 
        (get_cell_value(cell1), get_cell_value(cell2))))
    return result if result else None

def add_page(page: dict, page_table: dict) -> None:
    """Merge a page entry into the page_table by title."""
    title = page["title"]
    # ensure an entry exists for this title
    entry = page_table.setdefault(title, {})
    # update/merge keys
    entry.update(page)

def process_archive_sheet(ws, page_table={}):
    parent_title = get_page_title_from_link(ws["D3"])
    source_type = get_cell_value(ws["C1"])
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["A1"]).replace(" ", "-"),
        "level": "archive",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": get_cell_value(ws["L1"]),
        "reference_date": get_cell_value(ws["O1"]),
        "doc_links": get_cell_value(ws["B4"]),
        "source_type": source_type,
        "parent": "",
        }, page_table)
    
    for r in range(7, ws.max_row+1):
        cell = ws[f"A{r}"]
        if str(cell.value).startswith("="):
            break
        if cell.value:
            title = get_page_title_from_link(cell)
            label = get_cell_value(cell)
            add_page({
                "title": title,
                "label": label,
                "level": "fond",
                "description": get_cell_value(ws[f"B{r}"]),
                "years": get_cell_value(ws[f"C{r}"]),
                "availability": get_cell_value(ws[f"D{r}"]),
                "source_type": source_type,
                "parent": parent_title,
                "comments": get_cell_value(ws[f"O{r}"]),
                }, page_table)
            #print(f"{title}, {description}, {date_range}, {availability}, {doc_link}")
    return page_table

def process_fond_sheet(ws, page_table={}):
    parent_title = get_page_title_from_link(ws["D3"])
    source_type = get_cell_value(ws["C1"])
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["G1"]),
        "level": "fond",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": get_cell_value(ws["L1"]),
        "reference_date": get_cell_value(ws["O1"]),
        "doc_links": get_cell_value(ws["B4"]),
        "source_type": source_type,
        }, page_table)
    
    for r in range(7, ws.max_row+1):
        cell = ws[f"A{r}"]
        if str(cell.value).startswith("="):
            break
        if cell.value:
            title = get_page_title_from_link(cell)
            label = get_cell_value(cell)
            add_page({
                "title": title,
                "label": label,
                "level": "opus",
                "description": get_cell_value(ws[f"B{r}"]),
                "years": get_cell_value(ws[f"C{r}"]),
                "availability": get_cell_value(ws[f"D{r}"]),
                "source_type": source_type,
                "parent": parent_title,
                "comments": get_cell_value(ws[f"O{r}"]),
                }, page_table)
            #print(f"{title}, {description}, {date_range}, {availability}, {doc_link}")
    return page_table

def process_opus_sheet(ws, page_table={}):
    parent_title = get_page_title_from_link(ws["D3"])
    source_type = get_cell_value(ws["C1"])
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["H1"]),
        "level": "opus",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": get_cell_value(ws["L1"]),
        "reference_date": get_cell_value(ws["O1"]),
        "doc_links": get_cell_value(ws["B4"]),
        "source_type": source_type,
        }, page_table)
    
    for r in range(7, ws.max_row+1):
        cell = ws[f"A{r}"]
        if str(cell.value).startswith("="):
            break
        if cell.value:
            title = get_page_title_from_link(cell)
            label = get_cell_value(cell)
            add_page({
                "title": title,
                "label": label,
                "level": "case",
                "description": get_cell_value(ws[f"B{r}"]),
                "years": get_cell_value(ws[f"C{r}"]),
                "source_type": source_type,
                "parent": parent_title,
                "doc_links": get_cell_link(ws[f"B{r}"]),
                "doc_type": get_cell_value(ws[f"D{r}"]),
                "content_code": get_cell_value(ws[f"E{r}"]),
                "process_code": get_cell_value(ws[f"F{r}"]),
                "comments": get_cell_value(ws[f"G{r}"]),
                "processor": combine_cell_value(ws[f"I{r}"], ws[f"L{r}"]),
                "pages_processed": get_cell_int_value(ws[f"J{r}"]) + get_cell_int_value(ws[f"M{r}"]),
                "availability": "linked" if get_cell_link(ws[f"A{r}"]) is not None else "unlinked",
                }, page_table)
            #print(f"{title}, {description}, {date_range}, {availability}, {doc_link}")
    return page_table

# ------------------------------------------------------------

# Nocodb Client

# FIXME: make this less hardcoded
def _get_table_id(table_name):
    return _TABLE_ID[table_name.lower()]

def _get_field_id(field_name):
    return _FIELD_ID[field_name.lower()]

_TABLE_KEY_FIELD = {
    "pages": "title",
    "documents": "link",
}

_TABLE_FIELDS = {
    "pages": {
        "title",
        "description",
        "years",
        "source_type",
        "availability",
        "label",
        "level",
        "comments",
        },
    "documents": {
        "title",
        "link",
        "doc_type",
        "content_code",
        "process_code",
        "pages_processed",
        "processor",
    },
}

class NocoDBClient:
    def __init__(self, base_id=_BASE_ID):
        self._base_id = base_id
        self._session = requests.Session()
        self._session.headers.update({
            "xc-token": _NOCODB_API_TOKEN,
            "Content-Type": "application/json",
            })
        self._init_id_map()

    def _init_id_map(self):
        self._id_map = {}
        for table_name in ("pages", "documents"):
            records = self.list_records(table_name)
            key_field = _TABLE_KEY_FIELD[table_name]
            _logger.info(f"{table_name} loaded ({len(records)} records)")
            self._id_map[table_name] = { rec[key_field]: rec["Id"] for rec in records }

    def _id(self, table_name, key):
        return self._id_map[table_name].get(key)

    def _known(self, table_name, key):
        return key in self._id_map[table_name]

    def _iso_date_or_none(self, s):
        """Accepts '21 Aug 2025', '1905-1912', etc.; returns ISO date if it looks like a date, else None."""
        if not s:
            return None
        s = s.strip()
        # Try flexible day-mon-year like '21 Aug 2025'
        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except Exception:
                pass
        # not a single date; keep original (e.g., year range)
        return None

    def _records_url(self, table_name):
        return f"{_NOCODB_V2_API_ROOT}/tables/{_get_table_id(table_name)}/records"

    def _get(self, url, params=None):
        sleep(_NOCODB_API_DELAY)
        return self._session.get(url, params=params)

    def _post(self, url, json=None):
        sleep(_NOCODB_API_DELAY)
        return self._session.post(url, json=json)

    def _patch(self, url, json=None):
        sleep(_NOCODB_API_DELAY)
        return self._session.patch(url, json=json)

    def _normalize_payload(self, table_name, payload):
        # extract fields from payload relevant to this table
        out = {field: payload.get(field) for field in _TABLE_FIELDS[table_name]}

        # Format dates if present
        cd = self._iso_date_or_none(payload.get("change_date"))
        rd = self._iso_date_or_none(payload.get("reference_date"))
        if cd: out["change_date"] = cd
        if rd: out["reference_date"] = rd

        # drop empty entries
        return {k: v for k, v in out.items() if v not in (None, "")}

    def list_records(self, table_name):
        result = []
        offset = 0
        url = self._records_url(table_name)
        while True:
            params = {"offset": offset}
            #_logger.info(f"***** offset={offset}")
            response = self._get(url, params=params)
            response.raise_for_status()
            response = response.json()
            #_logger.info(response)
            payload = response.get("list", [])
            result.extend(payload)
            offset += len(payload)
            if not payload or response["pageInfo"]["isLastPage"]:
                return result

    def list_links(self, table_name, field_name, record_id):
        url = f"{_NOCODB_V2_API_ROOT}/tables/{_get_table_id(table_name)}"
        url += f"/links/{_get_field_id(field_name)}"
        url += f"/records/{record_id}"
        response = self._get(url)
        response.raise_for_status()
        return response.json()

    def create_link(self, table_name, field_name, record_id, link_target_ids):
        url = f"{_NOCODB_V2_API_ROOT}/tables/{_get_table_id(table_name)}"
        url += f"/links/{_get_field_id(field_name)}"
        url += f"/records/{record_id}"
        if isinstance(link_target_ids, (list, tuple)):
            payload = [{"Id": value} for value in link_target_ids]
        else:
            payload = [{"Id": link_target_ids}]
        #_logger.info(f"payload = {payload}")
        response = self._post(url, json=payload)
        response.raise_for_status()
        return response.json()

    def create_record(self, table_name, payload):
        payload = self._normalize_payload(table_name, payload)
        field_name = _TABLE_KEY_FIELD[table_name]
        if not payload.get(field_name):
            raise ValueError(f"Record must have {field_name} field")
        if self._known(table_name, payload.get(field_name)):
            raise ValueError(f"Cannot create record with duplicate {field_name}: {payload[field_name]}") 
        url = self._records_url(table_name)
        #_logger.info(f"post: {payload}")
        response = self._post(url, json=payload)
        response.raise_for_status()
        record_id = response.json().get("Id")
        self._id_map[table_name][payload[field_name]] = record_id
        return record_id

    def update_record(self, table_name, payload):
        payload = self._normalize_payload(table_name, payload)
        field_name = _TABLE_KEY_FIELD[table_name]
        if not payload.get(field_name):
            raise ValueError(f"Record must have {field_name}")
        if not self._known(table_name, payload.get(field_name)):
            raise ValueError(f"Cannot update record with unknown {field_name}: {payload[field_name]}") 
        url = self._records_url(table_name)
        payload["Id"] = self._id_map[table_name][payload[field_name]]
        #_logger.info(f"patch: {payload}")
        response = self._patch(url, json=payload)
        response.raise_for_status()
        return response.json()["Id"]

    def upsert_pages(self, page_data):
        if isinstance(page_data, dict):
            page_data = list(page_data.values())
        result = []
        for page in page_data:
            title = page.get("title")
            _logger.info(f"Upsert: {title}")

            # upload the page record
            if self._known("pages", title):
                result.append(self.update_record("pages", page))
            else:
                result.append(self.create_record("pages", page))

            # link to parent
            parent_title = page.get("parent")
            if parent_title:
                # ensure parent exists
                if not self._known("pages", parent_title):
                    self.create_record("pages", {"title": parent_title})
                self.create_link(
                    "pages", 
                    "parent", 
                    self._id("pages", title), 
                    self._id("pages", parent_title))

            # check for doc links
            doc_links = page.get("doc_links")
            if doc_links:
                if isinstance(doc_links, str):
                    doc_links = [doc_links]
                if not isinstance(doc_links, (list, tuple)):
                    raise TypeError("doc_links must be str, list or tuple")
                doc_fields = _TABLE_FIELDS["documents"]
                for doc_link in doc_links:
                    _logger.info(f"Adding doc link for {title}: {doc_link}")
                    doc_payload = { k: v for k, v in page.items() if k in doc_fields }
                    doc_payload["title"] = doc_link.rstrip("/").rsplit("/", 1)[-1]
                    doc_payload["link"] = doc_link
                    _logger.info(f"doc title: {doc_payload['title']}, doc link: {doc_payload['link']}")
                    if not self._known("documents", doc_link):
                        self.create_record("documents", doc_payload)
                    else:
                        self.update_record("documents", doc_payload)
                    self.create_link(
                        "documents", 
                        "owning_page", 
                        self._id("documents", doc_link), 
                        self._id("pages", title))

        return result
