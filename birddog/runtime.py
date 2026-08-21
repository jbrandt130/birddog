# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

import os
import time
import json
from datetime import datetime, timedelta, UTC

from cachetools import LRUCache

from birddog.core import (
    Page,
    )
from birddog.wiki import (
    WIKI_NAMESPACE,
    canonicalize_title,
    literal_parent_title,
    title_in_scope,
    )
from birddog.tracker import (
    PageTracker,
    WikiDocTracker,
    DocumentMap,
    WIKIMEDIA_COMMONS_DOC_TRACKER_SPEC,
    UK_WIKISOURCE_DOC_TRACKER_SPEC,
    )
from birddog.translate import TranslationManager
from birddog.excel import ExportManager
from birddog.store import KeyValueStore
from birddog.env import detect_environment
from birddog.utility import HeartbeatManager
from birddog.fetch import FetchUrlFailError

#_ENABLE_DB_SYNC = os.environ.get("BIRDDOG_ENABLE_DB_SYNC", False)
_ENABLE_DB_SYNC = True
_ENABLE_DB_HOUSEKEEPING = True
_ENABLE_DOC_TRACKER = True
if _ENABLE_DB_SYNC:
    from birddog.database_updater import DatabaseUpdater, DatabaseUpdateManager
    from birddog.database import Database

from birddog.log import get_logger, ServiceLogger, EventLogger
_logger = get_logger()

# ----------------------------------------------------------------------------
# Page LRU memory cache

class PageLRU:
    def __init__(self, maxsize=100, reset_limit=5 * 60):
        self._reset_limit = reset_limit # seconds
        self._timer_start = time.time()
        self._lru = LRUCache(maxsize=maxsize)

    def key(self, title):
        return canonicalize_title(title)

    def _child_key(self, page, child_id):
        return f"{self.key(page.title)}/{child_id}"

    def _get_page(self, title, runtime):
        return Page(title, runtime=runtime)

    def lookup_child(self, page, child_id, runtime=None):
        return self.lookup_by_title(self._child_key(page, child_id), runtime=runtime)

    def lookup_by_title(self, title, runtime=None):
        # periodically flush the lru to ensure the pages don't become stale
        if time.time() - self._timer_start >= self._reset_limit:
            _logger.info("PageLRU: flushing all entries")
            self._lru.clear()
            self._timer_start = time.time()

        title = canonicalize_title(title)
        try:
            page = self._lru[title]
            #_logger.info(f"{f'PageLRU.lookup({title}): hit'}")
            return page
        except KeyError:
            #_logger.info(f"{f'PageLRU.lookup({title}): miss'}")
            page = self._get_page(title, runtime)
            #_logger.info(f"instantiating lru page: {page.title}, {page.exists}")
            self._lru[title] = page

            if not page.exists:
                p_title = literal_parent_title(title)
                if p_title:
                    _logger.info(f"PageLRU: evicting parent {p_title} due to nonexistent child {title}")
                    self.evict(p_title, runtime)

            return page

    def evict(self, title, runtime):
        title = canonicalize_title(title)
        page = self._lru.pop(title, None)
        if not page:
            page = self._get_page(title, runtime)
        if page:
            _logger.info(f"evicting {page.title} from cache")
            page.evict_from_cache()

# ----------------------------------------------------------------------------
# Page Update Manager

