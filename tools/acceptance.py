"""One offline acceptance command for the property and mutation suites.

Runs, in order:

1. The complete unittest suite (existing product tests + the new property
   regressions and mutation-runner self-tests) in a subprocess.
2. The isolated mutation runner: a green baseline copy of the project, then
   five single-defect mutants, each scored for whether a new regression
   assertion kills it.
3. A corpus sample summary: seeds, documents, paragraphs and query counts
   derived by the independent oracle.

Writes a JSON report (default ``run/acceptance-report.json``) containing the
sample sizes and per-mutant evidence.  Exit 0 only when every test passes,
every mutant is killed, and the working-tree source digest is unchanged.
No network access, no third-party packages, no writes outside the report path
and temporary directories.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))
sys.path.insert(0, str(REPO_ROOT / 'tools'))

import property_oracle as oracle  # noqa: E402
import mutation_runner  # noqa: E402


def source_digest():
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / 'src').rglob('*.py')):
        digest.update(str(path.relative_to(REPO_ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def sample_summary():
    samples = []
    for seed in oracle.SEEDS:
        corpus = oracle.build_corpus(seed)
        rows = oracle.derive_rows(corpus)
        queries = oracle.query_bank(seed, corpus)
        collections = sorted({row['collection'] for row in rows})
        samples.append({
            'seed': seed,
            'documents': len(corpus['documents']),
            'paragraphs': len(rows),
            'collections': collections,
            'queries': len(queries),
            'queries_with_hits': sum(
                1 for query in queries if oracle.expected_hits(rows, query)),
            'reference_hits': sum(len(oracle.expected_hits(rows, query))
                                  for query in queries),
            'duplicate_whole_documents': sum(
                1 for raw in {d['raw'] for d in corpus['documents']}
                if [d['raw'] for d in corpus['documents']].count(raw) > 1),
            'crlf_documents': sum(1 for d in corpus['documents']
                                  if b'\r\n' in d['raw']),
        })
    return {
        'seeds': list(oracle.SEEDS),
        'per_seed': samples,
        'totals': {
            'documents': sum(s['documents'] for s in samples),
            'paragraphs': sum(s['paragraphs'] for s in samples),
            'queries': sum(s['queries'] for s in samples),
            'queries_with_hits': sum(s['queries_with_hits'] for s in samples),
            'reference_hits': sum(s['reference_hits'] for s in samples),
        },
    }


def run_unittests():
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
    command = [sys.executable, '-m', 'unittest', 'discover',
               '-s', 'tests', '-v']
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True,
                               text=True, env=environment, timeout=900)
    output = completed.stdout + '\n' + completed.stderr
    lines = [line for line in output.splitlines()
             if line.startswith(('FAIL:', 'ERROR:', 'OK', 'Ran '))]
    ran = next((int(line.split()[1])
                for line in lines if line.startswith('Ran ')), None)
    return {
        'returncode': completed.returncode,
        'ran': ran,
        'summary': lines[-2:],
        'output': output if completed.returncode != 0 else '',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default=str(REPO_ROOT / 'run' /
                                             'acceptance-report.json'))
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)

    digest_before = source_digest()
    report = {
        'python': sys.version.split()[0],
        'sqlite': __import__('sqlite3').sqlite_version,
        'source_sha256_before': digest_before,
        'samples': sample_summary(),
    }

    tests = run_unittests()
    report['tests'] = tests
    print(f"unittest: exit {tests['returncode']}, "
          f"{tests['ran']} tests, {tests['summary']}", flush=True)

    mutation = mutation_runner.run(verbose=not args.quiet)
    report['mutation'] = mutation
    report['source_unchanged'] = source_digest() == digest_before
    report['ok'] = bool(
        tests['returncode'] == 0
        and mutation.get('ok')
        and report['source_unchanged'])

    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')

    totals = report['samples']['totals']
    print(f"samples: {len(report['samples']['seeds'])} seeds, "
          f"{totals['documents']} documents, {totals['paragraphs']} paragraphs, "
          f"{totals['queries']} queries "
          f"({totals['queries_with_hits']} with hits, "
          f"{totals['reference_hits']} expected hits)")
    for record in mutation.get('mutants', []):
        evidence = ', '.join(record['fails']) or record['reason']
        print(f"  {record['status']:9} {record['name']:26} {evidence}")
    print(f"report: {output_path}")
    print('ACCEPTANCE PASSED' if report['ok'] else 'ACCEPTANCE FAILED')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
