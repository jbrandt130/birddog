import unittest
from unittest import mock
from datetime import timedelta

from birddog import significance
from birddog import watcher as watcher_mod
from birddog.utility import utc_now_dt


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


class FakeKVStore:
    """Minimal in-memory KV store matching the interface watcher.py uses."""
    def __init__(self):
        self._data = {}  # (namespace, key) -> value

    def insert(self, namespace, key, value):
        self._data[(namespace, key)] = value

    def get(self, namespace, key):
        try:
            return self._data[(namespace, key)]
        except KeyError as e:
            raise KeyError(f"Missing key: {namespace}/{key}") from e

    def get_all(self, namespace):
        return [(k, v) for (ns, k), v in self._data.items() if ns == namespace]

    def remove_if_exists(self, namespace, key):
        return self._data.pop((namespace, key), None) is not None

    def remove_all(self, namespace):
        for k in [k for k in self._data if k[0] == namespace]:
            del self._data[k]


class SignificanceTestBase(unittest.TestCase):
    def setUp(self):
        self.kv = FakeKVStore()
        self._patch = mock.patch.object(watcher_mod, "_watcher_kv", self.kv)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()


# ----------------------------------------------------------------------------
# _is_debounced

class DebounceTests(unittest.TestCase):
    def test_recent_change_is_debounced(self):
        now = utc_now_dt()
        entry = {"modified": _iso(now - timedelta(minutes=5))}
        self.assertTrue(significance._is_debounced(entry, now))

    def test_stable_change_is_not_debounced(self):
        now = utc_now_dt()
        entry = {"modified": _iso(now - timedelta(minutes=25))}
        self.assertFalse(significance._is_debounced(entry, now))

    def test_exactly_at_threshold_is_not_debounced(self):
        now = utc_now_dt()
        entry = {"modified": _iso(now - timedelta(minutes=20, seconds=1))}
        self.assertFalse(significance._is_debounced(entry, now))

    def test_missing_modified_is_debounced(self):
        now = utc_now_dt()
        self.assertTrue(significance._is_debounced({}, now))


# ----------------------------------------------------------------------------
# SignificanceLRU

class SignificanceLRUTests(unittest.TestCase):
    def test_memoizes_by_span(self):
        lru = significance.SignificanceLRU()
        calls = []
        def compute():
            calls.append(1)
            return False
        lru.lookup("Архів:ДААРК/1", "2026-01-01", "2026-01-02", compute)
        lru.lookup("Архів:ДААРК/1", "2026-01-01", "2026-01-02", compute)
        self.assertEqual(len(calls), 1)

    def test_different_span_recomputes(self):
        lru = significance.SignificanceLRU()
        calls = []
        def compute():
            calls.append(1)
            return False
        lru.lookup("Архів:ДААРК/1", "2026-01-01", "2026-01-02", compute)
        lru.lookup("Архів:ДААРК/1", "2026-01-01", "2026-01-03", compute)
        self.assertEqual(len(calls), 2)


# ----------------------------------------------------------------------------
# _classify

class FakePage:
    def __init__(self, exists=True):
        self.exists = exists
        self.reverted_to = None

    def revert_to(self, date):
        self.reverted_to = date
        return self


