# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

import time
import urllib.parse
import requests
from cachetools import LRUCache
import regex
from bs4 import BeautifulSoup

from birddog.utility import (
    ARCHIVE_BASE,
    SUBARCHIVES,
    ARCHIVE_LIST,
    lastmod,
    format_date,
    get_text,
    match_text,
    form_text_item,
    translate_page,
    equal_text,
    get_logger
    )
from birddog.cache import load_cached_object, save_cached_object, CacheMissError

logger = get_logger()

#
# global constants

REQUEST_TIMEOUT = 10 # seconds

def decode_subarchive(subarchive):
    for item in SUBARCHIVES:
        if subarchive in item.values():
            return item
    return SUBARCHIVES[0]

# HTML page element processing
def form_element_text(element):
    """Given HTML element, return multilingual text item containing the inner text"""
    text = element.text.strip() if element is not None else None
    return form_text_item(text)

def read_page(url):
    """
    Extract archive information for given page using HTTP get.
    Return struct with page:
        title,
        description,
        table header,
        table contents,
        lastmod date,
        doc_link [only for case pages],
        thumb_link [only for case pages],
    """
    soup = BeautifulSoup(requests.get(url, timeout=REQUEST_TIMEOUT).text, 'lxml')
    title = soup.find('span', attrs = {'class': 'mw-page-title-main'})
    desc = soup.find('span', attrs = {'id': 'header_section_text'})
    table = soup.find('table', attrs = {'class': 'wikitable'})
    children = []
    header = []
    if table:
        for tr_elem in table.find_all('tr'):
            if not header:
                for th_elem in tr_elem.find_all('th'):
                    header.append(form_element_text(th_elem))
            item = []
            for td_elem in tr_elem.find_all('td'):
                a_elem = td_elem.find('a')
                child_url = a_elem.get('href') if a_elem else None
                text = form_text_item(td_elem.text.strip())
                item.append({'text': text, 'link': child_url})
            if item:
                children.append(item)

    # check for document thumbnail
    doc_info = soup.find('figure', attrs = {'typeof': 'mw:File/Thumb'})
    doc_url = None
    thumb_url = None
    if doc_info:
        a_tag = doc_info.find('a')
        doc_url = a_tag.get('href')
        thumb_elem = a_tag.find('img')
        thumb_url = thumb_elem.get('src') if thumb_elem else None
    footer = soup.find('li', attrs={'id': 'footer-info-lastmod'})
    last_modified = lastmod(footer.text) if footer else None

    return {
        'title': form_element_text(title),
        'description': form_element_text(desc),
        'header': header,
        'children': children,
        'lastmod': last_modified,
        'link': url,
        'doc_link': doc_url,
        'thumb_link': thumb_url,
    }

def do_search(query_string, limit=10, offset=0):
    """
    Search archive site for matching entries, sorted on last modification date.
    For each hit, return dict with item with keys: title, link, and lastmod.
    """
    query_string = urllib.parse.quote(query_string, safe='', encoding=None, errors=None)
    url = f'{ARCHIVE_BASE}/w/index.php?limit={limit}&offset={offset}'
    url += f'&ns0=1&sort=last_edit_desc&search={query_string}'
    soup = BeautifulSoup(requests.get(url, timeout=REQUEST_TIMEOUT).text, 'lxml')
    results = []
    for result in soup.find_all('li', attrs = {'class': 'mw-search-result'}):
        div = result.find('div', attrs = {'class': 'mw-search-result-heading'})
        data = result.find('div', attrs = {'class': 'mw-search-result-data'})
        data = data.text.strip()
        pos = data.find('-')
        data = format_date(data[(pos + 1):].strip())
        item = {
            'title': div.a['title'],
            'link': div.a['href'],
            'lastmod': data
        }
        results.append(item)
    return results

def history_url(page):
    return page.default_url.replace(
        '/wiki/',
        '/w/index.php?action=history&title=')

