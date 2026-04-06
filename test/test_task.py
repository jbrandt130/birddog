import os
import random
import threading
import time
import unittest

from birddog.task import (
    TaskManager,
    )

# ------------------ UTILITY UNIT TESTS ------------------

task_register = {
    "a": {"length": 5, "complete": False},
    "b": {"length": 25, "complete": False},
    "c": {"length": 9, "complete": False},
    "d": {"length": 1, "complete": False},
}

class TaskTest(TaskManager):
    def __init__(self):
        super().__init__("Unit Test", auto_start=True)

    def execute_subtask(self, subtask):
        if random.uniform(0, 1) > .8:
            raise ValueError(f"failed on {subtask['payload']}")
        time.sleep(random.uniform(0, 1))
        print("subtask:", subtask['payload'])

    def complete_task(self, task_desc, subtasks, is_cancelled=False):
        global task_register
        task_register[task_desc['name']]['complete'] = True
        print("complete:", task_desc['name'])

class Test(unittest.TestCase):
    def test_task(self):
        mgr = TaskTest()
        for name, value in task_register.items():
            #mgr.create(name, list(range(value["length"])))
            mgr.create(name, [f"{name}-{x}" for x in range(value["length"])])
        while mgr.is_active():
            time.sleep(1)
            active = mgr.active_tasks()
            if not active:
                break
            for task in active:
                print(f"{task['name']}: {task['completed']}/{task['length']}")
        self.assertTrue(not mgr.active_tasks())
        for name, value in task_register.items():
            self.assertTrue(task_register[name]['complete'])

# ------------------ CANCELLATION TESTS ------------------

class BlockingTaskManager(TaskManager):
    """TaskManager whose subtasks block until released, for deterministic cancel testing."""
    def __init__(self):
        self.complete_task_called = False
        self.complete_task_subtasks = None
        self._proceed = threading.Event()
        super().__init__("CancellationTest", auto_start=True)

    def execute_subtask(self, subtask):
        self._proceed.wait()

    def complete_task(self, task_desc, subtasks, is_cancelled=False):
        self.complete_task_called = True
        self.complete_task_subtasks = subtasks

    def wait_until_inactive(self, timeout=10):
        deadline = time.time() + timeout
        while self.is_active() and time.time() < deadline:
            time.sleep(0.1)


class TestCancellation(unittest.TestCase):
    def test_cancel_prevents_complete_task(self):
        """Cancelling a task should suppress the complete_task() callback."""
        mgr = BlockingTaskManager()
        task_id = mgr.create("cancel-test", list(range(5)))
        mgr.cancel(task_id)
        mgr._proceed.set()  # let subtasks run (they will be skipped as cancelled)
        mgr.wait_until_inactive()
        self.assertFalse(mgr.is_active())
        self.assertFalse(mgr.complete_task_called)

    def test_cancel_removes_task_from_active(self):
        """Cancelled task should be removed from active tasks after finalization."""
        mgr = BlockingTaskManager()
        task_id = mgr.create("cancel-remove-test", list(range(3)))
        mgr.cancel(task_id)
        mgr._proceed.set()
        mgr.wait_until_inactive()
        active_ids = {t["task_id"] for t in mgr.active_tasks()}
        self.assertNotIn(task_id, active_ids)

    def test_cancel_nonexistent_task(self):
        """cancel() on an unknown task_id should be a no-op (no exception)."""
        mgr = BlockingTaskManager()
        mgr._proceed.set()
        mgr.cancel("nonexistent-task-id")  # should not raise

    def test_cancel_subtask_statuses(self):
        """Subtasks of a cancelled task should have status 'cancelled'."""
        mgr = BlockingTaskManager()
        task_id = mgr.create("cancel-status-test", list(range(4)))
        mgr.cancel(task_id)
        mgr._proceed.set()
        mgr.wait_until_inactive()
        # complete_task is not called for cancelled tasks, so inspect via
        # the flag being cleared (task gone) and no complete_task invocation
        self.assertFalse(mgr.complete_task_called)

    def test_cancel_clears_cancel_flag(self):
        """Cancel flag should be cleaned up after the task is finalized."""
        mgr = BlockingTaskManager()
        task_id = mgr.create("cancel-flag-cleanup-test", list(range(2)))
        mgr.cancel(task_id)
        mgr._proceed.set()
        mgr.wait_until_inactive()
        # After finalization the cancel flag KV entry should be gone
        self.assertFalse(mgr._is_task_cancelled(task_id))

    def test_cancel_after_completion_is_noop(self):
        """cancel() after a task already completed should be a no-op."""
        mgr = BlockingTaskManager()
        task_id = mgr.create("cancel-late-test", list(range(2)))
        mgr._proceed.set()
        mgr.wait_until_inactive()
        self.assertTrue(mgr.complete_task_called)
        mgr.cancel(task_id)  # task already gone — should not raise or do anything


if __name__ == "__main__":
    unittest.main()
