# Zoned weekly schedule expansion v1

Expand one local weekly recurrence with schedule id, IANA timezone, naive minute-precision local_start, interval_weeks 1..52, ISO weekdays 1..7, inclusive until within 3660 days, duration_minutes 1..10080, and excluded local dates. The recurrence is anchored to the Monday week containing local_start; only dates on/after local_start that match interval and weekday are emitted.

Nonexistent wall times follow gap_policy skip or shift_forward (advance minute-by-minute to the first valid local minute). Ambiguous wall times follow fold_policy earlier or later, chosen by UTC instant. Duration is elapsed time after conversion to UTC, so DST transitions do not distort it. Output records both resolved local start and UTC start/end.

The synthetic New York fixture covers a spring gap and autumn fold. This is a single recurrence rule, not an RFC 5545 parser, shared-resource booking system, reminder service or legal calendar.

## Shared-resource admission v1 (`calendar-admit`)

`calendar-admit` is a second, additive entry point. `calendar-expand` and its
report shape are unchanged.

### Input

The JSON root has exactly `resources` and `requests`, both arrays.

* `resources[]` has exactly `resource` (identifier) and `capacity`
  (integer 1..1000000). Resource names are unique; at most 16 resources.
* `requests[]` has exactly `rule`, `priority` (integer 1..1000000),
  `mandatory` (JSON boolean; integers are not accepted) and nonempty
  `demands`. At most 24 requests.
* `demands[]` has exactly `resource` (identifier, must exist) and `units`
  (integer 1..1000000). A request may not list the same resource twice.
* `rule` is the complete expansion object above. `rule.schedule` is the
  request id, it is globally unique, and it must expand to at least one
  occurrence. The same bounds and DST policies apply.

Identifiers follow `common.ident`. Unknown fields, non-integers, booleans in
integer positions and nonfinite JSON numbers are input errors (exit 2). Both
arrays may be empty. Every resource and every request, including its rule, is
validated before any feasibility conclusion, so an infeasible mandatory load
never hides a malformed trailing request.

`source_sha256` is computed over the exact input bytes and `schema_version`
stays 1, as in the expansion report.

### Occupancy semantics

An occurrence occupies the UTC half-open interval `[start_utc, end_utc)` with
elapsed-minute duration. On each demanded resource a request adds `units` per
occurrence. Overlapping instances inside one request add up; they are never
unioned first. Two occurrences that only share an endpoint (`a.end == b.start`)
do not overlap.

Resources are independent, but all constraints must hold simultaneously: a
request demanding two resources consumes capacity on both for the same
intervals.

### Result

`result` has exactly `feasible`, `accepted_ids`, `rejected_ids`,
`total_priority`, `rejections`, `mandatory_conflict`.

* Feasible: `feasible=true`, every request id appears in exactly one of
  `accepted_ids` or `rejected_ids`, every list is sorted by Unicode code point
  with no duplicates, and `total_priority` is the sum of each request's
  priority counted once, mandatory or not. `mandatory_conflict` is `null`.
* `rejections` is ordered by `request_id` and has one entry per rejected
  request: `{request_id, witness}`. Only optional requests can be rejected in
  a feasible answer.
* Infeasible: all mandatory requests cannot fit together. The business answer
  is `feasible=false`, empty `accepted_ids`, all request ids in
  `rejected_ids`, `total_priority=null`, empty `rejections`, and one
  `mandatory_conflict` witness. This is a successful computation (exit 0), not
  an input error.
* Empty request set: feasible, `total_priority=0`, all lists empty,
  `mandatory_conflict=null`.

A `witness` has exactly `start_utc`, `end_utc`, `resource`, `load`,
`capacity`, `active_ids`. Times are UTC minute precision
`YYYY-MM-DDTHH:MM:00Z` and every interval has positive length. `load` is the
instance multiplicity (not the number of requests). `active_ids` lists only
requests with a real occurrence covering the whole segment, sorted by code
point.

### Witness construction

A rejection witness is computed by recomputing the resource timelines from
only the final accepted set plus the rejected request. On that recomputation,
the reported segment is the earliest over-capacity basic segment: smallest
`start_utc`, then smallest `resource`. Basic segments are delimited by all
occurrence endpoints of exactly that recomputed set on that resource. Using
the final set means an unrelated request's peak can never explain a rejection,
and using actual instances means self-overlap is preserved in `load`.

The mandatory conflict uses the same recomputation restricted to mandatory
requests, with the same earliest-segment rule.

### Algorithm

The optimiser works on a compiled representation:

1. For each resource, all occurrence endpoints of all demanding requests form
   the elementary segment grid. A request's coefficient on a segment is
   `units × number of its occurrences covering the segment`. Touching endpoints
   fall in different segments, so they never add load together.
