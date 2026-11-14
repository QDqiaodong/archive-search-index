# Contrast fixture provenance

The five documents and this manifest were written specifically for this
project as a synthetic vocabulary-contrast example. No text was downloaded,
scraped or copied from another repository; the files are MIT-licensed along
with the code.

The example fixes every property of the contrast contract with hand-checkable
numbers:

- `alpha` has three documents; `beta` has two.
- `C1` and `C2` have byte-identical bodies: occurrences repeat across
  paragraphs and across distinct document IDs.
- `Zephyr` and `Kestrel` appear only in titles; `Harbor` also appears in the
  title of `C1`/`C2`/`C4` but the body-only count excludes those title hits.
- `Café` (in `alpha`) and `CAFÉ` (in `beta`) fold to the one unicode61 term
  `cafe` with `remove_diacritics 2`.
- `冬季维护` is one unicode61 token; it is never split into characters.
- `ledger`/`archive` and `gate`/`harbor` are two equal-ratio pairs, so their
  order proves the Unicode term tie-break.

`expected.json` is the complete deterministic report for `limit=100`.
