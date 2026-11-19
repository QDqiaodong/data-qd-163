"""Business tests for the persistent calendar-reminders queue.

Every expected identity, UTC instant, payload field and state transition is
written here as a literal derived from the contract, not produced by calling
the expansion code under test.  Assertions observe behavior only through the
public ``process`` entry point; no private table is read.
"""
import os
import tempfile
import unittest

from calendar_expand.reminders import process


def utc_rule(schedule, hour, *, tz='UTC', date='2024-11-04', weekday=1,
             until=None, duration=60, exceptions=None, gap_policy='skip',
             fold_policy='earlier', start=None):
    return {
        'schedule': schedule, 'timezone': tz,
        'local_start': start or f'{date}T{hour:02d}:00',
        'interval_weeks': 1, 'weekdays': [weekday],
        'until': until or date, 'duration_minutes': duration,
        'exceptions': [] if exceptions is None else exceptions,
        'gap_policy': gap_policy, 'fold_policy': fold_policy,
    }


def entry(rule, lead=30):
    return {'rule': rule, 'lead_minutes': lead}


def sync(now, schedules):
    return {'action': 'sync', 'now': now, 'schedules': schedules}


def claim(now, limit=1, lease=10):
    return {'action': 'claim', 'now': now, 'limit': limit,
            'lease_minutes': lease}


def ack(now, schedule, nominal_date, token):
    return {'action': 'ack', 'now': now, 'schedule': schedule,
            'nominal_date': nominal_date, 'token': token}


def inspect(now):
    return {'action': 'inspect', 'now': now}


class ReminderCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, 'queue.db')

    def tearDown(self):
        self._dir.cleanup()

    def call(self, document):
        return process(self.db, document)['result']

    def expect_failure(self, document, compare_now=None):
        # Assert failure and, when a readable clock is supplied, prove the
        # logical state is unchanged through the public inspect API only.
        existed = os.path.exists(self.db)
        before = process(self.db, inspect(compare_now))['result'] \
            if existed and compare_now is not None else None
        with self.assertRaises((ValueError, OverflowError)):
            process(self.db, document)
        if not existed:
            self.assertFalse(os.path.exists(self.db))
        elif compare_now is not None:
            self.assertEqual(process(self.db, inspect(compare_now))['result'],
                             before)


