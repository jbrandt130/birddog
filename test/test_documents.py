import unittest
from datetime import date

from birddog.abstract_database import And, Or
from birddog.documents import (
    BLANK,
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
                         And(Or(("process_code", "eq", "check")), ("anomaly", "eq", False)))

    def test_admin_all_codes_no_anomaly_condition(self):
        f = list_filter(ADMIN)
        self.assertEqual(len(f.conditions), 1)
        self.assertIn(("process_code", "blank", None), f.conditions[0].conditions)

    def test_chosen_code_prefix_and_anomalies(self):
        self.assertEqual(
            list_filter(ESCALATE, process_code="review", label_prefix="DAHMO/5/1/", anomalies_only=True),
            And(Or(("process_code", "eq", "review")), ("anomaly", "eq", True),
                Or(("label", "eq", "DAHMO/5/1"), ("label", "startswith", "DAHMO/5/1/"))))
        self.assertEqual(list_filter(ADMIN, process_code=BLANK),
                         And(Or(("process_code", "blank", None))))

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

if __name__ == "__main__":
    unittest.main()
