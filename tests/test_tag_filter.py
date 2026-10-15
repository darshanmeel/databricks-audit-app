"""tests/test_tag_filter.py -- P4-T-IDX (tasks/P4-T-SPEC.md section 5.8, item 4-6). Exercises
app/core/tags.TagFilter/tag_chain/tag_clause directly against the built findings tables in
tests/db_audit_test.duckdb, pinned to the exact row sets in the spec's section 7.5 table.

This is the mechanism app/core/data.py's read_finding/count_finding/aggregate_finding/
finding_window_counts call into (section 5.5) -- most of this file tests the chain-building and
SQL-building logic on its own, against real finding tables, rather than through that integration;
a few tests near the end go through app/api/service.load_outcome (the missing-index test) or
app/core/config.load_settings (the tag_coverage_floor validation test) instead, where the thing
being proven only exists at that layer.

Run after:
    python tests/fixtures/build_fixtures.py
    python tools/dbt_run.py build --target test
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as app_config  # noqa: E402
from app.core import tags as app_tags  # noqa: E402


@pytest.fixture(scope="module")
def con():
    db_path = os.environ.get("AUDIT_DB", str(ROOT / "tests" / "db_audit_test.duckdb"))
    c = duckdb.connect(db_path, read_only=True)
    try:
        yield c
    finally:
        c.close()


def _table_columns(con, query_id: str) -> list[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'findings' AND table_name = ? ORDER BY ordinal_position",
        [f"f_{query_id}"],
    ).fetchall()
    return [r[0] for r in rows]


def _filtered_ids(
    con, query_id: str, domain: str, id_col: str, id_like: str, tag_key: str, tag_value, window_days: int = 30
):
    col_names = _table_columns(con, query_id)
    chain = app_tags.tag_chain(query_id, col_names, domain)
    tf = app_tags.TagFilter.from_params(tag_key, tag_value)
    clause = app_tags.tag_clause(chain, f'findings."f_{query_id}" t', tf)
    if clause is None:
        return None  # not applicable
    sql = (
        f'SELECT DISTINCT t."{id_col}" FROM findings."f_{query_id}" t '
        f'WHERE t.window_days = ? AND t."{id_col}" LIKE ? AND ({clause.where_sql})'
    )
    rows = con.execute(sql, [window_days, id_like, *clause.params]).fetchall()
    return {r[0] for r in rows}


def _unfiltered_ids(con, query_id: str, id_col: str, id_like: str, window_days: int = 30):
    """The same id set `_filtered_ids` selects from, with NO tag clause at all -- section 5.8
    item 4's own invariant check needs the true unfiltered set, not `any value`'s own "not
    __untagged__" one, to prove `any value` and `__untagged__` partition it exactly."""
    sql = (
        f'SELECT DISTINCT t."{id_col}" FROM findings."f_{query_id}" t '
        f'WHERE t.window_days = ? AND t."{id_col}" LIKE ?'
    )
    rows = con.execute(sql, [window_days, id_like]).fetchall()
    return {r[0] for r in rows}


def _assert_any_and_untagged_partition_unfiltered(any_value, untagged, unfiltered):
    """Section 5.8 item 4: "any value" and "__untagged__" never overlap, and together they are
    exactly the unfiltered set -- a tag chain's COALESCE always resolves to either a real value or
    the untagged sentinel, never both, and never neither."""
    assert any_value.isdisjoint(untagged)
    assert any_value | untagged == unfiltered


# -------------------------------------------------------------------------------------------
# TagFilter.from_params
# -------------------------------------------------------------------------------------------


def test_tag_value_without_tag_key_is_a_value_error():
    with pytest.raises(ValueError):
        app_tags.TagFilter.from_params(None, "alpha")


def test_tag_key_normalising_to_empty_is_a_value_error():
    with pytest.raises(ValueError):
        app_tags.TagFilter.from_params("___", None)


def test_tag_key_is_normalised():
    tf = app_tags.TagFilter.from_params("TG-CC", "alpha")
    assert tf.key == "tgcc"
    assert tf.value == "alpha"


