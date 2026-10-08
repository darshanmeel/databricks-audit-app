// section 6 "Jobs > Run panel (new)": clicking a job (any
// <Ref kind="job"> anywhere in the app, or a job-table row on this tab) opens this right-hand
// drawer. It shows what the EXISTING job_id-scoped findings already carry for that one job.
// lakeflow_job_recent_runs carries each run's own duration, failing task, discounted cost and SQL
// error sample; job_run_cost is now only a fallback for a job recent_runs has nothing for yet.
// lakeflow_jobs_no_timeout / lakeflow_health_rule_coverage are now one row per job too, so this
// panel reads this job's own timeout/health-rule setup directly instead of the account-wide gap.

import React from "react";
import type { FindingData, Header, Id, Row } from "../types";

import { createRoot } from "react-dom/client";

import { Api } from "../api";
import { fmtBytes, fmtDbu, fmtDuration, fmtInt, fmtMoney } from "../format";
import { CopyLinkButton } from "./hash_state";
import { DbxLink, JobFocus, Names, deepLink, resolveName, useNames } from "./names";
import { fmtChangePct, numOrZero } from "./hooks";
import { ErrorBoundary, StatusPill } from "./primitives";
import { enumLabel } from "./labels";
import { HBarList } from "./charts";
import { EmptyWindowCard, ErrorCard, NotAssessedCard } from "./finding_detail";
import { isCancelledCode, shortRunWhen } from "../tabs/tab_jobs";

// The findings that already carry a job_id filter param and answer this panel's questions:
// reliability numbers (+ the timeout suggestion and success-duration stats), the run's own
// latest-failure fields, the failed-run waste dollar figure, lakeflow_job_recent_runs (duration,
// cost and failing task per run, job_run_cost its fallback), lakeflow_job_duration_regression
// (is the job slower than its own baseline, and which part grew), lakeflow_job_run_changes
// (what changed since the job's last good run), and this job's own timeout/health-rule setup.
const JOB_PANEL_IDS = ["lakeflow_job_reliability", "lakeflow_failed_jobs_wasted_dbus", "lakeflow_job_run_cost", "lakeflow_job_recent_runs", "lakeflow_job_duration_regression", "lakeflow_job_run_changes", "lakeflow_jobs_no_timeout", "lakeflow_health_rule_coverage"];

/** One check's answer for this job, or a stand-in error answer when the fetch failed. */
type PanelData = Partial<FindingData>;

/** A run as the panel shows it, from recent_runs or (older exports) run_cost. */
interface RunRow {
  run_id: Id;
  run_start: string | null;
  result_state: string | null;
  termination_code: string | null;
  in_flight: boolean | null;
  duration_s: number | null;
  duration_is_lower_bound: boolean;
  cost: number | null;
  net_run_dbus: number | null;
  failing_task_keys: string | null;
  failing_task_code: string | null;
  sql_error_sample: string | null;
  source: string;
}

/** Failed runs with the same termination code and failing task. */
interface FailGroup {
  termination_code: string | null;
  failing_task_keys: string | null;
  failing_task_code: string | null;
  count: number;
  latest: string | null;
  latestRunId: Id;
}

/** One "What to do" step, with an optional link to the run it names. */
interface Step {
  text: string;
  link?: { kind: string; workspaceId: Id; id: Id; runId: Id } | null;
}

