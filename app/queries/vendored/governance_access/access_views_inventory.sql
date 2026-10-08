-- query_id: access_views_inventory
-- title: View inventory (lineage completeness and materialized views)
-- domain: governance_access   tier: standard
-- reads: system.information_schema.views
-- requires: SELECT on system.information_schema; Unity Catalog required
-- empty_if: privilege_scoped, no_activity
-- params: :top_n (default 100000) row cap - current-state inventory, no window.
-- confidence: confirmed
-- confidence_note: views columns are transcribed verbatim from the information_schema reference; only view METADATA is emitted, never the view_definition SQL itself.
-- read_this: One row = one view (or materialized view) visible to your grants, with whether it is materialized, updatable/insertable, its sql_path, and the size of its definition. Use it to complete the lineage picture (views mediate table-to-table flows that table lineage may not attribute) and to inventory materialized views (which carry compute + storage cost).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input; is_materialized = true is a cost signal - cross-check spend)
-- next: access_table_lineage_blast_radius (the table lineage these views mediate), access_dead_table_candidates (unused views/tables to retire)
-- caveats: The view_definition SQL is NOT emitted (it can contain sensitive logic) - only its character length (definition_chars) and a derived flag. references_mask_or_filter is a TEXT heuristic (the definition string mentions MASK/FILTER) and is NOT authoritative masked-view detection - a view can wrap a masked table without saying so, and a definition can mention the words for unrelated reasons. system.information_schema is privilege-aware, so views the principal cannot see are absent. is_materialized separates materialized views (compute/storage cost) from plain views. Current-state inventory (no window). This is a full inventory, not a findings list (no status column) - a row cap silently drops whole catalogs/schemas past the alphabetical cutoff, never surfacing which ones were dropped; :top_n defaults to 100000 (was 500) so a real account's view count does not silently truncate, with no thresholds.yml override needed.
SELECT
  TABLE_CATALOG AS view_catalog,
  TABLE_SCHEMA  AS view_schema,
  TABLE_NAME    AS view_name,
  IS_MATERIALIZED    AS is_materialized,
  IS_UPDATABLE       AS is_updatable,
  IS_INSERTABLE_INTO AS is_insertable_into,
  SQL_PATH           AS sql_path,
  LENGTH(VIEW_DEFINITION) AS definition_chars,
  CASE WHEN upper(VIEW_DEFINITION) LIKE '%MASK%' OR upper(VIEW_DEFINITION) LIKE '%FILTER%'
       THEN true ELSE false END AS references_mask_or_filter
FROM system.information_schema.views
ORDER BY view_catalog, view_schema, view_name
LIMIT :top_n
