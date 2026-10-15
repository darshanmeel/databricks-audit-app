// F2 (U-UI-03/04, N-N9/N-N10/N-N11): human column names, units
// and one-sentence help for a wide finding's own data table (finding_detail.tsx's DataTable is the
// only reader). Before this file, DataTable printed c.name verbatim -- raw UPPERCASE snake_case
// (WASTED_DBUS_PROXY, LAST_FAILED_TERMINATION_CODE) with no unit and no explanation, which is
// exactly what the user's own screenshot flagged: a table nobody outside this codebase can read.
//
// This is a DISPLAY layer only. It never changes what a column IS (that is classify_columns'
// job, app/api/service.py) or what a header TEXT says (P4-03 owns read_this/investigate_if/
// actions -- this file only decides how one column NAME renders, and always keeps the raw name
// one hover away so a reader can still match it back to that header text).

import { resolveName } from "./names";
import type { Column, Row } from "../types";

// A curated definition for a column this app renders often enough, or ambiguously enough, that
// the generic humanize() pass below is not good enough on its own. `unit` overrides the kind-
// derived unit (needed for a column classify_columns cannot classify, e.g. est_usd_list_share --
// N-N10); `help` is one sentence, plain words, no jargon a FinOps/manager/CTO reader would have
// to look up.
interface Curated {
  label: string;
  unit?: string | null;
  help?: string;
  kind?: string;
}

/** A column's reader-facing name, its unit and one line of help. `raw` is the column name. */
export interface ColumnMeta {
  raw: string;
  label: string;
  unit: string | null;
  help: string | null;
}

/** A column as a finding's table shows it. */
export interface PlannedColumn extends ColumnMeta {
  name: string;
  kind: string | null;
  numeric: boolean;
}

/** planColumns: the order to show columns in, which are hidden and why, and what leads. */
export interface ColumnPlan {
  entity: string | null;
  entityNoun: { s: string; p: string } | null;
  entityDisplayCol: string | null;
  keyMetric: PlannedColumn | null;
  visible: PlannedColumn[];
  hidden: (PlannedColumn & { reason: string })[];
  totalCount: number;
}

