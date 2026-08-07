# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

# per-item watcher state (resolved/unresolved) storage and business logic

import json
from urllib.parse import quote

import regex

from birddog.wiki import (
    canonicalize_title,
    archive_root,
    page_title_from_address,
    title_in_scope,
    page_label,
    )
from birddog.cache import (
    load_cached_object,
    remove_cached_object,
    CacheMissError,
    )
from birddog.store import KeyValueStore
from birddog.utility import utc_now_dt, to_utc_format

from birddog.log import get_logger
_logger = get_logger()

# watcher state gets its own table: it's high-volume/high-churn (a row per
# unresolved/resolved item, across every watch) compared to the small,
# low-churn account/watchlist-metadata rows in the default "kv" table
_WATCHER_TABLE_NAME = "bd_watchers"
_watcher_kv = KeyValueStore(table_name=_WATCHER_TABLE_NAME)

def _watcher_ns(email):
    return f"w:{email}"

def _resolved_ns(email, archive_title):
    return f"r:{email}:{archive_title}"

def _unresolved_ns(email, archive_title):
    return f"u:{email}:{archive_title}"

def _watcher_cache_path(email, archive_title):
    # legacy (pre-KV) per-(user, watch) blob location; only ever read by the
    # one-time migration below, and by nothing else once migrated
    return f'watchers/{email}/{quote(archive_title, safe="")}.json'

# ---------------------------------------------------------------------
# storage primitives -- header ("watcher" row: version/include/exclude/
# cutoff_date/last_checked_date), resolved (per-item history list), and
# unresolved (per-item current state) are three separate KV partitions per
# (user, watch), so that touching a handful of items never requires reading
# or writing the watch's entire lifetime history.

def get_watcher(email, archive_title):
    # raises KeyError if this watch has no header yet
    return json.loads(_watcher_kv.get(_watcher_ns(email), archive_title))

def put_watcher(email, archive_title, header):
    _watcher_kv.insert(_watcher_ns(email), archive_title, json.dumps(header))

def remove_watcher(email, archive_title):
    _watcher_kv.remove_if_exists(_watcher_ns(email), archive_title)
    _watcher_kv.remove_all(_resolved_ns(email, archive_title))
    _watcher_kv.remove_all(_unresolved_ns(email, archive_title))
    try:
        remove_cached_object(_watcher_cache_path(email, archive_title))
    except CacheMissError:
        pass  # already gone, or never migrated off the legacy blob

def get_unresolved(email, archive_title, item_title):
    return json.loads(_watcher_kv.get(_unresolved_ns(email, archive_title), item_title))

def put_unresolved(email, archive_title, item_title, entry):
    _watcher_kv.insert(_unresolved_ns(email, archive_title), item_title, json.dumps(entry))

def remove_unresolved(email, archive_title, item_title):
    _watcher_kv.remove_if_exists(_unresolved_ns(email, archive_title), item_title)

def get_all_unresolved(email, archive_title):
    return {k: json.loads(v) for k, v in _watcher_kv.get_all(_unresolved_ns(email, archive_title))}

def get_resolved(email, archive_title, item_title):
    # full history for this one item; [] if it's never been resolved
    try:
        return json.loads(_watcher_kv.get(_resolved_ns(email, archive_title), item_title))
    except KeyError:
        return []

def put_resolved(email, archive_title, item_title, history):
    _watcher_kv.insert(_resolved_ns(email, archive_title), item_title, json.dumps(history))

# ---------------------------------------------------------------------
# unresolved-items tree (consumed by the client's tree view; ?tree=1/tree=1
# is always requested by both /watchlist/<title>/check and /resolve)

_DASH_CHARS = r"\-\u2010\u2011\u2012\u2013\u2014"
_ALPHA_DASH = fr"[\p{{L}}{_DASH_CHARS}]*"
_pattern = regex.compile(fr"^({_ALPHA_DASH})(\d+)({_ALPHA_DASH})$")

def _parse_string(s):
    match = _pattern.fullmatch(s)
    if match:
        prefix, number, suffix = match.groups()
        return (int(number), prefix, suffix)
    return (float('inf'), s, '')

