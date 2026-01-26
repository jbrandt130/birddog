# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

import os
import time
import queue
import json
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
    WIKI_NAMESPACE,
    canonicalize_title,
    archive_root,
    get_all_pages,
    page_address,
    page_title_from_address,
    parent_title,
    )
from birddog.tracker import PageTracker
from birddog.translate import TranslationManager
from birddog.excel import ExportManager
from birddog.store import get_string_queue_store, get_key_value_store
from birddog.env import detect_environment
from birddog.utility import HeartbeatManager, FetchUrlFailError

_ENABLE_DB_SYNC = os.environ.get("BIRDDOG_ENABLE_DB_SYNC", False)
if _ENABLE_DB_SYNC:
    from birddog.database_updater import DatabaseUpdateManager

from birddog.log import get_logger, ServiceLogger, EventLogger
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
            #_logger.info(f"{f'PageLRU.lookup({title}): hit'}")
            return page
        except KeyError:
            #_logger.info(f"{f'PageLRU.lookup({title}): miss'}")
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
        _logger.info(f"ArchiveWatcher init (runtime={runtime})")
        self._runtime = runtime
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
        updates = page_manager.get_updates(self._archive, self._subarchive)
        _logger.info(f"ArchiveWatcher.check: found {len(updates)} updates.")
        if updates:
            new_cutoff = self._last_checked_date
            for title, update in updates.items():
                address = page_address(title)
                assert address[0] == self._archive and address[1] == self._subarchive
                if len(address) >= 6:
                    # FIXME: handle longer addresses correctly
                    _logger.info(f"Watcher.check: ignoring nonconforming address: {address}")
                    continue
                item = ArchiveWatcher.key(*address)
                # Get most recent resolved mod date (if any)
                mod_date = update["timestamp"]
                new_cutoff = max(mod_date, new_cutoff)
                user = update.get("user", "")
                latest_resolved = self._resolved[item][-1]["modified"] if item in self._resolved else self._cutoff_date
                if latest_resolved is None or mod_date > latest_resolved:
                    self._unresolved[item] = {
                        "modified": mod_date,
                        "last_resolved": self._last_resolved_date(item),
                        "title": title,
                        "user": user,
                    }
            #_logger.info(f'ArchiveWatcher.check() unresolved: {json.dumps(self._unresolved, indent=4)}')
            self._last_checked_date = new_cutoff

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
    _PENDING_TITLE_UPDATES          = "pending_title_updates"
    _HEARTBEAT_INTERVAL             = 60 * 5 # seconds
    _API_DELAY                      = 1 # seconds
    _TITLE_BATCH_SIZE               = max(int(_HEARTBEAT_INTERVAL / _API_DELAY + .5), 1)

    def __init__(self, runtime):
        _logger.info(f"PageUpdateManager.init(): detect_environment=={detect_environment()}")
        self._runtime = runtime
        self._tracker = PageTracker()
        self._kv_store = get_key_value_store()
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
        if pending_updates:
            update_titles = [item[0] for item in pending_updates]
            self._runtime.update_to_database(update_titles, deep=False)

        error_count = 0
        for title, update in pending_updates:
            # This page may have just been created. 
            # If so, the parent's link status to this page needs to be
            # changed to indicate that the this page now exists.
            try:
                try:
                    parent_page_title = parent_title(title)
                except ValueError as err:
                    _logger.error(f"PageUpdateManager: unable to find parent of {title}. Skipping.")
                    parent_page_title = None
                if parent_page_title:
                    parent = self._runtime.lookup_by_title(parent_page_title)
                    update = json.loads(update)
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

        _logger.info("PageUpdateManager: finished update check...")

    def get_updates(self, archive, subarchive, cutoff_date=None):
        prefix = archive_root(archive, subarchive)
        _logger.info(f"PageUpdateManager.get_updates: prefix={prefix}, cutoff_date={cutoff_date}")
        updates = self._tracker.get_updates(prefix, cutoff_date=cutoff_date)
        _logger.info(f"PageUpdateManager.get_updates: {len(updates)} total updates")
        result = {}
        for title, update in updates.items():
            try:
                address = page_address(title)
                if address[:2] == (archive, subarchive):
                    result[title] = update
            except ValueError:
                _logger.error(f"PageUpdateManager.get_updates: cannot find title {title}. Skipping...")
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
    _LOG_CUTOFF_DAYS         = 60 # days

    def __init__(self):
        self._page_lru = PageLRU(maxsize=Runtime._LRU_SIZE)
        self._update_manager = PageUpdateManager(self)
        self._translation_manager = TranslationManager(self)
        self._export_manager = ExportManager(self)
        self._database_update_manager = DatabaseUpdateManager(self) if _ENABLE_DB_SYNC else None

        self._killswitch = KillSwitch(self)
        self._trim_logs()
        self._state = "ready"

    def _trim_logs(self):
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
            self._state = "paused"

    def unpause(self):
        if self._state == "paused":
            # unpause all threads
            _logger.info("Runtime unpausing")
            self._update_manager.release()
            if self._database_update_manager:
                self._database_update_manager.release()
            self._translation_manager.release()
            self._state = "running"

    def lookup_address(self, title):
        return page_address(title)

    def lookup_by_address(self, archive, subarchive, fond=None, opus=None, case=None):
        return self._page_lru.lookup_by_address(archive, subarchive, fond, opus, case, runtime=self)

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
        return bool(self._database_update_manager)

    def update_to_database(self, titles, deep=False):
        if self.database_update_enabled:
            self._database_update_manager.start_update(titles, deep)

    @property
    def active_database_updates(self):
        return self._database_update_manager.active_tasks()
