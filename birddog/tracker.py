# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

import hashlib
import json
import os
from urllib.parse import unquote

from datetime import datetime, timedelta, timezone
import time

from birddog.wiki import (
    get_recent_changes,
    get_all_pages,
    batch_page_exists,
    get_last_mod,
    canonicalize_title,
    lookup_namespace_id,
    )
from birddog.store import KeyValueStore
from birddog.utility import json_size, HeartbeatManager, utc_now_dt, to_utc_format, from_utc_format
from birddog.log import get_logger, LogService
_logger = get_logger()

_TRACKER_KV_TABLE = "bd_tracker_kv_store"
_TRACKER_KV_NS_TRACKER    = "pt"
_TRACKER_KV_NS_CHANGE_LOG = "pcl"


class PageChangeLog:
    def __init__(self):
        self._kv = KeyValueStore(table_name=_TRACKER_KV_TABLE)
        self._changes = {
            k: json.loads(v)
            for k, v in self._kv.get_all(_TRACKER_KV_NS_CHANGE_LOG)
        }

    def size(self):
        return len(self._changes)

    def oldest(self):
        if not self._changes:
            raise ValueError("empty page change log")
        return min(v["timestamp"] for v in self._changes.values())

    def newest(self):
        if not self._changes:
            raise ValueError("empty page change log")
        return max(v["timestamp"] for v in self._changes.values())

    def refresh(self):
        cutoff_date = self.newest() if self._changes else None
        utc_start = to_utc_format(cutoff_date) if cutoff_date else None
        updates = get_recent_changes(utc_start=utc_start)
        if updates:
            _logger.info(f"PageChangeLog: recording {len(updates)} new page changes")
            updates = {
                canonicalize_title(title): update
                for title, update in updates.items()
            }
            for title, update in updates.items():
                entry = {
                    "timestamp": from_utc_format(update["timestamp"]),
                    "user": update["user"],
                    "action": update.get("action"),
                }
                self._kv.insert(_TRACKER_KV_NS_CHANGE_LOG, title, json.dumps(entry))
                self._changes[title] = entry

    def get(self):
        return dict(self._changes)

class PageTracker:
    def __init__(self):
        self._change_log = PageChangeLog()
        self._kv = KeyValueStore(table_name=_TRACKER_KV_TABLE)
        self._load_cache()

    def _load_cache(self):
        self._page_dict = {
            k: json.loads(v)
            for k, v in self._kv.get_all(_TRACKER_KV_NS_TRACKER)
        }

    def reset(self, all_titles=None):
        if not all_titles:
            _logger.info("PageTracker.reset: generating title inventory for archive...")
            all_titles = get_all_pages()
        self._kv.remove_all(_TRACKER_KV_NS_TRACKER)
        if all_titles:
            _logger.info(f"PageTracker.reset: inserting {len(all_titles)} titles into tracker")
            for title, item in all_titles.items():
                self._kv.insert(_TRACKER_KV_NS_TRACKER, title, json.dumps(item))
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
            for title, item in newer_updates.items():
                self._kv.insert(_TRACKER_KV_NS_TRACKER, title, json.dumps(item))
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
                for title, item in updates.items():
                    self._kv.insert(_TRACKER_KV_NS_TRACKER, title, json.dumps(item))
                    self._page_dict[title] = item
            if bad_titles:
                # take non-existent titles out of tracker table
                _logger.info(f"PageTracker: removing {len(bad_titles)} non-existent titles")
                for title in bad_titles:
                    self._kv.remove(_TRACKER_KV_NS_TRACKER, title)
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

def process_tracker_unknowns():
    from time import sleep
    tracker = PageTracker()
    still_more = True
    while still_more:
        still_more = tracker.initialize_batch_of_unknowns()
        sleep(1)

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


def offset_utc(ts, seconds):
    dt = _parse_utc(ts)
    return _format_utc_z(dt + timedelta(seconds=seconds))

_WIKI_DOC_TRACKER_KV_TABLE = "bd_doc_tracker"
_WIKI_DOC_TRACKER_HEARTBEAT_INTERVAL = 300  # seconds
_WIKI_SENTINEL = "WIKI_SENTINEL"
_WIKI_CHANGE_EVENT_WINDOW = 1200  # seconds
_DOC_TABLE_SENTINEL = "DOC_SENTINEL"

WIKIMEDIA_COMMONS_DOC_TRACKER_SPEC = {
    "base_url": "https://commons.wikimedia.org",
    "namespace": "File",
    "table_view": "BD:WDT",
}

UK_WIKISOURCE_DOC_TRACKER_SPEC = {
    "base_url": "https://uk.wikisource.org",
    "namespace": "Файл",
    "table_view": "BD:WDT",
}

