"""
Ukraine records archive monitor and scraper.
"""

import re
import urllib.parse
import requests
from bs4 import BeautifulSoup
from birddog.utility import (
    ARCHIVE_BASE,
    SUBARCHIVES,
    ARCHIVE_LIST,
    lastmod,
    format_date,
    get_text,
    form_text_item,
    translate_page,
    equal_text,
    )
from birddog.cache import load_cached_object, save_cached_object, CacheMissError

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
        lastmod date
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
                child_url = a_elem['href'] if a_elem else None
                text = form_text_item(td_elem.text.strip())
                item.append({'text': text, 'link': child_url})
            if item:
                children.append(item)
    footer = soup.find('li', attrs={'id': 'footer-info-lastmod'})
    last_modified = lastmod(footer.text) if footer else None

    return {
        'title': form_element_text(title),
        'description': form_element_text(desc),
        'header': header,
        'children': children,
        'lastmod': last_modified,
        'link': url
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
    #print(f'get_page_history(limit={limit}, cutoff_date={cutoff_date})')
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
    #print(url)
    soup = BeautifulSoup(requests.get(url, timeout=REQUEST_TIMEOUT).text, 'lxml')
    #print(soup)
    result = []
    for elem in soup.find_all('a', attrs = {'class': 'mw-changeslist-date'}):
        date = format_date(elem.text)
        link = elem['href']
        result.append({
            'modified': date,
            'link': page.base + link,
            #'title': elem['title']
        })
    #print(result)
    return result

def report_page_changes(page):
    """
    Print a report of changes detected in check_page_changes().
    """
    if isinstance(page, Table):
        page = page.page
    if 'refmod' not in page:
        print('No changes to report. Run check_page_changes first.')
        return
    print(
        f'Change report for {get_text(page["title"])},' +
        f' lastmod={page["lastmod"]}, refmod={page["refmod"]}')
    for key in ['title', 'description']:
        if page[key]['edit'] is not None:
            print(f'{key}: {page[key]["edit"]}')
    for child in page['children']:
        #print(child)
        index = get_text(child[0]['text'])
        for i, item in enumerate(child):
            if 'edit' in item and item['edit'] is not None:
                print(f'{index}[{i}] ({item["edit"]}): {get_text(item["text"])}')

def check_page_changes(page, reference, report=False):
    """
    Compare a given page to a prior version of the same page and return any detected changes.
    """
    if isinstance(page, Table):
        page = page.page
    if isinstance(reference, Table):
        reference = reference.page
    page['refmod'] = reference['lastmod']
    for key in ['title', 'description']:
        changed = not equal_text(page[key], reference[key])
        page[key]['edit'] = 'changed' if changed else None
    ref_children = dict((get_text(c[0]['text']), c) for c in reference['children'])
    for child in page['children']:
        #print(child)
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

# -------------------------------------------------------------------------------
# class definitions for each of the page types in the archive
# Table is the abstract base class that implements most of the logic
#     subclasses of Table are:
#        Archive
#        Fond
#        Opus
#        Case
# The archive is organized hierarchically as Archive->Fond->Opus->Case

class Table:
    """Abstract base clase for all page types on the archive."""
    def __init__(self, spec, parent, use_cache=True):
        self._parent = parent
        self._spec = spec
        self._page = None
        if use_cache:
            if not self._cache_load():
                # not in the cache - get it
                if self.default_url is not None:
                    print(f'Loading page: {self.name} from {self.default_url}')
                    self._page = read_page(self.default_url)
                    self._cache_save()

    @property
    def _cache_path(self):
        return f'page_cache/{self.name}'

    def _cache_load(self, version=None):
        """Try to retrieve page contents from cache. Returns True if successful."""
        if not version:
            history = self.history(limit=1)
            version = history[0]["modified"]
        path = f'{self._cache_path}/{version}.json'
        try:
            self._page = load_cached_object(path)
            print(f'Retrieved from cache: {self.name}[{version}]: {path}')
            return True
        except CacheMissError:
            pass
        return False

    def _cache_save(self):
        """Store the page contents in the cache, later retrievable under modification date.
        """
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
            print('No version exists on or before', date)
            return None
        version = history[-1]
        if self._cache_load(version=version['modified']):
            return self

        print(f'Loading page: {self.name}, modified: {version["modified"]}')
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
        return self._page['children']

    @property
    def parent(self):
        return self._parent

    @property
    def description(self):
        return re.sub(r"^[0-9]+.? *", "", get_text(self._page['description']))

    @property
    def base(self):
        return self._parent.base

    @property
    def default_url(self):
        #print(self.base, self._spec)
        if self._spec is not None and self._spec[1] is not None:
            return self.base + self._spec[1]
        return None

    @property
    def url(self):
        if self._page is not None and 'link' in self._page:
            return self._page['link']
        return self.default_url

    @property
    def id(self):
        return self._spec[0]

    @property
    def name(self):
        return f'{self.parent.name}/{self.id}'
   
    @property
    def title(self):
        return get_text(self._page['title'])

    @property
    def lastmod(self):
        return self._page['lastmod']

    @property
    def refmod(self):
        return self._page['refmod'] if 'refmod' in self._page else ''

    @property
    def child_class(self):
        return None

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

    def lookup(self, entry_id, use_cache=True):
        matches = [x for x in self.children if get_text(x[0]['text']) == entry_id]
        if matches:
            child = matches[0][0]
            spec = (get_text(child['text']), child['link'])
            return self.child_class(spec, self, use_cache=use_cache)
        return None

    def translate(self):
        if translate_page(self._page) > 0:
            self._cache_save()
            return True
        return False

    def history(self, limit=None, cutoff_date=None):
        return get_page_history(self, limit, cutoff_date)

class Archive(Table):
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

    def latest_changes(self, limit=100):
        return do_search(ARCHIVE_LIST[self._tag], limit=limit)

class Fond(Table):
    """Represents fond page."""
    @property
    def kind(self):
        return 'fond'

    @property
    def child_class(self):
        return Opus

class Opus(Table):
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

class Case(Table):
    """Represents case page."""
    @property
    def kind(self):
        return 'case'
