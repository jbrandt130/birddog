from birddog.database import Database, timer
from birddog.database_updater import form_document_record
from birddog.abstract_database import (
    InvalidFieldValue,
    )
from birddog.wiki import (
    WIKI_NAMESPACE,
    get_title,
    parent_title,
    page_label,
    page_url_from_title,
    sequential_page_label,
    )
from datetime import datetime
from openpyxl import load_workbook
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
import time
from typing import List, Any
import re

from birddog.log import get_logger
_logger = get_logger()

# --------------------------------------------------
# console output highlighting
import os

_ENABLE_CONSOLE_HIGHLIGHTING = os.name == "nt"

START_HDR_ROW = 5 #the headers are sometimes in row 5 and sometimes in row 6 or 7
END_HDR_ROW = 7
FIRST_OPUS_HDR1 = "Case #, link"
FIRST_OPUS_HDR2 = "P#, link"
FIRST_OPUS_HDR3 = "Index #, link"
FIRST_OPUS_HDR4 = "NEW NUMBERING"
FIRST_OPUS_HDR5 = "#"
FIRST_FUND_HDR = "Opus #, link"

if _ENABLE_CONSOLE_HIGHLIGHTING:
    from colorama import Fore, init
    init()  # Windows fix
else:
    # stub out highlight directives
    class Fore:
        RESET = ""
        RED = ""
        GREEN = ""
# --------------------------------------------------

_NS_PREFIX = f"{WIKI_NAMESPACE}:"
def parent_title_no_ns(title, wiki_spreadsheet, archive_unit_name):
    if wiki_spreadsheet:
        result = parent_title(title)
        if not result:
            return None
        if result.startswith(_NS_PREFIX):
            result = result[len(_NS_PREFIX):]
        return result
    else:
        last_slash_pos = archive_unit_name.rfind('/')
        if last_slash_pos != -1:
            return archive_unit_name[0 : last_slash_pos]
        else:
            raise ValueError(f"Archive unit name: {archive_unit_name} contains no slashes")

def get_case_id(cell_contents: str) -> str:
    """If the cell contents is a string representing a number,
    we return its integer part. Otherwise, we return the original string."""
    try:
        case_id_float = float(cell_contents)
        case_id = str(int(case_id_float))
        return case_id
    except ValueError:
        return cell_contents

def url_to_title(url: str) -> str:
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

def cell_to_title(cell, archive_unit_name) -> str:
    val = cell.value
    if isinstance(val, float):
        subunit_name = str(int(val))
    else:
        subunit_name = str(val)
    return f"{archive_unit_name}/{subunit_name}"


def get_page_title_from_link(cell, wiki_spreadsheet, archive_unit_name):
    if wiki_spreadsheet:
        if cell.hyperlink:
            url = cell.hyperlink.target
        else:
            if isinstance(cell.value, str):
                url = cell.value
            else:
                try:
                    url = str(int(float(cell.value)))
                    return url
                except ValueError:
                    return None

        return url_to_title(url)
    else:
        return cell_to_title(cell, archive_unit_name)

def get_case_title(url, cell, wiki_spreadsheet, archive_unit_name):
    if wiki_spreadsheet:
        return url_to_title(url)
    else:
        return cell_to_title(cell, archive_unit_name)

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

def normalize_to_int_str(s: str) -> str:
    if not isinstance(s, str):
        return s

    # Check if integer
    if re.fullmatch(r'-?\d+', s):
        return str(int(s))

    # Check if float that converts to integer
    if re.fullmatch(r'-?\d+(\.\d+)?', s):
        try:
            f = float(s)
            if f.is_integer():
                return str(int(f))
        except ValueError:
            pass

    return s

def log_strange_parsing_result(ws, r, cell, subunits: List[str]):
    num_results = len(subunits)
    normaalized_val = normalize_to_int_str(str(cell.value))
    match num_results:
        case 1:
            #default result - do nothing
            return
        case 0:
            if cell.value is None or len(str(cell.value)) == 0:
                #empty cell - do nothing
                return
            _logger.warning(f"No URl found for the fund '{normaalized_val}', sheet='{ws.title}', row={r} - skipping")
        case _:
            _logger.warning(f"Irregular fund number '{normaalized_val}', sheet='{ws.title}', row={r} - splitting to {subunits}")

