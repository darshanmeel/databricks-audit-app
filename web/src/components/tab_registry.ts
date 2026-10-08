// the curated query_id lists behind every area of the
// redesign: AREA_REGISTRY maps area -> sub-tabs -> owned query_ids ("(ref)" ids become each sub-tab's own
// `refIds`; "Checks not placed above" is folded into its named home below). Badges, sub-tab
// badges, the checks table, All-findings links and the Guide's "browse by screen" cards all read
// this one map -- a page agent registers CONTENT here (AreaContent.register, primitives.tsx)
// without ever editing this file's own area/subtab/id lists.
//
// This file is DATA (+ the two small lookup builders at the bottom) so every area page and
// App.tsx can share one source of truth without importing each other's markup.
import type { StatusCounts } from "../types";

export interface Subtab {
  key: string;
  label: string;
  ids: string[];
  refIds: string[];
  linked?: string[];
}

export interface Area {
  label: string;
  scope: string;
  subtabs: Subtab[];
  refIds?: string[];
}

/** Where a check lives: its area and sub-tab. */
export interface Home {
  tab: string;
  subtab: string | null;
}

export type Band = "ERROR" | "NOT_ASSESSED" | "EMPTY_WINDOW" | "EMPTY_FILTERS" | "INVENTORY" | "RANKED" | "CRITICAL" | "WARN" | "OK";

// ─────────── The backend's own domain taxonomy (app/core/data.py), unrelated to and unchanged by
// this redesign's area/sub-tab reorganisation below -- every finding's `row.domain` value still
// comes from this vocabulary, and finding_detail.tsx + guide.tsx (neither owned by this pass)
// still read these two constants directly to label it. Kept exactly as before the redesign. ─────

const DOMAIN_LABELS: Record<string, string> = {
  cost: "Cost",
  compute: "Compute",
  jobs_pipelines: "Jobs & Pipelines",
  performance: "Query performance",
  governance_access: "Governance & Access",
  storage: "Storage",
  serving_ai: "ML & AI",
};
const DOMAIN_ORDER = ["cost", "compute", "jobs_pipelines", "performance", "governance_access", "storage", "serving_ai"];

// ─────────── Cost ─────────── (trend · product · allocation · resource · pricing)
const COST_SUBTABS: Subtab[] = [
  {
    key: "trend", label: "Trend",
    ids: ["cost_daily_spikes", "cost_period_over_period", "cost_by_hour_of_day"],
    refIds: ["cost_dollarized_by_sku_day", "cost_monthly_actuals", "cost_totals_by_sku_day", "cost_workspace_names"],
  },
  {
    key: "product", label: "By product & SKU",
    ids: ["cost_by_billing_origin_product", "cost_premium_serverless_photon", "cost_sku_trend_12m"],
    refIds: [],
  },
  {
    key: "allocation", label: "Allocation",
    ids: ["cost_chargeback_by_allocation_tag", "cost_chargeback_by_tag", "cost_chargeback_by_identity", "cost_dbsql_allocation_gap", "cost_unnamed_workspaces"],
    refIds: [],
  },
  {
    key: "chargeback", label: "Chargeback",
    // Nine app-owned chargeback cuts, current period vs. the one before it, each with its own
    // WARN/CRITICAL band -- the self-check (reconcile) sits last, after every cut it checks.
    ids: [
      "cost_chargeback_by_workspace", "cost_chargeback_by_service", "cost_chargeback_by_sku",
      "cost_chargeback_by_warehouse", "cost_chargeback_by_job", "cost_chargeback_by_cluster",
      "cost_chargeback_identity_by_source", "cost_chargeback_by_tag_value", "cost_chargeback_reconcile",
    ],
    refIds: [],
  },
  {
    key: "resource", label: "By resource",
    // cost_by_serving_endpoint's HOME is ML & AI ("Serving endpoint cost -> link to ML & AI") --
    // linked here, not owned, so it renders once and is never double-counted.
    ids: ["cost_by_job", "cost_by_compute_resource", "cost_by_notebook", "cost_default_storage_dsu", "cost_genai_token_gpu"],
    linked: ["cost_by_serving_endpoint"],
    refIds: [],
  },
  {
    key: "before_after", label: "Before & after",
    ids: [],
    refIds: ["cost_daily_by_resource", "perf_daily_by_resource", "perf_failure_causes_daily"],
  },
  {
    key: "pricing", label: "Pricing & policy",
    ids: ["cost_usage_policy_coverage", "cost_actual_vs_list_by_sku", "cost_networking_egress", "cost_cloud_infra", "cost_restatement_trust_metric"],
    // "Raw price tables -> Coverage reference tables": owned by Coverage, not Cost -- see
    // COVERAGE_AREA.refIds below.
    refIds: [],
  },
];

