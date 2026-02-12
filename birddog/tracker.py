# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

import os

from datetime import datetime, timedelta, timezone
import time

from birddog.env import detect_environment
from birddog.wiki import (
    get_recent_changes,
    get_all_pages,
    batch_page_exists,
    get_last_mod,
    canonicalize_title,
    lookup_namespace_id,
    get_recent_changes_v2,
    )
from birddog.store import KeyValueStore
from birddog.utility import json_size, HeartbeatManager
from birddog.log import get_logger, LogService
_logger = get_logger()


# -------------------------------------------------------------------------------
# SQLITE table implementations
import sqlite3

_SQLITE_PAGE_TRACKER_PATH = ".cache/page_tracker.db"

class SQLitePageChangeLogTable:
    def __init__(self, db_path=_SQLITE_PAGE_TRACKER_PATH):
        self._db_path = db_path
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS page_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    timestamp TEXT,
                    user TEXT
                )
            ''')
            conn.commit()

    def get(self):
        with LogService("PageChangeLog", "get") as log:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute("SELECT title, timestamp, user FROM page_changes")
                payload = cur.fetchall()
            log.size = json_size(payload)
        return payload

    def append(self, changes):
        with LogService("PageChangeLog", "append", size=json_size(changes)):
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    '''
                    INSERT INTO page_changes (title, timestamp, user)
                    VALUES (?, ?, ?)
                    ''',
                    [
                        (title, update["timestamp"], update["user"])
                        for title, update in changes.items()
                    ]
                )

    def truncate(self, cutoff_date):
        with LogService("PageChangeLog", "truncate"):
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM page_changes WHERE timestamp < ?", (cutoff_date,))
                conn.commit()

    def clear(self):
        with LogService("PageChangeLog", "clear"):
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM page_changes")
                conn.commit()

class SQLitePageTrackerTable:
    def __init__(self, db_path=_SQLITE_PAGE_TRACKER_PATH):
        self._db_path = db_path
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS page_tracker (
                    title TEXT PRIMARY KEY,
                    timestamp TEXT,
                    user TEXT
                )
            ''')
            conn.commit()

    def get(self):
        with LogService("PageTracker", "get") as log:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute("SELECT * FROM page_tracker")
                payload = cur.fetchall()
            log.size = json_size(payload)
        return payload

    def put(self, updates):
        with LogService("PageTracker", "put", size=json_size(updates)):
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    "REPLACE INTO page_tracker (title, timestamp, user) VALUES (?, ?, ?)",
                    [
                        (title, update.get("timestamp"), update.get("user"))
                        for title, update in updates.items()
                    ]
                )

    def remove(self, titles):
        with LogService("PageTracker", "put", size=json_size(titles)):
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    "DELETE FROM page_tracker WHERE title = ?",
                    [(t,) for t in titles]
                )
                conn.commit()

    def clear(self):
        with LogService("PageTracker", "clear"):
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM page_tracker")
                conn.commit()

# -------------------------------------------------------------------------------
# DynamoDB table implementations

import time
import uuid
import boto3
from boto3.dynamodb.conditions import Attr
from typing import Dict, List, Tuple

