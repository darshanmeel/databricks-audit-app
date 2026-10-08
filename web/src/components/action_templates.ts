// The fix templates behind Actions: one per kind of fix, with the checks it covers.

/** One kind of fix: the checks it covers, its title for n resources, and how to do it. */
export interface ActionTemplate {
  key: string;
  kind: string;
  team: string;
  waste?: string;
  checks: string[];
  noun: [string, string];
  title: (n: string, w: string) => string;
  effort: string;
  how: string;
  /** The statement to run, only where the fix really is SQL (most Databricks fixes are settings). */
  sql?: string;
  [extra: string]: unknown;
}

export function actionTemplateFor(queryId: string): ActionTemplate | null {
  return ACTION_TEMPLATES.find((x) => (x.checks || []).includes(queryId)) || null;
}

export function actionKeyFor(queryId: string): string | null {
  const t = ACTION_TEMPLATES.find((x) => (x.checks || []).includes(queryId));
  return t ? t.key : null;
}

export const ACTION_TEMPLATES: ActionTemplate[] = [
  { key: "warehouse_autostop", kind: "saving", team: "Platform", waste: "compute_warehouse_idle_minutes",
    checks: ["compute_warehouse_idle_minutes"], noun: ["idle SQL warehouse", "idle SQL warehouses"],
    title: (n, w) => `Lower auto-stop on ${n} ${w}`, effort: "5 min each",
    how: "SQL Warehouses → the warehouse → Edit → Auto stop: 10 min (5 min on serverless)." },
  { key: "idle_clusters", kind: "saving", team: "Platform", waste: "compute_idle_node_ratio",
    checks: ["compute_idle_node_ratio"], noun: ["idle cluster", "idle clusters"],
    title: (n, w) => `Shrink or auto-terminate ${n} ${w}`, effort: "15 min each",
    how: "Compute → the cluster → Edit: fewer workers or a shorter auto-termination. Check memory use before shrinking." },
  { key: "failed_jobs", kind: "saving", team: "Data eng", waste: "lakeflow_failed_jobs_wasted_dbus",
    checks: ["lakeflow_failed_jobs_wasted_dbus", "lakeflow_job_reliability"], noun: ["failing job", "failing jobs"],
    title: (n, w) => `Repair ${n} ${w}`, effort: "1–2 h each",
    how: "Open the latest failed run, fix the cause and re-run. The job drawer shows what changed since the last good run." },
  { key: "failed_statements", kind: "saving", team: "Data eng", waste: "cost_failed_statement_waste",
    checks: ["cost_failed_statement_waste"], noun: ["failing statement shape", "failing statement shapes"],
    title: (n, w) => `Fix ${n} ${w} that keep failing`, effort: "30 min each",
    how: "Queries → Reliability groups the errors; fix the query or the table it reads, or stop the schedule that re-runs it." },
  { key: "idle_endpoints", kind: "saving", team: "ML", waste: "compute_serving_endpoint_cost_status",
    checks: ["compute_serving_endpoint_cost_status", "compute_serving_dormant_endpoints"], noun: ["idle serving endpoint", "idle serving endpoints"],
    title: (n, w) => `Scale ${n} ${w} to zero, or delete them`, effort: "5 min each",
    how: "Serving → the endpoint → Edit: turn on scale to zero, or delete it if nothing calls it." },
  // The check flags workspace × usage-unit lines, not workspaces, so the count names lines.
  { key: "untagged", kind: "policy", team: "FinOps", checks: ["cost_chargeback_by_allocation_tag"],
    noun: ["untagged spend line", "untagged spend lines"], title: (n, w) => `Tag ${n} ${w} with a cost center`, effort: "1 day",
    how: "Require the cost_center tag in cluster policies, serverless budget policies and warehouse settings." },
  { key: "jobs_on_all_purpose", kind: "hygiene", team: "Platform", checks: ["lakeflow_jobs_on_all_purpose"],
    noun: ["job", "jobs"], title: (n, w) => `Move ${n} scheduled ${w} off all-purpose clusters`, effort: "10 min each",
    how: "Job → Compute → a new job cluster or serverless. Job compute costs less per DBU than all-purpose." },
  { key: "stale_jobs", kind: "hygiene", team: "Data eng", checks: ["lakeflow_stale_zombie_jobs"],
    noun: ["stale job", "stale jobs"], title: (n, w) => `Pause or delete ${n} ${w}`, effort: "5 min each",
    how: "Pause the schedule, or delete the job if nobody needs it." },
  { key: "no_timeout", kind: "hygiene", team: "Data eng", checks: ["lakeflow_jobs_no_timeout", "lakeflow_job_tasks_no_timeout"],
    noun: ["job", "jobs"], title: (n, w) => `Set a time limit on ${n} ${w}`, effort: "5 min each",
    how: "Set a timeout of about 2 × the job's p95 run time (the job drawer suggests one), or a run-duration health rule." },
  { key: "warm_pools", kind: "hygiene", team: "Platform", checks: ["instance_pools_idle_capacity"],
    noun: ["instance pool", "instance pools"], title: (n, w) => `Lower the idle floor on ${n} ${w}`, effort: "5 min each",
    how: "Compute → Pools → the pool: lower the minimum idle instances." },
  { key: "warehouse_pressure", kind: "hygiene", team: "Platform", checks: ["query_warehouse_pressure"],
    noun: ["SQL warehouse", "SQL warehouses"], title: (n, w) => `Resize ${n} ${w} that queue or spill`, effort: "10 min each",
    how: "More max clusters when queries wait for a slot; one size up when they spill." },
  { key: "cluster_policies", kind: "policy", team: "Platform", checks: ["compute_cluster_config_posture"],
    noun: ["cluster", "clusters"], title: (n, w) => `Put ${n} ${w} under a cluster policy`, effort: "1 day",
    how: "Create a small set of cluster policies (size, auto-termination, tags) and make them required." },
  { key: "unmasked", kind: "risk", team: "Security", checks: ["access_classified_unmasked"],
    noun: ["sensitive column", "sensitive columns"], title: (n, w) => `Mask ${n} ${w}`, effort: "1 h each table",
    how: "ALTER TABLE … ALTER COLUMN … SET MASK, or one ABAC column-mask policy on the catalog.",
    sql: "ALTER TABLE <catalog>.<schema>.<table> ALTER COLUMN <column> SET MASK <catalog>.<schema>.<mask_function>;" },
  { key: "admin_changes", kind: "risk", team: "Security", checks: ["access_admin_role_change_events"],
    noun: ["admin change pattern", "admin change patterns"], title: (n, w) => `Confirm ${n} ${w}`, effort: "30 min",
    how: "Check each change with the person or service that made it; revoke what nobody expected." },
  { key: "broad_grants", kind: "risk", team: "Security", checks: ["access_broad_grants"],
    noun: ["broad grant", "broad grants"], title: (n, w) => `Narrow ${n} ${w}`, effort: "30 min each",
    how: "Grant through groups, not people, and only the privileges needed. List your admin groups under admin_groups in settings.yml.",
    sql: "REVOKE <privilege> ON <securable_type> <securable_name> FROM `<principal>`;\nGRANT <privilege> ON <securable_type> <securable_name> TO `<group>`;" },
];
