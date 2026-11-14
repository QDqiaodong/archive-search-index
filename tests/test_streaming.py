"""Contract tests for bounded-memory streaming ingestion.

Covers table-level parity with the frozen pre-streaming builder, injectable
corpus limits, cumulative and per-document bounds, mid-stream failures that
must preserve the previous snapshot, single-read capture, a real subprocess
CLI build/search, and a tracemalloc subprocess whose build peak stays under
32 MiB and does not grow with corpus size.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive_index import ingest  # noqa: E402
from archive_index.ingest import DocumentStream, load  # noqa: E402
from archive_index.query import search  # noqa: E402
from archive_index.store import build  # noqa: E402
import legacy_build  # noqa: E402

ONE_MIB = 1024 * 1024
THIRTY_TWO_MIB = 32 * ONE_MIB


def write_corpus(root, documents, manifest_name='manifest.json'):
    """documents: {name: bytes|str}; returns list of manifest entries.

    Document ids derive from the file name, so writing a corpus twice keeps
    stable ids instead of colliding on call-order indices.
    """
    root = Path(root)
    entries = []
    for index, (name, content) in enumerate(sorted(documents.items())):
        path = root / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding='utf-8')
        entries.append({'id': f'D{index + 1:02d}', 'title': f'Title {index + 1:02d}',
                        'collection': 'engineering' if index % 2 else 'library',
                        'path': name})
    manifest = {'schema_version': 1, 'documents': entries}
    (root / manifest_name).write_text(json.dumps(manifest), encoding='utf-8')
    return entries


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def corpus(self):
        # Deliberately varied: CRLF, leading/trailing blanks, unicode, a
        # repeated paragraph and byte-identical bodies under different ids.
        return {
            'a.txt': 'Harbor restoration notes.\r\n\r\nLime mortar winter work.\r\n',
            'b.txt': '\n\nCopper rivets tide survey.\n\t \nArchive catalogue desk.\n\n',
            'c.txt': 'Café 原始档案\n\nRepeated note.\n\nRepeated note.\n',
            'd.txt': 'Copper rivets tide survey.\n\t \nArchive catalogue desk.\n\n',
            'e.txt': 'Final document without trailing newline\n\nSecond block here',
        }

    def dump(self, database):
        with sqlite3.connect(database) as connection:
            return {
                'metadata': connection.execute(
                    'SELECT manifest_sha256, manifest FROM metadata').fetchall(),
                'documents': connection.execute(
                    'SELECT id,title,collection,source_path,sha256,raw '
                    'FROM documents ORDER BY id').fetchall(),
                'chunks': connection.execute(
                    'SELECT id,document_id,paragraph,start_line,end_line,body '
                    'FROM chunks ORDER BY id').fetchall(),
                'search': connection.execute(
                    'SELECT rowid,title,body FROM search ORDER BY rowid').fetchall(),
                'user_version': connection.execute(
                    'PRAGMA user_version').fetchone()[0],
            }

    def test_every_table_matches_legacy_builder(self):
        write_corpus(self.root, self.corpus())
        manifest = self.root / 'manifest.json'
        legacy_db, new_db = self.root / 'legacy.sqlite', self.root / 'new.sqlite'
        legacy_summary = legacy_build.legacy_build(manifest, legacy_db)
        summary = build(manifest, new_db)
        self.assertEqual(summary, legacy_summary)
        old, new = self.dump(legacy_db), self.dump(new_db)
        self.assertEqual(old['user_version'], new['user_version'], 1)
        self.assertEqual(old['metadata'], new['metadata'])
        self.assertEqual(old['documents'], new['documents'])
        self.assertEqual(old['chunks'], new['chunks'])
        # The FTS index carries the same rowid-keyed title/body rows.
        self.assertEqual(old['search'], new['search'])
        queries = ('restoration', 'copper', '"winter work"', 'cafe', '原始档案',
                   'repeated', '(harbor OR copper) AND tide', 'absentword',
                   'title')
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(search(new_db, query, limit=100),
                                 search(legacy_db, query, limit=100))

    def test_rowids_are_stable_under_manifest_reordering(self):
        entries = write_corpus(self.root, self.corpus())
        first = self.root / 'first.sqlite'
        build(self.root / 'manifest.json', first)
        shuffled = list(reversed(entries))
        (self.root / 'manifest-rev.json').write_text(
            json.dumps({'schema_version': 1, 'documents': shuffled}),
            encoding='utf-8')
        second = self.root / 'second.sqlite'
        build(self.root / 'manifest-rev.json', second)
        self.assertEqual(self.dump(first)['chunks'], self.dump(second)['chunks'])
        self.assertEqual(self.dump(first)['search'], self.dump(second)['search'])


class StreamingContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / 'archive.sqlite'

    def test_limit_constants(self):
        self.assertEqual(ingest.MAX_MANIFEST, ONE_MIB)
        self.assertEqual(ingest.MAX_DOCUMENT, 2 * ONE_MIB)
        self.assertEqual(ingest.MAX_CORPUS, 1024 * ONE_MIB)
        self.assertEqual(ingest.MAX_DOCUMENTS, 1000)

    def test_load_returns_manifest_bytes_and_single_pass_stream(self):
        write_corpus(self.root, {'a.txt': 'alpha\n\nbeta\n',
                                 'b.txt': 'gamma\n'})
        raw, docs = load(self.root / 'manifest.json')
        self.assertIsInstance(docs, DocumentStream)
        self.assertEqual(raw, (self.root / 'manifest.json').read_bytes())
        # Bodies are captured lazily: consuming two documents opens two files.
        # pathlib.Path.open resolves io.open directly, so Path.open is the
        # interception point rather than builtins.open.
        opened = []
        real_open = Path.open

        def counting_open(path, *args, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, *args, **kwargs)

        with patch('pathlib.Path.open', autospec=True, side_effect=counting_open):
            iterator = iter(docs)
            first = next(iterator)
            second = next(iterator)
        self.assertEqual(first['id'], 'D01')
        self.assertEqual(second['id'], 'D02')
        self.assertEqual(sorted(name for name in opened if name.endswith('.txt')),
                         ['a.txt', 'b.txt'])

    def test_each_input_is_read_exactly_once_during_build(self):
        names = ['a.txt', 'b.txt', 'c.txt']
        write_corpus(self.root, {name: f'body {name}\n\nsecond {name}\n'
                                       for name in names})
        counts = {name: 0 for name in names}
        counts['manifest.json'] = 0
        real_open = Path.open

        def counting_open(path, *args, **kwargs):
            key = Path(path).name
            if key in counts:
                counts[key] += 1
            return real_open(path, *args, **kwargs)

        with patch('pathlib.Path.open', autospec=True, side_effect=counting_open):
            build(self.root / 'manifest.json', self.db)
        self.assertEqual(counts, {**{name: 1 for name in names},
                                  'manifest.json': 1})

    def test_injected_cumulative_corpus_cap_fails_whole_build(self):
        write_corpus(self.root, {
            'a.txt': 'alpha block\n\nsecond\n',
            'b.txt': 'beta block\n\nsecond\n',
            'c.txt': 'gamma block\n'})
        with patch('archive_index.ingest.MAX_CORPUS', 30):
            with self.assertRaisesRegex(ValueError, 'corpus exceeds 30 bytes'):
                build(self.root / 'manifest.json', self.db)
        self.assertFalse(self.db.exists())
        self.assertFalse(list(self.root.glob('.archive-index-*')))

    def test_corpus_cap_is_inclusive_at_the_boundary(self):
        write_corpus(self.root, {'a.txt': 'abcde', 'b.txt': 'fghij'})
        # Exactly ten captured bytes: the boundary succeeds.
        with patch('archive_index.ingest.MAX_CORPUS', 10):
            summary = build(self.root / 'manifest.json', self.db)
        self.assertEqual(summary['documents'], 2)
        # One byte less of headroom fails the whole build atomically.
        rebuilt = self.root / 'again.sqlite'
        with patch('archive_index.ingest.MAX_CORPUS', 9):
            with self.assertRaisesRegex(ValueError, 'corpus exceeds 9 bytes'):
                build(self.root / 'manifest.json', rebuilt)
        self.assertFalse(rebuilt.exists())
        self.assertFalse(list(self.root.glob('.archive-index-*')))

    def test_more_than_1000_documents_rejected_without_reading_bodies(self):
        entries = []
        for index in range(1001):
            name = f'd{index:04d}.txt'
            (self.root / name).write_text(f'document {index}\n')
            entries.append({'id': f'D{index:04d}', 'title': 'T',
                            'collection': 'c', 'path': name})
        (self.root / 'manifest.json').write_text(
            json.dumps({'schema_version': 1, 'documents': entries}))
        real_open = Path.open

        def counting_open(path, *args, **kwargs):
            if Path(path).name.startswith('d') and Path(path).name.endswith('.txt'):
                raise AssertionError('document body opened during manifest validation')
            return real_open(path, *args, **kwargs)

        with patch('pathlib.Path.open', autospec=True, side_effect=counting_open):
            with self.assertRaisesRegex(ValueError, '1..1000'):
                build(self.root / 'manifest.json', self.db)

    def test_last_document_invalid_utf8_preserves_old_snapshot(self):
        documents = {
            'a.txt': 'alpha\n\nbeta\n',
            'b.txt': 'good middle\n',
            'c.txt': 'still fine\n\nsecond\n',
            'd.txt': 'valid tail before corruption\n'}
        write_corpus(self.root, documents)
        build(self.root / 'manifest.json', self.db)
        before = self.db.read_bytes()
        # Corrupt the final (highest-id) document after the snapshot exists.
        (self.root / 'd.txt').write_bytes(
            b'leading good text\n\n\xff\xfe invalid tail')
        with self.assertRaises(UnicodeDecodeError):
            build(self.root / 'manifest.json', self.db)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.archive-index-*')))

    def test_mid_stream_nul_and_blank_documents_fail_atomically(self):
        build_manifest = self.root / 'manifest.json'
        write_corpus(self.root, {'a.txt': 'alpha\n\nbeta\n',
                                 'b.txt': 'good\n'})
        build(build_manifest, self.db)
        before = self.db.read_bytes()
        for bad in (b'good prefix\n\n\x00 nul', b'   \n\n\t\n'):
            with self.subTest(bad=bad):
                write_corpus(self.root, {'a.txt': 'alpha\n\nbeta\n',
                                         'b.txt': bad})
                with self.assertRaises(ValueError):
                    build(build_manifest, self.db)
                self.assertEqual(self.db.read_bytes(), before)
                self.assertFalse(list(self.root.glob('.archive-index-*')))

    def test_publish_failure_preserves_old_and_cleans_temporary(self):
        write_corpus(self.root, {'a.txt': 'alpha\n\nbeta\n'})
        build(self.root / 'manifest.json', self.db)
        before = self.db.read_bytes()
        with patch('archive_index.store.Path.replace',
                   side_effect=OSError('injected replacement failure')):
            with self.assertRaises(OSError):
                build(self.root / 'manifest.json', self.db)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.archive-index-*')))

    def test_real_cli_build_and_search_on_streamed_snapshot(self):
        write_corpus(self.root, {
            'a.txt': 'Harbor restoration used lime mortar.\n\nWinter work.\n',
            'b.txt': 'Archive catalogue desk notes.\n'})
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / 'src'))

        def cli(*args):
            return subprocess.run(
                [sys.executable, '-m', 'archive_index.cli', *map(str, args)],
                capture_output=True, text=True, cwd=self.root, env=env)

        built = cli('build', '--manifest', self.root / 'manifest.json',
                    '--database', self.db)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(json.loads(built.stdout)['documents'], 2)
        found = cli('search', '--database', self.db,
                    '--query', 'restoration', '--limit', '1')
        self.assertEqual(found.returncode, 0, found.stderr)
        matches = json.loads(found.stdout)['matches']
        self.assertEqual(matches[0]['document_id'], 'D01')
        self.assertEqual(matches[0]['text'],
                         'Harbor restoration used lime mortar.')
        self.assertEqual(search(self.db, 'restoration'), matches)


class MemoryProbeTests(unittest.TestCase):
    """The probe runs in a fresh ``-I`` subprocess and measures build only."""

    def run_probe(self, docs, doc_bytes, directory):
        corpus = Path(directory)
        corpus.mkdir(parents=True, exist_ok=True)
        entries = []
        # Roughly one hundred sizeable paragraphs per 1 MiB document,
        # mirroring a realistic archive text rather than pathological shards.
        word = b'archive index paragraph body word data content marker'
        sentence = (word + b' ') * (10 * 1024 // len(word) + 1)
        joined = b'\n\n'.join([sentence] * 128)
        block = joined[:doc_bytes]
        self.assertEqual(len(block), doc_bytes)
        self.assertIn(b'\n\n', block)
        for index in range(docs):
            name = f'doc{index:03d}.txt'
            (corpus / name).write_bytes(block)
            entries.append({'id': f'D{index:03d}', 'title': f'Title {index:03d}',
                            'collection': 'engineering', 'path': name})
        manifest = corpus / 'manifest.json'
        manifest.write_text(json.dumps({'schema_version': 1,
                                        'documents': entries}))
        database = corpus / 'archive.sqlite'
        report = corpus / 'report.json'
        completed = subprocess.run(
            [sys.executable, '-I', str(REPO_ROOT / 'tests' / 'memory_probe.py'),
             '--manifest', manifest, '--database', database, '--out', report],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(report.read_text())

    def measure_peaks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            small = self.run_probe(8, ONE_MIB, root / 'small')
            large = self.run_probe(16, ONE_MIB, root / 'large')
        return {'corpus_small': 8 * ONE_MIB, 'corpus_large': 16 * ONE_MIB,
                'small': small, 'large': large}

    def test_peak_stays_under_32mib_and_does_not_grow_with_corpus(self):
        info = self.measure_peaks()
        small, large = info['small'], info['large']
        self.assertLess(small['tracemalloc_peak_bytes'], THIRTY_TWO_MIB)
        self.assertLess(large['tracemalloc_peak_bytes'], THIRTY_TWO_MIB)
        # Doubling total corpus size must not double build memory: the peak
        # tracks the fixed 1 MiB document size, so both peaks stay close.
        self.assertLess(large['tracemalloc_peak_bytes'],
                        small['tracemalloc_peak_bytes'] + 2 * ONE_MIB)
        # Whole-process RSS includes the interpreter and SQLite; it must stay
        # bounded well below what an eager full-corpus build would need.
        self.assertLess(large['process_max_rss_kib'] * 1024,
                        THIRTY_TWO_MIB * 4)
        self.assertEqual(small['summary']['documents'], 8)
        self.assertEqual(large['summary']['documents'], 16)
        self.assertEqual(large['summary']['paragraphs'],
                         small['summary']['paragraphs'] * 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
