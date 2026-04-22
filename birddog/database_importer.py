from birddog.abstract_database import InvalidFieldValue
from birddog.database import Database, timer
from birddog.database_updater import form_document_record, normalize_url
from birddog.log import get_logger
from birddog.utility import utc_now_dt
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
from typing import List, Any
from urllib.parse import unquote, urlparse, parse_qs
import os
import re
import time

_ENABLE_CONSOLE_HIGHLIGHTING = os.name == "nt"# console output highlighting
START_HDR_ROW = 5 #the headers are sometimes in row 5 and sometimes in row 6 or 7
END_HDR_ROW = 7
FIRST_OPUS_HDR1 = "Case #, link".upper()
FIRST_OPUS_HDR2 = "P#, link".upper()
FIRST_OPUS_HDR3 = "Index #, link".upper()
FIRST_OPUS_HDR4 = "NEW NUMBERING".upper()
FIRST_OPUS_HDR5 = "#".upper()
FIRST_FUND_HDR = "Opus #, link".upper()
SECOND_VOLUME_HDR = "Other case #".upper()
AVAILABILITY_HDR = "Availability".upper()
SRC_HDR = "source:".upper()
COMMENTS_HDR = "Comments".upper()
CHANGE_DATE_HDR = "change date:".upper()
REF_DATE_HDR = "reference date:".upper()
RED_SUBSTRING = "?action=edit&redlink=1"
_NS_PREFIX = f"{WIKI_NAMESPACE}:"

DESCRIPTION_COL_OFFS = 1
YEARS_COL_OFFS = 2
DOC_TYPE_COL_OFFS = 3
CONTENT_CODE_COL_OFFS = 4
PROCESS_CODE_COL_OFFS = 5

if _ENABLE_CONSOLE_HIGHLIGHTING:
    from colorama import Fore, init
    init()  # Windows fix
else:
    # stub out highlight directives
    class Fore:
        RESET = ""
        RED = ""
        GREEN = ""

_logger = get_logger()

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

def get_case_id(cell_contents) -> str:
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

def parse_hyphen_triplet(s: str):
    parts = s.split('-')
    if len(parts) != 3:
        return [], False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return [], False
    return nums, True

def get_case_title(url, curr_parent_title, opus_name, cell, wiki_spreadsheet,
                   possible_hyphen_in_case_num, archive_unit_name):
    if wiki_spreadsheet:
        if possible_hyphen_in_case_num:
            # sometimes instead of a case number we have (fund)-(opus)-(case)
            three_numbers, is_triplet = parse_hyphen_triplet(cell.value)
            if is_triplet:
                return f"{curr_parent_title}/{three_numbers[1]}/{three_numbers[2]}"

        title = url_to_title(url)
        if curr_parent_title not in title:
            value = cell.value
            if isinstance(value, float) and value.is_integer() or isinstance(value, int):
                case_id = get_case_id(value)
                title = f"{curr_parent_title}/{opus_name}/{case_id}"

        return title

    else:
        return cell_to_title(cell, archive_unit_name)

def get_cell_link(cell):
    return unquote(cell.hyperlink.target) if cell.hyperlink else ""

def get_cell_value(cell, capitalized=False):
    value = cell.value
    if value is None:
        return value
    if isinstance(value, float):
        value = int(value)
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if capitalized:
        value = value.upper()

    return value

def get_cell_link_or_str(cell):
    result = get_cell_link(cell)
    if result == "":
        result = get_cell_value(cell)
    return result

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

def check_redlink(page: dict) -> dict:
    availability = page.get("availability", "linked")
    url = page["url"]
    index = url.find(RED_SUBSTRING)
    if index != -1:
        # if the URL contains a red_substring, availability status is "redlinked"
        url = url[:index]
        availability = "redlinked"
        page["url"] = url
    page["availability"] = availability

    return page

