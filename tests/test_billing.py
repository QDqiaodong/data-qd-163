"""Business tests for the persistent shared-facility billing ledger.

Every expected bill, adjustment and per-line charge is written here as a
literal derived from the contract.  Assertions go through the public
``process`` entry point; no private table is read.
"""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from calendar_expand.billing import process


def rule(schedule, *, tz='UTC', start='2025-01-06T09:00', until=None,
         weekdays=None, duration=60, gap='skip', fold='earlier',
         exceptions=()):
    if weekdays is None:
        weekdays = [date.fromisoformat(start[:10]).isoweekday()]
    return {
        'schedule': schedule, 'timezone': tz, 'local_start': start,
        'interval_weeks': 1, 'weekdays': list(weekdays),
        'until': until or start[:10], 'duration_minutes': duration,
        'exceptions': list(exceptions), 'gap_policy': gap,
        'fold_policy': fold,
    }


def booking(schedule, account, *, room='R', **changes):
    return {'room': room, 'account': account, 'rule': rule(schedule, **changes)}


def init(first_month='2025-01', rooms=None, tz='UTC'):
    return {'action': 'init', 'timezone': tz, 'first_month': first_month,
            'rooms': rooms or [{'room': 'R', 'free_minutes': 60,
                                'unit_cents': 2}]}


def replace(*bookings):
    return {'action': 'replace', 'bookings': list(bookings)}


def close(month):
    return {'action': 'close', 'month': month}


def show(month):
    return {'action': 'show', 'month': month}


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, 'ledger.db')

    def tearDown(self):
        self._dir.cleanup()

    def call(self, document):
        return process(self.db, document)

    def fresh(self, **changes):
        self.call(init(**changes))

    def expect_failure(self, document, probe=None):
        with self.assertRaises((ValueError, OverflowError)):
            process(self.db, document)
        if probe is not None:
            self.assertEqual(self.call(probe), self.call(probe))


class InitTests(LedgerCase):
    def test_init_returns_first_month_and_creates_database(self):
        self.assertEqual(self.call(init(first_month='2025-03')),
                         {'first_month': '2025-03'})
        self.assertTrue(os.path.exists(self.db))

    def test_init_never_reuses_an_existing_ledger(self):
        self.fresh()
        with self.assertRaises(ValueError):
            self.call(init(first_month='2030-01'))

    def test_init_rejects_duplicate_room_and_bad_bounds(self):
        for bad in (
            {'action': 'init', 'timezone': 'UTC', 'first_month': '2025-01',
             'rooms': [{'room': 'R', 'free_minutes': 1, 'unit_cents': 1},
                       {'room': 'R', 'free_minutes': 2, 'unit_cents': 2}]},
            {'action': 'init', 'timezone': 'UTC', 'first_month': '2025-01',
             'rooms': [{'room': 'R', 'free_minutes': -1, 'unit_cents': 1}]},
            {'action': 'init', 'timezone': 'UTC', 'first_month': '2025-01',
             'rooms': [{'room': 'R', 'free_minutes': 1, 'unit_cents': 0}]},
            {'action': 'init', 'timezone': 'UTC', 'first_month': '2025-01',
             'rooms': []},
            {'action': 'init', 'timezone': 'UTC', 'first_month': '2025-13',
             'rooms': [{'room': 'R', 'free_minutes': 1, 'unit_cents': 1}]},
            {'action': 'init', 'timezone': 'UTC', 'first_month': '0001-01',
             'rooms': [{'room': 'R', 'free_minutes': 1, 'unit_cents': 1}]},
            {'action': 'init', 'timezone': 'Not/AZone', 'first_month': '2025-01',
             'rooms': [{'room': 'R', 'free_minutes': 1, 'unit_cents': 1}]},
        ):
            with self.assertRaises(ValueError):
                process(self.db, bad)
        self.assertFalse(os.path.exists(self.db))

    def test_rejects_boolean_and_extra_fields(self):
        with self.assertRaises(ValueError):
            process(self.db, {'action': 'init', 'timezone': 'UTC',
                              'first_month': '2025-01',
                              'rooms': [{'room': 'R', 'free_minutes': True,
                                         'unit_cents': 1}]})
        with self.assertRaises(ValueError):
            process(self.db, {**init(), 'extra': 1})