class ExpansionIdentityTests(ReminderCase):
    def test_payload_has_exactly_seven_fields_and_utc_envelope(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        job = self.call(inspect('2024-11-04T08:00:00Z'))['jobs'][0]
        self.assertEqual(set(job['payload']),
                         {'schedule', 'nominal_date', 'timezone', 'local_start',
                          'start_utc', 'end_utc', 'due_utc'})
        self.assertEqual(job['payload'], {
            'schedule': 'A', 'nominal_date': '2024-11-04', 'timezone': 'UTC',
            'local_start': '2024-11-04T09:00',
            'start_utc': '2024-11-04T09:00:00Z',
            'end_utc': '2024-11-04T10:00:00Z',
            'due_utc': '2024-11-04T08:30:00Z'})
        self.assertEqual(set(job), {'payload', 'state', 'lease_until'})

    def test_apia_skip_generates_nothing(self):
        rule = utc_rule('apia', 23, tz='Pacific/Apia', date='2011-12-30',
                        weekday=5, start='2011-12-30T23:30')
        result = self.call(sync('2011-12-29T00:00:00Z', [entry(rule, lead=0)]))
        self.assertEqual(result['scheduled_count'], 0)
        self.assertEqual(result['unfinished_count'], 0)
        self.assertEqual(self.call(inspect('2011-12-29T00:00:00Z'))['jobs'], [])

    def test_apia_shift_forward_keeps_nominal_date_across_day_boundary(self):
        rule = utc_rule('apia', 23, tz='Pacific/Apia', date='2011-12-30',
                        weekday=5, start='2011-12-30T23:30', gap_policy='shift_forward')
        self.call(sync('2011-12-29T00:00:00Z', [entry(rule, lead=0)]))
        payload = self.call(inspect('2011-12-29T00:00:00Z'))['jobs'][0]['payload']
        # Identity stays on the nominal Friday even though the resolved wall
        # time crosses into Saturday 00:00 local.
        self.assertEqual(payload, {
            'schedule': 'apia', 'nominal_date': '2011-12-30',
            'timezone': 'Pacific/Apia', 'local_start': '2011-12-31T00:00',
            'start_utc': '2011-12-30T10:00:00Z',
            'end_utc': '2011-12-30T11:00:00Z',
            'due_utc': '2011-12-30T10:00:00Z'})

    def test_apia_exception_suppresses_instance(self):
        rule = utc_rule('apia', 23, tz='Pacific/Apia', date='2011-12-30',
                        weekday=5, start='2011-12-30T23:30', gap_policy='shift_forward',
                        exceptions=['2011-12-30'])
        result = self.call(sync('2011-12-29T00:00:00Z', [entry(rule, lead=0)]))
        self.assertEqual(result['scheduled_count'], 0)

    def test_apia_completed_identity_is_not_resurrected_under_december_31(self):
        rule = utc_rule('apia', 23, tz='Pacific/Apia', date='2011-12-30',
                        weekday=5, start='2011-12-30T23:30', gap_policy='shift_forward')
        self.call(sync('2011-12-29T00:00:00Z', [entry(rule, lead=0)]))
        token = self.call(claim('2011-12-30T10:00:00Z'))['claims'][0]['token']
        self.call(ack('2011-12-30T10:01:00Z', 'apia', '2011-12-30', token))
        self.call(sync('2011-12-30T10:02:00Z', []))
        self.call(sync('2011-12-30T10:03:00Z', [entry(rule, lead=0)]))
        self.assertEqual(self.call(claim('2011-12-30T10:04:00Z', limit=5))['claims'], [])
        completed = self.call(inspect('2011-12-30T10:05:00Z'))['completed']
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]['payload']['nominal_date'], '2011-12-30')
        self.assertEqual(completed[0]['payload']['local_start'], '2011-12-31T00:00')

    def test_paris_historical_second_offset_is_preserved(self):
        rule = utc_rule('paris', 9, tz='Europe/Paris', date='1900-01-01',
                        start='1900-01-01T09:00')
        self.call(sync('1899-12-31T00:00:00Z', [entry(rule, lead=30)]))
        payload = self.call(inspect('1899-12-31T00:00:00Z'))['jobs'][0]['payload']
        self.assertEqual(payload['start_utc'], '1900-01-01T08:50:39Z')
        self.assertEqual(payload['due_utc'], '1900-01-01T08:20:39Z')
        self.assertEqual(payload['end_utc'], '1900-01-01T09:50:39Z')

    def test_due_equality_is_claimable_and_lease_equality_is_expiry(self):
        rule = utc_rule('paris', 9, tz='Europe/Paris', date='1900-01-01',
                        start='1900-01-01T09:00')
        self.call(sync('1899-12-31T00:00:00Z', [entry(rule, lead=30)]))
        claims = self.call(claim('1900-01-01T08:20:39Z', lease=1))['claims']
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['lease_until'], '1900-01-01T08:21:39Z')
        self.assertEqual(self.call(inspect('1900-01-01T08:21:00Z'))['jobs'][0]['state'],
                         'leased')
        expired = self.call(inspect('1900-01-01T08:21:39Z'))['jobs'][0]
        self.assertEqual(expired['state'], 'ready')
        self.assertIsNone(expired['lease_until'])

    def test_different_schedules_same_utc_are_distinct_reminders(self):
        self.call(sync('2024-11-04T08:00:00Z',
                       [entry(utc_rule('A', 9)), entry(utc_rule('B', 9))]))
        result = self.call(claim('2024-11-04T08:30:00Z', limit=5))
        self.assertEqual([c['payload']['schedule'] for c in result['claims']],
                         ['A', 'B'])
        self.assertEqual(len({c['token'] for c in result['claims']}), 2)

    def test_nominal_dates_follow_recurrence_not_resolved_instants(self):
        # Two weekly Monday occurrences; nominal dates are the raw Mondays even
        # though both resolve to ordinary wall times.
        multi = utc_rule('wk', 9, date='2024-11-04', until='2024-11-11')
        self.call(sync('2024-11-01T08:00:00Z', [entry(multi, 0)]))
        jobs = self.call(inspect('2024-11-01T08:00:00Z'))['jobs']
        self.assertEqual([j['payload']['nominal_date'] for j in jobs],
                         ['2024-11-04', '2024-11-11'])
        self.assertEqual([j['payload']['local_start'] for j in jobs],
                         ['2024-11-04T09:00', '2024-11-11T09:00'])


