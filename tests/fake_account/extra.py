"""More Acme scenarios, each in a workspace of its own so the numbers pinned in test_fake_account.py
for acme-prod, acme-dev, acme-old, acme-ops and acme-analytics stay as they are.

Workspaces:
  2006 acme-finance  AWS, all three mandatory tags on every object kind, partial and oddly spelled tags,
                     tag conflicts, a warehouse whose cost_center changes, a renamed warehouse
  2007 acme-azure    Azure westeurope, billing only; workspace tags on every bill row; its own list prices
  2008 acme-gcp      GCP, billing only; a promotional price
  2009 acme-mixed    spend split 50/50 between two cost centers (the workspace tag becomes __mixed__)
  2010 acme-lowtag   40 percent of spend tagged (the workspace tag becomes __untagged__)
  2011 acme-uat      environment uat, an untagged Genie space
  2012 acme-eu       AWS eu-west-1: billed here, no rows in the regional tables
  2013 acme-closed   workspace cancelled; billing before that
  2014 acme-lab      jobs, pipelines, warehouses, compute, storage and access cases (cost_center lab)
No account price table exists in the export, so actual_vs_list stays NOT_ASSESSED.
"""
from __future__ import annotations

from datetime import timedelta

import build as b
from build import (ACCOUNT, D, DT, SKU_ALL_PURPOSE, SKU_DLT, SKU_DLT_SERVERLESS, SKU_EGRESS, SKU_JOBS, SKU_NOTEBOOK,
                   SKU_PRO, SKU_SERVERLESS, SKU_SERVING, SKU_TOKENS, bill, event, put, query, warehouse)

FIN, AZURE, GCP, MIXED, LOWTAG, UAT, EU, CLOSED, LAB = "2006", "2007", "2008", "2009", "2010", "2011", "2012", "2013", "2014"
LABTAG = {"cost_center": "lab"}
FINTAGS = {"cost_center": "finance", "domain": "accounting", "environment": "prod"}
SKU_SQL_CLASSIC = "PREMIUM_SQL_COMPUTE"
SKU_APPS, SKU_DB, SKU_ONLINE = "PREMIUM_APPS_COMPUTE_US_EAST", "PREMIUM_DATABASE_SERVERLESS_COMPUTE_US_EAST", "PREMIUM_ONLINE_TABLES_US_EAST"
SKU_CLEAN, SKU_GPU, SKU_SHARING = "PREMIUM_CLEAN_ROOM_US_EAST", "PREMIUM_SERVERLESS_GPU_COMPUTE_US_EAST", "PREMIUM_DELTA_SHARING_US_EAST"
NEW_RATES = {SKU_SQL_CLASSIC: 0.22, SKU_APPS: 0.30, SKU_DB: 0.40, SKU_ONLINE: 0.25, SKU_CLEAN: 0.50, SKU_SHARING: 0.09}
# SKU_GPU has no list price on purpose: spend that cannot be priced.
DBU_PER_HOUR_NEW = {"LARGE": 40, "2X_LARGE": 144}
NEW_ENTRIES = [
    ("finops@acme.example", "*", "*", "*"),
    ("de@acme.example", "2001", None, None),
    ("de@acme.example", "2005", None, None),
    ("bi@acme.example", "2001", "ac_wh_bi", None),
    ("ops@acme.example", None, None, "prod"),
    ("dev@acme.example", None, None, "dev"),
    ("  Mixed.Case@Acme.example ", "2011", None, None),
    ("unnamed@acme.example", "2999", None, None),
]


def span(con, rid, ws, sku, a, z, dbu_h, **kw) -> float:
    """Bills a..z one clock hour at a time; returns the DBUs."""
    total, i, t = 0.0, 0, a
    while t < z:
        nxt = min(z, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        total += bill(con, f"{rid}_{i}", ws, sku, t, (nxt - t).total_seconds() / 3600, dbu_h, **kw)
        i, t = i + 1, nxt
    return total


def session(con, wid, ws, a, z, dbu_h, *, clusters=1, tags=None, sku=SKU_PRO, serverless=False) -> None:
    event(con, wid, ws, "STARTING", a, clusters)
    event(con, wid, ws, "RUNNING", a, clusters)
    span(con, f"ac_b_{wid}_{a:%m%d%H%M}", ws, sku, a, z, dbu_h * clusters, tags=tags, serverless=serverless, warehouse_id=wid)
    event(con, wid, ws, "STOPPING", z, 0)
    event(con, wid, ws, "STOPPED", z + timedelta(seconds=30), 0)


def audit(con, eid, ws, t, service, action, *, email="admin@acme.example", status=200, params=None, ip="10.0.0.5",
          level="WORKSPACE_LEVEL", run_as=None, run_as_name=None):
    put(con, "access__audit", account_id=ACCOUNT, workspace_id=ws, event_time=t, event_date=t.date(), source_ip_address=ip,
        user_identity={"email": email, "subject_name": email}, service_name=service, action_name=action,
        request_params=params or {}, response={"status_code": status, "error_message": None, "result": None},
        audit_level=level, event_id=eid,
        identity_metadata={"run_by": None, "run_as": run_as, "acting_resource": None, "run_by_display_name": None,
                           "run_as_display_name": run_as_name})


def job_rows(con, ws, jid, *, tags=None, run_as=None, creator="1", created_by=None, deleted=None, timeout=None, trigger=None):
    put(con, "lakeflow__jobs", account_id=ACCOUNT, workspace_id=ws, job_id=jid, name=jid, creator_id=creator, tags=tags or {},
        run_as=run_as, change_time=DT(200), delete_time=deleted, create_time=DT(200), creator_user_name=created_by,
        run_as_user_name=run_as, timeout_seconds=timeout, trigger=trigger)


def run(con, ws, jid, d, start_h, start_m, secs, result="SUCCEEDED", *, queue=0, tasks=(), bill_dbu=None, tags=None,
        cluster=None, code=None, rid=None, product_kw=None):
    """One job run starting on day d: the run row, task rows (task_key, secs, result) and a JOBS bill."""
    rid = rid or f"{jid}_r{d}_{start_h}"
    a = DT(d, start_h, start_m)
    put(con, "lakeflow__job_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id=jid, run_id=rid, period_start_time=a,
        period_end_time=a + timedelta(seconds=secs), trigger_type="PERIODIC", result_state=result, run_type="JOB_RUN",
        termination_code=code or ("SUCCESS" if result == "SUCCEEDED" else "RUN_EXECUTION_ERROR"), run_duration_seconds=secs,
        queue_duration_seconds=queue, compute_ids=[cluster] if cluster else None)
    for key, tsecs, tres in tasks:
        put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id=jid, run_id=f"{rid}_{key}",
            job_run_id=rid, period_start_time=a, period_end_time=a + timedelta(seconds=tsecs), task_key=key, result_state=tres,
            termination_code="SUCCESS" if tres == "SUCCEEDED" else "RUN_EXECUTION_ERROR",
            compute_ids=[cluster] if cluster else None,
            compute=[{"type": "JOBS_COMPUTE", "cluster_id": cluster, "warehouse_id": None}] if cluster else None,
            setup_duration_seconds=30, execution_duration_seconds=max(0, tsecs - 30))
    dbu = bill_dbu if bill_dbu is not None else 8 * secs / 3600
    if dbu:
        bill(con, f"ac_b_{rid}", ws, SKU_JOBS, a, min(secs, 3600) / 3600, dbu / (min(secs, 3600) / 3600), tags=tags,
             product="JOBS", job_id=jid, job_run_id=rid, cluster_id=cluster, **(product_kw or {}))


def add_all(con, exp: dict) -> None:
    exp["entitlements"] = [dict(zip(("principal", "workspace_id", "warehouse_id", "environment"), e)) for e in NEW_ENTRIES]
    exp["new_active_warehouses"] = 0
    exp["tag_values"] = ["finance", "marketing", "treasury", "lab", "alpha", "beta", "gamma", "emea"]
    _world(con)
    _tags(con, exp)
    _billing(con, exp)
    _jobs(con, exp)
    _warehouses(con, exp)
    _compute(con)
    _serving(con)
    _storage(con)
    _access(con)