def add_page(page: dict, page_table: dict) -> None:
    """Merge a page entry into the page_table by title."""
    #(re)set some fields
    page = check_redlink(page)
    page["url"] = normalize_url(page["url"])
    page["last_imported"] = str(utc_now_dt().replace(microsecond=0))

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
    normalized_val = normalize_to_int_str(str(cell.value))
    match num_results:
        case 1:
            #default result - do nothing
            return
        case 0:
            if cell.value is None or len(str(cell.value)) == 0:
                #empty cell - do nothing
                return
            _logger.warning(f"No URl found for the fund '{normalized_val}', sheet='{ws.title}', row={r} - skipping")
        case _:
            _logger.warning(f"Irregular fund number '{normalized_val}', sheet='{ws.title}', "
                            f"row={r} - splitting to {subunits}")

def get_url_from_2_cells(unit_num_cell, unit_descr_cell) -> tuple[str, bool]:
    cell_to_use = unit_num_cell
    in_first_cell = True
    if cell_to_use.hyperlink is None:
        cell_to_use = unit_descr_cell
        in_first_cell = False
    if cell_to_use.hyperlink is None:
        return "", in_first_cell

    hyperlink = cell_to_use.hyperlink
    url = unquote(hyperlink.target)
#    if hyperlink.location is not None:
#        url = f"{url}#{hyperlink.location}"
    return url, in_first_cell


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

    url, _ = get_url_from_2_cells(fund_num_cell, fund_descr_cell)
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


def is_series_of_case_numbers(s: str, ignore_parts: bool) -> tuple[bool, int, bool]:
    """Case number should include digits but may also include letters.
    Case numbers are separated by hyphens."""
    inconsistent_parts = False
    if not isinstance(s, str) or not s:
        return False, 0, inconsistent_parts

    parts = s.split('-')
    num_parts = len(parts)
    if any(part == '' for part in parts):
        return False, num_parts, inconsistent_parts

    only_integers_found = False
    integers_and_letters_found = False
    for part in parts:
        if re.fullmatch(r'\d+', part):
            only_integers_found = True
            continue
        if re.fullmatch(r'\d+\.\d+', part) and float(part).is_integer():
            only_integers_found = True
            continue
        if any(ch.isdigit() for ch in part):
            integers_and_letters_found = True
            continue
        return False, num_parts, inconsistent_parts

    if ignore_parts:
        return True, 1, False

    inconsistent_parts = only_integers_found and integers_and_letters_found
    if inconsistent_parts:
        #DAOO-D-wiki-20260413 sheet DAOO 1-191 contains case number 1-T2
        return True, 1, inconsistent_parts

    return True, num_parts, inconsistent_parts

def find_header_in_row(ws, row, start_col, end_col, header):
    """Find a cell with the given header in the given row of the given worksheet.
    Returns the column if found, None otherwise."""
    start_ord = ord(start_col)
    end_ord = ord(end_col)
    for i in range(start_ord, end_ord + 1):
        cell_addr = f"{chr(i)}{row}"
        contents = get_cell_value(ws[cell_addr], True)
        if contents is None:
            continue
        if contents == header:
            return i
    return None

def find_header_in_col(ws, start_row, end_row, col, headers):
    """Find a cell with one of the given headers in the given column of the given worksheet.
    Returns 1) the row if found, None otherwise.
            2) The header found """
    for header in headers:
        for i in range(start_row, end_row + 1):
            cell_addr = f"{col}{i}"
            contents = get_cell_value(ws[cell_addr], True)
            if contents is None:
                continue
            if contents == header:
                return i, header
            if header == FIRST_OPUS_HDR4 and header in contents:
                # it can be something like "NEW NUMBERING (listed here 06/2025)"
                return i, header
    return None, None

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
        source_col = find_header_in_row(ws, row, "B", "Z", SRC_HDR)
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


def title_cell_val_identical(title: str, cell_val: str, num_parts: int, inconsistent_parts: bool) -> tuple[bool, str]:
    """Returns everything after the last '/' or original string if no slash."""
    title_after_last_slash = title.rsplit('/', 1)[-1] if '/' in title else title
    title_after_last_slash = cyrillic_to_latin(title_after_last_slash)
    cell_val_posit_int_cyr = to_positive_int_str(cell_val)
    cell_val_posit_int_lat = cyrillic_to_latin(cell_val_posit_int_cyr)
    identical = title_after_last_slash == cell_val_posit_int_lat
    if not identical and num_parts == 3:
        #maybe it is of format fund-opus-case
        parts = cell_val.split('-')
        all_parts_found = True
        for part in parts:
            if -1 == title.find(part):
                all_parts_found = False
                break
        if all_parts_found:
            identical = True

    modified_title = title
    if not identical and not inconsistent_parts:
        modified_title = title.rsplit('/', 1)[0] + "/" + cell_val_posit_int_cyr if '/' in title \
            else cell_val_posit_int_cyr
    return identical, modified_title

