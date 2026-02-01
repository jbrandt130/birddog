import os
import random
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

    def complete_task(self, task_desc, subtasks):
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

if __name__ == "__main__":
    unittest.main()
