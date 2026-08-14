import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from birddog.abstract_database import InvalidRecordId
from birddog.task import TaskManager
from birddog.database_updater import (
    DatabaseUpdater,
    DatabaseUpdateManager,
    _allowed_doc_link,
    _apply_doc_link_diff,
    _append_unlink_note,
    _create_links,
    _edit_links,
    _extract_links_from_wiki_parse,
    _form_simple_page_record,
    _get_linked_doc_urls,
    _has_processing_state,
    _is_category_link,
    _lookup_pages,
    _normalize_title,
    _page_urls_from_titles,
    _replace_links,
    _sniff_suffix,
    _source_from_url,
    form_document_record,
    get_child_titles,
    normalize_url,
    scan_child_titles_by_id,
)


_LOOKUP_FIELDS = ["label", "description"]
_LOOKUP_FIELD_MAPPING = {"description": "page_description"}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeDB:
    """
    Minimal in-memory stand-in for the Database interface, covering only the
    methods database_updater.py actually calls. Not a general-purpose fake -
    just enough surface for each test to configure the exact shape it needs.
    """

    def __init__(self):
        self.lookup_map = {}        # table -> {key: id}
        self.records = {}           # table -> {id: record dict}
        self.links = {}             # (table, field, source) -> set(target ids)
        self.scan_pages = {}        # table -> [(records, has_more), ...]
        self.scan_links_pages = {}  # (table, field, source) -> [(ids, has_more), ...]
        self.keys = {}              # table -> key field name
        self.create_calls = []
        self.delete_calls = []
        self.write_calls = []
        self._next_id = 0

    def key_field_name(self, table_name):
        return self.keys.get(table_name, "Id")

    def lookup(self, table_name, key_set):
        table = self.lookup_map.get(table_name, {})
        if isinstance(key_set, str):
            return table.get(key_set)
        if isinstance(key_set, set):
            return {k: table[k] for k in key_set if k in table}
        return [table.get(k) for k in key_set]

    def read(self, table_name, record_id, fields=None):
        table = self.records.get(table_name, {})
        if isinstance(record_id, str):
            return dict(table.get(record_id, {}))
        return [dict(table[rid]) if rid in table else {} for rid in record_id]

    def write(self, table_name, records):
        self.write_calls.append((table_name, records))
        singleton = isinstance(records, dict)
        if singleton:
            records = [records]
        ids = []
        for rec in records:
            self._next_id += 1
            rid = rec.get("Id") or f"new{self._next_id}"
            self.records.setdefault(table_name, {})[rid] = dict(rec)
            ids.append(rid)
        return ids[0] if singleton else ids

    def scan(self, table_name, limit=100, cursor=None, where=None,
             view_name=None, sort=None, fields=None, raw=False, use_v3=False):
        pages = self.scan_pages.get(table_name, [])
        idx = cursor or 0
        if idx >= len(pages):
            return [], None
        records, has_more = pages[idx]
        return records, (idx + 1 if has_more else None)

    def get_links(self, table_name, link_field, source_record):
        return list(self.links.get((table_name, link_field, source_record), set()))

    def create_links(self, table_name, link_field, source_record, target_records):
        self.create_calls.append((table_name, link_field, source_record, target_records))
        key = (table_name, link_field, source_record)
        existing = self.links.setdefault(key, set())
        if isinstance(target_records, (list, tuple, set)):
            existing.update(target_records)
        else:
            existing.add(target_records)

    def delete_links(self, table_name, link_field, source_record, target_records):
        self.delete_calls.append((table_name, link_field, source_record, target_records))
        key = (table_name, link_field, source_record)
        existing = self.links.setdefault(key, set())
        if isinstance(target_records, (list, tuple, set)):
            existing.difference_update(target_records)
        else:
            existing.discard(target_records)

    def scan_links(self, table_name, link_field, source_record, limit=100, cursor=None):
        key = (table_name, link_field, source_record)
        queue = self.scan_links_pages.get(key, [])
        idx = cursor or 0
        if idx >= len(queue):
            return [], None
        ids, has_more = queue[idx]
        return ids, (idx + 1 if has_more else None)


class FakeKVStore:
    """Minimal in-memory stand-in for AbstractKeyValueStore."""

    def __init__(self):
        self.data = {}

    def insert(self, namespace, key, value):
        self.data[(namespace, key)] = value

    def get(self, namespace, key):
        return self.data[(namespace, key)]

    def remove(self, namespace, key):
        del self.data[(namespace, key)]

    def get_all(self, namespace):
        return [(k[1], v) for k, v in self.data.items() if k[0] == namespace]


def make_updater(db=None):
    return DatabaseUpdater(runtime=SimpleNamespace(database=db or FakeDB()))


def make_manager():
    """Bypasses __init__ (which touches a real KeyValueStore/TaskManager) and
    sets only the attributes exercised by the methods under test."""
    manager = DatabaseUpdateManager.__new__(DatabaseUpdateManager)
    manager._state_lock = threading.RLock()
    manager._state_store = FakeKVStore()
    return manager


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

