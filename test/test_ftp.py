# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Hermetic unit tests for birddog.ftp.FTPSiteTracker.

The SFTP server and the NocoDB backend are both faked:
  - FakeSFTP serves a mutable directory tree; stat() of a directory reports the
    mtime carried in its parent's listing (as a real server does), and the
    mutation helpers bump that mtime the way adding/removing a child would.
  - FakeDB models the single "FTP Repo" table with cursor/where semantics that
    match birddog.nocodb_database (offset paging, last-page => cursor None) and
    returns datetime values with the '+00:00' suffix NocoDB adds on read.
No network, no paramiko connection.
"""

import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from birddog.abstract_database import ConfigError
from birddog import ftp
from birddog.ftp import (
    FTPSiteTracker, _join, _depth, _parent, _utc_mtime, _mtime_matches,
    _load_ftp_config,
)


# ---------------------------------------------------------------------------
# Fakes

class FakeAttr:
    def __init__(self, name, mode, size, mtime):
        self.filename = name
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


def _mode(kind):
    if kind == "dir":
        return stat.S_IFDIR | 0o755
    if kind == "link":
        return stat.S_IFLNK | 0o777
    return stat.S_IFREG | 0o644


class FakeSFTP:
    """
    tree: { "/abs/dir": [ (name, kind, size, mtime), ... ] }, kind in
    {"dir", "file", "link"}. A directory's own mtime lives in its parent's
    listing tuple; root's lives in self.root_mtime.
    """
    def __init__(self, tree, root_mtime=1):
        self.tree = {k: list(v) for k, v in tree.items()}
        self.root_mtime = root_mtime
        self.calls = []          # listdir_attr paths
        self.stat_calls = []     # stat paths

    def listdir_attr(self, path):
        self.calls.append(path)
        if path not in self.tree:
            raise FileNotFoundError(2, "No such file", path)
        return [FakeAttr(n, _mode(k), s, m) for (n, k, s, m) in self.tree[path]]

    def stat(self, path):
        self.stat_calls.append(path)
        if path == "/":
            return FakeAttr("/", _mode("dir"), None, self.root_mtime)
        parent, _, name = path.rpartition("/")
        parent = parent or "/"
        for (n, k, s, m) in self.tree.get(parent, []):
            if n == name:
                return FakeAttr(name, _mode(k), s, m)
        raise FileNotFoundError(2, "No such file", path)

    def close(self):
        pass

    # -- mutation helpers (each bumps the relevant directory's mtime) --------

    def set_children(self, path, children):
        self.tree[path] = list(children)
        self._bump(path)

    def remove_dir(self, path):
        self.tree.pop(path, None)
        parent, _, name = path.rpartition("/")
        parent = parent or "/"
        self.tree[parent] = [e for e in self.tree.get(parent, []) if e[0] != name]
        self._bump(parent)

    def _bump(self, path):
        if path == "/":
            self.root_mtime += 1000
            return
        parent, _, name = path.rpartition("/")
        parent = parent or "/"
        self.tree[parent] = [
            (n, k, s, m + 1000) if n == name else (n, k, s, m)
            for (n, k, s, m) in self.tree.get(parent, [])
        ]


class FakeDB:
    """Minimal stand-in for NocoDBDatabase, table 'FTP Repo' only."""
    def __init__(self):
        self.rows = {}          # path -> record dict (with Id, _seq)
        self._next_id = 1
        self._seq = 0

    # -- write / delete --
    def write(self, table, records):
        assert table == ftp._FTP_TABLE
        if isinstance(records, dict):
            records = [records]
        ids = []
        for r in records:
            p = r["path"]
            if p in self.rows:
                self.rows[p].update(r)
            else:
                self._seq += 1
                self.rows[p] = {**r, "Id": self._next_id, "_seq": self._seq}
                self._next_id += 1
            ids.append(self.rows[p]["Id"])
        return ids

    def delete(self, table, record_id):
        assert table == ftp._FTP_TABLE
        ids = record_id if isinstance(record_id, (list, tuple)) else [record_id]
        idset = set(ids)
        gone = [p for p, row in self.rows.items() if row["Id"] in idset]
        for p in gone:
            del self.rows[p]
        return len(gone)

    # -- scan --
    def _out(self, r):
        d = dict(r)
        m = d.get("mtime")
        if isinstance(m, str) and m and "+" not in m and not m.endswith("Z"):
            d["mtime"] = m + "+00:00"          # NocoDB adds an offset on read
        return d

    def _filter(self, where):
        rows = list(self.rows.values())
        if where:
            field, op, value = where
            assert op == "eq", op
            rows = [r for r in rows if r.get(field) == value]
        return rows

    def scan(self, table, limit=100, cursor=None, where=None, view_name=None,
             sort=None, fields=None, raw=False):
        assert table == ftp._FTP_TABLE
        rows = self._filter(where)
        if view_name == ftp._FTP_DIR_VIEW:
            rows = [r for r in rows if r["type"] == "dir"]
        rows.sort(key=lambda r: r["_seq"])          # CreatedAt asc
        offset = int(cursor) if cursor else 0
        page = rows[offset:offset + limit]
        is_last = offset + len(page) >= len(rows) or len(page) < limit
        next_cursor = None if is_last else str(offset + len(page))
        return [self._out(r) for r in page], next_cursor

    def scan_all(self, table, where=None, fields=None, sort=None, view_name=None):
        assert table == ftp._FTP_TABLE
        rows = self._filter(where)
        if view_name == ftp._FTP_DIR_VIEW:
            rows = [r for r in rows if r["type"] == "dir"]
        return [self._out(r) for r in rows]


# ---------------------------------------------------------------------------

_TREE = {
    "/": [
        ("Berdichev", "dir", None, 100),
        ("Odessa", "dir", None, 110),
        (".quarantine", "dir", None, 120),          # dot dir: skip + do not traverse
        ("readme.txt", "file", 10, 130),            # non-pdf: skip
        ("top.pdf", "file", 4444, 140),             # kept
    ],
    "/Berdichev": [
        ("1-2-3.pdf", "file", 111, 200),
        ("1-2-4.pdf", "file", 222, 210),
        ("scan.jpg", "file", 999, 220),             # jpg: skip
        ("sub", "dir", None, 230),
        ("weird name (1).pdf", "file", 333, 240),   # special chars in name
    ],
    "/Berdichev/sub": [
        ("9-9-9.pdf", "file", 900, 300),
        ("shortcut", "link", None, 310),            # symlink: skip
    ],
    "/Odessa": [
        ("a.pdf", "file", 1, 400),
    ],
}

_ALL_PDF_PATHS = {
    "/top.pdf",
    "/Berdichev/1-2-3.pdf",
    "/Berdichev/1-2-4.pdf",
    "/Berdichev/weird name (1).pdf",
    "/Berdichev/sub/9-9-9.pdf",
    "/Odessa/a.pdf",
}
_ALL_DIR_PATHS = {"/Berdichev", "/Odessa", "/Berdichev/sub"}


def _make_tracker(db, tree):
    """Construct an FTPSiteTracker with config + connection stubbed out."""
    cfg = {
        "host": "h", "user": "u", "password": "pw-resolved", "port": 22,
        "heartbeat_interval": 60, "scan_batch": 50,
    }
    with patch.object(ftp, "_load_ftp_config", return_value=cfg):
        tracker = FTPSiteTracker(db)
    tracker._sftp = FakeSFTP(tree)      # _ensure_connection() returns this as-is
    return tracker


def _snapshot(db):
    return {p: (r["type"], r.get("size"), r.get("mtime")) for p, r in db.rows.items()}


def _run_until_stable(tracker, patience=4, max_beats=400):
    """Run heartbeats until the table stops changing for `patience` beats."""
    last = None
    stable = 0
    for _ in range(max_beats):
        tracker.heartbeat()
        snap = _snapshot(tracker._db)
        if snap == last:
            stable += 1
            if stable >= patience:
                return
        else:
            stable = 0
            last = snap
    raise AssertionError("inventory did not stabilize")


# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def _write_cfg(self, body):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump(body, f)
        self.addCleanup(os.unlink, path)
        return path

    def test_missing_key(self):
        path = self._write_cfg({"host": "h", "user": "u", "password": "X"})
        with self.assertRaises(ConfigError):
            _load_ftp_config(path)

    def test_missing_env_var(self):
        path = self._write_cfg({"host": "h", "user": "u", "password": "NOPE_NOT_SET", "port": 22})
        os.environ.pop("NOPE_NOT_SET", None)
        with self.assertRaises(ConfigError):
            _load_ftp_config(path)

    def test_resolves_password_and_defaults(self):
        path = self._write_cfg({"host": "h", "user": "u", "password": "FTP_TEST_PW", "port": 22})
        with patch.dict(os.environ, {"FTP_TEST_PW": "sekret"}):
            cfg = _load_ftp_config(path)
        self.assertEqual(cfg["password"], "sekret")
        self.assertEqual(cfg["heartbeat_interval"], ftp._FTP_DEFAULT_INTERVAL)
        self.assertEqual(cfg["scan_batch"], ftp._FTP_DEFAULT_SCAN_BATCH)

    def test_config_overrides(self):
        path = self._write_cfg({
            "host": "h", "user": "u", "password": "FTP_TEST_PW", "port": 22,
            "heartbeat_interval": 15, "scan_batch": 7,
        })
        with patch.dict(os.environ, {"FTP_TEST_PW": "s"}):
            cfg = _load_ftp_config(path)
        self.assertEqual(cfg["heartbeat_interval"], 15)
        self.assertEqual(cfg["scan_batch"], 7)


class TestHelpers(unittest.TestCase):
    def test_join(self):
        self.assertEqual(_join("/", "A"), "/A")
        self.assertEqual(_join("/A", "b.pdf"), "/A/b.pdf")
        self.assertEqual(_join("/A/", "b.pdf"), "/A/b.pdf")

    def test_depth(self):
        self.assertEqual(_depth("/A"), 0)
        self.assertEqual(_depth("/A/b.pdf"), 1)
        self.assertEqual(_depth("/A/B/c.pdf"), 2)

    def test_parent(self):
        self.assertEqual(_parent("/A"), "/")
        self.assertEqual(_parent("/A/B"), "/A")

    def test_utc_mtime(self):
        self.assertEqual(_utc_mtime(0), "1970-01-01 00:00:00")
        self.assertIsNone(_utc_mtime(None))

    def test_mtime_matches(self):
        # stored value as NocoDB returns it, fresh epoch from SFTP
        self.assertTrue(_mtime_matches("1970-01-01 00:01:40+00:00", 100))
        self.assertTrue(_mtime_matches("1970-01-01 00:01:40", 100))       # offset-free
        self.assertFalse(_mtime_matches("1970-01-01 00:01:40+00:00", 101))
        self.assertFalse(_mtime_matches(None, 100))
        self.assertFalse(_mtime_matches("1970-01-01 00:01:40+00:00", None))
        self.assertFalse(_mtime_matches("not-a-date", 100))


class TestBootstrap(unittest.TestCase):
    def test_empty_view_lists_root(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        tracker.heartbeat()
        self.assertEqual(tracker._sftp.calls, ["/"])
        self.assertEqual(set(db.rows), {"/", "/Berdichev", "/Odessa", "/top.pdf"})
        self.assertEqual(db.rows["/top.pdf"]["type"], "file")
        self.assertEqual(db.rows["/top.pdf"]["size"], 4444)
        self.assertEqual(db.rows["/Berdichev"]["type"], "dir")
        self.assertIsNone(db.rows["/Berdichev"]["size"])
        self.assertEqual(db.rows["/Berdichev"]["depth"], 0)

        root = db.rows["/"]
        self.assertEqual(root["type"], "dir")
        self.assertEqual(root["filename"], "")
        self.assertIsNone(root["folder"])
        self.assertEqual(root["depth"], 0)
        self.assertIsNotNone(root["mtime"])
        self.assertEqual(min(r["_seq"] for r in db.rows.values()), root["_seq"])

    def test_dot_directory_never_traversed(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)
        self.assertNotIn("/.quarantine", tracker._sftp.calls)
        self.assertNotIn("/.quarantine", db.rows)


class TestFullWalk(unittest.TestCase):
    def test_converges_to_full_pdf_inventory(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)

        files = {p for p, r in db.rows.items() if r["type"] == "file"}
        dirs = {p for p, r in db.rows.items() if r["type"] == "dir"}
        self.assertEqual(files, _ALL_PDF_PATHS)
        self.assertEqual(dirs, _ALL_DIR_PATHS | {"/"})

        # every directory (root + nested) was discovered and listed
        self.assertTrue((_ALL_DIR_PATHS | {"/"}).issubset(set(tracker._sftp.calls)))

        before = _snapshot(db)
        _run_until_stable(tracker)
        self.assertEqual(_snapshot(db), before)          # a second sweep is stable

    def test_small_batch_still_covers_tree(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        tracker._scan_batch = 1                          # force multi-page sweeps
        _run_until_stable(tracker)
        files = {p for p, r in db.rows.items() if r["type"] == "file"}
        self.assertEqual(files, _ALL_PDF_PATHS)

    def test_fields_populate(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)
        row = db.rows["/Berdichev/1-2-3.pdf"]
        self.assertEqual(row["filename"], "1-2-3.pdf")
        self.assertEqual(row["folder"], "/Berdichev")
        self.assertEqual(row["suffix"], "pdf")
        self.assertEqual(row["depth"], 1)
        self.assertEqual(row["mtime"], _utc_mtime(200))


class TestIncremental(unittest.TestCase):
    def test_unchanged_dirs_are_not_relisted(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)

        tracker._sftp.calls.clear()
        tracker._sftp.stat_calls.clear()
        _run_until_stable(tracker)                       # another full sweep

        self.assertEqual(tracker._sftp.calls, [])        # nothing re-listed
        self.assertTrue((_ALL_DIR_PATHS | {"/"}).issubset(tracker._sftp.stat_calls))

    def test_changed_dir_is_relisted_and_new_pdf_added(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)

        tracker._sftp.set_children("/Odessa", [
            ("a.pdf", "file", 1, 400),
            ("b.pdf", "file", 2, 500),
        ])
        _run_until_stable(tracker)
        self.assertIn("/Odessa/b.pdf", db.rows)
        self.assertIn("/Odessa", tracker._sftp.calls)

        # and it settles back to skipping /Odessa
        tracker._sftp.calls.clear()
        _run_until_stable(tracker)
        self.assertNotIn("/Odessa", tracker._sftp.calls)

    def test_root_relisted_only_when_it_changes(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)

        tracker._sftp.calls.clear()
        _run_until_stable(tracker)
        self.assertNotIn("/", tracker._sftp.calls)       # root skipped when unchanged

        tracker._sftp.set_children("/", _TREE["/"] + [("Lviv", "dir", None, 900)])
        _run_until_stable(tracker)
        self.assertIn("/", tracker._sftp.calls)
        self.assertIn("/Lviv", db.rows)

    def test_file_replaced_without_rename_is_missed_until_relist(self):
        # documents the known trade-off: an in-place content change that does
        # not bump the directory mtime is not seen until something else in the
        # directory changes.
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)

        # a.pdf grows but /Odessa's mtime is left untouched
        tracker._sftp.tree["/Odessa"] = [("a.pdf", "file", 9999, 400)]
        _run_until_stable(tracker)
        self.assertEqual(db.rows["/Odessa/a.pdf"]["size"], 1)     # stale, as expected

        # now a sibling is added -> mtime bumps -> the whole dir is re-read
        tracker._sftp.set_children("/Odessa", [
            ("a.pdf", "file", 9999, 400),
            ("b.pdf", "file", 2, 500),
        ])
        _run_until_stable(tracker)
        self.assertEqual(db.rows["/Odessa/a.pdf"]["size"], 9999)


class TestPrune(unittest.TestCase):
    def _seed_full(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)
        _run_until_stable(tracker)
        return db, tracker

    def test_prune_stale_file(self):
        db, tracker = self._seed_full()
        tracker._sftp.set_children("/Odessa", [])        # a.pdf gone, mtime bumped
        _run_until_stable(tracker)
        self.assertNotIn("/Odessa/a.pdf", db.rows)
        self.assertIn("/Odessa", db.rows)

    def test_prune_stale_subtree(self):
        db, tracker = self._seed_full()
        tracker._sftp.remove_dir("/Berdichev/sub")
        _run_until_stable(tracker)
        self.assertNotIn("/Berdichev/sub", db.rows)
        self.assertNotIn("/Berdichev/sub/9-9-9.pdf", db.rows)
        self.assertIn("/Berdichev/1-2-3.pdf", db.rows)

    def test_prune_dir_retyped_to_plain_file(self):
        db, tracker = self._seed_full()
        keep = [e for e in tracker._sftp.tree["/Berdichev"] if e[0] != "sub"]
        tracker._sftp.set_children("/Berdichev", keep + [("sub", "file", 5, 999)])
        tracker._sftp.tree.pop("/Berdichev/sub", None)
        _run_until_stable(tracker)
        self.assertNotIn("/Berdichev/sub", db.rows)              # not a pdf -> dropped
        self.assertNotIn("/Berdichev/sub/9-9-9.pdf", db.rows)

    def test_prune_dir_retyped_to_kept_file_same_path(self):
        tree = {
            "/": [("data.pdf", "dir", None, 10)],
            "/data.pdf": [("inner.pdf", "file", 7, 20)],
        }
        db = FakeDB()
        tracker = _make_tracker(db, tree)
        _run_until_stable(tracker)
        self.assertEqual(db.rows["/data.pdf"]["type"], "dir")
        self.assertIn("/data.pdf/inner.pdf", db.rows)

        tracker._sftp.set_children("/", [("data.pdf", "file", 42, 30)])
        tracker._sftp.tree.pop("/data.pdf", None)
        _run_until_stable(tracker)
        self.assertEqual(db.rows["/data.pdf"]["type"], "file")
        self.assertEqual(db.rows["/data.pdf"]["size"], 42)
        self.assertNotIn("/data.pdf/inner.pdf", db.rows)

    def test_transient_listing_failure_does_not_prune(self):
        db, tracker = self._seed_full()
        n_before = len(db.rows)

        real_listdir = tracker._sftp.listdir_attr

        def flaky(path):
            if path == "/Odessa":
                raise FileNotFoundError(2, "No such file", path)
            return real_listdir(path)

        tracker._sftp.listdir_attr = flaky
        tracker._sftp._bump("/Odessa")                   # force a listing attempt
        _run_until_stable(tracker)

        self.assertIn("/Odessa/a.pdf", db.rows)
        self.assertEqual(len(db.rows), n_before)


class TestConnectionErrors(unittest.TestCase):
    def test_transport_error_closes_connection(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)

        def boom(path):
            raise __import__("paramiko").SSHException("channel closed")

        tracker._sftp.stat = boom
        tracker.heartbeat()
        self.assertIsNone(tracker._sftp)
        self.assertIsNone(tracker._client)

    def test_scan_failure_is_swallowed(self):
        db = FakeDB()
        tracker = _make_tracker(db, _TREE)

        def boom(*a, **k):
            raise RuntimeError("db down")

        tracker._db.scan = boom
        tracker.heartbeat()          # must not raise

    def test_none_db_is_noop(self):
        tracker = _make_tracker(FakeDB(), _TREE)
        tracker._db = None
        tracker.heartbeat()


if __name__ == "__main__":
    unittest.main()
