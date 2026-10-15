-- query_id: access_column_masks_inventory
-- title: Column masks inventory
-- domain: governance_access   tier: standard
-- reads: system.information_schema.column_masks
-- requires: SELECT on system.information_schema; Public Preview, DBR 12.2 LTS+, Unity Catalog required
-- empty_if: abac_only, privilege_scoped
-- params: none - static current-state inventory, no window.
-- confidence: confirmed
-- confidence_note: Columns verified against a live workspace on 2026-05-30: table_catalog, table_schema, table_name, column_name, mask_name, using_columns.
-- read_this: One row = a column in the metastore that has a column mask attached. The columns that matter are mask_name (which masking function) and using_columns (what it reads to decide the mask).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: access_classified_unmasked (cross-check which classified columns are missing from this list), access_row_filters_inventory (the row-level equivalent)
-- caveats: This is already a compact current-state inventory - one row per masked column, not pre-aggregated. It is privilege-aware: tables visible only via BROWSE are excluded, so treat this as a privilege-scoped inventory, not a complete list of every mask in the metastore. It reads system.information_schema.column_masks, which is Public Preview and requires DBR 12.2 LTS+ - if unsupported on your account, expect an empty or erroring result, not a true zero. system.information_schema is metastore-wide.
-- column_masks lists only manually-applied (ALTER ... SET MASK) masks; ABAC tag-based policy masks never appear here, so an ABAC-governed account can look falsely unmasked - enumerate those via the UC REST API.
SELECT table_catalog, table_schema, table_name, column_name,
       mask_name, using_columns
FROM system.information_schema.column_masks
ORDER BY table_catalog, table_schema, table_name, column_name
