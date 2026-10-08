// SVG-only chart primitives, no charting library (tasks/
// T-58-web-tabs-and-charts.md hard requirement). Adapted from the reference dashboards
// components/primitives.tsx (Sparkline/Donut/Treemap-squarify/HBar/MiniBar), ported to this
// apps own theme tokens and extended with a few widgets the reference file did not need: Kpi
// (a numeric tile for the Overview KPI row), MultiLine (the daily-DBU trend, one line per
// series), ParetoLine (PLAN.md 6.3s "Top N of M = X% of net DBUs" line), and ChartNote (the
// honesty rules own reach into chart space -- a chart backed by a non-ok_rows outcome must say
// so, never render as an empty/zero chart that could be misread as "nothing to see").
//
// Every primitive here is presentation-only: it takes already-aggregated {name/label, value}
// data and draws it. Aggregation itself (group-by, top-N-plus-Other, sums) lives in hooks.ts so
// it can be reasoned about independently of markup.

import React from "react";

import { flushSync } from "react-dom";

import { Facts } from "./primitives";
import { EmptyChart } from "./charts_more";
import { notAssessedText } from "./scope";
import type { Entry, Fact, StatusInfo } from "../types";

/** What ChartNote reads from a fetch state; a bare phase is enough while loading. */
export interface NoteState {
  phase: string;
  outcome?: string | null;
  data?: { outcome?: string; status_info?: StatusInfo; error?: string } | null;
  error?: string | null;
}

/** An item after foldTopNOther: its colour is set, and Other/untagged are marked. */
export interface FoldedItem extends Entry {
  color: string;
  hatch: boolean;
  isOther?: boolean;
  bg?: string;
}

export interface FoldOptions {
  n?: number | null;
  categorical?: boolean;
  color?: string;
  otherLabel?: string;
}

export interface LegendItem {
  label: React.ReactNode;
  color?: string;
  hatch?: boolean;
  bg?: string;
}

/** A chart's own table twin: columns and one record per row. */
export interface ChartTable {
  columns: { key: string; label: React.ReactNode; align?: string }[];
  rows: Record<string, React.ReactNode>[];
}

export interface Pad {
  l: number;
  r: number;
  t: number;
  b: number;
}

/** The plot area ChartFrame hands to renderPlot. */
export interface PlotGeometry {
  pad: Pad;
  innerW: number;
  innerH: number;
  width: number;
  height: number;
}

/** A donut or stacked-bar slice. */
export interface Segment {
  label: string;
  value: number;
  color?: string;
  display?: React.ReactNode;
}

// P4 (design/DESIGN-DIRECTION.md sections 2 & 5): the chart-series scale is its own token set,
// never a status colour and never the UI accent -- "colour is evidence". Six slots: --c1..--c5 in
// fixed order, then --c-other for whatever a chart folds past its 5th series. Read as CSS variable
// strings (not resolved to hex) so a theme toggle repaints every chart with zero JS re-render.
// paletteColor(i) keeps its old signature (existing callers -- the tabs, pressure.tsx -- index
// into it directly and must keep working unchanged); new P4 components use foldTopNOther below
// instead, which folds at 5 + Other and never cycles past it.
const CHART_PALETTE = [
  "var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)",
];
export const CHART_OTHER_COLOR = "var(--c-other)";
export const CHART_UNTAGGED_BG = "var(--c-untagged-bg)";
export const CHART_UNTAGGED_LINE = "var(--c-untagged-line)";

// Review round 1: this used to be `CHART_PALETTE[i % CHART_PALETTE.length]` over a 6-slot array
// (--c1..--c5, --c-other), so index 5 landed on --c-other same as intended, but index 6+ WRAPPED
// back onto --c1..--c5 -- a 7th or 8th real category silently reused an earlier category's colour
// (indistinguishable in a legend) instead of reading as "folded". Past the 5 validated series
// slots there is only one colour left to give out, Other -- never a repeat of c1..c5.
export function paletteColor(i: number): string {
  return i < CHART_PALETTE.length ? CHART_PALETTE[i] : CHART_OTHER_COLOR;
}

// ─────────── id/name helpers -- "name (abcd…wxyz)", the chart-specific form of NAMES-SPEC's
// "name, not id" rule (section 2 bullet 4: "Charts use 'name (abcd…wxyz)'"). Kept local to
// charts.tsx rather than reusing names.tsx's <Ref> -- <Ref> renders a DOM node (link, copy
// button, job-focus click); chart marks need a plain string for SVG text/title content. ───────────
export function shortId(id: unknown): string {
  const s = id === undefined || id === null ? "" : String(id);
  return s.length <= 10 ? s : s.slice(0, 4) + "…" + s.slice(-4);
}
export function chartLabel(name: unknown, id?: unknown): string {
  // No name at all: NAMES-SPEC's own words for it ("10d3c322-000033 · no name: not in system
  // tables"), not a bare id that could be mistaken for a resolved name that just looks odd.
  if (name === undefined || name === null || name === "") {
    const hasId = id !== undefined && id !== null && id !== "";
    return hasId ? `${shortId(id)} · no name: not in system tables` : "no name: not in system tables";
  }
  const hasId = id !== undefined && id !== null && id !== "";
  return hasId ? `${name} (${shortId(id)})` : String(name);
}
// Per-mark tooltip text, section 5: "name (id) · date · value · basis" -- any missing part is
// skipped rather than rendered as "undefined" or a bare " · ".
export function tipText({ name, id, date, value, basis }: { name?: unknown; id?: unknown; date?: unknown; value?: unknown; basis?: unknown }): string {
  const parts: string[] = [];
  if (name !== undefined && name !== null && name !== "") parts.push(id ? chartLabel(name, id) : String(name));
  else if (id !== undefined && id !== null && id !== "") parts.push(shortId(id));
  if (date) parts.push(String(date));
  if (value !== undefined && value !== null) parts.push(String(value));
  if (basis) parts.push(String(basis));
  return parts.join(" · ");
}

