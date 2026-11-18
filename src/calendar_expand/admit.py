"""Exact whole-request admission for shared recurring calendar capacity.

Each request is one complete recurring calendar and is accepted or rejected as
a unit.  Every expanded occurrence is an elapsed-minute UTC half-open interval
[start, end).  On a given resource a request contributes ``units`` load per
occurrence, so occurrences of one request are counted separately: two
overlapping instances give coefficient 2 instead of being unioned into 1.

Elementary segments
-------------------
For each resource the union of all occurrence endpoints of all requests that
demand it partitions time into positive-length elementary segments.  An
occurrence covers whole segments because its endpoints are in the point set,
and two occurrences that merely touch at an endpoint share no segment, so
touching endpoints are never simultaneous.  A request's coefficient is
constant on each segment.

Segments with the same *capacity signature* — the same set of (request,
effective units) pairs covering them — impose exactly the same constraint on
any subset.  The compiler sweeps each resource's endpoints once and merges
segments by signature into equivalence classes.  All capacity checks then run
on class ids: repeated instances stay separate coefficients (they contribute
separate units), and nothing depends on how often a class repeats in time.  For
weekly calendars spanning years this collapses hundreds of thousands of
segments to a few hundred patterns.  Per-request class operations are stored as
``(resource, value, class ids)`` lists.

Exact search
------------
Mandatory requests are checked first by direct recomputation.  If they alone
overflow a segment the instance is business-infeasible.  Otherwise they are
pinned.  Optional requests split into independent components by union-find over
shared capacity classes: two requests interact only when they load a common
class, so feasibility, additive priority and the tie-break separate across
components.  Each component is solved by branch and bound over its class
operations.  The bound is the minimum over valid single-constraint relaxations:
tight classes (avoiding requests add full priority, touching ones are packed
fractionally) and one unit-minute relaxation per resource.  The tree is
exhaustive within those bounds, so each component's score is its global maximum,
not a greedy or pairwise approximation.

Ties are broken by the lexicographically smallest sorted accepted-id list,
shorter common prefix first.  A component's value pass runs in a
priority-per-demand order to reach a strong incumbent; a second pass in Unicode
id order, targeting exactly that optimal score, returns the first set of that
score.  Since equal-score sets are never in a subset relation (priorities are
positive), that first set is the lexicographically smallest optimal list.  Ids
from different components never interact, so concatenating per-component
choices is the global list.  Requests with identical priority and identical
operations are interchangeable; a canonical solution uses them as a prefix of
their visit group, preserving the optimum (and, on id order, the tie-break).

Witnesses
---------
A rejection witness is never explained by the all-request grid.  The resource
timeline is recomputed from the actual occurrences of the final accepted set
plus the rejected request.  Its basic segments refine the all-request grid, and
the load is constant across each recomputed segment, so the first overflowing
grid segment maps to the recomputed segment containing its start.  The earliest
such segment (earliest UTC start, then smallest resource id) is reported with
its instance-multiplicity load and the ids occupying it.  The mandatory conflict
uses the same direct recomputation restricted to mandatory requests.  Witness
loads and segments therefore always match a naive recomputation of the reported
set.
"""
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from .common import ident, number, require, rows
from .model import analyze

MAX_RESOURCES = 16
MAX_REQUESTS = 24
INTEGER_MIN = 1
INTEGER_MAX = 1_000_000
PROBE_LIMIT = 24


