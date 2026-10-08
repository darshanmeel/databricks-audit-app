// the ONE place the compute-pressure enums become words
// (tasks/T-65-compute-pressure-verdicts.md "The words", DEC-65 rule 3). Two finding queries emit
// them: lakeflow_job_compute_pressure (one row per job) and query_warehouse_pressure (one row per
// SQL warehouse). Each carries `pressure` (the verdict) and `scaling_hint` (the lever) as enums
// written by the SQL, and `pressure_reason` (the WHY, built in SQL from the row's own numbers).
// Every screen that shows a verdict or a lever -- the Jobs strip and Compute fit sub-tab, the job
// focus panel, the Query performance strip and charts -- reads its words from this file and
// nowhere else, so no screen paraphrases a lever.
//
// SCALING_HINTS[*].text is copied VERBATIM from the two headers' `actions:` ladders -- the clause
// on the rung whose cost tier the hint carries (free / config / spend, DEC-59) -- except SCALE_OUT,
// which both queries emit, so its text is the task file's own wording that reads for both. "Scale
// up" and "scale out" are the user's words and Databricks uses them differently (a warehouse's
// warehouse_events logs SCALED_UP when it ADDS A CLUSTER), so each label says which knob it means
// and the chip's title carries the full clause, one hover away (T-65 rule 9).
//
// No dollars anywhere (DEC-61.1, DEC-65 rule 6): the `spend` chip is a cost TIER, never a figure.
//
// PRESSURE_FIRST_STEPS (review round 1, DEC-65 rule 7 / DEC-59): the FREE rung-1 clause of each
// verdict, copied verbatim from the same two ladders (the MEMORY clause of the jobs ladder is its
// remedy half, "first cut the input per run ...", shortened but never rewritten, so a tile's
// 90-character head names the fix rather than the diagnosis). It is keyed by query and verdict,
// because the two ladders give MEMORY different first steps. A verdict whose ladder has no free
// clause -- CPU at the ceiling, NONE -- has none here; review round 2: the tile then carries NO
// action (the header's rung 1 for that verdict is the meta-instruction "read scaling_hint with
// ...", not a remedy), and the lever chip on its reading is the only suggestion (DEC-59.2: omitted
// rather than invented).
//
// Review round 2, a display-only derivation (every word still lives here): a CPU row whose
// scaling_hint is NONE (status OK -- CPU-bound with no scale-out lever, DEC-65 rule 4) is its own
// chart segment and badge, "CPU-bound, not flagged", so a healthy CPU job never counts beside the
// flagged CPU-at-ceiling ones.
//
// SCALE_UP_THEN_WARM is the SQL's own lever for a MEMORY_AND_CAPACITY warehouse that queued BELOW
// its maximum (at_ceiling FALSE): size first, then a warm cluster. It is shown as the two chips
// SCALE_UP then KEEP_WARM, so each step keeps its own tier (spend, then config).

import React from "react";
import type { FindingState, Row } from "../types";
import type { Segment } from "./charts";
import type { TileProps } from "./overview_tile";

import { fmtDuration, fmtInt, fmtMoney } from "../format";
import { countBy, numOrZero } from "./hooks";
import { CHART_OTHER_COLOR } from "./charts";
import { firstAction } from "./overview_tile";

export const PRESSURE_LABELS: Record<string, string> = {
  MEMORY: "Memory-bound",
  CPU: "CPU-bound",
  SKEW: "Skewed",
  DRIVER: "Driver-bound",
  IO_WAIT: "I/O wait",
  IDLE: "Idle workers",
  NONE: "No pressure",
  MEMORY_AND_CAPACITY: "Memory + concurrency",
  CAPACITY: "Concurrency-bound",
  // task_cluster_utilization's own bottleneck_hint enum -- a task-level read, distinct from (and
  // never colliding with) the job-level `pressure` values just above.
  WAITING_OR_IDLE: "Waiting or idle",
  DRIVER_BOUND: "Driver-bound",
  SKEWED: "Skewed",
  COMPUTE_BOUND: "CPU-bound",
  MEMORY_PRESSURE: "Memory-bound",
  MIXED: "Mixed",
};

