import os
import time
import unittest
from unittest.mock import patch

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
      - lookup (Pages.url, Documents.url)
      - read (single and batch by Id)
      - write (create + update; single + batch)
      - create_links / delete_links / get_links (Pages.parent, Pages.children, Documents.owning_pages)

    Cleanup:
      - Deletes any records created during the test (tracked by returned Ids).
      - All created records contain "test" in title (and Documents.url / Pages.url).
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
        # 4) batch write: Documents (with multiselect OLD_doc_type as list)
        # -----------------------
        batch_docs = [
            {
                "url": self._mk_doc_link(i),
                "title": f"test {self._run_suffix} doc_{i}",
                "OLD_doc_type": ["O", "C", "L", "V"],
            }
            for i in range(5)
        ]
        doc_ids = self.db.write("Documents", batch_docs)
        self.assertEqual(len(doc_ids), len(batch_docs))
        self.assertTrue(all(doc_ids))

        self.created_doc_ids.extend(doc_ids)

        # lookup Documents by url
        for i in range(5):
            url = self._mk_doc_link(i)
            did = self.db.lookup("Documents", url)
            self.assertTrue(did)

        # -----------------------
        # 4b) lookup with sequences: Documents.url (order, duplicates, empty, missing)
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

        # filtered get_links returns only the requested targets that are linked
        missing_id = max(child2_id, child3_id) + 1000000
        subset = self.db.get_links("Pages", "children", parent_id, target_ids=[child3_id, missing_id])
        self.assertEqual(subset, [child3_id])
        self.assertEqual(self.db.get_links("Pages", "children", parent_id, target_ids=[]), [])

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


    def test_parallel_read_correctness(self):
        """
        Verifies parallelized read preserves input order, maps missing IDs to {},
        handles duplicates, and returns the right shape for singleton vs sequence input.

        _NOCODB_BATCH_SIZE is patched to 3 so that 7 IDs produce three batches
        and trigger the ThreadPoolExecutor path without creating hundreds of records.
        """
        import birddog.nocodb_database as ndb_mod

        n = 7
        pages = [
            {"title": self._mk_page_title(f"rd_{i}"),
             "url":   self._mk_page_url(f"rd_{i}")}
            for i in range(n)
        ]
        ids = self.db.write("Pages", pages)
        self.created_page_ids.extend(ids)

        # singleton input must return a plain dict, not a list
        single = self.db.read("Pages", ids[0])
        self.assertIsInstance(single, dict)
        self.assertEqual(single.get("title"), self._mk_page_title("rd_0"))

        # sequence read with batch_size=3 → 3 batches → parallel execution
        # includes a duplicate, a missing ID, and non-sequential ordering
        _MISSING = 999_999_999
        read_order = [ids[4], ids[1], ids[6], _MISSING, ids[1], ids[0], ids[3]]
        expected = [
            self._mk_page_title("rd_4"),
            self._mk_page_title("rd_1"),
            self._mk_page_title("rd_6"),
            None,                            # missing -> {}
            self._mk_page_title("rd_1"),     # duplicate
            self._mk_page_title("rd_0"),
            self._mk_page_title("rd_3"),
        ]

        with patch.object(ndb_mod, "_NOCODB_BATCH_SIZE", 3):
            result = self.db.read("Pages", read_order)

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), len(read_order))
        for pos, (rec, title) in enumerate(zip(result, expected)):
            if title is None:
                self.assertEqual(rec, {}, f"position {pos}: expected {{}} for missing ID")
            else:
                self.assertIsInstance(rec, dict, f"position {pos}: expected dict")
                self.assertEqual(rec.get("title"), title, f"position {pos}: wrong title")

    def test_parallel_write_correctness(self):
        """
        Verifies correctness of the parallelized write path.

        A mixed batch of existing (PATCH) and new (POST) records produces two
        independent tasks that run concurrently via ThreadPoolExecutor.  The
        critical invariant is that each POST response ID is assigned to the
        correct record dict so that the final result list is aligned to the
        input order.
        """
        n_exist = 6
        n_new = 6

        # --- create initial records ---
        initial = [
            {"title": self._mk_page_title(f"pw_exist_{i}"),
             "url":   self._mk_page_url(f"pw_exist_{i}")}
            for i in range(n_exist)
        ]
        initial_ids = self.db.write("Pages", initial)
        self.created_page_ids.extend(initial_ids)

        # --- mixed batch: updates to all existing + all-new creates ---
        # This yields 1 PATCH task + 1 POST task => parallel execution.
        updates = [
            {"title":    self._mk_page_title(f"pw_exist_{i}"),
             "url":      self._mk_page_url(f"pw_exist_{i}"),
             "comments": f"updated_{i}"}
            for i in range(n_exist)
        ]
        creates = [
            {"title": self._mk_page_title(f"pw_new_{i}"),
             "url":   self._mk_page_url(f"pw_new_{i}")}
            for i in range(n_new)
        ]
        mixed = updates + creates
        result_ids = self.db.write("Pages", mixed)
        new_ids = result_ids[n_exist:]
        self.created_page_ids.extend(new_ids)

        # result must be aligned to input order
        self.assertEqual(len(result_ids), len(mixed))

        # existing records must keep their original IDs
        self.assertEqual(result_ids[:n_exist], initial_ids)

        # new IDs must be non-null and distinct
        self.assertEqual(len(new_ids), n_new)
        self.assertTrue(all(new_ids), "some new IDs are falsy")
        self.assertEqual(len(set(new_ids)), n_new, "new IDs are not all distinct")

        # PATCH path: updates must have been applied
        updated_recs = self.db.read("Pages", initial_ids)
        for i, rec in enumerate(updated_recs):
            self.assertEqual(rec.get("comments"), f"updated_{i}",
                             f"update not applied for pw_exist_{i}")

        # POST path: each new ID must resolve to the correct record
        # (verifies batch[j]["Id"] = item["Id"] assigned the right ID to the
        # right dict, regardless of which parallel worker handled it)
        new_recs = self.db.read("Pages", new_ids)
        for i, rec in enumerate(new_recs):
            self.assertEqual(rec.get("title"), self._mk_page_title(f"pw_new_{i}"),
                             f"wrong record at new_ids[{i}]")

    def test_concurrent_write_no_duplication(self):
        """
        Two threads simultaneously write batches that share N_SHARED URLs, each
        writing a different field on the shared records.  The batch size ensures
        both threads are inside their NocoDB lookup calls at the same time,
        creating a genuine concurrent check-then-create window.

        Assertions:
          - Each shared URL resolves to the same record ID in both threads
            (a duplicate would yield two distinct IDs).
          - Every shared record contains fields written by both threads,
            confirming the losing thread completed as a PATCH not a POST.
          - Thread-exclusive records are created independently without
            interference (verifies the Reserver does not over-serialise).
        """
        import threading

        N_SHARED    = 5
        N_EXCLUSIVE = 4

        shared_urls   = [self._mk_page_url(f"conc_sh_{i}")  for i in range(N_SHARED)]
        shared_title  = self._mk_page_title("conc_shared")
        a_urls        = [self._mk_page_url(f"conc_a_{i}")   for i in range(N_EXCLUSIVE)]
        b_urls        = [self._mk_page_url(f"conc_b_{i}")   for i in range(N_EXCLUSIVE)]

        # Thread A writes shared records with `comments`, plus A-exclusive records.
        # Thread B writes shared records with `description`, plus B-exclusive records.
        # Shared records are listed first so result slicing is straightforward.
        a_records = (
            [{"url": u, "title": shared_title, "comments": "from_a"}
             for u in shared_urls] +
            [{"url": u, "title": self._mk_page_title(f"conc_a_{i}")}
             for i, u in enumerate(a_urls)]
        )
        b_records = (
            [{"url": u, "title": shared_title, "description": "from_b"}
             for u in shared_urls] +
            [{"url": u, "title": self._mk_page_title(f"conc_b_{i}")}
             for i, u in enumerate(b_urls)]
        )

        results = [None, None]
        errors  = [None, None]
        barrier = threading.Barrier(2)

        def writer(idx, records):
            try:
                barrier.wait()
                results[idx] = self.db.write("Pages", records)
            except Exception as exc:
                errors[idx] = exc

        t0 = threading.Thread(target=writer, args=(0, a_records))
        t1 = threading.Thread(target=writer, args=(1, b_records))
        t0.start()
        t1.start()
        t0.join(timeout=120)
        t1.join(timeout=120)

        # Track all returned IDs for tearDown cleanup before any assertions.
        # Collects both threads' IDs so duplicates are also cleaned up on failure.
        seen = set()
        for result in results:
            if result:
                for rid in result:
                    if rid and rid not in seen:
                        seen.add(rid)
                        self.created_page_ids.append(rid)

        self.assertIsNone(errors[0], f"Thread 0 raised: {errors[0]}")
        self.assertIsNone(errors[1], f"Thread 1 raised: {errors[1]}")
        self.assertIsNotNone(results[0], "Thread 0 returned no result")
        self.assertIsNotNone(results[1], "Thread 1 returned no result")
        self.assertEqual(len(results[0]), N_SHARED + N_EXCLUSIVE)
        self.assertEqual(len(results[1]), N_SHARED + N_EXCLUSIVE)

        a_shared_ids    = results[0][:N_SHARED]
        b_shared_ids    = results[1][:N_SHARED]
        a_exclusive_ids = results[0][N_SHARED:]
        b_exclusive_ids = results[1][N_SHARED:]

        # Shared URLs must map to identical record IDs in both threads —
        # a mismatch means a duplicate record was created.
        self.assertEqual(a_shared_ids, b_shared_ids,
                         "Duplicate records created for shared URLs: "
                         f"Thread 0 got {a_shared_ids}, Thread 1 got {b_shared_ids}")

        # Every shared record must contain the field written by each thread,
        # confirming the losing thread issued a PATCH rather than being dropped.
        shared_recs = self.db.read("Pages", a_shared_ids)
        for i, rec in enumerate(shared_recs):
            self.assertEqual(rec.get("comments"), "from_a",
                             f"shared_{i}: comments from Thread 0 missing or wrong")
            self.assertEqual(rec.get("description"), "from_b",
                             f"shared_{i}: description from Thread 1 missing or wrong")

        # Exclusive records must all be present and non-overlapping.
        self.assertTrue(all(a_exclusive_ids), "Thread 0 exclusive IDs contain falsy values")
        self.assertTrue(all(b_exclusive_ids), "Thread 1 exclusive IDs contain falsy values")
        self.assertEqual(len(set(a_exclusive_ids) & set(b_exclusive_ids)), 0,
                         "Thread-exclusive IDs overlap — unexpected shared record")

    def test_reservation_survives_slow_lookup(self):
        """
        Regression test for a specific duplicate-creation bug: _write_unique()
        reserves a key before checking whether it exists, but the reservation
        used to be a flat TTL with no renewal while the guarded lookup()/write
        was still in flight. If NocoDB was slow enough that a lookup() call
        (which retries on read timeout) outlived the reservation window, the
        reservation would silently expire mid-operation, letting a second
        writer for the same key reserve it, find nothing yet, and create a
        duplicate record — even though the first writer was still working.
        This was observed in production for wiki document URLs during a
        high-concurrency crawl (see nocodb_database.py's Reserver/_write_unique
        and the _touch_reservation heartbeat added to fix it).

        Reproduces the race deterministically: shrinks the reservation
        interval/touch cadence so the test runs fast, then forces exactly one
        writer's lookup() call for a shared key to run for several multiples
        of the (shrunk) reservation window. Without the heartbeat renewing the
        reservation during that delay, the second, concurrently-retrying
        writer would see the key as free partway through and create a
        duplicate. With the fix, it must stay rejected until the first writer
        actually finishes.
        """
        import threading
        import birddog.nocodb_database as ncdb

        shared_url = self._mk_page_url("reservation_race")
        title = self._mk_page_title("reservation_race")

        # Shrink the reservation window and heartbeat cadence so the delay
        # below only has to be a second or so, not 30+ real seconds, while
        # still being several multiples of the (shrunk) window — i.e. the
        # reservation would have expired at least 3-4 times over without the
        # heartbeat keeping it alive.
        test_interval = 0.3
        test_touch_interval = 0.1
        lookup_delay = 1.2

        real_lookup = ncdb.NocoDBDatabase.lookup
        delay_event = threading.Event()

        def slow_lookup(self_db, table_name, key_set, use_in_clause=True):
            # Only delay the *first* caller that looks up the shared key —
            # that's whichever thread wins the initial reservation. The
            # losing thread must stay rejected in the collision-retry loop
            # and never reach lookup() until the winner releases.
            if (table_name == "Pages" and isinstance(key_set, set)
                    and shared_url in key_set and not delay_event.is_set()):
                delay_event.set()
                time.sleep(lookup_delay)
            return real_lookup(self_db, table_name, key_set, use_in_clause=use_in_clause)

        barrier = threading.Barrier(2)
        results = [None, None]
        errors = [None, None]

        def writer(idx, comments_value):
            try:
                barrier.wait()
                results[idx] = self.db.write(
                    "Pages",
                    {"url": shared_url, "title": title, "comments": comments_value},
                )
            except Exception as exc:
                errors[idx] = exc

        with patch.object(ncdb.NocoDBDatabase, "lookup", slow_lookup), \
             patch.object(ncdb, "_WRITE_RESERVATION_INTERVAL", test_interval), \
             patch.object(ncdb, "_WRITE_RESERVATION_TOUCH_INTERVAL", test_touch_interval):
            t0 = threading.Thread(target=writer, args=(0, "from_a"))
            t1 = threading.Thread(target=writer, args=(1, "from_b"))
            t0.start()
            t1.start()
            t0.join(timeout=30)
            t1.join(timeout=30)

        for rid in results:
            if rid and rid not in self.created_page_ids:
                self.created_page_ids.append(rid)

        self.assertIsNone(errors[0], f"Thread 0 raised: {errors[0]}")
        self.assertIsNone(errors[1], f"Thread 1 raised: {errors[1]}")
        self.assertIsNotNone(results[0], "Thread 0 returned no result")
        self.assertIsNotNone(results[1], "Thread 1 returned no result")
        self.assertEqual(
            results[0], results[1],
            "Reservation expired mid-lookup and let a second writer create a "
            f"duplicate: thread 0 got {results[0]}, thread 1 got {results[1]}"
        )

        # Direct ground-truth check, independent of the id-equality assertion
        # above: exactly one row must exist for this url.
        matches, _ = self.db.scan("Pages", where=("url", "eq", shared_url))
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one Pages record for {shared_url!r}, found {len(matches)}"
        )

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
        #all_records = self.db.scan_all("Pages", view_name=view_name)
        #self.assertIsInstance(all_records, list)

        # If the view returned pages, scan_all should return at least as many
        # as the first page.
        #self.assertGreaterEqual(len(all_records), len(records))

        # -----------------------
        # An unknown view name must raise InvalidViewName.
        # -----------------------
        with self.assertRaises(InvalidViewName):
            self.db.scan("Pages", view_name="__nonexistent_view__")

    def test_scan_use_v3_matches_v2(self):
        """
        scan(use_v3=True) hits the v3 records endpoint and flattens the
        {"fields": {...}} shape back to the v2 flat-dict shape. Verify both
        paths return the same records for an identical query.
        """
        n = 6
        pages = [
            {"title": self._mk_page_title(f"v3_{i}"),
             "url":   self._mk_page_url(f"v3_{i}")}
            for i in range(n)
        ]
        ids = self.db.write("Pages", pages)
        self.created_page_ids.extend(ids)

        urls = [p["url"] for p in pages]
        where = ("url", "in", urls)

        v2_records, v2_cursor = self.db.scan("Pages", limit=100, where=where, use_v3=False)
        v3_records, v3_cursor = self.db.scan("Pages", limit=100, where=where, use_v3=True)

        self.assertIsNone(v2_cursor)
        self.assertIsNone(v3_cursor)
        self.assertEqual(len(v2_records), n)
        self.assertEqual(len(v3_records), n)

        v2_by_id = {rec["Id"]: rec for rec in v2_records}
        v3_by_id = {rec["Id"]: rec for rec in v3_records}
        self.assertEqual(set(v2_by_id), set(ids))
        self.assertEqual(set(v3_by_id), set(ids))

        for rid in ids:
            v2_rec = v2_by_id[rid]
            v3_rec = v3_by_id[rid]
            self.assertEqual(v3_rec.get("title"), v2_rec.get("title"))
            self.assertEqual(v3_rec.get("url"), v2_rec.get("url"))

    def test_scan_use_v3_linked_field(self):
        """
        scan(use_v3=True) requesting a link field (Pages.children) must return
        a flat list of linked record ids -- _flatten_v3_record's
        _reduce_link_value collapses the nested {"id": ...} objects returned
        by the v3 API down to the same plain-id-list shape the v2 path uses.
        """
        parent_title = self._mk_page_title("v3link_parent")
        parent_url = self._mk_page_url("v3link_parent")
        child_title = self._mk_page_title("v3link_child")
        child_url = self._mk_page_url("v3link_child")

        parent_id = self.db.write("Pages", {"title": parent_title, "url": parent_url})
        child_id = self.db.write("Pages", {"title": child_title, "url": child_url})
        self.created_page_ids.extend([parent_id, child_id])

        self.db.create_links("Pages", "children", parent_id, child_id)

        records, cursor = self.db.scan(
            "Pages",
            where=("Id", "eq", parent_id),
            fields=["title", "children"],
            use_v3=True,
        )
        self.assertIsNone(cursor)
        self.assertEqual(len(records), 1)

        record = records[0]
        self.assertEqual(record.get("Id"), parent_id)
        self.assertIsInstance(record.get("children"), list)
        self.assertIn(child_id, record.get("children"))

    def test_scan_links_treats_invalid_offset_as_end_of_page(self):
        """
        NocoDB Cloud rejects offset >= total linked records with HTTP 422
        ERR_INVALID_OFFSET_VALUE instead of returning an empty page (which is
        what self-hosted NocoDB does). scan_links() must treat that the same
        way -- end of pagination, not a fatal error -- since it's reachable
        in production whenever NocoDB reports isLastPage=False on what turns
        out to be the last page.
        """
        import requests

        # Warm the table/field id caches for real so scan_links's own lookups
        # below don't go through the patched _fetch.
        self.db._table_id("Pages")
        self.db._field_id("Pages", "children")

        resp = requests.Response()
        resp.status_code = 422
        resp._content = b'{"error":"ERR_INVALID_OFFSET_VALUE","message":"Offset value \'100\' is invalid"}'
        http_err = requests.exceptions.HTTPError("HTTP 422: ...", response=resp)

        def fake_fetch(*args, **kwargs):
            raise FailedIO(http_err) from http_err

        with patch.object(self.db, "_fetch", side_effect=fake_fetch):
            link_ids, cursor = self.db.scan_links("Pages", "children", 1, cursor="100")

        self.assertEqual(link_ids, [])
        self.assertIsNone(cursor)

    def test_get_links_with_target_ids_filters_and_chunks(self):
        """
        get_links(target_ids=...) must push an "Id in (...)" filter to the server
        instead of paging the full link set, and split large id lists into
        requests of at most _GET_LINKS_TARGET_CHUNK_SIZE ids.
        """
        from birddog.nocodb_database import _GET_LINKS_TARGET_CHUNK_SIZE

        # Warm the table/field id caches for real so the lookups below don't
        # go through the patched _fetch.
        self.db._table_id("Pages")
        self.db._field_id("Pages", "children")

        requested = list(range(1, 2 * _GET_LINKS_TARGET_CHUNK_SIZE + 2))
        linked = {5, _GET_LINKS_TARGET_CHUNK_SIZE + 7, 2 * _GET_LINKS_TARGET_CHUNK_SIZE + 1}
        wheres = []

        def fake_fetch(url, params=None, **kwargs):
            where = params["where"]
            wheres.append(where)
            self.assertTrue(where.startswith("(Id,in,") and where.endswith(")"))
            ids = [int(x) for x in where[len("(Id,in,"):-1].split(",")]
            hits = [{"Id": i} for i in ids if i in linked]
            return {"list": hits, "pageInfo": {"isLastPage": True}}

        with patch.object(self.db, "_fetch", side_effect=fake_fetch):
            result = self.db.get_links("Pages", "children", 1, target_ids=requested)

        self.assertEqual(set(result), linked)
        self.assertEqual(len(wheres), 3)
        chunk_sizes = [len(w[len("(Id,in,"):-1].split(",")) for w in wheres]
        self.assertTrue(all(n <= _GET_LINKS_TARGET_CHUNK_SIZE for n in chunk_sizes))
        self.assertEqual(sum(chunk_sizes), len(requested))

    def _mk_pages(self, names):
        ids = [self.db.write("Pages", {"title": self._mk_page_title(n), "url": self._mk_page_url(n)})
               for n in names]
        self.created_page_ids.extend(ids)
        return ids

    def _page_exists(self, page_id):
        return bool(self.db.read("Pages", page_id))

    def test_count(self):
        before = self.db.count("Pages")
        self.assertIsInstance(before, int)
        ids = self._mk_pages(["count a", "count b"])
        self.assertEqual(self.db.count("Pages"), before + 2)
        self.db.delete("Pages", ids)
        self.assertEqual(self.db.count("Pages"), before)
        with self.assertRaises(InvalidTableName):
            self.db.count("No Such Table")

    def test_update_changes_existing_and_never_creates(self):
        live, = self._mk_pages(["upd live"])
        missing_url = self._mk_page_url("upd never existed")
        before = self.db.count("Pages")

        result = self.db.update("Pages", [
            {"url": self._mk_page_url("upd live"), "description": "updated"},
            {"url": missing_url, "description": "should not exist"},
        ])

        self.assertEqual(result, [live, None])
        self.assertEqual(self.db.read("Pages", live)["description"], "updated")
        self.assertIsNone(self.db.lookup("Pages", missing_url))
        self.assertEqual(self.db.count("Pages"), before)
        self.assertIsNone(self.db.update("Pages", {"url": missing_url, "description": "x"}))

    def _stale_lookup(self, stale):
        """lookup() that still reports keys in stale (key -> old Id) after
        their records were deleted: the window between lookup and PATCH."""
        real_lookup = self.db.lookup

        def lookup(table_name, key_set):
            result = real_lookup(table_name, key_set)
            if isinstance(key_set, set):
                result.update({k: v for k, v in stale.items() if k in key_set})
            return result
        return patch.object(self.db, "lookup", side_effect=lookup)

    def test_update_skips_record_deleted_after_lookup(self):
        """
        NocoDB rejects a whole bulk PATCH if any Id in it is missing. update()
        must still update the records that exist and skip the deleted one.
        """
        gone, live = self._mk_pages(["upd gone", "upd live2"])
        gone_url, live_url = self._mk_page_url("upd gone"), self._mk_page_url("upd live2")
        self.db.delete("Pages", gone)
        before = self.db.count("Pages")

        with self._stale_lookup({gone_url: gone}):
            result = self.db.update("Pages", [
                {"url": gone_url, "description": "late"},
                {"url": live_url, "description": "updated"},
            ])

        self.assertEqual(result, [None, live])
        self.assertEqual(self.db.read("Pages", live)["description"], "updated")
        self.assertIsNone(self.db.lookup("Pages", gone_url))
        self.assertEqual(self.db.count("Pages"), before)

    def test_write_skips_record_deleted_after_lookup(self):
        """write() updates existing records the same way, so it gets the same
        narrowing: the deleted record is skipped (Id None), not recreated."""
        gone, live = self._mk_pages(["wr gone", "wr live"])
        gone_url, live_url = self._mk_page_url("wr gone"), self._mk_page_url("wr live")
        self.db.delete("Pages", gone)

        with self._stale_lookup({gone_url: gone}):
            result = self.db.write("Pages", [
                {"url": gone_url, "description": "late"},
                {"url": live_url, "description": "updated"},
            ])

        self.assertEqual(result, [None, live])
        self.assertEqual(self.db.read("Pages", live)["description"], "updated")
        self.assertIsNone(self.db.lookup("Pages", gone_url))

    def test_delete_skips_already_deleted_records(self):
        """
        NocoDB rejects a whole bulk DELETE if any id in it is missing. delete()
        must still remove the ids that exist and treat missing ones as done.
        """
        gone, live1, live2 = self._mk_pages(["del gone", "del live1", "del live2"])
        self.assertEqual(self.db.delete("Pages", gone), 1)

        self.assertEqual(self.db.delete("Pages", [live1, gone, live2]), 2)
        self.assertFalse(self._page_exists(live1))
        self.assertFalse(self._page_exists(live2))
        self.assertEqual(self.db.delete("Pages", [gone, live1]), 0)

    def test_delete_survives_retry_after_lost_response(self):
        """
        fetch_url retries a DELETE whose response timed out. When the server
        had already deleted the batch, the retry gets ERR_RECORD_NOT_FOUND;
        delete() must treat that as done rather than raise.
        """
        ids = self._mk_pages(["retry a", "retry b"])
        real_fetch = self.db._fetch

        def lost_response(url, params=None, json=None, method="GET", **kwargs):
            if method == "DELETE" and not lost_response.done:
                lost_response.done = True
                real_fetch(url, params=params, json=json, method=method, **kwargs)
                raise FailedIO('Failed to fetch (404): {"error":"ERR_RECORD_NOT_FOUND"}')
            return real_fetch(url, params=params, json=json, method=method, **kwargs)
        lost_response.done = False

        with patch.object(self.db, "_fetch", side_effect=lost_response):
            self.assertEqual(self.db.delete("Pages", ids), 0)
        self.assertFalse(any(self._page_exists(i) for i in ids))

    def test_delete_other_errors_still_raise(self):
        page_id, = self._mk_pages(["del error"])
        with patch.object(self.db, "_fetch", side_effect=FailedIO("HTTP 500: server error")):
            with self.assertRaises(FailedIO):
                self.db.delete("Pages", page_id)
        self.assertTrue(self._page_exists(page_id))


