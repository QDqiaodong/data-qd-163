"""Recipient-specific calendar views and safe delta updates.

Operations publishes each external partner a *view* of the scheduled
activities: every expanded instance becomes at most one public row keyed by
``(alias, start_utc)``.  The recipient's highest access level among the
activity's publication groups decides whether the instance is hidden
(``none``), shown as a bare busy interval (``busy``) or shown with its title
(``detail``).  Internal rule ids, group names, access configuration, hidden
activities and the raw input digest never enter the published document.

A *delta* carries only the visible difference between two views of the same
recipient: complete replacement rows for new or changed public keys
(``upserts``) and public key objects for keys that must disappear
(``withdrawals``).  The receiver holds only its saved view — never the
internal source — and applies a delta only when recipient and base content
fingerprint both match, then verifies the target fingerprint before
atomically replacing the saved view.

Content fingerprint
-------------------
Both sides derive the view fingerprint independently: the object
``{schema_version, recipient, rows}`` is serialized with
``json.dumps(sort_keys=True, ensure_ascii=True, separators=(',', ':'))`` and
the UTF-8 bytes are hashed with SHA-256 (lowercase hex).  The fingerprint
field itself and any trailing newline are excluded.  The fingerprint
constrains content equality only; it authenticates no sender, and equal
content legitimately reproduces the same fingerprint (no monotonic sequence).

Correctness obligations of this module:

1. every snapshot is validated completely — including rules without any
   access — before a single row is rendered, so a malformed trailing
   activity can never hide behind an empty expansion or a ``none`` grant;
2. rows are rendered only from post-authorization visible content, so
   editing a hidden activity, an invisible title or an internal rule id
   cannot change any published byte, fingerprint or delta operation;
3. the apply path validates the saved view, the delta shape and every
   operation before touching anything, and only a result whose fingerprint
   equals ``target_sha256`` is written back.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from .common import ident, require, rows
from .model import plan

MAX_GROUPS = 64
MAX_RULES = 64
MAX_INSTANCES = 2048
MAX_ROWS = 2048
ACCESS_LEVELS = {'none': 0, 'busy': 1, 'detail': 2}

_HEX_TEXT = re.compile(r'^[0-9a-f]{64}$')
_UTC_TEXT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


def _text(value):
    """Contract string: ``common.ident`` rules plus Unicode scalar values only."""
    ident(value)
    if any('\ud800' <= char <= '\udfff' for char in value):
        raise ValueError('expected a valid Unicode scalar sequence')
    return value


def _format_utc(instant):
    """Canonical ``YYYY-MM-DDTHH:MM:SSZ``; historical seconds offsets kept."""
    return instant.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse_utc(text, field):
    """Strictly parse the only accepted public timestamp shape."""
    if not isinstance(text, str) or not _UTC_TEXT.match(text):
        raise ValueError(field + ' must be YYYY-MM-DDTHH:MM:SSZ in UTC')
    try:
        parsed = datetime.strptime(text, '%Y-%m-%dT%H:%M:%S%z')
    except ValueError as error:
        raise ValueError(field + ' must be a valid UTC second timestamp') from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(field + ' must be a valid UTC second timestamp')
    return parsed


def _snapshot(data):
    """Validate one ``{groups, activities}`` snapshot completely.

    Every rule is expanded through the shared ``model.plan`` capability even
    when the recipient has no access to it, and every instance counts toward
    the 2048 pre-merge bound.  Returns the activities with their effective
    access level (highest grant among the referenced groups; an empty member
    list means ``none``).
    """
    require(data, 'groups activities')
    access = {}
    for entry in rows(data['groups'], MAX_GROUPS):
        require(entry, 'group access')
        name = _text(entry['group'])
        if name in access:
            raise ValueError('duplicate group: ' + name)
        level = entry['access']
        if not isinstance(level, str) or level not in ACCESS_LEVELS:
            raise ValueError('access must be one of: none busy detail')
        access[name] = ACCESS_LEVELS[level]
    activities = []
    schedules = set()
    aliases = set()
    total_instances = 0
    for entry in rows(data['activities'], MAX_RULES):
        require(entry, 'rule alias title groups')
        alias = _text(entry['alias'])
        title = _text(entry['title'])
        refs = [_text(ref) for ref in rows(entry['groups'], MAX_GROUPS)]
        if len(refs) != len(set(refs)):
            raise ValueError('duplicate group reference in activity: ' + alias)
        level = 0
        for ref in refs:
            if ref not in access:
                raise ValueError('unknown group: ' + ref)
            level = max(level, access[ref])
        rule = entry['rule']
        if not isinstance(rule, dict):
            raise ValueError('calendar rule must be an object')
        try:
            planned = plan(rule)
        except TypeError as error:  # malformed timestamp value types
            raise ValueError('invalid calendar rule') from error
        _text(planned['schedule'])
        _text(planned['timezone'])
        if planned['schedule'] in schedules:
            raise ValueError('duplicate schedule: ' + planned['schedule'])
        if alias in aliases:
            raise ValueError('duplicate alias: ' + alias)
        schedules.add(planned['schedule'])
        aliases.add(alias)
        total_instances += len(planned['items'])
        activities.append({'alias': alias, 'title': title, 'level': level,
                           'items': planned['items']})
    if total_instances > MAX_INSTANCES:
        raise ValueError('more than 2048 expanded instances')
    return activities


def _render(activities):
    """Render the visible rows of one validated snapshot, keyed by public key."""
    visible = {}
    for activity in activities:
        if activity['level'] == 0:
            continue
        for item in activity['items']:
            start = _format_utc(item['instant'])
            key = (activity['alias'], start)
            if key in visible:
                # One rule expanding twice onto the same key carries identical
                # duration and activity information: a single row is emitted.
                continue
            row = {'alias': activity['alias'], 'start_utc': start,
                   'end_utc': _format_utc(item['end'])}
            if activity['level'] == ACCESS_LEVELS['detail']:
                row['title'] = activity['title']
            visible[key] = row
    return visible


def _fingerprint(recipient, row_list):
    """Content fingerprint of a view body (without the fingerprint field)."""
    body = {'schema_version': 1, 'recipient': recipient, 'rows': row_list}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True,
                           separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _view(recipient, visible):
    """Build the full-view object from rendered rows."""
    row_list = [visible[key] for key in sorted(visible)]
    return {'schema_version': 1, 'recipient': recipient, 'rows': row_list,
            'view_sha256': _fingerprint(recipient, row_list)}


def publish(data):
    """Validate a publish source and build the full view or delta object."""
    require(data, 'recipient current previous')
    recipient = _text(data['recipient'])
    current = _render(_snapshot(data['current']))
    if data['previous'] is None:
        return _view(recipient, current)
    previous = _render(_snapshot(data['previous']))
    upserts = [current[key] for key in sorted(current)
               if key not in previous or previous[key] != current[key]]
    withdrawals = [{'alias': key[0], 'start_utc': key[1]}
                   for key in sorted(previous) if key not in current]
    return {'schema_version': 1, 'recipient': recipient,
            'base_sha256': _fingerprint(recipient, [previous[key] for key in sorted(previous)]),
            'target_sha256': _fingerprint(recipient, [current[key] for key in sorted(current)]),
            'upserts': upserts, 'withdrawals': withdrawals}


def _public_row(row):
    """Validate one visible row (busy or detail); return ``(key, row)``."""
    if not isinstance(row, dict) or set(row) not in (
            {'alias', 'start_utc', 'end_utc'},
            {'alias', 'start_utc', 'end_utc', 'title'}):
        raise ValueError('expected row fields: alias start_utc end_utc [title]')
    alias = _text(row['alias'])
    start = _parse_utc(row['start_utc'], 'start_utc')
    end = _parse_utc(row['end_utc'], 'end_utc')
    if end <= start:
        raise ValueError('end_utc must be later than start_utc')
    canonical = {'alias': alias, 'start_utc': row['start_utc'],
                 'end_utc': row['end_utc']}
    if 'title' in row:
        canonical['title'] = _text(row['title'])
    return (alias, row['start_utc']), canonical


def _public_key(item):
    """Validate one withdrawal key object; return the public key."""
    require(item, 'alias start_utc')
    alias = _text(item['alias'])
    _parse_utc(item['start_utc'], 'start_utc')
    return alias, item['start_utc']


def _check_version(value):
    if type(value) is not int or value != 1:
        raise ValueError('schema_version must be the integer 1')


def _check_digest(value, field):
    if not isinstance(value, str) or not _HEX_TEXT.match(value):
        raise ValueError(field + ' must be 64 lowercase hex characters')
    return value


def _check_view(data):
    """Strictly validate a saved full view and recompute its fingerprint."""
    require(data, 'schema_version recipient rows view_sha256')
    _check_version(data['schema_version'])
    recipient = _text(data['recipient'])
    digest = _check_digest(data['view_sha256'], 'view_sha256')
    visible = {}
    previous_key = None
    for row in rows(data['rows'], MAX_ROWS):
        key, canonical = _public_row(row)
        if previous_key is not None and key <= previous_key:
            raise ValueError('rows must be strictly ordered by public key')
        previous_key = key
        visible[key] = canonical
    row_list = [visible[key] for key in sorted(visible)]
    if _fingerprint(recipient, row_list) != digest:
        raise ValueError('view fingerprint mismatch')
    return recipient, visible, digest


def _check_delta(data):
    """Strictly validate the delta shape and every operation it lists."""
    require(data, 'schema_version recipient base_sha256 target_sha256 upserts withdrawals')
    _check_version(data['schema_version'])
    recipient = _text(data['recipient'])
    base = _check_digest(data['base_sha256'], 'base_sha256')
    target = _check_digest(data['target_sha256'], 'target_sha256')
    upserts = {}
    previous_key = None
    for row in rows(data['upserts'], MAX_ROWS):
        key, canonical = _public_row(row)
        if previous_key is not None and key <= previous_key:
            raise ValueError('upserts must be strictly ordered by public key')
        previous_key = key
        upserts[key] = canonical
    withdrawals = []
    previous_key = None
    for item in rows(data['withdrawals'], MAX_ROWS):
        key = _public_key(item)
        if previous_key is not None and key <= previous_key:
            raise ValueError('withdrawals must be strictly ordered by public key')
        previous_key = key
        withdrawals.append(key)
    if set(upserts).intersection(withdrawals):
        raise ValueError('a public key cannot be both upserted and withdrawn')
    return recipient, base, target, upserts, withdrawals


def apply(view_data, delta_data):
    """Apply a validated delta to a saved view; return the new view object.

    Nothing is mutated before every check passes: the saved view is strictly
    validated and its fingerprint recomputed, the delta shape and all
    operations are validated, recipient and base fingerprint must match the
    view, withdrawals must reference existing keys, an upsert must not
    restate the existing row, and the resulting view — sorted and
    fingerprinted per the full-view contract — must equal ``target_sha256``.
    """
    recipient, visible, digest = _check_view(view_data)
    delta_recipient, base, target, upserts, withdrawals = _check_delta(delta_data)
    if delta_recipient != recipient:
        raise ValueError('delta recipient does not match the view recipient')
    if base != digest:
        raise ValueError('base fingerprint does not match the saved view')
    for key in withdrawals:
        if key not in visible:
            raise ValueError('withdrawal references a key not in the view')
    for key, row in upserts.items():
        if key in visible and visible[key] == row:
            raise ValueError('upsert restates the existing row')
    for key in withdrawals:
        del visible[key]
    visible.update(upserts)
    result_rows = [visible[key] for key in sorted(visible)]
    if len(result_rows) > MAX_ROWS:
        raise ValueError('more than 2048 rows in the resulting view')
    if _fingerprint(recipient, result_rows) != target:
        raise ValueError('target fingerprint does not match the resulting view')
    return {'schema_version': 1, 'recipient': recipient, 'rows': result_rows,
            'view_sha256': target}