// ─────────── foldTopNOther -- "fold at 5 + Other, colours never cycle" (section 2 & 5), shared by
// every P4 multi-item chart (RankedBars, Columns' series, Icicle's per-parent children). Untagged/
// unknown items are pulled out first and always rendered last with the hatch pattern -- they are
// never counted toward the N and never folded into Other (section 2's untagged rule). `categorical`
// assigns each of the top N its own --c1..--c5 slot (a real category breakdown, e.g. donut
// replacement); the default (false) is one uniform series colour for a plain ranked measure, with
// only the Other bucket getting its own muted colour. ───────────
export function foldTopNOther(items: Entry[] | null | undefined, opts?: FoldOptions): FoldedItem[] {
  const o = opts || {};
  const categorical = !!o.categorical;
  // Review round 1: a categorical chart gives each head item its OWN --c1..--c5 slot (paletteColor
  // below, one per rank) -- there are only 5 of those, so a caller asking for more (n=8, following
  // max_chart_categories) must still fold at 5, or the 6th-8th head items would all fall back to
  // the shared Other colour while still being labelled as if they were distinct. A non-categorical
  // chart (one uniform series colour for every head item) has no such ceiling.
  const n = categorical ? Math.min(o.n || 5, 5) : (o.n || 5);
  const uniform = o.color || "var(--c1)";
  const otherLabel = o.otherLabel || "Other";
  const list = (items || []).filter((it) => it && Number.isFinite(it.value));
  const untagged = list.filter((it) => it.untagged);
  // pooled = rows a query already folded below its own top N: they belong in Other, never ranked
  const pooledTotal = list.filter((it) => it.pooled).reduce((s, it) => s + it.value, 0);
  const rest = list.filter((it) => !it.untagged && !it.pooled).sort((a, b) => b.value - a.value);
  const head: FoldedItem[] = rest.slice(0, n).map((it, i) => ({
    ...it, color: it.color || (categorical ? paletteColor(i) : uniform), hatch: false,
  }));
  const tail = rest.slice(n);
  const tailTotal = tail.reduce((s, it) => s + it.value, 0) + pooledTotal;
  const out = head.slice();
  // "Other (12)" says how many it holds; a pooled row hides its own count, so then just "Other".
  const otherName = tail.length && !pooledTotal ? `${otherLabel} (${tail.length})` : otherLabel;
  if (tailTotal > 0) out.push({ name: otherName, value: tailTotal, color: CHART_OTHER_COLOR, hatch: false, isOther: true });
  untagged.forEach((it) => out.push({ ...it, color: CHART_UNTAGGED_LINE, bg: CHART_UNTAGGED_BG, hatch: true }));
  return out;
}

// ─────────── niceTicks -- 3-5 "clean" y-axis tick values spanning [min,max] (section 5 grammar).
// A small D3-style nice-number pass: round the step to 1/2/5 x a power of ten, then snap both
// ends outward to a multiple of that step. ───────────
export function niceTicks(min: number, max: number, count?: number): number[] {
  const c = count || 4;
  let lo = Number.isFinite(min) ? min : 0;
  let hi = Number.isFinite(max) ? max : 0;
  if (lo === hi) { lo = Math.min(0, lo); hi = Math.max(1, hi); }
  const span = hi - lo;
  const rawStep = span / Math.max(1, c);
  const mag = Math.pow(10, Math.floor(Math.log10(Math.abs(rawStep) || 1)));
  const norm = rawStep / mag;
  const niceNorm = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  const step = niceNorm * mag;
  const niceMin = Math.floor(lo / step) * step;
  const niceMax = Math.ceil(hi / step) * step;
  const out: number[] = [];
  for (let v = niceMin; v <= niceMax + step / 2; v += step) out.push(Math.round(v * 1e6) / 1e6);
  return out;
}

// ─────────── colour-luminance ink (section 5: "Treemap label ink follows the fill's luminance") --
// resolves a "var(--x)" string against the live theme, then picks whichever of a light or dark ink
// has the higher WCAG contrast against that fill, so a label reads on any of --c1..--c5/--seq-*/
// --c-other in either theme, not a fixed near-black. ───────────
function resolveColorVar(value: string): string {
  if (typeof value !== "string") return value;
  const m = value.match(/^var\((--[a-zA-Z0-9-]+)\)$/);
  if (!m) return value;
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim();
    return v || value;
  } catch (e) {
    return value;
  }
}
interface Rgb {
  r: number;
  g: number;
  b: number;
}

