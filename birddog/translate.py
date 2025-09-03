# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""Translation support from Ukrainian to English with async Translation manager"""

import os
import time
import random
import time
import threading
from collections import deque
import requests
from requests.exceptions import Timeout, ReadTimeout, ConnectTimeout, ConnectionError as RequestsConnectionError

from deep_translator import GoogleTranslator, DeeplTranslator
from google.cloud import translate_v2 as google_translate
from google.api_core.exceptions import GoogleAPICallError

from birddog.task import TaskManager
from birddog.logging import get_logger
_logger = get_logger()

# --- Google Cloud service support ---

_MAX_TRANSLATE_REQUESTS_PER_HOUR = 10  # example, adjust as needed

class RateLimitExceeded(Exception):
    """Raised when translation quota is exceeded."""
    pass

class ServiceCallError(Exception):
    """Raised when translation quota is exceeded."""
    pass

class GoogleCloudTranslator:
    def __init__(self, source="uk", target="en"):
        self._source = source
        self._target = target
        self._client = google_translate.Client()
        self._lock = threading.Lock()
        self._timestamps = deque()  # store request timestamps

    def _log_bytes(self, text):
        try:
            if isinstance(text, (list, tuple)):
                total_bytes = sum(len(t.encode('utf-8')) for t in text)
            else:
                total_bytes = len(text.encode('utf-8'))
            _logger.info(f"GoogleCloudTranslator: translating {total_bytes} bytes")
        except Exception as e:
            _logger.error(f"Unable to encode byte length for translation result: {text}")
            pass

    def _check_quota(self):
        now = time.time()
        cutoff = now - 3600  # 1 hour ago

        with self._lock:
            # Remove timestamps older than 1 hour
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= _MAX_TRANSLATE_REQUESTS_PER_HOUR:
                raise RateLimitExceeded(
                    f"Translation quota exceeded: "
                    f"{_MAX_TRANSLATE_REQUESTS_PER_HOUR} per hour"
                )

            # Record this request
            self._timestamps.append(now)

    def translate(self, text):
        self._check_quota()
        self._log_bytes(text)
        try:
            result = self._client.translate(
                text, source_language=self._source, target_language=self._target
            )
        except GoogleAPICallError as e:
            raise ServiceCallError()
        if isinstance(text, (list, tuple)):
            return [item['translatedText'] for item in result]
        return result['translatedText']

    def translate_batch(self, text):
        return self.translate(text)

# --- Configure Translator ---

_ENABLE_TRANSLATION = False

_DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", None)
_USE_GOOGLE_CLOUD_TRANSLATE = os.getenv("BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE", None) in ("true", "True", "1")

_translator = None

if _ENABLE_TRANSLATION:
    if _USE_GOOGLE_CLOUD_TRANSLATE:
        #_logger.info(f'Using Google Cloud translation API (credentials file:{os.getenv("GOOGLE_APPLICATION_CREDENTIALS")})')
        _logger.info('Using Google Cloud translation API')
        _translator = GoogleCloudTranslator(source="uk", target="en")
    elif _DEEPL_API_KEY:
        _logger.info('Using DeepL translation API')
        _translator = DeeplTranslator(api_key=_DEEPL_API_KEY, source="uk", target="en", use_free_api=True)
    else:
        _logger.info('Using free Google translation API')
        _translator = GoogleTranslator(source='uk', target='en')

# --- Basic Translation Logic ---

class MaxRetriesExceeded(Exception):
    """Raised when the maximum number of retries for a service call is exceeded."""
    def __init__(self, *, attempts, last_error=None):
        msg = f"Service call failed after {attempts} attempts."
        super().__init__(msg)
        self.attempts = attempts
        self.last_error = last_error

class IncompleteTranslationError(Exception):
    """Raised when the batch translation result length doesn't match the input length."""
    def __init__(self, *, expected, got):
        super().__init__(f"Batch translation length mismatch: expected {expected}, got {got}")
        self.expected = expected
        self.got = got

class TranslationDisabledError(RuntimeError):
    pass

