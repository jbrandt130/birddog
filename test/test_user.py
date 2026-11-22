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

    def resolve_watcher(email, archive_title, item, runtime=None, deep=False, now=None):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    def resolve_many(email, archive_title, items, runtime=None, now=None):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    def undo_resolve_many(email, archive_title, items, runtime=None):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    def unresolved_tree(unresolved):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    def remove_watcher(email, archive_title):
        raise NotImplementedError  # replaced by FakeWatcher in setUp()

    watcher_mod.check_watcher = check_watcher
    watcher_mod.resolve_watcher = resolve_watcher
    watcher_mod.resolve_many = resolve_many
    watcher_mod.undo_resolve_many = undo_resolve_many
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
        self.resolve_many_calls = []
        self.undo_calls = []
        self.removed = []
        self.check_result = {"example": {"count": 1}}
        self.resolve_result = {}
        self.resolve_raises = None
        self.undo_result = ({}, [], [])  # (unresolved, restored, stale)
        self.undo_raises = None
        # controls get_watcher(): True (default) means the watch still
        # exists after a resolve, as it does for an ordinary item resolve.
        # Tests simulate resolve_watcher() having retired the whole watch
        # (issue #136, step 3 -- the "moved" marker was just resolved) by
        # setting this False before calling User.resolve_item().
        self.watcher_exists = True

    def check_watcher(self, email, archive_title, runtime, include, cutoff_date, exclude=None):
        self.check_calls.append((email, archive_title, runtime, include, cutoff_date, exclude))
        return self.check_result

    def resolve_watcher(self, email, archive_title, item, runtime=None, deep=False, now=None):
        self.resolve_calls.append((email, archive_title, item, runtime, deep, now))
        if self.resolve_raises:
            raise self.resolve_raises
        return self.resolve_result

    def resolve_many(self, email, archive_title, items, runtime=None, now=None):
        self.resolve_many_calls.append((email, archive_title, list(items), runtime, now))
        if self.resolve_raises:
            raise self.resolve_raises
        return self.resolve_result

    def undo_resolve_many(self, email, archive_title, items, runtime=None):
        self.undo_calls.append((email, archive_title, list(items), runtime))
        if self.undo_raises:
            raise self.undo_raises
        return self.undo_result

    def get_watcher(self, email, archive_title):
        if not self.watcher_exists:
            raise KeyError(archive_title)
        return {"include": [archive_title]}

    def unresolved_tree(self, unresolved):
        return {"name": "root", "children": [{"name": k} for k in unresolved]}

    def remove_watcher(self, email, archive_title):
        self.removed.append((email, archive_title))


class FakeUsersDB:
    """The bit of the database API the Birddog Users code uses."""

    def __init__(self, rows=None, fail=None):
        self.rows = {r["email"]: dict(r, Id=i + 1) for i, r in enumerate(rows or [])}
        self.fail = fail          # exception to raise from every call
        self.calls = []

    def _check(self, op):
        self.calls.append(op)
        if self.fail:
            raise self.fail

    def lookup(self, table, key):
        self._check("lookup")
        assert table == "Birddog Users"
        row = self.rows.get(key)
        return row["Id"] if row else None

    def read(self, table, record_id, fields=None):
        self._check("read")
        row = next(r for r in self.rows.values() if r["Id"] == record_id)
        return {k: v for k, v in row.items() if fields is None or k in fields or k == "Id"}

    def write(self, table, records):
        self._check("write")
        for rec in records:
            self.rows[rec["email"]] = dict(rec, Id=len(self.rows) + 1)
        return [self.rows[r["email"]]["Id"] for r in records]

    def update(self, table, record):
        self._check("update")
        row = self.rows.get(record["email"])
        if row is None:
            return None
        row.update(record)
        return row["Id"]


class FakeRuntime:
    def __init__(self, database):
        self.database = database


