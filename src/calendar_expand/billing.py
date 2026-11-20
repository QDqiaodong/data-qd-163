"""Persistent local shared-facility booking ledger with monthly billing.

A ledger owns one immutable configuration: a fixed IANA billing timezone, the
earliest billable service month, and one free-allowance/price pair per room.
Operators keep one current booking list (``replace``) made of complete
recurring calendar rules.  Finance closes service months strictly in order
(``close``), producing an immutable bill; every later ``close`` also re-derives
the correct charges of every earlier service month from the current booking
list and posts the non-zero differences as ``adjustments`` on the new bill.
Old bills stay queryable through ``show``; a repeated ``close`` of an already
closed month returns the archived bill untouched and never consumes a pending
difference.

Service month
-------------
Each generated instance belongs to exactly one service month: its real UTC
start is converted to the ledger timezone and the local year-month is taken.
All elapsed minutes belong to that month even when the interval crosses a
month boundary.  Ordering inside one ``(service_month, room)`` uses the real
UTC start (seconds preserved for historical sub-minute offsets) and, for equal
instants, the Unicode code point order of ``(schedule, nominal_date)``.

Line identity and cumulative posting
------------------------------------
A billing line is identified by ``(service_month, room, account, schedule,
nominal_date)``.  The cumulative amount already posted for a line is the sum of
its original charge and every adjustment actually issued since.  Maintaining
that as the persistent ``posted`` projection makes the "difference to post now"
comparison independent of how many bills have already been written and keeps it
faithful to the bills on file rather than to a recomputation of their base.
"""
import json
import os
import re
import sqlite3
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ident, number, require, rows
from .model import plan

MAX_ROOMS = 32
MAX_BOOKINGS = 128
MAX_INSTANCES = 16384
MAX_FREE_MINUTES = 1_000_000
MAX_UNIT_CENTS = 1_000_000
MAX_MONTH = '9998-12'
BUSY_WAIT_SECONDS = 10.0

_MONTH_TEXT = re.compile(r'^([0-9]{4})-([0-9]{2})$')
_SCHEMA_TABLES = ('meta', 'rooms', 'bookings', 'instances', 'bills', 'posted')
_LINE_FIELDS = ('service_month', 'room', 'account', 'schedule', 'nominal_date')


def _ident(value):
    """Repository identifier rule plus valid Unicode scalar values only."""
    ident(value)
    if any('\ud800' <= char <= '\udfff' for char in value):
        raise ValueError('expected a valid Unicode scalar sequence')
    return value


def _month(value):
    """Parse the bounded ``YYYY-MM`` service month, returning its canonical text."""
    if not isinstance(value, str):
        raise ValueError('month must be a YYYY-MM string')
    match = _MONTH_TEXT.match(value)
    if match is None:
        raise ValueError('month must be YYYY-MM')
    year, month = int(match.group(1)), int(match.group(2))
    if not 2 <= year <= 9998 or not 1 <= month <= 12:
        raise ValueError('month outside the supported range')
    return value


def _next_month(value):
    year, month = int(value[:4]), int(value[5:])
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    if year > 9998:
        raise ValueError('no later service month is available')
    return f'{year:04d}-{month:02d}'


def _format_utc(instant):
    """Canonical ``YYYY-MM-DDTHH:MM:SSZ`` kept at real second precision."""
    utc = instant.astimezone(timezone.utc)
    return (f'{utc.year:04d}-{utc.month:02d}-{utc.day:02d}'
            f'T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}Z')


def _service_month(instant, zone):
    local = instant.astimezone(zone)
    return f'{local.year:04d}-{local.month:02d}'


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('unknown IANA timezone') from error


