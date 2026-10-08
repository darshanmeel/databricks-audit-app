// Cost > Trend: spend by workspace, warehouse or SKU, by day or by month. The chart stacks the top 5,
// the table lists every one, and the picked one gets its own daily and monthly bars.
import React from "react";

import { estLabel, fmtDayShort, fmtInt, fmtMoney, fmtMoneyShort } from "../format";
import { useNames, wsPhrase } from "../components/names";
import { fillDays, fmtChangePct, numOrZero, useFindingAgg } from "../components/hooks";
import { Card } from "../components/primitives";
import { ChartNote } from "../components/charts";
import { Columns } from "../components/charts_more";
import { csvField } from "../components/findings_table";
import { COST_MONEY_COL } from "../components/tab_registry";
import { DAY_KEY, DAY_ROWS_MAX, MONTH_ROWS_MAX, GrainSwitch, fmtMonth, latest, monthOf, monthRun, partialMonths } from "../components/grain";
import type { Grain } from "../components/grain";
import { baName } from "./tab_cost_before_after";
import type { AggState, Filters, Meta } from "../types";

const LEVELS = [
  { key: "workspace", label: "Workspace", noun: "workspace", plural: "workspaces" },
  { key: "warehouse", label: "Warehouse", noun: "warehouse", plural: "warehouses" },
  { key: "sku", label: "SKU", noun: "SKU", plural: "SKUs" },
];
type Src = { qid: string; group: string[]; col: string };
const DAY_SRC: Record<string, Src> = {
  workspace: { qid: "cost_dollarized_by_sku_day", group: ["workspace_id", "usage_date"], col: COST_MONEY_COL },
  sku: { qid: "cost_dollarized_by_sku_day", group: ["sku_name", "usage_date"], col: COST_MONEY_COL },
  warehouse: { qid: "cost_daily_by_resource", group: ["resource_type", "resource_key", "usage_date"], col: "usd_list" },
};
// Warehouses have no monthly history: their months are summed from the window's days.
const MONTH_SRC: Record<string, Src | null> = {
  workspace: { qid: "cost_monthly_actuals", group: ["workspace_id", "month_start"], col: "net_list_cost_usd" },
  sku: { qid: "cost_sku_trend_12m", group: ["sku_name", "month_start"], col: "net_list_cost_usd" },
  warehouse: null,
};
const TOP_N = 5;
const SHOWN = 20;

interface Grid { keys: string[]; items: Map<string, Map<string, number>> }
interface TrendRow { key: string; name: string; values: number[]; total: number; avg: number | null; last: number | null; prev: number | null }

export function ready(s: AggState): boolean {
  return s.phase === "ready" && s.outcome === "ok_rows" && s.data != null;
}

// item -> period -> value, from aggregate groups whose item sits at itemAt and period at periodAt.
export function gridOf(state: AggState, itemAt: number, periodAt: number, scale: number, toPeriod: (k: string) => string, typeFilter?: string): Map<string, Map<string, number>> {
  const out = new Map<string, Map<string, number>>();
  if (!ready(state)) return out;
  state.data!.groups.forEach((g) => {
    if (typeFilter && g.key[0] !== typeFilter) return;
    const day = String(g.key[periodAt]).slice(0, 10);
    // Right after a level switch the previous level's groups are still here for one render.
    if (!DAY_KEY.test(day)) return;
    const item = String(g.key[itemAt] == null ? "account" : g.key[itemAt]);
    const period = toPeriod(day);
    if (!out.has(item)) out.set(item, new Map());
    const m = out.get(item)!;
    m.set(period, (m.get(period) || 0) + numOrZero(g.value) * scale);
  });
  return out;
}

function itemName(level: string, key: string): string {
  if (level === "workspace") return key === "account" ? "Account-level (no workspace)" : wsPhrase(key);
  if (level === "sku") return key === "account" ? "(no SKU)" : key;
  return baName("warehouse", key, "warehouses");
}

function sumOf(vals: number[]): number {
  return vals.reduce((a, b) => a + b, 0);
}

