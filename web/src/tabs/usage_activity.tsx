// Data usage › Activity: which workspaces are busy and how much of each is in use, then inside one
// workspace which tables and people read and write most. Names open Tables, People or Volumes.
import React from "react";

import { Api } from "../api";
import { fmtInt } from "../format";
import { AreaContent, Card, PqgsNote } from "../components/primitives";
import { Donut } from "../components/charts";
import { Columns } from "../components/charts_more";
import { CLASS_META, handoff } from "./tab_usage";

const READ = "#2456A6";
const WRITE = "#D27A2C";
const USERS = "#2E7D4F";
const CLASS_ORDER = ["active", "cooling", "written_never_read", "idle_90d"];

function useActivity(window: number, ids: string[] | null, envs: string[] | null, ws: string | null) {
  const [st, setSt] = React.useState<{ phase: string; data: any; key: string }>({ phase: "loading", data: null, key: "" });
  const key = JSON.stringify([window, ids, envs, ws]);
  React.useEffect(() => {
    let live = true;
    Api.activity(window, ids, envs, ws).then((d: any) => { if (live) setSt({ phase: "ready", data: d, key }); })
      .catch(() => { if (live) setSt({ phase: "error", data: null, key }); });
    return () => { live = false; };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  // the previous pick's data never shows under the new one
  return st.key === key ? st : { phase: "loading", data: null, key };
}

function Link({ text, onClick }: { text: string; onClick: () => void }) {
  return <button type="button" className="ba-pick" title={text} onClick={onClick}
    style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "100%", textAlign: "left" }}>{text}</button>;
}