def _world(con) -> None:
    for ws, name, status in ((FIN, "acme-finance", "RUNNING"), (AZURE, "acme-azure", "RUNNING"), (GCP, "acme-gcp", "RUNNING"),
                             (MIXED, "acme-mixed", "RUNNING"), (LOWTAG, "acme-lowtag", "RUNNING"), (UAT, "acme-uat", "RUNNING"),
                             (EU, "acme-eu", "RUNNING"), (CLOSED, "acme-closed", "CANCELLED"), (LAB, "acme-lab", "RUNNING")):
        put(con, "access__workspaces_latest", account_id=ACCOUNT, workspace_id=ws, workspace_name=name,
            workspace_url=f"https://{name}.example", create_time=DT(300), status=status)

    def price(sku, cloud, start, end, rate, promo=None):
        put(con, "billing__list_prices", account_id=ACCOUNT, price_start_time=start, price_end_time=end, sku_name=sku,
            cloud=cloud, currency_code="USD", usage_unit="GB" if sku == SKU_EGRESS else "DBU",
            pricing={"default": rate, "promotional": {"default": promo}, "effective_list": {"default": promo or rate}})
    for sku, rate in NEW_RATES.items():
        price(sku, "aws", b.datetime(2020, 1, 1), None, rate)
    # Azure: a list price that changed 60 days ago; GCP: a promotion for twenty days.
    price(SKU_JOBS, "azure", b.datetime(2020, 1, 1), DT(60), 0.30)
    price(SKU_JOBS, "azure", DT(60), None, 0.35)
    price(SKU_NOTEBOOK, "azure", b.datetime(2020, 1, 1), None, 1.05)
    price(SKU_EGRESS, "azure", b.datetime(2020, 1, 1), None, 0.12)
    price(SKU_JOBS, "gcp", b.datetime(2020, 1, 1), DT(30), 0.16)
    price(SKU_JOBS, "gcp", DT(30), DT(10), 0.16, promo=0.10)
    price(SKU_JOBS, "gcp", DT(10), None, 0.16)
    price(SKU_NOTEBOOK, "gcp", b.datetime(2020, 1, 1), None, 0.98)


def _tags(con, exp: dict) -> None:
    ws = FIN
    # T1: every object kind with all three tags.
    warehouse(con, "ac_wh_fin", ws, "SERVERLESS", "SMALL", 10, DT(60), tags=FINTAGS)
    for d in range(1, 8):
        a = DT(d, 9)
        session(con, "ac_wh_fin", ws, a, a + timedelta(minutes=40), 12, tags=FINTAGS, sku=SKU_SERVERLESS, serverless=True)
        for i in range(4):
            tg = {**FINTAGS, "cost_center": "marketing"} if i == 3 and d <= 3 else FINTAGS  # T4: a query tag beats the warehouse
            query(con, f"ac_q_fin_{d}_{i}", "ac_wh_fin", ws, a + timedelta(minutes=5 + 8 * i), a + timedelta(minutes=9 + 8 * i),
                  tags=tg, executed_by="fin.analyst@acme.example")
    exp["new_active_warehouses"] += 1
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_fin", cluster_name="ac_cl_fin",
        owned_by="fin.owner@acme.example", create_time=DT(100), worker_count=2, auto_termination_minutes=30, tags=FINTAGS,
        cluster_source="UI", dbr_version="15.4.x-scala2.12", change_time=DT(100), change_date=D(100),
        data_security_mode="USER_ISOLATION", policy_id="ac_policy", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")
    for d in range(1, 31):
        bill(con, f"ac_b_fin_cl_{d}", ws, SKU_ALL_PURPOSE, DT(d, 10), 1.0, 4, tags=FINTAGS, product="ALL_PURPOSE", cluster_id="ac_cl_fin")
    pl = {"photon": True, "development": False, "continuous": False, "serverless": False, "edition": "ADVANCED", "channel": "CURRENT"}
    put(con, "lakeflow__pipelines", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_fin", pipeline_type="ETL", name="ac_pl_fin",
        created_by="fin.owner@acme.example", tags=FINTAGS, settings=pl, change_time=DT(100), create_time=DT(100))
    put(con, "lakeflow__pipelines", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_fin_sl", pipeline_type="ETL", name="ac_pl_fin_sl",
        created_by="fin.owner@acme.example", tags={}, settings={**pl, "serverless": True}, change_time=DT(100), create_time=DT(100))
    for d in range(1, 8):
        put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_fin", update_id=f"ac_fpu_{d}",
            update_type="DEFAULT", trigger_type="SCHEDULE", result_state="COMPLETED", period_start_time=DT(d, 6), period_end_time=DT(d, 6, 20))
        bill(con, f"ac_b_fpl_{d}", ws, SKU_DLT, DT(d, 6), 1.0, 2, tags=FINTAGS, product="DLT", dlt_pipeline_id="ac_pl_fin", dlt_update_id=f"ac_fpu_{d}")
        put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_fin_sl", update_id=f"ac_fspu_{d}",
            update_type="DEFAULT", trigger_type="SCHEDULE", result_state="COMPLETED", period_start_time=DT(d, 7), period_end_time=DT(d, 7, 15))
        bill(con, f"ac_b_fspl_{d}", ws, SKU_DLT_SERVERLESS, DT(d, 7), 1.0, 3, product="DLT", serverless=True, dlt_pipeline_id="ac_pl_fin_sl",
             dlt_update_id=f"ac_fspu_{d}")
    # a job, a serverless notebook and a serving endpoint, all with the full set
    job_rows(con, ws, "ac_job_fin", tags=FINTAGS)
    for d in range(1, 11):
        run(con, ws, "ac_job_fin", d, 3, 0, 1200, tags=FINTAGS)
    for d in range(1, 8):
        bill(con, f"ac_b_fin_nb_{d}", ws, SKU_NOTEBOOK, DT(d, 11), 1.0, 2, tags=FINTAGS, product="INTERACTIVE", serverless=True,
             notebook_id="ac_nb_fin", notebook_path="/Users/fin.owner/close", run_as="fin.owner@acme.example")
        bill(con, f"ac_b_fin_ep_{d}", ws, SKU_SERVING, DT(d, 0), 1.0, 5, tags=FINTAGS, product="MODEL_SERVING", usage_type="GPU_TIME",
             serverless=True, features={"serving_type": "MODEL"}, endpoint_id="ac_ep_fin", endpoint_name="ac-fin-ep")
    put(con, "serving__served_entities", served_entity_id="ac_ep_fin_e", account_id=ACCOUNT, workspace_id=ws, created_by="fin.owner@acme.example",
        endpoint_name="ac-fin-ep", endpoint_id="ac_ep_fin", served_entity_name="fin-1", entity_type="CUSTOM_MODEL",
        entity_name="acme.ml.fin", entity_version="1", endpoint_config_version=1, change_time=DT(60),
        custom_model_config={"min_concurrency": 0, "max_concurrency": 4, "compute_type": "CPU"})
    put(con, "serving__endpoint_usage", workspace_id=ws, account_id=ACCOUNT, client_request_id="ac_frq", databricks_request_id="ac_fdrq",
        requester="fin.owner@acme.example", status_code=200, request_time=DT(2, 9), served_entity_id="ac_ep_fin_e")

    # T2: partly tagged.
    job_rows(con, ws, "ac_job_fin_partial", tags={"cost_center": "finance"})
    put(con, "compute__warehouses", warehouse_id="ac_wh_fin_dom", workspace_id=ws, account_id=ACCOUNT, warehouse_name="ac_wh_fin_dom",
        warehouse_type="PRO", warehouse_channel="CHANNEL_NAME_CURRENT", warehouse_size="X_SMALL", min_clusters=1, max_clusters=1,
        auto_stop_minutes=10, tags={"domain": "accounting"}, change_time=DT(60), created_by="fin.owner@acme.example")
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_fin_env", cluster_name="ac_cl_fin_env",
        owned_by="fin.owner@acme.example", create_time=DT(100), worker_count=1, auto_termination_minutes=30, tags={"environment": "dev"},
        cluster_source="UI", dbr_version="15.4.x-scala2.12", change_time=DT(100), change_date=D(100), data_security_mode="USER_ISOLATION",
        policy_id="ac_policy", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")
    # T3: spelling and case of the key and the value, and an empty value
    for jid, tg in (("ac_job_fin_c1", {"Cost Center": "Finance"}), ("ac_job_fin_c2", {"COST-CENTER": "finance"}),
                    ("ac_job_fin_c3", {"costCenter": ""})):
        job_rows(con, ws, jid, tags=tg)
        for d in range(1, 6):
            run(con, ws, jid, d, 4, 0, 900, tags=tg)
    # T4: a job tagged differently from its job cluster; a cluster that takes its tag from its policy
    job_rows(con, ws, "ac_job_fin_conf", tags={"cost_center": "finance"})
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_fin_conf", cluster_name="ac_cl_fin_conf",
        owned_by="fin.owner@acme.example", create_time=DT(100), worker_count=1, auto_termination_minutes=0, tags={"cost_center": "treasury"},
        cluster_source="JOB", dbr_version="15.4.x-scala2.12", change_time=DT(100), change_date=D(100), data_security_mode="SINGLE_USER",
        policy_id="ac_policy", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")
    for d in range(1, 6):
        run(con, ws, "ac_job_fin_conf", d, 5, 0, 900, tags={"cost_center": "finance"}, cluster="ac_cl_fin_conf")
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_fin_pol", cluster_name="ac_cl_fin_pol",
        owned_by="fin.owner@acme.example", create_time=DT(100), worker_count=1, auto_termination_minutes=30, tags={},
        cluster_source="UI", dbr_version="15.4.x-scala2.12", change_time=DT(100), change_date=D(100), data_security_mode="USER_ISOLATION",
        policy_id="ac_policy_fin_tags", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")
    for d in range(1, 6):
        bill(con, f"ac_b_fin_pol_{d}", ws, SKU_ALL_PURPOSE, DT(d, 13), 1.0, 4, tags={"cost_center": "finance"}, product="ALL_PURPOSE",
             cluster_id="ac_cl_fin_pol")
    # T6: serverless that gets its tags only from a budget policy, from a usage policy, or from nothing
    for d in range(1, 8):
        bill(con, f"ac_b_fin_bp_{d}", ws, SKU_NOTEBOOK, DT(d, 14), 1.0, 2, tags={"cost_center": "finance"}, product="INTERACTIVE", serverless=True,
             notebook_id="ac_nb_fin_bp", budget_policy_id="ac_bp_fin")
        bill(con, f"ac_b_fin_up_{d}", ws, SKU_NOTEBOOK, DT(d, 15), 1.0, 2, tags={"cost_center": "finance"}, product="JOBS", serverless=True,
             job_id="ac_job_fin_up", usage_policy_id="ac_up_fin")
        bill(con, f"ac_b_fin_none_{d}", ws, SKU_NOTEBOOK, DT(d, 16), 1.0, 2, product="INTERACTIVE", serverless=True, notebook_id="ac_nb_fin_none")
    # T8 and E3: cost_center changes 20 days ago; the warehouse is renamed on the way
    for change, name, tg in ((DT(60), "fin-reports", {"cost_center": "finance"}), (DT(20), "fin-reporting", {"cost_center": "treasury"})):
        put(con, "compute__warehouses", warehouse_id="ac_wh_fin_scd", workspace_id=ws, account_id=ACCOUNT, warehouse_name=name,
            warehouse_type="PRO", warehouse_channel="CHANNEL_NAME_CURRENT", warehouse_size="X_SMALL", min_clusters=1, max_clusters=1,
            auto_stop_minutes=10, tags=tg, change_time=change, created_by="fin.owner@acme.example")
    for d in range(1, 31):
        tg = {"cost_center": "treasury" if d <= 20 else "finance"}
        session(con, "ac_wh_fin_scd", ws, DT(d, 8), DT(d, 8, 30), 6, tags=tg)
        if d <= 7:
            query(con, f"ac_q_fin_scd_{d}", "ac_wh_fin_scd", ws, DT(d, 8, 5), DT(d, 8, 9), executed_by="fin.analyst@acme.example")
    exp["new_active_warehouses"] += 1

    # T5: a workspace splitting its spend 50/50 between two cost centers, one with 40 percent tagged
    for d in range(1, 31):
        for n, tg in (("a", {"cost_center": "alpha"}), ("b", {"cost_center": "beta"})):
            bill(con, f"ac_b_mixed_{n}_{d}", MIXED, SKU_JOBS, DT(d, 2), 1.0, 10, tags=tg, product="JOBS", job_id=f"ac_job_mixed_{n}")
        bill(con, f"ac_b_low_t_{d}", LOWTAG, SKU_JOBS, DT(d, 2), 1.0, 8, tags={"cost_center": "gamma"}, product="JOBS", job_id="ac_job_low_t")
        bill(con, f"ac_b_low_u_{d}", LOWTAG, SKU_JOBS, DT(d, 3), 1.0, 12, product="JOBS", job_id="ac_job_low_u")
    # T7 and M1: an Azure workspace whose bill rows all carry workspace-level tags; GCP and a promotion
    for d in range(1, 31):
        bill(con, f"ac_b_az_job_{d}", AZURE, SKU_JOBS, DT(d, 2), 1.0, 12, tags={"cost_center": "emea", "domain": "sales"}, product="JOBS",
             job_id="ac_job_az", cloud="azure")
        bill(con, f"ac_b_az_nb_{d}", AZURE, SKU_NOTEBOOK, DT(d, 9), 1.0, 3, tags={"cost_center": "emea", "domain": "sales"},
             product="INTERACTIVE", serverless=True, notebook_id="ac_nb_az", cloud="azure")
        bill(con, f"ac_b_gcp_job_{d}", GCP, SKU_JOBS, DT(d, 2), 1.0, 12, product="JOBS", job_id="ac_job_gcp", cloud="gcp")
        bill(con, f"ac_b_gcp_nb_{d}", GCP, SKU_NOTEBOOK, DT(d, 9), 1.0, 3, product="INTERACTIVE", serverless=True, notebook_id="ac_nb_gcp", cloud="gcp")
    for d in range(61, 70):  # before and after the Azure price change
        bill(con, f"ac_b_az_old_{d}", AZURE, SKU_JOBS, DT(d, 2), 1.0, 12, tags={"cost_center": "emea", "domain": "sales"}, product="JOBS",
             job_id="ac_job_az", cloud="azure")


