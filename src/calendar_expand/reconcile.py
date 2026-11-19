"""Reconciliation of planned calendar instances against actual executions.

A reconciliation never picks one arbitrary pairing and calls it a fact.  It
computes the two-level optimum (maximum number of pairings, then minimum total
absolute minute deviation) and reports *every* triple
``(observation, plan instance, observed UTC instant)`` that occurs in at least
one globally co-optimal explanation, together with whether each object can stay
unmatched in such an explanation.

Ambiguous wall times are kept ambiguous until the global solve.  An actual
record whose naive local time falls in a fold yields two distinct valid UTC
instants (fold 0 and fold 1, round-trip verified); a gap yields zero candidates
and stays in the report as an unmatchable record.  The plan rule's fold policy
is never used to pre-select an observed instant.

Algorithm
---------
The legal triples form a bipartite graph between observations and normalized
plan instances; parallel arcs between the same pair carry different candidate
UTC labels (and possibly different costs).  Cardinality-maximizing,
deviation-minimizing matching is a minimum-cost maximum-flow problem on
``source -> observations -> instances -> sink`` with unit capacities and edge
cost ``abs(delta_minutes)``.  Successive shortest augmenting paths with Johnson
potentials solve it exactly with integer arithmetic.

All co-optimal alternatives are then read from ONE optimal flow without
enumerating explanations.  At an optimum every residual arc has non-negative
reduced cost; let G0 be the residual arcs whose reduced cost is exactly zero.
Pushing one unit around a zero-reduced-cost cycle preserves both flow value and
cost, so:

* an arc carrying flow belongs to the current optimal explanation;
* an arc carrying no flow belongs to some co-optimal explanation exactly when
  it is itself a zero-reduced-cost residual arc and its endpoints lie in the
  same strongly connected component of G0 (the zero cycle reroutes the flow);
* an observation can be unmatched in a co-optimal explanation exactly when it
  is reachable from the source in G0 (the first residual hop must free a
  source arc, which only an unmatched observation offers);
* an instance can be unmatched exactly when it can reach the sink in G0.

Objects without any legal incident triple (for example a gap record with zero
valid UTC instants) are isolated from the flow.  They are unmatched in every
explanation, so the report marks them ``can_be_unmatched=true`` explicitly; the
residual graph has no path that could express this forced state.

The maximum-flow precondition is what makes the cycle characterization
complete: any reroute keeping the flow value closes into a residual cycle,
including reroutes that pass through the source or sink via reversed arcs.

Alternative design
------------------
One could instead force each candidate arc into the matching and re-solve the
minimum-cost flow once per arc (a constraint re-optimization oracle).  That has
a simpler per-arc proof but repeats the whole shortest-path computation up to
E times and still needs separate reruns for the unmatched questions.  The
chosen residual/SCC design pays one flow solve plus one linear SCC pass; its
correctness obligations are (1) the potential invariant that every residual
reduced cost stays non-negative, (2) solving to true maximum flow before the
cycle analysis, (3) keeping fold candidates as distinct parallel arcs instead
of merging labels, and (4) rendering both report sides from one shared set of
supported triples so the two views can never disagree.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import heapq
import re

from .common import ident, require, rows
from .model import analyze

MAX_RULES = 32
MAX_OBSERVATIONS = 32
MAX_INSTANCES = 32
MAX_TOLERANCE_MINUTES = 10080

_MINUTE_TEXT = re.compile(
    r'^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?:Z|[+-]\d{2}(?::?\d{2})?)?)?$')


def _minute_text(text):
    """True when the string carries no explicit seconds or fractional part."""
    return bool(_MINUTE_TEXT.match(text))


def _parse_utc(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _format_utc(value):
    return value.strftime('%Y-%m-%dT%H:%M:00Z')


def _is_minute_precise(value):
    return value.second == 0 and value.microsecond == 0


def observation_candidates(local, zone):
    """All distinct valid UTC instants of a naive wall time in ``zone``.

    Both fold values are independently round-trip verified.  Normal times
    yield one instant, fold times two and gap times zero; the plan's fold
    policy never selects between them.
    """
    candidates = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        back = utc.astimezone(zone)
        if back.replace(tzinfo=None) == local and all(item != utc for item in candidates):
            candidates.append(utc)
    return sorted(candidates)


def normalize_instances(raw_instances):
    """Collapse repeated ``(schedule, start_utc)`` obligations into one.

    The end instant is carried along for completeness; reconciliation
    identifies an obligation solely by ``(schedule, start_utc)``, so different
    schedules at the same UTC remain distinct obligations while duplicate
    identities are reconciled and reported exactly once.
    """
    instances = {}
    for schedule, start, end in raw_instances:
        key = (schedule, start)
        if key not in instances:
            instances[key] = end
    return instances


def validate_document(data):
    """Validate the whole document before any pairing shortcut is taken."""
    require(data, 'rules observations tolerance_minutes')
    tolerance = data['tolerance_minutes']
    if type(tolerance) is not int or not 0 <= tolerance <= MAX_TOLERANCE_MINUTES:
        raise ValueError('tolerance_minutes must be an integer in 0..10080')
    rule_rows = rows(data['rules'], MAX_RULES)
    observation_rows = rows(data['observations'], MAX_OBSERVATIONS)

    # Every complete rule is validated through model.analyze even when later
    # rules duplicate an earlier schedule id.
    expanded = []
    schedule_ids = set()
    for rule in rule_rows:
        result = analyze(rule)
        if result['schedule'] in schedule_ids:
            raise ValueError('duplicate schedule: ' + result['schedule'])
        schedule_ids.add(result['schedule'])
        expanded.append(result)

    observations = []
    observation_ids = set()
    for row in observation_rows:
        require(row, 'observation_id timezone local_start eligible_schedules')
        observation_id = ident(row['observation_id'])
        if observation_id in observation_ids:
            raise ValueError('duplicate observation_id: ' + observation_id)
        observation_ids.add(observation_id)
        zone_name = ident(row['timezone'])
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError('unknown IANA timezone') from error
        local_text = row['local_start']
        if not isinstance(local_text, str):
            raise ValueError('local_start must be an ISO timestamp string')
        try:
            local = datetime.fromisoformat(local_text)
        except ValueError as error:
            raise ValueError('unparseable local_start') from error
        if local.tzinfo is not None or local.second or local.microsecond:
            raise ValueError('local_start must be a naive minute timestamp')
        if not _minute_text(local_text):
            raise ValueError('local_start must be minute precision without seconds')
        eligible = rows(row['eligible_schedules'], MAX_RULES)
        if not eligible:
            raise ValueError('eligible_schedules must be nonempty')
        allowed = []
        for name in eligible:
            name = ident(name)
            if name in allowed:
                raise ValueError('duplicate eligible schedule: ' + name)
            if name not in schedule_ids:
                raise ValueError('unknown eligible schedule: ' + name)
            allowed.append(name)
        observations.append({
            'observation_id': observation_id,
            'candidates': observation_candidates(local, zone),
            'eligible': set(allowed),
        })

    # Minute-precision gate for every actual candidate (a gap has zero
    # candidates and is legal) before any identity normalization or shortcut.
    for observation in observations:
        for candidate in observation['candidates']:
            if not _is_minute_precise(candidate):
                raise ValueError('observed UTC instant is not minute precise')

    # Every planned instance is precision-checked while still raw, before the
    # (schedule, start_utc) identity normalization merges duplicates.
    raw_instances = []
    for result in expanded:
        for occurrence in result['occurrences']:
            start = _parse_utc(occurrence['start_utc'])
            end = _parse_utc(occurrence['end_utc'])
            if not _is_minute_precise(start) or not _is_minute_precise(end):
                raise ValueError('planned UTC instant is not minute precise')
            raw_instances.append((result['schedule'], start, end))
    if len(raw_instances) > MAX_INSTANCES:
        raise ValueError('more than 32 expanded plan instances')
    instances = normalize_instances(raw_instances)
    if len(instances) > MAX_INSTANCES:
        raise ValueError('more than 32 normalized plan instances')
    return tolerance, observations, instances


class MinCostFlow:
    """Integer successive-shortest-path min-cost flow with Johnson potentials."""

    def __init__(self, size):
        self.size = size
        self.graph = [[] for _ in range(size)]

    def add_edge(self, source, target, capacity, cost, key=None):
        forward = [target, len(self.graph[target]), capacity, cost, key]
        backward = [source, len(self.graph[source]), 0, -cost, None]
        self.graph[source].append(forward)
        self.graph[target].append(backward)
        return forward

    def solve(self, source, sink, limit):
        potential = [0] * self.size
        infinite = float('inf')
        flow = total_cost = 0
        while flow < limit:
            distance = [infinite] * self.size
            previous = [None] * self.size
            distance[source] = 0
            heap = [(0, source)]
            while heap:
                current, node = heapq.heappop(heap)
                if current != distance[node]:
                    continue
                for edge_index, edge in enumerate(self.graph[node]):
                    target, _rev, capacity, cost, _key = edge
                    if capacity <= 0:
                        continue
                    candidate = current + cost + potential[node] - potential[target]
                    if candidate < distance[target]:
                        distance[target] = candidate
                        previous[target] = (node, edge_index)
                        heapq.heappush(heap, (candidate, target))
            if distance[sink] == infinite:
                break
            for node in range(self.size):
                if distance[node] != infinite:
                    potential[node] += distance[node]
            node = sink
            path_cost = 0
            while node != source:
                previous_node, edge_index = previous[node]
                edge = self.graph[previous_node][edge_index]
                edge[2] -= 1
                self.graph[node][edge[1]][2] += 1
                path_cost += edge[3]
                node = previous_node
            flow += 1
            total_cost += path_cost
        return flow, total_cost, potential


def _strong_components(graph):
    """Tarjan SCC ids; mutually reachable nodes share one component id."""
    size = len(graph)
    indices = [-1] * size
    low = [0] * size
    stack = []
    on_stack = [False] * size
    component = [-1] * size
    counter = component_count = 0

    def visit(node):
        nonlocal counter, component_count
        indices[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack[node] = True
        for neighbor in graph[node]:
            if indices[neighbor] == -1:
                visit(neighbor)
                low[node] = min(low[node], low[neighbor])
            elif on_stack[neighbor]:
                low[node] = min(low[node], indices[neighbor])
        if low[node] == indices[node]:
            while True:
                neighbor = stack.pop()
                on_stack[neighbor] = False
                component[neighbor] = component_count
                if neighbor == node:
                    break
            component_count += 1

    for node in range(size):
        if indices[node] == -1:
            visit(node)
    return component


def _reachable_from(graph, starts):
    """Nodes reachable from ``starts`` following directed arcs."""
    seen = set(starts)
    queue = list(starts)
    while queue:
        node = queue.pop()
        for neighbor in graph[node]:
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def _can_reach(graph, targets):
    """Nodes from which some node in ``targets`` is reachable."""
    reversed_graph = [set() for _ in graph]
    for node, neighbors in enumerate(graph):
        for neighbor in neighbors:
            reversed_graph[neighbor].add(node)
    return _reachable_from([list(neighbors) for neighbors in reversed_graph],
                           targets)


def evaluate(data):
    """Validate, solve and build the reconciliation report result object."""
    tolerance, observations, instances = validate_document(data)
    observation_count = len(observations)
    occurrence_keys = sorted(instances)
    instance_count = len(occurrence_keys)

    source = 0
    observation_base = 1
    instance_base = observation_base + observation_count
    sink = instance_base + instance_count
    network = MinCostFlow(sink + 1)
    for i in range(observation_count):
        network.add_edge(source, observation_base + i, 1, 0)
    for j in range(instance_count):
        network.add_edge(instance_base + j, sink, 1, 0)

    triples = []
    for i, observation in enumerate(observations):
        for j, (schedule, start) in enumerate(occurrence_keys):
            if schedule not in observation['eligible']:
                continue
            for candidate in observation['candidates']:
                delta = int((candidate - start).total_seconds() // 60)
                if abs(delta) <= tolerance:
                    edge = network.add_edge(
                        observation_base + i, instance_base + j, 1, abs(delta))
                    triples.append({
                        'observation': i, 'instance': j,
                        'observed': candidate, 'delta': delta, 'edge': edge,
                    })

    matched_count, total_deviation, potential = network.solve(
        source, sink, min(observation_count, instance_count))

    # G0: residual arcs with zero reduced cost.
    zero_graph = [set() for _ in range(network.size)]
    for node, edges in enumerate(network.graph):
        for edge in edges:
            target, _rev, capacity, cost, _key = edge
            if capacity > 0 and cost + potential[node] - potential[target] == 0:
                zero_graph[node].add(target)
    zero_graph = [list(neighbors) for neighbors in zero_graph]
    component = _strong_components(zero_graph)
    reachable_from_source = _reachable_from(zero_graph, [source])
    can_reach_sink = _can_reach(zero_graph, [sink])

    # Incident-edge bookkeeping: an object without any legal triple is forced
    # unmatched in every explanation, which the contract still reports as
    # can_be_unmatched=true (it is unmatched in at least, indeed every,
    # optimal explanation).  Residual reachability alone cannot see isolated
    # nodes, so the forced case is added explicitly.
    observation_has_edge = [False] * observation_count
    instance_has_edge = [False] * instance_count
    for triple in triples:
        observation_has_edge[triple['observation']] = True
        instance_has_edge[triple['instance']] = True

    # One shared fact set guarantees both report views present exactly the
    # same triples.
    supported = set()
    for triple in triples:
        edge = triple['edge']
        observation_node = observation_base + triple['observation']
        instance_node = instance_base + triple['instance']
        if edge[2] == 0:
            # The arc carries one unit of the current optimal flow.
            is_supported = True
        else:
            reduced_cost = edge[3] + potential[observation_node] - potential[instance_node]
            is_supported = (reduced_cost == 0 and
                            component[observation_node] == component[instance_node])
        if is_supported:
            supported.add((triple['observation'], triple['instance'],
                           triple['observed'], triple['delta']))

    observation_reports = []
    for i, observation in enumerate(observations):
        supports = []
        for observation_index, instance_index, observed, delta in supported:
            if observation_index != i:
                continue
            schedule, start = occurrence_keys[instance_index]
            supports.append({
                'schedule': schedule,
                'start_utc': _format_utc(start),
                'observed_utc': _format_utc(observed),
                'delta_minutes': delta,
            })
        supports.sort(key=lambda item: (item['schedule'], item['start_utc'],
                                        item['observed_utc']))
        observation_reports.append({
            'observation_id': observation['observation_id'],
            'candidates_utc': [_format_utc(value)
                               for value in observation['candidates']],
            'supports': supports,
            'can_be_unmatched': (
                not observation_has_edge[i] or
                observation_base + i in reachable_from_source),
        })
    observation_reports.sort(key=lambda item: item['observation_id'])

    occurrence_reports = []
    for j, (schedule, start) in enumerate(occurrence_keys):
        supports = []
        for observation_index, instance_index, observed, delta in supported:
            if instance_index != j:
                continue
            supports.append({
                'observation_id': observations[observation_index]['observation_id'],
                'observed_utc': _format_utc(observed),
                'delta_minutes': delta,
            })
        supports.sort(key=lambda item: (item['observation_id'],
                                        item['observed_utc']))
        occurrence_reports.append({
            'schedule': schedule,
            'start_utc': _format_utc(start),
            'supports': supports,
            'can_be_unmatched': (
                not instance_has_edge[j] or
                instance_base + j in can_reach_sink),
        })
    occurrence_reports.sort(key=lambda item: (item['schedule'], item['start_utc']))

    return {
        'matched_count': matched_count,
        'total_deviation_minutes': total_deviation,
        'observations': observation_reports,
        'occurrences': occurrence_reports,
    }
