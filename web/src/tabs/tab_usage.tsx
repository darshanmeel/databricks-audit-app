// Data usage: Workspaces, Tables (click one for its detail), Volumes (click one for who used it) and
// People. Every table and volume falls in one usage class (app/core/usage.py): written never read,
// idle 90d+, cooling or active. Reads and writes come from lineage, volume use from the audit log's
// file events, sizes from DESCRIBE DETAIL and volume listings.
import React from "react";

import { Api } from "../api";
import { fmtBytes, fmtDayShort, fmtDurationMs, fmtInt, fmtPct } from "../format";
import { AreaContent, Card, PqgsNote } from "../components/primitives";
import { HBarList, paletteColor } from "../components/charts";
import { Columns } from "../components/charts_more";
import { csvField } from "../components/findings_table";

const READ = "#2456A6";
const WRITE = "#D27A2C";
const PII_BG = "#EFE8FB";
const PII_INK = "#5B2FA6";
export const CLASS_META: Record<string, { label: string; color: string; bg: string; ink: string; action: string }> = {
  written_never_read: { label: "Written, never read", color: "#C2362B", bg: "#FDE8E6", ink: "#9F1D14", action: "Pause the writer" },
  idle_90d: { label: "Idle 90d+", color: "#8E8B84", bg: "#ECEBE7", ink: "#45433E", action: "Archive or drop" },
  cooling: { label: "Cooling", color: "#E0A93B", bg: "#FBF0D9", ink: "#6E4B00", action: "Ask the owner" },
  active: { label: "Active", color: "#2456A6", bg: "#E4ECF8", ink: "#1D4A8F", action: "Check who reads it" },
};
const CLASS_ORDER = ["written_never_read", "idle_90d", "cooling", "active"];
const PAGE = 50;

// Sub-tab to sub-tab hand-off: a workspace or class picked on Workspaces opens Tables filtered by it;
// a person or volume picked on Activity opens it on People or Volumes.
export const handoff: { workspace?: string | null; cls?: string | null; table?: string | null; person?: string | null; volume?: string | null } = {};

function useApi(fn: () => Promise<any>, key: string) {
  const [state, setState] = React.useState<{ phase: string; data: any }>({ phase: "loading", data: null });
  React.useEffect(() => {
    let live = true;
    setState({ phase: "loading", data: null });
    fn().then((d: any) => { if (live) setState({ phase: "ready", data: d }); })
      .catch(() => { if (live) setState({ phase: "error", data: null }); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

let asOf: string | null = null; // the export's last day: "3d ago" counts back from it

function ago(v: any, end?: string | null): { rel: string; date: string } | null {
  if (!v) return null;
  const d = new Date(String(v).slice(0, 10) + "T00:00:00Z");
  const last = end || asOf;
  const e = last ? new Date(String(last).slice(0, 10) + "T00:00:00Z") : new Date();
  const days = Math.max(0, Math.round((e.getTime() - d.getTime()) / 86400000));
  return { rel: days === 0 ? "today" : `${days}d ago`, date: fmtDayShort(String(v).slice(0, 10)) };
}

function When({ v, never, end }: { v: any; never?: string; end?: string | null }) {
  const a = ago(v, end);
  if (!a) return <span style={{ color: never ? "#C2362B" : undefined }}>{never || "-"}</span>;
  return <span>{a.rel}<span className="muted" style={{ marginLeft: 6, fontSize: 12 }}>{a.date}</span></span>;
}

export function ClassChip({ cls, isNew }: { cls: string | null | undefined; isNew?: boolean }) {
  const m = cls ? CLASS_META[cls] : null;
  if (!m) return <span className="muted">-</span>;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "2px 9px", borderRadius: 999, fontSize: 12, fontWeight: 600, background: m.bg, color: m.ink, whiteSpace: "nowrap" }}>
      <span style={{ width: 7, height: 7, borderRadius: 999, background: m.color }} />{m.label}{isNew ? " · new" : ""}
    </span>
  );
}

function PiiChip({ text }: { text: string }) {
  return <span style={{ display: "inline-block", padding: "2px 8px", borderRadius: 999, fontSize: 12, fontWeight: 600, background: PII_BG, color: PII_INK, whiteSpace: "nowrap" }}>{text}</span>;
}

function RWBar({ r, w, max }: { r: number; w: number; max: number }) {
  const pct = (v: number) => (max > 0 ? Math.max(v > 0 ? 2 : 0, (100 * v) / max) : 0);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 90 }} title={`${fmtInt(r)} reads · ${fmtInt(w)} writes`}>
      <div style={{ height: 6, width: `${pct(r)}%`, background: READ, borderRadius: 3 }} />
      <div style={{ height: 6, width: `${pct(w)}%`, background: WRITE, borderRadius: 3 }} />
    </div>
  );
}

