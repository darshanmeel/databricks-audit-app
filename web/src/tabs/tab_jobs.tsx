// Jobs area, 5 sub-tabs (section 6): Failures, Slow & queued,
// Hygiene, Compute fit, Pipelines. Registers each sub-tab's content via AreaContent.register
// (primitives.tsx); the run panel drawer itself lives in job_panel.tsx (opened by JobFocus.open,
// names.tsx, from any <Ref kind="job"> or a job-table row here).
//
// CjKpi/CjKpis/LOADING_VALUE/readyRows/findingFor come from tab_compute.tsx, loaded just before
// this file (index.html) -- one small headline-card shape shared by both Compute and Jobs rather
// than a second copy. jobNameFallback (names.tsx) is this file's own job-label call everywhere
// below instead of hooks.ts's plain jobName -- same lookup, plus one fallback: a workspace_id:
// job_id key with no name tries job_id alone next, only when that id is unique across every
// workspace (never borrowed when two workspaces reuse the same numeric id).

import React from "react";

import { fmtChange, fmtDate, fmtDuration, fmtInt, fmtMoney, fmtPct } from "../format";
import { JobFocus, jobNameFallback, resolveName } from "../components/names";
import { groupSum, numOrZero, pipelineName, sumBy, useFindingAgg, useFindingData } from "../components/hooks";
import { AreaContent, Card, StatusPill, bandPillKind } from "../components/primitives";
import { enumLabel } from "../components/labels";
import { ChartNote, Donut, HBar, HBarList, shortId } from "../components/charts";
import { BandRows } from "../components/band_rows";
import { ScalingHintChips, pressureColor, pressureRowLabel, pressureSegments } from "../components/pressure";
import { CjKpi, CjKpis, findingFor, flaggedCountLabel, kpiPending, readyRows } from "./tab_compute";
import { slowdownSentence, slowdownSplit } from "../components/overview_tile";
import { ShareBar } from "../components/design_parts";
import { JobStateCard, RunTimeCard } from "./job_state";
import type { Row } from "../types";

// A cancel reads "CANCELED" or "CANCELLED" depending on which system wrote the row (both spellings
// turn up on real exports) -- matched loosely so a cancel is never miscounted as a failure because
// of one letter.
export function isCancelledCode(code: any) { return /CANCEL/i.test(String(code || "")); }

// "24 Sep, 22:08" -- fixed, UTC reading of a run's own start timestamp (also used by job_panel.tsx,
// loaded after this file).
export function shortRunWhen(iso: any) {
  if (!iso) return "-";
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(iso));
  if (!m) return String(iso);
  const mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][Number(m[2]) - 1];
  return `${Number(m[3])} ${mon}, ${m[4]}:${m[5]}`;
}

// Job identity is (workspace_id, job_id); jobNameFallback alone collides when two different jobs
// (almost always in different workspaces) share a name. dupNameCounts counts each rendered name
// across one list's own rows; jobLabel appends the workspace only when that name is not already
// unique within THAT list (never a global count, so one list's collision never touches another).
function dupNameCounts(dims: any, rows: any) {
  const counts = new Map();
  (rows || []).forEach((r: any) => {
    const n = jobNameFallback(dims, r.workspace_id, r.job_id);
    counts.set(n, (counts.get(n) || 0) + 1);
  });
  return counts;
}
function jobLabel(dims: any, counts: any, r: any) {
  const n = jobNameFallback(dims, r.workspace_id, r.job_id);
  return counts.get(n) > 1 ? `${n} (${resolveName("workspace", r.workspace_id, r.workspace_id)})` : n;
}

// ─────────────────────────────────────────── Failures ─────────────────────────────────────────
// The per-job table below deliberately does NOT build a run-by-run strip or a multi-reason count
// from lakeflow_job_run_cost fetched account-wide: that finding's own ORDER BY is cost-descending
// (its inventory purpose, not "most recent"), so a server-side row cap on a busy account silently
// drops the cheap, fast-failing runs a broken job actually produces -- exactly the rows this table
// most needs. lakeflow_job_reliability's own per-job fields (latest_run_start/latest_result_state/
// latest_termination_code/last_n_summary) are already complete at this grain (one row per job, no
// cap), so the table reads its "latest run" and "reason" from there; the FULL per-run history is
// fetched job-scoped (small, uncapped in practice) in job_panel.tsx's own drawer instead.

const TERMINATION_BUCKET_META: Record<string, any> = {
  success: { label: "Succeeded", color: "var(--st-ok)", hint: "" },
  execution: { label: "Execution errors", color: "var(--st-crit)", hint: "code or data" },
  driver: { label: "Driver / cluster errors", color: "var(--div-up)", hint: "cluster driver lost" },
  cancelled: { label: "Cancelled by user", color: "var(--text-faint)", hint: "not a failure" },
  timedout: { label: "Timed out", color: "var(--st-warn)", hint: "hit its time limit" },
};
const DRIVER_CODES = new Set(["DRIVER_ERROR", "CLUSTER_ERROR", "DRIVER_UNREACHABLE", "DRIVER_UNRESPONSIVE", "CLOUD_FAILURE"]);
function terminationBucket(code: any) {
  if (code === "SUCCESS") return "success";
  if (isCancelledCode(code)) return "cancelled";
  if (code === "TIMED_OUT" || code === "TIMEDOUT") return "timedout";
  if (DRIVER_CODES.has(code)) return "driver";
  return "execution";
}

// Failures only: this check counts attempts with a termination code, so a run total here would
// disagree with the reliability cards, which count each run once.
function RunsByResult({ taxonomyRows, windowDays }: LooseProps) {
  if (!taxonomyRows) return null;
  const totals = new Map();
  taxonomyRows.forEach((r: any) => {
    const b = terminationBucket(r.termination_code);
    totals.set(b, (totals.get(b) || 0) + numOrZero(r.run_rows));
  });
  const order = ["execution", "driver", "timedout"].filter((b) => totals.get(b) > 0);
  const grand = order.reduce((s, b) => s + totals.get(b), 0);
  if (!grand) return <span className="muted">No failed runs in this window.</span>;
  // The same failures by their own termination code: which one cause dominates.
  const byCode = new Map<string, number>();
  taxonomyRows.forEach((r: any) => {
    if (["success", "cancelled"].includes(terminationBucket(r.termination_code))) return;
    const label = enumLabel(r.termination_code) || String(r.termination_code || "no code recorded");
    byCode.set(label, (byCode.get(label) || 0) + numOrZero(r.run_rows));
  });
  const segments = order.map((b) => ({
    label: TERMINATION_BUCKET_META[b].label, value: totals.get(b), color: TERMINATION_BUCKET_META[b].color,
  }));
  return (
    <div>
      <div className="cj-card-subtitle" style={{ marginBottom: 6 }}>
        <span>{`${fmtInt(grand)} failed run attempts · last ${windowDays}d · a retried run counts once per attempt`}</span>
      </div>
      <ShareBar parts={[...byCode].map(([label, value]) => ({ label, value }))} fmt={(v) => `${fmtInt(v)} runs`} noun="termination codes" />
      <HBar segments={segments} height={14} />
      <div className="cj-results-legend">
        {order.map((b) => (
          <div className="cj-results-legend-item" key={b}>
            <span className="sw" style={{ background: TERMINATION_BUCKET_META[b].color }}></span>
            <span>{TERMINATION_BUCKET_META[b].label}</span>
            <span className="n">{fmtInt(totals.get(b))}</span>
            {TERMINATION_BUCKET_META[b].hint && <span className="hint">{TERMINATION_BUCKET_META[b].hint}</span>}
          </div>
        ))}
      </div>
      <p className="cj-chart-takeaway">Execution errors come from the job's own code or data; driver errors mean the cluster's driver crashed or was lost. Cancelled runs aren't counted as failures.</p>
    </div>
  );
}

export const STATUS_RANK: Record<string, number> = { CRITICAL: 0, WARN: 1, NOT_ASSESSED: 2, OK: 3 };
const SLOWDOWN_CAUSE: Record<string, string> = { queue_or_setup: "queue and cluster start grew", run_time: "run time grew" };