// Display-only keys derived from a row (never emitted by the SQL): see the header comment.
const PRESSURE_DERIVED_LABELS: Record<string, string> = {
  CPU_NOT_FLAGGED: "CPU-bound, not flagged",
  UNDER_FLOOR: "Pressure seen, too little use to judge",
};

/** A scaling lever: its label, its cost tier and the ladder clause behind it. */
interface ScalingHint {
  label: string;
  tier: string;
  text: string;
}

const SCALING_HINTS: Record<string, ScalingHint | null> = {
  SCALE_UP_MEMORY: {
    label: "Scale up (memory per worker)", tier: "spend",
    text: "scale up: a worker node type with more memory per worker (Databricks: increase the amount of memory available on your instances)",
  },
  SCALE_OUT: {
    label: "Scale out (workers / clusters)", tier: "spend",
    text: "more workers / more clusters: raise max_autoscale_workers on a job cluster, max_clusters on a warehouse -- CPU or concurrency was saturated at the ceiling",
  },
  SCALE_DOWN: {
    label: "Scale down", tier: "config",
    text: "fewer or smaller workers, or autoscale down to the minimum",
  },
  SCALE_UP: {
    label: "Scale up (warehouse size)", tier: "spend",
    text: "scale up: a larger warehouse_size gives every cluster more memory per statement",
  },
  SCALE_UP_THEN_OUT: {
    label: "Scale up (size), then out (clusters)", tier: "spend",
    text: "size first, then clusters",
  },
  SCALE_UP_THEN_WARM: {
    label: "Scale up (size), then keep a cluster warm", tier: "spend",
    text: "size first, then raise min_clusters so a second cluster is already running when the load arrives",
  },
  KEEP_WARM: {
    label: "Keep a cluster warm", tier: "config",
    text: "raise min_clusters so a second cluster is already running when the load arrives, or route the heaviest recurring shapes to their own warehouse",
  },
  FIX_SKEW: {
    label: "Fix the key, not the cluster", tier: "free",
    text: "salt or repartition the hot key, adding nodes will not help",
  },
  DISTRIBUTE_WORK: {
    label: "Distribute the work", tier: "free",
    text: "move pandas / collect() / driver-side loops onto Spark",
  },
  FIX_IO: {
    label: "Fix the I/O", tier: "free",
    text: "compact small files or cache the input",
  },
  NONE: null,
};

const PRESSURE_FIRST_STEPS: Record<string, Record<string, string>> = {
  lakeflow_job_compute_pressure: {
    MEMORY: "first cut the input per run (narrow the reprocessed window, filter and prune earlier, process incrementally)",
    SKEW: "salt or repartition the hot key, adding nodes will not help",
    DRIVER: "move pandas / collect() / driver-side loops onto Spark",
    IO_WAIT: "compact small files or cache the input",
    IDLE: "find the pause in the driver log, an external call or a serial loop",
  },
  query_warehouse_pressure: {
    MEMORY: "find the spilling statements in query_local_spillage and cut their shuffle: broadcast the small side, filter earlier, avoid exploding joins",
    CAPACITY: "check with query_workload_mix_hours whether the queueing clusters at a few hours before changing the warehouse",
    // size first (the ladder's rung 3), so the free step is the memory one
    MEMORY_AND_CAPACITY: "find the spilling statements in query_local_spillage and cut their shuffle: broadcast the small side, filter earlier, avoid exploding joins",
  },
};

// Both headers' :top_n default (100000, far above any account's job or warehouse count, so every
// job and warehouse keeps its verdict row). The build-time LIMIT applies across every workspace
// and cuts the OK rows first (the ORDER BY ranks them last); a thresholds.yml override is not
// visible to the UI, so this note fires only at the default cap.
const PRESSURE_ROW_CAP = 100000;

