"""Publish a standalone SQLite search snapshot after complete validation.

Validation and publication are fused into one streaming pass: each document
is captured, checked and inserted immediately, so memory stays bounded by
the largest document rather than by the whole corpus.  The snapshot is still
all-or-nothing: any failure happens before the target is replaced, leaving a
previous database untouched and removing the temporary database file.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from .ingest import load

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


def build(manifest, database):
    raw, docs = load(manifest)
    target = Path(database).absolute()
    if target.is_symlink():
        raise ValueError('database target cannot be a symlink')
    manifest_path = Path(manifest).resolve()
    if target.resolve() == manifest_path:
        raise ValueError('database would overwrite manifest')
    root = manifest_path.parent
    for entry in docs.entries:
        if target.resolve() == (root / entry['path']).resolve():
            raise ValueError('database would overwrite an input document')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.archive-index-', suffix='.sqlite', dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        with connection:
            connection.executescript(SCHEMA)
            connection.execute('INSERT INTO metadata VALUES (?,?)', (hashlib.sha256(raw).hexdigest(), raw))
            count = 0
            documents = 0
            for doc in docs:
                documents += 1
                connection.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)',
                    (doc['id'],doc['title'],doc['collection'],doc['path'],doc['sha256'],doc['raw']))
                for number, (start,end,body) in enumerate(doc['chunks'],1):
                    count += 1
                    connection.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?)',
                                       (count,doc['id'],number,start,end,body))
                    connection.execute('INSERT INTO search(rowid,title,body) VALUES (?,?,?)',
                                       (count,doc['title'],body))
            connection.execute("INSERT INTO search(search) VALUES ('integrity-check')")
            if connection.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('internal citation foreign key failure')
        # sqlite3's context manager commits but does not close its connection.
        connection.close()
        connection = None
        temporary.replace(target)
        return {'documents': documents, 'paragraphs': count,
                'manifest_sha256': hashlib.sha256(raw).hexdigest()}
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
