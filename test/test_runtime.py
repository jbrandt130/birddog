import unittest
from unittest import mock

from birddog.runtime import (
    ArchiveWatcher,
    _title_from_name,
    _parse_string,
    _sort_keys,
    _flatten_hierarchy,
    _make_tree,
    )
from birddog.wiki import ARCHIVE_BY_ADDRESS, archive_root
from birddog.utility import utc_now_dt, to_utc_format


# ----------------------------------------------------------------------------
# Test doubles
#
# ArchiveWatcher.check() only ever touches self._runtime.update_manager, so a
# minimal fake covering that one attribute is enough to unit test it without
# any live wiki/AWS access. FakePageUpdateManager mirrors the real
# PageTracker.get_updates() cutoff_date filtering (cutoff_date=None means "no
# floor") so tests can verify ArchiveWatcher actually narrows its queries.

class FakePageUpdateManager:
    def __init__(self, updates):
        self._updates = updates
        self.calls = []

    def get_updates(self, archive, subarchive, cutoff_date=None):
        self.calls.append((archive, subarchive, cutoff_date))
        floor = cutoff_date or "0"
        return {
            title: update
            for title, update in self._updates.items()
            if update["timestamp"] >= floor
        }


class FakeRuntime:
    def __init__(self, update_manager):
        self.update_manager = update_manager


def _fake_page_address(title):
    # Test titles are pre-formatted as ArchiveWatcher key strings
    # ("ARCHIVE,SUB,fond,opus,case"), so recovering the address is just a split.
    parts = title.split(",")
    parts += [""] * (5 - len(parts))
    return tuple(parts[:5])


def _fake_page_title_from_address(address):
    return "/".join(part for part in address if part)


PATCH_ADDRESS = mock.patch("birddog.runtime.page_address", side_effect=_fake_page_address)
PATCH_TITLE = mock.patch("birddog.runtime.page_title_from_address", side_effect=_fake_page_title_from_address)


# ----------------------------------------------------------------------------
# Pure helper functions

class HelperFunctionTests(unittest.TestCase):
    def test_title_from_name_top_level(self):
        self.assertEqual(_title_from_name("DAKO-D"), ARCHIVE_BY_ADDRESS[("DAKO", "D")])

    def test_title_from_name_nested(self):
        self.assertEqual(
            _title_from_name("DAKO-D/1/2"),
            f"{archive_root('DAKO', 'D')}/1/2",
            )

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
            "DAKO,D,1,2,3": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None, "title": "leaf"},
        }
        tree = _make_tree(unresolved)
        flattened = dict(_flatten_hierarchy(tree))

        # the leaf itself carries its own record
        self.assertEqual(flattened["DAKO-D/1/2/3"]["title"], "leaf")

        # intermediate nodes with no unresolved record of their own get a
        # synthesized title and no modified/last_resolved fields
        self.assertNotIn("modified", flattened["DAKO-D/1"])
        self.assertEqual(flattened["DAKO-D/1"]["title"], _title_from_name("DAKO-D/1"))
        self.assertEqual(flattened["DAKO-D/1/2"]["title"], _title_from_name("DAKO-D/1/2"))

    def test_flatten_hierarchy_orders_children(self):
        unresolved = {
            "DAKO,D,10,,": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None, "title": "t10"},
            "DAKO,D,2,,": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None, "title": "t2"},
        }
        tree = _make_tree(unresolved)
        paths = [path for path, _ in _flatten_hierarchy(tree)]
        self.assertLess(paths.index("DAKO-D/2"), paths.index("DAKO-D/10"))


# ----------------------------------------------------------------------------
# ArchiveWatcher.key / resolve / unresolve

class ArchiveWatcherKeyTests(unittest.TestCase):
    def test_key_join(self):
        self.assertEqual(ArchiveWatcher.key("A", "B", "1", "2", "3"), "A,B,1,2,3")

    def test_key_defaults_none_to_empty(self):
        self.assertEqual(ArchiveWatcher.key("A", "B"), "A,B,,,")