// The job focus panel's words for a job missing from a result the build-time LIMIT reached
// (review round 2): not a pass -- the cap cut OK and NOT_ASSESSED rows first.
function pressureCappedOutNote() {
  return `not in the capped result (top ${fmtInt(PRESSURE_ROW_CAP)} rows across all workspaces) -- no verdict shown for this job`;
}

// The free first step for one verdict of one query, verbatim, or null when the ladder has none.
function pressureFirstStep(queryId: string, pressure: unknown): string | null {
  if (pressure === null || pressure === undefined) return null;
  const byQuery = Object.prototype.hasOwnProperty.call(PRESSURE_FIRST_STEPS, queryId) ? PRESSURE_FIRST_STEPS[queryId] : null;
  if (!byQuery) return null;
  const key = String(pressure);
  return Object.prototype.hasOwnProperty.call(byQuery, key) ? byQuery[key] : null;
}

// A tile `action` for the worst row: its free first step, cut to the tile's head by the SAME
// firstAction() every other tile uses (the clause is fed back as a one-rung ladder ending
// "(free)", so the truncation and the tier chip are the shared helper's, not a second copy).
// null when the verdict has no free step -- pressureTileWithAction then leaves the tile without one.
function pressureTileAction(queryId: string, row: Row | null | undefined) {
  const step = row ? pressureFirstStep(queryId, row.pressure) : null;
  return step ? firstAction({ actions: [`${step} (free)`] }) : null;
}

// The tile with its `action` settled (review round 2). A flagged tile whose worst row is known
// carries that row's free first step, or NO action when its verdict has none (CPU at the ceiling):
// the header's rung 1 is then the meta-instruction "read scaling_hint with ...", which names no
// remedy, and the reading's lever chip already says what to change. Until the rows load (no worst
// row yet) the shared helper's own action is left as it was.
function pressureTileWithAction(tile: TileProps, queryId: string, worst: Row | null | undefined): TileProps {
  if (!tile || !worst) return tile;
  if (tile.severity !== "critical" && tile.severity !== "warn") return tile;
  return { ...tile, action: pressureTileAction(queryId, worst) || undefined };
}

// True for the two verdict queries (one row per object whatever its status).
function isPressureQuery(queryId: string): boolean {
  return Object.prototype.hasOwnProperty.call(PRESSURE_FIRST_STEPS, queryId);
}

// {label, tier, text} for a scaling_hint value, or null for NONE, NULL (not assessed) and any value
// this map does not know -- a lever with no words written for it gets no invented ones. Own keys
// only, so a stray "constructor" can never resolve to Object's prototype.
function scalingHint(hint: unknown): ScalingHint | null {
  if (hint === null || hint === undefined) return null;
  const key = String(hint);
  return Object.prototype.hasOwnProperty.call(SCALING_HINTS, key) ? SCALING_HINTS[key] : null;
}

// The pressure label, falling back to the raw enum for a value this map does not know (never
// dropped, never renamed into something the query did not say).
function pressureLabel(pressure: unknown): string {
  if (pressure === null || pressure === undefined) return "Not assessed";
  const key = String(pressure);
  if (Object.prototype.hasOwnProperty.call(PRESSURE_DERIVED_LABELS, key)) return PRESSURE_DERIVED_LABELS[key];
  return Object.prototype.hasOwnProperty.call(PRESSURE_LABELS, key) ? PRESSURE_LABELS[key] : key;
}

// The display key of a row: its pressure, except a CPU row with no lever (scaling_hint NONE --
// status OK) reads CPU_NOT_FLAGGED, so it is never counted with the flagged CPU-at-ceiling rows.
function pressureRowKey(row: Row | null | undefined): string | null {
  if (!row || row.pressure === null || row.pressure === undefined) return null;
  const p = String(row.pressure);
  if (row.below_floor && p !== "NONE") return "UNDER_FLOOR";
  if (p === "CPU" && row.scaling_hint === "NONE") return "CPU_NOT_FLAGGED";
  return p;
}

