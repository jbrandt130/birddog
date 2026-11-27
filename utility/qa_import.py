#!/usr/bin/env python3
"""
QA check of a spreadsheet import: read the spreadsheets independently of the
importer and report what the database is missing or holds differently.

The expectations come from the import requirements ("EXPECTED FROM SPREADSHEET
IMPORTS"):
  - every row of every sheet becomes a record, including rows with only a
    comment or with no link, and each row gets its own record
  - case rows keep columns B (title, link) and D..O; a row without a file
    link keeps D..O on a self-link record (the case page's own url)
  - all sheets: A2 (full description, kept in Pages.OLD_description where the
    base has it), B4 (as text, all links), D3 (all links), I3 (comment); other
    header links are reported
  - overview and fund sheets: column O comments
  - opus sheets: column G comments; column N (when transcribed) as the text
    shown in the cell, not as a date

Records are matched by url: the page by the row's case/opus/fund link (column
A), the document by the description link (column B). Nothing is written.

Usage (from anywhere; checks LOCAL unless --stage or --prod is given):
    venv/bin/python utility/qa_import.py "var/WAT/DACHKO (Cherkassy)"/DACHKO-*.xlsx
    venv/bin/python utility/qa_import.py --prod --csv qa.csv FILE.xlsx ...
"""

import argparse
import csv
import datetime
import os
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import parse_qs, unquote, urlparse

from openpyxl import load_workbook

from rename_field import REPO_ROOT, connect

SHEET_KINDS = {"Fund": "overview", "Opus": "fund", "Case": "opus"}
HEADER_ROWS = range(4, 10)
URL_RE = re.compile(r"https?://[^\s,;]+")
_COMMONS_UPLOAD_RE = re.compile(
    r"^https?://upload\.wikimedia\.org/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/?#]+)")
PAGE_FIELDS = ["Id", "title", "url", "level", "description", "OLD_description", "years", "comments",
               "import_message", "availability", "root_label", "doc_links"]
DOC_FIELDS = ["Id", "title", "url", "wikisource_url", "OLD_doc_type", "OLD_content_code",
              "OLD_process_code", "comments", "filename", "processor", "pages_processed",
              "when_transcribed", "language", "description", "years", "import_message"]
# opus sheet columns by header text; the two "Processor"/"Pages processed"
# headers are told apart by position (first = priority file, second = other file)
OPUS_HEADERS = {"Doc type": "doc_type", "Content": "content", "Process": "process",
                "Comments": "comments", "Priority file name": "file1", "Other file name": "file2",
                "Processor": "processor", "Pages processed": "pages",
                "When transcribed": "transcribed", "Extra language(s)": "language"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Check an import against its spreadsheets.")
    parser.add_argument("spreadsheets", nargs="+", help="xlsx files that were imported")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--local", dest="env", action="store_const", const="LOCAL",
                        help="local dev base (the default)")
    target.add_argument("--stage", dest="env", action="store_const", const="CLOUD_STAGE",
                        help="NocoDB Cloud staging base")
    target.add_argument("--prod", dest="env", action="store_const", const="CLOUD_PROD",
                        help="NocoDB Cloud production base")
    parser.set_defaults(env="LOCAL")
    parser.add_argument("--root", help="root_label of the archive (default: from the file name)")
    parser.add_argument("--csv", help="write every finding to this CSV file")
    parser.add_argument("--examples", type=int, default=5, help="examples shown per finding type")
    return parser.parse_args(argv)


# ---------------------------------------------------------------- cell values

