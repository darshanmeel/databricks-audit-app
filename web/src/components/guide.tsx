// the "How it works" page (hash tab=guide). Renders from GET /api/guide:
// home (rules, every tab at a glance, statuses), one page per tab, one per check, topics,
// troubleshooting, glossary and the check reference. Deep links go through nav_hash.ts's navHref:
// #tab=guide&focus=<query_id> (a check), how-<slug> (a topic), how-area-<key> (a tab page),
// how-troubleshooting, how-glossary, how-checks.

import React from "react";
import { createPortal } from "react-dom";

import { Api } from "../api";
import { fmtCell, fmtInt, fmtPct, fmtSnapshotWhen } from "../format";
import { wsPhrase } from "./names";
import { useDims, useFindingData } from "./hooks";
import { AREA_ORDER, AREA_REGISTRY, OVERVIEW_IDS, RANKED_QUERY_IDS, WASTE_IDS, WASTE_ITEMS, bandOf, homeForQuery } from "./tab_registry";
import { ROLES, ROLE_AREAS, ROLE_LABEL, roleAreasFor } from "./roles";
import { ACTION_TEMPLATES } from "./action_templates";
import { Badge } from "./primitives";
import { getCheckLabel } from "./labels";
import { columnMeta, planColumns } from "./columns";
import { actionTier } from "./overview_tile";
import { entityDisplayName } from "../tabs/tab_waste";
import { guideLinkProps, navHref } from "./nav_hash";
import { fetchGuideData, firstClause, splitFlagClauses, upperFirst } from "./guide_drawer";
import type { Affected, Filters, FindingSummary, LibraryCorrection, Meta } from "../types";
import type { Area, Home } from "./tab_registry";
import type { CheckLabel } from "./labels";
import type { PlannedColumn } from "./columns";

// For the nav search's keyboard hint -- Windows/Linux readers get "Ctrl+K", never "⌘K".
function isMac(): boolean {
  try { return /Mac|iPhone|iPad/.test((navigator as any).userAgentData?.platform || navigator.platform || ""); } catch (e) { return false; }
}

/** Which tags a check's tag filter follows. */
interface TagReach {
  kind: string;
  words: string[];
  reason: string | null;
}

/** One check as GET /api/guide describes it. */
interface GuideFinding {
  query_id: string;
  title: string;
  domain: string;
  tier: string;
  stars: boolean;
  origin: string | null;
  is_finding: boolean;
  windowed: boolean;
  period_kind: string;
  summary: string;
  summary_source: string;
  first_step: { text: string; full: string; tier: string | null } | null;
  read_this: string;
  healthy: string;
  investigate_if: string;
  severity_cap: string | null;
  actions: string[];
  not_assessed_reasons: string[];
  empty_if: string[];
  empty_if_words: string[];
  caveats: string;
  confidence: string;
  confidence_note: string;
  requires: string;
  params: { name: string; meaning: string | null; value: unknown; source: string }[];
  next: { query_id: string; title: string | null; if: string }[];
  reads: string[];
  docs: string[];
  columns: { name: string; kind: string | null }[] | null;
  money: { column: string } | null;
  library_corrections: LibraryCorrection[];
  tag_reach: TagReach | null;
}

/** A Databricks docs page for a table or concept, per cloud. */
interface GuideDoc {
  kind: string;
  title: string | null;
  what: string | null;
  enable: string | null;
  note: string | null;
  urls: Record<string, { url: string; aws_fallback: boolean }>;
}

/** The app-wide numbers the "How this app works" topics quote. */
interface GuideApp {
  as_of: string | null;
  direct_export: { as_of: string | null; [extra: string]: unknown } | null;
  window_options: number[];
  default_window: number;
  discount_pct: number;
  idle_gap_seconds: number | null;
  cloud: string;
  cloud_source: string;
  columns_available: boolean;
  default_enable: string;
  docs_error: string | null;
  materiality_floors_configured: number | null;
  mandatory_tag_keys: string[] | null;
  tag_share_floor: number | null;
  tag_coverage_floor: number | null;
}

/** GET /api/guide. */
export interface GuideData {
  app: GuideApp;
  docs: Record<string, GuideDoc>;
  sections: Record<string, string[]>;
  areas: Record<string, string[]>;
  findings: GuideFinding[];
}

/** The guide's checks by query_id. */
type GuideIndex = Record<string, GuideFinding>;

/** The filters a check page reads to fetch its example row. */
type GuideFilters = Pick<Filters, "window" | "workspaceIds" | "envs">;

/** One screen's Guide page text. `screen` items name a panel (t) or a sub-tab (sub). */
interface GuideTabInfo {
  lens: string;
  card?: string;
  lede: string;
  questions: string[];
  screen: { t?: string; sub?: string; d: string }[];
  measure: string;
  judge: string;
  price: string;
  troubles: { see: string; why: string; doit: string }[];
  limits: string[];
  tables?: string[];
  queries?: { id: string; title: string }[];
  extra?: { h?: string; items: string[] }[];
}

// this list's slugs, in this order, must equal app/api/guide.py's own GUIDE_SECTION_SLUGS.
const GUIDE_SECTIONS = [
  { slug: "flow", title: "Where the data comes from", teaser: "Your own credentials ran every check on Databricks; only the result rows are saved here. Nothing is sent anywhere else." },
  { slug: "window", title: "Windows and snapshots", teaser: "Most checks cover the last 7, 30 or 90 days up to the export, today partial. A snapshot check shows today's settings, not a period." },
  { slug: "status", title: "What each status means", teaser: "Critical and Warn are flagged. Only OK is a verified pass. Not assessed and No data never are." },
  { slug: "floors", title: "Materiality floors", teaser: "A tiny row is too small to matter: it reads OK instead of Critical or Warn, and every count already excludes it." },
  { slug: "money", title: "How dollars are worked out", teaser: "Usage × list price (effective). Never your invoice, and never your cloud VM, storage or network bill." },
  { slug: "steps", title: "Free, config and spend steps", teaser: "Free is a setting or a read. Config changes how something is configured. Spend costs money to carry out." },
  { slug: "tags", title: "Tags and chargeback", teaser: "Spend takes its most specific tag first: query, then compute, then workspace, then account. A workspace tag is read from its bill." },
  { slug: "names", title: "Names, not ids", teaser: "A row shows a name wherever one is known. When it can't be found, the row says why instead of guessing." },
  { slug: "coverage", title: "Coverage and gaps", teaser: "An empty panel means couldn't look or nothing happened -- never all clear. Coverage & Gaps says which." },
  { slug: "coverage-data-sources", title: "Coverage & Gaps: Data sources", teaser: "Which system tables this audit could read, which came back empty, and which need a grant." },
  { slug: "coverage-couldnt-check", title: "Coverage & Gaps: Not assessed", teaser: "Not assessed, a read error, nothing in the window, or excluded by your filters -- never a pass." },
  { slug: "coverage-known-limitations", title: "Coverage & Gaps: Known limitations", teaser: "Fixed modelling choices that change how to read a number elsewhere in this app." },
];

// The Coverage & Gaps sub-topics apply to that one screen, not every screen -- "Start here" shows
// them nested under the "Coverage and gaps" topic instead of counted alongside the universal ones.
const GUIDE_UNIVERSAL_SECTIONS = GUIDE_SECTIONS.filter((s) => !s.slug.startsWith("coverage-"));
const GUIDE_COVERAGE_SUBSECTIONS = GUIDE_SECTIONS.filter((s) => s.slug.startsWith("coverage-"));

const CLOUD_OPTIONS = ["aws", "azure", "gcp"];
// AWS and GCP are acronyms (all caps reads right); Azure is a proper name, never "AZURE".
const CLOUD_WORD: Record<string, string> = { aws: "AWS", azure: "Azure", gcp: "GCP" };
const GUIDE_CLOUD_STORAGE_KEY = "guideCloudOverride";

// Every screen with a Guide page, in sidebar order (All findings is the same checks as one list).
// "money" is the executive overview: the CFO's only page and the CTO's Overview.
const GUIDE_SCREEN_ORDER = AREA_ORDER.filter((k) => k !== "findings").flatMap((k) => (k === "actions" ? [k, "money"] : [k]));
const MONEY_CHECK_IDS = [
  "cost_period_over_period", "cost_dollarized_by_sku_day", "cost_monthly_actuals", "cost_chargeback_by_tag_value",
  "cost_chargeback_by_warehouse", "cost_chargeback_by_cluster", "cost_unnamed_workspaces", "overview_serverless_classic_split",
  "query_failed_queries_daily", "query_queuing_waits", "lakeflow_job_reliability", "lakeflow_job_duration_regression",
];
function guideArea(k: string): Area | null {
  if (k === "money") return { label: "Overview (CFO, CTO)", subtabs: [], scope: "" };
  return AREA_REGISTRY[k] || null;
}
// Only the waste sources with a dollar column are waste; the rest are flagged, not priced.
function isPricedWaste(id: string): boolean { return WASTE_ITEMS.some((w) => w.id === id && w.dollarCol); }