def test_empty_value_maps_to_key_only_sentinel():
    tf = app_tags.TagFilter.from_params("tg_cc", "__empty__")
    assert tf.value == ""


def test_no_params_is_no_filter():
    assert app_tags.TagFilter.from_params(None, None) is None


# -------------------------------------------------------------------------------------------
# tag_chain -- section 5.4's chain table, column-driven
# -------------------------------------------------------------------------------------------


def test_chain_query_costly_statements_is_query_then_compute_then_workspace(con):
    chain = app_tags.tag_chain(
        "query_costly_statements", _table_columns(con, "query_costly_statements"), "performance"
    )
    assert chain.kind == "entity"
    assert chain.chain_words == ["query", "compute", "workspace"]


def test_chain_cost_chargeback_by_tag_is_row_tags(con):
    chain = app_tags.tag_chain(
        "cost_chargeback_by_tag", _table_columns(con, "cost_chargeback_by_tag"), "cost"
    )
    assert chain.kind == "row_tags"


def test_chain_price_list_is_not_applicable(con):
    # A price list has no workspace, compute, job, query or object column at all.
    chain = app_tags.tag_chain(
        "pricing_list_prices_raw", _table_columns(con, "pricing_list_prices_raw"), "cost"
    )
    assert chain.kind == "none"
    assert chain.reason


def test_chain_cost_dollarized_by_sku_day_is_workspace_after_p4_47(con):
    chain = app_tags.tag_chain(
        "cost_dollarized_by_sku_day", _table_columns(con, "cost_dollarized_by_sku_day"), "cost"
    )
    assert chain.kind == "entity"
    assert chain.chain_words == ["workspace"]


def test_chain_access_dead_table_candidates_is_table_then_schema(con):
    chain = app_tags.tag_chain(
        "access_dead_table_candidates", _table_columns(con, "access_dead_table_candidates"), "governance_access"
    )
    assert chain.kind == "entity"
    assert chain.chain_words == ["object", "object"]


# -------------------------------------------------------------------------------------------
# tag_clause -- section 7.5's pinned row sets (key tg_cc, tg entities only, 30d)
# -------------------------------------------------------------------------------------------


def test_query_costly_statements_filter_matches_section_7_5(con):
    alpha = _filtered_ids(con, "query_costly_statements", "performance", "statement_id", "tg_stmt%", "tg_cc", "alpha")
    q_fin = _filtered_ids(con, "query_costly_statements", "performance", "statement_id", "tg_stmt%", "tg_cc", "q_fin")
    untagged = _filtered_ids(
        con, "query_costly_statements", "performance", "statement_id", "tg_stmt%", "tg_cc", app_tags.tag_keys.UNTAGGED
    )
    any_value = _filtered_ids(con, "query_costly_statements", "performance", "statement_id", "tg_stmt%", "tg_cc", None)

    assert alpha == {"tg_stmt_4", "tg_stmt_6", "tg_stmt_9"}
    assert q_fin == {"tg_stmt_1", "tg_stmt_2"}
    assert untagged == set()  # "no tg row" (section 7.5)
    assert any_value == {"tg_stmt_1", "tg_stmt_2", "tg_stmt_4", "tg_stmt_5", "tg_stmt_6", "tg_stmt_7", "tg_stmt_9"}
    # rule (section 5.8 item 4): any-value and untagged never overlap, and together they are
    # exactly the unfiltered set -- mixed never allocates, so every row is one or the other.
    unfiltered = _unfiltered_ids(con, "query_costly_statements", "statement_id", "tg_stmt%")
    _assert_any_and_untagged_partition_unfiltered(any_value, untagged, unfiltered)