def translation(text, *, retries=5, base_delay=1.0, max_delay=8.0, jitter=0.25):
    """
    Translate a single string or a sequence of strings.

    Raises:
        MaxRetriesExceeded: if all retries fail.
        IncompleteTranslationError: if batch result length != input length.

    Returns:
        str for single input, list[str] for batch input.
    """

    if not _ENABLE_TRANSLATION:
        raise TranslationDisabledError("Translation is disabled in this configuration")

    last_exc = None
    delay = base_delay

    for attempt in range(1, retries + 1):
        try:
            if isinstance(text, (list, tuple)):
                result = _translator.translate_batch(text)
            else:
                result = _translator.translate(text)

            # Validate batch size if applicable
            if isinstance(text, (list, tuple)):
                if not isinstance(result, (list, tuple)) or len(result) != len(text):
                    raise IncompleteTranslationError(expected=len(text), got=len(result) if hasattr(result, "__len__") else "n/a")
            return result

        except (Timeout, ReadTimeout, ConnectTimeout, RequestsConnectionError) as e:
            last_exc = e
            _logger.info("translation timeout/connection issue (attempt %d/%d). retrying...", attempt, retries)
        except requests.HTTPError as e:
            # Optional: decide whether certain 5xx errors are retryable
            last_exc = e
            if 500 <= getattr(e.response, "status_code", 500) < 600:
                _logger.info("server error %s (attempt %d/%d). retrying...", getattr(e.response, "status_code", "?"), attempt, retries)
            else:
                # Non-retryable HTTP error
                raise

        if attempt < retries:
            sleep_for = min(delay, max_delay) + random.uniform(0, jitter)
            time.sleep(sleep_for)
            delay = min(delay * 2, max_delay)

    # All attempts failed
    raise MaxRetriesExceeded(attempts=retries, last_error=last_exc) from last_exc

# --- TRANSLATION SUPPORT ---

def needs_translation(item):
    """True if text item needs to be translated to English"""
    return isinstance(item, dict) and 'uk' in item and 'en' not in item

def _traverse_page(obj, select_fn, action_fn):
    if select_fn(obj):
        action_fn(obj)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _traverse_page(value, select_fn, action_fn)
    elif isinstance(obj, dict):
        for value in obj.values():
            _traverse_page(value, select_fn, action_fn)

def get_translation_items(page):
    batch = []
    _traverse_page(page,
        needs_translation,
        lambda obj: batch.append(obj['uk']))
    return batch

def apply_translation(page, translation_map):
    def _apply_trans(obj):
        mapping = translation_map.get(obj['uk'])
        if mapping:
            obj['en'] = mapping

    _traverse_page(page,
        needs_translation,
        _apply_trans)

def translate_structure(structure, dry_run=False):
    items = get_translation_items(structure)
    if items and not dry_run:
        _logger.info(f'Batch translation: {len(items)} items...')
        start = time.time()
        result = translation(items)
        elapsed = time.time() - start
        _logger.info(f'    ...completed ({elapsed:.2f} sec.)')
        translation_map = { x[0]: x[1] for x in zip(items, result) }
        apply_translation(structure, translation_map)
    return len(items)

# --- ASYNC TRANSLATION MANAGER ---

_BATCH_SIZE = 10 if _USE_GOOGLE_CLOUD_TRANSLATE else 5 # google cloud is faster than the alternatives

class TranslationManager(TaskManager):
    def __init__(self, runtime):
        self._runtime = runtime
        self._available = True
        super().__init__("TranslationManager", auto_start=True)

    def execute_subtask(self, subtask):
        try:
            result = translation(subtask['payload'])
            # save translation pairs in resulting payload
            assert len(result) == len(subtask['payload'])
            subtask['payload'] = [list(x) for x in zip(subtask['payload'], result)]
            self._available = True
        except RateLimitExceeded as e:
            _logger.error(f'Translation Rate Limit Exception: {e}')
            # discard subtask - will result in incomplete translation
            subtask['payload'] = []
            self._available = False
        except ServiceCallError:
            _logger.error(f'Translation service API error')
            # discard subtask - will result in incomplete translation
            subtask['payload'] = []
            self._available = False            

    def complete_task(self, task_desc, subtasks):
        _logger.info(f"TranslationManager.complete: {task_desc['name']}")
        translation_map = {}
        for subtask in subtasks:
            for item in subtask['payload']:
                translation_map[item[0]] = item[1]
        page = self._runtime.lookup_by_title(task_desc['name'])
        page.apply_translation(translation_map)

    def translate(self, page):
        if not _ENABLE_TRANSLATION:
            raise TranslationDisabledError("Translation is disabled in this configuration")
        task_name = page.title
        try:
            self.lookup_by_name(task_name)
            # translation is already in progress
            return
        except KeyError:
            pass
        items = get_translation_items(page._page)
        total = len(items)
        if total > 0:
            batches = []
            for i in range(0, total, _BATCH_SIZE):
                batches.append(items[i:i+_BATCH_SIZE])
            self.create(task_name, batches)

    @property
    def available(self):
        return self.enabled and self._available

    @property
    def enabled(self):
        return _ENABLE_TRANSLATION

