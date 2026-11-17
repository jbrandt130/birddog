#!/usr/bin/env python3
"""
Rename one or more fields of a NocoDB table, keeping the Schema and Schema
Values tables in step with the new names.

For each OLD:NEW pair this
  1. renames the NocoDB column, both its title and the physical column_name
     in the underlying database (meta API; data, views, filters and formulas
     follow automatically since NocoDB refers to columns by id),
  2. rewrites field_name on the matching Schema row(s),
  3. rewrites field_name on the matching Schema Values rows.

Each step is skipped if it is already done, so an interrupted run can simply
be run again. Stop the app first: a running app holds the old schema in memory.

Usage (from anywhere; exactly one target is required):
    venv/bin/python utility/rename_field.py --local Documents doc_type:OLD_doc_type
    venv/bin/python utility/rename_field.py --stage Documents a:OLD_a b:OLD_b
"""

import argparse
import logging
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_TABLES = ("Schema", "Schema Values")
BATCH_SIZE = 100

def parse_pair(text):
    old, sep, new = text.partition(":")
    if not sep or not old or not new or old == new:
        raise argparse.ArgumentTypeError(f"expected OLD:NEW, got {text!r}")
    return old, new

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Rename fields of a NocoDB table and its Schema rows.")
    parser.add_argument("table", help="name of the table whose fields are renamed")
    parser.add_argument("renames", nargs="+", type=parse_pair, metavar="OLD:NEW",
                        help="field rename, e.g. doc_type:OLD_doc_type")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", dest="env", action="store_const", const="LOCAL",
                        help="local dev base")
    target.add_argument("--stage", dest="env", action="store_const", const="CLOUD_STAGE",
                        help="NocoDB Cloud staging base")
    target.add_argument("--prod", dest="env", action="store_const", const="CLOUD_PROD",
                        help="NocoDB Cloud production base")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    return parser.parse_args(argv)

def connect(env_name):
    """Open the backend for env_name. The environment is selected when
    birddog.nocodb_database is first imported, and its config path is relative
    to the repo root, so both have to be set up before the import happens here."""
    os.environ["BIRDDOG_NOCODB_ENV"] = env_name
    os.chdir(REPO_ROOT)
    from birddog.database import Database
    logging.getLogger().setLevel(logging.WARNING)
    db = Database()
    # the meta and raw record APIs live on the NocoDB implementation
    return getattr(db, "_db", db)

def column_map(db, table_name):
    """Fresh title -> column info map (bypasses the cached field map)."""
    info = db._fetch(db._table_info_url(db._table_id(table_name)))
    return {c["title"]: c for c in info["columns"]}

def column_url(db, column_id):
    return f"{db._host}/api/v2/meta/columns/{column_id}"

def rename_column(db, column_id, new_name):
    """Rename both the NocoDB title and the physical column_name. A title-only
    PATCH leaves the underlying database column with its old name; changing
    column_name needs the full column definition (including select options in
    colOptions) or NocoDB rebuilds the column without them."""
    url = column_url(db, column_id)
    column = db._fetch(url)
    options = sorted(o["title"] for o in (column.get("colOptions") or {}).get("options", []))
    column["title"] = new_name
    column["column_name"] = new_name
    db._fetch(url, json=column, method="PATCH")
    after = db._fetch(url)
    after_options = sorted(o["title"] for o in (after.get("colOptions") or {}).get("options", []))
    if after["title"] != new_name or after["column_name"] != new_name or after_options != options:
        raise RuntimeError(f"column {column_id} did not rename cleanly: "
                           f"title={after['title']!r} column_name={after['column_name']!r} "
                           f"options {options} -> {after_options}")

def schema_rows(db, schema_table, table_name):
    return [rec for rec in db.scan_all(schema_table) if rec.get("table_name") == table_name]

def patch_rows(db, schema_table, updates):
    url = db._records_url(db._table_id(schema_table))
    for i in range(0, len(updates), BATCH_SIZE):
        db._fetch(url, json=updates[i:i + BATCH_SIZE], method="PATCH")

def plan(db, table_name, renames):
    """Work out what is left to do. Returns (steps, errors)."""
    columns = column_map(db, table_name)
    rows = {t: schema_rows(db, t, table_name) for t in SCHEMA_TABLES}
    steps, errors = [], []
    for old, new in renames:
        if old in columns and new in columns:
            if columns[new].get("column_name") == new:
                # rename already done and a newer field has since taken the
                # old name; its Schema rows are its own, so leave them alone
                continue
            errors.append(f"{table_name} has both {old!r} and {new!r}")
            continue
        if old in columns:
            steps.append(("column", old, new, columns[old]["id"]))
        elif new in columns and columns[new].get("column_name") != new:
            # an earlier title-only rename: finish the physical column
            steps.append(("column", columns[new]["column_name"], new, columns[new]["id"]))
        elif new not in columns:
            errors.append(f"{table_name} has no field {old!r} (or {new!r})")
            continue
        for schema_table in SCHEMA_TABLES:
            if any(r.get("field_name") == new for r in rows[schema_table]) and \
               any(r.get("field_name") == old for r in rows[schema_table]):
                errors.append(f"{schema_table} has rows for both {old!r} and {new!r}")
                continue
            ids = [r["Id"] for r in rows[schema_table] if r.get("field_name") == old]
            if ids:
                steps.append((schema_table, old, new, ids))
    if not any(r.get("field_name") in {o for o, _ in renames} | {n for _, n in renames}
               for r in rows["Schema"]):
        print(f"Note: none of these fields has a Schema row for {table_name}.")
    return steps, errors

def describe(step):
    kind, old, new, ref = step
    if kind == "column":
        return f"column        {old} -> {new}"
    return f"{kind:13} {old} -> {new}  ({len(ref)} row(s))"

def apply(db, step):
    kind, old, new, ref = step
    if kind == "column":
        rename_column(db, ref, new)
    else:
        patch_rows(db, kind, [{"Id": rid, "field_name": new} for rid in ref])

def main(argv=None):
    args = parse_args(argv)

    sys.path.insert(0, REPO_ROOT)
    from birddog.abstract_database import ConfigError, FailedIO, InvalidTableName

    try:
        db = connect(args.env)
        print(f"Target: {args.env}")
        try:
            steps, errors = plan(db, args.table, args.renames)
        except InvalidTableName:
            print(f"No table named {args.table!r}.", file=sys.stderr)
            return 1
        if errors:
            for err in errors:
                print(f"Error: {err}", file=sys.stderr)
            print("Nothing was changed.", file=sys.stderr)
            return 1
        if not steps:
            print("Nothing to do: all renames are already applied.")
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
