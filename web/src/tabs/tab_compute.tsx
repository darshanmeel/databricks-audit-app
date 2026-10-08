// Compute area, 3 sub-tabs (section 6): Warehouses,
// Clusters & pools, Configuration. Registers each sub-tab's content via AreaContent.register
// (primitives.tsx); the generic AreaPage frame renders the header, sub-tabs and checks table
// around whatever is registered here. task_cluster_utilization moved fully to Jobs > Compute fit
// (tab_registry.ts), so it is not read anywhere in this file.

// ─────────────────────────────────────────── small local helpers ───────────────────────────────

import React from "react";

import { fmtDayShort, fmtDuration, fmtInt, fmtMoney, fmtPct } from "../format";
import { resolveName } from "../components/names";
import { clusterName, groupsToEntries, numOrZero, sumBy, topNWithOther, useFindingAgg, useFindingData, warehouseName } from "../components/hooks";
import { bandOf } from "../components/tab_registry";
import { AreaContent, Card, Facts, RowStatusPill, StatusIcon, StatusPill, bandPillKind, isUnderFloor } from "../components/primitives";
import { getCheckLabel } from "../components/labels";
import { ChartNote, Donut, HBarList, paletteColor, shortId } from "../components/charts";
import { BandRows } from "../components/band_rows";
import type { BandRow } from "../components/band_rows";
import { warehouseChangeLines } from "../components/pressure";
import type { AggState, DimMaps, Fact, FindingState, FindingSummary, Floor, Row } from "../types";
import type { Band } from "../components/tab_registry";
import type { TabProps } from "../components/primitives";
import { useStartupWaits } from "./startup_waits";
import type { Startup } from "./startup_waits";

// CjKpi/CjKpis: the redesign's own headline-card shape (section 4.6) -- label, one big mono
// value, one or two plain lines, an optional link. Deliberately simpler than overview_tile.tsx's
// SummaryTile (no action ladder, no severity border, no corrections chip): "no threshold text, no
// chips" on a sub-tab card, per the brief -- the fix belongs in the checks table below.
export function CjKpi({ label, value, tone, note, facts, link, onLink }: {
  label: React.ReactNode; value: React.ReactNode; tone?: string | null; note?: React.ReactNode; facts?: (Fact | null)[] | null;
  link?: React.ReactNode; onLink?: () => void;
}) {
  // A check that did not run never shows its all-clear facts ("Risk None") under the dash.
  if (value === NOT_RUN_VALUE) { facts = [{ label: "Status", value: "Not assessed", tone: "muted" }]; tone = ""; }
  if (value === LOADING_VALUE) facts = null;
  return (
    <div className="card cj-kpi">
      <span className="cj-kpi-label">{label}</span>
      <span className={`cj-kpi-value ${tone || ""}`}>{value}</span>
      {facts ? <Facts items={facts} /> : (note && <span className="cj-kpi-note">{note}</span>)}
      {link && <button type="button" className="cj-kpi-link" onClick={onLink}>{link}</button>}
    </div>
  );
}
export function CjKpis({ children }: { children?: React.ReactNode }) { return <div className="cj-kpis">{children}</div>; }

// A KPI value while its finding is still loading -- the same "..." every table cell in this app
// uses (findings_table.tsx's AffectedCell/MoneyCell), never a fabricated number or a blank.
const LOADING_VALUE = "...";
const NOT_RUN_VALUE = "–";

// The value a KPI shows once its fetch has SETTLED but there are no real rows to compute from --
// never LOADING_VALUE again once phase is "ready" (that would read as a stuck spinner forever for
// an outcome that already resolved, e.g. a source this account has never populated).
export function kpiPending(state: { phase: string; outcome: unknown } | null | undefined): string {
  if (!state || state.phase === "loading") return LOADING_VALUE;
  if (state.outcome === "not_assessed" || state.outcome === "error") return NOT_RUN_VALUE;
  return "0"; // ok_empty_window / ok_empty_filters: a settled, real absence of rows
}

export function readyRows(state: FindingState | null | undefined): Row[] | null {
  return state && state.phase === "ready" && state.outcome === "ok_rows" ? state.data.rows : null;
}

// The finding row for one query_id, from the props `findings` list already scoped to this
// sub-tab -- used for the small band/status chip on a card (never fetched again).
export function findingFor(findings: FindingSummary[] | null | undefined, queryId: string): FindingSummary | null {
  return (findings || []).find((f) => f.query_id === queryId) || null;
}

// ─────────────────────────────────────────── Warehouses ───────────────────────────────────────

// Per-row "the fix": derived from compute_warehouse_idle_minutes' own numeric breakdown of WHERE
// the counted idle time sits (start/between/tail/no-query), never the row's raw `waste_reason`
// text (that is a description of what happened, written for the detail panel, not a plain
// instruction -- and it names raw column-shaped numbers the redesign's plain-words rule (DEC-74)
// keeps out of a headline table). Whichever bucket has the most minutes drives the sentence.
function warehouseFixText(row: Row): string {
  const start = numOrZero(row.start_gap_minutes), between = numOrZero(row.between_queries_minutes);
  const tail = numOrZero(row.stop_tail_minutes), noQuery = numOrZero(row.no_query_minutes);
  const counted = numOrZero(row.counted_idle_minutes);
  // Under an hour reads in minutes, never "0.0 h".
  const asHours = (m: number) => (m < 60 ? `${fmtInt(Math.round(m))} min` : m < 600 ? `${(m / 60).toFixed(1)} h` : `${fmtInt(m / 60)} h`);
  if (counted <= 0) {
    return "Not idle enough to flag on time alone; flagged on possible-waste dollars instead -- check its busiest hours.";
  }
  const top = Math.max(start, between, tail, noQuery);
  if (top === tail && tail > 0) {
    return `Lower auto-stop below ${fmtInt(row.auto_stop_minutes)} min. ${asHours(tail)} of its idle time came after the last query, waiting to stop.`;
  }
  if (top === noQuery && noQuery > 0) {
    const periods = row.no_query_periods != null ? ` across ${fmtInt(row.no_query_periods)} running period${row.no_query_periods === 1 ? "" : "s"}` : "";
    return `${asHours(noQuery)} running with no query at all${periods}: find what kept it up.`;
  }
  if (top === between && between > 0) {
    return `Idle is mostly between queries (${asHours(between)}). Schedule its work together, or move it to a shared warehouse, so it runs in fewer bursts.`;
  }
  if (top === start && start > 0) {
    return `${asHours(start)} idle before its first query each time it starts. A shorter auto-stop or serverless (starts in seconds) would cost less here.`;
  }
  return "Review whether this warehouse's schedule matches when it is actually queried.";
}

// The auto-stop floor for a warehouse's own type (compute_warehouse_autostop_churn/
// compute_warehouse_cache_reuse's own "actions" text) -- serverless can go to 5 minutes in the
// UI, classic/pro's minimum is 10 minutes. Unknown kind -> null, never a guessed number.
function suggestedAutoStopMinutes(kind: string | null | undefined): number | null {
  if (kind === "serverless") return 5;
  if (kind === "classic" || kind === "pro") return 10;
  return null;
}

function WarehouseRuntimeChart({ rows, maxCat, dims }: { rows: Row[] | null; maxCat: number; dims: DimMaps | null }) {
  if (!rows || !rows.length) return null;
  const ranked = [...rows].sort((a, b) => numOrZero(b.running_minutes) - numOrZero(a.running_minutes)).slice(0, maxCat || 8);
  const scaleMax = Math.max(...ranked.map((r) => numOrZero(r.running_minutes)), 1);
  const pct = (m: unknown) => `${Math.max(0, Math.min(100, (numOrZero(m) / scaleMax) * 100))}%`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round((scaleMax * f) / 60));
  return (
    <div>
      <div className="cj-card-subtitle">
        <span className="cj-legend-sw"><span className="sw" style={{ background: "var(--st-ok)" }}></span>Busy (a query running)</span>
        <span className="cj-legend-sw"><span className="sw" style={{ background: "var(--st-crit)" }}></span>Idle, over 1 min (counted as waste)</span>
        <span className="cj-legend-sw"><span className="sw" style={{ background: "var(--border-strong)" }}></span>Short pauses, normal</span>
      </div>
      {ranked.map((r) => {
        const busy = numOrZero(r.busy_minutes);
        const idle = numOrZero(r.counted_idle_minutes);
        const short = Math.max(0, numOrZero(r.idle_minutes) - idle);
        const name = warehouseName(dims, r.warehouse_id);
        return (
          <div className="cj-runtime-row" key={`${r.workspace_id}:${r.warehouse_id}`}>
            <span className="cj-runtime-name" title={name}>{name}</span>
            <div className="cj-runtime-track" title={`${Math.round(numOrZero(r.running_minutes) / 60)} h running, ${Math.round(busy / 60)} h busy, ${Math.round(idle / 60)} h idle over 1 min`}>
              <div className="cj-runtime-fill" style={{ width: pct(r.running_minutes) }}>
                <div className="cj-runtime-seg-busy" style={{ width: `${(busy / Math.max(numOrZero(r.running_minutes), 1)) * 100}%` }}></div>
                <div className="cj-runtime-seg-idle" style={{ width: `${(idle / Math.max(numOrZero(r.running_minutes), 1)) * 100}%` }}></div>
                <div className="cj-runtime-seg-short" style={{ width: `${(short / Math.max(numOrZero(r.running_minutes), 1)) * 100}%` }}></div>
              </div>
            </div>
            <span className="cj-runtime-total">{`${fmtInt(Math.round(numOrZero(r.running_minutes) / 60))} h`}</span>
          </div>
        );
      })}
      <div className="cj-runtime-axis">{ticks.map((h, i) => <span key={i}>{`${h} h`}</span>)}</div>
    </div>
  );
}

