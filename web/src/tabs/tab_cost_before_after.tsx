// Cost > Before & after: one resource's average day (or average query or run) before a chosen date
// against from that date on, and what drove the change, so a fix made a week ago reads as a number
// with a reason on the next rerun.

import React from "react";

import { estLabel, fmtDayShort, fmtDbu, fmtDuration, fmtInt, fmtMoney, fmtMoneyShort } from "../format";
import { resolveName, useNames, wsPhrase } from "../components/names";
import { fillDays, fmtChangePct, numOrZero, useFindingAgg } from "../components/hooks";
import { AreaContent, Card, CaveatNote } from "../components/primitives";
import { ChartNote, shortId } from "../components/charts";
import { Columns } from "../components/charts_more";
import { csvField } from "../components/findings_table";
import { enumLabel } from "../components/labels";
import { COST_MONEY_COL } from "../components/tab_registry";
import { productWords } from "./tab_money";
import type { AggState, AggregateData, Filters } from "../types";
import type { TabProps } from "../components/primitives";

const COST_QID = "cost_daily_by_resource";
const PERF_QID = "perf_daily_by_resource";
const CAUSE_QID = "perf_failure_causes_daily";
const BA_GROUP = ["resource_type", "resource_key", "usage_date"];
const BA_LEVELS = [
  { key: "workspace", label: "Workspace", plural: "workspaces" },
  { key: "warehouse", label: "Warehouse", plural: "warehouses" },
  { key: "job", label: "Job", plural: "jobs" },
  { key: "cluster", label: "Cluster", plural: "all-purpose clusters" },
  { key: "pipeline", label: "Pipeline", plural: "pipelines" },
];
// What one run is called at each level.
const RUN_WORD: Record<string, string> = { warehouse: "query", job: "run", pipeline: "update" };
const RUNS_WORD: Record<string, string> = { warehouse: "Queries", job: "Runs", pipeline: "Updates" };

type Kind = "money" | "dbu" | "count" | "seconds" | "hours" | "gb";
// A metric with `den` is a ratio over the whole side (time per query); one without is a per-day average.
interface BaMetric { key: string; label: (level: string) => string; levels: string[]; qid: string; num: string; den?: string; kind: Kind; neutral?: boolean }
const ALL_LEVELS = BA_LEVELS.map((l) => l.key);
const BA_METRICS: BaMetric[] = [
  { key: "usd", label: () => "$", levels: ALL_LEVELS, qid: COST_QID, num: "usd_list", kind: "money" },
  { key: "dbus", label: () => "DBUs", levels: ALL_LEVELS, qid: COST_QID, num: "dbus", kind: "dbu" },
  { key: "usd_run", label: (l) => `$ per ${RUN_WORD[l]}`, levels: ["job", "pipeline"], qid: COST_QID, num: "usd_list", den: "billed_runs", kind: "money" },
  { key: "usd_hour", label: () => "$ per hour", levels: ["warehouse", "cluster"], qid: COST_QID, num: "usd_list", den: "billed_hours", kind: "money" },
  { key: "hours", label: () => "Hours running", levels: ["warehouse", "cluster"], qid: COST_QID, num: "billed_hours", kind: "hours", neutral: true },
  { key: "time", label: (l) => `Avg ${RUN_WORD[l]} time`, levels: ["warehouse", "job", "pipeline"], qid: PERF_QID, num: "total_s", den: "runs", kind: "seconds" },
  { key: "setup", label: () => "Avg task start-up", levels: ["job"], qid: PERF_QID, num: "setup_s", den: "task_runs", kind: "seconds" },
  { key: "runs", label: (l) => RUNS_WORD[l], levels: ["warehouse", "job", "pipeline"], qid: PERF_QID, num: "runs", kind: "count", neutral: true },
  { key: "failed", label: (l) => `Failed ${RUNS_WORD[l].toLowerCase()}`, levels: ["warehouse", "job", "pipeline"], qid: PERF_QID, num: "failed", kind: "count" },
  { key: "queue", label: () => "Avg queue wait", levels: ["warehouse"], qid: PERF_QID, num: "queued_s", den: "runs", kind: "seconds" },
  { key: "spill", label: () => "Spilled GB", levels: ["warehouse"], qid: PERF_QID, num: "spilled_gb", kind: "gb" },
];
// Under this a change is rounding, not a move.
const BA_NOISE: Record<Kind, number> = { money: 0.01, dbu: 0.1, count: 0.05, seconds: 0.05, hours: 0.05, gb: 0.001 };
const BA_SHOWN = 20;
// Fewer days than this on a side and one odd day moves its average a lot.
const BA_FEW_DAYS = 3;

