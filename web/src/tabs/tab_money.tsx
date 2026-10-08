// the one executive overview component, used as both the CFO
// home ("Money") and the CTO home ("Overview", App.tsx's own role-based swap -- the CTO still gets
// every area's own summary from the sidebar as before, only its OWN landing page is this one).
// Spend, month-by-month bars, spend by environment, top cost drivers, possible waste, cost-center
// coverage, unnamed workspaces -- the same numbers for both roles, `showPerformance` (CTO only)
// adds a Performance section on top. Reuses Cost's own query ids and this app's shared chart/
// format helpers; no checks table, no sub-tabs -- a reader gets the numbers, not the audit mechanics.

import React from "react";
import { GrainLines, GrainSwitch, MONTH_ROWS_MAX } from "../components/grain";
import type { Grain } from "../components/grain";

import { estLabel, fmtChange, fmtChangeVs, fmtDayShort, fmtDuration, fmtInt, fmtMoney, fmtMoneyShort, fmtPct } from "../format";
import { Names, resolveName, useNames, warehouseInWorkspace, wsPhrase } from "../components/names";
import { FLAGGED_STATUSES, IgnoredFilterTracker, buildMonthBars, excludedNoWorkspaceNote, filledDailySeries, groupsToEntries, numOrZero, sumBy, useFindingAgg, useFindingData, useMultiFindingData } from "../components/hooks";
import { COST_MONEY_COL, EST_SPEND_CAVEAT, WASTE_IDS } from "../components/tab_registry";
import { Card, Facts, FilterReachNote, PageFooter, PageIntro, TagOriginNote } from "../components/primitives";
import { ChartNote, Donut, Kpi, KpiRow, paletteColor } from "../components/charts";
import { TopTagSwitch, allocBars, allocSplit, tagLabelMid, useTagAllocation, useTopTags } from "../components/tag_rollup";
import { RankedBars } from "../components/charts_more";
import { slowdownSentence, slowdownSplit, windowRangeLabel } from "../components/overview_tile";
import { buildWasteWorklist, wasteTotal } from "../components/waste_total";
import { MonthChart } from "./tab_cost";
import type { AggGroup, AggKey, AggregateData, AggState, Entry, Fact, Filters, FindingState, FindingSummary, Id, Meta, Row } from "../types";
import { startsText, useStartupWaits } from "./startup_waits";
import type { Segment } from "../components/charts";
import type { Nav } from "../components/nav_hash";

/** One name's spend in each environment. */
interface EnvRow {
  name: string;
  byEnv: Record<string, number>;
  total: number;
}

/** One bar of the spend waterfall: a period total, or one mover's change. */
interface WaterfallRow {
  kind: string;
  label: string;
  hint?: string | null;
  start: number;
  end: number;
  value: number;
}

