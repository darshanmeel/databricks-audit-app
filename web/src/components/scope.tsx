// T-71: say what the data covers.
//
// Two facts a screen must be able to state, and the ONE place their words live:
//  1. Region. tools/snapshot.py connects through one workspace. system.billing.* and
//     access.workspaces_latest are account-wide, but every other system table is regional: it
//     holds rows only for the workspaces of the metastore region the snapshot connected through
//     (/api/meta `metastore`). app/api/service.py reads a regional finding for workspaces outside
//     that region alone as NOT_ASSESSED (status_info.outside_region / not_assessed_reason
//     "outside_region") and reports the selected ones with spend as `region_gap`.
//  2. Filter reach. A finding with no workspace_id column (most of Governance and Lineage) is not
//     filtered by workspace: /api/findings rows carry `has_workspace_id` and `regional`.
// Nothing here decides coverage -- the API computes every fact; this file only words it.
// OUTSIDE_REGION_LABEL must equal app/api/service.py's (tests/test_scope_disclosure.py checks it).

import React from "react";
import type { Filters, FindingSummary, Meta, Metastore, RegionGap, Scope, StatusInfo } from "../types";

import { fmtInt } from "../format";
import { NO_WORKSPACE_MATCH } from "./filters";
import { Badge } from "./primitives";
import { getCheckLabel } from "./labels";

export const OUTSIDE_REGION_LABEL = "outside the snapshot's region";
const OUTSIDE_REGION_READING = `Not assessed: every selected workspace is ${OUTSIDE_REGION_LABEL}.`;
const REGIONAL_CHECK_AREAS = "compute, jobs, query, serving, storage and audit";

// A finding whose own findings.f_<id> table is simply missing (service.FindingNotBuiltError path)
// reads NOT_ASSESSED with status_info.not_built_reason -- the shape a real export built before a
// newly-added check existed will hit. That raw reason is written for a developer rebuilding a dev
// db ("rebuild -- dbt may be running..."), so every reader-facing surface (ChartNote, a bespoke
// card that never goes through ChartNote) shows this one honest line instead.
export const NOT_BUILT_READING = "Not in this export yet.";
/** The part of a finding or aggregate answer the scope helpers read. */
type ScopedData = { status_info?: StatusInfo; scope?: Scope } | null | undefined;

export function isNotBuilt(data: ScopedData): boolean {
  return !!(data && data.status_info && data.status_info.not_built_reason);
}

// One line for why a check did not run, from its status_info.
export function notAssessedText(data: ScopedData): string {
  if (isNotBuilt(data)) return NOT_BUILT_READING;
  if (isOutsideRegion(data)) return OUTSIDE_REGION_READING;
  const info: StatusInfo = (data && data.status_info) || {};
  const src = (info.blocking_sources || [])[0];
  if (src) return `Not assessed: ${src.source} is ${src.reason === "no_grant" ? "not readable with this token" : "not in this export"}.`;
  if (info.status === "error") return "Not assessed: this check failed in the export.";
  return "Not in this export.";
}

export function metastoreText(meta: { metastore: Metastore | null } | null | undefined): string {
  const ms = meta && meta.metastore;
  if (!ms || !ms.region) return "region not recorded";
  return ms.cloud ? `${ms.cloud} ${ms.region}` : String(ms.region);
}

// True when an /api/finding response is NOT_ASSESSED because every selected workspace is outside
// the snapshot's region.
export function isOutsideRegion(data: ScopedData): boolean {
  return !!(data && data.status_info && data.status_info.outside_region);
}

// One tile-sized line for a finding the workspace filter does not reach; null when it does.
export function findingScopeLabel(f: { has_workspace_id: boolean; regional: boolean } | null, meta: Meta | null): string | null {
  if (!f || f.has_workspace_id !== false) return null;
  return f.regional
    ? `Whole metastore (${metastoreText(meta)}) -- not filtered by workspace`
    : "Whole account -- not filtered by workspace";
}

export function regionWorkspaceNames(list: RegionGap[] | null | undefined, max?: number): string {
  const names = (list || []).map((w) => w.name || w.workspace_id);
  const cap = max || 5;
  if (names.length <= cap) return names.join(", ");
  return `${names.slice(0, cap).join(", ")} and ${names.length - cap} more`;
}

function regionGapTitle(list: RegionGap[] | null | undefined): string {
  return (list || []).map((w) => `${w.name || w.workspace_id} (${w.workspace_id}): ${w.reason}`).join("\n");
}

