import unittest
from datetime import date

from birddog.abstract_database import And, Or
from birddog.documents import (
    BLANK,
    IMPLICIT_FILTERS,
    DocumentAccessError,
    DocumentError,
    allowed_actions,
    append_comment,
    can_curate,
    can_see,
    comment_stamp,
    label_prefix_filter,
    list_filter,
    plan_update,
    sees_anomalies,
    sort_spec,
    visible_codes,
)

CHECK = ["doc_check"]
REVIEW = ["doc_review"]
ESCALATE = ["doc_escalate"]
ADMIN = ["doc_admin"]

def doc(code=None, anomaly=False, comments=None):
    return {"url": "u", "process_code": code, "anomaly": anomaly, "comments": comments}

class VisibilityTest(unittest.TestCase):

    def test_visible_codes(self):
        self.assertEqual(visible_codes(CHECK), ["check"])
        self.assertEqual(visible_codes(REVIEW), ["check", "review"])
        self.assertEqual(visible_codes(ESCALATE), ["check", "review", "escalate"])
        self.assertEqual(visible_codes(ADMIN),
                         ["new", "check", "review", "escalate", "reject", "defer", "save", BLANK])
        self.assertNotIn("duplicate", visible_codes(ADMIN))

    def test_privileges_combine_and_ignore_others(self):
        self.assertEqual(visible_codes(["doc_check", "doc_review", "site_admin"]), ["check", "review"])
        self.assertEqual(visible_codes(["site_admin"]), [])
        self.assertFalse(can_curate(["site_admin"]))
        self.assertFalse(can_curate([]))
        self.assertFalse(can_curate(None))

    def test_anomalies(self):
        self.assertFalse(sees_anomalies(CHECK + REVIEW))
        self.assertTrue(sees_anomalies(ESCALATE))
        self.assertTrue(sees_anomalies(ADMIN))

    def test_can_see(self):
        self.assertTrue(can_see(doc("check"), CHECK))
        self.assertFalse(can_see(doc("check", anomaly=True), CHECK))
        self.assertTrue(can_see(doc("check", anomaly=True), ESCALATE))
        self.assertFalse(can_see(doc("review"), CHECK))
        self.assertTrue(can_see(doc(None), ADMIN))
        self.assertTrue(can_see(doc(""), ADMIN))
        self.assertFalse(can_see(doc("duplicate"), ADMIN))

class ListFilterTest(unittest.TestCase):

    def test_checker(self):
        self.assertEqual(list_filter(CHECK),
                         And(Or(("process_code", "eq", "check")), *IMPLICIT_FILTERS, ("anomaly", "eq", False)))

    def test_admin_all_codes_no_anomaly_condition(self):
        f = list_filter(ADMIN)
        self.assertEqual(f.conditions[1:], IMPLICIT_FILTERS)
        self.assertIn(("process_code", "blank", None), f.conditions[0].conditions)

    def test_chosen_code_prefix_and_anomalies(self):
        self.assertEqual(
            list_filter(ESCALATE, process_code="review", label_prefix="DAHMO/5/1/", anomalies_only=True),
            And(Or(("process_code", "eq", "review")), *IMPLICIT_FILTERS, ("anomaly", "eq", True),
                Or(("label", "eq", "DAHMO/5/1"), ("label", "startswith", "DAHMO/5/1/"))))
        self.assertEqual(list_filter(ADMIN, process_code=BLANK),
                         And(Or(("process_code", "blank", None)), *IMPLICIT_FILTERS))

    def test_refusals(self):
        with self.assertRaises(DocumentAccessError):
            list_filter([])
        with self.assertRaises(DocumentAccessError):
            list_filter(CHECK, process_code="review")
        with self.assertRaises(DocumentAccessError):
            list_filter(ADMIN, process_code="duplicate")
        with self.assertRaises(DocumentAccessError):
            list_filter(REVIEW, anomalies_only=True)

    def test_label_prefix(self):
        self.assertIsNone(label_prefix_filter(" / "))
        self.assertIsNone(label_prefix_filter(None))

    def test_sort(self):
        self.assertEqual(sort_spec(), [("seq_label", True), ("Id", True)])
        self.assertEqual(sort_spec("last_updated", False), [("UpdatedAt", False), ("Id", True)])
        with self.assertRaises(DocumentError):
            sort_spec("url")

