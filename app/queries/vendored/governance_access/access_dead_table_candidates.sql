-- query_id: access_dead_table_candidates
-- title: Dead-table cleanup candidates
-- domain: governance_access   tier: standard
-- reads: system.access.table_lineage, system.information_schema.tables
-- requires: SELECT on system.access and system.information_schema; system.access.table_lineage is GA; information_schema requires Unity Catalog
-- empty_if: lineage_inference_only, retention_window, privilege_scoped
-- params: :period_days (default 30) rolling window in days; :warn_dead_days (default 90) days since last altered for a table with no lineage-source hits that flags WARN; :crit_dead_days (default 365) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: Lineage undercounts reads that have no captured source (e.g. INSERT ... VALUES literals), so a table absent from lineage is a cleanup candidate to cross-check, not proof it is unused.
-- read_this: One row = a MANAGED or EXTERNAL table that never appeared as a lineage source (was never read) in the window. The columns that matter are days_since_altered (staleness) and table_owner (who to ask before dropping anything).
-- healthy: status = OK; days_since_altered below :warn_dead_days for a table that never appeared as a lineage source - field heuristic; a candidate can still be OK for a while (e.g. quarterly jobs).
-- investigate_if: status = WARN at/above :warn_dead_days, CRITICAL at/above :crit_dead_days, or NOT_ASSESSED when last_altered is NULL (age unknown - do not treat as a finding) - field heuristic; widen :period_days first if your account's retention allows it, since a short lineage window over-flags quarterly/long-tail tables.
-- actions: 1) cross-check the table against system.access.audit, query.history, and system.storage.table_metrics_history before touching anything (free); 2) if genuinely unused, revoke write access or move it to a to-be-deleted schema for a cooling-off period (config); 3) once confirmed dead, formally retire and drop it to reclaim storage spend (spend).
-- next: access_table_lineage_blast_radius (see the table's full lineage graph if it does show up elsewhere), po_vacuum_reclaimed_bytes (reclaim storage once you drop it)
-- caveats: A table that never shows up as a lineage SOURCE in the window is a cleanup CANDIDATE, not proof it's unused - a SELECT whose result is never written to another table emits no lineage row at all, and write-only targets never appear as a source either. Treat this as a candidate list to cross-check against system.access.audit, query.history, and system.storage.table_metrics_history before you touch anything - never as a DROP recommendation on its own. Lineage is only captured when Unity Catalog observes the operation. direct_access=false (indirect/view-expansion) edges still count as "appeared as a source" here, so a view-mediated read does not falsely mark the underlying base table dead. Retention on system.access.* tables is workspace-configurable, and a short window over-flags quarterly or long-tail tables - widen :period_days if your account's retention allows it. System tables are excluded by exact catalog name ('system'), not a bare NOT LIKE 'system' (which has no wildcard and would behave as <> 'system', missing system schemas that don't match exactly). information_schema is privilege-aware, so tables you cannot see are absent from this list, not proven dead. last_altered/created can be NULL or late-populated on some object types - read that as "age unknown," never as a finding on its own (this drives the NOT_ASSESSED status).
WITH lineage_window AS (
  SELECT *
  FROM system.access.table_lineage
  WHERE event_date >= dateadd(day, -:period_days, current_date())
    AND event_date < current_date()
),
-- Every table that appeared as a lineage SOURCE in the window (any direct_access
-- value - an indirect/view-mediated read of a base table still proves it was read,
-- so we must NOT exclude direct_access=false here or we over-flag base tables).
-- Prefer the discrete *_catalog/_schema/_name columns over CONCAT vs _full_name,
-- because _full_name can be backtick-quoted for reserved-word identifiers.
source_tables AS (
  SELECT DISTINCT
         source_table_catalog AS catalog,
         source_table_schema  AS schema,
         source_table_name    AS name
  FROM lineage_window
  WHERE source_table_name IS NOT NULL
),
-- The managed/external tables this account holds (privilege-scoped inventory).
inventory AS (
  SELECT table_catalog,
         table_schema,
         table_name,
         table_type,
         table_owner,
         created,
         last_altered
  FROM system.information_schema.tables
  WHERE table_catalog <> 'system'
    AND table_schema <> 'information_schema'
    AND table_type IN ('MANAGED', 'EXTERNAL')
)
SELECT inv.table_catalog,
       inv.table_schema,
       inv.table_name,
       inv.table_type,
       CASE
         WHEN inv.table_owner IS NULL OR inv.table_owner = '__REDACTED__' THEN inv.table_owner
         WHEN inv.table_owner RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN inv.table_owner
         ELSE concat(substr(sha2(lower(trim(inv.table_owner)), 256), 1, 8), ' ', substr(inv.table_owner, 1, 2), '***')
       END AS table_owner,
       inv.created,
       inv.last_altered,
       -- Age in whole days since the object was last altered (NULL -> age unknown).
       datediff(current_date(), DATE(inv.last_altered)) AS days_since_altered,
       -- status: worst-first band on staleness (field heuristic; :warn_dead_days / :crit_dead_days). NULL age -> NOT_ASSESSED, never a finding.
       CASE
         WHEN datediff(current_date(), DATE(inv.last_altered)) IS NULL THEN 'NOT_ASSESSED'
         WHEN datediff(current_date(), DATE(inv.last_altered)) >= :crit_dead_days THEN 'CRITICAL'
         WHEN datediff(current_date(), DATE(inv.last_altered)) >= :warn_dead_days THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM inventory inv
LEFT JOIN source_tables src
  ON  inv.table_catalog = src.catalog
  AND inv.table_schema  = src.schema
  AND inv.table_name    = src.name
WHERE src.name IS NULL          -- never appeared as a lineage source in the window
ORDER BY days_since_altered DESC NULLS LAST
