# Bounded-memory streaming build validation — 2026-11-14

Executed in this Linux container (no network), Python 3.11.2 / SQLite 3.40.1,
standard library only. The package was exercised with `PYTHONPATH=src`
(a user-site `.pth` makes the same `src` importable from the real CLI
subprocesses); no third-party code is involved.

## Change

`ingest.load` keeps its `(manifest_bytes, documents)` interface but returns
a single-pass `DocumentStream` instead of an eagerly cached list. Manifest
metadata (schema, labels, path containment, symlink/regular-file and
duplicate identity checks) is still validated completely before any body is
read. Each document file is then opened and read once on demand; its
SHA256, UTF-8 decode, paragraph citations and retained raw BLOB all derive
from that one captured byte string. `store.build` inserts each document
into the temporary database inside the same streaming pass, so nothing
accumulates across documents. Limits are now manifest 1 MiB, per-document
2 MiB, total corpus 1 GiB across at most 1000 documents. The SQLite schema,
FTS5 configuration, rowid numbering, BM25 ranking query, CLI and on-disk
library format are unchanged.

## Bounded-memory evidence

`tests/memory_probe.py` runs `store.build` in a fresh `python -I`
subprocess with `tracemalloc.start()`/`reset_peak()` bracketing only the
build, and also records whole-process `ru_maxrss`. A fixed corpus of 100
documents of exactly 1 MiB each (100 MiB total, generated under
`run/memory-corpus`, ≥ the required 96 MiB) produced:

- corpus size: **104,857,600 bytes (100.0 MiB)**, 100 documents, 10,100
  paragraphs; snapshot 372,473,856 bytes.
- tracemalloc build peak: **5,375,766 bytes (5.13 MiB)** — below the
  required 32 MiB; current after build: 35,672 bytes.
- process max RSS: 28,508 KiB (interpreter and SQLite included).

To show the peak tracks document size rather than corpus size, the same
1 MiB documents were built at two corpus sizes:

| corpus    | documents | tracemalloc peak |
|-----------|-----------|------------------|
| 25 MiB    | 25        | 5,313,872 bytes  |
| 100 MiB   | 100       | 5,375,619 bytes  |

Quadrupling the corpus raised the peak by ~62 KiB (~1%). The
`MemoryProbeTests` case repeats this comparison inside the suite (8 and 16
one-MiB documents, about a hundred paragraphs per MiB) and asserts both
peaks stay under 32 MiB and do not grow with the corpus; in this container
those runs measured ~5.05 MiB tracemalloc peak and ~27 MiB process RSS at
both corpus sizes.

## Parity and failure coverage

`tests/legacy_build.py` is a frozen copy of the pre-streaming eager
builder. `tests/test_streaming.py` builds the same varied corpus (CRLF,
leading/trailing blanks, Unicode, repeated and byte-identical paragraphs)
with both builders and compares every row of `metadata`, `documents`,
`chunks` and the rowid-keyed FTS `search` table, plus library `search()`
output across words, phrases, boolean queries and CJK tokens; it also
proves chunk rowids are identical when the manifest order is reversed.

The suite additionally covers: the four limit constants; lazy body
capture; exactly one `Path.open` per input during a build; an injected
shrunken total cap failing cumulatively and at the exact inclusive
boundary; the 1001-document rejection before any body is opened; an
invalid-UTF-8 final document and mid-stream NUL/blank documents failing
the whole rebuild while the previous snapshot bytes are preserved and no
`.archive-index-*` temporary remains; an injected publish
`Path.replace` failure preserving the old database; and a real
subprocess CLI build plus search whose result equals the library call.

## Full results

- `PYTHONPATH=src python3 -m unittest discover -s tests`: **61 tests,
  exit 0** (the prior 48 unchanged plus 13 streaming tests).
- `PYTHONPATH=src python3 tools/acceptance.py`: **ACCEPTED PASSED** —
  61 tests green, baseline green, 5/5 mutants killed, working-tree source
  digest unchanged. The mutation anchors in `store.py`/`query.py` were
  retained verbatim.
- Real CLI search against the 100 MiB snapshot returns correct
  document/paragraph citations; the migration CLI run on streaming-built
  old/new libraries still reports `migrated 3, ambiguous 1, unmatched 0,
  vanished 1`, identical to the recorded fixture outcome.