// A few codes read oddly title-cased word by word (SQL/DLT are acronyms, DBSQL is a product name);
// every other code falls back to the plain title-cased reading tab_overview.tsx's own productLabel
// uses -- good enough for a CFO's chart label without duplicating that file's whole dictionary.
// INTERACTIVE is serverless notebooks; classic all-purpose clusters bill as ALL_PURPOSE.
const PRODUCT_WORD_EXCEPTIONS: Record<string, string> = { DBSQL: "Databricks SQL", SQL: "SQL", DLT: "DLT", INTERACTIVE: "Serverless notebooks", ALL_PURPOSE: "All-purpose clusters" };
export function productWords(code: unknown): string {
  if (!code) return "(unlabeled product)";
  if (PRODUCT_WORD_EXCEPTIONS[String(code)]) return PRODUCT_WORD_EXCEPTIONS[String(code)];
  return String(code).replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

// dims.dim_workspace.env (Names.workspaceRecord) -- the SAME env value the top-bar Env filter
// already groups workspaces by, never a second guess at what "prod" means on this account.
const ENV_LABEL: Record<string, string> = { prod: "Prod", dev: "Dev", test: "Test", uat: "UAT", unknown: "Unknown" };
const CLUSTER_KIND_WORD: Record<string, string> = { all_purpose: "cluster", job_cluster: "job clusters", pipeline: "pipeline" };
export function envLabel(env: string | null | undefined): string {
  return ENV_LABEL[String(env)] || String(env || "unknown").replace(/\b\w/g, (c) => c.toUpperCase());
}

function wsEnv(wsId: Id): string {
  const rec = Names.workspaceRecord(wsId);
  return (rec && rec.env) || "unknown";
}

// [name, workspace_id] aggregate groups -> one row per name with its value in each env.
function splitByEnv(groups: AggGroup[] | null, discountPct: unknown, nameOf: (k: AggKey) => string): EnvRow[] {
  const disc = numOrZero(discountPct);
  const rows = new Map<string, EnvRow>();
  (groups || []).forEach((g) => {
    const name = nameOf(g.key[0]);
    const env = wsEnv(g.key[1]);
    const v = numOrZero(g.value) * (1 - disc);
    if (!rows.has(name)) rows.set(name, { name, byEnv: {}, total: 0 });
    const r = rows.get(name)!;
    r.byEnv[env] = (r.byEnv[env] || 0) + v;
    r.total += v;
  });
  return Array.from(rows.values()).sort((a, b) => b.total - a.total);
}

function EnvSplitTable({ rows, envs, n, label, fmt }: { rows: EnvRow[]; envs: string[]; n: number; label: string; fmt: (v: number) => string }) {
  return (
    <div className="table-wrap">
    <table className="data money-env-split">
      <thead>
        <tr><th>{label}</th>{envs.map((e) => <th key={e} className="num">{envLabel(e)}</th>)}<th className="num">Total</th></tr>
      </thead>
      <tbody>
        {rows.slice(0, n).map((r) => (
          <tr key={r.name}>
            <td>{r.name}</td>
            {envs.map((e) => <td key={e} className="num">{r.byEnv[e] ? fmt(r.byEnv[e]) : "-"}</td>)}
            <td className="num">{fmt(r.total)}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
}

// Per-env totals as labelled facts, envs in the page's own spend order.
function envFacts(envs: string[], byEnv: Record<string, number>, fmt: (v: number) => string): Fact[] {
  return envs.map((e) => ({ label: envLabel(e), value: fmt(byEnv[e] || 0) }));
}

// ─────────── Where the money goes: compute, engine and resource type as donuts; top SKUs and tags ───────────
const RESOURCE_FAMILY: Record<string, string> = {
  SQL: "SQL warehouse", JOBS: "Job", INTERACTIVE: "Serverless notebook", ALL_PURPOSE: "All-purpose cluster",
  MODEL_SERVING: "Model serving", GENIE: "Genie", VECTOR_SEARCH: "Vector Search", DLT: "Pipeline (DLT)",
  APPS: "Apps", LAKEBASE: "Lakebase",
};

// Aggregate groups come back at list price; net them of the discount like the Spend total.
function netEntries(data: AggregateData, keyFmt: (k: AggKey[]) => string): Entry[] {
  const net = 1 - numOrZero(data && data.discount_pct);
  return groupsToEntries(data, keyFmt).map((e) => ({ ...e, value: e.value * net }));
}

function donutSegments(entries: Entry[]): Segment[] {
  const list = entries.filter((e) => e.value > 0).sort((a, b) => b.value - a.value);
  return list.map((e, i) => ({ label: e.name, value: e.value, display: fmtPct((e.value / list.reduce((s, x) => s + x.value, 0)) * 100, 0), color: paletteColor(i) }));
}

function MoneyShapeSection({ filters, prodAgg }: { filters: Filters; prodAgg: AggState }) {
  const splitState = useFindingData("overview_serverless_classic_split", filters.window, filters.workspaceIds, filters.envs);
  // No server-side top here -- RankedBars below already folds past n itself; a server fold too
  // would leave two "Other" bars (its own, plus RankedBars' own fold of whatever that pushed out).
  const skuAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["sku_name"], "sum", COST_MONEY_COL);
  const tagAgg = useFindingAgg("cost_chargeback_by_tag_value", filters.window, filters.workspaceIds, filters.envs, ["tag_key", "tag_value"], "sum", "usd_list");
  const split = splitState.phase === "ready" && splitState.outcome === "ok_rows" ? splitState.data.rows : null;
  const serverless = split ? sumBy(split, "serverless_net_dbus") : 0, classic = split ? sumBy(split, "classic_net_dbus") : 0;
  const photon = split ? sumBy(split, "photon_net_dbus") : 0, total = split ? sumBy(split, "total_net_dbus") : 0;
  const prodReady = prodAgg.phase === "ready" && prodAgg.outcome === "ok_rows";
  const families = new Map<string, number>();
  if (prodReady) {
    (prodAgg.data.groups || []).forEach((g) => {
      const fam = RESOURCE_FAMILY[String(g.key && g.key[0])] || "Other";
      families.set(fam, (families.get(fam) || 0) + numOrZero(g.value));
    });
  }
  const skuItems = skuAgg.phase === "ready" && skuAgg.outcome === "ok_rows" ? netEntries(skuAgg.data, (k) => String((k && k[0]) || "(no SKU)")) : null;
  // The untagged and pooled "(other)" rows are not a tag value. A dollar carries several tags, so the rest is not summed into Other.
  const tagItems = tagAgg.phase === "ready" && tagAgg.outcome === "ok_rows"
    ? netEntries({ ...tagAgg.data, groups: (tagAgg.data.groups || []).filter((g) => g.key && g.key[0] && !["(untagged)", "(other)"].includes(String(g.key[1]))) },
      (k) => `${k[0]}: ${k[1]}`).sort((a, b) => b.value - a.value).slice(0, 10) : null;
  return (
    <React.Fragment>
      <div className="money-section-title">Where the money goes</div>
      <div className="grid-3">
        <Card title="Compute" right={<span className="muted">share of usage</span>}>
          {split ? <Donut segments={donutSegments([{ name: "Classic", value: classic }, { name: "Serverless", value: serverless }])} centerLabel={fmtPct(total > 0 ? (serverless / total) * 100 : 0, 0)} centerSub="serverless" />
            : <ChartNote state={splitState} label="overview_serverless_classic_split" />}
        </Card>
        <Card title="Photon engine" right={<span className="muted">share of usage</span>}>
          {split ? <Donut segments={donutSegments([{ name: "Photon", value: photon }, { name: "Not Photon", value: Math.max(total - photon, 0) }])} centerLabel={fmtPct(total > 0 ? (photon / total) * 100 : 0, 0)} centerSub="on Photon" />
            : <ChartNote state={splitState} label="overview_serverless_classic_split" />}
        </Card>
        <Card title="Resource type">
          {prodReady ? <Donut segments={donutSegments([...families.entries()].map(([name, value]) => ({ name, value })))} centerLabel={fmtMoneyShort([...families.values()].reduce((a, b) => a + b, 0))} centerSub="list price" />
            : <ChartNote state={prodAgg} label="cost_dollarized_by_sku_day" />}
        </Card>
      </div>
      <div className="grid-2">
        <Card title="Spend by SKU" right={<span className="muted">{`top 12, ${estLabel(skuAgg.phase === "ready" ? skuAgg.data.discount_pct : 0)}`}</span>}>
          {skuItems ? <RankedBars items={skuItems} n={12} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText="No SKU spend in this window." />
            : <ChartNote state={skuAgg} label="cost_dollarized_by_sku_day" />}
        </Card>
        <Card title="Spend by tag value" right={<span className="muted">{`top 10, values overlap, ${estLabel(tagAgg.phase === "ready" && tagAgg.data ? tagAgg.data.discount_pct : 0)}`}</span>}>
          {tagItems ? <RankedBars items={tagItems} n={10} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText="No tagged spend in this window." />
            : <ChartNote state={tagAgg} label="cost_chargeback_by_tag_value" />}
        </Card>
      </div>
    </React.Fragment>
  );
}

// One workspace-grain money check (cost_dollarized_by_sku_day, already fetched as `wsAgg` for the
// "top workspaces" card) joined to each workspace's own env -- one row per workspace already, so a
// workspace can never be counted into two envs. `periodByEnv` (from the SAME cost_period_over_period
// fetch the KPI row's own change% uses) supplies each env's change vs the previous period, so this
// card can never disagree with the page's own headline number about what changed.
function buildEnvBreakdown(wsGroups: AggGroup[] | null, discountPct: unknown, periodByEnv: Record<string, { cur: number; prev: number }> | null) {
  const disc = numOrZero(discountPct);
  const byEnv = new Map<string, { env: string; value: number; workspaces: { id: AggKey; name: string; value: number }[] }>();
  (wsGroups || []).forEach((g) => {
    const wsId = g.key[0];
    const env = wsEnv(wsId);
    const usd = numOrZero(g.value) * (1 - disc);
    if (!byEnv.has(env)) byEnv.set(env, { env, value: 0, workspaces: [] });
    const bucket = byEnv.get(env)!;
    bucket.value += usd;
    if (usd > 0) bucket.workspaces.push({ id: wsId, name: moneyWsName(wsId), value: usd });
  });
  const total = Array.from(byEnv.values()).reduce((s, b) => s + b.value, 0);
  return Array.from(byEnv.values())
    .map((b) => {
      const ch = periodByEnv && periodByEnv[b.env];
      const changeUsd = ch ? ch.cur - ch.prev : null;
      const changePct = ch && ch.prev > 0 ? ((ch.cur - ch.prev) / ch.prev) * 100 : null;
      return {
        ...b,
        sharePct: total > 0 ? (b.value / total) * 100 : null,
        wsCount: b.workspaces.filter((w) => !isAccountLevel(w.id)).length,
        topWorkspaces: [...b.workspaces].sort((a, c) => c.value - a.value).slice(0, 3),
        // changeUsd/changePct stay for the amber-tone threshold; cur/prev are the raw pair fmtChange needs.
        changeUsd, changePct, cur: ch ? ch.cur : null, prev: ch ? ch.prev : null,
      };
    })
    .sort((a, b) => b.value - a.value);
}

// cost_period_over_period's own workspace-grain rows (the KPI row's own change% source), summed
// per env with the SAME "drop NOT_ASSESSED rows" rule that change% already applies -- a row this
// build could not judge never pulls an env's change toward zero.
function periodChangeByEnv(rows: Row[] | null, discountPct: unknown): Record<string, { cur: number; prev: number }> {
  const disc = numOrZero(discountPct);
  const byEnv = new Map<string, { cur: number; prev: number }>();
  (rows || []).filter((r) => r.status !== "NOT_ASSESSED").forEach((r) => {
    const env = wsEnv(r.workspace_id);
    if (!byEnv.has(env)) byEnv.set(env, { cur: 0, prev: 0 });
    const b = byEnv.get(env)!;
    b.cur += numOrZero(r.est_current_usd_list) * (1 - disc);
    b.prev += numOrZero(r.est_previous_usd_list) * (1 - disc);
  });
  return Object.fromEntries(byEnv);
}

// EnvBreakdown: bars (one per env) + a small table with $, share, change vs the previous period --
// a row expands (click) to the top 3 workspaces inside that env, so "where does the money go" has
// an answer one click deeper without a whole extra page.
type EnvItem = ReturnType<typeof buildEnvBreakdown>[number];

function EnvBreakdown({ items, state }: { items: EnvItem[] | null; state: AggState }) {
  const [openEnv, setOpenEnv] = React.useState<string | null>(null);
  if (!items) return <ChartNote state={state} label="cost_dollarized_by_sku_day" />;
  if (!items.length) return <div className="chart-note ok"><div className="chart-note-text">No spend by environment in this window.</div></div>;
  const barItems = items.map((e) => ({ name: envLabel(e.env), value: e.value }));
  return (
    <React.Fragment>
      <RankedBars items={barItems} n={items.length} categorical valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" ariaLabel="Spend by environment" />
      <div className="table-wrap">
      <table className="data money-env-table">
        <thead>
          <tr><th>Environment</th><th className="num">Spend</th><th className="num">Share</th><th className="num">vs prior period</th><th></th></tr>
        </thead>
        <tbody>
          {items.map((e) => (
            <React.Fragment key={e.env}>
              <tr
                className="money-env-row"
                tabIndex={0}
                role="button"
                aria-expanded={openEnv === e.env}
                onClick={() => setOpenEnv(openEnv === e.env ? null : e.env)}
                onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); setOpenEnv(openEnv === e.env ? null : e.env); } }}
              >
                <td>{envLabel(e.env)} <span className="muted">({fmtInt(e.wsCount)} ws)</span></td>
                <td className="num mono">{fmtMoney(e.value, 0)}</td>
                <td className="num mono">{e.sharePct != null ? fmtPct(e.sharePct, 0) : "-"}</td>
                <td className={`num mono${e.changePct != null && e.changePct >= 15 ? " tone-amber" : ""}`}>
                  {e.cur != null && e.prev != null ? fmtChange(e.cur, e.prev) : "-"}
                </td>
                <td className="money-env-caret">{openEnv === e.env ? "▾" : "▸"}</td>
              </tr>
              {openEnv === e.env && (
                <tr className="money-env-detail">
                  <td colSpan={5}>
                    {e.topWorkspaces.length
                      ? `Top: ${e.topWorkspaces.map((w) => `${w.name} (${fmtMoney(w.value, 0)})`).join(", ")}`
                      : "No workspace breakdown for this environment."}
                  </td>
                </tr>
              )}
            </React.Fragment>
          ))}
        </tbody>
      </table>
      </div>
    </React.Fragment>
  );
}

