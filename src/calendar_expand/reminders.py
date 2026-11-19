"""Persistent local reminder work queue over recurring calendar snapshots.

A reminder is identified once and forever by ``(schedule, nominal_date)``.
The nominal date is the raw recurring local date produced by the Monday anchor,
week interval, ISO weekday and inclusive until bounds *before* gap/fold
resolution; exceptions are applied before resolution.  A ``skip`` gap creates
no reminder, a ``shift_forward`` gap may move the actual start across a day
without changing the identity, and a fold chooses between the two UTC instants
without changing it.  Identity is therefore never inferred from the resolved
local start or the UTC instants.

All state lives in one SQLite database.  Each successful ``sync``, ``claim`` or
``ack`` is one serialized transaction that checks and changes every table
together, so a concurrent process can never observe half a snapshot swap, claim
the same reminder twice or act on a stale lease.  ``inspect`` only reads under
the same locking discipline and never writes.

Persistence projection
----------------------
The current snapshot is stored twice in complementary form: ``snapshot_rules``
keeps every validated rule plus its ``lead_minutes`` as an auditable record of
the exact accepted generation (the work decisions themselves read the
materialized tables, never this audit copy), while ``reminders`` materializes
every identity the snapshot produces together with its immutable payload.
Completed identities stay in ``reminders`` while they remain in the snapshot
(they are filtered out of every work query); their durable suppression lives in
``completions``, which is independent of the snapshot and survives removal and
re-adding the rule.
The projection's correctness obligations are:

1. a snapshot transaction replaces both tables inside one commit, so rules and
   materialized payloads can never describe different generations;
2. payloads are recomputed from the new generation before any lease is touched,
   and an active lease whose payload disappears or changes rejects the whole
   sync before the first row changes;
3. tombstones are keyed by identity and never rewritten by snapshot
   replacement, so removal, re-adding, timezone changes or fold changes cannot
   resurrect a completed identity;
4. claim, ack and inspect all decide state from one join of these tables plus
   the caller-provided ``now``; leases past their ``lease_until`` are treated
   as absent (equality means expired) without any background cleanup.
"""
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

from .common import ident, number, require, rows
from .model import plan

MAX_RULES = 32
MAX_REMINDERS = 8192
MAX_LEAD_MINUTES = 10080
MAX_LIMIT = 256
MAX_LEASE_MINUTES = 1440
BUSY_WAIT_SECONDS = 10.0

_NOW_TEXT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
_SCHEMA_TABLES = ('meta', 'snapshot_rules', 'reminders', 'leases',
                  'completions', 'seq')


def parse_now(text):
    """Parse the only accepted clock format: UTC, seconds precision, no leap second."""
    if not isinstance(text, str) or not _NOW_TEXT.match(text):
        raise ValueError('now must be YYYY-MM-DDTHH:MM:SSZ in UTC')
    try:
        parsed = datetime.strptime(text, '%Y-%m-%dT%H:%M:%S%z')
    except ValueError as error:
        raise ValueError('now must be a valid UTC second timestamp') from error
    if parsed.second == 60 or parsed.utcoffset() != timedelta(0):
        raise ValueError('now must be a valid UTC second without leap second')
    return parsed


def format_utc(instant):
    return instant.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def expand_reminders(rule, lead_minutes):
    """Expand one validated rule into complete reminder payloads.

    Payload fields are exactly the seven contracted ones.  ``due_utc`` subtracts
    ``lead_minutes`` as elapsed UTC minutes, so historical sub-minute offsets
    (for example the Paris 1900 ``08:50:39Z`` instant) are preserved.
    """
    planned = plan(rule)
    payloads = []
    for item in planned['items']:
        start = item['instant']
        due = start - timedelta(minutes=lead_minutes)
        payloads.append({
            'schedule': planned['schedule'],
            'nominal_date': item['nominal_date'].isoformat(),
            'timezone': planned['timezone'],
            'local_start': item['actual'].replace(tzinfo=None).isoformat(timespec='minutes'),
            'start_utc': format_utc(start),
            'end_utc': format_utc(item['end']),
            'due_utc': format_utc(due),
        })
    return payloads


