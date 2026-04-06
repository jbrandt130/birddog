# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""Translation support from Ukrainian to English with async Translation manager"""

import os
import time
import random
import threading
import html
from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence, Union, Protocol

from requests.exceptions import Timeout, ReadTimeout, ConnectTimeout, ConnectionError as RequestsConnectionError

from google.cloud import translate_v2 as google_translate
from google.api_core.exceptions import (
    GoogleAPICallError, RetryError,
    ServiceUnavailable, DeadlineExceeded, InternalServerError,
    TooManyRequests, ResourceExhausted,
    InvalidArgument, PermissionDenied, FailedPrecondition, Unauthorized, NotFound,
)

from birddog.task import TaskManager
from birddog.utility import json_size
from birddog.fetch import fetch_url, FetchUrlFailError
from birddog.log import get_logger, LogService
_logger = get_logger()

# --- Translation globals ---

_ENABLE_TRANSLATION = True
_USE_GOOGLE_CLOUD_TRANSLATE = os.getenv("BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE", None) in ("true", "True", "1")

if _ENABLE_TRANSLATION and _USE_GOOGLE_CLOUD_TRANSLATE:
    _logger.info("Translation is enabled. Using GCP translator")

_MAX_TRANSLATE_REQUESTS_PER_MINUTE = 1000  # example, adjust as needed

_translator = None

# -- Standardized translator exceptions

TextLike = Union[str, Sequence[str]]

class TranslationError(Exception):
    """Base class for all translation errors."""

@dataclass
class TransientServiceError(TranslationError):
    """Temporary failure; caller may retry."""
    provider: str
    status_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    original: Optional[BaseException] = None

@dataclass
class QuotaExceededError(TranslationError):
    """Provider (or our app) rate/quota exceeded; retry after a delay."""
    provider: str
    retry_after_seconds: Optional[float] = None
    original: Optional[BaseException] = None

@dataclass
class PermanentServiceError(TranslationError):
    """Non-retryable: auth, invalid args, not found, etc."""
    provider: str
    status_code: Optional[int] = None
    original: Optional[BaseException] = None

class IncompleteTranslationError(TranslationError):
    def __init__(self, *, expected: int, got: Union[int, str]):
        super().__init__(f"Batch translation length mismatch: expected {expected}, got {got}")
        self.expected, self.got = expected, got

class TranslationDisabledError(TranslationError):
    """Feature is disabled."""

class MaxRetriesExceeded(TranslationError):
    def __init__(self, *, attempts: int, last_error: Optional[BaseException] = None):
        super().__init__(f"Service call failed after {attempts} attempts.")
        self.attempts, self.last_error = attempts, last_error

# --- Translator protocol ---

class Translator(Protocol):
    def translate(self, text: TextLike) -> TextLike: ...
    def translate_batch(self, text: Sequence[str]) -> Sequence[str]: ...

# --- Google Cloud service support ---

