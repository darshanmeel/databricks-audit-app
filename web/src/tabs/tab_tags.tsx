// the Tags page: which queries, jobs, pipelines, serverless notebooks, clusters, warehouses and
// workspaces miss the mandatory tags (settings mandatory_tag_keys), and where each tag is found:
// on the object itself, on the job or pipeline that ran a query, on its compute, or on its
// workspace (GET /api/tag_compliance).

import React from "react";

import { Api } from "../api";
import { fmtDate, fmtInt, fmtPct } from "../format";
import { useNames, wsPhrase } from "../components/names";
import { AreaContent, Card } from "../components/primitives";
import { HBar, Kpi, KpiRow } from "../components/charts";
import { csvField } from "../components/findings_table";
import { WorkspaceTagsCard } from "./tab_cost";
import { envLabel } from "./tab_money";

// Where each level looks, in the order a tag is looked for.
const TAG_LEVEL_LABELS: Record<string, any> = {
  queries: { own: "the query", job: "the job or pipeline that ran it", compute: "its compute", workspace: "its workspace" },
  jobs: { own: "the job", compute: "its compute bill", workspace: "its workspace" },
  pipelines: { own: "the pipeline", compute: "its compute bill", workspace: "its workspace" },
  notebooks: { compute: "its usage policy (bill)", workspace: "its workspace" },
  clusters: { own: "the cluster", compute: "its bill", workspace: "its workspace" },
  warehouses: { own: "the warehouse", compute: "its bill", workspace: "its workspace" },
  workspaces: { own: "the workspace" },
};
const TAG_KIND_TITLE: Record<string, string> = {
  queries: "Queries", jobs: "Jobs", pipelines: "Pipelines", notebooks: "Serverless notebooks",
  clusters: "Clusters", warehouses: "Warehouses", workspaces: "Workspaces",
};
const TAG_KIND_ONE: Record<string, string> = {
  queries: "query", jobs: "job", pipelines: "pipeline", notebooks: "serverless notebook",
  clusters: "cluster", warehouses: "warehouse", workspaces: "workspace",
};
// Where a query came from (query history's query_source and client_application).
const ORIGIN_LABELS: Record<string, string> = {
  sql_editor: "SQL editor", dashboard: "Dashboards", genie: "Genie", alert: "Alerts", notebook: "Notebooks",
  job: "Jobs", pipeline: "Pipelines", tool: "Other tools (BI, JDBC, connectors)", other: "Unknown", unknown: "Unknown",
};
function originLabel(o: any) {
  return ORIGIN_LABELS[o] || o;
}

function tagNoun(kind: any, n: any) {
  return n === 1 ? TAG_KIND_ONE[kind] : TAG_KIND_TITLE[kind].toLowerCase();
}

function tagVerb(n: any, one: any, many: any) {
  return n === 1 ? one : many;
}
const TAG_OFFENDERS_SHOWN = 10;

function useTagCompliance(filters: any) {
  const key = JSON.stringify([filters.window, filters.workspaceIds || [], filters.envs || []]);
  const [state, setState] = React.useState<{ phase: string; data: any }>({ phase: "loading", data: null });
  React.useEffect(() => {
    let live = true;
    setState({ phase: "loading", data: null });
    Api.tagCompliance(filters.window, filters.workspaceIds, filters.envs)
      .then((d) => { if (live) setState({ phase: "ready", data: d }); })
      .catch(() => { if (live) setState({ phase: "error", data: null }); });
    return () => { live = false; };
  }, [key]);
  return state;
}

// "646 (72%)" -- a count with its share of the type's total.
function tagCount(n: any, total: any) {
  return total > 0 ? `${fmtInt(n)} (${fmtPct((n / total) * 100, 0)})` : fmtInt(n);
}

// "name (id)", or "warehouse <id>" when the export has no name for it.
function offenderLabel(o: any, t: any) {
  if (o.kind === "serverless") return o.name;
  if (t.kind === "workspaces") return wsPhrase(o.id);
  const noun = t.kind === "queries" ? o.kind : TAG_KIND_TITLE[t.kind].slice(0, -1).toLowerCase();
  if (!o.name) return `${noun} ${o.id}`;
  return o.name === String(o.id) ? o.name : `${o.name} (${o.id})`;
}

