#
#
#
import os
import unittest
import filecmp
from copy import copy
from birddog.core import Archive, Fond, Opus
from birddog.wiki import check_page_changes
from birddog.excel import export_page

UNITTEST_RESOURCE_DIR = 'test/resources'

# ------------------ EXCEL UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_export(self):
        for fname in ['unittest_DAZHO', 'unittest_DAZHO_1', 'unittest_DAZHO_1_74']:
            path = f'var/{fname}.xlsx'
            if os.path.isfile(path):
                os.remove(path)
        archive = Archive('DAZHO')
        archive.prepare_to_download()
        export_page(archive, 'var/unittest_DAZHO.xlsx')
        fond1 = archive.lookup('1')
        fond1.prepare_to_download()
        export_page(fond1, 'var/unittest_DAZHO_1.xlsx')
        opus74 = fond1.lookup('74')
        opus74.prepare_to_download()
        export_page(opus74, 'var/unittest_DAZHO_1_74.xlsx')
        for fname in ['unittest_DAZHO', 'unittest_DAZHO_1', 'unittest_DAZHO_1_74']:
            with open(f'var/{fname}.xlsx', 'rb') as file:
                buffer1 = file.read()
            with open(f'{UNITTEST_RESOURCE_DIR}/{fname}.xlsx', 'rb') as file:
                buffer2 = file.read()
            #self.assertTrue(len(buffer1) == len(buffer2))
            # excel file contents will differ unfortunately
        
        # test difference reporting
        opus74_copy = copy(opus74)
        opus74.revert_to('2024')
        opus74_copy.revert_to('2023')
        opus74.prepare_to_download()
        check_page_changes(opus74, opus74_copy)
        fname = 'unittest_DAZHO_1_74_2024_2023'
        export_page(opus74, f'var/{fname}.xlsx')
        with open(f'var/{fname}.xlsx', 'rb') as file:
            buffer1 = file.read()
        with open(f'{UNITTEST_RESOURCE_DIR}/{fname}.xlsx', 'rb') as file:
            buffer2 = file.read()
        #self.assertTrue(len(buffer1) == len(buffer2))

if __name__ == "__main__":
    unittest.main()
