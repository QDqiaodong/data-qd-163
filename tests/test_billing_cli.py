"""End-to-end CLI tests for calendar-billing.

Each check runs the real command in a fresh Python process against a real
SQLite file, so cross-process durability of archived bills is exercised rather
than assumed.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'calendar_expand.billing_cli'
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def rule(schedule, start='2025-01-06T09:00', duration=60):
    return {
        'schedule': schedule, 'timezone': 'UTC', 'local_start': start,
        'interval_weeks': 1, 'weekdays': [1], 'until': start[:10],
        'duration_minutes': duration, 'exceptions': [],
        'gap_policy': 'skip', 'fold_policy': 'earlier'}


def booking(schedule, account, *, room='R', **changes):
    return {'room': room, 'account': account,
            'rule': rule(schedule, **changes)}


def init():
    return {'action': 'init', 'timezone': 'UTC', 'first_month': '2025-01',
            'rooms': [{'room': 'R', 'free_minutes': 60, 'unit_cents': 2}]}


def replace(*bookings):
    return {'action': 'replace', 'bookings': list(bookings)}


def close(month):
    return {'action': 'close', 'month': month}


def show(month):
    return {'action': 'show', 'month': month}


class BillingCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.directory = Path(self._dir.name)
        self.db = self.directory / 'ledger.db'
        self._names = 0

    def tearDown(self):
        self._dir.cleanup()

    def run_cli(self, document, db=None):
        self._names += 1
        source = self.directory / f'input-{self._names}.json'
        source.write_text(json.dumps(document))
        return subprocess.run(
            [sys.executable, '-m', MODULE, '--db', str(db or self.db),
             '--source', str(source)],
            capture_output=True, text=True, timeout=60, env=ENV)

    def ok(self, document, db=None):
        result = self.run_cli(document, db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def test_stdout_is_the_bare_result_object(self):
        self.assertEqual(self.ok(init()), {'first_month': '2025-01'})
        self.assertEqual(self.ok(replace(booking('A', '甲'))),
                         {'occurrence_count': 1})
        bill = self.ok(close('2025-01'))
        self.assertEqual(set(bill),
                         {'month', 'charges', 'adjustments', 'account_totals'})
        self.assertEqual(bill['charges'][0]['amount_cents'], 0)
        self.assertEqual(self.ok(show('2025-01')), bill)

    def test_bills_survive_across_processes(self):
        self.ok(init())
        self.ok(replace(booking('A', '甲', start='2025-01-06T09:00'),
                        booking('B', '乙', start='2025-01-06T10:00')))
        january = self.ok(close('2025-01'))
        # A completely separate process reopens the archived bill.
        self.assertEqual(self.ok(show('2025-01')), january)

    def test_duplicate_and_missing_fields_exit_two_with_stderr(self):
        self.ok(init())
        source = self.directory / 'dup.json'
        source.write_text('{"action":"close","month":"2025-01",'
                          '"month":"2025-02"}')
        result = subprocess.run(
            [sys.executable, '-m', MODULE, '--db', str(self.db),
             '--source', str(source)],
            capture_output=True, text=True, timeout=60, env=ENV)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('duplicate', result.stderr)
        self.assertEqual(self.ok(close('2025-01')),
                         {'month': '2025-01', 'charges': [],
                          'adjustments': [], 'account_totals': []})

    def test_nonfinite_numbers_are_rejected(self):
        self.ok(init())
        source = self.directory / 'nan.json'
        source.write_text('{"action":"init","timezone":"UTC",'
                          '"first_month":"2025-01","rooms":[{"room":"R",'
                          '"free_minutes":NaN,"unit_cents":1}]}')
        result = subprocess.run(
            [sys.executable, '-m', MODULE, '--db', str(self.db),
             '--source', str(source)],
            capture_output=True, text=True, timeout=60, env=ENV)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_missing_database_and_foreign_database_exit_two(self):
        missing = self.run_cli(close('2025-01'))
        self.assertEqual(missing.returncode, 2)
        self.assertIn('does not exist', missing.stderr)
        self.assertFalse(self.db.exists())
        foreign = self.directory / 'foreign.db'
        connection = sqlite3.connect(foreign)
        connection.execute('CREATE TABLE unrelated(value)')
        connection.execute("INSERT INTO unrelated VALUES('keep')")
        connection.commit()
        connection.close()
        result = self.run_cli(init(), db=foreign)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        check = sqlite3.connect(foreign)
        self.assertEqual(check.execute('SELECT * FROM unrelated').fetchall(),
                         [('keep',)])
        tables = check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(tables, [('unrelated',)])
        check.close()

    def test_symlink_database_and_source_alias_are_rejected(self):
        self.ok(init())
        link = self.directory / 'link.db'
        os.symlink(self.db, link)
        result = self.run_cli(close('2025-01'), db=link)
        self.assertEqual(result.returncode, 2)
        self.assertIn('symlink', result.stderr)
        # A source path that resolves to the database is refused.
        source = self.directory / 'alias.json'
        source.write_text(json.dumps(close('2025-01')))
        result = subprocess.run(
            [sys.executable, '-m', MODULE, '--db', str(source),
             '--source', str(source)],
            capture_output=True, text=True, timeout=60, env=ENV)
        self.assertEqual(result.returncode, 2)
        self.assertIn('differ', result.stderr)

    def test_invalid_action_and_month_exit_two(self):
        self.ok(init())
        for document in ({'action': 'nope'},
                         close('2025-13'),
                         close('0001-12'),
                         show('2025-02'),
                         replace(booking('A', '甲', room='NOPE'))):
            result = self.run_cli(document)
            self.assertEqual(result.returncode, 2, document)
            self.assertEqual(result.stdout, '')

    def test_source_digest_is_not_required_but_input_is_strict(self):
        # The bare result contract carries no digest, yet the source is still
        # parsed under the repository's strict JSON rules.
        self.ok(init())
        result = self.run_cli({'action': 'close', 'month': '2025-01',
                               'unexpected': True})
        self.assertEqual(result.returncode, 2)
        self.assertIn('fields', result.stderr)

    def test_multiple_replaces_then_one_close_bills_the_last_list(self):
        self.ok(init())
        self.ok(replace(booking('A', '甲', start='2025-01-06T09:00')))
        self.ok(replace(booking('B', '乙', start='2025-01-06T09:00')))
        self.ok(replace(booking('C', '丙', start='2025-01-06T09:00')))
        bill = self.ok(close('2025-01'))
        self.assertEqual([line['schedule'] for line in bill['charges']], ['C'])
        self.assertEqual(self.ok(close('2025-02'))['adjustments'], [])

    def test_replay_of_repository_fixtures_matches_the_documented_bills(self):
        fixtures = ROOT / 'fixtures' / 'billing'

        def run(name):
            return self.ok(json.loads((fixtures / name).read_text()))

        self.assertEqual(run('01_init.json'), {'first_month': '2025-01'})
        self.assertEqual(run('02_replace_original.json'),
                         {'occurrence_count': 2})
        january = run('03_close_january.json')
        self.assertEqual(
            [(line['account'], line['schedule'], line['amount_cents'])
             for line in january['charges']],
            [('乙', 'B', 120), ('甲', 'A', 0)])
        self.assertEqual(january['account_totals'],
                         [{'account': '乙', 'amount_cents': 120},
                          {'account': '甲', 'amount_cents': 0}])
        self.assertEqual(run('04_replace_swapped.json'),
                         {'occurrence_count': 2})
        self.assertEqual(run('05_close_january_retry.json'), january)
        february = run('06_close_february.json')
        self.assertEqual(february['charges'], [])
        self.assertEqual(
            [(line['account'], line['schedule'], line['amount_cents'])
             for line in february['adjustments']],
            [('丙', 'C', 120), ('乙', 'B', -120)])
        self.assertEqual(february['account_totals'],
                         [{'account': '丙', 'amount_cents': 120},
                          {'account': '乙', 'amount_cents': -120}])
        self.assertEqual(run('07a_close_march_quiet.json'), {
            'month': '2025-03', 'charges': [], 'adjustments': [],
            'account_totals': []})
        self.assertEqual(run('09_show_january.json'), january)
        self.assertEqual(run('10_show_february.json'), february)

    def test_revert_branch_reverses_differences_and_keeps_old_bills(self):
        fixtures = ROOT / 'fixtures' / 'billing'
        for name in ('01_init.json', '02_replace_original.json',
                     '03_close_january.json', '04_replace_swapped.json',
                     '05_close_january_retry.json', '06_close_february.json'):
            self.ok(json.loads((fixtures / name).read_text()))
        january = self.ok(show('2025-01'))
        february = self.ok(show('2025-02'))
        self.ok(json.loads(
            (fixtures / '07b_replace_original_again.json').read_text()))
        march = self.ok(json.loads(
            (fixtures / '08b_close_march_after_revert.json').read_text()))
        self.assertEqual(
            [(line['account'], line['schedule'], line['amount_cents'])
             for line in march['adjustments']],
            [('丙', 'C', -120), ('乙', 'B', 120)])
        self.assertEqual(self.ok(show('2025-01')), january)
        self.assertEqual(self.ok(show('2025-02')), february)


if __name__ == '__main__':
    unittest.main()
