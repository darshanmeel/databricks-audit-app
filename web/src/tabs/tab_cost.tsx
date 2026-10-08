// Cost area content (redesign section 6): trend / product /
// allocation / resource / pricing sub-tabs. Each registers with AreaContent (primitives.tsx),
// which already renders PageIntro + SubTabs + this component + the checks table -- this file only
// owns the cards and panels in between. Old strip/SubTabNav body fully replaced.

import React from "react";

import { Api } from "../api";
import { estLabel, fmtChange, fmtDate, fmtDayMonth, fmtDayShort, fmtDbu, fmtInt, fmtMoney, fmtMoneyShort, fmtPct } from "../format";
import { Ref, resolveName, useNames, warehouseInWorkspace, wsPhrase } from "../components/names";
import { SPIKE_PCT, buildMonthBars, cappedRowsNote, clusterName, excludedNoWorkspaceNote, fillDays, fmtChangePct, groupSum, groupsToEntries, jobName, numOrZero, regroupByKeyIndex, sumBy, topNWithOther, totalSpikeDays, useFindingAgg, useFindingData, warehouseName } from "../components/hooks";
import { tagValueLabel } from "../components/tag_picker";
import { COST_MONEY_COL } from "../components/tab_registry";
import { AreaContent, Card, CaveatNote, LinkOut, MetricCard, MetricGrid, StatusPill } from "../components/primitives";
import { enumLabel } from "../components/labels";
import { ChartNote, Donut, HBarList, SubTabNav, paletteColor, shortId } from "../components/charts";
import { DAY_ROWS_MAX, GrainSwitch, MONTH_ROWS_MAX, fmtMonth, latest, monthOf } from "../components/grain";
import type { Grain } from "../components/grain";
import { TagRollupView, TopTagSwitch, allocBars, allocSplit, normalizeTagKeyLike, tagLabelMid, useTagAllocation, useTopTags, withSameTagKeys } from "../components/tag_rollup";
import { Columns, DivergingBars, RankedBars } from "../components/charts_more";
import { isNotBuilt } from "../components/scope";
import { aggregateGroupTileProps, aggregateTileProps, singleIdTileProps, windowFacts } from "../components/overview_tile";
import { productLabel } from "./tab_overview";
import { productWords } from "./tab_money";
import { TrendByCard } from "./cost_trend_by";
import type { AggKey, AggregateData, AggState, Entry, Filters, FindingState, FindingSummary, Id, Row, WorkspaceTagRow, WorkspaceTagsAnswer } from "../types";
import type { MonthBar } from "../components/hooks";
import type { TabProps } from "../components/primitives";
import type { TileProps } from "../components/overview_tile";
import type { NoteState, Segment } from "../components/charts";

// The overview_tile.tsx helpers (aggregateTileProps/aggregateGroupTileProps/singleIdTileProps)
// return `.reading`/`.severity` on every non-ok_rows outcome (loading/error/empty/not_assessed),
// but this file's own compute() callbacks return the more compact `.note`/`.tone` for the ok_rows
// case -- these two read whichever the tile actually set, so a MetricCard shows the right words on
// every outcome, not just the happy path.
function tileNote(t: TileProps | null): React.ReactNode { return (t && (t.note != null ? t.note : t.reading)) || null; }
const SEVERITY_TONE: Record<string, string> = { critical: "crit", warn: "warn", not_assessed: "na" };
function tileTone(t: TileProps | null): string | null { return (t && (t.tone || SEVERITY_TONE[String(t.severity)])) || null; }

