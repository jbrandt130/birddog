import unittest

from birddog.runtime import PageUpdateManager


# ----------------------------------------------------------------------------
# Test doubles

class FakeTracker:
    def __init__(self, updates_by_prefix):
        self._updates_by_prefix = updates_by_prefix
        self.calls = []

    def get_updates(self, prefix, cutoff_date=None):
        self.calls.append((prefix, cutoff_date))
        return dict(self._updates_by_prefix.get(prefix, {}))


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


if __name__ == "__main__":
    unittest.main()