export function TrendByCard({ filters, meta }: { filters: Filters; meta: Meta | null }) {
  useNames();
  const [level, setLevel] = React.useState("workspace");
  const [grain, setGrain] = React.useState<Grain>("day");
  const [picked, setPicked] = React.useState<string | null>(null);
  const [showAll, setShowAll] = React.useState(false);
  const lv = LEVELS.find((l) => l.key === level) || LEVELS[0];
  const ds = DAY_SRC[level];
  const ms = MONTH_SRC[level];
  const dayAgg = useFindingAgg(ds.qid, filters.window, filters.workspaceIds, filters.envs, ds.group, "sum", ds.col);
  const monthAgg = useFindingAgg(ms ? ms.qid : null, filters.window, filters.workspaceIds, filters.envs, ms ? ms.group : null, "sum", ms ? ms.col : null);
  const isWh = level === "warehouse";

  const days = React.useMemo(() => {
    const filled = fillDays([], meta && meta.as_of_date, filters.window, meta && meta.snapshot_days).map((d) => d.day);
    if (filled.length || !ready(dayAgg)) return filled;
    const at = isWh ? 2 : 1;
    return [...new Set(dayAgg.data!.groups.map((g) => String(g.key[at]).slice(0, 10)).filter((d) => DAY_KEY.test(d)))].sort();
  }, [meta, filters.window, dayAgg, isWh]);

  const dayGrid = React.useMemo((): Grid | null => {
    if (!ready(dayAgg) || !days.length) return null;
    const scale = 1 - numOrZero(dayAgg.data!.discount_pct);
    const items = isWh ? gridOf(dayAgg, 1, 2, scale, (d) => d, "warehouse") : gridOf(dayAgg, 0, 1, scale, (d) => d);
    return { keys: days, items };
  }, [dayAgg, days, isWh]);

  const monthGrid = React.useMemo((): Grid | null => {
    if (isWh) {
      if (!dayGrid) return null;
      const items = new Map<string, Map<string, number>>();
      dayGrid.items.forEach((byDay, item) => {
        const m = new Map<string, number>();
        byDay.forEach((v, d) => m.set(monthOf(d), (m.get(monthOf(d)) || 0) + v));
        items.set(item, m);
      });
      return { keys: monthRun(days.map(monthOf)), items };
    }
    if (!ready(monthAgg)) return null;
    const items = gridOf(monthAgg, 0, 1, 1 - numOrZero(monthAgg.data!.discount_pct), monthOf);
    const keys: string[] = [];
    items.forEach((m) => m.forEach((_, k) => keys.push(k)));
    return { keys: monthRun(keys), items };
  }, [isWh, dayGrid, days, monthAgg]);

  // Months not fully covered: the one still running, and for warehouses the window's first.
  const { open: openMonth, cut: cutMonth } = partialMonths(days, isWh);
  // Today, when the export includes it, is a part day: left out of averages and the 7-day change.
  const partDay = meta && meta.direct_export && meta.direct_export.includes_today && days.length ? days[days.length - 1] : null;
  const isFull = (m: string) => m !== openMonth && m !== cutMonth;

  const grid = grain === "day" ? dayGrid : monthGrid;
  const keys = grid ? latest(grid.keys, grain === "day" ? DAY_ROWS_MAX : MONTH_ROWS_MAX) : [];

  const rows = React.useMemo((): TrendRow[] | null => {
    if (!grid) return null;
    const out: TrendRow[] = [];
    grid.items.forEach((byKey, key) => {
      const values = keys.map((k) => byKey.get(k) || 0);
      const total = sumOf(values);
      if (total <= 0) return;
      let last: number | null = null, prev: number | null = null, avg: number | null = null;
      if (grain === "day") {
        const whole = partDay && keys[keys.length - 1] === partDay ? values.slice(0, -1) : values;
        avg = whole.length ? sumOf(whole) / whole.length : null;
        if (whole.length >= 14) { last = sumOf(whole.slice(-7)); prev = sumOf(whole.slice(-14, -7)); }
      } else {
        const full = keys.map((k, i) => ({ k, v: values[i] })).filter((x) => isFull(x.k));
        if (full.length) last = full[full.length - 1].v;
        if (full.length > 1) prev = full[full.length - 2].v;
      }
      out.push({ key, name: itemName(level, key), values, total, avg, last, prev });
    });
    return out.sort((a, b) => b.total - a.total || a.name.localeCompare(b.name));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grid, keys.join(","), grain, level, openMonth, cutMonth, partDay]);

  const sel = rows && rows.length ? rows.find((r) => r.key === picked) || rows[0] : null;
  const grand = rows ? sumOf(rows.map((r) => r.total)) : 0;
  const shown = rows ? (showAll ? rows : rows.slice(0, SHOWN)) : null;
  const pooled = !!rows && rows.some((r) => r.key.endsWith(":other"));
  const disc = ready(dayAgg) ? dayAgg.data!.discount_pct : null;
  const xFmt = grain === "month" ? (k: string) => fmtMonth(k) : undefined;
  const span = keys.length ? (grain === "day" ? `${fmtDayShort(keys[0])} - ${fmtDayShort(keys[keys.length - 1])}` : `${fmtMonth(keys[0])} - ${fmtMonth(keys[keys.length - 1])}`) : "";
  const changeOf = (r: TrendRow) => (r.last != null && r.prev != null && r.prev > 0 ? fmtChangePct(((r.last - r.prev) / r.prev) * 100) : "-");

  const downloadCsv = () => {
    if (!rows || !keys.length) return;
    const head = [lv.label, ...keys.map((k) => (grain === "day" ? k : k.slice(0, 7))), "Total"];
    const lines = [head.map(csvField).join(",")];
    rows.forEach((r) => lines.push([r.name, ...r.values.map((v) => Math.round(v * 100) / 100), Math.round(r.total * 100) / 100].map(csvField).join(",")));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
    a.download = `spend-by-${level}-${grain}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const pending = grain === "month" && !isWh ? monthAgg : dayAgg;
  const pendingQid = grain === "month" && ms ? ms.qid : ds.qid;
  const monthNote = isWh ? "Months inside this export's window only." : level === "sku" ? "Last 12 months and this month." : `Last ${MONTH_ROWS_MAX} months of the billing history.`;

  return (
    <React.Fragment>
      <Card title="Spend by workspace, warehouse or SKU" right={<span className="card-right-row"><span className="muted">{estLabel(disc)}</span><GrainSwitch value={grain} onChange={(g) => { setGrain(g); setShowAll(false); }} /></span>}>
        <div className="ws-actions">
          {LEVELS.map((l) => (
            <button key={l.key} type="button" className={l.key === level ? "on" : ""} onClick={() => { setLevel(l.key); setPicked(null); setShowAll(false); }}>{l.label}</button>
          ))}
        </div>
        {rows && keys.length ? (
          rows.length ? (
            <React.Fragment>
              <Columns
                days={keys}
                series={rows.map((r) => ({ name: r.name, id: r.key, values: r.values }))}
                n={TOP_N}
                valueFmt={(v) => fmtMoneyShort(v)}
                unitLabel="$"
                dollarAxis
                xFmt={xFmt}
                xName={grain === "day" ? "Day" : "Month"}
                partial={grain === "day" ? [partDay] : [openMonth, cutMonth]}
                note={`${span}: ${fmtMoney(grand, 0)} across ${fmtInt(rows.length)} ${rows.length === 1 ? lv.noun : lv.plural}.${rows.length > TOP_N ? ` The top ${TOP_N} stacked, the rest as Other.` : ""}${grain === "month" ? ` ${monthNote}` : keys.length < DAY_ROWS_MAX && filters.window < 90 ? " Pick 90d in the top bar for up to 45 days." : ""}`}
                ariaLabel={`Spend by ${lv.noun} by ${grain}`}
              />
              <div className="ba-table-head trend-by-head">
                <span className="muted">{`Every ${lv.noun}, most spend first. Click one for its own days and months.`}</span>
                <button type="button" className="csv-download-btn" onClick={downloadCsv}>Download CSV</button>
              </div>
              <div className="data-table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>{lv.label}</th>
                      <th className="num">Total<div className="ba-span">{span}</div></th>
                      <th className="num">Share</th>
                      {grain === "day" ? <th className="num">Per day</th> : <th className="num">Last full month</th>}
                      <th className="num">{grain === "day" ? "Last 7 days vs 7 before" : "vs month before"}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(shown || []).map((r) => (
                      <tr key={r.key} className={`ba-row${sel && r.key === sel.key ? " on" : ""}`} onClick={() => setPicked(r.key)}>
                        <td><button type="button" className="ba-pick" onClick={() => setPicked(r.key)}>{r.name}</button></td>
                        <td className="num mono">{fmtMoney(r.total, 0)}</td>
                        <td className="num mono">{grand > 0 ? `${((r.total / grand) * 100).toFixed(1)}%` : "-"}</td>
                        <td className="num mono">{grain === "day" ? fmtMoney(r.avg, r.avg != null && r.avg < 100 ? 2 : 0) : r.last != null ? fmtMoney(r.last, 0) : "-"}</td>
                        <td className="num mono">{changeOf(r)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {rows.length > SHOWN && (
                <button type="button" className="ov-link-btn" onClick={() => setShowAll(!showAll)}>
                  {showAll ? `Show the ${SHOWN} largest` : `Show all ${fmtInt(rows.length)}`}
                </button>
              )}
              {pooled && <div className="metric-note">{`The 100 ${lv.plural} with the most spend have their own rows; the rest are one "Other" row per workspace.`}</div>}
            </React.Fragment>
          ) : <div className="muted">{`No ${lv.plural} with spend in ${span}.`}</div>
        ) : <ChartNote state={pending} label={pendingQid} />}
      </Card>

      {sel && dayGrid && (
        <TrendItemCard name={sel.name} byDay={dayGrid.items.get(sel.key) || null} days={dayGrid.keys}
          byMonth={monthGrid ? monthGrid.items.get(sel.key) || null : null} months={monthGrid ? monthGrid.keys : []}
          monthNote={monthNote} openMonth={openMonth} isFull={isFull} partDay={partDay} />
      )}
    </React.Fragment>
  );
}

// One workspace, warehouse or SKU: its latest 45 days and its months side by side.
function TrendItemCard({ name, byDay, days, byMonth, months, monthNote, openMonth, isFull, partDay }: {
  name: string; byDay: Map<string, number> | null; days: string[]; byMonth: Map<string, number> | null; months: string[];
  monthNote: string; openMonth: string | null; isFull: (m: string) => boolean; partDay: string | null;
}) {
  const dKeys = latest(days, DAY_ROWS_MAX);
  const dVals = dKeys.map((d) => (byDay && byDay.get(d)) || 0);
  const mKeys = latest(months, MONTH_ROWS_MAX);
  const mVals = mKeys.map((m) => (byMonth && byMonth.get(m)) || 0);
  const whole = partDay && dKeys[dKeys.length - 1] === partDay ? dVals.slice(0, -1) : dVals;
  const peakAt = dVals.reduce((best, v, i) => (v > dVals[best] ? i : best), 0);
  const full = mKeys.map((k, i) => ({ k, v: mVals[i] })).filter((x) => isFull(x.k));
  const lastFull = full.length ? full[full.length - 1] : null;
  const prevFull = full.length > 1 ? full[full.length - 2] : null;
  const openIdx = openMonth ? mKeys.indexOf(openMonth) : -1;
  const dayNote = dKeys.length
    ? `Average ${fmtMoney(whole.length ? sumOf(whole) / whole.length : 0, 2)} a day over ${fmtInt(whole.length)} whole days; highest ${fmtMoney(dVals[peakAt], 0)} on ${fmtDayShort(dKeys[peakAt])}.`
    : null;
  const monthParts = [
    lastFull ? `${fmtMonth(lastFull.k)} ${fmtMoney(lastFull.v, 0)}${prevFull && prevFull.v > 0 ? `, ${fmtChangePct(((lastFull.v - prevFull.v) / prevFull.v) * 100)} on ${fmtMonth(prevFull.k)}` : ""}` : null,
    openIdx >= 0 ? `${fmtMonth(openMonth)} so far ${fmtMoney(mVals[openIdx], 0)}` : null,
  ].filter(Boolean);
  return (
    <Card title={name} right={<span className="muted">by day and by month</span>}>
      <div className="grid-2">
        <div>
          <div className="chart-card-title">By day</div>
          <Columns days={dKeys} series={[{ name: "$ per day", values: dVals }]} valueFmt={(v) => fmtMoneyShort(v)} unitLabel="$" dollarAxis partial={[partDay]} note={dayNote} ariaLabel={`${name} by day`} />
        </div>
        <div>
          <div className="chart-card-title">By month</div>
          {mKeys.length > 1
            ? <Columns days={mKeys} series={[{ name: "$ per month", values: mVals }]} valueFmt={(v) => fmtMoneyShort(v)} unitLabel="$" dollarAxis xFmt={(k) => fmtMonth(k)} xName="Month" partial={mKeys.filter((k) => !isFull(k))}
                note={`${monthParts.join(" · ")}${monthParts.length ? ". " : ""}${monthNote}`} ariaLabel={`${name} by month`} />
            : <div className="muted">{`Only ${mKeys.length ? fmtMonth(mKeys[0]) : "one month"} in this window: pick a longer window for a monthly trend.`}</div>}
        </div>
      </div>
    </Card>
  );
}
