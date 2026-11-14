"""Self-tests for the mutation runner's isolation and kill accounting.

These tests never mutate the real checkout: every scenario gets its own copy
of the project, produced by the runner itself.  They prove the runner refuses
to score import failures, timeouts and unrelated assertion failures as kills.
"""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1]
RUNNER_PATH = REPO_ROOT / 'tools' / 'mutation_runner.py'

_spec = importlib.util.spec_from_file_location('mutation_runner', RUNNER_PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class ClassificationTests(unittest.TestCase):
    def result(self, **overrides):
        base = {'returncode': 1, 'timed_out': False, 'ran': 10,
                'fails': ['test_hits_match_reference_for_every_query'],
                'output': 'FAIL: test_hits_match_reference_for_every_query '
                          '(test_property.PropertyTests.X)',
                'seconds': 1.0}
        base.update(overrides)
        return base

    def test_green_suite_survives(self):
        self.assertEqual(runner.classify(self.result(returncode=0))[0],
                         'survived')

    def test_expected_assertion_is_a_kill(self):
        status, _ = runner.classify(
            self.result(), ('test_hits_match_reference_for_every_query',))
        self.assertEqual(status, 'killed')

    def test_unrelated_assertion_is_not_a_kill(self):
        status, reason = runner.classify(
            self.result(fails=['test_manifest_order_does_not_change_hits']),
            ('test_hits_match_reference_for_every_query',))
        self.assertEqual(status, 'unexpected')
        self.assertIn('expected assertions', reason)

    def test_failure_without_method_is_not_a_kill(self):
        status, _ = runner.classify(self.result(fails=[]))
        self.assertEqual(status, 'invalid')

    def test_import_error_is_not_a_kill(self):
        output = ('ERROR: test_property (unittest.loader._FailedTest)\n'
                  'ImportError: cannot import name build\n'
                  'FAILED (errors=1)')
        status, reason = runner.classify(self.result(output=output,
                                                     fails=['test_property']))
        self.assertEqual(status, 'invalid')
        self.assertIn('ImportError', reason)

    def test_module_not_found_is_not_a_kill(self):
        output = 'ModuleNotFoundError: No module named archive_index'
        status, _ = runner.classify(self.result(output=output))
        self.assertEqual(status, 'invalid')

    def test_timeout_is_not_a_kill(self):
        status, reason = runner.classify(
            self.result(timed_out=True, returncode=None))
        self.assertEqual(status, 'invalid')
        self.assertIn('timed out', reason)

    def test_no_collection_is_not_a_kill(self):
        status, _ = runner.classify(self.result(ran=None, returncode=2))
        self.assertEqual(status, 'invalid')


class ApplicationTests(unittest.TestCase):
    def test_mutation_applies_once_or_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            shutil.copytree(REPO_ROOT / 'src', root / 'src')
            text = (root / 'src' / 'archive_index' / 'store.py').read_text()
            mutant = {'name': 'probe', 'file': 'src/archive_index/store.py',
                      'old': "(count,doc['title'],body)",
                      'new': "(count,'',body)"}
            runner.apply_mutation(root, mutant)
            mutated = (root / 'src' / 'archive_index' / 'store.py').read_text()
            self.assertNotEqual(text, mutated)
            self.assertEqual(mutated.count(mutant['new']), 1)
            with self.assertRaisesRegex(ValueError, 'expected one match'):
                runner.apply_mutation(root, mutant)

    def test_every_declared_mutant_is_present_exactly_once(self):
        for mutant in runner.MUTANTS:
            with self.subTest(mutant=mutant['name']):
                text = (REPO_ROOT / mutant['file']).read_text(encoding='utf-8')
                self.assertEqual(text.count(mutant['old']), 1)
                self.assertEqual(mutant['new'].count(mutant['old']), 0)
