# CDAVO scan

from tsdavo import collection, base, load_fond, index_fonds, OpusTable, translation, Fond
from openpyxl import Workbook, load_workbook
import os.path
import json

keywords = ['pogrom', 'jewish', 'anti-semitism', 'hebrew', 'camps', 'judaic', 'judaism']

candidates = collection.load()

def save_object(obj, fname):
    with open(fname, 'w') as f:
        f.write(json.dumps(obj))

def load_object(fname):
    try:
        with open(fname) as f:
            return json.loads(f.read())
    except:
        pass
    return None

def filename(opus_id, match):
    match = '' if match else 'No'
    return f'results/CDAVO Opus {opus_id} {match} Match.xlsx'

def format_fond(fond, base=base):
    return (fond._name, fond._url, fond._dates, fond._num_opi, fond._num_cases)

finished = load_object('cache/finished.json')
if not finished:
    finished = []

initial_scan = False
if initial_scan:
    for fond in candidates:
        if fond['number'] not in finished:
            print('Scanning fond:', fond['number'], fond['title'])
            opi, description = load_fond(base + fond['link'])
            print('Scanning', len(opi), 'opi')
            for i in range(len(opi)):
                opus = OpusTable(fond['number'], i + 1)
                if os.path.isfile(filename(opus._opus_id, True)) or os.path.isfile(filename(opus._opus_id, False)):
                    continue
                opus_match, case_match = opus.scan_for_keywords(keywords)
                if opus_match or any(case_match):
                    print('Match:', opus._opus_id)
                    opus.export(fname=filename(opus._opus_id, True), case_filter=case_match)
                case_no_match = [not x for x in case_match]
                if any(case_no_match):
                    print('Match:', opus._opus_id)
                    opus.export(fname=filename(opus._opus_id, False), case_filter=case_no_match)
            finished.append(fond['number'])
            save_object(finished, 'cache/finished.json')

#candidates = [ collection.lookup('1')]

def add_row(row, row_values, link):
    for i, v in enumerate(row_values):
        row[i].value = v
    row[0].hyperlink = link
    row[1].hyperlink = link

wb = Workbook()
sheet = wb.active
sheet.append(['Fond', 'Link', 'Dates', '#Opi', '#Cases', '#Matched Opi', '#Matched Cases'])

for j, fond_data in enumerate(candidates):
    fond = Fond(fond_data['number'])
    opus_match_count = 0
    case_match_count = 0
    for i in range(len(fond._opi)):
        opus = OpusTable(fond._name, i + 1)
        opus_match, case_match = opus.scan_for_keywords(keywords)
        if opus_match or any(case_match):
            opus_match_count += 1
        case_match_count += sum([1 if x else 0 for x in case_match])
        row_values = format_fond(fond) + (opus_match_count, case_match_count)
    #print('row_values', row_values)
    print(row_values)
    add_row(sheet[j+2], row_values, row_values[1])

wb.save('results/CDAVO Scan Summary.xlsx')
