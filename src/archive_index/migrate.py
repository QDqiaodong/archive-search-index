"""Migrate paragraph citations from an old archive snapshot to a new one.

Both databases are opened read-only and read inside a single transaction,
then verified against their own original bytes: the stored SHA256 must
match the raw text, and the stored paragraph/line citations must equal
those rebuilt from the raw text. Citations are checked against old-library
facts before any alignment runs; any bad citation or bad library fails the
whole run.

Only documents with the same ID are aligned. Paragraphs match by exact LF
text, and the alignment is an order-preserving one-to-one matching of
maximum size. All maximum matchings are characterized analytically with
dynamic programming and are never enumerated: a cited paragraph migrates
only when every maximum matching pairs it with the same new paragraph.
"""
import hashlib
import json
import os
from array import array
from collections import defaultdict
from pathlib import Path
import sqlite3
import tempfile

from .ingest import capture, label, paragraphs, unique_object

MAX_CITATIONS_FILE = 16 * 1024 * 1024
MAX_CITATIONS = 100000
MAX_ALIGNMENT_CELLS = 16_000_000
_WORD = 'I' if array('I').itemsize >= 4 else 'L'

CITATION_KEYS = ('document_id', 'sha256', 'paragraph', 'start_line', 'end_line')
STATUSES = ('migrated', 'ambiguous', 'unmatched', 'vanished')


def load_citations(path):
    """Read and strictly validate a citations JSON file."""
    raw = capture(Path(path), MAX_CITATIONS_FILE)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != {'schema_version', 'citations'}:
        raise ValueError('citations file requires schema_version and citations only')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported citations schema_version')
    items = value['citations']
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CITATIONS:
        raise ValueError(f'citations must contain 1..{MAX_CITATIONS} entries')
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) != set(CITATION_KEYS):
            raise ValueError('citation requires document_id, sha256, paragraph, '
                             'start_line, end_line only')
        label(item['document_id'], 'document_id')
        sha256 = item['sha256']
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(c not in '0123456789abcdef' for c in sha256)):
            raise ValueError('sha256: expected 64 lowercase hexadecimal characters')
        for key in ('paragraph', 'start_line', 'end_line'):
            if type(item[key]) is not int or item[key] < 1:
                raise ValueError(f'{key}: expected a positive integer')
        if item['start_line'] > item['end_line']:
            raise ValueError('start_line must not exceed end_line')
        result.append({key: item[key] for key in CITATION_KEYS})
    return result


def load_library(path):
    """Read one consistent read-only snapshot and verify it against itself.

    Returns {document_id: {'sha256': hex, 'paragraphs': [(start, end, text)]}}
    with paragraphs rebuilt from the stored original bytes. Any digest or
    rebuild mismatch, foreign key violation or unsupported version fails.
    """
    resolved = Path(path).resolve()
    connection = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None)
    try:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('unsupported archive database version')
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('archive database fails foreign key check')
        documents = connection.execute(
            'SELECT id, sha256, raw FROM documents ORDER BY id').fetchall()
        chunks = connection.execute(
            'SELECT document_id, paragraph, start_line, end_line, body '
            'FROM chunks ORDER BY document_id, paragraph').fetchall()
        connection.execute('COMMIT')
    finally:
        connection.close()
    grouped = defaultdict(list)
    for document_id, paragraph, start, end, body in chunks:
        grouped[document_id].append((paragraph, start, end, body))
    library = {}
    for document_id, sha256, raw in documents:
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError(f'{document_id}: stored sha256 does not match the original text')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError(f'{document_id}: original text is not valid UTF-8') from None
        rebuilt = [(number, start, end, body)
                   for number, (start, end, body) in enumerate(paragraphs(text), 1)]
        if grouped.pop(document_id, []) != rebuilt:
            raise ValueError(f'{document_id}: stored paragraphs do not match the original text')
        library[document_id] = {'sha256': sha256,
                                'paragraphs': [(start, end, body)
                                               for start, end, body in paragraphs(text)]}
    if grouped:
        raise ValueError('stored paragraphs reference unknown documents')
    return library


