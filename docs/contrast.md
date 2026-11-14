# Collection vocabulary contrast contract v1

`archive-search contrast` compares the body vocabulary of two collections
inside one existing search snapshot. Standard library only; `build`,
`search` and `migrate` are unchanged.

## Inputs and validation

- `--database`: a snapshot produced by `archive-search build`
  (`user_version=1`), opened read-only with SQLite URI `mode=ro`. A missing
  database is never created; an unsupported version or a `search` table
  without a `body` column is an error.
- `--collection-a`, `--collection-b`: exact case-sensitive collection names
  following the manifest label rules (nonempty, no outer whitespace, at most
  255 characters, no control characters). They must be different, and both
  must exist in the snapshot. A collection with zero documents is treated as
  nonexistent, so `N_A` and `N_B` are always at least 1.
- `--limit`: integer 1–100 (booleans rejected).
- `--report`: output path. It must not be a symlink and must not be the
  source database.

Everything is read inside one explicit transaction, so the collection sizes
and every TF/DF value come from the same consistent snapshot. The database
is never modified; the command is repeatable and produces identical JSON
for identical inputs.

## Which terms and counts

Only the FTS5 `body` column is counted. Title tokens are excluded entirely,
both as vocabulary and as occurrences of a body term.

Counting is done by SQLite over `fts5vocab` in instance mode, i.e. over the
tokens that were actually written to the index. Case folding, diacritic
removal (`remove_diacritics 2`) and Chinese token boundaries are therefore
exactly the tokenizer's; the tool never re-tokenizes, splits or stems text
in Python.

For each term and side A/B:

- `tf` — occurrences of the term in that collection's bodies. Repeated
  occurrences inside one paragraph and across paragraphs all add to `tf`;
  identical body text under different document IDs is counted separately.
  The same token may contribute to both sides when two collections contain
  it.
- `df` — number of distinct documents in the collection whose body contains
  the term at least once.
- `N_A`, `N_B` — number of documents in each collection.

## Ranking and report

The term domain is the union of the body vocabularies of the two
collections. Each term has

    ratio = ((DF_A + 1) / (N_A + 2)) / ((DF_B + 1) / (N_B + 2))

Terms are ordered by descending ratio, ties by ascending Unicode code point
order of the term. The ratio is compared exactly: the candidate stores the
integer pair `(DF_A + 1) * (N_B + 2)` / `(N_A + 2) * (DF_B + 1)` and
comparisons cross-multiply, so no floating-point value is ever used for
ordering. The report prints that pair reduced by its greatest common
divisor (always with positive integers). The top `--limit` terms are kept in
a bounded heap; the full term list is never materialized in Python.

Report shape (UTF-8 JSON, sorted keys, one-space indent, trailing newline,
no timestamps or host paths):

```json
{
 "collections": {
  "a": {"documents": 3, "name": "alpha"},
  "b": {"documents": 2, "name": "beta"}
 },
 "limit": 100,
 "schema_version": 1,
 "terms": [
  {"a": {"df": 2, "tf": 2}, "b": {"df": 0, "tf": 0},
   "ratio": {"denominator": 5, "numerator": 12}, "term": "ledger"}
 ]
}
```

Each term object lists both sides' `tf`/`df` and the reduced ratio, so a
reader can recompute the ordering and the collection statistics from the
report alone.

## Failure and publication

A bad database, unknown/duplicate collection, out-of-range limit or bad
output path fails the whole run: nothing is written and any previous report
is preserved. The report is written to a temporary file in the target
directory and atomically replaced; a write failure also preserves the
previous report and cleans up the temporary file. CLI errors exit 2 with a
message on stderr; a successful run exits 0 and prints a one-line JSON
summary with `report`, per-side `documents` and the number of terms written.

API: `archive_index.contrast.compare(database, collection_a, collection_b,
limit)` and `write_report(report, target, inputs)`.

## Example

`fixtures/contrast/` is a hand-checkable five-document example (see its
`PROVENANCE.md`) with a frozen `expected.json`:

```sh
.venv/bin/archive-search build --manifest fixtures/contrast/manifest.json \
  --database run/contrast.sqlite
.venv/bin/archive-search contrast --database run/contrast.sqlite \
  --collection-a alpha --collection-b beta --limit 100 \
  --report run/contrast-report.json
```
