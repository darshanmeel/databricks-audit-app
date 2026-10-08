// the "How it works" page (hash tab=guide). Renders from GET /api/guide:
// home (rules, every tab at a glance, statuses), one page per tab, one per check, topics,
// troubleshooting, glossary and the check reference. Deep links go through nav_hash.ts's navHref:
// #tab=guide&focus=<query_id> (a check), how-<slug> (a topic), how-area-<key> (a tab page),
// how-troubleshooting, how-glossary, how-checks.

import React from "react";

import { Api } from "../api";
import { fmtCell, fmtInt, fmtPct, fmtSnapshotWhen } from "../format";
import { wsPhrase } from "./names";
import { useDims, useFindingData } from "./hooks";
import { AREA_ORDER, AREA_REGISTRY, OVERVIEW_IDS, RANKED_QUERY_IDS, WASTE_IDS, WASTE_ITEMS, bandOf, homeForQuery } from "./tab_registry";
import { ROLES, ROLE_AREAS, ROLE_LABEL } from "./roles";
import { ACTION_TEMPLATES } from "./action_templates";
import { Badge, Card } from "./primitives";
import { getCheckLabel } from "./labels";
import { columnMeta, planColumns } from "./columns";
import { actionTier } from "./overview_tile";
import { entityDisplayName } from "../tabs/tab_waste";
import { guideLinkProps, navHref } from "./nav_hash";
import type { Affected, Filters, FindingSummary, LibraryCorrection, Meta } from "../types";
import type { Area, Home } from "./tab_registry";
import type { CheckLabel } from "./labels";
import type { PlannedColumn } from "./columns";

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
  represents: string;
  flags: string;
  wrong: string;
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
}

