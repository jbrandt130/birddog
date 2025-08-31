# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""Translation support from Ukrainian to English with async Translation manager"""

import os
import time
from httpcore._exceptions import ReadTimeout, ConnectTimeout

import requests
from deep_translator import GoogleTranslator, DeeplTranslator
from google.cloud import translate_v2 as google_translate

from birddog.task import TaskManager
from birddog.logging import get_logger
_logger = get_logger()

# --- Google Cloud service support ---

class GoogleCloudTranslator:
    def __init__(self, source="uk", target="en"):
        self._source = source
        self._target = target
        self._client = google_translate.Client()

    def _log_bytes(self, text):
        if isinstance(text, (list, tuple)):
            total_bytes = sum(len(t.encode('utf-8')) for t in text)
        else:
            total_bytes = len(text.encode('utf-8'))
        _logger.info(f"GoogleCloudTranslator: translating {total_bytes} bytes")

    def translate(self, text):
        self._log_bytes(text)
        result = self._client.translate(text, source_language=self._source, target_language=self._target)
        if isinstance(text, (list, tuple)):
            return [item['translatedText'] for item in result]
        return result['translatedText']

    def translate_batch(self, text):
        return self.translate(text)

# --- Configure Translator ---

_DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", None)
_USE_GOOGLE_CLOUD_TRANSLATE = os.getenv("BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE", None) in ("true", "True", "1")

_translator = None
if _USE_GOOGLE_CLOUD_TRANSLATE:
    _logger.info(f'Using Google Cloud translation API (credentials file:{os.getenv("GOOGLE_APPLICATION_CREDENTIALS")})')
    #_logger.info('Using Google Cloud translation API')
    _translator = GoogleCloudTranslator(source="uk", target="en")
elif _DEEPL_API_KEY:
    _logger.info('Using DeepL translation API')
    _translator = DeeplTranslator(api_key=_DEEPL_API_KEY, source="uk", target="en", use_free_api=True)
else:
    _logger.info('Using free Google translation API')
    _translator = GoogleTranslator(source='uk', target='en')

# --- Basic Translation Logic ---

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
            _logger.info("translation timeout. retrying...")
        time.sleep(wait_time)
        wait_time *= 2
    return result

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
        super().__init__("TranslationManager", auto_start=True)

    def execute_subtask(self, subtask):
        result = translation(subtask['payload'])
        # save translation pairs in resulting payload
        subtask['payload'] = [list(x) for x in zip(subtask['payload'], result)]

    def complete_task(self, task_desc, subtasks):
        print("TranslationManager.complete:", task_desc['name'])
        translation_map = {}
        for subtask in subtasks:
            for item in subtask['payload']:
                translation_map[item[0]] = item[1]
        page = self._runtime.lookup_by_title(task_desc['name'])
        page.apply_translation(translation_map)

    def translate(self, page):
        task_name = page.title
        items = get_translation_items(page._page)
        total = len(items)
        if total > 0:
            batches = []
            for i in range(0, total, _BATCH_SIZE):
                batches.append(items[i:i+_BATCH_SIZE])
            self.create(task_name, batches)
