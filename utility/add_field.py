#!/usr/bin/env python3
"""
Add a field to a NocoDB table from a JSON spec, together with its Schema row
and, for select fields, one Schema Values row per allowed value.

Spec file (see utility/specs/):
    {
      "table": "Documents",
      "field": "doc_type",
      "type": "multi_select",              # a Schema field_type, see UIDT below
      "description": "...",                # Schema row description
      "column_type": "LongText",           # optional, see COLUMN_TYPES below
      "values": [["Vitals", "Examples include ..."], ...]   # selects only
    }

Each step is skipped if it is already done, so an interrupted run can simply be
run again. Values missing from an existing select column's options are added,
and Schema / Schema Values descriptions that differ from the spec are updated;
values are never removed. Stop the app first: a running app holds the old schema.

Usage (from anywhere; exactly one target is required):
    venv/bin/python utility/add_field.py --local utility/specs/documents_doc_type.json
    venv/bin/python utility/add_field.py --stage utility/specs/*.json
"""

import argparse
import json
import logging
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Schema field_type -> NocoDB column type (uidt)
UIDT = {
    "single_select": "SingleSelect",
    "multi_select": "MultiSelect",
    "bool": "Checkbox",
    "text": "SingleLineText",
}
SELECT_TYPES = {"single_select", "multi_select"}
# Optional "column_type" overrides allowed per Schema field_type; the Schema row
# keeps the plain field_type (e.g. page_description is a "text" LongText column).
COLUMN_TYPES = {"text": {"SingleLineText", "LongText"}}
# Values that would be split apart: NocoDB joins multiselect values with ","
# and birddog also splits multiselect strings on "/".
FORBIDDEN_CHARS = ",/"

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Add a field and its Schema rows.")
    parser.add_argument("specs", nargs="+", help="path(s) to JSON field specs")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", dest="env", action="store_const", const="LOCAL",
                        help="local dev base")
    target.add_argument("--stage", dest="env", action="store_const", const="CLOUD_STAGE",
                        help="NocoDB Cloud staging base")
    target.add_argument("--prod", dest="env", action="store_const", const="CLOUD_PROD",
                        help="NocoDB Cloud production base")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser.parse_args(argv)

def is_select(spec):
    return spec["type"] in SELECT_TYPES

def column_uidt(spec):
    return spec.get("column_type") or UIDT[spec["type"]]

def load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    errors = []
    for key in ("table", "field", "type", "description"):
        if key not in spec:
            errors.append(f"{path}: spec is missing {key!r}")
    if spec.get("type") not in UIDT:
        errors.append(f"{path}: type must be one of {sorted(UIDT)}")
        return spec, errors
    if "column_type" in spec and spec["column_type"] not in COLUMN_TYPES.get(spec["type"], ()):
        errors.append(f"{path}: column_type for {spec['type']} must be one of "
                      f"{sorted(COLUMN_TYPES.get(spec['type'], ()))}")
    if is_select(spec):
        names = [v[0] for v in spec.get("values", [])]
        if not names:
            errors.append(f"{path}: a select field needs values")
        if len(names) != len(set(names)):
            errors.append(f"{path}: duplicate values in spec")
        for name in names:
            if not name or name != name.strip() or any(c in name for c in FORBIDDEN_CHARS):
                errors.append(f"{path}: invalid value {name!r} (no leading/trailing space, ',' or '/')")
    elif spec.get("values"):
        errors.append(f"{path}: only select fields take values")
    spec.setdefault("values", [])
    return spec, errors

def connect(env_name):
    """Open the backend for env_name (see utility/clear_table.py for why the
    environment and working directory are set up before the import)."""
    os.environ["BIRDDOG_NOCODB_ENV"] = env_name
    os.chdir(REPO_ROOT)
    from birddog.database import Database
    logging.getLogger().setLevel(logging.WARNING)
    db = Database()
    return getattr(db, "_db", db)

def get_column(db, table_name, field):
    info = db._fetch(db._table_info_url(db._table_id(table_name)))
    return next((c for c in info["columns"] if c["title"] == field), None)

def column_options(column):
    return [o["title"] for o in (column.get("colOptions") or {}).get("options", [])]

def plan(db, spec):
    table, field = spec["table"], spec["field"]
    values = [v[0] for v in spec["values"]]
    steps = []
    column = get_column(db, table, field)
    if column is None:
        steps.append(("create column", values))
    else:
        if column["uidt"] != column_uidt(spec):
            raise ValueError(f"{table}.{field} exists as {column['uidt']}, not {column_uidt(spec)}")
        missing = [v for v in values if v not in column_options(column)]
        if missing:
            steps.append(("add options", missing))
    schema = [r for r in db.scan_all("Schema")
              if r.get("table_name") == table and r.get("field_name") == field]
    if not schema:
        steps.append(("Schema row", None))
    else:
        if any(r.get("field_type") != spec["type"] for r in schema):
            raise ValueError(f"Schema row for {table}.{field} has type "
                             f"{schema[0].get('field_type')!r}, not {spec['type']!r}")
        if any(r.get("description") != spec["description"] for r in schema):
            steps.append(("Schema description", [r["Id"] for r in schema]))
    if is_select(spec):
        existing = {r.get("field_value"): r for r in db.scan_all("Schema Values")
                    if r.get("table_name") == table and r.get("field_name") == field}
        new_values = [v for v in spec["values"] if v[0] not in existing]
        if new_values:
            steps.append(("Schema Values rows", new_values))
        changed = [(existing[v]["Id"], v, desc) for v, desc in spec["values"]
                   if v in existing and existing[v].get("description") != desc]
        if changed:
            steps.append(("Schema Values descriptions", changed))
    return steps

