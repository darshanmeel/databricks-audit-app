// ML & AI area content (redesign section 6): spend / endpoints
// / gateway sub-tabs. Registers with AreaContent (primitives.tsx), which already renders PageIntro
// + SubTabs + this component + the checks table. Old strip/chart body fully replaced.

import React from "react";

import { estLabel, fmtDbu, fmtInt, fmtMoney, fmtPct } from "../format";
import { DbxLink, resolveName, useNames, wsPhrase } from "../components/names";
import { groupsToEntries, numOrZero, regroupByKeyIndex, useFindingAgg, useFindingData } from "../components/hooks";
import { COST_MONEY_COL } from "../components/tab_registry";
import { AreaContent, Card, CaveatNote, LinkOut, MetricCard, MetricGrid, NoDataBlock, RowStatusPill, StatusPill, bandPillKind, isUnderFloor } from "../components/primitives";
import { ChartNote, shortId } from "../components/charts";
import { RankedBars } from "../components/charts_more";
import { windowFacts } from "../components/overview_tile";

// "name · workspace": the same endpoint name in prd and uat is two endpoints, never one bar. With no
// name the workspace goes first, so a clipped label still says where the spend is.
const NO_ENDPOINT_NAME = "(no endpoint name)";
function endpointLabel(name: any, workspaceId: any, endpointId?: any) {
  const ws = workspaceId == null ? null : (resolveName("workspace", workspaceId, workspaceId) || workspaceId);
  if (name) return ws ? `${name} · ${ws}` : name;
  const n = endpointId ? `endpoint ${shortId(endpointId)}` : NO_ENDPOINT_NAME;
  return ws ? `${ws} · ${n}` : n;
}

