// SummaryTile (tasks/T-60-overview-is-the-product.md's
// "shape" sharpening): the one reusable building block for a domain-level summary strip. Overview
// is the first caller (a 5-tile row: Spend, Run-rate, Waste, Jobs, Performance); a follow-up task
// is expected to give every other tab (Cost, Compute, Jobs, ...) its own 3-5 tile strip above that
// tab's own drill-down, using this SAME component -- see the "How another tab should call this"
// note at the bottom of this file.
//
// A tile answers "is this a problem" (severity -> left-accent colour), "how big" (value/unit +
// reading -- the WHAT), "why" (reason -- the mechanism the query actually flagged, its own line),
// and "where do I go to look closer" (href, optional). Deliberately no row-level detail, no
// scrollbar -- that is the drill-down, and it stays in the tab this tile links to.
//
// WHAT vs WHY (both optional, never fabricated): `reading` is what the figure is about / which
// finding is driving it ("Led by SQL warehouse idle tail before auto-stop"); `reason` is the CAUSE
// that finding's own data gives for being flagged ("3 warehouses never auto-stop" / "longest idle
// stretch 6h, threshold 4h") -- never an adjective, never a restatement of the number. A caller
// must source `reason` from the finding itself, in this order: (1) a column on the finding's own
// rows the caller has verified exists (the query's predicate IS the reason it flagged that row);
// (2) the header's `investigate_if`; (3) the header's `read_this`. If none of those yields a
// concrete cause, `reason` is omitted -- a missing reason is honest, a guessed one is not. A tile
// with no verified figure yet passes value="-" and a `reading` that says why (loading / not
// assessed / no estimate) rather than a fake zero -- the honesty rule reaches into tile space
// exactly the way ChartNote already does for charts (charts.tsx).
//
// AND THE FIRST STEP (DEC-59): `action` = {text, tier} -- the FIRST rung of the finding's own
// `actions:` ladder, which the library writes deliberately in cost order (free, then config, then
// spend), so rung 1 is always the cheapest thing that might fix it. Rendered only when the tile is
// critical or warn -- the component enforces that rather than trusting the caller, because a
// suggestion under a clean tile is noise and noise is how a reader learns to skim the whole strip.
// Same sourcing discipline as `reason`: verbatim from the header, shortened but never rewritten,
// and absent when the header has no ladder -- an inventory query's "actions: n/a" parses to an
// empty list, and 45 of the 112 headers in the two manifests are that shape). `tier` is the "(free)" / "(config)" / "(spend)" suffix the header's own
// text carries, surfaced as a chip so a reader scanning a strip of CRITICAL tiles can do the
// free ones first. Rungs 2 and 3 stay in the drill-down (finding_detail.tsx already renders the
// whole ladder) -- they are choices with trade-offs; rung 1 is what you do before you think.

import React from "react";
import type {
  AggState, AggregateData, Fact, FindingData, FindingState, FindingSummary, LibraryCorrection, Meta, Row,
} from "../types";

import { fmtInt } from "../format";
import { numOrZero } from "./hooks";
import { bandOf } from "./tab_registry";
import { Badge, Facts, StatusPill, Tip } from "./primitives";
import { OUTSIDE_REGION_LABEL, notAssessedText } from "./scope";

// -- DEC-59: the first thing to try ----------------------------------------------------------
// Every library header writes `actions:` as a three-rung ladder in cost order -- free, then
// config, then spend -- so rung 1 is always the cheapest thing that might fix the finding, which
// is exactly what belongs on a summary. The manifest parser has already stripped the '1)'
// numbering and split the rungs, and turns an inventory query's 'actions: n/a' into an empty
// list, so 'no ladder' arrives here as falsy rather than as the literal string (45 of the 112
// headers in the two manifests are that shape). Taken verbatim, only shortened -- never
// rewritten, never invented, exactly as findingReason() above treats the WHY.
//
// The trailing cost tier is NOT always one of three words: the headers also carry
// '(spend/eng time)', '(spend/process)', '(config / spend)' and four more variants, so it is
// classified by what the text contains, cheapest reading first, rather than matched against a
// fixed list that would silently drop the chip on the seven queries that phrase it differently.
/** A fix to try first: the tile-sized text, the full rung, and its cost tier. */
export interface TileAction {
  text: string;
  full: string;
  tier: string | null;
}

export interface TileDelta {
  text: React.ReactNode;
  direction?: string;
}

