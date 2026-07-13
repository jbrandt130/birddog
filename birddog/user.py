# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

# user management

import json
from threading import RLock, Lock

from werkzeug.security import generate_password_hash, check_password_hash

from birddog.runtime import ArchiveWatcher
from birddog.cache import (
    load_cached_object,
    save_cached_object,
    remove_cached_object,
    CacheMissError)
from birddog.store import KeyValueStore
from birddog.utility import utc_now_dt, to_utc_format

from birddog.log import get_logger
_logger = get_logger()

_kv_store = KeyValueStore()

# ---------------------------------------------------------------------
# WATCHLIST MANAGEMENT

def _watchlist_key(archive, subarchive):
    return f'{archive}-{subarchive}'

def _watcher_cache_path(email, archive, subarchive):
    return f'watchers/{email}/{archive}-{subarchive}.json'

def _watchlist_namespace(email):
    return f"wl:{email}"

def _to_utc(value):
    # normalize a legacy "YYYY,MM,DD,HH:MM" date to UTC ISO8601; already-ISO
    # (or falsy) values pass through unchanged
    return to_utc_format(value) if value and "," in value else value

def _load_watchlist_item(email, key, raw):
    # parse a stored watchlist item, normalizing any legacy date fields to UTC
    # ISO8601 and persisting the update back to the store if anything changed
    item = json.loads(raw)
    cutoff_date = _to_utc(item.get("cutoff_date"))
    last_checked_date = _to_utc(item.get("last_checked_date"))
    if cutoff_date != item.get("cutoff_date") or last_checked_date != item.get("last_checked_date"):
        item["cutoff_date"] = cutoff_date
        item["last_checked_date"] = last_checked_date
        _kv_store.insert(_watchlist_namespace(email), key, json.dumps(item))
    return item

def _get_watchlist_item(email, key):
    # will raise KeyError if key not in watchlist store
    return _load_watchlist_item(email, key, _kv_store.get(_watchlist_namespace(email), key))

def _get_watchlist(email):
    return {
        k: _load_watchlist_item(email, k, v)
        for k, v in _kv_store.get_all(_watchlist_namespace(email))
    }

def _add_watchlist_item(email, key, cutoff, last_checked=None):
    payload = { "cutoff_date": _to_utc(cutoff)}
    if last_checked:
        payload["last_checked_date"] = _to_utc(last_checked)
    _kv_store.insert(_watchlist_namespace(email), key, json.dumps(payload))

def _remove_watchlist_item(email, key):
    _kv_store.remove(_watchlist_namespace(email), key)

def _update_watchlist_item(email, key, last_checked):
    ns = _watchlist_namespace(email)
    item = _load_watchlist_item(email, key, _kv_store.get(ns, key))
    item["last_checked_date"] = _to_utc(last_checked)
    _kv_store.insert(ns, key, json.dumps(item))

def _load_watchlist(email, watchlist):
    for key, item in watchlist.items():
        _add_watchlist_item(
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
# USER MANAGEMENT

_global_lock = Lock()
_global_user_locks = {}

class User:
    def __init__(self, name, email, password, runtime=None, watchlist=None, preferences=None, is_hashed=False):
        self._name = name
        self._email = email
        self._password_hash = password if is_hashed else generate_password_hash(password)
        self._runtime = runtime

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

    def add_to_watchlist(self, archive, subarchive, cutoff_date):
        key = _watchlist_key(archive, subarchive)
        with self._lock:
            _add_watchlist_item(self.email, key, cutoff_date)

    def remove_from_watchlist(self, archive, subarchive):
        key = _watchlist_key(archive, subarchive)
        with self._lock:
            _remove_watchlist_item(self.email, key)

        # Remove associated watcher file (outside lock)
        watcher_path = _watcher_cache_path(self.email, archive, subarchive)
        try:
            remove_cached_object(watcher_path)
        except CacheMissError:
            pass  # it's already gone
        return True

    def check_archive(self, archive, subarchive, tree=False):
        key = _watchlist_key(archive, subarchive)
        path = _watcher_cache_path(self.email, archive, subarchive)
        with self._lock:
            watchlist_item = _get_watchlist_item(self.email, key)
            try:
                watcher_data = load_cached_object(path)
                watcher = ArchiveWatcher.load(watcher_data, runtime=self._runtime)
            except CacheMissError:
                watcher = ArchiveWatcher(
                    archive, subarchive,
                    watchlist_item['cutoff_date'],
                    runtime=self._runtime)
            watcher.check()
            save_cached_object(watcher.save(), path)
            _update_watchlist_item(self.email, key, utc_now_dt().strftime('%Y-%m-%dT%H:%M:%SZ'))

        # Return just the result, not the watcher itself
        if tree:
            return watcher.unresolved_tree
        return [{'name': k, **v} for k, v in watcher.unresolved.items()]

    def resolve_item(self, archive, subarchive, fond=None, opus=None, case=None, tree=False, deep=False):
        key = _watchlist_key(archive, subarchive)
        path = _watcher_cache_path(self.email, archive, subarchive)
        with self._lock:
            try:
                watcher_data = load_cached_object(path)
            except CacheMissError:
                raise FileNotFoundError('No watcher found')

            watcher = ArchiveWatcher.load(watcher_data, runtime=self._runtime)

            resolve_key = ArchiveWatcher.key(archive, subarchive, fond, opus, case)
            _logger.info(f'Resolving {resolve_key}, deep={deep}, tree={tree}')
            watcher.resolve(resolve_key, deep=deep)

            save_cached_object(watcher.save(), path)

        if tree:
            return watcher.unresolved_tree
        else:
            return [{'name': k, **v} for k, v in watcher.unresolved.items()]

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
        )