// App-wide line under the filter bar: the selected workspaces WITH spend that the snapshot's
// regional tables cannot see (/api/findings `region_gap`). Nothing when there are none. Names the
// workspaces rather than only the metastore, so the sentence reads the same whether or not the
// manifest recorded a region (metastoreText's own "region not recorded" used to leak into this
// exact sentence as "cover metastore region not recorded only").
export function RegionScopeBanner({ regionGap }: { meta?: Meta | null; regionGap: RegionGap[] | null }) {
  if (!regionGap || regionGap.length === 0) return null;
  const n = regionGap.length;
  const text = `${fmtInt(n)} workspace${n === 1 ? "" : "s"} with spend ${n === 1 ? "is" : "are"} outside the export's region `
    + `(${regionWorkspaceNames(regionGap, 5)}): ${n === 1 ? "its" : "their"} ${REGIONAL_CHECK_AREAS} checks are not assessed; `
    + `${n === 1 ? "its" : "their"} spend is counted.`;
  return <div className="scope-banner" title={regionGapTitle(regionGap)}>{text}</div>;
}

// The same fact on one finding (/api/finding `scope.region_gap`), above its outcome card.
function RegionGapLine({ data }: { data: ScopedData }) {
  const gap = data && data.scope && data.scope.region_gap;
  if (!gap || gap.length === 0 || isOutsideRegion(data)) return null;
  const n = gap.length;
  const text = `Not assessed for ${n} selected workspace${n === 1 ? "" : "s"} with spend ${OUTSIDE_REGION_LABEL} `
    + `(${regionWorkspaceNames(gap, 5)}). This check reads regional system tables, so the result below covers the snapshot's region only.`;
  return <div className="scope-note" title={regionGapTitle(gap)}>{text}</div>;
}

// The NOT_ASSESSED card when every selected workspace is outside the snapshot's region.
export function OutsideRegionCard({ info }: { info: StatusInfo | null | undefined }) {
  const ws = (info && info.outside_region && info.outside_region.workspaces) || [];
  return (
    <div className="honest-card not_assessed">
      <div className="h-title">{`Not assessed: ${OUTSIDE_REGION_LABEL}`}</div>
      <div className="h-note">{`Every selected workspace is ${OUTSIDE_REGION_LABEL}. This check reads regional system tables, which hold rows only for the metastore region the snapshot connected through, so it could not look at them. This is not a verified zero.`}</div>
      <ul>
        {ws.map((w) => (
          <li key={w.workspace_id}>
            <span className="mono">{w.name || w.workspace_id}</span>{` (${w.workspace_id}): ${w.reason}`}
          </li>
        ))}
      </ul>
      <div className="h-note">A snapshot taken through a workspace in their region would cover them; this app reads one snapshot, and so one region, at a time.</div>
    </div>
  );
}

// Detail-panel badge for a finding the workspace filter does not reach.
export function FindingScopeBadge({ scope }: { scope: Scope | null | undefined }) {
  if (!scope || scope.has_workspace_id !== false) return null;
  return (
    <Badge kind="info">{scope.regional ? "whole metastore, not filtered by workspace" : "whole account, not filtered by workspace"}</Badge>
  );
}

