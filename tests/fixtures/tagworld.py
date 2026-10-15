"""tests/fixtures/tagworld.py -- P4-T (tag search with the nested rollup): the ONE shared fixture
builder for both P4-T lanes (TAG-IDX and TAG-ROLL). Written byte-identical in both worktrees
from tasks/P4-T-SPEC.md appendix A; owned by TAG-ROLL. Never edit it inside TAG-IDX -- report a
needed change to the orchestrator instead.

Every id this file writes carries the `tg_` prefix, and it writes only into its own three
workspaces (tg_ws_a, tg_ws_m, tg_ws_s), so no other builder's per-workspace figures move. The tag
keys it uses (tg_cc, TG-CC, tg_app, tg_env, tg_pii) exist nowhere else in the fixture set, so for
those keys every other builder's spend is simply "untagged".

Scenario map (all DBU; every usage row cloud 'aws'; prices: tg_SKU_SQL $1, tg_SKU_JOBS $1,
tg_SKU_SERVERLESS $1, tg_SKU_AP $2 per DBU; tg_SKU_NOPRICE has no list price):

tg_ws_a  "tg-alpha"  workspace tag tg_cc = alpha (inferred over classic compute only: 205 of 260
                     tg_cc-tagged DBUs = 78.8% dominance, 260 of 330 classic DBUs = 78.8% coverage;
                     tg_env prod on 150 of 330 = 45.5%, under the coverage floor)
  tg_wh_a1  warehouse, tags {tg_cc: alpha, tg_env: prod} (an older SCD2 row said tg_cc: old)
            D(1)  billed 100 DBU; attributed s1 30 (tg_cc q_fin), s2 10 (tg_cc q_fin),
                  s3 20 (no tags), s4 10 (tg_app bi)  -> 70 to queries, 30 idle
            D(3)  billed 50 DBU; no attributed rows    -> 50 not split to queries
  tg_wh_a2  warehouse, no tags
            D(1)  billed 40 DBU; attributed s5 30 (tg_cc q_mkt), s6 20 (no tags)
                  -> attributed 50 > billed 40: scaled to 24 + 16, idle 0
            D(200) billed 30 DBU (outside every window; workspace-tag coverage only)
  tg_wh_a3  warehouse, tags {TG-CC: beta}  (spelling variant of tg_cc; a conflict with alpha)
            D(2)  billed 25 DBU; no attributed rows    -> 25 not split
  tg_cl_a1  all-purpose cluster, tags {tg_cc: alpha}
            D(1) 30 DBU at $2; D(1) 5 DBU on tg_SKU_NOPRICE (unpriced); D(20) 10 DBU at $2;
            D(45) 10 DBU at $2; D0 999 DBU at $2 (today: never counted anywhere)
  tg_cl_a2  job cluster of tg_job_a1, no resource tags; billing carries the job's tag
            D(2)  20 DBU, custom_tags {tg_cc: etl}, job tg_job_a1 (job tag tg_cc etl)
  tg_cl_a3  all-purpose cluster, no own tags, on pool tg_pool_a1 {tg_cc: poolteam}
            D(4)  10 DBU at $2, custom_tags {tg_cc: poolteam}
  serverless job tg_job_a2 (no job tags), budget policy tg_bp_1
            D(2)  15 DBU, custom_tags {tg_cc: shared}
  serverless pipeline tg_pl_a1 (pipeline tag tg_cc stream)
            D(2)  12 DBU, custom_tags {}
tg_ws_m  "tg-mixed"  workspace tag tg_cc = mixed (red 50 / blue 40 of 90 tagged DBUs = 55.6%)
  tg_cl_m1  all-purpose, {tg_cc: red}   D(1) 50 DBU at $2
  tg_cl_m2  all-purpose, {tg_cc: blue}  D(1) 40 DBU at $2
  tg_cl_m3  job cluster of tg_job_m1 (untagged job), no tags  D(1) 10 DBU at $1
tg_ws_s  "tg-sparse" workspace tag tg_cc not inferred (20 of 100 DBUs tagged = 20% coverage)
  tg_cl_s1  all-purpose, {tg_cc: gamma} D(1) 20 DBU at $2
  tg_wh_s1  warehouse, no tags
            D(1)  billed 80 DBU; attributed s7 40 (tg_cc delta, tg_app etl), s8 20 (no tags)
                  -> 60 to queries, 20 idle

query.history (performance rollup; statement ids equal the attributed dbsql_statement_ids):
  s1..s8 as above on D(1); s3 and s8 FAILED; s9 serverless (no warehouse) in tg_ws_a on D(2);
  s10 on tg_wh_a1 at D(40) (tg_cc q_fin; inside the 90-day window only).
lakeflow.job_run_timeline: three SUCCEEDED runs for each of tg_job_a1, tg_job_a2, tg_job_m1 (so
  lakeflow_job_reliability has rows to filter).
Unity Catalog: tg_cat.tg_sch.tg_tbl1 (table tag tg_cc data_fin), tg_cat.tg_sch.tg_tbl2 (no table
  tag; schema tag tg_cc data_sch), column tag tg_pii with an EMPTY value on tg_tbl1.col1, volume
  tag tg_cc data_vol on tg_cat.tg_sch.tg_vol1. Both tables were last altered 400 days ago, so
  access_dead_table_candidates lists them.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()  # 2026-09-21 -- "today" on the pinned test target


def D(n: int) -> date:
    return D0 - timedelta(days=n)


def T(n: int, hour: int = 0) -> datetime:
    return datetime.combine(D(n), datetime.min.time()) + timedelta(hours=hour)


ACCOUNT_ID = "tg_acct"
WS_A, WS_M, WS_S = "tg_ws_a", "tg_ws_m", "tg_ws_s"
CLOUD = "aws"

SKU_SQL, SKU_JOBS, SKU_SERVERLESS, SKU_AP, SKU_NOPRICE = (
    "tg_SKU_SQL", "tg_SKU_JOBS", "tg_SKU_SERVERLESS", "tg_SKU_AP", "tg_SKU_NOPRICE",
)


def _usage_metadata(**ids):
    md = {k: None for k in (
        "cluster_id", "job_id", "warehouse_id", "instance_pool_id", "node_type", "job_run_id",
        "notebook_id", "dlt_pipeline_id", "endpoint_name", "endpoint_id", "dlt_update_id",
        "dlt_maintenance_id", "run_name", "job_name", "notebook_path", "central_clean_room_id",
        "source_region", "destination_region", "app_id", "app_name", "metastore_id",
        "private_endpoint_name", "storage_api_type", "budget_policy_id", "ai_runtime_pool_id",
        "catalog_id", "networking_client", "recipient_id", "usage_policy_id",
    )}
    md.update(ids)
    return md


def _product_features(is_serverless: bool = False):
    return {
        "jobs_tier": None, "sql_tier": None, "dlt_tier": None,
        "is_serverless": is_serverless, "is_photon": False, "serving_type": None,
        "networking": {"connectivity_type": None},
        "ai_runtime": {"compute_type": None},
        "model_serving": {"offering_type": None},
        "ai_gateway": {"feature_type": None},
        "serverless_gpu": {"workload_type": None},
        "agent_bricks": {"problem_type": None, "workload_type": None},
        "performance_target": None,
        "ai_functions": {"ai_function": None},
        "apps": {"compute_size": None},
        "lakeflow_connect": {"task_type": None, "zerobus_request_type": None},
        "lakebase": {"storage_type": None, "compute_type": None},
        "ai_bi_genie": {"capability_type": None, "offering_type": None},
        "genie": {"offering_type": None},
    }


_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def usage(con, record_id, *, ws, day, qty, sku, tags=None, product="ALL_PURPOSE",
          serverless=False, **ids):
    start = T(day, 1)
    con.execute(_USAGE_SQL, [
        ACCOUNT_ID, ws, record_id, sku, CLOUD, start, start + timedelta(hours=1), D(day),
        tags or {}, "DBU", qty, _usage_metadata(**ids),
        {"run_as": None, "created_by": None, "owned_by": None, "run_by": None},
        "ORIGINAL", D(day), product, _product_features(serverless), "COMPUTE_TIME",
    ])


_ATTR_SQL = (
    "INSERT INTO billing__attributed_usage (record_id, usage_metadata, identity_metadata, "
    "start_time, end_time, usage_date, usage_unit, active_usage_quantity, granular_tags, "
    "usage_record_ids, account_id, workspace_id, sku_name, cloud, billing_origin_product, "
    "custom_tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def attributed(con, stmt, *, ws, warehouse, day, qty, query_tags, usage_record_id):
    con.execute(_ATTR_SQL, [
        f"tg_attr_{stmt}",
        {"dbsql_statement_id": stmt, "warehouse_id": warehouse, "client_application": None},
        {"executed_by": "tg_user@example.com"}, T(day, 1), T(day, 2), D(day), "DBU", qty,
        {"query_tags": query_tags}, [usage_record_id], ACCOUNT_ID, ws, SKU_SQL, CLOUD, "SQL", {},
    ])


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(con, sku, rate):
    con.execute(_LP_SQL, [
        ACCOUNT_ID, AS_OF - timedelta(days=400), None, sku, CLOUD, "USD", "DBU",
        {"default": rate, "promotional": {"default": None}, "effective_list": {"default": rate}},
    ])


def warehouse(con, wh, ws, tags, change_day=30):
    con.execute(
        "INSERT INTO compute__warehouses (warehouse_id, workspace_id, account_id, warehouse_name, "
        "warehouse_type, warehouse_channel, warehouse_size, min_clusters, max_clusters, "
        "auto_stop_minutes, tags, change_time, delete_time, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [wh, ws, ACCOUNT_ID, f"{wh}-name", "PRO", "CURRENT", "Small", 1, 1, 10, tags,
         T(change_day), None, "tg_owner@example.com"],
    )


def cluster(con, cl, ws, tags, source="UI", pool=None):
    con.execute(
        "INSERT INTO compute__clusters (account_id, workspace_id, cluster_id, cluster_name, "
        "owned_by, create_time, delete_time, driver_node_type, worker_node_type, worker_count, "
        "min_autoscale_workers, max_autoscale_workers, auto_termination_minutes, "
        "enable_elastic_disk, tags, cluster_source, driver_instance_pool_id, "
        "worker_instance_pool_id, dbr_version, change_time, change_date, data_security_mode) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [ACCOUNT_ID, ws, cl, f"{cl}-name", "tg_owner@example.com", T(60), None, "m5.xlarge",
         "m5.xlarge", 2, None, None, 30, True, tags, source, pool, pool, "15.4.x-scala2.12",
         T(60), D(60), "USER_ISOLATION"],
    )


def job(con, job_id, ws, tags):
    con.execute(
        "INSERT INTO lakeflow__jobs (account_id, workspace_id, job_id, name, creator_id, tags, "
        "run_as, change_time, delete_time, create_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [ACCOUNT_ID, ws, job_id, f"{job_id}-name", "tg_creator", tags, "tg_runner", T(60), None,
         T(60)],
    )


def job_runs(con, job_id, ws):
    for n in (1, 2, 3):
        con.execute(
            "INSERT INTO lakeflow__job_run_timeline (account_id, workspace_id, job_id, run_id, "
            "period_start_time, period_end_time, trigger_type, result_state, run_type, "
            "termination_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [ACCOUNT_ID, ws, job_id, f"{job_id}_run{n}", T(n + 1, 2), T(n + 1, 3), "PERIODIC",
             "SUCCEEDED", "JOB_RUN", "SUCCESS"],
        )


_QH_COLS = (
    "account_id, workspace_id, statement_id, executed_by, execution_status, compute, "
    "statement_text, statement_type, total_duration_ms, waiting_for_compute_duration_ms, "
    "waiting_at_capacity_duration_ms, execution_duration_ms, start_time, end_time, "
    "from_result_cache, spilled_local_bytes, query_tags"
)


def statement(con, stmt, *, ws, warehouse_id, day, status, dur, queue_capacity=0,
              queue_compute=0, spill=0, query_tags=None):
    ctype = "PRO_WAREHOUSE" if warehouse_id else "SERVERLESS_COMPUTE"
    con.execute(
        f"INSERT INTO query__history ({_QH_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [ACCOUNT_ID, ws, stmt, "tg_user@example.com", status,
         {"type": ctype, "cluster_id": None, "warehouse_id": warehouse_id},
         f"SELECT * FROM tg_cat.tg_sch.tg_tbl1 /* {stmt} */", "SELECT", dur, queue_compute,
         queue_capacity, dur - queue_compute - queue_capacity, T(day, 1),
         T(day, 1) + timedelta(milliseconds=dur), False, spill, query_tags or {}],
    )


def build(con: duckdb.DuckDBPyConnection) -> None:
    for ws, name in ((WS_A, "tg-alpha"), (WS_M, "tg-mixed"), (WS_S, "tg-sparse")):
        con.execute(
            "INSERT INTO access__workspaces_latest (account_id, workspace_id, workspace_name, "
            "workspace_url, create_time, status) VALUES (?,?,?,?,?,?)",
            [ACCOUNT_ID, ws, name, f"https://{name}.cloud.databricks.com/", T(300), "RUNNING"],
        )

    for sku, rate in ((SKU_SQL, 1.0), (SKU_JOBS, 1.0), (SKU_SERVERLESS, 1.0), (SKU_AP, 2.0)):
        list_price(con, sku, rate)  # SKU_NOPRICE deliberately has no price row

    # ---- resources (latest SCD2 row wins; tg_wh_a1 also has an older, superseded row) -------
    warehouse(con, "tg_wh_a1", WS_A, {"tg_cc": "old"}, change_day=90)
    warehouse(con, "tg_wh_a1", WS_A, {"tg_cc": "alpha", "tg_env": "prod"}, change_day=30)
    warehouse(con, "tg_wh_a2", WS_A, {})
    warehouse(con, "tg_wh_a3", WS_A, {"TG-CC": "beta"})
    warehouse(con, "tg_wh_s1", WS_S, {})
    con.execute(
        "INSERT INTO compute__instance_pools (account_id, workspace_id, instance_pool_id, "
        "change_time, create_time, delete_time, instance_pool_name, tags, node_type) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [ACCOUNT_ID, WS_A, "tg_pool_a1", T(60), T(60), None, "tg-pool", {"tg_cc": "poolteam"},
         "m5.xlarge"],
    )
    cluster(con, "tg_cl_a1", WS_A, {"tg_cc": "alpha"})
    cluster(con, "tg_cl_a2", WS_A, {}, source="JOB")
    cluster(con, "tg_cl_a3", WS_A, {}, pool="tg_pool_a1")
    cluster(con, "tg_cl_m1", WS_M, {"tg_cc": "red"})
    cluster(con, "tg_cl_m2", WS_M, {"tg_cc": "blue"})
    cluster(con, "tg_cl_m3", WS_M, {}, source="JOB")
    cluster(con, "tg_cl_s1", WS_S, {"tg_cc": "gamma"})
    job(con, "tg_job_a1", WS_A, {"tg_cc": "etl"})
    job(con, "tg_job_a2", WS_A, {})
    job(con, "tg_job_m1", WS_M, {})
    for j, ws in (("tg_job_a1", WS_A), ("tg_job_a2", WS_A), ("tg_job_m1", WS_M)):
        job_runs(con, j, ws)
    con.execute(
        "INSERT INTO lakeflow__pipelines (workspace_id, pipeline_id, pipeline_type, name, "
        "created_by, run_as, tags, change_time, delete_time, account_id, create_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [WS_A, "tg_pl_a1", "ETL", "tg-pipeline", "tg_creator", "tg_runner", {"tg_cc": "stream"},
         T(60), None, ACCOUNT_ID, T(60)],
    )

    # ---- billing.usage ------------------------------------------------------------------------
    wh_a1_tags = {"tg_cc": "alpha", "tg_env": "prod"}
    usage(con, "tg_u_wh_a1_d1", ws=WS_A, day=1, qty=100.0, sku=SKU_SQL, tags=wh_a1_tags,
          product="SQL", warehouse_id="tg_wh_a1")
    usage(con, "tg_u_wh_a1_d3", ws=WS_A, day=3, qty=50.0, sku=SKU_SQL, tags=wh_a1_tags,
          product="SQL", warehouse_id="tg_wh_a1")
    usage(con, "tg_u_wh_a2_d1", ws=WS_A, day=1, qty=40.0, sku=SKU_SQL, product="SQL",
          warehouse_id="tg_wh_a2")
    # Outside every window: only the unwindowed workspace-tag inference sees it.
    usage(con, "tg_u_wh_a2_d200", ws=WS_A, day=200, qty=30.0, sku=SKU_SQL, product="SQL",
          warehouse_id="tg_wh_a2")
    usage(con, "tg_u_wh_a3_d2", ws=WS_A, day=2, qty=25.0, sku=SKU_SQL, tags={"TG-CC": "beta"},
          product="SQL", warehouse_id="tg_wh_a3")
    ap_alpha = {"tg_cc": "alpha"}
    usage(con, "tg_u_cl_a1_d1", ws=WS_A, day=1, qty=30.0, sku=SKU_AP, tags=ap_alpha, cluster_id="tg_cl_a1")
    usage(con, "tg_u_cl_a1_np", ws=WS_A, day=1, qty=5.0, sku=SKU_NOPRICE, tags=ap_alpha, cluster_id="tg_cl_a1")
    usage(con, "tg_u_cl_a1_d20", ws=WS_A, day=20, qty=10.0, sku=SKU_AP, tags=ap_alpha, cluster_id="tg_cl_a1")
    usage(con, "tg_u_cl_a1_d45", ws=WS_A, day=45, qty=10.0, sku=SKU_AP, tags=ap_alpha, cluster_id="tg_cl_a1")
    usage(con, "tg_u_cl_a1_d0", ws=WS_A, day=0, qty=999.0, sku=SKU_AP, tags=ap_alpha, cluster_id="tg_cl_a1")
    usage(con, "tg_u_cl_a2_d2", ws=WS_A, day=2, qty=20.0, sku=SKU_JOBS, tags={"tg_cc": "etl"},
          product="JOBS", cluster_id="tg_cl_a2", job_id="tg_job_a1", job_run_id="tg_job_a1_run1")
    usage(con, "tg_u_cl_a3_d4", ws=WS_A, day=4, qty=10.0, sku=SKU_AP, tags={"tg_cc": "poolteam"},
          cluster_id="tg_cl_a3", instance_pool_id="tg_pool_a1")
    usage(con, "tg_u_job_a2_d2", ws=WS_A, day=2, qty=15.0, sku=SKU_SERVERLESS,
          tags={"tg_cc": "shared"}, product="JOBS", serverless=True, job_id="tg_job_a2",
          job_run_id="tg_job_a2_run1", budget_policy_id="tg_bp_1")
    usage(con, "tg_u_pl_a1_d2", ws=WS_A, day=2, qty=12.0, sku=SKU_SERVERLESS, product="DLT",
          serverless=True, dlt_pipeline_id="tg_pl_a1")
    usage(con, "tg_u_cl_m1_d1", ws=WS_M, day=1, qty=50.0, sku=SKU_AP, tags={"tg_cc": "red"}, cluster_id="tg_cl_m1")
    usage(con, "tg_u_cl_m2_d1", ws=WS_M, day=1, qty=40.0, sku=SKU_AP, tags={"tg_cc": "blue"}, cluster_id="tg_cl_m2")
    usage(con, "tg_u_cl_m3_d1", ws=WS_M, day=1, qty=10.0, sku=SKU_JOBS, product="JOBS",
          cluster_id="tg_cl_m3", job_id="tg_job_m1", job_run_id="tg_job_m1_run1")
    usage(con, "tg_u_cl_s1_d1", ws=WS_S, day=1, qty=20.0, sku=SKU_AP, tags={"tg_cc": "gamma"}, cluster_id="tg_cl_s1")
    usage(con, "tg_u_wh_s1_d1", ws=WS_S, day=1, qty=80.0, sku=SKU_SQL, product="SQL",
          warehouse_id="tg_wh_s1")

    # ---- billing.attributed_usage (per statement) ---------------------------------------------
    for stmt, qty, qtags in (("tg_stmt_1", 30.0, {"tg_cc": "q_fin"}), ("tg_stmt_2", 10.0, {"tg_cc": "q_fin"}),
                             ("tg_stmt_3", 20.0, {}), ("tg_stmt_4", 10.0, {"tg_app": "bi"})):
        attributed(con, stmt, ws=WS_A, warehouse="tg_wh_a1", day=1, qty=qty, query_tags=qtags,
                   usage_record_id="tg_u_wh_a1_d1")
    for stmt, qty, qtags in (("tg_stmt_5", 30.0, {"tg_cc": "q_mkt"}), ("tg_stmt_6", 20.0, {})):
        attributed(con, stmt, ws=WS_A, warehouse="tg_wh_a2", day=1, qty=qty, query_tags=qtags,
                   usage_record_id="tg_u_wh_a2_d1")
    for stmt, qty, qtags in (("tg_stmt_7", 40.0, {"tg_cc": "delta", "tg_app": "etl"}), ("tg_stmt_8", 20.0, {})):
        attributed(con, stmt, ws=WS_S, warehouse="tg_wh_s1", day=1, qty=qty, query_tags=qtags,
                   usage_record_id="tg_u_wh_s1_d1")

    # ---- query.history (performance rollup) ---------------------------------------------------
    statement(con, "tg_stmt_1", ws=WS_A, warehouse_id="tg_wh_a1", day=1, status="FINISHED", dur=10000,
              queue_capacity=1000, query_tags={"tg_cc": "q_fin"})
    statement(con, "tg_stmt_2", ws=WS_A, warehouse_id="tg_wh_a1", day=1, status="FINISHED", dur=20000,
              spill=1000, query_tags={"tg_cc": "q_fin"})
    statement(con, "tg_stmt_3", ws=WS_A, warehouse_id="tg_wh_a1", day=1, status="FAILED", dur=5000,
              queue_compute=500)
    statement(con, "tg_stmt_4", ws=WS_A, warehouse_id="tg_wh_a1", day=1, status="FINISHED", dur=3000,
              query_tags={"tg_app": "bi"})
    statement(con, "tg_stmt_5", ws=WS_A, warehouse_id="tg_wh_a2", day=1, status="FINISHED", dur=8000,
              queue_capacity=2000, query_tags={"tg_cc": "q_mkt"})
    statement(con, "tg_stmt_6", ws=WS_A, warehouse_id="tg_wh_a2", day=1, status="FINISHED", dur=4000)
    statement(con, "tg_stmt_7", ws=WS_S, warehouse_id="tg_wh_s1", day=1, status="FINISHED", dur=6000,
              spill=500, query_tags={"tg_cc": "delta", "tg_app": "etl"})
    statement(con, "tg_stmt_8", ws=WS_S, warehouse_id="tg_wh_s1", day=1, status="FAILED", dur=2000)
    statement(con, "tg_stmt_9", ws=WS_A, warehouse_id=None, day=2, status="FINISHED", dur=1000)
    statement(con, "tg_stmt_10", ws=WS_A, warehouse_id="tg_wh_a1", day=40, status="FINISHED", dur=7000,
              query_tags={"tg_cc": "q_fin"})

    # ---- Unity Catalog objects and tags -------------------------------------------------------
    for tbl in ("tg_tbl1", "tg_tbl2"):
        con.execute(
            "INSERT INTO information_schema__tables (table_catalog, table_schema, table_name, "
            "table_type, table_owner, created, last_altered) VALUES (?,?,?,?,?,?,?)",
            ["tg_cat", "tg_sch", tbl, "MANAGED", "tg_owner@example.com", T(500), T(400)],
        )
    con.execute("INSERT INTO information_schema__table_tags VALUES (?,?,?,?,?)",
                ["tg_cat", "tg_sch", "tg_tbl1", "tg_cc", "data_fin"])
    con.execute("INSERT INTO information_schema__schema_tags VALUES (?,?,?,?)",
                ["tg_cat", "tg_sch", "tg_cc", "data_sch"])
    con.execute(
        "INSERT INTO information_schema__column_tags (catalog_name, schema_name, table_name, "
        "column_name, tag_name, tag_value) VALUES (?,?,?,?,?,?)",
        ["tg_cat", "tg_sch", "tg_tbl1", "col1", "tg_pii", ""],
    )
    con.execute("INSERT INTO information_schema__volume_tags VALUES (?,?,?,?,?)",
                ["tg_cat", "tg_sch", "tg_vol1", "tg_cc", "data_vol"])
