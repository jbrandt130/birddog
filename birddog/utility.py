# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Utility functions for Bird Dog
"""

import os
import re
import time
import json
import requests
import random
import traceback
import sys
from threading import Lock, Semaphore, Event, Thread
from datetime import datetime, timezone
from collections import deque

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

def is_linked(item):
    return item and item.get("exists") and item.get("link") and not "redlink" in item.get("link")

def link_status(item):
    if item.get("link"):
        if item.get("exists"):
            return "exists"
        return "linked"
    return "unlinked"

def json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=float).encode("utf-8"))

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
RATE_LIMIT = 3     # fastest request rate allowed in reqs/sec
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
            if rate > RATE_LIMIT:
                _logger.info(f"fetch_url: rate limit exceeded - sleeping...")
                time.sleep(5)
            _last_log_time = now

def fetch_url(url, params=None, json=False, content=False, method="GET"):
    with _fetch_semaphore:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                if method == "POST":
                    response = requests.post(url, data=params, timeout=REQUEST_TIMEOUT, headers=_url_headers)
                else:
                    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=_url_headers)

                if response.status_code == 429:
                    raise TooManyRequestsError("429 Too Many Requests")
                if not response.ok:
                    if response.status_code == 404:
                        raise RuntimeError("Failed to fetch page (404)")
                    raise requests.RequestException(f"Unexpected status: {response.status_code}")
                
                _record_fetch_event()
                return response.json() if json else response.content if content else response.text
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

def from_utc_format(utc_str):
    dt = datetime.strptime(utc_str, '%Y-%m-%dT%H:%M:%SZ')
    return dt.strftime('%Y,%m,%d,%H:%M')

def to_utc_format(time_str):
    # Split the input and pad with default values
    parts = time_str.split(',')
    defaults = ['2000', '01', '01', '00:00']  # month, day, hour, minute
    full_parts = parts + defaults[len(parts):]
    # Join into full timestamp string
    full_str = ','.join(full_parts)
    dt = datetime.strptime(full_str, '%Y,%m,%d,%H:%M')
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
   
#
# multilingual support

number_pattern = re.compile('[0-9]+([–-][0-9]+)?')
def is_numeric(text):
    """True if string is one or more digits or a dash separated numeric range."""
    return re.fullmatch(number_pattern, text.strip()) is not None

def is_english(text):
    """True if argument is decodable to ascii (misnomer?)"""
    try:
        text.encode(encoding='utf-8').decode('ascii')
    except UnicodeDecodeError:
        return False
    return True

def form_text_item(source_text):
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
    return text_item.get('en', text_item.get('uk' '')) if isinstance(text_item, dict) else text_item

def match_text(text_item, text):
    """Check if the given text matches either the Ukrainian or English version
    of a multilingual text item."""
    return text == text_item.get('uk') or text == text_item.get('en')

# BACKGROUND PROCESSING ----------------------------------------------------------

class HeartbeatManager:
    def __init__(self, interval=1.0):
        self.interval = interval
        self._stop_event = Event()
        self._thread = Thread(target=self._run_heartbeat, daemon=True)
        self._started = False
        self._held = False

    def start(self):
        if not self._started:
            self._thread.start()
            self._started = True

    def stop(self):
        if self._started:
            self._stop_event.set()
            self._thread.join()
            self._started = False

    def hold(self):
        self._held = True

    def release(self):
        self._held = False

    def _run_heartbeat(self):
        while not self._stop_event.is_set():
            try:
                if self._started and not self._held:
                    self.heartbeat()
            except Exception as e:
                _logger.error(f"Exception during heartbeat: {e}")
                tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                _logger.error(f"Stack trace:\n{tb_str}")
            time.sleep(self.interval)

    def heartbeat(self):
        """Override this method in subclasses to perform periodic actions."""
        _logger.info("Heartbeat...")