def general_page_label(title, wiki_spreadsheet):
    if wiki_spreadsheet:
        return page_label(title)
    else:
        return title

def get_page_url(title: str, url: str, change_url: bool, import_message: str = "")->tuple[str,str]:
    if change_url:
        changed_url = page_url_from_title(title)
        if changed_url != url:
            mess = f"URL changed from {url} to {changed_url}"
            if import_message == "":
                import_message = mess
            else:
                import_message = import_message + "; " + mess
        return changed_url, import_message
    else:
        return url, import_message


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
        if contents is not None and any(ch.isdigit() for ch in contents):
            #in DAHO-D-wiki-20260324.xlsx sheet "DAHO D wiki fund list" we have text
            # "to be updated when BirdDog not adding incorect links"
            num_nonempty_cells += 1
            fund_opus_attributes.append(str(contents))
    return num_nonempty_cells, fund_opus_attributes

def sheet_contains_volumes(ws):
    #the header row can be 5, 6 or 7
    headers = [FIRST_FUND_HDR]
    case_num_col = "A"
    hdr_row, _ = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, case_num_col, headers)
    if hdr_row is None:
        case_num_col = chr(ord(case_num_col) + 1)
        hdr_row, _ = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, case_num_col, headers)
        if hdr_row is None:
            # in DAHMO-R-wiki-20260415.xlsx sheet DAHMO R-6193-12 summary the headers are "Case #, link" in column A
            # and then "Availability" in column D
            headers = [AVAILABILITY_HDR]
            hdr_row, _ = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, chr(ord(case_num_col) + 2), headers)
            if hdr_row is None:
                return False, case_num_col, 0

    return True, case_num_col, hdr_row


def case_like_headers_in_row(ws, opus_title, opus_name):
    #the header row can be 5, 6 or 7
    headers = [FIRST_OPUS_HDR1, FIRST_OPUS_HDR2, FIRST_OPUS_HDR3, FIRST_OPUS_HDR4, FIRST_OPUS_HDR5]
    case_num_col = "A"
    hdr_row, header_found = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, case_num_col, headers)
    import_message = ""
    possible_hyphen_in_case_num = False
    old_numbering_column = "A"
    case_amount_column = case_num_col
    contains_volumes = False
    additional_column = False
    if hdr_row is None:
        case_num_col = chr(ord(case_num_col) + 1)
        case_amount_column = case_num_col
        hdr_row, header_found = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, case_num_col, headers)
        if hdr_row is None:
            #could it be a sheet with volumes and not cases?
            contains_volumes, case_num_col, hdr_row = sheet_contains_volumes(ws)
            if contains_volumes:
                import_message = f"Opus {opus_title} contains volumes"
                _logger.warning(import_message)
            else:
                raise ValueError(f"No '{FIRST_OPUS_HDR1}' header found in sheet {opus_name}")
    elif header_found == FIRST_OPUS_HDR1:
        # in DAHMO-R-wiki-20260415.xlsx sheet DAHMO R-6193-12 summary the headers are "Case #, link" in column A
        # and then "Availability" in column D
        headers = [AVAILABILITY_HDR]
        availability_hdr_row, _ = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, chr(ord(case_num_col) + 3), headers)
        if availability_hdr_row is not None:
            contains_volumes = True

    if header_found == FIRST_OPUS_HDR4:
        possible_hyphen_in_case_num = True
        old_numbering_column = chr(ord(case_num_col) - 1)
        case_amount_column = old_numbering_column
        import_message = "New case numbering"

    if header_found == FIRST_OPUS_HDR3:
        #in DAHMO-R-wiki-20260415.xlsx sheet R-6193-12 cases 5341-10422 there is an extra column "Other case #"
        cell_addr = f"{"B"}{hdr_row}"
        contents = get_cell_value(ws[cell_addr], True)
        if SECOND_VOLUME_HDR == contents:
            additional_column = True

    return (possible_hyphen_in_case_num, case_amount_column, case_num_col, hdr_row, import_message,
            old_numbering_column, contains_volumes, additional_column)


