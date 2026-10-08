"""tests/test_workspace_attributes.py -- T-63 (DEC-60): dims.dim_workspace's new workspace
TAG-ATTRIBUTE columns (cost_center/team/business_unit/domain -- config/tag_aliases.yml's
canonical keys), built by the generalised dbt/models/dims/int_workspace_tag_hints.sql.

This is a WORKSPACE-attribute filter, not the row-level custom_tags filter (DEC-60 rule 1) -- the
store's existing has_tag/tag_key/tag_value branch is untouched and out of scope here.

Reads the already dbt-built tests/db_audit_test.duckdb through app/core/data.read_dim_workspace()
-- the exact function GET /api/workspaces calls -- via tests/conftest.py's autouse session
fixture (AUDIT_DB / AUDIT_CONFIG_DIR already point at the test db and a live copy of config/, no
setup needed here). tests/fixtures/billing.py's own "SEC L (T-63)" block (bl_ta_cc_* rows) is
this file's fixture; read-only here, never modified.

Run after:
    python tests/fixtures/build_fixtures.py
    python tools/dbt_run.py build --target test
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))

from app.core import config as app_config  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
import billing as bl  # noqa: E402

PARQUET_DIR = TESTS_DIR / "fixtures" / "parquet"

# DEC-15: drilldown.py's own workspaces -- billing.py never writes billing.usage rows for either,
# so they carry zero billed DBUs at all -- the "no usage rows at all" third state (rule 3).
WS_NO_USAGE = "9001"


def _raw_usage_scalar(select_expr: str, where: str):
    """Independently-derived scalar straight from the raw fixture parquet (tests/dbutil.py's own
    convention: usage_sum()/_raw_scalar(), scoped by record_id -- collision-free, since
    workspace_id is shared across every builder per DEC-15) -- never a hard-coded total."""
    pattern = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT {select_expr} FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        result = con.execute(sql).fetchone()
        return result[0] if result else None
    finally:
        con.close()


@pytest.fixture(scope="module")
def ws_df():
    return app_core_data.read_dim_workspace().set_index("workspace_id")


def _row(df, workspace_id):
    return df.loc[str(workspace_id)]


# -------------------------------------------------------------------------------------------
# DEC-60 rule 4/5: config/tag_aliases.yml carries `domain` now; dim_workspace exposes one
# <key>/<key>_share/<key>_reason column set per canonical key, the same shape as env/env_source/
# env_reason.
# -------------------------------------------------------------------------------------------


def test_tag_aliases_yml_has_domain():
    assert app_config.load_top_tags()["domain"] == "Domain"


def test_top_tags_reject_a_reserved_or_badly_spelt_key():
    for bad in ({"env": "Env"}, {"Cost Center": None}):
        with pytest.raises(app_config.ConfigError):
            app_config._validate_tag_aliases({"top_tags": bad})
    assert app_config._validate_tag_aliases({"top_tags": {"bu": None}}) == {"top_tags": {"bu": "bu"}}


def test_dim_workspace_has_one_column_set_per_canonical_key(ws_df):
    canonical_keys = app_config.load_top_tags().keys()
    assert set(canonical_keys) == {"team", "cost_center", "business_unit", "domain"}
    for key in canonical_keys:
        assert key in ws_df.columns
        assert f"{key}_share" in ws_df.columns
        assert f"{key}_reason" in ws_df.columns
        # Every workspace gets a definite value -- never a raw NULL on the value column itself
        # (NULL is reserved for share, which is meaningless for 'not_tagged'/'no_usage').
        assert ws_df[key].notna().all()
        assert ws_df[f"{key}_reason"].notna().all()


# -------------------------------------------------------------------------------------------
# DEC-60 rule 2 -- THE rule this task exists to prove: rank by spend, not rows.
# bl_ta_cc_eng (1 row, enormous DBUs) beats bl_ta_cc_sandbox_r1/r2/r3 (3 rows, tiny DBUs) on
# WS_PROD's cost_center, even though sandbox wins on COUNT(*).
# -------------------------------------------------------------------------------------------


def test_fixture_actually_disagrees_on_row_count_vs_dbus():
    """Guard against a vacuous proof: the two rankings must genuinely disagree, or the test below
    would pass even with the old COUNT(*)-based model."""
    eng_dbus = _raw_usage_scalar("SUM(usage_quantity)", "record_id = 'bl_ta_cc_eng'")
    eng_rows = _raw_usage_scalar("COUNT(*)", "record_id = 'bl_ta_cc_eng'")
    sandbox_dbus = _raw_usage_scalar("SUM(usage_quantity)", "record_id LIKE 'bl_ta_cc_sandbox_r%'")
    sandbox_rows = _raw_usage_scalar("COUNT(*)", "record_id LIKE 'bl_ta_cc_sandbox_r%'")

    assert sandbox_rows > eng_rows  # row-count winner: sandbox (3 rows > 1 row)
    assert eng_dbus > sandbox_dbus  # DBU winner: engineering (by many orders of magnitude)


def test_rank_by_spend_not_row_count(ws_df):
    row = _row(ws_df, bl.WS_PROD)
    assert row["cost_center"] == "engineering"  # the model followed the DBU winner...
    assert row["cost_center"] != "sandbox"       # ...never the row-count winner


# -------------------------------------------------------------------------------------------
# DEC-60 rule 3 -- a plurality is not a label: value + share + reason, floor from
# config/settings.yml (never hard-coded in SQL), and the three-way not_tagged / mixed / no_usage
# distinction.
# -------------------------------------------------------------------------------------------


def test_clean_tagged_workspace_reads_dominant_value_above_floor(ws_df):
    floor = app_config.load_settings()["tag_share_floor"]
    row = _row(ws_df, bl.WS_PROD)
    assert row["cost_center"] == "engineering"
    assert not pd.isna(row["cost_center_share"])
    assert row["cost_center_share"] >= floor
    assert row["cost_center_share"] > 0.9  # ~95%+ dominance, per the fixture's own design (SEC L)
    assert "engineering" in row["cost_center_reason"]


def test_ambiguous_workspace_reads_not_tagged_below_the_coverage_floor(ws_df):
    # 2222 splits cost_center 55/45 (share 0.55, below the 0.6 share floor) on well under the 0.5
    # coverage floor too (C2/C25, the same gate tags.tag_workspace applies) -- coverage is checked
    # first, so this reads 'not_tagged' outright, never a confident 'mixed' off that little data.
    coverage_floor = app_config.load_settings()["tag_coverage_floor"]
    row = _row(ws_df, bl.WS_DEV)
    assert row["cost_center"] == "not_tagged"
    assert not pd.isna(row["cost_center_share"])
    assert row["cost_center_share"] == pytest.approx(0.55, abs=0.01)
    assert row["cost_center_coverage"] < coverage_floor
    assert "%" in row["cost_center_reason"]


def test_untagged_workspace_reads_not_tagged(ws_df):
    row = _row(ws_df, bl.WS_UAT)
    assert row["cost_center"] == "not_tagged"
    assert pd.isna(row["cost_center_share"])  # no matched value at all -- share is meaningless


def test_no_billed_dbu_workspace_reads_no_usage_distinct_from_not_tagged_and_mixed(ws_df):
    assert WS_NO_USAGE in ws_df.index
    row = _row(ws_df, WS_NO_USAGE)
    assert row["cost_center"] == "no_usage"
    assert row["cost_center"] not in ("not_tagged", "mixed")
    assert pd.isna(row["cost_center_share"])


def test_share_floor_is_configurable_not_hard_coded():
    settings = app_config.load_settings()
    assert "tag_share_floor" in settings
    assert 0 <= settings["tag_share_floor"] <= 1


# -------------------------------------------------------------------------------------------
# DEC-60 rule 5 -- "no account is known to carry these tags" must build and pass cleanly: a
# canonical key nobody has ever tagged (business_unit, domain -- neither appears anywhere in the
# fixtures) never crashes the build and never reports a real value.
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["business_unit", "domain"])
def test_never_tagged_canonical_key_builds_cleanly(ws_df, key):
    assert set(ws_df[key].unique()) <= {"not_tagged", "no_usage"}
    assert ws_df[f"{key}_share"].isna().all()

# -------------------------------------------------------------------------------------------
# The share DENOMINATOR. These tests exist because the suite once passed with either denominator,
# which meant nothing pinned the semantics and a silent regression was free. `share` must be
# dominance AMONG THE TAGGED DBUs; how much of the workspace is tagged at all is `coverage`.
# -------------------------------------------------------------------------------------------


def test_share_is_dominance_among_tagged_not_diluted_by_untagged_spend(ws_df):
    # Workspace 1111 carries exactly one team value, on a tiny fraction of its billed DBUs.
    # Dominance (share) is total, computed among the TAGGED DBUs only -- diluting it by untagged
    # spend used to label this 'mixed' (ambiguous), which was simply false. But near-zero coverage
    # is its own gate (C2/C25, tags.tag_workspace's own coverage_floor): the workspace's own value
    # still reads 'not_tagged' -- confident dominance on a sliver of coverage is not real evidence.
    row = _row(ws_df, bl.WS_PROD)
    assert row["team"] == "not_tagged"
    assert row["team_share"] == pytest.approx(1.0)
    assert row["team_coverage"] < 0.01
    assert row["team_share"] > row["team_coverage"]
    assert row["team_coverage"] < app_config.load_settings()["tag_coverage_floor"]


def test_share_floor_is_the_dominance_boundary_independent_of_coverage(ws_df):
    # 2222 splits cost_center 55/45, so dominance (share, among the TAGGED DBUs only) is 0.55 --
    # under the 0.6 share floor. That is the boundary this fixture was built to exercise; with the
    # old denominator it read 'mixed' at 4.3% and never tested the floor at all. This workspace's
    # coverage is ALSO under the 0.5 coverage floor (C2/C25), which gates the final value to
    # 'not_tagged' before share is even consulted -- see the not_tagged test above -- but `share`
    # itself is still the raw dominance figure, unaffected by that separate gate.
    row = _row(ws_df, bl.WS_DEV)
    assert row["cost_center_share"] == pytest.approx(0.55, abs=0.01)
    assert row["cost_center_share"] < app_config.load_settings()["tag_share_floor"]
    # Mostly untagged, and visible as its own fact.
    assert row["cost_center_coverage"] < app_config.load_settings()["tag_coverage_floor"]


def test_coverage_is_absent_only_when_nothing_at_all_carries_the_key(ws_df):
    # 'not_tagged' now covers two different facts (C2/C25): literally zero tagged rows (coverage
    # undefined -- NaN) and some tagged rows that never reach the coverage floor (coverage a real,
    # defined number, just too small to trust). A real value (cost_center="engineering" etc.)
    # always has a defined coverage at or above the floor.
    floor = app_config.load_settings()["tag_coverage_floor"]
    for key in ("cost_center", "team", "business_unit", "domain"):
        no_usage = ws_df[key] == "no_usage"
        assert ws_df.loc[no_usage, f"{key}_coverage"].isna().all()
        has_value = ~ws_df[key].isin(["not_tagged", "no_usage"])
        assert ws_df.loc[has_value, f"{key}_coverage"].notna().all()
        assert (ws_df.loc[has_value, f"{key}_coverage"] >= floor).all()
        not_tagged = ws_df[key] == "not_tagged"
        cov = ws_df.loc[not_tagged, f"{key}_coverage"]
        assert (cov.isna() | (cov < floor)).all()


# -------------------------------------------------------------------------------------------
# C2/C25 -- int_workspace_tag_hints (dims.dim_workspace's own attribute columns) and
# tags.tag_workspace must be the SAME engine: for every workspace and canonical key, one agrees
# with the other or both are null. Compared only for a canonical key with exactly one alias --
# tag_workspace has no alias-consolidation concept of its own (one row per raw normalised key it
# actually saw), so a multi-alias canonical key has no 1:1 counterpart to compare against.
# -------------------------------------------------------------------------------------------


def test_dim_workspace_attribute_matches_tag_workspace_or_both_are_null(ws_df):
    from app.core import tag_keys

    aliases = {key: [] for key in app_config.load_top_tags()}
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        tw_rows = con.execute("SELECT workspace_id, tag_key, tag_value FROM tags.tag_workspace").fetchall()
    finally:
        con.close()
    tag_workspace = {(w, k): v for w, k, v in tw_rows}

    checked = 0
    for canonical_key, alias_list in aliases.items():
        if canonical_key not in ws_df.columns:
            continue
        real_aliases = alias_list or [canonical_key]
        if len(real_aliases) != 1:
            continue
        norm_key = tag_keys.normalize_tag_key(real_aliases[0])
        for workspace_id, row in ws_df.iterrows():
            dim_value = row[canonical_key]
            tw_value = tag_workspace.get((workspace_id, norm_key))
            if dim_value in ("not_tagged", "no_usage"):
                assert tw_value is None or tw_value == "__untagged__", (
                    f"{workspace_id}/{canonical_key}: dim says {dim_value!r}, tag_workspace says {tw_value!r}"
                )
            else:
                expected = "__mixed__" if dim_value == "mixed" else dim_value
                assert tw_value == expected, (
                    f"{workspace_id}/{canonical_key}: dim says {dim_value!r}, tag_workspace says {tw_value!r}"
                )
            checked += 1
    assert checked > 0, "no single-alias canonical key found to compare -- fixture/config changed shape"
