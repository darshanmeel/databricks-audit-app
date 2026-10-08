// Waste & savings (redesign section 6, DEC-74): a roll-up
// with no checks table of its own -- every WASTE_ITEMS id is homed on another area's own sub-tab
// (tab_registry.ts). Three sub-tabs, each its own registered Content component: priced (the
// resources actually costing money), unpriced (flagged checks with no $ column), method (how the
// $18,955-style total is counted). The shared worklist/total math lives in waste_total.ts --
// this file only fetches raw rows for the "top offenders" detail and lays the page out.

// A query on this tab may also appear on its own home tab -- these are views, not a partition.

import React from "react";

import { fmtInt, fmtMoney, fmtMoneyShort, fmtPct } from "../format";
import { resolveName, useNames } from "../components/names";
import { FLAGGED_STATUSES, belowFloorNoteText, clusterName, jobName, numOrZero, pipelineName, useFindingData, useMultiFindingData, warehouseName } from "../components/hooks";
import { AREA_REGISTRY, WASTE_IDS, WASTE_ITEMS, bandOf, homeForQuery } from "../components/tab_registry";
import { AreaContent, Card, ErrorBoundary, Facts, StatusPill, bandPillKind } from "../components/primitives";
import { getCheckLabel } from "../components/labels";
import { windowFacts } from "../components/overview_tile";
import { buildWasteWorklist, flaggedRows, useBelowFloorWaste, wasteRowKey, wasteTotal } from "../components/waste_total";
import { autostopChange, autostopChangeLine, autostopOptions } from "../components/pressure";

// One line for the page header (same sentence on all 3 sub-tabs, matching the design's shared
// <header> above the sub-tab bar) -- $ total, resource count, and the single biggest offender.
function wasteVerdictSentence({ ready, total, priced, resourceCount, windowDays, biggest }: LooseProps) {
  if (!ready) return "Loading possible waste for this window...";
  if (priced.length === 0) {
    return total.flaggedCount > 0
      ? `${fmtInt(total.flaggedCount)} check${total.flaggedCount === 1 ? "" : "s"} flagged possible waste over the last ${windowDays} days, none of them priced yet.`
      : `Nothing flagged as possible waste over the last ${windowDays} days.`;
  }
  const lead = `${fmtMoney(total.usd, 0)} of possible waste over the last ${windowDays} days, from ${fmtInt(resourceCount)} resource${resourceCount === 1 ? "" : "s"}.`;
  if (!biggest) return lead;
  return `${lead} One ${lowerFirstWord(biggest.sourceLabel)}, ${biggest.name}, is ${fmtMoney(biggest.v, 0)} of it${biggest.why ? `: ${lowerFirstWord(biggest.why)}` : "."}`;
}
function lowerFirstWord(s: any) {
  const t = String(s || "");
  if (!t) return t;
  // An acronym's second letter (SQL, ML, DBU...) is also upper case -- lowering only the first
  // letter there reads as a typo ("sQL warehouse"), so an acronym is left alone entirely.
  const second = t.charAt(1);
  if (second && second === second.toUpperCase() && second !== second.toLowerCase()) return t;
  return t.charAt(0).toLowerCase() + t.slice(1);
}

const hrs = (min: any) => `${fmtInt(Math.round(numOrZero(min) / 60))} h`;

// The auto-stop change line: pressure.tsx's autostopChangeLine, shared with Compute and Queries
// so the same warehouse never reads a different target or saving depending on which tab it is on.
const warehouseAutostopChange = autostopChangeLine;

