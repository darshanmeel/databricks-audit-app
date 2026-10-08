// data-fetching hooks and aggregation helpers shared by every
// tab (tasks/T-58-web-tabs-and-charts.md). Nothing here renders markup; it only fetches rows
// through Api.finding (the same endpoint the findings table itself uses) and reduces them into
// the small {name, value} shapes charts.tsx draws. Keeping this in one place means every tab's
// chart is backed by the SAME honesty-rule outcome the finding's own detail panel would show --
// a chart is only ever computed from an "ok_rows" fetch; every other outcome is threaded through
// unchanged for ChartNote to render.

import React from "react";

import { Api, type AggOptions } from "../api";
import type {
  AggGroup, AggKey, AggState, AggregateData, DimMaps, Entry, ExcludedNoWorkspace, FindingState, Meta, Row, Scope,
} from "../types";
import { fmtInt, fmtMoney, fmtPct } from "../format";
import { resolveName } from "./names";
import { CHART_OTHER_COLOR } from "./charts";

// ---------------------------------------------------------------------------------------------
// IgnoredFilterTracker: the checks on the current page that an active workspace or tag filter
// did not narrow, so the page can say so once.
// ---------------------------------------------------------------------------------------------
/** Per check: which active filters it did not narrow. */
export interface IgnoredFilters {
  workspace: boolean;
  tag: boolean;
  wsTag: boolean;
}

export const IgnoredFilterTracker = (function () {
  let state: Record<string, IgnoredFilters> = {};
  const listeners = new Set<() => void>();
  function notify() { listeners.forEach((fn) => fn()); }
  function reset() {
    if (Object.keys(state).length === 0) return;
    state = {};
    notify();
  }
  function report(queryId: string, workspaceIgnored: boolean, tagIgnored: boolean, tagWorkspaceOnly: boolean) {
    if (!queryId) return;
    const prev = state[queryId];
    if (!workspaceIgnored && !tagIgnored && !tagWorkspaceOnly) {
      if (!prev) return;
      const next = { ...state };
      delete next[queryId];
      state = next;
      notify();
      return;
    }
    if (prev && prev.workspace === !!workspaceIgnored && prev.tag === !!tagIgnored && prev.wsTag === !!tagWorkspaceOnly) return;
    state = { ...state, [queryId]: { workspace: !!workspaceIgnored, tag: !!tagIgnored, wsTag: !!tagWorkspaceOnly } };
    notify();
  }
  function subscribe(fn: () => void) { listeners.add(fn); return () => { listeners.delete(fn); }; }
  function getSnapshot() { return state; }
  return { reset, report, subscribe, getSnapshot };
})();

// Only a filter that was actually set can be ignored.
function reportIgnoredFilters(queryId: string, workspaceActive: boolean, tagActive: boolean, scope: Scope | null | undefined) {
  if (!scope) return;
  const workspaceIgnored = !!workspaceActive && scope.has_workspace_id === false;
  const tagIgnored = !!tagActive && !!scope.tag && scope.tag.applied === false;
  const chain = (scope.tag && scope.tag.chain) || [];
  const tagWorkspaceOnly = !!tagActive && !tagIgnored && chain.length === 1 && chain[0] === "workspace";
  IgnoredFilterTracker.report(queryId, workspaceIgnored, tagIgnored, tagWorkspaceOnly);
}

// The ignored checks, split by filter, for FilterReachNote.
export function useIgnoredFilterSummary() {
  const state = React.useSyncExternalStore(IgnoredFilterTracker.subscribe, IgnoredFilterTracker.getSnapshot, IgnoredFilterTracker.getSnapshot);
  return React.useMemo(() => {
    const workspaceIds: string[] = [], tagIds: string[] = [], wsTagIds: string[] = [];
    Object.keys(state).forEach((id) => {
      if (state[id].workspace) workspaceIds.push(id);
      if (state[id].tag) tagIds.push(id);
      if (state[id].wsTag) wsTagIds.push(id);
    });
    return { workspaceIds, tagIds, wsTagIds };
  }, [state]);
}

