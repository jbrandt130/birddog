"""
Exploratory: plain-text report of Documents-table records added or changed in a
time window, split into categories.

Scans Documents sorted by UpdatedAt (desc) until it passes the window start,
then assigns each record exactly one category (first match wins):
  external   not hosted on a wiki (domain not in WIKI_HOSTS). May well be linked,
             but links aren't tested and no metadata is collected for them, so
             wiki-style classification doesn't apply: just new (CreatedAt in
             window) vs changed
  unlinked   wiki doc with availability != "linked" (placeholders, errors,
             orphaned links); new or changed, kept apart so they don't drown
             the real activity. No pages/size shown (never populated)
  new        linked, CreatedAt in window
  content    linked, pre-existing, wiki file `timestamp` in window. `timestamp`
             is the file's latest imageinfo version time (see
             _fetch_mediawiki_file_metadata), so it only moves on a re-upload.
  metadata   linked, pre-existing, last_birddog_update (or last_imported) in
             window but file unchanged: descriptive/wiki-page/curation fields
  touched    UpdatedAt only (link or housekeeping writes); counted, not listed

Limits: Documents keeps no history, so metadata changes can't say WHICH field
changed, and a doc that flipped unlinked -> linked shows as metadata.
Archives come from the owning page titles (first path segment); watchers come
from the bd_watchers "active" index in DynamoDB.

Usage (BIRDDOG_NOCODB_ENV picks the base; AWS_* picks DDB):
    BIRDDOG_NOCODB_ENV=CLOUD_PROD AWS_PROFILE=birddog-admin AWS_DEFAULT_REGION=us-east-2 \
        PYTHONPATH=. venv/bin/python research/daily_doc_changes_report.py [--hours 24]
"""

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

from birddog.nocodb_database import NocoDBDatabase

ARCHIVE_PREFIX = "Архів:"
WIKI_HOSTS = ("commons.wikimedia.org", "uk.wikisource.org")
FIELDS = [
    "title", "label", "seq_label", "root_label", "domain", "doc_type", "mediatype",
    "page_count", "byte_size", "availability", "owning_pages", "num_owners",
    "is_duplicate", "last_birddog_update", "last_imported", "timestamp",
    "years", "page_description", "page_native_description",
    "CreatedAt", "UpdatedAt",
]
SECTIONS = [
    ("new", "NEW LINKED DOCUMENTS"),
    ("content", "CONTENT CHANGES (file re-uploaded)"),
    ("metadata", "METADATA CHANGES (description / wiki page / curation)"),
    ("unlinked", "UNLINKED WIKI DOCUMENTS (new or changed)"),
    ("external", "NON-WIKI DOCUMENTS (new or changed; links untested)"),
]
# sections whose records carry no page/size data worth showing
NO_SIZE = ("unlinked", "external")


def fetch_window(db, cutoff):
    out, cursor = [], None
    while True:
        recs, cursor = db.scan("Documents", limit=100, cursor=cursor,
                               sort=("UpdatedAt", False), fields=FIELDS)
        for r in recs:
            if (r.get("UpdatedAt") or "") < cutoff:
                return out
            out.append(r)
        if not cursor:
            return out


def active_watchers():
    sfx = "_STAGE" if os.environ.get("BIRDDOG_NOCODB_ENV") == "CLOUD_STAGE" else ""
    t = boto3.resource("dynamodb").Table("bd_watchers" + sfx)
    watchers, kw = defaultdict(set), dict(KeyConditionExpression=Key("namespace").eq("active"))
    while True:
        r = t.query(**kw)
        for i in r["Items"]:
            v = json.loads(i["value"])
            watchers[v["title"]].add(v["email"].split("@")[0])
        if "LastEvaluatedKey" not in r:
            return watchers
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def classify(r, cutoff):
    """-> (category, detail)"""
    is_new = (r.get("CreatedAt") or "") >= cutoff
    kind = "new" if is_new else "chg"
    if (r.get("domain") or "") not in WIKI_HOSTS:
        return "external", kind
    avail = r.get("availability") or "?"
    if avail != "linked":
        # "unlinked" is implied by the section; only call out other states
        return "unlinked", kind + ("" if avail == "unlinked" else " " + avail)
    if is_new:
        return "new", ""
    if (r.get("timestamp") or "") >= cutoff:
        return "content", ""
    # last_imported may be a bare date; compare on the date part only
    if (r.get("last_birddog_update") or "") >= cutoff:
        return "metadata", ""
    if (r.get("last_imported") or "") >= cutoff[:10]:
        return "metadata", "import"
    return "touched", ""


