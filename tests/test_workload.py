"""Business tests for per-employee daily meeting-load statistics.

Every expected ``busy_seconds`` is a literal hand-computed from the contract
(real UTC elapsed seconds, per-employee local-day preimages, union of
intervals), not produced by re-running the implementation under test.
"""
import itertools
import random
import unittest
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from calendar_expand.model import plan
from calendar_expand.workload import evaluate


def weekday_of(day):
    return date.fromisoformat(day).isoweekday()


def oracle_result(doc):
    """Contract-level oracle: attribute every covered UTC second one by one.

    Each expanded instance contributes its integer UTC seconds to the set of
    the owning employee's local date (the employee timezone of each second is
    computed independently), so overlaps can never be counted twice and day
    boundaries come straight from zoneinfo.  This shares no code with the
    union-and-sweep implementation under test.
    """
    from_date = date.fromisoformat(doc['from_date'])
    through_date = date.fromisoformat(doc['through_date'])
    zones = {row['employee']: ZoneInfo(row['timezone'])
             for row in doc['employees']}
    busy = {row['employee']: defaultdict(set) for row in doc['employees']}
    for entry in doc['schedules']:
        owner = entry['employee']
        zone = zones[owner]
        planned = plan(entry['rule'])
        for item in planned['items']:
            start = int(item['instant'].timestamp())
            end = int(item['end'].timestamp())
            for stamp in range(start, end):
                instant = datetime.fromtimestamp(stamp, timezone.utc)
                local_date = instant.astimezone(zone).date()
                if from_date <= local_date <= through_date:
                    busy[owner][local_date].add(stamp)
    employees = []
    for row in sorted(doc['employees'], key=lambda item: item['employee']):
        days = []
        day = from_date
        while day <= through_date:
            seconds = len(busy[row['employee']][day])
            days.append({'date': day.isoformat(), 'busy_seconds': seconds,
                         'over_limit': seconds > row['daily_limit_minutes'] * 60})
            day += timedelta(days=1)
        employees.append({'employee': row['employee'],
                          'timezone': row['timezone'],
                          'daily_limit_minutes': row['daily_limit_minutes'],
                          'days': days})
    return {'from_date': from_date.isoformat(),
            'through_date': through_date.isoformat(),
            'employees': employees}


def rule(schedule, start, *, tz='UTC', weekdays=None, until=None, duration=60,
         interval=1, exceptions=(), gap='skip', fold='earlier'):
    day = start[:10]
    return {
        'schedule': schedule, 'timezone': tz, 'local_start': start,
        'interval_weeks': interval,
        'weekdays': weekdays if weekdays is not None else [weekday_of(day)],
        'until': until if until is not None else day,
        'duration_minutes': duration, 'exceptions': list(exceptions),
        'gap_policy': gap, 'fold_policy': fold,
    }


def employee(name, tz='UTC', limit=1440):
    return {'employee': name, 'timezone': tz, 'daily_limit_minutes': limit}


def schedule(owner, calendar_rule):
    return {'employee': owner, 'rule': calendar_rule}


def document(from_date='2024-01-01', through_date='2024-01-01',
             employees=(), schedules=()):
    return {'from_date': from_date, 'through_date': through_date,
            'employees': list(employees), 'schedules': list(schedules)}


def days_of(result, name):
    entry = next(item for item in result['employees'] if item['employee'] == name)
    return [(day['date'], day['busy_seconds'], day['over_limit'])
            for day in entry['days']]


class HandComputedExampleTests(unittest.TestCase):
    """The contract's worked example: union of two overlapping New York calendars."""

    def test_spec_example_union_and_over_limit(self):
        doc = document('2024-03-10', '2024-03-10',
                       [employee('alice', 'America/New_York', 100)],
                       [schedule('alice', rule('a', '2024-03-10T01:30',
                                               tz='America/New_York', duration=120)),
                        schedule('alice', rule('b', '2024-03-10T02:30',
                                               tz='America/New_York', duration=60,
                                               gap='shift_forward'))])
        result = evaluate(doc)
        # 01:30 EST -> 06:30 UTC (+120min); 02:30 is in the spring gap and
        # shifts to 03:00 EDT -> 07:00 UTC (+60min).  The UTC intervals
        # [06:30, 08:30) and [07:00, 08:00) union to exactly 7200 seconds.
        self.assertEqual(days_of(result, 'alice'),
                         [('2024-03-10', 7200, True)])
        self.assertEqual(result['employees'][0]['daily_limit_minutes'], 100)
        self.assertEqual(result['employees'][0]['timezone'], 'America/New_York')

    def test_result_envelope_fields(self):
        result = evaluate(document('2024-01-01', '2024-01-02',
                                   [employee('alice')], []))
        self.assertEqual(set(result), {'from_date', 'through_date', 'employees'})
        self.assertEqual(result['from_date'], '2024-01-01')
        self.assertEqual(result['through_date'], '2024-01-02')
        self.assertEqual(set(result['employees'][0]),
                         {'employee', 'timezone', 'daily_limit_minutes', 'days'})
        self.assertEqual(set(result['employees'][0]['days'][0]),
                         {'date', 'busy_seconds', 'over_limit'})


