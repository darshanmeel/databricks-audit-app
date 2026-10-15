"""tests/test_findings/test_overview_serverless_classic_split.py -- T-11 port assertions.

The overview_serverless_classic_split model reads system.billing.usage rows from the billing
fixture (T-09) and aggregates net DBU split by workspace between serverless and classic, plus
Photon and untagged shares. Tests assert grain uniqueness, window exclusions, and correctness of
the total plus every per-workspace serverless/photon/untagged breakdown against the raw fixture
parquet.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Add parent directory to path to import dbutil
import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dbutil import rows, usage_sum

_WINDOW_30 = "usage_date >= DATE '2026-09-21' - INTERVAL 30 DAY AND usage_date < DATE '2026-09-21'"


def test_overview_serverless_classic_split():
    """Assert grain, window exclusions, and correctness of f_overview_serverless_classic_split."""
    query_id = "overview_serverless_classic_split"

    # (a) rows(id, 30) has >= 1 row all with window_days == 30
    rows_30 = rows(query_id, 30)
    assert len(rows_30) >= 1, f"Expected >= 1 rows at window_days=30, got {len(rows_30)}"
    for row in rows_30:
        assert row["window_days"] == 30, f"Expected window_days=30, got {row['window_days']}"

    # (b) no duplicate grain (window_days, workspace_id)
    grain_keys = [(r["window_days"], r["workspace_id"]) for r in rows_30]
    assert len(grain_keys) == len(set(grain_keys)), f"Duplicate grain entries found in rows_30"

    # (c) the grand total across all rows equals an independent usage_sum() derivation off the
    # raw fixture parquet, filtered to the same 30-day window and DBU unit as the model.
    sum_30 = sum(r["total_net_dbus"] for r in rows_30)
    expected_sum_30 = usage_sum(f"usage_unit = 'DBU' AND {_WINDOW_30}")
    assert abs(sum_30 - expected_sum_30) < 1e-6, (
        f"30-day total mismatch: expected {expected_sum_30}, got {sum_30}"
    )

    # (d) every per-workspace untagged_net_dbus / serverless_net_dbus / photon_net_dbus equals a
    # filtered usage_sum() over the raw parquet -- summing usage_quantity restricted to the same
    # predicate the model's CASE expression uses is equivalent to the model's conditional SUM.
    for row in rows_30:
        ws_id = row["workspace_id"]
        ws_clause = "workspace_id IS NULL" if ws_id is None else f"workspace_id = '{ws_id}'"
        base = f"usage_unit = 'DBU' AND {_WINDOW_30} AND {ws_clause}"

        expected_untagged = usage_sum(f"{base} AND cardinality(custom_tags) = 0")
        assert abs(row["untagged_net_dbus"] - expected_untagged) < 1e-6, (
            f"untagged_net_dbus mismatch for workspace_id={ws_id}: "
            f"expected {expected_untagged}, got {row['untagged_net_dbus']}"
        )

        expected_serverless = usage_sum(f"{base} AND (product_features.is_serverless = TRUE OR upper(sku_name) LIKE '%SERVERLESS%' OR billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS', 'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS'))")
        assert abs(row["serverless_net_dbus"] - expected_serverless) < 1e-6, (
            f"serverless_net_dbus mismatch for workspace_id={ws_id}: "
            f"expected {expected_serverless}, got {row['serverless_net_dbus']}"
        )

        expected_photon = usage_sum(f"{base} AND product_features.is_photon = TRUE")
        assert abs(row["photon_net_dbus"] - expected_photon) < 1e-6, (
            f"photon_net_dbus mismatch for workspace_id={ws_id}: "
            f"expected {expected_photon}, got {row['photon_net_dbus']}"
        )

    # (e) window exclusions: rows(id, 90) sums more than 30 and rows(id, 7) sums less
    rows_90 = rows(query_id, 90)
    rows_7 = rows(query_id, 7)

    sum_90 = sum(r.get("total_net_dbus", 0) for r in rows_90)
    sum_7 = sum(r.get("total_net_dbus", 0) for r in rows_7)

    assert sum_90 > sum_30, f"Expected sum_90 > sum_30, got {sum_90} vs {sum_30}"
    assert sum_7 < sum_30, f"Expected sum_7 < sum_30, got {sum_7} vs {sum_30}"

    # (f) rows(id, 0) == []
    rows_0 = rows(query_id, 0)
    assert rows_0 == [], f"Expected no rows at window_days=0, got {len(rows_0)} rows"