class ChargeOrderingTests(LedgerCase):
    def test_free_allowance_goes_to_the_earlier_real_start(self):
        self.fresh()
        self.assertEqual(self.call(replace(
            booking('B', '乙', start='2025-01-06T10:00'),
            booking('A', '甲', start='2025-01-06T09:00'))),
            {'occurrence_count': 2})
        bill = self.call(close('2025-01'))
        by_schedule = {line['schedule']: line for line in bill['charges']}
        self.assertEqual(by_schedule['A']['amount_cents'], 0)
        self.assertEqual(by_schedule['A']['free_minutes'], 60)
        self.assertEqual(by_schedule['B']['amount_cents'], 120)
        self.assertEqual(by_schedule['B']['free_minutes'], 0)

    def test_same_start_is_decided_by_identifier_not_input_order(self):
        outcomes = []
        for order in (('B', '乙', 'A', '甲'), ('A', '甲', 'B', '乙')):
            db = os.path.join(self._dir.name, 'order-%s.db' % order[0])
            process(db, init())
            process(db, replace(
                booking(order[0], order[1], start='2025-01-06T09:00'),
                booking(order[2], order[3], start='2025-01-06T09:00')))
            bill = process(db, close('2025-01'))
            outcomes.append({line['schedule']: line['amount_cents']
                             for line in bill['charges']})
        self.assertEqual(outcomes[0], {'A': 0, 'B': 120})
        self.assertEqual(outcomes[0], outcomes[1])

    def test_partial_free_instance_pays_only_the_excess(self):
        self.fresh(rooms=[{'room': 'R', 'free_minutes': 90,
                           'unit_cents': 3}])
        self.call(replace(booking('A', '甲', start='2025-01-06T09:00'),
                          booking('B', '乙', start='2025-01-06T10:00')))
        bill = self.call(close('2025-01'))
        by_schedule = {line['schedule']: line for line in bill['charges']}
        self.assertEqual(by_schedule['A']['free_minutes'], 60)
        self.assertEqual(by_schedule['A']['amount_cents'], 0)
        self.assertEqual(by_schedule['B']['free_minutes'], 30)
        self.assertEqual(by_schedule['B']['minutes'], 60)
        self.assertEqual(by_schedule['B']['amount_cents'], 90)

    def test_allowance_resets_for_each_room_and_month(self):
        self.fresh(rooms=[{'room': 'R', 'free_minutes': 60, 'unit_cents': 2},
                          {'room': 'S', 'free_minutes': 0, 'unit_cents': 5}])
        self.call(replace(
            booking('A', '甲', start='2025-01-06T09:00'),
            booking('B', '乙', room='S', start='2025-01-06T09:00'),
            booking('C', '丙', start='2025-02-03T09:00')))
        january = self.call(close('2025-01'))
        february = self.call(close('2025-02'))
        self.assertEqual({line['schedule']: line['amount_cents']
                          for line in january['charges']},
                         {'A': 0, 'B': 300})
        self.assertEqual({line['schedule']: line['amount_cents']
                          for line in february['charges']}, {'C': 0})
        self.assertEqual(february['adjustments'], [])

    def test_historical_seconds_offset_preserved_and_ordered(self):
        self.fresh(first_month='1900-01',
                   rooms=[{'room': 'R', 'free_minutes': 60, 'unit_cents': 2}])
        self.call(replace(
            booking('late', '乙', tz='Europe/Paris',
                    start='1900-01-01T10:00'),
            booking('early', '甲', tz='Europe/Paris',
                    start='1900-01-01T09:00')))
        bill = self.call(close('1900-01'))
        by_schedule = {line['schedule']: line for line in bill['charges']}
        self.assertEqual(by_schedule['early']['start_utc'],
                         '1900-01-01T08:50:39Z')
        self.assertEqual(by_schedule['late']['start_utc'],
                         '1900-01-01T09:50:39Z')
        self.assertEqual(by_schedule['early']['amount_cents'], 0)
        self.assertEqual(by_schedule['late']['amount_cents'], 120)

    def test_fold_instances_are_ordered_by_real_utc_not_wall_clock(self):
        # Both local 01:30 in New York fall back to the same wall-clock text.
        # The earlier fold is 05:30Z and the later fold is 06:30Z.
        self.fresh(first_month='2024-11', rooms=[
            {'room': 'R', 'free_minutes': 60, 'unit_cents': 2}])
        self.call(replace(
            booking('later', '乙', tz='America/New_York',
                    start='2024-11-03T01:30', fold='later'),
            booking('earlier', '甲', tz='America/New_York',
                    start='2024-11-03T01:30', fold='earlier')))
        bill = self.call(close('2024-11'))
        by_schedule = {line['schedule']: line for line in bill['charges']}
        self.assertEqual(by_schedule['earlier']['start_utc'],
                         '2024-11-03T05:30:00Z')
        self.assertEqual(by_schedule['later']['start_utc'],
                         '2024-11-03T06:30:00Z')
        self.assertEqual(by_schedule['earlier']['free_minutes'], 60)
        self.assertEqual(by_schedule['later']['free_minutes'], 0)


    def test_partially_free_result_ignores_input_permutation(self):
        counter = iter(range(3))

        def run(order):
            db = os.path.join(self._dir.name, 'perm-%d.db' % next(counter))
            process(db, init(rooms=[{'room': 'R', 'free_minutes': 90,
                                     'unit_cents': 3}]))
            process(db, replace(*order))
            lines = process(db, close('2025-01'))['charges']
            return [(line['schedule'], line['free_minutes'],
                     line['amount_cents']) for line in lines]
        # Charges are sorted by identity, whose account column orders the
        # CJK accounts 丙 < 乙 < 甲.
        expected = [('C', 0, 180), ('B', 30, 90), ('A', 60, 0)]
        noop = booking('A', '甲', start='2025-01-06T09:00')
        second = booking('B', '乙', start='2025-01-06T10:00')
        third = booking('C', '丙', start='2025-01-06T11:00')
        for order in ((noop, second, third), (third, noop, second),
                      (second, third, noop)):
            self.assertEqual(run(order), expected)

    def test_free_instance_appears_in_charges(self):
        self.fresh()
        self.call(replace(booking('A', '甲', start='2025-01-06T09:00')))
        charges = self.call(close('2025-01'))['charges']
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]['amount_cents'], 0)
        self.assertEqual(charges[0]['free_minutes'], 60)
        self.assertEqual(charges[0]['minutes'], 60)

    def test_unicode_identifiers_round_trip(self):
        self.fresh(rooms=[{'room': '大会议室', 'free_minutes': 0,
                           'unit_cents': 7}])
        self.call(replace({'room': '大会议室', 'account': '研发中心',
                           'rule': rule('周会', start='2025-01-06T09:00')}))
        bill = self.call(close('2025-01'))
        self.assertEqual(bill['charges'][0]['room'], '大会议室')
        self.assertEqual(bill['charges'][0]['account'], '研发中心')
        self.assertEqual(bill['charges'][0]['schedule'], '周会')

    def test_lone_surrogate_identifier_is_rejected(self):
        self.fresh()
        with self.assertRaises(ValueError):
            self.call(replace({'room': 'R', 'account': '\ud800',
                               'rule': rule('A')}))


