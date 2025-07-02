# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

import time
import regex
import threading
import queue
from urllib.parse import quote, unquote
from cachetools import LRUCache

from birddog.utility import (
    get_text,
    match_text,
    translate_page,
    is_linked
    )
from birddog.cache import load_cached_object, save_cached_object, CacheMissError
from birddog.wiki import (
    ARCHIVE_BASE,
    SUBARCHIVES,
    WIKI_NAMESPACE,
    ARCHIVES,
    find_archive,
    get_all_pages,
    HistoryLRU,
    PageTracker,
    mw_read_page,
    do_search,
    batch_fetch_document_links,
    check_page_updates
    )
from birddog.ai import classify_table_columns

from birddog.logging import get_logger
_logger = get_logger()

#
# global constants

def decode_subarchive(subarchive):
    if not subarchive:
        return SUBARCHIVES[0]
    for item in SUBARCHIVES:
        if subarchive in item.values():
            return item
    return None

# -------------------------------------------------------------------------------
# class definitions for each of the page types in the archive
# Page is the abstract base class that implements most of the logic
#     subclasses of Page are:
#        Archive
#        Fond
#        Opus
#        Case
# The archive is organized hierarchically as Archive->Fond->Opus->Case

def _entry_hit(entry, entry_id):
    if match_text(entry['text'], entry_id):
        return True
    if entry.get('link'):
        return unquote(entry['link'].split('/')[-1]) == entry_id
    return False

_history_lru = HistoryLRU()