def get_page_history(page, limit=None, cutoff_date=None):
    """
    Return version history of given page, sorted in reverse chronological order.
    Returns list of dicts containing keys:
        "modified": modification date in standard format
        "link": url to the corresponding page version
    """
    if cutoff_date is not None:
        # search increasingly for cutoff date  because
        # api does not allow for paging through search results
        last_result_length = 0
        attempt = 10
        while True:
            result = get_page_history(page, limit=attempt)
            if len(result) == last_result_length:
                return result # no more history to be had
            if result[-1]['modified'] <= cutoff_date:
                for index, item in enumerate(result):
                    if item['modified'] <= cutoff_date:
                        return result[:(index+1)]
                return result
            # increase limit length and try again
            last_result_length = len(result)
            attempt *= 2

    url = history_url(page)
    if limit is not None:
        limit = max(limit, 1) # low limit value returns no result
        url = f'{url}&limit={limit}'
    soup = BeautifulSoup(requests.get(url, timeout=REQUEST_TIMEOUT).text, 'lxml')
    result = []
    for elem in soup.find_all('a', attrs = {'class': 'mw-changeslist-date'}):
        date = format_date(elem.text)
        link = elem['href']
        result.append({
            'modified': date,
            'link': page.base + link,
        })
    return result

def report_page_changes(page):
    """
    Print a report of changes detected in check_page_changes().
    """
    if isinstance(page, Page):
        page = page.page
    if 'refmod' not in page:
        logger.info(f"No changes to report. Run check_page_changes first.")
        return
    logger.info(
        f'Change report for {get_text(page["title"])},' +
        f' lastmod={page["lastmod"]}, refmod={page["refmod"]}')
    for key in ['title', 'description']:
        if page[key]['edit'] is not None:
            logger.info(f'{key}: {page[key]["edit"]}')
    for child in page['children']:
        index = get_text(child[0]['text'])
        for i, item in enumerate(child):
            if 'edit' in item and item['edit'] is not None:
                logger.info(f'{index}[{i}] ({item["edit"]}): {get_text(item["text"])}')

def check_page_changes(page, reference, report=False):
    """
    Compare a given page to a prior version of the same page and return any detected changes.
    """
    if isinstance(page, Page):
        page = page.page
    if isinstance(reference, Page):
        reference = reference.page
    page['refmod'] = reference['lastmod']
    for key in ['title', 'description']:
        changed = not equal_text(page[key], reference[key])
        page[key]['edit'] = 'changed' if changed else None
    ref_children = dict((get_text(c[0]['text']), c) for c in reference['children'])
    for child in page['children']:
        index = get_text(child[0]['text'])
        if index in ref_children:
            ref_child = ref_children[index]
            for item, ref_item in zip(child, ref_child):
                changed = not equal_text(item['text'], ref_item['text'])
                item['edit'] = 'changed' if changed else None
                if 'link' in item:
                    if 'link' in ref_item:
                        item['link_edit'] = 'changed' if item['link'] != ref_item['link'] else None
                    else:
                        item['link_edit'] = 'added'
        else:
            for item in child:
                item['edit'] = 'added'
    if report:
        report_page_changes(page)

def _page_update_summary(archive, change_list):
    assert isinstance(archive, Archive)
    archive_prefix = archive.url[:archive.url.rfind('/')]
    archive_prefix = archive_prefix.replace(ARCHIVE_BASE, '')
    archive_prefix = archive_prefix.replace('%3A', ':')
    # Form list of fonds belonging to this archive
    fond_list={get_text(c[0]['text']) for c in archive.children}
    result = {}
    for item in change_list:
        page_spec = item["title"].split('/')
        address = (archive.tag, archive.subarchive["en"])
        address += tuple(entry for entry in page_spec[1:])
        address = (address + ("",) * 3)[:5]
        fond = address[2]
        address = ','.join(address)
        mod_date = item["lastmod"]
        # Confirm that the item belongs to the selected archive
        if fond in fond_list and item["link"].startswith(archive_prefix):
            if address in result:
                result[address] = max(mod_date, result["address"])
            else:
                result[address] = mod_date
    return result

def check_page_updates(archive, cutoff_date):
    assert isinstance(archive, Archive)
    change_list = []
    batch_size = 50
    offset = 0
    while True:
        logger.info(f'check_page_updates: {archive.name}, {batch_size}, {offset}')
        changes = archive.latest_changes(limit=batch_size, offset=offset)
        change_list += changes
        if not changes or changes[-1]["lastmod"] < cutoff_date:
            break
        offset += batch_size
        batch_size *= 2 # search geometrically longer history 
    change_list = [item for item in change_list if item["lastmod"] >= cutoff_date]
    logger.info(f"check_page_updates, {len(change_list)}, changes found")
    return _page_update_summary(archive, change_list)

# -------------------------------------------------------------------------------
# class definitions for each of the page types in the archive
# Page is the abstract base class that implements most of the logic
#     subclasses of Page are:
#        Archive
#        Fond
#        Opus
#        Case
# The archive is organized hierarchically as Archive->Fond->Opus->Case

