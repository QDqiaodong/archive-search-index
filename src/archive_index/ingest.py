"""Capture a bounded, local corpus and derive paragraph citations."""
import hashlib
import json
from pathlib import Path

MAX_MANIFEST = 1024 * 1024
MAX_DOCUMENT = 2 * 1024 * 1024
MAX_CORPUS = 20 * 1024 * 1024


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


def load(manifest):
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
    if not isinstance(docs, list) or not 1 <= len(docs) <= 1000:
        raise ValueError('documents must contain 1..1000 entries')
    root = manifest.resolve().parent
    ids, paths, result, total = set(), set(), [], 0
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
        data = capture(path, MAX_DOCUMENT)
        total += len(data)
        if total > MAX_CORPUS:
            raise ValueError('corpus exceeds 20 MiB')
        text = data.decode('utf-8')
        if '\x00' in text:
            raise ValueError('NUL is not supported in text documents')
        chunks = list(paragraphs(text))
        if not chunks:
            raise ValueError(f'{item["id"]}: document contains no paragraphs')
        result.append(dict(item, raw=data, sha256=hashlib.sha256(data).hexdigest(), chunks=chunks))
    return raw, sorted(result, key=lambda d: d['id'])
