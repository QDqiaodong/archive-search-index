import itertools
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive_index.migrate import (analyze, load_citations, load_library,
                                   migrate, write_report)
from archive_index.store import build

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'migration'


def brute(old, new):
    """Independently enumerate every maximum matching for tiny sequences."""
    from functools import lru_cache
    m, n = len(old), len(new)

    @lru_cache(maxsize=None)
    def lcs(i, j):
        if i == m or j == n:
            return 0
        best = max(lcs(i + 1, j), lcs(i, j + 1))
        if old[i] == new[j]:
            best = max(best, lcs(i + 1, j + 1) + 1)
        return best

    candidates = [set() for _ in range(m)]
    unmatched = [False] * m

    def walk(i, j, pairs):
        if lcs(i, j) == 0:
            used = set()
            for a, b in pairs:
                candidates[a].add(b)
                used.add(a)
            for a in range(m):
                if a not in used:
                    unmatched[a] = True
            return
        if old[i] == new[j] and lcs(i, j) == lcs(i + 1, j + 1) + 1:
            walk(i + 1, j + 1, pairs + ((i, j),))
        if lcs(i, j) == lcs(i + 1, j):
            walk(i + 1, j, pairs)
        if lcs(i, j) == lcs(i, j + 1):
            walk(i, j + 1, pairs)

    walk(0, 0, ())
    return lcs(0, 0), candidates, unmatched


