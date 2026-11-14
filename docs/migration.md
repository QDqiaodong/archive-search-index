# Citation migration contract v1

After an archive revision, old paragraph line numbers cannot be reused
directly. `archive-search migrate` checks strict JSON citations against an
old snapshot and aligns each cited document with the same-ID document in a
new snapshot, then publishes a deterministic, self-contained JSON report.
Standard library only; the `build`/`search` entry points are unchanged.

## Inputs

Both databases are snapshots produced by `archive-search build`
(user_version=1). Each is opened with SQLite URI `mode=ro` and read inside
one explicit transaction, so all facts come from a single consistent
snapshot. Before any alignment, every document in both libraries is
verified against itself: the stored SHA256 must equal the SHA256 of the
retained original bytes, the original must be valid UTF-8, and the stored
paragraph/line citations must equal those rebuilt from the original bytes
(blank-line separated paragraphs, LF-normalized text, physical 1-based
lines). Foreign keys are checked as well. Any violation is a bad library
and fails the whole run.

The citations file is UTF-8 JSON of at most 16 MiB: an object with exactly
`schema_version: 1` (integer, not boolean) and `citations`. Duplicate JSON
keys are errors. There are 1–100000 citation objects, each with exactly
`document_id`, `sha256`, `paragraph`, `start_line`, `end_line`.
`document_id` follows the manifest label rules; `sha256` is 64 lowercase
hexadecimal characters; the three numbers are positive integers (booleans
are rejected) with `start_line <= end_line`.

Every citation is checked against old-library facts before alignment: the
document must exist in the old library, its SHA256 must match, the
paragraph must exist, and its start/end lines must equal the rebuilt ones.
Any mismatch is a bad citation and fails the whole run.

## Alignment

Only documents with the same ID are aligned; identical text under a
different document ID is never used. Paragraphs match by exact equality of
their LF full text (CRLF originals are normalized the same way as at
ingest). The alignment is an order-preserving one-to-one matching of
maximum size — equivalently a longest common subsequence of the two
paragraph sequences. All maximum matchings are characterized analytically
with forward/backward dynamic programming; alignments are never
enumerated, and no nearest-line-number guessing is used.

For each cited old paragraph, the report records every new paragraph that
is paired with it in at least one maximum matching (`candidates`) and
whether some maximum matching leaves it unmatched (`possibly_unmatched`).
A pair (i, j) is viable exactly when the paragraphs are equal and
`lcs(old[:i], new[:j]) + 1 + lcs(old[i+1:], new[j+1:])` equals the maximum;
an old paragraph can stay unmatched exactly when the maximum is still
reachable with it removed. Statuses:

- `migrated`: exactly one candidate and matched in every maximum matching.
  `new_paragraph`/`new_start_line`/`new_end_line` are added.
- `ambiguous`: one or more candidates, but some maximum matching chooses a
  different target or leaves it unmatched. All candidates are listed.
- `unmatched`: no maximum matching ever pairs it (the paragraph is gone).
- `vanished`: the document ID is absent from the new library.
  `possibly_unmatched` is null.

Documents with more than 16,000,000 (old+1)×(new+1) alignment cells fail
the run with a clear error; this bounds the quadratic dynamic programming
tables and is far above any corpus the 2 MiB document limit produces in
practice.

## Report

The report is deterministic UTF-8 JSON (sorted keys, LF newlines, no
timestamps or host paths) with `schema_version: 1` and:

- `libraries.old/new.documents`: every document ID with its SHA256, so a
  `vanished` conclusion stays checkable.
- `alignments`: one entry per cited document ID with both sides' SHA256
  and full paragraph sequences (`paragraph`, `start_line`, `end_line`,
  `text`), plus `max_matches` — the maximum matching size. `new` is null
  for vanished documents.
- `citations`: the verified old citations in input order, each with its
  `status`, all `candidates` with line numbers, and `possibly_unmatched`.

The report is self-contained: after the inputs are removed, every
conclusion can be re-derived from the retained paragraph sequences and
digests alone.

## Failure and publication

A bad citation or bad library fails the whole run: nothing is written and
any previous report is preserved. The report is written to a temporary
file in the target directory and atomically replaced into place, so a
write failure also preserves the previous report and cleans up the
temporary file. The report target must not be a symlink and must not be
any input (old database, new database or citations file). CLI contract,
input and SQLite errors exit 2 with a message on stderr; a successful run
(including runs whose citations are ambiguous, unmatched or vanished)
exits 0 and prints a one-line JSON summary with per-status counts.

API: `archive_index.migrate.load_citations(path)`,
`load_library(database)`, `analyze(old_paragraphs, new_paragraphs)`,
`migrate(old_db, new_db, citations_path)` and
`write_report(report, target, inputs)`.
