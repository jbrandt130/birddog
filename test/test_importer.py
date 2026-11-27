import datetime
import unittest

from birddog.database_importer import (
    add_page,
    canonical_wiki_url,
    check_redlink,
    commons_file_url_from_upload,
    displayed_date,
    get_cell_display_text,
    merge_descriptions,
    doc_link_availability,
    get_doc_links,
    is_totals_row,
    is_url_redlinked,
    title_from_transliterated_link,
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
        self.assertEqual(map_old_process_code("C2"), "postpone")
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
                         {"doc_type": ["Vitals"], "process_code": "duplicate", "index_process_type": None})

    def test_blank_results_clear_new_fields(self):
        payload = {"OLD_doc_type": "C", "OLD_process_code": "E"}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": None, "process_code": None, "index_process_type": None})

    def test_c3_is_save_with_anomaly(self):
        payload = {"OLD_doc_type": "V", "OLD_process_code": " c3\t"}
        self.assertEqual(new_codes_from_old(payload), {
            "doc_type": ["Vitals"],
            "process_code": "save",
            "index_process_type": None,
            "anomaly": True,
            "anomaly_comment": "formerly C3 process code",
        })

    def test_other_codes_leave_anomaly_alone(self):
        for code in ("P1", "E", None):
            self.assertNotIn("anomaly", new_codes_from_old({"OLD_process_code": code}), code)

    def test_empty_old_values_clear_new_fields(self):
        payload = {"OLD_doc_type": None, "OLD_process_code": None}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": None, "process_code": None, "index_process_type": None})

    def test_save_with_when_transcribed_is_externally_indexed(self):
        for code in ("P1", "P2", "C3"):
            payload = {"OLD_process_code": code, "when_transcribed": "2019"}
            self.assertEqual(new_codes_from_old(payload)["index_process_type"], "externally_indexed", code)

    def test_save_without_when_transcribed_leaves_index_process_type_blank(self):
        for transcribed in (None, "", "  "):
            payload = {"OLD_process_code": "P1", "when_transcribed": transcribed}
            self.assertIsNone(new_codes_from_old(payload)["index_process_type"], transcribed)
        self.assertIsNone(new_codes_from_old({"OLD_process_code": "P1"})["index_process_type"])

    def test_when_transcribed_only_counts_for_save(self):
        for code in ("C1", "DP", "NO", "FX", "E", None):
            payload = {"OLD_process_code": code, "when_transcribed": "2019"}
            self.assertIsNone(new_codes_from_old(payload)["index_process_type"], code)

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


class TestCheckRedlinkUnlinkedComment(unittest.TestCase):
    """A row commented "unlinked" with no process code is deliberately pageless."""

    def _page(self, comments, process_code=None, url=f"{_CASE}?{_RED}"):
        return {"title": "t", "url": url, "comments": comments, "OLD_process_code": process_code}

    def test_unlinked_comment_overrides_redlink(self):
        page = check_redlink(self._page("unlinked"))
        self.assertEqual(page["availability"], "unlinked")
        self.assertEqual(page["url"], _CASE)

    def test_unlinked_comment_is_case_and_space_insensitive(self):
        self.assertEqual(check_redlink(self._page(" Unlinked "))["availability"], "unlinked")

    def test_process_code_keeps_redlinked(self):
        self.assertEqual(check_redlink(self._page("unlinked", "S"))["availability"], "redlinked")

    def test_unlinked_with_a_reason_counts(self):
        for comment in ("unlinked - unnecessary description file", "unnecessary description file - unlinked",
                        "unlinked, unnecessary description file", "unlinked [JB] assuming Jewish"):
            self.assertEqual(check_redlink(self._page(comment))["availability"], "unlinked", comment)

    def test_comment_merely_mentioning_unlinked_does_not_count(self):
        for comment in ("linked to page with 2 unlinked opuses", "has unlinked siblings"):
            self.assertEqual(check_redlink(self._page(comment))["availability"], "redlinked", comment)

    def test_unlinked_at_the_end_counts(self):
        for comment in ("unnecessary; unlinked", "unnecessary, unlinked.", "unlinked. [JB] assuming non-Jewish"):
            self.assertEqual(check_redlink(self._page(comment))["availability"], "unlinked", comment)

    def test_other_comments_keep_redlinked(self):
        for comment in (None, "", "unnecessary description file", "File link is no good", "relinked later"):
            self.assertEqual(check_redlink(self._page(comment))["availability"], "redlinked", comment)


