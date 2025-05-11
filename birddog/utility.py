# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Utility functions for Bird Dog
"""

import re
import time
import json
import requests
import random
from threading import Lock, Semaphore
from datetime import datetime, timezone
from collections import deque

from birddog.translate import (
    translation,
    is_english,
    queue_translation)

from birddog.logging import get_logger
_logger = get_logger()

# INITIALIZATION --------------------------------------------------------------

# global constants

UK_MONTHS       = None

# used for standardizing dates in numerical format
with open('resources/months.json', encoding="utf8") as f:
    UK_MONTHS = json.load(f)

# UTILITY FUNCTIONS --------------------------------------------------------

#
# helper functions

def is_linked(url):
    return url and not "redlink" in url

#
# page loading

MAX_RETRIES = 5
BASE_BACKOFF = 1.0  # seconds
MAX_BACKOFF = 30.0  # max wait time in seconds
REQUEST_TIMEOUT = 10 # seconds
MAX_CONCURRENT_FETCHES = 5

_fetch_semaphore = Semaphore(MAX_CONCURRENT_FETCHES)
_url_headers = {
        'User-Agent': 'BirddogBot/1.0 (non-commercial research, contact: birddogpound@gmail.com)'
    }

# fetch rate instrumentation
_fetch_timestamps = deque()
_fetch_timestamps_lock = Lock()
LOG_INTERVAL = 10  # seconds between logs
RATE_WINDOW = 60   # how far back to count requests/sec
_last_log_time = 0

def _record_fetch_event():
    global _last_log_time
    now = time.time()
    with _fetch_timestamps_lock:
        _fetch_timestamps.append(now)
        cutoff = now - RATE_WINDOW
        while _fetch_timestamps and _fetch_timestamps[0] < cutoff:
            _fetch_timestamps.popleft()
        if now - _last_log_time >= LOG_INTERVAL:
            rate = len(_fetch_timestamps) / RATE_WINDOW if _fetch_timestamps else 0.0
            if rate > 0:
                _logger.info(f"fetch_url: {len(_fetch_timestamps)} requests in last {RATE_WINDOW}s → {rate:.2f} req/s")
            _last_log_time = now

def fetch_url(url, params=None, json=False):
    with _fetch_semaphore:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=_url_headers)
                if response.status_code == 429:
                    raise TooManyRequestsError("429 Too Many Requests")
                if not response.ok:
                    raise requests.RequestException(f"Unexpected status: {response.status_code}")
                _record_fetch_event()
                return response.json() if json else response.text
            except (requests.RequestException, TooManyRequestsError) as e:
                wait = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** attempt))  # exponential backoff
                wait += random.uniform(0, 1)  # add jitter
                _logger.info(f"[{attempt+1}/{MAX_RETRIES}] Error: {e}. Retrying in {wait:.2f} seconds...")
                time.sleep(wait)
                attempt += 1
        raise RuntimeError("Failed to fetch page after several retries")

class TooManyRequestsError(Exception):
    pass

#
# date handling

def now(universal=False):
    result = datetime.now(timezone.utc) if universal else datetime.now()
    return result.strftime('%Y,%m,%d,%H:%M')

def format_date(message):
    """Convert Ukranian date string such as "19:15, 20 травня 2023" to standard form.
    Standard form is "YYYY,MM,DD,HH:mm"
    """
    message = message.replace(',', '').split(' ')
    def date_number(num):
        return UK_MONTHS[num] if num in UK_MONTHS else f'0{num}' if len(num) == 1 else num
    message = map(date_number, message)
    message = ','.join(reversed(list(message)))
    return message

lastmod_pattern = re.compile('[0-9][0-9]:[0-9][0-9].+[0-9][0-9]?.+[0-9][0-9][0-9][0-9]')

def lastmod(message):
    """Extract 'last modified' date string from within a section of text.
    It is formatted in standard form: "YYYY,MM,DD,HH:mm"
    """
    result = re.search(lastmod_pattern, message)
    if result is not None:
        return format_date(result.group(0))
    return message

def convert_utc_time(utc_str):
    dt = datetime.strptime(utc_str, '%Y-%m-%dT%H:%M:%SZ')
    return dt.strftime('%Y,%m,%d,%H:%M')

#
# multilingual support

number_pattern = re.compile('[0-9]+([–-][0-9]+)?')
def is_numeric(text):
    """True if string is one or more digits or a dash separated numeric range."""
    return re.fullmatch(number_pattern, text.strip()) is not None

def form_text_item(source_text, translate=False):
    """Form a multilingual text item from a fragment of text.
    A text item is a dict containing keys "uk" and "en", representing the
    Ukrainian and English versions of the text, respectively.
    If the input text is numeric or is English, then both language versions
    will be the same. If the translate argument is True (default False),
    then the Ukrainian text will be automatically translated to English.
    Otherwise, the English version of the text will be left empty.
    """
    result = { 'uk': source_text }
    if not source_text or is_numeric(source_text) or is_english(source_text):
        result['en'] = source_text
    if translate:
        result['en'] = translation(source_text)
    return result

def equal_text(item1, item2):
    """True if both language versions of the text item are equal.
    If the English translation is missing from either item, then only
    the Ukrainian text is compared.
    """
    if 'en' in item1 and 'en' in item2:
        return item1['en'] == item2['en']
    return item1['uk'] == item2['uk']

def get_text(text_item):
    """Return the English version of the text item if present, else use Ukrainian."""
    return text_item.get('en', text_item.get('uk' '')) if isinstance(text_item, dict) else ''

def match_text(text_item, text):
    """Check if the given text matches either the Ukrainian or English version
    of a multilingual text item."""
    return text == text_item.get('uk') or text == text_item.get('en')

def needs_translation(item):
    """True if text item needs to be translated to English"""
    return isinstance(item, dict) and 'uk' in item and 'en' not in item

def translate_page(
    page,
    dry_run=False,
    asynchronous=False,
    progress_callback=None,
    completion_callback=None):
    """Traverse a page and translate all untranslated text items.
    The changes are done in place on each multilingual text item.
    Returns the total number of items translated.
    """
    batch = []
    items = []

    # recursive function to traverse down dictionaries and lists
    # to find all multilingual text items and queue them into an
    # array for batch translation.
    def queue_items(obj, batch, items):
        if needs_translation(obj):
            batch.append(obj['uk'])
            items.append(obj)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                queue_items(value, batch, items)
        elif isinstance(obj, dict):
            for value in obj.values():
                queue_items(value, batch, items)

    def store_result(items, batch):
        for i, value in enumerate(batch):
            items[i]['en'] = value

    queue_items(page, batch, items)

    if batch and not dry_run:
        if asynchronous:
            def completion_cb(task_id, result):
                store_result(items, result)
                if completion_callback:
                    completion_callback(task_id, result)
            return queue_translation(batch, progress_callback, completion_cb)
        else:
            _logger.info(f'Batch translation: {len(batch)} items...')
            start = time.time()
            batch = translation(batch)
            elapsed = time.time() - start
            _logger.info(f'    ...completed ({elapsed:.2f} sec.)')
            store_result(items, batch)
    return len(batch)