export function pressureRowLabel(row: Row | null | undefined): string {
  return pressureLabel(pressureRowKey(row));
}

// The lever(s) to show for a row, in order. SCALE_UP_THEN_WARM (a MEMORY_AND_CAPACITY warehouse
// below its ceiling) is size first, then a warm cluster, so it shows those two levers, each with
// its own tier.
function pressureRowHints(row: Row | null | undefined): string[] {
  if (!row || row.scaling_hint === null || row.scaling_hint === undefined) return [];
  if (row.scaling_hint === "SCALE_UP_THEN_WARM") return ["SCALE_UP", "KEEP_WARM"];
  return [String(row.scaling_hint)];
}

// "Scale up (warehouse size) then Keep a cluster warm" -- the labels of a row's levers, or null.
export function pressureLeverLabel(row: Row | null | undefined): string | null {
  const labels = pressureRowHints(row).map((h) => scalingHint(h)).filter((h): h is ScalingHint => !!h).map((h) => h.label);
  return labels.length ? labels.join(" then ") : null;
}

// The lever chip: the existing tier chip (free / config / spend -- so "scale up" visibly says
// `spend`) plus the lever's label, with the lever's full ladder clause in `title`. Renders nothing
// for NONE or a NULL hint.
function ScalingHintChip({ hint }: { hint: string }) {
  const h = scalingHint(hint);
  if (!h) return null;
  return (
    <span className="scaling-hint-chip" title={h.text}>
      <span className={`tier-chip tier-${h.tier}`}>{h.tier}</span>
      <span className="scaling-hint-label">{h.label}</span>
    </span>
  );
}

// A row's lever chip(s): one ScalingHintChip, or two joined by "then" (pressureRowHints).
export function ScalingHintChips({ row }: { row: Row | null | undefined }) {
  const hints = pressureRowHints(row).filter((h) => scalingHint(h));
  if (!hints.length) return null;
  return (
    <span className="scaling-hint-chips">
      {hints.map((h, i) => (
        <React.Fragment key={h}>
          {i > 0 && <span className="scaling-hint-then"> then </span>}
          <ScalingHintChip hint={h} />
        </React.Fragment>
      ))}
    </span>
  );
}

// ---------------------------------------------------------------------------------------------
// Chart and tile helpers shared by tab_jobs.tsx and tab_query.tsx, so the enum -> label -> colour
// mapping and the NOT_ASSESSED handling exist once, beside the words they render.