// ─────────── Waste & savings ─────────── (DEC-74: no checks table of its own -- every id here
// is homed on another area's own sub-tab; this is a roll-up that links out). `dollarCol`/`dbuCol`
// name the one column (DEC-68) that may carry a possible-waste figure; a check with neither ranks
// by severity alone. Domain keys are the new area keys (section 4.2), so a link built from this
// list always lands on a real tab.
export const WASTE_ITEMS = [
  { id: "compute_warehouse_idle_minutes", area: "compute", dollarCol: "est_wasted_usd_list", dbuCol: null, why: "Warehouse running with no query for more than a minute (the gap is set in config/thresholds.yml): billed while idle. Possible waste." },
  { id: "compute_idle_node_ratio", area: "compute", dollarCol: "est_wasted_usd_list", dbuCol: null, why: "Classic cluster minutes with almost no CPU in use: billed while idle. Possible waste." },
  { id: "lakeflow_failed_jobs_wasted_dbus", area: "jobs", dollarCol: "est_wasted_usd_list", dbuCol: "wasted_dbus", why: "Job runs that failed, and failed attempts later repaired: compute that produced no result. Possible waste; some is unavoidable." },
  { id: "cost_failed_statement_waste", area: "queries", dollarCol: "est_wasted_usd_list", dbuCol: "failed_dbus", why: "SQL statements that failed after using billed compute. Possible waste; some is unavoidable." },
  { id: "compute_serving_endpoint_cost_status", area: "mlai", dollarCol: "est_wasted_usd_list", dbuCol: null, spendCol: "est_usd_list", spendWords: "billed with no requests", why: "Serving endpoints billed with zero requests in the window. Possible waste." },
  { id: "instance_pools_idle_capacity", area: "compute", dollarCol: null, dbuCol: null, why: "Warm pool instances held idle (the cloud VM bill, which no system table carries) -- no waste $ on this check." },
  { id: "lakeflow_jobs_on_all_purpose", area: "jobs", dollarCol: null, dbuCol: null, why: "Jobs on all-purpose (interactive-priced) compute -- no waste $ on this check yet." },
  { id: "lakeflow_stale_zombie_jobs", area: "jobs", dollarCol: null, dbuCol: null, why: "Scheduled jobs with no recent successful run -- likely abandoned; no waste $ on this check." },
  { id: "lakeflow_pipeline_idle_tail_duration", area: "jobs", dollarCol: null, dbuCol: null, why: "Pipeline compute that may linger after its last update -- no waste $ on this check yet." },
  { id: "compute_serving_dormant_endpoints", area: "mlai", dollarCol: null, dbuCol: null, why: "Serving endpoints with no recent traffic -- flagged by request volume; no waste $ on this check." },
  { id: "cost_premium_serverless_photon", area: "cost", dollarCol: null, dbuCol: null, why: "Usage on premium-priced serverless/Photon SKUs -- worth checking it is intentional; spend, not waste." },
];
export type WasteItem = (typeof WASTE_ITEMS)[number];
export const WASTE_IDS = WASTE_ITEMS.map((w) => w.id);

