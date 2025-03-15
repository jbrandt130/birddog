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

def get_page_history(page):
    """
    Return version history of given page, sorted in reverse chronological order.
    Returns list of dicts containing keys: 
        "modified": modification date in standard format
        "link": url to the corresponding page version
    """
    url = page.history_url
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
        self._pages = None
        if use_cache:
            try:
                self._pages = load_cached_object(f'{self.name}.json')
                print(f'Retrieving from cache: {self.name}')
                # sort by ascending mod date
                self._pages.sort(key=lambda x: x['lastmod'])
                self._page = self._pages[-1]
            except CacheMissError:
                # drop through on cache miss
                pass
        if not self._page:
            if self.default_url is not None:
                print(f'Loading page: {self.name}')
                self._page = read_page(self.default_url)
                self._pages = [self._page]
                self._update_cache()

    def _update_cache(self):
        self._pages.sort(key=lambda x: x['lastmod'])
        save_cached_object(self._pages, f'{self.name}.json')

    def latest(self):
        """Set page state to the latest version."""
        new_page = read_page(self.default_url)
        cache_page = next(
            (page for page in self._pages if page['lastmod'] == new_page['lastmod']),
            None)
        if cache_page:
            print('Nothing new.')
            self._page = cache_page
        else:
            print('Found new version:', new_page['lastmod'])
            self._pages.append(new_page)
            self._page = new_page
            self._update_cache()
        return self

    def revert_to(self, date):
        """Revert page state to particular version date."""
        version = next((v for v in self.history if v['modified'] <= date), None)
        if not version:
            print('No version exists on or before', date)
            return None
        modified_date = version['modified']
        cached_page = next((page for page in self._pages if page['lastmod'] == modified_date), None)
        if cached_page:
            self._page = cached_page
            print(f'Retrieving from cache: {self.name}, modified: {modified_date}')
        else:
            print(f'Loading page: {self.name}, modified: {modified_date}')
            self._page = read_page(version['link'])
            self._pages.append(self._page)
            self._update_cache()
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
    def history_url(self):
        # arbitrary cutoff of page edit history to 10000 items (not sure what the site allows)
        return self.default_url.replace(
            '/wiki/', 
            '/w/index.php?action=history&limit=10000&title=')

    @property
    def history(self):
        return get_page_history(self)

    @property
    def id(self):
        return self._spec[0]

    @property
    def name(self):
        return f'{self._parent.name}/{self.id}'

    @property
    def ascii_name(self):
        return f'{self.parent.ascii_name}/{self.id}'
   
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
            self._update_cache()
            return True
        return False

class Archive(Table):
    """Represents a top level archive page."""
    def __init__(self, tag, subarchive=SUBARCHIVES[0], base=ARCHIVE_BASE, use_cache=True):
        self._tag = tag
        archive_name = ARCHIVE_LIST[tag] if tag in ARCHIVE_LIST else None
        archive_name = f'{archive_name}/{subarchive}'
        self._name = archive_name
        self._subarchive = subarchive
        self._base = base
        super().__init__(None, None, use_cache=use_cache)

    @property
    def tag(self):
        return self._tag

    @property
    def id(self):
        return self._tag

    @property
    def name(self):
        return self._name

    @property
    def ascii_name(self):
        return self.tag

    @property
    def subarchive(self):
        return self._subarchive

    @property
    def base(self):
        return self._base

    @property
    def default_url(self):
        return self._base + '/wiki/' + str(urllib.parse.quote(self._name))

    @property
    def report(self):
        return f'{self.kind},{self.tag}/{self.subarchive},{self.lastmod}'

    @property
    def child_class(self):
        return Fond

    @property
    def kind(self):
        return 'archive'

    def latest_changes(self, limit=100):
        return do_search(ARCHIVE_LIST[self._tag], limit=limit)

class Fond(Table):
    """Represents fond page."""
    @property
    def name(self):
        return f'{self._parent.name}/{self.id}'

    @property
    def child_class(self):
        return Opus

    @property
    def kind(self):
        return 'fond'


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
