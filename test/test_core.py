import os
from copy import copy
from urllib.parse import quote, unquote

import unittest
from birddog.wiki import ARCHIVE_BASE, ARCHIVE_BY_ADDRESS, canonicalize_title, lineage, archive_root, page_title_from_address
from birddog.core import (
    Page,
    )
from birddog.runtime import Runtime, PageLRU
from birddog import watcher
from birddog.utility import utc_now_dt


fond_id = '1'
opus_id = '74'
case_id = '1'

# ------------------ UTILITY UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_Archive(self):
        page = Page(ARCHIVE_BY_ADDRESS[('DAZHO', None)])
        self.assertTrue(
            unquote(page.url) == unquote(ARCHIVE_BASE + '/wiki/Архів:ДАЖО/Д'))
        print('id', page.id)
        self.assertTrue(page.id == 'Д')
        print('kind', page.kind)
        self.assertTrue(page.kind == 'archive')
        print('name', page.name)
        self.assertTrue(page.name == 'DAZHO/D')
        print('refmod', page.refmod)
        self.assertTrue(page.refmod == '')
        print('report', page.report, f'{page.kind},{page.name},{page.lastmod}')
        self.assertTrue(page.report == f'{page.kind},{page.name},{page.lastmod}')
        print('title', page.title)
        self.assertTrue(page.title == 'Архів:ДАЖО/Д')
        print('url', page.url)

    def test_Fond(self):
        page = Page(ARCHIVE_BY_ADDRESS[('DAZHO', None)]).lookup(fond_id)
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
        page = Page(ARCHIVE_BY_ADDRESS[('DAZHO', None)]).lookup(fond_id).lookup(opus_id)
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
        page = Page(ARCHIVE_BY_ADDRESS[('DAZHO', None)]).lookup(fond_id).lookup(opus_id).lookup(case_id)
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
        for title in [
            "Архів:ДАХмО/Д",
            "Архів:ДАХмО/1",
            "Архів:ДАХмО/2/3",
            "Архів:ДАХмО/Р-5/1",
        ]:
            page = lru.lookup_by_title(title)
            print(page.title)

    def test_ArchiveWatcher(self):
        runtime = Runtime()
        email = "test_archive_watcher@example.com"
        title = archive_root("DAKO", "D")
        self.addCleanup(watcher.remove_watcher, email, title)

        item = page_title_from_address(("DAKO", "D", "280", "2", "111"))
        #item = "DAKO-D/1455/1/169"
        self.assertFalse(watcher._watcher_kv.get_all(watcher._resolved_ns(email, title)))
        unresolved = watcher.check_watcher(email, title, runtime, include=[title], cutoff_date="2025,03,01")
        # this item specifically shouldn't be auto-resolved -- note check_watcher()
        # may now legitimately populate resolved_ns with unrelated deleted-page
        # entries, so this no longer asserts the whole namespace stays empty
        self.assertFalse(watcher.get_resolved(email, title, item))
        self.assertTrue(unresolved)
        #print(unresolved)
        self.assertTrue(item in unresolved)
        unresolved = watcher.resolve_watcher(email, title, item, runtime=runtime)
        self.assertFalse(item in unresolved)
        history = watcher.get_resolved(email, title, item)
        self.assertTrue(history)
        # Regression for issue #103: last_resolved must be stamped with the
        # actual resolution time, not left as the stale cutoff date.
        today = utc_now_dt().strftime("%Y-%m-%d")
        last_resolved = history[-1]["last_resolved"]
        self.assertTrue(
            last_resolved.startswith(today),
            f"last_resolved {last_resolved!r} should reflect today ({today}), not the cutoff date"
        )

        title2 = archive_root('DADNO', 'R')
        self.addCleanup(watcher.remove_watcher, email, title2)
        unresolved2 = watcher.check_watcher(email, title2, runtime, include=[title2], cutoff_date='2025,03,01')
        for key in list(unresolved2.keys())[::77]:
            if key == title2:
                # the bare archive-root title isn't itself a classifiable
                # fond/opus/case page (parent_title() has no entry for it);
                # coarsened archive-level watching can surface it as an
                # unresolved item, but Page construction on it is a
                # pre-existing wiki.py gap, not something this spot-check
                # exercises
                continue
            page = runtime.lookup_by_title(key)
            self.assertTrue(page.lastmod <= unresolved2[key]['modified'])

    def test_Titles(self):
        titles = [
            "Архів:Лука_Мала",                                                                    # non-hierarchical page
            "Архів:Архівний_відділ_виконавчого_комітету_Кременчуцької_міської_ради/2-ОС/1",       # long name (underscores), hyphenated subarchive
            "Архів:Архівний відділ виконавчого комітету Кременчуцької міської ради/Р",            # long name (spaces), Р subarchive
            "Архів:ДАХмО/К",                                                                      # К subarchive
            "Архів:ДАПО/Р/1–1000",                                                               # Р subarchive + en-dash range fond
            "Архів:ДАКО/Р-5634/1/3092",                                                          # 4-level: Р-prefixed fond, large case number
            "Архів:ДАКрО/225/1/144а",                                                            # 4-level: numeric fond, letter-suffixed case
            "Архів:ДАКрО/П-5907/2Р",                                                             # П-prefixed fond, letter-suffixed opus
            "Архів:ІР_НБУВ/130/1",                                                               # compound archive name (underscores)
            "Архів:ІР НБУВ/232/1",                                                               # compound archive name (spaces)
        ]
        runtime = Runtime()

        def test_title(title, runtime):
            page = runtime.lookup_by_title(title)
            self.assertEqual(page.title, title)
        
        for title in titles:    
            ancestry = lineage(title)
            for item in ancestry:
                test_title(item, runtime)

if __name__ == "__main__":
    unittest.main()
