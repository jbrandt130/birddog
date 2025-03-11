import os
from copy import copy
import unittest
from birddog.utility import ARCHIVE_BASE
from birddog.core import (
    read_page,
    do_search,
    get_page_history,
    report_page_changes,
    check_page_changes,
    Archive,
    Fond,
    Opus,
    Case,
    )

archive_path = '%D0%90%D1%80%D1%85%D1%96%D0%B2:%D0%94%D0%90%D0%96%D0%9E'
fond_id = '1'
opus_id = '74'
case_id = '1'

# ------------------ UTILITY UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_Archive(self):
        page = Archive('DAZHO')
        print('base', page.base)
        self.assertTrue(page.base, ARCHIVE_BASE)
        print('child_class', page.child_class)
        self.assertTrue(page.child_class == Fond)
        print('default_url', page.default_url)
        self.assertTrue(
            page.default_url == ARCHIVE_BASE + '/wiki/%D0%90%D1%80%D1%85%D1%96%D0%B2%3A%D0%94%D0%90%D0%96%D0%9E/%D0%94')
        print('history_url', page.history_url)
        self.assertTrue(
            page.history_url == ARCHIVE_BASE + '/w/index.php?action=history&limit=10000&title=%D0%90%D1%80%D1%85%D1%96%D0%B2%3A%D0%94%D0%90%D0%96%D0%9E/%D0%94')
        print('id', page.id)
        self.assertTrue(page.id == 'DAZHO')
        print('kind', page.kind)
        self.assertTrue(page.kind == 'archive')
        print('name', page.name)
        self.assertTrue(page.name == 'Архів:ДАЖО/Д')
        print('refmod', page.refmod)
        self.assertTrue(page.refmod == '')
        print('report', page.report, f'{page.kind},{page.tag}/{page.subarchive},{page.lastmod}')
        self.assertTrue(page.report == f'{page.kind},{page.tag}/{page.subarchive},{page.lastmod}')
        print('subarchive', page.subarchive)
        self.assertTrue(page.subarchive == 'Д')
        print('tag', page.tag)
        self.assertTrue(page.tag == 'DAZHO')
        print('title', page.title)
        self.assertTrue(page.title == 'ДАЖО/Д')
        print('url', page.url)
        self.assertTrue(
            page.url == ARCHIVE_BASE + '/wiki/%D0%90%D1%80%D1%85%D1%96%D0%B2%3A%D0%94%D0%90%D0%96%D0%9E/%D0%94')
        
    def test_Fond(self):
        page = Archive('DAZHO').lookup(fond_id)
        print('base', page.base)
        self.assertTrue(page.base, ARCHIVE_BASE)
        print('child_class', page.child_class)
        self.assertTrue(page.child_class == Opus)
        print('default_url', page.default_url)
        self.assertTrue(
            page.default_url == f'{ARCHIVE_BASE}/wiki/{archive_path}/{fond_id}')
        print('history_url', page.history_url)
        self.assertTrue(
            page.history_url == f'{ARCHIVE_BASE}/w/index.php?action=history&limit=10000&title={archive_path}/{fond_id}')
        print('id', page.id)
        self.assertTrue(page.id == fond_id)
        print('kind', page.kind)
        self.assertTrue(page.kind == 'fond')
        print('name', page.name)
        self.assertTrue(page.name == f'Архів:ДАЖО/Д/{fond_id}')
        print('refmod', page.refmod)
        self.assertTrue(page.refmod == '')
        print('report', page.report)
        self.assertTrue(page.report == f'{page.kind},{page.name.replace(",", "")},{page.lastmod}')
        print('title', page.title)
        self.assertTrue(page.title == f'Dago/{fond_id}')
        print('url', page.url)
        self.assertTrue(page.url == page.default_url)
       
    def test_Opus(self):
        page = Archive('DAZHO').lookup(fond_id).lookup(opus_id)
        print('base', page.base)
        self.assertTrue(page.base, ARCHIVE_BASE)
        print('child_class', page.child_class)
        self.assertTrue(page.child_class == Case)
        print('default_url', page.default_url)
        self.assertTrue(page.default_url == f'{page.parent.default_url}/{opus_id}')
        print('history_url', page.history_url)
        self.assertTrue(page.history_url == f'{page.parent.history_url}/{opus_id}')
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
        self.assertTrue(page.title == f'{page.parent.title}/{opus_id}')
        print('url', page.url)
        self.assertTrue(page.url == page.default_url)

    def test_Case(self):
        page = Archive('DAZHO').lookup(fond_id).lookup(opus_id).lookup(case_id)
        print('base', page.base)
        self.assertTrue(page.base, ARCHIVE_BASE)
        print('child_class', page.child_class)
        self.assertTrue(page.child_class is None)
        print('default_url', page.default_url)
        self.assertTrue(
            page.default_url == f'{page.parent.default_url}/{case_id}')
        print('history_url', page.history_url)
        self.assertTrue(
            page.history_url == f'{page.parent.history_url}/{case_id}')
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
        self.assertTrue(page.url == page.default_url)

if __name__ == "__main__":
    unittest.main()
