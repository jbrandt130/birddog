#
# Ukraine records archive scraper

import pandas as pd
import numpy as np
import requests
import urllib.request, urllib.parse, urllib.error
import io
import re
import json
import datetime
import os
import random
from time import sleep
from bs4 import BeautifulSoup
from translate import translation, translate_field, is_english
from cache import load_cached_object, save_cached_object

#
# global constants

archive_base    = 'https://uk.wikisource.org'
#column_names    = [ '№' , 'Опис', 'Номер', 'Фонд', '#' ]
subarchives     = ['Д', 'Р', 'П']
auto_translate  = True

with open('archives.json') as f:
    archive_list = json.load(f)

# used for standardizing dates in numerical format
with open('months.json') as f:
    uk_months = json.load(f)


# date handling

def format_date(message, uk_months=uk_months):
    message = message.replace(',', '')
    message = message.split(' ')
    message = map(lambda x: uk_months[x] if x in uk_months else x, message)
    message = ','.join(reversed(list(message)))
    return message

lastmod_pattern = re.compile('[0-9][0-9]:[0-9][0-9].+[0-9][0-9]?.+[0-9][0-9][0-9][0-9]')

def lastmod(message):
    #print(message)
    result = re.search(lastmod_pattern, message)
    if result is not None:
        return format_date(result.group(0))
    else:
        return message

number_pattern = re.compile('[0-9]+([–-][0-9]+)?')
def is_numeric(s):
    return re.fullmatch(number_pattern, s.strip()) is not None

def translate_descriptions(table):
    batch = []
    for row in table:
        for entry in row:
            text = entry['text']
            if text and not is_numeric(text) and not is_english(text):
                batch.append(entry['text'])
    print(f'batch translation - {len(batch)} items')
    batch = translation(batch)
    for row in table:
        for entry in row:
            text = entry['text']
            if text and not is_numeric(text) and not is_english(text):
                entry['text'] = batch.pop(0)

def get_text(element):
    text = element.text.strip() if element is not None else None
    if text is not None and auto_translate:
        text = translation(text)
    return text

# extract archive information for given page
# return struct with page title, description, table header, table contents, and lastmod date
# optionally translate relevant text to english from ukrainian
def read_page(url):
    soup = BeautifulSoup(requests.get(url).text, 'lxml')
    title = soup.find('span', attrs = {'class': 'mw-page-title-main'})
    desc = soup.find('span', attrs = {'id': 'header_section_text'})
    table = soup.find('table', attrs = {'class': 'wikitable'})
    children = []
    header = []
    if table:
        for tr in table.find_all('tr'):
            if not header:
                for th in tr.find_all('th'):
                    header.append(th.text.strip())
            item = []
            for td in tr.find_all('td'):
                a = td.find('a')
                url = a['href'] if a else None
                text = td.text.strip()
                item.append({'text': text, 'link': url})
            if item:
                children.append(item)
        if auto_translate:
            translate_descriptions(children)
            header = translation(header)
    footer = soup.find('li', attrs={'id': 'footer-info-lastmod'})
    last_modified = lastmod(footer.text) if footer else None

    return { 
        'title': get_text(title),
        'description': get_text(desc),
        'header': header,
        'children': children,
        'lastmod': last_modified
    }

# search for matching entries, sorted on last modification date
# for each hit, return dict with item title, link, and lastmod date
def do_search(query_string, limit=10, offset=0):
    query_string = urllib.parse.quote(query_string, safe='', encoding=None, errors=None)
    url = f'{archive_base}/w/index.php?limit={limit}&offset={offset}&ns0=1&sort=last_edit_desc&search={query_string}'
    soup = BeautifulSoup(requests.get(url).text, 'lxml')
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
        if use_cache:
            try:
                self._page = load_cached_object(f'{self.name}.json')
                print(f'Retrieving from cache: {self.name}')
            except:
                pass
        if not self._page:
            print(f'Loading page: {self.name}')
            self._page = read_page(self.url)
            save_cached_object(self._page, f'{self.name}.json')

    @property
    def children(self):
        return self._page['children']

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
    def report(self):
        # make sure no commas in the name
        return f'{self.kind},{self.name.replace(",", "")},{self.lastmod}'

    def lookup(self, entry_id, use_cache=True):
        matches = [x for x in self.children if x[0]['text'] == entry_id]
        print(matches)
        if matches:
            child = matches[0][0]
            spec = (child['text'], child['link'])
            return self.child_class(spec, self, use_cache=use_cache)
        return None

class Archive(Table):
    def __init__(self, tag, archives=archive_list, subarchive=subarchives[0], base=archive_base, use_cache=True):
        self._tag = tag
        archive_name = archives[tag] if tag in archive_list else None
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
def scan_archive(archive, stream=None):
    output = Logger(stream)
    output.write(archive.report)
    for f in range(len(archive.children)):
        fond = Fond(archive.children[f], archive)
        output.write(fond.report)
        for o in range(len(fond.children)):
            opus = Opus(fond.children[o], fond)
            output.write(opus.report)
            for c in range(len(opus.children)):
                case = Case(opus.children[c], opus)
                output.write(case.report)

# -----------  probably obsolete (keep for now) -------
def run_report(items = archive_list):
    out_dir = './var/' + str(datetime.datetime.now())
    try:
        os.mkdir('var')
    except:
        pass
    os.mkdir(out_dir)
    print('reporting to', out_dir)
    for item in items:
        if items[item] is not None:
            with open(f'{out_dir}/{item}.csv', 'w') as file:
                for sub in subarchives:
                    print(f'scanning {item}/{sub}...')
                    try:
                        scan_archive(Archive(item, subarchive=sub), file)
                    except KeyboardInterrupt:
                        return
                    except BaseException as e:
                        print(f'... EXCEPTION occured while scanning {item}/{sub}')
                        print(e)

# -----------  probably obsolete (keep for now) -------
class Logger:
    def __init__(self, output = None):
        self._stream = output

    def write(self, message):
        if self._stream is None:
            print(message)
        else:
            self._stream.write(message + '\n')

if __name__ == "__main__":
    print("Running archive report")
    run_report()