def _billing(con, exp: dict) -> None:
    # M2: billed in eu-west-1; nothing in the regional tables.  E1: a cancelled workspace's last bills.
    for d in range(1, 31):
        bill(con, f"ac_b_eu_{d}", EU, SKU_JOBS, DT(d, 2), 1.0, 6, product="JOBS", job_id="ac_job_eu")
    for d in range(40, 70):
        bill(con, f"ac_b_closed_{d}", CLOSED, SKU_NOTEBOOK, DT(d, 2), 1.0, 3, product="INTERACTIVE", serverless=True, notebook_id="ac_nb_closed")
    # M3: egress between regions and across clouds
    for d in range(1, 8):
        for src, dst, ws, cloud, gb in (("us-east-1", "eu-west-1", LAB, "aws", 80), ("us-east-1", "us-west-2", LAB, "aws", 15),
                                        ("westeurope", "us-east-1", AZURE, "azure", 30), ("eu-west-1", "us-east-1", EU, "aws", 20)):
            bill(con, f"ac_b_xeg_{ws}_{dst}_{d}", ws, SKU_EGRESS, DT(d, 17), 1.0, gb, product="NETWORKING", unit="GB", usage_type="NETWORK_BYTE",
                 source_region=src, destination_region=dst, cloud=cloud)
    # S3: other billed products; the GPU one has no list price
    for d in range(1, 15):
        for rid, sku, product, kw, dbu in (("apps", SKU_APPS, "APPS", {"app_id": "ac_app_portal", "app_name": "portal"}, 12),
                                           ("db", SKU_DB, "DATABASE", {}, 8), ("mon", SKU_DLT_SERVERLESS, "LAKEHOUSE_MONITORING", {}, 3),
                                           ("onl", SKU_ONLINE, "ONLINE_TABLES", {}, 4), ("clean", SKU_CLEAN, "CLEAN_ROOM", {"central_clean_room_id": "ac_cr1"}, 2),
                                           ("gpu", SKU_GPU, "AI_RUNTIME", {"ai_runtime_pool_id": "ac_pool_gpu"}, 16),
                                           ("share", SKU_SHARING, "DELTA_SHARING", {}, 40)):
            bill(con, f"ac_b_p_{rid}_{d}", LAB, sku, DT(d, 6), 1.0, dbu, tags=LABTAG, product=product, serverless=True, **kw)
    # P5: one day at five times the usual spend; P3: today's hours so far; P4: a retraction and its restatement
    for d in range(1, 31):
        bill(con, f"ac_b_spk_{d}", LAB, SKU_JOBS, DT(d, 20), 1.0, 400 if d == 6 else 80, tags=LABTAG, product="JOBS", job_id="ac_job_lab_spike")
    for h in range(0, 11):
        bill(con, f"ac_b_today_{h}", LAB, SKU_JOBS, DT(0, h), 1.0, 40, tags=LABTAG, product="JOBS", job_id="ac_job_lab_spike")
    audit(con, "ac_a_today", LAB, DT(0, 9), "accounts", "login", email="lab.user@acme.example")
    for kind, qty, ing in (("ORIGINAL", 100.0, 8), ("RETRACTION", -100.0, 3), ("RESTATEMENT", 130.0, 3)):
        bill(con, f"ac_b_rst_{kind}", LAB, SKU_JOBS, DT(8, 21), 1.0, qty, tags=LABTAG, product="JOBS", job_id="ac_job_lab_restated",
             record_type=kind, ingested=D(ing))
    # Genie in the UAT workspace, no tags (S4)
    for d in range(1, 15):
        bill(con, f"ac_b_uat_genie_{d}", UAT, SKU_SERVERLESS, DT(d, 12), 1.0, 2, product="GENIE", serverless=True,
             genie={"agent_id": None, "channel": "UI", "surface": "GENIE_ONE"}, run_as="uat.user@acme.example")
        bill(con, f"ac_b_uat_job_{d}", UAT, SKU_JOBS, DT(d, 2), 1.0, 5, product="JOBS", job_id="ac_job_uat")
    # E4: spend only between 70 and 50 days ago, so the 7 and 30 day windows never see it
    for d in range(50, 71):
        bill(con, f"ac_b_old_only_{d}", LAB, SKU_JOBS, DT(d, 1), 1.0, 20, tags=LABTAG, product="JOBS", job_id="ac_job_lab_old")



