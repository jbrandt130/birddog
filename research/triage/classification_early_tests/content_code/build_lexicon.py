#!/usr/bin/env python3
"""
Build the Latin+Cyrillic Jewish name lexicon for the content_code
name-lexicon experiment, and emit both the JSON lexicon and the NocoDB
`where`-clause OR-fragments used to test it server-side (countRecords),
so we never have to pull bulk record text into context.

Genesis: the base list is the content checker's hand lexicon (see
CONTENT_CODE_HANDOFF.md). A first pass tested it against 786 J-labeled +
317 non-J-labeled TRAIN docs that have page_native_description populated
(train is the mining split -- fair game, unlike validation/test):
  - hit rate J:      237/786  (30.2%)
  - hit rate non-J:   14/317  ( 4.4%)
  - precision (P(J|hit)): 94.4%
  - almost ALL of that (226/237) came from the CYRILLIC/native_description
    channel, not the Latin/page_description channel (only 11/237)  --
    confirms the handoff's finding that English descriptions rarely carry
    names; native text is the whole game.

A raw eyeball pass over 80 J-labeled native-description records surfaced
two concrete, cheap wins the hand lexicon was missing:
  1. "Zelman"/Зельман -- a common given name in the actual corpus (appears
     3x in one record alone: Зельмана, Зельману) that isn't in the content
     checker's seed list at all.
  2. Case declension: Slavic archival text inflects names (Мордко ->
     Мордка/Мордку/Мордком genitive/dative/instrumental), so a literal
     vowel-final Cyrillic form misses its own declined forms. E.g. "мошко"
     does not match "Мошка Фурмана" (genitive). Fix: for vowel-final terms
     >=5 chars, add the consonant stem (drop final vowel) as an additional
     variant, UNLESS the stem collides with an unrelated common word
     (checked by hand -- see STEM_EXCLUDE below: шифра/злата/роза/хваля/
     цина/etc. have stems that mean "cipher"/"gold"/"rose"/"praise"/"tin"
     in Russian/Ukrainian and would hurt precision more than they'd help
     recall).

Usage:
    python3 build_lexicon.py             # writes jewish_name_lexicon.json
    python3 build_lexicon.py --where-frag   # also prints the NocoDB OR-fragment
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).parent

# vowel-final terms whose stem would collide with an unrelated common word
# or place name -- checked by hand against the raw sample, kept as
# full-word-only matches.
STEM_EXCLUDE = {
    "шифра", "злата", "злате", "роза", "хваля", "цина", "хуле", "холе",
    "мири", "маша", "муся", "хая",
}

LEXICON = {
    "_purpose": (
        "Latin + Cyrillic Jewish given-name/surname-marker lexicon for the "
        "content_code name-lexicon experiment. v2: adds Zelman (missing "
        "from the content checker's seed list, found by eyeballing a raw "
        "train sample) and declension-safe stems for vowel-final Cyrillic "
        "given names, minus a hand-checked collision exclusion list."
    ),
    "male_given_names": {
        "avrum":     {"latin": ["avrum", "avraham"], "cyrillic": ["аврум", "авраам", "абрам"]},
        "chaim":     {"latin": ["chaim"], "cyrillic": ["хаим", "хайм"]},
        "duvid":     {"latin": ["duvid", "david"], "cyrillic": ["дувид", "давид"]},
        "hirsh":     {"latin": ["hirsh", "gerko"], "cyrillic": ["гирш", "герш", "герко"]},
        "ide_leib":  {"latin": ["ide-leib", "ideleib"], "cyrillic": ["иде-лейб", "іде-лейб"]},
        "iosif":     {"latin": ["iosif", "yos"], "cyrillic": ["иосиф", "іосиф", "йосиф", "йось"]},
        "leyb":      {"latin": ["leyb"], "cyrillic": ["лейб", "лейба"]},
        "mendel":    {"latin": ["mendel"], "cyrillic": ["мендель", "мендел"]},
        "mordko":    {"latin": ["mordko", "mordechai"], "cyrillic": ["мордко", "мордк", "мордхе", "мордх", "мордехай"]},
        "moshko":    {"latin": ["moshko", "moshe"], "cyrillic": ["мошко", "мошк", "мойше", "мойш", "моше"]},
        "nukhim":    {"latin": ["nukhim"], "cyrillic": ["нухим"]},
        "nuta_bir":  {"latin": ["nuta-bir"], "cyrillic": ["нута-бир"]},
        "pesach":    {"latin": ["pesach"], "cyrillic": ["песах"]},
        "pinkhus":   {"latin": ["pinkhus"], "cyrillic": ["пинхус", "пинхас"]},
        "shloima":   {"latin": ["shloima", "shlomo"], "cyrillic": ["шлойма", "шлойм", "шломо", "шлом"]},
        "shmul":     {"latin": ["shmul"], "cyrillic": ["шмуль", "шмул"]},
        "srul":      {"latin": ["srul", "yisroel"], "cyrillic": ["срул"]},
        "wolf":      {"latin": ["wolf"], "cyrillic": ["вольф"]},
        "yankel":    {"latin": ["yankel"], "cyrillic": ["янкель", "янкел"]},
        "zeylik":    {"latin": ["zeylik"], "cyrillic": ["зейлик"]},
        "zelman":    {"latin": ["zelman"], "cyrillic": ["зельман"]},
    },
    "female_given_names": {
        "rivka":     {"latin": ["rivka", "rivke", "riva"], "cyrillic": ["рівка", "рівк", "ривка", "ривк", "рива"]},
        "malka":     {"latin": ["malka", "malke"], "cyrillic": ["малка", "малк", "малке"]},
        "miriam":    {"latin": ["miriam", "mirl", "miri", "masha", "musia"], "cyrillic": ["мириам", "мірл", "мири", "маша", "муся"]},
        "chaya":     {"latin": ["chaya", "chaya-sura"], "cyrillic": ["хая", "хая-сура"]},
        "sura":      {"latin": ["sura", "sura-hana", "sura-rivka"], "cyrillic": ["сура", "сура-хана", "сура-рівка"]},
        "ruchel":    {"latin": ["ruchel", "rachael", "rochele"], "cyrillic": ["рухель", "рухел", "рухля", "рухл", "рохеле", "рохел"]},
        "sheine":    {"latin": ["sheine"], "cyrillic": ["шейна", "шейн", "шейне"]},
        "shifra":    {"latin": ["shifra"], "cyrillic": ["шифра"]},
        "zlata":     {"latin": ["zlata", "zlate"], "cyrillic": ["злата", "злате"]},
        "gitl":      {"latin": ["gitl", "gitlya"], "cyrillic": ["гітл", "гітля"]},
        "mindel":    {"latin": ["mindel"], "cyrillic": ["міндель", "міндел"]},
        "roisa":     {"latin": ["roisa", "rosa"], "cyrillic": ["роїса", "роїс", "роза"]},
        "khinka":    {"latin": ["khinka", "chinke"], "cyrillic": ["хінка", "хінк", "хінке"]},
        "khivra":    {"latin": ["khivra", "chivre"], "cyrillic": ["хівра", "хівр", "хівре"]},
        "chule":     {"latin": ["chule", "chole"], "cyrillic": ["хуле", "холе"]},
        "khvalya":   {"latin": ["khvalya"], "cyrillic": ["хваля"]},
        "tsimka":    {"latin": ["tsimka", "tsyna"], "cyrillic": ["цимка", "цимк", "цина"]},
        "ranya_liebe":{"latin": ["ranya-liebe"], "cyrillic": ["раня-лібе"]},
        "rezlya":    {"latin": ["rezlya"], "cyrillic": ["резля", "резл"]},
    },
    "surname_markers": {
        "latin": ["katz", "rabinovich", "shapiro", "furman", "tsap"],
        "cyrillic": ["кац", "рабінович", "рабинович", "шапіро", "шапір", "шапиро", "шапир", "фурман", "цап"],
        "suffix_markers_note": (
            "-ovich/-ович/-івич, -in/-ін/-ин, -es are too short/common to "
            "use as standalone substring matches (would fire on many "
            "non-Jewish Slavic surnames too) -- excluded from the regex. "
            "Additional surnames seen in the mining sample (Гуревич, Бухін, "
            "Ровинський, Любарський, Браїловський, Дашевський, Толчинський) "
            "are town-derived Slavic-pattern surnames, not from the seed "
            "list -- flagged for a future data-driven mining pass, not "
            "added here without broader corpus validation."
        ),
    },
    "jewish_word_stem": {
        "cyrillic": ["єврей", "еврей"],
        "note": "Ukrainian єврей- and Russian еврей- (both mean Jew-/Jewish-).",
    },
}


def flat_terms(lex):
    latin, cyr = set(), set()
    for grp in ("male_given_names", "female_given_names"):
        for v in lex[grp].values():
            latin.update(v["latin"])
            cyr.update(v["cyrillic"])
    latin.update(lex["surname_markers"]["latin"])
    cyr.update(lex["surname_markers"]["cyrillic"])
    cyr.update(lex["jewish_word_stem"]["cyrillic"])
    latin.discard("yos")  # too short/ambiguous standalone
    return sorted(latin), sorted(cyr)


def where_fragment(latin, cyr):
    def clause(field, terms):
        return "~or".join(f"({field},like,%{t}%)" for t in terms)
    return clause("page_description", latin) + "~or" + clause("page_native_description", cyr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--where-frag", action="store_true")
    args = ap.parse_args()

    out_path = HERE / "jewish_name_lexicon.json"
    out_path.write_text(json.dumps(LEXICON, indent=2, ensure_ascii=False), encoding="utf-8")
    latin, cyr = flat_terms(LEXICON)
    print(f"Wrote {out_path.name}: {len(latin)} Latin terms, {len(cyr)} Cyrillic terms")

    if args.where_frag:
        frag = where_fragment(latin, cyr)
        (HERE / "_where_fragment.txt").write_text(frag, encoding="utf-8")
        print(f"Wrote _where_fragment.txt: {len(frag)} chars")


if __name__ == "__main__":
    main()