class Page:
    """Abstract base clase for all page types on the archive."""
    def __init__(self, spec, parent, use_cache=True):
        self._parent = parent
        self._spec = spec
        self._page = {}
        if use_cache:
            if not self._cache_load():
                # not in the cache - get it
                if self.default_url is not None:
                    logger.info(f"{f'Loading page: {self.name} from {self.default_url}'}")
                    self._page = read_page(self.default_url)
                    self._cache_save()

    class LookupError(LookupError):
        def __init__(self, page_name, key):
            self.page_name = page_name
            self.key = key
            message = f"Lookup failed for key '{key}' in page '{page_name}'"
            super().__init__(message)

    @property
    def _cache_path(self):
        return f'page_cache/{self.name}'

    def _cache_load(self, version=None):
        """Try to retrieve page contents from cache. Returns True if successful."""
        if not self.default_url:
            return False
        if not version:
            history = self.history(limit=1)
            if not history:
                logger.info(f"{f'{self.name}: no history'}")
                return False # bad page?
            version = history[0]["modified"]
        path = f'{self._cache_path}/{version}.json'
        try:
            self._page = load_cached_object(path)
            logger.info(f"{f'Retrieved from cache: {self.name}[{version}]: {path}'}")
            return True
        except CacheMissError:
            pass
        return False

    def _cache_save(self):
        """Store the page contents in the cache, later retrievable under modification date.
        """
        if self.lastmod:
            path = f'{self._cache_path}/{self.lastmod}.json'
            save_cached_object(self._page, path)

    def latest(self):
        """Set page state to the latest version."""
        self._cache_load()
        return self

    def revert_to(self, date):
        """Revert page state to particular version date."""
        history = self.history(cutoff_date=date)
        if not history:
            logger.info(f'No version exists on or before {date}')
            return None
        version = history[-1]
        if self._cache_load(version=version['modified']):
            return self

        logger.info(f'Loading page: {self.name}, modified: {version["modified"]}')
        self._page = read_page(version['link'])
        self._cache_save()
        return self

    @property
    def page(self):
        """Page data"""
        return self._page

    @property
    def children(self):
        """List of child page data"""
        return self._page.get('children')

    @property
    def child_ids(self):
        return [get_text(child[0]['text']) for child in self.children]

    @property
    def parent(self):
        return self._parent

    @property
    def description(self):
        return regex.sub(r"^\p{N}+\p{P}?\p{Zs}*", "", get_text(self._page.get('description')))

    @property
    def base(self):
        return self._parent.base

    @property
    def default_url(self):
        if self._spec is not None and self._spec[1] is not None:
            return self.base + self._spec[1]
        return None

    @property
    def url(self):
        if self._page is not None and 'link' in self._page:
            return self._page.get('link')
        return self.default_url

    @property
    def id(self):
        return self._spec[0]

    @property
    def name(self):
        return f'{self.parent.name}/{self.id}'

    @property
    def title(self):
        return get_text(self._page.get('title'))

    @property
    def lastmod(self):
        return self._page.get('lastmod', '')

    @property
    def refmod(self):
        return self._page.get('refmod', '')

    @property
    def child_class(self):
        return None

    @property
    def doc_url(self):
        url = self._page.get('doc_link')
        return self.base + url if url else None

    @property
    def thumb_url(self):
        url = self._page.get('thumb_link')
        return self.base + url if url else None

    @property
    def kind(self):
        return 'table'

    @property
    def is_latest(self):
        return self.history(limit=1)[0]['modified'] == self.lastmod

    @property
    def report(self):
        # make sure no commas in the name
        return f'{self.kind},{self.name.replace(",", "")},{self.lastmod}'

    def get_child_row(self, entry_id):
        return next((x for x in self.children if match_text(x[0]['text'], entry_id)), None)

    def lookup(self, entry_id, use_cache=True):
        row = self.get_child_row(entry_id)
        if row:
            return self.child_class((get_text(row[0]['text']), row[0]['link']), self, use_cache=use_cache)
        raise LookupError(self.name, entry_id)

    def __getitem__(self, key):
        return self.lookup(key)

    @property
    def needs_translation(self):
        return translate_page(self._page, dry_run=True) > 0

    def translate(self, asynchronous=False, progress_callback=None, completion_callback=None):
        if asynchronous:
            if not self.needs_translation:
                return False # nothing to translate
            def completion_cb(task_id, results):
                # set up completion callback to update the cache
                if results:
                    self._cache_save()
                # chain to caller
                if completion_callback:
                    completion_callback(task_id, results)
            return translate_page(self._page, asynchronous=True, progress_callback=progress_callback, completion_callback=completion_cb)
        if translate_page(self._page) > 0:
            self._cache_save()
            return True
        return False

    def history(self, limit=None, cutoff_date=None):
        return get_page_history(self, limit, cutoff_date)