class ServiceMonthTests(LedgerCase):
    def test_service_month_uses_the_ledger_timezone(self):
        # 2024-10-31T23:50Z is still 2024-10-31 in UTC but 2024-10-31 19:50
        # in New York; 2024-11-01T00:10Z is already November in UTC but
        # 2024-10-31 20:10 in New York.  Both land in the October service
        # month, and ordering uses the real instants.
        self.fresh(first_month='2024-10', tz='America/New_York', rooms=[
            {'room': 'R', 'free_minutes': 30, 'unit_cents': 10}])
        self.call(replace(
            booking('early', '乙', start='2024-10-31T23:50',
                    duration=30),
            booking('late', '甲', start='2024-11-01T00:10', duration=30)))
        october = self.call(close('2024-10'))
        self.assertEqual({line['schedule']: line['service_month']
                          for line in october['charges']},
                         {'early': '2024-10', 'late': '2024-10'})
        by_schedule = {line['schedule']: line for line in october['charges']}
        self.assertEqual(by_schedule['early']['free_minutes'], 30)
        self.assertEqual(by_schedule['late']['amount_cents'], 300)
        self.assertEqual(self.call(close('2024-11'))['charges'], [])

    def test_reschedule_across_a_month_boundary_moves_its_service_month(self):
        self.fresh(first_month='2025-01',
                   rooms=[{'room': 'R', 'free_minutes': 0, 'unit_cents': 2}])
        self.call(replace(booking('A', '甲', start='2025-01-31T23:30')))
        january = self.call(close('2025-01'))
        self.assertEqual(january['charges'][0]['service_month'], '2025-01')
        self.assertEqual(january['charges'][0]['amount_cents'], 120)
        self.call(replace(booking('A', '甲', start='2025-02-01T00:30')))
        february = self.call(close('2025-02'))
        self.assertEqual(
            [(line['service_month'], line['room'], line['account'],
              line['schedule'], line['nominal_date'], line['amount_cents'])
             for line in february['adjustments']],
            [('2025-01', 'R', '甲', 'A', '2025-01-31', -120)])
        self.assertEqual(
            {line['service_month'] for line in february['charges']},
            {'2025-02'})
        self.assertEqual(february['charges'][0]['amount_cents'], 120)

    def test_month_outside_the_ledger_range_fails_the_whole_replace(self):
        self.fresh(first_month='2025-01')
        with self.assertRaises(ValueError):
            self.call(replace(booking('A', '甲', start='2024-12-31T23:30')))
        with self.assertRaises(ValueError):
            self.call(replace(
                booking('ok', '甲', start='2025-01-06T09:00'),
                booking('ancient', '甲', start='2024-12-01T09:00')))
        # Neither failed replace installed anything.
        self.assertEqual(self.call(close('2025-01'))['charges'], [])
        with self.assertRaises(ValueError):
            self.call(replace(booking('future', '甲', start='9999-01-04T09:00')))


