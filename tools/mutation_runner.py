"""Isolated mutation runner for the archive search property suite.

Copies the project into a fresh directory per mutant (source is never
modified), applies exactly one textual edit, then runs the new property
regression suite in a subprocess.  A mutant counts as killed only when the
suite exits non-zero *and* at least one added regression assertion failed.
Import failures, collection errors, syntax errors and timeouts do not count:
they exit non-zero for the wrong reason and are recorded as ``invalid``.

The baseline (unmutated copy) must pass all tests before mutants are scored.

Usage:
    python tools/mutation_runner.py            # human report + exit status
    python tools/mutation_runner.py --json out # machine report to a file
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1]
PROPERTY_MODULE = 'tests/test_property.py'
BASELINE_TIMEOUT = 600
MUTANT_TIMEOUT = 600

MUTANTS = (
    {
        'name': 'title_not_indexed',
        'defect': 'titles are dropped from the FTS row while rows stay aligned',
        'file': 'src/archive_index/store.py',
        'old': "(count,doc['title'],body)",
        'new': "(count,'',body)",
        'expect_methods': ('test_hits_match_reference_for_every_query',
                           'test_collection_filter_is_exact'),
    },
    {
        'name': 'fts_row_misaligned',
        'defect': 'each FTS row is keyed to the next paragraph rowid',
        'file': 'src/archive_index/store.py',
        'old': "(count,doc['title'],body)",
        'new': "(count + 1,doc['title'],body)",
        'expect_methods': ('test_hits_match_reference_for_every_query',
                           'test_limit_truncates_a_stable_prefix'),
    },
    {
        'name': 'line_number_offset',
        'defect': 'stored citation lines are shifted by one from real lines',
        'file': 'src/archive_index/store.py',
        'old': "(count,doc['id'],number,start,end,body)",
        'new': "(count,doc['id'],number,start + 1,end + 1,body)",
        'expect_methods': ('test_hits_match_reference_for_every_query',),
    },
    {
        'name': 'collection_filter_dropped',
        'defect': 'the collection predicate is always true, so filters leak',
        'file': 'src/archive_index/query.py',
        'old': "AND (? IS NULL OR d.collection=?)",
        'new': "AND (? IS NULL OR ? IS NULL)",
        'expect_methods': ('test_collection_filter_is_exact',
                           'test_cli_matches_library_and_oracle'),
    },
    {
        'name': 'tie_order_reversed',
        'defect': 'ties sort by descending document ID and paragraph',
        'file': 'src/archive_index/query.py',
        'old': "ORDER BY rank,d.id,c.paragraph",
        'new': "ORDER BY rank,d.id DESC,c.paragraph DESC",
        'expect_methods': ('test_ordering_and_ties_are_stable',),
    },
)

# Any of these in the captured output means the mutant died for the wrong
# reason and must not be reported as killed.
_IMPORT_ERROR = re.compile(
    r'(ImportError|ModuleNotFoundError|SyntaxError|IndentationError|'
    r'NameError|AttributeError: module|collect.*error)', re.IGNORECASE)
_TEST_LINE = re.compile(r'(?:FAIL|ERROR): (test_\w+)')


def copy_project(destination):
    shutil.copytree(
        REPO_ROOT, destination,
        ignore=shutil.ignore_patterns('.git', 'run', '.venv', '__pycache__',
                                      '*.pyc', 'build', 'dist', '*.egg-info'))
    return destination


def run_suite(root, filter_pattern=None, timeout=600):
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(root) / 'src')
    environment.pop('PYTHONWARNINGS', None)
    command = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
               '-p', 'test_property.py', '-v']
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=root, capture_output=True,
                                   text=True, env=environment,
                                   timeout=timeout)
        timed_out = False
        stdout, stderr, returncode = (completed.stdout, completed.stderr,
                                      completed.returncode)
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        stdout = (expired.stdout or b'').decode('utf-8', 'replace') \
            if isinstance(expired.stdout, bytes) else (expired.stdout or '')
        stderr = (expired.stderr or b'').decode('utf-8', 'replace') \
            if isinstance(expired.stderr, bytes) else (expired.stderr or '')
        returncode = None
    elapsed = round(time.monotonic() - started, 2)
    output = stdout + '\n' + stderr
    failing = sorted({match.group(1) for match in _TEST_LINE.finditer(output)})
    ran = re.search(r'Ran (\d+) test', output)
    return {
        'returncode': returncode,
        'timed_out': timed_out,
        'output': output,
        'fails': failing,
        'ran': int(ran.group(1)) if ran else None,
        'seconds': elapsed,
    }


def classify(result, expect_methods=None):
    """Return (status, reason) for one suite execution."""
    if result['timed_out']:
        return 'invalid', 'timed out'
    if result['returncode'] is None:
        return 'invalid', 'no exit status'
    if result['ran'] is None:
        return 'invalid', 'suite did not collect or start'
    if result['returncode'] == 0:
        return 'survived', 'suite passed'
    problems = _IMPORT_ERROR.search(result['output'])
    if problems:
        return 'invalid', f'import/setup failure: {problems.group(0)}'
    if not result['fails']:
        return 'invalid', 'non-zero exit without a regression assertion'
    if expect_methods and not set(expect_methods) <= set(result['fails']):
        return 'unexpected', (f'failed {result["fails"]} but not the '
                              f'expected assertions {list(expect_methods)}')
    return 'killed', f'failed new assertions: {result["fails"]}'


def apply_mutation(root, mutant):
    path = Path(root) / mutant['file']
    text = path.read_text(encoding='utf-8')
    count = text.count(mutant['old'])
    if count != 1:
        raise ValueError(f'{mutant["name"]}: expected one match for '
                         f'{mutant["old"]!r} in {mutant["file"]}, found {count}')
    path.write_text(text.replace(mutant['old'], mutant['new'], 1),
                    encoding='utf-8')
    return path


def source_digest():
    import hashlib
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / 'src').rglob('*.py')):
        digest.update(str(path.relative_to(REPO_ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(verbose=False, keep=None, mutants=MUTANTS, root_override=None):
    """Execute the baseline and every mutant; return a report dict."""
    started = time.monotonic()
    digest_before = source_digest()
    report = {
        'suite': PROPERTY_MODULE,
        'python': sys.version.split()[0],
        'sqlite': __import__('sqlite3').sqlite_version,
        'source_sha256': digest_before,
        'mutants': [],
    }
    with tempfile.TemporaryDirectory(prefix='archive-mutants-') as temporary:
        root = Path(temporary) / 'baseline'
        copy_project(root)
        baseline = run_suite(root, timeout=BASELINE_TIMEOUT)
        status, reason = classify(baseline, None)
        if status != 'survived':
            report['baseline'] = {'status': 'failed', 'reason': reason,
                                  'returncode': baseline['returncode'],
                                  'fails': baseline['fails'],
                                  'ran': baseline['ran']}
            report['ok'] = False
            report['source_unchanged'] = source_digest() == digest_before
            return report
        report['baseline'] = {'status': 'green', 'ran': baseline['ran'],
                              'seconds': baseline['seconds']}
        for mutant in mutants:
            work = Path(temporary) / mutant['name']
            copy_project(work)
            apply_mutation(work, mutant)
            result = run_suite(work, timeout=MUTANT_TIMEOUT)
            status, reason = classify(result, mutant['expect_methods'])
            record = {
                'name': mutant['name'],
                'defect': mutant['defect'],
                'file': mutant['file'],
                'change': {'old': mutant['old'], 'new': mutant['new']},
                'status': status,
                'reason': reason,
                'fails': result['fails'],
                'returncode': result['returncode'],
                'ran': result['ran'],
                'seconds': result['seconds'],
                'expected_methods': list(mutant['expect_methods']),
            }
            if verbose:
                print(f'[{status:9}] {mutant["name"]}: {reason}', flush=True)
            report['mutants'].append(record)
    report['source_unchanged'] = source_digest() == digest_before
    report['duration_seconds'] = round(time.monotonic() - started, 2)
    report['killed'] = sum(1 for item in report['mutants']
                           if item['status'] == 'killed')
    report['total'] = len(report['mutants'])
    report['ok'] = (report['killed'] == report['total']
                    and report['source_unchanged'])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', metavar='PATH',
                        help='write the full machine-readable report here')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)
    report = run(verbose=args.verbose)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + '\n', encoding='utf-8')
    print(f"baseline: {report['baseline'].get('status')} "
          f"({report['baseline'].get('ran')} tests)")
    for record in report.get('mutants', []):
        print(f"{record['status']:9} {record['name']:26} "
              f"{record['reason']}")
    if 'killed' in report:
        print(f"{report['killed']}/{report['total']} mutants killed; "
              f"source unchanged: {report['source_unchanged']}")
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