def _jobs(con, exp: dict) -> None:
    ws = LAB
    sp_admin = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    # J1 duration regression: ten minutes for 20 days, forty for the last five
    job_rows(con, ws, "ac_job_regress", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 26):
        run(con, ws, "ac_job_regress", d, 1, 0, 2400 if d <= 5 else 600, tags=LABTAG)
    # failed runs piling on one termination code: 6 (WARN) and 22 (CRITICAL)
    for jid, n in (("ac_job_failing", 6), ("ac_job_failing_hard", 22)):
        job_rows(con, ws, jid, tags=LABTAG, created_by="lab.owner@acme.example")
        for k in range(n):
            run(con, ws, jid, 1 + k % 20, 6 + k // 20, 0, 300, "FAILED", tags=LABTAG)
    # a job whose workers work at about half their CPU: compute pressure judged OK
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_ok", cluster_name="ac_cl_ok",
        owned_by="lab.owner@acme.example", create_time=DT(50), worker_count=2, auto_termination_minutes=0, tags=LABTAG,
        cluster_source="JOB", dbr_version="15.4.x-scala2.12", change_time=DT(50), change_date=D(50), data_security_mode="SINGLE_USER",
        policy_id="ac_policy", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")
    job_rows(con, ws, "ac_job_healthy", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 4):
        run(con, ws, "ac_job_healthy", d, 20, 0, 1800, tasks=(("work", 1780, "SUCCEEDED"),), tags=LABTAG, cluster="ac_cl_ok", bill_dbu=8)
        for minute in range(30):
            t = DT(d, 20, minute)
            for i in range(3):
                put(con, "compute__node_timeline", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_ok", instance_id=f"ac_i_ok_{i}_{d}",
                    start_time=t, end_time=t + timedelta(minutes=1), driver=i == 0, cpu_user_percent=50.0, cpu_system_percent=5.0,
                    cpu_wait_percent=0.0, mem_used_percent=55.0, mem_swap_percent=0.0, node_type="m5.xlarge")
    # J2 queue time
    job_rows(con, ws, "ac_job_queued", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 11):
        run(con, ws, "ac_job_queued", d, 8, 0, 900, queue=700, tags=LABTAG)
    # J3 a task that finishes just before its limit
    job_rows(con, ws, "ac_job_neartimeout", tags=LABTAG, created_by="lab.owner@acme.example")
    put(con, "lakeflow__job_tasks", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_neartimeout", task_key="load",
        change_time=DT(200), timeout_seconds=3600)
    for d in range(1, 6):
        run(con, ws, "ac_job_neartimeout", d, 9, 0, 3420, tasks=(("load", 3400, "SUCCEEDED"),), tags=LABTAG)
    # J4 a job last run 100 days ago, and a scheduled job that never ran
    job_rows(con, ws, "ac_job_stale", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(100, 106):
        run(con, ws, "ac_job_stale", d, 2, 0, 600, tags=LABTAG)
    job_rows(con, ws, "ac_job_neverran", tags=LABTAG, created_by="lab.owner@acme.example",
             trigger={"file_arrival": None, "periodic": None, "continuous": None, "table_update": None,
                      "schedule": {"quartz_cron_expression": "0 0 2 * * ?", "timezone_id": "UTC"}})
    # J5 owners who left, and a deleted user (they appear nowhere in the audit log)
    job_rows(con, ws, "ac_job_orphan", tags=LABTAG, created_by="left.company@acme.example", run_as="left.company@acme.example")
    for d in range(1, 6):
        run(con, ws, "ac_job_orphan", d, 10, 0, 600, tags=LABTAG)
    # J6 a task retried three times in one run; J7 a run that succeeded though a task failed
    job_rows(con, ws, "ac_job_retry", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 4):
        rid = f"ac_job_retry_r{d}"
        run(con, ws, "ac_job_retry", d, 11, 0, 1500, rid=rid, tags=LABTAG)
        for k, res in enumerate(("FAILED", "FAILED", "FAILED", "SUCCEEDED")):
            put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_retry", run_id=f"{rid}_a{k}",
                job_run_id=rid, period_start_time=DT(d, 11, 5 * k), period_end_time=DT(d, 11, 5 * k + 4), task_key="fetch", result_state=res,
                termination_code="SUCCESS" if res == "SUCCEEDED" else "RUN_EXECUTION_ERROR", setup_duration_seconds=10, execution_duration_seconds=230)
    job_rows(con, ws, "ac_job_partial", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 6):
        run(con, ws, "ac_job_partial", d, 12, 0, 900, tasks=(("extract", 400, "SUCCEEDED"), ("notify", 60, "FAILED")), tags=LABTAG)
    # J8 runs that never started
    job_rows(con, ws, "ac_job_skipped", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 9):
        res, code = (("SKIPPED", "SKIPPED"), ("UPSTREAM_CANCELED", "UPSTREAM_CANCELED"), ("CANCELED", "CANCELED"))[d % 3]
        run(con, ws, "ac_job_skipped", d, 13, 0, 0, res, code=code, bill_dbu=0)
    # J9 eight workers at 5 percent CPU
    put(con, "compute__clusters", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_big", cluster_name="ac_cl_big",
        owned_by="lab.owner@acme.example", create_time=DT(50), worker_count=8, auto_termination_minutes=0, tags=LABTAG,
        cluster_source="JOB", dbr_version="15.4.x-scala2.12", change_time=DT(50), change_date=D(50), data_security_mode="SINGLE_USER",
        policy_id="ac_policy", driver_node_type="m5.xlarge", worker_node_type="m5.xlarge")
    job_rows(con, ws, "ac_job_big", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 4):
        run(con, ws, "ac_job_big", d, 14, 0, 1800, tasks=(("crunch", 1780, "SUCCEEDED"),), tags=LABTAG, cluster="ac_cl_big", bill_dbu=40)
        for minute in range(30):
            t = DT(d, 14, minute)
            for i in range(9):
                put(con, "compute__node_timeline", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_big", instance_id=f"ac_i_big_{i}_{d}",
                    start_time=t, end_time=t + timedelta(minutes=1), driver=i == 0, cpu_user_percent=5.0, cpu_system_percent=1.0,
                    cpu_wait_percent=0.0, mem_used_percent=15.0, mem_swap_percent=0.0, node_type="m5.xlarge")
    # J10 a deleted job and a deleted pipeline with spend before they went
    job_rows(con, ws, "ac_job_deleted", tags=LABTAG, created_by="lab.owner@acme.example", deleted=DT(10))
    for d in range(11, 31):
        run(con, ws, "ac_job_deleted", d, 15, 0, 900, tags=LABTAG)
    pl = {"photon": False, "development": False, "continuous": False, "serverless": False, "edition": "CORE", "channel": "CURRENT"}
    put(con, "lakeflow__pipelines", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_deleted", pipeline_type="ETL", name="ac_pl_deleted",
        created_by="lab.owner@acme.example", tags=LABTAG, settings=pl, change_time=DT(12), create_time=DT(100), delete_time=DT(12))
    for d in range(13, 20):
        bill(con, f"ac_b_pldel_{d}", ws, SKU_DLT, DT(d, 5), 1.0, 2, tags=LABTAG, product="DLT", dlt_pipeline_id="ac_pl_deleted", dlt_update_id=f"ac_pdu_{d}")
    # J11 a continuous pipeline, one in development mode, one failing three times in a row with retries
    for pid, settings in (("ac_pl_cont", {**pl, "continuous": True}), ("ac_pl_dev", {**pl, "development": True}), ("ac_pl_fail", pl)):
        put(con, "lakeflow__pipelines", account_id=ACCOUNT, workspace_id=ws, pipeline_id=pid, pipeline_type="ETL", name=pid,
            created_by="lab.owner@acme.example", tags=LABTAG, settings=settings, change_time=DT(60), create_time=DT(60))
    put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_cont", update_id="ac_cu_1",
        update_type="DEFAULT", trigger_type="API_CALL", result_state="RUNNING", period_start_time=DT(10), period_end_time=DT(0, 11))
    for d in range(1, 11):
        bill(con, f"ac_b_plcont_{d}", ws, SKU_DLT, DT(d, 0), 1.0, 60, tags=LABTAG, product="DLT", dlt_pipeline_id="ac_pl_cont", dlt_update_id="ac_cu_1")
        put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_dev", update_id=f"ac_du_{d}",
            update_type="DEFAULT", trigger_type="USER_ACTION", result_state="COMPLETED", period_start_time=DT(d, 16), period_end_time=DT(d, 16, 10))
        bill(con, f"ac_b_pldev_{d}", ws, SKU_DLT, DT(d, 16), 1.0, 1, tags=LABTAG, product="DLT", dlt_pipeline_id="ac_pl_dev", dlt_update_id=f"ac_du_{d}")
    for i, trig in enumerate(("SCHEDULE",) + ("RETRY_ON_FAILURE",) * 12):
        put(con, "lakeflow__pipeline_update_timeline", account_id=ACCOUNT, workspace_id=ws, pipeline_id="ac_pl_fail", update_id=f"ac_fu_{i}",
            update_type="DEFAULT", trigger_type=trig, result_state="FAILED", period_start_time=DT(2, 4) + timedelta(minutes=10 * i),
            period_end_time=DT(2, 4) + timedelta(minutes=10 * i + 8))
        bill(con, f"ac_b_plfail_{i}", ws, SKU_DLT, DT(2, 4) + timedelta(minutes=10 * i), 1 / 6, 6, tags=LABTAG, product="DLT", dlt_pipeline_id="ac_pl_fail", dlt_update_id=f"ac_fu_{i}")
    bill(con, "ac_b_plfail_big", ws, SKU_DLT, DT(3, 4), 1.0, 2500, tags=LABTAG, product="DLT", dlt_pipeline_id="ac_pl_fail", dlt_update_id="ac_fu_0")
    # J12 run as an admin service principal next to an ordinary user; J13 jobs on serverless and on a SQL warehouse
    job_rows(con, ws, "ac_job_asadmin", tags=LABTAG, created_by="lab.owner@acme.example", run_as=sp_admin)
    audit(con, "ac_a_spadmin", ws, DT(4, 10), "accounts", "setAdmin", params={"targetUserName": sp_admin})
    for d in range(1, 6):
        run(con, ws, "ac_job_asadmin", d, 17, 0, 600, tags=LABTAG, product_kw={"run_as": sp_admin})
    job_rows(con, ws, "ac_job_serverless", tags=LABTAG, created_by="lab.owner@acme.example")
    job_rows(con, ws, "ac_job_sqltask", tags=LABTAG, created_by="lab.owner@acme.example")
    for d in range(1, 8):
        rid = f"ac_job_serverless_r{d}"
        run(con, ws, "ac_job_serverless", d, 18, 0, 600, rid=rid, bill_dbu=0)
        put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_serverless", run_id=f"{rid}_t",
            job_run_id=rid, period_start_time=DT(d, 18), period_end_time=DT(d, 18, 10), task_key="main", result_state="SUCCEEDED",
            termination_code="SUCCESS", compute=[{"type": "SERVERLESS", "cluster_id": None, "warehouse_id": None}],
            setup_duration_seconds=5, execution_duration_seconds=595)
        bill(con, f"ac_b_jsl_{d}", ws, SKU_NOTEBOOK, DT(d, 18), 10 / 60, 24, tags=LABTAG, product="JOBS", serverless=True, job_id="ac_job_serverless",
             job_run_id=rid, features={"performance_target": "STANDARD"})
        rid = f"ac_job_sqltask_r{d}"
        run(con, ws, "ac_job_sqltask", d, 19, 0, 600, rid=rid, bill_dbu=0)
        put(con, "lakeflow__job_task_run_timeline", account_id=ACCOUNT, workspace_id=ws, job_id="ac_job_sqltask", run_id=f"{rid}_t",
            job_run_id=rid, period_start_time=DT(d, 19), period_end_time=DT(d, 19, 10), task_key="sql", result_state="SUCCEEDED",
            termination_code="SUCCESS", compute=[{"type": "SQL_WAREHOUSE", "cluster_id": None, "warehouse_id": "ac_wh_gaps"}],
            setup_duration_seconds=0, execution_duration_seconds=600)


