# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

import time
import queue
from datetime import datetime, timedelta, UTC

import regex
from cachetools import LRUCache

from birddog.cache import load_cached_object, save_cached_object, CacheMissError
from birddog.core import (
    Archive,
    Page,
    )
from birddog.wiki import (
    ARCHIVE_BY_TITLE,
    ARCHIVE_BY_ADDRESS,
    canonicalize_title,
    archive_root,
    get_all_pages,
    page_address,
    PageTracker,
    page_title_from_address,
    )
from birddog.translate import TranslationManager
from birddog.store import get_string_queue_store
from birddog.env import detect_environment
from birddog.utility import HeartbeatManager

from birddog.logging import get_logger, ServiceLogger
_logger = get_logger()

# ----------------------------------------------------------------------------
# Page LRU memory cache

class PageLRU:
    class NotFoundError(Exception):
        def __init__(self, address):
            self._address = address
            super().__init__(f"Page not found: {address}")

        @property
        def address(self):
            return self._address

    def __init__(self, maxsize=100, reset_limit=5 * 60):
        self._reset_limit = reset_limit # seconds
        self._timer_start = time.time()
        self._lru = LRUCache(maxsize=maxsize)

    def key(self, title):
        return canonicalize_title(title)

    def _child_key(self, page, child_id):
        return f"{self.key(page.title)}/{child_id}"

    def lookup_child(self, page, child_id, runtime=None):
        return self.lookup_by_title(self._child_key(page, child_id), runtime=runtime)

    def lookup_by_address(self, archive, subarchive, fond=None, opus=None, case=None, runtime=None):
        archive_title = ARCHIVE_BY_ADDRESS[(archive, subarchive)]
        _logger.info(f"PageLRU.lookup_by_address({archive}, {subarchive}, {fond}, {opus}, {case}), archive={archive_title}")
        page = self.lookup_by_title(archive_title, runtime=runtime)
        if fond:
            page = page[fond]
            if opus:
                page = page[opus]
                if case:
                    page = page[case]
        return page

    def lookup_by_title(self, title, runtime=None):
        # periodically flush the lru to ensure the pages don't become stale
        if time.time() - self._timer_start >= self._reset_limit:
            _logger.info("PageLRU: flushing all entries")
            self._lru.clear()
            self._timer_start = time.time()

        title = canonicalize_title(title)
        try:
            page = self._lru[title]
            _logger.info(f"{f'PageLRU.lookup({title}): hit'}")
            return page
        except KeyError:
            _logger.info(f"{f'PageLRU.lookup({title}): miss'}")
            # FIXME: Archive should not be a subclass
            if title in ARCHIVE_BY_TITLE:
                page = Archive(*ARCHIVE_BY_TITLE[title], runtime=runtime)
            else:
                page = Page(title, runtime=runtime)
            self._lru[title] = page
            return page

# ----------------------------------------------------------------------------
# Update watcher

# Unicode-aware parsing
_DASH_CHARS = r"\-\u2010\u2011\u2012\u2013\u2014"
_ALPHA_DASH = fr"[\p{{L}}{_DASH_CHARS}]*"
_pattern = regex.compile(fr"^({_ALPHA_DASH})(\d+)({_ALPHA_DASH})$")

def _title_from_name(name):
    parts = name.split("/", 1)
    archive, subarchive = parts[0].split("-")
    if len(parts) == 1:
        return ARCHIVE_BY_ADDRESS[(archive, subarchive)]
    return f"{archive_root(archive, subarchive)}/{parts[1]}"

def _parse_string(s):
    match = _pattern.fullmatch(s)
    if match:
        prefix, number, suffix = match.groups()
        return (int(number), prefix, suffix)
    return (float('inf'), s, '')

def _sort_keys(keys):
    return sorted(keys, key=_parse_string)

