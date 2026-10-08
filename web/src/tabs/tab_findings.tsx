// "All findings" (was Domains): every check the app runs,
// one row per check, worst status. Registers into the "findings" area (no sub-tabs, no owned/ref
// ids of its own -- tab_registry.ts) via AreaContent.register, so it reads every check through
// allFindingsIndex rather than a narrowed `findings` prop. Shares its whole tab vocabulary
// (TAB_ORDER/TAB_META/tabOf, CheckStatusTabs, FixNowLine) with findings_table.tsx, loaded first.

import React from "react";

import { fmtInt, fmtMoney } from "../format";
import { AF_HOME_AREAS, AF_REFERENCE_IDS, AREA_REGISTRY, bandOf, homeForQuery } from "../components/tab_registry";
import { CONTENT_AREAS } from "../components/roles";
import { AreaContent, Card, StatusPill, bandPillKind } from "../components/primitives";
import { getCheckLabel } from "../components/labels";
import { AffectedCell, CheckStatusTabs, FixNowLine, OtherMoneyCell, PossibleWasteCell, TAB_META, TAB_ORDER, TagNotApplicableChip, csvField, findingSearchText, reasonFor, sortByWasteThenMoney, statusText, tabOf } from "../components/findings_table";
import { windowRangeLabel } from "../components/overview_tile";
import { navHref } from "../components/nav_hash";

// Every id any area's own sub-tab lists as a reference table, unioned across the whole app --
// findings_table.tsx's own tabOf() applies this same test per sub-tab; here it is asked once for
// every check at once, exactly so the Reference tab hides the same rows every other checks table
// already collapses.
const AF_PAGE_SIZE = 20;

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

function AfRow({ row, band, reason, goTo, filters }: LooseProps) {
  const label = getCheckLabel(row.query_id, row.title);
  const home = afHome(row);
  return (
    <tr
      className="row-item"
      style={home ? undefined : { cursor: "default" }}
      onClick={home ? () => goTo({ tab: home.tab, subtab: home.subtab, focusQueryId: row.query_id }) : undefined}
    >
      <td style={{ width: 110 }}><StatusPill kind={bandPillKind(band)} /></td>
      <td>
        <div className="td-title">
          {label.title}
          {reason && <span className="ck-reason">{reason}</span>}
          <TagNotApplicableChip row={row} />
        </div>
        {label.why && <div className="ck-why">{label.why}</div>}
        {band === "CRITICAL" && <FixNowLine queryId={row.query_id} filters={filters} />}
      </td>
      <td><AffectedCell row={row} /></td>
      <td><PossibleWasteCell row={row} band={band} /></td>
      <td><OtherMoneyCell row={row} /></td>
      <td style={{ textAlign: "right" }}><AfHomeLink row={row} goTo={goTo} /></td>
    </tr>
  );
}

// Same sub-tab (home area + home sub-tab), same status tab -- collapsed into one toggle row, so a
// cluster like the nine chargeback cuts landing in Critical together reads as one line, not nine.
// `rows` is [{row, band}], already the current status tab's own rows.
function groupBySubtab(rows: any) {
  const byKey = new Map();
  rows.forEach((entry: any) => {
    const home = homeForQuery(entry.row);
    const key = `${home.tab}::${home.subtab || ""}`;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(entry);
  });
  const out: any[] = [];
  byKey.forEach((members, key) => {
    if (members.length < 2) { out.push({ isGroup: false, entry: members[0] }); return; }
    const [tab, subtab] = key.split("::");
    const area = AREA_REGISTRY[tab];
    const sub = area && (area.subtabs || []).find((s) => s.key === subtab);
    const label = area ? (sub ? `${area.label} › ${sub.label}` : area.label) : "All findings";
    const sorted = sortByWasteThenMoney(members);
    out.push({ isGroup: true, key, label, members: sorted });
  });
  // Groups sort with the SAME waste-then-other-then-severity comparator members do within a
  // group, applied to each group's own top (already-sorted) member -- waste never ranks below a
  // bigger chargeback change $ just because the two $ kinds compare unlike.
  const repOf = (item: any) => (item.isGroup ? item.members[0] : item.entry);
  const repToItem = new Map(out.map((item) => [repOf(item), item]));
  return sortByWasteThenMoney([...repToItem.keys()]).map((rep) => repToItem.get(rep));
}

function AfGroupRow({ group, expanded, onToggle }: LooseProps) {
  const m = group.members[0] && group.members[0].row.money;
  const money = m && m.usd ? ` · ${fmtMoney(m.usd, 0)} ${m.kind === "waste" ? "possible waste" : m.kind}` : "";
  return (
    <tr className="row-item af-group-row" onClick={onToggle}>
      <td colSpan={6}>
        <button type="button" className="ck-link">
          {`${expanded ? "▾" : "▸"} ${group.label}: ${fmtInt(group.members.length)} checks${money}`}
        </button>
      </td>
    </tr>
  );
}

