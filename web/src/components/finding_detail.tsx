// the expand-in-place detail: summary first (a verdict
// sentence with real numbers, a top-N chart, a table grouped by the check's own main entity),
// details on click. Renders exactly one of the four/five honesty-rule cards (PLAN.md 6.2) for a
// non-ok_rows outcome -- never a generic "no data" fallback, and an empty outcome never reads as
// "no problems".

import React from "react";

import { Api } from "../api";
import { fmtCell, fmtDayShort, fmtInt, fmtMoney, isNumericValue } from "../format";
import { CopyLinkButton } from "./hash_state";
import { Names, Ref, resolveName, useNames } from "./names";
import { numOrZero } from "./hooks";
import { bandOf } from "./tab_registry";
import { actionKeyFor, actionTemplateFor } from "./action_templates";
import { Badge, Card, STATUS_WORD, StatusPill, Tip, bandPillKind, floorWord, isUnderFloor } from "./primitives";
import { CHECK_LABELS } from "./labels";
import { shortId } from "./charts";
import { RankedBars } from "./charts_more";
import { FindingScopeBadge, OUTSIDE_REGION_LABEL, OutsideRegionCard, isOutsideRegion, regionWorkspaceNames } from "./scope";
import { columnMeta, curatedColumnLabel, planColumns, sortOrderNote } from "./columns";
import { csvField } from "./findings_table";
import { guideLinkProps, navHref } from "./nav_hash";
import type { Column, FindingData, FindingSummary, Floor, Header, Id, LibraryCorrection, NextCheck, Row } from "../types";
import type { Band } from "./tab_registry";
import type { ColumnPlan, PlannedColumn } from "./columns";
import type { RankedItem } from "./charts_more";

/** A count noun, singular and plural. */
interface Noun {
  s: string;
  p: string;
}

/** One distinct entity in a check's rows: its worst status, row count and key-metric figures. */
interface EntityGroup {
  mapKey: string;
  id: Id;
  name: string | null;
  workspaceId: Id;
  count: number;
  noEntity: boolean;
  worstRank: number;
  worstStatus: string | null;
  sum: number;
  hasMetric: boolean;
  min: number;
  max: number;
  days?: Set<string>;
  avg: number | null;
}

/** The rows grouped by the check's main entity; the chart and grouped table both read it. */
interface EntitySummary {
  entity: string;
  refKind: string | null;
  metric: PlannedColumn | null;
  list: EntityGroup[];
}

// The first fetch grabs up to this many rows in one go (same cap useFindingData/hooks.ts already
// uses everywhere else in this app for a chart/tile that needs the WHOLE table, not a capped
// page) so the verdict line, the chart and the grouped table below are all computed from the real
// data instead of guessing from whatever page happened to load first. "Load more" (raw view only,
// and only once a table's true row count exceeds this) still pages in PAGE_SIZE-sized chunks.
const INITIAL_LIMIT = 5000;
// Grouping, status counts and totals are worked out from the fetched rows, so a check is read in
// full up to this many rows rather than judged from its first page.
const FULL_FETCH_CAP = 50000;
const PAGE_SIZE = 100;

// Whether user names are masked; App sets it from /api/meta, a check's Details reads it.
let appMasking = false;
export function setAppMasking(on: boolean) {
  appMasking = on;
}

