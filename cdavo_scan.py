# CDAVO scan

from tsdavo import FondCollection, Inventory, base, load_fond, index_fonds, OpusTable, translation

keywords = ['pogrom', 'jewish', 'anti-semitism', 'hebrew', 'camps', 'judaic', 'judaism']

fonds = FondCollection().load()
fond_index = index_fonds(fonds)

candidates = [ fond_index[id] for id in ['1123', '1688', '413', '19'] ]
candidates = fonds

for fond in candidates:
    print('Scanning fond:', fond['number'], fond['title'])
    opi, description = load_fond(base + fond['link'])
    print('Scanning', len(opi), 'opi')
    for i in range(len(opi)):
        opus = OpusTable(fond['number'], i + 1)
        opus_match, case_match = opus.scan_for_keywords(keywords)
        if opus_match or any(case_match):
            print('Match:', opus._opus_id)
            opus.export(fname=f'results/CDAVO Opus {opus._opus_id} Match.xlsx', case_filter=case_match)
        case_no_match = [not x for x in case_match]
        if any(case_no_match):
            print('Match:', opus._opus_id)
            opus.export(fname=f'results/CDAVO Opus {opus._opus_id} No Match.xlsx', case_filter=case_no_match)
