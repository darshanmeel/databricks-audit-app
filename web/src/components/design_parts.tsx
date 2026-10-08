// Page parts shared by several pages: heat grid, state lines, share bar, coverage strip and the
// spend path. Plain HTML and CSS (css/c_parts.css); the data comes in already aggregated.

import React from "react";

import { fmtInt, fmtMoney, fmtPct } from "../format";
import { bandOf } from "./tab_registry";
import { navHref } from "./nav_hash";
import type { FindingSummary } from "../types";

type Cell = number | null | undefined;

// Four cut points at the 20th-80th percentile of the positive values, for a measure with no
// natural scale (GB, minutes); null when there are too few distinct values to split.
export function quantileEdges(values: (number | null | undefined)[]): number[] | null {
  const pos = values.filter((v): v is number => v != null && Number.isFinite(v) && v > 0).sort((a, b) => a - b);
  if (pos.length < 2) return pos.length ? [pos[0], pos[0] * 2, pos[0] * 3, pos[0] * 4] : null;
  const at = (q: number) => pos[Math.min(pos.length - 1, Math.floor(q * pos.length))];
  const edges = [at(0.2), at(0.4), at(0.6), at(0.8)];
  for (let i = 1; i < 4; i += 1) if (edges[i] <= edges[i - 1]) edges[i] = edges[i - 1] * 1.0001 + 1e-9;
  return edges;
}

/** A heat-grid row: a label and, for the tooltip, its full name. */
export interface HeatRow { key: string; label: React.ReactNode; title?: string; right?: React.ReactNode }
export interface HeatCol { key: string; label: string; partial?: boolean }

// Level 0 is a real zero, 1-5 climb through `edges` (4 ascending cut points); null is no data.
function levelOf(v: Cell, edges: number[]): number | null {
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  if (!(v > 0)) return 0;
  let lvl = 1;
  edges.forEach((e) => { if (v > e) lvl += 1; });
  return Math.min(5, lvl);
}

// `emptyLabel` names a cell with nothing in it (e.g. "no queries"): drawn blank, not hatched,
// since nothing ran there; hatching is for data that is missing.
export function HeatGrid({ rows, cols, cell, edges, fmt, rightHead, labelEvery, onRow, activeRow, compact, ariaLabel, emptyLabel }: {
  rows: HeatRow[]; cols: HeatCol[]; cell: (ri: number, ci: number) => Cell; edges: number[];
  fmt: (v: number) => string; rightHead?: string; labelEvery?: number; onRow?: (key: string) => void;
  activeRow?: string | null; compact?: boolean; ariaLabel?: string; emptyLabel?: string;
}) {
  if (!rows.length || !cols.length) return null;
  const every = labelEvery || Math.max(1, Math.ceil(cols.length / 8));
  const legend = [`none`, `< ${fmt(edges[0])}`, ...edges.slice(0, 3).map((e, i) => `${fmt(e)}–${fmt(edges[i + 1])}`), `> ${fmt(edges[3])}`];
  const style = { gridTemplateColumns: `minmax(110px, 220px) repeat(${cols.length}, minmax(5px, 1fr)) minmax(48px, auto)` };
  return (
    <div className={`hg${compact ? " compact" : ""}`} role="table" aria-label={ariaLabel}>
      <div className="hg-legend">
        {legend.map((l, i) => <span key={i}><i className={`hg-c l${i}`} />{l}</span>)}
        <span><i className={`hg-c ${emptyLabel ? "blank" : "nodata"}`} />{emptyLabel || "no data"}</span>
      </div>
      <div className="hg-scroll">
        <div className="hg-grid" style={style}>
          <div className="hg-head" />
          {cols.map((c, ci) => (
            <div key={c.key} className="hg-head hg-col">{ci % every === 0 || ci === cols.length - 1 ? c.label : ""}</div>
          ))}
          <div className="hg-head hg-right">{rightHead || ""}</div>
          {rows.map((r, ri) => (
            <React.Fragment key={r.key}>
              {onRow
                ? <button type="button" className={`hg-label link${activeRow === r.key ? " active" : ""}`} title={r.title} onClick={() => onRow(r.key)}>{r.label}</button>
                : <div className="hg-label" title={r.title}>{r.label}</div>}
              {cols.map((c, ci) => {
                const v = cell(ri, ci);
                const lvl = levelOf(v, edges);
                const cls = `hg-c ${lvl === null ? (emptyLabel ? "blank" : "nodata") : `l${lvl}`}${c.partial ? " partial" : ""}`;
                const tip = `${r.title || ""}${r.title ? " · " : ""}${c.label}${c.partial ? " (partial)" : ""}: ${lvl === null ? (emptyLabel || "no data") : fmt(v as number)}`;
                return <div key={c.key} className={cls} title={tip} />;
              })}
              <div className="hg-right mono">{r.right}</div>
            </React.Fragment>
          ))}
        </div>
      </div>
    </div>
  );
}