// Review round 1: this used to be a `charts.tsx` paletteColor INDEX per verdict, over what is now
// a 5-slot palette that no longer cycles (paletteColor(i>=5) is always the Other grey, never wraps
// back onto c1..c5). Index 1 (NONE) and index 7 (CPU_NOT_FLAGGED, 7%6==1 under the OLD 6-slot
// palette) landed on the same colour by accident; index 5 (MEMORY_AND_CAPACITY, IO_WAIT) both read
// as the Other grey; a null (not-assessed) row got index 2, one slot from Critical.
//
// Every verdict now maps to an explicit token, not an index someone has to keep in sync with
// palette length:
// - Jobs and warehouses never share a chart (lakeflow_job_compute_pressure and
//   query_warehouse_pressure are two different tiles/charts), so a job-only verdict and a
//   warehouse-only verdict CAN share a token on purpose without ever being confused for each
//   other on screen -- MEMORY is the one verdict both queries emit, so it keeps one colour either
//   way. The one query that can genuinely show more flagged verdicts at once than the palette has
//   slots (jobs: MEMORY/CPU/SKEW/DRIVER/IO_WAIT/IDLE, 6 keys) reuses its rarest pair, IDLE and
//   DRIVER -- both name a stalled/paused execution, the closest two in meaning -- rather than an
//   arbitrary collision.
// - NONE and CPU_NOT_FLAGGED are both "nothing to flag here" reads of a chart that is otherwise a
//   category breakdown, not two more categories competing for a series slot -- one shared neutral,
//   never a chart-series hue (never confusable with a real flagged verdict).
// - null (not assessed) is the app's own not-assessed colour, --st-na -- what it actually means --
//   never a series slot (and never the former index-2 near-miss with Critical).
const PRESSURE_COLOR_MAP: Record<string, string> = {
  MEMORY: "var(--c1)",
  CPU: "var(--c2)",
  SKEW: "var(--c3)",
  // One hue per cause; purple stays "Not assessed" only.
  DRIVER: "var(--c6)",
  IDLE: "var(--c5)",
  IO_WAIT: "var(--c7)",
  CAPACITY: "var(--c2)", // warehouse-only; shares CPU's token (jobs/warehouses never share a chart)
  MEMORY_AND_CAPACITY: "var(--c3)", // warehouse-only; shares SKEW's token, same reason
  NONE: "var(--text-3)",
  CPU_NOT_FLAGGED: "var(--text-3)",
  UNDER_FLOOR: "var(--c-other)",
  // task_cluster_utilization hints, the same hue as the job-level cause they match
  MEMORY_PRESSURE: "var(--c1)",
  COMPUTE_BOUND: "var(--c2)",
  SKEWED: "var(--c3)",
  WAITING_OR_IDLE: "var(--c5)",
  DRIVER_BOUND: "var(--c6)",
  MIXED: "var(--c7)",
};

// Segment/legend order, worst first across both queries -- independent of colour now that colour
// is an explicit map rather than a positional index.
const PRESSURE_KEY_ORDER = [
  "MEMORY_AND_CAPACITY", "MEMORY", "CAPACITY", "SKEW", "CPU", "IO_WAIT", "DRIVER", "IDLE",
  "CPU_NOT_FLAGGED", "UNDER_FLOOR", "NONE",
];

export function pressureColor(pressure: unknown): string {
  if (pressure === null || pressure === undefined) return "var(--st-na)";
  const key = String(pressure);
  if (Object.prototype.hasOwnProperty.call(PRESSURE_COLOR_MAP, key)) return PRESSURE_COLOR_MAP[key];
  return CHART_OTHER_COLOR;
}

// Donut segments: one per verdict present (a CPU row with no lever as its own "CPU-bound, not
// flagged" segment -- pressureRowKey), plus ONE "Not assessed" segment for the rows whose pressure
// is NULL (status NOT_ASSESSED) -- kept, never dropped (T-65 rule 4). A verdict this file does not
// know still gets its own segment under its raw name.
export function pressureSegments(rows: Row[] | null | undefined): Segment[] {
  const counts = new Map<string | null, number>();
  (rows || []).forEach((r) => {
    const k = pressureRowKey(r);
    counts.set(k, (counts.get(k) || 0) + 1);
  });
  const known = PRESSURE_KEY_ORDER;
  const keys: (string | null)[] = [
    ...known.filter((k) => counts.has(k)),
    ...[...counts.keys()].filter((k) => k !== null && !known.includes(k)),
  ];
  if (counts.has(null)) keys.push(null);
  return keys.map((k) => ({
    label: pressureLabel(k), value: counts.get(k)!, display: fmtInt(counts.get(k)), color: pressureColor(k),
  }));
}

// Both queries' ORDER BY rank: CRITICAL, WARN, NOT_ASSESSED, then everything else.
function pressureStatusRank(status: unknown): number {
  if (status === "CRITICAL") return 0;
  if (status === "WARN") return 1;
  if (status === "NOT_ASSESSED") return 2;
  return 3;
}