function fmtKind(kind: Kind, v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "-";
  const a = Math.abs(v);
  if (kind === "money") return fmtMoney(v, a < 100 ? 2 : 0);
  if (kind === "dbu") return fmtDbu(v, a < 100 ? 1 : 0);
  if (kind === "seconds") return a >= 1 && a < 10 ? `${v.toFixed(1)} s` : fmtDuration(v);
  if (kind === "hours") return `${v.toFixed(1)} h`;
  if (kind === "gb") return `${a < 10 ? v.toFixed(2) : fmtInt(Math.round(v))} GB`;
  return a < 10 ? v.toFixed(1) : fmtInt(Math.round(v));
}

// "Avg query time" -> "avg query time", but "DBUs" stays "DBUs".
function midSentence(label: string): string {
  return /^[A-Z][A-Z]/.test(label) ? label : label.charAt(0).toLowerCase() + label.slice(1);
}

interface Move { before: number | null; after: number | null; change: number | null; pct: number | null }
function move(before: number | null, after: number | null): Move {
  const change = before != null && after != null ? after - before : null;
  return { before, after, change, pct: before && change != null ? (change / before) * 100 : null };
}
function dirOf(kind: Kind, mv: Move): number {
  return mv.change == null || Math.abs(mv.change) < BA_NOISE[kind] ? 0 : Math.sign(mv.change);
}
function changeText(kind: Kind, mv: Move): string {
  if (mv.change == null) return mv.before == null ? "none before" : "none after";
  if (dirOf(kind, mv) === 0) return "no change";
  return `${mv.change > 0 ? "+" : "-"}${fmtKind(kind, Math.abs(mv.change))} (${mv.pct == null ? "new" : fmtChangePct(mv.pct)})`;
}
function toneOf(kind: Kind, mv: Move, neutral?: boolean): string {
  const d = dirOf(kind, mv);
  return neutral || d === 0 ? "" : d < 0 ? "ba-down" : "ba-up";
}

function isReady(s: AggState): s is AggState & { data: AggregateData } {
  return s.phase === "ready" && s.outcome === "ok_rows" && s.data != null;
}
// Settled with no rows in reach: zeros, not a gap.
function isEmpty(s: AggState): boolean {
  return s.phase === "ready" && typeof s.outcome === "string" && s.outcome.startsWith("ok_empty");
}

// key -> day -> summed value, from groups whose first column (when typeFilter is set) matches it.
function aggMaps(state: AggState, days: string[], scale: number, typeFilter: string | null, keyAt = 1, dayAt = 2): Map<string, Map<string, number>> {
  const out = new Map<string, Map<string, number>>();
  if (!isReady(state)) return out;
  state.data.groups.forEach((g) => {
    if (typeFilter && g.key[0] !== typeFilter) return;
    const key = String(g.key[keyAt] == null ? "account" : g.key[keyAt]);
    const day = String(g.key[dayAt]).slice(0, 10);
    if (day < days[0] || day > days[days.length - 1]) return;
    if (!out.has(key)) out.set(key, new Map());
    const m = out.get(key)!;
    m.set(day, (m.get(day) || 0) + numOrZero(g.value) * scale);
  });
  return out;
}
function sides(map: Map<string, number> | undefined, fix: string): { b: number; a: number } {
  let b = 0, a = 0;
  (map || new Map<string, number>()).forEach((v, d) => { if (d < fix) b += v; else a += v; });
  return { b, a };
}

// resource_key is the workspace id for a workspace, else "<workspace_id>:<resource_id>".
export function baName(level: string, key: string, plural: string): string {
  if (level === "workspace") return key === "account" ? "Account-level (no workspace)" : wsPhrase(key);
  const i = key.indexOf(":");
  const ws = key.slice(0, i) === "account" ? null : key.slice(0, i);
  const id = key.slice(i + 1);
  const where = ws ? wsPhrase(ws) : "account-level";
  if (id === "other") return `Other ${plural} · ${where}`;
  const name = resolveName(level, ws, id) || `${level} ${shortId(id)}`;
  // A warehouse name shared across workspaces already carries its workspace.
  return name.endsWith(` · ${where}`) ? name : `${name} · ${where}`;
}
function keyWorkspace(level: string, key: string): string | null {
  const ws = level === "workspace" ? key : key.slice(0, key.indexOf(":"));
  return ws && ws !== "account" ? ws : null;
}

