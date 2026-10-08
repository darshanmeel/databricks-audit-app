// Queries > Trend: one SQL warehouse measure (spill, shuffle, slot wait, provisioning wait, cache,
// query time) by day or by month, across all warehouses and for each one.
import React from "react";

import { fmtDayShort, fmtDuration, fmtGb, fmtInt } from "../format";
import { useNames } from "../components/names";
import { fillDays, fmtChangePct, numOrZero, useFindingAgg, useFindingData } from "../components/hooks";
import { AreaContent, Card } from "../components/primitives";
import { ChartNote } from "../components/charts";
import { Columns } from "../components/charts_more";
import { HeatGrid, quantileEdges } from "../components/design_parts";
import { csvField } from "../components/findings_table";
import { DAY_KEY, DAY_ROWS_MAX, MONTH_ROWS_MAX, GrainSwitch, fmtMonth, latest, monthOf, monthRun, partialMonths } from "../components/grain";
import type { Grain } from "../components/grain";
import { baName } from "./tab_cost_before_after";
import { gridOf, ready } from "./cost_trend_by";
import { startsText } from "./startup_waits";
import type { TabProps } from "../components/primitives";

const QID = "perf_daily_by_resource";
const GROUP = ["resource_type", "resource_key", "usage_date"];
type Unit = "gb" | "wait" | "pct" | "seconds" | "count";
// sum: adds up across warehouses and days; ratio: num over den, worked out over whatever is summed.
interface Measure { key: string; label: string; num: string; den?: string; unit: Unit; badUp: boolean; what: string }
const MEASURES: Measure[] = [
  { key: "spill", label: "Spill", num: "spilled_gb", unit: "gb", badUp: true, what: "Data queries wrote to local disk because memory ran out. A bigger warehouse or a leaner query cuts it." },
  { key: "shuffle", label: "Shuffle", num: "shuffle_gb", unit: "gb", badUp: true, what: "Data moved between nodes for joins and aggregations." },
  { key: "slot", label: "Slot wait", num: "slot_wait_s", unit: "wait", badUp: true, what: "Time queries waited for a free slot because the warehouse was full. More clusters (max scaling) cut it." },
  { key: "provision", label: "Provisioning wait", num: "provision_clock_s", unit: "wait", badUp: true, what: "Clock time queries waited for their warehouse to start; queries waiting together count once. On classic or pro a longer auto-stop cuts it; on serverless a long wait means a slow or failed start." },
  { key: "disk_cache", label: "Disk cache", num: "cache_read_gb", den: "read_gb", unit: "pct", badUp: false, what: "Share of the data read that came from the warehouse's local disk cache. It drops after the warehouse stops." },
  { key: "result_cache", label: "Result cache", num: "result_cache_runs", den: "runs", unit: "pct", badUp: false, what: "Share of queries answered from the result cache, at no compute." },
  { key: "time", label: "Avg query time", num: "total_s", den: "runs", unit: "seconds", badUp: true, what: "Average duration per query, waits included." },
  { key: "queries", label: "Queries", num: "runs", unit: "count", badUp: false, what: "Statements run." },
  { key: "failed", label: "Failed", num: "failed", unit: "count", badUp: true, what: "Statements that ended FAILED." },
];
const NEW_COLS = new Set(["slot_wait_s", "provision_s", "provision_clock_s", "shuffle_gb", "read_gb", "cache_read_gb", "result_cache_runs"]);
const TOP_N = 5;
const SHOWN = 20;
const HEAT_ROWS = 10;
// Under this a change is noise.
const NOISE_PCT = 5;

function fmtUnit(unit: Unit, v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "-";
  if (unit === "gb") return fmtGb(v, Math.abs(v) < 10 ? 2 : 0);
  if (unit === "wait") return fmtDuration(v);
  if (unit === "pct") return `${(v * 100).toFixed(0)}%`;
  if (unit === "seconds") return Math.abs(v) >= 1 && Math.abs(v) < 10 ? `${v.toFixed(1)} s` : fmtDuration(v);
  return fmtInt(Math.round(v));
}

