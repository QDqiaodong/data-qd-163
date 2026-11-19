from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .common import require, ident, number, rows


def resolve(local, zone, gap_policy, fold_policy):
    candidates = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        back = utc.astimezone(zone)
        if back.replace(tzinfo=None) == local and all(x[0] != utc for x in candidates):
            candidates.append((utc, back))
    if not candidates:
        if gap_policy == 'skip':
            return None, None
        if gap_policy != 'shift_forward':
            raise ValueError('unknown gap policy')
        shifted = local
        for _ in range(181):
            shifted += timedelta(minutes=1)
            result, actual = resolve(shifted, zone, 'skip', fold_policy)
            if result is not None:
                return result, actual
        raise ValueError('unable to resolve local time gap')
    candidates.sort(key=lambda x: x[0])
    if len(candidates) > 1:
        if fold_policy not in ('earlier', 'later'):
            raise ValueError('unknown fold policy')
        selected = candidates[0] if fold_policy == 'earlier' else candidates[-1]
    else:
        selected = candidates[0]
    return selected


def plan(data):
    """Validate one complete calendar rule and split it into raw dates and resolved items.

    The split is the single internal expansion capability shared by the
    single-shot report and the persistent reminder queue.  Each generated item
    keeps its *nominal* local date (the raw recurring date before gap/fold
    resolution) alongside the actually resolved wall time and UTC instants.
    ``skipped`` holds gap-skip entries keyed by nominal date.  All validation
    rules are exactly those of ``analyze``.
    """
    require(data, 'schedule timezone local_start interval_weeks weekdays until duration_minutes exceptions gap_policy fold_policy')
    name = ident(data['schedule'])
    try:
        zone = ZoneInfo(ident(data['timezone']))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('unknown IANA timezone') from error
    start = datetime.fromisoformat(data['local_start'])
    if start.tzinfo is not None or start.second or start.microsecond:
        raise ValueError('local_start must be a naive minute timestamp')
    interval = number(data['interval_weeks'], 1, 52)
    duration = number(data['duration_minutes'], 1, 10080)
    weekdays = [number(x, 1, 7) for x in rows(data['weekdays'], 7)]
    if not weekdays or len(set(weekdays)) != len(weekdays):
        raise ValueError('weekdays must be nonempty and unique')
    until = date.fromisoformat(data['until'])
    if until < start.date() or (until - start.date()).days > 3660:
        raise ValueError('until outside bounded expansion range')
    exceptions = {date.fromisoformat(x) for x in rows(data['exceptions'], 3660)}
    if data['gap_policy'] not in ('skip', 'shift_forward') or data['fold_policy'] not in ('earlier', 'later'):
        raise ValueError('unsupported DST policy')
    week0 = start.date() - timedelta(days=start.date().weekday())
    items, skipped = [], []
    cursor = start.date()
    while cursor <= until:
        week = (cursor - week0).days // 7
        if cursor >= start.date() and cursor.weekday() + 1 in weekdays and week % interval == 0 and cursor not in exceptions:
            local = datetime.combine(cursor, start.time())
            instant, actual = resolve(local, zone, data['gap_policy'], data['fold_policy'])
            if instant is None:
                skipped.append({'date': cursor, 'reason': 'nonexistent_local_time'})
            else:
                end = instant + timedelta(minutes=duration)
                items.append({'nominal_date': cursor, 'instant': instant,
                              'actual': actual, 'end': end})
        cursor += timedelta(days=1)
    return {'schedule': name, 'timezone': data['timezone'],
            'items': items, 'skipped': skipped}


def analyze(data):
    planned = plan(data)
    occurrences = [
        {'local_start': item['actual'].replace(tzinfo=None).isoformat(timespec='minutes'),
         'start_utc': item['instant'].isoformat().replace('+00:00', 'Z'),
         'end_utc': item['end'].isoformat().replace('+00:00', 'Z')}
        for item in planned['items']]
    skipped = [{'date': entry['date'].isoformat(), 'reason': entry['reason']}
               for entry in planned['skipped']]
    return {'schedule': planned['schedule'], 'timezone': planned['timezone'],
            'occurrences': occurrences, 'skipped': skipped}