def _minute(value):
    return int(value.timestamp() // 60)


def _format(minute):
    return datetime.fromtimestamp(minute * 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:00Z')


def _instants(rule, request_id):
    """Validate one complete rule and return its nonempty minute intervals."""
    try:
        expanded = analyze(rule)
    except TypeError as error:  # malformed timestamp value types
        raise ValueError('invalid rule for request ' + request_id) from error
    if expanded['schedule'] != request_id:
        raise ValueError('rule schedule must match request id')
    if not expanded['occurrences']:
        raise ValueError('request rule expands to no instances: ' + request_id)
    intervals = []
    for occurrence in expanded['occurrences']:
        start = datetime.fromisoformat(occurrence['start_utc'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(occurrence['end_utc'].replace('Z', '+00:00'))
        intervals.append((_minute(start), _minute(end)))
    return intervals


def validate(data):
    """Validate the whole document before any business decision is made."""
    require(data, 'resources requests')
    resource_rows = rows(data['resources'], MAX_RESOURCES)
    request_rows = rows(data['requests'], MAX_REQUESTS)
    capacities = {}
    for row in resource_rows:
        require(row, 'resource capacity')
        name = ident(row['resource'])
        if name in capacities:
            raise ValueError('duplicate resource: ' + name)
        capacities[name] = number(row['capacity'], INTEGER_MIN, INTEGER_MAX)

    parsed = []
    request_ids = set()
    for row in request_rows:
        require(row, 'rule priority mandatory demands')
        priority = number(row['priority'], INTEGER_MIN, INTEGER_MAX)
        if type(row['mandatory']) is not bool:
            raise ValueError('mandatory must be a boolean')
        demand_rows = rows(row['demands'], MAX_RESOURCES)
        if not demand_rows:
            raise ValueError('demands must be nonempty')
        demands = {}
        for demand in demand_rows:
            require(demand, 'resource units')
            name = ident(demand['resource'])
            if name in demands:
                raise ValueError('duplicate demand resource: ' + name)
            if name not in capacities:
                raise ValueError('unknown demand resource: ' + name)
            demands[name] = number(demand['units'], INTEGER_MIN, INTEGER_MAX)
        rule = row['rule']
        if not isinstance(rule, dict):
            raise ValueError('request rule must be an object')
        request_id = ident(rule.get('schedule'))
        if request_id in request_ids:
            raise ValueError('duplicate request id: ' + request_id)
        request_ids.add(request_id)
        parsed.append({'request_id': request_id, 'priority': priority,
                       'mandatory': row['mandatory'], 'demands': demands,
                       'occurrences': _instants(rule, request_id)})
    return capacities, parsed


def _points(intervals):
    return sorted({edge for interval in intervals for edge in interval})


def _direct_conflict(parsed, capacities, indices):
    """Earliest over-capacity segment recomputed from an explicit request set.

    Only the occurrences of ``indices`` are used, so unrelated requests cannot
    explain a rejection.  ``load`` is the running instance multiplicity: the
    same request contributes its units once per overlapping occurrence.
    """
    best = None
    for name in sorted(capacities):
        relevant = [index for index in indices if name in parsed[index]['demands']]
        intervals = [(start, end, parsed[index]['demands'][name])
                     for index in relevant for start, end in parsed[index]['occurrences']]
        if not intervals:
            continue
        points = _points([(start, end) for start, end, _ in intervals])
        delta = [0] * len(points)
        for start, end, units in intervals:
            delta[bisect_left(points, start)] += units
            delta[bisect_left(points, end)] -= units
        running = 0
        for position in range(len(points) - 1):
            running += delta[position]
            if running <= capacities[name]:
                continue
            start, end = points[position], points[position + 1]
            key = (start, name)
            if best is None or key < best[0]:
                active = sorted(parsed[index]['request_id'] for index in relevant
                                if any(lo <= start and end <= hi
                                       for lo, hi in parsed[index]['occurrences']))
                best = (key, {'start_utc': _format(start), 'end_utc': _format(end),
                              'resource': name, 'load': running,
                              'capacity': capacities[name], 'active_ids': active})
            break
    return None if best is None else best[1]


def _point_runs(parsed, points, name):
    """Per-request coefficient runs on a resource's point grid."""
    runs = [[] for _ in parsed]
    for position, request in enumerate(parsed):
        if name not in request['demands']:
            continue
        for start, end in request['occurrences']:
            left = bisect_left(points, start)
            right = bisect_left(points, end)
            if left < right:
                runs[position].append((left, right))
    return runs


def _compile(capacities, parsed):
    """Resource class grids plus per-request class operations.

    Fills each request with ``ops`` (resource -> value -> class ids),
    ``class_values`` (resource -> class -> value), ``first`` (resource ->
    class -> earliest covered point segment) and ``spend`` (unit minutes per
    resource).
    """
    resources = []
    for name in sorted(capacities):
        resources.append({'name': name, 'capacity': capacities[name],
                          'points': _points([interval for request in parsed
                                             if name in request['demands']
                                             for interval in request['occurrences']]),
                          'class_lengths': []})
    for request in parsed:
        request['ops'] = [{} for _ in resources]
        request['class_values'] = [{} for _ in resources]
        request['first'] = [{} for _ in resources]
        request['spend'] = [0] * len(resources)

    for resource_index, resource in enumerate(resources):
        points = resource['points']
        spans = _point_runs(parsed, points, resource['name'])
        changes = {}
        for position, runs in enumerate(spans):
            for start, stop in runs:
                changes.setdefault(start, []).append((position, 1))
                changes.setdefault(stop, []).append((position, -1))
        classes = {}
        counts = {}
        members = []
        current = None
        previous = None
        for position in sorted(changes):
            if previous is not None and position > previous:
                resource['class_lengths'][current] += points[position] - points[previous]
            for request_index, delta in changes[position]:
                units = parsed[request_index]['demands'][resource['name']]
                count = counts.get(request_index, 0) + delta
                if count:
                    counts[request_index] = count
                    effective = units * count
                else:
                    counts.pop(request_index, None)
                    effective = None
                slot = bisect_left(members, (request_index, -1))
                if slot < len(members) and members[slot][0] == request_index:
                    if effective is None:
                        del members[slot]
                    else:
                        members[slot] = (request_index, effective)
                elif effective is not None:
                    members.insert(slot, (request_index, effective))
            # The incrementally maintained sorted member list is the canonical
            # signature: identical signatures impose identical constraints, so
            # they share one class regardless of how the sweep reached them.
            signature = tuple(members)
            following = classes.get(signature)
            if following is None:
                following = len(resource['class_lengths'])
                classes[signature] = following
                resource['class_lengths'].append(0)
                for member, effective in signature:
                    ops = parsed[member]['ops'][resource_index]
                    ops.setdefault(effective, []).append(following)
                    parsed[member]['class_values'][resource_index][following] = effective
                    parsed[member]['first'][resource_index][following] = position
            current = following
            previous = position
        for request in parsed:
            request['spend'][resource_index] = sum(
                occurrence[1] - occurrence[0]
                for occurrence in request['occurrences']
                if resource['name'] in request['demands']) * \
                request['demands'].get(resource['name'], 0)
    for request in parsed:
        request['flat_ops'] = [(resource_index, value, ids)
                               for resource_index in range(len(resources))
                               for value, ids in request['ops'][resource_index].items()]
        request['op_key'] = tuple(
            (resource_index, value, tuple(ids))
            for resource_index, value, ids in request['flat_ops'])
    return resources


def _try_add(loads, caps, ops):
    """Add one request's class operations when all classes stay in capacity.

    Every operation is checked before any mutation, so a failed attempt leaves
    the loads untouched for backtracking.
    """
    for resource_index, value, ids in ops:
        load = loads[resource_index]
        capacity = caps[resource_index]
        for class_id in ids:
            if load[class_id] + value > capacity:
                return False
    for resource_index, value, ids in ops:
        load = loads[resource_index]
        for class_id in ids:
            load[class_id] += value
    return True


def _apply(loads, ops, sign):
    for resource_index, value, ids in ops:
        load = loads[resource_index]
        amount = value * sign
        for class_id in ids:
            load[class_id] += amount


def _probes(resources, items):
    """Single-constraint relaxations used by the branch-and-bound bound.

    Tight classes: classes whose total demand over the component exceeds
    capacity.  For each, requests touching the class are ranked by
    priority-per-unit.  Resource probes use unit-minutes instead of
    instantaneous load: a resource offers ``capacity × span`` unit-minutes and
    a request needs ``units × covered length``.  Both are valid relaxations, so
    both upper-bound the true optimum.
    """
    class_total = []
    for resource_index, resource in enumerate(resources):
        total = [0] * len(resource['class_lengths'])
        for position, item in enumerate(items):
            for value, ids in item['ops'][resource_index].items():
                for class_id in ids:
                    total[class_id] += value
        class_total.append(total)
    candidates = []
    for resource_index, resource in enumerate(resources):
        for class_id, total in enumerate(class_total[resource_index]):
            if total > resource['capacity']:
                candidates.append((resource['capacity'] / total,
                                   resource_index, class_id))
    candidates.sort()
    probes = []
    for _, resource_index, class_id in candidates[:PROBE_LIMIT]:
        ranked = []
        for position, item in enumerate(items):
            value = item['class_values'][resource_index].get(class_id)
            if value:
                ranked.append((item['priority'] / value, position,
                               item['priority'], value))
        probes.append({'resource': resource_index, 'segment': class_id,
                       'capacity': resources[resource_index]['capacity'],
                       'ranked': sorted(ranked, reverse=True),
                       'touched': {entry[1] for entry in ranked}})
    for resource_index, resource in enumerate(resources):
        if not resource['points'] or len(resource['points']) < 2:
            continue
        ranked = []
        for position, item in enumerate(items):
            demand = item['spend'][resource_index]
            if demand:
                ranked.append((item['priority'] / demand, position,
                               item['priority'], demand))
        if len(ranked) < 2:
            continue
        probes.append({'resource': resource_index, 'segment': None,
                       'capacity': sum(resource['class_lengths']) * resource['capacity'],
                       'ranked': sorted(ranked, reverse=True),
                       'touched': {entry[1] for entry in ranked}})
    return probes


def _span(resource):
    return sum(resource['class_lengths'])


def _prepare_probe(probes, items, order):
    """Attach visit-order indexing and untouched-priority suffix sums."""
    visit = [0] * len(items)
    for step, position in enumerate(order):
        visit[position] = step
    priorities = [items[position]['priority'] for position in order]
    prepared = []
    for probe in probes:
        touched = [False] * len(items)
        for position in probe['touched']:
            touched[visit[position]] = True
        suffix = [0] * (len(items) + 1)
        for step in range(len(items) - 1, -1, -1):
            suffix[step] = suffix[step + 1] + (0 if touched[step] else priorities[step])
        ranked = [(visit[position], priority, value)
                  for _, position, priority, value in probe['ranked']]
        prepared.append((probe['resource'], probe['segment'], probe['capacity'],
                         ranked, suffix))
    return prepared


def _bound(loads, free, prepared, step, score):
    """Upper bound of ``score`` plus any completion from ``step`` onward."""
    best = None
    for resource_index, segment, capacity, ranked, suffix in prepared:
        total = float(suffix[step])
        if best is not None and total >= best:
            continue
        room = free[resource_index] if segment is None else \
            capacity - loads[resource_index][segment]
        if room < 0:
            return score
        if room > 0:
            for visit_step, priority, value in ranked:
                if visit_step < step:
                    continue
                take = value if value <= room else room
                total += priority * take / value
                room -= take
                if room <= 0:
                    break
        if best is None or total < best:
            best = total
    return None if best is None else score + best


def _search(resources, loads, caps, free, probes, items, target=None, order=None):
    """Branch and bound visiting ``items`` in ``order`` (ascending id default).

    Without ``target`` returns ``(score, chosen)`` for the maximum score or
    ``None`` when nothing positive is feasible.  With ``target`` returns the
    first complete set scoring exactly ``target``.  The id-order target pass
    yields the lexicographically smallest optimal accepted-id list because
    equal-score sets are never in a subset relation and the include branch is
    taken first.  A different value-pass order stays exact: the tree remains
    exhaustive within the bounds.

    A request that does not fit cannot belong to any completion, so its skip
    branch is a sound cut.  All scores are integers; the fractional bound is
    pruned with a 0.5 slack, so no completion reaching the target or beating
    the incumbent is discarded.
    """
    count = len(items)
    if order is None:
        order = list(range(count))
    priority = [items[position]['priority'] for position in order]
    suffix = [0] * (count + 1)
    for step in range(count - 1, -1, -1):
        suffix[step] = suffix[step + 1] + priority[step]
    groups = {}
    group_of = []
    sizes = {}
    for position in order:
        item = items[position]
        key = (item['priority'], item['op_key'])
        group = groups.setdefault(key, len(groups))
        group_of.append(group)
        sizes[group] = sizes.get(group, 0) + 1
    prepared = _prepare_probe(probes, items, order) if probes else []
    blocked = [False] * len(groups)
    chosen = []
    best = None

    def search(step, score):
        nonlocal best
        if target is not None:
            if score > target or score + suffix[step] < target:
                return None
            if score == target:
                return (score, frozenset(chosen))
            if step == count:
                return None
        elif best is not None and score + suffix[step] <= best[0]:
            return None
        if prepared:
            limit = _bound(loads, free, prepared, step, score)
            if limit is not None:
                if target is not None:
                    if limit < target - 0.5:
                        return None
                elif best is not None and limit < best[0] + 0.5:
                    return None
        if step == count:
            if target is None and (best is None or score > best[0]):
                best = (score, frozenset(chosen))
            return None
        index = order[step]
        item = items[index]
        group = group_of[step]
        if not blocked[group] and _try_add(loads, caps, item['flat_ops']):
            spend = item['spend']
            for resource_index, amount in enumerate(spend):
                free[resource_index] -= amount
            chosen.append(index)
            found = search(step + 1, score + item['priority'])
            chosen.pop()
            for resource_index, amount in enumerate(spend):
                free[resource_index] += amount
            _apply(loads, item['flat_ops'], -1)
            if found is not None:
                return found
        was_blocked = blocked[group]
        if sizes[group] > 1:
            blocked[group] = True
        skipped = search(step + 1, score)
        blocked[group] = was_blocked
        return skipped

    found = search(0, 0)
    return found if found is not None else best


def _components(items):
    """Split optional requests into independent capacity components.

    Two requests interact only if they load a common capacity class, so
    union-find over class membership yields components whose feasibility,
    additive priority and tie-break separate exactly.
    """
    parent = list(range(len(items)))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def join(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    owners = {}
    for position, item in enumerate(items):
        for resource_index, value, ids in item['flat_ops']:
            for class_id in ids:
                key = (resource_index, class_id)
                first = owners.setdefault(key, position)
                if first != position:
                    join(first, position)
    groups = {}
    for position in range(len(items)):
        groups.setdefault(find(position), []).append(position)
    return [groups[root] for root in sorted(groups)]


def _solve_component(resources, caps, baseline, mandatory_spend, items):
    """Optimal optional subset within one independent component."""
    if not items:
        return []
    loads = [list(load) for load in baseline]
    free = [_span(resource) * resource['capacity'] for resource in resources]
    for resource_index, amount in enumerate(mandatory_spend):
        free[resource_index] -= amount
    probes = _probes(resources, items)
    order = sorted(range(len(items)),
                   key=lambda position: (-(items[position]['priority'] /
                                           (1 + sum(items[position]['spend']))),
                                         position))
    found = _search(resources, loads, caps, free, probes, items, order=order)
    if not found or found[0] <= 0:
        return []
    selected = _search(resources, loads, caps, free, probes, items,
                       target=found[0])
    if selected is None:
        raise ValueError('optimal admission selection became unreachable')
    return [items[position] for position in sorted(selected[1])]


def _request_ends(resources, chosen):
    """Sorted endpoint union per resource for one explicit request set."""
    ends = [[] for _ in resources]
    for request in chosen:
        for resource_index in range(len(resources)):
            if request['ops'][resource_index]:
                ends[resource_index].extend(edge for interval in request['occurrences']
                                            for edge in interval)
    return [sorted(set(values)) for values in ends]


def _rejection_witness(resources, loads, accepted_ends, accepted, rejected):
    """Earliest over-capacity recomputed basic segment for one rejected request.

    ``loads`` is the accepted set's class load.  For every class the rejected
    request touches, the combined load is constant on every occurrence of that
    class, so if it overflows, the earliest occurrence (recorded when the class
    was first created) is the first overflow of that class.  That grid point is
    mapped down to the recomputed basic segment containing it: the segment
    delimited by the endpoints of the final accepted set plus the rejected
    request.  The reported end is the next recomputed endpoint, so the interval
    can span several grid segments while staying a single basic segment of the
    reported set.  Unrelated requests never influence either value.
    """
    rejected_edges = sorted({edge for interval in rejected['occurrences']
                             for edge in interval})
    edges_cache = {}
    best = None
    for resource_index, value, ids in rejected['flat_ops']:
        resource = resources[resource_index]
        load = loads[resource_index]
        capacity = resource['capacity']
        points = resource['points']
        for class_id in ids:
            if load[class_id] + value <= capacity:
                continue
            raw_begin = points[rejected['first'][resource_index][class_id]]
            edges = edges_cache.get(resource_index)
            if edges is None:
                edges = sorted(set(accepted_ends[resource_index]) | set(rejected_edges))
                edges_cache[resource_index] = edges
            position = bisect_right(edges, raw_begin) - 1
            begin, end = edges[position], edges[position + 1]
            name = resource['name']
            key = (begin, name)
            if best is None or key < best[0]:
                active = [request['request_id'] for request in accepted
                          if name in request['demands']
                          and any(lo <= begin and end <= hi
                                  for lo, hi in request['occurrences'])]
                active.append(rejected['request_id'])
                best = (key, {'start_utc': _format(begin), 'end_utc': _format(end),
                              'resource': name, 'load': load[class_id] + value,
                              'capacity': capacity, 'active_ids': sorted(active)})
    if best is None:
        raise ValueError('rejected request has no capacity conflict')
    return best[1]


def evaluate(data):
    """Validate, solve and build the result object of one admission document."""
    capacities, parsed = validate(data)
    mandatory_indices = [index for index, request in enumerate(parsed) if request['mandatory']]
    optional_indices = [index for index, request in enumerate(parsed) if not request['mandatory']]

    conflict = _direct_conflict(parsed, capacities, mandatory_indices)
    if conflict is not None:
        return {'feasible': False,
                'accepted_ids': [],
                'rejected_ids': sorted(request['request_id'] for request in parsed),
                'total_priority': None,
                'rejections': [],
                'mandatory_conflict': conflict}

    resources = _compile(capacities, parsed)
    caps = [resource['capacity'] for resource in resources]
    loads = [[0] * len(resource['class_lengths']) for resource in resources]
    mandatory_spend = [0] * len(resources)
    for index in mandatory_indices:
        _apply(loads, parsed[index]['flat_ops'], 1)
        for resource_index, amount in enumerate(parsed[index]['spend']):
            mandatory_spend[resource_index] += amount

    ordered = sorted(optional_indices, key=lambda value: parsed[value]['request_id'])
    items = [parsed[index] for index in ordered]
    position_of = {id(item): position for position, item in enumerate(items)}

    decisions = set()
    for component in _components(items):
        chosen = _solve_component(resources, caps, loads, mandatory_spend,
                                  [items[position] for position in component])
        decisions.update(ordered[position_of[id(item)]] for item in chosen)

    mandatory_priority = sum(parsed[index]['priority'] for index in mandatory_indices)
    total = mandatory_priority + sum(parsed[index]['priority'] for index in decisions)
    accepted = sorted([parsed[index]['request_id'] for index in mandatory_indices] +
                      [parsed[index]['request_id'] for index in decisions])
    rejected_indices = sorted((index for index in optional_indices if index not in decisions),
                              key=lambda value: parsed[value]['request_id'])
    made = sorted(set(mandatory_indices) | decisions)
    accepted_requests = [parsed[index] for index in made]
    final_loads = [list(load) for load in loads]
    for index in decisions:
        _apply(final_loads, parsed[index]['flat_ops'], 1)
    ends = _request_ends(resources, accepted_requests)
    rejections = []
    for index in rejected_indices:
        witness = _rejection_witness(resources, final_loads, ends,
                                     accepted_requests, parsed[index])
        rejections.append({'request_id': parsed[index]['request_id'], 'witness': witness})
    return {'feasible': True,
            'accepted_ids': accepted,
            'rejected_ids': [parsed[index]['request_id'] for index in rejected_indices],
            'total_priority': total,
            'rejections': rejections,
            'mandatory_conflict': None}
