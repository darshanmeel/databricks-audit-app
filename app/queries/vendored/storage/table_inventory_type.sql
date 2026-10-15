-- query_id: table_inventory_type
-- title: Table and view inventory by type and format
-- domain: storage   tier: deep
-- reads: system.information_schema.tables
-- requires: SELECT on system.information_schema; Unity Catalog required
-- empty_if: privilege_scoped
-- params: none (catalog-wide inventory snapshot, no time window)
-- confidence: needs_confirmation
-- confidence_note: Only table_type (MANAGED/EXTERNAL/VIEW) is verbatim-confirmed against Databricks docs; table_catalog, table_schema, and data_source_format are plausible information_schema column names but unverified until you run this in your own workspace and confirm they resolve.
-- read_this: One row = a catalog + schema + table_type + data_source_format combination, with the count of tables/views matching it. table_type tells you managed vs external vs view; data_source_format (when it resolves) flags DELTA vs ICEBERG. EXTERNAL tables are not eligible for Predictive Optimization, so a high EXTERNAL count here caps how much of po_maintenance_cost_by_table's coverage can ever reach 100%.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: iceberg_uniform_metadata (drill into EXTERNAL / non-DELTA tables to confirm UniForm), po_maintenance_cost_by_table (cross-check PO coverage against the EXTERNAL count here)
-- caveats: information_schema is privilege-aware - you only see tables/views your own credentials can access, so counts here are a floor, not the whole metastore. This carries no size columns; pair with storage_breakdown_analyze for actual bytes. data_source_format (DELTA vs ICEBERG) helps identify Iceberg candidates, but its presence and exact name are unverified until confirmed in your workspace.
SELECT table_catalog, table_schema, table_type, data_source_format,
       COUNT(*) AS table_count
FROM system.information_schema.tables
GROUP BY table_catalog, table_schema, table_type, data_source_format
ORDER BY table_catalog, table_schema, table_type
