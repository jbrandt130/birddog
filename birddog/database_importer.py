from birddog.database import Database
from birddog.nocodb import process_worksheets
from birddog.abstract_database import (
    InvalidFieldValue,
    )
from colorama import Fore, init
from openpyxl import load_workbook

from birddog.log import get_logger
_logger = get_logger()
init()  # Windows fix

def import_spreadsheet(filepath):
    db = Database()
    workbook = load_workbook(filepath)
    page_data = process_worksheets(workbook)

    doc_only_fields_all_caps = {
        "doc_type",
        "content_code",
        "process_code",
    }
    doc_only_fields = doc_only_fields_all_caps | {
        "pages_processed",
        "processor",
    }

    if isinstance(page_data, dict):
        page_data = list(page_data.values())
    result = []
    for page in page_data:
        title = page.get("title")
        _logger.info(f"Upsert: {title}")

        # upload the page record
        page_payload = {k: v for k, v in page.items() if k not in doc_only_fields}
        curr_record_id = db.write("Pages", page_payload)
        result.append(curr_record_id)

        # link to parent
        parent_title = page.get("parent")
        if parent_title:
            # ensure parent exists
            parent_record_id = db.lookup("Pages", parent_title)
            if parent_record_id is None:
                parent_record_id = db.write("Pages", {"title": parent_title})
            db.create_links(
                "Pages",
                "children",
                parent_record_id,
                curr_record_id)

        # check for doc links
        doc_links = page.get("doc_links")
        if doc_links:
            if isinstance(doc_links, str):
                doc_links = [doc_links]
            if not isinstance(doc_links, (list, tuple)):
                raise TypeError("doc_links must be str, list or tuple")
            for doc_link in doc_links:
                _logger.info(f"Adding doc link for {title}: {doc_link}")
                doc_payload = {k: v for k, v in page.items() if k in doc_only_fields}
                for doc_field in doc_only_fields_all_caps:
                    if doc_field in doc_payload and doc_payload[doc_field] is not None:
                        doc_payload[doc_field] = doc_payload[doc_field].rstrip().upper()
                doc_payload["title"] = doc_link.rstrip("/").rsplit("/", 1)[-1]
                doc_payload["link"] = doc_link
                doc_payload["owning_pages"] = title
                _logger.info(f"doc title: {doc_payload['title']}, doc link: {doc_payload['link']}")
                try:
                    doc_record_id = db.write("Documents", doc_payload)
                    db.create_links(
                        "Documents",
                        "owning_pages",
                        doc_record_id,
                        curr_record_id)
                except InvalidFieldValue as e:
                    print(f"{Fore.RED}{e}{Fore.RESET}")


    return result

#testing
#filepath = "C:/Users/user/Downloads/DAHO-D-wiki-20251217.xlsx"
#filepath = "C:/Users/user/Downloads/DAVO-D-wiki-20251226.xlsx"
#filepath = "C:/Users/user/Downloads/DASO-D-wiki-20260119.xlsx"
filepath = "C:/Users/user/Downloads/DAVO-D-wiki-20260125.xlsx"
rc = import_spreadsheet(filepath)