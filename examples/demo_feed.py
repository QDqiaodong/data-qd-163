"""Local, repeatable demonstration of the calendar-feed publish/apply flow.

The script uses only the Python standard library and the project's own
``calendar-feed`` command.  It plays the operations side (``publish``) and the
partner side (``apply``) of the contracted two-group revocation sequence in a
fresh temporary directory, then shows that a delta whose base fingerprint does
not match the saved view is rejected while the view keeps its exact bytes.
Run it from the repository root:

    python3 -I examples/demo_feed.py
or  PYTHONPATH=src python3 examples/demo_feed.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'feed'
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def invoke(*arguments):
    completed = subprocess.run(
        [sys.executable, '-m', 'calendar_expand.feed_cli', *arguments],
        capture_output=True, text=True, env=ENV)
    if completed.returncode != 0:
        return {'_exit': 2, '_stderr': completed.stderr.strip()}
    return json.loads(completed.stdout)


def heading(text):
    print('\n=== ' + text + ' ===')


def main():
    with tempfile.TemporaryDirectory() as directory:
        view = Path(directory) / 'view.json'
        heading('Publish the full view (alpha=detail, beta=busy)')
        print(json.dumps(invoke('publish', '--source',
                                str(FIXTURES / '01_publish_full.json'),
                                '--output', str(view)),
                         ensure_ascii=False))
        heading('Revoke alpha detail; partner applies the busy-only delta')
        delta = Path(directory) / 'delta1.json'
        print(json.dumps(invoke('publish', '--source',
                                str(FIXTURES / '02_publish_delta_busy.json'),
                                '--output', str(delta)),
                         ensure_ascii=False))
        print(json.dumps(invoke('apply', '--view', str(view),
                                '--delta', str(delta)), ensure_ascii=False))
        heading('Revoke beta as well; partner applies the withdrawal delta')
        delta = Path(directory) / 'delta2.json'
        print(json.dumps(invoke('publish', '--source',
                                str(FIXTURES / '03_publish_delta_hidden.json'),
                                '--output', str(delta)),
                         ensure_ascii=False))
        print(json.dumps(invoke('apply', '--view', str(view),
                                '--delta', str(delta)), ensure_ascii=False))
        heading('A replayed stale delta is rejected; the saved view is kept')
        before = view.read_bytes()
        print(json.dumps(invoke('apply', '--view', str(view),
                                '--delta', str(delta))))
        print('view bytes preserved:', view.read_bytes() == before)
    print('\nDemo finished; temporary files were removed.')


if __name__ == '__main__':
    main()