class GoogleCloudTranslator(Translator):
    """
    Uses v2 REST with API key if GOOGLE_TRANSLATE_API_KEY is set.
    Otherwise uses translate_v2.Client() (service account / ADC).
    """
    _V2_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self, source="uk", target="en", *, provider_name="gcloud", timeout=10, use_client=False):
        self._provider = provider_name
        self._source = source
        self._target = target
        self._timeout = timeout

        self._api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY")
        self._use_rest = bool(self._api_key) and not use_client
        if self._use_rest:
            _logger.info(f"GoogleCloudTranslator using REST API")
        else:
            # falls back to your existing client (needs service-account/ADC)
            _logger.info(f"GoogleCloudTranslator using translate_v2 python client")
            self._client = google_translate.Client()

        # local quota guard
        self._lock = threading.Lock()
        self._timestamps = deque()

    # ----------- quota guard (unchanged behavior) -----------------------------
    def _check_quota_local(self):
        sample_window = 5 # minutes
        now = time.time()
        cutoff = now - sample_window * 60 # seconds
        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= _MAX_TRANSLATE_REQUESTS_PER_MINUTE * sample_window:
                raise QuotaExceededError(provider=self._provider, retry_after_seconds=60)
            self._timestamps.append(now)

    # ----------- exception mapping (works for REST + client) ------------------

    def _map_exc(self, e: BaseException) -> TranslationError:
        _logger.info(f"Translation exception: {e}")
        # Retryable (Google client)
        if isinstance(e, (ServiceUnavailable, DeadlineExceeded, InternalServerError, RetryError)):
            return TransientServiceError(provider=self._provider, status_code=getattr(e, "code", None), original=e)
        if isinstance(e, (TooManyRequests, ResourceExhausted)):
            retry_after = getattr(getattr(e, "response", None), "retry_after", None)
            secs = None
            if retry_after:
                try: secs = float(retry_after)
                except Exception: pass
            return QuotaExceededError(provider=self._provider, retry_after_seconds=secs, original=e)
        # Non-retryable (Google client)
        if isinstance(e, (InvalidArgument, PermissionDenied, FailedPrecondition, Unauthorized, NotFound)):
            return PermanentServiceError(provider=self._provider, status_code=getattr(e, "code", None), original=e)
        if isinstance(e, GoogleAPICallError):
            return PermanentServiceError(provider=self._provider, status_code=getattr(e, "code", None), original=e)

        # If we bubbled an HTTP error with a response, classify by status
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        if isinstance(status, int):
            if status == 429:
                return QuotaExceededError(provider=self._provider, original=e)
            if 500 <= status < 600:
                return TransientServiceError(provider=self._provider, status_code=status, original=e)
            # 4xx default: permanent
            if 400 <= status < 500:
                return PermanentServiceError(provider=self._provider, status_code=status, original=e)

        if isinstance(e, FetchUrlFailError):
            cause = getattr(e, "__cause__", None)
            # If the last cause was an HTTPError with a response, classify by status
            resp = getattr(cause, "response", None)
            status = getattr(resp, "status_code", None)
            if status == 429:
                return QuotaExceededError(provider=self._provider, original=e)
            if isinstance(status, int) and 500 <= status < 600:
                return TransientServiceError(provider=self._provider, status_code=status, original=e)
            # networkish causes
            if isinstance(cause, (Timeout, ReadTimeout, ConnectTimeout, RequestsConnectionError)):
                return TransientServiceError(provider=self._provider, original=e)

        return PermanentServiceError(provider=self._provider, original=e)

    # ----------- public API ---------------------------------------------------
    def translate(self, text: TextLike) -> TextLike:
        self._check_quota_local()
        try:
            if self._use_rest:
                with LogService("GoogleCloudTranslate", "translate", size=json_size(text)):
                    return self._translate_v2_rest(text)
            else:
                with LogService("GoogleCloudTranslate", "translate", size=json_size(text)):
                    return self._translate_v2_client(text)
        except Exception as e:
            raise self._map_exc(e)

    def translate_batch(self, text: Sequence[str]) -> Sequence[str]:
        return self.translate(text)  # both paths support batch

    # ----------- v2 via REST (API key) ---------------------------------------
    def _translate_v2_rest(self, text: TextLike) -> TextLike:
        is_list = isinstance(text, (list, tuple))
        q = list(text) if is_list else [text]

        params = {
            "key": self._api_key,
            "source": self._source,
            "target": self._target,
            "format": "text",
        }

        # Google v2 REST expects repeated 'q' fields; preserve prior behavior.
        form_data = [("q", s) for s in q]

        payload = fetch_url(
            self._V2_ENDPOINT,
            method="POST",
            params=params,
            data=form_data,
            return_json=True,
            timeout=self._timeout,
        )

        # REST v2: payload["data"]["translations"] -> list of dicts
        translations = payload["data"]["translations"]
        out = [html.unescape(t["translatedText"]) for t in translations]

        if is_list:
            if len(out) != len(q):
                raise IncompleteTranslationError(expected=len(q), got=len(out))
            return out

        return out[0]

    # ----------- v2 via client (service account) ------------------------------
    def _translate_v2_client(self, text: TextLike) -> TextLike:
        if isinstance(text, (list, tuple)):
            result = self._client.translate(
                text,
                source_language=self._source,
                target_language=self._target,
                format_="text",
            )
            return [html.unescape(r["translatedText"]) for r in result]
        else:
            result = self._client.translate(
                text,
                source_language=self._source,
                target_language=self._target,
                format_="text",
            )
            return html.unescape(result["translatedText"])

# --- Configure Translator ---

if _ENABLE_TRANSLATION:
    if _USE_GOOGLE_CLOUD_TRANSLATE:
        _logger.info('Using Google Cloud translation API')
        _translator = GoogleCloudTranslator(source="uk", target="en")
    else:
        _ENABLE_TRANSLATION = False

# --- Basic Translation Logic ---

