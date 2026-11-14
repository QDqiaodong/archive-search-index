"""Replay one saved property failure without touching the product oracle.

Every failure bundle contains a ``replay.sh`` with the exact command.  The
probe rebuilds a fresh database from the saved raw bytes, then re-executes
only the check that failed:

  search   compare the complete hit set (and citation fields) to the oracle
  limit    compare a truncated result to the ranked prefix
  order    re-check the documented ordering and tie rules
  reorder  rebuild from a reversed manifest and compare hit sets per query
  rebuild  rebuild twice from the same manifest and compare hit sets
  build    run the real CLI build and check document/paragraph counts

``--via-cli`` routes searches through ``python -m archive_index.cli``.

Exit 0 means the case passes now; exit 1 means it still fails; exit 2 is a
usage or setup error.  ``--json`` emits a machine-readable report.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))

import property_oracle as oracle  # noqa: E402
from archive_index.query import search  # noqa: E402
from archive_index.store import build  # noqa: E402

MODES = ('search', 'limit', 'order', 'reorder', 'rebuild', 'build')


class ProbeError(Exception):
    """Setup failure: the case could not be evaluated at all."""


def cli_search(database, query, collection, limit):
    command = [sys.executable, '-m', 'archive_index.cli', 'search',
               '--database', str(database), '--query', query,
               '--limit', str(limit)]
    if collection is not None:
        command += ['--collection', collection]
    completed = subprocess.run(command, capture_output=True, text=True,
                               cwd=REPO_ROOT, env=environment())
    if completed.returncode != 0:
        raise ProbeError(f'CLI search exited {completed.returncode}: '
                         f'{completed.stderr.strip()}')
    return json.loads(completed.stdout)['matches']


def environment():
    return dict(os.environ, PYTHONPATH=str(REPO_ROOT / 'src'))


def cli_build(manifest, database):
    command = [sys.executable, '-m', 'archive_index.cli', 'build',
               '--manifest', str(manifest), '--database', str(database)]
    completed = subprocess.run(command, capture_output=True, text=True,
                               cwd=REPO_ROOT, env=environment())
    if completed.returncode != 0:
        raise ProbeError(f'CLI build exited {completed.returncode}: '
                         f'{completed.stderr.strip()}')
    return json.loads(completed.stdout)


def keys(hits):
    return [(hit['document_id'], hit['paragraph']) for hit in hits]


def run_search(database, query, collection, limit, via_cli):
    if via_cli:
        return cli_search(database, query, collection, limit)
    return search(database, query, collection, limit)


def check_mode(mode, corpus_root, query, collection, limit, via_cli):
    """Return (ok, issues, extra) for one replayed case."""
    working = Path(corpus_root)
    corpus = oracle.corpus_from_directory(working)
    rows = oracle.derive_rows(corpus)
    if mode == 'build':
        database = working / 'cli-build.sqlite'
        summary = cli_build(working / 'manifest.json', database)
        expected_paragraphs = len(rows)
        issues = []
        if summary['documents'] != len({row['document_id'] for row in rows}):
            issues.append(f'documents {summary["documents"]} != '
                          f'{len({row["document_id"] for row in rows})}')
        if summary['paragraphs'] != expected_paragraphs:
            issues.append(f'paragraphs {summary["paragraphs"]} != '
                          f'{expected_paragraphs}')
        return not issues, issues, summary

    database = working / 'archive.sqlite'
    build(working / 'manifest.json', database)

    if mode == 'search':
        hits = run_search(database, query, collection, limit, via_cli)
        if limit < 100:
            issues = oracle.check_hits(hits, rows, query, collection,
                                       expect_complete=False)
            full = run_search(database, query, collection, 100, via_cli)
            if keys(hits) != keys(full[:limit]):
                issues.append(f'truncated result {keys(hits)} is not the '
                              f'ranked prefix {keys(full[:limit])}')
        else:
            issues = oracle.check_hits(hits, rows, query, collection)
        return not issues, issues, {'actual': keys(hits)}

    if mode == 'limit':
        full = run_search(database, query, collection, 100, via_cli)
        cut = run_search(database, query, collection, limit, via_cli)
        issues = oracle.check_hits(cut, rows, query, collection,
                                   expect_complete=False)
        if keys(cut) != keys(full[:limit]):
            issues.append(f'truncation {keys(cut)} is not the prefix of '
                          f'{keys(full)}')
        return not issues, issues, {'actual': keys(cut),
                                    'prefix': keys(full[:limit])}

    if mode == 'order':
        hits = run_search(database, query, collection, 100, via_cli)
        issues = oracle.ordering_issues(hits)
        again = run_search(database, query, collection, 100, via_cli)
        if keys(again) != keys(hits):
            issues.append(f'second run differs: {keys(hits)} != {keys(again)}')
        return not issues, issues, {'actual': keys(hits)}

    if mode == 'reorder':
        identifiers = [document['id'] for document in corpus['documents']]
        reordered = working / 'reordered'
        oracle.write_corpus(reordered, corpus, order=list(reversed(identifiers)))
        other = reordered / 'archive.sqlite'
        build(reordered / 'manifest.json', other)
        hits = run_search(database, query, collection, 100, via_cli)
        other_hits = run_search(other, query, collection, 100, via_cli)
        issues = []
        if keys(hits) != keys(other_hits):
            issues.append(f'{keys(hits)} != {keys(other_hits)}')
        return not issues, issues, {'actual': keys(hits),
                                    'reordered': keys(other_hits)}

    if mode == 'rebuild':
        rebuilt = working / 'rebuilt.sqlite'
        build(working / 'manifest.json', rebuilt)
        hits = run_search(database, query, collection, 100, via_cli)
        again = run_search(rebuilt, query, collection, 100, via_cli)
        issues = []
        if keys(hits) != keys(again):
            issues.append(f'{keys(hits)} != {keys(again)}')
        return not issues, issues, {'actual': keys(hits),
                                    'rebuilt': keys(again)}

    raise ValueError(f'unknown mode {mode}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', required=True,
                        help='directory holding manifest.json and seed.txt')
    parser.add_argument('--mode', default=None, choices=MODES)
    parser.add_argument('--query')
    parser.add_argument('--collection')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--via-cli', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    source = Path(args.corpus)
    request_path = source / 'request.json'
    request = (json.loads(request_path.read_text(encoding='utf-8'))
               if request_path.exists() else {})
    query = args.query if args.query is not None else request.get('query')
    collection = (args.collection if args.collection is not None
                  else request.get('collection'))
    limit = args.limit if args.limit is not None else request.get('limit', 100)
    mode = args.mode if args.mode is not None else request.get('mode', 'search')
    via_cli = args.via_cli or bool(request.get('via_cli'))
    limit = min(int(limit or 100), 100)
    if mode in ('search', 'limit', 'order', 'reorder', 'rebuild') and not query:
        parser.error('no query supplied and none recorded in request.json')

    corpus = oracle.corpus_from_directory(source)
    with tempfile.TemporaryDirectory(prefix='property-replay-') as temporary:
        working = Path(temporary) / 'corpus'
        shutil.copytree(source, working)
        # Keep the original manifest for reorder mode.
        (working / 'reordered').mkdir()
        try:
            ok, issues, extra = check_mode(mode, working, query, collection,
                                           limit, via_cli)
        except ProbeError as error:
            ok, issues, extra, exit_code = False, [str(error)], {}, 2
        except (ValueError, OSError, __import__('sqlite3').Error) as error:
            ok, issues, extra, exit_code = False, \
                [f'{type(error).__name__}: {error}'], {}, 1
        else:
            exit_code = 0 if ok else 1

        report = {'ok': ok, 'mode': mode, 'seed': corpus.get('seed'),
                  'query': query, 'collection': collection, 'limit': limit,
                  'via_cli': via_cli, 'issues': issues, **extra}
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        elif issues:
            print(f'FAIL mode={mode} seed={corpus.get("seed")} '
                  f'query={query!r}')
            for issue in issues:
                print(f'  {issue}')
        else:
            print(f'PASS mode={mode} seed={corpus.get("seed")} '
                  f'query={query!r}')
        return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