// ═══════════════════════ Spend ═══════════════════════
function MlaiSpendContent({ filters, maxCat, onExternalJump, onVerdict }: LooseProps) {
  // COMPUTE_TIME/GPU_TIME/TOKEN are different physical units (the header's own rule) -- grouped
  // by usage_type too, so the ranked list below can pick one unit rather than mixing them.
  // Id and name both in the key: Vector Search bills by name with no id, so an id-only key merged
  // every such endpoint into one bar under whichever name came last.
  const epAgg = useFindingAgg("cost_by_serving_endpoint", filters.window, filters.workspaceIds, filters.envs, ["endpoint_id", "endpoint_name", "usage_type"], "sum", "net_usage_quantity");
  // endpoint_id -> its workspace, so two endpoints sharing a name never merge into one bar.
  const epWsAgg = useFindingAgg("cost_by_serving_endpoint", filters.window, filters.workspaceIds, filters.envs, ["endpoint_id", "workspace_id"], "sum", "net_usage_quantity");
  const modeAgg = useFindingAgg("cost_serving_mode_by_endpoint", filters.window, filters.workspaceIds, filters.envs, ["endpoint_name", "is_launch_sku"], "sum", "net_list_cost");
  const modeWsAgg = useFindingAgg("cost_serving_mode_by_endpoint", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "endpoint_name"], "sum", "net_list_cost");
  const vsAgg = useFindingAgg("cost_vector_search_spend", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "endpoint_name"], "sum", "net_list_cost");

  // Totals come from the SKU-day table, so they match the Cost pages under any filter.
  const prodAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["billing_origin_product"], "sum", COST_MONEY_COL);
  const prodReady = prodAgg.phase === "ready" && prodAgg.outcome === "ok_rows";
  const prodUsd = (code: any) => (prodReady ? numOrZero(((prodAgg.data.groups || []).find((g) => g.key && g.key[0] === code) || {}).value) : null);
  const prodDisc = prodReady ? numOrZero(prodAgg.data.discount_pct) : 0;
  const modeReady = modeAgg.phase === "ready" && modeAgg.outcome === "ok_rows";
  const vsReady = vsAgg.phase === "ready" && vsAgg.outcome === "ok_rows";
  const modeTotal = modeReady ? numOrZero(modeAgg.data.total_value) : null;
  const vsTotal = vsReady ? numOrZero(vsAgg.data.total_value) : null;
  const totalSpend = prodReady ? prodUsd("MODEL_SERVING")! + prodUsd("VECTOR_SEARCH")! : (modeTotal || 0) + (vsTotal || 0);
  const disc = modeReady ? numOrZero(modeAgg.data.discount_pct) : (vsReady ? numOrZero(vsAgg.data.discount_pct) : 0);
  // One total once every source has answered, never a partial sum that grows while loading.
  const spendSettled = [prodAgg, modeAgg, vsAgg].every((st) => st.phase !== "loading");
  const spendKnown = prodReady || modeReady || vsReady;

  let launchPct = null;
  if (modeReady) {
    const byLaunch = regroupByKeyIndex(modeAgg.data, 1);
    const launchVal = numOrZero((byLaunch.find((e) => e.name === true) || {}).value);
    const total = byLaunch.reduce((s, e) => s + e.value, 0) || 1;
    launchPct = (launchVal / total) * 100;
  }

  // Unfolded -- RankedBars (n=maxCat below) does the one top-N-plus-Other fold; folding here too
  // would re-rank the first fold's own "Other" bucket as if it were one more real endpoint.
  const epReady = epAgg.phase === "ready" && epAgg.outcome === "ok_rows";
  const epWs = new Map(epWsAgg.phase === "ready" && epWsAgg.outcome === "ok_rows"
    ? (epWsAgg.data.groups || []).filter((g) => g.key && g.key[0] != null).map((g) => [g.key[0], g.key[1]]) : []);
  // One bar per endpoint: by id when it has one (its name may be missing on some rows), else by name.
  const epItems = epReady ? (() => {
    const byEp = new Map();
    epAgg.data.groups.filter((g) => g.key && g.key[2] === "COMPUTE_TIME").forEach((g) => {
      const [id, name] = g.key;
      const k = id != null ? `id:${id}` : `name:${name || ""}`;
      const cur = byEp.get(k) || { id, name: null, value: 0 };
      cur.name = cur.name || name;
      cur.value += numOrZero(g.value);
      byEp.set(k, cur);
    });
    return [...byEp.values()].map((e) => ({ name: endpointLabel(e.name, e.id != null ? epWs.get(e.id) : null, e.id), value: e.value }));
  })() : null;

  const modeWsReady = modeWsAgg.phase === "ready" && modeWsAgg.outcome === "ok_rows";
  const modeByEndpoint = modeWsReady ? groupsToEntries(modeWsAgg.data, (k) => endpointLabel(k && k[1], k && k[0])) : null;
  const vsItems = vsReady ? groupsToEntries(vsAgg.data, (k) => endpointLabel(k && k[1], k && k[0])) : null;

  const verdictSentence = spendSettled && (modeReady || vsReady)
    ? `${fmtMoney(totalSpend * (1 - (prodReady ? prodDisc : disc)), 0)} on model serving and Vector Search over the last ${filters.window} days${launchPct != null ? `; ${fmtPct(launchPct, 0)} of model-serving $ is scale-from-zero` : ""}.`
    : "Loading ML & AI spend...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <MetricGrid>
        <MetricCard label="ML & AI spend" value={!spendSettled ? "..." : spendKnown ? fmtMoney(totalSpend * (1 - (prodReady ? prodDisc : disc)), 0) : "-"} facts={[{ label: "Basis", value: estLabel(prodReady ? prodDisc : disc) }, { label: "Scope", value: "Model serving + Vector Search" }]} />
        <MetricCard label="Scale-from-zero launches" value={launchPct != null ? fmtPct(launchPct, 0) : "-"} facts={[{ label: "Basis", value: "Model-serving $ on launch (cold-start) SKUs", tone: numOrZero(launchPct) > 30 ? "warn" : undefined }]} tone={numOrZero(launchPct) > 30 ? "warn" : null} />
        <MetricCard label="Vector Search spend" value={!spendSettled ? "..." : prodReady ? fmtMoney(prodUsd("VECTOR_SEARCH")! * (1 - prodDisc), 0) : (vsTotal != null ? fmtMoney(vsTotal * (1 - disc), 0) : "-")} facts={[{ label: "Basis", value: estLabel(prodReady ? prodDisc : disc) }]} />
      </MetricGrid>

      <Card title="Compute time by endpoint" right={<span className="muted">DBU, compute time only</span>}>
        {epItems ? (epItems.length ? <RankedBars items={epItems} n={maxCat} valueFmt={(v) => fmtDbu(v, 0)} unitLabel="DBU" /> : <div className="chart-note ok"><div className="chart-note-text">No COMPUTE_TIME usage in this window (GPU_TIME/TOKEN rows, if any, are a different unit and not shown here).</div></div>) : <ChartNote state={epAgg} label="cost_by_serving_endpoint" />}
      </Card>

      <div className="grid-2">
        <Card title="Model-serving $ by endpoint" right={<span className="muted">{estLabel(disc)}</span>}>
          {modeByEndpoint ? <RankedBars items={modeByEndpoint} n={maxCat} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" /> : <ChartNote state={modeAgg} label="cost_serving_mode_by_endpoint" />}
        </Card>
        <Card title="Vector Search $ by endpoint" right={<span className="muted">{estLabel(disc)}</span>}>
          {vsItems ? <RankedBars items={vsItems} n={maxCat} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" /> : <ChartNote state={vsAgg} label="cost_vector_search_spend" />}
        </Card>
      </div>

      <div className="metric-note">
        GenAI token & GPU spend has its own home: <LinkOut onClick={() => onExternalJump("cost_genai_token_gpu")}>Cost &gt; By resource →</LinkOut>
      </div>
    </div>
  );
}
AreaContent.register("mlai", "spend", MlaiSpendContent);

