# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import requests
from copy import copy

from birddog.database import (
    Database,
    FailedIO,
    FileNotFound,
    SchemaError,
    InvalidFieldName,
    InvalidFieldValue,
    InvalidRecordId,
    InvalidSourceId,
    InvalidTableName,
    InvalidTargetId,
    MissingKey,
    )

#_NOCODB_V3_API_ROOT     = "https://app.nocodb.com/api/v3"
_NOCODB_V2_API_ROOT     = "https://app.nocodb.com/api/v2"
_NOCODB_API_DELAY       = .25
_NOCODB_BATCH_SIZE      = 20
_NOCODB_BASE_ID         = "pljzqjmv8a5nvku"

# ----------------------------------------------------------------------
# NocoDB API endpoints

def _records_url(table_id):
    return f"{_NOCODB_V2_API_ROOT}/tables/{table_id}/records"

def _list_tables_url():
    return f"{_NOCODB_V2_API_ROOT}/meta/bases/{_NOCODB_BASE_ID}/tables"

def _table_info_url(table_id):
    return f"{_NOCODB_V2_API_ROOT}/meta/tables/{table_id}" 

def _links_url(table_id, link_field_id, record_id):
    return f"{_NOCODB_V2_API_ROOT}/tables/{table_id}/links/{link_field_id}/records/{record_id}"

# ----------------------------------------------------------------------
# Data validation, normalization, and encoding

def _escape_key_value(value: str) -> str:
    # escape backslashes and double quotes
    return value.replace("\\", "\\\\").replace('"', '\\"')

def _validate_single_select(value, spec):
    return value in spec["values"]

def _validate_multi_select(value, spec):
    return isinstance(value, (list, tuple)) and all(v in spec["values"] for v in value)

_field_validator = {
    "text": lambda value, spec: True,
    "date": lambda value, spec: True,
    "user": lambda value, spec: True,
    "url": lambda value, spec:  True,
    "bool": lambda value, spec: isinstance(value, bool),
    "number": lambda value, spec: isinstance(value, (int, float)),
    "single_select": _validate_single_select,
    "multi_select": _validate_multi_select,
}

def _validate_record(table_schema, record, other_allowed_fields=None):
    if not table_schema["key"] in record:
        raise MissingKey(table_schema["key"])
    for key, value in record.items():
        if key not in table_schema["fields"]:
            if other_allowed_fields and key in other_allowed_fields:
                continue
            raise InvalidFieldName(key)
        field_spec = table_schema["fields"][key]
        validator = _field_validator.get(field_spec["type"])
        if not validator:
            raise SchemaError(f"Unsupported type '{field_spec['type']}' for field '{key}'")
        if value is not None and not validator(value, field_spec):
            raise InvalidFieldValue(f"{key}: {value}")
    return True

_MULTI_SPLIT_RE = re.compile(r"[,/]+")

