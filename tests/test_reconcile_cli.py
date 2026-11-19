import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'calendar_expand.reconcile_cli'


def rule(schedule, start='2024-11-03T01:30', tz='America/New_York',
         fold='earlier', until='2024-11-03', weekdays=None, gap='skip'):
    return {
        'schedule': schedule, 'timezone': tz, 'local_start': start,
        'interval_weeks': 1, 'weekdays': weekdays or [7], 'until': until,
        'duration_minutes': 60, 'exceptions': [],
        'gap_policy': gap, 'fold_policy': fold,
    }


def observation(observation_id, tz='America/New_York',
                start='2024-11-03T01:30', eligible=('s-early', 's-later')):
    return {'observation_id': observation_id, 'timezone': tz,
            'local_start': start, 'eligible_schedules': list(eligible)}


def sample():
    return {
        'rules': [rule('s-early', fold='earlier'),
                  rule('s-later', fold='later')],
        'observations': [
            observation('obs-ny'),
            observation('obs-utc', tz='UTC', start='2024-11-03T05:30'),
        ],
        'tolerance_minutes': 0,
    }


class ReconcileCliTests(unittest.TestCase):
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
            payload = report['result']
            self.assertEqual(set(payload),
                             {'matched_count', 'total_deviation_minutes',
                              'observations', 'occurrences'})
            self.assertEqual(payload['matched_count'], 2)
            self.assertEqual(payload['total_deviation_minutes'], 0)
            original = output.read_bytes()
            self.assertEqual(self.invoke(source, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_fixture_report_is_published_and_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures/reconcile.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            payload = report['result']
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(payload['matched_count'], 2)
            self.assertEqual(payload['total_deviation_minutes'], 0)
            # The fold record is forced onto the later instance by the second
            # record; the two views report identical triple facts.
            triples = {
                (s['schedule'], s['start_utc'], s['observed_utc'],
                 s['delta_minutes'])
                for item in payload['observations'] for s in item['supports']}
            reverse_triples = {
                (item['schedule'], item['start_utc'], s['observed_utc'],
                 s['delta_minutes'])
                for item in payload['occurrences'] for s in item['supports']}
            self.assertEqual(triples, reverse_triples)
            self.assertEqual(triples, {
                ('s-early', '2024-11-03T05:30:00Z',
                 '2024-11-03T05:30:00Z', 0),
                ('s-later', '2024-11-03T06:30:00Z',
                 '2024-11-03T06:30:00Z', 0),
            })

    def test_invalid_inputs_exit_two_and_preserve_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'bad.json'
            output.write_text('previous report')
            valid = sample()
            broken_documents = [
                '{',
                '{"rules":[],"observations":[],"tolerance_minutes":0,"x":1}',
                json.dumps({**valid, 'tolerance_minutes': -1}),
                json.dumps({**valid, 'tolerance_minutes': True}),
                json.dumps({**valid,
                            'observations': [{**valid['observations'][0],
                                              'timezone': 'Mars/Olympus'}]}),
                json.dumps({**valid,
                            'observations': [{**valid['observations'][0],
                                              'local_start': '2024-11-03T01:30:00'}]}),
                json.dumps({**valid,
                            'observations': [{**valid['observations'][0],
                                              'local_start': '2024-11-03T01:30Z'}]}),
                json.dumps({'rules': valid['rules'], 'observations': [],
                            'tolerance_minutes': 0,
                            'extra': []}),
            ]
            for raw in broken_documents:
                with self.subTest(raw=raw[:40]):
                    source.write_text(raw)
                    result = self.invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(output.read_text(), 'previous report')

    def test_historical_second_offsets_rejected_without_touching_report(self):
        document = {
            'rules': [rule('paris', start='1900-01-01T09:00',
                           tz='Europe/Paris', until='1900-01-01',
                           weekdays=[1])],
            'observations': [], 'tolerance_minutes': 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(document))
            output = Path(temporary) / 'report.json'
            output.write_text('previous report')
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertIn('minute precise', result.stderr)
            self.assertEqual(output.read_text(), 'previous report')

    def test_gap_record_is_a_successful_report(self):
        document = {
            'rules': [rule('a', start='2024-03-10T09:00', tz='UTC',
                           until='2024-03-10', weekdays=[7])],
            'observations': [
                observation('gap', tz='America/New_York',
                            start='2024-03-10T02:30', eligible=('a',))],
            'tolerance_minutes': 30,
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_text(json.dumps(document))
            output = Path(temporary) / 'report.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output.read_text())['result']
            self.assertEqual(payload['matched_count'], 0)
            self.assertEqual(payload['total_deviation_minutes'], 0)
            self.assertEqual(payload['observations'][0]['candidates_utc'], [])
            self.assertTrue(payload['observations'][0]['can_be_unmatched'])

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
