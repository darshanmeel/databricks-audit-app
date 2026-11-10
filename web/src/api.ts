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

// The tag filter is module state: App sets it before any child calls below.
// Shape: null, or {groups: [{key, display_key, values}]} (empty values = any value).
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
  noTagFilter?: boolean;  // account-wide figures (the commit) ignore the top-bar Tag filter
}

export const Api = {
  // The first call App makes: {state, settings_error, database}.
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
  // GET /api/tags, the tag picker's feed; `tagKey` limits it to one key's values.
  tags: (search: string, tagKey?: string | null, limitKeys?: number, limitValues?: number) => getJson<TagsAnswer>("/api/tags", { search, tag_key: tagKey, limit_keys: limitKeys, limit_values: limitValues }),
  // The stored tag filter rides along, so the table's counts follow it.
  tagOrigins: (window_: number, workspaceIds: string[] | null, envs: string[] | null) =>
    getJson<TagOrigins>("/api/tag_origins", { window: window_, workspace_ids: workspaceIds, env: envs, ..._tagParams() }),
  tagCompliance: (window_: number, workspaceIds: string[] | null, envs: string[] | null) => getJson("/api/tag_compliance", { window: window_, workspace_ids: workspaceIds, env: envs, ..._tagParams() }),
  abacPolicies: () => getJson("/api/abac_policies"),
  tableSizes: () => getJson("/api/table_sizes"),
  tables: (workspaceIds: string[] | null, envs: string[] | null, window_?: number) => getJson("/api/tables", { workspace_ids: workspaceIds, env: envs, ...(window_ ? { window: window_ } : {}), ..._tagParams() }),
  table: (name: string, window_?: number) => getJson("/api/table", { name, ...(window_ ? { window: window_ } : {}) }),
  people: (window_: number, workspaceIds: string[] | null, envs: string[] | null) => getJson("/api/people", { window: window_, workspace_ids: workspaceIds, env: envs }),
  volumes: (workspaceIds: string[] | null, envs: string[] | null, window_ = 30) => getJson("/api/volumes", { window: window_, workspace_ids: workspaceIds, env: envs, ..._tagParams() }),
  volumeUsers: (volume: string, window_ = 30) => getJson("/api/volume_users", { volume, window: window_ }),
  warehouses: (window_: number, workspaceIds: string[] | null, envs: string[] | null) => getJson("/api/warehouses", { window: window_, workspace_ids: workspaceIds, env: envs }),
  warehouse: (warehouseId: string, window_: number) => getJson("/api/warehouse", { warehouse_id: warehouseId, window: window_ }),
  workspaceSchemas: (workspaceId: string, window: number) => getJson("/api/workspace_schemas", { ...(workspaceId ? { workspace_id: workspaceId } : {}), window }),
  activity: (window_: number, workspaceIds: string[] | null, envs: string[] | null, workspaceId?: string | null) => getJson("/api/activity", { window: window_, workspace_ids: workspaceIds, env: envs, ...(workspaceId ? { workspace_id: workspaceId } : {}) }),
  personTables: (identity: string, window: number) => getJson("/api/person_tables", { identity, window }),
  workspaceUsage: (window_: number, workspaceIds: string[] | null, envs: string[] | null) => getJson("/api/workspace_usage", { window: window_, workspace_ids: workspaceIds, env: envs, ..._tagParams() }),
  workspaceTags: (workspaceIds: string[] | null, envs: string[] | null) => getJson<WorkspaceTagsAnswer>("/api/workspace_tags", { workspace_ids: workspaceIds, env: envs, ..._tagParams() }),
  findings: (window_: number, workspaceIds: string[] | null, envs: string[] | null) =>
    getJson("/api/findings", {
      window: window_, workspace_ids: workspaceIds, env: envs, ..._tagParams(),
    }),
  // jobId scopes to one job on a check with a job_id column; the job panel leaves the tag filter off.
  // statuses scopes to those status values server-side. The stored tag filter rides along otherwise.
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
  // A server-side GROUP BY over every matching row (no row cap); `group` is 0-2 columns, `opts` {statuses, top, belowFloor}.
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
      ...(o.noTagFilter ? {} : _tagParams()),
      // Extra tag conditions, e.g. "costcenter:__untagged__": they replace the Tag filter's own
      // condition on the same key (values of one key are OR'ed) and AND with its other keys.
      ...(extra ? { tag: [
        ...(_tagParams().tag || []).filter((t) => !extra.some((x) => t.split(":")[0] === x.split(":")[0])),
        ...extra,
      ] } : {}),
    });
  },
};

