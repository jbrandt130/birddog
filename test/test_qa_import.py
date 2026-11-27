import datetime
import os
import sys
import unittest
from collections import defaultdict

from openpyxl import Workbook

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utility"))

import qa_import  # noqa: E402

PAGE = "https://uk.wikisource.org/wiki/Архів:ДАЧкО/1/1"
FILE = "https://commons.wikimedia.org/wiki/File:ДАЧкО_1-1-1.pdf"


def _identity_index():
    index = qa_import.Index(lambda url: url)
    index.docs_by_id = {}
    return index


def _opus_sheet():
    ws = Workbook().active
    ws.title = "DACHKO 1-1"
    ws["A2"] = "Police department"
    ws["D3"] = PAGE
    ws["D3"].hyperlink = PAGE
    headers = ["Case #, link", "Case description, file link", "Date(s)", "Doc type", "Content",
               "Process", "Comments", "Priority file name", "Processor", "Pages processed",
               "Other file name", "Processor", "Pages processed", "When transcribed",
               "Extra language(s)"]
    for c, h in enumerate(headers, 1):
        ws.cell(6, c, h)
    # row 7: a case with a file and values
    ws["A7"] = 1.0
    ws["A7"].hyperlink = PAGE + "/1"
    ws["B7"] = "Lists"
    ws["B7"].hyperlink = FILE
    ws["C7"] = 1865.0
    ws["D7"] = "O/M"
    ws["F7"] = "c1"
    ws["G7"] = "[JB] note"
    ws["N7"] = datetime.datetime(2026, 3, 1)
    ws["N7"].number_format = "mmm yyyy"
    # row 8: a red-linked case page without a file
    ws["A8"] = 2.0
    ws["A8"].hyperlink = "https://uk.wikisource.org/w/index.php?title=Архів:ДАЧкО/1/1/2&action=edit&redlink=1"
    ws["B8"] = "Orders"
    ws["D8"] = "L"
    # row 9: no link at all
    ws["A9"] = 3.0
    ws["B9"] = "Family Search 123"
    ws["A10"] = 3
    ws["B10"] = "# cases listed"
    ws["A11"] = 99.0  # past the totals row: ignored
    return ws


class TestHelpers(unittest.TestCase):
    def test_displayed_date(self):
        d = datetime.datetime(2025, 9, 1)
        self.assertEqual(qa_import.displayed_date(d, "mmm yyyy"), "Sep 2025")
        self.assertEqual(qa_import.displayed_date(d, "d mmm yyyy"), "1 Sep 2025")
        self.assertEqual(qa_import.displayed_date(d, "dd.mm.yyyy"), "01.09.2025")

    def test_wiki_url(self):
        self.assertEqual(
            qa_import.wiki_url("https://uk.wikisource.org/w/index.php?title=X/1&action=edit&redlink=1"),
            "https://uk.wikisource.org/wiki/X/1")
        self.assertEqual(qa_import.wiki_url("https://c.org/wiki/File:A_(18??).pdf"),
                         "https://c.org/wiki/File:A_(18%3F%3F).pdf")
        self.assertEqual(
            qa_import.wiki_url("https://upload.wikimedia.org/wikipedia/commons/d/d2/A_b.pdf"),
            "https://commons.wikimedia.org/wiki/File:A_b.pdf")

    def test_codes_treat_m_as_v(self):
        self.assertEqual(qa_import._codes("O/M"), ["O", "V"])
        self.assertEqual(qa_import._codes(["V", "O"]), ["O", "V"])


class TestOpusSheet(unittest.TestCase):
    def _run(self, index):
        ws = _opus_sheet()
        f = qa_import.Findings()
        claimed, claimed_docs = defaultdict(list), defaultdict(list)
        qa_import.check_header(ws, index, f)
        qa_import.check_opus_sheet(ws, 6, index, f, claimed, claimed_docs)
        return {(r["finding"], r["cell"]) for r in f.rows}, f

    def test_findings(self):
        index = _identity_index()
        index.add_page({"url": PAGE, "description": "Police department", "doc_links": []})
        index.add_page({"url": PAGE + "/1", "description": "Lists", "years": "1865", "comments": "[JB] note"})
        index.add_page({"url": "https://uk.wikisource.org/wiki/Архів:ДАЧкО/1/1/2?action=edit&redlink=1",
                        "description": "Orders", "years": ""})
        index.add_doc({"url": FILE, "OLD_doc_type": ["O", "V"], "OLD_process_code": "C1",
                       "comments": "[JB] note", "when_transcribed": "2026-03-01 00:00:00"})
        found, f = self._run(index)
        self.assertEqual(found, {
            ("N stored as a date, not as shown", "N7"),
            ("D..O values not stored (no document)", "A8"),
            ("row not imported (no link)", "A9"),
        })
        n7 = next(r for r in f.rows if r["cell"] == "N7")
        self.assertEqual(n7["expected"], "Mar 2026")

    def test_a2_checked_against_old_description(self):
        index = _identity_index()
        index.add_page({"url": PAGE, "description": "Police", "doc_links": [],
                        "OLD_description": "Police department\n[parent list] Police"})
        found, _ = self._run(index)
        self.assertNotIn(("A2 description not stored", "A2"), found)
        index.add_page({"url": PAGE, "description": "Police department", "doc_links": [],
                        "OLD_description": "Police"})
        found, _ = self._run(index)
        self.assertIn(("A2 description not stored", "A2"), found)

    def test_missing_sheet_page_and_rows(self):
        found, _ = self._run(_identity_index())
        self.assertIn(("sheet page missing", "D3"), found)
        self.assertIn(("row not imported", "A7"), found)
        self.assertNotIn(("row not imported", "A11"), found)


if __name__ == "__main__":
    unittest.main()