/** What a summary tile shows; the tile helpers below return it and pages may add to it. */
export interface TileProps {
  label?: React.ReactNode;
  value?: React.ReactNode;
  unit?: React.ReactNode;
  reading?: React.ReactNode;
  reason?: React.ReactNode;
  action?: TileAction;
  severity?: string;
  facts?: Fact[];
  corrections?: LibraryCorrection[];
  href?: React.ReactNode;
  onClick?: () => void;
  scope?: React.ReactNode;
  delta?: TileDelta | null;
  sparkline?: number[] | null;
  footnote?: React.ReactNode;
  /** The compact ok_rows reading a page's own compute() sets instead of reading/severity. */
  note?: React.ReactNode;
  tone?: string | null;
  [extra: string]: unknown;
}

/** What a single-check tile's caller computes from its flagged rows. */
export type PickFlagged = (
  flagged: Row[], data: FindingData, slice: { flaggedTotal: number; sliced: boolean },
) => Partial<TileProps> | null | undefined;

export function actionTier(raw: unknown): string | null {
  const t = String(raw || '').toLowerCase();
  if (t.includes('free')) return 'free';
  if (t.includes('config')) return 'config';
  if (t.includes('spend')) return 'spend';
  return null;
}

// `full` is the whole rung, `text` is the tile-sized head of it. A tile is ~170px wide, and a
// 120-character instruction inside one is eight lines of 10.5px text -- that is a paragraph, and
// DEC-58 rule 4 ("a number, a reading, and a link, never a table") plus DEC-57 ("volume is the
// enemy") both say no. So the tile shows the head and carries the untruncated rung in `title`,
// where it costs no screen space -- the same trick the deep-link caveat uses in names.tsx.
// Nothing is lost and nothing is rewritten: the drill-down panel still renders all three rungs.
export function firstAction(header: { actions?: string[] } | null | undefined): TileAction | null {
  const ladder = header && header.actions;
  if (!ladder || !ladder.length) return null;
  let full = String(ladder[0]).trim();
  if (!full || full.toLowerCase().startsWith("n/a")) return null;
  // Classify the cost tier from wherever the rung states it -- one header
  // (cost_vector_search_spend) puts "(free)" mid-sentence rather than at the end -- but only
  // STRIP it when it trails, since cutting it out of the middle would mangle the sentence.
  let tier: string | null = null;
  const anywhere = full.match(/\(([^()]*(?:free|config|spend)[^()]*)\)/i);
  if (anywhere) tier = actionTier(anywhere[1]);
  const trailing = full.match(/\s*\(([^()]*)\)\s*[;.]?\s*$/);
  if (trailing && actionTier(trailing[1])) full = full.slice(0, trailing.index).trim();
  full = full.replace(/[;.]+$/, "").trim();
  if (!full) return null;
  // Truncate on a word boundary, never mid-word.
  let text = full;
  if (text.length > 90) {
    const cut = text.slice(0, 90);
    const lastSpace = cut.lastIndexOf(" ");
    text = `${(lastSpace > 40 ? cut.slice(0, lastSpace) : cut).replace(/[\s,;:.–-]+$/, "")}...`;
  }
  return { text, full, tier };
}

const TILE_SEVERITIES = ["critical", "warn", "ok", "not_assessed", "neutral"];

// One-fact shorthand for the shared honesty branches below (loading/error/empty/not-assessed) --
// a single labelled "Status" row instead of the same words as a bare sentence, trailing period(s)
// stripped since a Facts value never ends in one.
function statusFact(text: string, tone: string): Fact[] {
  return [{ label: "Status", value: String(text || "").replace(/\.+$/, ""), tone }];
}

// P3 (DESIGN-DIRECTION.md section 6): the optional 36px --c1 sparkline, last point marked with a
// dot. `points` is a plain array of numbers (already computed by the caller -- this draws, never
// fetches or aggregates). Fewer than 2 points has nothing to draw a line between, so it renders
// nothing rather than a flat or fabricated line.
export function SummaryTileSpark({ points }: { points?: number[] | null }) {
  // Review round 1: a single null/NaN point (a day with no reading) rode straight into the path
  // string as "MNaN,NaN" -- a console error and no line at all, not just a gap at that point.
  const finite = (points || []).filter((p) => Number.isFinite(p));
  if (finite.length < 2) return null;
  const w = 120, h = 36, pad = 2;
  const min = Math.min(...finite), max = Math.max(...finite);
  const span = max - min || 1;
  const stepX = (w - pad * 2) / (finite.length - 1);
  const coords = finite.map((p, i) => [
    pad + i * stepX,
    pad + (h - pad * 2) * (1 - (p - min) / span),
  ]);
  const path = coords.map((c, i) => `${i === 0 ? "M" : "L"}${c[0].toFixed(1)},${c[1].toFixed(1)}`).join(" ");
  const last = coords[coords.length - 1];
  return (
    <svg className="summary-tile-spark" viewBox={`0 0 ${w} ${h}`} width="100%" height="36" preserveAspectRatio="none" aria-hidden="true">
      <path d={path} fill="none" stroke="var(--c1)" strokeWidth="1.6" />
      <circle cx={last[0]} cy={last[1]} r="2.4" fill="var(--c1)" />
    </svg>
  );
}

