import copy
import random
import unittest
from bisect import bisect_left
from datetime import date

from calendar_expand.admit import evaluate, validate
from calendar_expand.model import analyze


def rule(request_id, start='2024-01-01T09:00', until=None, weekdays=None, duration=60,
         timezone='UTC', interval=1, exceptions=(), gap='skip', fold='earlier'):
    """Build one complete recurrence rule; the schedule id is the request id."""
    day = date.fromisoformat(start[:10])
    return {
        'schedule': request_id,
        'timezone': timezone,
        'local_start': start,
        'interval_weeks': interval,
        'weekdays': weekdays if weekdays is not None else [day.isoweekday()],
        'until': until if until is not None else start[:10],
        'duration_minutes': duration,
        'exceptions': list(exceptions),
        'gap_policy': gap,
        'fold_policy': fold,
    }


def request(request_id, priority, mandatory, demands, **kwargs):
    return {
        'rule': rule(request_id, **kwargs),
        'priority': priority,
        'mandatory': mandatory,
        'demands': [{'resource': name, 'units': units} for name, units in demands],
    }


def witness(start, end, resource, load, capacity, active):
    return {'start_utc': start, 'end_utc': end, 'resource': resource,
            'load': load, 'capacity': capacity, 'active_ids': active}


class AdmissionTests(unittest.TestCase):
    def test_capacity_two_three_identical_groups_tie(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                request('a', 5, False, [('room', 1)]),
                request('b', 3, False, [('room', 1)]),
                request('c', 3, False, [('room', 1)]),
            ],
        }
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['a', 'b'],
            'rejected_ids': ['c'],
            'total_priority': 8,
            'rejections': [{'request_id': 'c',
                            'witness': witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                               'room', 3, 2, ['a', 'b', 'c'])}],
            'mandatory_conflict': None,
        })

    def test_capacity_two_priority_decides_which_pair(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                request('a', 5, False, [('room', 1)]),
                request('b', 3, False, [('room', 1)]),
                request('c', 4, False, [('room', 1)]),
            ],
        }
        result = evaluate(data)
        self.assertEqual(result['accepted_ids'], ['a', 'c'])
        self.assertEqual(result['rejected_ids'], ['b'])
        self.assertEqual(result['total_priority'], 9)
        self.assertEqual(result['rejections'][0]['witness'],
                         witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                 'room', 3, 2, ['a', 'b', 'c']))

    def test_self_overlap_and_cross_group_share_one_decision(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                # Two 25-hour instances on consecutive days overlap for one
                # hour on January 2nd, so this request alone carries load 2.
                request('a', 5, False, [('room', 1)], start='2024-01-01T09:00',
                        until='2024-01-02', weekdays=[1, 2], duration=1500),
                request('b', 9, False, [('room', 1)], start='2024-01-02T09:30',
                        duration=30),
            ],
        }
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['b'],
            'rejected_ids': ['a'],
            'total_priority': 9,
            'rejections': [{'request_id': 'a',
                            'witness': witness('2024-01-02T09:30:00Z', '2024-01-02T10:00:00Z',
                                               'room', 3, 2, ['a', 'b'])}],
            'mandatory_conflict': None,
        })

    def test_two_resources_constrain_together(self):
        data = {
            'resources': [{'resource': 'r1', 'capacity': 1},
                          {'resource': 'r2', 'capacity': 1}],
            'requests': [
                request('a', 5, False, [('r1', 1), ('r2', 1)]),
                request('b', 4, False, [('r1', 1)]),
                request('c', 4, False, [('r2', 1)]),
            ],
        }
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['b', 'c'],
            'rejected_ids': ['a'],
            'total_priority': 8,
            'rejections': [{'request_id': 'a',
                            'witness': witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                               'r1', 2, 1, ['a', 'b'])}],
            'mandatory_conflict': None,
        })

    def test_global_optimum_beats_priority_descending_greedy(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('big', 10, False, [('room', 1)], start='2024-01-01T09:00',
                        duration=180),
                request('x', 6, False, [('room', 1)], start='2024-01-01T09:00'),
                request('y', 6, False, [('room', 1)], start='2024-01-01T11:00'),
            ],
        }
        # Taking the highest priority first gives 10; the optimum is 12.
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['x', 'y'],
            'rejected_ids': ['big'],
            'total_priority': 12,
            'rejections': [{'request_id': 'big',
                            'witness': witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                               'room', 2, 1, ['big', 'x'])}],
            'mandatory_conflict': None,
        })

    def test_unit_knapsack_beats_priority_greedy(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 10}],
            'requests': [
                request('a', 10, False, [('room', 7)]),
                request('b', 9, False, [('room', 5)]),
                request('c', 9, False, [('room', 5)]),
            ],
        }
        # Greedy takes a (10), leaving no room for b or c: total 10.  Taking
        # b and c uses exactly the 10 units and scores 18.
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['b', 'c'],
            'rejected_ids': ['a'],
            'total_priority': 18,
            'rejections': [{'request_id': 'a',
                            'witness': witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                               'room', 17, 10, ['a', 'b', 'c'])}],
            'mandatory_conflict': None,
        })

    def test_tie_between_different_length_id_lists(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('m', 5, False, [('room', 1)], start='2024-01-01T09:00',
                        duration=180),
                request('a', 3, False, [('room', 1)], start='2024-01-01T09:00'),
                request('c', 2, False, [('room', 1)], start='2024-01-01T11:00'),
            ],
        }
        # {m} and {a, c} both score 5; ['a', 'c'] sorts before ['m'].
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['a', 'c'],
            'rejected_ids': ['m'],
            'total_priority': 5,
            'rejections': [{'request_id': 'm',
                            'witness': witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                               'room', 2, 1, ['a', 'm'])}],
            'mandatory_conflict': None,
        })

    def test_touching_endpoints_are_not_simultaneous(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('a', 5, False, [('room', 1)], start='2024-01-01T09:00'),
                request('b', 5, False, [('room', 1)], start='2024-01-01T10:00'),
            ],
        }
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['a', 'b'],
            'rejected_ids': [],
            'total_priority': 10,
            'rejections': [],
            'mandatory_conflict': None,
        })

    def test_repeated_week_instances_add_up(self):
        # Two consecutive 25-hour instances overlap once; capacity 2 absorbs a
        # single extra unit but not its own multiplicity plus a second request.
        base = dict(start='2024-01-01T09:00', until='2024-01-02', weekdays=[1, 2],
                    duration=1500)
        only = evaluate({
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [request('long', 4, False, [('room', 1)], **base)],
        })
        self.assertEqual(only, {
            'feasible': True,
            'accepted_ids': ['long'],
            'rejected_ids': [],
            'total_priority': 4,
            'rejections': [],
            'mandatory_conflict': None,
        })
        with_neighbor = evaluate({
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [request('long', 4, False, [('room', 1)], **base),
                         request('other', 8, False, [('room', 1)],
                                 start='2024-01-02T09:30', duration=30)],
        })
        self.assertEqual(with_neighbor['accepted_ids'], ['other'])
        self.assertEqual(with_neighbor['rejections'][0]['witness'],
                         witness('2024-01-02T09:30:00Z', '2024-01-02T10:00:00Z',
                                 'room', 3, 2, ['long', 'other']))

    def test_cross_timezone_same_utc_instant_conflicts(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('ny', 5, False, [('room', 1)], timezone='America/New_York',
                        start='2024-01-01T04:00'),
                request('tk', 3, False, [('room', 1)], timezone='Asia/Tokyo',
                        start='2024-01-01T18:00'),
            ],
        }
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['ny'],
            'rejected_ids': ['tk'],
            'total_priority': 5,
            'rejections': [{'request_id': 'tk',
                            'witness': witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                               'room', 2, 1, ['ny', 'tk'])}],
            'mandatory_conflict': None,
        })

    def test_spring_gap_policy_changes_the_optimal_set(self):
        shared = dict(timezone='America/New_York', start='2024-03-04T02:30',
                      until='2024-03-10', weekdays=[1, 7])
        other = dict(timezone='America/New_York', start='2024-03-10T03:00',
                     until='2024-03-10', weekdays=[7])

        skipped = evaluate({
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [request('a', 5, False, [('room', 1)], gap='skip', **shared),
                         request('b', 1, False, [('room', 1)], **other)],
        })
        self.assertEqual(skipped['accepted_ids'], ['a', 'b'])
        self.assertEqual(skipped['total_priority'], 6)
        self.assertEqual(skipped['rejections'], [])

        shifted = evaluate({
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [request('a', 5, False, [('room', 1)], gap='shift_forward', **shared),
                         request('b', 1, False, [('room', 1)], **other)],
        })
        self.assertEqual(shifted, {
            'feasible': True,
            'accepted_ids': ['a'],
            'rejected_ids': ['b'],
            'total_priority': 5,
            'rejections': [{'request_id': 'b',
                            'witness': witness('2024-03-10T07:00:00Z', '2024-03-10T08:00:00Z',
                                               'room', 2, 1, ['a', 'b'])}],
            'mandatory_conflict': None,
        })

    def test_fall_fold_policy_changes_the_overlap(self):
        other = dict(start='2024-11-03T05:30')

        earlier = evaluate({
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [request('a', 5, False, [('room', 1)], timezone='America/New_York',
                                 start='2024-11-03T01:30', fold='earlier'),
                         request('b', 7, False, [('room', 1)], **other)],
        })
        self.assertEqual(earlier['accepted_ids'], ['b'])
        self.assertEqual(earlier['total_priority'], 7)
        self.assertEqual(earlier['rejections'][0]['witness'],
                         witness('2024-11-03T05:30:00Z', '2024-11-03T06:30:00Z',
                                 'room', 2, 1, ['a', 'b']))

        later = evaluate({
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [request('a', 5, False, [('room', 1)], timezone='America/New_York',
                                 start='2024-11-03T01:30', fold='later'),
                         request('b', 7, False, [('room', 1)], **other)],
        })
        self.assertEqual(later['accepted_ids'], ['a', 'b'])
        self.assertEqual(later['total_priority'], 12)
        self.assertEqual(later['rejections'], [])

    def test_mandatory_overload_is_business_infeasible(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                request('m1', 4, True, [('room', 2)], start='2024-01-01T09:00'),
                request('m2', 5, True, [('room', 1)], start='2024-01-01T09:30',
                        duration=60),
                request('o', 9, False, [('room', 1)]),
            ],
        }
        self.assertEqual(evaluate(data), {
            'feasible': False,
            'accepted_ids': [],
            'rejected_ids': ['m1', 'm2', 'o'],
            'total_priority': None,
            'rejections': [],
            'mandatory_conflict': witness('2024-01-01T09:30:00Z', '2024-01-01T10:00:00Z',
                                          'room', 3, 2, ['m1', 'm2']),
        })

    def test_mandatory_requests_are_counted_once_and_pinned(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                request('m', 40, True, [('room', 1)]),
                request('o1', 7, False, [('room', 1)]),
                request('o2', 7, False, [('room', 1)]),
            ],
        }
        result = evaluate(data)
        self.assertEqual(result['accepted_ids'], ['m', 'o1'])
        self.assertEqual(result['rejected_ids'], ['o2'])
        self.assertEqual(result['total_priority'], 47)

    def test_mandatory_ids_are_not_in_rejections(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                request('m', 1, True, [('room', 1)]),
                request('a', 5, False, [('room', 2)]),
                request('b', 5, False, [('room', 2)]),
            ],
        }
        result = evaluate(data)
        self.assertEqual(result['rejected_ids'], ['a', 'b'])
        self.assertEqual([item['request_id'] for item in result['rejections']], ['a', 'b'])

    def test_witness_uses_only_final_set_not_all_requests(self):
        # A loud early rejection in an unrelated early hour must not be offered
        # as the witness: the reported segment is the 10:00 conflict.
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('noise', 1, False, [('room', 1)],
                        start='2024-01-01T00:00', duration=60),
                request('m', 5, True, [('room', 1)], start='2024-01-01T09:00'),
                request('late', 6, False, [('room', 1)], start='2024-01-01T10:00'),
                request('third', 2, False, [('room', 1)], start='2024-01-01T10:00'),
            ],
        }
        result = evaluate(data)
        self.assertEqual(result['accepted_ids'], ['late', 'm', 'noise'])
        self.assertEqual(result['rejected_ids'], ['third'])
        self.assertEqual(result['rejections'],
                         [{'request_id': 'third',
                           'witness': witness('2024-01-01T10:00:00Z',
                                              '2024-01-01T11:00:00Z',
                                              'room', 2, 1, ['late', 'third'])}])

    def test_witness_does_not_borrow_an_unrelated_peak(self):
        # `noise` was rejected, but its 00:00 conflict with `m` is impossible:
        # `m` is at 09:00.  The witness for `noise` is the real 00:00 result of
        # adding it to the final set; it must not cite `m` merely because `m`
        # exists.
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('noise', 1, False, [('room', 1)],
                        start='2024-01-01T00:00', duration=60),
                request('busy', 4, False, [('room', 1)],
                        start='2024-01-01T00:00', duration=60),
                request('m', 5, True, [('room', 1)], start='2024-01-01T09:00'),
            ],
        }
        result = evaluate(data)
        self.assertEqual(result['accepted_ids'], ['busy', 'm'])
        self.assertEqual(result['rejected_ids'], ['noise'])
        self.assertEqual(result['rejections'],
                         [{'request_id': 'noise',
                           'witness': witness('2024-01-01T00:00:00Z',
                                              '2024-01-01T01:00:00Z',
                                              'room', 2, 1, ['busy', 'noise'])}])

    def test_zero_features_are_not_simultaneous_with_a_later_start(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('a', 5, False, [('room', 1)], start='2024-01-01T09:00',
                        duration=60),
                request('b', 5, False, [('room', 1)], start='2024-01-01T10:00',
                        duration=60),
                request('c', 5, False, [('room', 1)], start='2024-01-01T11:00',
                        duration=60),
            ],
        }
        # Three touching one-hour slots on capacity 1 all fit, un-lengthed.
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['a', 'b', 'c'],
            'rejected_ids': [],
            'total_priority': 15,
            'rejections': [],
            'mandatory_conflict': None,
        })

    def test_identifier_order_is_unicode_code_point(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                request('Z', 9, False, [('room', 1)]),
                request('a', 5, False, [('room', 1)]),
                request('B', 5, False, [('room', 1)]),
            ],
        }
        # {'B', 'Z'} and {'Z', 'a'} both score 14; compare sorted lists by code
        # point: ['B', 'Z'] < ['Z', 'a'].
        self.assertEqual(evaluate(data), {
            'feasible': True,
            'accepted_ids': ['B', 'Z'],
            'rejected_ids': ['a'],
            'total_priority': 14,
            'rejections': [{'request_id': 'a',
                            'witness': witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                               'room', 3, 2, ['B', 'Z', 'a'])}],
            'mandatory_conflict': None,
        })

    def test_input_permutations_do_not_change_the_result(self):
        base = {
            'resources': [{'resource': 'r1', 'capacity': 1},
                          {'resource': 'r2', 'capacity': 1}],
            'requests': [
                request('a', 5, False, [('r1', 1), ('r2', 1)]),
                request('b', 4, False, [('r1', 1)]),
                request('c', 4, False, [('r2', 1)]),
            ],
        }
        expected = evaluate(copy.deepcopy(base))
        permuted = copy.deepcopy(base)
        permuted['resources'].reverse()
        permuted['requests'].reverse()
        for item in permuted['requests']:
            item['demands'].reverse()
        self.assertEqual(evaluate(permuted), expected)
        self.assertEqual(evaluate(copy.deepcopy(base)), expected)

    def test_empty_document_is_feasible_zero(self):
        self.assertEqual(evaluate({'resources': [], 'requests': []}), {
            'feasible': True,
            'accepted_ids': [],
            'rejected_ids': [],
            'total_priority': 0,
            'rejections': [],
            'mandatory_conflict': None,
        })

    def test_declared_but_unused_resource_is_harmless(self):
        data = {
            'resources': [{'resource': 'idle', 'capacity': 1},
                          {'resource': 'room', 'capacity': 1}],
            'requests': [request('a', 3, False, [('room', 1)])],
        }
        result = evaluate(data)
        self.assertTrue(result['feasible'])
        self.assertEqual(result['accepted_ids'], ['a'])

    def test_optional_that_cannot_fit_alone_is_rejected_with_witness(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('m', 1, True, [('room', 1)]),
                request('big', 100, False, [('room', 2)]),
            ],
        }
        result = evaluate(data)
        self.assertEqual(result['accepted_ids'], ['m'])
        self.assertEqual(result['rejected_ids'], ['big'])
        self.assertEqual(result['rejections'][0]['witness'],
                         witness('2024-01-01T09:00:00Z', '2024-01-01T10:00:00Z',
                                 'room', 3, 1, ['big', 'm']))
        self.assertEqual(result['total_priority'], 1)


