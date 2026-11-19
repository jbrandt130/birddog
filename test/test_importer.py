import unittest

from birddog.database_importer import (
    add_page,
    canonical_wiki_url,
    doc_link_availability,
    get_doc_links,
    is_url_redlinked,
    map_old_doc_type,
    map_old_process_code,
    new_codes_from_old,
)


class TestMapOldDocType(unittest.TestCase):
    def test_direct_mappings(self):
        self.assertEqual(map_old_doc_type("V"), ["Vitals"])
        self.assertEqual(map_old_doc_type("P"), ["Residence & Identity"])
        self.assertEqual(map_old_doc_type("O"), ["Other"])

    def test_obsolete_m_is_vitals(self):
        self.assertEqual(map_old_doc_type("M"), ["Vitals"])

    def test_c_and_l_are_blank(self):
        self.assertEqual(map_old_doc_type("C"), [])
        self.assertEqual(map_old_doc_type("L"), [])

    def test_unrecognized_and_empty_are_blank(self):
        self.assertEqual(map_old_doc_type("X"), [])
        self.assertEqual(map_old_doc_type(""), [])
        self.assertEqual(map_old_doc_type(None), [])

    def test_multiple_codes(self):
        self.assertEqual(map_old_doc_type("V/C/P"), ["Vitals", "Residence & Identity"])
        self.assertEqual(map_old_doc_type(["O", "L"]), ["Other"])
        self.assertEqual(map_old_doc_type("V, M"), ["Vitals"])

    def test_case_and_whitespace(self):
        self.assertEqual(map_old_doc_type(" v\t"), ["Vitals"])


class TestMapOldProcessCode(unittest.TestCase):
    def test_mappings(self):
        self.assertEqual(map_old_process_code("C1"), "check")
        self.assertEqual(map_old_process_code("C2"), "check")
        self.assertEqual(map_old_process_code("P1"), "save")
        self.assertEqual(map_old_process_code("P2"), "save")
        self.assertEqual(map_old_process_code("DP"), "duplicate")
        self.assertEqual(map_old_process_code("NO"), "reject")
        self.assertEqual(map_old_process_code("C3"), "save")
        self.assertEqual(map_old_process_code("FX"), "defer")

    def test_blank_codes(self):
        for code in ("E", "ZZ", "", None):
            self.assertIsNone(map_old_process_code(code), code)

    def test_case_and_whitespace(self):
        self.assertEqual(map_old_process_code(" p1\t"), "save")


class TestNewCodesFromOld(unittest.TestCase):
    def test_both_mapped(self):
        payload = {"OLD_doc_type": "V", "OLD_process_code": "DP"}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": ["Vitals"], "process_code": "duplicate", "save_status": None})

    def test_blank_results_clear_new_fields(self):
        payload = {"OLD_doc_type": "C", "OLD_process_code": "E"}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": None, "process_code": None, "save_status": None})

    def test_c3_is_save_with_anomaly(self):
        payload = {"OLD_doc_type": "V", "OLD_process_code": " c3\t"}
        self.assertEqual(new_codes_from_old(payload), {
            "doc_type": ["Vitals"],
            "process_code": "save",
            "save_status": None,
            "anomaly": True,
            "anomaly_comment": "formerly C3 process code",
        })

    def test_other_codes_leave_anomaly_alone(self):
        for code in ("P1", "E", None):
            self.assertNotIn("anomaly", new_codes_from_old({"OLD_process_code": code}), code)

    def test_empty_old_values_clear_new_fields(self):
        payload = {"OLD_doc_type": None, "OLD_process_code": None}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": None, "process_code": None, "save_status": None})

    def test_save_with_when_transcribed_is_externally_indexed(self):
        for code in ("P1", "P2", "C3"):
            payload = {"OLD_process_code": code, "when_transcribed": "2019"}
            self.assertEqual(new_codes_from_old(payload)["save_status"], "externally_indexed", code)

    def test_save_without_when_transcribed_leaves_save_status_blank(self):
        for transcribed in (None, "", "  "):
            payload = {"OLD_process_code": "P1", "when_transcribed": transcribed}
            self.assertIsNone(new_codes_from_old(payload)["save_status"], transcribed)
        self.assertIsNone(new_codes_from_old({"OLD_process_code": "P1"})["save_status"])

    def test_when_transcribed_only_counts_for_save(self):
        for code in ("C1", "DP", "NO", "FX", "E", None):
            payload = {"OLD_process_code": code, "when_transcribed": "2019"}
            self.assertIsNone(new_codes_from_old(payload)["save_status"], code)

    def test_missing_old_fields(self):
        self.assertEqual(new_codes_from_old({"url": "https://x"}), {})


_RED = "action=edit&redlink=1"
_WS = "https://uk.wikisource.org"
_FILE = "https://commons.wikimedia.org/wiki/File:DACHKO_R-5899-12-45_test_(1935).pdf"
_CASE = f"{_WS}/wiki/Архів:ДАЧкО/Р-5899/12/45"


