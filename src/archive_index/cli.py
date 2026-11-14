import argparse
import json
import sqlite3
from .store import build
from .query import search
from .migrate import STATUSES, migrate, write_report


def main():
    parser = argparse.ArgumentParser(description='Build and search a local text archive with paragraph citations')
    commands = parser.add_subparsers(dest='command',required=True)
    b = commands.add_parser('build')
    b.add_argument('--manifest',required=True)
    b.add_argument('--database',required=True)
    q = commands.add_parser('search')
    q.add_argument('--database',required=True)
    q.add_argument('--query',required=True)
    q.add_argument('--collection')
    q.add_argument('--limit',type=int,default=10)
    m = commands.add_parser('migrate')
    m.add_argument('--old',required=True)
    m.add_argument('--new',required=True)
    m.add_argument('--citations',required=True)
    m.add_argument('--report',required=True)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            result = build(args.manifest,args.database)
        elif args.command == 'search':
            result = {'matches':search(args.database,args.query,args.collection,args.limit)}
        else:
            report = migrate(args.old,args.new,args.citations)
            write_report(report,args.report,inputs=(args.old,args.new,args.citations))
            counts = {status:sum(1 for c in report['citations'] if c['status']==status)
                      for status in STATUSES}
            result = {'report':args.report,'citations':len(report['citations']),**counts}
    except (ValueError,OSError,sqlite3.Error) as error:
        parser.exit(2,f'{error}\n')
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__ == '__main__':
    main()
