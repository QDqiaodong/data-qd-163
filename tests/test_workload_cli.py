import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'calendar_expand.workload_cli'


def sample():
    return {
        'from_date': '2024-03-10',
        'through_date': '2024-03-10',
        'employees': [
            {'employee': 'alice', 'timezone': 'America/New_York',
             'daily_limit_minutes': 100},
        ],
        'schedules': [
            {'employee': 'alice', 'rule': {
                'schedule': 'a', 'timezone': 'America/New_York',
                'local_start': '2024-03-10T01:30', 'interval_weeks': 1,
                'weekdays': [7], 'until': '2024-03-10', 'duration_minutes': 120,
                'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier'}},
            {'employee': 'alice', 'rule': {
                'schedule': 'b', 'timezone': 'America/New_York',
                'local_start': '2024-03-10T02:30', 'interval_weeks': 1,
                'weekdays': [7], 'until': '2024-03-10', 'duration_minutes': 60,
                'exceptions': [], 'gap_policy': 'shift_forward',
                'fold_policy': 'earlier'}},
        ],
    }


class WorkloadCliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', MODULE, '--source', str(source),
             '--output', str(output)],
            capture_output=True, text=True, timeout=30)

    def test_success_writes_report_and_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(sample()))
            output = Path(temporary) / 'report.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(result.stdout))
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            days = report['result']['employees'][0]['days']
            self.assertEqual(days, [{'date': '2024-03-10', 'busy_seconds': 7200,
                                     'over_limit': True}])
            original = output.read_bytes()
            self.assertEqual(self.invoke(source, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_fixture_report_matches_contract_example(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            result = self.invoke(ROOT / 'fixtures/workload.json', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['result']['from_date'], '2024-03-10')
            self.assertEqual(report['result']['through_date'], '2024-03-10')
            employees = report['result']['employees']
            self.assertEqual(len(employees), 1)
            self.assertEqual(employees[0]['employee'], 'alice')
            self.assertEqual(employees[0]['timezone'], 'America/New_York')
            self.assertEqual(employees[0]['daily_limit_minutes'], 100)
            self.assertEqual(employees[0]['days'],
                             [{'date': '2024-03-10', 'busy_seconds': 7200,
                               'over_limit': True}])

    def test_invalid_inputs_exit_two_and_preserve_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'bad.json'
            output.write_text('previous report')
            for raw in ('{', '{"from_date":"2024-01-01","from_date":"2024-01-02",'
                             '"through_date":"2024-01-01","employees":[],'
                             '"schedules":[]}',
                        '{"from_date":"2024-01-01","through_date":"2024-01-01",'
                        '"employees":[],"schedules":[],"x":NaN}',
                        '{"from_date":"2024-01-01","through_date":"2024-01-01",'
                        '"employees":[],"schedules":[]} extra',
                        '[]'):
                with self.subTest(raw=raw):
                    source.write_text(raw)
                    result = self.invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(output.read_text(), 'previous report')
                    self.assertEqual(result.stdout, '')

    def test_semantically_invalid_input_exits_two(self):
        data = sample()
        data['employees'][0]['daily_limit_minutes'] = 1441
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(data))
            output = Path(temporary) / 'report.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_trailing_invalid_rule_exits_two_and_preserves_report(self):
        data = sample()
        data['schedules'].append(
            {'employee': 'alice', 'rule': {
                'schedule': 'broken', 'timezone': 'Mars/Olympus',
                'local_start': '2024-03-10T09:00', 'interval_weeks': 1,
                'weekdays': [7], 'until': '2024-03-10', 'duration_minutes': 30,
                'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier'}})
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(data))
            output = Path(temporary) / 'report.json'
            output.write_text('previous report')
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_text(), 'previous report')

    def test_source_output_collision_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(sample()))
            original = source.read_bytes()
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(source.read_bytes(), original)

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            output = Path(temporary) / 'report.json'
            output.symlink_to(target)
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(sample()))
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_missing_source_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            result = self.invoke(Path(temporary) / 'absent.json', output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_oversized_input_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'big.json'
            source.write_bytes(b' ' * 8_000_001)
            output = Path(temporary) / 'report.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertIn('8 MB', result.stderr)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