2. Segments with the same *capacity signature* (the same set of
   `(request, effective units)` pairs) impose the same constraint on every
   subset, so the compiler merges them into one equivalence class. Checks and
   loads then run on class ids; repeated instances remain separate coefficient
   units, and time multiplicity lives only in the class lengths used by the
   unit-minute bound. A sweep over endpoints maintains the active signature
   incrementally, so a year of weekly occurrences collapses into a few hundred
   classes instead of hundreds of thousands of segments.
3. Mandatory requests are summed and checked for capacity. If they overflow,
   the answer is the mandatory conflict.
4. Optional requests are split into independent components by union-find over
   shared classes. Two requests can only interact by loading a common class, so
   different components add their priorities, remain independently feasible,
   and concatenate to the global lexicographic optimum.
5. Each component is solved by branch and bound. A candidate is accepted only
   if adding its class operations stays within capacity on every touched class.
   Bounds are the minimum over several valid single-constraint relaxations:
   tight classes where untouched requests add full priority and touched ones
   are packed fractionally, plus one unit-minute relaxation per resource. The
   search is exhaustive within those bounds, so each component's selection is
   globally optimal. It is deliberately not a per-request greedy or
   pairwise-conflict heuristic. Two passes run per component: a value pass in
   priority-per-demand order (only to find a strong incumbent) and, for exactly
   that optimal score, a target pass in id order that fixes the tie-break.
6. Requests with identical priority and identical class operations are
   symmetric. The search treats them as an interchangeable group with an
   "include a prefix" canonical form, which keeps both the optimum and the
   tie-break.
7. Ties: the sorted accepted-id lists are compared lexicographically by code
   point, shorter common prefix first. Within a component, the target pass
   visits requests in id order, include branch first, and the first set
   reaching the optimal score is the required list; components concatenate
   because their id sets never interact.
8. Witnesses are recomputed from the final set as described above rather than
   read off the all-request grid.

**Structure trade-off.** The compiled-class design stores, per request,
`(resource, effective units)` membership in each class. Repeated instances stay
separate coefficient units (effective units are `units × active occurrences`),
and endpoint handling is structural: a class exists only between distinct
endpoint times, so `[a, b)` and `[b, c)` never share a class. Because witnesses
are recomputed from actual instances of the final set, the reported segment and
`load` always match a naive recalculation even though the solver uses merged
classes. The alternative — maintaining and recomputing per-request intervals
for each candidate set — uses less precomputation but repeats interval
arithmetic at every node, makes each branch check O(instances) instead of
O(classes), and makes it easier to accidentally union self-overlapping
instances. Merging classes by signature is exact for selection because the
signature determines the constraint; correctness of the answer is safeguarded
by recomputing both witnesses directly from the reported set, so the compiled
structure only drives the choice of accepted ids.

### Boundaries and operations

* At most 16 resources and 24 requests; 8 MB input cap; duplicate JSON keys,
  nonfinite numbers and a 3660-day expansion bound are rejected.
* Exit 0 on a published report, including business infeasibility. Exit 2 on
  input, calculation or file errors, without replacing an existing report.
* On success the report is atomically published to `--output` and the same JSON
  is printed to stdout. Input and output paths must differ; an output symlink is
  rejected.
* Reruns are idempotent. Standard library only; IANA time zones come from the
  operating system.

### Arithmetic example (hand-checkable)

`fixtures/admission.json` uses a digestive-room capacity of 2. Monday 09:00
local time on 2024-03-04 is 14:00 UTC (America/New_York is UTC-5 then). The
first occurrences are `triage` 14:00–15:00 UTC (mandatory, priority 5), `scopy`
14:30–15:30 (priority 9, also needs surgery capacity 1), `swallow` 14:00–15:30
every 4th week (priority 7), and `motility` 14:00–16:00 every 4th week
(priority 7). On the shared 14:30–15:00 UTC digestive segment on 2024-03-04,
`triage + scopy` already load 2, so no other optional request fits with both.
`triage + swallow` loads 2 but adds only 7 against `scopy`'s 9; likewise
`triage + motility`. Taking all three optional requests would load 3 in that
segment, so `triage + scopy = 14` is strictly best. Both `swallow` and
`motility` are rejected, and each witness is the recomputed 14:30–15:00 UTC
digestive segment with load 3 (triage, scopy and the rejected request) — the
earliest over-capacity segment after adding that request, reported against the
final accepted set.

The tests state all expected values explicitly; they do not call the admission
solver to produce expectations.

### Commands

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/calendar-admit --source fixtures/admission.json --output run/admission.json
.venv/bin/python -m unittest discover -s tests -v
```
