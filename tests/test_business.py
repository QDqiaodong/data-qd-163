import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from calendar_expand.model import analyze, resolve

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return json.loads((ROOT / 'fixtures/input.json').read_text())


class CalendarExpandTests(unittest.TestCase):
    def test_spring_gap_skipped(self):
        data = fixture()
        report = analyze(data)
        self.assertEqual(report['skipped'], [{'date': '2024-03-10', 'reason': 'nonexistent_local_time'}])

    def test_spring_gap_shift_forward(self):
        data = fixture()
        data['gap_policy'] = 'shift_forward'
        report = analyze(data)
        occurrence = next(x for x in report['occurrences'] if x['start_utc'].startswith('2024-03-10'))
        self.assertEqual(occurrence['local_start'], '2024-03-10T03:00')

    def test_fall_fold_earlier_and_later(self):
        zone = ZoneInfo('America/New_York')
        local = datetime.fromisoformat('2024-11-03T01:30')
        earlier, _ = resolve(local, zone, 'skip', 'earlier')
        later, _ = resolve(local, zone, 'skip', 'later')
        self.assertEqual((later - earlier).total_seconds(), 3600)

    def test_exceptions_remove_date(self):
        data = fixture()
        data['exceptions'] = ['2024-03-17']
        self.assertNotIn('2024-03-17', [x['local_start'][:10] for x in analyze(data)['occurrences']])

    def test_two_week_interval(self):
        data = fixture()
        data.update(interval_weeks=2, until='2024-03-31')
        self.assertEqual([x['local_start'][:10] for x in analyze(data)['occurrences']], ['2024-03-03', '2024-03-17', '2024-03-31'])

    def test_elapsed_duration_uses_utc(self):
        data = fixture()
        data.update(local_start='2024-03-09T23:30', weekdays=[6], until='2024-03-09')
        data['duration_minutes'] = 120
        occurrence = analyze(data)['occurrences'][0]
        start = datetime.fromisoformat(occurrence['start_utc'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(occurrence['end_utc'].replace('Z', '+00:00'))
        self.assertEqual((end - start).total_seconds(), 7200)

    def test_duplicate_weekday_rejected(self):
        data = fixture()
        data['weekdays'] = [7, 7]
        with self.assertRaisesRegex(ValueError, 'weekdays'):
            analyze(data)

    def test_unknown_timezone_rejected(self):
        data = fixture()
        data['timezone'] = 'Mars/Olympus'
        with self.assertRaisesRegex(ValueError, 'timezone'):
            analyze(data)

    def test_aware_local_start_rejected(self):
        data = fixture()
        data['local_start'] = '2024-03-03T02:30-05:00'
        with self.assertRaisesRegex(ValueError, 'naive'):
            analyze(data)

    def test_expansion_bound(self):
        data = fixture()
        data['until'] = '2040-01-01'
        with self.assertRaisesRegex(ValueError, 'bounded'):
            analyze(data)