const CURATED: Record<string, Curated> = {
  status: { label: "Status", help: "CRITICAL/WARN need action soon; OK is clean; NOT_ASSESSED means this one row could not be judged -- it is not a pass." },
  not_assessed_reason: { label: "Why not assessed", help: "The specific reason this row could not be judged." },
  workspace_id: { label: "Workspace" },
  job_id: { label: "Job" },
  run_id: { label: "Run" },
  cluster_id: { label: "Cluster" },
  warehouse_id: { label: "Warehouse" },
  pipeline_id: { label: "Pipeline" },
  // A job or pipeline has a creator and a run-as identity, never an "owner" -- the two can differ
  // (a job created by one person, scheduled to run as a service principal).
  creator_user_name: { label: "Created by" },
  run_as: { label: "Runs as" },
  run_as_user_name: { label: "Runs as" },
  distinct_runs: { label: "Runs", help: "How many runs of this job landed in the window." },
  failed_runs: { label: "Failed runs" },
  last_failed_termination_code: { label: "Why the last failure happened" },
  net_job_dbus: { label: "DBUs used (whole job)" },
  net_dbus: { label: "DBUs used (whole resource)" },
  wasted_dbus_proxy: { label: "Wasted DBUs (est.)", unit: "DBU", help: "This job's DBUs, scaled by the share of its runs that failed -- an estimate of the DBUs those failed runs burned, not a metered figure." },
  wasted_dbus: { label: "Wasted DBUs (est.)", unit: "DBU", help: "This job's DBUs, scaled by the share of its runs that failed -- an estimate of the DBUs those failed runs burned, not a metered figure." },
  est_usd_list: { label: "Est. $ (whole resource, not the waste)" },
  est_wasted_usd_list: { label: "Est. wasted $" },
  est_usd_list_share: { label: "Est. $ per job (shared cluster split evenly)", unit: "$", help: "A shared cluster's cost split evenly across the jobs that ran on it in this window -- not a metered per-job bill." },
  cost_basis: { label: "Cost basis", help: "statement: the cost Databricks attributed to each statement. run_time_share: no per-statement cost, so each billed hour of the warehouse is split across its statements by task time." },
  price_basis: { label: "Price basis", help: "Whether this row's dollar figure is a priced list-price estimate, unpriced (no list price on record), free-tier usage, or no_size (no size on record, so not priced)." },
  // Default DBU: several vendored queries that select net_usage_quantity filter their SQL to
  // usage_unit = 'DBU' without selecting usage_unit itself as a column (cost_by_job.sql is one),
  // so there is no per-row signal to read at all -- DBU is the correct, and by far the most
  // common, unit for a Databricks cost figure. The per-row check in planColumns below overrides
  // this default whenever usage_unit IS present as an actual column, so a query that exposes a
  // real (possibly non-DBU, e.g. storage GB-month) unit still shows its own true unit.
  net_usage_quantity: { label: "Usage", unit: "DBU" },
  usage_date: { label: "Day" },
  sku_name: { label: "SKU (price line)" },
  billing_origin_product: { label: "Product" },
  run_hours: { label: "Run hours" },
  in_flight: { label: "Still running" },
  top_task_share_pct: { label: "Busiest task time vs run time", help: "Can exceed 100% when a job's tasks run in parallel -- more than one task can be running at the same instant." },
  pressure: { label: "Pressure" },
  pressure_reason: { label: "Pressure reason" },
  scaling_hint: { label: "Suggested lever" },
  at_ceiling: { label: "At max workers" },
  status_raw: { label: "Status before the floor or cap", help: "The status this row had before a materiality floor (below a size/impact cutoff) moved it to OK, or a report cap moved Critical to Warn." },
  failed_run_rows: { label: "failed runs" },
  net_list_cost: { label: "list-price spend", unit: "$" },
  abac_mask_policies: { label: "ABAC mask policies" },
  never_started_runs_zero_length: { label: "runs that never started" },
  wait_share_pct: { label: "wait share", unit: "%" },
  failed_update_rows: { label: "failed updates" },
  // cost_unnamed_workspaces
  dbus_365d: { label: "DBUs, last 365 days" },
  net_list_cost_usd_365d: { label: "Spend, last 365 days", unit: "$" },
  lifetime_days: { label: "Days active, first use to last" },
  short_lived: { label: "Active under 30 days" },
  active_days: { label: "Days with any usage" },
  // query_top_by_cost
  group_id: { label: "Query group" },
  group_kind: { label: "Grouped by" },
  est_cost_usd_list: { label: "Est. $ (this query group)" },
  share_of_warehouse_cost: { label: "Share of warehouse spend" },
  from_result_cache_share: { label: "Share of runs served from cache" },
  // lakeflow_job_oversized
  job_list_cost_usd: { label: "Job's $ (whole job, not the saving)", unit: "$" },
  est_saving_usd_list: { label: "Est. saving" },
  suggested_action: { label: "Suggested fix" },
  // cost_by_hour_of_day
  usd_list: { label: "Spend in this row", unit: "$" },
  hour_usd_list: { label: "Spend, this hour of day (whole window)", unit: "$" },
  window_usd_list: { label: "Spend, whole window", unit: "$" },
  // kind: "pct" -- classify_columns has no pattern for this name (no "pct"/"percent" substring),
  // but the value is already a real percentage (SQL multiplies by 100); without this override a
  // grouped table/chart SUMS it across rows instead of averaging, which is meaningless for a %.
  share_of_window_spend: { label: "Share of window spend", unit: "%", kind: "pct" },
  weekday_num: { label: "Weekday (0 = Monday)" },
  // cost_sku_trend_12m
  usd_last_3m_avg: { label: "Avg monthly spend, last 3 months", unit: "$" },
  usd_prior_3m_avg: { label: "Avg monthly spend, 3 months before that", unit: "$" },
  usd_first_3m_avg: { label: "Avg monthly spend, oldest 3 months", unit: "$" },
  share_of_total_last_3m: { label: "Share of total spend, last 3 months", unit: "%", kind: "pct" },
  new_this_year: { label: "New this year -- no spend before" },
  is_partial_month: { label: "Month still in progress" },
};

