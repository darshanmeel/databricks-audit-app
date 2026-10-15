"""A fake account (Acme) with known answers, written as a snapshot folder the dbt dev build reads.

    python tests/fake_account/build.py [--out <folder>]   # default tests/fake_account/snapshot

Then build and open it like a real snapshot:

    AUDIT_SNAPSHOT_DIR=tests/fake_account/snapshot AUDIT_DB=tests/fake_account/acme.duckdb \\
        python tools/dbt_run.py build --target dev
    AUDIT_DB=tests/fake_account/acme.duckdb python -m app.api

tests/test_fake_account.py builds it and checks every scenario below against the hand-worked
numbers in EXPECTED. All times are relative to AS_OF (2026-09-21 12:00 UTC); D(n) is n days back.

Workspaces: 2001 acme-prod, 2002 acme-dev, 2003 acme-old (last billed D(120), nothing since).
Warehouses:
  ac_wh_bi      serverless Small, auto-stop 10, tag cost_center=bi; D(1)-D(7) one 5 s dashboard
                query (query tag cost_center=bi) per hour 08-17, then the 10-minute auto-stop wait.
  ac_wh_etl     pro Medium, auto-stop 5, tag cost_center=data; D(1)-D(7) two long loads 01:00-03:00,
                then 5 minutes to auto-stop; the second load on D(1) failed.
  ac_wh_alwayson pro X-Small, auto-stop off, no tags, running since D(40); one query on D(10).
  ac_wh_unused  pro Small created D(200), never started, queried or billed.
  ac_wh_new     serverless created D(5), not used yet.
  ac_wh_gone    deleted D(20).
Clusters: ac_cl_shared (all-purpose, no isolation, no policy, auto-termination off, last billed
  D(100)), ac_cl_team (all-purpose, user isolation, policy, 30 min, billed D(2), tag team=platform),
  ac_cl_job (job cluster).
Jobs (acme-prod): ac_job_ok (10 daily successes), ac_job_flaky (3 of 10 failed, latest ok),
  ac_job_broken (8 runs, the last 3 failed), ac_job_badstart (2 task runs failed while starting).
Audit: people active in acme-prod on D(1) and acme-dev on D(3); nothing in acme-old.
_more, _long_task, _governance and _rest add the rest, so every check has rows: job costs and
tasks, a 12-hour run, a pipeline, notebooks, egress, vector search, serving and the AI gateway,
this app's own queries, spill and pruning, cluster node use, Unity Catalog tables, grants, tags,
masks, row filters, shares, classification, lineage, audit events, network denials, an instance
pool, Genie, default storage and predictive optimization.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
import ddl  # noqa: E402
sys.path.insert(0, str(HERE.parents[1]))
from tools.snapshot import PREDICATES, parse_metastore_id  # noqa: E402  (as a real export records them)
import idle_waste as iw  # noqa: E402  (struct builders only)

AS_OF = datetime(2026, 9, 21, 12, 0, 0)
ACCOUNT = "acme-account"
PROD, DEV, OLD, OPS, ANALYTICS = "2001", "2002", "2003", "2004", "2005"
SKU_SERVERLESS = "PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST"
SKU_PRO = "PREMIUM_SQL_PRO_COMPUTE_US_EAST"
SKU_ALL_PURPOSE = "PREMIUM_ALL_PURPOSE_COMPUTE"
SKU_JOBS = "PREMIUM_JOBS_COMPUTE"
SKU_DLT = "PREMIUM_DLT_ADVANCED_COMPUTE"
SKU_NOTEBOOK = "PREMIUM_SERVERLESS_COMPUTE_US_EAST"
SKU_SERVING = "PREMIUM_SERVERLESS_REAL_TIME_INFERENCE_US_EAST"
SKU_TOKENS = "PREMIUM_DATABRICKS_FOUNDATION_MODEL_TOKENS"
SKU_VECTOR = "PREMIUM_VECTOR_SEARCH_ENDPOINT_US_EAST"
SKU_EGRESS = "PREMIUM_PUBLIC_CONNECTIVITY_DATA_PROCESSED_US_EAST"
SKU_DLT_SERVERLESS = "PREMIUM_JOBS_SERVERLESS_COMPUTE_US_EAST"
SKU_DSU, SKU_DSU_EAST = "PREMIUM_DEFAULT_STORAGE", "PREMIUM_DEFAULT_STORAGE_US_EAST"
RATES = {SKU_SERVERLESS: 0.70, SKU_PRO: 0.55, SKU_ALL_PURPOSE: 0.55, SKU_JOBS: 0.15, SKU_DLT: 0.36,
         SKU_NOTEBOOK: 0.95, SKU_SERVING: 0.07, SKU_TOKENS: 0.07, SKU_VECTOR: 0.28, SKU_EGRESS: 0.10,
         SKU_DLT_SERVERLESS: 0.35, SKU_DSU: 0.023, SKU_DSU_EAST: 0.023}
UNITS = {SKU_EGRESS: "GB", SKU_DSU: "DSU", SKU_DSU_EAST: "DSU"}
DBU_PER_HOUR = {"SMALL": 12, "MEDIUM": 24, "X_SMALL": 6}


def D(n: int) -> date:
    return (AS_OF - timedelta(days=n)).date()


def DT(n: int, h: int = 0, m: int = 0, s: int = 0) -> datetime:
    return datetime.combine(D(n), datetime.min.time()) + timedelta(hours=h, minutes=m, seconds=s)


class Batched:
    """The connection the scenarios write through: put() rows are held per table and column set and
    inserted together before any other statement runs, which keeps a 20,000-row build fast."""

    def __init__(self, con):
        self.con, self.rows = con, {}

    def add(self, table: str, row: dict) -> None:
        self.rows.setdefault((table, tuple(row)), []).append(list(row.values()))

    def flush(self) -> None:
        for (table, cols), rows in self.rows.items():
            names = ", ".join(f'"{c}"' for c in cols)
            self.con.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({", ".join("?" for _ in cols)})', rows)
        self.rows = {}

    def execute(self, *args, **kwargs):
        self.flush()
        return self.con.execute(*args, **kwargs)


def put(con, table: str, **row) -> None:
    con.add(table, row)


def bill(con, rid: str, ws: str, sku: str, start: datetime, hours: float, dbu_per_hour: float, *,
         tags: dict | None = None, serverless: bool = False, product: str = "SQL", unit: str = "DBU",
         usage_type: str = "COMPUTE_TIME", features: dict | None = None, run_as: str | None = None,
         **meta) -> float:
    """One billing row from `start` lasting `hours` (within one clock hour); returns its quantity."""
    dbus = round(dbu_per_hour * hours, 6)
    put(con, "billing__usage", account_id=ACCOUNT, workspace_id=ws, record_id=rid, sku_name=sku, cloud="aws",
        usage_start_time=start.replace(minute=0, second=0), usage_end_time=start.replace(minute=0, second=0) + timedelta(hours=1),
        usage_date=start.date(), custom_tags=tags or {}, usage_unit=unit, usage_quantity=dbus,
        usage_metadata=iw._usage_metadata(**meta), identity_metadata=iw._identity_metadata(run_as=run_as),
        record_type="ORIGINAL", ingestion_date=start.date(), billing_origin_product=product,
        product_features=iw._product_features(is_serverless=serverless, **(features or {})), usage_type=usage_type)
    return dbus


def warehouse(con, wid: str, ws: str, wtype: str, size: str, auto_stop: int, created: datetime, *,
              tags: dict | None = None, deleted: datetime | None = None) -> None:
    put(con, "compute__warehouses", warehouse_id=wid, workspace_id=ws, account_id=ACCOUNT, warehouse_name=wid,
        warehouse_type=wtype, warehouse_channel="CHANNEL_NAME_CURRENT", warehouse_size=size, min_clusters=1,
        max_clusters=1, auto_stop_minutes=auto_stop, tags=tags or {}, change_time=created, delete_time=deleted,
        created_by="owner@acme.example")


def event(con, wid: str, ws: str, kind: str, t: datetime, clusters: int) -> None:
    put(con, "compute__warehouse_events", account_id=ACCOUNT, workspace_id=ws, warehouse_id=wid,
        event_type=kind, cluster_count=clusters, event_time=t)


def query(con, sid: str, wid: str, ws: str, start: datetime, end: datetime, *, status: str = "FINISHED",
          tags: dict | None = None, dashboard: str | None = None, **extra) -> None:
    ms = int((end - start).total_seconds() * 1000)
    row = dict(account_id=ACCOUNT, workspace_id=ws, statement_id=sid, executed_by="analyst@acme.example",
               execution_status=status, compute={"type": "WAREHOUSE", "cluster_id": None, "warehouse_id": wid},
               statement_text="SELECT 1", statement_type="SELECT", error_message="boom" if status == "FAILED" else None,
               client_application="Databricks SQL Dashboard", total_duration_ms=ms, waiting_for_compute_duration_ms=0,
               waiting_at_capacity_duration_ms=0, execution_duration_ms=ms, total_task_duration_ms=ms,
               start_time=start, end_time=end, update_time=end, read_bytes=10**9, read_files=10, pruned_files=0,
               spilled_local_bytes=0, shuffle_read_bytes=0, from_result_cache=False,
               query_source={"dashboard_id": dashboard} if dashboard else None, query_tags=tags)
    row.update(extra)
    put(con, "query__history", **row)


def build(con) -> dict:
    """Writes every scenario; returns the expected numbers the test checks."""
    exp: dict = {}
    for sku, rate in RATES.items():
        put(con, "billing__list_prices", account_id=ACCOUNT, price_start_time=datetime(2020, 1, 1), sku_name=sku,
            cloud="aws", currency_code="USD", usage_unit=UNITS.get(sku, "DBU"),
            pricing={"default": rate, "promotional": {"default": None}, "effective_list": {"default": rate}})
    for ws, name, created in ((PROD, "acme-prod", DT(400)), (DEV, "acme-dev", DT(400)), (OLD, "acme-old", DT(400))):
        put(con, "access__workspaces_latest", account_id=ACCOUNT, workspace_id=ws, workspace_name=name,
            workspace_url=f"https://{name}.example", create_time=created, status="RUNNING")

    # ac_wh_bi: per period RUNNING h:00:05, query h:00:10-h:00:15, STOPPING h:10:15 -> 610 s running,
    # 5 s busy, a 5 s start gap (under the 60 s limit) and a 600 s tail. On D(3) Databricks stopped
    # it late three times, 25:16, 17:12 and 15:36 after the query, as seen on a live account.
    warehouse(con, "ac_wh_bi", PROD, "SERVERLESS", "SMALL", 10, DT(60), tags={"cost_center": "bi"})
    bi_dbus, tails = 0.0, []
    late = {(3, 9): 1516, (3, 12): 1032, (3, 15): 936}
    for d in range(1, 8):
        for h in range(8, 18):
            tail = late.get((d, h), 600)
            tails.append(tail)
            stop = DT(d, h, 0, 15) + timedelta(seconds=tail)
            event(con, "ac_wh_bi", PROD, "STARTING", DT(d, h), 1)
            event(con, "ac_wh_bi", PROD, "RUNNING", DT(d, h, 0, 5), 1)
            query(con, f"ac_q_bi_{d}_{h}", "ac_wh_bi", PROD, DT(d, h, 0, 10), DT(d, h, 0, 15), tags={"cost_center": "bi"},
                  dashboard="ac_dash_sales")
            event(con, "ac_wh_bi", PROD, "STOPPING", stop, 0)
            event(con, "ac_wh_bi", PROD, "STOPPED", stop + timedelta(seconds=30), 0)
            bi_dbus += bill(con, f"ac_b_bi_{d}_{h}", PROD, SKU_SERVERLESS, DT(d, h), (15 + tail) / 3600, 12,
                            tags={"cost_center": "bi"}, serverless=True, warehouse_id="ac_wh_bi")
    exp["bi"] = {"running_min": sum(10 + t for t in tails) / 60, "counted_min": sum(tails) / 60, "tail_min": sum(tails) / 60,
                 "busy_min": 70 * 5 / 60, "dbus": bi_dbus, "usd": bi_dbus * 0.70,
                 "wasted_usd": bi_dbus * 0.70 * sum(tails) / sum(15 + t for t in tails),
                 "queries": 70, "late_stops": 3, "late_extra_min": (916 + 432 + 336) / 60, "longest_wait_min": 1516 / 60}

    # ac_wh_etl: RUNNING 01:00:00, loads 01:00:30-02:00:00 and 02:00:20-03:00:00, STOPPING 03:05:00 ->
    # 7500 s running, 7150 s busy, 30 s and 20 s gaps (not counted), a 300 s tail.
    warehouse(con, "ac_wh_etl", PROD, "PRO", "MEDIUM", 5, DT(60), tags={"cost_center": "data"})
    etl_dbus = 0.0
    for d in range(1, 8):
        event(con, "ac_wh_etl", PROD, "STARTING", DT(d, 1), 1)
        event(con, "ac_wh_etl", PROD, "RUNNING", DT(d, 1), 1)
        query(con, f"ac_q_etl_{d}_a", "ac_wh_etl", PROD, DT(d, 1, 0, 30), DT(d, 2))
        query(con, f"ac_q_etl_{d}_b", "ac_wh_etl", PROD, DT(d, 2, 0, 20), DT(d, 3), status="FAILED" if d == 1 else "FINISHED")
        event(con, "ac_wh_etl", PROD, "STOPPING", DT(d, 3, 5), 0)
        event(con, "ac_wh_etl", PROD, "STOPPED", DT(d, 3, 5, 30), 0)
        for h, hours in ((1, 1.0), (2, 1.0), (3, 5 / 60)):
            etl_dbus += bill(con, f"ac_b_etl_{d}_{h}", PROD, SKU_PRO, DT(d, h), hours, 24,
                             tags={"cost_center": "data"}, warehouse_id="ac_wh_etl")
    exp["etl"] = {"running_min": 7 * 7500 / 60, "counted_min": 7 * 300 / 60, "busy_min": 7 * 7150 / 60,
                  "dbus": etl_dbus, "failed": 1, "queries": 14}

    # ac_wh_alwayson: RUNNING since D(40), never stopped, one 5 s query on D(10) 12:00, billed every
    # hour of the last 30 days.
    warehouse(con, "ac_wh_alwayson", DEV, "PRO", "X_SMALL", 0, DT(60))
    event(con, "ac_wh_alwayson", DEV, "STARTING", DT(40, 9), 1)
    event(con, "ac_wh_alwayson", DEV, "RUNNING", DT(40, 9), 1)
    query(con, "ac_q_on_1", "ac_wh_alwayson", DEV, DT(10, 12), DT(10, 12, 0, 5))
    on_dbus = sum(bill(con, f"ac_b_on_{d}_{h}", DEV, SKU_PRO, DT(d, h), 1.0, 6, warehouse_id="ac_wh_alwayson")
                  for d in range(1, 31) for h in range(24))
    exp["alwayson"] = {"running_min_7d": 7 * 24 * 60, "dbus": on_dbus}

    warehouse(con, "ac_wh_unused", PROD, "PRO", "SMALL", 10, DT(200))
    warehouse(con, "ac_wh_new", PROD, "SERVERLESS", "SMALL", 10, DT(5))
    warehouse(con, "ac_wh_gone", DEV, "PRO", "SMALL", 10, DT(80), deleted=DT(20))

    # Clusters.
    for cid, ws, src, mode, policy, term, dbr in (
        ("ac_cl_shared", PROD, "UI", "NONE", None, 0, "15.4.x-scala2.12"),
        ("ac_cl_team", DEV, "UI", "USER_ISOLATION", "ac_policy", 30, "15.4.x-scala2.12"),
        ("ac_cl_job", PROD, "JOB", "SINGLE_USER", "ac_policy", 0, "15.4.x-scala2.12"),
    ):
        put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id=cid, cluster_name=cid,
            owned_by="owner@acme.example", create_time=DT(300), delete_time=None, driver_node_type="m5.xlarge",
            worker_node_type="m5.xlarge", worker_count=2, auto_termination_minutes=term, tags={},
            cluster_source=src, dbr_version=dbr, change_time=DT(300), change_date=D(300),
            data_security_mode=mode, policy_id=policy)
    bill(con, "ac_b_cl_shared", PROD, SKU_ALL_PURPOSE, DT(100, 10), 1.0, 4, product="ALL_PURPOSE", cluster_id="ac_cl_shared")
    bill(con, "ac_b_cl_team", DEV, SKU_ALL_PURPOSE, DT(2, 10), 1.0, 4, tags={"team": "platform"},
         product="ALL_PURPOSE", cluster_id="ac_cl_team")
    bill(con, "ac_b_old", OLD, SKU_ALL_PURPOSE, DT(120, 10), 1.0, 4, product="ALL_PURPOSE")

    # Jobs: one run per day at 02:00 for 30 minutes.
    def job(jid: str, results: list[str]) -> None:
        put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=PROD, job_id=jid, name=jid, creator_id="1",
            change_time=DT(200), delete_time=None, create_time=DT(200))
        for i, result in enumerate(results):
            d = len(results) - i
            put(con, "lakeflow__job_run_timeline", account_id=ACCOUNT, workspace_id=PROD, job_id=jid,
                run_id=f"{jid}_r{d}", period_start_time=DT(d, 2), period_end_time=DT(d, 2, 30),
                trigger_type="PERIODIC", result_state=result, run_type="JOB_RUN",
                termination_code="RUN_EXECUTION_ERROR" if result == "FAILED" else "SUCCESS",
                run_duration_seconds=1800, queue_duration_seconds=0)
    job("ac_job_ok", ["SUCCEEDED"] * 10)
    job("ac_job_flaky", ["FAILED", "SUCCEEDED", "SUCCEEDED", "FAILED", "SUCCEEDED", "SUCCEEDED",
                         "FAILED", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED"])
    job("ac_job_broken", ["SUCCEEDED"] * 5 + ["FAILED"] * 3)
    put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_badstart", name="ac_job_badstart",
        creator_id="1", change_time=DT(200), delete_time=None, create_time=DT(200))
    for d in (1, 2):
        put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_badstart",
            run_id=f"ac_job_badstart_t{d}", job_run_id=f"ac_job_badstart_r{d}", period_start_time=DT(d, 4),
            period_end_time=DT(d, 4, 3), task_key="load", result_state="FAILED", termination_code="CLOUD_FAILURE",
            setup_duration_seconds=180, execution_duration_seconds=0)
    exp["badstart"] = {"failed_starts": 2, "setup_s": 360}

    # Audit: a person in acme-prod on D(1) and acme-dev on D(3).
    for ws, d in ((PROD, 1), (DEV, 3)):
        put(con, "access__audit", account_id=ACCOUNT, workspace_id=ws, event_time=DT(d, 9), event_date=D(d),
            user_identity={"email": "analyst@acme.example", "subject_name": None}, service_name="sqlPermissions",
            action_name="getTable", audit_level="WORKSPACE_LEVEL", event_id=f"ac_ev_{ws}")
    _more(con, exp)
    return exp


def _more(con, exp: dict) -> None:
    """Jobs' cost and tasks, a long run, a pipeline, notebooks, egress, vector search, serving, the
    AI gateway, this app's own queries, query spill and pruning, and cluster node use."""
    # Job runs bill 4 DBUs each on a job cluster; ac_job_ok's single task has no time limit.
    jobs_usd = 0.0
    for jid, n in (("ac_job_ok", 10), ("ac_job_flaky", 10), ("ac_job_broken", 8)):
        for d in range(1, n + 1):
            jobs_usd += 0.15 * bill(con, f"ac_b_{jid}_{d}", PROD, SKU_JOBS, DT(d, 2), 0.5, 8, product="JOBS",
                                    job_id=jid, job_run_id=f"{jid}_r{d}", cluster_id="ac_cl_job")
        put(con, "lakeflow__job_tasks", account_id=ACCOUNT, workspace_id=PROD, job_id=jid, task_key="main",
            change_time=DT(200), timeout_seconds=None if jid == "ac_job_ok" else 3600)
    exp["jobs"] = {"usd": jobs_usd}
    for d in range(1, 11):
        put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_ok",
            run_id=f"ac_job_ok_t{d}", job_run_id=f"ac_job_ok_r{d}", period_start_time=DT(d, 2),
            period_end_time=DT(d, 2, 30), task_key="main", result_state="SUCCEEDED", termination_code="SUCCESS",
            compute_ids=["ac_cl_job"], compute=[{"type": "JOBS_COMPUTE", "cluster_id": "ac_cl_job", "warehouse_id": None}],
            setup_duration_seconds=60, execution_duration_seconds=1740)
        for minute in range(30):
            for inst, drv, cpu in (("ac_i_job_d", True, 20.0), ("ac_i_job_w1", False, 4.0), ("ac_i_job_w2", False, 3.0)):
                t = DT(d, 2, minute)
                put(con, "compute__node_timeline", account_id=ACCOUNT, workspace_id=PROD, cluster_id="ac_cl_job",
                    instance_id=f"{inst}_{d}", start_time=t, end_time=t + timedelta(minutes=1), driver=drv,
                    cpu_user_percent=cpu, cpu_system_percent=1.0, cpu_wait_percent=0.0, mem_used_percent=30.0,
                    mem_swap_percent=0.0, node_type="m5.xlarge")

    # ac_job_long: one 12-hour run on D(2), one timeline row per hour, the result on the last.
    put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_long", name="ac_job_long",
        creator_id="1", change_time=DT(200), delete_time=None, create_time=DT(200))
    for h in range(12):
        last = h == 11
        put(con, "lakeflow__job_run_timeline", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_long",
            run_id="ac_job_long_r1", period_start_time=DT(2, 1 + h), period_end_time=DT(2, 2 + h),
            trigger_type="PERIODIC", run_type="JOB_RUN", result_state="SUCCEEDED" if last else None,
            termination_code="SUCCESS" if last else None, run_duration_seconds=43200 if last else None)
    exp["long"] = {"runs": 1, "hours": 12}

    # Pipeline ac_pl_sales: six 20-minute updates, two failed, billed 2 DBUs each.
    put(con, "lakeflow__pipelines", account_id=ACCOUNT, workspace_id=PROD, pipeline_id="ac_pl_sales", pipeline_type="ETL",
        name="ac_pl_sales", created_by="owner@acme.example", tags={},
        settings={"photon": True, "development": False, "continuous": False, "serverless": False,
                  "edition": "ADVANCED", "channel": "CURRENT"}, change_time=DT(100), create_time=DT(100))
    for d in range(1, 7):
        put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=PROD, pipeline_id="ac_pl_sales",
            update_id=f"ac_pu_{d}", update_type="DEFAULT", trigger_type="JOB_TASK",
            result_state="FAILED" if d in (2, 4) else "COMPLETED", period_start_time=DT(d, 5), period_end_time=DT(d, 5, 20))
        bill(con, f"ac_b_pl_{d}", PROD, SKU_DLT, DT(d, 5), 1.0, 2, product="DLT", dlt_pipeline_id="ac_pl_sales",
             dlt_update_id=f"ac_pu_{d}")
    exp["pipeline"] = {"updates": 6, "failed": 2, "dbus": 12}

    # Serverless notebook, egress, vector search, and a workspace Databricks no longer lists.
    for d in (1, 2, 3):
        bill(con, f"ac_b_nb_{d}", DEV, SKU_NOTEBOOK, DT(d, 14), 1.0, 2, product="INTERACTIVE", serverless=True,
             notebook_id="ac_nb_explore", notebook_path="/Users/analyst/explore")
        bill(con, f"ac_b_eg_{d}", PROD, SKU_EGRESS, DT(d, 15), 1.0, 50, product="NETWORKING", unit="GB",
             usage_type="NETWORK_BYTE", destination_region="eu-west-1")
        bill(con, f"ac_b_vs_{d}", PROD, SKU_VECTOR, DT(d, 16), 1.0, 4, product="VECTOR_SEARCH", serverless=True,
             endpoint_id="ac_vs_docs", endpoint_name="ac-vs-docs")
    bill(con, "ac_b_unnamed", "2999", SKU_JOBS, DT(3, 9), 1.0, 5, product="JOBS")

    # Model serving: a chat endpoint with traffic and a custom endpoint billed with none.
    for eid, name, etype in (("ac_ep_chat", "ac-chat", "FOUNDATION_MODEL"), ("ac_ep_custom", "ac-custom", "CUSTOM_MODEL")):
        put(con, "serving__served_entities", served_entity_id=f"{eid}_e", account_id=ACCOUNT, workspace_id=PROD,
            created_by="owner@acme.example", endpoint_name=name, endpoint_id=eid, served_entity_name=f"{name}-1",
            entity_type=etype, entity_name=f"acme.ml.{name}", entity_version="1", endpoint_config_version=1,
            task="llm/v1/chat" if etype == "FOUNDATION_MODEL" else None, change_time=DT(60))
    for d in (1, 2, 3):
        for i in range(30):
            put(con, "serving__endpoint_usage", workspace_id=PROD, account_id=ACCOUNT, client_request_id=f"ac_r_{d}_{i}",
                databricks_request_id=f"ac_dr_{d}_{i}", requester="app@acme.example", status_code=200 if i else 500,
                request_time=DT(d, 10, i), input_token_count=500, output_token_count=200, served_entity_id="ac_ep_chat_e")
        # pay-per-token serving bills DBUs, with usage_type TOKEN: 21,000 tokens a day, 300 DBU
        bill(con, f"ac_b_tok_{d}", PROD, SKU_TOKENS, DT(d, 10), 1.0, 300, product="MODEL_SERVING",
             usage_type="TOKEN", serverless=True, features={"serving_type": "FOUNDATION_MODEL"},
             endpoint_id="ac_ep_chat", endpoint_name="ac-chat")
        for h in range(24):
            bill(con, f"ac_b_cus_{d}_{h}", PROD, SKU_SERVING, DT(d, h), 1.0, 10, product="MODEL_SERVING",
                 usage_type="GPU_TIME", serverless=True, features={"serving_type": "MODEL"},
                 endpoint_id="ac_ep_custom", endpoint_name="ac-custom")
        for i in range(10):
            put(con, "ai_gateway__usage", account_id=ACCOUNT, workspace_id=PROD, request_id=f"ac_gw_{d}_{i}",
                endpoint_id="ac_ep_chat", endpoint_name="ac-chat", event_time=DT(d, 11, i), latency_ms=800,
                destination_type="FOUNDATION_MODEL", destination_name="ac-chat", requester="app@acme.example",
                requester_type="USER", input_tokens=500, output_tokens=200, total_tokens=700,
                status_code=429 if i == 9 else 200)

    # This app's own export queries on their own small warehouse.
    warehouse(con, "ac_wh_audit", PROD, "PRO", "X_SMALL", 5, DT(60))
    event(con, "ac_wh_audit", PROD, "STARTING", DT(1, 6), 1)
    event(con, "ac_wh_audit", PROD, "RUNNING", DT(1, 6), 1)
    for i in range(3):
        query(con, f"ac_q_audit_{i}", "ac_wh_audit", PROD, DT(1, 6, i * 3), DT(1, 6, i * 3 + 2),
              tags={"audit_app": "export"}, client_application="databricks-audit-app")
    event(con, "ac_wh_audit", PROD, "STOPPING", DT(1, 6, 13), 0)
    event(con, "ac_wh_audit", PROD, "STOPPED", DT(1, 6, 13, 30), 0)
    bill(con, "ac_b_audit", PROD, SKU_PRO, DT(1, 6), 13 / 60, 6, warehouse_id="ac_wh_audit")

    # A heavy MERGE on ac_wh_etl's D(2) load: spills, shuffles and reads every file.
    con.execute("UPDATE query__history SET statement_type = 'MERGE', spilled_local_bytes = 60000000000, "
                "shuffle_read_bytes = 120000000000, read_bytes = 400000000000, read_files = 5000, pruned_files = 0, "
                "written_bytes = 50000000000 WHERE statement_id = 'ac_q_etl_2_a'")
    con.execute("UPDATE query__history SET read_files = 10, pruned_files = 990 WHERE statement_id LIKE 'ac_q_bi_%'")

    # ac_cl_team on D(2) 10:00-11:00: two workers idle for 40 minutes, then busy.
    put(con, "compute__node_types", account_id=ACCOUNT, node_type="m5.xlarge", core_count=4.0, memory_mb=16384, gpu_count=0)
    for minute in range(60):
        t = DT(2, 10, minute)
        for inst, drv in (("ac_i_team_d", True), ("ac_i_team_w1", False), ("ac_i_team_w2", False)):
            cpu = 10.0 if drv else (2.0 if minute < 40 else 70.0)
            put(con, "compute__node_timeline", account_id=ACCOUNT, workspace_id=DEV, cluster_id="ac_cl_team",
                instance_id=inst, start_time=t, end_time=t + timedelta(minutes=1), driver=drv, cpu_user_percent=cpu,
                cpu_system_percent=1.0, cpu_wait_percent=0.0, mem_used_percent=40.0, mem_swap_percent=0.0,
                node_type="m5.xlarge")
    for inst in ("ac_i_team_w1", "ac_i_team_w2"):
        for kind, t in (("STARTED", DT(2, 10)), ("TERMINATED", DT(2, 11))):
            put(con, "compute__instance_events", account_id=ACCOUNT, workspace_id=DEV, instance_id=inst, event_time=t,
                event_type=kind, cluster_id="ac_cl_team", node_type="m5.xlarge",
                state="RUNNING" if kind == "STARTED" else "TERMINATED", availability_type="ON_DEMAND")
    _long_task(con, exp)
    _governance(con, exp)