class DynamoDBPageChangeLogTable:
    """
    Simple DynamoDB-backed page change log.

    Table schema:
      - PK:  title (S)
      - SK:  tsk   (S)  -> f"{timestamp}#{random}" to avoid collisions
      - Attrs:
          - ts   (S)    -> original timestamp string (used for truncate/filter/sort)
          - user (S)

    Notes:
      - We keep 'ts' as an attribute for filtering and global sorting.
      - 'tsk' ensures uniqueness even if multiple changes share the same (title, timestamp).
      - Efficiency: get()/truncate()/clear() use scans by design (you said occasional truncate and ~10k/mo).
    """

    def __init__(self, table_name: str = 'birddog_page_changes'):
        self._table_name = table_name
        self._dynamodb = boto3.resource('dynamodb')   # region auto from env/role
        self._client = boto3.client('dynamodb')
        self._ensure_table_exists()
        self._table = self._dynamodb.Table(self._table_name)

    # --- setup ---

    def _ensure_table_exists(self):
        if self._table_name in self._client.list_tables()['TableNames']:
            return
        self._dynamodb.create_table(
            TableName=self._table_name,
            KeySchema=[
                {'AttributeName': 'title', 'KeyType': 'HASH'},
                {'AttributeName': 'tsk',   'KeyType': 'RANGE'},
            ],
            AttributeDefinitions=[
                {'AttributeName': 'title', 'AttributeType': 'S'},
                {'AttributeName': 'tsk',   'AttributeType': 'S'},
            ],
            BillingMode='PAY_PER_REQUEST',
        ).wait_until_exists()

    def get(self) -> List[Tuple[str, str, str]]:
        """
        Return list of (title, timestamp, user) for all rows, sorted by timestamp asc.
        """
        items: List[dict] = []
        last_evaluated_key = None
        with LogService("PageChangeLog", "get") as log:
            while True:
                kwargs = {
                    'ProjectionExpression': '#t, #ts, #u',
                    'ExpressionAttributeNames': {'#t': 'title', '#ts': 'ts', '#u': 'user'},
                }
                if last_evaluated_key:
                    kwargs['ExclusiveStartKey'] = last_evaluated_key
                resp = self._table.scan(**kwargs)
                items.extend(resp.get('Items', []))
                last_evaluated_key = resp.get('LastEvaluatedKey')
                if not last_evaluated_key:
                    break
            log.size = json_size(items)

        # Sort globally by the stored string timestamp.
        items.sort(key=lambda it: it.get('ts', ''))
        return [(it.get('title', ''), it.get('ts', ''), it.get('user', '')) for it in items]

    def append(self, changes: Dict[str, Dict[str, str]]) -> None:
        """
        changes: { title: {"timestamp": "...", "user": "..."} }
        """
        if not changes:
            return

        to_put = []
        now_ms = int(time.time() * 1000)
        with LogService("PageChangeLog", "append") as log:
            for i, (title, update) in enumerate(changes.items()):
                ts = update["timestamp"]
                user = update["user"]
                # Unique, order-friendly sort key; includes provided ts for readability + random suffix
                tsk = f"{ts}#{now_ms:x}-{i:x}-{uuid.uuid4().hex[:8]}"
                to_put.append({'title': title, 'tsk': tsk, 'ts': ts, 'user': user})

            log.size = json_size(to_put)

            with self._table.batch_writer() as batch:
                for item in to_put:
                    batch.put_item(Item=item)

    def truncate(self, cutoff_date: str) -> None:
        """
        Delete all rows where ts < cutoff_date.
        Uses a filtered scan (simple and OK for your scale/frequency).
        """
        last_evaluated_key = None
        deleted = 0
        with LogService("PageChangeLog", "truncate") as log:
            while True:
                kwargs = {
                    'FilterExpression': Attr('ts').lt(cutoff_date),
                    'ProjectionExpression': '#t, #k, #ts',
                    'ExpressionAttributeNames': {'#t': 'title', '#k': 'tsk', '#ts': 'ts'},
                }
                if last_evaluated_key:
                    kwargs['ExclusiveStartKey'] = last_evaluated_key

                resp = self._table.scan(**kwargs)
                items = resp.get('Items', [])
                if items:
                    with self._table.batch_writer() as batch:
                        for it in items:
                            batch.delete_item(Key={'title': it['title'], 'tsk': it['tsk']})
                    deleted += len(items)

                last_evaluated_key = resp.get('LastEvaluatedKey')
                if not last_evaluated_key:
                    break
            log.size = deleted

    def clear(self) -> None:
        """
        Delete all rows (full-table scan + batch deletes).
        """
        last_evaluated_key = None
        deleted = 0
        with LogService("PageChangeLog", "clear") as log:
            while True:
                kwargs = {
                    'ProjectionExpression': '#t, #k',
                    'ExpressionAttributeNames': {'#t': 'title', '#k': 'tsk'},
                }
                if last_evaluated_key:
                    kwargs['ExclusiveStartKey'] = last_evaluated_key

                resp = self._table.scan(**kwargs)
                items = resp.get('Items', [])
                if items:
                    with self._table.batch_writer() as batch:
                        for it in items:
                            batch.delete_item(Key={'title': it['title'], 'tsk': it['tsk']})
                    deleted += len(items)

                last_evaluated_key = resp.get('LastEvaluatedKey')
                if not last_evaluated_key:
                    break
            log.size = deleted

