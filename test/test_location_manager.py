import threading
import unittest
from unittest.mock import patch

from birddog.fetch import RateLimitedError

from birddog.locations.manager import LocationUpdateManager


class FakeDB:
    def __init__(self, docs, locations, links=None):
        self.docs = docs  # url -> Id
        self.locations = locations  # location_id -> Id
        self.links = links or {}  # doc Id -> list of Location Ids

    def lookup(self, table, key_set):
        return {u: self.docs[u] for u in key_set if u in self.docs}

    def scan_all(self, table, where=None, fields=None, sort=None, view_name=None):
        return [{"Id": i, "location_id": loc} for loc, i in self.locations.items()]

    def get_links(self, table, field, source, target_ids=None):
        return list(self.links.get(source, []))

    def delete_links(self, table, field, source, targets):
        self.links[source] = [t for t in self.links.get(source, []) if t not in targets]

    def create_links(self, table, field, source, targets):
        self.links.setdefault(source, []).extend(targets)


class FakeFinder:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def get_doc_locations_batched(self, doc_ids, batch_size=20, only_smallest_locations=True):
        self.calls.append((list(doc_ids), only_smallest_locations))
        return {i: self.results[i] for i in doc_ids}


def make_manager(db, finder):
    m = LocationUpdateManager.__new__(LocationUpdateManager)
    m._db = db
    m._finder = finder
    m._finder_lock = threading.Lock()
    m._location_record_ids = None
    return m


class TestUpdateLocations(unittest.TestCase):
    def test_replaces_links_from_results(self):
        db = FakeDB({"u1": 1, "u2": 2}, {"-10": 100, "-20": 200}, links={1: [200]})
        finder = FakeFinder({1: (False, ["-10"], [], [], set()), 2: (False, ["-20"], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1", "u2"])
        self.assertEqual(db.links[1], [100])
        self.assertEqual(db.links[2], [200])
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(finder.calls, [([1, 2], True)])

    def test_unchanged_links_reported(self):
        db = FakeDB({"u1": 1}, {"-10": 100}, links={1: [100]})
        finder = FakeFinder({1: (False, ["-10"], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1"])
        self.assertEqual((summary["updated"], summary["unchanged"]), (0, 1))

    def test_error_and_empty_results_leave_links_alone(self):
        db = FakeDB({"u1": 1, "u2": 2}, {"-10": 100}, links={1: [100], 2: [100]})
        finder = FakeFinder({1: (True, [], [], [], set()), 2: (False, [], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1", "u2"])
        self.assertEqual(db.links, {1: [100], 2: [100]})
        self.assertEqual((summary["errors"], summary["empty"]), (1, 1))

    def test_missing_docs_and_unknown_locations(self):
        db = FakeDB({"u1": 1}, {"-10": 100})
        finder = FakeFinder({1: (False, ["-99"], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1", "nope"])
        self.assertEqual(summary["not_found"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(db.links, {})

    def test_url_with_comma_is_found(self):
        url = "https://commons.wikimedia.org/wiki/File:A,_B.pdf"
        db = FakeDB({url: 1}, {"-10": 100})
        finder = FakeFinder({1: (False, ["-10"], [], [], set())})
        summary = make_manager(db, finder).update_locations([url])
        self.assertEqual((summary["updated"], summary["not_found"]), (1, 0))

    def test_no_docs_found_skips_inference(self):
        finder = FakeFinder({})
        summary = make_manager(FakeDB({}, {}), finder).update_locations(["nope"])
        self.assertEqual(finder.calls, [])
        self.assertEqual(summary["not_found"], 1)


class TestRateLimitedSubtask(unittest.TestCase):
    def make(self, side_effect, cancelled=False):
        m = make_manager(FakeDB({}, {}), FakeFinder({}))
        m._update_progress = lambda task_id, n: None
        m._is_task_cancelled = lambda task_id: cancelled
        patcher = patch.object(m, "update_locations", side_effect=side_effect)
        self.update = patcher.start()
        self.addCleanup(patcher.stop)
        return m

    def subtask(self):
        return {"task_id": "t1", "index": 0, "payload": {"kind": "update_locations", "urls": ["u1"]}}

    def test_waits_out_rate_limit_then_retries(self):
        m = self.make([RateLimitedError("TPD", 432.5), {"updated": 1}])
        sub = self.subtask()
        with patch("birddog.locations.manager.time.sleep") as sleep, \
             patch("birddog.locations.manager.time.monotonic", side_effect=[0.0, 0.0, 500.0]):
            m.execute_subtask(sub)
        self.assertEqual(self.update.call_count, 2)
        sleep.assert_called_once_with(m._CANCEL_POLL_S)
        self.assertEqual(sub["payload"]["updated"], 1)

    def test_cancel_during_rate_limit_wait_stops(self):
        m = self.make([RateLimitedError("TPD", 432.5)], cancelled=True)
        sub = self.subtask()
        with patch("birddog.locations.manager.time.sleep"):
            m.execute_subtask(sub)
        self.assertEqual(self.update.call_count, 1)
        self.assertTrue(sub["payload"]["cancelled"])

    def test_other_errors_propagate_for_task_retry(self):
        m = self.make([RuntimeError("boom")])
        with self.assertRaises(RuntimeError):
            m.execute_subtask(self.subtask())


if __name__ == "__main__":
    unittest.main()