function firstSentence(text: unknown): string {
  if (!text) return "";
  const m = String(text).match(/^(.*?[.!?])(\s|$)/);
  return (m ? m[1] : String(text)).trim();
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// tasks/T-61-names-links-job-focus.md: which Ref kind (names.tsx) an "id"-kind column renders
// as, detected by column NAME alone -- a column this does not recognise keeps its existing raw
// fallback (fmtCell), never a blank. Exact names first (the common case across every finding);
// a small suffix set covers the few columns that qualify a role rather than being the plain id
// (e.g. lakeflow_long_running_runs.top_task_cluster_id) -- every one of them still has its own
// workspace_id column on the same row to build a link from.
function refKindForColumn(name: string): string | null {
  if (name === "workspace_id") return "workspace";
  if (name === "job_id") return "job";
  if (name === "run_id") return "run";
  if (name === "cluster_id" || /_cluster_id$/.test(name)) return "cluster";
  if (name === "warehouse_id" || /_warehouse_id$/.test(name)) return "warehouse";
  if (name === "pipeline_id" || /_pipeline_id$/.test(name)) return "pipeline";
  if (name === "notebook_id") return "notebook";
  return null;
}

// A day/date-grain column, by name alone -- "day" (query_pruning_effectiveness) and "usage_date"
// (the cost queries) are this library's own two spellings; a check with neither has no day grain
// at all and its verdict line/entity noun says so ("row", not "day").
function findDayColumn(rawCols: Column[] | null): string | null {
  const hit = (rawCols || []).find((c) => /^day$/i.test(c.name) || (/_date$/i.test(c.name) && !/^(first|last)_/i.test(c.name)));
  return hit ? hit.name : null;
}

const STATUS_TO_BADGE_KIND: Record<string, string> = { CRITICAL: "critical", WARN: "warn", OK: "ok", NOT_ASSESSED: "not_assessed" };
const STATUS_COLOR_VAR: Record<string, string> = { CRITICAL: "var(--st-crit)", WARN: "var(--st-warn)", OK: "var(--st-ok)", NOT_ASSESSED: "var(--st-na)" };
const STATUS_RANK: Record<string, number> = { CRITICAL: 0, WARN: 1, NOT_ASSESSED: 2, OK: 3 };
function statusBadgeKind(status: string): string { return STATUS_TO_BADGE_KIND[status] || "reference"; }
function statusColorVar(status: string | null): string {
  return STATUS_COLOR_VAR[String(status)] || "var(--st-ref)";
}
function rankOfStatus(status: unknown): number | undefined {
  return STATUS_RANK[String(status)];
}
// Rows arrive worst-status-first (the store's own ORDER BY, app/core/data._order_by_clause) --
// so tallying the LOADED page already gives the exact true count for every status rank ahead of
// whichever rank the load actually ran out on; only that one rank can be an undercount. Mirrors
// tab_compute.tsx's own flaggedCountLabel "X+ of Y" convention, just inline on the one figure that
// might be short instead of a second sentence explaining the cap.
function statusCutRank(rows: Row[] | null, rowsTotal: number): number | null | undefined {
  if (!rows || !rows.length || rows.length >= rowsTotal) return null;
  return rankOfStatus(rows[rows.length - 1].status);
}
function countLabel(n: number, isCut: boolean): string { return `${fmtInt(n)}${isCut ? "+" : ""}`; }

function tallyStatusCounts(rows: Row[] | null): Record<string, number> {
  const counts: Record<string, number> = {};
  (rows || []).forEach((r) => {
    const s = r.status;
    if (s === null || s === undefined) return;
    counts[s] = (counts[s] || 0) + 1;
  });
  return counts;
}

// The noun a verdict line/chart/table counts in -- the check's own entity ("warehouse"), combined
// with "-day" when the grain also has a day column (query_pruning_effectiveness's "warehouse-
// day"), or a bare "row"/"day" when there is no entity to name at all.
function grainNounFor(plan: ColumnPlan, dayCol: string | null): Noun {
  if (plan.entity && plan.entityNoun) {
    return dayCol
      ? { s: `${plan.entityNoun.s}-day`, p: `${plan.entityNoun.s}-days` }
      : plan.entityNoun;
  }
  return dayCol ? { s: "day", p: "days" } : { s: "row", p: "rows" };
}

// T-75B review must-fix 1: the verdict line's own headline counted ROWS and labelled them with the
// entity's name ("681 warehouse-days" that was really 163 warehouses' worth of daily rows) --
// wrong whenever more than one row shares an entity (or an entity+day). This tallies the worst
// status per distinct GRAIN UNIT instead: one entity when the check has no day column, one
// (entity, day) pair when it does (matching whatever grainNounFor already calls that combination),
// so "N <noun> CRITICAL" always counts the same units the noun names. Returns null when the check
// has no entity at all -- the raw per-row tally (tallyStatusCounts) is already exactly right then,
// since each row already IS the grain unit.
function buildGrainCounts(rows: Row[], plan: ColumnPlan, dayCol: string | null, hasStatus: boolean): { size: number; counts: Record<string, number> } | null {
  const entity = plan.entity;
  if (!entity) return null;
  const compositeKey = entity === "job_id" || entity === "pipeline_id" || entity === "group_id";
  const counts: Record<string, number> = { CRITICAL: 0, WARN: 0, NOT_ASSESSED: 0, OK: 0 };
  const RANK_STATUS = ["CRITICAL", "WARN", "NOT_ASSESSED", "OK"];
  const map = new Map<string, number | null>();
  (rows || []).forEach((row) => {
    const raw = row[entity];
    // A row with no entity is still one unit, counted under one "(none)" key like buildEntitySummary.
    const hasEntity = raw !== null && raw !== undefined && raw !== "";
    const entityKey = hasEntity ? (compositeKey ? `${row.workspace_id}:${raw}` : String(raw)) : "\u0000none";
    const key = dayCol ? `${entityKey}|${row[dayCol]}` : entityKey;
    if (!hasStatus) { map.set(key, null); return; }
    const r = rankOfStatus(row.status);
    const rr = r === undefined ? 4 : r;
    const cur = map.get(key);
    if (cur == null || rr < cur) map.set(key, rr);
  });
  if (hasStatus) {
    map.forEach((rank) => { if (rank !== null && rank <= 3) counts[RANK_STATUS[rank]] += 1; });
  }
  return { size: map.size, counts };
}

// A metric value shown next to a name in the verdict line -- fmtCell already appends a unit for a
// classified kind (money/dbu/pct/hours/gb); a numeric column with no classified kind (a plain
// count, e.g. query_count) fell through to a bare, unrounded number with no unit at all
// ("-- 1,355.804."). Rounded like every other count in this app, plus the column's own unit or
// (failing that) its label, so a verdict figure never reads as a bare, unlabelled digit.
const UNIT_CARRYING_KINDS = new Set<string | null>(["money", "dbu", "pct", "hours", "gb", "gb_bytes"]);
function metricValueText(metric: PlannedColumn | null, value: unknown, row?: Row): React.ReactNode {
  if (!metric || !isNumericValue(value)) return null;
  if (UNIT_CARRYING_KINDS.has(metric.kind)) return fmtCell(value, metric.kind, row);
  const suffix = metric.unit || metric.label;
  const numText = fmtInt(Math.round(Number(value)));
  return suffix ? `${numText} ${suffix}` : numText;
}

// A resolved name that is not unique among this check's own entities ("Serverless Starter
// Warehouse" matching 7 different warehouses) reads as if it named one thing when it does not --
// disambiguated with the workspace it belongs to, or a short id when even that repeats.
function disambiguateName(name: string, id: Id, workspaceId: Id, list: EntityGroup[] | null | undefined): string {
  if (!name || !list || list.length < 2) return name;
  const dupes = list.filter((g) => g.name === name).length;
  if (dupes <= 1) return name;
  const wsName = workspaceId !== undefined && workspaceId !== null
    ? resolveName("workspace", workspaceId, workspaceId)
    : null;
  const tag = wsName || (id !== undefined && id !== null ? shortId(id) : null);
  return tag ? `${name} (${tag})` : name;
}

// "worst: name — value on 16 Sep" / "largest: name — value" -- the single row app/core/data's own
// ORDER BY already put first (worst status, then the model's own order_by) named in plain words,
// only when there is something worth pointing at (a real entity, and a band worth flagging).
function worstEntityClause(plan: ColumnPlan, worstRow: Row | null, band: Band | null, rawCols: Column[], entitySummary: EntitySummary | null): string | null {
  if (!worstRow || !plan.entity) return null;
  if (band !== "CRITICAL" && band !== "WARN" && band !== "RANKED") return null;
  const rawId = worstRow[plan.entity];
  if (rawId === null || rawId === undefined || rawId === "") return null;
  const refKind = refKindForColumn(plan.entity);
  let name = refKind ? (resolveName(refKind, worstRow.workspace_id, rawId) || String(rawId)) : String(rawId);
  if (refKind) name = disambiguateName(name, rawId, worstRow.workspace_id, entitySummary && entitySummary.list);
  let metricText = "";
  if (plan.keyMetric) {
    const v = metricValueText(plan.keyMetric, worstRow[plan.keyMetric.name], worstRow);
    if (v) metricText = ` — ${v}`;
  }
  const dayCol = findDayColumn(rawCols);
  const dateText = dayCol && worstRow[dayCol] ? ` on ${fmtDayShort(worstRow[dayCol])}` : "";
  const verb = band === "RANKED" ? "largest" : "worst";
  return `${verb}: ${name}${metricText}${dateText}`;
}

// Same sort GroupedTable renders by (worst status first when it means something, else the key
// metric, else row count) -- shared so the verdict line's own "worst"/"largest" clause can never
// name a different entity than the one sitting first in the table right below it.
function sortEntityGroups(list: EntityGroup[], metric: PlannedColumn | null, statusMeaningful: boolean): EntityGroup[] {
  const useAvg = !!(metric && metric.kind === "pct");
  const aggValue = (g: EntityGroup) => (useAvg ? g.avg : g.sum);
  return [...list].sort((a, b) => {
    if (statusMeaningful) {
      const r = a.worstRank - b.worstRank;
      if (r !== 0) return r;
    }
    if (metric) {
      const av = aggValue(a);
      const bv = aggValue(b);
      if (av === null && bv === null) return b.count - a.count;
      if (av === null) return 1;
      if (bv === null) return -1;
      return bv - av;
    }
    return b.count - a.count;
  });
}

// "worst: name — total" / "largest: name — total" from the entity the grouped table itself
// leads with (its own aggregated total/average, not one day's peak) -- preferred over a single
// row's own figure whenever grouping succeeded, so the verdict line is never naming a different
// entity than row one of the table underneath it.
function worstGroupClause(topGroup: EntityGroup | null, plan: ColumnPlan, band: Band | null, list: EntityGroup[] | null | undefined): string | null {
  if (!topGroup) return null;
  if (band !== "CRITICAL" && band !== "WARN" && band !== "RANKED") return null;
  const rawName = topGroup.name || String(topGroup.id);
  const name = disambiguateName(rawName, topGroup.id, topGroup.workspaceId, list);
  const metric = plan.keyMetric;
  let metricText = "";
  if (metric && topGroup.hasMetric) {
    const useAvg = metric.kind === "pct";
    const v = metricValueText(metric, useAvg ? topGroup.avg : topGroup.sum);
    if (v) metricText = ` — ${v}`;
  }
  const verb = band === "RANKED" ? "largest" : "worst";
  return `${verb}: ${name}${metricText}`;
}

// The one verdict sentence: counts by status in the check's own grain noun, plus the entity that
// matters most -- everything a reader used to have to infer from a wide raw table, in one line.
// T-75B review must-fix 1: the headline now counts distinct GRAIN UNITS (buildGrainCounts), never
// raw rows, whenever the check has an entity to count -- a raw-row total that collapsed onto fewer
// units is kept as a parenthetical ("4 grantees CRITICAL (3,432 rows)") rather than silently
// dropped, so the reader can still see how many individual rows that headline came from.
function buildVerdictText(data: FindingData, plan: ColumnPlan, band: Band | null, hasStatus: boolean, statusCounts: Record<string, number>,
  worstRow: Row | null, topGroup: EntityGroup | null, entitySummary: EntitySummary | null): string {
  const dayCol = findDayColumn(data.columns);
  const noun = grainNounFor(plan, dayCol);
  const plural = (n: number, w: Noun) => (n === 1 ? w.s : w.p);
  const cutRank = statusCutRank(data.rows, data.rows_total);
  const grain = plan.entity ? buildGrainCounts(data.rows, plan, dayCol, hasStatus) : null;
  const total = grain ? grain.size : data.rows_total;
  const counts = grain ? grain.counts : statusCounts;
  const rawNote = (word: string) => {
    if (!grain) return "";
    const raw = (statusCounts && statusCounts[word]) || 0;
    const distinct = counts[word] || 0;
    return raw > distinct ? ` across ${fmtInt(raw)} rows` : "";
  };

  let main: string;
  if (!hasStatus) {
    main = `${fmtInt(total)} ${plural(total, noun)}`;
  } else if (band === "RANKED") {
    main = plan.keyMetric
      ? `${fmtInt(total)} ${plural(total, noun)} ranked by ${plan.keyMetric.label}`
      : `${fmtInt(total)} ${plural(total, noun)}`;
  } else {
    const critC = counts.CRITICAL || 0;
    const warnC = counts.WARN || 0;
    if (critC > 0 || warnC > 0) {
      const raw: [string, string, string][] = [];
      if (critC > 0) raw.push([countLabel(critC, cutRank === 0), "CRITICAL", rawNote("CRITICAL")]);
      if (warnC > 0) raw.push([countLabel(warnC, cutRank === 1), "WARN", rawNote("WARN")]);
      main = raw.map(([label, word, note], i) => `${label}${i === 0 ? ` ${plural(critC || warnC, noun)}` : ""} ${word}${note}`).join(", ");
    } else {
      const okC = counts.OK || 0;
      const naC = counts.NOT_ASSESSED || 0;
      if (okC > 0 && naC === 0) main = `${fmtInt(total)} ${plural(total, noun)}, all OK`;
      else if (naC > 0 && okC === 0) main = `${fmtInt(total)} ${plural(total, noun)}, none could be judged (not assessed)`;
      else main = `${fmtInt(total)} ${plural(total, noun)}: ${fmtInt(okC)} OK, ${fmtInt(naC)} not assessed`;
    }
  }

  const clause = topGroup
    ? worstGroupClause(topGroup, plan, band, entitySummary && entitySummary.list)
    : worstEntityClause(plan, worstRow, band, data.columns, entitySummary);
  const sentence = clause ? `${main}; ${clause}` : main;
  return `${sentence}.`;
}

// A bare snake_case token inside an action SENTENCE -- a column name (scaling_hint) or another
// check's own query_id (cost_dollarized_by_sku_day) -- read as raw code to anyone outside this
// codebase. Never matches the ALL-CAPS enum values (SCALE_UP_MEMORY) those sentences also carry;
// those are already meant to read as fixed vocabulary, not a column to translate.
// Skips a part of a dotted name (system.storage.table_metrics_history stays a table name).
const ACTION_TOKEN_RE = /(?<![.\w])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![.\w])/g;
function actionToken(name: string): string {
  const check = CHECK_LABELS[name];
  if (check) return check.title;
  // Mid-sentence: plain lower-case words ("read the error message sample"), never a Title Case header.
  const words = curatedColumnLabel(name) || name.replace(/_id$/, "").replace(/_/g, " ");
  return /^[A-Z][a-z]/.test(words) ? words.charAt(0).toLowerCase() + words.slice(1) : words;
}

