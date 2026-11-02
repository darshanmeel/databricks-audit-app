"""Usage class, index and signal (app/core/usage.py), with the edge cases of the Data usage design."""
import datetime as dt

import pytest

from app.core import usage

END = dt.date(2026, 9, 21)


def ago(days: int) -> str:
    return (END - dt.timedelta(days=days)).isoformat() + " 09:00:00"


@pytest.mark.parametrize("reads, writes, last_read, last_write, created, want", [
    (5, 3, ago(0), ago(0), ago(300), ("active", False)),  # read and written today
    (0, 0, None, ago(400), ago(500), ("idle_90d", False)),  # never read, last written 400 days ago
    (0, 2, None, ago(1), ago(300), ("written_never_read", False)),  # never read, written yesterday
    (0, 0, ago(45), None, ago(300), ("cooling", False)),  # read 45 days ago, window 30
    (4, 0, ago(45), None, ago(300), ("active", False)),  # the same table in a 90-day window
    (0, 0, None, None, ago(3), ("active", True)),  # new, no lineage yet: not shamed
    (0, 0, None, None, ago(30), ("idle_90d", False)),  # no lineage and not new
    (0, 0, ago(90), None, ago(300), ("idle_90d", False)),  # exactly 90 days: idle
])
def test_usage_class(reads, writes, last_read, last_write, created, want):
    assert usage.usage_class(reads, writes, last_read, last_write, created, END) == want


def test_scale_is_log_against_the_largest_and_zero_when_nobody_has_it():
    assert usage.scale([0, 0]) == [0.0, 0.0]
    got = usage.scale([0, 9, 99])
    assert got[0] == 0 and got[2] == 100 and 40 < got[1] < 60


def test_indexes_reach_100_for_the_largest_and_count_active_days_linearly():
    rows = [{"views": 10, "view_columns": 100, "tables": 50, "table_columns": 500, "volume_files": 1000,
             "size_in_bytes": 10**12, "active_people": 20, "active_service_principals": 3, "queries": 5000, "active_days": 30},
            {"views": 0, "view_columns": 0, "tables": 0, "table_columns": 0, "volume_files": 0, "size_in_bytes": 0,
             "active_people": 0, "active_service_principals": 0, "queries": 0, "active_days": 15}]
    assert usage.complexity_index(rows) == [100.0, 0.0]
    assert usage.usage_index(rows, 30) == [100.0, 10.0]  # half the days: 0.20 x 50
    assert sum(usage.COMPLEXITY_WEIGHTS.values()) == pytest.approx(1)
    assert sum(usage.USAGE_WEIGHTS.values()) + usage.ACTIVE_DAYS_WEIGHT == pytest.approx(1)


@pytest.mark.parametrize("complexity, use, want", [
    (80, 5, "Retire candidate"), (60, 30, "Review"), (30, 30, "Light"), (60, 70, "Healthy"), (None, 40, "Light"),
])
def test_signal(complexity, use, want):
    assert usage.signal(complexity, use) == want


def test_clean_up_first_puts_unread_sensitive_tables_first_then_class_then_size():
    rows = [{"name": "big_idle", "usage_class": "idle_90d", "size_in_bytes": 900},
            {"name": "small_wo", "usage_class": "written_never_read", "size_in_bytes": 1},
            {"name": "pii_idle", "usage_class": "idle_90d", "size_in_bytes": 5, "sensitive": ["Email"]},
            {"name": "big_wo", "usage_class": "written_never_read", "size_in_bytes": 50}]
    assert [r["name"] for r in sorted(rows, key=usage.cleanup_order)] == ["pii_idle", "big_wo", "small_wo", "big_idle"]