// ─────────── What changed: the selected resource's change split into its drivers ───────────
const DRIVER_COLS = [
  { id: "usd", qid: COST_QID, col: "usd_list", money: true, levels: ["warehouse", "job", "cluster", "pipeline"] },
  { id: "dbus", qid: COST_QID, col: "dbus", levels: ["cluster"] },
  { id: "hours", qid: COST_QID, col: "billed_hours", levels: ["warehouse", "cluster"] },
  { id: "bruns", qid: COST_QID, col: "billed_runs", levels: ["job", "pipeline"] },
  { id: "runs", qid: PERF_QID, col: "runs", levels: ["warehouse", "job", "pipeline"] },
  { id: "failed", qid: PERF_QID, col: "failed", levels: ["warehouse", "job", "pipeline"] },
  { id: "total", qid: PERF_QID, col: "total_s", levels: ["warehouse", "job", "pipeline"] },
  { id: "queued", qid: PERF_QID, col: "queued_s", levels: ["warehouse"] },
  { id: "exec", qid: PERF_QID, col: "exec_s", levels: ["warehouse", "job"] },
  { id: "spill", qid: PERF_QID, col: "spilled_gb", levels: ["warehouse"] },
  { id: "tasks", qid: PERF_QID, col: "task_runs", levels: ["job"] },
  { id: "setup", qid: PERF_QID, col: "setup_s", levels: ["job"] },
];
interface Driver { label: string; kind: Kind; mv: Move; neutral?: boolean; sub?: boolean }
type SideFn = (id: string) => { b: number; a: number } | null;
type DriverSpec = [string, Kind, Move | null, { neutral?: boolean; sub?: boolean }?];

function driverRows(level: string, S: SideFn, nB: number, nA: number): Driver[] {
  const perDay = (id: string): Move | null => { const s = S(id); return s ? move(s.b / nB, s.a / nA) : null; };
  const ratio = (n: string, d: string, mult = 1): Move | null => {
    const sn = S(n), sd = S(d);
    return sn && sd ? move(sd.b > 0 ? (sn.b / sd.b) * mult : null, sd.a > 0 ? (sn.a / sd.a) * mult : null) : null;
  };
  const spec: DriverSpec[] = level === "warehouse" ? [
    ["Spend a day", "money", perDay("usd")],
    ["Hours running a day", "hours", perDay("hours"), { neutral: true }],
    ["$ per hour running", "money", ratio("usd", "hours")],
    ["Queries a day", "count", perDay("runs"), { neutral: true }],
    ["$ per 1,000 queries", "money", ratio("usd", "runs", 1000)],
    ["Avg query time", "seconds", ratio("total", "runs")],
    ["Queue wait per query", "seconds", ratio("queued", "runs"), { sub: true }],
    ["Execution per query", "seconds", ratio("exec", "runs"), { sub: true }],
    ["Spilled GB a day", "gb", perDay("spill")],
    ["Failed queries a day", "count", perDay("failed")],
  ] : level === "job" ? [
    ["Spend a day", "money", perDay("usd")],
    ["Runs billed a day", "count", perDay("bruns"), { neutral: true }],
    ["$ per run", "money", ratio("usd", "bruns")],
    ["Avg run time", "seconds", ratio("total", "runs")],
    ["Start-up per task", "seconds", ratio("setup", "tasks"), { sub: true }],
    ["Execution per task", "seconds", ratio("exec", "tasks"), { sub: true }],
    ["Failed runs a day", "count", perDay("failed")],
  ] : level === "cluster" ? [
    ["Spend a day", "money", perDay("usd")],
    ["Hours running a day", "hours", perDay("hours"), { neutral: true }],
    ["$ per hour running", "money", ratio("usd", "hours")],
    ["DBUs per hour running", "dbu", ratio("dbus", "hours")],
  ] : level === "pipeline" ? [
    ["Spend a day", "money", perDay("usd")],
    ["Updates billed a day", "count", perDay("bruns"), { neutral: true }],
    ["$ per update", "money", ratio("usd", "bruns")],
    ["Avg update time", "seconds", ratio("total", "runs")],
    ["Failed updates a day", "count", perDay("failed")],
  ] : [];
  return spec.filter((s) => s[2] != null).map(([label, kind, mv, o]) => ({ label, kind, mv: mv!, ...(o || {}) }));
}

