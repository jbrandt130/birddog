import requests
import json
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook
from googletrans import Translator
import re
from datetime import date

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

class OpusTable:
    def __init__(self, fond_id, opus_index=1):
        self._fond_id = fond_id
        self._opus_index = opus_index
        self._opus_id = f'{fond_id}-{opus_index}'
        self._cases = None

    def load(self, base=base):
        try:
            self._cases = load_object(f'opus{self._opus_id}.json')
            return
        except:
            pass
        fonds = load_object('fonds.json')
        fond_index = index_fonds(fonds)
        url = base + fond_index[self._fond_id]['link']
        print(f'loading {self._fond_id} from {url}')
        opi = load_fond(url)
        url = opi[self._opus_index-1]['link']
        print(f'loading {self._opus_id} from {url}')
        inv = Inventory(url)
        self._cases = inv.load_all()
        save_object(self._cases, f'opus{self._opus_id}.json')

    def format_case(self, case, base=base):
        link = case['link']
        num = re.sub('Case ', '', case['title'])
        link = base + link
        desc = case['description']
        return (num, desc, link, case['date'])

    def add_row(self, row, case, base=base):
        num, desc, link, date = self.format_case(case, base)
        #print(num, title, link)
        row[0].value = num
        row[0].hyperlink = link
        row[0].style = 'Hyperlink'
        row[1].value = desc
        row[1].hyperlink = link
        row[1].style = 'Hyperlink'
        row[2].value = date

    def export(self, fname=None):
        if not fname:
            fname = f'TSDAVO Opus {self._opus_id}.xlsx'
        wb = load_workbook(filename = 'Opus Sample.xlsx')
        sheet = wb.active
        sheet.title = f"CDAVO {self._opus_id}"
        sheet["A1"].value = f"Archives / CDAVO / {self._fond_id} / {self._opus_index}"
        sheet["A2"].value = f"{self._opus_index} ..."
        sheet["D3"].value = "URL NEEDED!"
        sheet["D4"].value = date.today().strftime('%d %b %Y')
        #sheet.append(['Number', 'Description', 'Date'])
        first_row = 9
        for i, item in enumerate(self._cases):
            self.add_row(sheet[i + first_row], item)
        last_row = sheet.max_row
        #formula_row = sheet.max_row + 2
        sheet.append([])
        sheet.append([
            f"=rows(A9:a{last_row})",
            "totals",
            "",
            "",
            f"=counta(E9:E{last_row})",
            f"=counta(F9:F{last_row})",
            f"=counta(G9:G{last_row})",
            f"=sum(H9:H{last_row})",
            ])
        print(f'saving {sheet.max_row} rows to {fname}')
        wb.save(fname)


