import sys
import types
import unittest
from unittest import mock

# ---------------------------------------------------------------------
# Import resilience
#
# In the Birddog repo, this should import cleanly as birddog.user.
# For isolated execution (running this file alone), we provide minimal
# stubs for birddog.* dependencies so that user.py can be imported, and
# then we patch those dependencies in setUp().
# ---------------------------------------------------------------------

def _ensure_birddog_stubs():
    if "birddog" in sys.modules:
        return  # real package present

    birddog_pkg = types.ModuleType("birddog")
    birddog_pkg.__path__ = []  # mark as package
    sys.modules["birddog"] = birddog_pkg

    # birddog.log
    log_mod = types.ModuleType("birddog.log")

    class _StubLogger:
        def info(self, *args, **kwargs):
            pass

    def get_logger():
        return _StubLogger()

    log_mod.get_logger = get_logger
    sys.modules["birddog.log"] = log_mod

    # birddog.store
    store_mod = types.ModuleType("birddog.store")

    class _BootstrapKV:
        def __init__(self):
            self._data = {}

        def insert(self, namespace, key, value):
            self._data[(namespace, key)] = value

        def get(self, namespace, key):
            return self._data[(namespace, key)]

        def get_all(self, namespace):
            return [(k, v) for (ns, k), v in self._data.items() if ns == namespace]

        def remove(self, namespace, key):
            del self._data[(namespace, key)]

    store_mod.KeyValueStore = _BootstrapKV
    sys.modules["birddog.store"] = store_mod

    # birddog.cache
    cache_mod = types.ModuleType("birddog.cache")

    class CacheMissError(Exception):
        pass

    def load_cached_object(path):
        raise CacheMissError()

    def save_cached_object(obj, path):
        return None

    def remove_cached_object(path):
        raise CacheMissError()

    cache_mod.CacheMissError = CacheMissError
    cache_mod.load_cached_object = load_cached_object
    cache_mod.save_cached_object = save_cached_object
    cache_mod.remove_cached_object = remove_cached_object
    sys.modules["birddog.cache"] = cache_mod

    # birddog.watcher
    watcher_mod = types.ModuleType("birddog.watcher")

    def check_watcher(email, archive_title, runtime, include, cutoff_date, exclude=None):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    def resolve_watcher(email, archive_title, item, runtime=None, deep=False):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    def unresolved_tree(unresolved):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    def remove_watcher(email, archive_title):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    watcher_mod.check_watcher = check_watcher
    watcher_mod.resolve_watcher = resolve_watcher
    watcher_mod.unresolved_tree = unresolved_tree
    watcher_mod.remove_watcher = remove_watcher
    sys.modules["birddog.watcher"] = watcher_mod
    birddog_pkg.watcher = watcher_mod  # `from birddog import watcher` needs this bound on the package too

    # birddog.wiki
    wiki_mod = types.ModuleType("birddog.wiki")

    def archive_root(archive, subarchive):
        return f"Archive:{archive}"

    def canonicalize_title(title, include_namespace=True):
        if not title:
            return None
        if include_namespace and not title.startswith("Archive:"):
            return f"Archive:{title}"
        return title

    wiki_mod.archive_root = archive_root
    wiki_mod.canonicalize_title = canonicalize_title
    sys.modules["birddog.wiki"] = wiki_mod


_ensure_birddog_stubs()

# Module under test
from birddog import user as user_mod  # type: ignore

# Re-export the functions/classes under test for readability
_get_watchlist = user_mod._get_watchlist
_new_watch_item = user_mod._new_watch_item
_save_watch_item = user_mod._save_watch_item
_watch_title = user_mod._watch_title
_load_watchlist = user_mod._load_watchlist

_set_preference = user_mod._set_preference
_get_preference = user_mod._get_preference
_load_preferences = user_mod._load_preferences

User = user_mod.User


class FakeKVStore:
    """Minimal in-memory KV store matching the interface used by user.py."""
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
        try:
            del self._data[(namespace, key)]
        except KeyError as e:
            raise KeyError(f"Missing key: {namespace}/{key}") from e


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