// Reads and writes stacked per row, one scale for all rows.
function RWBars({ rows }: { rows: { key: string; label: string; sub?: string; r: number; w: number; open?: () => void }[] }) {
  if (!rows.length) return <span className="muted">Nothing read or written in the window.</span>;
  const max = Math.max(1, ...rows.map((x) => x.r + x.w));
  return (
    <div>
      {rows.map((x) => (
        <div key={x.key} style={{ display: "grid", gridTemplateColumns: "minmax(120px, 34%) 1fr 92px", gap: 10, alignItems: "center", margin: "5px 0", fontSize: 13 }}>
          <div style={{ minWidth: 0, overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" }}>
            {x.open ? <Link text={x.label} onClick={x.open} /> : x.label}
            {x.sub ? <span className="muted" style={{ fontSize: 11.5, marginLeft: 6 }}>{x.sub}</span> : null}
          </div>
          <div style={{ display: "flex", height: 14, borderRadius: 3, overflow: "hidden", background: "var(--panel-2, #eee)" }} title={`${fmtInt(x.r)} reads · ${fmtInt(x.w)} writes`}>
            <div style={{ width: `${(100 * x.r) / max}%`, background: READ }} /><div style={{ width: `${(100 * x.w) / max}%`, background: WRITE }} />
          </div>
          <span className="mono muted" style={{ textAlign: "right", fontSize: 12 }}>{`${fmtInt(x.r)} · ${fmtInt(x.w)}`}</span>
        </div>
      ))}
      <Legend />
    </div>
  );
}

function Legend() {
  return (
    <div className="pqgs-card-note" style={{ marginTop: 6 }}>
      <span><span className="legend-dot" style={{ background: READ }} /> Read events</span>
      <span style={{ marginLeft: 12 }}><span className="legend-dot" style={{ background: WRITE }} /> Write events</span>
    </div>
  );
}

// Active of all, per workspace: tables, people with access, volumes.
function InUse({ rows, open }: { rows: any[]; open: (id: string) => void }) {
  const bar = (a: number, of: number, c: string) => (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 52px", gap: 6, alignItems: "center" }} title={`${a} of ${of}`}>
      <div style={{ height: 12, borderRadius: 3, overflow: "hidden", background: "var(--panel-2, #eee)" }}><div style={{ height: "100%", width: `${of ? (100 * a) / of : 0}%`, background: c }} /></div>
      <span className="mono muted" style={{ fontSize: 11.5 }}>{`${a}/${of}`}</span>
    </div>
  );
  const grid = { display: "grid", gridTemplateColumns: "minmax(110px, 24%) 1fr 1fr 1fr", gap: 10, alignItems: "center", margin: "5px 0", fontSize: 13 } as const;
  return (
    <div>
      <div className="muted" style={{ ...grid, fontSize: 12 }}><span /><span>Tables</span><span>People</span><span>Volumes</span></div>
      {rows.map((r) => (
        <div key={r.workspace_id} style={grid}>
          <div style={{ minWidth: 0 }}><Link text={r.workspace_name} onClick={() => open(r.workspace_id)} /></div>
          {bar(r.tables_active, r.tables, READ)}{bar(r.people_active, r.people, USERS)}{bar(r.volumes_active, r.volumes, WRITE)}
        </div>
      ))}
    </div>
  );
}

// Rows against the days of the window; darker = more read and write events.
function Heat({ rows, days, open }: { rows: any[]; days: string[]; open?: (key: string) => void }) {
  const max = Math.max(1, ...rows.flatMap((r) => r.cells));
  if (!rows.some((r) => r.cells.some((v: number) => v))) return <span className="muted">No daily lineage in the window.</span>;
  return (
    <div>
      {rows.map((r) => (
        <div key={r.key} style={{ display: "grid", gridTemplateColumns: "minmax(110px, 28%) 1fr", gap: 10, alignItems: "center", margin: "3px 0", fontSize: 13 }}>
          <div style={{ minWidth: 0, overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" }}>{open ? <Link text={r.label} onClick={() => open(r.key)} /> : r.label}</div>
          <div style={{ display: "grid", gridTemplateColumns: `repeat(${days.length}, 1fr)`, gap: 2 }}>
            {r.cells.map((v: number, i: number) => (
              <span key={i} title={`${days[i]}: ${fmtInt(v)} events`} style={{ height: 16, borderRadius: 2,
                background: v ? `color-mix(in srgb, ${READ} ${15 + (85 * v) / max}%, transparent)` : "var(--panel-2, #eee)" }} />
            ))}
          </div>
        </div>
      ))}
      <div className="pqgs-card-note" style={{ marginTop: 6 }}>{`${days[0]} to ${days[days.length - 1]} · darker = more read and write events`}</div>
    </div>
  );
}

// Read events across, write events up, bubble = people; a click opens it.
function Bubbles({ rows, size, open }: { rows: { key: string; label: string; r: number; w: number; s: number }[]; size: string; open: (key: string) => void }) {
  if (!rows.length) return <span className="muted">Nothing read or written in the window.</span>;
  const W = 620, H = 280, P = 36, R = 110;
  const mx = Math.max(1, ...rows.map((x) => x.r)), my = Math.max(1, ...rows.map((x) => x.w)), ms = Math.max(1, ...rows.map((x) => x.s));
  const X = (v: number) => P + (v / mx) * (W - P - R), Y = (v: number) => H - P - (v / my) * (H - 2 * P);
  const rad = (s: number) => 4 + 14 * Math.sqrt(s / ms);
  const labelled = new Set([...rows].sort((a, b) => b.r + b.w - (a.r + a.w)).slice(0, 12).map((x) => x.key));
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block" }} role="img" aria-label={`Read against write events, bubble size = ${size}`}>
        <line x1={P} y1={H - P} x2={W - 8} y2={H - P} stroke="currentColor" opacity={0.3} />
        <line x1={P} y1={8} x2={P} y2={H - P} stroke="currentColor" opacity={0.3} />
        <text x={W - 8} y={H - 10} textAnchor="end" fontSize={11} fill="currentColor" opacity={0.7}>Read events</text>
        <text x={8} y={18} fontSize={11} fill="currentColor" opacity={0.7}>Write events</text>
        {[...rows].sort((a, b) => b.s - a.s).map((x) => (
          <g key={x.key} style={{ cursor: "pointer" }} onClick={() => open(x.key)}>
            <circle cx={X(x.r)} cy={Y(x.w)} r={rad(x.s)} fill={READ} fillOpacity={0.3} stroke={READ}>
              <title>{`${x.label}: ${fmtInt(x.r)} reads, ${fmtInt(x.w)} writes, ${fmtInt(x.s)} ${size}`}</title>
            </circle>
            {labelled.has(x.key) && <text x={X(x.r) + rad(x.s) + 3} y={Y(x.w) + 4} fontSize={10.5} fill="currentColor">{x.label.length > 22 ? `${x.label.slice(0, 21)}…` : x.label}</text>}
          </g>
        ))}
      </svg>
      <div className="pqgs-card-note">{`Bubble = ${size}. Bottom right: read, rarely written. Top left: written, rarely read.`}</div>
    </div>
  );
}

function Pct({ of }: { of: number }) {
  return <span className="muted" style={{ fontSize: 14, marginLeft: 6 }}>{`of ${fmtInt(of)}`}</span>;
}

function UsageActivityContent({ filters, goTo, onVerdict }: LooseProps) {
  const [ws, setWs] = React.useState<string | null>(null);
  const all = useActivity(filters.window, filters.workspaceIds, filters.envs, null);
  const one = useActivity(filters.window, filters.workspaceIds, filters.envs, ws);
  const d = ws ? (one.phase === "ready" ? one.data : null) : (all.phase === "ready" ? all.data : null);
  const list: any[] = all.phase === "ready" && all.data ? all.data.workspaces || [] : [];
  const t = d ? d.totals : null;
  const verdict = t
    ? `${ws ? d.workspace_name : "All workspaces"}: ${fmtInt(t.tables_active)} of ${fmtInt(t.tables)} tables, ${fmtInt(t.people_active)} of ${fmtInt(t.people)} people and ${fmtInt(t.volumes_active)} of ${fmtInt(t.volumes)} volumes were active in ${filters.window} days.`
    : "Loading activity...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  const toTable = (name: string) => { Object.assign(handoff, { workspace: null, cls: null, table: name }); goTo({ tab: "usage", subtab: "tables" }); };
  const toPerson = (who: string) => { handoff.person = who; goTo({ tab: "usage", subtab: "people" }); };
  const toVolume = (name: string) => { handoff.volume = name; goTo({ tab: "usage", subtab: "volumes" }); };
  if ((ws ? one : all).phase === "loading") return <span className="muted">Loading activity...</span>;
  if (!d || d.outcome === "not_assessed") return <span className="muted">Table reads and writes are not in this export: export again.</span>;
  const pick = (
    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
      {ws ? <button type="button" className="ov-link-btn" onClick={() => setWs(null)}>← All workspaces</button> : null}
      <select value={ws || ""} onChange={(e) => setWs(e.target.value || null)} aria-label="Workspace" style={{ padding: "6px 8px", borderRadius: 8 }}>
        <option value="">All workspaces</option>
        {list.map((r) => <option key={r.workspace_id} value={r.workspace_id}>{r.workspace_name}</option>)}
      </select>
      <span className="muted">{`Read and write events in the last ${filters.window} days, lineage and file events`}</span>
    </div>
  );
  const cards = (
    <div className="pqgs-cards-grid">
      <Card title="Active tables"><div className="pqgs-card-value">{fmtInt(t.tables_active)}<Pct of={t.tables} /></div><PqgsNote facts={[{ label: "Did", value: "read or written" }]} /></Card>
      <Card title="Active people and SPs"><div className="pqgs-card-value">{fmtInt(t.people_active)}<Pct of={t.people} /></div><PqgsNote facts={[{ label: "Of", value: "everyone seen in a year" }]} /></Card>
      <Card title="Active volumes"><div className="pqgs-card-value">{fmtInt(t.volumes_active)}<Pct of={t.volumes} /></div><PqgsNote facts={[{ label: "Did", value: "a file read or written" }]} /></Card>
      <Card title="Table reads"><div className="pqgs-card-value" style={{ color: READ }}>{fmtInt(t.reads)}</div><PqgsNote facts={[{ label: "Writes", value: fmtInt(t.writes) }]} /></Card>
    </div>
  );
  const people = (d.top_people || []).map((p: any) => ({ key: p.identity, label: p.identity, sub: `${p.tables} tables${ws ? "" : ` · ${p.workspaces.length} ws`}`, r: p.reads, w: p.writes, open: () => toPerson(p.identity) }));
  const peopleNote = d.people_days > filters.window ? `Counts over ${d.people_days} days for people who used a table in the window` : "";
  if (!ws) {
    const rows: any[] = d.workspaces || [];
    return (
      <div>
        {pick}{cards}
        <div className="grid-2">
          <Card title="Most active workspaces" right={<span className="muted">tables and files · click one</span>}>
            <RWBars rows={rows.slice(0, 15).map((r) => ({ key: r.workspace_id, label: r.workspace_name, r: r.reads + r.file_reads, w: r.writes + r.file_writes, open: () => setWs(r.workspace_id) }))} />
          </Card>
          <Card title="How much of each workspace is in use" right={<span className="muted">active of all</span>}>
            <InUse rows={rows.slice(0, 15)} open={setWs} />
          </Card>
        </div>
        <div className="grid-2">
          <Card title="Workspaces: reads against writes" right={<span className="muted">click a bubble</span>}>
            <Bubbles rows={rows.filter((r) => r.reads + r.writes).map((r) => ({ key: r.workspace_id, label: r.workspace_name, r: r.reads, w: r.writes, s: r.people_active }))} size="active people" open={setWs} />
          </Card>
          <Card title="Activity per workspace per day" right={<span className="muted">a table counts in the workspace using it most</span>}>
            <Heat rows={(d.heat || []).slice(0, 15)} days={d.days} open={setWs} />
          </Card>
        </div>
        <div className="grid-2">
          <Card title="Most used tables" right={<span className="muted">all workspaces · click one</span>}>
            <RWBars rows={(d.top_tables || []).map((x: any) => ({ key: `${x.workspace_id}|${x.table_full_name}`, label: x.table_full_name, sub: x.workspace_name, r: x.reads, w: x.writes, open: () => toTable(x.table_full_name) }))} />
          </Card>
          <Card title="Most active people" right={<span className="muted">{peopleNote || "all workspaces · click one"}</span>}>
            <RWBars rows={people} />
          </Card>
        </div>
      </div>
    );
  }
  const mix = d.classes || {};
  return (
    <div>
      {pick}{cards}
      <div className="grid-2">
        <Card title="Tables by usage class">
          <Donut segments={CLASS_ORDER.filter((k) => mix[k]).map((k) => ({ label: CLASS_META[k].label, value: mix[k], color: CLASS_META[k].color, display: fmtInt(mix[k]) }))}
            centerLabel={fmtInt(t.tables)} centerSub="tables" />
        </Card>
        <Card title="People seen in this workspace">
          <Donut segments={[{ label: "Active in the window", value: t.people_active, color: USERS, display: fmtInt(t.people_active) },
            { label: "Not seen in the window", value: d.people_unseen, color: CLASS_META.written_never_read.color, display: fmtInt(d.people_unseen) }]}
            centerLabel={fmtInt(t.people)} centerSub="people" />
        </Card>
      </div>
      <div className="grid-2">
        <Card title="Tables: reads against writes" right={<span className="muted">click a bubble</span>}>
          <Bubbles rows={(d.bubbles || []).map((x: any) => ({ key: x.table_full_name, label: x.table_full_name.split(".").slice(1).join("."), r: x.reads, w: x.writes, s: x.people }))} size="people using it" open={toTable} />
        </Card>
        <Card title="Read and write events per day" right={<span className="muted">tables this workspace uses most</span>}>
          <Columns days={(d.daily || []).map((x: any) => x.day)} wide ariaLabel="Read and write events per day"
            series={[{ name: "Reads", values: d.daily.map((x: any) => x.reads), color: READ }, { name: "Writes", values: d.daily.map((x: any) => x.writes), color: WRITE }]}
            valueFmt={(v) => fmtInt(v)} unitLabel="events" />
        </Card>
      </div>
      <div className="grid-2">
        <Card title="Most used tables" right={<span className="muted">click one</span>}>
          <RWBars rows={(d.top_tables || []).map((x: any) => ({ key: x.table_full_name, label: x.table_full_name, sub: `${x.people} people`, r: x.reads, w: x.writes, open: () => toTable(x.table_full_name) }))} />
        </Card>
        <Card title="Most active people" right={<span className="muted">{peopleNote || "click one"}</span>}>
          <RWBars rows={people} />
        </Card>
      </div>
      <div className="grid-2">
        <Card title="Busiest tables per day"><Heat rows={d.heat || []} days={d.days} open={toTable} /></Card>
        <Card title="Volumes" right={<span className="muted">file events · click one</span>}>
          <RWBars rows={(d.volumes || []).map((v: any) => ({ key: v.volume_full_name, label: v.volume_full_name, sub: `${v.people} people`, r: v.reads, w: v.writes, open: () => toVolume(v.volume_full_name) }))} />
        </Card>
      </div>
    </div>
  );
}

AreaContent.register("usage", "activity", UsageActivityContent);
