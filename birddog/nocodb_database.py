# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed

import mimetypes
import os
import re
import threading
import time
import requests
from urllib.parse import urlparse, parse_qs
from copy import copy
from datetime import datetime

from birddog.abstract_database import (
    Database,
    FailedIO,
    SchemaError,
    InvalidFieldName,
    InvalidFieldValue,
    InvalidRecordId,
    InvalidTableName,
    InvalidViewName,
    MissingKey,
    )
from birddog.fetch import fetch_url, make_session
from birddog.utility import json_size
from birddog.timer import FunctionTimer

from birddog.log import get_logger, LogService
_logger = get_logger()
timer = FunctionTimer()

_NOCODB_RUN_LOCAL       = os.environ.get("BIRDDOG_USE_LOCAL_NOCODB")
_NOCODB_RUN_HOSTED      = os.environ.get("BIRDDOG_USE_CLOUD_NOCODB")

if _NOCODB_RUN_LOCAL:
    # use locally running nocodb
    _NOCODB_BASE_ID     = "p79fvr9cjqgpv5n"
    _NOCODB_API_TOKEN   = os.environ["NOCODB_API_TOKEN_LOCAL"]
    _NOCODB_HOST        = "http://localhost:8080"
    _NOCODB_API_DELAY   = .01
    _NOCODB_BATCH_SIZE  = 100
    _NOCODB_EDIT_LINK_BATCH_SIZE  = 200
    _logger.info(f"Using local nocodb api: {_NOCODB_HOST}")
elif _NOCODB_RUN_HOSTED:
    # use hosted nocodb on nocodb.com
    _NOCODB_BASE_ID     = os.environ["BIRDDOG_CLOUD_BASE_ID"]
    _NOCODB_API_TOKEN   = os.environ["BIRDDOG_CLOUD_NOCODB_API_TOKEN"]
    _NOCODB_HOST        = "https://app.nocodb.com"
    _NOCODB_API_DELAY   = .25
    _NOCODB_BATCH_SIZE  = 100
    _NOCODB_EDIT_LINK_BATCH_SIZE  = 200
    _logger.info(f"Using cloud hosted nocodb api: {_NOCODB_HOST}")
else:
    # use self-managed nocodb on aws
    _NOCODB_BASE_ID     = os.environ["BIRDDOG_AWS_BASE_ID"]
    _NOCODB_API_TOKEN   = os.environ["BIRDDOG_AWS_NOCODB_API_TOKEN"]
    _NOCODB_HOST        = os.environ["BIRDDOG_AWS_NOCODB_HOST"]
    _NOCODB_API_DELAY   = .1
    _NOCODB_BATCH_SIZE  = 100
    _NOCODB_EDIT_LINK_BATCH_SIZE  = 200
    _logger.info(f"Using aws nocodb api: {_NOCODB_HOST}")

_WRITE_CHUNK_SIZE               = 500       # max records per reservation window
_WRITE_RESERVATION_INTERVAL     = 30        # seconds a write reservation is held
_WRITE_RESERVATION_TOUCH_INTERVAL = 10       # seconds between reservation renewals while a write is in flight
_WRITE_WAIT_SLEEP               = 0.1       # seconds to sleep before retrying rejected keys

# ----------------------------------------------------------------------
# Data normalization, and encoding

def _iso_date(s):
    """
    Accepts:
      - '21 Aug 2025'
      - '2025-08-21'
      - '2026-02-09 19:54:45+00:00'
      - '2026-02-09T19:54:45+00:00'
    Returns the full datetime string ('YYYY-MM-DD HH:MM:SS[+TZ]') when a time
    component is present, or 'YYYY-MM-DD 00:00:00' for date-only inputs.
    Raises ValueError for unrecognized formats.
    """
    if s is None:
        return None
    if not isinstance(s, str):
        raise TypeError("_iso_date: s must be string")
    s = s.strip()
    if not s:
        return None

    has_time = 'T' in s or (len(s) > 10 and s[10] == ' ')

    # 1. Try full ISO datetime (with optional timezone)
    try:
        dt = datetime.fromisoformat(s)
        if has_time:
            return dt.isoformat(sep=' ')
        else:
            return dt.date().isoformat() + " 00:00:00"
    except Exception:
        pass

    # 2. Try date-only formats
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat() + " 00:00:00"
        except Exception:
            pass

    # Not a single date (e.g. '1905-1912')
    raise ValueError(f"unrecognized date: {s}")


def _validate_single_select(value, spec):
    return value in spec["values"]

def _validate_multi_select(value, spec):
    if isinstance(value, str):
        value = value.split('/')
    return isinstance(value, (list, tuple)) and all(v in spec["values"] for v in value)

def _validate_date(value, spec):
    try:
        _iso_date(value)
        return True
    except:
        return False

_field_validator = {
    "text": lambda value, spec: True,
    "date": _validate_date,
    "user": lambda value, spec: True,
    "url": lambda value, spec:  True,
    "bool": lambda value, spec: isinstance(value, bool),
    "number": lambda value, spec: isinstance(value, (int, float)),
    "single_select": _validate_single_select,
    "multi_select": _validate_multi_select,
}

def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"Invalid int for bool: {value}")
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "t", "yes", "y", "1"):
            return True
        if v in ("false", "f", "no", "n", "0"):
            return False
        raise ValueError(f"Invalid str for bool: {value!r}")
    raise TypeError(f"Unsupported type for bool coercion: {type(value).__name__}")

_MULTI_SPLIT_RE = re.compile(r"[,/]+")

