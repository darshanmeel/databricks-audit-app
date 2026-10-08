// the Genie tab: who uses Genie, in which workspace and env,
// and how (Genie Code, Genie agents or Genie One; built into apps or used in the browser). Reads
// genie_usage (one row per day, workspace, surface, channel, agent, run-as and free/paid split).

import React from "react";

import { estLabel, fmtChange, fmtChangeVs, fmtDbu, fmtInt, fmtMoney } from "../format";
import { Names, resolveName } from "../components/names";
import { numOrZero, useFindingAgg, useFindingData } from "../components/hooks";
import { AreaContent, Card, EmptyState, MetricCard, MetricGrid, noDataText } from "../components/primitives";
import { ChartNote } from "../components/charts";
import { Columns, RankedBars } from "../components/charts_more";
import { envLabel } from "./tab_money";
import { DAY_ROWS_MAX, GrainSwitch, MONTH_ROWS_MAX, fillMonths, fmtMonth, latest } from "../components/grain";
import type { Grain } from "../components/grain";

const GENIE_SURFACE_LABEL: Record<string, string> = {
  GENIE_CODE: "Genie Code (assistant: writes SQL or Python, a person runs it)",
  GENIE_AGENTS: "Genie agents (answers questions and runs SQL)",
  GENIE_ONE: "Genie One (chat)",
};
const GENIE_SURFACE_SHORT: Record<string, string> = { GENIE_CODE: "Genie Code", GENIE_AGENTS: "Genie agents", GENIE_ONE: "Genie One" };
const GENIE_USE_LABEL: Record<string, string> = {
  built_in: "Built into apps or agents",
  browser: "People in the browser",
  unknown: "Not recorded",
};

function genieSurfaceLabel(surface: any, short?: any) {
  if (surface == null || surface === "") return "Not recorded";
  return (short ? GENIE_SURFACE_SHORT : GENIE_SURFACE_LABEL)[surface] || surface;
}
function genieChannelLabel(channel: any) {
  if (channel == null || channel === "") return "Not recorded";
  return String(channel).toUpperCase() === "UI" ? "UI" : `Non-UI · ${channel}`;
}
// Built in: reached outside the browser, or run as a service principal. Browser: UI and a person.
function genieUse(r: any) {
  const ui = r.channel != null && String(r.channel).toUpperCase() === "UI";
  if (r.identity_type === "service_principal" || (r.channel != null && r.channel !== "" && !ui)) return "built_in";
  if (ui && r.identity_type === "user") return "browser";
  return "unknown";
}
function genieWho(r: any) {
  if (!r.run_as) return "Not recorded";
  return r.identity_type === "service_principal" ? `service principal (${r.run_as})` : r.run_as;
}
function genieWsEnv(wsId: any) {
  const rec = typeof Names !== "undefined" && Names.workspaceRecord ? Names.workspaceRecord(wsId) : null;
  return (rec && rec.env) || "unknown";
}

// Sums $ (after the what-if discount) and DBUs per key, current and previous period apart.
function genieRollup(rows: any, keyFn: any, disc: any) {
  const m = new Map();
  rows.forEach((r: any) => {
    const k = keyFn(r);
    const e = m.get(k) || { key: k, usd: 0, prevUsd: 0, dbus: 0, prevDbus: 0, sample: r };
    const usd = numOrZero(r.usd_list) * (1 - disc);
    if (r.period === "current") { e.usd += usd; e.dbus += numOrZero(r.dbus); } else { e.prevUsd += usd; e.prevDbus += numOrZero(r.dbus); }
    m.set(k, e);
  });
  return [...m.values()];
}

// Server-side sums (exact whatever the row count) as rows: {<col>: key value, ..., value}.
function genieAggRows(state: any, cols: any) {
  if (!state || state.phase !== "ready" || state.outcome !== "ok_rows") return null;
  return (state.data.groups || []).map((g: any) => {
    const o: Record<string, number> = { value: numOrZero(g.value) };
    cols.forEach((c: any, i: any) => { o[c] = g.key ? g.key[i] : null; });
    return o;
  });
}
// Joins a $ sum and a DBU sum over the same group columns into genieRollup's row shape.
function genieJoin(usdState: any, dbuState: any, cols: any) {
  const usd = genieAggRows(usdState, cols);
  const dbu = genieAggRows(dbuState, cols);
  if (!usd || !dbu) return null;
  const m = new Map();
  const keyOf = (o: any) => cols.map((c: any) => String(o[c])).join("\u0000");
  usd.forEach((o: any) => m.set(keyOf(o), { ...o, usd_list: o.value, dbus: 0 }));
  dbu.forEach((o: any) => { const e = m.get(keyOf(o)) || { ...o, usd_list: 0 }; e.dbus = o.value; m.set(keyOf(o), e); });
  return [...m.values()];
}