// ---------------------------------------------------------------------------------------------
// useFindingData -- one query_id's full row set (up to `limit`) for aggregation, independent of
// the paginated 100-row view FindingDetail renders inside an expanded table row.
//
// `statuses` (T-68, optional, e.g. ["CRITICAL", "WARN"]) scopes the fetch to those status values
// SERVER-side, on a finding that carries a status column -- a caller that only needs flagged rows
// (a tile summing a column across CRITICAL/WARN rows) gets only those, up to the SAME `limit`, so
// the cap applies to the flagged subset rather than to the whole unfiltered table. A caller that
// wants every row regardless of status still passes nothing (the existing default).
// ---------------------------------------------------------------------------------------------
const LOADING = { phase: "loading", outcome: null, data: null, error: null } as const;

export function useFindingData(queryId: string | null, windowDays: number, workspaceIds: string[] | null,
  envs: string[] | null, limit?: number, statuses?: string[] | null, columns?: string[] | null): FindingState {
  const lim = limit || 5000;
  const [state, setState] = React.useState<FindingState>(LOADING);
  // P4-T-IDX: Api.tagFilterKey() joins the memo key -- the stored tag filter is not one of this
  // hook's own arguments (Api.finding reads it internally), so without this the fetch would not
  // re-run when only the tag filter changed.
  const key = queryId + "|" + windowDays + "|" + JSON.stringify(workspaceIds) + "|" + JSON.stringify(envs) + "|" + lim + "|" + JSON.stringify(statuses || null) + "|" + JSON.stringify(columns || null) + "|" + Api.tagFilterKey();
  React.useEffect(() => {
    let cancelled = false;
    setState(LOADING);
    if (!queryId) return undefined;
    const workspaceActive = !!((workspaceIds && workspaceIds.length > 0) || (envs && envs.length > 0));
    const tagActive = Api.tagFilterKey() !== "";
    Api.finding(queryId, windowDays, workspaceIds, envs, lim, 0, undefined, statuses, columns)
      .then((d) => {
        if (cancelled) return;
        setState({ phase: "ready", outcome: d.outcome, data: d, error: null });
        reportIgnoredFilters(queryId, workspaceActive, tagActive, d.scope);
      })
      .catch((e) => { if (!cancelled) setState({ phase: "error", outcome: null, data: null, error: e.message || String(e) }); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

// ---------------------------------------------------------------------------------------------
// useFindingAgg -- T-68: the server-side aggregate twin of useFindingData. `group` is an array of
// 0-2 real column names; `agg` is "sum" | "count" | "count_distinct"; `value` is the column to
// sum/count-distinct (ignored for "count"). `opts` is {statuses, top} (both optional; `top` folds
// the tail past that many groups into one "other" bucket, keeping the true total exact). Returns
// the same {phase, outcome, data, error} shape as useFindingData -- `data` carries
// {groups, other, total_value, rows_total, matched_rows, discount_pct, window_coverage, ...}
// instead of `rows` on an ok_rows outcome. See overview_tile.tsx's aggregateGroupTileProps for the
// shared four-outcome handling every caller should route through, same discipline as
// aggregateTileProps for a row-shaped useFindingData state.
// ---------------------------------------------------------------------------------------------
export function useFindingAgg(queryId: string | null, windowDays: number, workspaceIds: string[] | null,
  envs: string[] | null, group: string[] | null, agg: string, value?: string | null, opts?: AggOptions): AggState {
  const o = opts || {};
  const grp = group || [];
  const [state, setState] = React.useState<AggState>(LOADING);
  const key = [
    queryId, windowDays, JSON.stringify(workspaceIds), JSON.stringify(envs),
    grp.join(","), agg, value || "", JSON.stringify(o.statuses || null), o.top || "", JSON.stringify(o.extraTags || null),
    Api.tagFilterKey(),
  ].join("|");
  React.useEffect(() => {
    let cancelled = false;
    setState(LOADING);
    if (!queryId) return undefined;
    const workspaceActive = !!((workspaceIds && workspaceIds.length > 0) || (envs && envs.length > 0));
    const tagActive = Api.tagFilterKey() !== "";
    Api.aggregate(queryId, windowDays, workspaceIds, envs, grp, agg, value, o)
      .then((d) => {
        if (cancelled) return;
        setState({ phase: "ready", outcome: d.outcome, data: d, error: null });
        reportIgnoredFilters(queryId, workspaceActive, tagActive, d.scope);
      })
      .catch((e) => { if (!cancelled) setState({ phase: "error", outcome: null, data: null, error: e.message || String(e) }); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

// Turn an aggregate response's `groups` (plus its `other` bucket, when present) into the plain
// [{name, value}] shape charts.tsx's primitives (HBarList/Treemap/Donut/groupSum's own output)
// already expect. `keyFmt(keyArray)` names a group ("(unlabeled)" fallbacks belong to the caller,
// same as groupSum's own keyFn); `otherLabel` names the folded tail, defaulting to "Other".
export function groupsToEntries(data: AggregateData | null, keyFmt: (key: AggKey[]) => string, otherLabel?: string): Entry[] {
  if (!data) return [];
  const entries: Entry[] = (data.groups || []).map((g) => ({ name: keyFmt(g.key), value: numOrZero(g.value) }));
  if (data.other) entries.push({ name: otherLabel || "Other", value: numOrZero(data.other.value), color: CHART_OTHER_COLOR });
  return entries;
}

// Collapse a 2-group-column aggregate response onto ONE of its key columns, re-summing across the
// other -- one server round trip covers two chart breakdowns that share a source table (e.g.
// cost_usage_policy_coverage grouped by [policy_coverage, tag_coverage] in one call, then this
// reduces it to a policy-only cut and, separately, a tag-only cut). `idx` is which key position to
// keep (0 or 1); `fallback` names a null/missing key value (defaults "unknown").
export function regroupByKeyIndex(data: { groups: AggGroup[]; other?: AggregateData["other"] } | null, idx: number, fallback?: string): { name: AggKey; value: number }[] {
  const order: AggKey[] = [];
  const totals = new Map<AggKey, number>();
  ((data && data.groups) || []).forEach((g) => {
    const raw = g.key && g.key[idx];
    const k = raw != null ? raw : (fallback || "unknown");
    if (!totals.has(k)) { totals.set(k, 0); order.push(k); }
    totals.set(k, totals.get(k)! + numOrZero(g.value));
  });
  if (data && data.other) {
    // The folded tail (past `top`) has no per-group key -- it can only be attributed to `fallback`,
    // never split across real key values. Callers that need an exact per-key cut should omit `top`
    // on a 2-column group-by (cardinality here is bounded by the dimension, not the row grain, so
    // omitting it is normally safe -- see the callers of this helper).
    const k = fallback || "unknown";
    if (!totals.has(k)) { totals.set(k, 0); order.push(k); }
    totals.set(k, totals.get(k)! + numOrZero(data.other.value));
  }
  return order.map((k) => ({ name: k, value: totals.get(k)! }));
}

// data.rows_total / data.returned (a plain finding fetch) says a raw-row view is showing fewer
// rows than exist -- the "make every remaining capped view say it is capped" half of T-68, for any
// chart/tile that still reads `.rows` directly instead of going through useFindingAgg. null when
// nothing was cut (the common case, once useFindingAgg has replaced the views that can realistically
// exceed the cap).
export function cappedRowsNote(data: { rows_total: number; returned: number } | null | undefined, noun?: string): string | null {
  if (!data || !(data.rows_total > data.returned)) return null;
  return `showing ${fmtInt(data.returned)} of ${fmtInt(data.rows_total)} ${noun || "rows"}`;
}

// A spike day is judged on the day's total (the bar on the chart): at least SPIKE_PCT above the
// median of the 7 days before it. The median keeps low weekends from turning every Monday into a
// spike. `days` is [{day, value}] in date order; days with fewer than 5 earlier days are not judged.
export const SPIKE_PCT = 15;
export interface SpikeDay {
  day: string;
  value: number;
  median: number;
  pct: number;
}

export function totalSpikeDays(days: { day: string; value: number }[] | null): SpikeDay[] {
  return (days || []).map((d, i, all): SpikeDay | null => {
    const prev = all.slice(Math.max(0, i - 7), i).map((x) => x.value).sort((a, b) => a - b);
    if (prev.length < 5) return null;
    const n = prev.length;
    const median = n % 2 ? prev[(n - 1) / 2] : (prev[n / 2 - 1] + prev[n / 2]) / 2;
    if (!(median > 0) || d.value < median * (1 + SPIKE_PCT / 100)) return null;
    return { day: d.day, value: d.value, median, pct: (d.value / median - 1) * 100 };
  }).filter((d): d is SpikeDay => d !== null);
}

// ---------------------------------------------------------------------------------------------
// useMultiFindingData -- several query_ids at once (a tab's whole chart set), one state object
// keyed by query_id so each chart can independently render its own ChartNote.
// ---------------------------------------------------------------------------------------------
// Waste totals and lists read only flagged rows; fetching every row cost megabytes per check.
export const FLAGGED_STATUSES = ["CRITICAL", "WARN"];

/** Several checks' states at once, by query_id. */
export interface MultiFindingState {
  phase: "loading" | "ready";
  byId: Record<string, FindingState>;
}

export function useMultiFindingData(queryIds: string[] | null, windowDays: number, workspaceIds: string[] | null,
  envs: string[] | null, limit?: number | null, statuses?: string[] | null): MultiFindingState {
  const lim = limit || 5000;
  const ids = queryIds || [];
  const key = ids.join(",") + "|" + windowDays + "|" + JSON.stringify(workspaceIds) + "|" + JSON.stringify(envs) + "|" + lim + "|" + JSON.stringify(statuses || null) + "|" + Api.tagFilterKey();
  const [state, setState] = React.useState<MultiFindingState>({ phase: "loading", byId: {} });
  React.useEffect(() => {
    let cancelled = false;
    setState({ phase: "loading", byId: {} });
    if (!ids.length) { setState({ phase: "ready", byId: {} }); return undefined; }
    Promise.all(ids.map((id) =>
      Api.finding(id, windowDays, workspaceIds, envs, lim, 0, undefined, statuses)
        .then((d): [string, FindingState] => [id, { phase: "ready", outcome: d.outcome, data: d, error: null }])
        .catch((e): [string, FindingState] => [id, { phase: "error", outcome: null, data: null, error: e.message || String(e) }])
    )).then((pairs) => {
      if (cancelled) return;
      const byId: Record<string, FindingState> = {};
      pairs.forEach(([id, v]) => { byId[id] = v; });
      setState({ phase: "ready", byId });
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

// ---------------------------------------------------------------------------------------------
// useDims -- dims.dim_job/cluster/warehouse/pipeline, fetched once and turned into lookup maps
// ("Names, not ids" -- T-58's own rule). Falls back to the raw id when a dim row is missing (a
// deleted job, a dim not yet built) -- never a blank label.
// ---------------------------------------------------------------------------------------------
export function useDims(): DimMaps | null {
  const [maps, setMaps] = React.useState<DimMaps | null>(null);
  React.useEffect(() => {
    let cancelled = false;
    Api.dims().then((d) => {
      if (cancelled) return;
      const jobs: DimMaps["jobs"] = new Map();
      (d.jobs || []).forEach((j) => jobs.set(j.workspace_id + ":" + j.job_id, j));
      const clusters: DimMaps["clusters"] = new Map();
      (d.clusters || []).forEach((c) => clusters.set(c.cluster_id, c));
      const warehouses: DimMaps["warehouses"] = new Map();
      (d.warehouses || []).forEach((w) => warehouses.set(w.warehouse_id, w));
      const pipelines: DimMaps["pipelines"] = new Map();
      (d.pipelines || []).forEach((p) => pipelines.set(p.workspace_id + ":" + p.pipeline_id, p));
      setMaps({ jobs, clusters, warehouses, pipelines });
    }).catch(() => { if (!cancelled) setMaps({ jobs: new Map(), clusters: new Map(), warehouses: new Map(), pipelines: new Map() }); });
    return () => { cancelled = true; };
  }, []);
  return maps;
}

export function jobName(maps: DimMaps | null, workspaceId: unknown, jobId: unknown): string {
  if (!maps) return String(jobId);
  const j = maps.jobs.get(String(workspaceId) + ":" + String(jobId));
  return (j && j.name) || `job ${jobId}`;
}
export function clusterName(maps: DimMaps | null, clusterId: string): string {
  if (!maps) return String(clusterId);
  const c = maps.clusters.get(clusterId);
  return (c && c.cluster_name) || `cluster ${clusterId}`;
}
export function warehouseName(maps: DimMaps | null, warehouseId: string): string {
  if (!maps) return String(warehouseId);
  // The shared names lookup adds the workspace when the name repeats across warehouses.
  const shared = typeof resolveName === "function" ? resolveName("warehouse", null, warehouseId) : null;
  if (shared) return shared;
  const w = maps.warehouses.get(warehouseId);
  return (w && w.warehouse_name) || `warehouse ${warehouseId}`;
}
export function pipelineName(maps: DimMaps | null, workspaceId: unknown, pipelineId: unknown): string {
  if (!maps) return String(pipelineId);
  const p = maps.pipelines.get(String(workspaceId) + ":" + String(pipelineId));
  return (p && p.pipeline_name) || `pipeline ${pipelineId}`;
}

// ---------------------------------------------------------------------------------------------
// Aggregation helpers -- plain reductions over the JSON rows an ok_rows finding returns.
// ---------------------------------------------------------------------------------------------

export function numOrZero(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export function sumBy(rows: Row[] | null | undefined, valKey: string): number {
  return (rows || []).reduce((s, r) => s + numOrZero(r[valKey]), 0);
}

// Group rows by keyFn(row) and sum valKey within each group -> [{name, value}], insertion order.
export function groupSum(rows: Row[] | null | undefined, keyFn: (r: Row) => any, valKey: string): Entry[] {
  const order: any[] = [];
  const totals = new Map<any, number>();
  (rows || []).forEach((r) => {
    const k = keyFn(r);
    if (!totals.has(k)) { totals.set(k, 0); order.push(k); }
    totals.set(k, totals.get(k)! + numOrZero(r[valKey]));
  });
  return order.map((k) => ({ name: k, value: totals.get(k)! }));
}

// Top N entries by value desc, the rest folded into one "Other" bucket (omitted if nothing
// remains). Mirrors config.ui.max_chart_categories -- callers pass meta.max_chart_categories.
export function topNWithOther(entries: Entry[] | null | undefined, n: number, otherLabel?: string): Entry[] {
  const sorted = [...(entries || [])].sort((a, b) => b.value - a.value);
  if (sorted.length <= n) return sorted;
  const head = sorted.slice(0, n);
  const restTotal = sorted.slice(n).reduce((s, e) => s + e.value, 0);
  if (restTotal > 0) head.push({ name: otherLabel || "Other", value: restTotal, color: CHART_OTHER_COLOR });
  return head;
}

// Count rows by keyFn(row) -> [{name, value}], insertion order (the categorical-distribution
// twin of groupSum, for columns with no numeric magnitude to sum -- e.g. a status/reason code).
export function countBy(rows: Row[] | null | undefined, keyFn: (r: Row) => any): Entry[] {
  const order: any[] = [];
  const counts = new Map<any, number>();
  (rows || []).forEach((r) => {
    const k = keyFn(r);
    if (!counts.has(k)) { counts.set(k, 0); order.push(k); }
    counts.set(k, counts.get(k)! + 1);
  });
  return order.map((k) => ({ name: k, value: counts.get(k)! }));
}

// Group rows by day (a date/timestamp column already ISO-formatted by the API) and sum valKey,
// sorted ascending by day -- the shape MultiLine/Sparkline want for a trend.
/** Days and their values, in date order. */
export interface DaySeries {
  days: string[];
  values: number[];
}

export function filledDailySeries(series: DaySeries | null, meta: Meta | null, windowDays: number): DaySeries | null {
  if (!series) return null;
  const filled = fillDays(series.days.map((d, i) => ({ day: d, value: series.values[i] })),
    meta && meta.as_of_date, windowDays, meta && meta.snapshot_days);
  return { days: filled.map((d) => d.day), values: filled.map((d) => d.value) };
}

export function dailySeries(rows: Row[] | null | undefined, dayKey: string, valKey: string): DaySeries {
  const totals = new Map<string, number>();
  (rows || []).forEach((r) => {
    const d = r[dayKey];
    if (!d) return;
    const day = String(d).slice(0, 10);
    totals.set(day, (totals.get(day) || 0) + numOrZero(r[valKey]));
  });
  const days = [...totals.keys()].sort();
  return { days, values: days.map((d) => totals.get(d)!) };
}

// Pull the curated rows for a tab out of the already-loaded full findings list, in the order
// `ids` names them (falls back to whatever order `findings` is in for any id not found --
// should not happen for a curated id, but never crashes if the registry and a stale ids list
// disagree).
function filterByIds<T extends { query_id: string }>(findings: T[] | null, ids: string[]): T[] {
  const idx: Record<string, T> = {};
  (findings || []).forEach((f) => { idx[f.query_id] = f; });
  return (ids || []).map((id) => idx[id]).filter(Boolean);
}

// fillDays -- one {day, value} per calendar day (UTC) from as_of minus min(windowDays,
// snapshotDays) through as_of minus 1, 0 where `days` had no row for that day. A daily average or
// a spike scan built straight off a sparse aggregate silently skips zero-spend days, which both
// inflates the average and misaligns "the 7 days before" a spike compares against.
export function fillDays(days: { day: string; value: unknown }[] | null, asOfDate: string | null | undefined,
  windowDays: number, snapshotDays?: number | null): { day: string; value: number }[] {
  const byDay = new Map((days || []).map((d) => [d.day, numOrZero(d.value)]));
  if (!asOfDate) {
    return [...byDay.keys()].sort().map((day) => ({ day, value: byDay.get(day)! }));
  }
  const span = Math.max(0, Math.min(windowDays, snapshotDays != null ? snapshotDays : windowDays));
  const asOf = new Date(`${String(asOfDate).slice(0, 10)}T00:00:00Z`);
  const out: { day: string; value: number }[] = [];
  for (let i = span; i >= 1; i -= 1) {
    const d = new Date(asOf.getTime());
    d.setUTCDate(d.getUTCDate() - i);
    const day = d.toISOString().slice(0, 10);
    out.push({ day, value: byDay.get(day) || 0 });
  }
  return out;
}

// excludedNoWorkspaceNote -- one line when a workspace/env/attribute filter is silently leaving
// out account-level spend (no workspace_id at all) that an aggregate response's
// excluded_no_workspace reports; null when no such filter applies (the server leaves the field
// null itself) or the excluded amount rounds to nothing.
export function excludedNoWorkspaceNote(data: { excluded_no_workspace: ExcludedNoWorkspace | null; discount_pct: number } | null): string | null {
  const exc = data && data.excluded_no_workspace;
  if (!exc) return null;
  const usd = numOrZero(exc.value) * (1 - numOrZero(data.discount_pct));
  if (usd < 1) return null;
  return `${fmtMoney(usd, 0)} account-level spend (no workspace) is not in this filter.`;
}

// belowFloorNoteText -- "Not counted" for the $ a per-check materiality floor left OUT of
// wasteTotal's own priced total (never folded into it), shared by Waste and Overview's waste tile
// so the two never word the same figure differently.
/** The $ a materiality floor left out of a waste total, and the floor itself. */
export interface BelowFloor {
  belowFloorUsd: number;
  belowFloorRows: number;
  floor: { min: number; unit: string; per_days?: number } | null;
}

export function belowFloorNoteText(total: BelowFloor | null | undefined, windowDays: number): string | null {
  if (!total || !(total.belowFloorUsd >= 1)) return null;
  const f = total.floor;
  // The floor scales with the window, so a 7-day view says the 7-day amount it actually used.
  const scaled = f && f.unit === "USD" && windowDays && f.per_days && windowDays !== f.per_days;
  const floorText = !f ? "each check's floor"
    : scaled ? `each check's floor (${fmtMoney((f.min * windowDays) / f.per_days!, 0)} per ${fmtInt(windowDays)} days, scaled from ${fmtMoney(f.min, 0)} per ${fmtInt(f.per_days)} days)`
    : `each check's floor (${fmtMoney(f.min, 0)} per ${fmtInt(f.per_days)} days)`;
  return `Not counted: ${fmtMoney(total.belowFloorUsd, 0)} in ${fmtInt(total.belowFloorRows)} row${total.belowFloorRows === 1 ? "" : "s"} under ${floorText}.`;
}

// fmtChangePct -- the one wording for a current-vs-previous percent this app uses everywhere:
// "new" when there is no previous total to compare against (pct is null), a multiple once it has
// doubled ("6.2×", never "+518%"), else a signed, rounded percent.
export function fmtChangePct(pct: number | null | undefined): string {
  if (pct == null) return "new";
  const p = Number(pct);
  if (p >= 9900) return ">100×";
  if (p >= 100) return `${(1 + p / 100).toFixed(1)}×`;
  return `${p >= 0 ? "+" : "−"}${fmtPct(Math.abs(p), 0)}`;
}

const MONTH_BAR_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// One calendar month after an ISO month_start key (always day 01), for filling gaps.
function addMonthKey(key: string): string {
  const [y, m] = key.split("-").map(Number);
  const total = y * 12 + (m - 1) + 1;
  return `${Math.floor(total / 12)}-${String((total % 12) + 1).padStart(2, "0")}-01`;
}

// buildMonthBars -- the ONE month-bar colouring rule Money and Cost both use (they used to each
// slice to a different length BEFORE colouring, so the same month came out red on one page and
// "steady" on the other). Colours every month against its own trailing 3 FULL months from the
// WHOLE history handed in, then slices to `displayCount` only at the end -- so a caller may show
// 12 months or 13 without changing what colour any shared month gets. `tone` is "crit"/"warn"/
// "calm"/"steady" once 3 full priors exist, else null (not enough history -- a caller must render
// this as its own neutral colour, never reuse "steady"'s).
/** One calendar month's bar, coloured against its trailing 3 full months. */
export interface MonthBar {
  month: string;
  label: string;
  shortLabel: string;
  total: number;
  partial: boolean;
  changePct: number | null;
  tone: "crit" | "warn" | "calm" | "steady" | null;
}

export function buildMonthBars(groups: AggGroup[] | null, discountPct: number | null | undefined, displayCount?: number): MonthBar[] {
  const disc = numOrZero(discountPct);
  const sparse = (groups || [])
    .map((g) => ({
      key: g.key && g.key[0] ? String(g.key[0]).slice(0, 10) : null,
      partial: !!(g.key && g.key[1] === true),
      total: numOrZero(g.value) * (1 - disc),
    }))
    .filter((m): m is { key: string; partial: boolean; total: number } => !!m.key)
    .sort((a, b) => a.key.localeCompare(b.key));
  // A month with no billing row at all is a real $0, not a gap to skip -- otherwise the trailing-3-
  // month average and the bar count both silently drop straight past it.
  const all: { key: string; partial: boolean; total: number }[] = [];
  sparse.forEach((m, i) => {
    all.push(m);
    if (i < sparse.length - 1) {
      let next = addMonthKey(m.key);
      while (next < sparse[i + 1].key) {
        all.push({ key: next, partial: false, total: 0 });
        next = addMonthKey(next);
      }
    }
  });
  const withTone = all.map((m, i, arr): MonthBar => {
    let tone: MonthBar["tone"] = null, changePct: number | null = null;
    if (!m.partial) {
      const prior3 = arr.slice(Math.max(0, i - 3), i).filter((p) => !p.partial);
      if (prior3.length === 3) {
        const avg3 = prior3.reduce((s, p) => s + p.total, 0) / 3;
        if (avg3 > 0) {
          changePct = ((m.total - avg3) / avg3) * 100;
          if (changePct >= 30) tone = "crit";
          else if (changePct >= 15) tone = "warn";
          else if (changePct <= -15) tone = "calm";
          else tone = "steady";
        } else if (m.total > 0) {
          tone = "crit"; // real spend where the trailing 3 months were flat $0
        } else {
          tone = "steady";
        }
      }
    }
    const d = new Date(`${m.key}T00:00:00Z`);
    const validDate = !Number.isNaN(d.getTime());
    const shortLabel = validDate ? MONTH_BAR_NAMES[d.getUTCMonth()] : m.key;
    const label = validDate ? `${MONTH_BAR_NAMES[d.getUTCMonth()]} ${d.getUTCFullYear()}` : m.key;
    return { month: m.key, label, shortLabel, total: m.total, partial: m.partial, changePct, tone };
  });
  return displayCount ? withTone.slice(-displayCount) : withTone;
}

export function rowsOf(state: FindingState): Row[] | null {
  return state.phase === "ready" && state.outcome === "ok_rows" ? state.data.rows : null;
}