def _validate(data):
    """Validate one complete strict document before touching the database."""
    if not isinstance(data, dict) or 'action' not in data:
        raise ValueError('expected an object with an action field')
    action = data['action']
    if action not in ('init', 'replace', 'close', 'show'):
        raise ValueError('unknown action')
    if action == 'init':
        require(data, 'action timezone first_month rooms')
        timezone_name = _ident(data['timezone'])
        _zone(timezone_name)
        first_month = _month(data['first_month'])
        entries = rows(data['rooms'], MAX_ROOMS)
        if not entries:
            raise ValueError('at least one room is required')
        rooms, seen = [], set()
        for entry in entries:
            require(entry, 'room free_minutes unit_cents')
            room = _ident(entry['room'])
            if room in seen:
                raise ValueError('duplicate room: ' + room)
            seen.add(room)
            rooms.append({'room': room,
                          'free_minutes': number(entry['free_minutes'], 0,
                                                 MAX_FREE_MINUTES),
                          'unit_cents': number(entry['unit_cents'], 1,
                                               MAX_UNIT_CENTS)})
        return action, {'timezone': timezone_name, 'first_month': first_month,
                        'rooms': rooms}
    if action == 'replace':
        require(data, 'action bookings')
        entries = rows(data['bookings'], MAX_BOOKINGS)
        prepared, schedules, total, over = [], set(), 0, False
        # Every rule is expanded and validated, including trailing ones, before
        # any count-based conclusion can short-circuit validation.  Once the
        # instance bound is exceeded the remaining rules are still expanded for
        # validation but their instances are dropped to keep memory bounded.
        for entry in entries:
            require(entry, 'room account rule')
            room = _ident(entry['room'])
            account = _ident(entry['account'])
            rule = entry['rule']
            if not isinstance(rule, dict):
                raise ValueError('calendar rule must be an object')
            try:
                planned = plan(rule)
            except TypeError as error:  # malformed timestamp value types
                raise ValueError('invalid calendar rule') from error
            schedule = _ident(planned['schedule'])
            _ident(planned['timezone'])
            if schedule in schedules:
                raise ValueError('duplicate schedule: ' + schedule)
            schedules.add(schedule)
            total += len(planned['items'])
            over = over or total > MAX_INSTANCES
            prepared.append({'room': room, 'account': account,
                             'schedule': schedule, 'rule': rule,
                             'items': [] if over else planned['items']})
        if over:
            raise ValueError('more than 16384 expanded instances')
        return action, {'bookings': prepared, 'count': total}
    require(data, 'action month')
    return action, {'month': _month(data['month'])}