// "20 Sep" -- day before month, no year (fmtDate's "Sep 20, 2026" carries a year this card has no
// room for and reads out of the app's own day-month order).
function shortDayMonth(dateStr: unknown): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(dateStr || ""));
  if (!m) return String(dateStr || "");
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${Number(m[3])} ${names[Number(m[2]) - 1] || m[2]}`;
}

function costGridMax(grid: number[][] | null): number {
  let m = 0;
  (grid || []).forEach((row) => (row || []).forEach((v) => { if (v > m) m = v; }));
  return m > 0 ? m : 1;
}

// month tone -> its bar colour, shared by every page that draws buildMonthBars' output (hooks.ts)
// -- "steady" (3 full priors, within +-15%) reads --c1, same as before; a month with fewer than 3
// full priors carries no tone at all and must never reuse that same blue, or it silently reads as
// a verdict ("steady") this app never actually reached.
const MONTH_TONE_VAR: Record<string, string> = { crit: "var(--st-crit)", warn: "var(--st-warn)", calm: "var(--st-ok)", steady: "var(--c1)" };
function monthToneColor(tone: string | null): string { return MONTH_TONE_VAR[String(tone)] || "var(--c-other)"; }

// MonthBarLegend -- the one key Cost's "By calendar month" and Money's month chart show beside
// buildMonthBars' bars, so the colour rule reads the same everywhere.
const MONTH_LEGEND: [string | null, string][] = [
  ["crit", "Up 30% or more"],
  ["warn", "Up 15% or more"],
  ["steady", "Steady"],
  ["calm", "Down 15% or more"],
  [null, "Under 3 months of history"],
];
function MonthBarLegend() {
  return (
    <div className="month-legend">
      <div className="month-legend-title">vs 3-month average</div>
      {MONTH_LEGEND.map(([tone, text]) => (
        <div key={text} className="month-legend-row">
          <span className="month-legend-swatch" style={{ background: monthToneColor(tone) }} />{text}
        </div>
      ))}
      <div className="month-legend-row">
        <span className="month-legend-swatch month-bar-fill partial" />Month in progress
      </div>
    </div>
  );
}

// MonthChart -- the bars with their key on the right (below on a narrow screen).
export function MonthChart({ months }: { months: MonthBar[] }) {
  return (
    <React.Fragment>
      <div className="month-chart">
        <CostMonthBars months={months} />
        <MonthBarLegend />
      </div>
      <MonthChartSummary months={months} />
    </React.Fragment>
  );
}

// The bars' values in one line: this month so far, the last full month with its change, and the high.
function MonthChartSummary({ months }: { months: MonthBar[] | null }) {
  if (!months || !months.length) return null;
  const current = months.find((m) => m.partial);
  const full = months.filter((m) => !m.partial);
  const last = full.length ? full[full.length - 1] : null;
  const high = full.length ? full.reduce((a, b) => (b.total > a.total ? b : a)) : null;
  const parts = [
    current ? `${current.label} so far ${fmtMoney(current.total, 0)}` : null,
    last ? `${last.label} ${fmtMoney(last.total, 0)}${last.changePct != null ? `, ${fmtChangePct(last.changePct)} vs its 3-month average` : ""}` : null,
    high && high !== last ? `High ${high.label} ${fmtMoney(high.total, 0)}` : null,
  ].filter(Boolean);
  return parts.length ? <div className="month-chart-summary muted">{parts.join(" · ")}</div> : null;
}

// CostMonthBars -- calendar-month bars, coloured by buildMonthBars (hooks.ts) against each
// month's own trailing 3-month average -- the one rendering both Cost's Trend and Money read, so
// the same month can never be a different colour on the two pages.
function CostMonthBars({ months }: { months: MonthBar[] }) {
  const max = Math.max(1, ...months.map((m) => m.total));
  return (
    <div className="month-bars">
      {months.map((m) => (
        <div className="month-bar-col" key={m.month}>
          <div className="month-bar-track">
            <div
              className={`month-bar-fill${m.partial ? " partial" : ""}`}
              style={{ height: `${Math.max(2, (m.total / max) * 100)}%`, background: m.partial ? undefined : monthToneColor(m.tone) }}
              title={`${m.label}: ${fmtMoney(m.total, 0)}${m.partial ? " (in progress)" : m.changePct != null ? ` (${fmtChangePct(m.changePct)} vs trailing 3-mo avg)` : " (fewer than 3 full months on record)"}`}
            />
          </div>
          <div className="month-bar-label">{m.shortLabel}</div>
        </div>
      ))}
    </div>
  );
}

const WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function hourCellColor(pct: number): string {
  if (!(pct > 0)) return "var(--seq-0)";
  return `var(--heat-${Math.min(5, Math.max(1, Math.ceil(pct * 5)))})`;
}
// CostHourHeatmap -- a plain weekday x hour grid (not the shared Heatmap primitive, which has no
// room for a second job/query figure in its tooltip); UTC, native title tooltips.
function CostHourHeatmap({ usdGrid, jobsGrid, queriesGrid }: { usdGrid: number[][]; jobsGrid: number[][]; queriesGrid: number[][] }) {
  const max = costGridMax(usdGrid);
  return (
    <div className="hour-heatmap">
      <div className="hh-row hh-header">
        <div className="hh-row-label" />
        {Array.from({ length: 24 }, (_, h) => <div key={h} className="hh-hour-label">{h % 3 === 0 ? h : ""}</div>)}
      </div>
      {WEEKDAY_SHORT.map((wd, wi) => (
        <div className="hh-row" key={wd}>
          <div className="hh-row-label">{wd}</div>
          {Array.from({ length: 24 }, (_, h) => {
            const v = usdGrid[wi][h] || 0;
            const jobs = (jobsGrid[wi] && jobsGrid[wi][h]) || 0;
            const queries = (queriesGrid[wi] && queriesGrid[wi][h]) || 0;
            const tip = `${wd} ${String(h).padStart(2, "0")}:00 UTC -- ${fmtMoney(v, 0)}` + (jobs || queries ? ` -- ${fmtInt(jobs)} job run${jobs === 1 ? "" : "s"}, ${fmtInt(queries)} quer${queries === 1 ? "y" : "ies"}` : "");
            return <div key={h} className="hh-cell" style={{ background: hourCellColor(v / max) }} title={tip} />;
          })}
        </div>
      ))}
    </div>
  );
}

// CostSkuSparkbars -- a small per-SKU monthly trend, plain divs (no need for an SVG axis at this size).
function CostSkuSparkbars({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  return (
    <div className="sku-spark">
      {values.map((v, i) => <div key={i} className="sku-spark-bar" style={{ height: `${Math.max(3, (v / max) * 100)}%` }} />)}
    </div>
  );
}

// ═══════════════════════ Trend ═══════════════════════
// Change vs the previous period at one level at a time: product, workspace, workspace x product
// (from cost_period_over_period), or warehouse / cluster / job (from their chargeback checks,
// fetched only when picked).
const CHANGE_LEVELS = [
  { key: "product", label: "Product" },
  { key: "workspace", label: "Workspace" },
  { key: "ws_product", label: "Workspace & product" },
  { key: "warehouse", label: "Warehouse", qid: "cost_chargeback_by_warehouse" },
  { key: "cluster", label: "Cluster", qid: "cost_chargeback_by_cluster" },
  { key: "job", label: "Job", qid: "cost_chargeback_by_job" },
];

function periodRowsBy(rows: Row[], keyFn: (r: Row) => string, valueOf: (r: Row) => number): Entry[] {
  const m = new Map<string, number>();
  rows.forEach((r) => {
    const k = keyFn(r);
    m.set(k, (m.get(k) || 0) + valueOf(r));
  });
  return [...m.entries()].map(([name, value]) => ({ name, value }));
}

// Spend (this window's total) or change vs the previous window, at the same levels.
const CHANGE_METRICS = [{ key: "spend", label: "Spend" }, { key: "change", label: "Change" }];

function ChangeBreakdownCard({ periodState, filters, maxCat }: { periodState: FindingState; filters: Filters; maxCat: number }) {
  const [level, setLevel] = React.useState("product");
  const [metric, setMetric] = React.useState("spend");
  const isSpend = metric === "spend";
  const lv = CHANGE_LEVELS.find((l) => l.key === level) || CHANGE_LEVELS[0];
  const resState = useFindingData(lv.qid || null, filters.window, filters.workspaceIds, filters.envs);
  // Spend counts every row; a change is only read where both windows could be priced.
  const judged = (r: Row) => isSpend || r.status !== "NOT_ASSESSED";
  const periodData = periodState.phase === "ready" && periodState.outcome === "ok_rows" ? periodState.data : null;
  const periodRows = periodData ? periodData.rows.filter(judged) : null;
  let items: Entry[] | null = null, state: NoteState = periodState;
  if (!lv.qid && periodData && periodRows) {
    const net = 1 - numOrZero(periodData.discount_pct);
    const valueOf = (r: Row) => numOrZero(isSpend ? r.est_current_usd_list : r.est_change_usd_list) * net;
    items = level === "product" ? periodRowsBy(periodRows, (r: Row) => productWords(r.billing_origin_product), valueOf)
      : level === "workspace" ? periodRowsBy(periodRows, (r: Row) => wsPhrase(r.workspace_id), valueOf)
      : periodRowsBy(periodRows, (r: Row) => `${productWords(r.billing_origin_product)} · ${wsPhrase(r.workspace_id)}`, valueOf);
  } else if (lv.qid) {
    state = resState;
    const resData = resState.phase === "ready" && resState.outcome === "ok_rows" ? resState.data : null;
    const rows = resData ? resData.rows.filter((r) => judged(r) && (isSpend || !r.is_other)) : null;
    const net = resData ? 1 - numOrZero(resData.discount_pct) : 1;
    const nameOf = level === "warehouse" ? (r: Row) => warehouseInWorkspace(r.workspace_id, r.warehouse_id)
      : level === "job" ? (r: Row) => `${r.job_name || resolveName("job", r.workspace_id, r.job_id) || `job ${r.job_id}`} · ${wsPhrase(r.workspace_id)}`
      : (r: Row) => `${r.name || r.entity_id} · ${wsPhrase(r.workspace_id)}`;
    // The warehouse check names its columns usd_list/change_usd_list; the others est_*.
    const valueOf = isSpend
      ? (r: Row) => numOrZero(r.usd_list != null ? r.usd_list : r.est_current_usd_list) * net
      : (r: Row) => numOrZero(r.change_usd_list != null ? r.change_usd_list : r.est_change_usd_list) * net;
    // A check's pooled remainder row is still spend: shown as its own bar, never dropped.
    items = rows ? rows.map((r) => ({ name: r.is_other ? `Other ${lv.label.toLowerCase()}s` : nameOf(r), value: valueOf(r) })) : null;
  }
  return (
    <Card
      title={isSpend ? `Spend by ${lv.label.toLowerCase()}` : "Biggest changes vs previous period"}
      right={(
        <div className="ws-actions">
          {CHANGE_METRICS.map((m) => (
            <button key={m.key} type="button" className={m.key === metric ? "on" : ""} onClick={() => setMetric(m.key)}>{m.label}</button>
          ))}
        </div>
      )}
    >
      <div className="ws-actions">
        {CHANGE_LEVELS.map((l) => (
          <button key={l.key} type="button" className={l.key === level ? "on" : ""} onClick={() => setLevel(l.key)}>{l.label}</button>
        ))}
      </div>
      {items && isSpend ? (
        <RankedBars
          items={items}
          n={maxCat}
          valueFmt={(v) => fmtMoney(v, 0)}
          unitLabel="$"
          emptyText="No spend in this window."
          note={`Total for the last ${filters.window} days.`}
        />
      ) : items ? (
        <DivergingBars
          items={items}
          n={maxCat}
          valueFmt={(v) => `${v >= 0 ? "+" : "-"}${fmtMoney(Math.abs(v), 0)}`}
          unitLabel="Change"
          note="This period against the one just before it, same length. 25% up is over the warning line, 50% is critical."
        />
      ) : <ChartNote state={state} label={lv.qid || "cost_period_over_period"} />}
    </Card>
  );
}

function CostTrendContent({ findings, filters, meta, maxCat, onVerdict }: TabProps) {
  useNames(); // re-render once workspace names (names.tsx) are ready -- resolveName below is read at render time
  const dailyAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["usage_date"], "sum", COST_MONEY_COL);
  const periodState = useFindingData("cost_period_over_period", filters.window, filters.workspaceIds, filters.envs);
  const spikeState = useFindingData("cost_daily_spikes", filters.window, filters.workspaceIds, filters.envs);
  const monthAgg = useFindingAgg("cost_monthly_actuals", filters.window, filters.workspaceIds, filters.envs, ["month_start", "is_partial_month"], "sum", "net_list_cost_usd");
  const hourState = useFindingData("cost_by_hour_of_day", filters.window, filters.workspaceIds, filters.envs);

  const winFacts = windowFacts(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);
  const findingsById = React.useMemo(() => {
    const idx: Record<string, FindingSummary> = {}; (findings || []).forEach((f) => { idx[f.query_id] = f; }); return idx;
  }, [findings]);

  const dailyReady = dailyAgg.phase === "ready" && dailyAgg.outcome === "ok_rows";
  // fillDays (hooks.ts): a day with no billed row is a real $0, not a gap -- skipping it inflates
  // the daily average and misaligns the spike scan's "7 days before" window.
  const daily = dailyReady
    ? fillDays(
        dailyAgg.data.groups.map((g) => ({ day: String(g.key[0]).slice(0, 10), value: numOrZero(g.value) })),
        meta && meta.as_of_date, filters.window, meta && meta.snapshot_days
      )
    : null;
  const avg = daily && daily.length ? daily.reduce((s, d) => s + d.value, 0) / daily.length : null;
  const dailyShown = daily ? latest(daily, DAY_ROWS_MAX) : null;

  const spikeRows = spikeState.phase === "ready" && spikeState.outcome === "ok_rows" ? spikeState.data.rows : null;
  const totalSpikes = totalSpikeDays(daily);
  const markers = totalSpikes.map((s) => ({ day: s.day, label: `Spike: ${fmtMoney(s.value, 0)}, ${fmtChange(s.value, s.median, true)} vs the 7-day median` }));
  const worstSpike = totalSpikes.length ? [...totalSpikes].sort((a, b) => b.pct - a.pct)[0] : null;
  // "Spike days" counts distinct calendar days, never the flagged workspace x product ROWS (one
  // day can carry several flagged rows) -- a server-side count_distinct over usage_date.
  const spikeDaysAgg = useFindingAgg("cost_daily_spikes", filters.window, filters.workspaceIds, filters.envs, [], "count_distinct", "usage_date", { statuses: ["CRITICAL", "WARN"] });
  const spikeDaysReady = spikeDaysAgg.phase === "ready" && spikeDaysAgg.outcome === "ok_rows";
  const spikeDaysCount = spikeDaysReady ? numOrZero(spikeDaysAgg.data.total_value) : null;

  // The headline spend total -- periodTile's own judged-rows total is compared against this below,
  // since any unpriced usage now withholds a row's own verdict.
  const spendTotal = dailyReady ? numOrZero(dailyAgg.data.total_value) * (1 - numOrZero(dailyAgg.data.discount_pct)) : null;

  const spendTile = aggregateGroupTileProps(dailyAgg, () => ({
    value: fmtMoney(spendTotal, 0),
    facts: [...winFacts, avg != null ? { label: "Daily avg", value: fmtMoney(avg, 0) } : null].filter((f) => f != null),
  }), "No billed usage recorded in this window.");

  const periodTile = aggregateTileProps(periodState, (rows, data) => {
    const judged = rows.filter((r) => r.status !== "NOT_ASSESSED");
    if (!judged.length) return { value: "-", facts: [{ label: "Status", value: "Nothing could be judged this window", tone: "muted" }], tone: "na" };
    const disc = numOrZero(data.discount_pct);
    const cur = sumBy(judged, "est_current_usd_list") * (1 - disc);
    const prev = sumBy(judged, "est_previous_usd_list") * (1 - disc);
    const change = cur - prev;
    const pct = prev > 0 ? (change / prev) * 100 : null;
    const flagged = judged.filter((r) => r.status === "CRITICAL" || r.status === "WARN");
    const mover = (flagged.length ? flagged : judged).slice()
      .sort((a, b) => Math.abs(numOrZero(b.est_change_usd_list)) - Math.abs(numOrZero(a.est_change_usd_list)))[0];
    const moverWs = mover && (mover.workspace_id == null || mover.workspace_id === "") ? "Account-level (no workspace)" : mover ? wsPhrase(mover.workspace_id) : null;
    const moverName = mover ? `${mover.billing_origin_product ? productLabel(mover.billing_origin_product) : "Spend"} · ${moverWs}` : null;
    // Some usage stayed unpriced on at least one side (NOT_ASSESSED rows dropped above) -- the
    // judged total this change is measured on can fall short of the headline spend, so say so
    // rather than let the two dollar figures silently disagree.
    const comparedOn = spendTotal != null && Math.abs(cur - spendTotal) > 1
      ? { label: "Priced in both periods", value: fmtMoney(cur, 0), detail: `of ${fmtMoney(spendTotal, 0)} spend; the rest has no list price for part of it`, tone: "muted" }
      : null;
    return {
      value: `${change >= 0 ? "+" : "-"}${fmtMoney(Math.abs(change), 0)}`,
      facts: [
        pct != null ? { label: "Change", value: fmtChange(cur, prev, true) } : null,
        moverName ? { label: "Mover", value: moverName } : null,
        comparedOn,
      ].filter((f) => f != null),
      // The figure's own colour follows the direction it moved (a fall is never shown as critical
      // red) -- the check's real CRITICAL/WARN status is its own pill in the checks table below.
      tone: change > 0.5 ? "warn" : null,
    };
  }, "No billed usage recorded in this window or the one before it.");

  const spikeTile = singleIdTileProps("Spikes", findingsById.cost_daily_spikes, spikeState, (flagged) => {
    if (!flagged.length) return {};
    const worst = [...flagged].sort((a, b) => numOrZero(b.spike_ratio_actual) - numOrZero(a.spike_ratio_actual))[0];
    return {
      value: spikeDaysReady ? fmtInt(spikeDaysCount) : fmtInt(flagged.length),
      reading: `Worst: ${fmtDate(worst.usage_date) || worst.usage_date}, ${worst.billing_origin_product ? productLabel(worst.billing_origin_product) : "spend"} in ${wsPhrase(worst.workspace_id)}, ${fmtMoney(numOrZero(worst.est_day_usd_list), 0)}.`,
      facts: [
        { label: "Worst day", value: fmtDate(worst.usage_date) || worst.usage_date },
        { label: "Where", value: `${worst.billing_origin_product ? productLabel(worst.billing_origin_product) : "spend"} in ${wsPhrase(worst.workspace_id)}` },
        { label: "Spike", value: fmtMoney(numOrZero(worst.est_day_usd_list), 0), tone: "warn" },
      ],
    };
  }, "No spike above its own trailing baseline this window.");

  const monthReady = monthAgg.phase === "ready" && monthAgg.outcome === "ok_rows";
  // buildMonthBars (hooks.ts) colours every month against its own trailing 3 FULL months from the
  // whole history returned, then this page keeps the last 18 (17 full + the current partial) --
  // the same function Money's own month chart calls, so the two can never disagree on a month's
  // colour just because one page sliced to a different length first.
  const monthsForBars = monthReady ? buildMonthBars(monthAgg.data.groups, monthAgg.data.discount_pct, MONTH_ROWS_MAX) : null;
  const lastFull = monthsForBars ? [...monthsForBars].reverse().find((m) => !m.partial) : null;

  // When you spend: weekday x hour, UTC (cost_by_hour_of_day carries no browser/workspace
  // timezone). job_runs_active/queries_started repeat on every product row of the same
  // workspace+weekday+hour slot (the finding's own grain), so they are counted once per slot
  // (first row wins) before being added across workspaces -- usd_list carries no such repeat and
  // is summed straight across every row.
  const hourReady = hourState.phase === "ready" && hourState.outcome === "ok_rows";
  let hourGrids: { usdGrid: number[][]; jobsGrid: number[][]; queriesGrid: number[][]; maxUsd: number } | null = null;
  let peakLine: string | null = null;
  if (hourReady) {
    const usdGrid = Array.from({ length: 7 }, () => Array(24).fill(0));
    const jobsGrid = Array.from({ length: 7 }, () => Array(24).fill(0));
    const queriesGrid = Array.from({ length: 7 }, () => Array(24).fill(0));
    const seenSlot = new Set<string>();
    (hourState.data.rows || []).forEach((r) => {
      const wd = Number(r.weekday_num), hr = Number(r.hour_of_day);
      if (!(wd >= 0 && wd <= 6) || !(hr >= 0 && hr <= 23)) return;
      usdGrid[wd][hr] += numOrZero(r.usd_list);
      const slotKey = `${r.workspace_id}|${wd}|${hr}`;
      if (!seenSlot.has(slotKey)) {
        seenSlot.add(slotKey);
        jobsGrid[wd][hr] += numOrZero(r.job_runs_active);
        queriesGrid[wd][hr] += numOrZero(r.queries_started);
      }
    });
    hourGrids = { usdGrid, jobsGrid, queriesGrid, maxUsd: costGridMax(usdGrid) };
    const hourTotals = Array.from({ length: 24 }, (_, h) => ({ h, total: usdGrid.reduce((s, row) => s + row[h], 0) }));
    const grandTotal = hourTotals.reduce((s, e) => s + e.total, 0);
    const topHours = [...hourTotals].sort((a, b) => b.total - a.total).slice(0, 3).filter((e) => e.total > 0);
    if (topHours.length && grandTotal > 0) {
      const share = (topHours.reduce((s, e) => s + e.total, 0) / grandTotal) * 100;
      peakLine = `Peak hours (UTC): ${topHours.map((e) => `${String(e.h).padStart(2, "0")}:00`).join(", ")} -- ${fmtPct(share, 0)} of this window's spend.`;
    }
  }

  // A partial snapshot (10 days captured under a 30d window) never says "the last 30 days" --
  // that names days the export does not have; daily.length is the actual span read.
  const verdictSentence = dailyReady
    ? `${spendTile.value} over the last ${daily ? daily.length : filters.window} days${periodTile.value && periodTile.value !== "-" ? `, ${periodTile.value} vs the previous period` : ""}.${totalSpikes.length > 0 ? ` ${fmtInt(totalSpikes.length)} spike day${totalSpikes.length === 1 ? "" : "s"}.` : ""}`
    : "Loading this window's spend...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  const excludedNote = dailyReady ? excludedNoWorkspaceNote(dailyAgg.data) : null;

  return (
    <div>
      <MetricGrid>
        <MetricCard label="Spend" value={spendTile.value} facts={spendTile.facts} note={tileNote(spendTile)} />
        <MetricCard label="Vs previous period" value={periodTile.value} facts={periodTile.facts} note={tileNote(periodTile)} tone={tileTone(periodTile)} />
        <MetricCard label="Spike days" value={daily ? fmtInt(totalSpikes.length) : "-"} tone={totalSpikes.length ? "warn" : null} facts={daily ? [
          worstSpike ? { label: "Worst", value: fmtDayShort(worstSpike.day), detail: `${fmtMoney(worstSpike.value, 0)}, ${fmtChange(worstSpike.value, worstSpike.median, true)}`, tone: "warn" } : null,
          { label: "Rule", value: `day's total ${SPIKE_PCT}%+ over the 7-day median` },
          spikeDaysReady ? { label: "In one workspace & product", value: `${fmtInt(spikeDaysCount)} days`, detail: "a line 2x its own median" } : null,
        ].filter((f) => f != null) : null} />
        <MetricCard label="Last full month" value={lastFull ? fmtMoney(lastFull.total, 0) : "-"} facts={lastFull ? [{ label: "Month", value: lastFull.label }] : null} />
      </MetricGrid>
      {excludedNote && <CaveatNote>{excludedNote}</CaveatNote>}

      <Card title="Daily spend" right={<span className="muted">{dailyReady ? estLabel(dailyAgg.data.discount_pct) : "list price (effective)"}</span>}>
        {daily && dailyShown ? (
          <Columns
            days={dailyShown.map((d) => d.day)}
            series={[{ name: "$ per day", values: dailyShown.map((d) => d.value) }]}
            markers={markers}
            partial={meta && meta.direct_export && meta.direct_export.includes_today && dailyShown.length ? [dailyShown[dailyShown.length - 1].day] : null}
            valueFmt={(v) => fmtMoneyShort(v)}
            unitLabel="$"
            dollarAxis
            note={avg != null ? `Each bar is one day, all workspaces and products${daily.length > dailyShown.length ? `, latest ${dailyShown.length} of ${daily.length} days` : ""}. ${daily.length}-day average ${fmtMoney(avg, 0)}. Triangles mark days ${SPIKE_PCT}% or more above the median of the 7 days before.` : null}
          />
        ) : <ChartNote state={dailyAgg} label="cost_dollarized_by_sku_day" />}
      </Card>

      <div className="grid-2">
        <ChangeBreakdownCard periodState={periodState} filters={filters} maxCat={maxCat} />
        <Card title="By calendar month" right={<span className="muted">{`last ${MONTH_ROWS_MAX - 1} months + this month`}</span>}>
          {monthsForBars && monthsForBars.length ? (
            <MonthChart months={monthsForBars} />
          ) : <ChartNote state={monthAgg} label="cost_monthly_actuals" />}
        </Card>
      </div>

      <TrendByCard filters={filters} meta={meta} />

      <Card title="When you spend" right={<span className="muted">weekday x hour, UTC</span>}>
        {hourGrids ? (
          <div>
            <CostHourHeatmap {...hourGrids} />
            <div className="metric-note" style={{ marginTop: 8 }}>
              {`Blank = $0 an hour-slot; green to red as spend rises, deepest red = up to ${fmtMoney(hourGrids.maxUsd, 0)}.`}
              {peakLine ? ` ${peakLine}` : ""}
            </div>
          </div>
        ) : <ChartNote state={hourState} label="cost_by_hour_of_day" />}
      </Card>
    </div>
  );
}
AreaContent.register("cost", "trend", CostTrendContent);

