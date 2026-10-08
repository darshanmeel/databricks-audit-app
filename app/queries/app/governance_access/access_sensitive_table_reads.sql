-- query_id: access_sensitive_table_reads
-- title: Who reads sensitive (PII-tagged or classified) tables, and how often
-- domain: governance_access   tier: standard
-- reads: system.access.table_lineage, system.information_schema.table_tags,
--   system.information_schema.column_tags, system.information_schema.tables,
--   system.data_classification.results
-- requires: SELECT on system.access, system.information_schema and system.data_classification;
--   system.access.table_lineage is GA; system.information_schema requires Unity Catalog;
--   system.data_classification.results is Public Preview and needs BOTH the data-classification
--   feature AND the system.data_classification schema enabled -- OPTIONAL for this query: it only
--   widens which tables count as sensitive (see caveats: SENSITIVE); a table_tags or column_tags
--   row alone is enough, so this finding still judges reads on every table those two catch even
--   when system.data_classification.results is absent from the snapshot
-- empty_if: schema_not_enabled, preview_unavailable, privilege_scoped, lineage_inference_only,
--   retention_window, no_activity
-- params: :period_days (default 30) rolling window in days; :warn_reads (default 20) reads by
--   one non-owner reader on one sensitive table in the window, above which that reader flags WARN
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Reuses the exact sensitivity-tag regex
--   access_pii_outside_tables already applies to volume/schema tags ('(?i)(pii|sensitiv|
--   confidential|gdpr|personal|secret|restricted)'), applied here to table_tags and column_tags,
--   plus any table with a system.data_classification.results row (class_tag IS NOT NULL). Confirm
--   on your account: a table you know is tagged sensitive appears with the right sensitivity_basis,
--   and a reader you know is not that table's owner and reads it often shows WARN.
-- read_this: One row = a workspace x sensitive table x reader pair with at least one read from
--   that workspace in the window (read as: this masked person or service principal read this
--   table this many times FROM THAT WORKSPACE). The columns that matter are read_count (how
--   often), reader vs table_owner (is this even the table's own owner), and status. A row with
--   every identifying column blank and status NOT_ASSESSED means the account has no sensitivity
--   signal at all yet -- see not_assessed_reason; that row carries no workspace_id, so it drops
--   out whenever a workspace filter is applied (it is not tied to any one workspace).
-- healthy: status = OK - the reader IS the table's owner, or read_count is at/below :warn_reads -
--   field heuristic; a table's own owner reading their own table is never flagged, regardless of
--   volume.
-- investigate_if: status = WARN - a reader who is NOT the table's owner read a sensitive table
--   more than :warn_reads times in the window (field heuristic; tune :warn_reads for your
--   account). status = NOT_ASSESSED - read not_assessed_reason: the account has no sensitivity
--   tag or classification result at all, so nothing here could be judged sensitive in the first
--   place.
-- actions: 1) confirm the reader's access is expected (a support ticket, an approved analysis) by
--   asking them or checking access_grants_inventory for how they can see the table (free); 2) if
--   the access is broader than needed, narrow the grant or route it through a masked/aggregated
--   view instead of the raw table (config); 3) if many people need the same sensitive table
--   regularly, formalize it as a governed, access-reviewed data product with a named owner instead
--   of ad hoc reads (spend/eng time).
-- next: access_data_classification_inventory (the classified-column inventory this reads from),
--   access_classified_unmasked (whether those same columns are even masked),
--   access_pii_outside_tables (the file/schema equivalent of this table-level view),
--   access_grants_inventory (what a flagged reader can see and why)
-- not_assessed_reasons: no_sensitivity_signal: nothing in the account is tagged or classified as
--   sensitive yet, so no read could be judged
-- caveats: SOURCE - this reads system.access.table_lineage (GA), not system.access.audit: a
--   lineage row whose source_table_name is populated proves that table was read FROM, regardless
--   of whether the same statement also wrote somewhere (a READ_WRITE edge still counts as a read
--   of its source); table_lineage carries entity_type/entity_id and created_by, so it actually
--   names the reader. system.access.audit is not used: its Unity Catalog getTable/getTableInfoV2
--   events are metadata lookups (they also fire on browse/describe, not only on data reads) and
--   carry the table name only inside request_params, so they cannot count data reads per table.
--   Only DIRECT reads count here (direct_access IS NULL OR direct_access = true; NULL keeps rows
--   written before the column existed) -- a read through a view is recorded against the view, not
--   against the raw table(s) it selects from, so tag or classify a view that exposes sensitive
--   columns unmasked and its readers will show up here under the view's own name, never folded
--   into the base table's count. Column lineage is inference-only and misses unsupported paths
--   (spark-submit/runs-submit jobs, RDDs, JDBC, UDFs, path-based access, literal-only writes), so
--   a real reader can be invisible here -- absence is a coverage gap, never proof nobody read the
--   table. SENSITIVE - a table counts as sensitive when ANY of: its own table_tags row matches the
--   sensitivity regex, ANY of its
--   columns' column_tags rows match the same regex, or it has ANY row (for any column) in
--   system.data_classification.results with class_tag NOT NULL; sensitivity_basis lists which of
--   the three applied, comma-joined. system.data_classification.results only ever WIDENS this set
--   -- table_tags and column_tags alone are enough to judge every row this query can ever emit, so
--   an account (or a snapshot) without that Public Preview table still gets a real, non-degraded
--   result from tags alone; it never blocks or narrows what table_tags/column_tags already caught.
--   The regex is a heuristic tuned to under-detect
--   ('(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)') -- tune it in the SQL to
--   your tag vocabulary. OWNER - is-owner is judged by a case-insensitive, trimmed match of the
--   RAW read event's created_by against system.information_schema.tables.table_owner, done BEFORE
--   either identity is masked for display; a table absent from information_schema.tables (dropped,
--   or outside the inventory) has table_owner NULL, and a NULL owner never matches, so such a
--   table's reads are judged as non-owner reads (the safer direction). table_owner is often a
--   group (Unity Catalog tables are commonly group-owned); a reader is matched only against the
--   owner name itself, never against group membership, so on a group-owned table every reader --
--   including members of that owning group -- is judged a non-owner and can show WARN; check the
--   owning group in access_grants_inventory before acting on a WARN row here. PRIVACY - reader and
--   table_owner are masked to DEC-66.3's identity format only when privacy.mask_user_identities is
--   on (off by default, so both are raw): a service-principal GUID passes through unchanged;
--   NULL/'__REDACTED__' pass through unchanged; otherwise the first 8 hex chars of
--   sha2(lower(trim(x)), 256) + ' ' + the first 2 chars of the raw value + '***' --
--   table_lineage carries no per-row real user id, so every masked identity here is the hash-
--   derived form, never a real user id passthrough. NOT_ASSESSED - the single all-blank,
--   NOT_ASSESSED, not_assessed_reason='no_sensitivity_signal' row is emitted only when the account
--   has ZERO rows across all three sensitivity sources above; once even one table is tagged or
--   classified, that sentinel disappears and ordinary rows (or, with no reads yet, no rows at all
--   -- DEC-57 / empty_if) take over. A sensitive table with zero reads in the window has no row
--   here by design, same as every other verdict-only query in this library -- pair with
--   access_data_classification_inventory / access_tags_inventory for full sensitive-table
--   coverage regardless of read activity. information_schema is privilege-aware, so a tag or table
--   this principal cannot see is invisible, not proven absent. Retention on system.access.* is
--   workspace-configurable; widen :period_days where your retention allows it. WORKSPACE_ID:
--   added to table_reads/reads_agg's own SELECT and GROUP BY, straight from table_lineage's own
--   workspace_id (confirmed present) -- the app's workspace filter now actually narrows this
--   finding. read_count/status now band on reads FROM ONE WORKSPACE, not the account-wide total
--   for that table+reader; a table read by the same person from several workspaces now surfaces
--   as one row per workspace instead of one pooled row, so a reader who is quiet in each
--   workspace individually but busy overall no longer necessarily flags WARN. is_owner/
--   table_owner/sensitivity_basis are unaffected -- those come from information_schema and
--   data_classification, which are metastore-wide, not per-workspace. The single account-wide
--   NOT_ASSESSED sentinel row has no natural workspace, so its workspace_id is NULL.
WITH sensitive_table_tags AS (
  SELECT DISTINCT catalog_name, schema_name, table_name, 'table_tag' AS basis
  FROM system.information_schema.table_tags
  WHERE tag_name  RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
     OR tag_value RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
),
sensitive_column_tags AS (
  SELECT DISTINCT catalog_name, schema_name, table_name, 'column_tag' AS basis
  FROM system.information_schema.column_tags
  WHERE tag_name  RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
     OR tag_value RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
),
sensitive_classification AS (
  SELECT DISTINCT catalog_name, schema_name, table_name, 'classification' AS basis
  FROM system.data_classification.results
  WHERE class_tag IS NOT NULL
),
sensitive_union AS (
  SELECT catalog_name, schema_name, table_name, basis FROM sensitive_table_tags
  UNION ALL
  SELECT catalog_name, schema_name, table_name, basis FROM sensitive_column_tags
  UNION ALL
  SELECT catalog_name, schema_name, table_name, basis FROM sensitive_classification
),
sensitive_tables AS (
  -- one row per table the account has flagged sensitive by any of the three sources above
  SELECT catalog_name, schema_name, table_name,
         array_join(collect_set(basis), ', ') AS sensitivity_basis
  FROM sensitive_union
  GROUP BY catalog_name, schema_name, table_name
),
sensitive_table_count AS (
  SELECT COUNT(*) AS n FROM sensitive_tables
),
table_reads AS (
  -- every table_lineage row that proves a READ of its source table (see caveats: SOURCE)
  SELECT workspace_id,
         source_table_catalog AS catalog_name,
         source_table_schema  AS schema_name,
         source_table_name    AS table_name,
         created_by,
         event_time,
         event_date
  FROM system.access.table_lineage
  WHERE source_table_name IS NOT NULL
    AND source_table_catalog <> 'system'
    AND (direct_access IS NULL OR direct_access = true)
    AND event_date >= dateadd(day, -:period_days, current_date())
    AND event_date < current_date()
),
table_owners AS (
  SELECT table_catalog AS catalog_name, table_schema AS schema_name, table_name, table_owner
  FROM system.information_schema.tables
),
reads_agg AS (
  SELECT r.workspace_id, r.catalog_name, r.schema_name, r.table_name, r.created_by,
         COUNT(*)                     AS read_count,
         MIN(r.event_time)            AS first_read_time,
         MAX(r.event_time)            AS last_read_time,
         COUNT(DISTINCT r.event_date) AS distinct_read_days,
         MAX(s.sensitivity_basis)     AS sensitivity_basis
  FROM table_reads r
  JOIN sensitive_tables s
    ON  s.catalog_name = r.catalog_name
    AND s.schema_name  = r.schema_name
    AND s.table_name   = r.table_name
  GROUP BY r.workspace_id, r.catalog_name, r.schema_name, r.table_name, r.created_by
),
judged AS (
  SELECT a.workspace_id, a.catalog_name, a.schema_name, a.table_name, a.created_by,
         a.read_count, a.first_read_time, a.last_read_time, a.distinct_read_days,
         a.sensitivity_basis,
         o.table_owner,
         -- raw, unmasked comparison so masking (below) never hides a real owner match
         CASE
           WHEN o.table_owner IS NOT NULL AND a.created_by IS NOT NULL
            AND lower(trim(o.table_owner)) = lower(trim(a.created_by))
           THEN 1 ELSE 0
         END AS is_owner
  FROM reads_agg a
  LEFT JOIN table_owners o
    ON  o.catalog_name = a.catalog_name
    AND o.schema_name  = a.schema_name
    AND o.table_name   = a.table_name
),
combined AS (
  SELECT j.workspace_id AS workspace_id,
         j.catalog_name AS table_catalog,
         j.schema_name  AS table_schema,
         j.table_name   AS table_name,
         j.sensitivity_basis,
         CASE
           WHEN j.created_by IS NULL OR j.created_by = '__REDACTED__' THEN j.created_by
           WHEN j.created_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN j.created_by
           ELSE concat(substr(sha2(lower(trim(j.created_by)), 256), 1, 8), ' ', substr(j.created_by, 1, 2), '***')
         END AS reader,
         CASE
           WHEN j.table_owner IS NULL OR j.table_owner = '__REDACTED__' THEN j.table_owner
           WHEN j.table_owner RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN j.table_owner
           ELSE concat(substr(sha2(lower(trim(j.table_owner)), 256), 1, 8), ' ', substr(j.table_owner, 1, 2), '***')
         END AS table_owner,
         CAST(j.read_count AS BIGINT)         AS read_count,
         j.first_read_time,
         j.last_read_time,
         CAST(j.distinct_read_days AS BIGINT) AS distinct_read_days,
         -- status: an owner is never flagged; a non-owner over :warn_reads reads is WARN.
         CASE
           WHEN j.is_owner = 1        THEN 'OK'
           WHEN j.read_count > :warn_reads THEN 'WARN'
           ELSE 'OK'
         END AS status,
         CAST(NULL AS STRING) AS not_assessed_reason
  FROM judged j

  UNION ALL

  -- the one account-wide sentinel row: nothing anywhere has ever been tagged or classified
  -- sensitive, so nothing below could have been judged in the first place (never silently OK).
  -- No natural workspace to tie it to, so workspace_id is NULL -- it drops out under a workspace
  -- filter rather than falsely claiming to speak for one particular workspace.
  SELECT
    CAST(NULL AS STRING)    AS workspace_id,
    CAST(NULL AS STRING)    AS table_catalog,
    CAST(NULL AS STRING)    AS table_schema,
    CAST(NULL AS STRING)    AS table_name,
    CAST(NULL AS STRING)    AS sensitivity_basis,
    CAST(NULL AS STRING)    AS reader,
    CAST(NULL AS STRING)    AS table_owner,
    CAST(0 AS BIGINT)       AS read_count,
    CAST(NULL AS TIMESTAMP) AS first_read_time,
    CAST(NULL AS TIMESTAMP) AS last_read_time,
    CAST(0 AS BIGINT)       AS distinct_read_days,
    'NOT_ASSESSED'          AS status,
    'no_sensitivity_signal' AS not_assessed_reason
  FROM sensitive_table_count
  WHERE n = 0
)
SELECT workspace_id, table_catalog, table_schema, table_name, sensitivity_basis, reader, table_owner,
       read_count, first_read_time, last_read_time, distinct_read_days, status, not_assessed_reason
FROM combined
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         read_count DESC,
         table_catalog, table_schema, table_name, reader
