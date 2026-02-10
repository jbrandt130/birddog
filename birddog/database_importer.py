from birddog.database import Database, timer
from birddog.database_updater import form_document_record
from birddog.abstract_database import (
    InvalidFieldValue,
    )
from birddog.utility import trim_after_last_slash
from birddog.wiki import (
    WIKI_NAMESPACE,
    get_title,
    )
from colorama import Fore, init
from openpyxl import load_workbook
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
import time

from birddog.log import get_logger
_logger = get_logger()
init()  # Windows fix


def get_page_title_from_link(cell):
    if cell.hyperlink:
        url = cell.hyperlink.target
    else:
        if isinstance(cell.value, str):
            url = cell.value
        else:
            return None

    if "index.php" in url:
        query = urlparse(url).query
        params = parse_qs(query)
        if "title" in params:
            result = params["title"][0]
            ns_prefix = f"{WIKI_NAMESPACE}:"
            if result.startswith(ns_prefix):
                result = result[len(ns_prefix):]
            return result
    if "redlink" in url:
        url = urlparse(url).path
        #_logger.info(f"redlink title: {url}")
    return get_title(unquote(url), include_namespace=False)

def get_cell_link(cell):
    return unquote(cell.hyperlink.target) if cell.hyperlink else ""

def get_cell_value(cell):
    value = cell.value
    if value is None:
        return value
    if isinstance(value, float):
        value = int(value)
    if not isinstance(value, str):
        value = str(value)
    return value

def get_cell_int_value(cell):
    value = get_cell_value(cell)
    if value is None:
        return 0
    if isinstance(value, (str, int, float)):
        return int(value)
    raise TypeError(f"get_cell_int_value: unrecognized type: {type(value)}")

def combine_cell_value(cell1, cell2):
    result = ",".join(filter(
        lambda x: x is not None,
        (get_cell_value(cell1), get_cell_value(cell2))))
    return result if result else None

def add_page(page: dict, page_table: dict) -> None:
    """Merge a page entry into the page_table by title."""
    title = page["title"]
    # ensure an entry exists for this title
    entry = page_table.setdefault(title, dict())
    # update/merge keys
    entry.update(page)

def is_positive_int(s: str) -> bool:
    """Check if a string is a positive integer.
    This returns True only for positive integers like "123", and False for "0", "-5", "3.14", "", "abc"."""
    return bool(s) and s.isdigit() and s != '0'

def find_header(ws, row, start_col, end_col, header):
    """Find a cell with the given header in the given row of the given worksheet.
    Returns the column if found, None otherwise."""
    header = header.upper()
    start_ord = ord(start_col)
    end_ord = ord(end_col)
    for i in range(start_ord, end_ord + 1):
        cell_addr = f"{chr(i)}{row}"
        contents = get_cell_value(ws[cell_addr])
        if contents is None:
            continue
        contents = contents.upper()
        if contents == header:
            return i
    return None

def process_archive_sheet(ws, page_table=dict()):
    parent_title = get_page_title_from_link(ws["D3"])
    source_type = get_cell_value(ws["C1"])
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["A1"]).replace(" ", "-"),
        "level": "archive",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": get_cell_value(ws["L1"]),
        "timestamp": get_cell_value(ws["O1"]),
        "doc_links": get_cell_value(ws["B4"]),
        "source_type": source_type,
        "parent": "",
        "wiki_link": get_cell_link(ws["D3"]),
    }, page_table)

    for r in range(7, ws.max_row + 1):
        cell = ws[f"A{r}"]
        if str(cell.value).startswith("="):
            break
        if not is_positive_int(str(cell.value)):
            # sometimes there is some text in the A column, like "General page content" -
            # we skip such lines
            continue
        if cell.value:
            title = get_page_title_from_link(cell)
            label = get_cell_value(cell)
            add_page({
                "title": title,
                "label": label,
                "level": "fond",
                "description": get_cell_value(ws[f"B{r}"]),
                "years": get_cell_value(ws[f"C{r}"]),
                "availability": get_cell_value(ws[f"D{r}"]),
                "source_type": source_type,
                "parent": parent_title,
                "comments": get_cell_value(ws[f"O{r}"]),
            }, page_table)
    return page_table


def process_fond_sheet(ws, page_table=dict()):
    parent_title = get_page_title_from_link(ws["D3"])
    source_type = get_cell_value(ws["C1"])
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["G1"]),
        "level": "fond",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": get_cell_value(ws["L1"]),
        "timestamp": get_cell_value(ws["O1"]),
        "doc_links": get_cell_value(ws["B4"]),
        "source_type": source_type,
        "parent": trim_after_last_slash(parent_title),
        "wiki_link": get_cell_link(ws["D3"]),
    }, page_table)

    for r in range(7, ws.max_row + 1):
        cell = ws[f"A{r}"]
        if str(cell.value).startswith("="):
            break
        if cell.value:
            title = get_page_title_from_link(cell)
            label = get_cell_value(cell)
            add_page({
                "title": title,
                "label": label,
                "level": "opus",
                "description": get_cell_value(ws[f"B{r}"]),
                "years": get_cell_value(ws[f"C{r}"]),
                "availability": get_cell_value(ws[f"D{r}"]),
                "source_type": source_type,
                "parent": parent_title,
                "comments": get_cell_value(ws[f"O{r}"]),
            }, page_table)
    return page_table


