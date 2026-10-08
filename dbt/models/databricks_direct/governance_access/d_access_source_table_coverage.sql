{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/app/governance_access/access_source_table_coverage.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH unioned AS (
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'audit' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_date) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) < ({{ w }} - 1) THEN 'WARN'
            WHEN datediff({{ audit_today() }}, CAST(MAX(event_date) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM {{ source('system_access', 'audit') }}
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'column_lineage' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_access', 'column_lineage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'inbound_network' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_access', 'inbound_network') }}
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'outbound_network' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_access', 'outbound_network') }}
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'table_lineage' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_access', 'table_lineage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'workspaces_latest' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_access', 'workspaces_latest') }}
UNION ALL
SELECT 'system' AS catalog_name, 'ai_gateway' AS schema_name, 'usage' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_ai_gateway', 'usage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'attributed_usage' AS table_name,
       TRUE AS has_time_column, 'usage_date' AS time_column_name,
       SUM(CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(usage_date) AS STRING) AS min_time,
       CAST(MAX(usage_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(usage_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(usage_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(usage_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(usage_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_billing', 'attributed_usage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'list_prices' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ list_prices() }} list_prices
UNION ALL
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'usage' AS table_name,
       TRUE AS has_time_column, 'usage_date' AS time_column_name,
       SUM(CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(usage_date) AS STRING) AS min_time,
       CAST(MAX(usage_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(usage_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(usage_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(usage_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(usage_date) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN usage_date >= date_sub({{ audit_today() }}, {{ w }}) THEN CAST(usage_date AS DATE) END) < ({{ w }} - 1) THEN 'WARN'
            WHEN datediff({{ audit_today() }}, CAST(MAX(usage_date) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM {{ source('system_billing', 'usage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'clusters' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_compute', 'clusters') }}
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'instance_events' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_compute', 'instance_events') }}
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'instance_pools' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_compute', 'instance_pools') }}
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'node_timeline' AS table_name,
       TRUE AS has_time_column, 'start_time' AS time_column_name,
       SUM(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(start_time) AS STRING) AS min_time,
       CAST(MAX(start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) < ({{ w }} - 1) THEN 'WARN'
            WHEN datediff({{ audit_today() }}, CAST(MAX(start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM {{ source('system_compute', 'node_timeline') }}
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'node_types' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_compute', 'node_types') }}
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'warehouse_events' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_compute', 'warehouse_events') }}
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'warehouses' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_compute', 'warehouses') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'catalog_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'catalog_privileges') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'column_masks' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'column_masks') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'column_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'column_tags') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'connection_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'connection_privileges') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'credential_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'credential_privileges') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'external_location_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'external_location_privileges') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'row_filters' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'row_filters') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'schema_privileges') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_share_usage' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'schema_share_usage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'schema_tags') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'share_recipient_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'share_recipient_privileges') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'shares' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'shares') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'table_privileges') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_share_usage' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'table_share_usage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'table_tags') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'tables' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'tables') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'views' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'views') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'volume_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'volume_tags') }}
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'volumes' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM {{ source('system_information_schema', 'volumes') }}
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_run_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) < ({{ w }} - 1) THEN 'WARN'
            WHEN datediff({{ audit_today() }}, CAST(MAX(period_start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM {{ source('system_lakeflow', 'job_run_timeline') }}
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_task_run_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_tasks' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_lakeflow', 'job_tasks') }}
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'jobs' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_lakeflow', 'jobs') }}
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'pipeline_update_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_lakeflow', 'pipeline_update_timeline') }}
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'pipelines' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_lakeflow', 'pipelines') }}
UNION ALL
SELECT 'system' AS catalog_name, 'query' AS schema_name, 'history' AS table_name,
       TRUE AS has_time_column, 'start_time' AS time_column_name,
       SUM(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(start_time) AS STRING) AS min_time,
       CAST(MAX(start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN start_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(start_time AS DATE) END) < ({{ w }} - 1) THEN 'WARN'
            WHEN datediff({{ audit_today() }}, CAST(MAX(start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM {{ source('system_query', 'history') }}
UNION ALL
SELECT 'system' AS catalog_name, 'serving' AS schema_name, 'endpoint_usage' AS table_name,
       TRUE AS has_time_column, 'request_time' AS time_column_name,
       SUM(CASE WHEN request_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(request_time) AS STRING) AS min_time,
       CAST(MAX(request_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN request_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(request_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN request_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(request_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN request_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(request_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(request_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_serving', 'endpoint_usage') }}
UNION ALL
SELECT 'system' AS catalog_name, 'serving' AS schema_name, 'served_entities' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub({{ audit_now() }}, {{ w }}) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff({{ audit_today() }}, CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM {{ source('system_serving', 'served_entities') }}
)
SELECT * FROM unioned
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, schema_name, table_name
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
