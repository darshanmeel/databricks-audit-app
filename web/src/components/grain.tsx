// Day or month for a dated chart or table: a day view shows the latest 45 days, a month view the latest 18 months.
import React from "react";
import { fmtDayShort } from "../format";
import { MultiLine } from "./charts";

export const DAY_ROWS_MAX = 45;
export const MONTH_ROWS_MAX = 18;
export type Grain = "day" | "month";

export function GrainSwitch({ value, onChange }: { value: Grain; onChange: (g: Grain) => void }) {
  return (
    <div className="ws-actions grain-switch" role="group" aria-label="Day or month">
      <button type="button" className={value === "day" ? "on" : ""} onClick={() => onChange("day")}>Day</button>
      <button type="button" className={value === "month" ? "on" : ""} onClick={() => onChange("month")}>Month</button>
    </div>
  );
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "2026-09-01" -> "Sep 2026".
export function fmtMonth(key: unknown): string {
  const m = /^(\d{4})-(\d{2})/.exec(String(key || ""));
  return m ? `${MONTH_NAMES[Number(m[2]) - 1]} ${m[1]}` : String(key || "");
}

export function monthOf(day: string): string {
  return `${day.slice(0, 7)}-01`;
}

// Day keys (YYYY-MM-DD, oldest first) summed into month keys (YYYY-MM-01), for each value series.
export function rollUpMonths(days: string[], series: number[][]): { months: string[]; series: number[][] } {
  const months: string[] = [];
  const at = new Map<string, number>();
  days.forEach((d) => { const k = monthOf(d); if (!at.has(k)) { at.set(k, months.length); months.push(k); } });
  const out = series.map(() => months.map(() => 0));
  days.forEach((d, di) => {
    const mi = at.get(monthOf(d))!;
    series.forEach((s, si) => { out[si][mi] += Number(s[di]) || 0; });
  });
  return { months, series: out };
}

// Month totals (any order, YYYY-MM-DD keys) as a gap-free run oldest first: a month with no row is a real $0.
export function fillMonths(entries: { key: string; value: number }[]): { key: string; value: number }[] {
  const sums = new Map<string, number>();
  entries.forEach((e) => { const k = monthOf(e.key); sums.set(k, (sums.get(k) || 0) + e.value); });
  const keys = Array.from(sums.keys()).sort();
  if (!keys.length) return [];
  const out: { key: string; value: number }[] = [];
  let [y, m] = keys[0].split("-").map(Number);
  const last = keys[keys.length - 1];
  for (let k = keys[0]; k <= last; ) {
    out.push({ key: k, value: sums.get(k) || 0 });
    m += 1; if (m > 12) { m = 1; y += 1; }
    k = `${y}-${String(m).padStart(2, "0")}-01`;
  }
  return out;
}

export const DAY_KEY = /^\d{4}-\d{2}-\d{2}$/;

export function nextDay(d: string): string {
  const t = new Date(`${d}T00:00:00Z`);
  t.setUTCDate(t.getUTCDate() + 1);
  return t.toISOString().slice(0, 10);
}

function nextMonth(m: string): string {
  let [y, mo] = m.split("-").map(Number);
  mo += 1; if (mo > 12) { mo = 1; y += 1; }
  return `${y}-${String(mo).padStart(2, "0")}-01`;
}

// Every month from the first to the last key, so a month with nothing is a real 0.
export function monthRun(keys: string[]): string[] {
  const sorted = [...new Set(keys.filter((k) => DAY_KEY.test(k)))].sort();
  if (!sorted.length) return [];
  const out: string[] = [];
  for (let k = sorted[0]; k <= sorted[sorted.length - 1] && out.length < 600; k = nextMonth(k)) out.push(k);
  return out;
}

// Months a day list does not fully cover: the one still running, and (from) the first when it starts mid-month.
export function partialMonths(days: string[], checkFirst: boolean): { open: string | null; cut: string | null } {
  const last = days.length ? days[days.length - 1] : null;
  return {
    open: last && monthOf(nextDay(last)) === monthOf(last) ? monthOf(last) : null,
    cut: checkFirst && days.length && days[0].slice(8) !== "01" ? monthOf(days[0]) : null,
  };
}

// The latest n of a list kept oldest first.
export function latest<T>(list: T[], n: number): T[] {
  return list.length > n ? list.slice(list.length - n) : list;
}

// A daily line chart (count series) at either grain: the latest 45 days, or the days summed into months.
export function GrainLines({ grain, days, series }: { grain: Grain; days: string[]; series: { label: string; color: string; data: number[] }[] }) {
  if (grain === "day") {
    const from = Math.max(0, days.length - DAY_ROWS_MAX);
    return <MultiLine series={series.map((s) => ({ ...s, data: s.data.slice(from) }))} xLabels={days.slice(from).map(fmtDayShort)} />;
  }
  const r = rollUpMonths(days, series.map((s) => s.data));
  const from = Math.max(0, r.months.length - MONTH_ROWS_MAX);
  if (r.months.length - from < 2) {
    return <div className="chart-note ok"><div className="chart-note-text">{`Only ${fmtMonth(r.months[r.months.length - 1])} in this window: pick a longer window for a monthly trend.`}</div></div>;
  }
  return <MultiLine series={series.map((s, i) => ({ ...s, data: r.series[i].slice(from) }))} xLabels={r.months.slice(from).map(fmtMonth)} />;
}
