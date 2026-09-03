# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
FTPSiteManager - background inventory builder for the targeted SFTP repo.

On each heartbeat the tracker pulls a batch of directory records from the
"BD:Directories" view of the "FTP Site" table (type == dir, sorted on CreatedAt),
advancing an in-memory cursor. When the scan reaches the end of the view the
cursor resets to the beginning, so the tracker cycles through the tree forever,
first discovering it and then continuously refreshing it.

For every directory in the batch it first stats the directory: if the stored
mtime still matches the server's, the contents are current and the listing is
skipped. Otherwise it issues one sftp.listdir_attr() call and collects a row per
child entry (plus the directory's own row), then reconciles the table against
the listing (marking rows for entries that no longer exist on the server). Only
directories and PDF files are kept; dot-entries and every other file type
(notably jpg) are ignored.

Rows gathered from every directory in the batch are written in a single upsert,
and stale rows removed in a single delete, at the end of the heartbeat - one
reservation-guarded round trip per beat instead of one per directory.

The root "/" is a normal row too (created by the bootstrap on an empty table),
so it is scanned from the view and stat-checked once per sweep like any other
directory; a top-level add or remove bumps its mtime and triggers a re-list.

FTPSiteManager also maintains the many-to-many relation between "FTP Site" PDF
rows and "Documents". Each heartbeat, after the inventory pass, it match-checks a
slice of the "BD:Need Match Check" view: for a PDF row it computes a content
fingerprint (size + sha1 of the first and last 64 KB, read over SFTP), finds
Documents of the same byte size, fingerprints those from the wiki (imageinfo +
HTTP Range), and links the row to every Document whose fingerprint matches - all
of them, since one file can sit at several FTP paths and one file backs both a
Commons and a Wikisource Document. Equal fingerprint is treated as definitive:
no fingerprint match means not the same file, so no link. A PDF row re-enters the
view when its size or mtime changes, and on_documents_changed() puts affected
rows back in the view when a Document is written.

The reference walk lives in notebooks/sftp.ipynb; matching experiments are in
notebooks/FTP Sync.ipynb.
"""

import hashlib
import json
import logging
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import unquote

import paramiko
import requests

from birddog.abstract_database import ConfigError
from birddog.fetch import fetch_url, FetchUrlFailError
from birddog.utility import HeartbeatManager
from birddog.log import get_logger

_logger = get_logger()

# paramiko's transport thread logs every connect/auth/close at INFO and dumps a
# full traceback at ERROR whenever a connection is refused mid-banner. Those are
# expected here (jewishgen rate-limits connection setup; _connect retries), so
# quiet them - _connect / heartbeat log the outcome that actually matters.
logging.getLogger("paramiko").setLevel(logging.WARNING)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

_FTP_CONFIG_PATH        = "resources/ftp_config.json"
_FTP_TABLE              = "FTP Site"
_FTP_LINK_FIELD         = "source_docs"      # FTP Site -> Documents (m:m)
_DOC_TABLE              = "Documents"
_DOC_LINK_FIELD         = "ftp_items"        # Documents -> FTP Site (m:m)
_FTP_DIR_VIEW           = "BD:Directories"
_FTP_MATCH_VIEW         = "BD:Need Match Check"
_KEEP_SUFFIXES          = frozenset({"pdf"})

_FTP_DEFAULT_LINK_BATCH = 200     # FTP rows match-checked per heartbeat
_MATCH_FLUSH_EVERY      = 25      # persist match_checked in sub-batches (interruptible)

# content fingerprint: sha1 of the first and last _FP_CHUNK bytes, plus the byte
# size. Equal fingerprints imply byte-identical files (validated empirically -
# head alone collides on shared cover pages, head+tail does not).
_FP_CHUNK              = 65536
_WIKI_HOSTS           = ("commons.wikimedia.org", "wikisource.org", "wikipedia.org")

_FTP_DEFAULT_INTERVAL   = 60      # seconds between heartbeats
_FTP_DEFAULT_SCAN_BATCH = 50      # directories processed per heartbeat
_FTP_CONNECT_TIMEOUT    = 30      # seconds
_FTP_KEEPALIVE          = 15      # seconds; keeps an idle SFTP channel open
_FTP_MAX_CONN_AGE       = 1800    # seconds; a last-resort proactive recycle so a
                                  # dead socket can't be stranded forever. jewishgen
                                  # already drops the connection every ~60-90s, so
                                  # reactive reconnect does the real work - keep this
                                  # high to avoid piling on its connection rate limit

_REQUIRED_CONFIG_KEYS   = ("host", "user", "password", "port")


# ---------------------------------------------------------------------------
# Configuration

def _load_ftp_config(path=_FTP_CONFIG_PATH):
    """
    Load resources/ftp_config.json and resolve the password.

    The "password" value names an environment variable that holds the actual
    password, so the secret never lands in a checked-in file. "heartbeat_interval"
    and "scan_batch" are optional and fall back to module defaults.
    """
    with open(path, encoding="utf8") as f:
        cfg = json.load(f)

    for key in _REQUIRED_CONFIG_KEYS:
        if key not in cfg:
            raise ConfigError(f"ftp_config: missing required key {key!r}")

    pw_var = cfg["password"]
    password = os.environ.get(pw_var)
    if not password:
        raise ConfigError(
            f"ftp_config: password env var {pw_var!r} is not set"
        )

    cfg = dict(cfg)
    cfg["password"] = password
    cfg.setdefault("heartbeat_interval", _FTP_DEFAULT_INTERVAL)
    cfg.setdefault("scan_batch", _FTP_DEFAULT_SCAN_BATCH)
    return cfg


# ---------------------------------------------------------------------------
# Path / value helpers

def _join(parent, name):
    """Join a POSIX directory path and a child name into an absolute path."""
    if parent == "/":
        return "/" + name
    return parent.rstrip("/") + "/" + name


def _depth(path):
    """
    Depth of an item below the FTP root.

    "/Berdichev" -> 0, "/Berdichev/1-2-3.pdf" -> 1. Matches the semantics of the
    existing research/ftp inventory export.
    """
    return max(len(PurePosixPath(path).parts) - 2, 0)


def _parent(path):
    """POSIX parent directory of an absolute path ('/A/b' -> '/A', '/A' -> '/')."""
    return str(PurePosixPath(path).parent)


def _utc_mtime(epoch):
    """Format an epoch timestamp as a UTC 'YYYY-MM-DD HH:MM:SS' string."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _mtime_matches(stored, epoch):
    """
    True if a table mtime value and a fresh SFTP epoch refer to the same second.

    The stored value comes back from NocoDB with a timezone offset
    ('2025-04-14 04:41:27+00:00'); a value we wrote is offset-free. Both are
    parsed to instants (a naive stored value is assumed UTC, which is how we
    write them) and compared at whole-second resolution.
    """
    if stored is None or epoch is None:
        return False
    try:
        s = datetime.fromisoformat(str(stored))
    except ValueError:
        return False
    if s.tzinfo is None:
        s = s.replace(tzinfo=timezone.utc)
    fresh = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return s == fresh


# ---------------------------------------------------------------------------
# Content fingerprint

def _content_fp(size, head, tail):
    """
    "<size>:<sha1(head)>:<sha1(tail)>" - the match key for a file.

    Both sides (SFTP file, wiki file) must build it the same way: files at or
    below 2*_FP_CHUNK are hashed whole (head == tail == the whole file); larger
    files use the first and last _FP_CHUNK bytes.
    """
    return (
        f"{size}:{hashlib.sha1(head).hexdigest()}:{hashlib.sha1(tail).hexdigest()}"
    )


def _fp_ftp(sftp, path, size):
    """Content fingerprint of an SFTP file, reading only the first/last chunk."""
    with sftp.open(path, "rb") as f:
        if size is None or size <= 2 * _FP_CHUNK:
            blob = f.read()
            return _content_fp(size if size is not None else len(blob), blob, blob)
        head = f.read(_FP_CHUNK)
        f.seek(size - _FP_CHUNK, 0)
        tail = f.read(_FP_CHUNK)
    return _content_fp(size, head, tail)


def _parse_wiki_page_url(url):
    """
    ("commons.wikimedia.org", "File:Name.pdf") for a wiki File: page URL,
    otherwise (None, None) - the caller then skips fingerprint verification.
    """
    try:
        host = url.split("/", 3)[2]
    except (IndexError, AttributeError):
        return None, None
    if "/wiki/" not in url:
        return None, None
    if not any(host == h or host.endswith("." + h) for h in _WIKI_HOSTS):
        return None, None
    return host, unquote(url.split("/wiki/", 1)[1])


class _FingerprintUnavailable(Exception):
    """
    A wiki file's fingerprint could not be obtained this pass. The row is left
    unchecked and retried - a Document with byte_size metadata has a valid wiki
    title, so the fetch is expected to succeed eventually.
    """


_WIKI_GONE = object()   # sentinel: imageinfo says the file is genuinely deleted


def _wiki_file_info(host, title):
    """
    (direct_file_url, size, sha1) from the MediaWiki imageinfo API.

    Returns _WIKI_GONE only when the API explicitly reports the file missing or
    the title invalid. Any other shortfall (transient error, malformed response,
    imageinfo without a url/size) raises _FingerprintUnavailable so the caller
    retries rather than concluding "no match".
    """
    try:
        data = fetch_url(
            f"https://{host}/w/api.php",
            params={
                "action": "query", "format": "json", "prop": "imageinfo",
                "iiprop": "url|size|sha1", "titles": title,
            },
            return_json=True,
        )
    except FetchUrlFailError as e:
        raise _FingerprintUnavailable(f"imageinfo fetch failed: {e}") from e
    except requests.exceptions.RequestException as e:
        raise _FingerprintUnavailable(f"imageinfo fetch failed: {e}") from e
    except RuntimeError as e:
        raise _FingerprintUnavailable(f"imageinfo fetch failed: {e}") from e

    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        # A file hosted on a shared repo (Commons) has no local description
        # page, so a wiki that only *references* it reports "missing" while
        # still returning usable imageinfo from the shared repo. Prefer the
        # imageinfo; only conclude the file is gone when there is none.
        info = page.get("imageinfo")
        if info and info[0].get("url") and info[0].get("size") is not None:
            return info[0]["url"], info[0]["size"], info[0].get("sha1")
        if "missing" in page or "invalid" in page:
            return _WIKI_GONE
    raise _FingerprintUnavailable(f"imageinfo response had no usable file info for {title!r}")


def _range_bytes(url, spec):
    """GET one byte range ("0-65535" or "-65536") via the throttled fetch client."""
    try:
        return fetch_url(url, headers={"Range": f"bytes={spec}"}, content=True)
    except FetchUrlFailError as e:
        raise _FingerprintUnavailable(f"range fetch failed: {e}") from e
    except requests.exceptions.RequestException as e:
        raise _FingerprintUnavailable(f"range fetch failed: {e}") from e
    except RuntimeError as e:
        raise _FingerprintUnavailable(f"range fetch failed: {e}") from e


def _fp_wiki(page_url):
    """
    Content fingerprint of a wiki-hosted file.

    Returns None only when the URL is not a wiki File: page or the wiki reports
    the file genuinely deleted. Raises _FingerprintUnavailable for every other
    failure so the caller retries instead of treating the row as checked.
    """
    host, title = _parse_wiki_page_url(page_url)
    if not host:
        return None
    info = _wiki_file_info(host, title)
    if info is _WIKI_GONE:
        return None
    direct_url, size, _sha1 = info
    if size <= 2 * _FP_CHUNK:
        blob = _range_bytes(direct_url, f"0-{size - 1}")
        return _content_fp(size, blob, blob)
    head = _range_bytes(direct_url, f"0-{_FP_CHUNK - 1}")
    tail = _range_bytes(direct_url, f"-{_FP_CHUNK}")
    return _content_fp(size, head, tail)


# ---------------------------------------------------------------------------

class FTPSiteManager(HeartbeatManager):
    def __init__(self, db, config_path=_FTP_CONFIG_PATH):
        self._db = db
        self._config = _load_ftp_config(config_path)
        self._scan_batch = int(self._config["scan_batch"])

        # opaque paging token into the "BD:Directories" view; None means "start at
        # the beginning" both on the very first heartbeat and after a full sweep.
        self._cursor = None

        # per-sweep counters, for the "sweep complete" summary log
        self._sweep_started_at = None
        self._sweep_dirs = 0
        self._sweep_written = 0
        self._sweep_pruned = 0
        self._sweep_skipped = 0

        self._client = None    # paramiko.SSHClient
        self._sftp = None      # paramiko.SFTPClient
        self._connected_at = 0.0

        # relation maintenance: match-check up to this many FTP rows per heartbeat
        self._link_batch = int(self._config.get("link_batch", _FTP_DEFAULT_LINK_BATCH))

        _logger.info(
            f"FTPSiteManager: host={self._config['host']} "
            f"interval={self._config['heartbeat_interval']}s batch={self._scan_batch} "
            f"link_batch={self._link_batch}"
        )
        super().__init__(interval=float(self._config["heartbeat_interval"]))

    # -- SFTP connection ----------------------------------------------------

    def _connect(self):
        # jewishgen rate-limits connection setup: a rapid reconnect authenticates
        # and answers one op, then the server resets the channel a fraction of a
        # second later. Retry with backoff, and only accept a connection that
        # survives a warm-up (stat, pause, stat) - so callers get a working
        # connection instead of a burst of mid-drain reconnects.
        last = None
        for attempt in range(6):
            if attempt:
                time.sleep(min(2 ** attempt, 20))    # 2, 4, 8, 16, 20 s
            client = None
            try:
                client = self._open_client()          # banner/reset failures land here too
                sftp = client.open_sftp()
                sftp.stat("/")
                time.sleep(1.0)
                sftp.stat("/")                       # a doomed channel dies here
            except (OSError, paramiko.SSHException, EOFError) as e:
                last = e
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                continue
            self._client = client
            self._sftp = sftp
            self._connected_at = time.monotonic()
            _logger.info(
                f"FTPSiteManager: connected to {self._config['host']}"
                + (f" (after {attempt} retr{'y' if attempt == 1 else 'ies'})"
                   if attempt else "")
            )
            return
        raise last if last is not None else paramiko.SSHException("connect failed")

    def _open_client(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # stop paramiko scanning ssh-agent / ~/.ssh, which can hang or pick the
        # wrong credential (see notebooks/sftp.ipynb).
        old_sock = os.environ.pop("SSH_AUTH_SOCK", None)
        try:
            client.connect(
                self._config["host"],
                port=int(self._config["port"]),
                username=self._config["user"],
                password=self._config["password"],
                timeout=_FTP_CONNECT_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
        finally:
            if old_sock is not None:
                os.environ["SSH_AUTH_SOCK"] = old_sock

        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(_FTP_KEEPALIVE)
        return client

    def _ensure_connection(self):
        if self._sftp is not None and (
            time.monotonic() - self._connected_at > _FTP_MAX_CONN_AGE
        ):
            _logger.debug("FTPSiteManager: recycling aged SFTP connection")
            self._close_connection()
        if self._sftp is None:
            self._connect()
        return self._sftp

    def _close_connection(self):
        for obj in (self._sftp, self._client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._sftp = None
        self._client = None

    def _is_transport_error(self, exc):
        """
        True if exc means the SFTP channel is unusable and must be rebuilt.

        Only a genuinely per-path failure - a missing or forbidden path - is
        survivable on the same connection. Everything else (a dropped socket
        surfaces as OSError("Socket is closed"), a timeout, EOFError, or an
        SSHException) needs a reconnect. The old transport.is_active() probe was
        unreliable: after the peer closes the socket the transport thread can
        still briefly report the channel as active, so a dead connection was
        reused forever.
        """
        return not isinstance(exc, (FileNotFoundError, PermissionError))

    def stop(self):
        super().stop()
        self._close_connection()

    # -- heartbeat --------------------------------------------------------

    def heartbeat(self):
        if self._db is None:
            return

        try:
            sftp = self._ensure_connection()
        except Exception as e:
            _logger.warning(f"FTPSiteManager: SFTP connect failed: {e}")
            return

        try:
            rows, next_cursor = self._db.scan(
                _FTP_TABLE,
                limit=self._scan_batch,
                cursor=self._cursor,
                view_name=_FTP_DIR_VIEW,
                fields=["path", "mtime"],
            )
        except Exception as e:
            _logger.warning(f"FTPSiteManager: directory scan failed: {e}")
            return

        starting_sweep = self._cursor is None
        if starting_sweep:
            self._begin_sweep()

        # (path, last-known mtime) for each directory to visit this heartbeat.
        # The root "/" is a normal row (created by the bootstrap below), so it
        # comes back from the view like any other directory - it is the oldest
        # row, so every sweep starts with it.
        dirs = [(r["path"], r.get("mtime")) for r in rows]
        if not dirs and starting_sweep:
            # empty table: seed the root row. Once "/" exists it comes back from
            # the view like any other directory and needs no special-casing.
            dirs = [("/", None)]

        # advance the cursor now; a next_cursor of None restarts the sweep on
        # the following heartbeat.
        self._cursor = next_cursor

        beat_skipped = 0
        beat_dirs = 0
        pending_writes = []       # rows to upsert, batched across the whole beat
        pending_prune = []        # (path, Id) pairs to delete, batched
        for dirpath, known_mtime in dirs:
            try:
                records, prune_pairs, skipped = self._sync_directory(
                    sftp, dirpath, known_mtime)
                beat_dirs += 1
                beat_skipped += skipped
                pending_writes.extend(records)
                pending_prune.extend(prune_pairs)
            except (OSError, paramiko.SSHException, EOFError) as e:
                _logger.warning(f"FTPSiteManager: sync failed for {dirpath}: {e}")
                if self._is_transport_error(e):
                    self._close_connection()
                    break

        # one write and one delete for the entire heartbeat, rather than a
        # reservation-guarded round trip per directory.
        beat_written = self._flush_writes(pending_writes)
        beat_pruned = self._flush_prune(pending_prune, pending_writes)

        self._sweep_dirs += beat_dirs
        self._sweep_written += beat_written
        self._sweep_pruned += beat_pruned
        self._sweep_skipped += beat_skipped

        _logger.info(
            f"FTPSiteManager: heartbeat - {beat_dirs}/{len(dirs)} dir(s), "
            f"{beat_skipped} unchanged, {beat_written} row(s) upserted, "
            f"{beat_pruned} pruned"
        )

        if self._cursor is None:
            self._end_sweep()

        # maintain the Documents <-> FTP Site relation for a slice of the
        # "needs match check" backlog, on the same connection/thread.
        self._drain_match_queue()

    def _begin_sweep(self):
        self._sweep_started_at = time.monotonic()
        self._sweep_dirs = 0
        self._sweep_written = 0
        self._sweep_pruned = 0
        self._sweep_skipped = 0
        _logger.info("FTPSiteManager: starting inventory sweep")

    def _end_sweep(self):
        elapsed = 0.0
        if self._sweep_started_at is not None:
            elapsed = time.monotonic() - self._sweep_started_at
        _logger.info(
            f"FTPSiteManager: sweep complete - {self._sweep_dirs} directories "
            f"({self._sweep_skipped} unchanged), {self._sweep_written} row(s) upserted, "
            f"{self._sweep_pruned} pruned, {elapsed:.0f}s"
        )

    # -- batched flush --------------------------------------------------

    def _flush_writes(self, records):
        """Upsert every row collected this heartbeat in one call."""
        if not records:
            return 0
        try:
            self._db.write(_FTP_TABLE, records)
        except Exception as e:
            _logger.warning(
                f"FTPSiteManager: batch write of {len(records)} row(s) failed: {e}"
            )
            return 0
        return len(records)

    def _flush_prune(self, prune_pairs, written_records):
        """Delete every stale row collected this heartbeat in one call."""
        if not prune_pairs:
            return 0
        # never delete a row that this same heartbeat re-created under a
        # different parent (an entry that moved between listed directories).
        written_paths = {r["path"] for r in written_records}
        del_ids, seen = [], set()
        for path, rid in prune_pairs:
            if path in written_paths or rid in seen:
                continue
            seen.add(rid)
            del_ids.append(rid)
        if not del_ids:
            return 0
        try:
            self._db.delete(_FTP_TABLE, del_ids)
        except Exception as e:
            _logger.warning(
                f"FTPSiteManager: batch prune of {len(del_ids)} row(s) failed: {e}"
            )
            return 0
        _logger.info(f"FTPSiteManager: pruned {len(del_ids)} stale row(s)")
        return len(del_ids)

    # -- relation maintenance ------------------------------------------

    def on_documents_changed(self, records):
        """
        Public hook: call after Document rows are written. `records` is an
        iterable of the written record dicts (each with at least "url").

        Does no SFTP or network work - it only invalidates state so the next
        heartbeat's match-check pass re-evaluates the affected rows:
          - clears each changed Document's content_fp (forces a re-fingerprint,
            which also catches a file that was deleted or moved on the wiki),
          - clears match_checked on every FTP row currently linked to a changed
            Document (so a now-wrong link is dropped) and on every FTP row whose
            size equals a changed Document's byte_size (so a new match is found).
        """
        if self._db is None:
            return
        records = [r for r in records if r.get("url")]
        if not records:
            return

        doc_fp_clear = [{"url": r["url"], "content_fp": None} for r in records]
        sizes = {int(r["byte_size"]) for r in records if r.get("byte_size")}

        recheck_paths = set()
        id_map = self._db.lookup(_DOC_TABLE, {r["url"] for r in records})
        for doc_id in id_map.values():
            if not doc_id:
                continue
            linked = self._db.get_links(_DOC_TABLE, _DOC_LINK_FIELD, doc_id)
            for row in self._db.read(_FTP_TABLE, list(linked), fields=["path"]):
                if row.get("path"):
                    recheck_paths.add(row["path"])
        for size in sizes:
            for row in self._db.scan_all(
                _FTP_TABLE, where=("size", "eq", size), fields=["path"]
            ):
                recheck_paths.add(row["path"])

        try:
            self._db.write(_DOC_TABLE, doc_fp_clear)
            if recheck_paths:
                self._db.write(
                    _FTP_TABLE,
                    [{"path": p, "match_checked": False} for p in recheck_paths],
                )
        except Exception as e:
            _logger.warning(f"FTPSiteManager: on_documents_changed write failed: {e}")
            return
        _logger.info(
            f"FTPSiteManager: doc change - {len(records)} doc(s), "
            f"{len(recheck_paths)} FTP row(s) queued for match check"
        )

    def _drain_match_queue(self):
        """Match-check a slice of the BD:Need Match Check backlog."""
        if self._db is None or self._link_batch <= 0:   # 0 disables relation upkeep
            return
        try:
            sftp = self._ensure_connection()
        except Exception as e:
            _logger.warning(f"FTPSiteManager: match-check SFTP connect failed: {e}")
            return
        try:
            rows, _ = self._db.scan(
                _FTP_TABLE, view_name=_FTP_MATCH_VIEW, limit=self._link_batch
            )
        except Exception as e:
            _logger.warning(f"FTPSiteManager: match-check scan failed: {e}")
            return
        if not rows:
            return

        started = time.monotonic()
        done = added = removed = matched = conflicts = no_peer = deferred = 0
        reconnects = 0
        wiki_down_streak = 0               # consecutive rows whose wiki fp failed
        pending = []                       # {path, match_checked[, content_fp]} to persist

        def flush():
            if not pending:
                return
            try:
                self._db.write(_FTP_TABLE, list(pending))
            except Exception as e:
                _logger.warning(f"FTPSiteManager: match_checked write failed: {e}")
            pending.clear()

        for row in rows:
            path = row["path"]
            try:
                a, r, n_cand, n_match, update, complete = self._reconcile_ftp_row(
                    sftp, row)
                added += a
                removed += r
                if complete:
                    wiki_down_streak = 0
                    matched += bool(n_match)
                    conflicts += bool(n_cand and not n_match)
                    no_peer += not n_cand
                    update["match_checked"] = True
                else:
                    # a wiki fingerprint could not be obtained: never mark the
                    # row checked - leave it in the queue and retry it. A doc
                    # with byte_size metadata has a valid wiki title, so this is
                    # expected to clear on its own.
                    wiki_down_streak += n_cand > 0
                    deferred += 1
                if len(update) > 1:            # {path} alone is a no-op write
                    pending.append(update)
                if wiki_down_streak >= 5:
                    _logger.warning(
                        "FTPSiteManager: wiki fingerprint service unresponsive "
                        "(likely rate-limited) - ending match check pass, will "
                        "resume next heartbeat"
                    )
                    break
            except (OSError, paramiko.SSHException, EOFError) as e:
                if self._is_transport_error(e):
                    # dropped socket - persist progress, rebuild, keep going;
                    # this row stays unchecked and is retried on the next pass.
                    self._close_connection()
                    flush()
                    reconnects += 1
                    if reconnects > 8:
                        _logger.warning(
                            "FTPSiteManager: SFTP keeps dropping - ending match "
                            "check pass early"
                        )
                        break
                    _logger.warning(
                        f"FTPSiteManager: SFTP connection lost ({e}) - reconnecting"
                    )
                    try:
                        sftp = self._ensure_connection()
                    except Exception as ce:
                        _logger.warning(f"FTPSiteManager: reconnect failed: {ce}")
                        break
                    continue
                # missing file - mark it done, the sweep will prune the row
                _logger.warning(
                    f"FTPSiteManager: match check skipped {path}: {e}"
                )
                pending.append({"path": path, "match_checked": True})
            except Exception as e:
                # unexpected (e.g. a NocoDB blip) - leave the row unchecked so it
                # is retried, rather than silently marking it done.
                _logger.warning(
                    f"FTPSiteManager: match check error for {path}: {e}"
                )

            done += 1
            if len(pending) >= _MATCH_FLUSH_EVERY:
                flush()
        flush()

        more = " (more queued)" if len(rows) >= self._link_batch else " (queue drained)"
        recon = f", {reconnects} reconnect(s)" if reconnects else ""
        defer = f", {deferred} deferred (wiki fp down)" if deferred else ""
        _logger.info(
            f"FTPSiteManager: match check - {done} row(s) in "
            f"{time.monotonic() - started:.0f}s: {matched} matched, "
            f"{conflicts} size-collision no fp-match, {no_peer} no size peer; "
            f"{added} link(s) added, {removed} removed{defer}{recon}{more}"
        )

    def _reconcile_ftp_row(self, sftp, row):
        """
        Ensure this PDF row's links to Documents reflect content-fingerprint
        equality. Returns (links_added, links_removed, n_candidates, n_matched,
        row_update, complete). row_update is the {path[, content_fp]} record to
        persist; the caller adds match_checked. complete is False when a
        candidate Document's fingerprint could not be fetched this pass - the
        caller must then leave the row unchecked for a retry.
        """
        path, row_id, size = row["path"], row["Id"], row.get("size")
        update = {"path": path}
        complete = True

        docs = []
        if size is not None:
            docs = self._db.scan_all(
                _DOC_TABLE,
                where=("byte_size", "eq", size),
                fields=["url", "byte_size", "content_fp", "Id"],
            )

        # only fingerprint the FTP file when there is something to match it to
        desired = set()
        if docs:
            fp, fresh = self._ensure_ftp_fp(sftp, path, size, row.get("content_fp"))
            if fresh:
                update["content_fp"] = fp
            if fp is not None:
                for doc in docs:
                    try:
                        dfp = self._ensure_doc_fp(doc)
                    except _FingerprintUnavailable as e:
                        complete = False
                        _logger.warning(
                            f"FTPSiteManager: wiki fingerprint unavailable for "
                            f"{doc['url']}: {e}"
                        )
                        continue
                    if dfp is not None and dfp == fp:
                        desired.add(doc["Id"])

        current = set(self._db.get_links(_FTP_TABLE, _FTP_LINK_FIELD, row_id))
        add = sorted(desired - current)
        # never drop a link on incomplete information: a doc that failed to
        # fingerprint this pass would look unmatched when it may still match.
        remove = sorted(current - desired) if complete else []
        if add:
            self._db.create_links(_FTP_TABLE, _FTP_LINK_FIELD, row_id, add)
            _logger.info(f"FTPSiteManager: linked {path} -> {len(add)} document(s)")
        if remove:
            self._db.delete_links(_FTP_TABLE, _FTP_LINK_FIELD, row_id, remove)
            _logger.info(
                f"FTPSiteManager: unlinked {path} from {len(remove)} document(s)"
            )
        if complete and docs and not desired and not current:
            _logger.info(
                f"FTPSiteManager: {path} - {len(docs)} same-size document(s), no "
                f"fingerprint match (source file differs or was replaced upstream)"
            )
        return len(add), len(remove), len(docs), len(desired), update, complete

    def _ensure_ftp_fp(self, sftp, path, size, stored):
        """
        Return (content_fp, fresh) for the FTP file. `fresh` is True when it was
        just computed, so the caller still has to persist it.
        """
        if stored and size is not None and stored.startswith(f"{size}:"):
            return stored, False
        _logger.debug(f"FTPSiteManager: fingerprinting {path} ({size} bytes)")
        return _fp_ftp(sftp, path, size), True

    def _ensure_doc_fp(self, doc):
        """
        Return a Document's content_fp, computing + persisting it if stale.
        Returns None if the URL is not a fingerprintable wiki file; raises
        _FingerprintUnavailable if the file exists but could not be fetched.
        """
        size = doc.get("byte_size")
        stored = doc.get("content_fp")
        if stored and size is not None and stored.startswith(f"{size}:"):
            return stored
        _logger.debug(f"FTPSiteManager: fingerprinting wiki file {doc['url']}")
        fp = _fp_wiki(doc["url"])                 # may raise _FingerprintUnavailable
        if fp is not None:
            try:
                self._db.write(_DOC_TABLE, [{"url": doc["url"], "content_fp": fp}])
            except Exception as e:
                _logger.warning(
                    f"FTPSiteManager: doc content_fp write failed for {doc['url']}: {e}"
                )
        return fp

    # -- directory sync + prune -----------------------------------------

    def _sync_directory(self, sftp, dirpath, known_mtime=None):
        """
        Reconcile one directory with the table. Returns
        (records_to_write, prune_pairs, dirs_skipped) for the caller to batch.

        Fast path: a directory whose stored mtime still matches the server's has
        had no child added, removed, or renamed since we last listed it, so its
        contents are current and the listing is skipped. (A file replaced in
        place without a rename does not bump the directory mtime and is picked up
        on the next full re-list, when its own or a sibling's change triggers
        one.)
        """
        st = sftp.stat(dirpath)
        if known_mtime is not None and _mtime_matches(known_mtime, st.st_mtime):
            _logger.debug(f"FTPSiteManager: {dirpath} unchanged (mtime {known_mtime}) - skipping")
            return [], [], 1

        entries = sftp.listdir_attr(dirpath)

        # snapshot the table's current record of this directory's children
        # BEFORE writing: needed so a dir -> file retype is still visible as
        # "dir" for pruning, and to spot a PDF whose size/mtime changed.
        existing = self._db.scan_all(
            _FTP_TABLE,
            where=("folder", "eq", dirpath),
            fields=["path", "type", "Id", "size", "mtime"],
        )
        existing_by_path = {r["path"]: r for r in existing}

        kept_paths = set()
        kept_types = {}
        records = []
        for entry in entries:
            name = entry.filename
            if name in (".", "..") or name.startswith("."):
                continue
            if stat.S_ISLNK(entry.st_mode):
                continue

            is_dir = stat.S_ISDIR(entry.st_mode)
            suffix = "" if is_dir else PurePosixPath(name).suffix.lstrip(".").lower()
            if not is_dir and suffix not in _KEEP_SUFFIXES:
                continue

            full = _join(dirpath, name)
            kind = "dir" if is_dir else "file"
            kept_paths.add(full)
            kept_types[full] = kind
            record = {
                "path": full,
                "filename": name,
                "folder": dirpath,
                "suffix": suffix,
                "type": kind,
                "size": None if is_dir else int(entry.st_size),
                # a subdirectory's mtime is only recorded once we have actually
                # listed it (below), so a freshly-discovered dir always has a
                # None mtime and is never mistaken for "already listed".
                "mtime": None if is_dir else _utc_mtime(entry.st_mtime),
                "depth": _depth(full),
            }

            prev = existing_by_path.get(full)
            if not is_dir and prev is not None and prev.get("type") == kind:
                if (prev.get("size") == record["size"]
                        and _mtime_matches(prev.get("mtime"), entry.st_mtime)):
                    # unchanged file already on record - skip the redundant
                    # upsert. The re-list was triggered by a sibling's change,
                    # not this file's, and it is already in kept_paths so the
                    # prune pass leaves it alone.
                    continue
                # size or mtime moved: the bytes may have changed, so force a
                # fresh fingerprint and another match check.
                record["content_fp"] = None
                record["match_checked"] = False

            records.append(record)

        # (re)write this directory's own row, notably its fresh mtime, so a
        # one-time change here does not force a re-list on every later sweep.
        # "/" has no parent, so its folder is null and it is never a prune
        # candidate for any directory (which scan by folder == <dirpath>).
        self_record = {
            "path": dirpath,
            "filename": PurePosixPath(dirpath).name,
            "folder": None if dirpath == "/" else _parent(dirpath),
            "suffix": "",
            "type": "dir",
            "size": None,
            "mtime": _utc_mtime(st.st_mtime),
            "depth": _depth(dirpath),
        }
        # put the root's own row first in the create batch (best effort - it is
        # created before its children); for any other directory the row already
        # exists from its parent's listing, so batch order is immaterial.
        write_batch = [self_record] + records if dirpath == "/" else records + [self_record]

        prune_pairs = self._collect_prune(dirpath, existing, kept_paths, kept_types)

        _logger.debug(
            f"FTPSiteManager: {dirpath} - {len(entries)} entries, "
            f"{len(records)} to upsert, {len(prune_pairs)} to prune"
        )
        return write_batch, prune_pairs, 0

    def _collect_prune(self, dirpath, existing, kept_paths, kept_types):
        """
        Reconcile the pre-write snapshot of dirpath's children against a
        successful listing. Called only after listdir_attr() succeeded, so a
        transient listing failure can never contribute a deletion. Returns a
        list of (path, Id) pairs for the caller to delete in one batch.
        """
        to_delete = []
        for row in existing:
            path = row["path"]
            if path not in kept_paths:
                # gone from the server
                if row["type"] == "dir":
                    to_delete.extend(self._subtree_pairs(path))
                to_delete.append((path, row["Id"]))
            elif row["type"] == "dir" and kept_types.get(path) != "dir":
                # still present but changed dir -> file: orphan its old subtree.
                # the row itself is upserted with its new type by the caller.
                to_delete.extend(self._subtree_pairs(path))
        return to_delete

    def _subtree_pairs(self, path):
        """(path, Id) of every descendant of path, deepest first."""
        pairs = []
        for child in self._db.scan_all(
            _FTP_TABLE,
            where=("folder", "eq", path),
            fields=["path", "type", "Id"],
        ):
            if child["type"] == "dir":
                pairs.extend(self._subtree_pairs(child["path"]))
            pairs.append((child["path"], child["Id"]))
        return pairs