def test_lakeflow_job_reliability_filter_matches_section_7_5(con):
    etl = _filtered_ids(con, "lakeflow_job_reliability", "jobs_pipelines", "job_id", "tg_job%", "tg_cc", "etl")
    shared = _filtered_ids(con, "lakeflow_job_reliability", "jobs_pipelines", "job_id", "tg_job%", "tg_cc", "shared")
    untagged = _filtered_ids(
        con, "lakeflow_job_reliability", "jobs_pipelines", "job_id", "tg_job%", "tg_cc", app_tags.tag_keys.UNTAGGED
    )
    any_value = _filtered_ids(con, "lakeflow_job_reliability", "jobs_pipelines", "job_id", "tg_job%", "tg_cc", None)

    assert etl == {"tg_job_a1"}
    assert shared == {"tg_job_a2"}
    assert untagged == {"tg_job_m1"}
    assert any_value == {"tg_job_a1", "tg_job_a2"}
    unfiltered = _unfiltered_ids(con, "lakeflow_job_reliability", "job_id", "tg_job%")
    _assert_any_and_untagged_partition_unfiltered(any_value, untagged, unfiltered)


def test_compute_warehouse_config_posture_filter_matches_section_7_5(con):
    # This finding is a point-in-time config snapshot, window_days = 0 only (section 7.5's own
    # "window 0" note).
    alpha = _filtered_ids(
        con, "compute_warehouse_config_posture", "compute", "warehouse_id", "tg_wh%", "tg_cc", "alpha",
        window_days=0,
    )
    untagged = _filtered_ids(
        con, "compute_warehouse_config_posture", "compute", "warehouse_id", "tg_wh%", "tg_cc",
        app_tags.tag_keys.UNTAGGED, window_days=0,
    )
    any_value = _filtered_ids(
        con, "compute_warehouse_config_posture", "compute", "warehouse_id", "tg_wh%", "tg_cc", None,
        window_days=0,
    )
    assert alpha == {"tg_wh_a1", "tg_wh_a2"}
    assert untagged == {"tg_wh_s1"}
    assert any_value == {"tg_wh_a1", "tg_wh_a2", "tg_wh_a3"}
    unfiltered = _unfiltered_ids(
        con, "compute_warehouse_config_posture", "warehouse_id", "tg_wh%", window_days=0
    )
    _assert_any_and_untagged_partition_unfiltered(any_value, untagged, unfiltered)


def test_cost_by_compute_resource_filter_matches_section_7_5(con):
    # This finding has no single id column identifying every row uniquely across cluster/
    # warehouse/pool grains -- compare the resource id actually carrying the row instead.
    col_names = _table_columns(con, "cost_by_compute_resource")
    chain = app_tags.tag_chain("cost_by_compute_resource", col_names, "cost")

    def ids_for(tag_value):
        tf = app_tags.TagFilter.from_params("tg_cc", tag_value)
        clause = app_tags.tag_clause(chain, 'findings."f_cost_by_compute_resource" t', tf)
        sql = (
            'SELECT DISTINCT COALESCE(t.cluster_id, t.warehouse_id, t.instance_pool_id) '
            'FROM findings."f_cost_by_compute_resource" t '
            "WHERE t.window_days = 30 AND t.workspace_id LIKE 'tg_%' AND (" + clause.where_sql + ")"
        )
        return {r[0] for r in con.execute(sql, clause.params).fetchall()}

    alpha = ids_for("alpha")
    etl = ids_for("etl")
    untagged = ids_for(app_tags.tag_keys.UNTAGGED)
    any_value = ids_for(None)

    assert alpha == {"tg_wh_a1", "tg_wh_a2", "tg_cl_a1"}
    assert etl == {"tg_cl_a2"}
    assert untagged == {"tg_cl_m3", "tg_wh_s1"}
    # "every tg row except tg_cl_m3 and tg_wh_s1" (section 7.5) -- the seven tg clusters and four
    # tg warehouses, minus the two untagged resources above.
    unfiltered = (
        {f"tg_cl_{n}" for n in ("a1", "a2", "a3", "m1", "m2", "m3", "s1")}
        | {f"tg_wh_{n}" for n in ("a1", "a2", "a3", "s1")}
    )
    assert any_value == unfiltered - untagged
    _assert_any_and_untagged_partition_unfiltered(any_value, untagged, unfiltered)


