// P4-01-UI (tasks/P4-WASTE-SPEC.md section 9.3 item 1): the
// ONE place "how much is being wasted" is computed, so Overview's Waste tile, the Waste tab's own
// strip/charts and P4-44's printed summary can never silently disagree about the total.
//
// DEC-68 / DEC-50 / DEC-57 rule 4: `est_wasted_usd_list` is the only column whose basis is WASTE --
// verified against the generated SQL for every id in WASTE_ITEMS (e.g. compute_warehouse_idle_gaps.
// est_usd_list is the warehouse's WHOLE billed cost across every event, not just the idle stretch
// that earned it a CRITICAL/WARN status -- which is exactly why that id left WASTE_ITEMS). Every
// other dollar/DBU column on a WASTE_ITEMS entry is a magnitude shown for scale (a resource's own
// spend, or a waste MEASURE with no price yet), never counted into the $ total. A check that
// carries neither ranks by severity alone and reads "no cost estimate".
import { numOrZero, useFindingAgg } from "./hooks";
import { WASTE_ITEMS, bandOf } from "./tab_registry";
import { firstAction } from "./overview_tile";
import { findingReason } from "../tabs/tab_overview";
import type { AggState, Filters, FindingState, FindingSummary, Floor, LibraryCorrection, Row } from "../types";
import type { Band, WasteItem } from "./tab_registry";
import type { MultiFindingState } from "./hooks";
import type { TileAction } from "./overview_tile";

/** One possible-waste check on the fix-first list: its verdict and, when flagged, its figures. */
export interface WasteEntry {
  id: string;
  area: string;
  dollarCol: string | null;
  title: string;
  band: Band;
  isFlagged: boolean;
  dollarUsd: number | null;
  dbuVal: number | null;
  billedUsd: number | null;
  spendWords: string;
  flaggedCount: number;
  discountPct: number;
  truncated: boolean;
  returnedRows: number | null;
  rowsTotal: number | null;
  reason: string | null;
  topRow: Row | null;
  corrections: LibraryCorrection[];
  action: TileAction | null;
  rowCount: number;
}

/** The priced waste total, the flagged checks with no price, and what a floor left out. */
export interface WasteTotal {
  usd: number;
  pricedCount: number;
  flaggedCount: number;
  unpricedFlagged: WasteEntry[];
  overlapNote: string | null;
  belowFloorUsd: number;
  belowFloorRows: number;
  floor: Floor | null;
}

function isWasteDollarColumn(col: string | null): col is string {
  return col === "est_wasted_usd_list";
}

// One row-per-check identity, matched to the query's own grain (config/grains/*.yml) -- so summing
// a $/DBU column across a check's flagged rows counts each real-world resource once even if a
// capped or re-fetched page ever handed back the same key twice (tasks/P4-WASTE-SPEC.md 9.2's "row
// key (count once)" column). Only the five ids that carry a genuine magnitude column need one.
export function wasteRowKey(itemId: string, row: Row): string {
  switch (itemId) {
    case "compute_warehouse_idle_minutes":
    case "cost_failed_statement_waste":
      return `${row.workspace_id}:${row.warehouse_id}`;
    case "lakeflow_failed_jobs_wasted_dbus":
      return `${row.workspace_id}:${row.job_id}`;
    case "compute_idle_node_ratio":
      return String(row.cluster_id);
    case "compute_serving_endpoint_cost_status":
      return `${row.workspace_id}:${row.endpoint_id}:${row.served_entity_id}`;
    default:
      return JSON.stringify(row);
  }
}

export function flaggedRows(rows: Row[] | null | undefined): Row[] {
  return (rows || []).filter((r) => r.status === "CRITICAL" || r.status === "WARN");
}

// Sums `col` over `rows`' flagged rows, once per wasteRowKey(item.id, r) -- shared by the $ total
// below and by the DBU/magnitude figure a dollar-less check shows instead. Also reports whether
// any de-duplicated flagged row actually carried a non-null value for `col`: a check can be
// flagged (CRITICAL/WARN) with every row's $/DBU column NULL (e.g. an unpriced or unbilled
// warehouse, or a "tracking off" WARN with no measurement yet), and that must read as "unknown",
// never as a priced $0 (DEC-57/58).
function sumFlaggedByKey(item: WasteItem, rows: Row[], col: string): { total: number; anyNonNull: boolean } {
  const seen = new Set<string>();
  let total = 0;
  let anyNonNull = false;
  flaggedRows(rows).forEach((r) => {
    const key = wasteRowKey(item.id, r);
    if (seen.has(key)) return;
    seen.add(key);
    const v = r[col];
    if (v !== null && v !== undefined && Number.isFinite(Number(v))) anyNonNull = true;
    total += numOrZero(v);
  });
  return { total, anyNonNull };
}

