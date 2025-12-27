import os
import time
import unittest

from birddog.database import (
    Database,
    FailedIO,
    SchemaError,
    InvalidFieldName,
    InvalidFieldValue,
    InvalidRecordId,
    InvalidTableName,
    MissingKey,
)

from birddog.nocodb_database import NocoDBDatabase


class TestNocoDBDatabase(unittest.TestCase):
    """
    Integration-style unit test for the NocoDBDatabase backend.

    This test exercises:
      - scan (Pages, Documents)
      - lookup (Pages.title, Documents.link)
      - read (single and batch by Id)
      - write (create + update; single + batch)
      - create_links / delete_links / get_links (Pages.parent, Pages.children, Documents.owning_page)

    Cleanup:
      - Deletes any records created during the test (tracked by returned Ids).
      - All created records contain "test" in title (and Documents.link).
    """

    @classmethod
    def setUpClass(cls):
        # Skip if the token is not configured; this is an integration test.
        if not os.environ.get("NOCODB_API_TOKEN"):
            raise unittest.SkipTest("NOCODB_API_TOKEN is not set in environment.")

    def setUp(self):
        self.db = NocoDBDatabase()
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

    def _mk_doc_link(self, i: int) -> str:
        # Ensure "test" is present and uniqueness is guaranteed.
        return f"http://test.doc/{self._run_suffix}/{i}"

    def test_nocodb_database_pages_documents(self):
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
        page = {"title": title_one, "comments": "foo"}

        page_id = self.db.write("Pages", page)
        self.assertTrue(page_id)
        self.created_page_ids.append(page_id)

        read_back = self.db.read("Pages", page_id)
        self.assertIsInstance(read_back, dict)
        self.assertEqual(read_back.get("title"), title_one)
        self.assertEqual(read_back.get("comments"), "foo")

        # update (read/modify/write workflow)
        read_back["comments"] = "bar"
        page_id2 = self.db.write("Pages", read_back)
        self.assertEqual(page_id2, page_id)

        read_back2 = self.db.read("Pages", page_id)
        self.assertEqual(read_back2.get("comments"), "bar")

        # lookup by key
        lookup_id = self.db.lookup("Pages", title_one)
        self.assertEqual(lookup_id, page_id)

        # -----------------------
        # 3) batch write + batch read: Pages
        # -----------------------
        batch_pages = [{"title": self._mk_page_title(f"page_{i}")} for i in range(10)]
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
        # 5) Links: Pages.parent (child -> parent)
        # -----------------------
        parent_title = self._mk_page_title("parent")
        child_title = self._mk_page_title("child")

        parent_id = self.db.write("Pages", {"title": parent_title})
        child_id = self.db.write("Pages", {"title": child_title})
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
        child2_id = self.db.write("Pages", {"title": child2_title})
        child3_id = self.db.write("Pages", {"title": child3_title})
        self.created_page_ids.extend([child2_id, child3_id])

        # link single
        self.db.create_links("Pages", "children", parent_id, child2_id)
        # link batch
        self.db.create_links("Pages", "children", parent_id, [child3_id])

        children_links = self.db.get_links("Pages", "children", parent_id)
        self.assertIn(child2_id, children_links)
        self.assertIn(child3_id, children_links)

        # unlink one child and verify removal (idempotence/behavior is backend-defined;
        # we just require that it no longer appears if backend applies the delete)
        self.db.delete_links("Pages", "children", parent_id, child3_id)
        children_links2 = self.db.get_links("Pages", "children", parent_id)
        self.assertIn(child2_id, children_links2)
        self.assertNotIn(child3_id, children_links2)

        # -----------------------
        # 7) Links: Documents.owning_page (doc -> page)
        # -----------------------
        # Link first document to parent page.
        first_doc_link = self._mk_doc_link(0)
        first_doc_id = self.db.lookup("Documents", first_doc_link)
        self.assertTrue(first_doc_id)

        self.db.create_links("Documents", "owning_page", first_doc_id, parent_id)
        owning_links = self.db.get_links("Documents", "owning_page", first_doc_id)
        self.assertIn(parent_id, owning_links)

        # Remove and verify it disappears (if backend enforces)
        self.db.delete_links("Documents", "owning_page", first_doc_id, parent_id)
        owning_links2 = self.db.get_links("Documents", "owning_page", first_doc_id)
        self.assertNotIn(parent_id, owning_links2)


if __name__ == "__main__":
    unittest.main()
