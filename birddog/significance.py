# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Issue #138: background classification of watchlist alerts as significant or
cosmetic (whitespace, dash-variant, category-link-only, relabeled link text).

- Unit of classification is the alert SPAN (title, last_resolved, modified),
  not a revision -- compare endpoints directly, since significance isn't
  monotonic (an edit followed by its own reversal nets to zero).
- Runs in a debounced background sweep over unresolved items only, never on
  the global change path (PageUpdateManager) and never inline in the UI
  check -- bounded by watchlist breadth, not wiki edit volume.
- The verdict is a pure in-process memoization (SignificanceLRU), keyed by
  span; a restart just re-computes. The only durable output is
  `insignificant` + `sig_span` on the unresolved KV row.
- Fail-safe: `insignificant` is a positive assertion only this sweep ever
  sets. Debounced, unclassifiable, errored, or stale-span items are simply
  left untouched, which defaults to "shown as significant" wherever the UI
  later reads this flag -- this can only fail to hide a change, never hide
  one it didn't affirmatively clear.
"""

from datetime import datetime, timezone

from cachetools import LRUCache

from birddog.core import Page
from birddog.wiki import page_significance, canonicalize_title
from birddog.utility import utc_now_dt
from birddog import watcher
from birddog.log import get_logger
_logger = get_logger()

def _parse_utc(ts):
    # matches the one format used throughout watcher.py for 'modified'/
    # 'last_resolved' (e.g. utc_now_dt().strftime('%Y-%m-%dT%H:%M:%SZ'))
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

# "head stable for 20 min" -- aligns with tracker.py's _WIKI_CHANGE_EVENT_WINDOW
_DEBOUNCE_SECONDS = 20 * 60
# bounds new classification work per tick (network fetch + compare), not items
# visited -- get_all_unresolved() returns a stable key order every call, so
# counting mere visits would let an already-classified prefix permanently
# block the rest of a large backlog from ever being reached
_MAX_EXAMINED_PER_TICK = 200


class SignificanceLRU:
    """
    Verdict memoization keyed by span (title, from_date, to_date). Sibling to
    runtime.PageLRU -- pure memoization of a deterministic diff, never
    persisted; a restart just re-computes.
    """
    def __init__(self, maxsize=2000):
        self._lru = LRUCache(maxsize=maxsize)

    def _key(self, title, from_date, to_date):
        return (canonicalize_title(title), from_date, to_date)

    def contains(self, title, from_date, to_date):
        return self._key(title, from_date, to_date) in self._lru

    def lookup(self, title, from_date, to_date, compute):
        key = self._key(title, from_date, to_date)
        try:
            return self._lru[key]
        except KeyError:
            verdict = compute()
            self._lru[key] = verdict
            return verdict


_verdict_lru = SignificanceLRU()


def _is_debounced(entry, now):
    modified = entry.get("modified")
    if not modified:
        return True  # nothing to classify against yet
    age = (now - _parse_utc(modified)).total_seconds()
    return age < _DEBOUNCE_SECONDS


def _classify(title, from_date, to_date, runtime):
    def compute():
        page = Page(title, runtime=runtime)
        reference = Page(title, runtime=runtime).revert_to(from_date)
        if not page.exists or reference is None or not reference.exists:
            # chain gap (deleted/moved/no earlier version) -- direct span
            # compare isn't possible; don't cache a bogus verdict
            return None
        return page_significance(page, reference)
    return _verdict_lru.lookup(title, from_date, to_date, compute)


def _classify_item(email, archive_title, item_title, entry, runtime):
    """
    Returns True if this call performed a new classification (a verdict-LRU
    miss), False if it was a no-cost skip (already classified for this exact
    span, missing dates, or -- rare -- an LRU hit on an unwritten verdict).
    Callers use this to bound *work*, not items visited: get_all_unresolved()
    always returns the same stable key order, so counting mere visits against
    the per-tick cap would let an already-classified prefix permanently starve
    the rest of a large backlog -- see issue #138 sweep-progress bug.
    """
    span = (entry.get("last_resolved"), entry.get("modified"))
    if entry.get("insignificant") and tuple(entry.get("sig_span") or ()) == span:
        return False  # already classified for this exact span

    from_date, to_date = span
    if not from_date or not to_date:
        return False

    did_work = not _verdict_lru.contains(item_title, from_date, to_date)
    try:
        significant = _classify(item_title, from_date, to_date, runtime)
    except Exception:
        _logger.exception(f"significance: failed to classify {item_title} ({from_date} -> {to_date})")
        return did_work

    if significant is None or significant:
        return did_work  # unclassifiable, or genuinely significant -- write nothing, stays visible

    # check_watcher()/resolve_watcher() may have moved this item on while we
    # were fetching pages over the network -- only stamp the verdict if it's
    # still current, otherwise it's a stale span; discard silently
    try:
        current = watcher.get_unresolved(email, archive_title, item_title)
    except KeyError:
        return did_work
    if (current.get("last_resolved"), current.get("modified")) != span:
        return did_work

    current["insignificant"] = True
    current["sig_span"] = list(span)
    watcher.put_unresolved(email, archive_title, item_title, current)
    return did_work


def sweep(runtime):
    """One sweep pass over every active watch's unresolved items. Returns the
    number of items newly classified this tick, for logging."""
    now = utc_now_dt()
    examined = 0
    for watch in watcher.get_all_active_watches():
        email, archive_title = watch["email"], watch["title"]
        unresolved = watcher.get_all_unresolved(email, archive_title)
        for item_title, entry in unresolved.items():
            if entry.get("moved_to"):
                continue  # the watch's own dead-redirect marker, not a content page
            if _is_debounced(entry, now):
                continue
            if examined >= _MAX_EXAMINED_PER_TICK:
                _logger.info(
                    f"significance sweep: hit per-tick cap ({_MAX_EXAMINED_PER_TICK}), "
                    "resuming next cycle")
                return examined
            if _classify_item(email, archive_title, item_title, entry, runtime):
                examined += 1
    return examined
