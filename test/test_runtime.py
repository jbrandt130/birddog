import unittest
from unittest import mock

from birddog.runtime import (
    ArchiveWatcher,
    PageUpdateManager,
    _parse_string,
    _sort_keys,
    _flatten_hierarchy,
    _make_tree,
    )
from birddog.utility import utc_now_dt, to_utc_format


# ----------------------------------------------------------------------------
# Test doubles
#
# ArchiveWatcher.check() only ever touches self._runtime.update_manager, so a
# minimal fake covering that one attribute is enough to unit test it without
# any live wiki/AWS access. FakePageUpdateManager mirrors the real
# PageUpdateManager.get_updates() cutoff_date filtering (cutoff_date=None means
# "no floor") so tests can verify ArchiveWatcher actually narrows its queries.
#
# Fixtures use the real "Архів:ДААРК" (single subarchive) and "Архів:ДАЧгО"
# (D/R subarchives) archives, same ones test_wiki.py/test_user.py already use,
# so no mocking of archive_root()/page_title_from_address() is needed.

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


class FakeTracker:
    def __init__(self, updates_by_prefix):
        self._updates_by_prefix = updates_by_prefix
        self.calls = []

    def get_updates(self, prefix, cutoff_date=None):
        self.calls.append((prefix, cutoff_date))
        return dict(self._updates_by_prefix.get(prefix, {}))


# ----------------------------------------------------------------------------
# Pure helper functions

class HelperFunctionTests(unittest.TestCase):
    def test_parse_string_numeric_ordering(self):
        # "10" must sort after "2" numerically, not lexically
        self.assertLess(_parse_string("2"), _parse_string("10"))
        self.assertLess(_parse_string("144"), _parse_string("144а"))

    def test_parse_string_non_numeric_sorts_last(self):
        self.assertEqual(_parse_string("abc")[0], float('inf'))

    def test_sort_keys(self):
        self.assertEqual(_sort_keys(["10", "2", "1", "144а", "144"]), ["1", "2", "10", "144", "144а"])

    def test_make_tree_and_flatten_hierarchy(self):
        unresolved = {
            "Архів:ДААРК/1/2/3": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None},
        }
        tree = _make_tree(unresolved)
        flattened = dict(_flatten_hierarchy(tree))

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
        tree = _make_tree(unresolved)
        paths = [path for path, _ in _flatten_hierarchy(tree)]
        self.assertLess(paths.index("Архів:ДААРК/2"), paths.index("Архів:ДААРК/10"))


# ----------------------------------------------------------------------------
# ArchiveWatcher.resolve() / unresolve()

class ArchiveWatcherResolveTests(unittest.TestCase):
    def setUp(self):
        self.watcher = ArchiveWatcher(["Архів:ДААРК"], cutoff_date="2025-01-01T00:00:00Z")
        self.watcher._unresolved = {
            "Архів:ДААРК/100/1/5": {"modified": "2026-01-01T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z"},
            "Архів:ДААРК/100/1/6": {"modified": "2026-01-02T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z"},
            "Архів:ДААРК/200/1/5": {"modified": "2026-01-03T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z"},
        }

    def test_resolve_moves_item_and_stamps_now(self):
        item = "Архів:ДААРК/100/1/5"
        self.watcher.resolve(item)
        self.assertNotIn(item, self.watcher.unresolved)
        self.assertIn(item, self.watcher.resolved)

        today = utc_now_dt().strftime("%Y-%m-%d")
        last_resolved = self.watcher.resolved[item][-1]["last_resolved"]
        self.assertTrue(last_resolved.startswith(today))
        # "modified" must be carried over unchanged, not overwritten
        self.assertEqual(self.watcher.resolved[item][-1]["modified"], "2026-01-01T00:00:00Z")

    def test_resolve_deep_only_matches_prefix(self):
        self.watcher.resolve("Архів:ДААРК/100", deep=True)
        self.assertNotIn("Архів:ДААРК/100/1/5", self.watcher.unresolved)
        self.assertNotIn("Архів:ДААРК/100/1/6", self.watcher.unresolved)
        self.assertIn("Архів:ДААРК/100/1/5", self.watcher.resolved)
        self.assertIn("Архів:ДААРК/100/1/6", self.watcher.resolved)
        # sibling under a different fond must be untouched
        self.assertIn("Архів:ДААРК/200/1/5", self.watcher.unresolved)
        self.assertNotIn("Архів:ДААРК/200/1/5", self.watcher.resolved)

    def test_unresolve_reverses_resolve(self):
        item = "Архів:ДААРК/100/1/5"
        self.watcher.resolve(item)
        self.watcher.unresolve(item)
        self.assertIn(item, self.watcher.unresolved)
        self.assertNotIn(item, self.watcher.resolved)

    def test_unresolve_cleans_up_empty_resolved_list(self):
        item = "Архів:ДААРК/100/1/5"
        self.watcher.resolve(item)
        self.watcher.unresolve(item)
        self.assertNotIn(item, self.watcher._resolved)