def _sort_keys(keys):
    return sorted(keys, key=_parse_string)

def _flatten_hierarchy(d, prefix=None):
    result = []
    prefix = prefix or []

    children = [k for k in d if k != 'unresolved']
    sorted_children = _sort_keys(children)
    for key in sorted_children:
        current_path = prefix + [key]
        full_path_str = '/'.join(current_path)

        value = d[key]
        unresolved = value.get('unresolved') if isinstance(value, dict) else None

        result.append((full_path_str, unresolved))

        if isinstance(value, dict):
            result.extend(_flatten_hierarchy(value, current_path))

    def _gen_title(item):
        # a node's full_path_str is already its real title, since it's built
        # by rejoining literal title-path segments -- ensure every node
        # (leaf or synthesized intermediate) carries it in its own record,
        # since the frontend reads meta.title rather than the tree path.
        # "label" is this node's own latinized display segment (page_label()
        # transliterates path segments 1:1, so the last segment of
        # page_label(path) always corresponds to this node's own name).
        path, value = item
        label = page_label(path).split("/")[-1]
        if not value:
            return (path, { "title": path, "label": label })
        return (path, { **value, "title": path, "label": label })

    return [_gen_title(item) for item in result]

def _make_tree(unresolved):
    root = {}
    for key, value in unresolved.items():
        pos = root
        for part in key.split("/"):
            if part not in pos:
                pos[part] = {}
            pos = pos[part]
        pos['unresolved'] = value
    return root

def unresolved_tree(unresolved):
    return _flatten_hierarchy(_make_tree(unresolved))

# ---------------------------------------------------------------------
# legacy blob migration
#
# One-time, lazy transform of the pre-KV per-(user, watch) blob (a single
# JSON object holding the full resolved/unresolved history, versioned v1-v8)
# into the new per-item KV schema. Ported near-verbatim from the retired
# ArchiveWatcher.load()/check() -- see git history (birddog/runtime.py) for
# the class this replaces.

def _legacy_check(runtime, include, exclude, cutoff_date, last_checked_date, resolved, unresolved):
    page_manager = runtime.update_manager
    updates = page_manager.get_updates(include, exclude=exclude, cutoff_date=last_checked_date)
    if updates:
        new_cutoff = last_checked_date
        for title, update in updates.items():
            item = canonicalize_title(title)
            mod_date = update["timestamp"]
            new_cutoff = max(mod_date, new_cutoff)
            user = update.get("user", "")
            latest_resolved = resolved[item][-1]["modified"] if item in resolved else cutoff_date
            if latest_resolved is None or mod_date[:16] > latest_resolved[:16]:
                unresolved[item] = {
                    "modified": mod_date,
                    "last_resolved": resolved[item][-1]["last_resolved"] if item in resolved else cutoff_date,
                    "user": user,
                }
        last_checked_date = new_cutoff
    return last_checked_date

