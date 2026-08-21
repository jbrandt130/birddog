import unittest
from unittest import mock

from birddog import watcher as watcher_mod
from birddog.utility import utc_now_dt, to_utc_format


# ----------------------------------------------------------------------------
# Test doubles
#
# check_watcher() only ever touches runtime.update_manager, so a minimal fake
# covering that one attribute is enough to unit test it without any live
# wiki/AWS access. FakePageUpdateManager mirrors the real
# PageUpdateManager.get_updates() cutoff_date filtering (cutoff_date=None
# means "no floor") so tests can verify check_watcher() actually narrows its
# queries.
#
# Fixtures use the real "Архів:ДААРК" archive (same one test_wiki.py /
# test_user.py already use), so no mocking of archive_root() /
# page_title_from_address() is needed.

class FakePageUpdateManager:
    def __init__(self, updates):
        self._updates = updates
        self.calls = []

    def get_updates(self, include, exclude=None, cutoff_date=None):
        self.calls.append((tuple(include), tuple(exclude) if exclude else (), cutoff_date))
        floor = cutoff_date or "0"
        return {
            title: update
            for title, update in self._updates.items()
            if update["timestamp"] >= floor
        }


class FakeRuntime:
    def __init__(self, update_manager):
        self.update_manager = update_manager


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

    def remove(self, namespace, key):
        del self._data[(namespace, key)]

    def remove_if_exists(self, namespace, key):
        return self._data.pop((namespace, key), None) is not None

    def remove_all(self, namespace):
        for k in [k for k in self._data if k[0] == namespace]:
            del self._data[k]

    def insert_many(self, namespace, items):
        for key, value in items.items():
            self._data[(namespace, key)] = value

    def get_many(self, namespace, keys):
        return {k: self._data[(namespace, k)] for k in keys if (namespace, k) in self._data}

    def remove_many(self, namespace, keys):
        for k in keys:
            self._data.pop((namespace, k), None)


class FakeCacheMissError(Exception):
    pass


class FakeCache:
    def __init__(self):
        self._objects = {}

    def load(self, path):
        if path not in self._objects:
            raise FakeCacheMissError()
        return self._objects[path]

    def save(self, obj, path):
        self._objects[path] = obj

    def remove(self, path):
        if path not in self._objects:
            raise FakeCacheMissError()
        del self._objects[path]


class WatcherTestBase(unittest.TestCase):
    def setUp(self):
        self.kv = FakeKVStore()
        self.cache = FakeCache()
        self._patches = [
            mock.patch.object(watcher_mod, "_watcher_kv", self.kv),
            mock.patch.object(watcher_mod, "CacheMissError", FakeCacheMissError),
            mock.patch.object(watcher_mod, "load_cached_object", side_effect=self.cache.load),
            mock.patch.object(watcher_mod, "save_cached_object", side_effect=self.cache.save),
            mock.patch.object(watcher_mod, "remove_cached_object", side_effect=self.cache.remove),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()


# ----------------------------------------------------------------------------
# Pure helper functions

class HelperFunctionTests(unittest.TestCase):
    def test_parse_string_numeric_ordering(self):
        # "10" must sort after "2" numerically, not lexically
        self.assertLess(watcher_mod._parse_string("2"), watcher_mod._parse_string("10"))
        self.assertLess(watcher_mod._parse_string("144"), watcher_mod._parse_string("144а"))

    def test_parse_string_non_numeric_sorts_last(self):
        self.assertEqual(watcher_mod._parse_string("abc")[0], float('inf'))

    def test_sort_keys(self):
        self.assertEqual(watcher_mod._sort_keys(["10", "2", "1", "144а", "144"]), ["1", "2", "10", "144", "144а"])

    def test_make_tree_and_flatten_hierarchy(self):
        unresolved = {
            "Архів:ДААРК/1/2/3": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None},
        }
        tree = watcher_mod._make_tree(unresolved)
        flattened = dict(watcher_mod._flatten_hierarchy(tree))

        # every node's title reflects its own reconstructed path -- the
        # frontend reads meta.title directly, it doesn't derive it from the
        # tree path itself, so this must hold for leaves too, not just
        # synthesized intermediate nodes
        self.assertEqual(flattened["Архів:ДААРК/1/2/3"]["title"], "Архів:ДААРК/1/2/3")
        self.assertEqual(flattened["Архів:ДААРК/1/2/3"]["modified"], "2026-01-01T00:00:00Z")

        self.assertNotIn("modified", flattened["Архів:ДААРК/1"])
        self.assertEqual(flattened["Архів:ДААРК/1"]["title"], "Архів:ДААРК/1")
        self.assertEqual(flattened["Архів:ДААРК/1/2"]["title"], "Архів:ДААРК/1/2")

        # each node's "label" is its own latinized display segment (not the
        # full latinized path), for the frontend to show instead of the raw
        # Cyrillic tree key
        self.assertEqual(flattened["Архів:ДААРК/1/2/3"]["label"], "3")
        self.assertEqual(flattened["Архів:ДААРК/1"]["label"], "1")
        self.assertEqual(flattened["Архів:ДААРК"]["label"], "DAARK")

    def test_flatten_hierarchy_orders_children(self):
        unresolved = {
            "Архів:ДААРК/10": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None},
            "Архів:ДААРК/2": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None},
        }
        tree = watcher_mod._make_tree(unresolved)
        paths = [path for path, _ in watcher_mod._flatten_hierarchy(tree)]
        self.assertLess(paths.index("Архів:ДААРК/2"), paths.index("Архів:ДААРК/10"))

    def test_unresolved_tree_wraps_make_tree_and_flatten(self):
        unresolved = {"Архів:ДААРК/1": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None}}
        result = dict(watcher_mod.unresolved_tree(unresolved))
        self.assertEqual(result["Архів:ДААРК/1"]["title"], "Архів:ДААРК/1")