class ClassifyTests(unittest.TestCase):
    # each test uses its own title so the module-level verdict LRU (shared,
    # never reset between tests) can't serve a cached verdict from another case

    def test_delegates_to_page_significance_when_both_versions_exist(self):
        head = FakePage(exists=True)
        reference = FakePage(exists=True)
        make_page = mock.Mock(side_effect=[head, reference])
        with mock.patch.object(significance, "Page", make_page), \
             mock.patch.object(significance, "page_significance", return_value=False) as mock_sig:
            result = significance._classify("Архів:ДААРК/both-exist", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", runtime=None)
        self.assertEqual(reference.reverted_to, "2026-01-01T00:00:00Z")
        mock_sig.assert_called_once_with(head, reference)
        self.assertFalse(result)

    def test_missing_head_returns_none_without_calling_page_significance(self):
        head = FakePage(exists=False)
        reference = FakePage(exists=True)
        make_page = mock.Mock(side_effect=[head, reference])
        with mock.patch.object(significance, "Page", make_page), \
             mock.patch.object(significance, "page_significance") as mock_sig:
            result = significance._classify("Архів:ДААРК/no-head", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", runtime=None)
        self.assertIsNone(result)
        mock_sig.assert_not_called()

    def test_missing_reference_version_returns_none(self):
        head = FakePage(exists=True)
        reference = FakePage(exists=False)
        make_page = mock.Mock(side_effect=[head, reference])
        with mock.patch.object(significance, "Page", make_page), \
             mock.patch.object(significance, "page_significance") as mock_sig:
            result = significance._classify("Архів:ДААРК/no-ref", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", runtime=None)
        self.assertIsNone(result)
        mock_sig.assert_not_called()

    def test_no_earlier_version_at_all_returns_none(self):
        # Page.revert_to() returns None when history() finds nothing at or
        # before the reference date
        head = FakePage(exists=True)
        make_page = mock.Mock(side_effect=[head, mock.Mock(revert_to=mock.Mock(return_value=None))])
        with mock.patch.object(significance, "Page", make_page), \
             mock.patch.object(significance, "page_significance") as mock_sig:
            result = significance._classify("Архів:ДААРК/no-earlier", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", runtime=None)
        self.assertIsNone(result)
        mock_sig.assert_not_called()

    def test_result_is_memoized_across_repeated_calls(self):
        head = FakePage(exists=True)
        reference = FakePage(exists=True)
        make_page = mock.Mock(side_effect=[head, reference])
        with mock.patch.object(significance, "Page", make_page), \
             mock.patch.object(significance, "page_significance", return_value=False) as mock_sig:
            significance._classify("Архів:ДААРК/2", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z", runtime=None)
            significance._classify("Архів:ДААРК/2", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z", runtime=None)
        mock_sig.assert_called_once()


# ----------------------------------------------------------------------------
# _classify_item

class ClassifyItemTests(SignificanceTestBase):
    EMAIL = "a@example.com"
    TITLE = "Архів:ДААРК"
    ITEM = "Архів:ДААРК/1"

    def _entry(self, **overrides):
        entry = {"modified": "2026-01-02T00:00:00Z", "last_resolved": "2026-01-01T00:00:00Z", "user": "x"}
        entry.update(overrides)
        return entry

    def test_significant_writes_nothing(self):
        watcher_mod.put_unresolved(self.EMAIL, self.TITLE, self.ITEM, self._entry())
        with mock.patch.object(significance, "_classify", return_value=True):
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, self._entry(), runtime=None)
        stored = watcher_mod.get_unresolved(self.EMAIL, self.TITLE, self.ITEM)
        self.assertNotIn("insignificant", stored)

    def test_unclassifiable_writes_nothing(self):
        watcher_mod.put_unresolved(self.EMAIL, self.TITLE, self.ITEM, self._entry())
        with mock.patch.object(significance, "_classify", return_value=None):
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, self._entry(), runtime=None)
        stored = watcher_mod.get_unresolved(self.EMAIL, self.TITLE, self.ITEM)
        self.assertNotIn("insignificant", stored)

    def test_insignificant_stamps_flag_and_span(self):
        entry = self._entry()
        watcher_mod.put_unresolved(self.EMAIL, self.TITLE, self.ITEM, entry)
        with mock.patch.object(significance, "_classify", return_value=False):
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, entry, runtime=None)
        stored = watcher_mod.get_unresolved(self.EMAIL, self.TITLE, self.ITEM)
        self.assertTrue(stored["insignificant"])
        self.assertEqual(stored["sig_span"], [entry["last_resolved"], entry["modified"]])
        # original fields preserved
        self.assertEqual(stored["user"], "x")

    def test_already_classified_for_same_span_skips_reclassification(self):
        entry = self._entry(insignificant=True, sig_span=["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"])
        watcher_mod.put_unresolved(self.EMAIL, self.TITLE, self.ITEM, entry)
        with mock.patch.object(significance, "_classify") as mock_classify:
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, entry, runtime=None)
        mock_classify.assert_not_called()

    def test_missing_dates_skips_classification(self):
        entry = {"modified": None, "last_resolved": None}
        with mock.patch.object(significance, "_classify") as mock_classify:
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, entry, runtime=None)
        mock_classify.assert_not_called()

    def test_classify_exception_is_swallowed(self):
        watcher_mod.put_unresolved(self.EMAIL, self.TITLE, self.ITEM, self._entry())
        with mock.patch.object(significance, "_classify", side_effect=RuntimeError("boom")):
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, self._entry(), runtime=None)  # must not raise
        stored = watcher_mod.get_unresolved(self.EMAIL, self.TITLE, self.ITEM)
        self.assertNotIn("insignificant", stored)

    def test_stale_span_at_write_time_is_discarded(self):
        # entry we're classifying is the span as seen at sweep-scan time; by
        # the time classification finishes, check_watcher() has moved the KV
        # row on to a newer span -- the verdict must not be stamped onto it
        original = self._entry()
        watcher_mod.put_unresolved(self.EMAIL, self.TITLE, self.ITEM, original)
        newer = self._entry(modified="2026-01-03T00:00:00Z")

        def classify_and_race(*args, **kwargs):
            # simulate check_watcher() advancing the row mid-flight
            watcher_mod.put_unresolved(self.EMAIL, self.TITLE, self.ITEM, newer)
            return False

        with mock.patch.object(significance, "_classify", side_effect=classify_and_race):
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, original, runtime=None)
        stored = watcher_mod.get_unresolved(self.EMAIL, self.TITLE, self.ITEM)
        self.assertNotIn("insignificant", stored)
        self.assertEqual(stored, newer)

    def test_item_removed_before_write_back_is_discarded(self):
        entry = self._entry()
        # never stored -- item was resolved/removed before classification finished
        with mock.patch.object(significance, "_classify", return_value=False):
            significance._classify_item(self.EMAIL, self.TITLE, self.ITEM, entry, runtime=None)  # must not raise
        with self.assertRaises(KeyError):
            watcher_mod.get_unresolved(self.EMAIL, self.TITLE, self.ITEM)


