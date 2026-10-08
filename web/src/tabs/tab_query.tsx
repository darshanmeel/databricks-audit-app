// Queries area: heavy / efficiency / reliability / capacity /
// team sub-tabs (redesign section 6). Each Content component (AreaContent.register,
// primitives.tsx) renders that sub-tab's own headline cards + panels; AreaPage already renders the
// checks table below it, so nothing here re-lists a check's rows.

import React from "react";
import { DAY_ROWS_MAX, GrainLines, GrainSwitch } from "../components/grain";
import type { Grain } from "../components/grain";

import { fmtDayShort, fmtDuration, fmtDurationMs, fmtGb, fmtInt, fmtMoney, fmtPct } from "../format";
import { DbxLink, Ref, resolveName } from "../components/names";
import { dailySeries, filledDailySeries, groupSum, numOrZero, rowsOf, sumBy, topNWithOther, useDims, useFindingAgg, useFindingData, warehouseName } from "../components/hooks";
import { bandOf } from "../components/tab_registry";
import { AreaContent, Card, PqgsNote, StatusPill, bandPillKind } from "../components/primitives";
import { enumLabel, getCheckLabel } from "../components/labels";
import { ChartNote, Donut, HBarList, paletteColor, shortId } from "../components/charts";
import { QueueHeatCards } from "./query_queue";
import { startsText, useStartupWaits } from "./startup_waits";
import { TagRollupView } from "../components/tag_rollup";
import { notAssessedText } from "../components/scope";
import { maxClustersChangeLine, pressureColor, pressureLeverLabel } from "../components/pressure";
import { STATUS_RANK } from "./tab_jobs";
import type { Row } from "../types";

// ─────────── Heavy queries ───────────
// query_top_by_cost's own group_kind values -> the plain word this page shows for each.
const TOP_COST_GROUP_LABEL: Record<string, string> = {
  "text hash": "Repeated statement",
  "saved query": "Saved query",
  dashboard: "Dashboard",
  "Genie space": "Genie space",
  alert: "Alert",
  job: "Job",
  notebook: "Notebook",
  pipeline: "Pipeline",
  "ad-hoc, text hidden": "Ad-hoc, text hidden",
};
function topCostGroupLabel(kind: any) { return TOP_COST_GROUP_LABEL[kind] || kind || "(unknown)"; }

// The group's own identity cell when it has no real source (a "text hash" group with no captured
// query_source, or the "ad-hoc" bucket): a job resolves through the name store (Ref); every other
// kind has no dim to resolve against, so it shows its raw id -- shortened, except the ad-hoc
// bucket's id, which is already a short masked identity (DEC-66.3), not a system id worth
// truncating.
function topCostGroupCell(row: any) {
  if (row.group_kind === "job") {
    return <Ref kind="job" id={row.group_id} workspaceId={row.workspace_id} />;
  }
  if (row.group_kind === "ad-hoc, text hidden") {
    return <span className="mono" title={row.group_id}>{row.group_id}</span>;
  }
  return <span className="mono" title={row.group_id}>{shortId(row.group_id)}</span>;
}

// query_top_by_cost's own source_kind values -> the plain word this page shows for each -- a
// group's REAL origin, shown even when group_kind is "text hash" (grouped by statement text, not
// by source).
const SOURCE_KIND_LABEL: Record<string, string> = {
  "saved query": "Saved query", dashboard: "Dashboard", "Genie space": "Genie space",
  alert: "Alert", job: "Job", notebook: "Notebook", pipeline: "Pipeline",
};
function sourceCell(kind: any, id: any, workspaceId: any) {
  const label = SOURCE_KIND_LABEL[kind] || kind;
  if (kind === "job" || kind === "notebook" || kind === "pipeline") {
    return <React.Fragment>{`${label}: `}<Ref kind={kind} id={id} workspaceId={workspaceId} /></React.Fragment>;
  }
  return <React.Fragment>{`${label}: `}<span className="mono" title={id}>{shortId(id)}</span></React.Fragment>;
}
// A group's identity cell: named by its real source when one was captured (dashboard/Genie
// space/alert/job/notebook/saved query/pipeline), else by how it was grouped (a text hash or the
// ad-hoc bucket).
function heavyGroupCell(row: any) {
  if (row.source_kind && row.source_id != null && row.source_id !== "") {
    return sourceCell(row.source_kind, row.source_id, row.workspace_id);
  }
  return <React.Fragment>{`${topCostGroupLabel(row.group_kind)}: `}{topCostGroupCell(row)}</React.Fragment>;
}

// STATUS_RANK is shared with tab_jobs.tsx

// One row per group_id across every warehouse it ran on (query_top_by_cost's own grain is per
// warehouse, so the same job or repeated statement running on two warehouses used to show up
// twice, e.g. the same hash at $264 on one warehouse and $40 on another). Sums $ and runs, lists
// the warehouses, and carries the group's real source, its latest run as the sample, and its
// duration spread forward.
function rollupHeavyGroups(rows: any) {
  const byGroup = new Map();
  (rows || []).forEach((r: any) => {
    // A job/notebook/saved-query id is only unique WITHIN its workspace -- without workspace_id in
    // the key, the same id in two workspaces merges into one row under one name. A repeated
    // statement's text hash is content-addressed and meant to roll up across workspaces, so it
    // keeps the old, workspace-less key.
    const key = r.group_kind === "text hash"
      ? `${r.group_kind}\u0000${r.group_id}\u0000${r.statement_type}`
      : `${r.group_kind}\u0000${r.workspace_id}\u0000${r.group_id}\u0000${r.statement_type}`;
    if (!byGroup.has(key)) {
      byGroup.set(key, {
        group_kind: r.group_kind, group_id: r.group_id, statement_type: r.statement_type,
        est_cost_usd_list: 0, runs: 0, distinct_users: 0, topUser: null, topUserRuns: -1,
        sample: null, p50Sum: 0, p50Wt: 0, p95Sum: 0, p95Wt: 0,
        worst_status: null, warehouses: new Map(), read_files: null, pruned_files: 0, read_bytes: 0,
      });
    }
    const g = byGroup.get(key);
    g.est_cost_usd_list += numOrZero(r.est_cost_usd_list);
    g.runs += numOrZero(r.runs);
    // An export from before the file counts has no read_files at all: "-", never 0 of 0.
    if (r.read_files != null) g.read_files = numOrZero(g.read_files) + numOrZero(r.read_files);
    g.pruned_files += numOrZero(r.pruned_files);
    g.read_bytes += numOrZero(r.read_bytes);
    g.distinct_users = Math.max(g.distinct_users, numOrZero(r.distinct_users));
    if (r.warehouse_id && !g.warehouses.has(r.warehouse_id)) g.warehouses.set(r.warehouse_id, r.workspace_id);
    if (numOrZero(r.runs) > g.topUserRuns) { g.topUserRuns = numOrZero(r.runs); g.topUser = r.top_user; }
    if (!g.sample || String(r.last_seen || "") > String(g.sample.last_seen || "")) g.sample = r;
    if (r.p50_duration_ms != null) { g.p50Sum += r.p50_duration_ms * numOrZero(r.runs); g.p50Wt += numOrZero(r.runs); }
    if (r.p95_duration_ms != null) { g.p95Sum += r.p95_duration_ms * numOrZero(r.runs); g.p95Wt += numOrZero(r.runs); }
    if (r.status && (g.worst_status === null || STATUS_RANK[r.status] < STATUS_RANK[g.worst_status])) g.worst_status = r.status;
  });
  return [...byGroup.values()].map((g) => ({
    group_kind: g.group_kind, group_id: g.group_id, statement_type: g.statement_type,
    est_cost_usd_list: g.est_cost_usd_list, runs: g.runs, distinct_users: g.distinct_users,
    top_user: g.topUser, status: g.worst_status,
    source_kind: g.sample ? g.sample.source_kind : null, source_id: g.sample ? g.sample.source_id : null,
    sample_statement_id: g.sample ? g.sample.sample_statement_id : null,
    workspace_id: g.sample ? g.sample.workspace_id : ([...g.warehouses.entries()][0] || [])[1],
    warehouseIds: [...g.warehouses.entries()],
    p50_duration_ms: g.p50Wt > 0 ? g.p50Sum / g.p50Wt : null,
    p95_duration_ms: g.p95Wt > 0 ? g.p95Sum / g.p95Wt : null,
    read_files: g.read_files, pruned_files: g.pruned_files, read_bytes: g.read_bytes,
  })).sort((a, b) => b.est_cost_usd_list - a.est_cost_usd_list);
}

