"""Independent, seed-driven oracle for the archive search contract.

Nothing in this module imports or calls ``archive_index``.  Paragraphs, line
numbers, source digests and index terms are all derived directly from the raw
bytes that the corpus generator writes.  Queries are evaluated by a small
reference interpreter for the subset of FTS5 MATCH syntax exercised by the
regression bank: ASCII words, quoted phrases, and explicitly grouped binary
AND / OR / NOT.

FTS5 facts encoded here (verified against SQLite, not inferred from the
product implementation):

* ``unicode61`` folds ASCII case and splits on every non-alphanumeric
  character, so a word token is ``[a-z0-9]+`` over the folded text.
* A phrase matches only a contiguous run of tokens *within one column*; it
  never spans the title/body boundary.  Adjacent paragraphs are separate
  index rows.
* ``NOT`` is a binary operator (left AND NOT right); it is never a unary
  prefix.
* Rows with identical title and body bytes have identical BM25 statistics.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time

SEEDS = (20261114, 20261115, 20261116)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Words that appear only in titles: a title-only query must still retrieve
# every paragraph of the document, including paragraphs without the term.
TITLE_ONLY = ('zephyr', 'quayside', 'shelftoken', 'meridian', 'gazette', 'kestrel')

BODY_WORDS = (
    'harbor', 'gate', 'restoration', 'mortar', 'lime', 'winter', 'maintenance',
    'copper', 'tide', 'beacon', 'lantern', 'ledger', 'archive', 'catalogue',
    'workshop', 'notebook', 'dry', 'storage', 'room', 'rivet', 'canvas', 'rope',
    'register', 'summary', 'appendix', 'batch', 'gauge', 'reading', 'quay',
    'crate', 'signal', 'survey', 'anchor', 'pilot', 'chart', 'compass', 'shed',
    'timber', 'stone', 'brass', 'varnish', 'photograph', 'desk', 'map', 'log',
    'repair', 'seal',
)

ABSENT_WORDS = ('absentword', 'notaword', 'phantom', 'vacant')

_COLLECTIONS = ('engineering', 'library', 'survey')
_WORD_RE = re.compile(r'[a-z0-9]+')
_QUERY_TOKEN_RE = re.compile(r'\s*(\(|\)|"[^"]*"|[A-Za-z0-9]+)')
_OPERATORS = ('AND', 'OR', 'NOT')
# Words that also spell an operator; FTS5 rejects them as bare terms, so the
# reference query bank quotes them.  They still exist in ordinary prose.
_RESERVED = ('and', 'or', 'not')


# ---------------------------------------------------------------------------
# Raw-byte derivation
# ---------------------------------------------------------------------------

def raw_lines(raw):
    """Physical lines from raw bytes, LF and CRLF only, 1-based ordering.

    This is deliberately a separate implementation from ingest.paragraphs:
    decode, normalize CRLF to LF, split on LF, and drop the synthetic final
    empty line created by a trailing newline.
    """
    text = raw.decode('utf-8')
    if '\r' in text.replace('\r\n', ''):
        raise ValueError('reference corpus only supports LF and CRLF')
    lines = text.replace('\r\n', '\n').split('\n')
    if text.endswith('\n'):
        lines.pop()
    return lines


def raw_paragraphs(raw):
    """(start_line, end_line, LF-joined body) from raw bytes."""
    lines = raw_lines(raw)
    result = []
    start = None
    block = []
    for number, line in enumerate(lines, 1):
        if line.strip():
            if start is None:
                start = number
            block.append(line)
        elif block:
            result.append((start, number - 1, '\n'.join(block)))
            start, block = None, []
    if block:
        result.append((start, start + len(block) - 1, '\n'.join(block)))
    return result


def tokens(text):
    """unicode61's ASCII token stream for the vocabulary used here."""
    return _WORD_RE.findall(text.lower())


def derive_rows(corpus):
    """One reference row per paragraph, from raw bytes only."""
    rows = []
    for document in corpus['documents']:
        raw = document['raw']
        sha256 = hashlib.sha256(raw).hexdigest()
        title_tokens = tokens(document['title'])
        for number, (start, end, body) in enumerate(raw_paragraphs(raw), 1):
            rows.append({
                'document_id': document['id'],
                'title': document['title'],
                'collection': document['collection'],
                'source_path': document['path'],
                'source_sha256': sha256,
                'paragraph': number,
                'start_line': start,
                'end_line': end,
                'text': body,
                'title_tokens': title_tokens,
                'body_tokens': tokens(body),
            })
    return rows


