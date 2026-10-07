import os
import re
import unittest

import openpyxl

from birddog.database import Database
from birddog.file_location import (
    FileLocationFinder,
    remove_words_list,
)

UNITTEST_RESOURCE_DIR = 'test/resources'

class LocationPerformanceEvaluator:
    """Evaluator helper class that inspects excel data against database records."""

    def __init__(self, file_path: str, provider: str = "groq"):
        workbook = openpyxl.load_workbook(file_path, data_only=True)

        self.sheet = workbook.active
        if not self.sheet:
            raise RuntimeError(f"Failure reading the spreadsheet {file_path}")

        self.total_data_rows = self.sheet.max_row - 1
        db = Database()
        self.file_location_finder = FileLocationFinder(db, provider)
        self.num_evaluated_docs = 0
        self.num_docs_evaluated_correctly = 0
        self.num_docs_with_all_locations_found = 0
        self.total_num_cumulative_locs = 0
        self.total_num_cumulative_locs_in_jgdb = 0
        self.total_num_extracted_locs = 0
        self.total_num_coinciding_locs = 0
        self.rows_with_wrong_locations = []

    def get_file_towns_from_cumulative_report(self, row_number: int):
        if not self.sheet:
            return None, None

        if row_number > self.total_data_rows or row_number < 2:
            raise ValueError(
                f"Invalid document number {row_number - 1}, "
                f"it must be between 1 and {self.total_data_rows - 1}"
            )

        cell_c_value = self.sheet.cell(row=row_number, column=3).value
        cell_g_value = self.sheet.cell(row=row_number, column=7).value

        file = "" if cell_c_value is None else str(cell_c_value)
        towns = "" if cell_g_value is None else str(cell_g_value)

        words_to_delete = [
            "Koloniy", "Volost'", "Village", "Khutor", "a", "at", "the", "nr.", "near",
            "monastery", "sugar", "beet", "plant", "big", "road", "Fabrica", "forest",
            "station", "railway", "beh.bridge", "sloboda", "slobodka",
        ]

        town_list = []
        if towns:
            town_list = re.split(r",\s*|\s+I\s+", towns)

        no_brackets_town_list = []
        for town in town_list:
            match = re.match(r"\s*(.*?)\s*\(\s*(.*?)\s*\)\s*", town)
            if match:
                no_brackets_town_list.extend([match.group(1), match.group(2)])
            else:
                no_brackets_town_list.append(town)

        town_list = [
            cleaned_town
            for town in no_brackets_town_list
            if (cleaned_town := remove_words_list(town, words_to_delete).strip())
        ]

        return file, town_list

    def get_doc_labels(self, file: str):
        file = file.strip()
        split_parts = re.split(r"[ /]", file, maxsplit=1)
        if len(split_parts) < 2:
            raise ValueError(f"The provided string {file} does not contain any space or slash characters.")
        archive_name, fund_hyphen_etc = split_parts

        fund_slash_etc = fund_hyphen_etc.replace("-", "/")

        match = re.match(r"^[a-zA-Z]/(.*)$", fund_slash_etc)
        if match:
            fund_slash_etc = match.group(1)

        if re.search(r"[\u0400-\u04FF]", archive_name):
            latin_archive_name = self.file_location_finder.find_archive_name_by_cyrillic_abbr(archive_name)
            if not latin_archive_name:
                raise ValueError(f"The Cyrillic abbreviation {archive_name} was not found.")
        else:
            latin_archive_name = archive_name

        suffixes = ["/", "-D/", "-R/", "-K/", "-P/", "-N/", "-A/"]
        return [latin_archive_name + suffix + fund_slash_etc for suffix in suffixes]

    def get_doc_id(self, file: str):
        possible_labels = self.get_doc_labels(file)
        record_id = None

        for label in possible_labels:
            cursor = None
            while True:
                records, cursor = self.file_location_finder.scan_database(
                    table_name="Documents",
                    where=("label", "eq", label),
                    limit=100,
                    cursor=cursor,
                )
                if records or cursor is None:
                    break
            if records:
                record_id = records[0]["Id"]
                break

        return record_id

    def evaluate_location_extraction(
        self,
        rows: list[int],
        skip_extraction: bool = False,
        batch_size: int = 20,
    ) -> dict:
        """Evaluates rows and returns dictionary of doc locations and statistics."""
        doc_location_results = {}
        pending: list[tuple[int, str, list[str], int]] = []

        for row in rows:
            try:
                file, town_list = self.get_file_towns_from_cumulative_report(row)
                if file and town_list and (doc_id := self.get_doc_id(file)):
                    pending.append((row, file, town_list, doc_id))
            except ValueError:
                continue

        if not pending or skip_extraction:
            return doc_location_results

        for flush_start in range(0, len(pending), batch_size):
            chunk = pending[flush_start : flush_start + batch_size]
            buffered_doc_ids = [item[3] for item in chunk]

            batch_results = self.file_location_finder.get_doc_locations_batched(
                buffered_doc_ids,
                batch_size=batch_size,
                only_smallest_locations=True,
            )

            for row, _, town_list, doc_id in chunk:
                if not batch_results.get(doc_id, [])[0]:
                    extracted_locs = batch_results.get(doc_id, [])[1]
                    doc_location_results[doc_id] = extracted_locs

                    self.evaluate_on_one_doc_from_batch(
                        row,
                        doc_id,
                        town_list,
                        extracted_locs,
                        batch_results.get(doc_id, [])[2],
                        batch_results.get(doc_id, [])[3],
                        batch_results.get(doc_id, [])[4],
                    )

        return doc_location_results

    def evaluate_on_one_doc_from_batch(
        self,
        row: int,
        doc_id: int,
        cumulative_towns: list[str],
        doc_location_ids: list[str],
        archive_locs: list[dict],
        extended_archive_locs: list[list[dict]],
        district_names: set[str],
    ):
        cumulative_locations, district_names = self.file_location_finder.match_places_to_location_ids(
            doc_id, cumulative_towns, archive_locs, extended_archive_locs, district_names
        )
        cumulative_locations = [dict(f_set) for f_set in cumulative_locations]
        cumulative_locations_ids_set = {loc["loc_id"] for loc in cumulative_locations}

        if not cumulative_locations_ids_set:
            return

        doc_location_ids_set = set(doc_location_ids)
        if doc_location_ids_set == cumulative_locations_ids_set:
            self.num_docs_evaluated_correctly += 1
        else:
            self.rows_with_wrong_locations.append(row)

        if cumulative_locations_ids_set.issubset(doc_location_ids_set):
            self.num_docs_with_all_locations_found += 1

        intersection_set = doc_location_ids_set & cumulative_locations_ids_set

        self.num_evaluated_docs += 1
        self.total_num_cumulative_locs += len(set(cumulative_towns))
        self.total_num_cumulative_locs_in_jgdb += len(cumulative_locations_ids_set)
        self.total_num_extracted_locs += len(doc_location_ids_set)
        self.total_num_coinciding_locs += len(intersection_set)


