// Shapes of the app's API answers (app/api/app.py) and of the state the data hooks hand to pages.
import type React from "react";

export type Outcome = "ok_rows" | "ok_empty_window" | "ok_empty_filters" | "not_assessed" | "error";
export type Status = "CRITICAL" | "WARN" | "OK" | "NOT_ASSESSED";
export type StatusCounts = Partial<Record<Status, number>>;

/** One result row. Columns differ per check, so values stay loosely typed. */
export type Row = Record<string, any>;

export interface Column {
  name: string;
  kind: string | null;
  label: string | null;
}

export interface NextCheck {
  query_id: string;
  if: string;
}

export interface LibraryCorrection {
  id: string;
  query_id: string;
  problem: string;
  effect: string;
  status: string;
  fix: string;
  item: string;
}

export interface RegionGap {
  workspace_id: string;
  name: string | null;
  billed: boolean;
  reason: string;
}

export interface TagScope {
  applied: boolean;
  chain: string[];
  chain_label: string | null;
  reason: string | null;
}

export interface Scope {
  has_workspace_id: boolean;
  regional: boolean;
  region_gap: RegionGap[];
  tag: TagScope | null;
}

export interface WindowCoverage {
  query_id: string;
  window_days: number;
  as_of_date: string | null;
  partial: boolean;
  covered_days: number;
  sources: Record<string, { state: string | null; partial: boolean; [extra: string]: unknown }>;
}

export interface Floor {
  column: string;
  min: number;
  unit: string;
  label: string;
  rows_below: number;
  per_days?: number;
}

export interface Header {
  title: string;
  domain: string;
  tier: string;
  stars: boolean;
  origin: string;
  confidence: string;
  confidence_note: string;
  read_this: string;
  healthy: string;
  investigate_if: string;
  actions: string[];
  not_assessed_reasons: Record<string, string>;
  params: Record<string, any>;
  next: NextCheck[];
  caveats: string;
  empty_if: string[];
}

/** A source a check reads that is missing, name-lookup-only or only partly exported. */
export interface SourceIssue {
  source: string;
  state?: string;
  reason: string | null;
  message: string | null;
  /** The <x>_name column left NULL when a name-lookup source is missing. */
  degrades?: string;
  missing_days?: string[];
}

/** Why a check did not run (present when it was not assessed). */
export interface StatusInfo {
  status?: string;
  error_class?: string | null;
  message?: string | null;
  blocking_sources?: SourceIssue[];
  degraded_sources?: SourceIssue[];
  partial_sources?: SourceIssue[];
  not_built_reason?: string;
  outside_region?: { label: string; workspaces: RegionGap[] };
  [extra: string]: unknown;
}

export interface Metastore {
  cloud?: string | null;
  region?: string | null;
  [extra: string]: unknown;
}

/** What a filter left out because it has no workspace (account-level spend). */
export interface ExcludedNoWorkspace {
  rows: number;
  value: number | null;
}

/** GET /api/finding/{id}: one check's rows for a window and filters. */
export interface FindingData {
  query_id: string;
  outcome: Outcome;
  window_days: number;
  requested_window_days: number;
  is_finding: boolean;
  windowed: boolean;
  attributes: Record<string, string[]>;
  tag: Record<string, any> | null;
  scope: Scope;
  status: string[];
  library_corrections: LibraryCorrection[];
  degraded_sources: SourceIssue[];
  partial_sources: SourceIssue[];
  header: Header;
  rows_total: number;
  rows_in_window: number;
  limit: number;
  offset: number;
  returned: number;
  columns: Column[];
  rows: Row[];
  floor: Floor | null;
  excluded_no_workspace: ExcludedNoWorkspace | null;
  order_by: string | null;
  discount_pct: number;
  row_cap: number | null;
  window_coverage: WindowCoverage;
  truncated: boolean;
  truncated_max_rows: number | null;
  not_assessed_reason?: string | null;
  status_info?: StatusInfo;
  error?: string;
}

/** One value of an aggregate's group-by column, as the server returns it. */
export type AggKey = string | number | boolean | null;

/** An id as rows and aggregates carry it, or missing. */
export type Id = AggKey | undefined;

export interface AggGroup {
  key: AggKey[];
  value: number;
  row_count: number;
  null_rows: number;
}

/** GET /api/finding/{id}/aggregate: a server-side sum or count over every matching row. */
export interface AggregateData {
  query_id: string;
  outcome: Outcome;
  window_days: number;
  requested_window_days: number;
  group: string[];
  agg: string;
  value: string;
  status: string[];
  tag: Record<string, any> | null;
  scope: Scope;
  library_corrections: LibraryCorrection[];
  degraded_sources: SourceIssue[];
  partial_sources: SourceIssue[];
  groups: AggGroup[];
  other: AggGroup | null;
  total_value: number;
  group_count: number;
  rows_total: number;
  matched_rows: number;
  floor: Floor | null;
  excluded_no_workspace: ExcludedNoWorkspace | null;
  null_value_rows: number;
  rows_in_window: number;
  discount_pct: number;
  window_coverage: WindowCoverage;
  truncated: boolean;
  truncated_max_rows: number | null;
  not_assessed_reason?: string | null;
  status_info?: StatusInfo;
  error?: string;
}

/** A fetch in flight, failed, or answered. A chart draws only from ready + ok_rows. */
export type FetchState<T> =
  | { phase: "loading"; outcome: null; data: null; error: null }
  | { phase: "error"; outcome: null; data: null; error: string }
  | { phase: "ready"; outcome: Outcome; data: T; error: null };

export type FindingState = FetchState<FindingData>;
export type AggState = FetchState<AggregateData>;