# ----------------------------------------------------------------------------
# sweep()

class SweepTests(SignificanceTestBase):
    def test_debounced_item_is_not_classified(self):
        now = utc_now_dt()
        watcher_mod.put_watcher("a@example.com", "Архів:ДААРК", {"include": ["Архів:ДААРК"]})
        watcher_mod.put_unresolved("a@example.com", "Архів:ДААРК", "Архів:ДААРК/1", {
            "modified": _iso(now - timedelta(minutes=5)), "last_resolved": _iso(now - timedelta(days=1)),
        })
        with mock.patch.object(significance, "_classify_item") as mock_classify_item:
            examined = significance.sweep(runtime=None)
        mock_classify_item.assert_not_called()
        self.assertEqual(examined, 0)

    def test_stable_item_is_classified(self):
        now = utc_now_dt()
        watcher_mod.put_watcher("a@example.com", "Архів:ДААРК", {"include": ["Архів:ДААРК"]})
        watcher_mod.put_unresolved("a@example.com", "Архів:ДААРК", "Архів:ДААРК/1", {
            "modified": _iso(now - timedelta(minutes=25)), "last_resolved": _iso(now - timedelta(days=1)),
        })
        with mock.patch.object(significance, "_classify_item") as mock_classify_item:
            examined = significance.sweep(runtime=None)
        mock_classify_item.assert_called_once()
        self.assertEqual(examined, 1)

    def test_moved_marker_is_skipped(self):
        now = utc_now_dt()
        watcher_mod.put_watcher("a@example.com", "Архів:ДААРК", {"include": ["Архів:ДААРК"]})
        watcher_mod.put_unresolved("a@example.com", "Архів:ДААРК", "Архів:ДААРК", {
            "modified": _iso(now - timedelta(minutes=25)), "last_resolved": _iso(now - timedelta(days=1)),
            "moved_to": "Архів:ДАЛО",
        })
        with mock.patch.object(significance, "_classify_item") as mock_classify_item:
            significance.sweep(runtime=None)
        mock_classify_item.assert_not_called()

    def test_per_tick_cap_is_respected(self):
        now = utc_now_dt()
        watcher_mod.put_watcher("a@example.com", "Архів:ДААРК", {"include": ["Архів:ДААРК"]})
        for i in range(significance._MAX_EXAMINED_PER_TICK + 10):
            watcher_mod.put_unresolved("a@example.com", "Архів:ДААРК", f"Архів:ДААРК/{i}", {
                "modified": _iso(now - timedelta(minutes=25)), "last_resolved": _iso(now - timedelta(days=1)),
            })
        with mock.patch.object(significance, "_classify_item") as mock_classify_item:
            examined = significance.sweep(runtime=None)
        self.assertEqual(examined, significance._MAX_EXAMINED_PER_TICK)
        self.assertEqual(mock_classify_item.call_count, significance._MAX_EXAMINED_PER_TICK)

    def test_already_classified_prefix_does_not_starve_new_items(self):
        # regression for the sweep-progress bug found 2026-09-18: a large
        # already-classified prefix must not consume the per-tick budget and
        # block the rest of the backlog from ever being reached
        now = utc_now_dt()
        stable = {"modified": _iso(now - timedelta(minutes=25)), "last_resolved": _iso(now - timedelta(days=1))}
        watcher_mod.put_watcher("a@example.com", "Архів:ДААРК", {"include": ["Архів:ДААРК"]})

        already_done_count = significance._MAX_EXAMINED_PER_TICK + 50
        for i in range(already_done_count):
            watcher_mod.put_unresolved("a@example.com", "Архів:ДААРК", f"Архів:ДААРК/done/{i}", {
                **stable, "insignificant": True,
                "sig_span": [stable["last_resolved"], stable["modified"]],
            })
        new_items = [f"Архів:ДААРК/new/{i}" for i in range(5)]
        for title in new_items:
            watcher_mod.put_unresolved("a@example.com", "Архів:ДААРК", title, dict(stable))

        with mock.patch.object(significance, "_classify", return_value=False):
            examined = significance.sweep(runtime=None)

        self.assertEqual(examined, len(new_items))
        for title in new_items:
            self.assertTrue(watcher_mod.get_unresolved("a@example.com", "Архів:ДААРК", title)["insignificant"])

    def test_multiple_watches_all_swept(self):
        now = utc_now_dt()
        stable = {"modified": _iso(now - timedelta(minutes=25)), "last_resolved": _iso(now - timedelta(days=1))}
        watcher_mod.put_watcher("a@example.com", "Архів:ДААРК", {"include": ["Архів:ДААРК"]})
        watcher_mod.put_unresolved("a@example.com", "Архів:ДААРК", "Архів:ДААРК/1", stable)
        watcher_mod.put_watcher("b@example.com", "Архів:ДАЖО", {"include": ["Архів:ДАЖО"]})
        watcher_mod.put_unresolved("b@example.com", "Архів:ДАЖО", "Архів:ДАЖО/1", stable)
        with mock.patch.object(significance, "_classify_item") as mock_classify_item:
            examined = significance.sweep(runtime=None)
        self.assertEqual(examined, 2)
        self.assertEqual(mock_classify_item.call_count, 2)


if __name__ == "__main__":
    unittest.main()
