#!/usr/bin/env python3
"""
Delete all records from one or more tables of a NocoDB base, leaving the
tables and their fields in place.

Shows each table's record count, asks once for confirmation, then clears the
tables in the order given, with progress. If a run is interrupted or fails,
just run it again: whatever is left gets picked up.

This touches NocoDB only. Before clearing Pages or Documents, stop the app and
clear its task store: queued update tasks left behind will recreate orphan
Documents (local: utility/clear_local_tasks.sh, cloud: the bd_* DynamoDB task tables).

Usage (from anywhere; exactly one target is required):
    venv/bin/python utility/clear_table.py --local Documents
    venv/bin/python utility/clear_table.py --local Pages Documents
    venv/bin/python utility/clear_table.py --stage "FTP Site"
    venv/bin/python utility/clear_table.py --prod Locations
"""

import argparse
import logging
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_TABLES = ("Schema", "Schema Values")
# Records listed and deleted per round. The backend may return fewer per page
# (NocoDB Cloud caps pages at 100), and delete() splits large requests itself.
PAGE_SIZE = 1000

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Delete all records from NocoDB tables.")
    parser.add_argument("tables", nargs="+", metavar="table", help="name(s) of the table(s) to clear")
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

def confirm(env_name, table_names, total):
    """Require the environment name to be typed back; anything else aborts."""
    prompt = f"Type {env_name} to permanently delete {total} records from {', '.join(table_names)}: "
    try:
        return input(prompt).strip() == env_name
    except EOFError:
        return False

def delete_records(db, table_name, total):
    """Delete the first page of records until the table is empty, without
    listing the whole table up front. Returns True once the table is empty, or
    False if a record is still listed after deleting it (so the loop can't spin).

    Progress counts the ids listed rather than delete()'s result, which can be
    lower: a delete retried after a lost response finds those records already
    gone (see NocoDBDatabase.delete)."""
    deleted = 0
    last_first = None
    while True:
        records, _ = db.scan(table_name, limit=PAGE_SIZE, fields=["Id"])
        ids = [rec["Id"] for rec in records]
        if not ids:
            print()
            return True
        if ids[0] == last_first:
            print(f"\nRecord {ids[0]} is still listed after deleting it, stopping.")
            return False
        last_first = ids[0]
        db.delete(table_name, ids)
        deleted += len(ids)
        print(f"\r{table_name}: deleted {deleted}/{total}", end="", flush=True)

def main(argv=None):
    args = parse_args(argv)

    sys.path.insert(0, REPO_ROOT)
    from birddog.abstract_database import ConfigError, FailedIO, InvalidTableName

    tables = list(dict.fromkeys(args.tables))  # in order, each once
    try:
        db = connect(args.env)
        print(f"Target: {args.env}")
        # count every table before deleting anything, so a misspelled name
        # aborts the whole run
        counts = {}
        for table in tables:
            try:
                counts[table] = db.count(table)
            except InvalidTableName:
                print(f"No table named {table!r}. Nothing was deleted.", file=sys.stderr)
                return 1
            print(f"{table}: {counts[table]} records")
        to_clear = [t for t in tables if counts[t]]
        if not to_clear:
            print("Nothing to delete.")
            return 0

        for table in to_clear:
            if table in SCHEMA_TABLES:
                print(f"WARNING: {table} is configuration. "
                      "Without it the app runs schema-less.")
        if not confirm(args.env, to_clear, sum(counts[t] for t in to_clear)):
            print("Aborted. Nothing was deleted.")
            return 1

        for table in to_clear:
            if not delete_records(db, table, counts[table]):
                return 1
            print(f"{table} is empty.")
    except ConfigError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1
    except FailedIO as err:
        print(f"\nFailed: {err}\nSome records may already be deleted. "
              "Run again to continue.", file=sys.stderr)
        return 1

    print("Done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