function GenieChangeCell({ cur, prev, covered }: LooseProps) {
  if (!covered) return <span className="muted" title="The export's billing history doesn't reach back to the previous period.">not comparable</span>;
  const t = fmtChange(cur, prev);
  return <span>{t === "-" ? "-" : t}</span>;
}

function GeniePanel({ filters, meta, maxCat, roleAreas, goTo, onVerdict }: LooseProps) {
  const f = [filters.window, filters.workspaceIds, filters.envs] as const;
  // Exact totals from server-side sums; only the who-uses-it table reads rows (capped at 5,000).
  const kindUsd = useFindingAgg("genie_usage", ...f, ["period", "surface", "is_free"], "sum", "usd_list");
  const kindDbu = useFindingAgg("genie_usage", ...f, ["period", "surface", "is_free"], "sum", "dbus");
  const useUsd = useFindingAgg("genie_usage", ...f, ["period", "channel", "identity_type"], "sum", "usd_list");
  const useDbu = useFindingAgg("genie_usage", ...f, ["period", "channel", "identity_type"], "sum", "dbus");
  const wsUsd = useFindingAgg("genie_usage", ...f, ["period", "workspace_id"], "sum", "usd_list");
  const wsDbu = useFindingAgg("genie_usage", ...f, ["period", "workspace_id"], "sum", "dbus");
  const agentUsd = useFindingAgg("genie_usage", ...f, ["period", "workspace_id", "agent_id"], "sum", "usd_list");
  const dayUsd = useFindingAgg("genie_usage", ...f, ["period", "usage_date"], "sum", "usd_list");
  // Months reach past the export window: the monthly billing check carries Genie as its own product line.
  const monthUsd = useFindingAgg("cost_monthly_actuals", ...f, ["month_start", "billing_origin_product"], "sum", "net_list_cost_usd");
  const [grain, setGrain] = React.useState<Grain>("day");
  const whoDbu = useFindingAgg("genie_usage", ...f, ["period", "identity_type", "run_as"], "sum", "dbus");
  const unpricedAgg = useFindingAgg("genie_usage", ...f, ["period"], "sum", "unpriced_dbus");
  const rowsState = useFindingData("genie_usage", ...f, 5000);
  const srcAgg = useFindingAgg("query_provenance_by_source", ...f, ["source_kind"], "sum", "query_count");
  const [showAll, setShowAll] = React.useState(false);

  const states = [kindUsd, kindDbu, useUsd, useDbu, wsUsd, wsDbu, agentUsd, dayUsd, whoDbu, unpricedAgg, rowsState];
  const loading = states.some((st) => st.phase === "loading");
  const failed = states.find((st) => st.phase === "error");
  // An export from before this check: not run, which is not the same as no Genie use.
  const notRun = rowsState.phase === "ready" && rowsState.outcome === "not_assessed" ? rowsState : null;
  const disc = kindUsd.data ? numOrZero(kindUsd.data.discount_pct) : 0;
  const rows = rowsState.phase === "ready" && rowsState.outcome === "ok_rows" ? rowsState.data.rows : null;
  const truncated = rows && rowsState.data!.rows_in_window != null && rowsState.data!.rows_in_window > rows.length;
  const covered = rows ? rows.every((r) => r.previous_period_covered !== false) : true;

  const kinds = genieJoin(kindUsd, kindDbu, ["period", "surface", "is_free"]) || [];
  const cur = kinds.filter((r) => r.period === "current");
  const usdCur = cur.reduce((s, r) => s + numOrZero(r.usd_list), 0) * (1 - disc);
  const usdPrev = kinds.filter((r) => r.period === "previous").reduce((s, r) => s + numOrZero(r.usd_list), 0) * (1 - disc);
  const dbusCur = cur.reduce((s, r) => s + numOrZero(r.dbus), 0);
  const freeDbus = cur.filter((r) => r.is_free).reduce((s, r) => s + numOrZero(r.dbus), 0);
  const unpricedDbus = (genieAggRows(unpricedAgg, ["period"]) || []).filter((r: any) => r.period === "current").reduce((s: any, r: any) => s + r.value, 0);

  const bySurface = genieRollup(kinds, (r: any) => r.surface || "", disc).sort((a, b) => b.usd - a.usd || b.dbus - a.dbus);
  const byUse = genieRollup(genieJoin(useUsd, useDbu, ["period", "channel", "identity_type"]) || [], genieUse, disc);
  const useOf = (k: any) => byUse.find((e) => e.key === k) || { usd: 0, dbus: 0, prevUsd: 0 };
  const wsCur = (genieJoin(wsUsd, wsDbu, ["period", "workspace_id"]) || []).filter((r) => r.period === "current");
  const byWs = genieRollup(wsCur, (r: any) => r.workspace_id, disc).sort((a, b) => b.usd - a.usd || b.dbus - a.dbus);
  const byEnv = genieRollup(wsCur, (r: any) => genieWsEnv(r.workspace_id), disc).sort((a, b) => b.usd - a.usd);
  const agentRows = (genieAggRows(agentUsd, ["period", "workspace_id", "agent_id"]) || []).filter((r: any) => r.period === "current" && r.agent_id);
  const byAgent = agentRows.map((r: any) => ({ key: `${r.workspace_id}\u0000${r.agent_id}`, usd: r.value * (1 - disc) })).sort((a: any, b: any) => b.usd - a.usd);
  const whoRows = genieAggRows(whoDbu, ["period", "identity_type", "run_as"]) || [];
  const people = new Set(whoRows.filter((r: any) => r.period === "current" && r.identity_type === "user" && r.value !== 0).map((r: any) => r.run_as)).size;
  const agentCount = new Set(agentRows.map((r: any) => `${r.workspace_id}:${r.agent_id}`)).size;
  const byWho = rows
    ? genieRollup(rows, (r: any) => [r.run_as, r.identity_type, r.workspace_id, r.surface, r.channel].join("\u0000"), disc)
      .filter((e) => e.usd > 0 || e.dbus > 0 || e.prevUsd > 0)
      .sort((a, b) => b.usd - a.usd || b.dbus - a.dbus)
    : [];
  const daily = (genieAggRows(dayUsd, ["period", "usage_date"]) || []).filter((r: any) => r.period === "current")
    .map((r: any) => ({ key: String(r.usage_date).slice(0, 10), usd: r.value * (1 - disc) })).sort((a: any, b: any) => (a.key < b.key ? -1 : 1));
  const dayShown = latest(daily, DAY_ROWS_MAX);
  const monthDisc = monthUsd.data ? numOrZero(monthUsd.data.discount_pct) : 0;
  const months = monthUsd.phase === "ready" && monthUsd.outcome === "ok_rows"
    ? latest(fillMonths((monthUsd.data.groups || []).filter((g) => g.key && g.key[1] === "GENIE")
      .map((g) => ({ key: String(g.key[0]).slice(0, 10), value: numOrZero(g.value) * (1 - monthDisc) }))), MONTH_ROWS_MAX)
    : null;
  const topSurface = bySurface.find((e) => e.usd > 0 || e.dbus > 0);
  const genieSql = srcAgg.phase === "ready" && srcAgg.outcome === "ok_rows"
    ? numOrZero(((srcAgg.data.groups || []).find((g) => g.key && g.key[0] === "genie") || {}).value) : null;
  const canOpenQueries = !roleAreas || roleAreas.includes("queries");

  const verdict = loading
    ? "Loading Genie usage..."
    : failed ? "Couldn't load Genie usage."
    : notRun ? "Genie usage isn't in this export: re-run the export."
    : !cur.length
      ? noDataText(meta, filters.window)
      : `Genie cost ${fmtMoney(usdCur, 0)} in ${filters.window}d${covered ? ` (${fmtChangeVs(usdCur, usdPrev, "the period before") || "same as the period before"})` : ""}`
        + `; ${fmtMoney(useOf("built_in").usd, 0)} built into apps or agents, ${fmtMoney(useOf("browser").usd, 0)} by people in the browser`
        + (topSurface ? `; most of it ${genieSurfaceLabel(topSurface.key, true)}.` : ".");
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);

  if (loading) return <div className="loading-note">Loading Genie usage...</div>;
  if (failed) return <ChartNote state={failed} label="genie_usage" />;
  if (notRun) return <ChartNote state={notRun} label="genie_usage" />;
  if (!cur.length) return <EmptyState meta={meta} windowDays={filters.window} />;

  const whoShown = showAll ? byWho : byWho.slice(0, (maxCat || 8) * 2);
  return (
    <div className="genie-panel">
      <MetricGrid>
        <MetricCard label="Genie spend" value={fmtMoney(usdCur, 0)}
          facts={[
            { label: "Before", value: covered ? fmtMoney(usdPrev, 0) : "not comparable", detail: covered ? (fmtChange(usdCur, usdPrev) === "-" ? null : fmtChange(usdCur, usdPrev)) : "export too short" },
            { label: "Basis", value: estLabel(disc) },
          ]} />
        <MetricCard label="DBUs" value={fmtDbu(dbusCur, 0)}
          facts={[
            { label: "Free tier", value: fmtDbu(freeDbus, 0), detail: "a real $0" },
            unpricedDbus > 0 ? { label: "No price", value: fmtDbu(unpricedDbus, 0), detail: "not in the $", tone: "warn" } : null,
          ].filter(Boolean)} />
        <MetricCard label="Built into apps or agents" value={fmtMoney(useOf("built_in").usd, 0)}
          facts={[
            { label: "People in the browser", value: fmtMoney(useOf("browser").usd, 0) },
            useOf("unknown").dbus > 0 ? { label: "Not recorded", value: fmtDbu(useOf("unknown").dbus, 0) } : null,
          ].filter(Boolean)} />
        <MetricCard label="People using Genie" value={fmtInt(people)}
          facts={[{ label: "Workspaces", value: fmtInt(byWs.length) }, { label: "Genie agents", value: fmtInt(agentCount) }]} />
      </MetricGrid>
      <div className="metric-note">List price. Only DBUs the bill marks as free count as $0: the 150 free DBUs per named user a month and promotional discounts (such as 25% on Genie Code) are not taken off otherwise, so your bill may be lower.</div>

      <div className="grid-2">
        <Card title="By kind of use" right={<span className="muted">this period · change against the one before</span>}>
          <div className="table-wrap">
            <table className="data genie-table">
              <thead><tr><th>Kind</th><th className="num">DBUs</th><th className="num">$</th><th className="num">Change</th></tr></thead>
              <tbody>
                {bySurface.map((e) => (
                  <tr key={`s:${e.key}`}>
                    <td>{genieSurfaceLabel(e.key || null)}</td>
                    <td className="num mono">{fmtDbu(e.dbus, 0)}</td>
                    <td className="num mono">{fmtMoney(e.usd, 0)}</td>
                    <td className="num mono"><GenieChangeCell cur={e.usd} prev={e.prevUsd} covered={covered} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card title="Built in or in the browser" right={<span className="muted">built in = channel other than UI, or a service principal</span>}>
          <div className="table-wrap">
            <table className="data genie-table">
              <thead><tr><th>How Genie was reached</th><th className="num">DBUs</th><th className="num">$</th><th className="num">Change</th></tr></thead>
              <tbody>
                {["built_in", "browser", "unknown"].filter((k) => byUse.some((e) => e.key === k)).map((k) => (
                  <tr key={`u:${k}`}>
                    <td>{GENIE_USE_LABEL[k]}</td>
                    <td className="num mono">{fmtDbu(useOf(k).dbus, 0)}</td>
                    <td className="num mono">{fmtMoney(useOf(k).usd, 0)}</td>
                    <td className="num mono"><GenieChangeCell cur={useOf(k).usd} prev={useOf(k).prevUsd} covered={covered} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      <Card title="Who uses Genie, where and how" right={<span className="muted">{`${fmtInt(byWho.length)} rows · by $ this period`}</span>}>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr><th>User or service principal</th><th>Workspace</th><th>Env</th><th>Kind</th><th>Channel</th><th className="num">DBUs</th><th className="num">$</th><th className="num">Change</th></tr>
            </thead>
            <tbody>
              {whoShown.map((e) => {
                const r = e.sample;
                return (
                  <tr key={e.key}>
                    <td>{genieWho(r)}</td>
                    <td>{resolveName("workspace", r.workspace_id, r.workspace_id) || r.workspace_id}</td>
                    <td>{envLabel(genieWsEnv(r.workspace_id))}</td>
                    <td>{genieSurfaceLabel(r.surface, true)}</td>
                    <td>{genieChannelLabel(r.channel)}</td>
                    <td className="num mono">{fmtDbu(e.dbus, 0)}</td>
                    <td className="num mono">{fmtMoney(e.usd, 0)}</td>
                    <td className="num mono"><GenieChangeCell cur={e.usd} prev={e.prevUsd} covered={covered} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {byWho.length > whoShown.length || showAll ? (
          <div className="data-foot">
            <button type="button" className="link-out" onClick={() => setShowAll(!showAll)}>
              {showAll ? "Show fewer" : `Show all ${fmtInt(byWho.length)}`}
            </button>
          </div>
        ) : null}
        {truncated && <div className="metric-note">{`The first ${fmtInt(rows.length)} of ${fmtInt(rowsState.data!.rows_in_window)} rows are shown here; narrow the window or workspaces for the rest.`}</div>}
      </Card>

      <div className="grid-2">
        <Card title="By workspace" right={<span className="muted">{byEnv.map((e) => `${envLabel(e.key)} ${fmtMoney(e.usd, 0)}`).join(" · ")}</span>}>
          <RankedBars
            items={byWs.map((e) => ({ name: `${resolveName("workspace", e.key, e.key) || e.key} · ${envLabel(genieWsEnv(e.key))}`, id: e.key, value: e.usd }))}
            n={maxCat} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText={noDataText(meta, filters.window)}
          />
        </Card>
        <Card title="Top Genie agents" right={<span className="muted">by $ this period</span>}>
          {byAgent.length ? (
            <RankedBars
              items={byAgent.map((e: any) => {
                const [ws, agent] = e.key.split("\u0000");
                return { name: `Genie agent · ${resolveName("workspace", ws, ws) || ws}`, id: agent, value: e.usd };
              })}
              n={maxCat} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$"
            />
          ) : <div className="chart-note-oneline">No Genie agent usage in this window.</div>}
          <div className="metric-note" style={{ marginTop: 6 }}>
            {genieSql != null ? `Genie agents ran ${fmtInt(genieSql)} SQL statements on warehouses in this window. ` : ""}
            {canOpenQueries
              ? <button type="button" className="link-out" onClick={() => goTo({ tab: "queries", subtab: "heavy" })}>See them in Queries › Heavy queries →</button>
              : "Their SQL is on Queries › Heavy queries in the data engineer lens."}
          </div>
        </Card>
      </div>

      <Card title={grain === "day" ? "Genie spend per day" : "Genie spend per month"}
        right={<span className="card-right-row"><GrainSwitch value={grain} onChange={setGrain} /><span className="muted">{`${grain === "day" ? "this period" : "billing history"} · ${estLabel(grain === "day" ? disc : monthDisc)}`}</span></span>}>
        {grain === "day" ? (
          <Columns
            days={dayShown.map((d: any) => d.key)}
            series={[{ name: "$ per day", values: dayShown.map((d: any) => d.usd) }]}
            valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" dollarAxis
            note={daily.length > dayShown.length ? `Latest ${fmtInt(dayShown.length)} of ${fmtInt(daily.length)} days.` : null}
          />
        ) : months && months.length ? (
          <Columns
            days={months.map((m) => m.key)}
            series={[{ name: "$ per month", values: months.map((m) => m.value) }]}
            valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" dollarAxis xFmt={fmtMonth} xName="Month" wide
            note={`${fmtInt(months.length)} month${months.length === 1 ? "" : "s"} of Genie billing, at most ${MONTH_ROWS_MAX}; this month is partial.`}
          />
        ) : months ? <div className="muted">No Genie spend in the billing history.</div> : <ChartNote state={monthUsd} label="cost_monthly_actuals" />}
      </Card>
    </div>
  );
}

AreaContent.register("genie", null, GeniePanel);