function lastLevel(t: any) {
  return t.levels[t.levels.length - 1];
}

// Where a tag was found, colored from green (the object's own tag) to solid red (missing).
const TAG_SOURCE_ORDER = ["own", "job", "compute", "workspace", "missing"];
function tagSourceWord(level: any, t: any, o: any) {
  if (level === "own") return "Own tag";
  if (level === "job") return "Job or pipeline";
  if (level === "workspace") return "Workspace";
  if (level === "missing") return "Missing";
  if (t.kind === "jobs" || t.kind === "pipelines") return "Compute bill";
  if (t.kind === "notebooks") return "Usage policy";
  if (t.kind === "warehouses") return "Warehouse bill";
  if (t.kind === "clusters") return "Cluster bill";
  return o && o.kind === "warehouse" ? "Warehouse" : o && o.kind === "cluster" ? "Cluster" : "Usage policy";
}

// The tags on an object's bill: value, the day it first showed there (or, gone now, its last day),
// the other values it had, and the bill's own last day when that is old.
function billText(o: any) {
  const entries = Object.entries<any>(o.bill || {});
  const old = o.last_billed ? `last billed ${fmtDate(o.last_billed)}` : "";
  if (!entries.length) return old || "none";
  const tags = entries.map(([k, b]) => {
    const when = b.now ? (b.since ? ` since ${fmtDate(b.since)}` : "") : (b.until ? `, gone after ${fmtDate(b.until)}` : "");
    const before = b.others && b.others.length ? ` (before: ${b.others.join(", ")})` : "";
    return `${k}=${b.value == null ? "mixed" : b.value}${b.workspace ? " (the workspace's tag)" : b.policy ? " (usage policy)" : ""}${when}${before}`;
  }).join("; ");
  return old ? `${old}: ${tags}` : tags;
}

// "Missing (mixed: red, blue)" -- a workspace whose billed values split with no one value on most.
function mixedText(o: any, k: any) {
  const values = o.mixed && o.mixed[k];
  if (!values) return "";
  return values.length ? ` (mixed: ${values.join(", ")})` : " (mixed values)";
}
const BILL_HEAD: Record<string, string> = {
  queries: "On the warehouse's or cluster's bill", jobs: "On its bill", pipelines: "On its bill",
  notebooks: "On its bill (usage policy)", clusters: "On its bill", warehouses: "On its bill",
};

// An object's own tags as compact JSON; "none" when it carries no tag itself.
function tagJson(tags: any) {
  return tags && Object.keys(tags).length ? JSON.stringify(tags) : "none";
}

function TagSource({ level, children }: LooseProps) {
  return <span className={`tag-src ${level}`}>{children}</span>;
}

// One bar per mandatory tag: the level it was first found at, and the objects missing it everywhere.
const TAG_BAR_FILL: Record<string, string> = { own: "var(--st-ok)", job: "var(--st-ok-ink)", compute: "var(--st-warn)", workspace: "var(--st-crit-tint)", missing: "var(--st-crit)" };
function TagLevelBars({ t, keys, labels }: LooseProps) {
  const levelLabels = TAG_LEVEL_LABELS[t.kind];
  const word = (lv: string) => (lv === "missing" ? "missing at every level" : lv === "own" ? `on ${levelLabels.own}` : `from ${levelLabels[lv]}`);
  return (
    <div className="tag-level-bars">
      {keys.map((k: any) => (
        <div key={k} className="tag-level-bar">
          <span className="tag-level-name" title={labels[k] || k}>{labels[k] || k}</span>
          <HBar height={22} segments={[...t.levels, "missing"].map((lv: string) => {
            const n = Number(t.found[k][lv]) || 0;
            return { label: fmtPct((n / t.total) * 100, 0), value: n, color: TAG_BAR_FILL[lv], display: `${word(lv)}, ${tagCount(n, t.total)}` };
          })} />
        </div>
      ))}
    </div>
  );
}