class LinkedAcceptanceBranchOneTests(ReminderCase):
    """The 08:00 sync -> failed 08:35 sync -> ack -> suppress re-add flow."""

    def schedules(self, hour_a=9, hour_b=11):
        return [entry(utc_rule('A', hour_a)), entry(utc_rule('B', hour_b))]

    def test_full_flow_with_literal_contract_expectations(self):
        first = self.call(sync('2024-11-04T08:00:00Z', self.schedules()))
        self.assertEqual(first, {'action': 'sync', 'last_now': '2024-11-04T08:00:00Z',
                                 'scheduled_count': 2, 'unfinished_count': 2})
        claimed = self.call(claim('2024-11-04T08:30:00Z'))
        self.assertEqual(len(claimed['claims']), 1)
        grant = claimed['claims'][0]
        self.assertEqual(grant['lease_until'], '2024-11-04T08:40:00Z')
        self.assertEqual(grant['payload']['schedule'], 'A')
        self.assertEqual(grant['payload']['start_utc'], '2024-11-04T09:00:00Z')
        self.assertEqual(grant['payload']['due_utc'], '2024-11-04T08:30:00Z')
        token = grant['token']

        # The 08:35 snapshot changes both A and B while A's lease is active:
        # the whole batch must fail, leaving B untouched as well.
        self.expect_failure(sync('2024-11-04T08:35:00Z', self.schedules(10, 12)))
        view = self.call(inspect('2024-11-04T08:35:30Z'))
        self.assertEqual(view['last_now'], '2024-11-04T08:30:00Z')
        self.assertEqual([(j['payload']['schedule'], j['payload']['start_utc'], j['state'])
                          for j in view['jobs']],
                         [('A', '2024-11-04T09:00:00Z', 'leased'),
                          ('B', '2024-11-04T11:00:00Z', 'waiting')])

        acknowledged = self.call(ack('2024-11-04T08:36:00Z', 'A', '2024-11-04', token))
        self.assertFalse(acknowledged['duplicate'])
        self.assertEqual(acknowledged['completed']['completed_at'],
                         '2024-11-04T08:36:00Z')
        self.assertEqual(acknowledged['completed']['token'], token)
        retry = self.call(ack('2024-11-04T08:36:30Z', 'A', '2024-11-04', token))
        self.assertTrue(retry['duplicate'])
        self.assertEqual(retry['completed']['completed_at'], '2024-11-04T08:36:00Z')
        self.assertEqual(retry['completed']['payload'], grant['payload'])

        self.call(sync('2024-11-04T08:37:00Z', []))
        self.call(sync('2024-11-04T08:38:00Z', [entry(utc_rule('A', 10))]))
        self.assertEqual(self.call(claim('2024-11-04T09:30:00Z', limit=5))['claims'], [])
        final = self.call(inspect('2024-11-04T09:31:00Z'))
        self.assertEqual(final['jobs'], [])
        self.assertEqual(len(final['completed']), 1)
        self.assertEqual(final['completed'][0]['payload']['start_utc'],
                         '2024-11-04T09:00:00Z')
        self.assertEqual(final['completed'][0]['completed_at'],
                         '2024-11-04T08:36:00Z')


