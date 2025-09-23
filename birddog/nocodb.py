# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

import os
import re
import requests
from datetime import datetime
from typing import Any, Dict, Optional

from birddog.store import get_key_value_store
from birddog.logging import get_logger
_logger = get_logger()

_NOCODB_V3_API_ROOT = "https://app.nocodb.com/api/v3"
_NOCODB_V2_API_ROOT = "https://app.nocodb.com/api/v2"
_NOCODB_API_TOKEN    = os.environ["NOCODB_API_TOKEN"]

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

class NocoDBClient:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "xc-token": NOCODB_API_TOKEN,
            "Content-Type": "application/json",
            })

    def _iso_date_or_none(s: Optional[str]) -> Optional[str]:
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

    def _normalize_payload(d):
        """Map incoming keys to your table's columns."""
        out = {
            "title": d.get("title"),
            "label": d.get("label"),
            "level": d.get("level"),
            "description": d.get("description"),
            "years": d.get("years"),
            "availability": d.get("availability"),
            "comments": d.get("comments"),
            "doc_links": d.get("doc_links"),
            "source_type": d.get("source_type"),  # if you created this column
        }
        # Optional dates
        cd = _iso_date_or_none(d.get("change_date"))
        rd = _iso_date_or_none(d.get("reference_date"))
        if cd: out["change_date"] = cd
        if rd: out["reference_date"] = rd
        # leave parent for second pass (we need IDs)
        return {k: v for k, v in out.items() if v not in (None, "")}

def get_table_id(project_slug: str, table_name: str) -> str:
    # List tables in project; pick the one with matching title/name
    r = session.get(f"{NOCODB_URL}/api/v2/projects/{project_slug}/tables")
    r.raise_for_status()
    for t in r.json():
        if t.get("title") == table_name or t.get("table_name") == table_name:
            return t["id"]
    raise RuntimeError(f"Table '{table_name}' not found in project '{project_slug}'")

def find_by_title(table_id: str, title: str) -> Optional[Dict[str, Any]]:
    # Use a where filter: (title,eq,<value>); URL-encode handled by requests params
    params = {"where": f"(title,eq,{title})", "limit": 1}
    r = session.get(f"{NOCODB_URL}/api/v2/tables/{table_id}/records", params=params)
    r.raise_for_status()
    rows = r.json().get("list", [])
    return rows[0] if rows else None

def create_record(table_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = session.post(f"{NOCODB_URL}/api/v2/tables/{table_id}/records", json=payload)
    r.raise_for_status()
    return r.json()

def update_record(table_id: str, row_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = session.patch(f"{NOCODB_URL}/api/v2/tables/{table_id}/records/{row_id}", json=payload)
    r.raise_for_status()
    return r.json()

def ensure_row(table_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert on title."""
    title = payload["title"]
    existing = find_by_title(table_id, title)
    if existing:
        row_id = existing["Id"] if "Id" in existing else existing.get("id") or existing.get("row_id")
        return update_record(table_id, row_id, payload)
    else:
        return create_record(table_id, payload)

def upsert_table_data(data, project_slug=PROJECT_SLUG, table_name="Pages"):
    table_id = get_table_id(project_slug, table_name)

    # 1) Create/update all rows without parent links
    title_to_rowid: Dict[str, Any] = {}
    for key, raw in data.items():
        payload = _normalize_payload(raw)
        if not payload.get("title"):
            payload["title"] = key  # fallback
        rec = ensure_row(table_id, payload)
        row_id = rec.get("Id") or rec.get("id") or rec.get("row_id")
        title_to_rowid[payload["title"]] = row_id

    # 2) Wire up parent links (LinkToAnotherRecord expects list of ids)
    for key, raw in data.items():
        parent_title = (raw.get("parent") or "").strip()
        if not parent_title:
            continue
        child_row = find_by_title(table_id, raw["title"])
        parent_row_id = title_to_rowid.get(parent_title)
        if not child_row or not parent_row_id:
            continue
        child_row_id = child_row.get("Id") or child_row.get("id") or child_row.get("row_id")
        # If your column is named something else, change 'parent' below
        update_record(table_id, child_row_id, {"parent": [parent_row_id]})

    _logger.info(f"Loaded {len(title_to_rowid)} rows into '{table_name}' and linked parents.")

