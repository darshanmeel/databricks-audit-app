// "All findings" (was Domains): every check the app runs,
// one row per check, worst status, grouped by area (or ungrouped, "Group by: None"). Registers
// into the "findings" area (no sub-tabs, no owned/ref ids of its own -- tab_registry.ts) via
// AreaContent.register, so it reads every check through allFindingsIndex rather than a narrowed
// `findings` prop. A row opens in place (FindingDetail, same as every other checks table) instead
// of navigating away; its own home-tab link still jumps there. Shares its tab vocabulary
// (TAB_ORDER/TAB_META/tabOf, CheckStatusTabs) with findings_table.tsx, loaded first.

import React from "react";

import { fmtInt, fmtMoney } from "../format";
import { AF_HOME_AREAS, AF_REFERENCE_IDS, AREA_REGISTRY, bandOf, homeForQuery } from "../components/tab_registry";
import { AreaContent, BAND_LABEL, Card, StatusPill, bandPillKind } from "../components/primitives";
import { getCheckLabel } from "../components/labels";
import { AffectedCell, CheckStatusTabs, OtherMoneyCell, PossibleWasteCell, SEVERITY_RANK, TAB_META, TAB_ORDER, TagNotApplicableChip, csvField, findingSearchText, otherOf, reasonFor, sortByWasteThenMoney, statusText, tabOf, wasteOf } from "../components/findings_table";
import { FindingDetail } from "../components/finding_detail";
import { windowRangeLabel } from "../components/overview_tile";
import { navHref } from "../components/nav_hash";

// Every id any area's own sub-tab lists as a reference table, unioned across the whole app --
// findings_table.tsx's own tabOf() applies this same test per sub-tab; here it is asked once for
// every check at once, exactly so the Reference tab hides the same rows every other checks table
// already collapses.
const AF_PAGE_SIZE = 20;

// Below ~720px a 6-column table squeezes unreadable -- one card per check instead.
function useNarrow(breakpointPx: number): boolean {
  const query = `(max-width: ${breakpointPx}px)`;
  const [narrow, setNarrow] = React.useState(() => {
    try { return !!(window.matchMedia && window.matchMedia(query).matches); } catch (e) { return false; }
  });
  React.useEffect(() => {
    if (!window.matchMedia) return undefined;
    const mq = window.matchMedia(query);
    const onChange = () => setNarrow(mq.matches);
    if (mq.addEventListener) mq.addEventListener("change", onChange); else mq.addListener(onChange);
    return () => { if (mq.removeEventListener) mq.removeEventListener("change", onChange); else mq.removeListener(onChange); };
  }, [query]);
  return narrow;
}

function afHome(row: any) {
  const home = homeForQuery(row);
  const area = AREA_REGISTRY[home.tab];
  if (!area || home.tab === "findings") return null;
  const sub = (area.subtabs || []).find((s) => s.key === home.subtab);
  return { tab: home.tab, subtab: home.subtab, text: sub ? `${area.label} › ${sub.label}` : area.label };
}

function AfHomeLink({ row, goTo }: LooseProps) {
  const home = afHome(row);
  if (!home) return <span className="muted">This page</span>;
  const href = navHref({ tab: home.tab, subtab: home.subtab, focus: row.query_id });
  return (
    <a
      className="af-home-link"
      href={href}
      title={home.text}
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); goTo({ tab: home.tab, subtab: home.subtab, focusQueryId: row.query_id }); }}
    >
      {`${home.text} →`}
    </a>
  );
}

