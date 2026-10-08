// section 5.2/5.3 of the redesign brief: a plain display title
// and one-line "why it matters" for every check the redesign places (tab_registry.ts's
// AREA_REGISTRY), and a word for every enum code the UI would otherwise print raw (termination
// codes, not-assessed reason codes). Compute-pressure verdicts (WAITING_OR_IDLE, MEMORY_PRESSURE,
// ...) already have their own map in pressure.tsx (T-65) -- not duplicated here.
//
// getCheckLabel(queryId, fallbackTitle) is the one function every screen calls: a query_id with an
// entry below gets that title/why; one without falls back to the finding's own `.title` (from
// GET /api/findings, already a plain sentence) with no why line, rather than inventing one.

import { PRESSURE_LABELS } from "./pressure";

/** A check's plain title and its one-line why (null when none was written). */
export interface CheckLabel {
  title: string;
  why: string | null;
}

export const CHECK_LABELS: Record<string, CheckLabel> = {
  // ── Cost ──────────────────────────────────────────────────────────────────────────────────
  cost_daily_spikes: { title: "Spend spikes", why: "A day costing 2× or more its usual (trailing median) for that workspace and product, and at least $50." },
  cost_period_over_period: { title: "Change vs previous period", why: "A workspace and product line up 25% or more on the previous 30 days." },
  cost_dollarized_by_sku_day: { title: "Daily spend by SKU", why: "Every billing row priced at effective list, by day and SKU -- the one $ basis every other cost figure here is built from." },
  cost_monthly_actuals: { title: "Spend by calendar month", why: "The same daily spend rolled up to a month, so a partial current month is never mistaken for a full one." },
  cost_totals_by_sku_day: { title: "Spend by product, raw rows", why: "The daily-by-SKU rows behind the Daily spend chart, for export." },
  cost_workspace_names: { title: "Workspace names", why: "Feeds every workspace name shown elsewhere in the app; not a check on its own." },
  cost_by_billing_origin_product: { title: "Spend by product", why: "Warehouses, jobs, all-purpose clusters and the rest, each as its own share of spend." },
  cost_premium_serverless_photon: { title: "Serverless & Photon share", why: "How much spend sits on premium-priced serverless/Photon SKUs -- worth checking it is intentional." },
  cost_chargeback_by_allocation_tag: { title: "Spend with no cost center or team tag", why: "Share of a workspace's spend that can't be charged back; flagged from 20%. Fix: tag the resource or its serverless usage policy." },
  cost_chargeback_by_tag: { title: "Spend by cost center or team", why: "The same spend split by whichever tag key a workspace actually carries." },
  cost_chargeback_by_identity: { title: "Spend by who ran it", why: "DBUs by the identity on the billing row -- a person, or 'not recorded' for model serving and SQL warehouses, which bill to the endpoint rather than the caller." },
  cost_dbsql_allocation_gap: { title: "SQL spend that can't be split by query", why: "Warehouse spend with no matching query history to attribute it to a statement or user." },
  cost_by_job: { title: "Spend by job", why: "Jobs ranked by spend -- a size ranking, not a threshold check." },
  cost_by_compute_resource: { title: "Spend by cluster or warehouse", why: "Compute resources ranked by spend -- a size ranking, not a threshold check." },
  cost_by_notebook: { title: "Notebooks over the daily DBU limit", why: "A notebook over the daily DBU limit warns, and far over it is critical: a runaway ad-hoc notebook on an all-purpose cluster." },
  cost_default_storage_dsu: { title: "Managed storage spend", why: "Delta Sync/serving storage DSU spend, by workspace." },
  cost_genai_token_gpu: { title: "GenAI token & GPU spend", why: "Foundation-model token and provisioned-GPU spend, by endpoint." },
  cost_by_serving_endpoint: { title: "Spend by serving endpoint", why: "Model-serving and vector-search spend, by endpoint." },
  cost_usage_policy_coverage: { title: "Serverless usage without a policy", why: "Serverless usage with no budget/usage policy attached, so it carries no cost-center tag either." },
  cost_actual_vs_list_by_sku: { title: "Actual price vs list price", why: "Where a negotiated or discounted rate diverges from the list price this app otherwise assumes." },
  cost_networking_egress: { title: "Cross-region networking spend", why: "Data-transfer/egress SKUs, which a workspace region choice can usually avoid." },
  cost_cloud_infra: { title: "Cloud infrastructure spend", why: "All Databricks spend by cloud, not your cloud provider's bill." },
  cost_restatement_trust_metric: { title: "How much of last month's bill has since changed", why: "Cloud billing can restate a closed day; this measures how much of the prior month has moved since it first closed." },
  cost_unnamed_workspaces: { title: "Spend on workspaces with no name", why: "A workspace billed in the last 365 days but missing from the account's own workspace list -- deleted, or outside what this account can see." },
  cost_sku_trend_12m: { title: "Fastest-growing SKUs", why: "A SKU up 100% or more over its last 3 months (50% is a warning), or a new SKU with $500+ of spend in its first 3 months." },
  cost_by_hour_of_day: { title: "Spend concentrated by hour of day", why: "One hour of the day holding 25% or more of a workspace's spend across every weekday -- a peak to spread out, not a cheaper time to run." },
  // ── Cost > Chargeback ────────────────────────────────────────────────────────────────────
  cost_chargeback_by_workspace: { title: "Chargeback by workspace", why: "Each workspace's spend, current period versus the one before; flagged from 25% up." },
  cost_chargeback_by_service: { title: "Chargeback by product line", why: "Spend by product line -- warehouses, jobs, serving and the rest -- current period versus the one before; flagged from 25% up." },
  cost_chargeback_by_sku: { title: "Chargeback by SKU", why: "Spend by SKU, current period versus the one before; flagged from 25% up." },
  cost_chargeback_by_warehouse: { title: "Chargeback by warehouse", why: "Each SQL warehouse's spend and cost per 1,000 queries, current period versus the one before; flagged from 25% up." },
  cost_chargeback_by_job: { title: "Chargeback by job", why: "Each job's spend, current period versus the one before; flagged from 25% up." },
  cost_chargeback_by_cluster: { title: "Chargeback by cluster, job cluster or pipeline", why: "Each all-purpose cluster, job's job clusters, or classic pipeline, current period versus the one before; flagged from 25% up." },
  cost_chargeback_identity_by_source: { title: "Chargeback by identity", why: "Spend by the user or service principal who ran it, split by SQL warehouse, jobs or other compute." },
  cost_chargeback_by_tag_value: { title: "Chargeback by tag value", why: "Spend by every value of every custom tag key your account uses, plus its own (other) and (untagged) rows." },
  cost_daily_by_resource: { title: "Daily cost by resource", why: "Each workspace's, warehouse's, job's, all-purpose cluster's and pipeline's DBUs, dollars, hours and runs billed by day, for a before-and-after comparison." },
  lakeflow_daily_state: { title: "Jobs and pipelines, day by day", why: "Each job and pipeline per day: ran fine, failed, ran slow or skipped, with run counts and times; behind the state lines on Jobs > Failures and the run-time list." },
  perf_queue_by_hour: { title: "Queueing by day and hour", why: "SQL warehouse statements per day and UTC hour: how many waited for a slot or for compute; behind the weekday-by-hour grid on Queries > Capacity." },
  perf_daily_by_resource: { title: "Daily query, job and pipeline performance", why: "Each warehouse's queries, job's runs and pipeline's updates by day: count, failures, time, queue wait, start-up and spill, for a before-and-after comparison." },
  perf_failure_causes_daily: { title: "Daily failures by cause", why: "Failed queries per warehouse by error class and failed job runs by termination code, by day, to see which cause rose or fell." },
  cost_chargeback_reconcile: { title: "Chargeback self-check", why: "Whether the chargeback cuts above still add up to the account's own billing total; a gap of 5% or more is critical." },
  // ── ML & AI ───────────────────────────────────────────────────────────────────────────────
  cost_serving_mode_by_endpoint: { title: "Serving endpoints over the daily spend limit", why: "Endpoint-days at or above the daily $ limit (Warn, then Critical) at list price; a high share of scale-from-zero launches signals churn." },
  cost_vector_search_spend: { title: "Vector Search spend", why: "Vector Search index and query spend, by endpoint." },
  compute_serving_dormant_endpoints: { title: "Dormant serving endpoints", why: "Endpoints with no recent traffic -- still billed if provisioned. Deleted endpoints and Databricks' own pay-per-token ones are left out." },
  genie_usage: { title: "Genie usage by user", why: "Genie DBUs and dollars by user, workspace, kind of use and channel, this period and the one before." },
  compute_serving_endpoint_cost_status: { title: "Endpoints billed with no requests", why: "Serving endpoints that billed compute this window with zero requests. Possible waste." },
  compute_serving_endpoint_usage: { title: "Endpoint requests & errors", why: "Request volume, error rate and $/request per endpoint." },
  access_vector_search_traffic: { title: "Vector Search query traffic", why: "Who is querying each Vector Search index, and how often." },
  serving_endpoint_traffic_by_endpoint: { title: "Endpoint traffic, raw rows", why: "Daily request/latency rows behind the endpoint cards, for export." },
  compute_ai_gateway_usage: { title: "AI Gateway usage", why: "Requests routed through the AI Gateway, by endpoint: how many succeeded, were rate-limited or errored." },
  // ── Compute ───────────────────────────────────────────────────────────────────────────────
  compute_warehouse_idle_minutes: { title: "Warehouse idle time", why: "A warehouse billed while running with no query for longer than the idle gap in the thresholds file. Possible waste." },
  compute_warehouse_idle_gaps: { title: "Longest idle stretch per warehouse", why: "The single longest unbroken idle stretch on a running warehouse -- a second view of the same idle-time signal." },
  compute_warehouse_autoscale_churn: { title: "Warehouse autoscale churn", why: "How often a warehouse's cluster count changes -- frequent churn can mean the min/max size is set too tight. Counts starts and stops, not time." },
  compute_warehouse_autostop_churn: { title: "Warehouse auto-stop churn", why: "A warehouse auto-stopping often with a long wait first, or a classic/pro warehouse cold-starting often; 5+ qualifying auto-stops a day warns, 10+ is critical." },
  compute_warehouse_cache_reuse: { title: "Warehouse result-cache reuse", why: "A warehouse kept warm with a long auto-stop but actually serving mostly one-off queries, not repeat ones the result cache could serve." },
  sql_warehouse_events_activity: { title: "Warehouse start/stop activity", why: "How often each warehouse starts and stops, and how long each run lasts." },
  sql_warehouse_config_current: { title: "Warehouse configuration, current", why: "Every warehouse's current size, type and auto-stop setting, for reference." },
  compute_idle_node_ratio: { title: "Idle cluster nodes", why: "Classic cluster minutes with almost no CPU in use. Possible waste." },
  node_timeline_utilization: { title: "Cluster node utilization over time", why: "CPU/memory utilization per cluster node, minute by minute." },
  instance_pools_idle_capacity: { title: "Idle warm-pool capacity", why: "Warm-pool instances held idle. The idle VMs bill on your cloud bill, not here; the $ shown is the DBU spend of clusters that used the pool." },
  instance_events_idle_active: { title: "Pool instance idle/active time", why: "How long each pool instance spent idle vs actively attached to a cluster." },
  classic_clusters_config_current: { title: "Cluster configuration, current", why: "Every classic cluster's current node type, autoscale range and policy, for reference." },
  node_types_reference: { title: "Node type reference", why: "The instance-type catalogue (cores, memory, family) the other compute checks resolve names against." },
  compute_cluster_config_posture: { title: "Cluster setup risks", why: "Clusters on an end-of-support runtime, a Unity-Catalog-bypassing access mode, no policy, or auto-termination off or too high." },
  compute_warehouse_config_posture: { title: "Warehouse setup risks", why: "Warehouses with auto-stop off or too high, on the Preview channel, or of the deprecated Classic type." },
  // ── Jobs ──────────────────────────────────────────────────────────────────────────────────
  lakeflow_job_reliability: { title: "Broken or flaky jobs", why: "A job whose recent runs failed back to back, or whose failure rate crossed the threshold." },
  lakeflow_failed_jobs_wasted_dbus: { title: "Compute burned on failed runs", why: "Job runs that failed, and failed attempts later repaired: compute that produced no result. Possible waste; some is unavoidable." },
  lakeflow_failed_runs: { title: "Failed job runs", why: "Every job run that ended in a non-success terminal state, with its reason." },
  lakeflow_never_started_runs: { title: "Runs that never started", why: "Scheduled or triggered runs that never reached RUNNING -- usually a cluster or queue problem, not the job's own code." },
  lakeflow_succeeded_with_failed_tasks: { title: "Runs that 'succeeded' with a failed task", why: "A run reported success while at least one of its tasks failed -- worth checking task-level dependencies." },
  lakeflow_termination_taxonomy: { title: "Termination reasons, reference", why: "Every termination code this account has produced and how often, for reference." },
  lakeflow_job_run_changes: { title: "What changed since the last good run", why: "Runtime, cluster shape, job edits, upstream tables and input size between a job's last good run and its latest." },
  lakeflow_termination_type_probe: { title: "Termination code probe (developer)", why: "An internal probe of the termination-code taxonomy; not a check for end users." },
  lakeflow_job_duration_regression: { title: "Jobs running slower than usual", why: "A job whose median successful run in the last 7 days took 1.5x or longer than in the rest of the window; 2x is critical." },
  lakeflow_failed_cluster_starts: { title: "Failed cluster starts", why: "Job task runs that failed before their code ran, most often while the cluster was starting, by termination code." },
  lakeflow_job_queue_time: { title: "Jobs waiting on compute", why: "Time a job's runs spent queued before compute was available." },
  lakeflow_phase_cold_start: { title: "Cluster cold-start time", why: "95th-percentile start-up time of a job's successful task runs, before their code began; failed starts are listed separately." },
  lakeflow_workload_mix_hours: { title: "Job workload by hour of day", why: "When job workload actually runs, hour by hour -- for reference against capacity." },
  lakeflow_long_running_runs: { title: "Long-running runs", why: "Runs taking much longer than the job's own median -- the first step into a compute-fit drill-down." },
  lakeflow_retries_repairs: { title: "Jobs relying on retries or repairs", why: "A job that only succeeds after an automatic retry or a manual repair run -- a reliability smell even when it eventually passes." },
  lakeflow_stale_zombie_jobs: { title: "Stale jobs with no recent run", why: "An active job with no run at all -- success or failure -- in a long time, or never; likely abandoned but still configured." },
  lakeflow_jobs_no_timeout: { title: "Jobs with no timeout set", why: "A job with no timeout configured can run (and bill) indefinitely if it hangs." },
  lakeflow_job_tasks_no_timeout: { title: "Job tasks with no timeout set", why: "Same risk as above, at the individual task level." },
  lakeflow_jobs_on_all_purpose: { title: "Jobs on all-purpose clusters", why: "Scheduled jobs running on interactive (all-purpose) pricing instead of cheaper job-cluster pricing." },
  lakeflow_health_rule_coverage: { title: "Jobs with no health rule", why: "A job with no configured health rule has nothing watching its run duration for you." },
  lakeflow_job_ownership_orphans: { title: "Jobs with no clear owner", why: "A job whose creator/owner no longer looks active -- worth reassigning before it breaks unnoticed." },
  lakeflow_tasks_near_timeout: { title: "Tasks running close to their timeout", why: "A task regularly finishing close to its configured timeout -- one slow day away from failing outright." },
  lakeflow_job_cost_summary: { title: "Job cost summary, reference", why: "Per-job spend, for the job run panel; not a status check on its own." },
  lakeflow_job_run_cost: { title: "Per-run cost, reference", why: "Per-run spend behind the job run panel; not a status check on its own." },
  lakeflow_job_recent_runs: { title: "Recent runs, reference", why: "Last N runs per job with duration and the failing task, behind the job run panel; not a status check on its own." },
  lakeflow_job_compute_pressure: { title: "Job compute fit", why: "Whether a job's compute is under memory pressure, idle, driver-bound or otherwise a poor fit for its workload." },
  task_cluster_utilization: { title: "Task-level cluster utilization", why: "The same compute-fit read at the individual task-run level, for runs over two hours." },
  lakeflow_job_oversized: { title: "Jobs on bigger machines than they need", why: "A job whose workers run well under capacity across its recent runs, even at their busiest memory minute and with no swap -- the opposite of Job compute fit." },
  lakeflow_pipeline_cost: { title: "Spend by pipeline", why: "Pipelines ranked by spend -- a size ranking, not a threshold check." },
  lakeflow_pipeline_idle_tail_duration: { title: "Pipeline compute left running after its last update", why: "Compute that may linger after a pipeline's last update completes." },
  lakeflow_pipeline_update_failures_retries: { title: "Pipeline update failures & retries", why: "Pipeline updates that failed or needed a retry to complete." },
  lakeflow_pipelines_inventory_tier: { title: "Pipeline inventory, reference", why: "Every configured pipeline and its tier, for reference." },
  // ── Queries ───────────────────────────────────────────────────────────────────────────────
  query_costly_statements_grouped: { title: "Heaviest statement shapes", why: "SQL statements grouped by fingerprint, ranked by total cost -- the same query run many times counts once." },
  query_top_by_cost: { title: "Top queries by cost", why: "SQL-warehouse statements grouped by saved query, dashboard, job or text, ranked by their share of the warehouse's own spend; flagged at $200 or 40% of the warehouse." },
  query_provenance_by_source: { title: "Query traffic by source", why: "Statement volume split by how it was submitted (notebook, job, BI tool, API)." },
  audit_self_cost: { title: "This audit's own query cost", why: "The cost of the queries this audit itself runs against system tables -- kept small on purpose." },
  query_costly_statements: { title: "All statements, reference", why: "Every statement this window, priced -- mostly OK rows; kept as a reference list, not a check." },
  query_per_query_estimate_lane: { title: "Per-query cost estimate, reference", why: "The per-statement price basis the heavy-query views are built from." },
  query_local_spillage: { title: "Queries spilling to local disk", why: "A query shuffling or sorting more data than memory holds, spilling to disk and slowing down." },
  query_shuffle_write_amplification: { title: "Queries shuffling too much data", why: "A query moving far more data across the cluster network than its output size would suggest." },
  query_pruning_effectiveness: { title: "Queries not pruning partitions/files", why: "A query scanning far more files or partitions than it should for the rows it actually returned." },
  query_cache_coldstart: { title: "Queries missing the disk cache", why: "A query re-reading data that should have been served from the warehouse's local cache." },
  query_failed_queries_daily: { title: "Failed queries per day", why: "SQL statements that failed, by day and warehouse." },
  query_failed_statements_grouped: { title: "Failed statements by error", why: "Failed SQL statements grouped by error class and source; canceled ones counted apart." },
  cost_failed_statement_waste: { title: "Compute burned on failed statements", why: "SQL statements that failed after using billed compute. Possible waste; some is unavoidable." },
  query_warehouse_pressure: { title: "Warehouse memory & concurrency pressure", why: "Whether a warehouse is short of memory or hitting concurrency limits under its current load." },
  query_queuing_waits: { title: "Queries waiting for a warehouse slot", why: "Time statements spent queued before a warehouse could run them." },
  query_workload_mix_hours: { title: "Query workload by hour of day", why: "When query workload actually runs, hour by hour -- for reference against capacity." },
  query_task_statement_breakdown: { title: "Statement breakdown by task stage", why: "Where a statement's time actually went: planning, execution, or result fetch." },
  // ── Governance & PII ──────────────────────────────────────────────────────────────────────
  access_broad_grants: { title: "Overly broad grants", why: "ALL PRIVILEGES given to a person, or access given to every user in the account." },
  access_grants_inventory: { title: "Grants inventory", why: "Every active grant, for reference." },
  access_grants_inventory_extended: { title: "Grants inventory, extended", why: "The same inventory with inherited/effective grants included." },
  access_runas_escalation: { title: "Run-as identity escalation", why: "A job or query running as a more privileged identity than the person who triggered it." },
  access_login_concentration: { title: "Login concentration", why: "Whether access is spread across many people or concentrated in very few accounts." },
  access_network_inbound_denials: { title: "Inbound network denials", why: "Connections blocked by an IP access list or network policy, inbound." },
  access_network_outbound_denials: { title: "Outbound network denials", why: "Connections blocked by an egress/network policy, outbound." },
  access_admin_role_change_events: { title: "Admin and permission changes", why: "Admin roles, grants, settings and token permissions; a token permission grant warns, and in prod is critical." },
  access_classification_coverage: { title: "Sensitive-data classification coverage", why: "Share of tables that carry any data classification tag at all." },
  access_classified_unmasked: { title: "Classified data with no mask", why: "A column tagged as sensitive that carries no column mask or row filter." },
  access_sensitive_table_reads: { title: "Reads of sensitive tables", why: "Who read a table classified as sensitive, and how often." },
  access_pii_outside_tables: { title: "PII outside of tagged tables", why: "Volumes and schemas outside governed tables: tagged sensitive, or untagged and never classified. An untagged external volume flags critical by policy, not because PII was found." },
  access_data_classification_inventory: { title: "Data classification inventory", why: "Every table's classification tags, for reference." },
  access_column_masks_inventory: { title: "Column masks inventory", why: "Every column mask currently applied, for reference." },
  access_row_filters_inventory: { title: "Row filters inventory", why: "Every row filter currently applied, for reference." },
  access_tags_inventory: { title: "Table & column tags inventory", why: "Every Unity Catalog tag currently applied, for reference." },
  access_table_lineage_blast_radius: { title: "Table lineage blast radius", why: "How many downstream tables and jobs would be affected if a given table changed or broke." },
  access_column_lineage_sensitive_reach: { title: "Sensitive column lineage reach", why: "How far a sensitive column's data actually propagates downstream through views and derived tables." },
  access_pii_propagation_untagged: { title: "PII propagating into untagged tables", why: "A sensitive column's lineage reaching a downstream table that carries no matching tag." },
  access_views_inventory: { title: "Views inventory", why: "Every view and its base tables, for reference." },
  access_volumes_inventory: { title: "Volumes inventory", why: "Every volume with its type, owner, location and tags; an untagged external volume flags critical." },
  access_delta_sharing_exposure: { title: "Delta Sharing exposure", why: "What is shared externally via Delta Sharing, and to whom." },
  // ── Storage ───────────────────────────────────────────────────────────────────────────────
  storage_target_table_discovery: { title: "Table discovery", why: "Every managed table this audit can see, as the base list the other storage checks work from." },
  table_inventory_type: { title: "Table inventory by type", why: "Table counts by schema and table type (managed, external, view)." },
  access_dead_table_candidates: { title: "Tables not read or changed recently", why: "Of the tables never read in Unity Catalog lineage this window, those not altered for a long time. Reads outside Unity Catalog don't show, so check before dropping." },
  po_maintenance_cost_by_table: { title: "Maintenance spend by table", why: "OPTIMIZE/VACUUM/predictive-optimization compute spend, by table." },
  po_vacuum_reclaimed_bytes: { title: "Storage reclaimed by VACUUM", why: "Bytes actually reclaimed by VACUUM runs, by table." },
  po_clustering_activity: { title: "Liquid clustering activity", why: "How often and how much a table's liquid clustering has run." },
  po_clustering_column_churn: { title: "Clustering column changes", why: "How often a table's clustering columns have been changed -- frequent churn undoes earlier clustering work." },
  po_data_skipping_backfill: { title: "Data-skipping statistics backfill", why: "Whether a table's file statistics are current enough for data skipping to actually help queries." },
  po_failure_reasons: { title: "Predictive optimization failures", why: "Tables where OPTIMIZE, VACUUM or clustering keeps failing, by reason, with the DBUs spent on the failed attempts." },
  storage_growth: { title: "Fastest-growing tables", why: "Tables with a size history this window: growing fastest, dropped, or with no owner; flagged from 20% growth." },
  storage_po_coverage: { title: "Predictive optimization coverage", why: "Share of each catalog's tables and bytes with predictive optimization off, plus the largest tables still missing it." },
  storage_small_files: { title: "Tables with too many small files", why: "A table with 1,000+ files averaging under 32MB slows scans and inflates listing cost; under 8MB is critical." },
  // ── Overview roll-up reference feeds ──────────────────────────────────────────────────────
  overview_spend_estimate: { title: "Estimated spend, reference", why: "DBU spend only, at list price (effective); the Overview cards use every billed unit." },
  overview_dbu_by_sku: { title: "DBUs by SKU, reference", why: "Net DBU usage by SKU, for the account-shape numbers on Overview." },
  overview_serverless_classic_split: { title: "Serverless vs classic split, reference", why: "Serverless share of usage, for the account-shape numbers on Overview." },
  overview_daily_dbu_trend: { title: "Daily DBU trend, reference", why: "Net DBUs by day, account-wide, for reference." },
  overview_list_prices_raw: { title: "List prices, raw, reference", why: "The list-price table every dollar figure in this app is priced from." },
  // ── Coverage & Gaps reference feeds ───────────────────────────────────────────────────────
  cost_account_prices_raw: { title: "Account-specific prices, raw", why: "Any account-specific negotiated prices found in the snapshot, for reference." },
  pricing_list_prices_raw: { title: "List prices, raw", why: "The published Databricks list-price table as captured in this snapshot." },
  access_source_table_coverage: { title: "System table coverage", why: "Which system tables this audit reads have fresh, gap-free data, and which have gone stale." },
  cost_audit_self_usage: { title: "This audit's own usage, by warehouse", why: "How much of each warehouse's traffic and spend this audit itself was, not your own workloads -- for reference." },
};