// ─────────── ML & AI ─────────── (spend · endpoints · gateway)
const MLAI_SUBTABS: Subtab[] = [
  {
    key: "spend", label: "Spend",
    ids: ["cost_by_serving_endpoint", "cost_serving_mode_by_endpoint", "cost_vector_search_spend"],
    linked: ["cost_genai_token_gpu"],
    refIds: [],
  },
  {
    key: "endpoints", label: "Endpoints",
    // access_vector_search_traffic "moved from Governance" -- owned here now, not Governance.
    ids: ["compute_serving_dormant_endpoints", "compute_serving_endpoint_cost_status", "compute_serving_endpoint_usage", "access_vector_search_traffic"],
    refIds: ["serving_endpoint_traffic_by_endpoint"],
  },
  {
    key: "gateway", label: "AI Gateway",
    ids: ["compute_ai_gateway_usage"],
    refIds: [],
  },
];

// ─────────── Compute ─────────── (warehouses · clusters · config) -- task_cluster_utilization
// moved fully to Jobs > Compute fit (section 6), so it is not listed here at all.
const COMPUTE_SUBTABS: Subtab[] = [
  {
    key: "warehouses", label: "Warehouses",
    // compute_warehouse_idle_minutes + compute_warehouse_idle_gaps render as ONE check ("Warehouse
    // idle time") -- a page-content decision, both ids stay listed so neither is ever dropped.
    ids: ["compute_warehouse_idle_minutes", "compute_warehouse_idle_gaps", "compute_warehouse_autoscale_churn", "compute_warehouse_autostop_churn", "compute_warehouse_cache_reuse", "sql_warehouse_events_activity"],
    refIds: ["sql_warehouse_config_current"],
  },
  {
    key: "clusters", label: "Clusters & pools",
    ids: ["compute_idle_node_ratio", "node_timeline_utilization", "instance_pools_idle_capacity", "instance_events_idle_active"],
    refIds: ["classic_clusters_config_current", "node_types_reference"],
  },
  {
    key: "config", label: "Configuration",
    ids: ["compute_cluster_config_posture", "compute_warehouse_config_posture"],
    refIds: [],
  },
];

// ─────────── Jobs ─────────── (failures · slow · hygiene · compute_fit · pipelines)
// lakeflow_termination_type_probe is a developer probe, deliberately given no home (DEC-58.5:
// still executable and still listed on All findings -- never deleted, just not curated here).
const JOBS_SUBTABS: Subtab[] = [
  {
    key: "failures", label: "Failures",
    ids: ["lakeflow_job_reliability", "lakeflow_failed_jobs_wasted_dbus", "lakeflow_failed_runs", "lakeflow_never_started_runs", "lakeflow_succeeded_with_failed_tasks"],
    refIds: ["lakeflow_termination_taxonomy", "lakeflow_job_run_changes", "lakeflow_daily_state"],
  },
  {
    key: "slow", label: "Slow & queued",
    ids: ["lakeflow_job_duration_regression", "lakeflow_job_queue_time", "lakeflow_phase_cold_start"],
    refIds: ["lakeflow_failed_cluster_starts", "lakeflow_workload_mix_hours", "lakeflow_long_running_runs"],
  },
  {
    key: "hygiene", label: "Hygiene",
    ids: ["lakeflow_retries_repairs", "lakeflow_stale_zombie_jobs", "lakeflow_jobs_no_timeout", "lakeflow_job_tasks_no_timeout", "lakeflow_jobs_on_all_purpose", "lakeflow_health_rule_coverage", "lakeflow_job_ownership_orphans", "lakeflow_tasks_near_timeout"],
    refIds: ["lakeflow_job_cost_summary", "lakeflow_job_run_cost", "lakeflow_job_recent_runs"],
  },
  {
    key: "compute_fit", label: "Compute fit",
    ids: ["lakeflow_job_compute_pressure", "task_cluster_utilization", "lakeflow_job_oversized"],
    refIds: [],
  },
  {
    key: "pipelines", label: "Pipelines",
    ids: ["lakeflow_pipeline_cost", "lakeflow_pipeline_idle_tail_duration", "lakeflow_pipeline_update_failures_retries"],
    refIds: ["lakeflow_pipelines_inventory_tier"],
  },
];