class ActionsTest(unittest.TestCase):

    def test_allowed_actions(self):
        self.assertEqual(allowed_actions(doc(None)), ["check", "reject", "defer", "save", "anomaly"])
        self.assertEqual(allowed_actions(doc("new")), ["check", "reject", "defer", "save", "anomaly"])
        self.assertEqual(allowed_actions(doc("check")), ["review", "reject", "defer", "save", "anomaly"])
        self.assertEqual(allowed_actions(doc("review")), ["escalate", "reject", "defer", "save", "anomaly"])
        self.assertEqual(allowed_actions(doc("escalate")), ["reject", "defer", "save", "anomaly"])
        self.assertEqual(allowed_actions(doc("reject")), ["check", "defer", "save", "anomaly"])
        self.assertEqual(allowed_actions(doc("defer")), ["check", "reject", "save", "anomaly"])
        self.assertEqual(allowed_actions(doc("save")), ["reject", "defer", "anomaly"])
        self.assertEqual(allowed_actions(doc("check", anomaly=True))[-1], "clear_anomaly")

class CommentTest(unittest.TestCase):

    def test_append(self):
        self.assertEqual(append_comment(None, " a "), "a")
        self.assertEqual(append_comment("a", "b"), "a\nb")
        self.assertEqual(append_comment("a", "  "), "a")
        self.assertEqual(append_comment(None, None), "")

    def test_stamp(self):
        self.assertEqual(comment_stamp("Jon Brandt", date(2026, 11, 21), "check", "review"),
                         "[2026-11-21 Jon Brandt] check -> review: ")
        self.assertEqual(comment_stamp("", "2026-11-21T10:00:00", None, "check"),
                         "[2026-11-21] (blank) -> check: ")
        self.assertEqual(comment_stamp("Jo", date(2026, 11, 21)), "[2026-11-21 Jo]: ")

class PlanUpdateTest(unittest.TestCase):

    def test_forward_with_comment(self):
        self.assertEqual(plan_update(doc("check", comments="old"), CHECK, action="review", comment="ok"),
                         {"process_code": "review", "comments": "old\nok"})

    def test_save_sets_pending_and_status(self):
        self.assertEqual(plan_update(doc("check"), CHECK, action="save"),
                         {"process_code": "save", "save_pending": True, "save_status": "externally_indexed"})
        self.assertEqual(plan_update(doc("check"), CHECK, action="save", extract=False)["save_status"],
                         "not_indexed")

    def test_anomaly_flag_and_clear(self):
        self.assertEqual(plan_update(doc("check"), CHECK, action="anomaly", anomaly_comment=" bad scan "),
                         {"anomaly": True, "anomaly_comment": "bad scan"})
        with self.assertRaises(DocumentError):
            plan_update(doc("check"), CHECK, action="anomaly", anomaly_comment=" ")
        self.assertEqual(plan_update(doc("review", anomaly=True), ESCALATE, action="clear_anomaly"),
                         {"anomaly": False, "anomaly_comment": ""})

    def test_curated_only(self):
        self.assertEqual(plan_update(doc("check"), CHECK, curated={"doc_type": ["Vitals"], "filename": "5-1-4"}),
                         {"doc_type": ["Vitals"], "filename": "5-1-4"})
        self.assertEqual(plan_update(doc("check"), CHECK, curated={"language": None}), {"language": []})
        self.assertEqual(plan_update(doc("check"), CHECK), {})

    def test_refusals(self):
        cases = [
            (doc("review"), CHECK, {}, DocumentAccessError),                       # not visible
            (doc("check", anomaly=True), REVIEW, {}, DocumentAccessError),         # anomaly hidden
            (doc("check"), [], {}, DocumentAccessError),                           # deactivated
            (doc("check"), CHECK, {"action": "escalate"}, DocumentError),          # wrong step
            (doc("check"), CHECK, {"action": "check"}, DocumentError),
            (doc("check"), CHECK, {"action": "clear_anomaly"}, DocumentError),     # not flagged
            (doc("check"), CHECK, {"action": "duplicate"}, DocumentError),         # not an action
            (doc("check"), CHECK, {"curated": {"process_code": "save"}}, DocumentError),
            (doc("check"), CHECK, {"curated": {"comments": "x"}}, DocumentError),  # append only
            (doc("check"), CHECK, {"curated": {"doc_type": "Vitals"}}, DocumentError),
            (doc("check"), CHECK, {"curated": {"filename": 5}}, DocumentError),
            (doc("check"), CHECK, {"curated": ["filename"]}, DocumentError),
            (doc("check"), CHECK, {"comment": 5}, DocumentError),
        ]
        for d, privs, kw, err in cases:
            with self.subTest(doc=d, privs=privs, kw=kw), self.assertRaises(err):
                plan_update(d, privs, **kw)

