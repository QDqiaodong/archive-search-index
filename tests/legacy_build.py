"""Frozen pre-streaming build logic, retained only for parity tests.

This is a faithful copy of ``ingest.load`` plus the original eager
publication loop as they were before bounded-memory streaming: every
document body is captured and cached up front, sorted by id, then inserted.
The tests build a snapshot with this reference path and compare every table,
rowid and FTS result against the streaming product, so the rewrite is
checked against the old behavior rather than against itself.
"""
import hashlib
import json
import sqlite3
from pathlib import Path

MAX_MANIFEST = 1024 * 1024
MAX_DOCUMENT = 2 * 1024 * 1024


def capture(path, limit):
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{path.name}: exceeds {limit} bytes')
    return data


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def label(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f'{field}: expected nonempty text without outer whitespace')
    if len(value) > 255 or any(ord(c) < 32 for c in value):
        raise ValueError(f'{field}: invalid length or control character')
    return value


def paragraphs(text):
    start, block = None, []
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if start is None:
                start = number
            block.append(line)
        elif block:
            yield start, number - 1, '\n'.join(block)
            start, block = None, []
    if block:
        yield start, start + len(block) - 1, '\n'.join(block)


def legacy_load(manifest):
    manifest = Path(manifest)
    raw = capture(manifest, MAX_MANIFEST)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    docs = value['documents']
    root = manifest.resolve().parent
    ids, paths, result, total = set(), set(), [], 0
    for item in docs:
        relative = Path(item['path'])
        path = root / relative
        resolved = path.resolve()
        if item['id'] in ids or resolved in paths:
            raise ValueError('duplicate document id or path')
        ids.add(item['id'])
        paths.add(resolved)
        data = capture(path, MAX_DOCUMENT)
        total += len(data)
        text = data.decode('utf-8')
        chunks = list(paragraphs(text))
        result.append(dict(item, raw=data,
                           sha256=hashlib.sha256(data).hexdigest(),
                           chunks=chunks))
    return raw, sorted(result, key=lambda d: d['id'])


SCHEMA = '''
PRAGMA foreign_keys=ON;
PRAGMA user_version=1;
CREATE TABLE metadata(manifest_sha256 TEXT NOT NULL, manifest BLOB NOT NULL);
CREATE TABLE documents(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, collection TEXT NOT NULL,
 source_path TEXT NOT NULL, sha256 TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE chunks(
 id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 paragraph INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 body TEXT NOT NULL, UNIQUE(document_id,paragraph));
CREATE VIRTUAL TABLE search USING fts5(title, body, tokenize='unicode61 remove_diacritics 2');
'''


def legacy_build(manifest, database):
    """Reproduce the old eager snapshot, byte for byte at the row level."""
    raw, docs = legacy_load(manifest)
    connection = sqlite3.connect(database)
    with connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO metadata VALUES (?,?)',
                           (hashlib.sha256(raw).hexdigest(), raw))
        count = 0
        for doc in docs:
            connection.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)',
                (doc['id'], doc['title'], doc['collection'], doc['path'],
                 doc['sha256'], doc['raw']))
            for number, (start, end, body) in enumerate(doc['chunks'], 1):
                count += 1
                connection.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?)',
                                   (count, doc['id'], number, start, end, body))
                connection.execute('INSERT INTO search(rowid,title,body) VALUES (?,?,?)',
                                   (count, doc['title'], body))
        connection.execute("INSERT INTO search(search) VALUES ('integrity-check')")
    connection.close()
    return {'documents': len(docs), 'paragraphs': count,
            'manifest_sha256': hashlib.sha256(raw).hexdigest()}
