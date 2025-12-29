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
        log._changes.extend([
            ("Page:A", "2025-01-01T00:00:00Z", "u1"),
        ])

        #log.append({"Page:A": {"timestamp": "2025-01-01T00:00:00Z", "user": "u1"}})

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

            # cutoff_date passed should be the newest existing timestamp.
            grc.assert_called_once()
            _, kwargs = grc.call_args
            self.assertEqual(kwargs.get("cutoff_date"), "2025-01-01T00:00:00Z")

            # canonicalize_title called for each returned key
            self.assertEqual(canon.call_count, len(recent_changes))

        got = log.get()
        # Page:A should reflect the newer update
        self.assertEqual(got["PAGE:A"]["timestamp"], "2025-01-02T00:00:00Z")
        self.assertEqual(got["PAGE:A"]["user"], "u2")

        # PAGE:B should reflect the later of the two B updates (2025-01-04...)
        self.assertEqual(got["PAGE:B"]["timestamp"], "2025-01-04T00:00:00Z")
        self.assertEqual(got["PAGE:B"]["user"], "u4")


class TestPageTracker(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        self.addCleanup(lambda: os.chdir(self._old_cwd))

        self.tracker = _reload_tracker_with_local_env()

    def test_refresh_writes_only_newer_updates(self):
        tracker = self.tracker

        pt = tracker.PageTracker()

        # Seed existing state
        pt._table.put({
            "Page:Old": {"timestamp": "2025-01-10T00:00:00Z", "user": "u0"},
            "Page:Keep": {"timestamp": "2025-01-05T00:00:00Z", "user": "u0"},
        })
        pt._load_cache()

        updates = {
            "Page:Old": {"timestamp": "2025-01-09T00:00:00Z", "user": "u1"},   # older than latest -> ignored
            "Page:Keep": {"timestamp": "2025-01-06T00:00:00Z", "user": "u2"},  # newer -> applied
            "Page:New": {"timestamp": "2025-01-01T00:00:00Z", "user": "u3"},   # new title -> applied
        }

        with mock.patch.object(pt._change_log, "refresh", return_value=None) as _refresh, \
             mock.patch.object(pt._change_log, "get", return_value=updates), \
             mock.patch.object(pt._table, "put", wraps=pt._table.put) as put_spy:
            newer = pt.refresh()

        self.assertEqual(set(newer.keys()), {"Page:Keep", "Page:New"})
        # Verify persisted write includes only newer updates
        self.assertTrue(put_spy.called)
        written = put_spy.call_args.args[0]
        self.assertEqual(set(written.keys()), {"Page:Keep", "Page:New"})

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
             mock.patch.object(pt._table, "put") as put_mock, \
             mock.patch.object(pt._table, "remove") as remove_mock:
            still_more = pt.initialize_batch_of_unknowns(batch_size=50, api_delay=0.0)

        self.assertTrue(still_more)

        bpe.assert_called_once()
        glm.assert_called_once()

        # Good titles should be stored with {"timestamp": ...}
        put_mock.assert_called_once_with({"A": {"timestamp": "tA"}, "C": {"timestamp": "tC"}})

        # Bad titles removed
        remove_mock.assert_called_once_with(["B"])

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


if __name__ == "__main__":
    unittest.main()