def _flatten_hierarchy(d, prefix=None):
    result = []
    prefix = prefix or []

    children = [k for k in d if k != 'unresolved']
    sorted_children = _sort_keys(children)
    for key in sorted_children:
        current_path = prefix + [key]
        full_path_str = '/'.join(current_path)

        value = d[key]
        unresolved = value.get('unresolved') if isinstance(value, dict) else None

        result.append((full_path_str, unresolved))

        if isinstance(value, dict):
            result.extend(_flatten_hierarchy(value, current_path))

    def _gen_title(item):
        if not item[1]:
            return (item[0], { "title": _title_from_name(item[0]) })
        return item

    return [_gen_title(item) for item in result]

def _make_tree(unresolved):
    root = {}
    for key, value in unresolved.items():
        address = key.rstrip(',')
        address = address.replace(",", "-", 1)
        address = address.split(',')
        pos = root
        for item in address:
            if item not in pos:
                pos[item] = {}
            pos = pos[item]
        pos['unresolved'] = value
    return root

class ArchiveWatcher:
    def __init__(self, archive, subarchive, cutoff_date, runtime=None):
        self._runtime = runtime if runtime else Runtime()
        self._archive = archive
        if not subarchive:
            subarchive = Archive(archive).subarchive
        self._subarchive = subarchive
        self._cutoff_date = cutoff_date
        self._last_checked_date = cutoff_date
        self._resolved = {}
        self._unresolved = {}

    def save(self):
        return {
            'version': 'v4',
            'archive': self._archive,
            'subarchive': self._subarchive,
            'cutoff_date': self._cutoff_date,
            'resolved': self._resolved,
            'unresolved': self._unresolved,
            'last_checked_date': self._last_checked_date
        }

    @staticmethod
    def load(data, runtime=None):
        watcher = ArchiveWatcher(data['archive'], data['subarchive'], data['cutoff_date'], runtime=runtime)
        version = data.get("version", "v1")  # default to legacy

        # normalize unresolved (assume format is fine)
        watcher._unresolved = data.get('unresolved', {})
        watcher._last_checked_date = data.get('last_checked_date', watcher._cutoff_date)

        # normalize resolved entries for legacy versions
        if version == "v1":
            cutoff_date = watcher._cutoff_date
            watcher._resolved = {
                k: (
                    [{"modified": v, "last_resolved": cutoff_date}]
                    if not isinstance(v, list) else v
                )
                for k, v in data.get('resolved', {}).items()
            }
        else:
            watcher._resolved = data.get('resolved', {})

        if version == "v2":
            # patch missing titles in unresolved and resolved lists
            for key, unresolved_item in watcher._unresolved.items():
                if "title" not in unresolved_item:
                    unresolved_item["title"] = page_title_from_address(key.split(","))
            for key, resolved_items in watcher._resolved.items():
                for resolved_items in resolved_items:
                    if "title" not in resolved_items:
                        resolved_items["title"] = page_title_from_address(key.split(","))

        if version < "v4":
            # dispose of all unresolved items - force refresh
            watcher._unresolved = {}
            watcher.check()

        return watcher

    @staticmethod
    def key(archive, subarchive, fond=None, opus=None, case=None):
        return ','.join((archive, subarchive, fond or '', opus or '', case or ''))

    @property
    def resolved(self):
        return self._resolved

    @property
    def unresolved(self):
        return self._unresolved

    @property
    def cutoff_date(self):
        return self._cutoff_date

    @property
    def unresolved_tree(self):
        return _flatten_hierarchy(_make_tree(self.unresolved))

    def _last_resolved_date(self, item):
        entries = self._resolved.get(item, [])
        return entries[-1]["last_resolved"] if entries else self._cutoff_date

    def check(self):
        # register just in case it hasn't been already
        page_manager = self._runtime.update_manager
        # retrieve updates from page manager
        updates, _ = page_manager.get_updates(self._archive, self._subarchive)
        _logger.info(f"ArchiveWatcher.check: found {len(updates)} updates.")
        if updates:
            for title, mod_date in updates.items():
                address = page_address(title)
                assert address[0] == self._archive and address[1] == self._subarchive
                if len(address) >= 6:
                    _logger.info(f"Watcher.check: address={address}")
                item = ArchiveWatcher.key(*address)
                # Get most recent resolved mod date (if any)
                latest_resolved = self._resolved[item][-1]["modified"] if item in self._resolved else self._cutoff_date
                if latest_resolved is None or mod_date > latest_resolved:
                    self._unresolved[item] = {
                        "modified": mod_date,
                        "last_resolved": self._last_resolved_date(item),
                        "title": title
                    }
            #_logger.info(f'ArchiveWatcher.check() unresolved: {json.dumps(self._unresolved, indent=4)}')
            self._last_checked_date = max(max(updates.values()), self._last_checked_date)

    def resolve(self, item, deep=False):
        #_logger.info(f'ArchiveWatcher.resolve: before\n\tunresolved: {self._unresolved}\n\tresolved: {self._resolved}')
        if deep:
            _logger.info(f'ArchiveWatcher: deep resolve: {item}')
            item = item.rstrip(',').split(",")
            for key in list(self.unresolved.keys()):
                split_key = key.split(",")[:len(item)]
                if split_key == item:
                    unresolved_item = self._unresolved.pop(key)
                    _logger.info(f'ArchiveWatcher: deep resolving subitem: {key}; {item}; {unresolved_item}')
                    self._resolved.setdefault(key, []).append(unresolved_item)
        elif item in self._unresolved:
            unresolved_item = self._unresolved.pop(item)
            self._resolved.setdefault(item, []).append(unresolved_item)
        #_logger.info(f'ArchiveWatcher.resolve: after\n\tunresolved: {self._unresolved}\n\tresolved: {self._resolved}')

    def unresolve(self, item):
        if item in self._resolved and self._resolved[item]:
            self._unresolved[item] = self._resolved[item].pop()
            if not self._resolved[item]:  # Clean up empty lists
                del self._resolved[item]