def test_access_dead_table_candidates_filter_matches_section_7_5(con):
    # Section 7.5: neither of the two tagged tables (tg_tbl1 -> data_fin, tg_tbl2 -> data_sch via
    # the schema fallback) carries any of alpha/q_fin/etl/shared/untagged -- only "any value"
    # matches (both, since each has SOME allocating tg_cc value, just not one of the four named
    # ones above).
    col_names = _table_columns(con, "access_dead_table_candidates")
    chain = app_tags.tag_chain("access_dead_table_candidates", col_names, "governance_access")
    assert chain.kind == "entity"
    assert chain.chain_words == ["object", "object"]

    def ids_for(tag_value):
        tf = app_tags.TagFilter.from_params("tg_cc", tag_value)
        clause = app_tags.tag_clause(chain, 'findings."f_access_dead_table_candidates" t', tf)
        sql = (
            "SELECT DISTINCT lower(t.table_catalog) || '.' || lower(t.table_schema) || '.' || "
            "lower(t.table_name) FROM findings.\"f_access_dead_table_candidates\" t "
            "WHERE t.window_days = 30 AND lower(t.table_catalog) LIKE 'tg_cat%' AND ("
            + clause.where_sql + ")"
        )
        return {r[0] for r in con.execute(sql, clause.params).fetchall()}

    for tag_value in ("alpha", "q_fin", "etl", "shared", app_tags.tag_keys.UNTAGGED):
        assert ids_for(tag_value) == set()
    assert ids_for(None) == {"tg_cat.tg_sch.tg_tbl1", "tg_cat.tg_sch.tg_tbl2"}


def test_cost_chargeback_by_tag_filter_matches_section_7_5(con):
    col_names = _table_columns(con, "cost_chargeback_by_tag")
    chain = app_tags.tag_chain("cost_chargeback_by_tag", col_names, "cost")
    assert chain.kind == "row_tags"

    def values_for(tag_value):
        tf = app_tags.TagFilter.from_params("tg_cc", tag_value)
        clause = app_tags.tag_clause(chain, 'findings."f_cost_chargeback_by_tag" t', tf)
        sql = (
            'SELECT DISTINCT t.workspace_id, t.tag_key, t.tag_value '
            'FROM findings."f_cost_chargeback_by_tag" t '
            "WHERE t.workspace_id LIKE 'tg_%' AND (" + clause.where_sql + ")"
        )
        return con.execute(sql, clause.params).fetchall()

    assert values_for("alpha") == [("tg_ws_a", "tg_cc", "alpha")]
    assert values_for("etl") == [("tg_ws_a", "tg_cc", "etl")]
    assert values_for("shared") == [("tg_ws_a", "tg_cc", "shared")]

    # __untagged__ is not applicable for a row-tags chain (section 5.4/7.5): this table lists
    # tagged billing rows only, so tag_clause returns None here -- never a `1 = 0` clause that
    # would silently empty the table in a way indistinguishable from a real "no rows match".
    untagged_tf = app_tags.TagFilter.from_params("tg_cc", app_tags.tag_keys.UNTAGGED)
    assert app_tags.tag_clause(chain, 'findings."f_cost_chargeback_by_tag" t', untagged_tf) is None

    any_rows = values_for(None)
    assert ("tg_ws_a", "TG-CC", "beta") in any_rows  # a differently-spelled key still matches
    assert len(any_rows) == 8


def test_price_list_is_not_applicable_and_unfiltered(con):
    # Section 5.8 item 5: no clause, so rows_total under a tag filter equals the unfiltered one.
    col_names = _table_columns(con, "pricing_list_prices_raw")
    chain = app_tags.tag_chain("pricing_list_prices_raw", col_names, "cost")
    tf = app_tags.TagFilter.from_params("tg_cc", "alpha")
    clause = app_tags.tag_clause(chain, 'findings."f_pricing_list_prices_raw" t', tf)
    assert clause is None
    unfiltered = con.execute('SELECT count(*) FROM findings."f_pricing_list_prices_raw"').fetchone()[0]
    assert unfiltered > 0


