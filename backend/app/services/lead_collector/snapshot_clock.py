"""Timestamp provenance for admission; absence never becomes a new observation."""
import math
import time
from decimal import Decimal, InvalidOperation


def event_timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed <= 0 or parsed != parsed.to_integral_value():
            return None
        if parsed > int(time.time() * 1000):
            return None
        return int(parsed)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def event_interval(start, end, observed_ms=None):
    start, end = event_timestamp(start), event_timestamp(end)
    if start is None or end is None or end < start:
        return None
    if observed_ms is not None:
        observed = event_timestamp(observed_ms)
        if observed is None or end > observed:
            return None
    return start, end


def closed_interval(closed, observed_ms=None):
    if not isinstance(closed, dict):
        return None
    interval = event_interval(closed.get('opened'), closed.get('closed'), observed_ms)
    if interval is None or interval[0] == interval[1]:
        return None
    if 'updateTime' in closed:
        updated = event_timestamp(closed['updateTime'])
        if updated is None or updated < interval[1] or (observed_ms is not None and updated > observed_ms):
            return None
    return interval


def source_batch_interval(payload, observed_ms):
    observed_ms = event_timestamp(observed_ms)
    if observed_ms is None:
        return None
    interval = event_interval(payload.get('_source_start_ms', payload.get('startTime')),
                              payload.get('_source_time_ms', payload.get('endTime')), observed_ms)
    if interval is None:
        return None
    for field, expected in (('startTime', interval[0]), ('endTime', interval[1]), ('time', interval[1])):
        if field in payload and event_timestamp(payload[field]) != expected:
            return None
    if '_observed_at_ms' in payload:
        observed = event_timestamp(payload['_observed_at_ms'])
        if (observed is None or not interval[1] <= observed <= observed_ms or
                int(time.time() * 1000) - observed > 30000):
            return None
    for item in payload.get('items', []):
        if not isinstance(item, dict):
            return None
        item_interval = event_interval(item.get('orderTime'), item.get('time'), observed_ms)
        if item_interval is None or item_interval[0] < interval[0] or item_interval[1] > interval[1]:
            return None
    return interval


def snapshot_observed_ms(snapshot):
    clocks = snapshot_times(snapshot)
    if clocks is None:
        return None
    holdings = snapshot.get('openPositionsMeta') or {}
    if 'asOf' in holdings:
        return clocks.get('asOf')
    return clocks.get('generatedAt')


def snapshot_source_bound_ms(snapshot, received_at_ms=None):
    """Bound collected source events without inventing a holdings observation."""
    clocks = snapshot_times(snapshot)
    if clocks is None:
        return None
    bounds = [clocks[source] for source in ('asOf', 'generatedAt') if source in clocks]
    if not bounds:
        bounds.append(clocks['collectionStartedAtMs'])
    if received_at_ms is not None:
        received = event_timestamp(received_at_ms)
        if received is None or any(clock > received for clock in clocks.values()):
            return None
        bounds.append(received)
    return min(bounds) if bounds else None


def snapshot_times(snapshot, now_ms=None, max_age_ms=30000):
    if not isinstance(snapshot, dict):
        return None
    holdings = snapshot.get('openPositionsMeta') or {}
    metadata = snapshot.get('meta') or {}
    if not isinstance(holdings, dict) or not isinstance(metadata, dict):
        return None
    now = int(time.time() * 1000) if now_ms is None else now_ms
    values = {}
    for source, value in (('asOf', holdings.get('asOf')),
                          ('generatedAt', metadata.get('generatedAt')),
                          ('collectionStartedAtMs', metadata.get('collectionStartedAtMs'))):
        if value is None:
            continue
        if (type(value) not in (int, float) or value <= 0 or
                (type(value) is float and not math.isfinite(value))):
            return None
        if not 0 <= now - value <= max_age_ms:
            return None
        values[source] = value
    if not values:
        return None
    started = values.get('collectionStartedAtMs')
    generated = values.get('generatedAt')
    if started is not None and generated is not None and started > generated:
        return None
    observed = values.get('asOf')
    if observed is not None and generated is not None and observed > generated:
        return None
    if observed is not None and started is not None and started > observed:
        return None
    return values


def snapshot_is_current(incoming, saved, now_ms=None):
    incoming_times = snapshot_times(incoming, now_ms)
    if incoming_times is None:
        return False
    if not isinstance(saved, dict):
        return False
    if not saved:
        return True
    saved_times = snapshot_times(saved, now_ms)
    if saved_times is None:
        return False
    sources = ('collectionStartedAtMs', 'generatedAt', 'asOf')
    incoming_source = next(source for source in sources if source in incoming_times)
    saved_source = next(source for source in sources if source in saved_times)
    if incoming_source != saved_source:
        return False
    for source in incoming_times.keys() & saved_times.keys():
        if saved_times[source] > incoming_times[source]:
            return False
    if incoming_times[incoming_source] == saved_times[saved_source] and incoming != saved:
        return False
    failed = (saved.get('openPositionsMeta') or {}).get('lastFailureStartedAtMs')
    if failed is not None:
        if (type(failed) not in (int, float) or failed <= 0 or
                (type(failed) is float and not math.isfinite(failed))):
            return False
        generation = next(incoming_times[source] for source in
                          ('collectionStartedAtMs', 'generatedAt', 'asOf') if source in incoming_times)
        if failed >= generation:
            return False
    return True