# ----------------------------------------------------------------------------
# Page Update Manager

class PageUpdateManager(HeartbeatManager):
    _PAGE_UPDATE_MANAGER_PATH       = "page_update_manager.json"
    _PENDING_TITLES_QUEUE           = "pending_titles"
    _HEARTBEAT_INTERVAL             = 30 # seconds
    _PAGE_UPDATE_CHECK_INTERVAL     = 60 * 15 # seconds
    _API_DELAY                      = 1
    _TITLE_BATCH_SIZE               = max(int(_HEARTBEAT_INTERVAL / _API_DELAY + .5), 1)

    def __init__(self, page_lru=None):
        _logger.info(f"PageUpdateManager.init(): detect_environment=={detect_environment()}")
        self._lru = page_lru if page_lru else PageLRU()
        self._request_queue = queue.Queue()
        self._queue_store = get_string_queue_store()
        self._last_check = 0
        self._busy = False
        super().__init__(interval=PageUpdateManager._HEARTBEAT_INTERVAL)
        self.load()

    def _append_titles(self, titles):
        self._queue_store.append(PageUpdateManager._PENDING_TITLES_QUEUE, titles)

    def _peek_titles(self, n):
        return self._queue_store.peek(PageUpdateManager._PENDING_TITLES_QUEUE, n)

    def _pop_titles(self, n):
        return self._queue_store.pop(PageUpdateManager._PENDING_TITLES_QUEUE, n)

    def load(self):
        try:
            data = load_cached_object(PageUpdateManager._PAGE_UPDATE_MANAGER_PATH)
            self._tracker = PageTracker.from_dict(data["tracker"])
            self._registered_archives = set(data["registered_archives"])

            # one time check for old format
            pending_titles = data.get("pending_titles")
            if pending_titles:
                self._append_titles(pending_titles)
                self.save()

        except CacheMissError:
            self._tracker = PageTracker()
            self._registered_archives = set()

    def save(self):
        save_cached_object({
            "tracker": self._tracker.to_dict(),
            "registered_archives": list(self._registered_archives),
        }, PageUpdateManager._PAGE_UPDATE_MANAGER_PATH)

    @property
    def busy(self):
        return not self._request_queue.empty() or self._busy

    def heartbeat(self):
        _logger.info("PageUpdateManager heartbeat...")
        self._busy = True

        # 1. Update tracker periodically
        now = time.time()
        if now - self._last_check > PageUpdateManager._PAGE_UPDATE_CHECK_INTERVAL:
            _logger.info("PageUpdateManager: checking for page updates...")
            if self._tracker.update():
                self.save()
            _logger.info("PageUpdateManager: finished update check...")
            self._last_check = now
            self._busy = False
            return

        # 2. Process registration requests
        if not self._request_queue.empty():
            try:
                archive, subarchive = self._request_queue.get_nowait()
                key = f"{archive}-{subarchive}"
                if key not in self._registered_archives:
                    _logger.info(f"PageUpdateManager: registering {archive}-{subarchive}")
                    root = archive_root(archive, subarchive)
                    # inventory all pages that start with archive root and add latest mod times to tracker
                    titles = get_all_pages(prefix=root)
                    if titles:
                        self._append_titles(titles)
                    self._registered_archives.add(key)
                    self.save()
                    self._busy = False
                    return
            except queue.Empty:
                pass  # should not happen, but safe

        # 3. Insert batch of pending titles into tracker
        # FIXME: This won't work if more than one Runtime instance is running
        # FIXME: Atomically pop the batch and reinsert them if add_titles fails?
        batch = self._peek_titles(PageUpdateManager._TITLE_BATCH_SIZE)
        if batch:
            _logger.info(f"PageUpdateManager: adding batch of {len(batch)} titles to tracker")
            self._tracker.add_titles(batch, api_delay=PageUpdateManager._API_DELAY)
            # finally delete batch from pending titles (after successfully adding)
            self._pop_titles(len(batch))
        self._busy = False

    def register_archive(self, archive, subarchive):
        # let the tracker know there's interest in a particular archive
        # insert (archive, subarchive) into request queue
        self._request_queue.put((archive, subarchive))

    def get_updates(self, archive, subarchive, cutoff_date=None):
        self.register_archive(archive, subarchive)
        prefix = archive_root(archive, subarchive)
        _logger.info(f"PageUpdateManager.get_updates: prefix={prefix}")
        updates_pending = self.busy
        updates = self._tracker.get_updates(prefix, cutoff_date=cutoff_date)
        _logger.info(f"PageUpdateManager.get_updates: {len(updates)} total updates")
        result = {}
        for title, mod_date in updates.items():
            try:
                address = page_address(title)
                if address[:2] == (archive, subarchive):
                    result[title] = mod_date
            except ValueError:
                _logger.error(f"PageUpdateManager.get_updates: cannot find title {title}. Skipping...")
        return result, updates_pending

