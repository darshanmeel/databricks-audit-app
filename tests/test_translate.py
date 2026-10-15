"""tests/test_translate.py -- one test per rule in app/core/translate.RULES, using the exact
forms found in the vendored bodies (PLAN.md 5.4), plus test_tokens_untouched and the
masking / refusal guards.

The module is loaded by file path (importlib), the same pattern as tests/test_ddl.py, so a plain
`pytest tests/test_translate.py -q` works regardless of pytest's rootdir / sys.path behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tr = _load(ROOT / "app" / "core" / "translate.py", "audit_translate_under_test")
translate = tr.translate
RULE_NAMES = [r.name for r in tr.RULES]

# the generated fixture DDL, for the executable DuckDB regression below (same loader; the module
# is a plain dict of CREATE TABLE strings, it opens no database of its own)
ddl_mod = _load(ROOT / "tests" / "fixtures" / "ddl.py", "audit_fixture_ddl_under_test")


def _rule(name: str):
    return next(r for r in tr.RULES if r.name == name)


# ------------------------------------------------------------------------------------------
# the rule table itself
# ------------------------------------------------------------------------------------------


def test_rules_table_is_ordered_and_ends_with_pinning():
    """The as-of pinning pair is applied LAST (PLAN.md 5.4), after every dateadd/INTERVAL
    rewrite has emitted its current_date()/current_timestamp() text."""
    assert RULE_NAMES[-2:] == ["pin_time", "now"]
    # the one statement-shape rule runs first, on the library's own formatting
    assert RULE_NAMES[0] == "row_number_star_to_qualify"
    assert RULE_NAMES.index("lateral_view") < RULE_NAMES.index("explode")
    assert RULE_NAMES.index("interval_literal") < RULE_NAMES.index("interval_expr")
    assert len(RULE_NAMES) == len(set(RULE_NAMES))


@pytest.mark.parametrize("name", RULE_NAMES)
def test_rule_example_from_table(name):
    """Every rule's recorded example (a form lifted from a vendored body) translates to its
    recorded output through the whole table."""
    r = _rule(name)
    assert translate(r.example_in) == r.example_out


# ------------------------------------------------------------------------------------------
# one explicit test per rule, exact body forms
# ------------------------------------------------------------------------------------------


# the exact `entities` CTE of app/queries/vendored/serving_ai/compute_serving_endpoint_usage.sql
# (sentinel form: the generator substitutes sources AFTER translate(), so the FROM is still the
# plain `system.serving.served_entities se` at rule time)
SERVING_ENTITIES_IN = """WITH entities AS (
  -- served_entities is change-history; keep only the latest config row per entity.
  SELECT
    workspace_id,
    endpoint_id,
    served_entity_id
  FROM (
    SELECT
      se.*,
      ROW_NUMBER() OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
        ORDER BY se.change_time DESC
      ) AS _rn
    FROM system.serving.served_entities se
  )
  WHERE _rn = 1
)"""

SERVING_ENTITIES_OUT = """WITH entities AS (
  -- served_entities is change-history; keep only the latest config row per entity.
  SELECT
    workspace_id,
    endpoint_id,
    served_entity_id
  FROM (
    SELECT
      se.*
    FROM system.serving.served_entities se
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
        ORDER BY se.change_time DESC
      ) = 1
  )
)"""


def test_rule_row_number_star_to_qualify():
    """The exact compute_serving_endpoint_usage shape: the star survives, the row-number column
    and the outer WHERE are gone, the multi-line window clause is carried over verbatim, and the
    `-- served_entities is change-history` comment is untouched."""
    out = translate(SERVING_ENTITIES_IN)
    assert out == SERVING_ENTITIES_OUT
    assert "QUALIFY" in out and "_rn" not in out
    assert "-- served_entities is change-history" in out
    # idempotent: the rewritten body no longer matches the shape
    assert translate(out) == out
    # a one-line spelling, and a named derived table filtered through its own alias
    assert translate("FROM (SELECT se.*, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS rn "
                     "FROM t se) WHERE rn = 1") == (
        "FROM (SELECT se.* FROM t se QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1)"
    )
    assert translate("FROM (SELECT se.*, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS rn "
                     "FROM t se) x WHERE x.rn = 1") == (
        "FROM (SELECT se.* FROM t se QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1) x"
    )


def test_rule_row_number_bodies_without_the_outer_filter_are_untouched():
    """The 21 other ROW_NUMBER() bodies. Each is byte-identical out, for its own reason -- the
    bug needs `<alias>.*` AND a window column AND an outer filter on that column."""
    bodies = {
        # lakeflow_jobs_no_timeout et al: already QUALIFY, the window is not aliased at all
        "qualify": ("SELECT workspace_id, job_id, name AS job_name\n"
                    "FROM system.lakeflow.jobs\n"
                    "QUALIFY ROW_NUMBER() OVER (\n"
                    "  PARTITION BY workspace_id, job_id ORDER BY change_time DESC\n"
                    ") = 1"),
        # serving_endpoint_traffic_by_endpoint: same outer WHERE, but columns listed explicitly
        "explicit_columns": ("SELECT workspace_id, endpoint_id\n"
                             "FROM (\n"
                             "  SELECT\n"
                             "    se.workspace_id, se.endpoint_id,\n"
                             "    ROW_NUMBER() OVER (\n"
                             "      PARTITION BY se.workspace_id, se.endpoint_id\n"
                             "      ORDER BY se.change_time DESC\n"
                             "    ) AS _rn\n"
                             "  FROM system.serving.served_entities se\n"
                             ")\n"
                             "WHERE _rn = 1"),
        # classic_clusters_config_current: an unqualified `*` star, and the outer WHERE carries a
        # second conjunct, so the clause cannot be dropped (measured correct on DuckDB 1.5.1)
        "bare_star": ("SELECT cluster_id, cluster_name\n"
                      "FROM (\n"
                      "  SELECT *, ROW_NUMBER() OVER (PARTITION BY cluster_id "
                      "ORDER BY change_time DESC) AS rn\n"
                      "  FROM system.compute.clusters\n"
                      ")\n"
                      "WHERE rn = 1 AND delete_time IS NULL"),
        # instance_pools_idle_capacity: an unqualified star, and a JOIN sits between the derived
        # table and the WHERE
        "bare_star_joined": ("FROM (\n"
                             "  SELECT *, ROW_NUMBER() OVER (PARTITION BY instance_pool_id "
                             "ORDER BY change_time DESC) AS rn\n"
                             "  FROM system.compute.instance_pools\n"
                             ") p\n"
                             "LEFT JOIN cost_rollup cr ON cr.workspace_id = p.workspace_id\n"
                             "WHERE p.rn = 1 AND p.delete_time IS NULL"),
        # query_task_statement_breakdown `ranked`: `sc.*` but more select items, and the rank is
        # read by a later CTE rather than filtered here
        "star_plus_more_columns": ("SELECT sc.*,\n"
                                   "       100.0 * sc.total_ms AS share_raw,\n"
                                   "       ROW_NUMBER() OVER (PARTITION BY sc.workspace_id\n"
                                   "                          ORDER BY sc.total_ms DESC) "
                                   "AS stmt_rank_in_task\n"
                                   "FROM scoped sc"),
        # query_costly_statements: `b.*` plus a ranking column, no derived table and no filter
        "ranking_column": ("SELECT\n"
                           "  b.*,\n"
                           "  ROW_NUMBER() OVER (PARTITION BY b.warehouse_id "
                           "ORDER BY b.execution_duration_ms DESC) AS exec_rank_in_warehouse\n"
                           "FROM base b"),
        # the shape minus only the outer filter: correct on 1.5.1, so deliberately left alone
        "star_no_outer_filter": ("SELECT workspace_id\n"
                                 "FROM (\n"
                                 "  SELECT se.*,\n"
                                 "         ROW_NUMBER() OVER (PARTITION BY se.workspace_id "
                                 "ORDER BY se.change_time DESC) AS _rn\n"
                                 "  FROM system.serving.served_entities se\n"
                                 ")"),
    }
    for name, body in bodies.items():
        assert translate(body) == body, name


def test_rule_row_number_star_to_qualify_raises_rather_than_silently_skipping():
    """Once the rule has recognised `(SELECT a.*, ROW_NUMBER() ... AS rn FROM ...) WHERE rn`, a
    shape it cannot finish is an error: leaving the known-bad SQL in place would ship wrong
    values under right column names, with no error anywhere."""
    shape = ("FROM (SELECT se.*, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS _rn "
             "FROM t se)")
    # the outer WHERE carries more than the row-number test
    with pytest.raises(tr.Untranslatable) as e:
        translate(shape + " WHERE _rn = 1 AND deleted IS NULL")
    assert "more than the row-number test" in str(e.value)
    # the filter is not `= 1`
    with pytest.raises(tr.Untranslatable) as e:
        translate(shape + " WHERE _rn <= 2")
    assert "is not `= 1`" in str(e.value)
    # the row-number column is projected by the outer query too, so it cannot be deleted
    with pytest.raises(tr.Untranslatable) as e:
        translate("SELECT _rn " + shape + " WHERE _rn = 1")
    assert "referenced 3 times" in str(e.value)
    # a qualified outer reference that is not the derived table's alias
    with pytest.raises(tr.Untranslatable) as e:
        translate(shape + " WHERE q._rn = 1")
    assert "derived table's alias" in str(e.value)
    # and the whole table still refuses it through translate()
    with pytest.raises(tr.Untranslatable):
        translate(SERVING_ENTITIES_IN.replace("WHERE _rn = 1", "WHERE _rn = 1 AND x IS NULL"))


def test_row_number_star_to_qualify_returns_correct_rows():
    """The reason the rule exists, executed. Seeds an in-memory DuckDB from the generated
    served_entities DDL with two SCD2 rows for one entity and runs the translated (QUALIFY)
    form: every column comes back under its own name, with the right value."""
    pre = ("SELECT workspace_id, endpoint_id, served_entity_id, served_entity_name\n"
           "FROM (\n"
           "  SELECT\n"
           "    se.*,\n"
           "    ROW_NUMBER() OVER (\n"
           "      PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id\n"
           "      ORDER BY se.change_time DESC\n"
           "    ) AS _rn\n"
           "  FROM serving__served_entities se\n"
           ")\n"
           "WHERE _rn = 1")
    post = translate(pre)                     # what the rule emits, not a hand-written twin
    assert "QUALIFY" in post and "_rn" not in post

    con = duckdb.connect()                    # in-memory only; touches no file on disk
    try:
        con.execute(ddl_mod.DDL["serving__served_entities"])
        con.execute(
            "INSERT INTO serving__served_entities "
            "(served_entity_id, account_id, workspace_id, endpoint_name, endpoint_id, "
            " served_entity_name, entity_type, entity_name, entity_version, change_time) VALUES "
            "('se_1', 'acct', '1111', 'ep_name', 'ep_1', 'name_old', 'MODEL', 'c.s.m', '1', "
            " TIMESTAMP '2026-09-01 00:00:00'), "
            "('se_1', 'acct', '1111', 'ep_name', 'ep_1', 'name_new', 'MODEL', 'c.s.m', '2', "
            " TIMESTAMP '2026-09-10 00:00:00')"
        )
        post_cur = con.execute(post)
        post_names = [d[0] for d in post_cur.description]
        post_rows = post_cur.fetchall()
    finally:
        con.close()

    wanted = ["workspace_id", "endpoint_id", "served_entity_id", "served_entity_name"]
    assert post_names == wanted
    # the latest SCD2 row, every value under its own name.
    assert post_rows == [("1111", "ep_1", "se_1", "name_new")]


def test_rule_dateadd_day_forms():
    # cost_by_job / lakeflow_long_running_runs
    assert translate("WHERE usage_date >= dateadd(day, -__W__, current_date())") == (
        "WHERE usage_date >= ({{ audit_today() }} - (__W__) * INTERVAL 1 DAY)"
    )
    # task_cluster_utilization (LEAST), lakeflow_stale_zombie_jobs (another param), a number
    assert translate("dateadd(DAY, -LEAST(__W__, 90), current_date())") == (
        "({{ audit_today() }} - (LEAST(__W__, 90)) * INTERVAL 1 DAY)"
    )
    assert translate("dateadd(day, -__P__crit_stale_days__, current_date())") == (
        "({{ audit_today() }} - (__P__crit_stale_days__) * INTERVAL 1 DAY)"
    )
    assert translate("dateadd(day, -30, current_date())") == (
        "({{ audit_today() }} - (30) * INTERVAL 1 DAY)"
    )


def test_rule_dateadd_hour_form():
    # query_task_statement_breakdown
    assert translate("a.task_last_seen >= dateadd(hour, -__P__settle_hours__, current_timestamp())") == (
        "a.task_last_seen >= ({{ audit_now() }} - (__P__settle_hours__) * INTERVAL 1 HOUR)"
    )


def test_rule_date_add_with_cast():
    # instance_pools_idle_capacity
    assert translate("u.usage_date >= date_add(current_date(), -CAST(__W__ AS INT))") == (
        "u.usage_date >= ({{ audit_today() }} - (CAST(__W__ AS INT)) * INTERVAL 1 DAY)"
    )
    # lakeflow_job_tasks_no_timeout
    assert translate("date_add(current_date(), -__W__)") == "({{ audit_today() }} - (__W__) * INTERVAL 1 DAY)"


def test_rule_date_sub():
    # lakeflow_jobs_no_timeout
    assert translate("u.usage_date >= date_sub(current_date(), __W__)") == (
        "u.usage_date >= ({{ audit_today() }} - (__W__) * INTERVAL 1 DAY)"
    )


def test_rule_timestampdiff_nested_case():
    # lakeflow_long_running_runs: nested CASE with commas inside the third argument
    src = ("COALESCE(run_s_reported,\n"
           "                  timestampdiff(SECOND, run_start,\n"
           "                                CASE WHEN result_state IS NULL THEN current_timestamp()\n"
           "                                     ELSE last_seen END)) AS run_s")
    out = translate(src)
    assert "date_diff('second', run_start, CASE WHEN result_state IS NULL THEN {{ audit_now() }}" in out
    assert "timestampdiff" not in out
    assert out.endswith("ELSE last_seen END)) AS run_s")


def test_rule_percentile_approx():
    # task_cluster_utilization
    assert translate("percentile_approx(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9) AS worker_cpu_p90_pct") == (
        "quantile_cont(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9) AS worker_cpu_p90_pct"
    )
    # lakeflow_long_running_runs
    assert translate("percentile_approx(run_s, 0.5)     AS run_s_p50") == "quantile_cont(run_s, 0.5)     AS run_s_p50"


def test_rule_percentile_exact():
    # lakeflow_phase_cold_start
    assert translate("WHEN PERCENTILE(setup_duration_seconds, 0.95) >= __P__crit_setup_p95_s__ THEN 'CRITICAL'") == (
        "WHEN quantile_cont(setup_duration_seconds, 0.95) >= __P__crit_setup_p95_s__ THEN 'CRITICAL'"
    )


def test_rule_try_element_at_index_and_struct_access():
    # task_cluster_utilization / query_task_statement_breakdown
    assert translate("MAX(try_element_at(compute_ids, 1)) AS cluster_id") == "MAX((compute_ids[1])) AS cluster_id"
    assert translate("MAX(try_element_at(t.compute, 1).warehouse_id)") == "MAX((t.compute[1]).warehouse_id)"


def test_rule_try_element_at_map_key():
    assert translate("try_element_at(custom_tags, 'team')") == "(custom_tags['team'])"


def test_rule_sha2():
    # query_costly_statements / query_task_statement_breakdown, nested regexp_replace inside
    src = ("sha2(\n"
           "  regexp_replace(\n"
           "    regexp_replace(s.statement_text_devalued,\n"
           "                   '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?'),\n"
           "    '[?]( *, *[?])+', '?'),\n"
           "  256)")
    out = translate(src)
    assert out.startswith("sha256(regexp_replace(regexp_replace(s.statement_text_devalued, ")
    assert out.endswith("'[?]( *, *[?])+', '?', 'g'))")
    assert "256" not in out.split("sha256", 1)[1]
    with pytest.raises(tr.Untranslatable):
        translate("sha2(x, 512)")


def test_rule_regexp_replace_global_and_dollar_backref():
    # lakeflow_long_running_runs: the pattern's own '$' anchor is NOT a backreference
    assert translate("regexp_replace(workspace_url, '/+$', '') AS base_url") == (
        "regexp_replace(workspace_url, '/+$', '', 'g') AS base_url"
    )
    # query_task_statement_breakdown: $1 in the replacement becomes \1
    out = translate("regexp_replace(x, '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?')")
    assert out == "regexp_replace(x, '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '\\1?', 'g')"
    assert "$1" not in out
    # a 4-arg form has no rule
    with pytest.raises(tr.Untranslatable):
        translate("regexp_replace(a, b, c, 2)")


def test_rule_size_and_cardinality():
    # task_cluster_utilization
    assert translate("MAX(CASE WHEN t.compute_ids IS NULL THEN NULL ELSE size(t.compute_ids) END) AS compute_ids_count") == (
        "MAX(CASE WHEN t.compute_ids IS NULL THEN NULL ELSE len(t.compute_ids) END) AS compute_ids_count"
    )
    # cost_usage_policy_coverage / lakeflow_health_rule_coverage
    assert translate("CASE WHEN cardinality(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END") == (
        "CASE WHEN len(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END"
    )
    assert translate("CARDINALITY(health_rules) > 0") == "len(health_rules) > 0"


def test_rule_rlike_forms():
    guid = "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"
    # cost_chargeback_by_identity: struct dot chain, regex braces inside the literal survive
    assert translate(f"WHEN identity_metadata.run_as RLIKE {guid} THEN identity_metadata.run_as") == (
        f"WHEN regexp_matches(identity_metadata.run_as, {guid}) THEN identity_metadata.run_as"
    )
    # access_admin_role_change_events: a call as the left operand
    assert translate(f"WHEN COALESCE(user_identity.email, user_identity.subject_name) RLIKE {guid} THEN 1") == (
        f"WHEN regexp_matches(COALESCE(user_identity.email, user_identity.subject_name), {guid}) THEN 1"
    )
    # access_pii_outside_tables: two RLIKEs joined by OR, inline (?i) flag
    pat = "'(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'"
    assert translate(f"WHERE st.TAG_NAME  RLIKE {pat}\n     OR st.TAG_VALUE RLIKE {pat}") == (
        f"WHERE regexp_matches(st.TAG_NAME, {pat})\n     OR regexp_matches(st.TAG_VALUE, {pat})"
    )


def test_rule_unix_timestamp():
    # compute_warehouse_idle_gaps
    assert translate("ELSE unix_timestamp(next_event_time) - unix_timestamp(event_time)") == (
        "ELSE epoch(next_event_time) - epoch(event_time)"
    )
    # sql_warehouse_events_activity
    assert translate("unix_timestamp(current_timestamp()) - unix_timestamp(MAX(event_time))") == (
        "epoch({{ audit_now() }}) - epoch(MAX(event_time))"
    )
    assert translate("unix_timestamp()") == "epoch({{ audit_now() }})"


def test_rule_datediff():
    # access_dead_table_candidates: argument order flips (end, start) -> ('day', start, end)
    assert translate("datediff(current_date(), DATE(inv.last_altered)) AS days_since_altered") == (
        "date_diff('day', CAST(inv.last_altered AS DATE), {{ audit_today() }}) AS days_since_altered"
    )
    assert translate("datediff(HOUR, a, b)") == "date_diff('hour', a, b)"


def test_rule_date_cast():
    # cost_actual_vs_list_by_sku; DATE literals and date_trunc are untouched
    assert translate("AND (dp.price_end_time IS NULL OR u.usage_date < DATE(dp.price_end_time))") == (
        "AND (dp.price_end_time IS NULL OR u.usage_date < CAST(dp.price_end_time AS DATE))"
    )
    assert translate("x >= DATE '2026-09-01' AND date_trunc('DAY', ts) < y") == (
        "x >= DATE '2026-09-01' AND date_trunc('DAY', ts) < y"
    )


def test_rule_collect_set_and_array_join():
    # access_pii_outside_tables / compute_serving_endpoint_cost_status
    assert translate("array_join(collect_set(TAG_NAME), ', ') AS tag_names") == (
        "array_to_string(list_distinct(list(TAG_NAME)), ', ') AS tag_names"
    )
    assert translate("array_join(collect_set(product), ', ')                    AS products") == (
        "array_to_string(list_distinct(list(product)), ', ')                    AS products"
    )


def test_rule_array_join_alone():
    assert translate("array_join(tags, ';')") == "array_to_string(tags, ';')"


def test_rule_lateral_view_outer_explode():
    # cost_chargeback_by_tag (the one query; flagged for review in PLAN.md 5.4)
    src = ("FROM system.billing.usage\n"
           "     LATERAL VIEW OUTER explode(custom_tags) t AS tag_key, tag_value\n"
           "WHERE usage_date >= dateadd(day, -__W__, current_date())")
    out = translate(src)
    assert ("LEFT JOIN LATERAL (SELECT e.key AS tag_key, e.value AS tag_value "
            "FROM unnest(map_entries(custom_tags)) AS x(e)) t ON TRUE") in out
    assert "LATERAL VIEW" not in out and "explode" not in out.lower().replace("map_entries", "")
    # the non-OUTER and the single-column (array) forms
    assert translate("LATERAL VIEW explode(arr) t AS c") == "JOIN LATERAL (SELECT unnest(arr) AS c) t ON TRUE"


def test_rule_explode_in_select_list():
    # lakeflow_jobs_on_all_purpose
    assert translate("SELECT workspace_id, job_id, run_id, task_key, EXPLODE(compute_ids) AS compute_id") == (
        "SELECT workspace_id, job_id, run_id, task_key, UNNEST(compute_ids) AS compute_id"
    )


def test_rule_interval_literal():
    assert translate("INTERVAL 7 DAYS") == "INTERVAL 7 DAY"
    assert translate("INTERVAL 1 HOURS") == "INTERVAL 1 HOUR"
    assert translate("INTERVAL 1 DAY") == "INTERVAL 1 DAY"


def test_rule_interval_expr_with_sentinels():
    # compute_warehouse_idle_gaps / po_clustering_activity
    assert translate("WHERE event_time >= current_timestamp() - INTERVAL __W__ DAYS") == (
        "WHERE event_time >= {{ audit_now() }} - ((__W__) * INTERVAL 1 DAY)"
    )
    assert translate("AND start_time >= current_date() - INTERVAL __W__ DAYS AND start_time < current_date()") == (
        "AND start_time >= {{ audit_today() }} - ((__W__) * INTERVAL 1 DAY) AND start_time < {{ audit_today() }}"
    )
    assert translate("INTERVAL __P__settle_hours__ HOURS") == "((__P__settle_hours__) * INTERVAL 1 HOUR)"
    assert translate("INTERVAL (x + 1) DAY") == "(((x + 1)) * INTERVAL 1 DAY)"


def test_rule_pin_time_both_spellings():
    assert translate("current_date()") == "{{ audit_today() }}"
    assert translate("current_timestamp()") == "{{ audit_now() }}"
    assert translate("current_date") == "{{ audit_today() }}"
    assert translate("current_timestamp") == "{{ audit_now() }}"
    # lakeflow_jobs_on_all_purpose
    assert translate("AND period_end_time < date_trunc('DAY', current_timestamp())") == (
        "AND period_end_time < date_trunc('DAY', {{ audit_now() }})"
    )
    # pin_time() alone is the Databricks-branch substitution: nothing else is touched
    assert tr.pin_time("dateadd(day, -__W__, current_date()) AND unix_timestamp(x)") == (
        "dateadd(day, -__W__, {{ audit_today() }}) AND unix_timestamp(x)"
    )


def test_rule_now():
    assert translate("date_trunc('DAY', now())") == "date_trunc('DAY', {{ audit_now() }})"


# ------------------------------------------------------------------------------------------
# cross-cutting guarantees
# ------------------------------------------------------------------------------------------


def test_tokens_untouched():
    """A __W__ / __P__x__ token survives every rule unchanged, in every position a body puts
    one: dateadd delta, LEAST, CAST, INTERVAL, LIMIT, comparison, multiplication."""
    src = ("WHERE a >= dateadd(day, -LEAST(__W__, 90), current_date())\n"
           "  AND b >= current_date() - INTERVAL __W__ DAYS\n"
           "  AND c >= date_add(current_date(), -CAST(__W__ AS INT))\n"
           "  AND d >= __P__warn_run_hours__ * 3600\n"
           "  AND e >= dateadd(hour, -__P__settle_hours__, current_timestamp())\n"
           "  AND f < __P__crit_stmt_share__ * 100\n"
           "LIMIT __P__top_n__")
    out = translate(src)
    for tok in ("__W__", "__P__warn_run_hours__", "__P__settle_hours__", "__P__crit_stmt_share__", "__P__top_n__"):
        assert out.count(tok) == src.count(tok), tok
    assert "__W__" in out and "LIMIT __P__top_n__" in out
    assert "__P__" not in tr.mask("x") and out.count("__P__") == 4


def test_rules_never_touch_literals_or_comments():
    src = ("SELECT 'size(x) unix_timestamp(y) current_date()' AS s,   -- size(z) current_date()\n"
           "       size(l) AS n\n"
           "FROM t   -- system.lakeflow.jobs is SCD2, dateadd(day, -1, current_date())")
    out = translate(src)
    assert "'size(x) unix_timestamp(y) current_date()'" in out
    assert "-- size(z) current_date()" in out
    assert "-- system.lakeflow.jobs is SCD2, dateadd(day, -1, current_date())" in out
    assert "       len(l) AS n" in out


def test_comment_inside_rewritten_call_is_dropped_not_swallowing_the_rest():
    src = ("timestampdiff(SECOND, run_start,   -- the start\n"
           "              last_seen) AS run_s, other_col")
    out = translate(src)
    assert out == "date_diff('second', run_start, last_seen) AS run_s, other_col"


def test_mask_and_strip_comments():
    src = "SELECT 'a''b -- not a comment', x -- real comment\nFROM t"
    m = tr.mask(src)
    assert len(m) == len(src)
    literal = "'a''b -- not a comment'"
    comment = "-- real comment"
    assert m == ("SELECT '" + " " * (len(literal) - 2) + "', x " + " " * len(comment) + "\nFROM t")
    assert tr.strip_comments(src) == "SELECT 'a''b -- not a comment', x\nFROM t"


def test_strip_resource_mask_drops_the_preview_and_keeps_the_plain_column():
    # sql_warehouse_config_current / classic_clusters_config_current shape (THEN repeats the column)
    src = "CASE WHEN warehouse_name IS NULL THEN warehouse_name ELSE concat(substr(warehouse_name, 1, 2), '****') END AS warehouse_name"
    assert tr.strip_resource_mask(src) == "warehouse_name AS warehouse_name"
    # compute_serving_endpoint_cost_status shape (THEN is a NULL literal, X is a COALESCE call)
    src2 = (
        "CASE WHEN COALESCE(ent.endpoint_name, ce.endpoint_name_billing) IS NULL THEN NULL "
        "ELSE concat(substr(COALESCE(ent.endpoint_name, ce.endpoint_name_billing), 1, 2), '****') END AS endpoint_name"
    )
    assert tr.strip_resource_mask(src2) == "COALESCE(ent.endpoint_name, ce.endpoint_name_billing) AS endpoint_name"
    # access_vector_search_traffic shape (a map subscript)
    src3 = (
        "CASE WHEN request_params['endpoint_name'] IS NULL THEN request_params['endpoint_name'] "
        "ELSE concat(substr(request_params['endpoint_name'], 1, 2), '****') END AS endpoint_name"
    )
    assert tr.strip_resource_mask(src3) == "request_params['endpoint_name'] AS endpoint_name"


def test_mask_user_identities_becomes_one_macro_call():
    guid = "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"
    src = (
        "CASE\n"
        "  WHEN actor IS NULL OR actor = '__REDACTED__' THEN actor\n"
        f"  WHEN actor RLIKE {guid} THEN actor\n"
        "  ELSE concat(substr(sha2(lower(trim(actor)), 256), 1, 8), ' ', substr(actor, 1, 2), '***')\n"
        "END AS actor"
    )
    assert tr.mask_user_identities(src) == "{{ mask_user('actor') }} AS actor"


def test_mask_user_identities_keeps_the_coalesced_real_id():
    guid = "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"
    src = (
        "CASE\n"
        "  WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by\n"
        f"  WHEN executed_by RLIKE {guid} THEN executed_by\n"
        "  ELSE concat(COALESCE(executed_by_user_id, substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')\n"
        "END AS executed_by"
    )
    assert tr.mask_user_identities(src) == "{{ mask_user('executed_by', 'executed_by_user_id') }} AS executed_by"


def test_untranslatable_raises_for_a_construct_without_a_rule():
    with pytest.raises(tr.Untranslatable) as e:
        translate("SELECT from_unixtime(x) FROM t")
    assert "from_unixtime(" in str(e.value)
    with pytest.raises(tr.Untranslatable):
        translate("SELECT * FROM t DISTRIBUTE BY x")
    assert tr.UNTRANSLATABLE is tr.Untranslatable
    # a name a rule already handled never trips the final check
    assert tr.untranslated(translate("size(x)")) == []


def test_rules_table_prints():
    text = tr.rules_table()
    for name in RULE_NAMES:
        assert text.count(f"{name}:") >= 1