def analyze(old, new):
    """Characterize every maximum matching between two LF paragraph sequences.

    Returns (maximum, analyses): maximum is the largest size of an
    order-preserving one-to-one matching by exact paragraph equality, and
    analyses holds one (candidates, possibly_unmatched) pair per old
    paragraph. candidates lists every new paragraph index (0-based) paired
    with the old paragraph in at least one maximum matching;
    possibly_unmatched is True when some maximum matching leaves the old
    paragraph unmatched. A pair (i, j) lies on some maximum matching exactly
    when old[i] == new[j] and lcs(old[:i], new[:j]) + 1 +
    lcs(old[i+1:], new[j+1:]) == maximum; old paragraph i can stay unmatched
    exactly when lcs(old without i, new) == maximum. Both facts come from
    one forward and one backward DP table; no matching is ever enumerated.
    """
    m, n = len(old), len(new)
    if (m + 1) * (n + 1) > MAX_ALIGNMENT_CELLS:
        raise ValueError(f'alignment of {m} x {n} paragraphs exceeds '
                         f'{MAX_ALIGNMENT_CELLS} cells')
    forward = [array(_WORD, [0]) * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        value, row, previous = old[i - 1], forward[i], forward[i - 1]
        for j in range(1, n + 1):
            if value == new[j - 1]:
                row[j] = previous[j - 1] + 1
            elif previous[j] >= row[j - 1]:
                row[j] = previous[j]
            else:
                row[j] = row[j - 1]
    backward = [array(_WORD, [0]) * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        value, row, following = old[i], backward[i], backward[i + 1]
        for j in range(n - 1, -1, -1):
            if value == new[j]:
                row[j] = following[j + 1] + 1
            elif following[j] >= row[j + 1]:
                row[j] = following[j]
            else:
                row[j] = row[j + 1]
    maximum = forward[m][n]
    positions = defaultdict(list)
    for j, value in enumerate(new):
        positions[value].append(j)
    analyses = []
    for i in range(m):
        before, after = forward[i], backward[i + 1]
        candidates = tuple(j for j in positions[old[i]]
                           if before[j] + 1 + backward[i + 1][j + 1] == maximum)
        possibly_unmatched = any(before[j] + after[j] == maximum
                                 for j in range(n + 1))
        analyses.append((candidates, possibly_unmatched))
    return maximum, analyses


def _paragraph_entries(document):
    return [{'paragraph': number, 'start_line': start, 'end_line': end, 'text': text}
            for number, (start, end, text) in enumerate(document['paragraphs'], 1)]


def migrate(old_database, new_database, citations_file):
    """Verify inputs and build the deterministic migration report."""
    citations = load_citations(citations_file)
    old_library = load_library(old_database)
    new_library = load_library(new_database)
    for citation in citations:
        document = old_library.get(citation['document_id'])
        if document is None:
            raise ValueError(f'citation document {citation["document_id"]} '
                             'is not in the old library')
        if document['sha256'] != citation['sha256']:
            raise ValueError(f'{citation["document_id"]}: citation sha256 does not '
                             'match the old library')
        if citation['paragraph'] > len(document['paragraphs']):
            raise ValueError(f'{citation["document_id"]}: citation paragraph '
                             f'{citation["paragraph"]} is not in the old library')
        start, end, _ = document['paragraphs'][citation['paragraph'] - 1]
        if (citation['start_line'], citation['end_line']) != (start, end):
            raise ValueError(f'{citation["document_id"]}: citation lines do not '
                             'match the old library')
    alignments, analyses = {}, {}
    for document_id in sorted({c['document_id'] for c in citations}):
        old_document = old_library[document_id]
        entry = {'document_id': document_id,
                 'old': {'sha256': old_document['sha256'],
                         'paragraphs': _paragraph_entries(old_document)},
                 'new': None}
        new_document = new_library.get(document_id)
        if new_document is not None:
            maximum, per_paragraph = analyze(
                [text for _, _, text in old_document['paragraphs']],
                [text for _, _, text in new_document['paragraphs']])
            analyses[document_id] = per_paragraph
            entry['new'] = {'sha256': new_document['sha256'],
                            'paragraphs': _paragraph_entries(new_document)}
            entry['max_matches'] = maximum
        alignments[document_id] = entry
    results = []
    for citation in citations:
        outcome = dict(citation)
        new_document = new_library.get(citation['document_id'])
        if new_document is None:
            outcome.update(status='vanished', candidates=[], possibly_unmatched=None)
        else:
            candidates, possibly_unmatched = \
                analyses[citation['document_id']][citation['paragraph'] - 1]
            listed = [{'paragraph': j + 1,
                       'start_line': new_document['paragraphs'][j][0],
                       'end_line': new_document['paragraphs'][j][1]}
                      for j in candidates]
            outcome['candidates'] = listed
            outcome['possibly_unmatched'] = possibly_unmatched
            if len(listed) == 1 and not possibly_unmatched:
                outcome['status'] = 'migrated'
                outcome['new_paragraph'] = listed[0]['paragraph']
                outcome['new_start_line'] = listed[0]['start_line']
                outcome['new_end_line'] = listed[0]['end_line']
            elif listed:
                outcome['status'] = 'ambiguous'
            else:
                outcome['status'] = 'unmatched'
        results.append(outcome)
    return {
        'schema_version': 1,
        'libraries': {side: {'documents': [{'id': document_id, 'sha256': d['sha256']}
                                           for document_id, d in sorted(library.items())]}
                      for side, library in (('old', old_library), ('new', new_library))},
        'alignments': [alignments[document_id] for document_id in sorted(alignments)],
        'citations': results}


def write_report(report, target, inputs=()):
    """Atomically publish the report; failure preserves any previous report.

    The target must not be a symlink and must not overwrite any input.
    """
    target = Path(target).absolute()
    if target.is_symlink():
        raise ValueError('report target cannot be a symlink')
    for other in inputs:
        if target.resolve() == Path(other).resolve():
            raise ValueError(f'report would overwrite input {other}')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.citation-migration-', suffix='.json',
                                dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=1)
            stream.write('\n')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
