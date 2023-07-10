#
# Ukraine records archive scraper

import pandas as pd
import numpy as np
import urllib.request, urllib.parse, urllib.error
import io
import re
import json
import datetime
import os
import random
from time import sleep



archive_base = base='https://uk.wikisource.org'
column_names = [ '№' , 'Опис', 'Номер', 'Фонд', '#' ]
subarchives = ['Д', 'Р', 'П']

with open('archives.json') as f:
    archive_list = json.load(f)

with open('months.json') as f:
    uk_months = json.load(f)


def pick_table(tables, column_index=0):
    if tables is None:
        return None
    for table in tables:
        try:
            if table.columns[0][column_index] in column_names:
                return table
        except:
            pass
    print('pick_table: not found')
    return None

def extract_table(df, only_linked=True):
    if df is None:
        return []
    table = df.to_numpy()
    table = table.tolist()
    result = [row[0] for row in table]
    #print (result)
    result = map(lambda x: (x[0], None) if x[1] is None or 'redlink' in x[1] else x, result)
    if only_linked:
        result = filter(lambda x: x[1] is not None, result)
    return list(result)

def web_throttle(scale_factor = 1):
    web_delay_limit = 1 # seconds
    sleep((1. + random.random() * web_delay_limit) * scale_factor)

def open_url(url, request_timeout=5):
    print('open_url:', url)
    tries = 1
    try_limit = 3
    while tries <= try_limit:
        web_throttle(tries)
        result = None
        try:
            result = urllib.request.urlopen(url, timeout=request_timeout)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print('404: Page not found')
                return None
            else:
                print('HTTP error:', e.code, e.reason)
        except urllib.error.URLError as e:
            print('URL error:', e.reason)
        except BaseException as e:
            print('exception in urlopen:', type(e))
        if result is not None:
            return result
        print(f'FAILED open_url(tries={tries}): {url}')
        tries += 1
    return None

def format_date(message, uk_months=uk_months):
    message = message.replace(',', '')
    message = message.split(' ')
    message = map(lambda x: uk_months[x] if x in uk_months else x, message)
    message = ','.join(reversed(list(message)))
    return message

lastmod_pattern = re.compile('[0-9][0-9]:[0-9][0-9].+[0-9][0-9]?.+[0-9][0-9][0-9][0-9]')

def lastmod(message):
    #print(message)
    if b'lastmod' in message:
        message = message.decode('utf-8')
        result = re.search(lastmod_pattern, message)
        if result is not None:
            return format_date(result.group(0))
        else:
            return message
    return None

def read_html(url):
    if url is None:
        return (None, None)
    file = open_url(url)
    if file is None:
        return (None, None)
    message = file.read()
    mod_date = lastmod(message)
    #print('read_html: Date:', mod_date)
    tables = None
    try:
        tables = pd.read_html(message, extract_links="all")
    except ImportError as e:
        print('ImportError encountered in read_html. Skipping...')
        pass
    return (tables, mod_date)

def get_lastmod(url):
    if url is not None:
        file = open_url(url)
        if file is None:
            return 'CONNECTION FAILED'
        return lastmod(file.read())
    return None

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

class Logger:
    def __init__(self, output = None):
        self._stream = output

    def write(self, message):
        if self._stream is None:
            print(message)
        else:
            self._stream.write(message + '\n')

class Table:
    def __init__(self, spec, parent, is_leaf=False):
        self._parent = parent
        self._spec = spec
        if is_leaf:
            self._lastmod = get_lastmod(self.url)
            self._table = None
            self._children = None
        else:
            result = read_html(self.url)
            self._lastmod = result[1]
            self._table = pick_table(result[0])
            if self._table is None:
                print('Table not found:', self.url)
            self._children = extract_table(self._table)

    @property
    def children(self):
        return self._children

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
        return self._lastmod

    @property
    def child_class(self):
        return None

    @property
    def report(self):
        # make sure no commas in the name
        return f'{self.kind},{self.name.replace(",", "")},{self.lastmod}'

    def lookup(self, entry_id):
        matches = [x for x in self._children if x[0] == entry_id]
        return self.child_class(matches[0], self) if len(matches) > 0 else None

class Archive(Table):
    def __init__(self, tag, archives=archive_list, subarchive=subarchives[0], base=archive_base):
        self._tag = tag
        archive_name = archives[tag] if tag in archive_list else None
        archive_name = f'{archive_name}/{subarchive}'
        self._name = archive_name
        self._subarchive = subarchive
        self._base = base
        super().__init__(None, None)

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
    def __init__(self, spec, opus):
        super().__init__(spec, opus, is_leaf=True)

    @property
    def kind(self):
        return 'case'


if __name__ == "__main__":
    print("Running archive report")
    run_report()