// The tables, volumes or people with the most read and write events, reads and writes stacked; a name opens it.
function MostUsed({ rows, open }: { rows: { name: string; reads: number; writes: number; sub: string }[]; open: (name: string) => void }) {
  if (!rows.length) return <span className="muted">Nothing read or written in the window.</span>;
  const max = Math.max(1, ...rows.map((r) => (r.reads || 0) + (r.writes || 0)));
  return (
    <div>
      {rows.map((r) => (
        <div key={r.name} style={{ display: "grid", gridTemplateColumns: "minmax(140px, 30%) 1fr 110px 150px", gap: 10, alignItems: "center", margin: "5px 0", fontSize: 13 }}>
          <div style={{ minWidth: 0, overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" }}>
            <button type="button" className="ba-pick" title={r.name} onClick={() => open(r.name)}>{r.name}</button>
          </div>
          <div style={{ display: "flex", height: 14, borderRadius: 3, overflow: "hidden", background: "var(--panel-2, #eee)" }} title={`${fmtInt(r.reads)} reads · ${fmtInt(r.writes)} writes`}>
            <div style={{ width: `${(100 * (r.reads || 0)) / max}%`, background: READ }} /><div style={{ width: `${(100 * (r.writes || 0)) / max}%`, background: WRITE }} />
          </div>
          <span className="mono" style={{ textAlign: "right", fontSize: 12 }}><span style={{ color: READ }}>{fmtInt(r.reads)}</span>{" · "}<span style={{ color: WRITE }}>{fmtInt(r.writes)}</span></span>
          <span className="muted" style={{ fontSize: 12 }}>{r.sub}</span>
        </div>
      ))}
      <div className="pqgs-card-note" style={{ marginTop: 6 }}>
        <span><span className="legend-dot" style={{ background: READ }} /> Read events</span>
        <span style={{ marginLeft: 12 }}><span className="legend-dot" style={{ background: WRITE }} /> Write events</span>
      </div>
    </div>
  );
}

function topUsed(list: any[], name: (r: any) => string, sub: (r: any) => string) {
  return list.filter((r) => (r.reads || 0) + (r.writes || 0)).sort((a, b) => (b.reads || 0) + (b.writes || 0) - (a.reads || 0) - (a.writes || 0))
    .slice(0, 10).map((r) => ({ name: name(r), reads: r.reads || 0, writes: r.writes || 0, sub: sub(r) }));
}
const count = (n: number, one: string) => `${fmtInt(n)} ${one}${n === 1 ? "" : "s"}`;
const rwSub = (r: any) => `${count(r.readers || 0, "reader")} · ${count(r.writers || 0, "writer")}`;

function names(n: number | null | undefined, list: string | null | undefined) {
  if (!n) return "0";
  const shown = (list || "").split(", ").filter(Boolean);
  return `${fmtInt(n)}${shown.length ? ` · ${shown.join(", ")}${n > shown.length ? ` +${n - shown.length}` : ""}` : ""}`;
}

function ClassLegend() {
  return (
    <div className="pqgs-card-note" style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
      {CLASS_ORDER.map((k) => <span key={k}><span style={{ display: "inline-block", width: 9, height: 9, borderRadius: 2, background: CLASS_META[k].color, marginRight: 5 }} />{CLASS_META[k].label}</span>)}
    </div>
  );
}

function csv(rows: any[][]): void {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([rows.map((r) => r.map(csvField).join(",")).join("\n")], { type: "text/csv;charset=utf-8;" }));
  a.download = "tables.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

export function envColors(rows: any[]): Record<string, string> {
  const envs = Array.from(new Set(rows.map((r) => r.env || "unknown"))).sort();
  return Object.fromEntries(envs.map((e, i) => [e, paletteColor(i)]));
}

// Each workspace by complexity (x) and usage (y), bubble = data it holds, colour = env; a click picks it.
export function Scatter({ rows, onPick }: { rows: any[]; onPick?: (workspaceId: string) => void }) {
  const W = 960, H = 320, P = 40;
  const colors = envColors(rows);
  const held = (r: any) => (r.size_in_bytes || 0) + (r.volume_bytes || 0);
  const maxSize = Math.max(1, ...rows.map(held));
  const x = (v: number) => P + (v / 100) * (W - 2 * P);
  const y = (v: number) => H - P - (v / 100) * (H - 2 * P);
  const rad = (s: number) => 4 + 18 * Math.sqrt((s || 0) / maxSize);
  return (
    <div style={{ overflowX: "auto" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: W, display: "block" }} role="img" aria-label="Complexity against usage, bubble size = data size">
        <line x1={x(50)} y1={y(0)} x2={x(50)} y2={y(100)} stroke="var(--border)" strokeDasharray="4 4" />
        <line x1={x(0)} y1={y(50)} x2={x(100)} y2={y(50)} stroke="var(--border)" strokeDasharray="4 4" />
        <line x1={x(0)} y1={y(0)} x2={x(100)} y2={y(0)} stroke="currentColor" opacity={0.4} />
        <line x1={x(0)} y1={y(0)} x2={x(0)} y2={y(100)} stroke="currentColor" opacity={0.4} />
        <text x={x(100)} y={H - 8} textAnchor="end" fontSize={11} fill="currentColor" opacity={0.7}>Complexity index</text>
        <text x={8} y={P - 12} fontSize={11} fill="currentColor" opacity={0.7}>Usage index</text>
        <text x={x(98)} y={y(96)} textAnchor="end" fontSize={10} fill="currentColor" opacity={0.5}>built and used</text>
        <text x={x(98)} y={y(4)} textAnchor="end" fontSize={10} fill="currentColor" opacity={0.5}>built, little used</text>
        <text x={x(2)} y={y(4)} fontSize={10} fill="currentColor" opacity={0.5}>small, little used</text>
        {rows.map((r) => (
          <circle key={r.workspace_id} cx={x(r.complexity_index || 0)} cy={y(r.usage_index || 0)} r={rad(held(r))}
            fill={colors[r.env || "unknown"]} fillOpacity={0.55} stroke={colors[r.env || "unknown"]}
            style={onPick ? { cursor: "pointer" } : undefined} onClick={onPick ? () => onPick(String(r.workspace_id)) : undefined}>
            <title>{`${r.workspace_name}${r.env ? ` · ${r.env}` : ""}: complexity ${r.complexity_index ?? "-"}, usage ${r.usage_index}, ${fmtBytes(held(r))} in tables and volumes, ${r.signal}`}</title>
          </circle>
        ))}
      </svg>
      <div className="pqgs-card-note" style={{ marginTop: 6 }}>
        {Object.entries(colors).map(([env, c], k) => <span key={env} style={{ marginLeft: k ? 12 : 0 }}><span className="legend-dot" style={{ background: c }} />{` ${env}`}</span>)}
      </div>
    </div>
  );
}

// Governance › Access: people, service principals and queries per workspace, from the same reader.
export function ActivityByWorkspacePanel({ filters, maxCat }: LooseProps) {
  const key = JSON.stringify([filters.window, filters.workspaceIds, filters.envs, Api.tagParams()]);
  const ws = useApi(() => Api.workspaceUsage(filters.window, filters.workspaceIds, filters.envs), key);
  const d = ws.phase === "ready" ? ws.data : null;
  const rows: any[] = d && d.outcome !== "not_assessed" ? d.rows || [] : [];
  const shown = [...rows].sort((a, b) => b.active_people - a.active_people || b.queries - a.queries).slice(0, Math.max(maxCat || 8, 10));
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">{`Activity by workspace${d && d.window_days ? `, last ${d.window_days} days` : ""}`}</div>
      {ws.phase === "loading" ? <span className="muted">Loading activity...</span>
        : !rows.length ? <span className="muted">Activity per workspace is not in this export: export again.</span> : (
        <React.Fragment>
          <div className="data-table-wrap"><table className="data">
            <thead><tr><th>Workspace</th><th className="num">People</th><th className="num">Service principals</th><th className="num">Queries</th><th>Last activity</th></tr></thead>
            <tbody>{shown.map((r) => (
              <tr key={r.workspace_id}><td title={`${r.workspace_name} (${r.workspace_id})`}>{r.workspace_name}</td>
                <td className="num mono">{fmtInt(r.active_people)}</td><td className="num mono">{fmtInt(r.active_service_principals)}</td>
                <td className="num mono">{fmtInt(r.queries)}</td><td>{r.last_activity ? String(r.last_activity).slice(0, 10) : "-"}</td></tr>
            ))}</tbody>
          </table></div>
          <div className="pqgs-card-note" style={{ marginTop: 8 }}>
            {rows.length > shown.length ? `${fmtInt(rows.length - shown.length)} more workspaces, and who reads and writes what: ` : "Who reads and writes what, and who stopped coming: "}
            <a href={`#tab=usage&w=${filters.window}&subtab=people`}>Data usage › People</a>.
          </div>
        </React.Fragment>
      )}
    </div>
  );
}

// ─────────── Workspaces ───────────
function UsageWorkspacesContent({ filters, goTo, onVerdict }: LooseProps) {
  const [pick, setPick] = React.useState("");
  const ids = pick ? [pick] : filters.workspaceIds;
  const key = JSON.stringify([filters.window, filters.workspaceIds, filters.envs, Api.tagParams()]);
  const picked = JSON.stringify([key, pick]);
  const ws = useApi(() => Api.workspaceUsage(filters.window, filters.workspaceIds, filters.envs), key);
  const tb = useApi(() => Api.tables(ids, filters.envs, filters.window), picked);
  const pp = useApi(() => Api.people(filters.window, ids, filters.envs), picked);
  const [sort, setSort] = React.useState<{ k: string; dir: number }>({ k: "unread_bytes", dir: -1 });
  const w = ws.phase === "ready" ? ws.data : null;
  const t = tb.phase === "ready" ? tb.data : null;
  const p = pp.phase === "ready" ? pp.data : null;
  if (t && t.as_of) asOf = t.as_of;
  const tables: any[] = t && t.outcome !== "not_assessed" ? (t.rows || []).filter((r: any) => !r.view) : [];
  const tot = (t && t.totals) || {};
  const cls = (k: string) => ((t && t.classes) || []).find((c: any) => c.key === k) || { tables: 0, size_in_bytes: 0 };
  const size = tot.size_in_bytes || 0;
  const unreadBytes = cls("written_never_read").size_in_bytes + cls("idle_90d").size_in_bytes;
  const woDaily = cls("written_never_read").tables;
  const noSizes = !!t && !((t.totals || {}).sized);
  const noPii = !!t && t.sensitive_assessed === false;
  const verdict = t && t.total && noSizes
    ? `${fmtInt(t.total)} tables; their sizes are not in this export (Guide › Data usage › Sizes and volume files).`
    : t && t.total
    ? `${fmtInt(t.total)} tables hold ${fmtBytes(size)}. ${fmtBytes(unreadBytes)} (${fmtPct(size ? (100 * unreadBytes) / size : 0, 0)}) is in tables nobody reads${woDaily ? `, and ${fmtInt(woDaily)} of them are still being written` : ""}.`
    : "Loading data usage...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  if (ws.phase === "loading" || tb.phase === "loading") return <span className="muted">Loading data usage...</span>;
  if (!t || t.outcome === "not_assessed") return <span className="muted">Table sizes and usage are not in this export: export again.</span>;

  const toTables = (o: { workspace?: string | null; cls?: string | null; table?: string | null }) => {
    Object.assign(handoff, { workspace: null, cls: null, table: null }, o);
    goTo({ tab: "usage", subtab: "tables" });
  };
  const byClass = CLASS_ORDER.map((k) => ({ k, n: cls(k).tables, bytes: cls(k).size_in_bytes }));
  const reads = tot.reads || 0;
  const writes = tot.writes || 0;
  const sens = tables.filter((r) => r.sensitive && r.sensitive.length);
  const allWs: any[] = w && w.outcome !== "not_assessed" ? w.rows || [] : [];
  const wrows = pick ? allWs.filter((r) => String(r.workspace_id) === pick) : allWs;
  const sorted = [...wrows].sort((a, b) => sort.dir * ((a[sort.k] ?? -1) > (b[sort.k] ?? -1) ? 1 : (a[sort.k] ?? -1) < (b[sort.k] ?? -1) ? -1 : 0));
  const th = (label: string, k: string, num = true) => (
    <th className={num ? "num" : ""} style={{ cursor: "pointer", whiteSpace: "nowrap" }} onClick={() => setSort({ k, dir: sort.k === k ? -sort.dir : -1 })}>
      {label}{sort.k === k ? (sort.dir < 0 ? " ▾" : " ▴") : ""}
    </th>
  );
  const big: any[] = t.largest || [];
  const bigMax = Math.max(1, ...big.map((r) => (r.reads || 0) + (r.writes || 0)));
  // without sizes the split is by table count
  const share = (c: { n: number; bytes: number }) => (noSizes ? (t.total ? (100 * c.n) / t.total : 0) : size ? (100 * c.bytes) / size : 0);
  return (
    <div>
      <div className="ws-actions" style={{ flexWrap: "wrap" }}>
        <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="Workspace" style={{ padding: "6px 8px", borderRadius: 8 }}>
          <option value="">Workspace: all</option>
          {[...allWs].sort((a, b) => String(a.workspace_name).localeCompare(String(b.workspace_name)))
            .map((r) => <option key={r.workspace_id} value={String(r.workspace_id)}>{r.workspace_name}{r.env ? ` · ${r.env}` : ""}</option>)}
        </select>
      </div>
      <Card title={noSizes ? "Where the tables sit, by how they are used" : `Where the ${fmtBytes(size)} sits, by how it is used`}
        right={<span className="muted">{noSizes ? "every table in exactly one class · sizes not in this export" : "every table in exactly one class · size from DESCRIBE DETAIL"}</span>}>
        <div style={{ display: "flex", height: 26, borderRadius: 6, overflow: "hidden", margin: "4px 0 14px" }}>
          {byClass.filter((c) => share(c) > 0).map((c) => (
            <div key={c.k} title={`${CLASS_META[c.k].label}: ${noSizes ? `${fmtInt(c.n)} tables` : fmtBytes(c.bytes)}`} style={{ width: `${share(c)}%`, background: CLASS_META[c.k].color }} />
          ))}
        </div>
        <div className="pqgs-cards-grid">
          {byClass.map((c) => (
            <button key={c.k} type="button" className="card" onClick={() => toTables({ cls: c.k })}
              style={{ textAlign: "left", cursor: "pointer", padding: 14, border: "1px solid var(--border)", borderTop: `4px solid ${CLASS_META[c.k].color}`, borderRadius: 10, background: "var(--surface)", color: "var(--text)", font: "inherit" }}>
              <div style={{ fontWeight: 600 }}>{CLASS_META[c.k].label}</div>
              <div className="pqgs-card-value">{noSizes ? fmtInt(c.n) : fmtBytes(c.bytes)}</div>
              <div className="muted">{noSizes ? `tables · ${fmtPct(share(c), 0)} of tables` : `${fmtInt(c.n)} tables · ${fmtPct(share(c), 0)}`}</div>
              <div style={{ marginTop: 6, fontSize: 12.5, color: "var(--text-2)" }}>{(t.classes.find((x: any) => x.key === c.k) || {}).meaning}</div>
              <div style={{ marginTop: 6, fontSize: 12.5, color: "var(--link)" }}>{`${CLASS_META[c.k].action} →`}</div>
            </button>
          ))}
        </div>
      </Card>

      <div className="pqgs-cards-grid">
        <Card title="Table reads"><div className="pqgs-card-value" style={{ color: READ }}>{fmtInt(reads)}</div>
          <PqgsNote facts={[{ label: "From", value: "lineage", detail: `last ${t.window_days} days` }]} /></Card>
        <Card title="Table writes"><div className="pqgs-card-value" style={{ color: WRITE }}>{fmtInt(writes)}</div>
          <PqgsNote facts={[{ label: "Written never read", value: fmtInt(woDaily), detail: "tables" }]} /></Card>
        <Card title="People reading or writing">
          <div className="pqgs-card-value">{p && p.outcome !== "not_assessed" ? `${fmtInt(p.active_people)} + ${fmtInt(p.service_principals)} SPs` : "-"}</div>
          <PqgsNote facts={[{ label: "Inactive 90d+", value: p && p.inactive ? fmtInt(p.inactive.length) : "-" }]} /></Card>
        <Card title="Tables with sensitive data">
          <div className="pqgs-card-value" style={{ color: PII_INK }}>{noPii ? "-" : fmtInt(tot.sensitive_tables || 0)}</div>
          {noPii ? <div className="muted">Sensitive columns are not in this export: export again.</div>
            : <PqgsNote facts={[{ label: "Nobody reads", value: fmtInt(tot.sensitive_unread || 0), tone: tot.sensitive_unread ? "warn" : "ok" }, { label: "Masked", value: fmtInt(tot.sensitive_masked || 0) }]} />}</Card>
      </div>

      {wrows.length > 1 && (
        <Card title="Complexity against usage" right={<span className="muted">bubble = data held · right and low: built, little used</span>}>
          <Scatter rows={wrows} />
        </Card>
      )}

      <Card title="Every workspace" right={<span className="muted">click one for its tables</span>}>
        <ClassLegend />
        <div className="data-table-wrap">
          <table className="data">
            <thead>
              <tr>
                {th("Workspace", "workspace_name", false)}{th("Tables", "tables")}{th("Size", "size_in_bytes")}
                <th>Tables by class</th>{th("Reads", "reads")}{th("Writes", "writes")}{th("Last read", "last_read", false)}
                {th("Last write", "last_write", false)}{th("People", "active_people")}{th("PII tables", "sensitive_tables")}
                {th("Unread", "unread_bytes")}{th("Complexity", "complexity_index")}{th("Usage", "usage_index")}<th>Signal</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => {
                const mix = r.classes || {};
                const n = CLASS_ORDER.reduce((a, k) => a + (mix[k] || 0), 0);
                return (
                  <tr key={r.workspace_id} className="ba-row" onClick={() => toTables({ workspace: r.workspace_id })} style={{ cursor: "pointer" }}>
                    <td><button type="button" className="ba-pick">{r.workspace_name}</button>{r.env ? <span className="muted">{` · ${r.env}`}</span> : null}</td>
                    <td className="num mono">{fmtInt(r.tables)}</td>
                    <td className="num mono">{fmtBytes(r.size_in_bytes)}</td>
                    <td style={{ minWidth: 140 }}>
                      {n ? <div style={{ display: "flex", height: 10, borderRadius: 3, overflow: "hidden" }} title={CLASS_ORDER.map((k) => `${CLASS_META[k].label}: ${mix[k] || 0}`).join(" · ")}>
                        {CLASS_ORDER.filter((k) => mix[k]).map((k) => <div key={k} style={{ width: `${(100 * mix[k]) / n}%`, background: CLASS_META[k].color }} />)}
                      </div> : <span className="muted">-</span>}
                      {n ? <div className="muted" style={{ fontSize: 11.5 }}>{CLASS_ORDER.map((k) => mix[k] || 0).join(" · ")}</div> : null}
                    </td>
                    <td className="num mono" style={{ color: READ }}>{r.reads == null ? "-" : fmtInt(r.reads)}</td>
                    <td className="num mono" style={{ color: WRITE }}>{r.writes == null ? "-" : fmtInt(r.writes)}</td>
                    <td><When v={r.last_read} never="never" /></td>
                    <td><When v={r.last_write} /></td>
                    <td className="num mono">{`${fmtInt(r.active_people)} · ${fmtInt(r.active_service_principals)} SP`}</td>
                    <td className="num mono">{r.sensitive_tables == null ? "-" : fmtInt(r.sensitive_tables)}</td>
                    <td className="num mono">{r.unread_bytes == null ? "-" : fmtBytes(r.unread_bytes)}</td>
                    <td className="num mono">{r.complexity_index == null ? "-" : r.complexity_index}</td>
                    <td className="num mono">{r.usage_index}</td>
                    <td>{r.signal}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="metric-note">{"Reads and writes are table events in lineage. A table belongs to the workspace that reads and writes its catalog most. Unread = written-never-read + idle tables. Complexity: views, view columns, tables, columns, volume files and size; usage: people, service principals, queries and active days, each against the largest workspace shown."}</div>
      </Card>

      <div className="grid-2">
        <Card title="Clean up first" right={<span className="muted">sensitive and unread first, then class, then size</span>}>
          {(t.clean_up_first || []).length ? (
            <ol style={{ margin: 0, paddingLeft: 20 }}>
              {t.clean_up_first.map((r: any) => (
                <li key={r.table_full_name} style={{ margin: "8px 0" }}>
                  <button type="button" className="ba-pick" onClick={() => toTables({ table: r.table_full_name })}>{r.table_full_name}</button>
                  <span style={{ marginLeft: 8 }}><ClassChip cls={r.usage_class} /></span>
                  {r.sensitive && r.sensitive.length ? <span style={{ marginLeft: 6 }}><PiiChip text={r.sensitive.join(", ")} /></span> : null}
                  <span className="mono muted" style={{ float: "right" }}>{fmtBytes(r.size_in_bytes)}</span>
                </li>
              ))}
            </ol>
          ) : <span className="muted">Nothing to clean up: every table is read.</span>}
        </Card>
        <Card title="Largest tables: reads vs writes" right={<span className="muted">big and unread is the expensive corner</span>}>
          <div className="pqgs-card-note"><span style={{ color: READ }}>■ reads</span>{"  "}<span style={{ color: WRITE }}>■ writes</span></div>
          {big.map((r) => (
            <div key={r.table_full_name} style={{ display: "grid", gridTemplateColumns: "10px minmax(120px, 1fr) 70px 1fr", gap: 8, alignItems: "center", margin: "6px 0", fontSize: 13 }}>
              <span style={{ width: 8, height: 8, borderRadius: 999, background: CLASS_META[r.usage_class].color }} title={CLASS_META[r.usage_class].label} />
              <button type="button" className="ba-pick" style={{ textAlign: "left", overflow: "hidden", textOverflow: "ellipsis" }} onClick={() => toTables({ table: r.table_full_name })}>{r.table_full_name}</button>
              <span className="mono" style={{ textAlign: "right" }}>{fmtBytes(r.size_in_bytes)}</span>
              <RWBar r={r.reads || 0} w={r.writes || 0} max={bigMax} />
            </div>
          ))}
        </Card>
      </div>

      <Card title="Sensitive data" right={<span className="muted">column tags, data classification and column names</span>}>
        {t.sensitive_assessed === false ? <span className="muted">Sensitive columns are not in this export: export again.</span> : <div className="pqgs-cards-grid">
          <div><div className="pqgs-card-value" style={{ color: PII_INK }}>{fmtInt(tot.sensitive_tables || 0)}</div><div className="muted">tables hold email, phone, address, IP, IBAN or similar columns</div></div>
          <div><div className="pqgs-card-value">{fmtInt(tot.sensitive_unread || 0)}</div><div className="muted">{`unread${sens.some((r) => r.usage_class === "written_never_read" || r.usage_class === "idle_90d") ? `: ${sens.filter((r) => r.usage_class === "written_never_read" || r.usage_class === "idle_90d").slice(0, 4).map((r) => r.table_full_name.split(".").pop()).join(", ")}` : ""}`}</div></div>
          <div><div className="pqgs-card-value">{fmtInt(tot.sensitive_outside_prod || 0)}</div><div className="muted">outside prod</div></div>
          <div><div className="pqgs-card-value">{fmtInt(tot.sensitive_masked || 0)}</div><div className="muted">with a column mask</div></div>
        </div>}
      </Card>
    </div>
  );
}

// ─────────── Tables ───────────
function UsageTablesContent({ filters, goTo, onVerdict }: LooseProps) {
  const key = JSON.stringify([filters.window, filters.workspaceIds, filters.envs, Api.tagParams()]);
  const tb = useApi(() => Api.tables(filters.workspaceIds, filters.envs, filters.window), key);
  const [cls, setCls] = React.useState<string>(handoff.cls || "all");
  const [wsPick, setWsPick] = React.useState<string[]>(handoff.workspace ? [handoff.workspace] : []);
  // picked workspaces are counted by the server, so the class counts and largest tables cover all their tables
  const tp = useApi(() => (wsPick.length ? Api.tables(wsPick, filters.envs, filters.window) : Promise.resolve(null)),
    JSON.stringify([filters.window, wsPick, filters.envs, Api.tagParams()]));
  const [q, setQ] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [open, setOpen] = React.useState<string | null>(handoff.table || null);
  React.useEffect(() => { handoff.cls = null; handoff.workspace = null; handoff.table = null; }, []);
  const src = wsPick.length ? tp : tb;
  const t = src.phase === "ready" ? src.data : null;
  const base: any[] = tb.phase === "ready" && tb.data && tb.data.outcome !== "not_assessed" ? tb.data.rows || [] : [];
  const all: any[] = t && t.outcome !== "not_assessed" ? t.rows || [] : [];
  if (t && t.as_of) asOf = t.as_of;
  const verdict = all.length ? "Sorted by what needs attention: written but never read first, then idle, then cooling. Click a table for who reads and writes it." : "Loading tables...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  if (open) return <TableDetail name={open} window={filters.window} goTo={goTo} onBack={() => setOpen(null)} />;
  if (tb.phase === "loading" || src.phase === "loading") return <span className="muted">Loading tables...</span>;
  if (!t || t.outcome === "not_assessed") return <span className="muted">Table sizes are not in this export: export again.</span>;
  const clsOf = (k: string) => ((t.classes || []).find((c: any) => c.key === k) || { tables: 0 }).tables;
  // the counts cover every table in the workspaces shown; the list holds the biggest of each class
  const chips = [{ k: "all", label: "All", n: t.total }, ...CLASS_ORDER.map((k) => ({ k, label: CLASS_META[k].label, n: clsOf(k) })),
    { k: "sensitive", label: "Sensitive data", n: (t.totals || {}).sensitive_tables || 0 }];
  const rows = all.filter((r) => (cls === "all" || (cls === "sensitive" ? r.sensitive && r.sensitive.length : r.usage_class === cls))
    && (!q || r.table_full_name.toLowerCase().includes(q.toLowerCase())));
  const max = Math.max(1, ...rows.map((r) => (r.reads || 0) + (r.writes || 0)));
  const wsOptions: [string, string][] = Array.from(new Map(base.filter((r) => r.workspace_id).map((r) => [r.workspace_id, r.workspace_name])).entries());
  // a catalog no workspace read or wrote in the window has no home: its own pick, so idle tables stay findable
  if (base.some((r) => !r.workspace_id)) wsOptions.push(["none", "No workspace (unused in the window)"]);
  const shown = rows.slice(0, page * PAGE);
  const sum = (k: string) => rows.reduce((a, r) => a + (r[k] || 0), 0);
  const totalSize = (t.totals || {}).size_in_bytes || 0;
  const cut = CLASS_ORDER.filter((k) => clsOf(k) > (t.per_class_limit || Infinity));
  return (
    <div>
      <div className="ws-actions" style={{ flexWrap: "wrap" }}>
        {chips.map((c) => <button key={c.k} type="button" className={cls === c.k ? "on" : ""} onClick={() => { setCls(c.k); setPage(1); }}>{`${c.label} · ${fmtInt(c.n)}`}</button>)}
        <input type="search" placeholder="Search catalog.schema.table" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }}
          style={{ marginLeft: 8, padding: "6px 10px", borderRadius: 8, border: "1px solid var(--border)", minWidth: 220 }} aria-label="Search tables" />
        <WsMulti options={wsOptions} picked={wsPick} onChange={(ids) => { setWsPick(ids); setPage(1); }} />
        <button type="button" className="csv-download-btn" onClick={() => csv([["table", "workspace", "env", "class", "size_bytes", "reads", "writes", "last_read", "last_write", "readers", "writers", "sensitive"],
          ...rows.map((r) => [r.table_full_name, r.workspace_name, r.env, r.usage_class, r.size_in_bytes, r.reads, r.writes, r.last_read, r.last_write, r.readers, r.writers, (r.sensitive || []).join("; ")])])}>Export CSV</button>
      </div>
      <div className="pqgs-cards-grid">
        <Card title="Showing"><div className="pqgs-card-value">{fmtInt(rows.length)}</div><PqgsNote facts={[{ label: "Of", value: `${fmtInt(t.total)} tables` }]} /></Card>
        <Card title="Size">{(t.totals || {}).sized ? <React.Fragment><div className="pqgs-card-value">{fmtBytes(sum("size_in_bytes"))}</div><PqgsNote facts={[{ label: "Of", value: fmtBytes(totalSize) }]} /></React.Fragment>
          : <React.Fragment><div className="pqgs-card-value">-</div><div className="muted">Not in this export: Guide › Data usage › Sizes and volume files.</div></React.Fragment>}</Card>
        <Card title={`Reads · ${t.window_days}d`}><div className="pqgs-card-value" style={{ color: READ }}>{fmtInt(sum("reads"))}</div><PqgsNote facts={[{ label: "From", value: "lineage read events" }]} /></Card>
        <Card title={`Writes · ${t.window_days}d`}><div className="pqgs-card-value" style={{ color: WRITE }}>{fmtInt(sum("writes"))}</div><PqgsNote facts={[{ label: "From", value: "inserts, merges, overwrites" }]} /></Card>
      </div>
      <div className="grid-2">
        <Card title="Tables by class" right={<span className="muted">every table, not only the list below</span>}>
          <HBarList items={CLASS_ORDER.map((k) => ({ name: CLASS_META[k].label, value: clsOf(k), color: CLASS_META[k].color }))}
            valueFmt={(v) => `${fmtInt(v)} tables`} />
          <div className="metric-note">{`${fmtBytes((t.totals || {}).size_in_bytes || 0)} measured in ${fmtInt((t.totals || {}).sized || 0)} of ${fmtInt(t.total)} tables; the rest weren't described.`}</div>
        </Card>
        <Card title="Largest tables" right={<span className="muted">colour = class</span>}>
          {(t.largest || []).length ? <HBarList items={(t.largest || []).map((r: any) => ({ name: r.table_full_name, value: r.size_in_bytes || 0, color: CLASS_META[r.usage_class].color }))}
            valueFmt={(v) => fmtBytes(v)} /> : <span className="muted">No table sizes in this export: run the sizes step.</span>}
        </Card>
      </div>
      <Card title={`Most active tables · ${t.window_days}d`} right={<span className="muted">{cls !== "all" || q ? "the tables shown below · click one" : "every table · click one"}</span>}>
        <MostUsed open={setOpen} rows={topUsed(cls !== "all" || q ? rows : t.most_used || [], (r) => r.table_full_name, rwSub)} />
      </Card>
      {wsPick.length < 2 && wsPick[0] !== "none" ? <SchemaBreakdown workspaceId={wsPick[0] || ""} name={(wsOptions.find(([id]) => id === wsPick[0]) || [])[1] || wsPick[0]} window={filters.window} /> : null}
      <Card title="Tables" right={<span className="muted">click one for its detail</span>}>
        <div className="data-table-wrap">
          <table className="data">
            <thead><tr><th>Table</th><th>Class</th><th className="num">Size</th><th className="num">Reads</th><th className="num">Writes</th><th>Read / write</th>
              <th>Last read</th><th>Last write</th><th>Readers</th><th>Writers</th><th>Sensitive</th></tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.table_full_name} className="ba-row" style={{ cursor: "pointer" }} onClick={() => setOpen(r.table_full_name)}>
                  <td><button type="button" className="ba-pick">{r.table_full_name}</button>
                    <div className="muted" style={{ fontSize: 12 }}>{`${r.workspace_name || "no workspace"}${r.env ? ` · ${r.env}` : ""}${r.view ? " · view" : ""}`}</div></td>
                  <td><ClassChip cls={r.usage_class} isNew={r.new} /></td>
                  <td className="num mono">{r.view ? "-" : fmtBytes(r.size_in_bytes)}</td>
                  <td className="num mono" style={{ color: READ }}>{fmtInt(r.reads)}</td>
                  <td className="num mono" style={{ color: WRITE }}>{fmtInt(r.writes)}</td>
                  <td><RWBar r={r.reads || 0} w={r.writes || 0} max={max} /></td>
                  <td><When v={r.last_read} never="never (365d)" /></td>
                  <td><When v={r.last_write} /></td>
                  <td style={{ fontSize: 12.5 }}>{names(r.readers, r.reader_names)}</td>
                  <td style={{ fontSize: 12.5 }}>{r.writers == null ? "-" : names(r.writers, r.writer_names)}</td>
                  <td>{r.sensitive && r.sensitive.length ? <PiiChip text={Array.from(new Set(r.sensitive)).join(", ")} /> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.length > shown.length && <button type="button" className="ov-link-btn" onClick={() => setPage(page + 1)}>{`Show ${Math.min(PAGE, rows.length - shown.length)} more of ${fmtInt(rows.length - shown.length)}`}</button>}
        <div className="metric-note">{"Reads and writes: lineage · last read and write look back 365 days · SP = service principal"}</div>
        {cut.length ? <div className="metric-note">{`The list holds the biggest ${fmtInt(t.per_class_limit)} tables of ${cut.map((k) => `${CLASS_META[k].label.toLowerCase()} (${fmtInt(clsOf(k))})`).join(", ")}; the counts above cover them all. Search and the workspace pick look in the list.`}</div> : null}
        {(t.not_described || []).length ? (
          <div className="metric-note">{`Could not describe: ${t.not_described.slice(0, 5).map((f: any) => `${f.table_catalog}.${f.table_schema} (${fmtInt(f.tables)}, ${f.error})`).join(" · ")}`}</div>
        ) : null}
      </Card>
    </div>
  );
}