class ArchiveWatcherResolveTests(unittest.TestCase):
    def setUp(self):
        self.watcher = ArchiveWatcher("TEST", "_", cutoff_date="2025-01-01T00:00:00Z")
        self.watcher._unresolved = {
            "TEST,_,100,1,5": {"modified": "2026-01-01T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z", "title": "a"},
            "TEST,_,100,1,6": {"modified": "2026-01-02T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z", "title": "b"},
            "TEST,_,200,1,5": {"modified": "2026-01-03T00:00:00Z", "last_resolved": "2025-01-01T00:00:00Z", "title": "c"},
        }

    def test_resolve_moves_item_and_stamps_now(self):
        item = "TEST,_,100,1,5"
        self.watcher.resolve(item)
        self.assertNotIn(item, self.watcher.unresolved)
        self.assertIn(item, self.watcher.resolved)

        today = utc_now_dt().strftime("%Y-%m-%d")
        last_resolved = self.watcher.resolved[item][-1]["last_resolved"]
        self.assertTrue(last_resolved.startswith(today))
        # "modified" must be carried over unchanged, not overwritten
        self.assertEqual(self.watcher.resolved[item][-1]["modified"], "2026-01-01T00:00:00Z")

    def test_resolve_deep_only_matches_prefix(self):
        self.watcher.resolve("TEST,_,100,,", deep=True)
        self.assertNotIn("TEST,_,100,1,5", self.watcher.unresolved)
        self.assertNotIn("TEST,_,100,1,6", self.watcher.unresolved)
        self.assertIn("TEST,_,100,1,5", self.watcher.resolved)
        self.assertIn("TEST,_,100,1,6", self.watcher.resolved)
        # sibling under a different fond must be untouched
        self.assertIn("TEST,_,200,1,5", self.watcher.unresolved)
        self.assertNotIn("TEST,_,200,1,5", self.watcher.resolved)

    def test_unresolve_reverses_resolve(self):
        item = "TEST,_,100,1,5"
        self.watcher.resolve(item)
        self.watcher.unresolve(item)
        self.assertIn(item, self.watcher.unresolved)
        self.assertNotIn(item, self.watcher.resolved)

    def test_unresolve_cleans_up_empty_resolved_list(self):
        item = "TEST,_,100,1,5"
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
        return ArchiveWatcher("TEST", "_", cutoff_date=cutoff_date)

    def test_check_creates_unresolved_entries(self):
        updates = {
            "TEST,_,100,1,5": {"timestamp": "2026-01-01T10:00:00Z", "user": "alice"},
        }
        manager = FakePageUpdateManager(updates)
        watcher = self._watcher()
        watcher._runtime = FakeRuntime(manager)

        with PATCH_ADDRESS:
            watcher.check()

        entry = watcher.unresolved["TEST,_,100,1,5"]
        self.assertEqual(entry["modified"], "2026-01-01T10:00:00Z")
        self.assertEqual(entry["user"], "alice")
        self.assertEqual(watcher._last_checked_date, "2026-01-01T10:00:00Z")

    def test_check_passes_last_checked_date_as_cutoff(self):
        updates = {
            "TEST,_,100,1,5": {"timestamp": "2026-01-01T10:00:00Z", "user": "alice"},
            "TEST,_,100,1,6": {"timestamp": "2026-01-02T10:00:00Z", "user": "bob"},
        }
        manager = FakePageUpdateManager(updates)
        watcher = self._watcher()
        watcher._runtime = FakeRuntime(manager)

        with PATCH_ADDRESS:
            watcher.check()
            watcher.check()

        # second call must be bounded by what the first call advanced
        # last_checked_date to, not re-scan unbounded history (cutoff_date=None)
        self.assertEqual(manager.calls[0], ("TEST", "_", "2025-01-01T00:00:00Z"))
        self.assertEqual(manager.calls[1], ("TEST", "_", "2026-01-02T10:00:00Z"))

    def test_check_does_not_reflag_already_resolved_edit(self):
        # resolved["modified"] simulates a legacy date that passed through
        # to_utc_format() and had its seconds permanently zeroed; the "live"
        # update feed reports the same edit with real (non-zero) seconds
        # because get_updates is unbounded and keeps returning it.
        item = "TEST,_,100,1,5"
        watcher = self._watcher()
        watcher._resolved = {
            item: [{
                "modified": "2026-03-27T19:29:00Z",
                "last_resolved": "2026-03-29T13:42:00Z",
                "title": "case",
                "user": "someone",
            }],
        }
        watcher._last_checked_date = "2025-01-01T00:00:00Z"
        updates = {item: {"timestamp": "2026-03-27T19:29:30Z", "user": "someone"}}
        watcher._runtime = FakeRuntime(FakePageUpdateManager(updates))

        with PATCH_ADDRESS:
            watcher.check()

        self.assertNotIn(item, watcher.unresolved)

    def test_check_flags_genuine_new_edit_after_resolve(self):
        item = "TEST,_,100,1,5"
        watcher = self._watcher()
        watcher._resolved = {
            item: [{
                "modified": "2026-03-27T19:29:00Z",
                "last_resolved": "2026-03-29T13:42:00Z",
                "title": "case",
                "user": "someone",
            }],
        }
        watcher._last_checked_date = "2025-01-01T00:00:00Z"
        updates = {item: {"timestamp": "2026-04-01T10:00:00Z", "user": "someone-else"}}
        watcher._runtime = FakeRuntime(FakePageUpdateManager(updates))

        with PATCH_ADDRESS:
            watcher.check()

        self.assertIn(item, watcher.unresolved)
        entry = watcher.unresolved[item]
        self.assertEqual(entry["modified"], "2026-04-01T10:00:00Z")
        # last_resolved carried forward from the prior resolve action
        self.assertEqual(entry["last_resolved"], "2026-03-29T13:42:00Z")
        self.assertLess(entry["last_resolved"], entry["modified"])

    def test_check_ignores_nonconforming_long_addresses(self):
        watcher = self._watcher()
        watcher._runtime = FakeRuntime(FakePageUpdateManager({
            "TEST,_,1,2,3,4": {"timestamp": "2026-01-01T00:00:00Z", "user": ""},
        }))
        with mock.patch("birddog.runtime.page_address", return_value=("TEST", "_", "1", "2", "3", "4")):
            watcher.check()
        self.assertEqual(watcher.unresolved, {})