// this list's slugs, in this order, must equal app/api/guide.py's own GUIDE_SECTION_SLUGS.
const GUIDE_SECTIONS = [
  { slug: "flow", title: "Where the data comes from", teaser: "Your own credentials ran every check on Databricks; only the result rows are saved here. Nothing is sent anywhere else." },
  { slug: "window", title: "Windows and snapshots", teaser: "Most checks cover the last 7, 30 or 90 days up to the export, today partial. A snapshot check shows today's settings, not a period." },
  { slug: "status", title: "What each status means", teaser: "Critical and Warn are flagged. OK is a verified pass. Not assessed and Nothing to judge are never a pass." },
  { slug: "floors", title: "Materiality floors", teaser: "A tiny row is too small to matter -- it reads OK instead of Critical or Warn, and every count already excludes it." },
  { slug: "money", title: "How dollars are worked out", teaser: "Usage × list price (effective). Never your invoice, and never your cloud VM, storage or network bill." },
  { slug: "steps", title: "Free, config and spend steps", teaser: "Free is a setting or a read. Config changes how something is configured. Spend costs money to carry out." },
  { slug: "tags", title: "Tags and chargeback", teaser: "Spend is attributed query, then compute, then workspace, then account -- most specific tag first. A workspace tag is read from its bill. The Tags page counts what misses the mandatory tags." },
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
    represents: "The bill on one page: spend against the period before, what moved it, list price to your discount to the part a cost-allocation tag reaches, possible waste and what to fix first. The data engineer's view adds each job's state over the window and when queries queue.",
    flags: "Open the linked check or the Actions page.",
    wrong: "A jump from near zero reads as a multiple or \"new\"; workspaces outside the export region count spend only.",
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
    represents: "Flagged checks grouped into fixes: what to change, where, and what it saves.",
    flags: "Open the fix, apply the change in Databricks, re-run the export.",
    wrong: "A fix without a dollar figure has no priced source; the saving is possible waste, not a forecast.",
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
    represents: "The bill for executives: spend against the period before, possible waste, untagged spend and the biggest drivers.",
    flags: "Open the driver's own tab; tag the untagged spend.",
    wrong: "A jump from near zero reads as a multiple or \"new\"; spend with no cost-center tag can't be charged back.",
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
      { t: "Performance", d: "CTO lens only: failed queries, queue time, job reliability and jobs getting slower." },
    ],
    measure: "Net DBUs per day, workspace and SKU; the period before is the same number of days just before.",
    judge: "Nothing on this page is judged; each card adds up the checks behind it.",
    price: "Usage × list price (effective), less your what-if discount.",
    troubles: [
      { see: "A change reads 6.2× or \"new\"", why: "The previous period was near zero or empty.", doit: "Open What moved spend to see which workspace started." },
      { see: "The change is on less than the spend (\"priced in both periods\")", why: "The change is measured only on workspace-and-product rows fully priced in both periods; a row with any unpriced usage on either side is left out of the comparison, never out of the spend total.", doit: "See Cost › Pricing & policy for the usage with no list price." },
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
    represents: "Who spent what, whether it can be charged back, and which SKUs and workspaces grew.",
    flags: "Tag the owner at the source; open the spike's check.",
    wrong: "Untagged and account-level usage, SKU-days with no price, overlapping price rows, billing restatements.",
    lede: "Every dollar in the window: the trend, the product and SKU split, who spent it, and what can't be charged back.",
    questions: [
      "Which workspace, team or tag spent the most?",
      "How much spend can't be charged back?",
      "Which SKUs grew fastest?",
    ],
    screen: [
      { sub: "trend", d: "Spend by day and month with spike days marked, and a Spend / Change switch: each product's, workspace's, warehouse's, cluster's or job's total for the window, or its change against the period before. Spend by workspace, warehouse or SKU: the top 5 stacked by day or month, every one in a table, and the picked one's own daily and monthly bars." },
      { sub: "product", d: "Spend by product and SKU, the SKUs that grew, and serverless or Photon premiums." },
      { sub: "allocation", d: "The share of spend each tag or policy reaches, where each dollar lands, and each workspace's tags: which count and why the others don't." },
      { sub: "chargeback", d: "This period against the one before, by workspace, tag, warehouse, job, cluster or identity. By tag value: pick a workspace, then a tag key; only workspaces with tagged spend in the window are listed." },
      { sub: "resource", d: "Spend by cluster, warehouse, pool, job and notebook." },
      { sub: "before_after", d: "Pick the day you made a fix: each workspace's, warehouse's, job's, all-purpose cluster's or pipeline's average before it against from it, in $, DBUs, time, runs, failures, queue wait, start-up or spill, with its day-by-day chart. What changed splits the move into its drivers (more runs or costlier runs, waiting or executing, which product, which failure cause)." },
      { sub: "pricing", d: "Usage-policy and tag coverage, effective price by SKU, restatements, egress and cloud infra." },
    ],
    measure: "Usage rows joined to the list price valid for that SKU and time.",
    judge: "Spend cuts are Ranked; coverage and spike checks flag against config/thresholds.yml.",
    price: "Usage × list price (effective); unpriced SKU-days are listed, never added.",
    troubles: [
      { see: "Most spend is untagged", why: "Compute was created without the cost-center tag; account-level SKUs carry no workspace.", doit: "Enforce the tag with a cluster or budget policy." },
      { see: "A SKU is missing from the total", why: "No list price for that SKU-day.", doit: "It's listed under Pricing & policy; no dollars are guessed." },
      { see: "Last month changed", why: "Billing restated earlier usage.", doit: "Open the restatement check for the share." },
      { see: "A workspace's tag doesn't reach its queries and compute", why: "The tag is on under half of the workspace's classic compute usage, or no one value has most of it.", doit: "Allocation › Workspace tags shows each tag's share; tag the warehouses and clusters themselves." },
      { see: "Per-query dollars are below the warehouse", why: "Idle warehouse time isn't attributed to any query.", doit: "Charge back at the warehouse level." },
    ],
    limits: [
      "No negotiated-rate table: dollars are list price, less your what-if discount.",
      "All-purpose clusters are charged to the cluster's tag, not per query.",
    ],
  },
  waste: {
    lens: "FinOps · platform",
    represents: "What could stop being paid for: idle and failed work, priced where a source allows it.",
    flags: "Open the biggest offender and apply its fix.",
    wrong: "Only priced rows add up; flagged-but-unpriced checks are counted, not summed.",
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
    ],
  },
  mlai: {
    lens: "ML platform",
    represents: "What each endpoint costs against its traffic, which sit idle, and how the gateway behaves.",
    flags: "Scale to zero or delete once you confirm there are no callers.",
    wrong: "Usage tracking off reads as zero requests; endpoints not in served entities read Not assessed.",
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
    represents: "Who uses Genie, in which workspace and env, for what kind of work, and whether it is built into apps or used by people in the browser.",
    flags: "Nothing is flagged; this page shows where Genie spend goes.",
    wrong: "Accounts whose billing table has no Genie fields yet show no rows; Genie called from inside another product is billed to that product. Dollars are at list price: the free DBUs per named user a month and promotional discounts are taken off only where the bill marks the DBUs as free.",
    lede: "Genie spend from the bill, split three ways: by kind of use, by how Genie was reached, and by user and workspace.",
    questions: [
      "How much does Genie cost, and is it growing?",
      "Is Genie built into apps and agents, or used by people asking it for code and answers?",
      "Which users and workspaces use it most?",
    ],
    screen: [
      { t: "Kind of use", d: "Genie Code is the assistant: it writes SQL or Python and a person runs it. Genie agents (formerly Genie spaces) answer questions and run SQL themselves. Genie One is the chat." },
      { t: "Built in or in the browser", d: "Built into apps or agents: the channel is not UI, or it ran as a service principal. People in the browser: channel UI and a person. The channel shows as UI, or \"Non-UI · <value>\" for anything else." },
      { t: "Who, where and how", d: "Each user or service principal by workspace, env, kind and channel, with DBUs, $ and the change against the period before." },
      { t: "Workspaces, agents and days", d: "Spend by workspace with its env, the top Genie agents, and spend per day." },
    ],
    measure: "Billing rows with product GENIE: usage_metadata.genie.surface, channel and agent_id, and identity_metadata.run_as. The period before is the same number of days just before.",
    judge: "Nothing on this page is judged.",
    price: "DBUs × list price (effective). A free-tier SKU is a real $0; a SKU with no price adds nothing and is counted apart.",
    troubles: [
      { see: "No Genie rows but Genie is in use", why: "The export is older than the Genie tab, or the billing table has no Genie fields yet.", doit: "Re-run the export; check Coverage & Gaps if the check did not run." },
      { see: "Much of the spend reads Not recorded", why: "Older billing rows carry no Genie fields.", doit: "Newer rows fill in; compare a shorter window." },
      { see: "A change reads not comparable", why: "The export's billing history doesn't reach back to the period before.", doit: "Export a longer history or pick a shorter window." },
    ],
    limits: [
      "Genie agents have ids, not names, in the system tables.",
      "The SQL Genie agents run on warehouses is on Queries › Heavy queries (source Genie).",
    ],
  },
  compute: {
    lens: "Platform",
    represents: "Whether warehouses, clusters and pools are sized and stopped right.",
    flags: "Lower auto-stop, attach a cluster policy, resize for the pressure.",
    wrong: "Out-of-region workspaces aren't assessed; older clusters don't record access mode.",
    lede: "Idle time on SQL warehouses and classic clusters, auto-stop and autoscale churn, and configuration risk.",
    questions: [
      "Which warehouses run while nobody queries them, and what does that cost?",
      "Which clusters sit idle or run without a policy?",
      "Is a warehouse too small at peak? (Queries › Capacity)",
    ],
    screen: [
      { sub: "warehouses", d: "Running time split into busy, idle and waiting to auto-stop, with the idle dollars." },
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
    represents: "Which jobs fail, get slower or queue, what failures cost, and which pipelines idle.",
    flags: "Fix by termination cause; add timeouts and health rules.",
    wrong: "Slowdown needs enough runs on both sides; queue time is for single-task jobs only.",
    lede: "Failed and slow runs, what they cost, hygiene gaps, jobs on the wrong compute, and pipelines.",
    questions: [
      "Which jobs are failing, and why?",
      "Which jobs got slower?",
      "Which jobs run on all-purpose compute?",
    ],
    screen: [
      { sub: "failures", d: "One state line per job and pipeline over the window (ran fine, failed, ran slow, skipped), failed runs by cause with the top termination code's share, and the DBUs they burned." },
      { sub: "slow", d: "Every job and pipeline by average run time (top 20 or 50, all as CSV), jobs whose last 7 days ran 1.5x or longer than the rest of the window, queue time, cold starts of successful task runs, and task runs that failed while their cluster was starting." },
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
    represents: "Which SQL costs most, spills or shuffles, fails, and whether warehouses queue.",
    flags: "Tune the costliest query shapes; resize warehouses that queue.",
    wrong: "Redacted statement text pools into one shape; per-query dollars leave out idle time.",
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
      { sub: "capacity", d: "Queue waits and warehouse pressure, and the share of queries that waited for a slot by weekday and hour (UTC) and by warehouse and day." },
      { sub: "trend", d: "One SQL warehouse measure by day or month (spill, shuffle, slot wait, provisioning wait, disk and result cache, query time, queries, failures), for all warehouses and for each one, with the busiest ten as a warehouse-by-day grid." },
      { sub: "team", d: "Cost by the tag or user behind each query." },
    ],
    measure: "Each warehouse's bill is split across its statements by run time. Provisioning wait has three numbers: every query's wait for its warehouse to start added up (100 queries waiting the same 5 s read 500 s), the clock time at least one query waited (5 s), and the warehouse's own starts from system.compute.warehouse_events (STARTING to RUNNING: how many, how long, the longest, and starts that stopped before running).",
    judge: "Spill, shuffle and queue limits from config/thresholds.yml.",
    price: "Failed statements' share of warehouse DBUs × list price counts as possible waste.",
    troubles: [
      { see: "One huge redacted query", why: "Hidden statement text pools into one shape.", doit: "Read it per warehouse." },
      { see: "Query dollars below the warehouse bill", why: "Idle time isn't attributed to any statement.", doit: "Use Compute › Warehouses for idle." },
    ],
    limits: [
      "Per-query dollars are an estimate from run time, not a billed amount.",
    ],
  },
  governance: {
    lens: "Security · data governance",
    represents: "Who can reach sensitive data, where it spreads, and which admin or network events need a look.",
    flags: "Narrow grants, mask sensitive columns, tag copies.",
    wrong: "Grants, tags, masks and lineage cover the whole metastore and ignore the workspace filter. Untagged doesn't prove PII.",
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
    represents: "Which tables exist and grow, and whether table maintenance pays for itself.",
    flags: "Confirm unused tables with their owners before retiring them; fix failing predictive optimization.",
    wrong: "Table checks cover the whole metastore; a table unused in lineage may still be read outside Unity Catalog.",
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
    price: "Not priced: maintenance is shown in DBU (the predictive-optimization history carries no SKU or price); storage bytes are on your cloud bill.",
    troubles: [
      { see: "A used table reads unused", why: "It's read outside Unity Catalog lineage.", doit: "Confirm before you drop it." },
    ],
    limits: [
      "Table checks cover the whole metastore; maintenance cost follows the workspace filter.",
    ],
  },
  tags: {
    lens: "FinOps · governance · data engineering",
    represents: "Which queries, jobs, pipelines, serverless notebooks, clusters, warehouses and workspaces miss the tags every object must carry, and at which level each tag is found.",
    flags: "Tag the object or its compute; a workspace tag only covers what has neither.",
    wrong: "A workspace tag is read from the bill, so it counts only when it is on most of the workspace's billed usage.",
    lede: "The mandatory tags (settings: mandatory_tag_keys, up to 5) traced from the most specific level out: the object's own tag, the job or pipeline that ran a query, its compute, its workspace. Missing at every level is truly missing.",
    questions: [
      "How many queries, jobs, pipelines, notebooks, clusters, warehouses and workspaces miss a mandatory tag?",
      "Where does each tag come from: the object, the job that ran it, its compute, or its workspace?",
      "Where do the queries without a query tag come from (SQL editor, dashboards, jobs), and does their compute cover them?",
    ],
    screen: [
      { t: "One number per type", d: "The share of each object type that misses a mandatory tag at every level." },
      { t: "One card per type", d: "How many miss at least one mandatory tag and how many miss all, on the object alone and at every level; then, per tag, how many carry it themselves, how many get it from their compute or workspace, and how many miss it at every level. Missing means a mandatory tag is absent; other tags an object carries don't count." },
      { t: "Where queries come from", d: "Queries by origin (SQL editor, dashboards, Genie, jobs, notebooks, tools): how many carry no query tag, and how many miss the tag at every level." },
      { t: "What to tag", d: "The objects missing a mandatory tag at every level, and which ones. Queries are grouped by the warehouse or cluster they ran on, with where they came from." },
      { t: "Workspace tags", d: "Each workspace's tags, which count, and why the others don't." },
    ],
    measure: "Queries: the query tags on each statement in the window, then the tags of the job or pipeline that ran it, then the warehouse's or cluster's own tags or bill (on serverless, the usage policy's tags on the notebook's, job's or pipeline's bill), then the workspace tag. Jobs and pipelines: their own tags, then their bill, then the workspace's. Serverless notebooks (those that ran queries in the window): their usage policy's tags on the bill, then the workspace's. Clusters and warehouses: their own tags or their bill, then the workspace's. A bill's tag counts when it is on the object's latest billed day and differs from the workspace's own value. Only objects that are not deleted count.",
    judge: "A tag counts at a level when that level carries the key with any value; case, spaces, _ and - are ignored. A key found at several levels is credited to the most specific. A bill value equal to the workspace's own is the workspace's tag: on Azure it is copied onto every bill row. A value a usage policy put on the bill is the policy's, even when it equals the workspace's.",
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
      "Performance by tag and a job's or pipeline's own tag use the latest value for the whole window; cost uses each day's bill.",
      "Serverless notebooks are counted only when they ran queries in the window; notebooks carry no tags of their own.",
      "Deleted jobs, clusters and warehouses are left out only on exports that record delete times.",
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
    represents: "What this audit could see, what it couldn't, and why.",
    flags: "Grant the missing table, then re-run the export.",
    wrong: "No data means nothing happened or nothing was readable, never zero.",
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
  return trimmed.split(/(?<=[.!?])\s+/).filter(Boolean);
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
const STATUS_PREFIX_RE = /^status\s*=\s*\w+\s*-\s*/i;
const DEFAULT_PERCENT_RE = /(\d+(?:\.\d+)?)\s*\(default\)\s*percent/gi;
// A report capped at Warn says so, since its own rule still names Critical.
function capNote(f: GuideFinding | null): string {
  return f && f.severity_cap === "WARN" ? " Capped at Warn: a trend on its own never reads Critical." : "";
}

// "(default)" after every threshold is noise; one closing note says where they are set.
function cleanFlagPrefix(text: string): string {
  if (!text) return text;
  let s = String(text).replace(STATUS_PREFIX_RE, "");
  s = s.replace(DEFAULT_PERCENT_RE, "$1%");
  const stripped = s.replace(/\s*\(default\)/g, "");
  if (stripped !== s) s = `${stripped.replace(/\s*$/, "")} Defaults; set in config/thresholds.yml.`;
  return s.charAt(0).toUpperCase() + s.slice(1);
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
      <table className="tag-rollup-table" style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
        <thead>
          <tr className="faint">
            <th style={{ textAlign: "left" }}>The filter follows</th>
            <th style={{ textAlign: "right" }}>Checks</th>
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
                  <div className="faint">{byGroup[g.key].sort().join(", ")}</div>
                </details>
              </td>
              <td className="mono" style={{ textAlign: "right", verticalAlign: "top" }}>{byGroup[g.key].length}</td>
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
        <div>
          <div className="guide2-flow">
            {steps.map((step, i) => (
              <React.Fragment key={step}>
                {i > 0 && <span className="guide2-flow-arrow">&rarr;</span>}
                <span className="guide2-flow-step">{step}</span>
              </React.Fragment>
            ))}
          </div>
          {direct ? (
            <p>
              Your own credentials ran every check on Databricks and saved only the result rows
              locally -- nothing is sent anywhere else. Every check names the exact table(s) it
              reads ("Where the data comes from" on its own page), and the data on screen was
              computed on <span className="mono">{(direct.as_of || "").slice(0, 10)}</span>.
            </p>
          ) : (
            <p>
              Your own credentials read the system tables into local files -- nothing is sent
              anywhere else. Every check names the exact table(s) it reads, and the data on
              screen is as of <span className="mono">{app.as_of || "no snapshot captured yet"}</span>.
            </p>
          )}
        </div>
      );
    }
    case "window":
      return (
        <p>
          Most checks use a rolling window (<span className="mono">{app.window_options.join("/")}</span> days)
          up to the export. Today counts up to the moment the export ran and the top bar marks it
          "today partial"; billing lags a few hours, so its last hours can be missing
          (<span className="mono">include_today: false</span> exports full days only). A
          snapshot shorter than the window you picked is marked, so a partial window never reads
          as a complete one. A "snapshot" check has no window at all: it shows the current state
          (the latest known configuration), not a period.
        </p>
      );
    case "status":
      return <StatusMeaningsGrid />;
    case "floors":
      return (
        <p>
          Some checks also carry a materiality floor: a row that would flag Critical or Warn, but
          whose own dollar or unit column sits under a set minimum, reads OK instead and is marked{" "}
          <span className="mono">below_floor</span> -- too small to be worth judging. Every count,
          the worst status shown and every $ total already exclude floored rows; a "Not assessed"
          row is never touched by a floor. Floors are set per check in{" "}
          <span className="mono">config/materiality.yml</span>
          {app.materiality_floors_configured != null
            ? `, ${app.materiality_floors_configured} checks have one today.`
            : "."}
        </p>
      );
    case "money":
      return (
        <div>
          <p>
            Every dollar figure is usage &times; list price (effective) (today's what-if
            discount: <span className="mono">{fmtPct(app.discount_pct * 100)}</span>). This is never
            your real invoice, and it never includes cloud VM, storage or network-egress cost --
            only Databricks usage itself. The discount is a "what if we negotiated X% off list"
            figure, not a real contract rate the app knows.
          </p>
          <p>
            "Possible waste" means idle warehouse minutes
            {app.idle_gap_seconds != null ? ` over ${app.idle_gap_seconds}s at a stretch` : ""}{" "}
            (including the gap from start to first query, and from last query to auto-stop), idle
            classic-cluster nodes (near-zero CPU while billed), failed or timed-out job runs
            (including their repaired attempts), failed SQL statements, and serving endpoints
            billed with zero requests. No figure here is a forecast, and none is a "per month"
            number -- every one states the exact dates it covers.
          </p>
        </div>
      );
    case "steps":
      return (
        <p>
          Every step in a check's "What to do" is tagged{" "}
          <Badge kind="info">FREE</Badge> (a setting change or a read -- no spend),{" "}
          <Badge kind="info">CONFIG</Badge> (changes how something is configured, not what it
          costs directly) or <Badge kind="info">SPEND</Badge> (a step that costs money to carry
          out). A threshold named inside a step shows "default" when nobody has changed it, or
          "your setting" once <span className="mono">config/thresholds.yml</span> overrides it.
        </p>
      );
    case "tags":
      return (
        <div>
          <p>
            Spend is attributed query tag &rarr; compute tag &rarr; workspace tag &rarr; account:
            each level tries a more specific tag before falling back to a broader one. Every level
            shows its own "no tag, cannot be attributed" row with a count and a dollar figure, and
            the levels always sum back to the same billing total -- attribution narrows the picture,
            it never invents or drops spend. The filters this app offers are whichever tag keys your
            own account actually uses, never a fixed list.
          </p>
          <p>
            <b>What the Tag filter does today.</b>
          </p>
          <ul>
            <li><b>Spend</b> (Overview, Cost): each dollar takes its most specific tag: the query tag for the part of a warehouse's time Databricks attributes to queries, else the job's or pipeline's tag, else the tag on the bill (warehouse, cluster or budget policy), else the workspace's. The values add up to the bill.</li>
            <li><b>Single queries, jobs, pipelines, runs, warehouses and clusters:</b> the row's own tag, else the next level up, as listed below.</li>
            <li><b>Tables, schemas and catalogs:</b> their Unity Catalog tags.</li>
            <li><b>Several tag names:</b> a row must match every name; the values picked under one name are alternatives.</li>
          </ul>
          <p>
            <b>What it doesn't do yet.</b>
          </p>
          <ul>
            <li><b>Per-warehouse query numbers</b> (Queries › Capacity, the query trend, top queries by $, start-up waits) are summed per warehouse before the export. They keep or drop whole warehouses by the warehouse's and workspace's tags, so a value set only as a query tag leaves them empty.</li>
            <li><b>Idle warehouse time</b> has no query, so its dollars follow the warehouse's tag, then the workspace's.</li>
            <li><b>The Tags page</b> counts every object, whatever the filter.</li>
            <li><b>Checks no tag can reach</b> (price lists, grants, account settings) are never filtered and show "tag n/a".</li>
          </ul>
          <p>
            <b>Which tag each object gets.</b> For each tag key, the first level that has it wins:
          </p>
          <ul>
            <li><b>Query:</b> its own query tag, else the tag of the job or pipeline that ran it, else its compute's (the warehouse's or cluster's own tag or bill; on serverless, the usage policy's), else its workspace's.</li>
            <li><b>Job or pipeline:</b> its own tag, else its compute's tag, else its workspace's.</li>
            <li><b>Warehouse or cluster:</b> its own tag (a cluster's includes its compute policy's and pool's) or its bill's, else its workspace's.</li>
            <li><b>Serverless notebook:</b> its usage policy's tag, else its workspace's.</li>
            <li><b>Workspace:</b> its own tag; on Azure, the workspace resource's tag.</li>
          </ul>
          <p>
            On Azure, the workspace's own tag is copied onto every bill row, classic and serverless.
            On the Tags page a bill value equal to it counts as the workspace's, so a warehouse with
            no tag of its own is not credited with it. A value on a bill with a usage policy counts as
            the policy's, even when it equals the workspace's. To give two warehouses different tags,
            tag each warehouse; a usage policy on a serverless warehouse's bill counts too.
          </p>
          <p>
            Dollars follow each day's bill: Databricks copies a warehouse's or cluster's tags onto its
            bill, so each day's spend carries the value of that day. When an object's own tag and its
            bill disagree, the cost follows the bill, and the Tags page lists the object under
            "tagged one way, billed another". Performance by tag uses the bill's latest value for the
            whole window.
          </p>
          <p>
            <b>Example.</b> Warehouse BI is tagged cost_center=A on itself; its bill reads
            cost_center=B since 4 Sep. A query on BI tagged cost_center=C is charged to C. A query with
            no tag is charged to B from 4 Sep, and to the bill's earlier value before. The Tags page
            counts BI as tagged on itself and lists it as tagged A, billed B.
          </p>
          <p>
            <b>Dates and values on the Tags page.</b> "since 4 Sep": the value has been on the bill
            every billed day since. "gone after 1 Sep": the key was on the bill, but not on its latest
            day. "(before: A)": other values the key had on the bill. "last billed 12 Jun": no bill
            for over a week, so the tags shown are that day's. "mixed: red, blue": a workspace whose
            billed values split with none on most; it covers nothing.
          </p>
          <p>
            <b>Workspace tags.</b> Databricks has no table that lists a workspace's tags, so each
            one is read from the workspace's bill, where Azure copies the workspace resource's tags
            onto every row. A tag counts as the workspace's when at
            least {fmtPct((app.tag_coverage_floor || 0) * 100, 0)} of its billed usage without a usage policy carries the key and
            one value has at least {fmtPct((app.tag_share_floor || 0) * 100, 0)} of it (settings{" "}
            <span className="mono">tag_coverage_floor</span> and{" "}
            <span className="mono">tag_share_floor</span>). Then it also covers the workspace's
            serverless usage, queries, jobs and compute that have no tag of their own. When the key
            is there but split between values, it reads "mixed" and covers nothing. A workspace
            with no billed usage has no workspace tag to read. Cost › Allocation › Workspace tags
            shows each tag's share.
          </p>
          <p>
            <b>Mandatory tags.</b> The tags every object must carry are set in{" "}
            <span className="mono">mandatory_tag_keys</span>
            {app.mandatory_tag_keys ? ` (now ${app.mandatory_tag_keys.join(", ")})` : ""}, up to 5. The Tags
            page counts the queries, jobs, pipelines, serverless notebooks, clusters, warehouses and
            workspaces that miss them, level by level, and where queries without a query tag come
            from.
          </p>
          <p>
            <b>Chargeback by tag value.</b> Cost › Chargeback › Tag value splits the window's spend by
            one tag key at a time. Pick a workspace first, then a key. Mandatory tags come first;
            then keys with 2+ values inside a workspace (they split spend), keys with one value per
            workspace (the same split as the Workspace view) and keys with one value only (they split
            nothing). The workspace list holds only workspaces with tagged spend in the window, so it
            is usually shorter than the account's workspace count: a workspace with no spend in the
            window, or whose spend carries no tag at all, has nothing to split. The Tags page's
            Workspaces card counts every workspace with spend.
          </p>
          <TagReachTable findings={findings} />
        </div>
      );
    case "names":
      return (
        <p>
          Once a name is known, a row shows it as "name (id)" rather than the bare id. When no
          name could be resolved, the row says why ("no name: &lt;reason&gt;") instead of leaving
          a blank or guessing at one.
        </p>
      );
    case "coverage":
      return (
        <p>
          Coverage &amp; Gaps shows which system tables this snapshot actually captured, and what
          enabling one needs -- most schemas need only{" "}
          <span className="mono">{app.default_enable}</span>, and a check's own "Where the data
          comes from" names anything extra. Regional system tables cover only the metastore's own
          region; billing and the workspace list are the account-wide exceptions. Every check also
          carries its own confidence and any known issue with the vendored query behind it, in its
          own "For reviewers and admins".
        </p>
      );
    case "coverage-data-sources":
      return (
        <div>
          <p>
            The Sources tab lists every system table this app can read: rows captured, the date
            range or "current state" it covers, how many checks use it, and (if empty) what fills
            it in. It splits them into <b>With rows</b> (captured data this snapshot), <b>Empty</b>{" "}
            (read fine, 0 rows -- not a verified zero) and <b>Missing</b> (could not be read at all).
            Alongside that inventory, <b>System table coverage</b> is a real check of its own: it
            flags a table whose data has gone stale or has a gap inside this window (WARN), not
            just whether the table exists. <b>This audit's own usage</b> is a reference row showing
            how much of each warehouse's own traffic and spend was this audit itself.
          </p>
          <p>
            <b>What to do:</b> for a Missing source, grant the account or metastore the read the
            table needs (the row itself names the schema); for an Empty one, read its own "what
            fills it" note -- some are a true zero, others need a feature turned on first; for a
            System table coverage row flagged WARN, check that table's own ingestion job or grant.
          </p>
          <a className="ck-link" {...appTabLinkProps("coverage", "sources")}>{"Back to Coverage & Gaps › Sources →"}</a>
        </div>
      );
    case "coverage-couldnt-check":
      return (
        <div>
          <p>
            The Not assessed tab lists every check that judged nothing this window: its build
            was <b>not assessed</b> (skipped or failed), it hit a <b>read error</b>, it found{" "}
            <b>nothing in this window</b>, or your filters excluded every row. None of these is a
            pass, even the ones that ran cleanly and found nothing.
          </p>
          <p>
            <b>What to do:</b> open the check's own page and read "If it says Not assessed or is
            empty" for the exact reason and, where there is one, the grant or setting that fixes it.
          </p>
          <a className="ck-link" {...appTabLinkProps("coverage", "couldnt")}>{"Back to Coverage & Gaps › Not assessed →"}</a>
        </div>
      );
    case "coverage-known-limitations":
      return (
        <div>
          <p>
            The Known limitations tab lists fixed modelling choices that change how to read a
            number elsewhere in the app -- a ranking-by-size check that never reads Critical or
            Warn, autoscale churn counted in events rather than time, and the like. These are not
            bugs to fix; they are how the check was built.
          </p>
          <p><b>What to do:</b> nothing to configure -- just read the affected number with its own note in mind.</p>
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
  { kind: "not_assessed", word: "Not assessed", d: "Did not run in this export. Never a pass: its numbers read \"–\" with the reason, and the top bar's \"N of M checks ran\" leaves it out." },
  { kind: "reference", word: "No data", d: "Ran, but nothing happened in this window. Not a verified zero -- the checks table's own \"No data\" tab." },
  { kind: "reference", word: "No data (filtered)", d: "Rows exist, but your workspace or tag filters exclude them all -- also the \"No data\" tab." },
  { kind: "reference", word: "Reference", d: "A list to look things up in -- no pass or fail. A reference list that flags rows Critical or Warn counts as Critical or Warn, like any check." },
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

function guideFaqs() {
  return [
    {
      q: "Is this my Databricks invoice?",
      a: (
        <React.Fragment>
          No. Every dollar is your Databricks usage &times; the list price Databricks publishes,
          minus a what-if discount you can set (today's setting shows on the "How dollars are
          worked out" topic above). It never includes your cloud provider's VM, storage or network
          bill, and it never knows your negotiated rate. Use it to compare and rank, not to
          reconcile an invoice. <a {...guideLinkProps("how-money")}>How the dollar figures are worked out &rarr;</a>
        </React.Fragment>
      ),
    },
    {
      q: "Why is a panel empty, or a whole screen \"no data\"?",
      a: (
        <React.Fragment>
          An empty panel means the check ran and found nothing, or the source behind it isn't
          available in this snapshot -- never "all clear" by omission.{" "}
          <a {...appTabLinkProps("coverage", "sources")}>Coverage & Gaps &rarr;</a> says which sources are missing and what filling them would unlock.
        </React.Fragment>
      ),
    },
    {
      q: "What counts as possible waste -- and what doesn't?",
      a: (
        <React.Fragment>
          Idle warehouse minutes, idle classic-cluster nodes, failed or timed-out job runs, failed
          SQL statements, and serving endpoints billed with zero requests -- each priced at list, a
          lower bound and never a forecast. Spend on premium SKUs (serverless, Photon) is worth a
          look but is not counted as waste. <a {...guideLinkProps("how-money")}>More on how dollars are worked out &rarr;</a>
        </React.Fragment>
      ),
    },
    {
      q: "Why does a check say \"Not assessed\"?",
      a: "The build behind that check failed or was skipped this run -- never a verified pass. Open the check's own page and look under \"If it says Not assessed or is empty\" for the exact reason.",
    },
    {
      q: "How do I change a threshold, like the 60-second idle rule?",
      a: (
        <React.Fragment>
          Every threshold this app uses lives in <span className="mono">config/thresholds.yml</span>.
          A check's own "When it flags" section shows each value and whether it is still the default
          or has been set there.
        </React.Fragment>
      ),
    },
    {
      q: "Which grants does Databricks need for the missing tables?",
      a: (
        <React.Fragment>
          <a {...appTabLinkProps("coverage", "sources")}>Coverage & Gaps &rarr;</a> lists exactly which grant each missing source needs, next to that source.
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
  { k: "DBU", d: "Databricks Unit -- the unit Databricks bills compute in. Dollars = DBUs × the price for that product." },
  { k: "List price", d: "Databricks' published price per product, after any promotion. Not your negotiated rate." },
  { k: "Possible waste", d: "Measured idle time and failed work, priced at list. A lower bound, never a forecast." },
  { k: "Window", d: "The days a check covers: the last 7, 30 or 90, up to the export." },
  { k: "Auto-stop", d: "How long a warehouse waits after its last query before it shuts down." },
  { k: "Spill", d: "A query ran out of memory and wrote to disk, which slows it down." },
  { k: "Broken job", d: "Its latest 3 or more runs failed in a row (default threshold)." },
  { k: "Flaky job", d: "20% or more of its runs failed, with at least 5 runs in the window (default thresholds)." },
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

function TableChips({ tables, max }: { tables: string[]; max?: number }) {
  const shown = max ? tables.slice(0, max) : tables;
  return (
    <span className="gd-tables">
      {shown.map((t) => <code key={t} className="gd-code">{shortTable(t)}</code>)}
      {tables.length > shown.length && <span className="muted">{`+${tables.length - shown.length} more`}</span>}
    </span>
  );
}

// The Guide's own left column on every page but home: topics, tabs, reference.
function GuideDocsNav({ current, guideIndex, checkArea }: { current: string; guideIndex: GuideIndex; checkArea?: string | null }) {
  const [query, setQuery] = React.useState("");
  const needle = query.trim().toLowerCase();
  const matches = needle
    ? Object.values(guideIndex).filter((f) => {
      const t = getCheckLabel(f.query_id, f.title).title.toLowerCase();
      return t.includes(needle) || f.query_id.includes(needle);
    }).slice(0, 30)
    : null;
  const item = (id: string, label: string, extra?: number) => (
    <a key={id} className={`gd-nav-item${current === id ? " active" : ""}`} {...guideLinkProps(id)}>
      {label}{extra != null && <span className="muted">{` · ${extra}`}</span>}
    </a>
  );
  const areaChecks = checkArea ? tabCheckIds(checkArea).filter((id) => guideIndex[id]) : [];
  return (
    <nav className="gd-nav" aria-label="How it works">
      <a className="gd-nav-back" {...guideLinkProps(null)}>&larr; How it works</a>
      <input
        className="gd-nav-search"
        aria-label="Filter checks"
        placeholder={`Filter ${Object.keys(guideIndex).length} checks`}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {matches ? (
        <div className="gd-nav-group">
          <div className="gd-nav-h">{`${matches.length} match${matches.length === 1 ? "" : "es"}`}</div>
          {matches.map((f) => item(f.query_id, getCheckLabel(f.query_id, f.title).title))}
        </div>
      ) : (
        <React.Fragment>
          {checkArea && (
            <div className="gd-nav-group">
              <div className="gd-nav-h">{`${AREA_REGISTRY[checkArea].label} · ${areaChecks.length}`}</div>
              {areaChecks.map((id) => item(id, getCheckLabel(id, guideIndex[id].title).title))}
            </div>
          )}
          {!checkArea && (
            <div className="gd-nav-group">
              <div className="gd-nav-h">Start here</div>
              {GUIDE_UNIVERSAL_SECTIONS.map((s) => item(`how-${s.slug}`, s.title))}
            </div>
          )}
          <div className="gd-nav-group">
            <div className="gd-nav-h">{checkArea ? "Other tabs" : "Tabs"}</div>
            {GUIDE_SCREEN_ORDER.filter((k) => k !== checkArea).map((k) => item(tabGuideId(k), guideArea(k)!.label))}
          </div>
          <div className="gd-nav-group">
            <div className="gd-nav-h">Reference</div>
            {item("how-checks", "Check reference", Object.keys(guideIndex).length)}
            {item("how-troubleshooting", "Troubleshooting")}
            {item("how-glossary", "Glossary")}
          </div>
        </React.Fragment>
      )}
    </nav>
  );
}

function GuidePage({ current, guideIndex, checkArea, rail, children }: {
  current: string; guideIndex: GuideIndex; checkArea?: string | null; rail?: React.ReactNode; children?: React.ReactNode;
}) {
  React.useEffect(() => { window.scrollTo(0, 0); }, [current]);
  return (
    <div className={`gd-layout${rail ? " has-rail" : ""}`}>
      <GuideDocsNav current={current} guideIndex={guideIndex} checkArea={checkArea} />
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
  const roles = ROLES.filter((r) => r !== "cfo" && (ROLE_AREAS[r] || []).includes(areaKey));
  return roles.length ? roles.map((r) => ROLE_LABEL[r]).join(", ") : null;
}

const GUIDE_RULES = [
  "Dollars are usage × Databricks list price. Not your invoice, never your cloud VM bill.",
  "Windows are the last 7, 30 or 90 days up to the export; today is partial.",
  "Empty never means all clear. Only OK is a verified pass.",
];

const HOME_QUESTIONS = [
  { q: "Why is this panel empty?", focus: "how-troubleshooting" },
  { q: "Why don't two screens agree?", focus: "how-troubleshooting" },
  { q: "Is this my invoice?", focus: "how-money" },
  { q: "How is idle time counted?", focus: "compute_warehouse_idle_minutes" },
];

const SCREEN_ANATOMY = [
  { h: "The headline", d: "is the finding, written from the data. Start there." },
  { h: "Every number has a baseline", d: "-- a total or the period before. No baseline, no conclusion." },
  { h: "How is this worked out?", d: "opens the check's page here: the formula and the system tables behind it." },
  { h: "Status is a word", d: "as well as a colour. See the legend." },
  { h: "The region banner", d: "says when some workspaces show spend only." },
];

function AnatomyMock() {
  const dot = (n: number) => <span className="gd-anat-dot">{n}</span>;
  return (
    <div className="gd-anat-mock" aria-hidden="true">
      <div className="gd-anat-banner">{dot(5)}<span className="gd-anat-line w40" /></div>
      <div className="gd-anat-head">{dot(1)}<span className="gd-anat-line w80 dark" /></div>
      <div className="gd-anat-cards">
        <div className="gd-anat-card">{dot(2)}<span className="gd-anat-line w40" /><span className="gd-anat-num" /><span className="gd-anat-line w60" /></div>
        <div className="gd-anat-card">{dot(3)}<span className="gd-anat-line w40" /><span className="gd-anat-num" /><span className="gd-anat-line w50" /></div>
      </div>
      <div className="gd-anat-row"><span className="gd-anat-line w50" />{dot(4)}<Badge kind="critical">Critical</Badge></div>
    </div>
  );
}

function GuideHome({ data, guideIndex, cloud, setCloud, cloudNote, overrideStale, useSnapshotCloud }: {
  data: GuideData; guideIndex: GuideIndex; cloud: string; setCloud: (c: string) => void; cloudNote: string;
  overrideStale: boolean; useSnapshotCloud: () => void;
}) {
  const [query, setQuery] = React.useState("");
  const needle = query.trim().toLowerCase();
  const results = React.useMemo(() => {
    if (!needle) return [];
    return data.findings.filter((f) => guideSearchText(f, getCheckLabel(f.query_id, f.title)).includes(needle));
  }, [needle, data.findings]);
  const allTables = tablesFor(Object.keys(guideIndex), guideIndex);

  return (
    <div className="gd-home">
      <div className="gd-home-top">
        <div className="gd-home-intro">
          <div className="gd-eyebrow-row">
            <span className="gd-eyebrow">{`How it works · ${data.findings.length} checks · ${allTables.length} system tables`}</span>
            <CloudSwitch cloud={cloud} setCloud={setCloud} cloudNote={cloudNote} overrideStale={overrideStale} useSnapshotCloud={useSnapshotCloud} />
          </div>
          <h1 className="gd-display">What every tab shows, where its numbers come from, and what to do when they look wrong.</h1>
          <p className="muted">This app reads your Databricks system tables and shows what you spend, what is wasted and what is risky, at list price. New here? Start on Overview, then Actions. Pick the lens for your role at the top left.</p>
          <label className="guide2-search">
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="9" cy="9" r="6"></circle><path d="M13.5 13.5L18 18"></path></svg>
            <input
              type="search"
              aria-label="Search checks and topics"
              placeholder="Type a check, a table or a number you saw"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <div className="gd-qchips">
            {HOME_QUESTIONS.map((x) => <a key={x.q} className="gd-qchip" {...guideLinkProps(x.focus)}>{x.q}</a>)}
          </div>
        </div>
        <div className="gd-rules">
          <div className="gd-rules-h">Three rules for every screen</div>
          <ol>{GUIDE_RULES.map((r, i) => <li key={i}><span className="gd-rules-n">{i + 1}</span><span>{r}</span></li>)}</ol>
        </div>
      </div>

      {needle && (
        <Card title={`${results.length} matching check${results.length === 1 ? "" : "s"}`}>
          {results.length === 0 ? (
            <div className="muted">{`No check matches "${query}".`}</div>
          ) : (
            <ul className="guide2-search-results">
              {results.map((f) => {
                const label = getCheckLabel(f.query_id, f.title);
                return (
                  <li key={f.query_id}>
                    <a {...guideLinkProps(f.query_id)}>{label.title}</a>
                    {label.why && <span className="muted">{` -- ${label.why}`}</span>}
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      )}

      <section className="gd-section">
        <div className="gd-section-head"><h2>Every tab at a glance</h2><span className="muted">Open a row for its page: what's on screen, how it's worked out, its checks</span></div>
        <div className="gd-table-wrap">
          <table className="gd-table gd-glance">
            <thead><tr><th>Tab</th><th>What it represents</th><th>How the data gets there</th><th>If it flags something</th><th>Why it can look wrong</th></tr></thead>
            <tbody>
              {GUIDE_SCREEN_ORDER.map((k) => {
                const g = GUIDE_TABS[k];
                const ids = tabCheckIds(k).filter((id) => guideIndex[id]);
                const link = guideLinkProps(tabGuideId(k));
                return (
                  <tr key={k} className="gd-glance-row" onClick={link.onClick}>
                    <td>
                      <a className="gd-glance-name" {...link}>{guideArea(k)!.label}</a>
                      <div className="muted gd-small">{`${g.lens} · ${ids.length} checks`}</div>
                    </td>
                    <td>{g.represents}</td>
                    <td><TableChips tables={tablesFor(ids, guideIndex)} max={4} /></td>
                    <td>{g.flags}</td>
                    <td className="muted">{g.wrong}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <div className="gd-home-pair">
        <section className="gd-card">
          <h2 className="gd-card-h">Read any screen in 30 seconds</h2>
          <div className="gd-anat">
            <AnatomyMock />
            <ol className="gd-anat-list">
              {SCREEN_ANATOMY.map((a, i) => (
                <li key={i}><span className="gd-anat-dot">{i + 1}</span><span><b>{a.h}</b>{` ${a.d}`}</span></li>
              ))}
            </ol>
          </div>
        </section>
        <section className="gd-card">
          <div className="gd-card-head"><h2 className="gd-card-h">What each status means</h2><span className="muted gd-small">Only OK is a pass</span></div>
          <StatusMeaningsGrid />
          <div className="muted gd-small gd-legend-note">Rows below a materiality floor read OK and are left out of every count.</div>
        </section>
      </div>

      <div className="gd-home-trio">
        <a className="gd-card gd-link-card" {...guideLinkProps("how-troubleshooting")}>
          <span className="gd-eyebrow">Troubleshooting</span>
          <span className="gd-link-card-h">A number looks wrong or a panel is empty</span>
          <span className="muted">Follow the status word to the cause and the fix.</span>
        </a>
        <a className="gd-card gd-link-card" {...guideLinkProps("how-checks")}>
          <span className="gd-eyebrow">{`Check reference · ${data.findings.length}`}</span>
          <span className="gd-link-card-h">Every check, one page each</span>
          <span className="muted">What it flags, how it's worked out, thresholds, fixes, and when it misreads.</span>
        </a>
        <a className="gd-card gd-link-card" {...guideLinkProps("how-glossary")}>
          <span className="gd-eyebrow">Glossary</span>
          {GUIDE_GLOSSARY.slice(0, 4).map((g) => (
            <span key={g.k} className="gd-gloss-mini"><b>{g.k}</b><span className="muted">{g.d}</span></span>
          ))}
        </a>
      </div>

      {data.app.docs_error && <div className="muted guide2-footnote">Documentation links unavailable: {data.app.docs_error}</div>}
    </div>
  );
}

// ═══════════════════════════════════ Topic page ═══════════════════════════════════

function GuideTopicPage({ slug, data, guideIndex, cloud }: { slug: string; data: GuideData; guideIndex: GuideIndex; cloud: string }) {
  // GuideTab opens this page only for a known slug.
  const section = GUIDE_SECTIONS.find((s) => s.slug === slug)!;
  const isCoverage = slug.indexOf("coverage-") === 0;
  return (
    <GuidePage current={`how-${slug}`} guideIndex={guideIndex}>
      <Crumbs items={isCoverage ? [{ label: "Tabs" }, { label: AREA_REGISTRY.coverage.label, focus: tabGuideId("coverage") }] : [{ label: "Start here" }]} />
      <h1 className="gd-title">{section.title}</h1>
      <p className="gd-lede">{section.teaser}</p>
      <div className="gd-prose">{renderHowBody(slug, data.app, data.findings)}</div>
      {(data.sections[slug] || []).length > 0 && (
        <div className="guide2-topic-docs">
          {data.sections[slug].map((k) => <DocsLink key={k} docsKey={k} docs={data.docs} cloud={cloud} />)}
        </div>
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
        <div className="gd-kv"><span>Checks ran</span><b className="mono">{`${ran} / ${ids.length}`}</b></div>
        <div className="gd-kv"><span>Checks flagged</span><b className="mono">{flagged}</b></div>
        {refreshed && <div className="gd-kv"><span>Refreshed</span><b className="mono">{refreshed}</b></div>}
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
    <GuidePage current={tabGuideId(areaKey)} guideIndex={guideIndex} rail={rail}>
      <Crumbs items={[{ label: "Tabs" }, { label: area.label }]} />
      <h1 className="gd-title">{area.label}</h1>
      <p className="gd-lede">{g.lede}</p>
      <div className="gd-chips">
        <span className="gd-chip">{`In lenses: ${lensesForArea(areaKey) || g.lens}`}</span>
        <span className="gd-chip">{ids.length || !g.queries ? `${ids.length} checks` : `${g.queries.length} queries to run`}</span>
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
      <p className="gd-prose muted">{ids.length || !g.queries
        ? "Every figure is the output of a check: a SQL query run with your own credentials against Databricks system tables. Only the result rows are stored."
        : "Every figure is worked out from the tags the export read from Databricks system tables, with your own credentials. Only the result rows are stored."}</p>
      <div className="gd-flow">
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">1 · Read</span><b>System tables</b>{tables.length > 0 ? <TableChips tables={tables} max={8} /> : <span className="muted">No system table of its own.</span>}</div>
        <span className="gd-flow-arrow" aria-hidden="true">&rarr;</span>
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">2 · Measure</span><span>{g.measure}</span></div>
        <span className="gd-flow-arrow" aria-hidden="true">&rarr;</span>
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">3 · Judge</span><span>{codeifyProse(g.judge)}</span></div>
        <span className="gd-flow-arrow" aria-hidden="true">&rarr;</span>
        <div className="gd-card gd-flow-step"><span className="gd-eyebrow">4 · Price</span><span>{g.price}</span></div>
      </div>

      <div className="gd-section-head" id="gd-s3">
        <h2 className="gd-h2">{ids.length || !g.queries ? "Checks on this tab" : "Queries to run yourself"}</h2>
        <span className="muted">{ids.length || !g.queries ? `${ids.length} checks` : `${g.queries.length} queries`}</span>
      </div>
      {ids.length > 0 && <p className="muted gd-small">Each check's page ends with the SQL it runs, to paste into a Databricks SQL editor.</p>}
      {g.queries && g.queries.length > 0 && (
        <div className="gd-sql-list">
          <p className="muted gd-small">This page has no checks of its own. These queries answer the same questions in a Databricks SQL editor:</p>
          {g.queries.map((q) => <RunItYourself key={q.id} queryId={q.id} windowDays={data.app.default_window} title={q.title} />)}
        </div>
      )}
      {ids.length === 0 ? (
        !g.queries && <p className="muted">This page has no checks of its own; it rolls up the others.</p>
      ) : (
        <div className="gd-table-wrap">
          <table className="gd-table">
            <thead><tr><th>Check</th><th>Flags when</th><th>Money</th><th>This build</th></tr></thead>
            <tbody>
              {ids.map((id) => {
                const f = guideIndex[id];
                const s = findingsById[id];
                const money = f.money && f.money.column;
                return (
                  <tr key={id}>
                    <td>
                      <a {...guideLinkProps(id)}>{getCheckLabel(id, f.title).title}</a>
                      <div className="mono muted gd-small">{id}</div>
                    </td>
                    <td className="gd-small">{f.is_finding ? <React.Fragment>{codeifyProse(cleanFlagPrefix(f.investigate_if || "")) || "--"}{capNote(f)}</React.Fragment> : <span className="muted">Reference list, no pass or fail.</span>}</td>
                    <td>{money ? <span className={`gd-chip ${isPricedWaste(id) ? "ok" : ""}`}>{isPricedWaste(id) ? "Waste" : "Priced"}</span> : <span className="muted">--</span>}</td>
                    <td className="gd-small" style={{ whiteSpace: "nowrap" }}>{!s ? <span className="muted">--</span> : checkFlagged(s) ? <span className="gd-flag">Flagged</span> : checkRan(s) ? <span className="muted">Ran</span> : <span className="muted">Not assessed</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="gd-h2" id="gd-s4">When something looks wrong</h2>
      <div className="gd-table-wrap">
        <table className="gd-table">
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
    badge: "critical", word: "A number", d: "It has a value you don't believe.",
    steps: [
      "Press How is this worked out? next to it to see the formula and source tables.",
      "Read the region banner: some workspaces may show spend only.",
      "If another screen shows a different figure, use the table below.",
    ],
  },
];

const SCREENS_DIFFER = [
  { r: "Different window", ex: "Monthly bars use calendar months; the cards use the last 7, 30 or 90 full days.", fix: "Compare like for like." },
  { r: "Filter doesn't apply", ex: "Grants, tags, masks, lineage and table checks cover the whole metastore; picking a workspace doesn't narrow them.", fix: "Read the tab's scope note." },
  { r: "Different coverage", ex: "Spend counts every billed workspace; compute, jobs and queries only those in the export region.", fix: "Filter to the assessed workspaces." },
  { r: "Different attribution level", ex: "Spend is attributed query, then compute, workspace, account. A per-query view adds up to less than the warehouse bill.", fix: "Use the level that matches the question." },
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
  const faqs = guideFaqs().filter((x) => !/differ between screens|Not assessed/.test(x.q));
  return (
    <GuidePage current="how-troubleshooting" guideIndex={guideIndex}>
      <Crumbs items={[{ label: "Troubleshooting" }]} />
      <h1 className="gd-title">A panel is empty, or a number looks wrong</h1>
      <p className="gd-lede">Start with the status word on the panel. It tells you whether the audit couldn't look, looked and found nothing, or found something you don't expect.</p>

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
        <table className="gd-table">
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
    <GuidePage current="how-glossary" guideIndex={guideIndex}>
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
  const rest = Object.keys(guideIndex).filter((id) => !placed.has(id)).sort();
  if (rest.length) groups.push({ key: "other", label: "Not on a screen", ids: rest });
  return (
    <GuidePage current="how-checks" guideIndex={guideIndex}>
      <Crumbs items={[{ label: "Check reference" }]} />
      <h1 className="gd-title">Check reference</h1>
      <p className="gd-lede">{`All ${Object.keys(guideIndex).length} checks by the tab they live on, with the system tables each reads.`}</p>
      {groups.filter((gr) => gr.ids.length).map((gr) => (
        <section key={gr.key} className="gd-section">
          <div className="gd-section-head">
            <h2 className="gd-h2 flush">{gr.label}</h2>
            <span className="muted">{`${gr.ids.length} checks`}</span>
          </div>
          <div className="gd-table-wrap">
            <table className="gd-table">
              <tbody>
                {gr.ids.map((id) => {
                  const f = guideIndex[id];
                  const s = findingsById[id];
                  return (
                    <tr key={id}>
                      <td className="gd-ref-name"><a {...guideLinkProps(id)}>{getCheckLabel(id, f.title).title}</a><div className="mono muted gd-small">{id}</div></td>
                      <td><TableChips tables={(f.reads || []).filter((t) => String(t).indexOf("system.") === 0)} max={4} /></td>
                      <td className="gd-small" style={{ whiteSpace: "nowrap" }}>{!s ? "" : checkFlagged(s) ? <span className="gd-flag">Flagged</span> : checkRan(s) ? <span className="muted">Ran</span> : <span className="muted">Not assessed</span>}</td>
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
    return fmtCell(row[c.name], c.kind, row);
  };
  return (
    <div className="gd-card gd-example">
      {cells.map((c) => (
        <div key={c.name} className="gd-example-cell">
          <span className="muted gd-small">{c.label}{c.unit ? ` (${c.unit})` : ""}</span>
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

function CheckRail({ f, data, cloud, home, area, findingsById }: {
  f: GuideFinding; data: GuideData; cloud: string; home: Home; area: Area | null; findingsById: Record<string, FindingSummary>;
}) {
  const s = findingsById[f.query_id];
  const moneyCol = f.money && f.money.column;
  const sub = area && home.subtab ? area.subtabs.find((x) => x.key === home.subtab) : undefined;
  const subLabel = sub ? sub.label : null;
  const params = f.params.filter((p) => p.name !== "period_days" && p.name !== "top_n");
  const reads = (f.reads || []);
  const related = (f.next || []).filter((n) => n.title);
  let buildLine = "Not in this export";
  if (s && checkRan(s)) {
    const a: Partial<Affected> = s.affected || {};
    if (f.is_finding && a.total != null) buildLine = `${fmtInt(a.flagged || 0)} of ${fmtInt(a.total)} ${a.noun || "rows"} flagged`;
    else buildLine = s.row_count != null ? `${fmtInt(s.row_count)} rows` : "Ran";
  } else if (s) {
    buildLine = "Not assessed";
  }
  return (
    <React.Fragment>
      <RailCard title="At a glance">
        <div className="gd-rail-field">
          <span className="muted">Appears on</span>
          {area ? <a {...openInAppLinkProps(f)}>{subLabel ? `${area.label} › ${subLabel}` : area.label}</a> : <span>All findings</span>}
        </div>
        {moneyCol && <div className="gd-rail-field"><span className="muted">Money column</span><code className="gd-code">{moneyCol}</code></div>}
        {params.map((p) => (
          <div key={p.name} className="gd-rail-field">
            <span className="muted">{p.meaning || p.name}</span>
            <span><span className="mono">{String(p.value)}</span> <span className="muted gd-small">{p.source === "default" ? "default" : "config/thresholds.yml"}</span></span>
          </div>
        ))}
        <div className="gd-rail-field"><span className="muted">This build</span><span className="mono">{buildLine}</span></div>
      </RailCard>
      {reads.length > 0 && (
        <RailCard title="Reads">
          <div className="gd-rail-reads">
            {reads.map((key) => {
              const entry = data.docs[key];
              const link = entry && entry.urls[cloud];
              return link
                ? <a key={key} className="gd-code" href={link.url} target="_blank" rel="noreferrer" title={(entry && entry.what) || ""}>{key}</a>
                : <code key={key} className="gd-code" title={(entry && entry.what) || ""}>{key}</code>;
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
      <a className="gd-open-btn" {...openInAppLinkProps(f)}>Open in the app &rarr;</a>
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
            <button type="button" className="ck-link" onClick={copy}>{copied ? "Copied" : "Copy SQL"}</button>
          </div>
          <pre className="gd-sql-code"><code>{state.sql}</code></pre>
        </React.Fragment>
      )}
    </details>
  );
}

function GuideArticle({ f, data, filters, home, area }: { f: GuideFinding; data: GuideData; filters: GuideFilters; home: Home; area: Area | null }) {
  const label = getCheckLabel(f.query_id, f.title);
  const summary = label.why || f.summary;
  const sub = area && home.subtab ? area.subtabs.find((s) => s.key === home.subtab) : undefined;
  const subLabel = sub ? sub.label : null;
  const moneyCol = f.money && f.money.column;
  const isWaste = isPricedWaste(f.query_id);
  const wasteItem = WASTE_ITEMS.find((w) => w.id === f.query_id);
  const windowChip = f.period_kind === "days" ? "Window check" : f.period_kind === "fixed" ? "Own period" : "Snapshot";
  const [copied, setCopied] = React.useState(false);

  const grain = grainText(f.summary);
  const readRest = splitSentences(f.read_this).slice(1).join(" ");
  const steps: { h: string; t: React.ReactNode }[] = [];
  if (grain) steps.push({ h: "Each row", t: `is ${grain}.` });
  if (readRest) steps.push({ h: "Reading it.", t: codeifyProse(readRest) });
  if (f.is_finding && f.investigate_if) steps.push({ h: "Flags when", t: <React.Fragment>{codeifyProse(cleanFlagPrefix(f.investigate_if))}{capNote(f)}</React.Fragment> });
  if (f.is_finding && f.healthy) steps.push({ h: "Reads OK when", t: codeifyProse(cleanFlagPrefix(f.healthy)) });
  if (moneyCol) {
    steps.push({
      h: "Dollars.",
      t: wasteItem ? wasteItem.why : `Usage in the window × list price (effective), today's discount ${fmtPct((data.app.discount_pct || 0) * 100)}. Never your invoice or your cloud VM bill.`,
    });
  }
  if (f.period_kind === "days") steps.push({ h: "Window.", t: `The last ${filters.window} days, up to the export.` });

  const rungs = (f.actions || [])
    .map((a) => String(a).trim())
    .filter((a) => a && !a.toLowerCase().startsWith("n/a"))
    .map((a) => ({ tier: rungTier(a), text: codeifyProse(rungText(a)) }));
  const caveats = splitSentences(f.caveats);
  const offCards: { h: string | null; t?: React.ReactNode; list?: string[] }[] = [
    ...caveats.map((c) => ({ h: null, t: codeifyProse(c) })),
    ...(f.not_assessed_reasons.length ? [{ h: "Not assessed when", list: f.not_assessed_reasons }] : []),
    ...(f.empty_if_words.length ? [{ h: "No data when", list: f.empty_if_words }] : []),
  ];

  return (
    <article className="gd-article">
      <Crumbs items={[
        area ? { label: area.label, focus: GUIDE_TABS[home.tab] ? tabGuideId(home.tab) : null } : { label: "All findings" },
        ...(subLabel ? [{ label: subLabel }] : []),
      ]} />
      <div className="gd-title-row">
        <h1 className="gd-title">{label.title}</h1>
        <button type="button" className="ck-link" onClick={() => copyGuideLink(f.query_id, () => { setCopied(true); setTimeout(() => setCopied(false), 1200); })}>
          {copied ? "Copied" : "Copy link"}
        </button>
      </div>
      <div className="gd-chips">
        <code className="gd-code">{f.query_id}</code>
        {isWaste && <span className="gd-chip warn">Possible waste</span>}
        {moneyCol && <span className="gd-chip ok">Priced</span>}
        {!f.is_finding && <span className="gd-chip">Reference list</span>}
        <span className="gd-chip">{windowChip}</span>
        {rungs[0] && rungs[0].tier && <span className="gd-chip">{`Fix: ${rungs[0].tier}`}</span>}
      </div>
      {summary && <p className="gd-lede">{summary}</p>}

      {steps.length > 0 && (
        <React.Fragment>
          <h2 className="gd-h2">How it's worked out</h2>
          <ol className="gd-steps">
            {steps.map((s, i) => (
              <li key={i}><span className="gd-step-n mono">{String(i + 1).padStart(2, "0")}</span><span><b>{s.h}</b> {s.t}</span></li>
            ))}
          </ol>
          <p className="muted gd-small"><a {...guideLinkProps("how-money")}>How every dollar figure is worked out &rarr;</a></p>
        </React.Fragment>
      )}

      <h2 className="gd-h2">{f.is_finding ? "A flagged row from this export" : "A row from this export"}</h2>
      <ExampleRow f={f} filters={filters} />

      {rungs.length > 0 && (
        <React.Fragment>
          <h2 className="gd-h2">What to do</h2>
          <div className="gd-todo">
            {rungs.map((r, i) => (
              <div key={i} className="gd-card gd-todo-card">
                <span className={`gd-chip ${r.tier === "free" ? "ok" : r.tier === "spend" ? "warn" : ""}`}>{r.tier ? `${i + 1} · ${r.tier}` : `Step ${i + 1}`}</span>
                <span>{r.text}</span>
              </div>
            ))}
          </div>
        </React.Fragment>
      )}

      {offCards.length > 0 && (
        <React.Fragment>
          <h2 className="gd-h2">Why the number could be off</h2>
          <div className="gd-off">
            {offCards.map((c, i) => (
              <div key={i} className="gd-card gd-off-card">
                {c.h && <b>{c.h}</b>}
                {c.list ? <ul>{c.list.map((x, j) => <li key={j}>{codeifyProse(x)}</li>)}</ul> : <span>{c.t}</span>}
              </div>
            ))}
          </div>
        </React.Fragment>
      )}

      <h2 className="gd-h2">Run it yourself</h2>
      <p className="muted gd-small">The exact query the export runs for this check, with the window and today's date filled in.</p>
      <RunItYourself queryId={f.query_id} windowDays={filters.window} />

      {f.columns && f.columns.length > 0 && (
        <details className="gd-details">
          <summary>{`Columns (${f.columns.length})`}</summary>
          <table className="guide2-cols">
            <thead><tr><th>Column</th><th>Meaning</th><th>Unit</th></tr></thead>
            <tbody>
              {f.columns.map((c) => {
                const meta = columnMeta(c.name, c.kind);
                return (
                  <tr key={c.name}>
                    <td className="mono">{c.name}</td>
                    <td>{meta.label}{meta.help && <div className="muted">{meta.help}</div>}</td>
                    <td className="muted">{meta.unit || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </details>
      )}
      {f.requires && f.requires.trim().toLowerCase() !== "n/a" && <p className="muted gd-small">Needs: {codeifyProse(f.requires)}</p>}
      <ReviewerDetails f={f} />
    </article>
  );
}

function GuideCheckPage({ data, guideIndex, f, cloud, filters, findingsById }: {
  data: GuideData; guideIndex: GuideIndex; f: GuideFinding; cloud: string; filters: GuideFilters; findingsById: Record<string, FindingSummary>;
}) {
  const home = homeForQuery(f);
  const area = AREA_REGISTRY[home.tab] || null;
  return (
    <GuidePage
      current={f.query_id}
      guideIndex={guideIndex}
      checkArea={area && GUIDE_TABS[home.tab] ? home.tab : null}
      rail={<CheckRail f={f} data={data} cloud={cloud} home={home} area={area} findingsById={findingsById} />}
    >
      <GuideArticle f={f} data={data} filters={filters} home={home} area={area} />
    </GuidePage>
  );
}

// ═══════════════════════════════════ Root ═══════════════════════════════════

export function GuideTab({ focus, filters, findingsById, meta }: {
  focus: string | null; filters: Filters | null; findingsById?: Record<string, FindingSummary> | null; meta?: Meta | null;
}) {
  const [data, setData] = React.useState<GuideData | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [cloudOverride, setCloudOverride] = React.useState<string | null>(() => {
    try {
      const v = window.localStorage.getItem(GUIDE_CLOUD_STORAGE_KEY);
      return v && CLOUD_OPTIONS.includes(v) ? v : null;
    } catch (e) {
      return null;
    }
  });

  React.useEffect(() => {
    Api.guide().then(setData).catch((e) => setError(e.message || String(e)));
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

  if (topic && topic.indexOf("area-") === 0 && GUIDE_TABS[topic.slice(5)] && guideArea(topic.slice(5))) {
    return <GuideTabPage areaKey={topic.slice(5)} data={data} guideIndex={guideIndex} findingsById={byId} meta={meta} />;
  }
  if (topic === "troubleshooting") return <GuideTroubleshooting guideIndex={guideIndex} />;
  if (topic === "glossary") return <GuideGlossaryPage guideIndex={guideIndex} />;
  if (topic === "checks") return <GuideCheckReference guideIndex={guideIndex} findingsById={byId} />;
  if (topic && GUIDE_SECTIONS.some((s) => s.slug === topic)) {
    return <GuideTopicPage slug={topic} data={data} guideIndex={guideIndex} cloud={cloud} />;
  }
  const checkF = focus && !topic ? guideIndex[focus] : null;
  if (checkF) {
    return <GuideCheckPage data={data} guideIndex={guideIndex} f={checkF} cloud={cloud} filters={safeFilters} findingsById={byId} />;
  }

  return (
    <GuideHome
      data={data}
      guideIndex={guideIndex}
      cloud={cloud}
      setCloud={setCloud}
      cloudNote={cloudNote}
      overrideStale={overrideStale}
      useSnapshotCloud={useSnapshotCloud}
    />
  );
}

