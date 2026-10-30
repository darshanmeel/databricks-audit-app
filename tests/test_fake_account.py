"""The Acme fake account (tests/fake_account/build.py) built with the real dbt models, checked
against the numbers worked out by hand for each scenario."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fake_account"))
sys.path.insert(0, str(ROOT / "tools"))

import build as acme_build  # noqa: E402
import dbt_run  # noqa: E402
from app.core import data, rollup, tag_compliance, tag_spend  # noqa: E402
from app.core.tags import TagFilterSet  # noqa: E402


@pytest.fixture(scope="module")
def acme(tmp_path_factory):
    exe = dbt_run.dbt_executable()
    if exe is None:
        pytest.skip("dbt is not installed")
    tmp = tmp_path_factory.mktemp("acme")
    snapshot, db = tmp / "snapshot", tmp / "acme.duckdb"
    exp = acme_build.write(snapshot)
    cmd = dbt_run.construct_dbt_command("dev", vars_dict=dbt_run.build_vars("dev", snapshot / "manifest.json"))
    cmd[0] = exe
    env = {**os.environ, "AUDIT_DB": str(db), "AUDIT_SNAPSHOT_DIR": str(snapshot),
           "DBT_TARGET_PATH": str(tmp / "target"), "DBT_LOG_PATH": str(tmp / "logs")}
    done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout[-3000:]
    con = duckdb.connect(str(db), read_only=True)
    yield con, exp, db
    con.close()


def _rows(con, sql, *params) -> list[dict]:
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _by(rows, key) -> dict:
    return {r[key]: r for r in rows}


def test_warehouse_idle_time(acme):
    con, exp, _ = acme
    idle = _by(_rows(con, "SELECT * FROM findings.f_compute_warehouse_idle_minutes WHERE window_days = 7"), "warehouse_id")
    bi, etl, on = idle["ac_wh_bi"], idle["ac_wh_etl"], idle["ac_wh_alwayson"]
    # BI: 70 short dashboard queries, each followed by the full 10-minute auto-stop wait.
    assert bi["running_minutes"] == pytest.approx(exp["bi"]["running_min"], abs=0.1)
    assert bi["busy_minutes"] == pytest.approx(exp["bi"]["busy_min"], abs=0.1)
    assert bi["counted_idle_minutes"] == pytest.approx(exp["bi"]["counted_min"], abs=0.1)
    assert bi["stop_tail_minutes"] == pytest.approx(exp["bi"]["tail_min"], abs=0.1)
    assert bi["idle_share_pct"] == pytest.approx(98.4, abs=0.1) and bi["status"] == "CRITICAL"
    assert float(bi["net_dbus"]) == pytest.approx(exp["bi"]["dbus"], abs=0.01)
    assert float(bi["est_wasted_usd_list"]) == pytest.approx(exp["bi"]["wasted_usd"], abs=0.01)
    # ETL: busy almost all the time; 20 s and 30 s gaps stay under the 60 s limit.
    assert etl["running_minutes"] == pytest.approx(exp["etl"]["running_min"], abs=0.1)
    assert etl["busy_minutes"] == pytest.approx(exp["etl"]["busy_min"], abs=0.1)
    assert etl["counted_idle_minutes"] == pytest.approx(exp["etl"]["counted_min"], abs=0.1)
    assert etl["start_gap_minutes"] == 0 and etl["between_queries_minutes"] == 0 and etl["status"] == "OK"
    # Auto-stop off: running the whole 7 days with no query.
    assert on["running_minutes"] == exp["alwayson"]["running_min_7d"] and on["no_query_minutes"] == on["running_minutes"]
    assert on["status"] == "CRITICAL"


def test_running_time_matches_the_bill(acme):
    con, exp, _ = acme
    rates = acme_build.DBU_PER_HOUR
    rows = _rows(con, "SELECT i.warehouse_id, i.up_cluster_minutes, i.net_dbus, c.warehouse_size "
                      "FROM findings.f_compute_warehouse_idle_minutes i JOIN findings.f_sql_warehouse_config_current c "
                      "USING (warehouse_id) WHERE i.window_days = 7 AND c.window_days = 0")
    assert len(rows) == 7 + exp["new_active_warehouses"]  # the original seven, plus those extra.py runs in the last 7 days
    for r in rows:
        implied = float(r["net_dbus"]) / (float(r["up_cluster_minutes"]) / 60)
        assert implied == pytest.approx(rates[r["warehouse_size"]], rel=0.02), r["warehouse_id"]


def test_setup_and_last_activity(acme):
    con, _, _ = acme
    cfg = _by(_rows(con, "SELECT * FROM findings.f_compute_warehouse_config_posture"), "warehouse_id")
    assert cfg["ac_wh_alwayson"]["flag_autostop_risk"] and cfg["ac_wh_alwayson"]["status"] == "WARN"
    assert cfg["ac_wh_bi"]["status"] == "OK" and "ac_wh_gone" not in cfg
    cl = _by(_rows(con, "SELECT * FROM findings.f_compute_cluster_config_posture"), "cluster_id")
    assert cl["ac_cl_shared"]["status"] == "CRITICAL" and cl["ac_cl_shared"]["flag_no_policy"]
    assert cl["ac_cl_team"]["status"] == "OK"
    last = {r["warehouse_id"] or r["cluster_id"] or r["name"]: r for r in _rows(con, "SELECT * FROM findings.f_compute_last_activity")}
    assert last["ac_wh_unused"]["status"] == "WARN" and last["ac_wh_unused"]["last_activity_date"] is None
    assert last["ac_wh_new"]["status"] == "NOT_ASSESSED" and "ac_wh_gone" not in last
    assert last["ac_wh_bi"]["status"] == "OK" and last["ac_cl_team"]["status"] == "OK"
    assert last["ac_cl_shared"]["days_since_activity"] == 100 and last["ac_cl_shared"]["status"] == "WARN"
    assert last["acme-old"]["days_since_activity"] == 120 and last["acme-old"]["audit_seen"] is False
    assert last["acme-prod"]["audit_seen"] is True and last["acme-prod"]["status"] == "OK"


def test_jobs(acme):
    con, exp, _ = acme
    rel = _by(_rows(con, "SELECT * FROM findings.f_lakeflow_job_reliability WHERE window_days = 30"), "job_id")
    assert rel["ac_job_ok"]["status"] == "OK" and rel["ac_job_ok"]["failed_runs"] == 0
    assert rel["ac_job_flaky"]["status"] == "WARN" and rel["ac_job_flaky"]["failed_runs"] == 3
    assert rel["ac_job_broken"]["status"] == "CRITICAL" and rel["ac_job_broken"]["consecutive_failures"] == 3
    starts = _rows(con, "SELECT * FROM findings.f_lakeflow_failed_cluster_starts WHERE window_days = 30")
    assert [(r["job_id"], r["termination_code"], r["failed_starts"], r["setup_s_total"]) for r in starts] == [
        ("ac_job_badstart", "CLOUD_FAILURE", exp["badstart"]["failed_starts"], exp["badstart"]["setup_s"])]


def test_slices_match_the_daily_numbers_and_split_by_tag(acme, monkeypatch):
    con, exp, db = acme
    sl = _rows(con, "SELECT warehouse_id, max(origin) AS origin, sum(runs) AS runs, sum(failed) AS failed, "
                    "sum(total_s) AS total_s FROM findings.f_query_slices WHERE window_days = 7 GROUP BY 1 ORDER BY 1")
    perf = _rows(con, "SELECT resource_id AS warehouse_id, sum(runs) AS runs, sum(failed) AS failed, sum(total_s) AS total_s "
                      "FROM findings.f_perf_daily_by_resource WHERE window_days = 7 AND resource_type = 'warehouse' "
                      "GROUP BY 1 ORDER BY 1")
    assert [(r["warehouse_id"], r["runs"], r["failed"], r["total_s"]) for r in sl] == \
        [(r["warehouse_id"], r["runs"], r["failed"], r["total_s"]) for r in perf]
    assert {r["origin"] for r in _rows(con, "SELECT DISTINCT origin FROM findings.f_query_slices "
                                            "WHERE window_days = 7 AND warehouse_id = 'ac_wh_bi'")} == {"dashboard", "tool"}
    assert _by(sl, "warehouse_id")["ac_wh_etl"]["failed"] == exp["etl"]["failed"]

    monkeypatch.setenv("AUDIT_DB", str(db))
    def runs(*tags):
        r = data.read_finding("query_slices", 7, tag_filter=TagFilterSet.from_params(None, None, list(tags)))
        return int(r.df["runs"].sum()) if len(r.df) else 0
    assert runs("costcenter:bi") == exp["bi"]["queries"]       # their own query tag
    assert runs("costcenter:data") == exp["etl"]["queries"]    # their warehouse's tag
    assert runs("costcenter:analytics") > 0                    # acme-analytics' warehouse tag
    # extra.py adds warehouses of its own (finance, treasury, lab) and one query tag (marketing)
    assert sum(runs(f"costcenter:{v}") for v in ["bi", "data", "analytics", "__untagged__", *exp["tag_values"]]) == sum(r["runs"] for r in sl)
    assert runs("costcenter:marketing") == 3 and runs("costcenter:lab") > 0


def test_tags_page_follows_the_filter(acme):
    con, exp, _ = acme
    out = tag_compliance.compliance(con, ["cost_center"], 7, None, TagFilterSet.from_params(None, None, ["costcenter:bi"]))
    by_kind = {t["kind"]: t for t in out["types"]}
    assert by_kind["warehouses"]["total"] == 1
    assert by_kind["queries"]["total"] == exp["bi"]["queries"]


def test_tags_page_counts(acme):
    con, _, _ = acme
    out = tag_compliance.compliance(con, ["cost_center", "domain", "environment"], 30, None, None)
    by = {t["kind"]: t for t in out["types"]}
    # deleted warehouses (ac_wh_gone, and extra.py's ac_wh_oldonly billed 50-70 days ago) are left out;
    # the serverless notebook that ran statements is counted
    assert by["warehouses"]["deleted"] == 2 and by["notebooks"]["total"] == 1
    # on AWS a job's own bill tag is the job's, though the workspace tag inferred from it has the same value
    assert by["jobs"]["found"]["costcenter"]["compute"] == 1 and by["jobs"]["found"]["costcenter"]["workspace"] == 0


def test_untagged_spend(acme):
    con, exp, _ = acme
    rows = _rows(con, "SELECT * FROM findings.f_cost_chargeback_by_allocation_tag WHERE window_days = 30")
    dev_missing = [r for r in rows if r["workspace_id"] == acme_build.DEV and r["is_missing_allocation_key"]]
    # acme-dev: the always-on warehouse and the notebook (3 x 2 DBUs) are untagged; the team cluster is tagged.
    assert float(dev_missing[0]["net_usage_quantity"]) == pytest.approx(exp["alwayson"]["dbus"] + 6)
    assert dev_missing[0]["status"] == "CRITICAL"


def test_every_check_has_rows(acme):
    con, _, _ = acme
    empty = [t for (t,) in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'findings'").fetchall()
        if con.execute(f'SELECT count(*) FROM findings."{t}"').fetchone()[0] == 0]
    assert empty == []


def test_jobs_review_cases(acme):
    con, exp, _ = acme
    one = lambda sql: _rows(con, sql)[0]  # noqa: E731
    # A repaired run is timed on its final attempt, not across the wait before the repair.
    day = one("SELECT * FROM findings.f_lakeflow_daily_state WHERE window_days = 30 AND resource_id = 'ac_job_repaired'")
    assert day["total_run_s"] == exp["repaired"]["run_s"] and day["failed"] == 0
    run = one("SELECT * FROM findings.f_lakeflow_job_recent_runs WHERE window_days = 30 AND job_id = 'ac_job_repaired'")
    assert run["run_minutes"] == 20 and run["attempts"] == exp["repaired"]["attempts"]
    assert run["result_state"] == exp["repaired"]["result"] and not run["in_flight"]
    # Every hourly slice of a pipeline update counts as active time.
    pipe = one("SELECT * FROM findings.f_lakeflow_pipeline_cost WHERE window_days = 30 AND pipeline_id = 'ac_pl_sales'")
    assert pipe["active_seconds_total"] == exp["pipeline"]["active_s"]
    # A serverless pipeline billed by the hour has no idle tail, though its update fills a third of the hour.
    tail = one("SELECT * FROM findings.f_lakeflow_pipeline_idle_tail_duration WHERE window_days = 30 AND pipeline_id = 'ac_pl_orders'")
    assert tail["idle_dbus"] == 0 and tail["status"] == "OK"
    # A job on an all-purpose cluster is priced from the cluster's bill in the hours it ran.
    ap = one("SELECT * FROM findings.f_lakeflow_jobs_on_all_purpose WHERE window_days = 30 AND job_id = 'ac_job_on_ap'")
    assert ap["est_usd_list"] == pytest.approx(exp["on_all_purpose"]["usd"]) and ap["cost_basis"] == "cluster_hours"
    # A duration warning is not a timeout; a job's timeout bounds its tasks.
    jobs = _by(_rows(con, "SELECT * FROM findings.f_lakeflow_jobs_no_timeout WHERE window_days = 30"), "job_id")
    assert jobs["ac_job_ok"]["no_timeout"] and jobs["ac_job_ok"]["has_duration_warning"]
    tasks = _by(_rows(con, "SELECT * FROM findings.f_lakeflow_job_tasks_no_timeout WHERE window_days = 30"), "job_id")
    assert not tasks["ac_job_broken"]["no_timeout"] and tasks["ac_job_broken"]["job_timeout_seconds"] == 7200


def test_query_review_cases(acme):
    con, exp, _ = acme
    # Spill and failures are judged per warehouse and day, not per person.
    spill = _rows(con, "SELECT * FROM findings.f_query_local_spillage WHERE window_days = 7 "
                       "AND warehouse_id = 'ac_wh_etl' AND executed_by LIKE 'dev%'")
    assert len(spill) == 2 and all(r["warehouse_day_spilled_bytes"] == 1_200_000_000 and r["status"] == "WARN" for r in spill)
    failed = _rows(con, "SELECT * FROM findings.f_query_failed_queries_daily WHERE window_days = 7 AND warehouse_id = 'ac_wh_bi'")
    assert len(failed) == 3 and all(r["query_count"] == 2 and r["status"] == "WARN" for r in failed)
    # Statements that differ only in a number are one shape.
    grouped = _rows(con, "SELECT * FROM findings.f_query_costly_statements_grouped WHERE window_days = 7 "
                         "AND sample_statement_text LIKE 'INSERT INTO acme.sales.daily%'")
    assert len(grouped) == 1 and grouped[0]["runs"] == 2
    # A cold start alone is not a full warehouse.
    cold = _rows(con, "SELECT * FROM findings.f_query_queuing_waits WHERE window_days = 7 AND warehouse_id = 'ac_wh_bi' "
                      "AND avg_wait_ms = 120000")
    assert cold and cold[0]["status"] == "OK" and cold[0]["avg_slot_wait_ms"] is None
    # Waits added up per query count six queries waiting together six times; the clock counts once.
    wait = _rows(con, "SELECT sum(provision_s) AS added, sum(provision_clock_s) AS clock FROM findings.f_perf_daily_by_resource "
                      "WHERE window_days = 7 AND warehouse_id = 'ac_wh_bi'")[0]
    assert (float(wait["added"]), float(wait["clock"])) == (138.0, 123.0)
    # Result fetching by clients comes after execution and is counted on its own.
    fetch = _rows(con, "SELECT sum(fetch_s) AS s FROM findings.f_perf_daily_by_resource "
                       "WHERE window_days = 7 AND warehouse_id = 'ac_wh_analytics'")[0]["s"]
    assert float(fetch) == pytest.approx(exp["analytics_wh"]["fetch_s_7d"], abs=0.5)


def test_compute_review_cases(acme):
    con, exp, _ = acme
    last = {r["warehouse_id"]: r for r in _rows(con, "SELECT * FROM findings.f_compute_last_activity WHERE object_type = 'warehouse'")}
    # Billed every day but never queried or restarted: not in use.
    assert last["ac_wh_zombie"]["days_since_activity"] == exp["zombie"]["days"] and last["ac_wh_zombie"]["status"] == "WARN"
    assert last["ac_wh_alwayson"]["days_since_activity"] == 10 and last["ac_wh_alwayson"]["status"] == "OK"
    old = _rows(con, "SELECT * FROM findings.f_compute_cluster_config_posture WHERE cluster_id = 'ac_cl_old'")[0]
    assert old["flag_eol_runtime"] and old["status"] == "WARN"
    idle = {r["warehouse_id"]: r for r in _rows(con, "SELECT * FROM findings.f_compute_warehouse_idle_minutes WHERE window_days = 30")}
    # auto-stop off: nothing waits to stop it, so the time after the last query is running with no query
    on = idle["ac_wh_alwayson"]
    assert on["auto_stop_minutes"] == 0 and on["stop_tail_minutes"] == 0 and on["no_query_minutes"] > 0
    # auto-stop on at 10 min: it stops before its first query (which then waits for one cold start) and after its last
    assert (on["autostop_10_idle_minutes"], on["autostop_10_cold_starts"]) == (20, 1)
    # turning auto-stop on at 10 min: the zombie's one 30-day running period would idle 10 minutes
    z = idle["ac_wh_zombie"]
    assert z["autostop_10_idle_minutes"] == 10 and z["autostop_now_idle_minutes"] == z["no_query_minutes"]
    # three stops came 15-25 minutes after the last query with auto-stop at 10: late, not lag
    late = {r["warehouse_id"]: r for r in _rows(con, "SELECT * FROM findings.f_compute_warehouse_late_autostop WHERE window_days = 7")}
    bi = late["ac_wh_bi"]
    assert (bi["auto_stops"], bi["late_stops"], bi["status"]) == (70, exp["bi"]["late_stops"], "WARN")
    assert bi["late_extra_minutes"] == pytest.approx(exp["bi"]["late_extra_min"], abs=0.1)
    assert bi["longest_wait_minutes"] == pytest.approx(exp["bi"]["longest_wait_min"], abs=0.1)
    assert bi["typical_wait_minutes"] == 10.0
    assert late["ac_wh_etl"]["late_stops"] == 0 and late["ac_wh_etl"]["status"] == "OK"


def test_cost_review_cases(acme):
    con, exp, _ = acme
    split = _rows(con, "SELECT sum(perf_optimized_net_dbus) AS dbus FROM findings.f_overview_serverless_classic_split WHERE window_days = 30")
    # Standard-mode serverless is not performance-optimized.
    assert split[0]["dbus"] == exp["perf_optimized_dbus"]
    vs = _rows(con, "SELECT * FROM findings.f_cost_vector_search_spend WHERE window_days = 30 AND endpoint_id = 'ac_vs_split'")
    # $15 compute + $15 storage on one day is $30, over the $25 line.
    assert len(vs) == 2 and {r["status"] for r in vs} == {"WARN"}
    dsu = _rows(con, "SELECT sum(net_usage_quantity) AS q FROM findings.f_cost_default_storage_dsu WHERE window_days = 30 AND storage_api_type IS NULL")
    assert dsu[0]["q"] == exp["dsu_space"]


def test_governance_review_cases(acme):
    con, _, _ = acme
    admin = _rows(con, "SELECT action_name FROM findings.f_access_admin_role_change_events WHERE window_days = 30")
    # OAuth sign-ins are not admin changes.
    assert "mintOAuthToken" not in {r["action_name"] for r in admin}
    dry = _rows(con, "SELECT status FROM findings.f_access_network_outbound_denials "
                     "WHERE window_days = 30 AND access_type = 'DRY_RUN_DENIAL'")
    assert dry and {r["status"] for r in dry} == {"OK"}
    vs = _rows(con, "SELECT vector_search_key, distinct_index_names FROM findings.f_access_vector_search_traffic WHERE window_days = 30")
    assert {(r["vector_search_key"], r["distinct_index_names"]) for r in vs} == {("acme.docs.chunks_index", 1)}


def test_storage_serving_review_cases(acme):
    con, _, _ = acme
    # A dropped table is neither a small-files nor a predictive optimization to-do.
    assert not _rows(con, "SELECT 1 FROM findings.f_storage_small_files WHERE table_name = 'tmp_dropped'")
    assert not _rows(con, "SELECT 1 FROM findings.f_storage_po_coverage WHERE table_name = 'tmp_dropped'")
    # The external table has PO off but cannot get it: managed tables only, all on.
    cov = _by(_rows(con, "SELECT catalog_name, pct_bytes_without_po FROM findings.f_storage_po_coverage "
                         "WHERE window_days = 30 AND section = 'catalog_summary'"), "catalog_name")
    assert cov["acme"]["pct_bytes_without_po"] == 0.0 and cov["lab"]["pct_bytes_without_po"] > 0
    dorm = {r["endpoint_name"]: r for r in _rows(con, "SELECT * FROM findings.f_compute_serving_dormant_endpoints WHERE window_days = 30")}
    # Billed with no tracked request ever: tracking is off, not idle.
    assert dorm["ac-custom"]["status"] == "NOT_ASSESSED" and dorm["ac-custom"]["not_assessed_reason"] == "usage_tracking_off"


def test_workspace_tag_holds_only_from_its_first_day(acme, monkeypatch):
    con, exp, db = acme
    hist = _rows(con, "SELECT tag_value, valid_from, valid_to FROM tags.tag_workspace_history "
                      "WHERE workspace_id = ? AND tag_key = 'costcenter' ORDER BY valid_from", acme_build.OPS)
    assert [(r["tag_value"], r["valid_to"]) for r in hist] == [("__untagged__", exp["ops"]["from"]), ("ops", None)]
    monkeypatch.setenv("AUDIT_DB", str(db))
    # the untagged notebook: half of its 30 days fall before the tag showed, so half stays untagged
    body = rollup.rollup("cost", "cost_center", 30, workspace_ids=[acme_build.OPS])
    by_value = {e["value"]: e["metrics"]["usd"] for e in body["by_value"]}
    assert by_value["ops"] == pytest.approx(exp["ops"]["job_usd"] + exp["ops"]["nb_usd_30"] / 2)
    assert by_value["__untagged__"] == pytest.approx(exp["ops"]["nb_usd_30"] / 2)
    # the tag filter on spend: the same dollars, the notebook's share from the workspace tag
    found = tag_spend.origins(con, TagFilterSet.from_params(None, None, ["costcenter:ops"]), 30, {acme_build.OPS})
    assert found["usd"] == pytest.approx(exp["ops"]["job_usd"] + exp["ops"]["nb_usd_30"] / 2, abs=0.01)
    by_origin = {o["origin"]: o["usd"] for o in found["keys"][0]["origins"]}
    assert by_origin["workspace"] == pytest.approx(exp["ops"]["nb_usd_30"] / 2, abs=0.01)



def _statuses(con, table, where="1=1", col="status"):
    return {r[0] for r in con.execute(f"SELECT {col} FROM findings.{table} WHERE {where}").fetchall()}


def test_extra_scenarios_reach_their_statuses(acme):
    """The cases extra.py adds, each reaching a status no earlier scenario did."""
    con, _, _ = acme
    reach = {
        "f_lakeflow_job_duration_regression": {"WARN", "CRITICAL"}, "f_lakeflow_job_queue_time": {"CRITICAL"},
        "f_lakeflow_tasks_near_timeout": {"WARN"}, "f_lakeflow_stale_zombie_jobs": {"WARN", "CRITICAL"},
        "f_lakeflow_job_ownership_orphans": {"WARN"}, "f_lakeflow_retries_repairs": {"WARN"},
        "f_lakeflow_succeeded_with_failed_tasks": {"WARN"}, "f_lakeflow_never_started_runs": {"WARN"},
        "f_lakeflow_failed_runs": {"WARN", "CRITICAL"}, "f_lakeflow_pipeline_update_failures_retries": {"CRITICAL"},
        "f_lakeflow_pipeline_cost": {"CRITICAL"}, "f_lakeflow_job_compute_pressure": {"OK"},
        "f_compute_warehouse_autoscale_churn": {"WARN"}, "f_query_warehouse_pressure": {"WARN"},
        "f_query_queuing_waits": {"WARN"}, "f_query_cache_coldstart": {"WARN", "CRITICAL"},
        "f_instance_pools_idle_capacity": {"WARN"}, "f_node_timeline_utilization": {"WARN"},
        "f_compute_serving_dormant_endpoints": {"WARN"}, "f_storage_small_files": {"CRITICAL"},
        "f_storage_po_coverage": {"WARN"}, "f_storage_unused_table_cost": {"CRITICAL"}, "f_storage_growth": {"CRITICAL"},
        "f_po_failure_reasons": {"WARN"}, "f_po_vacuum_reclaimed_bytes": {"WARN", "CRITICAL"},
        "f_po_clustering_column_churn": {"WARN", "CRITICAL"}, "f_po_data_skipping_backfill": {"WARN", "CRITICAL"},
        "f_access_login_concentration": {"CRITICAL"}, "f_access_sensitive_table_reads": {"WARN"},
        "f_access_table_lineage_blast_radius": {"WARN"}, "f_access_column_lineage_sensitive_reach": {"WARN"},
        "f_access_pii_propagation_untagged": {"WARN", "CRITICAL"}, "f_access_network_inbound_denials": {"WARN", "CRITICAL"},
        "f_access_network_outbound_denials": {"WARN"},
    }
    missing = {t: want - _statuses(con, t) for t, want in reach.items() if want - _statuses(con, t)}
    assert missing == {}


def test_extra_workspaces_tags_and_regions(acme):
    con, _, _ = acme
    region = {r["workspace_id"]: r["in_snapshot_region"] for r in _rows(con, "SELECT workspace_id, in_snapshot_region FROM dims.dim_workspace")}
    # billed in another cloud or region, or cancelled: no row in any regional table
    assert region["2006"] and region["2014"] and region["2011"]
    assert not any(region[w] for w in ("2007", "2008", "2012", "2013"))
    tag = {r["workspace_id"]: r["tag_value"] for r in _rows(con, "SELECT workspace_id, tag_value FROM tags.tag_workspace WHERE tag_key = 'costcenter'")}
    assert tag["2009"] == "__mixed__" and tag["2010"] == "__untagged__"        # 50/50 and 40 percent tagged
    assert tag["2006"] == "finance" and tag["2007"] == "emea"                 # an Azure workspace tag on every bill row
    prices = _rows(con, "SELECT cloud, sku_name, pricing_default, price_end_time FROM findings.f_pricing_list_prices_raw WHERE window_days = 0")
    assert {r["cloud"] for r in prices} == {"aws", "azure", "gcp"}
    # the Azure jobs rate changed 60 days ago: an ended row at 0.30 and an open one at 0.35
    azure_jobs = sorted((float(r["pricing_default"]), r["price_end_time"] is None) for r in prices
                        if r["cloud"] == "azure" and r["sku_name"] == "PREMIUM_JOBS_COMPUTE")
    assert azure_jobs == [(0.30, False), (0.35, True)]


def test_example_entitlements_load(acme, tmp_path):
    """The entitlements file written next to the snapshot loads like the export's: names lower-cased
    and trimmed, a missing column meaning 'all', and a user with no row seeing nothing."""
    from app.core import rls
    _, exp, _ = acme
    snap = tmp_path / "snap"
    acme_build.write(snap)
    con = duckdb.connect()
    cfg = rls.Rls(True, "a.b.c", ("workspace", "warehouse", "environment"), "query")
    assert rls.load_entitlements(con, snap / "entitlements" / "entitlements.parquet", {"user": "finops@acme.example"}, cfg) is None
    assert rls.grants_for(con, "bi@acme.example") == [("2001", "ac_wh_bi", "*")]
    assert sorted(rls.grants_for(con, "de@acme.example")) == [("2001", "*", "*"), ("2005", "*", "*")]
    assert rls.grants_for(con, "ops@acme.example") == [("*", "*", "prod")]
    assert rls.grants_for(con, "mixed.case@acme.example") == [("2011", "*", "*")]
    assert rls.grants_for(con, "nobody@acme.example") == [] and len(exp["entitlements"]) == 8
