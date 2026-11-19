"""Tests for plan/execution reconciliation.

An independent exhaustive oracle enumerates complete matchings by backtracking
(no flow vocabulary, no residual graphs) and derives the optimum and the full
support / can-be-unmatched sets straight from the contract.  The implementation
under test never enumerates explanations, so agreement on many random small
documents, including fold ambiguity, is a strong cross-check of the
single-flow residual/SCC characterization.
"""
import copy
import random
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from calendar_expand.reconcile import (evaluate, normalize_instances,
                                      observation_candidates, validate_document)
from calendar_expand.model import analyze


def rule(schedule, start='2024-01-01T09:00', until=None, weekdays=None,
         duration=60, tz='UTC', interval=1, exceptions=(),
         gap='skip', fold='earlier'):
    day = date.fromisoformat(start[:10])
    return {
        'schedule': schedule, 'timezone': tz, 'local_start': start,
        'interval_weeks': interval,
        'weekdays': weekdays if weekdays is not None else [day.isoweekday()],
        'until': until if until is not None else start[:10],
        'duration_minutes': duration, 'exceptions': list(exceptions),
        'gap_policy': gap, 'fold_policy': fold,
    }


def observation(observation_id, tz='UTC', start='2024-01-01T09:00', eligible=()):
    return {'observation_id': observation_id, 'timezone': tz,
            'local_start': start, 'eligible_schedules': list(eligible)}


