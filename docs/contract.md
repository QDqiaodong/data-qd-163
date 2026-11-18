# Zoned weekly schedule expansion v1

Expand one local weekly recurrence with schedule id, IANA timezone, naive minute-precision local_start, interval_weeks 1..52, ISO weekdays 1..7, inclusive until within 3660 days, duration_minutes 1..10080, and excluded local dates. The recurrence is anchored to the Monday week containing local_start; only dates on/after local_start that match interval and weekday are emitted.

Nonexistent wall times follow gap_policy skip or shift_forward (advance minute-by-minute to the first valid local minute). Ambiguous wall times follow fold_policy earlier or later, chosen by UTC instant. Duration is elapsed time after conversion to UTC, so DST transitions do not distort it. Output records both resolved local start and UTC start/end.

The synthetic New York fixture covers a spring gap and autumn fold. This is a single recurrence rule, not an RFC 5545 parser, shared-resource booking system, reminder service or legal calendar.
