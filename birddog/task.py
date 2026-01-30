# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from datetime import datetime, UTC
import json
import threading
from threading import Thread
from typing import Optional

from birddog.store import get_string_queue_store, get_key_value_store
from birddog.utility import HeartbeatManager, new_id

from birddog.log import get_logger
_logger = get_logger()

_TASK_MANAGER_HEARTBEAT_INTERVAL    = 5.0 # seconds
_DEFAULT_STALE_SUBTASK_THRESHOLD_MS = 10000
_IN_PROCESS_SUBTASK_LIMIT           = 3

# Queue-lease defaults (tune as desired)
_DEFAULT_QUEUE_LEASE_MS = 10 * 60 * 1000      # 10 minutes
_DEFAULT_QUEUE_TOUCH_MS = 20 * 1000           # 20 seconds

# Retry defaults
_DEFAULT_MAX_ATTEMPTS = 5

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
        self._queue_store = get_string_queue_store()
        self._key_value_store = get_key_value_store()

        self._active_id = f"{manager_name}:active"
        self._pending_id = f"{manager_name}:pending"
        self._in_process_id = f"{manager_name}:in_process"

        self._completed_prefix = f"{manager_name}:completed"
        self._failed_prefix = f"{manager_name}:failed"

        # Used ONLY to prune dead in_process KV entries (no requeue storm behavior).
        self._stale_subtask_threshold_ms = _DEFAULT_STALE_SUBTASK_THRESHOLD_MS

        # Queue lease settings
        self._queue_lease_ms = _DEFAULT_QUEUE_LEASE_MS
        self._queue_touch_interval_ms = _DEFAULT_QUEUE_TOUCH_MS

        # Retry cap
        self._max_attempts = _DEFAULT_MAX_ATTEMPTS

        # Identify this manager instance for queue lease ownership
        self._consumer_id = f"{manager_name}:{new_id()}"

        super().__init__(interval=_TASK_MANAGER_HEARTBEAT_INTERVAL)
        if auto_start:
            self.start()

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

    @staticmethod
    def _form_subtask(payload, index: int, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "index": index,
            "payload": payload,
            # attempt is assigned on claim; not stored in the queue payload initially
        }

    def _queue_subtasks(self, batch: list[dict]):
        self._queue_store.append(self._pending_id, [json.dumps(item) for item in batch])

    # -------------------------
    # Queue operations (claim/ack/extend)
    # -------------------------

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
          - attempt: attempt counter for retry cap
        """
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
                self._queue_store.ack(self._pending_id, [item.receipt], consumer_id=self._consumer_id)
            except Exception as e2:
                _logger.error(f"TaskManager: failed to ack corrupt payload: {e2}")
            return None

        subtask["_receipt"] = item.receipt

        # Maintain attempt count in the queue payload itself so it persists across leases/retries.
        # (We will write it back into the queue item by extending lease and updating KV only,
        # but because the queue item itself is immutable in our design, we persist attempt in KV.)
        # Simpler: store attempt in KV in_process and in completed/failed records.
        subtask["attempt"] = int(subtask.get("attempt", 0)) + 1

        return subtask

    def _ack_subtask(self, subtask: dict):
        receipt = subtask.get("_receipt")
        if receipt:
            self._queue_store.ack(self._pending_id, [receipt], consumer_id=self._consumer_id)

    def _release_subtask(self, subtask: dict):
        """
        Best-effort: make the claimed item visible ASAP by setting lease to 0.
        If extend fails, it'll become visible when lease expires.
        """
        receipt = subtask.get("_receipt")
        if receipt:
            try:
                self._queue_store.extend(self._pending_id, [receipt], lease_ms=0, consumer_id=self._consumer_id)
            except Exception:
                pass

    # -------------------------
    # KV bookkeeping (in_process)
    # -------------------------

    def _mark_subtask_in_process(self, subtask: dict):
        now = _now_ms()
        subtask["start_ms"] = now
        subtask["last_touch_ms"] = now
        self._key_value_store.insert(
            self._in_process_id,
            self._subtask_id(subtask),
            json.dumps(subtask)
        )

    def _touch_subtask_in_process(self, subtask: dict):
        subtask["last_touch_ms"] = _now_ms()
        self._key_value_store.insert(
            self._in_process_id,
            self._subtask_id(subtask),
            json.dumps(subtask)
        )

    def _unmark_subtask_in_process(self, subtask: dict):
        self._key_value_store.remove(self._in_process_id, self._subtask_id(subtask))

    # -------------------------
    # Completion / failure bookkeeping
    # -------------------------

    def _store_task(self, task_desc: dict):
        self._key_value_store.insert(self._active_id, task_desc["task_id"], json.dumps(task_desc))

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
        self._store_task(task)

        if (completed_count + failed_count) != task["length"]:
            return

        completed_items = self._key_value_store.get_all(self._completed_id(task_id))
        failed_items = self._key_value_store.get_all(self._failed_id(task_id))

        if (len(completed_items) + len(failed_items)) != task["length"]:
            # Another manager may be mid-write; try again next time.
            return

        subtasks = [json.loads(v) for (_, v) in completed_items] + [json.loads(v) for (_, v) in failed_items]
        # Optional: stable ordering by index
        subtasks.sort(key=lambda s: int(s.get("index", 0)))

        # Cleanup + callback
        self._key_value_store.remove_all(self._completed_id(task_id))
        self._key_value_store.remove_all(self._failed_id(task_id))
        self.complete_task(task, subtasks)
        self._key_value_store.remove(self._active_id, task_id)
        _logger.info(f"TaskManager completed task {task_id} (completed={completed_count}, failed={failed_count})")

    def _mark_subtask_completed(self, subtask: dict):
        # remove from in-process
        try:
            self._unmark_subtask_in_process(subtask)
        except Exception:
            pass

        task_id = subtask["task_id"]

        # Avoid leaking internal receipt into results; harmless if you keep it, but usually noisy.
        subtask.pop("_receipt", None)

        # Mark status
        subtask["status"] = "completed"

        self._key_value_store.insert(
            self._completed_id(task_id),
            str(subtask["index"]),
            json.dumps(subtask)
        )

        self._maybe_finalize_task(task_id)

    def _mark_subtask_failed(self, subtask: dict, error: str):
        # remove from in-process
        try:
            self._unmark_subtask_in_process(subtask)
        except Exception:
            pass

        task_id = subtask["task_id"]

        subtask.pop("_receipt", None)
        subtask["status"] = "failed"
        subtask["error"] = str(error)

        self._key_value_store.insert(
            self._failed_id(task_id),
            str(subtask["index"]),
            json.dumps(subtask)
        )

        self._maybe_finalize_task(task_id)

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
        while True:
            subtask = self._claim_next()
            if not subtask:
                return

            task_id = subtask["task_id"]
            subtask_key = self._subtask_id(subtask)

            # If task is already gone, ack and skip (prevents zombie work after completion).
            try:
                self.lookup_task(task_id)
            except KeyError:
                try:
                    self._ack_subtask(subtask)
                except Exception:
                    pass
                continue

            # Retry cap check BEFORE doing work
            attempt = int(subtask.get("attempt", 1))
            if attempt > int(self._max_attempts):
                _logger.error(f"TaskManager: retry cap exceeded for {subtask_key} (attempt={attempt}), marking failed.")
                try:
                    self._mark_subtask_failed(subtask, error=f"retry cap exceeded (attempt={attempt})")
                finally:
                    # Ack so it doesn't reappear
                    try:
                        self._ack_subtask(subtask)
                    except Exception:
                        pass
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

            try:
                self.execute_subtask(subtask)
            except Exception as e:
                _logger.error(f"Exception in worker subtask {subtask_key} (attempt={attempt}): {e}")

                stop.set()
                try:
                    self._unmark_subtask_in_process(subtask)
                except Exception:
                    pass

                # Do NOT ack on transient failure: let it be retried.
                # But release lease early to speed retry.
                try:
                    self._release_subtask(subtask)
                except Exception:
                    pass

                # If we've now exhausted retries, mark failed + ack
                if attempt >= int(self._max_attempts):
                    try:
                        self._mark_subtask_failed(subtask, error=e)
                    finally:
                        try:
                            self._ack_subtask(subtask)
                        except Exception:
                            pass
                continue
            finally:
                stop.set()

            # Success: mark completed and ack
            try:
                self._mark_subtask_completed(subtask)
            finally:
                try:
                    self._ack_subtask(subtask)
                except Exception as e:
                    # If ack fails (lost ownership), item may reappear; if execute_subtask is non-idempotent,
                    # that can be harmful. Lease renewal reduces this likelihood substantially.
                    _logger.warning(f"TaskManager: ack failed for {subtask_key}: {e}")

    def _spawn_worker(self):
        thread = Thread(target=self._run_worker, name="task-worker", daemon=True)
        thread.start()

    # -------------------------
    # Heartbeat processing
    # -------------------------

    def heartbeat(self):
        now = _now_ms()

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
            try:
                worker_count = self._key_value_store.count(self._in_process_id)
            except Exception:
                worker_count = 0

            if worker_count < _IN_PROCESS_SUBTASK_LIMIT:
                _logger.info(f"TaskManager spawning additional worker (in_process={worker_count})")
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
        self._store_task(task_desc)

        batch = [self._form_subtask(item, i, task_id) for i, item in enumerate(subtasks)]
        self._queue_subtasks(batch)

        _logger.info(f"TaskManager starting task {task_id}")
        self._spawn_worker()
        return task_id