// Charts plot waits in minutes and shares as 0-100; the table and notes keep the readable form.
function plotOf(unit: Unit, v: number | null): number {
  if (v == null) return 0;
  return unit === "wait" ? v / 60 : unit === "pct" ? v * 100 : v;
}
function axisOf(unit: Unit, v: number): string {
  if (unit === "wait") return `${fmtInt(Math.round(v))} min`;
  if (unit === "pct") return `${Math.round(v)}%`;
  return fmtUnit(unit, v);
}
const UNIT_WORD: Record<Unit, string> = { gb: "GB", wait: "seconds", pct: "share 0-1", seconds: "seconds per query", count: "count" };

// A warehouse's waits added up and its own starts, over the periods on screen.
interface ProvRow { added: number; starts: number; startS: number; longest: number; failed: number }

interface Row { key: string; name: string; values: (number | null)[]; value: number | null; runs: number; last: number | null; prev: number | null }

function ratioOf(n: number, d: number): number | null {
  return d > 0 ? n / d : null;
}

type G = Map<string, Map<string, number>>;
// One grain's sums per warehouse and period; den is set for a ratio measure.
interface GridSet { num: G; den: G | null; runs: G; keys: string[] }

// The measure for one warehouse (or all, key null) over some periods: summed, or num over den.
function overIn(g: GridSet, key: string | null, ks: string[]): number | null {
  if (!ks.length) return null;
  const sumMap = (mp: G) => {
    let t = 0;
    (key ? [mp.get(key)] : [...mp.values()]).forEach((byK) => ks.forEach((k) => { t += (byK && byK.get(k)) || 0; }));
    return t;
  };
  const n = sumMap(g.num);
  return g.den ? ratioOf(n, sumMap(g.den)) : n;
}

