// Overview (redesign section 6): a verdict sentence, four
// cards, a ranked "Fix these first" list, a coverage panel, account shape, and spend in dollars.
// Registers as this area's Content (primitives.tsx AreaContent) -- overview has no sub-tabs, so
// AreaPage renders this once, above the reference-only checks table (OVERVIEW_IDS).

import React from "react";

import { estLabel, fmtChangeVs, fmtDayShort, fmtDbu, fmtInt, fmtMoney, fmtMoneyShort, fmtPct } from "../format";
import { useNames, wsPhrase } from "../components/names";
import { FLAGGED_STATUSES, SPIKE_PCT, belowFloorNoteText, buildMonthBars, clusterName, excludedNoWorkspaceNote, fillDays, jobName, numOrZero, pipelineName, sumBy, topNWithOther, totalSpikeDays, useFindingAgg, useFindingData, useMultiFindingData, warehouseName } from "../components/hooks";
import { AREA_REGISTRY, COST_MONEY_COL, EST_SPEND_CAVEAT, OVERVIEW_IDS, WASTE_IDS, bandOf, homeForQuery } from "../components/tab_registry";
import { AreaContent, Card, ErrorBoundary, Facts, StatusPill, assessedCounts, bandPillKind, isUncertainBand } from "../components/primitives";
import { enumLabel, getCheckLabel } from "../components/labels";
import { ChartNote } from "../components/charts";
import { Columns, RankedBars } from "../components/charts_more";
import { SummaryTileDelta, SummaryTileSpark, firstAction, windowShortLabel } from "../components/overview_tile";
import { buildWasteWorklist, useBelowFloorWaste, wasteTotal } from "../components/waste_total";
import { OFFENDER_META, WASTE_FIX } from "./tab_waste";
import { roleSeesCheck } from "./tab_actions";
import { TagsOverviewCard } from "./tab_tags";
import { SpendMoversCard } from "./tab_money";
import { MonthChart } from "./tab_cost";
import { DAY_ROWS_MAX, GrainSwitch, MONTH_ROWS_MAX, latest } from "../components/grain";
import type { Grain } from "../components/grain";
import { SpendPath } from "../components/design_parts";
import { useRollupTop } from "../components/tag_rollup";
import { JobStateCard } from "./job_state";
import { QueueHeatCards } from "./query_queue";
import { guideLinkProps } from "../components/nav_hash";

function depOk(state: any) {
  return !!(state && state.phase === "ready" && state.outcome === "ok_rows");
}

// Only est_wasted_usd_list counts as possible waste; est_usd_list is spend (waste_total.ts).

// First sentence of a header field, cut before the library's "field heuristic" hedge.
function firstClause(text: any, maxLen?: any) {
  if (!text) return null;
  let s = String(text).trim();
  if (!s || s.toLowerCase().startsWith("n/a")) return null;
  const lower = s.toLowerCase();
  let cutAt = -1;
  [" - field heuristic", " (field heuristic"].forEach((marker) => {
    const idx = lower.indexOf(marker);
    if (idx > 0 && (cutAt === -1 || idx < cutAt)) cutAt = idx;
  });
  if (cutAt > 0) s = s.slice(0, cutAt);
  const sentenceMatch = s.match(/^(.*?[.!?])(\s|$)/);
  if (sentenceMatch) s = sentenceMatch[1];
  s = s.trim();
  const cap = maxLen || 160;
  if (s.length > cap) s = `${s.slice(0, cap - 1).trim()}...`;
  return s || null;
}

// A finding's WHY: its own columns first, then investigate_if, then read_this.
const WASTE_REASON_IDS = ["compute_warehouse_idle_minutes", "lakeflow_failed_jobs_wasted_dbus", "cost_failed_statement_waste"];

function tier1Reason(itemId: any, rows: any) {
  if (itemId === "compute_idle_node_ratio" && rows && rows.length) {
    const flaggedRows = rows.filter((r: any) => r.status === "CRITICAL" || r.status === "WARN");
    const ratios = flaggedRows
      .map((r: any) => (r.total_slices ? numOrZero(r.idle_slices) / numOrZero(r.total_slices) : null))
      .filter((v: any) => v != null);
    if (ratios.length) {
      const avgPct = (ratios.reduce((s: any, v: any) => s + v, 0) / ratios.length) * 100;
      return `avg ${avgPct.toFixed(0)}% idle-slice ratio across ${flaggedRows.length} cluster${flaggedRows.length === 1 ? "" : "s"}`;
    }
  }
  if (WASTE_REASON_IDS.includes(itemId) && rows && rows.length) {
    const top = rows.find((r: any) => r.status === "CRITICAL" || r.status === "WARN");
    if (top && top.waste_reason) return top.waste_reason;
  }
  return null;
}

export function findingReason(itemId: any, header: any, rows: any) {
  return tier1Reason(itemId, rows)
    || firstClause(header && header.investigate_if)
    || firstClause(header && header.read_this)
    || null;
}

// Priced items first ($ desc), then CRITICAL before WARN, then size -- the app's one ranking rule
// for "what to fix first" (DEC-57 rule 4), shared by the waste worklist and the Fix-first list.
function rankWorklist(flagged: any) {
  const rank: Record<string, number> = { CRITICAL: 0, WARN: 1 };
  return [...flagged].sort((a, b) => {
    const aPriced = a.dollarUsd != null, bPriced = b.dollarUsd != null;
    if (aPriced !== bPriced) return aPriced ? -1 : 1;
    if (aPriced && bPriced) return b.dollarUsd - a.dollarUsd;
    if (rank[a.band] !== rank[b.band]) return rank[a.band] - rank[b.band];
    return (b.dbuVal || b.rowCount || 0) - (a.dbuVal || a.rowCount || 0);
  });
}

// Worst check: CRITICAL first, then more flagged rows, then one without an open library issue.
function hasOpenCorrection(f: any) {
  return ((f && f.library_corrections) || []).some((c: any) => c.status !== "fixed");
}

function isWorseCheck(candidate: any, current: any) {
  if (!current) return true;
  const cBand = bandOf(candidate), curBand = bandOf(current);
  const cRank = cBand === "CRITICAL" ? 0 : 1, curRank = curBand === "CRITICAL" ? 0 : 1;
  if (cRank !== curRank) return cRank < curRank;
  const cSc = candidate.status_counts || {}, curSc = current.status_counts || {};
  const cCrit = numOrZero(cSc.CRITICAL), curCrit = numOrZero(curSc.CRITICAL);
  if (cCrit !== curCrit) return cCrit > curCrit;
  const cWarn = numOrZero(cSc.WARN), curWarn = numOrZero(curSc.WARN);
  if (cWarn !== curWarn) return cWarn > curWarn;
  const cOpen = hasOpenCorrection(candidate), curOpen = hasOpenCorrection(current);
  if (cOpen !== curOpen) return !cOpen;
  return false;
}

function domainTileSummary(findings: any, domain: any) {
  const rows = (findings || []).filter((f: any) => f.domain === domain && f.is_finding);
  const total = rows.length;
  let flaggedCount = 0, uncertainCount = 0, worst: any = null;
  const uncertainBands: Record<string, any> = {};
  rows.forEach((f: any) => {
    const band = bandOf(f);
    if (band === "CRITICAL" || band === "WARN") {
      flaggedCount += 1;
      if (isWorseCheck(f, worst)) worst = f;
    } else if (isUncertainBand(band)) {
      uncertainCount += 1;
      uncertainBands[band] = (uncertainBands[band] || 0) + 1;
    }
  });
  return { total, flaggedCount, uncertainCount, uncertainBands, worst, worstBand: worst ? bandOf(worst) : null };
}

