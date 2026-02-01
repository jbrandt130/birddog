# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from datetime import datetime, UTC
import json
import threading
from threading import Thread
from typing import Optional

from birddog.utility import HeartbeatManager, new_id

from birddog.log import get_logger
_logger = get_logger()


from birddog.store import StringQueue, KeyValueStore

_DDB_TASK_MGR_TEST=0
if _DDB_TASK_MGR_TEST:
    from birddog.store import DynamoDBKeyValueStore, DynamoDBStringQueue
    _logger.info("Using DDB task manager store")

    StringQueue = DynamoDBStringQueue
    KeyValueStore = DynamoDBKeyValueStore

_QUEUE_TABLE_NAME       = "bd_task_queue"
_KV_STORE_TABLE_NAME    = "bd_task_kv_store"

_TASK_MANAGER_HEARTBEAT_INTERVAL    = 15.0 # seconds
_DEFAULT_STALE_SUBTASK_THRESHOLD_MS = 10000 # msec
_IN_PROCESS_SUBTASK_LIMIT           = 5

# Queue-lease defaults (tune as desired)
_DEFAULT_QUEUE_LEASE_MS = 120 * 1000           # 2 minutes
_DEFAULT_QUEUE_TOUCH_MS = 30 * 1000           # 30 seconds

# Retry defaults
_DEFAULT_MAX_ATTEMPTS = 5

# helper for queue diagnostics

_DIAGNOSE_TASK_QUEUE=0
if _DIAGNOSE_TASK_QUEUE:
    class QueueGuard:
        """
        Context manager to check subtask queue integrity on entry and exit.

        If task_mgr._check_subtask_queue(id_string) returns True,
        a warning is logged indicating possible corruption.
        """

        def __init__(self, task_mgr, id_string: str):
            self._task_mgr = task_mgr
            self._id = id_string

        def __enter__(self):
            try:
                if self._task_mgr._check_subtask_queue(self._id):
                    _logger.warning(
                        "Task queue '%s' appears corrupt on entry (task_mgr=%s)",
                        self._id,
                        type(self._task_mgr).__name__,
                    )
            except Exception as e:
                _logger.warning(
                    "Failed to check task queue '%s' on entry: %r",
                    self._id,
                    e,
                )
            return self

        def __exit__(self, exc_type, exc, tb):
            try:
                if self._task_mgr._check_subtask_queue(self._id):
                    _logger.warning(
                        "Task queue '%s' appears corrupt on exit (task_mgr=%s)",
                        self._id,
                        type(self._task_mgr).__name__,
                    )
            except Exception as e:
                _logger.warning(
                    "Failed to check task queue '%s' on exit: %r",
                    self._id,
                    e,
                )
            # Never suppress exceptions
            return False
else:
    class QueueGuard:
        def __init__(self, task_mgr, id_string: str):
            pass
        def __enter__(self): 
            return self
        def __exit__(self, exc_type, exc, tb): 
            return False

def _now_ms():
    return int(datetime.now(UTC).timestamp() * 1000)

