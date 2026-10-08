{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:compute', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/compute/node_timeline_utilization.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT cluster_id, node_type, driver,
       COUNT(*) AS minute_rows,
       MIN(start_time) AS first_minute, MAX(end_time) AS last_minute,
       AVG(cpu_user_percent + cpu_system_percent) AS avg_cpu_pct,
       MAX(cpu_user_percent + cpu_system_percent) AS peak_cpu_pct,
       AVG(mem_used_percent) AS avg_mem_pct, MAX(mem_used_percent) AS peak_mem_pct,
       AVG(cpu_wait_percent) AS avg_cpu_wait_pct,
       SUM(network_sent_bytes)     AS total_network_sent_bytes,
       SUM(network_received_bytes) AS total_network_received_bytes,
       -- status: oversized-candidate band (field heuristic). Too few slices -> NOT_ASSESSED.
       CASE
         WHEN COUNT(*) < {{ param('node_timeline_utilization', 'min_slices', 60) }} THEN 'NOT_ASSESSED'
         WHEN AVG(cpu_user_percent + cpu_system_percent) < {{ param('node_timeline_utilization', 'oversized_cpu_pct', 20) }}
          AND AVG(mem_used_percent) < {{ param('node_timeline_utilization', 'oversized_mem_pct', 30) }} THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN COUNT(*) < {{ param('node_timeline_utilization', 'min_slices', 60) }}
            THEN concat(CAST(COUNT(*) AS STRING), ' of ', CAST({{ param('node_timeline_utilization', 'min_slices', 60) }} AS STRING), ' minutes needed')
       END AS not_assessed_reason
FROM {{ source('system_compute', 'node_timeline') }}
WHERE start_time >= dateadd(DAY, -LEAST({{ w }}, 90), {{ audit_today() }})
GROUP BY cluster_id, node_type, driver
ORDER BY
  CASE status WHEN 'WARN' THEN 0 WHEN 'NOT_ASSESSED' THEN 1 ELSE 2 END,
  avg_cpu_pct ASC
LIMIT {{ param('node_timeline_utilization', 'top_n', 100000) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