class _C:
    def __init__(self, value):
        self.value = value


class TestIsTotalsRow(unittest.TestCase):
    def test_formula_text(self):
        self.assertTrue(is_totals_row(_C("=ROWS(A8)"), _C(None)))

    def test_cached_count_with_label(self):
        # data_only=True gives the formula's cached number; the label marks the row
        for label in ("# opuses listed", "# cases listed", " # funds listed"):
            self.assertTrue(is_totals_row(_C(3), _C(label)))

    def test_ordinary_rows(self):
        self.assertFalse(is_totals_row(_C(1.0), _C("Audit tales (1834, 1850)")))
        self.assertFalse(is_totals_row(_C(None), _C(None)))
        self.assertFalse(is_totals_row(_C(2), None))


class TestTitleFromTransliteratedLink(unittest.TestCase):
    URL = f"{_WS}/wiki/Архів:ДАЧкО/Р-5899/1/177а"

    def test_latin_cell_takes_cyrillic_link_title(self):
        self.assertEqual(title_from_transliterated_link("ДАЧкО/Р-5899/1/177a", self.URL), ("ДАЧкО/Р-5899/1/177а", ""))
        self.assertEqual(title_from_transliterated_link("ДАЧкО/Р-5899/1/177a", f"{self.URL}?{_RED}"),
                         ("ДАЧкО/Р-5899/1/177а", ""))
        self.assertEqual(title_from_transliterated_link("ДАЧкО/835/1/13zh", f"{_WS}/wiki/Архів:ДАЧкО/835/1/13ж"),
                         ("ДАЧкО/835/1/13ж", ""))

    def test_alphabet_position_letters_take_link_title_with_a_note(self):
        title, note = title_from_transliterated_link("ДАЧкО/550/2/1c", f"{_WS}/wiki/Архів:ДАЧкО/550/2/1в")
        self.assertEqual(title, "ДАЧкО/550/2/1в")
        self.assertIn("1c taken as 1в", note)

    def test_different_case_number_keeps_cell_title(self):
        self.assertEqual(title_from_transliterated_link("ДАЧкО/Р-5899/1/178a", self.URL), ("ДАЧкО/Р-5899/1/178a", ""))
        self.assertEqual(title_from_transliterated_link("ДАЧкО/Р-5899/1/177", self.URL), ("ДАЧкО/Р-5899/1/177", ""))

    def test_no_link_or_same_title(self):
        self.assertEqual(title_from_transliterated_link("ДАЧкО/1/1/40", ""), ("ДАЧкО/1/1/40", ""))
        self.assertEqual(title_from_transliterated_link("ДАЧкО/Р-5899/1/177а", self.URL), ("ДАЧкО/Р-5899/1/177а", ""))


class TestCommonsFileUrlFromUpload(unittest.TestCase):
    PAGE = "https://commons.wikimedia.org/wiki/File:ДАЧкО_92-1-32_Те_саме_(1914-1916).pdf"

    def test_upload_url(self):
        url = "https://upload.wikimedia.org/wikipedia/commons/d/d2/ДАЧкО_92-1-32_Те_саме_(1914-1916).pdf"
        self.assertEqual(commons_file_url_from_upload(url), self.PAGE)

    def test_thumbnail_and_encoded(self):
        url = ("https://upload.wikimedia.org/wikipedia/commons/thumb/d/d2/"
               "%D0%94%D0%90%D0%A7%D0%BA%D0%9E_92-1-32_%D0%A2%D0%B5_%D1%81%D0%B0%D0%BC%D0%B5_(1914-1916).pdf/"
               "page1-120px-x.jpg")
        self.assertEqual(commons_file_url_from_upload(url), self.PAGE)

    def test_other_urls_unchanged(self):
        for url in ("https://commons.wikimedia.org/wiki/File:X.pdf", "http://k/",
                    "https://upload.wikimedia.org/wikipedia/uk/a/ab/X.pdf"):
            self.assertEqual(commons_file_url_from_upload(url), url)

    def test_get_doc_links_normalizes(self):
        url = "https://upload.wikimedia.org/wikipedia/commons/d/d2/ДАЧкО_92-1-32_Те_саме_(1914-1916).pdf"
        _, links = get_doc_links(_Sheet({"B38": _Cell(url, "desc")}), "B38", "", _CASE)
        self.assertEqual(links, [self.PAGE])