function hexToRgb(hex: string): Rgb | null {
  const h = String(hex).trim().replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  if (full.length !== 6 || /[^0-9a-fA-F]/.test(full)) return null;
  return { r: parseInt(full.slice(0, 2), 16), g: parseInt(full.slice(2, 4), 16), b: parseInt(full.slice(4, 6), 16) };
}
function relLuminance(rgb: Rgb): number {
  const f = (v: number) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(rgb.r) + 0.7152 * f(rgb.g) + 0.0722 * f(rgb.b);
}
const INK_DARK = "#171511", INK_LIGHT = "#fbfaf7";
export function inkForFill(fill: string): string {
  const rgb = hexToRgb(resolveColorVar(fill));
  if (!rgb) return "var(--text)";
  const L = relLuminance(rgb);
  const contrast = (a: number, b: number) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  // The ink that contrasts more with the fill: light on a dark fill, dark on a light one.
  return contrast(L, 1) >= contrast(L, 0) ? INK_LIGHT : INK_DARK;
}

// ─────────── RoundRect -- the shared bar mark (section 5 grammar: "4px round data end, a square
// base"). Drawn as one fully-rounded rect plus a same-fill square patch over the base edge, which
// is simpler and more robust than hand-built rounded-on-two-corners path arcs. squareEdge names the
// edge that must stay sharp (the zero/baseline end); the opposite end keeps all its curvature. ──
export function RoundRect({ x, y, w, h, r = 4, fill, squareEdge = "left", className, opacity, title: titleText }: {
  x: number; y: number; w: number; h: number; r?: number; fill?: string;
  squareEdge?: "left" | "right" | "top" | "bottom"; className?: string; opacity?: number; title?: string;
}) {
  if (!(w > 0) || !(h > 0)) return null;
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  const patch =
    squareEdge === "left" ? { x, y, w: rr, h } :
    squareEdge === "right" ? { x: x + w - rr, y, w: rr, h } :
    squareEdge === "top" ? { x, y, w, h: rr } :
    { x, y: y + h - rr, w, h: rr };
  return (
    <g className={className} opacity={opacity}>
      <rect x={x} y={y} width={w} height={h} rx={rr} ry={rr} fill={fill} />
      {rr > 0 && <rect x={patch.x} y={patch.y} width={patch.w} height={patch.h} fill={fill} />}
      {titleText && <title>{titleText}</title>}
    </g>
  );
}

