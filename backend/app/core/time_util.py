from datetime import datetime, timedelta, timezone
from typing import Optional


BEIJING_TZ = timezone(timedelta(hours=8))


def to_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """SQLite's naive timestamps are UTC; always make that explicit in JSON."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
