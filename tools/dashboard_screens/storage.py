"""Storage tab (web/src/tabs/tab_storage.tsx): Tables, Maintenance. Whole metastore -- the catalog
checks here carry no workspace_id; the predictive-optimization checks read usage history and do,
so {ws} applies only to those."""
from tools.dashboard_kit import INT, MONEY, NUM, PCT, bar, kpi, table


def build(d):
    tables(d)
    maintenance(d)


def tables(d):
    p = d.page("tables")
    p.kpis(
        kpi("Tables discovered", """
SELECT count(*) AS value,
       concat('Top type: ', COALESCE((SELECT table_type FROM {S}.storage_target_table_discovery
              GROUP BY table_type ORDER BY count(*) DESC LIMIT 1), '-')) AS target
FROM {S}.storage_target_table_discovery
""", target="target", how="Tables and views found by Unity Catalog discovery this window; top object type below."),
        kpi("Tables and views by type", """
SELECT COALESCE(SUM(table_count), 0) AS value, 'Includes tables + views' AS target
FROM {S}.table_inventory_type
""", target="target", how="Tables and views in the metastore, from information_schema, all types summed."),
        kpi("Not altered recently", """
WITH r AS (SELECT * FROM {S}.access_dead_table_candidates)
SELECT (SELECT count_if(status IN ('CRITICAL', 'WARN')) FROM r) AS value,
       COALESCE((SELECT concat('Worst: ', table_catalog, '.', table_schema, '.', table_name, ', ',
                                CAST(days_since_altered AS STRING), ' days since altered') FROM r
                 WHERE status IN ('CRITICAL', 'WARN') ORDER BY days_since_altered DESC LIMIT 1), 'None flagged') AS target
""", target="target", tone="warn",
            how="Tables flagged critical or warn for going too long without a write; worst by days since altered below."),
        kpi("Fastest-growing tables", """
WITH r AS (SELECT * FROM {S}.storage_growth)
SELECT (SELECT count_if(status IN ('CRITICAL', 'WARN') AND NOT dropped_in_window) FROM r) AS value,
       concat(CAST((SELECT count_if(dropped_in_window) FROM r) AS STRING), ' dropped; worst: ',
              COALESCE((SELECT concat(full_name, ', +', CAST(ROUND(growth_bytes / 1073741824.0, 1) AS STRING), ' GB')
                        FROM r WHERE status IN ('CRITICAL', 'WARN') AND NOT dropped_in_window
                        ORDER BY growth_pct DESC LIMIT 1), 'none')) AS target
""", target="target", tone="warn",
            how="Tables that grew fastest this window (critical or warn), tables dropped since excluded; worst growth % below."),
    )
    p.charts(
        bar("Tables by type", """
SELECT table_type, SUM(table_count) AS tables
FROM {S}.table_inventory_type
GROUP BY 1 ORDER BY 2 DESC
""", "table_type", "tables", fmt=INT, category_title="Type", value_title="Tables",
            how="Table and view counts from information_schema, by object type."),
        bar("Oldest untouched tables", """
SELECT concat(table_catalog, '.', table_schema, '.', table_name) AS tbl, max(days_since_altered) AS days
FROM {S}.access_dead_table_candidates
WHERE status IN ('CRITICAL', 'WARN')
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "tbl", "days", fmt=NUM, category_title="Table", value_title="Days since altered",
            how="Tables flagged critical or warn for staleness, by days since the last write, top 8."),
    )
    p.table(table("Tables nobody read", """
SELECT concat(table_catalog, '.', table_schema, '.', table_name) AS tbl,
       COALESCE(table_type, '-') AS table_type,
       CASE WHEN days_since_last_write IS NULL THEN 'none on record'
            ELSE concat(CAST(days_since_last_write AS STRING), ' days ago') END AS last_write,
       ROUND(active_gb, 1) AS size_gb,
       ROUND(est_total_usd_month * {disc}, 0) AS usd_month
FROM {S}.storage_unused_table_cost
WHERE NOT is_other
ORDER BY est_total_usd_month DESC NULLS LAST
LIMIT 50
""", [("tbl", "Table"), ("table_type", "Type"), ("last_write", "Last write"), ("size_gb", "Size GB", NUM),
      ("usd_month", "$ a month", MONEY)],
      how="Tables with no query or job read on record this window, ranked by estimated storage + compute $ a month."))
    p.charts(
        bar("Growth, fastest first", """
SELECT full_name, ROUND(SUM(growth_bytes) / 1073741824.0, 1) AS gb
FROM {S}.storage_growth
WHERE status IN ('CRITICAL', 'WARN') AND NOT dropped_in_window
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "full_name", "gb", fmt=NUM, category_title="Table", value_title="GB grown",
            how="Table size growth in GB this window, tables flagged critical or warn, dropped tables excluded, top 8."),
    )
    p.checks()


