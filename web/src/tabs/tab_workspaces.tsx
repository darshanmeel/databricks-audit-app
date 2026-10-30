// Workspaces: who is active in each workspace (Governance > Access & grants) and, with what its
// catalogs hold, a complexity and a usage index per workspace (Governance > Workspaces). A tag such
// as a team's space name narrows it through the top-bar tag filter like every other screen.

import React from "react";

import { Api } from "../api";
import { fmtBytes, fmtDate, fmtInt } from "../format";
import { AreaContent, Card, PqgsNote } from "../components/primitives";
import { Donut, HBar, HBarList, paletteColor } from "../components/charts";
import { TableDetail } from "./table_detail";

function useWorkspaces(filters: any) {
  const [state, setState] = React.useState<{ phase: string; data: any }>({ phase: "loading", data: null });
  const key = JSON.stringify([filters.window, filters.workspaceIds, filters.envs, Api.tagParams()]);
  React.useEffect(() => {
    let live = true;
    setState({ phase: "loading", data: null });
    Api.workspaceUsage(filters.window, filters.workspaceIds, filters.envs)
      .then((d: any) => { if (live) setState({ phase: "ready", data: d }); })
      .catch(() => { if (live) setState({ phase: "error", data: null }); });
    return () => { live = false; };
  }, [key]);
  return state;
}

function notInExport(info: any) {
  if (!info) return "Not in this export: export again to get activity per workspace.";
  const err = info.activity && info.activity.error;
  return err ? `Not measured: ${err}` : "No workspace had activity.";
}

function lastSeen(d: any) {
  return d ? String(d).slice(0, 10) : "-";
}

// ─────────── Activity by workspace (Access & grants) ───────────
export function ActivityByWorkspacePanel({ filters, maxCat }: LooseProps) {
  const ws = useWorkspaces(filters);
  const d = ws.phase === "ready" ? ws.data : null;
  const rows: any[] = d && d.outcome !== "not_assessed" ? d.rows || [] : [];
  const shown = [...rows].sort((a, b) => b.active_people - a.active_people || b.queries - a.queries).slice(0, Math.max(maxCat || 8, 10));
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">{`Activity by workspace${d && d.window_days ? `, last ${d.window_days} days` : ""}`}</div>
      {ws.phase === "loading" ? <span className="muted">Loading activity...</span>
        : !rows.length ? <span className="muted">{notInExport(d && d.info)}</span> : (
        <React.Fragment>
          <div className="metric-note">People (an email) and service principals (an application id) seen signing in or running a query; queries from query history.</div>
          <div className="pqgs-rt">
            <div className="pqgs-rt-head pqgs-rt-cols-act">
              <span>Workspace</span><span style={{ textAlign: "right" }}>People</span>
              <span style={{ textAlign: "right" }}>Service principals</span><span style={{ textAlign: "right" }}>Queries</span>
              <span style={{ textAlign: "right" }}>Last activity</span>
            </div>
            {shown.map((r) => (
              <div className="pqgs-rt-row pqgs-rt-cols-act" key={r.workspace_id}>
                <span className="pqgs-rt-name" title={`${r.workspace_name} (${r.workspace_id})`}>{r.workspace_name}</span>
                <span className="pqgs-rt-num">{fmtInt(r.active_people)}</span>
                <span className="pqgs-rt-num">{fmtInt(r.active_service_principals)}</span>
                <span className="pqgs-rt-num">{fmtInt(r.queries)}</span>
                <span className="pqgs-rt-num">{lastSeen(r.last_activity)}</span>
              </div>
            ))}
          </div>
          <div className="pqgs-card-note" style={{ marginTop: 8 }}>
            {rows.length > shown.length ? `${fmtInt(rows.length - shown.length)} more workspaces, inactive people and what each one used: ` : "Inactive people and what each one used: "}
            <a href={`#tab=governance&w=${filters.window}&subtab=user_activity`}>Governance › User activity</a>.
          </div>
        </React.Fragment>
      )}
    </div>
  );
}


// One 100% bar per row, its parts labelled inside: e.g. a workspace's tables read and not read.
// A workspace in a chart: its name and environment.
export function wsLabel(r: any): string {
  return r.env ? `${r.workspace_name} · ${r.env}` : r.workspace_name;
}

// One colour per environment, in a fixed order so dev is always the same colour.
export function envColors(rows: any[]): Record<string, string> {
  const envs = Array.from(new Set(rows.map((r) => r.env || "unknown"))).sort();
  return Object.fromEntries(envs.map((e, i) => [e, paletteColor(i)]));
}

export function EnvLegend({ colors }: { colors: Record<string, string> }) {
  return (
    <div className="pqgs-card-note" style={{ marginTop: 6 }}>
      {Object.entries(colors).map(([env, c], k) => (
        <React.Fragment key={env}>
          <span className="legend-dot" style={{ background: c, marginLeft: k ? 12 : 0 }}></span>{` ${env}`}
        </React.Fragment>
      ))}
    </div>
  );
}

