#
#
#
import os
import unittest
import filecmp
from birddog.core import Archive, Fond, Opus
from birddog.excel import export_page

# ------------------ EXCEL UNIT TESTS ------------------ 
class Test(unittest.TestCase):
    def test_export(self):
        for fname in ['unittest_DAZHO', 'unittest_DAZHO_1', 'unittest_DAZHO_1_74']:
            path = f'var/{fname}.xlsx'
            if os.path.isfile(path):
                os.remove(path)
        archive = Archive('DAZHO')
        export_page(archive, 'var/unittest_DAZHO.xlsx')
        fond1 = archive.lookup('1')
        export_page(fond1, 'var/unittest_DAZHO_1.xlsx')
        opus74 = fond1.lookup('74')
        export_page(opus74, 'var/unittest_DAZHO_1_74.xlsx')
        for fname in ['unittest_DAZHO', 'unittest_DAZHO_1', 'unittest_DAZHO_1_74']:
            with open(f'var/{fname}.xlsx', 'rb') as file:
                buffer1 = file.read()
            with open(f'resources/unittest/{fname}.xlsx', 'rb') as file:
                buffer2 = file.read()
            self.assertTrue(len(buffer1) == len(buffer2))
            # excel file contents will differ unfortunately

if __name__ == "__main__":
    unittest.main()
