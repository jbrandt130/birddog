import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utility"))

import clear_table  # noqa: E402
from birddog.abstract_database import InvalidTableName  # noqa: E402


class _FakeDB:
    def __init__(self, tables):
        self.tables = {name: list(range(1, n + 1)) for name, n in tables.items()}
        self.deleted = []

    def count(self, table):
        if table not in self.tables:
            raise InvalidTableName(table)
        return len(self.tables[table])

    def scan(self, table, limit, fields):
        return [{"Id": i} for i in self.tables[table][:limit]], None

    def delete(self, table, ids):
        self.deleted.append(table)
        self.tables[table] = [i for i in self.tables[table] if i not in set(ids)]


class TestClearTables(unittest.TestCase):
    def _main(self, db, argv, answer="LOCAL"):
        with mock.patch.object(clear_table, "connect", return_value=db), \
                mock.patch("builtins.input", return_value=answer), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return clear_table.main(argv)

    def test_clears_several_tables_in_order(self):
        db = _FakeDB({"Pages": 3, "Documents": 2, "Locations": 0})
        self.assertEqual(self._main(db, ["--local", "Pages", "Documents", "Locations"]), 0)
        self.assertEqual(db.tables, {"Pages": [], "Documents": [], "Locations": []})
        self.assertEqual(db.deleted, ["Pages", "Documents"])

    def test_unknown_table_aborts_before_deleting(self):
        db = _FakeDB({"Pages": 3})
        self.assertEqual(self._main(db, ["--local", "Pages", "Nope"]), 1)
        self.assertEqual(db.tables["Pages"], [1, 2, 3])

    def test_wrong_confirmation_deletes_nothing(self):
        db = _FakeDB({"Pages": 3, "Documents": 2})
        self.assertEqual(self._main(db, ["--local", "Pages", "Documents"], answer="no"), 1)
        self.assertEqual(db.deleted, [])

    def test_single_table_still_works(self):
        db = _FakeDB({"Documents": 2})
        self.assertEqual(self._main(db, ["--local", "Documents"]), 0)
        self.assertEqual(db.tables["Documents"], [])


if __name__ == "__main__":
    unittest.main()