# ----------------------------------------------------------------------------
# storage primitives round-trip

class PrimitivesTests(WatcherTestBase):
    def test_watcher_header_round_trip(self):
        header = {"version": "v9", "include": ["Архів:ДААРК"], "exclude": [], "cutoff_date": "2025-01-01T00:00:00Z", "last_checked_date": "2025-01-01T00:00:00Z"}
        watcher_mod.put_watcher("a@example.com", "Архів:ДААРК", header)
        self.assertEqual(watcher_mod.get_watcher("a@example.com", "Архів:ДААРК"), header)

    def test_get_watcher_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            watcher_mod.get_watcher("nobody@example.com", "Архів:ДААРК")

    def test_unresolved_round_trip_and_remove(self):
        email, title, item = "a@example.com", "Архів:ДААРК", "Архів:ДААРК/1"
        watcher_mod.put_unresolved(email, title, item, {"modified": "2026-01-01T00:00:00Z"})
        self.assertEqual(watcher_mod.get_unresolved(email, title, item), {"modified": "2026-01-01T00:00:00Z"})
        self.assertEqual(watcher_mod.get_all_unresolved(email, title), {item: {"modified": "2026-01-01T00:00:00Z"}})

        watcher_mod.remove_unresolved(email, title, item)
        self.assertEqual(watcher_mod.get_all_unresolved(email, title), {})
        with self.assertRaises(KeyError):
            watcher_mod.get_unresolved(email, title, item)

    def test_resolved_defaults_to_empty_list(self):
        self.assertEqual(watcher_mod.get_resolved("a@example.com", "Архів:ДААРК", "Архів:ДААРК/1"), [])

    def test_resolved_round_trip(self):
        email, title, item = "a@example.com", "Архів:ДААРК", "Архів:ДААРК/1"
        history = [{"modified": "2026-01-01T00:00:00Z", "last_resolved": "2026-01-02T00:00:00Z"}]
        watcher_mod.put_resolved(email, title, item, history)
        self.assertEqual(watcher_mod.get_resolved(email, title, item), history)

    def test_remove_watcher_clears_all_three_namespaces_and_legacy_blob(self):
        email, title = "a@example.com", "Архів:ДААРК"
        watcher_mod.put_watcher(email, title, {"include": [title]})
        watcher_mod.put_unresolved(email, title, f"{title}/1", {"modified": "x"})
        watcher_mod.put_resolved(email, title, f"{title}/2", [{"modified": "y"}])
        self.cache.save({"anything": True}, watcher_mod._watcher_cache_path(email, title))

        watcher_mod.remove_watcher(email, title)

        with self.assertRaises(KeyError):
            watcher_mod.get_watcher(email, title)
        self.assertEqual(watcher_mod.get_all_unresolved(email, title), {})
        self.assertEqual(watcher_mod.get_resolved(email, title, f"{title}/2"), [])
        with self.assertRaises(FakeCacheMissError):
            self.cache.load(watcher_mod._watcher_cache_path(email, title))

    def test_remove_watcher_is_safe_with_nothing_to_remove(self):
        watcher_mod.remove_watcher("nobody@example.com", "Архів:ДААРК")  # must not raise


