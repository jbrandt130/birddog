"""
Documents curation workflow rules for the Documents tab.

Plain functions with no Flask and no database access, so the service layer
(service.py) stays thin and the rules can be tested on their own:

    - which documents a user can see, as a filter for Database.scan()/count()
    - which process-code actions apply to a document
    - which fields a request may change, and the field values to write

Privileges (birddog/user.py DOC_PRIVILEGES) only decide what a user can SEE:
the process codes on offer and whether anomaly-flagged documents show. The
actions offered depend only on the document's process code, never on the
privilege (agreed design, 2026-11-19). The server still checks that each
document a request touches is one the user can see.

Comments always append: request text is added on a new line after the
document's current comments, never replacing them. The client prefills a
stamp (see comment_stamp()) that the user may edit before submitting.
"""

from birddog.abstract_database import And, Or

# ---------------------------------------------------------------------------
# Process codes and privileges
# ---------------------------------------------------------------------------

BLANK = ""   # stands for a blank process_code in filters and actions

PROCESS_CODES = ("new", "duplicate", "check", "review", "escalate", "reject", "defer", "save")
END_CODES = ("reject", "defer", "save")

# forward step from each code; escalate and the end states have none
NEXT_CODE = {BLANK: "check", "new": "check", "check": "review", "review": "escalate"}
# end states that can be sent back to check
REOPEN_FROM = ("reject", "defer")

# process codes each privilege can see; duplicate is never listed (it is
# effectively rejected: the prevailing doc goes through the workflow)
VISIBLE_CODES = {
    "doc_check": ("check",),
    "doc_review": ("check", "review"),
    "doc_escalate": ("check", "review", "escalate"),
    "doc_admin": ("new", "check", "review", "escalate", "reject", "defer", "save", BLANK),
}
# privileges that see anomaly-flagged documents
ANOMALY_PRIVILEGES = ("doc_escalate", "doc_admin")

ANOMALY_ACTIONS = ("anomaly", "clear_anomaly")
ACTIONS = ("check", "review", "escalate") + END_CODES + ANOMALY_ACTIONS

# fields a user edits directly (process_code, anomaly and comments change
# only through actions and appended comment text)
CURATED_FIELDS = ("filename", "language", "doc_type", "doc_structure", "doc_scope")
MULTI_FIELDS = ("language", "doc_type", "doc_structure", "doc_scope")

# grid columns that can be sorted, and the stored field each sorts on
SORT_FIELDS = {
    "label": "seq_label",
    "process_code": "process_code",
    "native_description": "native_description",
    "description": "description",
    "years": "years",
    "page_count": "page_count",
    "byte_size": "byte_size",
    "doc_type": "doc_type",
    "doc_structure": "doc_structure",
    "doc_scope": "doc_scope",
    "language": "language",
    "filename": "filename",
    "anomaly_comment": "anomaly_comment",
    "comments": "comments",
    "last_updated": "UpdatedAt",
}

class DocumentError(ValueError):
    """A Documents request is invalid (maps to HTTP 400)."""

class DocumentAccessError(DocumentError):
    """The user may not see or change this document (maps to HTTP 403)."""

def _doc_privileges(privileges):
    return [p for p in VISIBLE_CODES if p in set(privileges or ())]

def can_curate(privileges):
    """True if the user has any document privilege (empty = deactivated)."""
    return bool(_doc_privileges(privileges))

def visible_codes(privileges):
    """Process codes the user can see, in workflow order (BLANK last)."""
    seen = set()
    for p in _doc_privileges(privileges):
        seen.update(VISIBLE_CODES[p])
    order = list(PROCESS_CODES) + [BLANK]
    return [c for c in order if c in seen]

def sees_anomalies(privileges):
    return any(p in ANOMALY_PRIVILEGES for p in _doc_privileges(privileges))

# ---------------------------------------------------------------------------
# Listing: filters and sort
# ---------------------------------------------------------------------------

def _code_condition(code):
    if code == BLANK:
        return ("process_code", "blank", None)
    return ("process_code", "eq", code)

def label_prefix_filter(prefix):
    """
    Whole path fragments, as wiki._title_prefix_match: "DAHMO/5/1" matches
    DAHMO/5/1 and DAHMO/5/1/4 but not DAHMO/5/10.
    """
    prefix = (prefix or "").strip().strip("/")
    if not prefix:
        return None
    return Or(("label", "eq", prefix), ("label", "startswith", prefix + "/"))

