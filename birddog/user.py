# user management

import json
import time
from threading import RLock, Lock

from werkzeug.security import generate_password_hash, check_password_hash

from birddog import watcher
from birddog.wiki import archive_root, canonicalize_title
from birddog.cache import save_cached_object
from birddog.store import KeyValueStore
from birddog.utility import utc_now_dt, to_utc_format
from birddog.abstract_database import FailedIO, InvalidTableName, SchemaError

from birddog.log import get_logger
_logger = get_logger()

_kv_store = KeyValueStore()

# ---------------------------------------------------------------------
# WATCHLIST MANAGEMENT

def _watch_title(archive, subarchive):
    # watchlist entries are coarsened to the whole owning archive -- there's
    # no title-prefix that isolates a single subarchive, since its fonds
    # interleave with other subarchives' fonds under the same literal title
    return archive_root(archive, subarchive)

def _watchlist_namespace(email):
    return f"wl:{email}"

# reserved key marking that the one-time legacy-format migration sweep (see
# _get_watchlist below) has already run for this user, so single-item call
# sites can skip straight to a single-key KV lookup instead of paying for a
# full-namespace scan on every add/remove/check
_MIGRATION_MARKER_KEY = "__migrated__"

def _is_watchlist_migrated(email):
    try:
        _kv_store.get(_watchlist_namespace(email), _MIGRATION_MARKER_KEY)
        return True
    except KeyError:
        return False

def _mark_watchlist_migrated(email):
    _kv_store.insert(_watchlist_namespace(email), _MIGRATION_MARKER_KEY, "1")

def _to_utc(value):
    # normalize a legacy "YYYY,MM,DD,HH:MM" date to UTC ISO8601; already-ISO
    # (or falsy) values pass through unchanged
    return to_utc_format(value) if value and "," in value else value

def _new_watch_item(cutoff_date, last_checked_date=None, include=None, exclude=None):
    item = {"cutoff_date": _to_utc(cutoff_date)}
    if last_checked_date:
        item["last_checked_date"] = _to_utc(last_checked_date)
    item["include"] = include or []
    item["exclude"] = exclude or []
    return item

def _older(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)

def _merge_watch_items(a, b):
    # multiple legacy (archive, subarchive) entries can coarsen to the same
    # archive-level title; take the older cutoff/last_checked of the two so
    # nothing pending on either original entry is silently dropped
    return _new_watch_item(
        _older(a.get("cutoff_date"), b.get("cutoff_date")),
        _older(a.get("last_checked_date"), b.get("last_checked_date")),
        include=a.get("include") or b.get("include"),
        exclude=a.get("exclude") or b.get("exclude"),
    )

def _parse_watch_item(raw):
    # normalize any legacy comma-format dates to UTC ISO8601
    item = json.loads(raw)
    item["cutoff_date"] = _to_utc(item.get("cutoff_date"))
    if item.get("last_checked_date"):
        item["last_checked_date"] = _to_utc(item["last_checked_date"])
    return item

def _save_watch_item(email, title, item):
    _kv_store.insert(_watchlist_namespace(email), title, json.dumps(item))

def _get_watchlist(email):
    # lazily upgrade legacy "archive-subarchive"-keyed entries (no "include"
    # field) to title-keyed entries, coarsened to the owning archive and
    # merged with any sibling subarchive entries (or a pre-existing
    # title-keyed entry) for the same archive
    ns = _watchlist_namespace(email)
    raw = {
        k: _parse_watch_item(v)
        for k, v in _kv_store.get_all(ns)
        if k != _MIGRATION_MARKER_KEY
    }

    result = {}
    legacy_keys = []
    changed_titles = set()
    for key, item in raw.items():
        if "include" in item:
            title = key
        else:
            archive, subarchive = key.split("-", 1)
            title = _watch_title(archive, subarchive)
            item = _new_watch_item(item.get("cutoff_date"), item.get("last_checked_date"), include=[title])
            legacy_keys.append(key)
            changed_titles.add(title)

        if title in result:
            result[title] = _merge_watch_items(result[title], item)
            changed_titles.add(title)
        else:
            result[title] = item

    for key in legacy_keys:
        _kv_store.remove(ns, key)
    for title in changed_titles:
        _save_watch_item(email, title, result[title])

    _mark_watchlist_migrated(email)
    return result

