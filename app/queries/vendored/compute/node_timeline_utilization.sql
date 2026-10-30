-- query_id: node_timeline_utilization
-- title: Per-cluster CPU / memory / network utilization profile
-- domain: compute   tier: lite
-- reads: system.compute.node_timeline
-- requires: SELECT on system.compute; GA
-- empty_if: compute_scope_gap
-- params: :period_days (default 30) rolling window in days, capped at 90 in-SQL by node_timeline retention; :min_slices (default 60) minimum node-minutes before a cluster is judged (fewer -> NOT_ASSESSED); :oversized_cpu_pct (default 20) avg CPU percent below which a cluster looks oversized; :oversized_mem_pct (default 30) avg memory percent below which a cluster looks oversized; :top_n (default 100000) row cap
-- confidence: confirmed
-- confidence_note: node_timeline utilization columns verified in a live workspace.
-- read_this: One row = one cluster + node_type + driver/worker role, with average and peak CPU and memory over the window. avg_cpu_pct and avg_mem_pct are the columns that matter: both low over many slices means the node is oversized for its workload. This is a right-sizing profile; compute_idle_node_ratio is the dedicated idle finding.
-- healthy: avg_cpu_pct at/above :oversized_cpu_pct OR avg_mem_pct at/above :oversized_mem_pct - the box is being used (field heuristic).
-- investigate_if: avg_cpu_pct below :oversized_cpu_pct AND avg_mem_pct below :oversized_mem_pct across enough slices (WARN = oversized candidate) - field heuristic; peak_* matters too, a low average with a high peak may just be bursty.
-- actions: 1) confirm the low utilization is steady (not a bursty job) from peak_cpu_pct / peak_mem_pct before acting (free); 2) drop to a smaller node type or fewer workers, or enable autoscaling (config); 3) switch to a memory- or compute-optimized family that matches the real bottleneck (spend).
-- next: compute_idle_node_ratio (to rank the same clusters by idle-slice ratio and wasted list-price $), cost_by_compute_resource (to attach billed DBUs to a right-sizing candidate)
-- caveats: node_timeline retention is 90 DAYS ONLY, so :period_days is capped at LEAST(:period_days, 90) in SQL - a longer trend silently truncates (read it as "assessed over the last 90 days at most"). Nodes that ran under ~10 minutes MAY NOT APPEAR (short-job blind spot). Classic compute ONLY - no SQL-warehouse or serverless node utilization exists in this table. mem_used_percent includes background processes, so it is not pure workload memory. disk_free_bytes_per_mount_point is a map and is not selected. Utilization is per region. Ordered worst-status-first (WARN, then NOT_ASSESSED, then OK, each by avg_cpu_pct ASC) before :top_n is applied - the old plain "avg_cpu_pct ASC" order kept only the least-busy groups when the cap bound, so a real account's genuinely-busy (OK) clusters were cut first and the finding could misread as all-idle; :top_n defaults to 100000 (was 200) so a low build-time cap does not silently cut the flagged rows that matter, with no thresholds.yml override needed. not_assessed_reason says why a node was not judged, e.g. '10 of 15 minutes needed'.
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
         WHEN COUNT(*) < :min_slices THEN 'NOT_ASSESSED'
         WHEN AVG(cpu_user_percent + cpu_system_percent) < :oversized_cpu_pct
          AND AVG(mem_used_percent) < :oversized_mem_pct THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN COUNT(*) < :min_slices
            THEN concat(CAST(COUNT(*) AS STRING), ' of ', CAST(:min_slices AS STRING), ' minutes needed')
       END AS not_assessed_reason
FROM system.compute.node_timeline
WHERE start_time >= dateadd(DAY, -LEAST(:period_days, 90), current_date())
GROUP BY cluster_id, node_type, driver
-- Worst-status-first: WARN (oversized candidate), then NOT_ASSESSED, then OK, each by
-- avg_cpu_pct ASC - so a low :top_n never cuts the busy (OK) clusters first and leaves only the
-- idle ones, which would make the whole finding misread as all-idle.
ORDER BY
  CASE status WHEN 'WARN' THEN 0 WHEN 'NOT_ASSESSED' THEN 1 ELSE 2 END,
  avg_cpu_pct ASC
LIMIT :top_n
