import logging
logger = logging.getLogger(__name__)
# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""Translation support from Ukrainian to English with async queue"""

from time import sleep
import requests
from deep_translator import GoogleTranslator
from httpcore._exceptions import ReadTimeout, ConnectTimeout
import threading
import uuid
import queue

# --- Basic Translation Logic ---

def is_english(text):
    """True if argument is decodable to ascii (misnomer?)"""
    try:
        text.encode(encoding='utf-8').decode('ascii')
    except UnicodeDecodeError:
        return False
    return True

_translator = GoogleTranslator(source='uk', target='en')

def translation(text):
    """ Translate single text string or list/tuple of strings. Returns None on failure. """
    result = None
    wait_time = 1.
    for _ in range(5):
        try:
            if isinstance(text, (list, tuple)):
                result = _translator.translate_batch(text)
            else:
                result = _translator.translate(text)
            break
        except (requests.Timeout, ReadTimeout, ConnectTimeout):
            logger.info(f"translation timeout. retrying...")
        sleep(wait_time)
        wait_time *= 2
    return result

# --- Async Queue Infrastructure ---

_NUM_WORKERS = 8
_BATCH_SIZE = 5

_task_queue = queue.Queue()
_task_registry = {}
_task_lock = threading.Lock()

class TranslationTask:
    def __init__(self, task_id, batch, progress_cb, completion_cb):
        self.task_id = task_id
        self.batch = batch
        self.progress_cb = progress_cb
        self.completion_cb = completion_cb
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()

    def run(self):
        results = []
        chunk_size = _BATCH_SIZE
        total = len(self.batch)
        for i in range(0, total, chunk_size):
            if self.cancelled.is_set():
                logger.info(f"Task {self.task_id} cancelled.")
                return
            chunk = self.batch[i:i+chunk_size]
            chunk_result = translation(chunk)
            if chunk_result is None:
                chunk_result = [None] * len(chunk)
            results.extend(chunk_result)
            if self.progress_cb:
                self.progress_cb(self.task_id, min(i + chunk_size, total), total)
        if self.completion_cb:
            self.completion_cb(self.task_id, results)

# Background worker pool

_worker_threads = []

def _worker():
    while True:
        task = _task_queue.get()
        if task is None:
            break  # Sentinel for shutdown
        task.run()
        with _task_lock:
            _task_registry.pop(task.task_id, None)

for _ in range(_NUM_WORKERS):
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    _worker_threads.append(t)

# --- Public Interface ---

def queue_translation(batch, progress_callback=None, completion_callback=None):
    task_id = str(uuid.uuid4())
    task = TranslationTask(task_id, batch, progress_callback, completion_callback)
    with _task_lock:
        _task_registry[task_id] = task
    _task_queue.put(task)
    return task_id

def cancel_translation(task_id):
    with _task_lock:
        task = _task_registry.get(task_id)
        if task:
            task.cancel()
            return True
    return False

def is_translation_running(task_id=None):
    with _task_lock:
        if task_id is not None:
            return task_id in _task_registry
        return bool(_task_registry)