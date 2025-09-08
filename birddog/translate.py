# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""Translation support from Ukrainian to English with async Translation manager"""

import os
import time
import random
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence, Union, Protocol

import requests
from requests.exceptions import Timeout, ReadTimeout, ConnectTimeout, ConnectionError as RequestsConnectionError

from deep_translator import GoogleTranslator, DeeplTranslator
from google.cloud import translate_v2 as google_translate
from google.api_core.exceptions import GoogleAPICallError
from google.api_core.exceptions import (
    GoogleAPICallError, RetryError,
    ServiceUnavailable, DeadlineExceeded, InternalServerError,
    TooManyRequests, ResourceExhausted,
    InvalidArgument, PermissionDenied, FailedPrecondition, Unauthorized, NotFound,
)

from birddog.task import TaskManager
from birddog.utility import json_size
from birddog.logging import get_logger, LogService
_logger = get_logger()

# --- Translation globals ---

_ENABLE_TRANSLATION = True

_USE_DUMMY_TRANSLATE = True
_USE_GOOGLE_CLOUD_TRANSLATE = os.getenv("BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE", None) in ("true", "True", "1")
_DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", None)

_MAX_TRANSLATE_REQUESTS_PER_HOUR = 1000  # example, adjust as needed

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

# ---- shared helpers ----

class _LocalRateLimitMixin:
    def __init__(self, *, max_per_hour: int | None = None):
        self._max_per_hour = max_per_hour
        self._ts = deque()
        self._lock = threading.Lock()

    def _check_quota_local(self, *, provider: str):
        if not self._max_per_hour:
            return
        now, cutoff = time.time(), time.time() - 3600
        with self._lock:
            while self._ts and self._ts[0] < cutoff:
                self._ts.popleft()
            if len(self._ts) >= self._max_per_hour:
                raise QuotaExceededError(provider=provider, retry_after_seconds=60)
            self._ts.append(now)

# --- Google Cloud service support ---

class GoogleCloudTranslator(Translator):
    def __init__(self, source="uk", target="en", *, provider_name="gcloud"):
        self._provider = provider_name
        self._source = source
        self._target = target
        self._client = google_translate.Client()
        self._lock = threading.Lock()
        self._timestamps = deque()

    def _check_quota_local(self):
        # app-level throttling (optional)
        now, cutoff = time.time(), time.time() - 3600
        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= _MAX_TRANSLATE_REQUESTS_PER_HOUR:
                raise QuotaExceededError(provider=self._provider, retry_after_seconds=60)
            self._timestamps.append(now)

    def _map_exc(self, e: BaseException) -> TranslationError:
        # Retryable classes
        if isinstance(e, (ServiceUnavailable, DeadlineExceeded, InternalServerError, RetryError)):
            return TransientServiceError(provider=self._provider, status_code=getattr(e, "code", None), original=e)
        if isinstance(e, (TooManyRequests, ResourceExhausted)):
            retry_after = getattr(getattr(e, "response", None), "retry_after", None)
            secs = None
            if retry_after:
                try:
                    secs = float(retry_after)
                except Exception:
                    pass
            return QuotaExceededError(provider=self._provider, retry_after_seconds=secs, original=e)
        # Non-retryable
        if isinstance(e, (InvalidArgument, PermissionDenied, FailedPrecondition, Unauthorized, NotFound)):
            return PermanentServiceError(provider=self._provider, status_code=getattr(e, "code", None), original=e)
        if isinstance(e, GoogleAPICallError):
            # default: treat unknown Google errors as non-retryable
            return PermanentServiceError(provider=self._provider, status_code=getattr(e, "code", None), original=e)
        # Networking fallbacks if you have any raw httpx/requests paths
        if isinstance(e, (Timeout, ReadTimeout, ConnectTimeout, RequestsConnectionError)):
            return TransientServiceError(provider=self._provider, original=e)
        # Last resort
        return PermanentServiceError(provider=self._provider, original=e)

    def translate(self, text: TextLike) -> TextLike:
        self._check_quota_local()
        try:
            with LogService("GCP", "translate", size=json_size(text)):
                result = self._client.translate(text, source_language=self._source, target_language=self._target)
            if isinstance(text, (list, tuple)):
                out = [item["translatedText"] for item in result]
                if len(out) != len(text):
                    raise IncompleteTranslationError(expected=len(text), got=len(out))
                return out
            return result["translatedText"]
        except Exception as e:
            raise self._map_exc(e)

    def translate_batch(self, text: Sequence[str]) -> Sequence[str]:
        return self.translate(text)  # supports batch

# --- Dummy translator (for debug) ---