# ---------------------------------------------------------------------------
# Corpus generation
# ---------------------------------------------------------------------------

def _sentence(rng, required=(), size=None):
    size = size or rng.randint(4, 8)
    pool = [word for word in BODY_WORDS if word not in required]
    chosen = list(required) + rng.sample(pool, max(0, size - len(required)))
    rng.shuffle(chosen)
    text = ' '.join(chosen)
    return text[:1].upper() + text[1:] + '.'


def _document(identifier, title, collection, path, raw):
    return {'id': identifier, 'title': title, 'collection': collection,
            'path': path, 'raw': raw}


def build_corpus(seed):
    """Deterministic small multi-collection corpus for ``seed``.

    Required features in every corpus: duplicate whole bodies across two
    documents with identical titles, a repeated paragraph inside one document,
    a paragraph duplicated across two documents, CRLF with leading/trailing
    blank lines and whitespace-only separators, title-only terms, and at least
    three collections.
    """
    rng = __import__('random').Random(seed)
    documents = []

    # D01: plain LF, two fixed paragraphs plus seeded filler.
    body = ('The harbor gate restoration used lime mortar.\n\n'
            'Winter maintenance requires a dry storage room.\n\n'
            + _sentence(rng, ('copper', 'timber', 'shed')) + '\n')
    documents.append(_document('D01', 'Zephyr harbor ledger', 'engineering',
                               'd01.txt', body.encode('utf-8')))

    # D02: library collection, shares harbor/copper with other documents.
    body = ('Readers can request archive photographs at the catalogue desk.\n\n'
            'The workshop includes harbor notebooks and copper charts.\n')
    documents.append(_document('D02', 'Quayside reading guide', 'library',
                               'd02.txt', body.encode('utf-8')))

    # D03: CRLF throughout, leading blank lines, whitespace-only separator,
    # trailing blank lines.
    lines = ['', '', 'Copper rivets resist tide corrosion.', '',
             '\t \t', 'Lantern rope canvas inventory.', '',
             'Tide gauge readings from the quay.', '', '']
    raw = '\r\n'.join(lines).encode('utf-8') + b'\r\n'
    assert b'\r\n\r\nCopper' in raw
    documents.append(_document('D03', 'Shelftoken tide survey', 'survey',
                               'd03.txt', raw))

    # D04/D05: byte-identical title and body, distinct identity and path.
    shared = ('Shared bulletin copper beacon.\n\n'
              'Shared bulletin tide register.\n')
    documents.append(_document('D04', 'Meridian shared bulletin',
                               'engineering', 'd04.txt', shared.encode('utf-8')))
    documents.append(_document('D05', 'Meridian shared bulletin',
                               'engineering', 'd05.txt', shared.encode('utf-8')))

    # D06: repeated paragraph inside one document, then a unique tail.
    body = ('Repeated note delta.\n\nRepeated note delta.\n\n'
            + _sentence(rng, ('unique', 'tail')) + '\n')
    documents.append(_document('D06', 'Gazette duplicate digest', 'library',
                               'd06.txt', body.encode('utf-8')))

    # D07: first paragraph is byte-identical to D01's second paragraph;
    # no trailing newline.
    body = ('Winter maintenance requires a dry storage room.\n\n'
            + _sentence(rng, ('survey', 'anchor')) + '\n\n'
            + _sentence(rng, ('pilot', 'chart'), size=6))
    documents.append(_document('D07', 'Kestrel harbor survey', 'survey',
                               'd07.txt', body.encode('utf-8')))

    ordered = list(documents)
    rng.shuffle(ordered)
    manifest = {
        'schema_version': 1,
        'documents': [{'id': d['id'], 'title': d['title'],
                       'collection': d['collection'], 'path': d['path']}
                      for d in ordered],
    }
    return {'seed': seed, 'manifest': manifest, 'documents': documents}


