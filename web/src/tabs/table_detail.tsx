// One table in detail: its facts, reads and writes per day (lineage), who used it, its change history
// (DESCRIBE HISTORY: writes, merges, deletes, OPTIMIZE, VACUUM, who and when) and predictive
// optimization's maintenance runs on it.

import React from "react";

import { Api } from "../api";
import { fmtBytes, fmtInt } from "../format";
import { Donut, paletteColor } from "../components/charts";

const day = (v: any) => (v ? String(v).slice(0, 10) : "-");
const when = (v: any) => (v ? String(v).slice(0, 16).replace("T", " ") : "-");

// "numOutputRows=5, numTargetRowsUpdated=2" -> {numOutputRows: 5, ...}
function metricsOf(m: any): Record<string, number> {
  const out: Record<string, number> = {};
  String(m || "").split(",").forEach((kv) => {
    const [k, v] = kv.split("=").map((x) => x.trim());
    if (k && v && !isNaN(Number(v))) out[k] = Number(v);
  });
  return out;
}

const WRITE_OPS = /WRITE|MERGE|UPDATE|DELETE|STREAMING UPDATE|CREATE OR REPLACE|COPY INTO|TRUNCATE|RESTORE/;
const MAINT_OPS = /OPTIMIZE|VACUUM|ANALYZE|REORG|CLUSTER/;
const daysAgo = (v: any) => {
  const t = Date.parse(String(v || "").replace(" ", "T"));
  return isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 86400000));
};
const ago = (v: any) => {
  const n = daysAgo(v);
  return n == null ? "" : n === 0 ? "today" : n === 1 ? "yesterday" : `${fmtInt(n)} days ago`;
};

// The table at a glance: when it was last read, written and maintained, and how many rows changed.
function Summary({ d }: { d: any }) {
  const hist: any[] = d.history || [];
  const writes = hist.filter((h) => WRITE_OPS.test(String(h.operation || "")));
  const maint = hist.filter((h) => MAINT_OPS.test(String(h.operation || "")));
  const po: any[] = d.maintenance || [];
  const lastWrite = writes[0] ? writes[0].timestamp : d.last_write || (d.facts || {}).last_modified;
  const lastMaint = [maint[0] && maint[0].timestamp, po[0] && po[0].start_time].filter(Boolean).sort().pop();
  const rows = { inserted: 0, updated: 0, deleted: 0, written: 0, bytes: 0 };
  writes.forEach((h) => {
    const m = metricsOf(h.metrics);
    rows.inserted += m.numTargetRowsInserted || 0;
    rows.updated += m.numTargetRowsUpdated || 0;
    rows.deleted += (m.numTargetRowsDeleted || 0) + (m.numDeletedRows || 0);
    rows.written += m.numOutputRows || 0;
    rows.bytes += m.numOutputBytes || 0;
  });
  const since = hist.length ? day(hist[hist.length - 1].timestamp) : null;
  const card = (title: string, value: string, note: string) => (
    <div className="chart-card" style={{ padding: "8px 10px" }}>
      <div className="pqgs-card-note">{title}</div>
      <div style={{ fontWeight: 600, margin: "2px 0" }}>{value}</div>
      <div className="pqgs-card-note">{note}</div>
    </div>
  );
  return (
    <div className="grid-4" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 8, marginTop: 8 }}>
      {card("Last read", d.last_read ? day(d.last_read) : "-",
        d.last_read ? ago(d.last_read) : d.read_lookback_days ? `not read in ${d.read_lookback_days} days` : "no lineage")}
      {card("Last write", lastWrite ? day(lastWrite) : "-",
        lastWrite ? `${ago(lastWrite)}${writes[0] ? ` · ${String(writes[0].operation).toLowerCase()}` : ""}` : "no write seen")}
      {card("Last maintenance", lastMaint ? day(lastMaint) : "-",
        lastMaint ? ago(lastMaint) : "no OPTIMIZE or VACUUM seen")}
      {card("Rows changed", fmtInt(rows.inserted + rows.updated + rows.deleted || rows.written),
        hist.length ? `${rows.inserted || rows.updated || rows.deleted
          ? `${fmtInt(rows.inserted)} in, ${fmtInt(rows.updated)} updated, ${fmtInt(rows.deleted)} deleted`
          : `${fmtInt(rows.written)} written`}${rows.bytes ? ` · ${fmtBytes(rows.bytes)}` : ""} since ${since}` : "no change history")}
    </div>
  );
}