def _warehouses(con, exp: dict) -> None:
    ws = LAB

    def wh(wid, wtype, size, auto_stop, mn=1, mx=1):
        put(con, "compute__warehouses", warehouse_id=wid, workspace_id=ws, account_id=ACCOUNT, warehouse_name=wid, warehouse_type=wtype,
            warehouse_channel="CHANNEL_NAME_CURRENT", warehouse_size=size, min_clusters=mn, max_clusters=mx, auto_stop_minutes=auto_stop,
            tags=LABTAG, change_time=DT(60), created_by="lab.owner@acme.example")
        exp["new_active_warehouses"] += 1

    # Q1 + Q2: min 1 max 4, flapping between one and two clusters every ten minutes; statements wait 70 s at capacity
    wh("ac_wh_scale", "PRO", "MEDIUM", 10, 1, 4)
    for d in range(1, 8):
        a, z = DT(d, 9), DT(d, 13)
        event(con, "ac_wh_scale", ws, "STARTING", a, 1)
        event(con, "ac_wh_scale", ws, "RUNNING", a, 1)
        count = 1
        for k in range(1, 24):
            count = 2 if count == 1 else 1
            event(con, "ac_wh_scale", ws, "SCALED_UP" if count == 2 else "SCALED_DOWN", a + timedelta(minutes=10 * k), count)
            span(con, f"ac_b_scale_{d}_{k}", ws, SKU_PRO, a + timedelta(minutes=10 * (k - 1)), a + timedelta(minutes=10 * k),
                 24 * (2 if count == 1 else 1), warehouse_id="ac_wh_scale", tags=LABTAG)
        span(con, f"ac_b_scale_{d}_z", ws, SKU_PRO, a + timedelta(minutes=230), z, 24 * (2 if count == 2 else 1), warehouse_id="ac_wh_scale", tags=LABTAG)
        event(con, "ac_wh_scale", ws, "STOPPING", z, 0)
        event(con, "ac_wh_scale", ws, "STOPPED", z + timedelta(seconds=30), 0)
        for i in range(48):
            s = a + timedelta(minutes=5 * i)
            query(con, f"ac_q_scale_{d}_{i}", "ac_wh_scale", ws, s, s + timedelta(seconds=600), executed_by=f"lab{i % 4}@acme.example",
                  tags=LABTAG, waiting_at_capacity_duration_ms=70000)
    # Q3 serverless, every statement waits for compute; Q5 failures of every kind on the idle-gaps warehouse
    wh("ac_wh_cold", "SERVERLESS", "SMALL", 1)
    for d in range(1, 8):
        for h in range(8, 20):
            a = DT(d, h)
            session(con, "ac_wh_cold", ws, a, a + timedelta(seconds=105), 12, tags=LABTAG, sku=SKU_SERVERLESS, serverless=True)
            query(con, f"ac_q_cold_{d}_{h}", "ac_wh_cold", ws, a, a + timedelta(seconds=45), tags=LABTAG, waiting_for_compute_duration_ms=30000,
                  executed_by="lab.bi@acme.example", read_io_cache_percent=30 if d % 2 else 10)
    # Q4 running all day, a ten-second statement every thirty minutes (auto-stop is 60)
    wh("ac_wh_gaps", "PRO", "SMALL", 60)
    errors = (("CANCELED", "Query was cancelled by user"), ("FAILED", "[PERMISSION_DENIED] User does not have SELECT on acme.sales.orders"),
              ("FAILED", "[STATEMENT_TIMEOUT] Statement timed out after 3600 seconds"))
    for d in range(1, 8):
        a, z = DT(d, 8), DT(d, 18, 20)
        session(con, "ac_wh_gaps", ws, a, z, 12, tags=LABTAG)
        for i in range(20):
            query(con, f"ac_q_gaps_{d}_{i}", "ac_wh_gaps", ws, a + timedelta(minutes=30 * i, seconds=30),
                  a + timedelta(minutes=30 * i, seconds=40), tags=LABTAG, executed_by="lab.report@acme.example")
        for i, (status, msg) in enumerate(errors):
            s = DT(d, 12, 5 * i)
            query(con, f"ac_q_gaps_f{d}_{i}", "ac_wh_gaps", ws, s, s + timedelta(seconds=900), status=status, error_message=msg, tags=LABTAG,
                  total_task_duration_ms=5_000_000, executed_by="lab.report@acme.example")
    # Q7 a 2X-Large classic warehouse for an hour a day
    wh("ac_wh_huge", "CLASSIC", "2X_LARGE", 10)
    for d in range(1, 8):
        session(con, "ac_wh_huge", ws, DT(d, 10), DT(d, 11), 144, tags=LABTAG, sku=SKU_SQL_CLASSIC)
        query(con, f"ac_q_huge_{d}", "ac_wh_huge", ws, DT(d, 10, 5), DT(d, 10, 50), tags=LABTAG, executed_by="lab.data@acme.example")
    # Q6 statements of a classic pipeline and of serverless compute (no warehouse), Photon off and on
    for d in range(1, 6):
        query(con, f"ac_q_plc_{d}", None, ws, DT(d, 5), DT(d, 5, 2), compute={"type": "CLASSIC_COMPUTE", "cluster_id": None, "warehouse_id": None},
              query_source={"pipeline_info": {"pipeline_id": "ac_pl_fail", "update_id": "ac_fu_0"}}, executed_by="lab.owner@acme.example")
        bill(con, f"ac_b_nophoton_{d}", ws, SKU_JOBS, DT(d, 22), 1.0, 8, tags=LABTAG, product="JOBS", job_id="ac_job_regress")
        bill(con, f"ac_b_photon_{d}", ws, SKU_JOBS, DT(d, 23), 1.0, 8, tags=LABTAG, product="JOBS", job_id="ac_job_regress", features={"is_photon": True})
    # P3 today's statements
    query(con, "ac_q_today", "ac_wh_gaps", ws, DT(0, 9), DT(0, 9, 1), tags=LABTAG, executed_by="lab.report@acme.example")
    # E4 a warehouse created after the window began and one billed only long ago
    put(con, "compute__warehouses", warehouse_id="ac_wh_fresh", workspace_id=ws, account_id=ACCOUNT, warehouse_name="ac_wh_fresh",
        warehouse_type="PRO", warehouse_channel="CHANNEL_NAME_CURRENT", warehouse_size="SMALL", min_clusters=1, max_clusters=1,
        auto_stop_minutes=10, tags=LABTAG, change_time=DT(2), created_by="lab.owner@acme.example")
    for d in range(55, 70):
        bill(con, f"ac_b_wh_old_{d}", ws, SKU_PRO, DT(d, 9), 1.0, 12, tags=LABTAG, warehouse_id="ac_wh_oldonly")
    put(con, "compute__warehouses", warehouse_id="ac_wh_oldonly", workspace_id=ws, account_id=ACCOUNT, warehouse_name="ac_wh_oldonly",
        warehouse_type="PRO", warehouse_channel="CHANNEL_NAME_CURRENT", warehouse_size="SMALL", min_clusters=1, max_clusters=1,
        auto_stop_minutes=10, tags=LABTAG, change_time=DT(80), delete_time=DT(50), created_by="lab.owner@acme.example")