class Archive(Page):
    """Represents a top level archive page."""
    def __init__(self, tag, subarchive=None, base=ARCHIVE_BASE, use_cache=True):
        self._tag = tag
        self._subarchive = decode_subarchive(subarchive)
        archive_name = ARCHIVE_LIST[tag] if tag in ARCHIVE_LIST else None
        self._archive_name = f'{archive_name}/{self._subarchive["uk"]}'
        self._base = base
        super().__init__(None, None, use_cache=use_cache)

    @property
    def kind(self):
        return 'archive'

    @property
    def child_class(self):
        return Fond

    @property
    def tag(self):
        return self._tag

    @property
    def id(self):
        return self._tag

    @property
    def name(self):
        return f'{self.tag}-{self.subarchive["en"]}'

    @property
    def subarchive(self):
        return self._subarchive

    @property
    def base(self):
        return self._base

    @property
    def default_url(self):
        return self._base + '/wiki/' + str(urllib.parse.quote(self._archive_name))

    def latest_changes(self, limit=100, offset=0):
        return do_search(ARCHIVE_LIST[self._tag], limit=limit, offset=offset)

class Fond(Page):
    """Represents fond page."""
    @property
    def kind(self):
        return 'fond'

    @property
    def child_class(self):
        return Opus

class Opus(Page):
    """Represents fond page."""
    @property
    def kind(self):
        return 'opus'

    @property
    def child_class(self):
        return Case

    @property
    def shortname(self):
        return f'{self.parent.parent.id} {self.parent.id}-{self.id}'

class Case(Page):
    """Represents case page."""
    @property
    def kind(self):
        return 'case'

class PageLRU:
    class NotFoundError(Exception):
        def __init__(self, address):
            self._address = address
            super().__init__(f"Page not found: {address}")

        @property
        def address(self):
            return self._address

    def __init__(self, maxsize=500, reset_limit=60 * 60):
        self._reset_limit = reset_limit # seconds
        self._timer_start = time.time()
        self._lru = LRUCache(maxsize=maxsize)

    def _key(self, archive, subarchive, fond=None, opus=None, case=None):
        return (archive or '', subarchive or '', fond or '', opus or '', case or '')

    def _page_key(self, page):
        a, rest = page.name.split('-', 1)
        parts = rest.split('/')
        return (a, *parts)

    def lookup_child(self, page, child_id):
        return self.lookup(*(*self._page_key(page), child_id))

    def lookup(self, archive, subarchive, fond=None, opus=None, case=None):
        # periodically flush the lru to ensure the pages don't become stale
        if time.time() - self._timer_start >= self._reset_limit:
            logger.info(f"PageLRU: flushing all entries")
            self._lru.clear()
            self._timer_start = time.time()

        key = self._key(archive, subarchive, fond, opus, case)
        try:
            item = self._lru[key]
            logger.info(f"{f'PageLRU.lookup({key}): hit'}")
            return item
        except KeyError:
            logger.info(f"{f'PageLRU.lookup({key}): miss'}")
            if not fond:
                item = Archive(archive, subarchive=subarchive)
            elif not opus:
                parent = self.lookup(archive, subarchive)
                item = parent.lookup(fond)
            elif not case:
                parent = self.lookup(archive, subarchive, fond)
                item = parent.lookup(opus)
            else:
                parent = self.lookup(archive, subarchive, fond, opus)
                item = parent.lookup(case)
            if not item:
                raise PageLRU.NotFoundError(key)
            self._lru[key] = item
            return item

# ----------------------------------------------------------------------------
# Update watcher

# Unicode-aware parsing
_DASH_CHARS = r"\-\u2010\u2011\u2012\u2013\u2014"
_ALPHA_DASH = fr"[\p{{L}}{_DASH_CHARS}]*"
_pattern = regex.compile(fr"^({_ALPHA_DASH})(\d+)({_ALPHA_DASH})$")

def _parse_string(s):
    match = _pattern.fullmatch(s)
    if match:
        prefix, number, suffix = match.groups()
        return (int(number), prefix, suffix)
    else:
        return (float('inf'), s, '')

def _sort_keys(keys):
    return sorted(keys, key=_parse_string)

