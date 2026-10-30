"""tests/test_findings/test_lakeflow_daily_state.py.

Proves findings.f_lakeflow_daily_state against perf_daily_by_resource's job and pipeline rows (the
same end-row rules): runs and failures add up, the grain holds, and day_state follows its rules.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

QID = "lakeflow_daily_state"


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_grain_uniqueness(window_days):
    cols = yaml.safe_load((ROOT / "config" / "grains" / f"{QID}.yml").read_text(encoding="ascii"))[QID]
    out = dbutil.rows(QID, window_days)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


@pytest.mark.parametrize("window_days", [7, 30, 90])
@pytest.mark.parametrize("rtype", ["job", "pipeline"])
def test_runs_and_failures_match_perf_daily(window_days, rtype):
    mine = [r for r in dbutil.rows(QID, window_days) if r["resource_type"] == rtype]
    perf = [r for r in dbutil.rows("perf_daily_by_resource", window_days) if r["resource_type"] == rtype]
    assert sum(r["runs"] for r in mine) == sum(r["runs"] for r in perf)
    assert sum(r["failed"] for r in mine) == sum(r["failed"] for r in perf)


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_day_state_follows_its_rules(window_days):
    out = dbutil.rows(QID, window_days)
    assert {r["day_state"] for r in out} <= {"ok", "failed", "slow", "skipped"}
    for r in out:
        assert r["runs"] >= 1
        if r["failed"] > 0:
            assert r["day_state"] == "failed"
        elif r["day_state"] == "slow":
            assert r["avg_run_s"] >= 1.5 * r["median_run_s"] - 0.1
            assert r["avg_run_s"] - r["median_run_s"] >= 60 - 0.1
        elif r["day_state"] == "skipped":
            assert r["skipped"] == r["runs"]
        assert r["timed_runs"] + r["skipped"] == r["runs"]
        if r["timed_runs"]:
            assert abs(r["avg_run_s"] * r["timed_runs"] - r["total_run_s"]) <= 0.1 * r["timed_runs"] + 0.1
            assert r["max_run_s"] >= r["avg_run_s"] - 0.1


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_trouble_rank_puts_most_failed_days_first(window_days):
    out = dbutil.rows(QID, window_days)
    per = {}
    for r in out:
        key = (r["resource_type"], r["resource_key"])
        per.setdefault(key, []).append(r)
    ranks = {}
    for key, rows in per.items():
        assert len({r["trouble_rank"] for r in rows}) == 1, key
        assert rows[0]["failed_days"] == sum(1 for r in rows if r["day_state"] == "failed")
        assert rows[0]["slow_days"] == sum(1 for r in rows if r["day_state"] == "slow")
        ranks[key] = (rows[0]["trouble_rank"], rows[0]["failed_days"], rows[0]["slow_days"])
    assert len({v[0] for v in ranks.values()}) == len(ranks), "one rank per resource"
    ordered = sorted(ranks.values())
    assert [(-f, -s) for _, f, s in ordered] == sorted((-f, -s) for _, f, s in ordered)
