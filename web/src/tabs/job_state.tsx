// One state line per job or pipeline over the window (lakeflow_daily_state): ran fine, failed,
// ran slow, skipped, or no run. Used on Jobs > Failures and the data engineer's Overview.
import React from "react";

import { fmtDayShort, fmtDuration, fmtInt } from "../format";
import { csvField } from "../components/findings_table";
import { DbxLink, JobFocus, jobNameFallback, resolveName, useNames } from "../components/names";
import { fillDays, numOrZero, pipelineName, useFindingAgg, useFindingData } from "../components/hooks";
import { Card } from "../components/primitives";
import { ChartNote } from "../components/charts";
import { StateBands } from "../components/design_parts";
import { DAY_KEY, nextDay } from "../components/grain";
import type { DayState, StateRow } from "../components/design_parts";
import type { DimMaps, Filters, Meta, Row } from "../types";

const QID = "lakeflow_daily_state";
// 12 lines fit a screen; rows arrive worst first, so this many days of rows covers them.
const LINES = 12;

// The window's days, without today (the check leaves the current day out). With no export date
// (a locally built database), the days the rows themselves span, gaps filled.
export function finishedDays(meta: Meta | null, windowDays: number, rowDays?: string[]): string[] {
  const filled = fillDays([], meta && meta.as_of_date, windowDays, meta && meta.snapshot_days).map((d) => d.day);
  if (filled.length) {
    const todayIn = !!(meta && meta.direct_export && meta.direct_export.includes_today);
    return todayIn ? filled.slice(0, -1) : filled;
  }
  const seen = [...new Set((rowDays || []).map((d) => String(d).slice(0, 10)).filter((d) => DAY_KEY.test(d)))].sort();
  if (!seen.length) return [];
  const out: string[] = [];
  for (let d = seen[0]; d <= seen[seen.length - 1] && out.length < 400; d = nextDay(d)) out.push(d);
  return out;
}

function resourceName(dims: DimMaps | null, r: Row): string {
  return r.resource_type === "pipeline"
    ? pipelineName(dims, r.workspace_id, r.resource_id)
    : jobNameFallback(dims, r.workspace_id, r.resource_id);
}

export function JobStateCard({ filters, meta, dims, title }: { filters: Filters; meta: Meta | null; dims: DimMaps | null; title?: string }) {
  useNames();
  const state = useFindingData(QID, filters.window, filters.workspaceIds, filters.envs, LINES * filters.window + 50);
  // How many jobs and pipelines ran, and how many of them failed on at least one day.
  const countAgg = useFindingAgg(QID, filters.window, filters.workspaceIds, filters.envs, ["resource_type", "failed_days"], "count_distinct", "resource_key");

  const rows = state.phase === "ready" && state.outcome === "ok_rows" && state.data ? state.data.rows : null;
  const days = React.useMemo(() => finishedDays(meta, filters.window, (rows || []).map((r) => r.usage_date)), [meta, filters.window, rows]);
  const lines = React.useMemo((): { rows: StateRow[]; shown: number } | null => {
    if (!rows) return null;
    const byKey = new Map<string, Row[]>();
    rows.forEach((r) => {
      const k = `${r.resource_type}|${r.resource_key}`;
      if (!byKey.has(k) && byKey.size >= LINES) return;
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k)!.push(r);
    });
    const dayIdx = new Map(days.map((d, i) => [d, i]));
    const out: StateRow[] = [];
    byKey.forEach((list, k) => {
      const first = list[0];
      const states: DayState[] = days.map(() => "none");
      let runs = 0, failed = 0;
      list.forEach((r) => {
        const i = dayIdx.get(String(r.usage_date).slice(0, 10));
        if (i !== undefined) states[i] = (r.day_state as DayState) || "ok";
        runs += numOrZero(r.runs);
        failed += numOrZero(r.failed);
      });
      // Only the worst resources' lines: one with nothing wrong is not worth a line here.
      if (!numOrZero(first.failed_days) && !numOrZero(first.slow_days)) return;
      const name = resourceName(dims, first);
      const kind = first.resource_type === "pipeline" ? "pipeline" : "job";
      out.push({
        key: k,
        label: <>{name} <span className="muted">{kind}</span></>,
        title: `${name} (${first.resource_id})`,
        states,
        cells: [
          fmtInt(runs),
          failed ? `${fmtInt(failed)} (${Math.round((failed / Math.max(runs, 1)) * 100)}%)` : "0",
          first.median_run_s != null ? fmtDuration(first.median_run_s) : "-",
          `${fmtInt(first.failed_days)} / ${fmtInt(first.slow_days)}`,
        ],
      });
    });
    return { rows: out, shown: out.length };
  }, [rows, days, dims]);

  const counts = React.useMemo(() => {
    if (countAgg.phase !== "ready" || countAgg.outcome !== "ok_rows" || !countAgg.data) return null;
    let total = 0, failing = 0;
    countAgg.data.groups.forEach((g) => {
      const n = numOrZero(g.value);
      total += n;
      if (numOrZero(g.key[1]) > 0) failing += n;
    });
    return { total, failing };
  }, [countAgg]);

  const byKey = React.useMemo(() => new Map((rows || []).map((r) => [`${r.resource_type}|${r.resource_key}`, r])), [rows]);
  const open = (k: string) => {
    const r = byKey.get(k);
    if (r && r.resource_type === "job") JobFocus.open(r.workspace_id, r.resource_id);
  };

  const right = counts ? `${fmtInt(counts.failing)} of ${fmtInt(counts.total)} jobs and pipelines failed on some day` : "";
  return (
    <Card title={title || `Jobs and pipelines · state over ${fmtInt(days.length)} days`} right={<span className="muted">{right}</span>}>
      {!lines ? <ChartNote state={state} label={QID} />
        : !lines.rows.length ? <div className="muted">No job or pipeline failed or ran slow in this window.</div>
        : (
          <StateBands
            days={days}
            rows={lines.rows}
            heads={["Runs", "Failed", "Median run", "Failed / slow days"]}
            dayFmt={fmtDayShort}
            onRow={open}
            more={`One line per job or pipeline, worst first: most failed days, then slow days (an average run 1.5× its median and a minute longer). Click a job for its runs.${counts && counts.failing + 0 > lines.shown ? ` Showing ${fmtInt(lines.shown)} of ${fmtInt(counts.failing)} that failed.` : ""}`}
          />
        )}
    </Card>
  );
}

