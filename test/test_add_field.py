import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utility"))

import add_field  # noqa: E402


class _FakeDB:
    """Just the surface add_field.plan() and apply() touch."""

    def __init__(self, options, schema_values, in_use=None):
        self.options = list(options)
        self.schema_values = list(schema_values)
        self.in_use = in_use or {}
        self.calls = []
        self._host = "http://nocodb"
        self.schema_type = "single_select"
        self.schema_types = ["bool", "text", "single_select", "multi_select", "date"]

    def _table_id(self, name):
        return f"t_{name}"

    def _table_info_url(self, table_id):
        return f"info/{table_id}"

    def _records_url(self, table_id):
        return f"records/{table_id}"

    def _fetch(self, url, json=None, method="GET"):
        self.calls.append((method, url, json))
        if url == "info/t_Schema":
            return {"columns": [{"id": "ft", "title": "field_type", "column_name": "field_type",
                                 "uidt": "SingleSelect",
                                 "colOptions": {"options": [{"title": t} for t in self.schema_types]}}]}
        if url.endswith("/columns/ft") and method == "GET":
            return {"colOptions": {"options": [{"title": t} for t in self.schema_types]}}
        if url.endswith("/columns/ft") and method == "PATCH":
            self.schema_types = [o["title"] for o in json["colOptions"]["options"]]
            return None
        if url.startswith("info/"):
            return {"columns": [{"id": "col1", "title": "process_code", "column_name": "process_code",
                                 "uidt": "SingleSelect",
                                 "colOptions": {"options": [{"title": o} for o in self.options]}}]}
        if url.endswith("/columns/col1") and method == "GET":
            return {"colOptions": {"options": [{"title": o} for o in self.options]}}
        if url.endswith("/columns/col1") and method == "PATCH":
            self.options = [o["title"] for o in json["colOptions"]["options"]]
        return None

    def scan_all(self, table):
        if table == "Schema":
            return [{"Id": 1, "table_name": "Documents", "field_name": "process_code",
                     "field_type": self.schema_type, "description": "d"}]
        return self.schema_values

    def scan(self, table, limit=100, cursor=None, where=None, fields=None):
        _, _, value = where
        return [{"Id": i} for i in range(self.in_use.get(value, 0))], None


def _spec(values, retired):
    data = {"table": "Documents", "field": "process_code", "type": "single_select",
            "description": "d", "values": [[v, v] for v in values], "retired_values": retired}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f)
    try:
        return add_field.load_spec(f.name)
    finally:
        os.unlink(f.name)


def _schema_values(values):
    return [{"Id": i + 10, "table_name": "Documents", "field_name": "process_code",
             "field_value": v, "description": v} for i, v in enumerate(values)]


def _write_spec(data):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f)
    try:
        return add_field.load_spec(f.name)
    finally:
        os.unlink(f.name)


class TestNewTypes(unittest.TestCase):
    def test_missing_schema_type_is_added_once(self):
        spec, errors = _write_spec({"table": "Documents", "field": "prefs", "type": "json",
                                    "description": "d"})
        self.assertEqual(errors, [])
        db = _FakeDB([], [])
        steps = add_field.plan(db, spec)
        self.assertEqual(steps[0], ("Schema type option", "json"))
        add_field.apply(db, spec, steps[0])
        add_field.apply(db, spec, steps[0])
        self.assertEqual(db.schema_types.count("json"), 1)

    def test_known_schema_type_needs_no_option(self):
        spec, _ = _write_spec({"table": "Documents", "field": "seen", "type": "date",
                               "description": "d"})
        steps = add_field.plan(_FakeDB([], []), spec)
        self.assertNotIn("Schema type option", [k for k, _ in steps])

    def test_default_and_key_field(self):
        spec, errors = _write_spec({"table": "T", "field": "active", "type": "bool",
                                    "description": "d", "default": True, "key_field": False})
        self.assertEqual(errors, [])
        db = _FakeDB([], [])
        add_field.apply(db, spec, ("create column", []))
        add_field.apply(db, spec, ("Schema row", None))
        self.assertEqual(db.calls[0][2]["cdf"], "true")
        self.assertIs(db.calls[1][2][0]["key_field"], False)

    def test_retype_from_changes_schema_row(self):
        spec, errors = _write_spec({"table": "Documents", "field": "process_code", "type": "single_select",
                                    "description": "d", "values": [["check", "check"]]})
        db = _FakeDB(["check"], _schema_values(["check"]))
        db.schema_type = "user"
        with self.assertRaises(ValueError):
            add_field.plan(db, spec)
        spec["retype_from"] = ["user"]
        steps = add_field.plan(db, spec)
        self.assertIn(("Schema type", [1]), steps)
        add_field.apply(db, spec, ("Schema type", [1]))
        self.assertIn(("PATCH", "records/t_Schema", [{"Id": 1, "field_type": "single_select"}]), db.calls)

    def test_default_rejected_for_json(self):
        _, errors = _write_spec({"table": "T", "field": "p", "type": "json",
                                 "description": "d", "default": "{}"})
        self.assertTrue(any("default" in e for e in errors))


class TestRetiredValues(unittest.TestCase):
    def test_plan_and_apply_remove_unused_value(self):
        spec, errors = _spec(["check", "save"], ["postpone"])
        self.assertEqual(errors, [])
        db = _FakeDB(["check", "postpone", "save"], _schema_values(["check", "postpone", "save"]))
        steps = add_field.plan(db, spec)
        self.assertEqual(steps, [("remove options", ["postpone"]),
                                 ("remove Schema Values rows", [(11, "postpone")])])
        for step in steps:
            add_field.apply(db, spec, step)
        self.assertEqual(db.options, ["check", "save"])
        self.assertIn(("DELETE", "records/t_Schema Values", [{"Id": 11}]), db.calls)
        add_field.verify(db, spec)

    def test_value_in_use_stops_the_plan(self):
        spec, _ = _spec(["check"], ["postpone"])
        db = _FakeDB(["check", "postpone"], _schema_values(["check", "postpone"]), in_use={"postpone": 2})
        with self.assertRaises(ValueError):
            add_field.plan(db, spec)

    def test_already_retired_is_a_no_op(self):
        spec, _ = _spec(["check"], ["postpone"])
        db = _FakeDB(["check"], _schema_values(["check"]))
        self.assertEqual(add_field.plan(db, spec), [])

    def test_spec_errors(self):
        _, errors = _spec(["check", "postpone"], ["postpone"])
        self.assertTrue(any("both allowed and retired" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