// Files the group read out of every file its filters could have skipped. A small table read whole
// is fine, so only a group that read 10 GB+ with almost nothing skipped is marked.
const PRUNE_MIN_BYTES = 10e9;
function FilesReadCell({ row }: LooseProps) {
  if (row.read_files == null) return <span className="muted">-</span>;
  const all = numOrZero(row.read_files) + numOrZero(row.pruned_files);
  if (!all) return <span className="muted" title="No table files read: metadata, cache or no table">none</span>;
  const share = numOrZero(row.read_files) / all;
  const poor = share >= 0.95 && numOrZero(row.read_bytes) >= PRUNE_MIN_BYTES;
  return (
    <span className={`files-read${poor ? " poor" : ""}`} title={`${fmtInt(row.read_files)} of ${fmtInt(all)} files read, ${fmtInt(row.pruned_files)} skipped by pruning; ${fmtGb(numOrZero(row.read_bytes) / 1e9, 1)} read`}>
      <span className="mono">{`${fmtInt(row.read_files)} of ${fmtInt(all)}`}</span>
      <i className="files-read-bar"><i style={{ width: `${Math.max(2, share * 100)}%` }} /></i>
    </span>
  );
}

// Up to 3 warehouse names, comma-joined, "+N more" beyond that -- a group can span many
// warehouses and this cell is not the place to list them all.
function WarehouseList({ ids, max }: LooseProps) {
  const cap = max || 3;
  const shown = ids.slice(0, cap);
  return (
    <React.Fragment>
      {shown.map(([whId, wsId]: any, i: any) => (
        <React.Fragment key={whId}>
          {i > 0 && ", "}
          <Ref kind="warehouse" id={whId} workspaceId={wsId} />
        </React.Fragment>
      ))}
      {ids.length > cap && <span className="muted">{` +${ids.length - cap} more`}</span>}
    </React.Fragment>
  );
}

// Fallback view for an export made before query_top_by_cost existed: ranks the same
// query_costly_statements_grouped rows the checks table already lists, by total execution time
// (it carries no $ column) -- so an older export's Heavy queries page shows something real
// instead of a wall of "Not assessed" boxes.
// The check has one row per workspace + shape; a shape used in several workspaces is one shape here.
// Warehouses belong to one workspace, so their counts add up across workspaces.
function rollupShapes(rows: any) {
  const sevRank: Record<string, number> = { CRITICAL: 0, WARN: 1, NOT_ASSESSED: 2, OK: 3 };
  const byShape = new Map();
  rows.forEach((r: any) => {
    const cur = byShape.get(r.statement_fingerprint);
    if (!cur) { byShape.set(r.statement_fingerprint, { ...r }); return; }
    cur.runs = numOrZero(cur.runs) + numOrZero(r.runs);
    cur.total_exec_ms = numOrZero(cur.total_exec_ms) + numOrZero(r.total_exec_ms);
    cur.distinct_warehouses = numOrZero(cur.distinct_warehouses) + numOrZero(r.distinct_warehouses);
    if ((sevRank[r.status] ?? 9) < (sevRank[cur.status] ?? 9)) cur.status = r.status;
  });
  return [...byShape.values()];
}

function QueryHeavyFallback({ rows: workspaceRows, maxCat, onVerdict }: LooseProps) {
  const rows = rollupShapes(workspaceRows);
  const sorted = [...rows].sort((a, b) => numOrZero(b.total_exec_ms) - numOrZero(a.total_exec_ms));
  const flagged = rows.filter((r) => r.status === "CRITICAL" || r.status === "WARN");
  const worst = sorted[0] || null;

  const verdictSentence = `${fmtInt(rows.length)} statement shape${rows.length === 1 ? "" : "s"} over the last statements window${worst ? `; heaviest: ${fmtDuration(numOrZero(worst.total_exec_ms) / 1000)} total across ${fmtInt(worst.runs)} runs` : ""}.`;
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="metric-note" style={{ marginBottom: 10 }}>
        query_top_by_cost isn&apos;t on this export yet -- ranking by total execution time instead.
      </div>
      <div className="pqgs-cards-grid">
        <Card title="Statement shapes flagged">
          <div className={`pqgs-card-value ${flagged.length ? "crit" : "ok"}`}>{fmtInt(flagged.length)}</div>
          <PqgsNote facts={[{ label: "Scope", value: `${fmtInt(rows.length)} shape${rows.length === 1 ? "" : "s"}`, detail: "seen this window" }]} />
        </Card>
        <Card title="Heaviest shape">
          {worst ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtDuration(numOrZero(worst.total_exec_ms) / 1000)}</div>
              <PqgsNote facts={[
                { label: "Type", value: worst.statement_type || "SQL" },
                { label: "Runs", value: fmtInt(worst.runs) },
                { label: "Warehouses", value: fmtInt(worst.distinct_warehouses) },
              ]} />
            </React.Fragment>
          ) : <span className="muted">None recorded</span>}
        </Card>
      </div>
      <Card title="Heaviest statement shapes, worst first" right={<span className="muted">by total execution time</span>}>
        <HBarList
          items={sorted.slice(0, maxCat).map((r) => ({ name: `${r.statement_type || "SQL"} · ${shortId(r.statement_fingerprint)}`, value: numOrZero(r.total_exec_ms) }))}
          valueFmt={(v) => fmtDuration(v / 1000)}
        />
      </Card>
    </div>
  );
}

