# Migration fixture provenance

All documents in `old/` and `new/` plus `citations.json` were written
specifically for this project as a synthetic citation-migration example.
No text was downloaded, scraped or copied from another repository; the
files are MIT-licensed along with the code.

The example covers, with real `archive-search build` snapshots:

- M1: a paragraph inserted in the new edition shifts later line numbers.
- M2: the document vanishes in the new edition while the unrelated M5
  carries byte-identical text (cross-document text is never aligned).
- M3: a duplicated paragraph loses one copy, making both old copies
  ambiguous; the unique tail paragraph still migrates.
- M4: an unchanged document migrates identically.
