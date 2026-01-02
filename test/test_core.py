import os
from copy import copy
from urllib.parse import quote, unquote

import unittest
from birddog.wiki import ARCHIVE_BASE, canonicalize_title, lineage
from birddog.core import (
    Archive,
    Page,
    )
from birddog.runtime import Runtime, PageLRU, ArchiveWatcher


fond_id = '1'
opus_id = '74'
case_id = '1'

# ------------------ UTILITY UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_Archive(self):
        page = Archive('DAZHO')
        self.assertTrue(
            unquote(page.url) == unquote(ARCHIVE_BASE + '/wiki/Архів:ДАЖО/Д'))
        print('id', page.id)
        self.assertTrue(page.id == 'DAZHO')
        print('kind', page.kind)
        self.assertTrue(page.kind == 'archive')
        print('name', page.name)
        self.assertTrue(page.name == 'DAZHO-D')
        print('refmod', page.refmod)
        self.assertTrue(page.refmod == '')
        print('report', page.report, f'{page.kind},{page.name},{page.lastmod}')
        self.assertTrue(page.report == f'{page.kind},{page.name},{page.lastmod}')
        print('subarchive', page.subarchive)
        self.assertTrue(page.subarchive == 'D')
        print('title', page.title)
        self.assertTrue(page.title == 'Архів:ДАЖО/Д')
        print('url', page.url)
        
    def test_Fond(self):
        page = Archive('DAZHO').lookup(fond_id)
        self.assertTrue(
            page.url == f'{ARCHIVE_BASE}/wiki/Архів:ДАЖО/1')
        print('id', page.id)
        self.assertTrue(page.id == fond_id)
        print('kind', page.kind)
        self.assertTrue(page.kind == 'fond')
        print('name', page.name)
        self.assertTrue(page.name == f'{page.parent.name}/{fond_id}')
        print('refmod', page.refmod)
        self.assertTrue(page.refmod == '')
        print('report', page.report)
        self.assertTrue(page.report == f'{page.kind},{page.name.replace(",", "")},{page.lastmod}')
        print('title', page.title)
        self.assertTrue(page.title == f'Архів:ДАЖО/{fond_id}')
        print('url', page.url)
       
    def test_Opus(self):
        page = Archive('DAZHO').lookup(fond_id).lookup(opus_id)
        self.assertTrue(page.url == f'{page.parent.url}/{opus_id}')
        print('id', page.id)
        self.assertTrue(page.id == opus_id)
        print('kind', page.kind)
        self.assertTrue(page.kind == 'opus')
        print('name', page.name)
        self.assertTrue(page.name == f'{page.parent.name}/{opus_id}')
        print('refmod', page.refmod)
        self.assertTrue(page.refmod == '')
        print('report', page.report)
        self.assertTrue(page.report == f'{page.kind},{page.name.replace(",", "")},{page.lastmod}')
        print('title', page.title)
        #print(f'{page.parent.title}/{opus_id}')
        self.assertTrue(page.title == f'{page.parent.title}/{opus_id}')
        print('url', page.url)

    def test_Case(self):
        page = Archive('DAZHO').lookup(fond_id).lookup(opus_id).lookup(case_id)
        self.assertTrue(
            page.url == f'{page.parent.url}/{case_id}')
        print('id', page.id)
        self.assertTrue(page.id == case_id)
        print('kind', page.kind)
        self.assertTrue(page.kind == 'case')
        print('name', page.name)
        self.assertTrue(page.name == f'{page.parent.name}/{case_id}')
        print('refmod', page.refmod)
        self.assertTrue(page.refmod == '')
        print('report', page.report)
        self.assertTrue(page.report == f'{page.kind},{page.name.replace(",", "")},{page.lastmod}')
        print('title', page.title)
        self.assertTrue(page.title == f'{page.parent.title}/{case_id}')
        print('url', page.url)

    def test_PageLRU(self):
        lru = PageLRU()
        page = lru.lookup_by_address("DAHMO", "D")
        print(page.title)
        page = lru.lookup_by_address("DAHMO", "D", "1")
        print(page.title)
        page = lru.lookup_by_address("DAHMO", "D", "2", "3")
        print(page.title)
        page = lru.lookup_by_address("DAHMO", "R", "Р-5", "1")
        print(page.title)

    def test_ArchiveWatcher(self):
        runtime = Runtime()
        watcher = ArchiveWatcher("DAKO", "D", cutoff_date="2025,03,01", runtime=runtime)
        self.assertFalse(watcher.resolved)
        self.assertFalse(watcher.unresolved)
        watcher.check()
        self.assertFalse(watcher.resolved)
        self.assertTrue(watcher.unresolved)
        #print(watcher.unresolved)
        item = watcher.key("DAKO", "D", "280", "2", "111")
        #item = "DAKO-D/1455/1/169"
        self.assertTrue(item in watcher.unresolved)
        watcher.resolve(item)
        self.assertFalse(item in watcher.unresolved)
        self.assertTrue(item in watcher.resolved)
        watcher.unresolve(item)
        self.assertTrue(item in watcher.unresolved)
        self.assertFalse(item in watcher.resolved)
        watcher = ArchiveWatcher('DADNO', 'R', '2025,03,01', runtime=runtime)
        watcher.check()
        for key in list(watcher.unresolved.keys())[::77]:
            address = key.rstrip(',').split(',') + 3 * [None]
            address = address[:5]
            page = runtime.lookup_by_address(*address)
            self.assertTrue(page.lastmod <= watcher.unresolved[key]['modified'])

    def test_Titles(self):
        titles = [ 
            "Архів:Архівний_відділ_виконавчого_комітету_Кременчуцької_міської_ради/2-ОС/1",
            #"Архів:ДАПО/Р-9126/2",
            "Архів:ДАЖО/1/1",
            "Архів:ДАК/312/1",
            "Архів:ДАЖО/1/74",
            "Архів:Лука_Мала",
            "Архів:ЦДІАК/1/1",
            "Архів:ДАХО/Д",
            "Архів:ДАПО/Р/1–1000",
            "Архів:ЦДІАК/28",
            "Архів:ЦДІАК/28/1",
            "Архів:ДАЖО/1",
            "Архів:ДАК/Р-352",
            "Архів:Архівний відділ виконавчого комітету Кременчуцької міської ради/Р",
            "Архів:ДАЖО/Д",
            "Архів:ДАДнО/Р-6478/2", 
            "Архів:ДАЖО/752", 
            "Архів:ДАКрО/225/1/25", 
            "Архів:ДАКрО/225", 
            "Архів:ДАСО/Р", 
            "Архів:ДАХмО/К", 
            "Архів:ДАКрО/225/1/144а", 
            "Архів:ДАПО/978/1",
            "Архів:ДАПО/Р",
            "Архів:ДАХмО/Р-6193",
            "Архів:ДАКрО/П-5907/2Р",
            "Архів:ДАОО/Р-8085/1",
            "Архів:ДАКрО/225/1",
            "Архів:ДАПО/1072/1/1",
            "Архів:ДАПО/978/1/135",
            "Архів:ДАПО/978",
            "Архів:ДАКО/Р-5634/1/3092",
            "Архів:ІР_НБУВ/130/1",
            "Архів:ІР НБУВ/232/1"
            ]
        runtime = Runtime()

        def test_title(title, runtime):
            page = runtime.lookup_by_title(title)
            self.assertEqual(page.title, title)
            page1 = runtime.lookup_by_address(*page.address)
            self.assertEqual(page1.title, title)
        
        for title in titles:    
            ancestry = lineage(title)
            for item in ancestry:
                test_title(item, runtime)

if __name__ == "__main__":
    unittest.main()
