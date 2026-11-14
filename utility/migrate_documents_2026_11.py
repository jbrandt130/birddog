#!/usr/bin/env python3
# (c) 2026 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
November 2026 Documents schema migration, in one rerunnable script:

  1. rename the legacy fields doc_type, content_code and process_code to
     OLD_doc_type, OLD_content_code and OLD_process_code (column title and
     physical column, plus their Schema and Schema Values rows), then
  2. add the new fields described by the specs in utility/specs/ (doc_type,
     doc_structure, doc_scope, process_code, extract_status, anomaly,
     anomaly_comment), each with its Schema and Schema Values rows, and bring
     their descriptions in line with the specs.

The rename has to come first: the new doc_type and process_code reuse the
legacy names. Every step is skipped once done, so the script can be rerun
after an interruption or after editing a spec (e.g. to fill in descriptions).

Before running against a cloud base: stop the app (it holds the old schema in
memory) and deploy the code that uses the OLD_* names together with it.

Usage (from anywhere; exactly one target is required):
    venv/bin/python utility/migrate_documents_2026_11.py --local
    venv/bin/python utility/migrate_documents_2026_11.py --stage
    venv/bin/python utility/migrate_documents_2026_11.py --prod
"""

import argparse
import os
import sys

UTILITY_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(UTILITY_DIR)
sys.path.insert(0, UTILITY_DIR)

import add_field  # noqa: E402
import rename_field  # noqa: E402

TABLE = "Documents"
RENAMES = [
    ("doc_type", "OLD_doc_type"),
    ("content_code", "OLD_content_code"),
    ("process_code", "OLD_process_code"),
]
SPECS = [
    "documents_doc_type.json",
    "documents_doc_structure.json",
    "documents_doc_scope.json",
    "documents_process_code.json",
    "documents_extract_status.json",
    "documents_anomaly.json",
    "documents_anomaly_comment.json",
]

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Documents schema migration, November 2026.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", dest="env", action="store_const", const="LOCAL",
                        help="local dev base")
    target.add_argument("--stage", dest="env", action="store_const", const="CLOUD_STAGE",
                        help="NocoDB Cloud staging base")
    target.add_argument("--prod", dest="env", action="store_const", const="CLOUD_PROD",
                        help="NocoDB Cloud production base")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser.parse_args(argv)

def load_specs():
    specs, errors = [], []
    for name in SPECS:
        spec, spec_errors = add_field.load_spec(os.path.join(UTILITY_DIR, "specs", name))
        specs.append(spec)
        errors.extend(spec_errors)
    return specs, errors

def confirm(env_name):
    try:
        return input(f"Type {env_name} to apply these changes: ").strip() == env_name
    except EOFError:
        return False

def plan_adds(db, specs):
    todo = []
    for spec in specs:
        todo.extend((spec, step) for step in add_field.plan(db, spec))
    return todo

def main(argv=None):
    args = parse_args(argv)
    specs, errors = load_specs()
    if errors:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    sys.path.insert(0, REPO_ROOT)
    from birddog.abstract_database import ConfigError, FailedIO, InvalidTableName

    try:
        db = rename_field.connect(args.env)
        print(f"Target: {args.env}")

        rename_steps, errors = rename_field.plan(db, TABLE, RENAMES)
        if errors:
            for err in errors:
                print(f"Error: {err}", file=sys.stderr)
            print("Nothing was changed.", file=sys.stderr)
            return 1

        # Until the renames are done the legacy columns still hold the new
        # names, so the add plan can only be worked out afterwards.
        if rename_steps:
            print("Step 1, rename legacy fields:")
            for step in rename_steps:
                print("  " + rename_field.describe(step))
            print("Step 2, add new fields (worked out after step 1):")
            for spec in specs:
                print(f"  {TABLE}.{spec['field']} ({spec['type']})")
            add_steps = None
        else:
            print("Step 1, rename legacy fields: already done")
            add_steps = plan_adds(db, specs)
            if not add_steps:
                print("Step 2, add new fields: already done")
                print("Nothing to do.")
                return 0
            print("Step 2, add new fields:")
            for spec, step in add_steps:
                print("  " + add_field.describe(spec, step))

        if not args.yes and not confirm(args.env):
            print("Aborted. Nothing was changed.")
            return 1

        for step in rename_steps:
            rename_field.apply(db, step)
            print("  done: " + rename_field.describe(step))
        if add_steps is None:
            add_steps = plan_adds(db, specs)
        for spec, step in add_steps:
            add_field.apply(db, spec, step)
            print("  done: " + add_field.describe(spec, step))
        for spec in specs:
            add_field.verify(db, spec)
    except InvalidTableName as err:
        print(f"No such table: {err}", file=sys.stderr)
        return 1
    except ValueError as err:
        print(f"Error: {err}\nSome changes may already be applied; fix the cause and run again.",
              file=sys.stderr)
        return 1
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
