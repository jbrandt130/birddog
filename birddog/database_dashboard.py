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
    parent_list = rec["parent"]
    if not parent_list:
        parent_list = []
    elif not isinstance(parent_list, list):
        parent_list = [ parent_list ] 
    return {
        "level": rec["level"],
        "label": rec["label"],
        "url": rec["url"],
        "parent": [p["Id"] for p in parent_list],
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
    fields = ["owning_pages", "processed", "pages_processed", "transcribed", "process_code"]
    doc_records = db.read("Documents", doc_ids, fields=fields)
    if not doc_map:
        doc_map = {}
    for rec in doc_records:
        owning_pages = rec["owning_pages"]
        if not isinstance(owning_pages, list):
            owning_pages = [ owning_pages ]
        owning_page_ids = [p["Id"] for p in owning_pages ]
        pid = rec["Id"]
        doc_map[pid] = { 
            "owning_pages" : owning_page_ids,
            "process_code": rec.get("process_code"),
            "processed": rec.get("processed"),
            "pages_processed": rec.get("pages_processed"),
            "transcribed": rec.get("transcribed"),
        }
    return doc_map

def _make_page_tree(db, page_ids):
    _logger.info(f"_make_page_tree: loading owning page records ({len(page_ids)})")
    field_list = ["parent", "level", "label", "url"]
    page_records = db.read("Pages", page_ids, fields=field_list)
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
        page_records = db.read("Pages", page_ids, fields=field_list)
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
        doc_rec["assigned_page"] = assigned_page
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

def _scan_opus_summary(db, codes):
    fields = codes + ["page", "label", "url"]
    cursor = None
    result = []
    while True:
        records, cursor = db.scan("OpusSummary", cursor=cursor, limit=500, fields=fields)
        result.extend(records)
        if not records or not cursor:
            break

    for rec in result:
        for code in codes:
            doc_ids = rec[code]
            if not isinstance(doc_ids, list):
                rec[code] = db.get_links("OpusSummary", code, rec["Id"])
            else:
                rec[code] = [ d["Id"] for d in doc_ids ]
        page_id = rec["page"]
        if isinstance(page_id, dict):
            rec["page"] = page_id["Id"]
    return result

def update_opus_summary(db, page_map, doc_code_map):
    codes = list({ code for code in doc_code_map.values() })

    opus_summary_map = {}
    records_to_delete = []
    for rec in _scan_opus_summary(db, codes):
        linked_page_id = rec.get("page")
        # each summary record must uniquely link to a valid page
        if linked_page_id and linked_page_id not in opus_summary_map:
            opus_summary_map[linked_page_id] = rec
        else:
            records_to_delete.append(rec["Id"])

    records_to_create = set()
    records_to_update = set()

    for page_id, page in page_map.items():
        # include all pages with 'opus' level that does not have a parent 
        # that is also 'opus' level
        if page['level'] == 'opus':
            parent_id = page.get("assigned_parent")
            if not parent_id or page_map[parent_id].get("level") != "opus":
                summary_record = opus_summary_map.get(page_id)            
                if summary_record:
                    # known opus page - check if update needed
                    assigned_docs = split_doc_list_by_code(
                        page.get('assigned_docs', []), doc_code_map)
                    for doc_link_field in codes:
                        doc_list = assigned_docs.get(doc_link_field, [])
                        summary_doc_list = summary_record.get(doc_link_field, [])
                        if set(doc_list) != set(summary_doc_list):
                            _logger.info(
                                "update_opus_summary: update needed:"
                                f" {page_id}, {doc_link_field}, {doc_list}")
                            records_to_update.add(page_id)
                else:
                    # new page to be added
                    records_to_create.add(page_id)

    if records_to_create:
        records = [{ 
            "url": page_map[page_id]["url"], 
            "label": page_map[page_id]["label"]
        } for page_id in records_to_create]
        _logger.info(f"update_opus_summary: create: {len(records)} records")
        record_ids = db.write("OpusSummary", records)
        for rec_id, page_id in zip(record_ids, records_to_create):
            assigned_docs = split_doc_list_by_code(
                page_map[page_id].get('assigned_docs', []), doc_code_map)
            db.create_links("OpusSummary", "page", rec_id, page_id)
            for code, doc_ids in assigned_docs.items():
                if doc_ids:
                    doc_link_field = code
                    _logger.info("update_opus_summary: "
                        f"{page_id}: {page_map[page_id]['label']},"
                        f" create doc links: {code}, {len(doc_ids)}")
                    db.create_links("OpusSummary", doc_link_field, rec_id, doc_ids)

    if records_to_update:
        for page_id in records_to_update:
            page = page_map[page_id]
            assigned_docs = split_doc_list_by_code(
                page.get('assigned_docs', []), doc_code_map)
            summary_record = opus_summary_map[page_id]
            rec_id = summary_record["Id"]
            for doc_link_field in codes:
                new_docs = set(assigned_docs.get(doc_link_field, []))
                cur_docs = set(summary_record.get(doc_link_field, []))
                _logger.info(f"{page_id}({doc_link_field}): {cur_docs} -> {new_docs}")
                added_docs = new_docs - cur_docs
                if added_docs:
                    _logger.info("update_opus_summary: "
                        f"{page_id} ({doc_link_field}): adding links: {added_docs}")
                    db.create_links("OpusSummary", doc_link_field, rec_id, list(added_docs))
                removed_docs = cur_docs - new_docs
                if removed_docs:
                    _logger.info("update_opus_summary: "
                        f"{page_id} ({doc_link_field}): removing links: {removed_docs}")
                    db.delete_links("OpusSummary", doc_link_field, rec_id, list(removed_docs))
          
    if records_to_delete:
        _logger.info(f"update_opus_summary: records to delete: {records_to_delete}")
        db.delete("OpusSummary", records_to_delete)

    return opus_summary_map, records_to_create, records_to_update, records_to_delete 