def _compute(con) -> None:
    ws = LAB
    # C1 a pool that keeps ten idle instances and is never used
    put(con, "compute__instance_pools", account_id=ACCOUNT, workspace_id=ws, instance_pool_id="ac_pool_idle", change_time=DT(90), create_time=DT(90),
        instance_pool_name="ac_pool_idle", tags=LABTAG, node_type="m5.xlarge", idle_instance_autotermination_minutes=0, min_idle_instances=10, max_capacity=20)
    for d in range(1, 31):
        bill(con, f"ac_b_pool_idle_{d}", ws, SKU_ALL_PURPOSE, DT(d, 0), 24.0, 20, tags=LABTAG, product="ALL_PURPOSE", instance_pool_id="ac_pool_idle")
    # C2 a spot cluster that lost nodes; C3 a GPU cluster that is barely used; C4 a deleted cluster with earlier spend
    base = dict(account_id=ACCOUNT, workspace_id=ws, owned_by="lab.owner@acme.example", create_time=DT(80), tags=LABTAG, cluster_source="UI",
                dbr_version="15.4.x-scala2.12", change_time=DT(80), change_date=D(80), data_security_mode="USER_ISOLATION", policy_id="ac_policy",
                driver_node_type="m5.xlarge")
    put(con, "compute__clusters", cluster_id="ac_cl_spot", cluster_name="ac_cl_spot", worker_node_type="m5.xlarge", worker_count=4,
        auto_termination_minutes=30, aws_attributes={"first_on_demand": 1, "availability": "SPOT_WITH_FALLBACK", "zone_id": "us-east-1a",
        "instance_profile_arn": None, "spot_bid_price_percent": 100, "ebs_volume_type": None, "ebs_volume_count": None, "ebs_volume_size": None,
        "ebs_volume_iops": None, "ebs_volume_throughput": None}, **base)
    for i in range(6):
        put(con, "compute__instance_events", account_id=ACCOUNT, workspace_id=ws, instance_id=f"ac_i_spot_{i}", event_time=DT(2, 10, 10 * i),
            event_type="SPOT_PREEMPTED", cluster_id="ac_cl_spot", node_type="m5.xlarge", state="TERMINATED", availability_type="SPOT")
    put(con, "compute__node_types", account_id=ACCOUNT, node_type="g5.4xlarge", core_count=16.0, memory_mb=65536, gpu_count=1)
    put(con, "compute__clusters", cluster_id="ac_cl_gpu", cluster_name="ac_cl_gpu", worker_node_type="g5.4xlarge", worker_count=2,
        auto_termination_minutes=0, **{**base, "driver_node_type": "g5.4xlarge"})
    for d in range(1, 15):
        bill(con, f"ac_b_gpu_{d}", ws, SKU_ALL_PURPOSE, DT(d, 8), 6.0, 30, tags=LABTAG, product="ALL_PURPOSE", cluster_id="ac_cl_gpu", node_type="g5.4xlarge")
    for minute in range(60):
        for inst, drv in (("ac_i_gpu_d", True), ("ac_i_gpu_w1", False), ("ac_i_gpu_w2", False)):
            t = DT(1, 8, minute)
            put(con, "compute__node_timeline", account_id=ACCOUNT, workspace_id=ws, cluster_id="ac_cl_gpu", instance_id=inst, start_time=t,
                end_time=t + timedelta(minutes=1), driver=drv, cpu_user_percent=4.0, cpu_system_percent=1.0, cpu_wait_percent=0.0,
                mem_used_percent=20.0, mem_swap_percent=0.0, node_type="g5.4xlarge")
    put(con, "compute__clusters", cluster_id="ac_cl_gone", cluster_name="ac_cl_gone", worker_node_type="m5.xlarge", worker_count=2,
        auto_termination_minutes=30, delete_time=DT(15), **base)
    for d in range(16, 31):
        bill(con, f"ac_b_clgone_{d}", ws, SKU_ALL_PURPOSE, DT(d, 9), 2.0, 4, tags=LABTAG, product="ALL_PURPOSE", cluster_id="ac_cl_gone")