# ----------------------------------------------------------------------------
# Resource usage monitor

_KILL_LIMITS = [
    {
        "timedelta": { "minutes": 5},
        "limits": [
            {
                "resource": "StringQueue",
                "metric": "count_per_minute",
                "threshold": 300
            },
            {
                "resource": "KVStore",
                "metric": "count_per_minute",
                "threshold": 1000
            },
            {
                "resource": "ModDateStore",
                "metric": "count_per_minute",
                "threshold": 50
            },
            {
                "resource": "ModDateStore",
                "metric": "size_per_minute",
                "threshold": 50000000
            },
            {
                "resource": "DummyTranslator",
                "metric": "size_per_minute",
                "threshold": 50000
            },
            {
                "resource": "DummyTranslator",
                "metric": "count_per_minute",
                "threshold": 100
            },
            {
                "resource": "GoogleCloudTranslate",
                "metric": "size_per_minute",
                "threshold": 50000
            },
            {
                "resource": "GoogleCloudTranslate",
                "metric": "count_per_minute",
                "threshold": 100
            },
        ]
    },
    {
        "timedelta": { "minutes": 30},
        "limits": [
            {
                "resource": "StringQueue",
                "metric": "count_per_minute",
                "threshold": 100
            },
            {
                "resource": "KVStore",
                "metric": "count_per_minute",
                "threshold": 1000
            },
            {
                "resource": "ModDateStore",
                "metric": "count_per_minute",
                "threshold": 50
            },
            {
                "resource": "ModDateStore",
                "metric": "size_per_minute",
                "threshold": 10000000
            },
            {
                "resource": "DummyTranslator",
                "metric": "size_per_minute",
                "threshold": 5000
            },
            {
                "resource": "DummyTranslator",
                "metric": "count_per_minute",
                "threshold": 50
            },
            {
                "resource": "GoogleCloudTranslate",
                "metric": "size_per_minute",
                "threshold": 5000
            },
            {
                "resource": "GoogleCloudTranslate",
                "metric": "count_per_minute",
                "threshold": 50
            },
        ]
    },
]

