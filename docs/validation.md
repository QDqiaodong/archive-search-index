# Baseline validation — 2026-11-14

Executed in a new repository-specific virtual environment on macOS:

- Python 3.12.14; SQLite 3.53.1 with FTS5 verified by table creation.
- setuptools 80.9.0 and wheel 0.45.1 installed from cached public wheels;
  package installed with `pip install --no-build-isolation --no-deps .`.
- `pip check`: exit 0, no broken requirements.
- `python -m unittest discover -s tests -v`: 12 tests, exit 0.
- Installed CLI built the sample corpus: 3 documents / 5 paragraphs.
- Phrase search `"winter maintenance"` in engineering returned A1 paragraph 2
  (line 4) and A3 paragraph 1 (lines 1–2).
- Search `restoration` in library returned only A2 paragraph 1.

Tests include a real subprocess build/search/rebuild sequence, exact raw-byte
hash/citation checks, accent and Chinese-token behavior, duplicate identities,
path escape/symlink rejection, malformed UTF-8/NUL/blank inputs, size bounds,
duplicate JSON fields, invalid query syntax and injected replacement failure.
An initial run exposed a missing `uri=True` read-only SQLite connection option;
it was corrected before the successful final installation and full test run.

Local detailed logs, installation receipt, first failure and final test output
remain under ignored `run/`; generated databases and virtual environment are
not tracked. No Linux container execution is claimed. A read-only resource
lookup found a matching prepared dependency image, but this baseline validation
did not start that image. Python 3.11 is the declared minimum, not an additional
runtime tested in this run.

# Citation migration validation — 2026-11-14

Executed in a Linux container (no network) on the declared minimum runtime:

- Python 3.11.2; SQLite 3.40.1 with FTS5 verified by table creation.
- `python3 -m venv --without-pip .venv` (this image lacks ensurepip/pip, so the
  package was exercised as `PYTHONPATH=$PWD/src .venv/bin/python ...` instead of
  an installed wheel; no third-party code was involved).
- `PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests`:
  28 tests, exit 0 — the 12 baseline tests unchanged plus 16 migration tests.
- Real CLI builds of `fixtures/migration/old` and `fixtures/migration/new`
  (4 documents / 9 paragraphs each), then
  `migrate --old run/old.sqlite --new run/new.sqlite --citations
  fixtures/migration/citations.json --report run/report.json` exited 0 with
  summary `{"ambiguous": 1, "citations": 5, "migrated": 3, "unmatched": 0,
  "vanished": 1}`; a second run produced byte-identical report JSON.

Migration tests cover: exhaustive all-optimal-matching checks of the dynamic
programming characterization against an independent brute-force enumerator
(every binary sequence pair up to length 5, 300 seeded random ternary pairs up
to length 7), inserted/deleted paragraphs, duplicate-paragraph ambiguity with
and without possible unmatch, cross-document identical text never aligning,
CRLF originals, deterministic self-contained reports re-verified after deleting
all inputs, bad citations/bad libraries failing the whole run while preserving
a previous report, report never overwriting inputs or symlinks, injected
write-replacement failure preserving the old report and cleaning temporaries,
and an end-to-end CLI run of the synthetic fixture (including exit 2 on a
report path equal to an input database).
