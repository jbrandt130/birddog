# CDAVO scan

from tsdavo import FondCollection, Inventory, base, load_fond, index_fonds, OpusTable, translation
import os.path
import json

keywords = ['pogrom', 'jewish', 'anti-semitism', 'hebrew', 'camps', 'judaic', 'judaism']

fonds = FondCollection().load()
fond_index = index_fonds(fonds)

candidates = [ fond_index[id] for id in ['1123', '1688', '413', '19'] ]
candidates = fonds

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

finished = load_object('finished.json')
if not finished:
    finished = []

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
        save_object(finished, 'finished.json')