def write_corpus(root, corpus, order=None):
    """Materialize the corpus; ``order`` is a list of document IDs."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for document in corpus['documents']:
        target = root / document['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(document['raw'])
    documents = corpus['manifest']['documents']
    if order is not None:
        by_id = {item['id']: item for item in documents}
        documents = [by_id[identifier] for identifier in order]
    manifest = {'schema_version': 1, 'documents': documents}
    (root / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    return manifest


def corpus_from_directory(root):
    """Reconstruct a corpus (raw bytes included) from a saved bundle."""
    root = Path(root)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    documents = []
    for item in manifest['documents']:
        documents.append({
            'id': item['id'], 'title': item['title'],
            'collection': item['collection'], 'path': item['path'],
            'raw': (root / item['path']).read_bytes(),
        })
    seed = None
    seed_file = root / 'seed.txt'
    if seed_file.exists():
        try:
            seed = int(seed_file.read_text(encoding='utf-8').strip())
        except ValueError:
            seed = None
    return {'seed': seed, 'manifest': manifest, 'documents': documents}


# ---------------------------------------------------------------------------
# Reference query language
# ---------------------------------------------------------------------------

def parse_query(expression):
    """Parse the exercised MATCH subset into a small AST."""
    parts = []
    position = 0
    while position < len(expression):
        match = _QUERY_TOKEN_RE.match(expression, position)
        if not match:
            if expression[position:].strip():
                raise ValueError(f'unparsable reference query at {position}')
            break
        parts.append(match.group(1))
        position = match.end()
    if not parts:
        raise ValueError('empty reference query')

    cursor = 0

    def atom():
        nonlocal cursor
        if cursor >= len(parts):
            raise ValueError('reference query ends early')
        part = parts[cursor]
        if part == '(':
            cursor += 1
            node = or_expression()
            if cursor >= len(parts) or parts[cursor] != ')':
                raise ValueError('reference query missing )')
            cursor += 1
            return node
        if part in _OPERATORS or part == ')':
            raise ValueError(f'reference query unexpected {part}')
        cursor += 1
        return ('leaf', part)

    def and_expression():
        # Implicit whitespace between atoms is FTS5's AND and binds at the
        # same level as the AND keyword, tighter than NOT and OR.
        nonlocal cursor
        node = atom()
        while cursor < len(parts):
            following = parts[cursor]
            if following == 'AND':
                cursor += 1
            elif following in ('OR', 'NOT', ')'):
                break
            else:
                pass  # implicit adjacency
            node = ('AND', node, atom())
        return node

    def not_expression():
        nonlocal cursor
        node = and_expression()
        while cursor < len(parts) and parts[cursor] == 'NOT':
            cursor += 1
            node = ('NOT', node, and_expression())
        return node

    def or_expression():
        nonlocal cursor
        node = not_expression()
        while cursor < len(parts) and parts[cursor] == 'OR':
            cursor += 1
            node = ('OR', node, not_expression())
        return node

    tree = or_expression()
    if cursor != len(parts):
        raise ValueError('reference query has trailing input')
    return tree


def _phrase_tokens(atom_text):
    if atom_text.startswith('"'):
        return tokens(atom_text.strip('"'))
    return tokens(atom_text)


def _has_phrase(sequence, phrase):
    if not phrase:
        return False
    width = len(phrase)
    return any(sequence[index:index + width] == phrase
               for index in range(len(sequence) - width + 1))


def evaluate(tree, row):
    if tree[0] == 'leaf':
        phrase = _phrase_tokens(tree[1])
        return (_has_phrase(row['title_tokens'], phrase)
                or _has_phrase(row['body_tokens'], phrase))
    operator, left, right = tree
    left_value = evaluate(left, row)
    right_value = evaluate(right, row)
    if operator == 'AND':
        return left_value and right_value
    if operator == 'OR':
        return left_value or right_value
    return left_value and not right_value


def expected_hits(rows, expression, collection=None):
    tree = parse_query(expression)
    return [row for row in rows
            if (collection is None or row['collection'] == collection)
            and evaluate(tree, row)]


# ---------------------------------------------------------------------------
# Query bank
# ---------------------------------------------------------------------------

_ESSENTIAL_QUERIES = (
    'zephyr', 'ZEPHYR', 'quayside', 'shelftoken', 'meridian', 'gazette',
    'kestrel', 'shared', '"shared bulletin"', 'shared bulletin',
    'repeated', '"repeated note"', '"unique tail"', 'dry storage',
    '"dry storage"', '"harbor gate"', '"lime mortar"', '"winter maintenance"',
    '"tide gauge"', 'restoration', 'harbor', 'beacon', 'tide', 'copper',
    'ledger', 'absentword', 'notaword',
    '(harbor OR beacon) AND copper',
    '((restoration OR tide) NOT mortar)',
    '(copper NOT harbor)',
    '(harbor NOT (copper OR beacon))',
    '("lime mortar" OR "tide gauge")',
    '(("shared bulletin" OR repeated) AND copper)',
    '((meridian OR zephyr) NOT copper)',
    'anchor OR timber OR brass',
    '((anchor AND timber) OR beacon)',
    '(kestrel NOT harbor)',
    '"repeated note" OR "unique tail"',
    '(dry AND storage) OR tide',
)


def _random_expression(rng, atoms, depth):
    if depth <= 0 or rng.random() < 0.3:
        return rng.choice(atoms)
    operator = rng.choice(_OPERATORS)
    return (f'({_random_expression(rng, atoms, depth - 1)} {operator} '
            f'{_random_expression(rng, atoms, depth - 1)})')


def query_bank(seed, corpus):
    """Deterministic queries covering words, phrases and boolean groups."""
    import random
    rng = random.Random(seed * 7 + 11)
    rows = derive_rows(corpus)
    vocabulary = sorted({token for row in rows
                         for token in row['title_tokens'] + row['body_tokens']})
    queries = list(_ESSENTIAL_QUERIES)

    def atom_for(token):
        # Reserved words cannot stand alone in MATCH; quote them so the same
        # text token is still exercised.
        return f'"{token}"' if token in _RESERVED else token

    for word in rng.sample(vocabulary, min(6, len(vocabulary))):
        queries.append(atom_for(word))
        if rng.random() < 0.3:
            queries.append(atom_for(word).upper())
    for word in rng.sample(ABSENT_WORDS, 3):
        queries.append(word)

    # Real adjacent bigrams from either column, including title phrases.
    for _ in range(6):
        row = rng.choice(rows)
        column = row['title_tokens'] if rng.random() < 0.4 else row['body_tokens']
        if len(column) < 2:
            continue
        index = rng.randrange(len(column) - 1)
        queries.append('"' + ' '.join(column[index:index + 2]) + '"')
    # Synthetic bigrams, usually absent.
    for _ in range(3):
        first, second = rng.sample(vocabulary, 2)
        queries.append(f'"{first} {second}"')

    atoms = []
    for _ in range(6):
        atoms.append(atom_for(rng.choice(vocabulary)))
    atoms.extend(rng.sample(ABSENT_WORDS, 2))
    for _ in range(3):
        row = rng.choice(rows)
        column = row['title_tokens'] + row['body_tokens']
        if len(column) >= 2:
            index = rng.randrange(len(column) - 1)
            atoms.append('"' + ' '.join(column[index:index + 2]) + '"')
    for _ in range(10):
        queries.append(_random_expression(rng, atoms, rng.randint(2, 3)))

    unique = []
    seen = set()
    for query in queries:
        if query not in seen:
            seen.add(query)
            unique.append(query)
    return unique


# ---------------------------------------------------------------------------
# Comparison and failure bundles
# ---------------------------------------------------------------------------

_RESULT_FIELDS = ('title', 'collection', 'source_path', 'source_sha256',
                  'start_line', 'end_line', 'text')


def check_hits(actual, rows, expression, collection=None,
               expect_complete=True):
    """Return human-readable discrepancies against the reference.

    ``expect_complete=False`` checks only that every returned hit is correct
    and unique, which is what a ``limit``-truncated result can promise.
    """
    expected = expected_hits(rows, expression, collection)
    expected_by_key = {(row['document_id'], row['paragraph']): row
                       for row in expected}
    issues = []
    seen = set()
    for hit in actual:
        key = (hit['document_id'], hit['paragraph'])
        if key in seen:
            issues.append(f'duplicate hit {key}')
            continue
        seen.add(key)
        reference = expected_by_key.get(key)
        if reference is None:
            issues.append(f'unexpected hit {key}')
            continue
        for field in _RESULT_FIELDS:
            if hit[field] != reference[field]:
                issues.append(f'{key} field {field}: {hit[field]!r} != '
                              f'{reference[field]!r}')
    if expect_complete:
        for key in sorted(set(expected_by_key) - seen):
            issues.append(f'missing hit {key}')
    return issues


def expected_key_list(rows, expression, collection=None):
    return sorted((row['document_id'], row['paragraph'])
                  for row in expected_hits(rows, expression, collection))


def ordering_issues(hits):
    """Check the documented ordering without asserting any BM25 value.

    Lower rank first, then document ID, then paragraph number.  Two hits
    whose indexed text is byte-identical must share a rank, because BM25 is a
    function of the indexed bytes and the global corpus statistics.
    """
    issues = []
    order = [(hit['rank'], hit['document_id'], hit['paragraph'])
             for hit in hits]
    if order != sorted(order):
        issues.append(f'not ordered by rank, document ID, paragraph: {order}')
    seen = set()
    groups = {}
    for hit in hits:
        key = (hit['document_id'], hit['paragraph'])
        if key in seen:
            issues.append(f'duplicate hit {key}')
        seen.add(key)
        groups.setdefault((hit['title'], hit['text']), []).append(hit)
    for (title, text), group in groups.items():
        ranks = {hit['rank'] for hit in group}
        if len(ranks) > 1:
            issues.append(f'identical text {text!r} under title {title!r} '
                          f'has unequal ranks {sorted(ranks)}')
    return issues


def failure_root():
    return Path(os.environ.get(
        'ARCHIVE_PROPERTY_FAILURE_DIR',
        REPO_ROOT / 'run' / 'property-failures'))


def replay_argv(case_directory, query, collection=None, limit=None,
                via_cli=False, mode='search'):
    argv = [sys_executable(), str(REPO_ROOT / 'tools' / 'property_probe.py'),
            '--corpus', str(case_directory), '--mode', mode]
    if query:
        argv += ['--query', query]
    if collection is not None:
        argv += ['--collection', collection]
    if limit is not None:
        argv += ['--limit', str(limit)]
    if via_cli:
        argv.append('--via-cli')
    return argv


def sys_executable():
    import sys
    return sys.executable


def replay_command(case_directory, query, collection=None, limit=None,
                   via_cli=False, mode='search'):
    argv = replay_argv(case_directory, query, collection, limit, via_cli, mode)
    quoted = ' '.join(shlex.quote(part) for part in argv)
    return f'cd {shlex.quote(str(REPO_ROOT))} && PYTHONPATH=src {quoted}'


def write_failure_bundle(test, corpus, query, collection=None, limit=None,
                         via_cli=False, detail=None, root=None,
                         mode='search'):
    """Persist corpus, query, seed and a single-case replay command.

    ``mode`` selects what the replay probe re-executes: the plain search
    comparison, the ordering rules, manifest reordering, rebuild equivalence,
    or the real CLI build.  Every mode needs only the saved corpus.
    """
    base = Path(root) if root is not None else failure_root()
    base.mkdir(parents=True, exist_ok=True)
    stamp = f'{int(time.time() * 1000)}-{os.getpid()}'
    case = base / f'seed-{corpus.get("seed")}-{stamp}'
    counter = 0
    while case.exists():
        counter += 1
        case = base / f'seed-{corpus.get("seed")}-{stamp}-{counter}'
    case.mkdir(parents=True)
    for document in corpus['documents']:
        target = case / document['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(document['raw'])
    (case / 'manifest.json').write_text(
        json.dumps(corpus['manifest'], ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    (case / 'seed.txt').write_text(f'{corpus.get("seed")}\n', encoding='utf-8')
    request = {'test': test, 'seed': corpus.get('seed'), 'query': query,
               'collection': collection, 'limit': limit, 'via_cli': via_cli,
               'detail': detail, 'cwd': str(REPO_ROOT), 'mode': mode}
    (case / 'request.json').write_text(
        json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    if query:
        (case / 'query.txt').write_text(query + '\n', encoding='utf-8')
    command = replay_command(case, query, collection, limit, via_cli, mode)
    (case / 'replay.sh').write_text('#!/bin/sh\nset -eu\n' + command + '\n',
                                    encoding='utf-8')
    os.chmod(case / 'replay.sh', 0o755)
    (case / 'README.txt').write_text(
        'Property-regression failure bundle.\n'
        'Contains the exact corpus bytes, manifest, query, seed, request and a\n'
        'single-case replay command.  Expected results are recomputed from the\n'
        'raw bytes by tests/property_oracle.py, never by the product.\n'
        f'Mode: {mode}\n'
        f'Replay: {command}\n', encoding='utf-8')
    return case, command