def _get_watch_item(email, title):
    # single-key fast path once this user's namespace is known to be free of
    # legacy-format entries; falls back to the full migration sweep exactly
    # once per account (see _get_watchlist), never again after that
    if not _is_watchlist_migrated(email):
        return _get_watchlist(email)[title]
    return _parse_watch_item(_kv_store.get(_watchlist_namespace(email), title))

def _write_legacy_watch_item(email, key, cutoff, last_checked=None):
    # writes the pre-title-migration shape; swept up by the lazy upgrade in
    # _get_watchlist() on first read, same as any other legacy KV entry
    payload = {"cutoff_date": _to_utc(cutoff)}
    if last_checked:
        payload["last_checked_date"] = _to_utc(last_checked)
    _kv_store.insert(_watchlist_namespace(email), key, json.dumps(payload))

def _load_watchlist(email, watchlist):
    for key, item in watchlist.items():
        _write_legacy_watch_item(
            email,
            key,
            item["cutoff_date"],
            last_checked=item.get("last_checked_date"))

# ---------------------------------------------------------------------
# PREFERENCES MANAGEMENT

def _preferences_namespace(email):
    return f"pref:{email}"

def _set_preference(email, key, value):
    _kv_store.insert(_preferences_namespace(email), key, json.dumps(value))

def _get_preference(email, key, default_value=None):
    try:
        return json.loads(_kv_store.get(_preferences_namespace(email), key))
    except KeyError:
        return default_value

def _load_preferences(email, preferences):
    for key, value in preferences.items():
        _set_preference(email, key, value)

# ---------------------------------------------------------------------
# PRIVILEGES
#
# site_admin is stored in the account record (S3), so site operations keep
# working without the database. The doc_* curation privileges and the
# Documents tab settings live in the NocoDB "Birddog Users" table, one row per
# account keyed by email, managed by hand in the NocoDB grid. An empty
# privileges list means no curation access.
#
# The last privileges read from the table are copied into the account record,
# so they still apply while the database is absent or unreachable. S3 is
# already split by environment (STAGE keeps its objects under "stage/").

SITE_ADMIN = "site_admin"
DOC_PRIVILEGES = ("doc_check", "doc_review", "doc_escalate", "doc_admin")
PRIVILEGES = (SITE_ADMIN,) + DOC_PRIVILEGES

_USERS_TABLE = "Birddog Users"
_USERS_FIELDS = ["email", "name", "privileges", "doc_preferences"]
_PRIVILEGE_TTL_S = 60

# email -> (expires_at, frozenset of doc privileges); User objects are
# rebuilt on every request, so the cache lives at module level
_doc_privilege_cache = {}
_doc_privilege_cache_lock = Lock()

def _cached_doc_privileges(email):
    with _doc_privilege_cache_lock:
        entry = _doc_privilege_cache.get(email)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    return None

def _cache_doc_privileges(email, privileges):
    with _doc_privilege_cache_lock:
        _doc_privilege_cache[email] = (time.monotonic() + _PRIVILEGE_TTL_S, privileges)

def _clean_doc_privileges(values):
    return frozenset(v for v in (values or []) if v in DOC_PRIVILEGES)

# ---------------------------------------------------------------------
# USER MANAGEMENT

_global_lock = Lock()
_global_user_locks = {}

