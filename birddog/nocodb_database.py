# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import json
import mimetypes
import os
import time
import requests
from copy import copy, deepcopy

from birddog.database import (
    Database,
    FailedIO,
    FileNotFound,
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
#_NOCODB_API_TOKEN       = os.environ["NOCODB_API_TOKEN"]
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
# Data validation

def _validate_single_select(value, spec):
    return value in spec["values"]

def _validate_multi_select(value, spec):
    return all(v in spec["values"] for v in value.split(","))

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
        if value is not None and not _field_validator[field_spec["type"]](value, field_spec):
            raise InvalidFieldValue(f"{key}: {value}")
    return True

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
        self._table_id_map = self._get_table_id_map()
        self._field_id_map = {}
        self._schema = self._load_schema()
        self._record_id_cache = {}

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
        except:
            raise InvalidFieldName(f"{table_name}.{field_name}")

    def _key_field_name(self, table_name):
        return self._schema[table_name]["key"]
    
    def _key_field_id(self, table_name):
        return self._field_id(table_name, self._key_field_name(table_name))

    def _load_schema(self):
        schema = dict() 
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
        table_schema = self._schema[table_name]
        field_info = self._get_field_map(table_name)
        for record in records:
            _validate_record(table_schema, record, other_allowed_fields=set(field_info.keys()))

    def scan(self, table_name, limit=100, cursor=None):
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
        id_cache = self._record_id_cache.get(table_name, dict())
        result = dict()
        missing = []
        for key in key_set:
            if key in id_cache:
                result[key] = id_cache[key]
            else:
                missing.append(key)
        if missing:
            url = _records_url(table_id)

            for i in range(0, len(missing), _NOCODB_BATCH_SIZE):
                batch = missing[i:i + _NOCODB_BATCH_SIZE]

                clauses = [
                    f"({key_field_id},eq,{key})"
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
                    #id_cache[key_value] = record_id

            #self._record_id_cache[table_name] = id_cache

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
        result = [result.get(i) for i in record_id]
        if singleton:
            return result[0]
        return result

    def write(self, table_name, records):
        """
        Write records to table.

        Args:

        Returns:

        Semantics:

        Errors:

        """
        table_id = self._table_id(table_name)
        key_field_name = self._key_field_name(table_name)
        key_field_id = self._field_id(table_name, key_field_name)
        id_field_id = self._field_id(table_name, "Id")

        if isinstance(records, dict):
            records = [ records ]
            singleton = True
        elif not isinstance(records, (list, tuple)):
            raise TypeError("records must be a single dict or sequence of dicts")
        else:
            singleton = False
        
        self.validate_records(table_name, records)

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
        table_id = self._table_id(table_name)        
        url = _records_url(table_id)
        if not isinstance(record_id, (list, tuple)):
            record_id = [record_id]
            singleton = True
        else:
            singleton = False
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
        self._edit_link(
            table_name, link_field, source_record, target_records,
            "POST")

    def delete_link(self, table_name, link_field, source_record, target_records):
        self._edit_link(
            table_name, link_field, source_record, target_records,
            "DELETE")

    def get_links(self, table_name, link_field, source_record):
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
