"""Public freshness uses the original observation time, never the failure time."""
from __future__ import annotations

import math
import time
from datetime import timezone

from app.core.config import get_settings


def lead_freshness(lead, snapshot: dict, now_ms: int | None = None) -> dict:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    generated = (snapshot.get("meta") or {}).get("generatedAt")
    if generated is None and lead.last_sync_at is not None:
        generated = int(lead.last_sync_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    valid = isinstance(generated, (int, float)) and math.isfinite(generated) and 0 < generated <= now
    age = (now - generated) / 1000 if valid else None
    failures = (snapshot.get("openPositionsMeta") or {}).get("consecutiveSyncFailures", 0)
    return {
        "generatedAt": generated if valid else None,  # UTC epoch milliseconds
        "stale": lead.status == "error" or age is None or age > get_settings().lead_snapshot_stale_sec,
        "snapshotAgeSec": age,
        "staleAfterSec": get_settings().lead_snapshot_stale_sec,
        "consecutiveSyncFailures": failures,
    }