class TestCaseSelfLinkNote(unittest.TestCase):
    def test_case_row_linking_its_page_is_noted(self):
        ws = _Sheet({"B7": _Cell(_CASE, "desc")})
        message, links = get_doc_links(ws, "B7", "", _CASE, note_self_link=True)
        self.assertIn("is the page itself, not a file", message)
        self.assertEqual(links, [_CASE])

    def test_opus_row_self_link_is_not_noted(self):
        ws = _Sheet({"B7": _Cell(_CASE, "desc")})
        with self.assertNoLogs("birddog.log", level="WARNING"):
            message, _ = get_doc_links(ws, "B7", "", _CASE)
        self.assertEqual(message, "")


if __name__ == "__main__":
    unittest.main()


class TestDisplayedDate(unittest.TestCase):
    def test_formats(self):
        d = datetime.datetime(2026, 3, 1)
        self.assertEqual(displayed_date(d, "mmm yyyy"), "Mar 2026")
        self.assertEqual(displayed_date(d, "d mmm yyyy"), "1 Mar 2026")
        self.assertEqual(displayed_date(d, "dd.mm.yyyy"), "01.03.2026")
        self.assertEqual(displayed_date(d, "mmmm yyyy"), "March 2026")
        self.assertEqual(displayed_date(d, "General"), "2026-03-01")
        self.assertEqual(displayed_date(d, "yyyy-mm-dd h:mm"), "2026-03-01")

    def test_cell_display_text(self):
        class Cell:
            def __init__(self, value, number_format="General"):
                self.value, self.number_format = value, number_format
        self.assertEqual(get_cell_display_text(Cell(datetime.datetime(2025, 9, 1), "mmm yyyy")), "Sep 2025")
        self.assertEqual(get_cell_display_text(Cell("Apr 2025")), "Apr 2025")
        self.assertEqual(get_cell_display_text(Cell(2019.0)), "2019")
        self.assertEqual(get_cell_display_text(Cell(None)), "")


class TestSheetDescription(unittest.TestCase):
    def _page(self, description):
        return {"title": "ДАЧкО/5", "url": f"{_WS}/wiki/Архів:ДАЧкО/5", "label": "DACHKO/5",
                "description": description, "comments": "", "doc_links": [], "import_message": ""}

    def test_merge_descriptions(self):
        self.assertEqual(merge_descriptions("A", None), "A")
        self.assertEqual(merge_descriptions("A  b", "A b"), "A  b")
        self.assertEqual(merge_descriptions("A", "B"), "A\n[parent list] B")

    def test_sheet_a2_wins_and_old_description_keeps_both(self):
        table = []
        add_page(self._page("Civil registration books of Cherkasy region, Zolotonosha city"), table, False)
        self.assertEqual(table[0]["OLD_description"],
                         "Civil registration books of Cherkasy region, Zolotonosha city")
        add_page(self._page("Civil registration books, Zolotonosha"), table, True)
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]["description"], "Civil registration books, Zolotonosha")
        self.assertEqual(table[0]["OLD_description"],
                         "Civil registration books, Zolotonosha\n"
                         "[parent list] Civil registration books of Cherkasy region, Zolotonosha city")

    def test_same_description_is_not_repeated(self):
        table = []
        add_page(self._page("Uman council"), table, False)
        add_page(self._page("Uman council"), table, True)
        self.assertEqual(table[0]["OLD_description"], "Uman council")