class FakeDocsDB:
    """Just enough of the Database interface for the /documents operations."""

    def __init__(self, docs):
        self.docs = {d["Id"]: dict(d) for d in docs}
        self.calls = []
        self.locations = {}
        self.pages = {}

    def scan(self, table, limit=100, cursor=None, where=None, sort=None, fields=None):
        self.calls.append(("scan", where, sort, cursor, limit))
        return [self.docs[i] for i in sorted(self.docs)][:limit], None

    def count(self, table, where=None):
        self.calls.append(("count", where))
        return len(self.docs)

    def read(self, table, ids, fields=None):
        store = {"Documents": self.docs, "Locations": self.locations, "Pages": self.pages}[table]
        return [dict(store.get(i, {})) for i in ids]

    def update(self, table, records):
        self.calls.append(("update", records))
        by_url = {d["url"]: d for d in self.docs.values()}
        for r in records:
            by_url[r["url"]].update(r)
        return [by_url[r["url"]]["Id"] for r in records]

    def get_links(self, table, field, record_id):
        return {"locations": [7] if self.locations else [],
                "owning_pages": [9] if self.pages else []}[field]

def rec(record_id, code, anomaly=False, comments=None):
    return {"Id": record_id, "url": f"u{record_id}", "label": f"A/{record_id}",
            "process_code": code, "anomaly": anomaly, "comments": comments,
            "UpdatedAt": None, "CreatedAt": "2026-11-01"}

class OperationsTest(unittest.TestCase):

    def test_list_first_page_counts_later_pages_do_not(self):
        from birddog.documents import list_documents
        db = FakeDocsDB([rec(1, "check"), rec(2, "check")])
        page = list_documents(db, CHECK, sort="last_updated", ascending=False)
        self.assertEqual(page["total"], 2)
        self.assertEqual([d["id"] for d in page["documents"]], [1, 2])
        self.assertEqual(page["documents"][0]["last_updated"], "2026-11-01")
        self.assertEqual(page["documents"][0]["actions"][0], "review")
        self.assertEqual(db.calls[0][2], [("UpdatedAt", False), ("Id", True)])
        db.calls.clear()
        page = list_documents(db, CHECK, cursor="100", limit=1000)
        self.assertIsNone(page["total"])
        self.assertEqual(db.calls, [("scan", list_filter(CHECK), [("seq_label", True), ("Id", True)], "100", 100)])

    def test_update_is_all_or_none(self):
        from birddog.documents import update_documents
        db = FakeDocsDB([rec(1, "check"), rec(2, "review")])
        with self.assertRaises(DocumentAccessError):
            update_documents(db, CHECK, [1, 2], action="review")
        db = FakeDocsDB([rec(1, "check"), rec(2, "save")])
        with self.assertRaises(DocumentError):
            update_documents(db, ADMIN, [1, 2], action="save")
        self.assertFalse([c for c in db.calls if c[0] == "update"])

    def test_update_writes_and_returns_fresh_rows(self):
        from birddog.documents import update_documents
        db = FakeDocsDB([rec(1, "check", comments="old"), rec(2, "check")])
        out = update_documents(db, CHECK, [1, 2, 1], action="review", comment="[stamp] ok")
        self.assertEqual(db.calls[-1], ("update", [
            {"url": "u1", "process_code": "review", "comments": "old\n[stamp] ok"},
            {"url": "u2", "process_code": "review", "comments": "[stamp] ok"}]))
        self.assertEqual([(d["id"], d["process_code"]) for d in out], [(1, "review"), (2, "review")])
        self.assertEqual(out[0]["actions"][0], "escalate")

    def test_update_without_changes_writes_nothing(self):
        from birddog.documents import update_documents
        db = FakeDocsDB([rec(1, "check")])
        update_documents(db, CHECK, [1], comment="  ")
        self.assertFalse([c for c in db.calls if c[0] == "update"])

    def test_bad_ids_and_missing(self):
        from birddog.documents import update_documents, get_document, DocumentNotFound
        db = FakeDocsDB([rec(1, "check")])
        for ids in (None, [], ["1"], [True], 1):
            with self.subTest(ids=ids), self.assertRaises(DocumentError):
                update_documents(db, CHECK, ids, comment="x")
        with self.assertRaises(DocumentNotFound):
            get_document(db, CHECK, 5)

    def test_detail_has_locations_and_owning_pages(self):
        from birddog.documents import get_document
        db = FakeDocsDB([rec(1, "check")])
        db.locations = {7: {"Id": 7, "location_id": "L7", "modern_location_name": "Balta",
                            "region": "Odesa", "c2000_country": "Ukraine"}}
        db.pages = {9: {"Id": 9, "title": "T", "label": "A", "url": "p"}}
        doc = get_document(db, CHECK, 1)
        self.assertEqual(doc["locations"][0]["name"], "Balta")
        self.assertEqual(doc["owning_pages"], [{"title": "T", "label": "A", "url": "p"}])
        with self.assertRaises(DocumentAccessError):
            get_document(FakeDocsDB([rec(1, "save")]), CHECK, 1)

if __name__ == "__main__":
    unittest.main()
