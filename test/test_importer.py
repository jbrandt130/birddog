import unittest

from birddog.database_importer import (
    map_old_doc_type,
    map_old_process_code,
    new_codes_from_old,
)


class TestMapOldDocType(unittest.TestCase):
    def test_direct_mappings(self):
        self.assertEqual(map_old_doc_type("V"), ["Vitals"])
        self.assertEqual(map_old_doc_type("P"), ["Residence & Identity"])
        self.assertEqual(map_old_doc_type("O"), ["Other"])

    def test_obsolete_m_is_vitals(self):
        self.assertEqual(map_old_doc_type("M"), ["Vitals"])

    def test_c_and_l_are_blank(self):
        self.assertEqual(map_old_doc_type("C"), [])
        self.assertEqual(map_old_doc_type("L"), [])

    def test_unrecognized_and_empty_are_blank(self):
        self.assertEqual(map_old_doc_type("X"), [])
        self.assertEqual(map_old_doc_type(""), [])
        self.assertEqual(map_old_doc_type(None), [])

    def test_multiple_codes(self):
        self.assertEqual(map_old_doc_type("V/C/P"), ["Vitals", "Residence & Identity"])
        self.assertEqual(map_old_doc_type(["O", "L"]), ["Other"])
        self.assertEqual(map_old_doc_type("V, M"), ["Vitals"])

    def test_case_and_whitespace(self):
        self.assertEqual(map_old_doc_type(" v\t"), ["Vitals"])


class TestMapOldProcessCode(unittest.TestCase):
    def test_mappings(self):
        self.assertEqual(map_old_process_code("C1"), "check")
        self.assertEqual(map_old_process_code("C2"), "check")
        self.assertEqual(map_old_process_code("P1"), "save")
        self.assertEqual(map_old_process_code("P2"), "save")
        self.assertEqual(map_old_process_code("DP"), "duplicate")
        self.assertEqual(map_old_process_code("NO"), "reject")
        self.assertEqual(map_old_process_code("C3"), "save")
        self.assertEqual(map_old_process_code("FX"), "defer")

    def test_blank_codes(self):
        for code in ("E", "ZZ", "", None):
            self.assertIsNone(map_old_process_code(code), code)

    def test_case_and_whitespace(self):
        self.assertEqual(map_old_process_code(" p1\t"), "save")


class TestNewCodesFromOld(unittest.TestCase):
    def test_both_mapped(self):
        payload = {"OLD_doc_type": "V", "OLD_process_code": "DP"}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": ["Vitals"], "process_code": "duplicate", "save_status": None})

    def test_blank_results_clear_new_fields(self):
        payload = {"OLD_doc_type": "C", "OLD_process_code": "E"}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": None, "process_code": None, "save_status": None})

    def test_c3_is_save_with_anomaly(self):
        payload = {"OLD_doc_type": "V", "OLD_process_code": " c3\t"}
        self.assertEqual(new_codes_from_old(payload), {
            "doc_type": ["Vitals"],
            "process_code": "save",
            "save_status": None,
            "anomaly": True,
            "anomaly_comment": "formerly C3 process code",
        })

    def test_other_codes_leave_anomaly_alone(self):
        for code in ("P1", "E", None):
            self.assertNotIn("anomaly", new_codes_from_old({"OLD_process_code": code}), code)

    def test_empty_old_values_clear_new_fields(self):
        payload = {"OLD_doc_type": None, "OLD_process_code": None}
        self.assertEqual(new_codes_from_old(payload),
                         {"doc_type": None, "process_code": None, "save_status": None})

    def test_save_with_when_transcribed_is_externally_indexed(self):
        for code in ("P1", "P2", "C3"):
            payload = {"OLD_process_code": code, "when_transcribed": "2019"}
            self.assertEqual(new_codes_from_old(payload)["save_status"], "externally_indexed", code)

    def test_save_without_when_transcribed_leaves_save_status_blank(self):
        for transcribed in (None, "", "  "):
            payload = {"OLD_process_code": "P1", "when_transcribed": transcribed}
            self.assertIsNone(new_codes_from_old(payload)["save_status"], transcribed)
        self.assertIsNone(new_codes_from_old({"OLD_process_code": "P1"})["save_status"])

    def test_when_transcribed_only_counts_for_save(self):
        for code in ("C1", "DP", "NO", "FX", "E", None):
            payload = {"OLD_process_code": code, "when_transcribed": "2019"}
            self.assertIsNone(new_codes_from_old(payload)["save_status"], code)

    def test_missing_old_fields(self):
        self.assertEqual(new_codes_from_old({"url": "https://x"}), {})


if __name__ == "__main__":
    unittest.main()