# ----------------------------------------------------------------------------
# resolve_watcher()

class ResolveWatcherTests(WatcherTestBase):
    EMAIL = "r@example.com"
    TITLE = "Архів:ДААРК"

    def setUp(self):
        super().setUp()
        watcher_mod.put_watcher(self.EMAIL, self.TITLE, {"include": [self.TITLE], "exclude": [], "cutoff_date": "2025-01-01T00:00:00Z", "last_checked_date": "2025-01-01T00:00:00Z"})
        self.items = {
            f"{self.TITLE}/100/1/5": {"modified": "2026-01-01T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z"},
            f"{self.TITLE}/100/1/6": {"modified": "2026-01-02T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z"},
            f"{self.TITLE}/200/1/5": {"modified": "2026-01-03T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z"},
        }
        for item, entry in self.items.items():
            watcher_mod.put_unresolved(self.EMAIL, self.TITLE, item, dict(entry))

    def test_resolve_raises_if_no_watcher(self):
        with self.assertRaises(FileNotFoundError):
            watcher_mod.resolve_watcher("nobody@example.com", self.TITLE, f"{self.TITLE}/1")

    def test_resolve_moves_item_and_stamps_now(self):
        item = f"{self.TITLE}/100/1/5"
        watcher_mod.resolve_watcher(self.EMAIL, self.TITLE, item)

        self.assertNotIn(item, watcher_mod.get_all_unresolved(self.EMAIL, self.TITLE))
        history = watcher_mod.get_resolved(self.EMAIL, self.TITLE, item)
        self.assertTrue(history)

        today = utc_now_dt().strftime("%Y-%m-%d")
        self.assertTrue(history[-1]["last_resolved"].startswith(today))
        # "modified" must be carried over unchanged, not overwritten
        self.assertEqual(history[-1]["modified"], "2026-01-01T00:00:00Z")

    def test_resolve_deep_only_matches_prefix(self):
        watcher_mod.resolve_watcher(self.EMAIL, self.TITLE, f"{self.TITLE}/100", deep=True)

        remaining = watcher_mod.get_all_unresolved(self.EMAIL, self.TITLE)
        self.assertNotIn(f"{self.TITLE}/100/1/5", remaining)
        self.assertNotIn(f"{self.TITLE}/100/1/6", remaining)
        self.assertTrue(watcher_mod.get_resolved(self.EMAIL, self.TITLE, f"{self.TITLE}/100/1/5"))
        self.assertTrue(watcher_mod.get_resolved(self.EMAIL, self.TITLE, f"{self.TITLE}/100/1/6"))

        # sibling under a different fond must be untouched
        self.assertIn(f"{self.TITLE}/200/1/5", remaining)
        self.assertEqual(watcher_mod.get_resolved(self.EMAIL, self.TITLE, f"{self.TITLE}/200/1/5"), [])

    def test_resolve_unknown_item_is_a_no_op(self):
        before = watcher_mod.get_all_unresolved(self.EMAIL, self.TITLE)
        result = watcher_mod.resolve_watcher(self.EMAIL, self.TITLE, f"{self.TITLE}/nope")
        self.assertEqual(result, before)


# ----------------------------------------------------------------------------
# check_watcher()
#
# Regression coverage for the two bugs that let already-resolved edits get
# permanently re-flagged as unresolved (last_resolved ending up
# chronologically *after* modified): reprocessing the entire unbounded
# update history every cycle, and comparing full-precision live timestamps
# against seconds-truncated legacy ones with a strict string compare.

