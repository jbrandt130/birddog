import os
from copy import copy
import unittest

from birddog.wiki import (
    ARCHIVE_BASE,
    ARCHIVES,
    SUBARCHIVES,
    all_archives,
    sniff_subarchives,
    check_page_updates,
    check_page_changes,
    report_page_changes,
    mw_read_page
    )

from birddog.utility import (
    get_text,
    )

from birddog.core import (
    Archive,
    )

# ------------------ WIKI UNIT TESTS ------------------ 

class Test(unittest.TestCase):
    def test_read_page(self):
        page = mw_read_page("ДААРК")
        print(get_text(page['title']), get_text(page['description']))
        self.assertTrue(set(page.keys()) == set([
            'title', 
            'template', 
            'revid', 
            'description', 
            'dates', 
            'notes', 
            'other_links', 
            'header', 
            'children', 
            'lastmod', 
            'link', 
            'doc_link']))

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

    """
    def test_all_archives(self):
        print("Checking opening all archives")
        for item in all_archives():
            print(item)
            archive = Archive(item[0], item[1])
            if archive.children is None:
                print("Children is None:", item[0], item[1])
            print(f'{item[0]}-{item[1]}: {archive.name}, #children={len(archive.children)}')
    """
if __name__ == "__main__":
    unittest.main()
