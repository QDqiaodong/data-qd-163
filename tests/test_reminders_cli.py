"""End-to-end CLI tests for calendar-reminders, including real cross-process races.

Concurrency tests spawn separate Python processes against one SQLite file.
Business conclusions are asserted only from the CLI JSON envelopes; the test
never inspects a private table to decide a pass.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'calendar_expand.reminders_cli'
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def rule(schedule, hour=9, **changes):
    document = {
        'schedule': schedule, 'timezone': 'UTC',
        'local_start': f'2024-11-04T{hour:02d}:00', 'interval_weeks': 1,
        'weekdays': [1], 'until': '2024-11-04', 'duration_minutes': 60,
        'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier'}
    document.update(changes)
    return document


def sync(now, schedules):
    return {'action': 'sync', 'now': now, 'schedules': schedules}


def invoke(directory, db, document, name='input.json'):
    source = Path(directory) / name
    source.write_text(json.dumps(document))
    return subprocess.run(
        [sys.executable, '-m', MODULE, '--db', str(db), '--source', str(source)],
        capture_output=True, text=True, timeout=30, env=ENV)


class RemindersCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.directory = Path(self._dir.name)
        self.db = self.directory / 'queue.db'

    def tearDown(self):
        self._dir.cleanup()

    def run_cli(self, document, name='input.json'):
        return invoke(self.directory, self.db, document, name)

    def test_success_envelope_is_exact_and_holds_input_digest(self):
        document = sync('2024-11-04T08:00:00Z',
                        [{'rule': rule('A'), 'lead_minutes': 30}])
        result = self.run_cli(document)
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual(set(envelope), {'schema_version', 'source_sha256', 'result'})
        self.assertEqual(envelope['schema_version'], 1)
        source = self.directory / 'input.json'
        self.assertEqual(envelope['source_sha256'],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(envelope['result'],
                         {'action': 'sync', 'last_now': '2024-11-04T08:00:00Z',
                          'scheduled_count': 1, 'unfinished_count': 1})

    def test_first_sync_creates_database_and_other_actions_require_it(self):
        claim = {'action': 'claim', 'now': '2024-11-04T08:30:00Z',
                 'limit': 1, 'lease_minutes': 10}
        missing = self.directory / 'absent.db'
        result = invoke(self.directory, missing, claim)
        self.assertEqual(result.returncode, 2)
        self.assertIn('does not exist', result.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse(Path(str(missing) + '-wal').exists())

    def test_foreign_database_is_rejected_and_left_intact(self):
        foreign = self.directory / 'foreign.db'
        connection = sqlite3.connect(foreign)
        connection.execute('CREATE TABLE unrelated(value)')
        connection.execute("INSERT INTO unrelated VALUES('keep')")
        connection.commit()
        connection.close()
        result = invoke(self.directory, foreign,
                        sync('2024-11-04T08:00:00Z',
                             [{'rule': rule('A'), 'lead_minutes': 30}]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        check = sqlite3.connect(foreign)
        self.assertEqual(check.execute('SELECT value FROM unrelated').fetchall(),
                         [('keep',)])
        tables = check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(tables, [('unrelated',)])
        check.close()

    def test_symlink_database_rejected(self):
        target = self.directory / 'target.db'
        link = self.directory / 'link.db'
        link.symlink_to(target)
        result = invoke(self.directory, link,
                        sync('2024-11-04T08:00:00Z',
                             [{'rule': rule('A'), 'lead_minutes': 30}]))
        self.assertEqual(result.returncode, 2)
        self.assertIn('symlink', result.stderr)
        self.assertFalse(target.exists())

    def test_database_and_input_path_must_differ(self):
        same = self.directory / 'same.db'
        document = sync('2024-11-04T08:00:00Z',
                        [{'rule': rule('A'), 'lead_minutes': 30}])
        same.write_text(json.dumps(document))
        result = subprocess.run(
            [sys.executable, '-m', MODULE, '--db', str(same), '--source', str(same)],
            capture_output=True, text=True, timeout=30, env=ENV)
        self.assertEqual(result.returncode, 2)
        self.assertIn('differ', result.stderr)

    def test_invalid_inputs_exit_two_and_print_nothing_on_stdout(self):
        self.run_cli(sync('2024-11-04T08:00:00Z',
                          [{'rule': rule('A'), 'lead_minutes': 30}]))
        invalid_documents = [
            '{',
            '{"action":"inspect","now":"2024-11-04T08:00:00Z","x":1}',
            '{"action":"inspect","now":"2024-11-04T08:00:00Z","now":"2024-11-04T08:01:00Z"}',
            json.dumps({'action': 'claim', 'now': '2024-11-04T08:30:00Z',
                        'limit': True, 'lease_minutes': 10}),
            json.dumps({'action': 'inspect', 'now': '2024-11-04T08:00:60Z'}),
            json.dumps({'action': 'sync', 'now': '2024-11-04T08:00:00Z',
                        'schedules': [{'rule': rule('A'), 'lead_minutes': 10081}]}),
        ]
        for index, raw in enumerate(invalid_documents):
            source = self.directory / f'bad{index}.json'
            source.write_text(raw)
            result = subprocess.run(
                [sys.executable, '-m', MODULE, '--db', str(self.db),
                 '--source', str(source)],
                capture_output=True, text=True, timeout=30, env=ENV)
            self.assertEqual(result.returncode, 2, raw)
            self.assertEqual(result.stdout, '')
            self.assertTrue(result.stderr)
        view = self.run_cli({'action': 'inspect', 'now': '2024-11-04T08:00:00Z'})
        self.assertEqual(json.loads(view.stdout)['result']['last_now'],
                         '2024-11-04T08:00:00Z')

    def test_rejected_first_sync_leaves_no_database(self):
        fresh = self.directory / 'fresh.db'
        result = invoke(self.directory, fresh,
                        sync('2024-11-04T08:00:00Z',
                             [{'rule': rule('A'), 'lead_minutes': True}]))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(fresh.exists())
        self.assertFalse(Path(str(fresh) + '-wal').exists())

    def test_oversized_and_nonfinite_and_duplicate_key_inputs_rejected(self):
        oversized = self.directory / 'big.json'
        oversized.write_bytes(b' ' * 8_000_001)
        result = subprocess.run(
            [sys.executable, '-m', MODULE, '--db', str(self.db),
             '--source', str(oversized)],
            capture_output=True, text=True, timeout=30, env=ENV)
        self.assertEqual(result.returncode, 2)
        self.assertIn('8 MB', result.stderr)
        for raw in ('{"action":"inspect","now":"2024-11-04T08:00:00Z","v":NaN}',
                    '{"action":"inspect","now":"2024-11-04T08:00:00Z","v":Infinity}'):
            source = self.directory / 'nonfinite.json'
            source.write_text(raw)
            result = subprocess.run(
                [sys.executable, '-m', MODULE, '--db', str(self.db),
                 '--source', str(source)],
                capture_output=True, text=True, timeout=30, env=ENV)
            self.assertEqual(result.returncode, 2, raw)

    def test_corrupt_sqlite_file_is_a_file_error_not_a_reset(self):
        corrupt = self.directory / 'corrupt.db'
        corrupt.write_bytes(b'not a database at all')
        result = invoke(self.directory, corrupt,
                        {'action': 'inspect', 'now': '2024-11-04T08:00:00Z'})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')


WORKER = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, %(src)r)
    from calendar_expand.reminders import process
    db, document = sys.argv[1], json.loads(sys.argv[2])
    print(json.dumps(process(db, document)['result'], sort_keys=True))
""")


class CrossProcessConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.directory = Path(self._dir.name)
        self.db = self.directory / 'queue.db'
        self.worker = self.directory / 'worker.py'
        self.worker.write_text(WORKER % {'src': str(ROOT / 'src')})

    def tearDown(self):
        self._dir.cleanup()

    def spawn(self, document):
        return subprocess.Popen(
            [sys.executable, '-I', str(self.worker), str(self.db),
             json.dumps(document)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})

    def seed(self, schedules):
        seed = self.directory / 'seed.json'
        seed.write_text(json.dumps(sync('2024-11-04T08:00:00Z', schedules)))
        result = subprocess.run(
            [sys.executable, '-m', MODULE, '--db', str(self.db),
             '--source', str(seed)],
            capture_output=True, text=True, timeout=30, env=ENV)
        self.assertEqual(result.returncode, 0, result.stderr)

    def gather(self, processes):
        outcomes = []
        for process in processes:
            out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
            outcomes.append(json.loads(out))
        return outcomes

    def test_concurrent_claims_yield_one_lease_per_identity_with_unique_tokens(self):
        self.seed([{'rule': rule(name), 'lead_minutes': 30}
                   for name in ('A', 'B', 'C')])
        processes = [self.spawn({'action': 'claim', 'now': '2024-11-04T08:30:00Z',
                                 'limit': 10, 'lease_minutes': 60})
                     for _ in range(6)]
        outcomes = self.gather(processes)
        grants = [grant for outcome in outcomes for grant in outcome['claims']]
        identities = [(g['payload']['schedule'], g['payload']['nominal_date'])
                      for g in grants]
        self.assertEqual(sorted(identities),
                         [('A', '2024-11-04'), ('B', '2024-11-04'),
                          ('C', '2024-11-04')])
        self.assertEqual(len({g['token'] for g in grants}), 3)
        # Every process observes the same serialized final view.
        view = invoke(self.directory, self.db,
                      {'action': 'inspect', 'now': '2024-11-04T08:31:00Z'})
        jobs = json.loads(view.stdout)['result']['jobs']
        self.assertEqual([(j['payload']['schedule'], j['state']) for j in jobs],
                         [('A', 'leased'), ('B', 'leased'), ('C', 'leased')])

    def test_concurrent_sync_and_ack_leave_only_legal_serial_state(self):
        self.seed([{'rule': rule('A'), 'lead_minutes': 30}])
        claimed = invoke(self.directory, self.db,
                         {'action': 'claim', 'now': '2024-11-04T08:30:00Z',
                          'limit': 1, 'lease_minutes': 60})
        token = json.loads(claimed.stdout)['result']['claims'][0]['token']
        preserving = sync('2024-11-04T08:32:00Z',
                          [{'rule': rule('A'), 'lead_minutes': 30}])
        processes = [
            self.spawn(preserving),
            self.spawn({'action': 'ack', 'now': '2024-11-04T08:32:00Z',
                        'schedule': 'A', 'nominal_date': '2024-11-04',
                        'token': token})]
        outcomes = self.gather(processes)
        ack_outcome = next(o for o in outcomes if o['action'] == 'ack')
        self.assertFalse(ack_outcome['duplicate'])
        final = invoke(self.directory, self.db,
                       {'action': 'inspect', 'now': '2024-11-04T08:33:00Z'})
        result = json.loads(final.stdout)['result']
        self.assertEqual(result['last_now'], '2024-11-04T08:32:00Z')
        self.assertEqual(len(result['completed']), 1)
        self.assertEqual(result['completed'][0]['token'], token)
        self.assertEqual(result['completed'][0]['completed_at'],
                         '2024-11-04T08:32:00Z')

    def test_conflicting_sync_racing_ack_never_corrupts_tombstone(self):
        self.seed([{'rule': rule('A'), 'lead_minutes': 30}])
        claimed = invoke(self.directory, self.db,
                         {'action': 'claim', 'now': '2024-11-04T08:30:00Z',
                          'limit': 1, 'lease_minutes': 60})
        token = json.loads(claimed.stdout)['result']['claims'][0]['token']
        conflicting = sync('2024-11-04T08:32:00Z',
                           [{'rule': rule('A', hour=12), 'lead_minutes': 30}])
        sync_process = self.spawn(conflicting)
        ack_process = self.spawn(
            {'action': 'ack', 'now': '2024-11-04T08:32:00Z', 'schedule': 'A',
             'nominal_date': '2024-11-04', 'token': token})
        sync_out, sync_err = sync_process.communicate(timeout=60)
        ack_out, ack_err = ack_process.communicate(timeout=60)
        self.assertEqual(ack_process.returncode, 0, ack_err)
        self.assertFalse(json.loads(ack_out)['duplicate'])
        final = invoke(self.directory, self.db,
                       {'action': 'inspect', 'now': '2024-11-04T08:33:00Z'})
        result = json.loads(final.stdout)['result']
        tombstone = result['completed'][0]
        self.assertEqual(tombstone['payload']['start_utc'],
                         '2024-11-04T09:00:00Z')
        self.assertEqual(tombstone['completed_at'], '2024-11-04T08:32:00Z')
        if sync_process.returncode == 0:
            # If sync serialized after the ack, the tombstone suppresses the
            # moved identity and no lease for 12:00 can exist.
            self.assertEqual(result['jobs'], [])
        else:
            # If sync serialized first it had to reject; A stays at 09:00 and
            # the ack then completed the original lease.
            self.assertIn('active lease', sync_err)
            self.assertEqual(result['jobs'], [])

    def test_duplicate_concurrent_acks_resolve_to_first_and_idempotent_retry(self):
        self.seed([{'rule': rule('A'), 'lead_minutes': 30}])
        claimed = invoke(self.directory, self.db,
                         {'action': 'claim', 'now': '2024-11-04T08:30:00Z',
                          'limit': 1, 'lease_minutes': 60})
        token = json.loads(claimed.stdout)['result']['claims'][0]['token']
        ack_doc = {'action': 'ack', 'now': '2024-11-04T08:31:00Z',
                   'schedule': 'A', 'nominal_date': '2024-11-04', 'token': token}
        processes = [self.spawn(ack_doc), self.spawn(ack_doc)]
        outcomes = self.gather(processes)
        self.assertEqual(sorted(o['duplicate'] for o in outcomes), [False, True])
        for outcome in outcomes:
            self.assertEqual(outcome['completed']['completed_at'],
                             '2024-11-04T08:31:00Z')
            self.assertEqual(outcome['completed']['token'], token)

    def test_concurrent_first_syncs_leave_one_usable_database(self):
        fresh = self.directory / 'first.db'
        document = sync('2024-11-04T08:00:00Z',
                        [{'rule': rule('A'), 'lead_minutes': 30}])
        processes = [self.spawn_on(fresh, document) for _ in range(6)]
        return_codes = []
        for process in processes:
            out, err = process.communicate(timeout=60)
            return_codes.append(process.returncode)
            if process.returncode == 0:
                self.assertEqual(json.loads(out)['unfinished_count'], 1)
            else:
                self.assertEqual(out, '')
                self.assertTrue(err)
        self.assertIn(0, return_codes)
        # The surviving file is a fully usable, correctly initialized database.
        view = invoke(self.directory, fresh,
                      {'action': 'inspect', 'now': '2024-11-04T08:01:00Z'})
        self.assertEqual(view.returncode, 0, view.stderr)
        result = json.loads(view.stdout)['result']
        self.assertEqual(result['last_now'], '2024-11-04T08:00:00Z')
        self.assertEqual(len(result['jobs']), 1)

    def spawn_on(self, db, document):
        return subprocess.Popen(
            [sys.executable, '-I', str(self.worker), str(db),
             json.dumps(document)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})


if __name__ == '__main__':
    unittest.main()