// The latest changes in one line each.
function Latest({ hist }: { hist: any[] }) {
  if (!hist.length) return null;
  return (
    <div className="pqgs-rt" style={{ marginTop: 10 }}>
      {hist.slice(0, 5).map((h) => {
        const m = metricsOf(h.metrics);
        const n = (m.numTargetRowsInserted || 0) + (m.numTargetRowsUpdated || 0) + (m.numTargetRowsDeleted || 0) || m.numOutputRows || 0;
        return (
          <div className="pqgs-rt-row pqgs-rt-cols-mnt" key={h.version}>
            <span className="pqgs-rt-num" style={{ textAlign: "left" }}>{when(h.timestamp)}</span>
            <span>{h.operation}</span>
            <span className="pqgs-rt-note">{ago(h.timestamp)}</span>
            <span className="pqgs-rt-num">{n ? `${fmtInt(n)} rows` : ""}</span>
          </div>
        );
      })}
    </div>
  );
}

// Reads and writes per day as stacked bars, one per day.
function DailyBars({ rows }: { rows: any[] }) {
  if (!rows.length) return <span className="muted">No read or write in lineage in the window.</span>;
  const W = 900, H = 150, P = 26;
  const max = Math.max(1, ...rows.map((r) => (r.reads || 0) + (r.writes || 0)));
  const bw = Math.max(2, (W - 2 * P) / rows.length - 2);
  const y = (v: number) => (v / max) * (H - 2 * P);
  return (
    <div style={{ overflowX: "auto" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: W, display: "block" }} role="img" aria-label="Reads and writes per day">
        <line x1={P} y1={H - P} x2={W - P} y2={H - P} stroke="currentColor" opacity={0.3} />
        {rows.map((r, i) => {
          const x = P + i * ((W - 2 * P) / rows.length);
          const hr = y(r.reads || 0), hw = y(r.writes || 0);
          return (
            <g key={r.day}>
              <title>{`${day(r.day)}: ${fmtInt(r.reads)} reads by ${fmtInt(r.readers)}, ${fmtInt(r.writes)} writes by ${fmtInt(r.writers)}`}</title>
              <rect x={x} y={H - P - hr} width={bw} height={hr} fill="var(--c1)" />
              <rect x={x} y={H - P - hr - hw} width={bw} height={hw} fill="var(--c2)" />
            </g>
          );
        })}
        <text x={P} y={H - 8} fontSize={10} fill="currentColor" opacity={0.6}>{day(rows[0].day)}</text>
        <text x={W - P} y={H - 8} fontSize={10} textAnchor="end" fill="currentColor" opacity={0.6}>{day(rows[rows.length - 1].day)}</text>
        <text x={P} y={14} fontSize={10} fill="currentColor" opacity={0.6}>{`${fmtInt(max)} a day at most`}</text>
      </svg>
      <div className="pqgs-card-note" style={{ marginTop: 4 }}>
        <span className="legend-dot" style={{ background: "var(--c1)" }}></span>{" reads  "}
        <span className="legend-dot" style={{ background: "var(--c2)", marginLeft: 10 }}></span>{" writes"}
      </div>
    </div>
  );
}

export function TableDetail({ name }: { name: string }) {
  const [st, setSt] = React.useState<{ phase: string; d: any }>({ phase: "loading", d: null });
  React.useEffect(() => {
    let live = true;
    setSt({ phase: "loading", d: null });
    Api.table(name).then((d: any) => { if (live) setSt({ phase: "ready", d }); })
      .catch(() => { if (live) setSt({ phase: "error", d: null }); });
    return () => { live = false; };
  }, [name]);
  if (st.phase === "loading") return <div className="muted" style={{ padding: "8px 12px" }}>{`Loading ${name}...`}</div>;
  const d = st.d;
  if (!d) return <div className="muted" style={{ padding: "8px 12px" }}>Could not load this table.</div>;
  const f = d.facts || {};
  const ops: Record<string, number> = {};
  (d.history || []).forEach((h: any) => { ops[h.operation || "?"] = (ops[h.operation || "?"] || 0) + 1; });
  const maint: any[] = d.maintenance || [];
  const reads = (d.daily || []).reduce((a: number, r: any) => a + (r.reads || 0), 0);
  const writes = (d.daily || []).reduce((a: number, r: any) => a + (r.writes || 0), 0);
  return <TableBody name={name} d={d} f={f} ops={ops} maint={maint} reads={reads} writes={writes} />;
}