class User:
    def __init__(self, name, email, password, runtime=None, watchlist=None, preferences=None,
                 is_hashed=False, site_admin=False, doc_privileges=None):
        self._name = name
        self._email = email
        self._password_hash = password if is_hashed else generate_password_hash(password)
        self._runtime = runtime
        self._site_admin = bool(site_admin)
        # last doc privileges read from the Birddog Users table
        self._doc_privileges_copy = _clean_doc_privileges(doc_privileges)

        with _global_lock:
            self._lock = _global_user_locks.get(email, RLock())
            _global_user_locks[email] = self._lock

        # one-time transfer of watchlist to kv store
        with self._lock:
            if isinstance(watchlist, dict):
                _load_watchlist(email, watchlist)
                self.save()

            # one-time transfer of preferences to kv store
            if isinstance(preferences, dict):
                _load_preferences(email, preferences)
                self.save()

    @property
    def name(self):
        return self._name

    @property
    def email(self):
        return self._email

    @property
    def site_admin(self):
        return self._site_admin

    def set_site_admin(self, value):
        with self._lock:
            self._site_admin = bool(value)
            self.save()

    # ---- privileges -----------------------------------------------------

    def _database(self):
        return getattr(self._runtime, "database", None) if self._runtime else None

    def _read_users_row(self, db):
        """The user's Birddog Users row, or None. Raises InvalidTableName or
        SchemaError when the table isn't set up, FailedIO when unreachable."""
        record_id = db.lookup(_USERS_TABLE, self._email)
        if record_id is None:
            return None
        return db.read(_USERS_TABLE, record_id, fields=_USERS_FIELDS)

    def _with_users_row(self, action, create=False):
        """Run action(db, row) against the user's row and return its result,
        or return None when the table is absent or unreachable. With create,
        a missing row is first created with no privileges."""
        db = self._database()
        if db is None:
            return None
        try:
            row = self._read_users_row(db)
            if row is None and create:
                db.write(_USERS_TABLE, [{"email": self._email, "name": self._name, "privileges": []}])
                _logger.info(f"Birddog Users: created a row for {self._email}")
                row = self._read_users_row(db)
            return action(db, row)
        except (InvalidTableName, SchemaError):
            return None   # table not set up on this base yet
        except FailedIO as err:
            _logger.warning(f"Birddog Users: unreachable for {self._email}: {err}")
            return None

    def _refresh_doc_privileges(self, create=False):
        privileges = self._with_users_row(
            lambda db, row: _clean_doc_privileges(row.get("privileges")) if row else frozenset(),
            create=create)
        if privileges is None:
            return self._doc_privileges_copy
        _cache_doc_privileges(self._email, privileges)
        if privileges != self._doc_privileges_copy:
            with self._lock:
                self._doc_privileges_copy = privileges
                self.save()
        return privileges

    def start_session(self):
        """Call when a session begins (login or page load): makes sure the
        user has a Birddog Users row and refreshes the cached privileges.
        Never raises for database problems, so sign-in can't fail on them.
        Skipped while the cached privileges are fresh: they are only cached
        after the row was read or created."""
        if _cached_doc_privileges(self._email) is None:
            self._refresh_doc_privileges(create=True)

    @property
    def doc_privileges(self):
        cached = _cached_doc_privileges(self._email)
        return cached if cached is not None else self._refresh_doc_privileges()

    @property
    def privileges(self):
        return self.doc_privileges | ({SITE_ADMIN} if self._site_admin else frozenset())

    def has_privilege(self, privilege):
        if privilege == SITE_ADMIN:
            return self._site_admin   # never needs the database
        return privilege in self.doc_privileges

    def get_doc_preferences(self):
        """Documents tab settings (a dict), or {} when there are none or the
        table is unavailable."""
        prefs = self._with_users_row(lambda db, row: row.get("doc_preferences") if row else None)
        return prefs if isinstance(prefs, dict) else {}

    def set_doc_preferences(self, prefs):
        """Saves the Documents tab settings. Returns False when the table is
        unavailable."""
        if not isinstance(prefs, dict):
            raise ValueError("doc preferences must be a dict")
        def _write(db, row):
            return db.update(_USERS_TABLE, {"email": self._email, "doc_preferences": prefs}) is not None
        return bool(self._with_users_row(_write, create=True))

    def check_password(self, password):
        return check_password_hash(self._password_hash, password)

    def change_password(self, current_password, new_password):
        with self._lock:
            if not self.check_password(current_password):
                return False
            self._password_hash = generate_password_hash(new_password)
            self.save()
            return True

    def set_password(self, new_password):
        with self._lock:
            self._password_hash = generate_password_hash(new_password)
            self.save()

    def get_watchlist(self):
        with self._lock:
            return _get_watchlist(self._email)

    def add_to_watchlist(self, title, cutoff_date):
        title = canonicalize_title(title)
        with self._lock:
            item = _new_watch_item(cutoff_date, include=[title])
            try:
                existing = _get_watch_item(self.email, title)
            except KeyError:
                existing = None
            if existing:
                item = _merge_watch_items(existing, item)
            _save_watch_item(self.email, title, item)

    def remove_from_watchlist(self, title):
        title = canonicalize_title(title)
        with self._lock:
            if not _is_watchlist_migrated(self.email):
                _get_watchlist(self.email)  # fold in any un-migrated sibling entries first
            _kv_store.remove(_watchlist_namespace(self.email), title)

        # Remove associated watcher state (outside lock)
        watcher.remove_watcher(self.email, title)
        return True

    def _check_watchlist_item_locked(self, title):
        # caller holds self._lock; title is already canonical
        watchlist_item = _get_watch_item(self.email, title)  # raises KeyError if not watched
        unresolved = watcher.check_watcher(
            self.email, title, self._runtime,
            include=watchlist_item['include'],
            cutoff_date=watchlist_item['cutoff_date'],
            exclude=watchlist_item.get('exclude'))
        watchlist_item["last_checked_date"] = _to_utc(utc_now_dt().strftime('%Y-%m-%dT%H:%M:%SZ'))
        _save_watch_item(self.email, title, watchlist_item)
        return unresolved

    def _recheck_after_resolve(self, title, unresolved):
        # issue #158: a resolve acts on the unresolved entries the *last*
        # check stored, and a watchlist page left open for hours never
        # re-checks on its own -- so edits the tracker picked up since then
        # (e.g. a newer edit to the very item just resolved, or new child
        # pages under it) stayed invisible until some later page load, where
        # they surfaced looking like the resolve had been lost. Re-checking
        # here hands them back in the resolve response itself.
        if self._runtime is None:
            return unresolved
        try:
            return self._check_watchlist_item_locked(title)
        except KeyError:
            return unresolved  # watch (or its wl: entry) already gone

    def check_watchlist_item(self, title, tree=False):
        title = canonicalize_title(title)
        with self._lock:
            unresolved = self._check_watchlist_item_locked(title)

        if tree:
            return watcher.unresolved_tree(unresolved)
        return [{'name': k, **v} for k, v in unresolved.items()]

    def resolve_item(self, title, item_title, tree=False, deep=False):
        # title identifies which watchlist entry/watcher; item_title is the
        # specific unresolved item within it being resolved -- distinct
        # because a watcher's scope (e.g. a whole archive) is coarser than
        # any one item inside it
        title = canonicalize_title(title)
        # issue #83 undo: generated here (rather than inside
        # watcher.resolve_watcher()) so it can be handed back to the caller
        # alongside the result -- the client stashes it and echoes it back
        # to /resolve/undo to identify exactly this action later
        now = utc_now_dt().strftime('%Y-%m-%dT%H:%M:%SZ')
        with self._lock:
            _logger.info(f'Resolving {item_title}, deep={deep}, tree={tree}')
            unresolved = watcher.resolve_watcher(
                self.email, title, item_title, runtime=self._runtime, deep=deep, now=now)
            try:
                watcher.get_watcher(self.email, title)
            except KeyError:
                # resolve_watcher() just retired this watch outright: its
                # own scope moved and the "moved" marker was the item just
                # resolved (issue #136). Drop the matching wl: watchlist
                # entry the same way remove_from_watchlist() does, or it'd
                # linger pointing at a watch that no longer exists.
                if not _is_watchlist_migrated(self.email):
                    _get_watchlist(self.email)  # fold in any un-migrated sibling entries first
                _kv_store.remove(_watchlist_namespace(self.email), title)
            else:
                unresolved = self._recheck_after_resolve(title, unresolved)

        if tree:
            result = watcher.unresolved_tree(unresolved)
        else:
            result = [{'name': k, **v} for k, v in unresolved.items()]
        return result, now

    def resolve_many(self, title, item_titles, tree=False):
        # issue #138 Alerts-table bulk resolve: item_titles is an explicit,
        # already-expanded list (checkbox/range/subtree selection), all under
        # this one watch -- see /resolve/batch, which groups a cross-watch
        # selection by title before calling this once per watch
        title = canonicalize_title(title)
        now = utc_now_dt().strftime('%Y-%m-%dT%H:%M:%SZ')  # see resolve_item() above
        with self._lock:
            _logger.info(f'Resolving {len(item_titles)} item(s) in bulk under {title}')
            unresolved = watcher.resolve_many(self.email, title, item_titles, runtime=self._runtime, now=now)
            try:
                watcher.get_watcher(self.email, title)
            except KeyError:
                # see resolve_item() above -- same watch-retirement case
                if not _is_watchlist_migrated(self.email):
                    _get_watchlist(self.email)
                _kv_store.remove(_watchlist_namespace(self.email), title)
            else:
                unresolved = self._recheck_after_resolve(title, unresolved)

        if tree:
            result = watcher.unresolved_tree(unresolved)
        else:
            result = [{'name': k, **v} for k, v in unresolved.items()]
        return result, now

    def undo_resolve(self, title, items, tree=False):
        # issue #83: items is a list of (item_title, expected_last_resolved)
        # pairs -- exactly what a prior resolve_item()/resolve_many() call
        # already reported back to the client for this one watch (its 'now'
        # return value), not a fresh selection. See watcher.undo_resolve_many()
        # for the staleness guard this relies on. Unlike resolve_item()/
        # resolve_many(), undoing never retires a watch -- it only ever
        # moves an item back to unresolved -- so there's no watchlist-entry
        # bookkeeping to do here.
        title = canonicalize_title(title)
        with self._lock:
            _logger.info(f'Undoing resolve of {len(items)} item(s) under {title}')
            unresolved, restored, stale = watcher.undo_resolve_many(
                self.email, title, items, runtime=self._runtime)

        if tree:
            result = watcher.unresolved_tree(unresolved)
        else:
            result = [{'name': k, **v} for k, v in unresolved.items()]
        return result, restored, stale

    def set_preference(self, key, value):
        with self._lock:
            _set_preference(self.email, key, value)

    def get_preference(self, key, default_value=None):
        with self._lock:
            return _get_preference(self.email, key, default_value)

    def save(self):
        with self._lock:
            save_cached_object(self.to_dict(), f'users/{self.email}.json')

    def to_dict(self):
        return {
            'name': self.name,
            'password': self._password_hash,
            'site_admin': self._site_admin,
            'doc_privileges': sorted(self._doc_privileges_copy),
            #'watchlist': self._watchlist,   # deprecated
            #'preferences': self._preferences # deprecated
        }

    @classmethod
    def from_dict(cls, email, d, runtime=None):
        return cls(
            name=d['name'],
            email=email,
            password=d['password'],
            watchlist=d.get('watchlist'),           # legacy
            preferences=d.get('preferences'),       # legacy
            is_hashed=True,
            runtime=runtime,
            # accounts saved before privileges carry role "admin" instead
            site_admin=d.get('site_admin', d.get('role') == 'admin'),
            doc_privileges=d.get('doc_privileges'),
        )