// Per-WASTE_ITEMS-id row-level extraction: the resource's display name, its numbers as labelled
// facts, and (warehouses) where its running time went -- never the row's raw reason sentence.
export const OFFENDER_META: Record<string, any> = {
  compute_warehouse_idle_minutes: {
    resource: "SQL warehouse",
    name: (r: any, dims: any) => warehouseName(dims, r.warehouse_id),
    facts: (r: any) => [
      { label: "Idle", value: fmtPct(r.idle_share_pct, 0), detail: `of ${hrs(r.running_minutes)} running`, tone: "crit" },
      { label: "Waiting to stop", value: hrs(r.stop_tail_minutes), detail: `auto-stop ${fmtInt(r.auto_stop_minutes)} min` },
      { label: "Idle gaps", value: fmtInt(r.counted_idle_gaps), detail: `over 1 min · longest ${fmtInt(Math.round(numOrZero(r.longest_idle_gap_minutes)))} min` },
    ],
    // Running queries includes pauses of a minute or less, so the parts add up to the running time.
    // Dollars: the idle cost split by idle minutes; running queries get the rest of the warehouse's cost.
    split: (r: any, disc: any) => {
      const between = numOrZero(r.between_queries_minutes) + numOrZero(r.start_gap_minutes);
      const tail = numOrZero(r.stop_tail_minutes), none = numOrZero(r.no_query_minutes);
      const net = 1 - numOrZero(disc), idle = between + tail + none;
      const priced = r.est_usd_list != null && r.est_wasted_usd_list != null;
      const idleUsd = (m: number) => (priced && idle > 0 ? (numOrZero(r.est_wasted_usd_list) * m / idle) * net : null);
      return [
        { label: "Running queries", value: Math.max(numOrZero(r.running_minutes) - idle, 0), color: "var(--st-ok)",
          usd: priced ? Math.max(numOrZero(r.est_usd_list) - numOrZero(r.est_wasted_usd_list), 0) * net : null },
        { label: "Idle between queries", value: between, color: "var(--st-warn)", usd: idleUsd(between) },
        { label: "Waiting for auto-stop", value: tail, color: "var(--st-crit)", usd: idleUsd(tail) },
        { label: "Running with no query", value: none, color: "var(--text-3)", usd: idleUsd(none) },
      ];
    },
    runningUsd: (r: any, disc: any) => (r.est_usd_list != null ? numOrZero(r.est_usd_list) * (1 - numOrZero(disc)) : null),
    detailHead: "How its running hours were spent",
    costWord: "idle cost",
    what: "paid for while running no queries",
    kind: (r: any) => r.warehouse_kind,
    autoStop: (r: any) => r.auto_stop_minutes,
    running: (r: any) => numOrZero(r.running_minutes),
    idlePct: (r: any) => r.idle_share_pct,
    gaps: (r: any) => `${fmtInt(r.counted_idle_gaps)} idle gaps over 1 min · longest ${fmtInt(Math.round(numOrZero(r.longest_idle_gap_minutes)))} min`,
    change: warehouseAutostopChange,
    changeData: (r: any, disc: any) => autostopChange(r, disc),
    changeOptions: (r: any, disc: any) => autostopOptions(r, disc),
    entityId: (r: any) => r.warehouse_id,
  },
  compute_idle_node_ratio: {
    resource: "cluster",
    name: (r: any, dims: any) => clusterName(dims, r.cluster_id),
    facts: (r: any) => [
      { label: "CPU idle", value: r.total_slices ? fmtPct((numOrZero(r.idle_slices) / numOrZero(r.total_slices)) * 100, 0) : "-", detail: "of node-minutes", tone: "crit" },
      { label: "Avg CPU", value: fmtPct(r.avg_cpu_pct_all, 0), detail: `peak ${fmtPct(r.peak_cpu_pct_all, 0)}` },
    ],
    detailHead: "How busy its nodes were",
    what: "paid for while the nodes sat idle",
  },
  lakeflow_failed_jobs_wasted_dbus: {
    resource: "job",
    name: (r: any, dims: any) => jobName(dims, r.workspace_id, r.job_id),
    facts: (r: any) => [
      { label: "Failed", value: `${fmtInt(r.failed_runs)} of ${fmtInt(r.distinct_runs)} runs`, detail: fmtPct(r.failure_rate_pct, 0), tone: "crit" },
      { label: "Last error", value: r.last_failed_termination_code || "-" },
    ],
    detailHead: "Failed runs",
    what: "paid for runs that failed",
  },
  cost_failed_statement_waste: {
    resource: "SQL warehouse",
    name: (r: any, dims: any) => warehouseName(dims, r.warehouse_id),
    facts: (r: any) => [
      { label: "Failed", value: `${fmtInt(r.failed_statements)} of ${fmtInt(r.statements)} statements`, tone: "crit" },
      { label: "Canceled", value: fmtInt(r.canceled_statements) },
    ],
    detailHead: "Failed statements",
    what: "paid for statements that failed",
  },
  compute_serving_endpoint_cost_status: {
    resource: "serving endpoint",
    name: (r: any) => r.endpoint_name || r.served_entity_id || String(r.endpoint_id || ""),
    facts: (r: any) => [{ label: "Requests", value: fmtInt(r.endpoint_requests_window), detail: "in the window" }],
    detailHead: "Requests",
    what: "paid for with no requests",
  },
};

// One plain fix per waste source, in place of the check's raw action text.
export const WASTE_FIX: Record<string, string> = {
  compute_warehouse_idle_minutes: "Lower auto-stop: serverless to 5 min in the UI (1 min through the API), pro and classic to 10 min. See Compute › Warehouses for the saving per setting. After the change, the saving shows here as lower possible waste in the next window.",
  compute_idle_node_ratio: "Shorten auto-termination and use smaller or fewer nodes; move scheduled work to job clusters.",
  lakeflow_failed_jobs_wasted_dbus: "Fix or pause the failing jobs; keep retries for errors that are really transient.",
  cost_failed_statement_waste: "Fix the statements that fail repeatedly, or stop what keeps sending them.",
  compute_serving_endpoint_cost_status: "Scale endpoints with no requests to zero, or delete them.",
};

// "Resources flagged" fact label per priced source -- two checks can share the same noun
// (compute_warehouse_idle_minutes and cost_failed_statement_waste both flag warehouses), so the
// generic affected.noun alone would print "Warehouses" twice in the same row; this names what
// EACH check actually flags them for.
const RESOURCE_FACT_LABEL: Record<string, string> = {
  compute_warehouse_idle_minutes: "Idle warehouses",
  compute_idle_node_ratio: "Idle clusters",
  lakeflow_failed_jobs_wasted_dbus: "Failed jobs",
  cost_failed_statement_waste: "Warehouses with failed statements",
  compute_serving_endpoint_cost_status: "Idle serving endpoints",
};