// The optional delta line ("+ $3,120 (7%) vs previous 30 days"): `direction` picks the arrow and
// the diverging colour (section 2's --div-up/--div-down are about MAGNITUDE of change, not
// good/bad -- more spend is not always bad, less is not always good, so this never reads as
// success/failure green/red). `text` is the caller's own already-worded rest of the sentence.
export function SummaryTileDelta({ delta }: { delta?: TileDelta | null }) {
  if (!delta || !delta.text) return null;
  const down = delta.direction === "down";
  return (
    <div className={`summary-tile-delta ${down ? "down" : "up"}`}>
      <span className="summary-tile-delta-arrow">{down ? "▼" : "▲"}</span>
      <span className="summary-tile-delta-text">{delta.text}</span>
    </div>
  );
}

// A short "Library issue" / "Corrected" line on any tile fed by a query_id the library-corrections
// register carries an entry for. Full problem/effect/fix text is one hover away; the finding's own
// detail panel (already one click away via the tile's own onClick/href) lays out every field.
function SummaryTileLibraryLine({ corrections }: { corrections?: LibraryCorrection[] | null }) {
  if (!corrections || corrections.length === 0) return null;
  const allFixed = corrections.every((c) => c.status === "fixed");
  const tip = corrections.map((c) => `${c.status === "fixed" ? "Corrected" : "Not fixed yet"}: ${c.problem}`).join(" / ");
  return (
    <div className="summary-tile-library">
      <Tip text={tip}>
        <Badge kind={allFixed ? "ok" : "warn"}>{allFixed ? "query corrected" : "library issue"}</Badge>
      </Tip>
    </div>
  );
}

// `scope` says what the figure covers when the workspace filter does not reach it.
function SummaryTile({ label, value, unit, reading, reason, action, severity, href, onClick, corrections, scope, delta, sparkline, footnote, facts }: TileProps) {
  const sev = severity && TILE_SEVERITIES.includes(severity) ? severity : "neutral";
  // An action belongs only under something that is wrong; enforced here so no caller can leak one.
  const showAction = !!(action && action.text && (sev === "critical" || sev === "warn"));
  const clickable = typeof onClick === "function";
  const hasStatus = sev !== "neutral";
  return (
    <div
      className={`summary-tile sev-${sev}${clickable ? " clickable" : ""}`}
      onClick={clickable ? onClick : undefined}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable ? (e) => { if (e.key === "Enter" || e.key === " ") onClick!(); } : undefined}
    >
      <div className="summary-tile-label">{label}</div>
      <div className="summary-tile-value">
        {value}
        {unit && <span className="summary-tile-unit">{unit}</span>}
      </div>
      {scope && <div className="summary-tile-scope">{scope}</div>}
      <SummaryTileDelta delta={delta} />
      <SummaryTileSpark points={sparkline} />
      {facts ? <div className="summary-tile-reading"><Facts items={facts} /></div>
        : (reading && <div className="summary-tile-reading" title={typeof reading === "string" ? reading : undefined}>{reading}</div>)}
      {reason && <div className="summary-tile-reason">{reason}</div>}
      <SummaryTileLibraryLine corrections={corrections} />
      {showAction && action && (
        <div className="summary-tile-action" title={action.full || action.text}>
          {action.tier && <span className={`tier-chip tier-${action.tier}`}>{action.tier}</span>}
          <span className="summary-tile-action-text">{action.text}</span>
        </div>
      )}
      {footnote && <div className="summary-tile-footnote">{footnote}</div>}
      {href && (hasStatus ? (
        <div className="summary-tile-status-row">
          <StatusPill kind={sev} />
          <span className="summary-tile-open">{"Open →"}</span>
        </div>
      ) : (
        <div className="summary-tile-link">{href} &rarr;</div>
      ))}
    </div>
  );
}