class TaskManager(HeartbeatManager):
    """
    TaskManager backed by:
      - StringQueue (pending queue) with claim/ack/extend lease semantics
      - KeyValueStore for:
          * active tasks
          * in-process bookkeeping (for worker capacity + observability)
          * completed subtasks (by index)
          * failed subtasks (by index) [new]

    Key properties:
      - No destructive pop: items are only deleted from the queue on ack().
      - Long-running healthy subtasks: queue lease is renewed while executing.
      - Worker death: lease expires, item becomes visible again and is retried.
      - Retry cap: after max attempts, subtask is marked failed and acked so the task can complete.
    """

    def __init__(self, manager_name, auto_start=False):
        self._queue_store = StringQueue(table_name=_QUEUE_TABLE_NAME)
        self._key_value_store = KeyValueStore(table_name=_KV_STORE_TABLE_NAME)
        self._name = manager_name

        # ---- KV Store namespaces
        
        # active tasks keyed on task id
        self._active_id = f"{self._name}:active"

        # in process subtasks keyed on task_id.subtask_index
        self._in_process_id = f"{self._name}:in_process"

        # completed subtasks (namespace = manager_name:completed.task_id, key = subtask_index)
        self._completed_prefix = f"{self._name}:completed"

        # failed subtasks (namespace = manager_name:completed.task_id, key = subtask_index)
        self._failed_prefix = f"{self._name}:failed"

        # attempt count for subtasks
        self._attempts_prefix = f"{self._name}:attempts"

        # ---- String queue identifiers

        self._pending_id = f"{self._name}:pending"

        # Identify this manager instance for queue lease ownership
        self._consumer_id = f"{self._name}:{new_id()}"

        # ---- Tunable timeouts

        # Used ONLY to prune dead in_process KV entries (no requeue storm behavior).
        self._stale_subtask_threshold_ms = _DEFAULT_STALE_SUBTASK_THRESHOLD_MS

        # Queue lease settings
        self._queue_lease_ms = _DEFAULT_QUEUE_LEASE_MS
        self._queue_touch_interval_ms = _DEFAULT_QUEUE_TOUCH_MS

        # Retry cap
        self._max_attempts = _DEFAULT_MAX_ATTEMPTS

        # worker throttling
        self._worker_sem = threading.BoundedSemaphore(_IN_PROCESS_SUBTASK_LIMIT)
        self._active_workers = 0
        self._active_workers_lock = threading.Lock()

        super().__init__(interval=_TASK_MANAGER_HEARTBEAT_INTERVAL)
        if auto_start:
            self.start()

    # diagnostic queue check
    def _check_subtask_queue(self, message):
        contents = self._queue_store.dump(self._pending_id)
        flagged = False
        for item in contents:
            if not item.get("value"):
                flagged = True
                _logger.warning(f"[{message}] empty pending item: {item}")
        return flagged

    # -------------------------
    # Helpers
    # -------------------------

    @staticmethod
    def _subtask_id(subtask: dict) -> str:
        return f"{subtask['task_id']}.{subtask['index']}"

    def _completed_id(self, task_id: str) -> str:
        return f"{self._completed_prefix}.{task_id}"

    def _failed_id(self, task_id: str) -> str:
        return f"{self._failed_prefix}.{task_id}"

    def _attempts_id(self, task_id: str) -> str:
        return f"{self._attempts_prefix}.{task_id}"

    @staticmethod
    def _form_subtask(payload, index: int, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "index": index,
            "payload": payload,
        }

    def _queue_subtasks(self, batch: list[dict]):
        with QueueGuard(self, "_queue_subtasks"):
            self._queue_store.append(self._pending_id, [json.dumps(item) for item in batch])

    # -------------------------
    # Queue operations (claim/ack/extend)
    # -------------------------

    def _get_attempts(self, subtask):
        try:
            return int(self._key_value_store.get(
                self._attempts_id(subtask["task_id"]), 
                str(subtask["index"])))
        except KeyError:
            return 0

    def _set_attempts(self, subtask, attempts):
        self._key_value_store.insert(
            self._attempts_id(subtask["task_id"]), 
            str(subtask["index"]), str(attempts))

    def _clear_attempts(self, subtask):
        try:
            self._key_value_store.remove(
                self._attempts_id(subtask["task_id"]), 
                str(subtask["index"]))
        except KeyError:
            pass

    def _any_pending(self) -> bool:
        # Updated queue.peek() returns visible (unleased/expired) items.
        try:
            return bool(self._queue_store.peek(self._pending_id, 1))
        except Exception:
            # Be conservative: if peek fails transiently, don't spin.
            return False

    def _claim_next(self) -> Optional[dict]:
        """
        Claim one visible queue item. Returns a decoded subtask dict, augmented with:
          - _receipt: opaque handle used for ack/extend
        """
        with QueueGuard(self, "_claim_next"):
            claimed = self._queue_store.claim(
                self._pending_id,
                n=1,
                lease_ms=self._queue_lease_ms,
                consumer_id=self._consumer_id,
            )

        if not claimed:
            return None

        item = claimed[0]
        try:
            subtask = json.loads(item.value)
        except Exception as e:
            _logger.error(f"TaskManager: corrupt queue payload, acking. err={e}")
            try:
                with QueueGuard(self, "_claim_next.ack"):
                    self._queue_store.ack(self._pending_id, [item.receipt], consumer_id=self._consumer_id)
            except Exception as e2:
                _logger.error(f"TaskManager: failed to ack corrupt payload: {e2}")
            return None

        subtask["_receipt"] = item.receipt
        #_logger.info(f"_claim_next: {self._subtask_id(subtask)} (receipt={subtask['_receipt']})")

        return subtask

    def _ack_subtask(self, subtask: dict):
        receipt = subtask.get("_receipt")
        if receipt:
            #_logger.info(f"_ack_subtask: {self._subtask_id(subtask)}")
            with QueueGuard(self, "_ack_subtask"):
                self._queue_store.ack(self._pending_id, [receipt], consumer_id=self._consumer_id)

    def _release_subtask(self, subtask: dict):
        """
        Best-effort: make the claimed item visible ASAP by setting lease to 0.
        If extend fails, it'll become visible when lease expires.
        """
        receipt = subtask.get("_receipt")
        if receipt:
            try:
                #_logger.info(f"_release_subtask: {self._subtask_id(subtask)}")
                with QueueGuard(self, "_release_subtask"):
                    self._queue_store.extend(self._pending_id, [receipt], lease_ms=0, consumer_id=self._consumer_id)
            except Exception:
                pass

    # -------------------------
    # Subtask bookkeeping (in_process)
    # -------------------------

    def _mark_subtask_in_process(self, subtask: dict):
        now = _now_ms()
        subtask["start_ms"] = now
        subtask["last_touch_ms"] = now
        #_logger.info(f"_mark_subtask_in_process: {self._subtask_id(subtask)}")

        self._key_value_store.insert(
            self._in_process_id,
            self._subtask_id(subtask),
            json.dumps(subtask)
        )

    def _touch_subtask_in_process(self, subtask: dict):
        subtask["last_touch_ms"] = _now_ms()
        #_logger.info(f"_touch_subtask_in_process: {self._subtask_id(subtask)}")
        self._key_value_store.insert(
            self._in_process_id,
            self._subtask_id(subtask),
            json.dumps(subtask)
        )

    def _unmark_subtask_in_process(self, subtask: dict):
        #_logger.info(f"_unmark_subtask_in_process: {self._subtask_id(subtask)}")
        self._key_value_store.remove(self._in_process_id, self._subtask_id(subtask))

    # -------------------------
    # Completion / failure bookkeeping
    # -------------------------

    def _insert_task(self, task_desc: dict):
        self._key_value_store.insert(self._active_id, task_desc["task_id"], json.dumps(task_desc))

    def _remove_task(self, task_id):
        self._key_value_store.remove(self._active_id, task_id)

    def _task_progress_counts(self, task_id: str) -> tuple[int, int]:
        """
        Returns (completed_count, failed_count).
        """
        completed = self._key_value_store.count(self._completed_id(task_id))
        failed = self._key_value_store.count(self._failed_id(task_id))
        return completed, failed

    def _maybe_finalize_task(self, task_id: str):
        """
        Finalize when completed + failed == length.
        Passes a single list of subtask dicts to complete_task(), including failures.
        Each failed subtask will include: {"status": "failed", "error": "..."}.
        """
        try:
            task = self.lookup_task(task_id)
        except KeyError:
            return

        completed_count, failed_count = self._task_progress_counts(task_id)
        task["completed"] = completed_count
        task["failed"] = failed_count
        self._insert_task(task)

        if (completed_count + failed_count) != task["length"]:
            return

        completed_items = self._key_value_store.get_all(self._completed_id(task_id))
        failed_items = self._key_value_store.get_all(self._failed_id(task_id))

        if (len(completed_items) + len(failed_items)) != task["length"]:
            # Another manager may be mid-write; try again next time.
            return

        subtasks = [json.loads(v) for (_, v) in completed_items] + [json.loads(v) for (_, v) in failed_items]
        # Optional: stable ordering by index
        #subtasks.sort(key=lambda s: int(s.get("index", 0)))

        # Cleanup subtasks
        self._key_value_store.remove_all(self._completed_id(task_id))
        self._key_value_store.remove_all(self._failed_id(task_id))

        # Finalize the task
        self.complete_task(task, subtasks)
        
        # Cleanup the task
        self._remove_task(task_id)
        _logger.info(f"TaskManager completed task {task_id} (completed={completed_count}, failed={failed_count})")

    def _mark_subtask_completed(self, subtask: dict, failed=False):
        #_logger.info(f"_mark_subtask_completed: {self._subtask_id(subtask)}, receipt={subtask.get('_receipt')}, failed={failed}")

        # remove from in-process
        try:
            self._unmark_subtask_in_process(subtask)
        except Exception:
            pass

        task_id = subtask["task_id"]

        # Mark status
        subtask["status"] = "failed" if failed else "completed"

        # store result for final processing
        namespace_id = self._failed_id(task_id) if failed else self._completed_id(task_id)
        self._key_value_store.insert(
            namespace_id,
            str(subtask["index"]),
            json.dumps(subtask)
        )

        # clear subtask from the queue
        self._ack_subtask(subtask)

        # remove attempts counter, if any
        self._clear_attempts(subtask)

        # check if all subtasks are done and run final processing if so
        self._maybe_finalize_task(task_id)

    def _mark_subtask_failed(self, subtask: dict, error: str):
        subtask["error"] = str(error)
        self._mark_subtask_completed(subtask, failed=True)

    def _check_finished(self, subtask):
        try:
            self._key_value_store.get(self._completed_id(subtask["task_id"]), str(subtask["index"]))
            return True
        except KeyError:
            pass
        try:
            self._key_value_store.get(self._failed_id(subtask["task_id"]), str(subtask["index"]))
            return True
        except KeyError:
            return False

    # -------------------------
    # Worker execution
    # -------------------------

    def _lease_toucher(self, subtask: dict, stop_event: threading.Event):
        """
        Periodically:
          - extend queue lease
          - update in_process KV heartbeat

        If extend fails (lost ownership), we keep trying KV touch but do not crash.
        """
        receipt = subtask.get("_receipt")
        if not receipt:
            return

        interval_ms = max(1, int(self._queue_touch_interval_ms))
        while not stop_event.wait(interval_ms / 1000.0):
            try:
                #_logger.info(f"_lease_toucher: extending lease: {self._subtask_id(subtask)}")
                with QueueGuard(self, "_lease_toucher"):
                    self._queue_store.extend(
                        self._pending_id,
                        [receipt],
                        lease_ms=self._queue_lease_ms,
                        consumer_id=self._consumer_id,
                    )
            except Exception as e:
                _logger.warning(f"TaskManager: lease extend failed for {self._subtask_id(subtask)}: {e}")

            try:
                self._touch_subtask_in_process(subtask)
            except Exception as e:
                _logger.warning(f"TaskManager: in_process touch failed for {self._subtask_id(subtask)}: {e}")

    def _run_worker(self):
        #_logger.info(f"{self._name}: starting new worker")
        while True:
            subtask = self._claim_next()
            if not subtask:
                _logger.info(f"{self._name}: nothing claimed - exiting")
                return

            task_id = subtask["task_id"]
            subtask_key = self._subtask_id(subtask)

            # If task is already gone, ack and skip (prevents zombie work after completion).
            try:
                self.lookup_task(task_id)
            except KeyError:
                _logger.info(f"_run_worker: removing zombie subtask: {subtask_key}")                
                try:
                    self._ack_subtask(subtask)
                except Exception:
                    pass
                continue

            # check if subtask is already completed or failed, skip if so and check for task completion
            if self._check_finished(subtask):
                self._ack_subtask(subtask)
                self._maybe_finalize_task(subtask["task_id"])
                continue

            # If we've now exhausted retries, mark failed + ack
            attempt = self._get_attempts(subtask) + 1
            self._set_attempts(subtask, attempt)
            if attempt > int(self._max_attempts):
                self._mark_subtask_failed(subtask, error=f"retry cap exceeded (attempt={attempt})")
                continue

            # Mark in process and start toucher
            self._mark_subtask_in_process(subtask)
            stop = threading.Event()
            toucher = Thread(
                target=self._lease_toucher,
                args=(subtask, stop),
                name="task-lease-toucher",
                daemon=True
            )
            toucher.start()

            # execute against a receipt-free dict so subclass can't break ack
            work_subtask = {
                "task_id": subtask["task_id"],
                "index": subtask["index"],
                "payload": subtask.get("payload"),
            }

            try:
                #_logger.info(f"running subtask {self._subtask_id(work_subtask)}, receipt={subtask.get('_receipt')}")
                self.execute_subtask(work_subtask)
            except Exception as e:
                _logger.error(f"Exception in worker subtask {subtask_key}: {e}")

                stop.set()
                try:
                    # remove subtask from in_process kv store
                    self._unmark_subtask_in_process(subtask)
                except Exception:
                    pass

                # Do NOT ack on transient failure: let it be retried.
                # But release lease early to speed retry.
                try:
                    self._release_subtask(subtask)
                except Exception:
                    pass
                continue
            finally:
                stop.set()

            # Copy results back onto the original (which still has _receipt)
            subtask["payload"] = work_subtask.get("payload")

            # Success: mark completed and ack
            try:
                self._mark_subtask_completed(subtask)
            except:
                pass

    def _run_worker_wrapper(self):
        try:
            self._run_worker()
        finally:
            # Always release capacity, even if worker crashes.
            with self._active_workers_lock:
                self._active_workers -= 1
                n = self._active_workers
            self._worker_sem.release()
            _logger.info(f"{self._name} worker exit (active_workers={n})")

    def _spawn_worker(self):
        # Non-blocking acquire: if at capacity, do nothing.
        if not self._worker_sem.acquire(blocking=False):
            return

        with self._active_workers_lock:
            self._active_workers += 1
            n = self._active_workers

        _logger.info(f"{self._name} spawning worker (active_workers={n})")

        thread = Thread(target=self._run_worker_wrapper, name=f"{self._name}-worker", daemon=True)
        thread.start()

    # -------------------------
    # Heartbeat processing
    # -------------------------

    def heartbeat(self):
        now = _now_ms()
        #_logger.info(f"{self._name} heartbeat: {now}")

        # 1) Prune stale in_process entries (worker died) so worker_count doesn't get stuck.
        try:
            in_process = [json.loads(item[1]) for item in self._key_value_store.get_all(self._in_process_id)]
        except Exception as e:
            _logger.warning(f"TaskManager heartbeat: failed to read in_process: {e}")
            in_process = []

        for subtask in in_process:
            last = subtask.get("last_touch_ms") or subtask.get("start_ms") or 0
            if last and (now - last) >= self._stale_subtask_threshold_ms:
                try:
                    _logger.info(f"TaskManager pruning stale in_process entry: {self._subtask_id(subtask)}")
                    self._unmark_subtask_in_process(subtask)
                except Exception:
                    pass

        # 2) Spawn workers if there is visible work and we have capacity.
        if self._any_pending():
            self._spawn_worker()

    # ========
    # subclass methods
    def execute_subtask(self, subtask):
        raise NotImplementedError

    def complete_task(self, task_desc, subtasks):
        raise NotImplementedError
    # ========

    # ========
    # PUBLIC METHODS

    def lookup_task(self, task_id):
        # KeyValueStore.get raises KeyError if missing
        item = self._key_value_store.get(self._active_id, task_id)
        return json.loads(item)

    def lookup_by_name(self, task_name):
        for task in self.active_tasks():
            if task['name'] == task_name:
                return task
        raise KeyError("Unknown task name")

    def active_tasks(self):
        items = self._key_value_store.get_all(self._active_id)
        return [json.loads(item[1]) for item in items]

    def is_active(self):
        return any(self._key_value_store.get_all(self._active_id))

    def create(self, task_name, subtasks):
        if not subtasks:
            raise ValueError("Empty task")

        task_id = new_id()
        task_desc = {
            "task_id": task_id,
            "name": task_name,
            "length": len(subtasks),
            "completed": 0,
            "failed": 0,
        }
        self._insert_task(task_desc)

        batch = [self._form_subtask(item, i, task_id) for i, item in enumerate(subtasks)]
        self._queue_subtasks(batch)

        _logger.info(f"TaskManager starting task {task_id}")
        self._spawn_worker()
        return task_id

