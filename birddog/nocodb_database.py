# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import mimetypes
import os
import re
import time
import requests
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
from birddog.fetch import fetch_url
from birddog.utility import json_size
from birddog.timer import FunctionTimer

from birddog.log import get_logger, LogService
_logger = get_logger()
timer = FunctionTimer()

_NOCODB_RUN_LOCAL       = os.environ.get("BIRDDOG_USE_LOCAL_NOCODB")
_NOCODB_RUN_HOSTED      = False

if _NOCODB_RUN_LOCAL:
    # use locally running nocodb
    _NOCODB_BASE_ID     = "p79fvr9cjqgpv5n"
    _NOCODB_API_TOKEN   = os.environ["NOCODB_API_TOKEN_LOCAL"]
    _NOCODB_HOST        = "http://localhost:8080"
    _NOCODB_API_DELAY   = .01
    _NOCODB_BATCH_SIZE  = 10
    _NOCODB_EDIT_LINK_BATCH_SIZE  = 200
    _logger.info(f"Using local nocodb api: {_NOCODB_HOST}")
elif _NOCODB_RUN_HOSTED:
    # use hosted nocodb on nocodb.com
    _NOCODB_BASE_ID     = "pljzqjmv8a5nvku"
    _NOCODB_API_TOKEN   = os.environ["NOCODB_API_TOKEN"]
    _NOCODB_HOST        = "https://app.nocodb.com"
    _NOCODB_API_DELAY   = .25
    _NOCODB_BATCH_SIZE  = 20
    _NOCODB_EDIT_LINK_BATCH_SIZE  = 200
    _logger.info(f"Using hosted nocodb api: {_NOCODB_HOST}")
else:
    # use self-managed nocodb on aws
    _NOCODB_BASE_ID     = os.environ["BIRDDOG_AWS_BASE_ID"]
    _NOCODB_API_TOKEN   = os.environ["BIRDDOG_AWS_NOCODB_API_TOKEN"]
    _NOCODB_HOST        = os.environ["BIRDDOG_AWS_NOCODB_HOST"]
    _NOCODB_API_DELAY   = .1
    _NOCODB_BATCH_SIZE  = 10
    _NOCODB_EDIT_LINK_BATCH_SIZE  = 200
    _logger.info(f"Using aws nocodb api: {_NOCODB_HOST}")

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
                    raise InvalidFieldValue(field_name, value)
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

def clone_table_schema(db1, table_name1, db2, table_name2):
    table_id1 = db1._table_id(table_name1)
    if db2._valid_table_name(table_name2):
        raise ValueError(f"cannot clone to existing table: {table_name2}")
    info = db1._fetch(db1._table_info_url(table_id1))
    columns = []
    for i, c in enumerate(info["columns"]):
        if not c["system"] and c["uidt"] not in ("Links", "ForeignKey", "Formula", "Lookup"):
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

# ----------------------------------------------------------------------