def _load_legacy_blob(data, runtime=None):
    version = data.get("version", "v1")  # default to legacy
    if "include" in data:
        include = list(data["include"])
        exclude = list(data.get("exclude") or [])
        cutoff_date = data["cutoff_date"]
    else:
        # legacy archive/subarchive-addressed watcher -- coarsen to the
        # whole archive, matching the watchlist-item migration in user.py
        include = [archive_root(data['archive'], data['subarchive'])]
        exclude = []
        cutoff_date = data['cutoff_date']

    unresolved = data.get('unresolved', {})
    last_checked_date = data.get('last_checked_date', cutoff_date)

    if version == "v1":
        resolved = {
            k: (
                [{"modified": v, "last_resolved": cutoff_date}]
                if not isinstance(v, list) else v
            )
            for k, v in data.get('resolved', {}).items()
        }
    else:
        resolved = data.get('resolved', {})

    if version == "v2":
        for key, unresolved_item in unresolved.items():
            if "title" not in unresolved_item:
                unresolved_item["title"] = page_title_from_address(key.split(","))
        for key, resolved_items in resolved.items():
            for resolved_entry in resolved_items:
                if "title" not in resolved_entry:
                    resolved_entry["title"] = page_title_from_address(key.split(","))

    if version < "v5":
        # convert legacy "YYYY,MM,DD,HH:MM" dates to UTC ISO8601 -- must run
        # before the "v4" check() below, since check() compares these dates
        # against already-UTC mod dates from the update pipeline
        def _migrate_date(value):
            return to_utc_format(value) if value else value

        cutoff_date = _migrate_date(cutoff_date)
        last_checked_date = _migrate_date(last_checked_date)
        for unresolved_item in unresolved.values():
            unresolved_item["modified"] = _migrate_date(unresolved_item.get("modified"))
            unresolved_item["last_resolved"] = _migrate_date(unresolved_item.get("last_resolved"))
        for resolved_entries in resolved.values():
            for resolved_entry in resolved_entries:
                resolved_entry["modified"] = _migrate_date(resolved_entry.get("modified"))
                resolved_entry["last_resolved"] = _migrate_date(resolved_entry.get("last_resolved"))

    if version < "v4":
        # dispose of all unresolved items - force refresh
        unresolved = {}
        last_checked_date = _legacy_check(runtime, include, exclude, cutoff_date, last_checked_date, resolved, unresolved)

    if version < "v6":
        # drop ghost entries: same source edit, same minute, no real new edit
        # (see ArchiveWatcher.load, retired -- git history has the full story)
        for key in list(unresolved.keys()):
            resolved_entries = resolved.get(key)
            if not resolved_entries:
                continue
            last_modified = resolved_entries[-1].get("modified")
            unresolved_modified = unresolved[key].get("modified")
            if (last_modified and unresolved_modified
                    and last_modified[:16] == unresolved_modified[:16]):
                del unresolved[key]

    if version < "v7":
        # re-run the same cleanup: the v6 comparison bug kept regenerating
        # the same ghosts on stores that were already at v6
        for key in list(unresolved.keys()):
            resolved_entries = resolved.get(key)
            if not resolved_entries:
                continue
            last_modified = resolved_entries[-1].get("modified")
            unresolved_modified = unresolved[key].get("modified")
            if (last_modified and unresolved_modified
                    and last_modified[:16] == unresolved_modified[:16]):
                del unresolved[key]

    if version < "v8":
        # legacy stores key resolved/unresolved by comma-joined address
        # tuple ("archive,subarchive,fond,opus,case"); re-key by title to
        # match the current item identity
        def _retitle(key):
            try:
                return page_title_from_address(key.split(","))
            except (KeyError, ValueError):
                return key
        resolved = {_retitle(k): v for k, v in resolved.items()}
        unresolved = {_retitle(k): v for k, v in unresolved.items()}

    header = {
        "version": "v9",
        "include": include,
        "exclude": exclude,
        "cutoff_date": cutoff_date,
        "last_checked_date": last_checked_date,
    }
    return header, resolved, unresolved

def _ensure_migrated(email, archive_title, runtime):
    try:
        get_watcher(email, archive_title)
        return  # already KV-native, nothing to do
    except KeyError:
        pass

    path = _watcher_cache_path(email, archive_title)
    try:
        data = load_cached_object(path)
    except CacheMissError:
        return  # no legacy blob either -- brand new watch

    header, resolved, unresolved = _load_legacy_blob(data, runtime=runtime)
    put_watcher(email, archive_title, header)
    # bulk writes: a long-lived watch can have accumulated thousands of
    # resolved/unresolved items over its history -- one row-per-call here
    # would turn a single migration into thousands of logged KV calls
    if resolved:
        _watcher_kv.insert_many(_resolved_ns(email, archive_title), {k: json.dumps(v) for k, v in resolved.items()})
    if unresolved:
        _watcher_kv.insert_many(_unresolved_ns(email, archive_title), {k: json.dumps(v) for k, v in unresolved.items()})
    remove_cached_object(path)

# ---------------------------------------------------------------------
# business logic (replaces ArchiveWatcher.check() / .resolve())