def _normalize_multiselect(key, value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        value = [t.strip() for t in _MULTI_SPLIT_RE.split(value)]
        return value
    raise InvalidFieldValue(key, value)

def _normalize_bool(key, value):
    return _coerce_bool(value)

def _normalize_date(key, value):
    try:
        return _iso_date(value)
    except ValueError:
        raise InvalidFieldValue(key, value)

_field_normalizer = {
    "multi_select": _normalize_multiselect,
    "bool": _normalize_bool,
    "date": _normalize_date,
}

def _normalize_record(table_schema, record):
    # normalize field values after read
    for key, value in copy(record).items():
        field_spec = table_schema["fields"].get(key)
        if field_spec:
            field_type = field_spec.get("type")
            if field_type:
                normalize = _field_normalizer.get(field_type)
                if normalize:
                    record[key] = normalize(key, value)

def _encode_date(value):
    return _iso_date(value)

def _encode_multiselect(value):
    if isinstance(value, str):
        value = value.split('/')
    encoded_value = ",".join(value) if value else None
    return encoded_value

_field_encoder = {
    "date": _encode_date,
    "multi_select": _encode_multiselect,
}

def _encode_record(table_schema, record):
    # prepare record for write. only include fields that are explicitly in the schema
    encoded = {}
    for field_name, value in record.items():
        field_spec = table_schema["fields"].get(field_name)
        if field_spec:
            field_type = field_spec.get("type")
            if field_type:
                validator = _field_validator.get(field_type)
                if not validator:
                    raise SchemaError(f"Unsupported type '{field_type}' for field '{field_name}'")
                if value is not None and not validator(value, field_spec):
                    name = field_name
                    if "title" in record and record["title"]:
                        name = f"{field_name} in {record['title']}"
                    raise InvalidFieldValue(name, value)
                encode = _field_encoder.get(field_type)
                if encode:
                    value = encode(value)
                encoded[field_name] = value
    return encoded

# ----------------------------------------------------------------------
# DATABASE TABLE BOOTSTRAPPING UTILITIES

# utility to create new table in db2 matching schema of existing table in db1
# (db1 and db2 can be the same or different)
# note that relational links are not created in the new table

_UIDT_SKIP_LIST = (
    "Links", 
    "ForeignKey", 
    "Formula", 
    "Lookup", 
    "LinkToAnotherRecord", 
    "Rollup")

def clone_table_schema(db1, table_name1, db2, table_name2):
    table_id1 = db1._table_id(table_name1)
    if db2._valid_table_name(table_name2):
        raise ValueError(f"cannot clone to existing table: {table_name2}")
    info = db1._fetch(db1._table_info_url(table_id1))
    columns = []
    for i, c in enumerate(info["columns"]):
        if not c["system"] and c["uidt"] not in _UIDT_SKIP_LIST:
            spec = {
                "title": c["title"],
                "description": c["description"],
                "uidt": c["uidt"],
            }
            if spec["uidt"] in ("SingleSelect", "MultiSelect"):
                spec["colOptions"] = {
                    "options": [
                        { "title": option["title"], "color": option["color"] }
                        for option in c["colOptions"]["options"]
                    ]
                }
            #_logger.info(f"spec={spec}")
            columns.append(spec)
    create_spec = {
        "title": table_name2,
        "description": info["description"],
        "columns": columns,
    }
    #_logger.info(f"{create_spec}")
    
    db2._fetch(db2._list_tables_url(), json=create_spec, method="POST")
    return create_spec

# copy records from one table to another. does not update links or attachments
# intended for table bootstrapping only

def copy_records(db1, table1, db2, table2):
    while True:
        records, cursor = db1.scan(table1)
        records = db1.encode_records(table1, records)
        db2.write(table2, records, raw=True)
        if not cursor:
            break

# patch a field name, notably the system fields created by nocodb in defining
# relational links (such as parent/child) may need to be renamed this way if
# UI doesn't allow it

def rename_field(db, field_id, new_name):
    url = f"{db._host}/api/v2/meta/columns/{field_id}"
    payload = { "title": new_name }
    db._fetch(url, json=payload, method="PATCH")

def copy_formula_field(db1, table1, field1, db2, table2, field2):
    """
    Copy a Formula field from one table to another, rewriting all field ID
    references in the compiled formula to match the destination table's field IDs.

    The destination table must already exist and have fields with names matching
    every field referenced by the source formula.

    Args:
        db1:    Source NocoDBDatabase instance.
        table1: Name of the source table.
        field1: Name of the formula field to copy from the source table.
        db2:    Destination NocoDBDatabase instance (may be the same as db1).
        table2: Name of the destination table.
        field2: Name to give the new formula field on the destination table.

    Returns:
        The NocoDB API response dict for the newly created column.

    Raises:
        InvalidTableName:  If table1 or table2 does not exist.
        InvalidFieldName:  If field1 is not found or is not a Formula field, or if
                           a referenced field name is missing from the destination table.
        FailedIO:          On any API communication failure.
    """
    # --- 1. Locate the formula field in the source table info ---
    src_info = db1._get_table_info(table1)
    src_col = next(
        (c for c in src_info["columns"] if c["title"] == field1 and c["uidt"] == "Formula"),
        None,
    )
    if src_col is None:
        formula_names = [c["title"] for c in src_info["columns"] if c["uidt"] == "Formula"]
        raise InvalidFieldName(
            f"{table1}.{field1} (not found or not a Formula; "
            f"available formula fields: {formula_names})"
        )

    col_options = src_col.get("colOptions") or {}
    formula_compiled = col_options.get("formula", "")
    formula_raw = (col_options.get("formula_raw") or "").strip()

    # --- 2. Build a reverse map: source field id -> field title ---
    src_id_to_title = {c["id"]: c["title"] for c in src_info["columns"]}

    # --- 3. Build a forward map: field title -> destination field id ---
    dest_info = db2._get_table_info(table2)
    dest_title_to_id = {c["title"]: c["id"] for c in dest_info["columns"]}

    # --- 4. Rewrite {{src_field_id}} -> {{dest_field_id}} in the compiled formula ---
    def _replace_ref(match):
        src_id = match.group(1)
        title = src_id_to_title.get(src_id)
        if title is None:
            raise InvalidFieldName(
                f"Formula in {table1}.{field1} references unknown source field id '{src_id}'"
            )
        dest_id = dest_title_to_id.get(title)
        if dest_id is None:
            raise InvalidFieldName(
                f"Destination table '{table2}' has no field named '{title}' "
                f"(required by formula in {table1}.{field1})"
            )
        return f"{{{{{dest_id}}}}}"

    rewritten_formula = re.sub(r"\{\{([^}]+)\}\}", _replace_ref, formula_compiled)

    # --- 5. POST the new column to the destination table ---
    dest_table_id = db2._table_id(table2)
    url = f"{db2._host}/api/v2/meta/tables/{dest_table_id}/columns"
    payload = {
        "title": field2,
        "uidt": "Formula",
        "formula_raw": formula_raw,   # human-readable expression, re-resolved by NocoDB
        "colOptions": {
            "formula": rewritten_formula,
        },
    }
    if src_col.get("description"):
        payload["description"] = src_col["description"]

    # invalidate the dest table info so it gets updated next time
    db2._field_id_map.pop(table2, None)

    return db2._fetch(url, json=payload, method="POST")

def list_formula_fields(db, table):
    """
    Return a list of column metadata dicts for all Formula fields in the given table.

    Args:
        db:    A NocoDBDatabase instance.
        table: Name of the table to inspect.

    Returns:
        List of column dicts (as returned by the table info API) where uidt == 'Formula'.

    Raises:
        InvalidTableName: If the table does not exist.
    """
    info = db._get_table_info(table)
    return [c["title"] for c in info["columns"] if c["uidt"] == "Formula"]

def list_lookup_fields(db, table):
    """
    Return a list of titles of all Lookup fields in the given table.

    Raises:
        InvalidTableName: If the table does not exist.
    """
    info = db._get_table_info(table)
    return [c["title"] for c in info["columns"] if c["uidt"] == "Lookup"]

def create_lookup_field(db, table_name, field_name, related_column_name, linked_table_name, linked_field_name):
    """
    Create a Lookup field on a table.

    Args:
        db:                   NocoDBDatabase instance.
        table_name:           Name of the table to add the Lookup field to.
        field_name:           Name to give the new Lookup field.
        related_column_name:  Name of the Links field on table_name that defines the relation.
        linked_table_name:    Name of the table the relation points to.
        linked_field_name:    Name of the field on the linked table to look up.

    Returns:
        The NocoDB API response dict for the newly created column.

    Raises:
        InvalidTableName: If any referenced table does not exist.
        InvalidFieldName: If any referenced field cannot be resolved.
        FailedIO:         On any API communication failure.
    """
    relation_col_id = db._field_id(table_name, related_column_name)
    lookup_col_id   = db._field_id(linked_table_name, linked_field_name)

    table_id = db._table_id(table_name)
    url = f"{db._host}/api/v2/meta/tables/{table_id}/columns"
    payload = {
        "title": field_name,
        "uidt": "Lookup",
        "fk_relation_column_id": relation_col_id,
        "fk_lookup_column_id": lookup_col_id,
    }

    db._field_id_map.pop(table_name, None)
    return db._fetch(url, json=payload, method="POST")

# ----------------------------------------------------------------------

class Reserver:
    """
    Tracks timed reservations on arbitrary hashable keys.

    reserve(keys, interval) attempts to reserve each key for `interval` seconds.
    Keys that are already actively reserved are rejected and returned as a list;
    the rest are reserved. Expired reservations are purged on every reserve() call.

    release(keys) removes reservations early (silent for unknown or expired keys).

    Thread-safe.
    """

    def __init__(self):
        self._reservations = {}     # key -> expiry (time.monotonic())
        self._lock = threading.Lock()

    def reserve(self, keys, interval):
        """
        Reserve each key in `keys` for `interval` seconds.

        Returns a list of rejected keys — those with an active reservation held
        by another caller. All non-rejected keys are now reserved by the caller.
        """
        now = time.monotonic()
        expiry = now + interval
        rejected = []

        with self._lock:
            expired = [k for k, exp in self._reservations.items() if exp <= now]
            for k in expired:
                del self._reservations[k]

            for key in keys:
                if key in self._reservations:
                    rejected.append(key)
                else:
                    self._reservations[key] = expiry

        return rejected

    def release(self, keys):
        """Release reservations for the given keys (silent if not reserved)."""
        with self._lock:
            for key in keys:
                self._reservations.pop(key, None)

    def extend(self, keys, interval):
        """
        Renew the expiry of keys currently held by the caller, for `interval` seconds
        from now. Keys not currently reserved (e.g. already released) are left alone
        rather than being re-reserved — same trust model as release().
        """
        now = time.monotonic()
        expiry = now + interval
        with self._lock:
            for key in keys:
                if key in self._reservations:
                    self._reservations[key] = expiry

# ----------------------------------------------------------------------

class NocoDBDatabase(Database):
    def __init__(self, host=_NOCODB_HOST, base_id=_NOCODB_BASE_ID, api_token=None):
        _logger.info(f"creating NocoDBDatabase(host={host}, base_id={base_id}) instance")
        self._verbose = False
        if not api_token:
            api_token = _NOCODB_API_TOKEN
        self._host = host
        self._base_id = base_id
        self._api_token = api_token

        self._session = make_session(host)
        self._session.headers.update(
            {
                "xc-token": api_token,
                "Content-Type": "application/json",
            }
        )
        self._reservers = {}
        self._reservers_lock = threading.Lock()
        self.load_schema()

    def load_schema(self):
        # load the ids for all the tables in the database
        self._table_id_map = self._get_table_id_map()
        # create placeholder for field ids and view ids that are loaded lazily as needed
        self._field_id_map = {}
        self._view_id_map = {}
        # initialize _schema member so that _load_schema_spec can do a scan()
        self._schema = None
        # load the actual schema - requires that the tables
        # "Schema" and "Schema Values" exist
        try:
            self._schema = self._load_schema_spec()
        except SchemaError as err:
            _logger.warning("No schema found for database. Running schema-less.")
            self._schema = None

    def _get_reserver(self, table_name):
        r = self._reservers.get(table_name)
        if r is None:
            with self._reservers_lock:
                r = self._reservers.get(table_name)
                if r is None:
                    _logger.info(f"creating Reserver(table_name={table_name})")
                    r = Reserver()
                    self._reservers[table_name] = r
        return r

    def _records_url(self, table_id):
        return f"{self._host}/api/v2/tables/{table_id}/records"

    def _records_url_v3(self, table_id):
        return f"{self._host}/api/v3/data/{self._base_id}/{table_id}/records"

    def _list_tables_url(self):
        return f"{self._host}/api/v2/meta/bases/{self._base_id}/tables"

    def _table_info_url(self, table_id):
        return f"{self._host}/api/v2/meta/tables/{table_id}"

    def _links_url(self, table_id, link_field_id, record_id):
        return f"{self._host}/api/v2/tables/{table_id}/links/{link_field_id}/records/{record_id}"

    def _view_info_url(self, table_id):
        return f"{self._host}/api/v2/meta/tables/{table_id}/views"

    def _fetch(self, url, params=None, json=None, method="GET", retry_read_timeout=True):
        """
        Centralized NocoDB fetch wrapper.

        Delegates HTTP mechanics (timeouts, retries, semaphore, session pooling) to fetch_url(),
        while preserving legacy behavior:
          - Log response text on 400 and 404
          - Raise FailedIO(...) on any communications / HTTP failure

        Pass retry_read_timeout=False for non-idempotent operations (POST) where a read
        timeout may mean the server already processed the request — retrying would create
        duplicate records. Connection errors are still retried since the request never arrived.
        """
        if self._verbose:
            _logger.info(f"{method}: {url}, params={params}, json={json}")

        try:
            # Use the shared utility (session => connection pooling/keep-alive)
            return fetch_url(
                url,
                params=params,
                send_json=json,
                return_json=True,
                method=method,
                session=self._session,
                headers=getattr(self._session, "headers", None),
                retry_read_timeout=retry_read_timeout,
            )

        except requests.exceptions.HTTPError as err:
            # fetch_url raises HTTPError for non-OK responses (except 404, if you kept that as RuntimeError)
            resp = getattr(err, "response", None)
            if resp is not None and resp.status_code in (400, 404):
                _logger.info(f"_fetch response ({resp.status_code}): {resp.text}")
            raise FailedIO(err) from err

        except RuntimeError as err:
            # If fetch_url maps 404 -> RuntimeError, log the message (ensure it includes body; see note below).
            msg = str(err)
            if "404" in msg:
                _logger.info(f"_fetch response (404): {msg}")
            raise FailedIO(err) from err

        except requests.exceptions.RequestException as err:
            # Covers timeouts, connection errors, etc., that fetch_url may surface directly in some paths.
            raise FailedIO(err) from err

        except Exception as err:
            # Defensive: keep the database layer error surface consistent
            raise FailedIO(err) from err

    def _get_table_id_map(self):
        table_info = self._fetch(self._list_tables_url())
        return {
            item.get("title"): item.get("id")
            for item in table_info.get("list")
            if item["type"] == "table"
        }

    def _get_table_info(self, table_name):
        self._validate_table_name(table_name)
        return self._fetch(self._table_info_url(self._table_id_map.get(table_name)))

    def _get_field_map(self, table_name):
        result = self._field_id_map.get(table_name)
        if result:
            return result
        table_info = self._get_table_info(table_name)
        result = {
            item["title"]: item["id"]
            for item in table_info.get("columns")
            }
        self._field_id_map[table_name] = result
        return result

    def _valid_table_name(self, table_name):
        return table_name in self._table_id_map

    def _validate_table_name(self, table_name):
        if not self._valid_table_name(table_name):
            raise InvalidTableName(table_name)

    def _table_id(self, table_name):
        try:
            return self._table_id_map[table_name]
        except KeyError:
            raise InvalidTableName(table_name)

    def _field_id(self, table_name, field_name):
        self._validate_table_name(table_name)
        field_info = self._get_field_map(table_name)
        try:
            return self._field_id_map[table_name][field_name]
        except KeyError:
            raise InvalidFieldName(f"{table_name}.{field_name}")

    def key_field_name(self, table_name):
        try:
            if not self._schema:
                raise SchemaError("key_field_name: no schema")
            return self._schema.get(table_name, {})["key"]
        except KeyError:
            raise SchemaError(f"Table {table_name} has no key field")

    def _key_field_id(self, table_name):
        return self._field_id(table_name, self.key_field_name(table_name))

    def _load_schema_spec(self):
        schema = dict()
        if "Schema" not in self._table_id_map:
            raise SchemaError("Missing Schema table")
        for record in self.scan_all("Schema"):
            table_spec = schema.get(record["table_name"], dict())
            field_spec = table_spec.get("fields", dict())
            field_spec[record["field_name"]] = {
                "type": record["field_type"],
                "description": record["description"]
            }
            table_spec["fields"] = field_spec
            if _coerce_bool(record.get("key_field", False)):
                table_spec["key"] = record["field_name"]
            schema[record["table_name"]] = table_spec
        if "Schema Values" not in self._table_id_map:
            raise SchemaError("Missing Schema Values table")
        for record in self.scan_all("Schema Values"):
            table_name = record["table_name"]
            if not table_name:
                continue
            table_spec = schema[table_name]
            field_spec = table_spec["fields"][record["field_name"]]
            values = field_spec.get("values", dict())
            values[record["field_value"]] = {
                "description": record["description"]
            }
            field_spec["values"] = values
        return schema

    def _encode_where_spec(self, table_name, where_spec):
        field_name, operator, value = where_spec
        field_id = self._field_id(table_name, field_name)
        operator = operator.lower()
        _VALID_OPS = ("eq", "neq", "is", "isnot", "lt", "le", "gt", "ge", "in")
        if operator not in _VALID_OPS:
            raise ValueError(f"unrecognized where operator: {operator}")
        if operator in ("is", "isnot"):
            if value is None:
                value = "null"
            elif value:
                value = "true"
            else:
                value = "false"
        elif operator == "in":
            if not isinstance(value, (list,tuple)):
                raise ValueError(f"operand for 'in' must be tuple or list")
            value = ",".join(value)
        return f"({field_id},{operator},{value})"

    def _encode_sort_spec(self, table_name, sort_spec):
        if isinstance(sort_spec, str):
            field_name = sort_spec
            ascending = True
        elif isinstance(sort_spec, (list, tuple)):
            field_name = sort_spec[0]
            ascending = bool(sort_spec[1])
        else:
            raise ValueError(f"unrecognized sort spec: {sort_spec}")
        field_id = self._field_id(table_name, field_name)
        return field_id if ascending else f"-{field_id}"

    def _encode_field_spec(self, table_name, fields):
        if isinstance(fields, str):
            fields = [ fields ]
        elif isinstance(fields, (list, tuple)):
            fields = list(fields)
        else:
            raise ValueError(f"unrecognized field spec: {fields}")
        if "Id" not in fields:
            fields.append("Id")
        for field_name in fields:
            # verify field names (will raise if invalid)
            self._field_id(table_name, field_name)
        return ",".join(fields)

    def _validate_key_set(self, table_name, key_set):
        if isinstance(key_set, str):
            key_set = { key_set }
            singleton = True
        else:
            if not isinstance(key_set, set):
                raise TypeError(f"key_set must be string or set of strings")
            if any([not isinstance(k, str) for k in key_set]):
                raise TypeError(f"key_set must be string or set of strings")
            singleton = False
        return key_set, singleton

    def _list_views(self, table_name):
        params = {"offset": 0, "limit": 100}
        url = self._view_info_url(self._table_id(table_name))
        views = {}
        while True:
            data = self._fetch(url, params=params)
            records = data.get("list", []) or []
            for record in records:
                title = record.get("title")
                view_id = record.get("id")
                if title and view_id:
                    views[title] = view_id
            page_info = data.get("pageInfo") or {}
            if not page_info or bool(page_info.get("isLastPage")) or not records:
                break
            params["offset"] += len(records)                
        return views

    def _view_id(self, table_name, view_name):
        view_ids = self._view_id_map.get(table_name)
        if not view_ids:
            # first time for this table - load the view id map
            view_ids = self._view_id_map[table_name] = self._list_views(table_name)
        view_id = view_ids.get(view_name)
        if not view_id:
            # view_name is not defined - refresh list to be sure
            view_ids = self._view_id_map[table_name] = self._list_views(table_name)
            view_id = view_ids.get(view_name)
            if not view_id:
                raise InvalidViewName(view_name)
        return view_id

    def normalize_records(self, table_name, records):
        self._validate_table_name(table_name)
        if not isinstance(records, (list, tuple)):
            raise TypeError("records must be a sequence of dicts")
        if not all([isinstance(record, dict) for record in records]):
            raise TypeError("records must be a sequence of dicts")
        if self._schema:
            table_schema = self._schema[table_name]
            for record in records:
                _normalize_record(table_schema, record)

    def encode_records(self, table_name, records):
        self._validate_table_name(table_name)
        table_schema = self._schema[table_name]
        return [_encode_record(table_schema, record) for record in records]

    def get_all_ids(self, table_name):
        table_id = self._table_id(table_name)
        offset = 0
        limit = 1000
        url = self._records_url(table_id)
        params = {"offset": offset, "limit": limit, "fields": "Id"}
        ids = []
        while True:
            data = self._fetch(url, params=params)
            records = data.get("list", []) or []
            ids.extend([rec["Id"] for rec in records])
            page_info = data.get("pageInfo") or {}
            if bool(page_info.get("isLastPage")) or not records:
                break
            params["offset"] += len(records)
        return ids

    def _extract_v3_page_cursor(self, next_url):
        """
        Pull the page number out of a v3 'next' URL. Only the page number is
        trusted from NocoDB's returned link -- pageSize, fields, where, and sort
        are re-supplied by the caller on the next scan() call rather than parsed
        out of this URL, both because pageSize is not reliably present in it and
        because the URL has been observed to accumulate stale/duplicate query
        params across pages.
        """
        qs = parse_qs(urlparse(next_url).query)
        page = qs.get("page", [None])[0]
        if page is None or not page.isdigit():
            raise InvalidRecordId(f"Unexpected v3 next cursor: {next_url}")
        return page

    def _flatten_v3_record(self, record):
        """
        Convert a v3 {"id": ..., "id_fields": {...}, "fields": {...}} record into
        a flat dict matching the v2 shape (e.g. {"Id": ..., "url": ..., ...}).
        Any field value that is a list of nested linked-record objects (each of
        the same {"id", "id_fields", "fields"} shape) is reduced to a plain list
        of linked record ids.
        """
        flat = {}
        flat.update(record.get("id_fields") or {})
        for key, value in (record.get("fields") or {}).items():
            flat[key] = self._reduce_link_value(value)
        return flat


    def _reduce_link_value(self, value):
        """
        If value is a list of dicts each matching the nested v3 linked-record
        shape ({"id": ..., ...}), collapse it to a plain list of ids. Any other
        value (scalar, attachment list, multi-select list, etc.) passes through
        unchanged.
        """
        if isinstance(value, list) and value and all(
            isinstance(item, dict) and "id" in item for item in value
        ):
            return [item["id"] for item in value]
        return value

    def scan(self, 
        table_name, 
        limit=100, 
        cursor=None, 
        where=None, 
        view_name=None, 
        sort=None, 
        fields=None, 
        raw=False,
        use_v3=False):
        """
        Page through records of a table without requiring the table key.

        Args:
            table_name:
                Logical table name (must exist in the NocoDB base).

            limit:
                Maximum number of records to return in this call. The backend may
                return fewer records (e.g., last page) but must not return more.

            cursor:
                Opaque paging token returned by a previous call to scan(), or None
                to request the first page. In this implementation the cursor is a
                stringified integer offset.

        Returns:
            (records, next_cursor)
                records:
                    List of record dicts as returned by NocoDB.
                next_cursor:
                    None if there are no more pages, otherwise an opaque token that
                    can be passed back to scan() to retrieve the next page.

        Semantics:
            - scan() is read-only and side-effect free.
            - Record ordering is backend-defined but stable across pages for a given
              traversal.
            - If cursor is invalid (non-integer for this backend), InvalidRecordId
              is raised.

        Errors:
            - InvalidTableName
            - InvalidRecordId
            - FailedIO
        """
        table_id = self._table_id(table_name)

        if use_v3:
            try:
                page = int(cursor) if cursor is not None else 1
            except (TypeError, ValueError):
                raise InvalidRecordId(f"Invalid v3 cursor value: {cursor}")
            url = self._records_url_v3(table_id)
            params = {"page": page, "pageSize": limit}
        else:
            try:
                offset = int(cursor) if cursor is not None else 0
            except (TypeError, ValueError):
                raise InvalidRecordId(f"Invalid v2 cursor value: {cursor}")
            url = self._records_url(table_id)
            params = {"offset": offset, "limit": limit}

        if where:
            params["where"] = self._encode_where_spec(table_name, where)
        if sort:
            params["sort"] = self._encode_sort_spec(table_name, sort)
        if fields:
            params["fields"] = self._encode_field_spec(table_name, fields)
        if view_name:
            params["viewId"] = self._view_id(table_name, view_name)

        with LogService("NocoDB", "scan") as log:
            data = self._fetch(url, params=params)

            if use_v3:
                raw_records = data.get("records", []) or []
                records = [self._flatten_v3_record(r) for r in raw_records]
                next_url = data.get("next")
                next_cursor = self._extract_v3_page_cursor(next_url) if next_url else None
            else:
                records = data.get("list", []) or []
                page_info = data.get("pageInfo") or {}
                is_last = bool(page_info.get("isLastPage")) or not records or len(records) < limit
                next_cursor = None if is_last else str(offset + len(records))

            if not raw:
                self.normalize_records(table_name, records)

            log.size = json_size(records)
            #_logger.info(f"scan: {table_name}(view={view_name}), {log.size}")
            return records, next_cursor

    @timer.timed
    def lookup(self, table_name, key_set, use_in_clause=True):
        """
        Look up record_id(s) by the table’s key field.

        Args:
            table_name:
                Table name.

            key_set:
                One of:
                - A single key value (str)
                - A set of key values (set[str])
                - A sequence of key values (list[str] or tuple[str])

        Returns:
            - If key_set is a string:
                record_id (str) if found, else None.

            - If key_set is a set:
                dict mapping key -> record_id for all matching keys
                present in the table. Missing keys are omitted.

            - If key_set is a sequence (list or tuple):
                list of record_id values aligned to the input order.
                For each input key:
                  - record_id if found
                  - None if the key is not found
                An empty input sequence returns an empty list.

        Required semantics:
            - Not-found is NOT an error.
            - Missing keys are omitted in dict results and yield None
              in sequence results.

        Errors:
            - InvalidTableName
            - FailedIO
            - TypeError: key_set as a sequence contains non-string elements
            - ValueError: key_set is not a valid key or collection of keys
        """

        if isinstance(key_set, (list, tuple)):
            if not all([isinstance(v, str) for v in key_set]):
                raise TypeError("key_set as sequence must a sequence of all strings")
            key_sequence = key_set
            key_set = set(key_set)
        else:
            key_sequence = None

        table_id = self._table_id(table_name)
        key_field_name = self.key_field_name(table_name)
        key_field_id = self._field_id(table_name, key_field_name)
        id_field_id = self._field_id(table_name, "Id")
        key_set, singleton = self._validate_key_set(table_name, key_set)
        result = dict()
        key_list = list(key_set)
        url = self._records_url(table_id)

        # batch size is limited by nocodb api limits
        _BATCH_SIZE_LIMIT = 1000
        _BATCH_COUNT_LIMIT = 500

        if key_list:
            # build all batches first
            batches = []
            pos = 0
            batch = []
            batch_size = 0
            while pos < len(key_list):
                item = key_list[pos]
                batch.append(item)
                pos += 1
                batch_size += len(item)
                if batch_size >= _BATCH_SIZE_LIMIT or len(batch) >= _BATCH_COUNT_LIMIT:
                    batches.append({"batch": batch, "result": {}})
                    batch = []
                    batch_size = 0
            if batch:
                batches.append({"batch": batch, "result": {}})

            def _do_batch(entry):
                batch = entry["batch"]
                # split batch into keys with commas and those without. Use 'in'
                # clause for all those without since it is faster than 'or'
                # if both, then join them together with 'or'
                if use_in_clause:
                    comma_keys = [k for k in batch if ',' in k]
                    plain_keys  = [k for k in batch if ',' not in k]
                else:
                    comma_keys = batch
                    plain_keys = []
                clauses = []
                if plain_keys:
                    clauses.append(f"({key_field_id},in,{','.join(plain_keys)})")
                if comma_keys:
                    clauses.extend(f"({key_field_id},eq,{k})" for k in comma_keys)
                params = {
                    "fields": f"{id_field_id},{key_field_id}",
                    "where": "~or".join(clauses) if len(clauses) > 1 else clauses[0],
                    "offset": 0,
                }
                while True:
                    data = self._fetch(url, params=params)
                    records = data.get("list", [])
                    for item in records:
                        entry["result"][item[key_field_name]] = item["Id"]
                    page_info = data.get("pageInfo", {})
                    if page_info.get("isLastPage") or not records:
                        return
                    params["offset"] += len(records)

            with LogService("NocoDB", "lookup", size=json_size(list(key_set))):
                _MAX_WORKERS = 5
                _max_workers = min(len(batches), _MAX_WORKERS)
                if len(batches) == 1:
                    _do_batch(batches[0])
                else:
                    with ThreadPoolExecutor(max_workers=_max_workers) as executor:
                        futures = [executor.submit(_do_batch, entry) for entry in batches]
                        for future in as_completed(futures):
                            future.result()  # propagate exceptions
                for entry in batches:
                    result.update(entry["result"])

        if singleton:
            return list(result.values())[0] if result else None
        if key_sequence is not None:
            return [result.get(key) for key in key_sequence]
        return result

    def read(self, table_name, record_id, fields=None):
        """
        Read record(s) by record_id.

        Args:
            table_name:
                Table name.

            record_id:
                A single record_id (str) or a sequence of record_ids.

        Returns:
            - If record_id is a string: dict for the record, or {} if not found.
            - If record_id is a sequence: list of dicts in the same order/length,
              using {} for not-found entries.

        Required semantics:
            - Not-found is NOT an error; return {} for that record_id.
            - Batch returns MUST preserve order and length.

        Errors:
            - InvalidTableName
            - InvalidRecordId          (syntactically invalid ID; not "not found")
            - FailedIO
        """
        table_id = self._table_id(table_name)
        id_field_id = self._field_id(table_name, "Id")

        url = self._records_url(table_id)
        if not isinstance(record_id, (list, tuple)):
            record_id = [record_id]
            singleton = True
        else:
            singleton = False

        with LogService("NocoDB", "read") as log:
            batches = [
                record_id[i:i + _NOCODB_BATCH_SIZE]
                for i in range(0, len(record_id), _NOCODB_BATCH_SIZE)
            ]

            def _do_batch(batch):
                clauses = [f"({id_field_id},eq,{rid})" for rid in batch]
                params = {"where": "~or".join(clauses), "limit": len(batch)}
                if fields:
                    params["fields"] = self._encode_field_spec(table_name, fields)
                data = self._fetch(url, params=params)
                return {item["Id"]: item for item in data.get("list", [])}

            result = {}
            _MAX_WORKERS = 5
            if len(batches) == 1:
                result = _do_batch(batches[0])
            elif batches:
                with ThreadPoolExecutor(max_workers=min(len(batches), _MAX_WORKERS)) as executor:
                    futures = [executor.submit(_do_batch, batch) for batch in batches]
                    for future in as_completed(futures):
                        result.update(future.result())

            result = [result.get(rid, {}) for rid in record_id]
            self.normalize_records(table_name, result)
            log.size = json_size(result)

        if singleton:
            return result[0]
        return result

    def _do_write_patch(self, url, batch):
        self._fetch(url, json=batch, method="PATCH")

    def _do_write_post(self, url, batch):
        data = self._fetch(url, json=batch, method="POST", retry_read_timeout=False)
        for j, item in enumerate(data):
            batch[j]["Id"] = item["Id"]

    def _run_write_batched(self, fn, records):
        batches = [records[i:i + _NOCODB_BATCH_SIZE]
                   for i in range(0, len(records), _NOCODB_BATCH_SIZE)]
        _MAX_WORKERS = 5
        if len(batches) == 1:
            fn(batches[0])
        elif batches:
            with ThreadPoolExecutor(max_workers=min(len(batches), _MAX_WORKERS)) as executor:
                futures = [executor.submit(fn, b) for b in batches]
                for future in as_completed(futures):
                    future.result()

    def _touch_reservation(self, reserver, keys, stop_event):
        """
        Periodically renew a set of key reservations while the guarded lookup/write
        is in flight. Without this, a reservation can expire mid-operation if NocoDB
        is slow (lookup() retries on read timeout can take well over the reservation
        window), letting a second writer reserve the same key and create a duplicate
        record before the first writer finishes.
        """
        while not stop_event.wait(_WRITE_RESERVATION_TOUCH_INTERVAL):
            reserver.extend(keys, _WRITE_RESERVATION_INTERVAL)

    def _write_non_unique(self, table_id, records):
        """POST all records without key-field dedup or reservation (no-key-field tables)."""
        url = self._records_url(table_id)
        with LogService("NocoDB", "write", size=json_size(records)):
            self._run_write_batched(lambda batch: self._do_write_post(url, batch), records)
        return [rec["Id"] for rec in records]

    def _write_unique(self, table_name, table_id, record_dict):
        """
        Write records keyed by the table's key field, using per-key reservations to
        prevent concurrent check-then-create races.

        Processes records in chunks of _WRITE_CHUNK_SIZE. For each chunk, a while
        loop retries any keys that were rejected (already reserved by another writer):
          1. reserve all remaining keys
          2. start a background toucher that renews those reservations every
             _WRITE_RESERVATION_TOUCH_INTERVAL seconds, so a slow/retrying lookup
             or write can't outlive the reservation and let a second writer in
          3. lookup to split existing (PATCH) vs new (POST)
          4. release reservations for existing keys immediately
          5. POST new records; release their reservations in a finally block
          6. PATCH existing records (no reservation needed)
          7. stop the toucher
          8. if rejected keys remain, sleep and repeat with them
        """
        url = self._records_url(table_id)
        reserver = self._get_reserver(table_name)
        all_keys = list(record_dict.keys())

        for chunk_start in range(0, len(all_keys), _WRITE_CHUNK_SIZE):
            chunk = all_keys[chunk_start:chunk_start + _WRITE_CHUNK_SIZE]
            remaining = chunk

            while remaining:
                rejected = reserver.reserve(remaining, _WRITE_RESERVATION_INTERVAL)
                if rejected:
                    _logger.info(f"_write_unique: key collision: {rejected}")
                reserved = [k for k in remaining if k not in set(rejected)]

                if reserved:
                    stop_touch = threading.Event()
                    toucher = threading.Thread(
                        target=self._touch_reservation,
                        args=(reserver, reserved, stop_touch),
                        name="write-reservation-toucher",
                        daemon=True,
                    )
                    toucher.start()
                    try:
                        with LogService("NocoDB", "write", size=json_size([record_dict[k] for k in reserved])):
                            known_key_map = self.lookup(table_name, set(reserved))

                            existing_keys = list(known_key_map.keys())
                            new_keys = [k for k in reserved if k not in known_key_map]

                            for key, record_id in known_key_map.items():
                                record_dict[key]["Id"] = record_id

                            existing_records = [record_dict[k] for k in existing_keys]
                            new_records = [record_dict[k] for k in new_keys]

                            reserver.release(existing_keys)

                            try:
                                self._run_write_batched(lambda batch: self._do_write_post(url, batch), new_records)
                            finally:
                                reserver.release(new_keys)

                            self._run_write_batched(lambda batch: self._do_write_patch(url, batch), existing_records)
                    finally:
                        stop_touch.set()
                        toucher.join(timeout=1)

                remaining = rejected
                if remaining:
                    time.sleep(_WRITE_WAIT_SLEEP)

        return [record_dict[key]["Id"] for key in all_keys]

    @timer.timed
    def write(self, table_name, records, raw=False):
        """
        Create or update record(s) in a table using the table's key field.

        Args:
            table_name:
                Logical table name.

            records:
                Either a single dict (single-record write) or a list/tuple of dicts
                (batch write). Each dict must include the table key field defined in
                the Schema tables (self._schema[table_name]["key"]).

                Partial update semantics:
                    - Only fields present in each record dict are written.
                    - Fields not present are left unchanged.

                Validation semantics:
                    - Field names must exist in the logical schema (Schema tables),
                      unless they are present in the NocoDB column list for the table
                      (to allow system fields such as "Id" when needed).
                    - Field values must satisfy basic type validation based on the
                      Schema field type. For select fields, values must be within the
                      allowed values defined in "Schema Values".

        Returns:
            - If records is a single dict: the record_id (NocoDB "Id") of the written record.
            - If records is a batch: list of record_ids in the same order as input.

        Semantics / atomicity:
            - All-or-none for validation errors:
                * The full input (single or batch) is validated before any network
                  calls are made. If validation fails, no write is attempted and an
                  appropriate exception is raised.
            - IO/backend failures:
                * If a network or backend error occurs while applying updates/creates,
                  FailedIO is raised and partial updates MAY have occurred.

        Errors:
            - InvalidTableName
            - MissingKey
            - InvalidFieldName
            - InvalidFieldValue
            - FailedIO
        """
        table_id = self._table_id(table_name)
        try:
            key_field_name = self.key_field_name(table_name)
        except SchemaError:
            # allow for no key field for write - just append records
            key_field_name = None
        if isinstance(records, dict):
            records = [ records ]
            singleton = True
        elif not isinstance(records, (list, tuple)):
            raise TypeError("records must be a single dict or sequence of dicts")
        elif not all([isinstance(record, dict) for record in records]):
            raise TypeError("records must be a single dict or sequence of dicts")
        else:
            singleton = False

        if not raw:
            records = self.encode_records(table_name, records)
        if key_field_name:
            record_dict = {record[key_field_name]: copy(record) for record in records}
            result = self._write_unique(table_name, table_id, record_dict)
        else:
            result = self._write_non_unique(table_id, records)
        if singleton:
            return result[0]
        return result

    def delete(self, table_name, record_id):
        """
        Delete record(s) from a table by record_id (NocoDB "Id").

        Args:
            table_name:
                Logical table name.

            record_id:
                A single record_id (str/int) or a list/tuple of record_ids.

        Returns:
            Total number of records deleted (as reported by the backend). Deleting a
            non-existent record is not treated as an error; the backend may return
            fewer deletions than requested.

        Semantics / atomicity:
            - The implementation issues one or more batch DELETE requests to NocoDB.
            - On FailedIO, partial deletions may have occurred.

        Errors:
            - InvalidTableName
            - FailedIO
        """
        table_id = self._table_id(table_name)
        url = self._records_url(table_id)
        if not isinstance(record_id, (list, tuple)):
            record_id = [record_id]
        result = dict()
        count = 0
        with LogService("NocoDB", "delete", size=json_size(record_id)):
            for i in range(0, len(record_id), _NOCODB_BATCH_SIZE):
                batch = [{"Id": i} for i in record_id[i:i + _NOCODB_BATCH_SIZE]]
                data = self._fetch(url, json=batch, method="DELETE")
                count += len(data)
        return count

    def _edit_link(self, table_name, link_field, source_record, target_records, method):
        table_id = self._table_id(table_name)
        link_field_id = self._field_id(table_name, link_field)
        url = self._links_url(table_id, link_field_id, source_record)
        with LogService("NocoDB", "edit_links", size=json_size(target_records)):
            if isinstance(target_records, (list, tuple)):
                # ensure no duplicate target ids
                target_records = list(set(target_records))
                for i in range(0, len(target_records), _NOCODB_EDIT_LINK_BATCH_SIZE):
                    batch = target_records[i:i + _NOCODB_EDIT_LINK_BATCH_SIZE]
                    payload = [{"Id": value} for value in batch]
                    self._fetch(url, json=payload, method=method)
            else:
                payload = [{"Id": target_records}]
                self._fetch(url, json=payload, method=method)

    @timer.timed
    def create_links(self, table_name, link_field, source_record, target_records):
        """
        Create relation link(s) from a source record to one or more target records.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table (must correspond to a
                NocoDB link-type column).

            source_record:
                Record_id ("Id") of the source record.

            target_records:
                Either a single target record_id or a list/tuple of target record_ids.

        Semantics:
            - Adds the specified target link(s) to the relation.
            - Should be idempotent if the backend ignores duplicates (NocoDB typically does).

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - FailedIO
        """
        self._edit_link(
            table_name, link_field, source_record, target_records,
            "POST")

    def delete_links(self, table_name, link_field, source_record, target_records):
        """
        Remove relation link(s) from a source record to one or more target records.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table.

            source_record:
                Record_id ("Id") of the source record.

            target_records:
                Either a single target record_id or a list/tuple of target record_ids.

        Semantics:
            - Removes the specified target link(s) if they exist.
            - Idempotent: if a link does not exist, no error is raised (backend behavior).

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - FailedIO
        """
        self._edit_link(
            table_name, link_field, source_record, target_records,
            "DELETE")

    def scan_links(self, table_name, link_field, source_record, limit=100, cursor=None):
        """
        Page through linked target record_ids for a relation field on a source record.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table.

            source_record:
                Record_id ("Id") of the source record.

            limit:
                Maximum number of linked record_ids to return in this call.

            cursor:
                Opaque paging token returned by a previous call to scan_links(), or
                None to request the first page. In this implementation the cursor is
                a stringified integer offset.

        Returns:
            (link_ids, next_cursor)
                link_ids:
                    List of linked target record_ids.
                next_cursor:
                    None if there are no more pages, otherwise an opaque token that
                    can be passed back to scan_links() to retrieve the next page.

        Semantics:
            - scan_links() is read-only and side-effect free.
            - If cursor is invalid (non-integer for this backend), InvalidRecordId
              is raised.
            - Supports both paged and non-paged NocoDB responses:
                * If NocoDB returns a paged payload with "list" and "pageInfo", a
                  single page of links is returned.
                * If NocoDB returns a singular object with an "Id", that single Id
                  is returned on the first page only.
            - An out-of-range offset (offset >= total linked records) is treated
              as an empty last page rather than an error, since some NocoDB
              backends (cloud) reject it with HTTP 422 ERR_INVALID_OFFSET_VALUE
              instead of returning an empty list.

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - InvalidRecordId
            - FailedIO
        """
        table_id = self._table_id(table_name)
        link_field_id = self._field_id(table_name, link_field)

        try:
            offset = int(cursor) if cursor is not None else 0
        except (TypeError, ValueError):
            raise InvalidRecordId(f"Invalid cursor value: {cursor}")

        url = self._links_url(table_id, link_field_id, source_record)
        params = {
            "offset": offset,
            "limit": limit,
        }

        with LogService("NocoDB", "scan_links") as log:
            try:
                response = self._fetch(url, params=params, method="GET")
            except FailedIO as err:
                # NocoDB Cloud rejects offset >= totalRows with HTTP 422
                # ERR_INVALID_OFFSET_VALUE instead of returning an empty page
                # (older self-hosted versions do the latter). This can happen
                # even on a cursor we computed ourselves, e.g. if NocoDB
                # reports isLastPage=False on what turns out to be an exact
                # last full page, or a record was unlinked concurrently
                # between the count and this fetch. Either way, "offset past
                # the end" means no more records -- treat it as such rather
                # than surfacing a fatal error.
                cause = err.__cause__
                resp = getattr(cause, "response", None)
                if resp is not None and resp.status_code == 422 and "ERR_INVALID_OFFSET_VALUE" in (resp.text or ""):
                    return [], None
                raise
            log.size = json_size(response)

            page_info = response.get("pageInfo")
            if page_info:
                link_ids = [item["Id"] for item in response.get("list", []) or []]
                is_last = bool(page_info.get("isLastPage")) or not link_ids or len(link_ids) < limit
                next_cursor = None if is_last else str(offset + len(link_ids))
                return link_ids, next_cursor

            # Non-paged fallback: some NocoDB responses may return a singular object.
            link_id = response.get("Id")
            if link_id:
                if offset > 0:
                    return [], None
                return [link_id], None

            return [], None


    def get_links(self, table_name, link_field, source_record):
        """
        Retrieve all linked target record_ids for a relation field on a source record.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table.

            source_record:
                Record_id ("Id") of the source record.

        Returns:
            A list of all linked target record_ids. If no links exist, returns an
            empty list.

        Semantics:
            - Eagerly retrieves all pages of links by calling scan_links().
            - For large fanout relations, callers that do not need the full link set
              should prefer scan_links().

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - FailedIO
        """
        result = []
        cursor = None

        with LogService("NocoDB", "get_links") as log:
            log.size = 0
            while True:
                page, cursor = self.scan_links(
                    table_name,
                    link_field,
                    source_record,
                    cursor=cursor,
                )
                log.size += json_size(page)
                result.extend(page)
                if not cursor:
                    return result

    def _upload_files(self, file_paths):
        """
        Upload one or more files via /api/v2/storage/upload.

        Args:
            file_paths: str or iterable of str

        Returns:
            Flat list of NocoDB file objects suitable for attachment fields.
        """
        if isinstance(file_paths, (str, os.PathLike)):
            file_paths = [file_paths]

        if not isinstance(file_paths, (list, tuple)) or not file_paths:
            raise ValueError("file_paths must be a path or non-empty list/tuple of paths")

        upload_url = f"{self._host}/api/v2/storage/upload"
        headers = {"xc-token": self._api_token}

        uploaded = []

        for file_path in file_paths:
            if not os.path.isfile(file_path):
                raise FileNotFoundError(file_path)

            basename = os.path.basename(file_path)
            name_root, ext = os.path.splitext(basename)
            safe_root = "".join(ch if ch.isascii() else "_" for ch in name_root)
            safe_name = (safe_root or "upload") + ext

            mime = mimetypes.guess_type(basename)[0] or "application/octet-stream"

            with open(file_path, "rb") as f:
                files = {"file": (safe_name, f, mime)}
                resp = requests.post(
                    upload_url,
                    headers=headers,
                    files=files,
                    timeout=30,
                )

            if resp.status_code != 200:
                raise RuntimeError(f"Upload failed {resp.status_code}: {resp.text}")

            data = resp.json()
            if not isinstance(data, list) or not data:
                raise RuntimeError(f"Unexpected upload response for {file_path}: {data!r}")

            uploaded.extend(data)

        return uploaded

    def set_attachments(self, table_name, record_id, attachment_field, file_paths):
        """
        Replace the contents of an attachment field with one or more files.

        Semantics:
          - Uploads all provided files
          - Replaces the attachment field entirely
          - Does NOT preserve existing attachments
        """
        table_id = self._table_id(table_name)
        uploaded = self._upload_files(file_paths)

        url = self._records_url(table_id)
        payload = [{
            "Id": record_id,
            attachment_field: uploaded,  # must be a list
        }]
        self._fetch(url, json=payload, method="PATCH")

    def clear_attachments(self, table_name, record_id, attachment_field):
        """
        Clear (remove) all files from an attachment field.

        Semantics:
          - Replaces the attachment field with an empty list
          - Does NOT delete files from NocoDB storage
        """
        table_id = self._table_id(table_name)

        url = self._records_url(table_id)
        payload = [{
            "Id": record_id,
            attachment_field: [],
        }]
        self._fetch(url, json=payload, method="PATCH")

