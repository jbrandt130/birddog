# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

import json
import os
import threading
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
    register_archive_root,
    )
from birddog.database_updater import normalize_url
from birddog.store import KeyValueStore
from birddog.utility import json_size, HeartbeatManager, utc_now_dt
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
        utc_start = self.newest() if self._changes else None
        updates = get_recent_changes(utc_start=utc_start)
        if updates:
            _logger.info(f"PageChangeLog: recording {len(updates)} new page changes")
            updates = {
                canonicalize_title(title): update
                for title, update in updates.items()
            }
            for title, update in updates.items():
                entry = {
                    "timestamp": update["timestamp"],
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
        self._discover_archive_roots(self._page_dict.keys())

    def _discover_archive_roots(self, titles):
        # a root archive is any title with no further "/" path segments;
        # register_archive_root() no-ops on anything else and is a cheap
        # cache-hit for titles already known, so this is safe to call freely.
        # This is a best-effort side effect (labeling), never allowed to
        # break the tracker's actual job of tracking page updates.
        for title in titles:
            try:
                register_archive_root(title)
            except Exception:
                _logger.exception(f"PageTracker: failed to register archive root for {title!r}")

    def reset(self, all_titles=None):
        if not all_titles:
            _logger.info("PageTracker.reset: generating title inventory for archive...")
            all_titles = get_all_pages()
        if not isinstance(all_titles, dict):
            # get_all_pages() (and any other title-list source) returns titles with
            # no known timestamp yet; seed them as unknowns for initialize_batch_of_unknowns().
            all_titles = {title: {} for title in all_titles}
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
            self._discover_archive_roots(newer_updates.keys())
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

_DOCUMENT_MAP_TABLE_VIEW = "BD:WDT"
_DOCUMENT_MAP_ID_BATCH_SIZE = 10000

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


def _canonical_doc_url(base_url, title):
    # must match database_updater.normalize_url exactly -- that's what every
    # Document.url was written through (wiki-scrape, spreadsheet import, and
    # this tracker's own doc updates all funnel through form_document_record(),
    # which normalizes via the same function). A weaker/different
    # normalization here (as the old _link_from_title did) can silently
    # miss an already-known document -- e.g. a differently-cased namespace
    # prefix ("File:" vs "file:") normalizes to a different string, so a
    # title that IS already tracked looks unknown, and the resulting "create"
    # mints a duplicate, ownerless Document (observed on commons.wikimedia.org,
    # 2026-08-04, which is why normalize_url canonicalizes namespace case at all).
    normalized_title = unquote(title).replace(" ", "_")
    return normalize_url(f"{base_url}/wiki/{normalized_title}")


class DocumentMap:
    """
    In-process cache mapping every known Document's canonical url to its
    {title, id}, shared by every WikiDocTracker instance (one per wiki site)
    so the (potentially large) full-table build happens once, not once per
    site. Not persisted -- rebuilt from scratch each process lifetime, so it
    can never silently drift out of sync with Documents across a restart the
    way a persisted mirror could.

    Built in batches across repeated refresh() calls rather than one long
    blocking scan:
      1. first call: snapshot every Documents id (self._db.get_all_ids), set
         the scan cursor to 0.
      2. each following call while the cursor hasn't reached the end of that
         snapshot: read the next batch of (url, title, Id) by explicit id and
         advance the cursor. Reading by a fixed, pre-captured id list (rather
         than re-paginating a live, growing table) means a document created
         mid-build can't cause a row to be skipped or double-counted the way
         offset-based pagination against a mutating table could.
      3. once the snapshot is fully drained: do an incremental refresh via the
         BD:WDT moving-window view (same idea as the old _refresh_doc_titles)
         to pick up documents created since the snapshot was taken, extend the
         id list with them, and pin the cursor back at the end -- so every
         call from here on lands in this branch. Returns True.

    refresh() returns True only once the map is fully current (case 3);
    False while still building (cases 1-2) or when it declines to do
    anything this call (see thread-safety below). Callers are expected to
    skip wiki-change processing entirely while refresh() returns False --
    the conservative assumption this relies on is that the full build (a few
    hundred thousand documents, done in ~10,000-id batches) completes in well
    under a day, so nothing created during the build can have aged out of the
    BD:WDT window by the time the first incremental refresh runs. Any wiki
    edits that happen during the build aren't lost, just delayed: the
    existing wiki-sentinel/window mechanism already tolerates catching up
    over several heartbeats.

    refresh() is safe to call from multiple threads at once (both
    WikiDocTracker instances share one DocumentMap), but never blocks: if
    another thread is already mid-refresh, it returns False immediately
    rather than waiting, so a heartbeat is never held up by the other site's
    tracker. In steady state this means an occasionally-contended tick just
    skips that one cycle's wiki-change processing for whichever tracker lost
    the race -- harmless, since the next heartbeat (300s later) tries again
    and the sentinel/window mechanism tolerates the delay the same way it
    tolerates any other startup or backlog delay.
    """

    def __init__(self, db):
        self._db = db
        self._lock = threading.Lock()
        self._doc_ids = None       # None until the first refresh() call
        self._known_ids = set()    # mirror of _doc_ids, for O(1) dedup
        self._scan_cursor = None
        self._doc_url = {}         # canonical url -> {"title":..., "id":...}
        self._init_logged = False  # so the "doc map init done" log only fires once

    @property
    def size(self):
        return len(self._doc_url)

    @property
    def build_complete(self):
        return self._doc_ids is not None and self._scan_cursor == len(self._doc_ids)

    def refresh(self):
        if not self._lock.acquire(blocking=False):
            return False
        try:
            return self._refresh_locked()
        finally:
            self._lock.release()

    def _refresh_locked(self):
        if self._doc_ids is None:
            _logger.info("DocumentMap: fetching document ids...")
            self._doc_ids = self._db.get_all_ids("Documents")
            self._known_ids = set(self._doc_ids)
            self._scan_cursor = 0
            _logger.info(f"DocumentMap: id fetch done, {len(self._doc_ids)} document id(s) to load")
            return False

        if self._scan_cursor < len(self._doc_ids):
            batch_ids = self._doc_ids[self._scan_cursor:self._scan_cursor + _DOCUMENT_MAP_ID_BATCH_SIZE]
            records = self._db.read("Documents", batch_ids, fields=["url", "title", "Id"])
            self._store_records(records)
            self._scan_cursor += len(batch_ids)
            _logger.info(f"DocumentMap: build progress {self._scan_cursor}/{len(self._doc_ids)}")
            return False

        self._incremental_refresh()
        if not self._init_logged:
            _logger.info(f"DocumentMap: doc map init done, {self.size} document(s) known")
            self._init_logged = True
        return True

    def _store_records(self, records):
        for rec in records:
            url = rec.get("url")
            title = rec.get("title")
            if not url or not title:
                continue
            self._doc_url[normalize_url(url)] = {"title": title, "id": rec.get("Id")}

    def _incremental_refresh(self):
        cursor = None
        new_ids = []
        while True:
            batch, cursor = self._db.scan(
                "Documents",
                cursor=cursor,
                limit=1000,
                view_name=_DOCUMENT_MAP_TABLE_VIEW,
                fields=["title", "url", "Id"])
            if not batch:
                break
            self._store_records(batch)
            for rec in batch:
                did = rec.get("Id")
                if did is not None and did not in self._known_ids:
                    self._known_ids.add(did)
                    new_ids.append(did)
            if not cursor:
                break
        if new_ids:
            self._doc_ids.extend(new_ids)
            _logger.info(f"DocumentMap: incremental refresh added {len(new_ids)} new document id(s)")
        self._scan_cursor = len(self._doc_ids)

    def check_titles(self, base_url, titles):
        """Return {title: url} for every title in `titles` that resolves (via
        base_url) to a url already present in the map."""
        result = {}
        for title in titles:
            url = _canonical_doc_url(base_url, title)
            if url in self._doc_url:
                result[title] = url
        return result


class WikiDocTracker(HeartbeatManager):
    def __init__(
        self,
        runtime,
        spec=None,
        cutoff_time=None,
        change_window_s=_WIKI_CHANGE_EVENT_WINDOW,
        doc_map=None,
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

        self._sentinel_kv_namespace = f"{self._base_url}:{self._namespace}:SENTINELS"

        self._cutoff_time = _format_utc_z(cutoff_time) if cutoff_time else None
        self._change_window_s = int(change_window_s)

        # shared across every WikiDocTracker instance when passed in by the
        # caller (see runtime.py); falls back to a private one so a tracker
        # can still be constructed standalone (e.g. in tests).
        self._doc_map = doc_map if doc_map is not None else DocumentMap(self._db)

        _logger.info(
            f"WikiDocTracker: base={self._base_url}, "
            f"namespace={self._namespace} (id={self._namespace_id})"
        )

        super().__init__(interval=_WIKI_DOC_TRACKER_HEARTBEAT_INTERVAL)

    def _reset(self):
        self._kv.remove_all(self._sentinel_kv_namespace)

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
            known = self._doc_map.check_titles(self._base_url, changes.keys())
            hits = {
                title: {**changes[title], "url": known[title]}
                for title in changes
                if title in known
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

        # doc_map is shared with the other site's tracker; only proceed with
        # wiki-change processing once it reports itself current (see
        # DocumentMap docstring for why this is safe to skip otherwise). A
        # False here has two very different causes worth distinguishing in the
        # log: the map is still (batch-)building, or it's already built and
        # this cycle just lost the non-blocking lock race to the other
        # tracker's own refresh -- the latter is harmless and self-resolves on
        # this tracker's next heartbeat.
        if not self._doc_map.refresh():
            if self._doc_map.build_complete:
                _logger.info(
                    f"WikiDocTracker ({self._base_url}): document map already built "
                    f"({self._doc_map.size} known) but busy with the other tracker's "
                    f"refresh this instant - skipping this cycle, will retry next heartbeat"
                )
            else:
                _logger.info(
                    f"WikiDocTracker ({self._base_url}): document map still building "
                    f"({self._doc_map.size} known so far) - skipping this cycle"
                )
            return

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
            f"docs={self._doc_map.size}"
        )
        hits, next_sentinel = self._get_wiki_changes(last_sentinel)

        # Process hits -> update doc records
        if hits:
            doc_updates = []
            for title, info in hits.items():
                doc_updates.append({
                    "title": title,
                    "url": info["url"],
                    "timestamp": info.get("timestamp"),
                    "user": info.get("user"),
                })
                _logger.info(f"WikiDocTracker ({self._base_url}): doc changed: {title} timestamp={info.get('timestamp')} user={info.get('user')}")

            if doc_updates:
                self._update_doc_records(doc_updates)

        # Advance wiki sentinel (monotonic)
        if next_sentinel:
            self._set_wiki_sentinel(next_sentinel)
            #_logger.info(f"WikiDocTracker: scanned window to {next_sentinel}, hits={len(hits) if hits else 0}")
        #else:
        #    _logger.info(f"WikiDocTracker: no changes found.")
        #_logger.info("WikiDocTracker: heartbeat finish")