// Pick one or more workspaces: a checkbox list; none picked = every workspace in the top-bar filter.
function WsMulti({ options, picked, onChange }: { options: [string, string][]; picked: string[]; onChange: (ids: string[]) => void }) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const label = !picked.length ? "all" : picked.length === 1 ? (options.find(([id]) => id === picked[0]) || [])[1] || picked[0] : `${picked.length} picked`;
  return (
    <div className="ws-picker" ref={ref}>
      <button type="button" className="ws-trigger" onClick={() => setOpen((o) => !o)}>{`Workspace: ${label}`}<span className="caret">▾</span></button>
      {open && (
        <div className="ws-pop">
          <div className="ws-actions"><button type="button" onClick={() => onChange([])}>All</button></div>
          <div className="ws-list">{options.map(([id, name]) => (
            <label key={id} className="ws-row">
              <input type="checkbox" checked={picked.includes(id)} onChange={() => onChange(picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id])} />
              <span className="ws-name">{name || id}</span>
            </label>
          ))}</div>
        </div>
      )}
    </div>
  );
}

// Schema by schema, for one workspace or the biggest of every workspace: what is built, what is served, how it is used.
function SchemaBreakdown({ workspaceId, name, window }: { workspaceId: string; name: string; window: number }) {
  const st = useApi(() => Api.workspaceSchemas(workspaceId, window), `${workspaceId}|${window}`);
  const d = st.phase === "ready" ? st.data : null;
  const rows: any[] = d ? d.schemas || [] : [];
  if (st.phase === "loading" || !rows.length) return null;
  const all = !workspaceId;
  return (
    <Card title={all ? `The biggest ${fmtInt(rows.length)} of ${fmtInt(d.total)} schemas, every workspace` : `${name} · by schema`}
      right={<span className="muted">{all ? "pick a workspace for all its schemas" : `catalogs it holds: ${d.catalogs.join(", ")}`}</span>}>
      <ClassLegend />
      <div className="data-table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}><table className="data">
        <thead><tr><th>Schema</th>{all ? <th>Workspace</th> : null}<th className="num">Tables</th><th>Tables by class</th><th className="num">Columns</th><th className="num">Views</th>
          <th className="num">View columns</th><th className="num">Volumes</th><th className="num">Volume files</th><th className="num">Size</th></tr></thead>
        <tbody>{rows.map((r) => {
          const n = CLASS_ORDER.reduce((a, k) => a + (r.classes[k] || 0), 0);
          return (
            <tr key={`${r.table_catalog}.${r.table_schema}`}>
              <td className="mono">{`${r.table_catalog}.${r.table_schema}`}</td>
              {all ? <td>{r.workspace_name || "-"}</td> : null}
              <td className="num mono">{fmtInt(r.tables)}</td>
              <td style={{ minWidth: 120 }}>{n ? <div style={{ display: "flex", height: 10, borderRadius: 3, overflow: "hidden" }} title={CLASS_ORDER.map((k) => `${CLASS_META[k].label}: ${r.classes[k] || 0}`).join(" · ")}>
                {CLASS_ORDER.filter((k) => r.classes[k]).map((k) => <div key={k} style={{ width: `${(100 * r.classes[k]) / n}%`, background: CLASS_META[k].color }} />)}
              </div> : <span className="muted">-</span>}</td>
              <td className="num mono">{fmtInt(r.table_columns)}</td>
              <td className="num mono">{fmtInt(r.views)}</td>
              <td className="num mono">{fmtInt(r.view_columns)}</td>
              <td className="num mono">{fmtInt(r.volumes)}</td>
              <td className="num mono">{fmtInt(r.volume_files)}</td>
              <td className="num mono">{fmtBytes(r.size_in_bytes + r.volume_bytes)}</td>
            </tr>
          );
        })}</tbody>
      </table></div>
      <div className="metric-note">Views serve data from elsewhere; tables and volumes are built here. Size is tables plus volume files.</div>
    </Card>
  );
}