class PageUpdateManager(HeartbeatManager):
    _PENDING_TITLE_UPDATES          = "pending_title_updates"
    _HEARTBEAT_INTERVAL             = 60 * 2 # seconds
    _API_DELAY                      = 1 # seconds
    _TITLE_BATCH_SIZE               = max(int(_HEARTBEAT_INTERVAL / _API_DELAY + .5), 1)

    def __init__(self, runtime):
        _logger.info(f"PageUpdateManager.init(): detect_environment=={detect_environment()}")
        self._runtime = runtime
        self._tracker = PageTracker()
        self._kv_store = KeyValueStore()
        super().__init__(interval=PageUpdateManager._HEARTBEAT_INTERVAL)

    def heartbeat(self):
        _logger.info("PageUpdateManager: checking for page updates...")
        try:
            newer_updates = self._tracker.refresh()
        except FetchUrlFailError as err:
            _logger.error(f'PageUpdateManager: unable to refresh page tracker (skipping for now): {err}')
            newer_updates = None

        if newer_updates:
            _logger.info(f"PageUpdateManager: found {len(newer_updates)} updates")
            # place titles into KV store so that they can be processed - accounts
            # for a possible exception during the update processing below, which probably means
            # there was a "too many requests" exception. In this case, back off and try
            # again on the next heartbeat
            for title, update in newer_updates.items():
                _logger.info(f"PageUpdateManager: inserting {title}, {update}")
                self._kv_store.insert(self._PENDING_TITLE_UPDATES, title, json.dumps(update))

        # extract pending updates as list of (title, update) pairs from kv store
        pending_updates = self._kv_store.get_all(self._PENDING_TITLE_UPDATES)

        # update database if updater is active
        if self._runtime.database_update_enabled:
            if pending_updates:
                update_titles = [item[0] for item in pending_updates]
                self._runtime.update_to_database(update_titles, deep=False)
            self._runtime.run_database_housekeeping()

        error_count = 0
        for title, update in pending_updates:
            try:
                update = json.loads(update)
                parent_page_title = literal_parent_title(title)

                if update.get("action") == "delete":
                    _logger.info(f"PageUpdateManager: page deleted: {title}")
                    page_lru = self._runtime.page_lru
                    page_lru.evict(title, self._runtime)
                    if parent_page_title:
                        _logger.info(f"PageUpdateManager: evicting parent after deletion: {parent_page_title}")
                        page_lru.evict(parent_page_title, self._runtime)
                else:
                    # This page may have just been created or edited.
                    # If so, the parent's link status to this page needs to be
                    # changed to indicate that the this page now exists.
                    if parent_page_title:
                        parent = self._runtime.lookup_by_title(parent_page_title)
                        if parent and parent.lastmod and parent.lastmod < update["timestamp"]:
                            # child has been updated - update child link status
                            try:
                                parent.set_child_link_status(title, True)
                            except ValueError:
                                _logger.error(f"Unable to update child link status for child {title}. Skipping...")

                # finished with this title
                self._kv_store.remove(self._PENDING_TITLE_UPDATES, title)
            except (ValueError,  FetchUrlFailError) as err:
                _logger.error(f"Exception in page update manager heartbeat: {err}")
                error_count += 1
                if error_count > 10:
                    # give up for now - try next heartbeat
                    break
                # back off - and try the next item
                time.sleep(10)

        self._runtime.trim_logs()
        _logger.info("PageUpdateManager: finished update check...")

    def get_updates(self, include, exclude=None, cutoff_date=None):
        _logger.info(f"PageUpdateManager.get_updates: include={include}, exclude={exclude}, cutoff_date={cutoff_date}")
        result = {}
        for prefix in include:
            prefix_updates = self._tracker.get_updates(prefix, cutoff_date=cutoff_date)
            for title, update in prefix_updates.items():
                if title_in_scope(title, include, exclude):
                    result[title] = update
        _logger.info(f"PageUpdateManager.get_updates: {len(result)} total updates")
        return result

# ----------------------------------------------------------------------------
# Resource usage monitor

_KILL_THRESHOLD_PATH = "resources/kill_thresholds.json"

