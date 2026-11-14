"""Independent vocabulary oracle for the collection-contrast contract.

Nothing in this module imports or calls ``archive_index``.  Document bodies
and paragraph boundaries are rebuilt from raw bytes, and tokens are derived
with the same published ``unicode61 remove_diacritics 2`` rules the index
uses: compatibility-decompose, drop combining marks, case-fold and split on
everything that is not alphanumeric.  Terms and counts are then aggregated
in plain Python with ``fractions.Fraction``.  This oracle is intentionally
quadratic-free but unbounded in memory; it is practical only for the small
fixtures and corpora used by the tests.
"""
from __future__ import annotations

import json
import re
import unicodedata
from fractions import Fraction
from pathlib import Path

_TOKEN = re.compile(r'[^\W_]+', re.UNICODE)


def normalize(text):
    decomposed = unicodedata.normalize('NFKD', text)
    without_marks = ''.join(c for c in decomposed
                            if not unicodedata.combining(c))
    return without_marks.casefold()


def tokenize(text):
    return _TOKEN.findall(normalize(text))


def body_paragraphs(raw):
    """Blank-line separated paragraphs with LF-normalized text."""
    text = raw.decode('utf-8').replace('\r\n', '\n')
    start, block = None, []
    for number, line in enumerate(text.split('\n'), 1):
        if line.strip():
            if start is None:
                start = number
            block.append(line)
        elif block:
            yield '\n'.join(block)
            start, block = None, []
    if block:
        yield '\n'.join(block)


def body_words(raw):
    """Every body token occurrence of one document, paragraph by paragraph."""
    tokens = []
    for paragraph in body_paragraphs(raw):
        tokens.extend(tokenize(paragraph))
    return tokens


def load_corpus(directory, manifest_path=None):
    directory = Path(directory)
    manifest = json.loads(
        Path(manifest_path or directory / 'manifest.json').read_text(
            encoding='utf-8'))
    documents = []
    for entry in manifest['documents']:
        raw = (directory / entry['path']).read_bytes()
        documents.append({'id': entry['id'], 'collection': entry['collection'],
                          'title': entry['title'], 'raw': raw})
    return documents


def expected_terms(documents, collection_a, collection_b):
    """Exact TF/DF aggregates and reduced rational ratios, best first."""
    counts = {}
    documents_a = sum(1 for d in documents if d['collection'] == collection_a)
    documents_b = sum(1 for d in documents if d['collection'] == collection_b)
    if not documents_a:
        raise ValueError('missing collection a')
    if not documents_b:
        raise ValueError('missing collection b')

    def slot(term):
        return counts.setdefault(term, {'tf': [0, 0], 'df': [0, 0], 'docs': [set(), set()]})

    for document in documents:
        sides = {collection_a: 0, collection_b: 1}
        if document['collection'] not in sides:
            continue
        side = sides[document['collection']]
        for word in body_words(document['raw']):
            entry = slot(word)
            entry['tf'][side] += 1
            entry['docs'][side].add(document['id'])
    terms = []
    for term, entry in counts.items():
        df_a, df_b = len(entry['docs'][0]), len(entry['docs'][1])
        ratio = Fraction((df_a + 1) * (documents_b + 2),
                         (documents_a + 2) * (df_b + 1))
        terms.append({'term': term,
                      'a': {'tf': entry['tf'][0], 'df': df_a},
                      'b': {'tf': entry['tf'][1], 'df': df_b},
                      'ratio': {'numerator': ratio.numerator,
                                'denominator': ratio.denominator},
                      '_ratio': ratio})
    # Stable two-pass sort: Unicode term ascending first, then ratio
    # descending keeps that term order inside equal ratios.
    terms.sort(key=lambda t: t['term'])
    terms.sort(key=lambda t: t['_ratio'], reverse=True)
    for term in terms:
        del term['_ratio']
    return documents_a, documents_b, terms


def expected_report(directory, collection_a, collection_b, limit,
                    manifest_path=None):
    documents = load_corpus(directory, manifest_path)
    documents_a, documents_b, terms = expected_terms(
        documents, collection_a, collection_b)
    return {
        'schema_version': 1,
        'collections': {'a': {'name': collection_a, 'documents': documents_a},
                        'b': {'name': collection_b, 'documents': documents_b}},
        'limit': limit,
        'terms': terms[:limit],
    }


def ratio_key(term_report):
    """Descending exact ratio, then ascending Unicode term, as a sort key."""
    numerator = term_report['ratio']['numerator']
    denominator = term_report['ratio']['denominator']
    return (-Fraction(numerator, denominator), term_report['term'])
