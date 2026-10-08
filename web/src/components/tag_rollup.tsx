// P4-T-ROLL (tasks/P4-T-SPEC.md section 6.6): the nested
// cost/performance rollup view, TagRollupView. Fetches GET /api/rollup and
// GET /api/rollup/keys with plain `fetch` (no api.ts changes -- that file is TAG-IDX's,
// section 4.7). Before the TAG-IDX merge, `filters.tag` is undefined and this view falls back to
// its own key chooser; after the merge it reads `filters.tag.key` when a tag filter is set
// elsewhere on the page.
//
// No new styles.css classes: every element below reuses an existing class (chart-card,
// section-title, hbar-list, faint, mono, subtabs) or a plain inline style, since styles.css is
// TAG-IDX's file to edit (section 4.7).

import React from "react";

import { Api } from "../api";
import { estLabel, fmtDbu, fmtDurationMs, fmtInt, fmtMoney, fmtPct } from "../format";
import { resolveName } from "./names";
import { clusterName, numOrZero, warehouseName } from "./hooks";
import { Facts } from "./primitives";
import { ChartNote } from "./charts";
import { windowFacts, windowRangeLabel } from "./overview_tile";
import type { DimMaps, Entry, Filters, Meta, Outcome, Workspace } from "../types";

/** A rollup figure set: usd and usd_disc for cost; statements, duration_ms, queue_ms... for performance. */
type Metrics = Record<string, number>;

interface RollupKey {
  tag_key: string;
  display_key?: string;
}

interface RollupValue {
  value: string;
  label: string;
  metrics: Metrics;
  share_of_total: number;
  by_level: Record<string, Metrics>;
  overrides_outer: Metrics;
}

interface RollupNode {
  id: string;
  label: string;
  reason?: string | null;
  source_label?: string | null;
  metrics: Metrics;
  untagged: Metrics;
  share_of_parent?: number | null;
  children?: RollupNode[];
  workspaces?: { workspace_id: string; metrics: Metrics }[];
  resources?: { compute_id: string; compute_kind: string; metrics: Metrics }[];
}

/** GET /api/rollup: one tag key's spend or query time, by value and as a level tree.
 *  On not_assessed, total, reconciliation and tree are null; they are read only on ok_rows. */
export interface RollupData {
  area: string;
  tag_key: string;
  display_key: string;
  window_days: number;
  primary_metric: string;
  discount_pct: number;
  outcome: Outcome;
  not_assessed_reason: string | null;
  total: Metrics;
  by_value: RollupValue[];
  reconciliation: { reconciled: boolean; billing_usd: number; rollup_usd: number; scaled_warehouse_days: number };
  unpriced?: { usage_unit: string | null; quantity: string | number }[];
  scope: { workspace_filtered: boolean; excluded_account_level: Metrics };
  source_status?: { source: string; effect: string | null }[];
  selected: { value: string } | null;
  tree: RollupNode;
}

/** GET /api/rollup/top: the top values of one key, one row per (level, value). */
interface RollupTopData {
  tag_key: string;
  display_key: string;
  primary_metric: string;
  outcome: Outcome;
  not_assessed_reason: string | null;
  total: Metrics;
  tagged: Metrics;
  untagged: { metrics: Metrics };
  rows: { level: string; value: string; label: string; metrics: Metrics }[];
  more_count: number;
}

/** A rollup fetch: idle with no key, loading, failed, or answered. */
export interface RollupState<T> {
  phase: string;
  outcome?: Outcome | null;
  data: T | null;
  error?: string | null;
}

/** The tag filter as this view first read it; today's filter carries `groups`, so these are unset. */
interface PreGroupsTag {
  key?: string;
  value?: string;
  display_key?: string;
}