class KillSwitch(HeartbeatManager):
    _HEARTBEAT_INTERVAL             = 30 # seconds

    def __init__(self, runtime):
        self._runtime = runtime
        super().__init__(interval=KillSwitch._HEARTBEAT_INTERVAL)

    def _trigger(self, resource, metric, threshold, value):
        _logger.info(f"KillSwitch._trigger(resource={resource}, metric={metric}, threshold={threshold}, value={value})")
        time.sleep(1)
        self._runtime.pause()

    def heartbeat(self):
        #_logger.info("KillSwitch heartbeat...")
        now = datetime.now(UTC)
        for limit_spec in _KILL_LIMITS:
            delta = timedelta(**limit_spec["timedelta"])
            df = ServiceLogger.get_logger().load_logs(now - delta, now)
            if not df.empty:
                summary = ServiceLogger.summarize_service_usage(
                    df,
                    by="resource",
                    sample_interval_minutes=delta.total_seconds() / 60.)
                for limit in limit_spec["limits"]:
                    resource = limit["resource"]
                    threshold = limit["threshold"]
                    metric = limit["metric"]
                    stat = summary.loc[summary["resource"].eq(resource), metric]
                    value = int(stat.iloc[0]) if not stat.empty else 0
                    #_logger.info(f'killswitch: resource={resource}, metric={metric}, value={value}')
                    if value >= threshold:
                        self._trigger(resource, metric, threshold, value)
                        return


# ----------------------------------------------------------------------------
# Birddog Runtime

class Runtime:
    _LRU_SIZE                = 500

    def __init__(self):
        self._page_lru = PageLRU(maxsize=Runtime._LRU_SIZE)
        self._update_manager = PageUpdateManager(page_lru=self._page_lru)
        self._translation_manager = TranslationManager(self)
        self._killswitch = KillSwitch(self)
        self._state = "ready"

    @property
    def page_lru(self):
        return self._page_lru

    @property
    def update_manager(self):
        return self._update_manager

    def start(self):
        if self._state == "ready":
            _logger.info(f"Runtime starting...")
            self._update_manager.start()
            if self.translation_enabled:
                self._translation_manager.start()
            self._killswitch.start()
            self._state = "running"

    def pause(self):
        if self._state == "running":
            # pause all threads
            _logger.info("Runtime pausing")
            self._update_manager.hold()
            self._translation_manager.hold()
            self._state = "paused"

    def unpause(self):
        if self._state == "paused":
            # unpause all threads
            _logger.info("Runtime unpausing")
            self._update_manager.release()
            self._translation_manager.release()
            self._state = "running"

    def lookup_address(self, title):
        return page_address(title)

    def lookup_by_address(self, archive, subarchive, fond=None, opus=None, case=None):
        return self._page_lru.lookup_by_address(archive, subarchive, fond, opus, case, runtime=self)

    def lookup_by_title(self, title):
        return self._page_lru.lookup_by_title(title, runtime=self)

    def start_translation(self, page):
        if self.translation_enabled:
            self._translation_manager.translate(page)

    @property
    def active_translations(self):
        return self._translation_manager.active_tasks()

    @property
    def translation_available(self):
        return self._translation_manager.available

    @property
    def translation_enabled(self):
        return self._translation_manager.enabled

    @property
    def state(self):
        return self._state