# -------------------------------------------------------------------------------------------
# tag_match_value/level/source (the SELECT side)
# -------------------------------------------------------------------------------------------


def test_tag_match_columns_report_level_and_source(con):
    col_names = _table_columns(con, "lakeflow_job_reliability")
    chain = app_tags.tag_chain("lakeflow_job_reliability", col_names, "jobs_pipelines")
    tf = app_tags.TagFilter.from_params("tg_cc", "shared")
    clause = app_tags.tag_clause(chain, 'findings."f_lakeflow_job_reliability" t', tf)
    sql = (
        f"SELECT t.job_id, {clause.select_sql} "
        'FROM findings."f_lakeflow_job_reliability" t '
        "WHERE t.window_days = 30 AND t.job_id = 'tg_job_a2' AND (" + clause.where_sql + ")"
    )
    rows = con.execute(sql, clause.select_params + clause.params).fetchall()
    assert rows == [("tg_job_a2", "shared", "job", "billing:budget_policy")]


# -------------------------------------------------------------------------------------------
# workspace_scoped -- a finding table with no workspace_id column at all must never crash
# (review round: ChainTerm.workspace_scoped used to default to True for every non-UC term).
# -------------------------------------------------------------------------------------------


def test_tag_filter_on_table_without_workspace_id_does_not_crash(con):
    # f_node_timeline_utilization carries no workspace_id column whatsoever (P4-FIXES28 gave
    # sql_warehouse_events_activity, this test's original example, a workspace_id column). Before
    # this fix, every non-UC ChainTerm defaulted workspace_scoped=True regardless of whether the
    # table could supply one, so _entity_subquery/_source_subquery always spliced in
    # `AND e."workspace_id" = t."workspace_id"` -- DuckDB then raised a binder error ('Table "t"
    # does not have a column named "workspace_id"') the moment any tag filter touched this table.
    # Section 5.3 writes the workspace match as OPTIONAL for exactly this reason.
    col_names = _table_columns(con, "node_timeline_utilization")
    assert "workspace_id" not in col_names
    chain = app_tags.tag_chain("node_timeline_utilization", col_names, "compute")
    assert chain.kind == "entity"
    assert all(not t.workspace_scoped for t in chain.terms)
    tf = app_tags.TagFilter.from_params("tg_cc", "alpha")
    clause = app_tags.tag_clause(chain, 'findings."f_node_timeline_utilization" t', tf)
    assert clause is not None
    sql = (
        'SELECT count(*) FROM findings."f_node_timeline_utilization" t '
        "WHERE t.window_days = 30 AND (" + clause.where_sql + ")"
    )
    # The assertion is simply that this executes at all -- it used to raise.
    con.execute(sql, clause.params).fetchone()


# -------------------------------------------------------------------------------------------
# TagFilterSet -- several tag names: OR within one name's values, AND across names. Additive to
# TagFilter above; a bare TagFilter still means exactly what it did (test_tag_key_is_normalised
# etc., unchanged above).
# -------------------------------------------------------------------------------------------


def test_filter_set_tag_value_without_tag_key_is_a_value_error():
    with pytest.raises(ValueError):
        app_tags.TagFilterSet.from_params(None, "alpha")


def test_filter_set_bad_tag_entry_normalising_to_empty_is_a_value_error():
    with pytest.raises(ValueError):
        app_tags.TagFilterSet.from_params(None, None, ["___:x"])


def test_filter_set_no_params_is_no_filter():
    assert app_tags.TagFilterSet.from_params(None, None) is None