function useJobPanelData(workspaceId: Id, jobId: Id, windowDays: number) {
  const key = `${workspaceId}|${jobId}|${windowDays}`;
  const [state, setState] = React.useState<{ phase: string; byId: Record<string, PanelData> }>({ phase: "loading", byId: {} });
  React.useEffect(() => {
    let cancelled = false;
    setState({ phase: "loading", byId: {} });
    if (workspaceId === undefined || workspaceId === null || jobId === undefined || jobId === null) return undefined;
    // job_id is passed server-side (app/core/data._build_filters), so every row that comes back is
    // already this job's own -- no client-side match needed.
    Promise.all(JOB_PANEL_IDS.map((id) =>
      Api.finding(id, windowDays, [String(workspaceId)], [], 200, 0, String(jobId))
        .then((d): [string, PanelData] => [id, d])
        .catch((e): [string, PanelData] => [id, { outcome: "error", error: e.message || String(e), header: {} as Header }])
    )).then((pairs) => {
      if (cancelled) return;
      const byId: Record<string, PanelData> = {};
      pairs.forEach(([id, d]) => { byId[id] = d; });
      setState({ phase: "ready", byId });
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

// shortRunWhen (a fixed "24 Sep, 22:08" UTC reading of a run's own start timestamp) comes from
// tab_jobs.tsx, loaded just before this file -- one definition for both the job table and this
// drawer's own recent-runs list.

function jobPanelRow(data: PanelData | null | undefined): Row | null {
  if (!data || data.outcome !== "ok_rows" || !data.rows || !data.rows.length) return null;
  return data.rows[0];
}

function JobRunPanelKpis({ reliability, wasted, windowDays }: { reliability?: PanelData; wasted?: PanelData; windowDays: number }) {
  const rel = jobPanelRow(reliability);
  const failedLabel = rel ? `${fmtInt(rel.failed_runs)} of ${fmtInt(rel.runs)}` : "-";
  const streak = rel ? fmtInt(rel.consecutive_failures) : "-";
  const wastedRow = jobPanelRow(wasted);
  const disc = wasted && wasted.outcome === "ok_rows" ? numOrZero(wasted.discount_pct) : 0;
  const lostUsd = wastedRow && wastedRow.est_wasted_usd_list != null
    ? fmtMoney(numOrZero(wastedRow.est_wasted_usd_list) * (1 - disc), 0)
    : "-";
  // T13: the job's whole window spend (wasted's own est_usd_list, "shown for scale") over its
  // successful runs -- '-' with zero successes, never a divide-by-zero blank.
  const perSuccess = rel && rel.successful_runs > 0 && wastedRow && wastedRow.est_usd_list != null
    ? fmtMoney((numOrZero(wastedRow.est_usd_list) * (1 - disc)) / rel.successful_runs, 2)
    : "-";
  return (
    <div className="jrp-kpis">
      <div className="jrp-kpi">
        <span className="jrp-kpi-label">{`Failed, ${windowDays}d`}</span>
        <span className={`jrp-kpi-value ${rel && rel.failed_runs > 0 ? "crit" : ""}`}>{failedLabel}</span>
      </div>
      <div className="jrp-kpi">
        <span className="jrp-kpi-label">In a row, now</span>
        <span className={`jrp-kpi-value ${rel && rel.consecutive_failures > 0 ? "crit" : ""}`}>{streak}</span>
      </div>
      <div className="jrp-kpi">
        <span className="jrp-kpi-label">$ lost on failures</span>
        <span className="jrp-kpi-value">{lostUsd}</span>
      </div>
      <div className="jrp-kpi">
        <span className="jrp-kpi-label">$ per successful run</span>
        <span className="jrp-kpi-value">{perSuccess}</span>
      </div>
    </div>
  );
}

// The latest non-cancelled failed row of the normalized recent runs -- shared by JobRunPanelFailure
// and jobPanelWhatToDo's own link so both name and open the same run.
function latestFailedRun(rows: RunRow[]): RunRow | null {
  return rows.find((r) => {
    const cancelled = isCancelledCode(r.result_state);
    return !r.in_flight && r.result_state && r.result_state !== "SUCCEEDED" && !cancelled;
  }) || null;
}

// "task X", plus the task's own termination code when it says more than the run's, e.g. a cloud
// failure starting its cluster behind a run that only reads "run execution error".
function taskPhrase(keys: string | null, taskCode: string | null, runCode: string | null): string | null {
  if (!keys) return null;
  const code = taskCode && taskCode !== runCode ? (enumLabel(taskCode) || taskCode).toLowerCase() : null;
  return code ? `task ${keys} (${code})` : `task ${keys}`;
}

// C16: names the latest FAILED run from recent_runs (its true first-slice start, UTC), not just
// reliability's own summary fields, and links straight to it.
function JobRunPanelFailure({ reliability, rows, workspaceId, jobId }: { reliability?: PanelData; rows: RunRow[]; workspaceId: Id; jobId: Id }) {
  const rel = jobPanelRow(reliability);
  const failed = latestFailedRun(rows);
  if (!failed) return null;
  const label = enumLabel(failed.termination_code) || enumLabel(failed.result_state) || failed.result_state;
  return (
    <div className="jrp-failure">
      <div className="jrp-failure-label">Latest failure</div>
      <div className="jrp-failure-title">{`${label} · ${shortRunWhen(failed.run_start)} UTC`}</div>
      <div className="jrp-failure-body">
        {failed.failing_task_keys ? `Failed in ${taskPhrase(failed.failing_task_keys, failed.failing_task_code, failed.termination_code)}. ` : ""}
        {rel && rel.last_n_summary ? `${rel.last_n_summary}. ` : ""}
        The error message itself is not in these system tables -- open the run in Databricks to read it.
      </div>
      <DbxLink kind="run" workspaceId={workspaceId} id={jobId} runId={failed.run_id} />
    </div>
  );
}

// One shape for the chart, the run list and the failure grouping below, so all three read the
// same runs: prefers lakeflow_job_recent_runs (duration, failing task, cost, sql error), falls
// back to job_run_cost alone (date/result/cost only) when the newer check has nothing for this
// job yet (not assessed, errored, or a source that predates it) -- C16 fix: recent_runs now
// carries its own discounted cost, so job_run_cost is a fallback, not a second lookup.
function normalizedRecentRuns(runCost: PanelData | undefined, recentRuns: PanelData | undefined): RunRow[] {
  const recentList = recentRuns && recentRuns.outcome === "ok_rows" && recentRuns.rows && recentRuns.rows.length ? recentRuns.rows : null;
  if (recentList) {
    const disc = numOrZero(recentRuns!.discount_pct);
    return [...recentList]
      .sort((a, b) => String(b.run_start || "").localeCompare(String(a.run_start || "")))
      .map((r): RunRow => ({
        run_id: r.run_id, run_start: r.run_start, result_state: r.result_state,
        termination_code: r.termination_code, in_flight: r.in_flight,
        duration_s: r.run_minutes != null ? r.run_minutes * 60 : null,
        duration_is_lower_bound: !!r.run_duration_is_lower_bound,
        cost: r.net_list_cost != null ? numOrZero(r.net_list_cost) * (1 - disc) : null,
        net_run_dbus: r.net_dbus, failing_task_keys: r.failing_task_keys,
        failing_task_code: r.failing_task_termination_code || null,
        sql_error_sample: r.sql_error_sample, source: "recent_runs",
      }));
  }
  if (!runCost || runCost.outcome !== "ok_rows" || !runCost.rows || !runCost.rows.length) return [];
  const disc = numOrZero(runCost.discount_pct);
  return [...runCost.rows]
    .sort((a, b) => String(b.run_start || "").localeCompare(String(a.run_start || "")))
    .map((r): RunRow => ({
      run_id: r.job_run_id, run_start: r.run_start, result_state: r.result_state,
      termination_code: r.termination_code, in_flight: r.in_flight,
      duration_s: null, duration_is_lower_bound: false,
      cost: r.net_list_cost != null ? numOrZero(r.net_list_cost) * (1 - disc) : null,
      net_run_dbus: r.net_run_dbus, failing_task_keys: null, failing_task_code: null, sql_error_sample: null,
      source: "run_cost",
    }));
}

function runDotKind(r: RunRow): string {
  const cancelled = isCancelledCode(r.result_state);
  const failed = r.result_state && r.result_state !== "SUCCEEDED" && !cancelled;
  return r.in_flight || cancelled ? "var(--text-faint)" : failed ? "var(--st-crit)" : "var(--st-ok)";
}

// T8: a small bar chart of the last 10 runs (duration, coloured by result) using HBarList, the
// existing "ranked horizontal bars" component -- each bar's own label doubles as its UTC-time
// tooltip (the label element's native title).
function JobRunPanelRunsChart({ rows }: { rows: RunRow[] }) {
  const withDuration = rows.filter((r): r is RunRow & { duration_s: number } => r.duration_s != null);
  if (!withDuration.length) return null;
  const items = withDuration.slice(0, 10).map((r) => ({
    name: shortRunWhen(r.run_start), value: r.duration_s, color: runDotKind(r),
  }));
  return <HBarList items={items} valueFmt={(v) => fmtDuration(v)} />;
}

function JobRunPanelRecentRuns({ rows }: { rows: RunRow[] }) {
  if (!rows.length) return <div className="jrp-gap-note">No billed run has landed for this job in this window yet.</div>;
  return (
    <React.Fragment>
      {rows.slice(0, 10).map((r, i) => {
        const cancelled = isCancelledCode(r.result_state);
        const failed = r.result_state && r.result_state !== "SUCCEEDED" && !cancelled;
        const resultLabel = r.in_flight ? "Running now" : (enumLabel(r.result_state) || r.result_state || "-");
        const reasonBits = failed && !r.in_flight
          ? [enumLabel(r.termination_code) || r.termination_code, taskPhrase(r.failing_task_keys, r.failing_task_code, r.termination_code)].filter((b): b is string => !!b)
          : [];
        const reasonLabel = reasonBits.length ? reasonBits.join(" -- ") : null;
        const durationLabel = r.duration_s != null ? `${fmtDuration(r.duration_s)}${r.duration_is_lower_bound ? "+" : ""}` : "-";
        const costLabel = r.cost != null ? fmtMoney(r.cost, 2) : (r.net_run_dbus != null ? fmtDbu(r.net_run_dbus, 1) : "-");
        return (
          <div key={String(r.run_id || i)}>
            <div className="jrp-run-row">
              <span className="jrp-run-dot" style={{ background: runDotKind(r) }}></span>
              <span className="jrp-run-when">{shortRunWhen(r.run_start)}</span>
              <span className={`jrp-run-result ${failed ? "fail" : ""}`}>
                {resultLabel}
                {reasonLabel && <span className="muted"> -- {reasonLabel}</span>}
              </span>
              <span className="jrp-run-value">{durationLabel}</span>
              <span className="jrp-run-value">{costLabel}</span>
            </div>
            {r.sql_error_sample && <div className="jrp-sql-error">{r.sql_error_sample}</div>}
          </div>
        );
      })}
      {rows.every((r) => r.source === "run_cost") && (
        <div className="jrp-gap-note" style={{ marginTop: 8 }}>
          Each run's own duration and which task failed are not in this source yet -- shown here is
          date, result and cost only.
        </div>
      )}
    </React.Fragment>
  );
}

// T13: identical failures (termination code + failing task) grouped as one problem, worst
// (most repeated) group first; each group keeps the run_id of its own latest occurrence so the
// "Do this" list can link straight to it (C16).
function groupFailures(rows: RunRow[]): FailGroup[] {
  const failed = rows.filter((r) => {
    const cancelled = isCancelledCode(r.result_state);
    return !r.in_flight && r.result_state && r.result_state !== "SUCCEEDED" && !cancelled;
  });
  const groups = new Map<string, FailGroup>();
  failed.forEach((r) => {
    const key = `${r.termination_code || "unknown"}|${r.failing_task_keys || ""}|${r.failing_task_code || ""}`;
    const g = groups.get(key) || { termination_code: r.termination_code, failing_task_keys: r.failing_task_keys, failing_task_code: r.failing_task_code, count: 0, latest: r.run_start, latestRunId: r.run_id };
    g.count += 1;
    if (String(r.run_start || "") > String(g.latest || "")) { g.latest = r.run_start; g.latestRunId = r.run_id; }
    groups.set(key, g);
  });
  return [...groups.values()].sort((a, b) => b.count - a.count);
}

function JobRunPanelFailureGroups({ groups: sorted }: { groups: FailGroup[] }) {
  if (!sorted.length) return null;
  return (
    <div>
      <div className="jrp-section-head">
        <h3 className="jrp-section-title">Recent failures</h3>
      </div>
      <ul className="jrp-fail-groups">
        {sorted.map((g, i) => {
          const label = enumLabel(g.termination_code) || g.termination_code || "failed";
          const taskBit = g.failing_task_keys ? ` in ${taskPhrase(g.failing_task_keys, g.failing_task_code, g.termination_code)}` : "";
          return (
            <li key={i}>
              {`${fmtInt(g.count)} run${g.count === 1 ? "" : "s"}: ${label.toLowerCase()}${taskBit} (last ${shortRunWhen(g.latest)} UTC)`}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const CHANGE_ATTR_LABEL: Record<string, string> = {
  job_definition: "the job definition", runtime_version: "the runtime version",
  worker_node_type: "the worker node type", worker_count: "the worker count",
  upstream_tables: "the upstream tables", input_bytes: "the input size",
  queue_s: "queue time", setup_s: "setup time", run_s: "run time",
};

// T8: every "Do this" step carries a number or a concrete setting -- streak, timeout, slower
// part, what changed, in that order (root cause first, prevention last). Each step is
// {text, link} -- link is an optional DbxLink({kind:'run', ...}) prop set, rendered next to the
// step's own text (C16: the failing run it names is openable, not just described).
function jobPanelWhatToDo(reliability: PanelData | undefined, wasted: PanelData | undefined, durReg: PanelData | undefined,
  changes: PanelData | undefined, failGroups: FailGroup[], runRows: RunRow[], workspaceId: Id, jobId: Id): Step[] {
  const rel = jobPanelRow(reliability);
  const wastedRow = jobPanelRow(wasted);
  const disc = wasted && wasted.outcome === "ok_rows" ? numOrZero(wasted.discount_pct) : 0;
  const steps: Step[] = [];
  const latestFailed = failGroups && failGroups.length ? failGroups[0] : null;
  const latestFailedRow = latestFailedRun(runRows);
  if (rel && rel.latest_result_state && rel.latest_result_state !== "SUCCEEDED" && !isCancelledCode(rel.latest_result_state)) {
    // text and link must name the SAME run -- rel.latest_run_start (reliability, today excluded)
    // and latestFailedRow (recent runs, today included) can disagree, so read both from
    // latestFailedRow when it exists and only fall back to reliability's own summary otherwise.
    const label = latestFailedRow
      ? (enumLabel(latestFailedRow.termination_code) || enumLabel(latestFailedRow.result_state) || latestFailedRow.result_state)
      : (enumLabel(rel.latest_termination_code) || enumLabel(rel.latest_result_state) || rel.latest_result_state);
    const when = latestFailedRow ? shortRunWhen(latestFailedRow.run_start) : shortRunWhen(rel.latest_run_start);
    const noCode = !(latestFailedRow ? latestFailedRow.termination_code : rel.latest_termination_code);
    steps.push({
      text: noCode
        ? `Open the ${when} run in Databricks for its error -- no termination code was recorded and the message itself isn't in these system tables.`
        : `Open the ${when} run in Databricks and read its error (${label}) -- the message itself isn't in these system tables.`,
      link: latestFailedRow ? { kind: "run", workspaceId, id: jobId, runId: latestFailedRow.run_id } : null,
    });
  }
  if (latestFailed && latestFailed.count >= 2) {
    const label = (enumLabel(latestFailed.termination_code) || latestFailed.termination_code || "").toLowerCase();
    const taskBit = latestFailed.failing_task_keys ? ` in ${taskPhrase(latestFailed.failing_task_keys, latestFailed.failing_task_code, latestFailed.termination_code)}` : "";
    steps.push({ text: label
      ? `${fmtInt(latestFailed.count)} of its recent runs failed the same way: ${label}${taskBit} -- fix that one cause first.`
      : `${fmtInt(latestFailed.count)} of its recent runs failed with no termination code recorded${taskBit} -- read the latest one first.` });
  } else if (rel && rel.consecutive_failures >= 2) {
    steps.push({ text: `This has failed ${fmtInt(rel.consecutive_failures)} times in a row -- check whether the earlier runs share the same cause before assuming this is one-off.` });
  }
  if (wastedRow && wastedRow.est_wasted_usd_list > 0) {
    steps.push({ text: `${fmtMoney(numOrZero(wastedRow.est_wasted_usd_list) * (1 - disc), 0)} of compute has been spent on runs that did not finish -- add a timeout so a stuck run does not keep billing.` });
  }
  if (rel && rel.suggested_timeout_minutes != null) {
    steps.push({ text: `Set a timeout of ${fmtInt(rel.suggested_timeout_minutes)} min (2x p95 of ${fmtInt(rel.successful_runs)} successful runs; longest ${fmtInt(rel.max_success_minutes)} min) if it has none.` });
  }
  const dr = jobPanelRow(durReg);
  if (dr && (dr.status === "CRITICAL" || dr.status === "WARN") && dr.grew_most && dr.baseline_median_minutes != null && dr.recent_median_minutes != null) {
    const part = dr.grew_most === "queue_or_setup" ? "queue and setup (capacity) grew" : "run time (code or data) grew";
    const pct = dr.slowdown_ratio != null ? ` (${fmtChangePct((dr.slowdown_ratio - 1) * 100)})` : "";
    steps.push({ text: `Slower: run time ${fmtDuration(dr.baseline_median_minutes * 60)} → ${fmtDuration(dr.recent_median_minutes * 60)}${pct} -- ${part}, look there first.` });
  }
  const changedRows = changes && changes.outcome === "ok_rows" ? (changes.rows || []).filter((r) => r.changed) : [];
  if (changedRows.length) {
    const top = changedRows[0];
    const attr = CHANGE_ATTR_LABEL[top.attribute] || top.attribute;
    steps.push({ text: `Something changed since its last good run: ${attr} was "${top.baseline_value}", now "${top.latest_value}".` });
  }
  if (!steps.length) {
    steps.push({ text: "Nothing flagged for this job right now." });
  }
  return steps;
}

function changeValueLabel(attribute: string, value: string | number | null | undefined): React.ReactNode {
  if (value == null || value === "null") return "-";
  if (attribute === "input_bytes") return fmtBytes(Number(value));
  if (attribute === "queue_s" || attribute === "setup_s" || attribute === "run_s") return fmtDuration(Number(value));
  return value;
}

// T11: what changed since the job's last good run before it failed or ran slow -- changed rows
// first (the finding's own ORDER BY already sorts that way; the client-side sort is a second,
// harmless guarantee for a caller that re-orders the rows).
// not_assessed/error get their own honest card (the finding errors outright rather than degrading
// when system.storage.table_metrics_history is off, so a bare "no rows" read would wrongly tell a
// failing job it is fine); "not failed or slow" is said only once reliability confirms the latest
// run actually succeeded, never as the default read of an empty/degraded result.
function JobRunPanelChanges({ changes, rel, windowDays }: { changes?: PanelData; rel: Row | null; windowDays: number }) {
  if (!changes) return null;
  if (changes.outcome === "error") return <ErrorCard data={changes} />;
  if (changes.outcome === "not_assessed") return <NotAssessedCard data={changes} />;
  if (changes.outcome === "ok_rows" && changes.rows && changes.rows.length) {
    if (changes.rows.some((r) => r.baseline_kind === "no_success_in_window")) {
      return <div className="jrp-gap-note">No successful run in this window to compare against.</div>;
    }
    const rows = [...changes.rows].sort((a, b) => (b.changed === true ? 1 : 0) - (a.changed === true ? 1 : 0));
    return (
      <div className="jrp-changes-table">
        {rows.map((r, i) => (
          <div className="jrp-changes-row" key={i}>
            <span className="jrp-changes-attr">{CHANGE_ATTR_LABEL[r.attribute] || r.attribute}</span>
            <span className={`jrp-changes-diff ${r.changed ? "changed" : ""}`}>
              {changeValueLabel(r.attribute, r.baseline_value)} {"→"} {changeValueLabel(r.attribute, r.latest_value)}
            </span>
          </div>
        ))}
      </div>
    );
  }
  if (rel && rel.latest_result_state === "SUCCEEDED") {
    return <div className="jrp-gap-note">Latest run is not failed or slow -- nothing to compare.</div>;
  }
  return <div className="jrp-gap-note">{`No data for this in the last ${fmtInt(windowDays)} days.`}</div>;
}

function JobRunPanel({ workspaceId, jobId, onClose }: { workspaceId: Id; jobId: Id; onClose: () => void }) {
  useNames();
  const [windowDays, setWindowDays] = React.useState(30);
  const focus = useJobPanelData(workspaceId, jobId, windowDays);

  React.useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onEsc);
    return () => document.removeEventListener("keydown", onEsc);
  }, [onClose]);

  const reliability = focus.byId.lakeflow_job_reliability;
  const wasted = focus.byId.lakeflow_failed_jobs_wasted_dbus;
  const runCost = focus.byId.lakeflow_job_run_cost;
  const recentRuns = focus.byId.lakeflow_job_recent_runs;
  const durReg = focus.byId.lakeflow_job_duration_regression;
  const changes = focus.byId.lakeflow_job_run_changes;
  const timeoutRow = jobPanelRow(focus.byId.lakeflow_jobs_no_timeout);
  const healthRow = jobPanelRow(focus.byId.lakeflow_health_rule_coverage);
  const rel = jobPanelRow(reliability);
  const runRows = React.useMemo(() => normalizedRecentRuns(runCost, recentRuns), [runCost, recentRuns]);
  const failGroups = React.useMemo(() => groupFailures(runRows), [runRows]);

  const jobRec = Names.jobRecord(workspaceId, jobId);
  // resolveName's own "job" case already tries another workspace's name for this same job_id when
  // it is unique account-wide, before falling back to the bare id (names.tsx).
  const jobDisplayName = (rel && rel.job_name) || resolveName("job", workspaceId, jobId) || `job ${jobId}`;
  const wsName = resolveName("workspace", workspaceId, workspaceId);
  const jobUrl = deepLink("job", workspaceId, jobId);

  const headerStatus = rel
    ? (rel.status === "CRITICAL" ? { word: "Critical · broken now", kind: "critical" }
      : rel.status === "WARN" ? { word: "Warning · flaky", kind: "warn" }
      : rel.status === "NOT_ASSESSED" ? { word: "Not assessed", kind: "not_assessed" }
      : { word: "OK", kind: "ok" })
    // Loaded with no run row for this job: say so, never leave the pill on "loading...".
    : focus.phase !== "loading" ? { word: "No runs in window", kind: "not_assessed" } : null;

  return (
    <React.Fragment>
      <div className="jrp-backdrop" onClick={onClose}></div>
      <div className="jrp-panel" role="dialog" aria-modal="true" aria-label={jobDisplayName}>
        <div className="jrp-head">
          <div className="jrp-head-row">
            {headerStatus ? <StatusPill kind={headerStatus.kind} word={headerStatus.word} /> : <span className="muted">loading...</span>}
            <button type="button" className="jrp-close" title="Close" onClick={onClose}>{"✕"}</button>
          </div>
          <h2 className="jrp-title">{jobDisplayName}</h2>
          <div className="jrp-sub">
            {wsName ? `${wsName}` : `workspace ${workspaceId}`}
            {rel ? ` · ${fmtInt(rel.runs)} runs in ${windowDays}d` : ""}
          </div>
        </div>

        <div className="jrp-body">
          {focus.phase === "loading" && <div className="loading-note">Loading...</div>}
          {focus.phase === "ready" && reliability && reliability.outcome === "not_assessed" && <NotAssessedCard data={reliability} />}
          {focus.phase === "ready" && reliability && reliability.outcome === "error" && <ErrorCard data={reliability} />}
          {focus.phase === "ready" && reliability && (reliability.outcome === "ok_empty_window" || reliability.outcome === "ok_empty_filters") && (
            <EmptyWindowCard data={reliability} />
          )}

          {rel && (
            <React.Fragment>
              <JobRunPanelKpis reliability={reliability} wasted={wasted} windowDays={windowDays} />
              <JobRunPanelFailure reliability={reliability} rows={runRows} workspaceId={workspaceId} jobId={jobId} />

              <div>
                <div className="jrp-section-head">
                  <h3 className="jrp-section-title">Recent runs</h3>
                  <span className="jrp-section-hint">most recent first</span>
                </div>
                <JobRunPanelRunsChart rows={runRows} />
                <JobRunPanelRecentRuns rows={runRows} />
              </div>

              <JobRunPanelFailureGroups groups={failGroups} />

              <div>
                <div className="jrp-section-head">
                  <h3 className="jrp-section-title">What changed since the last good run</h3>
                </div>
                <JobRunPanelChanges changes={changes} rel={rel} windowDays={windowDays} />
              </div>

              <div>
                <h3 className="jrp-section-title" style={{ marginBottom: 6 }}>Owner &amp; setup</h3>
                <div className="jrp-owner-grid">
                  <span className="jrp-owner-key">Workspace</span><span>{wsName || `workspace ${workspaceId}`}</span>
                  <span className="jrp-owner-key">Runs as</span>
                  <span>{(jobRec && (jobRec.run_as_user_name || jobRec.run_as)) || "Not recorded"}</span>
                  <span className="jrp-owner-key">Created by</span>
                  <span>{(jobRec && jobRec.creator_user_name) || "Not recorded"}</span>
                  <span className="jrp-owner-key">Timeout</span>
                  <span>
                    {timeoutRow
                      ? (timeoutRow.no_timeout ? "Not configured"
                        : timeoutRow.bounded_by_health_rule ? "Bounded by health rule"
                        : fmtDuration(numOrZero(timeoutRow.timeout_seconds)))
                      : "Not recorded"}
                  </span>
                  <span className="jrp-owner-key">Health rule</span>
                  <span>
                    {healthRow
                      ? (healthRow.health_rule_count == null ? "Not recorded"
                        : healthRow.health_rule_count > 0 ? `${fmtInt(healthRow.health_rule_count)} configured`
                        : "None configured")
                      : "Not recorded"}
                  </span>
                </div>
              </div>

              <div>
                <h3 className="jrp-section-title" style={{ marginBottom: 6 }}>What to do</h3>
                <ol className="jrp-todo">
                  {jobPanelWhatToDo(reliability, wasted, durReg, changes, failGroups, runRows, workspaceId, jobId).map((s, i) => (
                    <li key={i}>{s.text}{s.link && <DbxLink {...s.link} />}</li>
                  ))}
                </ol>
                <div className="jrp-actions" style={{ marginTop: 10 }}>
                  {jobUrl
                    ? <a className="jrp-btn-primary" href={jobUrl} target="_blank" rel="noopener noreferrer">Open job in Databricks</a>
                    : <span className="jrp-gap-note">No Databricks link -- this workspace has no known url.</span>}
                  <CopyLinkButton label="Copy job link" />
                </div>
              </div>
            </React.Fragment>
          )}
        </div>
      </div>
    </React.Fragment>
  );
}

function JobRunPanelOverlay() {
  const [state, setState] = React.useState(JobFocus.getState());
  React.useEffect(() => JobFocus.subscribe(setState), []);
  if (!state.open) return null;
  // Keyed by job so switching jobs while open remounts a fresh boundary/fetch.
  return (
    <ErrorBoundary key={`${state.workspaceId}:${state.jobId}`}>
      <JobRunPanel workspaceId={state.workspaceId} jobId={state.jobId} onClose={JobFocus.close} />
    </ErrorBoundary>
  );
}

// Self-mounted, independent of App's own root -- so a job reference on ANY tab (Overview, Cost,
// Compute, ...) can open this panel, not only one reached from the Jobs tab itself.
(function mountJobRunPanel() {
  if (typeof document === "undefined") return;
  let el = document.getElementById("job-focus-root");
  if (!el) {
    el = document.createElement("div");
    el.id = "job-focus-root";
    document.body.appendChild(el);
  }
  createRoot(el).render(<JobRunPanelOverlay />);
})();

// data_gaps (section 7 item 4 of the redesign brief): lakeflow_job_recent_runs closed the per-run
// duration and failing-task gap (see "Recent runs" above); lakeflow_jobs_no_timeout and
// lakeflow_health_rule_coverage are now one row per job too, so "Owner & setup" above reads this
// job's own timeout/health-rule setup directly instead of an account-wide aggregate.