if __name__ == "__main__":
    unittest.main()


class TestJsonFieldType(unittest.TestCase):
    """Pure encode/normalize checks for the "json" Schema field type."""

    _SCHEMA = {"fields": {"doc_preferences": {"type": "json"}, "name": {"type": "text"}}}

    def test_encode_dumps_objects_and_keeps_none(self):
        from birddog.nocodb_database import _encode_record
        encoded = _encode_record(self._SCHEMA, {"doc_preferences": {"columns": ["label"]}, "name": "x"})
        self.assertEqual(encoded, {"doc_preferences": '{"columns": ["label"]}', "name": "x"})
        self.assertIsNone(_encode_record(self._SCHEMA, {"doc_preferences": None})["doc_preferences"])

    def test_encode_rejects_non_objects(self):
        from birddog.nocodb_database import _encode_record
        with self.assertRaises(InvalidFieldValue):
            _encode_record(self._SCHEMA, {"doc_preferences": "not an object"})

    def test_normalize_accepts_objects_text_and_blank(self):
        from birddog.nocodb_database import _normalize_record
        for stored, expected in (({"a": 1}, {"a": 1}), ('{"a": 1}', {"a": 1}), ("", None), (None, None)):
            record = {"doc_preferences": stored}
            _normalize_record(self._SCHEMA, record)
            self.assertEqual(record["doc_preferences"], expected)


