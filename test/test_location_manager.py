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
        self.statuses = {}  # url -> location_status written
        self.backlog = []  # urls in the BD:Need Locations view
        self.scan_calls = []

    def lookup(self, table, key_set):
        return {u: self.docs[u] for u in key_set if u in self.docs}

    def write(self, table, records):
        for r in records:
            self.statuses[r["url"]] = r["location_status"]
        return [self.docs.get(r["url"]) for r in records]

    def scan(self, table, view_name=None, fields=None, limit=100, cursor=None):
        self.scan_calls.append((table, view_name, limit))
        return [{"Id": self.docs.get(u), "url": u} for u in self.backlog[:limit]], None

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
    # a provider with no key variable, so the missing-key guard stays out of the way
    m._provider = "test"
    return m


class TestUpdateLocations(unittest.TestCase):
    def test_replaces_links_from_results(self):
        db = FakeDB({"u1": 1, "u2": 2}, {"-10": 100, "-20": 200}, links={1: [200]})
        finder = FakeFinder({1: (False, ["-10"], [], [], set()), 2: (False, ["-20"], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1", "u2"])
        self.assertEqual(db.links[1], [100])
        self.assertEqual(db.links[2], [200])
        self.assertEqual(summary["updated"], 2)
        # ids arrive in set-iteration order, which varies between runs
        self.assertEqual([(sorted(ids), smallest) for ids, smallest in finder.calls], [([1, 2], True)])
        self.assertEqual(db.statuses, {"u1": "found", "u2": "found"})

    def test_unchanged_links_reported(self):
        db = FakeDB({"u1": 1}, {"-10": 100}, links={1: [100]})
        finder = FakeFinder({1: (False, ["-10"], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1"])
        self.assertEqual((summary["updated"], summary["unchanged"]), (0, 1))
        self.assertEqual(db.statuses, {"u1": "found"})

    def test_error_and_empty_results_leave_links_alone(self):
        db = FakeDB({"u1": 1, "u2": 2}, {"-10": 100}, links={1: [100], 2: [100]})
        finder = FakeFinder({1: (True, [], [], [], set()), 2: (False, [], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1", "u2"])
        self.assertEqual(db.links, {1: [100], 2: [100]})
        self.assertEqual((summary["errors"], summary["empty"]), (1, 1))
        self.assertEqual(db.statuses, {"u1": "failed", "u2": "none"})

    def test_missing_docs_and_unknown_locations(self):
        db = FakeDB({"u1": 1}, {"-10": 100})
        finder = FakeFinder({1: (False, ["-99"], [], [], set())})
        summary = make_manager(db, finder).update_locations(["u1", "nope"])
        self.assertEqual(summary["not_found"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(db.links, {})
        # unknown location ids count as failed; a missing doc gets no status
        self.assertEqual(db.statuses, {"u1": "failed"})

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


    def test_rate_limit_leaves_statuses_alone(self):
        class LimitedFinder:
            def get_doc_locations_batched(self, *a, **kw):
                raise RateLimitedError("TPD", 600)
        db = FakeDB({"u1": 1}, {})
        with self.assertRaises(RateLimitedError):
            make_manager(db, LimitedFinder()).update_locations(["u1"])
        self.assertEqual(db.statuses, {})


class TestSweep(unittest.TestCase):
    def make(self, backlog, active=()):
        db = FakeDB({u: i for i, u in enumerate(backlog, 1)}, {})
        db.backlog = list(backlog)
        m = make_manager(db, FakeFinder({}))
        m.active_tasks = lambda: [{"name": n, "task_id": n} for n in active]
        m.status = lambda: {n: {"completed": 40, "total": 100} for n in active}
        self.started = []
        m.start_location_update = lambda urls, task_prefix=None: self.started.append((urls, task_prefix)) or f"{task_prefix}_1"
        return m, db

    def sweep_log(self, m, **kw):
        with patch("birddog.locations.manager._logger") as logger:
            result = m.sweep(**kw)
        return result, logger.info.call_args.args[0]

    def test_starts_sweep_task_from_view(self):
        m, db = self.make(["u1", "u2"])
        result, line = self.sweep_log(m)
        self.assertEqual(result, "LOS_1")
        self.assertIn("sweep: started LOS_1 for 2 doc(s)", line)
        self.assertNotIn("more may be pending", line)
        self.assertEqual(self.started, [(["u1", "u2"], "LOS")])
        self.assertEqual(db.scan_calls, [("Documents", "BD:Need Locations", m._SWEEP_LIMIT)])

    def test_full_batch_notes_more_may_be_pending(self):
        m, _ = self.make(["u1", "u2", "u3"])
        _, line = self.sweep_log(m, limit=2)
        self.assertIn("for 2 doc(s), more may be pending", line)

    def test_no_new_sweep_while_one_is_active(self):
        m, db = self.make(["u1"], active=["LOS_0"])
        result, line = self.sweep_log(m)
        self.assertIsNone(result)
        self.assertEqual(db.scan_calls, [])
        self.assertIn("sweep: LOS_0 still running (40/100)", line)

    def test_manual_task_does_not_block_sweep(self):
        m, _ = self.make(["u1"], active=["LOC_0"])
        self.assertEqual(m.sweep(), "LOS_1")

    def test_empty_backlog_starts_nothing(self):
        m, _ = self.make([])
        result, line = self.sweep_log(m)
        self.assertIsNone(result)
        self.assertEqual(self.started, [])
        self.assertIn("sweep: nothing pending", line)


class TestMissingKey(unittest.TestCase):
    """Without an API key every call fails; documents must stay pending
    instead of being marked failed."""

    def make(self):
        m = make_manager(FakeDB({"u1": 1}, {}), FakeFinder({}))
        m._provider = "groq"
        m.active_tasks = lambda: []
        m._create_task = lambda *a: self.fail("task created without a key")
        return m

    def test_sweep_skips_without_key(self):
        m = self.make()
        with patch.dict("os.environ", {}, clear=True), \
             patch("birddog.locations.manager._logger") as logger:
            self.assertIsNone(m.sweep())
        self.assertIn("GROQ_LOCATION_KEY is not set", logger.warning.call_args.args[0])
        self.assertEqual(m._db.scan_calls, [])

    def test_start_skips_without_key(self):
        m = self.make()
        with patch.dict("os.environ", {}, clear=True), \
             patch("birddog.locations.manager._logger"):
            self.assertIsNone(m.start_location_update(["u1"]))

    def test_start_runs_with_key(self):
        m = self.make()
        created = []
        m._create_task = lambda *a: created.append(a)
        with patch.dict("os.environ", {"GROQ_LOCATION_KEY": "test-key"}, clear=True):
            name = m.start_location_update(["u1"])
        self.assertTrue(name.startswith("LOC_"))
        self.assertEqual(len(created), 1)


class TestStartupCredentialCheck(unittest.TestCase):
    def log_for(self, result):
        m = make_manager(FakeDB({}, {}), FakeFinder({}))
        m._provider = "groq"
        with patch("birddog.locations.extractor.LocationExtractor.check_credentials", return_value=result), \
             patch("birddog.locations.manager._logger") as logger:
            m._log_credential_check()
        return logger

    def test_good_credentials_logged_as_info(self):
        logger = self.log_for((True, "key accepted, model m available"))
        self.assertIn("groq credential check: key accepted", logger.info.call_args.args[0])
        logger.error.assert_not_called()

    def test_bad_credentials_logged_as_error(self):
        logger = self.log_for((False, "key rejected by groq (HTTP 401)"))
        self.assertIn("will not run until this is fixed", logger.error.call_args.args[0])

    def test_inconclusive_check_logged_as_warning(self):
        logger = self.log_for((None, "could not verify credentials: down"))
        self.assertIn("could not verify", logger.warning.call_args.args[0])


class TestFailedBatches(unittest.TestCase):
    def test_batches_out_of_retries_are_marked_failed(self):
        db = FakeDB({"u1": 1, "u2": 2, "u3": 3}, {})
        m = make_manager(db, FakeFinder({}))
        m._remove_state = lambda name: None
        m._get_state = lambda name: {}
        m.complete_task({"task_id": "t1", "name": "LOS_1"}, [
            {"status": "failed", "payload": {"kind": "update_locations", "urls": ["u1", "u2"]}},
            {"status": "completed", "payload": {"kind": "update_locations", "urls": ["u3"]}},
        ])
        self.assertEqual(db.statuses, {"u1": "failed", "u2": "failed"})

    def test_task_summary_totals_batches(self):
        db = FakeDB({}, {})
        m = make_manager(db, FakeFinder({}))
        m._remove_state = lambda name: None
        m._get_state = lambda name: {"started": 1000.0}
        subtasks = [
            {"status": "completed", "payload": {"urls": ["a"] * 20, "updated": 5, "unchanged": 3,
                                                "empty": 10, "errors": 2, "not_found": 0, "rate_limit_wait_s": 432.5}},
            {"status": "completed", "payload": {"urls": ["b"] * 5, "updated": 1, "unchanged": 0,
                                                "empty": 4, "errors": 0, "not_found": 0, "rate_limit_wait_s": 0}},
            {"status": "failed", "payload": {"urls": ["c"] * 20}},
        ]
        with patch("birddog.locations.manager._logger") as logger, \
             patch("birddog.locations.manager.time.time", return_value=1600.0):
            m._log_task_summary("LOS_1", subtasks, 1000.0, False)
        line = logger.info.call_args.args[0]
        self.assertIn("LOS_1 finished: 45 docs in 600s (rate-limit waits 432s)", line)
        self.assertIn("found=9 (updated=6) none=14 failed=2 not_found=0", line)
        self.assertIn("batches: 3 total, 1 failed, 0 cancelled", line)


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