class CheckWatcherTests(WatcherTestBase):
    EMAIL = "c@example.com"
    TITLE = "Архів:ДААРК"

    def _check(self, manager, cutoff_date="2025-01-01T00:00:00Z"):
        return watcher_mod.check_watcher(
            self.EMAIL, self.TITLE, FakeRuntime(manager),
            include=[self.TITLE], cutoff_date=cutoff_date)

    def test_check_creates_unresolved_entries(self):
        updates = {
            f"{self.TITLE}/100/1/5": {"timestamp": "2026-01-01T10:00:00Z", "user": "alice"},
        }
        manager = FakePageUpdateManager(updates)

        unresolved = self._check(manager)

        entry = unresolved[f"{self.TITLE}/100/1/5"]
        self.assertEqual(entry["modified"], "2026-01-01T10:00:00Z")
        self.assertEqual(entry["user"], "alice")
        self.assertEqual(watcher_mod.get_watcher(self.EMAIL, self.TITLE)["last_checked_date"], "2026-01-01T10:00:00Z")

    def test_check_passes_last_checked_date_as_cutoff(self):
        updates = {
            f"{self.TITLE}/100/1/5": {"timestamp": "2026-01-01T10:00:00Z", "user": "alice"},
            f"{self.TITLE}/100/1/6": {"timestamp": "2026-01-02T10:00:00Z", "user": "bob"},
        }
        manager = FakePageUpdateManager(updates)

        self._check(manager)
        self._check(manager)

        # second call must be bounded by what the first call advanced
        # last_checked_date to, not re-scan unbounded history (cutoff_date=None)
        self.assertEqual(manager.calls[0], ((self.TITLE,), (), "2025-01-01T00:00:00Z"))
        self.assertEqual(manager.calls[1], ((self.TITLE,), (), "2026-01-02T10:00:00Z"))

    def test_check_does_not_reflag_already_resolved_edit(self):
        # resolved["modified"] simulates a legacy date that passed through
        # to_utc_format() and had its seconds permanently zeroed; the "live"
        # update feed reports the same edit with real (non-zero) seconds
        # because get_updates is unbounded and keeps returning it.
        item = f"{self.TITLE}/100/1/5"
        watcher_mod.put_resolved(self.EMAIL, self.TITLE, item, [{
            "modified": "2026-03-27T19:29:00Z",
            "last_resolved": "2026-03-29T13:42:00Z",
            "user": "someone",
        }])
        watcher_mod.put_watcher(self.EMAIL, self.TITLE, {
            "include": [self.TITLE], "exclude": [],
            "cutoff_date": "2025-01-01T00:00:00Z", "last_checked_date": "2025-01-01T00:00:00Z",
        })
        updates = {item: {"timestamp": "2026-03-27T19:29:30Z", "user": "someone"}}
        manager = FakePageUpdateManager(updates)

        unresolved = self._check(manager)

        self.assertNotIn(item, unresolved)

    def test_check_flags_genuine_new_edit_after_resolve(self):
        item = f"{self.TITLE}/100/1/5"
        watcher_mod.put_resolved(self.EMAIL, self.TITLE, item, [{
            "modified": "2026-03-27T19:29:00Z",
            "last_resolved": "2026-03-29T13:42:00Z",
            "user": "someone",
        }])
        watcher_mod.put_watcher(self.EMAIL, self.TITLE, {
            "include": [self.TITLE], "exclude": [],
            "cutoff_date": "2025-01-01T00:00:00Z", "last_checked_date": "2025-01-01T00:00:00Z",
        })
        updates = {item: {"timestamp": "2026-04-01T10:00:00Z", "user": "someone-else"}}
        manager = FakePageUpdateManager(updates)

        unresolved = self._check(manager)

        self.assertIn(item, unresolved)
        entry = unresolved[item]
        self.assertEqual(entry["modified"], "2026-04-01T10:00:00Z")
        # last_resolved carried forward from the prior resolve action
        self.assertEqual(entry["last_resolved"], "2026-03-29T13:42:00Z")
        self.assertLess(entry["last_resolved"], entry["modified"])

    def test_check_creates_watcher_header_even_with_no_updates(self):
        manager = FakePageUpdateManager({})
        self._check(manager)
        header = watcher_mod.get_watcher(self.EMAIL, self.TITLE)
        self.assertEqual(header["include"], [self.TITLE])
        self.assertEqual(header["last_checked_date"], "2025-01-01T00:00:00Z")


