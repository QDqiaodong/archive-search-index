"""Capture a bounded, local corpus and derive paragraph citations.

Manifest metadata is validated eagerly and completely, but document bodies
are captured lazily through a single-pass :class:`DocumentStream`: each input
file is read at most once, and only one document's bytes, decoded text and
paragraphs are resident at a time.  Peak build memory therefore grows with
the largest document (at most ``MAX_DOCUMENT``), never with corpus size.
"""
import hashlib
import json
from pathlib import Path

MAX_MANIFEST = 1024 * 1024
MAX_DOCUMENT = 2 * 1024 * 1024
MAX_CORPUS = 1024 * 1024 * 1024
MAX_DOCUMENTS = 1000


def capture(path, limit):
    with path.open('rb') as stream:
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
    """Blank lines separate paragraphs; citations are physical 1-based lines."""
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


def parse_manifest(manifest):
    """Validate the manifest and every document entry without reading bodies.

    Returns ``(manifest_bytes, entries)`` with entries sorted by document id
    so streamed rowids stay independent of manifest ordering, matching the
    historical snapshot layout.
    """
    manifest = Path(manifest)
    if manifest.is_symlink():
        raise ValueError('manifest symlink is not supported')
    raw = capture(manifest, MAX_MANIFEST)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != {'schema_version', 'documents'}:
        raise ValueError('manifest requires schema_version and documents only')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported manifest schema_version')
    docs = value['documents']
    if not isinstance(docs, list) or not 1 <= len(docs) <= MAX_DOCUMENTS:
        raise ValueError(f'documents must contain 1..{MAX_DOCUMENTS} entries')
    root = manifest.resolve().parent
    ids, paths, entries = set(), set(), []
    for item in docs:
        if not isinstance(item, dict) or set(item) != {'id', 'title', 'collection', 'path'}:
            raise ValueError('document requires id, title, collection, path only')
        for key in item:
            label(item[key], key)
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('document path must stay inside manifest directory')
        path = root / relative
        if any(p.is_symlink() for p in [path, *path.parents] if p != root):
            raise ValueError('document symlinks are not supported')
        resolved = path.resolve()
        if root not in resolved.parents or not resolved.is_file():
            raise ValueError(f'{item["id"]}: not a regular local document')
        if item['id'] in ids or resolved in paths:
            raise ValueError('duplicate document id or path')
        ids.add(item['id'])
        paths.add(resolved)
        entries.append(dict(item))
    return raw, sorted(entries, key=lambda d: d['id'])


class DocumentStream:
    """Single-pass, bounded-memory iteration over validated documents.

    Each document file is opened and read once.  The SHA256 digest, the
    decoded text, the paragraph citations and the raw BLOB retained by the
    publisher all derive from that one captured byte string; nothing is
    buffered ahead of the current document.
    """

    def __init__(self, manifest, entries, max_document, max_corpus):
        self._manifest = Path(manifest)
        self.entries = entries
        self._max_document = max_document
        self._max_corpus = max_corpus
        self.total_bytes = 0
        self._iterator = self._stream()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def _stream(self):
        root = self._manifest.resolve().parent
        for item in self.entries:
            path = root / Path(item['path'])
            data = capture(path, self._max_document)
            self.total_bytes += len(data)
            if self.total_bytes > self._max_corpus:
                raise ValueError(
                    f'corpus exceeds {self._max_corpus} bytes')
            text = data.decode('utf-8')
            if '\x00' in text:
                raise ValueError('NUL is not supported in text documents')
            chunks = list(paragraphs(text))
            if not chunks:
                raise ValueError(f'{item["id"]}: document contains no paragraphs')
            yield dict(item, raw=data,
                       sha256=hashlib.sha256(data).hexdigest(),
                       chunks=chunks)


def load(manifest):
    """Validate manifest metadata and expose documents as a bounded stream.

    The interface is unchanged: ``(manifest_bytes, documents)`` where
    ``documents`` yields one document dictionary at a time.  The iterable is
    now single-pass: document bodies are captured on iteration instead of
    being cached ahead of the caller.
    """
    raw, entries = parse_manifest(manifest)
    return raw, DocumentStream(manifest, entries, MAX_DOCUMENT, MAX_CORPUS)