// header.actions[0] is already this check's own most-immediate fix (a list, most-actionable
// first) -- cut to its first clause (the full multi-branch instruction is one click away, in
// Details' own Actions list) and every raw column name or query_id inside it turned into its
// plain label, so the one line shown by default never reads as SQL.
// Plain "Fix now" lines for checks whose first header action is written for someone reading the
// raw table (field names, " - " asides); the header keeps the full detail for the Guide.
const FIX_NOW: Record<string, string> = {
  cost_serving_mode_by_endpoint: "Open the flagged endpoint on the Endpoints tab and check its size against its traffic: it billed over the $/day line, so right-size its provisioned capacity or move a low-traffic endpoint to pay-per-token.",
  lakeflow_job_oversized: "Resize the job as its suggested fix says: one node size down, or fewer workers and a lower autoscale max.",
  lakeflow_job_compute_pressure: "Fix the cause the job's pressure names (memory, skew, driver, I/O or idle) before adding hardware; the flagged row says which.",
  task_cluster_utilization: "Open the flagged task run's Spark UI and look at the stage its bottleneck hint points to.",
  lakeflow_long_running_runs: "Open the flagged run's slowest task: a high wait share means cluster start or queueing, otherwise compare its input with a normal run.",
  lakeflow_job_ownership_orphans: "Move a flagged job, especially a scheduled one, onto a service principal run-as.",
  compute_cluster_config_posture: "Set a Unity Catalog access mode (user isolation or single user) on a critical cluster, and attach an existing cluster policy.",
  compute_warehouse_idle_minutes: "When most idle time is the wait after the last query, lower the warehouse's auto-stop.",
  sql_warehouse_events_activity: "If a warehouse hasn't started or run for weeks, confirm with its owner it's still needed before changing it.",
};

export function actionLine(actions: string[] | null | undefined, queryId: string | null | undefined): string | null {
  if (queryId && FIX_NOW[queryId]) return FIX_NOW[queryId];
  if (!actions || !actions.length) return null;
  let s = String(actions[0]).trim();
  const cut = s.search(/;| - /);
  if (cut > 0) s = s.slice(0, cut);
  s = s.replace(ACTION_TOKEN_RE, actionToken).replace(/[;,]+$/, "").trim();
  if (!s) return null;
  if (!/[.!?]$/.test(s)) s += ".";
  return s;
}

// One (entity, worst status, row count, key-metric total) per distinct entity value, computed
// from every loaded row (up to INITIAL_LIMIT) -- the source both the grouped table and the top-N
// chart read from, so the two can never disagree. job_id/pipeline_id are unique only WITHIN a
// workspace (names.tsx's own dim-key rule), so those two group on workspace_id+id, never the bare
// id alone, or two different jobs that happen to share a number would silently merge into one row.
//
// T-75B review must-fix 4: a row with NO entity value (query_pruning_effectiveness's serverless
// rows, which carry no warehouse_id) used to be dropped from this map entirely -- invisible in
// both the chart and the table, however large its own share of the total. Those rows now land in
// one explicit "(no <entity>)" group instead of vanishing.
const COMPUTE_TYPE_LABEL: Record<string, string> = { SERVERLESS_COMPUTE: "Serverless notebooks & jobs", CLUSTER: "Clusters (notebooks & jobs)" };

function buildEntitySummary(rows: Row[], plan: ColumnPlan, hasStatus: boolean, dayCol: string | null): EntitySummary | null {
  const entity = plan.entity;
  if (!entity) return null;
  const refKind = refKindForColumn(entity);
  const metric = plan.keyMetric;
  const displayCol = plan.entityDisplayCol;
  const noEntityName = entity === "endpoint_name" ? "(no endpoint name)" : `(no ${(plan.entityNoun && plan.entityNoun.s) || "entity"})`;
  const compositeKey = entity === "job_id" || entity === "pipeline_id" || entity === "group_id";
  const map = new Map<string, Omit<EntityGroup, "avg">>();
  (rows || []).forEach((row) => {
    const raw = row[entity];
    const hasEntity = raw !== null && raw !== undefined && raw !== "";
    // A statement with no warehouse ran on other compute: name it by that compute type.
    const otherCompute = !hasEntity && row.compute_type ? row.compute_type : null;
    // Unnamed endpoints stay apart per workspace, as the ML & AI chart shows them.
    const noneKey = entity === "endpoint_name" && row.workspace_id != null ? `none:${row.workspace_id}` : "none";
    const mapKey = hasEntity ? (compositeKey ? `${row.workspace_id}:${raw}` : String(raw)) : `\u0000${otherCompute || noneKey}`;
    let g = map.get(mapKey);
    if (!g) {
      g = {
        mapKey, id: hasEntity ? raw : null,
        name: hasEntity ? null : (COMPUTE_TYPE_LABEL[String(otherCompute)] || (otherCompute ? `(${otherCompute.toLowerCase()})` : noEntityName)),
        workspaceId: row.workspace_id, count: 0, noEntity: !hasEntity,
        worstRank: 5, worstStatus: null, sum: 0, hasMetric: false, min: Infinity, max: -Infinity,
      };
      map.set(mapKey, g);
    }
    g.count += 1;
    // Several rows can share a day (per statement type, per error): days are counted once.
    if (dayCol && row[dayCol] != null) (g.days || (g.days = new Set())).add(String(row[dayCol]));
    if (hasStatus) {
      const r = rankOfStatus(row.status);
      const rr = r === undefined ? 4 : r;
      if (rr < g.worstRank) { g.worstRank = rr; g.worstStatus = row.status; }
    }
    if (metric) {
      const v = row[metric.name];
      if (typeof v === "number" && Number.isFinite(v)) {
        g.sum += v;
        g.hasMetric = true;
        if (v < g.min) g.min = v;
        if (v > g.max) g.max = v;
      }
    }
    // A non-id entity column already IS the name; an id entity with its own display column
    // (table_id -> table_name, T-75B review item 5) shows THAT instead of the raw id.
    if (hasEntity && !refKind && g.name === null) g.name = displayCol ? (row[displayCol] || raw) : raw;
  });
  const list = Array.from(map.values()).map((g): EntityGroup => ({
    ...g,
    name: g.noEntity ? g.name : (refKind ? (resolveName(refKind, g.workspaceId, g.id) || null) : g.name),
    avg: g.hasMetric ? g.sum / g.count : null,
  }));
  return { entity, refKind, metric, list };
}