function TagSourceLegend({ t }: LooseProps) {
  const levels = [...t.levels, "missing"];
  const words: Record<string, string> = { own: "own tag", job: "from the job or pipeline that ran it", compute: `from ${TAG_LEVEL_LABELS[t.kind].compute}`, workspace: "from its workspace", missing: "missing at every level" };
  return (
    <div className="tag-src-legend muted gd-small">
      {levels.map((lv) => (
        <span key={lv} className="tag-src-key"><span className="tag-src-swatch" style={{ background: TAG_BAR_FILL[lv] }} /><TagSource level={lv}>{words[lv]}</TagSource></span>
      ))}
    </div>
  );
}

function tagOffendersCsv(t: any, labels: any, keys: any) {
  const isQueries = t.kind === "queries";
  const from = isQueries && t.origins;  // older exports don't say where queries came from
  const head = [isQueries ? "where the queries ran" : "name", "id", "workspace", ...(isQueries ? ["queries"] : []), ...(from ? ["from"] : []),
    ...keys.map((k: any) => labels[k] || k), isQueries ? "query tag keys" : "own tags", ...(BILL_HEAD[t.kind] ? ["on the bill"] : [])];
  const lines = [head.map(csvField).join(",")];
  t.offenders.forEach((o: any) => {
    const src = (k: any) => TAG_SOURCE_ORDER.filter((lv) => o.source && o.source[k] && o.source[k][lv]).map((lv) => tagSourceWord(lv, t, o)).join(" + ");
    lines.push([offenderLabel(o, t), o.id, o.workspace_id != null ? wsPhrase(o.workspace_id) : "", ...(isQueries ? [o.count] : []), ...(from ? [originsText(o.origins)] : []),
      ...keys.map(src), tagJson(isQueries ? o.query_keys : o.tags), ...(BILL_HEAD[t.kind] ? [billText(o)] : [])].map(csvField).join(","));
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
  a.download = `missing-tags-${t.kind}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// "SQL editor 120, Dashboards 30" -- where a group's queries came from, most first.
function originsText(origins: any) {
  const rows = Object.entries<any>(origins || {}).sort((a, b) => b[1] - a[1]);
  return rows.length ? rows.slice(0, 3).map(([o, n]) => `${originLabel(o)} ${fmtInt(n)}`).join(", ") : "-";
}

// Queries by where they came from: how many carry no query tag for each key, and how many miss it
// at every level. Most SQL-editor queries carry none; their compute should.
function TagOrigins({ t, keys, labels }: LooseProps) {
  if (!t.origins) return null;
  const rows = Object.entries<any>(t.origins).sort((a, b) => b[1].total - a[1].total);
  if (!rows.length) return null;
  const lines = keys.map((k: any) => {
    const none = rows.reduce((s, [, o]) => s + o.no_own[k], 0);
    const lost = rows.reduce((s, [, o]) => s + o.missing[k], 0);
    const top = rows.filter(([, o]) => o.no_own[k] > 0).sort((a, b) => b[1].no_own[k] - a[1].no_own[k])[0];
    const most = top ? `, most from ${originLabel(top[0])} (${fmtPct((top[1].no_own[k] / none) * 100, 0)} of them)` : "";
    return `${labels[k] || k}: ${tagCount(none, t.total)} of queries carry no query tag${most}; ${tagCount(lost, t.total)} miss it at every level.`;
  });
  return (
    <React.Fragment>
      {lines.map((l: string, i: number) => <div key={i} className="money-perf-line">{l}</div>)}
      <div className="tag-table-wrap">
        <table className="data tag-levels">
          <thead>
            <tr>
              <th>Where the queries came from</th>
              <th className="num">Queries</th>
              {keys.map((k: any) => <th key={k} className="num">{`No ${labels[k] || k} query tag`}</th>)}
              {keys.map((k: any) => <th key={`m${k}`} className="num">{`${labels[k] || k} missing at every level`}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(([o, r]) => (
              <tr key={o}>
                <td>{originLabel(o)}</td>
                <td className="num mono">{fmtInt(r.total)}</td>
                {keys.map((k: any) => <td key={k} className="num mono">{tagCount(r.no_own[k], r.total)}</td>)}
                {keys.map((k: any) => <td key={`m${k}`} className="num mono">{tagCount(r.missing[k], r.total)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </React.Fragment>
  );
}

function TagOffenders({ t, labels, keys }: LooseProps) {
  const [showAll, setShowAll] = React.useState(false);
  if (!t.offenders.length) return null;
  const rows = showAll ? t.offenders : t.offenders.slice(0, TAG_OFFENDERS_SHOWN);
  const isQueries = t.kind === "queries";
  const from = isQueries && t.origins;  // older exports don't say where queries came from
  return (
    <React.Fragment>
      <div className="tag-table-wrap">
      <table className="data tag-offenders">
        <thead>
          <tr>
            <th>{isQueries ? "Where the queries ran" : TAG_KIND_TITLE[t.kind].replace(/s$/, "")}</th>
            {t.kind !== "workspaces" && <th>Workspace</th>}
            {isQueries && <th className="num">Queries</th>}
            {from && <th>From</th>}
            {keys.map((k: any) => <th key={k}>{labels[k] || k}</th>)}
            <th>{isQueries ? "Query tag keys on them (queries)" : "Its own tags"}</th>
            {BILL_HEAD[t.kind] && <th>{BILL_HEAD[t.kind]}</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((o: any, i: any) => (
            <tr key={`${o.id}:${o.workspace_id}:${i}`}>
              <td>{offenderLabel(o, t)}</td>
              {t.kind !== "workspaces" && <td>{o.workspace_id != null ? wsPhrase(o.workspace_id) : "-"}</td>}
              {isQueries && <td className="num mono">{fmtInt(o.count)}</td>}
              {from && <td>{originsText(o.origins)}</td>}
              {keys.map((k: any) => {
                const src = (o.source && o.source[k]) || {};
                const levels = TAG_SOURCE_ORDER.filter((lv) => src[lv]);
                return (
                  <td key={k}>
                    {levels.map((lv, i) => (
                      <React.Fragment key={lv}>
                        {i > 0 && " "}
                        <TagSource level={lv}>
                          {`${tagSourceWord(lv, t, o)}${lv === "missing" ? mixedText(o, k) : ""}${levels.length > 1 ? ` ${fmtInt(src[lv])}` : ""}`}
                        </TagSource>
                      </React.Fragment>
                    ))}
                  </td>
                );
              })}
              <td className="tag-json">{tagJson(isQueries ? o.query_keys : o.tags)}</td>
              {BILL_HEAD[t.kind] && <td className="tag-json">{billText(o)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <div className="data-foot">
        <span className="muted mono">{`Showing ${fmtInt(rows.length)} of ${fmtInt(t.offenders_total)}`}</span>
        {t.offenders.length > TAG_OFFENDERS_SHOWN && (
          <button type="button" className="load-more" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Show fewer"
              : t.offenders_total > t.offenders.length ? `Show the first ${fmtInt(t.offenders.length)}` : `Show all ${fmtInt(t.offenders.length)}`}
          </button>
        )}
        <button type="button" className="load-more" onClick={() => tagOffendersCsv(t, labels, keys)}>
          {t.offenders_total > t.offenders.length ? `Download CSV (first ${fmtInt(t.offenders.length)})` : "Download CSV"}
        </button>
      </div>
    </React.Fragment>
  );
}

// Objects tagged one way and billed another: the cost follows the bill's value.
const TAG_CONFLICTS_SHOWN = 50;
function TagConflicts({ t, labels }: LooseProps) {
  if (!t.conflicts || !t.conflicts.length) return null;
  const one = TAG_KIND_ONE[t.kind];
  const n = t.conflicts_total;
  return (
    <div className="tag-conflicts">
      <div className="money-perf-line">{`${fmtInt(n)} ${n === 1 ? one : TAG_KIND_TITLE[t.kind].toLowerCase()} tagged one way on the ${one} and another on its bill. The cost follows the bill: change the ${one}'s tag or the policy that sets the bill's.`}</div>
      <div className="tag-table-wrap">
        <table className="data">
          <thead><tr><th>{TAG_KIND_TITLE[t.kind].replace(/s$/, "")}</th><th>Workspace</th><th>Tag</th><th>{`On the ${one}`}</th><th>On its bill (the cost)</th></tr></thead>
          <tbody>
            {t.conflicts.slice(0, TAG_CONFLICTS_SHOWN).map((c: any, i: number) => (
              <tr key={`${c.id}:${c.key}:${i}`}>
                <td>{c.name || c.id}</td>
                <td>{c.workspace_id != null ? wsPhrase(c.workspace_id) : "-"}</td>
                <td>{labels[c.key] || c.key}</td>
                <td className="mono">{c.own}</td>
                <td className="mono">{c.bill}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {n > TAG_CONFLICTS_SHOWN && <div className="muted gd-small">{`${fmtInt(n - TAG_CONFLICTS_SHOWN)} more not listed.`}</div>}
    </div>
  );
}

// Missing at every level, per workspace environment: one row per env, biggest first.
function TagEnvTable({ t, keys, labels }: LooseProps) {
  const rows = Object.entries<any>(t.by_env || {}).sort((a, b) => b[1].total - a[1].total);
  if (!rows.length) return null;
  return (
    <div className="tag-table-wrap">
      <table className="data tag-levels">
        <thead>
          <tr>
            <th>Environment</th>
            <th className="num">{TAG_KIND_TITLE[t.kind]}</th>
            <th className="num">Missing at least one</th>
            {keys.map((k: any) => <th key={k} className="num">{`No ${labels[k] || k}`}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map(([env, e]) => (
            <tr key={env}>
              <td>{envLabel(env)}</td>
              <td className="num mono">{fmtInt(e.total)}</td>
              <td className="num mono">{tagCount(e.any, e.total)}</td>
              {keys.map((k: any) => <td key={k} className="num mono">{tagCount(e.by_key[k], e.total)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TagTypeCard({ t, keys, labels, windowDays, keyList }: LooseProps) {
  const title = TAG_KIND_TITLE[t.kind];
  if (t.outcome !== "ok") {
    return <Card title={title}><div className="chart-note-oneline">{t.reason}</div></Card>;
  }
  const levelLabels = TAG_LEVEL_LABELS[t.kind];
  const truly = t.missing[lastLevel(t)];
  const first = t.levels[0];
  const own = t.missing[first];
  const total = t.total;
  const noun = tagNoun(t.kind, total);
  const right = t.kind === "queries" ? `${fmtInt(total)} ${noun}, last ${windowDays} days` : `${fmtInt(total)} ${noun}`;
  const keyWord = keys.length === 1 ? "it" : `all ${keys.length}`;
  const chain = t.levels.map((lv: any) => levelLabels[lv]).join(", then ");
  const misses = (n: any) => `${tagCount(n, total)} ${tagVerb(n, "misses", "miss")}`;
  const summary = total === 0 ? `No ${TAG_KIND_TITLE[t.kind].toLowerCase()} in view.`
    : t.levels.length > 1
      ? `Checking ${chain}: ${misses(truly.any)} at least one of ${keyList}, ${misses(truly.all)} ${keyWord}.`
        + ` On ${levelLabels[first]} alone: ${misses(own.any)} at least one, ${misses(own.all)} ${keyWord}.`
      : `${misses(truly.any)} at least one of ${keyList}, ${misses(truly.all)} ${keyWord}.`;
  const allMiss = total > 0 && truly.any === total
    ? (total === 1 ? "It may still carry other tags; only these count." : "They may still carry other tags; only these count.")
    : null;
  return (
    <Card title={title} right={<span className="muted">{right}</span>}>
      <div className="money-perf-line">{summary}</div>
      {allMiss && <div className="muted gd-small">{allMiss}</div>}
      {t.deleted > 0 && <div className="muted gd-small">{`${fmtInt(t.deleted)} deleted ${tagNoun(t.kind, t.deleted)} ${tagVerb(t.deleted, "is", "are")} left out.`}</div>}
      {t.deleted === null && ["jobs", "clusters", "warehouses"].includes(t.kind) && <div className="muted gd-small">{`This export doesn't record deletions, so deleted ${TAG_KIND_TITLE[t.kind].toLowerCase()} may be counted. Re-run the export to leave them out.`}</div>}
      {t.kind === "workspaces" && t.unbilled && t.unbilled.length > 0 && (
        <div className="muted gd-small">{`${fmtInt(t.unbilled.length)} workspace${t.unbilled.length === 1 ? "" : "s"} with no usage on the bill ${tagVerb(t.unbilled.length, "is", "are")} left out (${t.unbilled.slice(0, 5).map((w: any) => w.name || w.id).join(", ")}${t.unbilled.length > 5 ? ", ..." : ""}): a workspace tag is read from its bill, so ${t.unbilled.length === 1 ? "it can't" : "they can't"} carry one.`}</div>
      )}
      {t.kind === "workspaces" && t.offenders.some((o: any) => o.mixed && Object.keys(o.mixed).length) && (
        <div className="muted gd-small">Mixed values: the tag is on enough of the workspace's billed usage, but no one value has most of it, so it isn't the workspace's tag.</div>
      )}
      {total > 0 && <TagLevelBars t={t} keys={keys} labels={labels} />}
      {total > 0 && (
        <div className="tag-table-wrap">
        <table className="data tag-levels">
          <thead>
            <tr>
              <th>Tag</th>
              {t.levels.map((lv: any) => <th key={lv} className="num">{lv === "own" ? `On ${levelLabels.own}` : `From ${levelLabels[lv]}`}</th>)}
              {t.levels.length > 1 && <th className="num">{`Not on ${levelLabels[first]}`}</th>}
              <th className="num">Missing at every level</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k: any) => (
              <tr key={k}>
                <td>{labels[k] || k}</td>
                {t.levels.map((lv: any) => (
                  <td key={lv} className="num mono">
                    {t.found[k][lv] ? <TagSource level={lv}>{tagCount(t.found[k][lv], total)}</TagSource> : tagCount(0, total)}
                  </td>
                ))}
                {t.levels.length > 1 && <td className="num mono">{tagCount(own.by_key[k], total)}</td>}
                <td className="num mono">
                  {t.found[k].missing ? <TagSource level="missing">{tagCount(t.found[k].missing, total)}</TagSource> : tagCount(0, total)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      {total > 0 && <TagSourceLegend t={t} />}
      {total > 0 && <TagOrigins t={t} keys={keys} labels={labels} />}
      {total > 0 && t.by_env && <div className="muted gd-small">By workspace environment, missing at every level:</div>}
      {total > 0 && <TagEnvTable t={t} keys={keys} labels={labels} />}
      <TagOffenders t={t} labels={labels} keys={keys} />
      <TagConflicts t={t} labels={labels} />
    </Card>
  );
}

function TagsPanel({ filters, onVerdict }: LooseProps) {
  useNames(); // workspace names resolve once names.tsx has them
  const state = useTagCompliance(filters);
  const d = state.data;
  const types = d ? d.types : [];
  const byKind = Object.fromEntries(types.map((t: any) => [t.kind, t]));
  const labels = (d && d.labels) || {};
  const keyList = d ? d.keys.map((k: any) => labels[k] || k).join(", ") : "";
  const q = byKind.queries;
  const verdict = state.phase !== "ready" ? "Loading tags..."
    : q && q.outcome === "ok" && q.total
      ? `${fmtPct((q.missing[lastLevel(q)].any / q.total) * 100, 0)} of queries miss at least one of ${keyList} at every level.`
      : `Mandatory tags: ${keyList}.`;
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);

  if (state.phase === "loading") return <div className="muted">Loading...</div>;
  if (state.phase === "error") return <div className="chart-note-oneline">Could not load tag counts.</div>;
  return (
    <div>
      <div className="scope-note">
        <strong>{`Mandatory tags: ${keyList} (settings: mandatory_tag_keys)${d!.keys.includes("environment") && !d!.keys.includes("env") ? "; env counts as environment" : ""}. A tag is looked for on the object itself, then (for a query) on the job or pipeline that ran it, then on its compute (the warehouse's or cluster's own tag, or its bill, where usage-policy tags land), then on its workspace; missing at every level is truly missing.`}</strong>
        <div>On Azure the workspace's own tag is copied onto every bill row, so a bill value equal to it counts as the workspace's, not the compute's, unless a usage policy put it there.</div>
        <div><strong>{`Missing means at least one of the ${d!.keys.length} is absent. 100% missing means every object lacks at least one of them, not that it has no tags: other tags may be there, and they aren't counted.`}</strong></div>
        <div>This page counts objects, and a tag inherited from the workspace counts. For the dollars with no cost center, see Cost › Allocation, which charges each billing row by its own tag.</div>
        {filters.tag && filters.tag.groups && filters.tag.groups.length ? " The Tag filter doesn't narrow this page: it counts every object's tags." : ""}
      </div>
      <KpiRow>
        {types.map((t: any) => {
          const ok = t.outcome === "ok" && t.total > 0;
          const truly = ok ? t.missing[lastLevel(t)] : null;
          return (
            <Kpi
              key={t.kind}
              label={`${TAG_KIND_TITLE[t.kind]} missing a mandatory tag`}
              value={ok ? fmtPct((truly.any / t.total) * 100, 0) : "-"}
              sub={ok ? [`${fmtInt(truly.any)} of ${fmtInt(t.total)} ${tagNoun(t.kind, t.total)}`,
                ...d!.keys.filter((k: any) => truly.by_key[k] > 0).sort((a: any, b: any) => truly.by_key[b] - truly.by_key[a]).map((k: any) => `${labels[k] || k} ${fmtInt(truly.by_key[k])}`)].join(" · ")
                : t.outcome === "ok" ? `No ${t.noun} in view` : "Not in this export"}
              tone={ok && truly.any / t.total >= 0.5 ? "coral" : ok && truly.any > 0 ? "amber" : "default"}
            />
          );
        })}
      </KpiRow>
      {types.map((t: any) => <TagTypeCard key={t.kind} t={t} keys={d!.keys} labels={labels} windowDays={d!.window_days} keyList={keyList} />)}
      <WorkspaceTagsCard filters={filters} />
    </div>
  );
}
AreaContent.register("tags", null, TagsPanel);

// Overview card: the share truly missing per object type, and on own tags alone.
export function TagsOverviewCard({ filters, onOpen }: LooseProps) {
  const state = useTagCompliance(filters);
  const d = state.data;
  const labels = (d && d.labels) || {};
  return (
    <Card
      title="Mandatory tags"
      right={<button type="button" className="ov-link-btn" onClick={onOpen}>{"Tags →"}</button>}
    >
      {state.phase === "loading" ? <div className="muted">Loading...</div>
        : state.phase === "error" ? <div className="chart-note-oneline">Could not load tag counts.</div>
        : (
          <React.Fragment>
            <div className="money-perf-line">{`Missing at least one of ${d!.keys.map((k: any) => labels[k] || k).join(", ")}, checked on the object, then its compute, then its workspace.`}</div>
            <div className="tag-table-wrap">
            <table className="data tag-levels">
              <thead><tr><th></th><th className="num">On the object</th><th className="num">After its compute</th><th className="num">After its workspace</th></tr></thead>
              <tbody>
                {d!.types.map((t: any) => (
                  <tr key={t.kind}>
                    <td>{TAG_KIND_TITLE[t.kind]}</td>
                    {t.outcome === "ok" && t.total > 0 ? ["own", "compute", "workspace"].map((lv) => (
                      <td key={lv} className="num mono">{t.missing[lv] ? tagCount(t.missing[lv].any, t.total) : "-"}</td>
                    )) : <td colSpan={3} className="num muted">{t.outcome === "ok" ? `No ${t.noun} in view` : "Not in this export"}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </React.Fragment>
        )}
    </Card>
  );
}
