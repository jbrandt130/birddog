import requests
import json
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook
from googletrans import Translator

base = 'https://e-resource.tsdavo.gov.ua'

translator = Translator()
def translation(text):
    result = Translator().translate(text, src='uk', dest='en')
    if isinstance(text, (list, tuple)):
        return [item.text for item in result]
    else:
        return result.text

def translate_field(items, field_name):
    batch = [item[field_name] for item in items]
    batch = translation(batch)
    for i, text in enumerate(batch):
        items[i][field_name] = text    

class FondCollection:
    def __init__(self):
        self._base = 'https://e-resource.tsdavo.gov.ua'
        self._endpoint = '/api/v1/fonds/'
        self._sortfield = 'FondNumber'
        self._sortorder = 'asc'
    
    def load_page(self, page=1, limit=20, translate=True):
        url = f'{self._base}{self._endpoint}?Limit={limit}&Page={page}&SortField={self._sortfield}&SortOrder={self._sortorder}'
        data = requests.get(url)
        result = json.loads(data.text)
        soup = BeautifulSoup(result['View'], 'lxml')
        items = []
        for tag in soup.find_all('td', attrs = {'class': 'name'}):
            date = tag.find_next_sibling()
            pages = date.find_next_sibling()
            number = tag.find_previous_sibling()
            title = tag.text.strip()
            item = { 'number': number.text,
                     'link': tag.a["href"], 
                     'title': title, 
                     'date': date.text,
                     'pages': pages.text}
            items.append(item)
        if translate:
            translate_field(items, 'title')
        return items
    
    def load_all(self, translate=True):
        result = []
        limit = 100
        page = 1
        while True:
            print('loading page', page)
            chunk = self.load_page(page=page, limit=limit, translate=translate)
            if not chunk: break
            print(f'  got {len(chunk)} items')
            result += chunk
            page += 1
        return result

class Inventory:
    def __init__(self, endpoint):
        self._base = 'https://e-resource.tsdavo.gov.ua'
        self._endpoint = '/api/v1' + endpoint
    
    def load_page(self, page=1, limit=20, translate=True):
        url = f'{self._base}{self._endpoint}?Limit={limit}&Page={page}'
        data = requests.get(url)
        result = json.loads(data.text)
        soup = BeautifulSoup(result['View'], 'lxml')
        items = []
        for tag in soup.find_all('div', attrs = {'class': 'row with-border-bottom column'}):
            #print(tag)
            left = tag.find('div', attrs = {'class': 'left'})
            link = left.a['href']
            title = left.a.text.strip()
            date = left.find('span', attrs = {'class': 'date'}).text
            right = tag.find('div', attrs = {'class': 'right'})
            desc = right.text.strip()
            items.append({
                'link': link,
                'title': title,
                'date': date,
                'description': desc
            })
        if translate:
            translate_field(items, 'title')
            translate_field(items, 'description')
        return items
        
    def load_all(self, translate=True):
        result = []
        limit = 100
        page = 1
        while True:
            print('loading page', page)
            chunk = self.load_page(page=page, limit=limit, translate=translate)
            if not chunk: break
            print(f'  got {len(chunk)} items')
            result += chunk
            page += 1
        return result

def load_fond(url):
    soup = BeautifulSoup(requests.get(url).text, 'lxml')
    result = []
    for tag in soup.find_all('div', attrs = {'class': 'row with-border-bottom thin-row'}):
        title = tag.find('div', attrs = {'class': 'left'})
        item = {'link': title.a["href"], 'title': title.text.strip()}
        result.append(item)
    return result

def index_fonds(fonds):
    index = {}
    for item in fonds:
        index[item['number']] = item
    return index

def save_object(obj, fname):
    with open(fname, 'w') as f:
        f.write(json.dumps(obj))

def load_object(fname):
    with open(fname) as f:
        return json.loads(f.read())
