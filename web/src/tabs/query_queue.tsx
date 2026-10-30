// Queries > Capacity: where and when SQL warehouse queries wait for a slot (perf_queue_by_hour),
// as a weekday-by-hour grid and a warehouse-by-day grid.
import React from "react";

import { fmtDayShort, fmtInt, fmtPct } from "../format";
import { useNames } from "../components/names";
import { numOrZero, useFindingAgg, warehouseName } from "../components/hooks";
import { Card } from "../components/primitives";
import { ChartNote } from "../components/charts";
import { HeatGrid } from "../components/design_parts";
import { finishedDays } from "./job_state";
import type { DimMaps, Filters, Meta } from "../types";

const QID = "perf_queue_by_hour";
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
// Queued share of a cell's queries, in percent: under 1, 1-3, 3-8, 8-15, over 15.
const EDGES = [1, 3, 8, 15];
const WAREHOUSE_ROWS = 10;

const okAgg = (s: { phase: string; outcome?: string | null; data?: unknown }) => s.phase === "ready" && s.outcome === "ok_rows" && !!s.data;
// Monday-first weekday of a YYYY-MM-DD day.
const weekdayOf = (day: string) => (new Date(`${day.slice(0, 10)}T00:00:00Z`).getUTCDay() + 6) % 7;
const pctFmt = (v: number) => (v < 1 && v > 0 ? "<1%" : fmtPct(v, 0));

function sums(state: { data?: { groups: { key: unknown[]; value: unknown }[] } | null }, keyOf: (k: unknown[]) => string): Map<string, number> {
  const m = new Map<string, number>();
  (state.data ? state.data.groups : []).forEach((g) => {
    const k = keyOf(g.key);
    m.set(k, (m.get(k) || 0) + numOrZero(g.value));
  });
  return m;
}

