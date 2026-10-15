"""query_slices: per-warehouse query numbers that a tag filter splits by each query's own tag."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dbutil  # noqa: E402
from app.core import data  # noqa: E402
from app.core.tags import TagFilterSet  # noqa: E402

MEASURES = ("runs", "failed", "total_s", "queued_s", "slot_wait_s", "provision_s", "spilled_gb", "read_gb", "result_cache_runs")


def _runs(*entries) -> tuple[int, list]:
    r = data.read_finding("query_slices", 30, tag_filter=TagFilterSet.from_params(None, None, list(entries)), limit=100000)
    return int(r.df["runs"].sum()) if len(r.df) else 0, r.df.to_dict("records")


@pytest.mark.parametrize("window", [7, 30, 90])
def test_filter_off_totals_match_perf_daily_by_resource(window):
    slices = dbutil.rows("query_slices", window)
    perf = [r for r in dbutil.rows("perf_daily_by_resource", window) if r["resource_type"] == "warehouse"]
    for m in MEASURES:
        assert sum(r[m] or 0 for r in slices) == pytest.approx(sum(r[m] or 0 for r in perf), abs=0.5), m


def test_any_value_and_untagged_add_up_to_the_total():
    total = sum(r["runs"] for r in dbutil.rows("query_slices", 30))
    assert _runs("tgcc")[0] + _runs("tgcc:__untagged__")[0] == total


def test_query_tag_beats_the_warehouse_tag():
    _, rows = _runs("tgcc:q_fin")
    assert rows and all(r["warehouse_id"] == "tg_wh_a1" and r["tag_match_level"] == "query" for r in rows)
    # The warehouse's own value (alpha) takes its other queries, never the one tagged q_fin itself.
    _, alpha = _runs("tgcc:alpha")
    on_a1 = [r for r in alpha if r["warehouse_id"] == "tg_wh_a1"]
    assert on_a1 and all("q_fin" not in str(r["query_tags"]) and r["tag_match_level"] == "compute" for r in on_a1)


def test_names_are_anded():
    n_cc, _ = _runs("tgcc:delta")
    n_both, rows = _runs("tgcc:delta", "tgapp:etl")
    n_none, _ = _runs("tgcc:delta", "tgapp:bi")
    assert n_cc >= n_both > 0 and n_none == 0
    assert all("delta" in r["query_tags"] for r in rows)