// ─────────── Table detail ───────────
// every day of the window up to the export's last day; lineage only has the days with an event
function windowDays(daily: any[], window: number): any[] {
  const byDay = new Map(daily.map((d) => [String(d.day).slice(0, 10), d]));
  const end = new Date(String(asOf || (daily.length ? daily[daily.length - 1].day : new Date().toISOString())).slice(0, 10) + "T00:00:00Z");
  return Array.from({ length: window }, (_, i) => {
    const day = new Date(end.getTime() - (window - 1 - i) * 86400000).toISOString().slice(0, 10);
    const d: any = byDay.get(day) || {};
    return { day, reads: d.reads || 0, writes: d.writes || 0 };
  });
}

function Mirror({ daily, window }: { daily: any[]; window: number }) {
  const last = windowDays(daily, window);
  if (!last.some((d) => d.reads + d.writes)) return <span className="muted">{`No read or write in the last ${fmtInt(window)} days.`}</span>;
  const reads = last.reduce((a, d) => a + (d.reads || 0), 0), writes = last.reduce((a, d) => a + (d.writes || 0), 0);
  return (
    <Columns days={last.map((d) => d.day)} wide={last.length <= 31} ariaLabel="Reads and writes per day"
      series={[{ name: "Reads", values: last.map((d) => d.reads || 0), color: READ }, { name: "Writes", values: last.map((d) => d.writes || 0), color: WRITE }]}
      valueFmt={(v) => fmtInt(v)} unitLabel="events"
      note={`${fmtInt(reads)} reads and ${fmtInt(writes)} writes in ${fmtInt(last.length)} days${reads ? "" : ": nobody read it in the window"}.`} />
  );
}

function Node({ n, here }: { n: any; here?: boolean }) {
  const m = n.usage_class ? CLASS_META[n.usage_class] : null;
  return (
    <div style={{ border: `1px solid ${here ? "var(--text)" : "var(--border)"}`, borderRadius: 8, padding: "6px 10px", margin: "4px 0", background: "var(--surface)", fontSize: 13 }}>
      <div className="mono" style={{ overflowWrap: "anywhere" }}>{n.name}</div>
      <div className="muted" style={{ fontSize: 12 }}>{m ? m.label : here ? "this table" : "not sized"}{n.events ? ` · ${fmtInt(n.events)} events` : ""}</div>
    </div>
  );
}

// how much faster reads got after a compaction: 44.6 s -> 4.1 s (-91%)
function Change({ a, b, fmt }: { a: number; b: number; fmt: (v: number) => string }) {
  const pct = a ? Math.round((100 * (b - a)) / a) : 0;
  return <span>{`${fmt(a)} → ${fmt(b)} `}<span style={{ color: pct < 0 ? "#1D7A46" : pct > 0 ? "#9F1D14" : undefined }}>{`(${pct > 0 ? "+" : ""}${pct}%)`}</span></span>;
}

const MAINT_OPS = /^(OPTIMIZE|VACUUM|COMPACTION|CLUSTERING|ANALYZE|AUTO_CLUSTERING|REORG|FSCK)/i;