class KillSwitch(HeartbeatManager):
    _HEARTBEAT_INTERVAL             = 30 # seconds

    def __init__(self, runtime):
        self._runtime = runtime
        with open(_KILL_THRESHOLD_PATH, encoding="utf8") as file:
            _logger.info(f"KillSwitch: loading thresholds from {_KILL_THRESHOLD_PATH}")
            self._kill_thresholds = json.loads(file.read())
        super().__init__(interval=KillSwitch._HEARTBEAT_INTERVAL)

    def _trigger(self, resource, metric, threshold, value):
        _logger.info(f"KillSwitch trigger(resource={resource}, metric={metric}, threshold={threshold}, value={value})")
        self._runtime.pause()

    def heartbeat(self):
        #_logger.info("KillSwitch heartbeat...")
        now = datetime.now(UTC)
        for limit_spec in self._kill_thresholds:
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
    _LOG_CUTOFF_DAYS         = 15 # days

    def __init__(self):
        self._page_lru = PageLRU(maxsize=Runtime._LRU_SIZE)
        self._update_manager = PageUpdateManager(self)
        self._translation_manager = TranslationManager(self)
        self._export_manager = ExportManager(self)
        if _ENABLE_DB_SYNC:
            self._database = Database()
            self._database_updater = DatabaseUpdater(
                self,
                db=self._database)
            self._database_update_manager = DatabaseUpdateManager(
                self, 
                updater=self._database_updater)
            if _ENABLE_DOC_TRACKER:
                # shared by both trackers so the (potentially large) full-table
                # build happens once, not once per wiki site.
                shared_doc_map = DocumentMap(self._database)
                self._commons_doc_tracker = WikiDocTracker(
                    self, spec=WIKIMEDIA_COMMONS_DOC_TRACKER_SPEC, doc_map=shared_doc_map)
                self._wikisource_doc_tracker = WikiDocTracker(
                    self, spec=UK_WIKISOURCE_DOC_TRACKER_SPEC, doc_map=shared_doc_map)
            else:
                self._commons_doc_tracker = None
                self._wikisource_doc_tracker = None

        else:
            self._database = None
            self._database_updater = None
            self._database_update_manager = None
            self._commons_doc_tracker = None
            self._wikisource_doc_tracker = None


        self._killswitch = KillSwitch(self)
        self.trim_logs()
        self._state = "ready"

    def trim_logs(self):
        cutoff = datetime.now(UTC) - timedelta(days=Runtime._LOG_CUTOFF_DAYS)
        _logger.info(f"Runtime: truncating log history before {cutoff}")
        ServiceLogger.get_logger().truncate(cutoff)
        EventLogger.get_logger().truncate(cutoff)

    @property
    def page_lru(self):
        return self._page_lru

    @property
    def update_manager(self):
        return self._update_manager

    @property
    def export_manager(self):
        return self._export_manager

    @property
    def state(self):
        return self._state

    def start(self):
        if self._state == "ready":
            _logger.info(f"Runtime starting...")
            self._update_manager.start()
            if self._database_update_manager:
                self._database_update_manager.start()
            if self.translation_enabled:
                self._translation_manager.start()
            if self._commons_doc_tracker:
                self._commons_doc_tracker.start()
            if self._wikisource_doc_tracker:
                self._wikisource_doc_tracker.start()
            self._killswitch.start()
            self._state = "running"

    def pause(self):
        if self._state == "running":
            # pause all threads
            _logger.info("Runtime pausing")
            self._update_manager.hold()
            if self._database_update_manager:
                self._database_update_manager.hold()
            self._translation_manager.hold()
            if self._commons_doc_tracker:
                self._commons_doc_tracker.hold()
            if self._wikisource_doc_tracker:
                self._wikisource_doc_tracker.hold()
            self._state = "paused"

    def unpause(self):
        if self._state == "paused":
            # unpause all threads
            _logger.info("Runtime unpausing")
            self._update_manager.release()
            if self._database_update_manager:
                self._database_update_manager.release()
            self._translation_manager.release()
            if self._commons_doc_tracker:
                self._commons_doc_tracker.release()
            if self._wikisource_doc_tracker:
                self._wikisource_doc_tracker.release()
            self._state = "running"

    def lookup_by_title(self, title):
        return self._page_lru.lookup_by_title(title, runtime=self)

    def start_translation(self, page=None, task_name=None, items=None):
        if self.translation_enabled:
            if page:
                # find and translate everything on the given page (page data is updated on completion)
                if task_name or items:
                    raise ValueError("Runtime.start_translation: invalid arguments: task_name, items")
                if not isinstance(page, Page):
                    raise TypeError("Runtime.start_translation: page must be instance of Page")
                self._translation_manager.translate(page)
            else:
                # translation all items in given list. on completion call back based on task name
                if not task_name or not items:
                    raise ValueError("Runtime.start_translation: one of page, or both of (task_name, items) required")
                if not isinstance(task_name, str):
                    raise TypeError("Runtime.start_translation: task_name must be str")
                if not isinstance(items, (list, tuple)) or not all([isinstance(item, str) for item in items]):
                    raise TypeError("Runtime.start_translation: items must be sequence of str")
                self._translation_manager.start_translate_task(task_name, items)

    def complete_translation(self, task_name, translation_map):
        # DO NOT CALL DIRECTLY: called by translation manager
        if task_name.startswith(WIKI_NAMESPACE):
            # page translation initiated through start_translation()
            page = self.lookup_by_title(task_name)
            page.apply_translation(translation_map)
        elif task_name.startswith("DBT_"):
            if not self.database_update_enabled:
                _logger.error("Runtime: unable to complete translation task: database unavailable")
            else:
                self._database_update_manager.complete_translation(task_name, translation_map)
        else:
            raise ValueError(f"Unrecognized translation task completion: {task_name}")

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
    def database_update_enabled(self):
        return _ENABLE_DB_SYNC

    def update_to_database(self, titles, deep=False):
        if self.database_update_enabled:
            self._database_update_manager.start_update(titles, deep)

    def update_documents_to_database(self, doc_urls):
        if self.database_update_enabled:
            self._database_update_manager.start_document_update(doc_urls)

    @property
    def active_database_updates(self):
        if self.database_update_enabled:
            return self._database_update_manager.status()
        return None

    def cancel_update(self, task_name):
        if self.database_update_enabled:
            self._database_update_manager.cancel(task_name)

    def run_database_housekeeping(self):
        if self.database_update_enabled and _ENABLE_DB_HOUSEKEEPING:
            self._database_updater.start_translation()
            self._database_update_manager.update_document_metadata()
            self._database_updater.refresh_doc_lookups()
            self._database_updater.link_hash_duplicates()

    @property
    def database(self):
        return self._database

    @property
    def database_updater(self):
        return self._database_updater
    