function pctWord(mv: Move): string { return mv.pct == null ? "new" : fmtChangePct(mv.pct); }
// Spend = how much it ran x what each hour or run cost; the factor whose log-ratio is larger drove it.
function spendReading(level: string, ds: Driver[]): string | null {
  const find = (l: string) => ds.find((d) => d.label === l);
  const spend = find("Spend a day");
  const vol = find(level === "job" ? "Runs billed a day" : level === "pipeline" ? "Updates billed a day" : "Hours running a day");
  const rate = find(level === "job" ? "$ per run" : level === "pipeline" ? "$ per update" : "$ per hour running");
  if (!spend || !vol || !rate || dirOf("money", spend.mv) === 0) return null;
  // A resource that stopped or started is the whole story.
  const noun = level === "job" ? "runs" : level === "pipeline" ? "updates" : "hours";
  if (vol.mv.before! > 0 && vol.mv.after === 0) return `No ${noun} billed from the date: it stopped running, ${changeText("money", spend.mv)} a day.`;
  if (vol.mv.before === 0 && vol.mv.after! > 0) return `No ${noun} billed before the date: it started running, ${changeText("money", spend.mv)} a day.`;
  const vals = [spend.mv, vol.mv, rate.mv].flatMap((x) => [x.before, x.after]);
  if (vals.some((v) => v == null || v <= 0)) return null;
  const lv = Math.log(vol.mv.after! / vol.mv.before!), lr = Math.log(rate.mv.after! / rate.mv.before!);
  const volWhy = level === "job" ? (lv > 0 ? "more runs" : "fewer runs")
    : level === "pipeline" ? (lv > 0 ? "more updates" : "fewer updates")
    : (lv > 0 ? "more hours running" : "fewer hours running");
  const rateWhy = level === "job" || level === "pipeline" ? `each ${RUN_WORD[level]} costing ${lr > 0 ? "more" : "less"}`
    : `each hour costing ${lr > 0 ? "more (a bigger size or more clusters)" : "less (a smaller size or fewer clusters)"}`;
  return `Spend ${spend.mv.change! > 0 ? "up" : "down"} ${pctWord(spend.mv)}: ${vol.label.toLowerCase()} ${pctWord(vol.mv)}, ${rate.label} ${pctWord(rate.mv)}, so mostly ${Math.abs(lv) >= Math.abs(lr) ? volWhy : rateWhy}.`;
}
// Time per query or task = waiting + doing; the part that moved more drove it.
function timeReading(level: string, ds: Driver[]): string | null {
  const [waitL, workL, waitWord, workWord] = level === "warehouse"
    ? ["Queue wait per query", "Execution per query", "queue wait", "execution"]
    : ["Start-up per task", "Execution per task", "start-up", "execution"];
  const wait = ds.find((d) => d.label === waitL), work = ds.find((d) => d.label === workL);
  if (!wait || !work || wait.mv.change == null || work.mv.change == null) return null;
  if (dirOf("seconds", wait.mv) === 0 && dirOf("seconds", work.mv) === 0) return null;
  const sign = (v: number) => (Math.abs(v) < BA_NOISE.seconds ? "no change" : `${v > 0 ? "+" : "-"}${fmtKind("seconds", Math.abs(v))}`);
  const waitBig = Math.abs(wait.mv.change) >= Math.abs(work.mv.change);
  const up = (waitBig ? wait : work).mv.change! > 0;
  const why = level === "warehouse"
    ? (waitBig ? `${up ? "more" : "less"} waiting for warehouse capacity` : `${up ? "slower" : "faster"} query execution`)
    : (waitBig ? `${up ? "slower" : "faster"} cluster start-up` : `${up ? "slower" : "faster"} task execution`);
  return `Per ${level === "warehouse" ? "query" : "task"}: ${waitWord} ${sign(wait.mv.change)}, ${workWord} ${sign(work.mv.change)}, so mostly ${why}.`;
}

