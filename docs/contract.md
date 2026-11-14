# Corpus and query contract v1

## Capture and identity

Manifest is a UTF-8 JSON object with exactly `schema_version: 1` and `documents`.
The version must be an integer, not a boolean. Duplicate JSON keys are errors.
There are 1–1000 document objects, each with exactly `id`, `title`, `collection`,
`path`. All fields are nonempty strings of at most 255 characters, with no leading
or trailing whitespace or ASCII control characters. Document IDs and resolved
paths must be unique; different documents may contain identical text.

Paths are relative to the manifest directory. Absolute paths, `..`, and symlinks
in the document path are rejected. Files must be regular UTF-8 text; NUL and
blank-only documents are rejected. Maximum manifest size is 1 MiB, document
size 2 MiB, combined document size 1 GiB across at most 1000 documents. Each
input is read once, on demand: its SHA256, parsing, paragraph citations and
retained raw BLOB all derive from that single captured byte string. Document
bodies are never cached ahead of the current document, so build memory grows
with the largest document rather than with total corpus size. This is a
trusted, static local corpus contract, not a sandbox for hostile concurrent
filesystem changes. The manifest must not itself be a symlink.

## Paragraphs and retrieval

A whitespace-only line separates paragraphs. Nonempty physical lines between
separators form one paragraph, numbered from 1 within the document. Citation
start/end lines count physical lines from 1, including preceding blank lines.
Line endings in displayed paragraphs are normalized to LF; raw bytes remain
unchanged in `documents.raw`. Blank lines are not indexed as paragraphs.

Each paragraph indexes its document title and its body using SQLite FTS5
`unicode61 remove_diacritics 2`. Title matches can therefore retrieve paragraphs
whose body does not contain the term. Queries use FTS5 MATCH syntax: quoted
phrases, AND/OR/NOT, prefix and column expressions as supported by SQLite.
This is not SQL; SQL values are always bound parameters. Malformed MATCH syntax
is an error. Empty query and queries longer than 4096 characters are rejected.
Optional collection is an exact case-sensitive equality filter. Limit is an
integer 1–100. No matches is a successful empty list.

Results are paragraph hits, not one hit per document. BM25 weights title/body
as 2/1; lower score sorts first, then document ID and paragraph number.
Ranking is SQLite-specific. Query JSON includes full paragraph text and exact
source hash/path plus citation range; it contains neither current time nor host
absolute paths. Source paths are descriptive evidence, not links to fetch data.
Search does not render or interpret source text as HTML or instructions.

## Stored snapshot and failure

Database user_version=1. `metadata` retains manifest bytes/hash; `documents`
retains identity/title/collection/relative path/hash/raw bytes; `chunks` retains
paragraph bodies and line ranges with document foreign keys. `search` is the
FTS5 table keyed by the matching chunk rowid. Stable ID ordering makes rowids
independent of manifest entry ordering. No deduplication of identical content
across distinct IDs is implied.

Build validates manifest metadata first, then captures, validates and inserts
each document in one streaming pass inside a temporary SQLite file beside the
target. It commits, checks FTS index/foreign keys, closes the database, then
replaces the target. A limit breach or a bad document encountered mid-pass
fails the whole build before replacement: the previous database is preserved
and the temporary file is cleaned. Existing input files and the manifest
cannot be selected as the output, and symlink targets are rejected.
Abrupt machine termination can leave a temporary file; no crash-recovery
daemon or power-loss durability guarantee is provided.

Read-only queries use SQLite URI mode=ro, never create a missing database and
reject unsupported user_version. An untrusted arbitrary database is outside
this local tool's input contract. Build is single-operator: do not overwrite a
database with active readers/writers. A later stdout failure does not undo a
completed file replacement. CLI contract/input/SQLite errors exit 2 with stderr;
valid build and valid searches (including no hits) exit 0.
