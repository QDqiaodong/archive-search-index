# Collection vocabulary contrast validation — 2026-11-14

Executed in this Linux container (no network), Python 3.11.2 / SQLite 3.40.1,
standard library only. The package was exercised with `PYTHONPATH=src` /
`PYTHONPATH=src:tests`; no third-party code is involved.

## Change

New read-only module `src/archive_index/contrast.py` and CLI subcommand
`archive-search contrast`. It opens an existing snapshot with SQLite URI
`mode=ro` and reads one explicit transaction: collection document counts
come from `documents`, per-term TF/DF come from `fts5vocab('main','search',
'instance')` aggregated by SQL inside the same transaction. No source
manifest or document file is opened, the snapshot is never written, and
Python only sees one aggregated row per term plus a bounded heap of the top
`limit` candidates.

The ranking predicate is the exact integer pair
`(DF_A+1)*(N_B+2)` over `(N_A+2)*(DF_B+1)`, cross-multiplied for ordering
and printed reduced. Equal pairs tie-break on the term string. New
`tests/test_contrast.py` (21 tests) and the independent oracle
`tests/vocabulary_oracle.py` were added; the fixture
`fixtures/contrast/` (5 documents, 9 paragraphs, 2 collections) carries a
frozen `expected.json`.

## Full results

- `PYTHONPATH=src python3 -m unittest discover -s tests`: **82 tests, exit 0**
  (the prior 61 unchanged plus 21 new contrast tests — the suite also passes
  with `-W error`).
- `PYTHONPATH=src python3 tools/acceptance.py`: **ACCEPTANCE PASSED** —
  82 tests green, baseline green, 5/5 mutants killed, working-tree source
  digest unchanged.
- Real CLI on the synthetic fixture:
  `build` reports 5 documents / 9 paragraphs, then
  `contrast --collection-a alpha --collection-b beta --limit 100` exits 0
  with `{"documents": {"a": 3, "b": 2}, "terms": 7}` and a report
  byte-identical to `fixtures/contrast/expected.json`; a second run is
  byte-identical; `--limit 3` writes exactly the first three terms.
  A blank-vocabulary corpus (`...`/`!!!` only) writes `"terms": []` and
  exits 0.

## Hand-checked fixture numbers

Corpus: `alpha` = C1,C2,C3 (N_A=3), `beta` = C4,C5 (N_B=2). C1 and C2 have
byte-identical bodies.

| term | alpha TF/DF | beta TF/DF | reduced ratio |
|------|-------------|------------|---------------|
| ledger | 2/2 | 0/0 | 12/5 |
| archive | 2/1 | 0/0 | 8/5 |
| cafe | 3/3 | 1/1 | 8/5 |
| gate | 2/2 | 1/1 | 6/5 |
| harbor | 6/2 | 1/1 | 6/5 |
| 冬季维护 | 1/1 | 1/1 | 4/5 |
| tide | 0/0 | 4/2 | 4/15 |

`harbor` TF 6 in alpha = 3 body occurrences (two spans in paragraph 1, one in
paragraph 2) in each of the two byte-identical documents. `harbor` also
occurs in the titles of C1/C2/C4, which contributes nothing. `archive` is a
single document's two paragraphs, so DF 1. `cafe` is `Café` (three alpha
docs) plus `CAFÉ` (one beta doc) after unicode61 case and diacritic folding;
`café`/`CAFÉ` never appear as separate terms. `冬季维护` is one token.
`zephyr`, `kestrel`, `log` (title-only) are absent from the report.
`archive`/`cafe` and `gate`/`harbor` are equal-ratio pairs ordered by
Unicode term.

## Assertions

`test_contrast.py` checks: the complete fixture report against the frozen
JSON and against an oracle rebuilt from raw bytes (separate unicode61
normalization, paragraph split, `Fraction` ranking); cross-paragraph and
duplicate-document TF; title exclusion; accent folding; a complete Chinese
token and no substring terms; equal-ratio Unicode tie-breaks; reduced
integer ratios; `--limit` ranked prefixes at 1/3/full; A/B asymmetry;
exact-rational comparator self-tests with different unreduced forms of the
same ratio; a bounded-heap result equal to a full exact sort; unknown, equal
and malformed collections and bad limits; a search table without a body
column; missing/version-broken databases; read-only repeatability; refusal
to overwrite the database or a symlink; injected replacement failure
preserving the old report and cleaning temporaries; error runs preserving an
existing report; seeded random corpora (accented, uppercase, generic and
Chinese word sets) against the oracle including limit prefixes; and a
`tracemalloc` check that the peak at 2×200,000 token instances stays under
32 MiB and within 1 MiB of the 2×20,000 case (measured 91,749 vs 86,501
bytes, i.e. flat with a 10× occurrence increase).

Not asserted: SQLite file bytes, absolute wall-clock performance.