export interface Affected {
  column: string;
  noun: string;
  flagged: number;
  total: number;
}

export interface Money {
  column: string;
  kind: string;
  usd: number;
}

/** One entry of GET /api/findings: a check's verdict without its rows. */
export interface FindingSummary {
  query_id: string;
  title: string;
  domain: string;
  tier: string;
  stars: boolean;
  origin: string;
  is_finding: boolean;
  windowed: boolean;
  window_days: number;
  outcome: Outcome;
  row_count: number;
  status_counts: StatusCounts | null;
  has_workspace_id: boolean;
  regional: boolean;
  not_assessed_reason: string | null;
  partial: boolean;
  tag_applied: boolean | null;
  tag_chain: string[] | null;
  tag_chain_label: string | null;
  library_corrections: LibraryCorrection[];
  severity_cap: Status | null;
  not_assessed: { code: string | null; label: string | null; partial: boolean } | null;
  money: Money | null;
  affected: Affected | null;
}

export interface Brand {
  name: string;
  url: string;
  contact_name: string;
  contact_email: string;
}

export interface WindowInfo {
  days: number;
  available: boolean;
  covered_days: number;
  partial: boolean;
}

/** GET /api/meta: what this database holds and how the app is set up. */
export interface Meta {
  as_of: string | null;
  as_of_date: string | null;
  snapshot_available: boolean;
  snapshot_days: number | null;
  snapshot_billing_days: number | null;
  metastore: Metastore | null;
  window_options: number[];
  default_window: number;
  windows: WindowInfo[];
  open_window: number;
  discount_pct: number;
  brand: Brand;
  mask_user_identities: boolean;
  mandatory_tag_keys: string[];
  attribute_keys_populated: string[];
  top_tags: { key: string; label: string }[];
  max_chart_categories: number;
  run_results_available: boolean;
  generated_at: string | null;
  cost_cutoff: { data_through: string; partial_day: string; partial_until: string } | null;
  direct_export: Record<string, any> | null;
  db_path: string;
  models: Record<string, number>;
}

export interface Workspace {
  workspace_id: string;
  name: string | null;
  url: string | null;
  env: string;
  env_source: string;
  env_reason: string;
  region_status: string;
  region_reason: string | null;
  [attribute: string]: string | number | null;
}

export interface DimJob { workspace_id: string; job_id: string; name: string; run_as: string | null; run_as_user_name: string | null; creator_user_name: string | null }
export interface DimCluster { workspace_id: string; cluster_id: string; cluster_name: string; cluster_source: string; owned_by: string | null }
export interface DimWarehouse { workspace_id: string; warehouse_id: string; warehouse_name: string; created_by: string | null }
export interface DimPipeline { workspace_id: string; pipeline_id: string; pipeline_name: string; created_by: string | null; run_as: string | null }
export interface DimNotebook { workspace_id: string; notebook_id: string; notebook_path: string }

/** GET /api/dims. */
export interface Dims {
  jobs: DimJob[];
  clusters: DimCluster[];
  warehouses: DimWarehouse[];
  pipelines: DimPipeline[];
  notebooks: DimNotebook[];
}

/** useDims(): the dims as lookups by id ("workspace:job" for jobs and pipelines). */
export interface DimMaps {
  jobs: Map<string, DimJob>;
  clusters: Map<string, DimCluster>;
  warehouses: Map<string, DimWarehouse>;
  pipelines: Map<string, DimPipeline>;
}

export interface TagGroup {
  key: string;
  display_key: string;
  values: string[];
  /** Every value ticked, untagged too: the name is picked but narrows nothing yet. */
  all?: boolean;
  /** Picker draft only: every value unticked, so the ones ticked next are the ones kept. */
  none?: boolean;
}

export interface TagFilter {
  groups: TagGroup[];
}

/** The filters every page reads: window, workspaces, environments and the tag filter. */
export interface Filters {
  window: number;
  /** Empty means every workspace. */
  workspaceIds: string[];
  envs: string[];
  workspaceIdSet: Set<string>;
  tag: TagFilter | null;
}

/** One fact under a tile or KPI: label, value, and an optional detail and tone. */
export interface Fact {
  label: React.ReactNode;
  value: React.ReactNode;
  detail?: React.ReactNode;
  tone?: string;
  title?: string;
}

/** GET /api/tag_origins: where the dollars a tag filter keeps got their tag. */
export interface TagOrigins {
  window_days: number;
  usd: number;
  keys: { tag_key: string; values: string[]; origins: { label: string; usd: number }[] }[];
}

/** One tag name in GET /api/tags, with its values when asked for. */
export interface TagKeyEntry {
  tag_key: string;
  display_key?: string;
  value_count: number;
  values?: { tag_value: string; object_count: number }[];
  values_truncated?: boolean;
}

/** GET /api/tags: tag names (and values) matching a search. */
export interface TagsAnswer {
  search: string;
  truncated: boolean;
  keys: TagKeyEntry[];
  source_labels: Record<string, string>;
}

/** One tag key on one workspace's bill, as GET /api/workspace_tags reads it. */
export interface WorkspaceTagRow {
  workspace_id: string | null;
  tag_key: string;
  norm_key: string;
  tag_value: string;
  top_value: string;
  is_valid: boolean;
  share: number;
  coverage: number;
}

/** GET /api/workspace_tags. */
export interface WorkspaceTagsAnswer {
  outcome: string;
  rows: WorkspaceTagRow[];
  coverage_floor: number;
  share_floor: number;
}

/** A plain chart entry. */
export interface Entry {
  name: string;
  value: number;
  color?: string;
  [extra: string]: any;
}
