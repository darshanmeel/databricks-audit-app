// Coverage & Gaps: sub-tabs sources / couldnt / limits.
// "An empty panel means could not look or nothing yet, never a verified zero" -- true whether
// this build is a dbt/snapshot build or a direct export (cov.export_mode, GET /api/coverage), and
// a source with 0 rows is never shown as a verified "ok" (state == "empty" is derived here from
// rows == 0, not read from the API, per section 7 item 2 of the redesign brief).

import React from "react";

import { Api } from "../api";
import { fmtDate, fmtDayMonth, fmtInt, fmtMoney } from "../format";
import { numOrZero, sumBy, useFindingData } from "../components/hooks";
import { bandOf } from "../components/tab_registry";
import { AreaContent, Card, Facts, StatusPill, assessedCounts, bandPillKind, isUncertainBand } from "../components/primitives";
import { getCheckLabel } from "../components/labels";
import { ChartNote, HBar, Kpi, KpiRow } from "../components/charts";
import { NOT_BUILT_READING, RegionCoverageCard, isNotBuilt, notAssessedText } from "../components/scope";
import { FindingsTable } from "../components/findings_table";
import { navHref } from "../components/nav_hash";
import type { Fact } from "../types";

// Coverage plus its load error; tab_governance.tsx's useCoverage() returns just the coverage.
function useCoverageWithError() {
  const [cov, setCov] = React.useState<any>(null);
  const [error, setError] = React.useState<any>(null);
  React.useEffect(() => {
    let cancelled = false;
    Api.coverage().then((d) => { if (!cancelled) setCov(d); }).catch((e) => { if (!cancelled) setError(e.message || String(e)); });
    return () => { cancelled = true; };
  }, []);
  return { cov, error };
}

// Source keys are Databricks system-table names (e.g. "system.compute.node_timeline"), not
// query_ids -- grouped here into the same areas the rest of the app uses, by name pattern only
// (presentation, no API change: app/api is off limits for this pass).
const CV_AREA_ORDER = ["Billing & account", "Compute", "Jobs & pipelines", "Queries", "ML & AI", "Governance & PII", "Storage"];
function cvAreaForKey(key: any) {
  if (/^system\.billing\./.test(key) || key === "system.access.workspaces_latest") return "Billing & account";
  if (/^system\.compute\./.test(key)) return "Compute";
  if (/^system\.lakeflow\./.test(key)) return "Jobs & pipelines";
  if (/^system\.query\./.test(key)) return "Queries";
  if (/^system\.serving\./.test(key) || /^system\.ai_gateway\./.test(key)) return "ML & AI";
  if (/^system\.storage\./.test(key)) return "Storage";
  return "Governance & PII"; // system.access.*, information_schema.*, system.data_classification.*
}

// A plain, sentence-case label mechanically derived from the table name -- never a fabricated
// description, just the technical name turned into words.
function cvSourceLabel(key: any) {
  const last = key.split(".").pop().replace(/_/g, " ");
  return last.charAt(0).toUpperCase() + last.slice(1);
}

// section 7 item 2: "ok" state at 0 rows is not really ok -- derive "empty" here so the amber
// styling and the KPI counts below never call a 0-row source a verified pass. "not_assessed"
// (this build never captured the source at all) stays its own state, apart from "error" (the
// source WAS attempted and the read failed) -- direct-export mode reports every source
// not_assessed, and that must never read as 47 real read failures.
const CV_READ_FAILED = new Set(["no_grant", "schema_not_enabled", "table_not_found", "timeout", "unknown"]);
function cvEffectiveState(info: any) {
  if (!info) return "not_assessed";
  if (info.state === "ok" && (info.rows === 0 || info.rows === null || info.rows === undefined)) return "empty";
  // A snapshot or direct export files a read failure under not_assessed with its reason.
  if (info.state === "not_assessed" && CV_READ_FAILED.has(info.reason)) return "error";
  return info.state || "not_assessed";
}

