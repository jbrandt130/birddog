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
    ARCHIVES,
    canonicalize_title,
    get_all_pages,
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

    def key(self, archive, subarchive, fond=None, opus=None, case=None):
        return (archive or '', subarchive or '', fond or '', opus or '', case or '')

    def _page_key(self, page):
        a, rest = page.name.split('-', 1)
        parts = rest.split('/')
        return (a, *parts)

    def lookup_child(self, page, child_id):
        return self.lookup(*(*self._page_key(page), child_id))

    def lookup(self, archive, subarchive, fond=None, opus=None, case=None):
        # periodically flush the lru to ensure the pages don't become stale
        if time.time() - self._timer_start >= self._reset_limit:
            _logger.info(f"PageLRU: flushing all entries")
            self._lru.clear()
            self._timer_start = time.time()

        key = self.key(archive, subarchive, fond, opus, case)
        try:
            item = self._lru[key]
            #_logger.info(f"{f'PageLRU.lookup({key}): hit'}")
            return item
        except KeyError:
            #_logger.info(f"{f'PageLRU.lookup({key}): miss'}")
            try:
                if not fond:
                    item = Archive(archive, subarchive=subarchive)
                elif not opus:
                    parent = self.lookup(archive, subarchive)
                    item = parent.lookup(fond)
                elif not case:
                    parent = self.lookup(archive, subarchive, fond)
                    item = parent.lookup(opus)
                else:
                    parent = self.lookup(archive, subarchive, fond, opus)
                    item = parent.lookup(case)
                if not item:
                    raise PageLRU.NotFoundError(key)
                self._lru[key] = item
                return item
            except Page.LookupError:
                _logger.error(f'PageLRU: exception during page lookup')
                _logger.info(f'... failed to find child page: parent={parent.name}, key={key}')
                raise PageLRU.NotFoundError(key)

# ----------------------------------------------------------------------------
# Efficient mapping from page title to Birddog address