// ═══════════════════════ Endpoints ═══════════════════════
const DAY_WARN_USD = 50; // cost_serving_mode_by_endpoint's header default, until its settings load

// Dedupe compute_serving_endpoint_cost_status's row fan-out (one row per served entity) down to
// one row per endpoint -- net_dbus/est_usd_list/endpoint_requests_window already repeat identically
// across an endpoint's served-entity rows (the header's own note), so the first row wins.
function dedupeByEndpoint(rows: any) {
  const byKey = new Map();
  (rows || []).forEach((r: any) => {
    const key = `${r.workspace_id}:${r.endpoint_id || r.endpoint_name || "(unknown)"}`;
    if (!byKey.has(key)) byKey.set(key, r);
  });
  return [...byKey.values()];
}

// Per-endpoint, per-day $ -- compute_serving_endpoint_usage splits the day's bill across served
// entities by request share, so their rows add back up to it.
// Discounted like every other $ figure on this page, so the "days over the warning line" count
// judges the same dollars the cards above show, not the undiscounted list price.
function dailyCostByEndpoint(rows: any, discountPct: any) {
  const disc = numOrZero(discountPct);
  const byEndpoint = new Map();
  (rows || []).forEach((r: any) => {
    const key = `${r.workspace_id}:${r.endpoint_id || r.endpoint_name || "(unknown)"}`;
    const byDay = byEndpoint.get(key) || new Map();
    const day = String(r.usage_date).slice(0, 10);
    byDay.set(day, (byDay.get(day) || 0) + numOrZero(r.est_usd_list) * (1 - disc));
    byEndpoint.set(key, byDay);
  });
  return byEndpoint;
}

function DailyCostRange({ min, max, warnAt }: LooseProps) {
  const scale = Math.max(max, warnAt * 1.2, 1);
  const lo = (min / scale) * 100, span = ((max - min) / scale) * 100, warnX = (warnAt / scale) * 100;
  return (
    <div className="daily-cost-range">
      <div className="dcr-track">
        <div className="dcr-span" style={{ left: `${lo}%`, width: `${span}%`, background: max >= warnAt ? "var(--st-warn)" : "var(--c1)" }} />
        <div className="dcr-warn" style={{ left: `${warnX}%` }} title={`Warning line: ${fmtMoney(warnAt, 0)}/day`} />
      </div>
      <div className="dcr-scale"><span>$0</span><span>{fmtMoney(scale, 0)}</span></div>
    </div>
  );
}