class TestWhereEncoding(unittest.TestCase):
    """Pure checks of filter/sort encoding for NocoDB (no network)."""

    def setUp(self):
        from birddog.nocodb_database import NocoDBDatabase
        db = NocoDBDatabase.__new__(NocoDBDatabase)
        db._table_id_map = {"T": "t1"}
        db._field_id_map = {"T": {"label": "cL", "anomaly": "cA", "code": "cC",
                                  "Id": "cI", "UpdatedAt": "cU"}}
        db._schema = {"T": {"fields": {"label": {"type": "text"},
                                       "anomaly": {"type": "bool"},
                                       "code": {"type": "single_select"}}}}
        self.db = db

    def enc(self, where):
        return self.db._encode_where_spec("T", where)

    def test_single_condition_unchanged(self):
        self.assertEqual(self.enc(("label", "eq", "A/1")), "(cL,eq,A/1)")
        self.assertEqual(self.enc(("code", "in", ["x", "y"])), "(cC,in,x,y)")
        self.assertEqual(self.enc(("code", "is", None)), "(cC,is,null)")
        self.assertEqual(self.enc(("Id", "gt", 5)), "(cI,gt,5)")

    def test_values_keep_commas_and_parens(self):
        self.assertEqual(self.enc(("label", "eq", "a,b_(1923)")), "(cL,eq,a,b_(1923))")

    def test_new_ops(self):
        self.assertEqual(self.enc(("label", "startswith", "A/1/")), "(cL,like,A/1/%)")
        self.assertEqual(self.enc(("code", "blank", None)), "(cC,blank)")
        self.assertEqual(self.enc(("code", "notblank", None)), "(cC,notblank)")

    def test_bool_eq_maps_to_checked(self):
        self.assertEqual(self.enc(("anomaly", "eq", False)), "(cA,notchecked)")
        self.assertEqual(self.enc(("anomaly", "eq", True)), "(cA,checked)")
        self.assertEqual(self.enc(("anomaly", "neq", True)), "(cA,notchecked)")
        # non-bool field: plain eq
        self.assertEqual(self.enc(("code", "eq", False)), "(cC,eq,false)")

    def test_groups_nest(self):
        from birddog.abstract_database import And, Or
        where = And(Or(("label", "eq", "A/1"), ("label", "startswith", "A/1/")),
                    ("anomaly", "eq", False))
        self.assertEqual(self.enc(where),
                         "((cL,eq,A/1)~or(cL,like,A/1/%))~and(cA,notchecked)")
        self.assertEqual(self.enc(And(("label", "eq", "x"))), "(cL,eq,x)")
        self.assertEqual(self.enc(Or(And(("label", "eq", "x")), ("code", "blank", None))),
                         "(cL,eq,x)~or(cC,blank)")

    def test_groups_compare_by_value(self):
        from birddog.abstract_database import And, Or
        self.assertEqual(And(("a", "eq", 1)), And(("a", "eq", 1)))
        self.assertNotEqual(And(("a", "eq", 1)), Or(("a", "eq", 1)))

    def test_rejected_filters(self):
        from birddog.abstract_database import And
        bad = [
            ("label", "like", "x"),                 # unknown op
            ("label", "eq"),                        # not a 3-tuple
            ["label", "eq", "x"],                   # list, not tuple
            ("label", "blank", "x"),                # blank takes None
            ("label", "in", "x"),                   # in needs a list
            ("label", "is", "x"),                   # is takes None/True/False
            ("label", "startswith", "A_1"),         # LIKE wildcard
            ("label", "startswith", "A%"),
            ("label", "eq", "a~orb"),               # NocoDB group syntax
            ("code", "in", ["a,b"]),                # comma inside an in item
            ("label", "eq", None),                  # use blank / is
        ]
        for where in bad:
            with self.subTest(where=where), self.assertRaises(ValueError):
                self.enc(where)
        with self.assertRaises(ValueError):
            And()
        with self.assertRaises(ValueError):
            And(("label", "eq", "x"), "label")
        with self.assertRaises(InvalidFieldName):
            self.enc(("nope", "eq", "x"))

    def test_sort_keys(self):
        enc = lambda s: self.db._encode_sort_spec("T", s)
        self.assertEqual(enc("label"), "cL")
        self.assertEqual(enc(("label", False)), "-cL")
        self.assertEqual(enc(["label", True]), "cL")       # legacy single-key list
        self.assertEqual(enc([("UpdatedAt", False), ("Id", True)]), "-cU,cI")
        self.assertEqual(enc(["label", "Id"]), "cL,cI")
        with self.assertRaises(ValueError):
            enc([])
