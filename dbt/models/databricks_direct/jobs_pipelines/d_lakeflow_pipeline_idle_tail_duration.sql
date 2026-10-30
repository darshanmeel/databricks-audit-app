{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_idle_tail_duration.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
),
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.dlt_pipeline_id AS dlt_pipeline_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND u.usage_date <  {{ audit_today() }}
  GROUP BY u.workspace_id, u.usage_metadata.dlt_pipeline_id
),
billed_hours AS (
  SELECT u.workspace_id, u.usage_metadata.dlt_pipeline_id AS pipeline_id, u.record_id,
         u.usage_start_time, u.usage_end_time, u.usage_quantity,
         COALESCE(u.product_features.is_serverless, FALSE) AS serverless
  FROM {{ source('system_billing', 'usage') }} u
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND u.usage_date <  {{ audit_today() }}
),
billed_active AS (
  -- whether any update slice overlaps the billed row's hour; serverless rows never linger
  SELECT b.workspace_id, b.pipeline_id, b.record_id, b.usage_quantity, b.serverless,
         MAX(CASE WHEN s.pipeline_id IS NOT NULL THEN 1 ELSE 0 END) AS overlapped
  FROM billed_hours b
  LEFT JOIN {{ source('system_lakeflow', 'pipeline_update_timeline') }} s
    ON s.workspace_id = b.workspace_id AND s.pipeline_id = b.pipeline_id
   AND s.period_start_time < b.usage_end_time AND s.period_end_time > b.usage_start_time
  GROUP BY b.workspace_id, b.pipeline_id, b.record_id, b.usage_quantity, b.serverless
),
idle AS (
  SELECT workspace_id, pipeline_id,
         SUM(CASE WHEN overlapped = 0 AND NOT serverless THEN usage_quantity ELSE 0 END) AS idle_dbus
  FROM billed_active
  GROUP BY workspace_id, pipeline_id
)
SELECT u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development,
       COUNT(DISTINCT CASE WHEN u.result_state IS NOT NULL THEN u.update_id END) AS updates,
       -- every hourly slice counts: an update over an hour writes one row per hour, the result on the last
       SUM(unix_timestamp(u.period_end_time) - unix_timestamp(u.period_start_time)) AS active_seconds_total,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       COALESCE(MAX(i.idle_dbus), 0) AS idle_dbus,
       MAX(i.idle_dbus) / NULLIF(MAX(cr.net_dbus), 0) AS idle_share,
       -- status: worst-first band on DBUs billed outside update time (field heuristic; {{ param('lakeflow_pipeline_idle_tail_duration', 'warn_idle_dbus', 100) }} / {{ param('lakeflow_pipeline_idle_tail_duration', 'crit_idle_dbus', 500) }}).
       CASE
         WHEN p.setting_continuous = true THEN 'NOT_ASSESSED'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= {{ param('lakeflow_pipeline_idle_tail_duration', 'crit_idle_dbus', 500) }} THEN 'CRITICAL'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= {{ param('lakeflow_pipeline_idle_tail_duration', 'warn_idle_dbus', 100) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_lakeflow', 'pipeline_update_timeline') }} u
LEFT JOIN (
  SELECT workspace_id, pipeline_id, pipeline_type,
         settings.continuous  AS setting_continuous,
         settings.development AS setting_development
  FROM {{ source('system_lakeflow', 'pipelines') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
) p
  ON u.workspace_id = p.workspace_id AND u.pipeline_id = p.pipeline_id
LEFT JOIN cost_rollup cr
  ON u.workspace_id = cr.workspace_id AND u.pipeline_id = cr.dlt_pipeline_id
LEFT JOIN idle i
  ON u.workspace_id = i.workspace_id AND u.pipeline_id = i.pipeline_id
WHERE u.period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND u.period_end_time < date_trunc('DAY', {{ audit_now() }})
GROUP BY u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development
ORDER BY idle_dbus DESC, net_dbus DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