def get_url_from_2_cells(unit_num_cell, unit_descr_cell) -> str:
    cell_to_use = unit_num_cell
    if cell_to_use.hyperlink is None:
        cell_to_use = unit_descr_cell
    if cell_to_use.hyperlink is None:
        return ""

    hyperlink = cell_to_use.hyperlink
    url = unquote(hyperlink.target)
#    if hyperlink.location is not None:
#        url = f"{url}#{hyperlink.location}"
    return url


def parse_cell_integers(fund_num_cell, fund_descr_cell) -> tuple[list[Any], str]:
    """
    Parses an openpyxl cell value into a list of integers based on specific rules.
    If no link found in both fund_num_cell, fund_descr_cell, the resulting list is empty

    Rules:
    1. Float that is integer (e.g., 41.0) -> [41]; integer -> integer
    2. String with comma-separated integers (e.g., "41, 42") -> [41, 42]
    3. datetime object -> [day, month]
    4. String range "10-13" -> [10, 11, 12, 13]
    5. Otherwise -> []
    All the integers are converted to strings.

    Args:
        fund_num_cell: openpyxl.cell.cell.Cell object
        fund_descr_cell: openpyxl.cell.cell.Cell object

    Returns:
        1) List[str]: List of parsed integers converted to strings
        2) str: the url from one of the two input cells.
    """
    value = fund_num_cell.value
    if value is None:
        return [], ""

    url = get_url_from_2_cells(fund_num_cell, fund_descr_cell)
    if url == "":
        return [], ""

    # Case 1: Float that is integer
    if isinstance(value, float) and value.is_integer():
        return [str(int(value))], url

    # Case 1: integer
    if isinstance(value, int):
        return [str(value)], url

    # Case 4: datetime -> [day, month]
    if isinstance(value, datetime):
        return [str(value.day), str(value.month)], url

    # Case 2 & 4: Convert to string and parse
    s = str(value).strip()

    if '-' in s:
        if re.match(r'^\d+-\d+$', s):
            # Case 4: Range like "10-13"
            try:
                start, end = map(int, s.split('-'))
                return [str(x) for x in list(range(start, end + 1))], url
            except ValueError:
                pass
        pattern = r'^[A-Z]-[1-9]\d*$'
        is_prefixed_number = bool(re.match(pattern, s))
        if is_prefixed_number:
            return [s], url

    # Case 2: Comma-separated "41, 42"
    if ',' in s:
        parts = [p.strip() for p in s.split(',')]
        ints = []
        for p in parts:
            try:
                num = float(p)
                if num.is_integer():
                    ints.append(str(int(num)))
            except ValueError:
                pass
        if ints:
            return ints, url

    return [], ""