class Page:
    """Abstract base clase for all page types on the archive."""
    def __init__(self, spec, parent):
        self._parent = parent
        self._spec = spec
        self._page = {}
        self._column_header_map = None
        if not self._cache_load():
            # not in the cache - get it
            if self.default_url is not None:
                #self._page = read_page(self.default_url)
                _logger.info(f'Loading page: {self.name} using title "{self.title}"')
                self._page = mw_read_page(self.title)

                # ensure lastmod == history[0]
                history = self.history(limit=1)
                if history:
                    self._page["lastmod"] = history[0]["modified"]
                # proactively get document links
                self.load_child_document_links(update_cache=False)
                self._cache_save()

    class LookupError(Exception):
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
        #if not is_linked(self.default_url):  # when does this happen?
        #    return False
        if not version:
            # determine latest version
            history = self.history(limit=1)
            if not history:
                _logger.info(f"{self.name}: no history")
                return False # bad page?
            version = history[0]["modified"]
        path = f'{self._cache_path}/{version}.json'
        try:
            #_logger.info(f"Fetching from cache: {self.name}[{version}]: {path}")
            self._page = load_cached_object(path)
            #_logger.info(f"Retrieved from cache: {self.name}[{version}]: {path}")
            return True
        except CacheMissError:
            pass
        return False

    def _cache_save(self):
        """Store the page contents in the cache, later retrievable under modification date.
        """
        if self.refmod:
            raise ValueError(f"Cannot save page when in comparison state: {self.name}") 
        if self.lastmod:
            path = f'{self._cache_path}/{self.lastmod}.json'
            #_logger.info(f"Saving page to cache: {self.name}[{self.lastmod}]")
            save_cached_object(self._page, path)

    def history(self, limit=None, cutoff_date=None):
        # needs to work if self._page is None
        if limit:
            return _history_lru.lookup(self.title, limit)
        if cutoff_date:
            return _history_lru.lookup_by_cutoff(self.title, cutoff_date=cutoff_date)
        raise ValueError(f'Page({self.title}).history must specify either limit or cutoff_date')

    def latest(self):
        """Set page state to the latest version."""
        self._cache_load()
        return self

    def revert_to(self, date):
        """Revert page state to particular version date."""
        history = self.history(cutoff_date=date)
        if not history:
            _logger.info(f'No version exists on or before {date}')
            return None
        version = history[-1]
        if self._cache_load(version=version['modified']):
            return self

        match = regex.search(r"[?&]oldid=(\d+)", version["link"])
        if match:
            oldid = match.group(1)
            _logger.info(f'Loading page: {self.name}, modified: {version["modified"]}')
            self._page = mw_read_page(self.title, oldid)
            self._cache_save()
            return self
        raise ValueError("URL does not contain oldid: unknown version")

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
    def header(self):
        return self._page['header']

    @property
    def base(self):
        return self._parent.base

    @property
    def default_url(self):
        if self._spec is not None and self._spec[1] is not None:
            url = self._spec[1]
            if url.startswith('http://') or url.startswith('https://'):
                return url
            return self.base + url
        return None

    @property
    def url(self):
        if self._page is not None and 'link' in self._page:
            return self._page.get('link')
        return self.default_url

    @property
    def unquoted_url(self):
        return unquote(self.url)

    @property
    def id(self):
        return self._spec[0]

    @property
    def name(self):
        return f'{self.parent.name}/{self.id}'

    @property
    def title(self):
        # must work even if self._page is None so history can be accessed before loading
        return unquote(self._spec[1].split(':')[1])

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
        return self._page.get('doc_link')

    @property
    def dates(self):
        #_logger(f"dates = '{self._page.get('dates', '')}'")
        return get_text(self._page.get('dates', ''))

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

    @property
    def child_ids(self):
        return [item[0]['text']['uk'] for item in self.children]

    def _find_child_row(self, entry_id):
        return next((x for x in self.children if _entry_hit(x[0], entry_id)), None)

    def lookup(self, entry_id):
        row = self._find_child_row(entry_id)
        if row:
            url = row[0]['link']
            split_url = url.rsplit('/', 1)
            child_id = get_text(row[0]['text'])
            if self.parent and split_url[0] == self.url.replace(self.base, '').rsplit('/', 1)[0]:
                # child url is at peer level: spawn sibling
                return self.parent.child_class((child_id, url), self.parent)
            # normal case: entry_id is listed in the parent page
            return self.child_class((child_id, url), self)
        # entry_id does not match known children - could be shadow child page
        # try to spawn it using the constructed url
        child_url = f'{self.url}/{entry_id}'
        child_url = child_url.replace(ARCHIVE_BASE, '')
        child_spec = (entry_id, child_url)
        try:
            result = self.child_class(child_spec, self)
            if result._page:
                return result
        except:
            pass
        # last ditch: search children lists
        for child_id in self.child_ids:
            child = self.lookup(child_id)
            row = child._find_child_row(entry_id)
            if row:
                return self.child_class((get_text(row[0]['text']), row[0]['link']), self)
        # unable to find matching id
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

    def load_child_document_links(self, update_cache=True):
        # do nothing unless (overridden in subclass)
        pass

    @property
    def column_header_map(self):
        result = self._column_header_map
        if not result:
            #_logger.info(f'column_header_map({self.name} {self.lastmod}): checking cache')
            # check if it is in the cached page data
            result = self._page.get("column_header_map")
            if not result:
                #_logger.info(f'column_header_map({self.name}): cache miss: fresh inference')
                # not in cache - try to infer the map
                classification = classify_table_columns(self)
                # form mapping from column header type to column index
                result = {}
                for i, col_type in enumerate(classification["mapping"]):
                    result[col_type] = i
                if classification["success"]:
                    # classification worked - retain it in the cache
                    #_logger.info(f'column_header_map({self.name}): updating cache')
                    self._page["column_header_map"] = result
                    self._cache_save()
            else:
                pass
                #_logger.info(f'column_header_map({self.name}): retrieved from cache')
            # keep non-persistent map for next time (regardless of success)
            self._column_header_map = result
        return result

    def prepare_to_download(self):
        _logger.info(f'prepare_to_download: {self.name} ({self.lastmod})')
        # trigger on-demand processing needed for download that may entail cache update
        self.load_child_document_links()
        self.column_header_map    

class Archive(Page):
    """Represents a top level archive page."""
    def __init__(self, tag, subarchive=None, base=ARCHIVE_BASE):
        self._tag = tag
        archive_data = find_archive(tag, subarchive)
        self._subarchive = archive_data["subarchive"]
        self._archive_name = archive_data["title"]["uk"]
        self._base = base
        super().__init__(None, None)

    @property
    def title(self):
        # must work even if self._page is None so history can be accessed before loading
        return self._archive_name.split(':')[1]

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
    def clean_name(self):
        # cleaned version for excel export standard
        sub = self.subarchive["en"]
        if sub in ["_", "D"]:
            return self.tag
        return f'{self.tag}-{sub}'

    @property
    def subarchive(self):
        return self._subarchive

    @property
    def base(self):
        return self._base

    @property
    def default_url(self):
        return self._base + '/wiki/' + str(quote(self._archive_name))

    def latest_changes(self, limit=100, offset=0):
        return do_search(self._archive_name.split('/')[0], limit=limit, offset=offset)

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

    def load_child_document_links(self, update_cache=True):
        items = []
        titles = []
        for i, child in enumerate(self.children):
            #_logger.info(f"load_child_document_links: {self.name}: {child}")
            if is_linked(child[0]) and not is_linked(child[1]):
                items.append(i)
                titles.append(f"{self.title}/{get_text(child[0]['text'])}")
        if items:
            need_save = False
            doc_links = batch_fetch_document_links(titles)
            for i, title in zip(items, titles):
                links = doc_links.get(title)
                if links:
                    # FIXME: what about multiple links? Ignoring them for now.
                    self.children[i][1]['link'] = links[0]
                    need_save = True
            if update_cache and need_save:
                #_logger.info(f'load_child_document_links({self.name}) updating cache')
                self._cache_save()