def maintenance(d):
    p = d.page("maintenance")
    p.kpis(
        kpi("Maintenance DBU burn", """
WITH r AS (SELECT * FROM {named:po_maintenance_cost_by_table} WHERE {ws}),
     f AS (SELECT * FROM {named:po_failure_reasons} WHERE {ws})
SELECT (SELECT ROUND(COALESCE(SUM(estimated_dbu), 0), 0) FROM r) AS value,
       concat(CAST((SELECT COALESCE(SUM(failed_operations), 0) FROM f) AS STRING), ' ops failed outright; worst: ',
              COALESCE((SELECT concat(full_name, ', ', CAST(ROUND(s, 0) AS STRING), ' DBU')
                        FROM (SELECT concat(catalog_name, '.', schema_name, '.', table_name) AS full_name,
                                     SUM(estimated_dbu) AS s FROM r GROUP BY 1
                              ORDER BY s DESC LIMIT 1) x), 'none')) AS target
""", target="target", how="DBUs spent on OPTIMIZE/VACUUM/clustering maintenance this window; worst table and failed-op count below."),
        kpi("Vacuum reclaiming nothing", """
WITH r AS (SELECT * FROM {named:po_vacuum_reclaimed_bytes} WHERE {ws})
SELECT (SELECT count(DISTINCT table_name) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS value,
       concat(CAST((SELECT ROUND(COALESCE(SUM(vacuum_estimated_dbu), 0), 1) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS STRING),
              ' DBU on zero-reclaim VACUUMs') AS target
""", target="target", tone="warn",
            how="Tables where VACUUM ran but freed no bytes (critical or warn); DBUs spent on those runs below."),
        kpi("Clustering activity", """
WITH r AS (SELECT * FROM {named:po_clustering_activity} WHERE {ws}),
     c AS (SELECT * FROM {named:po_clustering_column_churn} WHERE {ws})
SELECT (SELECT ROUND(COALESCE(SUM(clustering_estimated_dbu), 0), 0) FROM r) AS value,
       concat(CAST((SELECT count(DISTINCT table_name) FROM c WHERE lower(has_column_selection_changed) = 'true') AS STRING),
              ' tables re-picking clustering keys') AS target
""", target="target", how="DBUs spent on liquid-clustering maintenance; tables that changed clustering columns this window below."),
        kpi("Data-skipping backfill", """
WITH r AS (SELECT * FROM {named:po_data_skipping_backfill} WHERE {ws})
SELECT (SELECT count(*) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS value,
       COALESCE((SELECT concat('Worst: ', table_name) FROM r WHERE status IN ('CRITICAL', 'WARN')
                 ORDER BY scanned_bytes DESC LIMIT 1), 'None flagged') AS target
""", target="target", tone="warn",
            how="Tables re-scanned to backfill data-skipping stats after a column change (critical or warn); worst below."),
    )
    p.charts(
        bar("Maintenance DBU by table", """
SELECT concat(catalog_name, '.', schema_name, '.', table_name) AS tbl, ROUND(SUM(estimated_dbu), 1) AS dbu
FROM {named:po_maintenance_cost_by_table}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "tbl", "dbu", fmt=NUM, category_title="Table", value_title="DBU",
            how="DBUs spent on table maintenance (OPTIMIZE/VACUUM/clustering), by catalog.schema.table, top 10."),
    )
    p.table(table("Small files", """
SELECT full_name, status, active_files, ROUND(avg_file_size_mb, 1) AS avg_file_mb
FROM {S}.storage_small_files
WHERE status IN ('CRITICAL', 'WARN')
ORDER BY active_files DESC
LIMIT 50
""", [("full_name", "Table"), ("status", "Status"), ("active_files", "Files", INT), ("avg_file_mb", "Avg file MB", NUM)],
      how="Tables flagged for too many small files (critical or warn), ranked by active file count."))
    p.table(table("Predictive optimization coverage, by catalog", """
SELECT catalog_name, status, tables_without_po, tables_total,
       ROUND(pct_bytes_without_po, 0) AS pct_bytes_without_po
FROM {S}.storage_po_coverage
WHERE section = 'catalog_summary' AND status <> 'OK'
ORDER BY pct_bytes_without_po DESC
LIMIT 30
""", [("catalog_name", "Catalog"), ("status", "Status"), ("tables_without_po", "Tables w/o PO", INT),
      ("tables_total", "Tables total", INT), ("pct_bytes_without_po", "% bytes w/o PO", PCT)],
      how="Share of table bytes not covered by predictive optimization, by catalog; catalogs fully covered excluded."))
    p.charts(
        bar("Largest tables missing predictive optimization", """
SELECT full_name, ROUND(SUM(active_bytes_of_table) / 1073741824.0, 1) AS gb
FROM {S}.storage_po_coverage
WHERE section = 'largest_table_without_po'
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "full_name", "gb", fmt=NUM, category_title="Table", value_title="GB",
            how="Largest tables with predictive optimization off, by size in GB, top 8."),
    )
    p.table(table("Predictive optimization failures", """
SELECT full_name, status, COALESCE(reason, '-') AS reason, failed_operations,
       ROUND(estimated_dbus_spent, 1) AS dbu
FROM {named:po_failure_reasons}
WHERE {ws} AND status <> 'OK' AND NOT is_other
ORDER BY failed_operations DESC
LIMIT 50
""", [("full_name", "Table"), ("status", "Status"), ("reason", "Reason"), ("failed_operations", "Failed ops", INT),
      ("dbu", "Est. DBU", NUM)],
      how="Tables where an OPTIMIZE/VACUUM/clustering run failed outright, ranked by failed-operation count."))
    p.checks()