def check_watcher(email, archive_title, runtime, include, cutoff_date, exclude=None):
    _ensure_migrated(email, archive_title, runtime)
    try:
        header = get_watcher(email, archive_title)
    except KeyError:
        header = {
            "version": "v9",
            "include": list(include),
            "exclude": list(exclude) if exclude else [],
            "cutoff_date": cutoff_date,
            "last_checked_date": cutoff_date,
        }

    page_manager = runtime.update_manager
    # bounded to updates since the last check, so already-resolved edits
    # from prior cycles aren't handed back and re-evaluated forever
    updates = page_manager.get_updates(header["include"], exclude=header["exclude"], cutoff_date=header["last_checked_date"])
    _logger.info(f"check_watcher: found {len(updates)} updates.")
    if updates:
        # a stale/first-ever check can turn up thousands of changed items
        # (e.g. a year-old cutoff on a busy archive) -- one KV round trip
        # per item here would flood the store, so look up and write back
        # the whole batch as two bulk calls instead
        items = {canonicalize_title(title): update for title, update in updates.items()}
        resolved_raw = _watcher_kv.get_many(_resolved_ns(email, archive_title), list(items.keys()))
        resolved_by_item = {k: json.loads(v) for k, v in resolved_raw.items()}

        new_unresolved = {}
        new_cutoff = header["last_checked_date"]
        for item, update in items.items():
            mod_date = update["timestamp"]
            new_cutoff = max(mod_date, new_cutoff)
            user = update.get("user", "")
            resolved_history = resolved_by_item.get(item, [])
            latest_resolved = resolved_history[-1]["modified"] if resolved_history else header["cutoff_date"]
            # compare to minute precision: legacy dates have their seconds
            # permanently zeroed, so a raw compare against a full-precision
            # live mod_date always reads as "newer" by a few seconds even
            # when it's the same edit
            if latest_resolved is None or mod_date[:16] > latest_resolved[:16]:
                new_unresolved[item] = {
                    "modified": mod_date,
                    "last_resolved": resolved_history[-1]["last_resolved"] if resolved_history else header["cutoff_date"],
                    "user": user,
                }
        if new_unresolved:
            _watcher_kv.insert_many(_unresolved_ns(email, archive_title), {k: json.dumps(v) for k, v in new_unresolved.items()})
        header["last_checked_date"] = new_cutoff

    put_watcher(email, archive_title, header)
    return get_all_unresolved(email, archive_title)

def resolve_watcher(email, archive_title, item, runtime=None, deep=False):
    _ensure_migrated(email, archive_title, runtime)
    try:
        get_watcher(email, archive_title)
    except KeyError:
        raise FileNotFoundError('No watcher found')

    now = utc_now_dt().strftime('%Y-%m-%dT%H:%M:%SZ')
    item = canonicalize_title(item)

    if deep:
        _logger.info(f'resolve_watcher: deep resolve: {item}')
        # a coarse-scoped watch can have accumulated a large unresolved set
        # under one prefix -- resolve the whole batch as three bulk calls
        # rather than one KV round trip per matched item
        matches = {
            key: entry for key, entry in get_all_unresolved(email, archive_title).items()
            if title_in_scope(key, [item])
        }
        if matches:
            resolved_raw = _watcher_kv.get_many(_resolved_ns(email, archive_title), list(matches.keys()))
            resolved_by_item = {k: json.loads(v) for k, v in resolved_raw.items()}
            new_resolved = {}
            for key, entry in matches.items():
                _logger.info(f'resolve_watcher: deep resolving subitem: {key}; {item}')
                entry["last_resolved"] = now
                history = resolved_by_item.get(key, [])
                history.append(entry)
                new_resolved[key] = history
            _watcher_kv.remove_many(_unresolved_ns(email, archive_title), list(matches.keys()))
            _watcher_kv.insert_many(_resolved_ns(email, archive_title), {k: json.dumps(v) for k, v in new_resolved.items()})
    else:
        try:
            entry = get_unresolved(email, archive_title, item)
        except KeyError:
            entry = None
        if entry is not None:
            entry["last_resolved"] = now
            remove_unresolved(email, archive_title, item)
            history = get_resolved(email, archive_title, item)
            history.append(entry)
            put_resolved(email, archive_title, item, history)

    return get_all_unresolved(email, archive_title)