class AnalysisTests(unittest.TestCase):
    def check(self, old, new):
        maximum, per_paragraph = analyze(list(old), list(new))
        expected_maximum, candidates, unmatched = brute(old, new)
        self.assertEqual(maximum, expected_maximum)
        self.assertEqual(len(per_paragraph), len(old))
        for i in range(len(old)):
            self.assertEqual(set(per_paragraph[i][0]), candidates[i])
            self.assertEqual(per_paragraph[i][1], unmatched[i])

    def test_exhaustive_binary_sequences(self):
        sequences = [tuple(p) for length in range(6)
                     for p in itertools.product('ab', repeat=length)]
        for old in sequences:
            for new in sequences:
                with self.subTest(old=old, new=new):
                    self.check(old, new)

    def test_random_ternary_sequences(self):
        rng = random.Random(20261114)
        for _ in range(300):
            old = tuple(rng.choice('abc') for _ in range(rng.randrange(8)))
            new = tuple(rng.choice('abc') for _ in range(rng.randrange(8)))
            with self.subTest(old=old, new=new):
                self.check(old, new)

    def test_known_outcomes(self):
        # Insertion shifts later paragraphs; deletion invalidates; repeats
        # with fewer copies are ambiguous and possibly unmatched.
        self.assertEqual(analyze(['a', 'b'], ['a', 'x', 'b']),
                         (2, [((0,), False), ((2,), False)]))
        self.assertEqual(analyze(['a', 'b'], ['a']), (1, [((0,), False), ((), True)]))
        self.assertEqual(analyze(['r', 'r'], ['r']), (1, [((0,), True), ((0,), True)]))
        self.assertEqual(analyze(['r'], ['r', 'r']), (1, [((0, 1), False)]))

    def test_alignment_size_guard(self):
        with patch('archive_index.migrate.MAX_ALIGNMENT_CELLS', 8):
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                analyze(['a', 'b'], ['a', 'b'])


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.citations_file = self.root / 'citations.json'
        self.report = self.root / 'report.json'

    def make_library(self, name, documents):
        self.counter = getattr(self, 'counter', 0) + 1
        directory = self.root / f'{name}{self.counter}'
        directory.mkdir()
        manifest = {'schema_version': 1, 'documents': []}
        for doc_id, text in sorted(documents.items()):
            path = directory / f'{doc_id.lower()}.txt'
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_text(text, encoding='utf-8')
            manifest['documents'].append(dict(id=doc_id, title=f'Title {doc_id}',
                                              collection='test', path=path.name))
        (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        database = self.root / f'{name}{self.counter}.sqlite'
        build(directory / 'manifest.json', database)
        return database

    def cite(self, database, doc_id, paragraph):
        document = load_library(database)[doc_id]
        start, end, _ = document['paragraphs'][paragraph - 1]
        return {'document_id': doc_id, 'sha256': document['sha256'],
                'paragraph': paragraph, 'start_line': start, 'end_line': end}

    def run_migrate(self, citations):
        self.citations_file.write_text(
            json.dumps({'schema_version': 1, 'citations': citations}), encoding='utf-8')
        report = migrate(self.old_db, self.new_db, self.citations_file)
        write_report(report, self.report,
                     inputs=(self.old_db, self.new_db, self.citations_file))
        return report

    def build_pair(self, old_docs, new_docs):
        self.old_db = self.make_library('old', old_docs)
        self.new_db = self.make_library('new', new_docs)

    def test_inserted_paragraph_shifts_lines(self):
        self.build_pair({'D1': 'alpha\n\nbeta\n\ngamma\n'},
                        {'D1': 'alpha\n\ninserted\n\nbeta\n\ngamma\n'})
        report = self.run_migrate([self.cite(self.old_db, 'D1', 2),
                                   self.cite(self.old_db, 'D1', 3)])
        first, second = report['citations']
        self.assertEqual(first['status'], 'migrated')
        self.assertEqual((first['new_paragraph'], first['new_start_line'],
                          first['new_end_line']), (3, 5, 5))
        self.assertEqual((second['new_paragraph'], second['new_start_line']), (4, 7))
        alignment = report['alignments'][0]
        self.assertEqual(alignment['max_matches'], 3)
        self.assertEqual([p['text'] for p in alignment['new']['paragraphs']],
                         ['alpha', 'inserted', 'beta', 'gamma'])

    def test_deleted_paragraph_is_unmatched(self):
        self.build_pair({'D1': 'alpha\n\nbeta\n\ngamma\n'}, {'D1': 'alpha\n\ngamma\n'})
        report = self.run_migrate([self.cite(self.old_db, 'D1', 2),
                                   self.cite(self.old_db, 'D1', 3)])
        gone, kept = report['citations']
        self.assertEqual(gone['status'], 'unmatched')
        self.assertEqual(gone['candidates'], [])
        self.assertIs(gone['possibly_unmatched'], True)
        self.assertEqual((kept['status'], kept['new_paragraph']), ('migrated', 2))

    def test_duplicate_paragraphs_are_ambiguous(self):
        self.build_pair({'D1': 'repeat\n\nrepeat\n\nunique\n'},
                        {'D1': 'repeat\n\nunique\n'})
        report = self.run_migrate([self.cite(self.old_db, 'D1', p) for p in (1, 2, 3)])
        first, second, third = report['citations']
        for outcome in (first, second):
            self.assertEqual(outcome['status'], 'ambiguous')
            self.assertEqual([c['paragraph'] for c in outcome['candidates']], [1])
            self.assertIs(outcome['possibly_unmatched'], True)
        self.assertEqual((third['status'], third['new_paragraph']), ('migrated', 2))

    def test_extra_duplicate_copy_is_ambiguous_without_unmatched(self):
        self.build_pair({'D1': 'repeat\n\nunique\n'},
                        {'D1': 'repeat\n\nrepeat\n\nunique\n'})
        report = self.run_migrate([self.cite(self.old_db, 'D1', 1),
                                   self.cite(self.old_db, 'D1', 2)])
        first, second = report['citations']
        self.assertEqual(first['status'], 'ambiguous')
        self.assertEqual([c['paragraph'] for c in first['candidates']], [1, 2])
        self.assertIs(first['possibly_unmatched'], False)
        self.assertEqual((second['status'], second['new_paragraph']), ('migrated', 3))

    def test_cross_document_identical_text_is_not_aligned(self):
        text = 'shared exact text\n\nsecond note\n'
        self.build_pair({'D1': text, 'D2': 'unrelated\n'},
                        {'D2': 'shared exact text\n\nsecond note\n\nunrelated\n'})
        report = self.run_migrate([self.cite(self.old_db, 'D1', 1),
                                   self.cite(self.old_db, 'D2', 1)])
        vanished, moved = report['citations']
        self.assertEqual(vanished['status'], 'vanished')
        self.assertEqual(vanished['candidates'], [])
        self.assertIsNone(vanished['possibly_unmatched'])
        self.assertEqual((moved['status'], moved['new_paragraph']), ('migrated', 3))
        self.assertIsNone(report['alignments'][0]['new'])
        self.assertEqual({d['id'] for d in report['libraries']['new']['documents']},
                         {'D2'})

    def test_crlf_documents_align_on_lf_paragraphs(self):
        self.build_pair({'D1': b'alpha\r\nbeta\r\n\r\ngamma\r\n'},
                        {'D1': b'inserted\r\n\r\nalpha\r\nbeta\r\n\r\ngamma\r\n'})
        report = self.run_migrate([self.cite(self.old_db, 'D1', 1),
                                   self.cite(self.old_db, 'D1', 2)])
        first, second = report['citations']
        self.assertEqual((first['status'], first['new_paragraph']), ('migrated', 2))
        self.assertEqual((first['new_start_line'], first['new_end_line']), (3, 4))
        self.assertEqual((second['status'], second['new_paragraph']), ('migrated', 3))

    def test_report_is_deterministic_and_self_contained(self):
        self.build_pair({'D1': 'alpha\n\nbeta\n', 'D2': 'repeat\n\nrepeat\n'},
                        {'D1': 'beta\n\nalpha\n', 'D2': 'repeat\n'})
        citations = [self.cite(self.old_db, 'D1', 1), self.cite(self.old_db, 'D2', 1)]
        first = self.run_migrate(citations)
        first_bytes = self.report.read_bytes()
        second = self.run_migrate(citations)
        self.assertEqual(self.report.read_bytes(), first_bytes)
        self.assertEqual(first, second)
        self.assertNotIn(str(self.root), self.report.read_text(encoding='utf-8'))
        # Remove every input; the report alone still supports the conclusions.
        for path in (self.old_db, self.new_db, self.citations_file):
            path.unlink()
        report = json.loads(first_bytes)
        alignments = {a['document_id']: a for a in report['alignments']}
        for outcome in report['citations']:
            entry = alignments[outcome['document_id']]
            if entry['new'] is None:
                self.assertEqual(outcome['status'], 'vanished')
                continue
            old_texts = [p['text'] for p in entry['old']['paragraphs']]
            new_texts = [p['text'] for p in entry['new']['paragraphs']]
            maximum, per_paragraph = analyze(old_texts, new_texts)
            self.assertEqual(entry['max_matches'], maximum)
            candidates, possibly_unmatched = per_paragraph[outcome['paragraph'] - 1]
            self.assertEqual([c['paragraph'] for c in outcome['candidates']],
                             [j + 1 for j in candidates])
            self.assertEqual(outcome['possibly_unmatched'], possibly_unmatched)
            for candidate in outcome['candidates']:
                paragraph = entry['new']['paragraphs'][candidate['paragraph'] - 1]
                self.assertEqual((candidate['start_line'], candidate['end_line']),
                                 (paragraph['start_line'], paragraph['end_line']))

    def test_bad_citations_fail_without_touching_report(self):
        self.build_pair({'D1': 'alpha\n\nbeta\n'}, {'D1': 'alpha\n\nbeta\n'})
        good = self.cite(self.old_db, 'D1', 1)
        self.run_migrate([good])
        before = self.report.read_bytes()
        bad = [dict(good, sha256='0' * 64), dict(good, start_line=2),
               dict(good, paragraph=99), dict(good, document_id='D9'),
               dict(good, start_line=2, end_line=1)]
        for citation in bad:
            with self.subTest(citation=citation):
                self.citations_file.write_text(
                    json.dumps({'schema_version': 1, 'citations': [citation]}))
                with self.assertRaises(ValueError):
                    migrate(self.old_db, self.new_db, self.citations_file)
                self.assertEqual(self.report.read_bytes(), before)
        malformed = ['{"schema_version":1,"schema_version":1,"citations":[]}',
                     json.dumps({'schema_version': 1, 'citations': []}),
                     json.dumps({'schema_version': True, 'citations': [good]}),
                     json.dumps({'schema_version': 1, 'citations': [dict(good, paragraph=True)]}),
                     json.dumps({'schema_version': 1, 'citations': [dict(good, extra=1)]}),
                     json.dumps({'schema_version': 1, 'citations': [
                         {k: v for k, v in good.items() if k != 'sha256'}]}),
                     json.dumps({'citations': [good]})]
        for text in malformed:
            with self.subTest(text=text):
                self.citations_file.write_text(text)
                with self.assertRaises(ValueError):
                    migrate(self.old_db, self.new_db, self.citations_file)
                self.assertEqual(self.report.read_bytes(), before)

    def test_bad_library_fails_whole_run(self):
        self.build_pair({'D1': 'alpha\n\nbeta\n'}, {'D1': 'alpha\n\nbeta\n'})
        citation = self.cite(self.old_db, 'D1', 1)
        self.run_migrate([citation])
        before = self.report.read_bytes()
        self.citations_file.write_text(
            json.dumps({'schema_version': 1, 'citations': [citation]}))

        def tamper(database, statement):
            def apply():
                with sqlite3.connect(getattr(self, database)) as connection:
                    connection.execute(statement)
            return apply

        cases = [('paragraphs', tamper('new_db', 'UPDATE chunks SET start_line=start_line+1')),
                 ('sha256', tamper('old_db', "UPDATE documents SET raw='tampered' WHERE id='D1'")),
                 ('version', tamper('old_db', 'PRAGMA user_version=2')),
                 (None, lambda: self.new_db.unlink())]
        for pattern, corrupt in cases:
            with self.subTest(pattern or 'missing database'):
                self.build_pair({'D1': 'alpha\n\nbeta\n'}, {'D1': 'alpha\n\nbeta\n'})
                corrupt()
                if pattern is None:
                    with self.assertRaises(sqlite3.Error):
                        migrate(self.old_db, self.new_db, self.citations_file)
                else:
                    with self.assertRaisesRegex(ValueError, pattern):
                        migrate(self.old_db, self.new_db, self.citations_file)
                self.assertEqual(self.report.read_bytes(), before)

    def test_report_never_overwrites_inputs(self):
        self.build_pair({'D1': 'alpha\n'}, {'D1': 'alpha\n'})
        citation = self.cite(self.old_db, 'D1', 1)
        self.citations_file.write_text(
            json.dumps({'schema_version': 1, 'citations': [citation]}))
        report = migrate(self.old_db, self.new_db, self.citations_file)
        for target in (self.old_db, self.new_db, self.citations_file):
            with self.subTest(target=target):
                contents = target.read_bytes()
                with self.assertRaisesRegex(ValueError, 'overwrite'):
                    write_report(report, target,
                                 inputs=(self.old_db, self.new_db, self.citations_file))
                self.assertEqual(target.read_bytes(), contents)
        link = self.root / 'link.json'
        link.symlink_to(self.root / 'elsewhere.json')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            write_report(report, link)

    def test_write_failure_preserves_old_report_and_cleans_up(self):
        self.build_pair({'D1': 'alpha\n'}, {'D1': 'alpha\n'})
        citation = self.cite(self.old_db, 'D1', 1)
        self.citations_file.write_text(
            json.dumps({'schema_version': 1, 'citations': [citation]}))
        report = migrate(self.old_db, self.new_db, self.citations_file)
        write_report(report, self.report)
        before = self.report.read_bytes()
        with patch('archive_index.migrate.Path.replace', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                write_report(report, self.report)
        self.assertEqual(self.report.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.citation-migration-*')))

    def test_fixture_example_end_to_end_via_cli(self):
        self.old_db = self.root / 'old.sqlite'
        self.new_db = self.root / 'new.sqlite'
        build(FIXTURES / 'old' / 'manifest.json', self.old_db)
        build(FIXTURES / 'new' / 'manifest.json', self.new_db)

        def cli(*args):
            return subprocess.run([sys.executable, '-m', 'archive_index.cli', *map(str, args)],
                                  capture_output=True, text=True, cwd=self.root)

        citations = FIXTURES / 'citations.json'
        result = cli('migrate', '--old', self.old_db, '--new', self.new_db,
                     '--citations', citations, '--report', self.report)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary, {'report': str(self.report), 'citations': 5,
                                   'migrated': 3, 'ambiguous': 1,
                                   'unmatched': 0, 'vanished': 1})
        again = cli('migrate', '--old', self.old_db, '--new', self.new_db,
                    '--citations', citations, '--report', self.root / 'second.json')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.root / 'second.json').read_bytes(), self.report.read_bytes())
        report = json.loads(self.report.read_bytes())
        outcomes = {(c['document_id'], c['paragraph']): c for c in report['citations']}
        self.assertEqual(outcomes['M1', 2]['status'], 'migrated')
        self.assertEqual((outcomes['M1', 2]['new_paragraph'],
                          outcomes['M1', 2]['new_start_line']), (3, 5))
        self.assertEqual(outcomes['M2', 1]['status'], 'vanished')
        self.assertEqual(outcomes['M3', 1]['status'], 'ambiguous')
        self.assertEqual([c['paragraph'] for c in outcomes['M3', 1]['candidates']], [1])
        self.assertIs(outcomes['M3', 1]['possibly_unmatched'], True)
        self.assertEqual((outcomes['M3', 3]['status'], outcomes['M3', 3]['new_paragraph']),
                         ('migrated', 2))
        self.assertEqual((outcomes['M4', 1]['status'], outcomes['M4', 1]['new_paragraph']),
                         ('migrated', 1))
        alignments = {a['document_id']: a for a in report['alignments']}
        self.assertIsNone(alignments['M2']['new'])
        self.assertEqual(alignments['M1']['max_matches'], 3)
        self.assertEqual(alignments['M3']['max_matches'], 2)
        self.assertEqual([d['id'] for d in report['libraries']['new']['documents']],
                         ['M1', 'M3', 'M4', 'M5'])
        # Old entry points still work against the same databases.
        hits = cli('search', '--database', self.new_db, '--query', '"Unique tail"')
        self.assertEqual(hits.returncode, 0, hits.stderr)
        self.assertEqual(json.loads(hits.stdout)['matches'][0]['document_id'], 'M3')
        failure = cli('migrate', '--old', self.old_db, '--new', self.new_db,
                      '--citations', citations, '--report', self.old_db)
        self.assertEqual(failure.returncode, 2)
        self.assertEqual(cli('migrate', '--old', self.old_db, '--new', self.new_db,
                             '--citations', self.report, '--report', self.root / 'x.json'
                             ).returncode, 2)


if __name__ == '__main__':
    unittest.main()
