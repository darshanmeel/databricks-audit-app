// A thin fetch wrapper over app/api's JSON endpoints.
import type { AggregateData, Dims, FindingData, Meta, TagFilter, TagOrigins, TagsAnswer, Workspace, WorkspaceTagsAnswer } from "./types";
import type { GuideData } from "./components/guide";

type Params = Record<string, unknown>;

// Days a refresh can pull.
export const REFRESH_DAYS = [7, 15, 30, 60, 90];

function qs(params?: Params): string {
  const parts: string[] = [];
  Object.keys(params || {}).forEach((key) => {
    const value = params![key];
    if (value === undefined || value === null || value === "") return;
    if (Array.isArray(value)) {
      value.forEach((v) => parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(v))}`));
    } else {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
  });
  return parts.length ? `?${parts.join("&")}` : "";
}

async function getJson<T = any>(path: string, params?: Params): Promise<T> {
  const res = await fetch(path + qs(params), { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (e) { /* body was not JSON */ }
    const err: Error & { status?: number } = new Error(`${res.status} ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// P4-T-IDX (tasks/P4-T-SPEC.md section 4.6): the tag filter is module-level state, not a param
// every caller threads through by hand -- App calls Api.setTagFilter(tagFilter) at the top of its
// own render, before any child (and so before any of the calls below) runs. `filters.tag` shape:
// null, or {groups: [{key, display_key, values: [..]}]} (empty values = any value of that name).
let _tagFilter: TagFilter | null = null;

function setTagFilter(tag: TagFilter | null) {
  _tagFilter = tag || null;
}

function tagFilterKey(): string {
  return _tagFilter ? JSON.stringify(_tagFilter.groups.map((g) => [g.key, g.values])) : "";
}

// One repeated `tag` param per value ("key:value"), or "key" alone for any value; the server
// unquotes the key, so a ':' or '%' inside it is escaped.
function _tagParams(): { tag?: string[] } {
  if (!_tagFilter) return {};
  const enc = (k: string) => k.replace(/%/g, "%25").replace(/:/g, "%3A");
  return { tag: _tagFilter.groups.flatMap((g) => (g.values.length ? g.values.map((v) => `${enc(g.key)}:${v}`) : [enc(g.key)])) };
}

/** aggregate() options: status scope, top-N fold, below-floor rows, extra tag conditions. */
export interface AggOptions {
  statuses?: string[] | null;
  top?: number | null;
  belowFloor?: boolean;
  extraTags?: string[] | null;
}

export const Api = {
  // P2-NOBUILD: the one call App.tsx makes before anything else -- {state, settings_error,
  // database}, see app/api/app.py's own GET /api/status docstring for the four `state` values.
  status: () => getJson("/api/status"),
  meta: () => getJson<Meta>("/api/meta"),
  workspaces: () => getJson<Workspace[]>("/api/workspaces"),
  // The Settings panel's own cost-center picker: {keys, default, suggestions}. PUT validates
  // 1-5 non-empty keys and writes config/settings.local.yml -- picked up next request, no restart.
  dims: () => getJson<Dims>("/api/dims"),
  coverage: () => getJson("/api/coverage"),
  refreshStatus: () => getJson("/api/refresh"),
  refreshStart: (days: number) => fetch(`/api/refresh${qs({ days })}`, { method: "POST" }).then((r) => r.json()),
  // the Guide tab's one feed (app/api/guide.py's build_guide())
  guide: () => getJson<GuideData>("/api/guide"),
  guideSql: (queryId: string, window_: number) => getJson<{ sql: string }>(`/api/guide/sql/${encodeURIComponent(queryId)}`, { window: window_ }),
  setTagFilter,
  tagFilterKey,
  tagParams: _tagParams,
  // P4-T-IDX: GET /api/tags -- the tag search picker's own feed. `tagKey` (optional) restricts to
  // one key's "all values".
  tags: (search: string, tagKey?: string | null, limitKeys?: number, limitValues?: number) => getJson<TagsAnswer>("/api/tags", { search, tag_key: tagKey, limit_keys: limitKeys, limit_values: limitValues }),
  // P4-T-IDX: the stored tag filter (Api.setTagFilter) rides along automatically, same as
  // `finding`/`aggregate` below -- the findings table's own row/status counts and
  // tag_applied/tag_chain_label now reflect the active tag filter (section 5.5).
  tagOrigins: (window_: number, workspaceIds: string[] | null, envs: string[] | null) =>
    getJson<TagOrigins>("/api/tag_origins", { window: window_, workspace_ids: workspaceIds, env: envs, ..._tagParams() }),
  tagCompliance: (window_: number, workspaceIds: string[] | null, envs: string[] | null) => getJson("/api/tag_compliance", { window: window_, workspace_ids: workspaceIds, env: envs }),
  abacPolicies: () => getJson("/api/abac_policies"),
  workspaceTags: (workspaceIds: string[] | null, envs: string[] | null) => getJson<WorkspaceTagsAnswer>("/api/workspace_tags", { workspace_ids: workspaceIds, env: envs }),
  findings: (window_: number, workspaceIds: string[] | null, envs: string[] | null) =>
    getJson("/api/findings", {
      window: window_, workspace_ids: workspaceIds, env: envs, ..._tagParams(),
    }),
  // jobId (T-70) is optional and scopes the finding to one job server-side, on a finding that
  // carries a job_id column -- ignored, never an error, on one that doesn't
  // (app/core/data._build_filters). The job focus panel is the only caller that passes it, and a
  // job-scoped call is already one job -- the tag filter would only ever narrow it further
  // in a way the drawer never shows, so it leaves it off (a bare job_id fetch stayed unfiltered
  // under an active tag filter otherwise).
  // statuses (T-68) is optional and scopes to those status values server-side, on a finding that
  // carries a status column -- so a caller that only needs flagged rows (e.g. a tile summing a
  // column across CRITICAL/WARN rows) fetches only those instead of an unfiltered, capped page.
  // P4-T-IDX: the stored tag filter (Api.setTagFilter) rides along automatically as tag_key/
  // tag_value -- no caller of `finding` needs to pass it itself.
  finding: (queryId: string, window_: number, workspaceIds: string[] | null, envs: string[] | null,
    limit?: number, offset?: number, jobId?: string | null, statuses?: string[] | null, columns?: string[] | null) =>
    getJson<FindingData>(`/api/finding/${encodeURIComponent(queryId)}`, {
      window: window_,
      workspace_ids: workspaceIds,
      env: envs,
      limit,
      offset,
      job_id: jobId,
      status: statuses,
      columns,
      ...(jobId ? {} : _tagParams()),
    }),
  // T-68: a server-side GROUP BY/SUM (or COUNT/COUNT DISTINCT) over EVERY row matching the
  // filters -- no ui.max_rows cap before the aggregation -- for a Cost/ML & AI/Overview tile or
  // chart that used to derive its number by summing the capped row page `finding` above returns.
  // `group` is an array of 0-2 real column names; `opts` is {statuses, top, belowFloor}.
  aggregate: (queryId: string, window_: number, workspaceIds: string[] | null, envs: string[] | null,
    group: string[], agg: string, value?: string | null, opts?: AggOptions) => {
    const o = opts || {};
    const extra = o.extraTags;
    return getJson<AggregateData>(`/api/finding/${encodeURIComponent(queryId)}/aggregate`, {
      window: window_,
      workspace_ids: workspaceIds,
      env: envs,
      group,
      agg,
      value,
      status: o.statuses,
      top: o.top,
      below_floor: o.belowFloor,
      ..._tagParams(),
      // Extra tag conditions, e.g. "costcenter:__untagged__": they replace the Tag filter's own
      // condition on the same key (values of one key are OR'ed) and AND with its other keys.
      ...(extra ? { tag: [
        ...(_tagParams().tag || []).filter((t) => !extra.some((x) => t.split(":")[0] === x.split(":")[0])),
        ...extra,
      ] } : {}),
    });
  },
};

