import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utility"))

import drop_field  # noqa: E402


class _FakeDB:
    """Just the surface drop_field.plan(), apply() and verify() touch."""

    def __init__(self, columns, schema, schema_values):
        self.columns = dict(columns)
        self.rows = {"Schema": list(schema), "Schema Values": list(schema_values)}
        self.calls = []
        self._host = "http://nocodb"

    def _table_id(self, name):
        return f"t_{name}"

    def _table_info_url(self, table_id):
        return f"info/{table_id}"

    def _records_url(self, table_id):
        return f"records/{table_id}"

    def _fetch(self, url, json=None, method="GET"):
        self.calls.append((method, url, json))
        if url.startswith("info/"):
            return {"columns": [{"title": t, **c} for t, c in self.columns.items()]}
        if method == "DELETE" and "/meta/columns/" in url:
            column_id = url.rsplit("/", 1)[-1]
            self.columns = {t: c for t, c in self.columns.items() if c["id"] != column_id}
        elif method == "DELETE" and url.startswith("records/"):
            table = url[len("records/t_"):]
            ids = {r["Id"] for r in json}
            self.rows[table] = [r for r in self.rows[table] if r["Id"] not in ids]
        return None

    def scan_all(self, table):
        return self.rows[table]


def _db():
    return _FakeDB(
        {"save_pending": {"id": "c1", "uidt": "Checkbox"}, "url": {"id": "c2", "uidt": "SingleLineText"}},
        [{"Id": 1, "table_name": "Documents", "field_name": "save_pending"},
         {"Id": 2, "table_name": "Documents", "field_name": "url"}],
        [{"Id": 5, "table_name": "Documents", "field_name": "save_pending", "field_value": "x"}])


class TestDropField(unittest.TestCase):
    def test_plan_apply_and_verify(self):
        db = _db()
        steps = drop_field.plan(db, "Documents", ["save_pending"])
        self.assertEqual(steps, [("column", "save_pending", "c1"),
                                 ("Schema", "save_pending", [1]),
                                 ("Schema Values", "save_pending", [5])])
        for step in steps:
            drop_field.apply(db, step)
        self.assertEqual(list(db.columns), ["url"])
        self.assertEqual([r["Id"] for r in db.rows["Schema"]], [2])
        drop_field.verify(db, "Documents", ["save_pending"])
        self.assertEqual(drop_field.plan(db, "Documents", ["save_pending"]), [])

    def test_verify_reports_leftovers(self):
        with self.assertRaises(RuntimeError):
            drop_field.verify(_db(), "Documents", ["save_pending"])


if __name__ == "__main__":
    unittest.main()