function tagRollupQs(params: Record<string, unknown>): string {
  const parts: string[] = [];
  Object.keys(params || {}).forEach((key) => {
    const value = params[key];
    if (value === undefined || value === null || value === "") return;
    if (Array.isArray(value)) {
      value.forEach((v) => parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(v))}`));
    } else {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
  });
  return parts.length ? `?${parts.join("&")}` : "";
}

async function tagRollupFetch<T>(path: string, params: Record<string, unknown>): Promise<T> {
  const res = await fetch(path + tagRollupQs(params), { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (e) { /* not JSON */ }
    const err: Error & { status?: number } = new Error(`${res.status} ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function useRollupKeys(area: string, windowDays: number) {
  const [state, setState] = React.useState<{ phase: string; keys: RollupKey[] }>({ phase: "loading", keys: [] });
  React.useEffect(() => {
    let alive = true;
    setState({ phase: "loading", keys: [] });
    tagRollupFetch<{ keys: RollupKey[] }>("/api/rollup/keys", { area, window: windowDays })
      .then((body) => { if (alive) setState({ phase: "ready", keys: body.keys || [] }); })
      .catch(() => { if (alive) setState({ phase: "error", keys: [] }); });
    return () => { alive = false; };
  }, [area, windowDays]);
  return state;
}

function useRollupData(area: string, tagKey: string | null, windowDays: number, workspaceIds: string[], envs: string[], tagValue: string | null | undefined) {
  const [state, setState] = React.useState<RollupState<RollupData>>({ phase: "loading", data: null });
  const depKey = JSON.stringify({ area, tagKey, windowDays, workspaceIds, envs, tagValue, tag: Api.tagFilterKey() });
  React.useEffect(() => {
    if (!tagKey) { setState({ phase: "ready", data: null }); return; }
    let alive = true;
    setState({ phase: "loading", data: null });
    tagRollupFetch<RollupData>("/api/rollup", {
      area, tag_key: tagKey, window: windowDays, tag_value: tagValue,
      workspace_ids: workspaceIds, env: envs,
      ...Api.tagParams(),
    })
      .then((body) => { if (alive) setState({ phase: "ready", data: body }); })
      .catch((err) => { if (alive) setState({ phase: "error", data: null, error: err.message || String(err) }); });
    return () => { alive = false; };
  }, [depKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

// useRollupTop -- GET /api/rollup/top (by_tag_top): the top `top` tag VALUES for one key, ranked
// by $ (or query time for performance), one row per (level, value) -- levels are never merged, so
// a value tagged on both a job and a workspace shows as two separate rows (P4-CM's own "By tag"
// panel, tag_rollup.tsx section 6).
export function useRollupTop(area: string, tagKey: string | null, windowDays: number, workspaceIds: string[], envs: string[], top: number) {
  const [state, setState] = React.useState<RollupState<RollupTopData>>({ phase: "loading", data: null });
  const depKey = JSON.stringify({ area, tagKey, windowDays, workspaceIds, envs, top, tag: Api.tagFilterKey() });
  React.useEffect(() => {
    if (!tagKey) { setState({ phase: "ready", data: null }); return; }
    let alive = true;
    setState({ phase: "loading", data: null });
    tagRollupFetch<RollupTopData>("/api/rollup/top", {
      area, tag_key: tagKey, window: windowDays, top,
      workspace_ids: workspaceIds, env: envs,
      ...Api.tagParams(),
    })
      .then((body) => { if (alive) setState({ phase: "ready", data: body }); })
      .catch((err) => { if (alive) setState({ phase: "error", data: null, error: err.message || String(err) }); });
    return () => { alive = false; };
  }, [depKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

const ROLLUP_LEVEL_LABEL: Record<string, string> = { work: "query/job tag", compute: "compute tag", workspace: "workspace tag", account: "account", "billing line": "billing line" };

export function normalizeTagKeyLike(raw: unknown): string {
  return String(raw || "").toLowerCase().replace(/[ _-]+/g, "");
}

// Keys that mean the same tag, as in app/core/tag_compliance.py: env counts as environment and the reverse.
const SAME_TAG = [["environment", "env"]];
export function withSameTagKeys(normKeys: string[]): Set<string> {
  const out = new Set(normKeys);
  SAME_TAG.forEach((group) => { if (group.some((k) => out.has(k))) group.forEach((k) => out.add(k)); });
  return out;
}

// "Cost center" -> "cost center" mid-sentence; an acronym such as "BU" keeps its case.
export function tagLabelMid(label: string | null | undefined): string {
  const s = String(label || "tag");
  return /^[A-Z][a-z]/.test(s) ? s.charAt(0).toLowerCase() + s.slice(1) : s;
}

// The configured top tags (GET /api/meta top_tags, file order) that carry spend in this window, at
// most 4 -- or the first configured one, so its card can still say none was found.
export function useTopTags(meta: Meta | null, windowDays: number): { key: string; label: string }[] {
  const keysState = useRollupKeys("cost", windowDays);
  const configured = (meta && meta.top_tags) || [];
  return React.useMemo(() => {
    const have = new Set((keysState.keys || []).map((k) => normalizeTagKeyLike(k.tag_key)));
    const withSpend = configured.filter((t) => have.has(normalizeTagKeyLike(t.key))).slice(0, 4);
    return withSpend.length ? withSpend : configured.slice(0, 1);
  }, [keysState.keys, configured]);
}

// One button per top tag; nothing when there is only one to choose.
export function TopTagSwitch({ tags, value, onChange }: {
  tags: { key: string; label: string }[] | null; value: string | null; onChange: (key: string) => void;
}) {
  if (!tags || tags.length < 2) return null;
  return (
    <div className="ws-actions">
      {tags.map((t) => (
        <button key={t.key} type="button" className={t.key === value ? "on" : ""} onClick={() => onChange(t.key)}>{t.label}</button>
      ))}
    </div>
  );
}

// {total, untagged, items} from a ready rollup's by_value; zeros until then.
export function allocSplit(view: RollupState<RollupData> | null | undefined): { total: number; untagged: number; items: Entry[] } {
  const out: { total: number; untagged: number; items: Entry[] } = { total: 0, untagged: 0, items: [] };
  if (!view || view.phase !== "ready" || view.outcome !== "ok_rows" || !view.data) return out;
  out.total = numOrZero(view.data.total && view.data.total.usd);
  const rows = view.data.by_value || [];
  const untagged = rows.find((e) => e.value === "__untagged__");
  out.untagged = numOrZero(untagged && untagged.metrics && untagged.metrics.usd);
  out.items = rows.filter((e) => e.value !== "__untagged__")
    .map((e) => ({ name: e.label, value: numOrZero(e.metrics && e.metrics.usd) }));
  return out;
}

// The split's value bars plus one hatched bar for the untagged dollars, so the chart always shows both.
export function allocBars(split: { untagged: number; items: Entry[] }, untaggedLabel: string): Entry[] {
  return split.untagged > 0 ? [...split.items, { name: untaggedLabel, value: split.untagged, untagged: true }] : split.items;
}

// useTagAllocation -- one tag's spend split by value off GET /api/rollup's `by_value`, the
// one-dollar-basis partition that reconciles to `total` (by_tag_top's per-level rows overlap, so
// summing those would show bars that add up to more than the card's own total). Idle with no key.
export function useTagAllocation(filters: Filters, tagKey: string | null): RollupState<RollupData> {
  const depKey = JSON.stringify({
    tagKey, window: filters.window, workspaceIds: filters.workspaceIds, envs: filters.envs,
    tag: Api.tagFilterKey(),
  });
  const [state, setState] = React.useState<RollupState<RollupData>>({ phase: "loading", outcome: null, data: null, error: null });
  React.useEffect(() => {
    if (!tagKey) { setState({ phase: "idle", outcome: null, data: null, error: null }); return undefined; }
    let alive = true;
    setState({ phase: "loading", outcome: null, data: null, error: null });
    tagRollupFetch<RollupData>("/api/rollup", {
      area: "cost", tag_key: tagKey, window: filters.window,
      workspace_ids: filters.workspaceIds, env: filters.envs,
      ...Api.tagParams(),
    })
      .then((body) => { if (alive) setState({ phase: "ready", outcome: body.outcome, data: body, error: null }); })
      .catch((err) => { if (alive) setState({ phase: "error", outcome: null, data: null, error: err.message || String(err) }); });
    return () => { alive = false; };
  }, [depKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

// TagTopView -- a key picker over the generic "By tag, top 10 per level" panel.
function TagTopView({ area: areaProp, filters, top = 10 }: { area?: string; filters: Filters; top?: number }) {
  const area = areaProp || "cost";
  const keysState = useRollupKeys(area, filters.window);
  const [ownKey, setOwnKey] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (keysState.phase === "ready" && keysState.keys.length && !ownKey) setOwnKey(keysState.keys[0].tag_key);
  }, [keysState.phase, keysState.keys, ownKey]);

  const topState = useRollupTop(area, ownKey, filters.window, filters.workspaceIds, filters.envs, top);
  const primaryMetric = area === "cost" ? "usd" : "duration_ms";
  const fmtV = (v: unknown) => (area === "cost" ? fmtMoney(numOrZero(v), 0) : fmtInt(numOrZero(v)));

  // No tag keys at all: say so, never an empty picker.
  if (keysState.phase === "ready" && !keysState.keys.length) {
    return <div className="chart-note empty"><div className="chart-note-text">No tags on queries, warehouses, clusters or workspaces in this export, so there is nothing to split by. Tag warehouses and clusters, or set query tags, to see cost by team.</div></div>;
  }
  return (
    <div>
      <div className="row wrap" style={{ gap: 12, alignItems: "center", marginBottom: 8 }}>
        <select value={ownKey || ""} onChange={(e) => setOwnKey(e.target.value)} disabled={keysState.phase !== "ready" || !keysState.keys.length}>
          {keysState.phase === "ready" && keysState.keys.length
            ? keysState.keys.map((k) => <option key={k.tag_key} value={k.tag_key}>{k.display_key}</option>)
            : <option value="">
                {keysState.phase === "loading" ? "Loading tag keys..."
                  : keysState.phase === "error" ? "Could not load tag keys"
                  : "No tags found in this snapshot."}
              </option>}
        </select>
      </div>

      {topState.phase === "loading" && <ChartNote state={{ phase: "loading" }} label="rollup/top" />}
      {topState.phase === "error" && <ChartNote state={{ phase: "error" }} label="rollup/top" />}
      {topState.phase === "ready" && topState.data && topState.data.outcome === "not_assessed" && (
        <div className="chart-note not_assessed"><div className="chart-note-text">{topState.data.not_assessed_reason}</div></div>
      )}
      {topState.phase === "ready" && topState.data && topState.data.outcome === "ok_empty_window" && (
        <div className="chart-note ok"><div className="chart-note-text">No {area === "cost" ? "spend" : "statements"} carrying this key in this window.</div></div>
      )}
      {topState.phase === "ready" && topState.data && topState.data.outcome === "ok_empty_filters" && (
        <div className="chart-note ok"><div className="chart-note-text">No {area === "cost" ? "spend" : "statements"} carrying this key under the active filters.</div></div>
      )}
      {topState.phase === "ready" && topState.data && topState.data.outcome === "ok_rows" && (() => {
        const d = topState.data;
        const totalV = numOrZero(d.total && d.total[primaryMetric]);
        const taggedV = numOrZero(d.tagged && d.tagged[primaryMetric]);
        const untaggedV = numOrZero(d.untagged && d.untagged.metrics && d.untagged.metrics[primaryMetric]);
        return (
          <div>
            <div style={{ marginBottom: 8 }}>
              <div className="metric-label">Total</div>
              <div className="metric-value mono">{fmtV(totalV)}</div>
              <Facts items={[
                { label: "Tagged", value: fmtV(taggedV), detail: fmtPct(totalV ? (taggedV / totalV) * 100 : 0, 0) },
                { label: "Untagged", value: fmtV(untaggedV), detail: fmtPct(totalV ? (untaggedV / totalV) * 100 : 0, 0), tone: untaggedV > 0 ? "warn" : "ok" },
              ]} />
            </div>
            <div className="faint" style={{ fontSize: 11, marginBottom: 8 }}>Rows below are per level, never summed across levels.</div>
            {!d.rows.length ? (
              <div className="chart-note ok"><div className="chart-note-text">Every dollar in this window is untagged for this key.</div></div>
            ) : (
              <div className="tag-table-wrap">
                <table className="tag-rollup-table" style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                  <thead>
                    <tr className="faint">
                      <th style={{ textAlign: "left" }}>Value</th>
                      <th style={{ textAlign: "left" }}>Level</th>
                      <th style={{ textAlign: "right" }}>{area === "cost" ? "$" : primaryMetric}</th>
                      <th style={{ textAlign: "right" }}>Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.rows.map((r, i) => {
                      const v = numOrZero(r.metrics && r.metrics[primaryMetric]);
                      return (
                        <tr key={`${r.level}:${r.value}:${i}`}>
                          <td>{r.label}</td>
                          <td className="faint">{ROLLUP_LEVEL_LABEL[r.level] || r.level}</td>
                          <td className="mono" style={{ textAlign: "right" }}>{fmtV(v)}</td>
                          <td className="mono" style={{ textAlign: "right" }}>{fmtPct(totalV ? (v / totalV) * 100 : 0, 1)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {d.more_count > 0 && <div className="faint" style={{ marginTop: 6, fontSize: 11 }}>+{fmtInt(d.more_count)} more value(s) not shown, ranked lower.</div>}
          </div>
        );
      })()}
    </div>
  );
}

function tagRollupNodeName(dims: DimMaps | null, kind: string, id: string): string {
  if (!dims || !id) return id;
  if (kind === "warehouse") return warehouseName(dims, id);
  if (kind === "cluster") return clusterName(dims, id);
  return id;
}

function TagRollupTreeRow({ node, depth, area, primaryMetric, dims, wsMap, expanded, onToggle }: {
  node: RollupNode; depth: number; area: string; primaryMetric: string; dims: DimMaps | null;
  wsMap: Record<string, string> | null; expanded: Set<string>; onToggle: (id: string) => void;
}) {
  const isOpen = expanded.has(node.id);
  const hasChildren = node.children && node.children.length > 0;
  const value = area === "cost" ? node.metrics.usd : node.metrics[primaryMetric];
  const untagged = area === "cost" ? node.untagged.usd : node.untagged[primaryMetric];
  const valueTxt = area === "cost" ? fmtMoney(value, 0) : fmtInt(value);
  return (
    <React.Fragment>
      <div className="hbar-list-row" style={{ paddingLeft: depth * 16 }}>
        <div className="hbar-list-label" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {hasChildren ? (
            <button className="tag-rollup-toggle" onClick={() => onToggle(node.id)}
              style={{ background: "none", border: "none", cursor: "pointer", padding: 0, width: 14 }}>
              {isOpen ? "▾" : "▸"}
            </button>
          ) : <span style={{ display: "inline-block", width: 14 }} />}
          <span title={node.reason || ""}>{node.label}</span>
          {node.source_label && <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>({node.source_label})</span>}
          {untagged > 0 && (
            <span className="faint" style={{ marginLeft: 6, fontSize: 11 }}>
              untagged inside {area === "cost" ? fmtMoney(untagged, 0) : fmtInt(untagged)}
            </span>
          )}
        </div>
        <div className="hbar-list-value mono">
          {valueTxt}
          {node.share_of_parent != null && <span className="faint" style={{ marginLeft: 6 }}>{fmtPct(node.share_of_parent * 100, 0)} of parent</span>}
        </div>
      </div>
      {node.workspaces && isOpen && (
        <div style={{ paddingLeft: (depth + 1) * 16 }} className="faint">
          {node.workspaces.map((w) => (
            <div key={w.workspace_id} className="hbar-list-row">
              <div className="hbar-list-label">{(wsMap && wsMap[w.workspace_id]) || resolveName("workspace", w.workspace_id, w.workspace_id) || w.workspace_id}</div>
              <div className="hbar-list-value mono">{area === "cost" ? fmtMoney(w.metrics.usd, 0) : fmtInt(w.metrics[primaryMetric])}</div>
            </div>
          ))}
        </div>
      )}
      {node.resources && isOpen && (
        <div style={{ paddingLeft: (depth + 1) * 16 }} className="faint">
          {node.resources.map((r) => (
            <div key={r.compute_id} className="hbar-list-row">
              <div className="hbar-list-label">{tagRollupNodeName(dims, r.compute_kind, r.compute_id)}</div>
              <div className="hbar-list-value mono">{area === "cost" ? fmtMoney(r.metrics.usd, 0) : fmtInt(r.metrics[primaryMetric])}</div>
            </div>
          ))}
        </div>
      )}
      {hasChildren && isOpen && node.children!.map((c) => (
        <TagRollupTreeRow key={c.id} node={c} depth={depth + 1} area={area} primaryMetric={primaryMetric}
          dims={dims} wsMap={wsMap} expanded={expanded} onToggle={onToggle} />
      ))}
    </React.Fragment>
  );
}

function TagRollupByValueTable({ byValue, area, primaryMetric, selectedValue }: {
  byValue: RollupValue[] | null; area: string; primaryMetric: string; selectedValue: string | null;
}) {
  if (!byValue || !byValue.length) {
    return <div className="chart-note ok"><div className="chart-note-text">No values for this key in this window.</div></div>;
  }
  return (
    <div className="tag-table-wrap">
      <table className="tag-rollup-table" style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
        <thead>
          <tr className="faint">
            <th style={{ textAlign: "left" }}>Value</th>
            <th style={{ textAlign: "right" }}>{area === "cost" ? "$" : primaryMetric}</th>
            <th style={{ textAlign: "right" }}>Share</th>
            <th style={{ textAlign: "left" }}>Counted by</th>
            <th style={{ textAlign: "right" }}>Overrides outer</th>
          </tr>
        </thead>
        <tbody>
          {byValue.map((e) => {
            const v = area === "cost" ? e.metrics.usd : e.metrics[primaryMetric];
            const ov = area === "cost" ? e.overrides_outer.usd : e.overrides_outer[primaryMetric];
            const countedBy = ["work", "compute", "workspace", "account"]
              .filter((lvl) => (area === "cost" ? e.by_level[lvl].usd : e.by_level[lvl][primaryMetric]) > 0)
              .map((lvl) => ({ work: "query/job tag", compute: "compute tag", workspace: "workspace tag", account: "account" } as Record<string, string>)[lvl])
              .join(", ");
            const isSelected = selectedValue != null && String(e.value) === String(selectedValue);
            return (
              <tr key={e.value} style={isSelected ? { background: "var(--bg-hover, rgba(127,127,127,0.12))" } : undefined}>
                <td>{e.label}</td>
                <td className="mono" style={{ textAlign: "right" }}>{area === "cost" ? fmtMoney(v, 0) : fmtInt(v)}</td>
                <td className="mono" style={{ textAlign: "right" }}>{fmtPct(e.share_of_total * 100, 1)}</td>
                <td className="faint">{countedBy || "-"}</td>
                <td className="mono" style={{ textAlign: "right" }}>{ov > 0 ? (area === "cost" ? fmtMoney(ov, 0) : fmtInt(ov)) : "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function TagRollupView({ filters, area: areaProp, meta, workspaces, dims }: {
  filters: Filters; area?: string; meta: Meta | null; workspaces?: Workspace[] | null; dims: DimMaps | null; maxCat?: number;
}) {
  const [area, setArea] = React.useState(areaProp || "cost");
  React.useEffect(() => { if (areaProp) setArea(areaProp); }, [areaProp]);

  const keysState = useRollupKeys(area, filters.window);
  const fromFilter = (filters && filters.tag) as PreGroupsTag | null;
  const [ownKey, setOwnKey] = React.useState<string | null>(null);
  const chosenKey = (fromFilter && fromFilter.key) || ownKey;
  React.useEffect(() => {
    if (!fromFilter && keysState.phase === "ready" && keysState.keys.length && !ownKey) {
      setOwnKey(keysState.keys[0].tag_key);
    }
  }, [keysState.phase, keysState.keys, fromFilter, ownKey]);

  const data = useRollupData(area, chosenKey, filters.window, filters.workspaceIds, filters.envs,
    fromFilter ? fromFilter.value : null);

  const [expanded, setExpanded] = React.useState(() => new Set<string>(["account"]));
  const onToggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const rangeLabel = windowRangeLabel(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);
  const winFacts = windowFacts(meta && meta.as_of_date, filters.window, meta && meta.snapshot_days);
  const primaryMetric = area === "cost" ? "usd" : "statements";
  const wsMap = React.useMemo(() => {
    if (!workspaces) return null;
    const m: Record<string, string> = {};
    (Array.isArray(workspaces) ? workspaces : []).forEach((w) => { m[w.workspace_id] = w.name || w.workspace_id; });
    return m;
  }, [workspaces]);

  // No tag keys at all: say so, never an empty picker.
  if (keysState.phase === "ready" && !keysState.keys.length) {
    return <div className="chart-note empty"><div className="chart-note-text">No tags on queries, warehouses, clusters or workspaces in this export, so there is nothing to split by. Tag warehouses and clusters, or set query tags, to see cost by team.</div></div>;
  }
  return (
    <div>
      <div className="row wrap" style={{ gap: 12, alignItems: "center", marginBottom: 8 }}>
        {!fromFilter && (
          <select value={chosenKey || ""} onChange={(e) => setOwnKey(e.target.value)} disabled={keysState.phase !== "ready" || !keysState.keys.length}>
            {keysState.phase === "ready" && keysState.keys.length
              ? keysState.keys.map((k) => <option key={k.tag_key} value={k.tag_key}>{k.display_key}</option>)
              : <option value="">
                  {keysState.phase === "loading" ? "Loading tag keys..."
                    : keysState.phase === "error" ? "Could not load tag keys"
                    : "No tags found in this snapshot."}
                </option>}
          </select>
        )}
        {fromFilter && <div className="mono">{fromFilter.display_key || fromFilter.key}</div>}
        <div className="subtabs">
          <button className={area === "cost" ? "active" : ""} onClick={() => setArea("cost")}>Cost</button>
          <button className={area === "performance" ? "active" : ""} onClick={() => setArea("performance")}>Performance</button>
        </div>
        {rangeLabel && <div className="faint">{rangeLabel}</div>}
      </div>

      {data.phase === "loading" && <ChartNote state={{ phase: "loading" }} label="rollup" />}
      {data.phase === "error" && <ChartNote state={{ phase: "error" }} label="rollup" />}
      {data.phase === "ready" && data.data && data.data.outcome === "not_assessed" && (
        <div className="chart-note not_assessed"><div className="chart-note-text">{data.data.not_assessed_reason}</div></div>
      )}
      {data.phase === "ready" && data.data && data.data.outcome === "ok_empty_window" && (
        <div className="chart-note ok"><div className="chart-note-text">No {area === "cost" ? "spend" : "statements"} carrying this key in this window.</div></div>
      )}
      {data.phase === "ready" && data.data && data.data.outcome === "ok_empty_filters" && (
        <div className="chart-note ok"><div className="chart-note-text">No {area === "cost" ? "spend" : "statements"} carrying this key under the active filters.</div></div>
      )}
      {data.phase === "ready" && data.data && data.data.outcome === "ok_rows" && (
        <div>
          <div className="chart-card" style={{ marginBottom: 10 }}>
            {area === "cost" ? (() => {
              const untagged = data.data.by_value.find((e) => e.value === "__untagged__");
              const u = untagged ? untagged.metrics.usd : 0;
              const pct = data.data.total.usd ? (u / data.data.total.usd) * 100 : 0;
              const recon = data.data.reconciliation;
              return (
                <div>
                  <div className="metric-label">Total</div>
                  <div className="metric-value mono">{fmtMoney(data.data.total.usd, 0)}</div>
                  <Facts items={[
                    { label: "Basis", value: estLabel(0) },
                    ...winFacts,
                    { label: "Untagged", value: fmtMoney(u, 0), detail: fmtPct(pct, 0), tone: u > 0 ? "warn" : "ok" },
                    recon.reconciled
                      ? { label: "Reconciles", value: `${fmtMoney(recon.billing_usd, 0)} = ${fmtMoney(recon.rollup_usd, 0)}`, tone: "ok" }
                      : { label: "Reconciles", value: `${fmtMoney(recon.billing_usd, 0)} vs ${fmtMoney(recon.rollup_usd, 0)}`, detail: "not reconciled", tone: "crit" },
                  ].filter(Boolean)} />
                  {data.data.unpriced && data.data.unpriced.length > 0 && (
                    <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>
                      Unpriced: {data.data.unpriced.map((up) => (up.usage_unit === "DBU" ? fmtDbu(up.quantity, 0) : `${fmtInt(up.quantity)} ${String(up.usage_unit || "").toLowerCase()}`)).join(", ")}.
                      {recon.scaled_warehouse_days > 0 && ` ${recon.scaled_warehouse_days} warehouse-day(s) scaled down to the bill.`}
                    </div>
                  )}
                </div>
              );
            })() : (() => {
              const total = data.data.total;
              const avgMs = total.statements ? total.duration_ms / total.statements : 0;
              const queueSharePct = total.duration_ms ? (total.queue_ms / (total.duration_ms + total.queue_ms)) * 100 : 0;
              return (
                <div>
                  <div className="metric-label">Statements</div>
                  <div className="metric-value mono">{fmtInt(total.statements)}</div>
                  <Facts items={[
                    { label: "Failed", value: fmtInt(total.failed_statements), tone: total.failed_statements > 0 ? "warn" : "ok" },
                    { label: "Avg duration", value: fmtDurationMs(avgMs) },
                    { label: "Queue share", value: fmtPct(queueSharePct, 0), detail: "of run time spent queued" },
                    ...winFacts,
                  ]} />
                </div>
              );
            })()}
            {data.data.scope.workspace_filtered && (
              <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>
                Account-level usage outside the workspace filter is excluded from this total
                {area === "cost" ? ` (${fmtMoney(data.data.scope.excluded_account_level.usd, 0)}).` : "."}
              </div>
            )}
            {(data.data.source_status || []).filter((s) => s.effect).map((s) => (
              <div key={s.source} className="faint" style={{ fontSize: 11, marginTop: 4 }}>{s.effect}</div>
            ))}
            <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>
              All-purpose clusters stay at the cluster tag; no per-query dollars are estimated.
            </div>
          </div>

          <div className="chart-card-title">By value</div>
          <TagRollupByValueTable byValue={data.data.by_value} area={area} primaryMetric={primaryMetric}
            selectedValue={data.data.selected ? data.data.selected.value : null} />

          <div className="chart-card-title" style={{ marginTop: 14 }}>Tree</div>
          <div className="hbar-list">
            <TagRollupTreeRow node={data.data.tree} depth={0} area={area} primaryMetric={primaryMetric}
              dims={dims} wsMap={wsMap} expanded={expanded} onToggle={onToggle} />
          </div>
        </div>
      )}
    </div>
  );
}
