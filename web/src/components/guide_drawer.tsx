// The "How is this worked out?" drawer: opens over the dashboard instead of leaving the page.
// Kept out of guide.tsx on purpose -- guide.tsx transitively reaches primitives.tsx (AreaContent)
// through tab_waste, which would close an import cycle from findings_table.tsx/finding_detail.tsx
// (already upstream of primitives.tsx). This file sticks to the same light modules they use.
import React from "react";

import { Api } from "../api";
import { fmtInt } from "../format";
import { getCheckLabel } from "./labels";
import { AREA_REGISTRY, bandOf, homeForQuery } from "./tab_registry";
import { guideLinkProps } from "./nav_hash";
import type { Affected, FindingSummary } from "../types";
// Type-only -- erased at compile time, so importing guide.tsx's own type here closes no runtime
// cycle (see the file banner above, which is about a runtime import).
import type { GuideData } from "./guide";

// The one /api/guide fetch+cache (1.1 MB), shared with guide.tsx -- the check page would
// otherwise download the same payload a second time through a cache of its own.
let guideDataCache: Promise<GuideData> | null = null;
export function fetchGuideData(): Promise<GuideData> {
  if (!guideDataCache) {
    guideDataCache = Api.guide().catch((err) => {
      // A rejected promise would otherwise stay cached forever -- the next open should try again.
      guideDataCache = null;
      throw err;
    });
  }
  return guideDataCache;
}

