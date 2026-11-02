// Compute > Warehouse detail: every SQL warehouse with its spend, up, busy and idle time and queries;
// click one for its spend by day and month (the same card as Cost > Trend), up/busy/idle and queries
// per day, idle gaps and what-ifs, who uses it, the tables it touches and its slowest queries.
import React from "react";

import { Api } from "../api";
import { fmtDayShort, fmtDurationMs, fmtInt, fmtMoney, fmtPct } from "../format";
import { AreaContent, Card, PqgsNote } from "../components/primitives";
import { HBarList } from "../components/charts";
import { Columns } from "../components/charts_more";
import { MONTH_ROWS_MAX, monthOf, monthRun, partialMonths } from "../components/grain";
import { TrendItemCard } from "./cost_trend_by";

const BUSY = "#2456A6";
const IDLE = "#C9A227";
const SHOWN = 30;

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

const mins = (v: any) => (v == null ? "-" : v >= 120 ? `${fmtInt(Math.round(v / 60))} h` : `${fmtInt(Math.round(v))} min`);
const idlePct = (r: any) => (r.up_minutes ? (100 * (r.idle_minutes || 0)) / r.up_minutes : null);
const label = (name: string, id: string) => (name && name !== id ? `${name} (${id})` : id);
const day = (v: any) => (v ? String(v).slice(0, 16).replace("T", " ") : "-");

function WarehouseDetailContent({ filters, onVerdict }: LooseProps) {
  const listKey = JSON.stringify([filters.window, filters.workspaceIds, filters.envs]);
  const list = useApi(() => Api.warehouses(filters.window, filters.workspaceIds, filters.envs), listKey);
  const [picked, setPicked] = React.useState<string | null>(null);
  const [all, setAll] = React.useState(false);
  const d = list.phase === "ready" ? list.data : null;
  const rows: any[] = d && d.outcome !== "not_assessed" ? d.rows || [] : [];
  const disc = d ? Number(d.discount_pct || 0) : 0;
  const sel = rows.find((r) => r.warehouse_id === picked) || rows[0] || null;
  const total = rows.reduce((a, r) => a + r.usd_list * (1 - disc), 0);
  const idleTotal = rows.reduce((a, r) => a + (r.idle_minutes || 0), 0);
  const upTotal = rows.reduce((a, r) => a + (r.up_minutes || 0), 0);
  const verdict = rows.length
    ? `${fmtInt(rows.length)} warehouses cost ${fmtMoney(total, 0)} in ${filters.window} days${upTotal ? `; ${fmtPct((100 * idleTotal) / upTotal, 0)} of their up time had no query running` : ""}.`
    : "Loading warehouses...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  if (list.phase === "loading") return <span className="muted">Loading warehouses...</span>;
  if (!rows.length) return <span className="muted">{d && d.outcome === "not_assessed" ? "No warehouse spend or activity in this export: export again." : "No warehouse in the workspaces shown."}</span>;
  const shown = all ? rows : rows.slice(0, SHOWN);
  return (
    <div>
      <Card title="Every warehouse" right={<span className="muted">most spend first · click one for its detail</span>}>
        <div className="data-table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Warehouse</th><th>Workspace</th><th className="num">Spend</th><th className="num">Up</th>
                <th className="num">Busy</th><th className="num">Idle</th><th className="num">Idle %</th>
                <th className="num">Queries</th><th className="num">Failed</th><th className="num">Users</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.warehouse_id} className={`ba-row${sel && r.warehouse_id === sel.warehouse_id ? " on" : ""}`} onClick={() => setPicked(r.warehouse_id)}>
                  <td><button type="button" className="ba-pick" onClick={() => setPicked(r.warehouse_id)}>{label(r.warehouse_name, r.warehouse_id)}</button></td>
                  <td>{r.workspace_name || "-"}</td>
                  <td className="num mono">{fmtMoney(r.usd_list * (1 - disc), 0)}</td>
                  <td className="num mono">{mins(r.up_minutes)}</td>
                  <td className="num mono">{mins(r.busy_minutes)}</td>
                  <td className="num mono">{mins(r.idle_minutes)}</td>
                  <td className="num mono">{idlePct(r) == null ? "-" : fmtPct(idlePct(r), 0)}</td>
                  <td className="num mono">{r.queries == null ? "-" : fmtInt(r.queries)}</td>
                  <td className="num mono">{r.failed == null ? "-" : fmtInt(r.failed)}</td>
                  <td className="num mono">{r.users == null ? "-" : fmtInt(r.users)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.length > SHOWN && <button type="button" className="ov-link-btn" onClick={() => setAll(!all)}>{all ? `Show the ${SHOWN} largest` : `Show all ${fmtInt(rows.length)}`}</button>}
        {!d.detail && <div className="metric-note">Up, busy and idle time, users and queries need a newer export (warehouse/ folder).</div>}
      </Card>
      {sel && <WarehouseCard key={sel.warehouse_id} id={sel.warehouse_id} window={filters.window} />}
    </div>
  );
}