/** A day on a state line. "none" is a day with no run, which is normal for a weekly job. */
export type DayState = "ok" | "failed" | "slow" | "skipped" | "none";
export const STATE_WORD: Record<DayState, string> = { ok: "ran fine", failed: "failed", slow: "ran slow", skipped: "skipped", none: "no run" };

export interface StateRow { key: string; label: React.ReactNode; title?: string; states: DayState[]; cells: React.ReactNode[] }

export function StateBands({ days, rows, heads, dayFmt, onRow, more }: {
  days: string[]; rows: StateRow[]; heads: string[]; dayFmt: (d: string) => string;
  onRow?: (key: string) => void; more?: React.ReactNode;
}) {
  if (!rows.length) return null;
  const order: DayState[] = ["ok", "failed", "slow", "skipped", "none"];
  return (
    <div className="sb">
      <div className="sb-legend">
        {order.map((s) => <span key={s}><i className={`sb-c ${s}`} />{STATE_WORD[s]}</span>)}
      </div>
      <div className="table-wrap sb-wrap">
        <table className="sb-table">
          <thead>
            <tr>
              <th>Name</th>
              <th className="sb-band-head">{`${dayFmt(days[0])} → ${dayFmt(days[days.length - 1])}`}</th>
              {heads.map((h) => <th key={h} className="num">{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <td className="sb-name">
                  {onRow ? <button type="button" className="link-btn" title={r.title} onClick={() => onRow(r.key)}>{r.label}</button> : <span title={r.title}>{r.label}</span>}
                </td>
                <td className="sb-band-cell">
                  <div className="sb-band" style={{ gridTemplateColumns: `repeat(${days.length}, minmax(0, 1fr))` }}>
                    {r.states.map((s, i) => <span key={i} className={`sb-c ${s}`} title={`${dayFmt(days[i])}: ${STATE_WORD[s]}`} />)}
                  </div>
                </td>
                {r.cells.map((c, i) => <td key={i} className="num mono">{c}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {more && <div className="sb-more">{more}</div>}
    </div>
  );
}

/** One part of a whole, for ShareBar. */
export interface SharePart { label: string; value: number; title?: string }

// One kind of value split into its parts; past `n` parts fold into "Other (k)".
export function ShareBar({ parts, fmt, noun, n = 4, right }: {
  parts: SharePart[]; fmt: (v: number) => string; noun: string; n?: number; right?: React.ReactNode;
}) {
  const list = parts.filter((p) => p.value > 0).sort((a, b) => b.value - a.value);
  const total = list.reduce((s, p) => s + p.value, 0);
  if (!total) return null;
  const head = list.slice(0, n);
  const tail = list.slice(n);
  const shown = tail.length ? [...head, { label: `Other (${tail.length})`, value: tail.reduce((s, p) => s + p.value, 0), other: true }] : head;
  const pct = (v: number) => (v / total) * 100;
  const topShare = pct(head[0].value);
  return (
    <div className="shb">
      <div className="shb-top">
        <span>{`Top 1 of ${fmtInt(list.length)} ${noun} = `}<b className="mono">{fmtPct(topShare, 0)}</b></span>
        {right && <span className="mono">{right}</span>}
      </div>
      <div className="shb-bar" role="img" aria-label={shown.map((p) => `${p.label} ${fmtPct(pct(p.value), 0)}`).join(", ")}>
        {shown.map((p, i) => (
          <span key={p.label} className={`shb-seg s${(p as { other?: boolean }).other ? "o" : i}`} style={{ flexBasis: `${pct(p.value)}%` }} title={`${p.label}: ${fmt(p.value)} (${fmtPct(pct(p.value), 1)})`}>
            {pct(p.value) >= 8 ? fmtPct(pct(p.value), 0) : ""}
          </span>
        ))}
      </div>
      <div className="shb-legend">
        {shown.map((p, i) => (
          <span key={p.label}><i className={`shb-seg s${(p as { other?: boolean }).other ? "o" : i}`} />{`${p.label} · ${fmt(p.value)}`}</span>
        ))}
      </div>
    </div>
  );
}

// Shown only when one of the page's checks could not be judged: how many, and why.
export function CoverageStrip({ findings }: { findings: FindingSummary[] }) {
  if (!findings || !findings.length) return null;
  let crit = 0, warn = 0, notRun = 0;
  const reasons: string[] = [];
  findings.forEach((f) => {
    const band = bandOf(f);
    if (band === "CRITICAL") crit += 1;
    else if (band === "WARN") warn += 1;
    else if (band === "NOT_ASSESSED" || band === "ERROR") {
      notRun += 1;
      const why = f.not_assessed && f.not_assessed.label;
      if (why && !reasons.includes(why)) reasons.push(why);
    }
  });
  if (!notRun) return null;
  const parts = [`${fmtInt(findings.length)} check${findings.length === 1 ? "" : "s"} on this page`];
  if (crit) parts.push(`${fmtInt(crit)} critical`);
  if (warn) parts.push(`${fmtInt(warn)} warning`);
  return (
    <div className="cov-strip">
      <span className="mono">{parts.join(" · ")}</span>
      <span className="cov-gap">{`– ${fmtInt(notRun)} not assessed${reasons.length ? `: ${reasons.slice(0, 2).join("; ")}` : ""}`}</span>
      <a href={navHref({ tab: "coverage", subtab: "couldnt", focus: null })}>Why, and what to grant →</a>
    </div>
  );
}

// List price, then after the discount from settings, then the part a cost-allocation tag reaches.
// Not "ties to the bill": system tables carry no invoice, so the discount is the one in settings.
export function SpendPath({ listUsd, discountPct, tagged, tagLabel, onTags }: {
  listUsd: number | null; discountPct: number; tagged: { netUsd: number; share: number } | null; tagLabel: string; onTags?: () => void;
}) {
  if (listUsd == null) return null;
  const net = listUsd * (1 - discountPct);
  return (
    <div className="sp">
      <div className="sp-box">
        <div className="sp-label">At list price</div>
        <div className="sp-value mono">{fmtMoney(listUsd, 0)}</div>
        <div className="sp-note">usage × Databricks list price</div>
      </div>
      <div className="sp-arrow mono">{discountPct > 0 ? `→ −${fmtPct(discountPct * 100, 1)}` : "→"}</div>
      <div className="sp-box">
        <div className="sp-label">After your discount</div>
        <div className="sp-value mono">{fmtMoney(net, 0)}</div>
        <div className="sp-note">{discountPct > 0 ? "discount from settings, not your invoice" : "no discount set in settings"}</div>
      </div>
      <div className="sp-arrow mono">→</div>
      <div className="sp-box">
        <div className="sp-label">{`With a ${tagLabel.toLowerCase()} tag`}</div>
        <div className="sp-value mono">{tagged ? fmtMoney(tagged.netUsd, 0) : "–"}</div>
        <div className={`sp-note${tagged && tagged.share < 0.5 ? " warn" : ""}`}>
          {tagged ? `${fmtPct(tagged.share * 100, 0)} of spend · ${fmtPct((1 - tagged.share) * 100, 0)} has none` : "no tagged spend in this export"}
          {onTags && <> · <button type="button" className="link-btn" onClick={onTags}>Allocation →</button></>}
        </div>
      </div>
    </div>
  );
}
