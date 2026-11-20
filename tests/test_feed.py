import hashlib
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from calendar_expand import feed


def rule(schedule, local_start='2025-01-15T09:00', timezone='UTC', duration=60,
         until=None, weekdays=(3,), interval=1, exceptions=(), gap='skip',
         fold='earlier'):
    return {'schedule': schedule, 'timezone': timezone,
            'local_start': local_start, 'interval_weeks': interval,
            'weekdays': list(weekdays), 'until': until or local_start[:10],
            'duration_minutes': duration, 'exceptions': list(exceptions),
            'gap_policy': gap, 'fold_policy': fold}


def activity(alias, title='标题', groups=('alpha',), schedule=None, **kwargs):
    return {'rule': rule(schedule or alias + '-rule', **kwargs),
            'alias': alias, 'title': title, 'groups': list(groups)}


def snapshot(access, activities):
    return {'groups': [{'group': name, 'access': level}
                       for name, level in access.items()],
            'activities': activities}


def source(current, previous=None, recipient='partner'):
    return {'recipient': recipient, 'current': current, 'previous': previous}


def fingerprint(recipient, row_list):
    body = {'schema_version': 1, 'recipient': recipient, 'rows': row_list}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True,
                           separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


class PublishTests(unittest.TestCase):
    def test_full_view_levels_and_exact_fields(self):
        current = snapshot(
            {'alpha': 'detail', 'beta': 'busy', 'gamma': 'none'},
            [activity('detail-row', title='公开主题', groups=('alpha',)),
             activity('busy-row', title='内部主题', groups=('beta',)),
             activity('hidden-row', groups=('gamma',)),
             activity('ungrouped-row', groups=())])
        view = feed.publish(source(current))
        self.assertEqual(set(view), {'schema_version', 'recipient', 'rows',
                                     'view_sha256'})
        self.assertEqual(view['schema_version'], 1)
        self.assertEqual(view['recipient'], 'partner')
        self.assertEqual(view['rows'], [
            {'alias': 'busy-row', 'start_utc': '2025-01-15T09:00:00Z',
             'end_utc': '2025-01-15T10:00:00Z'},
            {'alias': 'detail-row', 'start_utc': '2025-01-15T09:00:00Z',
             'end_utc': '2025-01-15T10:00:00Z', 'title': '公开主题'}])
        self.assertEqual(view['view_sha256'],
                         fingerprint('partner', view['rows']))

    def test_highest_access_wins_and_rows_sorted_by_public_key(self):
        current = snapshot(
            {'alpha': 'none', 'beta': 'busy', 'gamma': 'detail'},
            [activity('zeta', groups=('gamma', 'beta')),
             activity('alpha-row', groups=('alpha', 'beta'),
                      local_start='2025-01-15T08:00')])
        view = feed.publish(source(current))
        self.assertEqual([row['alias'] for row in view['rows']],
                         ['alpha-row', 'zeta'])
        self.assertNotIn('title', view['rows'][0])
        self.assertEqual(view['rows'][1]['title'], '标题')

    def test_fingerprint_uses_contract_serialization(self):
        current = snapshot({'alpha': 'detail'}, [activity('meeting')])
        view = feed.publish(source(current))
        body = {'schema_version': 1, 'recipient': 'partner',
                'rows': view['rows']}
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=True,
                               separators=(',', ':'))
        self.assertNotIn('\n', canonical)
        self.assertNotIn('view_sha256', canonical)
        self.assertEqual(view['view_sha256'],
                         hashlib.sha256(canonical.encode('utf-8')).hexdigest())

    def test_same_rule_duplicate_public_key_emits_one_row(self):
        instant = datetime(2025, 1, 15, 9, 0, tzinfo=timezone.utc)
        end = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
        planned = {'schedule': 'dup-rule', 'timezone': 'UTC',
                   'items': [{'nominal_date': date(2025, 1, 15),
                              'instant': instant, 'actual': None, 'end': end},
                             {'nominal_date': date(2025, 1, 16),
                              'instant': instant, 'actual': None, 'end': end}],
                   'skipped': []}
        current = snapshot({'alpha': 'detail'}, [activity('meeting')])
        with mock.patch('calendar_expand.feed.plan', return_value=planned):
            view = feed.publish(source(current))
        self.assertEqual(view['rows'], [
            {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z',
             'end_utc': '2025-01-15T10:00:00Z', 'title': '标题'}])

    def test_invisible_changes_keep_view_and_empty_delta(self):
        base = snapshot(
            {'alpha': 'busy', 'beta': 'none'},
            [activity('busy-row', title='旧标题', groups=('alpha',)),
             activity('hidden-row', groups=('beta',),
                      local_start='2025-01-15T11:00')])
        changed = snapshot(
            {'alpha': 'busy', 'beta': 'none'},
            [activity('busy-row', title='新标题', groups=('alpha',),
                      schedule='renamed-rule'),
             activity('hidden-row', groups=('beta',),
                      local_start='2025-01-16T12:30', duration=15,
                      schedule='other-rule')])
        old_view = feed.publish(source(base))
        new_view = feed.publish(source(changed))
        self.assertEqual(old_view, new_view)
        delta = feed.publish(source(changed, previous=base))
        self.assertEqual(delta['upserts'], [])
        self.assertEqual(delta['withdrawals'], [])
        self.assertEqual(delta['base_sha256'], delta['target_sha256'])
        self.assertEqual(delta['base_sha256'], old_view['view_sha256'])

    def test_group_membership_change_triggers_access_change(self):
        access = {'alpha': 'detail', 'beta': 'busy'}
        before = snapshot(access, [activity('meeting', groups=('alpha',))])
        after = snapshot(access, [activity('meeting', groups=('beta',))])
        delta = feed.publish(source(after, previous=before))
        self.assertEqual(delta['withdrawals'], [])
        self.assertEqual(delta['upserts'], [
            {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z',
             'end_utc': '2025-01-15T10:00:00Z'}])

    def test_reschedule_withdraws_old_key_and_adds_new_key(self):
        access = {'alpha': 'detail'}
        before = snapshot(access, [activity('meeting')])
        after = snapshot(access, [activity('meeting',
                                           local_start='2025-01-15T11:00')])
        delta = feed.publish(source(after, previous=before))
        self.assertEqual(delta['withdrawals'], [
            {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z'}])
        self.assertEqual(delta['upserts'], [
            {'alias': 'meeting', 'start_utc': '2025-01-15T11:00:00Z',
             'end_utc': '2025-01-15T12:00:00Z', 'title': '标题'}])

    def test_new_york_autumn_both_folds(self):
        current = snapshot(
            {'alpha': 'detail'},
            [activity('fold-early', timezone='America/New_York',
                      local_start='2024-11-03T01:30', duration=30,
                      weekdays=(7,), fold='earlier'),
             activity('fold-late', timezone='America/New_York',
                      local_start='2024-11-03T01:30', duration=30,
                      weekdays=(7,), fold='later')])
        view = feed.publish(source(current))
        self.assertEqual(view['rows'], [
            {'alias': 'fold-early', 'start_utc': '2024-11-03T05:30:00Z',
             'end_utc': '2024-11-03T06:00:00Z', 'title': '标题'},
            {'alias': 'fold-late', 'start_utc': '2024-11-03T06:30:00Z',
             'end_utc': '2024-11-03T07:00:00Z', 'title': '标题'}])

    def test_paris_historical_seconds_offset_preserved(self):
        current = snapshot(
            {'alpha': 'detail'},
            [activity('paris', timezone='Europe/Paris',
                      local_start='1900-01-01T09:00', duration=30,
                      weekdays=(1,))])
        view = feed.publish(source(current))
        self.assertEqual(view['rows'], [
            {'alias': 'paris', 'start_utc': '1900-01-01T08:50:39Z',
             'end_utc': '1900-01-01T09:20:39Z', 'title': '标题'}])

    def test_gap_fold_and_exceptions_semantics_kept(self):
        current = snapshot(
            {'alpha': 'busy'},
            [activity('gap-skip', timezone='America/New_York',
                      local_start='2024-03-10T02:30', duration=30,
                      weekdays=(7,), gap='skip'),
             activity('gap-shift', timezone='America/New_York',
                      local_start='2024-03-10T02:30', duration=30,
                      weekdays=(7,), gap='shift_forward'),
             activity('excepted', timezone='UTC',
                      local_start='2025-01-15T09:00', weekdays=(3,),
                      until='2025-01-22', exceptions=('2025-01-15',))])
        view = feed.publish(source(current))
        self.assertEqual(view['rows'], [
            {'alias': 'excepted', 'start_utc': '2025-01-22T09:00:00Z',
             'end_utc': '2025-01-22T10:00:00Z'},
            {'alias': 'gap-shift', 'start_utc': '2024-03-10T07:00:00Z',
             'end_utc': '2024-03-10T07:30:00Z'}])

    def test_empty_snapshot_and_empty_expansion(self):
        empty = snapshot({'alpha': 'detail'}, [])
        exhausted = snapshot({'alpha': 'detail'}, [
            activity('excepted', until='2025-01-22',
                     exceptions=('2025-01-15', '2025-01-22'))])
        for current in (empty, exhausted):
            with self.subTest(current=current):
                view = feed.publish(source(current))
                self.assertEqual(view['rows'], [])
        delta = feed.publish(source(exhausted, previous=empty))
        self.assertEqual(delta['upserts'], [])
        self.assertEqual(delta['withdrawals'], [])
        self.assertEqual(delta['base_sha256'], delta['target_sha256'])

    def test_source_array_order_does_not_change_output(self):
        access = {'alpha': 'detail', 'beta': 'busy'}
        activities = [activity('one', groups=('alpha',)),
                      activity('two', groups=('beta',),
                               local_start='2025-01-15T11:00')]
        forward = snapshot(access, activities)
        backward = {'groups': list(reversed(forward['groups'])),
                    'activities': list(reversed(activities))}
        self.assertEqual(feed.publish(source(forward)),
                         feed.publish(source(backward)))
        other = snapshot(access, [activity('one', groups=('alpha',))])
        self.assertEqual(feed.publish(source(other, previous=forward)),
                         feed.publish(source(other, previous=backward)))

    def test_trailing_invalid_hidden_rule_rejects_whole_source(self):
        current = snapshot(
            {'alpha': 'detail', 'beta': 'none'},
            [activity('visible', groups=('alpha',)),
             activity('hidden-broken', groups=('beta',),
                      timezone='Mars/Olympus')])
        with self.assertRaises(ValueError):
            feed.publish(source(current))

    def test_snapshot_validation_boundaries(self):
        bad_sources = []
        # group, schedule and alias uniqueness; group reference integrity
        bad_sources.append({'groups': [{'group': 'alpha', 'access': 'busy'},
                                       {'group': 'alpha', 'access': 'none'}],
                            'activities': []})
        bad_sources.append(snapshot({'alpha': 'detail'},
                                    [activity('one'), activity('one')]))
        two = [activity('one'), activity('two')]
        two[1]['rule']['schedule'] = two[0]['rule']['schedule']
        bad_sources.append(snapshot({'alpha': 'detail'}, two))
        ref_twice = activity('one')
        ref_twice['groups'] = ['alpha', 'alpha']
        bad_sources.append(snapshot({'alpha': 'detail'}, [ref_twice]))
        bad_sources.append(snapshot({'alpha': 'detail'},
                                    [activity('one', groups=('ghost',))]))
        bad_sources.append({'groups': [{'group': 'alpha', 'access': 'owner'}],
                            'activities': []})
        bad_sources.append({'groups': [{'group': 'alpha', 'access': True}],
                            'activities': []})
        # contract strings: ident rules plus Unicode scalar values
        bad_sources.append(snapshot({'alpha': 'detail'}, [activity('  ')]))
        bad_sources.append(snapshot({'alpha': 'detail'},
                                    [activity('bad\ud800')]))
        bad_sources.append(snapshot({'alpha': 'detail'},
                                    [activity('ok', title='bad\udfff')]))
        bad_sources.append(snapshot({'alpha': 'detail'},
                                    [activity('one', title='')]))
        bad_sources.append(snapshot({'alpha': 'detail'},
                                    [activity('one', title=' padded ')]))
        surrogate_rule = activity('one')
        surrogate_rule['rule']['schedule'] = 'bad\ud800-rule'
        bad_sources.append(snapshot({'alpha': 'detail'}, [surrogate_rule]))
        # limits: 64 groups, 64 rules, 2048 pre-merge instances per snapshot
        bad_sources.append({'groups': [{'group': 'g%02d' % i, 'access': 'busy'}
                                       for i in range(65)],
                            'activities': []})
        bad_sources.append(snapshot({'alpha': 'detail'},
                                    [activity('a%02d' % i) for i in range(65)]))
        bad_sources.append(snapshot({'alpha': 'none'}, [
            activity('heavy', until='2030-08-25',
                     weekdays=(1, 2, 3, 4, 5, 6, 7))]))
        for current in bad_sources:
            with self.subTest(current=current):
                with self.assertRaises(ValueError):
                    feed.publish(source(current))
        # the heaviest legal snapshot: 64 groups, 64 rules, 2048 instances
        access = {'g%02d' % i: 'none' for i in range(64)}
        legal = snapshot(access, [
            activity('r%02d' % i, groups=(), until='2025-03-18',
                     weekdays=(1, 2, 3, 4, 5, 6, 7)) for i in range(32)])
        view = feed.publish(source(legal))
        self.assertEqual(view['rows'], [])

    def test_publish_source_shape(self):
        for bad in ({}, [],
                    {'recipient': 'partner', 'current': {}, 'previous': None,
                     'extra': 1},
                    {'recipient': '', 'current': {}, 'previous': None},
                    {'recipient': 'bad\ud800', 'current': {}, 'previous': None},
                    {'recipient': 'partner', 'current': [], 'previous': None},
                    {'recipient': 'partner', 'current': {'groups': []},
                     'previous': None},
                    {'recipient': 'partner',
                     'current': {'groups': [], 'activities': []},
                     'previous': 'yesterday'}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    feed.publish(bad)


class AcceptanceFlowTests(unittest.TestCase):
    """The contracted two-group revocation sequence for alias ``meeting``."""

    def setUp(self):
        self.snapshots = [
            {'groups': [{'group': 'alpha', 'access': alpha},
                        {'group': 'beta', 'access': beta}],
             'activities': [{'rule': rule('ops-weekly-417'),
                             'alias': 'meeting', 'title': '内部主题',
                             'groups': ['alpha', 'beta']}]}
            for alpha, beta in (('detail', 'busy'), ('none', 'busy'),
                                ('none', 'none'))]

    def test_revoke_detail_then_busy(self):
        view = feed.publish(source(self.snapshots[0]))
        self.assertEqual(view['rows'], [
            {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z',
             'end_utc': '2025-01-15T10:00:00Z', 'title': '内部主题'}])
        first = feed.publish(source(self.snapshots[1],
                                    previous=self.snapshots[0]))
        self.assertEqual(first['base_sha256'], view['view_sha256'])
        self.assertEqual(first['withdrawals'], [])
        self.assertEqual(first['upserts'], [
            {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z',
             'end_utc': '2025-01-15T10:00:00Z'}])
        busy_view = feed.apply(view, first)
        self.assertEqual(busy_view['rows'], first['upserts'])
        self.assertEqual(busy_view['view_sha256'], first['target_sha256'])
        second = feed.publish(source(self.snapshots[2],
                                     previous=self.snapshots[1]))
        self.assertEqual(second['upserts'], [])
        self.assertEqual(second['withdrawals'], [
            {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z'}])
        final = feed.apply(busy_view, second)
        self.assertEqual(final['rows'], [])
        self.assertEqual(final['view_sha256'], second['target_sha256'])

    def test_applied_view_equals_direct_publish_regardless_of_source_order(self):
        view = feed.publish(source(self.snapshots[0]))
        for index in (1, 2):
            delta = feed.publish(source(self.snapshots[index],
                                        previous=self.snapshots[index - 1]))
            view = feed.apply(view, delta)
            shuffled = {'groups': list(reversed(self.snapshots[index]['groups'])),
                        'activities': self.snapshots[index]['activities']}
            self.assertEqual(view, feed.publish(source(shuffled)))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.current = snapshot(
            {'alpha': 'detail', 'beta': 'busy'},
            [activity('meeting', title='内部主题', groups=('alpha',)),
             activity('standup', title='站会', groups=('beta',),
                      local_start='2025-01-15T11:00', duration=30)])
        self.view = feed.publish(source(self.current))

    def delta(self, **overrides):
        document = {'schema_version': 1, 'recipient': 'partner',
                    'base_sha256': self.view['view_sha256'],
                    'target_sha256': '0' * 64,
                    'upserts': [], 'withdrawals': []}
        document.update(overrides)
        return document

    def test_view_shape_rejections(self):
        row = self.view['rows'][0]
        bad_views = [
            None, [], 'view',
            dict(self.view, extra=1),
            {key: value for key, value in self.view.items()
             if key != 'view_sha256'},
            dict(self.view, schema_version=True),
            dict(self.view, schema_version='1'),
            dict(self.view, schema_version=2),
            dict(self.view, recipient=''),
            dict(self.view, view_sha256='0' * 63),
            dict(self.view, view_sha256='0' * 64),
            dict(self.view, view_sha256='A' * 64),
            dict(self.view, rows=list(reversed(self.view['rows']))),
            dict(self.view, rows=self.view['rows'] + self.view['rows'][:1]),
            dict(self.view, rows=[dict(row, end_utc=row['start_utc'])]),
            dict(self.view, rows=[dict(row, end_utc='2025-01-15T08:00:00Z')]),
            dict(self.view, rows=[dict(row, start_utc='2025-01-15T09:00Z')]),
            dict(self.view, rows=[dict(row, start_utc='2025-01-15T09:00:00+00:00')]),
            dict(self.view, rows=[dict(row, start_utc='2025-13-15T09:00:00Z')]),
            dict(self.view, rows=[dict(row, start_utc='2025-01-15T09:00:60Z')]),
            dict(self.view, rows=[dict(row, unknown=1)]),
            dict(self.view, rows=[{key: value for key, value in row.items()
                                  if key != 'end_utc'}]),
            dict(self.view, rows=[dict(row, title='')]),
            dict(self.view, rows=[dict(row, title='bad\ud800')]),
        ]
        for bad in bad_views:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    feed.apply(bad, self.delta())

    def test_delta_shape_rejections(self):
        row = self.view['rows'][0]
        key = {'alias': row['alias'], 'start_utc': row['start_utc']}
        bad_deltas = [
            None, [], 'delta',
            self.delta(extra=1),
            self.delta(schema_version=1.0),
            self.delta(recipient=''),
            self.delta(base_sha256='0' * 63),
            self.delta(target_sha256='F' * 64),
            self.delta(upserts=[row, row]),
            self.delta(upserts=[row], withdrawals=[key]),
            self.delta(withdrawals=[key, key]),
            self.delta(withdrawals=[{'alias': 'ghost',
                                     'start_utc': '2025-01-15T09:00:00Z'}]),
            self.delta(withdrawals=[{'alias': row['alias'],
                                     'start_utc': '2025-01-15T09:00:00Z',
                                     'end_utc': '2025-01-15T10:00:00Z'}]),
            self.delta(withdrawals=[{'alias': row['alias'],
                                     'start_utc': 'not-a-time'}]),
            self.delta(upserts=[row]),
            self.delta(upserts=[dict(row, end_utc=row['start_utc'])]),
            self.delta(upserts=[dict(row, title='  ')]),
        ]
        for bad in bad_deltas:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    feed.apply(self.view, bad)

    def test_recipient_base_and_target_must_match(self):
        row = {'alias': 'new', 'start_utc': '2025-01-16T09:00:00Z',
               'end_utc': '2025-01-16T10:00:00Z'}
        result_rows = sorted(self.view['rows'] + [row],
                             key=lambda item: (item['alias'], item['start_utc']))
        good = self.delta(upserts=[row],
                          target_sha256=fingerprint('partner', result_rows))
        self.assertEqual(feed.apply(self.view, good)['rows'], result_rows)
        with self.assertRaises(ValueError):
            feed.apply(self.view, self.delta(recipient='other', upserts=[row]))
        with self.assertRaises(ValueError):
            feed.apply(self.view, self.delta(base_sha256='0' * 64,
                                             upserts=[row]))
        with self.assertRaises(ValueError):
            feed.apply(self.view, self.delta(upserts=[row]))

    def test_withdrawal_and_replacement_semantics(self):
        busy = {'alias': 'meeting', 'start_utc': '2025-01-15T09:00:00Z',
                'end_utc': '2025-01-15T10:00:00Z'}
        delta = self.delta(
            upserts=[busy],
            withdrawals=[{'alias': 'standup',
                          'start_utc': '2025-01-15T11:00:00Z'}],
            target_sha256=fingerprint('partner', [busy]))
        result = feed.apply(self.view, delta)
        self.assertEqual(result['rows'], [busy])
        self.assertEqual(result['view_sha256'], delta['target_sha256'])
        # reverting to the same visible view is recognised by fingerprint
        back = self.delta(
            base_sha256=delta['target_sha256'],
            target_sha256=self.view['view_sha256'],
            upserts=[dict(self.view['rows'][0]),
                     dict(self.view['rows'][1])])
        self.assertEqual(feed.apply(result, back), self.view)

    def test_empty_delta_rewrites_equal_content(self):
        delta = self.delta(target_sha256=self.view['view_sha256'])
        self.assertEqual(feed.apply(self.view, delta), self.view)

    def test_row_count_limits(self):
        many_rows = [{'alias': 'r%04d' % index,
                      'start_utc': '2025-01-15T09:00:00Z',
                      'end_utc': '2025-01-15T10:00:00Z'}
                     for index in range(2049)]
        with self.assertRaises(ValueError):
            feed.apply(dict(self.view, rows=many_rows), self.delta())
        with self.assertRaises(ValueError):
            feed.apply(self.view, self.delta(upserts=many_rows))
        with self.assertRaises(ValueError):
            feed.apply(self.view, self.delta(
                withdrawals=[{'alias': row['alias'],
                              'start_utc': row['start_utc']}
                             for row in many_rows]))

    def test_resulting_view_row_limit(self):
        until = (date(2025, 1, 15) + timedelta(days=2047)).isoformat()
        packed = snapshot({'alpha': 'busy'}, [
            activity('full', until=until, weekdays=(1, 2, 3, 4, 5, 6, 7))])
        view = feed.publish(source(packed))
        self.assertEqual(len(view['rows']), 2048)
        extra = {'alias': 'extra', 'start_utc': '2031-01-01T00:00:00Z',
                 'end_utc': '2031-01-01T01:00:00Z'}
        delta = self.delta(base_sha256=view['view_sha256'], upserts=[extra])
        with self.assertRaises(ValueError):
            feed.apply(view, delta)


if __name__ == '__main__':
    unittest.main()