// Cents under $10, so a small endpoint's $ divided by its requests gives the $ per request shown.
function endpointMoney(usd: any) {
  return fmtMoney(usd, usd < 10 ? 2 : 0);
}

function EndpointCard({ row, errorInfo, daily, warnAt, onExternalJump }: LooseProps) {
  const requests = numOrZero(row.endpoint_requests_window);
  const usd = numOrZero(row.est_usd_list) * (1 - numOrZero(row.__discountPct));
  const perReq = requests > 0 ? usd / requests : null;
  const band = row.status === "CRITICAL" ? "critical" : row.status === "WARN" ? "warn" : row.status === "NOT_ASSESSED" ? "not_assessed" : "ok";
  const days = daily ? [...daily.entries()].sort((a, b) => a[0].localeCompare(b[0])) : [];
  const min = days.length ? Math.min(...days.map((d) => d[1])) : null;
  const max = days.length ? Math.max(...days.map((d) => d[1])) : null;
  // Not assessed = no request telemetry for it (Vector Search, or outside served entities), so
  // zero requests there is unknown traffic, not idle.
  const untracked = row.status === "NOT_ASSESSED";
  const idle = requests === 0 && usd > 0 && !untracked;
  const isVectorSearch = /VECTOR_SEARCH/i.test(String(row.products || ""));
  return (
    <div className={`endpoint-card tone-${band}`}>
      <div className="ec-head">
        <div>
          <div className="ec-name mono">{row.endpoint_name || row.endpoint_id || NO_ENDPOINT_NAME}</div>
          <div className="ec-sub">
            {wsPhrase(row.workspace_id)} · {row.products || "serving"}
            {row.endpoint_name && <React.Fragment>{" "}<DbxLink kind="endpoint" workspaceId={row.workspace_id} id={row.endpoint_name} /></React.Fragment>}
          </div>
        </div>
        {isUnderFloor(row) ? <RowStatusPill row={row} floor={row.__floor} /> : <StatusPill kind={bandPillKind(band.toUpperCase())} />}
      </div>
      <div className="ec-stats">
        <div><span className="ec-stat-label">Requests</span>{untracked && requests === 0 ? <span className="ec-stat-value muted">not tracked</span> : <span className="ec-stat-value mono">{fmtInt(requests)}</span>}</div>
        <div><span className="ec-stat-label">Errors</span>{errorInfo ? <span className="ec-stat-value mono">{fmtInt(errorInfo.errors)}</span> : <span className="ec-stat-value muted">no error data</span>}</div>
        <div><span className="ec-stat-label">{`$ over ${row.__window}d`}</span><span className="ec-stat-value mono">{endpointMoney(usd)}</span></div>
        <div><span className="ec-stat-label">$ per request</span><span className="ec-stat-value mono">{perReq != null ? fmtMoney(perReq, 2) : "n/a"}</span></div>
      </div>
      {min != null && (
        <div>
          <div className="ec-range-head"><span>Cost per day</span><span>dashed line = {fmtMoney(warnAt, 0)}/day warning</span></div>
          <DailyCostRange min={min} max={max} warnAt={warnAt} />
        </div>
      )}
      <div className="ec-mode">{row.tracking_status}</div>
      {idle && (
        <div className="ec-idle-note">
          <b>No requests in the window.</b> Billed {endpointMoney(usd)} anyway.{" "}
          {row.tracking_status && row.tracking_status.indexOf("OFF") >= 0
            ? "Usage tracking may be off -- open it in Databricks and check its own metrics before assuming it is idle."
            : "Before deleting it, open it in Databricks and confirm nothing else depends on it."}
        </div>
      )}
      {untracked && usd > 0 && (
        <div className="ec-mode">
          {isVectorSearch
            ? <React.Fragment>{`Vector Search has no request telemetry here; ${endpointMoney(usd)} billed. Its query traffic is under `}<LinkOut onClick={() => onExternalJump && onExternalJump("access_vector_search_traffic")}>Vector Search query traffic →</LinkOut></React.Fragment>
            : <React.Fragment>{`Not a tracked model-serving endpoint, so request counts don't apply; ${endpointMoney(usd)} billed. Its usage is under `}<LinkOut onClick={() => onExternalJump && onExternalJump("cost_genai_token_gpu")}>Cost › By resource →</LinkOut></React.Fragment>}
        </div>
      )}
    </div>
  );
}