def text(value):
    """A cell value as the importer-independent text a reader sees: integral
    floats without ".0", strings stripped, None as ""."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def displayed_date(value, number_format):
    """The text Excel shows for a date cell, for the formats the spreadsheets use
    (mmm yyyy, d mmm yyyy, yyyy, dd.mm.yyyy, ...); None for an unknown format."""
    fmt = (number_format or "").lower().replace("\\", "").replace('"', "")
    fmt = fmt.split(";")[0]
    if not isinstance(value, (datetime.date, datetime.datetime)):
        return None
    tokens = re.findall(r"y+|m+|d+|[^ymd]+", fmt)
    if not tokens:
        return None
    out = []
    for t in tokens:
        if t.startswith("y"):
            out.append(f"{value.year:04d}" if len(t) > 2 else f"{value.year % 100:02d}")
        elif t.startswith("m"):
            if len(t) >= 4:
                out.append(value.strftime("%B"))
            elif len(t) == 3:
                out.append(_MONTHS[value.month - 1])
            else:
                out.append(f"{value.month:02d}" if len(t) == 2 else str(value.month))
        elif t.startswith("d"):
            out.append(f"{value.day:02d}" if len(t) == 2 else str(value.day))
        else:
            out.append(t)
    return "".join(out)


def cell_links(cell):
    """Every url a cell holds: its hyperlink target plus urls typed in its text."""
    links = []
    if cell.hyperlink and cell.hyperlink.target:
        # kept percent-encoded: an encoded "?" in a file name must not turn
        # into a query string (the url key decodes the rest)
        links.append(cell.hyperlink.target.strip())
    for m in URL_RE.findall(text(cell.value)):
        link = m.rstrip(".)")
        if unquote(link) not in [unquote(x) for x in links]:
            links.append(link)
    return links


def non_link_text(cell):
    """Text of a cell left once its urls (and separators between them) are removed."""
    rest = URL_RE.sub(" ", text(cell.value))
    rest = re.sub(r"(?i)\b(and)\b|[,;]", " ", rest)
    return " ".join(rest.split())


# ---------------------------------------------------------------- database

def wiki_url(url):
    """A wiki link without its red link marker, an index.php?title=X link as /wiki/X."""
    url = url.strip().split("?action=edit")[0].split("&action=edit")[0]
    parsed = urlparse(url)
    if parsed.path.endswith("/index.php"):
        title = parse_qs(parsed.query).get("title")
        if title:
            return f"{parsed.scheme}://{parsed.netloc}/wiki/{title[0]}"
    if "/wiki/" in url:
        url = url.replace("?", "%3F")  # a literal "?" in a title, e.g. "(18??).pdf"
    match = _COMMONS_UPLOAD_RE.match(unquote(url))
    if match:  # a raw upload link names its commons file page
        url = f"https://commons.wikimedia.org/wiki/File:{match.group(1).replace(' ', '_')}"
    return url


class Index:
    """Pages and Documents of the archive, by normalized url."""

    def __init__(self, normalize):
        self.normalize = normalize
        self.pages = {}
        self.docs = {}

    def key(self, url):
        if not url:
            return ""
        try:
            return self.normalize(wiki_url(url))
        except Exception:  # noqa: BLE001 -- a malformed url just doesn't match
            return url

    def add_page(self, record):
        self.pages[self.key(record.get("url"))] = record

    def add_doc(self, record):
        for url in (record.get("url"), record.get("wikisource_url")):
            if url:
                self.docs[self.key(url)] = record

    def page(self, url):
        return self.pages.get(self.key(url))

    def doc(self, url):
        return self.docs.get(self.key(url))


def _scan(db, table, where, fields):
    cursor = None
    while True:
        records, cursor = db.scan(table, limit=1000, cursor=cursor, where=where, fields=fields)
        yield from records
        if not cursor:
            break


def load_index(db, root_label, normalize):
    index = Index(normalize)
    page_fields = ((getattr(db, "_schema", None) or {}).get("Pages") or {}).get("fields") or {}
    if "OLD_description" not in page_fields and "OLD_description" in PAGE_FIELDS:
        PAGE_FIELDS.remove("OLD_description")
    doc_ids = set()
    for page in _scan(db, "Pages", ("root_label", "eq", root_label), PAGE_FIELDS):
        index.add_page(page)
        doc_ids.update(d["Id"] for d in page.get("doc_links") or [])
    seen = set()
    for doc in _scan(db, "Documents", ("root_label", "eq", root_label), DOC_FIELDS):
        index.add_doc(doc)
        seen.add(doc["Id"])
    missing = sorted(doc_ids - seen)  # documents of the archive's pages held under another root
    for i in range(0, len(missing), 100):
        for doc in db.read("Documents", [str(x) for x in missing[i:i + 100]], fields=DOC_FIELDS):
            if doc:
                index.add_doc(doc)
    return index


def fetch_by_url(db, index, table, urls):
    """Look up records whose urls lie outside the archive's root label."""
    urls = sorted(set(urls))
    for i in range(0, len(urls), 5):
        chunk = urls[i:i + 5]
        fields = PAGE_FIELDS if table == "Pages" else DOC_FIELDS
        for rec in _scan(db, table, ("url", "in", chunk), fields):
            (index.add_page if table == "Pages" else index.add_doc)(rec)


