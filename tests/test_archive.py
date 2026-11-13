import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive_index.ingest import paragraphs
from archive_index.store import build
from archive_index.query import search

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / 'archive.sqlite'

    def custom(self, documents, texts):
        for name, text in texts.items():
            (self.root / name).write_text(text)
        p = self.root / 'manifest.json'
        p.write_text(json.dumps({'schema_version':1,'documents':documents}))
        return p

    def item(self, id='D1', path='one.txt'):
        return dict(id=id,title='Example',collection='notes',path=path)

    def test_fixture_search_and_citations(self):
        result = build(FIXTURES/'manifest.json',self.db)
        self.assertEqual((result['documents'],result['paragraphs']),(3,5))
        hits = search(self.db,'restoration')
        self.assertEqual({r['document_id'] for r in hits},{'A1','A2'})
        # Title matches all paragraphs of A1; the actual body citation is explicit.
        body = next(r for r in hits if 'lime mortar' in r['text'])
        self.assertEqual((body['start_line'],body['end_line']),(1,2))
        self.assertEqual(body['source_sha256'],hashlib.sha256((FIXTURES/'harbor.txt').read_bytes()).hexdigest())
        with sqlite3.connect(self.db) as c:
            raw = c.execute('SELECT raw FROM documents WHERE id=?',('A1',)).fetchone()[0]
            self.assertEqual(raw,(FIXTURES/'harbor.txt').read_bytes())
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_phrase_filter_and_no_results(self):
        build(FIXTURES/'manifest.json',self.db)
        hits = search(self.db,'"winter maintenance"',collection='engineering')
        self.assertEqual({r['document_id'] for r in hits},{'A1','A3'})
        self.assertEqual(search(self.db,'restoration',collection='library')[0]['document_id'],'A2')
        self.assertEqual(search(self.db,'unfindable'),[])

    def test_unicode_normalization_is_search_only(self):
        p=self.custom([self.item()],{'one.txt':'Café restoration\n\n原始档案内容'})
        build(p,self.db)
        self.assertEqual(search(self.db,'cafe')[0]['text'],'Café restoration')
        self.assertEqual(search(self.db,'原始档案内容')[0]['start_line'],3)

    def test_blank_lines_and_crlf_citations(self):
        self.assertEqual(list(paragraphs('\r\nalpha\r\nbeta\r\n \r\ngamma\r\n')),
                         [(2,3,'alpha\nbeta'),(5,5,'gamma')])

    def test_duplicate_ids_and_paths_rejected(self):
        for docs in [[self.item(),self.item(path='two.txt')],
                     [self.item(),self.item(id='D2')]]:
            with self.subTest(docs=docs):
                p=self.custom(docs,{'one.txt':'alpha','two.txt':'beta'})
                with self.assertRaisesRegex(ValueError,'duplicate'):
                    build(p,self.db)
                self.assertFalse(self.db.exists())

    def test_path_escape_and_symlink_rejected(self):
        for path in ['../escape.txt','/etc/hosts']:
            with self.subTest(path=path):
                p=self.custom([self.item(path=path)],{})
                with self.assertRaises(ValueError):build(p,self.db)
        (self.root/'real.txt').write_text('alpha')
        (self.root/'link.txt').symlink_to(self.root/'real.txt')
        p=self.custom([self.item(path='link.txt')],{})
        with self.assertRaisesRegex(ValueError,'symlink'):build(p,self.db)

    def test_invalid_text_does_not_replace_database(self):
        build(FIXTURES/'manifest.json',self.db)
        before=self.db.read_bytes()
        for data in [b'\xff',b'\x00',b'   \n\n']:
            p=self.custom([self.item()],{'one.txt':'placeholder'})
            (self.root/'one.txt').write_bytes(data)
            with self.assertRaises(ValueError):build(p,self.db)
            self.assertEqual(self.db.read_bytes(),before)

    def test_limits_duplicate_json_keys_and_bool_version(self):
        p=self.custom([self.item()],{'one.txt':'long enough'})
        with patch('archive_index.ingest.MAX_DOCUMENT',4):
            with self.assertRaisesRegex(ValueError,'exceeds'):build(p,self.db)
        p.write_text('{"schema_version":1,"schema_version":1,"documents":[]}')
        with self.assertRaisesRegex(ValueError,'duplicate JSON'):build(p,self.db)
        p.write_text('{"schema_version":true,"documents":[]}')
        with self.assertRaisesRegex(ValueError,'schema_version'):build(p,self.db)

    def test_publish_failure_preserves_old_and_cleans_temporary(self):
        build(FIXTURES/'manifest.json',self.db)
        before=self.db.read_bytes()
        with patch('archive_index.store.Path.replace',side_effect=OSError('injected replacement failure')):
            with self.assertRaises(OSError):build(FIXTURES/'manifest.json',self.db)
        self.assertEqual(before,self.db.read_bytes())
        self.assertFalse(list(self.root.glob('.archive-index-*')))
        self.assertEqual(build(FIXTURES/'manifest.json',self.db)['paragraphs'],5)

    def test_input_cannot_be_output(self):
        p=self.custom([self.item()],{'one.txt':'alpha'})
        for output in [p,self.root/'one.txt']:
            before=output.read_bytes()
            with self.assertRaisesRegex(ValueError,'overwrite'):build(p,output)
            self.assertEqual(output.read_bytes(),before)

    def test_query_read_only_missing_and_bad_syntax(self):
        with self.assertRaises(sqlite3.Error):search(self.db,'alpha')
        self.assertFalse(self.db.exists())
        build(FIXTURES/'manifest.json',self.db)
        before=self.db.read_bytes()
        with self.assertRaises(sqlite3.Error):search(self.db,'"unclosed')
        self.assertEqual(self.db.read_bytes(),before)
        for limit in [0,101,True]:
            with self.assertRaises(ValueError):search(self.db,'alpha',limit=limit)

    def test_cli_end_to_end_and_repeatability(self):
        def cli(*args):
            return subprocess.run([sys.executable,'-m','archive_index.cli',*map(str,args)],
                                  capture_output=True,text=True,cwd=self.root)
        p=cli('build','--manifest',FIXTURES/'manifest.json','--database',self.db)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout)['paragraphs'],5)
        args=('search','--database',self.db,'--query','"winter maintenance"','--limit','1')
        first=cli(*args)
        self.assertEqual(first.returncode,0,first.stderr)
        self.assertEqual(len(json.loads(first.stdout)['matches']),1)
        self.assertEqual(cli(*args).stdout,first.stdout)
        cli('build','--manifest',FIXTURES/'manifest.json','--database',self.db)
        self.assertEqual(cli(*args).stdout,first.stdout)
        self.assertEqual(cli('search','--database',self.db,'--query','"bad').returncode,2)


if __name__ == '__main__':
    unittest.main()