def _serving(con) -> None:
    ws = LAB

    def entity(eid, name, etype, **kw):
        put(con, "serving__served_entities", served_entity_id=f"{eid}_e", account_id=ACCOUNT, workspace_id=ws, created_by="lab.owner@acme.example",
            endpoint_name=name, endpoint_id=eid, served_entity_name=f"{name}-1", entity_type=etype, entity_name=f"acme.ml.{name}",
            entity_version="1", endpoint_config_version=1, change_time=DT(80), **kw)
    # S1 requests until 40 days ago, billed ever since
    entity("ac_ep_dormant", "ac-dormant", "CUSTOM_MODEL", custom_model_config={"min_concurrency": 0, "max_concurrency": 4, "compute_type": "CPU"})
    for d in range(41, 47):
        put(con, "serving__endpoint_usage", workspace_id=ws, account_id=ACCOUNT, client_request_id=f"ac_dq_{d}", databricks_request_id=f"ac_ddq_{d}",
            requester="lab.owner@acme.example", status_code=200, request_time=DT(d, 10), served_entity_id="ac_ep_dormant_e")
    for d in range(1, 31):
        bill(con, f"ac_b_dormant_{d}", ws, SKU_SERVING, DT(d, 0), 24.0, 4, tags=LABTAG, product="MODEL_SERVING", usage_type="GPU_TIME", serverless=True,
             features={"serving_type": "MODEL"}, endpoint_id="ac_ep_dormant", endpoint_name="ac-dormant")
    # S2 provisioned throughput, an external model, a GPU endpoint that never scales to zero
    entity("ac_ep_prov", "ac-prov", "FOUNDATION_MODEL", foundation_model_config={"min_provisioned_throughput": 950, "max_provisioned_throughput": 1900})
    entity("ac_ep_ext", "ac-ext", "EXTERNAL_MODEL", external_model_config={"provider": "openai"}, task="llm/v1/chat")
    entity("ac_ep_gpu", "ac-gpu", "CUSTOM_MODEL", custom_model_config={"min_concurrency": 4, "max_concurrency": 8, "compute_type": "GPU_MEDIUM"})
    for eid, name, usage_type, tier in (("ac_ep_prov", "ac-prov", "GPU_TIME", "FOUNDATION_MODEL"), ("ac_ep_gpu", "ac-gpu", "GPU_TIME", "MODEL")):
        for d in range(1, 15):
            bill(con, f"ac_b_{eid}_{d}", ws, SKU_SERVING, DT(d, 0), 24.0, 20, tags=LABTAG, product="MODEL_SERVING", usage_type=usage_type, serverless=True,
                 features={"serving_type": tier}, endpoint_id=eid, endpoint_name=name)
    for d in range(1, 8):
        bill(con, f"ac_b_ext_{d}", ws, SKU_TOKENS, DT(d, 10), 1.0, 20, tags=LABTAG, product="MODEL_SERVING", usage_type="TOKEN", serverless=True,
             features={"serving_type": "EXTERNAL_MODEL"}, endpoint_id="ac_ep_ext", endpoint_name="ac-ext")
        for i in range(5):
            put(con, "serving__endpoint_usage", workspace_id=ws, account_id=ACCOUNT, client_request_id=f"ac_eq_{d}_{i}", databricks_request_id=f"ac_edq_{d}_{i}",
                requester="lab.app@acme.example", status_code=200, request_time=DT(d, 10, i), input_token_count=300, output_token_count=100,
                served_entity_id="ac_ep_ext_e")
    # E2 a deleted endpoint that billed before
    put(con, "serving__served_entities", served_entity_id="ac_ep_del_e", account_id=ACCOUNT, workspace_id=ws, created_by="lab.owner@acme.example",
        endpoint_name="ac-deleted", endpoint_id="ac_ep_del", served_entity_name="del-1", entity_type="CUSTOM_MODEL", entity_name="acme.ml.del",
        entity_version="1", endpoint_config_version=1, change_time=DT(40), endpoint_delete_time=DT(25),
        custom_model_config={"min_concurrency": 0, "max_concurrency": 2, "compute_type": "CPU"})
    for d in range(26, 40):
        bill(con, f"ac_b_epdel_{d}", ws, SKU_SERVING, DT(d, 0), 24.0, 3, tags=LABTAG, product="MODEL_SERVING", usage_type="GPU_TIME", serverless=True,
             features={"serving_type": "MODEL"}, endpoint_id="ac_ep_del", endpoint_name="ac-deleted")


def _storage(con) -> None:
    def table(cat, sch, name, ttype="MANAGED", gb=10, files=100, po=True, owner="lab_eng", days=(1,), altered=1, meta=True):
        if meta:
            put(con, "information_schema__tables", table_catalog=cat, table_schema=sch, table_name=name, table_type=ttype, table_owner=owner,
                created=DT(300), created_by="lab.owner@acme.example", last_altered=DT(altered), last_altered_by="lab.owner@acme.example",
                data_source_format="DELTA")
        for d in days:
            put(con, "storage__table_metrics_history", account_id=ACCOUNT, catalog_name=cat, schema_name=sch, table_name=name, table_id=f"ac_t_{name}",
                table_type=ttype, table_owner=owner, table_creation_time=DT(300), snapshot_date=D(d), active_bytes=int(gb * 10**9),
                active_files=files, predictive_optimization_enabled=po)
    # ST1 50,000 files of 0.4 MB; ST2 a managed table without predictive optimization in its own catalog; ST3 a big table nobody reads
    table("lab", "sales", "clicks", gb=20, files=50_000, po=False)
    table("lab", "sales", "no_po", gb=40, files=400, po=False)
    table("lab", "sales", "archive", gb=1500, files=15_000, po=True, altered=200)
    table("lab", "sales", "archive_xl", gb=6000, files=60_000, po=True, altered=250)
    # ST5 a table growing threefold in 14 days
    for d in range(1, 15):
        table("lab", "sales", "growing", gb=30 + 60 * (14 - d) / 13, files=300, days=(d,), meta=d == 1)
    # ST4 a vacuum that reclaims space, repeated permission failures, column choices that keep moving
    def po(op, status, metrics, table_name, day, dbus=1.0):
        put(con, "storage__predictive_optimization_operations_history", account_id=ACCOUNT, workspace_id=LAB, start_time=DT(day, 3), end_time=DT(day, 3, 20),
            metastore_name="acme", metastore_id="ac_ms", catalog_name="lab", schema_name="sales", table_name=table_name, table_id=f"ac_t_{table_name}",
            operation_type=op, operation_id=f"ac_po3_{op}_{table_name}_{day}", operation_status=status, operation_metrics=metrics,
            usage_unit="ESTIMATED_DBU", usage_quantity=dbus)
    po("VACUUM", "SUCCESSFUL", {"number_of_deleted_files": "5000", "amount_of_data_deleted_bytes": str(200 * 10**9)}, "archive", 3, 4.0)
    for day in range(1, 6):
        po("COMPACTION", "FAILED: INSUFFICIENT_PERMISSIONS", {}, "clicks", day)
    for day in range(2, 8):  # the same switch six times, and one three times
        po("AUTO_CLUSTERING_COLUMN_SELECTION", "SUCCESSFUL", {"has_column_selection_changed": "true", "old_clustering_columns": "c1",
                                                              "new_clustering_columns": "c2"}, "growing", day)
    for day in (9, 10, 11):
        po("AUTO_CLUSTERING_COLUMN_SELECTION", "SUCCESSFUL", {"has_column_selection_changed": "true", "old_clustering_columns": "a",
                                                              "new_clustering_columns": "b"}, "clicks", day)
    # backfills that scan a lot and add nothing; vacuums that reclaim nothing
    for tname, gb in (("growing", 700), ("clicks", 150)):
        po("DATA_SKIPPING_COLUMN_SELECTION", "SUCCESSFUL", {"amount_of_scanned_bytes": str(gb * 10**9), "added_data_skipping_columns": "",
                                                           "removed_data_skipping_columns": "", "new_data_skipping_columns": ""}, tname, 2)
    po("VACUUM", "SUCCESSFUL", {"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"}, "no_po", 2, 6.0)
    po("VACUUM", "SUCCESSFUL", {"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"}, "growing", 2, 25.0)
    # E2 a volume with PII outside any table
    put(con, "information_schema__volumes", volume_catalog="lab", volume_schema="raw", volume_name="exports", volume_type="MANAGED", volume_owner="lab_eng",
        created=DT(100), created_by="lab.owner@acme.example", last_altered=DT(20))
    put(con, "information_schema__volume_tags", catalog_name="lab", schema_name="raw", volume_name="exports", tag_name="pii", tag_value="customer exports")