export function wsLabel(workspaceId: any) {
  if (workspaceId === undefined || workspaceId === null) return null;
  return resolveName("workspace", null, workspaceId) || `workspace ${workspaceId}`;
}

// De-duplicated (wasteRowKey), sorted desc by the item's own dollar column -- one entry per real
// resource, never per raw row (a resource billed on several days must count once).
export function offenderRows(item: any, rows: any, dims: any, windowDays: any) {
  const meta = OFFENDER_META[item.id];
  if (!meta || !rows) return [];
  const seen = new Set();
  const list: any[] = [];
  flaggedRows(rows).forEach((r) => {
    const key = wasteRowKey(item.id, r);
    if (seen.has(key)) return;
    // Discounted the same way the headline total is (item.discountPct), so offenders always sum
    // back to the priced total and "Share" can never read over 100%.
    const v = numOrZero(r[item.dollarCol]) * (1 - numOrZero(item.discountPct));
    // A flagged row priced at exactly $0 is not a waste offender to rank -- it stays flagged
    // (counted elsewhere), just not listed here as if it cost something.
    if (v <= 0) return;
    seen.add(key);
    const name = meta.name(r, dims) || key;
    const ws = wsLabel(r.workspace_id);
    // A shared warehouse name already carries its workspace ("Serverless Starter Warehouse · ws").
    const nameHasWs = !!ws && String(name).endsWith(` · ${ws}`);
    list.push({
      key, sourceId: item.id, sourceLabel: meta.resource, name,
      ws: nameHasWs ? null : ws, facts: meta.facts(r), split: meta.split ? meta.split(r, item.discountPct) : null,
      change: meta.change ? meta.change(r, item.discountPct, windowDays) : null, v,
      shortName: nameHasWs ? String(name).slice(0, -(String(ws).length + 3)) : name, wsName: ws,
      kind: meta.kind ? meta.kind(r) : null, autoStop: meta.autoStop ? meta.autoStop(r) : null,
      running: meta.running ? meta.running(r) : null, runningUsd: meta.runningUsd ? meta.runningUsd(r, item.discountPct) : null, idlePct: meta.idlePct ? meta.idlePct(r) : null,
      gaps: meta.gaps ? meta.gaps(r) : null, changeData: meta.changeData ? meta.changeData(r, item.discountPct) : null,
      changeOptions: meta.changeOptions ? meta.changeOptions(r, item.discountPct) : null,
      entityId: meta.entityId ? meta.entityId(r) : null,
    });
  });
  list.sort((a, b) => b.v - a.v);
  return list;
}

// Same resolution, generalised over the finding row's own affected.column -- for the unpriced
// sub-tab, where the id list (and so the id-column) varies, unlike the 5 hand-written priced ids above.
export function entityDisplayName(column: any, row: any, dims: any) {
  switch (column) {
    case "job_id": return jobName(dims, row.workspace_id, row.job_id);
    case "cluster_id": return clusterName(dims, row.cluster_id);
    case "warehouse_id": return warehouseName(dims, row.warehouse_id);
    case "pipeline_id": return pipelineName(dims, row.workspace_id, row.pipeline_id);
    case "endpoint_name": return row.endpoint_name;
    case "endpoint_id": return row.endpoint_name || row.endpoint_id;
    default: return row[column] != null ? String(row[column]) : null;
  }
}

export function KpiMini({ label, value, tone, note, facts }: LooseProps) {
  return (
    <div className="card ov-kpi-card">
      <div className="ov-kpi-label">{label}</div>
      <div className={`ov-kpi-value mono${tone ? ` tone-${tone}` : ""}`}>{value}</div>
      {facts ? <Facts items={facts} /> : (note && <div className="ov-kpi-caption">{note}</div>)}
    </div>
  );
}

function OverlapNote({ text }: LooseProps) {
  if (!text) return null;
  return <div className="card ov-note-card">{text}</div>;
}

// Where one warehouse's running time went, as one stacked bar with a hover label per part.
function TimeSplitBar({ parts }: LooseProps) {
  const total = parts.reduce((s: any, p: any) => s + p.value, 0);
  if (total <= 0) return null;
  return (
    <div className="ws-split-bar">
      {parts.filter((p: any) => p.value > 0).map((p: any) => (
        <span key={p.label} style={{ width: `${(p.value / total) * 100}%`, background: p.color }}
          title={`${p.label}: ${fmtHours(p.value)}${p.usd != null ? `, ${fmtMoney(p.usd, 0)}` : ""} (${fmtPct((p.value / total) * 100, 0)})`} />
      ))}
    </div>
  );
}

// Minutes as hours: one decimal under 10 h, whole hours above.
function fmtHours(min: any) {
  const h = numOrZero(min) / 60;
  if (h > 0 && h < 0.1) return "<0.1 h";
  return `${h < 10 ? h.toFixed(1) : fmtInt(Math.round(h))} h`;
}

function ClockIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" className="ws-clock">
      <circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <path d="M6 3.2V6l1.9 1.2" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function HoursSplit({ o }: LooseProps) {
  return (
    <React.Fragment>
      <div className="ws-hours-top">
        <span><b className="mono">{fmtHours(o.running)}</b>{o.runningUsd != null && <span className="ws-hours-usd">{` (${fmtMoney(o.runningUsd, 0)})`}</span>} running</span>
        {o.idlePct != null && <span><b className="mono">{fmtPct(o.idlePct, 0)}</b> idle</span>}
      </div>
      <TimeSplitBar parts={o.split} />
      <div className="ws-hours-legend">
        {o.split.filter((p: any) => p.value > 0).map((p: any) => (
          <span key={p.label}><i style={{ background: p.color }} /><b className="mono">{fmtHours(p.value)}</b>{p.usd != null && <span className="ws-hours-usd">{` (${fmtMoney(p.usd, 0)})`}</span>}{` ${p.label.toLowerCase()}`}</span>
        ))}
      </div>
      {o.gaps && <div className="ws-hours-gaps">{o.gaps}</div>}
    </React.Fragment>
  );
}

// What a stop throws away (compute_warehouse_cache_reuse). Little is lost only when the result cache is
// rarely hit (the check's WARN) and under half of reads come from the disk cache; a stop empties both.
const DISK_CACHE_HIGH_PCT = 50;
function cacheLine(c: any) {
  const parts = [
    c.from_result_cache_pct != null ? `result ${fmtPct(c.from_result_cache_pct, 0)} of queries` : null,
    c.avg_read_io_cache_percent != null ? `disk ${fmtPct(c.avg_read_io_cache_percent, 0)} of reads` : null,
  ].filter(Boolean).join(", ");
  if (!parts) return null;
  const little = c.status === "WARN" && numOrZero(c.avg_read_io_cache_percent) < DISK_CACHE_HIGH_PCT;
  return `Cache: ${parts}. ${little ? "Little is lost by stopping sooner." : "A stop empties both; the first queries after a start run slower."}`;
}

function ChangeCell({ o, windowDays, cacheBy }: LooseProps) {
  const opts = o.changeOptions || [];
  const cache = cacheBy && o.entityId != null ? cacheBy.get(String(o.entityId)) : null;
  const cacheText = cache ? cacheLine(cache) : null;
  if (!opts.length) {
    const target = o.kind === "serverless" ? 5 : 10;
    const text = o.autoStop === 0 ? "Auto-stop is off"
      : o.autoStop != null && o.autoStop <= target ? `Auto-stop already at ${fmtInt(o.autoStop)} min` : "No saving estimate";
    return <div className="ws-off-change ws-change-none">{text}</div>;
  }
  return (
    <div className="ws-off-change">
      <div className="ws-change-box">
        <div className="ws-change-title">{`Set auto-stop ${fmtInt(opts[0].current)} min to`}</div>
        <div className="ws-change-grid">
          <span /><span className="ws-change-head">saves</span><span className="ws-change-head">cold starts</span>
          {opts.map((ch: any) => (
            <React.Fragment key={ch.target}>
              <span className="ws-change-to">{`${fmtInt(ch.target)} min`}</span>
              <span className="ws-change-usd mono">{`−${fmtMoney(ch.savingUsd, 0)}`}</span>
              <span className="ws-change-starts mono">{`${ch.extraColdStarts > 0 ? "+" : ""}${fmtInt(ch.extraColdStarts)}`}</span>
            </React.Fragment>
          ))}
        </div>
        <div className="ws-change-per">{`over ${fmtInt(windowDays)} days`}</div>
      </div>
      {cacheText && <div className="ws-change-cache">{cacheText}</div>}
    </div>
  );
}

function OffenderRow({ o, max, hasChange, costWord, windowDays, cacheBy }: LooseProps) {
  const pct = max > 0 ? Math.max(2, Math.round((o.v / max) * 100)) : 2;
  return (
    <div className="ws-off-row" role="row">
      <div className="ws-off-name" role="cell">
        <div className="ws-off-title"><b>{o.shortName}</b>{o.kind && <span className="ws-kind-chip">{o.kind}</span>}</div>
        {o.wsName && <div className="ws-off-ws">{o.wsName}</div>}
        {o.autoStop != null && (
          <div className="ws-off-autostop"><ClockIcon />Auto-stop <b>{o.autoStop === 0 ? "off" : `${fmtInt(o.autoStop)} min`}</b></div>
        )}
      </div>
      <div className="ws-off-detail" role="cell">
        {o.split ? <HoursSplit o={o} /> : <Facts items={o.facts} />}
      </div>
      <div className="ws-off-cost" role="cell">
        <div className="ws-off-usd mono">{fmtMoney(o.v, 0)}</div>
        <div className="ws-off-cost-track"><div style={{ width: `${pct}%` }} /></div>
        <div className="ws-off-cost-word">{costWord}</div>
      </div>
      {hasChange && <ChangeCell o={o} windowDays={windowDays} cacheBy={cacheBy} />}
    </div>
  );
}

// "1 warehouse flagged", or "1 of 18 flagged warehouses priced · 17 with no cost estimate" when the
// check flags more than the resources it could price.
function pricedAffectedText(pricedCount: any, flaggedTotal: any, noun: any) {
  const one = noun.endsWith("s") ? noun.slice(0, -1) : noun;
  if (flaggedTotal != null && flaggedTotal > pricedCount) {
    return `${fmtInt(pricedCount)} of ${fmtInt(flaggedTotal)} flagged ${noun} priced · ${fmtInt(flaggedTotal - pricedCount)} with no cost estimate`;
  }
  return `${fmtInt(pricedCount)} ${pricedCount === 1 ? one : noun} flagged`;
}