// Changes and maintenance: DESCRIBE HISTORY and predictive optimization as one list, what OPTIMIZE
// and VACUUM did, and reads before and after each compaction.
function Upkeep({ d, window }: { d: any; window: number }) {
  const [only, setOnly] = React.useState("all");
  const [all, setAll] = React.useState(false);
  const tl: any[] = d.timeline || [];
  const u = d.upkeep || {};
  const shown = tl.filter((e) => only === "all" || (only === "maint" ? MAINT_OPS.test(e.operation) : !MAINT_OPS.test(e.operation)));
  const effect = tl.find((e) => e.effect);
  const since = new Date(Date.parse(String(d.as_of || "")) - window * 864e5).toISOString().slice(0, 10);
  return (
    <Card title="Changes and maintenance" right={<span className="muted">DESCRIBE HISTORY and predictive optimization · newest first</span>}>
      {!d.has_history ? <div className="muted" style={{ marginBottom: 8 }}>Writes, merges, OPTIMIZE and VACUUM come from DESCRIBE HISTORY, which runs with the sizes: not in this export (Guide › Data usage › Sizes and volume files).</div> : null}
      <div className="pqgs-cards-grid">
        <Card title={`Compactions · ${window}d`}><div className="pqgs-card-value">{fmtInt(u.compactions || 0)}</div>
          <PqgsNote facts={[{ label: "Files", value: u.files_merged ? `${fmtInt(u.files_merged)} → ${fmtInt(u.files_after)}` : "-" }]} /></Card>
        <Card title={`Freed by VACUUM · ${window}d`}><div className="pqgs-card-value">{u.freed_bytes ? fmtBytes(u.freed_bytes) : "-"}</div>
          <PqgsNote facts={[{ label: "Runs", value: fmtInt(u.vacuums || 0) }, { label: "Files deleted", value: u.files_deleted ? fmtInt(u.files_deleted) : "-" }]} /></Card>
        <Card title="Reads after the last compaction"><div className="pqgs-card-value" style={{ fontSize: 22, whiteSpace: "nowrap" }}>{effect ? <Change a={effect.effect.before.avg_ms} b={effect.effect.after.avg_ms} fmt={(v) => fmtDurationMs(v)} /> : "-"}</div>
          <PqgsNote facts={effect ? [{ label: "Files per read", value: `${fmtInt(Math.round(effect.effect.before.files_per_read))} → ${fmtInt(Math.round(effect.effect.after.files_per_read))}` }]
            : [{ label: "Needs", value: "3+ reads in the 7 days before and after" }]} /></Card>
        {u.history_in_window ? (
          <Card title={`Writes and merges · ${window}d`}><div className="pqgs-card-value">{fmtInt(u.writes || 0)}</div>
            <PqgsNote facts={[{ label: "Write events", value: u.lineage_writes == null ? "-" : `${fmtInt(u.lineage_writes)} (lineage)` },
              { label: "Predictive optimization", value: u.dbu ? `${u.dbu} DBU (est.)` : "-" }]} /></Card>
        ) : (
          <Card title={`Write events · ${window}d`}><div className="pqgs-card-value">{u.lineage_writes == null ? "-" : fmtInt(u.lineage_writes)}</div>
            <PqgsNote facts={[{ label: "From", value: "lineage: DESCRIBE HISTORY has nothing for it in the window" },
              { label: "Predictive optimization", value: u.dbu ? `${u.dbu} DBU (est.)` : "-" }]} /></Card>
        )}
      </div>
      {u.history_since && u.history_since > since ? <div className="metric-note">{`History reaches back to ${fmtDayShort(u.history_since)} only: changes before it in the window are not available.`}</div> : null}
      {tl.length ? (
        <React.Fragment>
          <div className="ws-actions" style={{ margin: "10px 0 6px" }}>
            {[["all", "All"], ["maint", "Maintenance"], ["changes", "Writes and merges"]].map(([k, l]) => (
              <button key={k} type="button" className={only === k ? "on" : ""} onClick={() => setOnly(k)}>{l}</button>))}
          </div>
          <div className="data-table-wrap"><table className="data">
            <thead><tr><th>When</th><th>Operation</th><th>Who</th><th className="num">Files</th><th className="num">Bytes</th><th className="num">Rows</th><th>Reads before → after</th></tr></thead>
            <tbody>{shown.slice(0, all ? shown.length : 30).map((e, i) => (
              <tr key={i} title={e.metrics || ""}>
                <td>{String(e.when || "").slice(0, 16)}</td>
                <td>{e.operation}{e.by === "predictive" ? <span className="muted" style={{ fontSize: 12 }}> · predictive{e.status && e.status !== "SUCCESSFUL" ? ` · ${e.status.toLowerCase()}` : ""}</span> : null}</td>
                <td style={{ fontSize: 12.5 }}>{e.by === "predictive" ? (e.dbu != null ? `${Number(e.dbu).toFixed(2)} DBU (est.)` : "-") : e.who || "-"}</td>
                <td className="num mono">{e.files_added == null && e.files_removed == null ? "-" : `+${fmtInt(e.files_added || 0)} −${fmtInt(e.files_removed || 0)}`}</td>
                <td className="num mono">{e.freed_bytes ? `${fmtBytes(e.freed_bytes)} freed` : e.bytes_added == null && e.bytes_removed == null ? "-" : `+${fmtBytes(e.bytes_added || 0)} −${fmtBytes(e.bytes_removed || 0)}`}</td>
                <td className="num mono">{e.rows == null ? "-" : fmtInt(e.rows)}</td>
                <td style={{ fontSize: 12.5 }}>{e.effect ? <span><Change a={e.effect.before.avg_ms} b={e.effect.after.avg_ms} fmt={(v) => fmtDurationMs(v)} />{` · files ${fmtInt(Math.round(e.effect.before.files_per_read))} → ${fmtInt(Math.round(e.effect.after.files_per_read))}`}</span> : ""}</td>
              </tr>
            ))}</tbody>
          </table></div>
          {shown.length > 30 && !all ? <button type="button" className="ov-link-btn" onClick={() => setAll(true)}>{`Show all ${fmtInt(shown.length)}`}</button> : null}
          <ul className="metric-note" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            <li>{`DESCRIBE HISTORY keeps the last 100 operations per table${u.history_since ? `, here back to ${fmtDayShort(u.history_since)}` : ""}; older ones are not available.`}</li>
            <li>OPTIMIZE frees no storage by itself: the merged files go when VACUUM runs. Bytes freed come from VACUUM's own metrics.</li>
            <li>Reads before and after: 7 days each side, 3+ reads on both, only reads in query history. An indication: other changes count too.</li>
            <li>Predictive optimization DBUs are its own estimate, not billed figures.</li>
          </ul>
        </React.Fragment>
      ) : <span className="muted">{u.lineage_writes ? `No DESCRIBE HISTORY or predictive optimization entry in this export; the ${fmtInt(u.lineage_writes)} write events above are from lineage.`
        : "No write, merge or maintenance run in this export."}</span>}
    </Card>
  );
}

// the query's profile in its workspace's query history
function profileUrl(q: any): string | null {
  if (!q.workspace_url || !q.statement_id) return null;
  const host = String(q.workspace_url).replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  return `https://${host}/sql/history?queryId=${encodeURIComponent(q.statement_id)}&uiQueryProfileVisible=true`;
}

function timeParts(q: any): string {
  const parts: [string, any][] = [["compile", q.compile_ms], ["run", q.exec_ms], ["task time over all cores", q.task_ms]];
  const out = parts.filter(([, v]) => v != null).map(([k, v]) => `${k} ${fmtDurationMs(v)}`);
  if (q.cache_pct != null) out.push(`${Math.round(q.cache_pct)}% read from disk cache`);
  if (q.result_cache) out.push("served from the result cache");
  if (q.shuffle_bytes) out.push(`${fmtBytes(q.shuffle_bytes)} shuffled`);
  if (q.written_bytes) out.push(`${fmtBytes(q.written_bytes)} written`);
  return out.join(" · ");
}