def _long_task(con, exp: dict) -> None:
    """ac_job_long's one task: 12 hours on its own job cluster (workers 85% busy), running SQL on
    ac_wh_jobs where one statement shape takes 9 of the 12 hours."""
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=PROD, cluster_id="ac_cl_job2", cluster_name="ac_cl_job2",
        owned_by="owner@acme.example", create_time=DT(3), worker_count=2, auto_termination_minutes=0, tags={},
        cluster_source="JOB", dbr_version="15.4.x-scala2.12", change_time=DT(3), change_date=D(3),
        data_security_mode="SINGLE_USER", policy_id="ac_policy", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")
    for h in range(12):
        last = h == 11
        put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_long",
            run_id="ac_job_long_t1", job_run_id="ac_job_long_r1", period_start_time=DT(2, 1 + h),
            period_end_time=DT(2, 2 + h), task_key="transform", result_state="SUCCEEDED" if last else None,
            termination_code="SUCCESS" if last else None, compute_ids=["ac_cl_job2"],
            compute=[{"type": "JOBS_COMPUTE", "cluster_id": "ac_cl_job2", "warehouse_id": None}],
            setup_duration_seconds=120 if last else None, execution_duration_seconds=43080 if last else None)
    for minute in range(12 * 60):
        t = DT(2, 1) + timedelta(minutes=minute)
        for inst, drv, cpu in (("ac_i_long_d", True, 15.0), ("ac_i_long_w1", False, 85.0), ("ac_i_long_w2", False, 84.0)):
            put(con, "compute__node_timeline", account_id=ACCOUNT, workspace_id=PROD, cluster_id="ac_cl_job2", instance_id=inst,
                start_time=t, end_time=t + timedelta(minutes=1), driver=drv, cpu_user_percent=cpu, cpu_system_percent=2.0,
                cpu_wait_percent=1.0, mem_used_percent=60.0, mem_swap_percent=0.0, node_type="m5.xlarge")
    warehouse(con, "ac_wh_jobs", PROD, "PRO", "SMALL", 10, DT(60))
    event(con, "ac_wh_jobs", PROD, "STARTING", DT(2, 1), 1)
    event(con, "ac_wh_jobs", PROD, "RUNNING", DT(2, 1), 1)
    src = {"job_info": {"job_id": "ac_job_long", "job_run_id": None, "job_task_run_id": "ac_job_long_t1"}}
    for sid, start, end, text in (
        ("ac_q_long_1", DT(2, 1, 10), DT(2, 6, 10), "INSERT INTO acme.sales.daily SELECT * FROM acme.sales.orders WHERE batch = 1"),
        ("ac_q_long_2", DT(2, 6, 20), DT(2, 10, 20), "INSERT INTO acme.sales.daily SELECT * FROM acme.sales.orders WHERE batch = 2"),
        ("ac_q_long_3", DT(2, 10, 30), DT(2, 11), "OPTIMIZE acme.sales.daily"),
    ):
        query(con, sid, "ac_wh_jobs", PROD, start, end, statement_text=text, statement_type="INSERT",
              client_application="Databricks Jobs", query_source=src)
    event(con, "ac_wh_jobs", PROD, "STOPPING", DT(2, 11, 10), 0)
    event(con, "ac_wh_jobs", PROD, "STOPPED", DT(2, 11, 10, 30), 0)
    for h in range(1, 12):
        bill(con, f"ac_b_jobs_{h}", PROD, SKU_PRO, DT(2, h), 1.0 if h < 11 else 10 / 60, 12, warehouse_id="ac_wh_jobs")
    exp["long_task"] = {"insert_share_pct": 9 / 12 * 100}


