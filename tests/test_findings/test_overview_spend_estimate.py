"""tests/test_findings/test_overview_spend_estimate.py -- T-11 port assertions.

The overview_spend_estimate model reads system.billing.usage and list_prices rows from the
billing fixture (T-09) and aggregates net spend (net DBUs x list price) by workspace. Tests
assert grain uniqueness, window exclusions, and correctness of the aggregated totals -- both
total_net_dbus and the dollarized net_list_cost_usd -- against the raw fixture parquet.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

# Add parent directory to path to import dbutil
import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from dbutil import rows, usage_sum
import billing as bl  # noqa: E402 -- T-69A review round 2: bl.WS_PROD for the price_basis assertion

PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"


def _spend_by_workspace(window_days: int) -> dict:
    """Independently dollarize billing__usage against the CURRENT (price_end_time IS NULL)
    billing__list_prices row per (sku_name, cloud), grouped by workspace_id, for the trailing
    window ending at the pinned test target's "today" (DATE '2026-09-21').

    This is deliberately simpler than the model's own date-range price join (it just picks the
    one row with price_end_time IS NULL instead of matching usage_end_time against
    [price_start_time, price_end_time)) -- an independent computation straight from the
    builder's own parquet, not a re-run of the model's own SQL. It holds for this fixture
    because every usage row's usage_date falls after the current price's price_start_time and
    before the expired price's price_end_time (tests/fixtures/billing.py's priced_sku()).

    A SKU with no matching list_prices row (bl_SKU_NO_PRICE) LEFT JOINs to NULL, so its DBUs
    still land in total_net_dbus but its dollar contribution is NULL and SUM() silently skips
    it -- net_list_cost_usd for a workspace whose priced-vs-unpriced usage nets to no matched
    price at all is NULL, not 0.

    Also computes price_basis with the same CASE overview_spend_estimate.sql itself uses
    (T-69A review round 2): 'unpriced' when the workspace's window has any non-FREE_USAGE SKU
    with no matching price row (a coverage gap), 'free' when every matched SKU is FREE_USAGE (a
    real $0), else 'priced'. Returns {workspace_id: (total_net_dbus, net_list_cost_usd,
    price_basis)}.
    """
    usage_glob = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    price_glob = (PARQUET_DIR / "billing__list_prices" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"""
            SELECT u.workspace_id,
                   SUM(u.usage_quantity) AS total_net_dbus,
                   SUM(u.usage_quantity * lp.list_rate) AS net_list_cost_usd,
                   CASE
                     WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                                   THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
                     WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
                     ELSE 'priced'
                   END AS price_basis
            FROM read_parquet('{usage_glob}', union_by_name=true) u
            LEFT JOIN (
                SELECT sku_name, cloud, CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
                FROM read_parquet('{price_glob}', union_by_name=true)
                WHERE price_end_time IS NULL
            ) lp ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud
            WHERE u.usage_unit = 'DBU'
              AND u.usage_date >= DATE '2026-09-21' - INTERVAL {int(window_days)} DAY
              AND u.usage_date < DATE '2026-09-21'
            GROUP BY u.workspace_id
        """
        return {ws: (total, cost, basis) for ws, total, cost, basis in con.execute(sql).fetchall()}
    finally:
        con.close()


def test_overview_spend_estimate():
    """Assert grain, window exclusions, and dollarized aggregation correctness of
    f_overview_spend_estimate."""
    query_id = "overview_spend_estimate"

    # (a) rows(id, 30) has >= 1 row all with window_days == 30
    rows_30 = rows(query_id, 30)
    assert len(rows_30) >= 1, f"Expected >= 1 rows at window_days=30, got {len(rows_30)}"
    for row in rows_30:
        assert row["window_days"] == 30, f"Expected window_days=30, got {row['window_days']}"

    # (b) no duplicate grain (window_days, workspace_id)
    grain_keys = [(r["window_days"], r["workspace_id"]) for r in rows_30]
    assert len(grain_keys) == len(set(grain_keys)), f"Duplicate grain entries found in rows_30"

    # (c) per-workspace total_net_dbus and net_list_cost_usd match an independent parquet
    # derivation (not a pasted literal -- see _spend_by_workspace's docstring). This also proves
    # bl_SKU_NO_PRICE's DBUs count toward total_net_dbus but are excluded from the dollar sum
    # (acme-prod/bl.WS_PROD carries this row alongside priced usage in this fixture -- see the
    # price_basis assertion below for the workspace this actually is).
    expected = _spend_by_workspace(30)
    assert set(r["workspace_id"] for r in rows_30) == set(expected.keys()), (
        f"workspace set mismatch: model {sorted((r['workspace_id'] for r in rows_30), key=str)} "
        f"vs parquet {sorted(expected.keys(), key=str)}"
    )
    for row in rows_30:
        ws = row["workspace_id"]
        exp_total, exp_cost, exp_basis = expected[ws]
        assert abs(row["total_net_dbus"] - exp_total) < 1e-6, (
            f"total_net_dbus mismatch for workspace_id={ws!r}: "
            f"expected {exp_total}, got {row['total_net_dbus']}"
        )
        if exp_cost is None:
            assert row["net_list_cost_usd"] is None, (
                f"expected NULL net_list_cost_usd for workspace_id={ws!r} "
                f"(unpriced-only usage), got {row['net_list_cost_usd']}"
            )
        else:
            assert abs(row["net_list_cost_usd"] - round(exp_cost, 2)) < 0.01, (
                f"net_list_cost_usd mismatch for workspace_id={ws!r}: "
                f"expected {round(exp_cost, 2)}, got {row['net_list_cost_usd']}"
            )
        # price_basis (T-69A review round 2): matches the independent CASE replica for every
        # workspace, never a pasted literal.
        assert row["price_basis"] == exp_basis, (
            f"price_basis mismatch for workspace_id={ws!r}: expected {exp_basis!r}, got {row['price_basis']!r}"
        )

    # acme-prod (bl.WS_PROD) carries bl_u_job_ok's deliberately-unpriced bl_SKU_NO_PRICE (D(1),
    # DBU, inside every window) alongside plenty of priced DBU usage -- one non-FREE_USAGE
    # unpriced row is enough to taint the whole workspace group to price_basis='unpriced' (the
    # same presence rule every other price_basis CASE in this app uses), never 'priced' despite
    # the majority of its usage being priced.
    ws_prod_row = next(r for r in rows_30 if r["workspace_id"] == bl.WS_PROD)
    assert ws_prod_row["price_basis"] == "unpriced"

    # (d) window exclusions: rows(id, 90) sums more than 30 and rows(id, 7) sums less
    rows_90 = rows(query_id, 90)
    rows_7 = rows(query_id, 7)

    sum_30 = sum(r.get("total_net_dbus", 0) for r in rows_30)
    sum_90 = sum(r.get("total_net_dbus", 0) for r in rows_90)
    sum_7 = sum(r.get("total_net_dbus", 0) for r in rows_7)

    assert sum_90 > sum_30, f"Expected sum_90 > sum_30, got {sum_90} vs {sum_30}"
    assert sum_7 < sum_30, f"Expected sum_7 < sum_30, got {sum_7} vs {sum_30}"

    # sum_30 (all-workspace total_net_dbus) must equal the same window's raw usage_quantity sum,
    # independent of the dollarization above.
    expected_sum_30 = usage_sum(
        "usage_unit = 'DBU' AND usage_date >= DATE '2026-09-21' - INTERVAL 30 DAY AND usage_date < DATE '2026-09-21'"
    )
    assert abs(sum_30 - expected_sum_30) < 1e-6, (
        f"30-day total_net_dbus sum mismatch: expected {expected_sum_30}, got {sum_30}"
    )

    # (e) rows(id, 0) == []
    rows_0 = rows(query_id, 0)
    assert rows_0 == [], f"Expected no rows at window_days=0, got {len(rows_0)} rows"