def _normalize_multiselect(key, value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        value = [t.strip() for t in _MULTI_SPLIT_RE.split(value)]
        return value
    raise InvalidFieldValue(f"{key}: {value}")

_field_normalizer = {
    "multi_select": _normalize_multiselect,
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

def _encode_multiselect(key, value):
    return ",".join(value) if value else None

_field_encoder = {
    "multi_select": _encode_multiselect,
}

def _encode_record(table_schema, record):
    # prepare record for write. only include fields that are explicitly in the schema
    key_field = table_schema.get("key")
    if not key_field:
        raise SchemaError("Table schema missing 'key'")
    if key_field not in record or record[key_field] in (None, ""):
        raise MissingKey(key_field)
    encoded = {}
    for key, value in record.items():
        field_spec = table_schema["fields"].get(key)
        if field_spec:
            field_type = field_spec.get("type")
            if field_type:
                validator = _field_validator.get(field_type)
                if not validator:
                    raise SchemaError(f"Unsupported type '{field_type}' for field '{key}'")
                if value is not None and not validator(value, field_spec):
                    raise InvalidFieldValue(f"{key}: {value}")
                encode = _field_encoder.get(field_type)
                if encode:
                    value = encode(key, value)
                encoded[key] = value
    return encoded

# ----------------------------------------------------------------------

class NocoDBDatabase:
    def __init__(self, api_token=None):
        if not api_token:
            api_token = os.environ["NOCODB_API_TOKEN"]
        self._session = requests.Session()
        self._session.headers.update(
            {
                "xc-token": api_token,
                "Content-Type": "application/json",
            }
        )
        # load the ids for all the tables in the database
        self._table_id_map = self._get_table_id_map()
        # create placeholder for field ids that are loaded lazily when
        # a table is first accessed
        self._field_id_map = {}
        # initialize _schema member so that _load_schema can do a scan()
        self._schema = None
        # load the actual schema - requires that the tables 
        # "Schema" and "Schema Values" exist
        self._schema = self._load_schema()

    def _fetch(self, url, params=None, json=None, method="GET"):
        time.sleep(_NOCODB_API_DELAY)
        try:
            if method == "GET":
                resp = self._session.get(url, params=params)
            elif method == "POST":
                resp = self._session.post(url, json=json)
            elif method == "PATCH":
                resp = self._session.patch(url, json=json)
            elif method == "DELETE":
                resp = self._session.delete(url, json=json)
            else:
                raise ValueError(f"_fetch: unrecognized method: {method}")
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as err:
            # Covers connection errors, timeouts, HTTP errors, etc.
            raise FailedIO(err) from err

    def _get_table_id_map(self):
        table_info = self._fetch(_list_tables_url())
        return { 
            item.get("title"): item.get("id")
            for item in table_info.get("list")
            if item["type"] == "table"
        }

    def _get_table_info(self, table_name):
        self._validate_table_name(table_name)
        return self._fetch(_table_info_url(self._table_id_map.get(table_name)))

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

    def _key_field_name(self, table_name):
        return self._schema[table_name]["key"]
    
    def _key_field_id(self, table_name):
        return self._field_id(table_name, self._key_field_name(table_name))

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
            if record["key_field"]:
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

    def validate_records(self, table_name, records):
        self._validate_table_name(table_name)
        if not isinstance(records, (list, tuple)):
            raise TypeError("records must be a sequence of dicts")
        if not all([isinstance(record, dict) for record in records]):
            raise TypeError("records must be a sequence of dicts")
        table_schema = self._schema[table_name]
        field_info = self._get_field_map(table_name)
        for record in records:
            _validate_record(table_schema, record, other_allowed_fields=set(field_info.keys()))
                
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

    def scan(self, table_name, limit=100, cursor=None):
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

        url = _records_url(table_id)
        params = {"offset": offset, "limit": limit}

        data = self._fetch(url, params=params)
        records = data.get("list", []) or []
        page_info = data.get("pageInfo") or {}
        is_last = bool(page_info.get("isLastPage")) or not records

        if is_last:
            next_cursor = None
        else:
            next_cursor = str(offset + len(records))

        self.normalize_records(table_name, records)
        return records, next_cursor

    def scan_all(self, table_name):
        records, cursor = self.scan(table_name)
        while cursor:
            more, cursor = self.scan(table_name, cursor=cursor)
            records.extend(more)
        return records

    def lookup(self, table_name, key_set):
        """
        Look up record_id(s) by the table’s key field.

        Args:
            table:
                Table name.

            key_set:
                A single key value (str) or a set of key values.

        Returns:
            - If key_set is a string: record_id (str) if found, else None.
            - If key_set is a set: dict of key:record_id values for all matching
              keys in the table.

        Required semantics:
            - Not-found is NOT an error; missing keys are omitted in the returned dict.

        Errors:
            - InvalidTableName
            - FailedIO
            - ValueError: key_set is not a string or set of strings
        """
        table_id = self._table_id(table_name)
        key_field_name = self._key_field_name(table_name)
        key_field_id = self._field_id(table_name, key_field_name)
        id_field_id = self._field_id(table_name, "Id")
        key_set, singleton = self._validate_key_set(table_name, key_set)
        result = dict()
        missing = [key for key in key_set]
        if missing:
            url = _records_url(table_id)

            for i in range(0, len(missing), _NOCODB_BATCH_SIZE):
                batch = missing[i:i + _NOCODB_BATCH_SIZE]

                clauses = [
                    f"({key_field_id},eq,{_escape_key_value(key)})"
                    for key in batch
                ]

                params = {
                    "fields": f"{id_field_id},{key_field_id}",
                    "where": "~or".join(clauses),
                }

                data = self._fetch(url, params=params)

                for item in data.get("list", []):
                    record_id = item["Id"]
                    key_value = item[key_field_name]
                    result[key_value] = record_id

        if singleton:
            return list(result.values())[0] if result else None
        return result

    def read(self, table_name, record_id):
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

        url = _records_url(table_id)
        if not isinstance(record_id, (list, tuple)):
            record_id = [record_id]
            singleton = True
        else:
            singleton = False
            
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

            data = self._fetch(url, params=params)

            for item in data.get("list", []):
                result[item["Id"]] = item
        result = [result.get(i, {}) for i in record_id]
        self.normalize_records(table_name, result)

        if singleton:
            return result[0]
        return result

    def write(self, table_name, records):
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
        key_field_name = self._key_field_name(table_name)
        if isinstance(records, dict):
            records = [ records ]
            singleton = True
        elif not isinstance(records, (list, tuple)):
            raise TypeError("records must be a single dict or sequence of dicts")
        elif not all([isinstance(record, dict) for record in records]):
            raise TypeError("records must be a single dict or sequence of dicts")
        else:
            singleton = False
        
        records = self.encode_records(table_name, records)

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

        url = _records_url(table_id)
        for i in range(0, len(known_records), _NOCODB_BATCH_SIZE):
            batch = known_records[i:i + _NOCODB_BATCH_SIZE]
            data = self._fetch(url, json=batch, method="PATCH")
        for i in range(0, len(unknown_records), _NOCODB_BATCH_SIZE):
            batch = unknown_records[i:i + _NOCODB_BATCH_SIZE]
            data = self._fetch(url, json=batch, method="POST")
            for j, item in enumerate(data):
                unknown_records[i+j]["Id"] = item["Id"]

        result = [record_dict[record[key_field_name]]["Id"] for record in records]
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
        url = _records_url(table_id)
        if not isinstance(record_id, (list, tuple)):
            record_id = [record_id]
        result = dict()
        count = 0
        for i in range(0, len(record_id), _NOCODB_BATCH_SIZE):
            batch = [{"Id": i} for i in record_id[i:i + _NOCODB_BATCH_SIZE]]
            data = self._fetch(url, json=batch, method="DELETE")
            count += len(data)
        return count

    def _edit_link(self, table_name, link_field, source_record, target_records, method):
        table_id = self._table_id(table_name)   
        link_field_id = self._field_id(table_name, link_field)
        url = _links_url(table_id, link_field_id, source_record)
        if isinstance(target_records, (list, tuple)):
            payload = [{"Id": value} for value in target_records]
        else:
            payload = [{"Id": target_records}]
        self._fetch(url, json=payload, method=method)

    def create_link(self, table_name, link_field, source_record, target_records):
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

    def delete_link(self, table_name, link_field, source_record, target_records):
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
        url = _links_url(table_id, link_field_id, source_record)
        result = []
        params = { "offset": 0}
        while True:
            response = self._fetch(url, params=params, method="GET")
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