// One entry per screen: the "Every tab at a glance" row on Guide home and that screen's own page.
// `screen` items without a `sub` describe a panel; with one, they name the sub-tab they describe.
const GUIDE_TABS: Record<string, GuideTabInfo> = {
  overview: {
    lens: "FinOps · Data engineer · Governance",
    card: "Spend, what moved it, possible waste, what to open first.",
    lede: "Spend for the window, what moved it, possible waste, and the few checks worth opening first.",
    questions: [
      "What did we spend, and is it rising?",
      "Which workspaces moved the total?",
      "What should someone open first?",
    ],
    screen: [
      { t: "Spend and possible waste", d: "Two numbers for the window, each against the period before." },
      { t: "What moved spend", d: "The previous total, the workspaces that added or removed the most, and the current total." },
      { t: "Fix these first", d: "Flagged checks ranked by money, then severity." },
      { t: "Checks that ran", d: "How many of this lens's checks ran: the same count as the top bar." },
      { t: "Daily spend and by product", d: "The trend by day and the split by product." },
      { t: "Mandatory tags", d: "How many queries, jobs, clusters, warehouses and workspaces miss a mandatory tag: on their own tag, after compute tags, after workspace tags." },
    ],
    measure: "Net DBUs per day, workspace and SKU; the period before is the same number of days just before.",
    judge: "Nothing here is judged itself; each card shows the headline of the check behind it.",
    price: "Usage × list price (effective) for every billed unit.",
    troubles: [
      { see: "A change reads 6.2× or \"new\"", why: "The previous period was near zero or empty.", doit: "Open What moved spend to see which workspace started." },
      { see: "Spend is higher than Cost › By resource", why: "Account-level SKUs have no cluster or warehouse.", doit: "Compare at the workspace level." },
      { see: "Health looks calm", why: "Workspaces outside the export region show spend only.", doit: "Read the region banner; see Coverage & Gaps." },
    ],
    limits: [
      "Monthly bars are calendar months; the cards use the rolling window.",
    ],
  },
  actions: {
    lens: "Everyone",
    card: "Flagged checks as fixes, with resources and money freed.",
    lede: "The flagged checks as a short list of fixes, each with the resources it touches and the money it frees.",
    questions: [
      "What should we fix first?",
      "How much possible waste does each fix remove?",
      "Which security risks need a change?",
    ],
    screen: [
      { t: "Four numbers", d: "Possible waste, security risks, fixes not priced, and flagged checks with no fix template yet." },
      { t: "Kinds", d: "Savings, risks, policies and hygiene." },
      { t: "Fix list", d: "One row per fix: how many resources and the money or severity." },
      { t: "Fix detail", d: "Impact, effort, how to fix it in Databricks, the worst resources, and the checks behind it." },
    ],
    measure: "Every flagged row of a check maps to one fix template; rows for the same fix merge into one fix.",
    judge: "A fix exists only while a check behind it reads Critical or Warn.",
    price: "The same possible-waste dollars as Waste & savings; risks carry the check's severity instead.",
    troubles: [
      { see: "A fix shows no dollars", why: "Its checks have no priced column, e.g. pool floors bill on your cloud VM.", doit: "Rank it by severity and effort." },
      { see: "A fix you made still shows", why: "The window still holds days from before the change.", doit: "Wait until those days leave the window, then re-run." },
      { see: "Other flagged checks", why: "Those checks have no fix template yet.", doit: "Open them in All findings." },
    ],
    limits: [
      "Fixes and their how-to are templates; check the setting in Databricks before you change it.",
    ],
  },
  money: {
    lens: "CFO · CTO",
    lede: "Spend, possible waste and untagged spend for the window, what moved it, and the products, workspaces and resources behind it.",
    questions: [
      "What did we spend, and is it rising?",
      "How much is possible waste, and how much has no cost center?",
      "Which products, workspaces and resources drive it?",
    ],
    screen: [
      { t: "Four numbers", d: "Spend, possible waste, spend with no cost center, and unnamed workspaces." },
      { t: "What moved spend", d: "The previous total, the workspaces that added or removed the most, and the current total." },
      { t: "Spend by month and environment", d: "The last 12 calendar months, and each environment's top workspaces." },
      { t: "Top cost drivers", d: "Each product's total for the window as bars, split by environment in the table below; by workspace; and the top 10 warehouses and clusters." },
      { t: "Spend by cost center", d: "Tagged spend against untagged spend." },
      { t: "Unused tables", d: "Monthly cost of flagged unused tables, not counted in possible waste." },
      { t: "Performance", d: "CTO lens only: failed queries, queue time added up and by the clock, job reliability and jobs getting slower." },
    ],
    measure: "Net DBUs per day, workspace and SKU; the period before is the same number of days just before.",
    judge: "Nothing on this page is judged; each card adds up the checks behind it.",
    price: "Usage × list price (effective), less your what-if discount.",
    troubles: [
      { see: "A change reads 6.2× or \"new\"", why: "The previous period was near zero or empty.", doit: "Open What moved spend to see which workspace started." },
      { see: "Change shown on less than total spend", why: "Only rows priced in both periods are compared; unpriced rows stay in the total.", doit: "See Cost › Pricing & policy." },
      { see: "No cost center is most of the spend", why: "Compute was created without the cost-center tag.", doit: "Enforce the tag with a cluster or budget policy." },
      { see: "Possible waste is lower than expected", why: "Only priced waste adds up; unpriced checks are counted, not summed.", doit: "See Waste & savings › Flagged, not priced." },
    ],
    limits: [
      "Monthly bars are calendar months; the cards use the rolling window.",
      "Dollars are list price, never your invoice or your cloud VM bill.",
    ],
  },
  cost: {
    lens: "FinOps · finance",
    card: "Spend trend, product and SKU split, who spent it, untagged spend.",
    lede: "Every dollar in the window: the trend, the product and SKU split, who spent it, and what can't be charged back.",
    questions: [
      "Which workspace, team or tag spent the most?",
      "How much spend can't be charged back?",
      "Which SKUs grew fastest?",
    ],
    screen: [
      { sub: "trend", d: "Spend by day/month with spikes marked; Spend / Change by product, workspace, warehouse, cluster or job; top 5 stacked, all in a table, one picked." },
      { sub: "product", d: "Spend by product and SKU, the SKUs that grew, and serverless or Photon premiums." },
      { sub: "allocation", d: "The share of spend each tag or policy reaches, where each dollar lands, and each workspace's tags: which count and why the others don't." },
      { sub: "chargeback", d: "This period vs the last, by workspace, tag, warehouse, job, cluster or identity; By tag value lists only workspaces with tagged spend." },
      { sub: "resource", d: "Spend by cluster, warehouse, pool, job and notebook." },
      { sub: "before_after", d: "Pick a fix day: each resource's average before vs after ($, DBUs, time, runs, failures, queue, start-up, spill), daily chart, and the drivers of the change." },
      { sub: "pricing", d: "Usage-policy and tag coverage, effective price by SKU, restatements, egress and cloud infra." },
    ],
    measure: "Usage rows joined to the list price valid for that SKU and time.",
    judge: "Spend cuts are Ranked; coverage and spike checks flag against config/thresholds.yml.",
    price: "Usage × list price (effective); unpriced SKU-days are listed, never added.",
    troubles: [
      { see: "Most spend is untagged", why: "Compute was created without the cost-center tag; account-level SKUs carry no workspace.", doit: "Enforce the tag with a cluster or budget policy." },
      { see: "A SKU is missing from the total", why: "No list price for that SKU-day.", doit: "It's listed under Pricing & policy; no dollars are guessed." },
      { see: "Last month changed", why: "Billing restated earlier usage.", doit: "Open the restatement check for the share." },
      { see: "Workspace tag doesn't reach its compute", why: "Under half of classic usage carries it, or no value dominates.", doit: "Tag warehouses and clusters directly (Allocation › Workspace tags)." },
      { see: "Per-query dollars are below the warehouse", why: "Idle warehouse time isn't attributed to any query.", doit: "Charge back at the warehouse level." },
    ],
    limits: [
      "No negotiated-rate table: dollars are list price, less your what-if discount.",
      "All-purpose clusters are charged to the cluster's tag, not per query.",
    ],
  },
  waste: {
    lens: "FinOps · platform",
    card: "Possible waste from 5 sources, one total, top resources.",
    lede: "Possible waste from five sources, one total, and the resources that make most of it.",
    questions: [
      "How much are we paying for idle or failed work?",
      "Which resources make most of it?",
      "What is flagged but can't be priced?",
    ],
    screen: [
      { sub: "priced", d: "The total and the ranked worklist of resources, in dollars." },
      { sub: "unpriced", d: "Checks that flag waste with no dollar source." },
      { sub: "method", d: "Which checks count as priced waste, and which are flagged but not priced." },
    ],
    measure: "Idle warehouse minutes, idle cluster nodes, failed job runs, failed SQL statements, endpoints billed with no requests.",
    judge: "Rows below each check's floor read OK and stay out of the total.",
    price: "Only the idle or failed share of each resource's DBUs × list price. The rest of its bill is never waste.",
    troubles: [
      { see: "Waste is $0 but checks flag", why: "Those checks have no priced column.", doit: "See Flagged, not priced." },
      { see: "A small resource is missing", why: "It is under that check's materiality floor.", doit: "Priced says how much the floors leave out; config/materiality.yml holds each floor." },
    ],
    limits: [
      "A floor, not a forecast: it prices what was measured, never what might happen.",
      "Stopping a classic or pro warehouse empties its disk cache; serverless keeps the result cache 24 h.",
    ],
  },
  mlai: {
    lens: "ML platform",
    card: "Serving and vector-search spend, idle endpoints, AI Gateway.",
    lede: "Serving and vector-search spend, endpoints billed with no traffic, and AI Gateway usage.",
    questions: [
      "What does each endpoint cost?",
      "Which endpoints are billed with no requests?",
      "Is the gateway rejecting calls?",
    ],
    screen: [
      { sub: "spend", d: "Serving, vector search and model spend by endpoint and mode." },
      { sub: "endpoints", d: "Endpoints with no recent traffic, idle first." },
      { sub: "gateway", d: "AI Gateway requests, errors and rate limits." },
    ],
    measure: "Serving spend by endpoint; requests from endpoint usage joined to served entities.",
    judge: "An endpoint billed in the window with zero requests is flagged.",
    price: "Serving DBUs × list price; an endpoint with no price reads unpriced.",
    troubles: [
      { see: "0 requests but real users call it", why: "Usage tracking is off on that endpoint.", doit: "Enable usage tracking, then re-run." },
      { see: "Not assessed on a billed endpoint", why: "It bills but isn't a tracked serving endpoint.", doit: "Confirm in the endpoint's own metrics." },
    ],
    limits: [
      "Provisioned throughput bills through idle hours, so cost per request can look huge on quiet endpoints.",
    ],
  },
  genie: {
    lens: "FinOps · ML platform · data engineering",
    card: "Genie spend by kind of use, entry point, user and workspace.",
    lede: "Genie spend from the bill, split three ways: by kind of use, by how Genie was reached, and by user and workspace.",
    questions: [
      "How much does Genie cost, and is it growing?",
      "Is Genie built into apps and agents, or used by people asking it for code and answers?",
      "Which users and workspaces use it most?",
    ],
    screen: [
      { t: "Kind of use", d: "Genie Code (writes SQL/Python, a person runs it) · Genie agents, formerly spaces (answer and run SQL) · Genie One (chat)." },
      { t: "Built in or in the browser", d: "Built in = channel not UI, or run as a service principal; In the browser = UI channel and a person. Other channels show as \"Non-UI · <value>\"." },
      { t: "Who, where and how", d: "Each user or service principal by workspace, env, kind and channel, with DBUs, $ and the change against the period before." },
      { t: "Workspaces, agents and days", d: "Spend by workspace with its env, the top Genie agents, and spend per day." },
    ],
    measure: "GENIE billing rows: surface, channel, agent id and run-as identity; the period before is the same length.",
    judge: "Nothing on this page is judged.",
    price: "DBUs × list price (effective). A free-tier SKU is a real $0; a SKU with no price adds nothing and is counted apart.",
    troubles: [
      { see: "No Genie rows but Genie is in use", why: "The export is older than the Genie tab, or the billing table has no Genie fields yet.", doit: "Re-run the export; check Coverage & Gaps if the check did not run." },
      { see: "Much of the spend reads Not recorded", why: "Older billing rows carry no Genie fields.", doit: "Newer rows fill in; compare a shorter window." },
      { see: "A change reads not comparable", why: "The export's billing history doesn't reach back to the period before.", doit: "Export a longer history or pick a shorter window." },
    ],
    limits: [
      "Genie agents have ids, not names, in the system tables.",
      "Dollars are list price; free DBUs per named user and promotions are taken off only where the bill marks them free.",
      "The SQL Genie agents run on warehouses is on Queries › Heavy queries (source Genie).",
    ],
  },
  compute: {
    lens: "Platform",
    card: "Idle warehouses and clusters, auto-stop, autoscale churn, config risk.",
    lede: "Idle time on SQL warehouses and classic clusters, auto-stop and autoscale churn, and configuration risk.",
    questions: [
      "Which warehouses run while nobody queries them, and what does that cost?",
      "Which clusters sit idle or run without a policy?",
      "Is a warehouse too small at peak? (Queries › Capacity)",
    ],
    screen: [
      { sub: "warehouses", d: "Running time split into busy, idle and waiting to auto-stop, with the idle dollars; warehouses that stop well after their auto-stop setting." },
      { sub: "clusters", d: "Idle nodes, node use and pools held warm." },
      { sub: "config", d: "Auto-stop, auto-terminate, policy and runtime risks." },
    ],
    measure: "Start and stop events give running time; statements and node CPU give busy time. Gaps over the idle limit are idle.",
    judge: "Limits live in config/thresholds.yml, e.g. the idle gap and the auto-stop limit.",
    price: "Only the idle share × the resource's DBU rate × list price.",
    troubles: [
      { see: "A warehouse is 100% idle", why: "Auto-stop is off, or its queries run in a workspace outside the export region.", doit: "Check auto-stop; read the region banner." },
      { see: "Idle time is high but idle cost is $0", why: "Its SKU has no list price in the window.", doit: "See Cost › Pricing & policy." },
      { see: "Access mode not recorded", why: "Older clusters leave the field empty.", doit: "Set an access mode on the cluster." },
    ],
    limits: [
      "Pool floors bill on your cloud VM, so they rank by instance count, not dollars.",
    ],
  },
  jobs: {
    lens: "Data engineering",
    card: "Failed and slow runs, their cost, hygiene, wrong compute, pipelines.",
    lede: "Failed and slow runs, what they cost, hygiene gaps, jobs on the wrong compute, and pipelines.",
    questions: [
      "Which jobs are failing, and why?",
      "Which jobs got slower?",
      "Which jobs run on all-purpose compute?",
    ],
    screen: [
      { sub: "failures", d: "Daily state per job and pipeline; failed runs by cause, top code's share, and DBUs burned." },
      { sub: "slow", d: "Run time ranking (top 20/50, CSV); jobs 1.5x slower in the last 7 days; queue time; cold starts; tasks failed while the cluster started." },
      { sub: "hygiene", d: "Retries, stale jobs, no timeout, no health rule, no owner, and jobs on all-purpose clusters." },
      { sub: "compute_fit", d: "Compute pressure, task cluster use and oversized jobs." },
      { sub: "pipelines", d: "Pipeline failures, retries and idle tails." },
    ],
    measure: "Runs from the job run timeline and tasks; cost joins each run to its billed usage.",
    judge: "Failure rate, slowdown and timeout rules from config/thresholds.yml.",
    price: "Failed runs' DBUs × list price count as possible waste.",
    troubles: [
      { see: "A slow job isn't flagged", why: "It needs enough successful runs before and after.", doit: "Widen the window to 90 days." },
      { see: "Many failures under Execution errors", why: "Unknown termination codes count as Execution errors.", doit: "Open the run in Databricks for the real message." },
      { see: "Run succeeded but a task failed", why: "That's flagged by its own check.", doit: "Check task retries." },
      { see: "A lightly used job isn't called oversized", why: "Its busiest memory minute was too high for a smaller machine, or a worker swapped.", doit: "Read Memory p90 / peak in the check's rows." },
    ],
    limits: [
      "Queue time is measured for single-task jobs only; cold start covers every successful task run.",
      "Machine use is recorded as one-minute averages, so a memory spike of a few seconds can be missed.",
      "Spill isn't recorded for job clusters; swap is the sign used instead. Watch the next few runs after downsizing.",
    ],
  },
  queries: {
    lens: "Data and analytics engineering",
    lede: "The costliest SQL, inefficient shapes, failures, and warehouse capacity.",
    questions: [
      "Which queries cost the most?",
      "Which ones spill, shuffle or scan too much?",
      "Are warehouses queuing?",
    ],
    screen: [
      { sub: "heavy", d: "Query groups ranked by estimated cost, with the files each read out of all its filters could have skipped." },
      { sub: "efficiency", d: "Spill, shuffle, pruning and cache reuse, worst warehouses first." },
      { sub: "reliability", d: "Failed statements and what they burned." },
      { sub: "capacity", d: "Queue waits, warehouse pressure, share of queries that waited by hour (UTC) and by warehouse-day, and result fetch time." },
      { sub: "trend", d: "One warehouse measure by day or month (spill, shuffle, waits, cache, time, counts, failures), all and per warehouse, plus a top-10 grid." },
      { sub: "team", d: "Cost by the tag or user behind each query." },
    ],
    measure: "Warehouse bill split across statements by run time.",
    judge: "Spill, shuffle and queue limits from config/thresholds.yml.",
    price: "Failed statements' share of warehouse DBUs × list price counts as possible waste.",
    troubles: [
      { see: "One huge redacted query", why: "Hidden statement text pools into one shape.", doit: "Read it per warehouse." },
      { see: "Query dollars below the warehouse bill", why: "Idle time isn't attributed to any statement.", doit: "Use Compute › Warehouses for idle." },
    ],
    limits: [
      "Per-query dollars are an estimate from run time, not a billed amount.",
    ],
    extra: [{ items: [
      "Provisioning wait, summed: every query's wait added up (100 queries × 5 s = 500 s).",
      "Provisioning wait, clock: time at least one query waited (5 s).",
      "Warehouse starts: STARTING→RUNNING from system.compute.warehouse_events (count, time, longest, aborted).",
    ] }],
  },
  governance: {
    lens: "Security · data governance",
    card: "Grants, admin activity, sensitive-data reads, lineage, sharing.",
    lede: "Grants, admin activity, sensitive-data reads, lineage and sharing across the metastore.",
    questions: [
      "Who has broad access?",
      "Is sensitive data masked, and who read it unmasked?",
      "Where does sensitive data spread or leave?",
    ],
    screen: [
      { sub: "access", d: "Broad grants, grants inventory, run-as escalation, sign-ins and network denials." },
      { sub: "admin", d: "Admin and permission changes, including who got token permission." },
      { sub: "sensitive", d: "Classified columns, masks and unmasked reads." },
      { sub: "lineage", d: "How far sensitive tables reach downstream." },
      { sub: "sharing", d: "Delta Sharing recipients and exposure." },
    ],
    measure: "Grants, tags, masks and shares from information_schema; reads from lineage; events from the audit log.",
    judge: "Broad grantees and sensitive reads flag against config/thresholds.yml.",
    price: "Not priced; risks carry a severity.",
    troubles: [
      { see: "The workspace filter changes some panels but not others", why: "Grants, tags, masks, volumes, views and shares are metastore-wide; audit-log and lineage-event checks carry a workspace.", doit: "The page's scope note says which." },
      { see: "Classification checks are empty", why: "Data classification isn't enabled.", doit: "Enable it in Catalog Explorer, then re-run." },
      { see: "A masked column is still read", why: "Reads come from lineage whether or not the mask applied.", doit: "Use the masks inventory to confirm." },
    ],
    limits: [
      "It sees only objects the export identity can see.",
    ],
  },
  storage: {
    lens: "Platform",
    lede: "Table inventory, growth, unused tables, and maintenance cost and failures.",
    questions: [
      "Which tables grow fastest, and which look unused?",
      "Is predictive optimization working?",
      "What does maintenance cost?",
    ],
    screen: [
      { sub: "tables", d: "Types, growth, dead-table candidates, and tables nobody read in the window: still written to, or not touched at all." },
      { sub: "maintenance", d: "Vacuum, clustering and optimization runs, their cost and failures." },
    ],
    measure: "Table metadata and metrics history; maintenance runs from predictive optimization history.",
    judge: "No reads or writes in lineage flags a table as unused.",
    price: "Not priced: maintenance in DBU only (no SKU in the history); storage bytes bill on your cloud.",
    troubles: [
      { see: "A used table reads unused", why: "It's read outside Unity Catalog lineage.", doit: "Confirm before you drop it." },
    ],
    limits: [
      "Table checks cover the whole metastore; maintenance cost follows the workspace filter.",
    ],
  },
  tags: {
    lens: "FinOps · governance · data engineering",
    card: "What misses the mandatory tags (up to 5), level by level.",
    lede: "Which objects miss a mandatory tag (up to 5 keys), checked from the object out to its workspace.",
    questions: [
      "How many objects miss a mandatory tag?",
      "Where does each tag come from?",
      "Where do untagged queries come from, and does their compute cover them?",
    ],
    screen: [
      { t: "One number per type", d: "The share of each object type that misses a mandatory tag at every level." },
      { t: "One card per type", d: "Per type: missing at least one tag / missing all, on the object alone and at every level; per tag: own, from compute or workspace, missing everywhere." },
      { t: "Where queries come from", d: "Queries by origin (SQL editor, dashboards, Genie, jobs, notebooks, tools): how many carry no query tag, and how many miss the tag at every level." },
      { t: "What to tag", d: "Objects missing a tag at every level; queries grouped by warehouse or cluster, with their origin." },
      { t: "Workspace tags", d: "Each workspace's tags, which count, and why the others don't." },
    ],
    measure: "Most specific level first, then outward.",
    judge: "Key present with any value.",
    price: "Not priced; the counts are queries or objects.",
    troubles: [
      { see: "Every query misses its own tags", why: "Few teams set query tags; they are set per session or per query. SQL editor queries rarely carry any.", doit: "Read Where queries come from: tag the warehouses those queries run on." },
      { see: "A warehouse with no tag of its own reads Workspace, though its bill carries the tag", why: "The bill's value is the workspace's own tag, which Azure copies onto every bill row.", doit: "Tag the warehouse itself if it should differ from the workspace." },
      { see: "A tagged workspace doesn't cover its jobs", why: "The tag is on under half of its classic compute usage, or its values are mixed.", doit: "See Workspace tags for the share; tag the jobs and compute directly." },
      { see: "Queries read Not in this export", why: "The export is older than the Tags page.", doit: "Re-run the export." },
      { see: "An object is tagged one way and billed another", why: "Its own tag and its bill's differ, often because a usage policy sets the bill's.", doit: "The cost follows the bill: change the tag or the policy." },
      { see: "An object reads last billed <date>", why: "It has had no bill for over a week, so its tags are from that day.", doit: "Delete it if it is no longer used." },
    ],
    limits: [
      "Workspace tags come from the bill: Databricks has no table that lists them.",
      "Cost uses each day's bill.",
      "Performance uses each object's latest tags for the whole window; both use the workspace tag valid that day.",
      "Set the keys in settings: mandatory_tag_keys.",
      "Serverless notebooks are counted only when they ran queries in the window; notebooks carry no tags of their own.",
      "Deleted jobs, clusters and warehouses are left out only on exports that record delete times.",
    ],
    extra: [
      { h: "Measure: most specific level first, then outward", items: [
        "Queries: query tag → job/pipeline → warehouse or cluster (own tags or bill) → workspace.",
        "Jobs and pipelines: own tags → bill → workspace.",
        "Serverless notebooks (ran queries in the window): usage-policy tags on the bill → workspace.",
        "Clusters and warehouses: own tags or bill → workspace.",
        "A bill tag counts on the latest billed day; on Azure only if it differs from the workspace value.",
        "Deleted objects are left out.",
      ] },
      { h: "Judge: key present with any value", items: [
        "Case, spaces, _ and - are ignored.",
        "Found at several levels → credited to the most specific.",
        "Bill value = workspace value → the workspace's tag (Azure copies it to every row).",
        "Value set by a usage policy → the policy's.",
      ] },
    ],
    tables: ["system.query.history", "system.lakeflow.jobs", "system.compute.warehouses", "system.compute.clusters", "system.billing.usage"],
    queries: [
      { id: "tags_missing_objects", title: "Jobs, warehouses and clusters missing a mandatory tag (own tags)" },
      { id: "tags_missing_queries", title: "Queries missing each mandatory tag, by warehouse or cluster" },
      { id: "tags_on_workspaces", title: "Every tag on each workspace's billed usage" },
    ],
  },
  coverage: {
    lens: "Everyone",
    card: "Readable tables, checks that couldn't run, fixed limits.",
    lede: "Which system tables were readable, which checks couldn't run and why, and the fixed limits of this audit.",
    questions: [
      "Which checks ran?",
      "Which tables were missing or empty?",
      "What can't this audit see at all?",
    ],
    screen: [
      { sub: "sources", d: "Every system table: fresh, empty or missing, and the grant it needs." },
      { sub: "couldnt", d: "Checks that couldn't run, with the reason." },
      { sub: "limits", d: "Modelling choices that change how to read a number." },
    ],
    measure: "Each system table is checked for fresh, gap-free rows; the build log records checks that failed.",
    judge: "A table is fresh, empty or missing.",
    price: "Not priced.",
    troubles: [
      { see: "A table is missing", why: "The schema isn't enabled or the grant wasn't given.", doit: "Enable the system schema; grant USE and SELECT." },
      { see: "A table is empty", why: "Nothing happened, or the feature isn't in use.", doit: "Not a verified zero." },
    ],
    limits: [
      "The export region decides which workspaces are assessed.",
    ],
  },
};


function guideSearchText(f: GuideFinding, label: CheckLabel): string {
  const parts = [
    label.title, label.why, f.title, f.query_id, f.summary, f.read_this, f.investigate_if,
    ...(f.reads || []), ...(f.docs || []),
    ...((f.columns || []).map((c) => c.name)),
    ...((f.library_corrections || []).map((c) => `${c.problem} ${c.effect} ${c.fix}`)),
  ];
  return parts.filter(Boolean).join(" ").toLowerCase();
}

function splitSentences(text: string | null | undefined): string[] {
  if (!text) return [];
  const trimmed = String(text).trim();
  if (!trimmed || trimmed.toLowerCase().startsWith("n/a")) return [];
  return trimmed.replace(/\b(e\.g|i\.e|etc|vs)\./g, "$1․").split(/(?<=[.!?])\s+(?![^()]*\))/).map((x) => x.replace(/․/g, ".")).filter(Boolean);
}

const ANYWHERE_TIER_BRACKET_RE = /\(([^()]*(?:free|config|spend)[^()]*)\)/i;
const TRAILING_TIER_BRACKET_RE = /\s*\(([^()]*)\)\s*[;.]?\s*$/;
// A rung's tier comes only from a bracket that names free/config/spend -- never a bare word
// match on the whole sentence, which would tag "does not itself justify new spend" as SPEND.
function rungTier(action: string): string | null {
  const m = String(action || "").match(ANYWHERE_TIER_BRACKET_RE);
  return m ? actionTier(m[1]) : null;
}
// The chip already says the tier, so a trailing tier bracket is stripped from the shown text.
function rungText(action: string): string {
  const full = String(action || "").trim();
  const trailing = full.match(TRAILING_TIER_BRACKET_RE);
  return trailing && actionTier(trailing[1]) ? full.slice(0, trailing.index).trim() : full;
}

