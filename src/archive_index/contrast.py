"""Compare the body vocabulary of two collections inside one search snapshot.

Every count comes from the existing FTS5 index itself: ``fts5vocab`` in
instance mode yields one row per indexed token occurrence, so case folding,
accent removal and Chinese token boundaries are exactly those applied when
the snapshot was built.  Titles are excluded by filtering on the ``body``
column.  Terms are aggregated inside SQLite (counts and distinct documents);
Python only keeps the best ``limit`` aggregates in a bounded heap, never the
token occurrences.

Ranking uses the exact rational ratio

    ((DF_A + 1) / (N_A + 2)) / ((DF_B + 1) / (N_B + 2))

compared by integer cross-multiplication, so no floating-point ranking is
involved.  The reported numerator/denominator are the reduced integers of
that ratio.  Equal ratios sort by Unicode term order.
"""
import heapq
import json
from math import gcd
import os
from pathlib import Path
import sqlite3
import tempfile

from .ingest import label

# One row per indexed token occurrence.  ``doc`` is the FTS5 rowid, i.e. the
# matching chunk row; ``col`` names the indexed column.  Aggregation happens
# inside SQLite, so Python only ever sees one row per distinct term.
_AGGREGATE = '''
SELECT v.term,
       COALESCE(SUM(CASE WHEN d.collection = ? THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN d.collection = ? THEN 1 ELSE 0 END), 0),
       COUNT(DISTINCT CASE WHEN d.collection = ? THEN d.id END),
       COUNT(DISTINCT CASE WHEN d.collection = ? THEN d.id END)
FROM temp.contrast_vocabulary v
JOIN chunks c ON c.id = v.doc
JOIN documents d ON d.id = c.document_id
WHERE v.col = 'body' AND d.collection IN (?, ?)
GROUP BY v.term
'''


class _Candidate:
    """One term aggregate ordered by descending ratio, then Unicode term.

    ``__lt__`` is deliberately "ranks after": a min-heap of these objects
    keeps the lowest-ranked term at its root, so a bounded heap can discard
    it as better terms arrive.
    """

    __slots__ = ('term', 'tf_a', 'df_a', 'tf_b', 'df_b',
                 'numerator', 'denominator')

    def __init__(self, term, tf_a, df_a, tf_b, df_b, documents_a, documents_b):
        self.term = term
        self.tf_a = tf_a
        self.df_a = df_a
        self.tf_b = tf_b
        self.df_b = df_b
        numerator = (df_a + 1) * (documents_b + 2)
        denominator = (documents_a + 2) * (df_b + 1)
        divisor = gcd(numerator, denominator)
        self.numerator = numerator // divisor
        self.denominator = denominator // divisor

    def __lt__(self, other):
        left = self.numerator * other.denominator
        right = other.numerator * self.denominator
        if left != right:
            return left < right
        return self.term > other.term

    def as_report(self):
        return {
            'term': self.term,
            'a': {'tf': self.tf_a, 'df': self.df_a},
            'b': {'tf': self.tf_b, 'df': self.df_b},
            'ratio': {'numerator': self.numerator,
                      'denominator': self.denominator},
        }


def _collection_counts(connection, collection_a, collection_b):
    counts = dict(connection.execute(
        'SELECT collection, COUNT(*) FROM documents '
        'WHERE collection IN (?, ?) GROUP BY collection',
        (collection_a, collection_b)).fetchall())
    for name, field in ((collection_a, 'collection_a'),
                        (collection_b, 'collection_b')):
        if not counts.get(name):
            raise ValueError(f'{field} does not exist in the database')
    return counts[collection_a], counts[collection_b]


def compare(database, collection_a, collection_b, limit):
    """Return the deterministic contrast report for two collections.

    The database is opened read-only and both the document counts and the
    vocabulary aggregate come from one transaction, i.e. one snapshot.
    """
    collection_a = label(collection_a, 'collection_a')
    collection_b = label(collection_b, 'collection_b')
    if collection_a == collection_b:
        raise ValueError('collections must be different')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer in 1..100')
    path = Path(database).resolve()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None)
    try:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('unsupported search database version')
        columns = {row[1] for row in
                   connection.execute("PRAGMA table_info('search')")}
        if 'body' not in columns:
            raise ValueError('search index has no body column')
        documents_a, documents_b = _collection_counts(
            connection, collection_a, collection_b)
        connection.execute(
            "CREATE VIRTUAL TABLE temp.contrast_vocabulary "
            "USING fts5vocab('main','search','instance')")
        rows = connection.execute(_AGGREGATE, (
            collection_a, collection_b, collection_a, collection_b,
            collection_a, collection_b))
        best = []
        for term, tf_a, tf_b, df_a, df_b in rows:
            candidate = _Candidate(term, tf_a, df_a, tf_b, df_b,
                                   documents_a, documents_b)
            if len(best) < limit:
                heapq.heappush(best, candidate)
            elif best[0] < candidate:
                heapq.heapreplace(best, candidate)
        connection.execute('COMMIT')
    finally:
        connection.close()
    # ``__lt__`` ranks worst-first, so reverse gives best-first and, for
    # equal ratios, ascending Unicode term order.
    ranked = sorted(best, reverse=True)
    return {
        'schema_version': 1,
        'collections': {
            'a': {'name': collection_a, 'documents': documents_a},
            'b': {'name': collection_b, 'documents': documents_b},
        },
        'limit': limit,
        'terms': [candidate.as_report() for candidate in ranked],
    }


def write_report(report, target, inputs=()):
    """Atomically publish the report; failure preserves any previous report.

    The target must not be a symlink and must not overwrite any input,
    notably the source database.
    """
    target = Path(target).absolute()
    if target.is_symlink():
        raise ValueError('report target cannot be a symlink')
    for other in inputs:
        if target.resolve() == Path(other).resolve():
            raise ValueError(f'report would overwrite input {other}')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.collection-contrast-', suffix='.json',
                                dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=1)
            stream.write('\n')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
