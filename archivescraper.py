#
# Ukraine records archive scraper

import re
import json
import time
import urllib.parse
import requests
from bs4 import BeautifulSoup
from translate import translation, is_english
from cache import load_cached_object, save_cached_object

#
# global constants

ARCHIVE_BASE    = 'https://uk.wikisource.org'
SUBARCHIVES     = ['Д', 'Р', 'П']
REQUEST_TIMEOUT = 10 # seconds

with open('archives.json', encoding="utf8") as f:
    archive_list = json.load(f)

# used for standardizing dates in numerical format
with open('months.json', encoding="utf8") as f:
    uk_months = json.load(f)

#
# date handling

def format_date(message):
    message = message.replace(',', '')
    message = message.split(' ')
    message = map(lambda x: uk_months[x] if x in uk_months else x, message)
    message = ','.join(reversed(list(message)))
    return message

lastmod_pattern = re.compile('[0-9][0-9]:[0-9][0-9].+[0-9][0-9]?.+[0-9][0-9][0-9][0-9]')

def lastmod(message):
    result = re.search(lastmod_pattern, message)
    if result is not None:
        return format_date(result.group(0))
    return message

#
# multilingual support

number_pattern = re.compile('[0-9]+([–-][0-9]+)?')
def is_numeric(s):
    return re.fullmatch(number_pattern, s.strip()) is not None

def form_text_item(source_text, translate=False):
    result = { 'uk': source_text }
    if not source_text or is_numeric(source_text) or is_english(source_text):
        result['en'] = source_text
    if translate:
        result['en'] = translation(source_text)
    return result

def get_text(text_item):
    return text_item['en'] if 'en' in text_item else text_item['uk']

def form_element_text(element):
    text = element.text.strip() if element is not None else None
    return form_text_item(text)

def needs_translation(item):
    return isinstance(item, dict) and 'uk' in item and 'en' not in item

def translate_page(page):
    batch = []
    items = []

    def queue_items(x, batch, items):
        if needs_translation(x):
            batch.append(x['uk'])
            items.append(x)
        elif isinstance(x, (list, tuple)):
            for v in x:
                queue_items(v, batch, items)
        elif isinstance(x, dict):
            for v in x.values():
                queue_items(v, batch, items)

    queue_items(page, batch, items)
    if batch:
        print(f'Batch translation: {len(batch)} items...')
        start = time.time()
        batch = translation(batch)
        elapsed = time.time() - start
        print(f'    ...completed ({elapsed:.2f} sec.)')
        for i, v in enumerate(batch):
            items[i]['en'] = v

# extract archive information for given page
# return struct with page title, description, table header, table contents, and lastmod date
def read_page(url):
    soup = BeautifulSoup(requests.get(url, timeout=REQUEST_TIMEOUT).text, 'lxml')
    title = soup.find('span', attrs = {'class': 'mw-page-title-main'})
    desc = soup.find('span', attrs = {'id': 'header_section_text'})
    table = soup.find('table', attrs = {'class': 'wikitable'})
    children = []
    header = []
    if table:
        for tr in table.find_all('tr'):
            if not header:
                for th in tr.find_all('th'):
                    header.append(form_element_text(th))
            item = []
            for td in tr.find_all('td'):
                a = td.find('a')
                url = a['href'] if a else None
                text = form_text_item(td.text.strip())
                item.append({'text': text, 'link': url})
            if item:
                children.append(item)
    footer = soup.find('li', attrs={'id': 'footer-info-lastmod'})
    last_modified = lastmod(footer.text) if footer else None

    return {
        'title': form_element_text(title),
        'description': form_element_text(desc),
        'header': header,
        'children': children,
        'lastmod': last_modified
    }

# search for matching entries, sorted on last modification date
# for each hit, return dict with item title, link, and lastmod date
def do_search(query_string, limit=10, offset=0):
    query_string = urllib.parse.quote(query_string, safe='', encoding=None, errors=None)
    url = f'{ARCHIVE_BASE}/w/index.php?limit={limit}&offset={offset}&ns0=1&sort=last_edit_desc&search={query_string}'
    soup = BeautifulSoup(requests.get(url, timeout=REQUEST_TIMEOUT).text, 'lxml')
    results = []
    for result in soup.find_all('li', attrs = {'class': 'mw-search-result'}):
        div = result.find('div', attrs = {'class': 'mw-search-result-heading'})
        data = result.find('div', attrs = {'class': 'mw-search-result-data'})
        data = data.text.strip()
        p = data.find('-')
        data = format_date(data[(p+1):].strip())
        item = {
            'title': div.a['title'],
            'link': div.a['href'],
            'lastmod': data
        }
        results.append(item)
        #print(result.text)
    return results

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
            except:
                pass
        if not self._page:
            print(f'Loading page: {self.name}')
            self._page = read_page(self.url)
            self._pages = [self._page]
            self._update_cache()

    def _update_cache(self):
        #page_dict = {page['lastmod']: page for page in self._pages}
        #page_dict[self._page['lastmod']] = self._page
        #self._pages = list(page_dict.values())
        self._pages.sort(key=lambda x: x['lastmod'])
        save_cached_object(self._pages, f'{self.name}.json')

    def refresh(self):
        new_page = read_page(self.url)
        for page in self._pages:
            if page['lastmod'] == new_page['lastmod']:
                print('Nothing new.')
                return
        print('Found new version:', new_page['lastmod'])
        self._pages.append(new_page)
        self._page = new_page
        self._update_cache()

    @property
    def children(self):
        return self._page['children']

    @property
    def description(self):
        return get_text(self._page['description'])

    @property
    def base(self):
        return self._parent.base

    @property
    def url(self):
        return self.base + self._spec[1] if self._spec is not None else None

    @property
    def id(self):
        return self._spec[0]

    @property
    def name(self):
        return f'{self._parent.name}/{self.id}'

    @property
    def lastmod(self):
        return self._page['lastmod']

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
        #print(matches)
        if matches:
            child = matches[0][0]
            spec = (get_text(child['text']), child['link'])
            return self.child_class(spec, self, use_cache=use_cache)
        return None

    def translate(self):
        translate_page(self._page)
        self._update_cache()

class Archive(Table):
    def __init__(self, tag, subarchive=SUBARCHIVES[0], base=ARCHIVE_BASE, use_cache=True):
        self._tag = tag
        archive_name = archive_list[tag] if tag in archive_list else None
        archive_name = f'{archive_name}/{subarchive}'
        self._name = archive_name
        self._subarchive = subarchive
        self._base = base
        super().__init__(None, None, use_cache=use_cache)

    @property
    def tag(self):
        return self._tag

    @property
    def name(self):
        return self._name

    @property
    def subarchive(self):
        return self._subarchive

    @property
    def base(self):
        return self._base

    @property
    def url(self):
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

class Fond(Table):
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
    @property
    def kind(self):
        return 'opus'

    @property
    def child_class(self):
        return Case


class Case(Table):
    @property
    def kind(self):
        return 'case'

# -----------  probably obsolete (keep for now) -------
class Logger:
    def __init__(self, output = None):
        self._stream = output

    def write(self, message):
        if self._stream is None:
            print(message)
        else:
            self._stream.write(message + '\n')