// ─────────── Queries ─────────── (heavy · efficiency · reliability · capacity · trend · team)
const QUERY_SUBTABS: Subtab[] = [
  {
    key: "heavy", label: "Heavy queries",
    ids: ["query_top_by_cost", "query_provenance_by_source", "audit_self_cost"],
    // Demoted to a detail link at the bottom of the page (query_top_by_cost is the real headline).
    refIds: ["query_costly_statements_grouped", "query_costly_statements", "query_per_query_estimate_lane"],
  },
  {
    key: "efficiency", label: "Efficiency",
    ids: ["query_local_spillage", "query_shuffle_write_amplification", "query_pruning_effectiveness", "query_cache_coldstart"],
    refIds: [],
  },
  {
    key: "reliability", label: "Reliability",
    // cost_failed_statement_waste ("checks not placed above"): homed here, also a Waste priced
    // source once it can be assessed.
    ids: ["query_failed_queries_daily", "cost_failed_statement_waste", "query_failed_statements_grouped"],
    refIds: [],
  },
  {
    key: "capacity", label: "Capacity",
    ids: ["query_warehouse_pressure", "query_queuing_waits", "query_workload_mix_hours", "query_task_statement_breakdown"],
    refIds: ["perf_queue_by_hour"],
  },
  {
    // Daily SQL warehouse measures (perf_daily_by_resource) by day and month; no check of its own.
    key: "trend", label: "Trend",
    ids: [],
    refIds: [],
  },
  {
    // The tag rollup component (same one Cost > Allocation uses, in performance mode) -- no
    // query_id of its own.
    key: "team", label: "By team",
    ids: [],
    refIds: [],
  },
];

// ─────────── Governance & PII ─────────── (access · admin · sensitive · lineage · sharing)
// access_vector_search_traffic moved fully to ML & AI; access_dead_table_candidates moved fully
// to Storage (section 6's own "Moved out" note) -- neither is owned here any more.
const GOVERNANCE_SUBTABS: Subtab[] = [
  {
    key: "access", label: "Access & grants",
    ids: ["access_broad_grants", "access_grants_inventory", "access_grants_inventory_extended", "access_runas_escalation", "access_login_concentration", "access_network_inbound_denials", "access_network_outbound_denials"],
    refIds: [],
  },
  {
    key: "admin", label: "Admin activity",
    ids: ["access_admin_role_change_events"],
    refIds: [],
  },
  {
    key: "sensitive", label: "Sensitive data",
    ids: ["access_classification_coverage", "access_classified_unmasked", "access_sensitive_table_reads", "access_pii_outside_tables", "access_data_classification_inventory", "access_column_masks_inventory", "access_row_filters_inventory", "access_tags_inventory"],
    refIds: [],
  },
  {
    key: "lineage", label: "Lineage",
    ids: ["access_table_lineage_blast_radius", "access_column_lineage_sensitive_reach", "access_pii_propagation_untagged"],
    refIds: ["access_views_inventory", "access_volumes_inventory"],
  },
  {
    key: "sharing", label: "Sharing",
    ids: ["access_delta_sharing_exposure"],
    refIds: [],
  },
];

// ─────────── Storage ─────────── (tables · maintenance)
const STORAGE_SUBTABS: Subtab[] = [
  {
    key: "tables", label: "Tables",
    ids: ["storage_target_table_discovery", "table_inventory_type", "access_dead_table_candidates", "storage_growth"],
    refIds: [],
  },
  {
    key: "maintenance", label: "Maintenance",
    ids: [
      "po_maintenance_cost_by_table", "po_vacuum_reclaimed_bytes", "po_clustering_activity",
      "po_clustering_column_churn", "po_data_skipping_backfill", "po_failure_reasons",
      "storage_po_coverage", "storage_small_files",
    ],
    refIds: [],
  },
];

