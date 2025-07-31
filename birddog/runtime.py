# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

import time
import threading
import queue

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
    )

from birddog.logging import get_logger
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

    def __init__(self, maxsize=500, reset_limit=60 * 60):
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

    return result

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
    def __init__(self, archive, subarchive, cutoff_date):
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
            'version': 'v2',
            'archive': self._archive,
            'subarchive': self._subarchive,
            'cutoff_date': self._cutoff_date,
            'resolved': self._resolved,
            'unresolved': self._unresolved,
            'last_checked_date': self._last_checked_date
        }

    @staticmethod
    def load(data):
        watcher = ArchiveWatcher(data['archive'], data['subarchive'], data['cutoff_date'])
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

    def check(self, page_manager):
        # register just in case it hasn't been already
        page_manager.register_archive(self._archive, self._subarchive)
        # retrieve updates from page manager
        updates, _ = page_manager.get_updates(self._archive, self._subarchive, self._last_checked_date)
        if updates:
            for item, mod_date in updates.items():
                # Get most recent resolved mod date (if any)
                latest_resolved = self._resolved[item][-1]["modified"] if item in self._resolved else None
                if mod_date >= self._last_checked_date and (latest_resolved is None or mod_date > latest_resolved):
                    self._unresolved[item] = {
                        "modified": mod_date,
                        "last_resolved": self._last_resolved_date(item)
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

class HeartbeatManager:
    def __init__(self, interval=1.0):
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_heartbeat, daemon=True)
        self._started = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def start(self):
        if not self._started:
            self._thread.start()
            self._started = True

    def _run_heartbeat(self):
        while not self._stop_event.is_set():
            try:
                self.heartbeat()
            except Exception as e:
                _logger.info(f"Heartbeat error: {e}")
            time.sleep(self.interval)

    def heartbeat(self):
        """Override this method in subclasses to perform periodic actions."""
        _logger.info("Heartbeat...")

    def stop(self):
        self._stop_event.set()
        self._thread.join()

class PageUpdateManager(HeartbeatManager):
    _PAGE_UPDATE_MANAGER_PATH       = "page_update_manager.json"
    _HEARTBEAT_INTERVAL             = 30 # seconds
    _PAGE_UPDATE_CHECK_INTERVAL     = 60 * 15 # seconds
    _TITLE_BATCH_SIZE               = 500

    def __init__(self, page_lru=None):
        self._lru = page_lru if page_lru else PageLRU()
        self._request_queue = queue.Queue()
        self._last_check = 0
        self._busy = False
        super().__init__(interval=PageUpdateManager._HEARTBEAT_INTERVAL)
        self.load()

    def load(self):
        try:
            data = load_cached_object(PageUpdateManager._PAGE_UPDATE_MANAGER_PATH)
            self._tracker = PageTracker.from_dict(data["tracker"])
            self._pending_titles = data["pending_titles"]
            self._registered_archives = set(data["registered_archives"])
        except CacheMissError:
            self._tracker = PageTracker()
            self._pending_titles = []
            self._registered_archives = set()

    def save(self):
        save_cached_object({
            "tracker": self._tracker.to_dict(),
            "pending_titles": self._pending_titles,
            "registered_archives": list(self._registered_archives),
        }, PageUpdateManager._PAGE_UPDATE_MANAGER_PATH)

    @property
    def busy(self):
        return not self._request_queue.empty() or self._busy

    def heartbeat(self):
        #_logger.info("PageUpdateManager heartbeat...")
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
                        self._pending_titles.extend(titles)
                        self._registered_archives.add(key)
                        self.save()
                    self._busy = False
                    return
            except queue.Empty:
                pass  # should not happen, but safe

        # 3. Insert batch of pending titles into tracker
        if self._pending_titles:
            batch = self._pending_titles[:PageUpdateManager._TITLE_BATCH_SIZE]
            _logger.info(f"PageUpdateManager: adding batch of {len(batch)} titles to tracker")
            del self._pending_titles[:PageUpdateManager._TITLE_BATCH_SIZE]
            self._tracker.add_titles(batch)
            self.save()
        self._busy = False

    def register_archive(self, archive, subarchive):
        # let the tracker know there's interest in a particular archive
        # insert (archive, subarchive) into request queue
        self._request_queue.put((archive, subarchive))

    def get_updates(self, archive, subarchive, cutoff_date=None):
        prefix = archive_root(archive, subarchive)
        _logger.info(f"PageUpdateManager.get_updates: prefix={prefix}")
        updates_pending = self.busy
        updates = self._tracker.get_updates(prefix, cutoff_date=cutoff_date)
        _logger.info(f"PageUpdateManager.get_updates: {len(updates)} total updates")
        result = {}
        for title, mod_date in updates.items():
            try:
                # FIXME: remove address translation - ArchiveWatcher should run on titles
                address = page_address(title)
                if address[:2] == (archive, subarchive):
                    key = ArchiveWatcher.key(*address)
                    result[key] = mod_date
            except ValueError:
                _logger.error(f"PageUpdateManager.get_updates: cannot find title {title}. Skipping...")
        return result, updates_pending

# ----------------------------------------------------------------------------
# Birddog Runtime

class Runtime:
    _LRU_SIZE                = 500

    def __init__(self):
        self._page_lru = PageLRU(maxsize=Runtime._LRU_SIZE)
        self._update_manager = PageUpdateManager(page_lru=self._page_lru)

    @property
    def page_lru(self):
        return self._page_lru

    @property
    def update_manager(self):
        return self._update_manager

    def start(self):
        self._update_manager.start()

    def lookup_address(self, title):
        return page_address(title)

    def lookup_by_address(self, archive, subarchive, fond=None, opus=None, case=None):
        return self._page_lru.lookup_by_address(archive, subarchive, fond, opus, case, runtime=self)

    def lookup_by_title(self, title):
        return self._page_lru.lookup_by_title(title, runtime=self)


