# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Utility functions for Bird Dog
"""

import os
import re
import time
import psutil
import json
import requests
import random
import traceback
import sys
import ulid

from threading import Lock, Semaphore, Event, Thread
from datetime import datetime, timezone
from collections import deque

from birddog.log import get_logger
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

def new_id():
    return str(ulid.ulid())

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

class TooManyRequestsError(Exception):
    pass

class FetchUrlFailError(RuntimeError):
    """Raised when fetch_url fails after exhausting all retries."""
    pass

_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}

def fetch_url(
    url,
    params=None,
    send_json=None,
    return_json=False,
    content=False,
    method="GET",
    session=None,
    timeout=REQUEST_TIMEOUT,
    headers=None,
):
    """
    Centralized HTTP fetch utility with bounded concurrency, timeouts, and retry/backoff.

    - Uses `session` if provided for connection pooling; otherwise uses top-level requests.
    - Retries only transient conditions: timeouts, connection errors, 408/429/5xx.
    - Supports GET/POST/PATCH/DELETE with JSON payload.
    """
    client = session or requests
    hdrs = headers or _url_headers

    with _fetch_semaphore:
        attempt = 0
        last_exception = None

        while attempt < MAX_RETRIES:
            try:
                # Choose request function
                req = getattr(client, method.lower(), None)
                if req is None:
                    raise ValueError(f"fetch_url: unsupported method {method}")

                # Build kwargs
                kwargs = {"timeout": timeout, "headers": hdrs}
                if method.upper() == "GET":
                    kwargs["params"] = params
                else:
                    # For API calls, params can still be querystring; keep it if present
                    if params is not None:
                        kwargs["params"] = params
                    if send_json is not None:
                        kwargs["json"] = send_json

                response = req(url, **kwargs)
                #_logger.info(response.text)

                # Retry transient HTTP statuses
                if response.status_code in _TRANSIENT_HTTP:
                    raise requests.exceptions.HTTPError(
                        f"Transient HTTP {response.status_code}",
                        response=response,
                    )

                # Non-transient failures: do not retry by default
                if not response.ok:
                    # Preserve response text for debugging
                    if response.status_code == 404:
                        raise RuntimeError(f"Failed to fetch (404): {url} :: {response.text}")
                    raise requests.exceptions.HTTPError(
                        f"HTTP {response.status_code}: {response.text}",
                        response=response,
                    )

                _record_fetch_event()

                if return_json:
                    return response.json()
                if content:
                    return response.content
                return response.text

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exception = e

            except requests.exceptions.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                # Retry only transient HTTP statuses (already filtered above)
                if status not in _TRANSIENT_HTTP:
                    raise
                last_exception = e

            except requests.RequestException as e:
                # Other requests exceptions: usually not transient; raise
                raise FetchUrlFailError(str(e)) from e

            # Backoff and retry
            wait = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** attempt))
            wait += random.uniform(0, 1)
            _logger.info(
                f"[{attempt+1}/{MAX_RETRIES}] Error: {last_exception}. Retrying in {wait:.2f}s..."
            )
            time.sleep(wait)
            attempt += 1

        raise FetchUrlFailError(
            f"Failed to fetch after {MAX_RETRIES} retries: {url}"
        ) from last_exception

# SYSTEM RESOURCES --------------------------------------------------------

def system_resource_report(interval=1.0):
    """
    Return a snapshot of system resource utilization.
    Includes memory pressure, CPU load, and network I/O rate.
    
    Args:
        interval (float): seconds to wait for measuring CPU and network deltas.
    Returns:
        dict: resource statistics.
    """
    # --- CPU ---
    cpu_percent = psutil.cpu_percent(interval=interval)
    load_avg = None
    if hasattr(psutil, "getloadavg"):
        try:
            load_avg = psutil.getloadavg()
        except OSError:
            load_avg = None

    # --- Memory ---
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    mem_pressure = mem.percent + 0.5 * swap.percent  # heuristic composite

    # --- Network ---
    net1 = psutil.net_io_counters()
    time.sleep(interval)
    net2 = psutil.net_io_counters()
    bytes_sent_per_sec = (net2.bytes_sent - net1.bytes_sent) / interval
    bytes_recv_per_sec = (net2.bytes_recv - net1.bytes_recv) / interval

    return {
        "cpu_percent": cpu_percent,
        "load_avg": load_avg,
        "memory": {
            "total_mb": mem.total / 1e6,
            "available_mb": mem.available / 1e6,
            "used_percent": mem.percent,
            "swap_used_percent": swap.percent,
            "pressure_index": round(mem_pressure, 1),
        },
        "network": {
            "tx_kbps": bytes_sent_per_sec / 1024,
            "rx_kbps": bytes_recv_per_sec / 1024,
        }
    }

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

# Ukrainian letters (upper + lower, including Ґ, Є, І, Ї)
UA_LETTER = r"[А-ЩЬЮЯҐЄІЇа-щьюяґєії]"
# Optional hyphen: ASCII hyphen-minus + common Unicode hyphens
HYPHEN = r"[-\u00AD\u2010\u2011]?"
# Digits (one or more)
DIGITS = r"\d+"

def check_ukrainian_hyphen_numbers(s):
    """
    The pattern consists of a Ukrainian character, then optionally a hyphen
    as the second character, and numeric digits afterwards. If the string matches the pattern,
    it returns True; otherwise, it returns False.
    """

    # Matches Ukrainian char (Unicode Cyrillic range covers Ukrainian) + hyphen + digits
    pattern_prefix = re.compile(rf"^{UA_LETTER}{HYPHEN}{DIGITS}$")
    return bool(re.match(pattern_prefix, s))

def check_numbers_hyphen_ukrainian(s):
    """
    The pattern consists of numeric digits, then optionally a hyphen
    as the second character, and a Ukrainian character afterwards. If the string matches the pattern,
    it returns True; otherwise, it returns False.
    """

    # Matches Ukrainian char (Unicode Cyrillic range covers Ukrainian) + hyphen + digits
    pattern_suffix = re.compile(rf"^{DIGITS}{HYPHEN}{UA_LETTER}$")
    return bool(re.match(pattern_suffix, s))

def find_digits_around_ukrainian(s: str) -> tuple[int, int]:
    """
    Checks if string matches: one or more digits, followed by 1-2 Ukrainian letters,
    followed by one or more digits. Returns (position of first Ukrainian char, count)
    or (-1, 0) if no match.
    """
    pattern = re.compile(rf"^(\d+)({UA_LETTER}{{1,2}})(\d+)$")

    match = pattern.match(s)
    if match:
        first_digits = match.group(1)
        ukr_start = len(first_digits)  # 0-based position of first UA char
        ukr_count = len(match.group(2))
        return ukr_start, ukr_count
    return -1, 0

UKR_TO_LAT = {
    # Common Cyrillic + Ukrainian specifics
    'А': 'A', 'а': 'a', 'Б': 'B', 'б': 'b', 'В': 'V', 'в': 'v', 'Г': 'H', 'г': 'h',  # Г is /h/ in Ukrainian
    'Ґ': 'G', 'ґ': 'g', 'Д': 'D', 'д': 'd', 'Е': 'E', 'е': 'e', 'Є': 'Ye', 'є': 'ye',
    'Ж': 'Zh', 'ж': 'zh', 'З': 'Z', 'з': 'z', 'И': 'Y', 'и': 'y', 'І': 'I', 'і': 'i',
    'Ї': 'Yi', 'ї': 'yi', 'Й': 'Y', 'й': 'y', 'К': 'K', 'к': 'k', 'Л': 'L', 'л': 'l',
    'М': 'M', 'м': 'm', 'Н': 'N', 'н': 'n', 'О': 'O', 'о': 'o', 'П': 'P', 'п': 'p',
    'Р': 'R', 'р': 'r', 'С': 'S', 'с': 's', 'Т': 'T', 'т': 't', 'У': 'U', 'у': 'u',
    'Ф': 'F', 'ф': 'f', 'Х': 'Kh', 'х': 'kh', 'Ц': 'Ts', 'ц': 'ts', 'Ч': 'Ch', 'ч': 'ch',
    'Ш': 'Sh', 'ш': 'sh', 'Щ': 'Shch', 'щ': 'shch', 'Ь': '', 'ь': '', 'Ю': 'Yu', 'ю': 'yu',
    'Я': 'Ya', 'я': 'ya',
}

def translit_ukrainian_char(c: str) -> str:
    """Return Latin transliteration of a single Ukrainian character."""
    #print(f'Replacing {c} with {UKR_TO_LAT[c]}')
    return UKR_TO_LAT.get(c, c)

def transliterate(s, f=translit_ukrainian_char):
    if not s:
        return ''
    return ''.join(f(c) for c in str(s))

def replace_with_translit(s: str, ukr_start: int, ukr_count: int) -> str:
    """
    Replaces characters in a string with their Latin transliteration.

    Args:
        s: Input string
        ukr_start: Position of first Ukrainian letter
        ukr_count: Number of Ukrainian letters

    Returns:
        Modified string with Ukrainian characters transliterated
    """
    if not s:
        return s  # Return empty string unchanged

    result = s[: ukr_start]
    curr_posit = ukr_start
    for ind in range(ukr_count):
        result += translit_ukrainian_char(s[curr_posit])
        curr_posit += 1
    return result + s[curr_posit:]

def form_text_item(source_text):
    """Form a multilingual text item from a fragment of text.
    A text item is a dict containing keys "uk" and "en", representing the
    Ukrainian and English versions of the text, respectively.
    If the input text is numeric or is English, then both language versions
    will be the same. If the first character is a Ukrainian one followed by hyphen and numerals,
    the Ukrainian character is replaced by the Latin transliteration in the English version.
    Otherwise, the English version of the text will be left empty.
    """
    result = { 'uk': source_text }
    if not source_text or is_numeric(source_text) or is_english(source_text):
        result['en'] = source_text
    elif check_ukrainian_hyphen_numbers(source_text):
        result['en'] = replace_with_translit(source_text, 0, 1)
    elif check_numbers_hyphen_ukrainian(source_text):
        result['en'] = replace_with_translit(source_text, len(source_text) - 1, 1)
    elif (res := find_digits_around_ukrainian(source_text))[0] >= 0:
        result['en'] = replace_with_translit(source_text, res[0], res[1])
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