def translation(text: TextLike, *, retries=2, base_delay=1.0, max_delay=8.0, jitter=0.25):
    if not _ENABLE_TRANSLATION:
        raise TranslationDisabledError("Translation is disabled in this configuration")

    last_exc: Optional[TranslationError] = None
    delay = base_delay

    for attempt in range(1, retries + 1):
        try:
            result = _translator.translate_batch(text) if isinstance(text, (list, tuple)) else _translator.translate(text)
            # batch len check already done in adapter; single result returned as str
            return result

        except QuotaExceededError as e:
            last_exc = e
            # Treat as retryable, but consider a larger sleep if retry_after provided
            sleep_for = e.retry_after_seconds or (min(delay, max_delay) + random.uniform(0, jitter))
            _logger.info("quota exceeded by %s — sleeping %.2fs (attempt %d/%d)", e.provider, sleep_for, attempt, retries)
            if attempt < retries:
                time.sleep(sleep_for)
                delay = min(delay * 2, max_delay)
                continue
            break

        except TransientServiceError as e:
            last_exc = e
            _logger.info("transient error %s from %s (attempt %d/%d) — retrying", e.__class__.__name__, e.provider, attempt, retries)
            if attempt < retries:
                sleep_for = min(delay, max_delay) + random.uniform(0, jitter)
                time.sleep(sleep_for)
                delay = min(delay * 2, max_delay)
                continue
            break

        except PermanentServiceError:
            # fail fast — don't burn retries
            raise

        except IncompleteTranslationError:
            # fail fast; it's a logic/contract issue
            raise

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
        translation_map = dict(zip(items, result))
        apply_translation(structure, translation_map)
    return len(items)

# --- ASYNC TRANSLATION MANAGER ---

_BATCH_SIZE = 10

class TranslationManager(TaskManager):
    def __init__(self, runtime):
        self._runtime = runtime
        self._available = True
        super().__init__("TranslationManager")

    def execute_subtask(self, subtask):
        """
        Policy: never raise from here (prevents upstream requeue).
        On any failure, clear payload to [] so the overall translation is incomplete.
        Use _available to hint scheduler behavior.
        """
        try:
            src_batch = subtask["payload"]
            result = translation(src_batch)

            if not isinstance(result, (list, tuple)) or len(result) != len(src_batch):
                raise IncompleteTranslationError(expected=len(src_batch),
                                                 got=(len(result) if hasattr(result, "__len__") else "n/a"))

            subtask["payload"] = [list(p) for p in zip(src_batch, result)]
            self._available = True  # worker is healthy
            return

        # Provider/app quota exceeded (e.g., 429 / ResourceExhausted)
        except QuotaExceededError as e:
            _logger.warning(
                "Translation quota exceeded (provider=%s, retry_after=%s)",
                getattr(e, "provider", "?"),
                getattr(e, "retry_after_seconds", None),
            )
            subtask["payload"] = []     # mark shard incomplete
            self._available = False     # hint: cool this worker down
            return

        # Temporary network/service issues that *might* work later
        except TransientServiceError as e:
            _logger.info("Transient translation error (provider=%s): %s",
                         getattr(e, "provider", "?"), e)
            subtask["payload"] = []
            self._available = False     # hint: brief backoff
            return

        # translation() already retried and gave up
        except MaxRetriesExceeded as e:
            _logger.error("Translation retries exhausted: attempts=%s last_error=%r",
                          e.attempts, e.last_error)
            subtask["payload"] = []
            self._available = False
            return

        # Non-retryable: auth/config/bad request/etc.
        except PermanentServiceError as e:
            _logger.error("Permanent translation error (provider=%s): %s",
                          getattr(e, "provider", "?"), e)
            subtask["payload"] = []
            self._available = True      # worker fine; shard is dead
            return

        # Contract/logic problem in the adapter or upstream
        except IncompleteTranslationError as e:
            _logger.error("Translation contract error: %s", e)
            subtask["payload"] = []
            self._available = True
            return

        # Feature is disabled — don’t requeue
        except TranslationDisabledError as e:
            _logger.error("Translation disabled: %s", e)
            subtask["payload"] = []
            self._available = True
            return

        # Absolutely everything else — never raise
        except Exception as e:
            _logger.exception("Unexpected translation failure: %r", e)
            subtask["payload"] = []
            self._available = False     # be conservative
            return

    def complete_task(self, task_desc, subtasks, is_cancelled=False):
        if is_cancelled:
            return
        _logger.info(f"TranslationManager.complete: {task_desc['name']}")
        translation_map = {}
        for subtask in subtasks:
            for item in subtask['payload']:
                translation_map[item[0]] = item[1]
        self._runtime.complete_translation(task_desc['name'], translation_map)

    def translate(self, page):
        task_name = page.title
        try:
            self.lookup_by_name(task_name)
            # translation is already in progress
            return
        except KeyError:
            pass
        items = get_translation_items(page._page)
        self.start_translate_task(task_name, items)

    def start_translate_task(self, task_name, items):
        if not _ENABLE_TRANSLATION:
            raise TranslationDisabledError("Translation is disabled in this configuration")
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
