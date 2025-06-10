import os
import unittest
from unittest.mock import patch

from io import BytesIO
import openpyxl

from birddog.service import app
from birddog.wiki import ARCHIVES

# ------------------ WIKI UNIT TESTS ------------------ 

archive_master_list = [[arc, sub['subarchive']['en']] for arc, archive in ARCHIVES.items() for sub in archive.values()]
test_email = "birddog_test_user@example.com"
test_name = "Birddog Test User"

class Test(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.secret_key = 'test_secret'
        self.client = app.test_client()

        # Setup session manually
        with self.client.session_transaction() as sess:
            sess['user'] = {
                'email': test_email,
                'name': test_name
            }

    def test_archives(self):
        response = self.client.get("/archives")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/json")
        data = response.get_json()
        self.assertIsInstance(data, list)
        for item in data:
            self.assertIsInstance(item, list)
            self.assertTrue(len(item) == 2)
            self.assertTrue(item in archive_master_list)
        for item in archive_master_list:
            self.assertTrue(item in data)


    # need this patch construct to test authenticated endpoints
    @patch("birddog.service.users.lookup")
    def test_page(self, mock_lookup):
        # Simulate a valid user being found
        mock_lookup.return_value = {"email": test_email, "name": test_name}

        page_keys = set([
            'archive', 'case', 'children', 'description', 'doc_link', 'fond', 'header', 
            'history', 'kind', 'lastmod', 'link', 'name', 'needs_translation', 'opus', 
            'subarchive', 'title'])

        def _load_page_url(url, keys):
            print("Testing URL:", url)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, "application/json")
            data = response.get_json()
            self.assertIsInstance(data, dict)
            self.assertEqual(set(data.keys()), keys)

        address = [ "DAK", "D", "6", "1", "38"]
        for i in range(1, len(address)):
            url = f"/page/{'/'.join(address[:i])}"
            _load_page_url(url, page_keys)
            _load_page_url(url + "?compare=2023,12,31", page_keys | {"refmod"})

    @patch("birddog.service.users.lookup")
    def test_download(self, mock_lookup):
        # Simulate a valid user being found
        mock_lookup.return_value = {"email": test_email, "name": test_name}

        def _download_page_url(url):
            print("Testing Download:", url)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            # Check file download headers
            content_disp = response.headers.get("Content-Disposition")
            self.assertTrue(content_disp.startswith("attachment;"))

            wb = openpyxl.load_workbook(filename=BytesIO(response.data))
            ws = wb.active
            first_cell = ws.cell(row=1, column=1).value
            print("First table cell:", first_cell)
            wb.close()
            self.assertGreater(len(response.data), 100)

        address = [ "DAKIRO", "R", "Р-285", "2", "20"]
        for i in range(1, len(address)):
            url = f"/download/{'/'.join(address[:i])}"
            _download_page_url(url)
            _download_page_url(url + "?compare=2023,12,31")

if __name__ == "__main__":
    unittest.main()
