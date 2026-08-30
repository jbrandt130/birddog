# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Hermetic unit tests for birddog.ftp.FTPSiteManager.

The SFTP server and the NocoDB backend are both faked:
  - FakeSFTP serves a mutable directory tree; stat() of a directory reports the
    mtime carried in its parent's listing (as a real server does), and the
    mutation helpers bump that mtime the way adding/removing a child would.
  - FakeDB models the single "FTP Repo" table with cursor/where semantics that
    match birddog.nocodb_database (offset paging, last-page => cursor None) and
    returns datetime values with the '+00:00' suffix NocoDB adds on read.
No network, no paramiko connection.
"""

import hashlib
import json
import os
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from birddog.abstract_database import ConfigError
from birddog import ftp
from birddog.ftp import (
    FTPSiteManager, _join, _depth, _parent, _utc_mtime, _mtime_matches,
    _load_ftp_config, _content_fp, _fp_ftp, _fp_wiki, _parse_wiki_page_url,
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
    """Construct an FTPSiteManager with config + connection stubbed out."""
    cfg = {
        "host": "h", "user": "u", "password": "pw-resolved", "port": 22,
        "heartbeat_interval": 60, "scan_batch": 50,
    }
    with patch.object(ftp, "_load_ftp_config", return_value=cfg):
        tracker = FTPSiteManager(db)
    tracker._sftp = FakeSFTP(tree)      # _ensure_connection() returns this as-is
    tracker._connected_at = time.monotonic()
    tracker._link_batch = 0            # these tests exercise the inventory sweep only
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


# ---------------------------------------------------------------------------
# Fingerprint fakes + tests

class _FakeFile:
    def __init__(self, data):
        self._d, self._p = data, 0

    def read(self, n=-1):
        if n is None or n < 0:
            out, self._p = self._d[self._p:], len(self._d)
        else:
            out = self._d[self._p:self._p + n]
            self._p += len(out)
        return out

    def seek(self, off, whence=0):
        self._p = {0: off, 1: self._p + off, 2: len(self._d) + off}[whence]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeFileSFTP:
    def __init__(self, files):
        self.files = files            # path -> bytes

    def open(self, path, mode="rb"):
        return _FakeFile(self.files[path])


def _fake_fetch(imageinfo=None, blob=b""):
    """A stand-in for birddog.fetch.fetch_url serving one wiki file."""
    def _f(url, params=None, headers=None, return_json=False, content=False, **kw):
        if return_json:                                   # imageinfo call
            page = {"imageinfo": [imageinfo]} if imageinfo else {"missing": ""}
            return {"query": {"pages": {"1": page}}}
        spec = (headers or {}).get("Range", "").replace("bytes=", "")
        if spec.startswith("-"):
            return blob[-int(spec[1:]):]
        lo, hi = spec.split("-")
        return blob[int(lo):int(hi) + 1]
    return _f


def _sha(b):
    return hashlib.sha1(b).hexdigest()


class TestFingerprint(unittest.TestCase):
    def test_content_fp_shape(self):
        self.assertEqual(_content_fp(10, b"a", b"b"),
                         f"10:{_sha(b'a')}:{_sha(b'b')}")

    def test_fp_ftp_large_uses_head_and_tail(self):
        n = ftp._FP_CHUNK
        data = bytes(i % 256 for i in range(3 * n))            # > 2*_FP_CHUNK
        sftp = _FakeFileSFTP({"/f.pdf": data})
        fp = _fp_ftp(sftp, "/f.pdf", len(data))
        self.assertEqual(fp, f"{len(data)}:{_sha(data[:n])}:{_sha(data[-n:])}")

    def test_fp_ftp_small_hashes_whole_file_twice(self):
        data = b"small pdf bytes"
        sftp = _FakeFileSFTP({"/s.pdf": data})
        fp = _fp_ftp(sftp, "/s.pdf", len(data))
        self.assertEqual(fp, f"{len(data)}:{_sha(data)}:{_sha(data)}")

    def test_fp_wiki_matches_fp_ftp_for_identical_bytes(self):
        data = bytes((i * 7) % 256 for i in range(500000))
        sftp = _FakeFileSFTP({"/f.pdf": data})
        want = _fp_ftp(sftp, "/f.pdf", len(data))
        fake = _fake_fetch(
            imageinfo={"url": "https://upload.wikimedia.org/x/f.pdf",
                       "size": len(data), "sha1": "irrelevant"},
            blob=data,
        )
        with patch.object(ftp, "fetch_url", fake):
            got = _fp_wiki("https://commons.wikimedia.org/wiki/File:f.pdf")
        self.assertEqual(got, want)

    def test_fp_wiki_none_for_non_wiki_url(self):
        self.assertIsNone(
            _fp_wiki("https://www.szukajwarchiwach.gov.pl/jednostka/123"))

    def test_fp_wiki_none_when_file_missing(self):
        with patch.object(ftp, "fetch_url", _fake_fetch(imageinfo=None)):
            self.assertIsNone(
                _fp_wiki("https://commons.wikimedia.org/wiki/File:gone.pdf"))

    def test_fp_wiki_raises_when_fetch_fails(self):
        def boom(*a, **k):
            raise ftp.FetchUrlFailError("429 too many requests")
        with patch.object(ftp, "fetch_url", boom):
            with self.assertRaises(ftp._FingerprintUnavailable):
                _fp_wiki("https://commons.wikimedia.org/wiki/File:x.pdf")

    def test_parse_wiki_page_url(self):
        self.assertEqual(
            _parse_wiki_page_url("https://commons.wikimedia.org/wiki/File:A_b.pdf"),
            ("commons.wikimedia.org", "File:A_b.pdf"))
        self.assertEqual(                                     # percent-decoding
            _parse_wiki_page_url("https://uk.wikisource.org/wiki/File:A%20b_%281%29.pdf"),
            ("uk.wikisource.org", "File:A b_(1).pdf"))
        self.assertEqual(
            _parse_wiki_page_url("https://www.familysearch.org/ark:/1/2"),
            (None, None))


# ---------------------------------------------------------------------------
# Relation-matching fake DB + tests

class _MatchDB:
    """Models 'FTP Repo' + 'Documents' + the m:m junction, for match tests."""
    def __init__(self):
        self.ftp = {}          # path -> row
        self.docs = {}         # url -> row
        self.links = set()     # (ftp_id, doc_id)
        self._id = 1

    def add_ftp(self, path, size, fp=None, mtime="2020-01-01 00:00:00+00:00"):
        self.ftp[path] = {
            "Id": self._id, "path": path, "size": size, "mtime": mtime,
            "content_fp": fp, "match_checked": False,
            "type": "file", "suffix": "pdf", "folder": "/x",
        }
        self._id += 1
        return self.ftp[path]["Id"]

    def add_doc(self, url, byte_size, fp=None):
        self.docs[url] = {"Id": self._id, "url": url,
                          "byte_size": byte_size, "content_fp": fp}
        self._id += 1
        return self.docs[url]["Id"]

    # -- reads --
    def scan(self, table, limit=100, cursor=None, where=None,
             view_name=None, fields=None, raw=False, sort=None):
        rows = list(self.ftp.values())
        if view_name == ftp._FTP_MATCH_VIEW:
            rows = [r for r in rows if not r["match_checked"]]
        return [dict(r) for r in rows[:limit]], None

    def scan_all(self, table, where=None, fields=None, sort=None, view_name=None):
        src = self.docs if table == ftp._DOC_TABLE else self.ftp
        rows = list(src.values())
        if where:
            f, op, v = where
            assert op == "eq", op
            rows = [r for r in rows if r.get(f) == v]
        return [dict(r) for r in rows]

    def read(self, table, ids, fields=None):
        if isinstance(ids, (str, int)):
            ids = [ids]
        src = self.docs if table == ftp._DOC_TABLE else self.ftp
        by_id = {r["Id"]: r for r in src.values()}
        return [dict(by_id.get(i, {})) for i in ids]

    def lookup(self, table, key_set):
        assert table == ftp._DOC_TABLE
        if isinstance(key_set, str):
            key_set = {key_set}
        return {k: self.docs[k]["Id"] for k in key_set if k in self.docs}

    # -- writes --
    def write(self, table, records):
        if isinstance(records, dict):
            records = [records]
        src = self.docs if table == ftp._DOC_TABLE else self.ftp
        key = "url" if table == ftp._DOC_TABLE else "path"
        for rec in records:
            src[rec[key]].update(rec)
        return [src[rec[key]]["Id"] for rec in records]

    # -- links (symmetric m:m) --
    def get_links(self, table, field, rec_id):
        if table == ftp._FTP_TABLE:
            return sorted(d for (f, d) in self.links if f == rec_id)
        return sorted(f for (f, d) in self.links if d == rec_id)

    def create_links(self, table, field, rec_id, targets):
        for t in targets:
            self.links.add((rec_id, t) if table == ftp._FTP_TABLE else (t, rec_id))

    def delete_links(self, table, field, rec_id, targets):
        for t in targets:
            self.links.discard((rec_id, t) if table == ftp._FTP_TABLE else (t, rec_id))


def _match_tracker(db, files=None):
    cfg = {"host": "h", "user": "u", "password": "pw", "port": 22,
           "heartbeat_interval": 60, "scan_batch": 50, "link_batch": 100}
    with patch.object(ftp, "_load_ftp_config", return_value=cfg):
        mgr = FTPSiteManager(db)
    mgr._sftp = _FakeFileSFTP(files or {})
    mgr._connected_at = time.monotonic()
    return mgr


_BIG = bytes((i * 13) % 256 for i in range(400000))
_BIG_FP = f"{len(_BIG)}:{_sha(_BIG[:ftp._FP_CHUNK])}:{_sha(_BIG[-ftp._FP_CHUNK:])}"


class TestMatching(unittest.TestCase):
    def test_links_doc_with_matching_fingerprint(self):
        db = _MatchDB()
        fid = db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        did = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp="500:h:t")
        _match_tracker(db)._drain_match_queue()
        self.assertIn((fid, did), db.links)
        self.assertTrue(db.ftp["/a/1.pdf"]["match_checked"])

    def test_no_link_when_fingerprint_differs(self):
        db = _MatchDB()
        fid = db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        did = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp="500:OTHER:t")
        _match_tracker(db)._drain_match_queue()
        self.assertEqual(db.links, set())
        self.assertTrue(db.ftp["/a/1.pdf"]["match_checked"])

    def test_one_doc_links_every_byte_identical_ftp_path(self):
        db = _MatchDB()
        f1 = db.add_ftp("/Cherkassy/8-1-1.pdf", 500, fp="500:h:t")
        f2 = db.add_ftp("/Chigirin/8-1-1.pdf", 500, fp="500:h:t")
        did = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp="500:h:t")
        _match_tracker(db)._drain_match_queue()
        self.assertEqual(db.links, {(f1, did), (f2, did)})

    def test_one_ftp_row_links_commons_and_wikisource(self):
        db = _MatchDB()
        fid = db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        d1 = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp="500:h:t")
        d2 = db.add_doc("https://uk.wikisource.org/wiki/File:x.pdf", 500, fp="500:h:t")
        _match_tracker(db)._drain_match_queue()
        self.assertEqual(db.links, {(fid, d1), (fid, d2)})

    def test_computes_ftp_fingerprint_when_missing(self):
        db = _MatchDB()
        fid = db.add_ftp("/a/big.pdf", len(_BIG), fp=None)
        did = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf",
                         len(_BIG), fp=_BIG_FP)
        mgr = _match_tracker(db, files={"/a/big.pdf": _BIG})
        mgr._drain_match_queue()
        self.assertEqual(db.ftp["/a/big.pdf"]["content_fp"], _BIG_FP)
        self.assertIn((fid, did), db.links)

    def test_computes_doc_fingerprint_via_wiki(self):
        db = _MatchDB()
        fid = db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        did = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp=None)
        with patch.object(ftp, "_fp_wiki", return_value="500:h:t"):
            _match_tracker(db)._drain_match_queue()
        self.assertEqual(db.docs[list(db.docs)[0]]["content_fp"], "500:h:t")
        self.assertIn((fid, did), db.links)

    def test_row_stays_unchecked_when_wiki_fp_unavailable(self):
        db = _MatchDB()
        db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp=None)

        def down(url):
            raise ftp._FingerprintUnavailable("rate limited")

        with patch.object(ftp, "_fp_wiki", down):
            _match_tracker(db)._drain_match_queue()

        self.assertFalse(db.ftp["/a/1.pdf"]["match_checked"])   # left for retry
        self.assertEqual(db.links, set())                       # no conclusion drawn

    def test_link_kept_when_a_candidate_cannot_be_fingerprinted(self):
        db = _MatchDB()
        fid = db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        d1 = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp="500:h:t")
        d2 = db.add_doc("https://uk.wikisource.org/wiki/File:x.pdf", 500, fp=None)
        db.links.add((fid, d1))                     # existing good link

        def flaky(url):
            if "wikisource" in url:
                raise ftp._FingerprintUnavailable("down")
            return "500:h:t"

        with patch.object(ftp, "_fp_wiki", flaky):
            _match_tracker(db)._drain_match_queue()

        self.assertIn((fid, d1), db.links)          # not dropped on incomplete info
        self.assertFalse(db.ftp["/a/1.pdf"]["match_checked"])

    def test_stale_link_removed_when_fp_no_longer_matches(self):
        db = _MatchDB()
        fid = db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        did = db.add_doc("https://commons.wikimedia.org/wiki/File:x.pdf", 500, fp="500:NOW:different")
        db.links.add((fid, did))
        _match_tracker(db)._drain_match_queue()
        self.assertEqual(db.links, set())

    def test_on_documents_changed_requeues_rows(self):
        db = _MatchDB()
        url = "https://commons.wikimedia.org/wiki/File:x.pdf"
        fid_same = db.add_ftp("/a/1.pdf", 500, fp="500:h:t")
        fid_old = db.add_ftp("/a/2.pdf", 999, fp="999:h:t")
        did = db.add_doc(url, 500, fp="500:h:t")
        db.ftp["/a/1.pdf"]["match_checked"] = True
        db.ftp["/a/2.pdf"]["match_checked"] = True
        db.links.add((fid_old, did))          # a stale link from before a size change

        _match_tracker(db).on_documents_changed([{"url": url, "byte_size": 500}])

        self.assertIsNone(db.docs[url]["content_fp"])          # forced re-fingerprint
        self.assertFalse(db.ftp["/a/1.pdf"]["match_checked"])  # same-size candidate
        self.assertFalse(db.ftp["/a/2.pdf"]["match_checked"])  # currently-linked (old size)


if __name__ == "__main__":
    unittest.main()