// snake_case -> Sentence case, with a small set of abbreviation fixes so a reader never has to
// decode "dbus"/"usd"/"pct" by eye. Trailing "_id" is dropped (a bare id column CURATED does not
// already name gets its noun alone, e.g. "grantee_id" -> "Grantee") and "_disc" is dropped too --
// planColumns() below relabels a live discount twin explicitly, so a humanize() call only ever
// sees "_disc" on a twin that ISN'T live (discount 0%, about to be hidden as a duplicate anyway).
function humanizeColumnName(name: string): string {
  if (!name) return "";
  let s = String(name).replace(/_disc$/, "").replace(/_id$/, "");
  if (!s) s = String(name);
  const words = s.split("_").filter(Boolean);
  if (!words.length) return String(name);
  const FIXES: Record<string, string> = { dbus: "DBUs", dbu: "DBU", usd: "$", pct: "%", est: "Est.", cpu: "CPU", mem: "memory", gb: "GB", ui: "UI", url: "URL" };
  return words.map((w) => {
    const lw = w.toLowerCase();
    if (FIXES[lw]) return FIXES[lw];
    if (/^p(50|90|95|99)$/.test(lw)) return lw;
    return lw.charAt(0).toUpperCase() + lw.slice(1);
  }).join(" ");
}

// The unit shown beside a header label -- from the column's own `kind` first (the same magnitude
// families format.tsx's fmtCell already renders with a suffix).
function unitForClassifiedKind(kind: string | null): string | null {
  if (kind === "money") return "$";
  if (kind === "dbu") return "DBU";
  if (kind === "pct") return "%";
  if (kind === "hours") return "h";
  if (kind === "gb" || kind === "gb_bytes") return "GB";
  return null;
}

// A raw-seconds/milliseconds/minutes column-name suffix classify_columns has no `kind` for at
// all -- {unit, bareName}, where bareName has the matched suffix removed so humanizeColumnName()
// is never asked to turn the suffix into a SECOND, word-shaped copy of the very unit this already
// names (setup_s -> unit "s" + bareName "setup" -> label "Setup", not the bare, meaningless
// single-letter word "S" a plain word-split of "setup_s" would otherwise produce).
function suffixUnit(name: string): { unit: string; bareName: string } | null {
  if (/_ms$/.test(name)) return { unit: "ms", bareName: name.replace(/_ms$/, "") };
  if (/_minutes$/.test(name)) return { unit: "min", bareName: name.replace(/_minutes$/, "") };
  if (/_seconds$/.test(name)) return { unit: "s", bareName: name.replace(/_seconds$/, "") };
  if (/(^|_)s$/.test(name)) return { unit: "s", bareName: name.replace(/(^|_)s$/, "") };
  return null;
}