// `compact` clamps each reading to 2 lines, for Overview's at-a-glance row.
function SummaryTileRow({ children, compact }: { children?: React.ReactNode; compact?: boolean }) {
  return <div className={`summary-tile-row${compact ? " compact" : ""}`}>{children}</div>;
}


// ---------------------------------------------------------------------------------------------
// Shared strip helpers (T-62). Every per-tab strip needs the same three things: a header field
// trimmed to one tile-sized clause, a tile driven by ONE finding id's own CRITICAL/WARN band, and
// a tile driven by an aggregate over a finding's rows. They live here, beside the component they
// feed, so there is exactly ONE definition of the honesty logic -- the four-outcome rule in
// aggregateTileProps and the band handling in singleIdTileProps are the places a tab is most
// likely to quietly conflate "not assessed" with "zero", and that must not be re-implemented
// eleven times.

// DEC-58.6: a header field cut to its first sentence, with the library's own "- field heuristic"
// hedge stripped (it qualifies the threshold, not the cause, and eats the tile).
function clause(text: unknown, maxLen?: number): string | null {
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
  const m = s.match(/^(.*?[.!?])(\s|$)/);
  if (m) s = m[1];
  s = s.trim();
  const cap = maxLen || 160;
  if (s.length > cap) s = `${s.slice(0, cap - 1).trim()}...`;
  return s || null;
}

// The WHY, sourced in DEC-58.6 order: the finding's own columns are tier 1 and are the caller's
// job (only the caller knows which columns mean something); this is the tier 2/3 fallback. It
// returns null rather than inventing a cause, and a null reason renders as no reason at all.
function headerReason(header: { investigate_if?: string; read_this?: string } | null | undefined): string | null {
  return clause(header && header.investigate_if) || clause(header && header.read_this) || null;
}

// A tile driven by ONE finding id's own band. `pickFlagged(flaggedRows, data, sliceInfo)` returns
// any of {value, unit, reading, reason} computed from that id's flagged rows; whatever it omits
// falls back to a count of flagged rows and headerReason(). `sliceInfo` (review round 2, see
// below) is {flaggedTotal, sliced} -- most callers ignore it and use `flagged` as before; a caller
// that wants to word its own reading around the cap can read it. Non-flagged bands never reach
// pickFlagged, so a caller cannot accidentally print a number for a finding that did not run.
//
// T-67: every band bandOf() can return for a finding gets its own honest branch here -- CRITICAL/
// WARN (unchanged), NOT_ASSESSED (the model failed/was skipped, OR ran but every row was itself
// NOT_ASSESSED -- review round 2 splits these two, see below), ERROR (a local read failure),
// EMPTY_WINDOW (a real but unjudged zero -- nothing landed in this window to judge) and
// EMPTY_FILTERS (rows exist, the current filters hide every one). Only a genuine OK band -- rows
// existed, were judged, and none were flagged -- reads "0 flagged"/green; the other four never do,
// whatever band bandOf() previously fell through to (this helper used to default anything past
// NOT_ASSESSED straight to the OK branch, which is the bug this task closes). `emptyWindowReading`
// is an optional caller-supplied words for the EMPTY_WINDOW case (e.g. "No job ran in this
// window."), the same 3rd-arg shape aggregateTileProps' `emptyReading` already uses -- falls back
// to the same "Nothing in this window." every aggregate tile and pressureTileHonesty use, so the
// same outcome reads the same words wherever it is shown.
export function singleIdTileProps(label: string, f: FindingSummary | null | undefined, state: FindingState | null | undefined,
  pickFlagged?: PickFlagged | null, emptyWindowReading?: string): TileProps {
  const base = _singleIdTileProps(label, f, state, pickFlagged, emptyWindowReading);
  // T-75A review round 1 (DEC-66.2): `f` is this id's own row off the bulk /api/findings list,
  // which already carries `library_corrections` (service.library_corrections_for, attached to
  // EVERY row) -- no extra fetch needed. Merged onto every branch's return value here, once,
  // rather than at each of this function's many early returns.
  return { ...base, corrections: (f && f.library_corrections) || [] };
}

