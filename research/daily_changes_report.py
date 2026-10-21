"""
Exploratory: plain-text report of wiki pages changed or added in a time window.

Reads the page change log ("pcl" namespace of bd_tracker_kv_store) and the
active-watch index ("active" namespace of bd_watchers) straight from DynamoDB.
Note the change log holds only the LATEST change per title, so "new" means the
latest recorded action was a page creation; a page created and then edited
within the window shows as "edit".

Usage:
    AWS_PROFILE=birddog-admin AWS_DEFAULT_REGION=us-east-2 \
        venv/bin/python research/daily_changes_report.py [--hours 24] [--stage]
"""

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

BOT_SUFFIX = "Bot"
ARCHIVE_PREFIX = "Архів:"


def query_ns(table, namespace):
    items, kw = [], dict(KeyConditionExpression=Key("namespace").eq(namespace))
    while True:
        r = table.query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def split_title(title):
    """-> (archive root, fond path or None)"""
    t = title[len(ARCHIVE_PREFIX):] if title.startswith(ARCHIVE_PREFIX) else title
    parts = t.split("/")
    root = ARCHIVE_PREFIX + parts[0] if title.startswith(ARCHIVE_PREFIX) else parts[0]
    fond = "/".join(parts[1:2]) or None
    return root, fond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--max-pages", type=int, default=5, help="sample pages shown per fond")
    args = ap.parse_args()

    sfx = "_STAGE" if args.stage else ""
    ddb = boto3.resource("dynamodb")
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    changes = {}
    for i in query_ns(ddb.Table("bd_tracker_kv_store" + sfx), "pcl"):
        v = json.loads(i["value"])
        if v["timestamp"] >= cutoff:
            changes[i["key"]] = v

    watchers = defaultdict(set)  # watched archive title -> emails
    for i in query_ns(ddb.Table("bd_watchers" + sfx), "active"):
        v = json.loads(i["value"])
        watchers[v["title"]].add(v["email"].split("@")[0])

    # archive -> fond -> [(title, change)]
    tree = defaultdict(lambda: defaultdict(list))
    for title, v in changes.items():
        root, fond = split_title(title)
        tree[root][fond].append((title, v))

    n_new = sum(1 for v in changes.values() if v.get("action") == "new")
    n_bot = sum(1 for v in changes.values() if (v.get("user") or "").endswith(BOT_SUFFIX))
    print(f"Wiki changes since {cutoff} ({args.hours:g}h, {'STAGE' if args.stage else 'PROD'})")
    print(f"{len(changes)} pages in {len(tree)} archives: "
          f"{n_new} new, {len(changes) - n_new} edited, {n_bot} by bots")
    print("=" * 78)

    for root in sorted(tree, key=lambda r: -sum(len(p) for p in tree[r].values())):
        pages = [p for fp in tree[root].values() for p in fp]
        new = sum(1 for _, v in pages if v.get("action") == "new")
        users = defaultdict(int)
        for _, v in pages:
            users[v.get("user") or "?"] += 1
        watched = ", ".join(sorted(watchers.get(root, []))) or "-"
        print(f"\n{root}  --  {len(pages)} pages ({new} new, {len(pages) - new} edited)")
        print(f"  users:    " + ", ".join(f"{u} ({n})" for u, n in sorted(users.items(), key=lambda x: -x[1])))
        print(f"  watchers: {watched}")
        for fond in sorted(tree[root], key=lambda f: (f is None, f)):
            fp = sorted(tree[root][fond], key=lambda p: p[1]["timestamp"])
            fnew = sum(1 for _, v in fp if v.get("action") == "new")
            print(f"  {fond or '(root page)'}: {len(fp)} ({fnew} new)")
            for title, v in fp[:args.max_pages]:
                tail = title[len(root) + 1:] if title != root else title
                print(f"      {v['timestamp'][5:16]} {v.get('action') or 'edit':4} {v.get('user')}: {tail}")
            if len(fp) > args.max_pages:
                print(f"      ... +{len(fp) - args.max_pages} more")


if __name__ == "__main__":
    main()
