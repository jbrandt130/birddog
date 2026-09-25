# Name-lexicon experiment: results (2026-07-23)

## What was tested

Feature: does `page_description` (Latin/English) or `page_native_description`
(Cyrillic) contain a Jewish given name / surname marker from
`jewish_name_lexicon.json`, or the Cyrillic "Jew-" ethnonym stem
(Еврей-/Єврей-)? Tested only on the **keyword-silent residue** -- docs where
the Stage-1 English regex (`jew|hebrew|yiddish|synagog|rabbi` positive,
`church|parish|priest|orthodox|monaster|deanery|consistor|cathedral`
negative) fires on neither side. That's the target population per
CONTENT_CODE_HANDOFF.md: 67% of all validation docs, base rate P(J)=53-68%
depending on pool, where the LLM previously scored 27.5% (worse than
guessing J).

**Method note:** the validation pool has 8,704 labeled docs -- too large to
pull through the NocoDB MCP tool without hitting response-size limits, so
this was run as server-side `countRecords` aggregate queries (OR'd `LIKE`
across all lexicon terms) rather than a per-record local script. Every
number below is a real query against the live table, not a sample.
`score_name_lexicon.py` implements the identical logic for future local/
offline use once a bulk export exists.

## Lexicon construction

- Base: the content checker's hand list (Latin), from the handoff.
- Cyrillic forms: hand-built via standard Yiddish/Slavic archival
  transliteration, anchored to the handoff's two confirmed examples
  (Шмуль Цап, Мошко Фурман).
- Mining pass (train split, the mining-legal split): tested the v1 lexicon
  against keyword-silent train docs (432 J / 121 non-J with
  `page_native_description` populated) -- **10/432 J hit, 4/121 non-J hit**
  (71% precision, 2.3% recall). An 80-doc raw eyeball pass on train
  surfaced two fixable gaps: (1) **Zelman/Зельман**, a common given name
  missing from the seed list entirely, and (2) Slavic case declension
  (Мордко -> Мордка/Мордку genitive/dative isn't caught by a literal
  vowel-final match). v2 adds Zelman and declension-safe stems for
  vowel-final names, excluding stems that collide with unrelated common
  words (шифра/"cipher", злата/"gold", роза/"rose", хваля/"praise",
  цина/"pewter", etc. -- checked by hand). Net effect on the train
  sanity check was neutral (docs with these forms were already caught via
  a co-occurring term) -- the additions are real but their contribution
  wasn't visible at this sample size; kept because they don't cost
  precision and may help at validation scale.

## Validation results (held out, touched once, this pass)

Two strata, because `page_native_description` coverage is the binding
constraint (only ~2% of the silent bucket has one):

| Stratum | n | true J | hits | hit & J | precision P(J\|hit) | recall of silent-J |
|---|---|---|---|---|---|---|
| native_description present | 115 | 103 | 7 | 7 | **100%** (95% CI 65-100%) | 6.8% (95% CI 3-13%) |
| native_description absent (English only) | 5,253 | 3,527 | 164 | 137 | **83.5%** (95% CI 77-88%) | 3.9% (95% CI 3-5%) |
| **combined** | 5,368 | 3,630 | 171 | 144 | **84.2%** (95% CI 78-89%) | 4.0% (95% CI 3-5%) |

Silent-bucket base rate: P(J)=67.6% (95% CI 66-69%).

Zero false positives in the native-description stratum (0/12 non-J hit),
and all 7 hits there were genuine name matches -- **none** came from the
Еврей-/Єврей- ethnonym stem, so this stratum's 100% figure is a clean read
on the name signal specifically, not a native-language keyword echo.

## What this means

- **The lexicon works as designed: precision-first, low-coverage.**
  84% precision vs. a 68% base rate is a real, statistically distinguishable
  lift (the two 95% CIs, 78-89% vs 66-69%, don't overlap), but it only
  resolves ~3.2% of the residue (171/5,368). It will not replace the LLM;
  it's a high-confidence overlay that can safely upgrade ~170 previously-
  ambiguous docs per ~5,400-doc batch to a firm J, at a false-positive rate
  the cost-benefit score can tolerate (this is the *inclusion* side, where
  a wrong J costs one wasted extraction, not a missed record).
- **Coverage is bottlenecked by native-language text, exactly as the
  earlier quick test predicted.** The 2% of docs with a populated
  `page_native_description` get 100% precision / 6.8% recall; the other
  98% (English descriptions only) still get a usable 83.5% precision but
  only 3.9% recall. If more native-language description text becomes
  available (or page-body OCR), this lever gets much stronger --
  the ceiling here is metadata coverage, not the lexicon itself.
- **Stack with Stage 1:** combined cascade coverage is now Stage-1 keyword
  (33%) + Stage-2 name-lexicon (2.1% of the whole validation set, 3.2% of
  the residue) at high precision on both stages. The remaining ~64% of
  validation docs still have no resolved signal and are where the LLM (or
  a future stage) has to do the work.

## Next steps (not done here)

- Score AUC lift on the full validation set with the name-lexicon score
  slotted into the cascade (J=1.0 for keyword-positive or name-hit,
  N=0.0 for keyword-negative, LLM/base-rate score otherwise) and compare
  to the 0.733 baseline.
- Data-driven surname mining: the raw sample surfaced several town-derived
  Jewish surnames (Гуревич, Бухін, Ровинський, Любарський, Браїловський,
  Дашевський, Толчинський) not in the seed list -- a systematic frequency-
  based mining pass (J-correlated tokens vs. N/U in the native-description-
  bearing train docs) could expand this meaningfully, but needs a real
  frequency analysis over bulk text, which needs either a bulk NocoDB
  export outside the MCP tool's response-size limit or a paginated pull
  into local files.
