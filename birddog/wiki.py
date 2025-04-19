# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Wiki API access functions
"""

import time
import json
import mwparserfromhell
from urllib.parse import quote
from itertools import islice
from cachetools import LRUCache

from birddog.utility import (
    fetch_url,
    convert_utc_time,
    )

from birddog.logging import get_logger
_logger = get_logger()

# INITIALIZATION --------------------------------------------------------------

# global constants

ARCHIVE_BASE    = 'https://uk.wikisource.org'
WIKI_NAMESPACE  = 'Архів'
ARCHIVE_LIST    = None
ARCHIVES        = None

# load static data resources

with open('resources/archives.json', encoding="utf8") as f:
    ARCHIVE_LIST = json.load(f)
    ARCHIVE_LIST = {k: v for (k, v) in ARCHIVE_LIST.items() if v is not None}

with open('resources/archives_master.json', encoding="utf8") as f:
    ARCHIVES = json.load(f)

def _inventory_subarchives(archives):
    subarchives = {}
    for arc_key, arc in archives.items():
        for sub in arc.values():
            subarchives[sub['subarchive']['uk']] = sub['subarchive']
    return list(subarchives.values())

SUBARCHIVES = _inventory_subarchives(ARCHIVES)

# -------------------------------------------------------------------------------
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

# -------------------------------------------------------------------------------
# Page revision history handling (using wiki API)

def wiki_title(page_title):
    return f'{WIKI_NAMESPACE}:{page_title}'

def history_url(page_title, limit=1):
    return ('https://uk.wikisource.org/w/api.php?action=query&format=json'
            '&prop=revisions&rvprop=ids|timestamp'
            f'&rvlimit={limit}&titles={quote(wiki_title(page_title))}')

def page_revision_url(page_title, revid):
    return ('https://uk.wikisource.org/w/index.php?'
            f'title={quote(wiki_title(page_title))}&oldid={revid}')

def get_page_history(page_title, limit=10):
    result = fetch_url(history_url(page_title, limit=limit), json=True)
    query = result.get('query')
    #_logger.info(f'get_page_history({page_title}, limit={limit}): result={query}')

    if not query:
        _logger.error(f'get_page_history({page_title}, limit={limit}): no result returned')
        return []
    pages = query.get('pages')
    if not pages:
        _logger.error(f'get_page_history({page_title}, limit={limit}): empty result returned')
        return []
    if '-1' in pages:
        _logger.error(f'get_page_history({page_title}, limit={limit}): unrecognized page name')
        return []
    # assume only one page is returned (in future, pass multiple to reduce api calls)
    for page in pages.values():
        history = [ { 
            'revid': rev['revid'],
            'modified': convert_utc_time(rev['timestamp']),
            'link': page_revision_url(page_title, rev['revid'])
        } for rev in page.get('revisions') ]
        return history
    _logger.error(f'get_page_history({page_title}, limit={limit}): unexpected result returned')
    return []

def get_page_history_from_cutoff(page_title, cutoff_date):
    # search increasingly for cutoff date  because
    # api does not allow for paging through search results
    last_result_length = 0
    attempt = 50
    while True:
        result = get_page_history(page_title, limit=attempt)
        if not result:
            _logger.error(f'get_page_history({page_title}, cutoff_date={cutoff_date}): empty history')
            return []
        if len(result) == last_result_length:
            result[-1]['created'] = True
            return result # no more history to be had
        if result[-1]['modified'] <= cutoff_date:
            for index, item in enumerate(result):
                if item['modified'] <= cutoff_date:
                    return result[:(index+1)]
            return result
        # increase limit length and try again
        last_result_length = len(result)
        attempt *= 2


# -------------------------------------------------------------------------------
# History LRU

class HistoryLRU:
    def __init__(self, maxsize=500, reset_limit=60 * 60):
        self._reset_limit = reset_limit  # seconds
        self._timer_start = time.time()
        self._lru = LRUCache(maxsize=maxsize)

    def _flush_if_needed(self):
        if time.time() - self._timer_start >= self._reset_limit:
            _logger.info("HistoryLRU: flushing all entries")
            self._lru.clear()
            self._timer_start = time.time()

    def _filter_with_fallback(self, history, cutoff_date):
        split = next((i for i, h in enumerate(history) if h['modified'] <= cutoff_date), len(history))
        return history[:split + 1]

    def lookup(self, page_title, limit=10):
        self._flush_if_needed()
        try:
            history = self._lru[page_title]
            _logger.info(f"HistoryLRU.lookup({page_title}): cache hit")
            if len(history) >= limit:
                return history[:limit]
            _logger.info(f"HistoryLRU.lookup({page_title}): cache too short, refreshing")
        except KeyError:
            _logger.info(f"HistoryLRU.lookup({page_title}): cache miss")
        # Refresh
        history = get_page_history(page_title, limit=limit)
        self._lru[page_title] = history
        return history[:limit]

    def lookup_by_cutoff(self, page_title, cutoff_date):
        self._flush_if_needed()
        try:
            history = self._lru[page_title]
            _logger.info(f"HistoryLRU.lookup_by_cutoff({page_title}): cache hit")

            if history:
                oldest = history[-1]
                if oldest.get('created') or oldest['modified'] < cutoff_date:
                    # We have enough
                    return self._filter_with_fallback(history, cutoff_date)
                _logger.info(f"HistoryLRU.lookup_by_cutoff({page_title}): cache incomplete, refreshing")
        except KeyError:
            _logger.info(f"HistoryLRU.lookup_by_cutoff({page_title}): cache miss")

        # Refresh and filter
        history = get_page_history_from_cutoff(page_title, cutoff_date=cutoff_date)
        self._lru[page_title] = history
        return self._filter_with_fallback(history, cutoff_date)

# -------------------------------------------------------------------------------
# Document link extraction from wikitext

def _wiki_content_url(titles):
    batch_titles = '|'.join([quote(f'{WIKI_NAMESPACE}:{t}') for t in titles])
    return (f'{ARCHIVE_BASE}/w/api.php?'
            'action=query&format=json&prop=revisions&'
            'rvprop=content&rvslots=main&'
            f'titles={batch_titles}'
           )

def _extract_file_links(wikitext):
    wikicode = mwparserfromhell.parse(wikitext)
    file_links = []

    # 1. Top-level links like [[File:...]]
    for link in wikicode.filter_wikilinks():
        title = str(link.title)
        if title.lower().startswith("file:"):
            file_links.append(title)

    # 2. Template parameters containing [[File:...]] links
    for template in wikicode.filter_templates():
        for param in template.params:
            param_value = mwparserfromhell.parse(str(param.value))
            for link in param_value.filter_wikilinks():
                title = str(link.title)
                if title.lower().startswith("file:"):
                    file_links.append(title)

    return file_links

def _normalize_mediawiki_title(title):
    title = title.replace(' ', '_')         # Normalize space to underscore
    return title

def _file_link_to_url(link):
    if link.lower().startswith("file:"):
        filename = _normalize_mediawiki_title(link[5:])
        return f"/wiki/File:{filename}"
    else:
        return None

def _deduplicate_links(links):
    return list(dict.fromkeys(links))
    
def _chunked(iterable, size):
    """Yield successive chunks from iterable."""
    it = iter(iterable)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            break
        yield chunk

def batch_fetch_document_links(titles, map_to_url=True, chunk_size=20):
    if not isinstance(titles, (list, tuple)):
        titles = [titles]

    result = {}

    for chunk in _chunked(titles, chunk_size):
        data = fetch_url(_wiki_content_url(chunk), json=True)
        for page in data['query']['pages'].values():
            title = page['title'].split(':', 1)[-1]  # strip 'Архів:' prefix
            try:
                wikitext = page['revisions'][0]['slots']['main']['*']
                links = _extract_file_links(wikitext)
                if map_to_url:
                    links = [_file_link_to_url(link) for link in links]
                result[title] = _deduplicate_links(links)
            except (KeyError, IndexError):
                result[title] = []

    return result