function afVerdict(counts: any, total: any, windowDays: any, lensOnly: any, goTo: any) {
  if (total === 0) return "No checks are in scope for the current filters.";
  const bits = [];
  if (counts.CRITICAL) bits.push(`${fmtInt(counts.CRITICAL)} critical`);
  if (counts.WARN) bits.push(`${fmtInt(counts.WARN)} warning${counts.WARN === 1 ? "" : "s"}`);
  const lead = bits.length ? `${bits.join(" and ")} to look at first` : "nothing critical or warning right now";
  const ran = total - (counts.COULDNT || 0);
  const head = `Of ${lensOnly ? "the " : ""}${fmtInt(total)} checks${lensOnly ? " in this lens" : ""}, ${fmtInt(ran)} ran on the last ${windowDays} days: ${lead}. `
    + `${fmtInt(counts.NODATA)} of those had no data in this window; ${fmtInt(counts.COULDNT)} could not run`;
  if (!counts.COULDNT || !goTo) return `${head}.`;
  return (
    <React.Fragment>
      {`${head} (see `}
      <a href={navHref({ tab: "coverage", subtab: "couldnt" })} onClick={(e) => { e.preventDefault(); goTo({ tab: "coverage", subtab: "couldnt" }); }}>Coverage &amp; Gaps › Not assessed</a>
      {")."}
    </React.Fragment>
  );
}

function AllFindingsPanel({ filters, allFindingsIndex, goTo, meta, roleAreas }: LooseProps) {
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

  const visible = React.useMemo(() => sortByWasteThenMoney(filtered.filter((f) => f.tab === tab)), [filtered, tab]);

  // Same sub-tab, same status tab -- collapsed into one toggle row (T5): a cluster like the nine
  // chargeback cuts landing in Critical together no longer repeats the same story nine times.
  const grouped = React.useMemo(() => groupBySubtab(visible), [visible]);
  const [expandedGroups, setExpandedGroups] = React.useState(() => new Set());
  const toggleGroup = (key: any) => setExpandedGroups((prev) => {
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

  React.useEffect(() => { setShowAllRows(false); setExpandedGroups(new Set()); }, [tab, areaFilter, q]);

  // Pagination counts a group as one row -- opening one never pulls in the next page's worth.
  const visibleGroups = showAllRows ? grouped : grouped.slice(0, AF_PAGE_SIZE);

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
      <div className="page-intro-verdict">{afVerdict(tabCounts, checkedRows.length, filters.window, CONTENT_AREAS.some((k) => !allowedAreas.has(k)), goTo)}</div>

      <CheckStatusTabs counts={tabCounts} active={tab} onChange={setTab} />

      <Card>
        <div className="findings-toolbar">
          <input
            className="findings-search-input"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search checks, e.g. idle, timeout, grants"
            aria-label="Search checks"
          />
          <button type="button" className="csv-download-btn" onClick={downloadCsv} title="Download every check matching the current area/search, across all tabs, as a CSV file.">
            Download check list (CSV)
          </button>
        </div>

        <div className="af-chip-row">
          <span className="af-chip-label">Area</span>
          <button type="button" className={`af-chip${!areaFilter ? " active" : ""}`} onClick={() => setAreaFilter(null)}>
            All <span className="af-chip-count">{fmtInt(checkedRows.length)}</span>
          </button>
          {AF_HOME_AREAS.filter((key) => allowedAreas.has(key)).map((key) => (
            <button
              key={key} type="button"
              className={`af-chip${areaFilter === key ? " active" : ""}`}
              onClick={() => setAreaFilter(areaFilter === key ? null : key)}
            >
              {AREA_REGISTRY[key].label} <span className="af-chip-count">{fmtInt(areaCounts[key] || 0)}</span>
            </button>
          ))}
        </div>

        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>Ordered by possible waste, then other $, then severity.</div>
        <div className="table-wrap" style={{ marginTop: 6 }}>
          <table className="findings">
            <colgroup>
              <col style={{ width: 100 }} /><col /><col style={{ width: 130 }} /><col style={{ width: 90 }} /><col style={{ width: 100 }} /><col style={{ width: 210 }} />
            </colgroup>
            <thead>
              <tr>
                <th>Status</th><th>Check and why it matters</th><th>Affected</th>
                <th style={{ textAlign: "right" }}>Possible waste</th><th style={{ textAlign: "right" }}>Other $</th>
                <th style={{ textAlign: "right" }}>Home tab</th>
              </tr>
            </thead>
            <tbody>
              {visibleGroups.map((g) => (g.isGroup ? (
                <React.Fragment key={g.key}>
                  <AfGroupRow group={g} expanded={expandedGroups.has(g.key)} onToggle={() => toggleGroup(g.key)} />
                  {expandedGroups.has(g.key) && g.members.map(({ row, band }: any) => (
                    <AfRow key={row.query_id} row={row} band={band} reason={reasonFor(row, band, rangeLabel)} goTo={goTo} filters={filters} />
                  ))}
                </React.Fragment>
              ) : (
                <AfRow key={g.entry.row.query_id} row={g.entry.row} band={g.entry.band} reason={reasonFor(g.entry.row, g.entry.band, rangeLabel)} goTo={goTo} filters={filters} />
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
      </Card>
    </div>
  );
}

AreaContent.register("findings", null, AllFindingsPanel);