// The ONLY $ figure a WASTE_ITEMS entry ever contributes: null unless the item's own dollarCol IS
// the one waste-basis column (isWasteDollarColumn) -- never a resource's whole spend (DEC-68). Also
// null (not 0) when every flagged row's dollarCol was NULL, so the entry falls through to dbuVal
// and unpricedFlagged instead of reading as "$0 of waste".
function possibleWasteUsd(item: WasteItem, rows: Row[], discountPct: number): number | null {
  if (!isWasteDollarColumn(item.dollarCol)) return null;
  const { total, anyNonNull } = sumFlaggedByKey(item, rows, item.dollarCol);
  if (!anyNonNull) return null;
  return total * (1 - numOrZero(discountPct));
}

// The DBU (or other magnitude) fallback a check shows when it has no waste $ column at all. Null
// (not 0) when every flagged row's dbuCol was NULL, for the same reason as possibleWasteUsd above.
function possibleWasteMagnitude(item: WasteItem, rows: Row[]): number | null {
  if (!item.dbuCol) return null;
  const { total, anyNonNull } = sumFlaggedByKey(item, rows, item.dbuCol);
  return anyNonNull ? total : null;
}

// One WASTE_ITEMS id -> its worklist entry: band (the already-loaded findings list's own
// authoritative CRITICAL/WARN/OK/NOT_ASSESSED verdict), and, only when flagged, a $ figure
// (possibleWasteUsd) or a DBU figure as a labelled fallback (possibleWasteMagnitude). `wasteState`
// is that item's own entry from a useMultiFindingData(WASTE_IDS, ...) fetch.
function buildWasteWorklistEntry(item: WasteItem, findingsById: Record<string, FindingSummary>, wasteState: FindingState | undefined): WasteEntry {
  const f = findingsById[item.id];
  const band: Band = f ? bandOf(f) : "NOT_ASSESSED";
  const isFlagged = band === "CRITICAL" || band === "WARN";
  const data = wasteState && wasteState.phase === "ready" && wasteState.outcome === "ok_rows" ? wasteState.data : null;
  const rows = data ? data.rows : null;
  const discountPct = data ? numOrZero(data.discount_pct) : 0;

  let dollarUsd: number | null = null, dbuVal: number | null = null, billedUsd: number | null = null, flaggedCount = 0;
  let truncated = false, returnedRows: number | null = null, rowsTotal: number | null = null;
  let reason: string | null = null, topRow: Row | null = null;
  if (isFlagged && data && rows) {
    const flagged = flaggedRows(rows);
    flaggedCount = flagged.length;
    dollarUsd = possibleWasteUsd(item, rows, discountPct);
    if (dollarUsd == null) dbuVal = possibleWasteMagnitude(item, rows);
    // No waste price: what the flagged resources billed, shown for scale and never added to waste.
    if (dollarUsd == null && item.spendCol) {
      const { total, anyNonNull } = sumFlaggedByKey(item, rows, item.spendCol);
      billedUsd = anyNonNull ? total * (1 - discountPct) : null;
    }
    // T-67 review round 2 (carried over): a capped row slice never reads as the whole finding.
    truncated = !!(data.rows_total > data.returned);
    if (truncated) { returnedRows = data.returned; rowsTotal = data.rows_total; }
    reason = findingReason(item.id, data.header, rows);
    // The single biggest flagged row (server order_by is status then the item's own $/magnitude
    // column desc) -- the caller resolves it to a resource name for "where" on the Fix-first list,
    // this file stays generic about what a resource id even is.
    topRow = flagged[0] || null;
  }

  return {
    id: item.id,
    area: item.area,
    // tab_waste.tsx's PricedSourceCard re-reads raw rows through offenderRows(), which needs the
    // WASTE_ITEMS entry's own dollar column name (not just the already-summed dollarUsd below).
    dollarCol: item.dollarCol,
    title: (f && f.title) || item.id,
    band, isFlagged, dollarUsd, dbuVal, billedUsd, spendWords: item.spendWords || "billed", flaggedCount, discountPct,
    truncated, returnedRows, rowsTotal, reason, topRow,
    // T-75A review round 1 (DEC-66.2): `f` already carries library_corrections (every
    // /api/findings row does) -- no extra fetch.
    corrections: f ? (f.library_corrections || []) : [],
    action: data ? firstAction(data.header) : null,
    rowCount: f ? f.row_count : 0,
  };
}