def add_opus_page(page_table, ws, r, title, url, label, change_date, timestamp, source_type, parent_name,
                  level, availability, import_message=""):
    add_page({
        "title": title,
        "url": url,
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": level,
        "change_date": change_date,
        "timestamp": timestamp,
        "description": get_cell_value(ws[f"B{r}"]),
        "years": get_cell_value(ws[f"C{r}"]),
        "availability": availability,
        "source_type": source_type,
        "parent": parent_name,
        "comments": get_cell_value(ws[f"O{r}"]),
        "import_message": import_message,
    }, page_table)


def add_fund_page_if_necessary(ws, wiki_spreadsheet, archive_title, title, url,
                               source_type, r, page_table, change_date, timestamp):
    """ We only add linked pages or pages with comments."""
    availability = get_cell_value(ws[f"D{r}"])

    comments = get_cell_value(ws[f"O{r}"])
    if availability != 'linked' and comments is None:
        # we do not add such pages
        return

    label = general_page_label(title, wiki_spreadsheet)
    url, import_message = get_page_url(title, url, wiki_spreadsheet)
    add_page({
        "title": title,
        "url": url,
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": "fond",
        "change_date": change_date,
        "timestamp": timestamp,
        "description": get_cell_value(ws[f"B{r}"]),
        "years": get_cell_value(ws[f"C{r}"]),
        "availability": availability,
        "source_type": source_type,
        "parent": archive_title,
        "import_message": import_message,
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
        "doc_links": get_cell_link_or_str(ws["B4"]),
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
                add_fund_page_if_necessary(ws, wiki_spreadsheet, archive_name, title, url, source_type, r,
                                           page_table, change_date, timestamp)
            case _:
                for fund_id in fund_ids:
                    title = f"{archive_name}/{fund_id}"
                    add_fund_page_if_necessary(ws, wiki_spreadsheet, archive_name, title, url, source_type, r,
                                               page_table, change_date, timestamp)

    return page_table


def process_fond_sheet(ws, wiki_spreadsheet, fund_name, change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    parent_name = get_parent_title(ws, wiki_spreadsheet, fund_name)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, fund_name, change_date_col, ref_date_col)
    label = general_page_label(parent_name, wiki_spreadsheet)

    add_page({
        "title": parent_name,
        "url": get_cell_link(ws["D3"]),
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": "fond",
        "description": get_cell_value(ws["A2"]),
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
        "doc_links": get_cell_link_or_str(ws["B4"]),
        "source_type": source_type,
        "parent": parent_title_no_ns(parent_name, wiki_spreadsheet, fund_name),
    }, page_table)

    #the header row can be 5, 6 or 7
    hdr_row, _ = find_header_in_col(ws, START_HDR_ROW, END_HDR_ROW, "A", [FIRST_FUND_HDR])
    if hdr_row is None:
        raise ValueError(f"No '{FIRST_FUND_HDR}' header found in sheet {parent_name}")

    for r in range(hdr_row + 1, ws.max_row + 1):
        opus_num_cell = ws[f"A{r}"]
        opus_descr_cell = ws[f"B{r}"]
        if str(opus_num_cell.value).startswith("="):
            break
        if opus_num_cell.value:
            curr_import_message = ""
            availability = "linked" #default
            raw_url, in_first_cell = get_url_from_2_cells(opus_num_cell, opus_descr_cell)
            if raw_url == "":
                # no link found - cannot proceed
                _logger.warning(f"No link in sheet='{ws.title}', opus {get_cell_value(opus_num_cell)} - skipping")
                continue
            if not in_first_cell:
                availability = "unlinked"
                _logger.warning(f"Opus {get_cell_value(opus_num_cell)} in sheet='{ws.title}', "
                                f"row={r} has no page but has a document")
                curr_import_message = "Document without a page"
            title = get_page_title_from_link(opus_num_cell, wiki_spreadsheet, fund_name)
            if not title:
                _logger.warning(f"cannot determine page title: sheet='{ws.title}', row={r}  - skipping")
                continue

            url, curr_import_message = get_page_url(title, raw_url, wiki_spreadsheet, curr_import_message)
            label = general_page_label(title, wiki_spreadsheet)
            add_opus_page(page_table, ws, r, title, url, label, change_date, timestamp,
                          source_type, parent_name, "opus", availability, curr_import_message)
    return page_table

def process_opus_sheet(ws, wiki_spreadsheet, fund_and_opus_name, opus_name,
                       change_date_col, ref_date_col, page_table=None):
    if not page_table:
        page_table = {}
    opus_title = get_parent_title(ws, wiki_spreadsheet, fund_and_opus_name)
    label = general_page_label(opus_title, wiki_spreadsheet)
    source_type = get_source_type(ws)
    change_date, timestamp = get_dates(ws, fund_and_opus_name, change_date_col, ref_date_col)
    url = get_cell_link(ws["D3"])
    curr_parent_title = opus_title
    try:
        curr_parent_title = parent_title_no_ns(opus_title, wiki_spreadsheet, fund_and_opus_name)
    except ValueError as err:
        if "Unrecognized archive root" in str(err):
            source_type = "other"

    (possible_hyphen_in_case_num, case_amount_column, case_num_col, hdr_row, import_message,
     old_numbering_column, contains_volumes, additional_column) = case_like_headers_in_row(ws, opus_title, opus_name)

    #there are sometimes columns inserted between "process" and "Comments",
    #so we need to find the exact position of the latter one
    comments_col = find_header_in_row(ws, hdr_row, "G", "Z", COMMENTS_HDR)
    if comments_col is None:
        raise ValueError(f"No 'Comments' column found in sheet {opus_title}")
    description = get_cell_value(ws["A2"])

    add_page({
        "title": opus_title,
        "url": url,
        "label": label,
        "seq_label": sequential_page_label(label),
        "level": "opus",
        "description": description,
        "availability": "linked",
        "change_date": change_date,
        "timestamp": timestamp,
        "doc_links": get_cell_link_or_str(ws["B4"]),
        "parent": curr_parent_title,
        "source_type": source_type,
    }, page_table)

    usual_case_number_found = False
    for r in range(hdr_row + 1, ws.max_row + 1):
        curr_source_type = source_type
        case_num_cell = ws[f"{case_num_col}{r}"]
        case_descr_cell = ws[f"{chr(ord(case_num_col) + 1)}{r}"]
        case_amount_cell = ws[f"{case_amount_column}{r}"]
        if str(case_amount_cell.value).startswith("="):
            break
        curr_import_message = ""
        ignore_parts = usual_case_number_found and not possible_hyphen_in_case_num
        case_number_series, num_parts, inconsistent_parts = is_series_of_case_numbers(str(case_num_cell.value), ignore_parts)
        if not case_number_series:
            if possible_hyphen_in_case_num:
                curr_import_message = "Old case numbering"
                case_num_cell = ws[f"{old_numbering_column}{r}"]
                case_number_series, num_parts, inconsistent_parts = is_series_of_case_numbers(str(case_num_cell.value),
                                                                                              ignore_parts)
            if not case_number_series:
                # sometimes there is some text in the A column, like "INFORMATION AND INSTRUCTIONAL DEPARTMENT" -
                # we skip such lines
                continue
        level = "volume" if contains_volumes or num_parts == 2 else "case"
        if num_parts == 1:
            usual_case_number_found = True

        raw_url, in_first_cell = get_url_from_2_cells(case_num_cell, case_descr_cell)
        if raw_url == "":
            # no link found - cannot proceed
            continue
        if not in_first_cell and case_amount_column != case_num_col:
            case_amount_cell = ws[f"{case_amount_column}{r}"]
            if case_amount_cell.hyperlink is not None:
                #CDIAK-wiki-20260409.xlsx sheet CDIAK 1164-1 contains old and new case numbering and the case link
                # may be in both columns
                hyperlink = case_amount_cell.hyperlink
                raw_url = unquote(hyperlink.target)
                in_first_cell = True
        if not in_first_cell:
            if additional_column:
                curr_import_message = "Other case numbering"
            else:
                curr_import_message = "Document without a page"

        if case_num_cell.value:
            title = get_case_title(raw_url, curr_parent_title, opus_name, case_num_cell, wiki_spreadsheet,
                                   possible_hyphen_in_case_num, fund_and_opus_name)
            if title is None:
                # If it is a comment, like "Index files linked at top of page" - skip this line
                continue

            label = general_page_label(title, wiki_spreadsheet)
            if label is None:
                curr_source_type = "other"
                label = title

            #title sanity check
            identical, modified_title = title_cell_val_identical(title, str(case_num_cell.value),
                                                                 num_parts, inconsistent_parts)
            if identical:
                if curr_import_message == "" and level == "case":
                    curr_import_message = import_message
            else:
                case_id = get_case_id(case_num_cell.value)
                new_message = f"{level} {case_id} links to {title}"
                if curr_import_message == "":
                    curr_import_message = new_message
                    if import_message != "" and level == "case":
                        curr_import_message = f"{import_message}; {curr_import_message}"
                else:
                    curr_import_message = curr_import_message + ": " + new_message

                if not possible_hyphen_in_case_num and level == "case":
                    title = modified_title
                _logger.warning(f"{curr_import_message}")

            url, curr_import_message = get_page_url(title, raw_url,
                                                    wiki_spreadsheet and not in_first_cell, curr_import_message)
            if level == "volume":
                if possible_hyphen_in_case_num:
                    availability = get_cell_value(ws[f"D{r}"])
                else:
                    availability = "linked"
                if "" == curr_import_message:
                    curr_import_message = f"Volume in {title}"
                add_opus_page(page_table, ws, r, title, url, label, change_date, timestamp, source_type, opus_title,
                              level, availability, curr_import_message)
            else:
                comments_cell_addr = f"{chr(comments_col)}{r}" #default f"G{r}"
                processor_cell_addr1 = f"{chr(comments_col+ 2)}{r}" #default f"I{r}"
                processor_cell_addr2 = f"{chr(comments_col+ 5)}{r}" #default f"L{r}"
                pages_processed_cell_addr1 = f"{chr(comments_col + 3)}{r}" #default f"J{r}"
                pages_processed_cell_addr2 = f"{chr(comments_col + 6)}{r}"  # default f"M{r}"

                description_col_offs = DESCRIPTION_COL_OFFS
                years_col_offs = YEARS_COL_OFFS
                doc_type_col_offs = DOC_TYPE_COL_OFFS
                content_code_col_offs = CONTENT_CODE_COL_OFFS
                process_code_col_offs = PROCESS_CODE_COL_OFFS
                if additional_column:
                    description_col_offs = description_col_offs + 1
                    years_col_offs = years_col_offs + 1
                    doc_type_col_offs = doc_type_col_offs + 1
                    content_code_col_offs = content_code_col_offs + 1
                    process_code_col_offs = process_code_col_offs + 1

                process_code = get_cell_value(ws[f"{chr(ord(case_num_col) + process_code_col_offs)}{r}"], True)  # F
                # Remove trailing tab ('\\t') from the given string if present.
                if process_code is not None:
                    if process_code.endswith('\t'):
                        process_code = process_code[:-1]

                add_page({
                    "title": title,
                    "url": url,
                    "label": label,
                    "seq_label": sequential_page_label(label),
                    "level": level,
                    "change_date": change_date,
                    "timestamp": timestamp,
                    "description": get_cell_value(ws[f"{chr(ord(case_num_col) + description_col_offs)}{r}"]),
                    "years": get_cell_value(ws[f"{chr(ord(case_num_col) + years_col_offs)}{r}"]),
                    "source_type": curr_source_type,
                    "parent": opus_title,
                    "doc_links": get_cell_link(ws[f"{chr(ord(case_num_col) + 1)}{r}"]),#B
                    "doc_type": get_cell_value(ws[f"{chr(ord(case_num_col) + doc_type_col_offs)}{r}"], True),#D
                    "content_code": get_cell_value(ws[f"{chr(ord(case_num_col) + content_code_col_offs)}{r}"], True),#E
                    "process_code": process_code,
                    "import_message": curr_import_message,
                    #the columns for the following cells are not fixed
                    "comments": get_cell_value(ws[comments_cell_addr]),
                    "processor": combine_cell_value(ws[processor_cell_addr1], ws[processor_cell_addr2]),
                    "pages_processed": get_cell_int_value(ws[pages_processed_cell_addr1]) +
                                       get_cell_int_value(ws[pages_processed_cell_addr2]),
                }, page_table)

    return page_table

def process_worksheets(worksheets, wiki_spreadsheet, archive_name, page_table=None):
    if not page_table:
        page_table = {}
    hdr_row = 1
    for sheet in worksheets:
        try:
            _logger.info(f"processing worksheet: {sheet.title}")
            change_date_col = find_header_in_row(sheet, hdr_row, "I", "Z", CHANGE_DATE_HDR)
            if change_date_col is None:
                raise ValueError(f"No 'change date:' column, cannot proceed")
            ref_date_col = find_header_in_row(sheet, hdr_row, chr(change_date_col + 2), "Z", REF_DATE_HDR)
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
                    opus_name = fund_opus_attributes[-1]
                    page_table = process_opus_sheet(sheet, wiki_spreadsheet, archive_unit_name, opus_name,
                                                    change_date_col, ref_date_col, page_table)
                case 1:
                    page_table = process_fond_sheet(sheet, wiki_spreadsheet, archive_unit_name,
                                                    change_date_col, ref_date_col, page_table)
                case 0:
                    page_table = process_archive_sheet(sheet, wiki_spreadsheet, archive_unit_name,
                                                       change_date_col, ref_date_col, page_table)
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

    inp_page_data = process_worksheets(workbook, wiki_spreadsheet, archive_name)
    if not isinstance(inp_page_data, dict):
        raise TypeError("inp_page_data is not a dictionary")
    page_data = list(inp_page_data.values())

    doc_only_fields_all_caps = {
        "doc_type",
        "content_code",
        "process_code",
    }
    doc_only_fields = doc_only_fields_all_caps | {
        "pages_processed",
        "processor",
    }

    doc_and_page_fields = {
        "comments",
        "timestamp",
        "availability",
        "import_message",
        "last_imported",
#        "description",
    }

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
                _logger.error(f"malformed value: {err}, title: {page_payload["title"]}")
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

                doc_payload = {}
                for doc_field in doc_only_fields_all_caps:
                    if doc_field in doc_payload and doc_payload[doc_field] is not None:
                        doc_payload[doc_field] = doc_payload[doc_field].strip().upper()
                for joint_field in doc_and_page_fields:
                    if joint_field in page:
                        doc_payload[joint_field] = page[joint_field]

                doc_payload["title"] = quoted_record["title"]
                link = quoted_record["url"]
                doc_payload["url"] = link

                relevant_page_data = inp_page_data[page["title"]]
                for k in doc_only_fields:
                    if k in relevant_page_data:
                        doc_payload[k] = relevant_page_data[k]

                output_docs[link] = doc_payload
                linked_docs = doc_link_dict.get(page["title"], [])
                linked_docs.append(doc_payload)
                doc_link_dict[page["title"]] = linked_docs

    # Step 7: write doc records to db
    all_docs = list(output_docs.values())
    _logger.info(f"Writing {len(all_docs)} doc records")

    doc_ids = db.write("Documents", all_docs)
    for doc_id, doc in zip(doc_ids, all_docs):
        output_docs[doc["url"]]["Id"] = doc_id

    # Step 8: link pages to docs
    for page_title, linked_docs in doc_link_dict.items():
        page_id = page_dict[page_title]["Id"]
        linked_ids = [output_docs[d["url"]]["Id"] for d in doc_link_dict[page_title]]
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
#    filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/Archives/ImportProblems/DAHMO-K-wiki-20250820.xlsx"
    filepath = "C:/jewishGen/Import2DB/SourceSpreadsheets/wiki/DAKO-D-general-wiki-20260417.xlsx"
    import_spreadsheet(filepath)
#   dir_path = "C:/jewishGen/Import2DB/SourceSpreadsheets/wiki"
#   process_dir(dir_path)