def process_opus_sheet(ws, page_table=dict()):
    parent_title = get_page_title_from_link(ws["D3"])
    source_type = get_cell_value(ws["C1"])
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["H1"]),
        "level": "opus",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": get_cell_value(ws["L1"]),
        "timestamp": get_cell_value(ws["O1"]),
        "doc_links": get_cell_value(ws["B4"]),
        "parent": trim_after_last_slash(parent_title),
        "source_type": source_type,
        "wiki_link": get_cell_link(ws["D3"]),
    }, page_table)

    #there are sometimes columns inserted between "process" and "Comments",
    #so we need to find the exact position of the latter one
    hdr_row = 6
    comments_col = find_header(ws, hdr_row, "G", "Z", "Comments")
    if comments_col is None:
        raise ValueError(f"No 'Comments' column found in sheet {parent_title}")

    for r in range(hdr_row + 1, ws.max_row + 1):
        cell = ws[f"A{r}"]
        if str(cell.value).startswith("="):
            break
        if cell.value:
            title = get_page_title_from_link(cell)
            if title is None:
                # If it is a comment, like "Index files linked at top of page" - skip this line
                continue

            label = get_cell_value(cell)
            comments_cell_addr = f"{chr(comments_col)}{r}" #default f"G{r}"
            processor_cell_addr1 = f"{chr(comments_col+ 2)}{r}" #default f"I{r}"
            processor_cell_addr2 = f"{chr(comments_col+ 5)}{r}" #default f"L{r}"
            pages_processed_cell_addr1 = f"{chr(comments_col + 3)}{r}" #default f"J{r}"
            pages_processed_cell_addr2 = f"{chr(comments_col + 6)}{r}" #default f"M{r}"

            add_page({
                "title": title,
                "label": label,
                "level": "case",
                "description": get_cell_value(ws[f"B{r}"]),
                "years": get_cell_value(ws[f"C{r}"]),
                "source_type": source_type,
                "parent": parent_title,
                "doc_links": get_cell_link(ws[f"B{r}"]),
                "doc_type": get_cell_value(ws[f"D{r}"]),
                "content_code": get_cell_value(ws[f"E{r}"]),
                "process_code": get_cell_value(ws[f"F{r}"]),
                "availability": "linked" if get_cell_link(ws[f"A{r}"]) is not None else "unlinked",
                #the columns for the following cells are not fixed
                "comments": get_cell_value(ws[comments_cell_addr]),
                "processor": combine_cell_value(ws[processor_cell_addr1], ws[processor_cell_addr2]),
                "pages_processed": get_cell_int_value(ws[pages_processed_cell_addr1]) +
                                   get_cell_int_value(ws[pages_processed_cell_addr2]),
            }, page_table)
    return page_table


def process_worksheets(worksheets, page_table=dict()):
    for sheet in worksheets:
        try:
            _logger.info(f"processing worksheet: {sheet.title}")
            if get_cell_value(sheet["H1"]):
                page_table = process_opus_sheet(sheet, page_table)
            elif get_cell_value(sheet["G1"]):
                page_table = process_fond_sheet(sheet, page_table)
            else:
                page_table = process_archive_sheet(sheet, page_table)
        except Exception as e:
            e.args = (f"{e} | error in sheet {sheet.title}", *e.args[1:])
            raise  # re-raise same exception with modified message
    return page_table


def import_spreadsheet(sw_filepath):
    db = Database()
    workbook = load_workbook(sw_filepath)
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
    for page in page_data:
        title = page.get("title")
        _logger.info(f"Upsert: {title}")

        # upload the page record
        page_payload = {k: v for k, v in page.items() if k not in doc_only_fields}
        curr_record_id = db.write("Pages", page_payload)

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

                # quote for document titles that contain protected characters such as "?"
                quoted_record = form_document_record(doc_link)

                doc_payload = {k: v for k, v in page.items() if k in doc_only_fields}
                for doc_field in doc_only_fields_all_caps:
                    if doc_field in doc_payload and doc_payload[doc_field] is not None:
                        doc_payload[doc_field] = doc_payload[doc_field].rstrip().upper()
                doc_payload["title"] = quoted_record["title"]
                doc_payload["link"] = quoted_record["link"]
                doc_payload["owning_pages"] = title
                _logger.info(f"doc title: {doc_payload['title']}, doc link: {doc_link}")
                try:
                    doc_record_id = db.write("Documents", doc_payload)
                    db.create_links(
                        "Documents",
                        "owning_pages",
                        doc_record_id,
                        curr_record_id)
                except InvalidFieldValue as e:
                    print(f"{Fore.RED}{e}{Fore.RESET}")
    timer.report()  # See average time spent in DB functions

def process_dir(dir_path):
    dir_path = Path(dir_path)
    errors_file = dir_path / 'SummaryInfo.txt'
    with errors_file.open('a', encoding='utf-8') as f:
        for entry in dir_path.glob('*.xlsx'):
            if entry.is_file():
                start = time.perf_counter()
                msg = f"{Fore.GREEN}Processing the spreadsheet {entry}{Fore.RESET}"
                print(msg)
                f.write(msg + "\n")
                try:
                    import_spreadsheet(entry)
                except Exception as e:
                    error_msg = f"Error processing {entry}: {e}\n"
                    f.write(error_msg)
                    print(f"{Fore.RED}{error_msg}{Fore.RESET}")
                end = time.perf_counter()
                f.write(f"Elapsed: {end - start:.6f} seconds\n")
                f.flush()

#testing
if __name__ == "__main__":
    #filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/DAHO-D-wiki-20251217.xlsx"
    #filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/DASO-D-wiki-20260119.xlsx"
    #filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/DAVO-D-wiki-20260125.xlsx"
    #filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/DADO-D-wiki-20251027.xlsx"
    #filepath = "C:/temp/test.xlsx"
    #filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/DAHEO-D-wiki-20260119.xlsx"
    #import_spreadsheet(filepath)
    dir_path = "C:/jewishGen/Import2DB/SourceSpreadsheets/"
    process_dir(dir_path)