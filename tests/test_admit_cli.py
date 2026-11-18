import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'calendar_expand.admit_cli'


def sample():
    return {
        'resources': [
            {'resource': 'digestive', 'capacity': 2},
            {'resource': 'surgery', 'capacity': 1},
        ],
        'requests': [
            {
                'rule': {
                    'schedule': 'triage', 'timezone': 'UTC',
                    'local_start': '2024-01-01T09:00', 'interval_weeks': 1,
                    'weekdays': [1], 'until': '2024-01-01', 'duration_minutes': 60,
                    'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier',
                },
                'priority': 5, 'mandatory': True,
                'demands': [{'resource': 'digestive', 'units': 1}],
            },
            {
                'rule': {
                    'schedule': 'scopy', 'timezone': 'UTC',
                    'local_start': '2024-01-01T09:30', 'interval_weeks': 1,
                    'weekdays': [1], 'until': '2024-01-01', 'duration_minutes': 60,
                    'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier',
                },
                'priority': 9, 'mandatory': False,
                'demands': [{'resource': 'digestive', 'units': 1},
                            {'resource': 'surgery', 'units': 1}],
            },
        ],
    }


class AdmitCliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', MODULE, '--source', str(source), '--output', str(output)],
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
            self.assertEqual(report['result']['accepted_ids'], ['scopy', 'triage'])
            self.assertEqual(report['result']['rejected_ids'], [])
            self.assertEqual(report['result']['total_priority'], 14)
            self.assertIsNone(report['result']['mandatory_conflict'])
            original = output.read_bytes()
            self.assertEqual(self.invoke(source, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_fixture_report_matches_declared_optimum(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            result = self.invoke(ROOT / 'fixtures/admission.json', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            result_json = json.loads(output.read_text())['result']
            self.assertEqual(result_json['accepted_ids'], ['scopy', 'triage'])
            self.assertEqual(result_json['rejected_ids'], ['motility', 'swallow'])
            self.assertEqual(result_json['total_priority'], 14)
            self.assertEqual([item['request_id'] for item in result_json['rejections']],
                             ['motility', 'swallow'])
            self.assertEqual(result_json['rejections'][1]['witness'], {
                'start_utc': '2024-03-04T14:30:00Z',
                'end_utc': '2024-03-04T15:00:00Z',
                'resource': 'digestive',
                'load': 3,
                'capacity': 2,
                'active_ids': ['scopy', 'swallow', 'triage'],
            })

    def test_business_infeasibility_exits_zero(self):
        data = sample()
        data['requests'][0]['mandatory'] = True
        data['requests'][1]['priority'] = 9
        data['requests'][1]['rule']['local_start'] = '2024-01-01T09:10'
        data['requests'].append({
            'rule': {
                'schedule': 'second', 'timezone': 'UTC',
                'local_start': '2024-01-01T09:20', 'interval_weeks': 1,
                'weekdays': [1], 'until': '2024-01-01', 'duration_minutes': 60,
                'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier',
            },
            'priority': 4, 'mandatory': True,
            'demands': [{'resource': 'digestive', 'units': 2}],
        })
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(data))
            output = Path(temporary) / 'report.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output.read_text())['result']
            self.assertFalse(payload['feasible'])
            self.assertEqual(payload['accepted_ids'], [])
            self.assertEqual(payload['rejected_ids'], ['scopy', 'second', 'triage'])
            self.assertIsNone(payload['total_priority'])
            self.assertEqual(payload['rejections'], [])
            self.assertEqual(payload['mandatory_conflict']['resource'], 'digestive')
            self.assertEqual(payload['mandatory_conflict']['load'], 3)
            self.assertEqual(payload['mandatory_conflict']['capacity'], 2)

    def test_invalid_inputs_exit_two_and_preserve_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'bad.json'
            output.write_text('previous report')
            for raw in ('{', '{"resources":[],"resources":[],"requests":[]}',
                        '{"resources":[],"requests":[],"x":NaN}',
                        '{"resources":[],"requests":[]} extra', '[]'):
                with self.subTest(raw=raw):
                    source.write_text(raw)
                    result = self.invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(output.read_text(), 'previous report')
                    self.assertEqual(result.stdout, '')

    def test_semantically_invalid_input_exits_two(self):
        data = sample()
        data['requests'][1]['priority'] = 0
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(data))
            output = Path(temporary) / 'report.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

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
            self.assertEqual(self.invoke(source, output).returncode, 2)
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