class FakeWatcher:
    """
    Minimal stand-in for birddog.watcher, to exercise
    User.check_watchlist_item / resolve_item / remove_from_watchlist paths
    without touching real KV/cache storage. Records the arguments each
    method was called with, so tests can assert User correctly orchestrates
    calls into birddog.watcher (real watcher.py behavior -- check/resolve
    semantics, legacy migration -- has its own dedicated coverage in
    test_watcher.py).
    """
    def __init__(self):
        self.check_calls = []
        self.resolve_calls = []
        self.removed = []
        self.check_result = {"example": {"count": 1}}
        self.resolve_result = {}
        self.resolve_raises = None

    def check_watcher(self, email, archive_title, runtime, include, cutoff_date, exclude=None):
        self.check_calls.append((email, archive_title, runtime, include, cutoff_date, exclude))
        return self.check_result

    def resolve_watcher(self, email, archive_title, item, runtime=None, deep=False):
        self.resolve_calls.append((email, archive_title, item, runtime, deep))
        if self.resolve_raises:
            raise self.resolve_raises
        return self.resolve_result

    def unresolved_tree(self, unresolved):
        return {"name": "root", "children": [{"name": k} for k in unresolved]}

    def remove_watcher(self, email, archive_title):
        self.removed.append((email, archive_title))