// ─────────── Overview ─────────── -- a roll-up with no sub-tabs of its own (DEC-74): every id
// here is a reference feed for its cards, never an individually-flagged check.
export const OVERVIEW_IDS = [
  "overview_spend_estimate", "overview_dbu_by_sku", "overview_serverless_classic_split",
  "overview_daily_dbu_trend", "overview_list_prices_raw",
];

// ─────────── Coverage & Gaps ─────────── -- the meta page about coverage; the two raw price
// tables move here per section 6's "By resource"/"Pricing & policy" note ("Raw price tables ->
// Coverage reference tables"), plus this audit's own usage (a coverage-only check, no status
// column by design -- see its own header).
const COVERAGE_REF_IDS = ["cost_account_prices_raw", "pricing_list_prices_raw", "cost_audit_self_usage"];
// System table coverage IS a real check (OK/WARN, not just an inventory row) -- it owns the
// Sources sub-tab rather than sitting alongside its reference feeds.
const COVERAGE_SOURCES_IDS = ["access_source_table_coverage"];

// ─────────── AREA_REGISTRY -- area key -> {label, scope, subtabs}. `subtabs` is [] for a roll-up
// area with no checks table of its own (Waste & savings) or a single implicit page (Overview,
// Coverage & Gaps, All findings) -- the generic AreaPage frame (primitives.tsx) renders whatever
// is there and nothing more, so an area with an empty list still works. ───────────
export const AREA_REGISTRY: Record<string, Area> = {
  overview: { label: "Overview", scope: "List price (effective) · excludes your cloud VM bill", subtabs: [], refIds: OVERVIEW_IDS },
  actions: { label: "Actions", scope: "Flagged checks as fixes · possible waste at list price (effective)", subtabs: [] },
  cost: { label: "Cost", scope: "List price (effective) · excludes your cloud VM bill", subtabs: COST_SUBTABS },
  waste: { label: "Waste & savings", scope: "Possible waste, priced where a source supports it", subtabs: [
    { key: "priced", label: "Priced", ids: [], refIds: [] },
    { key: "unpriced", label: "Flagged, not priced", ids: [], refIds: [] },
    { key: "method", label: "How it's counted", ids: [], refIds: [] },
  ] },
  mlai: { label: "ML & AI", scope: "List price (effective) · excludes your cloud VM bill", subtabs: MLAI_SUBTABS },
  genie: { label: "Genie", scope: "Genie billing · list price (effective)", subtabs: [], refIds: ["genie_usage"] },
  compute: { label: "Compute", scope: "List price (effective) · excludes your cloud VM bill", subtabs: COMPUTE_SUBTABS },
  jobs: { label: "Jobs", scope: "List price (effective) · excludes your cloud VM bill", subtabs: JOBS_SUBTABS },
  queries: { label: "Queries", scope: "List price (effective) · excludes your cloud VM bill", subtabs: QUERY_SUBTABS },
  // AreaPage (primitives.tsx) shows the live version of this line, with the snapshot's actual
  // metastore -- this static text is only the fallback a caller with no `meta` (e.g. the Guide's
  // own per-check chip) reads.
  governance: { label: "Governance & PII", scope: "Metastore-wide · objects the export principal can see", subtabs: GOVERNANCE_SUBTABS },
  storage: { label: "Storage", scope: "Metastore-wide · objects the export principal can see", subtabs: STORAGE_SUBTABS },
  tags: { label: "Tags", scope: "Mandatory tags · own tag, then job, compute, workspace", subtabs: [] },
  findings: { label: "All findings", scope: "Every check, one list", subtabs: [] },
  coverage: { label: "Coverage & Gaps", scope: "What this audit does and doesn't cover", subtabs: [
    { key: "sources", label: "Data sources", ids: COVERAGE_SOURCES_IDS, refIds: COVERAGE_REF_IDS },
    { key: "couldnt", label: "Not assessed", ids: [], refIds: [] },
    { key: "limits", label: "Known limitations", ids: [], refIds: [] },
  ] },
};
export const AREA_ORDER = ["overview", "actions", "cost", "waste", "mlai", "genie", "compute", "jobs", "queries", "governance", "storage", "tags", "findings", "coverage"];