class TestNormalizeUrl(unittest.TestCase):
    def test_lowercases_scheme_and_host(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.ORG/path"),
            "https://example.org/path",
        )

    def test_decodes_percent_encoded_unicode_path(self):
        url = "https://uk.wikisource.org/wiki/%D0%A4%D0%B0%D0%B9%D0%BB:%D0%A2%D0%B5%D1%81%D1%82.pdf"
        self.assertEqual(
            normalize_url(url),
            "https://uk.wikisource.org/wiki/Файл:Тест.pdf",
        )

    def test_reencodes_unsafe_characters(self):
        # a literal (unencoded) '#' in the input is parsed as a URL fragment
        # delimiter and dropped, not preserved as a path character - so to
        # exercise the path re-encoding, feed already-percent-encoded input
        # (e.g. a filename that legitimately contains '#'/'?'/space)
        self.assertEqual(
            normalize_url("https://example.org/a%20b%23c%3Fd"),
            "https://example.org/a%20b%23c%3Fd",
        )

    def test_unencoded_hash_starts_a_fragment_which_is_dropped(self):
        self.assertEqual(
            normalize_url("https://example.org/a b#c?d"),
            "https://example.org/a%20b",
        )

    def test_drops_fragment_keeps_query(self):
        self.assertEqual(
            normalize_url("https://example.org/path?x=1#frag"),
            "https://example.org/path?x=1",
        )


class TestSourceFromUrl(unittest.TestCase):
    def test_commons(self):
        self.assertEqual(_source_from_url("https://commons.wikimedia.org/wiki/File:x"), "commons")
        self.assertEqual(_source_from_url("https://wikimedia.org/x"), "commons")

    def test_wikisource(self):
        self.assertEqual(_source_from_url("https://uk.wikisource.org/wiki/x"), "wikisource")
        self.assertEqual(_source_from_url("https://wikisource.org/x"), "wikisource")

    def test_neither(self):
        self.assertIsNone(_source_from_url("https://example.org/x"))


class TestFormDocumentRecord(unittest.TestCase):
    def test_wiki_file_url(self):
        rec = form_document_record("https://uk.wikisource.org/wiki/Файл:Foo.pdf")
        self.assertTrue(rec["wiki"])
        self.assertEqual(rec["title"], "Файл:Foo.pdf")
        self.assertEqual(rec["url"], "https://uk.wikisource.org/wiki/Файл:Foo.pdf")

    def test_non_wiki_recognized_document_suffix(self):
        rec = form_document_record("https://example.org/files/report.pdf")
        self.assertFalse(rec["wiki"])
        self.assertEqual(rec["title"], "report.pdf")

    def test_wiki_path_on_non_wiki_host(self):
        # /wiki/ path but not a wikisource/wikimedia host -> falls into the
        # generic branch, wiki stays False
        rec = form_document_record("https://example.org/wiki/SomePage")
        self.assertFalse(rec["wiki"])

    def test_unrecognized_suffix_falls_back_to_url_as_title(self):
        rec = form_document_record("https://example.org/some/page")
        self.assertEqual(rec["title"], "https://example.org/some/page")
        self.assertFalse(rec["wiki"])


class TestSniffSuffix(unittest.TestCase):
    def test_document_suffix(self):
        self.assertEqual(_sniff_suffix("foo.pdf"), "document")
        self.assertEqual(_sniff_suffix("foo.PDF"), "document")

    def test_image_suffix(self):
        self.assertEqual(_sniff_suffix("foo.jpg"), "image")

    def test_unknown_suffix(self):
        self.assertIsNone(_sniff_suffix("foo.html"))
        self.assertIsNone(_sniff_suffix("foo"))

    def test_ignores_query_string(self):
        self.assertEqual(_sniff_suffix("https://example.org/foo.pdf?x=1"), "document")


class TestAllowedDocLink(unittest.TestCase):
    def test_blocklisted_link_rejected(self):
        self.assertFalse(_allowed_doc_link("https://familysearch.org/thing"))

    def test_normal_link_allowed(self):
        self.assertTrue(_allowed_doc_link("https://example.org/thing.pdf"))


class TestIsCategoryLink(unittest.TestCase):
    def test_category(self):
        self.assertTrue(_is_category_link("Категорія:Foo"))

    def test_non_category(self):
        self.assertFalse(_is_category_link("Архів:Foo"))


class TestPageUrlsFromTitles(unittest.TestCase):
    def test_list(self):
        result = _page_urls_from_titles(["A/B"])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].endswith("A/B"))

    def test_set(self):
        result = _page_urls_from_titles({"A/B"})
        self.assertIsInstance(result, set)

    def test_str(self):
        result = _page_urls_from_titles("A/B")
        self.assertIsInstance(result, str)

    def test_invalid_type_raises(self):
        with self.assertRaises(TypeError):
            _page_urls_from_titles(123)


class TestFormSimplePageRecord(unittest.TestCase):
    def test_structure(self):
        rec = _form_simple_page_record("Архів:ДАЖО/Д")
        for field in ("title", "url", "label", "root_label", "level", "seq_label"):
            self.assertIn(field, rec)
        self.assertEqual(rec["source_type"], "wiki")
        self.assertEqual(rec["availability"], "linked")
        self.assertTrue(rec["url"].startswith("http"))