// ─────────── What moved spend: cost_period_over_period grouped by workspace into a waterfall from
// the previous period to this one -- the SAME judged rows (status != NOT_ASSESSED) and discount the
// page's own "vs previous period" KPI already applies, so the card and the KPI can never disagree.
// Called from both this page and tab_overview.tsx. ───────────
function isAccountLevel(wsId: Id): boolean {
  return wsId == null || String(wsId) === "0";
}

function moneyWsName(wsId: Id): string {
  if (isAccountLevel(wsId)) return "Account-level (no workspace)";
  return resolveName("workspace", wsId, wsId) || `workspace ${wsId}`;
}

function buildSpendMovers(rows: Row[] | null, discountPct: unknown) {
  const judged = (rows || []).filter((r) => r.status !== "NOT_ASSESSED");
  if (!judged.length) return null;
  const disc = numOrZero(discountPct);
  const byWs = new Map<string, { id: Id; cur: number; prev: number }>();
  judged.forEach((r) => {
    const key = r.workspace_id == null || String(r.workspace_id) === "0" ? "__account__" : String(r.workspace_id);
    if (!byWs.has(key)) byWs.set(key, { id: r.workspace_id, cur: 0, prev: 0 });
    const g = byWs.get(key)!;
    g.cur += numOrZero(r.est_current_usd_list) * (1 - disc);
    g.prev += numOrZero(r.est_previous_usd_list) * (1 - disc);
  });
  const groups = Array.from(byWs.values()).map((g) => ({ ...g, delta: g.cur - g.prev }));
  const totalPrev = groups.reduce((s, g) => s + g.prev, 0);
  const totalCur = groups.reduce((s, g) => s + g.cur, 0);
  const totalDelta = totalCur - totalPrev;
  const ranked = [...groups].sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
  const top = ranked.slice(0, 5);
  // The remainder, not a second sum -- "N others" always closes the exact gap to `current`,
  // whatever rounding the per-group sums above happened to pick up.
  const othersDelta = totalDelta - top.reduce((s, g) => s + g.delta, 0);
  return { totalPrev, totalCur, totalDelta, top, othersDelta, othersCount: ranked.length - top.length };
}

// Floating-bar rows: each delta bar starts where the running total left off; the two totals are
// full bars from zero.
function spendMoverRows(movers: NonNullable<ReturnType<typeof buildSpendMovers>>, windowDays: number): WaterfallRow[] {
  const rows: WaterfallRow[] = [{ kind: "total", label: `Previous ${windowDays} days`, start: 0, end: movers.totalPrev, value: movers.totalPrev }];
  let running = movers.totalPrev;
  movers.top.forEach((g) => {
    const start = running;
    running += g.delta;
    rows.push({
      kind: g.delta >= 0 ? "up" : "down",
      label: moneyWsName(g.id) + (g.prev < 1 && g.cur >= 1 ? " · new" : ""),
      hint: isAccountLevel(g.id) ? "Billed to the account, not to any workspace" : null,
      start, end: running, value: g.delta,
    });
  });
  if (movers.othersCount > 0) {
    const start = running;
    running += movers.othersDelta;
    rows.push({ kind: movers.othersDelta >= 0 ? "up" : "down", label: `${fmtInt(movers.othersCount)} others`, start, end: running, value: movers.othersDelta });
  }
  rows.push({ kind: "total", label: `This ${windowDays} days`, start: 0, end: movers.totalCur, value: movers.totalCur });
  return rows;
}

function MoneyWaterfall({ rows }: { rows: WaterfallRow[] }) {
  const domainMin = Math.min(0, ...rows.map((r) => Math.min(r.start, r.end)));
  const domainMax = Math.max(0, ...rows.map((r) => Math.max(r.start, r.end)));
  const span = domainMax - domainMin || 1;
  const pct = (v: number) => ((v - domainMin) / span) * 100;
  return (
    <div className="money-waterfall">
      {rows.map((r, i) => {
        const left = pct(Math.min(r.start, r.end));
        const w = Math.max(pct(Math.max(r.start, r.end)) - left, 0);
        const sign = r.kind !== "total" && r.value >= 0 ? "+" : "";
        return (
          <div className={`money-waterfall-row${r.kind === "total" ? " is-total" : ""}`} key={i}>
            <div className="money-waterfall-label" title={r.hint || r.label}>{r.label}</div>
            <div className="money-waterfall-track">
              <div className={`money-waterfall-bar tone-${r.kind}`} style={{ left: `${left}%`, width: `${w}%` }} />
            </div>
            <div className="money-waterfall-value mono">{`${sign}${fmtMoney(r.value, 0)}`}</div>
          </div>
        );
      })}
    </div>
  );
}