def is_series_of_integer_like_numbers(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False

    parts = s.split('-')
    if any(part == '' for part in parts):
        return False

    for part in parts:
        if re.fullmatch(r'\d+', part):
            continue
        if re.fullmatch(r'\d+\.\d+', part) and float(part).is_integer():
            continue
        return False

    return True

def find_header_in_row(ws, row, start_col, end_col, header):
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

def find_header_in_col(ws, start_row, end_row, col, headers):
    """Find a cell with one of the given headers in the given column of the given worksheet.
    Returns the row if found, None otherwise."""
    for header in headers:
        header = header.upper()
        for i in range(start_row, end_row + 1):
            cell_addr = f"{col}{i}"
            contents = get_cell_value(ws[cell_addr])
            if contents is None:
                continue
            contents = contents.upper()
            if contents == header:
                return i
            if header == FIRST_OPUS_HDR4 and header in contents:
                # it can be something like "NEW NUMBERING (listed here 06/2025)"
                return i
    return None

def get_source_type(ws):
    #it is usually in C1, but it may occur also in E1
    start_ord = ord("C")
    end_ord = ord("E")
    source_type = None #default
    for i in range(start_ord, end_ord + 1):
        cell_addr = f"{chr(i)}1"
        source_type = get_cell_value(ws[cell_addr])
        if source_type is None:
            continue

        #it is sometimes "archive" instead of "archives"
        if source_type.startswith("archive"):
            source_type = "archives"
        elif source_type.startswith("wiki"):
            source_type = "wiki"
        else:
            source_type = "other"
        break
    return source_type

def get_dates(ws, archive_name, change_date_col, ref_date_col):
    change_date = get_cell_value(ws[f"{chr(change_date_col + 1)}1"])  # default L1
    timestamp = get_cell_value(ws[f"{chr(ref_date_col + 1)}1"])  # default 01
    if str(timestamp) == '?' and "DAOO" in archive_name:
        # Juliana's recommendation for these specific spreadsheets
        timestamp = "09 Nov 2024"
        _logger.warning(f"Using change date of {timestamp}")

    return change_date, timestamp

def get_parent_title(ws, wiki_spreadsheet, archive_unit_name):
    if wiki_spreadsheet:
        row = 3
        source_col = find_header_in_row(ws, row, "B", "Z", "source:")
        parent_title_cell = ws[f"{chr(source_col + 1)}{row}"] #default D3
        title = get_page_title_from_link(parent_title_cell, wiki_spreadsheet, archive_unit_name)
    else:
        title = archive_unit_name
    return title

def to_positive_int_str(s: str) -> str:
    """
    If input converts to positive integer (>0), returns str(int).
    Otherwise, returns original input unchanged.
    Handles "11.0" → "11", rejects "0", negatives, non-numbers.
    """
    stripped = s.strip()
    if not stripped:
        return s
    try:
        num = float(stripped)
        if num.is_integer() and num > 0:
            return str(int(num))
        else:
            return s
    except ValueError:
        return s


def cyrillic_to_latin(s: str) -> str:
    """
    Converts Ukrainian/Russian Cyrillic letters to Latin equivalents.
    Leaves digits, hyphens, punctuation unchanged.
    """
    # Cyrillic → Latin transliteration map (Russian + Ukrainian)
    trans_map = {
        # Russian lowercase
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ъ': "'", 'ы': 'y', 'ь': "'", 'э': 'e', 'ю': 'yu', 'я': 'ya',

        # Ukrainian lowercase (+ unique letters)
        'ґ': 'g', 'є': 'ye', 'і': 'i', 'ї': 'yi',

        # Uppercase (same transliteration, capitalized)
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
        'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'H', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Shch',
        'Ъ': "'", 'Ы': 'Y', 'Ь': "'", 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya',
        'Ґ': 'G', 'Є': 'Ye', 'І': 'I', 'Ї': 'Yi'
    }

    result = []
    for char in s:
        result.append(trans_map.get(char, char))  # Keep non-Cyrillic chars unchanged

    return ''.join(result)


def title_cell_val_identical(title: str, cell_val:str) -> tuple[bool, str]:
    """Returns everything after the last '/' or original string if no slash."""
    title_after_last_slash = title.rsplit('/', 1)[-1] if '/' in title else title
    title_after_last_slash = cyrillic_to_latin(title_after_last_slash)
    cell_val_posit_int_cyr = to_positive_int_str(cell_val)
    cell_val_posit_int_lat = cyrillic_to_latin(cell_val_posit_int_cyr)
    identical = title_after_last_slash == cell_val_posit_int_lat

    modified_title = title
    if not identical:
        modified_title = title.rsplit('/', 1)[0] + "/" + cell_val_posit_int_cyr if '/' in title else cell_val_posit_int_cyr
    return identical, modified_title

def general_page_label(title, wiki_spreadsheet):
    if wiki_spreadsheet:
        return page_label(title)
    else:
        return title

def get_page_url(title, url, wiki_spreadsheet):
    if wiki_spreadsheet:
        return page_url_from_title(title)
    else:
        return url

def add_fund_page_if_necessary(ws, wiki_spreadsheet, archive_title, title, url, source_type, r, page_table):
    """ We only add linked pages or pages with comments."""
    availability = get_cell_value(ws[f"D{r}"])
    comments = get_cell_value(ws[f"O{r}"])
    if availability != 'linked' and comments is None:
        # we do not add such pages
        return

    label = general_page_label(title, wiki_spreadsheet)
    add_page({
        "title": title,
        "url": get_page_url(title, url, wiki_spreadsheet),
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": "fond",
        "description": get_cell_value(ws[f"B{r}"]),
        "years": get_cell_value(ws[f"C{r}"]),
        "availability": availability,
        "source_type": source_type,
        "parent": archive_title,
        "comments": comments,
    }, page_table)


def process_archive_sheet(ws, wiki_spreadsheet, archive_name, change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    archive_name = get_parent_title(ws, wiki_spreadsheet, archive_name)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, archive_name, change_date_col, ref_date_col)
    label = general_page_label(archive_name, wiki_spreadsheet)
    add_page({
        "title": archive_name,
        "url": get_cell_link(ws["D3"]),
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": "archive",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
        "doc_links": get_cell_value(ws["B4"]),
        "source_type": source_type,
        "parent": "",
    }, page_table)

    for r in range(7, ws.max_row + 1):
        fund_num_cell = ws[f"A{r}"]
        fund_descr_cell = ws[f"B{r}"]
        if str(fund_num_cell.value).startswith("="):
            break
        fund_ids, url = parse_cell_integers(fund_num_cell, fund_descr_cell)
        log_strange_parsing_result(ws, r, fund_num_cell, fund_ids)
        num_results = len(fund_ids)
        match num_results:
            case 0:
                # probably some text in the A column, like "General page content" -
                # we skip such lines
                continue
            case 1:
                # regular fund number
                title = get_page_title_from_link(fund_num_cell, wiki_spreadsheet, archive_name)
                add_fund_page_if_necessary(ws, wiki_spreadsheet, archive_name, title, url, source_type, r, page_table)
            case _:
                for fund_id in fund_ids:
                    title = f"{archive_name}/{fund_id}"
                    add_fund_page_if_necessary(ws, wiki_spreadsheet, archive_name, title, url, source_type, r, page_table)

    return page_table


def process_fond_sheet(ws, wiki_spreadsheet, fund_name, change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    parent_name = get_parent_title(ws, wiki_spreadsheet, fund_name)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, fund_name, change_date_col, ref_date_col)
    label = general_page_label(parent_name, wiki_spreadsheet)
    url = get_cell_link(ws["D3"])
    add_page({
        "title": parent_name,
        "url": url,
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": "fond",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
        "doc_links": get_cell_value(ws["B4"]),
        "source_type": source_type,
        "parent": parent_title_no_ns(parent_name, wiki_spreadsheet, fund_name),
    }, page_table)

    #the header row can be 5, 6 or 7
    hdr_row = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, "A", [FIRST_FUND_HDR])
    if hdr_row is None:
        raise ValueError(f"No '{FIRST_FUND_HDR}' header found in sheet {parent_name}")

    for r in range(hdr_row + 1, ws.max_row + 1):
        cell = ws[f"A{r}"]
        if str(cell.value).startswith("="):
            break
        if cell.value:
            title = get_page_title_from_link(cell, wiki_spreadsheet, fund_name)
            if not title:
                _logger.warning(f"cannot determine page title: sheet='{ws.title}', row={r} (skipping)")
                continue
            label = general_page_label(title, wiki_spreadsheet)
            raw_url = get_cell_link(cell)
            add_page({
                "title": title,
                "url": get_page_url(title, raw_url, wiki_spreadsheet),
                "label": label,
                "seq_label": sequential_page_label(label),
                "level": "opus",
                "description": get_cell_value(ws[f"B{r}"]),
                "years": get_cell_value(ws[f"C{r}"]),
                "availability": get_cell_value(ws[f"D{r}"]),
                "source_type": source_type,
                "parent": parent_name,
                "comments": get_cell_value(ws[f"O{r}"]),
            }, page_table)
    return page_table


