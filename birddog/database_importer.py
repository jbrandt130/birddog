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

def get_source_type(ws):
    source_type = get_cell_value(ws["C1"])
    #it is sometimes "archive" instead of "archives"
    if source_type.startswith("archive"):
        source_type = "archives"
    return source_type

def get_dates(ws, change_date_col, ref_date_col):
    change_date = get_cell_value(ws[f"{chr(change_date_col + 1)}1"])  # default L1
    timestamp = get_cell_value(ws[f"{chr(ref_date_col + 1)}1"])  # default 01
    return change_date, timestamp

def get_parent_title(ws):
    row = 3
    source_col = find_header(ws, row, "B", "Z", "source:")
    parent_title = ws[f"{chr(source_col + 1)}{row}"] #default D3
    parent_title = get_page_title_from_link(parent_title)
    return parent_title

def process_archive_sheet(ws, change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    parent_title = get_parent_title(ws)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, change_date_col, ref_date_col)
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["A1"]).replace(" ", "-"),
        "level": "archive",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
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


def process_fond_sheet(ws, change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    parent_title = get_parent_title(ws)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, change_date_col, ref_date_col)
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["G1"]),
        "level": "fond",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
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


def process_opus_sheet(ws, change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    parent_title = get_parent_title(ws)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, change_date_col, ref_date_col)
    add_page({
        "title": parent_title,
        "label": get_cell_value(ws["H1"]),
        "level": "opus",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
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

def count_fund_opus_attributes(sheet, end_col):
    """Fund name and opus name appear in the first row,
    typically but not always in cells G1 and H1. We count the number of nonempty cells.
    For archive, there would be none, for fund - one, anf for opus - two."""
    start_col = "D"
    row = 1
    start_ord = ord(start_col)
    num_nonempty_cells = 0
    for i in range(start_ord, end_col):
        cell_addr = f"{chr(i)}{row}"
        contents = get_cell_value(sheet[cell_addr])
        if contents is None:
            continue
        num_nonempty_cells += 1
    return num_nonempty_cells

def process_worksheets(worksheets, page_table=None):
    if not page_table:
        page_table = {}
    hdr_row = 1
    for sheet in worksheets:
        try:
            _logger.info(f"processing worksheet: {sheet.title}")
            change_date_col = find_header(sheet, hdr_row, "K", "Z", "change date:")
            if change_date_col is None:
                raise ValueError(f"No 'change date:' column")
            ref_date_col = find_header(sheet, hdr_row, chr(change_date_col + 2), "Z", "reference date:")
            if ref_date_col is None:
                raise ValueError(f"No 'reference date:' column")
            num_attributes = count_fund_opus_attributes(sheet, change_date_col)
            match num_attributes:
                case 2:
#            if get_cell_value(sheet[f"{chr(change_date_col - 3)}{hdr_row}"]): #default H1
                    page_table = process_opus_sheet(sheet, change_date_col, ref_date_col, page_table)
                case 1:
#            elif get_cell_value(sheet[f"{chr(change_date_col - 4)}{hdr_row}"]): #default G1
                    page_table = process_fond_sheet(sheet, change_date_col, ref_date_col, page_table)
                case 0:
#            else:
                    page_table = process_archive_sheet(sheet, change_date_col, ref_date_col, page_table)
                case _:
                    raise ValueError(f"{num_attributes} for worksheet {sheet.title}")

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

    # Step 1: validate all page records, remove any invalid ones
    output_pages = []
    _logger.info(f"Upserting: {len(page_data)} pages")

    for page in page_data:
        title = page.get("title")
        if not title:
            _logger.warning(f"Cannot import page with no title: parent={page.get('parent')}, label={page.get('label')}")
            continue
        # _logger.info(f"Validating: {title}")

        # upload the page record
        page_payload = {k: v for k, v in page.items() if k not in doc_only_fields}
        page_payload["import_message"] = ""  # clear any pre-existing import messages
        abort_page = False
        while True:
            try:
                # attempt to encode page payload to verify correctness
                db.encode_records("Pages", [page_payload])
                break
            except InvalidFieldValue as err:
                _logger.error(f"malformed value: {err}")
                if not err.value:
                    abort_page = True
                    break
                # replace unrecognized value and save a note in import_message
                page_payload[err.field] = ""
                prior_msg = page_payload.get("import_message")
                msg = f"malformed: {err.field}: {err.value}"
                page_payload["import_message"] = f"{prior_msg}; {msg}" if prior_msg else msg
                # retry record normalization

        if abort_page:
            _logger.error(f"skipping malformed page record: {title}")
            continue
        output_pages.append(page_payload)

    # Step 2: batch write the page records and preserve id
    _logger.info(f"Writing {len(output_pages)} page records to db")
    output_ids = db.write("Pages", output_pages)
    for page, record_id in zip(output_pages, output_ids):
        page["Id"] = record_id

    # Step 3: form dict by page title
    page_dict = {page["title"]: page for page in output_pages}

    # Step 4: locate all parents and link to children
    parent_dict = {}
    for page in page_dict.values():
        parent_title = page.get("parent")
        if parent_title:
            # _logger.info(f"parent->child link found: {parent_title}->{page['title']}")
            parent_page = parent_dict.get(parent_title)
            if not parent_page:
                parent_page = page_dict.get(parent_title)
                if not parent_page:
                    parent_page = {"title": parent_title}
                    parent_record_id = db.lookup("Pages", parent_title)
                    if not parent_record_id:
                        # create a stub page for parent
                        parent_record_id = db.write("Pages", parent_page)
                    parent_page["Id"] = parent_record_id
                parent_dict[parent_title] = parent_page
            child_ids = parent_page.get("child_ids", [])
            child_ids.append(page["Id"])
            # _logger.info(f"child_ids: {parent_title}: {child_ids}")
            parent_page["child_ids"] = child_ids

    # Step 5: link parents to their children
    for parent in parent_dict.values():
        child_ids = parent.get("child_ids")
        if child_ids:
            _logger.info(f"Linking {len(child_ids)} children for {parent['title']}")
            db.create_links(
                "Pages",
                "children",
                parent["Id"],
                child_ids)

        # Step 6: create records for all linked docs
        output_docs = {}
        doc_link_dict = {}
        for page in output_pages:
            doc_links = page.get("doc_links")
            if doc_links:
                if isinstance(doc_links, str):
                    doc_links = [doc_links]
                if not isinstance(doc_links, (list, tuple)):
                    raise TypeError("doc_links must be str, list or tuple")

                for doc_link in doc_links:
                    # quote for document titles that contain protected characters such as "?"
                    quoted_record = form_document_record(doc_link)

                    doc_payload = {k: v for k, v in page.items() if k in doc_only_fields}
                    for doc_field in doc_only_fields_all_caps:
                        if doc_field in doc_payload and doc_payload[doc_field] is not None:
                            doc_payload[doc_field] = doc_payload[doc_field].rstrip().upper()
                    doc_payload["title"] = quoted_record["title"]
                    link = quoted_record["link"]
                    doc_payload["link"] = link
                    output_docs[link] = doc_payload
                    linked_docs = doc_link_dict.get(page["title"], [])
                    linked_docs.append(doc_payload)
                    doc_link_dict[page["title"]] = linked_docs

        # Step 7: write doc records to db
        all_docs = list(output_docs.values())
        _logger.info(f"Writing {len(all_docs)} doc records")

        doc_ids = db.write("Documents", all_docs)
        for doc_id, doc in zip(doc_ids, all_docs):
            output_docs[doc["link"]]["Id"] = doc_id

        # Step 8: link pages to docs
        for page_title, linked_docs in doc_link_dict.items():
            page_id = page_dict[page_title]["Id"]
            linked_ids = [output_docs[d["link"]]["Id"] for d in doc_link_dict[page_title]]
            _logger.info(f"Adding doc links for {page_title} ({page_id}): {linked_ids}")
            db.create_links(
                "Pages",
                "doc_links",
                page_id,
                linked_ids)

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
                f.write(f"Elapsed: {end - start:.2f} seconds\n")
                f.flush()

#testing
if __name__ == "__main__":
    filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/TSDAVO-archive-20260119.xlsx"
    import_spreadsheet(filepath)
    #dir_path = "C:/jewishGen/Import2DB/SourceSpreadsheets/"
    #process_dir(dir_path)