class NocoDBDatabase(Database):
    def __init__(self, host=_NOCODB_HOST, base_id=_NOCODB_BASE_ID, api_token=None):
        self._verbose = False
        if not api_token:
            api_token = _NOCODB_API_TOKEN
        self._host = host
        self._base_id = base_id
        self._api_token = api_token

        self._session = requests.Session()
        self._session.headers.update(
            {
                "xc-token": api_token,
                "Content-Type": "application/json",
            }
        )
        # load the ids for all the tables in the database
        self._table_id_map = self._get_table_id_map()
        # create placeholder for field ids and view ids that are loaded lazily as needed
        self._field_id_map = {}
        self._view_id_map = {}
        # initialize _schema member so that _load_schema can do a scan()
        self._schema = None
        # load the actual schema - requires that the tables
        # "Schema" and "Schema Values" exist
        try:
            self._schema = self._load_schema()
        except SchemaError as err:
            _logger.warning("No schema found for database. Running schema-less.")
            self._schema = None

    def _records_url(self, table_id):
        return f"{self._host}/api/v2/tables/{table_id}/records"

    def _list_tables_url(self):
        return f"{self._host}/api/v2/meta/bases/{self._base_id}/tables"

    def _table_info_url(self, table_id):
        return f"{self._host}/api/v2/meta/tables/{table_id}"

    def _links_url(self, table_id, link_field_id, record_id):
        return f"{self._host}/api/v2/tables/{table_id}/links/{link_field_id}/records/{record_id}"

    def _view_info_url(self, table_id):
        return f"{self._host}/api/v2/meta/tables/{table_id}/views"

    def _fetch(self, url, params=None, json=None, method="GET"):
        """
        Centralized NocoDB fetch wrapper.

        Delegates HTTP mechanics (timeouts, retries, semaphore, session pooling) to fetch_url(),
        while preserving legacy behavior:
          - Log response text on 400 and 404
          - Raise FailedIO(...) on any communications / HTTP failure
        """
        if self._verbose:
            _logger.info(f"{method}: {url}, params={params}, json={json}")
        time.sleep(_NOCODB_API_DELAY)

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

    def _load_schema(self):
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
        _VALID_OPS = ("eq", "neq", "is", "isnot", "lt", "le", "gt", "ge")
        if operator not in _VALID_OPS:
            raise ValueError(f"unrecognized where operator: {operator}")
        if operator in ("is", "isnot"):
            if value is None:
                value = "null"
            elif value:
                value = "true"
            else:
                value = "false"
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
        elif not isinstance(fields, (list, tuple)):
            raise ValueError(f"unrecognized field spec: {fields}")
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

    def scan(self, 
        table_name, 
        limit=100, 
        cursor=None, 
        where=None, 
        view_name=None, 
        sort=None, 
        fields=None, 
        raw=False):
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
        try:
            offset = int(cursor) if cursor is not None else 0
        except (TypeError, ValueError):
            raise InvalidRecordId(f"Invalid cursor value: {cursor}")

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
            records = data.get("list", []) or []
            page_info = data.get("pageInfo") or {}
            is_last = bool(page_info.get("isLastPage")) or not records

            if is_last:
                next_cursor = None
            else:
                next_cursor = str(offset + len(records))

            if not raw:
                self.normalize_records(table_name, records)

            log.size = json_size(records)
            return records, next_cursor

    @timer.timed
    def lookup(self, table_name, key_set):
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

        def _do_batch(batch):
            #_logger.info(f"_do_batch: {batch}")
            clauses = [
                f"({key_field_id},eq,{key})"
                for key in batch
            ]

            params = {
                "fields": f"{id_field_id},{key_field_id}",
                "where": "~or".join(clauses),
                "offset": 0,
            }

            while True:
                data = self._fetch(url, params=params)
                records = data.get("list", [])
                for item in records:
                    record_id = item["Id"]
                    key_value = item[key_field_name]
                    result[key_value] = record_id

                page_info = data.get("pageInfo", {})
                if page_info.get("isLastPage") or not records:
                    return
                params["offset"] += len(records)

        _BATCH_SIZE_LIMIT = 1000
        _BATCH_COUNT_LIMIT = 100

        #_logger.info(f"lookup: {key_list}")
        if key_list:
            with LogService("NocoDB", "lookup", size=json_size(list(key_set))):
                pos = 0
                batch = []
                batch_size = 0
                while pos < len(key_list):
                    item = key_list[pos]
                    batch.append(item)
                    pos += 1
                    batch_size += len(item)
                    #batch_size += len(quote(item, safe=""))
                    if batch_size >= _BATCH_SIZE_LIMIT or len(batch) >= _BATCH_COUNT_LIMIT:
                        _do_batch(batch)
                        batch = []
                        batch_size = 0
                if batch:
                    _do_batch(batch)

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
            result = dict()
            for i in range(0, len(record_id), _NOCODB_BATCH_SIZE):
                batch = record_id[i:i + _NOCODB_BATCH_SIZE]

                clauses = [
                    f"({id_field_id},eq,{i})"
                    for i in batch
                ]

                params = {
                    "where": "~or".join(clauses),
                }
                if fields:
                    params["fields"] = self._encode_field_spec(table_name, fields)
                    
                data = self._fetch(url, params=params)

                for item in data.get("list", []):
                    result[item["Id"]] = item
            result = [result.get(i, {}) for i in record_id]
            self.normalize_records(table_name, result)
            log.size = json_size(result)

        if singleton:
            return result[0]
        return result

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
            key_set = set(record_dict.keys())
            known_key_map = self.lookup(table_name, key_set)
            known_records = []
            for key, record_id in known_key_map.items():
                record = record_dict[key]
                record["Id"] = record_id
                known_records.append(record)
            unknown_keys = key_set - set(known_key_map.keys())
            unknown_records = [record_dict[key] for key in unknown_keys]
        else:
            # no check for existing records, just append
            known_records = []
            unknown_records = records

        with LogService("NocoDB", "write", size=json_size(known_records) + json_size(unknown_records)):
            url = self._records_url(table_id)
            for i in range(0, len(known_records), _NOCODB_BATCH_SIZE):
                batch = known_records[i:i + _NOCODB_BATCH_SIZE]
                self._fetch(url, json=batch, method="PATCH")
            for i in range(0, len(unknown_records), _NOCODB_BATCH_SIZE):
                batch = unknown_records[i:i + _NOCODB_BATCH_SIZE]
                data = self._fetch(url, json=batch, method="POST")
                for j, item in enumerate(data):
                    unknown_records[i+j]["Id"] = item["Id"]

            if key_field_name:
                result = [record_dict[record[key_field_name]]["Id"] for record in records]
            else:
                result = [rec["Id"] for rec in records]
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

    def get_links(self, table_name, link_field, source_record):
        """
        Retrieve the linked target record_ids for a relation field on a source record.

        Args:
            table_name:
                Logical name of the source table that owns the relation field.

            link_field:
                Name of the relation field on the source table.

            source_record:
                Record_id ("Id") of the source record.

        Returns:
            A list of linked target record_ids (strings/ints as returned by NocoDB).
            If no links exist, returns an empty list.

        Semantics:
            - Supports both paged and non-paged NocoDB responses:
                * If NocoDB returns a paged payload with "list" and "pageInfo", the
                  method iterates pages until "isLastPage" is True.
                * If NocoDB returns a singular object with an "Id", that single Id is
                  returned as a one-element list.

        Errors:
            - InvalidTableName
            - InvalidFieldName
            - FailedIO
        """
        table_id = self._table_id(table_name)
        link_field_id = self._field_id(table_name, link_field)
        url = self._links_url(table_id, link_field_id, source_record)
        result = []
        params = { "offset": 0}
        with LogService("NocoDB", "get_links") as log:
            log.size = 0
            while True:
                response = self._fetch(url, params=params, method="GET")
                log.size += json_size(response)
                page_info = response.get("pageInfo")
                if page_info:
                    # paged results
                    resp = [item["Id"] for item in response.get("list", [])]
                    result.extend(resp)
                    if page_info["isLastPage"]:
                        return result
                    # iterate to get the next page
                    params["offset"] += len(resp)
                else:
                    # not paged: singular Id returned
                    link_id = response.get("Id")
                    if link_id:
                        result.append(link_id)
                    return result
            return response

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

