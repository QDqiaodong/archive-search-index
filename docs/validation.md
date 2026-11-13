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