def _governance(con, exp: dict) -> None:
    """Unity Catalog tables, grants, tags, masks, views and volumes; lineage; audit events (admin
    changes, logins, run-as, vector search); outbound denials; predictive optimization; sizes."""
    tables = (  # (schema, name, type, last altered, active GB)
        ("sales", "orders", "MANAGED", DT(1), 120),
        ("sales", "customers", "MANAGED", DT(5), 8),
        ("sales", "daily", "MANAGED", DT(2), 3),
        ("raw", "events_old", "EXTERNAL", DT(200), 500),
        ("sales", "v_orders", "VIEW", DT(30), 0),
    )
    for sch, name, ttype, altered, gb in tables:
        put(con, "information_schema__tables", table_catalog="acme", table_schema=sch, table_name=name, table_type=ttype,
            table_owner="data_eng", created=DT(400), created_by="owner@acme.example", last_altered=altered,
            last_altered_by="owner@acme.example", data_source_format=None if ttype == "VIEW" else "DELTA")
        if ttype != "VIEW":
            put(con, "storage__table_metrics_history", account_id=ACCOUNT, catalog_name="acme", schema_name=sch,
                table_name=name, table_id=f"ac_t_{name}", table_type=ttype, table_owner="data_eng",
                table_creation_time=DT(400), snapshot_date=D(1), active_bytes=gb * 10**9, active_files=gb * 10,
                predictive_optimization_enabled=ttype == "MANAGED")
    put(con, "information_schema__views", table_catalog="acme", table_schema="sales", table_name="v_orders",
        view_definition="SELECT * FROM acme.sales.orders", is_materialized="NO")
    put(con, "information_schema__volumes", volume_catalog="acme", volume_schema="raw", volume_name="landing",
        volume_type="MANAGED", volume_owner="data_eng", created=DT(300), created_by="owner@acme.example", last_altered=DT(10))
    put(con, "information_schema__volume_tags", catalog_name="acme", schema_name="raw", volume_name="landing",
        tag_name="pii", tag_value="contains customer exports")
    put(con, "information_schema__catalog_privileges", grantor="owner@acme.example", grantee="data_eng",
        catalog_name="acme", privilege_type="USE CATALOG", is_grantable="NO")
    for grantee, table, priv in (("account users", "customers", "SELECT"), ("account users", "orders", "MODIFY"),
                                 ("data_eng", "orders", "ALL PRIVILEGES"), ("analyst@acme.example", "daily", "SELECT")):
        put(con, "information_schema__table_privileges", grantor="owner@acme.example", grantee=grantee, table_catalog="acme",
            table_schema="sales", table_name=table, privilege_type=priv, is_grantable="NO")
    put(con, "information_schema__table_tags", catalog_name="acme", schema_name="sales", table_name="customers",
        tag_name="classification", tag_value="pii")
    put(con, "information_schema__column_tags", catalog_name="acme", schema_name="sales", table_name="customers",
        column_name="email", tag_name="pii", tag_value="email")
    put(con, "information_schema__column_masks", table_catalog="acme", table_schema="sales", table_name="customers",
        column_name="email", mask_name="acme.sec.mask_email")

    # Lineage: orders feeds daily (the job, daily) and the dashboard reads daily and customers; events_old
    # is never read.
    for d in range(1, 8):
        for i, (src_t, tgt_t, etype, eid, who) in enumerate((
            ("acme.sales.orders", "acme.sales.daily", "JOB", "ac_job_ok", "svc@acme.example"),
            ("acme.sales.daily", None, "DASHBOARD", "ac_dash_sales", "analyst@acme.example"),
            ("acme.sales.customers", None, "DASHBOARD", "ac_dash_sales", "analyst@acme.example"),
            ("acme.sales.customers", None, "NOTEBOOK", "ac_nb_explore", "intern@acme.example"),
        )):
            s_cat, s_sch, s_name = src_t.split(".")
            tgt = tgt_t.split(".") if tgt_t else (None, None, None)
            put(con, "access__table_lineage", account_id=ACCOUNT, workspace_id=PROD, entity_type=etype, entity_id=eid,
                source_table_full_name=src_t, source_table_catalog=s_cat, source_table_schema=s_sch, source_table_name=s_name,
                source_type="TABLE", target_table_full_name=tgt_t, target_table_catalog=tgt[0], target_table_schema=tgt[1],
                target_table_name=tgt[2], target_type="TABLE" if tgt_t else None, created_by=who, event_time=DT(d, 9, i),
                event_date=D(d), record_id=f"ac_l_{d}_{i}", event_id=f"ac_le_{d}_{i}", direct_access=etype == "NOTEBOOK")

    # Audit: an admin grant and a group change in prod, logins (two failed), a job run as a service
    # principal, vector search queries.
    sp = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    def audit(eid, ws, t, service, action, *, email="admin@acme.example", status=200, params=None, run_by=None,
              run_as=None, ip="10.0.0.5"):
        put(con, "access__audit", account_id=ACCOUNT, workspace_id=ws, event_time=t, event_date=t.date(),
            source_ip_address=ip, user_identity={"email": email, "subject_name": email}, service_name=service,
            action_name=action, request_params=params or {}, response={"status_code": status, "error_message": None, "result": None},
            audit_level="WORKSPACE_LEVEL", event_id=eid,
            identity_metadata={"run_by": run_by, "run_as": run_as, "acting_resource": None,
                               "run_by_display_name": None, "run_as_display_name": None})
    audit("ac_a_admin", PROD, DT(3, 11), "accounts", "setAdmin", params={"targetUserName": "intern@acme.example"})
    audit("ac_a_group", PROD, DT(3, 11, 5), "accounts", "addPrincipalToGroup", params={"targetGroupName": "admins"})
    for i, status in enumerate((200, 401, 401, 200)):
        audit(f"ac_a_login_{i}", PROD, DT(1, 8, i), "accounts", "login", email="analyst@acme.example", status=status,
              ip="203.0.113.7")
    audit("ac_a_runas", PROD, DT(1, 2), "jobs", "runNow", email="svc@acme.example", run_by="svc@acme.example", run_as=sp)
    for i in range(5):
        audit(f"ac_a_vs_{i}", PROD, DT(1, 16, i), "vectorSearch", "queryVectorIndex", email="app@acme.example",
              params={"name": "acme.docs.chunks_index", "endpoint_name": "ac-vs-docs"})
    # A service principal signing in with OAuth every hour: sign-ins, not admin changes.
    for i in range(150):
        audit(f"ac_a_mint_{i}", PROD, DT(1 + i // 24, i % 24), "workspace", "mintOAuthToken", email=sp)
    # A network policy in dry-run logs denials without blocking anything.
    for i in range(120):
        put(con, "access__outbound_network", account_id=ACCOUNT, workspace_id=PROD, destination_type="DNS",
            destination="api.partner.example", event_time=DT(2, 10, i % 60), access_type="DRY_RUN_DENIAL",
            event_id=f"ac_o_dry_{i}", network_source_type="SERVERLESS")
    for i in range(3):
        put(con, "access__outbound_network", account_id=ACCOUNT, workspace_id=PROD, destination_type="DNS",
            destination="pastebin.example", event_time=DT(1, 13, i), access_type="DROP", event_id=f"ac_o_{i}",
            network_source_type="SERVERLESS")

    # Predictive optimization on orders: compaction, clustering and a vacuum that deleted nothing.
    for op, metrics, dbus in (
        ("COMPACTION", {"number_of_compacted_files": "400", "amount_of_data_compacted_bytes": str(40 * 10**9)}, 6.0),
        ("CLUSTERING", {"number_of_clustered_files": "300", "amount_of_clustered_data_bytes": str(30 * 10**9),
                        "number_of_removed_files": "300", "amount_of_data_removed_bytes": str(30 * 10**9)}, 9.0),
        ("VACUUM", {"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"}, 2.0),
    ):
        put(con, "storage__predictive_optimization_operations_history", account_id=ACCOUNT, workspace_id=PROD,
            start_time=DT(2, 3), end_time=DT(2, 3, 30), metastore_name="acme", metastore_id="ac_ms", catalog_name="acme",
            schema_name="sales", table_name="orders", table_id="ac_t_orders", operation_type=op, operation_id=f"ac_po_{op}",
            operation_status="SUCCESSFUL", operation_metrics=metrics, usage_unit="ESTIMATED_DBU", usage_quantity=dbus)
    exp["po"] = {"dbus": 17.0}
    _rest(con, exp)
    _review_jobs(con, exp)
    _review_perf(con, exp)
    _review_compute(con, exp)
    _review_cost(con, exp)
    _review_storage(con)
    _review_tags(con, exp)
    _baseline(con, exp)
    # A recent account records health_rules on every job, as an empty list when it has none;
    # ac_job_badstart keeps the older empty column.
    con.execute("UPDATE lakeflow__jobs SET health_rules = [] WHERE health_rules IS NULL AND job_id <> 'ac_job_badstart'")
    _attributed(con)


def _baseline(con, exp: dict) -> None:
    """acme-analytics: a well-run workspace with 13 months of history, so trends, months and
    period changes have something to compare. Every resource carries the mandatory tags. The bill
    goes back 395 days; the other tables 90, as a real export. A BI warehouse runs office hours
    (resized SMALL to MEDIUM 180 days ago), a nightly Photon ETL job (one run repaired, one timed
    out), a serverless pipeline, notebooks under a usage policy, Genie in the browser and from an
    agent, a CPU serving endpoint, and a one-off backfill 45 days ago."""
    ws, tags = ANALYTICS, {"cost_center": "analytics", "domain": "sales", "environment": "prod"}
    sp = "5d6e7f80-1a2b-4c3d-8e9f-0a1b2c3d4e5f"  # the jobs' service principal
    put(con, "access__workspaces_latest", account_id=ACCOUNT, workspace_id=ws, workspace_name="acme-analytics",
        workspace_url="https://acme-analytics.example", create_time=DT(500), status="RUNNING")

    def weekday(n: int) -> bool:
        return D(n).weekday() < 5

    # BI warehouse: weekdays 09:00-17:10 (auto-stop 10 min after the 17:00 query), queries every 5 min.
    # Its tables are clustered (95% of files skipped) and mostly read from the disk cache; on Monday
    # mornings 10:00-11:00 the one cluster is full and statements wait 30 s for a slot.
    warehouse(con, "ac_wh_analytics", ws, "PRO", "SMALL", 10, DT(400), tags=tags)
    warehouse(con, "ac_wh_analytics", ws, "PRO", "MEDIUM", 10, DT(180), tags=tags)
    shapes = (("SELECT region, sum(amount) FROM sales.orders WHERE order_date >= '2026-01-01' GROUP BY region",
               {"dashboard_id": "ac_dash_exec"}, "Databricks SQL Dashboard"),
              ("SELECT * FROM sales.customers WHERE customer_id = 4711", {"genie_space_id": "ac_genie_sales"}, "Genie"),
              ("INSERT INTO sales.daily_kpis SELECT * FROM sales.kpi_view",
               {"job_info": {"job_id": "ac_job_kpis", "job_run_id": None, "job_task_run_id": None}}, "Databricks SQL"),
              ("SELECT count(*) FROM sales.orders o JOIN sales.customers c USING (customer_id)", None,
               "Databricks SQL Editor"))
    wh_usd, fetch_7d = 0.0, 0
    for n in range(1, 396):
        if not weekday(n):
            continue
        dbu_h = 24 if n <= 180 else 12
        for h in range(9, 17):
            wh_usd += 0.55 * bill(con, f"ac_b_an_wh_{n}_{h}", ws, SKU_PRO, DT(n, h), 1.0, dbu_h, tags=tags,
                                  warehouse_id="ac_wh_analytics")
        wh_usd += 0.55 * bill(con, f"ac_b_an_wh_{n}_17", ws, SKU_PRO, DT(n, 17), 10 / 60, dbu_h, tags=tags,
                              warehouse_id="ac_wh_analytics")
        if n > 90:
            continue
        event(con, "ac_wh_analytics", ws, "STARTING", DT(n, 8, 59, 30), 1)
        event(con, "ac_wh_analytics", ws, "RUNNING", DT(n, 9), 1)
        event(con, "ac_wh_analytics", ws, "STOPPING", DT(n, 17, 10), 0)
        event(con, "ac_wh_analytics", ws, "STOPPED", DT(n, 17, 10, 30), 0)
        for i in range(97):
            start = DT(n, 9) + timedelta(minutes=5 * i)
            text, source, client = shapes[i % 4]
            is_job = source is not None and "job_info" in source
            cached, failed = i % 10 == 3, (n * 97 + i) % 89 == 0
            end = start + timedelta(seconds=1 if cached else (30 if failed else 240))
            queued = D(n).weekday() == 0 and 12 <= i < 24 and not cached
            # clients fetch results after execution: the SQL editor pulls a large result slowly
            fetch_ms = 0 if failed else 50 if cached else 30000 if client == "Databricks SQL Editor" else 1500
            fetch_7d += fetch_ms if n <= 7 else 0
            query(con, f"ac_q_an_{n}_{i}", "ac_wh_analytics", ws, start, end, status="FAILED" if failed else "FINISHED",
                  tags={"cost_center": "analytics"} if is_job else None, statement_text=text, query_source=source,
                  statement_type=text.split()[0], client_application=client, from_result_cache=cached,
                  executed_by=sp if is_job else f"analyst{i % 3}@acme.example",
                  error_message="[TABLE_OR_VIEW_NOT_FOUND] sales.kpi_view" if failed else None,
                  read_files=20, pruned_files=380, read_io_cache_percent=70,
                  waiting_at_capacity_duration_ms=30000 if queued else 0, result_fetch_duration_ms=fetch_ms)
    exp["analytics_wh"] = {"usd": wh_usd, "fetch_s_7d": fetch_7d / 1000}

    # Nightly ETL: 02:00-03:00 on its own Photon job cluster, two tasks; D(40) failed and was
    # repaired at 08:00, D(12) timed out.
    put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_etl_daily", name="etl-daily",
        creator_id="1", tags=tags, run_as=sp, change_time=DT(400), delete_time=None, create_time=DT(400))
    for key, timeout in (("extract", 3600), ("transform", 7200)):
        put(con, "lakeflow__job_tasks", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_etl_daily", task_key=key,
            change_time=DT(400), timeout_seconds=timeout)
    codes = {"SUCCEEDED": "SUCCESS", "FAILED": "RUN_EXECUTION_ERROR", "TIMED_OUT": "RUN_EXECUTION_ERROR"}
    for n in range(1, 396):
        bill(con, f"ac_b_an_etl_{n}", ws, SKU_JOBS, DT(n, 2), 1.0, 20, tags=tags, product="JOBS", run_as=sp,
             features={"is_photon": True}, job_id="ac_job_etl_daily", job_run_id=f"ac_etl_r{n}",
             cluster_id=f"ac_cl_etl_r{n}")
        if n > 90:
            continue
        put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id=f"ac_cl_etl_r{n}",
            cluster_name=f"job-etl-daily-run-{n}", owned_by=sp, create_time=DT(n, 1, 55), delete_time=DT(n, 3, 5),
            worker_count=4, auto_termination_minutes=0, tags=tags, cluster_source="JOB",
            dbr_version="15.4.x-photon-scala2.12", change_time=DT(n, 1, 55), change_date=D(n),
            data_security_mode="SINGLE_USER", policy_id="ac_policy_jobs", driver_node_type="m5.2xlarge",
            worker_node_type="m5.2xlarge")
        result = "TIMED_OUT" if n == 12 else ("FAILED" if n == 40 else "SUCCEEDED")
        put(con, "lakeflow__job_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_etl_daily",
            run_id=f"ac_etl_r{n}", period_start_time=DT(n, 2), period_end_time=DT(n, 3), trigger_type="PERIODIC",
            result_state=result, run_type="JOB_RUN", termination_code=codes[result], run_duration_seconds=3600,
            queue_duration_seconds=0, compute_ids=[f"ac_cl_etl_r{n}"])
        for key, m0, m1, res in (("extract", 0, 20, "SUCCEEDED"), ("transform", 20, 60, result)):
            setup = 90 if key == "extract" else 0
            put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_etl_daily",
                run_id=f"ac_etl_r{n}_{key}", job_run_id=f"ac_etl_r{n}", task_key=key, period_start_time=DT(n, 2, m0),
                period_end_time=DT(n, 2, m1) if m1 < 60 else DT(n, 3), result_state=res, termination_code=codes[res],
                compute_ids=[f"ac_cl_etl_r{n}"],
                compute=[{"type": "CLASSIC", "cluster_id": f"ac_cl_etl_r{n}", "warehouse_id": None}],
                setup_duration_seconds=setup, execution_duration_seconds=(m1 - m0) * 60 - setup)
    # the repair: a second end row on the same run, the failed task re-run
    put(con, "lakeflow__job_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_etl_daily",
        run_id="ac_etl_r40", period_start_time=DT(40, 8), period_end_time=DT(40, 8, 40), trigger_type="PERIODIC",
        result_state="SUCCEEDED", run_type="JOB_RUN", termination_code="SUCCESS", run_duration_seconds=2400,
        queue_duration_seconds=0, compute_ids=["ac_cl_etl_r40"])
    put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_etl_daily",
        run_id="ac_etl_r40_transform_2", job_run_id="ac_etl_r40", task_key="transform", period_start_time=DT(40, 8),
        period_end_time=DT(40, 8, 40), result_state="SUCCEEDED", termination_code="SUCCESS",
        compute_ids=["ac_cl_etl_r40"], compute=[{"type": "CLASSIC", "cluster_id": "ac_cl_etl_r40", "warehouse_id": None}],
        setup_duration_seconds=0, execution_duration_seconds=2400)
    bill(con, "ac_b_an_etl_40_repair", ws, SKU_JOBS, DT(40, 8), 40 / 60, 20, tags=tags, product="JOBS", run_as=sp,
         features={"is_photon": True}, job_id="ac_job_etl_daily", job_run_id="ac_etl_r40", cluster_id="ac_cl_etl_r40")

    # A one-off backfill on D(45): six hours on a large cluster.
    bill(con, "ac_b_an_backfill", ws, SKU_JOBS, DT(45, 1), 1.0, 6 * 40, tags=tags, product="JOBS", run_as=sp,
         job_id="ac_job_backfill", job_run_id="ac_backfill_r1", cluster_id="ac_cl_backfill")

    # Serverless pipeline: one 20-minute update a day.
    put(con, "lakeflow__pipelines", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_orders", pipeline_type="ETL",
        name="orders-pipeline", created_by="owner@acme.example", run_as=sp, tags=tags,
        settings={"photon": True, "development": False, "continuous": False, "serverless": True,
                  "edition": "ADVANCED", "channel": "CURRENT"}, change_time=DT(400), create_time=DT(400))
    for n in range(1, 396):
        bill(con, f"ac_b_an_pl_{n}", ws, SKU_DLT_SERVERLESS, DT(n, 3), 1.0, 6, tags=tags, product="DLT",
             serverless=True, run_as=sp, dlt_pipeline_id="ac_pl_orders", dlt_update_id=f"ac_an_pu_{n}")
        if n <= 90:
            put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_orders",
                update_id=f"ac_an_pu_{n}", update_type="DEFAULT", trigger_type="SCHEDULE", run_as_user_name=sp,
                result_state="COMPLETED", period_start_time=DT(n, 3), period_end_time=DT(n, 3, 20))

    # Data-science notebooks on weekdays, under the team's usage policy (its tags reach the bill); the
    # last 90 days also hold the statements they ran on serverless compute.
    for n in range(1, 396):
        if weekday(n):
            bill(con, f"ac_b_an_nb_{n}", ws, SKU_NOTEBOOK, DT(n, 10), 1.0, 4, tags=tags, product="INTERACTIVE",
                 serverless=True, run_as="data.scientist@acme.example", notebook_id="ac_nb_forecast",
                 notebook_path="/Users/data.scientist@acme.example/forecast", usage_policy_id="ac_up_ds")
            for i in range(5 if n <= 90 else 0):
                query(con, f"ac_q_nb_{n}_{i}", None, ws, DT(n, 10, 10 * i), DT(n, 10, 10 * i + 3),
                      statement_text="SELECT * FROM sales.orders WHERE order_date >= date_sub(current_date(), 365)",
                      compute={"type": "SERVERLESS_COMPUTE", "cluster_id": None, "warehouse_id": None},
                      query_source={"notebook_id": "ac_nb_forecast"}, client_application="Databricks Notebook",
                      executed_by="data.scientist@acme.example")

    # Genie: people in the browser on weekdays; for the last 60 days also an agent built into an app.
    for n in range(1, 396):
        if weekday(n):
            bill(con, f"ac_b_an_genie_ui_{n}", ws, SKU_SERVERLESS, DT(n, 11), 1.0, 2, tags=tags, product="GENIE",
                 serverless=True, run_as=f"analyst{n % 3}@acme.example",
                 genie={"agent_id": None, "channel": "UI", "surface": "GENIE_ONE"})
        if n <= 60:
            bill(con, f"ac_b_an_genie_api_{n}", ws, SKU_SERVERLESS, DT(n, 12), 1.0, 1, tags=tags, product="GENIE",
                 serverless=True, run_as=sp, genie={"agent_id": "ac_agent_sales", "channel": "API", "surface": "GENIE_CODE"})

    # CPU serving endpoint: billed all day, 20 requests a day.
    put(con, "serving__served_entities", served_entity_id="ac_ep_recs_e", account_id=ACCOUNT, workspace_id=ws,
        created_by="owner@acme.example", endpoint_name="ac-recs", endpoint_id="ac_ep_recs", served_entity_name="recs-3",
        entity_type="CUSTOM_MODEL", entity_name="acme.ml.recommender", entity_version="3", endpoint_config_version=1,
        custom_model_config={"min_concurrency": 4, "max_concurrency": 8, "compute_type": "CPU"}, change_time=DT(200))
    for n in range(1, 396):
        bill(con, f"ac_b_an_recs_{n}", ws, SKU_SERVING, DT(n, 0), 1.0, 48, tags=tags, product="MODEL_SERVING",
             serverless=True, features={"serving_type": "MODEL"}, endpoint_id="ac_ep_recs", endpoint_name="ac-recs")
        if n <= 90:
            for i in range(20):
                put(con, "serving__endpoint_usage", workspace_id=ws, account_id=ACCOUNT, client_request_id=f"ac_rr_{n}_{i}",
                    databricks_request_id=f"ac_drr_{n}_{i}", requester=sp, status_code=200,
                    request_time=DT(n, 8 + i // 2, i), input_character_count=400, output_character_count=80,
                    served_entity_id="ac_ep_recs_e")

    # Logins on weekdays, and the nightly job's own run events every day.
    for n in range(1, 91):
        if weekday(n):
            put(con, "access__audit", account_id=ACCOUNT, workspace_id=ws, event_time=DT(n, 8, 55), event_date=D(n),
                user_identity={"email": f"analyst{n % 3}@acme.example", "subject_name": None}, service_name="accounts",
                action_name="login", audit_level="WORKSPACE_LEVEL", event_id=f"ac_ev_an_{n}")
        put(con, "access__audit", account_id=ACCOUNT, workspace_id=ws, event_time=DT(n, 2), event_date=D(n),
            user_identity={"email": sp, "subject_name": None}, service_name="jobs", action_name="runStart",
            audit_level="WORKSPACE_LEVEL", event_id=f"ac_ev_an_job_{n}")


def _attributed(con) -> None:
    """system.billing.attributed_usage the way Databricks fills it: each warehouse statement gets
    its warehouse's DBU rate for the seconds it ran in each billed hour, scaled down when concurrent
    statements would add up to more than the hour's bill. Result-cache hits get none, and idle or
    start-up time belongs to no statement."""
    rate = " ".join(f"WHEN '{size}' THEN {dbu}" for size, dbu in DBU_PER_HOUR.items())
    con.execute(f"""
        INSERT INTO billing__attributed_usage
        WITH wh AS (
            SELECT warehouse_id, CASE warehouse_size {rate} END AS dbu_h
            FROM compute__warehouses
            QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
        ),
        slices AS (
            SELECT q.statement_id, q.workspace_id, q.compute.warehouse_id AS wid, q.executed_by,
                   q.client_application, q.query_tags, h AS hour_start,
                   GREATEST(q.start_time, h) AS s, LEAST(q.end_time, h + INTERVAL 1 HOUR) AS e
            FROM query__history q,
                 unnest(generate_series(date_trunc('hour', q.start_time), date_trunc('hour', q.end_time),
                                        INTERVAL 1 HOUR)) AS t(h)
            WHERE q.compute.warehouse_id IS NOT NULL AND NOT COALESCE(q.from_result_cache, FALSE)
              AND q.end_time > q.start_time
        ),
        raw AS (
            SELECT s.*, w.dbu_h * epoch(s.e - s.s) / 3600 AS dbus
            FROM slices s JOIN wh w ON w.warehouse_id = s.wid
            WHERE s.e > s.s
        ),
        billed AS (
            SELECT workspace_id, usage_metadata.warehouse_id AS wid, date_trunc('hour', usage_start_time) AS hour_start,
                   any_value(sku_name) AS sku, any_value(cloud) AS cloud, any_value(custom_tags) AS custom_tags,
                   SUM(usage_quantity) AS dbus, list(record_id) AS rids
            FROM billing__usage
            WHERE usage_metadata.warehouse_id IS NOT NULL AND usage_unit = 'DBU'
            GROUP BY ALL
        )
        SELECT 'ac_au_' || r.statement_id || '_' || strftime(r.hour_start, '%Y%m%d%H'),
               {{'dbsql_statement_id': r.statement_id, 'warehouse_id': r.wid, 'client_application': r.client_application}},
               {{'executed_by': r.executed_by}},
               r.hour_start, r.hour_start + INTERVAL 1 HOUR, CAST(r.hour_start AS DATE), 'DBU',
               r.dbus * LEAST(1, b.dbus / SUM(r.dbus) OVER (PARTITION BY r.workspace_id, r.wid, r.hour_start)),
               {{'query_tags': COALESCE(r.query_tags, MAP {{}})}}, b.rids, '{ACCOUNT}', r.workspace_id, b.sku, b.cloud,
               'SQL', COALESCE(b.custom_tags, MAP {{}})
        FROM raw r
        JOIN billed b ON b.workspace_id = r.workspace_id AND b.wid = r.wid AND b.hour_start = r.hour_start
    """)


def _review_tags(con, exp: dict) -> None:
    """acme-ops: its jobs carry cost_center=ops only for the last 15 days, so that is the
    workspace's tag from then on; an untagged notebook bills every day of the last 40. The
    notebook's earlier days must not take the later tag."""
    put(con, "access__workspaces_latest", account_id=ACCOUNT, workspace_id=OPS, workspace_name="acme-ops",
        workspace_url="https://acme-ops.example", create_time=DT(400), status="RUNNING")
    nb_usd = 0.0
    for d in range(1, 41):
        nb = bill(con, f"ac_b_ops_nb_{d}", OPS, SKU_NOTEBOOK, DT(d, 9), 1.0, 2, serverless=True, product="INTERACTIVE",
                  notebook_id="ac_nb_ops")
        if d <= 30:
            nb_usd += nb * RATES[SKU_NOTEBOOK]
        if d <= 15:
            bill(con, f"ac_b_ops_job_{d}", OPS, SKU_JOBS, DT(d, 2), 1.0, 10, tags={"cost_center": "ops"},
                 product="JOBS", job_id="ac_job_ops")
            put(con, "lakeflow__job_run_timeline", account_id=ACCOUNT, workspace_id=OPS, job_id="ac_job_ops",
                run_id=f"ac_job_ops_r{d}", period_start_time=DT(d, 2), period_end_time=DT(d, 3),
                trigger_type="PERIODIC", result_state="SUCCEEDED", run_type="JOB_RUN", termination_code="SUCCESS",
                run_duration_seconds=3600, queue_duration_seconds=0)
    put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=OPS, job_id="ac_job_ops", name="ops-nightly",
        creator_id="1", change_time=DT(15), delete_time=None, create_time=DT(15))
    put(con, "access__audit", account_id=ACCOUNT, workspace_id=OPS, event_time=DT(2, 10), event_date=D(2),
        user_identity={"email": "ops@acme.example", "subject_name": None}, service_name="notebook",
        action_name="runCommand", audit_level="WORKSPACE_LEVEL", event_id="ac_ev_ops")
    exp["ops"] = {"from": D(15), "job_usd": 15 * 10 * RATES[SKU_JOBS], "nb_usd_30": nb_usd}