def archive_of(r):
    pages = r.get("owning_pages") or []
    if pages:
        return pages[0]["title"].split("/")[0]
    return "(no owner) " + (r.get("root_label") or r.get("domain") or "?")


def mb(n):
    return f"{(n or 0) / 1e6:,.0f}MB"


def print_section(title, items, watchers, max_docs, show_size=True):
    by_arch = defaultdict(list)
    for detail, r in items:
        by_arch[archive_of(r)].append((detail, r))
    totals = ""
    if show_size:
        pages = sum(r.get("page_count") or 0 for _, r in items)
        size = sum(r.get("byte_size") or 0 for _, r in items)
        totals = f", {pages:,} pages, {mb(size)}"
    print(f"\n{'=' * 78}\n{title}: {len(items)} docs in {len(by_arch)} archives"
          f"{totals}\n{'=' * 78}")
    for arch in sorted(by_arch, key=lambda a: -len(by_arch[a])):
        # seq_label sorts labels in natural order; blank ones go last
        rows = sorted(by_arch[arch], key=lambda x: (not x[1].get("seq_label"),
                                                    x[1].get("seq_label") or "",
                                                    x[1].get("UpdatedAt") or ""))
        w = ", ".join(sorted(watchers.get(ARCHIVE_PREFIX + arch, []))) or "-"
        print(f"\n{arch}  --  {len(rows)} docs   [watchers: {w}]")
        for detail, r in rows[:max_docs]:
            name = (r.get("title") or "?").split(":", 1)[-1]
            width = 30 if show_size else 56
            name = name if len(name) <= width else name[:width - 1] + "~"
            label = r.get("label") or "-"
            flags = " ".join(x for x in (detail, "dup" if r.get("is_duplicate") else "") if x)
            line = f"    {(r.get('UpdatedAt') or '')[5:16]} {label:24} {name:{width}}"
            if show_size:
                line += f" {r.get('page_count') or '-':>5}pp {mb(r.get('byte_size')):>7}"
            print(f"{line}  {flags}".rstrip())
        if len(rows) > max_docs:
            print(f"    ... +{len(rows) - max_docs} more")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--json", metavar="PATH", help="write raw records + watchers as JSON and exit")
    ap.add_argument("--max-docs", type=int, default=5, help="docs listed per archive per section")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=args.hours)).strftime("%Y-%m-%d %H:%M:%S+00:00")
    recs = fetch_window(NocoDBDatabase(), cutoff)
    watchers = active_watchers()

    if args.json:
        keep = ("title", "label", "seq_label", "domain", "page_count", "byte_size", "availability",
                "is_duplicate", "last_birddog_update", "last_imported", "timestamp", "CreatedAt", "UpdatedAt",
                "years", "page_description", "page_native_description")
        out = {
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "env": os.environ["BIRDDOG_NOCODB_ENV"],
            "watchers": {k: sorted(v) for k, v in watchers.items()},
            "records": [{**{k: r.get(k) for k in keep}, "archive": archive_of(r)} for r in recs],
        }
        with open(args.json, "w", encoding="utf8") as f:
            json.dump(out, f, ensure_ascii=False)
        print(f"wrote {len(recs)} records to {args.json}")
        return

    cats = defaultdict(list)
    for r in recs:
        cat, detail = classify(r, cutoff)
        cats[cat].append((detail, r))

    print(f"Documents changes since {cutoff[:19]}Z ({args.hours:g}h, {os.environ['BIRDDOG_NOCODB_ENV']})")
    print("  " + "   ".join(f"{label.split(' (')[0].title()}: {len(cats[key])}" for key, label in SECTIONS)
          + f"   (+{len(cats['touched'])} touched only, not listed)")

    for key, label in SECTIONS:
        if cats[key]:
            print_section(label, cats[key], watchers, args.max_docs,
                          show_size=key not in NO_SIZE)


if __name__ == "__main__":
    main()
