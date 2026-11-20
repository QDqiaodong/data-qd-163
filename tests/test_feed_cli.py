import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'calendar_expand.feed_cli'


def rule(schedule, local_start='2025-01-15T09:00', duration=60):
    return {'schedule': schedule, 'timezone': 'UTC',
            'local_start': local_start, 'interval_weeks': 1, 'weekdays': [3],
            'until': '2025-01-15', 'duration_minutes': duration,
            'exceptions': [], 'gap_policy': 'skip', 'fold_policy': 'earlier'}


def snapshot(alpha, beta, title='内部主题', local_start='2025-01-15T09:00',
             schedule='ops-weekly-417'):
    return {'groups': [{'group': 'alpha', 'access': alpha},
                       {'group': 'beta', 'access': beta}],
            'activities': [{'rule': rule(schedule, local_start),
                            'alias': 'meeting', 'title': title,
                            'groups': ['alpha', 'beta']}]}


def source(current, previous=None):
    return {'recipient': 'partner', 'current': current, 'previous': previous}


def flatten(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from flatten(item)
    elif isinstance(value, list):
        for item in value:
            yield from flatten(item)
    else:
        yield value


class FeedCliTests(unittest.TestCase):
    def invoke(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', MODULE, *arguments],
            capture_output=True, text=True, timeout=30)

    def publish(self, source_path, output_path):
        return self.invoke('publish', '--source', str(source_path),
                           '--output', str(output_path))

    def apply(self, view_path, delta_path):
        return self.invoke('apply', '--view', str(view_path),
                           '--delta', str(delta_path))

    def write(self, directory, name, document):
        path = Path(directory) / name
        path.write_text(json.dumps(document))
        return path

    def test_publish_full_view_success_and_no_internal_leak(self):
        with tempfile.TemporaryDirectory() as temporary:
            current = {'groups': [{'group': 'alpha', 'access': 'busy'},
                                  {'group': 'beta', 'access': 'none'}],
                       'activities': [
                           {'rule': rule('internal-id-1'), 'alias': 'meeting',
                            'title': '内部主题', 'groups': ['alpha']},
                           {'rule': rule('internal-id-2',
                                         local_start='2025-01-15T12:00'),
                            'alias': 'secret', 'title': '保密议题',
                            'groups': ['beta']}]}
            source_path = self.write(temporary, 'source.json', source(current))
            output = Path(temporary) / 'view.json'
            result = self.publish(source_path, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(result.stdout))
            self.assertEqual(report['rows'], [
                {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z',
                 'end_utc': '2025-01-15T10:00:00Z'}])
            leaked = set(flatten(report))
            for internal in ('alpha', 'beta', 'internal-id-1', 'internal-id-2',
                             '内部主题', '保密议题', 'secret', 'source_sha256'):
                self.assertNotIn(internal, leaked)
            original = output.read_bytes()
            self.assertEqual(self.publish(source_path, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_publish_delta_and_apply_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            steps = [snapshot('detail', 'busy'), snapshot('none', 'busy'),
                     snapshot('none', 'none')]
            view_path = Path(temporary) / 'view.json'
            full = self.write(temporary, 'full.json',
                              source(steps[0]))
            self.assertEqual(self.publish(full, view_path).returncode, 0)
            for index in (1, 2):
                delta_path = Path(temporary) / ('delta%d.json' % index)
                delta_source = self.write(
                    temporary, 'delta-source%d.json' % index,
                    source(steps[index], previous=steps[index - 1]))
                result = self.publish(delta_source, delta_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.apply(view_path, delta_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 json.loads(view_path.read_text()))
            self.assertEqual(json.loads(view_path.read_text())['rows'], [])
            # the applied view equals a direct full publish of the same target
            direct = Path(temporary) / 'direct.json'
            target = self.write(temporary, 'target.json', source(steps[2]))
            self.assertEqual(self.publish(target, direct).returncode, 0)
            self.assertEqual(view_path.read_bytes(), direct.read_bytes())

    def test_apply_failures_preserve_view_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            view_path = Path(temporary) / 'view.json'
            full = self.write(temporary, 'full.json',
                              source(snapshot('detail', 'busy')))
            self.assertEqual(self.publish(full, view_path).returncode, 0)
            original = view_path.read_bytes()
            view = json.loads(original)
            # build a real delta through publish
            delta_path = Path(temporary) / 'delta.json'
            delta_source = self.write(
                temporary, 'delta-source.json',
                source(snapshot('none', 'busy'),
                       previous=snapshot('detail', 'busy')))
            self.assertEqual(self.publish(delta_source, delta_path).returncode, 0)
            delta = json.loads(delta_path.read_text())
            tampered = {
                'recipient': dict(delta, recipient='someone-else'),
                'base': dict(delta, base_sha256='0' * 64),
                'target': dict(delta, target_sha256='0' * 64),
                'withdraw-ghost': dict(delta, upserts=[], withdrawals=[
                    {'alias': 'ghost',
                     'start_utc': '2025-01-15T09:00:00Z'}]),
                'restated': dict(delta, upserts=view['rows'], withdrawals=[]),
            }
            for name, document in tampered.items():
                with self.subTest(name=name):
                    bad = self.write(temporary, 'bad-delta.json', document)
                    result = self.apply(view_path, bad)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(view_path.read_bytes(), original)
            # a corrupt view is rejected as well and left untouched
            view_path.write_text(json.dumps(dict(view, view_sha256='0' * 64)))
            corrupt_bytes = view_path.read_bytes()
            result = self.apply(view_path, delta_path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(view_path.read_bytes(), corrupt_bytes)

    def test_publish_invalid_inputs_exit_two_and_preserve_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'view.json'
            output.write_text('previous view')
            bad = Path(temporary) / 'bad.json'
            for raw in ('{', '{"recipient":"x","recipient":"y","current":{},'
                             '"previous":null}',
                        '{"recipient":"x","current":{},"previous":null,"x":NaN}',
                        '[]',
                        '{"recipient":"partner","current":{"groups":[],'
                        '"activities":[]},"previous":null} extra'):
                with self.subTest(raw=raw):
                    bad.write_text(raw)
                    result = self.publish(bad, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(output.read_text(), 'previous view')

    def test_publish_source_output_collision_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = self.write(temporary, 'source.json',
                                     source(snapshot('detail', 'busy')))
            original = source_path.read_bytes()
            self.assertEqual(self.publish(source_path, source_path).returncode, 2)
            self.assertEqual(source_path.read_bytes(), original)

    def test_apply_view_delta_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write(temporary, 'same.json', source(snapshot('none', 'none')))
            self.assertEqual(self.apply(path, path).returncode, 2)

    def test_symlink_outputs_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            output = Path(temporary) / 'view.json'
            output.symlink_to(target)
            source_path = self.write(temporary, 'source.json',
                                     source(snapshot('detail', 'busy')))
            self.assertEqual(self.publish(source_path, output).returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')
            # apply refuses to replace a symlinked view as well
            real_view = Path(temporary) / 'real-view.json'
            self.assertEqual(self.publish(source_path, real_view).returncode, 0)
            linked = Path(temporary) / 'linked-view.json'
            linked.symlink_to(real_view)
            delta_source = self.write(
                temporary, 'delta-source.json',
                source(snapshot('none', 'busy'),
                       previous=snapshot('detail', 'busy')))
            delta_path = Path(temporary) / 'delta.json'
            self.assertEqual(self.publish(delta_source, delta_path).returncode, 0)
            original = real_view.read_bytes()
            self.assertEqual(self.apply(linked, delta_path).returncode, 2)
            self.assertEqual(real_view.read_bytes(), original)

    def test_missing_files_exit_two(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'view.json'
            self.assertEqual(
                self.publish(Path(temporary) / 'absent.json', output).returncode, 2)
            self.assertFalse(output.exists())
            self.assertEqual(
                self.apply(Path(temporary) / 'absent-view.json',
                           Path(temporary) / 'absent-delta.json').returncode, 2)

    def test_oversized_input_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            big = Path(temporary) / 'big.json'
            big.write_bytes(b' ' * 8_000_001)
            output = Path(temporary) / 'view.json'
            result = self.publish(big, output)
            self.assertEqual(result.returncode, 2)
            self.assertIn('8 MB', result.stderr)
            self.assertFalse(output.exists())

    def test_unknown_or_missing_subcommand_exits_two(self):
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.invoke('inspect').returncode, 2)


if __name__ == '__main__':
    unittest.main()