def _flatten_hierarchy(d, prefix=None):
    result = []
    prefix = prefix or []

    children = [k for k in d if k != 'unresolved']
    sorted_children = _sort_keys(children)

    for key in sorted_children:
        current_path = prefix + [key]
        full_path_str = '/'.join(current_path)

        value = d[key]
        unresolved = value.get('unresolved') if isinstance(value, dict) else None

        result.append((full_path_str, unresolved))

        if isinstance(value, dict):
            result.extend(_flatten_hierarchy(value, current_path))

    return result

def _make_tree(unresolved):
    root = {}
    for key, value in unresolved.items():
        address = key.rstrip(',')
        address = address.replace(",", "-", 1)
        address = address.split(',')
        pos = root
        for i, item in enumerate(address):
            if item not in pos:
                pos[item] = {}
            pos = pos[item]
        pos['unresolved'] = value
    return root

class ArchiveWatcher:
    def __init__(self, archive, subarchive, cutoff_date, lru=None):
        self._lru = lru if lru else PageLRU()
        self._archive = self._lru.lookup(archive, subarchive)
        self._cutoff_date = cutoff_date
        self._last_checked_date = cutoff_date
        self._resolved = {}
        self._unresolved = {}

    def save(self):
        return {
            'archive': self._archive.tag,
            'subarchive': self._archive.subarchive["en"],
            'cutoff_date': self._cutoff_date,
            'resolved': self._resolved,
            'unresolved': self._unresolved,
            'last_checked_date': self._last_checked_date
        }

    @staticmethod
    def load(data, lru=None):
        if not lru:
            lru = PageLRU()
        watcher = ArchiveWatcher(data['archive'], data['subarchive'], data['cutoff_date'], lru=lru)
        watcher._resolved = data['resolved']
        watcher._unresolved = data['unresolved']
        watcher._last_checked_date = data.get('last_checked_date', watcher._cutoff_date)
        return watcher

    @staticmethod
    def key(archive, subarchive, fond=None, opus=None, case=None):
        return ','.join((archive, subarchive, fond or '', opus or '', case or ''))

    def _last_resolved_date(self, item):
        return self._resolved.get(item, self._cutoff_date)

    @property
    def resolved(self):
        return self._resolved

    @property
    def unresolved(self):
        return self._unresolved

    @property
    def cutoff_date(self):
        return self._cutoff_date

    @property
    def unresolved_tree(self):
        return _flatten_hierarchy(_make_tree(self.unresolved))
    
    def check(self):
        def _check_ancestors(changes):
            def _add_result(kwargs):
                page = self._lru.lookup(**kwargs)
                key = ArchiveWatcher.key(**kwargs)
                if key not in result:
                    result[key] = page.history(limit=1)[0]['modified']

            def _merge_result(result, changes):
                for key, value in changes.items():
                    if not key in result or value > result[key]:
                        result[key] = value
                return result

            result = {}
            quick = True
            if quick:
                key = ArchiveWatcher.key(self._archive.tag, self._archive.subarchive['en'])
                result = {key: self._archive.history(limit=1)[0]['modified']}
            else:
                # could take a while to search all ancestors...
                for item in changes:
                    address = item.split(',')
                    kwargs = {
                        "archive": address[0],
                        "subarchive": address[1],
                    }
                    _add_result(kwargs)
                    kwargs["fond"] = address[2]
                    _add_result(kwargs)
                    kwargs["opus"] = address[3]
                    _add_result(kwargs)
                    kwargs["case"] = address[4]
                    _add_result(kwargs)
            return _merge_result(result, changes)

        updates = check_page_updates(self._archive, self._last_checked_date)
        if updates:
            updates = _check_ancestors(updates)
            for item, mod_date in updates.items():
                if mod_date >= self._last_checked_date:
                    if item not in self._resolved or mod_date > self._resolved[item]:
                        self._unresolved[item] = {
                            "modified": mod_date,
                            "last_resolved": self._last_resolved_date(item)
                        }
            self._last_checked_date = max(max(updates.values()), self._last_checked_date)

    def resolve(self, item, deep=False):
        if deep:
            # resolve item and all its subsidiaries
            logger.info(f'ArchiveWatcher: deep resolve: {item}')
            item = item.rstrip(',')
            # iterate over a copy of the keys to avoid problem mutating dict inside the loop
            for key in list(self.unresolved.keys()):
                if key.startswith(item):
                    self._resolved[key] = self._unresolved.pop(key)["modified"]
        if item in self._unresolved:
            self._resolved[item] = self._unresolved.pop(item)["modified"]