# ---------------------------------------------------------------- sheets

class Findings:
    def __init__(self):
        self.rows = []

    def add(self, kind, ws, cell, expected="", actual="", url=""):
        self.rows.append({"finding": kind, "sheet": ws.title, "cell": cell,
                          "expected": unquote(text(expected)), "actual": text(actual),
                          "url": unquote(text(url))})


def sheet_kind(ws):
    for r in HEADER_ROWS:
        head = text(ws.cell(r, 1).value)
        word = head.split(" ")[0]
        if word in SHEET_KINDS and "#" in head:
            return SHEET_KINDS[word], r
    return None, None


def data_rows(ws, hdr_row):
    """Rows after the header, up to the "# ... listed" totals row, holding anything."""
    for r in range(hdr_row + 1, ws.max_row + 1):
        label = ws.cell(r, 2).value
        if isinstance(label, str) and label.strip().startswith("#"):
            return
        if str(ws.cell(r, 1).value or "").startswith("="):
            return
        cells = [ws.cell(r, c) for c in range(1, 16)]
        if any(text(c.value) or c.hyperlink for c in cells):
            yield r


def opus_columns(ws, hdr_row):
    cols = {}
    seen = Counter()
    for c in range(3, ws.max_column + 1):
        name = OPUS_HEADERS.get(text(ws.cell(hdr_row, c).value))
        if not name:
            continue
        seen[name] += 1
        if name in ("processor", "pages"):
            name = f"{name}{seen[name]}"
        cols.setdefault(name, c)
    return cols


def same(a, b):
    return " ".join(text(a).split()) == " ".join(text(b).split())


def check_header(ws, index, f):
    """A2, B4, D3, I3 and any other header links of a sheet."""
    d3 = ws["D3"]
    d3_links = cell_links(d3)
    page = None
    for link in d3_links:
        page = page or index.page(link)
    if not d3_links:
        f.add("sheet has no D3 link", ws, "D3", actual=text(d3.value))
    elif page is None:
        f.add("sheet page missing", ws, "D3", expected=d3_links[0], url=d3_links[0])
    if len({index.key(link) for link in d3_links}) > 1:
        # usually a hyperlink copied from another sheet under the right url text
        f.add("D3 link and text differ", ws, "D3", expected=" | ".join(d3_links),
              actual=page.get("url") if page else "")
    if page is None:
        return None
    url = page.get("url")

    a2 = text(ws["A2"].value)
    if a2:
        old = page.get("OLD_description")
        if old is None and "OLD_description" not in page:
            stored = page.get("description")  # base without OLD_description: only description
        else:
            stored = old
        if not stored or " ".join(a2.split()) not in " ".join(stored.split()):
            f.add("A2 description not stored", ws, "A2", a2, stored, url)

    b4 = ws["B4"]
    page_docs = {index.key(d.get("url")) for d in docs_of(page, index)}
    for link in cell_links(b4):
        if index.key(link) not in page_docs:
            f.add("B4 link not stored", ws, "B4", link, "", url)
    rest = non_link_text(b4)
    if rest and rest not in (page.get("import_message") or "") and rest not in (page.get("comments") or ""):
        f.add("B4 text not stored", ws, "B4", rest, "", url)

    comments = page.get("comments") or ""
    for addr in ("I2", "I3"):
        value = text(ws[addr].value)
        if value and value not in comments:
            f.add(f"{addr} comment not stored", ws, addr, value, comments, url)

    # header links other than D3 and B4 (e.g. DAHO-style extra archive links)
    for r in range(1, 5):
        for c in range(1, 16):
            cell = ws.cell(r, c)
            if cell.coordinate in ("D3", "B4"):
                continue
            for link in cell_links(cell):
                stored = index.key(link) in page_docs or unquote(link) in (page.get("import_message") or "")
                if not stored:
                    f.add("other header link not stored", ws, cell.coordinate, link, "", url)
    return page