// What the top rows' recommended changes add up to; the source's plain fix when there are none.
function SourceCallout({ item, shownTop, windowDays }: LooseProps) {
  const withChange = shownTop.filter((o: any) => o.changeData);
  if (!withChange.length) return WASTE_FIX[item.id] ? <div className="ws-callout">{WASTE_FIX[item.id]}</div> : null;
  const saving = withChange.reduce((s: number, o: any) => s + o.changeData.savingUsd, 0);
  const starts = withChange.reduce((s: number, o: any) => s + o.changeData.extraColdStarts, 0);
  const which = withChange.length === shownTop.length
    ? (shownTop.length === 1 ? "the top warehouse" : `the top ${fmtInt(shownTop.length)}`)
    : `${fmtInt(withChange.length)} of the top ${fmtInt(shownTop.length)}`;
  return (
    <div className="ws-callout">
      {`Tightening auto-stop on ${which} alone would save `}<b>{fmtMoney(saving, 0)}</b>{` over ${fmtInt(windowDays)} days`}
      {starts > 0 ? <React.Fragment>{", at the cost of "}<b>{fmtInt(starts)}</b>{` extra cold start${starts === 1 ? "" : "s"}.`}</React.Fragment> : "."}
    </div>
  );
}

// Rows listed when the card is opened up; the rest stay on the source's own tab.
const MORE_ROWS_MAX = 50;

function PricedSourceCard({ rank, item, rows, dims, entityNoun, flaggedTotal, goTo, windowDays, cacheBy }: LooseProps) {
  const [open, setOpen] = React.useState(false);
  const offenders = React.useMemo(() => offenderRows(item, rows, dims, windowDays), [item, rows, dims, windowDays]);
  const meta = OFFENDER_META[item.id] || {};
  const shownTop = offenders.slice(0, 3);
  const rest = offenders.slice(3);
  const restShown = open ? rest.slice(0, MORE_ROWS_MAX) : [];
  const restUsd = rest.reduce((s, o) => s + o.v, 0);
  const max = offenders.length ? offenders[0].v : 1;
  const label = getCheckLabel(item.id, item.title);
  const target = { ...homeForQuery({ query_id: item.id }), focusQueryId: item.id };
  const hasChange = !!meta.changeData;
  const costWord = meta.costWord || "possible waste";
  const resource = String(meta.resource || entityNoun);
  const legend = meta.split
    ? (offenders[0] && offenders[0].split ? offenders[0].split : []).filter((p: any) => [...shownTop, ...restShown].some((o) => o.split && o.split.find((x: any) => x.label === p.label && x.value > 0)))
    : [];
  return (
    <Card className="ws-source-card">
      <div className="ws-source-left">
        <div className="ws-source-head">
          <span className="ws-source-rank mono">{rank}</span>
          <StatusPill kind={bandPillKind(item.band)} />
        </div>
        <div className="ws-source-name">{label.title}</div>
        <div className="ws-source-value mono tone-crit">{fmtMoney(item.dollarUsd, 0)}</div>
        <div className="ws-source-affected">{`${pricedAffectedText(offenders.length, flaggedTotal, entityNoun)}${meta.what ? ` · ${meta.what}` : ""}`}</div>
        <SourceCallout item={item} shownTop={shownTop} windowDays={windowDays} />
        <button type="button" className="ov-link-btn ws-source-open" onClick={() => goTo(target)}>{`Open in ${AREA_REGISTRY[item.area].label} →`}</button>
      </div>
      <div className="ws-source-right">
        <div className="ws-top-head">
          <div><span className="ws-source-top-label">Top offenders</span><span className="ws-top-sort">{`sorted by ${costWord}`}</span></div>
          {legend.length > 0 && (
            <div className="ws-split-legend">
              {legend.map((p: any) => <span key={p.label}><i style={{ background: p.color }} />{p.label}</span>)}
            </div>
          )}
        </div>
        {shownTop.length === 0 ? (
          <div className="faint">{label.why || "No offender in this slice."}</div>
        ) : (
          <div className={`ws-off-table${hasChange ? " has-change" : ""}`} role="table">
            <div className="ws-off-row ws-off-headrow" role="row">
              <div className="ws-off-name" role="columnheader">{resource}</div>
              <div className="ws-off-detail" role="columnheader">{meta.detailHead || "Details"}</div>
              <div className="ws-off-cost" role="columnheader">{costWord}</div>
              {hasChange && <div className="ws-off-change" role="columnheader">Recommended change</div>}
            </div>
            {[...shownTop, ...restShown].map((o) => (
              <OffenderRow key={o.key} o={o} max={max} hasChange={hasChange} costWord={costWord} windowDays={windowDays} cacheBy={cacheBy} />
            ))}
          </div>
        )}
        {rest.length > 0 && (
          <div className="ws-more">
            <button type="button" className="ws-more-btn" onClick={() => setOpen(!open)}>
              {open ? "Show the top 3 only" : `Show ${fmtInt(rest.length)} more ${rest.length === 1 && entityNoun.endsWith("s") ? entityNoun.slice(0, -1) : entityNoun} · ${fmtMoney(restUsd, 0)} ${costWord === "idle cost" ? "idle" : costWord}`}
            </button>
            {!open && <div className="ws-more-next">{`Next: ${rest.slice(0, 2).map((o) => `${o.name} ${fmtMoneyShort(o.v)}`).join(", ")}${rest.length > 2 ? " ..." : ""}`}</div>}
            {open && rest.length > MORE_ROWS_MAX && <div className="ws-more-next">{`${fmtInt(rest.length - MORE_ROWS_MAX)} more not listed here · open in ${AREA_REGISTRY[item.area].label}`}</div>}
          </div>
        )}
      </div>
    </Card>
  );
}

