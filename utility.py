import json
import os
import re
from translate import translation, is_english

# global constants

ARCHIVE_BASE    = 'https://uk.wikisource.org'
SUBARCHIVES     = ['Д', 'Р', 'П']

# cache support

cache_dir = './cache'

def make_path_if_needed(fname):
    pos = fname.rfind('/')
    if pos >= 0:
        os.makedirs(fname[:pos], exist_ok=True)

def save_cached_object(obj, fname):
    fname = f'{cache_dir}/{fname}'
    make_path_if_needed(fname)
    with open(fname, 'w') as f:
        f.write(json.dumps(obj))

def load_cached_object(fname):
    fname = f'{cache_dir}/{fname}'
    with open(fname) as f:
        return json.loads(f.read())


# static data resources

with open('resources/archives.json', encoding="utf8") as f:
    archive_list = json.load(f)

# used for standardizing dates in numerical format
with open('resources/months.json', encoding="utf8") as f:
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
    return len(batch)