// ═══════════════════════ By product & SKU ═══════════════════════
function CostProductContent({ filters, maxCat, onVerdict }: TabProps) {
  // No server-side `top` here -- RankedBars does its own top-N-plus-Other fold (n=maxCat below);
  // folding twice (once here, once inside RankedBars) would re-rank the first fold's own "Other"
  // bucket as if it were one more real SKU, competing on size with the real head items.
  const skuAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["sku_name"], "sum", COST_MONEY_COL);
  const prodState = useFindingData("cost_by_billing_origin_product", filters.window, filters.workspaceIds, filters.envs);
  const premAgg = useFindingAgg("cost_premium_serverless_photon", filters.window, filters.workspaceIds, filters.envs, ["is_serverless", "is_photon"], "sum", "net_usage_quantity");
  const splitState = useFindingData("overview_serverless_classic_split", filters.window, filters.workspaceIds, filters.envs);
  // Not windowed by filters.window -- cost_sku_trend_12m always covers the last 12 full calendar
  // months plus the current partial one, regardless of the 7/30/90-day selector.
  const skuTrendState = useFindingData("cost_sku_trend_12m", filters.window, filters.workspaceIds, filters.envs);

  const skuReady = skuAgg.phase === "ready" && skuAgg.outcome === "ok_rows";
  const skuItems = skuReady ? groupsToEntries(skuAgg.data, (k) => String((k && k[0]) || "(unlabeled SKU)")) : null;
  const topShare = skuReady && skuAgg.data.total_value > 0 ? (numOrZero(skuAgg.data.groups[0] && skuAgg.data.groups[0].value) / numOrZero(skuAgg.data.total_value)) * 100 : null;

  const prodReady = prodState.phase === "ready" && prodState.outcome === "ok_rows";
  // The header's own rule: non-DBU families (storage bytes, tokens, hours) are on their own scale
  // and must never be summed into DBUs -- DBU rows only, so every bar in this ranking is comparable.
  const prodItems = prodReady ? groupSum(prodState.data.rows.filter((r) => r.usage_unit === "DBU"), (r) => productLabel(r.billing_origin_product), "net_usage_quantity") : null;

  const premReady = premAgg.phase === "ready" && premAgg.outcome === "ok_rows";
  let serverlessPct: number | null = null, photonPct: number | null = null;
  // Same source and serverless rule as the Overview: Databricks' is_serverless flag misses
  // serverless-only products (model serving, Genie...), so the raw flag would disagree with it.
  const splitRows = splitState.phase === "ready" && splitState.outcome === "ok_rows" ? splitState.data.rows : null;
  const splitTotal = splitRows ? sumBy(splitRows, "total_net_dbus") : 0;
  if (splitTotal > 0) serverlessPct = (sumBy(splitRows, "serverless_net_dbus") / splitTotal) * 100;
  if (premReady) {
    const byPhoton = regroupByKeyIndex(premAgg.data, 1);
    const phTotal = byPhoton.reduce((s, e) => s + e.value, 0) || 1;
    photonPct = (numOrZero((byPhoton.find((e) => e.name === true) || {}).value) / phTotal) * 100;
  }

  // Grain is one row per workspace_id + sku_name + month; usd_*_3m_avg/status repeat on every one
  // of that workspace+SKU's own rows (the query's own GRAIN note) -- dedupe to one entry per
  // workspace first, then sum across workspaces into the per-SKU view the card shows.
  const skuTrendReady = skuTrendState.phase === "ready" && skuTrendState.outcome === "ok_rows";
  let skuTrends = null;
  if (skuTrendReady) {
    const sevRank: Record<string, number> = { CRITICAL: 0, WARN: 1, OK: 2 };
    const params = (skuTrendState.data.header && skuTrendState.data.header.params) || null;
    const paramNum = (name: string) => {
      const v = params && params[name] && Number(params[name].value);
      return Number.isFinite(v) ? v : null;
    };
    const warnGrowthPct = paramNum("warn_growth_pct"), critGrowthPct = paramNum("crit_growth_pct");
    const warnMinUsd = paramNum("warn_min_usd"), critMinUsd = paramNum("crit_min_usd"), newSkuMinUsd = paramNum("new_sku_min_usd");
    const paramsReady = [warnGrowthPct, critGrowthPct, warnMinUsd, critMinUsd, newSkuMinUsd].every((v) => v != null);

    const byWorkspace = new Map(); // sku_name -> Map(workspace_id -> {last3mAvg, prior3mAvg, first3mAvg, status})
    const monthTotals = new Map(); // sku_name -> Map(month key -> summed net_list_cost_usd across workspaces)
    (skuTrendState.data.rows || []).forEach((r) => {
      if (!byWorkspace.has(r.sku_name)) byWorkspace.set(r.sku_name, new Map());
      const wsMap = byWorkspace.get(r.sku_name);
      if (!wsMap.has(r.workspace_id)) {
        wsMap.set(r.workspace_id, {
          last3mAvg: numOrZero(r.usd_last_3m_avg), prior3mAvg: numOrZero(r.usd_prior_3m_avg),
          first3mAvg: numOrZero(r.usd_first_3m_avg), status: r.status,
        });
      }
      if (!r.is_partial_month) {
        if (!monthTotals.has(r.sku_name)) monthTotals.set(r.sku_name, new Map());
        const mMap = monthTotals.get(r.sku_name);
        const key = String(r.month_start).slice(0, 10);
        mMap.set(key, (mMap.get(key) || 0) + numOrZero(r.net_list_cost_usd));
      }
    });

    skuTrends = [...byWorkspace.entries()].map(([sku, wsMap]) => {
      const workspaces = [...wsMap.values()];
      const last3mAvg = workspaces.reduce((s, w) => s + w.last3mAvg, 0);
      const prior3mAvg = workspaces.reduce((s, w) => s + w.prior3mAvg, 0);
      const first3mAvg = workspaces.reduce((s, w) => s + w.first3mAvg, 0);
      const growthPct = prior3mAvg === 0 ? null : ((last3mAvg - prior3mAvg) / prior3mAvg) * 100;
      const newThisYear = first3mAvg === 0;
      let status;
      if (paramsReady) {
        status = growthPct != null && growthPct >= critGrowthPct && last3mAvg >= critMinUsd ? "CRITICAL"
          : growthPct != null && growthPct >= warnGrowthPct && last3mAvg >= warnMinUsd ? "WARN"
          : newThisYear && last3mAvg >= newSkuMinUsd ? "WARN"
          : "OK";
      } else {
        // Thresholds missing from the finding response -- fall back to the worst of this SKU's own
        // per-workspace statuses rather than guess at a default.
        status = workspaces.reduce((worst, w) => (sevRank[w.status] < sevRank[worst] ? w.status : worst), "OK");
      }
      const months = [...(monthTotals.get(sku) || new Map()).entries()].sort((a, b) => a[0].localeCompare(b[0]));
      return { sku, status, growthPct, last3mAvg, prior3mAvg, newThisYear, series: months.map((m) => m[1]) };
    }).sort((a, b) => (sevRank[a.status] - sevRank[b.status]) || (numOrZero(b.growthPct) - numOrZero(a.growthPct)));
  }
  // OK rows here are "growing, but not enough to flag" -- a "new this year · n/a · $0/mo" row adds
  // nothing to a "fastest-growing" list, so only a real CRITICAL/WARN verdict earns a place.
  const skuTrendsFlagged = skuTrends ? skuTrends.filter((s) => s.status === "CRITICAL" || s.status === "WARN") : null;
  const skuTrendsShown = skuTrendsFlagged ? skuTrendsFlagged.slice(0, maxCat) : null;

  const verdictSentence = skuReady
    ? `${fmtMoney(numOrZero(skuAgg.data.total_value) * (1 - numOrZero(skuAgg.data.discount_pct)), 0)} across ${fmtInt(skuAgg.data.group_count)} SKUs; the top SKU is ${fmtPct(topShare, 0)} of it.`
    : "Loading spend by product and SKU...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <MetricGrid>
        <MetricCard label="Spend" value={skuReady ? fmtMoney(numOrZero(skuAgg.data.total_value) * (1 - numOrZero(skuAgg.data.discount_pct)), 0) : "-"} facts={skuReady ? [{ label: "Basis", value: estLabel(skuAgg.data.discount_pct) }] : null} />
        <MetricCard label="Top SKU's share" value={topShare != null ? fmtPct(topShare, 0) : "-"} facts={skuReady ? [{ label: "Scope", value: `${fmtInt(skuAgg.data.group_count)} SKUs` }] : null} />
        <MetricCard label="Serverless share" value={serverlessPct != null ? fmtPct(serverlessPct, 0) : "-"} facts={[{ label: "Basis", value: "DBUs" }]} />
        <MetricCard label="Photon share" value={photonPct != null ? fmtPct(photonPct, 0) : "-"} facts={[{ label: "Basis", value: "DBUs" }]} />
      </MetricGrid>

      <Card title="Spend by SKU" right={<span className="muted">{skuReady ? estLabel(skuAgg.data.discount_pct) : "list price (effective)"}</span>}>
        {skuItems ? <RankedBars items={skuItems} n={maxCat} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText="No SKU rows in this window." /> : <ChartNote state={skuAgg} label="cost_dollarized_by_sku_day" />}
      </Card>

      <Card title="Fastest-growing SKUs" right={<span className="muted">last 12 months, worst first</span>}>
        {skuTrendsShown ? (
          skuTrendsShown.length ? (
            <div className="sku-trend-list">
              {skuTrendsShown.map((s) => (
                <div className="sku-trend-row" key={s.sku}>
                  <div className="sku-trend-name mono" title={s.sku}>{s.sku}{s.newThisYear && <span className="faint"> · new this year</span>}</div>
                  <CostSkuSparkbars values={s.series.length ? s.series : [0]} />
                  <div className={`sku-trend-growth${s.status === "CRITICAL" ? " tone-crit" : s.status === "WARN" ? " tone-warn" : ""}`}>
                    {s.growthPct != null ? fmtChange(s.last3mAvg, s.prior3mAvg, true) : "n/a"}
                  </div>
                  <div className="sku-trend-usd mono">{fmtMoney(s.last3mAvg, 0)}/mo</div>
                </div>
              ))}
            </div>
          ) : <div className="chart-note ok"><div className="chart-note-text">No SKU is growing fast enough to flag this window.</div></div>
        ) : <ChartNote state={skuTrendState} label="cost_sku_trend_12m" />}
      </Card>

      <div className="grid-2">
        <Card title="DBUs by product">
          {prodItems ? <RankedBars items={prodItems} n={maxCat} categorical valueFmt={(v) => fmtDbu(v, 0)} unitLabel="DBU" emptyText="No product rows in this window." /> : <ChartNote state={prodState} label="cost_by_billing_origin_product" />}
          <div className="metric-note" style={{ marginTop: 6 }}>DBU usage only -- storage bytes and token usage are on their own scale and excluded here.</div>
        </Card>
        <Card title="Premium usage">
          {serverlessPct != null || photonPct != null ? (
            <div className="row wrap" style={{ gap: 24 }}>
              {serverlessPct != null && <Donut segments={[{ label: "Serverless", value: serverlessPct, display: fmtPct(serverlessPct, 1), color: "var(--c1)" }, { label: "Classic/other", value: 100 - serverlessPct, display: fmtPct(100 - serverlessPct, 1), color: "var(--c-other)" }]} centerLabel={fmtPct(serverlessPct, 0)} centerSub="serverless" size={104} thickness={12} />}
              {photonPct != null && <Donut segments={[{ label: "Photon", value: photonPct, display: fmtPct(photonPct, 1), color: "var(--c3)" }, { label: "Non-Photon", value: 100 - photonPct, display: fmtPct(100 - photonPct, 1), color: "var(--c-other)" }]} centerLabel={fmtPct(photonPct, 0)} centerSub="Photon" size={104} thickness={12} />}
            </div>
          ) : <ChartNote state={premAgg} label="cost_premium_serverless_photon" />}
          <div className="metric-note" style={{ marginTop: 6 }}>Worth checking premium usage is intentional, not waste on its own.</div>
        </Card>
      </div>
    </div>
  );
}
AreaContent.register("cost", "product", CostProductContent);

// ═══════════════════════ Allocation (merges Chargeback + By tag, one $ basis) ═══════════════════════
// Tagged and untagged dollars per day and product for one tag, both read off tags.cost_day so they
// add up to the day's spend (the plain check may hold fewer days than the tag table).
const TAG_DAY_GROUP = ["usage_date", "billing_origin_product"];
function useTagDaySplit(filters: Filters, key: string | null) {
  const q = key ? "cost_dollarized_by_sku_day" : null;
  const tagged = useFindingAgg(q, filters.window, filters.workspaceIds, filters.envs, TAG_DAY_GROUP, "sum", COST_MONEY_COL, { extraTags: key ? [key] : null });
  const untagged = useFindingAgg(q, filters.window, filters.workspaceIds, filters.envs, TAG_DAY_GROUP, "sum", COST_MONEY_COL, { extraTags: key ? [`${key}:__untagged__`] : null });
  return { key, tagged, untagged };
}

// "<1%" and ">99%" so a sliver is never rounded to 0% or 100%.
function tagSharePct(p: number): string {
  if (p > 0 && p < 1) return "<1%";
  if (p > 99 && p < 100) return ">99%";
  return fmtPct(p, 0);
}