// The one worst-check's own first row, named generically by whichever resource id it carries --
// works for any domain's worst check (not just the one or two ids a domain happened to special-
// case before), so a newly added check needs no code here to get a "where" on its Fix-first row.
function domainWorstResourceName(worstState: any, dims: any) {
  if (!worstState || worstState.phase !== "ready" || worstState.outcome !== "ok_rows") return null;
  const row = (worstState.data.rows || [])[0];
  if (!row) return null;
  if (row.job_id != null && row.workspace_id != null) return jobName(dims, row.workspace_id, row.job_id);
  if (row.pipeline_id != null && row.workspace_id != null) return pipelineName(dims, row.workspace_id, row.pipeline_id);
  if (row.warehouse_id != null) return warehouseName(dims, row.warehouse_id);
  if (row.cluster_id != null) return clusterName(dims, row.cluster_id);
  return null;
}

// The one raw waste row waste_total.ts already picked out (biggest $, or first flagged), named by
// its grain's own id column -- only for the WASTE_REASON_IDS ids, whose `reason` is that SAME row's
// own waste_reason (a resource-level cause). The other waste ids' reason is an aggregate across
// several resources, so naming one of them next to it would misstate which resource it is about.
function wasteRowName(itemId: any, row: any, dims: any) {
  if (!row || !WASTE_REASON_IDS.includes(itemId)) return null;
  if (itemId === "lakeflow_failed_jobs_wasted_dbus") return jobName(dims, row.workspace_id, row.job_id);
  return warehouseName(dims, row.warehouse_id);
}