class DynamoDBPageTrackerTable:
    """
    DynamoDB equivalent of SQLitePageTrackerTable.

    Table schema:
      - PK: title (S)
      - Attrs: timestamp (S), user (S)

    Semantics:
      - put(updates) behaves like REPLACE INTO: put_item overwrites by PK (title).
      - get() returns list of (title, timestamp, user) tuples.
      - remove(titles) deletes by PK in batch.
      - clear() scans and batch-deletes everything.
    """

    def __init__(self, table_name: str = 'birddog_page_tracker'):
        self._table_name = table_name
        self._dynamodb = boto3.resource('dynamodb')   # region auto-detected from env/role
        self._client = boto3.client('dynamodb')
        self._ensure_table_exists()
        self._table = self._dynamodb.Table(self._table_name)

    # --- setup ---

    def _ensure_table_exists(self):
        if self._table_name in self._client.list_tables()['TableNames']:
            return
        self._dynamodb.create_table(
            TableName=self._table_name,
            KeySchema=[
                {'AttributeName': 'title', 'KeyType': 'HASH'},
            ],
            AttributeDefinitions=[
                {'AttributeName': 'title', 'AttributeType': 'S'},
            ],
            BillingMode='PAY_PER_REQUEST'
        ).wait_until_exists()

    # --- API parity ---

    def get(self) -> List[Tuple[str, str, str]]:
        """Return list of (title, timestamp, user) for all rows."""
        items: List[dict] = []
        last_evaluated_key = None
        with LogService("PageTracker", "get") as log:
            while True:
                kwargs = {
                    'ProjectionExpression': '#t, #ts, #u',
                    'ExpressionAttributeNames': {'#t': 'title', '#ts': 'timestamp', '#u': 'user'},
                }
                if last_evaluated_key:
                    kwargs['ExclusiveStartKey'] = last_evaluated_key
                resp = self._table.scan(**kwargs)
                batch = resp.get('Items', [])
                items.extend(batch)
                last_evaluated_key = resp.get('LastEvaluatedKey')
                if not last_evaluated_key:
                    break

            payload = [(it.get('title', ''), it.get('timestamp', ''), it.get('user', '')) for it in items]
            log.size = json_size(payload)
        return payload

    def put(self, updates: Dict[str, Dict[str, str]]) -> None:
        """
        updates: { title: {"timestamp": "...", "user": "..."} }
        Overwrites existing item with same title (REPLACE semantics).
        """
        if not updates:
            return

        items = [
            {'title': title,
             'timestamp': data.get('timestamp'),
             'user': data.get('user')}
            for title, data in updates.items()
        ]

        with LogService("PageTracker", "put", size=json_size(items)):
            with self._table.batch_writer() as batch:
                for it in items:
                    batch.put_item(Item=it)

    def remove(self, titles: List[str]) -> None:
        """Delete items by title."""
        if not titles:
            return

        with LogService("PageTracker", "remove", size=json_size(titles)):
            with self._table.batch_writer() as batch:
                for t in titles:
                    batch.delete_item(Key={'title': t})

    def clear(self) -> None:
        """Delete all items."""
        last_evaluated_key = None
        deleted = 0
        with LogService("PageTracker", "clear") as log:
            while True:
                kwargs = {
                    'ProjectionExpression': '#t',
                    'ExpressionAttributeNames': {'#t': 'title'},
                }
                if last_evaluated_key:
                    kwargs['ExclusiveStartKey'] = last_evaluated_key

                resp = self._table.scan(**kwargs)
                items = resp.get('Items', [])
                if items:
                    with self._table.batch_writer() as batch:
                        for it in items:
                            batch.delete_item(Key={'title': it['title']})
                    deleted += len(items)

                last_evaluated_key = resp.get('LastEvaluatedKey')
                if not last_evaluated_key:
                    break
            log.size = deleted

# -------------------------------------------------------------------------------

if detect_environment() == "aws":
    PageChangeLogTable = DynamoDBPageChangeLogTable
    PageTrackerTable = DynamoDBPageTrackerTable
else:
    PageChangeLogTable = SQLitePageChangeLogTable
    PageTrackerTable = SQLitePageTrackerTable

# -------------------------------------------------------------------------------

