"""Local, repeatable demonstration of the calendar-reminders lifecycle.

The script uses only the Python standard library and the project's own
``calendar-reminders`` command.  It drives two fresh, temporary SQLite
databases through the two acceptance branches and prints every envelope.  No
wall clock is read: every ``now`` is an explicit input, and no lease time is
waited for.  Run it from the repository root:

    python3 -I examples/demo_reminders.py
or  PYTHONPATH=src python3 examples/demo_reminders.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'reminders'
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def rule(schedule, hour):
    return {
        'schedule': schedule, 'timezone': 'UTC',
        'local_start': f'2024-11-04T{hour:02d}:00', 'interval_weeks': 1,
        'weekdays': [1], 'until': '2024-11-04', 'duration_minutes': 60,
        'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier'}


def entry(schedule, hour):
    return {'rule': rule(schedule, hour), 'lead_minutes': 30}


def invoke(db, source):
    completed = subprocess.run(
        [sys.executable, '-m', 'calendar_expand.reminders_cli',
         '--db', str(db), '--source', str(source)],
        capture_output=True, text=True, env=ENV)
    if completed.returncode != 0:
        return {'_exit': 2, '_stderr': completed.stderr.strip()}
    return json.loads(completed.stdout)


def write(document, directory, name):
    path = Path(directory) / name
    path.write_text(json.dumps(document))
    return path


def heading(text):
    print('\n=== ' + text + ' ===')


def branch_one(directory):
    """Conflict while leased, then ack and permanent tombstone suppression."""
    heading('Branch 1: active lease blocks a conflicting sync; ack suppresses re-add')
    db = Path(directory) / 'branch1.db'
    print('01 sync        :', json.dumps(invoke(db, FIXTURES / '01_sync.json')['result']))
    claim = invoke(db, FIXTURES / '02_claim.json')['result']
    print('02 claim       :', json.dumps(claim))
    token = claim['claims'][0]['token']
    conflict = invoke(db, FIXTURES / '03_conflicting_sync.json')
    print('03 bad sync    :', json.dumps(conflict))
    print('04 inspect     :', json.dumps(invoke(db, FIXTURES / '04_inspect_after_conflict.json')['result']))
    ack_doc = write({'action': 'ack', 'now': '2024-11-04T08:36:00Z',
                     'schedule': 'A', 'nominal_date': '2024-11-04',
                     'token': token}, directory, '05_ack.json')
    print('05 ack         :', json.dumps(invoke(db, ack_doc)['result']))
    print('06 empty sync  :', json.dumps(invoke(db, FIXTURES / '06_empty_sync.json')['result']))
    print('07 re-add A@10 :', json.dumps(invoke(db, FIXTURES / '07_readd_a_ten.json')['result']))
    print('08 claim (empty):', json.dumps(invoke(db, FIXTURES / '08_claim_after_readd.json')['result']))
    print('09 inspect     :', json.dumps(invoke(db, FIXTURES / '09_inspect_final.json')['result']))


def branch_two(directory):
    """Lease expiry allows reschedule; old tokens die and a new token is minted."""
    heading('Branch 2: expiry permits reschedule; old token cannot ack new work')
    db = Path(directory) / 'branch2.db'
    first = write({'action': 'sync', 'now': '2024-11-04T08:00:00Z',
                   'schedules': [entry('A', 9), entry('B', 11)]}, directory, 'b01.json')
    print('b01 sync       :', json.dumps(invoke(db, first)['result']))
    claim_doc = write({'action': 'claim', 'now': '2024-11-04T08:30:00Z',
                       'limit': 1, 'lease_minutes': 10}, directory, 'b02.json')
    old_token = invoke(db, claim_doc)['result']['claims'][0]['token']
    print('b02 claim token:', old_token)
    moved = write({'action': 'sync', 'now': '2024-11-04T08:40:00Z',
                   'schedules': [entry('A', 10), entry('B', 12)]}, directory, 'b03.json')
    print('b03 sync @expiry:', json.dumps(invoke(db, moved)['result']))
    stale = write({'action': 'ack', 'now': '2024-11-04T08:40:00Z',
                   'schedule': 'A', 'nominal_date': '2024-11-04',
                   'token': old_token}, directory, 'b04.json')
    print('b04 stale ack  :', json.dumps(invoke(db, stale)))
    reclaim = write({'action': 'claim', 'now': '2024-11-04T09:30:00Z',
                     'limit': 5, 'lease_minutes': 10}, directory, 'b05.json')
    new_grant = invoke(db, reclaim)['result']['claims'][0]
    print('b05 reclaim    :', json.dumps(new_grant))
    assert new_grant['token'] != old_token
    confirm = write({'action': 'ack', 'now': '2024-11-04T09:32:00Z',
                     'schedule': 'A', 'nominal_date': '2024-11-04',
                     'token': new_grant['token']}, directory, 'b06.json')
    print('b06 ack new    :', json.dumps(invoke(db, confirm)['result']))
    retry = write({'action': 'ack', 'now': '2024-11-04T09:33:00Z',
                   'schedule': 'A', 'nominal_date': '2024-11-04',
                   'token': new_grant['token']}, directory, 'b07.json')
    print('b07 retry dup  :', json.dumps(invoke(db, retry)['result']))


def main():
    with tempfile.TemporaryDirectory() as directory:
        branch_one(directory)
        branch_two(directory)
    print('\nDemo finished; temporary databases were removed.')


if __name__ == '__main__':
    main()