// ─────────── useChartWidth -- ChartFrame's viewBox used to be a fixed 760 (or whatever `width`
// prop the caller hardcoded) scaled to width="100%": on a half-width card (~550px) or a phone
// (390px), that shrinks EVERY svg unit along with it, so 12px tick/label text -- the spec's own
// floor -- rendered at ~8.7px or ~6px (measured). Every charts_more.tsx chart now measures its own
// wrapping element's real CSS width with a ResizeObserver and lays its plot out at THAT width
// instead, so 1 svg unit stays 1 CSS px whatever the card is. `fallback` is only what one frame
// renders before the observer's first callback fires (no ResizeObserver, e.g. an old browser or a
// non-DOM test render, just keeps the fallback forever -- still correct, just not responsive). ──
export function useChartWidth(fallback: number) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [width, setWidth] = React.useState(fallback);
  React.useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver((entries) => {
      const cw = entries[0] && entries[0].contentRect ? entries[0].contentRect.width : 0;
      if (cw > 0) setWidth((w) => (Math.abs(w - cw) >= 1 ? cw : w));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  // Printing: no tooltips on paper, so charts give names a wider column (see beforeprint below).
  const [printFull, setPrintFull] = React.useState(false);
  React.useEffect(() => {
    const onPrint = (e: Event) => {
      const el = ref.current;
      const cw = el ? el.getBoundingClientRect().width : 0;
      if (cw > 0) setWidth(cw);
      setPrintFull(e.type === "chart-print");
    };
    window.addEventListener("chart-print", onPrint);
    window.addEventListener("chart-screen", onPrint);
    return () => {
      window.removeEventListener("chart-print", onPrint);
      window.removeEventListener("chart-screen", onPrint);
    };
  }, []);
  return [ref, Math.max(1, Math.round(width)), printFull] as const;
}

// The browser lays the page out for paper right after beforeprint, so set the paper width
// (body.print-full, c_shell.css) and re-render every chart at it synchronously first.
window.addEventListener("beforeprint", () => {
  document.body.classList.add("print-full");
  flushSync(() => window.dispatchEvent(new Event("chart-print")));
});
window.addEventListener("afterprint", () => {
  document.body.classList.remove("print-full");
  flushSync(() => window.dispatchEvent(new Event("chart-screen")));
});

// ─────────── useThemeVersion -- ThemeToggle (App.tsx) only flips html[data-theme] and triggers no
// React re-render, so a chart that reads a CSS variable at render time (inkForFill, below) keeps
// its PRE-toggle answer until something else happens to re-render it. App.tsx's applyTheme fires a
// "themechange" DOM event on every call (direct toggle AND the matchMedia "Auto" listener); any
// component that subscribes here re-renders once, right after, with the new theme's colours. ────
let _themeVersion = 0;
const _themeVersionListeners = new Set<(v: number) => void>();
if (typeof window !== "undefined") {
  window.addEventListener("themechange", () => {
    _themeVersion += 1;
    _themeVersionListeners.forEach((fn) => fn(_themeVersion));
  });
}
export function useThemeVersion() {
  const [v, setV] = React.useState(_themeVersion);
  React.useEffect(() => {
    const fn = (nv: number) => setV(nv);
    _themeVersionListeners.add(fn);
    return () => { _themeVersionListeners.delete(fn); };
  }, []);
  return v;
}

// ─────────── the 45° "untagged/unknown" hatch (section 2: "never folded into Other"). One
// <pattern> per mounted chart instance (ids must be unique in the document when several charts
// share a page, e.g. gallery.html), referenced as fill={`url(#${id})`}. ───────────
let _hatchSeq = 0;
export function useHatchId(): string {
  const ref = React.useRef<string | null>(null);
  if (ref.current === null) { _hatchSeq += 1; ref.current = `chart-hatch-${_hatchSeq}`; }
  return ref.current;
}
export function HatchDef({ id, line = CHART_UNTAGGED_LINE, bg = CHART_UNTAGGED_BG }: { id: string; line?: string; bg?: string }) {
  return (
    <pattern id={id} width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
      <rect width="6" height="6" fill={bg} />
      <line x1="0" y1="0" x2="0" y2="6" stroke={line} strokeWidth="2" />
    </pattern>
  );
}

// ─────────── ChartFrame -- one shared shell for every axis-based chart (section 5): a hairline
// --grid at each y tick, an --axis baseline/zero line, 3-5 tabular y-tick labels, x labels at the
// first/middle/last position, a legend once there are >= 2 series, and the "Table" toggle that
// swaps the SVG for the chart's own accessible/drill twin. Geometry (padding, inner plot size) is
// handed to the caller's `renderPlot` so the marks and the grid always agree on the same
// coordinates. Charts with no cartesian axis (Heatmap, Icicle, Meter) pass no yTicks/zeroY and get
// just the legend + Table + note chrome. ───────────
export function ChartFrame({
  width = 760, height = 220, pad, yTicks, zeroY, xLabels, legend, table, note, ariaLabel,
  className = "", renderPlot,
}: {
  width?: number; height?: number; pad?: Partial<Pad>; yTicks?: { y: number; label: React.ReactNode }[] | null;
  zeroY?: number | null; xLabels?: React.ReactNode[] | null; legend?: LegendItem[] | null; table?: ChartTable | null;
  note?: React.ReactNode; ariaLabel?: string; className?: string; renderPlot?: (g: PlotGeometry) => React.ReactNode;
}) {
  const P: Pad = Object.assign({ l: 42, r: 8, t: 10, b: 26 }, pad || {});
  const innerW = Math.max(0, width - P.l - P.r);
  const innerH = Math.max(0, height - P.t - P.b);
  const [showTable, setShowTable] = React.useState(false);
  const hasLegend = legend && legend.length >= 2;
  const midXLabel = xLabels && xLabels.length > 2 ? xLabels[Math.floor((xLabels.length - 1) / 2)] : null;
  return (
    <div className={`cf ${className}`}>
      {(hasLegend || table) && (
        <div className="cf-top">
          <div className="cf-legend">
            {hasLegend && legend!.map((l, i) => (
              <span className="cf-leg-item" key={i}>
                {/* Review round 1: a hatched legend entry is always the untagged/no-data pattern
                    (never a per-series hatch colour), so the swatch is drawn entirely by the
                    ".cf-sw.hatch" CSS pattern -- no inline background, which used to paint a SOLID
                    --c-untagged-bg square that was indistinguishable from a real --seq-0 "zero"
                    swatch (both #efece5 in light). */}
                <span className={`cf-sw${l.hatch ? " hatch" : ""}`} style={l.hatch ? undefined : { background: l.color }}></span>
                {l.label}
              </span>
            ))}
          </div>
          {table && (
            <button type="button" className="cf-table-toggle" onClick={() => setShowTable((s) => !s)}>
              {showTable ? "Chart" : "Table"}
            </button>
          )}
        </div>
      )}
      {showTable && table ? (
        <div className="cf-table-wrap">
          <table className="data">
            <thead>
              <tr>{table.columns.map((c, i) => <th key={i} className={c.align === "right" ? "num" : ""}>{c.label}</th>)}</tr>
            </thead>
            <tbody>
              {table.rows.map((row, ri) => (
                <tr key={ri}>{table.columns.map((c, ci) => <td key={ci} className={c.align === "right" ? "num" : ""}>{row[c.key]}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img" aria-label={ariaLabel} className="cf-svg">
          {yTicks && yTicks.map((t, i) => (
            <g key={i}>
              <line x1={P.l} y1={t.y} x2={width - P.r} y2={t.y} stroke="var(--grid)" strokeWidth="1" />
              <text x={P.l - 8} y={t.y} textAnchor="end" dominantBaseline="middle" className="cf-ytick">{t.label}</text>
            </g>
          ))}
          {zeroY != null && <line x1={P.l} y1={zeroY} x2={width - P.r} y2={zeroY} stroke="var(--axis)" strokeWidth="1" />}
          {renderPlot && renderPlot({ pad: P, innerW, innerH, width, height })}
          {xLabels && xLabels.length > 0 && (
            <g>
              <text x={P.l} y={height - 6} textAnchor="start" className="cf-xtick">{xLabels[0]}</text>
              {midXLabel !== null && <text x={(P.l + width - P.r) / 2} y={height - 6} textAnchor="middle" className="cf-xtick">{midXLabel}</text>}
              {xLabels.length > 1 && <text x={width - P.r} y={height - 6} textAnchor="end" className="cf-xtick">{xLabels[xLabels.length - 1]}</text>}
            </g>
          )}
        </svg>
      )}
      {note && <div className="cf-note">{note}</div>}
    </div>
  );
}

// ─────────── ChartNote -- the honesty rule's presence in chart space ───────────
// A chart is only ever drawn from an ok_rows outcome. Every other outcome (still loading,
// not_assessed, empty-in-window, empty-by-filters, a read error) renders this note in the same
// visual slot instead -- never a blank box, never an empty-looking chart that could pass for a
// verified zero. `label` is accepted (many callers still pass a query_id for their own reference)
// but never shown -- a query id is never on-screen text a reader has to make sense of.
export function ChartNote({ state }: { state: NoteState | null | undefined; label?: string }) {
  const outcome = state && state.outcome;
  const phase = state && state.phase;
  let text = "Loading...";
  let kind = "loading";
  if (state && state.phase === "error") { text = state.error || "Could not load"; kind = "error"; }
  else if (state && outcome === "not_assessed") {
    text = notAssessedText(state.data);
    kind = "not_assessed";
  }
  else if (outcome === "ok_empty_window") { text = "Nothing in this window (not a verified zero -- see the finding below)."; kind = "empty"; }
  else if (outcome === "ok_empty_filters") { text = "Rows exist, but the current filters exclude all of them."; kind = "empty"; }
  else if (state && outcome === "error") { text = (state.data && state.data.error) || "Could not read this table."; kind = "error"; }
  // A gap, not a chart: several stacked panels backed by the same not-assessed source used to each
  // draw a full dashed box, reading as a wall of identical empty tiles -- one plain line instead.
  if (kind === "not_assessed") return <div className="chart-note-oneline">{text}</div>;
  return (
    <div className={`chart-note ${kind}`}>
      <div className="chart-note-text">{text}</div>
    </div>
  );
}

// ─────────── Kpi tile (Overview KPI row) ───────────
export function Kpi({ label, value, unit, sub, tone = "default", facts }: {
  label: React.ReactNode; value: React.ReactNode; unit?: React.ReactNode; sub?: React.ReactNode; tone?: string; facts?: Fact[] | null;
}) {
  return (
    <div className="kpi-tile">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value tone-${tone}`}>
        {value}
        {unit && <span className="kpi-unit">{unit}</span>}
      </div>
      {facts ? <Facts items={facts} /> : (sub && <div className="kpi-sub">{sub}</div>)}
    </div>
  );
}

export function KpiRow({ children }: { children: React.ReactNode }) {
  return <div className="kpi-row">{children}</div>;
}

// ─────────── Sparkline ───────────
function Sparkline({ data, color = "var(--c1)", height = 38, fill = true }: { data: number[] | null; color?: string; height?: number; fill?: boolean }) {
  if (!data || data.length < 2) return null;
  const w = 100, h = height;
  const max = Math.max(...data);
  const min = Math.min(...data);
  const range = max - min || 1;
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => [i * step, h - 4 - ((v - min) / range) * (h - 8)]);
  const path = pts.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ");
  const area = path + ` L ${w} ${h} L 0 ${h} Z`;
  const last = pts[pts.length - 1];
  return (
    // preserveAspectRatio="none" is what lets a sparkline stretch to its container, but it scales
    // x and y by different factors -- a 100x26 viewBox in a 780x150 panel is 7.8x by 5.8x. That
    // distorts a 1.6px stroke into a tapering wedge and turns the end-marker circle into a blob
    // (observed on the Pareto line, 2026-09-23). vector-effect keeps the stroke at its true width
    // whatever the scale; the marker is drawn as a short non-scaling stroke rather than a circle,
    // because a circle's radius cannot survive a non-uniform scale.
    <svg
      className="spark"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      // styles.css sets .spark{height:auto}, which derives height from the viewBox ratio and
      // blew a height={26} sparkline up to ~200px in a wide panel. The prop is the contract.
      style={{ height: `${h}px` }}
    >
      {fill && <path className="area" d={area} fill={color} opacity="0.18" />}
      <path
        className="line"
        d={path}
        stroke={color}
        fill="none"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
      <path
        d={`M ${last[0].toFixed(2)} ${last[1].toFixed(2)} l 0 0`}
        stroke={color}
        strokeWidth="5"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

// ─────────── MultiLine -- one line per series over a shared x domain (the daily DBU trend) ───────────
export function MultiLine({ series, height = 160, width = 760, xLabels }: {
  series: { data: number[]; color?: string; label?: React.ReactNode }[] | null; height?: number; width?: number;
  xLabels?: React.ReactNode[] | null;
}) {
  const withData = (series || []).filter((s) => s.data && s.data.length > 0);
  if (!withData.length) return null;
  const n = Math.max(...withData.map((s) => s.data.length));
  const allVals = withData.flatMap((s) => s.data);
  const max = Math.max(...allVals, 0);
  const min = Math.min(...allVals, 0);
  const range = max - min || 1;
  // Left gutter wide enough for the max/min value labels -- a line with no y-axis numbers at all
  // reads as a shape with no scale.
  const yTick = (v: number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
  const padL = 34, padR = 4, padT = 8, padB = 18;
  const innerW = width - padL - padR, innerH = height - padT - padB;
  const step = n > 1 ? innerW / (n - 1) : 0;
  const yOf = (v: number) => padT + innerH - ((v - min) / range) * innerH;
  const xOf = (i: number) => padL + i * step;
  return (
    <div className="multiline-wrap">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ width: "100%", height, display: "block" }}>
        <line x1={padL} y1={yOf(0)} x2={width - padR} y2={yOf(0)} stroke="var(--border)" strokeWidth="1" />
        <text x={padL - 6} y={yOf(max) + 3} textAnchor="end" className="cf-ytick">{yTick(max)}</text>
        {min !== max && <text x={padL - 6} y={yOf(min) - 2} textAnchor="end" className="cf-ytick">{yTick(min)}</text>}
        {withData.map((s, si) => {
          const pts = s.data.map((v, i) => [xOf(i), yOf(v)]);
          const path = pts.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ");
          return <path key={si} d={path} stroke={s.color || paletteColor(si)} fill="none" strokeWidth="1.8" />;
        })}
      </svg>
      <div className="multiline-legend">
        {withData.map((s, si) => (
          <span key={si} className="ml-leg">
            <span className="sw" style={{ background: s.color || paletteColor(si) }}></span>
            {s.label}
          </span>
        ))}
      </div>
      {xLabels && (
        <div className="multiline-xaxis">
          <span>{xLabels[0]}</span>
          <span>{xLabels[xLabels.length - 1]}</span>
        </div>
      )}
    </div>
  );
}

// ─────────── ParetoLine -- "Top N of M <things> = X% of <total>" (PLAN.md 6.3) ───────────
function ParetoLine({ entries, unitLabel, threshold = 0.6 }: { entries: Entry[] | null; unitLabel: React.ReactNode; threshold?: number }) {
  if (!entries || !entries.length) return null;
  const sorted = [...entries].sort((a, b) => b.value - a.value);
  const total = sorted.reduce((s, e) => s + e.value, 0) || 1;
  let acc = 0, n = 0;
  const cum: number[] = [];
  for (const e of sorted) {
    acc += e.value;
    n += 1;
    cum.push((acc / total) * 100);
    if (acc / total >= threshold) break;
  }
  const pct = cum[cum.length - 1];
  const sparkSeries = cum.length > 1 ? [0, ...cum] : [0, cum[0], cum[0]];
  return (
    <div className="pareto-line">
      <div className="pareto-text">
        Top <span className="mono">{n}</span> of <span className="mono">{sorted.length}</span> {unitLabel}
        {" = "}<span className="mono">{pct.toFixed(1)}%</span> of net DBUs
      </div>
      <Sparkline data={sparkSeries} color="var(--c1)" height={26} fill={false} />
    </div>
  );
}

// ─────────── Donut ───────────
export function Donut({ segments: given, centerLabel, centerSub, size = 108, thickness = 14 }: {
  segments: Segment[] | null; centerLabel?: React.ReactNode; centerSub?: React.ReactNode; size?: number; thickness?: number;
}) {
  const segments = given || [];
  // Fewer than 2 non-zero slices isn't a breakdown -- a wheel that is all one colour, or empty,
  // reads as a chart with nothing to compare; one plain line says the same fact in fewer pixels.
  const nonZero = segments.filter((s) => s && s.value > 0);
  if (nonZero.length < 2) {
    if (!nonZero.length) return <EmptyChart text="Nothing to show." />;
    const only = nonZero[0];
    const display = only.display != null ? only.display : only.value.toFixed(1);
    return <div className="chart-note-oneline">{`All ${display} in ${only.label}`}</div>;
  }
  const r = size / 2 - thickness / 2 - 1;
  const cx = size / 2, cy = size / 2;
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  const circ = 2 * Math.PI * r;
  let acc = 0;
  return (
    <div className="donut-wrap">
      <svg className="donut-svg" viewBox={`0 0 ${size} ${size}`} width={size} height={size}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--panel-2)" strokeWidth={thickness} />
        {segments.map((seg, i) => {
          const pct = seg.value / total;
          const dash = pct * circ;
          const offset = -acc * circ;
          acc += pct;
          return (
            <circle
              key={i} cx={cx} cy={cy} r={r} fill="none"
              stroke={seg.color || paletteColor(i)} strokeWidth={thickness}
              strokeDasharray={`${dash} ${circ - dash}`} strokeDashoffset={offset}
              transform={`rotate(-90 ${cx} ${cy})`}
            />
          );
        })}
        {centerLabel && (
          <>
            <text className="donut-center" x={cx} y={centerSub ? cy - 6 : cy + 2} textAnchor="middle" dominantBaseline="middle">{centerLabel}</text>
            {centerSub && <text className="donut-center-sub" x={cx} y={cy + 14} textAnchor="middle">{centerSub}</text>}
          </>
        )}
      </svg>
      <div className="donut-legend">
        {segments.map((seg, i) => (
          <div className="row" key={i} style={{ gap: 6 }}>
            <span className="sw" style={{ background: seg.color || paletteColor(i) }}></span>
            <span className="lab">{seg.label}</span>
            <span className="v mono">{seg.display != null ? seg.display : seg.value.toFixed(1)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─────────── Treemap (squarified) ───────────
/** A treemap cell's share of the area and its box. */
interface Box {
  area: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

function squarifiedTreemap<T extends Entry>(items: T[], x0: number, y0: number, w: number, h: number): (T & Box)[] {
  const data = [...items].sort((a, b) => b.value - a.value).filter((d) => d.value > 0);
  if (!data.length) return [];
  const total = data.reduce((s, x) => s + x.value, 0);
  let remaining = data.map((d) => ({ ...d, area: (d.value / total) * w * h }));
  let X = x0, Y = y0, W = w, H = h;
  const out: (T & Box)[] = [];
  function worst(row: { area: number }[], w_: number) {
    const s = row.reduce((s, r) => s + r.area, 0);
    const max = Math.max(...row.map((r) => r.area));
    const min = Math.min(...row.map((r) => r.area));
    return Math.max((w_ * w_ * max) / (s * s), (s * s) / (w_ * w_ * min));
  }
  function layoutRow(row: (T & { area: number })[], w_: number, horizontal: boolean) {
    const s = row.reduce((s, r) => s + r.area, 0);
    const thick = s / w_;
    let pos = 0;
    for (const r of row) {
      const along = r.area / thick;
      if (horizontal) out.push({ ...r, x: X + pos, y: Y, w: along, h: thick });
      else out.push({ ...r, x: X, y: Y + pos, w: thick, h: along });
      pos += along;
    }
    if (horizontal) { Y += thick; H -= thick; } else { X += thick; W -= thick; }
  }
  while (remaining.length) {
    const horizontal = W >= H;
    const w_ = horizontal ? W : H;
    let row = [remaining[0]];
    let i = 1;
    while (i < remaining.length) {
      const tryRow = row.concat(remaining[i]);
      if (worst(tryRow, w_) <= worst(row, w_)) { row = tryRow; i++; } else break;
    }
    layoutRow(row, w_, horizontal);
    remaining = remaining.slice(row.length);
  }
  return out;
}

function Treemap({ items, width = 760, height = 260, getLabel }: {
  items: Entry[] | null; width?: number; height?: number; getLabel?: (c: Entry) => React.ReactNode;
}) {
  // Review round 1: ThemeToggle flips data-theme without a React re-render, so inkForFill below
  // (reads the fill's CSS variable AT RENDER TIME) kept the pre-toggle ink until something else
  // happened to re-render this tree. Subscribing here re-renders once right after a toggle.
  useThemeVersion();
  if (!items || !items.length) {
    return <div className="chart-note ok"><div className="chart-note-text">Nothing to show -- every value is zero or absent at this window/filters.</div></div>;
  }
  const withColor = items.map((it, i) => ({ ...it, color: it.color || paletteColor(i) }));
  const cells = squarifiedTreemap(withColor, 0, 0, width, height);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ width: "100%", height, display: "block" }}>
      {cells.map((c, i) => {
        const small = c.w < 110 || c.h < 44;
        const tiny = c.w < 55 || c.h < 28;
        // section 5: "Treemap label ink follows the fill's luminance" -- was a fixed near-black
        // that failed on the theme's own darker fills (--c3 leather, --seq-4/5, dark-mode fills).
        const ink = inkForFill(c.color);
        const inkFaint = ink === INK_DARK ? "rgba(23,21,17,.72)" : "rgba(251,250,247,.82)";
        return (
          <g key={i}>
            <rect x={c.x + 1} y={c.y + 1} width={Math.max(0, c.w - 2)} height={Math.max(0, c.h - 2)} fill={c.color} rx="4" ry="4" />
            {/* Review round 1: fontSize 10/11.5/9 were all under the spec's 12px floor -- "small"
                now hides the value line (drops information) rather than shrinking the text past
                that floor; the name line is always 12px. */}
            {!tiny && (
              <foreignObject x={c.x + 8} y={c.y + 6} width={Math.max(0, c.w - 16)} height={Math.max(0, c.h - 12)}>
                <div style={{
                  height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between",
                  color: ink, fontFamily: "var(--f-sans)", overflow: "hidden",
                }}>
                  <div style={{ fontSize: 12, fontWeight: 600, lineHeight: 1.18, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {c.name}
                  </div>
                  {!small && (
                    <div style={{ color: inkFaint, fontFamily: "var(--f-mono)", fontSize: 12 }}>
                      {getLabel ? getLabel(c) : c.value.toFixed(1)}
                    </div>
                  )}
                </div>
              </foreignObject>
            )}
            {tiny && c.w > 16 && c.h > 14 && (
              <text x={c.x + 5} y={c.y + 15} fill={inkFaint} fontSize="12" fontFamily="var(--f-mono)">{c.value.toFixed(0)}</text>
            )}
            <title>{c.name}: {getLabel ? getLabel(c) : c.value.toFixed(1)}</title>
          </g>
        );
      })}
    </svg>
  );
}

// ─────────── MiniBar (inline gauge / table-cell bar) ───────────
function MiniBar({ value, max = 100, color = "amber", suffix = "%", width = 70, decimals }: {
  value: number; max?: number; color?: string; suffix?: string; width?: number; decimals?: number;
}) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  const dec = decimals != null ? decimals : (value < 10 ? 1 : 0);
  return (
    <span className="mini-bar">
      <span className="track" style={{ width }}>
        <span className={`fill ${color}`} style={{ width: pct + "%" }}></span>
      </span>
      <span className="v mono">{Number.isFinite(value) ? value.toFixed(dec) : "-"}{suffix}</span>
    </span>
  );
}

// ─────────── HBar (stacked horizontal bar) ───────────
export function HBar({ segments, height = 28 }: { segments: Segment[]; height?: number }) {
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  return (
    <div className="hbar" style={{ height }}>
      {segments.map((s, i) => {
        const pct = (s.value / total) * 100;
        if (pct <= 0) return null;
        const fill = s.color || paletteColor(i);
        return (
          <div
            key={i} className="hbar-seg"
            // Review round 1: .hbar-seg{color:#16130c} was a fixed near-black, 3.0:1 on --c3 and
            // 4.2:1 on --c1 in light, 3.5:1 on --c2 in dark -- all under the 4.5:1 text floor.
            // inkForFill picks whichever of light/dark ink actually reads on THIS segment's fill.
            style={{ background: fill, color: inkForFill(fill), width: pct + "%" }}
            title={`${s.label}: ${s.display || s.value}`}
          >
            {pct > 9 ? s.label : ""}
          </div>
        );
      })}
    </div>
  );
}

// ─────────── HBarList -- a ranked list of horizontal bars (one row per item), for "top N" panels ───────────
export function HBarList({ items, max, valueFmt }: {
  items: Entry[] | null; max?: number | null; valueFmt?: (v: number, it: Entry) => React.ReactNode;
}) {
  if (!items || !items.length) {
    return <div className="chart-note ok"><div className="chart-note-text">Nothing to rank -- every value is zero or absent at this window/filters.</div></div>;
  }
  const m = max != null ? max : Math.max(...items.map((i) => i.value), 1);
  return (
    <div className="hbar-list">
      {items.map((it, i) => (
        <div className="hbar-list-row" key={i}>
          <div className="hbar-list-label" title={it.title || it.name}>{it.name}</div>
          <div className="hbar-list-track">
            <div className="hbar-list-fill" style={{ width: `${Math.min(100, (it.value / (m || 1)) * 100)}%`, background: it.color || "var(--c1)" }}></div>
          </div>
          <div className="hbar-list-value mono">{valueFmt ? valueFmt(it.value, it) : it.value}</div>
        </div>
      ))}
    </div>
  );
}

// ─────────── SubTabNav -- the second-level pill row inside Cost/Compute/Jobs (PLAN.md 6.3) ───────────
export function SubTabNav({ subtabs, active, onSelect }: {
  subtabs: { key: string; label: React.ReactNode }[]; active: string | null; onSelect: (key: string) => void;
}) {
  return (
    <div className="subtabs">
      {subtabs.map((s) => (
        <button key={s.key} className={active === s.key ? "active" : ""} onClick={() => onSelect(s.key)}>
          {s.label}
        </button>
      ))}
    </div>
  );
}

// ─────────── AccountWideNote -- PLAN.md 6.3's "account-wide badge on the SKU-level queries that
// carry no workspace_id" -- rendered here as one explanatory line per sub-tab rather than a
// per-row badge (this task's own simplification; the underlying fact -- which ids ignore the
// workspace filter -- is still surfaced, just once per panel instead of on every row). ───────────
function AccountWideNote({ ids }: { ids: string[] | null }) {
  if (!ids || !ids.length) return null;
  return (
    <div className="account-wide-note">
      Account-wide (no workspace_id, ignores the workspace filter): <span className="mono">{ids.join(", ")}</span>
    </div>
  );
}
