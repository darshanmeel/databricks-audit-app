// the checks table every area page ends with (section
// 4.7): a Critical/Warn/OK/Not assessed/No data/Reference tab strip, each with its own count,
// Critical first; the active tab's rows as Status | Check and why | Affected | Possible waste |
// Other $ | actions. A worst-band pill per row (bandOf, primitives.tsx, including the "Ranked"
// band for a magnitude-only id), and `affected`/`money` read straight off the /api/findings row
// (contract B) -- never a per-row fetch. A row expands to the existing finding_detail, and a
// one-line "fix now" (the check's own first action) shows under every Critical row's title.
//
// tab_findings.tsx (the whole app's own "every check" list) shares this same tab vocabulary
// (TAB_ORDER/TAB_META/tabOf) and reuses AffectedCell/PossibleWasteCell/OtherMoneyCell/FixNowLine/
// CheckStatusTabs directly, so both files can never disagree
// about what "Critical" or "No data" means.

import React from "react";

import { Api } from "../api";
import { fmtInt, fmtMoney } from "../format";
import { HashState } from "./hash_state";
import { bandOf } from "./tab_registry";
import { BAND_LABEL, Badge, StatusPill, bandPillKind } from "./primitives";
import { getCheckLabel } from "./labels";
import { findingScopeLabel } from "./scope";
import { FindingDetail, actionLine } from "./finding_detail";
import { windowRangeLabel } from "./overview_tile";
import { navHref } from "./nav_hash";
import type { Affected, Filters, FindingSummary, Meta, Money } from "../types";
import type { Band } from "./tab_registry";
import type { CheckLabel } from "./labels";
import type { JumpTarget } from "./primitives";

/** The six status tabs of a checks list. */
export type TabKey = "CRITICAL" | "WARN" | "OK" | "COULDNT" | "NODATA" | "REFERENCE";

/** A check with its band and the tab it lands on. */
export interface CheckItem {
  row: FindingSummary;
  band: Band;
  tab: TabKey;
  searchText: string;
}

export const TAB_ORDER: TabKey[] = ["CRITICAL", "WARN", "OK", "COULDNT", "NODATA", "REFERENCE"];
export const TAB_META: Record<TabKey, { label: string; tone: string }> = {
  CRITICAL: { label: "Critical", tone: "coral" },
  WARN: { label: "Warn", tone: "amber" },
  OK: { label: "OK", tone: "sage" },
  COULDNT: { label: "Not assessed", tone: "lavender" },
  NODATA: { label: "No data", tone: "" },
  REFERENCE: { label: "Reference", tone: "" },
};
// The one-line reason shown next to a row's title whenever its band is not a plain CRITICAL/WARN/
// OK verdict -- RANKED rows land in the OK tab (nothing crossed a threshold) but still say so,
// since "OK" alone would otherwise read as a verified clean pass. EMPTY_WINDOW has no fixed text
// here -- reasonFor() below names the actual dates, since "nothing in this window" alone never
// says which window.
const GROUP_REASON: Record<string, string> = {
  RANKED: "ranked by size, not a threshold",
  NOT_ASSESSED: "not assessed",
  ERROR: "read error",
  EMPTY_FILTERS: "excluded by the current filters",
};

// reasonFor: `row.not_assessed.label` (contract B) names the real reason for a Not assessed row
// once the server carries one, else GROUP_REASON's generic text; EMPTY_WINDOW names the window's
// own dates ("No data for this in 15-24 Sep") rather than the generic "nothing in this window"; an
// OK row that still carries some NOT_ASSESSED rows says so, so "OK" never reads as every row
// having been judged.
export function reasonFor(row: FindingSummary, band: Band, rangeLabel: string | null): string | null {
  if (band === "EMPTY_WINDOW") return `No data for this in ${rangeLabel || "this window"}`;
  if (band === "NOT_ASSESSED") return (row.not_assessed && row.not_assessed.label) || GROUP_REASON.NOT_ASSESSED;
  if (band === "OK") {
    const n = row.status_counts && row.status_counts.NOT_ASSESSED;
    return n ? `partly assessed: ${fmtInt(n)} row${n === 1 ? "" : "s"} not assessed` : null;
  }
  return GROUP_REASON[band] || null;
}