function WhatChanged({ level, rowKey, name, fix, days, nBefore, nAfter, filters }: {
  level: string; rowKey: string; name: string; fix: string; days: string[]; nBefore: number; nAfter: number; filters: Filters;
}) {
  const ws = keyWorkspace(level, rowKey);
  const on = !!ws && !rowKey.endsWith(":other");
  const scoped = ws ? [ws] : null;
  // One read per driver column, scoped to the resource's own workspace so it stays small.
  const states = DRIVER_COLS.map((c) => useFindingAgg(on && c.levels.includes(level) ? c.qid : null, filters.window, scoped, filters.envs, BA_GROUP, "sum", c.col));
  const productAgg = useFindingAgg(on && level === "workspace" ? "cost_dollarized_by_sku_day" : null, filters.window, scoped, filters.envs, ["billing_origin_product", "usage_date"], "sum", COST_MONEY_COL);
  const causeAgg = useFindingAgg(on && (level === "warehouse" || level === "job") ? CAUSE_QID : null, filters.window, scoped, filters.envs, ["resource_key", "cause", "usage_date"], "sum", "failures");

  if (!on) return null;
  const needed = DRIVER_COLS.map((c, i) => ({ c, s: states[i] })).filter((x) => x.c.levels.includes(level));
  const waiting = [...needed.map((x) => x.s), ...(level === "workspace" ? [productAgg] : [])].find((s) => s.phase === "loading");
  if (waiting) return <Card title={`What changed: ${name}`}><ChartNote state={waiting} /></Card>;

  let pooledOut = false;
  const S: SideFn = (id) => {
    const x = needed.find((n) => n.c.id === id);
    if (!x || !(isReady(x.s) || isEmpty(x.s))) return null;
    const net = x.c.money && isReady(x.s) ? 1 - numOrZero(x.s.data.discount_pct) : 1;
    const maps = aggMaps(x.s, days, net, level);
    const own = maps.get(rowKey);
    // The cost and run tables keep their own top 100: here the resource sits in "Other".
    if (!own && maps.has(`${ws}:other`)) { pooledOut = true; return null; }
    return sides(own, fix);
  };
  let drivers = driverRows(level, S, nBefore, nAfter);
  const readings: string[] = [];
  if (level === "workspace") {
    const net = isReady(productAgg) ? 1 - numOrZero(productAgg.data.discount_pct) : 1;
    drivers = [...aggMaps(productAgg, days, net, null, 0, 1).entries()].map(([p, m]) => {
      const s = sides(m, fix);
      return { label: productWords(p === "account" ? null : p), kind: "money" as Kind, mv: move(s.b / nBefore, s.a / nAfter) };
    }).sort((x, y) => Math.abs(y.mv.change || 0) - Math.abs(x.mv.change || 0));
    const top = drivers[0];
    if (top && dirOf("money", top.mv) !== 0) readings.push(`Mostly ${top.label}: ${changeText("money", top.mv)} a day.`);
  } else {
    const sr = spendReading(level, drivers);
    const tr = level === "warehouse" || level === "job" ? timeReading(level, drivers) : null;
    if (sr) readings.push(sr);
    if (tr) readings.push(tr);
  }

  // Failures by cause, the biggest moves first.
  const causes = isReady(causeAgg)
    ? [...aggMaps(causeAgg, days, 1, rowKey).entries()].map(([cause, m]) => {
      const s = sides(m, fix);
      return { cause, mv: move(s.b / nBefore, s.a / nAfter) };
    }).sort((x, y) => Math.abs(y.mv.change || 0) - Math.abs(x.mv.change || 0)).slice(0, 5)
    : [];
  const topCause = causes[0];
  if (topCause && dirOf("count", topCause.mv) !== 0) {
    readings.push(`Biggest failure move: ${enumLabel(topCause.cause) || topCause.cause}, ${fmtKind("count", topCause.mv.before)} to ${fmtKind("count", topCause.mv.after)} a day.`);
  }

  return (
    <Card title={`What changed: ${name}`} right={<span className="muted">{`before and from ${fmtDayShort(fix)}`}</span>}>
      {readings.length > 0 && <div className="metric-note ba-reading">{readings.join(" ")}</div>}
      {drivers.length > 0 ? (
        <div className="data-table-wrap">
          <table className="data">
            <thead><tr><th>{level === "workspace" ? "Product, spend a day" : "Driver"}</th><th className="num">Before</th><th className="num">After</th><th className="num">Change</th></tr></thead>
            <tbody>
              {drivers.map((d) => (
                <tr key={d.label}>
                  <td className={d.sub ? "ba-sub" : ""}>{d.label}</td>
                  <td className="num mono">{fmtKind(d.kind, d.mv.before)}</td>
                  <td className="num mono">{fmtKind(d.kind, d.mv.after)}</td>
                  <td className={`num mono ${toneOf(d.kind, d.mv, d.neutral)}`}>{changeText(d.kind, d.mv)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="muted">Nothing to split this change by in this export.</div>}
      {pooledOut && <div className="metric-note">{`Some drivers are left out: in the ${RUN_WORD[level] ? "cost or run" : "cost"} data this ${level} is one of the smaller ones pooled into its workspace's "Other" row.`}</div>}
      {causes.length > 0 && (
        <div className="data-table-wrap ba-causes">
          <table className="data">
            <thead><tr><th>Failures by cause</th><th className="num">Before, per day</th><th className="num">After, per day</th><th className="num">Change</th></tr></thead>
            <tbody>
              {causes.map((c) => (
                <tr key={c.cause}>
                  <td>{enumLabel(c.cause) || c.cause}</td>
                  <td className="num mono">{fmtKind("count", c.mv.before)}</td>
                  <td className="num mono">{fmtKind("count", c.mv.after)}</td>
                  <td className={`num mono ${toneOf("count", c.mv)}`}>{changeText("count", c.mv)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

const BA_DIRS = [{ key: "all", label: "All" }, { key: "down", label: "Went down" }, { key: "up", label: "Went up" }];
interface BaRow extends Move { key: string; name: string; num: Map<string, number>; den: Map<string, number>; b: { num: number; den: number }; a: { num: number; den: number } }

function CostBeforeAfterContent({ filters, meta, onVerdict }: TabProps) {
  useNames();
  const [level, setLevel] = React.useState("warehouse");
  const [metricKey, setMetricKey] = React.useState("usd");
  const [fixDay, setFixDay] = React.useState<string | null>(null);
  const [picked, setPicked] = React.useState<string | null>(null);
  const [showAll, setShowAll] = React.useState(false);
  const [dirFilter, setDirFilter] = React.useState("all");
  const lv = BA_LEVELS.find((l) => l.key === level) || BA_LEVELS[0];
  const metrics = BA_METRICS.filter((x) => x.levels.includes(level));
  const m = metrics.find((x) => x.key === metricKey) || metrics[0];
  const mLabel = m.label(level);

  const numAgg = useFindingAgg(m.qid, filters.window, filters.workspaceIds, filters.envs, BA_GROUP, "sum", m.num);
  const denAgg = useFindingAgg(m.den ? m.qid : null, filters.window, filters.workspaceIds, filters.envs, BA_GROUP, "sum", m.den || null);
  const ready = isReady(numAgg) && (!m.den || isReady(denAgg));
  const notRun = numAgg.phase === "ready" && numAgg.outcome === "not_assessed";
  const net = m.kind === "money" && isReady(numAgg) ? 1 - numOrZero(numAgg.data.discount_pct) : 1;
  const fmt = (v: number | null) => fmtKind(m.kind, v);

  // Every calendar day of the window, so a day with no usage counts as zero in an average.
  const days = React.useMemo(() => {
    const filled = fillDays([], meta && meta.as_of_date, filters.window, meta && meta.snapshot_days).map((d) => d.day);
    if (filled.length || !isReady(numAgg)) return filled;
    return [...new Set(numAgg.data.groups.map((g) => String(g.key[2]).slice(0, 10)))].sort();
  }, [meta, filters.window, numAgg]);
  // Default split: the last 7 days are "after", or half the window when it is shorter.
  const defaultFix = days.length > 1 ? days[days.length > 14 ? days.length - 7 : Math.floor(days.length / 2)] : null;
  const fix = fixDay && days.length > 1 && fixDay > days[0] && fixDay <= days[days.length - 1] ? fixDay : defaultFix;
  const nBefore = fix ? days.filter((d) => d < fix).length : 0;
  const nAfter = days.length - nBefore;
  // The two periods as dates, so every figure says which days it averages.
  const beforeSpan = nBefore ? `${fmtDayShort(days[0])} - ${fmtDayShort(days[nBefore - 1])}` : "";
  const afterSpan = nAfter && fix ? `${fmtDayShort(fix)} - ${fmtDayShort(days[days.length - 1])}` : "";

  const rows = React.useMemo((): BaRow[] | null => {
    if (!ready || !fix || !nBefore || !nAfter) return null;
    const nums = aggMaps(numAgg, days, net, level);
    const dens = m.den ? aggMaps(denAgg, days, 1, level) : null;
    return [...nums.entries()].map(([key, num]): BaRow => {
      const den = (dens && dens.get(key)) || new Map<string, number>();
      const sn = sides(num, fix), sd = sides(den, fix);
      const before = m.den ? (sd.b > 0 ? sn.b / sd.b : null) : sn.b / nBefore;
      const after = m.den ? (sd.a > 0 ? sn.a / sd.a : null) : sn.a / nAfter;
      return { key, name: baName(level, key, lv.plural), num, den, b: { num: sn.b, den: sd.b }, a: { num: sn.a, den: sd.a }, ...move(before, after) };
    }).sort((x, y) => Number(x.change == null) - Number(y.change == null)
      || Math.abs(y.change || 0) - Math.abs(x.change || 0) || x.name.localeCompare(y.name));
  }, [ready, numAgg, denAgg, m, level, lv.plural, fix, nBefore, nAfter, days, net]);

  // A day with usage but no list price sums as $0; name the resources that have one.
  const unpriced = React.useMemo(() => {
    const out = new Set<string>();
    if (m.kind === "money" && isReady(numAgg)) {
      numAgg.data.groups.forEach((g) => { if (g.key[0] === level && numOrZero(g.null_rows) > 0) out.add(String(g.key[1] == null ? "account" : g.key[1])); });
    }
    return out;
  }, [m.kind, numAgg, level]);
  const filtered = rows ? rows.filter((r) => dirFilter === "all" || dirOf(m.kind, r) === (dirFilter === "down" ? -1 : 1)) : null;
  const sel = filtered && filtered.length ? filtered.find((r) => r.key === picked) || filtered[0] : null;
  const shown = filtered ? (showAll ? filtered : filtered.slice(0, BA_SHOWN)) : null;
  const hasPooled = !!rows && rows.some((r) => r.key.endsWith(":other"));
  const down = rows ? rows.filter((r) => dirOf(m.kind, r) < 0).length : 0;
  const up = rows ? rows.filter((r) => dirOf(m.kind, r) > 0).length : 0;
  // The level as a whole: summed per-day averages, or one ratio over every resource's runs.
  const total = (after: boolean) => {
    if (!rows) return null;
    const s = rows.reduce((acc, r) => ({ num: acc.num + (after ? r.a : r.b).num, den: acc.den + (after ? r.a : r.b).den }), { num: 0, den: 0 });
    return m.den ? (s.den > 0 ? s.num / s.den : null) : s.num / (after ? nAfter : nBefore);
  };
  const unitWord = m.den ? "" : " a day";
  const isSpend = m.kind === "money" && !m.den;

  const verdict = notRun
    ? `${m.qid === COST_QID ? "Daily cost per resource isn't" : "Daily query, job-run and pipeline figures aren't"} in this export: re-run the export.`
    : rows && fix
      ? `${lv.label}s, ${isSpend ? "spend" : midSentence(mLabel)}: ${fmt(total(false))}${unitWord} in ${beforeSpan}, ${fmt(total(true))}${unitWord} in ${afterSpan}. ${fmtInt(down)} went down, ${fmtInt(up)} went up.`
      : null;
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);

  const downloadCsv = () => {
    if (!filtered || !fix) return;
    const r4 = (v: number | null) => (v == null ? "" : Math.round(v * 10000) / 10000);
    const lines = [[lv.label, `${mLabel} ${days[0]} to ${days[nBefore - 1]}`, `${mLabel} ${fix} to ${days[days.length - 1]}`, "Change", "Change %"].map(csvField).join(",")];
    filtered.forEach((r) => {
      lines.push([r.name, r4(r.before), r4(r.after), r4(r.change), r.pct == null ? "" : Math.round(r.pct * 10) / 10].map(csvField).join(","));
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
    a.download = `before-after-${level}-${m.key}-${fix}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const controls = (
    <div className="ba-controls">
      <div className="ws-actions">
        {BA_LEVELS.map((l) => (
          <button key={l.key} type="button" className={l.key === level ? "on" : ""} onClick={() => { setLevel(l.key); setPicked(null); setShowAll(false); }}>{l.label}</button>
        ))}
      </div>
      <div className="ws-actions">
        {metrics.map((x) => (
          <button key={x.key} type="button" className={x.key === m.key ? "on" : ""} onClick={() => setMetricKey(x.key)}>{x.label(level)}</button>
        ))}
      </div>
      {days.length > 1 && fix && (
        <label className="ba-date">
          <span>After starts on</span>
          <input type="date" value={fix} min={days[1]} max={days[days.length - 1]} onChange={(e) => setFixDay(e.target.value || null)} />
        </label>
      )}
    </div>
  );
  const basis = m.qid === COST_QID ? (m.kind === "money" && isReady(numAgg) ? estLabel(numAgg.data.discount_pct) : m.kind === "dbu" ? "net DBUs" : "billing")
    : level === "job" ? "finished job runs" : level === "pipeline" ? "finished pipeline updates" : "SQL warehouse statements";

  if (!ready || !rows || !fix) {
    const pending = !isReady(numAgg) ? numAgg : denAgg;
    return (
      <div className="cost-ba">
        <Card title="Before and after a fix">
          {controls}
          {ready && days.length < 2 ? <div className="muted">The window has under two days: pick a longer one.</div> : <ChartNote state={pending} label={m.qid} />}
        </Card>
      </div>
    );
  }

  const perDay = (r: BaRow, d: string) => {
    const v = r.num.get(d) || 0;
    if (!m.den) return v;
    const n = r.den.get(d) || 0;
    return n > 0 ? v / n : 0;
  };
  const series = sel ? [
    { name: `Before: ${beforeSpan}`, values: days.map((d) => (d < fix ? perDay(sel, d) : 0)), color: "var(--c-other)" },
    { name: `After: ${afterSpan}`, values: days.map((d) => (d >= fix ? perDay(sel, d) : 0)), color: "var(--c1)" },
  ] : null;
  const denWord = m.den === "billed_hours" ? "hour running" : m.den === "task_runs" ? "task run" : RUN_WORD[level] || "day";
  const activity = m.qid === PERF_QID ? RUNS_WORD[level].toLowerCase() : "usage";
  const fewest = Math.min(nBefore, nAfter);

  return (
    <div className="cost-ba">
      <Card title="Before and after a fix" right={<span className="muted">{basis}</span>}>
        {controls}
        <div className="metric-note">
          {m.den
            ? `Before: ${beforeSpan} (${fmtInt(nBefore)} days). After: ${afterSpan} (${fmtInt(nAfter)} days). Average per ${denWord}.`
            : `Before: ${beforeSpan} (${fmtInt(nBefore)} days). After: ${afterSpan} (${fmtInt(nAfter)} days). Average per day; a day with none counts as zero.`}
        </div>
        {fewest < BA_FEW_DAYS && <CaveatNote>{`Only ${fmtInt(fewest)} day${fewest === 1 ? "" : "s"} on one side: one unusual day moves its average a lot. A longer window gives a steadier comparison.`}</CaveatNote>}
      </Card>

      {sel && series && (
        <Card title={sel.name} right={<span className={`mono ${toneOf(m.kind, sel, m.neutral)}`}>{changeText(m.kind, sel)}</span>}>
          <div className="metric-note">{`${isSpend ? "Spend" : mLabel}: ${fmt(sel.before)}${unitWord} in ${beforeSpan}, ${fmt(sel.after)}${unitWord} in ${afterSpan}.`}</div>
          <Columns
            days={days}
            series={series}
            markers={[{ day: fix, label: "After starts" }]}
            valueFmt={(v) => (m.kind === "money" ? fmtMoneyShort(v) : fmtKind(m.kind, v))}
            unitLabel={m.kind === "money" && !m.den ? "$" : m.den ? `per ${denWord}` : midSentence(mLabel)}
            dollarAxis={m.kind === "money"}
            note={m.kind === "seconds" ? `Seconds per ${denWord}, by day.` : null}
            ariaLabel={`${sel.name} by day`}
          />
        </Card>
      )}

      {sel && <WhatChanged level={level} rowKey={sel.key} name={sel.name} fix={fix} days={days} nBefore={nBefore} nAfter={nAfter} filters={filters} />}

      <Card title={`${lv.label}s by change`} right={(
        <span className="ba-table-head">
          <span className="muted">{`${fmtInt(rows.length)} with ${activity}`}</span>
          <button type="button" className="csv-download-btn" onClick={downloadCsv}>Download CSV</button>
        </span>
      )}>
        <div className="ws-actions">
          {BA_DIRS.map((d) => (
            <button key={d.key} type="button" className={d.key === dirFilter ? "on" : ""} onClick={() => { setDirFilter(d.key); setPicked(null); }}>
              {d.key === "all" ? d.label : `${d.label} · ${fmtInt(d.key === "down" ? down : up)}`}
            </button>
          ))}
        </div>
        {filtered && filtered.length ? (
          <div className="data-table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>{lv.label}</th>
                  <th className="num">{m.den ? "Before" : "Before, per day"}<div className="ba-span">{beforeSpan}</div></th>
                  <th className="num">{m.den ? "After" : "After, per day"}<div className="ba-span">{afterSpan}</div></th>
                  <th className="num">Change</th>
                </tr>
              </thead>
              <tbody>
                {(shown || []).map((r) => (
                  <tr key={r.key} className={`ba-row${sel && r.key === sel.key ? " on" : ""}`} onClick={() => setPicked(r.key)}>
                    <td><button type="button" className="ba-pick" onClick={() => setPicked(r.key)}>{r.name}{unpriced.has(r.key) ? " *" : ""}</button></td>
                    <td className="num mono">{fmt(r.before)}</td>
                    <td className="num mono">{fmt(r.after)}</td>
                    <td className={`num mono ${toneOf(m.kind, r, m.neutral)}`}>{changeText(m.kind, r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="muted">{rows.length ? `None ${dirFilter === "down" ? "went down" : "went up"}.` : `No ${lv.plural} with ${activity} in this window.`}</div>}
        {filtered && filtered.length > BA_SHOWN && (
          <button type="button" className="ov-link-btn" onClick={() => setShowAll(!showAll)}>
            {showAll ? `Show the ${BA_SHOWN} largest moves` : `Show all ${fmtInt(filtered.length)}`}
          </button>
        )}
        {unpriced.size > 0 && <CaveatNote>{`${fmtInt(unpriced.size)} ${unpriced.size === 1 ? lv.label.toLowerCase() : lv.plural} had days with no list price, counted as $0 (marked *). Switch to DBUs to compare them.`}</CaveatNote>}
        {hasPooled && <div className="metric-note">{`The 100 ${lv.plural} with the most ${m.qid === PERF_QID ? "run time" : "spend"} in the window have their own rows; the rest are one "Other" row per workspace.`}</div>}
      </Card>
    </div>
  );
}

AreaContent.register("cost", "before_after", CostBeforeAfterContent);