class PrivilegesTest(unittest.TestCase):
    def setUp(self):
        self.cache = FakeCache()
        self._patches = [
            mock.patch.object(user_mod, "_kv_store", FakeKVStore()),
            mock.patch.object(user_mod, "save_cached_object", side_effect=self.cache.save),
        ]
        for p in self._patches:
            p.start()
        user_mod._global_user_locks.clear()
        user_mod._doc_privilege_cache.clear()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        user_mod._doc_privilege_cache.clear()

    def _user(self, db, email="cur@example.com", doc_privileges=None, site_admin=False):
        return User(name="Cur", email=email, password="pw", runtime=FakeRuntime(db),
                    site_admin=site_admin, doc_privileges=doc_privileges)

    def test_start_session_creates_row_with_no_privileges(self):
        db = FakeUsersDB()
        u = self._user(db)
        u.start_session()
        self.assertEqual(db.rows["cur@example.com"]["privileges"], [])
        self.assertEqual(db.rows["cur@example.com"]["name"], "Cur")
        self.assertEqual(u.privileges, frozenset())

    def test_start_session_keeps_existing_row(self):
        db = FakeUsersDB([{"email": "cur@example.com", "name": "Edited", "privileges": ["doc_review"]}])
        u = self._user(db)
        u.start_session()
        self.assertNotIn("write", db.calls)
        self.assertEqual(db.rows["cur@example.com"]["name"], "Edited")
        self.assertTrue(u.has_privilege("doc_review"))
        self.assertFalse(u.has_privilege("doc_admin"))

    def test_privileges_are_cached_then_reread(self):
        db = FakeUsersDB([{"email": "cur@example.com", "privileges": ["doc_check"]}])
        u = self._user(db)
        self.assertEqual(u.doc_privileges, frozenset({"doc_check"}))
        db.rows["cur@example.com"]["privileges"] = ["doc_admin"]
        reads = db.calls.count("read")
        self.assertEqual(self._user(db).doc_privileges, frozenset({"doc_check"}))   # still cached
        self.assertEqual(db.calls.count("read"), reads)
        u.start_session()                                                          # fresh cache: no db call
        self.assertEqual(db.calls.count("read"), reads)
        user_mod._doc_privilege_cache.clear()                                      # as if the TTL passed
        self.assertEqual(self._user(db).doc_privileges, frozenset({"doc_admin"}))

    def test_unknown_values_are_ignored(self):
        db = FakeUsersDB([{"email": "cur@example.com", "privileges": ["doc_check", "site_admin", "bogus"]}])
        self.assertEqual(self._user(db).privileges, frozenset({"doc_check"}))

    def test_table_privileges_are_copied_to_the_account_record(self):
        db = FakeUsersDB([{"email": "cur@example.com", "privileges": ["doc_escalate", "doc_check"]}])
        self._user(db).start_session()
        saved = self.cache._objects["users/cur@example.com.json"]
        self.assertEqual(saved["doc_privileges"], ["doc_check", "doc_escalate"])

    def test_copy_not_resaved_when_unchanged(self):
        db = FakeUsersDB([{"email": "cur@example.com", "privileges": ["doc_check"]}])
        self._user(db, doc_privileges=["doc_check"]).start_session()
        self.assertNotIn("users/cur@example.com.json", self.cache._objects)

    def test_falls_back_to_copy_without_a_database(self):
        u = self._user(None, doc_privileges=["doc_admin"])
        u.start_session()
        self.assertEqual(u.privileges, frozenset({"doc_admin"}))

    def test_falls_back_to_copy_when_unreachable_or_table_missing(self):
        for err in (user_mod.FailedIO("down"), user_mod.InvalidTableName("Birddog Users")):
            with self.subTest(err=type(err).__name__):
                user_mod._doc_privilege_cache.clear()
                u = self._user(FakeUsersDB(fail=err), doc_privileges=["doc_review"])
                u.start_session()   # must not raise
                self.assertEqual(u.privileges, frozenset({"doc_review"}))
                self.assertEqual(u.get_doc_preferences(), {})
                self.assertFalse(u.set_doc_preferences({"columns": []}))

    def test_emptied_privileges_deactivate(self):
        db = FakeUsersDB([{"email": "cur@example.com", "privileges": []}])
        u = self._user(db, doc_privileges=["doc_admin"])
        self.assertEqual(u.privileges, frozenset())
        self.assertEqual(self.cache._objects["users/cur@example.com.json"]["doc_privileges"], [])

    def test_site_admin_never_touches_the_database(self):
        db = FakeUsersDB(fail=user_mod.FailedIO("down"))
        self.assertTrue(self._user(db, site_admin=True).has_privilege("site_admin"))
        self.assertEqual(db.calls, [])

    def test_doc_preferences_round_trip(self):
        db = FakeUsersDB()
        u = self._user(db)
        self.assertEqual(u.get_doc_preferences(), {})
        prefs = {"columns": ["label", "last_updated"], "widths": {"label": 190}}
        self.assertTrue(u.set_doc_preferences(prefs))
        self.assertEqual(u.get_doc_preferences(), prefs)
        self.assertEqual(db.rows["cur@example.com"]["privileges"], [])   # row was created
        with self.assertRaises(ValueError):
            u.set_doc_preferences(["not", "a", "dict"])


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

        # a new watch clears any leftover watcher state for its title
        self.assertEqual(self.watcher.removed, [(u.email, title)])

        # remove should succeed even if no watcher state exists yet
        self.assertTrue(u.remove_from_watchlist(title))
        wl2 = u.get_watchlist()
        self.assertNotIn(title, wl2)
        self.assertEqual(self.watcher.removed, [(u.email, title), (u.email, title)])

    def test_user_add_to_watchlist_keeps_state_when_already_watched(self):
        u = User(name="Test", email="w3@example.com", password="pw")
        title = _watch_title("DACHKO", "D")
        u.add_to_watchlist(title, "2026,09,01,00:00")
        self.watcher.removed.clear()

        # re-adding an existing watch merges; its resolve history must survive
        u.add_to_watchlist(title, "2026,02,01,00:00")
        self.assertEqual(self.watcher.removed, [])

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
        unresolved_after, resolved_at = u.resolve_item(title, f"{title}/1/2/3", tree=False, deep=True)
        self.assertEqual(unresolved_after, [])
        self.assertRegex(resolved_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        email, watch_title, item_title, runtime, deep, now = self.watcher.resolve_calls[-1]
        self.assertEqual(email, u.email)
        self.assertEqual(watch_title, title)
        self.assertEqual(item_title, f"{title}/1/2/3")
        self.assertTrue(deep)
        # the same timestamp handed back to the caller is what got passed
        # down to resolve_watcher() -- issue #83 undo relies on both sides
        # agreeing on exactly this value
        self.assertEqual(now, resolved_at)

    def test_resolve_item_removes_watchlist_entry_when_watcher_is_retired(self):
        # issue #136, step 3: resolve_watcher() tears down the whole watch
        # when the "moved" marker gets resolved -- resolve_item() must then
        # also drop the matching wl: watchlist entry, or it lingers pointing
        # at a watch that no longer exists.
        u = User(name="Test", email="retired@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")
        self.assertIn(title, u.get_watchlist())

        self.watcher.resolve_result = {}
        self.watcher.watcher_exists = False  # simulate resolve_watcher() having just retired it

        u.resolve_item(title, title)  # resolving the marker (item == archive title)

        self.assertNotIn(title, u.get_watchlist())

    def test_resolve_item_keeps_watchlist_entry_when_watcher_still_exists(self):
        u = User(name="Test", email="stillhere@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        self.watcher.resolve_result = {f"{title}/other": {"modified": "x"}}
        # watcher_exists stays True (default) -- an ordinary resolve, the
        # watch survives

        u.resolve_item(title, f"{title}/1/2/3")

        self.assertIn(title, u.get_watchlist())

    def test_user_to_dict_from_dict_and_save(self):
        u = User(name="Test", email="s@example.com", password="pw")
        d = u.to_dict()
        self.assertEqual(d["name"], "Test")
        self.assertIn("password", d)
        self.assertIs(d["site_admin"], False)
        self.assertEqual(d["doc_privileges"], [])
        self.assertNotIn("role", d)

        u.save()
        self.assertIn(f"users/{u.email}.json", self.cache._objects)

        u2 = User.from_dict("s2@example.com", {"name": "Test2", "password": u._password_hash}, runtime=None)
        self.assertEqual(u2.name, "Test2")
        self.assertEqual(u2.email, "s2@example.com")
        self.assertFalse(u2.site_admin)

    def test_site_admin_round_trips(self):
        u = User(name="Test", email="admin@example.com", password="pw")
        self.assertFalse(u.has_privilege("site_admin"))
        u.set_site_admin(True)
        reloaded = User.from_dict("admin@example.com", u.to_dict(), runtime=None)
        self.assertTrue(reloaded.site_admin)
        self.assertTrue(reloaded.has_privilege("site_admin"))
        self.assertEqual(reloaded.privileges, frozenset({"site_admin"}))

    def test_legacy_role_maps_to_site_admin(self):
        # accounts saved before privileges carry a role instead
        admin = User.from_dict("old1@example.com", {"name": "A", "password": "h", "role": "admin"}, runtime=None)
        plain = User.from_dict("old2@example.com", {"name": "B", "password": "h", "role": "user"}, runtime=None)
        legacy = User.from_dict("old3@example.com", {"name": "C", "password": "h"}, runtime=None)
        self.assertTrue(admin.site_admin)
        self.assertFalse(plain.site_admin)
        self.assertFalse(legacy.site_admin)
        self.assertNotIn("role", admin.to_dict())
        self.assertTrue(admin.to_dict()["site_admin"])

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
        result, _ = u.resolve_item(title, f"{title}/1/2/3", tree=True)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["name"], "root")

    def test_user_resolve_many_calls_watcher_and_returns_timestamp(self):
        u = User(name="Test", email="rmany@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        self.watcher.resolve_result = {}
        result, resolved_at = u.resolve_many(title, [f"{title}/1", f"{title}/2"])
        self.assertEqual(result, [])
        self.assertRegex(resolved_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        email, watch_title, items, runtime, now = self.watcher.resolve_many_calls[-1]
        self.assertEqual(email, u.email)
        self.assertEqual(watch_title, title)
        self.assertEqual(items, [f"{title}/1", f"{title}/2"])
        self.assertEqual(now, resolved_at)

    def test_resolve_rechecks_watch_so_newer_edits_come_back(self):
        # issue #158: a resolve made from a long-open page acted on the last
        # check's entries; edits the tracker picked up since then only
        # surfaced on some later page load. Both resolve paths now re-check
        # and return that result.
        u = User(name="Test", email="recheck@example.com", password="pw", runtime=object())
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")
        self.watcher.resolve_result = {}
        self.watcher.check_result = {f"{title}/1": {"modified": "2026-01-01T00:00:00Z"}}

        result, _ = u.resolve_item(title, f"{title}/1")
        self.assertEqual([r["name"] for r in result], [f"{title}/1"])
        self.assertEqual(len(self.watcher.check_calls), 1)

        result, _ = u.resolve_many(title, [f"{title}/1"])
        self.assertEqual([r["name"] for r in result], [f"{title}/1"])
        self.assertEqual(len(self.watcher.check_calls), 2)

    def test_resolve_skips_recheck_when_watch_retired(self):
        u = User(name="Test", email="recheck2@example.com", password="pw", runtime=object())
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")
        self.watcher.resolve_result = {}
        self.watcher.watcher_exists = False

        result, _ = u.resolve_item(title, title)
        self.assertEqual(result, [])
        self.assertEqual(self.watcher.check_calls, [])

    def test_user_undo_resolve_calls_watcher_and_formats_result(self):
        # issue #83
        u = User(name="Test", email="undo@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        self.watcher.undo_result = ({f"{title}/1": {"modified": "x"}}, [f"{title}/1"], [f"{title}/2"])
        result, restored, stale = u.undo_resolve(title, [(f"{title}/1", "2026-01-01T00:00:00Z"), (f"{title}/2", "2026-01-01T00:00:00Z")])

        self.assertEqual(result, [{"name": f"{title}/1", "modified": "x"}])
        self.assertEqual(restored, [f"{title}/1"])
        self.assertEqual(stale, [f"{title}/2"])

        email, watch_title, items, runtime = self.watcher.undo_calls[-1]
        self.assertEqual(email, u.email)
        self.assertEqual(watch_title, title)
        self.assertEqual(items, [(f"{title}/1", "2026-01-01T00:00:00Z"), (f"{title}/2", "2026-01-01T00:00:00Z")])

    def test_user_undo_resolve_tree_returns_dict(self):
        u = User(name="Test", email="undotree@example.com", password="pw")
        title = _watch_title("DAARK", "D")
        u.add_to_watchlist(title, "2020,01,01,00:00")

        self.watcher.undo_result = ({"x": 1}, ["x"], [])
        result, restored, stale = u.undo_resolve(title, [("x", "2026-01-01T00:00:00Z")], tree=True)
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