# ----------------------------------------------------------------------------
# ArchiveWatcher.check()
#
# These are regression tests for the two bugs that let already-resolved
# edits get permanently re-flagged as unresolved (last_resolved ending up
# chronologically *after* modified): check() reprocessing the entire
# unbounded update history every cycle, and comparing full-precision live
# timestamps against seconds-truncated legacy ones with a strict string
# compare.

class ArchiveWatcherCheckTests(unittest.TestCase):
    def _watcher(self, cutoff_date="2025-01-01T00:00:00Z"):
        return ArchiveWatcher(["Архів:ДААРК"], cutoff_date=cutoff_date)

    def test_check_creates_unresolved_entries(self):
        updates = {
            "Архів:ДААРК/100/1/5": {"timestamp": "2026-01-01T10:00:00Z", "user": "alice"},
        }
        manager = FakePageUpdateManager(updates)
        watcher = self._watcher()
        watcher._runtime = FakeRuntime(manager)

        watcher.check()

        entry = watcher.unresolved["Архів:ДААРК/100/1/5"]
        self.assertEqual(entry["modified"], "2026-01-01T10:00:00Z")
        self.assertEqual(entry["user"], "alice")
        self.assertEqual(watcher._last_checked_date, "2026-01-01T10:00:00Z")

    def test_check_passes_last_checked_date_as_cutoff(self):
        updates = {
            "Архів:ДААРК/100/1/5": {"timestamp": "2026-01-01T10:00:00Z", "user": "alice"},
            "Архів:ДААРК/100/1/6": {"timestamp": "2026-01-02T10:00:00Z", "user": "bob"},
        }
        manager = FakePageUpdateManager(updates)
        watcher = self._watcher()
        watcher._runtime = FakeRuntime(manager)

        watcher.check()
        watcher.check()

        # second call must be bounded by what the first call advanced
        # last_checked_date to, not re-scan unbounded history (cutoff_date=None)
        self.assertEqual(manager.calls[0], (("Архів:ДААРК",), (), "2025-01-01T00:00:00Z"))
        self.assertEqual(manager.calls[1], (("Архів:ДААРК",), (), "2026-01-02T10:00:00Z"))

    def test_check_does_not_reflag_already_resolved_edit(self):
        # resolved["modified"] simulates a legacy date that passed through
        # to_utc_format() and had its seconds permanently zeroed; the "live"
        # update feed reports the same edit with real (non-zero) seconds
        # because get_updates is unbounded and keeps returning it.
        item = "Архів:ДААРК/100/1/5"
        watcher = self._watcher()
        watcher._resolved = {
            item: [{
                "modified": "2026-03-27T19:29:00Z",
                "last_resolved": "2026-03-29T13:42:00Z",
                "user": "someone",
            }],
        }
        watcher._last_checked_date = "2025-01-01T00:00:00Z"
        updates = {item: {"timestamp": "2026-03-27T19:29:30Z", "user": "someone"}}
        watcher._runtime = FakeRuntime(FakePageUpdateManager(updates))

        watcher.check()

        self.assertNotIn(item, watcher.unresolved)

    def test_check_flags_genuine_new_edit_after_resolve(self):
        item = "Архів:ДААРК/100/1/5"
        watcher = self._watcher()
        watcher._resolved = {
            item: [{
                "modified": "2026-03-27T19:29:00Z",
                "last_resolved": "2026-03-29T13:42:00Z",
                "user": "someone",
            }],
        }
        watcher._last_checked_date = "2025-01-01T00:00:00Z"
        updates = {item: {"timestamp": "2026-04-01T10:00:00Z", "user": "someone-else"}}
        watcher._runtime = FakeRuntime(FakePageUpdateManager(updates))

        watcher.check()

        self.assertIn(item, watcher.unresolved)
        entry = watcher.unresolved[item]
        self.assertEqual(entry["modified"], "2026-04-01T10:00:00Z")
        # last_resolved carried forward from the prior resolve action
        self.assertEqual(entry["last_resolved"], "2026-03-29T13:42:00Z")
        self.assertLess(entry["last_resolved"], entry["modified"])


# ----------------------------------------------------------------------------
# ArchiveWatcher.save() / load() version migrations
#
# Legacy fixtures use "DAARK"/"D" (a real, single-subarchive archive) so
# archive_root()/page_title_from_address() work without mocking. Every
# pre-v8 load also runs the v8 upgrade (re-keying resolved/unresolved from
# comma-joined address tuples to plain titles), so assertions check the
# retitled key, not the original comma-joined one.