def list_filter(privileges, process_code=None, label_prefix=None, anomalies_only=False):
    """
    Filter for listing documents: the user's visibility rules combined with
    the filters they chose. process_code=None means every visible code.

    Raises DocumentAccessError if the user has no document privilege or asks
    for a process code or anomalies they cannot see.
    """
    if not can_curate(privileges):
        raise DocumentAccessError("no document privileges")
    codes = visible_codes(privileges)
    if process_code is not None:
        if process_code not in codes:
            raise DocumentAccessError(f"process code not available: {process_code!r}")
        codes = [process_code]
    conditions = [Or(*[_code_condition(c) for c in codes])]
    if anomalies_only:
        if not sees_anomalies(privileges):
            raise DocumentAccessError("anomalies are not available")
        conditions.append(("anomaly", "eq", True))
    elif not sees_anomalies(privileges):
        conditions.append(("anomaly", "eq", False))
    prefix = label_prefix_filter(label_prefix)
    if prefix:
        conditions.append(prefix)
    return And(*conditions)

def sort_spec(column="label", ascending=True):
    """Sort keys for a grid column, with Id last so paging is stable."""
    try:
        field = SORT_FIELDS[column]
    except KeyError:
        raise DocumentError(f"cannot sort on {column!r}")
    return [(field, bool(ascending)), ("Id", True)]

# ---------------------------------------------------------------------------
# Changing a document
# ---------------------------------------------------------------------------

def _code_of(doc):
    return doc.get("process_code") or BLANK

def can_see(doc, privileges):
    """The same rule as list_filter(), applied to one record."""
    if _code_of(doc) not in visible_codes(privileges):
        return False
    return sees_anomalies(privileges) or not doc.get("anomaly")

def allowed_actions(doc):
    """Actions offered for a document; they depend only on its process code."""
    code = _code_of(doc)
    actions = []
    if NEXT_CODE.get(code):
        actions.append(NEXT_CODE[code])
    if code in REOPEN_FROM:
        actions.append("check")
    actions.extend(a for a in END_CODES if a != code)
    actions.append("clear_anomaly" if doc.get("anomaly") else "anomaly")
    return actions

def append_comment(current, text):
    """Comments only ever grow: text goes on a new line after the current ones."""
    text = (text or "").strip()
    current = current or ""
    if not text:
        return current
    return f"{current}\n{text}" if current else text

def comment_stamp(name, date, from_code=None, to_code=None):
    """
    Default text the client prefills for a comment, e.g.
    "[2026-11-21 Jon Brandt] check -> review: ". date is a date/datetime or
    an ISO string. The user can edit it before submitting.
    """
    day = date.isoformat()[:10] if hasattr(date, "isoformat") else str(date)[:10]
    who = (name or "").strip()
    head = f"[{day} {who}]" if who else f"[{day}]"
    if to_code is not None:
        head += f" {from_code or '(blank)'} -> {to_code}"
    return head + ": "

def _check_curated(curated):
    if curated is None:
        return {}
    if not isinstance(curated, dict):
        raise DocumentError("curated changes must be an object")
    fields = {}
    for field, value in curated.items():
        if field not in CURATED_FIELDS:
            raise DocumentError(f"field cannot be edited: {field!r}")
        if field in MULTI_FIELDS:
            if value is None:
                value = []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise DocumentError(f"{field} must be a list of strings")
        elif value is not None and not isinstance(value, str):
            raise DocumentError(f"{field} must be text")
        fields[field] = value
    return fields

def plan_update(doc, privileges, action=None, curated=None, comment=None,
                anomaly_comment=None, extract=True):
    """
    Work out the fields to write for one document. Values for the select
    fields are checked against the Schema by the database on write.

    Args:
        doc:             the current record (needs process_code, anomaly, comments)
        privileges:      the user's privileges
        action:          None or one of ACTIONS
        curated:         {field: value} for CURATED_FIELDS
        comment:         text appended to comments (any request may carry one)
        anomaly_comment: required for the "anomaly" action; replaces the
                         anomaly comment
        extract:         for "save": the "Extract for indexing" choice

    Returns {field: value} (empty if nothing changes). The caller adds the key.

    Raises DocumentAccessError if the user cannot see the document, and
    DocumentError for an action that does not apply or bad field values.
    """
    if not can_see(doc, privileges):
        raise DocumentAccessError("document not available")
    fields = _check_curated(curated)

    if action is not None:
        if action not in ACTIONS:
            raise DocumentError(f"unknown action: {action!r}")
        if action not in allowed_actions(doc):
            raise DocumentError(f"action {action!r} does not apply to process code "
                                f"{_code_of(doc) or '(blank)'}")
        if action == "anomaly":
            text = (anomaly_comment or "").strip()
            if not text:
                raise DocumentError("flagging an anomaly needs an anomaly comment")
            fields.update(anomaly=True, anomaly_comment=text)
        elif action == "clear_anomaly":
            fields.update(anomaly=False, anomaly_comment="")
        else:
            fields["process_code"] = action
            if action == "save":
                fields.update(save_pending=True,
                              save_status="externally_indexed" if extract else "not_indexed")

    if comment is not None and not isinstance(comment, str):
        raise DocumentError("comment must be text")
    if comment and comment.strip():
        fields["comments"] = append_comment(doc.get("comments"), comment)
    return fields