# ----------------------------------------------------------------------------
# legacy blob migration
#
# Legacy fixtures use "DAARK"/"D" (a real, single-subarchive archive) so
# archive_root()/page_title_from_address() work without mocking. Every
# pre-v8 load also runs the v8 upgrade (re-keying resolved/unresolved from
# comma-joined address tuples to plain titles), so assertions check the
# retitled key, not the original comma-joined one.

class LegacyBlobLoadTests(unittest.TestCase):
    def test_load_current_shape_passes_through(self):
        data = {
            "version": "v8", "include": ["Архів:ДААРК"], "exclude": [], "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {"Архів:ДААРК/1": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None}},
            "resolved": {"Архів:ДААРК/2": [{"modified": "2025-06-01T00:00:00Z", "last_resolved": "2025-06-02T00:00:00Z"}]},
        }
        header, resolved, unresolved = watcher_mod._load_legacy_blob(data)
        self.assertEqual(header["version"], "v9")
        self.assertEqual(header["include"], ["Архів:ДААРК"])
        self.assertEqual(header["exclude"], [])
        self.assertEqual(unresolved, data["unresolved"])
        self.assertEqual(resolved, data["resolved"])

    def test_load_legacy_archive_subarchive_shape_coarsens_to_archive_title(self):
        data = {
            "version": "v7",
            "archive": "DACHGO", "subarchive": "R", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {}, "resolved": {},
        }
        header, _, _ = watcher_mod._load_legacy_blob(data)
        self.assertEqual(header["include"], ["Архів:ДАЧгО"])
        self.assertEqual(header["exclude"], [])

    def test_load_v1_normalizes_bare_resolved_dates_to_lists(self):
        # v1 stores are also pre-v5, so cutoff_date/resolved dates must be in
        # the legacy comma format the v5 migration (which runs right after)
        # expects -- to_utc_format() has no fallback for already-ISO input.
        # v1 is also pre-v4, so the forced-refresh check() fires too and
        # needs a (here, empty) live runtime to not crash.
        data = {
            "version": "v1",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025,01,01,00:00",
            "unresolved": {},
            "resolved": {"DAARK,D,1,,": "2025,06,01,08:15"},
        }
        _, resolved, _ = watcher_mod._load_legacy_blob(data, runtime=FakeRuntime(FakePageUpdateManager({})))
        self.assertEqual(
            resolved["Архів:ДААРК/1"],
            [{"modified": to_utc_format("2025,06,01,08:15"), "last_resolved": to_utc_format("2025,01,01,00:00")}],
        )

    def test_load_v2_patches_missing_titles(self):
        # v2 is also pre-v4, so the forced-refresh check() wipes unresolved
        # right after this patch runs -- only the resolved side (untouched
        # by that wipe) is observable here.
        data = {
            "version": "v2",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025,01,01,00:00",
            "unresolved": {},
            "resolved": {"DAARK,D,1,,": [{"modified": "2025,06,01,08:15", "last_resolved": "2025,06,02,00:00"}]},
        }
        _, resolved, _ = watcher_mod._load_legacy_blob(data, runtime=FakeRuntime(FakePageUpdateManager({})))
        self.assertEqual(resolved["Архів:ДААРК/1"][0]["title"], "Архів:ДААРК/1")

    def test_load_v5_migrates_legacy_comma_dates(self):
        # version="v4": below v5 (so the date migration fires) but not below
        # v4 (so the forced-refresh check(), which needs a live runtime,
        # doesn't) -- isolates the v5 migration on its own.
        data = {
            "version": "v4",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025,03,01,12:30",
            "unresolved": {"DAARK,D,1,,": {"modified": "2025,06,01,08:15", "last_resolved": "2025,03,01,00:00", "title": "t"}},
            "resolved": {"DAARK,D,2,,": [{"modified": "2025,05,01,09:00", "last_resolved": "2025,05,02,10:00", "title": "u"}]},
        }
        header, resolved, unresolved = watcher_mod._load_legacy_blob(data)

        self.assertEqual(header["cutoff_date"], to_utc_format("2025,03,01,12:30"))
        self.assertEqual(unresolved["Архів:ДААРК/1"]["modified"], to_utc_format("2025,06,01,08:15"))
        self.assertEqual(resolved["Архів:ДААРК/2"][0]["modified"], to_utc_format("2025,05,01,09:00"))
        # to_utc_format has no seconds field to preserve -- documents the
        # precision loss that makes the v6/v7 cleanups necessary
        self.assertTrue(header["cutoff_date"].endswith(":00Z"))

    def test_load_v4_forces_refresh_via_check(self):
        data = {
            "version": "v3",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025,01,01,00:00",
            "unresolved": {"DAARK,D,stale,,": {"modified": "2020,01,01,00:00", "last_resolved": None, "title": "stale"}},
            "resolved": {},
        }
        manager = FakePageUpdateManager({
            "Архів:ДААРК/1": {"timestamp": "2026-01-01T00:00:00Z", "user": ""},
        })
        _, _, unresolved = watcher_mod._load_legacy_blob(data, runtime=FakeRuntime(manager))

        # stale pre-migration entry discarded, replaced by a fresh check()
        self.assertNotIn("Архів:ДААРК/stale", unresolved)
        self.assertIn("Архів:ДААРК/1", unresolved)

    def test_load_v6_purges_matching_ghost_on_upgrade(self):
        data = {
            "version": "v5",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {"DAARK,D,1,,": {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {"DAARK,D,1,,": [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        _, _, unresolved = watcher_mod._load_legacy_blob(data)
        self.assertNotIn("Архів:ДААРК/1", unresolved)

    def test_load_v7_purges_ghost_left_by_already_v6_tagged_store(self):
        # Reproduces the real-world bug: a store already saved at "v6" (so
        # the v6 block above won't fire again) that accumulated a new ghost
        # entry afterward because check() kept re-flagging it. This is
        # exactly the shape found in the production AGAD/CDIAK watcher files.
        data = {
            "version": "v6",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {"DAARK,D,1,,": {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {"DAARK,D,1,,": [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        _, _, unresolved = watcher_mod._load_legacy_blob(data)
        self.assertNotIn("Архів:ДААРК/1", unresolved)

    def test_load_v7_preserves_genuinely_unresolved_items(self):
        # A real new edit landing on a different minute than the last
        # resolved copy must survive the v7 cleanup, not just anything with
        # resolved history.
        data = {
            "version": "v6",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {"DAARK,D,1,,": {"modified": "2026-04-11T15:31:18Z", "last_resolved": "2026-04-12T08:00:00Z", "title": "t"}},
            "resolved": {"DAARK,D,1,,": [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-04-12T08:00:00Z", "title": "t"}]},
        }
        _, _, unresolved = watcher_mod._load_legacy_blob(data)
        self.assertIn("Архів:ДААРК/1", unresolved)

    def test_load_already_v7_retitles_but_does_not_rerun_other_migrations(self):
        data = {
            "version": "v7",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {"DAARK,D,1,,": {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {"DAARK,D,1,,": [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        _, _, unresolved = watcher_mod._load_legacy_blob(data)
        # already at v7: the v6/v7 ghost-purge migrations don't re-run, so a
        # ghost sitting in an already-v7 file (shouldn't happen going
        # forward, but if it did) is left alone -- only re-keyed to a title
        self.assertIn("Архів:ДААРК/1", unresolved)


class LegacyBlobLazyMigrationTests(WatcherTestBase):
    def test_ensure_migrated_transforms_legacy_blob_into_kv_rows_and_quarantines_it(self):
        email, title = "legacy@example.com", "Архів:ДААРК"
        path = watcher_mod._watcher_cache_path(email, title)
        blob = {
            "version": "v8", "include": [title], "exclude": [], "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {f"{title}/1": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None}},
            "resolved": {f"{title}/2": [{"modified": "2025-06-01T00:00:00Z", "last_resolved": "2025-06-02T00:00:00Z"}]},
        }
        self.cache.save(blob, path)

        watcher_mod._ensure_migrated(email, title, runtime=None)

        header = watcher_mod.get_watcher(email, title)
        self.assertEqual(header["include"], [title])
        self.assertEqual(watcher_mod.get_all_unresolved(email, title), {
            f"{title}/1": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None},
        })
        self.assertEqual(
            watcher_mod.get_resolved(email, title, f"{title}/2"),
            [{"modified": "2025-06-01T00:00:00Z", "last_resolved": "2025-06-02T00:00:00Z"}],
        )
        # gone from its live lookup path -- can't be found (and its
        # already-migrated history resurrected) by a later remove/re-add --
        # but quarantined rather than deleted, as a recovery trail
        with self.assertRaises(FakeCacheMissError):
            self.cache.load(path)
        quarantine_path = path.replace("watchers/", "watchers/_migrated/", 1)
        self.assertEqual(self.cache.load(quarantine_path), blob)

    def test_ensure_migrated_is_a_no_op_for_a_brand_new_watch(self):
        watcher_mod._ensure_migrated("nobody@example.com", "Архів:ДААРК", runtime=None)
        with self.assertRaises(KeyError):
            watcher_mod.get_watcher("nobody@example.com", "Архів:ДААРК")

    def test_ensure_migrated_merges_multiple_legacy_subarchive_blobs(self):
        # Regression test for the real production bug (found 2026-08-21): a
        # multi-subarchive archive like DAChgO ("D" and "R") had one legacy
        # blob per subarchive, keyed by the old "ARCHIVEKEY-SUBARCHIVEKEY"
        # address pair. The address-to-title redesign coarsened watchlist
        # entries to the whole archive but never consolidated these blobs --
        # so the first touch after that rollout hit a cache miss on the new
        # title-keyed path and silently started over, discarding all
        # resolved/unresolved history in both legacy blobs.
        email, title = "multi@example.com", "Архів:ДАЧгО"
        path_d = "watchers/multi@example.com/DACHGO-D.json"
        path_r = "watchers/multi@example.com/DACHGO-R.json"
        self.cache.save({
            "version": "v7", "archive": "DACHGO", "subarchive": "D",
            "cutoff_date": "2025-02-02T00:00:00Z", "last_checked_date": "2026-07-24T07:00:19Z",
            "unresolved": {},
            "resolved": {"DACHGO,D,151,1,85": [
                {"modified": "2026-04-28T17:20:00Z", "last_resolved": "2026-04-29T15:05:00Z", "user": "Smaxims"},
            ]},
        }, path_d)
        self.cache.save({
            "version": "v7", "archive": "DACHGO", "subarchive": "R",
            "cutoff_date": "2025-04-10T00:00:00Z", "last_checked_date": "2026-07-23T12:34:35Z",
            "unresolved": {"DACHGO,R,Р-9022,1,151": {
                "modified": "2026-06-10T11:40:00Z", "last_resolved": "2026-04-10T00:00:00Z", "user": "Smaxims",
            }},
            "resolved": {},
        }, path_r)

        watcher_mod._ensure_migrated(email, title, runtime=None)

        header = watcher_mod.get_watcher(email, title)
        self.assertEqual(header["include"], [title])
        # oldest cutoff/last_checked across both merged blobs, not either one alone
        self.assertEqual(header["cutoff_date"], "2025-02-02T00:00:00Z")
        self.assertEqual(header["last_checked_date"], "2026-07-23T12:34:35Z")

        self.assertEqual(
            watcher_mod.get_resolved(email, title, "Архів:ДАЧгО/151/1/85"),
            [{"modified": "2026-04-28T17:20:00Z", "last_resolved": "2026-04-29T15:05:00Z", "user": "Smaxims"}],
        )
        unresolved = watcher_mod.get_all_unresolved(email, title)
        self.assertIn("Архів:ДАЧгО/Р-9022/1/151", unresolved)

        # both legacy blobs consumed, not just one -- and quarantined, not deleted
        for path in (path_d, path_r):
            with self.assertRaises(FakeCacheMissError):
                self.cache.load(path)
            quarantine_path = path.replace("watchers/", "watchers/_migrated/", 1)
            self.cache.load(quarantine_path)  # must not raise

    def test_check_watcher_migrates_legacy_blob_before_checking(self):
        email, title = "legacy2@example.com", "Архів:ДААРК"
        path = watcher_mod._watcher_cache_path(email, title)
        self.cache.save({
            "version": "v8", "include": [title], "exclude": [], "cutoff_date": "2025-01-01T00:00:00Z",
            "last_checked_date": "2025-06-01T00:00:00Z",
            "unresolved": {}, "resolved": {},
        }, path)
        manager = FakePageUpdateManager({})

        watcher_mod.check_watcher(email, title, FakeRuntime(manager), include=[title], cutoff_date="2025-01-01T00:00:00Z")

        # bounded by the migrated header's last_checked_date, not the fresh cutoff_date passed in
        self.assertEqual(manager.calls[0], ((title,), (), "2025-06-01T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
