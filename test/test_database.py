import os
import time
import unittest

from birddog.abstract_database import (
    FailedIO,
    SchemaError,
    InvalidFieldName,
    InvalidFieldValue,
    InvalidRecordId,
    InvalidTableName,
    InvalidViewName,
    MissingKey,
)

from birddog.database import Database


class TestDatabase(unittest.TestCase):
    """
    Integration-style unit test for the NocoDBDatabase backend.

    This test exercises:
      - scan (Pages, Documents)
      - lookup (Pages.url, Documents.link)
      - read (single and batch by Id)
      - write (create + update; single + batch)
      - create_links / delete_links / get_links (Pages.parent, Pages.children, Documents.owning_pages)

    Cleanup:
      - Deletes any records created during the test (tracked by returned Ids).
      - All created records contain "test" in title (and Documents.link / Pages.url).
    """

    def setUp(self):
        self.db = Database()
        self.created_page_ids = []
        self.created_doc_ids = []
        self._run_suffix = f"test_unittest_{int(time.time())}"

    def tearDown(self):
        # Best-effort cleanup; do not fail the test due to cleanup issues.
        try:
            if self.created_doc_ids:
                try:
                    self.db.delete("Documents", self.created_doc_ids)
                except Exception:
                    pass
            if self.created_page_ids:
                try:
                    self.db.delete("Pages", self.created_page_ids)
                except Exception:
                    pass
        finally:
            self.created_doc_ids = []
            self.created_page_ids = []

    def _mk_page_title(self, name: str) -> str:
        return f"test {self._run_suffix} {name}"

    def _mk_page_url(self, name: str) -> str:
        safe_name = name.replace(" ", "_")
        return f"http://test.page/{self._run_suffix}/{safe_name}"

    def _mk_doc_link(self, i: int) -> str:
        # Ensure "test" is present and uniqueness is guaranteed.
        return f"http://test.doc/{self._run_suffix}/{i}"

    def test_database_pages_documents(self):
        # -----------------------
        # 1) scan: Pages + Documents
        # -----------------------
        pages, next_cursor = self.db.scan("Pages", limit=5)
        self.assertIsInstance(pages, list)
        self.assertTrue(len(pages) <= 5)

        docs, next_cursor = self.db.scan("Documents", limit=5)
        self.assertIsInstance(docs, list)
        self.assertTrue(len(docs) <= 5)

        # -----------------------
        # 2) write + read + update: single Page
        # -----------------------
        title_one = self._mk_page_title("one")
        url_one = self._mk_page_url("one")
        page = {"title": title_one, "url": url_one, "comments": "foo"}

        page_id = self.db.write("Pages", page)
        self.assertTrue(page_id)
        self.created_page_ids.append(page_id)

        read_back = self.db.read("Pages", page_id)
        self.assertIsInstance(read_back, dict)
        self.assertEqual(read_back.get("title"), title_one)
        self.assertEqual(read_back.get("url"), url_one)
        self.assertEqual(read_back.get("comments"), "foo")

        # update (read/modify/write workflow)
        read_back["comments"] = "bar"
        page_id2 = self.db.write("Pages", read_back)
        self.assertEqual(page_id2, page_id)

        read_back2 = self.db.read("Pages", page_id)
        self.assertEqual(read_back2.get("comments"), "bar")

        # lookup by key (Pages.url)
        lookup_id = self.db.lookup("Pages", url_one)
        self.assertEqual(lookup_id, page_id)

        # -----------------------
        # 2b) lookup with sequences: Pages.url (order, duplicates, empty, missing)
        # -----------------------
        seq_title_a = self._mk_page_title("seq_a")
        seq_title_b = self._mk_page_title("seq_b")
        seq_title_c = self._mk_page_title("seq_c")

        seq_url_a = self._mk_page_url("seq_a")
        seq_url_b = self._mk_page_url("seq_b")
        seq_url_c = self._mk_page_url("seq_c")

        seq_id_a = self.db.write("Pages", {"title": seq_title_a, "url": seq_url_a})
        seq_id_b = self.db.write("Pages", {"title": seq_title_b, "url": seq_url_b})
        seq_id_c = self.db.write("Pages", {"title": seq_title_c, "url": seq_url_c})
        self.created_page_ids.extend([seq_id_a, seq_id_b, seq_id_c])

        # (a) Empty sequence -> empty list
        self.assertEqual(self.db.lookup("Pages", []), [])

        # (b) Order preserved + duplicates preserved
        seq_keys = [seq_url_b, seq_url_a, seq_url_b, seq_url_c]
        seq_expected = [seq_id_b, seq_id_a, seq_id_b, seq_id_c]
        self.assertEqual(self.db.lookup("Pages", seq_keys), seq_expected)

        # (c) Missing key yields None in corresponding position
        missing_key = self._mk_page_url("seq_missing_does_not_exist")
        seq_keys2 = [seq_url_a, missing_key, seq_url_c, missing_key]
        seq_expected2 = [seq_id_a, None, seq_id_c, None]
        self.assertEqual(self.db.lookup("Pages", seq_keys2), seq_expected2)

        # -----------------------
        # 3) batch write + batch read: Pages
        # -----------------------
        batch_pages = [
            {
                "title": self._mk_page_title(f"page_{i}"),
                "url": self._mk_page_url(f"page_{i}"),
            }
            for i in range(10)
        ]
        batch_ids = self.db.write("Pages", batch_pages)
        self.assertEqual(len(batch_ids), len(batch_pages))
        self.assertTrue(all(batch_ids))

        self.created_page_ids.extend(batch_ids)

        batch_read = self.db.read("Pages", batch_ids)
        self.assertEqual(len(batch_read), len(batch_ids))
        read_titles = {rec.get("title") for rec in batch_read if rec}
        expected_titles = {p["title"] for p in batch_pages}
        self.assertTrue(expected_titles.issubset(read_titles))

        # -----------------------
        # 4) batch write: Documents (with multiselect doc_type as list)
        # -----------------------
        batch_docs = [
            {
                "link": self._mk_doc_link(i),
                "title": f"test {self._run_suffix} doc_{i}",
                "doc_type": ["O", "C", "L", "V"],
            }
            for i in range(5)
        ]
        doc_ids = self.db.write("Documents", batch_docs)
        self.assertEqual(len(doc_ids), len(batch_docs))
        self.assertTrue(all(doc_ids))

        self.created_doc_ids.extend(doc_ids)

        # lookup Documents by link
        for i in range(5):
            link = self._mk_doc_link(i)
            did = self.db.lookup("Documents", link)
            self.assertTrue(did)

        # -----------------------
        # 4b) lookup with sequences: Documents.link (order, duplicates, empty, missing)
        # -----------------------
        self.assertEqual(self.db.lookup("Documents", []), [])

        link0 = self._mk_doc_link(0)
        link1 = self._mk_doc_link(1)
        link2 = self._mk_doc_link(2)
        missing_link = f"http://test.doc/{self._run_suffix}/does_not_exist"

        # Ensure the existing ones are resolvable (integration timing / consistency)
        id0 = self.db.lookup("Documents", link0)
        id1 = self.db.lookup("Documents", link1)
        id2 = self.db.lookup("Documents", link2)
        self.assertTrue(id0 and id1 and id2)

        seq_links = [link1, link0, link1, missing_link, link2]
        seq_ids = self.db.lookup("Documents", seq_links)

        self.assertEqual(seq_ids, [id1, id0, id1, None, id2])

        # -----------------------
        # 5) Links: Pages.parent (child -> parent)
        # -----------------------
        parent_title = self._mk_page_title("parent")
        child_title = self._mk_page_title("child")
        parent_url = self._mk_page_url("parent")
        child_url = self._mk_page_url("child")

        parent_id = self.db.write("Pages", {"title": parent_title, "url": parent_url})
        child_id = self.db.write("Pages", {"title": child_title, "url": child_url})
        self.created_page_ids.extend([parent_id, child_id])

        self.db.create_links("Pages", "parent", child_id, parent_id)
        parent_links = self.db.get_links("Pages", "parent", child_id)
        self.assertIsInstance(parent_links, list)
        self.assertIn(parent_id, parent_links)

        # -----------------------
        # 6) Links: Pages.children (parent -> children; include list form)
        # -----------------------
        child2_title = self._mk_page_title("child2")
        child3_title = self._mk_page_title("child3")
        child2_url = self._mk_page_url("child2")
        child3_url = self._mk_page_url("child3")

        child2_id = self.db.write("Pages", {"title": child2_title, "url": child2_url})
        child3_id = self.db.write("Pages", {"title": child3_title, "url": child3_url})
        self.created_page_ids.extend([child2_id, child3_id])

        # link single
        self.db.create_links("Pages", "children", parent_id, child2_id)
        # link batch
        self.db.create_links("Pages", "children", parent_id, [child3_id])

        children_links = self.db.get_links("Pages", "children", parent_id)
        self.assertIn(child2_id, children_links)
        self.assertIn(child3_id, children_links)

        # unlink one child and verify removal
        self.db.delete_links("Pages", "children", parent_id, child3_id)
        children_links2 = self.db.get_links("Pages", "children", parent_id)
        self.assertIn(child2_id, children_links2)
        self.assertNotIn(child3_id, children_links2)

        # -----------------------
        # 7) Links: Documents.owning_pages (doc -> page)
        # -----------------------
        first_doc_link = self._mk_doc_link(0)
        first_doc_id = self.db.lookup("Documents", first_doc_link)
        self.assertTrue(first_doc_id)

        self.db.create_links("Documents", "owning_pages", first_doc_id, parent_id)
        owning_links = self.db.get_links("Documents", "owning_pages", first_doc_id)
        self.assertIn(parent_id, owning_links)

        # Remove and verify it disappears
        self.db.delete_links("Documents", "owning_pages", first_doc_id, parent_id)
        owning_links2 = self.db.get_links("Documents", "owning_pages", first_doc_id)
        self.assertNotIn(parent_id, owning_links2)


    def test_scan_with_view_name(self):
        # -----------------------
        # Discover available views for the Pages table, then scan using one.
        # -----------------------
        views = self.db._list_views("Pages")
        self.assertIsInstance(views, dict)
        self.assertTrue(views, "Expected at least one view defined for the Pages table")

        # Pick the first available view and scan it.
        view_name = next(iter(views))
        records, cursor = self.db.scan("Pages", limit=5, view_name=view_name)
        self.assertIsInstance(records, list)
        self.assertLessEqual(len(records), 5)

        # scan_all with view_name should also work.
        all_records = self.db.scan_all("Pages", view_name=view_name)
        self.assertIsInstance(all_records, list)

        # If the view returned pages, scan_all should return at least as many
        # as the first page.
        self.assertGreaterEqual(len(all_records), len(records))

        # -----------------------
        # An unknown view name must raise InvalidViewName.
        # -----------------------
        with self.assertRaises(InvalidViewName):
            self.db.scan("Pages", view_name="__nonexistent_view__")


if __name__ == "__main__":
    unittest.main()