// Termination codes -- the Databricks Jobs API's fixed run-termination-code vocabulary, plus a
// few reason codes this library's own checks emit (not_assessed_reason values that are not
// already a compute-pressure verdict -- those are pressure.tsx's own map, T-65).
const ENUM_LABELS: Record<string, string> = {
  // job_run_timeline.result_state (a coarser, separate vocabulary from termination_code below)
  SUCCESS: "Succeeded",
  SUCCEEDED: "Succeeded",
  FAILED: "Failed",
  ERROR: "Error",
  BLOCKED: "Blocked",
  SKIPPED: "Skipped",
  USER_CANCELED: "Cancelled by user",
  USER_CANCELLED: "Cancelled by user",
  CANCELED: "Cancelled",
  CANCELLED: "Cancelled",
  TIMED_OUT: "Timed out",
  TIMEDOUT: "Timed out",
  // job_run_timeline.termination_code (root-cause detail)
  INVALID_RUN_CONFIGURATION: "Invalid run configuration",
  INTERNAL_ERROR: "Internal error",
  RUN_EXECUTION_ERROR: "Execution error",
  STORAGE_ACCESS_ERROR: "Storage access error",
  DRIVER_ERROR: "Driver error",
  CLUSTER_ERROR: "Cluster error",
  CLOUD_FAILURE: "Cloud provider failure",
  DRIVER_UNREACHABLE: "Driver unreachable",
  DRIVER_UNRESPONSIVE: "Driver unresponsive",
  REPOSITORY_CHECKOUT_FAILED: "Repo checkout failed",
  INVALID_CLUSTER_REQUEST: "Invalid cluster request",
  LIBRARY_INSTALLATION_ERROR: "Library install failed",
  WORKSPACE_RUN_LIMIT_EXCEEDED: "Workspace run limit reached",
  MAX_JOB_QUEUE_SIZE_EXCEEDED: "Job queue full",
  MAX_CONCURRENT_RUNS_REACHED: "Max concurrent runs reached",
  RESOURCE_NOT_FOUND: "A required resource was deleted",
  UNAUTHORIZED_ERROR: "Not authorized",
  BUDGET_POLICY_LIMIT_EXCEEDED: "Budget policy limit reached",
  SUCCESS_WITH_FAILURES: "Succeeded with a failed task",
  EXCLUDED: "Excluded",
  UPSTREAM_FAILED: "An upstream task failed",
  UPSTREAM_CANCELED: "An upstream task was cancelled",
  // reason codes (not_assessed_reason / no_baseline / no_cluster style values)
  no_cluster_recorded: "No cluster recorded",
  no_node_timeline_rows: "No node telemetry recorded",
  too_few_slices: "Too little overlapping telemetry to judge",
  task_not_executed: "Task never ran",
  no_baseline_history: "No earlier history to compare against",
  previous_window_not_covered: "Snapshot doesn't reach back far enough to compare",
  current_period_unpriced: "This window's spend could not be priced",
  no_negotiated_rate_source: "This account exposes no negotiated-rate source to compare against",
  // SQL warehouse size (system.compute.warehouses / warehouse config)
  "2X_SMALL": "2X-Small", X_SMALL: "X-Small", SMALL: "Small", MEDIUM: "Medium",
  LARGE: "Large", X_LARGE: "X-Large", "2X_LARGE": "2X-Large", "3X_LARGE": "3X-Large", "4X_LARGE": "4X-Large",
  // information_schema.tables.table_type
  MANAGED: "Managed table", EXTERNAL: "External table", VIEW: "View",
  MATERIALIZED_VIEW: "Materialized view", STREAMING_TABLE: "Streaming table", FOREIGN: "Foreign table",
};

export function enumLabel(code: unknown): string | null {
  if (code === null || code === undefined || code === "") return null;
  const key = String(code);
  return ENUM_LABELS[key] || PRESSURE_LABELS[key] || key;
}

// The one function every screen calls: CHECK_LABELS[queryId] when this pass wrote one, else the
// finding's own already-plain `.title` (GET /api/findings) with no why line -- never a fabricated
// explanation for a check this pass has not looked at yet.
export function getCheckLabel(queryId: string, fallbackTitle?: string | null): CheckLabel {
  const known = CHECK_LABELS[queryId];
  if (known) return known;
  return { title: fallbackTitle || queryId, why: null };
}