// A row opens in place (its own FindingDetail -- rows, the fix, "How is this worked out") rather
// than navigating away; AfHomeLink (its own stopPropagation) still jumps to the check's home tab.
function AfRow({ row, band, reason, goTo, filters, expanded, onToggle, onJumpTo, detailIndex, registerRef }: LooseProps) {
  const label = getCheckLabel(row.query_id, row.title);
  return (
    <React.Fragment>
      <tr className={`row-item${expanded ? " expanded" : ""}`} onClick={() => onToggle(row.query_id)} ref={registerRef}>
        <td style={{ width: 110 }}><StatusPill kind={bandPillKind(band)} /></td>
        <td>
          <div className="td-title">
            {label.title}
            {reason && <span className="ck-reason">{reason}</span>}
            <TagNotApplicableChip row={row} />
          </div>
          {label.why && <div className="ck-why">{label.why}</div>}
        </td>
        <td><AffectedCell row={row} /></td>
        <td><PossibleWasteCell row={row} band={band} /></td>
        <td><OtherMoneyCell row={row} /></td>
        <td style={{ textAlign: "right" }}>
          <div className="af-row-actions">
            <AfHomeLink row={row} goTo={goTo} />
            <button type="button" className="ck-link af-expand-btn" aria-expanded={expanded} aria-label={expanded ? `Hide ${label.title}` : `Open ${label.title}`} onClick={(e) => { e.stopPropagation(); onToggle(row.query_id); }}>
              {expanded ? "Hide ↓" : "Open →"}
            </button>
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

// Mobile card (<=720px): same status/title/why/affected/waste an AfRow shows, as one
// tappable card instead of six cramped columns.
function AfCard({ row, band, reason, goTo, filters, expanded, onToggle, onJumpTo, detailIndex, registerRef }: LooseProps) {
  const label = getCheckLabel(row.query_id, row.title);
  const isWaste = !!(row.money && row.money.kind === "waste");
  return (
    <div className={`af-card${expanded ? " expanded" : ""}`} ref={registerRef}>
      <button type="button" className="af-card-head" aria-expanded={expanded} onClick={() => onToggle(row.query_id)}>
        <span className="af-card-head-top">
          <StatusPill kind={bandPillKind(band)} />
          {isWaste ? <PossibleWasteCell row={row} band={band} /> : <OtherMoneyCell row={row} />}
        </span>
        <span className="af-card-title">
          {label.title}
          <TagNotApplicableChip row={row} />
        </span>
        {label.why && <span className="af-card-why">{label.why}</span>}
        <span className="af-card-sub">
          <AffectedCell row={row} />
          {reason && <span className="ck-reason">{reason}</span>}
        </span>
      </button>
      {expanded && (
        <div className="af-card-detail">
          <AfHomeLink row={row} goTo={goTo} />
          <FindingDetail
            queryId={row.query_id}
            windowDays={filters.window}
            workspaceIds={filters.workspaceIds}
            envs={filters.envs}
            onJumpTo={onJumpTo}
            findingsIndex={detailIndex}
          />
        </div>
      )}
    </div>
  );
}

// Same sub-tab (home area + home sub-tab), same status tab -- every row gets a group header,
// including a group of one, so a single check never shows up right under another group's header
// with nothing of its own to say it isn't a member. A group holds only the rows that share its
// key -- never another sub-tab's -- and sorts by its OWN total $, not its biggest member's, so a
// $5,118 group of four small checks outranks one $3,200 check the way its own total says it should.
function groupBySubtab(rows: any, byStatus = false) {
  const byKey = new Map();
  rows.forEach((entry: any) => {
    const home = homeForQuery(entry.row);
    const key = byStatus ? `status::${entry.band}` : `${home.tab}::${home.subtab || ""}`;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(entry);
  });
  const out: any[] = [];
  byKey.forEach((members, key) => {
    const [tab, subtab] = key.split("::");
    const area = AREA_REGISTRY[tab];
    const sub = area && (area.subtabs || []).find((s) => s.key === subtab);
    const label = byStatus ? (BAND_LABEL[subtab] || subtab) : area ? (sub ? `${area.label} › ${sub.label}` : area.label) : "All findings";
    const sorted = sortByWasteThenMoney(members);
    const otherKind = members.map((m: any) => m.row.money && m.row.money.kind).find((k: any) => k && k !== "waste");
    // The worst band any member carries -- not necessarily the top member's own, since that one
    // is picked by $ (e.g. a big-$ OK row can sort first in a group that also holds a $0 Critical).
    const worstBand = members.reduce((w: string, m: any) => ((SEVERITY_RANK[m.band] ?? 9) < (SEVERITY_RANK[w] ?? 9) ? m.band : w), sorted[0].band);
    out.push({
      isGroup: true, key, label, members: sorted, otherKind,
      waste: members.reduce((sum: number, m: any) => sum + wasteOf(m), 0),
      other: members.reduce((sum: number, m: any) => sum + otherOf(m), 0),
      band: worstBand,
      title: label,
    });
  });
  // Status groups run worst first; area groups sort by total possible waste, then worst status.
  return out.sort((a, b) => {
    if (byStatus) return (SEVERITY_RANK[a.band] ?? 9) - (SEVERITY_RANK[b.band] ?? 9);
    const w = b.waste - a.waste;
    if (w) return w;
    const s = (SEVERITY_RANK[a.band] ?? 9) - (SEVERITY_RANK[b.band] ?? 9);
    if (s) return s;
    return a.title.localeCompare(b.title);
  });
}

// "Group by: None" -- every row on its own, sorted by $ like the grouped view, just never
// clustered under an area header.
function ungrouped(rows: any) {
  return sortByWasteThenMoney(rows).map((e: any) => ({ isGroup: false, entry: e, waste: wasteOf(e), other: otherOf(e), band: e.band, title: getCheckLabel(e.row.query_id, e.row.title).title }));
}

function groupMoneyText(group: any) {
  if (group.waste > 0) return ` · ${fmtMoney(group.waste, 0)} possible waste`;
  if (group.other > 0) return ` · ${fmtMoney(group.other, 0)} ${group.otherKind || ""}`;
  return "";
}

function groupCountText(group: any) {
  return group.members.length === 1 ? "1 check" : `${fmtInt(group.members.length)} checks`;
}

function AfGroupRow({ group, expanded, onToggle }: LooseProps) {
  // The group's own total, not its biggest member's -- matches the sort order above.
  return (
    <tr className="row-item af-group-row" onClick={onToggle}>
      <td colSpan={6}>
        <button type="button" className="ck-link" aria-expanded={expanded}>
          {`${expanded ? "▾" : "▸"} ${group.label}: ${groupCountText(group)}${groupMoneyText(group)}`}
        </button>
      </td>
    </tr>
  );
}

function AfCardGroupHeader({ group, expanded, onToggle }: LooseProps) {
  return (
    <button type="button" className="af-card-group" aria-expanded={expanded} onClick={onToggle}>
      <span>{`${expanded ? "▾" : "▸"} ${group.label}`}</span>
      <span className="af-card-group-meta">{`${groupCountText(group)}${groupMoneyText(group)}`}</span>
    </button>
  );
}

// "{c} critical, {w} warnings · {n} couldn't run" -- a zero part is left out, not spelled out.
function afVerdict(counts: any, total: any, goTo: any) {
  if (total === 0) return "No checks are in scope for the current filters.";
  const bits = [];
  if (counts.CRITICAL) bits.push(`${fmtInt(counts.CRITICAL)} critical`);
  if (counts.WARN) bits.push(`${fmtInt(counts.WARN)} warning${counts.WARN === 1 ? "" : "s"}`);
  const lead = bits.length ? bits.join(", ") : "nothing critical or warning";
  if (!counts.COULDNT) return lead;
  if (!goTo) return `${lead} · ${fmtInt(counts.COULDNT)} couldn't run`;
  return (
    <React.Fragment>
      {`${lead} · `}
      <a href={navHref({ tab: "coverage", subtab: "couldnt" })} onClick={(e) => { e.preventDefault(); goTo({ tab: "coverage", subtab: "couldnt" }); }}>{`${fmtInt(counts.COULDNT)} couldn't run`}</a>
    </React.Fragment>
  );
}

function AllFindingsPanel({ filters, allFindingsIndex, goTo, meta, roleAreas, onExternalJump }: LooseProps) {
  // roleAreas (contract H): every area for data_engineer/cto, a real subset for the other roles --
  // All findings only ever lists a check whose home area the current role's own sidebar shows.
  const allowedAreas = React.useMemo(
    () => new Set(roleAreas && roleAreas.length ? roleAreas : AF_HOME_AREAS),
    [roleAreas]
  );
  const all = React.useMemo(
    () => Object.values<any>(allFindingsIndex || {}).filter((row) => allowedAreas.has(homeForQuery(row).tab)),
    [allFindingsIndex, allowedAreas]
  );
  const rangeLabel = windowRangeLabel(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);

  const [search, setSearch] = React.useState("");
  const [areaFilter, setAreaFilter] = React.useState<any>(null); // null = All
  // Lazily picks the first tab (Critical first) that actually has a row -- same rule
  // findings_table.tsx uses, so All findings never opens on an empty Critical tab either.
  const [tab, setTab] = React.useState(() => {
    const present = new Set<string>(all.map((row) => tabOf(row, bandOf(row), AF_REFERENCE_IDS)));
    return TAB_ORDER.find((k) => present.has(k)) || "CRITICAL";
  });
  const [showAllRows, setShowAllRows] = React.useState(false);
  const [groupMode, setGroupMode] = React.useState<"area" | "status" | "none">("area");
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const rowRefs = React.useRef<Record<string, HTMLElement | null>>({});
  const narrow = useNarrow(720);
  const [chipsOpen, setChipsOpen] = React.useState(false);

  const withMeta = React.useMemo(() => all.map((row) => {
    const band = bandOf(row);
    const tabKey = tabOf(row, band, AF_REFERENCE_IDS);
    const label = getCheckLabel(row.query_id, row.title);
    return { row, band, tab: tabKey, homeArea: homeForQuery(row).tab, searchText: findingSearchText(row, band, label) };
  }), [all]);

  // "Checks ran" excludes Reference (raw export lists, never a pass/fail check) -- matches every
  // other count on this page (the verdict sentence and the Area chips), only the tab strip itself
  // also carries the Reference count so that tab can say how many rows it holds.
  const checkedRows = React.useMemo(() => withMeta.filter((f) => f.tab !== "REFERENCE"), [withMeta]);

  // Both rows below are GLOBAL counts (every check in scope, ignoring the current area/search
  // filters) -- so a chip's own number never shifts as the reader tries a different chip; only
  // the table underneath responds to the combination of filters.
  const areaCounts = React.useMemo(() => {
    const counts: Record<string, any> = {};
    checkedRows.forEach((f) => { counts[f.homeArea] = (counts[f.homeArea] || 0) + 1; });
    return counts;
  }, [checkedRows]);

  const tabCounts = React.useMemo(() => {
    const counts: Record<string, any> = {};
    TAB_ORDER.forEach((k) => { counts[k] = 0; });
    withMeta.forEach((f) => { counts[f.tab] += 1; });
    return counts;
  }, [withMeta]);

  const q = search.trim().toLowerCase();
  const filtered = React.useMemo(() => withMeta.filter((f) => {
    if (areaFilter && f.homeArea !== areaFilter) return false;
    if (q && !f.searchText.includes(q)) return false;
    return true;
  }), [withMeta, areaFilter, q]);

  const visible = React.useMemo(() => sortByWasteThenMoney(groupMode === "status" ? filtered : filtered.filter((f) => f.tab === tab)), [filtered, tab, groupMode]);

  // Same sub-tab, same status tab -- collapsed into one toggle row (T5): a cluster like the nine
  // chargeback cuts landing in Critical together no longer repeats the same story nine times.
  // "Group by: None" skips the clustering and just lists every row by $.
  const grouped = React.useMemo(() => (groupMode === "none" ? ungrouped(visible) : groupBySubtab(visible, groupMode === "status")), [visible, groupMode]);
  // Groups start open (every group has something worth seeing); a key here means the reader
  // closed that one group themselves.
  const [collapsedGroups, setCollapsedGroups] = React.useState(() => new Set());
  const toggleGroup = (key: any) => setCollapsedGroups((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  // The window/filters changed under an already-open tab that is now empty -- move to the next
  // tab that actually has something, rather than sit on an empty one.
  React.useEffect(() => {
    if (tabCounts[tab] === 0) {
      const firstNonEmpty = TAB_ORDER.find((k) => tabCounts[k] > 0);
      if (firstNonEmpty && firstNonEmpty !== tab) setTab(firstNonEmpty);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all]);

  // Reset paging/collapse from the control that changed the view, not from an effect -- an effect
  // keyed on these same values would also fire after jumpTo sets them, undoing the jump.
  const resetView = () => { setShowAllRows(false); setCollapsedGroups(new Set()); };
  const changeTab = (k: any) => { setTab(k); resetView(); };
  const changeAreaFilter = (key: any) => { setAreaFilter((prev: any) => (prev === key ? null : key)); resetView(); };
  const changeSearch = (value: string) => { setSearch(value); resetView(); };
  const changeGroupMode = (mode: "area" | "status" | "none") => { setGroupMode(mode); resetView(); };

  // Pagination counts a group as one row -- opening one never pulls in the next page's worth.
  const visibleGroups = showAllRows ? grouped : grouped.slice(0, AF_PAGE_SIZE);

  // A related check a row links to (FindingDetail's own onJumpTo): open it in place when this
  // role's own All findings already lists it, else hand off to its home tab (onExternalJump).
  const jumpTo = (queryId: any) => {
    const target = withMeta.find((f) => f.row.query_id === queryId);
    if (!target) { if (onExternalJump) onExternalJump(queryId); return; }
    setSearch("");
    setAreaFilter(null);
    setTab(target.tab);
    setShowAllRows(true);
    if (groupMode !== "none") {
      const home = homeForQuery(target.row);
      const key = groupMode === "status" ? `status::${target.band}` : `${home.tab}::${home.subtab || ""}`;
      setCollapsedGroups((prev) => {
        if (!prev.has(key)) return prev;
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
    setExpandedId(queryId);
    setTimeout(() => {
      const el = rowRefs.current[queryId];
      if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 60);
  };
  const toggleRow = (id: any) => setExpandedId((prev) => (prev === id ? null : id));

  const downloadCsv = () => {
    const lines = [["Status", "Check", "Query ID", "Home tab"].map(csvField).join(",")];
    filtered.forEach(({ row, band }) => {
      const home = afHome(row);
      lines.push([
        csvField(statusText(row, band)),
        csvField(getCheckLabel(row.query_id, row.title).title),
        csvField(row.query_id),
        csvField(home ? home.text : ""),
      ].join(","));
    });
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "all_findings.csv";
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <div className="page-intro-verdict">{afVerdict(tabCounts, checkedRows.length, goTo)}</div>

      {groupMode !== "status" && <CheckStatusTabs counts={tabCounts} active={tab} onChange={changeTab} />}
      {/* The six tabs above include Reference; the verdict sentence above them does not, so its
      own total always reads lower than the six counts summed. */}
      {tabCounts.REFERENCE > 0 && (
        <div className="muted" style={{ fontSize: 12, margin: "4px 0 10px" }}>
          {`Reference: ${fmtInt(tabCounts.REFERENCE)} lists, not checks -- not counted in the ${fmtInt(checkedRows.length)} above.`}
        </div>
      )}

      <Card>
        <div className="findings-toolbar">
          <input
            className="findings-search-input"
            value={search}
            onChange={(e) => changeSearch(e.target.value)}
            placeholder="Search checks, e.g. idle, timeout, grants"
            aria-label="Search checks"
          />
          <label className="af-group-by">
            Group by
            <select value={groupMode} onChange={(e) => changeGroupMode(e.target.value as "area" | "status" | "none")} aria-label="Group checks by">
              <option value="area">Area</option>
              <option value="status">Status</option>
              <option value="none">None — by $</option>
            </select>
          </label>
          <button type="button" className="csv-download-btn" onClick={downloadCsv} title="Download matching checks as CSV">
            Download CSV
          </button>
        </div>

        {narrow && (
          <button type="button" className="af-filter-btn" aria-expanded={chipsOpen} onClick={() => setChipsOpen((o) => !o)}>
            {`Filter · Area${areaFilter ? `: ${AREA_REGISTRY[areaFilter].label}` : ""}`}
            <span aria-hidden="true">{chipsOpen ? "▴" : "▾"}</span>
          </button>
        )}
        {(!narrow || chipsOpen) && (
        <div className="af-chip-row">
          <span className="af-chip-label">Area</span>
          <button type="button" className={`af-chip${!areaFilter ? " active" : ""}`} onClick={() => changeAreaFilter(null)}>
            All <span className="af-chip-count">{fmtInt(checkedRows.length)}</span>
          </button>
          {AF_HOME_AREAS.filter((key) => allowedAreas.has(key) && ((areaCounts[key] || 0) > 0 || areaFilter === key)).map((key) => (
            <button
              key={key} type="button"
              className={`af-chip${areaFilter === key ? " active" : ""}`}
              onClick={() => changeAreaFilter(key)}
            >
              {AREA_REGISTRY[key].label} <span className="af-chip-count">{fmtInt(areaCounts[key] || 0)}</span>
            </button>
          ))}
        </div>
        )}

        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>{groupMode === "status" ? "Groups ordered worst status first." : groupMode === "none" ? "Ordered by possible waste, then other $." : "Groups ordered by possible waste, then worst status."}</div>

        {narrow ? (
          <div className="af-card-list" style={{ marginTop: 6 }}>
            {visibleGroups.map((g) => (g.isGroup ? (
              <React.Fragment key={g.key}>
                <AfCardGroupHeader group={g} expanded={!collapsedGroups.has(g.key)} onToggle={() => toggleGroup(g.key)} />
                {!collapsedGroups.has(g.key) && g.members.map(({ row, band }: any) => (
                  <AfCard
                    key={row.query_id} row={row} band={band} reason={reasonFor(row, band, rangeLabel)} goTo={goTo} filters={filters}
                    expanded={expandedId === row.query_id} onToggle={toggleRow} onJumpTo={jumpTo} detailIndex={allFindingsIndex}
                    registerRef={(el: HTMLElement | null) => { rowRefs.current[row.query_id] = el; }}
                  />
                ))}
              </React.Fragment>
            ) : (
              <AfCard
                key={g.entry.row.query_id} row={g.entry.row} band={g.entry.band} reason={reasonFor(g.entry.row, g.entry.band, rangeLabel)} goTo={goTo} filters={filters}
                expanded={expandedId === g.entry.row.query_id} onToggle={toggleRow} onJumpTo={jumpTo} detailIndex={allFindingsIndex}
                registerRef={(el: HTMLElement | null) => { rowRefs.current[g.entry.row.query_id] = el; }}
              />
            )))}
            {!showAllRows && grouped.length > AF_PAGE_SIZE && (
              <button type="button" className="load-more" onClick={() => setShowAllRows(true)}>
                {`Show ${fmtInt(grouped.length - AF_PAGE_SIZE)} more`}
              </button>
            )}
            {visible.length === 0 && (
              <div className="empty-note">
                {q || areaFilter ? "No checks match the current search and filters." : `Nothing in "${TAB_META[tab].label}" right now.`}
              </div>
            )}
          </div>
        ) : (
        <div className="table-wrap" style={{ marginTop: 6 }}>
          <table className="findings">
            <colgroup>
              <col style={{ width: 100 }} /><col /><col style={{ width: 130 }} /><col style={{ width: 90 }} /><col style={{ width: 100 }} /><col style={{ width: 190 }} />
            </colgroup>
            <thead>
              <tr>
                <th>Status</th><th>Check and why it matters</th><th>Affected</th>
                <th style={{ textAlign: "right" }}>Possible waste</th><th style={{ textAlign: "right" }}>Other $</th>
                <th style={{ textAlign: "right" }}><span className="visually-hidden">Open</span></th>
              </tr>
            </thead>
            <tbody>
              {visibleGroups.map((g) => (g.isGroup ? (
                <React.Fragment key={g.key}>
                  <AfGroupRow group={g} expanded={!collapsedGroups.has(g.key)} onToggle={() => toggleGroup(g.key)} />
                  {!collapsedGroups.has(g.key) && g.members.map(({ row, band }: any) => (
                    <AfRow
                      key={row.query_id} row={row} band={band} reason={reasonFor(row, band, rangeLabel)} goTo={goTo} filters={filters}
                      expanded={expandedId === row.query_id} onToggle={toggleRow} onJumpTo={jumpTo} detailIndex={allFindingsIndex}
                      registerRef={(el: HTMLTableRowElement | null) => { rowRefs.current[row.query_id] = el; }}
                    />
                  ))}
                </React.Fragment>
              ) : (
                <AfRow
                  key={g.entry.row.query_id} row={g.entry.row} band={g.entry.band} reason={reasonFor(g.entry.row, g.entry.band, rangeLabel)} goTo={goTo} filters={filters}
                  expanded={expandedId === g.entry.row.query_id} onToggle={toggleRow} onJumpTo={jumpTo} detailIndex={allFindingsIndex}
                  registerRef={(el: HTMLTableRowElement | null) => { rowRefs.current[g.entry.row.query_id] = el; }}
                />
              )))}
              {!showAllRows && grouped.length > AF_PAGE_SIZE && (
                <tr>
                  <td colSpan={6} style={{ textAlign: "center", padding: "10px 0" }}>
                    <button type="button" className="load-more" onClick={() => setShowAllRows(true)}>
                      {`Show ${fmtInt(grouped.length - AF_PAGE_SIZE)} more`}
                    </button>
                  </td>
                </tr>
              )}
              {visible.length === 0 && (
                <tr><td colSpan={6} className="empty-note">
                  {q || areaFilter ? "No checks match the current search and filters." : `Nothing in "${TAB_META[tab].label}" right now.`}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        )}
      </Card>
    </div>
  );
}

AreaContent.register("findings", null, AllFindingsPanel);