class Case(Page):
    """Represents case page."""
    @property
    def kind(self):
        return 'case'

# ----------------------------------------------------------------------------
# Page LRU memory cache

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

    def key(self, archive, subarchive, fond=None, opus=None, case=None):
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
            _logger.info(f"PageLRU: flushing all entries")
            self._lru.clear()
            self._timer_start = time.time()

        key = self.key(archive, subarchive, fond, opus, case)
        try:
            item = self._lru[key]
            #_logger.info(f"{f'PageLRU.lookup({key}): hit'}")
            return item
        except KeyError:
            #_logger.info(f"{f'PageLRU.lookup({key}): miss'}")
            try:
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
            except Page.LookupError:
                _logger.error(f'PageLRU: exception during page lookup')
                _logger.info(f'... failed to find child page: parent={parent.name}, key={key}')
                raise PageLRU.NotFoundError(key)

# ----------------------------------------------------------------------------
# Efficient mapping from page title to Birddog address

class TitleIndex:
    def __init__(self, page_lru=None, index=None, archives=None):
        self._lru = page_lru if page_lru else PageLRU()
        if not index or not archives:
            self._init_index()
        else:
            self._index = index
            self._archives = archives
        self._archive_root = { }
        for archive_root, addresses in self._archives.items():
            for address in addresses:
                self._archive_root[tuple(address)] = archive_root

    def to_dict(self):
        return {
            'index':        self._index,
            'archives':     self._archives,
        }

    @classmethod
    def from_dict(cls, d, page_lru=None):
        return cls(
            index=d['index'],
            archives=d['archives'],
            page_lru=page_lru
            )
    
    @staticmethod
    def _canonicalize_title(title):
        if not title.startswith(f"{WIKI_NAMESPACE}:"):
            title = f"{WIKI_NAMESPACE}:{title}"
        return title.replace(" ", "_")

    def _init_index(self):
        self._index = {}
        self._archives = {}
        for archive_key in ARCHIVES.keys():
            for key, entry in ARCHIVES[archive_key].items():
                subarchive_key = entry["subarchive"]["en"]
                archive_title = self._canonicalize_title(entry["title"]["uk"])
                address = self._lru.key(archive_key, subarchive_key)
                archive_root = archive_title.split("/")[0]
                #print(archive_title, archive_root, address)
                self._index[archive_title] = address
                if not archive_root in self._archives:
                    self._archives[archive_root] = [ address ]
                else:
                    self._archives[archive_root].append(address)
                
    def lookup_archive_root(self, archive_key, subarchive_key):
        address = self._lru.key(archive_key, subarchive_key)
        return self._archive_root[address]

    def lookup(self, page_title):
        page_title = self._canonicalize_title(page_title)
        if page_title in self._index:
            return self._index[page_title]
        page_split = page_title.split("/") + 4 * [None]
        archive_root = page_split[0]
        if not archive_root in self._archives:
            raise ValueError(f"Unrecognized title: {page_title}")
        for archive_address in self._archives[archive_root]:
            archive = self._lru.lookup(*archive_address)
            if archive:
                if self._canonicalize_title(archive.title) == page_title:
                    # the page title is the same as the archive title
                    self._index[page_title] = archive_address
                    return archive_address
                archive_split = archive.title.split("/")
                if len(archive_split) > 1 and archive_split[1] == page_split[1]:
                    # title is of the form archive/subarchive/fond/...
                    address = self._lru.key(
                        archive_address[0],
                        archive_address[1],
                        page_split[2],
                        page_split[3],
                        page_split[4])
                    self._index[page_title] = address
                    return address
                if page_split[1] in archive.child_ids:
                    # title is of the form archive/fond/...
                    # check for matching fond id
                    address = self._lru.key(
                        archive_address[0],
                        archive_address[1],
                        page_split[1],
                        page_split[2],
                        page_split[3])
                    self._index[page_title] = address
                    return address
        raise ValueError(f"Cannot find page: {page_title}")
        return None

# ----------------------------------------------------------------------------
# Page Update Manager

