"""Collection vocabulary contrast: exact counts, rational ranking, bounded memory.

The expected numbers are derived independently from raw fixture bytes by
``tests/vocabulary_oracle.py`` (it never imports ``archive_index``); the
production report is then compared term for term.
"""
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from archive_index.contrast import _Candidate, compare, write_report
from archive_index.store import build
from vocabulary_oracle import expected_report, ratio_key

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / 'fixtures' / 'contrast'


class ContrastFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / 'archive.sqlite'
        build(FIXTURES / 'manifest.json', self.db)

    def test_fixture_report_is_exactly_hand_checkable(self):
        report = compare(self.db, 'alpha', 'beta', 100)
        expected = json.loads((FIXTURES / 'expected.json').read_text(encoding='utf-8'))
        self.assertEqual(report, expected)
        self.assertEqual(report['collections'], {
            'a': {'name': 'alpha', 'documents': 3},
            'b': {'name': 'beta', 'documents': 2}})

    def test_counts_match_raw_byte_oracle(self):
        report = compare(self.db, 'alpha', 'beta', 100)
        self.assertEqual(report['terms'],
                         expected_report(FIXTURES, 'alpha', 'beta', 100)['terms'])

    def test_cross_paragraph_and_duplicate_documents_add_tf(self):
        terms = {t['term']: t for t in compare(self.db, 'alpha', 'beta', 100)['terms']}
        # harbor: two occurrences in paragraph 1 plus one in paragraph 2 of
        # C1, and the same again in the byte-identical C2 -> TF 6, DF 2.
        self.assertEqual(terms['harbor']['a'], {'tf': 6, 'df': 2})
        # Two separate paragraphs of one document still give DF 1.
        self.assertEqual(terms['archive']['a'], {'tf': 2, 'df': 1})
        self.assertEqual(terms['harbor']['b'], {'tf': 1, 'df': 1})

    def test_title_terms_and_title_occurrences_are_not_counted(self):
        report = compare(self.db, 'alpha', 'beta', 100)
        listed = {t['term'] for t in report['terms']}
        # zephyr/kestrel/log live only in titles.
        self.assertFalse({'zephyr', 'kestrel', 'log'} & listed)
        # Harbor sits in C1/C2/C4 titles; body TF/DF must ignore those hits.
        harbor = next(t for t in report['terms'] if t['term'] == 'harbor')
        self.assertEqual((harbor['a']['df'], harbor['b']['df']), (2, 1))

    def test_case_and_diacritics_fold_to_one_body_term(self):
        report = compare(self.db, 'alpha', 'beta', 100)
        # Café (alpha, three documents) + CAFÉ (beta, one document).
        cafe = [t for t in report['terms'] if t['term'] == 'cafe']
        self.assertEqual(len(cafe), 1)
        self.assertEqual(cafe[0]['a'], {'tf': 3, 'df': 3})
        self.assertEqual(cafe[0]['b'], {'tf': 1, 'df': 1})
        self.assertNotIn('café', {t['term'] for t in report['terms']})

    def test_chinese_stays_one_tokenizer_token(self):
        report = compare(self.db, 'alpha', 'beta', 100)
        terms = {t['term'] for t in report['terms']}
        self.assertIn('冬季维护', terms)
        for term in terms:
            self.assertFalse(set(term) <= set('冬季维护') and term != '冬季维护')

    def test_equal_ratios_break_ties_by_unicode_term(self):
        terms = [t['term'] for t in compare(self.db, 'alpha', 'beta', 100)['terms']]
        # archive and cafe both reduce to 8/5; gate and harbor both to 6/5.
        self.assertLess(terms.index('archive'), terms.index('cafe'))
        self.assertLess(terms.index('gate'), terms.index('harbor'))
        report = compare(self.db, 'alpha', 'beta', 100)
        ratios = {(t['term']): (t['ratio']['numerator'], t['ratio']['denominator'])
                  for t in report['terms']}
        self.assertEqual(ratios['archive'], ratios['cafe'])
        self.assertEqual(ratios['gate'], ratios['harbor'])

    def test_ratio_is_exact_and_reduced(self):
        report = compare(self.db, 'alpha', 'beta', 100)
        ratios = {t['term']: t['ratio'] for t in report['terms']}
        # ((2+1)/(3+2))/((0+1)/(2+2)) = 12/5
        self.assertEqual(ratios['ledger'], {'numerator': 12, 'denominator': 5})
        # ((0+1)/(3+2))/((2+1)/(2+2)) = 4/15
        self.assertEqual(ratios['tide'], {'numerator': 4, 'denominator': 15})
        for t in report['terms']:
            n, d = t['ratio']['numerator'], t['ratio']['denominator']
            self.assertEqual(math.gcd(n, d), 1)
            self.assertGreater(n, 0)
            self.assertGreater(d, 0)

    def test_limit_takes_ranked_prefix(self):
        full = compare(self.db, 'alpha', 'beta', 100)
        for limit in (1, 3, len(full['terms'])):
            cut = compare(self.db, 'alpha', 'beta', limit)
            self.assertEqual(cut['terms'], full['terms'][:limit])
            self.assertEqual(cut['limit'], limit)
        self.assertEqual(compare(self.db, 'alpha', 'beta', 1)['terms'][0]['term'],
                         'ledger')

    def test_direction_is_asymmetric(self):
        forward = compare(self.db, 'alpha', 'beta', 100)
        backward = compare(self.db, 'beta', 'alpha', 100)
        self.assertEqual(forward['collections']['a']['name'], 'alpha')
        self.assertEqual(backward['collections']['a']['name'], 'beta')
        self.assertEqual(backward['terms'][0]['term'], 'tide')
        by_term = {t['term']: t for t in backward['terms']}
        self.assertEqual(by_term['ledger']['a'], {'tf': 0, 'df': 0})
        self.assertEqual(by_term['ledger']['b'], {'tf': 2, 'df': 2})
        self.assertEqual((by_term['ledger']['ratio']['numerator'],
                          by_term['ledger']['ratio']['denominator']), (5, 12))

    def test_ranked_prefix_beats_a_bounded_heap_drift(self):
        # Every distinct ratio order must be reproduced by a bounded heap;
        # compare it against a full in-memory sort of the same aggregates.
        report = compare(self.db, 'alpha', 'beta', 100)
        ordered = sorted(report['terms'], key=lambda t: ratio_key(t))
        self.assertEqual(report['terms'], ordered)

    def test_unknown_equal_or_invalid_arguments_are_rejected(self):
        for arguments in [('missing', 'beta', 10), ('alpha', 'missing', 10),
                          ('alpha', 'alpha', 10), ('alpha', 'beta', 0),
                          ('alpha', 'beta', 101), ('alpha', 'beta', True)]:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    compare(self.db, *arguments)
        for value in ['', ' alpha', 'a\nb']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    compare(self.db, value, 'beta', 10)

    def test_report_deterministic_and_read_only(self):
        before = self.db.read_bytes()
        first = compare(self.db, 'alpha', 'beta', 100)
        second = compare(self.db, 'alpha', 'beta', 100)
        self.assertEqual(first, second)
        self.assertEqual(self.db.read_bytes(), before)
        report = self.root / 'report.json'
        write_report(first, report, inputs=(self.db,))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertNotIn(str(self.root), report.read_text(encoding='utf-8'))

    def test_report_refuses_to_overwrite_the_database(self):
        report = compare(self.db, 'alpha', 'beta', 10)
        before = self.db.read_bytes()
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            write_report(report, self.db, inputs=(self.db,))
        self.assertEqual(self.db.read_bytes(), before)
        link = self.root / 'link.json'
        link.symlink_to(self.root / 'elsewhere.json')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            write_report(report, link)

    def test_write_failure_preserves_old_report(self):
        report = compare(self.db, 'alpha', 'beta', 10)
        target = self.root / 'report.json'
        write_report(report, target)
        before = target.read_bytes()
        with patch('archive_index.contrast.Path.replace',
                   side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                write_report(report, target)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.collection-contrast-*')))

    def test_missing_or_bad_database_is_an_error(self):
        with self.assertRaises(sqlite3.Error):
            compare(self.root / 'absent.sqlite', 'alpha', 'beta', 10)
        self.assertFalse((self.root / 'absent.sqlite').exists())
        with sqlite3.connect(self.db) as connection:
            connection.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(ValueError, 'version'):
            compare(self.db, 'alpha', 'beta', 10)

    def cli(self, *args):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
        return subprocess.run(
            [sys.executable, '-m', 'archive_index.cli', *map(str, args)],
            capture_output=True, text=True, cwd=self.root, env=environment)

    def test_cli_end_to_end(self):
        target = self.root / 'cli-report.json'
        expected = self.root / 'library-report.json'
        write_report(compare(self.db, 'alpha', 'beta', 100), expected,
                     inputs=(self.db,))
        database_before = self.db.read_bytes()
        cli = self.cli

        result = cli('contrast', '--database', self.db,
                     '--collection-a', 'alpha', '--collection-b', 'beta',
                     '--limit', '100', '--report', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'report': str(target), 'documents': {'a': 3, 'b': 2},
                          'terms': 7})
        self.assertEqual(target.read_bytes(), expected.read_bytes())
        again = cli('contrast', '--database', self.db,
                    '--collection-a', 'alpha', '--collection-b', 'beta',
                    '--limit', '100', '--report', self.root / 'again.json')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.root / 'again.json').read_bytes(),
                         target.read_bytes())
        # A failed run must leave a previous report byte-for-byte intact.
        failed_target = self.root / 'existing.json'
        failed_target.write_bytes(b'previous report\n')
        failed = cli('contrast', '--database', self.db, '--collection-a', 'alpha',
                     '--collection-b', 'alpha', '--report', failed_target)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed_target.read_bytes(), b'previous report\n')
        for bad in [
            ('contrast', '--database', self.db, '--collection-a', 'alpha',
             '--collection-b', 'alpha', '--report', self.root / 'x.json'),
            ('contrast', '--database', self.db, '--collection-a', 'alpha',
             '--collection-b', 'beta', '--limit', '0', '--report',
             self.root / 'x.json'),
            ('contrast', '--database', self.db, '--collection-a', 'alpha',
             '--collection-b', 'beta', '--report', self.db),
            ('contrast', '--database', self.root / 'absent.sqlite',
             '--collection-a', 'alpha', '--collection-b', 'beta',
             '--report', self.root / 'x.json'),
            ('contrast', '--database', self.db, '--collection-a', 'alpha',
             '--collection-b', 'beta', '--report', self.db,
             '--limit', 'not-a-number'),
        ]:
            with self.subTest(bad=bad):
                failed = cli(*bad)
                self.assertEqual(failed.returncode, 2)
                self.assertTrue(failed.stderr.strip())
                self.assertFalse((self.root / 'x.json').exists())
        self.assertEqual(self.db.read_bytes(), database_before)


class ContrastPropertyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_database(self, documents, name='corpus'):
        directory = self.root / name
        directory.mkdir()
        manifest = {'schema_version': 1, 'documents': []}
        for identifier, collection, title, body in documents:
            path = directory / (identifier + '.txt')
            path.write_text(body, encoding='utf-8')
            manifest['documents'].append(dict(id=identifier, title=title,
                                              collection=collection,
                                              path=path.name))
        (directory / 'manifest.json').write_text(json.dumps(manifest),
                                                 encoding='utf-8')
        database = self.root / 'archive.sqlite'
        build(directory / 'manifest.json', database)
        return database, directory

    def test_random_corpora_match_the_raw_byte_oracle(self):
        rng = random.Random(20261114)
        alphabet = ['alpha', 'Beta', 'café', 'CAFÉ', '归档', 'tide', 'TIDE',
                    'gate', 'harbor', '冬季维护']
        for round_number in range(8):
            documents = []
            for index in range(rng.randrange(2, 7)):
                paragraphs = []
                for _ in range(rng.randrange(1, 4)):
                    words = [rng.choice(alphabet)
                             for _ in range(rng.randrange(0, 6))]
                    paragraph = ' '.join(words)
                    paragraphs.append(paragraph if paragraph else 'padding')
                documents.append((f'D{index:02d}',
                                  rng.choice(['left', 'right']),
                                  ' '.join(rng.sample(alphabet, 2)),
                                  '\n\n'.join(paragraphs) + '\n'))
            if not {'left', 'right'} <= {d[1] for d in documents}:
                continue
            with self.subTest(round=round_number):
                database, directory = self.make_database(documents,
                                                        f'corpus{round_number}')
                report = compare(database, 'left', 'right', 100)
                expected = expected_report(directory, 'left', 'right', 100,
                                           manifest_path=directory / 'manifest.json')
                self.assertEqual(report, expected)
                for limit in (1, 2, 5):
                    cut = compare(database, 'left', 'right', limit)
                    self.assertEqual(cut['terms'], report['terms'][:limit])

    def test_empty_body_vocabulary_yields_no_terms(self):
        directory = self.root / 'blank'
        directory.mkdir()
        for name in ('a.txt', 'b.txt'):
            (directory / name).write_text('...\n\n!!!\n', encoding='utf-8')
        (directory / 'manifest.json').write_text(json.dumps({
            'schema_version': 1,
            'documents': [
                dict(id='A', title='...', collection='x', path='a.txt'),
                dict(id='B', title='---', collection='y', path='b.txt')]}),
            encoding='utf-8')
        database = self.root / 'blank.sqlite'
        build(directory / 'manifest.json', database)
        report = compare(database, 'x', 'y', 100)
        self.assertEqual(report['terms'], [])
        self.assertEqual(report['collections'],
                         {'a': {'name': 'x', 'documents': 1},
                          'b': {'name': 'y', 'documents': 1}})

    def test_exact_ratio_order_is_independent_of_representation(self):        # Two mathematically equal ratios reach the comparator as different
        # unreduced integer pairs.  Cross-multiplication must call them equal,
        # so term order decides instead of a truncated decimal.
        candidates = [
            _Candidate('beta', 1, 1, 1, 1, 2, 2),    # (2/4)/(2/4) = 1
            _Candidate('alpha', 2, 2, 2, 2, 4, 4),   # (3/6)/(3/6) = 1
            _Candidate('gamma', 0, 0, 0, 0, 1, 1),   # (1/3)/(1/3) = 1
        ]
        self.assertEqual(sorted(candidates, reverse=True),
                         [next(c for c in candidates if c.term == 'alpha'),
                          next(c for c in candidates if c.term == 'beta'),
                          next(c for c in candidates if c.term == 'gamma')])
        for candidate in candidates:
            self.assertEqual((candidate.numerator, candidate.denominator), (1, 1))
        # A bounded heap must drop the worst candidate only, and keep every
        # candidate whose ratio is strictly larger regardless of term order.
        better = _Candidate('zeta', 0, 3, 0, 0, 2, 2)
        self.assertFalse(better < candidates[0])
        self.assertTrue(candidates[0] < better)


class ContrastMemoryTests(unittest.TestCase):
    def test_peak_does_not_track_token_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            peaks = {}
            for total in (20000, 200000):
                directory = root / str(total)
                directory.mkdir()
                words = [f'w{i % 5000}' for i in range(total)]
                (directory / 'a.txt').write_text(' '.join(words[:total // 2]))
                (directory / 'b.txt').write_text(' '.join(words[total // 2:]))
                (directory / 'manifest.json').write_text(json.dumps({
                    'schema_version': 1,
                    'documents': [
                        dict(id='A', title='T', collection='left', path='a.txt'),
                        dict(id='B', title='T', collection='right', path='b.txt')]}))
                database = directory / 'archive.sqlite'
                build(directory / 'manifest.json', database)
                tracemalloc.start()
                tracemalloc.reset_peak()
                report = compare(database, 'left', 'right', 100)
                peaks[total] = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                self.assertEqual(len(report['terms']), 100)
            self.assertLess(peaks[200000], 32 * 1024 * 1024)
            self.assertLess(peaks[200000], peaks[20000] + 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