// "(default)" after a threshold is noise here too -- a short mirror of guide.tsx's own
// cleanFlagPrefix, kept separate rather than imported (see the file banner above).
function cleanFlagPrefix(text: string): string {
  if (!text) return text;
  const s = String(text).replace(/^status\s*=\s*[A-Z_]+(?:\s+or\s+[A-Z_]+)?\s*-?\s*/i, "").replace(/\s*\(default\)/g, "");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const BAND_WORD: Record<string, string> = {
  CRITICAL: "Critical", WARN: "Warn", OK: "OK", RANKED: "Ranked", NOT_ASSESSED: "Not assessed", ERROR: "Error",
  EMPTY_WINDOW: "No data in this window", EMPTY_FILTERS: "Hidden by filters", INVENTORY: "Reference list",
};

const LONG_RULE = 130;

export type FlagClause = { level: "WARN" | "CRITICAL" | "BOTH"; body: string };

// Splits a rule text into its WARN / CRITICAL clauses; NOT_ASSESSED and OK only end the clause before them.
export function splitFlagClauses(text: string): FlagClause[] {
  const re = /(?:[Ss]tatus\s*=\s*)?\b(WARN|CRITICAL|NOT_ASSESSED|OK)\b(?:\s+or\s+(WARN|CRITICAL))?\s*[-–:]?/g;
  // Only a clause start counts: text start, "status =", or after ". " / "; " -- not "30 (WARN) or 60 (CRITICAL)".
  const marks = [...text.matchAll(re)].filter((m) => /^status/i.test(m[0]) || /(^|[.;]\s*)$/.test(text.slice(0, m.index!)));
  const out: FlagClause[] = [];
  marks.forEach((m, i) => {
    if (m[1] !== "WARN" && m[1] !== "CRITICAL") return;
    const end = i + 1 < marks.length ? marks[i + 1].index! : text.length;
    const body = text.slice(m.index! + m[0].length, end).replace(/^[\s\-–,:]+/, "").replace(/[\s;.]+$/, "");
    // "WARN (at/above 5) or CRITICAL (at/above 50)" names both levels in one clause.
    const both = m[2] || (m[1] === "WARN" ? /\bCRITICAL\b/ : /\bWARN\b/).test(body);
    if (body) out.push({ level: both ? "BOTH" : (m[1] as "WARN" | "CRITICAL"), body });
  });
  // No WARN/CRITICAL clause of its own: keep the whole rule as one row rather than drop it.
  if (!out.length && text.trim()) out.push({ level: "BOTH", body: text.trim() });
  return out;
}

const CLAUSE_LABEL = { WARN: "Warn", CRITICAL: "Critical", BOTH: "Warn or Critical" };

function flagRowsOf(f: GuideData["findings"][number], windowDays: number | null): { text: string; long?: boolean }[] {
  const rows: { text: string; long?: boolean }[] = [];
  const add = (label: string, body: string) => {
    const t = cleanFlagPrefix(body.replace(/^[\s\-–,:]+/, "").replace(/[\s;]+$/, ""));
    if (t) rows.push({ text: `${label}: ${t}`, long: t.length > LONG_RULE });
  };
  if (f.is_finding && f.investigate_if) splitFlagClauses(f.investigate_if).forEach((c) => add(CLAUSE_LABEL[c.level], c.body));
  if (f.is_finding && f.healthy) add("Reads OK", f.healthy);
  if (f.period_kind === "days" && windowDays) rows.push({ text: `Window: the last ${windowDays} days up to the export` });
  return rows;
}

type DrawerState = { queryId: string; summary: FindingSummary | null; opener: HTMLElement | null; windowDays: number | null } | null;
let drawerState: DrawerState = null;
const drawerListeners = new Set<() => void>();
function notifyDrawer() { drawerListeners.forEach((fn) => fn()); }

/** `summary` is the row the caller already has (for the "why this flags" line); omit it and that
 * line just doesn't show. `windowDays` is the window the reader is actually looking at -- the
 * "Window" bullet falls back to the app's own default only when the caller has none to give. */
export function openGuideDrawer(queryId: string, summary?: FindingSummary | null, windowDays?: number | null) {
  const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  drawerState = { queryId, summary: summary || null, opener, windowDays: windowDays || null };
  notifyDrawer();
}
export function closeGuideDrawer() {
  const opener = drawerState && drawerState.opener;
  drawerState = null;
  notifyDrawer();
  if (opener && document.contains(opener)) opener.focus();
}
function useGuideDrawerState(): DrawerState {
  const [state, setState] = React.useState(drawerState);
  React.useEffect(() => {
    const onChange = () => setState(drawerState);
    drawerListeners.add(onChange);
    return () => { drawerListeners.delete(onChange); };
  }, []);
  return state;
}

function drawerNavProps(focusId: string | null) {
  const g = guideLinkProps(focusId);
  return { href: g.href, onClick: (e: React.MouseEvent) => { g.onClick(e); closeGuideDrawer(); } };
}

// App.tsx mounts exactly one of these at the root; every panel's "How is this worked out?" opens
// it through openGuideDrawer rather than a prop threaded down through every tab.
export function GuideDrawer() {
  const state = useGuideDrawerState();
  const [data, setData] = React.useState<GuideData | null>(null);
  const [loadError, setLoadError] = React.useState(false);
  const closeRef = React.useRef<HTMLButtonElement | null>(null);
  const panelRef = React.useRef<HTMLElement | null>(null);
  const [openRows, setOpenRows] = React.useState<Set<number>>(new Set());
  const toggleRow = (i: number) => setOpenRows((prev) => { const n = new Set(prev); if (n.has(i)) n.delete(i); else n.add(i); return n; });
  React.useEffect(() => {
    if (!state) return;
    setLoadError(false);
    fetchGuideData().then(setData).catch(() => setLoadError(true));
    setOpenRows(new Set());
  }, [state && state.queryId]);
  React.useEffect(() => {
    if (!state) return undefined;
    if (closeRef.current) closeRef.current.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { closeGuideDrawer(); return; }
      // Tab/Shift+Tab wrap inside the panel -- a backdrop click already closes it, so focus has
      // no reason to ever reach the dashboard sitting behind it.
      if (e.key !== "Tab" || !panelRef.current) return;
      const focusable = panelRef.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled])");
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    // A tab/area change while the drawer is open (sidebar click, back button) leaves the panel it
    // was explaining -- close rather than float the drawer over an unrelated screen.
    const onHash = () => closeGuideDrawer();
    document.addEventListener("keydown", onKey);
    window.addEventListener("hashchange", onHash);
    return () => {
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("hashchange", onHash);
    };
  }, [state]);
  if (!state) return null;

  const f = data ? data.findings.find((x) => x.query_id === state.queryId) : null;
  const home = f ? homeForQuery(f) : { tab: "findings", subtab: null };
  const area = AREA_REGISTRY[home.tab] || null;
  const label = f ? getCheckLabel(f.query_id, f.title) : null;
  const flagRows = f ? flagRowsOf(f, state.windowDays || (data ? data.app.default_window : null)) : [];
  let whyLine: string | null = null;
  if (f && state.summary) {
    const band = bandOf(state.summary);
    const status = BAND_WORD[band] || null;
    const a: Partial<Affected> = state.summary.affected || {};
    const judged = band === "CRITICAL" || band === "WARN" || band === "OK";
    const count = judged && f.is_finding && a.total != null ? `${fmtInt(a.flagged || 0)} of ${fmtInt(a.total)} ${a.noun || "rows"} ${a.flagged === 1 ? "is" : "are"} over a threshold.` : null;
    whyLine = status ? (count ? `${status} — ${count}` : status) : count;
  }

  return (
    <div className="gdw-wrap">
      <div className="gdw-backdrop" onClick={() => closeGuideDrawer()} />
      <aside ref={panelRef} role="dialog" aria-label="How it's worked out" aria-modal="true" className="gdw-panel">
        <div className="gdw-head">
          <span className="gd-eyebrow">{area ? `Guide · ${area.label}` : "Guide"}</span>
          <button type="button" ref={closeRef} className="gdw-close" aria-label="Close" onClick={() => closeGuideDrawer()}>
            <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
          </button>
        </div>
        {!f ? (
          <div className="loading-note">
            {loadError
              ? <React.Fragment>Couldn't load this check. <a {...drawerNavProps(state.queryId)}>Open its page →</a></React.Fragment>
              : data
                ? <React.Fragment>Couldn't find this check. <a {...drawerNavProps(null)}>Open the Guide →</a></React.Fragment>
                : "Loading..."}
          </div>
        ) : (
          <React.Fragment>
            <h2 className="gdw-title">{label!.title}</h2>
            <p className="gdw-summary">{label!.why || f.summary}</p>
            {flagRows.length > 0 && (
              <ul className="gdw-list">
                {flagRows.map((r, i) => (
                  <li key={i}>
                    <span className={r.long && !openRows.has(i) ? "gdw-clamp" : undefined}>{r.text}</span>
                    {r.long && (
                      <button type="button" className="gdw-more" aria-expanded={openRows.has(i)} onClick={() => toggleRow(i)}>{openRows.has(i) ? "Less" : "More"}</button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {whyLine && (
              <div className="gdw-why">
                <span className="muted gd-small">Why this panel has its current status</span>
                <span>{whyLine}</span>
              </div>
            )}
            {(f.reads || []).length > 0 && (
              <div className="gdw-reads">
                <span className="muted gd-small">Reads</span>
                <span className="gd-tables">
                  {f.reads.map((t) => <code key={t} className="gd-code">{String(t).replace(/^system\./, "")}</code>)}
                </span>
              </div>
            )}
            <div className="gdw-actions">
              <a className="gd-open-btn" {...drawerNavProps(f.query_id)}>Full formula &amp; fix</a>
              <a className="gd-copy-btn" {...drawerNavProps("how-troubleshooting")}>Number looks wrong?</a>
            </div>
          </React.Fragment>
        )}
      </aside>
    </div>
  );
}
