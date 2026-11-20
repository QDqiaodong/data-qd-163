"""Local, repeatable demonstration of the calendar-billing ledger.

The script uses only the Python standard library and the project's own
``calendar-billing`` command.  It drives one fresh, temporary SQLite ledger
through the full adjustment lifecycle: an original January bill, a revised
booking list that posts January differences on the February bill, a quiet March
bill, and a revert that reverses those differences on the new March bill while
both old bills stay byte-for-byte intact.  Every input is explicit; no system
clock is read.  Run it from the repository root:

    python3 -I examples/demo_billing.py
or  PYTHONPATH=src python3 examples/demo_billing.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'billing'
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def invoke(db, source):
    completed = subprocess.run(
        [sys.executable, '-m', 'calendar_expand.billing_cli',
         '--db', str(db), '--source', str(source)],
        capture_output=True, text=True, env=ENV)
    if completed.returncode != 0:
        return {'_exit': completed.returncode,
                '_stderr': completed.stderr.strip()}
    return json.loads(completed.stdout)


def line(entry):
    """Compact one billing line for printing."""
    fields = [entry['service_month'], entry['room'], entry['account'],
              entry['schedule'], entry['nominal_date'],
              entry['amount_cents']]
    return ' | '.join(str(field) for field in fields)


def show_bill(bill):
    for charge in bill['charges']:
        print('  charge    :', line(charge),
              '(start %s, %d min, %d free)'
              % (charge['start_utc'], charge['minutes'],
                 charge['free_minutes']))
    for adjustment in bill['adjustments']:
        print('  adjustment:', line(adjustment))
    print('  totals    :', json.dumps(bill['account_totals'],
                                      ensure_ascii=False))


def main():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / 'ledger.db'
        steps = [
            ('01 init', '01_init.json'),
            ('02 replace A=甲@09:00, B=乙@10:00', '02_replace_original.json'),
            ('03 close 2025-01', '03_close_january.json'),
            ('04 replace B=乙@10:00, C=丙@11:00', '04_replace_swapped.json'),
            ('05 retry close 2025-01 (archived)', '05_close_january_retry.json'),
            ('06 close 2025-02 (posts differences)', '06_close_february.json'),
            ('07a close 2025-03 (quiet)', '07a_close_march_quiet.json'),
        ]
        for heading, name in steps:
            emit(db, heading, name)
        print('\n--- Branch B: revert the list before closing March ---')
        branch = Path(directory) / 'branch.db'
        for heading, name in steps[:-1]:
            run_quiet(branch, FIXTURES / name)
        emit(branch, '07b replace back to A=甲@09:00, B=乙@10:00',
             '07b_replace_original_again.json')
        emit(branch, '08b close 2025-03 (reverses differences)',
             '08b_close_march_after_revert.json')
        emit(branch, '09 show 2025-01 (original bill unchanged)',
             '09_show_january.json')
        emit(branch, '10 show 2025-02 (difference bill unchanged)',
             '10_show_february.json')
        print('\nDemo finished; the temporary ledgers were removed.')


def run_quiet(db, source):
    result = invoke(db, source)
    if '_exit' in result:
        raise SystemExit('fixture failed: %s' % result['_stderr'])
    return result


def emit(db, heading, name):
    result = invoke(db, FIXTURES / name)
    print('\n=== ' + heading + ' ===')
    if '_exit' in result:
        print('  failed:', result['_stderr'])
    elif 'month' in result:
        show_bill(result)
    else:
        print(' ', json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