def _review_storage(con) -> None:
    """A table dropped 10 days ago: 100,000 files of 1 MB and no predictive optimization. The drop
    time is set only on its last snapshot."""
    for d in range(10, 21):
        put(con, "storage__table_metrics_history", account_id=ACCOUNT, catalog_name="acme", schema_name="raw",
            table_name="tmp_dropped", table_id="ac_t_tmp_dropped", table_type="MANAGED", table_owner="data_eng",
            table_creation_time=DT(60), table_dropped_time=DT(10) if d == 10 else None, snapshot_date=D(d),
            active_bytes=100 * 10**9, active_files=100_000, predictive_optimization_enabled=False)


def _review_cost(con, exp: dict) -> None:
    """Cases the cost checks got wrong before the 2026-10 review."""
    # A serverless job in standard mode is not performance-optimized; one in performance mode is.
    bill(con, "ac_b_job_std", PROD, SKU_NOTEBOOK, DT(3, 2), 1.0, 100, serverless=True, product="JOBS",
         features={"performance_target": "STANDARD"}, job_id="ac_job_std")
    bill(con, "ac_b_job_perf", PROD, SKU_NOTEBOOK, DT(3, 3), 1.0, 40, serverless=True, product="JOBS",
         features={"performance_target": "PERFORMANCE_OPTIMIZED"}, job_id="ac_job_perf")
    exp["perf_optimized_dbus"] = 40.0
    # ac_vs_split: $15 compute and $15 storage on one day = $30, over the $25 WARN line.
    for kind in ("COMPUTE_TIME", "STORAGE_SPACE"):
        bill(con, f"ac_b_vs_split_{kind}", PROD, SKU_VECTOR, DT(4, 5), 1.0, 15 / 0.28, product="VECTOR_SEARCH",
             usage_type=kind, endpoint_id="ac_vs_split", endpoint_name="ac-vs-split")
    # Stored data in default storage has no storage_api_type; only API calls carry one.
    bill(con, "ac_b_dsu_space", PROD, "PREMIUM_DEFAULT_STORAGE_US_EAST", DT(5, 0), 1.0, 500,
         product="DEFAULT_STORAGE", unit="DSU", usage_type="STORAGE_SPACE")
    exp["dsu_space"] = 500.0