class DummyTranslator(_LocalRateLimitMixin):
    """
    A deterministic/stochastic stub for tests and dev. Implements the Translator contract and
    raises only standardized exceptions.

    Failure knobs:
      - p_transient: chance per call to raise TransientServiceError (simulate 500/503/timeouts)
      - p_permanent: chance per call to raise PermanentServiceError (simulate 4xx/auth/args)
      - p_quota:     chance per call to raise QuotaExceededError (simulate provider 429)
      - p_contract:  chance per *batch* call to violate the length contract (debugging)
    Latency knobs:
      - mean_latency: base seconds per call (scaled by batch size)
      - jitter:       added uniform[0, jitter] seconds
      - per_item_ms:  extra milliseconds per input item (simulates batch size effect)
    """
    def __init__(
        self,
        *,
        source: str = "uk",
        target: str = "en",
        provider_name: str = "dummy",
        mode: str = "constant",             # "constant" or "echo"
        max_per_hour: int | None = None,    # app-level local quota
        seed: int | None = None,
        p_transient: float = 0.05,
        p_permanent: float = 0.01,
        p_quota: float = 0.00,
        p_contract: float = 0.00,
        mean_latency: float = 0.05,         # seconds per call
        jitter: float = 0.05,               # extra random seconds
        per_item_ms: float = 2.0,           # ms per item in a batch
        quota_retry_after_range: tuple[float, float] = (10.0, 30.0),
    ):
        super().__init__(max_per_hour=max_per_hour)
        self._provider = provider_name
        self._source, self._target = source, target
        self._mode = mode

        self._p_transient = float(p_transient)
        self._p_permanent = float(p_permanent)
        self._p_quota = float(p_quota)
        self._p_contract = float(p_contract)

        self._mean_latency = float(mean_latency)
        self._jitter = float(jitter)
        self._per_item_ms = float(per_item_ms)
        self._quota_retry_after_range = quota_retry_after_range

        self._rng = random.Random(seed)
        self._rng_lock = threading.Lock()   # thread-safe RNG

    # -------------- helpers --------------
    def _rand(self) -> float:
        with self._rng_lock:
            return self._rng.random()

    def _uniform(self, a: float, b: float) -> float:
        with self._rng_lock:
            return self._rng.uniform(a, b)

    def _maybe_sleep(self, n_items: int):
        # Base latency + jitter + small per-item scaling
        sleep_s = self._mean_latency + self._uniform(0.0, self._jitter) + (self._per_item_ms / 1000.0) * max(1, n_items)
        if sleep_s > 0:
            time.sleep(sleep_s)

    def _maybe_fault(self):
        # Order: provider quota -> transient -> permanent (tune as needed)
        r = self._rand()
        if r < self._p_quota:
            ra = self._uniform(*self._quota_retry_after_range)
            raise QuotaExceededError(provider=self._provider, retry_after_seconds=ra)
        r = self._rand()
        if r < self._p_transient:
            raise TransientServiceError(provider=self._provider)
        r = self._rand()
        if r < self._p_permanent:
            raise PermanentServiceError(provider=self._provider, original=ValueError("dummy permanent failure"))

    def _produce(self, s: str) -> str:
        if self._mode == "constant":
            return "I am a test dummy translator. This is placeholder text."
        elif self._mode == "echo":
            return s
        else:
            # Programmer error — don't retry
            raise PermanentServiceError(provider=self._provider,
                                        original=ValueError(f"Unknown dummy mode: {self._mode!r}"))

    # -------------- contract methods --------------
    def translate(self, text: TextLike) -> TextLike:
        # local app quota
        self._check_quota_local(provider=self._provider)

        # determine batch size for latency
        n_items = len(text) if isinstance(text, (list, tuple)) else 1

        # simulate latency/jitter
        self._maybe_sleep(n_items)

        # simulate faults
        self._maybe_fault()

        with LogService("DummyTranslator", "translate", size=json_size(text)):
            # normal path
            if isinstance(text, (list, tuple)):
                out = [self._produce(t) for t in text]

                # Optional: simulate a contract bug in the adapter
                if self._p_contract > 0.0 and self._rand() < self._p_contract:
                    out = out[:-1] or out  # drop one to trigger mismatch

                if len(out) != len(text):
                    raise IncompleteTranslationError(expected=len(text), got=len(out))
                return out

            if isinstance(text, str):
                return self._produce(text)

        # Bad caller input → permanent
        raise PermanentServiceError(provider=self._provider, original=TypeError(f"Invalid input type: {type(text)}"))

    def translate_batch(self, text: Sequence[str]) -> Sequence[str]:
        return self.translate(text)

# --- Configure Translator ---

if _ENABLE_TRANSLATION:
    if _USE_DUMMY_TRANSLATE:
        _logger.info('Using test dummy translator')
        _translator = DummyTranslator(source="uk", target="en")
    elif _USE_GOOGLE_CLOUD_TRANSLATE:
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

def translation(text: TextLike, *, retries=5, base_delay=1.0, max_delay=8.0, jitter=0.25):
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

        except PermanentServiceError as e:
            # fail fast — don't burn retries
            raise e

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
        translation_map = { x[0]: x[1] for x in zip(items, result) }
        apply_translation(structure, translation_map)
    return len(items)

# --- ASYNC TRANSLATION MANAGER ---

_BATCH_SIZE = 10 if _USE_GOOGLE_CLOUD_TRANSLATE else 5 # google cloud is faster than the alternatives

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