// T-68 (DEC-66.1): the list-price dollar column cost_dollarized_by_sku_day carries -- read at HEAD
// rather than guessed, since Cost/ML & AI/Overview tiles all sum this same underlying finding.
export const COST_MONEY_COL = "net_list_cost";
export const EST_SPEND_CAVEAT = "excludes your cloud provider's own VM bill";

// ─────────── countsForIds -- ONE tally function every badge reads (top-bar area badges,
// SubTabs count chips) so the two can never disagree (section 4.1's own instruction). `ids` is a
// sub-tab's or area's owned id list; `refIds` add to the badge only when they flag rows.
// An empty id list is a roll-up sub-tab (Waste, Coverage) that owns no checks of its own -- it
// gets no badge at all, not a "no data" one. Otherwise noData is true only when every owned id is
// NOT_ASSESSED/EMPTY_WINDOW/EMPTY_FILTERS/ERROR and none is OK/RANKED/INVENTORY-with-rows. ──
export function countsForIds(findings: BandInput[] | null, ids: string[], refIds?: string[]): { critical: number; warn: number; noData: boolean; notRun?: boolean } {
  if (!ids || ids.length === 0) return { critical: 0, warn: 0, noData: false };
  const byId: Record<string, BandInput> = {};
  (findings || []).forEach((f) => { byId[f.query_id] = f; });
  let critical = 0, warn = 0, hasSignal = false, ran = false;
  ids.forEach((id) => {
    const row = byId[id];
    const band = row && bandOf ? bandOf(row) : "NOT_ASSESSED";
    if (band !== "NOT_ASSESSED" && band !== "ERROR") ran = true;
    if (band === "CRITICAL") { critical += 1; hasSignal = true; }
    else if (band === "WARN") { warn += 1; hasSignal = true; }
    else if (band === "OK" || band === "RANKED") { hasSignal = true; }
    else if (band === "INVENTORY" && row && (row.row_count || 0) > 0) { hasSignal = true; }
  });
  // Reference lists add to the badge only when they flag rows, the same rule tabOf follows.
  (refIds || []).forEach((id) => {
    const row = byId[id];
    const band = row && bandOf ? bandOf(row) : null;
    if (band === "CRITICAL") critical += 1;
    else if (band === "WARN") warn += 1;
  });
  return { critical, warn, noData: !hasSignal, notRun: !ran };
}

// ─────────── Old-key redirects (section 4.2) -- a link/bookmark from before the redesign still
// lands on the right place. ───────────
export const OLD_TAB_REDIRECTS: Record<string, string> = { query: "queries", lineage: "governance", domains: "findings" };
export const OLD_SUBTAB_REDIRECTS: Record<string, string> = {
  chargeback: "allocation", by_tag: "allocation",
  coverage_premium: "pricing", trust_pricing: "pricing",
  pools: "clusters",
  task_fit: "compute_fit",
  timing: "slow",
  waste_hygiene: "hygiene",
};

