// Governance & PII: access / admin / sensitive / lineage
// / sharing sub-tabs (redesign section 6). Includes the former Lineage & PII tab as this area's
// own "lineage" sub-tab.
// Whole metastore, no workspace filter (AREA_REGISTRY's own scope chip covers that once, in
// PageIntro -- nothing here repeats it).

import React from "react";

import { Api } from "../api";
import { fmtInt, fmtMoney } from "../format";
import { resolveName } from "../components/names";
import { countBy, groupSum, numOrZero, rowsOf, sumBy, topNWithOther, useFindingAgg, useFindingData } from "../components/hooks";
import { countsForIds } from "../components/tab_registry";
import { AreaContent, Card, NoDataBlock, PqgsNote } from "../components/primitives";
import { ChartNote, Donut, HBarList, paletteColor } from "../components/charts";
import { LastActivityCard } from "../components/last_activity";

// A card's headline count: the finding's own row_count (an exact tally from the bulk /api/findings
// list, service.list_findings), never rows.length off this tile's OWN fetch -- that fetch caps at
// 5,000 rows by default (useFindingData), so a card with more real rows than the cap silently
// showed the cap itself as if it were the true count.
function rowCountOf(finding: any, rows: any) {
  return finding && finding.row_count != null ? finding.row_count : (rows ? rows.length : null);
}
function findingsIndexOf(findings: any) {
  const idx: Record<string, any> = {};
  (findings || []).forEach((f: any) => { idx[f.query_id] = f; });
  return idx;
}

// run_by/run_as/actor is a raw identity string -- a service-principal UUID, or a human's
// email/username. Only the UUID shape is shortened (its first 8 chars, full id on hover); an
// email or username is already as short as it gets, so it renders in full.
const GOV_UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
// An id-shaped identity in the audit log is a service principal's application id.
export function identityLabel(s: any) {
  if (s === "__REDACTED__") return "(identity redacted by Databricks)";
  return s && GOV_UUID_RE.test(s) ? `SP ${s.slice(0, 8)}` : s;
}
function identityFact(s: any) {
  if (!s) return s;
  return GOV_UUID_RE.test(s) ? <span title={`Service principal ${s}`}>{identityLabel(s)}</span> : identityLabel(s);
}
// Account-level audit events can arrive with no identity; one label everywhere.
function actorLabel(a: any) {
  return identityLabel(a) || "(no actor recorded)";
}

// "A → B", or plain "A" when there is no target to point at (a lineage edge with a null
// downstream table/column is still worth naming, just not as an arrow to nothing).
function arrowPair(a: any, b: any) {
  return b ? `${a} → ${b}` : a;
}
// "catalog.schema.table" -> "table".
function lastSegment(fullName: any) {
  const parts = String(fullName || "?").split(".");
  return parts[parts.length - 1];
}
// "N thing"/"N things" -- fmtInt-formatted count plus a singular-aware noun.
function countNoun(n: any, noun: any) {
  return `${fmtInt(n)} ${noun}${n === 1 ? "" : "s"}`;
}

function useCoverage() {
  const [cov, setCov] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    Api.coverage().then((d) => { if (!cancelled) setCov(d); }).catch(() => { if (!cancelled) setCov(null); });
    return () => { cancelled = true; };
  }, []);
  return cov;
}

// Sources behind `ids` that /api/coverage says came back with zero rows this snapshot -- the same
// "rows == 0 means empty" signal Coverage & Gaps uses. Returns null (unknown, not empty) when no
// manifest is loaded, so a dev checkout with no captured snapshot never falsely claims emptiness.
function emptySourcesForIds(cov: any, ids: any) {
  if (!cov || !cov.tables) return null;
  const idSet = new Set(ids);
  const rows: any[] = [];
  Object.keys(cov.tables).forEach((key) => {
    const info = cov.tables[key];
    const deps = (info.dependent_query_ids || []).filter((id: any) => idSet.has(id));
    if (deps.length && info.rows === 0) {
      rows.push({ name: key.replace(/^system\./, ""), unlocks: `${deps.length} check${deps.length === 1 ? "" : "s"}` });
    }
  });
  return rows;
}

const GOV_REEXPORT = "A workspace/account admin re-exports just these sources, then rebuilds: "
  + "python tools/snapshot.py --out snapshot --only <the tables above>, then python tools/dbt_run.py build --target dev.";

// ─────────── Access & grants ───────────
const ACCESS_IDS = [
  "access_broad_grants", "access_grants_inventory", "access_grants_inventory_extended",
  "access_runas_escalation", "access_login_concentration", "access_network_inbound_denials",
  "access_network_outbound_denials",
];