function JobRow({ job, dims, active, showId }: LooseProps) {
  const latestLabel = job.latest_result_state ? (enumLabel(job.latest_result_state) || job.latest_result_state) : null;
  const reasonLabel = job.latest_termination_code ? (enumLabel(job.latest_termination_code) || `Unknown code ${job.latest_termination_code}`) : null;
  const failedNoCode = !job.latest_termination_code && job.latest_result_state && job.latest_result_state !== "SUCCEEDED";
  return (
    <button type="button" className={`cj-job-row ${active ? "active" : ""}`} onClick={() => JobFocus.open(job.workspace_id, job.job_id)}>
      <span><StatusPill kind={bandPillKind(job.status === "NOT_ASSESSED" ? "NOT_ASSESSED" : job.status)} /></span>
      <div>
        <div className="cj-job-name">
          {jobNameFallback(dims, job.workspace_id, job.job_id)}
          {showId && <span className="muted"> ({shortId(job.job_id)})</span>}
        </div>
        <div className="cj-job-meta">
          {resolveName("workspace", job.workspace_id, job.workspace_id)}
          {job.status === "CRITICAL" ? ` · broken: last ${fmtInt(job.consecutive_failures)} runs failed` : job.status === "WARN" ? ` · flaky: ${job.failure_rate_pct}% of runs failed` : ""}
        </div>
      </div>
      <span className="cj-job-meta">{latestLabel ? `${shortRunWhen(job.latest_run_start)}: ${latestLabel}` : "-"}</span>
      <span className="cj-job-failed">{`${fmtInt(job.failed_runs)} of ${fmtInt(job.runs_total)}`}</span>
      <span className="cj-job-reason">{reasonLabel && reasonLabel !== latestLabel ? reasonLabel : failedNoCode ? <span className="muted">No termination code recorded</span> : (job.last_n_summary || "-")}</span>
      <span className="cj-job-usd">{job.wastedUsd != null ? fmtMoney(job.wastedUsd, 0) : "-"}</span>
      <span className="cj-job-chevron">{"›"}</span>
    </button>
  );
}