export function QueueHeatCards({ filters, meta, dims, only }: { filters: Filters; meta: Meta | null; dims: DimMaps | null; only?: "week" }) {
  useNames();
  const args = [filters.window, filters.workspaceIds, filters.envs] as const;
  const runsByHour = useFindingAgg(QID, ...args, ["usage_date", "usage_hour"], "sum", "runs");
  const queuedByHour = useFindingAgg(QID, ...args, ["usage_date", "usage_hour"], "sum", "queued_runs");
  const whQid = only === "week" ? null : QID;
  const runsByWh = useFindingAgg(whQid, ...args, ["warehouse_key", "usage_date"], "sum", "runs");
  const queuedByWh = useFindingAgg(whQid, ...args, ["warehouse_key", "usage_date"], "sum", "queued_runs");
  const days = React.useMemo(() => finishedDays(meta, filters.window,
    okAgg(runsByWh) ? runsByWh.data!.groups.map((g) => String(g.key[1])) : []), [meta, filters.window, runsByWh]);

  const week = React.useMemo(() => {
    if (!okAgg(runsByHour) || !okAgg(queuedByHour)) return null;
    const cellKey = (k: unknown[]) => `${weekdayOf(String(k[0]))}|${Number(k[1])}`;
    const runs = sums(runsByHour, cellKey);
    const queued = sums(queuedByHour, cellKey);
    const share = (wd: number, h: number) => {
      const r = runs.get(`${wd}|${h}`) || 0;
      return r > 0 ? ((queued.get(`${wd}|${h}`) || 0) / r) * 100 : null;
    };
    const dayTotal = (wd: number) => {
      let r = 0, q = 0;
      for (let h = 0; h < 24; h += 1) { r += runs.get(`${wd}|${h}`) || 0; q += queued.get(`${wd}|${h}`) || 0; }
      return { r, q };
    };
    let peak: { wd: number; h: number; v: number; q: number } | null = null;
    let totalQ = 0, totalR = 0;
    for (let wd = 0; wd < 7; wd += 1) {
      for (let h = 0; h < 24; h += 1) {
        const v = share(wd, h);
        const q = queued.get(`${wd}|${h}`) || 0;
        totalQ += q; totalR += runs.get(`${wd}|${h}`) || 0;
        if (v != null && q >= 3 && (!peak || v > peak.v)) peak = { wd, h, v, q };
      }
    }
    return { share, dayTotal, peak, totalQ, totalR };
  }, [runsByHour, queuedByHour]);

  const whGrid = React.useMemo(() => {
    if (!okAgg(runsByWh) || !okAgg(queuedByWh)) return null;
    const cellKey = (k: unknown[]) => `${String(k[0])}|${String(k[1]).slice(0, 10)}`;
    const runs = sums(runsByWh, cellKey);
    const queued = sums(queuedByWh, cellKey);
    const perWh = new Map<string, { r: number; q: number }>();
    runs.forEach((r, k) => {
      const wh = k.split("|")[0];
      const cur = perWh.get(wh) || { r: 0, q: 0 };
      perWh.set(wh, { r: cur.r + r, q: cur.q + (queued.get(k) || 0) });
    });
    const ranked = [...perWh.entries()].filter(([, v]) => v.q > 0).sort((a, b) => b[1].q - a[1].q || b[1].r - a[1].r);
    return { runs, queued, ranked, rows: ranked.slice(0, WAREHOUSE_ROWS) };
  }, [runsByWh, queuedByWh]);

  const hours = Array.from({ length: 24 }, (_, h) => ({ key: String(h), label: String(h).padStart(2, "0") }));
  const whName = (key: string) => {
    const [, id] = key.split(":");
    return warehouseName(dims, id);
  };

  return (
    <>
      <Card title="Queued share, weekday by hour (UTC)" right={<span className="muted">share of queries that waited for a slot · all warehouses</span>}>
        {!week ? <ChartNote state={okAgg(runsByHour) ? queuedByHour : runsByHour} label={QID} />
          : !week.totalR ? <div className="muted">No SQL warehouse query in this window.</div>
          : (
            <>
              <HeatGrid
                rows={WEEKDAYS.map((w, i) => {
                  const t = week.dayTotal(i);
                  return { key: w, label: w, title: w, right: t.r ? pctFmt((t.q / t.r) * 100) : "-" };
                })}
                cols={hours}
                cell={(ri, ci) => week.share(ri, ci)}
                edges={EDGES}
                fmt={pctFmt}
                rightHead="queued"
                emptyLabel="no queries"
                labelEvery={3}
                ariaLabel="Queued share by weekday and hour"
              />
              <div className="metric-note" style={{ marginTop: 8 }}>
                {week.totalQ
                  ? `${fmtInt(week.totalQ)} of ${fmtInt(week.totalR)} queries waited for a slot (${pctFmt((week.totalQ / week.totalR) * 100)}).${week.peak ? ` Worst: ${WEEKDAYS[week.peak.wd]} ${String(week.peak.h).padStart(2, "0")}:00, ${pctFmt(week.peak.v)} of that hour's queries.` : ""} A block of dark cells at the same hours is a schedule clash, not a size problem.`
                  : "No query waited for a slot in this window."}
              </div>
            </>
          )}
      </Card>
      {!only && <Card title="Queued share, warehouse by day" right={<span className="muted">{whGrid ? `${fmtInt(whGrid.rows.length)} of ${fmtInt(whGrid.ranked.length)} warehouses that queued, most queued first` : ""}</span>}>
        {!whGrid ? <ChartNote state={okAgg(runsByWh) ? queuedByWh : runsByWh} label={QID} />
          : !whGrid.rows.length ? <div className="muted">No warehouse queued in this window.</div>
          : (
            <HeatGrid
              rows={whGrid.rows.map(([key, v]) => ({ key, label: whName(key), title: whName(key), right: `${pctFmt((v.q / Math.max(v.r, 1)) * 100)} of ${fmtInt(v.r)}` }))}
              cols={days.map((d) => ({ key: d, label: fmtDayShort(d) }))}
              cell={(ri, ci) => {
                const k = `${whGrid.rows[ri][0]}|${days[ci]}`;
                const r = whGrid.runs.get(k) || 0;
                return r > 0 ? ((whGrid.queued.get(k) || 0) / r) * 100 : null;
              }}
              edges={EDGES}
              fmt={pctFmt}
              rightHead="queued"
              emptyLabel="no queries"
              compact
              ariaLabel="Queued share by warehouse and day"
            />
          )}
      </Card>}
    </>
  );
}
