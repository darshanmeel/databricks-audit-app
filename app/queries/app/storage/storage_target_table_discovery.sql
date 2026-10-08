-- query_id: storage_target_table_discovery
-- title: Table discovery for per-table analysis - candidates for time-travel and Iceberg migration
-- domain: storage   tier: standard
-- reads: system.information_schema.tables
-- requires: SELECT on system.information_schema; Unity Catalog required
-- params: none (config snapshot, no time window)
-- confidence: needs_confirmation
-- confidence_note: table_catalog, table_schema, table_name, table_type are standard
--   information_schema columns; data_source_format is plausible but unverified until confirmed
--   in your own workspace. The per-table queries (table_props_time_travel_config,
--   storage_breakdown_analyze, iceberg_uniform_metadata) require running against each table
--   discovered here.
-- read_this: One row = a fully-qualified table. The target_table column is ready to
--   paste into the :target_table param of the three per-table queries; table_type excludes VIEW
--   rows only (table_type <> 'VIEW', so MANAGED, EXTERNAL, and any other non-VIEW type pass
--   through). Use this to find tables eligible for time-travel configuration, UniFORM
--   conversion, or Iceberg migration.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (discovery; each row targets a separate per-table analysis)
-- next: table_inventory_type (for a catalog-wide summary by type and format),
--   po_maintenance_cost_by_table (to cross-check which tables qualify for Predictive Optimization)
-- caveats: information_schema is privilege-aware - you see only tables/views your credentials
--   can access, so this list is a floor, not the whole metastore. data_source_format (DELTA vs
--   ICEBERG) is plausible but unverified until confirmed in your workspace. This carries no
--   size columns; pair with storage_breakdown_analyze for actual bytes. Ported from the
--   MIT-licensed reference (c) 2026 darshanmeel.
SELECT
    table_catalog || '.' || table_schema || '.' || table_name AS target_table,
    table_catalog,
    table_schema,
    table_name,
    table_type,
    data_source_format
FROM system.information_schema.tables
WHERE table_type <> 'VIEW'
ORDER BY table_catalog, table_schema, table_name