class WikiDocTracker(HeartbeatManager):
    def __init__(
        self,
        runtime,
        spec=None,
        cutoff_time=None,
        change_window_s=_WIKI_CHANGE_EVENT_WINDOW,
    ):
        if spec is None:
            spec = WIKIMEDIA_COMMONS_DOC_TRACKER_SPEC
        self._runtime = runtime
        self._base_url = spec["base_url"]
        self._namespace = spec["namespace"]
        self._table_view = spec["table_view"]
        self._db = self._runtime.database

        self._namespace_id = lookup_namespace_id(self._namespace)
        self._kv = KeyValueStore(table_name=_WIKI_DOC_TRACKER_KV_TABLE)

        self._doc_kv_namespace = f"{self._base_url}:{self._namespace}"
        self._sentinel_kv_namespace = f"{self._base_url}:{self._namespace}:SENTINELS"

        self._cutoff_time = _format_utc_z(cutoff_time) if cutoff_time else None
        self._change_window_s = int(change_window_s)

        # In-memory doc index (normalized titles). None means "not loaded yet".
        self._doc_map = None  # set[str] of normalized titles

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
        return unquote(title).replace(" ", "_")

    def _kv_key(self, normalized_title):
        # form a fixed length key from the title, since raw title can exceed DDB key length limit
        return hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()

    def _link_from_title(self, normalized_title):
        return f"{self._base_url}/wiki/{normalized_title}"

    def _ensure_doc_map(self):
        if self._doc_map is None:
            self._doc_map = {k for k, _ in self._kv.get_all(self._doc_kv_namespace)}
            _logger.info(f"WikiDocTracker: loaded {len(self._doc_map)} doc titles into memory")

    def _store_relevant_titles(self, records):
        """
        Store relevant titles in KV and update in-memory doc_map incrementally.
        """
        inserts = 0
        for rec in records:
            link = rec.get("url", "")
            if not link or not link.startswith(self._base_url):
                continue

            title = rec.get("title")
            if not title:
                continue

            nt = self._normalize_title(title)
            kv_key = self._kv_key(nt)

            if kv_key in self._doc_map:
                continue

            self._kv.insert(self._doc_kv_namespace, kv_key, "")
            self._doc_map.add(kv_key)
            inserts += 1

        if inserts:
            _logger.info(f"WikiDocTracker: inserted {inserts} new doc titles")

    def _refresh_doc_titles(self):
        if not self._db:
            _logger.warning(f"WikiDocTracker: database unavailable - skipping document title refresh")
            return
        self._ensure_doc_map()

        cursor = None
        while True:
            batch, cursor = self._db.scan(
                "Documents",
                cursor=cursor,
                limit=100,
                view_name=self._table_view,
                fields=["title", "url", "CreatedAt"])
            if not batch:
                break
            self._store_relevant_titles(batch)
            if not cursor:
                break

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
        end_dt = min(start_dt + timedelta(seconds=self._change_window_s), utc_now_dt())
        utc_start_z = _format_utc_z(start_dt)
        utc_end_z = _format_utc_z(end_dt)

        changes = get_recent_changes(
            base=self._base_url,
            namespace=self._namespace_id,
            utc_start=utc_start_z,
            utc_end=utc_end_z,
        )

        if changes:
            _logger.info(f"WikiDocTracker ({self._base_url}): found {len(changes)} changes")
            newest_seen = max(v["timestamp"] for v in changes.values())
            hits = {
                self._normalize_title(title): changes[title]
                for title in changes
                if self._kv_key(self._normalize_title(title)) in self._doc_map
            }
            return hits, newest_seen
        return None, utc_end_z

    def _update_doc_records(self, doc_updates):
        doc_links = [update["url"] for update in doc_updates]
        _logger.info(f"WikiDocTracker ({self._base_url}): updating {len(doc_links)} document record(s)")
        self._runtime.update_documents_to_database(doc_links)

    def heartbeat(self):
        if not self._db or not self._runtime.database_update_enabled:
            _logger.warning(f"WikiDocTracker ({self._base_url}): database unavailable - skipping heartbeat processing")
            return

        # Incremental doc discovery (slow-changing)
        self._refresh_doc_titles()

        # Scan wiki changes window and collect hits
        try:
            last_sentinel = self._get_wiki_sentinel()
        except ValueError:
            # No sentinel set yet — start from now and track forward only.
            last_sentinel = offset_utc(utc_now_dt(), -self._change_window_s)
            self._set_wiki_sentinel(last_sentinel)
            _logger.info(f"WikiDocTracker: initialized wiki sentinel to {last_sentinel}")
        _logger.info(
            f"WikiDocTracker: heartbeat start: wiki sentinel={last_sentinel}, "
            f"docs={len(self._doc_map) if self._doc_map is not None else 0}"
        )
        hits, next_sentinel = self._get_wiki_changes(last_sentinel)

        # Process hits -> update doc records
        if hits:
            doc_updates = []
            for nt, info in hits.items():
                doc_updates.append({
                    "title": nt,
                    "url": self._link_from_title(nt),
                    "timestamp": info.get("timestamp"),
                    "user": info.get("user"),
                })
                _logger.info(f"WikiDocTracker ({self._base_url}): doc changed: {nt} timestamp={info['timestamp']} user={info.get('user')}")

            if doc_updates:
                self._update_doc_records(doc_updates)

        # Advance wiki sentinel (monotonic)
        if next_sentinel:
            self._set_wiki_sentinel(next_sentinel)
            #_logger.info(f"WikiDocTracker: scanned window to {next_sentinel}, hits={len(hits) if hits else 0}")
        #else:
        #    _logger.info(f"WikiDocTracker: no changes found.")
        #_logger.info("WikiDocTracker: heartbeat finish")