class TestExtractLinksFromWikiParse(unittest.TestCase):
    TITLE = "Архів:ДАЖО/Д/1/74/1"

    def test_children_parent_category_and_internal_links(self):
        parse = {
            "links": [
                {"*": f"{self.TITLE}/2", "exists": True},           # child
                {"*": "Архів:ДАЖО/Д/1/74", "exists": True},          # parent
                {"*": "Категорія:Щось", "exists": True},             # category
                {"*": "Файл:Other.pdf", "exists": True},             # plain internal doc link
            ],
            "images": [
                "Файл:Embedded.pdf",   # document -> included
                "Файл:Photo.jpg",      # image, not document -> excluded
            ],
            "iwlinks": [
                {"*": "commons thing", "url": "https://commons.wikimedia.org/wiki/File:X"},
                {"*": "other wiki", "url": "https://en.wikipedia.org/wiki/Y"},
            ],
            "externallinks": ["https://example.org/z"],
        }

        result = _extract_links_from_wiki_parse(self.TITLE, parse)

        self.assertEqual([c["title"] for c in result["children"]], [f"{self.TITLE}/2"])
        self.assertEqual(result["parent"]["title"], "Архів:ДАЖО/Д/1/74")
        # category links are matched on the raw title (before namespace canonicalization
        # prepends "Архів:" to anything without a recognized archive-namespace alias)
        self.assertEqual([c["title"] for c in result["category_links"]], ["Архів:Категорія:Щось"])

        internal_titles = {item["title"] for item in result["internal_links"]}
        # links sourced from parse["links"] go through canonicalize_title, which
        # prepends the archive namespace since "Файл" isn't a recognized alias;
        # links sourced from parse["images"] are built directly and keep the
        # bare "Файл:" prefix (see the two different construction paths above)
        self.assertIn("Архів:Файл:Other.pdf", internal_titles)
        self.assertIn("Файл:Embedded.pdf", internal_titles)
        self.assertNotIn("Файл:Photo.jpg", internal_titles)

        self.assertEqual(len(result["commons_links"]), 1)
        self.assertEqual(result["commons_links"][0]["url"], "https://commons.wikimedia.org/wiki/File:X")
        self.assertEqual(len(result["interwiki_links"]), 1)
        self.assertEqual(result["external_links"], ["https://example.org/z"])

    def test_no_parent_for_unrecognized_archive_root(self):
        # parent_title() raises for an unrecognized root; _safe_parent_title
        # swallows it, so no link should ever match as "parent"
        parse = {"links": [{"*": "Архів:ДАЖО/Д/1/74", "exists": True}]}
        result = _extract_links_from_wiki_parse("Test:Unrecognized/Root", parse)
        self.assertIsNone(result["parent"])


# ---------------------------------------------------------------------------
# DB-backed helper functions (module level)
# ---------------------------------------------------------------------------

