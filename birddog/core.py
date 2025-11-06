# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Ukraine records archive monitor and scraper.
"""

from urllib.parse import unquote
import regex
from copy import copy, deepcopy

from birddog.utility import (
    get_text,
    match_text,
    form_text_item,
    is_linked
    )
from birddog.cache import load_cached_object, save_cached_object, CacheMissError
from birddog.wiki import (
    ARCHIVE_BASE,
    SUBARCHIVES,
    ARCHIVE_BY_ADDRESS,
    canonicalize_title,
    page_name,
    page_address,
    is_archive,
    get_title,
    classify_page,
    parent_title,
    page_exists,
    HistoryLRU,
    mw_read_page,
    do_search,
    batch_fetch_document_links,
    )
from birddog.ai import classify_table_columns
from birddog.translate import (
    get_translation_items,
    apply_translation,
    )
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

def _entry_hit(entry, entry_id):
    if match_text(entry['text'], entry_id):
        return True
    if entry.get('link'):
        return unquote(entry['link'].split('/')[-1]) == entry_id
    return False

_history_lru = HistoryLRU()

class Page:
    """Abstract base clase for all page types on the archive."""
    def __init__(self, title, runtime=None):
        self._runtime = runtime
        self._title = canonicalize_title(title)
        self._page = {}
        self._detached = False
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
                self._cache_save()

    class LookupError(Exception):
        def __init__(self, name, key):
            self.name = name
            self.key = key
            message = f"Lookup failed for key '{key}' in page '{name}'"
            super().__init__(message)

    def detached_copy(self):
        result = copy(self)
        result._page = deepcopy(self._page)
        result._detached = True
        return result

    @property
    def _cache_path(self):
        return f'page_cache/{self.title}'

    def _cache_load(self, version=None):
        """Try to retrieve page contents from cache. Returns True if successful."""
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
        if self._detached:
            # don't save detached pages to cache
            return
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
        if not self._cache_load():
            raise ValueError(f"Unable to load latest version of {self.title}")
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
    def title(self):
        return self._title

    @property
    def address(self):
        return page_address(self._title)

    @property
    def page(self):
        """Page data"""
        return self._page

    @property
    def tables(self):
        return self._page.get("tables", [])

    @property
    def children(self):
        """List of child page data"""
        return [child for table in self.tables for child in table["children"]]

    @property
    def child_ids(self):
        return [child[0]['text']['uk'] for child in self.children]

    @property
    def parent(self):
        parent = parent_title(self._title)
        if not parent:
            return None
        if self._runtime:
            return self._runtime.lookup_by_title(parent)
        return Page(parent, runtime=self._runtime)

    @property
    def description(self):
        return regex.sub(r"^\p{N}+\p{P}?\p{Zs}*", "", get_text(self._page.get('description')))

    #@property
    #def header(self):
    #    return self._page['header']

    @property
    def default_url(self):
        # FIXME: is this needed?!
        return f"{ARCHIVE_BASE}/wiki/{self.title}"

    @property
    def url(self):
        return self.default_url

    @property
    def unquoted_url(self):
        return unquote(self.url)

    @property
    def id(self):
        if is_archive(self.title):
            return self.archive_name
        return self.title.rsplit("/", 1)[-1]

    @property
    def name(self):
        return page_name(self._title)

    @property
    def display_name(self):
        return page_name(self._title).replace("/", " ")

    @property
    def archive_name(self):
        address = page_address(self._title)
        if address[1] in "D_":
            return address[0]
        return f"{address[0]}-{address[1]}"

    @property
    def subarchive(self):
        return page_address(self._title)[1]

    @property
    def lastmod(self):
        return self._page.get('lastmod', '')

    @property
    def refmod(self):
        return self._page.get('refmod', '')

    @property
    def doc_url(self):
        return self._page.get('doc_link')

    @property
    def dates(self):
        #_logger(f"dates = '{self._page.get('dates', '')}'")
        return get_text(self._page.get('dates', ''))

    @property
    def kind(self):
        result = classify_page(self.title)
        if result == "case" and self.children:
            return "opus"
        return result

    @property
    def is_latest(self):
        return self.history(limit=1)[0]['modified'] == self.lastmod

    @property
    def report(self):
        # make sure no commas in the name
        return f'{self.kind},{self.name.replace(",", "")},{self.lastmod}'

    def _find_child_row(self, entry_id):
        return next((x for x in self.children if _entry_hit(x[0], entry_id)), None)

    def lookup_table(self, table_name):
        for table in self.tables:
            if table["name"] == table_name:
                return table
        raise ValueError(f"Named table not found: {table_name}")

    def add_table(self, table_name, header=None, only_if_needed=False):
        if table_name in [table["name"] for table in self.tables]:
            if only_if_needed:
                return self.lookup_table(table_name)
            raise ValueError("Duplicate table name")
        if not self.tables:
            self._page["tables"] = []
        if not header:
            header = [form_text_item("")]
        table = {"name": table_name, "header": header, "children": []}
        self.tables.append(table)
        return table
            
    def adopt(self, child_id, child_title):
        if child_id not in self.child_ids:
            _logger.info(f"{self.name} adopting {child_id} with title {child_title}")
            adoptees = self.add_table("Adoptees", only_if_needed=True)
            row = [{ "text": form_text_item(child_id), "link": f"/wiki/{child_title}", "exists": True }]
            adoptees["children"].append(row)

    def lookup(self, entry_id):
        #_logger.info(f"Page.lookup title={self.title}, entry_id={entry_id}")
        row = self._find_child_row(entry_id)
        if row:
            url = row[0].get("link", "")
            child_title = get_title(url)
            if child_title:
                #_logger.info(f"spawning child of {self.title}: {child_title}")
                if self._runtime:
                    return self._runtime.lookup_by_title(child_title)
                return Page(child_title, runtime=self._runtime)

        # entry_id does not match known children - could be shadow child page
        # try to spawn it using the constructed title
        child_title = f"{self.title}/{entry_id}"
        if page_exists(child_title):
            #_logger.info(f"spawning unlinked child of {self.title}: {child_title}")
            if self._runtime:
                return self._runtime.lookup_by_title(child_title)
            return Page(child_title, runtime=self._runtime)

        # last ditch: search children lists
        """
        for child_id in self.child_ids:
            child = self.lookup(child_id)
            if child:
                row = child._find_child_row(entry_id)
                if row:
                    url = row[0].get("link", "")
                    child_title = get_title(url)
                    if child_title:
                        _logger.info(f"spawning grandchild of {self.title}: {child_title}")
                        if self._runtime:
                            return self._runtime.lookup_by_title(child_title)
                        return Page(child_title, runtime=self._runtime)
        """

        # can't find it - go ahead and adopt
        self.adopt(entry_id, child_title)
        if self._runtime:
            return self._runtime.lookup_by_title(child_title)
        return Page(child_title, runtime=self._runtime)

    def __getitem__(self, key):
        return self.lookup(key)

    @property
    def needs_translation(self):
        return len(get_translation_items(self._page)) > 0

    def apply_translation(self, translation_map):
        apply_translation(self._page, translation_map)
        self._cache_save()

    def load_child_document_links(self, update_cache=False):
        if self.kind == 'opus':
            items = []
            titles = []
            for i, child in enumerate(self.children):
                #_logger.info(f"load_child_document_links: {self.name}: {child}")
                if is_linked(child[0]) and len(child) > 1 and not is_linked(child[1]):
                    items.append(i)
                    titles.append(f"{self.title}/{child[0]['text']['uk']}")
            if items:
                need_save = False
                _logger.info(f'load_child_document_links: fetching {len(titles)} links')
                doc_links = batch_fetch_document_links(titles)
                for i, title in zip(items, titles):
                    links = doc_links.get(title)
                    if links:
                        # FIXME: what about multiple links? Ignoring them for now.
                        self.children[i][1]['link'] = links[0]
                        self.children[i][1]['exists'] = True
                        need_save = True
                if update_cache and need_save:
                    _logger.info(f'load_child_document_links({self.name}) updating cache')
                    self._cache_save()

    def prepare_to_download(self):
        _logger.info(f'prepare_to_download: {self.name} ({self.lastmod})')
        # trigger on-demand processing needed for download that may entail cache update
        self.load_child_document_links()

    def latest_changes(self, limit=100, offset=0):
        return do_search(self.title.split('/')[0], limit=limit, offset=offset)

class Archive(Page):
    """Represents a top level archive page."""
    def __init__(self, archive_tag, subarchive=None, runtime=None):
        super().__init__(ARCHIVE_BY_ADDRESS[(archive_tag, subarchive)], runtime=runtime)