// tabOf: a finding forced into "Reference" regardless of its own band (an inventory list, or an
// id the registry marks refIds for this sub-tab -- INVENTORY already means "no status column at
// all", which is exactly a reference table), else the six-tab bucket its band belongs to. RANKED
// (a magnitude-only ranking, e.g. cost_by_job) and a verified OK both read as the OK tab; a
// genuinely empty window and a real "hidden by your filters" both read as No data -- the two facts
// still differ per row via GROUP_REASON, but the app's own top-level list no longer needs eight
// buckets to say so.
export function tabOf(row: { query_id: string }, band: Band, refIdSet: Set<string>): TabKey {
  if (band === "INVENTORY") return "REFERENCE";
  // A reference list that flags rows counts like any check: its Critical pill is a Critical.
  if (band === "CRITICAL") return "CRITICAL";
  if (band === "WARN") return "WARN";
  // A reference list that didn't run sits with the other checks that didn't run.
  if (band === "NOT_ASSESSED" || band === "ERROR") return "COULDNT";
  if (refIdSet.has(row.query_id)) return "REFERENCE";
  if (band === "EMPTY_WINDOW" || band === "EMPTY_FILTERS") return "NODATA";
  return "OK"; // OK or RANKED
}

// The words statusText/CSV/search all share for a row's status -- BAND_LABEL for a band with no
// real per-row breakdown, or the CRITICAL/WARN/OK/NOT_ASSESSED counts a finding does carry.
export function statusText(row: Pick<FindingSummary, "status_counts">, band: string): string {
  const sc = row.status_counts;
  if (sc) {
    const parts = (["CRITICAL", "WARN", "NOT_ASSESSED", "OK"] as const)
      .filter((k) => (sc[k] || 0) > 0)
      .map((k) => `${sc[k]} ${BAND_LABEL[k]}`);
    if (parts.length) return parts.join(", ");
  }
  return BAND_LABEL[band] || band;
}

export function findingSearchText(row: FindingSummary, band: Band, label: CheckLabel): string {
  return [label.title, row.query_id, statusText(row, band)]
    .filter((v) => v !== null && v !== undefined)
    .join(" ")
    .toLowerCase();
}

// Severity, when the two $ figures tie (most rows carry no money at all) -- Critical first, worst
// to least certain last. Shared by findings_table.tsx and tab_findings.tsx so the two orderings
// never disagree given the same rows.
const SEVERITY_RANK: Record<string, number> = { CRITICAL: 0, WARN: 1, OK: 2, RANKED: 3, NOT_ASSESSED: 4, ERROR: 5, EMPTY_WINDOW: 6, EMPTY_FILTERS: 7, INVENTORY: 8 };

// The three kinds of $ never add together -- sort by possible waste first, then every other $
// figure (change/spend/saving/gap), then severity, then title for a stable final order.
export function sortByWasteThenMoney<T extends { row: FindingSummary; band: Band }>(rows: T[]): T[] {
  const wasteOf = (f: T) => (f.row.money && f.row.money.kind === "waste" ? f.row.money.usd : 0) || 0;
  const otherOf = (f: T) => (f.row.money && f.row.money.kind !== "waste" ? f.row.money.usd : 0) || 0;
  return [...rows].sort((a, b) => {
    const w = wasteOf(b) - wasteOf(a);
    if (w) return w;
    const o = otherOf(b) - otherOf(a);
    if (o) return o;
    const s = (SEVERITY_RANK[a.band] ?? 9) - (SEVERITY_RANK[b.band] ?? 9);
    if (s) return s;
    return getCheckLabel(a.row.query_id, a.row.title).title.localeCompare(getCheckLabel(b.row.query_id, b.row.title).title);
  });
}

