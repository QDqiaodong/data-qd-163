DESCRIPTION = 'Persistent local calendar reminder work queue'
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .common import read
from .reminders import process


def run(db_path, source):
    data, digest = read(source)
    report = process(str(db_path), data)
    # The envelope must carry exactly schema_version, source_sha256 and result.
    return {'schema_version': 1, 'source_sha256': digest,
            'result': report['result']}


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.db.is_symlink():
            raise ValueError('database cannot be a symlink')
        if args.source.resolve() == args.db.resolve():
            raise ValueError('database and input paths must differ')
        report = run(args.db, args.source)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
