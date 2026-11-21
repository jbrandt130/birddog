#!/usr/bin/env python3
"""
Create a NocoDB table with its key field, then add the table's other fields.

Spec file (see utility/specs/):
    {
      "table": "Birddog Users",
      "key": "birddog_users_email.json",           # field spec of the key field
      "fields": ["birddog_users_name.json", ...]   # other field specs, in order
    }

Paths inside the spec are relative to the spec file. The key field spec must
have "key_field": true and type "text"; it becomes the table's display value.
The other fields are added with utility/add_field.py, so each one gets its
Schema (and Schema Values) rows the usual way.

Each step is skipped if it is already done, so an interrupted run can simply be
run again. Stop the app first: a running app holds the old schema.

Usage (from anywhere; exactly one target is required):
    venv/bin/python utility/add_table.py --local utility/specs/birddog_users_table.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_field  # noqa: E402

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Create a table and its fields.")
    parser.add_argument("spec", help="path to the JSON table spec")
    add_field.add_target_args(parser)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompts")
    return parser.parse_args(argv)

def load_table_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    errors = []
    for key in ("table", "key"):
        if key not in spec:
            errors.append(f"{path}: spec is missing {key!r}")
    if errors:
        return spec, None, [], errors
    base = os.path.dirname(os.path.abspath(path))
    key_path = os.path.join(base, spec["key"])
    field_paths = [os.path.join(base, p) for p in spec.get("fields", [])]
    key_spec, key_errors = add_field.load_spec(key_path)
    errors.extend(key_errors)
    if not key_spec.get("key_field") or key_spec.get("type") != "text":
        errors.append(f"{key_path}: the key field must be a text field with \"key_field\": true")
    for p in [key_path] + field_paths:
        with open(p, encoding="utf-8") as f:
            if json.load(f).get("table") != spec["table"]:
                errors.append(f"{p}: table must be {spec['table']!r}")
    return spec, key_spec, field_paths, errors

def table_exists(db, table_name):
    tables = db._fetch(db._list_tables_url()).get("list", [])
    return any(t.get("title") == table_name for t in tables)

def create_table(db, table_name, key_spec):
    # NocoDB adds its own system columns (Id, CreatedAt, UpdatedAt, ...)
    db._fetch(db._list_tables_url(), method="POST", json={
        "title": table_name,
        "table_name": table_name,
        "columns": [{
            "title": key_spec["field"],
            "column_name": key_spec["field"],
            "uidt": add_field.column_uidt(key_spec),
            "pv": True,
        }],
    })
    db.load_schema()   # pick up the new table id

def confirm(env, args):
    if args.yes:
        return True
    try:
        answer = input(f"Type {env} to apply these changes: ").strip()
    except EOFError:
        answer = ""
    return answer == env

def main(argv=None):
    args = parse_args(argv)
    spec, key_spec, field_paths, errors = load_table_spec(args.spec)
    if errors:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    sys.path.insert(0, add_field.REPO_ROOT)
    from birddog.abstract_database import ConfigError, FailedIO

    table = spec["table"]
    try:
        db = add_field.connect(args.env)
        print(f"Target: {args.env}")
        if table_exists(db, table):
            print(f"Table {table!r} already exists.")
        else:
            print(f"  create table {table!r} with key field {key_spec['field']!r}")
            if not confirm(args.env, args):
                print("Aborted. Nothing was changed.")
                return 1
            create_table(db, table, key_spec)
            print(f"  done: create table {table!r}")
    except ConfigError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1
    except FailedIO as err:
        print(f"\nFailed: {err}\nRun again to continue.", file=sys.stderr)
        return 1

    # key field's Schema row, then the other fields
    target = {"LOCAL": "--local", "CLOUD_STAGE": "--stage", "CLOUD_PROD": "--prod"}[args.env]
    field_args = [os.path.join(os.path.dirname(os.path.abspath(args.spec)), spec["key"])] + field_paths
    return add_field.main(field_args + [target] + (["--yes"] if args.yes else []))

if __name__ == "__main__":
    sys.exit(main())