export function csvField(value: unknown): string {
  if (value === null || value === undefined) return "";
  const s = String(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// CheckStatusTabs: one row of six buttons, Critical first, each with its own count -- the single
// selection replaces the old KPI tiles + status chip filters + four collapsed groups, since a tab
// IS the collapse: switching away hides everything not in it, with nothing left half-open.
export function CheckStatusTabs({ counts, active, onChange }: { counts: Record<string, number>; active: TabKey; onChange: (k: TabKey) => void }) {
  return (
    <div className="check-tabs" role="tablist">
      {TAB_ORDER.map((key) => {
        const tone = TAB_META[key].tone;
        const cls = ["check-tab", tone ? `tone-${tone}` : "", active === key ? "active" : "", !counts[key] ? "zero" : ""].filter(Boolean).join(" ");
        return (
          <button key={key} type="button" role="tab" aria-selected={active === key} className={cls} onClick={() => onChange(key)}>
            {TAB_META[key].label} <span className="check-tab-count">{fmtInt(counts[key] || 0)}</span>
          </button>
        );
      })}
    </div>
  );
}

// AffectedCell: "N of M <noun>" (or just "M <noun>" when nothing is flagged), read straight off
// the row's own `affected` (contract B) -- no per-row fetch, never a fabricated noun.
export function AffectedCell({ row }: { row: { affected: Affected | null } }) {
  const a = row.affected;
  if (!a) return <span className="muted">-</span>;
  // affected.noun is always plural -- stripping the trailing "s" reads right for a count of 1
  // without a per-noun singular/plural table.
  const noun = (n: number) => (n === 1 && a.noun.endsWith("s") ? a.noun.slice(0, -1) : a.noun);
  if (a.flagged >= a.total || a.flagged === 0) return <span className="ck-affected">{`${fmtInt(a.total)} ${noun(a.total)}`}</span>;
  return <span className="ck-affected">{`${fmtInt(a.flagged)} of ${fmtInt(a.total)} ${noun(a.total)}`}</span>;
}

// PossibleWasteCell / OtherMoneyCell: the row's own `money` (contract B) split into the app's two
// kinds of $ that never add together -- "waste" is possible waste, everything else (change/spend/
// saving/gap) is "Other $" with its own kind named alongside the figure.
export function PossibleWasteCell({ row, band }: { row: { money: Money | null }; band: Band }) {
  const m = row.money;
  if (!m || m.kind !== "waste") return <span className="muted">-</span>;
  return <span className={band === "CRITICAL" ? "ck-money crit" : "ck-money"}>{fmtMoney(m.usd, 0)}</span>;
}
export function OtherMoneyCell({ row }: { row: { money: Money | null } }) {
  const m = row.money;
  if (!m || m.kind === "waste") return <span className="muted">-</span>;
  return <span className="ck-money">{fmtMoney(m.usd, 0)}<span className="ck-money-kind">{m.kind}</span></span>;
}

// tag_picker.tsx's own banner promises a "tag n/a" mark on a check the active tag filter cannot
// reach -- row.tag_applied is null/undefined unless a tag filter is set (P4-T-IDX), so this only
// ever shows while one is active.
export function TagNotApplicableChip({ row }: { row: { tag_applied: boolean | null } }) {
  if (row.tag_applied !== false) return null;
  return <Badge kind="info">tag n/a</Badge>;
}

// A check's header text (finding_detail.tsx's actionLine) depends only on query_id/window/config
// thresholds -- never on which workspaces/envs are selected or which rows loaded (substitute_
// header_params reads config/thresholds.yml, not the filtered data) -- so it is cached by
// (query_id, window, tag) across every FixNowLine on the page. A Critical tab can hold dozens of
// rows at once, each otherwise firing its own GET /api/finding; this turns every repeat view
// (switching tabs back, a filter that does not change the window/tag) into zero extra requests.
const _fixNowCache = new Map<string, string | null>();

// FixNowLine: a Critical row's own first action, in plain words -- fetched at limit=1 since the
// header text this reads never depends on which rows load. Renders nothing while loading, on a
// fetch error, or when this check's own header carries no actions at all.
export function FixNowLine({ queryId, filters }: { queryId: string; filters: Filters }) {
  const cacheKey = `${queryId}|${filters.window}|${filters.envs.join(",")}|${Api.tagFilterKey()}`;
  const [line, setLine] = React.useState<string | null>(() => (_fixNowCache.has(cacheKey) ? _fixNowCache.get(cacheKey)! : null));
  React.useEffect(() => {
    if (_fixNowCache.has(cacheKey)) { setLine(_fixNowCache.get(cacheKey)!); return undefined; }
    let cancelled = false;
    Api.finding(queryId, filters.window, filters.workspaceIds, filters.envs, 1, 0)
      .then((d) => {
        const l = actionLine(d.header && d.header.actions, queryId);
        _fixNowCache.set(cacheKey, l);
        if (!cancelled) setLine(l);
      })
      .catch(() => { if (!cancelled) setLine(null); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey]);
  if (!line) return null;
  return <div className="ck-fixnow"><b>Fix now: </b>{line}</div>;
}

function CheckRow({ row, band, reason, expanded, onToggle, filters, onJumpTo, detailIndex, registerRef, meta }: {
  row: FindingSummary; band: Band; reason: string | null; expanded: boolean; onToggle: (id: string) => void; filters: Filters;
  onJumpTo: (id: string) => void; detailIndex: Record<string, FindingSummary>; registerRef: (el: HTMLTableRowElement | null) => void;
  meta: Meta | null;
}) {
  const label = getCheckLabel(row.query_id, row.title);
  const scopeNote = findingScopeLabel(row, meta);
  return (
    <React.Fragment>
      <tr className={`row-item ${expanded ? "expanded" : ""}`} onClick={() => onToggle(row.query_id)} ref={registerRef}>
        <td style={{ width: 120 }}><StatusPill kind={bandPillKind(band)} compact={false} /></td>
        <td>
          <div className="td-title" title={label.why || undefined}>
            {row.stars ? <span className="stars" title="A first-audit pick: one of the checks worth reading first">*</span> : null}{label.title}
            {reason && <span className="ck-reason">{reason}</span>}
            <TagNotApplicableChip row={row} />
          </div>
          {scopeNote && <div className="ck-why">{scopeNote}</div>}
        </td>
        <td><AffectedCell row={row} /></td>
        <td><PossibleWasteCell row={row} band={band} /></td>
        <td><OtherMoneyCell row={row} /></td>
        <td>
          <div className="ck-actions">
            <button type="button" className="ck-link" onClick={(e) => { e.stopPropagation(); onToggle(row.query_id); }}>
              {expanded ? "Hide ↓" : "Rows →"}
            </button>
            <a className="ck-link" title="How this check is worked out" href={navHref({ tab: "guide", subtab: null, focus: row.query_id })} onClick={(e) => e.stopPropagation()}>
              How →
            </a>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="detail-row">
          <td colSpan={6}>
            <FindingDetail
              queryId={row.query_id}
              windowDays={filters.window}
              workspaceIds={filters.workspaceIds}
              envs={filters.envs}
              onJumpTo={onJumpTo}
              findingsIndex={detailIndex}
            />
          </td>
        </tr>
      )}
    </React.Fragment>
  );
}

// `refIds` (optional, an array): ids this sub-tab always groups under the Reference tab
// regardless of their own band -- AreaPage (primitives.tsx) passes each sub-tab's own refIds.
export function FindingsTable({ findings, refIds, filters, initialFocusId, allFindingsIndex, onExternalJump, meta }: {
  findings: FindingSummary[]; refIds?: string[]; filters: Filters; initialFocusId?: string | null;
  allFindingsIndex?: Record<string, FindingSummary> | null; onExternalJump?: (target: JumpTarget) => void; meta: Meta | null;
}) {
  const refIdSet = React.useMemo(() => new Set<string>(refIds || []), [refIds]);
  const rangeLabel = windowRangeLabel(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);
  const [search, setSearch] = React.useState("");
  // Lazily picks the first tab (Critical first) that actually has a row, so a sub-tab whose
  // Critical/Warn are empty (the common case) never opens on a flash of "nothing here".
  const [tab, setTab] = React.useState<TabKey>(() => {
    const present = new Set(findings.map((f) => tabOf(f, bandOf(f), refIdSet)));
    return TAB_ORDER.find((k) => present.has(k)) || "CRITICAL";
  });
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const rowRefs = React.useRef<Record<string, HTMLTableRowElement | null>>({});

  React.useEffect(() => {
    try { HashState.set({ focus: expandedId || null }); } catch (e) { /* no hash on this page */ }
  }, [expandedId]);

  const findingsIndex = React.useMemo(() => {
    const idx: Record<string, FindingSummary> = {};
    findings.forEach((f) => { idx[f.query_id] = f; });
    return idx;
  }, [findings]);

  const withTab = React.useMemo(
    () => findings.map((f): CheckItem => {
      const band = bandOf(f);
      const label = getCheckLabel(f.query_id, f.title);
      return { row: f, band, tab: tabOf(f, band, refIdSet), searchText: findingSearchText(f, band, label) };
    }),
    [findings, refIdSet]
  );

  const counts = React.useMemo(() => {
    const c: Record<string, number> = {};
    TAB_ORDER.forEach((k) => { c[k] = 0; });
    withTab.forEach((f) => { c[f.tab] += 1; });
    return c;
  }, [withTab]);

  // The window/filters changed under an already-open tab that is now empty (e.g. Critical cleared
  // up) -- move to the next tab that actually has something, rather than sit on an empty one.
  React.useEffect(() => {
    if (counts[tab] === 0) {
      const firstNonEmpty = TAB_ORDER.find((k) => counts[k] > 0);
      if (firstNonEmpty && firstNonEmpty !== tab) setTab(firstNonEmpty);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findings]);

  const searched = React.useMemo(() => {
    if (!search.trim()) return withTab;
    const q = search.trim().toLowerCase();
    return withTab.filter((f) => f.searchText.includes(q));
  }, [withTab, search]);

  const visible = React.useMemo(() => sortByWasteThenMoney(searched.filter((f) => f.tab === tab)), [searched, tab]);

  const downloadCsv = () => {
    // Column order predates this table's redesign (DEC-74) and stays exactly as it was --
    // new columns, if any, are appended after these, never inserted before or renamed. Every
    // check matching the current search, across every tab -- not just the one on screen.
    const lines = [["Finding", "Query ID", "Domain", "Tier", "Status", "Rows"].map(csvField).join(",")];
    searched.forEach(({ row, band }) => {
      lines.push([
        csvField(getCheckLabel(row.query_id, row.title).title),
        csvField(row.query_id),
        csvField(row.domain),
        csvField(row.tier),
        csvField(statusText(row, band)),
        csvField(row.row_count === null || row.row_count === undefined ? "" : row.row_count),
      ].join(","));
    });
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "checks.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const detailIndex = allFindingsIndex || findingsIndex;

  const jumpTo = (queryId: string) => {
    const target = findingsIndex[queryId];
    if (!target) {
      if (onExternalJump) onExternalJump(queryId);
      return;
    }
    setTab(tabOf(target, bandOf(target), refIdSet));
    setExpandedId(queryId);
    setTimeout(() => {
      const el = rowRefs.current[queryId];
      if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 60);
  };

  React.useEffect(() => {
    if (initialFocusId && findingsIndex[initialFocusId]) jumpTo(initialFocusId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFocusId, findingsIndex]);

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <div className="card-title">Checks in this view</div>
          <div className="metric-note">Click a check for its rows and the fix. * marks a first-audit pick.</div>
        </div>
        <button type="button" className="csv-download-btn" onClick={downloadCsv} title="Download every check matching the current search, across all tabs, as a CSV file.">
          Download check list (CSV)
        </button>
      </div>
      <CheckStatusTabs counts={counts} active={tab} onChange={setTab} />
      <div className="findings-toolbar">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search check, id, status..."
          className="findings-search-input"
        />
      </div>
      <div className="table-wrap">
        <table className="findings">
          <colgroup>
            <col style={{ width: 120 }} />
            <col />
            <col style={{ width: 150 }} />
            <col style={{ width: 110 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 120 }} />
          </colgroup>
          <thead>
            <tr>
              <th>Status</th>
              <th>Check</th>
              <th>Affected</th>
              <th style={{ textAlign: "right" }}>Possible waste</th>
              <th style={{ textAlign: "right" }}>Other $</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {visible.map(({ row, band }) => (
              <CheckRow
                key={row.query_id}
                row={row}
                band={band}
                reason={reasonFor(row, band, rangeLabel)}
                expanded={expandedId === row.query_id}
                onToggle={(id) => setExpandedId(expandedId === id ? null : id)}
                filters={filters}
                onJumpTo={jumpTo}
                detailIndex={detailIndex}
                registerRef={(el) => { rowRefs.current[row.query_id] = el; }}
                meta={meta}
              />
            ))}
            {visible.length === 0 && (
              <tr>
                <td colSpan={6} className="empty-note">
                  {search.trim() ? "No checks match the current search." : `Nothing in "${TAB_META[tab].label}" for this view.`}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