// The slowest queries on the table in the window, and reads that pruned no file.
function TableQueries({ d, window }: { d: any; window: number }) {
  const qs: any[] = d.queries || [];
  const sc = d.scans;
  const full = (q: any) => q.access === "read" && !q.pruned_files && (q.read_files || 0) >= 50;
  const other: any[] = d.other || [];
  const na = <span className="muted">not available</span>;
  return (
    <Card title={`Slowest queries · ${window}d`} right={<span className="muted">query history joined to lineage by statement</span>}>
      {!d.has_queries ? <span className="muted">Queries per table are not in this export: export again.</span> : !qs.length ? <span className="muted">No query in query history read or wrote it in the window.</span> : (
        <React.Fragment>
          {sc ? <div style={{ marginBottom: 8 }}>{`${fmtInt(sc.queries)} reads, ${fmtDurationMs(sc.avg_ms)} on average, ${fmtBytes(sc.read_bytes)} read. `}
            <span style={{ color: sc.full_scans ? "#9F1D14" : undefined, fontWeight: sc.full_scans ? 600 : undefined }}>{`${fmtInt(sc.full_scans)} pruned no file`}</span>
            {sc.pruned_share != null ? `; ${fmtPct(sc.pruned_share, 0)} of files skipped overall.` : "."}</div> : null}
          <div className="data-table-wrap"><table className="data">
            <thead><tr><th>When</th><th>Who</th><th>Type</th><th className="num">Time</th><th className="num">Waited</th><th className="num">Read</th><th className="num">Spilled</th><th className="num">Files read</th><th className="num">Pruned</th><th>Query</th></tr></thead>
            <tbody>{qs.map((q) => (
              <tr key={q.statement_id}>
                <td>{String(q.start_time || "").slice(0, 16)}<div className="muted" style={{ fontSize: 12 }}>{[q.workspace_name, q.warehouse_name].filter(Boolean).join(" · ")}</div></td>
                <td style={{ fontSize: 12.5 }}>{q.identity}</td>
                <td>{q.statement_type}{q.access === "write" ? <span className="muted"> · writes it</span> : null}{q.status && q.status !== "FINISHED" ? <span className="muted">{` · ${String(q.status).toLowerCase()}`}</span> : null}</td>
                <td className="num mono" title={timeParts(q)}>{fmtDurationMs(q.total_ms)}</td>
                <td className="num mono" title="Waiting for compute plus queued at capacity">{q.queue_ms == null && q.wait_compute_ms == null ? "-" : fmtDurationMs((q.queue_ms || 0) + (q.wait_compute_ms || 0))}</td>
                <td className="num mono">{q.read_bytes == null ? "-" : fmtBytes(q.read_bytes)}</td>
                <td className="num mono" style={{ color: q.spilled_bytes ? "#9F1D14" : undefined }}>{q.spilled_bytes == null ? "-" : q.spilled_bytes ? fmtBytes(q.spilled_bytes) : "0"}</td>
                <td className="num mono">{q.read_files == null ? "-" : fmtInt(q.read_files)}</td>
                <td className="num mono">{full(q) ? <span className="badge" style={{ color: "#9F1D14" }}>none · full scan</span> : q.pruned_files == null ? "-" : fmtInt(q.pruned_files)}</td>
                <td className="mono" style={{ fontSize: 12, maxWidth: 360 }}>
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={q.text || ""}>{q.text || "-"}</div>
                  <div className="muted" style={{ fontSize: 11.5 }}>{q.statement_id}{profileUrl(q) ? <React.Fragment>{" · "}<a href={profileUrl(q)!} target="_blank" rel="noreferrer">profile ↗</a></React.Fragment> : null}</div>
                </td>
              </tr>
            ))}</tbody>
          </table></div>
        </React.Fragment>
      )}
      {d.has_other ? (
        <div style={{ marginTop: 14 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{`Not in query history · ${fmtInt(d.other_events || 0)} reads and writes`}</div>
          {other.length ? (
            <div className="data-table-wrap"><table className="data">
              <thead><tr><th>Who</th><th>Through</th><th>Access</th><th className="num">Events</th><th>Last</th><th>Time, bytes, files</th></tr></thead>
              <tbody>{other.map((o, i) => (
                <tr key={i}><td style={{ fontSize: 12.5 }}>{o.identity || "-"}</td><td>{o.via}</td><td>{o.access === "write" ? "Writes" : "Reads"}</td>
                  <td className="num mono">{fmtInt(o.events)}</td><td><When v={o.last_event} /></td><td>{na}</td></tr>
              ))}</tbody>
            </table></div>
          ) : <span className="muted">None: every read and write in the window is in query history.</span>}
        </div>
      ) : null}
      <ul className="metric-note" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
        <li>Query history has SQL warehouses and serverless only. Jobs, notebooks and pipelines on classic compute are in lineage with who and when; their time, bytes and files are not available.</li>
        <li>The 10 slowest per window; bytes and files are the whole query's, so a join shows them on each table it reads.</li>
        <li>Full scan: 50+ files read and none pruned. Filter on a clustering or partition column.</li>
        <li>Spilled: data that did not fit in memory and went to disk; a bigger warehouse or a narrower query avoids it. Waited: for compute to start plus queued at capacity. Hover Time for compile, run and cache.</li>
        <li>Reads that never reach Unity Catalog lineage (paths, external engines) are not counted.</li>
      </ul>
    </Card>
  );
}

// a person's row on People, opened
function toPerson(goTo: any, who: string) {
  handoff.person = who;
  goTo({ tab: "usage", subtab: "people" });
}

function TableDetail({ name, window, goTo, onBack }: { name: string; window: number; goTo: any; onBack: () => void }) {
  const st = useApi(() => Api.table(name, window), `${name}|${window}`);
  const [copied, setCopied] = React.useState(false);
  if (st.phase === "loading") return <span className="muted">Loading the table...</span>;
  if (!st.data) return <span className="muted">Could not load this table.</span>;
  const d = st.data;
  if (d.as_of) asOf = d.as_of;
  const f = d.facts || {};
  const sens: any[] = d.sensitive || [];
  const unmasked = sens.filter((s) => !s.masked);
  const users: any[] = d.users || [];
  const readers = users.filter((u) => u.reads > 0), writers = users.filter((u) => u.writes > 0);
  const lastReader = [...readers].sort((a, b) => String(b.last_access).localeCompare(String(a.last_access)))[0];
  const lastWriter = [...writers].sort((a, b) => String(b.last_access).localeCompare(String(a.last_access)))[0];
  const gb = (f.size_in_bytes || 0) / 1e9;
  const filesPerGb = gb > 0 ? (f.num_files || 0) / gb : 0;
  const optimized = (d.history || []).some((h: any) => /OPTIMIZE|VACUUM/.test(h.operation || "")) || (d.maintenance || []).length > 0;
  const todo: { text: string; sql: string }[] = [];
  if (unmasked.length) todo.push({ text: `Add column masks on ${unmasked.map((s) => s.column_name).join(", ")}: ${fmtInt(readers.length)} readers, none needs raw values to count rows.`,
    sql: unmasked.map((s) => `ALTER TABLE ${name} ALTER COLUMN ${s.column_name} SET MASK <your_mask_function>;`).join("\n") });
  if ((d.unused_grants || []).length) todo.push({ text: `Revoke SELECT from ${d.unused_grants.map((g: any) => g.grantee).join(", ")}: no reads in the longest exported window.`,
    sql: d.unused_grants.map((g: any) => `REVOKE SELECT ON TABLE ${name} FROM \`${g.grantee}\`;`).join("\n") });
  if (d.usage_class === "written_never_read") todo.push({ text: `Pause the writer: ${writers.map((u) => u.identity).join(", ") || "its job"} writes it and nobody reads it.`, sql: `-- pause the job or pipeline that writes ${name}` });
  if (d.usage_class === "idle_90d") todo.push({ text: "Archive or drop it: no read and no write in 90 days.", sql: `-- after checking with the owner:\n-- DROP TABLE ${name};` });
  const sc = d.scans;
  if (sc && sc.full_scans >= 5 && sc.full_scans >= sc.queries / 5) todo.push({ text: `Cluster it by the columns its reads filter on: ${fmtInt(sc.full_scans)} of ${fmtInt(sc.queries)} reads in ${window} days pruned no file.`,
    sql: `ALTER TABLE ${name} CLUSTER BY (<columns the reads filter on>);\nOPTIMIZE ${name};` });
  if (filesPerGb > 200 && f.num_files > 50 && !optimized) todo.push({ text: `Run OPTIMIZE: ${fmtInt(f.num_files)} files for ${fmtBytes(f.size_in_bytes)}, never optimized or vacuumed.`, sql: `OPTIMIZE ${name};` });
  const copy = () => { navigator.clipboard?.writeText(todo.map((x) => x.sql).join("\n\n")); setCopied(true); };
  const [cat, sch] = name.split(".");
  return (
    <div>
      <button type="button" className="ov-link-btn" onClick={onBack}>← All tables</button>
      <Card title={name} right={<span className="muted">{`${cat} · ${sch}`}</span>}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <ClassChip cls={d.usage_class} isNew={d.new} />
          {sens.length ? <PiiChip text={`${sens.length} sensitive column${sens.length === 1 ? "" : "s"} · ${unmasked.length ? "unmasked" : "masked"}`} /> : null}
          {[d.workspace_name ? `${d.workspace_name}${d.env ? ` · ${d.env}` : ""}` : null, [f.table_type, f.data_source_format].filter(Boolean).join(" · "),
            f.column_count != null ? `${fmtInt(f.column_count)} columns` : null, f.size_in_bytes != null ? `${fmtBytes(f.size_in_bytes)} · ${fmtInt(f.num_files)} files` : null,
            f.owner ? `Owner: ${f.owner}` : null, f.created ? `Created ${fmtDayShort(String(f.created).slice(0, 10))}` : null].filter(Boolean)
            .map((x) => <span key={String(x)} className="badge">{x}</span>)}
        </div>
      </Card>
      <div className="pqgs-cards-grid">
        <Card title={`Reads · ${window}d`}><div className="pqgs-card-value" style={{ color: READ }}>{fmtInt(d.reads)}</div><PqgsNote facts={[{ label: "By", value: `${fmtInt(d.readers)} readers` }]} /></Card>
        <Card title={`Writes · ${window}d`}><div className="pqgs-card-value" style={{ color: WRITE }}>{fmtInt(d.writes)}</div><PqgsNote facts={[{ label: "By", value: d.writers == null ? "-" : `${fmtInt(d.writers)} writers` }]} /></Card>
        <Card title="Last read"><div className="pqgs-card-value"><When v={d.last_read} never="never" /></div><PqgsNote facts={[{ label: "Who", value: lastReader ? lastReader.identity : "-" }]} /></Card>
        <Card title="Last write"><div className="pqgs-card-value"><When v={d.last_write} /></div><PqgsNote facts={[{ label: "Who", value: lastWriter ? lastWriter.identity : "-" }]} /></Card>
        <Card title="Rows changed"><div className="pqgs-card-value">{d.rows_changed == null ? "-" : fmtInt(d.rows_changed)}</div><PqgsNote facts={[{ label: "From", value: "DESCRIBE HISTORY", detail: `last ${window} days` }]} /></Card>
      </div>
      <Card title="Reads and writes per day" right={<span className="muted">lineage events, stacked</span>}>
        {d.lineage ? <Mirror daily={d.daily || []} window={window} /> : <span className="muted">Lineage is not in this export.</span>}
      </Card>
      <div className="grid-2">
        <Card title="Who reads and writes it" right={<span className="muted">most active first · click one</span>}>
          {users.length ? <div style={{ marginBottom: 10 }}><MostUsed rows={users.slice(0, 10).map((u) => ({ name: u.identity, reads: u.reads, writes: u.writes, sub: u.kind }))}
            open={(who) => toPerson(goTo, who)} /></div> : null}
          {users.length ? (
            <div className="data-table-wrap"><table className="data">
              <thead><tr><th>Who</th><th>Access</th><th className="num">Reads</th><th className="num">Writes</th><th>Last used</th><th>Via</th></tr></thead>
              <tbody>{users.map((u) => (
                <tr key={u.identity}><td>{u.identity}<div className="muted" style={{ fontSize: 12 }}>{u.kind}</div></td>
                  <td>{u.reads && u.writes ? "Both" : u.writes ? "Writes" : "Reads"}</td>
                  <td className="num mono" style={{ color: READ }}>{fmtInt(u.reads)}</td><td className="num mono" style={{ color: WRITE }}>{fmtInt(u.writes)}</td>
                  <td><When v={u.last_access} /></td><td style={{ fontSize: 12.5 }}>{u.via || "-"}</td></tr>
              ))}</tbody>
            </table></div>
          ) : <span className="muted">Nobody read or wrote it in the window.</span>}
          {d.unused_grants && d.unused_grants.length ? (
            <div style={{ marginTop: 10, padding: 10, borderRadius: 8, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
              <div style={{ fontWeight: 600 }}>{"Can read it, but never did in the window"}</div>
              {d.unused_grants.map((g: any) => <div key={g.grantee} style={{ fontSize: 13 }}>{`${g.grantee} · ${g.privilege} (direct grant)`}</div>)}
              <div className="muted" style={{ fontSize: 12 }}>Good candidates to revoke. Grants through a schema, catalog or group aren't listed.</div>
            </div>
          ) : null}
        </Card>
        <Card title="Sensitive columns" right={<span className="muted">{f.column_count ? `${fmtInt(sens.length)} of ${fmtInt(f.column_count)}` : ""}</span>}>
          {sens.length ? sens.map((s) => (
            <div key={s.column_name} style={{ display: "grid", gridTemplateColumns: "1fr auto auto auto", gap: 8, alignItems: "center", margin: "6px 0", fontSize: 13 }}>
              <span className="mono">{s.column_name}</span><PiiChip text={s.pii_type} /><span className="muted">{s.method}</span>
              <span style={{ color: s.masked ? "#1D4A8F" : "#9F1D14", fontWeight: 600 }}>{s.masked ? "masked" : "unmasked"}</span>
            </div>
          )) : <span className="muted">No column looks sensitive by tag, classification or name.</span>}
          <div className="metric-note">Column tags, Databricks data classification and column names. Values are never read or stored.</div>
        </Card>
      </div>
      <Card title="Where it comes from, where it goes">
        {d.has_edges ? (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 30px minmax(0, 1fr) 30px minmax(0, 1fr)", alignItems: "center" }}>
            <div><div className="muted" style={{ fontSize: 12 }}>Upstream</div>{(d.upstream || []).length ? d.upstream.map((n: any) => <Node key={n.name} n={n} />) : <span className="muted">none in the window</span>}</div>
            <div style={{ textAlign: "center" }}>→</div>
            <div><Node n={{ name, usage_class: d.usage_class, events: d.writes }} here /></div>
            <div style={{ textAlign: "center" }}>→</div>
            <div><div className="muted" style={{ fontSize: 12 }}>Downstream</div>{(d.downstream || []).length ? d.downstream.map((n: any) => <Node key={n.name} n={n} />) : <span className="muted">none in the window</span>}</div>
          </div>
        ) : <span className="muted">Table lineage edges are not in this export: export again.</span>}
        {(d.downstream || []).some((n: any) => n.usage_class === "idle_90d" || n.usage_class === "written_never_read")
          ? <div className="metric-note">{`Downstream ${d.downstream.filter((n: any) => n.usage_class === "idle_90d" || n.usage_class === "written_never_read").map((n: any) => n.name).join(", ")} is not read: the job that builds it can likely stop.`}</div> : null}
      </Card>
      <Card title="What to do">
        {todo.length ? (
          <React.Fragment>
            <ol style={{ margin: 0, paddingLeft: 20 }}>{todo.map((x) => <li key={x.text} style={{ margin: "6px 0" }}>{x.text}</li>)}</ol>
            <button type="button" className="csv-download-btn" style={{ marginTop: 10 }} onClick={copy}>{copied ? "Copied" : "Copy SQL for these fixes"}</button>
          </React.Fragment>
        ) : <span className="muted">Nothing to fix: it is read, masked where needed and not fragmented.</span>}
      </Card>
      <Upkeep d={d} window={window} />
      <TableQueries d={d} window={window} />
    </div>
  );
}

// ─────────── Volumes ───────────
function UsageVolumesContent({ filters, goTo, onVerdict }: LooseProps) {
  const key = JSON.stringify([filters.window, filters.workspaceIds, filters.envs, Api.tagParams()]);
  const st = useApi(() => Api.volumes(filters.workspaceIds, filters.envs, filters.window), key);
  const [open, setOpen] = React.useState<string | null>(handoff.volume || null);
  React.useEffect(() => { handoff.volume = null; }, []);
  const d = st.phase === "ready" ? st.data : null;
  const vols: any[] = d && d.outcome !== "not_assessed" ? d.volumes || [] : [];
  if (d && d.as_of) asOf = d.as_of;
  const size = vols.reduce((a, v) => a + (v.size_in_bytes || 0), 0);
  const files = vols.reduce((a, v) => a + (v.files || 0), 0);
  const idle = vols.filter((v) => v.usage_class === "idle_90d" || v.usage_class === "written_never_read");
  const verdict = vols.length ? `${fmtInt(vols.length)} volumes, ${fmtBytes(size)} in ${fmtInt(files)} files. ${fmtInt(idle.length)} not read.` : "Loading volumes...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  if (st.phase === "loading") return <span className="muted">Loading volumes...</span>;
  if (!vols.length) return <span className="muted">{!d || d.outcome === "not_assessed" ? "Volumes are not in this export: export again." : "No volume in the workspaces shown."}</span>;
  const reads = vols.reduce((a, v) => a + (v.reads || 0), 0), writes = vols.reduce((a, v) => a + (v.writes || 0), 0);
  const max = Math.max(1, ...vols.map((v) => (v.reads || 0) + (v.writes || 0)));
  const notListed = vols.filter((v) => v.detail && v.detail !== "listed");
  const byWs = new Map<string, { r: number; w: number }>();
  vols.forEach((v) => { const k = v.workspace_name || "No workspace"; const x = byWs.get(k) || { r: 0, w: 0 }; x.r += v.reads || 0; x.w += v.writes || 0; byWs.set(k, x); });
  const wsMax = Math.max(1, ...Array.from(byWs.values()).map((x) => x.r + x.w));
  const sel = open ? vols.find((v) => v.volume_full_name === open) : null;
  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="File reads"><div className="pqgs-card-value" style={{ color: READ }}>{fmtInt(reads)}</div><PqgsNote facts={[{ label: "From", value: "file-system audit events" }]} /></Card>
        <Card title="File writes"><div className="pqgs-card-value" style={{ color: WRITE }}>{fmtInt(writes)}</div><PqgsNote facts={[{ label: "From", value: "uploads, puts, deletes" }]} /></Card>
        <Card title="People using volumes"><div className="pqgs-card-value">{fmtInt((d.people || []).filter((p: any) => p.kind === "Person").length)}</div>
          <PqgsNote facts={[{ label: "Service principals", value: fmtInt((d.people || []).filter((p: any) => p.kind === "Service principal").length) }]} /></Card>
        <Card title="Not read"><div className={`pqgs-card-value ${idle.length ? "warn" : "ok"}`}>{fmtInt(idle.length)}</div>
          <PqgsNote facts={[{ label: "Size", value: fmtBytes(idle.reduce((a, v) => a + (v.size_in_bytes || 0), 0)) }]} /></Card>
      </div>
      <Card title="Most active volumes" right={<span className="muted">file events · click one</span>}>
        <MostUsed open={setOpen} rows={topUsed(vols, (v) => v.volume_full_name, rwSub)} />
      </Card>
      <Card title="Every volume" right={<span className="muted">click one for who used it</span>}>
        <div className="data-table-wrap">
          <table className="data">
            <thead><tr><th>Volume</th><th>Class</th><th className="num">Size</th><th className="num">Files</th><th>Reads / writes</th><th>Last read</th><th>Last write</th><th className="num">Readers · writers</th></tr></thead>
            <tbody>
              {vols.map((v) => (
                <tr key={v.volume_full_name} className={`ba-row${open === v.volume_full_name ? " on" : ""}`} style={{ cursor: "pointer" }} onClick={() => setOpen(open === v.volume_full_name ? null : v.volume_full_name)}>
                  <td><button type="button" className="ba-pick">{v.volume_full_name}</button><div className="muted" style={{ fontSize: 12 }}>{`${v.workspace_name || "no workspace"} · ${(v.volume_type || "").toLowerCase() || "-"}`}</div></td>
                  <td><ClassChip cls={v.usage_class} /></td>
                  <td className="num mono">{v.size_in_bytes == null ? "-" : fmtBytes(v.size_in_bytes)}</td>
                  <td className="num mono">{v.files == null ? "-" : `${fmtInt(v.files)}${v.truncated ? "+" : ""}`}</td>
                  <td><RWBar r={v.reads || 0} w={v.writes || 0} max={max} /><span className="muted" style={{ fontSize: 12 }}>{`${fmtInt(v.reads)} / ${fmtInt(v.writes)}`}</span></td>
                  <td><When v={v.last_read} never="never" /></td>
                  <td><When v={v.last_write} /></td>
                  <td className="num mono">{`${fmtInt(v.readers || 0)} · ${fmtInt(v.writers || 0)}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {notListed.length ? <NotListed vols={notListed} /> : null}
      </Card>
      {sel && <VolumeDetail v={sel} window={filters.window} goTo={goTo} wsNames={new Map(vols.filter((x) => x.workspace_id).map((x) => [String(x.workspace_id), x.workspace_name]))} />}
      <Card title="File reads and writes by workspace">
        {Array.from(byWs.entries()).sort((a, b) => b[1].r + b[1].w - (a[1].r + a[1].w)).map(([n, x]) => (
          <div key={n} style={{ display: "grid", gridTemplateColumns: "minmax(110px, 180px) 1fr 110px", gap: 10, alignItems: "center", margin: "6px 0", fontSize: 13 }}>
            <span>{n}</span>
            <div style={{ display: "flex", height: 16, width: `${(100 * (x.r + x.w)) / wsMax}%`, borderRadius: 3, overflow: "hidden" }} title={`${fmtInt(x.r)} reads · ${fmtInt(x.w)} writes`}>
              <div style={{ width: `${(100 * x.r) / ((x.r + x.w) || 1)}%`, background: READ }} /><div style={{ flex: 1, background: WRITE }} />
            </div>
            <span className="mono" style={{ textAlign: "right" }}>{`${fmtInt(x.r)} / ${fmtInt(x.w)}`}</span>
          </div>
        ))}
      </Card>
    </div>
  );
}

// Volumes with no size: used but missing from the listing, or the listing failed. Grouped, names on request.
function NotListed({ vols }: { vols: any[] }) {
  const [open, setOpen] = React.useState(false);
  const why = (v: any) => (v.detail === "not listed" ? "Used, but not in the listing" : "Listing failed");
  const groups = new Map<string, Map<string, number>>();
  vols.forEach((v) => {
    const g = groups.get(why(v)) || new Map<string, number>();
    const cat = String(v.volume_full_name).split(".")[0];
    g.set(cat, (g.get(cat) || 0) + 1);
    groups.set(why(v), g);
  });
  return (
    <div className="metric-note">
      {`${fmtInt(vols.length)} volumes have no size. `}
      {Array.from(groups.entries()).map(([reason, cats]) => {
        const n = Array.from(cats.values()).reduce((a, b) => a + b, 0);
        const top = Array.from(cats.entries()).sort((a, b) => b[1] - a[1]);
        return <span key={reason}>{`${reason}: ${fmtInt(n)} in ${fmtInt(cats.size)} catalog${cats.size === 1 ? "" : "s"} (${top.slice(0, 4).map(([c, k]) => `${c} ${k}`).join(", ")}${top.length > 4 ? ", …" : ""}). `}</span>;
      })}
      {"The sizes run lists only what its account can read: give it READ VOLUME on those catalogs, then run --sizes-only. "}
      <button type="button" className="link-btn" onClick={() => setOpen(!open)}>{open ? "Hide the names" : "Show the names"}</button>
      {open && (
        <div className="data-table-wrap" style={{ marginTop: 6, maxHeight: 260, overflowY: "auto" }}><table className="data">
          <thead><tr><th>Volume</th><th>Why</th><th>Workspace</th></tr></thead>
          <tbody>{vols.map((v) => <tr key={v.volume_full_name}><td className="mono">{v.volume_full_name}</td><td>{v.detail === "not listed" ? "not in the listing" : v.detail}</td><td>{v.workspace_name || "-"}</td></tr>)}</tbody>
        </table></div>
      )}
    </div>
  );
}

function VolumeDetail({ v, wsNames, window, goTo }: { v: any; wsNames: Map<string, string>; window: number; goTo: any }) {
  const st = useApi(() => Api.volumeUsers(v.volume_full_name, window), `${v.volume_full_name}|${window}`);
  const rows: any[] = st.phase === "ready" && st.data ? st.data.rows || [] : [];
  return (
    <Card title={`${v.volume_full_name}: who used it`} right={<ClassChip cls={v.usage_class} />}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
        {[v.workspace_name, (v.volume_type || "").toLowerCase(), v.size_in_bytes != null ? fmtBytes(v.size_in_bytes) : null,
          v.files != null ? `${fmtInt(v.files)}${v.truncated ? "+" : ""} files` : null, v.detail && v.detail !== "listed" ? v.detail : null].filter(Boolean)
          .map((x) => <span key={String(x)} className="badge">{x}</span>)}
      </div>
      {rows.length ? <div style={{ marginBottom: 10 }}><MostUsed rows={Array.from(rows.reduce((m, r) => {
        const a = m.get(r.identity) || { name: r.identity, reads: 0, writes: 0, sub: r.kind };
        a.reads += r.reads || 0; a.writes += r.writes || 0;
        return m.set(r.identity, a);
      }, new Map<string, any>()).values() as Iterable<any>).sort((a: any, b: any) => b.reads + b.writes - (a.reads + a.writes)).slice(0, 10)} open={(who) => toPerson(goTo, who)} /></div> : null}
      {st.phase === "loading" ? <span className="muted">Loading...</span> : rows.length ? (
        <div className="data-table-wrap"><table className="data">
          <thead><tr><th>Who</th><th>Kind</th><th>Workspace</th><th className="num">Reads</th><th className="num">Writes</th><th>Last read</th><th>Last write</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={`${r.identity}|${r.workspace_id}`}><td>{r.identity}</td><td>{r.kind}</td><td>{wsNames.get(String(r.workspace_id)) || r.workspace_id}</td>
              <td className="num mono" style={{ color: READ }}>{fmtInt(r.reads)}</td><td className="num mono" style={{ color: WRITE }}>{fmtInt(r.writes)}</td>
              <td><When v={r.last_read} never="never" /></td><td><When v={r.last_write} /></td></tr>
          ))}</tbody>
        </table></div>
      ) : <span className="muted">Nobody read or wrote a file in it in the window.</span>}
    </Card>
  );
}

// ─────────── People ───────────
function UsagePeopleContent({ filters, goTo, onVerdict }: LooseProps) {
  const [wsPick, setWsPick] = React.useState<string[]>([]);
  const ids = wsPick.length ? wsPick : filters.workspaceIds;
  const key = JSON.stringify([filters.window, ids, filters.envs]);
  const st = useApi(() => Api.people(filters.window, ids, filters.envs), key);
  const wl = useApi(() => Api.workspaces(), "workspaces");
  // the picker offers the workspaces of the top-bar filter
  const wsOptions: [string, string][] = (wl.phase === "ready" ? wl.data || [] : [])
    .filter((w: any) => !filters.workspaceIds.length || filters.workspaceIdSet.has(w.workspace_id))
    .map((w: any) => [w.workspace_id, w.name || w.workspace_id] as [string, string])
    .sort((a: [string, string], b: [string, string]) => a[1].localeCompare(b[1]));
  const [open, setOpen] = React.useState<string | null>(handoff.person || null);
  const [all, setAll] = React.useState(false);
  React.useEffect(() => { handoff.person = null; }, []);
  const d = st.phase === "ready" ? st.data : null;
  const people: any[] = d && d.outcome !== "not_assessed" ? d.people || [] : [];
  if (d && d.as_of) asOf = d.as_of;
  const inactive: any[] = d ? d.inactive || [] : [];
  const verdict = people.length
    ? `${fmtInt(d.active_people)} people and ${fmtInt(d.service_principals)} service principals were active. ${fmtInt(d.touched_sensitive)} touched tables with sensitive data; ${fmtInt(inactive.length)} more have not been seen in ${d.inactive_days}+ days.`
    : "Loading people...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  const picker = <div className="ws-actions"><WsMulti options={wsOptions} picked={wsPick} onChange={setWsPick} /></div>;
  if (st.phase === "loading") return <div>{picker}<span className="muted">Loading people...</span></div>;
  if (!people.length) return <div>{picker}<span className="muted">{!d || d.outcome === "not_assessed" ? "Activity is not in this export: export again." : "Nobody active in the workspaces shown."}</span></div>;
  const max = Math.max(1, ...people.map((p) => (p.reads || 0) + (p.writes || 0)));
  const shown = all ? people : people.slice(0, PAGE).concat(people.slice(PAGE).filter((p) => p.identity === open));
  return (
    <div>
      {picker}
      <div className="pqgs-cards-grid">
        <Card title="Active people"><div className="pqgs-card-value">{fmtInt(d.active_people)}</div><PqgsNote facts={[{ label: "Did", value: "signed in, queried or used a table" }]} /></Card>
        <Card title="Service principals"><div className="pqgs-card-value">{fmtInt(d.service_principals)}</div>
          <PqgsNote facts={[{ label: "Share of writes", value: d.sp_write_share == null ? "-" : fmtPct(d.sp_write_share, 0) }]} /></Card>
        <Card title="Touched sensitive data"><div className="pqgs-card-value" style={{ color: PII_INK }}>{fmtInt(d.touched_sensitive)}</div><PqgsNote facts={[{ label: "Read or wrote", value: "a table with sensitive columns" }]} /></Card>
        <Card title={`Inactive ${d.inactive_days}d+`}><div className={`pqgs-card-value ${inactive.length ? "warn" : "ok"}`}>{fmtInt(inactive.length)}</div>
          <PqgsNote facts={[{ label: "Seen", value: "in the last year, not lately" }]} /></Card>
      </div>
      <SeenBands bands={d.seen_bands || []} />
      <Card title="Most active people" right={<span className="muted">table events · click one for their tables</span>}>
        <MostUsed open={setOpen} rows={topUsed(people, (p) => p.identity, (p) => `${count(p.tables_read || 0, "table")} read · ${fmtInt(p.tables_written || 0)} written`)} />
      </Card>
      <Card title="Who reads and who writes" right={<span className="muted">most table writes first, then reads · click one for its tables</span>}>
        <div className="data-table-wrap"><table className="data">
          <thead><tr><th>Who</th><th>Workspaces</th><th className="num">Queries</th><th className="num">Tables read</th><th className="num">Tables written</th><th>Reads / writes</th>
            <th>Writes nobody reads</th><th>Sensitive tables</th><th>Last seen</th></tr></thead>
          <tbody>{shown.map((p) => (
            <React.Fragment key={p.identity}>
            <tr className={`ba-row${open === p.identity ? " on" : ""}`} style={{ cursor: "pointer" }} onClick={() => setOpen(open === p.identity ? null : p.identity)}>
              <td><button type="button" className="ba-pick">{`${open === p.identity ? "▾" : "▸"} ${p.identity}`}</button>{p.kind === "Service principal" ? <span className="badge" style={{ marginLeft: 6 }}>SP</span> : null}</td>
              <td style={{ fontSize: 12.5 }}>{p.workspaces.join(", ")}</td>
              <td className="num mono">{fmtInt(p.queries)}</td>
              <td className="num mono">{fmtInt(p.tables_read)}</td>
              <td className="num mono">{fmtInt(p.tables_written)}</td>
              <td><RWBar r={p.reads || 0} w={p.writes || 0} max={max} /><span className="muted" style={{ fontSize: 12 }}>{`${fmtInt(p.reads)} / ${fmtInt(p.writes)}`}</span></td>
              <td>{p.writes_nobody_reads.length ? <span style={{ display: "inline-block", padding: "2px 8px", borderRadius: 999, fontSize: 12, fontWeight: 600, background: CLASS_META.written_never_read.bg, color: CLASS_META.written_never_read.ink }}>{p.writes_nobody_reads.map((n: string) => n.split(".").pop()).join(", ")}</span> : "—"}</td>
              <td>{p.sensitive_tables.length ? <PiiChip text={`${p.sensitive_tables.length}: ${p.sensitive_tables.slice(0, 2).map((n: string) => n.split(".").pop()).join(", ")}${p.sensitive_tables.length > 2 ? "…" : ""}`} /> : "—"}</td>
              <td><When v={p.last_seen} /></td>
            </tr>
            {open === p.identity && <tr><td colSpan={9} style={{ background: "var(--surface-2)" }}><PersonTables identity={p.identity} window={filters.window} goTo={goTo} /></td></tr>}
            </React.Fragment>
          ))}</tbody>
        </table></div>
        {people.length > PAGE && <button type="button" className="ov-link-btn" onClick={() => setAll(!all)}>{all ? `Show the first ${PAGE}` : `Show all ${fmtInt(people.length)}`}</button>}
      </Card>
      <Card title={`Not seen for ${d.inactive_days}+ days, still with access`} right={<span className="muted">remove from the workspace or its groups; sensitive tables first</span>}>
        {inactive.length ? (
          <div className="data-table-wrap"><table className="data">
            <thead><tr><th>Who</th><th>Workspace</th><th>Last seen</th><th className="num">Days</th><th>Can still read</th><th>Via</th></tr></thead>
            <tbody>{inactive.map((r) => (
              <tr key={`${r.identity}|${r.workspace}`}><td>{r.identity}</td><td>{r.workspace}</td><td>{fmtDayShort(String(r.last_seen).slice(0, 10))}</td>
                <td className="num mono">{fmtInt(r.days)}</td>
                <td style={{ fontSize: 12.5 }}>{r.can_read.length ? r.can_read.map((n: string) => `${n.split(".").pop()}${r.sensitive_can_read.includes(n) ? " (PII)" : ""}`).join(", ") : d.grants ? "no direct grant" : "-"}</td>
                <td style={{ fontSize: 12.5 }}>{r.can_read.length ? "direct grant" : "-"}</td></tr>
            ))}</tbody>
          </table></div>
        ) : <span className="muted">{`Everyone seen in the last year was seen in the last ${d.inactive_days} days.`}</span>}
        {!d.grants ? <div className="metric-note">Direct grants are not in this export: export again.</div> : <div className="metric-note">Direct table grants only; access through a schema, catalog or group isn't expanded.</div>}
      </Card>
    </div>
  );
}

const SEEN_COLORS: Record<string, string> = { "Under 30 days": "#2456A6", "30-90 days": "#E0A93B", "90-180 days": "#D27A2C", "180+ days": "#C2362B" };

// Everyone seen in the last year, by when they were last seen anywhere.
function SeenBands({ bands }: { bands: any[] }) {
  const total = bands.reduce((a, b) => a + b.people + b.service_principals, 0);
  if (!total) return null;
  return (
    <Card title="Everyone by last seen" right={<span className="muted">{`${fmtInt(total)} identities seen in the last year · audit log`}</span>}>
      <div style={{ display: "flex", height: 22, borderRadius: 6, overflow: "hidden", margin: "4px 0 10px" }}>
        {bands.filter((b) => b.people + b.service_principals).map((b) => (
          <div key={b.band} title={`${b.band}: ${b.people} people, ${b.service_principals} SPs`} style={{ width: `${(100 * (b.people + b.service_principals)) / total}%`, background: SEEN_COLORS[b.band] }} />
        ))}
      </div>
      <div className="pqgs-cards-grid">
        {bands.map((b) => (
          <div key={b.band}><div className="pqgs-card-value" style={{ color: SEEN_COLORS[b.band] }}>{fmtInt(b.people + b.service_principals)}</div>
            <div className="muted">{`last seen ${b.band.toLowerCase()} ago · ${fmtInt(b.people)} people, ${fmtInt(b.service_principals)} SPs`}</div></div>
        ))}
      </div>
    </Card>
  );
}

// The tables one person or service principal used, across workspaces; a name opens its detail.
function PersonTables({ identity, window, goTo }: { identity: string; window: number; goTo: any }) {
  const st = useApi(() => Api.personTables(identity, window), `${identity}|${window}`);
  const rows: any[] = st.phase === "ready" && st.data ? st.data.rows || [] : [];
  if (st.phase === "loading") return <span className="muted">Loading tables...</span>;
  if (!rows.length) return <span className="muted">No table read or written in the window (lineage).</span>;
  const max = Math.max(1, ...rows.map((r) => r.reads + r.writes));
  const toTable = (name: string) => { Object.assign(handoff, { workspace: null, cls: null, table: name }); goTo({ tab: "usage", subtab: "tables" }); };
  return (
    <React.Fragment>
    <div style={{ margin: "4px 0 10px" }}><MostUsed rows={rows.slice(0, 10).map((r) => ({ name: r.table_full_name, reads: r.reads, writes: r.writes, sub: r.workspaces.join(", ") }))} open={toTable} /></div>
    <table className="data">
      <thead><tr><th>Table</th><th>Class</th><th className="num">Reads</th><th className="num">Writes</th><th>Reads / writes</th><th>Last used</th><th>Workspaces</th><th>Sensitive</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.table_full_name}>
          <td><button type="button" className="ba-pick" onClick={(e) => { e.stopPropagation(); toTable(r.table_full_name); }}>{r.table_full_name}</button></td>
          <td><ClassChip cls={r.usage_class} /></td>
          <td className="num mono" style={{ color: READ }}>{fmtInt(r.reads)}</td>
          <td className="num mono" style={{ color: WRITE }}>{fmtInt(r.writes)}</td>
          <td><RWBar r={r.reads} w={r.writes} max={max} /></td>
          <td><When v={r.last_access} /></td>
          <td style={{ fontSize: 12.5 }}>{r.workspaces.join(", ")}</td>
          <td>{r.sensitive.length ? <PiiChip text={r.sensitive.join(", ")} /> : "—"}</td>
        </tr>
      ))}</tbody>
    </table>
    </React.Fragment>
  );
}

AreaContent.register("usage", "workspaces", UsageWorkspacesContent);
AreaContent.register("usage", "tables", UsageTablesContent);
AreaContent.register("usage", "volumes", UsageVolumesContent);
AreaContent.register("usage", "people", UsagePeopleContent);