// Every job and pipeline by its average run time over the window: the longest first.
const RUN_SHOWN = [20, 50];
type RunSort = "avg" | "total" | "longest" | "runs" | "failed";
const RUN_SORTS: { key: RunSort; label: string }[] = [
  { key: "avg", label: "Average run" },
  { key: "total", label: "Total run time" },
  { key: "longest", label: "Longest run" },
  { key: "runs", label: "Runs" },
  { key: "failed", label: "Failed" },
];
interface RunRow { k: string; kind: string; ws: string; id: string; name: string; runs: number; total: number; avg: number | null; longest: number | null; failed: number }
const runVal = (r: RunRow, key: RunSort) => numOrZero(key === "avg" ? r.avg : key === "longest" ? r.longest : r[key]);
const isRange = (sort: RunSort) => sort === "avg" || sort === "longest";

// One bar per row for the sorted measure; average and longest share one bar, longest in the lighter part.
function RunBar({ r, sort, top }: { r: RunRow; sort: RunSort; top: number }) {
  const pct = (v: number | null) => `${top > 0 ? Math.min(100, (numOrZero(v) / top) * 100) : 0}%`;
  return (
    <div className="rt-bar" aria-hidden="true">
      {isRange(sort) && <span className="rt-max" style={{ width: pct(r.longest) }} />}
      <span className={`rt-main${sort === "failed" ? " fail" : ""}`} style={{ width: pct(isRange(sort) ? r.avg : runVal(r, sort)) }} />
    </div>
  );
}

