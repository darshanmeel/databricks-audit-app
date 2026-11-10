// SummaryTile: one summary figure with its severity, cause and first fix; the detail stays in the tab it links to.
// A missing reason or action is shown as nothing, never guessed; the first action shows only on critical or warn.

import React from "react";
import type {
  AggState, AggregateData, Fact, FindingData, FindingState, FindingSummary, LibraryCorrection, Meta, Row,
} from "../types";

import { fmtInt } from "../format";
import { numOrZero } from "./hooks";
import { bandOf } from "./tab_registry";
import { Badge, Facts, StatusPill, Tip } from "./primitives";
import { OUTSIDE_REGION_LABEL, notAssessedText } from "./scope";

// The first rung of the header's actions ladder (cheapest first), verbatim; the cost tier is read from the text since headers phrase it several ways.
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

// The tile shows the head of the rung; the whole rung sits in `title`.
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


// Shared strip helpers: one place for the not-assessed vs zero logic every tab's strip needs.

// A header field cut to its first sentence, without the library's "- field heuristic" hedge.
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

// The cause from the header (investigate_if, then read_this), or null rather than an invented one.
function headerReason(header: { investigate_if?: string; read_this?: string } | null | undefined): string | null {
  return clause(header && header.investigate_if) || clause(header && header.read_this) || null;
}

// A tile driven by one finding's band. Only a real OK band reads "0 flagged"; not assessed, error, empty window
// and empty filters each read as what they are.
export function singleIdTileProps(label: string, f: FindingSummary | null | undefined, state: FindingState | null | undefined,
  pickFlagged?: PickFlagged | null, emptyWindowReading?: string): TileProps {
  const base = _singleIdTileProps(label, f, state, pickFlagged, emptyWindowReading);
  // Library corrections ride on the bulk findings row, so they are merged once here.
  return { ...base, corrections: (f && f.library_corrections) || [] };
}

function _singleIdTileProps(label: string, f: FindingSummary | null | undefined, state: FindingState | null | undefined,
  pickFlagged?: PickFlagged | null, emptyWindowReading?: string): TileProps {
  const band = f ? bandOf(f) : "NOT_ASSESSED";
  if (band === "CRITICAL" || band === "WARN") {
    // The tile's own fetch can settle differently from the bulk list; show its own outcome, not "Loading..." forever.
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
    // The bulk list's status_counts are exact; the tile's own rows may be capped, so `sliced` says so.
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
    // "Could not run" (not_assessed) differs from "ran, but every row was not assessable" (ok_rows).
    if (f.outcome === "ok_rows") {
      const naCount = f.status_counts ? numOrZero(f.status_counts.NOT_ASSESSED) : 0;
      const base = `${label}: ran, but none of its ${fmtInt(naCount)} row${naCount === 1 ? "" : "s"} could be judged.`;
      // Say why a row was not assessed, in the header's own words for the code.
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
    // Every selected workspace is outside the snapshot's region.
    if (f.not_assessed_reason === "outside_region") {
      return { value: "-", reading: `${label}: not assessed, ${OUTSIDE_REGION_LABEL}.`, facts: statusFact(OUTSIDE_REGION_LABEL, "muted"), severity: "not_assessed" };
    }
    return { value: "-", reading: `${label} did not run in this export.`, facts: statusFact("Did not run in this export", "muted"), severity: "not_assessed" };
  }
  if (band === "ERROR") return { value: "-", reading: "Could not read this table.", facts: statusFact("Could not read this table.", "muted"), severity: "not_assessed" };
  // "-", never "0": next to "flagged" a 0 reads as a clean pass.
  if (band === "EMPTY_WINDOW") { const t = emptyWindowReading || "Nothing in this window."; return { value: "-", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  if (band === "EMPTY_FILTERS") { const t = "Rows exist, but the current filters exclude them."; return { value: "-", reading: t, facts: statusFact(t, "muted"), severity: "neutral" }; }
  return { value: "0", unit: "flagged", reading: `No ${label.toLowerCase()} findings flagged this window.`, facts: statusFact("Nothing flagged", "ok"), severity: "ok" };
}

// A tile from an aggregate over one finding's rows (inventory checks have no status). A network error is caught
// before the ready check, so it never reads "Loading...".
export function aggregateTileProps(state: FindingState | null | undefined, compute: (rows: Row[], data: FindingData) => TileProps,
  emptyReading?: string): TileProps {
  const base = _aggregateTileProps(state, compute, emptyReading);
  // Every outcome carries library_corrections.
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

// aggregateTileProps for a useFindingAgg state: same four outcomes; compute() gets the whole payload.
export function aggregateGroupTileProps(state: AggState | null | undefined, compute: (data: AggregateData) => TileProps,
  emptyReading?: string): TileProps {
  const base = _aggregateGroupTileProps(state, compute, emptyReading);
  // Every outcome carries library_corrections.
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
  // A short snapshot read at a longer window names the days it actually holds.
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