class AdjustmentTests(LedgerCase):
    def test_removed_and_added_lines_post_opposite_adjustments(self):
        self.fresh()
        self.call(replace(booking('A', '甲', start='2025-01-06T09:00'),
                          booking('B', '乙', start='2025-01-06T10:00')))
        self.assertEqual(
            {line['schedule']: line['amount_cents']
             for line in self.call(close('2025-01'))['charges']},
            {'A': 0, 'B': 120})
        self.call(replace(booking('B', '乙', start='2025-01-06T10:00'),
                          booking('C', '丙', start='2025-01-06T11:00')))
        february = self.call(close('2025-02'))
        self.assertEqual(february['charges'], [])
        self.assertEqual(
            [(line['account'], line['schedule'], line['amount_cents'])
             for line in february['adjustments']],
            [('丙', 'C', 120), ('乙', 'B', -120)])
        self.assertEqual(february['account_totals'],
                         [{'account': '丙', 'amount_cents': 120},
                          {'account': '乙', 'amount_cents': -120}])
        # The unchanged list posts nothing on a later bill.
        march = self.call(close('2025-03'))
        self.assertEqual(march['adjustments'], [])
        self.assertEqual(march['account_totals'], [])

    def test_reverting_a_list_reverses_the_earlier_adjustment(self):
        self.fresh()
        original = [booking('A', '甲', start='2025-01-06T09:00'),
                    booking('B', '乙', start='2025-01-06T10:00')]
        self.call(replace(*original))
        january = self.call(close('2025-01'))
        self.call(replace(booking('B', '乙', start='2025-01-06T10:00'),
                          booking('C', '丙', start='2025-01-06T11:00')))
        february = self.call(close('2025-02'))
        self.call(replace(*original))
        march = self.call(close('2025-03'))
        self.assertEqual(
            [(line['account'], line['schedule'], line['amount_cents'])
             for line in march['adjustments']],
            [('丙', 'C', -120), ('乙', 'B', 120)])
        # Old bills remain exactly as issued.
        self.assertEqual(self.call(show('2025-01')), january)
        self.assertEqual(self.call(show('2025-02')), february)

    def test_cumulative_posted_includes_prior_adjustments(self):
        # The historical line B moves between three lists; each close compares
        # against the cumulative posted amount, not just the original bill.
        self.fresh(first_month='2025-01',
                   rooms=[{'room': 'R', 'free_minutes': 0, 'unit_cents': 10}])
        self.call(replace(booking('B', '乙', start='2025-01-06T10:00')))
        self.assertEqual(self.call(close('2025-01'))['charges'][0]['amount_cents'],
                         600)
        self.call(replace(booking('B', '乙', start='2025-01-06T10:00',
                                  duration=30)))
        self.assertEqual(
            self.call(close('2025-02'))['adjustments'][0]['amount_cents'], -300)
        # Now remove it entirely: the cumulative posted is 300, so -300.
        self.call(replace())
        self.assertEqual(
            self.call(close('2025-03'))['adjustments'],
            [{'service_month': '2025-01', 'room': 'R', 'account': '乙',
              'schedule': 'B', 'nominal_date': '2025-01-06',
              'amount_cents': -300}])
        self.assertEqual(self.call(close('2025-04'))['adjustments'], [])

    def test_account_totals_separate_positive_and_negative_lines(self):
        # The same account is owed a refund on one line and a surcharge on
        # another; both lines stay listed even though they cancel in the total.
        self.fresh()
        self.call(replace(
            booking('A', 'shared', start='2025-01-06T09:00'),
            booking('B', 'shared', start='2025-01-06T10:00')))
        self.assertEqual(
            {line['schedule']: line['amount_cents']
             for line in self.call(close('2025-01'))['charges']},
            {'A': 0, 'B': 120})
        # Swap which instance is charged: A moves later and now pays, B becomes
        # free.  Both differences belong to the same account and must appear.
        self.call(replace(
            booking('A', 'shared', start='2025-01-06T11:00'),
            booking('B', 'shared', start='2025-01-06T10:00')))
        february = self.call(close('2025-02'))
        self.assertEqual(
            {line['schedule']: line['amount_cents']
             for line in february['adjustments']},
            {'A': 120, 'B': -120})
        self.assertEqual(february['account_totals'],
                         [{'account': 'shared', 'amount_cents': 0}])