function QueryTrendContent({ filters, meta, onVerdict }: TabProps) {
  useNames();
  const [mKey, setMKey] = React.useState("slot");
  const [grain, setGrain] = React.useState<Grain>("day");
  const [picked, setPicked] = React.useState<string | null>(null);
  const [showAll, setShowAll] = React.useState(false);
  const probe = useFindingData(QID, filters.window, filters.workspaceIds, filters.envs, 1);
  const cols = probe.data && probe.data.columns ? new Set((probe.data.columns as any[]).map((c) => c.name)) : null;
  const m = MEASURES.find((x) => x.key === mKey) || MEASURES[0];
  // An export from before these columns existed answers them with an error, so they are never asked.
  const held = !!cols && [m.num, m.den].every((c) => !c || !NEW_COLS.has(c) || cols.has(c));
  const numAgg = useFindingAgg(held ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "sum", m.num);
  const denAgg = useFindingAgg(held && m.den && m.den !== "runs" ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "sum", m.den || null);
  const runsAgg = useFindingAgg(held ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "sum", "runs");
  const denState = m.den === "runs" ? runsAgg : denAgg;
  // Provisioning wait also shows every query's wait added up and the warehouses' own starts.
  const isProv = held && m.key === "provision";
  const waitAgg = useFindingAgg(isProv ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "sum", "provision_s");
  const startsAgg = useFindingAgg(isProv ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "sum", "starts");
  const startSAgg = useFindingAgg(isProv ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "sum", "start_s");
  const failedAgg = useFindingAgg(isProv ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "sum", "failed_starts");
  const longestAgg = useFindingAgg(isProv ? QID : null, filters.window, filters.workspaceIds, filters.envs, GROUP, "max", "start_s_max");
  const isRatio = !!m.den;

  // The daily table leaves today out, so an export that includes today drops it here too.
  const todayIn = !!(meta && meta.direct_export && meta.direct_export.includes_today);
  const days = React.useMemo(() => {
    const filled = fillDays([], meta && meta.as_of_date, filters.window, meta && meta.snapshot_days).map((d) => d.day);
    if (filled.length) return todayIn ? filled.slice(0, -1) : filled;
    if (!ready(runsAgg)) return filled;
    return [...new Set(runsAgg.data!.groups.map((g) => String(g.key[2]).slice(0, 10)).filter((d) => DAY_KEY.test(d)))].sort();
  }, [meta, filters.window, runsAgg, todayIn]);
  const { open: openMonth, cut: cutMonth } = partialMonths(days, true);
  const isFull = (k: string) => k !== openMonth && k !== cutMonth;

  const allReady = ready(numAgg) && ready(runsAgg) && (!isRatio || ready(denState));
  // Both grains built once per answer: the main view and the picked warehouse's card read them.
  const grids = React.useMemo((): { day: GridSet; month: GridSet } | null => {
    if (!allReady) return null;
    const build = (toPeriod: (d: string) => string, keys: string[]): GridSet => {
      const num = gridOf(numAgg, 1, 2, 1, toPeriod, "warehouse");
      const runs = gridOf(runsAgg, 1, 2, 1, toPeriod, "warehouse");
      const den = isRatio ? (m.den === "runs" ? runs : gridOf(denState, 1, 2, 1, toPeriod, "warehouse")) : null;
      return { num, den, runs, keys };
    };
    return { day: build((d) => d, latest(days, DAY_ROWS_MAX)), month: build(monthOf, latest(monthRun(days.map(monthOf)), MONTH_ROWS_MAX)) };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allReady, numAgg, runsAgg, denState, days, isRatio, m.den]);
  const cur = grids ? grids[grain] : null;
  const over = (key: string | null, ks: string[]): number | null => (cur ? overIn(cur, key, ks) : null);
  // Last 7 days against the 7 before, or the last full month against the one before.
  const lastPrev = (key: string | null): [number | null, number | null] => {
    if (!cur) return [null, null];
    if (grain === "day") return cur.keys.length >= 14 ? [over(key, cur.keys.slice(-7)), over(key, cur.keys.slice(-14, -7))] : [null, null];
    const full = cur.keys.filter(isFull);
    return [full.length ? over(key, full.slice(-1)) : null, full.length > 1 ? over(key, full.slice(-2, -1)) : null];
  };

  // Per warehouse over the periods on screen: waits added up and starts; the longest start is a max.
  const prov = React.useMemo((): { byWh: Map<string, ProvRow>; total: ProvRow } | null => {
    if (!isProv || !cur || ![waitAgg, startsAgg, startSAgg, failedAgg, longestAgg].every(ready)) return null;
    const toP = grain === "day" ? (d: string) => d : monthOf;
    const inKeys = new Set(cur.keys);
    const fold = (s: any, max: boolean) => {
      const out = new Map<string, number>();
      gridOf(s, 1, 2, 1, (d) => d, "warehouse").forEach((byDay, wh) => {
        let t = 0;
        byDay.forEach((v, d) => { if (inKeys.has(toP(d))) t = max ? Math.max(t, v) : t + v; });
        out.set(wh, t);
      });
      return out;
    };
    const [w, n, st, fl, lg] = [fold(waitAgg, false), fold(startsAgg, false), fold(startSAgg, false), fold(failedAgg, false), fold(longestAgg, true)];
    const byWh = new Map<string, ProvRow>();
    const total: ProvRow = { added: 0, starts: 0, startS: 0, longest: 0, failed: 0 };
    new Set([...w.keys(), ...n.keys()]).forEach((k) => {
      const x = { added: w.get(k) || 0, starts: n.get(k) || 0, startS: st.get(k) || 0, longest: lg.get(k) || 0, failed: fl.get(k) || 0 };
      byWh.set(k, x);
      total.added += x.added; total.starts += x.starts; total.startS += x.startS; total.failed += x.failed;
      total.longest = Math.max(total.longest, x.longest);
    });
    return { byWh, total };
  }, [isProv, cur, grain, waitAgg, startsAgg, startSAgg, failedAgg, longestAgg]);
  const rows = React.useMemo((): Row[] | null => {
    if (!cur) return null;
    // A warehouse that only started (no query that day) still gets a row for provisioning wait.
    const started = prov ? [...prov.byWh.entries()].filter(([, x]) => x.starts > 0).map(([k]) => k) : [];
    const keys = new Set<string>([...cur.num.keys(), ...cur.runs.keys(), ...started]);
    const out: Row[] = [];
    keys.forEach((key) => {
      const runs = cur.keys.reduce((a, k) => a + ((cur.runs.get(key) && cur.runs.get(key)!.get(k)) || 0), 0);
      const value = over(key, cur.keys);
      if (!runs && !value && !started.includes(key)) return;
      const [last, prev] = lastPrev(key);
      out.push({ key, name: baName("warehouse", key, "warehouses"), values: cur.keys.map((k) => overIn(cur, key, [k])), value, runs, last, prev });
    });
    // A share means little on a few queries, so shares list the busiest warehouses first.
    const byValue = (a: Row, b: Row) => numOrZero(b.value) - numOrZero(a.value);
    const byRuns = (a: Row, b: Row) => b.runs - a.runs;
    const [first, second] = m.unit === "pct" ? [byRuns, byValue] : [byValue, byRuns];
    return out.sort((a, b) => first(a, b) || second(a, b) || a.name.localeCompare(b.name));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur, grain, openMonth, cutMonth, m.unit, prov]);

  const keys = cur ? cur.keys : [];
  const sel = rows && rows.length ? rows.find((r) => r.key === picked) || rows[0] : null;
  const shown = rows ? (showAll ? rows : rows.slice(0, SHOWN)) : null;
  const total = over(null, keys);
  const [tLast, tPrev] = lastPrev(null);
  const xFmt = grain === "month" ? (k: string) => fmtMonth(k) : undefined;
  const span = keys.length ? (grain === "day" ? `${fmtDayShort(keys[0])} - ${fmtDayShort(keys[keys.length - 1])}` : `${fmtMonth(keys[0])} - ${fmtMonth(keys[keys.length - 1])}`) : "";
  const fmt = (v: number | null) => fmtUnit(m.unit, v);
  const plotVal = (v: number | null) => plotOf(m.unit, v);
  const axisFmt = (v: number) => axisOf(m.unit, v);

  const change = (last: number | null, prev: number | null): { text: string; tone: string } => {
    if (last == null || prev == null) return { text: "-", tone: "" };
    if (m.unit === "pct") {
      const pts = (last - prev) * 100;
      return { text: `${pts >= 0 ? "+" : ""}${pts.toFixed(0)} pts`, tone: Math.abs(pts) < 2 ? "" : (pts > 0) === m.badUp ? "ba-up" : "ba-down" };
    }
    if (prev <= 0) return { text: last > 0 ? "new" : "-", tone: last > 0 && m.badUp ? "ba-up" : "" };
    const pct = ((last - prev) / prev) * 100;
    return { text: fmtChangePct(pct), tone: Math.abs(pct) < NOISE_PCT ? "" : (pct > 0) === m.badUp ? "ba-up" : "ba-down" };
  };
  const changeHead = grain === "day" ? "Last 7 days vs 7 before" : "Last full month vs before";

  const verdict = rows && total != null
    ? `SQL warehouses, ${m.label.toLowerCase()}: ${fmt(total)} in ${span}${tLast != null && tPrev != null ? `, ${change(tLast, tPrev).text} ${grain === "day" ? "in the last 7 days" : "in the last full month"}` : ""}.`
    : null;
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);

  const downloadCsv = () => {
    if (!rows || !keys.length) return;
    const r4 = (v: number | null) => (v == null ? "" : Math.round(v * 10000) / 10000);
    const lines = [[`Warehouse (${m.label}, ${UNIT_WORD[m.unit]})`, ...keys.map((k) => (grain === "day" ? k : k.slice(0, 7))), "Period"].map(csvField).join(",")];
    rows.forEach((r) => lines.push([r.name, ...r.values.map(r4), r4(r.value)].map(csvField).join(",")));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
    a.download = `warehouse-${m.key}-${grain}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const picker = (
    <div className="ws-actions">
      {MEASURES.map((x) => (
        <button key={x.key} type="button" className={x.key === m.key ? "on" : ""} onClick={() => setMKey(x.key)}>{x.label}</button>
      ))}
    </div>
  );
  const pending = [probe, numAgg, runsAgg, ...(isRatio ? [denState] : [])].find((s) => !ready(s as any)) || numAgg;
  // The busiest warehouses' cells, shaded by where each value sits among all of them.
  const heat = React.useMemo(() => {
    if (!rows || !rows.length) return null;
    const top = rows.slice(0, HEAT_ROWS);
    const edges = quantileEdges(top.flatMap((r) => r.values));
    return edges ? { rows: top, edges } : null;
  }, [rows]);
  const chartTotal = cur && isRatio
    ? [{ name: m.label, values: keys.map((k) => plotVal(over(null, [k]))) }]
    : rows ? rows.map((r) => ({ name: r.name, id: r.key, values: r.values.map(plotVal) })) : null;

  return (
    <div>
      <Card title="SQL warehouse trend" right={<GrainSwitch value={grain} onChange={(g) => { setGrain(g); setShowAll(false); }} />}>
        {picker}
        <div className="metric-note">{m.what}</div>
        {!held && cols ? (
          <div className="muted">Not in this export: re-run the export to get slot wait, provisioning wait, shuffle and cache by day.</div>
        ) : rows && chartTotal ? (
          rows.length ? (
            <React.Fragment>
              <Columns
                days={keys}
                series={chartTotal}
                n={TOP_N}
                valueFmt={axisFmt}
                unitLabel={m.unit === "wait" ? "minutes" : m.unit === "gb" ? "GB" : m.unit === "pct" ? "%" : m.unit === "seconds" ? "seconds" : m.label.toLowerCase()}
                xFmt={xFmt}
                xName={grain === "day" ? "Day" : "Month"}
                partial={grain === "month" ? [openMonth, cutMonth] : null}
                note={`${span}: ${fmt(total)}${isRatio ? " over all warehouses" : ` across ${fmtInt(rows.length)} warehouse${rows.length === 1 ? "" : "s"}${rows.length > TOP_N ? `; the top ${TOP_N} stacked, the rest as Other` : ""}`}.${prov ? ` Queries waited ${fmtDuration(prov.total.added)} added up; warehouses ${startsText(prov.total)}.` : ""}${grain === "month" ? " Months inside this export's window only." : ""}`}
                ariaLabel={`${m.label} by ${grain}`}
              />
              {heat && (
                <>
                  <div className="chart-card-title" style={{ marginTop: 14 }}>{`By warehouse and ${grain === "day" ? "day" : "month"}${rows.length > heat.rows.length ? `, top ${fmtInt(heat.rows.length)}` : ""}`}</div>
                  <HeatGrid
                    rows={heat.rows.map((r) => ({ key: r.key, label: r.name, title: r.name, right: fmt(r.value) }))}
                    cols={keys.map((k) => ({ key: k, label: grain === "day" ? fmtDayShort(k) : fmtMonth(k), partial: grain === "month" && !isFull(k) }))}
                    cell={(ri, ci) => heat.rows[ri].values[ci]}
                    edges={heat.edges}
                    fmt={(v) => fmt(v)}
                    rightHead={grain === "day" ? "window" : "months"}
                    onRow={setPicked}
                    activeRow={sel ? sel.key : null}
                    compact={grain === "day"}
                    ariaLabel={`${m.label} by warehouse and ${grain}`}
                  />
                </>
              )}
              <div className="ba-table-head trend-by-head">
                <span className="muted">Every warehouse. Click one for its own days and months.</span>
                <button type="button" className="csv-download-btn" onClick={downloadCsv}>Download CSV</button>
              </div>
              <div className="data-table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Warehouse</th>
                      <th className="num">{m.label}<div className="ba-span">{span}</div></th>
                      {prov && <><th className="num">Added up</th><th className="num">Starts</th><th className="num">Time starting</th><th className="num">Longest start</th></>}
                      <th className="num">Queries</th>
                      <th className="num">{changeHead}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(shown || []).map((r) => {
                      const c = change(r.last, r.prev);
                      return (
                        <tr key={r.key} className={`ba-row${sel && r.key === sel.key ? " on" : ""}`} onClick={() => setPicked(r.key)}>
                          <td><button type="button" className="ba-pick" onClick={() => setPicked(r.key)}>{r.name}</button></td>
                          <td className="num mono">{fmt(r.value)}</td>
                          {prov && (() => {
                            const x = prov.byWh.get(r.key);
                            return <><td className="num mono">{x ? fmtDuration(x.added) : "-"}</td>
                              <td className="num mono">{x && x.starts ? `${fmtInt(x.starts)}${x.failed ? ` (${fmtInt(x.failed)} failed)` : ""}` : "-"}</td>
                              <td className="num mono">{x && x.starts ? fmtDuration(x.startS) : "-"}</td>
                              <td className="num mono">{x && x.starts ? fmtDuration(x.longest) : "-"}</td></>;
                          })()}
                          <td className="num mono">{fmtInt(r.runs)}</td>
                          <td className={`num mono ${c.tone}`}>{c.text}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {rows.length > SHOWN && (
                <button type="button" className="ov-link-btn" onClick={() => setShowAll(!showAll)}>
                  {showAll ? `Show the top ${SHOWN}` : `Show all ${fmtInt(rows.length)}`}
                </button>
              )}
              {rows.some((r) => r.key.endsWith(":other")) && <div className="metric-note">The 100 warehouses with the most query time have their own rows; the rest are one "Other" row per workspace.</div>}
            </React.Fragment>
          ) : <div className="muted">{`No SQL warehouse queries in ${span}.`}</div>
        ) : <ChartNote state={pending as any} label={QID} />}
      </Card>

      {sel && grids && <WarehouseTrendCard name={sel.name} m={m} days={grids.day.keys} months={grids.month.keys}
        dayVal={(k) => overIn(grids.day, sel.key, [k])} monthVal={(k) => overIn(grids.month, sel.key, [k])}
        isFull={isFull} openMonth={openMonth} />}
    </div>
  );
}

function WarehouseTrendCard({ name, m, days, months, dayVal, monthVal, isFull, openMonth }: {
  name: string; m: Measure; days: string[]; months: string[]; dayVal: (k: string) => number | null; monthVal: (k: string) => number | null;
  isFull: (k: string) => boolean; openMonth: string | null;
}) {
  const plot = (v: number | null) => plotOf(m.unit, v);
  const axisFmt = (v: number) => axisOf(m.unit, v);
  const dVals = days.map(dayVal);
  const mVals = months.map(monthVal);
  const peakAt = dVals.reduce((best: number, v, i) => (numOrZero(v) > numOrZero(dVals[best]) ? i : best), 0);
  const full = months.map((k, i) => ({ k, v: mVals[i] })).filter((x) => isFull(x.k));
  const lastFull = full.length ? full[full.length - 1] : null;
  const openIdx = openMonth ? months.indexOf(openMonth) : -1;
  return (
    <Card title={name} right={<span className="muted">{`${m.label}, by day and by month`}</span>}>
      <div className="grid-2">
        <div>
          <div className="chart-card-title">By day</div>
          <Columns days={days} series={[{ name: m.label, values: dVals.map(plot) }]} valueFmt={axisFmt} unitLabel={m.label}
            note={days.length ? `Highest ${fmtUnit(m.unit, dVals[peakAt])} on ${fmtDayShort(days[peakAt])}.` : null} ariaLabel={`${name} ${m.label} by day`} />
        </div>
        <div>
          <div className="chart-card-title">By month</div>
          {months.length > 1
            ? <Columns days={months} series={[{ name: m.label, values: mVals.map(plot) }]} valueFmt={axisFmt} unitLabel={m.label} xFmt={(k) => fmtMonth(k)} xName="Month"
                note={[lastFull ? `${fmtMonth(lastFull.k)} ${fmtUnit(m.unit, lastFull.v)}` : null, openIdx >= 0 ? `${fmtMonth(openMonth)} so far ${fmtUnit(m.unit, mVals[openIdx])}` : null, "Months inside this export's window only."].filter(Boolean).join(" · ")}
                ariaLabel={`${name} ${m.label} by month`} />
            : <div className="muted">{`Only ${months.length ? fmtMonth(months[0]) : "one month"} in this window: pick a longer window for a monthly trend.`}</div>}
        </div>
      </div>
    </Card>
  );
}

AreaContent.register("queries", "trend", QueryTrendContent);
