// Storage area: tables / maintenance sub-tabs (redesign
// section 6). access_dead_table_candidates moves in here from Governance -- it is table hygiene,
// not access. Whole metastore, no workspace filter (AREA_REGISTRY's own scope chip covers that
// once, in PageIntro).

import React from "react";

import { fmtBytes, fmtChange, fmtDbu, fmtGb, fmtInt, fmtMoney, fmtPct } from "../format";
import { resolveName } from "../components/names";
import { countBy, groupSum, numOrZero, rowsOf, sumBy, topNWithOther, useFindingData } from "../components/hooks";
import { AreaContent, Card, PqgsNote, StatusPill, bandPillKind } from "../components/primitives";
import { enumLabel } from "../components/labels";
import { ChartNote, Donut, HBarList, paletteColor } from "../components/charts";

// ─────────── Tables ───────────
// ─────────── Tables nobody read (storage_unused_table_cost) ───────────
const UNREAD_SHOWN = 20;
const UNREAD_MAX = 500;

// Tables with no read in the window, split by whether anything still writes to them: written but
// never read costs writes and storage for nothing, so it comes first.
function UnreadTablesCard({ filters, meta }: LooseProps) {
  const state = useFindingData("storage_unused_table_cost", filters.window, filters.workspaceIds, filters.envs);
  const [which, setWhich] = React.useState<"written" | "idle">("written");
  const [showAll, setShowAll] = React.useState(false);
  const days = Math.min(filters.window, numOrZero(meta && meta.snapshot_days) || filters.window);
  const rows = rowsOf(state);
  const real = rows ? rows.filter((r) => !r.is_other) : null;
  const written = real ? real.filter((r) => r.days_since_last_write != null && numOrZero(r.days_since_last_write) <= days) : null;
  const idle = real ? real.filter((r) => !(r.days_since_last_write != null && numOrZero(r.days_since_last_write) <= days)) : null;
  const list = (which === "written" ? written : idle) || [];
  // Costliest first; with no size on record, the longest without a write first.
  const age = (r: any) => (r.days_since_last_write == null ? Infinity : numOrZero(r.days_since_last_write));
  const sorted = [...list].sort((a, b) => numOrZero(b.est_total_usd_month) - numOrZero(a.est_total_usd_month)
    || numOrZero(b.active_gb) - numOrZero(a.active_gb) || age(b) - age(a));
  const shown = sorted.slice(0, showAll ? UNREAD_MAX : UNREAD_SHOWN);
  const usd = (xs: any[] | null) => (xs ? xs.reduce((a, r) => a + numOrZero(r.est_total_usd_month), 0) : 0);
  const priced = (xs: any[] | null) => (xs ? xs.filter((r) => r.est_total_usd_month != null).length : 0);
  // A table with no size on record has no $ figure: unknown, not $0.
  const usdText = (xs: any[] | null) => (!xs || !xs.length ? "" : !priced(xs) ? "No size on record for these, so no $ figure."
    : `Storage and upkeep: ${fmtMoney(usd(xs), 0)} a month${priced(xs) < xs.length ? ` for the ${fmtInt(priced(xs))} with a size on record` : ""}.`);
  const lastWrite = (r: any) => {
    const d = r.days_since_last_write;
    return d == null ? "none on record" : numOrZero(d) === 0 ? "today" : `${fmtInt(d)} day${numOrZero(d) === 1 ? "" : "s"} ago`;
  };
  return (
    <Card title={`Tables nobody read in the last ${fmtInt(days)} days`} right={real && priced(real) ? <span className="muted">{`${fmtMoney(usd(real), 0)} a month storage and upkeep`}</span> : null}>
      {real ? (
        <React.Fragment>
          <div className="ws-actions">
            <button type="button" className={which === "written" ? "on" : ""} onClick={() => { setWhich("written"); setShowAll(false); }}>{`Written, never read · ${fmtInt(written!.length)}`}</button>
            <button type="button" className={which === "idle" ? "on" : ""} onClick={() => { setWhich("idle"); setShowAll(false); }}>{`Not read or written · ${fmtInt(idle!.length)}`}</button>
          </div>
          <div className="metric-note">
            {which === "written"
              ? `Something still writes to these, but nothing read them: you pay for the writes and the storage. The strongest candidates to stop. ${usdText(written)}`
              : `No read and no write in the window. Check the owner, then drop or archive. ${usdText(idle)}`}
          </div>
          {sorted.length ? (
            <div className="data-table-wrap">
              <table className="data">
                <thead><tr><th>Table</th><th>Type</th><th>Last write</th><th className="num">Size</th><th className="num">$ a month</th><th>Owner</th></tr></thead>
                <tbody>
                  {shown.map((r, i) => (
                    <tr key={`${r.table_catalog}.${r.table_schema}.${r.table_name}.${i}`}>
                      <td className="mono">{`${r.table_catalog}.${r.table_schema}.${r.table_name}`}</td>
                      <td className="muted">{r.table_type ? String(r.table_type).charAt(0) + String(r.table_type).slice(1).toLowerCase() : ""}</td>
                      <td>{lastWrite(r)}</td>
                      <td className="num mono">{r.active_gb != null ? fmtGb(r.active_gb) : "-"}</td>
                      <td className="num mono">{r.est_total_usd_month != null ? fmtMoney(r.est_total_usd_month, r.est_total_usd_month < 10 ? 2 : 0) : "-"}</td>
                      <td className="muted">{r.table_owner || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <div className="muted">{which === "written" ? "None: every table written in the window was also read." : "None."}</div>}
          {sorted.length > UNREAD_SHOWN && (
            <button type="button" className="ov-link-btn" onClick={() => setShowAll(!showAll)}>
              {showAll ? `Show the ${UNREAD_SHOWN} costliest` : sorted.length > UNREAD_MAX ? `Show the ${UNREAD_MAX} costliest of ${fmtInt(sorted.length)}` : `Show all ${fmtInt(sorted.length)}`}
            </button>
          )}
          <div className="metric-note">Reads come from Unity Catalog lineage, which misses reads by storage path, from clusters without Unity Catalog, through Delta Sharing and by tools that read the files directly. Check notebooks and PySpark on all-purpose clusters by hand, and ask the owner, before dropping anything. Views are not listed; a read through a view counts for its tables.</div>
        </React.Fragment>
      ) : <ChartNote state={state} label="storage_unused_table_cost" />}
    </Card>
  );
}

function StorageTablesContent({ filters, maxCat, onVerdict, meta }: LooseProps) {
  const discState = useFindingData("storage_target_table_discovery", filters.window, filters.workspaceIds, filters.envs);
  const invState = useFindingData("table_inventory_type", filters.window, filters.workspaceIds, filters.envs);
  // Only the flagged tables and the five columns the cards read: the full list runs to megabytes.
  const deadState = useFindingData("access_dead_table_candidates", filters.window, filters.workspaceIds, filters.envs, 5000,
    ["CRITICAL", "WARN"], ["table_catalog", "table_schema", "table_name", "days_since_altered"]);
  const growthState = useFindingData("storage_growth", filters.window, filters.workspaceIds, filters.envs);

  const disc = rowsOf(discState);
  const inv = rowsOf(invState);
  // Asked for flagged rows only, so "none match" means none flagged.
  const dead = deadState.phase === "ready" && deadState.outcome === "ok_empty_filters" ? [] : rowsOf(deadState);
  const growth = rowsOf(growthState);

  const discByType = disc ? countBy(disc, (r) => (r.table_type ? enumLabel(r.table_type) : "(unknown)")) : null;
  const discTop = discByType && discByType.length ? [...discByType].sort((a, b) => b.value - a.value)[0] : null;

  const invTotal = inv ? sumBy(inv, "table_count") : null;
  const invByType = inv ? groupSum(inv, (r) => (r.table_type ? enumLabel(r.table_type) : "(unknown)"), "table_count") : null;

  const deadFlagged = dead ? dead.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const deadWorst = deadFlagged && deadFlagged.length
    ? [...deadFlagged].sort((a, b) => numOrZero(b.days_since_altered) - numOrZero(a.days_since_altered))[0] : null;
  const deadItems = deadFlagged ? [...deadFlagged]
    .sort((a, b) => numOrZero(b.days_since_altered) - numOrZero(a.days_since_altered))
    .slice(0, maxCat)
    .map((r) => ({ name: `${r.table_catalog}.${r.table_schema}.${r.table_name}`, value: numOrZero(r.days_since_altered) })) : null;

  const growthFlagged = growth ? growth.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const growthDropped = growth ? growth.filter((r) => r.dropped_in_window) : null;
  // A dropped table is flagged (WARN) but never "growing" -- the flagged count on its own conflates
  // the two, so the two are split out for display (must-fix #14).
  const growthFlaggedGrowing = growthFlagged ? growthFlagged.filter((r) => !r.dropped_in_window) : null;
  const growthNoOwnerRows = growth ? growth.filter((r) => r.no_owner) : null;
  const growthNoOwner = growthNoOwnerRows ? growthNoOwnerRows.length : null;
  const growthGrowers = growthFlagged ? [...growthFlagged]
    .filter((r) => !r.dropped_in_window && r.growth_pct != null)
    .sort((a, b) => numOrZero(b.growth_pct) - numOrZero(a.growth_pct))
    .slice(0, maxCat)
    .map((r) => ({ name: r.full_name, value: numOrZero(r.growth_pct), bytes: numOrZero(r.growth_bytes), lastBytes: numOrZero(r.last_bytes), firstBytes: numOrZero(r.first_bytes) })) : null;
  const growthWorst = growthGrowers && growthGrowers.length ? growthGrowers[0] : null;

  const verdictSentence = disc
    ? `${fmtInt(disc.length)} tables discovered${dead ? `; ${fmtInt(deadFlagged!.length)} not altered in a long time` : ""}.`
    : "Loading tables...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Tables discovered">
          {disc ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(disc.length)}</div>
              <PqgsNote facts={[
                { label: "Scope", value: "Tables only", detail: "not views" },
                { label: "Top type", value: discTop ? discTop.name : null, detail: discTop ? `${fmtInt(discTop.value)} of ${fmtInt(disc.length)}` : null },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={discState} label="storage_target_table_discovery" />}
        </Card>
        <Card title="Tables and views by type">
          {inv ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(invTotal)}</div>
              <PqgsNote facts={[{ label: "Includes", value: "Tables + views" }]} />
            </React.Fragment>
          ) : <ChartNote state={invState} label="table_inventory_type" />}
        </Card>
        <Card title="Not altered recently">
          {dead ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${deadFlagged!.length ? "warn" : "ok"}`}>{fmtInt(deadFlagged!.length)}</div>
              <PqgsNote facts={[
                {
                  label: "Worst",
                  value: deadWorst ? `${deadWorst.table_catalog}.${deadWorst.table_schema}.${deadWorst.table_name}` : "None flagged",
                  tone: deadWorst ? undefined : "ok",
                  detail: deadWorst ? `${fmtInt(deadWorst.days_since_altered)} days since altered` : null,
                },
                { label: "Rule", value: "not read in the window", detail: "and no change for 90+ days" },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={deadState} label="access_dead_table_candidates" />}
        </Card>
        <Card title="Fastest-growing tables">
          {growth ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${growthFlagged!.length ? "warn" : "ok"}`}>
                {growthDropped!.length ? `${fmtInt(growthFlaggedGrowing!.length)} growing, ${fmtInt(growthDropped!.length)} dropped` : fmtInt(growthFlaggedGrowing!.length)}
              </div>
              <PqgsNote facts={[
                {
                  label: "Worst",
                  value: growthWorst ? growthWorst.name : "None flagged",
                  tone: growthWorst ? undefined : "ok",
                  detail: growthWorst ? `${fmtChange(growthWorst.lastBytes, growthWorst.firstBytes, false)} (+${fmtBytes(growthWorst.bytes)}) over ${filters.window}d` : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={growthState} label="storage_growth" />}
        </Card>
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Tables by type</div>
          {invByType
            ? <Donut segments={invByType.map((e, i) => ({ label: e.name, value: e.value, display: fmtInt(e.value), color: paletteColor(i) }))} centerLabel={fmtInt(invTotal)} centerSub="tables + views" />
            : <ChartNote state={invState} label="table_inventory_type" />}
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Oldest untouched tables</div>
          {deadItems
            ? <HBarList items={deadItems} valueFmt={(v) => `${fmtInt(v)} days`} />
            : <ChartNote state={deadState} label="access_dead_table_candidates" />}
        </div>
      </div>
      <UnreadTablesCard filters={filters} meta={meta} />
      {/* The "Fastest-growing tables" card above already carries storage_growth's own ChartNote on
          any non-ok outcome -- repeating it here for the SAME check would read as two different
          problems (must-fix #5: "2 lines for the single check storage_growth"), so this whole
          panel is skipped once that outcome is known, not just re-noted a second time. */}
      {growth || growthState.phase !== "ready" ? (
        <div className="chart-card" style={{ marginTop: 14 }}>
          <div className="chart-card-title">Growth, fastest first</div>
          {growth ? (
            <React.Fragment>
              {growthGrowers && growthGrowers.length
                ? <HBarList items={growthGrowers} valueFmt={(v, it) => `${fmtChange(it.lastBytes, it.firstBytes, false)} (+${fmtBytes(it.bytes)})`} />
                : <span className="muted">{`No table grew fast enough to flag, out of ${fmtInt(growth.length)} checked.`}</span>}
              <PqgsNote facts={[
                {
                  label: "Dropped",
                  value: growthDropped!.length ? growthDropped!.slice(0, 3).map((r) => r.full_name).join(", ") : null,
                  detail: growthDropped!.length > 3 ? `+${fmtInt(growthDropped!.length - 3)} more` : null,
                },
                {
                  label: "No owner",
                  value: growthNoOwner ? growthNoOwnerRows!.slice(0, 3).map((r) => r.full_name).join(", ") : "0",
                  tone: numOrZero(growthNoOwner) > 0 ? "warn" : "ok",
                  detail: numOrZero(growthNoOwner) > 0
                    ? `${numOrZero(growthNoOwner) > 3 ? `+${fmtInt(numOrZero(growthNoOwner) - 3)} more · ` : ""}set an owner (config); of ${fmtInt(growth.length)} with a size history`
                    : "every table has an owner",
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={growthState} label="storage_growth" />}
        </div>
      ) : null}
    </div>
  );
}

// ─────────── Small files (storage_small_files) ───────────
function SmallFilesPanel({ rows, state, maxCat }: LooseProps) {
  const flagged = rows ? [...rows.filter((r: any) => r.status === "CRITICAL" || r.status === "WARN")]
    .sort((a, b) => numOrZero(b.active_files) - numOrZero(a.active_files)) : null;
  const shown = flagged ? flagged.slice(0, maxCat || 8) : null;
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">Small files</div>
      {rows ? (
        flagged!.length ? (
          <React.Fragment>
            <div className="pqgs-rt">
              <div className="pqgs-rt-head pqgs-rt-cols-4">
                <span>Table</span><span style={{ textAlign: "right" }}>Files</span>
                <span style={{ textAlign: "right" }}>Avg file size</span><span>Status</span>
              </div>
              {shown!.map((r) => (
                <div className="pqgs-rt-row pqgs-rt-cols-4" key={r.table_id}>
                  <span className="pqgs-rt-name" title={r.full_name}>{r.full_name}</span>
                  <span className="pqgs-rt-num">{fmtInt(r.active_files)}</span>
                  <span className="pqgs-rt-num">{r.avg_file_size_mb != null ? `${r.avg_file_size_mb.toLocaleString()} MB` : "-"}</span>
                  <span><StatusPill kind={bandPillKind(r.status)} /></span>
                </div>
              ))}
            </div>
            {flagged!.length > shown!.length && (
              <div className="pqgs-card-note" style={{ marginTop: 8 }}>{`${fmtInt(flagged!.length - shown!.length)} more flagged table${flagged!.length - shown!.length === 1 ? "" : "s"} not shown -- see Checks below.`}</div>
            )}
          </React.Fragment>
        ) : <span className="muted">{`No table has too many small files, out of ${fmtInt(rows.length)} checked.`}</span>
      ) : <ChartNote state={state} label="storage_small_files" />}
    </div>
  );
}

// ─────────── PO coverage (storage_po_coverage) ───────────
function PoCoveragePanel({ rows, state, maxCat }: LooseProps) {
  const catalogs = rows ? rows.filter((r: any) => r.section === "catalog_summary") : null;
  const topTables = rows ? rows.filter((r: any) => r.section === "largest_table_without_po") : null;
  const flaggedCatalogs = catalogs ? [...catalogs.filter((r: any) => r.status !== "OK")]
    .sort((a, b) => numOrZero(b.pct_bytes_without_po) - numOrZero(a.pct_bytes_without_po)) : null;
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">Predictive optimization coverage</div>
      {rows ? (
        <React.Fragment>
          {flaggedCatalogs!.length ? (
            <div className="pqgs-rt">
              <div className="pqgs-rt-head pqgs-rt-cols-4">
                <span>Catalog</span><span style={{ textAlign: "right" }}>Tables w/o PO</span>
                <span style={{ textAlign: "right" }}>% bytes w/o PO</span><span>Status</span>
              </div>
              {flaggedCatalogs!.slice(0, maxCat || 8).map((r) => (
                <div className="pqgs-rt-row pqgs-rt-cols-4" key={r.catalog_name}>
                  <span className="pqgs-rt-name" title={r.catalog_name}>{r.catalog_name}</span>
                  <span className="pqgs-rt-num">{`${fmtInt(r.tables_without_po)} of ${fmtInt(r.tables_total)}`}</span>
                  <span className="pqgs-rt-num">{fmtPct(r.pct_bytes_without_po, 0)}</span>
                  <span><StatusPill kind={bandPillKind(r.status)} /></span>
                </div>
              ))}
            </div>
          ) : <span className="muted">{`No catalog has enough bytes without predictive optimization to flag, out of ${fmtInt(catalogs.length)} checked.`}</span>}
          {topTables.length > 0 && (
            <React.Fragment>
              <div className="pqgs-card-note" style={{ marginTop: 10 }}>Largest tables still missing it, biggest first:</div>
              <HBarList
                items={[...topTables]
                  .sort((a, b) => numOrZero(b.active_bytes_of_table) - numOrZero(a.active_bytes_of_table))
                  .slice(0, maxCat || 8)
                  .map((r) => ({ name: r.full_name, value: numOrZero(r.active_bytes_of_table) }))}
                valueFmt={(v) => fmtBytes(v)}
              />
            </React.Fragment>
          )}
        </React.Fragment>
      ) : <ChartNote state={state} label="storage_po_coverage" />}
    </div>
  );
}

// po_failure_reasons.sql's own fallback reason text for any operation_status it does not translate
// -- that text points a reader at "operation_status", a column this panel never otherwise shows, so
// the fallback row also prints the raw code it means (must-fix #16).
const PO_GENERIC_REASON = "operation failed; see operation_status for detail";
function poFailureReasonText(r: any) {
  return r.reason === PO_GENERIC_REASON && r.operation_status ? `${r.reason} (${r.operation_status})` : (r.reason || "-");
}

// ─────────── PO failure reasons (po_failure_reasons) ───────────
function PoFailureReasonsPanel({ rows, state, maxCat, windowDays }: LooseProps) {
  const flagged = rows ? rows.filter((r: any) => r.status !== "OK") : null;
  const okKept = rows ? rows.filter((r: any) => r.status === "OK" && !r.is_other) : null;
  const other = rows ? rows.find((r: any) => r.is_other) : null;
  const shown = rows ? [...flagged, ...okKept].slice(0, maxCat || 8) : null;
  return (
    <div className="chart-card" style={{ marginTop: 14 }}>
      <div className="chart-card-title">Predictive optimization failures</div>
      {rows ? (
        shown!.length || (other && other.pooled_count > 0) ? (
          <div className="pqgs-rt">
            <div className="pqgs-rt-head pqgs-rt-cols-5">
              <span>Table</span><span>Reason</span><span style={{ textAlign: "right" }}>Failed ops</span>
              <span style={{ textAlign: "right" }}>Est. DBU</span><span>Status</span>
            </div>
            {shown!.map((r) => (
              // One table can fail from several workspaces, so the key includes the workspace.
              <div className="pqgs-rt-row pqgs-rt-cols-5" key={`${r.workspace_id}:${r.table_id}:${r.operation_status}`}>
                <span className="pqgs-rt-name" title={r.full_name}>{r.workspace_id ? `${r.full_name} · ${resolveName("workspace", r.workspace_id, r.workspace_id) || r.workspace_id}` : r.full_name}</span>
                <span className="pqgs-rt-note">{poFailureReasonText(r)}</span>
                <span className="pqgs-rt-num">{fmtInt(r.failed_operations)}</span>
                <span className="pqgs-rt-num">{fmtDbu(r.estimated_dbus_spent, 1)}</span>
                <span><StatusPill kind={bandPillKind(r.status)} /></span>
              </div>
            ))}
            {other && other.pooled_count > 0 && (
              <div className="pqgs-rt-row pqgs-rt-cols-5">
                <span className="pqgs-rt-name">{`Other (${fmtInt(other.pooled_count)})`}</span>
                <span className="pqgs-rt-note">-</span>
                <span className="pqgs-rt-num">{fmtInt(other.failed_operations)}</span>
                <span className="pqgs-rt-num">{fmtDbu(other.estimated_dbus_spent, 1)}</span>
                <span><StatusPill kind={bandPillKind("OK")} /></span>
              </div>
            )}
          </div>
        ) : <span className="muted">{`No predictive optimization failures in the last ${fmtInt(windowDays)} days.`}</span>
      ) : poNotUsedNote(state) || <ChartNote state={state} label="po_failure_reasons" />}
    </div>
  );
}

// po_maintenance_cost_by_table/po_vacuum_reclaimed_bytes/po_clustering_activity/
// po_data_skipping_backfill all read the same predictive-optimization operation history -- an
// ok_empty_window outcome on any of them means PO has not run at all in this account, not "quiet
// this window" (ChartNote's own default reading for that outcome), so they share this one note
// instead of each showing their own empty-chart box. Wording matches storage_po_coverage's own
// `actions` line (dbt/models/findings/storage/_findings__storage.yml) so the fix reads the same
// wherever a reader meets it.
function poUnused(state: any) {
  return !!(state && state.phase === "ready" && state.outcome === "ok_empty_window");
}
function poNotUsedNote(state: any) {
  if (!poUnused(state)) return null;
  return (
    <div className="chart-note empty">
      <div className="chart-note-text">
        Predictive optimization has not run on any table in this account. Turn it on at the catalog level (ALTER CATALOG ... SET PREDICTIVE OPTIMIZATION ON) or per table.
      </div>
    </div>
  );
}

// ─────────── Maintenance ───────────
function StorageMaintenanceContent({ filters, maxCat, onVerdict }: LooseProps) {
  const maintState = useFindingData("po_maintenance_cost_by_table", filters.window, filters.workspaceIds, filters.envs);
  const vacState = useFindingData("po_vacuum_reclaimed_bytes", filters.window, filters.workspaceIds, filters.envs);
  const clusterState = useFindingData("po_clustering_activity", filters.window, filters.workspaceIds, filters.envs);
  const churnState = useFindingData("po_clustering_column_churn", filters.window, filters.workspaceIds, filters.envs);
  const skipState = useFindingData("po_data_skipping_backfill", filters.window, filters.workspaceIds, filters.envs);
  const smallFilesState = useFindingData("storage_small_files", filters.window, filters.workspaceIds, filters.envs);
  const poCoverageState = useFindingData("storage_po_coverage", filters.window, filters.workspaceIds, filters.envs);
  const poFailureState = useFindingData("po_failure_reasons", filters.window, filters.workspaceIds, filters.envs);

  const maint = rowsOf(maintState);
  const vac = rowsOf(vacState);
  const cluster = rowsOf(clusterState);
  const churn = rowsOf(churnState);
  const skip = rowsOf(skipState);
  const smallFiles = rowsOf(smallFilesState);
  const poCoverage = rowsOf(poCoverageState);
  const poFailure = rowsOf(poFailureState);

  const maintByTable = maint ? groupSum(maint, (r) => r.table_name || "(unknown table)", "estimated_dbu") : null;
  const maintTotal = maintByTable ? maintByTable.reduce((s, e) => s + e.value, 0) : null;
  const maintTop = maintByTable && maintByTable.length ? [...maintByTable].sort((a, b) => b.value - a.value)[0] : null;
  // po_failure_reasons is the one authoritative failed-operation count on this page (per table x
  // reason, every WARN/CRITICAL row kept, the rest pooled) -- reading it here too, instead of a
  // second row-count off po_maintenance_cost_by_table's own coarser grain, is what keeps this card
  // and the failures panel below from stating two different failure counts on the same page.
  const totalFailedOps = poFailure ? sumBy(poFailure, "failed_operations") : null;

  // Rows are per workspace; roll them back up to one per table.
  const vacFlaggedRows = vac ? vac.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const vacFlaggedByTable = vacFlaggedRows ? groupSum(vacFlaggedRows, (r) => r.table_name || "(unknown table)", "vacuum_estimated_dbu") : null;
  const vacFlaggedDbu = vacFlaggedRows ? sumBy(vacFlaggedRows, "vacuum_estimated_dbu") : null;
  const vacWorst = vacFlaggedByTable && vacFlaggedByTable.length ? [...vacFlaggedByTable].sort((a, b) => b.value - a.value)[0] : null;

  const clusterByTable = cluster ? groupSum(cluster, (r) => r.table_name || "(unknown table)", "clustering_estimated_dbu") : null;
  const clusterTotal = clusterByTable ? clusterByTable.reduce((s, e) => s + e.value, 0) : null;
  const clusterTop = clusterByTable && clusterByTable.length ? [...clusterByTable].sort((a, b) => b.value - a.value)[0] : null;
  const churnFlaggedTables = churn ? new Set(churn.filter((r) => r.has_column_selection_changed === true || r.has_column_selection_changed === "true").map((r) => r.table_name)) : null;
  const churnFlagged = churnFlaggedTables ? churnFlaggedTables.size : null;

  const skipFlaggedRows = skip ? skip.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const skipFlaggedByTable = skipFlaggedRows ? groupSum(skipFlaggedRows, (r) => r.table_name || "(unknown table)", "scanned_bytes") : null;
  const skipWorst = skipFlaggedByTable && skipFlaggedByTable.length ? [...skipFlaggedByTable].sort((a, b) => b.value - a.value)[0] : null;

  const verdictSentence = maintByTable
    ? `${fmtDbu(maintTotal, 0)} spent on maintenance over the last ${filters.window} days${vacFlaggedByTable && vacFlaggedByTable.length ? `; ${fmtDbu(vacFlaggedDbu, 1)} on zero-reclaim VACUUMs` : ""}.`
    : poUnused(maintState) ? "Predictive optimization has not run on any table in this account."
    : "Loading maintenance...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Maintenance DBU burn">
          {maintByTable ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtDbu(maintTotal, 0)}</div>
              <PqgsNote facts={[
                { label: "Failed", value: totalFailedOps ? fmtInt(totalFailedOps) : null, tone: totalFailedOps ? "crit" : undefined, detail: totalFailedOps ? "operations failed outright" : null },
                { label: "Worst", value: maintTop ? maintTop.name : null, detail: maintTop ? fmtDbu(maintTop.value, 0) : null },
              ]} />
            </React.Fragment>
          ) : poNotUsedNote(maintState) || <ChartNote state={maintState} label="po_maintenance_cost_by_table" />}
        </Card>
        <Card title="Vacuum reclaiming nothing">
          {vac ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${vacFlaggedByTable!.length ? "warn" : "ok"}`}>{fmtInt(vacFlaggedByTable!.length)}</div>
              <PqgsNote facts={[
                { label: "Wasted", value: fmtDbu(vacFlaggedDbu, 1), tone: vacFlaggedByTable!.length ? "warn" : "ok", detail: vacFlaggedByTable!.length ? "zero-reclaim VACUUMs" : "every VACUUM reclaimed real bytes" },
                { label: "Worst", value: vacWorst ? vacWorst.name : null },
              ]} />
            </React.Fragment>
          ) : poNotUsedNote(vacState) || <ChartNote state={vacState} label="po_vacuum_reclaimed_bytes" />}
        </Card>
        <Card title="Clustering activity">
          {clusterByTable ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtDbu(clusterTotal, 0)}</div>
              <PqgsNote facts={[
                { label: "Worst", value: clusterTop ? clusterTop.name : null, detail: clusterTop ? fmtDbu(clusterTop.value, 0) : null },
                { label: "Key churn", value: churnFlagged ? fmtInt(churnFlagged) : "0", tone: churnFlagged ? "warn" : "ok", detail: churnFlagged ? "tables re-picking keys" : null },
              ]} />
            </React.Fragment>
          ) : poNotUsedNote(clusterState) || <ChartNote state={clusterState} label="po_clustering_activity" />}
        </Card>
        <Card title="Data-skipping backfill">
          {skip ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${skipFlaggedByTable!.length ? "warn" : "ok"}`}>{fmtInt(skipFlaggedByTable!.length)}</div>
              <PqgsNote facts={[
                {
                  label: "Worst",
                  value: skipWorst ? skipWorst.name : "None flagged",
                  tone: skipWorst ? undefined : "ok",
                  detail: skipWorst ? `${fmtGb(skipWorst.value, 1, true)} scanned, no new skip columns` : null,
                },
              ]} />
            </React.Fragment>
          ) : poNotUsedNote(skipState) || <ChartNote state={skipState} label="po_data_skipping_backfill" />}
        </Card>
      </div>
      <div className="chart-card">
        <div className="chart-card-title">Maintenance DBU by table</div>
        {maintByTable
          ? <HBarList items={topNWithOther(maintByTable, maxCat, "Other tables")} valueFmt={(v) => fmtDbu(v, 1)} />
          : poNotUsedNote(maintState) || <ChartNote state={maintState} label="po_maintenance_cost_by_table" />}
      </div>
      <SmallFilesPanel rows={smallFiles} state={smallFilesState} maxCat={maxCat} />
      <PoCoveragePanel rows={poCoverage} state={poCoverageState} maxCat={maxCat} />
      <PoFailureReasonsPanel rows={poFailure} state={poFailureState} maxCat={maxCat} windowDays={filters.window} />
    </div>
  );
}

AreaContent.register("storage", "tables", StorageTablesContent);
AreaContent.register("storage", "maintenance", StorageMaintenanceContent);