class DayLengthTests(unittest.TestCase):
    """DST day lengths come from the zone rules, never from 86400-second days."""

    def test_spring_forward_day_is_82800_seconds(self):
        # New York 2024-03-10 runs 05:00 UTC to 04:00 UTC next day (23 hours).
        doc = document('2024-03-10', '2024-03-10',
                       [employee('alice', 'America/New_York')],
                       [schedule('alice', rule('m', '2024-03-10T00:00',
                                               tz='America/New_York',
                                               duration=1440))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-03-10', 82800, False)])

    def test_fall_back_day_is_90000_seconds(self):
        # New York 2024-11-03 runs 04:00 UTC to 05:00 UTC next day (25 hours).
        doc = document('2024-11-03', '2024-11-03',
                       [employee('alice', 'America/New_York')],
                       [schedule('alice', rule('m', '2024-11-03T00:00',
                                               tz='America/New_York',
                                               duration=1500))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-11-03', 90000, True)])

    def test_historical_second_offset_day_length(self):
        # Paris 1911-03-10 lasts 24h09m21s = 86961s: the PMT -> WET transition
        # at midnight repeats 9m21s of the previous evening.
        doc = document('1911-03-10', '1911-03-10',
                       [employee('alice', 'Europe/Paris')],
                       [schedule('alice', rule('m', '1911-03-09T00:00',
                                               tz='Europe/Paris',
                                               until='1911-03-09',
                                               duration=10080))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('1911-03-10', 86961, True)])

    def test_skipped_local_date_has_zero_seconds(self):
        # Pacific/Apia skipped 2011-12-30 entirely; the 29th ends and the 31st
        # begins at the same UTC instant 2011-12-30T10:00:00Z.
        meetings = [schedule('alice', rule('m', '2011-12-29T12:00',
                                           tz='Pacific/Apia', duration=2880,
                                           weekdays=[4], until='2011-12-29'))]
        doc = document('2011-12-29', '2011-12-31',
                       [employee('alice', 'Pacific/Apia', 1439)], meetings)
        # 12:00-10:00 -> 22:00 UTC Dec 29, +2880min -> 22:00 UTC Dec 31.
        # Dec 29 local day is [10:00Z Dec 29, 10:00Z Dec 30): 12 hours inside.
        # Dec 30 never happens: zero.  Dec 31 is [10:00Z Dec 30, 10:00Z Dec 31):
        # the meeting covers it completely: 86400s (over the 1439-minute limit).
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2011-12-29', 43200, False),
                          ('2011-12-30', 0, False),
                          ('2011-12-31', 86400, True)])


class MidnightSplitTests(unittest.TestCase):
    def test_interval_split_across_local_midnight(self):
        doc = document('2024-01-01', '2024-01-02', [employee('alice')],
                       [schedule('alice', rule('m', '2024-01-01T23:30'))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-01', 1800, False),
                          ('2024-01-02', 1800, False)])

    def test_spring_day_midnight_split_uses_real_utc_boundary(self):
        # A meeting 22:30-00:30 local across the New York spring-forward
        # midnight: 22:30 EDT -> 02:30Z Mar 11, +120min -> 04:30Z = 00:30 EDT.
        # The Mar 10 local day ends at 04:00Z Mar 11 (23-hour day), so Mar 10
        # gets [02:30Z, 04:00Z) = 5400s and Mar 11 gets [04:00Z, 04:30Z) = 1800s.
        doc = document('2024-03-10', '2024-03-11',
                       [employee('alice', 'America/New_York')],
                       [schedule('alice', rule('m', '2024-03-10T22:30',
                                               tz='America/New_York',
                                               duration=120))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-03-10', 5400, False),
                          ('2024-03-11', 1800, False)])

    def test_fall_day_midnight_split_uses_real_utc_boundary(self):
        # 22:30-00:30 local across the fall-back midnight: 22:30 EST -> 03:30Z
        # Nov 4, +120min -> 05:30Z = 00:30 EST.  The Nov 3 local day ends at
        # 05:00Z Nov 4 (25-hour day): Nov 3 gets [03:30Z, 05:00Z) = 5400s and
        # Nov 4 gets [05:00Z, 05:30Z) = 1800s.
        doc = document('2024-11-03', '2024-11-04',
                       [employee('alice', 'America/New_York')],
                       [schedule('alice', rule('m', '2024-11-03T22:30',
                                               tz='America/New_York',
                                               duration=120))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-11-03', 5400, False),
                          ('2024-11-04', 1800, False)])

    def test_range_edges_clip_without_counting_outside_days(self):
        # Only 2024-01-02 is reported; occupancy on Jan 1 and Jan 3 is dropped,
        # not folded into the reported day.
        doc = document('2024-01-02', '2024-01-02', [employee('alice')],
                       [schedule('alice', rule('before', '2024-01-01T23:00',
                                               duration=120)),
                        schedule('alice', rule('after', '2024-01-02T23:00',
                                               duration=120))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-02', 7200, False)])


class UnionTests(unittest.TestCase):
    def test_overlapping_calendars_union_not_sum(self):
        # [10:00,12:00) + [11:00,13:00) + [12:30,14:00) union to 4 hours;
        # per-calendar sums would give 5.5 hours.
        doc = document('2024-01-01', '2024-01-01', [employee('alice')],
                       [schedule('alice', rule('r1', '2024-01-01T10:00', duration=120)),
                        schedule('alice', rule('r2', '2024-01-01T11:00', duration=120)),
                        schedule('alice', rule('r3', '2024-01-01T12:30', duration=90))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-01', 14400, False)])

    def test_touching_endpoints_do_not_overlap(self):
        doc = document('2024-01-01', '2024-01-01', [employee('alice')],
                       [schedule('alice', rule('r1', '2024-01-01T09:00')),
                        schedule('alice', rule('r2', '2024-01-01T10:00'))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-01', 7200, False)])

    def test_employees_are_independent(self):
        # Same UTC hour booked by two employees: each gets their own 3600s.
        doc = document('2024-01-01', '2024-01-01',
                       [employee('alice'), employee('bob')],
                       [schedule('alice', rule('ra', '2024-01-01T09:00')),
                        schedule('bob', rule('rb', '2024-01-01T09:00'))])
        result = evaluate(doc)
        self.assertEqual(days_of(result, 'alice'), [('2024-01-01', 3600, False)])
        self.assertEqual(days_of(result, 'bob'), [('2024-01-01', 3600, False)])

    def test_overlap_across_midnight_unions_per_day(self):
        # Two calendars overlapping across midnight: per-day union on both sides.
        doc = document('2024-01-01', '2024-01-02', [employee('alice')],
                       [schedule('alice', rule('r1', '2024-01-01T22:00', duration=240)),
                        schedule('alice', rule('r2', '2024-01-01T23:30', duration=120))])
        # Union [22:00, 02:00+1d): Jan 1 gets 2h, Jan 2 gets 2h.
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-01', 7200, False),
                          ('2024-01-02', 7200, False)])


class HistoricalOffsetTests(unittest.TestCase):
    def test_paris_1900_midnight_boundary_keeps_seconds(self):
        # Paris 1900 runs on PMT (+00:09:21): local midnight is 23:50:39 UTC.
        # A meeting ending exactly at that instant belongs fully to Jan 1;
        # one starting at local midnight belongs fully to Jan 2.
        doc = document('1900-01-01', '1900-01-02',
                       [employee('alice', 'Europe/Paris')],
                       [schedule('alice', rule('before', '1900-01-01T23:59',
                                               tz='Europe/Paris', duration=1)),
                        schedule('alice', rule('after', '1900-01-02T00:00',
                                               tz='Europe/Paris', duration=1))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('1900-01-01', 60, False),
                          ('1900-01-02', 60, False)])

    def test_historical_offset_utc_attribution(self):
        # The same Paris 1900 meeting seen by a UTC employee: 08:50:39 UTC.
        doc = document('1900-01-01', '1900-01-01', [employee('alice', 'UTC')],
                       [schedule('alice', rule('m', '1900-01-01T09:00',
                                               tz='Europe/Paris'))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('1900-01-01', 3600, False)])


class TimezoneSeparationTests(unittest.TestCase):
    def test_rule_timezone_and_employee_timezone_are_independent(self):
        # One New York rule at 15:30 EDT = 19:30 UTC Mar 10 = 04:30 JST Mar 11.
        meeting = rule('m', '2024-03-10T15:30', tz='America/New_York')
        doc = document('2024-03-10', '2024-03-11',
                       [employee('emp-ny', 'America/New_York'),
                        employee('emp-tokyo', 'Asia/Tokyo'),
                        employee('emp-utc', 'UTC')],
                       [schedule('emp-ny', meeting),
                        schedule('emp-tokyo', dict(meeting, schedule='m2')),
                        schedule('emp-utc', dict(meeting, schedule='m3'))])
        result = evaluate(doc)
        self.assertEqual(days_of(result, 'emp-ny'),
                         [('2024-03-10', 3600, False), ('2024-03-11', 0, False)])
        self.assertEqual(days_of(result, 'emp-utc'),
                         [('2024-03-10', 3600, False), ('2024-03-11', 0, False)])
        self.assertEqual(days_of(result, 'emp-tokyo'),
                         [('2024-03-10', 0, False), ('2024-03-11', 3600, False)])


class OrderingAndShapeTests(unittest.TestCase):
    def test_employees_sorted_by_code_point_and_days_ascending(self):
        doc = document('2024-01-02', '2024-01-03',
                       [employee('b'), employee('A'), employee('a')], [])
        result = evaluate(doc)
        self.assertEqual([item['employee'] for item in result['employees']],
                         ['A', 'a', 'b'])
        for item in result['employees']:
            self.assertEqual([day['date'] for day in item['days']],
                             ['2024-01-02', '2024-01-03'])

    def test_empty_employees_produce_empty_report(self):
        result = evaluate(document('2024-01-01', '2024-01-03', [], []))
        self.assertEqual(result['employees'], [])

    def test_employees_without_schedules_have_zero_days(self):
        result = evaluate(document('2024-01-01', '2024-01-03',
                                   [employee('alice', limit=0)], []))
        self.assertEqual(days_of(result, 'alice'),
                         [('2024-01-01', 0, False),
                          ('2024-01-02', 0, False),
                          ('2024-01-03', 0, False)])

    def test_empty_expansion_rule_is_legal(self):
        # The only occurrence is excluded by exceptions: zero instances.
        meeting = rule('m', '2024-01-01T09:00', exceptions=['2024-01-01'])
        result = evaluate(document('2024-01-01', '2024-01-01',
                                   [employee('alice')],
                                   [schedule('alice', meeting)]))
        self.assertEqual(days_of(result, 'alice'), [('2024-01-01', 0, False)])

    def test_every_range_day_present_exactly_once(self):
        result = evaluate(document('2024-01-01', '2024-01-31',
                                   [employee('alice')], []))
        days = result['employees'][0]['days']
        self.assertEqual(len(days), 31)
        self.assertEqual(len({day['date'] for day in days}), 31)

    def test_permutation_of_arrays_does_not_change_result(self):
        employees = [employee('alice', 'America/New_York', 100),
                     employee('bob', 'Asia/Tokyo', 300),
                     employee('carol', 'UTC', 0)]
        meetings = [schedule('alice', rule('s1', '2024-03-10T01:30',
                                           tz='America/New_York', duration=120)),
                    schedule('alice', rule('s2', '2024-03-10T02:30',
                                           tz='America/New_York', duration=60,
                                           gap='shift_forward')),
                    schedule('bob', rule('s3', '2024-03-10T23:30',
                                         tz='Asia/Tokyo', duration=120)),
                    schedule('carol', rule('s4', '2024-03-11T09:00')),
                    schedule('bob', rule('s5', '2024-03-11T00:30',
                                         tz='Asia/Tokyo', duration=45))]
        base = evaluate(document('2024-03-10', '2024-03-11', employees, meetings))
        for emp_perm in itertools.permutations(employees):
            for sch_perm in (meetings, list(reversed(meetings)),
                             [meetings[2], meetings[4], meetings[0],
                              meetings[3], meetings[1]]):
                doc = document('2024-03-10', '2024-03-11', list(emp_perm),
                               list(sch_perm))
                self.assertEqual(evaluate(doc), base)


class LimitTests(unittest.TestCase):
    def test_equal_to_limit_is_not_over(self):
        doc = document('2024-01-01', '2024-01-01', [employee('alice', limit=100)],
                       [schedule('alice', rule('m', '2024-01-01T09:00',
                                               duration=100))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-01', 6000, False)])

    def test_one_minute_over_limit(self):
        doc = document('2024-01-01', '2024-01-01', [employee('alice', limit=100)],
                       [schedule('alice', rule('m', '2024-01-01T09:00',
                                               duration=101))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-01', 6060, True)])

    def test_zero_limit(self):
        doc = document('2024-01-01', '2024-01-02', [employee('alice', limit=0)],
                       [schedule('alice', rule('m', '2024-01-01T09:00'))])
        self.assertEqual(days_of(evaluate(doc), 'alice'),
                         [('2024-01-01', 3600, True),
                          ('2024-01-02', 0, False)])


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, doc):
        with self.assertRaises((ValueError, TypeError)):
            evaluate(doc)

    def test_root_fields_are_exact(self):
        extra = document(employees=[], schedules=[])
        extra['comment'] = 1
        self.assert_invalid(extra)
        for key in ('from_date', 'through_date', 'employees', 'schedules'):
            missing = document(employees=[], schedules=[])
            del missing[key]
            self.assert_invalid(missing)

    def test_date_format_and_validity(self):
        for bad in ('2024-3-10', '20240310', '2024-02-30', '10/03/2024', 20240310,
                    '2024-01-01T00:00', ''):
            with self.subTest(bad=bad):
                self.assert_invalid(document(bad, '2024-01-01', [], []))
                self.assert_invalid(document('2024-01-01', bad, [], []))

    def test_range_bounds(self):
        self.assert_invalid(document('2024-01-02', '2024-01-01', [], []))
        # 3660 inclusive days are allowed, 3661 are not.
        evaluate(document('2024-01-01', '2034-01-07', [], []))
        self.assert_invalid(document('2024-01-01', '2034-01-08', [], []))

    def test_employee_validation(self):
        self.assert_invalid(document(employees=[employee('a'), employee('a')]))
        self.assert_invalid(document(employees=[employee('a', 'Mars/Olympus')]))
        self.assert_invalid(document(employees=[employee('a', limit=True)]))
        self.assert_invalid(document(employees=[employee('a', limit=-1)]))
        self.assert_invalid(document(employees=[employee('a', limit=1441)]))
        self.assert_invalid(document(employees=[employee('a', limit=1.5)]))
        self.assert_invalid(document(employees=[employee('a', limit='60')]))
        self.assert_invalid(document(employees=[employee('')]))
        self.assert_invalid(document(employees=[employee(' x')]))
        self.assert_invalid(document(employees=[employee(123)]))
        row = employee('a')
        row['note'] = 1
        self.assert_invalid(document(employees=[row]))
        self.assert_invalid(document(employees=[{'employee': 'a',
                                                 'timezone': 'UTC'}]))
        self.assert_invalid(document(employees=['a']))
        self.assert_invalid(document(employees='alice'))

    def test_employee_and_schedule_count_bounds(self):
        self.assert_invalid(document(
            employees=[employee('e%02d' % i) for i in range(33)]))
        evaluate(document(employees=[employee('e%02d' % i) for i in range(32)]))
        self.assert_invalid(document(
            employees=[employee('a')],
            schedules=[schedule('a', rule('s%02d' % i, '2024-01-01T09:00'))
                       for i in range(33)]))

    def test_schedule_validation(self):
        self.assert_invalid(document(employees=[employee('a')],
                                     schedules=[schedule('ghost', rule('r', '2024-01-01T09:00'))]))
        both = [schedule('a', rule('same', '2024-01-01T09:00')),
                schedule('a', rule('same', '2024-01-01T10:00'))]
        self.assert_invalid(document(employees=[employee('a')], schedules=both))
        self.assert_invalid(document(employees=[employee('a')],
                                     schedules=[schedule('a', 'not-a-dict')]))
        self.assert_invalid(document(employees=[employee('a')],
                                     schedules=[{'employee': 'a'}]))
        bad_rule = rule('r', '2024-01-01T09:00')
        bad_rule['weekdays'] = []
        self.assert_invalid(document(employees=[employee('a')],
                                     schedules=[schedule('a', bad_rule)]))
        bad_tz = rule('r', '2024-01-01T09:00', tz='Mars/Olympus')
        self.assert_invalid(document(employees=[employee('a')],
                                     schedules=[schedule('a', bad_tz)]))

    def test_instance_count_bound(self):
        many = [schedule('a', rule('r%02d' % i, '2024-01-01T09:00',
                                   weekdays=[1, 2, 3, 4, 5, 6, 7],
                                   until='2024-12-31'))
                for i in range(23)]  # 23 x 366 = 8418 > 8192
        self.assert_invalid(document(employees=[employee('a')], schedules=many))
        within = many[:22]  # 22 x 366 = 8052 <= 8192
        evaluate(document('2024-01-01', '2024-01-01',
                          employees=[employee('a')], schedules=within))

    def test_trailing_invalid_items_are_not_hidden(self):
        # An early empty expansion and an over-limit day are successful
        # business outcomes; they must not skip validation of later items.
        leading_empty = schedule('a', rule('empty', '2024-01-01T09:00',
                                           exceptions=['2024-01-01']))
        over_limit = schedule('a', rule('heavy', '2024-01-02T09:00',
                                        duration=1440))
        for trailing in (
                schedule('a', rule('bad-tz', '2024-01-01T09:00',
                                   tz='Mars/Olympus')),
                schedule('ghost', rule('ok', '2024-01-01T09:00')),
                schedule('a', rule('heavy', '2024-01-01T09:00')),  # duplicate id
                {'employee': 'a'},
                schedule('a', 'not-a-dict')):
            with self.subTest(trailing=trailing):
                doc = document('2024-01-01', '2024-01-02',
                               [employee('a', limit=1)],
                               [leading_empty, over_limit, trailing])
                self.assert_invalid(doc)


class RandomizedOracleTests(unittest.TestCase):
    """Cross-check the sweep against the per-second oracle on random documents."""

    WINDOWS = [('2024-03-08', 5), ('2024-11-01', 5), ('2024-01-01', 4),
               ('1900-01-01', 3), ('1911-03-08', 4), ('2011-12-28', 5),
               ('2019-09-06', 4), ('2024-04-05', 4)]
    ZONES = ['UTC', 'America/New_York', 'Europe/Paris', 'Asia/Tokyo',
             'Pacific/Apia', 'America/Santiago']

    def random_document(self, rng):
        window_start, span = rng.choice(self.WINDOWS)
        first = date.fromisoformat(window_start)
        from_date = first + timedelta(days=rng.randrange(span))
        through_date = from_date + timedelta(days=rng.randrange(3))
        employees = []
        for index in range(rng.randrange(4)):
            employees.append(employee('emp%d' % index, rng.choice(self.ZONES),
                                      rng.choice([0, 60, 240, 1440])))
        schedules = []
        for index in range(rng.randrange(6)):
            if not employees:
                break
            day = first + timedelta(days=rng.randrange(span + 2))
            start = '%sT%02d:%02d' % (day.isoformat(), rng.randrange(24),
                                      rng.choice([0, 15, 30, 45, 59]))
            schedules.append(schedule(
                rng.choice(employees)['employee'],
                rule('cal%d' % index, start,
                     tz=rng.choice(self.ZONES),
                     duration=rng.choice([1, 30, 60, 90, 120, 240]),
                     gap=rng.choice(['skip', 'shift_forward']),
                     fold=rng.choice(['earlier', 'later']))))
        return document(from_date.isoformat(), through_date.isoformat(),
                        employees, schedules)

    def test_random_documents_match_oracle(self):
        rng = random.Random(20241103)
        for _ in range(120):
            doc = self.random_document(rng)
            with self.subTest(doc=doc):
                try:
                    expected = oracle_result(doc)
                except ValueError:
                    # Unresolvable rule (for example a shift_forward start
                    # inside Apia's 24-hour skipped date): an input error.
                    with self.assertRaises(ValueError):
                        evaluate(doc)
                else:
                    self.assertEqual(evaluate(doc), expected)


if __name__ == '__main__':
    unittest.main()
