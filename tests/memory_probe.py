"""Subprocess memory probe: measure only ``store.build`` with tracemalloc.

Run under ``python -I`` so site packages and environment variables cannot
alter the interpreter; the repository ``src`` directory is added explicitly.
Tracemalloc is started after imports and reset immediately before the build,
so the reported peak covers the build alone rather than interpreter startup.
"""
import argparse
import json
import resource
import sys
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))

from archive_index.store import build  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    tracemalloc.start()
    tracemalloc.reset_peak()
    summary = build(args.manifest, args.database)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # Whole-process high-water mark; KiB on Linux.  This complements the
    # tracemalloc number, which attributes only Python-tracked allocations.
    max_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    result = {
        'summary': summary,
        'tracemalloc_current_bytes': current,
        'tracemalloc_peak_bytes': peak,
        'process_max_rss_kib': max_rss_kib,
        'database_bytes': Path(args.database).stat().st_size,
    }
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + '\n',
                              encoding='utf-8')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