def _validate_document(data):
    """Validate the complete strict document before any database access."""
    if not isinstance(data, dict) or 'action' not in data:
        raise ValueError('expected an object with an action field')
    action = data['action']
    if action not in ('sync', 'claim', 'ack', 'inspect'):
        raise ValueError('unknown action')
    expected = {
        'sync': 'action now schedules',
        'claim': 'action now limit lease_minutes',
        'ack': 'action now schedule nominal_date token',
        'inspect': 'action now',
    }[action]
    require(data, expected)
    now = parse_now(data['now'])
    if action == 'sync':
        entries = rows(data['schedules'], MAX_RULES)
        names = set()
        payloads = {}
        total = 0
        # Every complete rule is validated, including trailing ones, before any
        # count-based or tombstone-based conclusion can short-circuit.
        for entry in entries:
            require(entry, 'rule lead_minutes')
            lead = number(entry['lead_minutes'], 0, MAX_LEAD_MINUTES)
            if not isinstance(entry['rule'], dict):
                raise ValueError('schedule rule must be an object')
            generated = expand_reminders(entry['rule'], lead)
            name = generated[0]['schedule'] if generated else ident(entry['rule']['schedule'])
            if name in names:
                raise ValueError('duplicate schedule: ' + name)
            names.add(name)
            total += len(generated)
            if total > MAX_REMINDERS:
                raise ValueError('more than 8192 generated reminders')
            payloads[name] = generated
        return action, now, {'entries': entries, 'payloads': payloads, 'total': total}
    if action == 'claim':
        limit = number(data['limit'], 1, MAX_LIMIT)
        lease_minutes = number(data['lease_minutes'], 1, MAX_LEASE_MINUTES)
        return action, now, {'limit': limit, 'lease_minutes': lease_minutes}
    if action == 'ack':
        schedule = ident(data['schedule'])
        token = ident(data['token'])
        nominal_text = data['nominal_date']
        if not isinstance(nominal_text, str):
            raise ValueError('nominal_date must be a YYYY-MM-DD string')
        try:
            nominal = date.fromisoformat(nominal_text)
        except ValueError as error:
            raise ValueError('nominal_date must be YYYY-MM-DD') from error
        if nominal.isoformat() != nominal_text:
            raise ValueError('nominal_date must be YYYY-MM-DD')
        return action, now, {'schedule': schedule,
                             'nominal_date': nominal.isoformat(), 'token': token}
    return action, now, {}


def _connect(db_path, may_create):
    """Open the database file and decide whether it needs first-sync creation.

    The schema itself is created inside the caller's write transaction so a
    failed first sync never leaves an initialized database behind.
    """
    if not os.path.exists(db_path):
        if not may_create:
            raise ValueError('database does not exist')
        parent = os.path.dirname(os.path.abspath(db_path))
        if not os.path.isdir(parent):
            raise ValueError('database directory does not exist')
    connection = sqlite3.connect(db_path, timeout=BUSY_WAIT_SECONDS,
                                 isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=10000')
    # WAL allows the read-only inspect to run concurrently with one writer;
    # every mutation still takes BEGIN IMMEDIATE to serialize check-and-change.
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA foreign_keys=ON')
    existing = {row[0] for row in
                connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not existing:
        if not may_create:
            connection.close()
            raise ValueError('database is not initialized')
        return connection, True
    if not set(_SCHEMA_TABLES) <= existing:
        connection.close()
        raise ValueError('database is not a calendar-reminders database')
    return connection, False


def _discard_if_tableless(db_path):
    """Remove an empty, never-committed first-sync database, only while empty.

    Re-reading the table list makes this safe against another process winning
    the first-sync race and committing real tables into the same file.
    """
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


_SCHEMA_STATEMENTS = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE seq (name TEXT PRIMARY KEY, value INTEGER NOT NULL)",
    """
    CREATE TABLE snapshot_rules (
        schedule TEXT PRIMARY KEY,
        rule_json TEXT NOT NULL,
        lead_minutes INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE reminders (
        schedule TEXT NOT NULL,
        nominal_date TEXT NOT NULL,
        timezone TEXT NOT NULL,
        local_start TEXT NOT NULL,
        start_utc TEXT NOT NULL,
        end_utc TEXT NOT NULL,
        due_utc TEXT NOT NULL,
        PRIMARY KEY (schedule, nominal_date)
    )
    """,
    """
    CREATE TABLE leases (
        schedule TEXT NOT NULL,
        nominal_date TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        lease_until TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (schedule, nominal_date),
        FOREIGN KEY (schedule, nominal_date)
            REFERENCES reminders(schedule, nominal_date)
    )
    """,
    """
    CREATE TABLE completions (
        schedule TEXT NOT NULL,
        nominal_date TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        PRIMARY KEY (schedule, nominal_date)
    )
    """,
    "INSERT INTO seq(name, value) VALUES ('token', 0)",
)


def _create_schema(connection):
    # Individual execute() calls, never executescript(): the latter commits any
    # open transaction implicitly and would break first-sync atomicity.
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)