class HeartbeatManager:
    def __init__(self, interval=1.0):
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_heartbeat, daemon=True)
        self._started = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def start(self):
        if not self._started:
            self._thread.start()
            self._started = True

    def _run_heartbeat(self):
        while not self._stop_event.is_set():
            try:
                self.heartbeat()
            except Exception as e:
                _logger.info(f"Heartbeat error: {e}")
            time.sleep(self.interval)

    def heartbeat(self):
        """Override this method in subclasses to perform periodic actions."""
        _logger.info("Heartbeat...")

    def stop(self):
        self._stop_event.set()
        self._thread.join()

class PageUpdateManager(HeartbeatManager):
    _PAGE_UPDATE_MANAGER_PATH       = "page_update_manager.json"
    _HEARTBEAT_INTERVAL             = 30 # seconds
    _PAGE_UPDATE_CHECK_INTERVAL     = 60 * 15 # seconds
    _TITLE_BATCH_SIZE               = 500
    
    def __init__(self, page_lru=None):
        self._lru = page_lru if page_lru else PageLRU()
        self._request_queue = queue.Queue()
        self._last_check = 0
        self._busy = False
        super().__init__(interval=PageUpdateManager._HEARTBEAT_INTERVAL)
        self._load()
        
    def _load(self):
        try:
            data = load_cached_object(PageUpdateManager._PAGE_UPDATE_MANAGER_PATH)
            self._tracker = PageTracker.from_dict(data["tracker"])
            self._title_index = TitleIndex.from_dict(data["title_index"], page_lru=self._lru)
            self._pending_titles = data["pending_titles"]
            self._registered_archives = set(data["registered_archives"])
        except CacheMissError:
            self._tracker = PageTracker()
            self._title_index = TitleIndex(page_lru=self._lru)
            self._pending_titles = []
            self._registered_archives = set()

    def _save(self):
        tracker_data = self._tracker.to_dict()
        title_index_data = self._title_index.to_dict()
        save_cached_object({
            "tracker": self._tracker.to_dict(),
            "title_index": self._title_index.to_dict(),
            "pending_titles": self._pending_titles,
            "registered_archives": list(self._registered_archives),
        }, PageUpdateManager._PAGE_UPDATE_MANAGER_PATH)

    @property
    def busy(self):
        return not self._request_queue.empty() or self._busy
        
    def heartbeat(self):
        #_logger.info("PageUpdateManager heartbeat...")
        self._busy = True

        # 1. Update tracker periodically
        now = time.time()
        if now - self._last_check > PageUpdateManager._PAGE_UPDATE_CHECK_INTERVAL:
            _logger.info("PageUpdateManager: checking for page updates...")
            if self._tracker.update():
                self._save()
            _logger.info("PageUpdateManager: finished update check...")
            self._last_check = now
            self._busy = False
            return

        # 2. Process registration requests
        if not self._request_queue.empty():
            try:
                archive, subarchive = self._request_queue.get_nowait()
                key = f"{archive}-{subarchive}"
                if key not in self._registered_archives:
                    _logger.info(f"PageUpdateManager: registering {archive}-{subarchive}")
                    archive_root = self._title_index.lookup_archive_root(archive, subarchive)
                    # inventory all pages that start with archive root and add latest mod times to tracker
                    titles = get_all_pages(prefix=archive_root)
                    if titles:
                        self._pending_titles.extend(titles)
                        self._registered_archives.add(key)
                        self._save()
                    self._busy = False
                    return
            except queue.Empty:
                pass  # should not happen, but safe

        # 3. Insert batch of pending titles into tracker
        if self._pending_titles:
            batch = self._pending_titles[:PageUpdateManager._TITLE_BATCH_SIZE]
            del self._pending_titles[:PageUpdateManager._TITLE_BATCH_SIZE]
            _logger.info(f"PageUpdateManager: adding batch of {len(batch)} titles to tracker")
            self._tracker.add_titles(batch)
            self._save()
        self._busy = False

    def register_archive(self, archive, subarchive):
        # let the tracker know there's interest in a particular archive
        # insert (archive, subarchive) into request queue
        self._request_queue.put((archive, subarchive))

    def get_updates(self, archive, subarchive, cutoff_date=None):
        prefix = self._title_index.lookup_archive_root(archive, subarchive)
        _logger.info(f"PageUpdateManager.get_updates: prefix={prefix}")
        updates_pending = self.busy
        updates = self._tracker.get_updates(prefix, cutoff_date=cutoff_date)
        _logger.info(f"PageUpdateManager.get_updates: {len(updates)} total updates")
        result = {}
        for title, mod_date in updates.items():
            try:
                address = self._title_index.lookup(title)
                #_logger.info(f"{title}, {mod_date}, {address}")
                if address and address[0] == archive and address[1] == subarchive:
                    key = ArchiveWatcher.key(*address)
                    result[key] = mod_date
            except ValueError as error:
                _logger.error(f"PageUpdateManager.get_updates: cannot find title {title}. Skipping...")
        return result, updates_pending

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
        for item in address:
            if item not in pos:
                pos[item] = {}
            pos = pos[item]
        pos['unresolved'] = value
    return root

