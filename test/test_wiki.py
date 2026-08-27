import os
import json
import tempfile
from copy import copy
import unittest
from unittest.mock import patch, MagicMock

import mwparserfromhell

from birddog.store import SQLiteKeyValueStore

from birddog.wiki import (
    ARCHIVE_BASE,
    WIKI_NAMESPACE,
    ARCHIVE_BY_TITLE,
    ARCHIVES_BY_ROOT,
    LABELS_BY_PREFIX,
    canonicalize_title,
    get_title,
    expand_link_target,
    page_url_from_title,
    classify_page,
    is_archive,
    page_kind,
    page_label,
    parent_title,
    title_in_scope,
    title_lineage,
    title_lineage_labels,
    ROOT_HUB_TITLE,
    is_root_title,
    register_archive_root,
    archive_root_label,
    all_archive_roots,
    refresh_curated_archive_root_labels,
    deduplicate_archive_root_labels,
    mw_page_doc_url,
    mw_read_page,
    get_all_pages,
    batch_fetch_document_links,
    check_page_changes,
    report_page_changes,
    _normalize_link_title,
    _is_table,
    _extract_links,
    _subtract_links,
    _extract_colspan,
    _expand_colspan,
    _tokenize_wikitext_table_cell,
    _tokenize_wikitext_table_line,
    _parse_wikitext_table_lines,
    _parse_wikitext_table,
    _parse_wiki_text,
    _extract_table,
    _normalize_child_link_positions,
)

from birddog.utility import (
    get_text,
    form_text_item,
)

# ── Test helpers ─────────────────────────────────────────────────────────────

# Pick a sample archive title dynamically so tests are independent of specific
# archive names in the master data.
_SAMPLE_ARCHIVE_TITLE = next(iter(ARCHIVE_BY_TITLE.keys()))
# Build title strings for each hierarchy level.
_ARCHIVE_ROOT = _SAMPLE_ARCHIVE_TITLE.split("/")[0]   # e.g. "Архів:ДАІФО"
_SAMPLE_FOND  = _ARCHIVE_ROOT + "/Ф-1"
_SAMPLE_OPUS  = _ARCHIVE_ROOT + "/Ф-1/1"
_SAMPLE_CASE  = _ARCHIVE_ROOT + "/Ф-1/1/1"


def _blank_page_links():
    return {
        "commons_links": [],
        "internal_links": [],
        "external_links": [],
        "category_links": [],
    }


# ── canonicalize_title ────────────────────────────────────────────────────────