// T-75B review should-fix "header still carries extra chips": starred/needs-confirmation/scope
// used to sit in the header next to the pill and window chip, competing with them -- they read as
// detail, not verdict, so they move in here with everything else a reader only needs on request.
// A check's caveats are written for reviewers and can run to hundreds of words: the first two
// plain sentences here, the rest in the Guide. Sentences about masking are replaced by what the
// current setting actually does.
const MASKING_SENTENCE_RE = /\bMASKING\b|masked in-SQL|\bREDACTED\b|sha2\(|\bmask(ed|ing)\b[^.]*\b(user|identit|email|principal|run_as|owned_by|created_by|people|person)/;
function readerCaveats(text: string | null | undefined): { text: string; more: boolean } | null {
  if (!text) return null;
  const sentences = String(text).split(/(?<=[.!?])\s+(?=[A-Z])/).map((x) => x.trim()).filter(Boolean);
  const plain = sentences.filter((x) => !MASKING_SENTENCE_RE.test(x));
  const hadMasking = plain.length < sentences.length;
  const shown = plain.slice(0, 2);
  let out = shown.join(" ");
  if (hadMasking) {
    out += (out ? " " : "") + (appMasking
      ? "People's names and emails are masked here (privacy setting on)."
      : "People's names and emails are shown in full (masking is off in Settings).");
  }
  return { text: out, more: plain.length > shown.length };
}

function Guidance({ header, queryId, hasStatus, stars, confidenceNote, scopeBadge }: {
  header: Header; queryId: string; hasStatus: boolean; stars: boolean; confidenceNote: string | null; scopeBadge: React.ReactNode;
}) {
  // Fully collapsed by default -- the verdict line above already carries the grain's own numbers,
  // so nothing here needs to compete with the table for space; "Details" is a plain disclosure.
  const [open, setOpen] = React.useState(false);
  const allFields: [string, React.ReactNode][] = [
    ["What this is", header.read_this],
    ["Healthy looks like", header.healthy],
    ["Investigate if", header.investigate_if],
    ["Caveats", (() => {
      const c = readerCaveats(header.caveats);
      if (!c || !c.text) return null;
      return c.more ? <React.Fragment>{c.text} <a {...guideLinkProps(queryId)}>{"More in the Guide →"}</a></React.Fragment> : c.text;
    })()],
  ];
  const fields = allFields.filter(([, v]) => v && !(typeof v === "string" && v.toLowerCase().startsWith("n/a")));
  if (hasStatus) fields.push(["Status bands", columnMeta("status", null).help]);
  if (stars) fields.push(["Starred", "Marked as a priority check to watch."]);
  if (confidenceNote) fields.push(["Needs confirmation", confidenceNote]);
  if (scopeBadge) fields.push(["Scope", scopeBadge]);

  return (
    <div className={`guidance ${open ? "open" : ""}`}>
      <div className="g-lede">
        <button type="button" className="g-toggle" onClick={() => setOpen(!open)}>
          {open ? "hide details" : "Details"}
        </button>
      </div>
      {open && (
        <React.Fragment>
          <div className="d-grid">
            {fields.map(([k, v]) => (
              <div className="d-field" key={k}>
                <div className="k">{k}</div>
                <div className="v">{v}</div>
              </div>
            ))}
          </div>
          {header.actions && header.actions.length > 0 && (
            <div className="d-actions">
              <div className="k">Actions</div>
              <ol>{header.actions.map((a, i) => <li key={i}>{a}</li>)}</ol>
            </div>
          )}
          <a className="g-guide-link" {...guideLinkProps(queryId)}>{"How this check works →"}</a>
        </React.Fragment>
      )}
    </div>
  );
}

// T-75A (DEC-66.2): the library-corrections register's own entries for this query_id, if any --
// a small "N known issue(s)"/"corrected" badge that opens the full problem/effect/fix list on
// click; no sentence competing with the verdict line above it. Renders nothing when the array is
// empty (the common case -- most vendored queries carry no known defect).
function LibraryIssueBadge({ corrections }: { corrections: LibraryCorrection[] | null }) {
  const [open, setOpen] = React.useState(false);
  if (!corrections || corrections.length === 0) return null;
  const allFixed = corrections.every((c) => c.status === "fixed");
  const label = allFixed ? `${corrections.length} fix${corrections.length === 1 ? "" : "es"} applied to the query` : `${corrections.length} known issue${corrections.length === 1 ? "" : "s"}`;
  const kind = allFixed ? "ok" : "warn";
  return (
    <div className={`guidance lib-corrections ${open ? "open" : ""}`}>
      <button type="button" className="lib-badge-trigger" onClick={() => setOpen(!open)}
        title={allFixed ? "This check's query differs from the Databricks sample query it came from; click for what changed and why." : "Known issues with this check's query; click for their effect on the numbers."}>
        <Badge kind={kind}>{label}</Badge>
      </button>
      {open && (
        <div className="d-grid" style={{ gridTemplateColumns: "1fr" }}>
          {corrections.map((c) => (
            <div className="d-field" key={c.id}>
              <div className="k">
                <Badge kind={c.status === "fixed" ? "ok" : "warn"}>
                  {c.status === "fixed" ? "Corrected" : "Not fixed yet"}
                </Badge>
              </div>
              <div className="v">
                <div><b>What's wrong: </b>{c.problem}</div>
                <div><b>Effect on this finding: </b>{c.effect}</div>
                <div><b>{c.status === "fixed" ? "Fix: " : "Fix or plan: "}</b>{c.fix}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// P2-PARTIAL: a source app_core_data.finding_status() classified as name-lookup-only or
// partially-exported never blocks a finding any more (it still computes), but the finding is not
// perfectly clean either -- this renders a small note saying exactly what is degraded/partial and
// why, on top of whichever outcome card the finding actually reached (never its own card, so it
// reads next to real rows/an empty window/etc. instead of pre-empting them).
const NAME_COLUMN_LABEL: Record<string, string> = {
  workspace_name: "Workspace names",
  job_name: "Job names",
  pipeline_name: "Pipeline names",
  cluster_name: "Cluster names",
  warehouse_name: "Warehouse names",
};

function DataQualityNote({ data }: { data: FindingData }) {
  const degraded = data.degraded_sources || [];
  const partial = data.partial_sources || [];
  if (degraded.length === 0 && partial.length === 0) return null;
  return (
    <div className="honest-card warn dq-note">
      <div className="h-title">Partial data</div>
      {degraded.map((s) => (
        <div className="h-note" key={`degraded-${s.source}`}>
          {(s.degrades && NAME_COLUMN_LABEL[s.degrades]) || "Names"} could not be looked up, so ids show instead
          -- <span className="mono">{s.source}</span>{s.reason ? ` (${s.reason})` : ""}
        </div>
      ))}
      {partial.map((s) => (
        <div className="h-note" key={`partial-${s.source}`}>
          <span className="mono">{s.source}</span> only exported part of its window
          {s.missing_days && s.missing_days.length > 0
            ? ` -- missing ${s.missing_days.join(", ")}`
            : ""}
          {s.reason ? ` (${s.reason})` : ""}
        </div>
      ))}
    </div>
  );
}

function EmptyIfList({ tokens }: { tokens: string[] | null | undefined }) {
  if (!tokens || !tokens.length) return null;
  return (
    <div>
      <div className="d-field"><div className="k">Why this could be empty</div></div>
      <ul>
        {tokens.map((t) => (
          <li key={t}>{String(t).replace(/_/g, " ")}</li>
        ))}
      </ul>
    </div>
  );
}

// A raw system-table source name ("system.information_schema.tables") reads better as its last,
// plain-word segment than as a dotted identifier -- used only for the one-line summary; the raw
// name still appears verbatim in Details, since that is exactly what a reader would search the
// Databricks docs for.
function friendlySourceName(src: string | null | undefined): string {
  if (!src) return "unknown source";
  const last = String(src).split(".").pop() || "";
  return last.replace(/_/g, " ");
}

// T-75B review should-fix "not-assessed card shows the full SQL error": `info.message` is this
// model's own raw exception text (can be a multi-line "Search path: ..." dump) -- now one line
// naming which source is missing, with that raw text (and the per-source detail) behind Details.
export function NotAssessedCard({ data }: { data: Partial<FindingData> }) {
  const info = data.status_info || {};
  if (info.outside_region) return <OutsideRegionCard info={info} />;
  const [open, setOpen] = React.useState(false);
  const blocking = info.blocking_sources || [];
  const oneLine = info.not_built_reason
    || (blocking.length > 0
      ? `Source not ok: ${blocking.map((s) => friendlySourceName(s.source)).join(", ")}.`
      : (info.status ? `Model status: ${info.status}.` : "This check could not be assessed."));
  const hasDetail = !!(info.message || info.error_class || blocking.length > 0);
  return (
    <div className="honest-card not_assessed">
      <div className="h-title">Not assessed</div>
      <div className="h-note">{oneLine}</div>
      <div className="h-note">Unknown for now, not a clean result.</div>
      {hasDetail && (
        <React.Fragment>
          <button type="button" className="g-toggle" onClick={() => setOpen(!open)}>
            {open ? "hide details" : "Details"}
          </button>
          {open && (
            <div className="d-grid" style={{ gridTemplateColumns: "1fr" }}>
              {info.error_class && (
                <div className="d-field"><div className="k">Error</div><div className="v mono">{info.error_class}</div></div>
              )}
              {blocking.length > 0 && (
                <div className="d-field">
                  <div className="k">Sources not ok</div>
                  <div className="v">
                    <ul>
                      {blocking.map((s) => (
                        <li key={s.source}>
                          <span className="mono">{s.source}</span> -- {s.state}
                          {s.reason ? ` (${s.reason})` : ""}
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
              {info.message && (
                <div className="d-field"><div className="k">Raw error</div><div className="v mono">{info.message}</div></div>
              )}
            </div>
          )}
        </React.Fragment>
      )}
    </div>
  );
}

// T-75B review should-fix "empty state is text-heavy and out of order": "a longer window may have
// rows" only makes sense on a windowed check (a snapshot has no other window to try); raw source
// names/states move behind Details instead of an always-open list.
const CELL_CARRIES_UNIT = new Set<string | null>(["money", "dbu", "pct", "hours"]);

// Every column of every loaded row, raw values, for evidence outside the app.
function downloadFindingRows(data: FindingData) {
  const cols = (data.columns || []).map((c) => c.name);
  const lines = [cols.map(csvField).join(",")];
  (data.rows || []).forEach((r) => lines.push(cols.map((c) => csvField(r[c])).join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
  a.download = `${data.query_id}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

export function EmptyWindowCard({ data }: { data: Partial<FindingData> }) {
  const sources = (data.window_coverage && data.window_coverage.sources) || {};
  const [open, setOpen] = React.useState(false);
  return (
    <div className="honest-card empty_window">
      <div className="h-title">Nothing in window</div>
      <div className="h-note">
        Nothing matched in this window. This is not a verified zero -- see the source coverage
        below before reading it as "no problems".
      </div>
      {data.windowed && <div className="h-note">A longer window may have rows for this.</div>}
      <EmptyIfList tokens={data.header && data.header.empty_if} />
      {Object.keys(sources).length > 0 && (
        <React.Fragment>
          <button type="button" className="g-toggle" onClick={() => setOpen(!open)}>
            {open ? "hide source coverage" : "Source coverage"}
          </button>
          {open && (
            <ul>
              {Object.entries(sources).map(([src, info]) => (
                <li key={src}>
                  {friendlySourceName(src)} -- {info.state || "unknown"}
                  {info.partial ? " (partial -- snapshot shorter than this window)" : ""}
                  {" "}<span className="mono muted">{src}</span>
                </li>
              ))}
            </ul>
          )}
        </React.Fragment>
      )}
    </div>
  );
}

// A materiality floor (config/materiality.yml) already moved every below-floor CRITICAL/WARN row
// to OK before this data ever reached the browser (app/core/materiality.py) -- floor.label is
// already the full plain-word phrase ("100 GB scanned"), so this is one sentence, not a rebuild
// from column/min/unit. Nothing renders when this query has no floor, or the floor caught nothing.
function FloorLine({ floor }: { floor: Floor | null }) {
  if (!floor || !floor.rows_below) return null;
  const n = floor.rows_below;
  return (
    <div className="floor-line muted">
      {`${fmtInt(n)} row${n === 1 ? "" : "s"} under ${floor.label} not judged.`}
    </div>
  );
}

function EmptyFiltersCard({ data }: { data: FindingData }) {
  return (
    <div className="honest-card empty_filters">
      <div className="h-title">Excluded by current filters</div>
      <div className="h-note">
        {fmtInt(data.rows_in_window)} row{data.rows_in_window === 1 ? "" : "s"} exist in this
        window, but the current workspace/env filters exclude every one of them.
      </div>
    </div>
  );
}

export function ErrorCard({ data }: { data: Partial<FindingData> }) {
  return (
    <div className="honest-card error">
      <div className="h-title">Could not read this table</div>
      <div className="h-note mono">{data.error}</div>
      <div className="h-note">
        dbt may be running -- the database file can be briefly locked during a rebuild. Reload to
        retry.
      </div>
    </div>
  );
}

// F1 (U-UI-01): the box below is the one thing that scrolls sideways for a wide finding (styles.
// css's .data-table-wrap comment has the root cause). Nothing on screen said so, and its own
// horizontal scrollbar sits at the BOTTOM of a 460px box, easy to miss -- so a reader who never
// scrolled down that far read the table as simply cut off at the right edge. This ref + these two
// handlers are the "is there more, and how much" signal: a muted count above the box, a fading
// right edge, and a click-to-scroll button, all driven by comparing the scrollable width against
// what is actually visible -- and all three disappear together the moment nothing is cut off.
function useHorizontalScrollCue(depsKey: string) {
  const wrapRef = React.useRef<HTMLDivElement>(null);
  const [state, setState] = React.useState({ needsScroll: false, scrolledLeft: false, moreCols: 0 });

  const measure = React.useCallback(() => {
    const el = wrapRef.current;
    if (!el) return;
    const needsScroll = el.scrollWidth > el.clientWidth + 1;
    const scrolledLeft = el.scrollLeft > 0;
    let moreCols = 0;
    if (needsScroll) {
      const visibleRight = el.scrollLeft + el.clientWidth;
      el.querySelectorAll<HTMLElement>("table.data thead th").forEach((th) => {
        if (th.offsetLeft + th.offsetWidth > visibleRight + 4) moreCols += 1;
      });
    }
    setState({ needsScroll, scrolledLeft, moreCols });
  }, []);

  React.useEffect(() => {
    measure();
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [depsKey, measure]);

  const scrollMore = () => {
    const el = wrapRef.current;
    if (el) el.scrollBy({ left: el.clientWidth * 0.8, behavior: "smooth" });
  };

  return { wrapRef, ...state, onScroll: measure, scrollMore };
}

// What a hidden column's own reason (columns.ts's planColumns) reads as in the Columns menu.
function hideReasonText(reason: string): string {
  return ({
    window: "same on every row -- the window chip above already says this",
    discount: "superseded by the discounted column",
    duplicate: "identical to another visible column",
  } as Record<string, string>)[reason] || reason;
}

// A small "Columns (N hidden)" menu -- replaces the old always-on "N columns -- scroll sideways"
// sentence and the plain show/hide button with one compact trigger that also says WHY each column
// is hidden (columns.ts already computes the reason; nothing showed it before this).
function ColumnsMenu({ plan, showAll, onToggleAll }: { plan: ColumnPlan; showAll: boolean; onToggleAll: () => void }) {
  const [open, setOpen] = React.useState(false);
  const boxRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e: MouseEvent) => { if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false); };
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);
  if (!plan.hidden.length) return null;
  return (
    <div className="ws-picker columns-menu" ref={boxRef}>
      <button type="button" className="ws-trigger" onClick={() => setOpen((o) => !o)}>
        {`Columns (${plan.hidden.length} hidden)`} <span className="caret">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div className="ws-pop">
          <div className="ws-list">
            {plan.hidden.map((c) => (
              <div className="ws-row columns-hidden-row" key={c.name}>
                <span className="ws-name">{c.label}</span>
                <span className="muted">{hideReasonText(c.reason)}</span>
              </div>
            ))}
          </div>
          <div className="ws-foot">
            <button type="button" className="ck-link" onClick={() => { onToggleAll(); setOpen(false); }}>
              {showAll ? "Hide these again" : `Show all ${plan.totalCount} columns`}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function DataTable({ data, plan, onLoadMore, loadingMore }: { data: FindingData; plan: ColumnPlan; onLoadMore: () => void; loadingMore: boolean }) {
  const rows = data.rows || [];
  const [showAll, setShowAll] = React.useState(false);
  const cols = showAll ? [...plan.visible, ...plan.hidden] : plan.visible;
  const scrollCue = useHorizontalScrollCue(`${cols.length}:${rows.length}`);

  // One line stating the $ basis ONCE for the whole table, instead of repeating "(list price
  // (effective))" after every money header (the old per-header suffix this replaces). The
  // compared string is app/api/service.py's own est_label text, which now matches format.tsx's
  // client-side estLabel word for word.
  const rawCols = data.columns || [];
  const moneyCol = rawCols.find((c) => c.kind === "money" && c.label);
  const moneyNote = moneyCol
    ? (moneyCol.label === "list price (effective)"
        ? "$ = list price (effective), not your invoice"
        : `$ = ${moneyCol.label}`)
    : null;

  return (
    <div>
      {moneyNote && <div className="data-money-note muted">{moneyNote}</div>}
      <ColumnsMenu plan={plan} showAll={showAll} onToggleAll={() => setShowAll(!showAll)} />
      <div className="data-table-outer">
      <div
        className={`data-table-wrap ${scrollCue.scrolledLeft ? "scrolled" : ""}`}
        ref={scrollCue.wrapRef}
        onScroll={scrollCue.onScroll}
      >
        <table className="data">
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.name}>
                  <Tip text={`${c.help ? `${c.help} ` : ""}column: ${c.raw}`} placement="down">
                    <div className="th-label">
                      {c.label}
                      {c.unit && !CELL_CARRIES_UNIT.has(c.kind) && <span className="th-unit">{" "}{c.unit}</span>}
                    </div>
                  </Tip>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className={row.below_floor ? "row-below-floor" : undefined}>
                {cols.map((c) => {
                  const value = row[c.name];
                  const underFloor = c.name === "status" && isUnderFloor(row);
                  const statusClass = c.name === "status" && value && !underFloor ? ` status-${value}` : "";
                  const refKind = c.kind === "id" ? refKindForColumn(c.name) : null;
                  const hasValue = value !== null && value !== undefined && value !== "";
                  let content: React.ReactNode;
                  if (refKind && hasValue) {
                    const wsId = row.workspace_id;
                    content = refKind === "run"
                      ? <Ref kind="run" id={row.job_id} workspaceId={wsId} runId={value} />
                      : <Ref kind={refKind} id={value} workspaceId={wsId} />;
                  } else if (c.name === "not_assessed_reason" && hasValue) {
                    // P4-03: the header's own not_assessed_reasons dict (service.
                    // substitute_header_params) puts this code into words -- never the raw code
                    // (e.g. "no_cluster_recorded") a reader has no way to look up.
                    const words = data.header && data.header.not_assessed_reasons;
                    content = (words && words[value]) || value;
                  } else if (underFloor) {
                    content = floorWord(data.floor);
                  } else if (c.name === "status" && hasValue) {
                    content = <StatusPill kind={String(value).toLowerCase()} compact />;
                  } else {
                    content = fmtCell(value, c.kind, row);
                  }
                  return (
                    <td
                      key={c.name}
                      className={(c.numeric ? "num" : c.kind === "id" ? "id" : "") + statusClass}
                    >
                      {content}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {scrollCue.needsScroll && scrollCue.moreCols > 0 && (
        <React.Fragment>
          <div className="data-scroll-fade" />
          <button type="button" className="data-scroll-more" onClick={scrollCue.scrollMore}>
            &rarr; {scrollCue.moreCols} more column{scrollCue.moreCols === 1 ? "" : "s"}
          </button>
        </React.Fragment>
      )}
      </div>
      {data.truncated && (
        <div className="data-truncated-note muted">
          Worst {fmtInt(data.truncated_max_rows)} rows shown; the check returned more.
        </div>
      )}
      <div className="data-foot">
        <span className="muted mono">
          Showing {fmtInt(data.offset + rows.length)} of {fmtInt(data.rows_total)} rows
          {data.order_by ? ` -- ${sortOrderNote(data.order_by)}` : ""}
        </span>
        {data.offset + rows.length < data.rows_total && (
          <button className="load-more" disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? "Loading..." : `Load ${Math.min(PAGE_SIZE, data.rows_total - data.offset - rows.length)} more`}
          </button>
        )}
      </div>
    </div>
  );
}

// The default view for a check with a real entity: one row per entity, worst status first, the
// key metric aggregated across it -- exactly what the old wide daily-rows table forced a reader
// to reconstruct in their head. "Total" for an additive figure (money/DBUs/hours/GB/a plain
// count); "Average" for a rate (a pct column) -- summing a percentage across rows is meaningless.
// A check whose entity has real cardinality (hundreds of jobs/tables) would otherwise turn
// "grouped" into just as long a scroll as the raw table it replaces -- capped at a page, same as
// the raw table's own "Load more", with a plain "Show all N" past it rather than silently cutting
// the list.
const GROUPED_PAGE_SIZE = 25;

// T-75B review should-fix "grouped table headers are unclear": "Rows" reads as "Days" when the
// check groups by day (each row already IS one day for that entity); the metric column says
// Total/Avg up front instead of only in its hover tip.
function GroupedTable({ summary, plan, statusMeaningful, dayCol }: { summary: EntitySummary; plan: ColumnPlan; statusMeaningful: boolean; dayCol: string | null }) {
  const [showAll, setShowAll] = React.useState(false);
  const noun = plan.entityNoun || { s: "row", p: "rows" };
  const metric = plan.keyMetric;
  const useAvg = !!(metric && metric.kind === "pct");
  const aggValue = (g: EntityGroup) => (useAvg ? g.avg : g.sum);
  const sortedAll = sortEntityGroups(summary.list, metric, statusMeaningful);
  const sorted = showAll ? sortedAll : sortedAll.slice(0, GROUPED_PAGE_SIZE);
  const countWord = dayCol ? "Days" : "Rows";
  const metricPrefix = useAvg ? "Avg" : "Total";
  return (
    <div className="data-table-outer">
      <table className="data">
        <thead>
          <tr>
            <th>
              <Tip text={`One row = one ${noun.s}, combining every loaded row for it.`} placement="down">
                <div className="th-label">{capitalize(noun.s)}</div>
              </Tip>
            </th>
            {statusMeaningful && (
              <th>
                <Tip text={columnMeta("status", null).help} placement="down">
                  <div className="th-label">Worst status</div>
                </Tip>
              </th>
            )}
            <th>
              <Tip text={`How many of the loaded ${dayCol ? "days" : "rows"} this ${noun.s} covers.`} placement="down">
                <div className="th-label">{countWord}</div>
              </Tip>
            </th>
            {metric && (
              <th>
                <Tip
                  text={`${useAvg ? "Average" : "Total"} ${metric.label}${metric.help ? ` -- ${metric.help}` : ""}, across every row for this ${noun.s}.`}
                  placement="down"
                >
                  <div className="th-label">
                    {metricPrefix} {metric.label}
                    {metric.unit && <span className="th-unit">{" "}{metric.unit}</span>}
                  </div>
                </Tip>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {sorted.map((g) => (
            <tr key={g.mapKey}>
              <td className={summary.refKind && !g.noEntity ? "id" : ""}>
                {summary.refKind && !g.noEntity
                  ? <Ref kind={summary.refKind} id={g.id} workspaceId={g.workspaceId} />
                  : (g.name || String(g.id))}
              </td>
              {statusMeaningful && (
                <td className={g.worstStatus ? `status-${g.worstStatus}` : ""}>
                  {g.worstStatus
                    ? <Badge kind={statusBadgeKind(g.worstStatus)}>{STATUS_WORD[statusBadgeKind(g.worstStatus)]}</Badge>
                    : <span className="muted">{"—"}</span>}
                </td>
              )}
              <td className="num">{fmtInt(dayCol && g.days ? g.days.size : g.count)}</td>
              {metric && (
                <td className="num">
                  {g.hasMetric ? (
                    <Tip text={`min ${fmtCell(g.min, metric.kind)} · max ${fmtCell(g.max, metric.kind)} across ${fmtInt(g.count)} ${g.count === 1 ? noun.s : noun.p}`}>
                      {fmtCell(aggValue(g), metric.kind)}
                    </Tip>
                  ) : <span className="muted">{"—"}</span>}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {sortedAll.length > GROUPED_PAGE_SIZE && (
        <div className="data-foot">
          <span className="muted mono">
            Showing {fmtInt(sorted.length)} of {fmtInt(sortedAll.length)} {noun.p}
          </span>
          <button type="button" className="load-more" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Show fewer" : `Show all ${fmtInt(sortedAll.length)} ${noun.p}`}
          </button>
        </div>
      )}
    </div>
  );
}

// T-75B review must-fix 3 (accepted fallback: "or at least mark the table as partial"): the
// entity summary is built only from the rows this page actually fetched (INITIAL_LIMIT), so a
// check with more rows than that has a grouped table/chart built from a subset -- said here
// rather than silently shown as if it were the whole table.
function PartialGroupNote({ data }: { data: FindingData }) {
  if (!data.rows || data.rows.length >= data.rows_total) return null;
  return (
    // Reuses data-money-note's own spacing (styles.css, owned by another branch right now) rather
    // than adding a new selector to a file two tasks are editing at once.
    <div className="data-money-note muted">
      {`Grouped from the first ${fmtInt(data.rows.length)} of ${fmtInt(data.rows_total)} rows -- totals below may be undercounts.`}
    </div>
  );
}

// The toggle between the grouped default and the old raw-rows view -- only shown when this check
// actually has an entity to group by; a check with none (or a pure reference list with nothing to
// aggregate) just shows the raw table, exactly as before.
function FindingTableSection({ data, plan, canGroup, entitySummary, statusMeaningful, dayCol, loadingMore, onLoadMore }: {
  data: FindingData; plan: ColumnPlan; canGroup: boolean; entitySummary: EntitySummary | null; statusMeaningful: boolean;
  dayCol: string | null; loadingMore: boolean; onLoadMore: () => void;
}) {
  const [showRaw, setShowRaw] = React.useState(!canGroup);
  // canGroup implies an entity, so the plan has its noun.
  const noun = plan.entityNoun || { s: "row", p: "rows" };
  const title = !canGroup ? "Rows" : (showRaw ? "All rows" : `By ${noun.s}`);
  const toggle = canGroup && (
    <button type="button" className="table-mode-toggle" onClick={() => setShowRaw(!showRaw)}>
      {showRaw ? `Group by ${noun.s}` : `Show all rows (${fmtInt(data.rows_total)})`}
    </button>
  );
  return (
    <Card title={title} right={toggle}>
      {(!canGroup || showRaw)
        ? <DataTable data={data} plan={plan} loadingMore={loadingMore} onLoadMore={onLoadMore} />
        : (
          <React.Fragment>
            <PartialGroupNote data={data} />
            <GroupedTable summary={entitySummary!} plan={plan} statusMeaningful={statusMeaningful} dayCol={dayCol} />
          </React.Fragment>
        )}
    </Card>
  );
}

// Top N entities by the key metric, bars coloured by worst status -- skipped entirely (both here
// and by the caller) when there is no entity+numeric metric pair worth plotting. A magnitude-only
// ranking (RANKED band, e.g. cost_by_job) draws one uniform colour instead: status is not
// meaningful there, so colouring by it would imply a verdict the check never makes.
function EntityChart({ summary, plan, statusMeaningful, data }: { summary: EntitySummary | null; plan: ColumnPlan; statusMeaningful: boolean; data: FindingData }) {
  if (!summary || !plan.keyMetric) return null;
  const metric = plan.keyMetric;
  const useAvg = metric.kind === "pct";
  const items = summary.list
    .filter((g) => g.hasMetric)
    .map((g): RankedItem => ({
      // A named ref with no name yet stays null so the chart shows its id.
      name: (summary.refKind ? g.name : (g.name || String(g.id))) as string,
      id: summary.refKind && !g.noEntity ? g.id : undefined,
      value: (useAvg ? g.avg : g.sum) as number,
      color: statusMeaningful ? statusColorVar(g.worstStatus) : undefined,
    }));
  // Nothing above zero (e.g. every endpoint had 0 requests): no chart, not an empty one.
  if (!items.some((it) => numOrZero(it.value) > 0)) return null;
  return (
    <Card title={`Top ${Math.min(10, items.length)} by ${metric.label.charAt(0).toLowerCase() + metric.label.slice(1)}`}>
      <PartialGroupNote data={data} />
      <RankedBars
        items={items}
        n={10}
        valueFmt={(v) => fmtCell(v, metric.kind)}
        unitLabel={metric.unit || metric.label}
        ariaLabel={`Top entities by ${metric.label}`}
        emptyText="Nothing to plot."
      />
    </Card>
  );
}

// One short line of links to related checks ("Also check: Disk spills · Shuffle-heavy queries"),
// replacing the old "next: <title> -- <if>" chips -- a related check this page's own findingsIndex
// does not know about is skipped rather than falling back to its raw query_id.
function RelatedChecks({ next, findingsIndex, onJumpTo }: {
  next: NextCheck[] | null | undefined; findingsIndex: Record<string, FindingSummary> | null | undefined; onJumpTo: (queryId: string) => void;
}) {
  const items = (next || [])
    .map((n) => ({ queryId: n.query_id, target: findingsIndex && findingsIndex[n.query_id] }))
    .filter((x): x is { queryId: string; target: FindingSummary } => !!x.target);
  if (!items.length) return null;
  return (
    <div className="related-checks">
      <span className="related-checks-label">Also check:</span>
      {items.map((x, i) => (
        <React.Fragment key={x.queryId}>
          {i > 0 && <span className="related-checks-sep">{"·"}</span>}
          <button type="button" className="related-checks-link" onClick={() => onJumpTo(x.queryId)}>
            {x.target.title}
          </button>
        </React.Fragment>
      ))}
    </div>
  );
}

// P4-T-IDX (section 5.7): one line from scope.tag, only while a tag filter is actually set
// (data.tag is non-null -- app/api/app.py's own "tag": {...} | null echo).
function TagScopeLine({ data }: { data: FindingData }) {
  const tag = data && data.tag;
  const scopeTag = data && data.scope && data.scope.tag;
  if (!tag || !scopeTag) return null;
  const text = scopeTag.applied
    ? `Tag filter applied by: ${scopeTag.chain_label}.`
    : `Tag filter not applied: ${scopeTag.reason}`;
  return <div className="scope-note">{text}</div>;
}

// T-71: the same region-gap fact scope.tsx's RegionGapLine states, worded as one short line with
// the affected workspace names one click away instead of only in a hover title.
function RegionScopeLine({ data }: { data: FindingData }) {
  const [open, setOpen] = React.useState(false);
  if (isOutsideRegion(data)) return null; // that case gets its own NotAssessedCard already
  const gap = data && data.scope && data.scope.region_gap;
  if (!gap || gap.length === 0) return null;
  const n = gap.length;
  return (
    <div className="scope-note">
      {`${fmtInt(n)} workspace${n === 1 ? "" : "s"} ${OUTSIDE_REGION_LABEL} ${n === 1 ? "is" : "are"} not assessed here. `}
      <button type="button" className="g-toggle" onClick={() => setOpen(!open)}>
        {open ? "hide names" : "show names"}
      </button>
      {open && <div className="scope-note-names muted">{regionWorkspaceNames(gap, 20)}</div>}
    </div>
  );
}

const MONEY_KIND_WORD: Record<string, string> = { waste: "Possible waste", saving: "Possible saving", spend: "Spend", change: "Change", gap: "Gap to list" };

// The check's own numbers, one box per kind: never added together.
function FigureBoxes({ summary, windowDays }: { summary: FindingSummary | null | undefined; windowDays: number }) {
  if (!summary) return null;
  const a = summary.affected;
  const m = summary.money;
  if (!a && !m) return null;
  return (
    <div className="d-figures">
      {a && (
        <div className="d-figure">
          <div className="d-figure-label">Flagged</div>
          <div className="d-figure-value mono">{a.flagged && a.flagged < a.total ? `${fmtInt(a.flagged)} of ${fmtInt(a.total)}` : fmtInt(a.total)}</div>
          <div className="d-figure-note">{a.noun}</div>
        </div>
      )}
      {m && (
        <div className="d-figure">
          <div className="d-figure-label">{MONEY_KIND_WORD[m.kind] || m.kind}</div>
          <div className="d-figure-value mono">{fmtMoney(m.usd, 0)}</div>
          <div className="d-figure-note">{`last ${windowDays} days${m.kind === "waste" ? " · counted on Waste & savings" : ""}`}</div>
        </div>
      )}
    </div>
  );
}

function FixBox({ queryId }: { queryId: string }) {
  const t = actionTemplateFor(queryId);
  const [copied, setCopied] = React.useState(false);
  if (!t) return null;
  const copy = () => {
    try { navigator.clipboard.writeText(t.sql || ""); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch (e) { /* no clipboard */ }
  };
  return (
    <div className="d-fix">
      <div className="d-fix-head">
        <span className="d-fix-title">The fix</span>
        <span className="d-fix-meta">{`${t.team} · ${t.effort}`}</span>
      </div>
      <div className="d-fix-how">{t.how}</div>
      {t.sql && (
        <div className="d-fix-sql">
          <pre className="mono">{t.sql}</pre>
          <button type="button" className="copy-link-btn" onClick={copy}>{copied ? "Copied" : "Copy SQL"}</button>
        </div>
      )}
    </div>
  );
}

export function FindingDetail({ queryId, windowDays, workspaceIds, envs, onJumpTo, findingsIndex }: {
  queryId: string; windowDays: number; workspaceIds: string[]; envs: string[]; onJumpTo: (queryId: string) => void;
  findingsIndex: Record<string, FindingSummary> | null | undefined;
}) {
  // T-75B review must-fix 2: subscribes this component to Names' cache -- without it, a name that
  // finishes loading AFTER plan/entitySummary below were first computed (names.tsx's own /api/dims
  // fetch can take many seconds) never triggered a re-render, so the verdict line and chart kept
  // showing raw ids/"no name: not in system tables" forever, even once Names actually had the name.
  useNames();
  const [data, setData] = React.useState<FindingData | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [loadingMore, setLoadingMore] = React.useState(false);

  const load = React.useCallback((offset: number, append: boolean) => {
    const setBusy = offset > 0 ? setLoadingMore : setLoading;
    setBusy(true);
    const limit = offset === 0 ? INITIAL_LIMIT : PAGE_SIZE;
    Api.finding(queryId, windowDays, workspaceIds, envs, limit, offset)
      .then((d) => {
        setLoadError(null);
        setData((prev) => {
          if (append && prev && prev.rows) {
            return { ...d, rows: [...prev.rows, ...d.rows] };
          }
          return d;
        });
      })
      .catch((err) => setLoadError(err.message || String(err)))
      .finally(() => setBusy(false));
    // Api.finding reads the module-level tag filter (Api.setTagFilter), not a prop -- without
    // Api.tagFilterKey() in this dependency list, an open detail kept `load` (and so "Load more")
    // bound to the OLD tag filter after the tag changed, so "Load more" could append rows fetched
    // under the NEW filter onto a first page fetched under the old one.
  }, [queryId, windowDays, JSON.stringify(workspaceIds), JSON.stringify(envs), Api.tagFilterKey()]);

  React.useEffect(() => {
    let live = true;
    setData(null);
    setLoading(true);
    (async () => {
      try {
        const first = await Api.finding(queryId, windowDays, workspaceIds, envs, INITIAL_LIMIT, 0);
        let rows = first.rows || [];
        const want = Math.min(first.rows_total || 0, FULL_FETCH_CAP);
        while (live && first.outcome === "ok_rows" && rows.length < want) {
          const next = await Api.finding(queryId, windowDays, workspaceIds, envs, INITIAL_LIMIT, rows.length);
          if (!next.rows || !next.rows.length) break;
          rows = rows.concat(next.rows);
        }
        if (live) { setLoadError(null); setData({ ...first, rows }); }
      } catch (err) {
        if (live) setLoadError((err as Error).message || String(err));
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryId, windowDays, JSON.stringify(workspaceIds), JSON.stringify(envs), Api.tagFilterKey()]);

  const okData = data && data.outcome === "ok_rows" ? data : null;
  const isOkRows = !!okData;
  const rawCols = okData ? (okData.columns || []) : [];
  const rows = okData ? (okData.rows || []) : [];
  const dayCol = React.useMemo(() => findDayColumn(rawCols), [rawCols]);
  // Names.isReady() as a dep: a query_id/id column resolved via resolveName inside planColumns
  // (the workspace_name duplicate check) or buildEntitySummary below must recompute once names
  // finish loading, not only when the rows/plan themselves change (must-fix 2).
  const plan = React.useMemo(
    () => planColumns(rawCols, rows, data ? data.discount_pct : undefined, data && data.order_by),
    [rawCols, rows, data && data.discount_pct, data && data.order_by, Names.isReady()]
  );
  const hasStatus = rawCols.some((c) => c.name === "status");
  const statusCounts = React.useMemo((): Record<string, number> => (hasStatus ? tallyStatusCounts(rows) : {}), [rows, hasStatus]);
  const band = data
    ? bandOf({ outcome: data.outcome, status_counts: (isOkRows && hasStatus) ? statusCounts : null, query_id: data.query_id })
    : null;
  const statusMeaningful = hasStatus && band !== "RANKED";
  const entitySummary = React.useMemo(
    () => (isOkRows ? buildEntitySummary(rows, plan, hasStatus, dayCol) : null),
    [rows, plan, hasStatus, isOkRows, dayCol, Names.isReady()]
  );
  const canGroup = !!(entitySummary && entitySummary.list.length > 0);
  // T-75B review must-fix 6: a check with no status column at all, or whose every entity has only
  // one row (a plain reference list, e.g. sql_warehouse_config_current), has nothing for grouping
  // to add -- "Rows = 1" on every line and a chart whose every bar is the same height. Those checks
  // keep the plain raw table and no chart, exactly as if they had no entity at all.
  const groupingMeaningful = canGroup && hasStatus && entitySummary!.list.some((g) => g.count > 1);
  const topGroup = canGroup ? sortEntityGroups(entitySummary!.list, plan.keyMetric, statusMeaningful)[0] : null;

  if (loading && !data) return <div className="loading-note">Loading this check...</div>;
  if (loadError && !data) return <div className="honest-card error"><div className="h-title">Could not load this finding</div><div className="h-note mono">{loadError}</div></div>;
  if (!data) return null;

  const header = data.header;
  const worstRow = isOkRows && rows.length > 0 ? rows[0] : null;
  const verdictText = isOkRows ? buildVerdictText(data, plan, band, hasStatus, statusCounts, worstRow, topGroup, entitySummary) : null;
  const fixText = isOkRows && (band === "CRITICAL" || band === "WARN") ? actionLine(header.actions, data.query_id) : null;

  return (
    <div className="detail-wrap">
      <div className="d-head">
        <div>
          <div className="d-title">{header.title}</div>
          <div className="d-meta">
            {band && <StatusPill kind={bandPillKind(band)} />}
            <Badge kind="info">{data.windowed ? `${data.window_days} d window` : "snapshot"}</Badge>
          </div>
        </div>
        {/* P4-44: this row is already open by the time this renders -- FindingsTable syncs its own
            expanded/drilled-into query_id to location.hash (the "focus" key) the moment it opens,
            so the current address already reopens straight into this same finding. */}
        <CopyLinkButton label="Copy link to this finding" />
        {isOkRows && data.rows && data.rows.length > 0 && (
          <button type="button" className="copy-link-btn" onClick={() => downloadFindingRows(data)}>
            {data.rows_total > data.rows.length ? `Download rows (CSV, ${fmtInt(data.rows.length)} of ${fmtInt(data.rows_total)})` : "Download rows (CSV)"}
          </button>
        )}
        {typeof actionKeyFor === "function" && actionKeyFor(data.query_id) && (
          <a className="copy-link-btn" href={navHref({ tab: "actions", subtab: null, focus: null })}>Open in Actions →</a>
        )}
      </div>

      <LibraryIssueBadge corrections={data.library_corrections} />

      {isOkRows && (
        <React.Fragment>
          <div className="verdict-line">{verdictText}</div>
          <FloorLine floor={data.floor} />
          {(band === "CRITICAL" || band === "WARN") && <FigureBoxes summary={findingsIndex && findingsIndex[queryId]} windowDays={data.window_days || windowDays} />}
          {fixText && !actionTemplateFor(queryId) && <div className="fix-line"><b>Fix now: </b>{fixText}</div>}
          {(band === "CRITICAL" || band === "WARN") && <FixBox queryId={queryId} />}
          {groupingMeaningful && (
            <EntityChart summary={entitySummary} plan={plan} statusMeaningful={statusMeaningful} data={data} />
          )}
          <FindingTableSection
            data={data}
            plan={plan}
            canGroup={groupingMeaningful}
            entitySummary={entitySummary}
            statusMeaningful={statusMeaningful}
            dayCol={dayCol}
            loadingMore={loadingMore}
            onLoadMore={() => load(data.offset + data.rows.length, true)}
          />
        </React.Fragment>
      )}

      {/* T-75B review should-fix "empty state is out of order": the outcome card for a non-ok_rows
          result IS this finding's own content -- it used to render below Details/Also check,
          which is backwards for a page whose whole point is summary-before-detail. */}
      {data.outcome === "not_assessed" && <NotAssessedCard data={data} />}
      {data.outcome === "error" && <ErrorCard data={data} />}
      {data.outcome === "ok_empty_window" && <EmptyWindowCard data={data} />}
      {data.outcome === "ok_empty_filters" && <EmptyFiltersCard data={data} />}

      <Guidance
        header={header}
        queryId={queryId}
        hasStatus={hasStatus}
        stars={header.stars}
        confidenceNote={header.confidence === "needs_confirmation" ? header.confidence_note : null}
        scopeBadge={data.scope && data.scope.has_workspace_id === false ? <FindingScopeBadge scope={data.scope} /> : null}
      />
      <RelatedChecks next={header.next} findingsIndex={findingsIndex} onJumpTo={onJumpTo} />

      <div style={{ marginTop: 14 }}>
        <RegionScopeLine data={data} />
        <TagScopeLine data={data} />
        {data.outcome !== "not_assessed" && data.outcome !== "error" && (
          <DataQualityNote data={data} />
        )}
      </div>
    </div>
  );
}