# ----------------------------------------------------------------------------
# ArchiveWatcher.save() / load() version migrations

class ArchiveWatcherSaveLoadTests(unittest.TestCase):
    def test_save_round_trip_is_v7(self):
        watcher = ArchiveWatcher("TEST", "_", cutoff_date="2025-01-01T00:00:00Z")
        watcher._unresolved = {"TEST,_,1,,": {"modified": "2026-01-01T00:00:00Z", "last_resolved": None, "title": "t"}}
        watcher._resolved = {"TEST,_,2,,": [{"modified": "2025-06-01T00:00:00Z", "last_resolved": "2025-06-02T00:00:00Z", "title": "u"}]}

        saved = watcher.save()
        self.assertEqual(saved["version"], "v7")

        reloaded = ArchiveWatcher.load(saved)
        self.assertEqual(reloaded.unresolved, watcher.unresolved)
        self.assertEqual(reloaded.resolved, watcher.resolved)

    def test_load_v1_normalizes_bare_resolved_dates_to_lists(self):
        # v1 stores are also pre-v5, so cutoff_date/resolved dates must be in
        # the legacy comma format the v5 migration (which runs right after)
        # expects -- to_utc_format() has no fallback for already-ISO input.
        # v1 is also pre-v4, so the forced-refresh check() fires too and
        # needs a (here, empty) live runtime to not crash.
        data = {
            "version": "v1",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025,01,01,00:00",
            "unresolved": {},
            "resolved": {"TEST,_,1,,": "2025,06,01,08:15"},
        }
        with PATCH_ADDRESS:
            watcher = ArchiveWatcher.load(data, runtime=FakeRuntime(FakePageUpdateManager({})))
        self.assertEqual(
            watcher.resolved["TEST,_,1,,"],
            [{"modified": to_utc_format("2025,06,01,08:15"), "last_resolved": to_utc_format("2025,01,01,00:00")}],
            )

    def test_load_v2_patches_missing_titles(self):
        # v2 is also pre-v4, so the forced-refresh check() wipes _unresolved
        # right after this patch runs -- only the _resolved side (untouched
        # by that wipe) is observable here.
        data = {
            "version": "v2",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025,01,01,00:00",
            "unresolved": {},
            "resolved": {"TEST,_,1,,": [{"modified": "2025,06,01,08:15", "last_resolved": "2025,06,02,00:00"}]},
        }
        with PATCH_TITLE, PATCH_ADDRESS:
            watcher = ArchiveWatcher.load(data, runtime=FakeRuntime(FakePageUpdateManager({})))
        self.assertEqual(watcher.resolved["TEST,_,1,,"][0]["title"], "TEST/_/1")

    def test_load_v5_migrates_legacy_comma_dates(self):
        # version="v4": below v5 (so the date migration fires) but not below
        # v4 (so the forced-refresh check(), which needs a live runtime,
        # doesn't) -- isolates the v5 migration on its own.
        data = {
            "version": "v4",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025,03,01,12:30",
            "unresolved": {"TEST,_,1,,": {"modified": "2025,06,01,08:15", "last_resolved": "2025,03,01,00:00", "title": "t"}},
            "resolved": {"TEST,_,2,,": [{"modified": "2025,05,01,09:00", "last_resolved": "2025,05,02,10:00", "title": "u"}]},
        }
        watcher = ArchiveWatcher.load(data)

        self.assertEqual(watcher.cutoff_date, to_utc_format("2025,03,01,12:30"))
        self.assertEqual(watcher.unresolved["TEST,_,1,,"]["modified"], to_utc_format("2025,06,01,08:15"))
        self.assertEqual(watcher.resolved["TEST,_,2,,"][0]["modified"], to_utc_format("2025,05,01,09:00"))
        # to_utc_format has no seconds field to preserve -- documents the
        # precision loss that makes the v6/v7 cleanups necessary
        self.assertTrue(watcher.cutoff_date.endswith(":00Z"))

    def test_load_v4_forces_refresh_via_check(self):
        data = {
            "version": "v3",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025,01,01,00:00",
            "unresolved": {"TEST,_,stale,,": {"modified": "2020,01,01,00:00", "last_resolved": None, "title": "stale"}},
            "resolved": {},
        }
        manager = FakePageUpdateManager({
            "TEST,_,1,,": {"timestamp": "2026-01-01T00:00:00Z", "user": ""},
        })
        with PATCH_ADDRESS:
            watcher = ArchiveWatcher.load(data, runtime=FakeRuntime(manager))

        # stale pre-migration entry discarded, replaced by a fresh check()
        self.assertNotIn("TEST,_,stale,,", watcher.unresolved)
        self.assertIn("TEST,_,1,,", watcher.unresolved)

    def test_load_v6_purges_matching_ghost_on_upgrade(self):
        item = "TEST,_,1,,"
        data = {
            "version": "v5",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {item: {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {item: [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        watcher = ArchiveWatcher.load(data)
        self.assertNotIn(item, watcher.unresolved)

    def test_load_v7_purges_ghost_left_by_already_v6_tagged_store(self):
        # Reproduces the real-world bug: a store already saved at "v6" (so
        # the v6 block above won't fire again) that accumulated a new ghost
        # entry afterward because check() kept re-flagging it. This is
        # exactly the shape found in the production AGAD/CDIAK watcher files.
        item = "TEST,_,1,,"
        data = {
            "version": "v6",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {item: {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {item: [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        watcher = ArchiveWatcher.load(data)
        self.assertNotIn(item, watcher.unresolved)

    def test_load_v7_preserves_genuinely_unresolved_items(self):
        # A real new edit landing on a different minute than the last
        # resolved copy must survive the v7 cleanup, not just anything with
        # resolved history.
        item = "TEST,_,1,,"
        data = {
            "version": "v6",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {item: {"modified": "2026-04-11T15:31:18Z", "last_resolved": "2026-04-12T08:00:00Z", "title": "t"}},
            "resolved": {item: [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-04-12T08:00:00Z", "title": "t"}]},
        }
        watcher = ArchiveWatcher.load(data)
        self.assertIn(item, watcher.unresolved)

    def test_load_already_v7_does_not_rerun_migrations(self):
        item = "TEST,_,1,,"
        data = {
            "version": "v7",
            "archive": "TEST", "subarchive": "_", "cutoff_date": "2025-01-01T00:00:00Z",
            "unresolved": {item: {"modified": "2026-03-27T19:29:30Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}},
            "resolved": {item: [{"modified": "2026-03-27T19:29:00Z", "last_resolved": "2026-03-29T13:42:00Z", "title": "t"}]},
        }
        watcher = ArchiveWatcher.load(data)
        # already at v7: migrations don't re-run, so a ghost sitting in an
        # already-v7 file (shouldn't happen going forward, but if it did)
        # is left alone rather than silently mutated on every load
        self.assertIn(item, watcher.unresolved)


if __name__ == "__main__":
    unittest.main()