function WastePricedContent({ filters, meta, dims, goTo, setVerdict, allFindingsIndex }: LooseProps) {
  useNames();
  const multi = useMultiFindingData(WASTE_IDS, filters.window, filters.workspaceIds, filters.envs, 5000, FLAGGED_STATUSES);
  const cacheState = useFindingData("compute_warehouse_cache_reuse", filters.window, filters.workspaceIds, filters.envs);
  const cacheBy = React.useMemo(() => {
    const rows = cacheState.phase === "ready" && cacheState.outcome === "ok_rows" ? cacheState.data.rows : [];
    return new Map((rows || []).map((r: any) => [String(r.warehouse_id), r]));
  }, [cacheState]);
  // Waste's own ids:[] (every WASTE_IDS check is homed on another area), so the scoped `findings`
  // AreaPage hands this content is always empty -- read the full index instead.
  const findingsById = allFindingsIndex || {};
  const worklist = React.useMemo(() => buildWasteWorklist(findingsById, multi), [findingsById, multi]);
  const belowFloorById = useBelowFloorWaste(filters);
  const total = wasteTotal(worklist, belowFloorById);
  const ready = multi.phase === "ready";
  const flagged = worklist.filter((w) => w.isFlagged);
  const priced = [...flagged.filter((w) => w.dollarUsd != null)].sort((a, b) => b.dollarUsd! - a.dollarUsd!);
  const winFacts = windowFacts(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);

  const allOffenders = React.useMemo(
    () => priced.flatMap((w) => offenderRows(w, multi.byId[w.id] && multi.byId[w.id].data && multi.byId[w.id].data!.rows, dims, filters.window)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [priced.map((w) => w.id).join(","), multi, dims, filters.window]
  );
  const sortedOffenders = [...allOffenders].sort((a, b) => b.v - a.v);
  const top3 = sortedOffenders.slice(0, 3);
  const top3Sum = top3.reduce((s, o) => s + o.v, 0);
  const biggest = sortedOffenders[0] || null;
  // Distinct offenders per source (offenderRows already de-dupes by wasteRowKey) -- never the raw
  // flagged-ROW count, which double-counts a resource billed on more than one day.
  const bySource = new Map();
  allOffenders.forEach((o) => bySource.set(o.sourceId, (bySource.get(o.sourceId) || 0) + 1));
  const resourcesFlagged = Array.from(bySource.values()).reduce((s, n) => s + n, 0);
  const resourceFacts = priced
    .map((w) => {
      const n = bySource.get(w.id) || 0;
      if (!n) return null;
      const f = findingsById[w.id];
      const noun = (f && f.affected && f.affected.noun) || "rows";
      const label = RESOURCE_FACT_LABEL[w.id] || (noun.charAt(0).toUpperCase() + noun.slice(1));
      const flaggedTotal = f && f.affected ? f.affected.flagged : null;
      return { label, value: fmtInt(n), detail: flaggedTotal != null && flaggedTotal > n ? `of ${fmtInt(flaggedTotal)} flagged` : undefined };
    })
    .filter(Boolean)
    .slice(0, 4);

  const sentence = wasteVerdictSentence({ ready, total, priced, resourceCount: resourcesFlagged, windowDays: filters.window, biggest });
  React.useEffect(() => { if (setVerdict) setVerdict(sentence); }, [sentence, setVerdict]);

  const totalFacts = ready
    ? [
        { label: "Sources", value: `${fmtInt(priced.length)} of ${fmtInt(WASTE_ITEMS.filter((w) => w.dollarCol).length)}`, detail: "priced sources with rows this window" },
        ...winFacts,
      ]
    : [{ label: "Status", value: "Loading...", tone: "muted" }];
  const top3Facts = top3.length
    ? [
        total.usd > 0 ? { label: "Share", value: fmtPct((top3Sum / total.usd) * 100, 0), detail: "of possible waste" } : null,
        { label: "Top", value: top3[0].name },
        top3.length > 1 ? { label: "Also", value: top3.slice(1).map((o) => o.name).join(", ") } : null,
      ].filter(Boolean)
    : [{ label: "Status", value: "No priced resources this window", tone: "muted" }];
  // The raw waste_reason/why string is a multi-clause sentence with unformatted numbers, not a
  // fact -- this card names the resource only; its own source card (below) carries the detail.
  const biggestFacts = biggest
    ? [{ label: "Resource", value: biggest.name }]
    : [{ label: "Status", value: "n/a", tone: "muted" }];

  return (
    <div>
      <div className="ov-kpi-grid">
        <KpiMini label={`Possible waste, ${filters.window} days`} value={ready ? fmtMoney(total.usd, 0) : "-"} tone="crit" facts={totalFacts} />
        <KpiMini label="Top 3 resources" value={top3.length ? fmtMoney(top3Sum, 0) : "-"} facts={top3Facts} />
        <KpiMini label="Biggest single fix" value={biggest ? fmtMoney(biggest.v, 0) : "-"} facts={biggestFacts} />
        <KpiMini label="Resources flagged" value={fmtInt(resourcesFlagged)} facts={resourceFacts.length ? resourceFacts : [{ label: "Status", value: "None", tone: "muted" }]} />
      </div>

      <OverlapNote text={total.overlapNote ? `The priced sources can overlap: ${total.overlapNote} All figures are possible waste, not a guaranteed saving.`
        : (priced.length > 1 ? "No overlap this window: idle cluster minutes and failed job runs, the two sources that can overlap, are not both priced. All figures are possible waste, not a guaranteed saving." : null)} />
      <OverlapNote text={belowFloorNoteText(total, filters.window)} />

      {priced.length > 0 && (
        <React.Fragment>
          <div className="section-title" style={{ marginTop: 4 }}>Where it comes from, largest first</div>
          {priced.map((item, i) => (
            <ErrorBoundary key={item.id}>
              <PricedSourceCard rank={i + 1} item={item} rows={multi.byId[item.id] && multi.byId[item.id].data && multi.byId[item.id].data!.rows} dims={dims}
                entityNoun={(findingsById[item.id] && findingsById[item.id].affected && findingsById[item.id].affected.noun) || "rows"}
                flaggedTotal={findingsById[item.id] && findingsById[item.id].affected ? findingsById[item.id].affected.flagged : null}
                goTo={goTo} windowDays={filters.window} cacheBy={cacheBy} />
            </ErrorBoundary>
          ))}
        </React.Fragment>
      )}
      {ready && priced.length === 0 && (
        <div className="card">
          <div className="h-title">{flagged.length > 0 ? "Nothing priced yet" : "Nothing flagged"}</div>
          <div className="h-note">
            {flagged.length > 0
              ? `${fmtInt(flagged.length)} check${flagged.length === 1 ? "" : "s"} flagged possible waste this window, but none of them carry a cost estimate.`
              : "No idle warehouse minutes, idle cluster nodes, failed job runs or failed statements flagged this window."}
          </div>
          {flagged.length > 0 && <button type="button" className="ov-link-btn" onClick={() => goTo({ tab: "waste", subtab: "unpriced" })}>{"See what was flagged →"}</button>}
        </div>
      )}
    </div>
  );
}

function FlaggedRow({ w, dims, goTo }: LooseProps) {
  const finding = w.finding;
  const label = getCheckLabel(w.id, finding && finding.title);
  const target = { ...homeForQuery({ query_id: w.id }), focusQueryId: w.id };
  const ent = finding && finding.affected;
  // A check with no entity column (dormant endpoints, pools, premium serverless) has nothing an
  // id-derived name could stand for -- "N rows flagged" instead of a fake "(unnamed) on ...".
  // Counts come from the server (every row), never from the capped page loaded here.
  const total = ent ? ent.flagged : w.flaggedCount;
  const noun = ent ? (total === 1 && ent.noun.endsWith("s") ? ent.noun.slice(0, -1) : ent.noun) : (total === 1 ? "row" : "rows");
  const firstTwo: any[] = [];
  const seen = new Set();
  (w.rows || []).forEach((r: any) => {
    if (!ent || !ent.column || firstTwo.length >= 2) return;
    const key = `${r.workspace_id}:${r[ent.column]}`;
    if (seen.has(key)) return;
    seen.add(key);
    firstTwo.push(`${entityDisplayName(ent.column, r, dims) || "(unnamed)"} on ${wsLabel(r.workspace_id) || "(no workspace)"}`);
  });
  // The count leads, so it stays visible however long the names are.
  const who = firstTwo.length
    ? `${fmtInt(total)} ${noun}: ${firstTwo.join("; ")}${total > firstTwo.length ? ` +${fmtInt(total - firstTwo.length)} more` : ""}`
    : `${fmtInt(total)} ${noun} flagged`;
  return (
    <div className="ws-flagged-row">
      <StatusPill kind={bandPillKind(w.band)} />
      <span className="ws-flagged-title">{label.title}</span>
      <span className="ws-flagged-who faint">{who}</span>
      <button type="button" className="ov-link-btn" onClick={() => goTo(target)}>{`Open in ${AREA_REGISTRY[w.area].label} →`}</button>
    </div>
  );
}

function WasteUnpricedContent({ filters, meta, dims, goTo, setVerdict, allFindingsIndex }: LooseProps) {
  useNames();
  const unpricedItems = WASTE_ITEMS.filter((w) => !w.dollarCol);
  const ids = unpricedItems.map((w) => w.id);
  const multi = useMultiFindingData(ids, filters.window, filters.workspaceIds, filters.envs, 2000);
  const findingsById = allFindingsIndex || {};
  const ready = multi.phase === "ready";

  const rows = unpricedItems.map((item) => {
    const f = findingsById[item.id];
    const band = f ? bandOf(f) : "NOT_ASSESSED";
    const st = multi.byId[item.id];
    const okRows = st && st.phase === "ready" && st.outcome === "ok_rows" ? st.data.rows : null;
    const flaggedCount = okRows ? okRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN").length : 0;
    return { id: item.id, area: item.area, band, isFlagged: band === "CRITICAL" || band === "WARN", flaggedCount, rows: okRows ? okRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null, finding: f };
  });
  const flagged = rows.filter((r) => r.isFlagged);
  const notFlagged = rows.filter((r) => !r.isFlagged && r.id !== "cost_premium_serverless_photon");

  // cost_premium_serverless_photon (INVENTORY, spend not waste) always sits in "not counted".
  const spendItem = WASTE_ITEMS.find((w) => w.id === "cost_premium_serverless_photon");
  const spendLabel = spendItem ? getCheckLabel(spendItem.id, spendItem.id) : null;

  const sentence = !ready ? "Loading flagged, unpriced checks..."
    : flagged.length > 0
      ? `${fmtInt(flagged.length)} check${flagged.length === 1 ? "" : "s"} flagged possible waste this window with no dollar figure yet -- ranked by severity, not counted in the priced total.`
      : "Nothing flagged among the checks with no waste dollar column.";
  React.useEffect(() => { if (setVerdict) setVerdict(sentence); }, [sentence, setVerdict]);

  return (
    <div>
      <Card className="ws-flagged-card" title="Also flagged, no dollar figure yet" right={<span className="faint">{`${fmtInt(flagged.length)} check${flagged.length === 1 ? "" : "s"} · not in the priced total`}</span>}>
        {!ready ? <div className="faint">Loading...</div>
          : flagged.length === 0 ? <div className="faint">Nothing flagged this window.</div>
          : flagged.map((w) => <FlaggedRow key={w.id} w={w} dims={dims} goTo={goTo} />)}
      </Card>

      <Card className="ws-not-counted" title="Not counted as waste">
        <div className="ws-not-counted-line">
          {spendLabel ? `${spendLabel.title} -- ${spendLabel.why}` : "Premium serverless and Photon usage -- spend, not waste (see Cost › Pricing & policy)."}
        </div>
        {notFlagged.map((r) => (
          <div className="ws-not-counted-line" key={r.id}>
            {`${getCheckLabel(r.id).title} -- ${r.band === "NOT_ASSESSED" || r.band === "ERROR" ? "couldn't be assessed in this export." : r.band === "OK" ? "nothing flagged this window." : "nothing to judge in this window."}`}
          </div>
        ))}
      </Card>
    </div>
  );
}

// A source by its name first; the check id stays for anyone looking it up in the Guide.
function MethodLine({ w }: LooseProps) {
  const label = getCheckLabel(w.id, w.id);
  return (
    <div className="ws-method-line">
      <b>{label.title}</b> <span className="mono faint">{w.id}</span>{` · ${AREA_REGISTRY[w.area].label} -- ${label.why}`}
    </div>
  );
}

function WasteMethodContent({ setVerdict }: LooseProps) {
  const sentence = "How the possible-waste total is counted: only each check's possible-waste dollars, never a resource's whole spend.";
  React.useEffect(() => { if (setVerdict) setVerdict(sentence); }, [sentence, setVerdict]);
  const priced = WASTE_ITEMS.filter((w) => w.dollarCol);
  const unpriced = WASTE_ITEMS.filter((w) => !w.dollarCol);
  return (
    <div>
      <Card title="How it's counted">
        <p className="ws-method-p">
          Each source above is a check that already runs on its own home tab (Compute, Jobs, ML & AI, Queries) --
          nothing here is a separate measurement. A check counts toward the priced total only when its own rows
          carry a possible-waste dollar column (<span className="mono">est_wasted_usd_list</span> in the SQL), the one
          column this app treats as recoverable waste. A resource's ordinary spend is never counted as waste, even on a check that is flagged for another
          reason -- for example a warehouse's idle-time estimate only prices the idle stretch, never its whole bill.
        </p>
        <p className="ws-method-p">
          A check with no dollar column ranks by how many rows it flags instead (Flagged, not priced). A source
          whose own rows can overlap with another (idle cluster minutes during a failed job run, say) says so once,
          rather than silently double-counting or subtracting an estimate no query here actually computes.
        </p>
      </Card>
      <Card title="Priced sources">
        {priced.map((w) => <MethodLine key={w.id} w={w} />)}
      </Card>
      <Card title="Flagged, no dollar figure">
        {unpriced.filter((w) => w.id !== "cost_premium_serverless_photon").map((w) => <MethodLine key={w.id} w={w} />)}
      </Card>
    </div>
  );
}

AreaContent.register("waste", "priced", WastePricedContent);
AreaContent.register("waste", "unpriced", WasteUnpricedContent);
AreaContent.register("waste", "method", WasteMethodContent);