def process_opus_sheet(ws, wiki_spreadsheet, opus_name, change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    parent_name = get_parent_title(ws, wiki_spreadsheet, opus_name)
    label = general_page_label(parent_name, wiki_spreadsheet)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, opus_name, change_date_col, ref_date_col)
    url = get_cell_link(ws["D3"])
    curr_parent_title = parent_name
    try:
        curr_parent_title = parent_title_no_ns(parent_name, wiki_spreadsheet, opus_name)
    except ValueError as err:
        if "Unrecognized archive root" in str(err):
            source_type = "other"

    add_page({
        "title": parent_name,
        "url": url,
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": "opus",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
        "doc_links": get_cell_value(ws["B4"]),
        "parent": curr_parent_title,
        "source_type": source_type,
    }, page_table)

    #the header row can be 5, 6 or 7
    headers = [FIRST_OPUS_HDR1, FIRST_OPUS_HDR2, FIRST_OPUS_HDR3, FIRST_OPUS_HDR4, FIRST_OPUS_HDR5]
    case_num_col = "A"
    hdr_row = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, case_num_col, headers)
    if hdr_row is None:
        case_num_col = chr(ord(case_num_col) + 1)
        hdr_row = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, case_num_col, headers)
        if hdr_row is None:
            raise ValueError(f"No '{FIRST_OPUS_HDR1}' header found in sheet {parent_name}")

    #there are sometimes columns inserted between "process" and "Comments",
    #so we need to find the exact position of the latter one
    comments_col = find_header_in_row(ws, hdr_row, "G", "Z", "Comments")
    if comments_col is None:
        raise ValueError(f"No 'Comments' column found in sheet {parent_name}")

    for r in range(hdr_row + 1, ws.max_row + 1):
        curr_source_type = source_type
        case_num_cell = ws[f"{case_num_col}{r}"]
        case_descr_cell = ws[f"{chr(ord(case_num_col) + 1)}{r}"]
        if str(case_num_cell.value).startswith("="):
            break
        if not is_series_of_integer_like_numbers(str(case_num_cell.value)):
            # sometimes there is some text in the A column, like "INFORMATION AND INSTRUCTIONAL DEPARTMENT" -
            # we skip such lines
            continue
        raw_url = get_url_from_2_cells(case_num_cell, case_descr_cell)
        if raw_url == "":
            # no link found - cannot proceed
            continue

        if case_num_cell.value:
            title = get_case_title(raw_url, case_num_cell, wiki_spreadsheet, opus_name)
            if title is None:
                # If it is a comment, like "Index files linked at top of page" - skip this line
                continue

            label = general_page_label(title, wiki_spreadsheet)
            if label is None:
                curr_source_type = "other"
                label = title

            comments_cell_addr = f"{chr(comments_col)}{r}" #default f"G{r}"
            processor_cell_addr1 = f"{chr(comments_col+ 2)}{r}" #default f"I{r}"
            processor_cell_addr2 = f"{chr(comments_col+ 5)}{r}" #default f"L{r}"
            pages_processed_cell_addr1 = f"{chr(comments_col + 3)}{r}" #default f"J{r}"
            pages_processed_cell_addr2 = f"{chr(comments_col + 6)}{r}" #default f"M{r}"

            #title sanity check
            identical, modified_title = title_cell_val_identical(title, str(case_num_cell.value))
            if identical:
                import_message = ""
            else:
                case_id = get_case_id(case_num_cell.value)
                import_message = f"Case {case_id} links to {title}"
                title = modified_title
                _logger.warning(f"Opus {parent_name}: {import_message}")

            add_page({
                "title": title,
                "url": get_page_url(title, raw_url, wiki_spreadsheet),
                "label": label,
                "seq_label": sequential_page_label(label),
                "level": "case",
                "description": get_cell_value(ws[f"{chr(ord(case_num_col) + 1)}{r}"]),
                "years": get_cell_value(ws[f"{chr(ord(case_num_col) + 2)}{r}"]),
                "source_type": curr_source_type,
                "parent": parent_name,
                "doc_links": get_cell_link(ws[f"{chr(ord(case_num_col) + 1)}{r}"]),#B
                "doc_type": get_cell_value(ws[f"{chr(ord(case_num_col) + 3)}{r}"]),#D
                "content_code": get_cell_value(ws[f"{chr(ord(case_num_col) + 4)}{r}"]),#E
                "process_code": get_cell_value(ws[f"{chr(ord(case_num_col) + 5)}{r}"]),#F
                "availability": "linked" if get_cell_link(ws[f"A{r}"]) is not None else "unlinked",
                "import_message": import_message,
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
    fund_opus_attributes = []
    for i in range(start_ord, end_col):
        cell_addr = f"{chr(i)}{row}"
        contents = get_cell_value(sheet[cell_addr])
        if contents is None:
            continue
        num_nonempty_cells += 1
        fund_opus_attributes.append(str(contents))
    return num_nonempty_cells, fund_opus_attributes

def process_worksheets(worksheets, wiki_spreadsheet, archive_name, page_table=None):
    if not page_table:
        page_table = {}
    hdr_row = 1
    for sheet in worksheets:
        try:
            _logger.info(f"processing worksheet: {sheet.title}")
            change_date_col = find_header_in_row(sheet, hdr_row, "I", "Z", "change date:")
            if change_date_col is None:
                raise ValueError(f"No 'change date:' column, cannot proceed")
            ref_date_col = find_header_in_row(sheet, hdr_row, chr(change_date_col + 2), "Z", "reference date:")
            if ref_date_col is None:
                if archive_name == "DAHO" or archive_name == "DAOO":
                    # Juliana's recommendation for these specific spreadsheets
                    ref_date_col = change_date_col + 3
                    _logger.warning(f"No 'change date:' column, proceeding anyway with date")
                else:
                    raise ValueError(f"No 'reference date:' column")

            num_attributes, fund_opus_attributes = count_fund_opus_attributes(sheet, change_date_col)
            archive_unit_name = archive_name #unit is either archive, or fund, or opus
            for attribute in fund_opus_attributes:
                archive_unit_name = f"{archive_unit_name}/{attribute}"

            match num_attributes:
                case 2:
                    page_table = process_opus_sheet(sheet, wiki_spreadsheet, archive_unit_name, change_date_col, ref_date_col, page_table)
                case 1:
                    page_table = process_fond_sheet(sheet, wiki_spreadsheet, archive_unit_name, change_date_col, ref_date_col, page_table)
                case 0:
                    page_table = process_archive_sheet(sheet, wiki_spreadsheet, archive_unit_name, change_date_col, ref_date_col, page_table)
                case _:
                    raise ValueError(f"{num_attributes} for worksheet {sheet.title}")

        except Exception as e:
            e.args = (f"{e} | error in sheet {sheet.title}", *e.args[1:])
            raise  # re-raise same exception with modified message
    return page_table


def import_spreadsheet(sw_filepath):
    db = Database()
    workbook = load_workbook(sw_filepath)
    wiki_spreadsheet = "-wiki" in sw_filepath.lower()
    archive_name = "" # default for wiki, where we'll construct it later
    if not wiki_spreadsheet:
        last_slash_pos = max(sw_filepath.rfind('/'), sw_filepath.rfind('\\'))
        if last_slash_pos != -1:
            file_name = sw_filepath[last_slash_pos + 1:]
        else:
            file_name = sw_filepath
        archive_substring_position = file_name.find("-archive")
        if archive_substring_position != -1:
            archive_name = file_name[0 : archive_substring_position]
        else:
            _logger.error(f"Spreadsheet file name {file_name} contains neither 'wiki' nor 'archive'")
            return

    page_data = process_worksheets(workbook, wiki_spreadsheet, archive_name)

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

        # upload the page record
        page_payload = {k: v for k, v in page.items() if k not in doc_only_fields}
        #page_payload["import_message"] = "" # clear any pre-existing import messages
        abort_page = False
        while True:
            try:
                # attempt to encode page payload to verify correctness
                db.encode_records("Pages", [ page_payload ])
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
        parent_name = page.get("parent")
        if parent_name:
            parent_page = parent_dict.get(parent_name)
            if not parent_page:
                parent_page = page_dict.get(parent_name)
            if parent_page:
                parent_dict[parent_name] = parent_page
                child_ids = parent_page.get("child_ids", [])
                child_ids.append(page["Id"])
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
                _logger.info(msg)
                f.write(msg + "\n")
                try:
                    import_spreadsheet(str(entry))
                except Exception as e:
                    error_msg = f"Error processing {entry}: {e}\n"
                    f.write(error_msg)
                    _logger.info(f"{Fore.RED}{error_msg}{Fore.RESET}")
                end = time.perf_counter()
                f.write(f"Elapsed: {end - start:.2f} seconds\n")
                f.flush()

#testing
if __name__ == "__main__":
#    filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/Archives/DAHEO-D-archive-202501009.xlsx"
    filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/wiki/ImportProblems/DAVIO-D-wiki+other-20260213.xlsx"
    import_spreadsheet(filepath)
#    dir_path = "C:/jewishGen/Import2DB/SourceSpreadsheets/wiki"
#    process_dir(dir_path)