class ArchiveWatcher:
    def __init__(self, archive, subarchive, cutoff_date):
        self._archive = archive
        if not subarchive:
            subarchive = Archive(archive).subarchive["en"]
        self._subarchive = subarchive
        self._cutoff_date = cutoff_date
        self._last_checked_date = cutoff_date
        self._resolved = {}
        self._unresolved = {}

    def save(self):
        return {
            'version': 'v2',
            'archive': self._archive,
            'subarchive': self._subarchive,
            'cutoff_date': self._cutoff_date,
            'resolved': self._resolved,
            'unresolved': self._unresolved,
            'last_checked_date': self._last_checked_date
        }

    @staticmethod
    def load(data):
        watcher = ArchiveWatcher(data['archive'], data['subarchive'], data['cutoff_date'])
        version = data.get("version", "v1")  # default to legacy

        # normalize unresolved (assume format is fine)
        watcher._unresolved = data.get('unresolved', {})
        watcher._last_checked_date = data.get('last_checked_date', watcher._cutoff_date)

        # normalize resolved entries for legacy versions
        if version == "v1":
            cutoff_date = watcher._cutoff_date
            watcher._resolved = {
                k: (
                    [{"modified": v, "last_resolved": cutoff_date}]
                    if not isinstance(v, list) else v
                )
                for k, v in data.get('resolved', {}).items()
            }
        else:
            watcher._resolved = data.get('resolved', {})

        return watcher

    @staticmethod
    def key(archive, subarchive, fond=None, opus=None, case=None):
        return ','.join((archive, subarchive, fond or '', opus or '', case or ''))

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

    def _last_resolved_date(self, item):
        entries = self._resolved.get(item, [])
        return entries[-1]["last_resolved"] if entries else self._cutoff_date

    def check(self, page_manager):
        # register just in case it hasn't been already
        page_manager.register_archive(self._archive, self._subarchive)
        # retrieve updates from page manager
        updates, page_manager_busy = page_manager.get_updates(self._archive, self._subarchive, self._last_checked_date)
        if updates:
            for item, mod_date in updates.items():
                # Get most recent resolved mod date (if any)
                latest_resolved = self._resolved[item][-1]["modified"] if item in self._resolved else None
                if mod_date >= self._last_checked_date and (latest_resolved is None or mod_date > latest_resolved):
                    self._unresolved[item] = {
                        "modified": mod_date,
                        "last_resolved": self._last_resolved_date(item)
                    }
            #_logger.info(f'ArchiveWatcher.check() unresolved: {json.dumps(self._unresolved, indent=4)}')
            self._last_checked_date = max(max(updates.values()), self._last_checked_date)

    def check_legacy(self):
        #import json

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
                # Get most recent resolved mod date (if any)
                latest_resolved = self._resolved[item][-1]["modified"] if item in self._resolved else None
                if mod_date >= self._last_checked_date and (latest_resolved is None or mod_date > latest_resolved):
                    self._unresolved[item] = {
                        "modified": mod_date,
                        "last_resolved": self._last_resolved_date(item)
                    }
            #_logger.info(f'ArchiveWatcher.check() unresolved: {json.dumps(self._unresolved, indent=4)}')
            self._last_checked_date = max(max(updates.values()), self._last_checked_date)

    def resolve(self, item, deep=False):
        #_logger.info(f'ArchiveWatcher.resolve: before\n\tunresolved: {self._unresolved}\n\tresolved: {self._resolved}')
        if deep:
            _logger.info(f'ArchiveWatcher: deep resolve: {item}')
            item = item.rstrip(',')
            for key in list(self.unresolved.keys()):
                if key.startswith(item):
                    unresolved_item = self._unresolved.pop(key)
                    self._resolved.setdefault(key, []).append(unresolved_item)
        elif item in self._unresolved:
            unresolved_item = self._unresolved.pop(item)
            self._resolved.setdefault(item, []).append(unresolved_item)
        #_logger.info(f'ArchiveWatcher.resolve: after\n\tunresolved: {self._unresolved}\n\tresolved: {self._resolved}')

    def unresolve(self, item):
        if item in self._resolved and self._resolved[item]:
            self._unresolved[item] = self._resolved[item].pop()
            if not self._resolved[item]:  # Clean up empty lists
                del self._resolved[item]