class TitleIndex:
    _TITLE_INDEX_PATH       = "title_index.json"

    def __init__(self, page_lru=None):
        self._lru = page_lru if page_lru else PageLRU()
        self.load()

    def load(self):
        try:
            data = load_cached_object(TitleIndex._TITLE_INDEX_PATH)
            self._index = data["index"]
            self._archives = data["archives"]
        except CacheMissError:
            self._init_index()
            self.save()
        self._archive_root = { }
        for archive_root, addresses in self._archives.items():
            for address in addresses:
                self._archive_root[tuple(address)] = archive_root

    def save(self):
        save_cached_object({
            "index": self._index,
            "archives": self._archives,
        }, TitleIndex._TITLE_INDEX_PATH)

    def _init_index(self):
        self._index = {}
        self._archives = {}
        for archive_key in ARCHIVES.keys():
            for entry in ARCHIVES[archive_key].values():
                subarchive_key = entry["subarchive"]["en"]
                archive_title = canonicalize_title(entry["title"]["uk"])
                address = self._lru.key(archive_key, subarchive_key)
                archive_root = archive_title.split("/")[0]
                #print(archive_title, archive_root, address)
                self._index[archive_title] = address
                if not archive_root in self._archives:
                    self._archives[archive_root] = [ address ]
                else:
                    self._archives[archive_root].append(address)

    def _select_parent_archive(self, archive_root, fond_id):
        _logger.info(f"_select_parent_archive: {archive_root}, {fond_id}")
        parent_archive = None
        known_child = False
        if not archive_root in self._archives:
            raise ValueError(f"Unknown archive root: {archive_root}")
        for archive_address in self._archives[archive_root]:
            archive = self._lru.lookup(*archive_address)
            if fond_id.upper().startswith(archive.subarchive["uk"]):
                # look for fond prefix matching subarchive id
                parent_archive = archive_address
                known_child = fond_id in archive.child_ids
                break
            elif archive_address[1] == "D" or len(self._archives[archive_root]) == 1:
                # default to singleton archive or "D" archive which doesn't have prefix on fond ids
                parent_archive = archive_address
                known_child = fond_id in archive.child_ids
        return parent_archive, known_child

    def lookup_archive_root(self, archive_key, subarchive_key):
        address = self._lru.key(archive_key, subarchive_key)
        return self._archive_root[address]

    def lookup(self, page_title):
        page_title = canonicalize_title(page_title)
        if page_title in self._index:
            return self._index[page_title]
        _logger.info(f"TitleIndex.lookup({page_title})")
        page_split = page_title.split("/") + 4 * [None]
        archive_root = page_split[0]
        if not archive_root in self._archives:
            raise ValueError(f"Unrecognized title: {page_title}")

        def _gen_address_for(archive, subarchive, fond, opus, case):
            address = self._lru.key(archive, subarchive, fond, opus, case)
            self._index[page_title] = address
            return address

        for archive_address in self._archives[archive_root]:
            archive = self._lru.lookup(*archive_address)
            if archive:
                if canonicalize_title(archive.title) == page_title:
                    # the page title is the same as the archive title (shouldn't happen, but safe)
                    return _gen_address_for(*archive_address)
                archive_split = archive.title.split("/")
                if len(archive_split) > 1 and archive_split[1] == page_split[1]:
                    # title is of the form archive/subarchive/fond/...
                    return _gen_address_for(*archive_address[:2], *page_split[2:5])
                if page_split[1] in archive.child_ids:
                    # title is of the form archive/fond/...
                    # check for matching fond id
                    return _gen_address_for(*archive_address[:2], *page_split[1:4])

        # failed to find matching fond - test for fond adoption candidate
        if page_split[1]:
            archive_address, known_child = self._select_parent_archive(*page_split[:2])
            if not known_child:
                archive = self._lru.lookup(*archive_address)
                archive.adopt(page_split[1], "/".join(page_split[:2]))
                return _gen_address_for(*archive_address[:2], *page_split[1:4])

        raise ValueError(f"Cannot find page: {page_title}")

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

    def __init__(self, page_lru=None, title_index=None):
        self._lru = page_lru if page_lru else PageLRU()
        self._title_index = title_index if title_index else TitleIndex(page_lru=self._lru)
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
        self._title_index.save() # FIXME: title index should be database

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
                    archive_root = self._title_index.lookup_archive_root(archive, subarchive)
                    # inventory all pages that start with archive root and add latest mod times to tracker
                    titles = get_all_pages(prefix=archive_root)
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
            del self._pending_titles[:PageUpdateManager._TITLE_BATCH_SIZE]
            _logger.info(f"PageUpdateManager: adding batch of {len(batch)} titles to tracker")
            self._tracker.add_titles(batch)
            self.save()
        self._busy = False

    def register_archive(self, archive, subarchive):
        # let the tracker know there's interest in a particular archive
        # insert (archive, subarchive) into request queue
        self._request_queue.put((archive, subarchive))

    def get_updates(self, archive, subarchive, cutoff_date=None):
        prefix = self._title_index.lookup_archive_root(archive, subarchive)
        _logger.info(f"PageUpdateManager.get_updates: prefix={prefix}")
        updates_pending = self.busy
        updates = self._tracker.get_updates(prefix, cutoff_date=cutoff_date)
        _logger.info(f"PageUpdateManager.get_updates: {len(updates)} total updates")
        result = {}
        for title, mod_date in updates.items():
            try:
                address = self._title_index.lookup(title)
                #_logger.info(f"{title}, {mod_date}, {address}")
                if address and address[0] == archive and address[1] == subarchive:
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
        self._title_index = TitleIndex(page_lru=self._page_lru)
        self._update_manager = PageUpdateManager(page_lru=self._page_lru, title_index=self._title_index)

    @property
    def page_lru(self):
        return self._page_lru

    @property
    def update_manager(self):
        return self._update_manager

    @property
    def title_index(self):
        return self._title_index

    def start(self):
        self._update_manager.start()

    def lookup_address(self, title):
        return self._title_index.lookup(title)

    def lookup(self, archive, subarchive, fond=None, opus=None, case=None):
        return self._page_lru.lookup(archive, subarchive, fond, opus, case)

    def lookup_title(self, title):
        address = self.lookup_address(title)
        _logger.info(f"lookup_title: {title} -> {address}")
        return self.lookup(*address)