def _review_compute(con, exp: dict) -> None:
    """Cases the compute checks got wrong before the 2026-10 review: a warehouse billed every day
    but unused, and a runtime whose support has ended."""
    # ac_wh_zombie: auto-stop off, started D(150) and never stopped, billed every hour of the last
    # 30 days, no query at all: billed, but not in use.
    warehouse(con, "ac_wh_zombie", PROD, "PRO", "X_SMALL", 0, DT(200))
    event(con, "ac_wh_zombie", PROD, "STARTING", DT(150, 9), 1)
    event(con, "ac_wh_zombie", PROD, "RUNNING", DT(150, 9), 1)
    for d in range(1, 31):
        for h in range(24):
            bill(con, f"ac_b_zombie_{d}_{h}", PROD, SKU_PRO, DT(d, h), 1.0, 6, warehouse_id="ac_wh_zombie")
    exp["zombie"] = {"days": 150}
    # ac_cl_old: an all-purpose cluster on 17.2, past Databricks' end of support.
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=DEV, cluster_id="ac_cl_old", cluster_name="ac_cl_old",
        owned_by="owner@acme.example", create_time=DT(300), worker_count=1, auto_termination_minutes=30, tags={},
        cluster_source="UI", dbr_version="17.2.x-scala2.13", change_time=DT(300), change_date=D(300),
        data_security_mode="USER_ISOLATION", policy_id="ac_policy", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")


