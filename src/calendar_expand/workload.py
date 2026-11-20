"""Per-employee daily meeting-load statistics over local dates.

Each expanded calendar instance is a UTC half-open interval ``[start, end)``
with real elapsed duration (seconds precision and historical sub-minute UTC
offsets are preserved).  Every instance belongs to one employee; the employee's
own IANA timezone — independent of the rule's timezone — decides which local
civil date each covered second is attributed to.

For one employee and one local date the busy time is the *union* of all the
employee's instance intervals clipped to that date's UTC preimage: two
calendars of the same employee that overlap in UTC are never double counted,
while different employees are accounted independently.  Intervals that merely
touch at an endpoint share no time.  An instance crossing the employee's local
midnight contributes its elapsed seconds on each side to the two adjacent
dates; it is not duplicated into two calendar instances.

Local day boundaries
--------------------
The UTC preimage of a local date is ``[day_start(d), day_start(d + 1))`` where
``day_start`` is the instant at which the local midnight occurs:

* a normal midnight round-trips to exactly one UTC instant;
* a midnight inside a repeated (fold) period keeps the earliest verified
  instant, so the fallback day — for example a 25-hour Santiago day whose
  midnight is reached only after the clocks fall back — keeps its true length;
* a midnight skipped by a transition (Chile spring forward at 24:00, or the
  whole skipped Apia 2011-12-30) resolves to the first valid wall time after
  the gap, which is exactly the gap-end instant; a fully skipped date then has
  an empty preimage and contributes zero seconds.

Day lengths therefore come from the time zone rules (23/25-hour DST days,
24h09m21s for Paris 1911-03-10, zero for a skipped date) instead of assuming
every local day is 86400 seconds.

Computation
-----------
Per employee all instance intervals are first merged into one disjoint UTC
union; the merged list and the ascending day boundaries are then swept
together, so each day's busy seconds are the measure of the union intersected
with that day's preimage.  This is the "merge globally, then intersect each
day" design; its correctness obligations are (1) the union merge keeps
half-open endpoints and seconds precision, (2) every in-range day's preimage
endpoints come from the zone rules as above, and (3) the sweep attributes each
covered second to exactly one local date.
"""
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ident, number, require, rows
from .model import plan

MAX_EMPLOYEES = 32
MAX_SCHEDULES = 32
MAX_INSTANCES = 8192
MAX_RANGE_DAYS = 3660
MAX_DAILY_LIMIT_MINUTES = 1440

_DATE_TEXT = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _parse_date(value, field):
    if not isinstance(value, str) or not _DATE_TEXT.match(value):
        raise ValueError(field + ' must be a YYYY-MM-DD date')
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(field + ' must be a valid calendar date') from error


def _local_valid(naive, zone):
    """True when the naive wall time occurs in ``zone`` (round-trip check)."""
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            return True
    return False


def _local_start_utc(naive, zone):
    """Earliest verified UTC instant of a naive wall time in ``zone``.

    A wall time skipped by a transition resolves to the first valid wall time
    after the gap (located by bisection at seconds precision), which is exactly
    the gap-end instant.
    """
    candidates = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive and utc not in candidates:
            candidates.append(utc)
    if candidates:
        return min(candidates)
    low, high = naive, naive + timedelta(hours=25)
    if not _local_valid(high, zone):
        raise ValueError('unable to resolve local day boundary')
    while high - low > timedelta(seconds=1):
        middle = (low + (high - low) / 2).replace(microsecond=0)
        if middle <= low:
            middle = low + timedelta(seconds=1)
        if _local_valid(middle, zone):
            high = middle
        else:
            low = middle
    return _local_start_utc(high, zone)


def _day_start(zone, zone_name, day, cache):
    """UTC instant at which local date ``day`` begins in ``zone``."""
    key = (zone_name, day)
    if key not in cache:
        cache[key] = _local_start_utc(datetime.combine(day, time.min), zone)
    return cache[key]


def _seconds(delta):
    """Whole seconds of a duration; expansion instants are second-precise."""
    return delta.days * 86400 + delta.seconds