// Every WASTE_ITEMS id, in order -- the single call Overview and the Waste tab both make.
// `multiState` is a useMultiFindingData(WASTE_IDS, ...) result ({phase, byId}).
export function buildWasteWorklist(findingsById: Record<string, FindingSummary> | null, multiState: MultiFindingState | null | undefined): WasteEntry[] {
  const byId = (multiState && multiState.byId) || {};
  return WASTE_ITEMS.map((item) => buildWasteWorklistEntry(item, findingsById || {}, byId[item.id]));
}

// useBelowFloorWaste -- for every WASTE_ITEMS entry with a dollar column, the $ its own
// materiality floor moved from CRITICAL/WARN to OK (contract D: Api.aggregate's opts.belowFloor),
// so wasteTotal can report what the priced total leaves out without ever adding it in. WASTE_ITEMS
// is a fixed, module-level list, so this calls useFindingAgg the same number of times every
// render, same as any other hook.
export function useBelowFloorWaste(filters: Filters): Record<string, AggState> {
  const states = WASTE_ITEMS.map((item) => useFindingAgg(
    item.dollarCol ? item.id : null, filters.window, filters.workspaceIds, filters.envs,
    [], "sum", item.dollarCol, { belowFloor: true }
  ));
  const byId: Record<string, AggState> = {};
  WASTE_ITEMS.forEach((item, i) => { byId[item.id] = states[i]; });
  return byId;
}

// The one $ total: summed only from flagged, priced entries. `unpricedFlagged` is every OTHER
// flagged entry (no possibleWasteUsd), CRITICAL first then WARN, for a "flagged, no waste dollar
// figure" list. `overlapNote` names the one known cross-finding overlap (9.3 item 1): idle
// cluster-node minutes and failed-job-run cost can both land on the same classic job cluster, so a
// total that includes both flagged and priced says so plainly rather than silently subtracting
// anything (a later release will add the ledger that removes the double-count). `belowFloorById`
// (useBelowFloorWaste's own result, optional) adds belowFloorUsd/belowFloorRows/floor: dollars and
// rows a per-check materiality floor left OUT of every total above -- never folded into `usd`.
export function wasteTotal(worklist: WasteEntry[] | null, belowFloorById?: Record<string, AggState>): WasteTotal {
  const list = worklist || [];
  const flagged = list.filter((w) => w.isFlagged);
  const priced = flagged.filter((w): w is WasteEntry & { dollarUsd: number } => w.dollarUsd != null);
  const usd = priced.reduce((s, w) => s + w.dollarUsd, 0);
  const bandRank: Record<string, number> = { CRITICAL: 0, WARN: 1 };
  const unpricedFlagged = flagged
    .filter((w) => w.dollarUsd == null)
    .sort((a, b) => (bandRank[a.band] ?? 2) - (bandRank[b.band] ?? 2));
  const byId: Record<string, WasteEntry> = {};
  list.forEach((w) => { byId[w.id] = w; });
  const idleNode = byId.compute_idle_node_ratio;
  const failedJobs = byId.lakeflow_failed_jobs_wasted_dbus;
  const overlapNote = (idleNode && idleNode.isFlagged && idleNode.dollarUsd != null
    && failedJobs && failedJobs.isFlagged && failedJobs.dollarUsd != null)
    ? "idle cluster minutes during failed job runs can appear in both."
    : null;

  let belowFloorUsd = 0, belowFloorRows = 0, floor: Floor | null = null;
  if (belowFloorById) {
    list.filter((w) => w.dollarCol).forEach((w) => {
      const st = belowFloorById[w.id];
      if (st && st.phase === "ready" && st.outcome === "ok_rows") {
        belowFloorUsd += numOrZero(st.data.total_value) * (1 - numOrZero(st.data.discount_pct));
        belowFloorRows += numOrZero(st.data.matched_rows);
        if (!floor && st.data.floor) floor = st.data.floor;
      }
    });
  }

  return {
    usd, pricedCount: priced.length, flaggedCount: flagged.length, unpricedFlagged, overlapNote,
    belowFloorUsd, belowFloorRows, floor,
  };
}
