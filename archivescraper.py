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
column_names = [ '№' , 'Опис', 'Номер', 'Фонд' ]
subarchives = ['Д', 'Р', 'П']

with open('archives.json') as f:
    archive_list = json.load(f)

with open('months.json') as f:
    uk_months = json.load(f)

#print(archive_list)

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

def to_csv(df, stream=None):
    output = Logger(stream)
    if df is not None:
        cols = df.columns
        cols = [col[0] for col in cols]
        cols.insert(1, 'Link')
        output.write(','.join(cols))
        table = df.to_numpy()
        table = table.tolist()
        for row in table:
            #print(row)
            fixed_row = list(row[0]) + [x[0] for x in row[1:]]
            fixed_row = map(lambda x: '' if 'redlink' in x else x, fixed_row)
            output.write(','.join(fixed_row))

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

lastmod_pattern = re.compile('[0-9][0-9]:[0-9][0-9].+[0-9][0-9]?.+[0-9][0-9][0-9][0-9]')

def web_throttle(scale_factor = 1):
    web_delay_limit = 2 # seconds
    sleep(random.random() * web_delay_limit * scale_factor)

def open_url(url):
    print('open_url:', url)
    tries = 1
    try_limit = 3
    while tries <= try_limit:
        web_throttle(tries)
        result = None
        try:
            result = urllib.request.urlopen(url)
        except urllib.error.HTTPError as e:
            #print('HTTP error:', e.code, e.reason)
            if e.code == 404:
                print('404: Page not found')
                return None
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
    for f in range(len(archive.fonds)):
        fond = Fond(archive.fonds[f], archive)
        output.write(fond.report)
        for o in range(len(fond.opi)):
            opus = Opus(fond.opi[o], fond)
            output.write(opus.report)
            for c in range(len(opus.cases)):
                case = Case(opus.cases[c], opus)
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
                        print(e.with_traceback)

class Logger:
    def __init__(self, output = None):
        self._stream = output

    def write(self, message):
        if self._stream is None:
            print(message)
        else:
            self._stream.write(message + '\n')

class Archive:
    def __init__(self, tag, archives=archive_list, subarchive=subarchives[0], base=archive_base):
        self._tag = tag
        archive_name = archives[tag] if tag in archive_list else None
        archive_name = f'{archive_name}/{subarchive}'
        self._name = archive_name
        self._subarchive = subarchive
        self._base = base
        result = read_html(self.url)
        self._lastmod = result[1]
        self._table = pick_table(result[0])
        self._fonds = self.get_fonds()

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
    def lastmod(self):
        return self._lastmod

    @property
    def base(self):
        return self._base
    
    @property
    def fonds(self):
        return self._fonds

    @property
    def url(self):
        return self._base + '/wiki/' + str(urllib.parse.quote(self._name))

    @property
    def report(self):
        return f'archive,{self.tag}/{self.subarchive},{self.lastmod}'
    
    def to_csv(self, stream=None):
        to_csv(self._table, stream)

    # output list of tuples: (fond_no, fond_link). If fond is redlinked then tuple is (fond_no, None).
    def get_fonds(self, only_linked=True):
        return extract_table(self._table, only_linked)

    def lookup(self, fond_id):
        matches = [x for x in self.fonds if x[0] == fond_id]
        return Fond(matches[0], self) if len(matches) > 0 else None

class Fond:
    def __init__(self, fond_spec, archive):
        self._archive = archive
        self._fond_spec = fond_spec
        result = read_html(self.url)
        self._lastmod = result[1]
        self._table = pick_table(result[0])
        if self._table is None:
            print('Fond: table not found:', self.url)
        self._opi = self.get_opi()

    @property
    def opi(self):
        return self._opi

    @property
    def url(self):
        return self._archive.base + self._fond_spec[1] if self._fond_spec is not None else None

    @property
    def id(self):
        return self._fond_spec[0]

    @property
    def base(self):
        return self._archive.base
    
    @property
    def name(self):
        return f'{self._archive.tag}/{self.id}'

    @property
    def lastmod(self):
        return self._lastmod

    @property
    def report(self):
        return f'fond,{self.name},{self.lastmod}'

    def to_csv(self, stream=None):
        to_csv(self._table, stream)

    def get_opi(self, only_linked=True):        
        return extract_table(self._table, only_linked)

    def lookup(self, fond_id):
        matches = [x for x in self.opi if x[0] == fond_id]
        return Opus(matches[0], self) if len(matches) > 0 else None

class Opus:
    def __init__(self, opus_spec, fond):
        self._fond = fond
        self._opus_spec = opus_spec
        result = read_html(self.url)
        self._lastmod = result[1]
        self._table = pick_table(result[0])
        self._cases = self.get_cases()

    @property
    def cases(self):
        return self._cases

    @property
    def id(self):
        return self._opus_spec[0]
    
    @property
    def base(self):
        return self._fond.base

    @property
    def url(self):
        return self.base + self._opus_spec[1] if self._opus_spec[1] is not None else None

    @property
    def name(self):
        return f'{self._fond.name}/{self.id}'

    @property
    def lastmod(self):
        return self._lastmod

    @property
    def report(self):
        return f'opus,{self.name},{self._lastmod}'

    def to_csv(self, stream=None):
        to_csv(self._table, stream)

    def get_cases(self, only_linked=True):        
        return extract_table(self._table, only_linked)

    def lookup(self, fond_id):
        matches = [x for x in self.cases if x[0] == fond_id]
        return Case(matches[0], self) if len(matches) > 0 else None

class Case:
    def __init__(self, case_spec, opus):
        self._opus = opus
        self._case_spec = case_spec
        self._lastmod = get_lastmod(self.url)

    @property
    def id(self):
        return self._case_spec[0]
    
    @property
    def base(self):
        return self._opus.base

    @property
    def url(self):
        return self.base + self._case_spec[1] if self._case_spec[1] is not None else None

    @property
    def name(self):
        return f'{self._opus.name}/{self.id}'

    @property
    def report(self):
        return f'case,{self.name},{self._lastmod}'


if __name__ == "__main__":
    print("Running archive report")
    run_report()

