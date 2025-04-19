# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""Translation support from Ukrainian to English with async queue"""

from time import sleep
import requests
import os
from deep_translator import GoogleTranslator, DeeplTranslator
from httpcore._exceptions import ReadTimeout, ConnectTimeout
import threading
import uuid
import queue


from birddog.logging import get_logger
_logger = get_logger()

# --- Google Cloud service support ---

import os
from google.cloud import translate_v2 as google_translate

# Set the environment variable to your credentials file
#os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "./keys/google-cloud-translate-key.json"

class GoogleCloudTranslator:
    def __init__(self, source="uk", target="en"):
        self._source = source
        self._target = target
        self._client = google_translate.Client()

    def translate(self, text):
        result = self._client.translate(text, source_language=self._source, target_language=self._target)
        if isinstance(text, (list, tuple)):
            return [item['translatedText'] for item in result]
        else:
            return result['translatedText']

    def translate_batch(self, text):
        return self.translate(text)

# --- Configure Translator ---

_translator = None
_DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", None)
_USE_GOOGLE_CLOUD_TRANSLATE = os.getenv("BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE", False) in ("true", "True", "1")

if _USE_GOOGLE_CLOUD_TRANSLATE:
    _logger.info(f'Using Google Cloud translation API (credentials file:{os.getenv("GOOGLE_APPLICATION_CREDENTIALS")})')
    _translator = GoogleCloudTranslator(source="uk", target="en")
elif _DEEPL_API_KEY:
    _logger.info('Using DeepL translation API')
    _translator = DeeplTranslator(api_key=_DEEPL_API_KEY, source="uk", target="en", use_free_api=True)
else:
    _logger.info('Using free Google translation API')
    _translator = GoogleTranslator(source='uk', target='en')

# --- Basic Translation Logic ---

def is_english(text):
    """True if argument is decodable to ascii (misnomer?)"""
    try:
        text.encode(encoding='utf-8').decode('ascii')
    except UnicodeDecodeError:
        return False
    return True

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
            _logger.info(f"translation timeout. retrying...")
        sleep(wait_time)
        wait_time *= 2
    return result

# --- Async Queue Infrastructure ---

_NUM_WORKERS = 8
_BATCH_SIZE = 10 if _USE_GOOGLE_CLOUD_TRANSLATE else 5 # google cloud is faster than the alternatives

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
                _logger.info(f"Task {self.task_id} cancelled.")
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