def _get_meta(connection, key):
    row = connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return None if row is None else row['value']


def _set_meta(connection, key, value):
    connection.execute(
        'INSERT INTO meta(key, value) VALUES(?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def _new_token(connection):
    """Allocate a token that can never repeat for the database's whole life."""
    counter = connection.execute(
        "SELECT value FROM seq WHERE name='token'").fetchone()['value'] + 1
    connection.execute("UPDATE seq SET value=? WHERE name='token'", (counter,))
    return f'{counter:012d}-{uuid.uuid4().hex}'


def _payload_from_row(row):
    return {
        'schedule': row['schedule'],
        'nominal_date': row['nominal_date'],
        'timezone': row['timezone'],
        'local_start': row['local_start'],
        'start_utc': row['start_utc'],
        'end_utc': row['end_utc'],
        'due_utc': row['due_utc'],
    }


def _check_clock(connection, now):
    last_now = _get_meta(connection, 'last_now')
    now_text = format_utc(now)
    if last_now is not None and now_text < last_now:
        raise ValueError('now must not be earlier than last_now')
    return last_now, now_text


def _sync(connection, now_text, detail):
    active = connection.execute(
        'SELECT schedule, nominal_date, payload_json FROM leases '
        'WHERE lease_until > ? ORDER BY schedule, nominal_date',
        (now_text,)).fetchall()
    new_payloads = {
        (payload['schedule'], payload['nominal_date']): payload
        for generated in detail['payloads'].values() for payload in generated}
    # Every still-effective, uncompleted lease must find an identical full
    # payload in the new generation.  The whole check finishes before any write.
    for lease in active:
        identity = (lease['schedule'], lease['nominal_date'])
        if identity not in new_payloads or \
                json.loads(lease['payload_json']) != new_payloads[identity]:
            raise ValueError('snapshot change conflicts with an active lease')
    connection.execute('DELETE FROM leases WHERE lease_until <= ?', (now_text,))
    connection.execute('DELETE FROM snapshot_rules')
    connection.execute('DELETE FROM reminders')
    for entry in detail['entries']:
        name = ident(entry['rule']['schedule'])
        connection.execute(
            'INSERT INTO snapshot_rules(schedule, rule_json, lead_minutes) '
            'VALUES(?, ?, ?)',
            (name, json.dumps(entry['rule'], sort_keys=True), entry['lead_minutes']))
        for payload in detail['payloads'][name]:
            connection.execute(
                'INSERT INTO reminders(schedule, nominal_date, timezone, local_start, '
                'start_utc, end_utc, due_utc) VALUES(?, ?, ?, ?, ?, ?, ?)',
                (payload['schedule'], payload['nominal_date'], payload['timezone'],
                 payload['local_start'], payload['start_utc'], payload['end_utc'],
                 payload['due_utc']))
    unfinished = connection.execute(
        'SELECT COUNT(*) AS n FROM reminders r WHERE NOT EXISTS '
        '(SELECT 1 FROM completions c WHERE c.schedule=r.schedule '
        'AND c.nominal_date=r.nominal_date)').fetchone()['n']
    _set_meta(connection, 'last_now', now_text)
    return {'action': 'sync', 'last_now': now_text,
            'scheduled_count': detail['total'], 'unfinished_count': unfinished}


def _claim(connection, now_text, detail):
    lease_until = format_utc(
        datetime.strptime(now_text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        + timedelta(minutes=detail['lease_minutes']))
    rows = connection.execute(
        'SELECT r.* FROM reminders r WHERE r.due_utc <= ? '
        'AND NOT EXISTS (SELECT 1 FROM completions c WHERE c.schedule=r.schedule '
        'AND c.nominal_date=r.nominal_date) '
        'AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.schedule=r.schedule '
        'AND l.nominal_date=r.nominal_date AND l.lease_until > ?) '
        'ORDER BY r.due_utc, r.schedule, r.nominal_date LIMIT ?',
        (now_text, now_text, detail['limit'])).fetchall()
    claims = []
    for row in rows:
        payload = _payload_from_row(row)
        token = _new_token(connection)
        connection.execute(
            'INSERT INTO leases(schedule, nominal_date, token, lease_until, payload_json) '
            'VALUES(?, ?, ?, ?, ?)',
            (payload['schedule'], payload['nominal_date'], token, lease_until,
             json.dumps(payload, sort_keys=True)))
        claims.append({'payload': payload, 'token': token, 'lease_until': lease_until})
    _set_meta(connection, 'last_now', now_text)
    return {'action': 'claim', 'last_now': now_text, 'claims': claims}


def _ack(connection, now_text, detail):
    identity = (detail['schedule'], detail['nominal_date'])
    tombstone = connection.execute(
        'SELECT token, payload_json, completed_at FROM completions '
        'WHERE schedule=? AND nominal_date=?', identity).fetchone()
    if tombstone is not None:
        # Idempotent retry only with the exact original token.  The stored
        # payload and completed_at are never rewritten.
        if tombstone['token'] != detail['token']:
            raise ValueError('ack token does not match completion tombstone')
        _set_meta(connection, 'last_now', now_text)
        return {'action': 'ack', 'last_now': now_text, 'duplicate': True,
                'completed': {'payload': json.loads(tombstone['payload_json']),
                              'token': tombstone['token'],
                              'completed_at': tombstone['completed_at']}}
    lease = connection.execute(
        'SELECT token, lease_until, payload_json FROM leases '
        'WHERE schedule=? AND nominal_date=?', identity).fetchone()
    if lease is None or lease['token'] != detail['token'] \
            or lease['lease_until'] <= now_text:
        raise ValueError('unknown or expired ack token')
    payload = json.loads(lease['payload_json'])
    connection.execute(
        'INSERT INTO completions(schedule, nominal_date, token, payload_json, completed_at) '
        'VALUES(?, ?, ?, ?, ?)',
        (identity[0], identity[1], detail['token'], lease['payload_json'], now_text))
    connection.execute('DELETE FROM leases WHERE schedule=? AND nominal_date=?', identity)
    _set_meta(connection, 'last_now', now_text)
    return {'action': 'ack', 'last_now': now_text, 'duplicate': False,
            'completed': {'payload': payload, 'token': detail['token'],
                          'completed_at': now_text}}


def _inspect(connection, now_text):
    jobs = []
    rows = connection.execute(
        'SELECT r.*, l.lease_until AS active_until FROM reminders r '
        'LEFT JOIN leases l ON l.schedule=r.schedule AND l.nominal_date=r.nominal_date '
        'WHERE NOT EXISTS (SELECT 1 FROM completions c WHERE c.schedule=r.schedule '
        'AND c.nominal_date=r.nominal_date) '
        'ORDER BY r.due_utc, r.schedule, r.nominal_date').fetchall()
    for row in rows:
        if row['active_until'] is not None and row['active_until'] > now_text:
            state, lease_until = 'leased', row['active_until']
        else:
            # An expired lease is presented as ready/wearing without any write.
            lease_until = None
            state = 'ready' if row['due_utc'] <= now_text else 'waiting'
        jobs.append({'payload': _payload_from_row(row), 'state': state,
                     'lease_until': lease_until})
    completed = [
        {'payload': json.loads(row['payload_json']), 'token': row['token'],
         'completed_at': row['completed_at']}
        for row in connection.execute(
            'SELECT payload_json, token, completed_at FROM completions '
            'ORDER BY schedule, nominal_date')]
    return {'action': 'inspect', 'last_now': _get_meta(connection, 'last_now'),
            'jobs': jobs, 'completed': completed}


def process(db_path, data):
    """Validate and execute one reminder document against the SQLite database.

    Any failure raises and leaves the logical database state untouched; a
    rejected first sync does not even leave an initialized database behind.
    """
    action, now, detail = _validate_document(data)
    connection, created = _connect(db_path, may_create=(action == 'sync'))
    try:
        connection.execute('BEGIN IMMEDIATE')
        # Snapshot replacement deletes and rebuilds reminders while active
        # leases keep referencing them; defer the lease->reminders foreign key
        # to commit time, when every surviving lease points at a regenerated row.
        connection.execute('PRAGMA defer_foreign_keys=ON')
        try:
            if created:
                # Schema creation is part of the first-sync transaction, so a
                # rollback removes it together with every provisional row.
                _create_schema(connection)
            _last_now, now_text = _check_clock(connection, now)
            if action == 'sync':
                result = _sync(connection, now_text, detail)
            elif action == 'claim':
                result = _claim(connection, now_text, detail)
            elif action == 'ack':
                result = _ack(connection, now_text, detail)
            else:
                result = _inspect(connection, now_text)
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
    except BaseException:
        connection.close()
        if created:
            _discard_if_tableless(db_path)
        raise
    connection.close()
    return {'schema_version': 1, 'result': result}