// "3 not assessed (2 no_cluster_recorded, 1 too_few_slices)." -- the named reasons, never a
// blank; null when every row was judged.
function pressureNotAssessedLine(rows: Row[] | null | undefined): string | null {
  const na = (rows || []).filter((r) => r.status === "NOT_ASSESSED");
  if (!na.length) return null;
  const parts = countBy(na, (r) => r.not_assessed_reason || "reason not recorded")
    .sort((a, b) => b.value - a.value)
    .map((e) => `${fmtInt(e.value)} ${e.name}`);
  return `${fmtInt(na.length)} not assessed (${parts.join(", ")}).`;
}

// T-67: singleIdTileProps (overview_tile.tsx) now derives the tile straight from the finding's own
// band, including ERROR/NOT_ASSESSED/EMPTY_WINDOW/EMPTY_FILTERS (it used to fall through all four
// to a green "0 flagged"), and bandOf (primitives.tsx) now reads a finding whose rows are ALL
// NOT_ASSESSED (every job serverless, say) as band NOT_ASSESSED rather than OK -- so callers pass
// their own EMPTY_WINDOW words straight to singleIdTileProps' own `emptyWindowReading` argument.
// Review round 2: singleIdTileProps' own CRITICAL/WARN branch now also reads THIS tile's own
// per-row fetch (`state`) honestly -- phase 'error' and outcome 'error'/'not_assessed'/
// 'ok_empty_window'/'ok_empty_filters' are all caught there before it ever builds a flagged tile
// -- so a fetch failure on this tile's own call no longer needs catching here too. The one thing
// that still belongs in this file, not the shared helper, is naming WHICH not_assessed_reason
// codes drove the verdict (no_cluster_recorded, too_few_slices, ...), and -- when the finding is
// genuinely OK (some rows judged clean) but a few rows are NOT_ASSESSED too -- keeping OK and
// just annotating it, never downgrading it (DEC-65): only an ALL-NOT_ASSESSED result is
// downgraded, and bandOf already did that.
function pressureTileHonesty(tile: TileProps, state: FindingState | null | undefined): TileProps {
  if (!tile) return tile;
  if (!state || state.phase !== "ready" || state.outcome !== "ok_rows") return tile;
  const rows = state.data.rows || [];
  const naLine = pressureNotAssessedLine(rows);
  if (!naLine) return tile;
  if (tile.severity === "not_assessed") return { ...tile, reading: `Nothing could be judged this window: ${naLine}` };
  if (tile.severity !== "ok") return tile;
  const judged = rows.filter((r) => r.status !== "NOT_ASSESSED").length;
  return { ...tile, reading: `None flagged among ${fmtInt(judged)} judged; ${naLine}` };
}

// "Capped at 100,000 rows (:top_n) ..." under a pressure chart when the build-time LIMIT was reached
// (rows_in_window is the pre-filter count across every workspace), else null.
function pressureCapNote(state: FindingState | null | undefined): string | null {
  const n = state && state.data ? state.data.rows_in_window : null;
  if (n === null || n === undefined || n < PRESSURE_ROW_CAP) return null;
  return `Capped at ${fmtInt(PRESSURE_ROW_CAP)} rows (:top_n) across all workspaces; the OK rows beyond the cap are not counted here.`;
}

// ---------------------------------------------------------------------------------------------
// Warehouse change lines -- a concrete before -> after with both sides, shared by Compute >
// Warehouses (tab_compute.tsx) and Queries > Capacity (tab_query.tsx) so the same warehouse reads
// the same fix in both places. Never a line when a value it needs is null.