class LifecycleTests(LedgerCase):
    def test_close_must_start_at_first_month_and_never_skip(self):
        self.fresh(first_month='2025-03')
        self.assertRaises(ValueError, self.call, close('2025-04'))
        self.call(close('2025-03'))
        self.assertRaises(ValueError, self.call, close('2025-05'))
        self.call(close('2025-04'))
        self.assertEqual(self.call(close('2025-04'))['month'], '2025-04')

    def test_empty_month_can_be_closed(self):
        self.fresh()
        self.assertEqual(self.call(close('2025-01')),
                         {'month': '2025-01', 'charges': [],
                          'adjustments': [], 'account_totals': []})

    def test_repeated_close_returns_archived_bill_without_reposting(self):
        self.fresh()
        self.call(replace(booking('A', '甲', start='2025-01-06T09:00'),
                          booking('B', '乙', start='2025-01-06T10:00')))
        january = self.call(close('2025-01'))
        self.call(replace(booking('B', '乙', start='2025-01-06T10:00'),
                          booking('C', '丙', start='2025-01-06T11:00')))
        self.assertEqual(self.call(close('2025-01')), january)
        february = self.call(close('2025-02'))
        self.assertEqual(len(february['adjustments']), 2)

    def test_show_lists_only_closed_months_and_is_read_only(self):
        self.fresh()
        with self.assertRaises(ValueError):
            self.call(show('2025-01'))
        self.call(close('2025-01'))
        self.assertEqual(self.call(show('2025-01'))['month'], '2025-01')
        with self.assertRaises(ValueError):
            self.call(show('2025-02'))

    def test_failed_operations_leave_no_partial_update(self):
        self.fresh()
        self.call(replace(booking('A', '甲', start='2025-01-06T09:00'),
                          booking('B', '乙', start='2025-01-06T10:00')))
        january = self.call(close('2025-01'))
        before = self.call(show('2025-01'))
        bad = replace(booking('B', '乙', start='2025-01-06T10:00'),
                      {'room': 'UNKNOWN', 'account': '丙',
                       'rule': rule('C')})
        with self.assertRaises(ValueError):
            self.call(bad)
        # The rejected replace changed nothing; the ledger still bills A and B.
        self.assertEqual(self.call(show('2025-01')), before)
        self.assertEqual(self.call(close('2025-01')), january)
        february = self.call(close('2025-02'))
        self.assertEqual(february['adjustments'], [])
        with self.assertRaises(ValueError):
            self.call(close('2025-04'))
        self.assertEqual(self.call(close('2025-03')), {
            'month': '2025-03', 'charges': [], 'adjustments': [],
            'account_totals': []})

    def test_last_replace_wins_for_a_single_close(self):
        self.fresh()
        self.call(replace(booking('A', '甲', start='2025-01-06T09:00')))
        self.call(replace(booking('B', '乙', start='2025-01-06T09:00')))
        self.call(replace(booking('C', '丙', start='2025-01-06T09:00')))
        bill = self.call(close('2025-01'))
        self.assertEqual([line['schedule'] for line in bill['charges']], ['C'])
        self.assertEqual(bill['charges'][0]['amount_cents'], 0)

    def test_invalid_trailing_rule_is_validated(self):
        self.fresh()
        broken = booking('B', '乙', start='2025-01-06T10:00')
        broken['rule']['gap_policy'] = 'nonsense'
        with self.assertRaises(ValueError):
            self.call(replace(booking('A', '甲', start='2025-01-06T09:00'),
                              broken))
        # The failed replace never installed A, so January closes empty.
        self.assertEqual(self.call(close('2025-01'))['charges'], [])

    def test_duplicate_schedule_and_unknown_room_are_rejected(self):
        self.fresh()
        with self.assertRaises(ValueError):
            self.call(replace(booking('A', '甲'), booking('A', '乙')))
        with self.assertRaises(ValueError):
            self.call(replace(booking('A', '甲', room='NOPE')))

    def test_empty_replace_is_legal_and_clears_the_list(self):
        self.fresh()
        self.call(replace(booking('A', '甲', start='2025-01-06T09:00')))
        self.assertEqual(self.call(replace()), {'occurrence_count': 0})
        self.assertEqual(self.call(close('2025-01'))['charges'], [])