function JobsFailuresContent({ findings, filters, meta, dims, maxCat, goTo, onVerdict }: LooseProps) {
  const reliabilityState = useFindingData("lakeflow_job_reliability", filters.window, filters.workspaceIds, filters.envs);
  // The two rules in one line, with the thresholds this export actually used.
  const relParams = reliabilityState.data && reliabilityState.data.header && reliabilityState.data.header.params;
  const pv = (k: any, d: any) => (relParams && relParams[k] && relParams[k].value != null ? relParams[k].value : d);
  const reliabilityRule = reliabilityState.phase === "ready"
    ? `Broken: the last ${pv("crit_consecutive_failures", 3)}+ runs failed in a row. Flaky: ${pv("warn_failure_rate_pct", 20)}%+ of runs failed (${pv("min_runs", 5)}+ runs).`
    : null;
  const wastedState = useFindingData("lakeflow_failed_jobs_wasted_dbus", filters.window, filters.workspaceIds, filters.envs);
  // One row per job and termination code: summed server-side, so a large account is never capped.
  const taxonomyState = useFindingAgg("lakeflow_termination_taxonomy", filters.window, filters.workspaceIds, filters.envs, ["termination_code"], "sum", "run_rows");

  // The headline rate and flagged-failure $ are Api.aggregate sums over every job in scope, never
  // a row page capped at ui.max_rows -- the same undercount-on-large-accounts fix already applied
  // to AI Gateway.
  const runsAgg = useFindingAgg("lakeflow_job_reliability", filters.window, filters.workspaceIds, filters.envs, [], "sum", "runs");
  const failedRunsAgg = useFindingAgg("lakeflow_job_reliability", filters.window, filters.workspaceIds, filters.envs, [], "sum", "failed_runs");
  const wastedSumAgg = useFindingAgg("lakeflow_failed_jobs_wasted_dbus", filters.window, filters.workspaceIds, filters.envs, [], "sum", "est_wasted_usd_list", { statuses: ["CRITICAL", "WARN"] });
  const wastedCountAgg = useFindingAgg("lakeflow_failed_jobs_wasted_dbus", filters.window, filters.workspaceIds, filters.envs, [], "count", null, { statuses: ["CRITICAL", "WARN"] });

  const relRows = readyRows(reliabilityState);
  const wastedRows = readyRows(wastedState);
  const taxonomyRows = taxonomyState.phase === "ready" && String(taxonomyState.outcome).startsWith("ok_")
    ? ((taxonomyState.data && taxonomyState.data.groups) || []).map((g) => ({ termination_code: g.key[0], run_rows: g.value }))
    : null;

  // The per-row $ column keeps every job's own possible waste, whatever its status -- the KPI/
  // verdict totals below instead sum only the FLAGGED (CRITICAL/WARN) rows, the same set
  // lakeflow_failed_jobs_wasted_dbus itself bands and the Waste tile counts (C15).
  const wastedByJob = React.useMemo(() => {
    const m = new Map();
    const disc = wastedState.data ? numOrZero(wastedState.data.discount_pct) : 0;
    (wastedRows || []).forEach((r) => {
      if (r.est_wasted_usd_list != null) m.set(`${r.workspace_id}:${r.job_id}`, numOrZero(r.est_wasted_usd_list) * (1 - disc));
    });
    return m;
  }, [wastedRows, wastedState.data]);

  const jobs = relRows ? relRows.map((r): Row => {
    const key = `${r.workspace_id}:${r.job_id}`;
    return { ...r, runs_total: r.runs, wastedUsd: wastedByJob.has(key) ? wastedByJob.get(key) : null };
  }) : null;

  const withFailures = jobs ? jobs.filter((j) => j.failed_runs > 0)
    .sort((a, b) => (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9) || b.failed_runs - a.failed_runs) : null;
  const noFailures = jobs ? jobs.length - withFailures!.length : 0;
  // Two different jobs sharing one scheduled name (a template reused across workspaces, or two
  // jobs simply named the same) would otherwise show as identical rows -- the muted id tells them
  // apart only when a name is not already unique on this list.
  const dupNames = React.useMemo(() => {
    if (!withFailures) return null;
    const counts = new Map();
    withFailures.forEach((j) => {
      const n = jobNameFallback(dims, j.workspace_id, j.job_id);
      counts.set(n, (counts.get(n) || 0) + 1);
    });
    return counts;
  }, [withFailures, dims]);

  const brokenNow = jobs ? jobs.filter((j) => j.status === "CRITICAL") : null;
  const flaky = jobs ? jobs.filter((j) => j.status === "WARN") : null;
  const worstBroken = brokenNow && brokenNow.length ? [...brokenNow].sort((a, b) => b.consecutive_failures - a.consecutive_failures)[0] : null;
  const worstFlaky = flaky && flaky.length ? [...flaky].sort((a, b) => b.failure_rate_pct - a.failure_rate_pct)[0] : null;

  // C15: the cost tile and the verdict's job count both come from the FLAGGED rows of
  // lakeflow_failed_jobs_wasted_dbus (its own already-floored status), never the wider
  // "has any failed run" set withFailures counts -- same label, same scope.
  const wastedFlagged = React.useMemo(() => {
    const disc = wastedState.data ? numOrZero(wastedState.data.discount_pct) : 0;
    return (wastedRows || []).filter((r) => r.status === "CRITICAL" || r.status === "WARN")
      .map((r): Row => ({ ...r, wastedUsd: r.est_wasted_usd_list != null ? numOrZero(r.est_wasted_usd_list) * (1 - disc) : 0 }));
  }, [wastedRows, wastedState.data]);
  const worstCost = wastedFlagged.length ? [...wastedFlagged].sort((a, b) => b.wastedUsd - a.wastedUsd)[0] : null;

  // C15: the headline rate is SUM(failed_runs) / SUM(runs) over the SAME reliability rows the
  // table below shows, not a run-level taxonomy count that also includes SKIPPED runs and rows
  // with no termination code -- the two used to disagree (87% vs 20%). Api.aggregate sums, never
  // the ui.max_rows-capped row page, so a large account is never undercounted.
  const totalRuns = runsAgg.phase === "ready" && runsAgg.outcome === "ok_rows" ? numOrZero(runsAgg.data.total_value) : 0;
  const totalFailedRuns = failedRunsAgg.phase === "ready" && failedRunsAgg.outcome === "ok_rows" ? numOrZero(failedRunsAgg.data.total_value) : 0;
  const failureRatePct = totalRuns ? (totalFailedRuns / totalRuns) * 100 : 0;
  const costTotal = wastedSumAgg.phase === "ready" && wastedSumAgg.outcome === "ok_rows"
    ? numOrZero(wastedSumAgg.data.total_value) * (1 - numOrZero(wastedSumAgg.data.discount_pct)) : 0;
  const wastedFlaggedCount = wastedCountAgg.phase === "ready" && wastedCountAgg.outcome === "ok_rows" ? numOrZero(wastedCountAgg.data.total_value) : 0;

  React.useEffect(() => {
    if (!onVerdict || !jobs) return;
    const costBit = `${fmtMoney(costTotal, 0)} lost on flagged failures of ${fmtInt(wastedFlaggedCount)} job${wastedFlaggedCount === 1 ? "" : "s"}`;
    if (worstBroken) {
      onVerdict(`${fmtInt(brokenNow!.length)} job${brokenNow!.length === 1 ? " is" : "s are"} broken right now; worst: ${jobNameFallback(dims, worstBroken.workspace_id, worstBroken.job_id)}, failed its last ${fmtInt(worstBroken.consecutive_failures)} runs. Across all jobs, ${fmtInt(totalFailedRuns)} of ${fmtInt(totalRuns)} runs failed (${fmtPct(failureRatePct, 1)}), and ${costBit}.`);
    } else {
      onVerdict(`No job is broken right now. Across all jobs, ${fmtInt(totalFailedRuns)} of ${fmtInt(totalRuns)} runs failed (${fmtPct(failureRatePct, 1)}), and ${costBit}.`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs, worstBroken, totalFailedRuns, totalRuns, costTotal, wastedFlaggedCount, dims]);

  if (reliabilityState.phase !== "loading" && reliabilityState.outcome && reliabilityState.outcome !== "ok_rows") {
    return <div className="card"><ChartNote state={reliabilityState} label="lakeflow_job_reliability" /></div>;
  }

  return (
    <div>
      <CjKpis>
        <CjKpi
          label="Broken now"
          tone={brokenNow && brokenNow.length ? "crit" : ""}
          value={jobs ? `${fmtInt(brokenNow!.length)} job${brokenNow!.length === 1 ? "" : "s"}` : kpiPending(reliabilityState)}
          facts={jobs ? (worstBroken ? [
            { label: "Worst", value: jobNameFallback(dims, worstBroken.workspace_id, worstBroken.job_id), detail: `${fmtInt(worstBroken.consecutive_failures)} runs failed` },
          ] : [
            { label: "Broken", value: "None right now", tone: "ok" },
          ]) : null}
          link={worstBroken ? "See its runs →" : null}
          onLink={() => worstBroken && JobFocus.open(worstBroken.workspace_id, worstBroken.job_id)}
        />
        <CjKpi
          label="Flaky jobs"
          tone={flaky && flaky.length ? "warn" : ""}
          value={jobs ? fmtInt(flaky!.length) : kpiPending(reliabilityState)}
          facts={worstFlaky ? [
            { label: "Worst", value: jobNameFallback(dims, worstFlaky.workspace_id, worstFlaky.job_id), detail: `${worstFlaky.failure_rate_pct}% failed` },
            { label: "Rule", value: `${pv("warn_failure_rate_pct", 20)}%+ of runs failed` },
          ] : null}
        />
        <CjKpi
          label="Cost of failed runs"
          tone={costTotal > 0 ? "warn" : ""}
          value={jobs ? fmtMoney(costTotal, 0) : kpiPending(reliabilityState)}
          facts={worstCost ? [
            { label: "Jobs", value: fmtInt(wastedFlaggedCount) },
            { label: "Worst", value: jobNameFallback(dims, worstCost.workspace_id, worstCost.job_id), detail: fmtMoney(worstCost.wastedUsd, 0) },
          ] : null}
          link="Counted in Waste & savings →"
          onLink={() => goTo({ tab: "waste" })}
        />
        <CjKpi
          label="Failure rate"
          value={jobs ? fmtPct(failureRatePct, 1) : kpiPending(reliabilityState)}
          facts={jobs ? [
            { label: "Failed", value: `${fmtInt(totalFailedRuns)} of ${fmtInt(totalRuns)}` },
          ] : null}
        />
      </CjKpis>
      {reliabilityRule && <div className="cj-chart-takeaway">{reliabilityRule}</div>}

      <JobStateCard filters={filters} meta={meta} dims={dims} />

      <Card title="Jobs with failed runs" right={<span className="muted">{jobs ? `${fmtInt(withFailures!.length)} of ${fmtInt(jobs.length)} jobs · click a job for its runs` : ""}</span>}>
        {jobs ? (
          <div className="cj-job-table">
            <div className="cj-job-head">
              <span>Status</span><span>Job</span><span>Latest run</span>
              <span style={{ textAlign: "right" }}>Failed</span><span>Reason</span>
              <span style={{ textAlign: "right" }}>$ lost</span><span></span>
            </div>
            {withFailures!.map((j) => (
              <JobRow
                key={`${j.workspace_id}:${j.job_id}`}
                job={j}
                dims={dims}
                showId={dupNames!.get(jobNameFallback(dims, j.workspace_id, j.job_id)) > 1}
              />
            ))}
            {noFailures > 0 && (
              <div className="cj-flag-row" style={{ gridTemplateColumns: "1fr" }}>
                <span className="muted">{`${fmtInt(noFailures)} job${noFailures === 1 ? "" : "s"} had no failed runs in this window.`}</span>
              </div>
            )}
          </div>
        ) : <ChartNote state={reliabilityState} label="lakeflow_job_reliability" />}
      </Card>

      <Card title="Failed runs by reason">
        {taxonomyRows ? <RunsByResult taxonomyRows={taxonomyRows} windowDays={filters.window} /> : <ChartNote state={taxonomyState} label="lakeflow_termination_taxonomy" />}
      </Card>
    </div>
  );
}
AreaContent.register("jobs", "failures", JobsFailuresContent);

// ─────────────────────────────────────────── Slow & queued ────────────────────────────────────

// Jobs by how long their cluster takes to start (p95 of their task runs' setup time).
const START_BANDS: { label: string; test: (s: number) => boolean }[] = [
  { label: "Under 2 min", test: (s) => s < 120 },
  { label: "2–5 min", test: (s) => s >= 120 && s < 300 },
  { label: "5–10 min", test: (s) => s >= 300 && s < 600 },
  { label: "Over 10 min", test: (s) => s >= 600 },
];

function StartTimeBandsCard({ rows, state }: { rows: Row[] | null; state: any }) {
  if (!rows) return <Card title="Jobs by cluster start time (p95)"><ChartNote state={state} label="lakeflow_phase_cold_start" /></Card>;
  const measured = rows.filter((r) => r.setup_s_p95 != null);
  if (!measured.length) return null;
  const bands = START_BANDS.map((b) => {
    const list = measured.filter((r) => b.test(numOrZero(r.setup_s_p95)));
    const hours = list.reduce((s, r) => s + numOrZero(r.setup_s_total), 0) / 3600;
    const runs = list.reduce((s, r) => s + numOrZero(r.runs), 0);
    return {
      label: b.label, total: hours,
      sub: list.length ? `${fmtInt(list.length)} job${list.length === 1 ? "" : "s"} · ${fmtInt(runs)} runs` : "No jobs",
      text: list.length ? <React.Fragment><b className="mono">{`${fmtInt(Math.round(hours))} h`}</b>{" of task start time"}</React.Fragment> : <span className="muted">none</span>,
    };
  });
  return (
    <Card title="Jobs by cluster start time (p95)" right={<span className="muted">{`${fmtInt(measured.length)} jobs measured`}</span>}>
      <div className="cj-card-subtitle"><span>Start time is the setup before a task's code runs: the cluster starting and its libraries. Successful task runs only.</span></div>
      <BandRows bands={bands} />
    </Card>
  );
}

function JobsSlowContent({ findings, filters, meta, dims, maxCat, onVerdict }: LooseProps) {
  const durState = useFindingData("lakeflow_job_duration_regression", filters.window, filters.workspaceIds, filters.envs);
  const queueState = useFindingData("lakeflow_job_queue_time", filters.window, filters.workspaceIds, filters.envs);
  const coldState = useFindingData("lakeflow_phase_cold_start", filters.window, filters.workspaceIds, filters.envs);
  const failStartState = useFindingData("lakeflow_failed_cluster_starts", filters.window, filters.workspaceIds, filters.envs);
  const split = slowdownSplit(meta, filters.window);

  const durRows = readyRows(durState);
  const queueRows = readyRows(queueState);
  const coldRows = readyRows(coldState);
  // Serverless or classic per job, from its own billing, so a seconds-long start reads right.
  // Grouped by workspace_id too -- job_id alone is not unique across workspaces.
  const computeAgg = useFindingAgg("cost_by_job", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "job_id", "is_serverless"], "sum", "usd_list");
  const serverlessByJob = new Map();
  if (computeAgg.phase === "ready" && computeAgg.outcome === "ok_rows") {
    (computeAgg.data.groups || []).forEach((g) => {
      const key = `${g.key[0]}:${g.key[1]}`;
      const prev = serverlessByJob.get(key);
      serverlessByJob.set(key, prev === undefined ? !!g.key[2] : prev && !!g.key[2]);
    });
  }
  const computeWord = (workspaceId: any, jobId: any) => {
    const key = `${workspaceId}:${jobId}`;
    return serverlessByJob.has(key) ? (serverlessByJob.get(key) ? "serverless" : "classic") : null;
  };
  // Two jobs of the same name in different workspaces would otherwise read as one identical bar --
  // counted per list (queue vs. cold start have different rows), never globally.
  const queueNameCounts = React.useMemo(() => dupNameCounts(dims, queueRows), [queueRows, dims]);
  const coldNameCounts = React.useMemo(() => dupNameCounts(dims, coldRows), [coldRows, dims]);
  const timedName = (r: any, counts: any) => {
    const word = computeWord(r.workspace_id, r.job_id);
    return `${jobLabel(dims, counts, r)}${word ? ` (${word})` : ""}`;
  };
  // Queue time is recorded only for single-task jobs (no task-level queue column exists at all);
  // cold start (setup_s_p95) is now measured from job_task_run_timeline instead, real on
  // multi-task jobs too -- see lakeflow_phase_cold_start's own header.
  const measured = (rows: any, col: any) => (rows ? rows.filter((r: any) => r[col] != null).length : 0);
  const measuredFact = (rows: any, col: any) => (rows ? { label: "Measured", value: `${fmtInt(measured(rows, col))} of ${fmtInt(rows.length)} jobs`, detail: col === "queue_s_p95" ? "single-task jobs only" : null } : null);

  const durFlagged = durRows ? durRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const durNameCounts = React.useMemo(() => dupNameCounts(dims, durFlagged), [durFlagged, dims]);
  // A "worst" job is only a real finding once its own p95 is above zero -- a queue/cold-start
  // fetch that came back all-zero or all-null still has a row[0] after the sort, but naming it
  // reads as a flagged job next to a value of "-".
  const worstQueueAll = queueRows && queueRows.length ? [...queueRows].sort((a, b) => numOrZero(b.queue_s_p95) - numOrZero(a.queue_s_p95))[0] : null;
  const worstQueue = worstQueueAll && numOrZero(worstQueueAll.queue_s_p95) > 0 ? worstQueueAll : null;
  // Measured, but no row's queue_s_p95 is above zero -- say so instead of a bare "-". Nothing
  // measured is not "no wait": it stays a dash with the reason.
  const queueMeasured = measured(queueRows, "queue_s_p95");
  const noQueueWait = !!queueRows && queueMeasured > 0 && !worstQueue;
  const worstColdAll = coldRows && coldRows.length ? [...coldRows].sort((a, b) => numOrZero(b.setup_s_p95) - numOrZero(a.setup_s_p95))[0] : null;
  const worstCold = worstColdAll && numOrZero(worstColdAll.setup_s_p95) > 0 ? worstColdAll : null;

  React.useEffect(() => {
    if (!onVerdict || !durRows) return;
    onVerdict(`${slowdownSentence(durFlagged!.length, split)}.${durFlagged!.length && worstQueue ? ` Worst queue wait: ${jobLabel(dims, queueNameCounts, worstQueue)}, ${fmtDuration(worstQueue.queue_s_p95)} at the 95th percentile.` : ""}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [durRows, durFlagged, worstQueue]);

  return (
    <div>
      <CjKpis>
        <CjKpi
          label="Slower than usual"
          tone={durFlagged && durFlagged.length ? "warn" : ""}
          value={durRows ? fmtInt(durFlagged!.length) : kpiPending(durState)}
          facts={durRows ? [
            { label: "Judged", value: `${fmtInt(durRows.filter((r) => !r.not_assessed_reason).length)} of ${fmtInt(durRows.length)} jobs`, detail: `last ${split.recent} days' median 1.5x the ${split.baseline} days before flags` },
            { label: "Too few runs", value: fmtInt(durRows.filter((r) => r.not_assessed_reason).length), detail: "under 3 successful runs recently or before" },
            { label: "Too short", value: fmtInt(durRows.filter((r) => r.below_floor).length), detail: durState.data && durState.data.floor ? `slower, but under ${durState.data.floor.label}` : null },
          ] : null}
        />
        <CjKpi
          label="Worst queue wait (p95)"
          value={worstQueue ? fmtDuration(worstQueue.queue_s_p95) : (noQueueWait ? "No queue wait" : (queueRows ? "-" : kpiPending(queueState)))}
          facts={[worstQueue ? { label: "Worst", value: timedName(worstQueue, queueNameCounts) } : null, measuredFact(queueRows, "queue_s_p95")].filter(Boolean)}
        />
        <CjKpi
          label="Worst cold start (p95)"
          value={worstCold ? fmtDuration(worstCold.setup_s_p95) : (coldRows ? "-" : kpiPending(coldState))}
          facts={[worstCold ? { label: "Worst", value: timedName(worstCold, coldNameCounts) } : null, measuredFact(coldRows, "setup_s_p95")].filter(Boolean)}
        />
        <CjKpi label="Jobs measured" value={durRows ? fmtInt(durRows.length) : kpiPending(durState)} facts={[{ label: "Window", value: `${filters.window}d` }]} />
      </CjKpis>
      <div className="cj-charts-2">
        <Card title="Jobs waiting longest on compute (p95 queue time)">
          {queueRows && queueMeasured === 0 ? (
            <div className="muted">Not measured: Databricks records queue time only for single-task jobs, and none ran this window.</div>
          ) : queueRows && !queueRows.some((r) => numOrZero(r.queue_s_p95) > 0) ? (
            <div className="muted">{`No queue wait this window (${fmtInt(queueMeasured)} single-task jobs measured).`}</div>
          ) : queueRows ? (
            <HBarList
              items={[...queueRows].filter((r) => r.queue_s_p95 != null).sort((a, b) => b.queue_s_p95 - a.queue_s_p95).slice(0, maxCat || 8)
                .map((r) => ({ name: timedName(r, queueNameCounts), value: numOrZero(r.queue_s_p95) }))}
              valueFmt={(v) => fmtDuration(v)}
            />
          ) : <ChartNote state={queueState} label="lakeflow_job_queue_time" />}
        </Card>
        <Card title="Slowest cluster cold starts (p95)">
          {coldRows && <div className="metric-note">Successful task runs only. Task runs that failed while starting are listed under Failed cluster starts.</div>}
          {coldRows ? (
            <HBarList
              items={[...coldRows].filter((r) => r.setup_s_p95 != null).sort((a, b) => b.setup_s_p95 - a.setup_s_p95).slice(0, maxCat || 8)
                .map((r) => ({ name: timedName(r, coldNameCounts), value: numOrZero(r.setup_s_p95) }))}
              valueFmt={(v) => fmtDuration(v)}
            />
          ) : <ChartNote state={coldState} label="lakeflow_phase_cold_start" />}
        </Card>
      </div>
      <RunTimeCard filters={filters} dims={dims} />
      <StartTimeBandsCard rows={coldRows} state={coldState} />
      <FailedStartsCard state={failStartState} dims={dims} maxCat={maxCat} />
      {durFlagged && durFlagged.length > 0 && (
        <Card title="Slower than usual, by job">
          <HBarList
            items={[...durFlagged].sort((a, b) => b.slowdown_ratio - a.slowdown_ratio).slice(0, maxCat || 8)
              .map((r) => ({ name: `${jobLabel(dims, durNameCounts, r)} · ${SLOWDOWN_CAUSE[r.grew_most] || "no phase data"}`, value: r.slowdown_ratio, recentMin: r.recent_median_minutes, baselineMin: r.baseline_median_minutes }))}
            valueFmt={(v, it) => fmtChange(it.recentMin, it.baselineMin, false)}
          />
        </Card>
      )}
    </div>
  );
}
AreaContent.register("jobs", "slow", JobsSlowContent);

// Task runs that failed before their code ran, by termination code, and the jobs they hit most.
function FailedStartsCard({ state, dims, maxCat }: LooseProps) {
  const rows = readyRows(state);
  const empty = state.phase === "ready" && typeof state.outcome === "string" && state.outcome.startsWith("ok_empty");
  if (!rows && !empty) return <Card title="Failed cluster starts"><ChartNote state={state} label="lakeflow_failed_cluster_starts" /></Card>;
  if (!rows || !rows.length) {
    return <Card title="Failed cluster starts"><div className="muted">No task run failed before its code ran this window.</div></Card>;
  }
  const byCode = new Map<string, number>();
  const byJob = new Map<string, { row: Row; n: number; setup: number }>();
  let total = 0, setup = 0;
  rows.forEach((r) => {
    const n = numOrZero(r.failed_starts);
    total += n;
    setup += numOrZero(r.setup_s_total);
    byCode.set(r.termination_code, (byCode.get(r.termination_code) || 0) + n);
    const key = `${r.workspace_id}:${r.job_id}`;
    const j = byJob.get(key) || { row: r, n: 0, setup: 0 };
    j.n += n;
    j.setup += numOrZero(r.setup_s_total);
    byJob.set(key, j);
  });
  const nameCounts = dupNameCounts(dims, [...byJob.values()].map((j) => j.row));
  return (
    <Card title="Failed cluster starts" right={<span className="muted">{`${fmtInt(total)} task run${total === 1 ? "" : "s"} · ${fmtInt(byJob.size)} job${byJob.size === 1 ? "" : "s"}`}</span>}>
      <div className="metric-note">{`${fmtInt(total)} task run${total === 1 ? "" : "s"} failed before ${total === 1 ? "its" : "their"} code ran, after ${fmtDuration(setup)} of start-up in all.`}</div>
      <div className="cj-charts-2">
        <div>
          <div className="chart-card-title">By termination code</div>
          <HBarList
            items={[...byCode.entries()].sort((a, b) => b[1] - a[1]).slice(0, maxCat || 8).map(([code, n]) => ({ name: enumLabel(code) || code, value: n }))}
            valueFmt={(v) => fmtInt(v)}
          />
        </div>
        <div>
          <div className="chart-card-title">Jobs with the most failed starts</div>
          <HBarList
            items={[...byJob.values()].sort((a, b) => b.n - a.n || b.setup - a.setup).slice(0, maxCat || 8)
              .map((j) => ({ name: jobLabel(dims, nameCounts, j.row), value: j.n }))}
            valueFmt={(v) => fmtInt(v)}
          />
        </div>
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────── Hygiene ───────────────────────────────────────────

// CjFlaggedRow (tab_compute.tsx) puts a full [icon+word] StatusPill in a fixed 100px CSS-grid
// column; a long word ("Not assessed") is wider than that track, and a grid item overflows into
// the next column by default instead of being clipped -- the icon+word visually overlaps the job
// name. This flex row uses a compact (icon + count only, no word) pill instead, so nothing needs
// more room than the track gives it.
function HygieneFlagRow({ name, sub, status, reasons }: LooseProps) {
  return (
    <div className="cj-flag-row" style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
      <span style={{ flex: "0 0 auto" }}><StatusPill kind={bandPillKind(status)} compact /></span>
      <span className="cj-flag-name" style={{ flex: "0 1 auto" }}>{name}{sub && <span className="cj-flag-sub">{sub}</span>}</span>
      <span className="cj-flag-reasons" style={{ flex: "1 1 auto" }}>{reasons || "-"}</span>
    </div>
  );
}

function JobsHygieneContent({ findings, filters, dims, maxCat, onVerdict }: LooseProps) {
  const noTimeoutState = useFindingData("lakeflow_jobs_no_timeout", filters.window, filters.workspaceIds, filters.envs);
  const taskTimeoutState = useFindingData("lakeflow_job_tasks_no_timeout", filters.window, filters.workspaceIds, filters.envs);
  const healthState = useFindingData("lakeflow_health_rule_coverage", filters.window, filters.workspaceIds, filters.envs);
  const ownerState = useFindingData("lakeflow_job_ownership_orphans", filters.window, filters.workspaceIds, filters.envs);
  const retriesState = useFindingData("lakeflow_retries_repairs", filters.window, filters.workspaceIds, filters.envs);
  const staleState = useFindingData("lakeflow_stale_zombie_jobs", filters.window, filters.workspaceIds, filters.envs);

  const noTimeoutRows = readyRows(noTimeoutState);
  const taskTimeoutRows = readyRows(taskTimeoutState);
  const healthRows = readyRows(healthState);
  const ownerRows = readyRows(ownerState);
  const retriesRows = readyRows(retriesState);
  const staleRows = readyRows(staleState);

  // lakeflow_jobs_no_timeout / lakeflow_job_tasks_no_timeout / lakeflow_health_rule_coverage /
  // lakeflow_job_ownership_orphans are now one row per job (or task) so a tag filter can reach
  // them by the job's own tag -- rolled back up per workspace here so this page still shows the
  // same account-wide numbers it did when each check was one row per workspace.
  // Not-assessed rows have no recorded timeout yet, so they are counted apart, never as gaps.
  const noTimeout = (r: any) => (r.no_timeout && r.status !== "NOT_ASSESSED" ? 1 : 0);
  const noTimeoutTotal = noTimeoutRows ? noTimeoutRows.reduce((s, r) => s + noTimeout(r), 0) : null;
  const taskTimeoutTotal = taskTimeoutRows ? taskTimeoutRows.reduce((s, r) => s + noTimeout(r), 0) : null;
  const countWhere = (rows: any, fn: any) => (rows ? rows.filter(fn).length : 0);
  const timeoutFacts = (rows: any, noun: any) => [
    countWhere(rows, (r: any) => noTimeout(r) && r.below_floor) ? { label: "Under the floor", value: fmtInt(countWhere(rows, (r: any) => noTimeout(r) && r.below_floor)), detail: `${noun}, not flagged` } : null,
    countWhere(rows, (r: any) => r.status === "NOT_ASSESSED") ? { label: "Not assessed", value: fmtInt(countWhere(rows, (r: any) => r.status === "NOT_ASSESSED")), detail: "timeout not recorded yet" } : null,
  ].filter(Boolean);
  const ownerFlagged = (r: any) => (r.status === "CRITICAL" || r.status === "WARN" ? 1 : 0);
  const ownershipRisk = ownerRows ? ownerRows.reduce((s, r) => s + ownerFlagged(r), 0) : null;
  // Worst workspace by count, in place of a static one-line definition of the risk.
  const worstWorkspace = (rows: any, valFn: any) => {
    if (!rows) return null;
    const byWs = groupSum(rows.map((r: any) => ({ workspace_id: r.workspace_id, _v: valFn(r) })), (r) => r.workspace_id, "_v");
    const flagged = byWs.filter((e) => e.value > 0);
    return flagged.length ? [...flagged].sort((a, b) => b.value - a.value)[0] : null;
  };
  const noTimeoutWorst = worstWorkspace(noTimeoutRows, noTimeout);
  const taskTimeoutWorst = worstWorkspace(taskTimeoutRows, noTimeout);
  const ownerWorst = worstWorkspace(ownerRows, ownerFlagged);
  const timeoutNotAssessed = countWhere(noTimeoutRows, (r: any) => r.status === "NOT_ASSESSED");
  const healthPct = healthRows ? (() => {
    const judged = healthRows.filter((r) => r.health_rule_count != null);
    const withRule = judged.filter((r) => numOrZero(r.health_rule_count) > 0).length;
    return judged.length > 0 ? (withRule / judged.length) * 100 : null;
  })() : null;

  const retriesFlagged = retriesRows ? [...retriesRows].filter((r) => r.total_retries > 0).sort((a, b) => b.total_retries - a.total_retries).slice(0, maxCat || 8) : null;
  // Two jobs of the same name in different workspaces would otherwise read as one identical bar.
  const retriesNameCounts = React.useMemo(() => dupNameCounts(dims, retriesFlagged), [retriesFlagged, dims]);
  // Worst first: CRITICAL (over :crit_stale_days) before WARN (never ran, or over :stale_days) --
  // sorting by last_run_start alone put every never-ran job (a null date, so it sorts first as an
  // empty string) ahead of jobs that HAVE run but not in a very long time, the more urgent signal.
  const staleFlagged = staleRows ? [...staleRows].filter((r) => r.is_stale)
    .sort((a, b) => (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9) || String(a.last_run_start || "").localeCompare(String(b.last_run_start || "")))
    .slice(0, maxCat || 8) : null;

  React.useEffect(() => {
    if (!onVerdict || !noTimeoutRows) return;
    onVerdict(`${fmtInt(noTimeoutTotal)} jobs and ${fmtInt(taskTimeoutTotal)} tasks have no timeout set${timeoutNotAssessed ? ` (${fmtInt(timeoutNotAssessed)} more jobs not assessed)` : ""}; ${healthPct != null ? fmtPct(healthPct, 0) : "an unmeasured share"} of active jobs have a health rule.`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [noTimeoutRows, noTimeoutTotal, taskTimeoutTotal, healthPct, timeoutNotAssessed]);

  return (
    <div>
      <CjKpis>
        <CjKpi label="Jobs with no timeout" tone={numOrZero(noTimeoutTotal) > 0 ? "warn" : ""} value={noTimeoutRows ? fmtInt(noTimeoutTotal) : kpiPending(noTimeoutState)}
          facts={[...(noTimeoutWorst ? [{ label: "Worst", value: resolveName("workspace", noTimeoutWorst.name, noTimeoutWorst.name), detail: `${fmtInt(noTimeoutWorst.value)} jobs` }] : [{ label: "Risk", value: "None", tone: "ok" }]), ...timeoutFacts(noTimeoutRows, "jobs")]} />
        <CjKpi label="Tasks with no timeout" tone={numOrZero(taskTimeoutTotal) > 0 ? "warn" : ""} value={taskTimeoutRows ? fmtInt(taskTimeoutTotal) : kpiPending(taskTimeoutState)}
          facts={[...(taskTimeoutWorst ? [{ label: "Worst", value: resolveName("workspace", taskTimeoutWorst.name, taskTimeoutWorst.name), detail: `${fmtInt(taskTimeoutWorst.value)} tasks` }] : [{ label: "Risk", value: "None", tone: "ok" }]), ...timeoutFacts(taskTimeoutRows, "tasks")]} />
        <CjKpi label="Health-rule coverage" value={healthRows ? (healthPct != null ? fmtPct(healthPct, 0) : "not measured") : kpiPending(healthState)} facts={[{ label: "Metric", value: "Active jobs with a health rule" }]} />
        <CjKpi label="Ownership-risk jobs" tone={numOrZero(ownershipRisk) > 0 ? "warn" : ""} value={ownerRows ? fmtInt(ownershipRisk) : kpiPending(ownerState)}
          facts={ownerWorst ? [{ label: "Worst", value: resolveName("workspace", ownerWorst.name, ownerWorst.name), detail: `${fmtInt(ownerWorst.value)} jobs` }] : [{ label: "Risk", value: "None", tone: "ok" }]} />
      </CjKpis>
      <div className="cj-charts-2">
        <Card title="Jobs relying on retries or repairs">
          {retriesFlagged ? (retriesFlagged.length
            ? <HBarList items={retriesFlagged.map((r) => ({ name: jobLabel(dims, retriesNameCounts, r), value: r.total_retries }))} valueFmt={(v) => fmtInt(v)} />
            : <span className="muted">No job needed a retry or repair in this window.</span>
          ) : <ChartNote state={retriesState} label="lakeflow_retries_repairs" />}
        </Card>
        <Card title="Stale jobs with no recent run">
          {staleFlagged ? (staleFlagged.length
            ? staleFlagged.map((r) => (
              <HygieneFlagRow
                key={`${r.workspace_id}:${r.job_id}`}
                name={jobNameFallback(dims, r.workspace_id, r.job_id)}
                sub={resolveName("workspace", r.workspace_id, r.workspace_id)}
                status={r.status}
                reasons={r.last_run_start ? `last ran ${fmtDate(r.last_run_start)}` : "never recorded a run"}
              />
            ))
            : <span className="muted">No stale jobs found.</span>
          ) : <ChartNote state={staleState} label="lakeflow_stale_zombie_jobs" />}
        </Card>
      </div>
    </div>
  );
}
AreaContent.register("jobs", "hygiene", JobsHygieneContent);

// ─────────────────────────────────────────── Compute fit ──────────────────────────────────────

// "one node size down" -> "One node size down" -- suggested_action is already plain words (the
// SQL's own two levers), just sentence-cased for a table cell.
function sentenceCase(s: any) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

// "p90 / peak" in one cell; memory is judged on the peak, CPU on p90.
const p90Peak = (p90: any, peak: any) => (p90 == null ? "-" : `${fmtPct(p90, 0)} / ${peak != null ? fmtPct(peak, 0) : "-"}`);

// One flagged job: its cause, the numbers behind it, and the fix, instead of the check's reason sentence.
// "p90 87%" with the number in red at or over the check's limit.
function StackLine({ label, value, limit }: LooseProps) {
  if (value == null) return null;
  const over = limit != null && numOrZero(value) >= limit;
  return <span><span className="cj-stack-lbl">{label}</span><span className={over ? "cj-over" : ""}>{fmtPct(value, 0)}</span></span>;
}

function PressureJobRow({ row, dims, limits }: LooseProps) {
  return (
    <div className="cj-oversized-row cj-pressure-grid">
      <div>
        <button type="button" className="cj-oversized-name cj-name-link" onClick={() => JobFocus.open(row.workspace_id, row.job_id)}>{jobNameFallback(dims, row.workspace_id, row.job_id)}</button>
        <div className="cj-oversized-meta">{resolveName("workspace", row.workspace_id, row.workspace_id)}</div>
      </div>
      <div>
        <div className="cj-oversized-action">{pressureRowLabel(row)}</div>
        {row.at_ceiling && <div className="cj-oversized-meta">at its max workers</div>}
      </div>
      <div className="cj-stack">
        <StackLine label="p90" value={row.worker_mem_p90_pct} limit={limits.mem} />
        <StackLine label="peak" value={row.worker_mem_peak_pct} />
        <StackLine label="swap" value={row.worker_swap_p90_pct} limit={limits.swap} />
      </div>
      <div className="cj-stack">
        <StackLine label="p90" value={row.worker_cpu_p90_pct} />
        <StackLine label="median" value={row.worker_cpu_p50_pct} limit={limits.cpu} />
      </div>
      <div>
        <ScalingHintChips row={row} />
        {row.hottest_task_key && <div className="cj-oversized-meta">{`hottest task ${row.hottest_task_key}`}</div>}
      </div>
      <span className="cj-oversized-pct" style={{ textAlign: "right" }}>{row.task_hours_total != null ? `${fmtInt(Math.round(numOrZero(row.task_hours_total)))} h` : "-"}</span>
    </div>
  );
}

function OversizedJobRow({ row, dims }: LooseProps) {
  return (
    <div className="cj-oversized-row" key={`${row.workspace_id}:${row.job_id}`}>
      <div>
        <button type="button" className="cj-oversized-name cj-name-link" onClick={() => JobFocus.open(row.workspace_id, row.job_id)}>{jobNameFallback(dims, row.workspace_id, row.job_id)}</button>
        <div className="cj-oversized-meta">{resolveName("workspace", row.workspace_id, row.workspace_id)}</div>
      </div>
      <span className="cj-oversized-pct">{p90Peak(row.worker_cpu_p90_pct, row.worker_cpu_peak_pct)}</span>
      <span className="cj-oversized-pct">{p90Peak(row.worker_mem_p90_pct, row.worker_mem_peak_pct)}</span>
      <span className="cj-oversized-action">{sentenceCase(row.suggested_action) || "-"}</span>
      <span className="cj-oversized-usd">{row.saving_usd != null ? fmtMoney(row.saving_usd, 0) : "-"}</span>
    </div>
  );
}

function JobsComputeFitContent({ findings, filters, dims, maxCat, onVerdict }: LooseProps) {
  const pressureState = useFindingData("lakeflow_job_compute_pressure", filters.window, filters.workspaceIds, filters.envs);
  const taskState = useFindingData("task_cluster_utilization", filters.window, filters.workspaceIds, filters.envs);
  // The donut counts every task run server-side; the row page above stays worst-first for flags.
  const hintAgg = useFindingAgg("task_cluster_utilization", filters.window, filters.workspaceIds, filters.envs, ["bottleneck_hint"], "count");
  const oversizedState = useFindingData("lakeflow_job_oversized", filters.window, filters.workspaceIds, filters.envs);

  const pressureRows = readyRows(pressureState);
  const taskRows = readyRows(taskState);
  const oversizedRows = readyRows(oversizedState);

  const flagged = pressureRows ? pressureRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  // The check's own limits (its settings in effect), so red marks exactly what it flags on.
  const pressureParams = (pressureState.data && pressureState.data.header && pressureState.data.header.params) || {};
  const paramValue = (k: string, dflt: number) => { const v = pressureParams[k] && Number(pressureParams[k].value); return Number.isFinite(v) ? v : dflt; };
  const pressureLimits = { mem: paramValue("crit_mem_pct", 85), swap: paramValue("crit_swap_pct", 10), cpu: paramValue("busy_cpu_pct", 70) };
  const taskFlagged = taskRows ? taskRows.filter((r) => r.status === "CRITICAL") : null;
  const pressureSegs = pressureRows ? pressureSegments(pressureRows) : null;
  // The top cause among FLAGGED jobs only -- pressureSegs above counts every job (including the
  // ones with no pressure at all), which is the right mix for the donut but the wrong one for
  // "what's actually wrong with the flagged jobs".
  const flaggedCauseSegs = flagged && flagged.length ? pressureSegments(flagged) : null;
  const topCause = flaggedCauseSegs && flaggedCauseSegs.length ? [...flaggedCauseSegs].sort((a, b) => b.value - a.value)[0] : null;
  const hintReady = hintAgg.phase === "ready" && hintAgg.outcome === "ok_rows";
  const taskSegs = hintReady
    ? (hintAgg.data.groups || []).map((g) => { const name = String((g.key && g.key[0]) || "(none)"); return { label: enumLabel(name) || name, value: numOrZero(g.value), display: fmtInt(numOrZero(g.value)), color: pressureColor(name) }; })
    : null;

  const oversizedDisc = oversizedState.data ? numOrZero(oversizedState.data.discount_pct) : 0;
  const oversizedWithSaving = oversizedRows
    ? oversizedRows.map((r): Row => ({ ...r, saving_usd: r.est_saving_usd_list != null ? numOrZero(r.est_saving_usd_list) * (1 - oversizedDisc) : null }))
    : null;
  const oversizedFlagged = oversizedWithSaving
    ? [...oversizedWithSaving.filter((r) => r.status === "CRITICAL" || r.status === "WARN")].sort((a, b) => numOrZero(b.saving_usd) - numOrZero(a.saving_usd))
    : null;
  const oversizedTotalUsd = oversizedFlagged ? oversizedFlagged.reduce((s, r) => s + numOrZero(r.saving_usd), 0) : 0;
  // Oversized but saving less than the check's flag line: still shown, never hidden behind "0".
  const oversizedSmall = oversizedWithSaving
    ? [...oversizedWithSaving.filter((r) => r.oversized === true && r.status === "OK")].sort((a, b) => numOrZero(b.saving_usd) - numOrZero(a.saving_usd))
    : null;
  const oversizedSmallUsd = oversizedSmall ? oversizedSmall.reduce((s, r) => s + numOrZero(r.saving_usd), 0) : 0;
  const oversizedShown = oversizedFlagged && oversizedSmall ? [...oversizedFlagged, ...oversizedSmall] : null;

  React.useEffect(() => {
    if (!onVerdict || !pressureRows) return;
    const oversizedTail = oversizedFlagged && oversizedFlagged.length
      ? ` ${fmtInt(oversizedFlagged.length)} more job${oversizedFlagged.length === 1 ? "" : "s"} run${oversizedFlagged.length === 1 ? "s" : ""} on more machine than ${oversizedFlagged.length === 1 ? "it" : "they"} use, about ${fmtMoney(oversizedTotalUsd, 0)} to save.`
      : "";
    onVerdict((flagged!.length
      ? `${fmtInt(flagged!.length)} of ${fmtInt(pressureRows.length)} jobs' compute is a poor fit for their workload this window.`
      : `${fmtInt(pressureRows.length)} jobs checked -- none are under compute pressure.`) + oversizedTail);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pressureRows, flagged, oversizedFlagged, oversizedTotalUsd]);

  return (
    <div>
      <CjKpis>
        <CjKpi label="Jobs under pressure" tone={flagged && flagged.length ? "crit" : ""} value={pressureRows ? `${fmtInt(flagged!.length)} of ${fmtInt(pressureRows.length)}` : kpiPending(pressureState)}
          facts={topCause ? [{ label: "Top cause", value: topCause.label, detail: `${fmtInt(topCause.value)} of ${fmtInt(flagged!.length)}` }] : [{ label: "Causes", value: "None", tone: "ok" }]} />
        <CjKpi label="Tasks flagged" tone={taskFlagged && taskFlagged.length ? "crit" : ""} value={taskRows ? flaggedCountLabel(taskState, taskRows, taskFlagged, findingFor(findings, "task_cluster_utilization")) : kpiPending(taskState)} facts={[{ label: "Scope", value: "Runs over 2h" }]} />
        <CjKpi
          label="Jobs on oversized compute"
          tone={oversizedFlagged && oversizedFlagged.length ? "warn" : ""}
          value={oversizedRows ? fmtInt(oversizedShown!.length) : kpiPending(oversizedState)}
          facts={oversizedRows ? (oversizedFlagged && oversizedFlagged.length ? [
            { label: "To save", value: fmtMoney(oversizedTotalUsd + oversizedSmallUsd, 0) },
            { label: "Flagged", value: fmtInt(oversizedFlagged.length), detail: `worst ${jobNameFallback(dims, oversizedFlagged[0].workspace_id, oversizedFlagged[0].job_id)}` },
          ] : oversizedSmall && oversizedSmall.length ? [
            { label: "To save", value: fmtMoney(oversizedSmallUsd, 0) },
            { label: "Flagged", value: "0", detail: "each saves less than the flag line" },
          ] : [
            { label: "Oversized", value: "None found", tone: "ok" },
          ]) : null}
        />
      </CjKpis>
      <div className="cj-charts-2">
        <Card title="Jobs by compute pressure">
          {pressureSegs ? <Donut segments={pressureSegs} centerLabel={String(pressureRows!.length)} centerSub="jobs" /> : <ChartNote state={pressureState} label="lakeflow_job_compute_pressure" />}
        </Card>
        <Card title="Tasks by bottleneck hint">
          {taskSegs ? (
            <React.Fragment>
              <Donut segments={taskSegs} centerLabel={fmtInt(numOrZero(hintAgg.data!.total_value))} centerSub="tasks" />
            </React.Fragment>
          ) : <ChartNote state={hintAgg} label="task_cluster_utilization" />}
        </Card>
      </div>
      {flagged && flagged.length > 0 && (
        <Card title="Flagged jobs, worst first" right={<span className="muted">{`${fmtInt(flagged.length)} of ${fmtInt(pressureRows!.length)} jobs`}</span>}>
          <div className="cj-oversized-table">
            <div className="cj-oversized-head cj-pressure-grid">
              <span>Job</span><span>Cause</span><span>Memory</span><span>CPU</span><span>Fix</span><span style={{ textAlign: "right" }}>Task hours</span>
            </div>
            {flagged.slice(0, maxCat || 8).map((r) => <PressureJobRow key={`${r.workspace_id}:${r.job_id}`} row={r} dims={dims} limits={pressureLimits} />)}
            {flagged.length > (maxCat || 8) && (
              <div className="cj-chart-takeaway">{`${fmtInt(flagged.length - (maxCat || 8))} more flagged job${flagged.length - (maxCat || 8) === 1 ? "" : "s"} not shown -- see Checks below.`}</div>
            )}
          </div>
        </Card>
      )}

      {oversizedState.phase !== "loading" && oversizedState.outcome && oversizedState.outcome !== "ok_rows" && (
        <ChartNote state={oversizedState} label="lakeflow_job_oversized" />
      )}
      {oversizedShown && oversizedShown.length > 0 && (
        <Card title="Jobs on bigger machines than they need" right={<span className="muted">{`${fmtInt(oversizedFlagged!.length)} flagged${oversizedSmall!.length ? ` · ${fmtInt(oversizedSmall!.length)} more with a small saving` : ""}, biggest saving first`}</span>}>
          <div className="cj-oversized-table">
            <div className="cj-oversized-head">
              <span>Job</span><span>CPU p90 / peak</span><span>Memory p90 / peak</span><span>Fix</span><span style={{ textAlign: "right" }}>Est. saving</span>
            </div>
            {oversizedShown.slice(0, maxCat || 8).map((r) => <OversizedJobRow key={`${r.workspace_id}:${r.job_id}`} row={r} dims={dims} />)}
            {oversizedShown.length > (maxCat || 8) && (
              <div className="cj-chart-takeaway">{`${fmtInt(oversizedShown.length - (maxCat || 8))} more oversized job${oversizedShown.length - (maxCat || 8) === 1 ? "" : "s"} not shown -- see Checks below.`}</div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
AreaContent.register("jobs", "compute_fit", JobsComputeFitContent);

// ─────────────────────────────────────────── Pipelines ────────────────────────────────────────

// pipelineName's own fallback ("pipeline <id>") reads as a bare id with no explanation once dims
// has no row for it -- the pipeline was deleted, or system.lakeflow.pipelines' recording window
// starts after it last changed (that table is a snapshot, not full history). Checked the same way
// pipelineName itself resolves a name, so this never misreads a real pipeline literally named
// "pipeline <its own id>" as missing.
function pipelineLabel(dims: any, workspaceId: any, pipelineId: any) {
  const known = dims && dims.pipelines && dims.pipelines.has(`${workspaceId}:${pipelineId}`);
  return known
    ? pipelineName(dims, workspaceId, pipelineId)
    // Deleted, or unchanged since recording started: the id leads so a cut-off label still names it.
    : `pipeline ${String(pipelineId).slice(0, 8)} (no name recorded)`;
}

function JobsPipelinesContent({ findings, filters, dims, maxCat, onVerdict }: LooseProps) {
  const costState = useFindingData("lakeflow_pipeline_cost", filters.window, filters.workspaceIds, filters.envs);
  const idleTailState = useFindingData("lakeflow_pipeline_idle_tail_duration", filters.window, filters.workspaceIds, filters.envs);
  const failuresState = useFindingData("lakeflow_pipeline_update_failures_retries", filters.window, filters.workspaceIds, filters.envs);

  const costRows = readyRows(costState);
  const idleTailRows = readyRows(idleTailState);
  const failureRows = readyRows(failuresState);

  const disc = costState.data ? numOrZero(costState.data.discount_pct) : 0;
  const totalCost = costRows ? costRows.reduce((s, r) => s + numOrZero(r.net_list_cost), 0) * (1 - disc) : 0;
  const idleTailFlaggedRows = idleTailRows ? idleTailRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const idleTailFlagged = idleTailFlaggedRows ? idleTailFlaggedRows.length : null;
  const idleTailWorst = idleTailFlaggedRows && idleTailFlaggedRows.length
    ? [...idleTailFlaggedRows].sort((a, b) => numOrZero(b.net_dbus) - numOrZero(a.net_dbus))[0] : null;
  const failedUpdates = failureRows ? sumBy(failureRows, "failed_update_rows") : null;
  const idleDisc = idleTailState.data ? numOrZero(idleTailState.data.discount_pct) : 0;
  const failedWorst = failureRows && failedUpdates
    ? groupSum(failureRows, (r) => `${r.workspace_id}\u0000${r.pipeline_id}`, "failed_update_rows").sort((a, b) => b.value - a.value)[0] : null;

  React.useEffect(() => {
    if (!onVerdict || !costRows) return;
    onVerdict(`${fmtInt(costRows.length)} pipeline${costRows.length === 1 ? "" : "s"} spent about ${fmtMoney(totalCost, 0)} in ${filters.window}d.${failedUpdates ? ` ${fmtInt(failedUpdates)} update${failedUpdates === 1 ? "" : "s"} failed.` : ""}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [costRows, totalCost, failedUpdates]);

  return (
    <div>
      <CjKpis>
        <CjKpi label="Pipeline spend" value={costRows ? fmtMoney(totalCost, 0) : kpiPending(costState)} facts={costRows ? [{ label: "Pipelines", value: fmtInt(costRows.length) }, { label: "Window", value: `${filters.window}d` }] : null} />
        <CjKpi label="Idle tail flagged" tone={numOrZero(idleTailFlagged) > 0 ? "warn" : ""} value={idleTailRows ? fmtInt(idleTailFlagged) : kpiPending(idleTailState)}
          facts={idleTailWorst ? [{ label: "Worst", value: pipelineLabel(dims, idleTailWorst.workspace_id, idleTailWorst.pipeline_id), detail: fmtMoney(numOrZero(idleTailWorst.est_usd_list) * (1 - idleDisc), 0) }] : [{ label: "Risk", value: "None", tone: "ok" }]} />
        <CjKpi label="Failed updates" tone={numOrZero(failedUpdates) > 0 ? "warn" : ""} value={failureRows ? fmtInt(failedUpdates) : kpiPending(failuresState)}
          facts={failedWorst ? [{ label: "Worst", value: pipelineLabel(dims, ...(failedWorst.name.split("\u0000") as [string, string])), detail: `${fmtInt(failedWorst.value)} failed` }, { label: "Window", value: `${filters.window}d` }] : [{ label: "Window", value: `${filters.window}d` }]} />
      </CjKpis>
      <Card title="Spend by pipeline" right={<StatusPill kind="ranked" word="Ranked" compact />}>
        {costRows ? (
          <HBarList
            items={[...costRows].sort((a, b) => numOrZero(b.net_list_cost) - numOrZero(a.net_list_cost)).slice(0, maxCat || 8)
              .map((r) => ({ name: pipelineLabel(dims, r.workspace_id, r.pipeline_id), value: numOrZero(r.net_list_cost) * (1 - disc) }))}
            valueFmt={(v) => fmtMoney(v, 0)}
          />
        ) : <ChartNote state={costState} label="lakeflow_pipeline_cost" />}
        <p className="cj-chart-takeaway">Ranked by spend alone, not a threshold check -- see Checks below for the flagged pipeline checks.</p>
      </Card>
    </div>
  );
}
AreaContent.register("jobs", "pipelines", JobsPipelinesContent);