def describe(spec, step):
    kind, data = step
    where = f"{spec['table']}.{spec['field']}"
    if kind == "create column":
        extra = f" with {len(data)} options" if is_select(spec) else ""
        return f"create {column_uidt(spec)} column {where}{extra}"
    if kind == "add options":
        return f"add options to {where}: {data}"
    if kind == "Schema row":
        return f"add Schema row for {where} ({spec['type']})"
    if kind == "Schema description":
        return f"update Schema description for {where}"
    if kind == "Schema Values descriptions":
        return f"update {len(data)} Schema Values descriptions for {where}: {[v for _, v, _ in data]}"
    return f"add {len(data)} Schema Values rows for {where}"

def apply(db, spec, step):
    kind, data = step
    table, field = spec["table"], spec["field"]
    table_id = db._table_id(table)
    if kind == "create column":
        payload = {"title": field, "column_name": field, "uidt": column_uidt(spec)}
        if is_select(spec):
            payload["colOptions"] = {
                "options": [{"title": v, "order": i + 1} for i, v in enumerate(data)]
            }
        db._fetch(f"{db._host}/api/v2/meta/tables/{table_id}/columns", json=payload, method="POST")
    elif kind == "add options":
        column = get_column(db, table, field)
        url = f"{db._host}/api/v2/meta/columns/{column['id']}"
        full = db._fetch(url)
        options = full["colOptions"]["options"]
        start = max((o.get("order") or 0 for o in options), default=0)
        options.extend({"title": v, "order": start + i + 1} for i, v in enumerate(data))
        db._fetch(url, json=full, method="PATCH")
    elif kind == "Schema row":
        db._fetch(db._records_url(db._table_id("Schema")), method="POST", json=[{
            "table_name": table, "field_name": field, "field_type": spec["type"],
            "key_field": False, "description": spec["description"],
        }])
    elif kind == "Schema description":
        db._fetch(db._records_url(db._table_id("Schema")), method="PATCH", json=[
            {"Id": rid, "description": spec["description"]} for rid in data
        ])
    elif kind == "Schema Values descriptions":
        db._fetch(db._records_url(db._table_id("Schema Values")), method="PATCH", json=[
            {"Id": rid, "description": desc} for rid, _, desc in data
        ])
    else:
        db._fetch(db._records_url(db._table_id("Schema Values")), method="POST", json=[
            {"table_name": table, "field_name": field, "field_value": v, "description": desc}
            for v, desc in data
        ])

def verify(db, spec):
    column = get_column(db, spec["table"], spec["field"])
    want = [v[0] for v in spec["values"]]
    have = column_options(column) if column else []
    missing = [v for v in want if v not in have]
    if column is None or column.get("column_name") != spec["field"] or missing:
        raise RuntimeError(f"column check failed for {spec['field']}: "
                           f"column={column and column.get('column_name')!r} missing options={missing}")

def main(argv=None):
    args = parse_args(argv)
    specs, errors = [], []
    for path in args.specs:
        spec, spec_errors = load_spec(path)
        specs.append(spec)
        errors.extend(spec_errors)
    if errors:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    sys.path.insert(0, REPO_ROOT)
    from birddog.abstract_database import ConfigError, FailedIO, InvalidTableName

    try:
        db = connect(args.env)
        print(f"Target: {args.env}")
        todo = []
        for spec in specs:
            try:
                todo.extend((spec, step) for step in plan(db, spec))
            except InvalidTableName:
                print(f"No table named {spec['table']!r}.", file=sys.stderr)
                return 1
            except ValueError as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1
        if not todo:
            print("Nothing to do: all fields are already set up.")
            return 0
        for spec, step in todo:
            print("  " + describe(spec, step))
        if not args.yes:
            try:
                answer = input(f"Type {args.env} to apply these changes: ").strip()
            except EOFError:
                answer = ""
            if answer != args.env:
                print("Aborted. Nothing was changed.")
                return 1
        for spec, step in todo:
            apply(db, spec, step)
            print("  done: " + describe(spec, step))
        for spec in specs:
            verify(db, spec)
    except ConfigError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1
    except FailedIO as err:
        print(f"\nFailed: {err}\nSome changes may already be applied. "
              "Run again to continue.", file=sys.stderr)
        return 1

    print("Done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
