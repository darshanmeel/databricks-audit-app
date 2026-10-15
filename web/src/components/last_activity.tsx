// Last activity of each warehouse, all-purpose cluster or workspace over all the history the
// system tables keep (compute_last_activity), banded by days since, with the flagged ones listed.
import React from "react";

import { fmtDate, fmtInt } from "../format";
import { resolveName } from "./names";
import { clusterName, numOrZero, useFindingData, warehouseName } from "./hooks";
import { BandRows } from "./band_rows";
import { ChartNote } from "./charts";
import { Card } from "./primitives";
import type { DimMaps, Row } from "../types";

type Kind = "warehouse" | "cluster" | "workspace";

const NOUN: Record<Kind, [string, string]> = {
  warehouse: ["warehouse", "warehouses"],
  cluster: ["all-purpose cluster", "all-purpose clusters"],
  workspace: ["workspace", "workspaces"],
};
const SIGNALS: Record<Kind, string> = {
  warehouse: "no query or start",
  cluster: "no bill",
  workspace: "no bill, job run, query or event by a person",
};
const LIST_MAX = 100;

const BANDS: { label: string; test: (r: Row) => boolean }[] = [
  { label: "Last 30 days", test: (r) => r.days_since_activity != null && numOrZero(r.days_since_activity) < 30 },
  { label: "30–89 days ago", test: (r) => r.days_since_activity != null && numOrZero(r.days_since_activity) >= 30 && numOrZero(r.days_since_activity) < 90 },
  { label: "90–179 days ago", test: (r) => numOrZero(r.days_since_activity) >= 90 && numOrZero(r.days_since_activity) < 180 },
  { label: "180+ days ago", test: (r) => numOrZero(r.days_since_activity) >= 180 },
  { label: "Never", test: (r) => r.last_activity_date == null && r.status !== "NOT_ASSESSED" },
  { label: "New, not used yet", test: (r) => r.status === "NOT_ASSESSED" },
];

function day(v: unknown): string { return v == null ? "-" : (fmtDate(String(v).slice(0, 10)) || String(v)); }

export function LastActivityCard({ kind, filters, dims }: { kind: Kind; filters: any; dims: DimMaps | null }) {
  const state = useFindingData("compute_last_activity", filters.window, filters.workspaceIds, filters.envs);
  const [one, many] = NOUN[kind];
  const title = `${many[0].toUpperCase()}${many.slice(1)} by last activity`;
  if (state.phase !== "ready" || !String(state.outcome).startsWith("ok_")) return <Card title={title}><ChartNote state={state} label="compute_last_activity" /></Card>;
  const all = ((state.data && state.data.rows) || []).filter((r: Row) => r.object_type === kind);
  // Workspaces Databricks no longer lists (no name) are most likely deleted already.
  const rows = kind === "workspace" ? all.filter((r: Row) => r.name) : all;
  const unlisted = all.length - rows.length;
  const nameOf = (r: Row) => kind === "warehouse" ? String(r.name || warehouseName(dims, r.warehouse_id))
    : kind === "cluster" ? String(r.name || clusterName(dims, r.cluster_id)) : String(r.name);
  const wsName = (r: Row) => String(resolveName("workspace", r.workspace_id, r.workspace_id) || r.workspace_id);
  const flagged = rows.filter((r: Row) => r.status === "WARN").sort((a: Row, b: Row) =>
    Number(a.last_activity_date != null) - Number(b.last_activity_date != null)
    || numOrZero(b.days_since_activity) - numOrZero(a.days_since_activity) || nameOf(a).localeCompare(nameOf(b)));
  const bands = BANDS.map((b) => {
    const n = rows.filter(b.test).length;
    return { label: b.label, sub: "", total: n, text: <React.Fragment><b className="mono">{fmtInt(n)}</b>{` ${n === 1 ? one : many}`}</React.Fragment> };
  }).filter((b) => b.total > 0 || !["Never", "New, not used yet"].includes(b.label));
  const noAudit = kind === "workspace" ? rows.filter((r: Row) => r.audit_seen === false).length : 0;
  return (
    <Card title={title} right={<span className="muted">{`${fmtInt(flagged.length)} of ${fmtInt(rows.length)} flagged`}</span>}>
      <div className="cj-card-subtitle"><span>
        {`${fmtInt(flagged.length)} ${flagged.length === 1 ? one : many} had ${SIGNALS[kind]} for 90 days or more, or never, in the history the system tables keep (up to a year). `}
        {kind === "workspace" ? "Ask the owner, then cancel what is not needed." : "Stopped ones cost nothing; removing them cuts clutter and access."}
      </span></div>
      <BandRows bands={bands} />
      {flagged.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 12 }}>
          <table className="data">
            <thead><tr>
              <th>{one[0].toUpperCase() + one.slice(1)}</th>{kind !== "workspace" && <th>Workspace</th>}
              <th>Last activity</th><th className="num">Days</th>
              {kind === "warehouse" && <React.Fragment><th>Last query</th><th>Last start</th></React.Fragment>}
              {kind === "workspace" && <React.Fragment><th>Last job run</th><th>Last query</th><th>Last person</th></React.Fragment>}
              <th>Last bill</th>
            </tr></thead>
            <tbody>
              {flagged.slice(0, LIST_MAX).map((r: Row) => (
                <tr key={`${r.workspace_id}:${r.warehouse_id || r.cluster_id || ""}`}>
                  <td>{nameOf(r)}</td>{kind !== "workspace" && <td>{wsName(r)}</td>}
                  <td>{r.last_activity_date == null ? "never" : day(r.last_activity_date)}</td>
                  <td className="num mono">{r.days_since_activity == null ? "-" : fmtInt(r.days_since_activity)}</td>
                  {kind === "warehouse" && <React.Fragment><td>{day(r.last_query_date)}</td><td>{day(r.last_start_date)}</td></React.Fragment>}
                  {kind === "workspace" && <React.Fragment>
                    <td>{day(r.last_job_run_date)}</td><td>{day(r.last_query_date)}</td>
                    <td>{r.audit_seen === false ? "no audit rows" : day(r.last_user_event_date)}</td>
                  </React.Fragment>}
                  <td>{day(r.last_billed_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {flagged.length > LIST_MAX && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(flagged.length - LIST_MAX)} more not listed.`}</div>}
      {noAudit > 0 && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(noAudit)} ${noAudit === 1 ? "workspace has" : "workspaces have"} no audit rows, most likely outside the export's region: only ${noAudit === 1 ? "its bills are" : "their bills are"} seen.`}</div>}
      {unlisted > 0 && <div className="muted" style={{ marginTop: 6 }}>{`${fmtInt(unlisted)} more ${unlisted === 1 ? "workspace" : "workspaces"} in the billing history ${unlisted === 1 ? "is" : "are"} no longer in Databricks' workspace list, most likely deleted, and not counted.`}</div>}
    </Card>
  );
}
