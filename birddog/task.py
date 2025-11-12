# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from datetime import datetime, UTC
import json
from threading import Thread

import ulid

from birddog.store import get_string_queue_store, get_key_value_store
from birddog.utility import HeartbeatManager

from birddog.logging import get_logger
_logger = get_logger()

_TASK_MANAGER_HEARTBEAT_INTERVAL    = 5.0 # seconds
_STALE_SUBTASK_THRESHOLD_MS         = 10000
_IN_PROCESS_SUBTASK_LIMIT           = 3

def _new_id():
    return str(ulid.ulid())

def _now_ms():
    return int(datetime.now(UTC).timestamp() * 1000)

class TaskManager(HeartbeatManager):
    def __init__(self, manager_name, auto_start=False):
        self._queue_store = get_string_queue_store()
        self._key_value_store = get_key_value_store()
        self._active_id = f"{manager_name}:active"
        self._pending_id = f"{manager_name}:pending"
        self._in_process_id = f"{manager_name}:in_process"
        self._completed_prefix = f"{manager_name}:completed"
        super().__init__(interval=_TASK_MANAGER_HEARTBEAT_INTERVAL)
        if auto_start:
            self.start()

    @staticmethod
    def _subtask_id(subtask):
        return f"{subtask['task_id']}.{subtask['index']}"

    def _completed_id(self, task_id):
       return f"{self._completed_prefix}.{task_id}"

    @staticmethod
    def _form_subtask(payload, index, task_id):
        return {
            "task_id": task_id,
            "index": index,
            "payload": payload        
        }

    def _queue_subtask(self, subtask):
        self._queue_store.append(self._pending_id, [json.dumps(subtask)])

    def _queue_subtasks(self, batch):
        batch = [json.dumps(item) for item in batch]
        self._queue_store.append(self._pending_id, batch)

    def _requeue_subtask(self, subtask):
        self._key_value_store.remove(self._in_process_id, self._subtask_id(subtask))
        _logger.info(f"TaskManager requeueing subtask: {self._subtask_id(subtask)}")
        self._queue_subtask(subtask)

    def _next_subtask(self):
        item = self._queue_store.pop(self._pending_id, 1)
        #_logger.info(f"_next_subtask: {item}")
        return json.loads(item[0]) if item else None

    def _any_pending(self):
        return self._queue_store.peek(self._pending_id, 1)

    def _mark_subtask_in_process(self, subtask):
        subtask["start"] = _now_ms()
        self._key_value_store.insert(
            self._in_process_id, 
            self._subtask_id(subtask), 
            json.dumps(subtask))

    def _mark_subtask_completed(self, subtask):
        # move from in process to completed list
        self._key_value_store.remove(self._in_process_id, self._subtask_id(subtask))
        task_id = subtask["task_id"]
        value = json.dumps(subtask)
        _logger.info(f"_mark_subtask_completed: {task_id}: marking subtask complete (len={len(value)})")
        self._key_value_store.insert(
            self._completed_id(task_id), 
            str(subtask["index"]), 
            value)
        _logger.info(f"_mark_subtask_completed: {task_id}: done marking subtask complete")

        # update task progress
        task = self.lookup_task(task_id)
        task["completed"] = self._key_value_store.count(self._completed_id(task_id))
        self._store_task(task)

        if task["completed"] == task["length"]:
            # all done: clean up
            completed_subtasks = self._key_value_store.get_all(self._completed_id(task_id))
            # check one more time in case another manager already cleaned up
            if len(completed_subtasks) == task["length"]:
                completed_subtasks = [json.loads(item[1]) for item in completed_subtasks]
                self._key_value_store.remove_all(self._completed_id(task_id))    
                self.complete_task(task, completed_subtasks)
                self._key_value_store.remove(self._active_id, task_id)
                _logger.info(f"TaskManager completed task {task_id}")


    def _store_task(self, task_desc):
        self._key_value_store.insert(
            self._active_id, 
            task_desc["task_id"], 
            json.dumps(task_desc))

    def _run_worker(self):
        while True:
            subtask = self._next_subtask()
            if subtask:
                self._mark_subtask_in_process(subtask)
                try:
                    self.execute_subtask(subtask)
                except Exception as e:
                    _logger.error(f"Exception in worker subtask: {e}, requeueing")
                    self._requeue_subtask(subtask)
                    continue
                self._mark_subtask_completed(subtask)
            else:
                # no work left, exit
                return

    def _spawn_worker(self):
        thread = Thread(target=self._run_worker, name="task-worker", daemon=True)
        thread.start()

    # heartbeat processing
    def heartbeat(self):
        #_logger.info("TaskManager: heartbeat")
        # 1: check for stale in-process subtasks and requeue them if necessary
        in_process_subtasks = [json.loads(item[1]) for item in self._key_value_store.get_all(self._in_process_id)]
        now = _now_ms()
        stale_subtasks = [item for item in in_process_subtasks 
                          if now - item["start"] >= _STALE_SUBTASK_THRESHOLD_MS]
        for subtask in stale_subtasks:
            self._requeue_subtask(subtask)

        # 2: spawn worker if in_process subtask count is less than threshold and there are pending subtasks
        if self._any_pending() and self._key_value_store.count(self._in_process_id) < _IN_PROCESS_SUBTASK_LIMIT:
            _logger.info(f"TaskManager spawning additional worker")
            self._spawn_worker()

    # ========
    # subclass methods
    def execute_subtask(self, subtask):
        # override
        raise NotImplementedError

    def complete_task(self, task_desc, subtasks):
        # override
        raise NotImplementedError
    # ========

    # ========
    # PUBLIC METHODS

    def lookup_task(self, task_id):
        item = self._key_value_store.get(self._active_id, task_id)
        if item:
            return json.loads(item)
        raise KeyError("Unknown task_id")

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

        task_id = _new_id()
        task_desc = {
            "task_id": task_id,
            "name": task_name,
            "length": len(subtasks),
            "completed": 0
        }
        self._store_task(task_desc)

        batch = [self._form_subtask(item, i, task_id) for i, item in enumerate(subtasks)]
        self._queue_subtasks(batch)
        #for i, item in enumerate(subtasks):
        #    self._queue_subtask(self._form_subtask(item, i, task_id))
        _logger.info(f"TaskManager starting task {task_id}")
        self._spawn_worker()
        return task_id