// Grounded per-source notes for the sources this app most commonly finds empty (Public Preview
// features, opt-in settings, or export-identity visibility) -- a source absent here gets an honest
// generic line rather than an invented reason.
const CV_FILL_TEXT: Record<string, string> = {
  "system.billing.attributed_usage": "Fills in once serverless SQL usage is attributed to a query; without it, failed-statement waste can't be priced.",
  "system.compute.instance_pools": "Only has rows if the account uses instance pools -- may be a true zero.",
  "system.ai_gateway.usage": "Only has rows once endpoints route traffic through AI Gateway.",
  "system.access.audit": "Public Preview; holds only what the export identity may see. Run-as detail needs verbose audit logging turned on.",
  "system.access.table_lineage": "Written only when Unity Catalog sees a read or write; retention is set per workspace.",
  "system.access.column_lineage": "Same as table lineage, at column level.",
  "system.data_classification.results": "Needs the data-classification feature and schema turned on (Public Preview, serverless required).",
  "system.storage.predictive_optimization_operations_history": "Needs Predictive Optimization turned on (default only for accounts created after 11 Nov 2024); managed tables only.",
  "system.access.inbound_network": "Public Preview; rows appear only when a network policy blocks a request -- may be a true zero.",
  "system.access.outbound_network": "Public Preview; rows appear only when a network policy blocks a request -- may be a true zero.",
};
function cvFillText(key: any, noun: any) {
  if (CV_FILL_TEXT[key]) return CV_FILL_TEXT[key];
  if (/^information_schema\./.test(key)) return "Empty until something matching exists, or the export identity can't see it.";
  return `Empty in this ${noun} -- may be a true zero, or a feature not turned on for this account.`;
}

// cov.export_mode: "direct" (tools/load_direct_results.py, no snapshot_manifest.json) reads its
// wording as "export"; an ordinary dbt/snapshot build keeps "snapshot"; "none" (nothing run yet)
// falls back to the generic "build".
function cvNoun(mode: any) {
  if (mode === "direct") return "export";
  if (mode === "none") return "build";
  return "snapshot";
}

// A source/check's own reason CODE (direct_source_states / checks_not_ok, app/core/data.py's
// COVERAGE_REASON_FIX -- and the same vocabulary tools/snapshot.py's manifest `reason` already
// uses in snapshot mode) -> the plain words a reader sees, never the bare code.
const CV_REASON_LABEL: Record<string, string> = {
  schema_not_enabled: "system table not enabled",
  table_not_found: "system table not enabled",
  no_grant: "no permission to read it",
  too_much_data: "too much data to export",
  timeout: "timed out reading it",
  excluded_by_flag: "excluded from this export",
  not_exported: "not run this export",
  unknown: "failed in the export",
};
function cvReasonText(info: any) {
  if (info.reason && CV_REASON_LABEL[info.reason]) return CV_REASON_LABEL[info.reason];
  return info.reason || info.message || "Not in this export.";
}

// Every sub-tab's own one-line "what this shows" plus a deep link into the matching Guide
// section (slugs fixed on the Guide side: coverage-data-sources / coverage-couldnt-check /
// coverage-known-limitations) -- these are home topics, not checks, so the link needs the
// "how-<slug>" prefix guide.tsx's GuideTab uses to tell the two kinds of focus apart.
function HowToReadThis({ id, goTo }: LooseProps) {
  const focus = `how-${id}`;
  return (
    <a
      className="ck-link"
      href={navHref({ tab: "guide", focus })}
      onClick={(e) => { e.preventDefault(); goTo({ tab: "guide", focusQueryId: focus }); }}
    >
      {"How to read this →"}
    </a>
  );
}
function CvLede({ text, guideId, goTo }: LooseProps) {
  return (
    <div className="muted" style={{ marginBottom: 10 }}>
      {text} <HowToReadThis id={guideId} goTo={goTo} />
    </div>
  );
}