function _singleIdTileProps(label: string, f: FindingSummary | null | undefined, state: FindingState | null | undefined,
  pickFlagged?: PickFlagged | null, emptyWindowReading?: string): TileProps {
  const band = f ? bandOf(f) : "NOT_ASSESSED";
  if (band === "CRITICAL" || band === "WARN") {
    // T-67 review round 2: `f`/`band` comes from the bulk /api/findings list; `state` is this
    // tile's OWN separate /api/finding/{id} fetch -- a different HTTP call that can resolve to a
    // different outcome (a network blip hits one call and not the other, or the two race a filter
    // change). This branch used to show "Loading..." for every non-ok_rows state forever, so a
    // tile whose OWN fetch had already settled into outcome 'error'/'not_assessed'/'ok_empty_
    // window'/'ok_empty_filters' stayed on a red/amber "Loading..." instead of an honest read.
    // "Loading..." now shows only while state itself is genuinely still loading; once it resolves,
    // this tile reads its own outcome rather than free-riding on the bulk list's happier band.
    if (!state || state.phase === "loading") {
      return { value: "-", reading: "Loading...", facts: statusFact("Loading...", "muted"), severity: band === "CRITICAL" ? "critical" : "warn" };
    }
    if (state.phase === "error" || state.outcome === "error") {
      return { value: "-", reading: "Could not read this table.", facts: statusFact("Could not read this table.", "muted"), severity: "not_assessed" };
    }
    if (state.outcome === "not_assessed") {
      const text = `${label} did not run in this export.`;
      return { value: "-", reading: text, facts: statusFact(text, "muted"), severity: "not_assessed" };
    }
    if (state.outcome === "ok_empty_filters") {
      const text = "Rows exist, but the current filters exclude them.";
      return { value: "-", reading: text, facts: statusFact(text, "muted"), severity: "neutral" };
    }
    if (state.outcome === "ok_empty_window") {
      const text = emptyWindowReading || "Nothing in this window.";
      return { value: "-", reading: text, facts: statusFact(text, "muted"), severity: "neutral" };
    }
    // state.outcome === "ok_rows" from here on -- the only outcome pickFlagged() may read rows for.
    const rows = state.data.rows;
    const flagged = rows.filter((r) => r.status === "CRITICAL" || r.status === "WARN");
    // T-67 review round 2 ("a capped row slice never reads as the whole finding"): this tile's own
    // fetch defaults to a 5,000-row cap (useFindingData), so `flagged` can be a slice of every
    // flagged row that build actually judged. The bulk /api/findings list's own status_counts is
    // an EXACT tally over every row the build judged under the SAME filters (service.list_findings
    // counts off the full result, not a paginated slice), so it is the source of truth for "how
    // many are really flagged" and for the tile's default value -- a caller's pickFlagged may still
    // compute a sum/worst-row off the (possibly smaller) `flagged` slice, so `sliced` is passed
    // through for it to word its own reading around.
    const sc = f && f.status_counts;
    const flaggedTotal = sc ? (numOrZero(sc.CRITICAL) + numOrZero(sc.WARN)) : flagged.length;
    const sliced = flaggedTotal > flagged.length;
    const picked: Partial<TileProps> = pickFlagged ? (pickFlagged(flagged, state.data, { flaggedTotal, sliced }) || {}) : {};
    const header = state.data.header;
    const baseReading = picked.reading || `${flagged.length} row${flagged.length === 1 ? "" : "s"} flagged.`;
    const slicedNote = sliced
      ? ` Figures from the first ${fmtInt(flagged.length)} of ${fmtInt(flaggedTotal)} flagged rows.` : "";
    // `baseReading` can be a JSX element (a couple of pressure tiles render a chip inline), so the
    // note is appended by string concatenation only when it is plain text, never by coercing a
    // React element through `+` (which would print "[object Object]").
    const reading = !slicedNote ? baseReading
      : (typeof baseReading === "string" ? baseReading + slicedNote
          : <React.Fragment>{baseReading}{slicedNote}</React.Fragment>);
    // `picked.facts` is the caller's own labelled facts for this finding (preferred by every card
    // that renders `facts`); a caller with no facts of its own gets none here rather than a guessed
    // split of its free-text `reading` -- see the per-caller pickFlagged in tab_cost.tsx for the
    // worked example. A capped row slice still needs saying, so it is its own trailing fact rather
    // than silently dropped the way it would be if `facts` replaced `reading` wholesale.
    const facts = picked.facts
      ? (sliced ? [...picked.facts, { label: "Note", value: `first ${fmtInt(flagged.length)} of ${fmtInt(flaggedTotal)}`, detail: "flagged rows", tone: "muted" }] : picked.facts)
      : undefined;
    return {
      value: picked.value != null ? picked.value : fmtInt(flaggedTotal),
      unit: picked.unit,
      reading,
      facts,
      reason: (picked.reason !== undefined ? picked.reason : headerReason(header)) || undefined,
      action: header ? (firstAction(header) || undefined) : undefined,
      severity: band === "CRITICAL" ? "critical" : "warn",
    };
  }
  if (!f) return { value: "-", reading: `${label}: not in the registry.`, facts: statusFact("Not in the registry", "muted"), severity: "not_assessed" };
  if (band === "NOT_ASSESSED") {
    // T-67 review round 2: bandOf() folds two different truths into one band -- the finding never
    // ran at all (outcome 'not_assessed': the model failed/was skipped), vs it ran and returned
    // real rows where every single one was itself judged NOT_ASSESSED at the row level (outcome
    // 'ok_rows' -- bandOf's own T-67 rule, e.g. every job in scope was serverless so no cluster
    // metrics existed to judge). Only the first is honestly "could not run this build"; the second
    // ran fine and simply had nothing it could judge, which is a different fact for a reader.
    if (f.outcome === "ok_rows") {
      const naCount = f.status_counts ? numOrZero(f.status_counts.NOT_ASSESSED) : 0;
      const base = `${label}: ran, but none of its ${fmtInt(naCount)} row${naCount === 1 ? "" : "s"} could be judged.`;
      // T-69: the vendored convention is "NOT_ASSESSED is not a pass: read not_assessed_reason"
      // (task_cluster_utilization.sql and others already carry that column) -- when this tile's
      // own fetch has resolved and a row carries that column, surface it instead of only a bare
      // count, so a NOT_ASSESSED finding reads WHY, not just that. Silently a no-op (falls back
      // to `base`) for any finding that carries no such column.
      // P4-03: the raw code (e.g. "no_cluster_recorded") is never shown -- the header's own
      // not_assessed_reasons dict (service.substitute_header_params) is the one place those codes
      // are put into words; a code the header does not know (should not happen once every query
      // that emits this column carries the field) falls back to the raw code rather than nothing.
      let reason: string | null = null;
      if (state && state.phase === "ready" && state.outcome === "ok_rows" && state.data && state.data.rows) {
        const withReason = state.data.rows.find((r) => r.not_assessed_reason);
        if (withReason) {
          const words = state.data.header && state.data.header.not_assessed_reasons;
          reason = (words && words[withReason.not_assessed_reason]) || withReason.not_assessed_reason;
        }
      }
      const naReading = reason ? `${base} ${reason}` : base;
      return {
        value: "-", reading: naReading,
        facts: reason ? [{ label: "Not judged", value: fmtInt(naCount), detail: "rows" }, { label: "Why", value: reason }] : statusFact(base, "muted"),
        severity: "not_assessed",
      };
    }
    // T-71: every selected workspace is outside the snapshot's region -- say that, not "could not run".
    if (f.not_assessed_reason === "outside_region") {
      return { value: "-", reading: `${label}: not assessed, ${OUTSIDE_REGION_LABEL}.`, facts: statusFact(OUTSIDE_REGION_LABEL, "muted"), severity: "not_assessed" };
    }
    return { value: "-", reading: `${label} did not run in this export.`, facts: statusFact("Did not run in this export", "muted"), severity: "not_assessed" };
  }
  if (band === "ERROR") return { value: "-", reading: "Could not read this table.", facts: statusFact("Could not read this table.", "muted"), severity: "not_assessed" };
  // T-67 review round 2: value "-" here, never "0" -- next to unit "flagged" a "0" reads as a
  // verified clean pass (PLAN.md 6.2: an empty window/filter set is never "no problems").
  if (band === "EMPTY_WINDOW") { const t = emptyWindowReading || "Nothing in this window."; return { value: "-", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  if (band === "EMPTY_FILTERS") { const t = "Rows exist, but the current filters exclude them."; return { value: "-", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  return { value: "0", unit: "flagged", reading: `No ${label.toLowerCase()} findings flagged this window.`, facts: statusFact("Nothing flagged", "ok"), severity: "ok" };
}

// A tile driven by an AGGREGATE over one id's rows rather than a verdict -- the only option for
// the 45 inventory ids, which carry no status column at all. The four outcomes are handled here,
// once: not_assessed/error read "-", a verified empty window or empty filter set reads a real 0,
// and only ok_rows reaches `compute`. T-67 review round 2: `phase` itself can settle into "error"
// (the fetch failed at the network/HTTP level, before an application-level `outcome` ever
// arrives) -- `phase !== "ready"` alone read that the same as still-loading and said "Loading..."
// forever, so it is caught first, honestly, before the ready check.
export function aggregateTileProps(state: FindingState | null | undefined, compute: (rows: Row[], data: FindingData) => TileProps,
  emptyReading?: string): TileProps {
  const base = _aggregateTileProps(state, compute, emptyReading);
  // T-75A review round 1 (DEC-66.2): `state.data` is the whole API response for every outcome
  // (useFindingData/useFindingAgg set `data: d` on every settle, not only ok_rows; GET
  // /api/finding and GET /api/finding/{id}/aggregate both carry `library_corrections` on every
  // outcome), so this reads regardless of which of the four outcomes the fetch resolved to.
  const corrections = (state && state.data && state.data.library_corrections) || [];
  return { ...base, corrections };
}

function _aggregateTileProps(state: FindingState | null | undefined, compute: (rows: Row[], data: FindingData) => TileProps,
  emptyReading?: string): TileProps {
  const outcome = state && state.outcome;
  const phase = state && state.phase;
  if (phase === "error") return { value: "-", reading: "Could not read this table.", facts: statusFact("Could not read this table.", "muted"), severity: "not_assessed" };
  if (!state || state.phase !== "ready") return { value: "-", reading: "Loading...", facts: statusFact("Loading...", "muted"), severity: "neutral" };
  if (outcome === "not_assessed") { const t = notAssessedText(state.data); return { value: "-", reading: t, facts: statusFact(t, "muted"), severity: "not_assessed" }; }
  if (outcome === "error") return { value: "-", reading: "Could not read this table.", facts: statusFact("Could not read this table.", "muted"), severity: "not_assessed" };
  if (outcome === "ok_empty_window") { const t = emptyReading || "Nothing in this window."; return { value: "0", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  if (outcome === "ok_empty_filters") { const t = "Rows exist, but the current filters exclude them."; return { value: "0", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  return compute(state.data.rows, state.data);
}

// T-68: the aggregateTileProps twin for a useFindingAgg state (server-side groups/total_value
// instead of a row page) -- same four-outcome branching, so a caller cannot tell the two honesty
// rules apart by accident. `compute(data)` receives the whole ok_rows payload
// ({groups, other, total_value, rows_total, matched_rows, discount_pct, ...}) rather than a rows
// array, since a group-shaped result has no single "the rows" to hand back.
export function aggregateGroupTileProps(state: AggState | null | undefined, compute: (data: AggregateData) => TileProps,
  emptyReading?: string): TileProps {
  const base = _aggregateGroupTileProps(state, compute, emptyReading);
  // T-75A review round 1 (DEC-66.2): same reasoning as aggregateTileProps above -- `state.data`
  // carries `library_corrections` on every outcome once GET /api/finding/{id}/aggregate returns
  // it unconditionally.
  const corrections = (state && state.data && state.data.library_corrections) || [];
  return { ...base, corrections };
}

function _aggregateGroupTileProps(state: AggState | null | undefined, compute: (data: AggregateData) => TileProps,
  emptyReading?: string): TileProps {
  const outcome = state && state.outcome;
  const phase = state && state.phase;
  if (phase === "error") return { value: "-", reading: "Could not read this table.", facts: statusFact("Could not read this table.", "muted"), severity: "not_assessed" };
  if (!state || state.phase !== "ready") return { value: "-", reading: "Loading...", facts: statusFact("Loading...", "muted"), severity: "neutral" };
  if (outcome === "not_assessed") { const t = notAssessedText(state.data); return { value: "-", reading: t, facts: statusFact(t, "muted"), severity: "not_assessed" }; }
  if (outcome === "error") return { value: "-", reading: "Could not read this table.", facts: statusFact("Could not read this table.", "muted"), severity: "not_assessed" };
  if (outcome === "ok_empty_window") { const t = emptyReading || "Nothing in this window."; return { value: "0", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  if (outcome === "ok_empty_filters") { const t = "Rows exist, but the current filters exclude them."; return { value: "0", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  return compute(state.data);
}

// A tile's onClick: scroll to that tab's own detail below the strip. There is no router, so this
// is a scroll, not a navigation (see the note on `href` above).
function scrollToAnchor(anchorId: string) {
  const el = document.getElementById(anchorId);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}
// The finding queries window on usage_date >= as_of - N AND usage_date < as_of, so the last day
// with money in it is as_of - 1, never the snapshot day itself. This labels the days actually
// counted. Parsed as UTC on purpose: a local-time parse shifts the boundary by a day for anyone
// west of Greenwich, which would silently mislabel every figure on the tab.
// "16-22 Sep, 7 days of data": the days the money covers, capped at what the snapshot holds.
export function windowShortLabel(asOfDate: string | null | undefined, windowDays: number, snapshotDays?: number | null): string | null {
  const m = asOfDate && windowDays ? /^(\d{4})-(\d{2})-(\d{2})/.exec(String(asOfDate)) : null;
  if (!m) return null;
  const DAY = 86400000;
  const end = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const days = snapshotDays && snapshotDays < windowDays ? snapshotDays : windowDays;
  const first = new Date(end - days * DAY), last = new Date(end - DAY);
  const mon = (d: Date) => ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getUTCMonth()];
  const range = first.getUTCMonth() === last.getUTCMonth()
    ? `${first.getUTCDate()}–${last.getUTCDate()} ${mon(last)}`
    : `${first.getUTCDate()} ${mon(first)} – ${last.getUTCDate()} ${mon(last)}`;
  return `${range}, ${days} days of data`;
}

export function windowRangeLabel(asOfDate: string | null | undefined, windowDays: number, snapshotDays?: number | null): string | null {
  if (!asOfDate || !windowDays) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(asOfDate));
  if (!m) return null;
  const DAY = 86400000;
  const end = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const first = new Date(end - windowDays * DAY);
  const last = new Date(end - DAY);
  const fmt = (d: Date) => `${d.getUTCDate()} ${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getUTCMonth()]}`;
  const full = `${fmt(first)} - ${fmt(last)} ${last.getUTCFullYear()}`;
  // DEC-64: windows are BUILT for 7/30/90 whatever the export captured, so a --days 7 snapshot
  // read at 30d returns 7 days of money under a "30d" label. Naming a date range the snapshot
  // never covered would be a worse lie than the bare "over 30d" this replaced, so when the
  // snapshot is shorter the label states the days actually captured instead of inventing the
  // rest. `snapshot_days` comes from /api/meta (the exporter manifest's own `days`).
  if (snapshotDays && snapshotDays < windowDays) {
    const covFirst = new Date(end - snapshotDays * DAY);
    return `${fmt(covFirst)} - ${fmt(last)} ${last.getUTCFullYear()} -- partial, snapshot covers ${snapshotDays}d of ${windowDays}d`;
  }
  return full;
}

// lakeflow_job_duration_regression's split: the last min(7, half the window) days against the days
// before, whole days only -- with today in the export, today is left out and the range ends yesterday.
export function slowdownSplit(meta: Meta | null, windowDays: number): { recent: number; baseline: number; range: string | null } {
  const span = Math.min(7, Math.floor(windowDays / 2));
  const today = !!(meta && meta.direct_export && meta.direct_export.includes_today);
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String((meta && meta.as_of_date) || ""));
  let range: string | null = null;
  if (m && !(meta!.snapshot_days && meta!.snapshot_days < windowDays)) {
    const DAY = 86400000;
    const end = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    const first = new Date(end - windowDays * DAY), last = new Date(end - (today ? 2 : 1) * DAY);
    const fmt = (d: Date) => `${d.getUTCDate()} ${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getUTCMonth()]}`;
    range = `${fmt(first)} - ${fmt(last)} ${last.getUTCFullYear()}`;
  } else if (meta) {
    range = windowRangeLabel(meta.as_of_date, windowDays, meta.snapshot_days);
  }
  return { recent: today ? span - 1 : span, baseline: windowDays - span, range };
}

// "N jobs took 1.5x or longer in the last 7 days than in the 23 days before (median of successful runs)".
export function slowdownSentence(n: number, split: { recent: number; baseline: number }): string {
  const days = (d: number) => `${d} day${d === 1 ? "" : "s"}`;
  const span = `in the last ${days(split.recent)} than in the ${days(split.baseline)} before`;
  return n
    ? `${fmtInt(n)} job${n === 1 ? "" : "s"} took 1.5x or longer ${span} (median of successful runs)`
    : `No job took 1.5x or longer ${span} (median of successful runs)`;
}

// windowRangeLabel as a Window fact + (when partial) a separate amber Coverage fact, instead of
// one clause long enough to wrap across three lines in a facts value column.
export function windowFacts(asOfDate: string | null | undefined, windowDays: number, snapshotDays?: number | null): Fact[] {
  const full = windowRangeLabel(asOfDate, windowDays, snapshotDays);
  if (!full) return [];
  const cut = full.indexOf(" -- partial, snapshot covers ");
  if (cut === -1) return [{ label: "Window", value: full }];
  return [
    { label: "Window", value: full.slice(0, cut) },
    { label: "Coverage", value: full.slice(cut + " -- partial, snapshot covers ".length), tone: "warn" },
  ];
}