// Governance / Lineage: one line above the strip. `findings` are that tab's curated list rows.
export function TabScopeNote({ findings, meta, filters }: { findings: FindingSummary[] | null; meta: Meta | null; filters: Filters | null }) {
  const rows = findings || [];
  const unfiltered = rows.filter((f) => f.has_workspace_id === false);
  // Only worth saying while a workspace or environment filter narrows the page.
  const narrowed = !!(filters && ((filters.workspaceIds && filters.workspaceIds.length) || (filters.envs && filters.envs.length)));
  if (unfiltered.length === 0 || !narrowed) return null;
  const metastoreWide = unfiltered.filter((f) => f.regional).length;
  const accountWide = unfiltered.length - metastoreWide;
  // P2-FILTERS: when an attribute filter matches no checked workspace, filters.workspaceIds is
  // [NO_WORKSPACE_MATCH] (App.tsx's own "no filter = every workspace" convention needs a
  // sentinel id, not []) -- exclude it so this reads as "0 selected", not "1 selected".
  const selected = filters && filters.workspaceIds
    ? filters.workspaceIds.filter((id) => id !== NO_WORKSPACE_MATCH).length
    : 0;
  const n = unfiltered.length;
  // Name the checks on each side, so an auditor can quote which ones the filter reaches.
  const names = (list: FindingSummary[]) => {
    const t = list.map((f) => getCheckLabel(f.query_id, f.title).title);
    return t.length > 5 ? `${t.slice(0, 5).join(", ")} +${t.length - 5} more` : t.join(", ");
  };
  const followed = rows.filter((f) => f.has_workspace_id !== false);
  let text = n === rows.length
    ? `The workspace filter doesn't apply to ${n === 1 ? "this check" : `these ${n} checks`}`
    : `The workspace filter doesn't apply to ${n} of these ${rows.length} checks (${names(unfiltered)})`;
  text += selected > 0 ? `: ${n === 1 ? "it covers" : "they cover"} every workspace, not only the ${selected} selected.` : ".";
  if (metastoreWide > 0) text += ` ${metastoreWide === 1 && n === 1 ? "It covers" : `${metastoreWide} ${metastoreWide === 1 ? "covers" : "cover"}`} the whole metastore (${metastoreText(meta)}), not one workspace. Other metastores and regions are not captured.`;
  if (accountWide > 0) text += ` ${accountWide === 1 && n === 1 ? "It covers" : `${accountWide} ${accountWide === 1 ? "covers" : "cover"}`} the whole account, with no workspace to filter by.`;
  if (followed.length > 0 && n < rows.length) text += ` The other ${followed.length} count events with a workspace and follow the filter: ${names(followed)}.`;
  return <div className="scope-note">{text}</div>;
}

// Coverage & Gaps: the metastore, and every workspace the regional tables hold no row for.
// `region` is /api/coverage's `region`; `noun` is tab_coverage.tsx's own cvNoun (export_mode ->
// "snapshot"/"export"/"build") so this card never calls a direct export a "snapshot".
/** GET /api/coverage `region`: the metastore and the workspaces its regional tables hold no rows for. */
export interface CoverageRegion {
  metastore: Metastore | null;
  outside: RegionGap[] | null;
  unknown?: boolean;
  unknown_reason?: string;
  workspace_ids?: string[];
}

export function RegionCoverageCard({ region, noun = "snapshot" }: { region: CoverageRegion | null; noun?: string }) {
  if (!region) return null;
  const recorded = !!(region.metastore && region.metastore.region);
  const outside = region.outside;
  // F7: `first_run.py --workspace <id ...>` filters EVERY table, billing included
  // (tools/snapshot.py), not just the regional ones -- this must not still claim billing covers
  // the whole account when it does not.
  const filteredWorkspaceIds = region.workspace_ids || [];
  let body;
  if (outside === null || outside === undefined) {
    body = <div className="chart-note error"><div className="chart-note-text">Region coverage could not be read: dims.dim_workspace is not in this export.</div></div>;
  } else if (region.unknown) {
    body = <div className="chart-note not_assessed"><div className="chart-note-text">{`No workspace can be placed: ${region.unknown_reason}.`}</div></div>;
  } else if (outside.length === 0) {
    body = <div className="chart-note ok"><div className="chart-note-text">{`Every workspace in this ${noun} has rows in its regional tables.`}</div></div>;
  } else {
    body = (
      <React.Fragment>
      <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>These workspaces have no rows in any regional table, most likely because their metastore is in another region. Hover a row for the full reason.</div>
      <div className="data-table-wrap"><table className="data data-fit">
        <thead><tr><th>workspace</th><th>id</th><th>{`spend in ${noun}`}</th><th>regional rows</th></tr></thead>
        <tbody>
          {outside.map((w) => (
            <tr key={w.workspace_id}>
              <td>{w.name || "-"}</td>
              <td className="id mono">{w.workspace_id}</td>
              <td>{w.billed ? "yes" : "no"}</td>
              <td className="wrap" title={w.reason}>{w.billed ? "billed, no regional rows" : "no spend, no rows"}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      </React.Fragment>
    );
  }
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">Region coverage</div>
      <div className="scope-note">
        {`Regional system tables (compute, lakeflow, query, serving, storage, lineage, audit, Unity Catalog) were captured through metastore ${metastoreText({ metastore: region.metastore })}${recorded ? "" : " -- this export doesn't record it; re-run the export to record it"}.`
          + (filteredWorkspaceIds.length
            ? ` This ${noun} was limited with --workspace to ${filteredWorkspaceIds.join(", ")}: billing and every other table cover only those workspaces, and account-level audit events (workspace_id 0) were not exported.`
            : " Billing covers every workspace in the account.")}
      </div>
      {body}
    </div>
  );
}
