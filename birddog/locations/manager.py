# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

import json
import threading
import time

from birddog.database import Database
from birddog.database_updater import _replace_links
from birddog.env import env_table_name
from birddog.fetch import RateLimitedError
from birddog.store import KeyValueStore
from birddog.task import TaskManager
from birddog.utility import new_id

from birddog.log import get_logger
_logger = get_logger()


class LocationUpdateManager(TaskManager):
    """Runs location inference over batches of documents and updates the
    Documents.locations links from the results."""

    _BATCH_SIZE = 20
    _UPDATE_STATE_NS = "loc_update"
    _LOCATION_UPDATE_KIND = "update_locations"
    _LINK_FIELD = "locations"
    # used when the service gives no retry delay
    _DEFAULT_RATE_LIMIT_WAIT_S = 300.0
    _CANCEL_POLL_S = 15.0

    def __init__(self, db=None, finder=None, provider="groq"):
        self._db = db if db else Database()
        # FileLocationFinder loads the Locations table and the location
        # resources on construction, so build it on first use
        self._finder = finder
        self._provider = provider
        self._finder_lock = threading.Lock()
        self._location_record_ids = None
        self._state_store = KeyValueStore(table_name=env_table_name("bd_loc_update"))
        self._state_lock = threading.RLock()
        super().__init__("LocationUpdateManager")

    # ------------------------------------------------------------------
    # state helpers

    def _get_state(self, task_name):
        return json.loads(self._state_store.get(self._UPDATE_STATE_NS, task_name))

    def _put_state(self, task_name, value):
        with self._state_lock:
            self._state_store.insert(self._UPDATE_STATE_NS, task_name, json.dumps(value))

    def _remove_state(self, task_name):
        with self._state_lock:
            try:
                self._state_store.remove(self._UPDATE_STATE_NS, task_name)
            except KeyError:
                pass

    def _update_progress(self, task_id, increment):
        _logger.info(f"update progress: {task_id}: {increment}")
        with self._state_lock:
            try:
                task = self.lookup_task(task_id)
                task_name = task["name"]
                state = self._get_state(task_name)
                state["completed"] += increment
                self._put_state(task_name, state)
            except KeyError:
                # task is already completed - ignore
                pass

    def _create_task(self, task_name, description, total, batches):
        self._put_state(task_name, {
            "kind": self._LOCATION_UPDATE_KIND,
            "description": description,
            "total": total,
            "completed": 0,
        })
        self.create(task_name, batches)

    # ------------------------------------------------------------------
    # lazy resources

    def _get_finder(self):
        with self._finder_lock:
            if self._finder is None:
                from birddog.locations.finder import FileLocationFinder
                self._finder = FileLocationFinder(self._db, self._provider)
            return self._finder

    def _lookup_location_record_ids(self, location_ids):
        """Maps location_id strings (as returned by the finder) to Locations record Ids."""
        wanted = {str(loc_id) for loc_id in location_ids}
        with self._finder_lock:
            if self._location_record_ids is None or not wanted.issubset(self._location_record_ids):
                # first use, or the Locations table gained rows since the last scan
                records = self._db.scan_all("Locations", fields=["location_id"])
                self._location_record_ids = {
                    str(r["location_id"]): r["Id"]
                    for r in records if r.get("location_id") is not None
                }
            return {loc_id: self._location_record_ids.get(loc_id) for loc_id in wanted}

    def _lookup_doc_ids(self, urls):
        """Maps document urls to Documents record Ids; urls with no record are omitted."""
        # lookup() rather than a scan with an "in" clause: NocoDB's "in" takes a
        # comma-separated list, so a url containing a comma never matches there
        return self._db.lookup("Documents", set(urls))

    # ------------------------------------------------------------------
    # subtask execution

    def execute_subtask(self, subtask):
        payload = subtask["payload"]
        kind = payload.get("kind")
        if kind == self._LOCATION_UPDATE_KIND:
            self._execute_location_update_subtask(subtask)
        else:
            raise ValueError(f"LocationUpdateManager.execute_subtask: unknown kind: {kind}")

    def complete_task(self, task_desc, subtasks, is_cancelled=False):
        _logger.info(f"LocationUpdateManager: complete task {task_desc['task_id']}")
        self._remove_state(task_desc["name"])

    def _execute_location_update_subtask(self, subtask):
        _logger.info(f"LocationUpdateManager: location update subtask {subtask['task_id']}.{subtask['index']}")
        batch = subtask["payload"]
        urls = batch.get("urls", [])

        while True:
            # deliberately not caught here (except rate limits): let TaskManager's
            # bounded retry handle a transient failure instead of marking a
            # failed batch "completed"
            try:
                summary = self.update_locations(urls)
                break
            except RateLimitedError as err:
                # an exhausted quota is not a failure of this batch, so wait it
                # out here rather than spending TaskManager's retry attempts
                # (which would be used up in seconds while the service is blocked)
                wait_s = err.retry_after or self._DEFAULT_RATE_LIMIT_WAIT_S
                _logger.warning(f"LocationUpdateManager: rate limited, waiting {wait_s:.0f}s: {err}")
                if not self._wait_unless_cancelled(subtask["task_id"], wait_s):
                    _logger.info(f"LocationUpdateManager: task {subtask['task_id']} cancelled while rate limited")
                    subtask["payload"] = {"kind": batch.get("kind"), "urls": urls, "cancelled": True}
                    return
            except Exception as err:
                _logger.error(f"LocationUpdateManager: exception during location update: {err}, {batch}")
                raise

        subtask["payload"] = {
            "kind": batch.get("kind"),
            "urls": urls,
            **summary,
        }
        self._update_progress(subtask["task_id"], len(urls))

    def _wait_unless_cancelled(self, task_id, wait_s):
        """Sleeps for wait_s, returning False early if the task is cancelled.
        The subtask's lease toucher keeps running meanwhile, so a long wait
        doesn't let the subtask go stale and be re-claimed."""
        deadline = time.monotonic() + wait_s
        while (remaining := deadline - time.monotonic()) > 0:
            if self._is_task_cancelled(task_id):
                return False
            time.sleep(min(remaining, self._CANCEL_POLL_S))
        return not self._is_task_cancelled(task_id)

    def update_locations(self, urls):
        """Infers locations for the documents at the given urls and replaces
        their location links. Returns a summary of what happened.

        A document is left untouched when inference reports an error for it or
        finds no locations, so a failed or empty extraction never erases
        existing links.

        Raises birddog.fetch.RateLimitedError if the location service is rate
        limited for longer than the extractor will wait; no links are changed."""
        summary = {"updated": 0, "unchanged": 0, "not_found": 0, "errors": 0, "empty": 0}

        doc_ids = self._lookup_doc_ids(urls)
        not_found = [url for url in urls if url not in doc_ids]
        summary["not_found"] = len(not_found)
        if not_found:
            _logger.warning(f"LocationUpdateManager: no Documents record for {not_found}")
        if not doc_ids:
            _logger.info(f"LocationUpdateManager: update_locations: {summary}")
            return summary

        ids = list(doc_ids.values())
        results = self._get_finder().get_doc_locations_batched(
            ids, batch_size=self._BATCH_SIZE, only_smallest_locations=True
        )

        for url, doc_id in doc_ids.items():
            error, location_ids = results[doc_id][0], results[doc_id][1]
            if error:
                _logger.error(f"LocationUpdateManager: location inference failed for {url}")
                summary["errors"] += 1
                continue
            if not location_ids:
                summary["empty"] += 1
                continue

            record_ids = self._lookup_location_record_ids(location_ids)
            targets = []
            for loc_id, record_id in record_ids.items():
                if record_id is None:
                    _logger.warning(f"LocationUpdateManager: unknown location_id {loc_id} for {url}")
                else:
                    targets.append(record_id)
            if not targets:
                summary["errors"] += 1
                continue

            added, removed = _replace_links(self._db, "Documents", self._LINK_FIELD, doc_id, targets)
            summary["updated" if added or removed else "unchanged"] += 1

        _logger.info(f"LocationUpdateManager: update_locations: {summary}")
        return summary

    # ------------------------------------------------------------------
    # public API

    def start_location_update(self, doc_urls):
        if isinstance(doc_urls, str):
            doc_urls = [doc_urls]
        if not isinstance(doc_urls, (list, tuple)) or not all(
            [isinstance(url, str) for url in doc_urls]
        ):
            raise ValueError(
                "LocationUpdateManager.start_location_update: doc_urls must be str or sequence of str"
            )

        total = len(doc_urls)
        if total <= 0:
            return None

        task_name = f"LOC_{new_id()}"
        batches = [
            {"kind": self._LOCATION_UPDATE_KIND, "urls": doc_urls[i:i + self._BATCH_SIZE]}
            for i in range(0, total, self._BATCH_SIZE)
        ]
        urls_str = ", ".join(doc_urls[:50]) + ("..." if total > 50 else "")
        self._create_task(task_name, urls_str, total, batches)
        return task_name

    def status(self):
        result = {}
        for item in self._state_store.get_all(self._UPDATE_STATE_NS):
            result[item[0]] = json.loads(item[1])
        return result

    def cancel(self, task_name):
        _logger.info(f"LocationUpdateManager: cancel task {task_name}")
        for task in self.active_tasks():
            if task.get("name") == task_name:
                super().cancel(task.get("task_id"))
                break
        self._remove_state(task_name)