def test_filter_set_single_pair_matches_legacy_TagFilter(con):
    # "today's single key:value keeps working": TagFilterSet.single wraps a legacy TagFilter into
    # the exact one-group shape from_params(tag_key, tag_value) alone also produces.
    legacy = app_tags.TagFilter.from_params("tg_cc", "alpha")
    fs_legacy = app_tags.TagFilterSet.single(legacy)
    fs_params = app_tags.TagFilterSet.from_params("tg_cc", "alpha")
    assert fs_legacy == fs_params == app_tags.TagFilterSet(
        groups=(app_tags.TagValueGroup(key="tgcc", values=("alpha",)),)
    )


def test_filter_set_bare_tag_key_is_all_same_as_no_value():
    # "<key>" alone (no ':') means All -- the same empty-values group a bare tag_key with no
    # tag_value produces.
    fs = app_tags.TagFilterSet.from_params(None, None, ["tg_cc"])
    assert fs.groups == (app_tags.TagValueGroup(key="tgcc", values=()),)


def test_filter_set_all_swallows_specific_values_on_the_same_key():
    fs = app_tags.TagFilterSet.from_params(None, None, ["tg_cc:alpha", "tg_cc"])
    assert fs.groups == (app_tags.TagValueGroup(key="tgcc", values=()),)


def test_filter_set_or_within_a_name_matches_the_union(con):
    # cost_by_compute_resource's own pinned section 7.5 sets: tg_cc=alpha is {tg_wh_a1, tg_wh_a2,
    # tg_cl_a1}, tg_cc=etl is {tg_cl_a2} -- OR'ing both values must match their union.
    col_names = _table_columns(con, "cost_by_compute_resource")
    chain = app_tags.tag_chain("cost_by_compute_resource", col_names, "cost")
    fs = app_tags.TagFilterSet.from_params(None, None, ["tg_cc:alpha", "tg_cc:etl"])
    clause = app_tags.tag_set_clause(chain, 'findings."f_cost_by_compute_resource" t', fs)
    sql = (
        'SELECT DISTINCT COALESCE(t.cluster_id, t.warehouse_id, t.instance_pool_id) '
        'FROM findings."f_cost_by_compute_resource" t '
        "WHERE t.window_days = 30 AND t.workspace_id LIKE 'tg_%' AND (" + clause.where_sql + ")"
    )
    ids = {r[0] for r in con.execute(sql, clause.params).fetchall()}
    assert ids == {"tg_wh_a1", "tg_wh_a2", "tg_cl_a1", "tg_cl_a2"}


def test_filter_set_and_across_names_narrows_to_the_intersection(con):
    # tg_wh_a1 alone carries BOTH tg_cc=alpha and tg_env=prod on its own latest tags (tg_wh_a2 has
    # no tags at all) -- ANDing the two names must narrow section 7.5's own {tg_wh_a1, tg_wh_a2}
    # (tg_cc=alpha alone) down to {tg_wh_a1}.
    col_names = _table_columns(con, "compute_warehouse_config_posture")
    chain = app_tags.tag_chain("compute_warehouse_config_posture", col_names, "compute")
    fs = app_tags.TagFilterSet.from_params(None, None, ["tg_cc:alpha", "tg_env:prod"])
    clause = app_tags.tag_set_clause(chain, 'findings."f_compute_warehouse_config_posture" t', fs)
    sql = (
        'SELECT DISTINCT t.warehouse_id FROM findings."f_compute_warehouse_config_posture" t '
        "WHERE t.window_days = 0 AND t.warehouse_id LIKE 'tg_wh%' AND (" + clause.where_sql + ")"
    )
    assert {r[0] for r in con.execute(sql, clause.params).fetchall()} == {"tg_wh_a1"}

    # A name this chain cannot satisfy at all (a value only "None" could match) -- AND'ing it in
    # must read not-applicable for the WHOLE set, never silently drop that one name.
    untagged_fs = app_tags.TagFilterSet.from_params(None, None, ["tg_cc:alpha", "tg_env:__untagged__"])
    chain_row_tags = app_tags.tag_chain("cost_chargeback_by_tag", ["tag_key", "tag_value"], "cost")
    assert app_tags.tag_set_clause(
        chain_row_tags, 'findings."f_cost_chargeback_by_tag" t',
        app_tags.TagFilterSet.from_params(None, None, ["tg_cc:alpha", "tg_cc:__untagged__"]),
    ) is None  # __untagged__ cannot be shown on a row-tags chain, so the OR'd group is not applicable
    assert app_tags.tag_set_clause(
        chain_row_tags, 'findings."f_cost_chargeback_by_tag" t', untagged_fs,
    ) is None  # tg_env:__untagged__ alone cannot be represented on a row-tags chain


