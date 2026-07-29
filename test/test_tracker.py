import os
import tempfile
import unittest
import importlib
from unittest import mock


class _DummyLogService:
    """No-op context manager used to stub birddog.log.LogService in unit tests."""
    def __init__(self, *args, **kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False


def _reload_tracker_with_local_env():
    """
    tracker.py selects SQLite vs DynamoDB backends at import time based on
    birddog.env.detect_environment(). Force "local" for unit tests.
    """
    with mock.patch("birddog.env.detect_environment", return_value="local"), \
         mock.patch("birddog.log.LogService", _DummyLogService):
        import birddog.tracker as tracker  # noqa: F401
        tracker = importlib.reload(tracker)
    return tracker


class TestPageChangeLog(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        self.addCleanup(lambda: os.chdir(self._old_cwd))

        # birddog.store binds LogService at import time, so patch it there
        # for the full duration of each test (not just during reload).
        p = mock.patch("birddog.store.LogService", _DummyLogService)
        p.start()
        self.addCleanup(p.stop)

        self.tracker = _reload_tracker_with_local_env()

    def test_oldest_newest_empty_raises(self):
        log = self.tracker.PageChangeLog()
        self.assertEqual(log.size(), 0)
        with self.assertRaises(ValueError):
            log.oldest()
        with self.assertRaises(ValueError):
            log.newest()

    def test_refresh_appends_and_get_dedupes_latest_per_title(self):
        tracker = self.tracker

        # Start with one existing change so refresh uses newest() as cutoff_date.
        log = tracker.PageChangeLog()
        log._changes["Page:A"] = {"timestamp": "2025-01-01T00:00:00Z", "user": "u1"}


        # Prepare a refresh payload that includes:
        # - a newer update to an existing title
        # - a new title
        # - a second update to the new title (later), to validate get() deduping
        recent_changes = {
            "Page:A": {"timestamp": "2025-01-02T00:00:00Z", "user": "u2"},
            "Page:B": {"timestamp": "2025-01-03T00:00:00Z", "user": "u3"},
            "page:b": {"timestamp": "2025-01-04T00:00:00Z", "user": "u4"},
        }

        # Canonicalize to upper-case so Page:B and page:b collide.
        with mock.patch.object(tracker, "canonicalize_title", side_effect=lambda t: t.upper()) as canon, \
             mock.patch.object(tracker, "get_recent_changes", return_value=recent_changes) as grc:
            log.refresh()

            # utc_start passed should be the newest existing timestamp, unmodified.
            grc.assert_called_once()
            _, kwargs = grc.call_args
            self.assertEqual(kwargs.get("utc_start"), "2025-01-01T00:00:00Z")

            # canonicalize_title called for each returned key
            self.assertEqual(canon.call_count, len(recent_changes))

        got = log.get()
        # Page:A should reflect the newer update
        self.assertEqual(got["PAGE:A"]["timestamp"], "2025-01-02T00:00:00Z")
        self.assertEqual(got["PAGE:A"]["user"], "u2")

        # PAGE:B should reflect the later of the two B updates (2025-01-04...)
        self.assertEqual(got["PAGE:B"]["timestamp"], "2025-01-04T00:00:00Z")
        self.assertEqual(got["PAGE:B"]["user"], "u4")

    def test_changes_persisted_and_reloaded(self):
        """__init__ deserializes entries that a previous refresh() wrote to the KV store."""
        tracker = self.tracker
        log = tracker.PageChangeLog()
        self.assertEqual(log.size(), 0)

        recent_changes = {
            "Page:X": {"timestamp": "2025-06-01T00:00:00Z", "user": "alice"},
            "Page:Y": {"timestamp": "2025-06-02T00:00:00Z", "user": "bob"},
        }
        with mock.patch.object(tracker, "get_recent_changes", return_value=recent_changes), \
             mock.patch.object(tracker, "canonicalize_title", side_effect=lambda t: t.upper()):
            log.refresh()

        # New instance should deserialize from the same KV store (same temp dir)
        log2 = tracker.PageChangeLog()
        got = log2.get()
        self.assertEqual(got["PAGE:X"]["timestamp"], "2025-06-01T00:00:00Z")
        self.assertEqual(got["PAGE:X"]["user"], "alice")
        self.assertEqual(got["PAGE:Y"]["timestamp"], "2025-06-02T00:00:00Z")
        self.assertEqual(got["PAGE:Y"]["user"], "bob")

    def test_refresh_with_empty_log_passes_none_cutoff(self):
        """refresh() on an empty log calls get_recent_changes with utc_start=None."""
        tracker = self.tracker
        log = tracker.PageChangeLog()

        with mock.patch.object(tracker, "get_recent_changes", return_value={}) as grc:
            log.refresh()

        grc.assert_called_once()
        _, kwargs = grc.call_args
        self.assertIsNone(kwargs.get("utc_start"))


class TestPageTracker(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        self.addCleanup(lambda: os.chdir(self._old_cwd))

        # birddog.store binds LogService at import time, so patch it there
        # for the full duration of each test (not just during reload).
        p = mock.patch("birddog.store.LogService", _DummyLogService)
        p.start()
        self.addCleanup(p.stop)

        self.tracker = _reload_tracker_with_local_env()

        # register_archive_root() lives in birddog.wiki's own module-level KV
        # store, unrelated to what these tests exercise -- stub it out so
        # tracker tests don't depend on that real, unmocked storage.
        p2 = mock.patch.object(self.tracker, "register_archive_root", lambda title: None)
        p2.start()
        self.addCleanup(p2.stop)

    def test_refresh_writes_only_newer_updates(self):
        tracker = self.tracker

        pt = tracker.PageTracker()

        # Seed existing state directly into the in-memory cache
        pt._page_dict = {
            "Page:Old":  {"timestamp": "2025-01-10T00:00:00Z", "user": "u0"},
            "Page:Keep": {"timestamp": "2025-01-05T00:00:00Z", "user": "u0"},
        }

        updates = {
            "Page:Old":  {"timestamp": "2025-01-09T00:00:00Z", "user": "u1"},  # older -> ignored
            "Page:Keep": {"timestamp": "2025-01-06T00:00:00Z", "user": "u2"},  # newer -> applied
            "Page:New":  {"timestamp": "2025-01-01T00:00:00Z", "user": "u3"},  # new title -> applied
        }

        with mock.patch.object(pt._change_log, "refresh"), \
             mock.patch.object(pt._change_log, "get", return_value=updates), \
             mock.patch.object(pt._kv, "insert") as insert_spy:
            newer = pt.refresh()

        self.assertEqual(set(newer.keys()), {"Page:Keep", "Page:New"})
        # Verify only the newer titles were persisted
        written_titles = {c.args[1] for c in insert_spy.call_args_list}
        self.assertEqual(written_titles, {"Page:Keep", "Page:New"})

        # Cache updated
        self.assertEqual(pt._page_dict["Page:Keep"]["timestamp"], "2025-01-06T00:00:00Z")
        self.assertEqual(pt._page_dict["Page:New"]["user"], "u3")
        self.assertEqual(pt._page_dict["Page:Old"]["timestamp"], "2025-01-10T00:00:00Z")

    def test_initialize_batch_of_unknowns_updates_good_and_removes_bad(self):
        tracker = self.tracker
        pt = tracker.PageTracker()

        # Build a cache with unknown timestamps
        pt._page_dict = {
            "A": {},                 # unknown -> good
            "B": {"timestamp": None},# unknown -> bad (non-existent)
            "C": {},                 # unknown -> good
        }

        with mock.patch.object(tracker, "batch_page_exists", return_value={"A": True, "B": False, "C": True}) as bpe, \
             mock.patch.object(tracker, "get_last_mod", return_value={"A": "tA", "C": "tC"}) as glm, \
             mock.patch.object(pt._kv, "insert") as insert_mock, \
             mock.patch.object(pt._kv, "remove") as remove_mock:
            still_more = pt.initialize_batch_of_unknowns(batch_size=50, api_delay=0.0)

        self.assertTrue(still_more)

        bpe.assert_called_once()
        glm.assert_called_once()

        # Good titles should each be inserted into the kv store
        inserted_titles = {c.args[1] for c in insert_mock.call_args_list}
        self.assertEqual(inserted_titles, {"A", "C"})

        # Bad titles each removed from the kv store
        removed_titles = {c.args[1] for c in remove_mock.call_args_list}
        self.assertEqual(removed_titles, {"B"})

        self.assertEqual(pt._page_dict["A"]["timestamp"], "tA")
        self.assertEqual(pt._page_dict["C"]["timestamp"], "tC")
        self.assertNotIn("B", pt._page_dict)

        # If no unknowns remain, returns False
        pt._page_dict = {"X": {"timestamp": "tX"}}
        self.assertFalse(pt.initialize_batch_of_unknowns())

    def test_get_updates_filters_by_prefix_and_cutoff(self):
        tracker = self.tracker
        pt = tracker.PageTracker()

        pt._page_dict = {
            "ARCHIVE:ABC/1": {"timestamp": "2025-01-01T00:00:00Z", "user": "u1"},
            "ARCHIVE:ABC/2": {"timestamp": "2025-01-03T00:00:00Z", "user": "u2"},
            "ARCHIVE:XYZ/1": {"timestamp": "2025-01-04T00:00:00Z", "user": "u3"},
            "ARCHIVE:ABC/NO_TS": {"user": "u4"},
        }

        # Canonicalize to upper
        with mock.patch.object(tracker, "canonicalize_title", side_effect=lambda s: s.upper()):
            got = pt.get_updates("archive:abc", cutoff_date="2025-01-02T00:00:00Z")

        self.assertEqual(set(got.keys()), {"ARCHIVE:ABC/2"})
        self.assertEqual(got["ARCHIVE:ABC/2"]["user"], "u2")

    def test_reset_clears_and_repopulates(self):
        """reset() calls remove_all on the PT namespace then inserts each supplied title."""
        tracker = self.tracker
        pt = tracker.PageTracker()

        new_titles = {"Page:A": {"timestamp": "t1"}, "Page:B": {"timestamp": "t2"}}
        with mock.patch.object(pt._kv, "remove_all") as remove_all_spy, \
             mock.patch.object(pt._kv, "insert") as insert_spy, \
             mock.patch.object(pt._kv, "get_all", return_value=[]):
            pt.reset(all_titles=new_titles)

        remove_all_spy.assert_called_once_with(tracker._TRACKER_KV_NS_TRACKER)
        inserted_titles = {c.args[1] for c in insert_spy.call_args_list}
        self.assertEqual(inserted_titles, {"Page:A", "Page:B"})

    def test_refresh_no_op_when_nothing_newer(self):
        """refresh() returns {} and writes nothing to KV when all change-log entries are older."""
        tracker = self.tracker
        pt = tracker.PageTracker()

        pt._page_dict = {
            "Page:X": {"timestamp": "2025-05-10T00:00:00Z", "user": "u0"},
        }
        updates = {
            "Page:X": {"timestamp": "2025-05-09T00:00:00Z", "user": "u1"},  # older -> ignored
        }
        with mock.patch.object(pt._change_log, "refresh"), \
             mock.patch.object(pt._change_log, "get", return_value=updates), \
             mock.patch.object(pt._kv, "insert") as insert_spy:
            result = pt.refresh()

        self.assertEqual(result, {})
        insert_spy.assert_not_called()
        self.assertEqual(pt._page_dict["Page:X"]["user"], "u0")

    def test_page_dict_persisted_and_reloaded(self):
        """_load_cache() correctly deserializes entries written by a previous refresh()."""
        tracker = self.tracker
        pt1 = tracker.PageTracker()

        updates = {
            "Page:Alpha": {"timestamp": "2025-03-01T00:00:00Z", "user": "u1"},
            "Page:Beta":  {"timestamp": "2025-03-02T00:00:00Z", "user": "u2"},
        }
        with mock.patch.object(pt1._change_log, "refresh"), \
             mock.patch.object(pt1._change_log, "get", return_value=updates):
            pt1.refresh()

        # New instance reads from the same KV store (same temp dir)
        pt2 = tracker.PageTracker()
        self.assertEqual(pt2._page_dict["Page:Alpha"]["timestamp"], "2025-03-01T00:00:00Z")
        self.assertEqual(pt2._page_dict["Page:Beta"]["user"], "u2")
        self.assertNotIn("Page:Gamma", pt2._page_dict)


if __name__ == "__main__":
    unittest.main()