// Warehouse auto-stop bands; Off shows only when a warehouse has it.
const AUTOSTOP_BANDS: { label: string; test: (m: number) => boolean }[] = [
  { label: "1 min", test: (m) => m === 1 },
  { label: "2 min", test: (m) => m === 2 },
  { label: "3–5 min", test: (m) => m >= 3 && m <= 5 },
  { label: "6–10 min", test: (m) => m >= 6 && m <= 10 },
  { label: "11–15 min", test: (m) => m >= 11 && m <= 15 },
  { label: "16–20 min", test: (m) => m >= 16 && m <= 20 },
  { label: "21–30 min", test: (m) => m >= 21 && m <= 30 },
  { label: "Over 30 min", test: (m) => m > 30 },
  { label: "Off", test: (m) => m === 0 },
];

// Cost bands: how many in each, their cost and the idle part of it. A band with none reads "none".
function costBand(label: string, n: number, noun: string, sub: string, total: number, idle: number, priced: boolean): BandRow {
  return {
    label, total, part: idle,
    sub: n ? `${fmtInt(n)} ${noun}${n === 1 ? "" : "s"}${sub ? ` · ${sub}` : ""}` : `No ${noun}s`,
    title: priced ? `${fmtMoney(idle, 0)} idle of ${fmtMoney(total, 0)}` : undefined,
    text: !n ? <span className="muted">none</span> : !priced ? <span className="muted">no cost in the window</span>
      : <React.Fragment><b className="mono">{fmtMoney(idle, 0)}</b>{` idle of ${fmtMoney(total, 0)}`}{total > 0 ? ` (${fmtPct((idle / total) * 100, 0)})` : ""}</React.Fragment>,
  };
}

// Every measured warehouse, flagged or not, by its auto-stop setting: how many, their cost and the idle part of it.
function AutostopBandsCard({ rows, disc, windowDays }: { rows: Row[] | null; disc: number; windowDays: number }) {
  if (!rows || !rows.length) return null;
  const net = 1 - disc;
  const bands = AUTOSTOP_BANDS.map((b) => {
    const list = rows.filter((r) => r.auto_stop_minutes != null && b.test(numOrZero(r.auto_stop_minutes)));
    if (b.label === "Off" && !list.length) return null;
    const kinds = new Map<string, number>();
    list.forEach((r) => { const k = String(r.warehouse_kind || "unknown type"); kinds.set(k, (kinds.get(k) || 0) + 1); });
    const priced = list.filter((r) => r.est_usd_list != null);
    return costBand(b.label, list.length, "warehouse", Array.from(kinds.entries()).map(([k, n]) => `${fmtInt(n)} ${k}`).join(", "),
      priced.reduce((s, r) => s + numOrZero(r.est_usd_list), 0) * net, priced.reduce((s, r) => s + numOrZero(r.est_wasted_usd_list), 0) * net, priced.length > 0);
  }).filter((b): b is BandRow => !!b);
  const noSetting = rows.filter((r) => r.auto_stop_minutes == null).length;
  return (
    <Card title="Warehouses by auto-stop setting" right={<span className="muted">{`idle cost of total cost, ${windowDays}d`}</span>}>
      <div className="cj-card-subtitle">
        <span className="cj-legend-sw"><span className="sw" style={{ background: "var(--st-crit)" }}></span>Idle cost</span>
        <span className="cj-legend-sw"><span className="sw" style={{ background: "var(--border-strong)" }}></span>Rest of the cost</span>
        <span>Recommended: serverless 5 min or lower, pro and classic 10 min.</span>
      </div>
      <BandRows bands={bands} />
      {noSetting > 0 && <div className="cj-band-note">{`${fmtInt(noSetting)} warehouse${noSetting === 1 ? " has" : "s have"} no auto-stop setting in this export.`}</div>}
    </Card>
  );
}

// All-purpose cluster auto-termination bands; Databricks' lowest setting is 10 min. Off shows only when a cluster has it.
const AUTOTERM_BANDS: { label: string; test: (m: number) => boolean }[] = [
  { label: "Up to 15 min", test: (m) => m > 0 && m <= 15 },
  { label: "16–30 min", test: (m) => m > 15 && m <= 30 },
  { label: "31–60 min", test: (m) => m > 30 && m <= 60 },
  { label: "61–120 min", test: (m) => m > 60 && m <= 120 },
  { label: "Over 120 min", test: (m) => m > 120 },
  { label: "Off", test: (m) => m === 0 },
];
// Cluster sources that are jobs, pipelines or Databricks-run compute; the rest are all-purpose.
const NOT_ALL_PURPOSE = new Set(["JOB", "PIPELINE", "PIPELINE_MAINTENANCE", "SQL", "MODELS"]);

// All-purpose clusters by auto-termination: how many, how many ran, their cost and the idle-node part of it.
function AutotermBandsCard({ cfgAgg, idleRows, disc, windowDays }: { cfgAgg: AggState; idleRows: Row[] | null; disc: number; windowDays: number }) {
  if (cfgAgg.phase !== "ready" || cfgAgg.outcome !== "ok_rows") {
    return <Card title="All-purpose clusters by auto-termination"><ChartNote state={cfgAgg} label="classic_clusters_config_current" /></Card>;
  }
  const net = 1 - disc;
  const termOf = new Map<string, number>();
  (cfgAgg.data.groups || []).forEach((g) => {
    if (g.key[0] == null || NOT_ALL_PURPOSE.has(String(g.key[0])) || g.key[2] == null) return;
    termOf.set(String(g.key[1]), numOrZero(g.key[2]));
  });
  const idleBy = new Map((idleRows || []).map((r) => [String(r.cluster_id), r]));
  const bands = AUTOTERM_BANDS.map((b) => {
    const ids = Array.from(termOf.entries()).filter(([, m]) => b.test(m)).map(([id]) => id);
    if (b.label === "Off" && !ids.length) return null;
    const ran = ids.map((id) => idleBy.get(id)).filter((r): r is Row => !!r && r.est_usd_list != null);
    return costBand(b.label, ids.length, "cluster", ids.length ? `${fmtInt(ran.length)} ran` : "",
      ran.reduce((s, r) => s + numOrZero(r.est_usd_list), 0) * net, ran.reduce((s, r) => s + numOrZero(r.est_wasted_usd_list), 0) * net, ran.length > 0);
  }).filter((b): b is BandRow => !!b);
  return (
    <Card title="All-purpose clusters by auto-termination" right={<span className="muted">{`idle cost of total cost, ${windowDays}d`}</span>}>
      <div className="cj-card-subtitle">
        <span className="cj-legend-sw"><span className="sw" style={{ background: "var(--st-crit)" }}></span>Idle cost (idle nodes)</span>
        <span className="cj-legend-sw"><span className="sw" style={{ background: "var(--border-strong)" }}></span>Rest of the cost</span>
        <span>Cost only for clusters that ran in the window.</span>
      </div>
      <BandRows bands={bands} />
    </Card>
  );
}

// Days the selected window really holds: a 62-day export fills the 90-day window with 62.
function windowDaysHeld(filters: any, meta: any): number {
  return Math.min(filters.window, numOrZero(meta && meta.snapshot_days) || filters.window);
}
const UNUSED_LIST_MAX = 50;

const LONG_AUTOTERM_MIN = 30;