// Card for both the Money/CTO page (right after the KPI row) and the FinOps overview (near its own
// top) -- one component, called from both files.
export function SpendMoversCard({ periodState, spend }: { periodState: FindingState; spend: number | null }) {
  const ready = periodState.phase === "ready" && periodState.outcome === "ok_rows";
  if (!ready) return <Card className="money-movers-card" title="What moved spend"><ChartNote state={periodState} label="cost_period_over_period" /></Card>;
  const movers = buildSpendMovers(periodState.data.rows, periodState.data.discount_pct);
  if (!movers) {
    const noPrev = (periodState.data.rows || []).some((r) => r.not_assessed_reason === "previous_window_not_covered");
    const note = noPrev ? "No previous period to compare in this window." : "Spend in one of the two periods has no list price, so the change can't be worked out.";
    return <Card className="money-movers-card" title="What moved spend"><div className="chart-note-oneline">{note}</div></Card>;
  }
  // The server's own window, so this page and the Overview label the same rows the same way.
  const rows = spendMoverRows(movers, periodState.data.window_days);
  const topDelta = movers.top.reduce((s, g) => s + g.delta, 0);
  // Only a real share: some workspaces left out, the top ones moving the same way as the total,
  // and at least $1 of net change.
  const share = movers.othersCount > 0 && Math.abs(movers.totalDelta) >= 1 ? topDelta / movers.totalDelta : null;
  const hasAccount = movers.top.some((g) => isAccountLevel(g.id));
  const wsTop = movers.top.length - (hasAccount ? 1 : 0);
  const who = hasAccount
    ? `Account-level usage${wsTop ? ` and ${fmtInt(wsTop)} workspace${wsTop === 1 ? "" : "s"}` : ""} explain${wsTop ? "" : "s"}`
    : `${fmtInt(wsTop)} workspace${wsTop === 1 ? "" : "s"} explain${wsTop === 1 ? "s" : ""}`;
  const sentence = share != null && share > 0 && share <= 1
    ? `${who} ${fmtPct(share * 100, 0)} of the ${fmtMoneyShort(Math.abs(movers.totalDelta))} ${movers.totalDelta >= 0 ? "increase" : "decrease"}.`
    : null;
  // Rows with unpriced usage on either side are left out of the comparison, never out of the spend.
  const notCompared = spend != null && spend - movers.totalCur > 1
    ? `${fmtMoney(spend - movers.totalCur, 0)} of this period's ${fmtMoney(spend, 0)} is not in this chart: part of its usage has no list price.`
    : null;
  return (
    <Card className="money-movers-card" title="What moved spend">
      {(sentence || notCompared) && <div className="money-mover-note">{[sentence, notCompared].filter(Boolean).join(" ")}</div>}
      <MoneyWaterfall rows={rows} />
    </Card>
  );
}

// UnusedTablesCard: only when this build actually carries the check (findingsById has the row at
// all) -- never a box for a check this account's version of the app does not run yet. Counts and
// sums only the flagged (WARN/CRITICAL, non-pooled) rows -- storage_unused_table_cost's own OK and
// unsized rows carry no dollars worth surfacing here, and the pooled is_other row is always OK.
function UnusedTablesCard({ finding, state, wasteUsd }: { finding: FindingSummary | null | undefined; state: FindingState; wasteUsd: number }) {
  if (!finding || state.phase !== "ready") return null;
  if (state.outcome !== "ok_rows") {
    return <Card title="Unused tables"><ChartNote state={state} label="storage_unused_table_cost" /></Card>;
  }
  const rows = state.data.rows || [];
  if (!rows.length) return null;
  const flagged = rows.filter((r) => (r.status === "WARN" || r.status === "CRITICAL") && !r.is_other);
  // No size on record means no $: these are unused, but their cost is unknown, never $0.
  const unsized = rows.filter((r) => r.price_basis === "no_size" && !r.is_other).length;
  const monthlyUsd = sumBy(flagged, "est_total_usd_month") * (1 - numOrZero(state.data.discount_pct));
  const unsizedText = `${fmtInt(unsized)} ${flagged.length ? "more " : ""}unused table${unsized === 1 ? " has" : "s have"} no size on record, so no price.`;
  const outside = wasteUsd > 0 ? `not in the ${fmtMoney(wasteUsd, 0)} possible waste` : "not counted as possible waste";
  return (
    <Card title="Unused tables">
      <div className="money-perf-line">
        {flagged.length
          ? `${fmtInt(flagged.length)} unused table${flagged.length === 1 ? "" : "s"} cost about ${fmtMoney(monthlyUsd, 0)} a month to keep (storage and upkeep; ${outside}).${unsized ? ` ${unsizedText}` : ""}`
          : unsized
          ? unsizedText
          : "0 unused tables cost money."}
      </div>
    </Card>
  );
}

