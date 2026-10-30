-- query_id: access_row_filters_inventory
-- title: Row filters inventory
-- domain: governance_access   tier: standard
-- reads: system.information_schema.row_filters
-- requires: SELECT on system.information_schema; Public Preview, DBR 12.2 LTS+, Unity Catalog required
-- empty_if: abac_only, privilege_scoped
-- params: none - static current-state inventory, no window.
-- confidence: confirmed
-- confidence_note: Columns verified against a live workspace on 2026-05-30: table_catalog, table_schema, table_name, filter_name, target_columns.
-- read_this: One row = a table in the metastore that has a row-level access filter attached. The columns that matter are filter_name (which filter function) and target_columns (what it evaluates to decide row visibility).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: access_column_masks_inventory (the column-level equivalent), access_grants_inventory (see who has access to these filtered tables)
-- caveats: This is already a compact current-state inventory - one row per filtered table. It is privilege-aware / object-visibility-scoped, so it only shows tables you can see. It reads system.information_schema.row_filters, which is Public Preview and requires DBR 12.2 LTS+. system.information_schema is metastore-wide.
-- It lists only manually-applied (table-level) row filters set via ALTER TABLE; ABAC tag-based policy filters do not appear here, so an ABAC-governed account can look falsely unfiltered - enumerate those via the UC REST API.
SELECT table_catalog, table_schema, table_name,
       filter_name, target_columns
FROM system.information_schema.row_filters
ORDER BY table_catalog, table_schema, table_name
