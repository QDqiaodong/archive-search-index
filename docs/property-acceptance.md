# Property and mutation acceptance — 2026-11-14

Goal: make archive search regressions replayable without changing product
behaviour. The product source (`src/archive_index/`) is untouched; everything
below is standard-library test and tooling code.

## One offline command

```sh
PYTHONPATH=$PWD/src python3 tools/acceptance.py
```

Exit 0 means: the whole unittest suite is green, all five injected defects are
killed by the new regression assertions, and no source byte changed while the
mutants ran. The machine-readable report is written to
`run/acceptance-report.json`; a human summary is printed:

```
unittest: exit 0, 48 tests, ['Ran 48 tests in 2.525s', 'OK']
[killed] title_not_indexed: ...
...
samples: 3 seeds, 21 documents, 54 paragraphs, 193 queries (159 with hits, 448 expected hits)
ACCEPTANCE PASSED
```

No network, no third-party packages, no writes outside `run/` and temporary
directories. `python3 tools/mutation_runner.py` runs just the isolation step;
`python3 tools/property_probe.py --corpus DIR` replays one saved case.

## Samples

| seed | documents | paragraphs | collections | queries | with hits | expected hits |
|------|-----------|-----------|-------------|---------|-----------|---------------|
| 20261114 | 7 | 18 | 3 | 60 | 47 | 135 |
| 20261115 | 7 | 18 | 3 | 66 | 57 | 158 |
| 20261116 | 7 | 18 | 3 | 67 | 55 | 155 |

Totals: 3 seeds, 21 document instances, 54 paragraph instances, 193 queries.
Each seeded corpus is built four times (canonical, reversed manifest, a
rebuild, and a real-CLI build) and searched by both the library and the CLI.

Corpus features every seed carries:

- duplicate whole bodies: documents `D04`/`D05` share identical title and bytes;
- a paragraph repeated inside `D06`, and the same `D07` paragraph text as
  `D01` paragraph 2 across documents;
- title-only terms (`zephyr`, `quayside`, `shelftoken`, `meridian`, `gazette`,
  `kestrel`) absent from every body, so a hit proves title indexing;
- a CRLF document with leading/trailing blank lines and a whitespace-only
  (`\t \t`) separator;
- three collections (`engineering`, `library`, `survey`).

## Independent oracle

`tests/property_oracle.py` never imports `archive_index`. It derives, from raw
bytes only:

- physical lines (CRLF normalised, trailing newline not counted), paragraph
  start/end lines and LF-joined body text;
- source SHA256 per document;
- token streams (`[a-z0-9]+` over ASCII case-folded text, matching `unicode61`
  for this vocabulary);
- the reference hit set by a small interpreter for ASCII words, quoted
  phrases (matched within one column, never across the title/body boundary),
  and grouped binary AND / OR / NOT using FTS5 precedence
  (adjacency/AND > NOT > OR; `NOT` is never unary).

Rank values are never compared. Ordering is checked only as the documented
tuple (rank, document ID, paragraph) and as equality between byte-identical
rows, so BM25 numbers are not locked in.

## Failure bundles

Any property failure writes `run/property-failures/seed-<seed>-<stamp>/`
containing the exact document bytes, `manifest.json`, `seed.txt`, `query.txt`,
`request.json` (query, collection, limit, mode, via_cli) and `replay.sh`. The
replay re-derives expectations from the saved bytes and re-runs only the failed
check:

```sh
cd /workspace/project && PYTHONPATH=src \
  /usr/bin/python3 tools/property_probe.py --corpus <bundle> --mode order --query shared
```

Modes: `search` (complete hit set + citation fields), `limit` (ranked prefix),
`order` (ordering/tie rules), `reorder` (reversed manifest), `rebuild`
(two builds), `build` (CLI build counts). `--via-cli` routes searches through
the real `python -m archive_index.cli`. The probe exits 0 on pass, 1 on a
still-failing case, 2 on setup errors. The `test_failure_bundle_is_self_contained`
test proves a bundle reproduces its corpus bytes and reference hits alone.

## Mutation evidence

`tools/mutation_runner.py` copies the project into a temporary directory per
mutant (the real checkout is never edited), applies one textual change,
and runs `tests/test_property.py` in a subprocess. A mutant is **killed** only
if the suite exits non-zero *and* at least one expected new test method failed.
Import/collection errors, missing modules, timeouts and unrelated assertion
failures are classified `invalid`/`unexpected` and do not count. The baseline
copy must be green first. Source digests are compared before and after.

| mutant | injected defect | status | killed by |
|--------|-----------------|--------|-----------|
| `title_not_indexed` | `search` titles replaced by `''` | killed | `test_hits_match_reference_for_every_query`, `test_collection_filter_is_exact`, `test_cli_matches_library_and_oracle`, `test_ordering_and_ties_are_stable` |
| `fts_row_misaligned` | FTS row keyed to `count+1` | killed | reference hits, limit prefix, collection filter, CLI, ordering |
| `line_number_offset` | stored `start_line`/`end_line` shifted by 1 | killed | reference hits (citation fields), limit prefix, collection filter, CLI |
| `collection_filter_dropped` | filter predicate always true | killed | `test_collection_filter_is_exact`, `test_cli_matches_library_and_oracle` |
| `tie_order_reversed` | ties ordered by descending ID/paragraph | killed | `test_ordering_and_ties_are_stable` |

Result: 5/5 killed, baseline green, source unchanged. `tests/test_mutation_runner.py`
additionally asserts the scorer rejects import errors, timeouts, collection
failures and unrelated assertion failures, and that each declared mutant's
`old` text occurs exactly once.

## What is and is not asserted

Asserted: complete hit sets against the oracle, citation line ranges and
verbatim paragraph text, source hash/path, manifest-order independence,
collection exactness (including case sensitivity and absent collections),
`limit` truncating the ranked prefix, and stable ties for identical inputs.

Not asserted: specific BM25 scores, SQLite file bytes, or performance. A
rebuilt database is required only to return the same logical hits.