export function MoneyTab({ filters, meta, findingsById, goTo, showPerformance, onFullView }: {
  filters: Filters; meta: Meta | null; findingsById: Record<string, FindingSummary> | null; goTo?: (target: Partial<Nav>) => void;
  role?: string; showPerformance?: boolean; onFullView?: () => void;
}) {
  useNames(); // re-render once workspace names (names.tsx) are ready
  // Money is not inside AreaPage, so it resets the ignored-filter note itself.
  React.useEffect(() => { IgnoredFilterTracker.reset(); }, []);

  const dailyAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["usage_date"], "sum", COST_MONEY_COL);
  const periodState = useFindingData("cost_period_over_period", filters.window, filters.workspaceIds, filters.envs);
  const monthAgg = useFindingAgg("cost_monthly_actuals", filters.window, filters.workspaceIds, filters.envs, ["month_start", "is_partial_month"], "sum", "net_list_cost_usd");
  const prodAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["billing_origin_product"], "sum", COST_MONEY_COL);
  const wsAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["workspace_id"], "sum", COST_MONEY_COL);
  const unnamedState = useFindingData("cost_unnamed_workspaces", filters.window, filters.workspaceIds, filters.envs);
  const multiWaste = useMultiFindingData(WASTE_IDS, filters.window, filters.workspaceIds, filters.envs, 5000, FLAGGED_STATUSES);
  // The headline and "No <tag>" follow the first top tag; the "Spend by" card can switch tags. Both
  // read GET /api/rollup, the same one-dollar basis Cost > Allocation uses.
  const topTags = useTopTags(meta, filters.window);
  const [chosenTag, setChosenTag] = React.useState<string | null>(null);
  const primaryTag = topTags[0] || null;
  const shownTag = topTags.find((t) => t.key === chosenTag) || primaryTag;
  const ccView = useTagAllocation(filters, primaryTag && primaryTag.key);
  const otherView = useTagAllocation(filters, shownTag && primaryTag && shownTag.key !== primaryTag.key ? shownTag.key : null);
  const cardView = shownTag && primaryTag && shownTag.key !== primaryTag.key ? otherView : ccView;

  const worklist = React.useMemo(() => buildWasteWorklist(findingsById || {}, multiWaste), [findingsById, multiWaste]);
  const waste = wasteTotal(worklist);

  const dailyReady = dailyAgg.phase === "ready" && dailyAgg.outcome === "ok_rows";
  const discountPct = dailyReady ? numOrZero(dailyAgg.data.discount_pct) : 0;
  const spend = dailyReady ? numOrZero(dailyAgg.data.total_value) * (1 - discountPct) : null;

  const periodReady = periodState.phase === "ready" && periodState.outcome === "ok_rows";
  // Loaded but not measured (the check is not in this export): say so instead of waiting forever.
  const notMeasured = (st: { phase: string; outcome: string | null }) => st.phase === "ready" && !String(st.outcome).startsWith("ok_");
  const spendMissing = notMeasured(dailyAgg);
  let periodCur: number | null = null, periodPrev: number | null = null;
  if (periodReady) {
    const judged = periodState.data.rows.filter((r) => r.status !== "NOT_ASSESSED");
    const disc = numOrZero(periodState.data.discount_pct);
    periodCur = sumBy(judged, "est_current_usd_list") * (1 - disc);
    periodPrev = sumBy(judged, "est_previous_usd_list") * (1 - disc);
  }
  // The one "vs previous period" reading (fmtChangeVs, format.tsx) every text on this page uses;
  // null when there is nothing to compare.
  const vsPrevPeriod = periodReady ? fmtChangeVs(periodCur, periodPrev, "the previous period") : null;
  const vsDaysBefore = periodReady ? fmtChangeVs(periodCur, periodPrev, `the ${filters.window} days before`) : null;
  // Amber at +15%, or when spend grew past $50 from under it (a multiple or "new").
  const periodRising = periodReady && periodCur != null && periodPrev != null && periodCur > periodPrev
    && (periodPrev < 50 ? periodCur >= 50 : (periodCur - periodPrev) / periodPrev >= 0.15);
  // Some usage stayed unpriced on at least one side (cost_period_over_period withholds a verdict
  // there), so the judged total the change above is measured on can fall short of the headline
  // spend -- said explicitly rather than left for the two dollar figures to silently disagree.
  const notComparedUsd = dailyReady && periodReady && periodCur != null && spend != null && Math.abs(periodCur - spend) > 1 ? spend - periodCur : null;
  const periodComparedOn = notComparedUsd != null
    ? `, on the ${fmtMoney(periodCur, 0)} priced in both periods; ${fmtMoney(notComparedUsd, 0)} not compared (no list price for part of it)` : "";

  const monthReady = monthAgg.phase === "ready" && monthAgg.outcome === "ok_rows";
  // buildMonthBars/CostMonthBars (tab_cost.tsx, loaded first): the SAME colouring function Cost's
  // own "By calendar month" card uses, over the full history before slicing to 18 -- so a month
  // can never read red here and steady there.
  const monthsForBars = monthReady ? buildMonthBars(monthAgg.data.groups, monthAgg.data.discount_pct, MONTH_ROWS_MAX) : null;

  const prodReady = prodAgg.phase === "ready" && prodAgg.outcome === "ok_rows";
  const prodItems = prodReady ? netEntries(prodAgg.data, (k) => productWords(k && k[0])) : null;

  const wsReady = wsAgg.phase === "ready" && wsAgg.outcome === "ok_rows";
  const multiEnvWs = wsReady && new Set((wsAgg.data.groups || []).filter((g) => numOrZero(g.value) > 0).map((g) => wsEnv(g.key[0]))).size > 1;
  const wsItems = wsReady
    ? (wsAgg.data.groups || []).map((g) => ({
        name: g.key[0] == null
          ? "Account-level (no workspace)"
          : resolveName("workspace", g.key[0], g.key[0]) + (multiEnvWs ? ` · ${envLabel(wsEnv(g.key[0]))}` : ""),
        id: g.key[0], value: numOrZero(g.value) * (1 - numOrZero(wsAgg.data.discount_pct)),
      }))
    : null;

  // Spend by environment: the SAME workspace-grain aggregate as `wsItems` above, joined to each
  // workspace's own env, plus cost_period_over_period (already fetched for the KPI row's own
  // change%) summed the same way -- never a third $ total this page has to reconcile.
  const envPeriodByEnv = periodReady ? periodChangeByEnv(periodState.data.rows, periodState.data.discount_pct) : null;
  const envItems = wsReady ? buildEnvBreakdown(wsAgg.data.groups, wsAgg.data.discount_pct, envPeriodByEnv) : null;

  // More than one environment in view: split the cards below by environment.
  const envOrder = envItems ? envItems.filter((e) => e.value > 0).map((e) => e.env) : [];
  const multiEnv = envOrder.length > 1;
  const prodEnvAgg = useFindingAgg(multiEnv ? "cost_dollarized_by_sku_day" : null, filters.window, filters.workspaceIds, filters.envs, ["billing_origin_product", "workspace_id"], "sum", COST_MONEY_COL);
  const prodEnvRows = multiEnv && prodEnvAgg.phase === "ready" && prodEnvAgg.outcome === "ok_rows"
    ? splitByEnv(prodEnvAgg.data.groups, prodEnvAgg.data.discount_pct, productWords) : null;

  const unusedFinding = findingsById && findingsById.storage_unused_table_cost;
  const unusedState = useFindingData(unusedFinding ? "storage_unused_table_cost" : null, filters.window, filters.workspaceIds, filters.envs);

  // CTO-only Performance panels -- queryId is null (no fetch) for CFO, same pattern jobsWorstState
  // (tab_overview.tsx) already uses for a hook that only sometimes needs to run.
  // Aggregated server-side (useFindingAgg), not a raw useFindingData row page -- a raw page caps at
  // 5,000 rows, which a large account can exceed at 90d and silently undercount the total.
  const failedQAgg = useFindingAgg(showPerformance ? "query_failed_queries_daily" : null, filters.window, filters.workspaceIds, filters.envs, ["day"], "sum", "query_count");
  const failedQEnvAgg = useFindingAgg(showPerformance && multiEnv ? "query_failed_queries_daily" : null, filters.window, filters.workspaceIds, filters.envs, ["day", "workspace_id"], "sum", "query_count");
  const queueCapAgg = useFindingAgg(showPerformance ? "query_queuing_waits" : null, filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "warehouse_id"], "sum", "waiting_at_capacity_ms_sum");
  const queueComputeAgg = useFindingAgg(showPerformance ? "query_queuing_waits" : null, filters.window, filters.workspaceIds, filters.envs, ["workspace_id", "warehouse_id"], "sum", "waiting_for_compute_ms_sum");
  const startup = useStartupWaits(filters.window, filters.workspaceIds, filters.envs, showPerformance);
  const jobRelState = useFindingData(showPerformance ? "lakeflow_job_reliability" : null, filters.window, filters.workspaceIds, filters.envs);
  const durRegState = useFindingData(showPerformance ? "lakeflow_job_duration_regression" : null, filters.window, filters.workspaceIds, filters.envs);
  // CTO only: the costliest warehouses and clusters; job clusters come rolled up per job.
  const whCostState = useFindingData(showPerformance ? "cost_chargeback_by_warehouse" : null, filters.window, filters.workspaceIds, filters.envs);
  const clCostState = useFindingData(showPerformance ? "cost_chargeback_by_cluster" : null, filters.window, filters.workspaceIds, filters.envs);

  const unnamedReady = unnamedState.phase === "ready" && unnamedState.outcome === "ok_rows";
  const unnamedRows = unnamedReady ? unnamedState.data.rows : [];
  // The same 365-day column, over the same flagged (CRITICAL/WARN) rows only, the checks table's
  // own Money cell sums (MoneyCell, findings_table.tsx) -- summing every row here (including an OK
  // one the checks table never counts) silently disagreed with that same number.
  const unnamedFlagged = unnamedReady ? unnamedRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : [];
  const unnamedSpend = unnamedReady ? sumBy(unnamedFlagged, "net_list_cost_usd_365d") : null;
  const unnamedBilling = unnamedReady ? unnamedRows.filter((r) => numOrZero(r.net_list_cost_usd) > 0) : [];
  const unnamedActive = unnamedBilling.length;
  const unnamedWindowUsd = sumBy(unnamedBilling, "net_list_cost_usd") * (1 - discountPct);
  // Not a "-" / "Not assessed" KPI box -- this account's version of the app either has this check
  // or it does not; when it does not, the KPI is left out of the row entirely (should 9).
  const unnamedNotAssessed = unnamedState.phase === "error" || unnamedState.outcome === "not_assessed";
  const unnamedSub = unnamedState.phase === "loading" ? "Loading..."
    : unnamedActive ? `${fmtInt(unnamedActive)} of ${fmtInt(unnamedRows.length)} still billing: ${fmtMoney(unnamedWindowUsd, 0)} in the last ${filters.window} days; ${fmtMoney(unnamedSpend, 0)} is the last 365 days`
    : unnamedRows.length ? `${fmtInt(unnamedRows.length)} found, $0 in the last ${filters.window} days; ${fmtMoney(unnamedSpend, 0)} is the last 365 days`
    : "None found";

  const allocReady = ccView.phase === "ready" && ccView.outcome === "ok_rows";
  const ccSplit = allocSplit(ccView);
  const ccTotal = ccSplit.total, ccUntagged = ccSplit.untagged;
  const ccHasValues = allocReady && ccTotal > 0;
  const ccUntaggedPct = ccHasValues ? (ccUntagged / ccTotal) * 100 : null;
  const ccLabel = tagLabelMid(primaryTag && primaryTag.label);
  const cardReady = cardView.phase === "ready" && cardView.outcome === "ok_rows";
  const cardSplit = allocSplit(cardView);
  const cardLabel = tagLabelMid(shownTag && shownTag.label);

  const rangeLabel = windowRangeLabel(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);

  const verdict = dailyReady
    ? `${fmtMoney(spend, 0)} spent ${rangeLabel || `in the last ${filters.window} days`}`
      + (vsDaysBefore
        ? (notComparedUsd != null
          ? `, ${vsDaysBefore} on the ${fmtMoney(periodCur, 0)} priced in both periods; the other ${fmtMoney(notComparedUsd, 0)} has no list price for part of its usage, so it is not compared`
          : `, ${vsDaysBefore}`)
        : "") + "."
      + (waste.usd > 0 ? ` Possible waste: ${fmtMoney(waste.usd, 0)}${spend != null && spend > 0 ? ` (${fmtPct((waste.usd / spend) * 100, 0)} of spend)` : ""}.` : "")
      + (ccHasValues ? (ccUntagged >= ccTotal - 0.5 ? ` No spend carries a ${ccLabel} tag yet, so none can be charged back.` : ` ${fmtMoney(ccUntagged, 0)} of ${fmtMoney(ccTotal, 0)} spend has no ${ccLabel} tag, so it can't be charged back.`)
        + (ccUntagged > 0.5 ? " To decide: make the tag mandatory." : "") : "")
    : spendMissing
      ? `Spend is not in this export yet.${waste.usd > 0 ? ` Possible waste: ${fmtMoney(waste.usd, 0)}.` : ""}`
      : "Loading this window's spend...";
  const excludedNote = dailyReady ? excludedNoWorkspaceNote(dailyAgg.data) : null;

  const jump = (target: Partial<Nav>) => { if (goTo) goTo(target); };

  // ─────────── CTO-only Performance panels (showPerformance) ───────────
  const perfRange = rangeLabel || `over the last ${filters.window} days`;
  const slowSplit = slowdownSplit(meta, filters.window);

  // query_failed_queries_daily already filters to FAILED/CANCELED rows at the SQL layer -- the
  // aggregate's own total_value and per-day groups are exact, never capped at a row limit.
  const failedQReady = showPerformance && failedQAgg.phase === "ready" && failedQAgg.outcome === "ok_rows";
  const failedQByDay = failedQReady
    ? (() => {
        const sorted = [...(failedQAgg.data.groups || [])].sort((a, b) => String(a.key[0]).localeCompare(String(b.key[0])));
        return filledDailySeries({ days: sorted.map((g) => String(g.key[0]).slice(0, 10)), values: sorted.map((g) => numOrZero(g.value)) }, meta, filters.window);
      })()
    : null;
  const failedQTotal = failedQReady ? numOrZero(failedQAgg.data.total_value) : null;
  const failedQEnvReady = failedQByDay && multiEnv && failedQEnvAgg.phase === "ready" && failedQEnvAgg.outcome === "ok_rows";
  const failedQByEnv: Record<string, number> = {};
  const failedQSeries = failedQEnvReady
    ? envOrder.map((env, i) => {
        const perDay = new Map();
        (failedQEnvAgg.data.groups || []).forEach((g) => {
          if (wsEnv(g.key[1]) !== env) return;
          const day = String(g.key[0]).slice(0, 10);
          perDay.set(day, (perDay.get(day) || 0) + numOrZero(g.value));
          failedQByEnv[env] = (failedQByEnv[env] || 0) + numOrZero(g.value);
        });
        return { label: envLabel(env), color: `var(--c${(i % 5) + 1})`, data: failedQByDay.days.map((d) => perDay.get(d) || 0) };
      })
    : null;
  const [failGrain, setFailGrain] = React.useState<Grain>("day");
  const failedQWorstIdx = failedQByDay && failedQByDay.values.length ? failedQByDay.values.indexOf(Math.max(...failedQByDay.values)) : -1;

  // Two aggregates (one per wait bucket), each grouped by warehouse -- SUM is associative, so
  // summing across every matching row this way gives the exact total and the exact per-warehouse
  // ranking, never the raw-row 5,000 cap a large account could exceed at 90d.
  const queueReady = showPerformance && queueCapAgg.phase === "ready" && queueCapAgg.outcome === "ok_rows"
    && queueComputeAgg.phase === "ready" && queueComputeAgg.outcome === "ok_rows";
  // Two different waits: for a slot (the warehouse was full -- the real queue) and for start-up
  // (the warehouse was stopped or still provisioning). Kept apart, slot first.
  const slotS = queueReady ? numOrZero(queueCapAgg.data.total_value) / 1000 : null;
  const startS = queueReady ? numOrZero(queueComputeAgg.data.total_value) / 1000 : null;
  const waitsByWh = new Map<string, { slot: number; start: number }>();
  if (queueReady) {
    ([["slot", queueCapAgg.data], ["start", queueComputeAgg.data]] as ["slot" | "start", AggregateData][]).forEach(([kind, data]) => {
      (data.groups || []).forEach((g) => {
        const key = `${g.key[0]}:${g.key[1]}`;
        const w = waitsByWh.get(key) || { slot: 0, start: 0 };
        w[kind] += numOrZero(g.value) / 1000;
        waitsByWh.set(key, w);
      });
    });
  }
  const slotByEnv: Record<string, number> = {}, startByEnv: Record<string, number> = {};
  waitsByWh.forEach((w, key) => {
    const env = wsEnv(key.split(":")[0]);
    slotByEnv[env] = (slotByEnv[env] || 0) + w.slot;
    startByEnv[env] = (startByEnv[env] || 0) + w.start;
  });
  const topWaits = (kind: "slot" | "start") => (queueReady ? Array.from(waitsByWh.entries())
    .filter(([, w]) => w[kind] > 0)
    .map(([key, w]) => {
      const [ws, wh] = key.split(":");
      return { name: warehouseInWorkspace(ws, wh), id: wh, value: w[kind] };
    })
    .sort((a, b) => b.value - a.value).slice(0, 5) : null);
  const slotTopWh = topWaits("slot");
  const startTopWh = topWaits("start");

  const jobRelReady = showPerformance && jobRelState.phase === "ready" && jobRelState.outcome === "ok_rows";
  const jobRelRows = jobRelReady ? jobRelState.data.rows : null;
  const jobRelCritical = (jobRelRows || []).filter((r) => r.status === "CRITICAL");
  const jobRelWarn = (jobRelRows || []).filter((r) => r.status === "WARN");
  const jobRelFlagged = jobRelRows ? jobRelRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  // Ranked by failed runs (not failure_rate_pct) -- several jobs tied at 100% failure rate used to
  // read as an identical, useless top 5; failed-run counts always break that tie meaningfully.
  const topFailingJobs = jobRelFlagged ? [...jobRelFlagged]
    .sort((a, b) => numOrZero(b.failed_runs) - numOrZero(a.failed_runs))
    .slice(0, 5).map((r) => ({ name: `${r.job_name || `job ${r.job_id}`} (${wsPhrase(r.workspace_id)})`, value: numOrZero(r.failed_runs) })) : null;
  const countByEnv = (rows: Row[] | null) => (rows || []).reduce<Record<string, number>>((m, r) => { const e = wsEnv(r.workspace_id); m[e] = (m[e] || 0) + 1; return m; }, {});
  const jobRelByEnv = countByEnv(jobRelFlagged);
  const failedRunsWaste = worklist.find((w) => w.id === "lakeflow_failed_jobs_wasted_dbus");
  const failedRunsUsd = failedRunsWaste && failedRunsWaste.isFlagged ? failedRunsWaste.dollarUsd : null;

  // lakeflow_job_duration_regression: each job vs its OWN earlier baseline in the same window --
  // replaces "Warehouse idle $" (should 6), whose dollars were already counted inside the "Possible
  // waste" KPI above and which had no query/job duration panel at all.
  const durRegReady = showPerformance && durRegState.phase === "ready" && durRegState.outcome === "ok_rows";
  const durRegRows = durRegReady ? durRegState.data.rows : null;
  const durRegFlagged = durRegRows ? durRegRows.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const durRegTop = durRegFlagged ? [...durRegFlagged]
    .sort((a, b) => numOrZero(b.slowdown_ratio) - numOrZero(a.slowdown_ratio))
    .slice(0, 5).map((r) => ({ name: `${r.job_name || `job ${r.job_id}`} (${wsPhrase(r.workspace_id)})`, value: numOrZero(r.slowdown_ratio) })) : null;

  const durRegByEnv = countByEnv(durRegFlagged);

  // A check that did not run is not "no spend": only a clean empty result counts as zero rows.
  const rowsOnceReady = (st: FindingState): Row[] | null => (st.phase === "ready" && String(st.outcome).startsWith("ok_") ? (st.outcome === "ok_rows" ? st.data.rows : []) : null);
  const whCostRows = showPerformance ? rowsOnceReady(whCostState) : null;
  const clCostRows = showPerformance ? rowsOnceReady(clCostState) : null;
  const netOf = (st: FindingState) => 1 - numOrZero(st.data && st.data.discount_pct);
  const computeItems = whCostRows && clCostRows ? [
    ...whCostRows.map((r) => ({
      pooled: !!r.is_other,
      name: `${resolveName("warehouse", r.workspace_id, r.warehouse_id) || r.warehouse_id} · warehouse`,
      value: numOrZero(r.usd_list) * netOf(whCostState),
    })),
    ...clCostRows.map((r) => ({
      pooled: !!r.is_other,
      name: `${r.name || r.entity_id} · ${CLUSTER_KIND_WORD[r.cluster_kind] || "cluster"}`,
      value: numOrZero(r.est_current_usd_list) * netOf(clCostState),
    })),
  ].filter((it) => it.value > 0) : null;
  const computeTotal = computeItems ? computeItems.reduce((s, it) => s + it.value, 0) : null;

  const pageTitle = "Overview";

  return (
    <div className="money-page">
      <PageIntro
        overline={`${pageTitle.toUpperCase()} · last ${filters.window} days`}
        title={pageTitle}
        scope={`${estLabel(discountPct)} · ${EST_SPEND_CAVEAT}`}
        verdict={verdict}
        action={(
          <React.Fragment>
            {onFullView && <button type="button" className="copy-link-btn" onClick={onFullView}>Full view</button>}
          </React.Fragment>
        )}
      />
      <FilterReachNote />
      <TagOriginNote filters={filters} />

      <KpiRow>
        <Kpi
          label="Spend"
          value={dailyReady ? fmtMoney(spend, 0) : "-"}
          sub={notMeasured(periodState) ? "Not in this export yet" : !periodReady ? "Loading..." : vsPrevPeriod ? `${vsPrevPeriod}${periodComparedOn}` : "No previous period to compare"}
          tone={periodRising ? "amber" : "default"}
        />
        <Kpi
          label="Possible waste"
          value={multiWaste.phase !== "ready" ? "..." : dailyReady || waste.pricedCount ? fmtMoney(waste.usd, 0) : "-"}
          sub={multiWaste.phase !== "ready" ? "Loading..." : waste.pricedCount
            ? `from ${fmtInt(waste.pricedCount)} priced check${waste.pricedCount === 1 ? "" : "s"}`
            : waste.flaggedCount ? `${fmtInt(waste.flaggedCount)} flagged, none priced yet` : "Nothing flagged"}
          tone={multiWaste.phase !== "ready" ? "default" : waste.usd > 0 ? "amber" : "sage"}
        />
        <Kpi
          label={`No ${ccLabel}`}
          // One decimal near the ends, so 99.9% never reads as "100%" while some spend is tagged.
          value={ccUntaggedPct != null ? (ccUntaggedPct > 99.5 && ccUntaggedPct < 100 ? fmtPct(Math.min(99.9, Math.round(ccUntaggedPct * 10) / 10), 1) : fmtPct(ccUntaggedPct, 0)) : "-"}
          sub={ccHasValues ? `${fmtMoney(ccUntagged, 0)} of ${fmtMoney(ccTotal, 0)}`
            : ccView.phase === "idle" ? "No top tags in config/tag_aliases.yml"
            : ccView.phase !== "ready" ? "Loading..."
            : ccView.outcome === "not_assessed" ? "Not in this export"
            : ccView.outcome === "error" ? "Could not load"
            : `No ${ccLabel} tags found`}
          tone={ccUntaggedPct != null && ccUntaggedPct >= 50 ? "coral" : ccUntaggedPct != null && ccUntaggedPct >= 20 ? "amber" : "default"}
        />
        {unnamedState.phase === "ready" && !unnamedNotAssessed && (
          <Kpi
            label="Unnamed workspaces"
            value={unnamedReady ? fmtMoney(unnamedSpend, 0) : "-"}
            sub={unnamedSub}
            tone={unnamedActive > 0 ? "coral" : "default"}
          />
        )}
      </KpiRow>
      {excludedNote && <div className="caveat-note">{excludedNote}</div>}

      <SpendMoversCard periodState={periodState} spend={spend} />

      <Card title="Spend by calendar month" right={<span className="muted">{`last ${MONTH_ROWS_MAX} months`}</span>}>
        {monthsForBars && monthsForBars.length ? (
          <MonthChart months={monthsForBars} />
        ) : <ChartNote state={monthAgg} label="cost_monthly_actuals" />}
      </Card>

      <Card title="Spend by environment" right={<span className="muted">click a row for its top workspaces</span>}>
        <EnvBreakdown items={envItems} state={wsAgg} />
      </Card>

      <div className="grid-2">
        <Card title="Top cost drivers by product" right={<span className="muted">{`total, last ${filters.window} days`}</span>}>
          {prodItems
            ? <RankedBars items={prodItems} n={6} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText="No product spend in this window." />
            : <ChartNote state={prodAgg} label="cost_dollarized_by_sku_day" />}
          {/* The chart gives each product's total; the table splits the same totals by environment. */}
          {prodItems && multiEnv && prodEnvRows && (
            <EnvSplitTable rows={prodEnvRows} envs={envOrder} n={6} label="Product" fmt={(v) => fmtMoney(v, 0)} />
          )}
        </Card>
        <Card title="Top cost drivers by workspace">
          {wsItems
            ? <RankedBars items={wsItems} n={5} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText="No workspace spend in this window." />
            : <ChartNote state={wsAgg} label="cost_dollarized_by_sku_day" />}
        </Card>
      </div>

      {showPerformance && (
        <Card
          title="Top 10 warehouses and clusters"
          right={computeTotal != null && spend != null ? <span className="muted">{`${fmtMoney(computeTotal, 0)} of ${fmtMoney(spend, 0)} spend`}</span> : null}
        >
          {computeItems
            ? <RankedBars items={computeItems} n={10} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText="No warehouse or cluster spend in this window." />
            : <ChartNote state={rowsOnceReady(whCostState) ? clCostState : whCostState} label={rowsOnceReady(whCostState) ? "cost_chargeback_by_cluster" : "cost_chargeback_by_warehouse"} />}
        </Card>
      )}

      <MoneyShapeSection filters={filters} prodAgg={prodAgg} />

      <Card
        title={`Spend by ${cardLabel}`}
        right={topTags.length > 1
          ? <TopTagSwitch tags={topTags} value={shownTag && shownTag.key} onChange={setChosenTag} />
          : <span className="muted">{cardReady && cardSplit.total > 0 ? `${fmtMoney(cardSplit.total - cardSplit.untagged, 0)} tagged of ${fmtMoney(cardSplit.total, 0)}` : null}</span>}
      >
        {cardView.phase === "loading" ? <div className="muted">Loading...</div>
          : cardReady && cardSplit.total > 0 ? (
            <React.Fragment>
              {topTags.length > 1 && <div className="muted">{`${fmtMoney(cardSplit.total - cardSplit.untagged, 0)} tagged of ${fmtMoney(cardSplit.total, 0)}`}</div>}
              <RankedBars items={allocBars(cardSplit, `No ${cardLabel}`)} n={5} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="$" emptyText={`Every dollar in this window carries no ${cardLabel} tag.`} />
            </React.Fragment>
          )
          : cardReady ? <div className="chart-note ok"><div className="chart-note-text">No priced spend in this window.</div></div>
          : <ChartNote state={cardView} label="rollup top" />}
      </Card>

      <UnusedTablesCard finding={unusedFinding} state={unusedState} wasteUsd={waste.usd} />

      {showPerformance && (
        <React.Fragment>
          <div className="money-section-title">Performance</div>
          <div className="grid-2">
            <Card title={`Failed or cancelled queries per ${failGrain}`} right={<span className="card-right-row"><GrainSwitch value={failGrain} onChange={setFailGrain} /><button type="button" className="ov-link-btn" onClick={() => jump({ tab: "queries" })}>{"Queries →"}</button></span>}>
              <div className="money-perf-line">
                {failedQByDay
                  ? `${fmtInt(failedQTotal)} failed or canceled quer${failedQTotal === 1 ? "y" : "ies"} ${perfRange}${failedQWorstIdx >= 0 ? `, worst day ${fmtDayShort(failedQByDay.days[failedQWorstIdx])} (${fmtInt(failedQByDay.values[failedQWorstIdx])})` : ""}.`
                  : "Loading..."}
              </div>
              {failedQSeries && <Facts items={envFacts(envOrder, failedQByEnv, fmtInt)} />}
              {failedQByDay ? (
                failedQByDay.days.length > 1
                  ? <GrainLines grain={failGrain} days={failedQByDay.days} series={failedQSeries || [{ label: "failed or cancelled", color: "var(--st-crit)", data: failedQByDay.values }]} />
                  : <div className="chart-note ok"><div className="chart-note-text">Only one day of data in this window -- not enough to draw a trend.</div></div>
              ) : <ChartNote state={failedQAgg} label="query_failed_queries_daily" />}
            </Card>
            <Card title="Time spent queued" right={<button type="button" className="ov-link-btn" onClick={() => jump({ tab: "queries", subtab: "capacity" })}>{"Queries →"}</button>}>
              <div className="money-perf-line">
                {queueReady ? `Waiting for a slot ${fmtDuration(slotS)}; waiting for start-up ${fmtDuration(startS)} added up${startup ? `, ${fmtDuration(startup.total.clock)} by the clock` : ""} ${perfRange}.` : "Loading..."}
              </div>
              {queueReady ? (
                <React.Fragment>
                  <div className="money-queue-part">
                    <div className="money-queue-title"><b>Waiting for a slot</b><span className="muted"> — warehouse full; fix: more clusters or spread the load</span></div>
                    {multiEnv && <Facts items={envFacts(envOrder, slotByEnv, fmtDuration)} />}
                    <RankedBars items={slotTopWh} n={5} valueFmt={(v) => fmtDuration(v)} unitLabel="slot wait" emptyText="No query waited for a slot this window." />
                  </div>
                  <div className="money-queue-part">
                    <div className="money-queue-title"><b>Waiting for start-up</b><span className="muted"> — warehouse stopped or provisioning; fix: longer auto-stop on classic or pro; on serverless a long wait means a slow or failed start</span></div>
                    {startup && <div className="muted">{`Warehouses ${startsText(startup.total)}.`}</div>}
                    {multiEnv && <Facts items={envFacts(envOrder, startByEnv, fmtDuration)} />}
                    <RankedBars items={startTopWh} n={5} valueFmt={(v) => fmtDuration(v)} unitLabel="start-up wait" emptyText="No query waited for start-up this window." />
                  </div>
                </React.Fragment>
              ) : <ChartNote state={queueCapAgg} label="query_queuing_waits" />}
            </Card>
            <Card title="Job reliability" right={<button type="button" className="ov-link-btn" onClick={() => jump({ tab: "jobs" })}>{"Jobs →"}</button>}>
              <div className="money-perf-line">
                {jobRelRows
                  ? `${fmtInt(jobRelCritical.length)} failing now (3+ in a row), ${fmtInt(jobRelWarn.length)} more fail ≥20% of runs`
                    + (failedRunsUsd != null ? `, ${fmtMoney(failedRunsUsd, 0)} burned on failed runs.` : ".")
                  : "Loading..."}
              </div>
              {jobRelRows && multiEnv && <Facts items={envFacts(envOrder, jobRelByEnv, (v) => `${fmtInt(v)} job${v === 1 ? "" : "s"}`)} />}
              {topFailingJobs
                ? <RankedBars items={topFailingJobs} n={5} valueFmt={(v) => fmtInt(v)} unitLabel="failed runs" emptyText="No failing jobs this window." />
                : <ChartNote state={jobRelState} label="lakeflow_job_reliability" />}
            </Card>
            <Card title="Jobs getting slower" right={<button type="button" className="ov-link-btn" onClick={() => jump({ tab: "jobs" })}>{"Jobs →"}</button>}>
              <div className="money-perf-line">
                {durRegFlagged
                  ? `${slowdownSentence(durRegFlagged.length, slowSplit)}${slowSplit.range ? `, ${slowSplit.range}` : ""}.`
                  : "Loading..."}
              </div>
              {durRegRows && multiEnv && <Facts items={envFacts(envOrder, durRegByEnv, (v) => `${fmtInt(v)} job${v === 1 ? "" : "s"}`)} />}
              {durRegTop
                ? <RankedBars items={durRegTop} n={5} valueFmt={(v) => `${v.toFixed(1)}x`} unitLabel="slower" emptyText="No job slowdowns this window." />
                : <ChartNote state={durRegState} label="lakeflow_job_duration_regression" />}
            </Card>
          </div>
        </React.Fragment>
      )}

      <PageFooter meta={meta} />
    </div>
  );
}
