# (c) 2026 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

from collections import deque, defaultdict

from birddog.database import Database

from birddog.log import get_logger
_logger = get_logger()

# ----------------------------------------------------------------------

def get_doc_id_by_process_code(db, process_code, limit=None):
    if isinstance(process_code, (list, tuple)):
        where_clause = ("process_code", "in", process_code)
    else:
        where_clause = ("process_code", "eq", process_code)
    result = []
    cursor = None
    while True:
        records, cursor = db.scan("Documents", where=where_clause, limit=1000, fields=["Id", "process_code"], cursor=cursor)
        result.extend(records)
        print(len(result))
        if not records or not cursor:
            break
        if limit and len(result) > limit:
            break
    if isinstance(process_code, (list, tuple)):
        return {rec["Id"]: rec["process_code"] for rec in result}
    else:
        return [rec["Id"] for rec in result]

def _form_tag_lists(d):
    result = defaultdict(list)
    for id_, tag in d.items():
        result[tag].append(id_)
    return result

_level_rank = {
    "volume": 0,
    "case": 1,
    "opus": 2,
    "fond": 3,
    "archive": 4,
}

def _page_entry(rec):
    return {
        "level": rec["level"],
        "label": rec["label"],
        "parent": [p["Id"] for p in rec["parent"]],
    }

def _lowest_rank_page(page_tree, page_ids):
    best = page_ids[0]
    best_rank = _level_rank[page_tree[best]["level"]]
    for pid in page_ids[1:]:
        rank = _level_rank[page_tree[pid]["level"]]
        if rank < best_rank or (rank == best_rank and pid < best):
            best = pid
            best_rank = rank
    return best

def _make_doc_map(db, doc_ids, doc_map=None):
    # construct doc map of record id to owning page record ids
    _logger.info(f"_make_doc_map: loading doc records ({len(doc_ids)})")
    doc_records = db.read("Documents", doc_ids, fields="owning_pages")
    if not doc_map:
        doc_map = {}
    doc_map.update({
        rec["Id"]: {
            "owning_pages": [ p["Id"] for p in rec["owning_pages"] ]
        }
        for rec in doc_records
    })
    return doc_map

def _make_page_tree(db, page_ids):
    _logger.info(f"_make_page_tree: loading owning page records ({len(page_ids)})")
    page_records = db.read("Pages", page_ids, fields=["parent", "level", "label"])
    page_tree = {rec["Id"]: _page_entry(rec) for rec in page_records}

    # traverse the parent hierarchy of the owning pages
    while True:
        page_ids = list({
            pid for value in page_tree.values() for pid in value["parent"]
            if pid not in page_tree
        })
        if not page_ids:
            break
        _logger.info(f"_make_page_tree: loading parent page records ({len(page_ids)})")
        #_logger.info(f"{page_ids}")
        page_records = db.read("Pages", page_ids, fields=["parent", "level", "label"])
        #_logger.info(f"done read")
        page_tree.update({rec["Id"]: _page_entry(rec) for rec in page_records})

    # remove parent ids that are not represented in the map
    #_logger.info(f"trim dangling parents")
    for page_rec in page_tree.values():
        parents = page_rec.get("parent", [])
        page_rec["parent"] = [p for p in parents if p in page_tree]

    return page_tree

def assign_docs_to_pages(db, doc_ids):
    doc_map = _make_doc_map(db, doc_ids)
    
    # construct page map for every owning page
    page_ids = list({pid for v in doc_map.values() for pid in v['owning_pages']})
    page_tree = _make_page_tree(db, page_ids)

    # assign each document to one parent; if more than one parent, then assign to the lowest level;
    # if more than one at lowest level, assign to lowest page record id (tie-breaker)
    #_logger.info(f"assign docs to unique parent")
    frontier = deque()
    for doc_id, doc_rec in doc_map.items():
        owning_pages = doc_rec.get("owning_pages")
        if not owning_pages:
            raise ValueError("orphan doc")
        assigned_page = _lowest_rank_page(page_tree, owning_pages)
        assigned_docs = page_tree[assigned_page].get("assigned_docs", [])
        assigned_docs.append(doc_id)
        page_tree[assigned_page]["assigned_docs"] = assigned_docs
        frontier.append(assigned_page)

    # iteratively propagate assigned docs to parent
    # correctness invariant: every time a page's assigned_docs is updated it is re-appended to the
    # frontier, so partial propagations are always superseded -- even if a page is popped before
    # all its children have contributed, it will be re-queued and re-propagated once they do.
    #_logger.info(f"propagate upward doc assignments")
    while frontier:
        pid = frontier.popleft()
        #_logger.info(f"   pid={pid}, frontier={len(frontier)}")
        page_rec = page_tree[pid]
        parents = page_rec.get("parent")
        if not parents:
            continue
        assigned_docs = page_rec.get("assigned_docs")
        if not assigned_docs:
            continue
        assigned_page = _lowest_rank_page(page_tree, parents)
        if assigned_page == pid:
            continue
        old_docs = set(page_tree[assigned_page].get("assigned_docs", []))
        new_docs = old_docs | set(assigned_docs)
        page_tree[pid]["assigned_parent"] = assigned_page
        if new_docs != old_docs:
            page_tree[assigned_page]["assigned_docs"] = list(new_docs)
            frontier.append(assigned_page)

    return doc_map, page_tree

def split_doc_list_by_code(doc_list, doc_code_map):
    result = defaultdict(list)
    for did in doc_list:
        code = doc_code_map.get(did, "?")
        result[code].append(did)
    return result