def _review_perf(con, exp: dict) -> None:
    """Cases the query checks got wrong before the 2026-10 review: spill and failures judged per
    user, number-only differences splitting a shape, and a cold start read as a full warehouse."""
    # Two people each spill 0.6 GB on ac_wh_etl on D(3): 1.2 GB for the warehouse that day.
    for sid, who in (("ac_q_etl_3_a", "dev1@acme.example"), ("ac_q_etl_3_b", "dev2@acme.example")):
        con.execute("UPDATE query__history SET executed_by = ?, spilled_local_bytes = 600000000 WHERE statement_id = ?",
                    [who, sid])
    # Six failed SELECTs on ac_wh_bi on D(2) 09:00, two each by three people, inside its busy seconds.
    for i in range(6):
        query(con, f"ac_q_bi_fail_{i}", "ac_wh_bi", PROD, DT(2, 9, 0, 11), DT(2, 9, 0, 14), status="FAILED",
              executed_by=f"user{i // 2}@acme.example", tags={"cost_center": "bi"})
    exp["bi"]["queries"] += 6
    # ac_wh_bi's first query on D(1) waited two minutes for compute to start: a cold start only.
    con.execute("UPDATE query__history SET waiting_for_compute_duration_ms = 120000 WHERE statement_id = 'ac_q_bi_1_8'")
    # The six failed SELECTs waited 3 s each for the same start: 18 s added up, 3 s by the clock.
    con.execute("UPDATE query__history SET waiting_for_compute_duration_ms = 3000 WHERE statement_id LIKE 'ac_q_bi_fail_%'")


