#!/usr/bin/env python3
# (c) 2026 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Delete all records from one table of a NocoDB base, leaving the table and its
fields in place.

Shows the record count, asks for confirmation, then deletes with progress. If a
run is interrupted or fails, just run it again: whatever is left gets picked up.

This touches NocoDB only. Before clearing Pages or Documents, stop the app and
clear its task store: queued update tasks left behind will recreate orphan
Documents (local: sh/clear_local_tasks.sh, cloud: the bd_* DynamoDB task tables).

Usage (from anywhere; exactly one target is required):
    venv/bin/python utility/clear_table.py --local Documents
    venv/bin/python utility/clear_table.py --stage "FTP Site"
    venv/bin/python utility/clear_table.py --prod Locations
"""

import argparse
import logging
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_TABLES = ("Schema", "Schema Values")
DELETE_BATCH_SIZE = 100

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Delete all records from a NocoDB table.")
    parser.add_argument("table", help="name of the table to clear")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", dest="env", action="store_const", const="LOCAL",
                        help="local dev base")
    target.add_argument("--stage", dest="env", action="store_const", const="CLOUD_STAGE",
                        help="NocoDB Cloud staging base")
    target.add_argument("--prod", dest="env", action="store_const", const="CLOUD_PROD",
                        help="NocoDB Cloud production base")
    return parser.parse_args(argv)

def connect(env_name):
    """Open the Database for env_name. The environment is selected when
    birddog.nocodb_database is first imported, and its config path is relative
    to the repo root, so both have to be set up before the import happens here."""
    os.environ["BIRDDOG_NOCODB_ENV"] = env_name
    os.chdir(REPO_ROOT)
    from birddog.database import Database
    # INFO logging goes to stdout and would break up the progress display
    logging.getLogger().setLevel(logging.WARNING)
    return Database()

def confirm(env_name, table_name, total):
    """Require the environment name to be typed back; anything else aborts."""
    prompt = f"Type {env_name} to permanently delete {total} records from {table_name}: "
    try:
        return input(prompt).strip() == env_name
    except EOFError:
        return False

def delete_records(db, table_name, ids):
    for start in range(0, len(ids), DELETE_BATCH_SIZE):
        db.delete(table_name, ids[start:start + DELETE_BATCH_SIZE])
        done = min(start + DELETE_BATCH_SIZE, len(ids))
        print(f"\r{table_name}: {done}/{len(ids)} ({done * 100 // len(ids)}%)",
              end="", flush=True)
    print()

def main(argv=None):
    args = parse_args(argv)

    sys.path.insert(0, REPO_ROOT)
    from birddog.abstract_database import ConfigError, FailedIO

    try:
        db = connect(args.env)
        print(f"Target: {args.env} ({db._host}, base {db._base_id})")
        if args.table not in db._table_id_map:
            print(f"No table named {args.table!r}. Tables: "
                  f"{', '.join(db._table_id_map)}", file=sys.stderr)
            return 1

        ids = db.get_all_ids(args.table)
        print(f"{args.table}: {len(ids)} records")
        if not ids:
            print("Nothing to delete.")
            return 0

        if args.table in SCHEMA_TABLES:
            print(f"WARNING: {args.table} is configuration. "
                  "Without it the app runs schema-less.")
        if not confirm(args.env, args.table, len(ids)):
            print("Aborted. Nothing was deleted.")
            return 1

        delete_records(db, args.table, ids)
        remaining = len(db.get_all_ids(args.table))
    except ConfigError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1
    except FailedIO as err:
        print(f"\nFailed: {err}\nSome records may already be deleted. "
              "Run again to continue.", file=sys.stderr)
        return 1

    if remaining:
        print(f"Done, but {remaining} records remain. Run again to clear them.")
        return 1
    print(f"Done. Deleted {len(ids)} records from {args.table}.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