class ExhaustiveOracle:
    """Contract-level oracle: enumerate all matchings, keep the co-optimal."""

    def __init__(self, document):
        tolerance, observations, instances = validate_document(copy.deepcopy(document))
        self.tolerance = tolerance
        self.observation_ids = [item['observation_id'] for item in observations]
        self.candidates = {item['observation_id']: [c for c in item['candidates']]
                           for item in observations}
        self.eligible = {item['observation_id']: item['eligible'] for item in observations}
        self.instance_keys = sorted(instances)
        self.edges = []
        for item in observations:
            for index, (schedule, start) in enumerate(self.instance_keys):
                if schedule not in item['eligible']:
                    continue
                for candidate in item['candidates']:
                    delta = int((candidate - start).total_seconds() // 60)
                    if abs(delta) <= tolerance:
                        self.edges.append((item['observation_id'], index,
                                           candidate, delta))
        self._solve()

    def _solve(self):
        best = None
        supported = set()
        obs_unmatched = set()
        inst_unmatched = set()
        all_instances = set(range(len(self.instance_keys)))
        all_observations = set(self.observation_ids)

        def backtrack(edge_index, used_observations, used_instances, chosen, cost):
            nonlocal best, supported, obs_unmatched, inst_unmatched
            if edge_index == len(self.edges):
                count = len(chosen)
                score = (count, -cost)
                if best is None or score > best:
                    best = score
                    supported = set(chosen)
                    obs_unmatched = all_observations - used_observations
                    inst_unmatched = all_instances - used_instances
                elif score == best:
                    supported.update(chosen)
                    obs_unmatched |= all_observations - used_observations
                    inst_unmatched |= all_instances - used_instances
                return
            observation_id, instance_index, observed, delta = self.edges[edge_index]
            backtrack(edge_index + 1, used_observations, used_instances,
                      chosen, cost)
            if observation_id not in used_observations and instance_index not in used_instances:
                backtrack(edge_index + 1,
                          used_observations | {observation_id},
                          used_instances | {instance_index},
                          chosen + [(observation_id, instance_index, observed, delta)],
                          cost + abs(delta))

        backtrack(0, set(), set(), [], 0)
        self.best = best
        self.supported = supported
        self.obs_unmatched = obs_unmatched
        self.inst_unmatched = inst_unmatched

    def matches(self, result):
        expected_count, negative_cost = self.best
        if result['matched_count'] != expected_count:
            return False
        if result['total_deviation_minutes'] != -negative_cost:
            return False
        got_supported = set()
        key_to_index = {key: index for index, key in enumerate(self.instance_keys)}
        for item in result['observations']:
            if item['can_be_unmatched'] != (item['observation_id'] in self.obs_unmatched):
                return False
            if item['candidates_utc'] != [c.strftime('%Y-%m-%dT%H:%M:00Z')
                                          for c in self.candidates[item['observation_id']]]:
                return False
            for support in item['supports']:
                instance_index = key_to_index[(
                    support['schedule'],
                    datetime.fromisoformat(support['start_utc'].replace('Z', '+00:00')))]
                observed = datetime.fromisoformat(
                    support['observed_utc'].replace('Z', '+00:00'))
                got_supported.add((item['observation_id'], instance_index,
                                   observed, support['delta_minutes']))
        for item in result['occurrences']:
            index = key_to_index[(
                item['schedule'],
                datetime.fromisoformat(item['start_utc'].replace('Z', '+00:00')))]
            if item['can_be_unmatched'] != (index in self.inst_unmatched):
                return False
            for support in item['supports']:
                observed = datetime.fromisoformat(
                    support['observed_utc'].replace('Z', '+00:00'))
                triple = (support['observation_id'], index, observed,
                          support['delta_minutes'])
                if triple not in got_supported:
                    return False
        return got_supported == self.supported


class ReconcileOracleTests(unittest.TestCase):
    def test_random_documents_match_exhaustive_oracle(self):
        generator = random.Random(163163)
        zones = ['UTC', 'America/New_York', 'Asia/Tokyo', 'Europe/Paris']
        for trial in range(180):
            with self.subTest(trial=trial):
                rule_count = generator.randint(1, 4)
                observation_count = generator.randint(1, 4)
                rules = []
                schedules = []
                for i in range(rule_count):
                    schedules.append(f'r{i}')
                    day = generator.randint(1, 6)
                    hour = generator.randint(0, 22)
                    tz = generator.choice(zones)
                    rules.append(rule(
                        f'r{i}', start=f'2024-01-{day:02d}T{hour:02d}:00',
                        until=f'2024-01-{day + generator.randint(0, 2):02d}',
                        tz=tz,
                        weekdays=sorted(generator.sample(range(1, 8),
                                                         generator.randint(1, 3))),
                        fold=generator.choice(['earlier', 'later']),
                        gap=generator.choice(['skip', 'shift_forward'])))
                observations = []
                for i in range(observation_count):
                    day = generator.randint(1, 6)
                    hour = generator.randint(0, 22)
                    minute = generator.choice([0, 15, 30, 45])
                    tz = generator.choice(zones)
                    eligible_count = generator.randint(1, rule_count)
                    eligible = generator.sample(schedules, eligible_count)
                    observations.append(observation(
                        f'o{i}', tz=tz,
                        start=f'2024-01-{day:02d}T{hour:02d}:{minute:02d}',
                        eligible=eligible))
                document = {'rules': rules, 'observations': observations,
                            'tolerance_minutes': generator.choice([0, 15, 60, 240])}
                # Keep the oracle enumeration small: cap legal edges.
                _, _, instances = validate_document(document)
                if len(instances) > 6:
                    continue
                result = evaluate(copy.deepcopy(document))
                self.assertTrue(ExhaustiveOracle(document).matches(result))

    def test_fold_documents_match_oracle(self):
        # Documents centered on the 2024-11-03 New York fold so several records
        # carry two candidate UTC instants and compete for shared instances.
        rules = [
            rule('early', start='2024-11-03T01:30', tz='America/New_York',
                 until='2024-11-03', weekdays=[7], fold='earlier'),
            rule('late', start='2024-11-03T01:30', tz='America/New_York',
                 until='2024-11-03', weekdays=[7], fold='later'),
            rule('plain', start='2024-11-03T05:30', tz='UTC',
                 until='2024-11-03', weekdays=[7]),
        ]
        observations = [
            observation('fold-a', 'America/New_York', '2024-11-03T01:30',
                        ['early', 'late', 'plain']),
            observation('fold-b', 'America/New_York', '2024-11-03T01:30',
                        ['early', 'late']),
            observation('utc-c', 'UTC', '2024-11-03T05:30',
                        ['early', 'plain']),
        ]
        for tolerance in (0, 30, 90):
            with self.subTest(tolerance=tolerance):
                document = {'rules': rules, 'observations': observations,
                            'tolerance_minutes': tolerance}
                result = evaluate(copy.deepcopy(document))
                self.assertTrue(ExhaustiveOracle(document).matches(result))

    def test_permutation_invariance_matches_oracle(self):
        document = {
            'rules': [
                rule('a', start='2024-01-01T09:00'),
                rule('b', start='2024-01-01T10:00'),
                rule('c', start='2024-01-02T09:00', until='2024-01-02',
                     weekdays=[2]),
            ],
            'observations': [
                observation('o1', start='2024-01-01T09:05', eligible=['a', 'b', 'c']),
                observation('o2', start='2024-01-01T10:00', eligible=['a', 'b']),
                observation('o3', start='2024-01-02T09:00', eligible=['c']),
            ],
            'tolerance_minutes': 120,
        }
        expected = evaluate(copy.deepcopy(document))
        permutations = []
        for rule_order in [(0, 1, 2), (2, 0, 1)]:
            for observation_order in [(0, 1, 2), (2, 1, 0)]:
                variant = copy.deepcopy(document)
                variant['rules'] = [document['rules'][i] for i in rule_order]
                variant['observations'] = [document['observations'][i]
                                           for i in observation_order]
                for item in variant['observations']:
                    item['eligible_schedules'].reverse()
                permutations.append(variant)
        for variant in permutations:
            self.assertEqual(evaluate(variant), expected)
        self.assertTrue(ExhaustiveOracle(document).matches(expected))


class NewYorkFoldAcceptanceTests(unittest.TestCase):
    """The linked acceptance scenario with exact support expectations."""

    def rules(self):
        return [
            rule('s-early', start='2024-11-03T01:30', tz='America/New_York',
                 until='2024-11-03', weekdays=[7], fold='earlier'),
            rule('s-later', start='2024-11-03T01:30', tz='America/New_York',
                 until='2024-11-03', weekdays=[7], fold='later'),
        ]

    def test_both_records_force_the_later_fold(self):
        document = {
            'rules': self.rules(),
            'observations': [
                observation('obs-ny', 'America/New_York', '2024-11-03T01:30',
                            ['s-early', 's-later']),
                observation('obs-utc', 'UTC', '2024-11-03T05:30',
                            ['s-early', 's-later']),
            ],
            'tolerance_minutes': 0,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(result['total_deviation_minutes'], 0)
        ny = next(item for item in result['observations']
                  if item['observation_id'] == 'obs-ny')
        utc = next(item for item in result['observations']
                   if item['observation_id'] == 'obs-utc')
        self.assertEqual(ny['candidates_utc'],
                         ['2024-11-03T05:30:00Z', '2024-11-03T06:30:00Z'])
        self.assertEqual(ny['supports'], [{
            'schedule': 's-later', 'start_utc': '2024-11-03T06:30:00Z',
            'observed_utc': '2024-11-03T06:30:00Z', 'delta_minutes': 0}])
        self.assertFalse(ny['can_be_unmatched'])
        self.assertEqual(utc['supports'], [{
            'schedule': 's-early', 'start_utc': '2024-11-03T05:30:00Z',
            'observed_utc': '2024-11-03T05:30:00Z', 'delta_minutes': 0}])
        self.assertFalse(utc['can_be_unmatched'])
        early = next(item for item in result['occurrences']
                     if item['schedule'] == 's-early')
        later = next(item for item in result['occurrences']
                     if item['schedule'] == 's-later')
        self.assertEqual(early['supports'], [{
            'observation_id': 'obs-utc',
            'observed_utc': '2024-11-03T05:30:00Z', 'delta_minutes': 0}])
        self.assertFalse(early['can_be_unmatched'])
        self.assertEqual(later['supports'], [{
            'observation_id': 'obs-ny',
            'observed_utc': '2024-11-03T06:30:00Z', 'delta_minutes': 0}])
        self.assertFalse(later['can_be_unmatched'])

    def test_removing_the_second_record_keeps_both_fold_supports(self):
        document = {
            'rules': self.rules(),
            'observations': [
                observation('obs-ny', 'America/New_York', '2024-11-03T01:30',
                            ['s-early', 's-later']),
            ],
            'tolerance_minutes': 0,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 1)
        self.assertEqual(result['total_deviation_minutes'], 0)
        ny = result['observations'][0]
        self.assertEqual(ny['supports'], [
            {'schedule': 's-early', 'start_utc': '2024-11-03T05:30:00Z',
             'observed_utc': '2024-11-03T05:30:00Z', 'delta_minutes': 0},
            {'schedule': 's-later', 'start_utc': '2024-11-03T06:30:00Z',
             'observed_utc': '2024-11-03T06:30:00Z', 'delta_minutes': 0},
        ])
        self.assertFalse(ny['can_be_unmatched'])
        self.assertEqual([item['schedule'] for item in result['occurrences']],
                         ['s-early', 's-later'])
        for item in result['occurrences']:
            self.assertEqual(len(item['supports']), 1)
            self.assertEqual(item['supports'][0]['observation_id'], 'obs-ny')
            self.assertTrue(item['can_be_unmatched'])

    def test_two_records_competing_for_one_instance_can_each_be_unmatched(self):
        document = {
            'rules': [rule('only')],
            'observations': [
                observation('o1', eligible=['only']),
                observation('o2', eligible=['only']),
            ],
            'tolerance_minutes': 0,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 1)
        self.assertEqual(result['total_deviation_minutes'], 0)
        for item in result['observations']:
            self.assertEqual(len(item['supports']), 1)
            self.assertTrue(item['can_be_unmatched'])
        occurrence = result['occurrences'][0]
        self.assertFalse(occurrence['can_be_unmatched'])
        self.assertEqual([item['observation_id'] for item in occurrence['supports']],
                         ['o1', 'o2'])

    def test_larger_deviation_edge_same_cardinality_is_unsupported(self):
        # Both perfect matchings have cardinality 2, but x-b / y-a costs 120
        # while x-a / y-b costs 0; the costly triples are never supported.
        document = {
            'rules': [rule('a'), rule('b', start='2024-01-01T10:00')],
            'observations': [
                observation('x', start='2024-01-01T09:00', eligible=['a', 'b']),
                observation('y', start='2024-01-01T10:00', eligible=['a', 'b']),
            ],
            'tolerance_minutes': 120,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(result['total_deviation_minutes'], 0)
        self.assertEqual([(s['schedule'], s['delta_minutes'])
                          for s in result['observations'][0]['supports']], [('a', 0)])
        self.assertEqual([(s['schedule'], s['delta_minutes'])
                          for s in result['observations'][1]['supports']], [('b', 0)])

    def test_legal_but_costly_candidate_enters_no_optimal_explanation(self):
        # The 06:30Z fold candidate is legal under tolerance 60 but costs 60;
        # the 05:30Z candidate costs 0, so only the latter is supported.
        document = {
            'rules': [rule('a', start='2024-11-03T01:30',
                           tz='America/New_York', until='2024-11-03',
                           weekdays=[7], fold='earlier')],
            'observations': [
                observation('fold', 'America/New_York', '2024-11-03T01:30',
                            ['a'])],
            'tolerance_minutes': 60,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 1)
        self.assertEqual(result['total_deviation_minutes'], 0)
        record = result['observations'][0]
        self.assertEqual(record['candidates_utc'],
                         ['2024-11-03T05:30:00Z', '2024-11-03T06:30:00Z'])
        self.assertEqual([(item['observed_utc'], item['delta_minutes'])
                          for item in record['supports']],
                         [('2024-11-03T05:30:00Z', 0)])

    def test_cardinality_beats_local_nearest(self):        # x is closest to a (delta 0); matching x-a leaves b unmatched because
        # z can only use b. The cardinality-optimal explanation pairs x-b (5)
        # and z-b... impossible twice, so the optimum is x-a and z-b with cost
        # 1; x-b is also co-optimal only if z stays unmatched, which would
        # sacrifice cardinality and is therefore excluded.
        document = {
            'rules': [rule('a'), rule('b', start='2024-01-01T09:05')],
            'observations': [
                observation('x', start='2024-01-01T09:00', eligible=['a', 'b']),
                observation('z', start='2024-01-01T09:06', eligible=['b']),
            ],
            'tolerance_minutes': 10,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(result['total_deviation_minutes'], 1)
        x = next(item for item in result['observations']
                 if item['observation_id'] == 'x')
        z = next(item for item in result['observations']
                 if item['observation_id'] == 'z')
        self.assertEqual([(s['schedule'], s['delta_minutes'])
                          for s in x['supports']], [('a', 0)])
        self.assertEqual([(s['schedule'], s['delta_minutes'])
                          for s in z['supports']], [('b', 1)])
        self.assertFalse(x['can_be_unmatched'])
        self.assertFalse(z['can_be_unmatched'])


class CandidateAndSupportTests(unittest.TestCase):
    def test_equal_absolute_deviation_both_folds_are_separate_supports(self):
        document = {
            'rules': [rule('mid', start='2024-11-03T01:00',
                           tz='America/New_York', until='2024-11-03',
                           weekdays=[7], fold='later')],
            'observations': [
                observation('fold', 'America/New_York', '2024-11-03T01:30',
                            ['mid'])],
            'tolerance_minutes': 30,
        }
        result = evaluate(document)
        supports = result['observations'][0]['supports']
        self.assertEqual([(item['observed_utc'], item['delta_minutes'])
                          for item in supports],
                         [('2024-11-03T05:30:00Z', -30),
                          ('2024-11-03T06:30:00Z', 30)])
        # The occurrence view must carry the same two triples.
        self.assertEqual(len(result['occurrences'][0]['supports']), 2)

    def test_gap_record_has_zero_candidates_and_stays_unmatched(self):
        document = {
            'rules': [rule('a')],
            'observations': [
                observation('gap', 'America/New_York', '2024-03-10T02:30',
                            ['a'])],
            'tolerance_minutes': 60,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 0)
        self.assertEqual(result['total_deviation_minutes'], 0)
        record = result['observations'][0]
        self.assertEqual(record['candidates_utc'], [])
        self.assertEqual(record['supports'], [])
        self.assertTrue(record['can_be_unmatched'])
        self.assertEqual(result['occurrences'][0]['supports'], [])
        self.assertTrue(result['occurrences'][0]['can_be_unmatched'])

    def test_normal_fold_and_gap_candidate_counts(self):
        zone = ZoneInfo('America/New_York')
        self.assertEqual(len(observation_candidates(
            datetime.fromisoformat('2024-06-01T12:00'), zone)), 1)
        self.assertEqual(len(observation_candidates(
            datetime.fromisoformat('2024-11-03T01:30'), zone)), 2)
        self.assertEqual(observation_candidates(
            datetime.fromisoformat('2024-03-10T02:30'), zone), [])

    def test_complete_bipartite_returns_1024_supports_without_enumeration(self):
        schedules = [f'r{i:02d}' for i in range(32)]
        document = {
            'rules': [rule(schedule) for schedule in schedules],
            'observations': [
                observation(f'o{i:02d}', eligible=schedules) for i in range(32)
            ],
            'tolerance_minutes': 0,
        }
        result = evaluate(document)
        self.assertEqual(result['matched_count'], 32)
        self.assertEqual(result['total_deviation_minutes'], 0)
        self.assertEqual(sum(len(item['supports'])
                             for item in result['observations']), 1024)
        self.assertEqual(sum(len(item['supports'])
                             for item in result['occurrences']), 1024)
        for item in result['observations'] + result['occurrences']:
            self.assertFalse(item['can_be_unmatched'])

    def test_empty_sides_are_successful_zero_reports(self):
        for document in (
            {'rules': [], 'observations': [], 'tolerance_minutes': 0},
            {'rules': [rule('a')], 'observations': [], 'tolerance_minutes': 0},
        ):
            with self.subTest(document=document):
                result = evaluate(document)
                self.assertEqual(result['matched_count'], 0)
                self.assertEqual(result['total_deviation_minutes'], 0)
                self.assertEqual(result['observations'], [])
        result = evaluate({
            'rules': [rule('empty', until='2024-01-01', weekdays=[2])],
            'observations': [observation('o', eligible=['empty'])],
            'tolerance_minutes': 0,
        })
        self.assertEqual(result['occurrences'], [])
        self.assertEqual(result['observations'][0]['supports'], [])
        self.assertTrue(result['observations'][0]['can_be_unmatched'])

    def test_plan_gap_skip_and_shift_forward(self):
        skipped = evaluate({'rules': [rule('g', start='2024-03-10T02:30',
                                           tz='America/New_York', gap='skip')],
                            'observations': [], 'tolerance_minutes': 0})
        self.assertEqual(skipped['occurrences'], [])
        shifted = evaluate({'rules': [rule('g', start='2024-03-10T02:30',
                                           tz='America/New_York',
                                           gap='shift_forward')],
                            'observations': [], 'tolerance_minutes': 0})
        self.assertEqual(shifted['occurrences'][0]['start_utc'],
                         '2024-03-10T07:00:00Z')

    def test_exception_dates_remove_instances(self):
        result = evaluate({'rules': [rule('a', start='2024-01-01T09:00',
                                          until='2024-01-08', weekdays=[1],
                                          exceptions=['2024-01-08'])],
                           'observations': [], 'tolerance_minutes': 0})
        self.assertEqual([item['start_utc'] for item in result['occurrences']],
                         ['2024-01-01T09:00:00Z'])

    def test_identity_normalization_helper(self):
        # The normalization contract is a pure function of the validated
        # instance stream; feed two identical obligations directly.
        from calendar_expand.reconcile import normalize_instances
        first = datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)
        second = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc)
        normalized = normalize_instances([
            ('a', first, end), ('a', first, end), ('a', second, end),
            ('b', first, end),
        ])
        self.assertEqual(sorted(normalized.items()),
                         [((('a', first)), end), ((('a', second)), end),
                          ((('b', first)), end)])

    def test_observation_side_historical_seconds_rejected(self):
        document = {
            'rules': [],
            'observations': [
                observation('old', 'Europe/Paris', '1900-01-01T09:00',
                            ['unused'])],
            'tolerance_minutes': 0,
        }
        # Unknown eligible id is checked first; make it known with an
        # expansion-less rule so the precision gate is reached.
        document['rules'] = [rule('unused', start='2024-01-01T09:00',
                                  until='2024-01-01', weekdays=[2])]
        with self.assertRaisesRegex(ValueError, 'minute precise'):
            evaluate(document)

    def test_plan_side_historical_seconds_rejected_even_without_pairings(self):
        document = {
            'rules': [rule('old', start='1900-01-01T09:00',
                           tz='Europe/Paris')],
            'observations': [],
            'tolerance_minutes': 0,
        }
        with self.assertRaisesRegex(ValueError, 'minute precise'):
            evaluate(document)


class ReconcileValidationTests(unittest.TestCase):
    def base(self):
        return {
            'rules': [rule('a')],
            'observations': [observation('o', eligible=['a'])],
            'tolerance_minutes': 0,
        }

    def assert_invalid(self, document):
        with self.assertRaises(ValueError):
            evaluate(document)

    def test_root_shape(self):
        for document in ([], {}, {'rules': [], 'observations': []},
                         {'rules': [], 'observations': [],
                          'tolerance_minutes': 0, 'extra': 1}):
            self.assert_invalid(document)

    def test_tolerance_types_and_bounds(self):
        for value in (-1, 10081, True, 1.0, '0', None):
            document = self.base()
            document['tolerance_minutes'] = value
            self.assert_invalid(document)

    def test_array_bounds(self):
        document = self.base()
        document['rules'] = [rule(f'r{i:02d}') for i in range(33)]
        self.assert_invalid(document)
        document = self.base()
        document['observations'] = [observation(f'o{i:02d}', eligible=['a'])
                                    for i in range(33)]
        self.assert_invalid(document)

    def test_raw_instance_cap_enforced_before_normalization(self):
        # Exactly 32 raw instances are accepted; a 33rd (still within the
        # 32-rule array bound) is rejected using the raw, pre-normalization
        # count.
        rules = [rule('r00', start='2024-01-01T09:00', until='2024-01-07',
                      weekdays=[1, 2])]
        rules.extend(rule(f'r{i:02d}') for i in range(1, 31))
        total = sum(len(analyze(item)['occurrences']) for item in rules)
        self.assertEqual(total, 32)
        accepted = evaluate({'rules': rules, 'observations': [],
                             'tolerance_minutes': 0})
        self.assertEqual(len(accepted['occurrences']), 32)
        rules.append(rule('r31'))
        self.assertEqual(sum(len(analyze(item)['occurrences'])
                             for item in rules), 33)
        with self.assertRaisesRegex(ValueError, '32'):
            evaluate({'rules': rules, 'observations': [],
                      'tolerance_minutes': 0})

    def test_duplicate_and_unknown_identifiers(self):
        document = self.base()
        document['rules'].append(rule('a'))
        self.assert_invalid(document)
        document = self.base()
        document['observations'].append(observation('o', eligible=['a']))
        self.assert_invalid(document)
        document = self.base()
        document['observations'][0]['eligible_schedules'] = ['a', 'a']
        self.assert_invalid(document)
        document = self.base()
        document['observations'][0]['eligible_schedules'] = ['ghost']
        self.assert_invalid(document)
        document = self.base()
        document['observations'][0]['eligible_schedules'] = []
        self.assert_invalid(document)

    def test_boolean_integer_positions_rejected(self):
        document = self.base()
        document['tolerance_minutes'] = True
        self.assert_invalid(document)

    def test_timezone_and_timestamp_validation(self):
        document = self.base()
        document['observations'][0]['timezone'] = 'Mars/Olympus'
        self.assert_invalid(document)
        for bad_start in ('2024-01-01T09:00:00', '2024-01-01T09:00:00.5',
                          '2024-01-01T09:00+00:00', '2024-01-01T09:00Z',
                          '2024-01-01T09:00+05:30', 'not a timestamp', 900):
            document = self.base()
            document['observations'][0]['local_start'] = bad_start
            self.assert_invalid(document)

    def test_observation_field_set_must_be_exact(self):
        document = self.base()
        document['observations'][0]['extra'] = 1
        self.assert_invalid(document)
        document = self.base()
        del document['observations'][0]['timezone']
        self.assert_invalid(document)

    def test_trailing_illegal_record_validated_without_pairing_shortcut(self):
        document = {
            'rules': [rule('a')],
            'observations': [
                observation('gap', 'America/New_York', '2024-03-10T02:30',
                            ['a']),
                observation('bad', eligible=['a']),
            ],
            'tolerance_minutes': 0,
        }
        document['observations'][1]['local_start'] = '2024-01-01T09:00+00:00'
        self.assert_invalid(document)

    def test_rule_validation_is_reused_unchanged(self):
        document = self.base()
        document['rules'][0]['fold_policy'] = 'nearest'
        self.assert_invalid(document)


if __name__ == '__main__':
    unittest.main()
