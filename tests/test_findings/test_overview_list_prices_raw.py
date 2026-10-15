"""tests/test_findings/test_overview_list_prices_raw.py -- T-11 port assertions.

The overview_list_prices_raw model is a windowless inventory query that reads
system.billing.list_prices and emits raw pricing structs as JSON. Tests assert that the model
produces exactly one window_days=0 row set (no window parameter).
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Add parent directory to path to import dbutil
import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dbutil import rows


def test_overview_list_prices_raw():
    """Assert grain and window behavior of f_overview_list_prices_raw (windowless inventory)."""
    query_id = "overview_list_prices_raw"

    # (a) rows(id, 0) has >= 1 row (windowless inventory)
    rows_0 = rows(query_id, 0)
    assert len(rows_0) >= 1, f"Expected >= 1 rows at window_days=0 (windowless), got {len(rows_0)}"

    for row in rows_0:
        assert row["window_days"] == 0, f"Expected window_days=0, got {row['window_days']}"

    # (b) grain uniqueness: (window_days, sku_name, cloud, currency_code, price_start_time)
    grain_keys = [(r["window_days"], r["sku_name"], r["cloud"], r["currency_code"], r["price_start_time"]) for r in rows_0]
    assert len(grain_keys) == len(set(grain_keys)), f"Duplicate grain entries found"

    # (c) rows(id, 30) and other window_days values should be empty (windowless query)
    rows_30 = rows(query_id, 30)
    assert rows_30 == [], f"Expected no rows at window_days=30 (windowless query), got {len(rows_30)} rows"

    rows_90 = rows(query_id, 90)
    assert rows_90 == [], f"Expected no rows at window_days=90 (windowless query), got {len(rows_90)} rows"

    rows_7 = rows(query_id, 7)
    assert rows_7 == [], f"Expected no rows at window_days=7 (windowless query), got {len(rows_7)} rows"