def docs_of(page, index):
    docs = []
    for d in page.get("doc_links") or []:
        rec = index.docs_by_id.get(d["Id"]) if hasattr(index, "docs_by_id") else None
        docs.append(rec or d)
    return docs


def check_list_sheet(ws, hdr_row, index, f, claimed):
    """Overview (fund list) and fund (opus list) sheets: a page per row, column O comments."""
    for r in data_rows(ws, hdr_row):
        a, b = ws.cell(r, 1), ws.cell(r, 2)
        links = cell_links(a) or cell_links(b)
        o = text(ws.cell(r, 15).value)
        if not links:
            f.add("row not imported (no link)", ws, f"A{r}", f"{text(a.value)} {text(b.value)}".strip())
            continue
        page = index.page(links[0])
        if page is None:
            f.add("row not imported", ws, f"A{r}", links[0], "", links[0])
            continue
        claimed[index.key(page["url"])].append(f"{ws.title}!A{r}")
        if o and not same(o, page.get("comments")):
            f.add("column O comment differs", ws, f"O{r}", o, page.get("comments"), page["url"])


def check_opus_sheet(ws, hdr_row, index, f, claimed, claimed_docs):
    cols = opus_columns(ws, hdr_row)
    for r in data_rows(ws, hdr_row):
        a, b = ws.cell(r, 1), ws.cell(r, 2)
        page_links, doc_links = cell_links(a), cell_links(b)
        values = {k: ws.cell(r, c) for k, c in cols.items()}
        has_values = any(text(c.value) for c in values.values())
        page = index.page(page_links[0]) if page_links else None
        doc = None
        for link in doc_links:
            doc = doc or index.doc(link)
        if not doc_links and page_links:
            # a row without a file keeps its D..O values on a self-link record
            doc = index.doc(page_links[0])

        if page is None and doc is None:
            what = page_links[0] if page_links else (doc_links[0] if doc_links else "")
            kind = "row not imported" if what else "row not imported (no link)"
            if not what and not text(a.value):
                kind = "row not imported (no case number, no link)"
            f.add(kind, ws, f"A{r}", what or f"{text(a.value)} {text(b.value)}".strip()[:120], "", what)
            continue
        if page is not None:
            claimed[index.key(page["url"])].append(f"{ws.title}!A{r}")
        if page_links and page is None:
            f.add("case page missing", ws, f"A{r}", page_links[0], "", page_links[0])
        if doc_links and doc is None:
            f.add("document missing", ws, f"B{r}", doc_links[0], "", doc_links[0])

        if page is not None and not same(b.value, page.get("description")):
            f.add("B description differs", ws, f"B{r}", text(b.value), page.get("description"), page["url"])
        if page is not None and not same(ws.cell(r, 3).value, page.get("years")):
            f.add("C years differ", ws, f"C{r}", text(ws.cell(r, 3).value), page.get("years"), page["url"])

        if doc is None:
            # without a document only the page fields (description, years,
            # comments) are stored; the D..O values have nowhere to go
            comment = values.get("comments")
            if comment is not None and text(comment.value) and page is not None \
                    and not same(comment.value, page.get("comments")):
                f.add("G comment differs", ws, comment.coordinate, text(comment.value),
                      page.get("comments"), page["url"])
            lost = [k for k in values if k != "comments" and text(values[k].value)]
            if lost:
                filled = ", ".join(f"{text(ws.cell(hdr_row, cols[k]).value)}={text(values[k].value)}"
                                   for k in lost)
                f.add("D..O values not stored (no document)", ws, f"A{r}", filled, "",
                      page["url"] if page else "")
            continue
        claimed_docs[index.key(doc["url"])].append(f"{ws.title}!B{r}")
        check_doc_values(ws, r, values, doc, f)