def _access(con) -> None:
    ws = LAB
    # A1 fifty failed logins from one address for one user, and sign-ins from many addresses
    for i in range(50):
        audit(con, f"ac_a_brute_{i}", ws, DT(1, 3, i), "accounts", "login", email="target@acme.example", status=401, ip="198.51.100.77")
    for i in range(30):
        audit(con, f"ac_a_spread_{i}", ws, DT(2, 8, i), "accounts", "login", email=f"traveler{i}@acme.example", ip=f"192.0.2.{i + 10}")
    # A2 twenty people read the PII table; column lineage carries email into an untagged table
    for i in range(20):
        who = "intern@acme.example" if i == 0 else f"reader{i}@acme.example"
        put(con, "access__table_lineage", account_id=ACCOUNT, workspace_id=b.PROD, entity_type="NOTEBOOK", entity_id=f"ac_nb_r{i}",
            source_table_full_name="acme.sales.customers", source_table_catalog="acme", source_table_schema="sales", source_table_name="customers",
            source_type="TABLE", created_by=who, event_time=DT(1 + i % 5, 10, i), event_date=D(1 + i % 5), record_id=f"ac_l2_{i}", event_id=f"ac_le2_{i}",
            direct_access=True)
    for d in (1, 2):
        put(con, "access__column_lineage", account_id=ACCOUNT, workspace_id=ws, entity_type="JOB", entity_id="ac_job_regress",
            source_table_full_name="acme.sales.customers", source_table_catalog="acme", source_table_schema="sales", source_table_name="customers",
            source_type="TABLE", source_column_name="email", target_table_full_name="lab.sales.export_tmp", target_table_catalog="lab",
            target_table_schema="sales", target_table_name="export_tmp", target_type="TABLE", target_column_name="mail",
            created_by="svc@acme.example", event_time=DT(d, 2, 10), event_date=D(d), direct_access=True)
    for i in range(25):  # one reader opening the PII table 25 times
        put(con, "access__table_lineage", account_id=ACCOUNT, workspace_id=b.PROD, entity_type="NOTEBOOK", entity_id="ac_nb_heavy",
            source_table_full_name="acme.sales.customers", source_table_catalog="acme", source_table_schema="sales", source_table_name="customers",
            source_type="TABLE", created_by="reader1@acme.example", event_time=DT(1 + i % 10, 14, i), event_date=D(1 + i % 10),
            record_id=f"ac_l2h_{i}", event_id=f"ac_le2h_{i}", direct_access=True)
    for i in range(60):  # a sensitive column flowing to an untagged one, driven by twelve people
        put(con, "access__column_lineage", account_id=ACCOUNT, workspace_id=ws, entity_type="NOTEBOOK", entity_id="ac_nb_export",
            source_table_full_name="acme.sales.customers", source_table_catalog="acme", source_table_schema="sales", source_table_name="customers",
            source_type="TABLE", source_column_name="email", target_table_full_name="lab.sales.export_tmp", target_table_catalog="lab",
            target_table_schema="sales", target_table_name="export_tmp", target_type="TABLE", target_column_name="mail",
            created_by=f"exporter{i % 12}@acme.example", event_time=DT(1 + i % 20, 5, i % 60), event_date=D(1 + i % 20), direct_access=True)
    # A3 a dimension table that twelve tables and two dashboards are built from
    for i in range(12):
        put(con, "access__table_lineage", account_id=ACCOUNT, workspace_id=ws, entity_type="JOB", entity_id="ac_job_regress",
            source_table_full_name="lab.sales.core_dim", source_table_catalog="lab", source_table_schema="sales", source_table_name="core_dim",
            source_type="TABLE", target_table_full_name=f"lab.mart.t{i}", target_table_catalog="lab", target_table_schema="mart", target_table_name=f"t{i}",
            target_type="TABLE", created_by="svc@acme.example", event_time=DT(1, 4, i), event_date=D(1), record_id=f"ac_l3_{i}", event_id=f"ac_le3_{i}",
            direct_access=False)
    # A4 an enforced outbound policy dropping many requests, and a burst of inbound denials
    for i in range(400):
        put(con, "access__outbound_network", account_id=ACCOUNT, workspace_id=ws, destination_type="DNS", destination=f"host{i % 25}.unknown.example",
            event_time=DT(1, 12, i % 60), access_type="DROP", event_id=f"ac_o2_{i}", network_source_type="SERVERLESS")
    for i in range(210):  # two addresses 75 times each (CRITICAL) and six addresses 10 times each (WARN)
        ip = f"203.0.113.{i % 2}" if i < 150 else f"203.0.113.{10 + i % 6}"
        put(con, "access__inbound_network", account_id=ACCOUNT, workspace_id=ws, event_id=f"ac_in2_{i}", request_path="/api/2.0/sql/statements",
            source={"ip": ip}, authenticated_as="unknown", event_time=DT(1, 21, i % 60), policy_outcome="DENY", rule_label="office-only")
    # A5 schema tags, a schema shared, an external location privilege
    put(con, "information_schema__schema_tags", catalog_name="lab", schema_name="sales", tag_name="owner", tag_value="lab_eng")
    put(con, "information_schema__schema_tags", catalog_name="acme", schema_name="sales", tag_name="domain", tag_value="sales")
    put(con, "information_schema__schema_share_usage", catalog_name="lab", schema_name="sales", share_name="ac_partner_share", shared_as_schema="sales")
    put(con, "information_schema__external_location_privileges", grantee="account users", privilege_type="READ FILES", table_name="ac_landing")
    put(con, "information_schema__credential_privileges", grantee="lab_eng", privilege_type="ACCESS")
    put(con, "information_schema__connection_privileges", grantee="account users", privilege_type="USE CONNECTION")
    # A6 admin changes in a second workspace and at account level (workspace 0)
    audit(con, "ac_a_adm2", UAT, DT(2, 9), "accounts", "setAdmin", params={"targetUserName": "uat.user@acme.example"})
    audit(con, "ac_a_adm3", "0", DT(2, 10), "accounts", "addPrincipalToGroup", level="ACCOUNT_LEVEL", params={"targetGroupName": "account admins"})
    # A7 display names and an email inside request parameters; IPs and hosts must stay as they are
    audit(con, "ac_a_mask", ws, DT(1, 15), "jobs", "runNow", email="lab.owner@acme.example", ip="192.0.2.55", run_as="lab.owner@acme.example",
          run_as_name="Lab Owner", params={"notify": "lab.owner@acme.example", "host": "lab-gateway.acme.example"})
    # A8 low-confidence results, a classified column with a mask and one without
    for col, tag, conf in (("mail", "class.email_address", "LOW"), ("name", "class.name", "LOW"), ("card", "class.credit_card", "HIGH"),
                           ("iban", "class.iban", "HIGH")):
        put(con, "data_classification__results", catalog_name="lab", schema_name="sales", table_name="users", column_name=col, class_tag=tag,
            confidence=conf, first_detected_time=DT(30), latest_detected_time=DT(1))
    put(con, "information_schema__tables", table_catalog="lab", table_schema="sales", table_name="users", table_type="MANAGED", table_owner="lab_eng",
        created=DT(300), created_by="lab.owner@acme.example", last_altered=DT(3), last_altered_by="lab.owner@acme.example", data_source_format="DELTA")
    put(con, "information_schema__column_masks", table_catalog="lab", table_schema="sales", table_name="users", column_name="card", mask_name="lab.sec.mask_card")
