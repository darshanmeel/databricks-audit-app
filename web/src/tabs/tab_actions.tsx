// Actions: the flagged checks turned into a short list of fixes,
// each with its impact ($, risk or not priced), effort, team and how to fix it. Findings from the
// data, not tickets: no owner, no status. Possible waste uses the Waste worklist, so the total here
// is the same "possible waste" the Overview and Waste & savings show.

import React from "react";

import { fmtInt, fmtMoney, fmtPct } from "../format";
import { FLAGGED_STATUSES, numOrZero, useFindingAgg, useFindingData, useMultiFindingData } from "../components/hooks";
import { AREA_ORDER, WASTE_IDS, bandOf, homeForQuery } from "../components/tab_registry";
import { ACTION_TEMPLATES } from "../components/action_templates";
import { AreaContent } from "../components/primitives";
import { getCheckLabel } from "../components/labels";
import { ChartNote } from "../components/charts";
import { csvField } from "../components/findings_table";
import { buildWasteWorklist } from "../components/waste_total";
import { identityLabel } from "./tab_governance";
import { KpiMini, entityDisplayName, offenderRows, wsLabel } from "./tab_waste";

// kind: saving (priced $ in the window), risk (security), policy (untagged spend), hygiene.
// `waste` names the WASTE_ITEMS entry that prices it; `checks` are the checks that open it.
// The Actions fix a check feeds, for an "Open in Actions" link on the check itself.
const ACTION_KIND_LABEL: Record<string, string> = { saving: "Savings", risk: "Risks", policy: "Policies", hygiene: "Hygiene" };
const ACTION_KIND_ORDER = ["saving", "risk", "policy", "hygiene"];

// A lens sees a check when its home tab is in the lens, or it prices waste and the lens has
// Waste & savings -- so Actions and the Waste total count the same sources.
export function roleSeesCheck(allowedAreas: any, row: any) {
  return allowedAreas.has(homeForQuery(row).tab) || (allowedAreas.has("waste") && WASTE_IDS.includes(row.query_id));
}

// One action per template with at least one flagged check the role can see. N comes from the
// main check's own "N of M" count, so the title never counts rows.
function buildActions(templates: any, index: any, worklist: any, allowedAreas: any, wasteReady: any) {
  const wasteById: Record<string, any> = {};
  (worklist || []).forEach((w: any) => { wasteById[w.id] = w; });
  return templates.map((t: any) => {
    const rows = t.checks.map((id: any) => index[id]).filter(Boolean)
      .filter((row: any) => roleSeesCheck(allowedAreas, row));
    const flagged = rows.filter((row: any) => { const b = bandOf(row); return b === "CRITICAL" || b === "WARN"; });
    if (!flagged.length) return null;
    const main = flagged[0];
    const n = main.affected && main.affected.flagged ? main.affected.flagged : null;
    const word = n === 1 ? t.noun[0] : t.noun[1];
    const w = t.waste ? wasteById[t.waste] : null;
    const usd = t.kind === "saving" && w && w.isFlagged && w.dollarUsd != null ? w.dollarUsd : null;
    const spendUsd = t.kind === "policy" && main.money && main.money.usd ? main.money.usd : null;
    const critical = flagged.some((row: any) => bandOf(row) === "CRITICAL");
    return {
      ...t, rows: flagged, main, n, usd, spendUsd, critical, wasteEntry: w,
      pending: t.kind === "saving" && !!t.waste && !wasteReady,
      label: n != null ? t.title(fmtInt(n), word) : t.title("", t.noun[1]).replace(/\s+/g, " "),
      checkCount: flagged.length,
    };
  }).filter(Boolean);
}

function actionImpact(a: any) {
  if (a.pending) return { text: "…", tone: "muted" };
  if (a.usd != null) return { text: fmtMoney(a.usd, 0), tone: "money" };
  if (a.kind === "risk") return { text: a.critical ? "Critical" : "Warn", tone: a.critical ? "crit" : "warn" };
  if (a.spendUsd != null) return { text: `${fmtMoney(a.spendUsd, 0)} untagged`, tone: null };
  return { text: "not priced", tone: "muted" };
}