class TestEditLinks(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.db.links[("Pages", "children", "p1")] = {"a", "b"}

    def test_replace_no_change_is_noop(self):
        added, removed = _replace_links(self.db, "Pages", "children", "p1", ["a", "b"])
        self.assertEqual((added, removed), (set(), set()))
        self.assertEqual(self.db.create_calls, [])
        self.assertEqual(self.db.delete_calls, [])

    def test_replace_deletes_and_recreates_full_set(self):
        added, removed = _replace_links(self.db, "Pages", "children", "p1", ["b", "c"])
        self.assertEqual(added, {"c"})
        self.assertEqual(removed, {"a"})
        # replace mode deletes all existing targets, then writes the full new set
        self.assertEqual(set(self.db.delete_calls[0][3]), {"a", "b"})
        self.assertEqual(set(self.db.create_calls[0][3]), {"b", "c"})

    def test_replace_accepts_scalar_target(self):
        _replace_links(self.db, "Pages", "children", "p1", "z")
        self.assertEqual(set(self.db.create_calls[0][3]), {"z"})

    def test_create_only_skips_when_subset_of_existing(self):
        added, removed = _create_links(self.db, "Pages", "children", "p1", ["a"])
        self.assertEqual((added, removed), (set(), set()))
        self.assertEqual(self.db.create_calls, [])
        self.assertEqual(self.db.delete_calls, [])

    def test_create_only_adds_without_deleting(self):
        added, removed = _create_links(self.db, "Pages", "children", "p1", ["a", "c"])
        self.assertEqual(added, {"c"})
        self.assertEqual(removed, set())
        self.assertEqual(self.db.delete_calls, [])
        self.assertEqual(len(self.db.create_calls), 1)


class TestHasProcessingState(unittest.TestCase):
    def test_blank_record_has_no_state(self):
        self.assertFalse(_has_processing_state({"url": "https://x", "owning_pages": []}))

    def test_blank_string_and_zero_are_not_state(self):
        rec = {"comments": "", "pages_processed": 0, "doc_type": None}
        self.assertFalse(_has_processing_state(rec))

    def test_any_populated_field_counts(self):
        self.assertTrue(_has_processing_state({"processor": "Juliana"}))
        self.assertTrue(_has_processing_state({"pages_processed": 3}))
        self.assertTrue(_has_processing_state({"comments": "reviewed"}))

    def test_unrelated_fields_do_not_count(self):
        self.assertFalse(_has_processing_state({"url": "https://x", "sha1_hash": "abc"}))


class TestAppendUnlinkNote(unittest.TestCase):
    def test_first_note_on_blank_comments(self):
        result = _append_unlink_note("", "ДАХмО/Р-582/2/272", "2026-08-14")
        self.assertEqual(result, "unlinked from ДАХмО/Р-582/2/272 on 2026-08-14")

    def test_first_note_on_none_comments(self):
        result = _append_unlink_note(None, "Page A", "2026-08-14")
        self.assertEqual(result, "unlinked from Page A on 2026-08-14")

    def test_appends_to_existing_comments(self):
        result = _append_unlink_note("processor: Juliana", "Page A", "2026-08-14")
        self.assertEqual(result, "processor: Juliana\nunlinked from Page A on 2026-08-14")

    def test_repeat_call_for_same_page_is_idempotent(self):
        first = _append_unlink_note("", "Page A", "2026-08-14")
        second = _append_unlink_note(first, "Page A", "2026-08-20")
        self.assertEqual(second, first)

    def test_different_page_gets_its_own_note(self):
        first = _append_unlink_note("", "Page A", "2026-08-14")
        second = _append_unlink_note(first, "Page B", "2026-08-14")
        self.assertEqual(
            second,
            "unlinked from Page A on 2026-08-14\nunlinked from Page B on 2026-08-14",
        )


class TestApplyDocLinkDiff(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.db.links[("Pages", "doc_links", "p1")] = {"d1", "d2"}

    def test_deletes_removed_and_creates_added(self):
        added, removed = _apply_doc_link_diff(
            self.db, "Pages", "doc_links", "p1", {"d3"}, {"d1"})
        self.assertEqual((added, removed), ({"d3"}, {"d1"}))
        self.assertEqual(self.db.delete_calls, [("Pages", "doc_links", "p1", ["d1"])])
        self.assertEqual(self.db.create_calls, [("Pages", "doc_links", "p1", ["d3"])])
        self.assertEqual(self.db.links[("Pages", "doc_links", "p1")], {"d2", "d3"})

    def test_no_op_when_both_sets_empty(self):
        _apply_doc_link_diff(self.db, "Pages", "doc_links", "p1", set(), set())
        self.assertEqual(self.db.create_calls, [])
        self.assertEqual(self.db.delete_calls, [])

    def test_add_only_skips_delete_call(self):
        _apply_doc_link_diff(self.db, "Pages", "doc_links", "p1", {"d3"}, set())
        self.assertEqual(self.db.delete_calls, [])
        self.assertEqual(len(self.db.create_calls), 1)


class TestGetLinkedDocUrls(unittest.TestCase):
    def test_unknown_title_raises(self):
        db = FakeDB()
        with self.assertRaises(ValueError):
            _get_linked_doc_urls(db, "Архів:Unknown/1")

    def test_no_linked_docs_returns_empty(self):
        db = FakeDB()
        url = _page_urls_from_titles("Архів:ДАЖО/Д")
        db.lookup_map["Pages"] = {url: "p1"}
        self.assertEqual(_get_linked_doc_urls(db, "Архів:ДАЖО/Д"), [])

    def test_filters_missing_urls(self):
        db = FakeDB()
        url = _page_urls_from_titles("Архів:ДАЖО/Д")
        db.lookup_map["Pages"] = {url: "p1"}
        db.links[("Pages", "doc_links", "p1")] = {"d1", "d2"}
        db.records["Documents"] = {
            "d1": {"url": "https://example.org/d1.pdf"},
            "d2": {},  # missing url -> filtered out
        }
        self.assertEqual(_get_linked_doc_urls(db, "Архів:ДАЖО/Д"), ["https://example.org/d1.pdf"])


class TestLookupPages(unittest.TestCase):
    def test_converts_titles_to_urls_before_lookup(self):
        db = FakeDB()
        url = _page_urls_from_titles("Архів:ДАЖО/Д")
        db.lookup_map["Pages"] = {url: "p1"}
        result = _lookup_pages(db, ["Архів:ДАЖО/Д"])
        self.assertEqual(result, ["p1"])


class TestScanChildTitlesById(unittest.TestCase):
    def test_single_page_single_parent(self):
        db = FakeDB()
        db.scan_links_pages[("Pages", "children", "p1")] = [(["c1", "c2"], False)]
        db.records["Pages"] = {"c1": {"title": "T1"}, "c2": {"title": "T2"}}

        titles, cursor = scan_child_titles_by_id(db, ["p1"], limit=100)
        self.assertEqual(titles, ["T1", "T2"])
        self.assertIsNone(cursor)

    def test_resumes_across_pages_at_limit_boundary(self):
        db = FakeDB()
        db.scan_links_pages[("Pages", "children", "p1")] = [
            (["c1", "c2", "c3"], True),
            (["c4", "c5", "c6"], False),
        ]
        db.records["Pages"] = {f"c{i}": {"title": f"T{i}"} for i in range(1, 7)}

        titles, cursor = scan_child_titles_by_id(db, ["p1"], limit=3)
        self.assertEqual(titles, ["T1", "T2", "T3"])
        self.assertIsNotNone(cursor)

        more_titles, cursor2 = scan_child_titles_by_id(db, ["p1"], limit=3, cursor=cursor)
        self.assertEqual(more_titles, ["T4", "T5", "T6"])
        self.assertIsNone(cursor2)

    def test_skips_falsy_parent_ids(self):
        db = FakeDB()
        db.scan_links_pages[("Pages", "children", "p1")] = [(["c1"], False)]
        db.records["Pages"] = {"c1": {"title": "T1"}}

        titles, cursor = scan_child_titles_by_id(db, [None, "p1"], limit=100)
        self.assertEqual(titles, ["T1"])
        self.assertIsNone(cursor)

    def test_invalid_cursor_raises_invalid_record_id(self):
        db = FakeDB()
        with self.assertRaises(InvalidRecordId):
            scan_child_titles_by_id(db, ["p1"], cursor=object())


class TestGetChildTitles(unittest.TestCase):
    def test_looks_up_parents_and_dedupes_children(self):
        db = FakeDB()
        parent_url = _page_urls_from_titles("Архів:ДАЖО/Д")
        db.lookup_map["Pages"] = {parent_url: "p1"}
        db.scan_links_pages[("Pages", "children", "p1")] = [(["c1", "c2"], False)]
        db.records["Pages"] = {"c1": {"title": "T1"}, "c2": {"title": "T1"}}  # duplicate title

        result = get_child_titles(db, ["Архів:ДАЖО/Д"])
        self.assertEqual(result, ["T1"])


# ---------------------------------------------------------------------------
# DatabaseUpdater
# ---------------------------------------------------------------------------

class TestSetDocLookupFields(unittest.TestCase):
    """
    Unit tests for DatabaseUpdater._set_doc_lookup_fields, which refresh_doc_lookups
    uses to decide which Documents get lookup_status="valid" plus copied-over page
    metadata (label, description, ...).

    Regression coverage for: a Document with no owning page yet must NOT be marked
    "valid" - doing so drops it out of the "BD:Need Page Lookups" view before it is
    ever actually linked, permanently stranding it with empty metadata.
    """

    def setUp(self):
        self.updater = DatabaseUpdater(runtime=None)

    def test_linked_doc_marked_valid_with_metadata(self):
        doc_map = {"d1": {"Id": "d1", "url": "https://example.org/d1"}}
        page_map = {"p1": {"Id": "p1", "label": "Some Label", "description": "Some Description"}}
        owner_map = {"d1": ["p1"]}

        updates = self.updater._set_doc_lookup_fields(
            doc_map, page_map, owner_map, _LOOKUP_FIELDS, _LOOKUP_FIELD_MAPPING
        )

        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update["url"], "https://example.org/d1")
        self.assertEqual(update["lookup_status"], "valid")
        self.assertEqual(update["label"], "Some Label")
        self.assertEqual(update["page_description"], "Some Description")

    def test_unlinked_doc_is_not_marked_valid(self):
        # d1 has no owning page yet (e.g. just created via a manual spreadsheet
        # paste, before the link to its Page record was made)
        doc_map = {"d1": {"Id": "d1", "url": "https://example.org/d1"}}
        page_map = {}
        owner_map = {"d1": []}

        updates = self.updater._set_doc_lookup_fields(
            doc_map, page_map, owner_map, _LOOKUP_FIELDS, _LOOKUP_FIELD_MAPPING
        )

        self.assertEqual(updates, [])

    def test_mixed_batch_only_updates_linked_docs(self):
        doc_map = {
            "d1": {"Id": "d1", "url": "https://example.org/d1"},
            "d2": {"Id": "d2", "url": "https://example.org/d2"},
        }
        page_map = {"p1": {"Id": "p1", "label": "Label 1", "description": "Desc 1"}}
        owner_map = {"d1": ["p1"], "d2": []}

        updates = self.updater._set_doc_lookup_fields(
            doc_map, page_map, owner_map, _LOOKUP_FIELDS, _LOOKUP_FIELD_MAPPING
        )

        updated_urls = {u["url"] for u in updates}
        self.assertEqual(updated_urls, {"https://example.org/d1"})


class TestOwnerIdsAndFieldLookups(unittest.TestCase):
    def setUp(self):
        self.updater = DatabaseUpdater(runtime=None)

    def test_get_owner_ids_defaults_missing_to_empty_list(self):
        result = self.updater._get_owner_ids([{"Id": "d1"}, {"Id": "d2", "owning_pages": ["p1"]}])
        self.assertEqual(result, {"d1": [], "d2": ["p1"]})

    def test_get_field_lookups_skips_dangling_links(self):
        doc_map = {"d1": {"Id": "d1"}}
        page_map = {"p1": {"label": "L1"}}
        owner_map = {"d1": ["p1", "p_deleted"]}
        result = self.updater._get_field_lookups(doc_map, page_map, owner_map, "label")
        self.assertEqual(result, {"d1": ["L1"]})

    def test_reduce_update_value_takes_first_or_none(self):
        self.assertEqual(self.updater._reduce_update_value("label", ["A", "B"]), "A")
        self.assertIsNone(self.updater._reduce_update_value("label", []))


class TestUpdateDocRecordsFromRecords(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.updater = make_updater(self.db)

    def test_new_doc_is_written(self):
        doc_records = {"https://x/1": {"url": "https://x/1", "title": "1"}}
        changed = self.updater._update_doc_records_from_records(doc_records, update_doc_metadata=False)
        self.assertTrue(changed)
        self.assertEqual(len(self.db.write_calls), 1)

    def test_unchanged_doc_is_not_written(self):
        self.db.lookup_map["Documents"] = {"https://x/1": "d1"}
        self.db.records["Documents"] = {"d1": {"Id": "d1", "url": "https://x/1", "title": "1"}}
        doc_records = {"https://x/1": {"url": "https://x/1", "title": "1"}}

        changed = self.updater._update_doc_records_from_records(doc_records, update_doc_metadata=False)
        self.assertFalse(changed)
        self.assertEqual(self.db.write_calls, [])

    def test_changed_field_triggers_write(self):
        self.db.lookup_map["Documents"] = {"https://x/1": "d1"}
        self.db.records["Documents"] = {"d1": {"Id": "d1", "url": "https://x/1", "title": "old"}}
        doc_records = {"https://x/1": {"url": "https://x/1", "title": "new"}}

        changed = self.updater._update_doc_records_from_records(doc_records, update_doc_metadata=False)
        self.assertTrue(changed)
        self.assertEqual(len(self.db.write_calls), 1)


class TestUpdateDocRecordsValidation(unittest.TestCase):
    def setUp(self):
        self.updater = make_updater()

    def test_rejects_non_string_input(self):
        with self.assertRaises(ValueError):
            self.updater.update_doc_records(123)

    def test_empty_input_returns_false_without_db_access(self):
        self.assertFalse(self.updater.update_doc_records([]))

    def test_string_input_is_wrapped(self):
        # should not raise; single url is accepted same as a one-element list
        result = self.updater.update_doc_records("https://example.org/x.pdf", update_doc_metadata=False)
        self.assertTrue(result)


class TestGetDocsWithMissingMetadata(unittest.TestCase):
    def test_extracts_urls_from_scan(self):
        db = FakeDB()
        db.scan_pages["Documents"] = [([{"url": "https://x/1"}, {"url": "https://x/2"}], False)]
        updater = make_updater(db)
        self.assertEqual(
            updater.get_docs_with_missing_metadata(), ["https://x/1", "https://x/2"]
        )


class TestCollectTranslations(unittest.TestCase):
    def test_paginates_and_filters(self):
        db = FakeDB()
        db.keys["Pages"] = "Id"
        db.scan_pages["Pages"] = [
            ([
                {"Id": "p1", "description": None, "native_description": "Опис1"},
                {"Id": "p2", "description": "already translated", "native_description": "Опис2"},
            ], True),
            ([
                {"Id": "p3", "description": "", "native_description": "Опис3"},
            ], False),
        ]
        updater = make_updater(db)
        translations = updater._collect_translations()
        self.assertEqual(
            {t["Id"]: t["native_description"] for t in translations},
            {"p1": "Опис1", "p3": "Опис3"},
        )


class TestStartTranslation(unittest.TestCase):
    def test_starts_when_translations_pending(self):
        calls = []
        runtime = SimpleNamespace(
            database=FakeDB(),
            start_translation=lambda task_name, items: calls.append((task_name, items)),
        )
        updater = DatabaseUpdater(runtime=runtime)
        updater._collect_translations = lambda: [{"Id": "p1", "native_description": "X"}]

        updater.start_translation()

        self.assertEqual(len(calls), 1)
        task_name, items = calls[0]
        self.assertTrue(task_name.startswith("DBT_"))
        self.assertEqual(items, ["X"])

    def test_noop_when_nothing_pending(self):
        calls = []
        runtime = SimpleNamespace(
            database=FakeDB(),
            start_translation=lambda **kw: calls.append(kw),
        )
        updater = DatabaseUpdater(runtime=runtime)
        updater._collect_translations = lambda: []

        updater.start_translation()
        self.assertEqual(calls, [])


class TestCompleteTranslation(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.db.keys["Pages"] = "Id"
        self.updater = make_updater(self.db)

    def test_applies_translation_and_invalidates_linked_documents(self):
        self.updater._collect_translations = lambda: [
            {"Id": "p1", "native_description": "Опис1"},
            {"Id": "p2", "native_description": "Опис2"},  # no translation available
        ]
        self.db.links[("Pages", "doc_links", "p1")] = {"d1"}
        self.db.records["Documents"] = {"d1": {"url": "https://x/1"}}

        self.updater.complete_translation("task1", {"Опис1": "Translated 1"})

        pages_write = [c for c in self.db.write_calls if c[0] == "Pages"][0]
        self.assertEqual(len(pages_write[1]), 1)
        self.assertEqual(pages_write[1][0]["description"], "Translated 1")

        docs_write = [c for c in self.db.write_calls if c[0] == "Documents"][0]
        self.assertEqual(docs_write[1], [{"url": "https://x/1", "lookup_status": "invalid"}])

    def test_no_matching_translation_writes_nothing(self):
        self.updater._collect_translations = lambda: [
            {"Id": "p1", "native_description": "Опис1"},
        ]
        self.updater.complete_translation("task1", {"Something else": "X"})
        self.assertEqual(self.db.write_calls, [])


# ---------------------------------------------------------------------------
# DatabaseUpdateManager
# ---------------------------------------------------------------------------

class TestBatchSplitting(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()
        self.recorded = []
        self.manager._create_task = lambda *args, **kw: self.recorded.append((args, kw))

    def test_start_document_update_rejects_non_string_elements(self):
        with self.assertRaises(ValueError):
            self.manager.start_document_update([1, 2, 3])

    def test_start_document_update_empty_returns_none(self):
        self.assertIsNone(self.manager.start_document_update([]))
        self.assertEqual(self.recorded, [])

    def test_start_document_update_batches_by_batch_size(self):
        urls = [f"https://x/{i}" for i in range(45)]
        task_name = self.manager.start_document_update(urls)

        self.assertTrue(task_name.startswith("DBD_"))
        (name, kind, desc, total, batches, *_), kw = self.recorded[0]
        self.assertEqual(total, 45)
        sizes = [len(b["urls"]) for b in batches]
        self.assertEqual(sizes, [20, 20, 5])

    def test_start_update_normalizes_titles_and_sets_deep_flag(self):
        self.manager.start_update(["архів:дажо/д "], deep=True)
        (name, kind, desc, total, batches, *_), kw = self.recorded[0]
        self.assertEqual(batches[0]["titles"], [_normalize_title("архів:дажо/д ")])
        self.assertTrue(batches[0]["deep"])


class TestExecuteSubtaskDispatch(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_dispatches_page_update(self):
        calls = []
        self.manager._execute_page_update_subtask = lambda st: calls.append(("page", st))
        self.manager._execute_doc_update_subtask = lambda st: calls.append(("doc", st))
        subtask = {"payload": {"kind": DatabaseUpdateManager._PAGE_UPDATE_KIND}}
        self.manager.execute_subtask(subtask)
        self.assertEqual(calls, [("page", subtask)])

    def test_dispatches_doc_update(self):
        calls = []
        self.manager._execute_page_update_subtask = lambda st: calls.append(("page", st))
        self.manager._execute_doc_update_subtask = lambda st: calls.append(("doc", st))
        subtask = {"payload": {"kind": DatabaseUpdateManager._DOC_UPDATE_KIND}}
        self.manager.execute_subtask(subtask)
        self.assertEqual(calls, [("doc", subtask)])

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            self.manager.execute_subtask({"payload": {"kind": "nonsense"}})


class TestExecutePageUpdateSubtask(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()
        self.progress_calls = []
        self.manager._update_progress = lambda tid, inc: self.progress_calls.append((tid, inc))
        self.manager._update_doc_metadata = lambda titles: None
        self.manager._start_child_page_update_task = lambda titles: None

    def test_success_path_records_updated_result(self):
        self.manager._updater = SimpleNamespace(
            update_page_records=lambda titles, update_doc_metadata: True
        )
        subtask = {
            "task_id": "t1",
            "index": 0,
            "payload": {"kind": "update_pages", "titles": ["A", "B"], "deep": False},
        }
        self.manager._execute_page_update_subtask(subtask)

        self.assertEqual(subtask["payload"]["updated"], True)
        self.assertEqual(subtask["payload"]["titles"], ["A", "B"])
        self.assertNotIn("error", subtask["payload"])
        self.assertEqual(self.progress_calls, [("t1", 2)])

    def test_failure_path_records_error_and_skips_followups(self):
        def boom(titles, update_doc_metadata):
            raise RuntimeError("network exploded")

        self.manager._updater = SimpleNamespace(update_page_records=boom)
        followup_calls = []
        self.manager._update_doc_metadata = lambda titles: followup_calls.append(titles)

        subtask = {"task_id": "t1", "index": 0, "payload": {"kind": "update_pages", "titles": ["A"]}}
        self.manager._execute_page_update_subtask(subtask)

        self.assertEqual(subtask["payload"]["error"], "network exploded")
        self.assertEqual(followup_calls, [])  # skipped since update_page_records failed

    def test_deep_batch_triggers_child_page_task(self):
        self.manager._updater = SimpleNamespace(
            update_page_records=lambda titles, update_doc_metadata: True
        )
        child_calls = []
        self.manager._start_child_page_update_task = lambda titles: child_calls.append(titles)

        subtask = {"task_id": "t1", "index": 0, "payload": {"kind": "update_pages", "titles": ["A"], "deep": True}}
        self.manager._execute_page_update_subtask(subtask)

        self.assertEqual(child_calls, [["A"]])


class TestExecuteDocUpdateSubtask(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()
        self.progress_calls = []
        self.manager._update_progress = lambda tid, inc: self.progress_calls.append((tid, inc))

    def test_success_path(self):
        self.manager._updater = SimpleNamespace(
            update_doc_records=lambda urls, update_doc_metadata: True
        )
        subtask = {"task_id": "t1", "index": 0, "payload": {"kind": "update_docs", "urls": ["u1", "u2"]}}
        self.manager._execute_doc_update_subtask(subtask)

        self.assertEqual(subtask["payload"]["updated"], True)
        self.assertNotIn("error", subtask["payload"])
        self.assertEqual(self.progress_calls, [("t1", 2)])

    def test_failure_path(self):
        def boom(urls, update_doc_metadata):
            raise RuntimeError("timeout")

        self.manager._updater = SimpleNamespace(update_doc_records=boom)
        subtask = {"task_id": "t1", "index": 0, "payload": {"kind": "update_docs", "urls": ["u1"]}}
        self.manager._execute_doc_update_subtask(subtask)

        self.assertEqual(subtask["payload"]["error"], "timeout")
        self.assertNotIn("updated", subtask["payload"])


class TestUpdateDocMetadataHelper(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.manager = make_manager()

    def test_small_doc_set_batched_directly(self):
        page_url = _page_urls_from_titles("Архів:ДАЖО/Д")
        self.db.lookup_map["Pages"] = {page_url: "p1"}
        self.db.links[("Pages", "doc_links", "p1")] = {"d1"}
        self.db.records["Documents"] = {"d1": {"url": "https://x/1"}}

        recorded = []
        self.manager._updater = SimpleNamespace(
            _db=self.db,
            update_doc_records=lambda urls, update_doc_metadata: recorded.append(urls),
        )

        self.manager._update_doc_metadata(["Архів:ДАЖО/Д"])
        self.assertEqual(recorded, [["https://x/1"]])

    def test_large_doc_set_creates_batched_task_instead(self):
        page_url = _page_urls_from_titles("Архів:ДАЖО/Д")
        self.db.lookup_map["Pages"] = {page_url: "p1"}
        doc_ids = {f"d{i}" for i in range(10)}
        self.db.links[("Pages", "doc_links", "p1")] = doc_ids
        self.db.records["Documents"] = {did: {"url": f"https://x/{did}"} for did in doc_ids}

        created_tasks = []
        self.manager._create_task = lambda *args, **kw: created_tasks.append((args, kw))
        recorded = []
        self.manager._updater = SimpleNamespace(
            _db=self.db,
            update_doc_records=lambda urls, update_doc_metadata: recorded.append(urls),
        )

        self.manager._update_doc_metadata(["Архів:ДАЖО/Д"])

        self.assertEqual(recorded, [])  # not handled inline
        self.assertEqual(len(created_tasks), 1)

    def test_lookup_error_for_one_title_does_not_abort_others(self):
        page_url = _page_urls_from_titles("Архів:ДАЖО/Д")
        self.db.lookup_map["Pages"] = {page_url: "p1"}
        self.db.links[("Pages", "doc_links", "p1")] = {"d1"}
        self.db.records["Documents"] = {"d1": {"url": "https://x/1"}}

        recorded = []
        self.manager._updater = SimpleNamespace(
            _db=self.db,
            update_doc_records=lambda urls, update_doc_metadata: recorded.append(urls),
        )

        self.manager._update_doc_metadata(["Архів:Unknown/Title", "Архів:ДАЖО/Д"])
        self.assertEqual(recorded, [["https://x/1"]])


class TestUpdateProgress(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_increments_completed_count(self):
        self.manager._state_store.insert(
            "db_update", "taskA", '{"completed": 5, "total": 10}'
        )
        self.manager.lookup_task = lambda task_id: {"name": "taskA"}

        self.manager._update_progress("t1", 3)

        import json
        state = json.loads(self.manager._state_store.get("db_update", "taskA"))
        self.assertEqual(state["completed"], 8)

    def test_missing_task_is_silently_ignored(self):
        def raise_key_error(task_id):
            raise KeyError(task_id)

        self.manager.lookup_task = raise_key_error
        # must not raise
        self.manager._update_progress("gone", 1)


class TestCompleteTaskAndStatus(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_complete_task_removes_state(self):
        self.manager._state_store.insert("db_update", "taskA", '{"completed": 1}')
        self.manager.complete_task({"task_id": "t1", "name": "taskA"}, [])
        with self.assertRaises(KeyError):
            self.manager._state_store.get("db_update", "taskA")

    def test_complete_task_missing_state_does_not_raise(self):
        self.manager.complete_task({"task_id": "t1", "name": "unknown"}, [])

    def test_status_parses_all_entries(self):
        self.manager._state_store.insert("db_update", "taskA", '{"completed": 1}')
        self.manager._state_store.insert("db_update", "taskB", '{"completed": 2}')
        result = self.manager.status()
        self.assertEqual(result, {"taskA": {"completed": 1}, "taskB": {"completed": 2}})


class TestCancel(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_cancels_matching_active_task(self):
        self.manager._state_store.insert("db_update", "taskA", '{"completed": 0}')
        self.manager.active_tasks = lambda: [{"name": "taskA", "task_id": "t1"}]

        with patch.object(TaskManager, "cancel") as mock_cancel:
            self.manager.cancel("taskA")

        # patch.object replaces the class attribute with a plain Mock, which
        # (unlike a real function) isn't a descriptor, so super().cancel(...)
        # does not get self auto-bound
        mock_cancel.assert_called_once_with("t1")
        with self.assertRaises(KeyError):
            self.manager._state_store.get("db_update", "taskA")

    def test_no_matching_task_still_clears_state(self):
        self.manager._state_store.insert("db_update", "taskA", '{"completed": 0}')
        self.manager.active_tasks = lambda: []

        with patch.object(TaskManager, "cancel") as mock_cancel:
            self.manager.cancel("taskA")

        mock_cancel.assert_not_called()


class TestUpdateDocumentMetadataManager(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_starts_update_when_docs_missing_metadata(self):
        self.manager._updater = SimpleNamespace(
            get_docs_with_missing_metadata=lambda limit: ["https://x/1"]
        )
        recorded = []
        self.manager.start_document_update = lambda urls: recorded.append(urls)

        self.manager.update_document_metadata()
        self.assertEqual(recorded, [["https://x/1"]])

    def test_noop_when_nothing_missing(self):
        self.manager._updater = SimpleNamespace(get_docs_with_missing_metadata=lambda limit: [])
        recorded = []
        self.manager.start_document_update = lambda urls: recorded.append(urls)

        self.manager.update_document_metadata()
        self.assertEqual(recorded, [])


if __name__ == "__main__":
    unittest.main()