class LinkedAcceptanceBranchTwoTests(ReminderCase):
    """Lease expiry allows reschedule; old tokens die, re-claim mints new ones."""

    def test_expired_lease_reschedule_reclaim_and_idempotent_ack(self):
        self.call(sync('2024-11-04T08:00:00Z',
                       [entry(utc_rule('A', 9)), entry(utc_rule('B', 11))]))
        old_token = self.call(claim('2024-11-04T08:30:00Z'))['claims'][0]['token']

        moved = self.call(sync('2024-11-04T08:40:00Z',
                               [entry(utc_rule('A', 10)), entry(utc_rule('B', 12))]))
        self.assertEqual(moved['last_now'], '2024-11-04T08:40:00Z')
        self.assertEqual(moved['scheduled_count'], 2)
        self.expect_failure(ack('2024-11-04T08:40:00Z', 'A', '2024-11-04', old_token))

        reclaimed = self.call(claim('2024-11-04T09:30:00Z', limit=5))['claims']
        self.assertEqual(len(reclaimed), 1)
        new_grant = reclaimed[0]
        self.assertEqual(new_grant['payload']['due_utc'], '2024-11-04T09:30:00Z')
        self.assertEqual(new_grant['payload']['start_utc'], '2024-11-04T10:00:00Z')
        self.assertNotEqual(new_grant['token'], old_token)
        # The old token can never complete the new lease.
        self.expect_failure(ack('2024-11-04T09:31:00Z', 'A', '2024-11-04', old_token))

        first = self.call(ack('2024-11-04T09:32:00Z', 'A', '2024-11-04',
                              new_grant['token']))
        self.assertFalse(first['duplicate'])
        second = self.call(ack('2024-11-04T09:33:00Z', 'A', '2024-11-04',
                               new_grant['token']))
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['completed']['completed_at'],
                         '2024-11-04T09:32:00Z')
        self.expect_failure(ack('2024-11-04T09:34:00Z', 'A', '2024-11-04', old_token))

    def test_state_survives_process_restart_via_reopened_database(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        token = self.call(claim('2024-11-04T08:30:00Z'))['claims'][0]['token']
        # A brand-new connection to the same file is the process-restart case.
        reopened = process(self.db, inspect('2024-11-04T08:31:00Z'))['result']
        self.assertEqual(reopened['jobs'][0]['state'], 'leased')
        self.assertEqual(reopened['jobs'][0]['lease_until'], '2024-11-04T08:40:00Z')
        # Token uniqueness counter survives restart as well: the next lease on
        # another identity must not reuse the sequence value.
        self.call(ack('2024-11-04T08:32:00Z', 'A', '2024-11-04', token))
        self.call(sync('2024-11-04T08:33:00Z',
                       [entry(utc_rule('A', 9)), entry(utc_rule('B', 9))]))
        b_grant = process(self.db, claim('2024-11-04T08:34:00Z', limit=5))['result']
        b_token = next(c['token'] for c in b_grant['claims']
                       if c['payload']['schedule'] == 'B')
        self.assertNotEqual(b_token, token)
        self.assertEqual(len({token, b_token}), 2)


class NewYorkFoldTests(ReminderCase):
    def rule(self, fold):
        return utc_rule('ny', 1, tz='America/New_York', date='2024-11-03',
                        weekday=7, start='2024-11-03T01:30', fold_policy=fold)

    def test_fold_change_against_active_lease_rolls_back_then_succeeds(self):
        self.call(sync('2024-11-03T04:00:00Z', [entry(self.rule('earlier'))]))
        grant = self.call(claim('2024-11-03T05:00:00Z'))['claims'][0]
        self.assertEqual(grant['payload']['start_utc'], '2024-11-03T05:30:00Z')
        self.assertEqual(grant['payload']['due_utc'], '2024-11-03T05:00:00Z')
        self.assertEqual(grant['lease_until'], '2024-11-03T05:10:00Z')

        self.expect_failure(sync('2024-11-03T05:05:00Z',
                                 [entry(self.rule('later'))]))
        intact = self.call(inspect('2024-11-03T05:06:00Z'))
        self.assertEqual(intact['jobs'][0]['payload']['start_utc'],
                         '2024-11-03T05:30:00Z')
        self.assertEqual(intact['last_now'], '2024-11-03T05:00:00Z')

        self.call(sync('2024-11-03T05:10:00Z', [entry(self.rule('later'))]))
        changed = self.call(inspect('2024-11-03T05:11:00Z'))['jobs'][0]
        self.assertEqual(changed['payload']['start_utc'], '2024-11-03T06:30:00Z')
        self.assertEqual(changed['payload']['due_utc'], '2024-11-03T06:00:00Z')
        self.assertIsNone(changed['lease_until'])
        self.expect_failure(ack('2024-11-03T05:12:00Z', 'ny', '2024-11-03',
                                grant['token']))


class ClockAndLifecycleTests(ReminderCase):
    def test_backward_now_rejected_for_every_action_without_state_change(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        for document in (sync('2024-11-04T07:59:59Z', []),
                         claim('2024-11-04T07:59:59Z'),
                         inspect('2024-11-04T07:59:59Z')):
            self.expect_failure(document, compare_now='2024-11-04T08:00:00Z')
        self.assertEqual(self.call(inspect('2024-11-04T08:00:00Z'))['last_now'],
                         '2024-11-04T08:00:00Z')

    def test_inspect_does_not_advance_but_same_now_mutations_allowed(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        self.assertEqual(self.call(inspect('2024-11-04T08:00:00Z'))['last_now'],
                         '2024-11-04T08:00:00Z')
        first = self.call(claim('2024-11-04T08:30:00Z'))
        second = self.call(claim('2024-11-04T08:30:00Z', limit=5))
        self.assertEqual(second['claims'], [])
        self.assertEqual(second['last_now'], first['last_now'])

    def test_empty_snapshot_keeps_tombstones_and_re_add_needs_new_token(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        token = self.call(claim('2024-11-04T08:30:00Z'))['claims'][0]['token']
        self.call(ack('2024-11-04T08:31:00Z', 'A', '2024-11-04', token))
        self.call(sync('2024-11-04T08:32:00Z', []))
        self.assertEqual(self.call(inspect('2024-11-04T08:32:30Z'))['jobs'], [])
        readded = self.call(sync('2024-11-04T08:33:00Z', [entry(utc_rule('A', 9))]))
        self.assertEqual(readded['scheduled_count'], 1)
        self.assertEqual(readded['unfinished_count'], 0)
        self.assertEqual(self.call(claim('2024-11-04T08:34:00Z'))['claims'], [])

    def test_uncompleted_removed_identity_can_be_claimed_again_with_new_token(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        token = self.call(claim('2024-11-04T08:30:00Z'))['claims'][0]['token']
        # Let the lease expire, then remove and re-add without a tombstone.
        self.call(sync('2024-11-04T08:40:00Z', []))
        self.call(sync('2024-11-04T08:41:00Z', [entry(utc_rule('A', 9))]))
        grants = self.call(claim('2024-11-04T08:42:00Z'))['claims']
        self.assertEqual(len(grants), 1)
        self.assertNotEqual(grants[0]['token'], token)

    def test_payload_preserving_sync_keeps_lease_token_and_deadline(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        grant = self.call(claim('2024-11-04T08:30:00Z', lease=10))['claims'][0]
        # Reordering the schedules array and re-syncing an identical rule must
        # not extend the lease or rotate the token.
        self.call(sync('2024-11-04T08:32:00Z',
                       [entry(utc_rule('B', 11)), entry(utc_rule('A', 9))]))
        view = self.call(inspect('2024-11-04T08:33:00Z'))
        a_job = next(j for j in view['jobs'] if j['payload']['schedule'] == 'A')
        self.assertEqual(a_job['state'], 'leased')
        self.assertEqual(a_job['lease_until'], '2024-11-04T08:40:00Z')
        self.call(ack('2024-11-04T08:34:00Z', 'A', '2024-11-04', grant['token']))

    def test_rule_changes_that_do_not_change_payload_keep_commitment(self):
        self.call(sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))]))
        token = self.call(claim('2024-11-04T08:30:00Z'))['claims'][0]['token']
        reordered = utc_rule('A', 9)
        reordered['weekdays'] = [1]
        self.call(sync('2024-11-04T08:32:00Z', [entry(reordered)]))
        self.call(ack('2024-11-04T08:33:00Z', 'A', '2024-11-04', token))

    def test_ack_error_matrix_and_failure_does_not_advance_clock(self):
        self.call(sync('2024-11-04T08:00:00Z',
                       [entry(utc_rule('A', 9)), entry(utc_rule('B', 9))]))
        token = self.call(claim('2024-11-04T08:30:00Z'))['claims'][0]['token']
        for bad in (
                ack('2024-11-04T08:31:00Z', 'Z', '2024-11-04', token),
                ack('2024-11-04T08:31:00Z', 'B', '2024-11-04', token),
                ack('2024-11-04T08:31:00Z', 'A', '2024-11-04', 'other-token'),
                ack('2024-11-04T08:41:00Z', 'A', '2024-11-04', token)):
            self.expect_failure(bad)
        self.assertEqual(self.call(inspect('2024-11-04T08:42:00Z'))['last_now'],
                         '2024-11-04T08:30:00Z')

    def test_overflow_is_a_whole_operation_error(self):
        self.call(sync('9999-12-30T00:00:00Z', [entry(utc_rule('A', 9), 0)]))
        self.expect_failure(claim('9999-12-31T23:58:00Z', lease=1440),
                            compare_now='9999-12-31T23:58:01Z')
        view = self.call(inspect('9999-12-31T23:58:01Z'))
        self.assertEqual(view['last_now'], '9999-12-30T00:00:00Z')
        self.assertEqual(view['jobs'][0]['state'], 'ready')


class OrderingAndLimitsTests(ReminderCase):
    def test_claim_orders_by_due_schedule_nominal_date_and_respects_limit(self):
        schedules = [entry(utc_rule('zeta', 9)), entry(utc_rule('alpha', 9)),
                     entry(utc_rule('mid', 9))]
        self.call(sync('2024-11-04T08:00:00Z', schedules))
        first = self.call(claim('2024-11-04T08:30:00Z', limit=2))
        self.assertEqual([c['payload']['schedule'] for c in first['claims']],
                         ['alpha', 'mid'])
        second = self.call(claim('2024-11-04T08:30:30Z', limit=2))
        self.assertEqual([c['payload']['schedule'] for c in second['claims']],
                         ['zeta'])

    def test_inspect_jobs_and_completed_ordering(self):
        self.call(sync('2024-11-04T08:00:00Z',
                       [entry(utc_rule('A', 9)), entry(utc_rule('B', 9))]))
        grants = self.call(claim('2024-11-04T08:30:00Z', limit=5))['claims']
        for grant in grants:
            self.call(ack('2024-11-04T08:31:00Z', grant['payload']['schedule'],
                          grant['payload']['nominal_date'], grant['token']))
        completed = self.call(inspect('2024-11-04T08:32:00Z'))['completed']
        self.assertEqual([c['payload']['schedule'] for c in completed], ['A', 'B'])
        self.assertEqual([set(c) for c in completed],
                         [{'payload', 'token', 'completed_at'}] * 2)


class StrictValidationTests(ReminderCase):
    def valid_sync(self):
        return sync('2024-11-04T08:00:00Z', [entry(utc_rule('A', 9))])

    def test_unknown_and_missing_action_fields_rejected(self):
        for document in ({'action': 'frobnicate', 'now': '2024-11-04T08:00:00Z'},
                         {'now': '2024-11-04T08:00:00Z'},
                         {'action': 'inspect', 'now': '2024-11-04T08:00:00Z',
                          'extra': 1},
                         {'action': 'inspect'},
                         'not-an-object', []):
            self.expect_failure(document)

    def test_now_requires_utc_seconds_without_leap_second(self):
        for bad in ('2024-11-04T08:00:00.5Z', '2024-11-04T08:00:00+01:00',
                    '2024-11-04 08:00:00Z', '2024-11-04T08:00:60Z',
                    '2024-11-04T08:00:00', '20241104T080000Z', 800, True):
            self.expect_failure({'action': 'inspect', 'now': bad})

    def test_booleans_rejected_in_integer_positions(self):
        for document in (
                {**self.valid_sync(),
                 'schedules': [{'rule': utc_rule('A', 9), 'lead_minutes': True}]},
                claim('2024-11-04T08:30:00Z') | {'limit': True},
                claim('2024-11-04T08:30:00Z') | {'lease_minutes': False}):
            self.expect_failure(document)

    def test_integer_bounds_enforced(self):
        self.expect_failure({**self.valid_sync(),
                             'schedules': [{'rule': utc_rule('A', 9),
                                            'lead_minutes': 10081}]})
        self.expect_failure(claim('2024-11-04T08:30:00Z', limit=0))
        self.expect_failure(claim('2024-11-04T08:30:00Z', limit=257))
        self.expect_failure(claim('2024-11-04T08:30:00Z', lease=0))
        self.expect_failure(claim('2024-11-04T08:30:00Z', lease=1441))

    def test_duplicate_schedule_rejected_even_when_second_expands_empty(self):
        empty = utc_rule('A', 9, exceptions=['2024-11-04'])
        self.expect_failure(sync('2024-11-04T08:00:00Z',
                                 [entry(utc_rule('A', 9)), entry(empty)]))

    def test_trailing_invalid_rule_cannot_be_hidden_by_completed_tombstone(self):
        self.call(self.valid_sync())
        token = self.call(claim('2024-11-04T08:30:00Z'))['claims'][0]['token']
        self.call(ack('2024-11-04T08:31:00Z', 'A', '2024-11-04', token))
        bad_rule = utc_rule('B', 9)
        bad_rule['interval_weeks'] = True
        self.expect_failure(sync('2024-11-04T08:32:00Z',
                                 [entry(utc_rule('A', 9)), entry(bad_rule)]))
        # The tombstone remains and the clock was not advanced by the failure.
        view = self.call(inspect('2024-11-04T08:33:00Z'))
        self.assertEqual(view['last_now'], '2024-11-04T08:31:00Z')
        self.assertEqual(view['completed'][0]['payload']['schedule'], 'A')

    def test_more_than_32_rules_rejected_without_creating_database(self):
        fresh = os.path.join(self._dir.name, 'fresh.db')
        document = sync('2024-11-04T08:00:00Z',
                        [entry(utc_rule(f's{i}', 9)) for i in range(33)])
        with self.assertRaises(ValueError):
            process(fresh, document)
        self.assertFalse(os.path.exists(fresh))

    def test_8192_reminder_boundary_including_suppressed_identities(self):
        from datetime import date, timedelta
        start = date(2024, 11, 4)
        full = (start + timedelta(days=3651)).isoformat()
        boundary = (start + timedelta(days=887)).isoformat()
        over = (start + timedelta(days=888)).isoformat()

        def daily(name, until):
            return utc_rule(name, 9, date=start.isoformat(), until=until,
                            weekday=1)

        def daily_doc(name, until):
            rule = daily(name, until)
            rule['weekdays'] = [1, 2, 3, 4, 5, 6, 7]
            return entry(rule, lead=0)

        exact = self.call(sync('2024-11-04T08:00:00Z', [
            daily_doc('a', full), daily_doc('b', full), daily_doc('c', boundary)]))
        self.assertEqual(exact['scheduled_count'], 8192)
        self.expect_failure(sync('2024-11-04T08:01:00Z', [
            daily_doc('a', full), daily_doc('b', full), daily_doc('c', over)]))
        view = self.call(inspect('2024-11-04T08:02:00Z'))
        self.assertEqual(view['last_now'], '2024-11-04T08:00:00Z')
        self.assertEqual(len(view['jobs']), 8192)

    def test_ack_identifier_and_date_validation(self):
        self.call(self.valid_sync())
        self.expect_failure({'action': 'ack', 'now': '2024-11-04T08:30:00Z',
                             'schedule': ' A', 'nominal_date': '2024-11-04',
                             'token': 't'})
        self.expect_failure({'action': 'ack', 'now': '2024-11-04T08:30:00Z',
                             'schedule': 'A', 'nominal_date': '2024-11-4',
                             'token': 't'})
        self.expect_failure({'action': 'ack', 'now': '2024-11-04T08:30:00Z',
                             'schedule': 'A', 'nominal_date': '2024-11-04',
                             'token': ''})

    def test_empty_snapshot_and_empty_expansion_are_legal(self):
        self.assertEqual(self.call(sync('2024-11-04T08:00:00Z', [])),
                         {'action': 'sync', 'last_now': '2024-11-04T08:00:00Z',
                          'scheduled_count': 0, 'unfinished_count': 0})
        empty = utc_rule('A', 9, exceptions=['2024-11-04'])
        result = self.call(sync('2024-11-04T08:01:00Z', [entry(empty)]))
        self.assertEqual(result['scheduled_count'], 0)
        self.assertEqual(self.call(claim('2024-11-04T08:02:00Z'))['claims'], [])

    def test_skipped_gap_and_exceptions_are_not_counted(self):
        apia_skip = utc_rule('apia', 23, tz='Pacific/Apia', date='2011-12-30',
                             weekday=5, start='2011-12-30T23:30', gap_policy='skip')
        result = self.call(sync('2011-12-29T00:00:00Z', [entry(apia_skip, 0)]))
        self.assertEqual(result['scheduled_count'], 0)
        apia_except = dict(apia_skip, gap_policy='shift_forward',
                           exceptions=['2011-12-30'])
        result = self.call(sync('2011-12-29T00:01:00Z', [entry(apia_except, 0)]))
        self.assertEqual(result['scheduled_count'], 0)

    def test_removing_a_rule_with_an_active_lease_rejects_the_whole_sync(self):
        self.call(sync('2024-11-04T08:00:00Z',
                       [entry(utc_rule('A', 9)), entry(utc_rule('B', 11))]))
        self.call(claim('2024-11-04T08:30:00Z'))
        # Drop A (leased) while keeping B: the entire snapshot must be rejected.
        self.expect_failure(sync('2024-11-04T08:32:00Z',
                                 [entry(utc_rule('B', 11))]),
                            compare_now='2024-11-04T08:32:00Z')
        view = self.call(inspect('2024-11-04T08:33:00Z'))
        self.assertEqual([j['payload']['schedule'] for j in view['jobs']],
                         ['A', 'B'])
        self.assertEqual(view['last_now'], '2024-11-04T08:30:00Z')

    def test_ready_and_waiting_states_follow_due_comparison_without_writes(self):
        # A due at 08:30, B due at 10:30; at 09:00 A is ready and B waiting.
        self.call(sync('2024-11-04T08:00:00Z',
                       [entry(utc_rule('A', 9)), entry(utc_rule('B', 11))]))
        view = self.call(inspect('2024-11-04T09:00:00Z'))
        self.assertEqual([(j['payload']['schedule'], j['state'], j['lease_until'])
                          for j in view['jobs']],
                         [('A', 'ready', None), ('B', 'waiting', None)])
        # A read-only inspect must not have advanced the clock.
        self.assertEqual(view['last_now'], '2024-11-04T08:00:00Z')


class ForeignDatabaseTests(ReminderCase):
    def test_non_sync_on_missing_database_fails(self):
        missing = os.path.join(self._dir.name, 'missing.db')
        for document in (claim('2024-11-04T08:30:00Z'),
                         ack('2024-11-04T08:30:00Z', 'A', '2024-11-04', 't'),
                         inspect('2024-11-04T08:30:00Z')):
            with self.assertRaises(ValueError):
                process(missing, document)
            self.assertFalse(os.path.exists(missing))

    def test_foreign_database_is_never_cleared_or_overwritten(self):
        import sqlite3
        foreign = os.path.join(self._dir.name, 'foreign.db')
        connection = sqlite3.connect(foreign)
        connection.execute('CREATE TABLE other(value TEXT)')
        connection.execute("INSERT INTO other VALUES('kept')")
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            process(foreign, sync('2024-11-04T08:00:00Z',
                                  [entry(utc_rule('A', 9))]))
        check = sqlite3.connect(foreign)
        self.assertEqual(check.execute('SELECT value FROM other').fetchall(),
                         [('kept',)])
        self.assertEqual(check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                         [('other',)])
        check.close()


if __name__ == '__main__':
    unittest.main()