function MlaiEndpointsContent({ filters, meta, onVerdict, onExternalJump }: LooseProps) {
  useNames(); // re-render once workspace names (names.tsx) are ready -- resolveName below is read at render time
  const statusState = useFindingData("compute_serving_endpoint_cost_status", filters.window, filters.workspaceIds, filters.envs);
  const trafficState = useFindingData("serving_endpoint_traffic_by_endpoint", filters.window, filters.workspaceIds, filters.envs);
  const usageState = useFindingData("compute_serving_endpoint_usage", filters.window, filters.workspaceIds, filters.envs);
  // Days over the line from the check that bands them, so card and check count the same billing rows.
  const flaggedDaysAgg = useFindingAgg("cost_serving_mode_by_endpoint", filters.window, filters.workspaceIds, filters.envs,
    ["usage_date", "workspace_id", "endpoint_id"], "count", null, { statuses: ["CRITICAL", "WARN"] });
  // one row is enough: only the check's own warning line (its setting in effect) is read here
  const modeProbe = useFindingData("cost_serving_mode_by_endpoint", filters.window, filters.workspaceIds, filters.envs, 1);
  const warnParam = modeProbe.data && modeProbe.data.header && modeProbe.data.header.params
    ? modeProbe.data.header.params.warn_endpoint_usd_per_day : null;
  const dayWarnUsd = warnParam && Number.isFinite(Number(warnParam.value)) ? Number(warnParam.value) : DAY_WARN_USD;
  const dayWarnSource = warnParam ? warnParam.source : "default";

  const statusReady = statusState.phase === "ready" && statusState.outcome === "ok_rows";
  const discPct = statusReady ? numOrZero(statusState.data.discount_pct) : 0;
  const billed = statusReady ? dedupeByEndpoint(statusState.data.rows).map((r) => ({ ...r, __window: filters.window, __discountPct: discPct, __floor: statusState.data.floor })) : null;

  // Requests per endpoint from the traffic check, which sees every served endpoint; the billing-led
  // status check lists only endpoints billed by id or name, so it can miss the busy ones.
  const trafficByKey = React.useMemo(() => {
    const m = new Map();
    if (trafficState.phase === "ready" && trafficState.outcome === "ok_rows") {
      (trafficState.data.rows || []).forEach((r) => {
        const key = r.endpoint_id || r.endpoint_name;
        if (!key) return;
        const prev = m.get(key) || { requests: 0, errors: 0, workspace_id: r.workspace_id, endpoint_id: r.endpoint_id, endpoint_name: r.endpoint_name };
        m.set(key, { ...prev, requests: prev.requests + numOrZero(r.request_count), errors: prev.errors + numOrZero(r.error_requests) });
      });
    }
    return m;
  }, [trafficState]);
  // One list for cards, headline and totals: billed endpoints plus any seen only with traffic.
  const endpoints = billed ? (() => {
    const known = new Set(billed.map((e) => e.endpoint_id || e.endpoint_name));
    const trafficOnly = [...trafficByKey.entries()].filter(([key, t]) => !known.has(key) && t.requests > 0).map(([, t]) => ({
      workspace_id: t.workspace_id, endpoint_id: t.endpoint_id, endpoint_name: t.endpoint_name,
      est_usd_list: 0, status: "OK", products: "serving", tracking_status: "Traffic, but no bill in the window",
      __window: filters.window, __discountPct: discPct, __floor: statusState.data!.floor,
    }));
    return [...billed, ...trafficOnly];
  })() : null;
  const errorsByKey = trafficByKey;
  const requestsOf = (e: any) => {
    const t = trafficByKey.get(e.endpoint_id || e.endpoint_name);
    return t ? Math.max(t.requests, numOrZero(e.endpoint_requests_window)) : numOrZero(e.endpoint_requests_window);
  };

  const usageDiscPct = usageState.phase === "ready" && usageState.outcome === "ok_rows" ? numOrZero(usageState.data.discount_pct) : 0;
  const dailyByEndpoint = React.useMemo(
    () => (usageState.phase === "ready" && usageState.outcome === "ok_rows" ? dailyCostByEndpoint(usageState.data.rows, usageDiscPct) : new Map()),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [usageState, usageDiscPct]
  );

  const winFacts = windowFacts(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);

  let kpis = null;
  if (endpoints) {
    const withTraffic = endpoints.filter((e) => requestsOf(e) > 0);
    const totalRequests = endpoints.reduce((s, e) => s + requestsOf(e), 0);
    let totalErrors = 0;
    trafficByKey.forEach((t) => { totalErrors += t.errors; });
    const idleBilled = endpoints.filter((e) => requestsOf(e) === 0 && numOrZero(e.est_usd_list) > 0 && e.status !== "NOT_ASSESSED");
    const idleUsd = idleBilled.reduce((s, e) => s + numOrZero(e.est_usd_list), 0) * (1 - discPct);
    // Only waste above the floor reaches Waste & savings (DEC-68); the rest is shown apart, not counted.
    const wasteOf = (rows: any) => rows.reduce((s: any, e: any) => s + numOrZero(e.est_wasted_usd_list), 0) * (1 - discPct);
    const idleWasteUsd = wasteOf(idleBilled.filter((e) => !e.below_floor));
    const idleWasteUnderFloor = wasteOf(idleBilled.filter((e) => e.below_floor));
    const largestIdle = idleBilled.slice().sort((a, b) => numOrZero(b.est_usd_list) - numOrZero(a.est_usd_list))[0];
    const daysOverWarn = flaggedDaysAgg.phase === "ready" && flaggedDaysAgg.outcome === "ok_rows"
      ? (flaggedDaysAgg.data.groups || []).length
      : flaggedDaysAgg.phase === "ready" ? 0 : null;
    kpis = {
      withTraffic: withTraffic.length, total: endpoints.length,
      totalRequests, totalErrors,
      idleUsd, idleWasteUsd, idleWasteUnderFloor, largestIdle,
      daysOverWarn,
    };
  }
  const floorLabel = statusReady && statusState.data.floor && statusState.data.floor.label ? statusState.data.floor.label : "the floor";

  const verdictSentence = kpis
    ? `${fmtInt(kpis.withTraffic)} of ${fmtInt(kpis.total)} endpoints had traffic this window; ${fmtMoney(kpis.idleUsd, 0)} billed with none`
      + (kpis.idleWasteUsd > 0 ? ` (${fmtMoney(kpis.idleWasteUsd, 0)} counted as possible waste)`
        : kpis.idleWasteUnderFloor > 0 ? ` (${fmtMoney(kpis.idleWasteUnderFloor, 0)} under ${floorLabel}, not counted as waste)` : "")
      + "."
    : "Loading endpoints...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <MetricGrid>
        <MetricCard label="Endpoints with traffic" value={kpis ? `${fmtInt(kpis.withTraffic)} of ${fmtInt(kpis.total)}` : "-"} facts={winFacts.length ? winFacts : null} />
        <MetricCard label="Requests" value={kpis ? fmtInt(kpis.totalRequests) : "-"} facts={kpis && kpis.totalRequests > 0 ? [{ label: "Errors", value: fmtInt(kpis.totalErrors), detail: fmtPct((kpis.totalErrors / kpis.totalRequests) * 100, 1), tone: kpis.totalErrors > 0 ? "warn" : "ok" }] : null} />
        <MetricCard
          label="Spend with no requests"
          value={kpis ? fmtMoney(kpis.idleUsd, 0) : "-"}
          facts={kpis && kpis.largestIdle ? [
            { label: "Largest bill", value: kpis.largestIdle.endpoint_name || shortId(kpis.largestIdle.endpoint_id), title: kpis.largestIdle.endpoint_name ? undefined : String(kpis.largestIdle.endpoint_id) },
            { label: "Counted as waste", value: fmtMoney(kpis.idleWasteUsd, 0), tone: kpis.idleWasteUsd > 0 ? "warn" : undefined },
            kpis.idleWasteUnderFloor > 0 ? { label: "Under the floor", value: fmtMoney(kpis.idleWasteUnderFloor, 0), detail: "not counted" } : null,
          ].filter(Boolean) : null}
          tone={kpis && kpis.idleWasteUsd > 0 ? "warn" : null}
        />
        <MetricCard label={`Days over ${fmtMoney(dayWarnUsd, 0)}/day`} value={kpis && kpis.daysOverWarn != null ? fmtInt(kpis.daysOverWarn) : "-"} facts={[{ label: "Counted as", value: "endpoint-days the check flagged" }, { label: "Threshold", value: dayWarnSource }]} tone={kpis && numOrZero(kpis.daysOverWarn) > 0 ? "warn" : null} />
      </MetricGrid>

      <CaveatNote>
        Endpoint names are shown in full. Masking applies to people (who sent a request, who owns
        it), not to resources you need to find and fix.
      </CaveatNote>

      {endpoints ? (
        endpoints.length ? (
          <div className="endpoint-grid">
            {endpoints.map((row) => {
              const key = `${row.workspace_id}:${row.endpoint_id || row.endpoint_name}`;
              return (
                <EndpointCard
                  key={key}
                  row={{ ...row, endpoint_requests_window: requestsOf(row) }}
                  errorInfo={errorsByKey.get(row.endpoint_id || row.endpoint_name)}
                  daily={dailyByEndpoint.get(key)}
                  warnAt={dayWarnUsd}
                  onExternalJump={onExternalJump}
                />
              );
            })}
          </div>
        ) : <div className="chart-note ok"><div className="chart-note-text">No endpoint billed in this window.</div></div>
      ) : <ChartNote state={statusState} label="compute_serving_endpoint_cost_status" />}
    </div>
  );
}
AreaContent.register("mlai", "endpoints", MlaiEndpointsContent);