class BoundaryTests(LedgerCase):
    def test_more_than_128_rules_is_rejected(self):
        self.fresh()
        many = [booking('S%03d' % index, '甲',
                        start='2025-01-06T09:00')
                for index in range(129)]
        with self.assertRaises(ValueError):
            self.call(replace(*many))
        self.assertEqual(self.call(close('2025-01'))['charges'], [])

    def test_more_than_16384_instances_is_rejected(self):
        self.fresh(first_month='2020-01')
        # Weekly over ten years is ~520 instances per rule; 32 rules exceed
        # 16384 while staying within the 128-rule bound.
        huge = [booking('S%02d' % index, '甲',
                        start='2020-01-06T09:00', until='2030-01-06')
                for index in range(32)]
        with self.assertRaises(ValueError):
            self.call(replace(*huge))

    def test_non_sqlite_database_is_rejected_without_side_effects(self):
        with open(self.db, 'wb') as stream:
            stream.write(b'this is not a database')
        before = Path(self.db).read_bytes()
        with self.assertRaises(ValueError):
            self.call(close('2025-01'))
        with self.assertRaises(ValueError):
            self.call(replace(booking('A', '甲')))
        self.assertEqual(Path(self.db).read_bytes(), before)

    def test_database_with_a_foreign_schema_is_rejected(self):
        import sqlite3
        connection = sqlite3.connect(self.db)
        connection.execute('CREATE TABLE rooms (room TEXT)')
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            self.call(close('2025-01'))

    def test_month_year_bounds(self):
        self.fresh(first_month='0002-01', rooms=[
            {'room': 'R', 'free_minutes': 0, 'unit_cents': 1}])
        self.call(replace(booking('A', '甲', start='0002-01-07T09:00')))
        self.assertEqual(self.call(close('0002-01'))['month'], '0002-01')
        self.assertRaises(ValueError, self.call, close('0002-03'))
        self.assertEqual(self.call(close('0002-02'))['month'], '0002-02')
        # 9998-12 is the last addressable service month.
        self.fresh_db = os.path.join(self._dir.name, 'late.db')
        process(self.fresh_db, init(first_month='9998-12',
                                    rooms=[{'room': 'R', 'free_minutes': 0,
                                            'unit_cents': 1}]))
        process(self.fresh_db, replace(booking('A', '甲',
                                               start='9998-12-07T09:00')))
        final = process(self.fresh_db, close('9998-12'))
        self.assertEqual(final['month'], '9998-12')
        # 9998-12 is the last addressable month: no later close is possible and
        # a repeat close returns the archived bill.
        self.assertEqual(process(self.fresh_db, close('9998-12')), final)
        self.assertRaises(ValueError, process, self.fresh_db, close('9998-11'))
        self.assertRaises(ValueError, process, self.fresh_db, close('9999-01'))


if __name__ == '__main__':
    unittest.main()