def _connect(db_path):
    """Open one existing ledger, rejecting missing or foreign database files.

    Identification only reads ``sqlite_master``; the journal mode set once by
    ``init`` is persistent, so later connections never need to change it and a
    plain ``SELECT`` never takes the write lock.  A non-SQLite file surfaces as
    a ``sqlite3.DatabaseError`` and is reported as an invalid ledger.
    """
    if not os.path.exists(db_path):
        raise ValueError('database does not exist')
    if os.path.isdir(db_path):
        raise ValueError('database path is a directory')
    connection = sqlite3.connect(db_path, timeout=BUSY_WAIT_SECONDS,
                                 isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA busy_timeout=10000')
        existing = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as error:
        connection.close()
        raise ValueError('database is not a calendar-billing ledger') from error
    except BaseException:
        connection.close()
        raise
    if not set(_SCHEMA_TABLES) <= existing:
        connection.close()
        raise ValueError('database is not a calendar-billing ledger')
    return connection


_SCHEMA_STATEMENTS = (
    'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    """
    CREATE TABLE rooms (
        room TEXT PRIMARY KEY,
        free_minutes INTEGER NOT NULL,
        unit_cents INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE bookings (
        schedule TEXT PRIMARY KEY,
        room TEXT NOT NULL,
        account TEXT NOT NULL,
        rule_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE instances (
        service_month TEXT NOT NULL,
        room TEXT NOT NULL,
        account TEXT NOT NULL,
        schedule TEXT NOT NULL,
        nominal_date TEXT NOT NULL,
        start_utc TEXT NOT NULL,
        minutes INTEGER NOT NULL,
        PRIMARY KEY (schedule, nominal_date)
    )
    """,
    """
    CREATE TABLE bills (
        month TEXT PRIMARY KEY,
        charges_json TEXT NOT NULL,
        adjustments_json TEXT NOT NULL,
        account_totals_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE posted (
        service_month TEXT NOT NULL,
        room TEXT NOT NULL,
        account TEXT NOT NULL,
        schedule TEXT NOT NULL,
        nominal_date TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        PRIMARY KEY (service_month, room, account, schedule, nominal_date)
    )
    """,
)


def _create_schema(connection):
    # Individual execute() calls, never executescript(): the latter commits any
    # open transaction implicitly and would break first-init atomicity.
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)


def _discard_if_tableless(db_path):
    """Remove a never-committed first-init database, only while still empty."""
    guard = sqlite3.connect(db_path, timeout=BUSY_WAIT_SECONDS)
    try:
        tables = guard.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        guard.close()
    if tables:
        return
    for suffix in ('', '-wal', '-shm'):
        sidecar = db_path + suffix
        if os.path.exists(sidecar):
            os.unlink(sidecar)


def _init(db_path, detail):
    parent = os.path.dirname(os.path.abspath(db_path))
    if not os.path.isdir(parent):
        raise ValueError('database directory does not exist')
    try:
        descriptor = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as error:
        raise ValueError('database already exists') from error
    os.close(descriptor)
    connection = None
    try:
        connection = sqlite3.connect(db_path, timeout=BUSY_WAIT_SECONDS,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=10000')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('BEGIN IMMEDIATE')
        try:
            # Schema creation is part of the first-init transaction, so a
            # rollback removes it together with every provisional row.
            _create_schema(connection)
            connection.execute('INSERT INTO meta(key, value) VALUES(?, ?)',
                               ('timezone', detail['timezone']))
            connection.execute('INSERT INTO meta(key, value) VALUES(?, ?)',
                               ('first_month', detail['first_month']))
            for room in detail['rooms']:
                connection.execute(
                    'INSERT INTO rooms(room, free_minutes, unit_cents) '
                    'VALUES(?, ?, ?)',
                    (room['room'], room['free_minutes'], room['unit_cents']))
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
    except BaseException:
        if connection is not None:
            connection.close()
        _discard_if_tableless(db_path)
        raise
    connection.close()
    return {'first_month': detail['first_month']}


def _get_meta(connection, key):
    row = connection.execute('SELECT value FROM meta WHERE key=?',
                             (key,)).fetchone()
    if row is None:
        raise ValueError('ledger metadata is missing: ' + key)
    return row['value']


def _load_rooms(connection):
    return {row['room']: (row['free_minutes'], row['unit_cents'])
            for row in connection.execute(
                'SELECT room, free_minutes, unit_cents FROM rooms')}


def _instance_rows(prepared, zone, first_month, rooms):
    """Expand prepared bookings into durable instance rows.

    Raises for an unknown room or any instance whose service month falls
    outside ``first_month`` .. ``9998-12``; the whole replace then fails.
    """
    instances, seen = [], set()
    for entry in prepared:
        if entry['room'] not in rooms:
            raise ValueError('unknown room: ' + entry['room'])
        for item in entry['items']:
            service_month = _service_month(item['instant'], zone)
            if service_month < first_month or service_month > MAX_MONTH:
                raise ValueError(
                    'booking outside the billable service months: ' + service_month)
            nominal = item['nominal_date'].isoformat()
            key = (entry['schedule'], nominal)
            # The same expanded instance is billed once even if a list somehow
            # repeated it; ``schedule`` uniqueness already makes that rare.
            if key in seen:
                continue
            seen.add(key)
            minutes = int((item['end'] - item['instant']).total_seconds() // 60)
            instances.append((service_month, entry['room'], entry['account'],
                              entry['schedule'], nominal,
                              _format_utc(item['instant']), minutes))
    return instances


def _computed_charges(instances, rooms):
    """Derive the correct charge line of every instance from the current list.

    Returns ``{service_month: {line_key: info}}``.  Free allowance is consumed
    within each ``(service_month, room)`` in real UTC start order; equal starts
    are ordered by the Unicode code points of ``(schedule, nominal_date)``.
    Every instance consumes its own whole elapsed duration even when intervals
    overlap, and partially free instances pay only for the excess minutes.
    """
    groups = {}
    for row in instances:
        groups.setdefault((row['service_month'], row['room']), []).append(row)
    computed = {}
    for (service_month, room), group in groups.items():
        if room not in rooms:
            raise ValueError('unknown room: ' + room)
        free_limit, unit_cents = rooms[room]
        remaining = free_limit
        group.sort(key=lambda row: (row['start_utc'], row['schedule'],
                                    row['nominal_date']))
        lines = computed.setdefault(service_month, {})
        for row in group:
            minutes = row['minutes']
            applied = min(remaining, minutes)
            remaining -= applied
            line = (service_month, room, row['account'], row['schedule'],
                    row['nominal_date'])
            lines[line] = {
                'amount_cents': (minutes - applied) * unit_cents,
                'start_utc': row['start_utc'],
                'minutes': minutes,
                'free_minutes': applied,
            }
    return computed


def _identity(line):
    return dict(zip(_LINE_FIELDS, line))


def _load_posted(connection):
    posted = {}
    for row in connection.execute(
            'SELECT service_month, room, account, schedule, nominal_date, '
            'amount_cents FROM posted'):
        line = (row['service_month'], row['room'], row['account'],
                row['schedule'], row['nominal_date'])
        posted.setdefault(row['service_month'], {})[line] = row['amount_cents']
    return posted


def _store_posted(connection, month, lines):
    connection.execute('DELETE FROM posted WHERE service_month=?', (month,))
    for line, info in lines.items():
        if info['amount_cents']:
            connection.execute(
                'INSERT INTO posted(service_month, room, account, schedule, '
                'nominal_date, amount_cents) VALUES(?, ?, ?, ?, ?, ?)',
                (*line, info['amount_cents']))


def _replace(connection, detail):
    zone = _zone(_get_meta(connection, 'timezone'))
    first_month = _get_meta(connection, 'first_month')
    rooms = _load_rooms(connection)
    instances = _instance_rows(detail['bookings'], zone, first_month, rooms)
    connection.execute('DELETE FROM instances')
    connection.execute('DELETE FROM bookings')
    for entry in detail['bookings']:
        connection.execute(
            'INSERT INTO bookings(schedule, room, account, rule_json) '
            'VALUES(?, ?, ?, ?)',
            (entry['schedule'], entry['room'], entry['account'],
             json.dumps(entry['rule'], sort_keys=True, ensure_ascii=False)))
    for row in instances:
        connection.execute(
            'INSERT INTO instances(service_month, room, account, schedule, '
            'nominal_date, start_utc, minutes) VALUES(?, ?, ?, ?, ?, ?, ?)', row)
    return {'occurrence_count': len(instances)}


def _stored_instances(connection):
    return connection.execute(
        'SELECT service_month, room, account, schedule, nominal_date, '
        'start_utc, minutes FROM instances').fetchall()


def _bill_row(connection, month):
    return connection.execute(
        'SELECT month, charges_json, adjustments_json, account_totals_json '
        'FROM bills WHERE month=?', (month,)).fetchone()


def _bill_from_row(row):
    return {'month': row['month'],
            'charges': json.loads(row['charges_json']),
            'adjustments': json.loads(row['adjustments_json']),
            'account_totals': json.loads(row['account_totals_json'])}


def _show(connection, month):
    row = _bill_row(connection, month)
    if row is None:
        raise ValueError('month is not closed: ' + month)
    return _bill_from_row(row)


def _build_charges(computed, month):
    charges = []
    for line, info in computed.get(month, {}).items():
        entry = _identity(line)
        entry['amount_cents'] = info['amount_cents']
        entry['start_utc'] = info['start_utc']
        entry['minutes'] = info['minutes']
        entry['free_minutes'] = info['free_minutes']
        charges.append(entry)
    charges.sort(key=lambda entry: tuple(entry[field] for field in _LINE_FIELDS))
    return charges


def _build_adjustments(connection, computed, month):
    posted = _load_posted(connection)
    months = {value for value in computed if value < month}
    months |= {value for value in posted if value < month}
    adjustments = []
    for service_month in sorted(months):
        current = computed.get(service_month, {})
        previous = posted.get(service_month, {})
        for line, info in current.items():
            difference = info['amount_cents'] - previous.get(line, 0)
            if difference:
                entry = _identity(line)
                entry['amount_cents'] = difference
                adjustments.append(entry)
        # A line that left the current list owes its whole posted amount back.
        for line, amount in previous.items():
            if line not in current and amount:
                entry = _identity(line)
                entry['amount_cents'] = -amount
                adjustments.append(entry)
        # The cumulative posting now exactly mirrors the recomputed truth,
        # whatever the original bill and every later adjustment summed to.
        _store_posted(connection, service_month, current)
    adjustments.sort(key=lambda entry: tuple(entry[field] for field in _LINE_FIELDS))
    return adjustments


def _account_totals(charges, adjustments):
    totals = {}
    for entry in charges:
        totals[entry['account']] = totals.get(entry['account'], 0) + \
            entry['amount_cents']
    for entry in adjustments:
        totals[entry['account']] = totals.get(entry['account'], 0) + \
            entry['amount_cents']
    return [{'account': account, 'amount_cents': totals[account]}
            for account in sorted(totals)]


def _close(connection, month):
    existing = _bill_row(connection, month)
    if existing is not None:
        # A repeated close is a pure read: it neither consumes nor recomputes
        # any pending adjustment.
        return _bill_from_row(existing)
    last = connection.execute(
        'SELECT month FROM bills ORDER BY month DESC LIMIT 1').fetchone()
    first_month = _get_meta(connection, 'first_month')
    expected = first_month if last is None else _next_month(last['month'])
    if month != expected:
        raise ValueError('close must target the next unclosed service month')
    rooms = _load_rooms(connection)
    computed = _computed_charges(_stored_instances(connection), rooms)
    charges = _build_charges(computed, month)
    adjustments = _build_adjustments(connection, computed, month)
    # The current month's own charges join the cumulative posted projection;
    # they have never been adjusted before because adjustments only ever touch
    # earlier months.
    _store_posted(connection, month, computed.get(month, {}))
    bill = {'month': month, 'charges': charges, 'adjustments': adjustments,
            'account_totals': _account_totals(charges, adjustments)}
    connection.execute(
        'INSERT INTO bills(month, charges_json, adjustments_json, '
        'account_totals_json) VALUES(?, ?, ?, ?)',
        (month,
         json.dumps(charges, sort_keys=True, ensure_ascii=False),
         json.dumps(adjustments, sort_keys=True, ensure_ascii=False),
         json.dumps(bill['account_totals'], sort_keys=True, ensure_ascii=False)))
    return bill


def process(db_path, data):
    """Validate and execute one billing document against the ledger database."""
    action, detail = _validate(data)
    if os.path.islink(db_path):
        raise ValueError('database cannot be a symlink')
    if action == 'init':
        return _init(db_path, detail)
    connection = _connect(db_path)
    try:
        if action == 'show':
            # A consistent read under a deferred transaction: it sees one whole
            # committed state and never blocks the writer.
            connection.execute('BEGIN')
            try:
                return _show(connection, detail['month'])
            finally:
                connection.execute('COMMIT')
        connection.execute('BEGIN IMMEDIATE')
        try:
            if action == 'replace':
                result = _replace(connection, detail)
            else:
                result = _close(connection, detail['month'])
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        return result
    finally:
        connection.close()
