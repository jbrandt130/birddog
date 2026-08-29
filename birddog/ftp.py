# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
FTPSiteTracker - background inventory builder for the targeted SFTP repo.

On each heartbeat the tracker pulls a batch of directory records from the
"Directories" view of the "FTP Repo" table (type == dir, sorted on CreatedAt),
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

The reference walk lives in notebooks/sftp.ipynb.
"""

import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath

import paramiko

from birddog.abstract_database import ConfigError
from birddog.utility import HeartbeatManager
from birddog.log import get_logger

_logger = get_logger()

_FTP_CONFIG_PATH        = "resources/ftp_config.json"
_FTP_TABLE              = "FTP Repo"
_FTP_DIR_VIEW           = "Directories"
_KEEP_SUFFIXES          = frozenset({"pdf"})

_FTP_DEFAULT_INTERVAL   = 60      # seconds between heartbeats
_FTP_DEFAULT_SCAN_BATCH = 50      # directories processed per heartbeat
_FTP_CONNECT_TIMEOUT    = 30      # seconds
_FTP_KEEPALIVE          = 30      # seconds; keeps an idle SFTP channel open

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

class FTPSiteTracker(HeartbeatManager):
    def __init__(self, db, config_path=_FTP_CONFIG_PATH):
        self._db = db
        self._config = _load_ftp_config(config_path)
        self._scan_batch = int(self._config["scan_batch"])

        # opaque paging token into the "Directories" view; None means "start at
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

        _logger.info(
            f"FTPSiteTracker: host={self._config['host']} "
            f"interval={self._config['heartbeat_interval']}s batch={self._scan_batch}"
        )
        super().__init__(interval=float(self._config["heartbeat_interval"]))

    # -- SFTP connection ----------------------------------------------------

    def _connect(self):
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

        self._client = client
        self._sftp = client.open_sftp()
        _logger.info(f"FTPSiteTracker: connected to {self._config['host']}")

    def _ensure_connection(self):
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
        True if exc means the SFTP channel is gone (reconnect needed), False for
        a per-path error such as a directory that vanished between the table
        scan and the listing (paramiko surfaces those as FileNotFoundError).
        """
        if isinstance(exc, (paramiko.SSHException, EOFError)):
            return True
        if isinstance(exc, FileNotFoundError):
            return False
        if isinstance(exc, OSError):
            # broken pipe / connection reset / timeout, or a dead transport
            transport = self._client.get_transport() if self._client else None
            return transport is None or not transport.is_active()
        return False

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
            _logger.warning(f"FTPSiteTracker: SFTP connect failed: {e}")
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
            _logger.warning(f"FTPSiteTracker: directory scan failed: {e}")
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
                _logger.warning(f"FTPSiteTracker: sync failed for {dirpath}: {e}")
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
            f"FTPSiteTracker: heartbeat - {beat_dirs}/{len(dirs)} dir(s), "
            f"{beat_skipped} unchanged, {beat_written} row(s) upserted, "
            f"{beat_pruned} pruned"
        )

        if self._cursor is None:
            self._end_sweep()

    def _begin_sweep(self):
        self._sweep_started_at = time.monotonic()
        self._sweep_dirs = 0
        self._sweep_written = 0
        self._sweep_pruned = 0
        self._sweep_skipped = 0
        _logger.info("FTPSiteTracker: starting inventory sweep")

    def _end_sweep(self):
        elapsed = 0.0
        if self._sweep_started_at is not None:
            elapsed = time.monotonic() - self._sweep_started_at
        _logger.info(
            f"FTPSiteTracker: sweep complete - {self._sweep_dirs} directories "
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
                f"FTPSiteTracker: batch write of {len(records)} row(s) failed: {e}"
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
                f"FTPSiteTracker: batch prune of {len(del_ids)} row(s) failed: {e}"
            )
            return 0
        _logger.info(f"FTPSiteTracker: pruned {len(del_ids)} stale row(s)")
        return len(del_ids)

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
            _logger.debug(f"FTPSiteTracker: {dirpath} unchanged (mtime {known_mtime}) - skipping")
            return [], [], 1

        entries = sftp.listdir_attr(dirpath)

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
            records.append({
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
            })

        # snapshot the table's current record of this directory's children
        # BEFORE writing, so a dir -> file retype is still visible as "dir".
        existing = self._db.scan_all(
            _FTP_TABLE,
            where=("folder", "eq", dirpath),
            fields=["path", "type", "Id"],
        )

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
            f"FTPSiteTracker: {dirpath} - {len(entries)} entries, "
            f"{len(records)} kept, {len(prune_pairs)} to prune"
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