def _review_jobs(con, exp: dict) -> None:
    """Cases the job and pipeline checks got wrong before the 2026-10 review: a repaired run, an
    update spanning hours, a job on an all-purpose cluster, and job versus task timeouts."""
    # ac_job_repaired: failed D(3) 01:00-01:10, repaired 09:00-09:20 under the same run_id.
    put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_repaired", name="ac_job_repaired",
        creator_id="1", change_time=DT(200), delete_time=None, create_time=DT(200), timeout_seconds=7200)
    for start, end, result in ((DT(3, 1), DT(3, 1, 10), "FAILED"), (DT(3, 9), DT(3, 9, 20), "SUCCEEDED")):
        put(con, "lakeflow__job_run_timeline", account_id=ACCOUNT, workspace_id=PROD, job_id="ac_job_repaired",
            run_id="ac_job_repaired_r1", period_start_time=start, period_end_time=end, trigger_type="PERIODIC",
            run_type="JOB_RUN", result_state=result, termination_code="SUCCESS" if result == "SUCCEEDED" else "RUN_EXECUTION_ERROR",
            run_duration_seconds=0)
    exp["repaired"] = {"run_s": 1200, "attempts": 2, "result": "SUCCEEDED"}

    # ac_pl_sales on D(8): one update 03:10-07:40, written as five hourly slices.
    bounds = [DT(8, 3, 10), DT(8, 4), DT(8, 5), DT(8, 6), DT(8, 7), DT(8, 7, 40)]
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=PROD, pipeline_id="ac_pl_sales",
            update_id="ac_pu_long", update_type="DEFAULT", trigger_type="JOB_TASK", period_start_time=a, period_end_time=b,
            result_state="COMPLETED" if i == len(bounds) - 2 else None)
    exp["pipeline"]["active_s"] = 6 * 1200 + 16200

    # ac_job_on_ap: three runs on the all-purpose team cluster, 03:00-03:30; billing carries no job_id
    # there, so the cluster's own bill in those hours prices it.
    put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=DEV, job_id="ac_job_on_ap", name="ac_job_on_ap",
        creator_id="1", change_time=DT(200), delete_time=None, create_time=DT(200))
    for d in (1, 2, 3):
        put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=DEV, job_id="ac_job_on_ap",
            run_id=f"ac_job_on_ap_t{d}", job_run_id=f"ac_job_on_ap_r{d}", period_start_time=DT(d, 3),
            period_end_time=DT(d, 3, 30), task_key="main", result_state="SUCCEEDED", termination_code="SUCCESS",
            compute_ids=["ac_cl_team"], compute=[{"type": "ALL_PURPOSE_COMPUTE", "cluster_id": "ac_cl_team", "warehouse_id": None}],
            setup_duration_seconds=5, execution_duration_seconds=1795)
        bill(con, f"ac_b_onap_{d}", DEV, SKU_ALL_PURPOSE, DT(d, 3), 1.0, 4, tags={"team": "platform"},
             product="ALL_PURPOSE", cluster_id="ac_cl_team")
    exp["on_all_purpose"] = {"usd": 3 * 4 * 0.55, "cost_basis": "cluster_hours"}

    # Timeouts: ac_job_ok only has a duration warning; ac_job_broken's task has none, but its job does.
    con.execute("UPDATE lakeflow__jobs SET health_rules = [{'metric': 'RUN_DURATION_SECONDS', 'operator': 'GREATER_THAN', "
                "'value': 3600}] WHERE job_id = 'ac_job_ok'")
    con.execute("UPDATE lakeflow__jobs SET timeout_seconds = 7200 WHERE job_id = 'ac_job_broken'")
    con.execute("UPDATE lakeflow__job_tasks SET timeout_seconds = NULL WHERE job_id = 'ac_job_broken'")