class _Hyperlink:
    def __init__(self, target):
        self.target = target


class _Cell:
    def __init__(self, link=None, value=None):
        self.hyperlink = _Hyperlink(link) if link else None
        self.value = value


class _Sheet:
    title = "DACHKO R-5899-12"

    def __init__(self, cells):
        self._cells = cells

    def __getitem__(self, addr):
        return self._cells.get(addr, _Cell())


class TestCanonicalWikiUrlRedlink(unittest.TestCase):
    def test_index_php_redlink_keeps_marker(self):
        url = canonical_wiki_url(f"{_WS}/w/index.php?title=Архів:ДАЧкО/1/1/40&{_RED}")
        self.assertEqual(url, f"{_WS}/wiki/Архів:ДАЧкО/1/1/40?{_RED}")
        self.assertEqual(is_url_redlinked(url), (True, f"{_WS}/wiki/Архів:ДАЧкО/1/1/40"))

    def test_index_php_without_redlink_has_no_query(self):
        url = canonical_wiki_url(f"{_WS}/w/index.php?title=Архів:ДАЧкО/1/1/40")
        self.assertEqual(url, f"{_WS}/wiki/Архів:ДАЧкО/1/1/40")

    def test_wiki_url_is_unchanged(self):
        url = f"{_FILE}?{_RED}"
        self.assertEqual(canonical_wiki_url(url), url)


class TestDocLinkAvailability(unittest.TestCase):
    def test_redlinked_file_is_redlinked_on_a_linked_page(self):
        self.assertEqual(doc_link_availability(f"{_FILE}?{_RED}", _CASE, "linked"), (_FILE, "redlinked"))

    def test_linked_file_inherits_page_availability(self):
        self.assertEqual(doc_link_availability(_FILE, _CASE, "linked"), (_FILE, "linked"))
        self.assertEqual(doc_link_availability(_FILE, _CASE, "unlinked"), (_FILE, "unlinked"))

    def test_linked_file_on_redlinked_page_is_linked(self):
        self.assertEqual(doc_link_availability(_FILE, _CASE, "redlinked"), (_FILE, "linked"))

    def test_self_link_of_redlinked_page_stays_redlinked(self):
        self.assertEqual(doc_link_availability(_CASE, _CASE, "redlinked"), (_CASE, "redlinked"))
        self.assertEqual(doc_link_availability(f"{_CASE}?{_RED}", _CASE, "redlinked"), (_CASE, "redlinked"))


class TestGetDocLinksRedlink(unittest.TestCase):
    def test_redlinked_file_is_not_flagged(self):
        ws = _Sheet({"B51": _Cell(f"{_FILE}?{_RED}", "desc")})
        with self.assertNoLogs("birddog.log", level="WARNING"):
            message, links = get_doc_links(ws, "B51", "", _CASE)
        self.assertEqual(message, "")
        self.assertEqual(links, [f"{_FILE}?{_RED}"])

    def test_non_document_link_is_still_flagged(self):
        ws = _Sheet({"B9": _Cell("http://k/", "desc")})
        with self.assertLogs("birddog.log", level="WARNING"):
            message, links = get_doc_links(ws, "B9", "", _CASE)
        self.assertIn("differs from the page url", message)

    def test_redlinked_self_link_is_not_flagged(self):
        ws = _Sheet({"B7": _Cell(f"{_CASE}?{_RED}", "desc")})
        with self.assertNoLogs("birddog.log", level="WARNING"):
            message, links = get_doc_links(ws, "B7", "", f"{_CASE}?{_RED}")
        self.assertEqual(message, "")


class TestAddPageRedlink(unittest.TestCase):
    def _page(self, url, parent_url):
        return {"title": "t", "url": url, "label": "DACHKO/1/1/40", "parent_url": parent_url}

    def test_index_php_redlinked_page_is_redlinked_with_bare_parent(self):
        table = []
        add_page(self._page(
            canonical_wiki_url(f"{_WS}/w/index.php?title=Архів:ДАЧкО/1/1/40&{_RED}"),
            canonical_wiki_url(f"{_WS}/w/index.php?title=Архів:ДАЧкО/1/1&{_RED}")), table, False)
        self.assertEqual(table[0]["availability"], "redlinked")
        self.assertEqual(table[0]["url"], f"{_WS}/wiki/Архів:ДАЧкО/1/1/40")
        self.assertEqual(table[0]["parent_url"], f"{_WS}/wiki/Архів:ДАЧкО/1/1")

    def test_linked_page_stays_linked(self):
        table = []
        add_page(self._page(f"{_WS}/wiki/Архів:ДАЧкО/1/1/40", f"{_WS}/wiki/Архів:ДАЧкО/1/1"), table, False)
        self.assertEqual(table[0]["availability"], "linked")
        self.assertEqual(table[0]["parent_url"], f"{_WS}/wiki/Архів:ДАЧкО/1/1")


if __name__ == "__main__":
    unittest.main()