class UserTest(unittest.TestCase):
    def setUp(self):
        self.kv = FakeKVStore()
        self.cache = FakeCache()
        self.watcher = FakeWatcher()

        self._patches = [
            mock.patch.object(user_mod, "_kv_store", self.kv),
            mock.patch.object(user_mod, "save_cached_object", side_effect=self.cache.save),
            mock.patch.object(user_mod, "watcher", self.watcher),
        ]
        for p in self._patches:
            p.start()

        # avoid lock leakage across tests
        user_mod._global_user_locks.clear()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    # ------------------ WATCHLIST MANAGEMENT ------------------

    def test_watchlist_crud_and_load(self):
        email = "test@example.com"
        title_a = _watch_title("DAARK", "D")

        _save_watch_item(email, title_a, _new_watch_item("2020,01,01,00:00", include=[title_a]))
        wl = _get_watchlist(email)
        self.assertIn(title_a, wl)
        self.assertEqual(wl[title_a]["cutoff_date"], "2020-01-01T00:00:00Z")
        self.assertNotIn("last_checked_date", wl[title_a])

        _save_watch_item(
            email, title_a,
            _new_watch_item("2020,01,01,00:00", last_checked_date="2022,03,04,05:06", include=[title_a]))
        wl2 = _get_watchlist(email)
        self.assertEqual(wl2[title_a]["last_checked_date"], "2022-03-04T05:06:00Z")

    def test_get_watchlist_empty_for_unknown_user(self):
        self.assertEqual(_get_watchlist("nobody@example.com"), {})

    def test_legacy_watchlist_item_coarsens_to_archive_title_on_read(self):
        email = "legacy@example.com"
        title = _watch_title("DAARK", "D")

        # simulate a pre-migration raw KV entry: "archive-subarchive" key,
        # no "include" field
        self.kv.insert(user_mod._watchlist_namespace(email), "DAARK-D", '{"cutoff_date": "2019,01,01,00:00"}')

        wl = _get_watchlist(email)
        self.assertIn(title, wl)
        self.assertEqual(wl[title]["cutoff_date"], "2019-01-01T00:00:00Z")
        self.assertEqual(wl[title]["include"], [title])

        # legacy key must be gone, replaced by the title-keyed entry
        self.assertNotIn(("wl:" + email, "DAARK-D"), self.kv._data)

    def test_legacy_sibling_subarchive_entries_merge_on_read(self):
        email = "legacy2@example.com"
        title = _watch_title("DACHGO", "D")
        self.assertEqual(title, _watch_title("DACHGO", "R"))  # same owning archive

        ns = user_mod._watchlist_namespace(email)
        self.kv.insert(ns, "DACHGO-D", '{"cutoff_date": "2021,06,01,00:00"}')
        self.kv.insert(ns, "DACHGO-R", '{"cutoff_date": "2020,01,01,00:00", "last_checked_date": "2020,02,01,00:00"}')

        wl = _get_watchlist(email)
        self.assertEqual(len(wl), 1)
        self.assertIn(title, wl)
        # oldest cutoff_date/last_checked_date of the merged entries wins
        self.assertEqual(wl[title]["cutoff_date"], "2020-01-01T00:00:00Z")
        self.assertEqual(wl[title]["last_checked_date"], "2020-02-01T00:00:00Z")

        self.assertNotIn((ns, "DACHGO-D"), self.kv._data)
        self.assertNotIn((ns, "DACHGO-R"), self.kv._data)

    # ------------------ PREFERENCES MANAGEMENT ------------------

    def test_preferences_set_get_default_and_load(self):
        email = "prefs@example.com"

        _set_preference(email, "theme", "dark")
        self.assertEqual(_get_preference(email, "theme"), "dark")

        _set_preference(email, "page_size", 50)
        self.assertEqual(_get_preference(email, "page_size"), 50)

        self.assertEqual(_get_preference(email, "missing", default_value="fallback"), "fallback")

        _load_preferences(email, {"a": 1, "b": {"x": True}})
        self.assertEqual(_get_preference(email, "a"), 1)
        self.assertEqual(_get_preference(email, "b"), {"x": True})

    # ------------------ USER ------------------

    def test_user_password_check_change_and_set(self):
        u = User(name="Test", email="u@example.com", password="pw1")
        self.assertTrue(u.check_password("pw1"))
        self.assertFalse(u.check_password("wrong"))

        self.assertFalse(u.change_password("wrong", "pw2"))
        self.assertTrue(u.check_password("pw1"))

        self.assertTrue(u.change_password("pw1", "pw2"))
        self.assertTrue(u.check_password("pw2"))

        u.set_password("pw3")
        self.assertTrue(u.check_password("pw3"))

    def test_user_watchlist_methods_add_get_remove(self):
        u = User(name="Test", email="w@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        wl = u.get_watchlist()
        self.assertIn(title, wl)
        self.assertEqual(wl[title]["cutoff_date"], "2020-01-01T00:00:00Z")
        self.assertEqual(wl[title]["include"], [title])

        # remove should succeed even if no watcher state exists yet
        self.assertTrue(u.remove_from_watchlist(title))
        wl2 = u.get_watchlist()
        self.assertNotIn(title, wl2)
        self.assertEqual(self.watcher.removed, [(u.email, title)])

    def test_user_add_to_watchlist_merges_when_already_watched(self):
        u = User(name="Test", email="w2@example.com", password="pw")
        title = _watch_title("DACHGO", "D")

        u.add_to_watchlist(title, "2021,06,01,00:00")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        wl = u.get_watchlist()
        self.assertEqual(len(wl), 1)
        # oldest cutoff_date wins on merge
        self.assertEqual(wl[title]["cutoff_date"], "2020-01-01T00:00:00Z")

    def test_user_check_watchlist_item_calls_watcher_and_updates_last_checked(self):
        u = User(name="Test", email="c@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        result = u.check_watchlist_item(title, tree=False)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["name"], "example")

        email, watch_title, runtime, include, cutoff_date, exclude = self.watcher.check_calls[-1]
        self.assertEqual(email, u.email)
        self.assertEqual(watch_title, title)
        self.assertEqual(include, [title])
        self.assertEqual(cutoff_date, "2020-01-01T00:00:00Z")

        item = _get_watchlist(u.email)[title]
        self.assertIn("last_checked_date", item)
        self.assertRegex(item["last_checked_date"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        result2 = u.check_watchlist_item(title, tree=True)
        self.assertIsInstance(result2, dict)
        self.assertEqual(result2["name"], "root")

    def test_user_resolve_item_raises_when_no_watcher(self):
        u = User(name="Test", email="r@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        self.watcher.resolve_raises = FileNotFoundError('No watcher found')
        with self.assertRaises(FileNotFoundError):
            u.resolve_item(title, f"{title}/1/2/3")

    def test_user_resolve_item_calls_watcher_and_formats_result(self):
        u = User(name="Test", email="r2@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        self.watcher.resolve_result = {}
        unresolved_after = u.resolve_item(title, f"{title}/1/2/3", tree=False, deep=True)
        self.assertEqual(unresolved_after, [])

        email, watch_title, item_title, runtime, deep = self.watcher.resolve_calls[-1]
        self.assertEqual(email, u.email)
        self.assertEqual(watch_title, title)
        self.assertEqual(item_title, f"{title}/1/2/3")
        self.assertTrue(deep)

    def test_user_to_dict_from_dict_and_save(self):
        u = User(name="Test", email="s@example.com", password="pw")
        d = u.to_dict()
        self.assertEqual(d["name"], "Test")
        self.assertIn("password", d)
        self.assertEqual(d["role"], "user")

        u.save()
        self.assertIn(f"users/{u.email}.json", self.cache._objects)

        u2 = User.from_dict("s2@example.com", {"name": "Test2", "password": u._password_hash}, runtime=None)
        self.assertEqual(u2.name, "Test2")
        self.assertEqual(u2.email, "s2@example.com")
        self.assertEqual(u2.role, "user")

    def test_user_role_defaults_and_set_role_round_trips(self):
        u = User(name="Test", email="role@example.com", password="pw")
        self.assertEqual(u.role, "user")

        u.set_role("admin")
        self.assertEqual(u.role, "admin")

        reloaded = User.from_dict("role@example.com", u.to_dict(), runtime=None)
        self.assertEqual(reloaded.role, "admin")

    def test_user_from_dict_defaults_missing_role_to_user(self):
        # pre-role accounts have no "role" key in their persisted dict
        u = User.from_dict("legacy@example.com", {"name": "Legacy", "password": "hash"}, runtime=None)
        self.assertEqual(u.role, "user")

    def test_check_watchlist_item_raises_if_not_in_watchlist(self):
        u = User(name="Test", email="nowatch@example.com", password="pw")
        with self.assertRaises(KeyError):
            u.check_watchlist_item("Archive:NOPE")

    def test_user_set_and_get_preference(self):
        u = User(name="Test", email="prefuser@example.com", password="pw")
        u.set_preference("theme", "dark")
        self.assertEqual(u.get_preference("theme"), "dark")
        self.assertIsNone(u.get_preference("missing"))
        self.assertEqual(u.get_preference("missing", default_value="fallback"), "fallback")

    def test_resolve_item_tree_returns_dict(self):
        u = User(name="Test", email="rtree@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")
        self.watcher.resolve_result = {"x": 1}
        result = u.resolve_item(title, f"{title}/1/2/3", tree=True)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["name"], "root")

    def test_check_watchlist_item_preserves_cutoff_date_while_updating_last_checked(self):
        u = User(name="Test", email="upd@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,06,01")

        u.check_watchlist_item(title)

        item = _get_watchlist(u.email)[title]
        self.assertEqual(item["cutoff_date"], "2020-06-01T00:00:00Z",
            "check_watchlist_item must not overwrite cutoff_date when updating last_checked_date")
        self.assertRegex(item["last_checked_date"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_user_migrates_legacy_watchlist_and_preferences_on_init(self):
        email = "migrate@example.com"
        title = _watch_title("DAARK", "D")
        legacy_watchlist = {"DAARK-D": {"cutoff_date": "2010,01,01,00:00"}}
        legacy_prefs = {"x": 123}

        u = User(name="Test", email=email, password="pw", watchlist=legacy_watchlist, preferences=legacy_prefs)
        self.assertIn(title, u.get_watchlist())
        self.assertEqual(_get_preference(email, "x"), 123)
        self.assertIn(f"users/{email}.json", self.cache._objects)


if __name__ == "__main__":
    unittest.main()