def _codes(value):
    """Doc type codes as a sorted list; the obsolete "M" is stored as "V"."""
    if isinstance(value, list):
        codes = [str(v).strip().upper() for v in value if v]
    else:
        codes = [p.strip().upper() for p in re.split(r"[/,;&+ ]+", text(value)) if p.strip()]
    return sorted("V" if c == "M" else c for c in codes)


def check_doc_values(ws, r, values, doc, f):
    url = doc.get("url")

    def cmp(kind, key, actual, expected=None):
        cell = values.get(key)
        if cell is None:
            return
        exp = text(cell.value) if expected is None else expected
        if exp != (actual if isinstance(actual, str) else text(actual)):
            f.add(kind, ws, cell.coordinate, exp, actual, url)

    if "doc_type" in values and _codes(values["doc_type"].value) != _codes(doc.get("OLD_doc_type")):
        f.add("D doc type differs", ws, values["doc_type"].coordinate,
              text(values["doc_type"].value), doc.get("OLD_doc_type"), url)
    cmp("E content code differs", "content", text(doc.get("OLD_content_code")),
        text(values["content"].value).upper() if "content" in values else None)
    cmp("F process code differs", "process", text(doc.get("OLD_process_code")),
        text(values["process"].value).upper() if "process" in values else None)
    if "comments" in values and not same(values["comments"].value, doc.get("comments")):
        f.add("G comment differs", ws, values["comments"].coordinate,
              text(values["comments"].value), doc.get("comments"), url)

    files = [text(values[k].value) for k in ("file1", "file2") if k in values and text(values[k].value)]
    if files and set(files) - set(text(doc.get("filename")).split(",")):
        f.add("H/K file name differs", ws, values.get("file1", values.get("file2")).coordinate,
              ",".join(files), doc.get("filename"), url)
    procs = [text(values[k].value) for k in ("processor1", "processor2") if k in values and text(values[k].value)]
    if procs and set(procs) - set(text(doc.get("processor")).split(",")):
        f.add("I/L processor differs", ws, values.get("processor1", values.get("processor2")).coordinate,
              ",".join(procs), doc.get("processor"), url)
    pages = [values[k].value for k in ("pages1", "pages2") if k in values and text(values[k].value)]
    if pages:
        try:
            total = sum(int(float(p)) for p in pages)
            if total != (doc.get("pages_processed") or 0):
                f.add("J/M pages processed differ", ws, values.get("pages1", values.get("pages2")).coordinate,
                      str(total), doc.get("pages_processed"), url)
        except (TypeError, ValueError):
            f.add("J/M pages processed not a number", ws, values.get("pages1", values.get("pages2")).coordinate,
                  " + ".join(text(p) for p in pages), doc.get("pages_processed"), url)

    n = values.get("transcribed")
    if n is not None and n.value is not None:
        shown = displayed_date(n.value, n.number_format) if not isinstance(n.value, str) else text(n.value)
        stored = text(doc.get("when_transcribed"))
        if shown is None:
            shown = text(n.value)
        if stored != shown:
            kind = ("N stored as a date, not as shown" if isinstance(n.value, (datetime.date, datetime.datetime))
                    else "N when transcribed differs")
            f.add(kind, ws, n.coordinate, shown, stored, url)

    lang = values.get("language")
    if lang is not None and text(lang.value):
        stored = doc.get("language") or []
        expected = [p for p in re.split(r"[/;,& ]+", text(lang.value)) if p]
        expected = ["Russian" if p == "Russ" else p for p in expected]
        if text(lang.value).lower() != "various" and sorted(expected) != sorted(stored):
            f.add("O language differs", ws, lang.coordinate, text(lang.value), stored, url)