class TestLocationVsCumulativeReport(unittest.TestCase):
    """Unit test suite for location extraction validation against the cumulative report."""

    @classmethod
    def setUpClass(cls):
        report_path = f'{UNITTEST_RESOURCE_DIR}/Cumulative Ukraine Research Report.xlsx'
        if not os.path.exists(report_path):
            raise unittest.SkipTest(f"Spreadsheet file not found at: {report_path}")

        cls.evaluator = LocationPerformanceEvaluator(report_path, provider="groq")
        cls.target_rows = [21, 48, 119, 213, 297, 314, 397, 451, 457, 459]
        cls.batch_size = 5

        # Execute evaluation once during class setup
        cls.extracted_doc_locations = cls.evaluator.evaluate_location_extraction(
            rows=cls.target_rows,
            skip_extraction=False,
            batch_size=cls.batch_size,
        )

    def test_processed_documents_count(self):
        """Verify that 9 target rows are processed."""
        self.assertEqual(
            self.evaluator.num_evaluated_docs,
            len(self.target_rows) - 1, # 9 out ot 10
            f"Expected {len(self.target_rows)} processed documents, got {self.evaluator.num_evaluated_docs}",
        )

    def test_correct_location_identifications_count(self):
        """Verify the number of documents with correctly identified locations matches expected count."""
        expected_correct_docs = 4
        self.assertEqual(
            self.evaluator.num_docs_evaluated_correctly,
            expected_correct_docs,
            f"Expected {expected_correct_docs} correctly identified documents, got {self.evaluator.num_docs_evaluated_correctly}",
        )

    def test_reported_locations_per_document(self):
        """Verify exact location IDs reported per document match expected mapping."""
        # Ground truth mapping: {doc_id: [expected_location_ids]}
        expected_doc_locations = {
            76800: ["-1057170"],
            68945: ["-1037001"],
            69400: ["-1037001"],
            69337: ["-1037001"],
            69397: ["-1037001"],
            69326: ["-1037001"],
            69971: ["-1042950"],
            78959: ["-1037320"],
            78963: ["-1037320"],
        }

        for doc_id, expected_loc_ids in expected_doc_locations.items():
            with self.subTest(doc_id=doc_id):
                actual_loc_ids = self.extracted_doc_locations.get(doc_id, [])
                self.assertCountEqual(
                    actual_loc_ids,
                    expected_loc_ids,
                    f"Doc {doc_id} locations mismatch. Expected: {expected_loc_ids}, Actual: {actual_loc_ids}",
                )


if __name__ == "__main__":
    unittest.main()