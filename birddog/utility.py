# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Utility functions for archivescraper.
"""

import re
import time
import json
import requests
from bs4 import BeautifulSoup

from birddog.translate import (
    translation,
    is_english,
    queue_translation,
    is_translation_running,
    )

#
# standard logger

import logging
import sys

def get_logger():
# Configure the logging system
    logging.basicConfig(
        level=logging.INFO,  # Change to DEBUG for more detailed logs
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout)  # <-- Critical for EB log capture
        ]
    )
    return logging.getLogger(__name__)

logger = get_logger()

# INITIALIZATION --------------------------------------------------------------

# global constants

ARCHIVE_BASE    = 'https://uk.wikisource.org'
UK_MONTHS       = None
ARCHIVE_LIST    = None
ARCHIVES        = None

# load static data resources

with open('resources/archives.json', encoding="utf8") as f:
    ARCHIVE_LIST = json.load(f)
    ARCHIVE_LIST = {k: v for (k, v) in ARCHIVE_LIST.items() if v is not None}

# used for standardizing dates in numerical format
with open('resources/months.json', encoding="utf8") as f:
    UK_MONTHS = json.load(f)

with open('resources/archives_master.json', encoding="utf8") as f:
    ARCHIVES = json.load(f)

def _inventory_subarchives(archives):
    subarchives = {}
    for arc_key, arc in archives.items():
        for sub in arc.values():
            subarchives[sub['subarchive']['uk']] = sub['subarchive']
    return list(subarchives.values())

SUBARCHIVES = _inventory_subarchives(ARCHIVES)

#
# subarchive sniffer

def find_subarchives(archive):
    url = f'{ARCHIVE_BASE}/wiki/{archive}'
    result = {}
    soup = BeautifulSoup(requests.get(url).text, 'lxml')
    for div in soup.find_all('div', attrs = {'id': 'mw-content-text'}):
        for item in div.find_all('a'):
            if item.has_attr('title'):
                if item['title'].startswith(archive):
                    if 'redlink' not in item['href']:
                        parsed = item['title'].split('/')
                        if len(parsed) == 2 and parsed[1] != 'видання':
                            subarchive = parsed[1]
                            result[subarchive] = {
                                'title': form_text_item(item['title']),
                                'archive': form_text_item(parsed[0]),
                                'subarchive': form_text_item(parsed[1]),
                                'description': form_text_item(item.text),
                                'link': item['href'],
                                }
    return result

#
# date handling

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
            logger.info(f'Batch translation: {len(batch)} items...')
            start = time.time()
            batch = translation(batch)
            elapsed = time.time() - start
            logger.info(f'    ...completed ({elapsed:.2f} sec.)')
            store_result(items, batch)
    return len(batch)