def check_workbook(path, index, f, claimed, claimed_docs):
    wb = load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        kind, hdr_row = sheet_kind(ws)
        if kind is None:
            f.add("unrecognized sheet layout", ws, "A6")
            continue
        check_header(ws, index, f)
        if kind == "opus":
            check_opus_sheet(ws, hdr_row, index, f, claimed, claimed_docs)
        else:
            check_list_sheet(ws, hdr_row, index, f, claimed)


def outside_urls(paths, index):
    """Row links of the spreadsheets that the archive's own records don't cover."""
    pages, docs = set(), set()
    for path in paths:
        wb = load_workbook(path, data_only=True)
        for ws in wb.worksheets:
            kind, hdr_row = sheet_kind(ws)
            if kind is None:
                continue
            for link in cell_links(ws["D3"]):
                if index.page(link) is None:
                    pages.add(link)
            for r in data_rows(ws, hdr_row):
                for link in cell_links(ws.cell(r, 1)):
                    if index.page(link) is None:
                        pages.add(link)
                for link in cell_links(ws.cell(r, 2)):
                    if index.page(link) is None and index.doc(link) is None:
                        pages.add(link)
                        docs.add(link)
    return pages, docs


def report(f, claimed, claimed_docs, examples):
    for key, where in claimed.items():
        if len(where) > 1:
            f.rows.append({"finding": "rows share one page record", "sheet": where[0].split("!")[0],
                           "cell": "; ".join(where), "expected": f"{len(where)} records",
                           "actual": "1", "url": key})
    for key, where in claimed_docs.items():
        if len(where) > 1:
            f.rows.append({"finding": "rows share one document record", "sheet": where[0].split("!")[0],
                           "cell": "; ".join(where), "expected": f"{len(where)} records",
                           "actual": "1", "url": key})
    by_kind = defaultdict(list)
    for row in f.rows:
        by_kind[row["finding"]].append(row)
    lines = []
    for kind, rows in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"\n{kind}: {len(rows)}")
        for row in rows[:examples]:
            exp = str(row["expected"])[:100]
            act = str(row["actual"])[:100]
            detail = f"expected {exp!r}" + (f", stored {act!r}" if act else "")
            lines.append(f"    {row['sheet']}!{row['cell']}: {detail}")
    return "\n".join(lines)


def main(argv=None):
    args = parse_args(argv)
    paths = [os.path.abspath(p) for p in args.spreadsheets]
    root = args.root or os.path.basename(paths[0]).split("-")[0]
    sys.path.insert(0, REPO_ROOT)
    db = connect(args.env)
    from birddog.database_updater import normalize_url
    print(f"Target: {args.env}, root label {root}")
    index = load_index(db, root, normalize_url)
    print(f"Loaded {len(index.pages)} pages and {len(set(id(d) for d in index.docs.values()))} documents")
    pages, docs = outside_urls(paths, index)
    print(f"Looking up {len(pages)} page and {len(docs)} document links outside {root}")
    if pages:
        fetch_by_url(db, index, "Pages", pages)
    if docs:
        fetch_by_url(db, index, "Documents", docs)
    index.docs_by_id = {d["Id"]: d for d in index.docs.values()}

    f = Findings()
    claimed, claimed_docs = defaultdict(list), defaultdict(list)
    for path in paths:
        print(f"Checking {os.path.basename(path)}")
        check_workbook(path, index, f, claimed, claimed_docs)
    print(report(f, claimed, claimed_docs, args.examples))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=["finding", "sheet", "cell", "expected", "actual", "url"])
            writer.writeheader()
            writer.writerows(f.rows)
        print(f"\nWrote {len(f.rows)} findings to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