// A check title up to its first qualifier: "Broad grants: ALL PRIVILEGES, ..." -> "Broad grants".
function shortTitle(title: any) {
  return String(title || "").split(/ - | -- |: | \(/)[0];
}

// `reading` stays a plain string too -- fixRowFromDomain's own `why` reuses it verbatim, and a Fix-
// first row is a list row, not a KPI card, so it keeps its one-line text rather than a facts list.
function domainTileProps(summary: any, worstName?: any): Record<string, any> {
  if (summary.total === 0) return { value: "-", reading: "No checks", facts: [{ label: "Status", value: "No checks", tone: "muted" }], severity: "neutral" };
  if (summary.flaggedCount > 0) {
    const worst = worstName || shortTitle(summary.worst.title);
    return {
      value: fmtInt(summary.flaggedCount), unit: "flagged",
      reading: `Worst: ${worst}`,
      // A resource name is never coloured -- only a number is (severity already shows on the
      // card's own big number via `severity` below).
      facts: [{ label: "Worst", value: worst }],
      severity: summary.worstBand === "CRITICAL" ? "critical" : "warn",
    };
  }
  if (summary.uncertainCount === summary.total) return { value: "-", reading: "Could not run", facts: [{ label: "Status", value: "Could not run", tone: "muted" }], severity: "not_assessed" };
  if (summary.uncertainCount > 0) {
    return {
      value: "0", unit: "flagged", reading: `${fmtInt(summary.uncertainCount)} of ${fmtInt(summary.total)} checks not run`,
      facts: [{ label: "Not run", value: `${fmtInt(summary.uncertainCount)} of ${fmtInt(summary.total)}`, detail: "checks", tone: "muted" }],
      severity: "not_assessed",
    };
  }
  return { value: "0", unit: "flagged", reading: "Nothing flagged", facts: [{ label: "Status", value: "Nothing flagged", tone: "ok" }], severity: "ok" };
}

// Measured $ change against the previous period of the same length (cost_period_over_period),
// structured (not pre-worded) so both the verdict sentence and the Spend card's delta line
// can phrase it their own way from the same two numbers.
function periodChangeDetail(rows: any, discountPct: any) {
  const judged = (rows || []).filter((r: any) => r.status !== "NOT_ASSESSED");
  if (!judged.length) return null;
  const disc = numOrZero(discountPct);
  const cur = sumBy(judged, "est_current_usd_list") * (1 - disc);
  const prev = sumBy(judged, "est_previous_usd_list") * (1 - disc);
  const diff = cur - prev;
  const dir = diff > 0.5 ? "up" : diff < -0.5 ? "down" : null;
  const rawPct = prev > 0 ? (Math.abs(diff) / prev) * 100 : null;
  // A percent off a near-zero previous-period base explodes to an unreadable number -- past 999%
  // every caller here already falls back to the $ figure instead, same as the no-previous-period case.
  const pct = rawPct != null && rawPct <= 999 ? rawPct : null;
  return { cur, prev, diff, dir, pct };
}

function lowerFirst(s: any) {
  const t = String(s || "");
  return t ? t.charAt(0).toLowerCase() + t.slice(1) : t;
}

// The one verdict sentence Overview leads with (section 6). Reported to AreaPage via setVerdict
// (primitives.tsx) rather than rendered as its own banner, so the shared page header carries it.
function verdictSentence({ ready, windowDays, estSpend, pcd, worklistReady, priced, flagged, wasteTotalObj, worklistTop, uncertainWasteCount, wasteCheckCount, comparedOn }: LooseProps) {
  if (!ready) return "Loading this window's numbers...";
  const spendText = estSpend != null ? fmtMoney(estSpend, 0) : "an unpriced amount";
  const changeText = pcd && pcd.dir ? fmtChangeVs(pcd.cur, pcd.prev, "the previous period") : null;
  const changePhrase = changeText ? `, ${changeText}${comparedOn || ""}` : "";
  const lead = `You spent ${spendText} in the last ${windowDays} days${changePhrase}.`;
  if (!worklistReady) return `${lead} Checking for possible waste...`;
  if (priced.length > 0) {
    const label = getCheckLabel(worklistTop.id, worklistTop.title);
    return `${lead} ${fmtMoney(wasteTotalObj.usd, 0)} of it is possible waste, most of it ${lowerFirst(label.title)}.`;
  }
  if (flagged.length > 0) {
    return `${lead} ${fmtInt(flagged.length)} check${flagged.length === 1 ? "" : "s"} flagged possible waste, but none of them carry a cost estimate yet.`;
  }
  if (wasteCheckCount > 0 && uncertainWasteCount === wasteCheckCount) {
    return `${lead} None of the waste checks could be verified in this export.`;
  }
  if (uncertainWasteCount > 0) {
    return `${lead} No possible waste priced yet, but ${fmtInt(uncertainWasteCount)} of ${fmtInt(wasteCheckCount)} waste checks weren't verified.`;
  }
  return `${lead} No possible waste flagged this window.`;
}

// A plain product name for system.billing.usage's billing_origin_product enum -- known codes get
// a real name; an unrecognised one is title-cased rather than left as a shouting enum value, so a
// product this pass never anticipated still reads as words, not a code (product must work on any
// customer's account, not just the ones whose product mix this pass has seen).
const PRODUCT_LABELS: Record<string, string> = {
  ALL_PURPOSE: "All-purpose clusters", INTERACTIVE: "Serverless notebooks", JOBS: "Jobs", SQL: "SQL warehouses",
  DLT: "Delta Live Tables", LAKEFLOW_PIPELINES: "Lakeflow pipelines",
  MODEL_SERVING: "Model serving", VECTOR_SEARCH: "Vector Search", MODEL_TRAINING: "Model training",
  GENIE: "Genie", LAKEHOUSE_MONITORING: "Lakehouse Monitoring",
  PREDICTIVE_OPTIMIZATION: "Predictive optimization", STORAGE: "Storage", SERVERLESS_GENIE: "Genie",
  DBSQL: "Databricks SQL", MODEL_SERVING_V2: "Model serving",
};
export function productLabel(code: any) {
  if (!code) return "(unlabeled product)";
  if (PRODUCT_LABELS[code]) return PRODUCT_LABELS[code];
  return String(code).replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

// Short name for a WASTE_ITEMS id on the "Possible waste" split bar and the Fix-first list's area
// column -- the resource TYPE the check watches, not the check's own longer display title.
const WASTE_AREA_SHORT: Record<string, string> = {
  compute_warehouse_idle_minutes: "Warehouses",
  compute_idle_node_ratio: "Clusters",
  lakeflow_failed_jobs_wasted_dbus: "Failed runs",
  cost_failed_statement_waste: "Failed statements",
  compute_serving_endpoint_cost_status: "Serving endpoints",
};
function wasteAreaShort(item: any) {
  return WASTE_AREA_SHORT[item.id] || (AREA_REGISTRY[item.area] && AREA_REGISTRY[item.area].label) || item.area;
}

const EFFORT_LABEL: Record<string, string> = { free: "Free fix", config: "Config fix", spend: "Spend" };

// The one labelled number Waste > Priced's own OFFENDER_META (tab_waste.tsx) leads with for this
// id's top flagged row, condensed to the single line a Fix-first row has room for -- reuses that
// map directly rather than recomputing it, so the two screens can never read a different number.
function wasteRowLeadFact(itemId: any, row: any) {
  const meta = OFFENDER_META[itemId];
  const lead = meta && row ? meta.facts(row)[0] : null;
  if (!lead) return null;
  return `${lead.label} ${lead.value}${lead.detail ? ` ${lead.detail}` : ""}`;
}

// One "Fix these first" row from a flagged waste worklist entry. `why` leads with the specific
// resource this row is about (its own row's name), not just the check's generic title, so the row
// answers WHERE as well as WHY. Without a resolvable resource name there is no "X: reason" to
// build, and label.why (a curated one-liner, or null) is the safe fallback -- never w.reason on
// its own, which can be the raw waste_reason column (a multi-clause sentence that can name a
// column). The fix line is the same WASTE_FIX (tab_waste.tsx) plain one-liner Waste > Priced
// shows, never firstAction's header text, which is written for any check and can also name one.
function fixRowFromWaste(w: any, dims: any) {
  const label = getCheckLabel(w.id, w.title);
  const name = wasteRowName(w.id, w.topRow, dims);
  const fact = wasteRowLeadFact(w.id, w.topRow);
  const why = name ? (fact ? `${name}: ${fact}` : name) : label.why;
  // The named resource's OWN dollar figure, never the whole check's total across every flagged
  // resource -- "Warehouse X: $1,004" must mean warehouse X, not warehouse X plus 18 others.
  const topRowUsd = name && w.topRow && w.dollarCol && w.topRow[w.dollarCol] != null
    ? numOrZero(w.topRow[w.dollarCol]) * (1 - numOrZero(w.discountPct))
    : null;
  // The whole fix's money leads; the named resource's own share sits under it.
  const moreCount = w.flaggedCount > 1 ? w.flaggedCount - 1 : 0;
  const money = w.dollarUsd != null ? w.dollarUsd : topRowUsd;
  const moneyNote = topRowUsd != null
    ? (moreCount > 0 ? `worst ${fmtMoney(topRowUsd, 0)} of ${fmtInt(w.flaggedCount)} flagged` : "possible waste")
    : (w.dollarUsd != null ? "possible waste"
      : w.billedUsd != null ? `${fmtMoney(w.billedUsd, 0)} ${w.spendWords}, not priced as waste`
      : (w.dbuVal != null ? `${fmtDbu(w.dbuVal, 0)}, no cost estimate` : "no cost estimate"));
  const fixLine = WASTE_FIX[w.id];
  return {
    id: w.id, band: w.band, area: `Waste · ${wasteAreaShort(w)}`,
    title: label.title, why,
    action: fixLine ? { text: fixLine, full: fixLine, tier: null } : null,
    money, moneyNote,
    target: { ...homeForQuery({ query_id: w.id }), focusQueryId: w.id },
  };
}

// firstAction's rung is a header author's own free-text sentence -- most stay plain prose, but at
// least one (compute_warehouse_idle_minutes) names a raw column mid-sentence ("auto_stop_minutes").
// An underscore never belongs on screen, so this turns any into a space rather than trusting every
// header across every domain to avoid one.
function screenAction(raw: any) {
  if (!raw) return null;
  return { ...raw, text: raw.text.replace(/_/g, " "), full: raw.full.replace(/_/g, " ") };
}

// One "Fix these first" row from a domain's own worst flagged finding (jobs/query/governance/...)
// -- `tile` is that domain's already-computed SummaryTile props (its `reading` is reused as WHY,
// so this never recomputes a second explanation for the same number the card above already shows).
function fixRowFromDomain(areaLabel: any, tile: any, summary: any, target: any, headerState: any) {
  if (!summary.worst) return null;
  const band = summary.worstBand;
  if (band !== "CRITICAL" && band !== "WARN") return null;
  const label = getCheckLabel(summary.worst.query_id, summary.worst.title);
  const header = headerState && headerState.phase === "ready" && headerState.outcome === "ok_rows" ? headerState.data.header : null;
  return {
    id: summary.worst.query_id, band, area: areaLabel,
    title: label.title, why: (tile && typeof tile.reading === "string" && tile.reading) || label.why,
    action: header ? screenAction(firstAction(header)) : null,
    money: null, moneyNote: "no cost estimate",
    target: { ...target, focusQueryId: summary.worst.query_id },
  };
}

function rankFixRows(rows: any) {
  const rank: Record<string, number> = { CRITICAL: 0, WARN: 1 };
  return [...rows].sort((a, b) => {
    const aP = a.money != null, bP = b.money != null;
    if (aP !== bP) return aP ? -1 : 1;
    if (aP && bP) return b.money - a.money;
    return (rank[a.band] ?? 2) - (rank[b.band] ?? 2);
  });
}

const FIX_LIST_SHOWN = 5;

function FixListRow({ n, row, onOpen }: LooseProps) {
  const critMoney = row.band === "CRITICAL";
  return (
    <div className="ov-fix-row">
      <div className="ov-fix-n mono">{n}</div>
      <div className="ov-fix-body">
        <div className="ov-fix-head">
          <StatusPill kind={bandPillKind(row.band)} />
          <span className="ov-fix-area">{row.area}</span>
        </div>
        <div className="ov-fix-title">{row.title}</div>
        {row.why && <div className="ov-fix-why">{row.why}</div>}
        {row.action && row.action.text && (
          <div className="ov-fix-action">
            {row.action.tier && <span className={`tier-chip tier-${row.action.tier}`}>{EFFORT_LABEL[row.action.tier] || row.action.tier}</span>}
            <span title={row.action.full}>{row.action.text}</span>
          </div>
        )}
      </div>
      <div className="ov-fix-money">
        <div className={`ov-fix-money-val mono${critMoney ? " tone-crit" : ""}`}>{row.money != null ? fmtMoney(row.money, 0) : "—"}</div>
        <div className="ov-fix-money-note">{row.moneyNote}</div>
        <button type="button" className="ov-fix-open" onClick={onOpen}>{"Open →"}</button>
      </div>
    </div>
  );
}

function FixFirstCard({ rows, ready, onOpen, onMore }: LooseProps) {
  const shown = rows.slice(0, FIX_LIST_SHOWN);
  const rest = rows.slice(FIX_LIST_SHOWN);
  const restUnpriced = rest.filter((r: any) => r.money == null).length;
  return (
    <Card className="ov-fix-card" title="Fix these first" right={<span className="faint">Ranked by estimated money, then severity</span>}>
      {!ready ? (
        // Ranked by money, so the list waits for the money instead of re-sorting under the reader.
        <div className="ov-fix-empty">Loading...</div>
      ) : shown.length === 0 ? (
        <div className="ov-fix-empty">Nothing flagged this window.</div>
      ) : shown.map((row: any, i: any) => (
        <FixListRow key={row.id} n={i + 1} row={row} onOpen={() => onOpen(row.target)} />
      ))}
      {rest.length > 0 && (
        <button type="button" className="ov-fix-more" onClick={onMore}>
          <span>{`${fmtInt(rest.length)} more flagged${restUnpriced === rest.length ? " without a cost estimate" : ""}`}</span>
          <span>{"Waste & savings →"}</span>
        </button>
      )}
    </Card>
  );
}

function OvMeter({ label, n, total, warn }: LooseProps) {
  const pct = total > 0 ? Math.min(100, (n / total) * 100) : 0;
  return (
    <div className="ov-meter">
      <div className="ov-meter-head"><span>{label}</span><span className="mono">{`${fmtInt(n)} / ${fmtInt(total)}`}</span></div>
      <div className={`ov-meter-track${warn ? " warn" : ""}`}><div className="ov-meter-fill" style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

function CoverageCard({ totalExecutable, notAssessedCount, findings, goTo }: LooseProps) {
  const assessed = totalExecutable - notAssessedCount;
  const govRows = (findings || []).filter((f: any) => f.domain === "governance_access" && f.is_finding);
  const govTotal = govRows.length;
  let govVerified = 0, govCouldntRun = 0, govNoData = 0, govCrit = 0, govWarn = 0;
  govRows.forEach((f: any) => {
    const band = bandOf(f);
    if (band === "CRITICAL") govCrit += 1;
    if (band === "WARN") govWarn += 1;
    if (!isUncertainBand(band)) govVerified += 1;
    else if (band === "NOT_ASSESSED" || band === "ERROR") govCouldntRun += 1;
    else govNoData += 1;
  });
  const govUncertain = govTotal - govVerified;
  return (
    <Card title="How much this audit could see">
      <OvMeter label="Checks that ran" n={assessed} total={totalExecutable} />
      {govTotal > 0 && <OvMeter label="Governance & access verified" n={govVerified} total={govTotal} warn={govUncertain > 0} />}
      {govTotal > 0 && govUncertain > 0 && (
        <div className="ov-warn-note">
          {`Governance: ${fmtInt(govCrit)} critical and ${fmtInt(govWarn)} warning on the ${fmtInt(govVerified)} checks that ran; ${fmtInt(govUncertain)} weren't verified`}
          {(govCouldntRun > 0 || govNoData > 0) ? ` -- ${[govCouldntRun > 0 ? `${fmtInt(govCouldntRun)} couldn't run` : null, govNoData > 0 ? `${fmtInt(govNoData)} had no data in this window` : null].filter(Boolean).join(", ")}` : ""}
          {". Silence here is not a pass."}
        </div>
      )}
      <button type="button" className="ov-link-btn" onClick={() => goTo({ tab: "coverage" })}>{"See what's missing and why →"}</button>
    </Card>
  );
}

function AccountShapeCard({ rows }: LooseProps) {
  return (
    <Card title="Account shape">
      {rows.map((r: any) => (
        <div className="ov-shape-row" key={r.k}>
          <span className="muted">{r.k}</span>
          <span className="mono">{r.v}</span>
        </div>
      ))}
    </Card>
  );
}

// `chart` is a visual element (delta arrow, sparkline, split bar) that always renders above the
// text slot -- it is never a sentence, so it stays out of the facts/children choice below it.
function OverviewKpiCard({ label, value, unit, tone, onClick, chart, facts, children }: LooseProps) {
  const clickable = typeof onClick === "function";
  return (
    <div
      className={`card ov-kpi-card${clickable ? " clickable" : ""}`}
      onClick={clickable ? onClick : undefined}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable ? (e) => { if (e.key === "Enter" || e.key === " ") onClick(); } : undefined}
    >
      <div className="ov-kpi-label">{label}</div>
      <div className="ov-kpi-value-row">
        <span className={`ov-kpi-value mono${tone ? ` tone-${tone}` : ""}`}>{value}</span>
        {unit && <span className="ov-kpi-unit">{unit}</span>}
      </div>
      {chart}
      {facts ? <Facts items={facts} /> : children}
    </div>
  );
}

// Governance's Overview reads a wholly different set of checks (grants, unmasked PII, run-as,
// shares, admin changes) than every other role's spend-first page -- a separate component keeps
// each side's own fixed hook set intact (React's rules of hooks forbid skipping SpendOverview's
// data fetches just because this role never renders them).
const GOV_OVERVIEW_CARDS = [
  { id: "access_broad_grants", subtab: "access", label: "Broad grants" },
  { id: "access_classified_unmasked", subtab: "sensitive", label: "Unmasked classified" },
  { id: "access_runas_escalation", subtab: "access", label: "Run-as escalation" },
  { id: "access_delta_sharing_exposure", subtab: "sharing", label: "Delta Sharing exposure" },
  { id: "access_admin_role_change_events", subtab: "admin", label: "Admin and permission changes" },
];

function GovernanceOverviewContent({ filters, goTo, setVerdict, allFindingsIndex }: LooseProps) {
  const findingsById = allFindingsIndex || {};
  const grants = findingsById["access_broad_grants"];
  const unmasked = findingsById["access_classified_unmasked"];
  const ready = GOV_OVERVIEW_CARDS.some((c) => findingsById[c.id]);

  const sentence = !ready ? "Loading governance checks..." : (() => {
    const n = grants && grants.affected ? numOrZero(grants.affected.flagged) : 0;
    const c = grants && grants.status_counts ? numOrZero(grants.status_counts.CRITICAL) : 0;
    const m = unmasked && unmasked.affected ? numOrZero(unmasked.affected.flagged) : 0;
    return `${fmtInt(n)} broad grant${n === 1 ? "" : "s"} (${fmtInt(c)} critical), ${fmtInt(m)} classified column${m === 1 ? "" : "s"} unmasked.`;
  })();
  React.useEffect(() => { if (setVerdict) setVerdict(sentence); }, [sentence, setVerdict]);

  const jump = (target: any) => { if (goTo) goTo(target); };

  return (
    <div>
    <div className="ov-kpi-grid">
      {GOV_OVERVIEW_CARDS.map((c) => {
        const f = findingsById[c.id];
        const band = f ? bandOf(f) : null;
        const ran = f && band !== "NOT_ASSESSED" && band !== "ERROR";
        const affected = ran ? f.affected : null;
        const value = affected != null ? fmtInt(affected.flagged) : (ran ? "0" : "-");
        const unit = affected != null ? `of ${fmtInt(affected.total)} ${affected.noun || ""}`.trim() : undefined;
        const statusWord = !f ? "Not in this export" : band === "EMPTY_WINDOW" ? "No data in this window"
          : band === "NOT_ASSESSED" || band === "ERROR" ? "Not assessed" : band === "CRITICAL" ? "Critical" : band === "WARN" ? "Warning" : "OK";
        return (
          <OverviewKpiCard
            key={c.id}
            label={c.label}
            value={value}
            unit={unit}
            tone={band === "CRITICAL" ? "crit" : band === "WARN" ? "warn" : undefined}
            onClick={() => jump({ tab: "governance", subtab: c.subtab, focusQueryId: c.id })}
            facts={[{ label: "Status", value: statusWord, tone: band === "CRITICAL" ? "crit" : band === "WARN" ? "warn" : undefined }]}
          />
        );
      })}
    </div>
    <ErrorBoundary><TagsOverviewCard filters={filters} onOpen={() => jump({ tab: "tags" })} /></ErrorBoundary>
    </div>
  );
}

// List price, after the discount in settings, and the part the first top tag (settings) reaches.
function SpendPathCard({ filters, meta, listUsd, discountPct, onTags }: LooseProps) {
  const top = meta && meta.top_tags && meta.top_tags.length ? meta.top_tags[0] : null;
  const roll = useRollupTop("cost", top ? top.key : null, filters.window, filters.workspaceIds, filters.envs, 1);
  const d = roll.phase === "ready" && roll.data && roll.data.outcome === "ok_rows" ? roll.data : null;
  const total = d ? numOrZero(d.total && d.total.usd) : 0;
  const tagged = d && total > 0
    ? { netUsd: numOrZero(d.tagged.usd_disc != null ? d.tagged.usd_disc : d.tagged.usd * (1 - discountPct)), share: numOrZero(d.tagged.usd) / total }
    : null;
  if (listUsd == null || !top) return null;
  return (
    <Card title={`From list price to ${top.label.toLowerCase()}`}>
      <SpendPath listUsd={listUsd} discountPct={discountPct} tagged={tagged} tagLabel={top.label} onTags={onTags} />
    </Card>
  );
}

function SpendOverviewContent({ filters, dims, meta, goTo, setVerdict, allFindingsIndex, roleAreas, role }: LooseProps) {
  useNames(); // re-render once workspace names (names.tsx) are ready, for the spike caption below
  const inRole = (area: any) => !roleAreas || roleAreas.includes(area);

  // Overview has no owned ids of its own (AREA_REGISTRY: ids:[], refIds: OVERVIEW_IDS) -- the
  // scoped `findings` AreaPage hands every content component would be almost empty here, so this
  // page reads the full set instead (every domain tile below needs checks it doesn't own).
  const findings = React.useMemo(() => Object.values<any>(allFindingsIndex || {}), [allFindingsIndex]);

  const multi = useMultiFindingData(
    OVERVIEW_IDS.filter((id) => id !== "overview_dbu_by_sku" && id !== "overview_daily_dbu_trend"),
    filters.window, filters.workspaceIds, filters.envs, 5000
  );
  const wasteMulti = useMultiFindingData(WASTE_IDS, filters.window, filters.workspaceIds, filters.envs, 5000, FLAGGED_STATUSES);
  const mlaiAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["billing_origin_product"], "sum", COST_MONEY_COL);
  const dailyAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, ["usage_date"], "sum", COST_MONEY_COL);
  const [grain, setGrain] = React.useState<Grain>("day");
  // Months come from the monthly billing check, so they reach past the export window; fetched once Month is picked.
  const monthAgg = useFindingAgg(grain === "month" ? "cost_monthly_actuals" : null, filters.window, filters.workspaceIds, filters.envs, ["month_start", "is_partial_month"], "sum", "net_list_cost_usd");
  const spendAgg = useFindingAgg("cost_dollarized_by_sku_day", filters.window, filters.workspaceIds, filters.envs, [], "sum", COST_MONEY_COL);
  const spikesState = useFindingData("cost_daily_spikes", filters.window, filters.workspaceIds, filters.envs, 3000);

  const byId = multi.byId;
  const spend = byId["overview_spend_estimate"];
  const splitFinding = byId["overview_serverless_classic_split"];

  const spendRows = depOk(spend) ? spend.data!.rows : null;
  const splitRows = depOk(splitFinding) ? splitFinding.data!.rows : null;
  const dailyAggReady = dailyAgg.phase === "ready" && dailyAgg.outcome === "ok_rows";
  const spendAggReady = spendAgg.phase === "ready" && spendAgg.outcome === "ok_rows";
  const mlaiAggReady = mlaiAgg.phase === "ready" && mlaiAgg.outcome === "ok_rows";

  const netDbus = spendRows ? sumBy(spendRows, "total_net_dbus") : null;
  const spendDiscountPct = spendAggReady ? numOrZero(spendAgg.data.discount_pct) : 0;
  const estSpend = spendAggReady ? numOrZero(spendAgg.data.total_value) * (1 - spendDiscountPct) : null;
  const excludedNote = spendAggReady ? excludedNoWorkspaceNote(spendAgg.data) : null;
  const rangeLabel = windowShortLabel(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);
  // A partial snapshot (10 days captured under a 30d window) never labels a comparison "30d" --
  // that names days the export does not have.
  const effectiveDays = meta && meta.snapshot_days && meta.snapshot_days < filters.window ? meta.snapshot_days : filters.window;

  const mlaiGroups = mlaiAggReady
    ? mlaiAgg.data.groups.filter((g) => g.key && (g.key[0] === "MODEL_SERVING" || g.key[0] === "VECTOR_SEARCH"))
    : null;
  const mlaiTotalRaw = mlaiGroups ? mlaiGroups.reduce((s, g) => s + numOrZero(g.value), 0) : null;
  const mlaiDiscountPct = mlaiAggReady ? numOrZero(mlaiAgg.data.discount_pct) : 0;
  const mlaiSpend = mlaiTotalRaw != null ? mlaiTotalRaw * (1 - mlaiDiscountPct) : null;
  const mlaiSourceTotal = mlaiAggReady ? numOrZero(mlaiAgg.data.total_value) : 0;
  const mlaiSharePct = mlaiSourceTotal > 0 && mlaiTotalRaw != null ? (mlaiTotalRaw / mlaiSourceTotal) * 100 : null;

  let serverlessPct = null, photonPct = null, untaggedPct = null;
  if (splitRows) {
    const totalDbu = sumBy(splitRows, "total_net_dbus");
    if (totalDbu > 0) {
      serverlessPct = (sumBy(splitRows, "serverless_net_dbus") / totalDbu) * 100;
      photonPct = (sumBy(splitRows, "photon_net_dbus") / totalDbu) * 100;
      untaggedPct = (sumBy(splitRows, "untagged_net_dbus") / totalDbu) * 100;
    }
  }

  const wsCount = filters.workspaceIdSet && filters.workspaceIdSet.size > 0 ? filters.workspaceIdSet.size : null;

  // Daily $ series (account-wide), ascending by date -- the Spend sparkline and the Daily
  // spend chart both read this one aggregate, never two different sums of the same rows.
  // fillDays (hooks.ts): a day with no billed row is a real $0, not a gap -- skipping it
  // misaligns the spike scan's "7 days before" window against real calendar days.
  const daily = dailyAggReady
    ? (() => {
        const filled = fillDays(
          dailyAgg.data.groups.map((g) => ({ day: String(g.key[0]).slice(0, 10), value: numOrZero(g.value) })),
          meta && meta.as_of_date, filters.window, meta && meta.snapshot_days
        );
        return { days: filled.map((d) => d.day), values: filled.map((d) => d.value) };
      })()
    : null;
  const dailyDays = daily ? latest(daily.days, DAY_ROWS_MAX) : [];
  const dailyValues = daily ? latest(daily.values, DAY_ROWS_MAX) : [];
  const monthsForBars = monthAgg.phase === "ready" && monthAgg.outcome === "ok_rows"
    ? buildMonthBars(monthAgg.data.groups, monthAgg.data.discount_pct, MONTH_ROWS_MAX) : null;

  // Spike days: cost_daily_spikes emits only WARN/NOT_ASSESSED rows (never OK) -- a WARN row is a
  // real, priced jump; group by day so a day with several spiking workspaces/products marks once.
  const spikeReady = spikesState.phase === "ready" && spikesState.outcome === "ok_rows";
  const spikeRows = spikeReady ? spikesState.data.rows.filter((r) => r.status === "WARN") : [];
  const spikeByDay = React.useMemo(() => {
    const m = new Map();
    spikeRows.forEach((r) => {
      const day = String(r.usage_date).slice(0, 10);
      const cur = m.get(day) || { total: 0, rows: [] };
      cur.total += numOrZero(r.est_day_usd_list);
      cur.rows.push(r);
      m.set(day, cur);
    });
    return m;
  }, [spikeRows]);
  // Triangles judge the day's total (the bar); the per-line spikes explain what drove a marked day.
  const totalSpikes = daily ? totalSpikeDays(daily.days.map((day, i) => ({ day, value: daily.values[i] }))) : [];
  const markers = totalSpikes.map((s) => ({ day: s.day, label: `Spike: ${fmtMoney(s.value, 0)}, +${fmtPct(s.pct, 0)} on the 7-day median` }));
  const driverOf = (day: any) => [...((spikeByDay.get(day) || {}).rows || [])].sort((a, b) => numOrZero(b.est_day_usd_list) - numOrZero(a.est_day_usd_list))[0];
  const topSpikes = [...totalSpikes].sort((a, b) => b.pct - a.pct).slice(0, 2);
  const spikeCaption = totalSpikes.length > 0
    ? `${fmtInt(totalSpikes.length)} spike day${totalSpikes.length === 1 ? "" : "s"} (${SPIKE_PCT}%+ over the 7-day median): ${topSpikes.map((s) => {
        const r = driverOf(s.day);
        return `${fmtDayShort(s.day)} ${fmtMoney(s.value, 0)} (+${fmtPct(s.pct, 0)})${r ? `, mostly ${productLabel(r.billing_origin_product)} in ${wsPhrase(r.workspace_id)}` : ""}`;
      }).join("; ")}.`
    : (dailyAggReady ? `No day ${SPIKE_PCT}% or more above the median of the 7 days before it.` : null);

  // By product, in dollars, plain names -- the same aggregate the ML & AI card reads, unfiltered.
  const productEntries = mlaiAggReady
    ? mlaiAgg.data.groups.map((g) => ({ name: productLabel(g.key && g.key[0]), value: numOrZero(g.value) }))
    : [];
  const productTotal = productEntries.reduce((s, e) => s + e.value, 0);

  const findingsById = React.useMemo(() => {
    const idx: Record<string, any> = {};
    (findings || []).forEach((f) => { idx[f.query_id] = f; });
    return idx;
  }, [findings]);

  const periodState = useFindingData("cost_period_over_period", filters.window, filters.workspaceIds, filters.envs);
  const pcd = periodState.phase === "ready" && periodState.outcome === "ok_rows"
    ? periodChangeDetail(periodState.data.rows, periodState.data.discount_pct) : null;

  const worklist = React.useMemo(() => buildWasteWorklist(findingsById, wasteMulti), [findingsById, wasteMulti]);
  const worklistReady = wasteMulti.phase === "ready";
  const flagged = worklist.filter((w) => w.isFlagged);
  const priced = flagged.filter((w) => w.dollarUsd != null);
  const rankedFlagged = rankWorklist(flagged);
  const wasteScoreable = worklist.filter((w) => w.band !== "INVENTORY");
  const wasteCheckCount = wasteScoreable.length;
  const uncertainWasteItems = wasteScoreable.filter((w) => isUncertainBand(w.band));
  const uncertainWasteCount = uncertainWasteItems.length;
  const belowFloorById = useBelowFloorWaste(filters);
  const wasteTotalObj = wasteTotal(worklist, belowFloorById);

  // assessedCounts (contract H): the SAME checks-assessed count the top bar shows, so Overview's
  // coverage meter never reads a different total than the page chrome around it (147/148 vs 148/148).
  const { assessed: assessedExecutable, total: totalExecutable } = assessedCounts(findings);
  const notAssessedCount = totalExecutable - assessedExecutable;
  const jobsSummary = React.useMemo(() => domainTileSummary(findings, "jobs_pipelines"), [findings]);
  const perfSummary = React.useMemo(() => domainTileSummary(findings, "performance"), [findings]);
  const governanceSummary = React.useMemo(() => domainTileSummary(findings, "governance_access"), [findings]);

  const jobsWorstState = useFindingData(jobsSummary.worst ? jobsSummary.worst.query_id : null, filters.window, filters.workspaceIds, filters.envs, 1);
  const perfWorstState = useFindingData(perfSummary.worst ? perfSummary.worst.query_id : null, filters.window, filters.workspaceIds, filters.envs, 1);
  const jobsBrokenState = useFindingData("lakeflow_job_reliability", filters.window, filters.workspaceIds, filters.envs, 1, ["CRITICAL"]);
  const jobsBrokenRow = (jobsBrokenState.phase === "ready" && jobsBrokenState.outcome === "ok_rows" && jobsBrokenState.data.rows && jobsBrokenState.data.rows[0]) || null;

  const jump = (target: any) => { if (goTo) goTo(target); };
  const worklistTop = rankedFlagged.length > 0 ? rankedFlagged[0] : null;
  const toWaste = worklistTop ? { ...homeForQuery({ query_id: worklistTop.id }), focusQueryId: worklistTop.id } : { tab: "waste" };
  const wasteSev = flagged.some((w) => w.band === "CRITICAL") ? "critical" : "warn";
  const unpricedCount = flagged.length - priced.length;

  let wasteTile;
  if (!worklistReady) {
    wasteTile = { value: "-", reading: "Loading...", severity: "neutral" };
  } else if (priced.length > 0) {
    wasteTile = {
      value: fmtMoney(wasteTotalObj.usd, 0), unit: "possible", noun: "possible waste",
      reading: `Top: ${shortTitle(worklistTop.title)} ${fmtMoney(worklistTop.dollarUsd, 0)}${unpricedCount > 0 ? ` · +${fmtInt(unpricedCount)} with no $` : ""}`,
      severity: wasteSev,
    };
  } else if (flagged.length > 0) {
    wasteTile = { value: fmtInt(flagged.length), unit: "flagged", noun: "waste checks flagged", reading: `Top: ${shortTitle(worklistTop.title)}`, severity: wasteSev };
  } else if (uncertainWasteCount === wasteCheckCount) {
    wasteTile = { value: "-", reading: "Could not run", severity: "not_assessed" };
  } else if (uncertainWasteCount > 0) {
    wasteTile = { value: "0", unit: "flagged", reading: `${fmtInt(uncertainWasteCount)} of ${fmtInt(wasteCheckCount)} checks not run`, severity: "not_assessed" };
  } else {
    wasteTile = { value: "0", unit: "flagged", reading: "Nothing flagged", severity: "ok" };
  }

  let jobsTile: Record<string, any> = { ...domainTileProps(jobsSummary, domainWorstResourceName(jobsWorstState, dims)), noun: "jobs checks flagged" };
  const reliabilityFinding = findingsById["lakeflow_job_reliability"];
  const brokenJobs = reliabilityFinding && reliabilityFinding.status_counts
    && ["CRITICAL", "WARN", "OK"].includes(bandOf(reliabilityFinding))
    ? numOrZero(reliabilityFinding.status_counts.CRITICAL) : 0;
  if (brokenJobs > 0) {
    const jobLabel = jobsBrokenRow ? jobName(dims, jobsBrokenRow.workspace_id, jobsBrokenRow.job_id) : null;
    jobsTile = {
      value: fmtInt(brokenJobs), unit: brokenJobs === 1 ? "broken job" : "broken jobs", severity: "critical",
      reading: jobLabel
        ? `${jobLabel} failed ${fmtInt(jobsBrokenRow!.consecutive_failures)} runs in a row`
        : jobsTile.reading,
      facts: jobLabel
        ? [{ label: "Worst", value: jobLabel }, { label: "Failed", value: fmtInt(jobsBrokenRow!.consecutive_failures), detail: "runs in a row", tone: "crit" }]
        : jobsTile.facts,
    };
  }

  const perfWorstName = domainWorstResourceName(perfWorstState, dims);
  const perfTile: Record<string, any> = { ...domainTileProps(perfSummary, perfWorstName), noun: "query checks flagged" };
  const perfWorstRow = perfWorstState.phase === "ready" && perfWorstState.outcome === "ok_rows"
    && perfWorstState.data.rows && perfWorstState.data.rows[0];
  if (perfWorstName && perfWorstRow && perfWorstRow.warehouse_size) {
    perfTile.reading = `Worst: ${perfWorstName} (${enumLabel(perfWorstRow.warehouse_size)})`;
    perfTile.facts = [{ label: "Worst", value: perfWorstName, detail: enumLabel(perfWorstRow.warehouse_size) }];
  }
  const govTile: Record<string, any> = { ...domainTileProps(governanceSummary), noun: "governance checks flagged" };
  const toGovernance = { tab: "governance", focusQueryId: governanceSummary.worst ? governanceSummary.worst.query_id : null };

  const periodFinding = findingsById["cost_period_over_period"];
  const periodBand = periodFinding ? bandOf(periodFinding) : null;

  // Fix these first: the waste worklist's own flagged entries, plus each other domain's own worst
  // flagged finding (never duplicated -- a domain whose worst IS a waste item is skipped here,
  // since the waste row above already carries it home-linked).
  const fixRows = React.useMemo(() => {
    const rows = rankedFlagged.map((w) => fixRowFromWaste(w, dims));
    const seen = new Set(rows.map((r) => r.id));
    const extra = [
      !seen.has(jobsSummary.worst && jobsSummary.worst.query_id) && fixRowFromDomain("Jobs", jobsTile, jobsSummary, { tab: "jobs" }, jobsWorstState),
      !seen.has(perfSummary.worst && perfSummary.worst.query_id) && fixRowFromDomain("Query performance", perfTile, perfSummary, { tab: "queries" }, perfWorstState),
      !seen.has(governanceSummary.worst && governanceSummary.worst.query_id) && fixRowFromDomain("Governance & PII", govTile, governanceSummary, toGovernance, null),
    ].filter(Boolean);
    return rankFixRows([...rows, ...extra]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rankedFlagged, dims, jobsSummary, perfSummary, governanceSummary, jobsWorstState.phase, perfWorstState.phase, jobsBrokenState.phase]);
  // Roles: this role's own Fix-first list keeps the fixes whose check this role sees, waste sources included.
  const roleFixRows = roleAreas ? fixRows.filter((r) => roleSeesCheck(new Set(roleAreas), { query_id: r.id })) : fixRows;

  // Some usage stayed unpriced on at least one side (cost_period_over_period withholds a verdict
  // there), so the judged total the change is measured on can fall short of the headline spend --
  // said explicitly rather than left for the two dollar figures to silently disagree (Cost/Money
  // carry the same clause).
  const periodComparedOn = estSpend != null && pcd && pcd.cur != null && Math.abs(pcd.cur - estSpend) > 1
    ? ` (on the ${fmtMoney(pcd.cur, 0)} priced in both periods; ${fmtMoney(estSpend - pcd.cur, 0)} not compared: no list price for part of it)` : "";
  const estDeltaText = pcd && pcd.dir ? fmtChangeVs(pcd.cur, pcd.prev, `prior ${effectiveDays}d`) : null;
  const estDelta = estDeltaText ? { direction: pcd!.dir!, text: `${estDeltaText}${periodComparedOn}` } : null;

  // Possible-waste split bar: up to the 3 largest priced sources, rest folded to "Other".
  const wasteSegments = topNWithOther(
    priced.map((w) => ({ name: wasteAreaShort(w), value: w.dollarUsd! })), 3, "Other"
  );
  const wasteSegMax = wasteSegments.reduce((s, e) => s + e.value, 0) || 1;
  const wasteSharePct = estSpend != null && estSpend > 0 && wasteTotalObj.usd > 0 ? (wasteTotalObj.usd / estSpend) * 100 : null;

  const jobsWasteEntry = worklist.find((w) => w.id === "lakeflow_failed_jobs_wasted_dbus");
  const jobsFootFacts = [
    jobsWasteEntry && jobsWasteEntry.isFlagged && jobsWasteEntry.dollarUsd != null
      ? { label: "Waste", value: fmtMoney(jobsWasteEntry.dollarUsd, 0), detail: "burned on failed runs", tone: "crit" } : null,
    { label: "Flagged", value: `${fmtInt(jobsSummary.flaggedCount)} of ${fmtInt(jobsSummary.total)}`, detail: "checks" },
  ].filter(Boolean);

  const queryWasteEntry = worklist.find((w) => w.area === "queries");
  const perfFootFacts = [queryWasteEntry && queryWasteEntry.isFlagged && queryWasteEntry.dollarUsd != null
    ? { label: "Waste", value: fmtMoney(queryWasteEntry.dollarUsd, 0), detail: "in failed statements", tone: "crit" }
    : { label: "Waste", value: "No cost estimate", tone: "muted" }];

  const estFacts = [
    { label: "Basis", value: estLabel(spendDiscountPct) },
    { label: "Note", value: EST_SPEND_CAVEAT },
    excludedNote ? { label: "Excluded", value: excludedNote, tone: "muted" } : null,
  ].filter(Boolean);
  const belowFloorNote = belowFloorNoteText(wasteTotalObj, filters.window);
  const wasteFacts = priced.length > 0
    ? [
        wasteSharePct != null ? { label: "Share", value: fmtPct(wasteSharePct, 0), detail: "of spend", tone: "crit" } : null,
        // Every segment the bar itself shows (up to 3, "Other" folds the rest) -- not just the
        // first 2, which silently dropped the 3rd priced source from the card (e.g. Jobs' $405).
        ...wasteSegments.slice(0, 3).map((s) => ({ label: s.name, value: fmtMoneyShort(s.value), tone: "crit" })),
        belowFloorNote ? { label: "Not counted", value: fmtMoney(wasteTotalObj.belowFloorUsd, 0), detail: "under each check's floor", tone: "muted" } : null,
      ].filter(Boolean)
    : flagged.length > 0
      ? [{ label: "Top", value: shortTitle(worklistTop.title), tone: "warn" }]
      : [{ label: "Status", value: wasteTile.reading, tone: wasteTile.severity === "ok" ? "ok" : "muted" }];
  const jobsFacts = [...(jobsTile.facts || []), ...jobsFootFacts];
  const perfFacts = [...(perfTile.facts || []), ...perfFootFacts];

  const sentence = verdictSentence({
    ready: spendAggReady, windowDays: effectiveDays, estSpend, pcd, worklistReady, priced, flagged,
    wasteTotalObj, worklistTop, uncertainWasteCount, wasteCheckCount, comparedOn: periodComparedOn,
  });
  React.useEffect(() => { if (setVerdict) setVerdict(sentence); }, [sentence, setVerdict]);


  return (
    <div>
        <p className="faint" style={{ fontSize: 12, margin: "0 0 10px" }}>
          Tag filter: spend, jobs, pipelines, compute and single queries follow it, most specific tag first (query, job,
          compute, workspace). Per-warehouse query numbers (Queries › Capacity, the query trend, top queries by $) follow
          only the warehouse's and workspace's tags, and the Tags page ignores it.{" "}
          <a {...guideLinkProps("how-tags")}>What it covers →</a>
        </p>
        <div className="ov-kpi-grid">
          {inRole("cost") && (
            <OverviewKpiCard
              label="Spend" value={estSpend != null ? fmtMoney(estSpend, 0) : spendAgg.phase === "loading" ? "..."
                : spendAgg.phase === "ready" && String(spendAgg.outcome).startsWith("ok_empty") ? fmtMoney(0, 0) : "-"} onClick={() => jump({ tab: "cost" })}
              chart={<React.Fragment><SummaryTileDelta delta={estDelta} />{daily && <SummaryTileSpark points={daily.values} />}</React.Fragment>}
              facts={estFacts}
            />
          )}

          {inRole("waste") && (
            <OverviewKpiCard
              label="Possible waste"
              value={!worklistReady ? "..." : priced.length > 0 ? fmtMoney(wasteTotalObj.usd, 0) : (flagged.length > 0 ? `${fmtInt(flagged.length)} flagged` : "0")}
              tone={worklistReady && flagged.length > 0 ? "crit" : undefined} onClick={() => jump(toWaste)}
              chart={wasteSegments.length > 0 && (
                <div className="ov-split-bar">
                  {wasteSegments.map((s, i) => (
                    <div key={s.name} className="ov-split-seg" style={{ width: `${(s.value / wasteSegMax) * 100}%`, opacity: 1 - i * 0.32 }} title={`${s.name}: ${fmtMoney(s.value, 0)}`} />
                  ))}
                </div>
              )}
              facts={worklistReady ? wasteFacts : null}
            />
          )}

          {inRole("jobs") && (
            <OverviewKpiCard label="Jobs & pipelines" value={jobsTile.value} unit={jobsTile.unit} tone={jobsTile.severity === "critical" ? "crit" : undefined} onClick={() => jump({ tab: "jobs" })} facts={jobsFacts} />
          )}

          {inRole("queries") && (
            <OverviewKpiCard label="Query performance" value={perfTile.value} unit={perfTile.unit} tone={perfTile.severity === "critical" ? "crit" : undefined} onClick={() => jump({ tab: "queries" })} facts={perfFacts} />
          )}
        </div>

        {inRole("cost") && <SpendMoversCard periodState={periodState} spend={estSpend} />}
        {inRole("cost") && (
          <ErrorBoundary>
            <SpendPathCard filters={filters} meta={meta} discountPct={spendDiscountPct}
              listUsd={spendAggReady ? numOrZero(spendAgg.data.total_value) : null}
              onTags={() => jump({ tab: "cost", subtab: "allocation" })} />
          </ErrorBoundary>
        )}

        <div className="ov-fix-grid">
          <FixFirstCard rows={roleFixRows} ready={worklistReady} onOpen={jump} onMore={() => jump({ tab: "waste" })} />
          <div className="ov-aside">
            <CoverageCard totalExecutable={totalExecutable} notAssessedCount={notAssessedCount} findings={findings} goTo={jump} />
            {inRole("cost") && (
              <AccountShapeCard rows={[
                { k: "Net DBUs", v: netDbus != null ? fmtInt(Math.round(netDbus)) : "-" },
                { k: "Serverless share", v: serverlessPct != null ? fmtPct(serverlessPct, 1) : "-" },
                { k: "Photon share", v: photonPct != null ? fmtPct(photonPct, 1) : "-" },
                { k: "No tags at all", v: untaggedPct != null ? fmtPct(untaggedPct, 1) : "-" },
                { k: "ML & AI spend", v: mlaiSpend != null ? `${fmtMoney(mlaiSpend, 0)}${mlaiSharePct != null ? ` · ${fmtPct(mlaiSharePct, 0)}` : ""}` : "-" },
                { k: "Workspaces", v: wsCount != null ? fmtInt(wsCount) : "-" },
              ]} />
            )}
          </div>
        </div>

        {role === "data_engineer" && (
          <ErrorBoundary>
            <JobStateCard filters={filters} meta={meta} dims={dims} />
            <QueueHeatCards filters={filters} meta={meta} dims={dims} only="week" />
          </ErrorBoundary>
        )}
        {inRole("cost") && (
          <div className="ov-money-grid">
            <ErrorBoundary>
            <div className="chart-card">
              <div className="chart-card-title-row">
                <div className="chart-card-title">{grain === "day" ? "Daily spend" : "Monthly spend"}</div>
                <GrainSwitch value={grain} onChange={setGrain} />
              </div>
              {grain === "month" ? (
                monthsForBars && monthsForBars.length ? <MonthChart months={monthsForBars} /> : <ChartNote state={monthAgg} label="cost_monthly_actuals" />
              ) : daily ? (
                <Columns
                  days={dailyDays}
                  series={[{ name: "Spend", values: dailyValues }]}
                  markers={markers}
                  partial={meta && meta.direct_export && meta.direct_export.includes_today ? [dailyDays[dailyDays.length - 1]] : null}
                  valueFmt={(v) => fmtMoney(v, 0)}
                  unitLabel="spend"
                  ariaLabel="Daily spend by day"
                  dollarAxis
                  note={daily.days.length > dailyDays.length ? `Latest ${fmtInt(dailyDays.length)} of ${fmtInt(daily.days.length)} days.` : null}
                />
              ) : <ChartNote state={dailyAgg} label="cost_dollarized_by_sku_day" />}
              {spikeCaption && grain === "day" && <div className="ov-kpi-caption" style={{ marginTop: 6 }}>{spikeCaption}</div>}
            </div>
            </ErrorBoundary>
            <ErrorBoundary>
            <div className="chart-card">
              <div className="chart-card-title">By product</div>
              {productEntries.length > 0 ? (
                <RankedBars items={productEntries} n={5} valueFmt={(v) => fmtMoney(v, 0)} unitLabel="Spend" color="var(--c1)" ariaLabel="Spend by product" />
              ) : <ChartNote state={mlaiAgg} label="cost_by_billing_origin_product" />}
              {productTotal > 0 && <div className="ov-kpi-caption faint">{`${fmtMoney(productTotal, 0)} across ${fmtInt(productEntries.length)} product${productEntries.length === 1 ? "" : "s"}, ${effectiveDays}d`}</div>}
              <button type="button" className="ov-link-btn" onClick={() => jump({ tab: "cost", subtab: "product" })}>{"By SKU and workspace in Cost →"}</button>
            </div>
            </ErrorBoundary>
          </div>
        )}

        {inRole("tags") && <ErrorBoundary><TagsOverviewCard filters={filters} onOpen={() => jump({ tab: "tags" })} /></ErrorBoundary>}
    </div>
  );
}

function OverviewContent(props: any) {
  return props.role === "governance" ? <GovernanceOverviewContent {...props} /> : <SpendOverviewContent {...props} />;
}

AreaContent.register("overview", null, OverviewContent);