// A readable name for a flagged row with no resource id: a grant, an admin action, a run-as pair.
function actionRowLabel(r: any) {
  if (r.securable != null) return `${r.securable} → ${r.grantee} · ${r.privilege_type}`;
  if (r.actor != null) return [identityLabel(r.actor), r.action_name, wsLabel(r.workspace_id)].filter(Boolean).join(" · ");
  if (r.run_by != null) return `${identityLabel(r.run_by)} runs as ${identityLabel(r.run_as)}`;
  if (r.principal != null) return [identityLabel(r.principal), r.source_ip_address].filter(Boolean).join(" · ");
  if (r.volume_name != null) return [r.volume_catalog, r.volume_schema, r.volume_name].filter(Boolean).join(".");
  if (r.table_name != null) return [r.table_catalog, r.table_schema, r.table_name].filter(Boolean).join(".");
  // An untagged spend line: its workspace, and the unit when it isn't DBUs.
  if (r.workspace_id != null && r.net_list_cost_usd != null) {
    return [wsLabel(r.workspace_id), r.usage_unit && r.usage_unit !== "DBU" ? r.usage_unit : null].filter(Boolean).join(" · ");
  }
  return null;
}

// The worst flagged rows of the action's main check, one per resource. Priced checks use the Waste
// page's own offender list, so both pages name the same worst 5.
function ActionEvidence({ action, filters, dims, wasteState }: LooseProps) {
  const priced = !!(action.wasteEntry && action.wasteEntry.dollarCol && action.main.query_id === action.waste);
  const state = useFindingData(priced ? null : action.main.query_id, filters.window, filters.workspaceIds, filters.envs, 200, FLAGGED_STATUSES);
  let items: any[];
  if (priced) {
    if (!wasteState || wasteState.phase !== "ready") return <div className="muted">Loading...</div>;
    if (wasteState.outcome !== "ok_rows") return <ChartNote state={wasteState} label={action.main.query_id} />;
    items = offenderRows(action.wasteEntry, wasteState.data.rows, dims, filters.window).map((o) => ({
      key: o.key, name: o.ws ? `${o.name} · ${o.ws}` : o.name,
      fact: o.facts && o.facts[0] ? `${o.facts[0].label} ${o.facts[0].value}` : "", usd: o.v,
    }));
  } else {
    if (state.phase !== "ready") return <div className="muted">Loading...</div>;
    if (state.outcome !== "ok_rows") return <ChartNote state={state} label={action.main.query_id} />;
    const entityCol = action.main.affected && action.main.affected.column;
    const disc = numOrZero(state.data.discount_pct);
    const rank = (r: any) => (r.status === "CRITICAL" ? 0 : 1);
    const seen = new Set();
    items = [];
    state.data.rows.slice().sort((a, b) => rank(a) - rank(b) || numOrZero(b.net_list_cost_usd) - numOrZero(a.net_list_cost_usd)
      || numOrZero(b.event_count) - numOrZero(a.event_count)).forEach((r) => {
      const key = entityCol ? `${r.workspace_id}:${r[entityCol]}` : JSON.stringify(r);
      if (seen.has(key)) return;
      seen.add(key);
      const name = entityCol ? entityDisplayName(entityCol, r, dims) : actionRowLabel(r);
      const band = r.status === "CRITICAL" ? "Critical" : "Warn";
      const fact = r.share_of_unit_pct != null ? `${band} · ${fmtPct(r.share_of_unit_pct, 0)} of its spend untagged`
        : r.event_count != null ? `${band} · ${fmtInt(r.event_count)} events` : band;
      items.push({ key, name, fact, usd: r.net_list_cost_usd != null ? numOrZero(r.net_list_cost_usd) * (1 - disc) : null });
    });
  }
  const rows = items.slice(0, 5);
  // A priced action lists only resources with a cost estimate; say how many of the flagged ones that is.
  const unpricedCount = priced && action.n != null ? action.n - items.length : 0;
  return (
    <div className="act-evidence">
      {rows.map((r, i) => (
        <div key={r.key} className="act-evidence-row">
          <span className="act-evidence-name">{r.name || `${fmtInt(i + 1)}. flagged row`}</span>
          <span className="act-evidence-fact">{r.fact}</span>
          <span className="act-evidence-usd mono">{r.usd != null ? fmtMoney(r.usd, 0) : ""}</span>
        </div>
      ))}
      {unpricedCount > 0 ? (
        <div className="muted">{`${fmtInt(items.length)} of ${fmtInt(action.n)} priced${rows.length < items.length ? `, the worst ${fmtInt(rows.length)} shown` : ""}; the other ${fmtInt(unpricedCount)} have no cost estimate.`}</div>
      ) : action.n != null && action.n > rows.length && (
        <div className="muted">{`The worst ${fmtInt(rows.length)} of ${fmtInt(action.n)}.`}</div>
      )}
    </div>
  );
}