export function RunTimeCard({ filters, dims }: { filters: Filters; dims: DimMaps | null }) {
  useNames();
  const [limit, setLimit] = React.useState(RUN_SHOWN[0]);
  const [sort, setSort] = React.useState<RunSort>("avg");
  const group = ["resource_type", "resource_key"];
  const a = [filters.window, filters.workspaceIds, filters.envs, group] as const;
  const totalAgg = useFindingAgg(QID, ...a, "sum", "total_run_s");
  const timedAgg = useFindingAgg(QID, ...a, "sum", "timed_runs");
  const failedAgg = useFindingAgg(QID, ...a, "sum", "failed");
  const maxAgg = useFindingAgg(QID, ...a, "max", "max_run_s");
  const states = [totalAgg, timedAgg, failedAgg, maxAgg];
  const ok = states.every((s) => s.phase === "ready" && s.outcome === "ok_rows" && s.data);

  const rows = React.useMemo((): RunRow[] | null => {
    if (!ok) return null;
    const val = (s: typeof totalAgg) => new Map(s.data!.groups.map((g) => [`${g.key[0]}|${g.key[1]}`, numOrZero(g.value)]));
    const timed = val(timedAgg), failed = val(failedAgg), longest = val(maxAgg);
    const list = totalAgg.data!.groups.map((g) => {
      const k = `${g.key[0]}|${g.key[1]}`;
      const [ws, ...rest] = String(g.key[1]).split(":");
      const id = rest.join(":");
      const n = timed.get(k) || 0;
      const r = { resource_type: g.key[0], workspace_id: ws, resource_id: id } as Row;
      return { k, kind: String(g.key[0]), ws, id, name: resourceName(dims, r), runs: n, total: numOrZero(g.value), avg: n ? numOrZero(g.value) / n : null, longest: longest.get(k) || null, failed: failed.get(k) || 0 };
    }).filter((r) => r.runs > 0);
    // Two jobs with one name (often one per workspace) say which workspace each is in.
    const named = new Map<string, number>();
    list.forEach((r) => named.set(r.name, (named.get(r.name) || 0) + 1));
    return list.map((r) => (named.get(r.name)! > 1 ? { ...r, name: `${r.name} · ${resolveName("workspace", r.ws, r.ws) || r.ws}` } : r));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ok, totalAgg, timedAgg, failedAgg, maxAgg, dims]);
  const sorted = React.useMemo(() => (rows
    ? [...rows].sort((x, y) => runVal(y, sort) - runVal(x, sort) || numOrZero(y.avg) - numOrZero(x.avg) || y.total - x.total)
    : null), [rows, sort]);

  const download = () => {
    if (!sorted) return;
    const lines = [["Name", "Kind", "Workspace", "Id", "Runs", "Average run (s)", "Longest run (s)", "Total run time (s)", "Failed"].map(csvField).join(",")];
    sorted.forEach((r) => lines.push([r.name, r.kind, r.ws, r.id, r.runs, r.avg == null ? "" : Math.round(r.avg), r.longest == null ? "" : Math.round(r.longest), Math.round(r.total), r.failed].map(csvField).join(",")));
    const el = document.createElement("a");
    el.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
    el.download = `run-time-${filters.window}d.csv`;
    el.click();
    URL.revokeObjectURL(el.href);
  };

  const shown = sorted ? sorted.slice(0, limit) : null;
  const sortLabel = RUN_SORTS.find((s) => s.key === sort)!.label;
  const top = shown && shown.length ? Math.max(...shown.map((r) => (isRange(sort) ? numOrZero(r.longest) : runVal(r, sort)))) : 0;
  const head = (key: RunSort, text: string) => <th className={`num${sort === key ? " rt-sorted" : ""}`}>{text}</th>;
  return (
    <Card title="Jobs and pipelines by run time" right={sorted ? <span className="muted">{`${fmtInt(Math.min(limit, sorted.length))} of ${fmtInt(sorted.length)}, by ${sortLabel.toLowerCase()}`}</span> : null}>
      {!shown ? <ChartNote state={states.find((s) => s.phase !== "ready" || s.outcome !== "ok_rows") || totalAgg} label={QID} />
        : !shown.length ? <div className="muted">No job or pipeline finished a run in this window.</div>
        : (
          <>
            <div className="ws-actions">
              {RUN_SORTS.map((s) => (
                <button key={s.key} type="button" className={s.key === sort ? "on" : ""} onClick={() => setSort(s.key)}>{s.label}</button>
              ))}
            </div>
            <div className="data-table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Job or pipeline</th>
                    <th className="rt-col">{isRange(sort) ? "Average, then longest" : sortLabel}</th>
                    {head("runs", "Runs")}{head("avg", "Average run")}{head("longest", "Longest run")}{head("total", "Total run time")}{head("failed", "Failed")}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <tr key={r.k}>
                      <td>
                        {r.kind === "job"
                          ? <button type="button" className="link-btn" title={`${r.name} (${r.id})`} onClick={() => JobFocus.open(r.ws, r.id)}>{r.name}</button>
                          : <span title={`${r.name} (${r.id})`}>{r.name}</span>}
                        <span className="muted">{` ${r.kind}`}</span>
                        {r.kind === "job" && <span className="rt-link"><DbxLink kind="job" workspaceId={r.ws} id={r.id} label="Runs ↗" /></span>}
                      </td>
                      <td className="rt-col"><RunBar r={r} sort={sort} top={top} /></td>
                      <td className="num mono">{fmtInt(r.runs)}</td>
                      <td className="num mono">{r.avg == null ? "-" : fmtDuration(r.avg)}</td>
                      <td className="num mono">{r.longest == null ? "-" : fmtDuration(r.longest)}</td>
                      <td className="num mono">{fmtDuration(r.total)}</td>
                      <td className={`num mono${r.failed ? " rt-fail" : ""}`}>{fmtInt(r.failed)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="trend-by-head">
              <span className="muted">Skipped and cancelled runs are left out of the times. Click a job for its runs here, or Runs ↗ to open them in Databricks.</span>
              <span>
                {sorted!.length > RUN_SHOWN[0] && (
                  <button type="button" className="ov-link-btn" onClick={() => setLimit(limit === RUN_SHOWN[0] ? RUN_SHOWN[1] : RUN_SHOWN[0])}>
                    {limit === RUN_SHOWN[0] ? `Show ${fmtInt(Math.min(RUN_SHOWN[1], sorted!.length))}` : `Show ${RUN_SHOWN[0]}`}
                  </button>
                )}
                <button type="button" className="csv-download-btn" onClick={download}>Download all (CSV)</button>
              </span>
            </div>
          </>
        )}
    </Card>
  );
}