// Each day's spend and the share carrying each top tag on its query, job, compute or workspace.
function TagShareByDayCard({ filters, topTags }: { filters: Filters; topTags: { key: string; label: string }[] }) {
  const splits = [
    useTagDaySplit(filters, topTags[0] ? topTags[0].key : null),
    useTagDaySplit(filters, topTags[1] ? topTags[1].key : null),
    useTagDaySplit(filters, topTags[2] ? topTags[2].key : null),
    useTagDaySplit(filters, topTags[3] ? topTags[3].key : null),
  ].filter((s) => s.key);
  const [product, setProduct] = React.useState("");
  const [grain, setGrain] = React.useState<Grain>("day");

  const states = splits.flatMap((s) => [s.tagged, s.untagged]);
  const okish = (st: AggState) => st.phase === "ready" && String(st.outcome).startsWith("ok_");
  const notOk = states.find((st) => !okish(st));
  const tagScope = states.length && states[0].data ? states[0].data.scope.tag : null;
  let body: React.ReactNode;
  if (!splits.length) body = <div className="chart-note-oneline">No top tag is set in settings (top_tags).</div>;
  else if (notOk) body = <ChartNote state={notOk} label="cost_dollarized_by_sku_day" />;
  else if (tagScope && !tagScope.applied) body = <div className="chart-note-oneline">{tagScope.reason || "Not in this export: it has no tag tables. Re-run the export."}</div>;
  else {
    const net = 1 - numOrZero(states[0].data!.discount_pct);
    // day -> product -> [tagged, untagged] per top tag
    const cells = new Map<string, Map<string, number[][]>>();
    splits.forEach((s, i) => {
      ([[s.tagged, 0], [s.untagged, 1]] as [AggState, number][]).forEach(([st, side]) => {
        ((st.data && st.data.groups) || []).forEach((g) => {
          const day = String(g.key && g.key[0]);
          const prod = String((g.key && g.key[1]) || "");
          if (!cells.has(day)) cells.set(day, new Map());
          const byProd = cells.get(day)!;
          if (!byProd.has(prod)) byProd.set(prod, splits.map(() => [0, 0]));
          byProd.get(prod)![i][side] += numOrZero(g.value) * net;
        });
      });
    });
    const prodSpend = new Map<string, number>();
    cells.forEach((byProd) => byProd.forEach((v, prod) => prodSpend.set(prod, (prodSpend.get(prod) || 0) + v[0][0] + v[0][1])));
    const products = [...prodSpend.entries()].filter(([, usd]) => usd > 0).sort((a, b) => b[1] - a[1]);
    const days = [...cells.keys()].sort().reverse().map((day) => {
      const sums = splits.map(() => [0, 0]);
      cells.get(day)!.forEach((v, prod) => {
        if (product && prod !== product) return;
        v.forEach((pair, i) => { sums[i][0] += pair[0]; sums[i][1] += pair[1]; });
      });
      return { day, spend: sums[0][0] + sums[0][1], sums };
    }).filter((d) => d.spend > 0);
    const pctOf = (pair: number[]) => (pair[0] + pair[1] > 0 ? (pair[0] / (pair[0] + pair[1])) * 100 : null);
    const tone = (p: number | null) => (p == null ? "" : 100 - p >= 50 ? " tone-crit" : 100 - p >= 20 ? " tone-warn" : "");
    const label0 = topTags[0].label;
    let summary = `No spend${product ? ` on ${productLabel(product)}` : ""} in the last ${filters.window} days.`;
    if (days.length) {
      const all = days.reduce((acc, d) => [acc[0] + d.sums[0][0], acc[1] + d.sums[0][1]], [0, 0]);
      const low = days.reduce((m, d) => (pctOf(d.sums[0])! < pctOf(m.sums[0])! ? d : m), days[0]);
      const latest = tagSharePct(pctOf(days[0].sums[0])!);
      const lowest = tagSharePct(pctOf(low.sums[0])!);
      summary = `${label0} is on ${tagSharePct(pctOf(all)!)} of ${fmtMoney(all[0] + all[1], 0)} over ${fmtInt(days.length)} days; `
        + `latest ${latest} on ${fmtDayMonth(days[0].day)}${lowest !== latest ? `, lowest ${lowest} on ${fmtDayMonth(low.day)}` : ""}.`;
    }
    // Months add up the days of this export window, so the oldest and the current month can be partial.
    const byMonth = new Map<string, { day: string; spend: number; sums: number[][] }>();
    days.forEach((d) => {
      const k = monthOf(d.day);
      const m = byMonth.get(k) || { day: k, spend: 0, sums: splits.map(() => [0, 0]) };
      m.spend += d.spend;
      d.sums.forEach((pair, i) => { m.sums[i][0] += pair[0]; m.sums[i][1] += pair[1]; });
      byMonth.set(k, m);
    });
    const rows = grain === "day" ? days : [...byMonth.values()];
    const shown = rows.slice(0, grain === "day" ? DAY_ROWS_MAX : MONTH_ROWS_MAX);
    body = (
      <React.Fragment>
        <div className="row wrap cb-tag-pick">
          <label className="cb-pick">
            <span className="af-chip-label">Resource type</span>
            <select value={product} onChange={(e) => setProduct(e.target.value)}>
              <option value="">{`All resource types (${fmtInt(products.length)})`}</option>
              {products.map(([prod, usd]) => <option key={prod} value={prod}>{`${productLabel(prod)} · ${fmtMoney(usd, 0)}`}</option>)}
            </select>
          </label>
        </div>
        <div className="money-perf-line">{summary}</div>
        {days.length > 0 && (
          <div className="tag-table-wrap">
            <table className="data tag-day-table">
              <thead><tr><th>{grain === "day" ? "Day" : "Month"}</th><th className="num">Spend</th>{splits.map((s, i) => <th key={s.key!} className="num">{topTags[i].label}</th>)}</tr></thead>
              <tbody>
                {shown.map((d) => (
                  <tr key={d.day}>
                    <td>{grain === "day" ? fmtDayMonth(d.day) : fmtMonth(d.day)}</td>
                    <td className="num mono">{fmtMoney(d.spend, 0)}</td>
                    {d.sums.map((pair, i) => {
                      const p = pctOf(pair);
                      return <td key={i} className={`num mono${tone(p)}`} title={`${fmtMoney(pair[0], 0)} of ${fmtMoney(pair[0] + pair[1], 0)} carries ${topTags[i].label}`}>{p == null ? "-" : tagSharePct(p)}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {grain === "day" && days.length > shown.length && (
          <div className="data-foot"><span className="muted mono">{`Latest ${fmtInt(shown.length)} of ${fmtInt(days.length)} days`}</span></div>
        )}
        {grain === "month" && shown.length > 0 && (
          <div className="data-foot"><span className="muted">{`Months add up the ${fmtInt(days.length)} days in this export window; the first and last can be partial.`}</span></div>
        )}
      </React.Fragment>
    );
  }
  return (
    <Card title={grain === "day" ? "Tagged share by day" : "Tagged share by month"}
      right={<span className="card-right-row"><GrainSwitch value={grain} onChange={setGrain} /><span className="muted">tag on the query, job, compute or workspace</span></span>}>
      {body}
    </Card>
  );
}

function CostAllocationContent({ filters, meta, dims, maxCat, onVerdict }: TabProps) {
  useNames(); // re-render once workspace names (names.tsx) are ready -- resolveName below is read at render time
  // DEC-74/section 7 item 1: cost_chargeback_by_allocation_tag is the ONE dollar basis here --
  // every other allocation view on this sub-tab either reads off this same id or is labelled as
  // the workspace-level view it actually is (the tag-rollup tree below). Api.aggregate accepts at
  // most 2 group columns, so the by-value cut and the per-workspace cut are two separate calls.
  // Every allocation figure below follows one top tag (the switch), read off GET /api/rollup.
  // "Who ran it" reads no tag.
  const topTags = useTopTags(meta, filters.window);
  const [chosenTag, setChosenTag] = React.useState<string | null>(null);
  const shownTag = topTags.find((t) => t.key === chosenTag) || topTags[0] || null;
  const ccView = useTagAllocation(filters, shownTag && shownTag.key);
  // Worst workspace on the headline's own basis: each dollar's most specific tag (query or job, then
  // compute, then workspace), so a workspace-tagged dollar is never counted as untagged here.
  const ccKey = ccView.phase === "ready" && ccView.data ? ccView.data.tag_key : null;
  // A Tag filter already on this key keeps untagged spend in view only when it lists "untagged".
  const ccFilterGroup = ((filters.tag && filters.tag.groups) || []).find((g) => normalizeTagKeyLike(g.key) === ccKey);
  const untaggedInView = !ccFilterGroup || ccFilterGroup.values.includes("__untagged__");
  const wsSpendAgg = useFindingAgg(ccKey && untaggedInView ? "cost_dollarized_by_sku_day" : null, filters.window, filters.workspaceIds, filters.envs, ["workspace_id"], "sum", COST_MONEY_COL);
  const wsUntaggedAgg = useFindingAgg(ccKey && untaggedInView ? "cost_dollarized_by_sku_day" : null, filters.window, filters.workspaceIds, filters.envs, ["workspace_id"], "sum", COST_MONEY_COL, { extraTags: ccKey ? [`${ccKey}:__untagged__`] : null });
  const idAgg = useFindingAgg("cost_chargeback_by_identity", filters.window, filters.workspaceIds, filters.envs, ["identity_type"], "sum", "net_usage_quantity");
  // Account-wide, 365-day population -- the window selector only narrows net_list_cost_usd (is it
  // STILL billing), never which workspaces are in scope.
  const unnamedState = useFindingData("cost_unnamed_workspaces", filters.window, filters.workspaceIds, filters.envs);

  const allocReady = ccView.phase === "ready" && ccView.outcome === "ok_rows";
  const ccSplit = allocSplit(ccView);
  const { total: grandTotal, untagged: missingTotal, items: byValue } = ccSplit;
  const missingPct = grandTotal > 0 ? (missingTotal / grandTotal) * 100 : 0;
  const ccLabel = tagLabelMid(shownTag && shownTag.label);

  // Worst workspace: highest untagged share of its own priced total, among workspaces with spend.
  // Set inside forEach below; the cast keeps TS from narrowing it to null for the rest of the function.
  let worstWs = null as { ws: AggKey; pct: number; missing: number; total: number } | null;
  const wsAggOk = (st: AggState) => st.phase === "ready" && (st.outcome === "ok_rows" || String(st.outcome).startsWith("ok_"));
  if (wsAggOk(wsSpendAgg) && wsAggOk(wsUntaggedAgg)) {
    const net = 1 - numOrZero(wsSpendAgg.data && wsSpendAgg.data.discount_pct);
    const missingBy = new Map<AggKey, number>(((wsUntaggedAgg.data && wsUntaggedAgg.data.groups) || []).map((g) => [g.key && g.key[0], numOrZero(g.value)]));
    ((wsSpendAgg.data && wsSpendAgg.data.groups) || []).forEach((g) => {
      const ws = g.key && g.key[0];
      const total = numOrZero(g.value) * net;
      const missing = (missingBy.get(ws) || 0) * net;
      if (ws == null || total <= 0 || missing <= 0) return;
      const pct = (missing / total) * 100;
      const better = !worstWs || pct > worstWs.pct
        || (pct === worstWs.pct && (missing > worstWs.missing || (missing === worstWs.missing && String(ws) < String(worstWs.ws))));
      if (better) worstWs = { ws, pct, missing, total };
    });
  }

  const idReady = idAgg.phase === "ready" && idAgg.outcome === "ok_rows";
  let notRecordedPct = null, notRecordedDbu = null, idSegments = null;
  if (idReady) {
    const entries = groupsToEntries(idAgg.data, (k) => String((k && k[0]) || "unknown"));
    const total = entries.reduce((s, e) => s + e.value, 0) || 1;
    const notRecorded = entries.find((e) => /unknown|not.?recorded/i.test(e.name));
    notRecordedPct = notRecorded ? (notRecorded.value / total) * 100 : 0;
    notRecordedDbu = notRecorded ? notRecorded.value : 0;
    idSegments = entries.map((e, i) => ({
      label: /unknown|not.?recorded/i.test(e.name) ? "Not recorded" : String(e.name).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
      value: e.value, display: fmtDbu(e.value, 0), color: paletteColor(i),
    }));
  }

  const unnamedReady = unnamedState.phase === "ready" && unnamedState.outcome === "ok_rows";
  const unnamedRows = unnamedReady ? unnamedState.data.rows : null;
  // Query orders CRITICAL (still billing) first, so rows[0] is already the worst one.
  const unnamedBilled = (unnamedRows || []).filter((r) => numOrZero(r.net_list_cost_usd) > 0);
  const unnamedBilledUsd = sumBy(unnamedBilled, "net_list_cost_usd");

  // Not run is a dash with its reason, never $0.
  const allocPendingValue = ccView.phase === "loading" ? "..." : "-";
  const allocPendingFacts = ccView.phase === "loading" ? null : [{ label: "Status", value: "Not assessed", tone: "muted" }];
  const verdictSentence = allocReady
    ? `${fmtPct(missingPct, 0)} of ${fmtMoney(grandTotal, 0)} carries no ${ccLabel} tag${worstWs ? `; worst: ${wsPhrase(worstWs.ws)} at ${fmtPct(worstWs.pct, 0)}` : ""}.`
    : "Loading cost allocation...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      {topTags.length > 1 && <TopTagSwitch tags={topTags} value={shownTag && shownTag.key} onChange={setChosenTag} />}
      <MetricGrid>
        <MetricCard label="Charged back" value={allocReady ? fmtMoney(grandTotal - missingTotal, 0) : allocPendingValue} facts={!allocReady ? allocPendingFacts : grandTotal > 0 ? [{ label: "Share", value: fmtPct(100 - missingPct, 0), detail: `of ${fmtMoney(grandTotal, 0)}` }] : null} />
        <MetricCard label={`No ${ccLabel}`} value={allocReady ? fmtMoney(missingTotal, 0) : allocPendingValue} facts={!allocReady ? allocPendingFacts : grandTotal > 0 ? [{ label: "Share", value: fmtPct(missingPct, 0), detail: "of spend", tone: missingPct >= 50 ? "crit" : missingPct >= 20 ? "warn" : undefined }] : null} tone={missingPct >= 50 ? "crit" : missingPct >= 20 ? "warn" : null} />
        <MetricCard
          label="Worst workspace"
          value={worstWs ? fmtPct(worstWs.pct, 0) : "-"}
          facts={worstWs ? [
            { label: "Where", value: wsPhrase(worstWs.ws) },
            { label: "Untagged", value: fmtMoney(worstWs.missing, 0), detail: `of ${fmtMoney(worstWs.total, 0)}`, tone: worstWs.pct >= 50 ? "crit" : worstWs.pct >= 20 ? "warn" : undefined },
          ] : null}
          tone={worstWs && worstWs.pct >= 50 ? "crit" : worstWs && worstWs.pct >= 20 ? "warn" : null}
        />
        <MetricCard label="No identity recorded" value={notRecordedPct != null ? fmtPct(notRecordedPct, 0) : "-"} facts={[{ label: "DBUs", value: notRecordedDbu != null ? fmtDbu(notRecordedDbu, 0) : "-" }]} />
      </MetricGrid>

      <CaveatNote>
        <b>One dollar basis.</b> Every figure above charges each billing row to its own <span className="mono">{ccLabel}</span> tag, at effective list price. The tags to choose from are set in config/tag_aliases.yml.
        {" "}The tag-value tree below charges whole workspaces first, then splits by tag within each one --
        label it as the workspace-level view it is when the two disagree.
      </CaveatNote>

      <Card title={`Spend by ${ccLabel}`}>
        {!allocReady ? <ChartNote state={ccView} label="rollup top" />
          : byValue.length ? <RankedBars items={allocBars(ccSplit, `No ${ccLabel}`)} n={maxCat} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" />
          : <div className="chart-note ok"><div className="chart-note-text">Every dollar of priced spend in this window carries no {ccLabel} tag -- nothing to rank by value.</div></div>}
      </Card>

      <TagShareByDayCard filters={filters} topTags={topTags} />

      <Card title="Where each dollar lands" right={<span className="muted">workspace-level view, tag rollup</span>}>
        <TagRollupView area="cost" filters={filters} meta={meta} dims={dims} maxCat={maxCat} />
      </Card>

      <WorkspaceTagsCard filters={filters} />

      <Card title="Who ran it" right={<span className="muted">DBUs, by identity on the billing row</span>}>
        {idSegments ? (
          <div>
            <Donut segments={idSegments} />
            <div className="metric-note" style={{ marginTop: 6 }}>
              &quot;Not recorded&quot; is model serving and SQL warehouse usage, which bills to the
              endpoint or warehouse rather than to whoever sent the request -- it still carries the
              cost-center tags above, it is not unallocated money.
            </div>
          </div>
        ) : <ChartNote state={idAgg} label="cost_chargeback_by_identity" />}
      </Card>

      {!unnamedRows ? (
        <Card title="Unnamed workspaces"><ChartNote state={unnamedState} label="cost_unnamed_workspaces" /></Card>
      ) : unnamedRows.length === 0 ? (
        <div className="metric-note">No workspace billed in the last 365 days is missing from this account's workspace list.</div>
      ) : (
        <Card title="Unnamed workspaces" right={<span className="muted">billed but missing from the account's workspace list</span>}>
          <MetricGrid>
            <MetricCard label="Unnamed workspaces" value={fmtInt(unnamedRows.length)} tone={unnamedBilled.length ? "crit" : null} />
            <MetricCard
              label="$ in this window" value={fmtMoney(unnamedBilledUsd, 0)}
              facts={[unnamedBilled.length
                ? { label: "Still billing", value: fmtInt(unnamedBilled.length), tone: "crit" }
                : { label: "Status", value: "None billing now", tone: "ok" }]}
              tone={unnamedBilledUsd > 0 ? "crit" : null}
            />
            <MetricCard label="Last used" value={shortDayMonth(unnamedRows[0].last_used)} facts={[{ label: "Workspace", value: shortId(unnamedRows[0].workspace_id), title: String(unnamedRows[0].workspace_id) }]} />
          </MetricGrid>
        </Card>
      )}
    </div>
  );
}
AreaContent.register("cost", "allocation", CostAllocationContent);

// Every tag seen on each workspace's bill: the values the app counts as the workspace's own tag,
// and the keys it doesn't (too little of the usage carries them, or no value dominates).
const WS_TAGS_SHOWN = 15;
export function WorkspaceTagsCard({ filters }: { filters: Filters }) {
  const wsKey = JSON.stringify([filters.workspaceIds || [], filters.envs || []]);
  const [state, setState] = React.useState<{ phase: string; data: WorkspaceTagsAnswer | null }>({ phase: "loading", data: null });
  const [showAll, setShowAll] = React.useState(false);
  React.useEffect(() => {
    let live = true;
    setState({ phase: "loading", data: null });
    Api.workspaceTags(filters.workspaceIds, filters.envs)
      .then((d) => { if (live) setState({ phase: "ready", data: d }); })
      .catch(() => { if (live) setState({ phase: "error", data: null }); });
    return () => { live = false; };
  }, [wsKey]);

  const d = state.data;
  const pct = (v: unknown) => (numOrZero(v) > 0 && numOrZero(v) < 0.01 ? "under 1%" : fmtPct(numOrZero(v) * 100, 0));
  let body: React.ReactNode;
  if (state.phase === "loading") body = <div className="muted">Loading...</div>;
  else if (state.phase === "error") body = <div className="chart-note-oneline">Could not load workspace tags.</div>;
  else if (!d || d.outcome === "not_assessed") body = <div className="chart-note-oneline">Not in this export: it has no tag tables. Re-run the export.</div>;
  else if (!d.rows.length) body = <div className="chart-note-oneline">No workspace in view carries any tag on its billed usage.</div>;
  else {
    // With a Tag filter on, only workspaces that carry the filtered tag, and what it does to them.
    const groups = ((filters.tag && filters.tag.groups) || []).map((g) => ({ ...g, norm: normalizeTagKeyLike(g.key) }));
    const hit = (r: WorkspaceTagRow, g: { norm: string; values: string[] }) => r.norm_key === g.norm && (!g.values.length || g.values.includes(r.top_value));
    const byWs = new Map<string, { id: Id; name: string; valid: WorkspaceTagRow[]; other: WorkspaceTagRow[]; rows: WorkspaceTagRow[] }>();
    d.rows.forEach((r) => {
      const k = String(r.workspace_id);
      if (!byWs.has(k)) byWs.set(k, { id: r.workspace_id, name: wsPhrase(r.workspace_id), valid: [], other: [], rows: [] });
      byWs.get(k)![r.is_valid ? "valid" : "other"].push(r);
      byWs.get(k)!.rows.push(r);
    });
    let list = [...byWs.values()];
    const filterLines = groups.map((g) => {
      const carrying = list.filter((w) => w.rows.some((r) => hit(r, g)));
      const counted = carrying.filter((w) => w.rows.some((r) => hit(r, g) && r.is_valid)).length;
      const label = `${g.display_key || g.key} = ${g.values.length ? g.values.map(tagValueLabel).join(" or ") : "any value"}`;
      if (!carrying.length) return `${label}: no workspace carries it on its billed usage, so only queries and compute tagged themselves are kept.`;
      const notCounted = carrying.length - counted;
      const one = (n: number, s: string, p: string) => (n === 1 ? s : p);
      return `${label}: ${fmtInt(counted)} workspace${one(counted, "", "s")} count${one(counted, "s", "")} it as the workspace's tag, so all ${one(counted, "its", "their")} queries and compute without a tag of their own are kept.`
        + (notCounted ? ` ${fmtInt(notCounted)} carr${one(notCounted, "ies", "y")} it on too little of ${one(notCounted, "its", "their")} usage or mixed with other values, so there only queries and compute tagged themselves are kept.` : "");
    });
    if (groups.length) list = list.filter((w) => groups.some((g) => w.rows.some((r) => hit(r, g))));
    // Workspaces with a valid tag first, then by name.
    list.sort((a, b) => Number(b.valid.length > 0) - Number(a.valid.length > 0) || a.name.localeCompare(b.name));
    const withValid = list.filter((w) => w.valid.length).length;
    const shown = showAll ? list : list.slice(0, WS_TAGS_SHOWN);
    body = (
      <React.Fragment>
        {filterLines.map((line) => <div key={line} className="money-perf-line">{line}</div>)}
        {list.length > 0 && (<React.Fragment>
        <div className="money-perf-line">
          {`${fmtInt(withValid)} of ${fmtInt(list.length)} workspaces ${groups.length ? "shown" : "with tags"} ${withValid === 1 ? "has" : "have"} at least one tag that counts. `
            + `A tag counts when it is on ${pct(d.coverage_floor)}+ of the workspace's billed usage without a usage policy (on Azure the workspace's tag is on every row) and one value has ${pct(d.share_floor)}+ of that.`}
        </div>
        <div className="tag-table-wrap">
        <table className="data ws-tags-table">
          <thead><tr><th>Workspace</th><th>Tags that count</th><th>Seen, not counted</th></tr></thead>
          <tbody>
            {shown.map((w) => (
              <tr key={String(w.id)}>
                <td>{w.name}</td>
                <td>
                  {w.valid.length ? w.valid.map((r) => (
                    <span key={r.tag_key} className="ws-tag" title={`On ${pct(r.coverage)} of this workspace's billed usage; this value on ${pct(r.share)} of it`}>
                      {`${r.tag_key}: ${r.tag_value}`}
                    </span>
                  )) : <span className="muted">none</span>}
                </td>
                <td className="muted">
                  {w.other.map((r) => (r.tag_value === "__mixed__"
                    ? `${r.tag_key}: mixed (top "${r.top_value}" ${pct(r.share)})`
                    : `${r.tag_key} (${pct(r.coverage)} of classic usage)`)).join(", ") || "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {list.length > WS_TAGS_SHOWN && (
          <div className="data-foot">
            <span className="muted mono">{`Showing ${fmtInt(shown.length)} of ${fmtInt(list.length)} workspaces`}</span>
            <button type="button" className="load-more" onClick={() => setShowAll(!showAll)}>
              {showAll ? "Show fewer" : `Show all ${fmtInt(list.length)} workspaces`}
            </button>
          </div>
        )}
        </React.Fragment>)}
      </React.Fragment>
    );
  }
  return (
    <Card title="Workspace tags" right={<span className="muted">from each workspace's classic compute bill</span>}>
      {body}
    </Card>
  );
}

// A few Cost checks measure whether an optional feature/spend category is used at all (default
// storage DSU, GenAI serving, cross-region egress) -- an ok_empty_window outcome there means the
// feature is not in use, not "quiet this window" (ChartNote's own default reading for that
// outcome), so these get their own one-line note instead of the generic empty-chart box.
function featureUnusedNote(state: AggState | FindingState, text: string) {
  if (state && state.phase === "ready" && state.outcome === "ok_empty_window") {
    return <div className="chart-note empty"><div className="chart-note-text">{text}</div></div>;
  }
  return null;
}

// ═══════════════════════ By resource ═══════════════════════
// A notebook by the last two parts of its path ("folder/name"), else its short id.
function notebookLabel(workspaceId: unknown, notebookId: unknown): string {
  const path = resolveName("notebook", workspaceId as string, notebookId as string);
  return path ? path.split("/").filter(Boolean).slice(-2).join("/") : `notebook ${shortId(notebookId)}`;
}

function CostResourceContent({ filters, dims, maxCat, onExternalJump, onVerdict }: TabProps) {
  useNames(); // notebook paths (names.tsx) are read at render time
  const jobAgg = useFindingAgg("cost_by_job", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "job_id"], "sum", "net_usage_quantity");
  const resAgg = useFindingData("cost_by_compute_resource", filters.window, filters.workspaceIds, filters.envs);
  const nbAgg = useFindingAgg("cost_by_notebook", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "notebook_id"], "sum", "net_usage_quantity");
  const dsuAgg = useFindingAgg("cost_default_storage_dsu", filters.window, filters.workspaceIds, filters.envs, ["storage_api_type"], "sum", "net_usage_quantity");
  const genaiAgg = useFindingAgg("cost_genai_token_gpu", filters.window, filters.workspaceIds, filters.envs, ["endpoint_name", "usage_unit"], "sum", "net_usage_quantity");
  // Older exports carry no usd_list: probe the columns first so the $ query is never a 422.
  const genaiProbe = useFindingData("cost_genai_token_gpu", filters.window, filters.workspaceIds, filters.envs, 1);
  const genaiHasUsd = !!(genaiProbe.data && (genaiProbe.data.columns || []).some((c) => c.name === "usd_list"));
  const genaiUsdAgg = useFindingAgg(genaiHasUsd ? "cost_genai_token_gpu" : null, filters.window, filters.workspaceIds, filters.envs, ["endpoint_name", "billing_origin_product"], "sum", "usd_list");
  const whState = useFindingData("compute_warehouse_idle_minutes", filters.window, filters.workspaceIds, filters.envs);

  // RankedBars (n=maxCat below) does its own top-N-plus-Other fold -- these stay unfolded so it is
  // the only fold, never a second one re-ranking the first fold's own "Other" bucket by size.
  const jobReady = jobAgg.phase === "ready" && jobAgg.outcome === "ok_rows";
  const jobItems = jobReady ? jobAgg.data.groups.map((g) => ({ name: jobName(dims, g.key[0], g.key[1]), value: numOrZero(g.value) })) : null;

  const resReady = resAgg.phase === "ready" && resAgg.outcome === "ok_rows";
  const resItems = resReady
    ? groupSum(resAgg.data.rows, (r) => (r.cluster_id ? clusterName(dims, r.cluster_id) : r.warehouse_id ? warehouseName(dims, r.warehouse_id) : r.instance_pool_id ? `pool ${shortId(r.instance_pool_id)}` : "(serverless / account-level)"), "net_usage_quantity")
    : null;

  // HBarList (unlike RankedBars) does no folding of its own -- pre-fold here so a long tail of
  // notebooks does not render as an unbounded list.
  const nbReady = nbAgg.phase === "ready" && nbAgg.outcome === "ok_rows";
  const nbItems = nbReady ? topNWithOther(groupsToEntries(nbAgg.data, (k) => (k && notebookLabel(k[0], k[1])) || "(unknown)"), maxCat, "Other notebooks") : null;

  const dsuReady = dsuAgg.phase === "ready" && dsuAgg.outcome === "ok_rows";
  const dsuItems = dsuReady ? groupsToEntries(dsuAgg.data, (k) => String((k && k[0]) || "(unknown)")) : null;

  // Billing records per-token serving in DBUs (usage_type TOKEN, usage_unit DBU), not in tokens:
  // show $ when the export priced it, else DBUs, and never add up rows of different units.
  const genaiUsdReady = genaiUsdAgg.phase === "ready" && genaiUsdAgg.outcome === "ok_rows";
  const genaiReady = genaiAgg.phase === "ready" && genaiAgg.outcome === "ok_rows";
  const genaiUsd = genaiUsdReady
    // Per-token use with no endpoint (Genie, AI functions) is named by its product instead.
    ? groupSum(genaiUsdAgg.data.groups.map((g) => ({ endpoint_name: g.key && g.key[0], product: g.key && g.key[1], value: numOrZero(g.value) })),
      (r) => r.endpoint_name || `${productWords(r.product)} (no endpoint)`, "value")
    : null;
  const genaiDbuGroups = genaiReady ? genaiAgg.data.groups.filter((g) => g.key && g.key[1] === "DBU") : null;
  const genaiDbu = genaiDbuGroups
    ? groupSum(genaiDbuGroups.map((g) => ({ endpoint_name: g.key[0], value: numOrZero(g.value) })), (r) => r.endpoint_name || "(no endpoint)", "value")
    : null;
  const genaiInUsd = !!(genaiUsd && genaiUsd.some((it) => it.value > 0));
  const genaiItems = genaiInUsd ? genaiUsd : genaiDbu;

  const whReady = whState.phase === "ready" && whState.outcome === "ok_rows";
  const whPriced = whReady ? whState.data.rows.filter((r) => r.est_usd_list != null) : null;
  const whTotal = whPriced ? sumBy(whPriced, "est_usd_list") : 0;
  const whTop = whPriced ? [...whPriced].sort((a, b) => numOrZero(b.est_usd_list) - numOrZero(a.est_usd_list)).slice(0, 10) : null;
  // The dot is the warehouse's own idle-time verdict; the bar stays the spend.
  const whItems = whTop ? whTop.map((r) => ({
    name: warehouseName(dims, r.warehouse_id), id: r.warehouse_id, value: numOrZero(r.est_usd_list),
    status: r.status === "CRITICAL" ? "crit" as const : r.status === "WARN" ? "warn" as const : null,
  })) : null;
  const whIdleFlagged = whTop ? whTop.filter((r) => r.status === "CRITICAL" || r.status === "WARN").length : 0;
  const whTopShare = whTop && whTotal > 0 ? (sumBy(whTop, "est_usd_list") / whTotal) * 100 : null;

  const topJob = jobItems && jobItems.length ? [...jobItems].sort((a, b) => b.value - a.value)[0] : null;
  const topRes = resItems && resItems.length ? [...resItems].sort((a, b) => b.value - a.value)[0] : null;
  const verdictSentence = topJob || topRes
    ? `Top job by DBU: ${topJob ? `${topJob.name}, ${fmtDbu(topJob.value, 0)}` : "n/a"}. Top compute resource: ${topRes ? `${topRes.name}, ${fmtDbu(topRes.value, 0)}` : "n/a"}.`
    : "Loading spend by resource...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="grid-2">
        <Card title="DBUs by job" right={<StatusPill kind="ranked" word="Ranked" compact />}>
          {jobItems ? <RankedBars items={jobItems} n={maxCat} valueFmt={(v) => fmtDbu(v, 0)} unitLabel="DBU" /> : <ChartNote state={jobAgg} label="cost_by_job" />}
        </Card>
        <Card title="DBUs by compute resource" right={<StatusPill kind="ranked" word="Ranked" compact />}>
          {resItems ? <RankedBars items={resItems} n={maxCat} valueFmt={(v) => fmtDbu(v, 0)} unitLabel="DBU" /> : <ChartNote state={resAgg} label="cost_by_compute_resource" />}
          {resReady && cappedRowsNote(resAgg.data, "resource rows") && <div className="metric-note" style={{ marginTop: 6 }}>{cappedRowsNote(resAgg.data, "resource rows")}</div>}
        </Card>
      </div>
      <Card title="Top warehouses by cost" right={<span className="muted">list price (effective)</span>}>
        {whItems ? (
          <div>
            <RankedBars items={whItems} n={10} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText="No SQL warehouse billed in this window." />
            {whTopShare != null && <div className="metric-note" style={{ marginTop: 6 }}>Top 10 of {fmtInt((whPriced || []).length)} warehouses, {fmtPct(whTopShare, 0)} of all warehouse spend.{whIdleFlagged ? ` A red or amber dot: ${fmtInt(whIdleFlagged)} of them flagged for idle time (Compute › Warehouses).` : ""}</div>}
          </div>
        ) : <ChartNote state={whState} label="compute_warehouse_idle_minutes" />}
      </Card>
      <div className="grid-2">
        <Card title="Notebooks & managed storage">
          {nbItems && <div className="metric-note" style={{ marginBottom: 4 }}>Interactive notebook DBUs, by notebook</div>}
          {nbItems ? <HBarList items={nbItems} valueFmt={(v) => fmtDbu(v, 0)} /> : <ChartNote state={nbAgg} label="cost_by_notebook" />}
          <div className="metric-note" style={{ margin: "10px 0 4px" }}>Managed storage (Delta Sync/serving)</div>
          {dsuItems ? <HBarList items={dsuItems} valueFmt={(v) => `${fmtInt(Math.round(v))} DSU`} />
            : featureUnusedNote(dsuAgg, `Managed storage (Delta Sync/serving) is not used in this account -- no DSU billed in the last ${filters.window} days.`)
            || <ChartNote state={dsuAgg} label="cost_default_storage_dsu" />}
        </Card>
        <Card title="GenAI serving by endpoint" right={<span className="muted">{genaiInUsd ? estLabel(genaiReady ? genaiAgg.data.discount_pct : 0) : "DBUs, billed per token"}</span>}>
          {genaiItems ? (genaiItems.length ? (
            <RankedBars
              items={genaiItems}
              n={maxCat}
              valueFmt={(v) => (genaiInUsd ? fmtMoney(v, 0) : `${fmtInt(Math.round(v))} DBU`)}
              unitLabel={genaiInUsd ? "$" : "DBU"}
            />
          ) : <div className="chart-note ok"><div className="chart-note-text">No GenAI serving billed in DBUs in this window.</div></div>)
            : featureUnusedNote(genaiAgg, `GenAI token/GPU serving is not used in this account -- no usage billed in the last ${filters.window} days.`)
            || <ChartNote state={genaiAgg} />}
        </Card>
      </div>
      <div className="metric-note">
        Serving endpoint cost has its own home: <LinkOut onClick={() => onExternalJump("cost_by_serving_endpoint")}>ML &amp; AI &gt; Spend →</LinkOut>
      </div>
    </div>
  );
}
AreaContent.register("cost", "resource", CostResourceContent);

// ═══════════════════════ Pricing & policy ═══════════════════════
// Products that only run on Databricks-managed compute; is_serverless is often false on them.
const SERVERLESS_ONLY_PRODUCTS = ["MODEL_SERVING", "VECTOR_SEARCH", "GENIE", "AI_FUNCTIONS", "AI_GATEWAY", "AGENT_BRICKS", "LAKEBASE", "APPS"];
const POLICY_COVERAGE_LABEL: Record<string, string> = { none: "No policy", usage_policy: "Usage policy", budget_policy_legacy: "Budget policy (legacy)" };
const TAG_COVERAGE_LABEL: Record<string, string> = { untagged: "Untagged", tagged: "Tagged" };
function CostPricingContent({ filters, maxCat, onVerdict }: TabProps) {
  const covAgg = useFindingAgg("cost_usage_policy_coverage", filters.window, filters.workspaceIds, filters.envs, ["policy_coverage", "is_serverless", "billing_origin_product"], "sum", "net_usage_quantity");
  const tagCovAgg = useFindingAgg("cost_usage_policy_coverage", filters.window, filters.workspaceIds, filters.envs, ["tag_coverage", "is_serverless", "billing_origin_product"], "sum", "net_usage_quantity");
  const skuState = useFindingData("cost_actual_vs_list_by_sku", filters.window, filters.workspaceIds, filters.envs);
  const trustState = useFindingData("cost_restatement_trust_metric", filters.window, filters.workspaceIds, filters.envs);
  // usage_unit rides along in the group key -- NETWORK_BYTE and NETWORK_HOUR are different physical
  // units (the header's own rule), so each is its own row, never summed into one figure.
  const egressAgg = useFindingAgg("cost_networking_egress", filters.window, filters.workspaceIds, filters.envs, ["usage_type", "usage_unit"], "sum", "net_usage_quantity");
  const infraAgg = useFindingAgg("cost_cloud_infra", filters.window, filters.workspaceIds, filters.envs, ["cloud"], "sum", "net_list_cost");

  const covReady = covAgg.phase === "ready" && covAgg.outcome === "ok_rows";
  const tagCovReady = tagCovAgg.phase === "ready" && tagCovAgg.outcome === "ok_rows";
  // Policies attach to serverless usage only (the Overview's serverless rule), so classic rows stay out.
  const serverlessGroups = (data: AggregateData) => ({ groups: (data.groups || []).filter((g) => g.key && (g.key[1] === true || SERVERLESS_ONLY_PRODUCTS.includes(String(g.key[2])))) });
  const srvCov = covReady ? serverlessGroups(covAgg.data) : null;
  const covSegments = (data: { groups: AggregateData["groups"] }, labels: Record<string, string>, offset: number) => regroupByKeyIndex(data, 0, "unknown")
    .map((e, i): Segment => ({ label: labels[String(e.name)] || enumLabel(e.name) || String(e.name), value: e.value, display: fmtDbu(e.value, 0), color: paletteColor(i + offset) }));
  const policySegments = srvCov ? covSegments(srvCov, POLICY_COVERAGE_LABEL, 0) : null;
  const tagCovSegments = tagCovReady ? covSegments(serverlessGroups(tagCovAgg.data), TAG_COVERAGE_LABEL, 2) : null;
  let uncoveredWs: number | null = null;
  if (srvCov) {
    uncoveredWs = srvCov.groups.filter((g) => g.key[0] === "none").reduce((s, g) => s + numOrZero(g.value), 0);
  }

  const skuRows = skuState.phase === "ready" && skuState.outcome === "ok_rows" ? skuState.data.rows : null;
  const skuJudged = skuRows ? skuRows.filter((r) => r.status !== "NOT_ASSESSED") : null;
  // Rows are per workspace; one bar per SKU.
  const gapItems = skuJudged && skuJudged.length
    ? groupSum(skuJudged, (r) => r.sku_name, "net_list_cost").filter((e) => e.value > 0)
    : null;

  const trustRows = trustState.phase === "ready" && trustState.outcome === "ok_rows" ? trustState.data.rows : null;
  let restatementPct: number | null = null;
  if (trustRows && trustRows.length) {
    const original = sumBy(trustRows, "original_usage_quantity");
    const retracted = sumBy(trustRows, "retracted_abs_quantity");
    restatementPct = original > 0 ? (retracted / original) * 100 : 0;
  }

  // Each group already carries its own usage_unit -- shown as raw quantity + that unit, never
  // converted or summed across NETWORK_BYTE/NETWORK_HOUR.
  const egressItems = egressAgg.phase === "ready" && egressAgg.outcome === "ok_rows"
    ? egressAgg.data.groups.map((g) => ({ name: `${g.key[0]} (${g.key[1] || "unit n/a"})`, value: numOrZero(g.value) }))
    : null;
  const infraReady = infraAgg.phase === "ready" && infraAgg.outcome === "ok_rows";
  const infraTotal = infraReady ? numOrZero(infraAgg.data.total_value) : null;

  const verdictSentence = covReady
    ? `${fmtDbu(uncoveredWs, 0)} of serverless usage has no usage/budget policy attached${restatementPct != null ? `; ${fmtPct(restatementPct, 1)} of prior-month usage was later retracted` : ""}.`
    : "Loading pricing & policy...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <MetricGrid>
        <MetricCard label="Serverless with no policy" value={uncoveredWs != null ? fmtDbu(uncoveredWs, 0) : "-"} facts={[{ label: "Basis", value: "DBUs with no usage/budget policy" }]} tone={numOrZero(uncoveredWs) > 0 ? "warn" : null} />
        <MetricCard label="Restatement" value={restatementPct != null ? fmtPct(restatementPct, 1) : "-"} facts={[{ label: "Basis", value: "Prior-month usage since retracted" }]} />
        <MetricCard label="Spend by cloud" value={infraTotal != null ? fmtMoney(infraTotal, 0) : "-"} facts={infraReady ? [{ label: "Basis", value: estLabel(infraAgg.data.discount_pct) }, { label: "Note", value: "Databricks usage by cloud, not your cloud provider's bill" }] : null} />
      </MetricGrid>

      <div className="grid-2">
        <Card title="Usage policy & tag coverage" right={<span className="muted">serverless DBUs</span>}>
          {policySegments && tagCovSegments ? (
            <div className="row wrap" style={{ gap: 24 }}>
              <Donut segments={policySegments} centerLabel="policy" size={100} thickness={12} />
              <Donut segments={tagCovSegments} centerLabel="tags" size={100} thickness={12} />
            </div>
          ) : <ChartNote state={covAgg} label="cost_usage_policy_coverage" />}
        </Card>
        <Card title="Effective list price by SKU" right={<span className="muted">rate gap</span>}>
          {skuRows && skuJudged && skuJudged.length === 0 ? (
            <div className="chart-note not_assessed"><div className="chart-note-text">{enumLabel((skuRows.find((r) => r.not_assessed_reason) || {}).not_assessed_reason) || "Could not be assessed at this window/filters."}</div></div>
          ) : gapItems ? <RankedBars items={gapItems} n={maxCat} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" /> : <ChartNote state={skuState} label="cost_actual_vs_list_by_sku" />}
          <div className="metric-note" style={{ marginTop: 6 }}>Both rates are approximated from list_prices -- this account exposes no negotiated-rate table, so this is not a discount.</div>
        </Card>
      </div>

      <Card title="Networking egress & cloud infra" right={<span className="muted">by usage type / cloud</span>}>
        <div className="grid-2">
          <div>
            {egressItems ? <HBarList items={egressItems} valueFmt={(v) => fmtInt(Math.round(v))} />
              : featureUnusedNote(egressAgg, `No cross-region networking/egress billed in the last ${filters.window} days -- likely not used, or the account runs in a single region.`)
              || <ChartNote state={egressAgg} label="cost_networking_egress" />}
            <div className="metric-note" style={{ marginTop: 6 }}>Bytes and hours are different units -- each row is its own scale, not comparable bar-to-bar.</div>
          </div>
          <div>
            <div className="chart-card-title">Spend by cloud</div>
            {infraReady ? <HBarList items={groupsToEntries(infraAgg.data, (k) => String((k && k[0]) || "(unknown)"))} valueFmt={(v) => fmtMoney(v, 0)} /> : <ChartNote state={infraAgg} label="cost_cloud_infra" />}
            <div className="metric-note" style={{ marginTop: 6 }}>Databricks usage by cloud, not your cloud provider's bill.</div>
          </div>
        </div>
      </Card>
    </div>
  );
}
AreaContent.register("cost", "pricing", CostPricingContent);

// ═══════════════════════ Chargeback ═══════════════════════
// One query_id per dimension -- each already ranked, pooled ("Other (n)") and windowed
// server-side, so this panel only picks which one is on screen and reshapes its own columns into
// one common {cur, prev, change} shape. Nothing here re-aggregates or re-folds what the SQL
// already computed (a second client-side top-N fold would double-pool the SQL's own Other row).
const CB_DIMENSIONS = [
  { key: "workspace", label: "Workspace", noun: "workspace", id: "cost_chargeback_by_workspace" },
  // Label/noun match the check's own title ("Chargeback by product line") -- billing_origin_product
  // is the grain, same word Cost > By product & SKU already uses for it (productLabel()).
  { key: "service", label: "Product line", noun: "product line", id: "cost_chargeback_by_service" },
  { key: "sku", label: "SKU", noun: "SKU", id: "cost_chargeback_by_sku" },
  { key: "warehouse", label: "Warehouse", noun: "warehouse", id: "cost_chargeback_by_warehouse", reconcileCheck: "by_warehouse" },
  { key: "job", label: "Job", noun: "job", id: "cost_chargeback_by_job", est: true, reconcileCheck: "by_job" },
  { key: "cluster", label: "Cluster", noun: "cluster", id: "cost_chargeback_by_cluster", est: true, reconcileCheck: "by_cluster" },
  { key: "identity", label: "User / identity", noun: "identity", id: "cost_chargeback_identity_by_source", reconcileCheck: "by_user" },
  { key: "tag_value", label: "Tag value", noun: "tag value", id: "cost_chargeback_by_tag_value", reconcileCheck: "by_tag_value" },
];
// The only chargeback dimensions whose rows are split by workspace.
type CbDim = (typeof CB_DIMENSIONS)[number];
const CB_WORKSPACE_SPLIT = new Set(["service", "sku", "tag_value"]);
// job/cluster carry the est_ prefix on their money columns; every other dimension doesn't -- so
// pricing.apply()'s server-side "_disc" twin (^est_(?:[a-z]+_)?usd_list$) only ever exists for
// job/cluster. Every dimension still owes the account's own configured discount (Cost's Trend/By
// SKU tiles already apply it), so `disc` scales every figure client-side instead; a null cur/prev/
// change (no priced usage on that side) stays null, never becomes a scaled $0.
// A move under this many dollars is rounding noise, not a finding worth naming as the "biggest
// mover" -- without a floor, a -$3 swing on an otherwise-flat row could out-rank every real move.
const CB_MIN_MOVER_USD = 10;
function cbMoney(dim: CbDim, row: Row, disc: number) {
  const scale = (v: number | null | undefined) => (v == null ? v : v * (1 - (disc || 0)));
  return dim.est
    ? { cur: scale(row.est_current_usd_list), prev: scale(row.est_previous_usd_list), change: scale(row.est_change_usd_list) }
    : { cur: scale(row.usd_list), prev: scale(row.prev_usd_list), change: scale(row.change_usd_list) };
}
// The one label every chart/table/verdict names a row by -- "Other (n)" only when the SQL itself
// already pooled the tail (is_other), never a second client-side fold.
function cbLabel(dim: CbDim, row: Row): string {
  if (row.is_other) return row.pooled_count != null ? `Other (${fmtInt(row.pooled_count)})` : "Other values";
  switch (dim.key) {
    // cost_chargeback_by_workspace's own header: a NULL workspace_id here is genuine account-level
    // usage (some networking/storage SKUs bill with no workspace_id at all), not an id this app
    // failed to resolve -- wsPhrase's generic "an unknown workspace" would read as the latter.
    case "workspace": return row.workspace_id == null ? "Account-level (no workspace)" : wsPhrase(row.workspace_id);
    case "service": return row.service_label || "(unlabeled service)";
    case "sku": return row.sku_name_readable || row.sku_name || "(unknown SKU)";
    case "warehouse": return resolveName("warehouse", row.workspace_id, row.warehouse_id) || `warehouse ${shortId(row.warehouse_id)}`;
    case "job": return row.job_name || `job ${shortId(row.job_id)}`;
    case "cluster": {
      const kind = row.cluster_kind === "job_cluster" ? "job cluster" : row.cluster_kind === "pipeline" ? "pipeline" : "cluster";
      return row.name || `${kind} ${shortId(row.entity_id)}`;
    }
    // The SQL's own COALESCE(..., 'unknown') (a billing row with no owned_by/run_as at all --
    // all-purpose-cluster usage the query's own header notes Databricks itself leaves blank) is a
    // real, nameable fact, not a lookup failure -- "unknown" reads as data this app failed to
    // resolve; it never had a name to find.
    case "identity": return (!row.identity_run_as || row.identity_run_as === "unknown") ? "No run-as identity" : row.identity_run_as;
    case "tag_value": return row.is_untagged ? "(untagged)" : (row.tag_value || "(blank value)");
    default: return "-";
  }
}
// One row per value again; the share's denominator is account-wide, so shares add up, and the
// worst workspace's status wins.
const CB_SEV_RANK: Record<string, number> = { CRITICAL: 0, WARN: 1, OK: 2, NOT_ASSESSED: 3 };
function cbCollapseWorkspaces(dim: CbDim, rows: Row[] | null): Row[] | null {
  if (!rows || !(CB_WORKSPACE_SPLIT.has(dim.key) || dim.key === "identity")) return rows;
  const keyFn = dim.key === "identity"
    ? (r: Row) => (r.is_other ? `\u0000other\u0000${r.status}` : String(r.identity_run_as || "unknown"))
    : dim.key === "sku"
    ? (r: Row) => [r.sku_name, r.cloud, r.usage_unit].join("\u0000")
    : dim.key === "tag_value"
    ? (r: Row) => [r.tag_key, r.is_other ? "\u0000other" : r.is_untagged ? "\u0000untagged" : r.tag_value].join("\u0000")
    : (r: Row) => r.billing_origin_product;
  const groups = new Map<string, Row[]>();
  rows.forEach((r) => {
    const k = keyFn(r);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(r);
  });
  const sum = (list: Row[], field: string) => list.reduce((s, r) => s + (r[field] == null ? 0 : Number(r[field])), 0);
  return [...groups.values()].map((list) => {
    const first = list[0];
    const assessed = list.filter((r) => r.status !== "NOT_ASSESSED");
    const cur = sum(list, "usd_list");
    const prev = assessed.length ? sum(assessed, "prev_usd_list") : null;
    const change = assessed.length ? sum(assessed, "change_usd_list") : null;
    const worst = [...list].sort((a, b) => CB_SEV_RANK[a.status] - CB_SEV_RANK[b.status])[0];
    const sources = dim.key === "identity" && !first.is_other
      ? list.map((r) => ({ source: r.source, usd: numOrZero(r.usd_list) })).filter((x) => x.usd > 0).sort((a, b) => b.usd - a.usd)
      : null;
    return {
      ...first,
      _sources: sources,
      workspace_id: null, // collapsed across workspaces -- never one workspace's own id
      usd_list: cur,
      prev_usd_list: prev,
      change_usd_list: change,
      change_pct: prev && change != null ? (change / prev) * 100 : null,
      share_of_total_pct: sum(list, "share_of_total_pct"),
      pooled_count: first.is_other ? (sum(list, "pooled_count") || null) : first.pooled_count,
      status: worst.status,
      not_assessed_reason: worst.status === "NOT_ASSESSED" ? worst.not_assessed_reason : null,
      price_basis: list.some((r) => r.price_basis === "unpriced") ? "unpriced"
        : list.every((r) => r.price_basis === "free") ? "free" : "priced",
    };
  });
}
// sql_warehouse/jobs/other -- cost_chargeback_identity_by_source's own three sources, in the same
// words cbSubLabel already prints under the entity name (shared here so a chart label agrees).
function cbSourceWord(source: string): string | null {
  return source === "sql_warehouse" ? "SQL warehouse" : source === "jobs" ? "Jobs" : source === "other" ? "other compute" : null;
}
// A row's label for a CHART bar, which has no second sub-line to disambiguate with -- the identity
// dimension's own grain is (identity, source[, warehouse]), so the SAME identity name can carry
// several bars (one per source) unless the source rides along in the one line a bar gets.
function cbChartLabel(dim: CbDim, row: Row): string {
  if (row.is_other) return cbLabel(dim, row);
  return cbLabel(dim, row);
}
// The entity cell -- a real Databricks object (job/cluster/pipeline/warehouse) gets the shared
// <Ref> (name (id), a Databricks link, job cluster opens the job focus panel); a service/SKU/tag
// value is not a Databricks object and has no such link.
function CbEntityCell({ dim, row }: { dim: CbDim; row: Row }) {
  if (row.is_other) return <span className="muted">{cbLabel(dim, row)}</span>;
  if (dim.key === "warehouse") return <Ref kind="warehouse" id={row.warehouse_id} workspaceId={row.workspace_id} />;
  if (dim.key === "job") return <Ref kind="job" id={row.job_id} workspaceId={row.workspace_id} label={row.job_name} />;
  if (dim.key === "cluster") {
    const kind = row.cluster_kind === "job_cluster" ? "job" : row.cluster_kind === "pipeline" ? "pipeline" : "cluster";
    return <Ref kind={kind} id={row.entity_id} workspaceId={row.workspace_id} label={row.name} />;
  }
  return <span>{cbLabel(dim, row)}</span>;
}
// A short "who/what" sub-line under the entity name -- only where the row itself carries one.
function cbSubLabel(dim: CbDim, row: Row): string | null {
  if (row.is_other) return null;
  if (dim.key === "job") return row.run_as ? `runs as ${row.run_as}` : null;
  if (dim.key === "cluster") return row.owner ? (row.cluster_kind === "all_purpose" ? `owner ${row.owner}` : `runs as ${row.owner}`) : null;
  if (dim.key === "sku") return [row.cloud, row.usage_unit].filter(Boolean).join(" · ") || null;
  if (dim.key === "identity") {
    const typeWord = row.identity_type === "service_principal" ? "service principal" : row.identity_type === "user" ? "person" : null;
    const srcWords = row._sources && row._sources.length
      ? row._sources.map((x: { source: string; usd: number }) => `${cbSourceWord(x.source) || x.source} ${fmtMoney(x.usd, 0)}`).join(", ")
      : cbSourceWord(row.source);
    return [typeWord, srcWords].filter(Boolean).join(" · ") || null;
  }
  return null;
}
// Why a dimension only reaches PART of the billing total, in plain words (cost_chargeback_reconcile's
// own "partial"/"coverage_type" caveat, one line per dimension).
const CB_SCOPE_NOTE: Record<string, string> = {
  warehouse: "DBU usage that carries a SQL warehouse id, so it can differ from the SQL product line",
  job: "DBU usage that carries a job id, whatever product billed it, so it can differ from the Jobs product line",
  cluster: "billed cluster, job-cluster and pipeline DBU usage only, by design",
};
// One account-wide reconcile line per check, summed over its workspace rows; worst status wins.
function reconcileRow(reconcileRows: Row[] | null, checkName: string) {
  const rows = (reconcileRows || []).filter((r) => r.check_name === checkName);
  if (!rows.length) return null;
  const sum = (field: string) => rows.reduce((s, r) => s + (r[field] == null ? 0 : Number(r[field])), 0);
  const billing = rows.some((r) => r.billing_total_usd_list != null) ? sum("billing_total_usd_list") : null;
  const view = rows.some((r) => r.view_total_usd_list != null) ? sum("view_total_usd_list") : null;
  const gap = billing != null && view != null ? view - billing : null;
  const worst = [...rows].sort((a, b) => CB_SEV_RANK[a.status] - CB_SEV_RANK[b.status])[0];
  return {
    check_name: checkName,
    coverage_type: rows[0].coverage_type,
    billing_total_usd_list: billing,
    view_total_usd_list: view,
    gap_usd_list: gap,
    gap_pct: gap != null && billing ? (gap / billing) * 100 : null,
    coverage_pct: billing ? (numOrZero(view) / billing) * 100 : null,
    status: worst.status,
  };
}
// The one reconcile line for the dimension on screen, from cost_chargeback_reconcile's own rows --
// null for workspace/service/sku, which simply re-sum the same priced rows by a different key and
// so are never checked there (their own gap is always algebraically 0). `tagCtx` (tag_value only)
// is this ONE selected key's own {keyTotal, billTotal} -- the reconcile row's own coverage_pct is
// summed across every key exploded together (by design, see the query's own header), so it would
// read as "add up to ~1000%" no matter which single key is on screen; a single key's own top +
// (other) + (untagged) set always partitions the bill on its own, so it is checked against the
// bill directly here instead.
function cbReconcileNote(dim: CbDim, reconcileRows: Row[] | null, tagCtx: { keyTotal: number | null; billTotal: number | null } | null): string | null {
  const row = dim.reconcileCheck && reconcileRow(reconcileRows, dim.reconcileCheck);
  if (!row) return null;
  if (row.coverage_type === "full") {
    const gap = Math.abs(numOrZero(row.gap_pct));
    return row.status === "OK"
      ? `Reconciles to the billing total (gap ${fmtPct(gap, 2)}).`
      : `Off by ${fmtPct(gap, 1)} from the billing total -- ${row.status === "CRITICAL" ? "investigate" : "check"} the split.`;
  }
  if (row.coverage_type === "overlapping") {
    if (!tagCtx || !(numOrZero(tagCtx.billTotal) > 0)) return null;
    const pct = (numOrZero(tagCtx.keyTotal) / numOrZero(tagCtx.billTotal)) * 100;
    return `This tag key's own values add up to ${fmtPct(pct, 0)} of billed $ (untagged fills the rest). A resource carrying several DIFFERENT tag keys counts once under each, so the sum across all keys combined can pass 100% -- that is expected, not a gap in this one key.`;
  }
  return `This cut reaches ${fmtPct(row.coverage_pct, 0)} of billed $ (${CB_SCOPE_NOTE[dim.key] || "partial coverage, by design"}).`;
}
// Tag value view: which keys actually split spend. A key with 2+ values inside a workspace splits
// it; one value per workspace only relabels workspaces; one value everywhere splits nothing.
const CB_KEY_GROUPS = [
  { kind: "mandatory", label: "Mandatory tags" },
  { kind: "split", label: "Splits spend (2+ values in a workspace)" },
  { kind: "per_ws", label: "One value per workspace (same split as the Workspace view)" },
  { kind: "one", label: "One value only (doesn't split spend)" },
];
const CB_ACCOUNT_WS = "__account__";
/** One tag key's spend and how its values spread over workspaces. */
interface KeyStat {
  key: string;
  usd: number;
  values: Set<string>;
  perWs: Map<string, Set<string>>;
  many: boolean;
  kind: string;
  shape: string;
  valueCount: number | null;
}
function cbWsKey(r: Row): string {
  return r.workspace_id == null ? CB_ACCOUNT_WS : String(r.workspace_id);
}
function cbTagKeyStats(rows: Row[], mandatoryNorm: Set<string>): KeyStat[] {
  const stats = new Map<string, Omit<KeyStat, "kind" | "shape" | "valueCount">>();
  rows.forEach((r) => {
    if (r.is_untagged) return;
    if (!stats.has(r.tag_key)) stats.set(r.tag_key, { key: r.tag_key, usd: 0, values: new Set(), perWs: new Map(), many: false });
    const st = stats.get(r.tag_key)!;
    st.usd += numOrZero(r.usd_list);
    if (r.is_other) { st.many = true; return; }
    st.values.add(r.tag_value);
    const ws = cbWsKey(r);
    if (!st.perWs.has(ws)) st.perWs.set(ws, new Set());
    st.perWs.get(ws)!.add(r.tag_value);
  });
  return [...stats.values()].map((st) => {
    const splitsInWs = st.many || [...st.perWs.values()].some((v) => v.size > 1);
    const shape = splitsInWs ? "split" : st.values.size > 1 ? "per_ws" : "one";
    const kind = mandatoryNorm.has(normalizeTagKeyLike(st.key)) ? "mandatory" : shape;
    return { ...st, kind, shape, valueCount: st.many ? null : st.values.size };
  }).sort((a, b) => b.usd - a.usd);
}
function cbKeyNote(st: KeyStat | null): string | null {
  if (!st) return null;
  if (st.shape === "per_ws") return `${st.key} has one value per workspace, so it splits spend the same way the Workspace view does.`;
  if (st.shape === "one") return `${st.key} has one value (${[...st.values][0]}) on all its spend, so it doesn't split spend; it only shows how much carries it.`;
  return null;
}
// Workspace first, then the tag key -- dropdowns, since an account can carry dozens of keys.
function TagKeyPickers({ wsOptions, ws, onWs, keyStats, active, onKey }: {
  wsOptions: { key: string; label: string }[]; ws: string; onWs: (key: string) => void; keyStats: KeyStat[];
  active: string | null | undefined; onKey: (key: string) => void;
}) {
  if (!keyStats.length) return null;
  const valueWord = (st: KeyStat) => (st.valueCount == null ? "many values" : `${fmtInt(st.valueCount)} value${st.valueCount === 1 ? "" : "s"}`);
  return (
    <div className="row wrap cb-tag-pick">
      <label className="cb-pick">
        <span className="af-chip-label">Workspace</span>
        <select value={ws} onChange={(e) => onWs(e.target.value)}>
          <option value="">{`All workspaces (${fmtInt(wsOptions.length)})`}</option>
          {wsOptions.map((w) => <option key={w.key} value={w.key}>{w.label}</option>)}
        </select>
      </label>
      <label className="cb-pick">
        <span className="af-chip-label">Tag key</span>
        <select value={active || ""} onChange={(e) => onKey(e.target.value)}>
          {CB_KEY_GROUPS.map((g) => {
            const list = keyStats.filter((st) => st.kind === g.kind);
            return list.length ? (
              <optgroup key={g.kind} label={`${g.label} · ${fmtInt(list.length)}`}>
                {list.map((st) => <option key={st.key} value={st.key}>{`${st.key} · ${valueWord(st)} · ${fmtMoney(st.usd, 0)} tagged`}</option>)}
              </optgroup>
            ) : null;
          })}
        </select>
      </label>
    </div>
  );
}

function CostChargebackContent({ filters, maxCat, meta, onVerdict }: TabProps) {
  useNames(); // re-render once workspace/job/cluster/warehouse names are ready
  const [dimKey, setDimKey] = React.useState("workspace");
  const [tagKey, setTagKey] = React.useState<string | null>(null);
  const [tagWs, setTagWs] = React.useState("");
  // Spend first: each row's own total; Change shows the movers against the previous period.
  const [cbMetric, setCbMetric] = React.useState("spend");
  const dim = CB_DIMENSIONS.find((d) => d.key === dimKey) || CB_DIMENSIONS[0];

  const rowsState = useFindingData(dim.id, filters.window, filters.workspaceIds, filters.envs, 5000);
  // One row per workspace per check, so a small cap cut off whole checks.
  const reconcileState = useFindingData("cost_chargeback_reconcile", filters.window, filters.workspaceIds, filters.envs, 5000);
  const reconcileReady = reconcileState.phase === "ready" && reconcileState.outcome === "ok_rows";

  const ready = rowsState.phase === "ready" && rowsState.outcome === "ok_rows";
  const allRows = ready ? rowsState.data.rows : null;

  // Tag value only: the SQL returns every tag_key's own top-values + (other) + (untagged) set in
  // one flat result -- narrow to one key at a time, defaulting to the one with the most TAGGED
  // spend (excluding each key's own (untagged) row, which pads every key's total up towards the
  // same whole bill and so cannot tell one key's real usage apart from another's).
  const isTagDim = dim.key === "tag_value";
  const tagWsOptions = React.useMemo(() => {
    if (!isTagDim || !allRows) return [];
    const byWs = new Map<string, number>();
    allRows.forEach((r) => { if (!r.is_untagged) byWs.set(cbWsKey(r), (byWs.get(cbWsKey(r)) || 0) + numOrZero(r.usd_list)); });
    return [...byWs.entries()].sort((a, b) => b[1] - a[1])
      .map(([k]) => ({ key: k, label: k === CB_ACCOUNT_WS ? "Account-level (no workspace)" : wsPhrase(k) }));
  }, [isTagDim, allRows]);
  const activeTagWs = tagWsOptions.some((w) => w.key === tagWs) ? tagWs : "";
  const tagScopeRows = React.useMemo(
    () => (isTagDim && allRows && activeTagWs ? allRows.filter((r) => cbWsKey(r) === activeTagWs) : allRows),
    [isTagDim, allRows, activeTagWs],
  );
  const mandatoryKeys = (meta && meta.mandatory_tag_keys) || [];
  const mandatoryNorm = React.useMemo(() => withSameTagKeys(mandatoryKeys.map(normalizeTagKeyLike)), [mandatoryKeys.join("|")]);
  const tagKeyStats = React.useMemo(
    () => (isTagDim && tagScopeRows ? cbTagKeyStats(tagScopeRows, mandatoryNorm) : []),
    [isTagDim, tagScopeRows, mandatoryNorm],
  );
  // Default: the mandatory tag with the most tagged spend, then the key that splits the most.
  const defaultStat = tagKeyStats.find((st) => st.kind === "mandatory") || tagKeyStats.find((st) => st.kind === "split")
    || tagKeyStats.find((st) => st.kind === "per_ws") || tagKeyStats[0];
  const defaultTagKey = defaultStat ? defaultStat.key : undefined;
  const activeTagKey = isTagDim ? (tagKeyStats.some((st) => st.key === tagKey) ? tagKey : defaultTagKey) : null;
  const activeKeyStat = tagKeyStats.find((st) => st.key === activeTagKey) || null;
  const rowsForDim = isTagDim && tagScopeRows ? tagScopeRows.filter((r) => r.tag_key === activeTagKey) : allRows;
  const rows = cbCollapseWorkspaces(dim, rowsForDim);

  // None of these 8 cuts' own money columns carry the "est_" prefix pricing.apply()'s server-side
  // "_disc" twin requires (job/cluster excepted) -- Cost's other sub-tabs already apply the
  // account's configured discount client-side the same way (CostTrendContent's spendTile/
  // periodTile), so Chargeback does too, or a discount configured in Settings would make this page
  // disagree with the rest of Cost on the very same dollars.
  const disc = rowsState.data ? numOrZero(rowsState.data.discount_pct) : 0;

  const judged = rows ? rows.filter((r) => !r.is_other && r.status !== "NOT_ASSESSED") : null;
  const otherRows = rows ? rows.filter((r) => r.is_other) : null;
  const naRows = rows ? rows.filter((r) => !r.is_other && r.status === "NOT_ASSESSED") : null;
  const notAssessedCount = naRows ? naRows.length : 0;
  // Every row's own current-period $, judged or not -- a NOT_ASSESSED row (too little history for
  // a previous-period comparison) still billed real dollars this window, so leaving it out of the
  // total understated actual spend.
  const total = rows ? rows.reduce((s, r) => s + numOrZero(cbMoney(dim, r, disc).cur), 0) : null;
  const anyUnpriced = rows ? rows.some((r) => r.price_basis === "unpriced") : false;
  // The raw not_assessed_reason codes, in the header's own words (service.substitute_header_params)
  // -- never the bare code, and never a made-up caption.
  const naReasonWords = rowsState.data && rowsState.data.header && rowsState.data.header.not_assessed_reasons;
  const naReasonCodes = naRows ? [...new Set<string>(naRows.map((r) => r.not_assessed_reason).filter(Boolean))] : [];
  const naReasonText = naReasonCodes.length ? naReasonCodes.map((c) => (naReasonWords && naReasonWords[c]) || c).join("; ") : null;

  const movers = judged
    ? judged.map((r) => ({ row: r, label: cbChartLabel(dim, r), change: numOrZero(cbMoney(dim, r, disc).change) }))
        .filter((m) => Math.abs(m.change) >= CB_MIN_MOVER_USD)
    : null;
  const gainer = movers ? movers.filter((m) => m.change > 0).sort((a, b) => b.change - a.change)[0] : null;
  const faller = movers ? movers.filter((m) => m.change < 0).sort((a, b) => a.change - b.change)[0] : null;

  // Real rows ranked by this window's own $ (a chargeback ledger reads biggest-spender-first, not
  // worst-first) -- NOT_ASSESSED rows after them, the pooled Other/untagged-tail row always last.
  const displayRows = React.useMemo(() => {
    if (!rows) return null;
    const real = [...rows.filter((r) => !r.is_other && r.status !== "NOT_ASSESSED")]
      .sort((a, b) => numOrZero(cbMoney(dim, b, disc).cur) - numOrZero(cbMoney(dim, a, disc).cur));
    const na = rows.filter((r) => !r.is_other && r.status === "NOT_ASSESSED");
    const other = rows.filter((r) => r.is_other);
    return [...real, ...na, ...other];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, dim.key, disc]);
  const spendItems = displayRows
    // The check's own pooled row is renamed so it isn't confused with the chart's "Other" bar.
    ? displayRows.map((r) => ({ name: r.is_other ? cbLabel(dim, r).replace(/^Other/, "Smaller ones pooled") : cbLabel(dim, r), value: numOrZero(cbMoney(dim, r, disc).cur) })).filter((e) => e.value > 0)
    : null;

  // price_join_fanout is a self-check on the shared price join every one of these 8 cuts uses, not
  // one more dimension row -- it never shows up unless read here directly, whichever dimension is
  // on screen.
  const fanoutRow = reconcileReady ? reconcileRow(reconcileState.data.rows, "price_join_fanout") : null;
  // Undiscounted on both sides on purpose -- a single account-wide discount scales every $ figure
  // the same way, so it cancels out of a coverage RATIO; keeping this pair raw (rather than the
  // already-discounted `total` above) is what keeps that cancellation exact.
  const billTotal = fanoutRow ? numOrZero(fanoutRow.billing_total_usd_list) : null;
  const rawKeyTotal = rows ? rows.reduce((s, r) => s + numOrZero(cbMoney(dim, r, 0).cur), 0) : null;
  // Every total on this page already dollarizes each usage row once, at its one latest-matching
  // price -- this gap measures overlapping validity windows in Databricks' own list_prices, a
  // source data-quality signal, not an error in the totals shown here.
  const fanoutNote = fanoutRow && fanoutRow.status !== "OK"
    ? `${fmtMoney(Math.abs(numOrZero(fanoutRow.gap_usd_list)) * (1 - disc), 0)} (${fmtPct(Math.abs(numOrZero(fanoutRow.gap_pct)), 1)}) gap from overlapping list-price rows in Databricks' price list -- totals here use the latest price, not overstated.`
    : null;
  const reconcileNote = reconcileReady
    // One workspace picked: its key total against the whole account's bill would read as a gap.
    ? cbReconcileNote(dim, reconcileState.data.rows, isTagDim && !activeTagWs ? { keyTotal: rawKeyTotal, billTotal } : null)
    : null;

  const verdictSentence = ready
    ? `${fmtMoney(total, 0)} charged back by ${dim.noun} this window`
      + (gainer ? `; biggest mover up: ${gainer.label}, +${fmtMoney(gainer.change, 0)}` : "")
      + (faller ? `${gainer ? "," : ";"} down: ${faller.label}, ${fmtMoney(faller.change, 0)}` : "")
      + "."
    : rowsState.phase === "ready"
      ? `No chargeback data by ${dim.noun} in the last ${filters.window} days for these filters.`
      : rowsState.phase === "error" ? "Couldn't load chargeback." : "Loading chargeback...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  // A build that predates these 8 cuts (a real export captured before this app added them) reads
  // NOT_ASSESSED with not_built_reason on every one of them alike -- 8 dimension buttons into 8
  // identical "not in this export yet" screens is nothing to browse, so they're hidden together
  // rather than one at a time as each gets clicked.
  const chargebackNotBuilt = rowsState.phase === "ready" && isNotBuilt(rowsState.data);

  return (
    <div>
      {!chargebackNotBuilt && <SubTabNav subtabs={CB_DIMENSIONS} active={dim.key} onSelect={setDimKey} />}
      {!chargebackNotBuilt && isTagDim && (
        <TagKeyPickers wsOptions={tagWsOptions} ws={activeTagWs} onWs={setTagWs} keyStats={tagKeyStats} active={activeTagKey} onKey={setTagKey} />
      )}
      {isTagDim && cbKeyNote(activeKeyStat) && <div className="metric-note" style={{ marginBottom: 10 }}>{cbKeyNote(activeKeyStat)}</div>}

      {!ready ? <ChartNote state={rowsState} label={dim.id} /> : (
        <div>
          {fanoutNote && <div className="metric-note" style={{ marginBottom: 10 }}>{fanoutNote}</div>}
          <MetricGrid>
            <MetricCard
              label={`Charged back by ${dim.noun}`}
              value={fmtMoney(total, 0)}
              facts={[otherRows && otherRows.length
                ? { label: "Incl.", value: otherRows.map((r) => cbLabel(dim, r)).join(", ") }
                : { label: "Rows", value: fmtInt((rows || []).length) }]}
            />
            <MetricCard label="Biggest mover up" value={gainer ? `+${fmtMoney(gainer.change, 0)}` : "-"} facts={[gainer ? { label: "Where", value: gainer.label, tone: "warn" } : { label: "Status", value: "Nothing grew this window", tone: "muted" }]} tone={gainer ? "warn" : null} />
            <MetricCard label="Biggest mover down" value={faller ? fmtMoney(faller.change, 0) : "-"} facts={[faller ? { label: "Where", value: faller.label } : { label: "Status", value: "Nothing fell this window", tone: "muted" }]} />
            <MetricCard label="Not assessed" value={fmtInt(notAssessedCount)} facts={[notAssessedCount ? { label: "Why", value: naReasonText || "Not enough history to judge", tone: "muted" } : { label: "Status", value: "Every row could be judged", tone: "ok" }]} tone={notAssessedCount ? "na" : null} />
          </MetricGrid>
          {anyUnpriced && <div className="metric-note" style={{ marginBottom: 10 }}>Some usage here has no matching price -- its row, and the total above, understate actual spend.</div>}

          <Card
            title={cbMetric === "spend" ? `Spend by ${dim.noun}` : "Biggest movers vs previous period"}
            right={(
              <div className="ws-actions">
                {CHANGE_METRICS.map((m) => (
                  <button key={m.key} type="button" className={m.key === cbMetric ? "on" : ""} onClick={() => setCbMetric(m.key)}>{m.label}</button>
                ))}
              </div>
            )}
          >
            {cbMetric === "spend" ? (spendItems && spendItems.length ? (
              <RankedBars
                items={spendItems}
                n={maxCat}
                valueFmt={(v) => fmtMoney(v, 0)}
                unitLabel="$"
                note={`Each ${dim.noun}'s total this window; the change against the previous period is under Change and in the table below.`}
              />
            ) : <div className="chart-note ok"><div className="chart-note-text">{`No spend by ${dim.noun} in this window.`}</div></div>)
            : movers && movers.length ? (
              <DivergingBars
                items={movers.map((m) => ({ name: m.label, value: m.change }))}
                n={maxCat}
                valueFmt={(v) => `${v >= 0 ? "+" : "-"}${fmtMoney(Math.abs(v), 0)}`}
                unitLabel="Change"
              />
            ) : <div className="chart-note ok"><div className="chart-note-text">Nothing moved enough to rank this window.</div></div>}
          </Card>

          <Card title={`Chargeback by ${dim.noun}`} right={<span className="muted">{fmtInt((displayRows || []).length)} rows · {estLabel(disc)}</span>}>
            {dim.key === "cluster" && <div className="metric-note" style={{ marginBottom: 6 }}>All-purpose clusters, job clusters (rolled up to their job) and classic Lakeflow pipelines.</div>}
            <div className="data-table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>{dim.label}</th>
                    <th className="num">$ this window</th>
                    <th className="num">Share</th>
                    <th className="num">vs previous</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {(displayRows || []).map((r, i) => {
                    const m = cbMoney(dim, r, disc);
                    const sub = cbSubLabel(dim, r);
                    return (
                      <tr key={i}>
                        <td>
                          <CbEntityCell dim={dim} row={r} />
                          {sub && <div className="cb-sub muted">{sub}</div>}
                        </td>
                        <td className="num mono">{fmtMoney(m.cur, 0)}</td>
                        <td className="num mono">{r.share_of_total_pct != null ? fmtPct(r.share_of_total_pct, 1) : "-"}</td>
                        <td className={`num mono status-${r.status}`}>
                          {r.status === "NOT_ASSESSED" ? "not assessed" : m.change != null ? `${m.change >= 0 ? "+" : "-"}${fmtMoney(Math.abs(m.change), 0)} (${fmtChangePct(r.change_pct)})` : "-"}
                        </td>
                        <td><StatusPill kind={r.status ? r.status.toLowerCase() : "not_assessed"} compact /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>

          {reconcileNote && <div className="metric-note">{reconcileNote}</div>}
        </div>
      )}
    </div>
  );
}
AreaContent.register("cost", "chargeback", CostChargebackContent);