function ActionDetail({ action, filters, dims, goTo, wasteState }: LooseProps) {
  const impact = actionImpact(action);
  const home = homeForQuery(action.main);
  return (
    <div className="card act-detail">
      <div className="act-detail-tags">
        <span className="act-team">{action.team}</span>
        <span className="muted">{ACTION_KIND_LABEL[action.kind]}</span>
      </div>
      <h3 className="act-detail-title">{action.label}</h3>
      <div className="act-detail-facts">
        <div><div className="act-fact-label">Impact</div><div className={`act-fact-value mono tone-${impact.tone || "none"}`}>{impact.text}</div>
          {action.usd != null && <div className="muted">{`possible waste, last ${filters.window} days`}</div>}</div>
        <div><div className="act-fact-label">Effort</div><div className="act-fact-value">{action.effort}</div></div>
      </div>
      <div className="act-fact-label">How to fix</div>
      <p className="act-how">{action.how}</p>
      <div className="act-fact-label">Where</div>
      <ActionEvidence action={action} filters={filters} dims={dims} wasteState={wasteState} />
      <div className="act-links">
        {action.rows.map((row: any) => (
          <button key={row.query_id} type="button" className="pqgs-inline-link"
            onClick={() => goTo({ tab: homeForQuery(row).tab, subtab: homeForQuery(row).subtab, focusQueryId: row.query_id })}>
            {`${getCheckLabel(row.query_id, row.title).title} →`}
          </button>
        ))}
      </div>
    </div>
  );
}