function WarehouseCard({ id, window }: { id: string; window: number }) {
  const st = useApi(() => Api.warehouse(id, window), `${id}|${window}`);
  if (st.phase === "loading") return <span className="muted">Loading the warehouse...</span>;
  if (st.phase === "error" || !st.data) return <span className="muted">Could not load this warehouse.</span>;
  const w = st.data;
  const disc = Number(w.discount_pct || 0);
  const costDays: string[] = w.cost_by_day.map((c: any) => c.day);
  const byDay = new Map<string, number>(w.cost_by_day.map((c: any) => [c.day, c.usd_list * (1 - disc)]));
  const byMonth = new Map<string, number>();
  byDay.forEach((v, k) => byMonth.set(monthOf(k), (byMonth.get(monthOf(k)) || 0) + v));
  const { open, cut } = partialMonths(costDays, true);
  const daily: any[] = w.daily || [];
  const days = daily.map((r) => r.day);
  const sum = (k: string) => daily.reduce((a, r) => a + (Number(r[k]) || 0), 0);
  const up = sum("up_minutes"), busy = sum("busy_minutes"), idle = sum("idle_minutes"), queries = sum("queries");
  const idleRow = w.idle || null;
  const cfg = w.config || null;
  const p = w.pressure || null;
  return (
    <div>
      {costDays.length
        ? <TrendItemCard name={`${label(w.warehouse_name, w.warehouse_id)}: spend`} byDay={byDay} days={costDays} byMonth={byMonth}
            months={monthRun(costDays.map(monthOf)).slice(-MONTH_ROWS_MAX)} monthNote="Months inside this export's window only."
            openMonth={open} isFull={(m) => m !== open && m !== cut} partDay={null} />
        : <Card title={label(w.warehouse_name, w.warehouse_id)}><span className="muted">No spend for this warehouse in the window.</span></Card>}

      <div className="pqgs-cards-grid">
        <Card title="Up time">
          <div className="pqgs-card-value">{mins(up)}</div>
          <PqgsNote facts={[{ label: "Busy", value: mins(busy), detail: "a query running" }]} />
        </Card>
        <Card title="Idle">
          <div className={`pqgs-card-value ${up && idle / up >= 0.3 ? "warn" : "ok"}`}>{up ? fmtPct((100 * idle) / up, 0) : "-"}</div>
          <PqgsNote facts={[{ label: "Idle", value: mins(idle), detail: "up, no query running" },
            ...(idleRow && idleRow.est_wasted_usd_list != null ? [{ label: "Idle cost", value: fmtMoney(idleRow.est_wasted_usd_list * (1 - disc), 0) }] : [])]} />
        </Card>
        <Card title="Queries">
          <div className="pqgs-card-value">{fmtInt(queries)}</div>
          <PqgsNote facts={[{ label: "Failed", value: fmtInt(sum("failed")) }, { label: "Queued", value: fmtDurationMs(sum("queued_ms")) }]} />
        </Card>
        <Card title="Settings">
          <div className="pqgs-card-value">{cfg ? cfg.warehouse_size || cfg.warehouse_type || "-" : "-"}</div>
          <PqgsNote facts={cfg ? [{ label: "Auto-stop", value: cfg.auto_stop_minutes != null ? `${cfg.auto_stop_minutes} min` : "-" },
            { label: "Clusters", value: `${cfg.min_clusters ?? "-"}-${cfg.max_clusters ?? "-"}` }] : [{ label: "Settings", value: "not in this export" }]} />
        </Card>
      </div>

      <div className="grid-2">
        <Card title="Busy and idle by day" right={<span className="muted">minutes up, split by whether a query ran</span>}>
          {daily.length
            ? <Columns days={days} series={[{ name: "Busy", values: daily.map((r) => Number(r.busy_minutes) || 0), color: BUSY },
                { name: "Idle", values: daily.map((r) => Number(r.idle_minutes) || 0), color: IDLE }]}
                valueFmt={(v) => mins(v)} unitLabel="min" ariaLabel="Busy and idle minutes by day"
                note={idleRow && idleRow.waste_reason ? `Idle gaps (Compute > Warehouses): ${idleRow.waste_reason}.` : undefined} />
            : <span className="muted">Up, busy and idle time need a newer export.</span>}
        </Card>
        <Card title="Queries by day">
          {daily.length
            ? <Columns days={days} series={[{ name: "Queries", values: daily.map((r) => Number(r.queries) || 0), color: BUSY },
                { name: "Failed", values: daily.map((r) => Number(r.failed) || 0), color: "#C2362B" }]}
                valueFmt={(v) => fmtInt(v)} unitLabel="queries" ariaLabel="Queries by day"
                note={p ? `Queued at capacity ${fmtPct(p.capacity_wait_pct, 0)} of the time, waiting for a cluster ${fmtPct(p.cold_start_wait_pct, 0)}; spill in ${fmtPct(p.spill_query_pct, 0)} of queries.` : undefined} />
            : <span className="muted">No queries in the window.</span>}
        </Card>
      </div>

      <div className="grid-2">
        <Card title="Who uses it" right={<span className="muted">by queries</span>}>
          {w.users.length
            ? <HBarList items={w.users.slice(0, 10).map((u: any) => ({ name: `${u.identity}${u.kind === "Service principal" ? " (service principal)" : ""}`, value: u.queries }))}
                valueFmt={(v) => `${fmtInt(v)} queries`} />
            : <span className="muted">No queries in the window.</span>}
        </Card>
        <Card title="Tables it touches most" right={<span className="muted">reads and writes by its queries</span>}>
          {w.tables.length
            ? <HBarList items={w.tables.slice(0, 10).map((t: any) => ({ name: t.table_full_name, value: (t.reads || 0) + (t.writes || 0) }))}
                valueFmt={(v, it: any) => { const t = w.tables.find((x: any) => x.table_full_name === it.name); return `${fmtInt(t ? t.reads : v)} reads · ${fmtInt(t ? t.writes : 0)} writes`; }} />
            : <span className="muted">{w.tables_error ? `Not available: lineage in this account doesn't link tables to queries (${w.tables_error}).` : "No table read or written by its queries in the window."}</span>}
        </Card>
      </div>

      {w.users.length > 0 && (
        <Card title="Everyone who ran queries on it">
          <div className="data-table-wrap">
            <table className="data">
              <thead><tr><th>Who</th><th>Kind</th><th className="num">Queries</th><th className="num">Failed</th><th className="num">Run time</th><th>Last query</th></tr></thead>
              <tbody>
                {w.users.map((u: any) => (
                  <tr key={u.identity}><td>{u.identity}</td><td>{u.kind}</td><td className="num mono">{fmtInt(u.queries)}</td>
                    <td className="num mono">{fmtInt(u.failed)}</td><td className="num mono">{fmtDurationMs(u.run_ms)}</td><td>{day(u.last_query)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {w.slow.length > 0 && (
        <Card title="Slowest queries" right={<span className="muted">{`the ${w.slow.length} longest in the window`}</span>}>
          <div className="data-table-wrap">
            <table className="data">
              <thead><tr><th>When</th><th>Who</th><th className="num">Took</th><th className="num">Queued</th><th>Status</th><th>Query</th></tr></thead>
              <tbody>
                {w.slow.map((q: any) => (
                  <tr key={q.statement_id}><td>{fmtDayShort(q.start_time)}</td><td>{q.identity}</td><td className="num mono">{fmtDurationMs(q.total_ms)}</td>
                    <td className="num mono">{fmtDurationMs(q.queued_ms)}</td><td>{q.status}</td>
                    <td className="mono" title={q.statement_id}>{q.text}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

AreaContent.register("compute", "warehouse_detail", WarehouseDetailContent);
