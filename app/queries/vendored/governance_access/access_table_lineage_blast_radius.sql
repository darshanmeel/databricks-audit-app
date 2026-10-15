-- query_id: access_table_lineage_blast_radius
-- title: Table-level lineage blast radius
-- domain: governance_access   tier: standard
-- reads: system.access.table_lineage
-- requires: SELECT on system.access; GA
-- empty_if: schema_not_enabled, lineage_inference_only, privilege_scoped
-- params: :period_days (default 30) rolling window in days; :warn_blast_principals (default 10) distinct principals driving one source-target edge that flags WARN; :crit_blast_principals (default 50) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: source_table_full_name is NULL for write-only events and target_table_full_name is NULL for read-only events; the access_class CASE reproduces Databricks' documented read/write rule from those two columns.
-- read_this: One row = a workspace x source table x target table data-flow edge (plus type, entity_type, and direct_access) in the window, kept as detail; event_count is the edge's own event volume, distinct_principals this edge's own distinct identity count within that workspace. source_distinct_principals is the SOURCE TABLE's total reach - every distinct identity that touched it via ANY target from ANY workspace in the window, not just this one edge - so a table fanned out across many small targets still reads its true blast radius instead of a count fragmented per edge; NULL on a target-less WRITE row, which has no source table to roll up to.
-- healthy: status = OK; source_distinct_principals (the source table's own total reach, or this edge's own distinct_principals when there is no source to roll up to) below :warn_blast_principals - field heuristic.
-- investigate_if: status = WARN at/above :warn_blast_principals, CRITICAL at/above :crit_blast_principals - field heuristic; an edge with direct_access=true, a sensitive-looking source, and high distinct_principals is the highest-priority combination to review.
-- actions: 1) confirm the source table's sensitivity via access_data_classification_inventory before treating a wide edge as risky (free); 2) tighten grants on the source table if the fan-out is broader than intended (config); 3) if this is a genuinely critical shared table, formalize it as a governed data product with an owner and change-review process (spend/eng time).
-- next: access_column_lineage_sensitive_reach (the column-level equivalent), access_pii_propagation_untagged (check if this edge is also an untagged PII gap)
-- caveats: source_table_full_name is NULL for write-only events and target_table_full_name is NULL for read-only events - the access_class column reproduces Databricks' documented read/write rule from those two columns. direct_access=false means an indirect or view-expansion access. This reads system.access.table_lineage, which is GA with a rolling 365-day retention. An empty result for a table you know is used means the lineage event was simply not captured (MERGE, JDBC, path-based, or temp-view access all have gaps), not that the table is unused - degrade to a coverage gap, never a false negative. Regional. This query historically used a 90-day window; set :period_days=90 to reproduce it. Rows where BOTH source_table_full_name and target_table_full_name are NULL (path-based, JDBC, or otherwise-unresolved access that this table cannot tie to a catalog table on EITHER side) are excluded, as access_column_lineage_sensitive_reach's own WHERE already does for its source side - without this filter every such access pooled into ONE fake 'UNKNOWN' edge with a shared distinct_principals count, so unrelated path-based accesses by many different principals could combine to a CRITICAL blast radius that no single real edge earned. A path-based or otherwise-unresolved access is invisible here by design, not folded into any other edge's principal count - it is a coverage gap, same as an uncaptured event. source_distinct_principals is computed per SOURCE TABLE (source_reach below), not per edge: a source read through five different targets by five different principals each used to read a per-edge count of 1 on every one of those five rows (each below :warn_blast_principals) even though the table's own real reach was 5 - source_distinct_principals now reads 5 on every one of that source's rows, while distinct_principals keeps the per-edge count (never overwritten) so the detail is not lost. A row with no source table (a WRITE access_class) has nothing to roll up to, so source_distinct_principals is NULL there and status falls back to distinct_principals, that row's own edge-level count.
WITH lineage_window AS (
  SELECT workspace_id, source_table_full_name, target_table_full_name, source_type, target_type,
         entity_type, direct_access, created_by, event_time
  FROM system.access.table_lineage
  WHERE event_date >= current_date() - INTERVAL :period_days DAYS
    AND event_date < current_date()
    -- Exclude accesses this table cannot tie to a catalog table on EITHER side (path-based, JDBC,
    -- unresolved) rather than pooling every one of them into one fake NULL-name "edge" (see caveats).
    AND (source_table_full_name IS NOT NULL OR target_table_full_name IS NOT NULL)
),
-- One row per SOURCE table: every distinct principal that touched it via ANY target in the
-- window, so a source fanned out across many small targets reads its true total reach instead of
-- a count split per edge.
source_reach AS (
  SELECT source_table_full_name, COUNT(DISTINCT created_by) AS distinct_principals
  FROM lineage_window
  WHERE source_table_full_name IS NOT NULL
  GROUP BY source_table_full_name
)
SELECT lw.workspace_id, lw.source_table_full_name, lw.target_table_full_name, lw.source_type, lw.target_type,
       lw.entity_type, lw.direct_access,
       CASE WHEN lw.source_type IS NOT NULL AND lw.target_type IS NULL THEN 'READ'
            WHEN lw.target_type IS NOT NULL AND lw.source_type IS NULL THEN 'WRITE'
            WHEN lw.source_type IS NOT NULL AND lw.target_type IS NOT NULL THEN 'READ_WRITE'
            ELSE 'UNKNOWN' END AS access_class,
       COUNT(*) AS event_count,
       COUNT(DISTINCT lw.created_by) AS distinct_principals,
       MAX(sr.distinct_principals) AS source_distinct_principals,
       MAX(lw.event_time) AS last_event_time,
       -- status: worst-first band on the source table's own total reach, this edge's own count
       -- for a WRITE row with no source to roll up to (field heuristic; :warn_blast_principals / :crit_blast_principals).
       CASE
         WHEN COALESCE(MAX(sr.distinct_principals), COUNT(DISTINCT lw.created_by)) >= :crit_blast_principals THEN 'CRITICAL'
         WHEN COALESCE(MAX(sr.distinct_principals), COUNT(DISTINCT lw.created_by)) >= :warn_blast_principals THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM lineage_window lw
LEFT JOIN source_reach sr ON sr.source_table_full_name = lw.source_table_full_name
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
ORDER BY distinct_principals DESC