function ActionsPanel({ filters, dims, allFindingsIndex, goTo, roleAreas, onVerdict }: LooseProps) {
  const allowedAreas = React.useMemo(() => new Set(roleAreas && roleAreas.length ? roleAreas : AREA_ORDER), [roleAreas]);
  const multiWaste = useMultiFindingData(WASTE_IDS, filters.window, filters.workspaceIds, filters.envs, 5000, FLAGGED_STATUSES);
  const index = allFindingsIndex || {};
  const worklist = React.useMemo(() => buildWasteWorklist(index, multiWaste), [index, multiWaste]);
  const wasteReady = multiWaste.phase === "ready";
  // All spend missing the allocation tag, small lines included: the flagged lines are only those over the $ floor.
  const untaggedAgg = useFindingAgg("cost_chargeback_by_allocation_tag", filters.window, filters.workspaceIds, filters.envs, ["is_missing_allocation_key"], "sum", "net_list_cost_usd");
  const untaggedAll = untaggedAgg.phase === "ready" && untaggedAgg.outcome === "ok_rows"
    ? numOrZero(((untaggedAgg.data.groups || []).find((g) => g.key && g.key[0] === true) || {}).value) * (1 - numOrZero(untaggedAgg.data.discount_pct))
    : null;
  const actions = React.useMemo(
    () => buildActions(ACTION_TEMPLATES, index, worklist, allowedAreas, wasteReady)
      .map((a: any) => (a.key === "untagged" && untaggedAll != null ? { ...a, spendUsd: untaggedAll } : a)),
    [index, worklist, allowedAreas, wasteReady, untaggedAll],
  );

  const [kind, setKind] = React.useState<any>(null);
  const [selectedKey, setSelectedKey] = React.useState<any>(null);
  const shown = actions.filter((a: any) => !kind || a.kind === kind);
  const selected = shown.find((a: any) => a.key === selectedKey) || shown[0] || null;

  // Every flagged check the role can see, and how many of them an action above covers.
  const flaggedIds = Object.values<any>(index).filter((row) => {
    const b = bandOf(row);
    return (b === "CRITICAL" || b === "WARN") && roleSeesCheck(allowedAreas, row);
  }).map((row) => row.query_id);
  const covered = new Set(actions.flatMap((a: any) => a.rows.map((row: any) => row.query_id)));
  const uncovered = flaggedIds.filter((id) => !covered.has(id)).length;

  const pricedUsd = actions.reduce((s: any, a: any) => s + (a.usd || 0), 0);
  const risks = actions.filter((a: any) => a.kind === "risk");
  // Security risks only in a lens with Governance: elsewhere a coverage check would pose as one.
  const seesGovernance = allowedAreas.has("governance");
  const govFlagged = seesGovernance ? Object.values<any>(index).filter((row) => {
    const b = bandOf(row);
    return row.domain === "governance_access" && (b === "CRITICAL" || b === "WARN") && allowedAreas.has(homeForQuery(row).tab);
  }) : [];
  const govCritical = govFlagged.filter((row) => bandOf(row) === "CRITICAL").length;
  const unpricedActions = actions.filter((a: any) => a.usd == null && a.kind !== "risk");
  const unpriced = unpricedActions.length;
  const unpricedNote = [
    ["policy", "policy", "policies"], ["hygiene", "hygiene", "hygiene"], ["saving", "saving with no estimate", "savings with no estimate"],
  ].map(([k, one, many]) => {
    const n = unpricedActions.filter((a: any) => a.kind === k).length;
    return n ? `${fmtInt(n)} ${n === 1 ? one : many}` : null;
  }).filter(Boolean).join(" · ") || "none";

  // The fix list as a spreadsheet: one row per fix, the same numbers the page shows.
  const downloadCsv = () => {
    const head = ["fix", "kind", "team", "impact", "possible waste $", "effort", "flagged", "checks"];
    const lines = [head.map(csvField).join(",")];
    actions.forEach((a: any) => {
      lines.push([a.label, ACTION_KIND_LABEL[a.kind], a.team, actionImpact(a).text, a.usd != null ? Math.round(a.usd) : "",
        a.effort, a.n != null ? a.n : "", a.rows.map((row: any) => getCheckLabel(row.query_id, row.title).title).join("; ")].map(csvField).join(","));
    });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
    link.download = "actions.csv";
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const pricedFixes = actions.filter((a: any) => a.usd != null).length;
  const verdict = !wasteReady ? null : actions.length
    ? `${fmtInt(actions.length)} fix${actions.length === 1 ? "" : "es"} for ${fmtInt(covered.size)} flagged checks; ${fmtMoney(pricedUsd, 0)} of possible waste in the last ${filters.window} days.`
    : "Nothing flagged that a fix on this page covers.";
  React.useEffect(() => { if (onVerdict && verdict) onVerdict(verdict); }, [verdict, onVerdict]);

  return (
    <div>
      <div className="ov-kpi-grid">
        <KpiMini label="Possible waste" value={wasteReady ? fmtMoney(pricedUsd, 0) : "…"}
          note={wasteReady ? `last ${filters.window} days, from ${fmtInt(pricedFixes)} fix${pricedFixes === 1 ? "" : "es"}` : "Loading..."}
          tone={pricedUsd > 0 ? "warn" : null} />
        {seesGovernance && (
          <KpiMini label="Security risks" value={fmtInt(govFlagged.length)} note={`${fmtInt(govCritical)} critical · ${fmtInt(risks.length)} with a fix here`} tone={govCritical ? "crit" : null} />
        )}
        <KpiMini label="Not priced" value={wasteReady ? fmtInt(unpriced) : "…"} note={unpricedNote} />
        <KpiMini label="Other flagged checks" value={fmtInt(uncovered)}
          note={<React.Fragment>{"no fix template yet · "}<button type="button" className="ov-link-btn" onClick={() => goTo({ tab: "findings" })}>All findings →</button></React.Fragment>} />
      </div>
      <div className="act-chips">
        {[null, ...ACTION_KIND_ORDER].map((k) => {
          const count = k ? actions.filter((a: any) => a.kind === k).length : actions.length;
          if (k && !count) return null;
          return (
            <button key={k || "all"} type="button" className={`act-chip${kind === k ? " on" : ""}`} onClick={() => { setKind(k); setSelectedKey(null); }}>
              {`${k ? ACTION_KIND_LABEL[k] : "All"} ${fmtInt(count)}`}
            </button>
          );
        })}
        {actions.length > 0 && <button type="button" className="load-more act-csv" onClick={downloadCsv}>Download CSV</button>}
      </div>
      {shown.length === 0 ? (
        <div className="card muted">No fix of this kind for the checks flagged in this window.</div>
      ) : (
        <div className="act-layout">
          <div className="card act-list">
            {ACTION_KIND_ORDER.filter((k) => shown.some((a: any) => a.kind === k)).map((k) => (
              <React.Fragment key={k}>
                <div className="act-group">{`${ACTION_KIND_LABEL[k]} ${fmtInt(shown.filter((a: any) => a.kind === k).length)}`}</div>
                {shown.filter((a: any) => a.kind === k).map((a: any) => {
                  const impact = actionImpact(a);
                  return (
                    <button key={a.key} type="button" className={`act-row${selected && selected.key === a.key ? " on" : ""}`} onClick={() => setSelectedKey(a.key)}>
                      <span className="act-row-title">{a.label}</span>
                      <span className="act-team">{a.team}</span>
                      <span className={`act-row-impact mono tone-${impact.tone || "none"}`}>{impact.text}</span>
                      <span className="act-row-effort muted">{a.effort}</span>
                    </button>
                  );
                })}
              </React.Fragment>
            ))}
          </div>
          {selected && <ActionDetail key={selected.key} action={selected} filters={filters} dims={dims} goTo={goTo}
            wasteState={selected.waste ? (multiWaste.byId || {})[selected.waste] : null} />}
        </div>
      )}
      <div className="muted act-note">Rebuilt from the data on every refresh: nothing here is assigned or tracked, and a fix disappears once its check stops flagging.</div>
    </div>
  );
}
AreaContent.register("actions", null, ActionsPanel);
