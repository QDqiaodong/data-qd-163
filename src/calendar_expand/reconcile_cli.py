DESCRIPTION = 'Reconcile planned calendar instances with actual execution records'
import argparse
import json
import sys
from pathlib import Path

from .common import read, save
from .reconcile import evaluate


def run(source):
    data, digest = read(source)
    return {'schema_version': 1, 'source_sha256': digest, 'result': evaluate(data)}


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
