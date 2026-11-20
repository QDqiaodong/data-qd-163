DESCRIPTION = 'Recipient-specific calendar views and delta updates'
import argparse
import json
import sys
from pathlib import Path

from .common import read, save
from . import feed


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    publish_parser = commands.add_parser(
        'publish', help='publish a full view or a delta from the internal source')
    publish_parser.add_argument('--source', type=Path, required=True)
    publish_parser.add_argument('--output', type=Path, required=True)
    apply_parser = commands.add_parser(
        'apply', help='apply a delta to the saved view and atomically update it')
    apply_parser.add_argument('--view', type=Path, required=True)
    apply_parser.add_argument('--delta', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'publish':
            if args.source.resolve() == args.output.resolve():
                raise ValueError('input and output must differ')
            data, _digest = read(args.source)
            report = feed.publish(data)
            save(args.output, report)
        else:
            if args.view.resolve() == args.delta.resolve():
                raise ValueError('view and delta paths must differ')
            view_data, _view_digest = read(args.view)
            delta_data, _delta_digest = read(args.delta)
            report = feed.apply(view_data, delta_data)
            save(args.view, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