// ─────────── query_id -> {tab, subtab} -- built once from AREA_REGISTRY, so a next:-chip or an
// Overview "top finding" click lands on the SAME tab a person would find it on by browsing. First
// writer wins (mlai is added before cost/governance, so a shared id's HOME is mlai -- section 6's
// own "moved" notes). An id absent here falls back to the All findings tab. ───────────
function buildQueryHome(): Record<string, Home> {
  const home: Record<string, Home> = {};
  const add = (tab: string, subtab: string | null, ids: string[]) => { (ids || []).forEach((id) => { if (id && !(id in home)) home[id] = { tab, subtab }; }); };

  MLAI_SUBTABS.forEach((s) => add("mlai", s.key, [...s.ids, ...(s.refIds || [])]));
  COST_SUBTABS.forEach((s) => add("cost", s.key, [...s.ids, ...(s.refIds || [])]));
  COMPUTE_SUBTABS.forEach((s) => add("compute", s.key, [...s.ids, ...(s.refIds || [])]));
  JOBS_SUBTABS.forEach((s) => add("jobs", s.key, [...s.ids, ...(s.refIds || [])]));
  QUERY_SUBTABS.forEach((s) => add("queries", s.key, [...s.ids, ...(s.refIds || [])]));
  GOVERNANCE_SUBTABS.forEach((s) => add("governance", s.key, [...s.ids, ...(s.refIds || [])]));
  STORAGE_SUBTABS.forEach((s) => add("storage", s.key, [...s.ids, ...(s.refIds || [])]));
  add("overview", null, OVERVIEW_IDS);
  add("coverage", "sources", [...COVERAGE_SOURCES_IDS, ...COVERAGE_REF_IDS]);
  return home;
}
const QUERY_HOME = buildQueryHome();

export function homeForQuery(row: { query_id: string }): Home {
  return QUERY_HOME[row.query_id] || { tab: "findings", subtab: null };
}

export const AF_HOME_AREAS = AREA_ORDER.filter((k) => !["overview", "waste", "findings"].includes(k));

export const AF_REFERENCE_IDS = (function () {
  const set = new Set<string>();
  ["overview", "coverage", ...AF_HOME_AREAS].forEach((key) => {
    const area = AREA_REGISTRY[key];
    if (!area) return;
    (area.refIds || []).forEach((id) => set.add(id));
    (area.subtabs || []).forEach((s) => (s.refIds || []).forEach((id) => set.add(id)));
  });
  return set;
})();

// DEC-74: these three checks rank resources by size alone (no critical/warn threshold) --
// cost_by_job, cost_by_compute_resource, lakeflow_pipeline_cost -- so they must always read
// "Ranked", never Critical/Warn, regardless of what their own row status happens to be.
export const RANKED_QUERY_IDS = new Set(["cost_by_job", "cost_by_compute_resource", "lakeflow_pipeline_cost"]);

/** What bandOf reads: a check's outcome, status counts and id. */
export interface BandInput {
  query_id: string;
  outcome: string | null;
  status_counts?: StatusCounts | null;
  row_count?: number;
}

export function bandOf(row: BandInput): Band {
  if (row.outcome === "error") return "ERROR";
  if (row.outcome === "not_assessed") return "NOT_ASSESSED";
  if (row.outcome === "ok_empty_window") return "EMPTY_WINDOW";
  if (row.outcome === "ok_empty_filters") return "EMPTY_FILTERS";
  // ok_rows
  const sc = row.status_counts;
  if (!sc) return "INVENTORY";
  // DEC-74: a magnitude-only ranking never reads Critical/Warn, whatever its own rows say.
  if (RANKED_QUERY_IDS.has(row.query_id)) return "RANKED";
  // The server caps a report-type check's own CRITICAL rows to WARN before they ever reach here
  // (contract E, app/core/materiality.severity_cap) -- status_counts.CRITICAL is already the
  // capped count, so this band always matches what the row itself carries.
  if ((sc.CRITICAL || 0) > 0) return "CRITICAL";
  if ((sc.WARN || 0) > 0) return "WARN";
  // T-67: a finding can return real rows where every single one was judged NOT_ASSESSED (no
  // cluster recorded, too few slices, ...) with zero rows ever reaching a CRITICAL/WARN/OK
  // verdict -- that is not a verified clean pass, so it must not fall through to OK just
  // because the row-level status is not one of the two flagged values. NOT_ASSESSED is never OK.
  if ((sc.NOT_ASSESSED || 0) > 0 && !(sc.OK || 0)) return "NOT_ASSESSED";
  return "OK";
}
