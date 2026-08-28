import json
import os
import tempfile
import unittest
from unittest import mock

from birddog.runtime import PageUpdateManager
from birddog.store import SQLiteKeyValueStore


# ----------------------------------------------------------------------------
# Test doubles

class FakeTracker:
    def __init__(self, updates_by_prefix):
        self._updates_by_prefix = updates_by_prefix
        self.calls = []

    def get_updates(self, prefix, cutoff_date=None):
        self.calls.append((prefix, cutoff_date))
        return dict(self._updates_by_prefix.get(prefix, {}))


class _NoOpTracker:
    """Stands in for the PageTracker heartbeat() refreshes at the top of its
    own cycle -- returns no new updates so heartbeat() moves straight on to
    processing whatever's already been seeded into the pending-updates KV."""
    def refresh(self):
        return {}


class _FakePageLRU:
    def __init__(self):
        self.evicted = []

    def evict(self, title, runtime):
        self.evicted.append(title)


class _FakeRuntimeForHeartbeat:
    def __init__(self):
        self.database_update_enabled = False  # skip the DB-sync block entirely
        self.page_lru = _FakePageLRU()

    def trim_logs(self):
        pass


# ----------------------------------------------------------------------------
# PageUpdateManager.get_updates()

class PageUpdateManagerGetUpdatesTests(unittest.TestCase):
    def _manager(self, updates_by_prefix):
        manager = PageUpdateManager.__new__(PageUpdateManager)
        manager._tracker = FakeTracker(updates_by_prefix)
        return manager

    def test_filters_by_title_in_scope_boundary(self):
        # the tracker itself does a naive startswith(); get_updates must
        # still enforce the "/" boundary title_in_scope provides
        manager = self._manager({
            "Архів:ДААРК": {
                "Архів:ДААРК/1": {"timestamp": "2026-01-01T00:00:00Z"},
                "Архів:ДААРК2/9": {"timestamp": "2026-01-01T00:00:00Z"},
            },
        })
        result = manager.get_updates(["Архів:ДААРК"])
        self.assertEqual(list(result.keys()), ["Архів:ДААРК/1"])

    def test_applies_exclude(self):
        manager = self._manager({
            "Архів:ДААРК": {
                "Архів:ДААРК/1": {"timestamp": "2026-01-01T00:00:00Z"},
                "Архів:ДААРК/2": {"timestamp": "2026-01-01T00:00:00Z"},
            },
        })
        result = manager.get_updates(["Архів:ДААРК"], exclude=["Архів:ДААРК/2"])
        self.assertEqual(list(result.keys()), ["Архів:ДААРК/1"])

    def test_queries_each_include_prefix(self):
        manager = self._manager({
            "Архів:ДААРК": {"Архів:ДААРК/1": {"timestamp": "2026-01-01T00:00:00Z"}},
            "Архів:ДАЧгО": {"Архів:ДАЧгО/1": {"timestamp": "2026-01-01T00:00:00Z"}},
        })
        result = manager.get_updates(["Архів:ДААРК", "Архів:ДАЧгО"])
        self.assertEqual(set(result.keys()), {"Архів:ДААРК/1", "Архів:ДАЧгО/1"})
        self.assertEqual({prefix for prefix, _ in manager._tracker.calls}, {"Архів:ДААРК", "Архів:ДАЧгО"})


# ----------------------------------------------------------------------------
# PageUpdateManager.heartbeat() -- move-event handling (issue #136, step 3)

class PageUpdateManagerHeartbeatMoveTests(unittest.TestCase):
    def _manager(self):
        manager = PageUpdateManager.__new__(PageUpdateManager)
        manager._tracker = _NoOpTracker()
        manager._runtime = _FakeRuntimeForHeartbeat()
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        # a dedicated temp-file-backed store, independent of cwd -- avoids
        # the shared-local-cache contamination a chdir-based fixture risks
        # when tests run concurrently or the suite is invoked more than once
        manager._kv_store = SQLiteKeyValueStore(db_path=os.path.join(tmpdir.name, "kv.db"))
        return manager

    def _seed_pending(self, manager, title, update):
        manager._kv_store.insert(manager._PENDING_TITLE_UPDATES, title, json.dumps(update))

    def test_move_of_root_title_removes_its_archive_root_registration(self):
        manager = self._manager()
        old_title = "Архів:Проєкт_\"Інвентаріум\""
        self._seed_pending(manager, old_title, {
            "timestamp": "2026-06-14T10:01:37Z",
            "user": "Madvin",
            "action": "move",
            "target_title": "Архів:Інвентаріум",
        })

        with mock.patch("birddog.runtime.remove_archive_root") as mock_remove:
            manager.heartbeat()

        mock_remove.assert_called_once_with(old_title)
        self.assertIn(old_title, manager._runtime.page_lru.evicted)

    def test_move_of_non_root_title_does_not_touch_archive_roots(self):
        manager = self._manager()
        old_title = "Архів:ДААРК/100/1/5"
        self._seed_pending(manager, old_title, {
            "timestamp": "2026-08-14T02:11:53Z",
            "user": "Boh.val",
            "action": "move",
            "target_title": "Архів:ДААРК/100/1/5-new",
        })

        with mock.patch("birddog.runtime.remove_archive_root") as mock_remove:
            manager.heartbeat()

        mock_remove.assert_not_called()
        self.assertIn(old_title, manager._runtime.page_lru.evicted)

    def test_move_clears_the_pending_update_once_processed(self):
        manager = self._manager()
        old_title = "Архів:Проєкт_\"Інвентаріум\""
        self._seed_pending(manager, old_title, {
            "timestamp": "2026-06-14T10:01:37Z",
            "user": "Madvin",
            "action": "move",
            "target_title": "Архів:Інвентаріум",
        })

        with mock.patch("birddog.runtime.remove_archive_root"):
            manager.heartbeat()

        self.assertEqual(manager._kv_store.get_all(manager._PENDING_TITLE_UPDATES), [])


if __name__ == "__main__":
    unittest.main()