// Auto-stop: the same target compute_warehouse_autostop_churn/compute_warehouse_cache_reuse use --
// serverless can go to 5 min, everything else's floor is 10 min. `idleRow` is a
// compute_warehouse_idle_minutes row (autostop_N_idle_minutes/autostop_N_cold_starts/
// usd_per_cluster_minute); `disc` is the account's own discount fraction (0 when none). The one
// rule Waste, Compute and Queries all share, so the same warehouse never reads two different
// targets or savings depending which tab it is on.
export function autostopChange(idleRow: Row | null | undefined, disc: number | null | undefined, toMinutes?: number): { current: number; target: number; savingUsd: number; extraColdStarts: number } | null {
  if (!idleRow) return null;
  const target = toMinutes || (idleRow.warehouse_kind === "serverless" ? 5 : 10);
  const current = idleRow.auto_stop_minutes;
  if (current == null || current <= target) return null;
  const nowIdle = idleRow.autostop_now_idle_minutes, targetIdle = idleRow[`autostop_${target}_idle_minutes`];
  const nowStarts = idleRow.autostop_now_cold_starts, targetStarts = idleRow[`autostop_${target}_cold_starts`];
  const rate = idleRow.usd_per_cluster_minute;
  if (nowIdle == null || targetIdle == null || nowStarts == null || targetStarts == null || rate == null) return null;
  const savingUsd = (nowIdle - targetIdle) * rate * (1 - (disc || 0));
  if (savingUsd < 1) return null;
  return { current, target, savingUsd, extraColdStarts: targetStarts - nowStarts };
}

// Serverless can go to 5 min in the UI and lower through the API, so it gets 5, 2 and 1 min; pro and classic stop at 10.
export function autostopOptions(idleRow: Row | null | undefined, disc: number | null | undefined) {
  if (!idleRow) return [];
  const targets = idleRow.warehouse_kind === "serverless" ? [5, 2, 1] : [10];
  return targets.map((t) => autostopChange(idleRow, disc, t)).filter((c): c is NonNullable<typeof c> => !!c);
}

export function autostopChangeLine(idleRow: Row | null | undefined, disc: number | null | undefined, windowDays: number): string | null {
  const ch = autostopChange(idleRow, disc);
  if (!ch) return null;
  const { current, target, savingUsd, extraColdStarts } = ch;
  const coldStartsText = extraColdStarts !== 0
    ? `, ${extraColdStarts > 0 ? "+" : ""}${fmtInt(extraColdStarts)} cold start${Math.abs(extraColdStarts) === 1 ? "" : "s"}`
    : "";
  return `Auto-stop ${fmtInt(current)} → ${target} min: ${savingUsd >= 0 ? "−" : "+"}${fmtMoney(Math.abs(savingUsd), 0)} / ${fmtInt(windowDays)} d${coldStartsText}`;
}

// Max clusters: only when query_warehouse_pressure saw this warehouse queueing at its own cluster
// ceiling. `pressureRow` is a query_warehouse_pressure row; N is the dims' own max_clusters, or
// (a warehouse whose config row is missing) the highest cluster count an event actually showed.
export function maxClustersChangeLine(pressureRow: Row | null | undefined, windowDays: number): string | null {
  if (!pressureRow || !pressureRow.at_ceiling) return null;
  if (pressureRow.status !== "WARN" && pressureRow.status !== "CRITICAL") return null;
  const waitedS = numOrZero(pressureRow.waiting_at_capacity_s_sum);
  if (waitedS <= 0) return null;
  const n = pressureRow.max_clusters != null ? pressureRow.max_clusters : pressureRow.max_cluster_count_seen;
  if (n == null) return null;
  return `Max clusters ${fmtInt(n)} → ${fmtInt(n + 1)}: -${fmtDuration(waitedS)} queued in ${fmtInt(windowDays)}d; cost not estimated`;
}

// Both lines for one warehouse, in order, never more than the ones that could be computed.
export function warehouseChangeLines(idleRow: Row | null | undefined, pressureRow: Row | null | undefined, disc: number | null | undefined, windowDays: number): string[] {
  return [autostopChangeLine(idleRow, disc, windowDays), maxClustersChangeLine(pressureRow, windowDays)].filter((l): l is string => !!l);
}
