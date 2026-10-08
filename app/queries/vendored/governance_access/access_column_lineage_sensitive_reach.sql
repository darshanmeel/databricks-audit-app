-- query_id: access_column_lineage_sensitive_reach
-- title: Column-level lineage reach
-- domain: governance_access   tier: deep
-- reads: system.access.column_lineage, system.information_schema.column_tags
-- requires: SELECT on system.access and system.information_schema; system.access.column_lineage is GA; information_schema requires Unity Catalog
-- empty_if: lineage_inference_only, schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_reach_principals (default 10) distinct principals driving one source-target column edge that flags WARN; :crit_reach_principals (default 50) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: The source/target table and column names, entity_type, direct_access, created_by and event_time columns are confirmed against system.access.column_lineage. This account's actual sensitivity tag_name/tag_value convention is not confirmed - the query matches common names (pii, sensitivity, sensitive, data_classification, classification, confidentiality, phi, pci) case-insensitively as a heuristic (the same one access_pii_propagation_untagged uses), written to under-detect rather than invent a match, which is the safe direction.
-- read_this: One row = a workspace x source column x target column data-flow edge (plus entity_type and direct_access), restricted to edges whose SOURCE column carries a sensitivity tag. The columns that matter are source_sensitivity_tags (which tag(s) made the source count as sensitive), event_count (how often that flow ran) and distinct_principals - how many different identities drove it FROM THAT WORKSPACE. This is per-edge-per-workspace, not a table-level rollup; pair it with access_table_lineage_blast_radius for the coarser view.
-- healthy: status = OK; distinct_principals below :warn_reach_principals for one sensitive-source edge - field heuristic.
-- investigate_if: status = WARN at/above :warn_reach_principals, CRITICAL at/above :crit_reach_principals - field heuristic; a column with no sensitivity tag at all never appears here at all (see caveats), so also check access_data_classification_inventory or access_tags_inventory for anything this query's own tag-name heuristic missed.
-- actions: 1) confirm the listed source_sensitivity_tags value is genuinely a sensitivity tag on your account, not a false match from the heuristic (free); 2) tighten grants on the source or add a mask, and check access_pii_propagation_untagged for the specific untagged-target case (config); 3) if a column is a genuinely critical, widely-reached asset, formalize it as a governed data product with an owner (spend/eng time).
-- next: access_pii_propagation_untagged (the specific untagged-target finding this feeds), access_table_lineage_blast_radius (the table-level rollup)
-- caveats: "sensitive" means the SOURCE column carries a UC column tag (system.information_schema.column_tags) whose name or value matches pii/sensitivity/sensitive/data_classification/classification/confidentiality/phi/pci, case-insensitively - a governance TAG, never an automated data_classification scan result (not available on this account). This is a heuristic on tag_name/tag_value, written to under-detect rather than invent a match; confirm your account's actual sensitivity-tag convention. A sensitive column with no such tag is invisible here entirely - the same blind spot access_pii_propagation_untagged already documents on its target side - so a clean or empty result is not proof nothing sensitive is reached, only that nothing TAGGED sensitive is. source_sensitivity_tags collapses every matching tag on the source column into one "name=value, name=value" string before the join to lineage, so a column carrying more than one matching tag (e.g. pii=true AND classification=confidential) still produces exactly one row per edge, not one per tag. direct_access = true/false is passed through unfiltered (unlike access_pii_propagation_untagged, which restricts to true), so both direct and view-expanded reach are visible; filter to direct_access = true yourself for the narrower, more certain reading. column_lineage is a SUBSET of all data movement - events with no captured source (e.g. INSERT ... VALUES literals) are NOT captured, so this undercounts; report it as coverage-bounded, never as a complete picture. External/path references show cloud path strings, not table names. statement_id is SQL-warehouse-only. entity_metadata subfields (job_info.job_id, notebook_id, sql_query_id, dlt_pipeline_info.*, genie_space_id, alert_id) are available in the source table for finer attribution but are kept out of this rollup. GA. Regional. This query historically used a 90-day window; set :period_days=90 to reproduce it. WORKSPACE_ID: added to the SELECT/GROUP BY (confirmed present on column_lineage) - the app's workspace filter now actually narrows this finding. distinct_principals (and event_count, status) is therefore reach WITHIN ONE WORKSPACE, not the account-wide total for that column edge; the same sensitive column reached from several workspaces now surfaces as one row per workspace instead of one pooled row. Aggregate across workspace_id yourself for the account-wide figure.
-- Lineage is also not inferred for work run via unsupported paths (RDD, JDBC, spark-submit jobs, UDFs, global temp views) or for pipeline column lineage below DBR 13.3 LTS, so an edge can be wholly absent - low or missing reach is not proof of safety.
WITH sensitive_tags AS (
  -- One row per SOURCE column (GROUP BY, not the raw per-tag rows): a column carrying more than
  -- one matching tag collapses here first, so the join below cannot fan the same edge out twice.
  SELECT catalog_name, schema_name, table_name, column_name,
         array_join(collect_set(concat(tag_name, '=', tag_value)), ', ') AS source_sensitivity_tags
  FROM system.information_schema.column_tags
  WHERE lower(tag_name) IN (
          'pii', 'sensitivity', 'sensitive', 'data_classification', 'classification',
          'confidentiality', 'phi', 'pci'
        )
     OR lower(tag_value) IN (
          'pii', 'sensitive', 'confidential', 'restricted', 'phi', 'pci'
        )
  GROUP BY catalog_name, schema_name, table_name, column_name
)
SELECT lw.workspace_id, lw.source_table_full_name, lw.source_column_name, st.source_sensitivity_tags,
       lw.target_table_full_name, lw.target_column_name, lw.entity_type, lw.direct_access,
       COUNT(*) AS event_count,
       COUNT(DISTINCT lw.created_by) AS distinct_principals,
       MAX(lw.event_time) AS last_event_time,
       -- status: worst-first band on how many distinct principals drove this sensitive-source
       -- column-to-column edge within this workspace (field heuristic; :warn_reach_principals /
       -- :crit_reach_principals).
       CASE
         WHEN COUNT(DISTINCT lw.created_by) >= :crit_reach_principals THEN 'CRITICAL'
         WHEN COUNT(DISTINCT lw.created_by) >= :warn_reach_principals THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.access.column_lineage lw
-- SOURCE column must carry a sensitivity tag, so a row with no sensitivity input never reads OK.
JOIN sensitive_tags st
  ON  lw.source_table_catalog = st.catalog_name
  AND lw.source_table_schema  = st.schema_name
  AND lw.source_table_name    = st.table_name
  AND lw.source_column_name   = st.column_name
WHERE lw.source_table_full_name IS NOT NULL
  AND lw.event_date >= current_date() - INTERVAL :period_days DAYS
  AND lw.event_date < current_date()
GROUP BY lw.workspace_id, lw.source_table_full_name, lw.source_column_name, st.source_sensitivity_tags,
         lw.target_table_full_name, lw.target_column_name, lw.entity_type, lw.direct_access
ORDER BY distinct_principals DESC