// access_source_table_coverage judges gap/lag on only these 5 system tables (the ones that fill
// every day) -- every other row it returns always reads OK regardless of count (its own
// investigate_if), so showing all 45 here would just repeat "Sources by area" above with a second
// row-count column. `key` matches the dotted keys used everywhere else on this page.
const CV_DAILY_FILL_TABLES = new Set([
  "system.access.audit", "system.billing.usage", "system.query.history",
  "system.compute.node_timeline", "system.lakeflow.job_run_timeline",
]);
function cvTableKey(row: any) {
  return `system.${row.schema_name}.${row.table_name}`;
}

// scope.tsx's own isNotBuilt/NOT_BUILT_READING (shared with ChartNote, charts.tsx) -- a small
// local wrapper for the two cards below, which render their own not-built state rather than
// falling through to a plain ChartNote.
function cvNotBuiltOneLiner(state: any) {
  return state && state.phase === "ready" && isNotBuilt(state.data) ? NOT_BUILT_READING : null;
}

function SourceTableHealthCard({ filters }: LooseProps) {
  const state = useFindingData("access_source_table_coverage", filters.window, filters.workspaceIds, filters.envs);
  const notBuilt = cvNotBuiltOneLiner(state);
  const rows = state.phase === "ready" && state.outcome === "ok_rows" ? state.data.rows : null;
  const daily = rows ? rows.filter((r) => CV_DAILY_FILL_TABLES.has(cvTableKey(r))) : null;
  const otherCount = rows ? rows.length - daily!.length : 0;
  const windowDays = state.data && state.data.window_days;

  return (
    <Card
      title="System table health"
      right={<span className="muted" style={{ fontSize: 12 }}>A live freshness check, not this export's own capture above</span>}
    >
      {notBuilt ? (
        <div className="chart-note-oneline">{notBuilt}</div>
      ) : daily && daily.length > 0 ? (
        <React.Fragment>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr><th>Table</th><th className="num">Rows</th><th>Newest</th><th className="num">Lag</th><th className="num">Days with no rows</th><th>Status</th></tr>
              </thead>
              <tbody>
                {daily.map((r) => {
                  const key = cvTableKey(r);
                  const gap = (windowDays && r.days_with_data_in_window != null)
                    ? Math.max(0, (windowDays - 1) - r.days_with_data_in_window)
                    : null;
                  return (
                    <tr key={key}>
                      <td>
                        <div>{cvSourceLabel(key)}</div>
                        <div className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>{key}</div>
                      </td>
                      <td className="num mono">{fmtInt(r.total_row_count)}</td>
                      <td>{fmtDayMonth(r.max_time) || "-"}</td>
                      <td className="num mono">{r.lag_days != null ? `${fmtInt(r.lag_days)}d` : "-"}</td>
                      <td className="num mono">{gap != null ? `${fmtInt(gap)}d` : "-"}</td>
                      <td><StatusPill kind={bandPillKind(r.status)} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {otherCount > 0 && (
            <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
              {`${fmtInt(otherCount)} more system table${otherCount === 1 ? "" : "s"} read OK. Warn above means a table that should fill every day has days with no rows; these ${fmtInt(otherCount)} fill only on events, so gaps there are not flagged.`}
            </div>
          )}
        </React.Fragment>
      ) : rows ? (
        <div className="muted">{`${fmtInt(rows.length)} system tables read; none of the 5 daily-fill tables are in this account's inventory.`}</div>
      ) : (
        <ChartNote state={state} label="access_source_table_coverage" />
      )}
    </Card>
  );
}

// One small card, not a table -- what running this audit itself cost, from cost_audit_self_usage
// (coverage-only: that query never emits a status column, see its own header).
function AuditSelfCostKpi({ filters }: LooseProps) {
  const state = useFindingData("cost_audit_self_usage", filters.window, filters.workspaceIds, filters.envs);
  const notBuilt = cvNotBuiltOneLiner(state);
  const ready = state.phase === "ready";
  const rows = ready && state.outcome === "ok_rows" ? state.data.rows : null;

  let value = "-";
  let sub = "Loading...";
  let facts: Fact[] | null = null;
  if (notBuilt) {
    facts = [{ label: "Status", value: notBuilt, tone: "muted" }];
  } else if (state.phase === "error") {
    sub = state.error || "Could not load.";
  } else if (ready && state.outcome === "not_assessed") {
    sub = notAssessedText(state.data);
  } else if (ready && state.outcome === "ok_empty_window") {
    value = fmtMoney(0, 2);
    sub = "No audit queries matched this window.";
  } else if (rows && rows.length > 0) {
    // est_audit_usd_list_disc is the same figure at the account's own configured discount --
    // columns.ts's own convention (see tab_query.tsx's waste total) -- falling back to the raw
    // list-price column only if a caller's build predates that twin.
    const auditUsd = (r: any) => (r.est_audit_usd_list_disc != null ? r.est_audit_usd_list_disc : r.est_audit_usd_list);
    const queries = sumBy(rows, "matched_statement_count");
    const priced = rows.filter((r) => auditUsd(r) != null);
    const usd = priced.reduce((s, r) => s + numOrZero(auditUsd(r)), 0);
    const unpriced = rows.some((r) => r.price_basis === "unpriced");
    value = fmtMoney(usd, 2);
    facts = [
      { label: "Queries", value: fmtInt(queries) },
      { label: "Warehouses", value: fmtInt(rows.length) },
      { label: "Basis", value: "List price" },
    ];
    if (unpriced) facts.push({ label: "Unpriced", value: "Some usage", tone: "warn", detail: "understated" });
  }

  return <Kpi label="What this audit cost" value={value} sub={sub} facts={facts} />;
}

function CoverageSourcesPanel({ goTo, filters }: LooseProps) {
  const { cov, error } = useCoverageWithError();
  if (error) return <div className="honest-card error"><div className="h-title">Could not read coverage</div><div className="h-note mono">{error}</div></div>;
  if (!cov) return <div className="loading-note">Loading coverage...</div>;

  const entries = Object.entries<any>(cov.tables || {});
  const withRows = entries.filter(([, i]) => (i.rows || 0) > 0 && i.state !== "error").length;
  const empty = entries.filter(([, i]) => cvEffectiveState(i) === "empty").length;
  // Red is real read failures only; a source this build never captured (not_assessed -- every
  // source, in direct-export mode) is its own state, not folded into "could not be read".
  const notRecorded = entries.filter(([, i]) => cvEffectiveState(i) === "not_assessed").length;
  const missing = entries.filter(([, i]) => cvEffectiveState(i) === "error").length;

  const models = Object.entries<any>(cov.models || {});
  const modelsOk = models.filter(([, m]) => m.status === "success" || m.status === "pass").length;
  const problemModels = models.filter(([, m]) => m.status && m.status !== "success" && m.status !== "pass");

  const grouped: Record<string, any> = {};
  entries.forEach(([key, info]) => { (grouped[cvAreaForKey(key)] = grouped[cvAreaForKey(key)] || []).push([key, info]); });

  const noun = cvNoun(cov.export_mode);
  const isDirect = cov.export_mode === "direct";

  return (
    <div>
      <CvLede
        text={`What this shows: which of the ${fmtInt(entries.length)} system tables this ${noun} actually read, and how many checks depend on each.`}
        guideId="coverage-data-sources"
        goTo={goTo}
      />
      {cov.export_mode === "none" ? (
        // No export record: this build can't say which sources it read, so the counts would all be 0.
        <div className="page-intro-verdict">
          {`This build carries no export record, so it can't say which of the ${fmtInt(entries.length)} system tables it read. System table health below is a live read.`}
        </div>
      ) : (
      <React.Fragment>
      <div className="page-intro-verdict">
        {`This audit reads ${fmtInt(entries.length)} Databricks system tables: ${fmtInt(withRows)} had data in this ${noun}`}
        {empty > 0 ? `, ${fmtInt(empty)} were empty (feature not used here)` : ""}
        {notRecorded > 0 ? `, ${fmtInt(notRecorded)} not captured by this ${noun}` : ""}
        {missing > 0 ? `, ${fmtInt(missing)} could not be read (not enabled or no access).` : "."}
      </div>

      <div className="kpi-row" style={{ marginTop: 14 }}>
        <div className="kpi-tile">
          <div className="kpi-label">Sources</div>
          <div className="kpi-value">{fmtInt(entries.length)}</div>
          <Facts items={[{ label: "Read OK", value: `${fmtInt(modelsOk)} of ${fmtInt(models.length)} checks` }]} />
        </div>
        <div className="kpi-tile">
          <div className="kpi-label">With rows</div>
          <div className="kpi-value tone-sage">{fmtInt(withRows)}</div>
          <Facts items={[{ label: "Status", value: "Holds data", tone: "ok" }]} />
        </div>
        <div className="kpi-tile">
          <div className="kpi-label">Empty</div>
          <div className="kpi-value tone-amber">{fmtInt(empty)}</div>
          <Facts items={[{ label: "Status", value: "0 rows", tone: "warn", detail: "not a verified zero" }]} />
        </div>
        <div className="kpi-tile">
          <div className="kpi-label">Not recorded</div>
          <div className="kpi-value tone-lavender">{fmtInt(notRecorded)}</div>
          <Facts items={[{ label: "Status", value: "Not captured", tone: "muted", detail: "this export" }]} />
        </div>
        <div className="kpi-tile">
          <div className="kpi-label">Missing</div>
          <div className={`kpi-value${missing > 0 ? " tone-coral" : ""}`}>{fmtInt(missing)}</div>
          <Facts items={[{ label: "Status", value: missing > 0 ? "Could not be read" : "None refused", tone: missing > 0 ? "crit" : "ok" }]} />
        </div>
      </div>
      </React.Fragment>
      )}

      <Card
        title="Sources by area"
        right={(
          <span className="muted" style={{ fontSize: 12 }}>
            {isDirect
              ? "\"Checks OK\" is how many of the checks reading this source ran clean -- \"Used by\" counts them"
              : "\"Used by\" counts the checks and reference tables that read the source"}
          </span>
        )}
      >
        <HBar
          height={20}
          segments={[
            { label: "With rows", value: withRows, color: "var(--st-ok)" },
            { label: "Empty", value: empty, color: "var(--st-warn)" },
            { label: "Not recorded", value: notRecorded, color: "var(--st-na)" },
            { label: "Missing", value: missing, color: "var(--st-crit)" },
          ].filter((s) => s.value > 0)}
        />
        <div className="table-wrap" style={{ marginTop: 12 }}>
          <table className="data cv-sources-table">
            <thead>
              <tr><th>Source</th><th className="num">{isDirect ? "Checks OK" : "Rows"}</th><th>Covers</th><th className="num">Used by</th><th>If empty: what fills it</th></tr>
            </thead>
            <tbody>
              {CV_AREA_ORDER.filter((a) => grouped[a]).map((area) => (
                <React.Fragment key={area}>
                  <tr className="cv-area-row"><td colSpan={5}>{area}</td></tr>
                  {grouped[area].map(([key, info]: [string, any]) => {
                    const state = cvEffectiveState(info);
                    const rows = info.rows;
                    // isDirect: a source's own true row count is never read (direct_source_states,
                    // data.py, only sums what its reading checks happened to return) -- "current
                    // state" is also wrong for the handful of sources that are a trailing window,
                    // not a live view, so both come from data.py's own covers/window_days instead
                    // of guessing from min_time/max_time (snapshot mode's own real per-table dates).
                    const range = !isDirect
                      ? ((info.min_time || info.max_time)
                        ? `${fmtDate(info.min_time) || "?"} – ${fmtDate(info.max_time) || "?"}`
                        : (rows ? "current state" : "-"))
                      : (state === "not_assessed" ? "-"
                        : info.covers === "window" ? (info.window_days ? `last ${fmtInt(info.window_days)}d` : "trailing window")
                          : "current state");
                    return (
                      <tr key={key} className={state === "empty" ? "cv-row-empty" : state === "not_assessed" ? "cv-row-na" : ""}>
                        <td>
                          <div>{cvSourceLabel(key)}</div>
                          <div className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>{key}</div>
                        </td>
                        <td className="num">
                          {isDirect
                            ? (info.checks_total ? `${fmtInt(info.checks_ok || 0)} of ${fmtInt(info.checks_total)}` : <span className="muted">-</span>)
                            : (rows == null
                              ? (state === "not_assessed"
                                ? <span className="cv-rows-badge na">not recorded</span>
                                : <span className="muted">-</span>)
                              : state === "empty"
                                ? <span className="cv-rows-badge empty">{`${fmtInt(rows)} rows`}</span>
                                : fmtInt(rows))}
                        </td>
                        <td>{range}</td>
                        <td className="num">{fmtInt((info.dependent_query_ids || []).length)}</td>
                        <td style={{ fontSize: 12 }}>
                          {state === "empty" ? cvFillText(key, noun)
                            : state === "not_assessed" ? "Not recorded in this export."
                            : state !== "ok" ? cvReasonText(info) : ""}
                        </td>
                      </tr>
                    );
                  })}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <SourceTableHealthCard filters={filters} />
      <KpiRow><AuditSelfCostKpi filters={filters} /></KpiRow>

      {!isDirect && problemModels.length > 0 && (
        <Card title="Models not built cleanly">
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>query_id</th><th>status</th><th>error class</th><th>message</th></tr></thead>
              <tbody>
                {problemModels.map(([qid, m]) => (
                  <tr key={qid}><td className="id mono">{qid}</td><td>{m.status}</td><td>{m.error_class || "-"}</td><td>{m.message || "-"}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <RegionCoverageCard region={cov.region} noun={noun} />

      <a
        className="cv-limits-pointer"
        href={navHref({ tab: "coverage", subtab: "limits" })}
        onClick={(e) => { e.preventDefault(); goTo({ tab: "coverage", subtab: "limits" }); }}
      >
        {"Known limitations →"}
      </a>
    </div>
  );
}

// checks_not_ok entries grouped by their shared (reason_label, fix) pair -- table_not_found and
// schema_not_enabled both read "system table not enabled" with the same fix, so a reader sees one
// bucket to act on, not two reason codes that mean the same thing.
function groupChecksNotOk(checks: any) {
  const byKey: Record<string, any> = {};
  const order: any[] = [];
  (checks || []).forEach((c: any) => {
    const key = `${c.reason_label}|${c.fix}`;
    if (!byKey[key]) { byKey[key] = { label: c.reason_label, fix: c.fix, checks: [] }; order.push(key); }
    byKey[key].checks.push(c);
  });
  return order.map((k) => byKey[k]).sort((a, b) => b.checks.length - a.checks.length);
}

function ChecksNotOkCard({ checks }: LooseProps) {
  if (!checks || checks.length === 0) return null;
  const groups = groupChecksNotOk(checks);
  return (
    <Card title={`${fmtInt(checks.length)} check${checks.length === 1 ? " is" : "s are"} not in this export, by reason`}>
      {groups.map((g) => (
        <div key={`${g.label}|${g.fix}`} className="cv-reason-group" style={{ marginBottom: 14 }}>
          <div><b>{`${fmtInt(g.checks.length)} ${g.label}`}</b></div>
          <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>{g.fix}</div>
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>Check</th><th>Reads</th></tr></thead>
              <tbody>
                {g.checks.map((c: any) => (
                  <tr key={c.query_id}>
                    <td>{c.title || c.query_id} <span className="mono muted" style={{ fontSize: 11 }}>{`(${c.query_id})`}</span></td>
                    <td className="mono" style={{ fontSize: 11 }}>{(c.sources || []).join(", ") || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </Card>
  );
}

function CoverageCouldntPanel({ allFindingsIndex, filters, onExternalJump, goTo, meta }: LooseProps) {
  const { cov, error } = useCoverageWithError();
  const all = React.useMemo(() => Object.values<any>(allFindingsIndex || {}), [allFindingsIndex]);
  const gaps = React.useMemo(() => all.filter((f) => isUncertainBand(bandOf(f))), [all]);
  const counts: Record<string, number> = { NOT_ASSESSED: 0, ERROR: 0, EMPTY_WINDOW: 0, EMPTY_FILTERS: 0 };
  gaps.forEach((f) => { const b = bandOf(f); counts[b] = (counts[b] || 0) + 1; });
  const noun = cov ? cvNoun(cov.export_mode) : "build";

  return (
    <div>
      <CvLede
        text="What this shows: every check that failed or never ran, grouped by why, plus every check with nothing to judge this window."
        guideId="coverage-couldnt-check"
        goTo={goTo}
      />

      {error ? (
        <div className="honest-card error"><div className="h-title">Could not read coverage</div><div className="h-note mono">{error}</div></div>
      ) : !cov ? (
        <div className="loading-note">Loading coverage...</div>
      ) : (
        <ChecksNotOkCard checks={cov.checks_not_ok} />
      )}

      <div className="page-intro-verdict" style={{ marginTop: 14 }}>
        {`${fmtInt(gaps.length)} of ${fmtInt(assessedCounts(all).total)} checks couldn't judge anything this window: `}
        {`${fmtInt(counts.NOT_ASSESSED)} not assessed, ${fmtInt(counts.ERROR)} read errors, `}
        {`${fmtInt(counts.EMPTY_WINDOW)} nothing in this window, ${fmtInt(counts.EMPTY_FILTERS)} excluded by filters.`}
      </div>
      <div className="chart-note not_assessed" style={{ margin: "14px 0" }}>
        <div className="chart-note-text">{`An empty panel means the audit couldn't look, or there was nothing to look at when the ${noun} was taken. It is never a verified zero.`}</div>
      </div>
      {gaps.length > 0 ? (
        <FindingsTable findings={gaps} refIds={[]} filters={filters} allFindingsIndex={allFindingsIndex} onExternalJump={onExternalJump} meta={meta} />
      ) : (
        <div className="card"><span className="muted">Every check could judge something in this window and these filters.</span></div>
      )}
    </div>
  );
}

// Plain notes only, where a limitation changes how to read a number elsewhere in the app -- not a
// developer changelog (the full library-corrections register leaves the UI; see docs/LIBRARY_CORRECTIONS.md).
const CV_LIMITATIONS = [
  {
    title: "Some rankings are by size, not a threshold",
    note: "Spend by job, spend by cluster or warehouse, and spend by pipeline are ranked by size alone -- they always read \"Ranked\", never Critical or Warn, however big the number.",
  },
  {
    title: "Autoscale churn counts events, not time",
    note: "Warehouse autoscale churn counts how often a warehouse's cluster count changes, not how long it spent scaled up.",
  },
  {
    title: "Task memory-pressure rule",
    note: "A job task is flagged for memory pressure from its 90th-percentile worker memory and swap; the swap threshold defaults to 10% in config/thresholds.yml, so a single brief spike can be enough to flag it.",
  },
  {
    title: "Redacted SQL text pools together",
    note: "When a statement's text is hidden from the export, every redacted statement in the window pools into one heaviest-statement shape instead of being counted separately.",
  },
];

function CoverageLimitsPanel({ goTo, allFindingsIndex }: LooseProps) {
  const { cov, error } = useCoverageWithError();
  const notFixed = cov ? (cov.library_corrections || []).filter((c: any) => c.status === "not_fixed") : [];
  const truncated = cov ? (cov.truncated_query_ids || []) : [];
  const outside = cov && cov.region ? cov.region.outside : null;
  const outsideCount = outside ? outside.length : 0;
  const outsideBilled = outside ? outside.filter((w: any) => w.billed).length : 0;

  return (
    <div>
      <CvLede
        text="What this shows: what still limits these numbers, from checks with a known accuracy issue to partial or out-of-region coverage."
        guideId="coverage-known-limitations"
        goTo={goTo}
      />
      <div className="page-intro-verdict">
        {cov
          ? `${fmtInt(notFixed.length)} check${notFixed.length === 1 ? "" : "s"} with a known accuracy issue, ${fmtInt(truncated.length)} export row cap${truncated.length === 1 ? "" : "s"} hit, ${fmtInt(outsideCount)} workspace${outsideCount === 1 ? "" : "s"} out of region (${fmtInt(outsideBilled)} with spend), plus ${fmtInt(CV_LIMITATIONS.length)} general note${CV_LIMITATIONS.length === 1 ? "" : "s"}.`
          : `${fmtInt(CV_LIMITATIONS.length)} general note${CV_LIMITATIONS.length === 1 ? "" : "s"}.`}
      </div>

      {error && <div className="honest-card error"><div className="h-title">Could not read coverage</div><div className="h-note mono">{error}</div></div>}

      {notFixed.length > 0 && (
        <Card title={`${fmtInt(notFixed.length)} check${notFixed.length === 1 ? "" : "s"} with a known accuracy issue`}>
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>Check</th><th>Effect on this number</th><th></th></tr></thead>
              <tbody>
                {notFixed.map((c: any) => (
                  <tr key={c.id}>
                    <td>{c.query_title || c.query_id}</td>
                    <td className="wrap" style={{ fontSize: 12 }}>{c.effect}</td>
                    <td style={{ fontSize: 12 }}>
                      <details>
                        <summary className="ck-link" style={{ cursor: "pointer" }}>Problem</summary>
                        <div className="muted" style={{ marginTop: 4 }}>{c.problem}</div>
                      </details>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {truncated.length > 0 && (
        <Card title={`${fmtInt(truncated.length)} check${truncated.length === 1 ? "" : "s"} hit their row cap this export`}>
          <p className="cv-limit-note">
            {"Only the first rows per window were kept for: "}
            {truncated.map((qid: any, i: any) => {
              const known = (allFindingsIndex && allFindingsIndex[qid]) || null;
              const label = getCheckLabel(qid, known ? known.title : qid);
              return (
                <React.Fragment key={qid}>
                  {i > 0 && ", "}
                  {label.title} <span className="mono muted" style={{ fontSize: 11 }}>{`(${qid})`}</span>
                </React.Fragment>
              );
            })}
            {"."}
          </p>
        </Card>
      )}

      {outsideCount > 0 && (
        <Card title={`${fmtInt(outsideCount)} workspace${outsideCount === 1 ? "" : "s"} outside the covered region`}>
          <p className="cv-limit-note">
            {"Their regional checks (compute, jobs, queries, serving, storage, lineage, audit) are not assessed. Billing and the workspace list are account-wide. "}
            <a
              className="ck-link"
              href={navHref({ tab: "coverage", subtab: "sources" })}
              onClick={(e) => { e.preventDefault(); goTo({ tab: "coverage", subtab: "sources" }); }}
            >
              See which workspaces →
            </a>
          </p>
        </Card>
      )}

      {CV_LIMITATIONS.map((l) => (
        <Card title={l.title} key={l.title}><p className="cv-limit-note">{l.note}</p></Card>
      ))}
    </div>
  );
}

AreaContent.register("coverage", "sources", CoverageSourcesPanel);
AreaContent.register("coverage", "couldnt", CoverageCouldntPanel);
AreaContent.register("coverage", "limits", CoverageLimitsPanel);