function GovAccessContent({ findings, filters, maxCat, onVerdict, meta }: LooseProps) {
  const cov = useCoverage();
  const broadState = useFindingData("access_broad_grants", filters.window, filters.workspaceIds, filters.envs);
  const grantsState = useFindingData("access_grants_inventory", filters.window, filters.workspaceIds, filters.envs);
  const runasState = useFindingData("access_runas_escalation", filters.window, filters.workspaceIds, filters.envs);
  const loginState = useFindingData("access_login_concentration", filters.window, filters.workspaceIds, filters.envs);
  const inState = useFindingData("access_network_inbound_denials", filters.window, filters.workspaceIds, filters.envs);
  const outState = useFindingData("access_network_outbound_denials", filters.window, filters.workspaceIds, filters.envs);

  const noData = countsForIds(findings, ACCESS_IDS).noData;
  const empty = emptySourcesForIds(cov, ACCESS_IDS);
  if (noData && empty && empty.length > 0) {
    return (
      <NoDataBlock
        sources={empty}
        unlocks={["Broad grants", "Grants inventory", "Run-as escalation", "Sign-in concentration", "Network denials"]}
        howToFill={GOV_REEXPORT}
        meta={meta} windowDays={filters.window}
      />
    );
  }

  const broad = rowsOf(broadState);
  const grants = rowsOf(grantsState);
  const runas = rowsOf(runasState);
  const login = rowsOf(loginState);
  const inRows = rowsOf(inState);
  const outRows = rowsOf(outState);

  const findingsById = React.useMemo(() => findingsIndexOf(findings), [findings]);
  const broadFinding = findingsById.access_broad_grants;
  const broadTotal = rowCountOf(broadFinding, broad);
  const broadCrit = broadFinding && broadFinding.status_counts ? numOrZero(broadFinding.status_counts.CRITICAL)
    : (broad ? broad.filter((r) => r.status === "CRITICAL").length : null);
  const broadTop = (() => {
    if (!broad || !broad.length) return null;
    const rank = (r: any) => (r.status === "CRITICAL" ? 0 : r.status === "WARN" ? 1 : 2);
    const byGrantee = new Map();
    broad.forEach((r) => {
      const g = byGrantee.get(r.grantee) || { first: r, rank: 9, rows: 0 };
      g.rank = Math.min(g.rank, rank(r));
      g.rows += 1;
      byGrantee.set(r.grantee, g);
    });
    let best: any = null;
    byGrantee.forEach((g) => { if (!best || g.rank < best.rank || (g.rank === best.rank && g.rows > best.rows)) best = g; });
    const first = broad.find((r) => r.grantee === best.first.grantee && rank(r) === best.rank) || best!.first;
    return { ...first, more: best!.rows - 1 };
  })();

  const grantsTotal = grants ? sumBy(grants, "grant_count") : null;
  const grantsByPriv = grants ? groupSum(grants, (r) => r.privilege_type || r.PRIVILEGE_TYPE || "(unknown)", "grant_count") : null;
  const grantsTop = grantsByPriv && grantsByPriv.length ? [...grantsByPriv].sort((a, b) => b.value - a.value)[0] : null;

  // Flagged pairs, like the check row; events are the detail, not the headline.
  const runasFlaggedRows = runas ? runas.filter((r) => r.status === "CRITICAL" || r.status === "WARN") : null;
  const runasAffected = findingsById.access_runas_escalation && findingsById.access_runas_escalation.affected;
  const runasPairs = runasAffected ? runasAffected.flagged : (runasFlaggedRows ? runasFlaggedRows.length : null);
  const runasEvents = runasFlaggedRows ? sumBy(runasFlaggedRows, "event_count") : null;
  const runasTop = runasFlaggedRows && runasFlaggedRows.length ? [...runasFlaggedRows].sort((a, b) => numOrZero(b.event_count) - numOrZero(a.event_count))[0] : null;

  const accessVerdict = broad
    ? `${fmtInt(broadTotal)} broad grant${broadTotal === 1 ? "" : "s"}${broadCrit ? `, ${fmtInt(broadCrit)} critical` : ""}${runas ? `; ${fmtInt(runasPairs)} run-as pair${runasPairs === 1 ? "" : "s"} flagged (${fmtInt(runasEvents)} events)` : ""}.`
    : "Loading access & grants...";
  React.useEffect(() => { if (onVerdict) onVerdict(accessVerdict); }, [accessVerdict, onVerdict]);

  const denialsTotal = (inRows ? sumBy(inRows, "denial_count") : 0) + (outRows ? sumBy(outRows, "denial_count") : 0);
  const denialsKnown = inRows !== null || outRows !== null;

  const loginByPrincipal = login ? topNWithOther(groupSum(login, (r) => identityLabel(r.principal) || "(unknown)", "event_count"), maxCat, "Other principals") : null;

  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Broad grants">
          {broad ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${broadCrit ? "crit" : ""}`}>{fmtInt(broadTotal)}</div>
              <PqgsNote facts={[
                { label: "Critical", value: broadCrit ? fmtInt(broadCrit) : null, tone: "crit" },
                {
                  label: "Worst",
                  value: broadTop ? identityLabel(broadTop.grantee) : "None flagged",
                  tone: broadTop ? undefined : "ok",
                  detail: broadTop ? `${broadTop.privilege_type} on ${broadTop.securable}${broadTop.more ? ` (+${fmtInt(broadTop.more)} more)` : ""}` : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={broadState} label="access_broad_grants" />}
        </Card>
        <Card title="Grants inventory">
          {grants ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(grantsTotal)}</div>
              <PqgsNote facts={[
                { label: "Top", value: grantsTop ? grantsTop.name : null, detail: grantsTop ? `${fmtInt(grantsTop.value)} grants` : null },
                { label: "Rows", value: fmtInt(grants.length), detail: "grantee × privilege pairs" },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={grantsState} label="access_grants_inventory" />}
        </Card>
        <Card title="Run-as escalation">
          {runas ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(runasPairs)}</div>
              <PqgsNote facts={[
                { label: "Pairs flagged", value: runasAffected ? `of ${fmtInt(runasAffected.total)}` : null, detail: `${fmtInt(runasEvents)} events` },
                {
                  label: "Worst",
                  value: runasTop ? <React.Fragment>{identityFact(runasTop.run_by)} running as {identityFact(runasTop.run_as)}</React.Fragment> : "None flagged",
                  tone: runasTop ? undefined : "ok",
                  detail: runasTop ? `${fmtInt(runasTop.event_count)} events` : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={runasState} label="access_runas_escalation" />}
        </Card>
        <Card title="Network denials">
          {denialsKnown ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(denialsTotal)}</div>
              <PqgsNote facts={[
                { label: "Scope", value: "Inbound + outbound" },
                { label: "Blocked by", value: "IP access list or network policy" },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={outState} label="access_network_outbound_denials" />}
        </Card>
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Grants by privilege type</div>
          {grantsByPriv
            ? <Donut segments={topNWithOther(grantsByPriv, maxCat, "Other privileges").map((e, i) => ({ label: e.name, value: e.value, display: fmtInt(e.value), color: e.color || paletteColor(i) }))} />
            : <ChartNote state={grantsState} label="access_grants_inventory" />}
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Sign-in events by principal</div>
          {loginByPrincipal
            ? <HBarList items={loginByPrincipal} valueFmt={(v) => fmtInt(v)} />
            : <ChartNote state={loginState} label="access_login_concentration" />}
        </div>
      </div>
    </div>
  );
}

// ─────────── Admin activity ───────────
const ADMIN_IDS = ["access_admin_role_change_events"];

function GovAdminContent({ findings, filters, maxCat, onVerdict, meta }: LooseProps) {
  const adminState = useFindingData("access_admin_role_change_events", filters.window, filters.workspaceIds, filters.envs);
  const admin = rowsOf(adminState);
  const total = admin ? sumBy(admin, "event_count") : null;
  // The same order as the check: critical first, then the most events.
  const rank = (r: any) => (r.status === "CRITICAL" ? 0 : r.status === "WARN" ? 1 : 2);
  const flaggedRows = admin ? admin.filter((r) => rank(r) < 2) : null;
  const top = flaggedRows && flaggedRows.length
    ? [...flaggedRows].sort((a, b) => rank(a) - rank(b) || numOrZero(b.event_count) - numOrZero(a.event_count))[0] : null;
  const affected = (findingsIndexOf(findings).access_admin_role_change_events || {}).affected;
  const flaggedN = affected ? affected.flagged : (flaggedRows ? flaggedRows.length : null);
  const critN = flaggedRows ? flaggedRows.filter((r) => r.status === "CRITICAL").length : 0;
  const byActor = admin ? topNWithOther(groupSum(admin, (r) => actorLabel(r.actor), "event_count"), maxCat, "Other actors") : null;
  // A workspace filter drops account-level events (workspace 0); say how many left the page.
  const excludedRows = adminState.data && adminState.data.excluded_no_workspace ? numOrZero(adminState.data.excluded_no_workspace.rows) : 0;
  const topWs = top && top.workspace_id != null ? resolveName("workspace", top.workspace_id, top.workspace_id) : null;

  const verdictSentence = admin
    ? `${fmtInt(flaggedN)} admin change pattern${flaggedN === 1 ? "" : "s"} flagged (${fmtInt(critN)} critical) in ${fmtInt(total)} events over the last ${filters.window} days${top ? `; worst: ${actorLabel(top.actor)} × ${top.action_name}${topWs ? ` in ${topWs}` : ""}, ${fmtInt(top.event_count)} events` : ""}.`
    : "Loading admin activity...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      {excludedRows > 0 && (
        <div className="scope-note">{`${countNoun(excludedRows, "account-level row")} (workspace 0: admin, account-admin and owner changes) ${excludedRows === 1 ? "is" : "are"} outside the workspace filter; ${excludedRows === 1 ? "it applies" : "they apply"} to every workspace. Clear the filter to see ${excludedRows === 1 ? "it" : "them"}.`}</div>
      )}
      <div className="pqgs-cards-grid">
        <Card title="Admin and permission changes">
          {admin ? (
            <React.Fragment>
              <div className={`pqgs-card-value ${critN ? "crit" : ""}`}>{fmtInt(flaggedN)}</div>
              <PqgsNote facts={[
                { label: "Patterns flagged", value: affected ? `of ${fmtInt(affected.total)}` : null, detail: `${fmtInt(total)} events` },
                {
                  label: "Worst",
                  value: top ? <React.Fragment>{top.actor ? identityFact(top.actor) : actorLabel(null)} × {top.action_name}</React.Fragment> : "None flagged",
                  tone: top ? (top.status === "CRITICAL" ? "crit" : "warn") : "ok",
                  detail: top ? [topWs, countNoun(top.event_count, "event")].filter(Boolean).join(", ") : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={adminState} label="access_admin_role_change_events" />}
        </Card>
      </div>
      <div className="chart-card">
        <div className="chart-card-title">Admin and permission change events by actor</div>
        {byActor ? <HBarList items={byActor} valueFmt={(v) => fmtInt(v)} /> : <ChartNote state={adminState} label="access_admin_role_change_events" />}
      </div>
      <LastActivityCard kind="workspace" filters={filters} dims={null} />
    </div>
  );
}

// ─────────── Sensitive data ───────────
const SENSITIVE_IDS = [
  "access_classification_coverage", "access_classified_unmasked", "access_sensitive_table_reads",
  "access_pii_outside_tables", "access_data_classification_inventory", "access_column_masks_inventory",
  "access_row_filters_inventory", "access_tags_inventory",
];
// The 4 checks that need system.data_classification.results -- named exactly, since that source is
// commonly not enabled at all (a Public Preview feature), a very different case from "empty this
// window" and worth its own plain explanation rather than a generic empty-panel note.
const CLASSIFICATION_IDS = ["access_classification_coverage", "access_classified_unmasked", "access_sensitive_table_reads", "access_data_classification_inventory"];

function classificationUnavailable(findings: any) {
  const byId: Record<string, any> = {};
  (findings || []).forEach((f: any) => { byId[f.query_id] = f; });
  return CLASSIFICATION_IDS.every((id) => !byId[id] || byId[id].outcome !== "ok_rows");
}

// ABAC row-filter and column-mask policies (SHOW POLICIES in the export); metastore-wide, so no filters apply.
function useAbacPolicies() {
  const [state, setState] = React.useState<{ phase: string; data: any }>({ phase: "loading", data: null });
  React.useEffect(() => {
    let live = true;
    Api.abacPolicies()
      .then((d) => { if (live) setState({ phase: "ready", data: d }); })
      .catch(() => { if (live) setState({ phase: "error", data: null }); });
    return () => { live = false; };
  }, []);
  return state;
}

// Where the export looked, in one line: what a 0 can and cannot mean.
function abacScopeText(info: any) {
  if (!info) return "Not in this export: re-run the export to list ABAC policies.";
  if (!info.exported) return "Not listed: the export could not read the catalog list.";
  const parts = [
    `Asked ${info.metastore ? "the metastore, " : ""}${fmtInt(info.catalogs)} catalog${info.catalogs === 1 ? "" : "s"} and ${fmtInt(info.schemas)} schema${info.schemas === 1 ? "" : "s"} this workspace can see`,
    info.not_readable ? `${fmtInt(info.not_readable)} could not be read (needs READ METADATA or MANAGE)` : null,
    info.schemas_not_asked ? `${fmtInt(info.schemas_not_asked)} more schemas not asked` : null,
  ].filter(Boolean);
  return `${parts.join("; ")}.`;
}

// ROW_FILTER -> "Row filter".
function policyTypeLabel(t: any) {
  const s = String(t || "").replace(/_/g, " ").toLowerCase();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}

// Where the policy is set: its own catalog.schema.table, else the securable the export asked.
function policyScope(r: any) {
  const name = [r.catalog, r.schema, r.table_name].filter(Boolean).join(".");
  if (!name) return `${String(r.on_type || "").toLowerCase()}${r.on_name ? ` ${r.on_name}` : ""}`;
  return `${r.table_name ? "table" : r.schema ? "schema" : "catalog"} ${name}`;
}

// A card count that leaves out the ABAC policies gets a red asterisk pointing at the list below.
function abacFact(n: number | null, noun: string) {
  if (n == null) return { label: "Scope", value: "Set on the table only", detail: "ABAC policies not in this export", tone: "muted" };
  return { label: n ? <span className="abac-star">* ABAC</span> : "ABAC", value: `${fmtInt(n)} ${noun} polic${n === 1 ? "y" : "ies"}`, detail: n ? "not in this count · listed below" : "listed below" };
}

function GovSensitiveContent({ findings, filters, maxCat, onVerdict, meta }: LooseProps) {
  const abac = useAbacPolicies();
  const abacRows: any[] | null = abac.phase === "ready" && abac.data && abac.data.outcome !== "not_assessed" ? abac.data.rows || [] : null;
  const abacMasks = abacRows ? abacRows.filter((r) => /MASK/i.test(String(r.policy_type || ""))).length : null;
  const abacFilters = abacRows ? abacRows.filter((r) => /FILTER/i.test(String(r.policy_type || ""))).length : null;
  const piiState = useFindingData("access_pii_outside_tables", filters.window, filters.workspaceIds, filters.envs);
  const masksState = useFindingData("access_column_masks_inventory", filters.window, filters.workspaceIds, filters.envs);
  const filtersState = useFindingData("access_row_filters_inventory", filters.window, filters.workspaceIds, filters.envs);
  const tagsState = useFindingData("access_tags_inventory", filters.window, filters.workspaceIds, filters.envs);
  const unmaskedState = useFindingData("access_classified_unmasked", filters.window, filters.workspaceIds, filters.envs);

  const pii = rowsOf(piiState);
  const unmasked = rowsOf(unmaskedState);
  const masks = rowsOf(masksState);
  const rowFilters = rowsOf(filtersState);
  const tags = rowsOf(tagsState);

  const findingsById = React.useMemo(() => findingsIndexOf(findings), [findings]);
  const piiTotal = rowCountOf(findingsById.access_pii_outside_tables, pii);
  const masksTotal = rowCountOf(findingsById.access_column_masks_inventory, masks);
  const rowFiltersTotal = rowCountOf(findingsById.access_row_filters_inventory, rowFilters);

  const piiCounts = pii ? countBy(pii, (r) => r.object_type || "(unknown)") : null;
  const piiTop = piiCounts && piiCounts.length ? [...piiCounts].sort((a, b) => b.value - a.value)[0] : null;

  const masksCounts = masks ? countBy(masks, (r) => r.mask_name || "(unnamed mask)") : null;
  const masksByName = masksCounts ? topNWithOther(masksCounts, maxCat, "Other masks") : null;
  const masksTop = masksCounts && masksCounts.length ? [...masksCounts].sort((a, b) => b.value - a.value)[0] : null;

  const cuFinding = findingsById.access_classified_unmasked;
  const cuReady = !!(cuFinding && cuFinding.outcome === "ok_rows");
  const cuFlagged = cuReady && cuFinding.status_counts ? numOrZero(cuFinding.status_counts.CRITICAL) + numOrZero(cuFinding.status_counts.WARN) : null;
  const cuTotal = cuReady ? rowCountOf(cuFinding, unmasked) : null;
  const cuWorst = unmasked ? countBy(unmasked.filter((r) => r.status === "CRITICAL" || r.status === "WARN"), (r) => r.table_name || "(unnamed table)")
    .sort((a, b) => b.value - a.value)[0] || null : null;

  const tagsTotal = tags ? sumBy(tags, "tagged_object_count") : null;
  const tagsTop = tags && tags.length ? [...tags].sort((a, b) => numOrZero(b.tagged_object_count) - numOrZero(a.tagged_object_count))[0] : null;

  const verdictSentence = pii
    ? `${cuFlagged != null ? `${fmtInt(cuFlagged)} of ${fmtInt(cuTotal)} classified columns have no mask; ` : ""}`
      + `${fmtInt(piiTotal)} ${piiTotal === 1 ? "volume or schema sits" : "volumes and schemas sit"} outside tagged tables`
      + `${masks ? `; ${countNoun(masksTotal, "column mask")} and ${countNoun(rowFiltersTotal, "row filter")} in place` : ""}.`
    : "Loading sensitive data...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      {classificationUnavailable(findings) && (
        <NoDataBlock
          title="Data Classification is off: 4 checks can't run"
          sources={[{ name: "Data Classification (system.data_classification.results)", unlocks: "4 checks" }]}
          unlocks={[
            "Sensitive-data classification coverage by catalog",
            "Classified columns with no mask",
            "Who reads sensitive tables",
            "Data classification inventory",
          ]}
          howToFill="Data Classification is not enabled on this account. A workspace admin turns on the Data Classification preview, picks the catalogs to classify, and grants SELECT on system.data_classification.results to whoever runs the export."
        />
      )}
      <div className="pqgs-cards-grid">
        {cuReady && (
          <Card title="Classified, no mask">
            <div className={`pqgs-card-value ${cuFlagged ? "crit" : "ok"}`}>{fmtInt(cuFlagged)}</div>
            <PqgsNote facts={[
              { label: "Of", value: countNoun(cuTotal, "classified column") },
              cuWorst ? { label: "Worst", value: cuWorst.name, detail: countNoun(cuWorst.value, "column") } : null,
            ].filter(Boolean)} />
          </Card>
        )}
        <Card title="Volumes & schemas outside tagged tables">
          {pii ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(piiTotal)}</div>
              <PqgsNote facts={[
                {
                  label: "Top",
                  value: piiTop ? piiTop.name : "None flagged",
                  tone: piiTop ? undefined : "ok",
                  detail: piiTop ? `${fmtInt(piiTop.value)} object${piiTop.value === 1 ? "" : "s"}` : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={piiState} label="access_pii_outside_tables" />}
        </Card>
        <Card title="Column masks in place">
          {masks ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(masksTotal)}{abacMasks ? <span className="abac-star">*</span> : null}</div>
              <PqgsNote facts={[
                {
                  label: "Top",
                  value: masksTop ? masksTop.name : "None",
                  tone: masksTop ? undefined : "ok",
                  detail: masksTop ? countNoun(masksTop.value, "column") : null,
                },
                abacFact(abacMasks, "column-mask"),
              ]} />
            </React.Fragment>
          ) : <ChartNote state={masksState} label="access_column_masks_inventory" />}
        </Card>
        <Card title="Row filters in place">
          {rowFilters || (filtersState.phase === "ready" && filtersState.outcome === "ok_empty_window") ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(rowFilters ? rowFiltersTotal : 0)}{abacFilters ? <span className="abac-star">*</span> : null}</div>
              <PqgsNote facts={[abacFact(abacFilters, "row-filter")]} />
            </React.Fragment>
          ) : <ChartNote state={filtersState} label="access_row_filters_inventory" />}
        </Card>
        <Card title="Tagged objects">
          {tags ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(tagsTotal)}</div>
              <PqgsNote facts={[
                { label: "Top tag", value: tagsTop ? tagsTop.TAG_NAME : null, detail: tagsTop && tagsTop.TAG_VALUE ? `= ${tagsTop.TAG_VALUE}` : null },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={tagsState} label="access_tags_inventory" />}
        </Card>
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Outside tagged tables, by object type</div>
          {piiCounts && piiCounts.length
            ? <Donut segments={piiCounts.map((e, i) => ({ label: e.name, value: e.value, display: fmtInt(e.value), color: paletteColor(i) }))} />
            : <ChartNote state={piiState} label="access_pii_outside_tables" />}
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Column masks by name</div>
          {masksByName ? <HBarList items={masksByName} valueFmt={(v) => fmtInt(v)} /> : <ChartNote state={masksState} label="access_column_masks_inventory" />}
        </div>
      </div>
      <Card title="ABAC policies" right={<span className="muted">row filters and column masks set by policy, metastore-wide</span>}>
        <div className="metric-note">{abac.phase === "ready" ? abacScopeText(abac.data && abac.data.info) : abac.phase === "error" ? "Could not load ABAC policies." : "Loading..."}</div>
        {abacRows && abacRows.length > 0 && (
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table className="data">
              <thead><tr><th>Policy</th><th>Type</th><th>Defined on</th><th>Comment</th></tr></thead>
              <tbody>
                {abacRows.slice(0, 200).map((r, i) => (
                  <tr key={i}>
                    <td className="mono">{r.policy_name}</td>
                    <td>{r.policy_type ? <span className={`abac-type ${/MASK/i.test(r.policy_type) ? "mask" : /FILTER/i.test(r.policy_type) ? "filter" : ""}`}>{policyTypeLabel(r.policy_type)}</span> : null}</td>
                    <td>{policyScope(r)}</td>
                    <td className="muted">{r.comment || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {abacRows && abacRows.length > 200 && <div className="muted">{`First 200 of ${fmtInt(abacRows.length)} policies.`}</div>}
      </Card>
    </div>
  );
}

// ─────────── Lineage ───────────
function GovLineageContent({ findings, filters, maxCat, onVerdict }: LooseProps) {
  const blastState = useFindingData("access_table_lineage_blast_radius", filters.window, filters.workspaceIds, filters.envs, undefined, null,
    ["source_table_full_name", "target_table_full_name", "source_distinct_principals", "distinct_principals", "access_class", "event_count"]);
  const reachState = useFindingData("access_column_lineage_sensitive_reach", filters.window, filters.workspaceIds, filters.envs);
  const propState = useFindingData("access_pii_propagation_untagged", filters.window, filters.workspaceIds, filters.envs);

  const blast = rowsOf(blastState);
  const reach = rowsOf(reachState);
  const prop = rowsOf(propState);

  const findingsById = React.useMemo(() => findingsIndexOf(findings), [findings]);
  const reachTotal = rowCountOf(findingsById.access_column_lineage_sensitive_reach, reach);

  const blastSourceTables = blast ? new Set(blast.map((r) => r.source_table_full_name).filter(Boolean)).size : null;
  // Ranked (and shown) by the source table's own total reach, the same column status is judged
  // on -- falls back to the edge's own count for a WRITE row with no source to roll up to.
  const blastReach = (r: any) => (r.source_distinct_principals != null ? numOrZero(r.source_distinct_principals) : numOrZero(r.distinct_principals));
  const blastTop = blast && blast.length ? [...blast].sort((a, b) => blastReach(b) - blastReach(a))[0] : null;

  const reachTop = reach && reach.length ? [...reach].sort((a, b) => numOrZero(b.distinct_principals) - numOrZero(a.distinct_principals))[0] : null;

  // edges = distinct (source column, target column) pairs, events = sum over edges, top edge by
  // events -- a row is already one edge per workspace (the query's own grain), grouped here again
  // so two workspaces sharing the same edge still read as one, never two. Keyed on the full
  // source/target catalog.schema.table.column, not the bare column name -- email->email between
  // two unrelated table pairs is two edges, not one.
  const propFullName = (catalog: any, schema: any, table: any, column: any) => `${catalog || "?"}.${schema || "?"}.${table || "?"}.${column || "?"}`;
  const propEdges = prop ? Object.values<any>(prop.reduce((acc, r) => {
    const sourceName = propFullName(r.source_table_catalog, r.source_table_schema, r.source_table_name, r.source_column_name);
    const targetName = propFullName(r.target_table_catalog, r.target_table_schema, r.target_table_name, r.target_column_name);
    const key = `${sourceName}\u0000${targetName}`;
    if (!acc[key]) acc[key] = { sourceName, targetName, sourceShort: `${r.source_table_name || "?"}.${r.source_column_name || "?"}`, targetShort: `${r.target_table_name || "?"}.${r.target_column_name || "?"}`, event_count: 0 };
    acc[key].event_count += numOrZero(r.event_count);
    return acc;
  }, {} as Record<string, any>)) : null;
  const propEdgeCount = propEdges ? propEdges.length : null;
  const propTotal = propEdges ? sumBy(propEdges, "event_count") : null;
  const propTop = propEdges && propEdges.length ? [...propEdges].sort((a, b) => numOrZero(b.event_count) - numOrZero(a.event_count))[0] : null;

  const accessSegments = blast ? groupSum(blast, (r) => r.access_class || "(unknown)", "event_count").map((e, i) => ({ label: e.name, value: e.value, display: fmtInt(e.value), color: paletteColor(i) })) : null;
  const propItems = propEdges ? topNWithOther(
    propEdges.map((r) => ({ name: `${r.sourceShort} → ${r.targetShort}`, title: `${r.sourceName} → ${r.targetName}`, value: numOrZero(r.event_count) })), maxCat, "Other edges"
  ) : null;

  const verdictSentence = blast
    ? `${fmtInt(blastSourceTables)} source table${blastSourceTables === 1 ? "" : "s"} with downstream readers${prop ? `; ${fmtInt(propEdgeCount)} PII propagation edge${propEdgeCount === 1 ? "" : "s"} still untagged` : ""}.`
    : "Loading lineage...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Table blast radius">
          {blast ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(blastSourceTables)}</div>
              <PqgsNote facts={[
                {
                  label: "Widest",
                  value: blastTop ? arrowPair(blastTop.source_table_full_name, blastTop.target_table_full_name) : "None",
                  tone: blastTop ? undefined : "ok",
                  detail: blastTop ? countNoun(blastReach(blastTop), "principal") : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={blastState} label="access_table_lineage_blast_radius" />}
        </Card>
        <Card title="Sensitive column reach">
          {reach ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(reachTotal)}</div>
              <PqgsNote facts={[
                {
                  label: "Widest",
                  value: reachTop ? arrowPair(`${lastSegment(reachTop.source_table_full_name)}.${reachTop.source_column_name}`, reachTop.target_column_name ? `${lastSegment(reachTop.target_table_full_name)}.${reachTop.target_column_name}` : null) : "None",
                  tone: reachTop ? undefined : "ok",
                  detail: reachTop ? countNoun(reachTop.distinct_principals, "principal") : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={reachState} label="access_column_lineage_sensitive_reach" />}
        </Card>
        <Card title="PII propagating untagged">
          {prop ? (
            <React.Fragment>
              <div className="pqgs-card-value">{fmtInt(propEdgeCount)}</div>
              <PqgsNote facts={[
                {
                  label: "Worst",
                  value: propTop ? arrowPair(propTop.sourceName, propTop.targetName) : "None",
                  tone: propTop ? undefined : "ok",
                  detail: propTop ? `${countNoun(propTop.event_count, "event")}, still untagged` : null,
                },
              ]} />
            </React.Fragment>
          ) : <ChartNote state={propState} label="access_pii_propagation_untagged" />}
        </Card>
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Downstream access events by class</div>
          {accessSegments ? <Donut segments={accessSegments} /> : <ChartNote state={blastState} label="access_table_lineage_blast_radius" />}
        </div>
        <div className="chart-card">
          <div className="chart-card-title">PII propagating into untagged columns</div>
          {propItems ? <HBarList items={propItems} valueFmt={(v) => fmtInt(v)} /> : <ChartNote state={propState} label="access_pii_propagation_untagged" />}
        </div>
      </div>
    </div>
  );
}

// ─────────── Sharing ───────────
function GovSharingContent({ filters, onVerdict }: LooseProps) {
  const shareState = useFindingData("access_delta_sharing_exposure", filters.window, filters.workspaceIds, filters.envs);
  const share = rowsOf(shareState);
  const recipients = share ? sumBy(share, "recipient_count") : null;
  const top = share && share.length ? [...share].sort((a, b) => numOrZero(b.recipient_count) - numOrZero(a.recipient_count))[0] : null;

  const shareEmpty = shareState.phase === "ready" && shareState.outcome === "ok_empty_window";
  const verdictSentence = share
    ? `${fmtInt(recipients)} external Delta Sharing recipient${recipients === 1 ? "" : "s"}${top ? `; widest: ${top.share_name || "(unnamed share)"}, ${fmtInt(top.recipient_count)} recipients` : ""}.`
    : shareEmpty ? "No Delta Sharing shares are visible to this export's token; check Catalog Explorer › Delta Sharing to be sure none exist."
    : "Loading Delta Sharing exposure...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdictSentence); }, [verdictSentence, onVerdict]);

  return (
    <div className="pqgs-cards-grid">
      <Card title="Delta Sharing exposure">
        {share ? (
          <React.Fragment>
            <div className="pqgs-card-value">{fmtInt(recipients)}</div>
            <PqgsNote facts={[
              {
                label: "Worst",
                value: top ? (top.share_name || "(unnamed share)") : "None",
                tone: top ? undefined : "ok",
                detail: top ? (numOrZero(top.shared_table_count) + numOrZero(top.shared_schema_count) === 0
                  ? `${countNoun(top.recipient_count, "recipient")}, nothing shared yet`
                  : `${countNoun(top.recipient_count, "recipient")}, ${countNoun(top.shared_table_count, "table")}`) : null,
              },
            ]} />
          </React.Fragment>
        ) : shareEmpty ? (
          <React.Fragment>
            <div className="pqgs-card-value">0</div>
            <PqgsNote facts={[{ label: "Status", value: "No shares visible", tone: "muted" }]} />
          </React.Fragment>
        ) : <ChartNote state={shareState} label="access_delta_sharing_exposure" />}
      </Card>
    </div>
  );
}

AreaContent.register("governance", "access", GovAccessContent);
AreaContent.register("governance", "admin", GovAdminContent);
AreaContent.register("governance", "sensitive", GovSensitiveContent);
AreaContent.register("governance", "lineage", GovLineageContent);
AreaContent.register("governance", "sharing", GovSharingContent);