class AdmissionOracleTests(unittest.TestCase):
    """Independent exhaustive oracle written from the contract, not the solver."""

    @staticmethod
    def collect(data):
        """Naive instance grid: every occurrence kept as its own interval."""
        capacity = {row['resource']: row['capacity'] for row in data['resources']}
        requests = []
        for row in data['requests']:
            occurrences = []
            for occurrence in analyze(row['rule'])['occurrences']:
                from datetime import datetime
                start = datetime.fromisoformat(occurrence['start_utc'].replace('Z', '+00:00'))
                end = datetime.fromisoformat(occurrence['end_utc'].replace('Z', '+00:00'))
                occurrences.append((int(start.timestamp() // 60), int(end.timestamp() // 60)))
            demands = {item['resource']: item['units'] for item in row['demands']}
            requests.append({'id': row['rule']['schedule'], 'priority': row['priority'],
                             'mandatory': row['mandatory'], 'demands': demands,
                             'occurrences': occurrences})
        return capacity, requests

    @classmethod
    def overflow(cls, capacity, requests, chosen):
        """First over-capacity basic segment for a chosen set, or None."""
        best = None
        for resource in sorted(capacity):
            intervals = [(start, end, request['demands'][resource])
                         for request in chosen for start, end in request['occurrences']
                         if resource in request['demands']]
            if not intervals:
                continue
            points = sorted({edge for start, end, _ in intervals for edge in (start, end)})
            delta = [0] * len(points)
            for start, end, units in intervals:
                delta[bisect_left(points, start)] += units
                delta[bisect_left(points, end)] -= units
            running = 0
            for position in range(len(points) - 1):
                running += delta[position]
                if running > capacity[resource]:
                    key = (points[position], resource)
                    if best is None or key < best[0]:
                        active = sorted(request['id'] for request in chosen
                                        if any(lo <= points[position] and points[position + 1] <= hi
                                               for lo, hi in request['occurrences'])
                                        and resource in request['demands'])
                        from datetime import datetime, timezone
                        fmt = lambda m: datetime.fromtimestamp(m * 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:00Z')
                        best = (key, {'start_utc': fmt(points[position]),
                                      'end_utc': fmt(points[position + 1]),
                                      'resource': resource, 'load': running,
                                      'capacity': capacity[resource], 'active_ids': active})
                    break
        return None if best is None else best[1]

    @classmethod
    def expected(cls, data):
        capacity, requests = cls.collect(data)
        mandatory = [request for request in requests if request['mandatory']]
        if cls.overflow(capacity, requests, mandatory) is not None:
            return {'feasible': False,
                    'accepted_ids': [],
                    'rejected_ids': sorted(request['id'] for request in requests),
                    'total_priority': None,
                    'rejections': [],
                    'mandatory_conflict': cls.overflow(capacity, requests, mandatory)}
        optional = [request for request in requests if not request['mandatory']]
        best = None
        for mask in range(1 << len(optional)):
            chosen = mandatory + [optional[index] for index in range(len(optional))
                                  if mask >> index & 1]
            if cls.overflow(capacity, requests, chosen) is not None:
                continue
            ids = sorted(request['id'] for request in chosen)
            score = sum(request['priority'] for request in chosen)
            if best is None or (-score, ids) < best[:2]:
                best = (-score, ids, chosen)
        _, ids, chosen = best
        rejections = []
        for request in sorted((r for r in requests if r['id'] not in set(ids)),
                              key=lambda r: r['id']):
            rejections.append({'request_id': request['id'],
                               'witness': cls.overflow(capacity, requests, chosen + [request])})
        return {'feasible': True,
                'accepted_ids': ids,
                'rejected_ids': [request['id'] for request in
                                 sorted((r for r in requests if r['id'] not in set(ids)),
                                        key=lambda r: r['id'])],
                'total_priority': sum(request['priority'] for request in chosen),
                'rejections': rejections,
                'mandatory_conflict': None}

    def test_random_instances_match_exhaustive_oracle(self):
        generator = random.Random(163)
        for trial in range(250):
            with self.subTest(trial=trial):
                resources = [{'resource': f'r{index}', 'capacity': generator.randint(1, 4)}
                             for index in range(generator.randint(1, 3))]
                requests = []
                for index in range(generator.randint(1, 7)):
                    day = generator.randint(1, 3)
                    start = f'2024-01-{day:02d}T{generator.randint(8, 12):02d}:00'
                    until = f'2024-01-{day + 6:02d}'
                    requests.append(request(
                        f'q{index:02d}', generator.randint(1, 6),
                        generator.random() < 0.25,
                        [(f'r{generator.randrange(len(resources))}',
                          generator.randint(1, 2))
                         for _ in range(generator.randint(1, len(resources)))],
                        start=start, until=until,
                        weekdays=sorted(generator.sample([1, 2, 3, 4, 5, 6, 7],
                                                         generator.randint(1, 2))),
                        duration=generator.choice([30, 60, 120, 300])))
                    # enforce unique demand resources without changing intent
                    seen, unique = set(), []
                    for name, units in ((d['resource'], d['units'])
                                        for d in requests[-1]['demands']):
                        if name not in seen:
                            seen.add(name)
                            unique.append({'resource': name, 'units': units})
                    requests[-1]['demands'] = unique
                data = {'resources': resources, 'requests': requests}
                self.assertEqual(evaluate(copy.deepcopy(data)), self.expected(copy.deepcopy(data)))

    def test_witnesses_are_basic_segments_of_final_plus_rejected(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [
                request('a', 5, False, [('room', 1)]),
                request('b', 3, False, [('room', 1)]),
                request('c', 3, False, [('room', 1)]),
            ],
        }
        result = evaluate(data)
        capacity, requests = self.collect(data)
        chosen = [r for r in requests if r['id'] in result['accepted_ids']]
        rejected = next(r for r in requests if r['id'] == 'c')
        self.assertEqual(result['rejections'][0]['witness'],
                         self.overflow(capacity, requests, chosen + [rejected]))

    def test_validator_collects_all_requests_before_deciding(self):
        capacity, requests = validate({
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [request('m', 1, True, [('room', 2)])],
        })
        self.assertEqual(capacity, {'room': 1})
        self.assertEqual(requests[0]['occurrences'], [(28401660, 28401720)])


class AdmissionValidationTests(unittest.TestCase):
    def base(self):
        return {
            'resources': [{'resource': 'room', 'capacity': 2}],
            'requests': [request('a', 5, False, [('room', 1)])],
        }

    def assert_invalid(self, data):
        with self.assertRaises(ValueError):
            evaluate(data)

    def test_root_shape(self):
        self.assert_invalid([])
        self.assert_invalid({'resources': []})
        self.assert_invalid({'resources': [], 'requests': [], 'extra': 1})
        self.assert_invalid({'resources': {}, 'requests': []})
        self.assert_invalid({'resources': [], 'requests': {}})

    def test_resource_shape_and_values(self):
        for resources in (
            [{'resource': 'r'}],
            [{'resource': 'r', 'capacity': 1, 'extra': 2}],
            [{'resource': '', 'capacity': 1}],
            [{'resource': 7, 'capacity': 1}],
            [{'resource': 'r', 'capacity': 0}],
            [{'resource': 'r', 'capacity': 1_000_001}],
            [{'resource': 'r', 'capacity': True}],
            [{'resource': 'r', 'capacity': 1.5}],
            [{'resource': 'r', 'capacity': '1'}],
            [{'resource': 'r', 'capacity': 1}, {'resource': 'r', 'capacity': 2}],
        ):
            with self.subTest(resources=resources):
                data = self.base()
                data['resources'] = resources
                data['requests'] = []
                self.assert_invalid(data)

    def test_request_shape_and_values(self):
        mutations = [
            {'priority': 5, 'mandatory': False, 'demands': [{'resource': 'room', 'units': 1}]},
            {'rule': rule('a', start='2024-01-01T09:00'), 'mandatory': False,
             'demands': [{'resource': 'room', 'units': 1}]},
            {'rule': rule('a', start='2024-01-01T09:00'), 'priority': 5,
             'demands': [{'resource': 'room', 'units': 1}]},
            {'rule': rule('a', start='2024-01-01T09:00'), 'priority': 5, 'mandatory': False},
            {'rule': rule('a', start='2024-01-01T09:00'), 'priority': 5, 'mandatory': False,
             'demands': [{'resource': 'room', 'units': 1}], 'extra': 0},
        ]
        for row in mutations:
            with self.subTest(row=row):
                data = self.base()
                data['requests'] = [row]
                self.assert_invalid(data)

    def test_priority_and_mandatory_types(self):
        for priority in (0, 1_000_001, -1, True, 4.0, '5', None):
            with self.subTest(priority=priority):
                data = self.base()
                data['requests'][0]['priority'] = priority
                self.assert_invalid(data)
        for mandatory in (0, 1, 'true', None, []):
            with self.subTest(mandatory=mandatory):
                data = self.base()
                data['requests'][0]['mandatory'] = mandatory
                self.assert_invalid(data)

    def test_demand_shape_and_values(self):
        for demands in (
            [],
            {},
            [{'resource': 'room'}],
            [{'units': 1}],
            [{'resource': 'room', 'units': 1, 'extra': 0}],
            [{'resource': '', 'units': 1}],
            [{'resource': 'room', 'units': 0}],
            [{'resource': 'room', 'units': 1_000_001}],
            [{'resource': 'room', 'units': True}],
            [{'resource': 'room', 'units': 1.0}],
            [{'resource': 'room', 'units': 1}, {'resource': 'room', 'units': 2}],
            [{'resource': 'absent', 'units': 1}],
        ):
            with self.subTest(demands=demands):
                data = self.base()
                data['requests'][0]['demands'] = demands
                self.assert_invalid(data)

    def test_rule_shape_and_expansion(self):
        good = rule('a', start='2024-01-01T09:00')
        for change in (
            lambda r: r.pop('schedule'),
            lambda r: r.update(extra=1),
            lambda r: r.update(schedule=''),
            lambda r: r.update(schedule=7),
            lambda r: r.update(until='2222-01-01'),
            lambda r: r.update(weekdays=[]),
            lambda r: r.update(timezone='Mars/Olympus'),
            lambda r: r.update(gap_policy='clamp'),
            lambda r: r.update(fold_policy='nearest'),
            lambda r: r.update(local_start='2024-01-01T09:00-05:00'),
            lambda r: (r.pop('until'), r.update(until='2023-12-31')),
            lambda r: r.update(local_start='2024-01-01T09:00', until='2023-12-31'),
            lambda r: r.update(local_start='2024-13-01T09:00'),
        ):
            with self.subTest(change=change):
                data = self.base()
                broken = copy.deepcopy(good)
                change(broken)
                data['requests'] = [{'rule': broken, 'priority': 5, 'mandatory': False,
                                     'demands': [{'resource': 'room', 'units': 1}]}]
                self.assert_invalid(data)

    def test_empty_expansion_is_rejected(self):
        data = self.base()
        data['requests'][0]['rule'].update(
            {'local_start': '2024-01-01T09:00', 'until': '2024-01-01', 'weekdays': [7]})
        self.assert_invalid(data)

    def test_duplicate_request_ids_are_rejected(self):
        data = self.base()
        data['requests'].append(copy.deepcopy(data['requests'][0]))
        self.assert_invalid(data)

    def test_bounded_sizes(self):
        data = {'resources': [{'resource': f'r{k}', 'capacity': 1} for k in range(17)],
                'requests': []}
        self.assert_invalid(data)
        data = {'resources': [{'resource': 'room', 'capacity': 1}],
                'requests': [request(f'q{k:02d}', 1, False, [('room', 1)]) for k in range(25)]}
        self.assert_invalid(data)

    def test_trailing_invalid_request_surfaces_beyond_early_infeasibility(self):
        data = {
            'resources': [{'resource': 'room', 'capacity': 1}],
            'requests': [
                request('m1', 1, True, [('room', 2)]),
                request('m2', 1, True, [('room', 2)]),
            ],
        }
        self.assertFalse(evaluate(data)['feasible'])
        data['requests'].append(request('bad', 1, False, [('room', 1)]))
        data['requests'][-1]['priority'] = 0
        self.assert_invalid(data)


if __name__ == '__main__':
    unittest.main()