// A header's free-text fields (read_this/healthy/investigate_if/actions, already id-/TRUE-FALSE-
// cleaned server-side by app/api/guide.py's _clean_prose) still name real column identifiers --
// "counted_idle_minutes", not English words. A reference page keeps those in code font, verbatim,
// so a reader can match one straight to the Columns table below, rather than guessing a rewording.
const PROSE_SNAKE_RE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g;
function codeifyProse(text: string | null | undefined): React.ReactNode[] | null {
  if (!text) return null;
  const s = String(text);
  const nodes: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  const re = new RegExp(PROSE_SNAKE_RE);
  // eslint-disable-next-line no-cond-assign
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) nodes.push(s.slice(last, m.index));
    nodes.push(<code key={m.index}>{m[0]}</code>);
    last = m.index + m[0].length;
  }
  if (last < s.length) nodes.push(s.slice(last));
  return nodes;
}

// Plain-string sibling of codeifyProse, for the few spots that need a string, not JSX nodes --
// an HTML attribute (title=) can't hold an array of <code> elements.
function desnakeProse(text: string | null | undefined): string | null | undefined {
  if (!text) return text;
  return String(text).replace(PROSE_SNAKE_RE, (m) => m.replace(/_/g, " "));
}

// healthy/investigate_if lead with a "status = OK - " (or WARN/CRITICAL) prefix that only
// repeats the Badge already shown beside the text, and resolve_params() (service.py) writes a
// threshold as "30 (default) percent" -- a number, unit and the word "percent" spelled out
// instead of "30%". Both are plain string shape, not new wording, so this stays a client-side
// format pass rather than a backend change. Runs before codeifyProse, which handles snake_case.
const STATUS_PREFIX_RE = /^status\s*=\s*[A-Z_]+(?:\s+or\s+[A-Z_]+)?\s*-?\s*/i;
const DEFAULT_PERCENT_RE = /(\d+(?:\.\d+)?)\s*\(default\)\s*percent/gi;
// The tab table's short form: one bullet per level, first sentence only; the check page has the rest.
function shortFlagBullets(f: GuideFinding): { label: string; text: string }[] {
  // A capped check never reads Critical, so its Critical rule is the top Warn band.
  const label = f.severity_cap === "WARN"
    ? { WARN: "Warn", CRITICAL: "Warn (top band)", BOTH: "Warn" }
    : { WARN: "Warn", CRITICAL: "Critical", BOTH: "Flags" };
  const out: { label: string; text: string }[] = splitFlagClauses(f.investigate_if || "")
    .map((c) => ({ label: label[c.level], text: firstClause(c.body) }))
    .filter((b) => b.text.length >= 12 && !/^(or\s|\()/i.test(b.text))
    .map((b) => ({ label: b.label, text: upperFirst(b.text) }));
  if (!out.length && f.investigate_if) {
    const t = firstClause(f.investigate_if.replace(STATUS_PREFIX_RE, ""));
    if (t) out.push({ label: "Flags", text: upperFirst(t) });
  }
  return out;
}

// Verdict colour class for a bullet label: Critical/Warn/problem words bad, OK/pass good.
function verdictCls(label: string): string {
  if (/^critical/i.test(label)) return "gd-v-crit";
  if (/^(warn|flags|not |missing|failed|read error|untagged)/i.test(label)) return "gd-v-warn";
  if (/^(ok|pass|counted|saved|covered)/i.test(label)) return "gd-v-ok";
  return "";
}

// Short label for a param: its name plus the unit the name carries.
const PARAM_UNITS: [RegExp, string][] = [
  [/_pct$/, "%"], [/_usd$/, "$"], [/_gb$/, "GB"], [/_mb$/, "MB"], [/_seconds$/, "s"], [/_minutes$/, "min"], [/_hours$/, "h"], [/_days$/, "days"],
];
function paramLabel(name: string): string {
  const base = humanizeParamName(name.replace(/_(pct|usd|gb|mb|seconds|minutes|hours|days)$/, ""));
  const u = PARAM_UNITS.find(([re]) => re.test(name));
  return u ? `${base}, ${u[1]}` : base;
}

// "SKEWED: ...; WAITING: ..." splits into one piece per upper-case lead word.
function splitRungCases(text: string): string[] {
  return String(text || "").split(/;\s+(?=[A-Z_]{3,}[\s:(-])/).filter(Boolean);
}
function boldLead(text: string): React.ReactNode {
  const m = /^([A-Z_]{3,})\b(.*)$/s.exec(text);
  return m ? <React.Fragment><b>{m[1]}</b>{codeifyProse(m[2])}</React.Fragment> : codeifyProse(text);
}
// First clause of a long sentence: cut at " - " or ";", then at 25 words.
function shortClause(text: string): string {
  const t = String(text || "").split(/;\s|\s-\s/)[0].replace(/[\s;.,]+$/, "");
  const w = t.split(/\s+/);
  return w.length > 25 ? `${w.slice(0, 25).join(" ")}…` : t;
}

// A stat box's own label -- short, never the full param.meaning sentence (that goes in its title
// attribute instead).
function humanizeParamName(name: string): string {
  const s = String(name || "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// "(default)" after every threshold is noise; one closing note says where they are set.
function cleanFlagPrefix(text: string): string {
  if (!text) return text;
  let s = String(text).replace(STATUS_PREFIX_RE, "").replace(/^(?:status\s+)?(?:OK|WARN|CRITICAL)\s*[-–:]\s+/i, "");
  s = s.replace(DEFAULT_PERCENT_RE, "$1%");
  s = s.replace(/\s*\(default\)/g, "");
  return upperFirst(s);
}

function appTabLinkProps(tab: string, subtab: string | null | undefined) {
  const href = navHref({ tab, subtab: subtab || null, focus: null });
  return { href, onClick: (e: React.MouseEvent) => { e.preventDefault(); window.location.hash = href; } };
}

function openInAppLinkProps(f: GuideFinding) {
  const home = homeForQuery(f);
  const build = () => navHref({
    tab: home.tab, subtab: home.subtab, domain: null, focusQueryId: f.query_id, view: null,
  });
  return {
    href: build(),
    onClick: (e: React.MouseEvent) => { e.preventDefault(); window.location.hash = build(); },
  };
}

function fallbackCopyText(text: string) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); } finally { document.body.removeChild(ta); }
}

function copyGuideLink(queryId: string, onDone?: () => void) {
  const url = `${window.location.origin}${window.location.pathname}#${new URLSearchParams({ tab: "guide", focus: queryId })}`;
  const clip = window.navigator && window.navigator.clipboard;
  const done = () => onDone && onDone();
  if (clip && clip.writeText) {
    clip.writeText(url).then(done).catch(() => { try { fallbackCopyText(url); done(); } catch (e) { /* nothing more to try */ } });
    return;
  }
  try { fallbackCopyText(url); done(); } catch (e) { /* nothing more to try */ }
}

function DocsLink({ docsKey, docs, cloud }: { docsKey: string; docs: Record<string, GuideDoc>; cloud: string }) {
  const entry = docs[docsKey];
  if (!entry) return null;
  const link = entry.urls[cloud];
  return (
    <div className="guide2-doclink">
      {link ? (
        <a href={link.url} target="_blank" rel="noreferrer">{entry.title || docsKey}</a>
      ) : (
        <span className="muted">{entry.title || docsKey}</span>
      )}
      {link && link.aws_fallback && <span className="muted"> (AWS page)</span>}
      {!link && <span className="muted"> -- {entry.note || "No Databricks page documents this yet."}</span>}
      {entry.what && <div className="muted guide2-doc-what">{entry.what}</div>}
    </div>
  );
}

// The 8 "How this app works" topics' full text, unchanged from before this redesign except the
// "money" case, which now names idle cluster nodes (compute_idle_node_ratio) alongside idle
// warehouse minutes -- Waste & savings prices that source too, so this explanation must agree.
// What a tag filter follows for each group of checks, most specific first.
const TAG_REACH_GROUPS = [
  { key: "spend", label: "Query, job or pipeline tag → warehouse, cluster or budget-policy tag → workspace tag", note: "Spend is recomputed from each billed dollar's most specific tag." },
  { key: "work", label: "Job, pipeline or query tag → compute tag → workspace tag", note: "Each row names the job, pipeline or query." },
  { key: "compute", label: "Warehouse, cluster or pool tag → workspace tag", note: "Each row names the compute." },
  { key: "endpoint", label: "Serving endpoint tag → workspace tag", note: "Each row names the endpoint." },
  { key: "object", label: "Table, schema or catalog tag (Unity Catalog)", note: "Each row names a table or other data object." },
  { key: "row", label: "The row's own tag", note: "These checks list tag values themselves." },
  { key: "workspace", label: "Workspace tag only", note: "Rows carry no query, job or compute, so the filter keeps or drops whole workspaces." },
  { key: "none", label: "Not filtered by tags", note: "Price lists, grants and other lists no tag can reach; they always cover everything." },
];

function tagReachGroup(reach: TagReach | null): string | null {
  if (!reach) return null;
  const w = reach.words || [];
  if (reach.kind === "spend") return "spend";
  if (reach.kind === "none") return "none";
  if (reach.kind === "row_tags") return "row";
  if (w.some((x) => x === "query" || x === "job" || x === "pipeline")) return "work";
  if (w.includes("endpoint")) return "endpoint";
  if (w.includes("object")) return "object";
  if (w.includes("compute")) return "compute";
  return "workspace";
}

function TagReachTable({ findings }: { findings: GuideFinding[] }) {
  const byGroup: Record<string, string[]> = {};
  let unknown = 0;
  (findings || []).forEach((f) => {
    const g = tagReachGroup(f.tag_reach);
    if (!g) { unknown += 1; return; }
    (byGroup[g] = byGroup[g] || []).push(getCheckLabel(f.query_id, f.title).title);
  });
  return (
    <div>
      <h3>What the tag filter reaches</h3>
      <table className="tag-rollup-table">
        <thead>
          <tr className="faint">
            <th>The filter follows</th>
            <th>Checks</th>
          </tr>
        </thead>
        <tbody>
          {TAG_REACH_GROUPS.filter((g) => byGroup[g.key]).map((g) => (
            <tr key={g.key}>
              <td>
                <div>{g.label}</div>
                <div className="faint">{g.note}</div>
                <details>
                  <summary className="faint">Show checks</summary>
                  <ul className="faint">{byGroup[g.key].sort().map((n) => <li key={n}>{n}</li>)}</ul>
                </details>
              </td>
              <td className="mono">{byGroup[g.key].length}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {unknown > 0 && <p className="faint">{unknown} checks are not built in this snapshot, so their reach is not known yet.</p>}
    </div>
  );
}

function renderHowBody(slug: string, app: GuideApp, findings: GuideFinding[]): React.ReactNode {
  switch (slug) {
    case "flow": {
      const direct = app.direct_export;
      const steps = direct
        ? ["Databricks system tables", "Computed on Databricks", "Result files", "This app"]
        : ["Databricks system tables", "Snapshot files", "Build (DuckDB)", "This app"];
      return (
        <div className="guide2-flow">
          {steps.map((step, i) => (
            <React.Fragment key={step}>
              {i > 0 && <span className="guide2-flow-arrow">&rarr;</span>}
              <span className="guide2-flow-step">{step}</span>
            </React.Fragment>
          ))}
        </div>
      );
    }
    case "window":
      return (
        <ul>
          <li>Today counts up to the export moment; the top bar says "today partial".</li>
          <li>Billing lags a few hours, so today's last hours can be missing. <span className="mono">include_today: false</span> exports full days only.</li>
          <li>A snapshot shorter than your window is marked, so a partial window never reads as complete.</li>
          <li>A snapshot check has no window: it shows the latest known configuration.</li>
        </ul>
      );
    case "status":
      return <StatusMeaningsGrid />;
    case "floors":
      return (
        <ul>
          <li>The row is marked <span className="mono">below_floor</span>.</li>
          <li>Counts, worst status and $ totals already exclude floored rows.</li>
          <li>A Not assessed row is never floored.</li>
          <li>Floors are set per check in <span className="mono">config/materiality.yml</span>.</li>
        </ul>
      );
    case "money":
      return (
        <div>
          <ul>
            <li>The discount ({fmtPct(app.discount_pct * 100)} today) is a "what if we negotiated X% off list", not your contract rate.</li>
            <li>Only Databricks usage: no cloud VM, storage or network-egress cost.</li>
          </ul>
          <p><b>Possible waste</b></p>
          <ul>
            <li>Idle warehouse minutes{app.idle_gap_seconds != null ? ` over ${app.idle_gap_seconds}s at a stretch` : ""}, incl. start→first query and last query→auto-stop</li>
            <li>Idle classic-cluster nodes (near-zero CPU while billed)</li>
            <li>Failed or timed-out job runs, incl. repaired attempts</li>
            <li>Failed SQL statements</li>
            <li>Serving endpoints billed with zero requests</li>
          </ul>
          <p>No forecasts, no "per month": every figure states its dates.</p>
        </div>
      );
    case "steps":
      return (
        <div>
          <ul className="gd-step-badges">
            <li><Badge kind="info">FREE</Badge> a setting change or a read, no spend</li>
            <li><Badge kind="info">CONFIG</Badge> changes how something is configured, not its cost directly</li>
            <li><Badge kind="info">SPEND</Badge> costs money to carry out</li>
          </ul>
          <p>A threshold in a step shows "default", or "your setting" once <span className="mono">config/thresholds.yml</span> overrides it.</p>
        </div>
      );
    case "tags":
      return (
        <div>
          <ul>
            <li>Each level has its own "no tag, cannot be attributed" row with a count and $.</li>
            <li>Levels always sum to the same billing total: nothing is invented or dropped.</li>
            <li>Filters offer the tag keys your account uses, not a fixed list.</li>
          </ul>
          <p><b>What the Tag filter does today</b></p>
          <ul>
            <li>
              <b>Spend</b> (Overview, Cost): each dollar takes its most specific tag, values add up to the bill.
              <ol>
                <li>Query tag (for the warehouse time Databricks attributes to queries)</li>
                <li>Job or pipeline tag</li>
                <li>Bill tag (warehouse, cluster, budget policy)</li>
                <li>Workspace tag</li>
              </ol>
            </li>
            <li><b>Single queries, jobs, pipelines, runs, warehouses and clusters:</b> the row's own tag, else the next level up, as listed below.</li>
            <li><b>Tables, schemas and catalogs:</b> their Unity Catalog tags.</li>
            <li><b>Several tag names:</b> a row must match every name; the values picked under one name are alternatives.</li>
            <li>
              <b>The Tags page:</b>
              <ul>
                <li>Counts objects whose own, bill or workspace tag matches.</li>
                <li>Lists those that match only through their workspace.</li>
                <li>A SQL warehouse query counts by its own query tag first.</li>
              </ul>
            </li>
            <li><b>Workspace tags on any cloud:</b> from <span className="mono">config/workspace.csv</span> (workspace_id, then one column per tag) for the workspaces it lists; the rest keep their tags from the bill (Azure only).</li>
          </ul>
          <p><b>What it doesn't do yet</b></p>
          <ul>
            <li><b>Queries › Trend and the Capacity waits</b> read query slices with a tag filter on, so each query counts by its own query tag first.</li>
            <li><b>Warehouse starts, clock waits, pressure, hourly grids, top queries by $:</b> per warehouse or query group, so they follow warehouse then workspace tags, and say so when the tag is only on queries.</li>
            <li><b>Idle warehouse time</b> has no query, so its dollars follow the warehouse's tag, then the workspace's.</li>
            <li><b>Checks no tag can reach</b> (price lists, grants, account settings) are never filtered and show "tag n/a".</li>
          </ul>
          <p><b>Which tag each object gets.</b> For each tag key, the first level that has it wins:</p>
          <ul>
            <li><b>Query:</b> its own query tag, else the tag of the job or pipeline that ran it, else its compute's (the warehouse's or cluster's own tag or bill; on serverless, the usage policy's), else its workspace's.</li>
            <li><b>Job or pipeline:</b> its own tag, else its compute's tag, else its workspace's.</li>
            <li><b>Warehouse or cluster:</b> its own tag (a cluster's includes its compute policy's and pool's) or its bill's, else its workspace's.</li>
            <li><b>Serverless notebook:</b> its usage policy's tag, else its workspace's.</li>
            <li><b>Workspace:</b> its own tag; on Azure, the workspace resource's tag.</li>
          </ul>
          <p><b>Azure</b></p>
          <ul>
            <li>The workspace tag is copied onto every bill row, classic and serverless.</li>
            <li>Tags page: a bill value equal to it counts as the workspace's, so an untagged warehouse isn't credited.</li>
            <li>A value on a bill with a usage policy counts as the policy's, even if equal.</li>
            <li>To split two warehouses, tag each one (a usage policy on a serverless bill counts too).</li>
          </ul>
          <p><b>Dollars follow the bill</b></p>
          <ul>
            <li>Each day's bill carries that day's warehouse/cluster tags.</li>
            <li>Own tag and bill disagree: cost follows the bill; the Tags page lists it under "tagged one way, billed another".</li>
            <li>Performance by tag: latest warehouse/cluster tags for the whole window; workspace tag valid on each query's day.</li>
          </ul>
          <p>
            <b>Example.</b> Warehouse BI is tagged cost_center=A on itself; its bill reads
            cost_center=B since 4 Sep. A query on BI tagged cost_center=C is charged to C. A query with
            no tag is charged to B from 4 Sep, and to the bill's earlier value before. The Tags page
            counts BI as tagged on itself and lists it as tagged A, billed B.
          </p>
          <p><b>Dates and values on the Tags page</b></p>
          <div className="gd-table-wrap">
            <table className="gd-table gd-term-table">
              <thead><tr><th>Term</th><th>Meaning</th></tr></thead>
              <tbody>
                <tr><td>since 4 Sep</td><td>On the bill every billed day since.</td></tr>
                <tr><td>gone after 1 Sep</td><td>Was on the bill, not on its latest day.</td></tr>
                <tr><td>(before: A)</td><td>Other values the key had on the bill.</td></tr>
                <tr><td>last billed 12 Jun</td><td>No bill for over a week; the tags are that day's.</td></tr>
                <tr><td>mixed: red, blue</td><td>Billed values split, none on most; covers nothing.</td></tr>
              </tbody>
            </table>
          </div>
          <p><b>Workspace tags</b></p>
          <ul>
            <li>No Databricks table lists workspace tags; each is read from the workspace's bill (Azure copies them onto every row).</li>
            <li>
              Counts when ≥{fmtPct((app.tag_coverage_floor || 0) * 100, 0)} of billed usage without a usage policy has the key and one value has ≥{fmtPct((app.tag_share_floor || 0) * 100, 0)}
              {" "}(<span className="mono">tag_coverage_floor</span>, <span className="mono">tag_share_floor</span>).
            </li>
            <li>Then it also covers untagged serverless usage, queries, jobs and compute.</li>
            <li>Split values read "mixed" and cover nothing; no billed usage = no workspace tag.</li>
            <li>Shares: Cost › Allocation › Workspace tags.</li>
          </ul>
          <p><b>Mandatory tags</b></p>
          <ul>
            <li>Set in <span className="mono">mandatory_tag_keys</span>{app.mandatory_tag_keys ? ` (now ${app.mandatory_tag_keys.join(", ")})` : ""}, up to 5.</li>
            <li>The Tags page counts queries, jobs, pipelines, serverless notebooks, clusters, warehouses and workspaces missing them, level by level, and where untagged queries come from.</li>
          </ul>
          <p><b>Chargeback by tag value</b></p>
          <ul>
            <li>Cost › Chargeback › Tag value splits spend by one key: pick a workspace, then a key.</li>
            <li>Key order: mandatory → 2+ values in a workspace → one value per workspace (= Workspace view) → one value only (splits nothing).</li>
            <li>The workspace list holds only workspaces with tagged spend in the window, so it is usually shorter than the account count; the Tags page Workspaces card counts every workspace with spend.</li>
          </ul>
          <TagReachTable findings={findings} />
        </div>
      );
    case "names":
      return (
        <ul>
          <li>Known name: shown as "name (id)".</li>
          <li>Unknown: "no name: &lt;reason&gt;", never blank or guessed.</li>
        </ul>
      );
    case "coverage":
      return (
        <ul>
          <li>Shows which system tables this snapshot captured and what enabling each needs.</li>
          <li>Most schemas need only <span className="mono">{app.default_enable}</span>; a check's "Where the data comes from" names anything extra.</li>
          <li>Regional tables cover only the metastore's region; billing and the workspace list are account-wide.</li>
          <li>Each check's "For reviewers and admins" gives its confidence and known query issues.</li>
        </ul>
      );
    case "coverage-data-sources":
      return (
        <div>
          <ul>
            <li>Sources tab: every readable system table: rows, date range or "current state", checks using it, what fills it if empty.</li>
            <li><b className="gd-v-ok">With rows:</b> captured this snapshot. <b className="gd-v-warn">Empty:</b> read fine, 0 rows, not a verified zero. <b className="gd-v-warn">Missing:</b> couldn't be read.</li>
            <li>System table coverage is a real check: WARN when a table is stale or has a gap in this window.</li>
            <li>This audit's own usage: reference row with the audit's share of each warehouse's traffic and spend.</li>
          </ul>
          <p><b>What to do</b></p>
          <ul>
            <li>Missing → grant the read the row names (account or metastore).</li>
            <li>Empty → read its "what fills it" note (true zero, or a feature to turn on).</li>
            <li>Coverage WARN → check that table's ingestion job or grant.</li>
          </ul>
          <a className="ck-link" {...appTabLinkProps("coverage", "sources")}>{"Back to Coverage & Gaps › Sources →"}</a>
        </div>
      );
    case "coverage-couldnt-check":
      return (
        <div>
          <p>Checks that judged nothing this window, never a pass:</p>
          <ul>
            <li><b className="gd-v-warn">Not assessed:</b> build skipped or failed</li>
            <li><b className="gd-v-warn">Read error</b></li>
            <li><b>Nothing in this window</b></li>
            <li><b>Filtered out:</b> your filters excluded every row</li>
          </ul>
          <p><b>What to do:</b> open the check's page → "If it says Not assessed or is empty" for the reason and the grant or setting that fixes it.</p>
          <a className="ck-link" {...appTabLinkProps("coverage", "couldnt")}>{"Back to Coverage & Gaps › Not assessed →"}</a>
        </div>
      );
    case "coverage-known-limitations":
      return (
        <div>
          <ul>
            <li>Fixed modelling choices, not bugs.</li>
            <li>E.g. a ranking-by-size check never reads Critical or Warn; autoscale churn is counted in events, not time.</li>
            <li>Read the affected number with its note in mind.</li>
          </ul>
          <a className="ck-link" {...appTabLinkProps("coverage", "limits")}>{"Back to Coverage & Gaps › Known limitations →"}</a>
        </div>
      );
    default:
      return null;
  }
}

// ═══════════════════════════════════ Status legend ═══════════════════════════════════

const STATUS_MEANING_ROWS = [
  { kind: "critical", word: "Critical", d: "Flagged as the worst kind of problem this check looks for." },
  { kind: "warn", word: "Warn", d: "Flagged, but not the worst kind this check has." },
  { kind: "ok", word: "OK", d: "Ran on real data in this window and found nothing. A verified pass." },
  { kind: "ranked", word: "Ranked", d: "Sorted by size only. Big isn't the same as wrong." },
  { kind: "not_assessed", word: "Not assessed", d: "Didn't run in this export. Never a pass: numbers show \"–\" and the reason; left out of \"N of M checks ran\"." },
  { kind: "reference", word: "No data", d: "Ran; nothing happened in this window. Not a verified zero. Listed on the \"No data\" tab." },
  { kind: "reference", word: "No data (filtered)", d: "Rows exist; your workspace or tag filters hide them all. Also on \"No data\"." },
  { kind: "reference", word: "Reference", d: "Lookup list, no pass or fail. If it flags rows Critical or Warn, it counts like a check." },
];
function StatusMeaningsGrid() {
  return (
    <div className="guide2-status-grid">
      {STATUS_MEANING_ROWS.map((r, i) => (
        <div className="guide2-status-row" key={i}>
          <Badge kind={r.kind}>{r.word}</Badge>
          <span>{r.d}</span>
        </div>
      ))}
    </div>
  );
}

const STATUS_LEGEND = [
  { kind: "critical", word: "Critical", short: "worst" },
  { kind: "warn", word: "Warn", short: "flagged" },
  { kind: "ok", word: "OK", short: "pass" },
  { kind: "ranked", word: "Ranked", short: "by size" },
  { kind: "not_assessed", word: "Not assessed", short: "didn't run" },
  { kind: "reference", word: "No data", short: "empty" },
  { kind: "reference", word: "Reference", short: "lookup" },
];

function guideFaqs() {
  return [
    {
      q: "Is this my Databricks invoice?",
      a: (
        <ul className="gd-bullets">
          <li>No: Databricks usage × published list price, minus your what-if discount.</li>
          <li>Leaves out your cloud VM, storage and network bill, and your negotiated rate.</li>
          <li>Use it to compare and rank, not to reconcile an invoice. <a {...guideLinkProps("how-money")}>How dollars are worked out &rarr;</a></li>
        </ul>
      ),
    },
    {
      q: "Why is a panel empty, or a whole screen \"no data\"?",
      a: (
        <ul className="gd-bullets">
          <li>Ran and found nothing, or the source isn't in this snapshot. Never "all clear".</li>
          <li><a {...appTabLinkProps("coverage", "sources")}>Coverage & Gaps &rarr;</a> lists the missing sources.</li>
        </ul>
      ),
    },
    {
      q: "What counts as possible waste, and what doesn't?",
      a: (
        <ul className="gd-bullets">
          <li>Counted: idle warehouse minutes, idle classic-cluster nodes, failed or timed-out job runs, failed SQL statements, serving endpoints billed with 0 requests.</li>
          <li>Priced at list: a lower bound, never a forecast.</li>
          <li>Not counted: premium SKUs (serverless, Photon). Worth a look, not waste.</li>
          <li><a {...guideLinkProps("how-money")}>How dollars are worked out &rarr;</a></li>
        </ul>
      ),
    },
    {
      q: "Why does a check say \"Not assessed\"?",
      a: "The build behind that check failed or was skipped this run, never a verified pass. Open the check's page and look under \"If it says Not assessed or is empty\" for the reason.",
    },
    {
      q: "How do I change a threshold, like the 60-second idle rule?",
      a: (
        <ul className="gd-bullets">
          <li>Every threshold lives in <span className="mono">config/thresholds.yml</span>.</li>
          <li>A check's "When it flags" shows each value and whether it is the default.</li>
        </ul>
      ),
    },
    {
      q: "Which grants does Databricks need for the missing tables?",
      a: (
        <React.Fragment>
          <a {...appTabLinkProps("coverage", "sources")}>Coverage & Gaps &rarr;</a> lists the grant each missing source needs, next to that source.
        </React.Fragment>
      ),
    },
    {
      q: "Why do some numbers differ between screens?",
      a: "Two screens can show different numbers for the same thing when one counts rows and the other counts distinct entities, or when a workspace/tag filter applies to one screen but not the other (metastore-wide Governance & PII and Storage checks ignore the workspace filter). Each screen states its own basis in its footer.",
    },
  ];
}

const GUIDE_GLOSSARY = [
  { k: "DBU", d: "Databricks Unit: the unit compute is billed in. Dollars = DBUs × that product's price." },
  { k: "List price", d: "Databricks' published price per product, after any promotion. Not your negotiated rate." },
  { k: "Possible waste", d: "Measured idle time and failed work, priced at list. A lower bound, never a forecast." },
  { k: "Window", d: "The days a check covers: the last 7, 30 or 90, up to the export." },
  { k: "Auto-stop", d: "How long a warehouse waits after its last query before it shuts down." },
  { k: "Spill", d: "A query ran out of memory and wrote to disk, which slows it down." },
  { k: "Broken job", d: "Its latest 3 or more runs failed in a row (default threshold)." },
  { k: "Flaky job", d: "20% or more of its runs failed, with at least 5 runs in the window (default thresholds)." },
  { k: "Not assessed", d: "The check didn't run. Never a pass." },
  { k: "No data", d: "It ran and saw nothing in the window. Not a verified zero." },
  { k: "Flagged", d: "At least one row is over a Warn or Critical threshold." },
  { k: "Reference", d: "A list or ranking with no pass/fail rule." },
];

function CloudSwitch({ cloud, setCloud, cloudNote, overrideStale, useSnapshotCloud }: {
  cloud: string; setCloud: (c: string) => void; cloudNote: string; overrideStale: boolean; useSnapshotCloud: () => void;
}) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div className="guide2-cloud" ref={ref}>
      <button type="button" className="guide2-cloud-btn" onClick={() => setOpen(!open)}>
        <span className="muted">Databricks docs for</span> <b>{CLOUD_WORD[cloud] || cloud}</b>{" "}
        <span className="caret">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div className="guide2-cloud-pop">
          <div className="seg">
            {CLOUD_OPTIONS.map((c) => (
              <button key={c} type="button" className={cloud === c ? "active" : ""} onClick={() => { setCloud(c); setOpen(false); }}>{CLOUD_WORD[c] || c}</button>
            ))}
          </div>
          <div className="muted guide2-cloud-note">{cloudNote}</div>
          {overrideStale && <button type="button" className="ck-link" onClick={useSnapshotCloud}>Use the snapshot's cloud</button>}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════ Shared ═══════════════════════════════════

// Every check id a screen shows, in its own sub-tab order -- the same lists the screen reads.
function tabCheckIds(areaKey: string): string[] {
  if (areaKey === "money") return MONEY_CHECK_IDS.slice();
  const area = AREA_REGISTRY[areaKey];
  if (!area) return [];
  if (areaKey === "overview") return OVERVIEW_IDS.slice();
  if (areaKey === "waste") return WASTE_IDS.slice();
  if (areaKey === "actions") return [...new Set(ACTION_TEMPLATES.flatMap((t) => t.checks))];
  const ids: string[] = [];
  const add = (id: string) => { if (!ids.includes(id)) ids.push(id); };
  (area.subtabs || []).forEach((s) => { (s.ids || []).forEach(add); (s.refIds || []).forEach(add); });
  (area.refIds || []).forEach(add);
  return ids;
}

// Check ids break only after an underscore.
function wrapIdAfterUnderscore(id: string): React.ReactNode[] {
  return id.split("_").flatMap((part, i, a) => (i < a.length - 1 ? [`${part}_`, <wbr key={i} />] : [part]));
}

function shortTable(key: string): string { return String(key).replace(/^system\./, ""); }

// System tables read by these checks, most used first.
function tablesFor(ids: string[], guideIndex: GuideIndex): string[] {
  const count: Record<string, number> = {};
  ids.forEach((id) => ((guideIndex[id] && guideIndex[id].reads) || []).forEach((t) => {
    if (String(t).indexOf("system.") === 0) count[t] = (count[t] || 0) + 1;
  }));
  return Object.keys(count).sort((a, b) => count[b] - count[a] || a.localeCompare(b));
}

function schemasFor(tables: string[]): string[] {
  return [...new Set(tables.map((t) => String(t).split(".").slice(0, 2).join(".")))];
}

function checkRan(s: FindingSummary | null | undefined): boolean { return !!s && String(s.outcome || "").indexOf("ok") === 0 && bandOf(s) !== "NOT_ASSESSED"; }
function checkFlagged(s: FindingSummary | null | undefined): boolean {
  if (!s || RANKED_QUERY_IDS.has(s.query_id)) return false;
  const c = s.status_counts || {};
  return (c.CRITICAL || 0) + (c.WARN || 0) > 0;
}

function tabGuideId(areaKey: string): string { return `how-area-${areaKey}`; }

/** One row per sub-tab of an area, each carrying only the checks it owns. */
interface GuideSubtabGroup { key: string | null; label: string | null; ids: string[] }

// The Guide nav opens the one sub-tab holding the current page by default, and any other sub-tab
// the reader opens by hand -- so a 17-check tab never starts by listing more than the handful a
// reader is actually looking at, but every group stays reachable.
function guideSubtabGroups(areaKey: string, guideIndex: GuideIndex): GuideSubtabGroup[] {
  const area = AREA_REGISTRY[areaKey];
  const flat = () => [{ key: null, label: null, ids: tabCheckIds(areaKey).filter((id) => guideIndex[id]) }];
  if (!area || !(area.subtabs || []).length) return flat();
  const groups: GuideSubtabGroup[] = [];
  const covered = new Set<string>();
  (area.subtabs || []).forEach((s) => {
    const ids: string[] = [];
    const add = (id: string) => { if (guideIndex[id] && !ids.includes(id)) { ids.push(id); covered.add(id); } };
    (s.ids || []).forEach(add);
    (s.refIds || []).forEach(add);
    if (ids.length) groups.push({ key: s.key, label: s.label, ids });
  });
  const rest = (area.refIds || []).filter((id) => guideIndex[id] && !covered.has(id));
  if (rest.length) groups.push({ key: null, label: null, ids: rest });
  return groups.length ? groups : flat();
}

// Tab order in the Guide nav matches the app sidebar for the current lens -- the current check's
// own area is still added when its lens wouldn't otherwise show that tab, so a deep link into a
// check never loses its own group.
function guideNavAreas(role: string | null | undefined, checkArea: string | null): string[] {
  const list = !role ? GUIDE_SCREEN_ORDER.slice()
    : role === "cfo" ? ["money"]
      : ["overview", ...roleAreasFor(role).filter((k) => k !== "findings")];
  if (checkArea && !list.includes(checkArea)) list.push(checkArea);
  return list;
}

function TableChips({ tables, max }: { tables: string[]; max?: number }) {
  const shown = max ? tables.slice(0, max) : tables;
  return (
    <span className="gd-tables">
      {shown.map((t) => <code key={t} className="gd-code">{shortTable(t)}</code>)}
      {tables.length > shown.length && <span className="muted">{`+${tables.length - shown.length} more`}</span>}
    </span>
  );
}

// The Guide's one menu -- App.tsx portals this into the app's own dark rail (desktop) and into
// the mobile Contents sheet, so it is the SAME list everywhere, never a second column.
// Same shape on every page: Guide home, 1 Start here, 2 Tabs in app order (the current one
// expanded to its own sub-tab), 3 Reference -- Reference always shows on every page.
export function GuideNav({ current, checkArea, guideIndex, role, backLabel, backHref, onBack, autoFocusSearch, searchRef, collapseLong }: {
  current: string; checkArea: string | null; guideIndex: GuideIndex; role?: string | null;
  backLabel: string; backHref: string; onBack: () => void; autoFocusSearch?: boolean; searchRef?: React.RefObject<HTMLInputElement>;
  collapseLong?: boolean;
}) {
  const ownSearchRef = React.useRef<HTMLInputElement>(null);
  const inputRef = searchRef || ownSearchRef;
  React.useEffect(() => { if (autoFocusSearch) inputRef.current?.focus(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const [query, setQuery] = React.useState("");
  // Sub-tab groups the reader opened by hand, beyond the one holding the current page (which is
  // always open) -- so a tab's "About this tab" page, or any other check in it, can still reach
  // every one of that tab's groups, not just the page's own.
  const [openGroups, setOpenGroups] = React.useState(() => new Set<string>());
  const toggleGroup = (key: string) => setOpenGroups((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  // Long check lists (phone sheet) show 5 until the reader asks for the rest.
  const [expandedLists, setExpandedLists] = React.useState(() => new Set<string>());
  const needle = query.trim().toLowerCase();
  // Checks first, then Start-here/Coverage topics, then glossary terms -- same needle, three
  // sources, so the nav search also finds a topic or term, not only a check (a term has no page
  // of its own, so every one of its matches still opens the glossary).
  const allMatches = React.useMemo(() => {
    if (!needle) return null;
    const checks = Object.values(guideIndex)
      .filter((f) => guideSearchText(f, getCheckLabel(f.query_id, f.title)).includes(needle))
      .map((f) => ({ key: f.query_id, navId: f.query_id, label: getCheckLabel(f.query_id, f.title).title }));
    const sections = GUIDE_SECTIONS
      .filter((s) => `${s.title} ${s.teaser}`.toLowerCase().includes(needle))
      .map((s) => ({ key: `how-${s.slug}`, navId: `how-${s.slug}`, label: s.title }));
    const glossary = GUIDE_GLOSSARY
      .filter((g) => `${g.k} ${g.d}`.toLowerCase().includes(needle))
      .map((g) => ({ key: `glossary-${g.k}`, navId: "how-glossary", label: g.k }));
    // A system table opens the first check that reads it.
    const tables = tablesFor(Object.keys(guideIndex), guideIndex)
      .filter((t) => t.toLowerCase().includes(needle))
      .map((t) => {
        const readers = Object.values(guideIndex).filter((f) => (f.reads || []).includes(t));
        return { key: `table-${t}`, navId: readers[0].query_id, label: `${shortTable(t)} · table, ${readers.length} check${readers.length === 1 ? "" : "s"}` };
      });
    return [...checks, ...tables, ...sections, ...glossary];
  }, [needle, guideIndex]);
  const matches = allMatches ? allMatches.slice(0, 30) : null;
  const item = (id: string, label: React.ReactNode, extra?: number, navId?: string) => (
    <a key={id} className={`gd-nav-item${current === (navId || id) ? " active" : ""}`} {...guideLinkProps(navId || id)}>
      {label}{extra != null && <span className="muted">{` · ${extra}`}</span>}
    </a>
  );
  const subItem = (id: string, label: string) => (
    <a key={id} className={`gd-nav-item gd-nav-sub${current === id ? " active" : ""}`} {...guideLinkProps(id)}>{label}</a>
  );
  const subList = (listKey: string, ids: string[]) => {
    const limit = 5;
    if (!collapseLong || ids.length <= limit + 1 || expandedLists.has(listKey)) return ids.map((id) => subItem(id, getCheckLabel(id, guideIndex[id].title).title));
    const shown = ids.slice(0, limit);
    if (ids.includes(current) && !shown.includes(current)) shown.push(current);
    return (
      <React.Fragment>
        {shown.map((id) => subItem(id, getCheckLabel(id, guideIndex[id].title).title))}
        <button type="button" className="gd-nav-subh gd-nav-more" onClick={() => setExpandedLists((prev) => new Set(prev).add(listKey))}>{`+ ${ids.length - shown.length} more`}</button>
      </React.Fragment>
    );
  };
  const isHome = current === "home";
  const isStartHere = GUIDE_UNIVERSAL_SECTIONS.some((s) => `how-${s.slug}` === current);
  const tabOrder = guideNavAreas(role, checkArea);
  return (
    <nav className="gd-nav" aria-label="Guide">
      <a className="gd-nav-back" href={backHref} onClick={(e) => { e.preventDefault(); onBack(); }}>{`← Back to ${backLabel}`}</a>
      <label className="gd-nav-search">
        <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.5" fill="none" /><path d="M11 11l3.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
        <input
          ref={inputRef}
          type="search"
          aria-label="Search checks, tables, terms"
          placeholder="Search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <span className="gd-nav-kbd">{isMac() ? "⌘K" : "Ctrl+K"}</span>
      </label>
      {matches ? (
        <div className="gd-nav-group">
          <div className="gd-nav-h">{`${allMatches!.length} match${allMatches!.length === 1 ? "" : "es"}`}</div>
          {matches.map((m) => item(m.key, m.label, undefined, m.navId))}
        </div>
      ) : (
        <React.Fragment>
          {item("home", "Guide home")}
          <details className="gd-nav-start" open={isHome || isStartHere}>
            <summary>{"1 · Start here"}<span className="muted">{` (${GUIDE_UNIVERSAL_SECTIONS.length})`}</span></summary>
            {GUIDE_UNIVERSAL_SECTIONS.map((s) => item(`how-${s.slug}`, s.title))}
          </details>
          <div className="gd-nav-group">
            <div className="gd-nav-h">2 · Tabs, in app order</div>
            {tabOrder.map((k) => {
              const a = guideArea(k);
              if (!a) return null;
              const n = tabCheckIds(k).filter((id) => guideIndex[id]).length;
              if (k !== checkArea) return item(tabGuideId(k), a.label, n || undefined);
              const groups = guideSubtabGroups(k, guideIndex);
              return (
                <div key={k} className="gd-nav-area">
                  <div className="gd-nav-item gd-nav-area-h">{a.label}{n > 0 && <span className="muted">{` · ${n}`}</span>}</div>
                  {subItem(tabGuideId(k), "About this tab")}
                  {groups.map((g, gi) => {
                    if (!g.label) return <React.Fragment key={gi}>{subList(`${k}:rest`, g.ids)}</React.Fragment>;
                    const groupKey = `${k}:${g.key}`;
                    const open = g.ids.includes(current) || openGroups.has(groupKey);
                    return (
                      <React.Fragment key={g.key}>
                        <button type="button" className="gd-nav-subh" aria-expanded={open} onClick={() => toggleGroup(groupKey)}>
                          {`${open ? "▾" : "▸"} ${g.label} · ${g.ids.length}`}
                        </button>
                        {open && subList(groupKey, g.ids)}
                      </React.Fragment>
                    );
                  })}
                </div>
              );
            })}
          </div>
          <div className="gd-nav-group">
            <div className="gd-nav-h">3 · Reference</div>
            {item("how-checks", "Check reference", Object.keys(guideIndex).length)}
            {item("how-troubleshooting", "Troubleshooting")}
            {item("how-glossary", "Glossary")}
          </div>
        </React.Fragment>
      )}
    </nav>
  );
}

function JumpChips({ sections, className }: { sections: { id: string; label: string }[]; className?: string }) {
  return (
    <nav className={`gd-jumpchips${className ? ` ${className}` : ""}`} aria-label="Jump to a section">
      {sections.map((s) => (
        <a key={s.id} className="gd-jumpchip" href={`#${s.id}`} onClick={(e) => { e.preventDefault(); const el = document.getElementById(s.id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }}>{s.label}</a>
      ))}
    </nav>
  );
}

function GuidePage({ current, rail, children }: { current: string; rail?: React.ReactNode; children?: React.ReactNode }) {
  React.useEffect(() => { window.scrollTo(0, 0); }, [current]);
  return (
    <div className={`gd-layout${rail ? " has-rail" : ""}`}>
      <main className="gd-main">{children}</main>
      {rail && <aside className="gd-rail">{rail}</aside>}
    </div>
  );
}

function Crumbs({ items }: { items: { label: string; focus?: string | null }[] }) {
  return (
    <nav className="gd-crumbs" aria-label="Breadcrumb">
      <a {...guideLinkProps(null)}>How it works</a>
      {items.map((it, i) => (
        <React.Fragment key={i}>
          <span className="gd-crumb-sep">/</span>
          {it.focus ? <a {...guideLinkProps(it.focus)}>{it.label}</a> : <span>{it.label}</span>}
        </React.Fragment>
      ))}
    </nav>
  );
}

function RailCard({ title, children }: { title?: string; children?: React.ReactNode }) {
  return (
    <div className="gd-rail-card">
      {title && <div className="gd-rail-h">{title}</div>}
      {children}
    </div>
  );
}

// ═══════════════════════════════════ Guide home ═══════════════════════════════════

// The picker's own lens names for an area; the CFO lens has its own page, not these areas.
function lensesForArea(areaKey: string): string | null {
  const roles = ROLES.filter((r) => r !== "cfo" && (areaKey === "overview" || (ROLE_AREAS[r] || []).includes(areaKey)));
  return roles.length ? roles.map((r) => ROLE_LABEL[r]).join(", ") : null;
}

const GUIDE_RULES = [
  "Dollars are usage × Databricks list price. Not your invoice, never your cloud VM bill.",
  "Windows are the last 7, 30 or 90 days up to the export; today is partial.",
  "Empty never means all clear. Only OK is a verified pass.",
];

// Start/Explore/Reference, in app order -- the same split the real sidebar uses (App.tsx), scoped
// to the current lens the same way the nav's own tabOrder is (guideNavAreas): a governance reader
// never sees a Jobs card here either.
const HOME_START_KEYS = ["overview", "actions", "money"];
const HOME_REFERENCE_KEYS = ["coverage", "findings"];
function homeGroupsForRole(role: string | null | undefined): { name: string; items: string[] }[] {
  const tabs = guideNavAreas(role, null);
  if (role === "cfo") return [{ name: "Tabs", items: tabs }];
  return [
    { name: "Start", items: tabs.filter((k) => HOME_START_KEYS.includes(k)) },
    { name: "Explore", items: tabs.filter((k) => !HOME_START_KEYS.includes(k) && !HOME_REFERENCE_KEYS.includes(k)) },
    // "findings" (All findings) is never in guideNavAreas (no guide page of its own) but every
    // non-CFO sidebar shows it, so it's added back here.
    { name: "Reference", items: [...tabs.filter((k) => HOME_REFERENCE_KEYS.includes(k)), "findings"] },
  ];
}

function GuideHome({ data, guideIndex, cloud, setCloud, cloudNote, overrideStale, useSnapshotCloud, role }: {
  data: GuideData; guideIndex: GuideIndex; cloud: string; setCloud: (c: string) => void; cloudNote: string;
  overrideStale: boolean; useSnapshotCloud: () => void; role?: string | null;
}) {
  const allTables = tablesFor(Object.keys(guideIndex), guideIndex);
  const homeGroups = homeGroupsForRole(role);
  const startSlug = GUIDE_UNIVERSAL_SECTIONS[0].slug;
  const startTitle = GUIDE_UNIVERSAL_SECTIONS[0].title;
  // Same split as assessedCounts() (primitives.tsx) and All findings' own Reference note --
  // is_finding false is a reference list (no pass/fail), not a second kind of check.
  const checkCount = data.findings.filter((f) => f.is_finding).length;
  const referenceCount = data.findings.length - checkCount;

  return (
    <div className="gd-home">
      <div className="gd-home-top">
        <div className="gd-home-intro">
          <div className="gd-eyebrow-row">
            <span className="gd-eyebrow">{`How it works · ${fmtInt(checkCount)} checks + ${fmtInt(referenceCount)} reference lists · ${allTables.length} system tables`}</span>
            <CloudSwitch cloud={cloud} setCloud={setCloud} cloudNote={cloudNote} overrideStale={overrideStale} useSnapshotCloud={useSnapshotCloud} />
          </div>
          <h1 className="gd-display">What every tab shows, where its numbers come from, and what to do when they look wrong.</h1>
        </div>
        <div className="gd-rules">
          <div className="gd-rules-h">Three rules for every screen</div>
          <ol>{GUIDE_RULES.map((r, i) => <li key={i}><span className="gd-rules-n">{i + 1}</span><span>{r}</span></li>)}</ol>
        </div>
      </div>

      <section aria-labelledby="gd-brought" className="gd-section">
        <h2 id="gd-brought">What brought you here?</h2>
        <div className="gd-entry-grid">
          <a className="gd-card gd-entry" {...guideLinkProps(`how-${startSlug}`)}>
            <span className="gd-eyebrow">New here</span>
            <span className="gd-entry-h">{`Read the ${GUIDE_UNIVERSAL_SECTIONS.length} start-here pages in order`}</span>
            <span className="muted">Each page ends with a link to the next.</span>
            <span className="gd-entry-link">{`Start with "${startTitle}" →`}</span>
          </a>
          <a className="gd-card gd-entry" {...guideLinkProps("how-troubleshooting")}>
            <span className="gd-eyebrow">Something looks off</span>
            <span className="gd-entry-h">A panel is empty or a number looks wrong</span>
            <span className="muted">Follow the status word on the panel to its cause and fix.</span>
            <span className="gd-entry-link">Open Troubleshooting &rarr;</span>
          </a>
          <a className="gd-card gd-entry" {...guideLinkProps("how-checks")}>
            <span className="gd-eyebrow">Looking something up</span>
            <span className="gd-entry-h">Find one check, table or term</span>
            <span className="muted">{`Press ${isMac() ? "⌘K" : "Ctrl+K"} to search. Try "idle", "billing.usage" or "DBU".`}</span>
            <span className="gd-entry-link">Open the check reference &rarr;</span>
          </a>
        </div>
      </section>

      <section aria-labelledby="gd-tabs" className="gd-section">
        <h2 id="gd-tabs">Every tab, in the order the app shows them</h2>
        {homeGroups.map((group) => (
          <div key={group.name} className="gd-tabgroup">
            <div className="gd-nav-h">{group.name}</div>
            <div className="gd-tabcard-grid">
              {group.items.map((k) => {
                const area = guideArea(k) || AREA_REGISTRY[k];
                if (!area) return null;
                const g = GUIDE_TABS[k];
                const ids = tabCheckIds(k).filter((id) => guideIndex[id]);
                // A tab with no GUIDE_TABS entry of its own (only "findings" today) has no count
                // distinct from its one-line "what" -- show the count chip only when it says
                // something the description below does not already say.
                const count = ids.length ? `${ids.length} check${ids.length === 1 ? "" : "s"}` : (g && g.queries ? `${g.queries.length} queries to run` : null);
                const what = g ? (g.card || g.lede) : area.scope;
                const link = g ? guideLinkProps(tabGuideId(k)) : appTabLinkProps(k, null);
                return (
                  <a key={k} className="gd-card gd-tabcard" {...link}>
                    <span className="gd-tabcard-top"><b>{area.label}</b>{count && <span className="muted gd-small">{count}</span>}</span>
                    <span className="muted gd-small">{what}</span>
                    <span className="gd-tabcard-link">{g ? "Open guide \u2192" : `Open ${area.label} \u2192`}</span>
                  </a>
                );
              })}
            </div>
          </div>
        ))}
        <p className="muted gd-small">Actions and the CFO page re-list checks other tabs already own, so these counts don't add to {data.findings.length}.</p>
      </section>

      <section aria-labelledby="gd-status" className="gd-section">
        <div className="gd-section-head"><h2 id="gd-status">What each status means</h2><span className="muted gd-small">Only OK is a pass</span></div>
        <div className="gd-legend-row">
          {STATUS_LEGEND.map((r) => <span key={r.kind + r.word} className="gd-legend-item"><Badge kind={r.kind}>{r.word}</Badge><span className="muted gd-small">{r.short}</span></span>)}
          <a className="gd-legend-more" {...guideLinkProps("how-status")}>All 8 explained &rarr;</a>
        </div>
        <div className="muted gd-small gd-legend-note">Rows below a materiality floor read OK and are left out of every count.</div>
      </section>

      {data.app.docs_error && <div className="muted guide2-footnote">Documentation links unavailable: {data.app.docs_error}</div>}
    </div>
  );
}

// ═══════════════════════════════════ Topic page ═══════════════════════════════════

// A 1-2 line, numbers-first summary per Start-here topic (the teaser above it is the plain-words
// version; this is the actual figures). The full prose (renderHowBody) moves behind <details> --
// short first, full formula on request.
function topicBullets(slug: string, app: GuideApp): React.ReactNode[] | null {
  switch (slug) {
    case "flow": {
      const direct = app.direct_export;
      return [
        "Every check names the table(s) it reads, on its own page.",
        direct
          ? `Computed on ${(direct.as_of || "").slice(0, 10)}.`
          : app.as_of ? `Data as of ${app.as_of}.` : "No snapshot captured yet.",
      ];
    }
    case "window":
      return [`Rolling window: ${app.window_options.join("/")} days, up to the export.`];
    case "floors":
      return [
        "A row under its $ or unit floor reads OK, not Critical or Warn.",
        app.materiality_floors_configured != null ? `${app.materiality_floors_configured} checks have a floor today.` : "Set per check in config/materiality.yml.",
      ];
    case "money":
      return [
        `Every dollar is usage × list price (effective discount ${fmtPct((app.discount_pct || 0) * 100)}).`,
        "Possible waste is a lower bound, never a forecast.",
      ];
    case "tags":
      return [
        app.mandatory_tag_keys && app.mandatory_tag_keys.length ? `Mandatory tags: ${app.mandatory_tag_keys.join(", ")}.` : "Mandatory tags are set in config, up to 5.",
        `A workspace tag needs ≥${fmtPct((app.tag_coverage_floor || 0) * 100, 0)} coverage and one value ≥${fmtPct((app.tag_share_floor || 0) * 100, 0)} share.`,
      ];
    case "coverage":
      return ["Coverage & Gaps lists data sources, not-assessed checks and known limits."];
    default:
      return null;
  }
}

// "status" (a compact grid already) and "names" (one sentence already) need no <details> wrapper;
// every other Start-here topic's prose is long enough to belong behind one.
const TOPIC_DETAILS_SLUGS = new Set(["flow", "window", "floors", "money", "tags", "coverage"]);

// Previous/next within the same sequence a topic belongs to -- Start-here topics in their own
// reading order, a Coverage sub-topic among its siblings only.
function topicNav(slug: string): { prev: { slug: string; title: string } | null; next: { slug: string; title: string } | null } {
  const list = slug.indexOf("coverage-") === 0 ? GUIDE_COVERAGE_SUBSECTIONS : GUIDE_UNIVERSAL_SECTIONS;
  const i = list.findIndex((s) => s.slug === slug);
  return { prev: i > 0 ? list[i - 1] : null, next: i >= 0 && i < list.length - 1 ? list[i + 1] : null };
}

function GuideTopicPage({ slug, data, guideIndex, cloud }: { slug: string; data: GuideData; guideIndex: GuideIndex; cloud: string }) {
  // GuideTab opens this page only for a known slug.
  const section = GUIDE_SECTIONS.find((s) => s.slug === slug)!;
  const isCoverage = slug.indexOf("coverage-") === 0;
  const bullets = topicBullets(slug, data.app);
  const body = renderHowBody(slug, data.app, data.findings);
  const { prev, next } = topicNav(slug);
  return (
    <GuidePage current={`how-${slug}`}>
      <Crumbs items={isCoverage ? [{ label: "Tabs" }, { label: AREA_REGISTRY.coverage.label, focus: tabGuideId("coverage") }] : [{ label: "Start here" }]} />
      <h1 className="gd-title">{section.title}</h1>
      <p className="gd-lede">{slug === "flow" && !data.app.direct_export ? "Your own credentials read the system tables into local files. Nothing is sent anywhere else." : section.teaser}</p>
      {bullets && <ul className="gd-bullets">{bullets.map((b, i) => <li key={i}>{b}</li>)}</ul>}
      {TOPIC_DETAILS_SLUGS.has(slug) ? (
        <details className="gd-details gd-topic-details">
          <summary>Full detail</summary>
          <div className="gd-prose">{body}</div>
        </details>
      ) : (
        <div className="gd-prose">{body}</div>
      )}
      {(data.sections[slug] || []).length > 0 && (
        <div className="guide2-topic-docs">
          {data.sections[slug].map((k) => <DocsLink key={k} docsKey={k} docs={data.docs} cloud={cloud} />)}
        </div>
      )}
      {(prev || next) && (
        <nav aria-label="Previous and next" className="gd-prevnext">
          {prev ? <a className="gd-pn gd-pn-prev" {...guideLinkProps(`how-${prev.slug}`)}><span className="muted gd-small">← Previous</span><span>{prev.title}</span></a> : <span />}
          {next ? <a className="gd-pn gd-pn-next" {...guideLinkProps(`how-${next.slug}`)}><span className="muted gd-small">Next →</span><span>{next.title}</span></a> : <span />}
        </nav>
      )}
    </GuidePage>
  );
}

// ═══════════════════════════════════ Tab page ═══════════════════════════════════

function GuideTabPage({ areaKey, data, guideIndex, findingsById, meta }: {
  areaKey: string; data: GuideData; guideIndex: GuideIndex; findingsById: Record<string, FindingSummary>; meta: Meta | null | undefined;
}) {
  // GuideTab opens this page only for an area guideArea knows.
  const area = guideArea(areaKey)!;
  const g = GUIDE_TABS[areaKey];
  const ids = tabCheckIds(areaKey).filter((id) => guideIndex[id]);
  const tables = ids.length ? tablesFor(ids, guideIndex) : (g.tables || []);
  const findingIds = ids.filter((id) => guideIndex[id].is_finding);
  const refIds = ids.filter((id) => !guideIndex[id].is_finding);
  const ran = ids.filter((id) => checkRan(findingsById[id])).length;
  const flagged = ids.filter((id) => checkFlagged(findingsById[id])).length;
  const subLabel = (key: string) => {
    const sub = (area.subtabs || []).find((s) => s.key === key);
    return (sub && sub.label) || key;
  };
  const metastoreWide = area.scope && area.scope.indexOf("Metastore-wide") === 0;
  const refreshed = meta && meta.generated_at ? fmtSnapshotWhen(meta.generated_at) : null;

  const rail = (
    <React.Fragment>
      <RailCard title="On this page">
        {["What this tab answers", "What's on the screen", "How the numbers get here", ids.length || !g.queries ? "Checks on this tab" : "Queries to run yourself", "When something looks wrong", "Known limitations"].map((h, i) => (
          <a key={h} className="gd-rail-link" href={`#gd-s${i}`} onClick={(e) => { e.preventDefault(); const el = document.getElementById(`gd-s${i}`); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }}>{h}</a>
        ))}
      </RailCard>
      <RailCard title="This build">
        {/* A tab with no registry check of its own (ids.length 0, e.g. Tags) has nothing to
        report "ran" or "flagged" -- only the build date applies. */}
        {ids.length > 0 && <div className="gd-kv"><span>Checks ran</span><b className="mono">{`${ran} / ${ids.length}`}</b></div>}
        {ids.length > 0 && <div className="gd-kv"><span>Checks flagged</span><b className="mono">{flagged}</b></div>}
        {refreshed && <div className="gd-kv"><span>Checks last built</span><b className="mono">{refreshed}</b></div>}
        <a className="ck-link" {...appTabLinkProps("coverage", "sources")}>Coverage & Gaps &rarr;</a>
      </RailCard>
      {tables.length > 0 && (
        <RailCard title="Grants needed">
          <div className="muted gd-small">USE and SELECT on</div>
          <TableChips tables={schemasFor(tables)} />
        </RailCard>
      )}
      <a className="gd-open-btn" {...appTabLinkProps(areaKey, null)}>{`Open ${area.label} →`}</a>
    </React.Fragment>
  );

  return (
    <GuidePage current={tabGuideId(areaKey)} rail={rail}>
      <Crumbs items={[{ label: "Tabs" }, { label: area.label }]} />
      <h1 className="gd-title">{area.label}</h1>
      <JumpChips sections={[
        { id: "gd-s0", label: "What it answers" }, { id: "gd-s1", label: "On the screen" }, { id: "gd-s2", label: "Where numbers come from" },
        { id: "gd-s3", label: ids.length || !g.queries ? "Checks" : "Queries" }, { id: "gd-s4", label: "When it looks wrong" }, { id: "gd-s5", label: "Limits" },
      ]} />
      <p className="gd-lede">{g.lede}</p>
      <div className="gd-chips">
        <span className="gd-chip">{`In lenses: ${lensesForArea(areaKey) || g.lens}`}</span>
        <span className="gd-chip">{ids.length || !g.queries ? `${ids.length} check${ids.length === 1 ? "" : "s"}` : `${g.queries.length} queries to run`}</span>
        <span className="gd-chip">Window: 7 / 30 / 90 days</span>
        {metastoreWide && <span className="gd-chip warn">Whole metastore</span>}
      </div>

      <h2 className="gd-h2" id="gd-s0">What this tab answers</h2>
      <div className="gd-qgrid">
        {g.questions.map((q, i) => (
          <div key={i} className="gd-card gd-q"><span className="gd-eyebrow">{`Q${i + 1}`}</span><span>{q}</span></div>
        ))}
      </div>

      <h2 className="gd-h2" id="gd-s1">What's on the screen</h2>
      <ol className="gd-numlist">
        {g.screen.map((s, i) => (
          <li key={i}>
            <span className="gd-num">{i + 1}</span>
            <span>
              {s.sub
                ? <a className="gd-strong-link" {...appTabLinkProps(areaKey, s.sub)}>{subLabel(s.sub)}</a>
                : <b>{s.t}</b>}
              {` -- ${s.d}`}
            </span>
          </li>
        ))}
      </ol>

      <h2 className="gd-h2" id="gd-s2">How the numbers get here</h2>
      <p className="muted gd-small">Run with your credentials; only result rows are stored (<a {...guideLinkProps("how-flow")}>how</a>).</p>
      <div className="gd-flow">
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">1 · Read</span><b>System tables</b>{tables.length > 0 ? <TableChips tables={tables} max={8} /> : <span className="muted">No system table of its own.</span>}</div>
        <span className="gd-flow-arrow" aria-hidden="true">&rarr;</span>
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">2 · Measure</span><span>{codeifyProse(g.measure)}</span></div>
        <span className="gd-flow-arrow" aria-hidden="true">&rarr;</span>
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">3 · Judge</span><span>{codeifyProse(g.judge)}</span></div>
        <span className="gd-flow-arrow" aria-hidden="true">&rarr;</span>
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">4 · Price</span><span>{g.price}</span></div>
      </div>
      {(g.extra || []).map((x, i) => (
        <React.Fragment key={i}>
          {x.h && <p className="gd-small"><b>{x.h}</b></p>}
          <ul className="gd-bullets">{x.items.map((it, j) => <li key={j}>{codeifyProse(it)}</li>)}</ul>
        </React.Fragment>
      ))}

      <div className="gd-section-head" id="gd-s3">
        <h2 className="gd-h2">{ids.length || !g.queries ? "Checks on this tab" : "Queries to run yourself"}</h2>
        <span className="muted">{ids.length || !g.queries ? `${ids.length} check${ids.length === 1 ? "" : "s"}` : `${g.queries.length} queries`}</span>
      </div>
      {ids.length > 0 && <p className="muted gd-small">Short form; open a check for the full rule and its SQL. Numbers are defaults from config/thresholds.yml.{ids.some((id) => guideIndex[id].severity_cap === "WARN") ? " Warn max = a trend alone never reads Critical." : ""}</p>}
      {g.queries && g.queries.length > 0 && (
        <div className="gd-sql-list">
          <p className="muted gd-small">This page has no checks of its own. These queries answer the same questions in a Databricks SQL editor:</p>
          {g.queries.map((q) => <RunItYourself key={q.id} queryId={q.id} windowDays={data.app.default_window} title={q.title} />)}
        </div>
      )}
      {ids.length === 0 ? (
        !g.queries && <p className="muted">This page has no checks of its own; it rolls up the others.</p>
      ) : (
        <React.Fragment>
          {findingIds.length > 0 && (
            <div className="gd-table-wrap">
              <table className="gd-table gd-checks">
                <thead><tr><th>Check</th><th>Flags when</th><th>Money</th><th>This build</th></tr></thead>
                <tbody>
                  {findingIds.map((id) => {
                    const f = guideIndex[id];
                    const s = findingsById[id];
                    const money = f.money && f.money.column;
                    return (
                      <tr key={id}>
                        <td className="gd-ct-check">
                          <a {...guideLinkProps(id)}>{getCheckLabel(id, f.title).title}</a>
                          <div className="mono muted gd-small">{wrapIdAfterUnderscore(id)}</div>
                        </td>
                        <td className="gd-small">
                          <ul className="gd-flag-bullets">{shortFlagBullets(f).map((b, i) => <li key={i}><b className={verdictCls(b.label)}>{b.label}:</b> {codeifyProse(b.text)}</li>)}</ul>
                        </td>
                        <td>{money ? <span className={`gd-chip ${isPricedWaste(id) ? "ok" : ""}`}>{isPricedWaste(id) ? "Waste" : "Priced"}</span> : <span className="muted">--</span>}</td>
                        <td className="gd-small" style={{ whiteSpace: "nowrap" }}>
                          {!s ? <span className="muted">--</span> : checkFlagged(s) ? <span className="gd-flag">Flagged</span> : checkRan(s) ? <span className="muted">Ran</span> : <span className="muted">Not assessed</span>}
                          {f.severity_cap === "WARN" && <div><span className="gd-chip" title="A trend alone never reads Critical">Warn max</span></div>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {refIds.length > 0 && (
            <p className="gd-small muted">
              Reference lists (no pass or fail):{" "}
              {refIds.map((id, i) => (
                <React.Fragment key={id}>{i > 0 && ", "}<a {...guideLinkProps(id)}>{getCheckLabel(id, guideIndex[id].title).title.replace(/, reference$/, "")}</a></React.Fragment>
              ))}
            </p>
          )}
        </React.Fragment>
      )}

      <h2 className="gd-h2" id="gd-s4">When something looks wrong</h2>
      <div className="gd-table-wrap">
        <table className="gd-table gd-trouble">
          <thead><tr><th>You see</th><th>Why it happens</th><th>What to do</th></tr></thead>
          <tbody>
            {g.troubles.map((t, i) => <tr key={i}><td><b>{t.see}</b></td><td>{t.why}</td><td>{t.doit}</td></tr>)}
          </tbody>
        </table>
      </div>

      <h2 className="gd-h2" id="gd-s5">Known limitations</h2>
      <ul className="gd-bullets">
        {g.limits.map((l, i) => <li key={i}>{l}</li>)}
        {metastoreWide && <li>Some of these checks cover the whole metastore; the workspace filter doesn't change those.</li>}
      </ul>
    </GuidePage>
  );
}

// ═══════════════════════════════════ Troubleshooting ═══════════════════════════════════

const STATUS_PATHS = [
  {
    badge: "not_assessed", word: "Not assessed", d: "The check didn't run. Never a pass.",
    causes: [
      { h: "Missing grant", d: "The row names the table it couldn't read.", doit: "Grant USE and SELECT on that system schema (below)." },
      { h: "Out of region", d: "Regional tables only hold the export region's workspaces.", doit: "An export taken in their region covers them; the app reads one region at a time." },
      { h: "Not in this export", d: "The check is newer than the export, or it errored.", doit: "Re-run the export." },
    ],
  },
  {
    badge: "reference", word: "No data", d: "It ran and saw nothing. Not a verified zero.",
    causes: [
      { h: "Nothing happened in the window", d: "No runs, queries or events of that kind.", doit: "Widen to 90 days to confirm." },
      { h: "No data (filtered)", d: "Rows exist, but your workspace or tag filters exclude them.", doit: "Clear the filters." },
    ],
  },
  {
    badge: "reference", word: "A number", d: "It has a value you don't believe.",
    steps: [
      "Press How is this worked out? next to it to see the formula and source tables.",
      "Read the region banner: some workspaces may show spend only.",
      "If another screen shows a different figure, use the table below.",
    ],
  },
];

const SCREENS_DIFFER = [
  { r: "Different window", ex: "Monthly bars use calendar months; the cards use the last 7, 30 or 90 full days.", fix: "Compare like for like." },
  { r: "Filter doesn't apply", ex: "Grants, tags, masks, lineage and table checks are metastore-wide; a workspace pick doesn't narrow them.", fix: "Read the tab's scope note." },
  { r: "Different coverage", ex: "Spend: every billed workspace. Compute, jobs, queries: export region only.", fix: "Filter to the assessed workspaces." },
  { r: "Different attribution level", ex: "Spend goes to query, then compute, workspace, account; per-query sums < warehouse bill.", fix: "Use the level that matches the question." },
  { r: "Priced vs counted", ex: "Possible waste sums only priced rows; unpriced findings are counted, not added.", fix: "Read priced and not priced separately." },
  { r: "Ranked vs judged", ex: "A spend ranking never reads Critical; checks judge against a rule.", fix: "Open the check for the rule." },
  { r: "Pricing gaps", ex: "SKU-days with no list price are left out of every dollar.", fix: "See Cost › Pricing & policy." },
];

const GRANT_SCHEMAS = [
  { s: "system.billing", d: "Spend, prices, chargeback -- every tab" },
  { s: "system.compute", d: "Warehouses, clusters, pools, node timeline" },
  { s: "system.query", d: "Busy time, costliest and failing SQL" },
  { s: "system.lakeflow", d: "Jobs, tasks, pipelines" },
  { s: "system.access", d: "Audit, lineage, network, workspace names" },
  { s: "system.serving", d: "Endpoint traffic and served entities" },
  { s: "system.storage", d: "Predictive optimization, table metrics" },
  { s: "system.information_schema", d: "Grants, tags, masks, shares" },
  { s: "system.ai_gateway", d: "AI Gateway usage" },
  { s: "system.data_classification", d: "Classified columns" },
];

function GuideTroubleshooting({ guideIndex }: { guideIndex: GuideIndex }) {
  const faqs = guideFaqs().filter((x) => !/differ between screens|Not assessed|Which grants/.test(x.q));
  return (
    <GuidePage current="how-troubleshooting">
      <Crumbs items={[{ label: "Troubleshooting" }]} />
      <h1 className="gd-title">A panel is empty, or a number looks wrong</h1>
      <p className="gd-lede">Read the status word on the panel, then follow its branch below.</p>

      <h2 className="gd-h2">Follow the status word</h2>
      <div className="gd-path">
        <div className="gd-path-start">
          <span className="gd-eyebrow">Start</span>
          <span className="gd-path-q">What does the panel say?</span>
          <span className="gd-small">The word is in the panel header or the row's status pill.</span>
        </div>
        <div className="gd-path-branches">
          {STATUS_PATHS.map((p) => (
            <div key={p.word} className="gd-card gd-path-branch">
              <div className="gd-path-word">
                <Badge kind={p.badge}>{p.word}</Badge>
                <span className="gd-small">{p.d}</span>
              </div>
              {p.causes ? (
                <div className="gd-path-causes">
                  {p.causes.map((c) => (
                    <div key={c.h} className="gd-path-cause">
                      <b>{c.h}</b>
                      <span className="muted gd-small">{c.d}</span>
                      <span className="gd-small"><b>Do: </b>{c.doit}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <ol className="gd-path-steps">{p.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
              )}
            </div>
          ))}
        </div>
      </div>

      <h2 className="gd-h2">Why two screens can show different numbers</h2>
      <div className="gd-table-wrap">
        <table className="gd-table gd-ts-table">
          <thead><tr><th>Reason</th><th>Example</th><th>How to line them up</th></tr></thead>
          <tbody>{SCREENS_DIFFER.map((x) => <tr key={x.r}><td><b>{x.r}</b></td><td>{x.ex}</td><td>{x.fix}</td></tr>)}</tbody>
        </table>
      </div>

      <div className="gd-faq-grid">
        {faqs.map((x) => (
          <div key={x.q} className="gd-card gd-faq">
            <h3>{x.q}</h3>
            <div className="muted">{x.a}</div>
          </div>
        ))}
      </div>

      <div className="gd-card gd-grants">
        <div>
          <h2 className="gd-h2 flush">Grants for missing tables</h2>
          <p className="muted">An account admin enables each system schema once. The identity running the audit then needs USE and SELECT on it.</p>
        </div>
        <div className="gd-grants-grid">
          {GRANT_SCHEMAS.map((x) => <div key={x.s}><code className="gd-code">{x.s}</code><div className="gd-small">{x.d}</div></div>)}
        </div>
      </div>
    </GuidePage>
  );
}

// ═══════════════════════════════════ Glossary and check reference ═══════════════════════════════════

function GuideGlossaryPage({ guideIndex }: { guideIndex: GuideIndex }) {
  return (
    <GuidePage current="how-glossary">
      <Crumbs items={[{ label: "Glossary" }]} />
      <h1 className="gd-title">Glossary</h1>
      <div className="gd-card">
        {GUIDE_GLOSSARY.map((g) => (
          <div className="guide2-glossary-row" key={g.k}>
            <div className="guide2-glossary-k">{g.k}</div>
            <div className="muted">{g.d}</div>
          </div>
        ))}
      </div>
    </GuidePage>
  );
}

function GuideCheckReference({ guideIndex, findingsById }: { guideIndex: GuideIndex; findingsById: Record<string, FindingSummary> }) {
  const placed = new Set<string>();
  const groups = GUIDE_SCREEN_ORDER.filter((k) => k !== "actions" && k !== "waste" && k !== "money").map((k) => {
    const ids = tabCheckIds(k).filter((id) => guideIndex[id] && !placed.has(id));
    ids.forEach((id) => placed.add(id));
    return { key: k, label: AREA_REGISTRY[k].label, ids };
  });
  const judged = Object.values(guideIndex).filter((f) => f.is_finding).length;
  const rest = Object.keys(guideIndex).filter((id) => !placed.has(id)).sort();
  if (rest.length) groups.push({ key: "other", label: "Not on a screen", ids: rest });
  return (
    <GuidePage current="how-checks">
      <Crumbs items={[{ label: "Check reference" }]} />
      <h1 className="gd-title">Check reference</h1>
      <p className="gd-lede">{`${Object.keys(guideIndex).length} checks: ${judged} judged against a rule, ${Object.keys(guideIndex).length - judged} reference lists. Grouped by tab, with the system tables each reads.`}</p>
      <JumpChips className="gd-ref-jump" sections={groups.filter((gr) => gr.ids.length).map((gr) => ({ id: `ref-${gr.key}`, label: `${gr.label} · ${gr.ids.length}` }))} />
      {groups.filter((gr) => gr.ids.length).map((gr) => (
        <section key={gr.key} className="gd-section">
          <div className="gd-section-head" id={`ref-${gr.key}`}>
            <h2 className="gd-h2 flush">{gr.label}</h2>
            <span className="muted">{`${gr.ids.length} check${gr.ids.length === 1 ? "" : "s"}`}</span>
          </div>
          <div className="gd-table-wrap">
            <table className="gd-table gd-ref-table">
              <thead><tr><th>Check</th><th>Reads</th><th className="gd-ref-status">This run</th></tr></thead>
              <tbody>
                {gr.ids.map((id) => {
                  const f = guideIndex[id];
                  const s = findingsById[id];
                  return (
                    <tr key={id}>
                      <td className="gd-ref-name"><a {...guideLinkProps(id)}>{getCheckLabel(id, f.title).title.replace(/, reference$/, "")}</a><div className="mono muted gd-small">{id}</div></td>
                      <td><TableChips tables={(f.reads || []).filter((t) => String(t).indexOf("system.") === 0)} max={4} /></td>
                      <td className="gd-small gd-ref-status">{!f.is_finding ? <span className="muted">Reference</span> : !s ? "" : checkFlagged(s) ? <span className="gd-flag">Flagged</span> : checkRan(s) ? <span className="muted">Ran</span> : <span className="muted">Not assessed</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </GuidePage>
  );
}

// ═══════════════════════════════════ Check page ═══════════════════════════════════

// grainText -- summary_line()'s "Each row is ..." sentence (app/api/guide.py), with that lead-in
// dropped, so the article can show it as a labelled "Grain: ..." line instead of a repeated clause.
function grainText(summary: string | null | undefined): string | null {
  if (!summary) return null;
  return String(summary).replace(/^Each row is\s*/, "").replace(/\.\s*$/, "");
}

function ReviewerDetails({ f }: { f: GuideFinding }) {
  const [openKey, setOpenKey] = React.useState<string | null>(null);
  const rows: { key: string; label: string; summary: string; body: React.ReactNode }[] = [
    { key: "confidence", label: "Confidence", summary: f.confidence === "needs_confirmation" ? "Needs confirmation" : "Confirmed", body: codeifyProse(f.confidence_note) },
  ];
  if (f.library_corrections && f.library_corrections.length > 0) {
    rows.push({
      key: "issues", label: "Known issues", summary: `${f.library_corrections.length}`, body: (
        <div>
          {f.library_corrections.map((c) => (
            <div key={c.id} className="guide2-issue">
              <Badge kind={c.status === "fixed" ? "ok" : "warn"}>{c.status === "fixed" ? "Corrected" : "Open"}</Badge>
              <div><b>Problem: </b>{c.problem}</div>
              <div><b>Effect: </b>{c.effect}</div>
              <div><b>{c.status === "fixed" ? "Fix: " : "Fix or plan: "}</b>{c.fix}</div>
            </div>
          ))}
        </div>
      ),
    });
  }
  rows.push({ key: "query", label: "Query", summary: `${f.query_id} · ${f.origin || "unknown origin"}${f.library_corrections && f.library_corrections.length ? "" : " · no known issues"}`, body: null });

  return (
    <section className="guide2-sec">
      <div className="card guide2-reviewer">
        <div className="muted guide2-reviewer-head">For reviewers and admins</div>
        {rows.map((r) => {
          const open = openKey === r.key;
          return (
            <div className="guide2-reviewer-row" key={r.key}>
              <button type="button" className="guide2-reviewer-toggle" onClick={() => setOpenKey(open ? null : r.key)} aria-expanded={open}>
                <span className="caret">{open ? "▾" : "▸"}</span>
                <span className="guide2-reviewer-label">{r.label}</span>
                <span className="muted guide2-reviewer-summary">{r.summary}</span>
              </button>
              {open && r.body && <div className="guide2-reviewer-body">{r.body}</div>}
            </div>
          );
        })}
      </div>
    </section>
  );
}

// One live row from the current build: the worst flagged row, or the first row of a list.
function ExampleRow({ f, filters }: { f: GuideFinding; filters: GuideFilters }) {
  const dims = useDims();
  const statuses = f.is_finding ? ["CRITICAL", "WARN"] : undefined;
  const st = useFindingData(f.query_id, filters.window, filters.workspaceIds, filters.envs, 1, statuses);
  if (st.phase === "loading") return <p className="muted">Loading a row...</p>;
  const rows = (st.data && st.data.rows) || [];
  if (st.phase !== "ready" || rows.length === 0) {
    return <p className="muted">{f.is_finding ? "No flagged row in this export for the current filters." : "No row in this export for the current filters."}</p>;
  }
  const d = st.data;
  const row = rows[0];
  const plan = planColumns(d.columns, rows, d.discount_pct, d.order_by);
  const byName = (n: string) => plan.visible.find((c) => c.name === n);
  const cells: PlannedColumn[] = [];
  const add = (c: PlannedColumn | null | undefined) => { if (c && cells.length < 5 && !cells.some((x) => x.name === c.name)) cells.push(c); };
  const entity = plan.entity ? byName(plan.entityDisplayCol || plan.entity) || byName(plan.entity) : null;
  add(entity);
  add(plan.keyMetric);
  plan.visible.filter((c) => c.numeric && c.name !== "status").forEach((c) => { if (cells.length < 4) add(c); });
  const status = row.status;
  const cellText = (c: PlannedColumn) => {
    if (entity && c.name === entity.name && plan.entity) return entityDisplayName(plan.entity, row, dims) || String(row[c.name]);
    return fmtCell(row[c.name], c.unit === "$" ? "money" : c.kind, row);
  };
  return (
    <div className="gd-card gd-example">
      {cells.map((c) => (
        <div key={c.name} className="gd-example-cell">
          <span className="muted gd-small">{c.label}{c.unit && !String(cellText(c)).includes(c.unit) ? ` (${c.unit})` : ""}</span>
          <span className={`mono gd-example-v${c.kind === "money" && status && status !== "OK" ? " tone-crit" : ""}`}>{cellText(c)}</span>
          {entity && c.name === entity.name && row.workspace_id != null && plan.entity !== "workspace_id" && (
            <span className="muted gd-small">{wsPhrase(row.workspace_id)}</span>
          )}
        </div>
      ))}
      {status && (
        <div className="gd-example-cell">
          <span className="muted gd-small">Status</span>
          <Badge kind={String(status).toLowerCase()}>{status === "WARN" ? "Warn" : status === "CRITICAL" ? "Critical" : status === "OK" ? "OK" : status}</Badge>
        </div>
      )}
    </div>
  );
}

// Which of GuideArticle's own sections will render for this check -- read by the article itself
// (for its headings) and by CheckRail (for the "On this page" outline), so the two can never list
// a section the other one didn't draw.
interface ArticleSection { id: string; label: string }
function articleSections(f: GuideFinding): ArticleSection[] {
  const hasSteps = true;
  const hasFlags = f.is_finding || f.params.some((p) => p.name !== "period_days" && p.name !== "top_n");
  const hasRungs = (f.actions || []).some((a) => String(a).trim() && !String(a).trim().toLowerCase().startsWith("n/a"));
  const hasOff = splitSentences(f.caveats).length > 0 || f.not_assessed_reasons.length > 0 || f.empty_if_words.length > 0;
  const out: ArticleSection[] = [];
  if (hasSteps) out.push({ id: "worked", label: "How it's worked out" });
  if (hasFlags) out.push({ id: "flags", label: "When it flags" });
  out.push({ id: "example", label: f.is_finding ? "A flagged row" : "A row from this export" });
  if (hasRungs) out.push({ id: "todo", label: "What to do" });
  if (hasOff) out.push({ id: "off", label: "Why the number could be off" });
  out.push({ id: "sql", label: "Run it yourself" });
  if (f.columns && f.columns.length > 0) out.push({ id: "cols", label: `Columns (${f.columns.length})` });
  return out;
}

// The order the Guide nav already shows for this area (About this tab, then every check, grouped
// by sub-tab) -- Previous/Next on a check page reuses the exact same order.
function checkSequence(areaKey: string, guideIndex: GuideIndex): string[] {
  return [tabGuideId(areaKey), ...guideSubtabGroups(areaKey, guideIndex).flatMap((g) => g.ids)];
}

// A table name may break after a dot, never mid-word.
function wrapKey(key: string): React.ReactNode[] {
  return key.split(".").flatMap((p, i, a) => (i < a.length - 1 ? [`${p}.`, <wbr key={i} />] : [p]));
}

function CheckRail({ f, data, cloud, reads, related }: {
  f: GuideFinding; data: GuideData; cloud: string; reads: string[]; related: { query_id: string; title: string | null; if: string }[];
}) {
  return (
    <React.Fragment>
      <RailCard title="On this page">
        {articleSections(f).map((s) => (
          <a key={s.id} className="gd-rail-link" href={`#${s.id}`} onClick={(e) => { e.preventDefault(); const el = document.getElementById(s.id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }}>{s.label}</a>
        ))}
      </RailCard>
      {reads.length > 0 && (
        <RailCard title="Reads">
          <div className="gd-rail-reads">
            {reads.map((key) => {
              const entry = data.docs[key];
              const link = entry && entry.urls[cloud];
              return link
                ? <a key={key} className="gd-code" href={link.url} target="_blank" rel="noreferrer" title={(entry && entry.what) || ""}>{wrapKey(key)}</a>
                : <code key={key} className="gd-code" title={(entry && entry.what) || ""}>{wrapKey(key)}</code>;
            })}
          </div>
          <div className="muted gd-small">If one is missing, this check reads Not assessed, never OK.</div>
        </RailCard>
      )}
      {related.length > 0 && (
        <RailCard title="Related checks">
          {related.map((n) => <a key={n.query_id} className="gd-rail-link" title={desnakeProse(n.if) || ""} {...guideLinkProps(n.query_id)}>{n.title}</a>)}
        </RailCard>
      )}
    </React.Fragment>
  );
}

// The SQL behind a check (or a Tags page query), fetched when opened, with a Copy button.
function RunItYourself({ queryId, windowDays, title }: { queryId: string; windowDays: number; title?: string }) {
  const [state, setState] = React.useState<{ phase: string; sql: string | null }>({ phase: "idle", sql: null });
  const [copied, setCopied] = React.useState(false);
  React.useEffect(() => { setState({ phase: "idle", sql: null }); }, [queryId, windowDays]);
  const load = () => {
    if (state.phase !== "idle") return;
    setState({ phase: "loading", sql: null });
    Api.guideSql(queryId, windowDays)
      .then((d) => setState({ phase: "ready", sql: d.sql }))
      .catch(() => setState({ phase: "missing", sql: null }));
  };
  const copy = () => {
    try {
      navigator.clipboard.writeText(state.sql || "").then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); });
    } catch (e) { /* clipboard blocked: the text is still selectable */ }
  };
  return (
    <details className="gd-details gd-sql" onToggle={(e) => { if (e.currentTarget.open) load(); }}>
      <summary>{title || `The SQL for the last ${windowDays} days`}</summary>
      {state.phase === "loading" && <div className="muted gd-small">Loading...</div>}
      {state.phase === "missing" && <div className="muted gd-small">No Databricks SQL for this check in this version.</div>}
      {state.phase === "ready" && (
        <React.Fragment>
          <div className="gd-sql-bar">
            <span className="muted gd-small">Paste into a Databricks SQL editor. It only reads system tables.</span>
            <button type="button" className="ck-link" style={{ whiteSpace: "nowrap" }} onClick={copy}>{copied ? "Copied" : "Copy SQL"}</button>
          </div>
          <pre className="gd-sql-code"><code>{state.sql}</code></pre>
        </React.Fragment>
      )}
    </details>
  );
}

function GuideArticle({ f, data, filters, home, area, guideIndex, findingsById }: {
  f: GuideFinding; data: GuideData; filters: GuideFilters; home: Home; area: Area | null;
  guideIndex: GuideIndex; findingsById: Record<string, FindingSummary>;
}) {
  const label = getCheckLabel(f.query_id, f.title);
  const summary = label.why || f.summary;
  const sub = area && home.subtab ? area.subtabs.find((s) => s.key === home.subtab) : undefined;
  const subLabel = sub ? sub.label : null;
  const moneyCol = f.money && f.money.column;
  const isWaste = isPricedWaste(f.query_id);
  const wasteItem = WASTE_ITEMS.find((w) => w.id === f.query_id);
  const windowChip = f.period_kind === "days" ? "Window check" : f.period_kind === "fixed" ? "Own period" : "Snapshot";
  const [copied, setCopied] = React.useState(false);

  // "This build": the same wording the old "At a glance" rail card used, now one of the three
  // headline numbers up top instead of a field buried in the right rail.
  const s = findingsById[f.query_id];
  let buildLine = "Not in this export";
  if (s && checkRan(s)) {
    const a: Partial<Affected> = s.affected || {};
    if (f.is_finding && a.total != null) buildLine = `${fmtInt(a.flagged || 0)} of ${fmtInt(a.total)} ${a.noun || "rows"} flagged`;
    else buildLine = s.row_count != null ? `${fmtInt(s.row_count)} row${s.row_count === 1 ? "" : "s"}` : "Ran";
  } else if (s) {
    buildLine = "Not assessed";
  }

  const grain = grainText(f.summary);
  // read_this's own first sentence already names the grain above, so this step starts at its
  // second sentence; only the FIRST of those is shown inline, the rest sits behind one "Full
  // detail" <details> under the steps list.
  const readSentences = splitSentences(f.read_this).slice(1);
  const readFirst = readSentences[0] ? readSentences[0].replace(/\s*\(default\)/g, "") : null;
  const params = f.params.filter((p) => p.name !== "period_days" && p.name !== "top_n");
  const readTables = (f.reads || []).filter((t) => String(t).indexOf("system.") === 0).map(shortTable);
  const discount = data.app.discount_pct || 0;
  // Always four steps; a step with no check-specific data falls back to generic wording.
  const steps: { h: string; t: React.ReactNode }[] = [
    {
      h: "Reads.",
      t: grain ? `One row is ${grain.replace(/\s*\(default\)/g, "")}.` : readTables.length ? `System tables ${readTables.slice(0, 3).join(", ")}${readTables.length > 3 ? ` +${readTables.length - 3} more` : ""}.` : "One row per item the check lists.",
    },
    {
      h: moneyCol ? "Dollars." : "Computes.",
      t: moneyCol
        ? (wasteItem ? wasteItem.why : `Usage in the window × list price (effective)${discount > 0 ? `, today's discount ${fmtPct(discount * 100)}` : ""}. Never your invoice or your cloud VM bill.`)
        : (readFirst ? codeifyProse(readFirst) : "Counts the rows that match. No dollar column."),
    },
    {
      h: "Window.",
      t: f.period_kind === "days" ? `The last ${filters.window} days, up to the export.` : f.period_kind === "fixed" ? "Its own fixed period, not the window you pick." : "The current state. No window.",
    },
    {
      h: "Judges.",
      t: !f.is_finding ? "A reference list. No pass or fail." : `Each row is compared with the ${f.severity_cap === "WARN" ? "Warn" : "Warn and Critical"} limits below.`,
    },
  ];
  const stepsMore = [...(moneyCol && readFirst ? [readFirst] : []), ...readSentences.slice(1)];

  // "Reads OK when": the first clause only; the rest goes behind "Why the number could be off".
  const healthyFull = f.is_finding && f.healthy ? cleanFlagPrefix(f.healthy) : "";
  const healthyShort = healthyFull ? firstClause(healthyFull) : "";
  const healthyRest = healthyFull && healthyFull.length > healthyShort.length + 2 ? healthyFull : "";
  const flagBullets = shortFlagBullets(f);
  const capped = f.severity_cap === "WARN";

  // Measure | Warn | Critical: params named *_warn / *_crit pair up; a lone value goes under Warn,
  // never a made-up Critical number.
  const flagTable: { measure: string; hint: string | null; warn: React.ReactNode; crit: React.ReactNode }[] = [];
  const paired: Record<string, { measure: string; hint: string | null; warn?: string; crit?: string }> = {};
  const lone: typeof params = [];
  params.forEach((p) => {
    const m = /^(.*?)_?(warn|warning|crit|critical)(?:_(.*))?$/.exec(p.name);
    if (!m) { lone.push(p); return; }
    const base = [m[1], m[3]].filter(Boolean).join("_") || p.name;
    const e = paired[base] || (paired[base] = { measure: paramLabel(base), hint: p.meaning });
    if (m[2].indexOf("warn") === 0) e.warn = String(p.value); else e.crit = String(p.value);
  });
  Object.values(paired).forEach((e) => flagTable.push({ measure: e.measure, hint: e.hint, warn: e.warn || "—", crit: e.crit || "—" }));
  lone.forEach((p) => flagTable.push({ measure: paramLabel(p.name), hint: p.meaning, warn: <span className="mono">{String(p.value)}</span>, crit: "—" }));
  const showCrit = !capped && flagTable.some((r) => r.crit !== "—");

  const rungs = (f.actions || [])
    .map((a) => String(a).trim())
    .filter((a) => a && !a.toLowerCase().startsWith("n/a"))
    .map((a) => ({ tier: rungTier(a), parts: splitRungCases(rungText(a)) }));
  const caveats = [...(healthyRest ? [healthyRest] : []), ...splitSentences(f.caveats)];
  const offLists: { h: string; list: string[] }[] = [
    ...(f.not_assessed_reasons.length ? [{ h: "Not assessed when", list: f.not_assessed_reasons }] : []),
    ...(f.empty_if_words.length ? [{ h: "No data when", list: f.empty_if_words }] : []),
  ];
  const offCount = caveats.length + offLists.length;
  const offItem = (c: string, i: number) => {
    const short = shortClause(c);
    return short === c.replace(/[\s;.,]+$/, "")
      ? <li key={i}>{codeifyProse(c)}</li>
      : <li key={i}><details><summary>{codeifyProse(short)}</summary>{codeifyProse(short.endsWith("…") || !c.startsWith(short) ? c : upperFirst(c.slice(short.length).replace(/^[\s;,.\-–]+/, "")) || c)}</details></li>;
  };

  const colMeta = (f.columns || []).map((c) => ({ c, meta: columnMeta(c.name, c.kind) }));
  const showUnit = colMeta.filter((x) => x.meta.unit).length >= 3;

  const seq = area && GUIDE_TABS[home.tab] ? checkSequence(home.tab, guideIndex) : [];
  const idx = seq.indexOf(f.query_id);
  const prevId = idx > 0 ? seq[idx - 1] : null;
  const nextId = idx >= 0 && idx < seq.length - 1 ? seq[idx + 1] : null;
  const seqLabel = (id: string) => (id === tabGuideId(home.tab) ? `About the ${area!.label} tab` : (guideIndex[id] ? getCheckLabel(id, guideIndex[id].title).title : id));

  return (
    <article className="gd-article">
      <Crumbs items={[
        area ? { label: area.label, focus: GUIDE_TABS[home.tab] ? tabGuideId(home.tab) : null } : { label: "All findings" },
        ...(subLabel ? [{ label: subLabel }] : []),
      ]} />
      <h1 className="gd-title">{label.title}</h1>
      <JumpChips sections={articleSections(f)} />
      <div className="gd-chips">
        <code className="gd-code">{f.query_id}</code>
        {isWaste && <span className="gd-chip warn">Possible waste</span>}
        {moneyCol && <span className="gd-chip ok">Priced</span>}
        {!f.is_finding && <span className="gd-chip">Reference list</span>}
        <span className="gd-chip">{windowChip}</span>
        {rungs[0] && rungs[0].tier && <span className="gd-chip">{`Fix: ${rungs[0].tier}`}</span>}
      </div>
      {summary && <p className="gd-lede">{summary}</p>}
      <div className="gd-article-actions">
        <a className="gd-open-btn" {...openInAppLinkProps(f)}>{`See it in the app: ${area ? (subLabel ? `${area.label} › ${subLabel}` : area.label) : "All findings"} →`}</a>
        <button type="button" className="gd-copy-btn" onClick={() => copyGuideLink(f.query_id, () => { setCopied(true); setTimeout(() => setCopied(false), 1200); })}>
          {copied ? "Copied" : "Copy link"}
        </button>
      </div>

      <div className="gd-statrow">
        <div className="gd-statbox"><span className="muted gd-small">This build</span><span className="gd-stat-v">{buildLine}</span></div>
        {params[0] && (
          <div className="gd-statbox">
            <span className="muted gd-small" title={params[0].meaning || undefined}>{humanizeParamName(params[0].name)}</span>
            <span className="gd-stat-v">{String(params[0].value)}</span>
            <span className="muted gd-small">{params[0].source === "default" ? "default" : "config/thresholds.yml"}</span>
          </div>
        )}
        {f.period_kind === "days" && (
          <div className="gd-statbox">
            <span className="muted gd-small">Window</span>
            <span className="gd-stat-v">{`${filters.window} days`}</span>
            <span className="muted gd-small">up to the export</span>
          </div>
        )}
      </div>

      <h2 className="gd-h2" id="worked">How it's worked out</h2>
      <ol className="gd-steps">
        {steps.map((s2, i) => (
          <li key={i}><span className="gd-step-n mono">{String(i + 1).padStart(2, "0")}</span><span><b>{s2.h}</b> {s2.t}</span></li>
        ))}
      </ol>
      {stepsMore.length > 0 && (
        <details className="gd-details gd-topic-details">
          <summary>Full detail</summary>
          <ul className="gd-flag-bullets">
            {stepsMore.map((x, i) => {
              const t = x.replace(/\s*\(default\)/g, "");
              const m = /^([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b(.*)$/s.exec(t);
              return <li key={i}>{m ? <React.Fragment><b>{m[1]}</b>{codeifyProse(m[2])}</React.Fragment> : codeifyProse(t)}</li>;
            })}
          </ul>
        </details>
      )}
      {moneyCol && <p className="muted gd-small"><a {...guideLinkProps("how-money")}>How every dollar figure is worked out &rarr;</a></p>}

      {(f.is_finding || flagTable.length > 0) && (
        <React.Fragment>
          <h2 className="gd-h2" id="flags">When it flags</h2>
          {(flagBullets.length > 0 || healthyShort || capped) && (
            <ul className="gd-flag-bullets">
              {healthyShort && <li><b className="gd-v-ok">OK:</b> {codeifyProse(healthyShort)}</li>}
              {flagBullets.map((b, i) => <li key={i}><b className={verdictCls(b.label)}>{b.label}:</b> {codeifyProse(b.text)}</li>)}
              {capped && <li><b className="gd-v-crit">Critical:</b> never; a trend alone tops out at Warn</li>}
            </ul>
          )}
          {flagTable.length > 0 && (
            <div className="gd-table-wrap">
              <table className="gd-table gd-flagtable">
                <thead><tr><th>Measure</th><th>Warn</th>{showCrit && <th>Critical</th>}</tr></thead>
                <tbody>
                  {flagTable.map((r, i) => (
                    <tr key={i}><td title={r.hint || undefined}>{r.measure}</td><td className="gd-small">{r.warn}</td>{showCrit && <td className="gd-small">{r.crit}</td>}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {flagTable.length === 0 && flagBullets.length === 0 && <p className="muted gd-small">This check lists no Warn or Critical limit.</p>}
          {params.length > 0 && <p className="muted gd-small">{`Set in config/thresholds.yml. ${params.filter((p) => p.source === "default").length} of ${params.length} still on the default.`}</p>}
        </React.Fragment>
      )}

      <h2 className="gd-h2" id="example">{f.is_finding ? "A flagged row from this export" : "A row from this export"}</h2>
      <ExampleRow f={f} filters={filters} />

      {rungs.length > 0 && (
        <React.Fragment>
          <h2 className="gd-h2" id="todo">What to do</h2>
          <div className="gd-todo">
            {rungs.map((r, i) => (
              <div key={i} className="gd-card gd-todo-card">
                <span className={`gd-chip ${r.tier === "free" ? "ok" : r.tier === "spend" ? "warn" : ""}`}>{r.tier ? `${i + 1} · ${r.tier}` : `Step ${i + 1}`}</span>
                {r.parts.length > 1
                  ? <ul>{r.parts.map((x, j) => <li key={j}>{boldLead(x)}</li>)}</ul>
                  : <span>{codeifyProse(r.parts[0] || "")}</span>}
              </div>
            ))}
          </div>
        </React.Fragment>
      )}

      {offCount > 0 && (
        <React.Fragment>
          <h2 className="gd-h2" id="off">Why the number could be off</h2>
          <details className="gd-details">
            <summary>{`${fmtInt(offCount)} way${offCount === 1 ? "" : "s"} it could be off: column names, edge cases`}</summary>
            <div className="gd-off">
              {caveats.length > 0 && (
                <ul className="gd-off-list">
                  {caveats.slice(0, 6).map(offItem)}
                  {caveats.length > 6 && (
                    <li><details><summary>{`Show ${caveats.length - 6} more`}</summary><ul className="gd-off-list">{caveats.slice(6).map(offItem)}</ul></details></li>
                  )}
                </ul>
              )}
              {offLists.map((l) => (
                <div key={l.h}><b>{l.h}</b><ul className="gd-off-list">{l.list.map((x, j) => <li key={j}>{codeifyProse(x)}</li>)}</ul></div>
              ))}
            </div>
          </details>
        </React.Fragment>
      )}

      <h2 className="gd-h2" id="sql">Run it yourself</h2>
      <RunItYourself queryId={f.query_id} windowDays={filters.window} title={f.period_kind === "days" ? undefined : "The SQL"} />

      {f.columns && f.columns.length > 0 && (
        <details className="gd-details" id="cols">
          <summary>{`Columns (${f.columns.length})`}</summary>
          <table className="guide2-cols">
            <thead><tr><th>Column</th><th>Meaning</th>{showUnit && <th>Unit</th>}</tr></thead>
            <tbody>
              {colMeta.map(({ c, meta }) => (
                <tr key={c.name}>
                  <td className="mono">{c.name}</td>
                  <td>{meta.label}{!showUnit && meta.unit ? ` (${meta.unit})` : ""}{meta.help && <div className="muted">{meta.help}</div>}</td>
                  {showUnit && <td className="muted gd-col-unit">{meta.unit || "—"}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      {f.requires && f.requires.trim().toLowerCase() !== "n/a" && <p className="muted gd-small">Needs: {codeifyProse(f.requires)}</p>}
      <ReviewerDetails f={f} />

      {(prevId || nextId) && (
        <nav aria-label="Previous and next" className="gd-prevnext">
          {prevId ? (
            <a className="gd-pn gd-pn-prev" {...guideLinkProps(prevId)}><span className="muted gd-small">{`← Previous in ${area!.label}`}</span><span>{seqLabel(prevId)}</span></a>
          ) : <span />}
          {nextId ? (
            <a className="gd-pn gd-pn-next" {...guideLinkProps(nextId)}><span className="muted gd-small">{`Next in ${area!.label} →`}</span><span>{seqLabel(nextId)}</span></a>
          ) : <span />}
        </nav>
      )}
    </article>
  );
}

function GuideCheckPage({ data, guideIndex, f, cloud, filters, findingsById }: {
  data: GuideData; guideIndex: GuideIndex; f: GuideFinding; cloud: string; filters: GuideFilters; findingsById: Record<string, FindingSummary>;
}) {
  const home = homeForQuery(f);
  const area = AREA_REGISTRY[home.tab] || null;
  const related = (f.next || []).filter((n) => n.title);
  return (
    <GuidePage
      current={f.query_id}
      rail={<CheckRail f={f} data={data} cloud={cloud} reads={f.reads || []} related={related} />}
    >
      <GuideArticle key={f.query_id} f={f} data={data} filters={filters} home={home} area={area} guideIndex={guideIndex} findingsById={findingsById} />
    </GuidePage>
  );
}

// ═══════════════════════════════════ Root ═══════════════════════════════════

// Mobile only (hidden >900px by CSS): back/title/Contents/search replace the in-page nav column,
// which moves into this same bottom sheet.
function GuideMobileBar(navProps: {
  current: string; checkArea: string | null; guideIndex: GuideIndex; role?: string | null;
  backLabel: string; backHref: string; onBack: () => void; themeToggle?: React.ReactNode; openSearchSignal?: number;
}) {
  const [open, setOpen] = React.useState(false);
  const [focusSearch, setFocusSearch] = React.useState(false);
  const openerRef = React.useRef<HTMLElement | null>(null);
  const closeRef = React.useRef<HTMLButtonElement | null>(null);
  const sheetRef = React.useRef<HTMLDivElement | null>(null);
  React.useEffect(() => { setOpen(false); }, [navProps.current]);
  const closeSheet = () => {
    setOpen(false);
    if (openerRef.current && document.contains(openerRef.current)) openerRef.current.focus();
  };
  const openSheet = (e: React.MouseEvent<HTMLElement>, viaSearch: boolean) => {
    openerRef.current = e.currentTarget;
    setFocusSearch(viaSearch);
    setOpen(true);
  };
  // Ctrl/Cmd+K (App.tsx) on a narrow screen: open this sheet and focus its search, same as the
  // search icon button.
  React.useEffect(() => {
    if (!navProps.openSearchSignal) return;
    openerRef.current = null;
    setFocusSearch(true);
    setOpen(true);
  }, [navProps.openSearchSignal]);
  React.useEffect(() => {
    if (!open) return undefined;
    if (!focusSearch && closeRef.current) closeRef.current.focus();
    // Page behind the sheet must not scroll while it is open.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { closeSheet(); return; }
      if (e.key !== "Tab" || !sheetRef.current) return;
      const els = Array.from(sheetRef.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input, summary, [tabindex]:not([tabindex='-1'])"))
        .filter((el) => el.offsetParent !== null);
      if (!els.length) return;
      const first = els[0];
      const last = els[els.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (!sheetRef.current.contains(active)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = prevOverflow; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  return (
    <React.Fragment>
      <div className="gd-mbar">
        <a className="gd-mbar-back" href={navProps.backHref} aria-label={`Back to ${navProps.backLabel}`} onClick={(e) => { e.preventDefault(); navProps.onBack(); }}>
          <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3L5 8l5 5" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </a>
        <span className="gd-mbar-title">Guide</span>
        <button type="button" className="gd-mbar-contents" onClick={(e) => openSheet(e, false)}>
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4h12M2 8h12M2 12h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
          Contents
        </button>
        <button type="button" className="gd-mbar-icon" aria-label="Search the guide" onClick={(e) => openSheet(e, true)}>
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.5" fill="none" /><path d="M11 11l3.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
        </button>
      </div>
      {open && (
        <div className="gd-sheet-wrap">
          <div className="gd-sheet-backdrop" onClick={closeSheet} />
          <div role="dialog" aria-modal="true" aria-label="Guide contents" className="gd-sheet" ref={sheetRef}>
            <div className="gd-sheet-grab" aria-hidden="true" />
            <div className="gd-sheet-head">
              <span>Contents</span>
              <div className="gd-sheet-head-right">
                {navProps.themeToggle}
                <button type="button" ref={closeRef} className="gd-sheet-close" aria-label="Close" onClick={closeSheet}>
                  <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
                </button>
              </div>
            </div>
            <div className="gd-sheet-body">
              <GuideNav {...navProps} collapseLong autoFocusSearch={focusSearch} onBack={() => { closeSheet(); navProps.onBack(); }} />
            </div>
          </div>
        </div>
      )}
    </React.Fragment>
  );
}

function backAreaLabel(tab: string): string {
  if (tab === "money") return "Overview";
  if (tab === "guide") return "How it works";
  return (AREA_REGISTRY[tab] && AREA_REGISTRY[tab].label) || tab;
}

export function GuideTab({ focus, filters, findingsById, meta, role, goTo, backTo, railNode, themeToggle }: {
  focus: string | null; filters: Filters | null; findingsById?: Record<string, FindingSummary> | null; meta?: Meta | null;
  role?: string | null; goTo?: (target: { tab: string; subtab?: string | null }) => void;
  backTo?: { tab: string; subtab: string | null } | null; railNode?: HTMLElement | null; themeToggle?: React.ReactNode;
}) {
  const [data, setData] = React.useState<GuideData | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const desktopSearchRef = React.useRef<HTMLInputElement>(null);
  const [mobileSearchSignal, setMobileSearchSignal] = React.useState(0);
  // Ctrl/Cmd+K: focus the visible nav search -- the desktop rail's input directly, or open the
  // mobile Contents sheet onto its own (GuideMobileBar reads window width via CSS, this via JS).
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== "k") return;
      e.preventDefault();
      if (railNode && window.matchMedia && window.matchMedia("(min-width: 901px)").matches) {
        desktopSearchRef.current?.focus();
      } else {
        setMobileSearchSignal((n) => n + 1);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [railNode]);
  const [cloudOverride, setCloudOverride] = React.useState<string | null>(() => {
    try {
      const v = window.localStorage.getItem(GUIDE_CLOUD_STORAGE_KEY);
      return v && CLOUD_OPTIONS.includes(v) ? v : null;
    } catch (e) {
      return null;
    }
  });

  React.useEffect(() => {
    fetchGuideData().then(setData).catch((e) => setError(e.message || String(e)));
  }, []);

  const guideIndex = React.useMemo(() => {
    const idx: GuideIndex = {};
    (data ? data.findings : []).forEach((f) => { idx[f.query_id] = f; });
    return idx;
  }, [data]);

  if (error) {
    return (
      <div className="honest-card error">
        <div className="h-title">Could not load How it works</div>
        <div className="h-note mono">{error}</div>
      </div>
    );
  }
  if (!data) {
    return <div className="loading-note">Loading How it works...</div>;
  }

  const cloud = cloudOverride || data.app.cloud;
  const setCloud = (c: string) => {
    setCloudOverride(c);
    try { window.localStorage.setItem(GUIDE_CLOUD_STORAGE_KEY, c); } catch (e) { /* private window / blocked storage */ }
  };
  const overrideStale = !!cloudOverride && cloudOverride !== data.app.cloud;
  const useSnapshotCloud = () => {
    setCloudOverride(null);
    try { window.localStorage.removeItem(GUIDE_CLOUD_STORAGE_KEY); } catch (e) { /* private window / blocked storage */ }
  };
  // cloud_source (app/api/guide.py): "snapshot" (tools/snapshot.py's own metastore probe),
  // "workspace" (read off dims.dim_workspace.url's host) or "default" (fell back to AWS).
  const detectedNote = data.app.cloud_source === "snapshot" ? "from your snapshot"
    : data.app.cloud_source === "workspace" ? "from your workspace address"
      : "not detected -- defaulted";
  const cloudNote = overrideStale ? `your choice (detected: ${CLOUD_WORD[data.app.cloud] || data.app.cloud}, ${detectedNote})` : detectedNote;

  const safeFilters: GuideFilters = filters || { window: 30, workspaceIds: [], envs: [] };
  const byId: Record<string, FindingSummary> = findingsById || {};
  const topic = focus && String(focus).indexOf("how-") === 0 ? String(focus).slice(4) : null;
  const checkF = focus && !topic ? guideIndex[focus] : null;

  let content: React.ReactNode;
  let checkArea: string | null = null;
  if (topic && topic.indexOf("area-") === 0 && GUIDE_TABS[topic.slice(5)] && guideArea(topic.slice(5))) {
    checkArea = topic.slice(5);
    content = <GuideTabPage areaKey={checkArea} data={data} guideIndex={guideIndex} findingsById={byId} meta={meta} />;
  } else if (topic === "troubleshooting") {
    content = <GuideTroubleshooting guideIndex={guideIndex} />;
  } else if (topic === "glossary") {
    content = <GuideGlossaryPage guideIndex={guideIndex} />;
  } else if (topic === "checks") {
    content = <GuideCheckReference guideIndex={guideIndex} findingsById={byId} />;
  } else if (topic && GUIDE_SECTIONS.some((s) => s.slug === topic)) {
    content = <GuideTopicPage slug={topic} data={data} guideIndex={guideIndex} cloud={cloud} />;
  } else if (checkF) {
    const home = homeForQuery(checkF);
    checkArea = AREA_REGISTRY[home.tab] && GUIDE_TABS[home.tab] ? home.tab : null;
    content = <GuideCheckPage data={data} guideIndex={guideIndex} f={checkF} cloud={cloud} filters={safeFilters} findingsById={byId} />;
  } else {
    content = (
      <GuideHome
        data={data}
        guideIndex={guideIndex}
        cloud={cloud}
        setCloud={setCloud}
        cloudNote={cloudNote}
        overrideStale={overrideStale}
        useSnapshotCloud={useSnapshotCloud}
        role={role}
      />
    );
  }

  const currentId = checkF ? checkF.query_id : (topic ? `how-${topic}` : "home");
  const backTab = (backTo && backTo.tab) || "overview";
  const backSubtab = (backTo && backTo.subtab) || null;
  const backArea = AREA_REGISTRY[backTab];
  const backSub = backArea && backSubtab ? (backArea.subtabs || []).find((s) => s.key === backSubtab) : null;
  const backLabel = `${backAreaLabel(backTab)}${backSub ? ` › ${backSub.label}` : ""}`;
  const backHref = navHref({ tab: backTab, subtab: backSubtab, focus: null });
  const onBack = () => { if (goTo) goTo({ tab: backTab, subtab: backSubtab }); else window.location.hash = backHref; };
  const navProps = { current: currentId, checkArea, guideIndex, role, backLabel, backHref, onBack, themeToggle };

  return (
    <React.Fragment>
      {railNode ? createPortal(<GuideNav {...navProps} searchRef={desktopSearchRef} />, railNode) : null}
      <GuideMobileBar {...navProps} openSearchSignal={mobileSearchSignal} />
      {content}
    </React.Fragment>
  );
}