class ArchiveWatcherSaveLoadTests(unittest.TestCase):
    def test_save_round_trip_is_v8(self):
        watcher = ArchiveWatcher(["Архів:ДААРК"], cutoff_date="2025-01-01T00:00:00Z")
        watcher._unresolved = {"Архів:ДААРК/1": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None}}
        watcher._resolved = {"Архів:ДААРК/2": [{"modified": "2025-06-01T00:00:00Z", "last_resolved": "2025-06-02T00:00:00Z"}]}

        saved = watcher.save()
        self.assertEqual(saved["version"], "v8")
        self.assertEqual(saved["include"], ["Архів:ДААРК"])
        self.assertEqual(saved["exclude"], [])

        reloaded = ArchiveWatcher.load(saved)
        self.assertEqual(reloaded.unresolved, watcher.unresolved)
        self.assertEqual(reloaded.resolved, watcher.resolved)

    def test_load_legacy_archive_subarchive_shape_coarsens_to_archive_title(self):
        data = {
            "version": "v7",
            "archive": "DACHGO", "subarchive": "R", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {}, "resolved": {},
        }
        watcher = ArchiveWatcher.load(data)
        self.assertEqual(watcher._include, ["Архів:ДАЧгО"])
        self.assertEqual(watcher._exclude, [])

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
        watcher = ArchiveWatcher.load(data, runtime=FakeRuntime(FakePageUpdateManager({})))
        self.assertEqual(
            watcher.resolved["Архів:ДААРК/1"],
            [{"modified": to_utc_format("2025,06,01,08:15"), "last_resolved": to_utc_format("2025,01,01,00:00")}],
            )

    def test_load_v2_patches_missing_titles(self):
        # v2 is also pre-v4, so the forced-refresh check() wipes _unresolved
        # right after this patch runs -- only the _resolved side (untouched
        # by that wipe) is observable here.
        data = {
            "version": "v2",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025,01,01,00:00",
            "unresolved": {},
            "resolved": {"DAARK,D,1,,": [{"modified": "2025,06,01,08:15", "last_resolved": "2025,06,02,00:00"}]},
        }
        watcher = ArchiveWatcher.load(data, runtime=FakeRuntime(FakePageUpdateManager({})))
        self.assertEqual(watcher.resolved["Архів:ДААРК/1"][0]["title"], "Архів:ДААРК/1")

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
        watcher = ArchiveWatcher.load(data)

        self.assertEqual(watcher.cutoff_date, to_utc_format("2025,03,01,12:30"))
        self.assertEqual(watcher.unresolved["Архів:ДААРК/1"]["modified"], to_utc_format("2025,06,01,08:15"))
        self.assertEqual(watcher.resolved["Архів:ДААРК/2"][0]["modified"], to_utc_format("2025,05,01,09:00"))
        # to_utc_format has no seconds field to preserve -- documents the
        # precision loss that makes the v6/v7 cleanups necessary
        self.assertTrue(watcher.cutoff_date.endswith(":00Z"))

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
        watcher = ArchiveWatcher.load(data, runtime=FakeRuntime(manager))

        # stale pre-migration entry discarded, replaced by a fresh check()
        self.assertNotIn("Архів:ДААРК/stale", watcher.unresolved)
        self.assertIn("Архів:ДААРК/1", watcher.unresolved)

    def test_load_v6_purges_matching_ghost_on_upgrade(self):
        data = {
            "version": "v5",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {"DAARK,D,1,,": {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {"DAARK,D,1,,": [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        watcher = ArchiveWatcher.load(data)
        self.assertNotIn("Архів:ДААРК/1", watcher.unresolved)

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
        watcher = ArchiveWatcher.load(data)
        self.assertNotIn("Архів:ДААРК/1", watcher.unresolved)

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
        watcher = ArchiveWatcher.load(data)
        self.assertIn("Архів:ДААРК/1", watcher.unresolved)

    def test_load_already_v7_retitles_but_does_not_rerun_other_migrations(self):
        data = {
            "version": "v7",
            "archive": "DAARK", "subarchive": "D", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {"DAARK,D,1,,": {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {"DAARK,D,1,,": [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        watcher = ArchiveWatcher.load(data)
        # already at v7: the v6/v7 ghost-purge migrations don't re-run, so a
        # ghost sitting in an already-v7 file (shouldn't happen going
        # forward, but if it did) is left alone -- only re-keyed to a title
        self.assertIn("Архів:ДААРК/1", watcher.unresolved)


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
