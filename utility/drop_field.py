#!/usr/bin/env python3
"""
Remove one or more fields from a NocoDB table, together with their Schema row
and any Schema Values rows.

For each field this
  1. deletes the NocoDB column, along with any values records hold in it,
  2. deletes the matching Schema and Schema Values rows.

Each step is skipped if it is already done, so an interrupted run can simply
be run again. Stop the app first: a running app holds the old schema in memory.

Usage (from anywhere; exactly one target is required):
    venv/bin/python utility/drop_field.py --local Documents save_pending
"""

import argparse
import sys

from rename_field import SCHEMA_TABLES, column_map, column_url, connect, schema_rows

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Remove fields of a NocoDB table and their Schema rows.")
    parser.add_argument("table", help="name of the table whose fields are removed")
    parser.add_argument("fields", nargs="+", help="field name(s) to remove")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", dest="env", action="store_const", const="LOCAL",
                        help="local dev base")
    target.add_argument("--stage", dest="env", action="store_const", const="CLOUD_STAGE",
                        help="NocoDB Cloud staging base")
    target.add_argument("--prod", dest="env", action="store_const", const="CLOUD_PROD",
                        help="NocoDB Cloud production base")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser.parse_args(argv)

def plan(db, table_name, fields):
    """Work out what is left to do."""
    columns = column_map(db, table_name)
    rows = {t: schema_rows(db, t, table_name) for t in SCHEMA_TABLES}
    steps = []
    for field in fields:
        column = columns.get(field)
        if column is not None:
            steps.append(("column", field, column["id"]))
        for schema_table in SCHEMA_TABLES:
            ids = [r["Id"] for r in rows[schema_table] if r.get("field_name") == field]
            if ids:
                steps.append((schema_table, field, ids))
    return steps

def describe(step):
    kind, field, ref = step
    if kind == "column":
        return f"delete column {field}"
    return f"delete {len(ref)} {kind} row(s) for {field}"

def apply(db, step):
    kind, field, ref = step
    if kind == "column":
        db._fetch(column_url(db, ref), method="DELETE")
    else:
        db._fetch(db._records_url(db._table_id(kind)), method="DELETE",
                  json=[{"Id": rid} for rid in ref])

def verify(db, table_name, fields):
    columns = column_map(db, table_name)
    left = [f for f in fields if f in columns]
    for schema_table in SCHEMA_TABLES:
        left += [f"{schema_table}:{r.get('field_name')}" for r in schema_rows(db, schema_table, table_name)
                 if r.get("field_name") in fields]
    if left:
        raise RuntimeError(f"removal check failed, still present: {left}")

def main(argv=None):
    args = parse_args(argv)
    db = connect(args.env)
    from birddog.abstract_database import ConfigError, FailedIO, InvalidTableName
    try:
        print(f"Target: {args.env}")
        try:
            steps = plan(db, args.table, args.fields)
        except InvalidTableName:
            print(f"No table named {args.table!r}.", file=sys.stderr)
            return 1
        if not steps:
            print("Nothing to do: the fields are already gone.")
            return 0
        for step in steps:
            print("  " + describe(step))
        if not args.yes:
            try:
                answer = input(f"Type {args.env} to apply these changes: ").strip()
            except EOFError:
                answer = ""
            if answer != args.env:
                print("Aborted. Nothing was changed.")
                return 1
        for step in steps:
            apply(db, step)
            print("  done: " + describe(step))
        verify(db, args.table, args.fields)
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