class TestCanonicalizeTitle(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(canonicalize_title(None))

    def test_empty_returns_none(self):
        self.assertIsNone(canonicalize_title(""))

    def test_bare_name_gets_namespace(self):
        t = canonicalize_title("ДАЖО")
        self.assertEqual(t, "Архів:ДАЖО")

    def test_already_namespaced_unchanged(self):
        t = canonicalize_title("Архів:ДАЖО")
        self.assertEqual(t, "Архів:ДАЖО")

    def test_strip_namespace(self):
        t = canonicalize_title("Архів:ДАЖО", include_namespace=False)
        self.assertEqual(t, "ДАЖО")

    def test_spaces_become_underscores(self):
        t = canonicalize_title("Архів ДАЖО")
        self.assertIn("_", t)

    def test_path_separator_treated_as_namespace(self):
        # "Архів/ДАЖО" uses "/" instead of ":" — should resolve to Архів:ДАЖО
        t = canonicalize_title("Архів/ДАЖО")
        self.assertEqual(t, "Архів:ДАЖО")

    def test_alias_namespace(self):
        # "Архіви" is a declared alias for "Архів"
        t = canonicalize_title("Архіви:ДАЖО")
        self.assertEqual(t, "Архів:ДАЖО")

    def test_subpath_preserved(self):
        t = canonicalize_title("Архів:ДАЖО/П-141")
        self.assertEqual(t, "Архів:ДАЖО/П-141")

    def test_strip_namespace_with_subpath(self):
        t = canonicalize_title("Архів:ДАЖО/П-141", include_namespace=False)
        self.assertEqual(t, "ДАЖО/П-141")


# ── get_title ─────────────────────────────────────────────────────────────────

class TestGetTitle(unittest.TestCase):
    def test_full_url_stripped(self):
        url = f"{ARCHIVE_BASE}/wiki/Архів:ДАЖО"
        self.assertEqual(get_title(url), "Архів:ДАЖО")

    def test_bare_title_gets_namespace(self):
        self.assertEqual(get_title("ДАЖО"), "Архів:ДАЖО")

    def test_strip_namespace(self):
        self.assertEqual(get_title("Архів:ДАЖО", include_namespace=False), "ДАЖО")

    def test_url_with_subpath(self):
        url = f"{ARCHIVE_BASE}/wiki/Архів:ДАЖО/П-141"
        self.assertEqual(get_title(url), "Архів:ДАЖО/П-141")

    def test_url_decoding(self):
        # percent-encoded "/" in path component
        url = f"{ARCHIVE_BASE}/wiki/%D0%90%D1%80%D1%85%D1%96%D0%B2:%D0%94%D0%90%D0%96%D0%9E"
        t = get_title(url)
        self.assertEqual(t, "Архів:ДАЖО")

    # File: / Файл: namespace cases ----------------------------------------

    def test_commons_file_url_returns_file_title(self):
        # Bug fix: domain was not stripped for non-archive wikis, producing
        # "https://commons.wikimedia.orgFile:..." instead of "File:..."
        url = "https://commons.wikimedia.org/wiki/File:scan.pdf"
        self.assertEqual(get_title(url), "File:scan.pdf")

    def test_commons_file_url_percent_encoded(self):
        url = "https://commons.wikimedia.org/wiki/File:%D0%90%D0%BB%D1%84%D0%B0%D0%B2%D1%96%D1%82.pdf"
        self.assertEqual(get_title(url), "File:Алфавіт.pdf")

    def test_commons_file_url_no_archive_namespace_prepended(self):
        # File: titles must NOT have Архів: added even with include_namespace=True
        url = "https://commons.wikimedia.org/wiki/File:scan.pdf"
        self.assertFalse(get_title(url, include_namespace=True).startswith("Архів:"))

    def test_commons_file_url_strip_namespace(self):
        url = "https://commons.wikimedia.org/wiki/File:scan.pdf"
        self.assertEqual(get_title(url, include_namespace=False), "scan.pdf")

    def test_strip_latin_file_namespace(self):
        self.assertEqual(get_title("File:doc.pdf", include_namespace=False), "doc.pdf")

    def test_strip_cyrillic_file_namespace(self):
        self.assertEqual(get_title("Файл:doc.pdf", include_namespace=False), "doc.pdf")

    def test_cyrillic_file_namespace_not_overwritten(self):
        self.assertFalse(get_title("Файл:doc.pdf", include_namespace=True).startswith("Архів:"))


# ── expand_link_target ────────────────────────────────────────────────────────

class TestExpandLinkTarget(unittest.TestCase):
    PAGE = "Архів:ДАЖО/П-141"

    def test_absolute_link_unchanged(self):
        result = expand_link_target("Архів:ДАЖО", self.PAGE)
        self.assertEqual(result, f"{ARCHIVE_BASE}/wiki/Архів:ДАЖО")

    def test_child_relative_slash(self):
        # /1/ → child of the page
        result = expand_link_target("/1/", self.PAGE)
        self.assertIn("П-141/1", result)

    def test_sibling_dot_dot(self):
        # ../П-142 → sibling at the parent level
        result = expand_link_target("../П-142", self.PAGE)
        self.assertIn("ДАЖО/П-142", result)
        self.assertNotIn("П-141", result)

    def test_dot_slash(self):
        result = expand_link_target("./sub", self.PAGE)
        self.assertIn("П-141/sub", result)

    def test_spaces_become_underscores(self):
        result = expand_link_target("My Page", self.PAGE)
        self.assertIn("My_Page", result)

    def test_multiple_slashes_collapsed(self):
        result = expand_link_target("//1//", self.PAGE)
        path = result.replace(f"{ARCHIVE_BASE}/wiki/", "")
        self.assertNotIn("//", path)

    def test_base_url_present(self):
        result = expand_link_target("SomePage", self.PAGE)
        self.assertTrue(result.startswith(ARCHIVE_BASE))


# ── _normalize_link_title ─────────────────────────────────────────────────────

class TestNormalizeLinkTitle(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(_normalize_link_title(""), "")

    def test_fragment_stripped(self):
        self.assertEqual(_normalize_link_title("Page#section"), "Page")

    def test_underscores_to_spaces(self):
        self.assertEqual(_normalize_link_title("My_Page"), "My Page")

    def test_leading_colon_stripped(self):
        self.assertEqual(_normalize_link_title(":Namespace:Page"), "Namespace:Page")

    def test_url_decoded(self):
        # percent-encoded "ДАЖО"
        result = _normalize_link_title("%D0%94%D0%90%D0%96%D0%9E")
        self.assertEqual(result, "ДАЖО")

    def test_combined(self):
        result = _normalize_link_title(":My_Page#anchor")
        self.assertEqual(result, "My Page")


# ── Page classification ───────────────────────────────────────────────────────

class TestClassifyPage(unittest.TestCase):
    def test_archive_level(self):
        self.assertEqual(classify_page(_SAMPLE_ARCHIVE_TITLE), "archive")

    def test_fond_level(self):
        self.assertEqual(classify_page(_SAMPLE_FOND), "fond")

    def test_opus_level(self):
        self.assertEqual(classify_page(_SAMPLE_OPUS), "opus")

    def test_case_level(self):
        self.assertEqual(classify_page(_SAMPLE_CASE), "case")

    def test_is_archive_true(self):
        self.assertTrue(is_archive(_SAMPLE_ARCHIVE_TITLE))

    def test_is_archive_false(self):
        self.assertFalse(is_archive(_SAMPLE_FOND))

    def test_page_kind_case_no_children(self):
        self.assertEqual(page_kind(_SAMPLE_CASE, has_children=False), "case")

    def test_page_kind_case_with_children_becomes_opus(self):
        self.assertEqual(page_kind(_SAMPLE_CASE, has_children=True), "opus")

    def test_page_kind_non_case_unaffected_by_children(self):
        self.assertEqual(page_kind(_SAMPLE_OPUS, has_children=True), "opus")


# ── bare archive-root titles ────────────────────────────────────────────────
#
# A bare archive-root title (e.g. "Архів:ДАЛО", with no "/subarchive" suffix)
# is never itself a subarchive page, so it has no ARCHIVE_BY_TITLE entry --
# only ARCHIVES_BY_ROOT knows about it. Coarsened archive-level watching (see
# user.py's _watch_title()) can surface a real edit to exactly this title, so
# it must be classifiable, not just the subarchive-suffixed titles.

class TestBareArchiveRoot(unittest.TestCase):
    def test_classify_page_is_archive(self):
        self.assertEqual(classify_page(_ARCHIVE_ROOT), "archive")

    def test_is_archive_true(self):
        self.assertTrue(is_archive(_ARCHIVE_ROOT))

    def test_parent_title_is_none(self):
        self.assertIsNone(parent_title(_ARCHIVE_ROOT))

class TestMwPageDocUrl(unittest.TestCase):
    def _page(self, notes=None, internal=None, other_commons=None, external=None,
              category_links=None):
        return {
            "title": {"uk": "Архів:ДАЖО/П-141"},
            "notes": notes or {},
            "other_links": {
                "internal_links": internal or [],
                "commons_links": other_commons or [],
                "external_links": external or [],
                "category_links": category_links or [],
            }
        }

    def test_prefers_notes_commons_link(self):
        url = "https://commons.wikimedia.org/wiki/File:doc.pdf"
        page = self._page(notes={"commons_links": [url]})
        self.assertEqual(mw_page_doc_url(page), url)

    def test_falls_back_to_internal_link(self):
        page = self._page(internal=["SomeOtherPage"])
        result = mw_page_doc_url(page)
        self.assertIsNotNone(result)
        self.assertIn("SomeOtherPage", result)

    def test_falls_back_to_other_commons(self):
        url = "https://commons.wikimedia.org/wiki/File:scan.pdf"
        page = self._page(other_commons=[url])
        self.assertEqual(mw_page_doc_url(page), url)

    def test_falls_back_to_external(self):
        page = self._page(external=["https://example.com/doc.pdf"])
        self.assertEqual(mw_page_doc_url(page), "https://example.com/doc.pdf")

    def test_returns_none_when_no_links(self):
        self.assertIsNone(mw_page_doc_url(self._page()))

    def test_notes_takes_priority_over_internal(self):
        notes_url = "https://commons.wikimedia.org/wiki/File:notes.pdf"
        page = self._page(
            notes={"commons_links": [notes_url]},
            internal=["SomePage"],
        )
        self.assertEqual(mw_page_doc_url(page), notes_url)

    def test_category_links_exclude_internal(self):
        # Internal link matches a category; should be excluded
        page = self._page(
            internal=["Городницький район"],
            category_links=["Категорія:Городницький район"],
        )
        self.assertIsNone(mw_page_doc_url(page))


# ── _is_table ─────────────────────────────────────────────────────────────────

class TestIsTable(unittest.TestCase):
    def _first_tag(self, wikitext):
        return mwparserfromhell.parse(wikitext).filter_tags()[0]

    def test_wikitable_detected(self):
        tag = self._first_tag('{| class="wikitable"\n|-\n|cell\n|}')
        self.assertTrue(_is_table(tag))

    def test_wikitable_with_extra_classes(self):
        tag = self._first_tag('{| class="wikitable sortable"\n|-\n|cell\n|}')
        self.assertTrue(_is_table(tag))

    def test_non_wikitable_class_rejected(self):
        tag = self._first_tag('{| class="other"\n|-\n|cell\n|}')
        self.assertFalse(_is_table(tag))

    def test_no_class_rejected(self):
        tag = self._first_tag('{|\n|-\n|cell\n|}')
        self.assertFalse(_is_table(tag))


# ── _extract_links ────────────────────────────────────────────────────────────

class TestExtractLinks(unittest.TestCase):
    def test_internal_link(self):
        result = _extract_links("[[Архів:ДАЖО/П-141]]")
        self.assertIn("Архів:ДАЖО/П-141", result["internal_links"])

    def test_relative_internal_link(self):
        result = _extract_links("[[/1/]]")
        self.assertIn("/1/", result["internal_links"])

    def test_external_link(self):
        result = _extract_links("[https://example.com Example]")
        self.assertIn("https://example.com", result["external_links"])

    def test_category_link(self):
        result = _extract_links("[[Категорія:Городницький район]]")
        self.assertTrue(
            any("Городницький" in c for c in result["category_links"])
        )

    def test_commons_shorthand_expanded(self):
        result = _extract_links("[[c:File:scan.pdf]]")
        self.assertTrue(
            any("commons.wikimedia.org" in c for c in result["commons_links"])
        )

    def test_accepts_wikicode_object(self):
        wc = mwparserfromhell.parse("[[SomePage]]")
        result = _extract_links(wc)
        self.assertIn("SomePage", result["internal_links"])

    def test_empty_wikitext_returns_empty_lists(self):
        result = _extract_links("")
        for v in result.values():
            self.assertEqual(v, [])

    def test_multiple_links(self):
        result = _extract_links("[[PageA]] [[PageB]] [https://x.com X]")
        self.assertIn("PageA", result["internal_links"])
        self.assertIn("PageB", result["internal_links"])
        self.assertIn("https://x.com", result["external_links"])


# ── _subtract_links ───────────────────────────────────────────────────────────

class TestSubtractLinks(unittest.TestCase):
    def test_removes_matching_entry(self):
        links = {"internal_links": ["Page1", "Page2", "Page3"]}
        _subtract_links(links, {"internal_links": ["Page2"]})
        self.assertNotIn("Page2", links["internal_links"])
        self.assertIn("Page1", links["internal_links"])
        self.assertIn("Page3", links["internal_links"])

    def test_key_absent_in_base_is_ignored(self):
        links = {"internal_links": ["Page1"]}
        _subtract_links(links, {"external_links": ["https://a.com"]})
        self.assertEqual(links["internal_links"], ["Page1"])

    def test_value_absent_in_base_is_ignored(self):
        links = {"internal_links": ["Page1"]}
        _subtract_links(links, {"internal_links": ["Page9"]})
        self.assertEqual(links["internal_links"], ["Page1"])

    def test_removes_multiple_keys(self):
        links = {
            "internal_links": ["A", "B"],
            "external_links": ["https://x.com"],
        }
        _subtract_links(links, {
            "internal_links": ["A"],
            "external_links": ["https://x.com"],
        })
        self.assertNotIn("A", links["internal_links"])
        self.assertIn("B", links["internal_links"])
        self.assertEqual(links["external_links"], [])


# ── _extract_colspan ──────────────────────────────────────────────────────────

class TestExtractColspan(unittest.TestCase):
    def test_absent_returns_zero(self):
        self.assertEqual(_extract_colspan("plain text"), 0)

    def test_double_quoted(self):
        self.assertEqual(_extract_colspan('colspan="3"'), 3)

    def test_single_quoted(self):
        self.assertEqual(_extract_colspan("colspan='2'"), 2)

    def test_unquoted(self):
        self.assertEqual(_extract_colspan("colspan=4"), 4)

    def test_case_insensitive(self):
        self.assertEqual(_extract_colspan("COLSPAN=5"), 5)

    def test_embedded_in_longer_string(self):
        self.assertEqual(_extract_colspan('style="bold" colspan="2" align="center"'), 2)


# ── _expand_colspan ───────────────────────────────────────────────────────────

class TestExpandColspan(unittest.TestCase):
    def test_no_colspan(self):
        cells = [{"text": "A", "colspan": 0}, {"text": "B", "colspan": 0}]
        self.assertEqual(_expand_colspan(cells), ["A", "B"])

    def test_colspan_two(self):
        cells = [{"text": "A", "colspan": 2}, {"text": "B", "colspan": 0}]
        self.assertEqual(_expand_colspan(cells), ["A", "", "B"])

    def test_colspan_three(self):
        cells = [{"text": "Merged", "colspan": 3}]
        self.assertEqual(_expand_colspan(cells), ["Merged", "", ""])

    def test_empty_input(self):
        self.assertEqual(_expand_colspan([]), [])

    def test_missing_colspan_key(self):
        # Cells without "colspan" key default to 0 (no expansion)
        cells = [{"text": "X"}, {"text": "Y"}]
        self.assertEqual(_expand_colspan(cells), ["X", "Y"])


# ── _tokenize_wikitext_table_cell ─────────────────────────────────────────────

class TestTokenizeWikitextTableCell(unittest.TestCase):
    def test_plain_text(self):
        text, colspan = _tokenize_wikitext_table_cell("Hello World")
        self.assertIn("Hello", text)
        self.assertEqual(colspan, 0)

    def test_directive_before_pipe(self):
        text, colspan = _tokenize_wikitext_table_cell('style="color:red" | Hello')
        self.assertIn("Hello", text)
        self.assertNotIn("style", text)

    def test_colspan_extracted(self):
        text, colspan = _tokenize_wikitext_table_cell('colspan="3" | Data')
        self.assertEqual(colspan, 3)
        self.assertIn("Data", text)

    def test_wikilink_preserved(self):
        text, colspan = _tokenize_wikitext_table_cell("[[/1/]]")
        self.assertIn("[[/1/]]", text)

    def test_empty_cell(self):
        text, colspan = _tokenize_wikitext_table_cell("")
        self.assertEqual(text, "")
        self.assertEqual(colspan, 0)

    def test_text_only_after_pipe(self):
        # Directive with no text content after pipe → empty text
        text, colspan = _tokenize_wikitext_table_cell('style="x" |')
        self.assertEqual(text.strip(), "")


# ── _tokenize_wikitext_table_line ─────────────────────────────────────────────

class TestTokenizeWikitextTableLine(unittest.TestCase):
    def test_single_cell(self):
        cells = _tokenize_wikitext_table_line("Only one cell")
        self.assertEqual(len(cells), 1)
        self.assertIn("Only one cell", cells[0])

    def test_pipe_pipe_separator(self):
        cells = _tokenize_wikitext_table_line("Cell 1||Cell 2||Cell 3")
        self.assertEqual(len(cells), 3)

    def test_bang_bang_separator(self):
        cells = _tokenize_wikitext_table_line("H1!!H2!!H3")
        self.assertEqual(len(cells), 3)

    def test_wikilink_in_cell(self):
        cells = _tokenize_wikitext_table_line("[[/1/]]||Description||1922-1957")
        self.assertEqual(len(cells), 3)
        self.assertIn("[[/1/]]", cells[0])

    def test_colspan_expands(self):
        cells = _tokenize_wikitext_table_line('colspan="2" | Wide||Narrow')
        # Wide cell expands to 2, plus Narrow → 3 total
        self.assertEqual(len(cells), 3)


# ── _parse_wikitext_table_lines ───────────────────────────────────────────────

class TestParseWikitextTableLines(unittest.TestCase):
    SIMPLE = "!H1||H2\n|-\n|A||B\n|-\n|C||D"

    def test_header_and_two_data_rows(self):
        rows = _parse_wikitext_table_lines(self.SIMPLE)
        self.assertEqual(len(rows), 3)  # header row + 2 data rows

    def test_header_cells_correct(self):
        rows = _parse_wikitext_table_lines(self.SIMPLE)
        self.assertIn("H1", rows[0])
        self.assertIn("H2", rows[0])

    def test_data_cells_correct(self):
        rows = _parse_wikitext_table_lines(self.SIMPLE)
        self.assertIn("A", rows[1])
        self.assertIn("B", rows[1])

    def test_empty_lines_skipped(self):
        wt = "!H1\n\n\n|-\n|A"
        rows = _parse_wikitext_table_lines(wt)
        self.assertEqual(len(rows), 2)

    def test_caption_line_ignored(self):
        wt = "|+ Caption\n!H1\n|-\n|A"
        rows = _parse_wikitext_table_lines(wt)
        self.assertEqual(len(rows), 2)

    def test_empty_input(self):
        self.assertEqual(_parse_wikitext_table_lines(""), [])

    def test_row_separator_without_trailing_data(self):
        # Trailing |- with no more data should not produce an extra row
        wt = "!H\n|-\n|A\n|-"
        rows = _parse_wikitext_table_lines(wt)
        self.assertEqual(len(rows), 2)

    def test_multiple_header_cells_on_one_line(self):
        wt = "!H1!!H2!!H3\n|-\n|A||B||C"
        rows = _parse_wikitext_table_lines(wt)
        self.assertEqual(len(rows[0]), 3)


# ── _parse_wikitext_table ──────────────────────────────────────────────────────

class TestParseWikitextTable(unittest.TestCase):
    CONTENT = "!H1||H2||H3\n|-\n|A||B||C\n|-\n|D||E||F"

    def test_header_and_body_counts(self):
        header, body = _parse_wikitext_table(self.CONTENT)
        self.assertEqual(len(header), 3)
        self.assertEqual(len(body), 2)

    def test_header_contains_expected_values(self):
        header, _ = _parse_wikitext_table(self.CONTENT)
        self.assertIn("H1", header)
        self.assertIn("H3", header)

    def test_body_row_cell_count(self):
        _, body = _parse_wikitext_table(self.CONTENT)
        self.assertEqual(len(body[0]), 3)

    def test_empty_input(self):
        header, body = _parse_wikitext_table("")
        self.assertEqual(header, [])
        self.assertEqual(body, [])

    def test_no_row_separator(self):
        # A table with only a header and no |- should still work
        header, body = _parse_wikitext_table("!H1||H2")
        self.assertEqual(header, ["H1", "H2"])
        self.assertEqual(body, [])

    def test_single_data_row_no_header(self):
        header, body = _parse_wikitext_table("|A||B")
        self.assertEqual(header, ["A", "B"])
        self.assertEqual(body, [])


# ── _extract_table ────────────────────────────────────────────────────────────

class TestExtractTable(unittest.TestCase):
    PAGE = "Архів:ДАЖО/П-141"

    def _run(self, table_content, page_links=None):
        if page_links is None:
            page_links = _blank_page_links()
        all_links = set()
        result = _extract_table(table_content, self.PAGE, page_links, all_links)
        return result, all_links

    def test_header_extracted_as_text_items(self):
        content = "!№||Анотація||Крайні дати\n|-\n|[[/1/]]||Опис 1||1922"
        result, _ = self._run(content)
        texts = [get_text(h) for h in result["header"]]
        self.assertIn("№", texts)
        self.assertIn("Анотація", texts)

    def test_internal_link_resolved(self):
        content = "!№\n|-\n|[[/1/]]"
        result, all_links = self._run(content)
        child = result["children"][0][0]
        self.assertIsNotNone(child["link"])
        self.assertIn("П-141/1", child["link"])

    def test_internal_link_added_to_all_links(self):
        content = "!№\n|-\n|[[/1/]]"
        result, all_links = self._run(content)
        self.assertTrue(any("П-141/1" in l for l in all_links))

    def test_anchor_links_skipped(self):
        content = "!H\n|-\n|[[#section]]"
        result, _ = self._run(content)
        self.assertIsNone(result["children"][0][0]["link"])

    def test_plain_text_cell_no_link(self):
        content = "!H\n|-\n|just text"
        result, _ = self._run(content)
        child = result["children"][0][0]
        self.assertEqual(get_text(child["text"]), "just text")
        self.assertIsNone(child["link"])

    def test_link_removed_from_page_links(self):
        page_links = _blank_page_links()
        page_links["internal_links"].append("/1/")
        content = "!H\n|-\n|[[/1/]]"
        self._run(content, page_links=page_links)
        self.assertNotIn("/1/", page_links["internal_links"])

    def test_multiple_rows(self):
        content = "!H\n|-\n|Row1\n|-\n|Row2\n|-\n|Row3"
        result, _ = self._run(content)
        self.assertEqual(len(result["children"]), 3)

    def test_result_has_header_and_children_keys(self):
        result, _ = self._run("!H\n|-\n|A")
        self.assertIn("header", result)
        self.assertIn("children", result)


# ── _normalize_child_link_positions ───────────────────────────────────────────

class TestNormalizeChildLinkPositions(unittest.TestCase):
    def _make_tables(self, children):
        return [{"header": [], "children": children}]

    def test_link_in_first_cell_unchanged(self):
        tables = self._make_tables([[
            {"text": {"uk": "A"}, "link": "/wiki/PageA", "exists": True},
            {"text": {"uk": "B"}},
        ]])
        _normalize_child_link_positions(tables)
        self.assertEqual(tables[0]["children"][0][0]["link"], "/wiki/PageA")

    def test_link_moved_from_second_cell(self):
        tables = self._make_tables([[
            {"text": {"uk": "A"}},
            {"text": {"uk": "B"}, "link": "/wiki/PageB", "exists": True},
        ]])
        _normalize_child_link_positions(tables)
        child = tables[0]["children"][0]
        self.assertEqual(child[0].get("link"), "/wiki/PageB")
        self.assertNotIn("link", child[1])
        self.assertNotIn("exists", child[1])

    def test_exists_flag_copied(self):
        tables = self._make_tables([[
            {"text": {"uk": "A"}},
            {"text": {"uk": "B"}, "link": "/wiki/PageB", "exists": True},
        ]])
        _normalize_child_link_positions(tables)
        self.assertTrue(tables[0]["children"][0][0]["exists"])

    def test_single_cell_not_modified(self):
        tables = self._make_tables([[{"text": {"uk": "A"}}]])
        _normalize_child_link_positions(tables)
        self.assertNotIn("link", tables[0]["children"][0][0])

    def test_non_wiki_link_not_moved(self):
        # Links not starting with /wiki/ should not be moved
        tables = self._make_tables([[
            {"text": {"uk": "A"}},
            {"text": {"uk": "B"}, "link": "https://example.com", "exists": True},
        ]])
        _normalize_child_link_positions(tables)
        self.assertNotIn("link", tables[0]["children"][0][0])

    def test_empty_tables_list(self):
        _normalize_child_link_positions([])   # should not raise

    def test_only_first_matching_link_moved(self):
        # Two later cells with /wiki/ links — only the first one gets promoted
        tables = self._make_tables([[
            {"text": {"uk": "A"}},
            {"text": {"uk": "B"}, "link": "/wiki/First", "exists": True},
            {"text": {"uk": "C"}, "link": "/wiki/Second", "exists": True},
        ]])
        _normalize_child_link_positions(tables)
        self.assertEqual(tables[0]["children"][0][0]["link"], "/wiki/First")


# ── _parse_wiki_text (core parser) ────────────────────────────────────────────

class TestParseWikiText(unittest.TestCase):
    """
    Tests for the core wikitext→page-dict parser.
    Network I/O (_check_page_existence_chunked) is patched out.
    """

    def _parse(self, wikitext, page_title="Архів:ДАЖО/П-141", title="ДАЖО/П-141",
               revid=None):
        with patch("birddog.wiki._check_page_existence_chunked", return_value={}):
            return _parse_wiki_text(wikitext, page_title, title, revid=revid)

    # ── output structure ────────────────────────────────────────────────────

    def test_page_dict_has_all_keys(self):
        page = self._parse("")
        expected = {"title", "template", "revid", "description", "dates",
                    "notes", "other_links", "tables", "link", "doc_link"}
        self.assertEqual(set(page.keys()), expected)

    def test_link_field_contains_base_url(self):
        page = self._parse("")
        self.assertIn(ARCHIVE_BASE, page["link"])

    def test_revid_stored(self):
        page = self._parse("", revid=42)
        self.assertEqual(page["revid"], 42)

    # ── template extraction ─────────────────────────────────────────────────

    def test_description_extracted(self):
        wt = "{{Архіви/фонд\n | назва = Тестова назва\n | рік = 1920-1940\n}}"
        page = self._parse(wt)
        self.assertEqual(get_text(page["description"]), "Тестова назва")

    def test_dates_extracted(self):
        wt = "{{Архіви/фонд\n | назва = Foo\n | рік = 1920-1940\n}}"
        page = self._parse(wt)
        self.assertEqual(get_text(page["dates"]), "1920-1940")

    def test_template_name_stored(self):
        wt = "{{Архіви/фонд\n | назва = Foo\n}}"
        page = self._parse(wt)
        self.assertIn("Архіви", get_text(page["template"]))

    def test_no_template_gives_none_description(self):
        wt = "{| class=\"wikitable\"\n!H\n|-\n|Cell\n|}"
        page = self._parse(wt)
        self.assertIsNone(get_text(page["description"]))

    def test_primtky_links_go_to_notes(self):
        wt = (
            "{{Архіви/фонд\n"
            " | назва = Foo\n"
            " | примітки = [[c:File:scan.pdf]]\n"
            "}}"
        )
        page = self._parse(wt)
        self.assertIn("commons_links", page["notes"])
        self.assertGreater(len(page["notes"]["commons_links"]), 0)

    # ── table parsing ───────────────────────────────────────────────────────

    def test_well_formed_table_found(self):
        wt = (
            "{{Архіви/фонд | назва = X}}\n"
            "{| class=\"wikitable\"\n"
            "!№||Анотація\n"
            "|-\n"
            "|[[/1/]]||Опис 1\n"
            "|}"
        )
        page = self._parse(wt)
        self.assertEqual(len(page["tables"]), 1)
        self.assertEqual(len(page["tables"][0]["children"]), 1)

    def test_table_gets_sequential_name(self):
        wt = "{| class=\"wikitable\"\n!H\n|-\n|Cell\n|}"
        page = self._parse(wt)
        self.assertEqual(page["tables"][0]["name"], "Table 1")

    def test_multiple_tables_numbered(self):
        wt = (
            "{| class=\"wikitable\"\n!H\n|-\n|A\n|}\n"
            "{| class=\"wikitable\"\n!H\n|-\n|B\n|}"
        )
        page = self._parse(wt)
        self.assertEqual(len(page["tables"]), 2)
        self.assertEqual(page["tables"][1]["name"], "Table 2")

    # ── unclosed table repair ───────────────────────────────────────────────

    def test_unclosed_table_repaired(self):
        # Reproduces the Архів:ДАЖО/П-141 bug: table missing closing |}
        wt = (
            "{{Архіви/фонд\n"
            " | назва = Городницький районний комітет ЛКСМУ\n"
            " | рік = 1922-1925\n"
            " | примітки =\n"
            "}}\n"
            "== Описи ==\n"
            "{| class=\"wikitable\"\n"
            "!№||Анотація||Одиниць зберігання||Крайні дати\n"
            "|-\n"
            "|[[/1/]]||Опис 1||1922-1957||1957\n"
            "|-\n\n"
            "[[Категорія: Городницький район]]"
        )
        page = self._parse(wt)
        self.assertEqual(len(page["tables"]), 1)
        self.assertEqual(len(page["tables"][0]["children"]), 1)

    def test_unclosed_table_row_data_preserved(self):
        wt = (
            "{| class=\"wikitable\"\n"
            "!Header\n"
            "|-\n"
            "|Content row\n"
            "|-"  # no closing |}
        )
        page = self._parse(wt)
        self.assertEqual(len(page["tables"]), 1)
        self.assertEqual(get_text(page["tables"][0]["children"][0][0]["text"]), "Content row")

    # ── synthetic table fallbacks ────────────────────────────────────────────

    def test_synthetic_table_from_subpage_links(self):
        wt = "* [[/1/]]\n* [[/2/]]\n* [[/3/]]"
        page = self._parse(wt)
        self.assertEqual(len(page["tables"]), 1)
        self.assertEqual(page["tables"][0]["name"], "Linked Pages")
        self.assertEqual(len(page["tables"][0]["children"]), 3)

    def test_no_table_no_links_yields_empty(self):
        page = self._parse("No tables, no links.")
        self.assertEqual(page["tables"], [])

    def test_synthetic_table_from_absolute_links_on_hub_page(self):
        # a bare archive-root page (e.g. "Архів:Архіви") links to other
        # archives by full title, not as a literal subpage
        wt = "* [[Архів:ДАЛО]]\n* [[Архів:ДААРК]]"
        page = self._parse(wt, page_title="Архів:Архіви", title="Архіви")
        self.assertEqual(len(page["tables"]), 1)
        self.assertEqual(page["tables"][0]["name"], "Linked Pages")
        children_text = {get_text(c[0]["text"]) for c in page["tables"][0]["children"]}
        self.assertEqual(children_text, {"ДАЛО", "ДААРК"})

    def test_absolute_links_not_picked_up_on_ordinary_content_page(self):
        # regression guard: ordinary content pages routinely cross-link to
        # other archives in prose -- those must NOT be swept into a
        # synthesized "Linked Pages" table (unlike a hub page's own links)
        wt = "See also [[Архів:ДАЛО]] and [[Архів:ДААРК]] for related records."
        page = self._parse(wt)  # default page_title is a fond-level title
        self.assertEqual(page["tables"], [])


# ── mw_read_page (mocked network) ────────────────────────────────────────────

class TestMwReadPage(unittest.TestCase):
    WIKITEXT = (
        "{{Архіви/фонд\n"
        " | назва = Городницький районний комітет ЛКСМУ\n"
        " | рік = 1922-1925\n"
        " | примітки =\n"
        "}}\n"
        "== Описи ==\n"
        "{| class=\"wikitable\"\n"
        "!№||Анотація||Одиниць зберігання||Крайні дати\n"
        "|-\n"
        "|[[/1/]]||Опис 1||1||1922-1957\n"
        "|}"
    )
    REVID = 1044473

    def _rev_response(self):
        return {
            "query": {
                "pages": {
                    "12345": {
                        "revisions": [{"timestamp": "2023-05-01T10:00:00Z"}]
                    }
                }
            }
        }

    def _read_page(self, wikitext=None):
        if wikitext is None:
            wikitext = self.WIKITEXT
        with (
            patch("birddog.wiki._read_wiki_text",
                  return_value=(wikitext, self.REVID, "ДАЖО/П-141")),
            patch("birddog.wiki.fetch_url", return_value=self._rev_response()),
            patch("birddog.wiki._check_page_existence_chunked", return_value={}),
        ):
            return mw_read_page("Архів:ДАЖО/П-141")

    def test_page_dict_has_all_keys(self):
        page = self._read_page()
        expected = {
            "title", "template", "revid", "description", "dates",
            "notes", "other_links", "tables", "lastmod", "link", "doc_link"
        }
        self.assertEqual(set(page.keys()), expected)

    def test_description_parsed(self):
        page = self._read_page()
        self.assertIn("Городницький", get_text(page["description"]))

    def test_dates_parsed(self):
        page = self._read_page()
        self.assertIn("1922", get_text(page["dates"]))

    def test_table_present(self):
        page = self._read_page()
        self.assertEqual(len(page["tables"]), 1)

    def test_table_has_one_row(self):
        page = self._read_page()
        self.assertEqual(len(page["tables"][0]["children"]), 1)

    def test_revid_stored(self):
        page = self._read_page()
        self.assertEqual(page["revid"], self.REVID)

    def test_lastmod_set(self):
        page = self._read_page()
        self.assertIsNotNone(page["lastmod"])

    def test_unclosed_table_repaired_in_full_pipeline(self):
        # Remove the closing |} to simulate the ДАЖО/П-141 bug
        broken = self.WIKITEXT.replace("\n|}", "")
        page = self._read_page(wikitext=broken)
        self.assertEqual(len(page["tables"]), 1)

    def test_nonexistent_page_returns_placeholder(self):
        with (
            patch("birddog.wiki._read_wiki_text",
                  side_effect=RuntimeError("not found")),
            patch("birddog.wiki.page_exists", return_value=False),
        ):
            page = mw_read_page("Архів:ДАЖО/DoesNotExist")
        self.assertIn("tables", page)

    def test_url_extracted_from_full_url(self):
        # mw_read_page should accept a full URL and strip to title
        full_url = f"{ARCHIVE_BASE}/wiki/Архів:ДАЖО/П-141"
        with (
            patch("birddog.wiki._read_wiki_text",
                  return_value=(self.WIKITEXT, self.REVID, "ДАЖО/П-141")),
            patch("birddog.wiki.fetch_url", return_value=self._rev_response()),
            patch("birddog.wiki._check_page_existence_chunked", return_value={}),
        ):
            page = mw_read_page(full_url)
        self.assertIn("title", page)


# ── LABELS_BY_PREFIX (archive label lookup table) ─────────────────────────────

class TestLabelsByPrefix(unittest.TestCase):
    """LABELS_BY_PREFIX maps wiki title roots to short archive labels."""

    def test_single_subarchive_maps_to_archive_key(self):
        # DAARK has only one subarchive; its prefix maps to just "DAARK" (no suffix)
        self.assertEqual(LABELS_BY_PREFIX.get("Архів:ДААРК"), "DAARK")

    def test_shared_prefix_multi_sub_maps_to_archive_key(self):
        # DACHGO has Д and Р subarchives, both under Архів:ДАЧгО — no suffix needed
        self.assertEqual(LABELS_BY_PREFIX.get("Архів:ДАЧгО"), "DACHGO")

    def test_distinct_prefix_maps_to_subarchive_label(self):
        # Decerkva subarchives have unique wiki roots; label retains the suffix
        self.assertEqual(LABELS_BY_PREFIX.get("Архів:Лука_Мала"), "Decerkva-MalaLuka")

    def test_unknown_prefix_returns_none(self):
        self.assertIsNone(LABELS_BY_PREFIX.get("Архів:НевідомийАрхів"))


# ── page_label ────────────────────────────────────────────────────────────────

class TestPageLabel(unittest.TestCase):
    """page_label returns a short transliterated label for a wiki page title."""

    def test_fond_level(self):
        self.assertEqual(page_label("Архів:ДААРК/Д/П-1"), "DAARK/D/P-1")

    def test_opus_level(self):
        self.assertEqual(page_label("Архів:ДААРК/Д/П-1/1"), "DAARK/D/P-1/1")

    def test_case_level(self):
        self.assertEqual(page_label("Архів:ДААРК/Д/П-1/1/1"), "DAARK/D/P-1/1/1")

    def test_multi_sub_shared_prefix_root_has_no_hyphen_suffix(self):
        # DACHGO-D and DACHGO-R share the same wiki root → label root is "DACHGO"
        self.assertTrue(page_label("Архів:ДАЧгО/Д/П-1").startswith("DACHGO/"))
        self.assertFalse(page_label("Архів:ДАЧгО/Д/П-1").startswith("DACHGO-"))

    def test_multi_sub_subarchives_produce_distinct_labels(self):
        # D and R fonds under the same archive still produce different labels
        self.assertNotEqual(
            page_label("Архів:ДАЧгО/Д/П-1"),
            page_label("Архів:ДАЧгО/Р/П-1"),
        )

    def test_distinct_prefix_keeps_subarchive_suffix(self):
        # Decerkva/MalaLuka has a unique wiki root; label retains the full suffix
        self.assertEqual(page_label("Архів:Лука_Мала/П-1/1/1"), "Decerkva-MalaLuka/P-1/1/1")

    def test_archive_root_page_returns_label_only(self):
        # A page at the archive root (no path tail) returns just the label root
        self.assertEqual(page_label("Архів:ДААРК"), "DAARK")

    def test_fallback_strips_namespace_prefix(self):
        # Unknown wiki root → fallback strips "Архів:" and returns the rest as-is
        self.assertEqual(page_label("Архів:НевідомийАрхів/П-1"), "НевідомийАрхів/П-1")

    def test_bare_title_normalised_same_as_explicit_namespace(self):
        # canonicalize_title adds the namespace; result must match the explicit form
        self.assertEqual(page_label("ДААРК/Д/П-1"), page_label("Архів:ДААРК/Д/П-1"))


# ── title_in_scope ───────────────────────────────────────────────────────────

class TestTitleInScope(unittest.TestCase):
    """title_in_scope matches a title against literal title-path prefixes."""

    def test_exact_match(self):
        self.assertTrue(title_in_scope(_SAMPLE_FOND, [_SAMPLE_FOND]))

    def test_descendant_matches(self):
        self.assertTrue(title_in_scope(_SAMPLE_CASE, [_SAMPLE_FOND]))

    def test_ancestor_does_not_match(self):
        self.assertFalse(title_in_scope(_ARCHIVE_ROOT, [_SAMPLE_FOND]))

    def test_unrelated_title_does_not_match(self):
        self.assertFalse(title_in_scope(_SAMPLE_FOND, ["Архів:НевідомийАрхів"]))

    def test_sibling_sharing_string_prefix_does_not_match(self):
        # ".../Ф-1" must not match a sibling like ".../Ф-10" -- boundary is "/", not raw prefix
        self.assertFalse(title_in_scope(_ARCHIVE_ROOT + "/Ф-10", [_SAMPLE_FOND]))

    def test_namespace_root_matches_everything(self):
        self.assertTrue(title_in_scope(_SAMPLE_CASE, [f"{WIKI_NAMESPACE}:"]))

    def test_any_include_prefix_is_sufficient(self):
        self.assertTrue(title_in_scope(_SAMPLE_FOND, ["Архів:НевідомийАрхів", _ARCHIVE_ROOT]))

    def test_no_include_match_returns_false(self):
        self.assertFalse(title_in_scope(_SAMPLE_FOND, ["Архів:НевідомийАрхів"]))

    def test_exclude_removes_otherwise_included_title(self):
        self.assertFalse(title_in_scope(_SAMPLE_FOND, [_ARCHIVE_ROOT], exclude=[_SAMPLE_FOND]))

    def test_exclude_only_affects_matching_subtree(self):
        self.assertTrue(title_in_scope(_SAMPLE_OPUS, [_ARCHIVE_ROOT], exclude=["Архів:НевідомийАрхів"]))

    def test_title_is_canonicalized(self):
        # bare title (no namespace) should match the same as the fully qualified form
        bare = _SAMPLE_FOND[len(f"{WIKI_NAMESPACE}:"):]
        self.assertTrue(title_in_scope(bare, [_SAMPLE_FOND]))


# ── title_lineage ────────────────────────────────────────────────────────────

class TestTitleLineage(unittest.TestCase):
    """title_lineage walks ancestors by literal title path only, and always
    terminates at ROOT_HUB_TITLE so every page has a way back to it."""

    def test_leaf_to_root_order(self):
        self.assertEqual(
            title_lineage(_SAMPLE_CASE),
            [_SAMPLE_CASE, _SAMPLE_OPUS, _SAMPLE_FOND, _ARCHIVE_ROOT, ROOT_HUB_TITLE],
        )

    def test_archive_root_has_no_synthetic_subarchive_level(self):
        # unlike lineage(), no synthetic subarchive level is inserted --
        # the bare archive-root title is the immediate parent of a fond
        self.assertEqual(title_lineage(_SAMPLE_FOND), [_SAMPLE_FOND, _ARCHIVE_ROOT, ROOT_HUB_TITLE])

    def test_bare_archive_root_terminates_at_hub(self):
        self.assertEqual(title_lineage(_ARCHIVE_ROOT), [_ARCHIVE_ROOT, ROOT_HUB_TITLE])

    def test_hub_title_is_single_element(self):
        # viewing the hub itself must not duplicate it
        self.assertEqual(title_lineage(ROOT_HUB_TITLE), [ROOT_HUB_TITLE])

    def test_does_not_raise_on_titles_parent_title_cannot_classify(self):
        # regression: parent_title()/lineage() raise ValueError on a bare
        # multi-subarchive archive root (e.g. "Архів:ДАДнО") since it's
        # never itself a classified entry in ARCHIVE_BY_TITLE
        self.assertEqual(title_lineage("Архів:ДАДнО"), ["Архів:ДАДнО", ROOT_HUB_TITLE])


class TestTitleLineageLabels(unittest.TestCase):
    """title_lineage_labels gives a latinized display label per lineage level."""

    def test_same_length_and_order_as_lineage(self):
        labels = title_lineage_labels(_SAMPLE_CASE)
        self.assertEqual(len(labels), len(title_lineage(_SAMPLE_CASE)))

    def test_each_label_is_that_levels_own_segment(self):
        # _SAMPLE_FOND is "<root>/Ф-1"; its lineage is [fond, root, hub]
        labels = title_lineage_labels(_SAMPLE_FOND)
        self.assertEqual(labels[1], page_label(_ARCHIVE_ROOT))
        # the hub's label comes from the dynamic archive-root registry (not
        # LABELS_BY_PREFIX) and, once registered, persists -- so compare
        # against page_label() itself rather than a hardcoded string; the
        # frontend shows an icon for this level regardless of its value
        self.assertEqual(labels[2], page_label(ROOT_HUB_TITLE))

    def test_hub_alone_has_one_label(self):
        self.assertEqual(title_lineage_labels(ROOT_HUB_TITLE), [page_label(ROOT_HUB_TITLE)])


# ── dynamic archive-root registry ───────────────────────────────────────────

class TestDynamicArchiveRootRegistry(unittest.TestCase):
    """is_root_title/register_archive_root/archive_root_label/all_archive_roots
    -- the KV-backed registry replacing offline subarchive-sniffing for
    root-level discovery. Uses synthetic, clearly-fake titles so this
    doesn't depend on real archive data, and stays idempotent across runs
    (register_archive_root never overwrites an already-known title). Uses a
    throwaway, temp-file-backed KV store swapped in for
    birddog.wiki._archive_roots_kv for the duration of each test, so this
    class never reads or writes the real local/production archive-roots
    store."""

    _PROBE_TITLE = "Архів:__TestRegistryProbe__"

    def setUp(self):
        import birddog.wiki as wiki_mod
        self._wiki_mod = wiki_mod
        self._orig_kv = wiki_mod._archive_roots_kv
        self._orig_cache = wiki_mod._archive_roots_cache
        self._tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp_db.close()
        wiki_mod._archive_roots_kv = SQLiteKeyValueStore(db_path=self._tmp_db.name)
        wiki_mod._archive_roots_cache = {}  # matches a fresh store's lazy-loaded state

    def tearDown(self):
        wiki_mod = self._wiki_mod
        wiki_mod._archive_roots_kv = self._orig_kv
        wiki_mod._archive_roots_cache = self._orig_cache
        os.remove(self._tmp_db.name)

    def test_is_root_title(self):
        self.assertTrue(is_root_title(_ARCHIVE_ROOT))
        self.assertFalse(is_root_title(_SAMPLE_FOND))

    def test_register_and_lookup_round_trip(self):
        label = register_archive_root(self._PROBE_TITLE, label="ProbeLabel")
        self.assertEqual(label, "ProbeLabel")
        self.assertEqual(archive_root_label(self._PROBE_TITLE), "ProbeLabel")

    def test_register_non_root_title_is_a_no_op(self):
        self.assertIsNone(register_archive_root(_SAMPLE_FOND))
        self.assertIsNone(archive_root_label(_SAMPLE_FOND))

    def test_register_default_label_is_transliterated(self):
        probe = "Архів:__ПробаРеєстру__"
        label = register_archive_root(probe)
        self.assertTrue(label)
        self.assertTrue(label.isascii())

    def test_register_default_label_prefers_curated_archive_key(self):
        # _ARCHIVE_ROOT has curated legacy data -- its default label must be
        # the clean archive_key (e.g. "DADNO"), not a raw transliteration of
        # the title, which can inherit inconsistent case from the wiki's own
        # Cyrillic spelling (e.g. "Архів:ДАДнО" -> "DADnO")
        expected = ARCHIVES_BY_ROOT[_ARCHIVE_ROOT][0][0]
        label = register_archive_root(_ARCHIVE_ROOT)
        self.assertEqual(label, expected)

    def test_refresh_curated_archive_root_labels_fixes_stale_default(self):
        import birddog.wiki as wiki_mod
        expected = ARCHIVES_BY_ROOT[_ARCHIVE_ROOT][0][0]
        # directly seed a stale, non-curated label -- bypassing
        # register_archive_root's no-overwrite guard -- to simulate data
        # registered before this preference existed
        wiki_mod._archive_roots_kv.insert(
            wiki_mod._ARCHIVE_ROOTS_NAMESPACE, _ARCHIVE_ROOT, json.dumps({"label": "StaleLabel", "description": ""}))
        wiki_mod._archive_roots_cache[_ARCHIVE_ROOT] = {"label": "StaleLabel", "description": ""}
        self.assertEqual(archive_root_label(_ARCHIVE_ROOT), "StaleLabel")

        fixed = refresh_curated_archive_root_labels()

        self.assertGreaterEqual(fixed, 1)
        self.assertEqual(archive_root_label(_ARCHIVE_ROOT), expected)

    def test_register_default_label_keeps_distinct_roots_split_from_one_curated_key_apart(self):
        # "Архів:ГДА_МВС" and "Архів:ГДА_МО" are different real archives,
        # but the legacy curated data split a compound code ("GDA-MVS",
        # "GDA-MOD") into archive_key="GDA" + a subarchive_key for each --
        # using archive_key alone as the label would collapse them onto the
        # same "GDA" label, so the (non-"D_") subarchive_key must be kept
        mvs = register_archive_root("Архів:ГДА_МВС")
        mod = register_archive_root("Архів:ГДА_МО")
        self.assertNotEqual(mvs, mod)
        self.assertEqual(mvs, "GDA-MVS")
        self.assertEqual(mod, "GDA-MOD")

    def test_register_disambiguates_default_colliding_with_another_title(self):
        first = register_archive_root("Архів:__DupProbeA__", label="DupLabel")
        second = register_archive_root("Архів:__DupProbeB__")
        self.assertEqual(first, "DupLabel")
        self.assertNotEqual(second, "DupLabel")

    def test_deduplicate_archive_root_labels_fixes_collision(self):
        import birddog.wiki as wiki_mod
        titles = ("Архів:__DedupProbeA__", "Архів:__DedupProbeB__")
        for title in titles:
            wiki_mod._archive_roots_kv.insert(
                wiki_mod._ARCHIVE_ROOTS_NAMESPACE, title, json.dumps({"label": "SharedProbeLabel", "description": ""}))
            wiki_mod._archive_roots_cache[title] = {"label": "SharedProbeLabel", "description": ""}

        fixed = deduplicate_archive_root_labels()

        self.assertGreaterEqual(fixed, 1)
        labels = {archive_root_label(title) for title in titles}
        self.assertEqual(len(labels), 2)

    def test_all_archive_roots_excludes_hub(self):
        titles = [e["title"] for e in all_archive_roots()]
        self.assertNotIn(ROOT_HUB_TITLE, titles)

    def test_all_archive_roots_includes_registered_probe(self):
        register_archive_root(self._PROBE_TITLE, label="ProbeLabel")
        entries = all_archive_roots()
        self.assertIn(
            {
                "title": self._PROBE_TITLE,
                "label": "ProbeLabel",
                "description": "",
                "url": page_url_from_title(self._PROBE_TITLE),
            },
            entries,
        )

    def test_all_archive_roots_sorted_by_label(self):
        entries = all_archive_roots()
        labels = [e["label"] for e in entries]
        self.assertEqual(labels, sorted(labels))


# ── get_all_pages ───────────────────────────────────────────────────────────

class TestGetAllPages(unittest.TestCase):
    def _empty_response(self):
        return {"query": {"allpages": []}}

    def test_excludes_redirects(self):
        # Regression for issue #135: a homoglyph-typo'd title (e.g. a Latin
        # "H" where a Cyrillic "Н" was meant) left behind as a #REDIRECT
        # stub after a rename must not be seeded into the tracker as if it
        # were its own page -- mirrors get_recent_changes()'s rcshow=!redirect.
        with patch("birddog.wiki.fetch_url",
                   return_value=self._empty_response()) as mock_fetch:
            get_all_pages()
        params = mock_fetch.call_args.kwargs.get("params") or mock_fetch.call_args[1]["params"]
        self.assertEqual(params.get("apfilterredir"), "nonredirects")


# ── batch_fetch_document_links ────────────────────────────────────────────────

class TestBatchFetchDocumentLinks(unittest.TestCase):
    TITLE = "Архів:ДАСО/993/1/15"
    COMMONS_PLAIN = "https://commons.wikimedia.org/wiki/File:Simple.pdf"
    EXTERNAL_URL = "https://example.com/doc.pdf"

    def _content_response(self, title, has_revision=True):
        page = {"title": title}
        if has_revision:
            page["revisions"] = [{"slots": {"main": {"*": ""}}}]
        return {"query": {"pages": {"1": page}}}

    def _run(self, urls, bpe_return=None, has_revision=True):
        with (
            patch("birddog.wiki.fetch_url",
                  return_value=self._content_response(self.TITLE, has_revision)),
            patch("birddog.wiki._check_page_existence_chunked", return_value={}),
            patch("birddog.wiki._collect_doc_links_from_page", return_value=urls),
            patch("birddog.wiki.batch_page_exists",
                  return_value=bpe_return or {}) as mock_bpe,
        ):
            result = batch_fetch_document_links([self.TITLE])
        return result, mock_bpe

    def test_confirmed_present_commons_url_returns_exists_true(self):
        result, _ = self._run(
            [self.COMMONS_PLAIN],
            bpe_return={"File:Simple.pdf": True},
        )
        entries = result[canonicalize_title(self.TITLE)]
        self.assertEqual(entries, [{"link": self.COMMONS_PLAIN, "exists": True}])

    def test_confirmed_missing_commons_url_returns_exists_false(self):
        result, _ = self._run(
            [self.COMMONS_PLAIN],
            bpe_return={"File:Simple.pdf": False},
        )
        entries = result[canonicalize_title(self.TITLE)]
        self.assertFalse(entries[0]["exists"])

    def test_non_wiki_url_skips_api_and_defaults_to_exists_true(self):
        result, mock_bpe = self._run([self.EXTERNAL_URL])
        mock_bpe.assert_not_called()
        entries = result[canonicalize_title(self.TITLE)]
        self.assertEqual(entries, [{"link": self.EXTERNAL_URL, "exists": True}])

    def test_semicolon_in_filename_sends_full_title_to_api(self):
        # urlparse splits paths at ';' — the fix reconstructs the full title.
        url = "https://commons.wikimedia.org/wiki/File:A;B.pdf"
        result, mock_bpe = self._run([url], bpe_return={"File:A;B.pdf": True})
        called_titles = mock_bpe.call_args[0][0]
        self.assertIn("File:A;B.pdf", called_titles)
        self.assertNotIn("File:A", called_titles)

    def test_page_missing_from_content_api_returns_empty_links(self):
        result, _ = self._run([], has_revision=False)
        self.assertEqual(result[canonicalize_title(self.TITLE)], [])


# ── Live network tests (skipped in offline environments) ──────────────────────

class TestLiveReadPage(unittest.TestCase):
    """Smoke tests that hit the real wiki.  Run only when network is available."""

    @classmethod
    def setUpClass(cls):
        import socket
        try:
            socket.setdefaulttimeout(3)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("uk.wikisource.org", 443))
            s.close()
        except OSError:
            raise unittest.SkipTest("No network access")

    def test_read_page_returns_expected_structure(self):
        page = mw_read_page("ДААРК")
        self.assertEqual(set(page.keys()), {
            "title", "template", "revid", "description", "dates",
            "notes", "other_links", "tables", "lastmod", "link", "doc_link"
        })

if __name__ == "__main__":
    unittest.main()
