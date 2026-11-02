"""Data usage: the usage class of a table or volume, the complexity and usage index of a workspace,
and its signal. The one place these rules and weights live; the Data usage screens and the AI/BI
Workspace Usage dashboard follow them.
"""
from __future__ import annotations

import datetime as dt
import math

IDLE_DAYS = 90  # no read and no write for this long: idle
NEW_DAYS = 7  # created this recently with no lineage yet: active, marked new

# exactly one per table, the first rule that holds wins (attention order)
CLASSES = (
    ("written_never_read", "Written, never read", "Something pays to write data nobody reads. Pause the writer."),
    ("idle_90d", "Idle 90d+", "No read and no write in 90 days. Archive or drop."),
    ("cooling", "Cooling", "Not read in the window, used within 90 days. Ask the owner."),
    ("active", "Active", "Read in the window. Check who reads and who writes it."),
)
CLASS_RANK = {key: i for i, (key, _label, _meaning) in enumerate(CLASSES)}
UNREAD_CLASSES = ("written_never_read", "idle_90d")  # their size is a workspace's unread GB

# each input on a log scale against the largest workspace shown; the weights sum to 1
COMPLEXITY_WEIGHTS = {"views": 0.20, "view_columns": 0.15, "tables": 0.20, "table_columns": 0.15,
                      "volume_files": 0.10, "size_in_bytes": 0.20}
USAGE_WEIGHTS = {"active_people": 0.35, "active_service_principals": 0.15, "queries": 0.30}
ACTIVE_DAYS_WEIGHT = 0.20  # share of the window's days with any activity, not log scaled


def _day(value) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def usage_class(reads: int, writes: int, last_read, last_write, created, end: dt.date,
                idle_days: int = IDLE_DAYS) -> tuple[str, bool]:
    """(class, new). reads and writes count the window; last_read and last_write look back a year."""
    reads, writes = int(reads or 0), int(writes or 0)
    if writes > 0 and reads == 0:
        return "written_never_read", False
    born = _day(created)
    if reads == 0 and writes == 0 and last_read in (None, "") and last_write in (None, "") \
            and born is not None and (end - born).days < NEW_DAYS:
        return "active", True  # too new to judge
    recent = [d for d in (_day(last_read), _day(last_write)) if d is not None]
    if not recent or (end - max(recent)).days >= idle_days:
        return "idle_90d", False
    if reads == 0:
        return "cooling", False
    return "active", False


def scale(values: list) -> list[float]:
    """0..100 per value against the largest, on a log scale; all 0 when the largest is 0."""
    top = max((math.log1p(v or 0) for v in values), default=0)
    return [100 * math.log1p(v or 0) / top if top else 0.0 for v in values]


def complexity_index(rows: list[dict]) -> list[float]:
    scaled = {k: scale([r.get(k) or 0 for r in rows]) for k in COMPLEXITY_WEIGHTS}
    return [round(sum(w * scaled[k][i] for k, w in COMPLEXITY_WEIGHTS.items()), 1) for i in range(len(rows))]


def usage_index(rows: list[dict], window_days: int) -> list[float]:
    scaled = {k: scale([r.get(k) or 0 for r in rows]) for k in USAGE_WEIGHTS}
    days = max(int(window_days or 1), 1)
    return [round(sum(w * scaled[k][i] for k, w in USAGE_WEIGHTS.items())
                  + ACTIVE_DAYS_WEIGHT * 100 * min(int(r.get("active_days") or 0), days) / days, 1)
            for i, r in enumerate(rows)]


def signal(complexity: float | None, usage: float | None) -> str:
    """Retire candidate, Review, Light or Healthy."""
    usage = usage or 0
    if usage < 10:
        return "Retire candidate"
    if complexity is not None and complexity >= 50 and usage < 50:
        return "Review"
    if complexity is None or complexity < 50:
        return "Light"
    return "Healthy"


def cleanup_order(t: dict) -> tuple:
    """Clean up first: unread sensitive tables, then written-never-read before idle, then biggest."""
    return (not (t.get("sensitive") and t.get("usage_class") in UNREAD_CLASSES),
            CLASS_RANK.get(t.get("usage_class"), 9), -(t.get("size_in_bytes") or 0))
