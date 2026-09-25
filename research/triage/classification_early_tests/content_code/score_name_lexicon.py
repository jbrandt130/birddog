#!/usr/bin/env python3
"""
Reusable scorer for the Jewish-name-lexicon content_code feature.

Given a record with page_description / page_native_description, returns
whether it hits the lexicon (jewish_name_lexicon.json) and, if so, what
kind of hit it was -- a personal given name/surname marker, or the native-
language "Jew-" ethnonym stem (Еврей-/Єврей-). These are reported
separately because they are different signals: the ethnonym stem in
native text is a native-language analogue of Stage 1's English keyword
regex, while the given-name hit is the genuinely new, previously-untested
document-level lever.

WHERE THE VALIDATION NUMBERS IN name_lexicon_results.md CAME FROM: this
project's NocoDB tables are too large to pull in bulk through the MCP tool
without exceeding response size limits (8,704 labeled validation records
alone), so the experiment was run as server-side NocoDB `where`-clause
aggregate counts (countRecords with OR'd LIKE conditions across all
lexicon terms) rather than by materializing per-record data locally and
looping over it. This module exists so the SAME lexicon logic can be run
locally, record-by-record, whenever a bulk export (e.g. eval_data.json-
style) is available, or against the p1_p2_no/eval_data.json 284-doc set
for quick sanity checks. `where_fragment()` regenerates the exact query
fragment used for the server-side version so the two stay in sync.

Usage:
    from score_name_lexicon import score_record, load_lexicon
    lex = load_lexicon()
    hit, kind, matched = score_record(rec, lex)
    # hit: bool: kind: "name" | "ethnonym" | None; matched: the term that fired
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
LEXICON_PATH = HERE / "jewish_name_lexicon.json"

POS_KEYWORD = re.compile(r"jew|hebrew|yiddish|synagog|rabbi", re.I)
NEG_KEYWORD = re.compile(
    r"church|parish|priest|orthodox|monaster|deanery|consistor|cathedral", re.I)


def load_lexicon(path=LEXICON_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _flat_terms(lex):
    latin, cyr = set(), set()
    for grp in ("male_given_names", "female_given_names"):
        for v in lex[grp].values():
            latin.update(v["latin"])
            cyr.update(v["cyrillic"])
    latin.update(lex["surname_markers"]["latin"])
    cyr.update(lex["surname_markers"]["cyrillic"])
    latin.discard("yos")
    ethnonym = set(lex["jewish_word_stem"]["cyrillic"])
    return sorted(latin), sorted(cyr - ethnonym), sorted(ethnonym)


def is_keyword_silent(page_description):
    """True if page_description matches neither the Stage-1 positive nor
    negative keyword regex -- i.e. this doc is in the residue the name
    lexicon targets, not already resolved by Stage 1."""
    desc = page_description or ""
    return not POS_KEYWORD.search(desc) and not NEG_KEYWORD.search(desc)


def score_record(rec, lex=None):
    """rec needs 'page_description' and optionally 'page_native_description'.
    Returns (hit: bool, kind: 'name'|'ethnonym'|None, matched_term: str|None).
    Checks ethnonym stem first so a doc that hits both is labeled by the
    stronger/more Stage-1-like signal; in practice these barely overlap."""
    lex = lex or load_lexicon()
    latin, cyr_names, ethnonym = _flat_terms(lex)
    desc = (rec.get("page_description") or "").lower()
    native = (rec.get("page_native_description") or "").lower()

    for t in ethnonym:
        if t in native:
            return True, "ethnonym", t
    for t in latin:
        if t in desc:
            return True, "name", t
    for t in cyr_names:
        if t in native:
            return True, "name", t
    return False, None, None


def where_fragment(lex=None):
    """Regenerate the NocoDB `where`-clause OR-fragment for
    (page_description LIKE %latin%) OR (page_native_description LIKE %cyrillic%),
    matching what was used for the server-side validation counts."""
    lex = lex or load_lexicon()
    latin, cyr_names, ethnonym = _flat_terms(lex)
    cyr_all = sorted(set(cyr_names) | set(ethnonym))

    def clause(field, terms):
        return "~or".join(f"({field},like,%{t}%)" for t in terms)
    return clause("page_description", latin) + "~or" + clause("page_native_description", cyr_all)


if __name__ == "__main__":
    lex = load_lexicon()
    latin, cyr_names, ethnonym = _flat_terms(lex)
    print(f"{len(latin)} Latin terms, {len(cyr_names)} Cyrillic name terms, "
          f"{len(ethnonym)} ethnonym-stem terms")

    # Smoke test against the two confirmed examples from the handoff.
    for desc, native, expect in [
        ("Case file", "Про справу Шмуля Цапа", True),
        ("Case file", "Про справу Мошка Фурмана", True),
        ("Parish register", None, False),
    ]:
        hit, kind, term = score_record(
            {"page_description": desc, "page_native_description": native}, lex)
        status = "OK" if hit == expect else "FAIL"
        print(f"[{status}] hit={hit} kind={kind} term={term!r}  <- {native or desc}")