def test_filter_set_all_matches_any_value_across_every_name(con):
    # "All" (bare "tg_cc") must match exactly the same set as the legacy any-value filter
    # (tag_value=None) -- section 7.5's own {tg_wh_a1..a3, tg_cl_a1..a3, tg_cl_m1..m2, tg_cl_s1}.
    col_names = _table_columns(con, "cost_by_compute_resource")
    chain = app_tags.tag_chain("cost_by_compute_resource", col_names, "cost")

    def ids_for(fs):
        clause = app_tags.tag_set_clause(chain, 'findings."f_cost_by_compute_resource" t', fs)
        sql = (
            'SELECT DISTINCT COALESCE(t.cluster_id, t.warehouse_id, t.instance_pool_id) '
            'FROM findings."f_cost_by_compute_resource" t '
            "WHERE t.window_days = 30 AND t.workspace_id LIKE 'tg_%' AND (" + clause.where_sql + ")"
        )
        return {r[0] for r in con.execute(sql, clause.params).fetchall()}

    all_ids = ids_for(app_tags.TagFilterSet.from_params(None, None, ["tg_cc"]))
    legacy_ids = ids_for(app_tags.TagFilterSet.single(app_tags.TagFilter.from_params("tg_cc", None)))
    assert all_ids == legacy_ids


# -------------------------------------------------------------------------------------------
# Missing index -- section 5.8 item 6: a filtered read with tags.tag_entity absent returns
# not_assessed / tag_models_not_built, never a silently-unfiltered read.
# -------------------------------------------------------------------------------------------


def test_filtered_read_without_tag_entity_is_not_assessed(tmp_path, monkeypatch):
    from app.api import service as app_service  # local: avoid importing app.api at module scope

    src = os.environ.get("AUDIT_DB", str(ROOT / "tests" / "db_audit_test.duckdb"))
    db_copy = tmp_path / "no_tag_entity.duckdb"
    shutil.copy(src, db_copy)
    c = duckdb.connect(str(db_copy))
    try:
        c.execute("DROP TABLE tags.tag_entity")
    finally:
        c.close()
    monkeypatch.setenv("AUDIT_DB", str(db_copy))

    tf = app_tags.TagFilter.from_params("tg_cc", "alpha")
    outcome = app_service.load_outcome("query_costly_statements", 30, tag=tf)
    assert outcome.outcome == app_service.OUTCOME_NOT_ASSESSED
    assert outcome.status_info["not_built_reason"] == app_service._TAG_MODELS_NOT_BUILT_REASON


# -------------------------------------------------------------------------------------------
# Settings validation -- section 5.8 item 8: tag_coverage_floor outside [0, 1] is rejected.
# -------------------------------------------------------------------------------------------


def test_tag_coverage_floor_above_one_is_rejected(tmp_path, monkeypatch):
    settings = tmp_path / "settings.yml"
    settings.write_text("tag_coverage_floor: 1.5\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(app_config.SettingsError) as exc_info:
        app_config.load_settings()
    assert exc_info.value.key == "tag_coverage_floor"


def test_tag_coverage_floor_below_zero_is_rejected(tmp_path, monkeypatch):
    settings = tmp_path / "settings.yml"
    settings.write_text("tag_coverage_floor: -0.1\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(app_config.SettingsError) as exc_info:
        app_config.load_settings()
    assert exc_info.value.key == "tag_coverage_floor"