// columnMeta(name, kind) -> {label, unit, help, raw}. `raw` is always the column name verbatim --
// a reader hovering the header sees "column: <raw>" so this label can always be matched back to
// whatever a header's own read_this/investigate_if/actions text says about that column (P4-03
// owns that text; this file never rewords it, only the column NAME above the data). Always the
// real unit, even when the label text happens to end in that same symbol -- a caller rendering
// label and unit as ONE inline string (planColumns' own toMeta below) suppresses the repeat
// itself; the Guide's Columns table renders them as two separate cells, where suppressing it here
// instead left a plainly wrong "-- " unit next to a dollar column.
function escapeRegExp(s: string): string { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

export function columnMeta(name: string, kind: string | null): ColumnMeta {
  const curated: Partial<Curated> = CURATED[name] || {};
  let bareName = name;
  // "bytes" in a gb_bytes column's own name (read_bytes_sum) just repeats what the GB unit badge
  // already says once the value is converted -- left in, the header reads "Read Bytes Sum GB",
  // implying two different units rather than one.
  if (!curated.label && kind === "gb_bytes") {
    bareName = bareName.replace(/(^|_)bytes(?=_|$)/i, "").replace(/^_|_$|__+/g, (m) => (m === "__" ? "_" : ""));
  }
  let unit: string | null | undefined;
  if (curated.unit !== undefined) {
    unit = curated.unit;
  } else {
    unit = unitForClassifiedKind(kind);
    if (!unit) {
      const suffix = suffixUnit(bareName);
      if (suffix) { unit = suffix.unit; bareName = suffix.bareName; }
    }
  }
  const label = curated.label || humanizeColumnName(bareName);
  return { raw: name, label, unit: unit || null, help: curated.help || null };
}

// The column named for CURATED's own label alone -- finding_detail.tsx's actionLine uses this to
// swap a bare column name inside an action SENTENCE for its plain label (never the generic
// humanize() fallback there: a name this pass has not curated reads better dropped/left as a
// humanized guess than as a curated-sounding label nobody actually wrote).
export function curatedColumnLabel(name: string): string | null {
  const c = CURATED[name];
  return c ? c.label : null;
}

// A term from a dbt model's own meta.order_by (app/core/data._model_order_by, e.g. "wasted_dbus_
// proxy DESC, failed_runs DESC") -> plain words, or null when the term IS the status-rank CASE
// expression every findings query with a status column already gets prepended (that is exactly
// what "most severe first" already says, so it is dropped rather than repeated as a second,
// unreadable clause). A term this cannot parse as a bare "<column> [ASC|DESC]" falls back to a
// named admission rather than printing raw SQL in the UI.
function humanizeOrderByTerm(term: string): string | null {
  const t = String(term || "").trim();
  if (!t) return null;
  if (/^case\s+status\b/i.test(t)) return null;
  // Neither a trailing NULLS FIRST/LAST nor a leading table-alias qualifier ("b.column") changes
  // what the term means -- both are real SQL a model's own meta.order_by can carry, and without
  // stripping them first every such term fell through to the same generic "this check's own
  // ranking" fallback below, however many of them a query had (cost_unnamed_workspaces' own
  // "net_list_cost_usd DESC NULLS LAST, b.last_used DESC, b.workspace_id" used to read as that
  // same unhelpful phrase three times in a row).
  const stripped = t.replace(/\s+nulls\s+(first|last)$/i, "");
  const m = stripped.match(/^(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?([a-zA-Z_][a-zA-Z0-9_]*)\s*(asc|desc)?$/i);
  if (!m) return "this check's own ranking";
  const dir = (m[2] || "desc").toLowerCase();
  return `${columnMeta(m[1], null).label} (${dir === "asc" ? "lowest first" : "highest first"})`;
}

// "Sorted: most severe first, then Wasted DBUs (est.) (highest first), then Failed runs (highest
// first)" -- replaces the old bare "ordered worst-first" (finding_detail.tsx's data-foot line),
// which named an ordering rule without saying what it actually was. null when the model carries no
// meta.order_by at all (unchanged from before this task: that case renders nothing extra).
export function sortOrderNote(orderBy: string | null | undefined): string | null {
  if (!orderBy) return null;
  const parts = String(orderBy).split(",").map(humanizeOrderByTerm).filter((p) => p !== null);
  return parts.length ? `Sorted: most severe first, then ${parts.join(", then ")}` : "Sorted: most severe first";
}

// Singular/plural English noun for each column planColumns' own ENTITY_PRIORITY can pick as a
// finding's main entity -- lets a caller (finding_detail.tsx's verdict line and grouped table)
// say "12 warehouses" instead of "12 rows" without hard-coding per-check wording.
const ENTITY_NOUN: Record<string, { s: string; p: string }> = {
  job_id: { s: "job", p: "jobs" },
  pipeline_id: { s: "pipeline", p: "pipelines" },
  notebook_id: { s: "notebook", p: "notebooks" },
  warehouse_id: { s: "warehouse", p: "warehouses" },
  cluster_id: { s: "cluster", p: "clusters" },
  endpoint_name: { s: "endpoint", p: "endpoints" },
  served_entity_name: { s: "served entity", p: "served entities" },
  statement_fingerprint: { s: "query", p: "queries" },
  table_full_name: { s: "table", p: "tables" },
  target_table: { s: "table", p: "tables" },
  table_id: { s: "table", p: "tables" },
  table_name: { s: "table", p: "tables" },
  grantee: { s: "grantee", p: "grantees" },
  principal: { s: "principal", p: "principals" },
  sku_name: { s: "SKU", p: "SKUs" },
  workspace_id: { s: "workspace", p: "workspaces" },
  group_id: { s: "query group", p: "query groups" },
};

// The one numeric column a summary (verdict line, top-N chart, grouped table) leads with --
// wasted money first, then any money, then wasted DBUs, then any DBU, then a rate, then a
// duration/size (all unchanged: a check that already headlines correctly off a classified column,
// e.g. compute_warehouse_idle_minutes' own est_wasted_usd_list, keeps doing exactly that). Only a
// candidate with NO classified magnitude at all -- a plain count column classify_columns has no
// kind for, e.g. lakeflow_failed_runs' own failed_run_rows/distinct_runs pair -- reaches the two
// T-75B review fallbacks below: a column named failed/wasted/idle (the check's own verdict column,
// whatever it happens to be called), then the check's own sort column (meta.order_by), before
// finally falling back to whichever numeric column the query's SELECT list put first.
// A configured setting (worker count, auto-stop minutes) is not a measure to add up or rank by.
const SETTING_COLUMN_RE = /^(worker_count|num_workers|min_workers|max_workers|min_num_clusters|max_num_clusters|cluster_count|autotermination_minutes|auto_termination_minutes|auto_stop_minutes|served_entity_count)$/;

function pickKeyMetric(visibleMeta: PlannedColumn[], entity: string | null, orderByCol: string | null): PlannedColumn | null {
  // A count of policies is context, never the measure a check ranks by.
  const candidates = visibleMeta.filter((c) => c.numeric && c.name !== entity && c.name !== "status" && !SETTING_COLUMN_RE.test(c.name) && !/_policies$/.test(c.name));
  if (!candidates.length) return null;
  const byKind = (kind: string) => candidates.find((c) => c.kind === kind);
  return (
    candidates.find((c) => c.kind === "money" && /wasted/i.test(c.name))
    || byKind("money")
    || candidates.find((c) => c.kind === "dbu" && /wasted/i.test(c.name))
    || byKind("dbu")
    || byKind("pct")
    || byKind("hours")
    || byKind("gb")
    || byKind("gb_bytes")
    || candidates.find((c) => c.name === "event_count" && candidates.some((x) => /^failed_/.test(x.name)))
    || candidates.find((c) => /failed|wasted|idle/i.test(c.name))
    || (orderByCol && candidates.find((c) => c.name === orderByCol))
    || candidates[0]
  );
}

// The lead column of a model's own meta.order_by (e.g. "wasted_dbus_proxy DESC, failed_runs DESC")
// -- pickKeyMetric's second-choice headline when no failed/wasted/idle-named column exists, since
// that column is by definition already this check's own idea of "worst first".
function firstOrderByColumn(orderBy: string | null | undefined): string | null {
  const first = String(orderBy || "").split(",")[0].trim();
  const m = first.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*(asc|desc)?$/i);
  return m ? m[1] : null;
}

// A non-id entity column (table_id) whose own value is not itself readable -- the column to show
// INSTEAD as that entity's display name (table_name), so grouping by the stable id never forces
// the table/chart to print a raw UUID (T-75B review item 5: table_id fixes the merge bug, but the
// reader still needs to see table_name, not the id, per row).
const ENTITY_DISPLAY_COL: Record<string, string> = { table_id: "table_name" };

// planColumns(columns, rows, discountPct, orderBy) -- the column PLAN for a finding's data table:
// what order to show them in, which to hide by default (and why), and the fully-resolved
// label/unit/help/numeric for each. `columns`/`rows` are exactly data.columns/data.rows off
// GET /api/finding; `discountPct` is data.discount_pct (unused directly -- the discount is
// already baked into each money column's own `label`, see the _disc handling below -- kept as a
// parameter so a future caller does not have to re-derive it from label text). `orderBy` is
// data.order_by, read only for pickKeyMetric's own fallback (see firstOrderByColumn).
export function planColumns(columns: Column[] | null, rows: Row[] | null, discountPct?: number, orderBy?: string | null): ColumnPlan {
  const cols = columns || [];
  const byName: Record<string, Column> = {};
  cols.forEach((c) => { byName[c.name] = c; });
  const loadedRows = rows || [];

  // table_id ranks above the bare table_name it always rides alongside (T-75B review item 5: two
  // different tables sharing a name were silently merged when table_name was the entity) -- a
  // check with no table_id at all still falls back to table_name exactly as before. group_id
  // (query_top_by_cost's own saved-query/dashboard/job/text-hash grain, no other query emits this
  // column) ranks above warehouse_id so a warehouse a query group happened to run on is never
  // mistaken for the entity itself.
  const ENTITY_PRIORITY = [
    "job_id", "pipeline_id", "notebook_id", "group_id", "warehouse_id", "cluster_id", "endpoint_name", "served_entity_name",
    "statement_fingerprint", "table_full_name", "target_table", "table_id", "table_name",
    "grantee", "principal", "sku_name", "workspace_id",
  ];
  const entity = ENTITY_PRIORITY.find((n) => byName[n]) || null;
  const entityNoun = entity ? ENTITY_NOUN[entity] || null : null;
  const entityDisplayCol = entity ? (ENTITY_DISPLAY_COL[entity] && byName[ENTITY_DISPLAY_COL[entity]] ? ENTITY_DISPLAY_COL[entity] : null) : null;

  const hideNames = new Set<string>();
  const hideReason: Record<string, string> = {};
  const overrideLabel: Record<string, string> = {};

  // window_days: the "N d window" badge (d-meta) already says this -- repeating it as a column
  // (every row identical) is pure noise.
  if (byName.window_days) { hideNames.add("window_days"); hideReason.window_days = "window"; }
  // A floor column that is false on every loaded row says nothing.
  if (byName.below_floor && loadedRows.length > 0 && loadedRows.every((r) => !r.below_floor)) { hideNames.add("below_floor"); hideReason.below_floor = "floor"; }

  // Endpoint rows are an endpoint's own cost, per day when the table has a day, never "a job".
  if (byName.endpoint_id || byName.endpoint_name) {
    const perDay = !!(byName.usage_date || byName.day || byName.usage_day);
    if (byName.est_usd_list) overrideLabel.est_usd_list = perDay ? "Endpoint $ that day" : "Endpoint $ in the window";
    if (byName.net_dbus) overrideLabel.net_dbus = perDay ? "DBUs that day" : "DBUs in the window";
  }

  // A "_disc" twin: at 0% discount its label is byte-identical to the base column's own label
  // (app/api/service.est_label) AND its values equal the base's -- so hide the (now-redundant)
  // twin. Once a real discount is set, the twin's label instead starts with "what-if:" and its
  // VALUES differ from the base -- so hide the base instead and rename the twin to say plainly
  // what it now is, rather than repeating the base column's own name twice on screen.
  cols.forEach((c) => {
    if (!c.name.endsWith("_disc")) return;
    const base = c.name.slice(0, -"_disc".length);
    if (!byName[base]) return;
    const live = !!(c.label && String(c.label).startsWith("what-if"));
    if (live) {
      hideNames.add(base);
      hideReason[base] = "discount";
      const m = String(c.label).match(/-([\d.]+)%/);
      overrideLabel[c.name] = m ? `After your ${m[1]}% discount` : "After your discount";
    } else {
      hideNames.add(c.name);
      hideReason[c.name] = "discount";
    }
  });

  // A column whose value equals an earlier (still-visible) column's on every loaded row (e.g.
  // net_dbus === net_job_dbus on every vendored query that carries both) -- comparing on the
  // PAGE actually loaded, same honesty rule the rest of this app applies to a capped slice: this
  // can only ever HIDE a genuine duplicate, never invent a false one, since two columns that
  // differ anywhere in the loaded page are left exactly as they are.
  if (loadedRows.length > 0) {
    const survivors = cols.filter((c) => !hideNames.has(c.name));
    for (let i = 0; i < survivors.length; i++) {
      const a = survivors[i];
      if (hideNames.has(a.name)) continue;
      for (let j = i + 1; j < survivors.length; j++) {
        const b = survivors[j];
        if (hideNames.has(b.name)) continue;
        if (loadedRows.every((r) => r[a.name] === r[b.name])) {
          hideNames.add(b.name);
          hideReason[b.name] = "duplicate";
        }
      }
    }
  }

  // workspace_name: hidden once workspace_id already resolves to that exact name on every loaded
  // row (the id already renders as a linked name via <Ref>, findings_table.tsx/finding_detail.tsx)
  // -- a plain text column repeating it is a duplicate, worded the same way as the case above.
  if (byName.workspace_name && byName.workspace_id && loadedRows.length > 0
      && typeof resolveName === "function") {
    const allResolve = loadedRows.every((r) => {
      const nm = resolveName("workspace", r.workspace_id, r.workspace_id);
      return !!nm && nm === r.workspace_name;
    });
    if (allResolve) { hideNames.add("workspace_name"); hideReason.workspace_name = "duplicate"; }
  }

  const NUMERIC_KINDS = new Set(["money", "dbu", "pct", "hours", "gb", "gb_bytes"]);
  const isNumericCol = (c: Column) => {
    if (c.kind !== null && NUMERIC_KINDS.has(c.kind)) return true;
    if (c.kind === "id") return false;
    if (!loadedRows.length) return false;
    let sawNumber = false;
    const allNumericOrEmpty = loadedRows.every((r) => {
      const v = r[c.name];
      if (v === null || v === undefined || v === "") return true;
      if (typeof v === "number" && Number.isFinite(v)) { sawNumber = true; return true; }
      return false;
    });
    return sawNumber && allNumericOrEmpty;
  };

  const toMeta = (c: Column): PlannedColumn => {
    const meta = columnMeta(c.name, c.kind);
    let unit = meta.unit;
    // net_usage_quantity's CURATED default (above) is "DBU" -- overridden here with the REAL
    // unit whenever usage_unit is actually present as a column and every loaded row agrees on one
    // value (a query that can carry more than one unit, e.g. a GB-month storage row, always
    // selects usage_unit itself; cost_by_job.sql is the opposite case -- it filters to DBU in SQL
    // without selecting the column at all, so there is nothing here to read and the default holds).
    if (c.name === "net_usage_quantity" && loadedRows.length > 0) {
      const units = new Set<string>(loadedRows.map((r) => r.usage_unit).filter((u) => u !== null && u !== undefined));
      if (units.size === 1) unit = Array.from(units)[0];
    }
    const label = overrideLabel[c.name] || meta.label;
    // Header shows label + unit inline, so drop the unit when the label already has it as a word.
    if (unit && new RegExp(`(^|\\s)${escapeRegExp(unit)}(\\s|$)`, "i").test(label.trim())) unit = null;
    return {
      name: c.name,
      // CURATED's own kind, when it names one, overrides classify_columns -- only for a handful
      // of columns that ARE a percentage but whose name gives classify_columns's regex no "pct"/
      // "percent" to match (see share_of_window_spend above); every other column keeps its
      // backend-classified kind exactly as before.
      kind: (CURATED[c.name] && CURATED[c.name].kind) || c.kind,
      raw: c.name,
      label,
      unit,
      help: meta.help,
      numeric: isNumericCol(c),
    };
  };

  // Priority order: the entity, then status and its not-assessed reason, then every money column (wasted-first), then every
  // DBU column (wasted-first), then everything else in the SQL's own column order.
  const orderIndex: Record<string, number> = {};
  cols.forEach((c, i) => { orderIndex[c.name] = i; });
  const rank = (c: Column) => {
    if (c.name === entity) return 0;
    if (c.name === "status") return 1;
    if (c.name === "not_assessed_reason") return 1.5; // why a row reads NOT_ASSESSED, next to it
    if (c.kind === "money") return /wasted/i.test(c.name) ? 2 : 3;
    if (c.kind === "dbu") return /wasted/i.test(c.name) ? 4 : 5;
    return 10;
  };
  const visibleRaw = cols.filter((c) => !hideNames.has(c.name));
  const orderedVisible = [...visibleRaw].sort((a, b) => {
    const r = rank(a) - rank(b);
    return r !== 0 ? r : orderIndex[a.name] - orderIndex[b.name];
  });
  const orderedHidden = cols.filter((c) => hideNames.has(c.name));
  const visibleMeta = orderedVisible.map(toMeta);

  return {
    entity,
    entityNoun,
    entityDisplayCol,
    // The column a verdict line/chart/grouped table leads with, or null when this table has no
    // numeric column worth headlining (a pure reference/inventory list) -- see pickKeyMetric.
    keyMetric: pickKeyMetric(visibleMeta, entity, firstOrderByColumn(orderBy)),
    visible: visibleMeta,
    hidden: orderedHidden.map((c) => ({ ...toMeta(c), reason: hideReason[c.name] })),
    totalCount: cols.length,
  };
}
