-- query_id: access_pii_propagation_untagged
-- title: PII propagation into untagged columns
-- domain: governance_access   tier: standard
-- reads: system.access.column_lineage, system.information_schema.column_tags
-- requires: SELECT on system.access and system.information_schema; system.access.column_lineage is GA; information_schema requires Unity Catalog
-- empty_if: lineage_inference_only, schema_not_enabled, privilege_scoped, retention_window
-- params: :period_days (default 30) rolling window in days; :warn_pii_gap_events (default 5) times a source-to-target untagged-propagation edge fired that flags WARN; :crit_pii_gap_events (default 50) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: This account's actual sensitivity tag_name/tag_value convention is not confirmed - the query matches common names (pii, sensitivity, sensitive, data_classification, classification, confidentiality, phi, pci) case-insensitively as a heuristic, written to under-detect rather than invent a match, which is the safe direction.
-- read_this: One row = a source column (tagged sensitive) to target column (carrying no sensitivity tag of its own) propagation edge, one workspace at a time. The columns that matter are event_count (how often that flow ran) and distinct_creators (how many different identities ran it).
-- healthy: status = OK; event_count below :warn_pii_gap_events for one source-to-target propagation edge - field heuristic; any row here is already a gap, so "healthy" just means it is not yet a well-established pipeline.
-- investigate_if: status = WARN at/above :warn_pii_gap_events, CRITICAL at/above :crit_pii_gap_events - field heuristic; prioritize edges with a high event_count, since those are recurring, established pipelines, not one-off ad hoc queries.
-- actions: 1) confirm the source column really is sensitive and the target genuinely lacks a tag (free); 2) tag the target column to match the source's classification, or add a column mask if it should stay untagged but restricted (config); 3) if this is a recurring ETL/pipeline pattern, fix the pipeline to propagate tags automatically instead of tagging targets one at a time (spend/eng time).
-- next: access_tags_inventory (see the full tag inventory), access_classified_unmasked (check whether the untagged target is also unmasked)
-- caveats: This detects sensitivity-tagged SOURCE columns that flow, via direct_access=true lineage, into target columns carrying no SENSITIVITY tag of their own (fixes access_pii_propagation_untagged-any-tag-counts-as-governed: the target used to be judged "untagged" by anti-joining against any column carrying ANY governance tag at all - ownership, lifecycle, anything - so a target tagged e.g. "owner=data-eng" read as governed and dropped out of this finding even though it carried no sensitivity classification; the target side now anti-joins against the SAME sensitive_tags set the source side already used, so a target only counts as governed when it carries a sensitivity tag itself). source_tags collapses every matching tag on the source column into one "name=value, name=value" string before the join, and the target-side anti-join does the same, so a column carrying more than one matching tag - on either side - produces exactly one row per edge, not one per tag; distinct_creators (COUNT DISTINCT, not a single masked identity) keeps that same one-row-per-edge shape when more than one identity ran the flow. The target-tag join uses exact ('=') matching on the four discrete catalog/schema/table/column columns rather than a LIKE/CONCAT comparison - '_' is a LIKE wildcard, so an underscore in an identifier would otherwise over-match and falsely mark a target "tagged", hiding a real gap. direct_access=true is a deliberate restriction: indirect/view-expansion edges are excluded, because a view does not re-expose the base sensitive column as a new physical target column, so view-mediated flows are intentionally invisible here. Tag values are free-text and case-sensitive, so coverage depends entirely on your account's tagging discipline - an empty result can mean "no PII tagged anywhere", not "no gaps", so verify you actually have sensitivity tags in use before reading zero rows as clean. The PII tag-name set (pii, sensitivity, sensitive, data_classification, classification, confidentiality, phi, pci) is a heuristic on tag_name and tag_value matched case-insensitively - confirm your account's actual sensitivity-tag convention, since this is written to under-detect rather than invent a match. A target column carrying some OTHER (non-sensitivity) governance tag now correctly still counts as a gap here; check access_tags_inventory if you need the full governance-tag picture rather than the sensitivity-only one this query judges "tagged" by. information_schema.column_tags is privilege-aware. Retention on system.access.* tables is workspace-configurable - widen :period_days where your retention allows it. The system catalog is excluded by exact name, not a bare NOT LIKE (which has no wildcard and would behave as <> 'system').
-- Column lineage is inference-only: PII propagation via unsupported paths (spark-submit/runs-submit jobs, RDDs, JDBC, UDFs, global temp views, path-only refs) or literal-only writes produces no lineage row and is invisible here, and Lakeflow pipeline column lineage additionally needs DBR 13.3 LTS+.
WITH lineage_window AS (
  SELECT workspace_id, source_table_catalog, source_table_schema, source_table_name, source_column_name,
         target_table_catalog, target_table_schema, target_table_name, target_column_name,
         created_by
  FROM system.access.column_lineage
  WHERE direct_access = true                       -- direct flows only; excludes view expansion
    AND source_column_name IS NOT NULL
    AND target_column_name IS NOT NULL
    AND source_table_catalog <> 'system'
    AND event_date >= dateadd(day, -:period_days, current_date())
    AND event_date < current_date()
),
-- One row per column (GROUP BY, not the raw per-tag rows): a column carrying more than one
-- matching tag collapses here first, so the joins below cannot fan the same lineage event out
-- once per tag (used on both the source and the target anti-join side).
sensitive_tags AS (
  SELECT catalog_name, schema_name, table_name, column_name,
         array_join(sort_array(collect_set(concat(tag_name, '=', tag_value))), ', ') AS tags
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
SELECT lw.workspace_id,
       lw.source_table_catalog,
       lw.source_table_schema,
       lw.source_table_name,
       lw.source_column_name,
       st.tags AS source_tags,
       lw.target_table_catalog,
       lw.target_table_schema,
       lw.target_table_name,
       lw.target_column_name,
       COUNT(DISTINCT lw.created_by) AS distinct_creators,
       COUNT(*) AS event_count,
       -- status: worst-first band on how often this untagged-PII-propagation edge fired (field heuristic; :warn_pii_gap_events / :crit_pii_gap_events).
       CASE
         WHEN COUNT(*) >= :crit_pii_gap_events THEN 'CRITICAL'
         WHEN COUNT(*) >= :warn_pii_gap_events THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM lineage_window lw
-- SOURCE column must be sensitivity-tagged (= join on discrete columns).
JOIN sensitive_tags st
  ON  lw.source_table_catalog = st.catalog_name
  AND lw.source_table_schema  = st.schema_name
  AND lw.source_table_name    = st.table_name
  AND lw.source_column_name   = st.column_name
-- TARGET column must carry NO SENSITIVITY tag: anti-joins against sensitive_tags, the same
-- sensitivity-specific set the source side uses, not any column carrying any governance tag at
-- all. Exact ('=') join on the four discrete columns, never LIKE.
LEFT JOIN sensitive_tags tt
  ON  lw.target_table_catalog = tt.catalog_name
  AND lw.target_table_schema  = tt.schema_name
  AND lw.target_table_name    = tt.table_name
  AND lw.target_column_name   = tt.column_name
WHERE tt.column_name IS NULL          -- target carries no SENSITIVITY tag -> propagation gap
GROUP BY lw.workspace_id, lw.source_table_catalog, lw.source_table_schema, lw.source_table_name,
         lw.source_column_name, st.tags,
         lw.target_table_catalog, lw.target_table_schema, lw.target_table_name,
         lw.target_column_name
ORDER BY event_count DESC