class PageChangeLog:
    def __init__(self):
        self._table = PageChangeLogTable()
        self._changes = self._table.get()

    def size(self):
        return len(self._changes)

    def oldest(self):
        if not self._changes:
            raise ValueError("empty page change log")
        return min(item[1] for item in self._changes)

    def newest(self):
        if not self._changes:
            raise ValueError("empty page change log")
        return max(item[1] for item in self._changes)

    def refresh(self):
        cutoff_date = self.newest() if self._changes else None
        updates = get_recent_changes(cutoff_date=cutoff_date)
        if updates:
            _logger.info(f"PageChangeLog: recording {len(updates)} new page changes")
            updates = {
                canonicalize_title(title): update
                for title, update in updates.items()
            }
            self._table.append(updates)
            self._changes.extend([
                (title, update["timestamp"], update["user"])
                for title, update in updates.items()])

    def truncate(self, cutoff_date):
        self._table.truncate(cutoff_date)
        self._changes = [item for item in self._changes if item[1] >= cutoff_date]

    def get(self):
        result = {}
        for title, timestamp, user in self._changes:
            if title not in result or result[title]["timestamp"] < timestamp:
                result[title] = { "timestamp": timestamp, "user": user }
        return result

class PageTracker:
    def __init__(self):
        self._change_log = PageChangeLog()
        self._table = PageTrackerTable()
        self._load_cache()

    def _load_cache(self):
        self._page_dict = {
            title: { "timestamp": timestamp, "user": user }
            for title, timestamp, user in self._table.get() }

    def reset(self, all_titles=None):
        if not all_titles:
            _logger.info("PageTracker.reset: generating title inventory for archive...")
            all_titles = get_all_pages()
        self._table.clear()
        if all_titles:
            _logger.info(f"PageTracker.reset: inserting {len(all_titles)} titles into tracker")
            self._table.put(all_titles)
        self._load_cache()

    def refresh(self):
        self._change_log.refresh()
        updates = self._change_log.get()
        _logger.info(f"PageTracker.refresh: processing change log (length={len(updates)})")
        newer_updates = {}
        for title, item in updates.items():
            latest = self._page_dict.get(title, {}).get("timestamp")
            if not latest or latest < item["timestamp"]:
                newer_updates[title] = item
        if newer_updates:
            _logger.info(f"PageTracker.refresh found {len(newer_updates)} page updates")
            self._table.put(newer_updates)
            for title, item in newer_updates.items():
                #_logger.info(f"{title}, {item}")
                self._page_dict[title] = item
        return newer_updates

    def initialize_batch_of_unknowns(self, batch_size=50, api_delay=.1):
        new_titles = [title for title, item in self._page_dict.items() if not item.get("timestamp")]
        _logger.info(f"PageTracker: {len(new_titles)} unknowns remaining")
        if not new_titles:
            return False
        new_titles = new_titles[:batch_size]
        if new_titles:
            check = batch_page_exists(new_titles)
            good_titles = [title for title in new_titles if check.get(title)]
            bad_titles = [title for title in new_titles if not check.get(title)]
            if good_titles:
                updates = get_last_mod(good_titles, api_delay=api_delay)
                updates = {title: {"timestamp": value} for title, value in updates.items()}
                _logger.info(f"PageTracker: initializing mod dates for {len(updates)} titles")
                self._table.put(updates)
                for title, item in updates.items():
                    self._page_dict[title] = item
            if bad_titles:
                # take non-existent titles out of tracker table
                _logger.info(f"PageTracker: removing {len(bad_titles)} non-existent titles")
                self._table.remove(bad_titles)
                for title in bad_titles:
                    del self._page_dict[title]
        return True

    def get_updates(self, prefix, cutoff_date=None):
        prefix = canonicalize_title(prefix)
        if not cutoff_date:
            cutoff_date = "0" # older than all dates
        def should_include(title, update):
            return title.startswith(prefix) and update.get("timestamp") and update.get("timestamp") >= cutoff_date
        return {
            title: update
            for title, update in self._page_dict.items()
            if should_include(title, update)
        }

# -------------------------------------------------------------------------------
# table bootstrapping utilities

def process_tracker_unknowns():
    from time import sleep
    tracker = PageTracker()
    still_more = True
    while still_more:
        still_more = tracker.initialize_batch_of_unknowns()
        sleep(1)

