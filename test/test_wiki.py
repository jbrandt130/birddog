import os
from copy import copy
import unittest

from birddog.wiki import (
    ARCHIVE_LIST,
    ARCHIVE_BASE,
    ARCHIVES,
    SUBARCHIVES,
    find_subarchives,
    wiki_title,
    read_page,
    check_page_updates,
    check_page_changes,
    report_page_changes,
    )

from birddog.utility import (
    get_text,
    )

from birddog.core import (
    Archive,
    )

# ------------------ WIKI UNIT TESTS ------------------ 

def _page_url(archive, sub=None):
    url = f'{ARCHIVE_BASE}/wiki/{wiki_title("ДААРК")}'
    if sub:
        url += f'/{sub}'
    return url

class Test(unittest.TestCase):
    def test_read_page(self):
        page = read_page(_page_url("ДААРК"))
        print(get_text(page['title']), get_text(page['description']))
        self.assertTrue(set(page.keys()) == set(['title', 'description', 'header', 'children', 'lastmod', 'link', 'doc_link', 'thumb_link']))

    def test_update_check(self):
        archive = Archive("DACHGO", subarchive="D")
        print(archive.title)
        updates = check_page_updates(archive, cutoff_date='2024,12,31')
        for item in updates:
            print(f'   {item}: {updates[item]}')
        archive = Archive("DACHGO", subarchive="R")
        updates = check_page_updates(archive, cutoff_date='2024,12,31')
        for item in updates:
            print(f'   {item}: {updates[item]}')

    def test_change_check(self):
        pass
    
    def test_sniffer(self):
        for archive in ARCHIVE_LIST.values():
            subarchives = find_subarchives(archive)
            print(f'{archive}:')
            #self.assertTrue(ARCHIVES[archive] == subarchives)
            for key, value in subarchives.items():
                print(f'    {key}: {get_text(value['title'])} ({get_text(value['description'])})')

if __name__ == "__main__":
    unittest.main()