// All-purpose clusters that ran with auto-termination over 30 min or off, most idle cost first.
function LongAutotermCard({ cfgAgg, idleState, dims }: { cfgAgg: AggState; idleState: FindingState; dims: DimMaps | null }) {
  const title = `All-purpose clusters that ran, auto-termination over ${LONG_AUTOTERM_MIN} min`;
  const cfgReady = cfgAgg.phase === "ready" && cfgAgg.outcome === "ok_rows";
  const idleRows = readyRows(idleState);
  if (!cfgReady || !idleRows) {
    return <Card title={title}><ChartNote state={cfgReady ? idleState : cfgAgg} label={cfgReady ? "compute_idle_node_ratio" : "classic_clusters_config_current"} /></Card>;
  }
  const net = 1 - (idleState.data ? numOrZero(idleState.data.discount_pct) : 0);
  const termOf = new Map<string, number>();
  (cfgAgg.data!.groups || []).forEach((g) => {
    if (g.key[0] == null || NOT_ALL_PURPOSE.has(String(g.key[0])) || g.key[2] == null) return;
    termOf.set(String(g.key[1]), numOrZero(g.key[2]));
  });
  const ran = idleRows.filter((r) => termOf.has(String(r.cluster_id)));
  const long = ran.filter((r) => { const m = termOf.get(String(r.cluster_id))!; return m === 0 || m > LONG_AUTOTERM_MIN; }).map((r) => {
    const id = String(r.cluster_id);
    const c = dims && dims.clusters ? dims.clusters.get(id) : null;
    return {
      id, term: termOf.get(id)!, name: clusterName(dims, id), ws: (c && c.workspace_id) || r.workspace_id, owner: c ? c.owned_by : null,
      idle: r.est_wasted_usd_list != null ? numOrZero(r.est_wasted_usd_list) * net : null,
      total: r.est_usd_list != null ? numOrZero(r.est_usd_list) * net : null,
      ratio: r.idle_ratio != null ? numOrZero(r.idle_ratio) : null,
    };
  }).sort((a, b) => (b.idle ?? -1) - (a.idle ?? -1) || (b.term || Infinity) - (a.term || Infinity));
  const idleSum = long.reduce((s, r) => s + (r.idle || 0), 0);
  const totalSum = long.reduce((s, r) => s + (r.total || 0), 0);
  return (
    <Card title={title} right={<span className="muted">{`${fmtInt(long.length)} of ${fmtInt(ran.length)} all-purpose clusters that ran`}</span>}>
      <div className="cj-card-subtitle">
        <span>{long.length ? `${fmtMoney(idleSum, 0)} idle of ${fmtMoney(totalSum, 0)} on these. A shorter auto-termination cuts the idle time after the last command: set it on the cluster, or cap it in its policy.` : ""}</span>
      </div>
      {long.length === 0 ? <div className="muted">{`No all-purpose cluster that ran has auto-termination over ${LONG_AUTOTERM_MIN} min.`}</div> : (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>Cluster</th><th>Workspace</th><th>Owner</th><th className="num">Auto-termination</th><th className="num">Idle</th><th className="num">Idle $</th><th className="num">Total $</th></tr></thead>
            <tbody>
              {long.slice(0, UNUSED_LIST_MAX).map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{r.ws ? resolveName("workspace", r.ws, r.ws) : "-"}</td>
                  <td className="muted">{r.owner || "-"}</td>
                  <td className="num mono">{r.term === 0 ? "off" : `${fmtInt(r.term)} min`}</td>
                  <td className="num mono">{r.ratio == null ? "-" : fmtPct(r.ratio * 100, 0)}</td>
                  <td className="num mono">{r.idle == null ? "-" : fmtMoney(r.idle, 0)}</td>
                  <td className="num mono">{r.total == null ? "-" : fmtMoney(r.total, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {long.length > UNUSED_LIST_MAX && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(long.length - UNUSED_LIST_MAX)} more not listed.`}</div>}
    </Card>
  );
}


// Each warehouse's starts (perf_daily_by_resource) beside its stops and scale-ups
// (sql_warehouse_events_activity) and running time (compute_warehouse_idle_minutes).
const STARTS_LIST_MAX = 15;

function perDayText(n: number): string { return n >= 10 ? fmtInt(n) : n.toFixed(1); }

function StartsStopsCard({ startup, eventsRows, idleRows, cfgRows, days, dims }: {
  startup: { byWh: Map<string, Startup>; total: Startup } | null; eventsRows: Row[] | null; idleRows: Row[] | null;
  cfgRows: Row[] | null; days: number; dims: DimMaps | null;
}) {
  if (!startup || !eventsRows) return null;
  const key = (r: Row) => `${r.workspace_id}:${r.warehouse_id}`;
  const ev = new Map<string, { stops: number; ups: number; peak: number }>();
  eventsRows.forEach((r) => {
    if (!r.event_type) return;
    const e = ev.get(key(r)) || { stops: 0, ups: 0, peak: 0 };
    if (r.event_type === "STOPPED") e.stops += numOrZero(r.event_count);
    if (r.event_type === "SCALED_UP") e.ups += numOrZero(r.event_count);
    e.peak = Math.max(e.peak, numOrZero(r.max_cluster_count));
    ev.set(key(r), e);
  });
  const idleBy = new Map((idleRows || []).map((r) => [key(r), r] as [string, Row]));
  const cfgBy = new Map((cfgRows || []).map((r) => [key(r), r] as [string, Row]));
  const keys = new Set<string>();
  startup.byWh.forEach((x, k) => { if (x.starts > 0) keys.add(k); });
  ev.forEach((e, k) => { if (e.stops > 0) keys.add(k); });
  const none: Startup = { clock: 0, starts: 0, startS: 0, longest: 0, failed: 0 };
  const list = [...keys].map((k) => {
    const idle = idleBy.get(k) || null;
    const cfg = cfgBy.get(k) || null;
    const wh = k.slice(k.indexOf(":") + 1);
    return {
      k, wh, idle, cfg, s: startup.byWh.get(k) || none, e: ev.get(k) || { stops: 0, ups: 0, peak: 0 },
      name: String((cfg && cfg.warehouse_name) || warehouseName(dims, wh)),
      kind: String((idle && idle.warehouse_kind) || (cfg && cfg.warehouse_type) || "").toLowerCase(),
      autoStop: idle && idle.auto_stop_minutes != null ? idle.auto_stop_minutes : cfg ? cfg.auto_stop_minutes : null,
    };
  }).sort((a, b) => b.s.starts - a.s.starts || b.e.stops - a.e.stops);
  const t = startup.total;
  const d = Math.max(1, days);
  const worst = list.length && list[0].s.starts > 0 ? list[0] : null;
  const fix = worst && worst.kind === "serverless"
    ? "serverless starts in seconds, so frequent starts cost little"
    : "a longer auto-stop means fewer starts and shorter waits for queries";
  const title = `Starts and stops, last ${fmtInt(days)} days`;
  return (
    <Card title={title} right={<span className="muted">{`${fmtInt(list.length)} warehouse${list.length === 1 ? "" : "s"}, most starts first`}</span>}>
      <div className="cj-card-subtitle"><span>
        {t.starts
          ? `Warehouses started ${fmtInt(t.starts)} times (${perDayText(t.starts / d)} a day) and spent ${fmtDuration(t.startS)} starting`
            + (t.failed ? `; ${fmtInt(t.failed)} start${t.failed === 1 ? "" : "s"} stopped before running` : "") + "."
            + (worst ? ` Most starts: ${worst.name}, ${perDayText(worst.s.starts / d)} a day`
              + (worst.autoStop ? ` with auto-stop at ${fmtInt(worst.autoStop)} min` : "") + `; ${fix}.` : "")
          : `No warehouse start recorded in the last ${fmtInt(days)} days.`}
      </span></div>
      {list.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <thead><tr>
              <th>Warehouse</th><th className="num">Starts</th><th className="num">A day</th><th className="num">Start, average / longest</th>
              <th className="num">Stops</th><th className="num">Scale-ups</th><th className="num">Running</th><th className="num">Queries waited, by the clock</th>
            </tr></thead>
            <tbody>
              {list.slice(0, STARTS_LIST_MAX).map((r) => {
                const running = r.idle ? numOrZero(r.idle.running_minutes) : 0;
                return (
                  <tr key={r.k}>
                    <td>
                      <div>{r.name}</div>
                      <div className="muted">{[resolveName("workspace", r.k.split(":")[0], r.k.split(":")[0]), r.kind,
                        r.autoStop != null ? (r.autoStop ? `auto-stop ${fmtInt(r.autoStop)} min` : "auto-stop off") : null].filter(Boolean).join(" · ")}</div>
                    </td>
                    <td className="num mono">{r.s.starts ? `${fmtInt(r.s.starts)}${r.s.failed ? ` (${fmtInt(r.s.failed)} failed)` : ""}` : "-"}</td>
                    <td className="num mono">{r.s.starts ? perDayText(r.s.starts / d) : "-"}</td>
                    <td className="num mono">{r.s.starts ? `${fmtDuration(r.s.startS / r.s.starts)} / ${fmtDuration(r.s.longest)}` : "-"}</td>
                    <td className="num mono">{r.e.stops ? fmtInt(r.e.stops) : "-"}</td>
                    <td className="num mono">{r.e.ups ? `${fmtInt(r.e.ups)} (peak ${fmtInt(r.e.peak)})` : "-"}</td>
                    <td className="num mono">{running > 0 ? `${fmtInt(Math.round(running / 60))} h, ${fmtPct((numOrZero(r.idle && r.idle.counted_idle_minutes) / running) * 100, 0)} idle` : "-"}</td>
                    <td className="num mono">{r.s.clock ? fmtDuration(r.s.clock) : "-"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {list.length > STARTS_LIST_MAX && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(list.length - STARTS_LIST_MAX)} more not listed.`}</div>}
    </Card>
  );
}

// Warehouses that are not deleted and had no start or stop in the window: candidates to remove.
function UnusedWarehousesCard({ cfgRows, eventsState, days, dims }: { cfgRows: Row[] | null; eventsState: FindingState; days: number; dims: DimMaps | null }) {
  const title = `Warehouses with no activity in the last ${fmtInt(days)} days`;
  const events = readyRows(eventsState);
  if (!cfgRows || !events) return <Card title={title}><ChartNote state={eventsState} label="sql_warehouse_events_activity" /></Card>;
  const active = new Set(events.map((r) => String(r.warehouse_id)));
  const live = cfgRows.filter((r) => r.delete_time == null);
  const wsName = (r: Row) => String(resolveName("workspace", r.workspace_id, r.workspace_id) || r.workspace_id);
  const unused = live.filter((r) => !active.has(String(r.warehouse_id))).sort((a, b) => wsName(a).localeCompare(wsName(b)));
  return (
    <Card title={title} right={<span className="muted">{`${fmtInt(unused.length)} of ${fmtInt(live.length)} warehouses`}</span>}>
      <div className="cj-card-subtitle"><span>Not started once in the window, so no queries either. A stopped warehouse costs nothing; removing it cuts clutter and access.</span></div>
      {unused.length === 0 ? <div className="muted">{`Every warehouse started at least once in the last ${fmtInt(days)} days.`}</div> : (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>Warehouse</th><th>Workspace</th><th>Type</th><th>Size</th><th className="num">Auto-stop</th><th>Created by</th></tr></thead>
            <tbody>
              {unused.slice(0, UNUSED_LIST_MAX).map((r) => {
                const dim = dims && dims.warehouses ? dims.warehouses.get(r.warehouse_id) : null;
                return (
                  <tr key={r.warehouse_id}>
                    <td>{r.warehouse_name || warehouseName(dims, r.warehouse_id)}</td>
                    <td>{wsName(r)}</td>
                    <td>{String(r.warehouse_type || "").toLowerCase()}</td>
                    <td>{r.warehouse_size || "-"}</td>
                    <td className="num mono">{r.auto_stop_minutes ? `${fmtInt(r.auto_stop_minutes)} min` : "off"}</td>
                    <td className="muted">{(dim && dim.created_by) || "-"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {unused.length > UNUSED_LIST_MAX && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(unused.length - UNUSED_LIST_MAX)} more not listed.`}</div>}
    </Card>
  );
}

// All-purpose clusters with no node running in the window: candidates to remove.
function UnusedClustersCard({ cfgAgg, usedAgg, days, dims }: { cfgAgg: AggState; usedAgg: AggState; days: number; dims: DimMaps | null }) {
  const title = `All-purpose clusters with no activity in the last ${fmtInt(days)} days`;
  const cfgReady = cfgAgg.phase === "ready" && cfgAgg.outcome === "ok_rows";
  const usedReady = usedAgg.phase === "ready" && String(usedAgg.outcome).startsWith("ok_");
  if (!cfgReady || !usedReady) {
    return <Card title={title}><ChartNote state={cfgReady ? usedAgg : cfgAgg} label={cfgReady ? "compute_idle_node_ratio" : "classic_clusters_config_current"} /></Card>;
  }
  const used = new Set(((usedAgg.data && usedAgg.data.groups) || []).map((g) => String(g.key[0])));
  const termOf = new Map<string, number | null>();
  (cfgAgg.data!.groups || []).forEach((g) => {
    if (g.key[0] == null || NOT_ALL_PURPOSE.has(String(g.key[0]))) return;
    termOf.set(String(g.key[1]), g.key[2] == null ? null : numOrZero(g.key[2]));
  });
  const unused = Array.from(termOf.entries()).filter(([id]) => !used.has(id)).map(([id, term]) => {
    const c = dims && dims.clusters ? dims.clusters.get(id) : null;
    return { id, term, name: clusterName(dims, id), ws: c ? c.workspace_id : null, owner: c ? c.owned_by : null };
  }).sort((a, b) => String(a.name).localeCompare(String(b.name)));
  return (
    <Card title={title} right={<span className="muted">{`${fmtInt(unused.length)} of ${fmtInt(termOf.size)} all-purpose clusters`}</span>}>
      <div className="cj-card-subtitle"><span>No node ran in the window. A terminated cluster costs nothing; removing it cuts clutter and access.</span></div>
      {unused.length === 0 ? <div className="muted">{`Every all-purpose cluster ran at least once in the last ${fmtInt(days)} days.`}</div> : (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>Cluster</th><th>Workspace</th><th>Owner</th><th className="num">Auto-termination</th></tr></thead>
            <tbody>
              {unused.slice(0, UNUSED_LIST_MAX).map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{r.ws ? resolveName("workspace", r.ws, r.ws) : "-"}</td>
                  <td className="muted">{r.owner || "-"}</td>
                  <td className="num mono">{r.term == null ? "-" : r.term === 0 ? "off" : `${fmtInt(r.term)} min`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {unused.length > UNUSED_LIST_MAX && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(unused.length - UNUSED_LIST_MAX)} more not listed.`}</div>}
    </Card>
  );
}

function AutoscaleChurnCard({ rows, state, maxCat, band, dims }: { rows: Row[] | null; state: FindingState; maxCat: number; band: Band | null; dims: DimMaps | null }) {
  const ranked = rows ? [...rows].sort((a, b) => numOrZero(b.churn_events) - numOrZero(a.churn_events)).slice(0, maxCat || 6) : null;
  const maxEvents = ranked ? Math.max(...ranked.map((r) => numOrZero(r.churn_events)), 1) : 1;
  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">Autoscale churn</span>
        {band && <StatusPill kind={bandPillKind(band)} />}
      </div>
      <p className="cj-chart-takeaway" style={{ marginTop: -4 }}>{getCheckLabel("compute_warehouse_autoscale_churn").why}</p>
      {ranked ? ranked.map((r) => (
        <div className="cj-churn-row" key={`${r.workspace_id}:${r.warehouse_id}`}>
          <span className="cj-churn-name" title={warehouseName(dims, r.warehouse_id)}>{warehouseName(dims, r.warehouse_id)}</span>
          <div className="cj-churn-track"><div className="cj-churn-fill" style={{ width: `${(numOrZero(r.churn_events) / maxEvents) * 100}%` }}></div></div>
          <span className="cj-churn-value">{`${fmtInt(r.churn_events)} · ${(numOrZero(r.churn_events) / Math.max(numOrZero(r.observed_hours), 0.01)).toFixed(1)}/h`}</span>
        </div>
      )) : <ChartNote state={state} label="compute_warehouse_autoscale_churn" />}
      <div className="cj-caveat">
        <StatusIcon kind="not_assessed" size={14} />
        <span>Known limitation: this also counts ordinary starts and stops, so it overstates real scaling. Read it as how often the warehouse changed state, not as thrashing.</span>
      </div>
    </div>
  );
}

function WarehouseTable({ rows, sizeByWarehouse, pressureByWarehouse, disc, windowDays, dims, floor }: {
  rows: Row[] | null; sizeByWarehouse: Map<string, string> | null; pressureByWarehouse: Map<string, Row> | null; disc: number;
  windowDays: number; dims: DimMaps | null; floor: Floor | null;
}) {
  if (!rows || !rows.length) return null;
  const ranked = [...rows].sort((a, b) => numOrZero(b.est_wasted_usd_list) - numOrZero(a.est_wasted_usd_list));
  return (
    <Card title="Idle time by warehouse" right={<span className="muted">{`${ranked.length} warehouse${ranked.length === 1 ? "" : "s"} · sorted by idle $`}</span>}>
      <div className="cj-wh-table">
        <div className="cj-wh-head">
          <span>Warehouse</span><span style={{ textAlign: "right" }}>Auto-stop</span><span>Idle share of running</span>
          <span style={{ textAlign: "right" }}>Idle $</span><span>Status</span><span>The fix</span>
        </div>
        {ranked.map((r) => {
          const band = r.status === "CRITICAL" ? "CRITICAL" : r.status === "WARN" ? "WARN" : r.status === "NOT_ASSESSED" ? "NOT_ASSESSED" : "OK";
          const size = (sizeByWarehouse && sizeByWarehouse.get(r.warehouse_id)) || null;
          const tailDominant = numOrZero(r.stop_tail_minutes) >= numOrZero(r.counted_idle_minutes) * 0.5 && numOrZero(r.counted_idle_minutes) > 0;
          const pressureRow = pressureByWarehouse ? pressureByWarehouse.get(r.warehouse_id) : null;
          const changeLines = warehouseChangeLines(r, pressureRow, disc, windowDays);
          return (
            <div className="cj-wh-row" key={`${r.workspace_id}:${r.warehouse_id}`}>
              <div>
                <div className="cj-wh-name">{warehouseName(dims, r.warehouse_id)}</div>
                <div className="cj-wh-meta">{[resolveName("workspace", r.workspace_id, r.workspace_id), size, r.warehouse_kind].filter(Boolean).join(" · ")}</div>
              </div>
              <span className={`cj-wh-autostop ${tailDominant ? "crit" : ""}`}>{r.auto_stop_minutes != null ? `${fmtInt(r.auto_stop_minutes)} min` : "off"}</span>
              <div className="cj-wh-idle-row">
                <div className="cj-wh-idle-track"><div className="cj-wh-idle-fill" style={{ width: `${Math.min(100, numOrZero(r.idle_share_pct))}%` }}></div></div>
                <span className="cj-wh-idle-pct">{fmtPct(r.idle_share_pct, 0)}</span>
              </div>
              <span className="cj-wh-usd">{r.est_wasted_usd_list != null ? fmtMoney(r.est_wasted_usd_list * (1 - disc), 0) : "-"}</span>
              <span>{isUnderFloor(r) ? <RowStatusPill row={r} floor={floor} /> : <StatusPill kind={bandPillKind(band)} />}</span>
              <span className="cj-wh-fix">
                {isUnderFloor(r)
                  ? <span className="muted">{`Too small to act on: under ${floor && floor.label ? floor.label : "the floor"}.`}</span>
                  : changeLines.length
                  ? changeLines.map((line, i) => <div key={i}>{line}</div>)
                  : warehouseFixText(r)}
              </span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ─────────── Auto-stop what-if (compute_warehouse_idle_minutes' autostop_N_* columns) ───────────
// The window's real gaps between queries replayed at a shorter auto-stop: idle $ saved vs extra
// cold starts. Serverless can go to 1 or 2 min (API) or 5 min (UI); pro and classic stop at 10 min.
const AUTOSTOP_OPTIONS: Record<string, number[]> = { serverless: [1, 2, 5], pro: [10], classic: [10] };

/** What one shorter auto-stop setting would have saved, and the cold starts it adds. */
interface AutostopOption {
  minutes: number;
  usd: number;
  starts: number;
}

function autostopOption(r: Row, minutes: number, disc: number): AutostopOption | null {
  if (r[`autostop_${minutes}_idle_minutes`] == null) return null;
  const now = numOrZero(r.autostop_now_idle_minutes);
  const at = numOrZero(r[`autostop_${minutes}_idle_minutes`]);
  const usd = Math.max(now - at, 0) * numOrZero(r.usd_per_cluster_minute) * (1 - disc);
  const starts = Math.max(numOrZero(r[`autostop_${minutes}_cold_starts`]) - numOrZero(r.autostop_now_cold_starts), 0);
  return { minutes, usd, starts };
}

function AutostopWhatIfPanel({ rows, churnRows, disc, maxCat, dims }: { rows: Row[] | null; churnRows: Row[] | null; disc: number; maxCat: number; dims: DimMaps | null }) {
  if (!rows || !rows.some((r) => r.autostop_now_idle_minutes != null)) return null;
  const stopsBy = new Map((churnRows || []).map((c) => [c.warehouse_id, c.total_autostop_count]));
  const items = rows
    .filter((r) => r.autostop_now_idle_minutes != null && r.usd_per_cluster_minute != null && AUTOSTOP_OPTIONS[r.warehouse_kind])
    .map((r): { r: Row; options: AutostopOption[] } => ({ r, options: AUTOSTOP_OPTIONS[r.warehouse_kind].filter((m) => m < numOrZero(r.auto_stop_minutes)).map((m) => autostopOption(r, m, disc)).filter((o): o is AutostopOption => !!o) }))
    .filter((it) => it.options.some((o) => o.usd >= 1))
    .sort((a, b) => Math.max(...b.options.map((o) => o.usd)) - Math.max(...a.options.map((o) => o.usd)));
  if (!items.length) return null;
  const optionAt = (it: (typeof items)[number], m: number) => it.options.find((o) => o.minutes === m);
  const total = (m: number) => items.reduce((s, it) => { const o = optionAt(it, m); return s + ((o && o.usd) || 0); }, 0);
  // Without a list price there is nothing to replay in dollars; say how many were left out.
  const unpriced = rows.filter((r) => r.autostop_now_idle_minutes != null && r.usd_per_cluster_minute == null).length;
  const totalStarts = (m: number) => items.reduce((s, it) => { const o = optionAt(it, m); return s + ((o && o.starts) || 0); }, 0);
  const hasTwo = items.some((it) => optionAt(it, 2));
  const cell = (it: (typeof items)[number], m: number) => {
    const o = optionAt(it, m);
    return o ? <span><b>{fmtMoney(o.usd, 0)}</b><span className="muted">{` · +${fmtInt(o.starts)} starts`}</span></span> : <span className="muted">-</span>;
  };
  return (
    <Card title="Auto-stop: what a shorter setting saves" right={<span className="muted">this window's real gaps between queries, replayed</span>}>
      <Facts items={[
        { label: "Serverless at 1 min", value: fmtMoney(total(1), 0), tone: "money", detail: `+${fmtInt(totalStarts(1))} cold starts, seconds each; set through the API` },
        hasTwo ? { label: "Serverless at 2 min", value: fmtMoney(total(2), 0), tone: "money", detail: `+${fmtInt(totalStarts(2))} cold starts; set through the API` } : null,
        { label: "Serverless at 5 min", value: fmtMoney(total(5), 0), tone: "money", detail: `+${fmtInt(totalStarts(5))} cold starts; the lowest the UI allows` },
        { label: "Pro / classic at 10 min", value: fmtMoney(total(10), 0), tone: "money", detail: `+${fmtInt(totalStarts(10))} cold starts, minutes each` },
      ]} />
      <div className="table-wrap" style={{ marginTop: 10 }}>
        <table className="data">
          <thead>
            <tr><th>Warehouse</th><th>Type</th><th className="num">Auto-stop now</th><th className="num">Stopped for inactivity</th><th>At 1 min</th>{hasTwo && <th>At 2 min</th>}<th>At 5 min</th><th>At 10 min</th></tr>
          </thead>
          <tbody>
            {items.slice(0, maxCat || 8).map((it) => (
              <tr key={`${it.r.workspace_id}:${it.r.warehouse_id}`}>
                <td>{warehouseName(dims, it.r.warehouse_id)}<div className="muted">{resolveName("workspace", it.r.workspace_id, it.r.workspace_id)}</div></td>
                <td>{it.r.warehouse_kind}</td>
                <td className="num mono">{`${fmtInt(it.r.auto_stop_minutes)} min`}</td>
                <td className="num mono">{stopsBy.has(it.r.warehouse_id) ? `${fmtInt(stopsBy.get(it.r.warehouse_id))}x` : "-"}</td>
                <td>{cell(it, 1)}</td>{hasTwo && <td>{cell(it, 2)}</td>}<td>{cell(it, 5)}</td><td>{cell(it, 10)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.length > (maxCat || 8) && <div className="muted" style={{ marginTop: 6 }}>{`Top ${fmtInt(maxCat || 8)} of ${fmtInt(items.length)} warehouses by saving.`}</div>}
      {unpriced > 0 && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(unpriced)} warehouse${unpriced === 1 ? "" : "s"} not replayed: no list price in the window.`}</div>}
    </Card>
  );
}

// ─────────── Auto-stop churn (compute_warehouse_autostop_churn) ───────────
function AutostopChurnPanel({ rows, state, maxCat, dims }: { rows: Row[] | null; state: FindingState; maxCat: number; dims: DimMaps | null }) {
  const flagged = rows ? rows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const ranked = flagged ? [...flagged].sort((a, b) => {
    const rank = (s: string) => (s === "CRITICAL" ? 0 : s === "WARN" ? 1 : 2);
    if (rank(a.status) !== rank(b.status)) return rank(a.status) - rank(b.status);
    return numOrZero(b.total_est_wasted_usd_list) - numOrZero(a.total_est_wasted_usd_list);
  }) : null;
  const shown = ranked ? ranked.slice(0, maxCat || 8) : null;
  const coldStartDays = rows ? sumBy(rows, "cold_start_risk_days") : 0;
  return (
    <Card title="Auto-stop churn" right={flagged && flagged.length ? <span className="muted">{`${fmtInt(flagged.length)} flagged, worst first`}</span> : null}>
      <p className="cj-chart-takeaway" style={{ marginTop: -4 }}>
        Warehouses that auto-stop many times a day -- each restart pays a fresh start-up cost. Lower auto-stop where there is room to; one already at its type's floor gets no cheaper from auto-stop alone -- consider serverless there instead.
      </p>
      {rows && flagged && shown ? (
        flagged.length ? (
          <React.Fragment>
            <div className="cj-asc-table">
              <div className="cj-asc-head">
                <span>Warehouse</span><span style={{ textAlign: "right" }}>Auto-stop</span>
                <span>Worst day</span><span style={{ textAlign: "right" }}>Autostops, window</span>
                <span style={{ textAlign: "right" }}>Est. $, list</span><span>Status</span>
              </div>
              {shown.map((r) => {
                const suggested = suggestedAutoStopMinutes(r.warehouse_kind);
                const canLower = r.auto_stop_minutes != null && suggested != null && r.auto_stop_minutes > suggested;
                const atFloor = r.auto_stop_minutes != null && suggested != null && r.auto_stop_minutes <= suggested && r.warehouse_kind !== "serverless";
                return (
                  <div className="cj-asc-row" key={`${r.workspace_id}:${r.warehouse_id}`}>
                    <div>
                      <div className="cj-asc-name">{warehouseName(dims, r.warehouse_id)}</div>
                      <div className="cj-asc-meta">{[resolveName("workspace", r.workspace_id, r.workspace_id), r.warehouse_kind].filter(Boolean).join(" · ")}</div>
                    </div>
                    <span className="cj-asc-num">
                      {r.auto_stop_minutes != null ? `${fmtInt(r.auto_stop_minutes)} min` : "off"}
                      {canLower ? ` → ${fmtInt(suggested)} min` : atFloor ? " · already at floor, try serverless" : ""}
                    </span>
                    <span className="cj-asc-meta">{fmtDayShort(r.worst_day)} · {fmtInt(r.worst_day_autostop_count)} stops</span>
                    <span className="cj-asc-num">{fmtInt(r.total_autostop_count)}</span>
                    <span className="cj-asc-usd">{r.total_est_wasted_usd_list != null ? fmtMoney(r.total_est_wasted_usd_list, 0) : "-"}</span>
                    <span><StatusPill kind={bandPillKind(r.status)} /></span>
                  </div>
                );
              })}
            </div>
            {flagged.length > shown.length && (
              <div className="cj-chart-takeaway">{`${fmtInt(flagged.length - shown.length)} more flagged warehouse${flagged.length - shown.length === 1 ? "" : "s"} not shown -- see Checks below.`}</div>
            )}
          </React.Fragment>
        ) : <span className="muted">{`Nothing flagged among ${fmtInt(rows.length)} warehouses checked.`}</span>
      ) : <ChartNote state={state} label="compute_warehouse_autostop_churn" />}
      {rows && coldStartDays > 0 && (
        <div className="cj-caveat">
          <StatusIcon kind="not_assessed" size={14} />
          <span>{`${fmtInt(coldStartDays)} warehouse-day${coldStartDays === 1 ? "" : "s"} also show a classic/pro warehouse restarting often (cold-start risk) -- informational, not counted in the status above.`}</span>
        </div>
      )}
    </Card>
  );
}

// ─────────── Cache reuse (compute_warehouse_cache_reuse) ───────────
function CacheReusePanel({ rows, state, maxCat, dims }: { rows: Row[] | null; state: FindingState; maxCat: number; dims: DimMaps | null }) {
  // Never CRITICAL by the query's own design (a low cache-reuse rate is a heuristic, not a
  // confirmed problem) -- WARN rows are the whole flagged set here.
  const flagged = rows ? rows.filter((r) => r.status === "WARN") : null;
  const ranked = flagged ? [...flagged].sort((a, b) => numOrZero(a.from_result_cache_pct) - numOrZero(b.from_result_cache_pct)) : null;
  const shown = ranked ? ranked.slice(0, maxCat || 8) : null;
  // High repeated_statement_pct next to a low from_result_cache_pct means the SAME queries keep
  // running but keep missing the cache -- a config/workload problem to go look at, not the "rarely
  // gets a repeat benefit" reading that fits a genuinely one-off workload.
  const highRepeat = flagged && flagged.some((r) => numOrZero(r.repeated_statement_pct) >= 50);
  return (
    <Card title="Cache reuse" right={flagged && flagged.length ? <span className="muted">{`${fmtInt(flagged.length)} flagged, lowest reuse first`}</span> : null}>
      <p className="cj-chart-takeaway" style={{ marginTop: -4 }}>
        {highRepeat
          ? "Mostly-repeated statements with almost no cache hit -- check for a changing filter or timestamp in otherwise identical queries."
          : "Warehouses kept warm for a repeat-query benefit they rarely get -- a shorter auto-stop would cost them little."}
      </p>
      {rows && flagged && shown ? (
        flagged.length ? (
          <React.Fragment>
            <div className="cj-cr-table">
              <div className="cj-cr-head">
                <span>Warehouse</span><span style={{ textAlign: "right" }}>Auto-stop</span>
                <span>Result-cache reuse</span><span style={{ textAlign: "right" }}>Repeat statements</span>
                <span style={{ textAlign: "right" }}>Median gap</span><span>Status</span>
              </div>
              {shown.map((r) => (
                <div className="cj-cr-row" key={r.warehouse_id}>
                  <div>
                    <div className="cj-asc-name">{warehouseName(dims, r.warehouse_id)}</div>
                    <div className="cj-asc-meta">{[r.warehouse_kind, `${fmtInt(r.queries)} queries`].filter(Boolean).join(" · ")}</div>
                  </div>
                  <span className="cj-asc-num">{r.auto_stop_minutes != null ? `${fmtInt(r.auto_stop_minutes)} min` : "off"}</span>
                  <div className="cj-wh-idle-row">
                    <div className="cj-wh-idle-track"><div className="cj-wh-idle-fill" style={{ width: `${Math.min(100, numOrZero(r.from_result_cache_pct))}%` }}></div></div>
                    <span className="cj-wh-idle-pct">{fmtPct(r.from_result_cache_pct, 0)}</span>
                  </div>
                  <span className="cj-asc-num">{fmtPct(r.repeated_statement_pct, 0)}</span>
                  <span className="cj-asc-num">{r.median_gap_minutes != null ? `${fmtInt(Math.round(r.median_gap_minutes))} min` : "-"}</span>
                  <span><StatusPill kind={bandPillKind(r.status)} /></span>
                </div>
              ))}
            </div>
            {flagged.length > shown.length && (
              <div className="cj-chart-takeaway">{`${fmtInt(flagged.length - shown.length)} more flagged warehouse${flagged.length - shown.length === 1 ? "" : "s"} not shown -- see Checks below.`}</div>
            )}
          </React.Fragment>
        ) : <span className="muted">{`Nothing flagged among ${fmtInt(rows.length)} warehouses checked.`}</span>
      ) : <ChartNote state={state} label="compute_warehouse_cache_reuse" />}
    </Card>
  );
}

function ComputeWarehousesContent({ findings, filters, dims, maxCat, goTo, onVerdict, meta }: TabProps) {
  const idleState = useFindingData("compute_warehouse_idle_minutes", filters.window, filters.workspaceIds, filters.envs);
  const gapsState = useFindingData("compute_warehouse_idle_gaps", filters.window, filters.workspaceIds, filters.envs);
  const churnState = useFindingData("compute_warehouse_autoscale_churn", filters.window, filters.workspaceIds, filters.envs);
  const cfgState = useFindingData("sql_warehouse_config_current", filters.window, filters.workspaceIds, filters.envs);
  const eventsState = useFindingData("sql_warehouse_events_activity", filters.window, filters.workspaceIds, filters.envs);
  const autostopChurnState = useFindingData("compute_warehouse_autostop_churn", filters.window, filters.workspaceIds, filters.envs);
  const cacheReuseState = useFindingData("compute_warehouse_cache_reuse", filters.window, filters.workspaceIds, filters.envs);
  const pressureState = useFindingData("query_warehouse_pressure", filters.window, filters.workspaceIds, filters.envs);

  const idleRows = readyRows(idleState);
  const validRows = idleRows ? idleRows.filter((r) => !r.not_assessed_reason) : null;
  const gapsRows = readyRows(gapsState);
  const churnRows = readyRows(churnState);
  const cfgRows = readyRows(cfgState);
  const autostopChurnRows = readyRows(autostopChurnState);
  const cacheReuseRows = readyRows(cacheReuseState);
  const pressureRows = readyRows(pressureState);
  const eventsRows = readyRows(eventsState);
  const startup = useStartupWaits(filters.window, filters.workspaceIds, filters.envs);

  const sizeByWarehouse = React.useMemo(() => {
    if (!cfgRows) return null;
    const m = new Map<string, string>();
    cfgRows.forEach((r) => m.set(r.warehouse_id, r.warehouse_size));
    return m;
  }, [cfgRows]);

  const pressureByWarehouse = React.useMemo(() => {
    if (!pressureRows) return null;
    const m = new Map<string, Row>();
    pressureRows.forEach((r) => m.set(r.warehouse_id, r));
    return m;
  }, [pressureRows]);

  const disc = idleState.data ? numOrZero(idleState.data.discount_pct) : 0;
  const flagged = validRows ? validRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const totalRunningMin = validRows ? validRows.reduce((s, r) => s + numOrZero(r.running_minutes), 0) : 0;
  const totalCountedMin = validRows ? validRows.reduce((s, r) => s + numOrZero(r.counted_idle_minutes), 0) : 0;
  const totalBusyMin = validRows ? validRows.reduce((s, r) => s + numOrZero(r.busy_minutes), 0) : 0;
  const totalTailMin = validRows ? validRows.reduce((s, r) => s + numOrZero(r.stop_tail_minutes), 0) : 0;
  const totalWastedUsd = flagged ? flagged.reduce((s, r) => s + numOrZero(r.est_wasted_usd_list), 0) * (1 - disc) : 0;
  const worstByUsd = flagged && flagged.length ? [...flagged].sort((a, b) => numOrZero(b.est_wasted_usd_list) - numOrZero(a.est_wasted_usd_list))[0] : null;
  const underFloorRows = validRows ? validRows.filter((r) => isUnderFloor(r) && numOrZero(r.est_wasted_usd_list) > 0) : [];
  const underFloorUsd = underFloorRows.reduce((s, r) => s + numOrZero(r.est_wasted_usd_list), 0) * (1 - disc);
  const notMeasured = idleRows && validRows ? idleRows.length - validRows.length : 0;
  const critCount = validRows ? validRows.filter((r) => r.status === "CRITICAL").length : 0;
  const warnCount = validRows ? validRows.filter((r) => r.status === "WARN").length : 0;

  const gapsWorst = gapsRows && gapsRows.length ? [...gapsRows].sort((a, b) => numOrZero(b.max_running_gap_seconds) - numOrZero(a.max_running_gap_seconds))[0] : null;

  const churnFinding = findingFor(findings, "compute_warehouse_autoscale_churn");
  const churnBand = churnFinding ? bandOf(churnFinding) : null;

  React.useEffect(() => {
    if (!onVerdict || !validRows || !validRows.length || totalRunningMin <= 0) return;
    const pct = fmtPct((totalCountedMin / totalRunningMin) * 100, 0);
    const sentence = worstByUsd
      ? `Your SQL warehouses sat idle for ${fmtInt(Math.round(totalCountedMin / 60))} of their ${fmtInt(Math.round(totalRunningMin / 60))} running hours (${pct}), about ${fmtMoney(totalWastedUsd, 0)} in ${filters.window}d above the floor; ${warehouseName(dims, worstByUsd.warehouse_id)} accounts for ${fmtMoney(numOrZero(worstByUsd.est_wasted_usd_list) * (1 - disc), 0)}.`
        + (underFloorRows.length ? ` ${fmtInt(underFloorRows.length)} more warehouse${underFloorRows.length === 1 ? "" : "s"} total ${fmtMoney(underFloorUsd, 0)} under the floor.` : "")
      : `Your SQL warehouses sat idle for ${fmtInt(Math.round(totalCountedMin / 60))} of their ${fmtInt(Math.round(totalRunningMin / 60))} running hours (${pct}) in ${filters.window}d, with no priced possible waste.`;
    onVerdict(sentence);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [validRows, totalCountedMin, totalRunningMin, totalWastedUsd, worstByUsd, dims]);

  if (idleState.phase !== "loading" && idleState.outcome && idleState.outcome !== "ok_rows") {
    return (
      <div className="card">
        <ChartNote state={idleState} label="compute_warehouse_idle_minutes" />
      </div>
    );
  }

  return (
    <div>
      <CjKpis>
        <CjKpi
          label="Idle while running"
          tone={validRows && totalRunningMin > 0 && (totalCountedMin / totalRunningMin) >= 0.3 ? "crit" : ""}
          value={validRows ? (totalRunningMin > 0 ? fmtPct((totalCountedMin / totalRunningMin) * 100, 0) : "-") : kpiPending(idleState)}
          facts={validRows ? [
            { label: "Idle", value: `${fmtInt(Math.round(totalCountedMin / 60))} h`, detail: "no query over 1 min" },
            { label: "Running", value: `${fmtInt(Math.round(totalRunningMin / 60))} h` },
          ] : null}
        />
        <CjKpi
          label={`Idle cost, ${filters.window}d`}
          tone={totalWastedUsd > 0 ? "crit" : ""}
          value={validRows ? fmtMoney(totalWastedUsd, 0) : kpiPending(idleState)}
          facts={worstByUsd ? [
            { label: "Worst", value: warehouseName(dims, worstByUsd.warehouse_id), detail: `${fmtMoney(numOrZero(worstByUsd.est_wasted_usd_list) * (1 - disc), 0)} list price` },
          ] : null}
          link="Counted in Waste & savings →"
          onLink={() => goTo({ tab: "waste" })}
        />
        <CjKpi
          label="Waiting for auto-stop"
          value={validRows ? `${fmtInt(Math.round(totalTailMin / 60))} h` : kpiPending(idleState)}
          facts={validRows && totalCountedMin > 0 ? [
            { label: "Share of idle", value: fmtPct((totalTailMin / totalCountedMin) * 100, 0) },
          ] : null}
        />
        <CjKpi
          label="Warehouses flagged"
          value={validRows ? `${fmtInt((flagged || []).length)} of ${fmtInt(validRows.length)} measured` : kpiPending(idleState)}
          facts={validRows ? [
            { label: "Critical", value: fmtInt(critCount), tone: critCount > 0 ? "crit" : "ok" },
            { label: "Warn", value: fmtInt(warnCount), tone: warnCount > 0 ? "warn" : "ok" },
            notMeasured > 0 ? { label: "Not assessed", value: fmtInt(notMeasured) } : null,
            { label: "Busy", value: `${fmtInt(Math.round(totalBusyMin / 60))} h` },
          ].filter((f) => f != null) : null}
        />
      </CjKpis>

      <div className="cj-charts-2">
        <Card title="Where each warehouse's running time went">
          {validRows ? <WarehouseRuntimeChart rows={validRows} maxCat={maxCat} dims={dims} /> : <ChartNote state={idleState} label="compute_warehouse_idle_minutes" />}
        </Card>
        <AutoscaleChurnCard rows={churnRows} state={churnState} maxCat={maxCat} band={churnBand} dims={dims} />
      </div>
      <AutostopBandsCard rows={validRows} disc={disc} windowDays={filters.window} />
      <StartsStopsCard startup={startup} eventsRows={eventsRows} idleRows={validRows} cfgRows={cfgRows} days={windowDaysHeld(filters, meta)} dims={dims} />
      <UnusedWarehousesCard cfgRows={cfgRows} eventsState={eventsState} days={windowDaysHeld(filters, meta)} dims={dims} />

      <WarehouseTable rows={validRows} sizeByWarehouse={sizeByWarehouse} pressureByWarehouse={pressureByWarehouse} disc={disc} windowDays={filters.window} dims={dims} floor={idleState.data && idleState.data.floor} />
      <AutostopWhatIfPanel rows={validRows} churnRows={autostopChurnRows} disc={disc} maxCat={maxCat} dims={dims} />
      <AutostopChurnPanel rows={autostopChurnRows} state={autostopChurnState} maxCat={maxCat} dims={dims} />
      <CacheReusePanel rows={cacheReuseRows} state={cacheReuseState} maxCat={maxCat} dims={dims} />
      {gapsWorst && numOrZero(gapsWorst.max_running_gap_seconds) > 0 && (
        <div className="cj-caveat">
          <StatusIcon kind="reference" size={14} />
          <span>
            Longest unbroken running stretch: {fmtInt(numOrZero(gapsWorst.max_running_gap_seconds) / 3600)} h on{" "}
            {warehouseName(dims, gapsWorst.warehouse_id)} (from the Longest idle stretch check).
          </span>
        </div>
      )}
    </div>
  );
}
AreaContent.register("compute", "warehouses", ComputeWarehousesContent);

// ─────────────────────────────────────────── Clusters & pools ─────────────────────────────────

function ComputeClustersContent({ filters, dims, maxCat, goTo, onVerdict, meta }: TabProps) {
  const idleState = useFindingData("compute_idle_node_ratio", filters.window, filters.workspaceIds, filters.envs);
  // Counted server-side over every cluster: a row page would be an arbitrary 5,000-row slice.
  const nodeAgg = useFindingAgg("classic_clusters_config_current", filters.window, filters.workspaceIds, filters.envs, ["worker_node_type"], "count");
  const termAgg = useFindingAgg("classic_clusters_config_current", filters.window, filters.workspaceIds, filters.envs, ["cluster_source", "auto_termination_minutes"], "count");
  // Per cluster, to put each one's cost into its auto-termination band.
  const termByCluster = useFindingAgg("classic_clusters_config_current", filters.window, filters.workspaceIds, filters.envs, ["cluster_source", "cluster_id", "auto_termination_minutes"], "count");
  // Every cluster with a node running in the window, not only a page of rows.
  const usedClusters = useFindingAgg("compute_idle_node_ratio", filters.window, filters.workspaceIds, filters.envs, ["cluster_id"], "count");
  const poolState = useFindingData("instance_pools_idle_capacity", filters.window, filters.workspaceIds, filters.envs);

  const idleRows = readyRows(idleState);
  const poolRows = readyRows(poolState);

  const disc = idleState.data ? numOrZero(idleState.data.discount_pct) : 0;
  const flagged = idleRows ? idleRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const wastedUsd = flagged ? flagged.reduce((s, r) => s + numOrZero(r.est_wasted_usd_list), 0) * (1 - disc) : 0;
  const idleUnderFloor = idleRows ? idleRows.filter((r) => r.below_floor).sort((a, b) => numOrZero(b.idle_ratio) - numOrZero(a.idle_ratio)) : [];

  // Job-run clusters ("job-<id>-run-<id>") grouped into one bucket, not listed one by one --
  // cluster_source is carried on dim_cluster (T-40) precisely for this split.
  const clusterKind = (id: string) => { const c = dims && dims.clusters && dims.clusters.get(id); return c && c.cluster_source; };
  const named = idleRows ? idleRows.filter((r) => clusterKind(r.cluster_id) !== "JOB") : null;
  const jobRun = idleRows ? idleRows.filter((r) => clusterKind(r.cluster_id) === "JOB") : null;
  const namedRanked = named ? [...named].sort((a, b) => numOrZero(b.idle_ratio) - numOrZero(a.idle_ratio)).slice(0, maxCat || 8) : null;
  const jobRunAvgIdle = jobRun && jobRun.length ? jobRun.reduce((s, r) => s + numOrZero(r.idle_ratio), 0) / jobRun.length : null;

  const termGroups = termAgg.phase === "ready" && termAgg.outcome === "ok_rows"
    ? (termAgg.data.groups || []).filter((g) => g.key && (g.key[0] === "UI" || g.key[0] === "API")) : null;
  const noTermCount = termGroups ? termGroups.filter((g) => !numOrZero(g.key[1])).reduce((n, g) => n + numOrZero(g.value), 0) : null;
  const noTermTotal = termGroups ? termGroups.reduce((n, g) => n + numOrZero(g.value), 0) : null;

  const poolIdleTotal = poolRows ? poolRows.reduce((s, r) => s + numOrZero(r.min_idle_instances), 0) : null;
  const poolWorst = poolRows && poolRows.length ? [...poolRows].sort((a, b) => numOrZero(b.min_idle_instances) - numOrZero(a.min_idle_instances))[0] : null;

  const nodeReady = nodeAgg.phase === "ready" && nodeAgg.outcome === "ok_rows";
  const nodeTypeSegments = nodeReady
    ? topNWithOther(groupsToEntries(nodeAgg.data, (k) => String((k && k[0]) || "(single-node / unknown)"), "Other node types"), maxCat || 8, "Other node types")
      .map((e, i) => ({ label: e.name, value: e.value, display: fmtInt(e.value), color: e.color || paletteColor(i) }))
    : null;

  React.useEffect(() => {
    if (!onVerdict || !idleRows) return;
    onVerdict(flagged && flagged.length
      ? `${fmtInt(flagged.length)} of ${fmtInt(idleRows.length)} classic clusters ran mostly idle in ${filters.window}d, about ${fmtMoney(wastedUsd, 0)} possible waste.`
      : idleUnderFloor.length
        ? `${fmtInt(idleRows.length)} classic clusters checked in ${filters.window}d: ${fmtInt(idleUnderFloor.length)} ran mostly idle (${idleUnderFloor.slice(0, 2).map((r) => `${clusterName(dims, r.cluster_id)} ${fmtPct(numOrZero(r.idle_ratio) * 100, 0)}`).join(", ")}) but each wastes too little to flag.`
        : `${fmtInt(idleRows.length)} classic clusters checked in ${filters.window}d -- none ran idle enough to flag.`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idleRows, flagged, wastedUsd, idleUnderFloor.length]);

  return (
    <div>
      <CjKpis>
        <CjKpi
          label="Idle clusters (possible waste)"
          tone={wastedUsd > 0 ? "crit" : ""}
          value={idleRows ? fmtMoney(wastedUsd, 0) : kpiPending(idleState)}
          facts={flagged ? [
            { label: "Flagged", value: fmtInt(flagged.length), tone: flagged.length > 0 ? "crit" : "ok" },
            idleUnderFloor.length ? { label: "Under the floor", value: fmtInt(idleUnderFloor.length), detail: "mostly idle, not flagged" } : null,
            { label: "Window", value: `${filters.window}d` },
          ].filter((f) => f != null) : null}
          link="Counted in Waste & savings →"
          onLink={() => goTo({ tab: "waste" })}
        />
        <CjKpi
          label="Worst idle-node ratio"
          value={namedRanked && namedRanked.length ? fmtPct(numOrZero(namedRanked[0].idle_ratio) * 100, 0) : (idleRows ? "-" : kpiPending(idleState))}
          facts={namedRanked && namedRanked.length ? [
            { label: "Worst", value: clusterName(dims, namedRanked[0].cluster_id) },
          ] : null}
        />
        <CjKpi
          label="Pool idle capacity"
          value={poolRows ? (poolIdleTotal != null ? `${fmtInt(poolIdleTotal)} instance${poolIdleTotal === 1 ? "" : "s"}` : "-") : kpiPending(poolState)}
          facts={poolRows ? (poolWorst ? [
            { label: "Metric", value: "Warm instances idle" },
            { label: "Worst", value: poolWorst.instance_pool_name || shortId(poolWorst.instance_pool_id), title: poolWorst.instance_pool_name ? undefined : poolWorst.instance_pool_id },
          ] : [
            { label: "Pools", value: "None in this snapshot", tone: "muted" },
          ]) : null}
        />
        <CjKpi
          label="No auto-terminate"
          value={termGroups ? `${fmtInt(noTermCount)} of ${fmtInt(noTermTotal)}` : kpiPending(termAgg)}
          facts={termGroups ? [{ label: "Scope", value: "Interactive (UI/API) clusters" }] : null}
        />
      </CjKpis>

      <div className="cj-charts-2">
        <Card title="Worst idle-node ratio, by cluster">
          {namedRanked ? (
            <React.Fragment>
              <HBarList items={namedRanked.map((r) => ({ name: clusterName(dims, r.cluster_id), value: numOrZero(r.idle_ratio) * 100 }))} valueFmt={(v) => fmtPct(v, 0)} />
              {jobRun && jobRun.length > 0 && (
                <div className="cj-chart-takeaway">
                  {`${fmtInt(jobRun.length)} job cluster${jobRun.length === 1 ? "" : "s"} (one per run) not listed individually -- average idle ratio ${fmtPct(numOrZero(jobRunAvgIdle) * 100, 0)}.`}
                </div>
              )}
            </React.Fragment>
          ) : <ChartNote state={idleState} label="compute_idle_node_ratio" />}
        </Card>
        <Card title="Cluster count by worker node type">
          {nodeTypeSegments ? (
            <React.Fragment>
              <Donut segments={nodeTypeSegments} centerLabel={fmtInt(numOrZero(nodeAgg.data!.total_value))} centerSub="clusters" />
            </React.Fragment>
          ) : <ChartNote state={nodeAgg} label="classic_clusters_config_current" />}
        </Card>
      </div>
      <AutotermBandsCard cfgAgg={termByCluster} idleRows={idleRows} disc={idleState.data ? numOrZero(idleState.data.discount_pct) : 0} windowDays={filters.window} />
      <LongAutotermCard cfgAgg={termByCluster} idleState={idleState} dims={dims} />
      <UnusedClustersCard cfgAgg={termByCluster} usedAgg={usedClusters} days={windowDaysHeld(filters, meta)} dims={dims} />
    </div>
  );
}
AreaContent.register("compute", "clusters", ComputeClustersContent);

// ─────────────────────────────────────────── Configuration ────────────────────────────────────

// "workspace · id" under a name that repeats in the list; nothing for a unique name.
function repeatedSub(rows: Row[], nameOf: (r: Row) => string, row: Row, id: string): string | undefined {
  const name = nameOf(row);
  if (rows.filter((x) => nameOf(x) === name).length < 2) return undefined;
  return [resolveName("workspace", row.workspace_id, row.workspace_id), id].join(" · ");
}

function CjFlaggedRow({ name, sub, status, reasons }: { name: string; sub?: string; status: string; reasons?: string | null }) {
  return (
    <div className="cj-flag-row">
      <span><StatusPill kind={bandPillKind(status)} /></span>
      <span className="cj-flag-name">{name}{sub && <span className="cj-flag-sub">{sub}</span>}</span>
      <span className="cj-flag-reasons">{reasons || "-"}</span>
    </div>
  );
}

// "X of Y" / "X+ of Y" -- a fetch's own default row cap (useFindingData, 5000) sorts CRITICAL/WARN
// rows first on every posture query, so a flagged count is exact UNLESS every single fetched row
// was flagged (the cap was hit before a clean OK row ever appeared) -- only then is the true count
// possibly higher, and the "+" says so instead of a false-precise "5,000 of 5,000".
export function flaggedCountLabel(state: FindingState, rows: Row[] | null, flagged: Row[] | null, finding: FindingSummary | null): string {
  // The check's own count covers every row; the loaded page may be capped at 5,000.
  const a = finding && finding.affected;
  if (a && a.total != null) return `${fmtInt(a.flagged)} of ${fmtInt(a.total)}`;
  const all = rows || [], hit = flagged || [];
  const data = state && state.data;
  const total = data ? (data.rows_total != null ? data.rows_total : all.length) : all.length;
  const capped = !!(data && data.rows_total > data.returned);
  const uncertain = capped && hit.length === all.length;
  return `${fmtInt(hit.length)}${uncertain ? "+" : ""} of ${fmtInt(total)}`;
}

// The most common single flag among a posture check's flagged rows, from its own boolean flag_*
// columns -- a real count in place of the static list of every reason the check can raise.
function topFlagFact(rows: Row[] | null, flagCols: [string, string][]): Fact | null {
  if (!rows || !rows.length) return null;
  const counts = flagCols.map(([col, label]) => ({ label, n: rows.filter((r) => r[col]).length }));
  const top = [...counts].sort((a, b) => b.n - a.n)[0];
  if (!top || !top.n) return null;
  return { label: "Top flag", value: top.label, detail: `${fmtInt(top.n)} of ${fmtInt(rows.length)}${rows.length >= 5000 ? " loaded" : ""}` };
}
const CLUSTER_FLAG_COLS: [string, string][] = [
  ["flag_no_isolation", "No user isolation"],
  ["flag_eol_runtime", "End-of-support runtime"],
  ["flag_no_policy", "No policy"],
  ["flag_auto_term_risk", "Auto-term off/too high"],
];
const WAREHOUSE_FLAG_COLS: [string, string][] = [
  ["flag_autostop_risk", "Auto-stop off/too high"],
  ["flag_classic_type", "Classic type"],
  ["flag_preview_channel", "Preview channel"],
];

function ComputeConfigContent({ findings, filters, dims, maxCat, onVerdict }: TabProps) {
  const clusterState = useFindingData("compute_cluster_config_posture", filters.window, filters.workspaceIds, filters.envs);
  const warehouseState = useFindingData("compute_warehouse_config_posture", filters.window, filters.workspaceIds, filters.envs);
  const clusterRows = readyRows(clusterState);
  const warehouseRows = readyRows(warehouseState);
  const clusterFinding = findingFor(findings, "compute_cluster_config_posture");
  const warehouseFinding = findingFor(findings, "compute_warehouse_config_posture");

  const clusterFlagged = clusterRows ? clusterRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const warehouseFlagged = warehouseRows ? warehouseRows.filter((r) => r.status === "WARN") : null;
  const clusterTopFlag = topFlagFact(clusterFlagged, CLUSTER_FLAG_COLS);
  const warehouseTopFlag = topFlagFact(warehouseFlagged, WAREHOUSE_FLAG_COLS);

  React.useEffect(() => {
    if (!onVerdict || !clusterRows || !warehouseRows) return;
    const own = (fnd: FindingSummary | null, loaded: Row[] | null) => (fnd && fnd.affected ? fnd.affected.flagged : (loaded || []).length);
    const n = own(clusterFinding, clusterFlagged) + own(warehouseFinding, warehouseFlagged);
    onVerdict(n
      ? `${fmtInt(n)} setup risks found: ${flaggedCountLabel(clusterState, clusterRows, clusterFlagged, clusterFinding)} clusters and ${flaggedCountLabel(warehouseState, warehouseRows, warehouseFlagged, warehouseFinding)} warehouses.`
      : `No cluster or warehouse setup risks flagged, out of ${fmtInt(clusterRows.length)} clusters and ${fmtInt(warehouseRows.length)} warehouses.`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterRows, warehouseRows, clusterFlagged, warehouseFlagged]);

  return (
    <div>
      <CjKpis>
        <CjKpi
          label="Clusters flagged"
          tone={clusterFlagged && clusterFlagged.length ? "warn" : ""}
          value={clusterRows ? flaggedCountLabel(clusterState, clusterRows, clusterFlagged, clusterFinding) : kpiPending(clusterState)}
          facts={clusterTopFlag ? [clusterTopFlag] : [{ label: "Flags", value: "None", tone: "ok" }]}
        />
        <CjKpi
          label="Warehouses flagged"
          tone={warehouseFlagged && warehouseFlagged.length ? "warn" : ""}
          value={warehouseRows ? flaggedCountLabel(warehouseState, warehouseRows, warehouseFlagged, warehouseFinding) : kpiPending(warehouseState)}
          facts={warehouseTopFlag ? [warehouseTopFlag] : [{ label: "Flags", value: "None", tone: "ok" }]}
        />
      </CjKpis>
      <div className="cj-charts-2">
        <Card title="Cluster setup risks" right={clusterFlagged && clusterFlagged.length ? <span className="muted">{`${flaggedCountLabel(clusterState, clusterRows, clusterFlagged, clusterFinding).split(" of ")[0]} flagged, worst first`}</span> : null}>
          {clusterFlagged ? (clusterFlagged.length
            ? (
              <React.Fragment>
                {clusterFlagged.slice(0, maxCat || 8).map((r) => <CjFlaggedRow key={r.cluster_id} name={clusterName(dims, r.cluster_id)} sub={repeatedSub(clusterFlagged, (x) => clusterName(dims, x.cluster_id), r, r.cluster_id)} status={r.status} reasons={r.reasons} />)}
                {clusterFlagged.length > (maxCat || 8) && (
                  <div className="cj-chart-takeaway">{`${fmtInt(clusterFlagged.length - (maxCat || 8))} more flagged cluster${clusterFlagged.length - (maxCat || 8) === 1 ? "" : "s"} not shown -- see Checks below.`}</div>
                )}
              </React.Fragment>
            )
            : <span className="muted">Nothing flagged.</span>
          ) : <ChartNote state={clusterState} label="compute_cluster_config_posture" />}
        </Card>
        <Card title="Warehouse setup risks" right={warehouseFlagged && warehouseFlagged.length ? <span className="muted">{`${fmtInt(warehouseFlagged.length)} flagged, worst first`}</span> : null}>
          {warehouseFlagged ? (warehouseFlagged.length
            ? (
              <React.Fragment>
                {warehouseFlagged.slice(0, maxCat || 8).map((r) => <CjFlaggedRow key={r.warehouse_id} name={warehouseName(dims, r.warehouse_id)} sub={repeatedSub(warehouseFlagged, (x) => warehouseName(dims, x.warehouse_id), r, r.warehouse_id)} status={r.status} reasons={r.reasons} />)}
                {warehouseFlagged.length > (maxCat || 8) && (
                  <div className="cj-chart-takeaway">{`${fmtInt(warehouseFlagged.length - (maxCat || 8))} more flagged warehouse${warehouseFlagged.length - (maxCat || 8) === 1 ? "" : "s"} not shown -- see Checks below.`}</div>
                )}
              </React.Fragment>
            )
            : <span className="muted">Nothing flagged.</span>
          ) : <ChartNote state={warehouseState} label="compute_warehouse_config_posture" />}
        </Card>
      </div>
    </div>
  );
}
AreaContent.register("compute", "config", ComputeConfigContent);