def validate(data):
    """Validate the whole document before any daily figure is computed.

    Every employee and every complete calendar rule is checked here, so an
    empty expansion, an over-limit day or any other early business observation
    can never hide a malformed trailing item.
    """
    require(data, 'from_date through_date employees schedules')
    from_date = _parse_date(data['from_date'], 'from_date')
    through_date = _parse_date(data['through_date'], 'through_date')
    if through_date < from_date:
        raise ValueError('through_date must not be before from_date')
    if (through_date - from_date).days >= MAX_RANGE_DAYS:
        raise ValueError('date range exceeds 3660 days')
    employee_rows = rows(data['employees'], MAX_EMPLOYEES)
    schedule_rows = rows(data['schedules'], MAX_SCHEDULES)

    employees = {}
    for row in employee_rows:
        require(row, 'employee timezone daily_limit_minutes')
        name = ident(row['employee'])
        if name in employees:
            raise ValueError('duplicate employee: ' + name)
        zone_name = ident(row['timezone'])
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError('unknown IANA timezone') from error
        limit = number(row['daily_limit_minutes'], 0, MAX_DAILY_LIMIT_MINUTES)
        employees[name] = {'employee': name, 'timezone': zone_name, 'zone': zone,
                           'daily_limit_minutes': limit}

    intervals = {name: [] for name in employees}
    schedule_ids = set()
    total_instances = 0
    for row in schedule_rows:
        require(row, 'employee rule')
        owner = ident(row['employee'])
        if owner not in employees:
            raise ValueError('unknown employee: ' + owner)
        rule = row['rule']
        if not isinstance(rule, dict):
            raise ValueError('schedule rule must be an object')
        try:
            planned = plan(rule)
        except TypeError as error:  # malformed timestamp value types
            raise ValueError('invalid calendar rule') from error
        if planned['schedule'] in schedule_ids:
            raise ValueError('duplicate schedule: ' + planned['schedule'])
        schedule_ids.add(planned['schedule'])
        total_instances += len(planned['items'])
        for item in planned['items']:
            intervals[owner].append((item['instant'], item['end']))
    if total_instances > MAX_INSTANCES:
        raise ValueError('more than 8192 expanded instances')
    return from_date, through_date, employees, intervals


def _merge(intervals):
    """Disjoint sorted union of half-open intervals; touching endpoints merge."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def _daily_busy(merged, starts):
    """Union seconds of ``merged`` inside each consecutive day preimage.

    ``starts`` holds the ascending UTC day boundaries; entry ``k`` of the
    result covers ``[starts[k], starts[k + 1])``.  The merged intervals and the
    boundaries are both ascending, so one forward pass attributes every covered
    second to exactly one local date.
    """
    busies = []
    index = 0
    count = len(merged)
    for position in range(len(starts) - 1):
        day_start, day_end = starts[position], starts[position + 1]
        while index < count and merged[index][1] <= day_start:
            index += 1
        busy = 0
        scan = index
        while scan < count and merged[scan][0] < day_end:
            low = max(merged[scan][0], day_start)
            high = min(merged[scan][1], day_end)
            if high > low:
                busy += _seconds(high - low)
            scan += 1
        busies.append(busy)
    return busies


def evaluate(data):
    """Validate and build the workload report result object."""
    from_date, through_date, employees, intervals = validate(data)
    cache = {}
    reports = []
    for name in sorted(employees):
        employee = employees[name]
        starts = []
        day = from_date
        while day <= through_date:
            starts.append(_day_start(employee['zone'], employee['timezone'], day, cache))
            day += timedelta(days=1)
        starts.append(_day_start(employee['zone'], employee['timezone'], day, cache))
        busies = _daily_busy(_merge(intervals[name]), starts)
        limit_seconds = employee['daily_limit_minutes'] * 60
        days = []
        day = from_date
        for busy in busies:
            days.append({'date': day.isoformat(), 'busy_seconds': busy,
                         'over_limit': busy > limit_seconds})
            day += timedelta(days=1)
        reports.append({'employee': name, 'timezone': employee['timezone'],
                        'daily_limit_minutes': employee['daily_limit_minutes'],
                        'days': days})
    return {'from_date': from_date.isoformat(),
            'through_date': through_date.isoformat(),
            'employees': reports}