export function StackRows({ rows, legend }: { rows: { name: string; parts: { label: string; value: number; color: string }[] }[]; legend: { label: string; color: string }[] }) {
  if (!rows.length) return <span className="muted">Nothing to show.</span>;
  return (
    <div>
      {rows.map((r) => {
        const total = r.parts.reduce((a, x) => a + x.value, 0);
        return (
          <div key={r.name} style={{ display: "grid", gridTemplateColumns: "minmax(110px, 160px) 1fr 56px", gap: 10, alignItems: "center", margin: "6px 0" }}>
            <span className="pqgs-rt-name" title={r.name} style={{ fontSize: 13 }}>{r.name}</span>
            {total ? <HBar height={24} segments={r.parts.filter((x) => x.value).map((x) => ({ label: `${fmtInt(x.value)}`, value: x.value, color: x.color, display: `${x.label}: ${fmtInt(x.value)}` }))} />
              : <span className="muted">none</span>}
            <span className="mono" style={{ textAlign: "right", fontSize: 12.5 }}>{fmtInt(total)}</span>
          </div>
        );
      })}
      <div className="pqgs-card-note" style={{ marginTop: 6 }}>
        {legend.map((l, k) => (
          <React.Fragment key={l.label}>
            <span className="legend-dot" style={{ background: l.color, marginLeft: k ? 12 : 0 }}></span>{` ${l.label}`}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

const BAND_COLORS: Record<string, string> = {
  "Under 30 days": "var(--c1)", "30-90 days": "var(--c3)", "90-180 days": "var(--c4, #c9a227)", "180+ days": "var(--crit, #c0392b)",
};
const BAND_LEGEND = Object.keys(BAND_COLORS).map((b) => ({ label: `seen ${b.toLowerCase()} ago`, color: BAND_COLORS[b] }));

function SeenDonut({ bands, title }: { bands: any[]; title: string }) {
  const total = bands.reduce((a, b) => a + b.people + b.service_principals, 0);
  return (
    <div className="chart-card">
      <div className="chart-card-title">{title}</div>
      {total ? (
        <React.Fragment>
          <Donut segments={bands.map((b) => ({ label: `Last seen ${b.band.toLowerCase()} ago`, value: b.people + b.service_principals,
            display: `${fmtInt(b.people)} people, ${fmtInt(b.service_principals)} SPs`, color: BAND_COLORS[b.band] }))}
            centerLabel={fmtInt(total)} centerSub="identities" />
          <div className="pqgs-card-note" style={{ marginTop: 6 }}>From the audit log over the last year; 90+ days reads inactive.</div>
        </React.Fragment>
      ) : <span className="muted">Last seen not in this export: export again.</span>}
    </div>
  );
}

function InactiveTable({ rows, total, days, showWorkspaces }: { rows: any[]; total: number; days: number; showWorkspaces?: boolean }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? rows : rows.slice(0, 20);
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">{`Inactive: not seen for ${days}+ days · ${fmtInt(total)}`}</div>
      {!rows.length ? <span className="muted">{`Everyone seen in the last year was active in the last ${days} days.`}</span> : (
        <React.Fragment>
          <div className="metric-note">People and service principals with an audit event in the last year but none in the last {days} days: candidates to remove from the workspace or its groups.</div>
          <div className="pqgs-rt">
            <div className={`pqgs-rt-head ${showWorkspaces ? "pqgs-rt-cols-inw" : "pqgs-rt-cols-in"}`}>
              <span>Who</span><span>Kind</span>{showWorkspaces && <span>Workspaces</span>}
              <span style={{ textAlign: "right" }}>Last seen</span><span style={{ textAlign: "right" }}>Last login</span>
              <span style={{ textAlign: "right" }}>Days</span>
            </div>
            {shown.map((r) => (
              <div className={`pqgs-rt-row ${showWorkspaces ? "pqgs-rt-cols-inw" : "pqgs-rt-cols-in"}`} key={r.identity}>
                <span className="pqgs-rt-name">{r.identity}</span>
                <span>{r.kind}</span>
                {showWorkspaces && <span className="pqgs-rt-note" title={(r.workspaces || []).join(", ")}>{(r.workspaces || []).length > 2 ? `${r.workspaces.slice(0, 2).join(", ")} +${r.workspaces.length - 2}` : (r.workspaces || []).join(", ")}</span>}
                <span className="pqgs-rt-num">{lastSeen(r.last_seen)}</span>
                <span className="pqgs-rt-num">{lastSeen(r.last_login)}</span>
                <span className="pqgs-rt-num">{r.days == null ? "-" : fmtInt(r.days)}</span>
              </div>
            ))}
          </div>
          {rows.length > shown.length && <button type="button" className="link-btn" style={{ marginTop: 8 }} onClick={() => setAll(true)}>{`Show all ${fmtInt(rows.length)}`}</button>}
        </React.Fragment>
      )}
    </div>
  );
}

const READ_COLOR = "var(--c1)";
// when a table was last read, oldest last; the names come from the API
const READ_BAND_COLORS = [READ_COLOR, "var(--c3)", "var(--c2)", "var(--c4, #c9a227)"];
const UNREAD_COLOR = "var(--c4, #c9a227)";

// ─────────── Complexity x usage scatter ───────────
function Scatter({ rows }: { rows: any[] }) {
  const W = 960, H = 320, P = 40;
  const colors = envColors(rows);
  const held = (r: any) => (r.size_in_bytes || 0) + (r.volume_bytes || 0);
  const maxSize = Math.max(1, ...rows.map(held));
  const x = (v: number) => P + (v / 100) * (W - 2 * P);
  const y = (v: number) => H - P - (v / 100) * (H - 2 * P);
  const rad = (s: number) => 4 + 18 * Math.sqrt((s || 0) / maxSize);
  return (
    <div style={{ overflowX: "auto" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: W, display: "block" }} role="img"
        aria-label="Complexity against usage, bubble size = data size">
        <line x1={x(50)} y1={y(0)} x2={x(50)} y2={y(100)} stroke="var(--line, #ddd)" strokeDasharray="4 4" />
        <line x1={x(0)} y1={y(50)} x2={x(100)} y2={y(50)} stroke="var(--line, #ddd)" strokeDasharray="4 4" />
        <line x1={x(0)} y1={y(0)} x2={x(100)} y2={y(0)} stroke="currentColor" opacity={0.4} />
        <line x1={x(0)} y1={y(0)} x2={x(0)} y2={y(100)} stroke="currentColor" opacity={0.4} />
        <text x={x(100)} y={H - 8} textAnchor="end" fontSize={11} fill="currentColor" opacity={0.7}>Complexity index</text>
        <text x={8} y={P - 12} fontSize={11} fill="currentColor" opacity={0.7}>Usage index</text>
        <text x={x(98)} y={y(96)} textAnchor="end" fontSize={10} fill="currentColor" opacity={0.5}>built and used</text>
        <text x={x(98)} y={y(4)} textAnchor="end" fontSize={10} fill="currentColor" opacity={0.5}>built, little used</text>
        <text x={x(2)} y={y(4)} fontSize={10} fill="currentColor" opacity={0.5}>small, little used</text>
        {rows.map((r) => (
          <circle key={r.workspace_id} cx={x(r.complexity_index || 0)} cy={y(r.usage_index || 0)} r={rad(held(r))}
            fill={colors[r.env || "unknown"]} fillOpacity={0.55} stroke={colors[r.env || "unknown"]}>
            <title>{`${wsLabel(r)}: complexity ${r.complexity_index}, usage ${r.usage_index}, ${fmtBytes(held(r))} in tables and volumes`}</title>
          </circle>
        ))}
      </svg>
      <EnvLegend colors={colors} />
    </div>
  );
}


// ─────────── One workspace: its tables and its people ───────────
function useAsync(fn: () => Promise<any>, deps: any[]) {
  const [state, setState] = React.useState<{ phase: string; data: any }>({ phase: "loading", data: null });
  React.useEffect(() => {
    let live = true;
    setState({ phase: "loading", data: null });
    fn().then((d: any) => { if (live) setState({ phase: "ready", data: d }); })
      .catch(() => { if (live) setState({ phase: "error", data: null }); });
    return () => { live = false; };
  }, deps);
  return state;
}

const day = (v: any) => (v ? fmtDate(v) || String(v).slice(0, 10) : "-");
const TABLES_SHOWN = 50;

function UserTables({ workspaceId, identity }: { workspaceId: string; identity: string }) {
  const st = useAsync(() => Api.workspaceUserTables(workspaceId, identity), [workspaceId, identity]);
  const rows: any[] = st.phase === "ready" && st.data ? st.data.rows || [] : [];
  if (st.phase === "loading") return <div className="muted" style={{ padding: "6px 0 10px 12px" }}>Loading tables...</div>;
  if (!rows.length) return <div className="muted" style={{ padding: "6px 0 10px 12px" }}>No table read or written in the window (lineage).</div>;
  return (
    <div className="pqgs-rt" style={{ margin: "4px 0 12px 12px" }}>
      <div className="pqgs-rt-head pqgs-rt-cols-ut">
        <span>Table</span><span style={{ textAlign: "right" }}>Reads</span><span style={{ textAlign: "right" }}>Writes</span>
        <span style={{ textAlign: "right" }}>Last</span>
      </div>
      {rows.map((r) => (
        <div className="pqgs-rt-row pqgs-rt-cols-ut" key={r.table_full_name}>
          <span className="pqgs-rt-name">{r.table_full_name}</span>
          <span className="pqgs-rt-num">{fmtInt(r.reads)}</span>
          <span className="pqgs-rt-num">{fmtInt(r.writes)}</span>
          <span className="pqgs-rt-num">{day(r.last_access)}</span>
        </div>
      ))}
    </div>
  );
}

function NotDescribed({ rows }: { rows: any[] }) {
  if (!rows || !rows.length) return null;
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">{`Could not describe · ${fmtInt(rows.reduce((a, r) => a + r.tables, 0))} tables`}</div>
      <div className="metric-note">DESCRIBE DETAIL failed here, so these tables have no size. Usually the export identity has no access: give it USE and SELECT (or run the export as one that has it).</div>
      <div className="pqgs-rt">
        <div className="pqgs-rt-head pqgs-rt-cols-nd"><span>Schema</span><span style={{ textAlign: "right" }}>Tables</span><span>First error</span></div>
        {rows.map((r) => (
          <div className="pqgs-rt-row pqgs-rt-cols-nd" key={`${r.table_catalog}.${r.table_schema}`}>
            <span className="pqgs-rt-name">{`${r.table_catalog}.${r.table_schema}`}</span>
            <span className="pqgs-rt-num">{fmtInt(r.tables)}</span>
            <span className="pqgs-rt-note">{r.error}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

type Part = "both" | "tables" | "people";

// Each schema of the workspace's catalogs: what is shopped (views, view columns) and what is built
// (tables, columns, volumes and their files), and the size of both.
// Tables per schema by when they were last read: where the unused data sits.
function SchemaReads({ rows, bands, lookback, top }: { rows: any[]; bands: string[]; lookback: number | null; top: number }) {
  const withTables = rows.filter((r) => r.tables && r.read_bands).sort((a, b) => b.tables - a.tables).slice(0, top);
  if (!withTables.length || !bands.length) return null;
  const label = (b: string) => (b === "Not read" && lookback ? `not read in ${lookback} days` : b.toLowerCase());
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">Tables by last read, per schema</div>
      <StackRows rows={withTables.map((r) => ({ name: `${r.table_catalog}.${r.table_schema}`,
        parts: bands.map((b, i) => ({ label: label(b), value: r.read_bands[b] || 0, color: READ_BAND_COLORS[i] })) }))}
        legend={bands.map((b, i) => ({ label: label(b), color: READ_BAND_COLORS[i] }))} />
    </div>
  );
}

function SchemaTable({ rows, sized, name }: { rows: any[]; sized: boolean; name: string }) {
  if (!rows.length) return null;
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">{`${name} · by schema`}</div>
      <div className="metric-note">Views are what a schema serves from elsewhere; tables and volumes are what was built in it. Size is tables plus volume files.</div>
      <div className="pqgs-rt">
        <div className="pqgs-rt-head pqgs-rt-cols-sch">
          <span>Schema</span><span style={{ textAlign: "right" }}>Tables</span><span style={{ textAlign: "right" }}>Columns</span>
          <span style={{ textAlign: "right" }}>Views</span><span style={{ textAlign: "right" }}>View columns</span>
          <span style={{ textAlign: "right" }}>Volumes</span><span style={{ textAlign: "right" }}>Volume files</span>
          <span style={{ textAlign: "right" }}>Size</span>
        </div>
        {rows.map((r) => (
          <div className="pqgs-rt-row pqgs-rt-cols-sch" key={`${r.table_catalog}.${r.table_schema}`}>
            <span className="pqgs-rt-name">{`${r.table_catalog}.${r.table_schema}`}</span>
            <span className="pqgs-rt-num">{fmtInt(r.tables)}</span>
            <span className="pqgs-rt-num">{fmtInt(r.table_columns)}</span>
            <span className="pqgs-rt-num">{fmtInt(r.views)}</span>
            <span className="pqgs-rt-num">{fmtInt(r.view_columns)}</span>
            <span className="pqgs-rt-num">{fmtInt(r.volumes)}</span>
            <span className="pqgs-rt-num">{fmtInt(r.volume_files)}</span>
            <span className="pqgs-rt-num">{sized ? fmtBytes((r.size_in_bytes || 0) + (r.volume_bytes || 0)) : "-"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// One workspace: its tables, its people, or both, from one fetch.
function WorkspaceDetail({ ws, window: w, maxCat, part }: { ws: any; window: number; maxCat?: number; part: Part }) {
  const st = useAsync(() => Api.workspaceDetail(ws.workspace_id), [ws.workspace_id]);
  const [which, setWhich] = React.useState<"all" | "30" | "90" | "none" | "written">("all");
  const [open, setOpen] = React.useState<string | null>(null);
  const [openTable, setOpenTable] = React.useState<string | null>(null);
  const d = st.phase === "ready" ? st.data : null;
  if (st.phase === "loading") return <div className="chart-card" style={{ marginTop: 14 }}><span className="muted">{`Loading ${ws.workspace_name}...`}</span></div>;
  if (!d || d.outcome === "not_assessed") return <div className="chart-card" style={{ marginTop: 14 }}><span className="muted">Not in this export: export again to get workspace details.</span></div>;
  const tables: any[] = (d.tables || []).filter((t: any) => !t.view);
  const unread = tables.filter((t) => !t.reads);
  const writtenOnly = tables.filter((t) => !t.reads && t.writes);
  const read = tables.filter((t) => t.reads);
  const bandNames: string[] = d.read_bands || [];
  const lookback: number | null = d.read_lookback_days || null;
  const notIn = (n: number) => tables.filter((t) => t.read_days == null || t.read_days >= n);
  const never = tables.filter((t) => t.read_days == null);
  const list = which === "30" ? notIn(30) : which === "90" ? notIn(90) : which === "none" ? never
    : which === "written" ? writtenOnly : tables;
  const bandCount = (name: string) => tables.filter((t) => t.read_band === name).length;
  const shown = list.slice(0, TABLES_SHOWN);
  const users: any[] = d.users || [];
  const people = users.filter((u) => u.kind === "Person").length;
  const sps = users.filter((u) => u.kind === "Service principal").length;
  const top = Math.max(maxCat || 8, 8);
  const bigUnread = [...unread].filter((t) => t.size_in_bytes).sort((a, b) => b.size_in_bytes - a.size_in_bytes).slice(0, top)
    .map((t) => ({ name: t.table_full_name, value: t.size_in_bytes }));
  const mostRead = [...read].sort((a, b) => b.reads - a.reads).slice(0, top).map((t) => ({ name: t.table_full_name, value: t.reads }));
  const byQueries = [...users].filter((u) => u.queries).sort((a, b) => b.queries - a.queries).slice(0, top)
    .map((u) => ({ name: u.identity, value: u.queries, color: u.kind === "Service principal" ? "var(--c2)" : undefined }));
  const unreadBytes = unread.reduce((a, t) => a + (t.size_in_bytes || 0), 0);
  const showTables = part !== "people";
  const showPeople = part !== "tables";
  return (
    <div style={{ marginTop: 14 }}>
      {showTables && (
        <React.Fragment>
          <div className="grid-2">
            <div className="chart-card">
              <div className="chart-card-title">{`${ws.workspace_name} · when were its tables last read?`}</div>
              <Donut segments={bandNames.map((b, i) => ({ label: b === "Not read" && lookback ? `Not read in ${lookback} days` : b,
                value: bandCount(b), display: fmtInt(bandCount(b)), color: READ_BAND_COLORS[i] }))}
                centerLabel={fmtInt(tables.length)} centerSub="tables" />
              {d.sized && <div className="pqgs-card-note" style={{ marginTop: 6 }}>{`${fmtBytes(unreadBytes)} in tables nobody read.`}</div>}
            </div>
            <div className="chart-card">
              <div className="chart-card-title">Biggest tables nobody read</div>
              {bigUnread.length ? <HBarList items={bigUnread} valueFmt={(v) => fmtBytes(v)} /> : <span className="muted">Every sized table was read.</span>}
            </div>
          </div>
          <SchemaReads rows={d.schemas || []} bands={bandNames} lookback={lookback} top={top} />
          <SchemaTable rows={d.schemas || []} sized={d.sized} name={ws.workspace_name} />
          <div className="chart-card" style={{ marginTop: 14 }}>
            <div className="chart-card-title">Most-read tables</div>
            {mostRead.length ? <HBarList items={mostRead} valueFmt={(v) => `${fmtInt(v)} reads`} /> : <span className="muted">No reads in lineage.</span>}
          </div>
          <div className="chart-card" style={{ marginTop: 14 }}>
            <div className="chart-card-title">{`${ws.workspace_name} · tables · click one for its history and maintenance`}</div>
            <div className="metric-note">
              {`${fmtInt(tables.length)} tables${d.catalogs.length ? ` in ${d.catalogs.join(", ")} and any other table used here` : " used here"}; last read and write from lineage${lookback ? ` over ${lookback} days` : ""}, reads and readers in the window.`}
              {!d.lineage ? " Lineage not in this export, so no reads or writes." : ""}
            </div>
            <div className="ws-actions">
              <button type="button" className={which === "all" ? "on" : ""} onClick={() => setWhich("all")}>{`All · ${fmtInt(tables.length)}`}</button>
              <button type="button" className={which === "30" ? "on" : ""} onClick={() => setWhich("30")}>{`Not read 30+ days · ${fmtInt(notIn(30).length)}`}</button>
              <button type="button" className={which === "90" ? "on" : ""} onClick={() => setWhich("90")}>{`Not read 90+ days · ${fmtInt(notIn(90).length)}`}</button>
              <button type="button" className={which === "none" ? "on" : ""} onClick={() => setWhich("none")}>{`${lookback ? `Not read in ${lookback} days` : "Never read"} · ${fmtInt(never.length)}`}</button>
              <button type="button" className={which === "written" ? "on" : ""} onClick={() => setWhich("written")}>{`Written, not read · ${fmtInt(writtenOnly.length)}`}</button>
            </div>
            <div className="pqgs-rt">
              <div className="pqgs-rt-head pqgs-rt-cols-tbl">
                <span>Table</span><span style={{ textAlign: "right" }}>Size</span><span style={{ textAlign: "right" }}>Files</span>
                <span style={{ textAlign: "right" }}>Last read</span><span style={{ textAlign: "right" }}>Last write</span>
                <span style={{ textAlign: "right" }}>Reads</span><span style={{ textAlign: "right" }}>Readers</span>
              </div>
              {shown.map((t) => (
                <React.Fragment key={t.table_full_name}>
                <div className="pqgs-rt-row pqgs-rt-cols-tbl" style={{ cursor: "pointer" }}
                  onClick={() => setOpenTable(openTable === t.table_full_name ? null : t.table_full_name)}>
                  <span className="pqgs-rt-name" title={t.detail && String(t.detail).startsWith("failed") ? t.detail : t.table_full_name}>{`${openTable === t.table_full_name ? "▾" : "▸"} ${t.table_full_name}`}</span>
                  <span className="pqgs-rt-num">{t.size_in_bytes == null ? "-" : fmtBytes(t.size_in_bytes)}</span>
                  <span className="pqgs-rt-num">{t.num_files == null ? "-" : fmtInt(t.num_files)}</span>
                  <span className="pqgs-rt-num" title={t.last_read ? day(t.last_read) : undefined}>
                    {t.read_days != null ? `${fmtInt(t.read_days)} days ago` : lookback ? `none in ${lookback} days` : "-"}</span>
                  <span className="pqgs-rt-num">{day(t.last_write || t.last_modified)}</span>
                  <span className="pqgs-rt-num">{fmtInt(t.reads)}</span>
                  <span className="pqgs-rt-num">{fmtInt(t.readers)}</span>
                </div>
                {openTable === t.table_full_name && <TableDetail name={t.table_full_name} />}
                </React.Fragment>
              ))}
            </div>
            {list.length > shown.length && <div className="pqgs-card-note" style={{ marginTop: 8 }}>{`Biggest ${TABLES_SHOWN} of ${fmtInt(list.length)} shown.`}</div>}
            <div className="pqgs-card-note" style={{ marginTop: 8 }}>
              Every account's unread tables, with their storage cost: <a href={`#tab=storage&w=${w}&subtab=tables`}>Storage › Tables</a>.
            </div>
          </div>
          <NotDescribed rows={d.not_described || []} />
        </React.Fragment>
      )}
      {showPeople && (
        <React.Fragment>
          <div className="grid-2" style={{ marginTop: showTables ? 14 : 0 }}>
            <div className="chart-card">
              <div className="chart-card-title">{`${ws.workspace_name} · people and service principals by queries`}</div>
              {byQueries.length ? <HBarList items={byQueries} valueFmt={(v) => fmtInt(v)} /> : <span className="muted">No queries in the window.</span>}
              <div className="pqgs-card-note" style={{ marginTop: 6 }}>Orange: a service principal.</div>
            </div>
            <SeenDonut bands={d.seen_bands || []} title={`${ws.workspace_name} · everyone by last seen`} />
          </div>
          <div className="chart-card" style={{ marginTop: 14 }}>
            <div className="chart-card-title">{`${ws.workspace_name} · who stopped coming`}</div>
            {(d.inactive || []).length ? <HBarList items={(d.inactive || []).slice(0, top).map((r: any) => ({ name: r.identity, value: r.days || 0,
              color: r.kind === "Service principal" ? "var(--c2)" : BAND_COLORS[r.band] }))} valueFmt={(v) => `${fmtInt(v)} days`} />
              : <span className="muted">{`Nobody inactive for ${d.inactive_days || 90}+ days.`}</span>}
          </div>
          <div className="chart-card" style={{ marginTop: 14 }}>
            <div className="chart-card-title">{`${ws.workspace_name} · people and service principals`}</div>
            <div className="metric-note">{`${fmtInt(people)} people, ${fmtInt(sps)} service principals seen signing in, querying or using a table. Click one for the tables it used.`}</div>
            <div className="pqgs-rt">
              <div className="pqgs-rt-head pqgs-rt-cols-usr">
                <span>Who</span><span>Kind</span><span style={{ textAlign: "right" }}>Last seen</span>
                <span style={{ textAlign: "right" }}>Logins</span><span style={{ textAlign: "right" }}>Queries</span>
                <span style={{ textAlign: "right" }}>Tables read</span><span style={{ textAlign: "right" }}>Tables written</span>
              </div>
              {users.slice(0, 200).map((u) => (
                <React.Fragment key={u.identity}>
                  <div className="pqgs-rt-row pqgs-rt-cols-usr" style={{ cursor: "pointer" }} onClick={() => setOpen(open === u.identity ? null : u.identity)}>
                    <span className="pqgs-rt-name">{`${open === u.identity ? "▾" : "▸"} ${u.identity}`}</span>
                    <span>{u.kind}</span>
                    <span className="pqgs-rt-num">{day(u.last_seen)}</span>
                    <span className="pqgs-rt-num">{fmtInt(u.logins)}</span>
                    <span className="pqgs-rt-num">{fmtInt(u.queries)}</span>
                    <span className="pqgs-rt-num">{fmtInt(u.tables_read)}</span>
                    <span className="pqgs-rt-num">{fmtInt(u.tables_written)}</span>
                  </div>
                  {open === u.identity && <UserTables workspaceId={ws.workspace_id} identity={u.identity} />}
                </React.Fragment>
              ))}
            </div>
            <div className="pqgs-card-note" style={{ marginTop: 8 }}>
              Every workspace's last activity: <a href={`#tab=governance&w=${w}&subtab=admin`}>Governance › Admin activity</a>.
            </div>
          </div>
          <InactiveTable rows={d.inactive || []} total={(d.inactive || []).length} days={d.inactive_days || 90} />
        </React.Fragment>
      )}
    </div>
  );
}

// ─────────── The workspace list: columns per screen, click a row for its detail ───────────
type Col = { label: string; value: (r: any) => React.ReactNode };
type Sort = { label: string; by: (r: any) => number };

const SIZE = (r: any) => (r.size_in_bytes || 0) + (r.volume_bytes || 0);
const SORTS: Record<string, Sort> = {
  usage: { label: "Usage", by: (r) => r.usage_index || 0 },
  complexity: { label: "Complexity", by: (r) => r.complexity_index || 0 },
  size: { label: "Size", by: SIZE },
  unread: { label: "Not read", by: (r) => r.tables_not_read || 0 },
};

function WorkspaceList({ rows, cols, title, picked, setPicked, note, sorts }: {
  rows: any[]; cols: Col[]; title: string; picked: any; setPicked: (r: any) => void; note?: React.ReactNode; sorts?: Sort[];
}) {
  const grid = { gridTemplateColumns: `minmax(170px, 1.6fr) repeat(${cols.length}, minmax(70px, 100px))` };
  const [sortIx, setSortIx] = React.useState(0);
  const sort = sorts && sorts[sortIx];
  const shown = sort ? [...rows].sort((a, b) => sort.by(b) - sort.by(a)) : rows;
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">{`${title} · click one for its detail`}</div>
      {sorts && (
        <div className="ws-actions">
          {sorts.map((s, i) => <button type="button" key={s.label} className={i === sortIx ? "on" : ""} onClick={() => setSortIx(i)}>{`By ${s.label.toLowerCase()}`}</button>)}
        </div>
      )}
      <div className="pqgs-rt">
        <div className="pqgs-rt-head" style={grid}>
          <span>Workspace</span>{cols.map((c) => <span key={c.label} style={{ textAlign: "right" }}>{c.label}</span>)}
        </div>
        {shown.map((r) => {
          const on = picked && picked.workspace_id === r.workspace_id;
          return (
            <div className={`pqgs-rt-row${on ? " on" : ""}`} style={{ ...grid, cursor: "pointer" }} key={r.workspace_id}
              onClick={() => setPicked(on ? null : r)}>
              <span className="pqgs-rt-name" title={`${r.workspace_id}${r.catalogs.length ? ` · catalogs: ${r.catalogs.join(", ")}` : ""}`}>
                {`${on ? "▾" : "▸"} ${r.workspace_name}`}
                {r.url ? <a href={/^https?:/.test(r.url) ? r.url : `https://${r.url}`} target="_blank" rel="noreferrer"
                  onClick={(e) => e.stopPropagation()} style={{ marginLeft: 6 }} title="Open the workspace">↗</a> : null}
              </span>
              {cols.map((c) => <span className="pqgs-rt-num" key={c.label}>{c.value(r)}</span>)}
            </div>
          );
        })}
      </div>
      {note && <div className="pqgs-card-note" style={{ marginTop: 8 }}>{note}</div>}
    </div>
  );
}

// The picked workspace, scrolled into view.
function usePicked() {
  const [picked, setPicked] = React.useState<any>(null);
  const ref = React.useRef<HTMLDivElement | null>(null);
  React.useEffect(() => { if (picked && ref.current) ref.current.scrollIntoView({ behavior: "smooth", block: "start" }); }, [picked]);
  return { picked, setPicked, ref };
}

const INDEX_NOTE = "A workspace holds the catalogs it reads and writes most (lineage). Indexes 0 to 100, on a log scale against the largest workspace shown: complexity = tables 25%, views 15%, columns 20%, size 25%, volume files 15%; usage = people 40%, service principals 20%, queries 40%. An input no workspace has drops out. Env stands in for the workspace type.";
const inactiveIn = (r: any) => (r.seen_bands ? (r.seen_bands["90-180 days"] || 0) + (r.seen_bands["180+ days"] || 0) : 0);

function useWorkspaceRows(filters: any, onVerdict: any, verdictOf: (rows: any[], d: any) => string) {
  const ws = useWorkspaces(filters);
  const d = ws.phase === "ready" ? ws.data : null;
  const rows: any[] = d && d.outcome !== "not_assessed" ? d.rows || [] : [];
  const verdict = rows.length ? verdictOf(rows, d) : "Loading workspaces...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  return { ws, d, rows };
}

const sumOf = (rows: any[], k: string) => rows.reduce((a, r) => a + (r[k] || 0), 0);

// ─────────── Governance > Workspaces: both sides at a glance ───────────
function WorkspacesContent({ filters, maxCat, onVerdict }: LooseProps) {
  const top = Math.max(maxCat || 8, 8);
  const { ws, d, rows } = useWorkspaceRows(filters, onVerdict, (rs) =>
    `${fmtInt(rs.length)} workspaces; ${fmtInt(rs.filter((r) => (r.usage_index || 0) < 20).length)} with little use.`);
  const { picked, setPicked, ref } = usePicked();
  if (ws.phase === "loading") return <span className="muted">Loading workspaces...</span>;
  if (!rows.length) return <span className="muted">{notInExport(d && d.info)}</span>;
  const sized = !!d.sized;
  const idle = rows.filter((r) => (r.usage_index || 0) < 20);
  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Workspaces">
          <div className="pqgs-card-value">{fmtInt(rows.length)}</div>
          <PqgsNote facts={[{ label: "Little use", value: fmtInt(idle.length), detail: "usage index under 20" }]} />
        </Card>
        <Card title="Active people">
          <div className="pqgs-card-value">{fmtInt(sumOf(rows, "active_people"))}</div>
          <PqgsNote facts={[{ label: "Service principals", value: fmtInt(sumOf(rows, "active_service_principals")) }, { label: "Window", value: `${d.window_days} days` }]} />
        </Card>
        <Card title="Queries">
          <div className="pqgs-card-value">{fmtInt(sumOf(rows, "queries"))}</div>
          <PqgsNote facts={[{ label: "From", value: "query history" }]} />
        </Card>
        <Card title="Data held">
          <div className="pqgs-card-value">{sized ? fmtBytes(sumOf(rows, "size_in_bytes")) : "-"}</div>
          <PqgsNote facts={[{ label: "Tables", value: fmtInt(sumOf(rows, "tables")), detail: sized ? `${fmtInt(sumOf(rows, "views"))} views` : "table sizes not in this export" }]} />
        </Card>
      </div>
      <div className="chart-card">
        <div className="chart-card-title">Complexity against usage</div>
        <div className="metric-note">Each bubble is a workspace, coloured by its environment and sized by the data its catalogs hold. Right and low: built but little used, the first to review. Tables in detail: <a href={`#tab=storage&w=${filters.window}&subtab=table_storage`}>Storage › Workspace table usage</a>; people: <a href={`#tab=governance&w=${filters.window}&subtab=user_activity`}>Governance › User activity</a>.</div>
        <Scatter rows={rows} />
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Most complex workspaces</div>
          {rows.some((r) => r.complexity_index) ? <HBarList items={[...rows].filter((r) => r.complexity_index).sort((a, b) => b.complexity_index - a.complexity_index).slice(0, top)
            .map((r) => ({ name: wsLabel(r), value: r.complexity_index }))} valueFmt={(v) => `${v}`} />
            : <span className="muted">No workspace holds a catalog yet.</span>}
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Data held by workspace</div>
          {sized && rows.some(SIZE) ? <HBarList items={[...rows].filter(SIZE).sort((a, b) => SIZE(b) - SIZE(a)).slice(0, top)
            .map((r) => ({ name: wsLabel(r), value: SIZE(r) }))} valueFmt={(v) => fmtBytes(v)} />
            : <span className="muted">Table sizes not in this export.</span>}
        </div>
      </div>
      <WorkspaceList rows={rows} title="Every workspace" sorts={[SORTS.usage, SORTS.complexity, SORTS.size]} picked={picked} setPicked={setPicked} note={INDEX_NOTE} cols={[
        { label: "Env", value: (r) => r.env || "-" },
        { label: "Tables", value: (r) => fmtInt(r.tables) },
        { label: "Size", value: (r) => (sized ? fmtBytes((r.size_in_bytes || 0) + (r.volume_bytes || 0)) : "-") },
        { label: "Not read", value: (r) => fmtInt(r.tables_not_read) },
        { label: "People", value: (r) => fmtInt(r.active_people) },
        { label: "Queries", value: (r) => fmtInt(r.queries) },
        { label: "Inactive", value: (r) => fmtInt(inactiveIn(r)) },
        { label: "Complexity", value: (r) => (r.complexity_index == null ? "-" : r.complexity_index) },
        { label: "Usage", value: (r) => r.usage_index },
      ]} />
      <div ref={ref}>{picked && <WorkspaceDetail ws={picked} window={filters.window} maxCat={maxCat} part="both" />}</div>
    </div>
  );
}

// ─────────── Every table, any workspace: click one for its history and maintenance ───────────
function EveryTable({ filters }: { filters: any }) {
  const st = useAsync(() => Api.tables(filters.workspaceIds, filters.envs), [JSON.stringify([filters.workspaceIds, filters.envs, Api.tagParams()])]);
  const [which, setWhich] = React.useState<"all" | "unread" | "written">("all");
  const [open, setOpen] = React.useState<string | null>(null);
  const [n, setN] = React.useState(50);
  const d = st.phase === "ready" ? st.data : null;
  if (!d || d.outcome === "not_assessed") return null;
  const tables: any[] = (d.rows || []).filter((t: any) => (t.table_type || "").toUpperCase() !== "VIEW");
  const unread = tables.filter((t) => !t.reads);
  const writtenOnly = tables.filter((t) => !t.reads && t.writes);
  const list = which === "unread" ? unread : which === "written" ? writtenOnly : tables;
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">{`Every table, biggest first · ${fmtInt(tables.length)}${d.total > (d.rows || []).length ? ` (the biggest ${fmtInt((d.rows || []).length)} of ${fmtInt(d.total)})` : ""} · click one for its history and maintenance`}</div>
      <div className="ws-actions">
        <button type="button" className={which === "all" ? "on" : ""} onClick={() => setWhich("all")}>{`All · ${fmtInt(tables.length)}`}</button>
        <button type="button" className={which === "unread" ? "on" : ""} onClick={() => setWhich("unread")}>{`Not read · ${fmtInt(unread.length)}`}</button>
        <button type="button" className={which === "written" ? "on" : ""} onClick={() => setWhich("written")}>{`Written, never read · ${fmtInt(writtenOnly.length)}`}</button>
      </div>
      <div className="pqgs-rt">
        <div className="pqgs-rt-head pqgs-rt-cols-all">
          <span>Table</span><span>Workspace · env</span><span style={{ textAlign: "right" }}>Size</span><span style={{ textAlign: "right" }}>Files</span>
          <span style={{ textAlign: "right" }}>Last read</span><span style={{ textAlign: "right" }}>Last write</span><span style={{ textAlign: "right" }}>Readers</span>
        </div>
        {list.slice(0, n).map((t) => (
          <React.Fragment key={t.table_full_name}>
            <div className="pqgs-rt-row pqgs-rt-cols-all" style={{ cursor: "pointer" }} onClick={() => setOpen(open === t.table_full_name ? null : t.table_full_name)}>
              <span className="pqgs-rt-name">{`${open === t.table_full_name ? "▾" : "▸"} ${t.table_full_name}`}</span>
              <span className="pqgs-rt-note">{t.workspace_name ? wsLabel(t) : "-"}</span>
              <span className="pqgs-rt-num">{t.size_in_bytes == null ? "-" : fmtBytes(t.size_in_bytes)}</span>
              <span className="pqgs-rt-num">{t.num_files == null ? "-" : fmtInt(t.num_files)}</span>
              <span className="pqgs-rt-num">{day(t.last_read)}</span>
              <span className="pqgs-rt-num">{day(t.last_write || t.last_modified)}</span>
              <span className="pqgs-rt-num">{fmtInt(t.readers)}</span>
            </div>
            {open === t.table_full_name && <TableDetail name={t.table_full_name} />}
          </React.Fragment>
        ))}
      </div>
      {list.length > n && <button type="button" className="link-btn" style={{ marginTop: 8 }} onClick={() => setN(n + 200)}>{`Show more (${fmtInt(list.length - n)} left)`}</button>}
    </div>
  );
}

// ─────────── Storage > Table storage: what each workspace holds and whether it's read ───────────
function TableStorageContent({ filters, maxCat, onVerdict }: LooseProps) {
  const sizes = useAsync(() => Api.tableSizes(), []);
  const { ws, d, rows } = useWorkspaceRows(filters, onVerdict, (rs) =>
    `${fmtInt(sumOf(rs, "tables_not_read"))} of ${fmtInt(sumOf(rs, "tables"))} tables not read in the window.`);
  const { picked, setPicked, ref } = usePicked();
  if (ws.phase === "loading") return <span className="muted">Loading table storage...</span>;
  if (!rows.length) return <span className="muted">{notInExport(d && d.info)}</span>;
  const sized = !!d.sized;
  const top = Math.max(maxCat || 8, 8);
  const holding = rows.filter((r) => r.tables);
  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Data held">
          <div className="pqgs-card-value">{sized ? fmtBytes(sumOf(rows, "size_in_bytes") + sumOf(rows, "volume_bytes")) : "-"}</div>
          <PqgsNote facts={[{ label: "Tables", value: fmtBytes(sumOf(rows, "size_in_bytes")) },
            { label: "Volumes", value: `${fmtInt(sumOf(rows, "volumes"))} · ${fmtInt(sumOf(rows, "volume_files"))} files` }]} />
        </Card>
        <Card title="Tables">
          <div className="pqgs-card-value">{fmtInt(sumOf(rows, "tables"))}</div>
          <PqgsNote facts={[{ label: "Views", value: fmtInt(sumOf(rows, "views")) }, { label: "Columns", value: fmtInt(sumOf(rows, "columns")) }]} />
        </Card>
        <Card title="Not read">
          <div className={`pqgs-card-value ${sumOf(rows, "tables_not_read") ? "warn" : "ok"}`}>{fmtInt(sumOf(rows, "tables_not_read"))}</div>
          <PqgsNote facts={[{ label: "Window", value: `${d.window_days} days`, detail: "no read in lineage" }]} />
        </Card>
        <Card title="Workspaces holding data">
          <div className="pqgs-card-value">{fmtInt(holding.length)}</div>
          <PqgsNote facts={[{ label: "Of", value: fmtInt(rows.length), detail: "workspaces shown" }]} />
        </Card>
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Data held by workspace</div>
          {sized ? <HBarList items={[...holding].filter((r) => r.size_in_bytes).sort((a, b) => b.size_in_bytes - a.size_in_bytes).slice(0, top)
            .map((r) => ({ name: wsLabel(r), value: r.size_in_bytes }))} valueFmt={(v) => fmtBytes(v)} />
            : <span className="muted">Table sizes not in this export.</span>}
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Are the tables used? Read and not read, by workspace</div>
          <StackRows rows={[...holding].sort((a, b) => b.tables_not_read - a.tables_not_read).slice(0, top)
            .map((r) => ({ name: wsLabel(r), parts: [
              { label: "read", value: r.tables_read, color: READ_COLOR }, { label: "not read", value: r.tables_not_read, color: UNREAD_COLOR }] }))}
            legend={[{ label: "read in the window", color: READ_COLOR }, { label: "not read", color: UNREAD_COLOR }]} />
        </div>
      </div>
      <WorkspaceList rows={holding} title="Workspaces holding data" sorts={[SORTS.size, SORTS.complexity, SORTS.unread]}
        picked={picked} setPicked={setPicked} note="A workspace holds the catalogs it reads and writes most (lineage). Env stands in for the workspace type." cols={[
          { label: "Env", value: (r) => r.env || "-" },
          { label: "Tables", value: (r) => fmtInt(r.tables) },
          { label: "Views", value: (r) => fmtInt(r.views) },
          { label: "Columns", value: (r) => fmtInt(r.columns) },
          { label: "Size", value: (r) => (sized ? fmtBytes(r.size_in_bytes || 0) : "-") },
          { label: "Volumes", value: (r) => fmtInt(r.volumes) },
          { label: "Volume files", value: (r) => fmtInt(r.volume_files) },
          { label: "Read", value: (r) => fmtInt(r.tables_read) },
          { label: "Not read", value: (r) => fmtInt(r.tables_not_read) },
          { label: "Complexity", value: (r) => (r.complexity_index == null ? "-" : r.complexity_index) },
        ]} />
      <div ref={ref}>{picked && <WorkspaceDetail ws={picked} window={filters.window} maxCat={maxCat} part="tables" />}</div>
      {!picked && <EveryTable filters={filters} />}
      {!picked && <NotDescribed rows={(sizes.phase === "ready" && sizes.data && sizes.data.not_described) || []} />}
    </div>
  );
}

// ─────────── Governance > User activity: who works where, and who stopped coming ───────────
function UserActivityContent({ filters, maxCat, onVerdict }: LooseProps) {
  const { ws, d, rows } = useWorkspaceRows(filters, onVerdict, (rs, dd) =>
    `${fmtInt(sumOf(rs, "active_people"))} active people; ${fmtInt(dd.inactive_total || 0)} inactive for ${dd.inactive_days || 90}+ days.`);
  const { picked, setPicked, ref } = usePicked();
  const [showPeople, setShowPeople] = React.useState(20);
  if (ws.phase === "loading") return <span className="muted">Loading user activity...</span>;
  if (!rows.length) return <span className="muted">{notInExport(d && d.info)}</span>;
  const people: any[] = d.people || [];
  const top = Math.max(maxCat || 8, 8);
  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Active people">
          <div className="pqgs-card-value">{fmtInt(sumOf(rows, "active_people"))}</div>
          <PqgsNote facts={[{ label: "Window", value: `${d.window_days} days`, detail: "signed in or queried" }]} />
        </Card>
        <Card title="Service principals">
          <div className="pqgs-card-value">{fmtInt(sumOf(rows, "active_service_principals"))}</div>
          <PqgsNote facts={[{ label: "Active", value: `${d.window_days} days` }]} />
        </Card>
        <Card title="Queries">
          <div className="pqgs-card-value">{fmtInt(sumOf(rows, "queries"))}</div>
          <PqgsNote facts={[{ label: "From", value: "query history" }]} />
        </Card>
        <Card title="Inactive">
          <div className={`pqgs-card-value ${d.inactive_total ? "warn" : "ok"}`}>{fmtInt(d.inactive_total || 0)}</div>
          <PqgsNote facts={[{ label: "Not seen", value: `${d.inactive_days || 90}+ days`, detail: "within the last year" }]} />
        </Card>
      </div>
      <div className="grid-2">
        <SeenDonut bands={d.seen_bands || []} title="Everyone by last seen" />
        <div className="chart-card">
          <div className="chart-card-title">People by last seen, by workspace</div>
          <StackRows rows={[...rows].filter((r) => r.seen_bands && Object.values(r.seen_bands).some((v: any) => v))
            .sort((a, b) => inactiveIn(b) - inactiveIn(a)).slice(0, top)
            .map((r) => ({ name: wsLabel(r), parts: Object.keys(BAND_COLORS).map((band) => ({ label: band, value: r.seen_bands[band] || 0, color: BAND_COLORS[band] })) }))}
            legend={BAND_LEGEND} />
        </div>
      </div>
      <div className="grid-2" style={{ marginTop: 14 }}>
        <div className="chart-card">
          <div className="chart-card-title">Active people by workspace</div>
          <HBarList items={[...rows].sort((a, b) => b.active_people - a.active_people).slice(0, top)
            .map((r) => ({ name: wsLabel(r), value: r.active_people }))} valueFmt={(v) => fmtInt(v)} />
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Most active people and service principals</div>
          <HBarList items={people.filter((p: any) => p.queries).slice(0, top).map((p: any) => ({
            name: p.identity, value: p.queries, title: `${p.identity} · ${p.workspaces.length} workspace${p.workspaces.length === 1 ? "" : "s"}`,
            color: p.kind === "Service principal" ? "var(--c2)" : undefined }))} valueFmt={(v) => `${fmtInt(v)} queries`} />
          <div className="pqgs-card-note" style={{ marginTop: 6 }}>Orange: a service principal. Hover a name for how many workspaces it uses.</div>
        </div>
      </div>
      <InactiveTable rows={d.inactive || []} total={d.inactive_total || 0} days={d.inactive_days || 90} showWorkspaces />
      <div className="chart-card" style={{ marginTop: 14 }}>
        <div className="chart-card-title">{`Everyone active in the window, all workspaces shown · ${fmtInt(d.people_total || 0)}`}</div>
        <div className="metric-note">Seen signing in, querying or using a table. A workspace's own list and what each one used: click the workspace below.</div>
        <div className="pqgs-rt">
          <div className="pqgs-rt-head pqgs-rt-cols-ppl">
            <span>Who</span><span>Kind</span><span>Workspaces</span><span style={{ textAlign: "right" }}>Last seen</span>
            <span style={{ textAlign: "right" }}>Logins</span><span style={{ textAlign: "right" }}>Queries</span>
            <span style={{ textAlign: "right" }}>Tables read</span>
          </div>
          {people.slice(0, showPeople).map((p: any) => (
            <div className="pqgs-rt-row pqgs-rt-cols-ppl" key={p.identity}>
              <span className="pqgs-rt-name">{p.identity}</span>
              <span>{p.kind}</span>
              <span className="pqgs-rt-note" title={p.workspaces.join(", ")}>{p.workspaces.length > 2 ? `${p.workspaces.slice(0, 2).join(", ")} +${p.workspaces.length - 2}` : p.workspaces.join(", ")}</span>
              <span className="pqgs-rt-num">{lastSeen(p.last_seen)}</span>
              <span className="pqgs-rt-num">{fmtInt(p.logins)}</span>
              <span className="pqgs-rt-num">{fmtInt(p.queries)}</span>
              <span className="pqgs-rt-num">{fmtInt(p.tables_read)}</span>
            </div>
          ))}
        </div>
        {people.length > showPeople && (
          <button type="button" className="link-btn" style={{ marginTop: 8 }} onClick={() => setShowPeople(people.length)}>{`Show all ${fmtInt(people.length)}`}</button>
        )}
      </div>
      <WorkspaceList rows={[...rows].sort((a, b) => b.active_people - a.active_people || b.queries - a.queries)} title="Workspaces by active people"
        picked={picked} setPicked={setPicked} cols={[
          { label: "People", value: (r) => fmtInt(r.active_people) },
          { label: "Service principals", value: (r) => fmtInt(r.active_service_principals) },
          { label: "Queries", value: (r) => fmtInt(r.queries) },
          { label: "Inactive", value: (r) => fmtInt(inactiveIn(r)) },
          { label: "Last activity", value: (r) => lastSeen(r.last_activity) },
          { label: "Usage", value: (r) => r.usage_index },
        ]} />
      <div ref={ref}>{picked && <WorkspaceDetail ws={picked} window={filters.window} maxCat={maxCat} part="people" />}</div>
    </div>
  );
}

AreaContent.register("governance", "workspaces", WorkspacesContent);
AreaContent.register("governance", "user_activity", UserActivityContent);
AreaContent.register("storage", "table_storage", TableStorageContent);