function TableBody({ name, d, f, ops, maint, reads, writes }: { name: string; d: any; f: any; ops: Record<string, number>; maint: any[]; reads: number; writes: number }) {
  const [more, setMore] = React.useState(false);
  return (
    <div className="table-detail" style={{ margin: "6px 0 14px 12px", padding: "12px 14px", border: "1px solid var(--line, #e2e1dc)", borderRadius: 8 }}>
      <div className="chart-card-title">{name}</div>
      <div className="metric-note">
        {[d.workspace_name ? `${d.workspace_name}${d.env ? ` · ${d.env}` : ""}` : null,
          f.table_type ? String(f.table_type).toLowerCase() : null, f.data_source_format ? String(f.data_source_format).toLowerCase() : null,
          f.column_count != null ? `${fmtInt(f.column_count)} columns` : null,
          f.size_in_bytes != null ? fmtBytes(f.size_in_bytes) : null, f.num_files != null ? `${fmtInt(f.num_files)} files` : null,
          f.last_modified ? `last changed ${day(f.last_modified)}` : null,
          `${fmtInt(reads)} reads, ${fmtInt(writes)} writes in the window`].filter(Boolean).join(" · ")}
        {f.detail && String(f.detail).startsWith("failed") ? ` · DESCRIBE DETAIL failed: ${String(f.detail).slice(8)}` : ""}
      </div>
      <Summary d={d} />
      <Latest hist={d.history || []} />
      <div className="pqgs-card-note" style={{ marginTop: 8 }}>
        <a href="#" onClick={(e) => { e.preventDefault(); setMore(!more); }}>
          {more ? "Hide the details" : "Reads per day, every change, who used it and maintenance"}</a>
      </div>
      {more && <React.Fragment>
      <div className="chart-card-title" style={{ marginTop: 10 }}>Reads and writes per day</div>
      {d.lineage ? <DailyBars rows={d.daily || []} /> : <span className="muted">Lineage not in this export.</span>}
      <div className="grid-2" style={{ marginTop: 12 }}>
        <div>
          <div className="chart-card-title">{`Last changes · ${fmtInt((d.history || []).length)}`}</div>
          {!d.has_history ? <span className="muted">Change history not in this export.</span>
            : !(d.history || []).length ? <span className="muted">No history (not described this time, or not a Delta table).</span> : (
            <React.Fragment>
              <Donut segments={Object.entries(ops).sort((a, b) => b[1] - a[1]).map(([op, n], i) => ({ label: op, value: n, display: fmtInt(n), color: paletteColor(i) }))}
                centerLabel={fmtInt((d.history || []).length)} centerSub="changes" size={96} />
            </React.Fragment>
          )}
        </div>
        <div>
          <div className="chart-card-title">{`Who used it · ${fmtInt((d.users || []).length)}`}</div>
          {(d.users || []).length ? (
            <div className="pqgs-rt">
              {(d.users || []).slice(0, 12).map((u: any) => (
                <div className="pqgs-rt-row pqgs-rt-cols-tu" key={u.identity}>
                  <span className="pqgs-rt-name">{u.identity}</span>
                  <span className="pqgs-rt-num">{`${fmtInt(u.reads)} r`}</span>
                  <span className="pqgs-rt-num">{`${fmtInt(u.writes)} w`}</span>
                  <span className="pqgs-rt-num">{day(u.last_access)}</span>
                </div>
              ))}
            </div>
          ) : <span className="muted">Nobody in lineage in the window.</span>}
        </div>
      </div>
      {(d.history || []).length > 0 && (
        <div className="pqgs-rt" style={{ marginTop: 10 }}>
          <div className="pqgs-rt-head pqgs-rt-cols-hist">
            <span>Version</span><span>When</span><span>Operation</span><span>Who</span><span>What changed</span>
          </div>
          {(d.history || []).map((h: any) => (
            <div className="pqgs-rt-row pqgs-rt-cols-hist" key={h.version}>
              <span className="pqgs-rt-num" style={{ textAlign: "left" }}>{h.version}</span>
              <span className="pqgs-rt-num" style={{ textAlign: "left" }}>{when(h.timestamp)}</span>
              <span>{h.operation}</span>
              <span className="pqgs-rt-note">{h.user_name || "-"}</span>
              <span className="pqgs-rt-note">{h.metrics || ""}</span>
            </div>
          ))}
        </div>
      )}
      <div className="chart-card-title" style={{ marginTop: 12 }}>{`Maintenance by predictive optimization · ${fmtInt(maint.length)}`}</div>
      {!d.has_maintenance ? <span className="muted">Not in this export.</span> : !maint.length
        ? <span className="muted">No compaction, vacuum or clustering run in the window (predictive optimization may be off). Manual OPTIMIZE and VACUUM show in Last changes.</span> : (
        <div className="pqgs-rt">
          <div className="pqgs-rt-head pqgs-rt-cols-mnt"><span>When</span><span>Operation</span><span>Status</span><span style={{ textAlign: "right" }}>Cost</span></div>
          {maint.slice(0, 30).map((m: any, i: number) => (
            <div className="pqgs-rt-row pqgs-rt-cols-mnt" key={i}>
              <span className="pqgs-rt-num" style={{ textAlign: "left" }}>{when(m.start_time)}</span>
              <span>{m.operation_type}</span>
              <span>{m.operation_status}</span>
              <span className="pqgs-rt-num">{m.usage_quantity == null ? "-" : `${Number(m.usage_quantity).toFixed(2)} ${m.usage_unit || ""}`}</span>
            </div>
          ))}
        </div>
      )}
      </React.Fragment>}
    </div>
  );
}
