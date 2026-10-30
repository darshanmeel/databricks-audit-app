-- generated from dbt/models/databricks_direct/compute/d_node_timeline_utilization.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/node_timeline_utilization.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
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
         WHEN COUNT(*) < 15 THEN 'NOT_ASSESSED'
         WHEN AVG(cpu_user_percent + cpu_system_percent) < 20
          AND AVG(mem_used_percent) < 30 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN COUNT(*) < 15
            THEN concat(CAST(COUNT(*) AS STRING), ' of ', CAST(15 AS STRING), ' minutes needed')
       END AS not_assessed_reason
FROM `system`.`compute`.`node_timeline`
WHERE start_time >= dateadd(DAY, -LEAST(__WINDOW_DAYS__, 90), __AS_OF_DATE__)
GROUP BY cluster_id, node_type, driver
ORDER BY
  CASE status WHEN 'WARN' THEN 0 WHEN 'NOT_ASSESSED' THEN 1 ELSE 2 END,
  avg_cpu_pct ASC
LIMIT 100000
) q