function QueryHeavyContent({ findings, filters, dims, maxCat, onExternalJump, onVerdict }: LooseProps) {
  const topState = useFindingData("query_top_by_cost", filters.window, filters.workspaceIds, filters.envs);
  const provState = useFindingData("query_provenance_by_source", filters.window, filters.workspaceIds, filters.envs);
  const selfState = useFindingData("cost_audit_self_usage", filters.window, filters.workspaceIds, filters.envs);
  const grpState = useFindingData("query_costly_statements_grouped", filters.window, filters.workspaceIds, filters.envs);
  const top = rowsOf(topState);
  const prov = rowsOf(provState);
  const typeAgg = useFindingAgg("query_top_by_cost", filters.window, filters.workspaceIds, filters.envs, ["statement_type"], "sum", "est_cost_usd_list");
  const typeRunsAgg = useFindingAgg("query_top_by_cost", filters.window, filters.workspaceIds, filters.envs, ["statement_type"], "sum", "runs");

  // One row per group_id across every warehouse it ran on (rule K19/T6/T10): the same job or
  // repeated statement on two warehouses is one row here, not two.
  const rolled = top ? rollupHeavyGroups(top) : null;
  const topTotal = top ? sumBy(top, "est_cost_usd_list") : null;
  const worst = rolled && rolled.length ? rolled[0] : null;

  // Counted on the client from the rolled groups, never query_top_by_cost's own `affected` --
  // that field's entity column is warehouse_id, so it would count warehouses, not groups.
  const groupsOverThreshold = rolled
    ? { flagged: rolled.filter((g) => g.status === "CRITICAL" || g.status === "WARN").length, total: rolled.length }
    : null;
  // The materiality floor is per 30 days and scales with the window; at 30 days or more it covers the share rule.
  const topData = topState.phase === "ready" ? topState.data : null;
  const topFloor = topData && topData.floor ? topData.floor : null;
  const floorUsd = topData && topFloor ? topFloor.min * numOrZero(topData.window_days || filters.window) / 30 : 0;
  const thresholdNote = floorUsd >= 50 ? `over ${fmtMoney(floorUsd, 0)} of warehouse spend`
    : floorUsd > 0 ? `over $50, or 20% of a warehouse's spend and over ${fmtMoney(floorUsd, 0)}`
    : "over $50 or 20% of warehouse spend";

  const provByType = prov ? groupSum(prov, (r) => r.source_kind || "unknown", "query_count") : null;
  const provTotal = provByType ? provByType.reduce((s, e) => s + e.value, 0) : 0;
  const sourceSegments = provByType ? topNWithOther(provByType, maxCat, "Other sources")
    .map((e, i) => ({ label: e.name, value: e.value, display: fmtInt(e.value), color: e.color || paletteColor(i) })) : null;

  const tableRows = rolled ? rolled.slice(0, maxCat) : null;

  // Each warehouse's own bill split across its statements, so an XS and a 5XL weigh by what they cost.
  const typeReady = typeAgg.phase === "ready" && typeAgg.outcome === "ok_rows"
    && typeRunsAgg.phase === "ready" && typeRunsAgg.outcome === "ok_rows";
  const typeTotal = typeReady ? numOrZero(typeAgg.data.total_value) : 0;
  const runsByType = new Map(typeReady ? (typeRunsAgg.data.groups || []).map((g) => [g.key[0], numOrZero(g.value)]) : []);
  const typeRows = typeReady ? (typeAgg.data.groups || [])
    .map((g) => ({ type: g.key[0] || "UNKNOWN", usd: numOrZero(g.value), runs: runsByType.get(g.key[0]) || 0 }))
    .sort((a, b) => b.usd - a.usd) : null;

  // No query_top_by_cost on this export at all (not just still loading) -- fall back to the
  // statement-shape grouping every export carries, rather than an empty page. QueryHeavyFallback
  // reports its own verdict, so the effect below is skipped while it is the one on screen.
  const grp = topState.phase === "ready" && topState.outcome !== "ok_rows" ? rowsOf(grpState) : null;
  const usingFallback = !!grp;

  // Gated on topState.phase, not just `top`: a build with no query_top_by_cost model settles
  // into a non-ok_rows outcome that never changes again, so a verdict gated on `top` alone would
  // read "Loading heavy queries..." forever instead of ever reaching this real sentence. The worst
  // group named here always comes from query_top_by_cost alone, never another check's own $.
  const worstLabel = worst
    ? (worst.source_kind ? `${SOURCE_KIND_LABEL[worst.source_kind] || worst.source_kind} ${worst.source_id}` : topCostGroupLabel(worst.group_kind))
    : null;
  const verdictSentence = topState.phase !== "ready"
    ? "Loading heavy queries..."
    : top
      ? `${fmtMoney(topTotal, 0)} across ${fmtInt(rolled!.length)} query group${rolled!.length === 1 ? "" : "s"} over the last ${filters.window} days${worst ? `; worst: ${worstLabel}, ${fmtMoney(worst.est_cost_usd_list, 0)} across ${fmtInt(worst.warehouseIds.length)} warehouse${worst.warehouseIds.length === 1 ? "" : "s"}` : ""}.`
      : "Query cost isn't in this export yet: re-run the export.";
  React.useEffect(() => { if (!usingFallback && onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict, usingFallback]);

  if (usingFallback) return <QueryHeavyFallback rows={grp} maxCat={maxCat} onVerdict={onVerdict} />;

  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Query groups over threshold">
          {top ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${groupsOverThreshold!.flagged ? "warn" : "ok"}`}>{`${fmtInt(groupsOverThreshold!.flagged)} of ${fmtInt(groupsOverThreshold!.total)}`}</div>
              <PqgsNote facts={[{ label: "Scope", value: "groups", detail: thresholdNote }]} />
            </React.Fragment>
          ) : <ChartNote state={topState} label="query_top_by_cost" />}
        </Card>
        <Card title="Worst query group">
          {worst ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtMoney(worst.est_cost_usd_list, 0)}</div>
              <PqgsNote facts={[
                { label: "Group", value: worstLabel },
                { label: "Warehouses", value: fmtInt(worst.warehouseIds.length) },
                { label: "p50 / p95", value: `${fmtDurationMs(worst.p50_duration_ms)} / ${fmtDurationMs(worst.p95_duration_ms)}`, detail: `${fmtInt(worst.runs)} runs` },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={topState} label="query_top_by_cost" />}
        </Card>
        <Card title="This audit's own query cost">
          {selfState.phase === "ready" && selfState.outcome === "ok_rows" ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtMoney(sumBy(selfState.data.rows, "est_audit_usd_list"), 0)}</div>
              <PqgsNote facts={[
                { label: "Statements", value: fmtInt(sumBy(selfState.data.rows, "matched_statement_count")) },
                { label: "Run time", value: fmtDuration(sumBy(selfState.data.rows, "matched_duration_secs")) },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={selfState} label="cost_audit_self_usage" />}
        </Card>
      </div>
      <div className="grid-2">
      <div className="chart-card">
        <div className="chart-card-title">Traffic by source</div>
        {sourceSegments
          ? <Donut segments={sourceSegments} centerLabel={fmtInt(provTotal)} centerSub="statements" />
          : <ChartNote state={provState} label="query_provenance_by_source" />}
      </div>
      <div className="card">
        <div className="card-head">
          <div className="card-title">Warehouse spend by statement type</div>
          <span className="muted">each warehouse's bill split by run time</span>
        </div>
        {typeRows && typeRows.length ? (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr><th>Statement type</th><th className="num">Est. $</th><th className="num">Share</th><th className="num">Runs</th></tr>
              </thead>
              <tbody>
                {typeRows.slice(0, 12).map((r) => (
                  <tr key={String(r.type)}>
                    <td>{r.type}</td>
                    <td className="num mono">{fmtMoney(r.usd, 0)}</td>
                    <td className="num mono">{typeTotal > 0 ? fmtPct((r.usd / typeTotal) * 100, 0) : "-"}</td>
                    <td className="num mono">{fmtInt(r.runs)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : typeRows ? <div className="muted">No SQL-warehouse statements this window.</div>
          : <ChartNote state={typeAgg} label="query_top_by_cost" />}
      </div>
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <div className="card-title">Top queries by $</div>
          <span className="muted">worst first · files read: amber when a group read 10 GB+ and its filters skipped under 5% of files</span>
        </div>
        {tableRows && tableRows.length === 0 ? (
          <div className="muted">No SQL-warehouse query group cost $50 or more, or 20% of its warehouse, this window.</div>
        ) : tableRows ? (
          <React.Fragment>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Group</th><th>Warehouses</th><th className="num">$</th>
                    <th className="num">Runs</th><th className="num">p50 / p95</th><th title="Files read out of all the files its filters could have skipped">Files read</th><th>Top user</th>
                    <th>Status</th><th>Latest run</th>
                  </tr>
                </thead>
                <tbody>
                  {tableRows.map((r, i) => (
                    <tr key={`${r.group_kind}-${r.group_id}-${r.statement_type}-${i}`}>
                      <td>{`${r.statement_type ? `${r.statement_type}: ` : ""}`}{heavyGroupCell(r)}</td>
                      <td><WarehouseList ids={r.warehouseIds} max={1} /></td>
                      <td className="num mono">{fmtMoney(r.est_cost_usd_list, 0)}</td>
                      <td className="num mono">{fmtInt(r.runs)}</td>
                      <td className="num mono">{`${fmtDurationMs(r.p50_duration_ms)} / ${fmtDurationMs(r.p95_duration_ms)}`}</td>
                      <td><FilesReadCell row={r} /></td>
                      <td className="mono">{r.top_user || "-"}</td>
                      <td>{r.status ? <StatusPill kind={bandPillKind(r.status)} /> : "-"}</td>
                      <td><DbxLink kind="query" workspaceId={r.workspace_id} id={r.sample_statement_id} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {rolled!.length > tableRows.length && (
              <div className="muted" style={{ marginTop: 8 }}>{`Top ${fmtInt(tableRows.length)} of ${fmtInt(rolled!.length)} groups by cost this window.`}</div>
            )}
          </React.Fragment>
        ) : <ChartNote state={topState} label="query_top_by_cost" />}
      </div>
      <div className="chart-note ok" style={{ marginTop: 14 }}>
        <div className="chart-note-text">
          {"Statements with readable text roll up by fingerprint hash instead, run by run. "}
          <button type="button" className="pqgs-inline-link" onClick={() => onExternalJump("query_costly_statements_grouped")}>Costliest statement shapes →</button>
        </div>
      </div>
    </div>
  );
}

// ─────────── Efficiency ───────────
// Fix text for exactly these 4 checks -- short and specific, not derived from the Guide's own
// longer field-heuristic prose (kept there for the Guide page, not repeated here).
const EFF_FIX: Record<string, string> = {
  query_local_spillage: "Filter or aggregate before the big joins, or use a larger warehouse.",
  query_shuffle_write_amplification: "Check join and group-by keys for skew.",
  query_cache_coldstart: "Keep these tables on a warehouse that stays up.",
  query_pruning_effectiveness: "Filter on clustering columns; OPTIMIZE or cluster the tables.",
};

const EFF_TOP = 3;

function ProblemCard({ rank, title, band, worst, emptyText, fix, onOpen }: LooseProps) {
  return (
    <div className="card pqgs-problem">
      <div className="pqgs-problem-head">
        <span className={`pqgs-problem-rank ${rank === 1 ? "top" : ""}`}>{rank}</span>
        <div className="card-title" style={{ flex: 1 }}>{title}</div>
        <StatusPill kind={bandPillKind(band)} />
      </div>
      {worst.length ? (
        <div className="pqgs-problem-list">
          {worst.map((w: any) => (
            <React.Fragment key={w.warehouse_id}>
              <span className="pqgs-problem-list-name" title={w.label}>{w.label}</span>
              <span className="pqgs-problem-list-value mono">{w.value}</span>
            </React.Fragment>
          ))}
        </div>
      ) : <div className="muted">{emptyText}</div>}
      <div className="pqgs-problem-fix">{fix}</div>
      <button type="button" className="pqgs-problem-link" onClick={onOpen}>Open check →</button>
    </div>
  );
}

// Why a problem card lists no warehouse: still loading, not run, or flagged only off-warehouse.
function effEmptyText(state: any, band: any) {
  if (!state || state.phase !== "ready") return "Loading...";
  if (state.outcome === "not_assessed") return notAssessedText(state.data);
  const rows = rowsOf(state) || [];
  const flagged = rows.filter((r) => r.status === "CRITICAL" || r.status === "WARN");
  if ((band === "CRITICAL" || band === "WARN") && flagged.length && flagged.every((r) => r.warehouse_id == null)) {
    return "Flagged on serverless notebooks and jobs, which have no warehouse.";
  }
  return "No warehouse affected this window.";
}

function perWarehouseEfficiency(spill: any, shuffle: any, prune: any, cache: any, dims: any) {
  const ids = new Set<string>();
  [spill, shuffle, prune, cache].forEach((rows) => (rows || []).forEach((r: any) => { if (r.warehouse_id) ids.add(r.warehouse_id); }));
  return [...ids].map((whId) => {
    const s = (spill || []).filter((r: any) => r.warehouse_id === whId);
    const sh = (shuffle || []).filter((r: any) => r.warehouse_id === whId);
    const p = (prune || []).filter((r: any) => r.warehouse_id === whId);
    const c = (cache || []).filter((r: any) => r.warehouse_id === whId && r.read_io_cache_percent_avg != null);
    const spillBytes = sumBy(s, "spilled_local_bytes_sum");
    const spillDays = s.filter((r: any) => numOrZero(r.spilled_local_bytes_sum) > 0).length;
    const shuffleBytes = sumBy(sh, "shuffle_read_bytes_sum");
    const pruned = sumBy(p, "pruned_files_sum");
    const read = sumBy(p, "read_files_sum");
    const prunePct = pruned + read > 0 ? (pruned / (pruned + read)) * 100 : null;
    const scans = sumBy(c, "scanned_query_count");
    // weighted by each day's own scan count, not a plain average of daily averages
    const cachePct = scans > 0
      ? c.reduce((s: any, r: any) => s + r.read_io_cache_percent_avg * numOrZero(r.scanned_query_count), 0) / scans
      : null;
    const first = [...s, ...sh, ...p, ...c][0] || {};
    return {
      warehouse_id: whId, workspace_id: first.workspace_id, name: warehouseName(dims, whId),
      spillBytes, spillDays, shuffleBytes, prunePct, cachePct,
      // volume behind each percentage, so "worst" means the most files or scans affected
      filesRead: read, cacheMissScans: cachePct != null ? scans * (1 - cachePct / 100) : 0, scans,
    };
  }).sort((a, b) => b.spillBytes - a.spillBytes);
}

function QueryEfficiencyContent({ findings, filters, dims, onExternalJump, onVerdict }: LooseProps) {
  const spillState = useFindingData("query_local_spillage", filters.window, filters.workspaceIds, filters.envs);
  const shuffleState = useFindingData("query_shuffle_write_amplification", filters.window, filters.workspaceIds, filters.envs);
  const pruneState = useFindingData("query_pruning_effectiveness", filters.window, filters.workspaceIds, filters.envs);
  const cacheState = useFindingData("query_cache_coldstart", filters.window, filters.workspaceIds, filters.envs);
  const spill = rowsOf(spillState), shuffle = rowsOf(shuffleState), prune = rowsOf(pruneState), cache = rowsOf(cacheState);

  const findingsById = React.useMemo(() => {
    const idx: Record<string, any> = {};
    (findings || []).forEach((f: any) => { idx[f.query_id] = f; });
    return idx;
  }, [findings]);

  // Serverless notebooks and jobs have no warehouse: never counted as one more warehouse.
  const whIds = (rows: any) => new Set(rows.filter((r: any) => r.warehouse_id != null).map((r: any) => r.warehouse_id));
  const spillWh = spill ? whIds(spill).size : null;
  const spillFlaggedWh = spill ? whIds(spill.filter((r) => numOrZero(r.spilled_local_bytes_sum) > 0)).size : null;
  const spillTotal = spill ? sumBy(spill, "spilled_local_bytes_sum") : null;
  const spillNoWh = spill ? sumBy(spill.filter((r) => r.warehouse_id == null), "spilled_local_bytes_sum") : 0;

  const shuffleWh = shuffle ? whIds(shuffle).size : null;
  const shuffleTotal = shuffle ? sumBy(shuffle, "shuffle_read_bytes_sum") : null;

  // weighted by each row's own scan count (scanned_query_count), not a plain average of daily
  // averages -- a warehouse with 10,000 scans counts 10,000x more than one with 1.
  const cacheRows = cache ? cache.filter((r) => r.read_io_cache_percent_avg != null) : null;
  const cacheScans = cacheRows ? sumBy(cacheRows, "scanned_query_count") : 0;
  const cacheAvgPct = cacheRows && cacheScans > 0
    ? cacheRows.reduce((s, r) => s + r.read_io_cache_percent_avg * numOrZero(r.scanned_query_count), 0) / cacheScans
    : null;
  const cacheWh = cache ? whIds(cache).size : null;

  const prunePruned = prune ? sumBy(prune, "pruned_files_sum") : null;
  const pruneRead = prune ? sumBy(prune, "read_files_sum") : null;
  const prunePct = prune && (prunePruned! + pruneRead!) > 0 ? (prunePruned! / (prunePruned! + pruneRead!)) * 100 : null;
  const pruneWh = prune ? whIds(prune).size : null;

  const kpis = [
    { label: "Spilled to local disk", value: fmtGb(spillTotal, 1, true), facts: spillFlaggedWh != null ? [
      { label: "Affected", value: `${fmtInt(spillFlaggedWh)} of ${fmtInt(spillWh)}`, detail: "warehouses spilling" },
      spillNoWh > 0 ? { label: "No warehouse", value: fmtGb(spillNoWh, 1, true), detail: "serverless notebooks and jobs" } : null,
    ] : null },
    { label: "Shuffled between nodes", value: fmtGb(shuffleTotal, 1, true), facts: shuffleWh != null ? [{ label: "Warehouses", value: fmtInt(shuffleWh) }] : null },
    { label: "Read from disk cache", value: cacheAvgPct != null ? fmtPct(cacheAvgPct, 0) : "-", facts: cacheWh != null ? [{ label: "Across", value: `${fmtInt(cacheWh)} warehouses` }] : null },
    { label: "Files skipped by pruning", value: prunePct != null ? fmtPct(prunePct, 0) : "-", facts: pruneWh != null ? [{ label: "Across", value: `${fmtInt(pruneWh)} warehouses` }] : null },
  ];

  const whRows: Row[] = perWarehouseEfficiency(spill, shuffle, prune, cache, dims);
  // Worst = most bytes, files or scans affected, never a tiny warehouse's extreme percentage.
  const worstWh = (key: any, fmtValue: any) => whRows
    .filter((w) => numOrZero(w[key]) > 0)
    .sort((a, b) => b[key] - a[key])
    .slice(0, EFF_TOP)
    .map((w) => {
      const ws = resolveName("workspace", w.workspace_id, w.workspace_id);
      // A shared warehouse name already carries its workspace ("Serverless Starter Warehouse · ws").
      return { warehouse_id: w.warehouse_id, label: String(w.name).endsWith(` · ${ws}`) ? w.name : `${w.name} · ${ws}`, value: fmtValue(w) };
    });

  const label = (id: any) => getCheckLabel(id, (findingsById[id] && findingsById[id].title) || id);
  const bandFor = (id: any) => (findingsById[id] ? bandOf(findingsById[id]) : "NOT_ASSESSED");

  const effStates: Record<string, any> = { query_local_spillage: spillState, query_shuffle_write_amplification: shuffleState, query_cache_coldstart: cacheState, query_pruning_effectiveness: pruneState };
  const problems = [
    { id: "query_local_spillage", worst: worstWh("spillBytes", (w: any) => fmtGb(w.spillBytes, 1, true)) },
    { id: "query_shuffle_write_amplification", worst: worstWh("shuffleBytes", (w: any) => fmtGb(w.shuffleBytes, 1, true)) },
    { id: "query_cache_coldstart", worst: worstWh("cacheMissScans", (w: any) => `${fmtPct(100 - w.cachePct, 0)} missed of ${fmtInt(w.scans)} scans`) },
    { id: "query_pruning_effectiveness", worst: worstWh("filesRead", (w: any) => `${fmtPct(w.prunePct, 0)} skipped of ${fmtInt(w.filesRead)} files`) },
  ].map((p) => ({ ...p, title: label(p.id).title, band: bandFor(p.id), fix: EFF_FIX[p.id], emptyText: effEmptyText(effStates[p.id], bandFor(p.id)) }));

  const sevRank: Record<string, number> = { CRITICAL: 0, WARN: 1, RANKED: 2, OK: 3, NOT_ASSESSED: 4 };
  problems.sort((a, b) => (sevRank[a.band] ?? 9) - (sevRank[b.band] ?? 9));

  const whShown = whRows.slice(0, 8);

  const worstProblem = problems.find((p) => p.band === "CRITICAL" || p.band === "WARN");
  const verdictSentence = spill || shuffle || prune || cache
    ? worstProblem
      ? `Worst: ${worstProblem.title}${worstProblem.worst.length ? `, ${worstProblem.worst[0].value} on ${worstProblem.worst[0].label}` : ""}.`
      : "No efficiency problem flagged this window."
    : "Loading query efficiency...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="pqgs-cards-grid">
        {kpis.map((k) => (
          <Card key={k.label} title={k.label}>
            <div className="pqgs-card-value">{k.value}</div>
            {k.facts && <PqgsNote facts={k.facts} />}
          </Card>
        ))}
      </div>
      <div className="section-title">Efficiency problems, biggest first</div>
      <div className="pqgs-problem-grid">
        {problems.map((p, i) => (
          <ProblemCard
            key={p.id}
            rank={i + 1}
            title={p.title}
            band={p.band}
            worst={p.worst}
            emptyText={p.emptyText}
            fix={p.fix}
            onOpen={() => onExternalJump(p.id)}
          />
        ))}
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <div className="card-title">Efficiency by warehouse</div>
          <span className="muted">the worst figure per warehouse, this window</span>
        </div>
        {whShown.length === 0 ? (
          <div className="muted">No warehouse ran a statement these four checks could judge in this window.</div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Warehouse</th><th className="num">Spilled to disk</th><th className="num">Shuffled</th>
                  <th className="num">From disk cache</th><th className="num">Files skipped</th>
                </tr>
              </thead>
              <tbody>
                {whShown.map((w) => (
                  <tr key={w.warehouse_id}>
                    <td><Ref kind="warehouse" id={w.warehouse_id} workspaceId={w.workspace_id} /><div className="muted">{resolveName("workspace", w.workspace_id, w.workspace_id)}</div></td>
                    <td className="num">{w.spillBytes > 0 ? `${fmtGb(w.spillBytes, 1, true)} (${w.spillDays}d)` : "0 GB"}</td>
                    <td className="num">{fmtGb(w.shuffleBytes, 1, true)}</td>
                    <td className="num">{w.cachePct != null ? fmtPct(w.cachePct, 0) : "-"}</td>
                    <td className="num">{w.prunePct != null ? fmtPct(w.prunePct, 0) : "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {whRows.length > whShown.length && (
          <div className="muted" style={{ marginTop: 8 }}>{`Top ${fmtInt(whShown.length)} of ${fmtInt(whRows.length)} warehouses by spill; the rest are in the checks table below.`}</div>
        )}
      </div>
      <div className="chart-note ok" style={{ marginTop: 14 }}>
        <div className="chart-note-text">
          {"Looking for the exact statements? These checks judge a warehouse's day, not single statements. "}
          <button type="button" className="pqgs-inline-link" onClick={() => onExternalJump("query_costly_statements_grouped")}>Heavy queries</button>
          {" groups statements into shapes: the same statement run many times, with only its values changing."}
        </div>
      </div>
    </div>
  );
}

// ─────────── Reliability ───────────
// query_failed_statements_grouped's own source_kind -> a short label, same words as Heavy's own
// SOURCE_KIND_LABEL (top_cost group_kind is a different vocabulary, so a separate map here).
const ERROR_SOURCE_LABEL: Record<string, string> = {
  "saved query": "Saved query", dashboard: "Dashboard", "Genie space": "Genie space",
  alert: "Alert", job: "Job", notebook: "Notebook", pipeline: "Pipeline",
};
function errorSourceCell(row: any) {
  if (!row.source_kind || row.source_id == null || row.source_id === "") return <span className="muted">Ad-hoc</span>;
  const label = ERROR_SOURCE_LABEL[row.source_kind] || row.source_kind;
  if (row.source_kind === "job" || row.source_kind === "notebook" || row.source_kind === "pipeline") {
    return <React.Fragment>{`${label}: `}<Ref kind={row.source_kind} id={row.source_id} workspaceId={row.workspace_id} /></React.Fragment>;
  }
  return <React.Fragment>{`${label}: `}<span className="mono" title={row.source_id}>{shortId(row.source_id)}</span></React.Fragment>;
}

function QueryReliabilityContent({ filters, meta, onVerdict }: LooseProps) {
  const dims = useDims();
  const groupedState = useFindingData("query_failed_statements_grouped", filters.window, filters.workspaceIds, filters.envs);
  const failedState = useFindingData("query_failed_queries_daily", filters.window, filters.workspaceIds, filters.envs);
  const wasteState = useFindingData("cost_failed_statement_waste", filters.window, filters.workspaceIds, filters.envs);
  const grouped = rowsOf(groupedState);
  const failed = rowsOf(failedState);
  const waste = rowsOf(wasteState);

  const failedGroups = grouped ? grouped.filter((r) => r.execution_status === "FAILED") : null;
  const canceledGroups = grouped ? grouped.filter((r) => r.execution_status === "CANCELED") : null;
  const failedStatements = failedGroups ? failedGroups.reduce((s, r) => s + numOrZero(r.statements), 0) : null;
  const canceledStatements = canceledGroups ? canceledGroups.reduce((s, r) => s + numOrZero(r.statements), 0) : null;
  const topErrors = failedGroups ? [...failedGroups].sort((a, b) => numOrZero(b.statements) - numOrZero(a.statements)) : null;

  const nonFinished = failed ? failed.filter((r) => r.execution_status && r.execution_status !== "FINISHED") : null;
  const byDay = nonFinished ? filledDailySeries(dailySeries(nonFinished, "day", "query_count"), meta, filters.window) : null;
  const daysWithFailures = byDay ? byDay.values.filter((v) => v > 0).length : 0;
  const [failGrain, setFailGrain] = React.useState<Grain>("day");
  // Most failures reach system tables without their error text; say so instead of "read the error".
  const noTextStatements = failedGroups ? failedGroups.filter((r) => !r.error_message_sample).reduce((s, r) => s + numOrZero(r.statements), 0) : 0;

  // Fetched with no status filter: the total is every failed statement's own compute, and how
  // many of the warehouses behind it are flagged, never just the flagged subset's own total.
  const wasteTotal = waste ? sumBy(waste, "est_wasted_usd_list_disc") : null;
  const wasteFlagged = waste ? waste.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const wasteTop = waste && waste.length ? [...waste].sort((a, b) => numOrZero(b.est_wasted_usd_list_disc) - numOrZero(a.est_wasted_usd_list_disc))[0] : null;

  const verdictSentence = failedGroups
    ? `${fmtInt(failedStatements)} failed statement${failedStatements === 1 ? "" : "s"}${canceledStatements ? ` (${fmtInt(canceledStatements)} canceled, not counted)` : ""} over the last ${filters.window} days${waste ? `; ${fmtMoney(wasteTotal, 0)} of compute burned on failed statements` : ""}.`
    : "Loading query reliability...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Failed statements">
          {failedGroups ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(failedStatements)}</div>
              <PqgsNote facts={[
                { label: "Canceled", value: fmtInt(canceledStatements), tone: "muted", detail: "not counted as a failure" },
                { label: "Error classes", value: fmtInt(new Set(failedGroups.map((r) => r.error_class)).size) },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={groupedState} label="query_failed_statements_grouped" />}
        </Card>
        <Card title="Compute burned on failed statements">
          {waste ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtMoney(wasteTotal, 0)}</div>
              <PqgsNote facts={[
                { label: "Flagged", value: `${fmtInt(wasteFlagged!.length)} of ${fmtInt(waste.length)}`, tone: wasteFlagged!.length ? "crit" : "ok", detail: "warehouses" },
                {
                  label: "Worst",
                  value: wasteTop ? warehouseName(dims, wasteTop.warehouse_id) : "None",
                  tone: wasteTop ? undefined : "ok",
                  detail: wasteTop ? `${fmtInt(wasteTop.failed_statements)} failed statements` : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={wasteState} label="cost_failed_statement_waste" />}
        </Card>
      </div>
      <div className="chart-card">
        <div className="chart-card-title-row">
          <div className="chart-card-title">{`Failed or cancelled queries per ${failGrain}`}</div>
          <GrainSwitch value={failGrain} onChange={setFailGrain} />
        </div>
        {byDay && byDay.days.length > 1 && (
          <div className="cj-chart-takeaway">{`${fmtInt(daysWithFailures)} of the ${fmtInt(byDay.days.length)} days in this export had a failed or cancelled query.${byDay.days.length > DAY_ROWS_MAX && failGrain === "day" ? ` Latest ${DAY_ROWS_MAX} shown.` : ""}`}</div>
        )}
        {byDay ? (
          byDay.days.length > 1
            ? <GrainLines grain={failGrain} days={byDay.days} series={[{ label: "failed or cancelled", color: "var(--st-crit)", data: byDay.values }]} />
            : <div className="chart-note ok"><div className="chart-note-text">Only one day of data in this window -- not enough to draw a trend.</div></div>
        ) : <ChartNote state={failedState} label="query_failed_queries_daily" />}
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <div className="card-title">Top errors</div>
          <span className="muted">error class · count · source · warehouse · last seen UTC, worst first</span>
        </div>
        {noTextStatements > 0 && (
          <div className="cj-chart-takeaway">{`${fmtInt(noTextStatements)} of ${fmtInt(failedStatements)} failed statements have no error text in system tables. Open the Sample link, or Query history in Databricks filtered to Status: Failed, for the message.`}</div>
        )}
        {topErrors && topErrors.length === 0 ? (
          <div className="muted">No failed statements this window.</div>
        ) : topErrors ? (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Error class</th><th className="num">Count</th><th>Source</th><th>Warehouse</th>
                  <th>Last seen (UTC)</th><th>Status</th><th>Sample</th>
                </tr>
              </thead>
              <tbody>
                {topErrors.slice(0, 20).map((r) => (
                  <tr key={`${r.workspace_id}-${r.warehouse_id}-${r.error_class}-${r.source_kind}-${r.source_id}`}>
                    <td className="mono">{r.error_class}</td>
                    <td className="num mono">{fmtInt(r.statements)}</td>
                    <td>{errorSourceCell(r)}</td>
                    <td><Ref kind="warehouse" id={r.warehouse_id} workspaceId={r.workspace_id} /></td>
                    <td className="mono">{r.last_seen ? `${String(r.last_seen).slice(0, 16).replace("T", " ")} UTC` : "-"}</td>
                    <td><StatusPill kind={bandPillKind(r.status)} /></td>
                    <td><DbxLink kind="query" workspaceId={r.workspace_id} id={r.sample_statement_id} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <ChartNote state={groupedState} label="query_failed_statements_grouped" />}
      </div>
    </div>
  );
}

// ─────────── Capacity ───────────
// query_queuing_waits rows (day x warehouse) -> one entry per warehouse with both waits and a
// per-day series, worst warehouse (by slot wait) first.
function queueWaitsByWarehouse(rows: any) {
  const byWh = new Map();
  (rows || []).forEach((r: any) => {
    if (!r.warehouse_id) return;
    const key = `${r.workspace_id}:${r.warehouse_id}`;
    if (!byWh.has(key)) byWh.set(key, { warehouse_id: r.warehouse_id, workspace_id: r.workspace_id, slotS: 0, startS: 0, days: new Map() });
    const w = byWh.get(key);
    const slot = numOrZero(r.waiting_at_capacity_ms_sum) / 1000, start = numOrZero(r.waiting_for_compute_ms_sum) / 1000;
    w.slotS += slot; w.startS += start;
    const day = String(r.day).slice(0, 10);
    const d = w.days.get(day) || { slot: 0, start: 0 };
    w.days.set(day, { slot: d.slot + slot, start: d.start + start });
  });
  return [...byWh.values()].filter((w) => w.slotS + w.startS > 0)
    .sort((a, b) => (b.slotS - a.slotS) || (b.startS - a.startS));
}

function QueueByWarehouse({ rows, dims, maxCat, pressureByWarehouse, windowDays, startup }: LooseProps) {
  if (!rows || !rows.length) return null;
  const shown = rows.slice(0, maxCat || 8);
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-head">
        <div className="card-title">Queued time by warehouse</div>
        <span className="muted">slot wait first; by day in the grid above</span>
      </div>
      <div className="table-wrap">
        <table className="data">
          <thead><tr><th>Warehouse</th><th className="num">Waiting for a slot</th><th className="num">Waiting for start-up, added up</th>{startup && <><th className="num">By the clock</th><th className="num">Starts</th><th className="num">Longest start</th></>}<th>The fix</th></tr></thead>
          <tbody>
            {shown.map((w: any) => {
              const pressureRow = pressureByWarehouse ? pressureByWarehouse.get(w.warehouse_id) : null;
              const fix = maxClustersChangeLine(pressureRow, windowDays);
              const su = startup ? startup.byWh.get(`${w.workspace_id}:${w.warehouse_id}`) : null;
              return (
                <tr key={`${w.workspace_id}:${w.warehouse_id}`}>
                  <td><Ref kind="warehouse" id={w.warehouse_id} workspaceId={w.workspace_id} /><div className="muted">{resolveName("workspace", w.workspace_id, w.workspace_id)}</div></td>
                  <td className={`num mono ${w.slotS > 0 ? "tone-crit" : ""}`}>{fmtDuration(w.slotS)}</td>
                  <td className="num mono">{fmtDuration(w.startS)}</td>
                  {startup && <><td className="num mono">{su ? fmtDuration(su.clock) : "-"}</td><td className="num mono">{su && su.starts ? `${fmtInt(su.starts)}${su.failed ? ` (${fmtInt(su.failed)} failed)` : ""}` : "-"}</td><td className="num mono">{su && su.starts ? fmtDuration(su.longest) : "-"}</td></>}
                  <td className="muted">{fix || "-"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length > shown.length && <div className="muted" style={{ marginTop: 6 }}>{`Top ${fmtInt(shown.length)} of ${fmtInt(rows.length)} warehouses that queued.`}</div>}
    </div>
  );
}

function QueryCapacityContent({ filters, meta, dims, maxCat, onExternalJump, onVerdict }: LooseProps) {
  const pressureState = useFindingData("query_warehouse_pressure", filters.window, filters.workspaceIds, filters.envs);
  const queueState = useFindingData("query_queuing_waits", filters.window, filters.workspaceIds, filters.envs);
  const mixState = useFindingData("query_workload_mix_hours", filters.window, filters.workspaceIds, filters.envs);
  const startup = useStartupWaits(filters.window, filters.workspaceIds, filters.envs);
  const pressureRows = rowsOf(pressureState);
  const queueRows = rowsOf(queueState);
  const mixRows = rowsOf(mixState);

  const flagged = pressureRows ? pressureRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  // Pressure on a warehouse under the minimum-use floor is real but not flagged; say how many.
  const lightUse = pressureRows ? pressureRows.filter((r) => r.below_floor && r.pressure && r.pressure !== "NONE") : null;
  const notJudged = pressureRows ? pressureRows.filter((r) => r.pressure == null).length : 0;
  const floorLabel = pressureState.data && pressureState.data.floor ? pressureState.data.floor.label : null;
  const worst = flagged && flagged.length ? [...flagged].sort((a, b) =>
    (numOrZero(b.spill_time_pct) - numOrZero(a.spill_time_pct)) || (numOrZero(b.capacity_wait_pct) - numOrZero(a.capacity_wait_pct)))[0] : null;

  const pressureByWarehouse = React.useMemo(() => {
    if (!pressureRows) return null;
    const m = new Map();
    pressureRows.forEach((r) => m.set(r.warehouse_id, r));
    return m;
  }, [pressureRows]);

  // Two different waits: a slot (the warehouse was full -- the real queue) and start-up (the
  // warehouse was stopped or still provisioning).
  const slotS = queueRows ? sumBy(queueRows, "waiting_at_capacity_ms_sum") / 1000 : null;
  const startS = queueRows ? sumBy(queueRows, "waiting_for_compute_ms_sum") / 1000 : null;
  // queueByWh is already sorted slot-first, then start-up -- the same one sort the table below
  // reads, so a "worst" tile and the table's own top row can never disagree.
  const queueByWh = queueWaitsByWarehouse(queueRows);
  const worstSlot = queueByWh.length ? queueByWh[0] : null;
  const worstStart = queueByWh.length ? [...queueByWh].sort((a, b) => b.startS - a.startS)[0] : null;

  // Query history hours are UTC -- one clock, no viewer-local conversion.
  const byHour = mixRows ? groupSum(mixRows, (r) => r.hour_of_day, "query_count") : null;
  const pad2 = (h: any) => String(h).padStart(2, "0");
  const hourItems = byHour ? [...byHour]
    .map((e) => ({ utc: Number(e.name), value: e.value }))
    .sort((a, b) => a.utc - b.utc)
    .map((e) => ({ name: `${pad2(e.utc)}:00 UTC`, value: e.value })) : null;
  const peakHour = hourItems && hourItems.length ? [...hourItems].sort((a, b) => b.value - a.value)[0] : null;
  const hourTotal = hourItems ? hourItems.reduce((s, e) => s + e.value, 0) : null;

  const verdictSentence = pressureRows
    ? `${fmtInt(flagged!.length)} warehouse${flagged!.length === 1 ? "" : "s"} under pressure this window${worst ? `; worst: ${warehouseName(dims, worst.warehouse_id)}` : ""}`
      + `${lightUse!.length ? ` (${fmtInt(lightUse!.length)} more show pressure but ran too little to flag)` : ""}${queueRows ? `. Queued for a slot ${fmtDuration(slotS)}; waiting for start-up ${fmtDuration(startS)}${startup ? ` (${fmtDuration(startup.total.clock)} by the clock)` : ""}.` : "."}`
    : "Loading query capacity...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Warehouses under pressure">
          {pressureRows ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(flagged!.length)}</div>
              <PqgsNote facts={[
                {
                  label: "Worst",
                  value: worst ? warehouseName(dims, worst.warehouse_id) : "None flagged",
                  tone: worst ? undefined : "ok",
                  detail: worst ? `${worst.warehouse_size ? enumLabel(worst.warehouse_size) : "size unknown"} · ${pressureLeverLabel(worst) || "no lever recorded"}` : null,
                },
                lightUse!.length ? { label: "Too little use", value: fmtInt(lightUse!.length), detail: floorLabel ? `under ${floorLabel}` : "under the minimum-use floor" } : null,
              ].filter(Boolean)} />
            </React.Fragment>
          ) : <ChartNote state={pressureState} label="query_warehouse_pressure" />}
        </Card>
        <Card title="Queued for a slot">
          {queueRows ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${numOrZero(slotS) > 0 ? "crit" : "ok"}`}>{fmtDuration(slotS)}</div>
              <PqgsNote facts={[
                { label: "Worst", value: worstSlot && worstSlot.slotS > 0 ? warehouseName(dims, worstSlot.warehouse_id) : "None", detail: worstSlot && worstSlot.slotS > 0 ? fmtDuration(worstSlot.slotS) : null },
                { label: "Fix", value: "raise max clusters, or spread the load" },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={queueState} label="query_queuing_waits" />}
        </Card>
        <Card title="Waiting for start-up">
          {queueRows ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtDuration(startS)}</div>
              <PqgsNote facts={[
                ...(startup ? [
                  { label: "By the clock", value: fmtDuration(startup.total.clock), detail: "queries waiting together count once" },
                  { label: "Warehouses", value: startsText(startup.total) },
                ] : []),
                { label: "Worst", value: worstStart && worstStart.startS > 0 ? warehouseName(dims, worstStart.warehouse_id) : "None", detail: worstStart && worstStart.startS > 0 ? fmtDuration(worstStart.startS) : null },
                { label: "Fix", value: "classic or pro: longer auto-stop where starts are frequent; serverless: a long wait means a slow or failed start" },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={queueState} label="query_queuing_waits" />}
        </Card>
        <Card title="Workload peak hour">
          {peakHour ? (
            <React.Fragment>
              <div className="pqgs-card-value">{peakHour.name}</div>
              <PqgsNote facts={[
                { label: "Share", value: hourTotal ? fmtPct((peakHour.value / hourTotal) * 100, 0) : null, detail: "of statements this window" },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={mixState} label="query_workload_mix_hours" />}
        </Card>
      </div>
      <QueueHeatCards filters={filters} meta={meta} dims={dims} />
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Warehouses by pressure</div>
          {pressureRows ? (
            <React.Fragment>
              <Donut
                segments={["MEMORY", "CAPACITY", "MEMORY_AND_CAPACITY", "NONE"]
                  .map((p) => { const n = pressureRows.filter((r) => r.pressure === p).length; return { label: enumLabel(p) || p, value: n, display: fmtInt(n), color: pressureColor(p) }; })
                  .filter((s) => s.value > 0)}
                centerLabel={fmtInt(pressureRows.length - notJudged)} centerSub="judged"
              />
              {(notJudged > 0 || lightUse!.length > 0) && (
                <div className="metric-note" style={{ marginTop: 6 }}>
                  {[lightUse!.length ? `${fmtInt(lightUse!.length)} with pressure ran too little to flag` : null,
                    notJudged ? `${fmtInt(notJudged)} more not judged: too few statements` : null].filter(Boolean).join("; ")}.
                </div>
              )}
            </React.Fragment>
          ) : <ChartNote state={pressureState} label="query_warehouse_pressure" />}
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Workload by hour of day (UTC)</div>
          {hourItems ? <HBarList items={hourItems} valueFmt={(v) => `${fmtInt(v)} statements`} /> : <ChartNote state={mixState} label="query_workload_mix_hours" />}
        </div>
      </div>
      <QueueByWarehouse rows={queueByWh} dims={dims} maxCat={maxCat} pressureByWarehouse={pressureByWarehouse} windowDays={filters.window} startup={startup} />
      <div className="chart-note ok" style={{ marginTop: 14 }}>
        <div className="chart-note-text">
          {"Warehouse memory and concurrency settings live on "}
          <button type="button" className="pqgs-inline-link" onClick={() => onExternalJump("compute_warehouse_config_posture")}>Compute › Warehouses</button>.
        </div>
      </div>
    </div>
  );
}

// ─────────── By team ───────────
// The SAME tag rollup component Cost > Allocation uses, in performance mode -- not a copy.
function QueryTeamContent({ filters, meta, dims, maxCat }: LooseProps) {
  return <TagRollupView area="performance" filters={filters} meta={meta} dims={dims} maxCat={maxCat} />;
}

AreaContent.register("queries", "heavy", QueryHeavyContent);
AreaContent.register("queries", "efficiency", QueryEfficiencyContent);
AreaContent.register("queries", "reliability", QueryReliabilityContent);
AreaContent.register("queries", "capacity", QueryCapacityContent);
AreaContent.register("queries", "team", QueryTeamContent);
