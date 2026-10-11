"""Dumps the Documents/Pages records needed by test_location_extraction.py into
test/resources/location_fixture.json, so the test does not depend on the contents
of the active database.

Run against a database that has the documents (e.g. STAGE):
    BIRDDOG_NOCODB_ENV=CLOUD_STAGE python -m test.dump_location_fixture
"""
import json

from birddog.database import Database
from test.test_location_extraction import (
    LocationPerformanceEvaluator, REPORT_PATH, TARGET_ROWS, FIXTURE_PATH,
)

DOC_FIELDS = ["Id", "label", "title", "description", "comments", "page_description", "owning_pages"]
PAGE_FIELDS = ["Id", "description", "root_label", "parent"]


def _link_ids(links):
    return [link["Id"] for link in links or []]


def main():
    ev = LocationPerformanceEvaluator(REPORT_PATH)
    db = Database()
    docs, pages = {}, {}
    for row in TARGET_ROWS:
        file, _ = ev.get_file_towns_from_cumulative_report(row)
        doc_id = ev.get_doc_id(file)
        if not doc_id:
            print(f"row {row}: {file} not found, skipping")
            continue
        rec = db.read("Documents", doc_id)
        docs[file] = {k: rec.get(k) for k in DOC_FIELDS}
        pending = _link_ids(rec.get("owning_pages"))
        while pending:
            page_id = pending.pop()
            if str(page_id) in pages:
                continue
            page = db.read("Pages", page_id)
            pages[str(page_id)] = {k: page.get(k) for k in PAGE_FIELDS}
            pending.extend(_link_ids(page.get("parent")))
    with open(FIXTURE_PATH, "w", encoding="utf8") as f:
        json.dump({"documents": docs, "pages": pages}, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(docs)} documents, {len(pages)} pages to {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
