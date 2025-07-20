# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

import time
import threading
import queue

from cachetools import LRUCache

from birddog.cache import load_cached_object, save_cached_object, CacheMissError
from birddog.core import (
    Archive,
    Page,
    ArchiveWatcher,
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