// ═══════════════════════ AI Gateway ═══════════════════════
// Every headline number here is a server-side sum (Api.aggregate), never a client sum over a
// capped row page -- a large account's AI Gateway traffic can exceed the row cap well within a
// window, which used to quietly undercount every total below.
function MlaiGatewayContent({ filters, maxCat, onVerdict }: LooseProps) {
  const reqAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, [], "sum", "total_requests");
  const errAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, [], "sum", "error_requests");
  const rlAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, [], "sum", "rate_limited_requests");
  const inTokAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, [], "sum", "input_tokens");
  const outTokAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, [], "sum", "output_tokens");
  const byEndpointAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "endpoint_name"], "sum", "total_requests");
  // Which endpoint the rejected calls hit, so the answer names where to look, not only how many.
  const rlByEpAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "endpoint_name"], "sum", "rate_limited_requests");
  const errByEpAgg = useFindingAgg("compute_ai_gateway_usage", filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "endpoint_name"], "sum", "error_requests");
  const topOf = (st: any) => {
    if (st.phase !== "ready" || st.outcome !== "ok_rows") return null;
    const g = (st.data.groups || []).filter((x: any) => numOrZero(x.value) > 0).sort((a: any, b: any) => numOrZero(b.value) - numOrZero(a.value))[0];
    return g ? { label: endpointLabel(g.key[1], g.key[0]), value: numOrZero(g.value) } : null;
  };
  const rlTop = topOf(rlByEpAgg), errTop = topOf(errByEpAgg);

  const ready = reqAgg.phase === "ready" && reqAgg.outcome === "ok_rows";
  const rlTotal = rlAgg.phase === "ready" && rlAgg.outcome === "ok_rows" ? numOrZero(rlAgg.data.total_value) : 0;
  const errTotal = errAgg.phase === "ready" && errAgg.outcome === "ok_rows" ? numOrZero(errAgg.data.total_value) : 0;
  const rejectedWhere = (() => {
    if (!rlTotal && !errTotal) return "";
    const parts = [rlTotal ? `${fmtInt(rlTotal)} rate-limited` : null, errTotal ? `${fmtInt(errTotal)} errored` : null].filter(Boolean).join(" and ");
    const tops = [rlTop, errTop].filter((t) => t != null);
    const one = tops.length && tops.every((t) => t.label === tops[0].label);
    const all = one && (!rlTotal || (rlTop && rlTop.value === rlTotal)) && (!errTotal || (errTop && errTop.value === errTotal));
    return `; ${parts}${one ? `, ${all ? "all" : "most"} on ${tops[0].label}` : ""}`;
  })();

  const gwVerdict = ready
    ? `${fmtInt(numOrZero(reqAgg.data.total_value))} AI Gateway requests over the last ${filters.window} days${rejectedWhere}.`
    : "Loading AI Gateway usage...";
  React.useEffect(() => { if (onVerdict) onVerdict(gwVerdict); }, [gwVerdict, onVerdict]);

  if (reqAgg.phase === "ready" && (reqAgg.outcome === "ok_empty_window" || reqAgg.outcome === "not_assessed")) {
    return (
      <NoDataBlock
        sources={[{ name: "system.ai_gateway.usage", unlocks: "AI Gateway request volume, errors and token counts" }]}
        howToFill="Enable the AI Gateway on at least one served entity, or confirm this read runs as an account admin (system.ai_gateway.usage is account-admin-only)."
      />
    );
  }

  const totalRequests = ready ? numOrZero(reqAgg.data.total_value) : null;
  const totalErrors = errAgg.phase === "ready" && errAgg.outcome === "ok_rows" ? numOrZero(errAgg.data.total_value) : null;
  const totalRateLimited = rlAgg.phase === "ready" && rlAgg.outcome === "ok_rows" ? numOrZero(rlAgg.data.total_value) : null;
  const tokensReady = inTokAgg.phase === "ready" && inTokAgg.outcome === "ok_rows" && outTokAgg.phase === "ready" && outTokAgg.outcome === "ok_rows";
  const totalTokens = tokensReady ? numOrZero(inTokAgg.data.total_value) + numOrZero(outTokAgg.data.total_value) : null;

  const byEndpointReady = byEndpointAgg.phase === "ready" && byEndpointAgg.outcome === "ok_rows";
  const byEndpoint = byEndpointReady ? groupsToEntries(byEndpointAgg.data, (k) => endpointLabel(k && k[1], k && k[0])) : null;

  return (
    <div>
      <MetricGrid>
        <MetricCard label="Requests" value={totalRequests != null ? fmtInt(totalRequests) : "-"} />
        <MetricCard label="Errored" value={totalRequests ? fmtPct((totalErrors! / totalRequests) * 100, 1) : "-"} facts={totalErrors != null ? [{ label: "Errored requests", value: fmtInt(totalErrors), tone: totalErrors > 0 ? "warn" : "ok" }, errTop ? { label: "Most on", value: errTop.label, detail: `${fmtInt(errTop.value)} of ${fmtInt(totalErrors)}` } : null].filter(Boolean) : null} tone={totalErrors! > 0 ? "warn" : null} />
        <MetricCard label="Rate-limited" value={totalRequests ? fmtPct((totalRateLimited! / totalRequests) * 100, 1) : "-"} facts={totalRateLimited != null ? [{ label: "Rate-limited requests", value: fmtInt(totalRateLimited), tone: totalRateLimited > 0 ? "warn" : "ok" }, rlTop ? { label: "Most on", value: rlTop.label, detail: `${fmtInt(rlTop.value)} of ${fmtInt(totalRateLimited)}` } : null].filter(Boolean) : null} />
        <MetricCard label="Tokens" value={totalTokens != null ? fmtInt(totalTokens) : "-"} facts={[{ label: "Basis", value: "Input + output" }]} />
      </MetricGrid>

      <Card title="Requests by endpoint">
        {byEndpoint ? <RankedBars items={byEndpoint} n={maxCat} valueFmt={(v) => fmtInt(v)} unitLabel="requests" /> : <ChartNote state={byEndpointAgg} label="compute_ai_gateway_usage" />}
      </Card>
    </div>
  );
}
AreaContent.register("mlai", "gateway", MlaiGatewayContent);