def copy_page_tracker_to_ddb(batch_size=100, limit=None):
    ddb_table = DynamoDBPageTrackerTable()
    page_tracker = PageTracker()
    entries = list(page_tracker._page_dict.items())
    if not limit:
        limit = len(entries)
    _logger.info(f"pushing {limit} entries to DDB, batch_size={batch_size}")
    for i in range(0, limit, batch_size):
        _logger.info(f"batch {i}")
        batch = { title: update for title, update in entries[i:(i+batch_size)] }
        ddb_table.put(batch)

# -------------------------------------------------------------------------------
# wiki document change tracking

def _parse_utc(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(ts, str):
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise TypeError(f"Unsupported timestamp type: {type(ts)}")

def _format_utc_z(dt):
    dt = _parse_utc(dt)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _utc_now_dt():
    return datetime.now(timezone.utc)

def offset_utc(ts, seconds):
    dt = _parse_utc(ts)
    return _format_utc_z(dt + timedelta(seconds=seconds))

_WIKIMEDIA_COMMONS = "https://commons.wikimedia.org"
_UK_WIKISOURCE = "https://uk.wikisource.org"
_UK_WIKISOURCE_NS = "Файл"
_WIKI_DOC_TRACKER_KV_TABLE = "doc_tracker"
_WIKI_DOC_TRACKER_HEARTBEAT_INTERVAL = 15  # seconds
_WIKI_SENTINEL = "WIKI_SENTINEL"
_WIKI_CHANGE_EVENT_WINDOW = 300  # seconds
_DOC_TABLE_SENTINEL = "DOC_SENTINEL"

class WikiDocTracker(HeartbeatManager):
    def __init__(
        self,
        runtime,
        cutoff_time=None,
        base_url=_WIKIMEDIA_COMMONS,
        namespace="File",
        change_window_s=_WIKI_CHANGE_EVENT_WINDOW,
    ):
        self._runtime = runtime
        self._base_url = base_url
        self._namespace = namespace
        self._db = self._runtime.database

        self._namespace_id = lookup_namespace_id(self._namespace)
        self._kv = KeyValueStore(table_name=_WIKI_DOC_TRACKER_KV_TABLE)

        self._doc_kv_namespace = f"{self._base_url}:{self._namespace}"
        self._sentinel_kv_namespace = f"{self._base_url}:{self._namespace}:SENTINELS"

        self._cutoff_time = _format_utc_z(cutoff_time) if cutoff_time else None
        self._change_window_s = int(change_window_s)

        # In-memory doc index (normalized titles). None means "not loaded yet".
        self._doc_map = None  # dict[str, str] normalized_title -> link

        _logger.info(
            f"WikiDocTracker: base={self._base_url}, "
            f"namespace={self._namespace} (id={self._namespace_id})"
        )

        super().__init__(interval=_WIKI_DOC_TRACKER_HEARTBEAT_INTERVAL)

    def _reset(self):
        self._kv.remove_all(self._doc_kv_namespace)
        self._kv.remove_all(self._sentinel_kv_namespace)
        self._doc_map = None

    def _normalize_title(self, title):
        return title.replace(" ", "_")

    def _ensure_doc_map(self):
        if self._doc_map is None:
            self._doc_map = {}
            for k, v in self._kv.get_all(self._doc_kv_namespace):  # iterable of (key, value)
                self._doc_map[k] = v
    
            _logger.info(f"WikiDocTracker: loaded {len(self._doc_map)} doc titles into memory")

    def _store_relevant_titles(self, records):
        """
        Store relevant titles in KV and update in-memory doc_map incrementally.
        """
        inserts = 0
        for rec in records:
            link = rec.get("link", "")
            if not link.startswith(self._base_url):
                continue

            title = rec.get("title")
            if not title:
                continue

            nt = self._normalize_title(title)

            if nt in self._doc_map:
                continue

            self._kv.insert(self._doc_kv_namespace, nt, link)
            self._doc_map[nt] = link
            inserts += 1

        if inserts:
            _logger.info(f"WikiDocTracker: inserted {inserts} new doc titles")

    def _refresh_doc_titles(self):
        if not self._db:
            _logger.warning(f"WikiDocTracker: database unavailable - skipping document title refresh")
            return
        self._ensure_doc_map()
        try:
            doc_sentinel = self._kv.get(self._sentinel_kv_namespace, _DOC_TABLE_SENTINEL)
        except KeyError:
            doc_sentinel = None

        # Sort by descending CreatedAt (newest first).
        sort_spec = ("CreatedAt", False)

        newest_creation_date = None
        cursor = None
        while True:
            batch, cursor = self._db.scan("Documents", cursor=cursor, sort=sort_spec)
            if not batch:
                break

            if newest_creation_date is None:
                newest_creation_date = max(rec["CreatedAt"] for rec in batch if rec.get("CreatedAt"))

            self._store_relevant_titles(batch)

            # Stop when we reach records older than previously-seen newest doc.
            if doc_sentinel:
                last_created = batch[-1].get("CreatedAt")
                if last_created and last_created < doc_sentinel:
                    break

            if not cursor:
                break

        if newest_creation_date:
            self._kv.insert(self._sentinel_kv_namespace, _DOC_TABLE_SENTINEL, str(newest_creation_date))

    def _get_wiki_sentinel(self):
        """
        Returns UTC Z string.
        """
        try:
            t = self._kv.get(self._sentinel_kv_namespace, _WIKI_SENTINEL)
            t = _format_utc_z(t)
            if self._cutoff_time:
                t = _format_utc_z(max(_parse_utc(t), _parse_utc(self._cutoff_time)))
            return t
        except KeyError:
            if self._cutoff_time:
                return self._cutoff_time
            raise ValueError("undefined wiki sentinel")

    def _set_wiki_sentinel(self, timestamp):
        self._kv.insert(self._sentinel_kv_namespace, _WIKI_SENTINEL, _format_utc_z(timestamp))

    def _get_wiki_changes(self, utc_start):
        self._ensure_doc_map()

        start_dt = _parse_utc(utc_start)
        end_dt = min(start_dt + timedelta(seconds=self._change_window_s), _utc_now_dt())
        utc_start_z = _format_utc_z(start_dt)
        utc_end_z = _format_utc_z(end_dt)

        changes = get_recent_changes_v2(
            base=self._base_url,
            namespace=self._namespace_id,
            utc_start=utc_start_z,
            utc_end=utc_end_z,
        )

        if changes:
            _logger.info(f"found {len(changes)} changes")
            newest_seen = max(v["timestamp"] for v in changes.values())
            hits = { title for title in changes if title in self._doc_map }
            return hits, newest_seen
        return None, utc_end_z

    def _update_doc_records(self, doc_updates):
        """
        doc_updates: list[{"title": <normalized_title>, "link": <link>, "timestamp": <utc>, "user": <user>}]
        Placeholder for the DB update step.
        """
        # TODO: implement:
        # - resolve link/title -> document record id
        # - refresh metadata for those docs
        # - write updates to DB
        if not self._runtime.database_updater:
            _logger.warning(f"WikiDocTracker: database updater unavailable - skipping document updates")
            return
        doc_links = [update["link"] for update in doc_updates]
        _logger.info(f"WikiDocTracker: updating {len(doc_links)} document record(s)")
        self._runtime.database_updater.update_doc_records(doc_links)

    def heartbeat(self):
        if not self._db:
            _logger.warning(f"WikiDocTracker: database unavailable - skipping heartbeat processing")
            return

        # Incremental doc discovery (slow-changing)
        self._refresh_doc_titles()

        # Scan wiki changes window and collect hits
        last_sentinel = self._get_wiki_sentinel()
        _logger.info(
            f"WikiDocTracker: heartbeat start: wiki sentinel={last_sentinel}, "
            f"docs={len(self._doc_map) if self._doc_map is not None else 0}"
        )
        hits, next_sentinel = self._get_wiki_changes(last_sentinel)

        # Process hits -> update doc records
        if hits:
            doc_updates = []
            for nt, info in hits.items():
                link = self._doc_map.get(nt)
                if link:
                    doc_updates.append({ 
                        "title": nt, 
                        "link": link, 
                        "timestamp": info.get("timestamp"), 
                        "user":info.get("user") 
                    })
                    _logger.info(f"doc changed: {nt} timestamp={info['timestamp']} user={info.get('user')}")

            if doc_updates:
                self._update_doc_records(doc_updates)

        # Advance wiki sentinel (monotonic)
        if next_sentinel:
            self._set_wiki_sentinel(next_sentinel)
            _logger.info(f"WikiDocTracker: scanned window to {next_sentinel}, hits={len(hits) if hits else 0}")
        else:
            _logger.info(f"WikiDocTracker: no changes found.")
        _logger.info("WikiDocTracker: heartbeat finish")