def _rest(con, exp: dict) -> None:
    """Classification, column lineage, Delta Sharing, row filters, schema grants, inbound denials,
    an instance pool, Genie, default storage, this app's own statements and three more predictive-
    optimization operations."""
    for col, tag, conf in (("email", "class.email_address", "HIGH"), ("phone", "class.phone_number", "HIGH")):
        put(con, "data_classification__results", catalog_name="acme", schema_name="sales", table_name="customers",
            column_name=col, class_tag=tag, confidence=conf, first_detected_time=DT(30), latest_detected_time=DT(1))
    for d in (1, 2):
        put(con, "access__column_lineage", account_id=ACCOUNT, workspace_id=PROD, entity_type="JOB", entity_id="ac_job_ok",
            source_table_full_name="acme.sales.customers", source_table_catalog="acme", source_table_schema="sales",
            source_table_name="customers", source_type="TABLE", source_column_name="email",
            target_table_full_name="acme.sales.daily", target_table_catalog="acme", target_table_schema="sales",
            target_table_name="daily", target_type="TABLE", target_column_name="customer_email",
            created_by="svc@acme.example", event_time=DT(d, 2, 10), event_date=D(d), direct_access=True)
    put(con, "information_schema__shares", share_name="ac_partner_share", share_owner="data_eng", created=DT(90),
        created_by="owner@acme.example", last_altered=DT(10))
    for recipient in ("partner_co", "auditor_llp"):
        put(con, "information_schema__share_recipient_privileges", grantor="owner@acme.example", recipient_name=recipient,
            share_name="ac_partner_share", privilege_type="SELECT")
    put(con, "information_schema__table_share_usage", catalog_name="acme", schema_name="sales", table_name="orders",
        share_name="ac_partner_share", shared_as_table="orders")
    put(con, "information_schema__row_filters", table_catalog="acme", table_schema="sales", table_name="orders",
        filter_name="acme.sec.region_filter", target_columns="region")
    put(con, "information_schema__schema_privileges", grantee="account users", catalog_name="acme", schema_name="sales",
        privilege_type="USE SCHEMA", inherited_from=None)
    for i in range(2):
        put(con, "access__inbound_network", account_id=ACCOUNT, workspace_id=PROD, event_id=f"ac_in_{i}",
            request_path="/api/2.0/clusters/list", source={"ip": "198.51.100.9"}, authenticated_as="intern@acme.example",
            event_time=DT(1, 20, i), policy_outcome="DENY", rule_label="office-only")
    put(con, "compute__instance_pools", account_id=ACCOUNT, workspace_id=PROD, instance_pool_id="ac_pool_warm",
        change_time=DT(90), create_time=DT(90), instance_pool_name="ac_pool_warm", tags={}, node_type="m5.xlarge",
        idle_instance_autotermination_minutes=60, min_idle_instances=4, max_capacity=20)
    for d in (1, 2, 3):
        bill(con, f"ac_b_pool_{d}", PROD, SKU_ALL_PURPOSE, DT(d, 0), 1.0, 2, product="ALL_PURPOSE", instance_pool_id="ac_pool_warm")
        bill(con, f"ac_b_genie_{d}", PROD, SKU_SERVERLESS, DT(d, 12), 1.0, 3, product="GENIE", serverless=True)
        bill(con, f"ac_b_dsu_{d}", PROD, "PREMIUM_DEFAULT_STORAGE", DT(d, 0), 1.0, 5, product="DEFAULT_STORAGE",
             unit="DSU", usage_type="STORAGE_SPACE", storage_api_type="TIER_1", catalog_id="ac_cat")
    con.execute("UPDATE query__history SET statement_text = 'SELECT * FROM system.billing.usage /* databricks_audit */' "
                "WHERE statement_id LIKE 'ac_q_audit_%'")
    for op, status, metrics in (
        ("AUTO_CLUSTERING_COLUMN_SELECTION", "SUCCESSFUL", {"old_clustering_columns": "region", "new_clustering_columns": "order_date"}),
        ("DATA_SKIPPING_COLUMN_SELECTION", "SUCCESSFUL", {"amount_of_scanned_bytes": str(80 * 10**9), "number_of_added_columns": "0"}),
        ("COMPACTION", "FAILED: INSUFFICIENT_PERMISSIONS", {}),
    ):
        put(con, "storage__predictive_optimization_operations_history", account_id=ACCOUNT, workspace_id=PROD,
            start_time=DT(1, 3), end_time=DT(1, 3, 10), metastore_name="acme", metastore_id="ac_ms", catalog_name="acme",
            schema_name="sales", table_name="customers", table_id="ac_t_customers", operation_type=op,
            operation_id=f"ac_po2_{op}", operation_status=status, operation_metrics=metrics, usage_unit="ESTIMATED_DBU",
            usage_quantity=1.5)


def write(out: Path) -> dict:
    """Builds the account in memory and writes one parquet per source to `out`, plus manifest.json."""
    if out.exists():
        shutil.rmtree(out)
    con = duckdb.connect()
    for create_sql in ddl.DDL.values():
        con.execute(create_sql)
    con.execute("BEGIN TRANSACTION")  # one commit, not one per row
    batched = Batched(con)
    exp = build(batched)
    batched.flush()
    con.execute("COMMIT")
    for key in ddl.DDL:
        folder = out / key
        folder.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY (SELECT * FROM \"{key}\") TO '{(folder / 'part-00000.parquet').as_posix()}' (FORMAT PARQUET)")
    # the shape a real export writes: every table it read, its row count and the dates it covers
    tables = {}
    for key in ddl.DDL:
        col = PREDICATES[key].time_column if key in PREDICATES else None
        rows, lo, hi = con.execute(
            f'SELECT count(*), min("{col}"), max("{col}") FROM "{key}"' if col else f'SELECT count(*), NULL, NULL FROM "{key}"'
        ).fetchone()
        tables[key] = {"state": "ok", "rows": rows, "files": 1, "time_column": col,
                       "min_time": str(lo) if lo is not None else None, "max_time": str(hi) if hi is not None else None,
                       "slices_failed": [], "reason": None, "message": None}
    (out / "manifest.json").write_text(json.dumps({
        "as_of": AS_OF.strftime("%Y-%m-%d %H:%M:%S"), "as_of_date": AS_OF.strftime("%Y-%m-%d"), "days": 90,
        "billing_days": 365, "host_fingerprint": "fake-account", "connector_version": "fake-account",
        "metastore": parse_metastore_id("aws:us-east-1:00000000-0000-4000-8000-0000000000ac"), "tables": tables,
    }), encoding="utf-8")
    con.close()
    return exp


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "snapshot"))
    print(json.dumps(write(Path(ap.parse_args().out)), indent=1, default=str))
