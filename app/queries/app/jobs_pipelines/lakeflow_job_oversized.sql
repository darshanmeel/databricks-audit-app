-- query_id: lakeflow_job_oversized
-- title: Jobs on bigger machines than they need - the opposite of lakeflow_job_compute_pressure
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_task_run_timeline, system.compute.node_timeline,
--   system.compute.clusters, system.compute.node_types, system.lakeflow.jobs,
--   system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.lakeflow, system.compute AND system.billing; GA. SCOPE LIMIT:
--   node_timeline holds classic all-purpose, job, Lakeflow pipeline and pipeline-maintenance
--   compute ONLY, so a job whose tasks ran on serverless or a SQL warehouse has no hardware
--   profile and comes back NOT_ASSESSED with a named reason, never as "not oversized"
-- empty_if: schema_not_enabled, compute_scope_gap, retention_window, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days - the telemetry side is capped in-SQL
--   at LEAST(:period_days, 90) by node_timeline retention, the cost side is not (see caveats);
--   :min_runs (default 3) minimum job-cluster runs carrying node telemetry pooled over the whole
--   job before it is judged (fewer -> NOT_ASSESSED); :oversized_cpu_pct (default 30) worker CPU
--   p90 percent below which the job's CPU is judged oversized; :oversized_mem_pct (default 40)
--   worker memory PEAK percent below which the job's memory is judged oversized;
--   :oversized_swap_pct (default 1) worker swap peak percent at/above which the job counts as
--   swapping and is never oversized - set above 0 so background swap noise doesn't block it;
--   :warn_saving_usd
--   (default 25) estimated saving in the window at/above which a job flags WARN;
--   :crit_saving_usd (default 200) the same at/above which a job flags CRITICAL; :top_n (default
--   100000) row cap - far above any account's job count, because this is a verdict per job and a
--   cut row would read as "not flagged"
-- confidence: needs_confirmation
-- confidence_note: Reuses the exact node-timeline telemetry (worker CPU/memory percentiles,
--   node type, workers configured vs seen, the dedup and single-node rules)
--   lakeflow_job_compute_pressure and task_cluster_utilization already carry, and the exact
--   per-run/per-job effective-list-price DBU join (DEC-66.1) lakeflow_job_run_cost /
--   lakeflow_job_cost_summary already carry, but this query has not itself been run against a
--   live workspace. Confirm on your account: 1) a job you know runs on a fixed-size or
--   lightly-loaded job cluster shows a plausible worker_cpu_p90_pct / worker_mem_p90_pct here;
--   2) job_list_cost_usd is close to that same job's own total in lakeflow_job_cost_summary for
--   the same window (see caveats for why the two can legitimately differ); 3) a serverless-only
--   job shows NOT_ASSESSED with no_cluster_recorded rather than an error.
-- read_this: One row = one job with at least one task run starting in the window: pooled worker
--   CPU p90, memory p90 and their peaks, plus the peak swap, across every job-cluster run's
--   deduplicated node-minutes (the same telemetry lakeflow_job_compute_pressure builds), the
--   busiest run's cluster node type and its workers configured vs seen, and the job's own compute
--   dollars at list price over the window (the same DEC-66.1 join lakeflow_job_cost_summary uses).
--   oversized is TRUE when CPU p90 and memory PEAK sit under their thresholds and no worker ever
--   swapped (:oversized_swap_pct), on a job that is not single-node and has at least :min_runs
--   runs with telemetry. est_saving_usd_list is a rough, MEASURED-utilisation estimate - a
--   fraction of the job's own list-priced dollars, never a forecast of what a resize would
--   actually save (see caveats). suggested_action names the lever.
-- healthy: oversized = FALSE (including every single-node job, which this check never sizes
--   down), or oversized = TRUE with est_saving_usd_list under :warn_saving_usd - field
--   heuristic, every threshold is a header param.
-- investigate_if: oversized = TRUE with est_saving_usd_list at/above :warn_saving_usd (WARN) or
--   :crit_saving_usd (CRITICAL) in the window. NOT_ASSESSED is not a pass: read
--   not_assessed_reason.
-- actions: 1) suggested_action = 'one node size down' - move worker_node_type to the next
--   smaller size in the same family; a node one size down roughly halves cores and memory,
--   which is what the est_saving_usd_list x0.5 case assumes (config); 2) suggested_action =
--   'fewer workers / lower autoscale max' - the job already runs fewer workers than its
--   configured ceiling, so lower max_autoscale_workers (or the fixed worker count) to match what
--   it actually uses, before touching the node type (config); 3) confirm on a quiet run first -
--   a job that is oversized most of the time can still spike on an unusual input; read
--   runs_with_telemetry with worker_mem_peak_pct / worker_cpu_peak_pct before resizing, and watch
--   the next few runs after a change (free).
-- next: lakeflow_job_compute_pressure (the opposite check - is this job UNDER-sized),
--   lakeflow_job_cost_summary (the job's per-run cost detail behind job_list_cost_usd),
--   task_cluster_utilization (per-run telemetry for any one run over two hours),
--   classic_clusters_config_current (the cluster's current configuration)
-- not_assessed_reasons: task_not_executed: every task run of every run was SKIPPED or BLOCKED
--   and never ran; no_cluster_recorded: the job's tasks ran on compute this check cannot see
--   (serverless, or a run from before cluster ids were recorded); no_node_timeline_rows: a
--   cluster was recorded but no matching node telemetry exists for it; too_few_runs: fewer than
--   :min_runs job-cluster runs had node telemetry pooled, so a low reading is not backed by
--   enough runs to size down on
-- caveats: SCOPE - identical to lakeflow_job_compute_pressure: system.compute.node_timeline
--   records classic all-purpose, job, Lakeflow pipeline and pipeline-maintenance compute only
--   (cluster_source UI / API / JOB / PIPELINE / PIPELINE_MAINTENANCE), never serverless or SQL
--   warehouses, so a job with no classic telemetry is NOT_ASSESSED (no_cluster_recorded /
--   no_node_timeline_rows), not "not oversized". POOLING - every run of the job in the window is
--   pooled before the p90s are taken (dedup: parallel tasks of one run sharing one cluster count
--   each node-minute once), exactly as lakeflow_job_compute_pressure pools; a job that is
--   genuinely oversized most weeks but spikes once can still average under the thresholds - read
--   runs_with_telemetry, this is a pooled verdict, not a per-run one. PEAK AND SWAP - memory is
--   judged on its highest minute, not p90, because a smaller node that cannot hold the busiest
--   minute spills or fails; one size down roughly halves memory, so a peak under 40% stays under
--   about 80% after the change. CPU stays on p90: a CPU peak only slows a run. node_timeline rows
--   are one-minute averages, so a spike of a few seconds can still be missed. Spill is not in the
--   system tables for job clusters; worker swap (mem_swap_percent at/above :oversized_swap_pct) is
--   the early sign used instead, and a job that swapped is never oversized. SINGLE-NODE jobs (no
--   worker minutes at all) are judged (single_node = TRUE, worker_cpu_p90_pct /
--   worker_mem_p90_pct are then NULL, both driver-only numbers) but are NEVER oversized here:
--   the lever this check names is a smaller WORKER node type or fewer workers, neither of which
--   applies to a one-node cluster the same way. REFERENCE CLUSTER - worker_node_type,
--   worker_cores, worker_memory_gb and cluster_source describe latest_cluster_id (the classic
--   cluster - one this query actually has node telemetry for - of the latest-starting task
--   run); workers_configured_max and worker_nodes_seen_max instead describe the job's BUSIEST
--   run and cluster (most workers running at once; ties: latest run, then cluster_id), which can
--   be a different cluster on a job that used more than one - suggested_action is decided from
--   that busiest pair. SAVING ESTIMATE - est_saving_usd_list is job_list_cost_usd (the job's own
--   list-priced compute dollars over the window) times 0.5 when CPU p90 and memory peak both sit
--   under half their thresholds (a node one size down roughly halves both cores and memory, so the whole job
--   cluster's dollars roughly halve too), else times 0.25 for a lighter, still-real margin; it is
--   a rough estimate from MEASURED utilisation, not a forecast of what resizing would actually
--   save - actual savings depend on the node family's real size steps, on whether other jobs
--   share the same cluster, and on whether the input volume stays the same. A NULL
--   job_list_cost_usd (no cost usage matched, see PRICE below) leaves est_saving_usd_list NULL
--   too rather than a fabricated number, and such a row reads OK on the dollar bands (a NULL
--   never meets a dollar band) although oversized may still be TRUE - read oversized itself, not
--   only status, before deciding nothing is there. PRICE - job_list_cost_usd and price_basis
--   reuse lakeflow_job_run_cost / lakeflow_job_cost_summary's own run_usage CTE unchanged
--   (usage_metadata.job_id/job_run_id, DEC-66.1 effective-list price, SUM(usage_quantity *
--   list_rate)) rolled up to the job: an estimate at list price, never a negotiated or billed
--   dollar, and, per that query's own attribution caveat, EXCLUDING any job_id-attributed usage
--   row with no job_run_id - so job_list_cost_usd can be LESS than this same job's whole-window
--   total in cost_by_job. price_basis is left NULL (not defaulted to 'priced') when the job's
--   runs matched NO system.billing.usage row carrying both ids in the window - genuinely no cost
--   data joined, never read as a real $0; 'free' is a real $0 (every matched SKU was
--   FREE_USAGE), 'unpriced' means at least one matched SKU had no list_prices row (job_list_cost_usd
--   then understates the job). WINDOW MISMATCH - the CPU/memory telemetry is judged over at most
--   the last 90 days (node_timeline retention, the same LEAST(:period_days, 90) cap
--   lakeflow_job_compute_pressure uses), while job_list_cost_usd is priced over the FULL
--   :period_days (system.billing.usage has its own, much longer retention); with :period_days
--   over 90 the judged runs are a recent subset of what job_list_cost_usd bills, so
--   est_saving_usd_list can then overstate what a smaller node type would actually have saved
--   across the whole priced window - keep :period_days at or under 90 to avoid this. No
--   identities are emitted, and there is no forecast anywhere in this query: every number is
--   measured over runs that already happened. :top_n orders worst (highest est_saving_usd_list)
--   first, so a cut row is always the smallest saving, never the one you most needed to see.
WITH task_runs AS (
  -- one row per TASK RUN starting in the window (run_id here is the task run id)
  SELECT workspace_id, job_id, job_run_id,
         run_id                              AS task_run_id,
         task_key,
         MIN(period_start_time)              AS task_start,
         MAX(period_end_time)                AS task_last_seen,
         MAX(result_state)                   AS task_result_state,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(compute_ids, 1)) AS cluster_id
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -LEAST(:period_days, 90), current_date())
  GROUP BY workspace_id, job_id, job_run_id, run_id, task_key
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_run_id, task_key, task_start, cluster_id,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (task_result_state IS NOT NULL AND task_start = task_last_seen)) AS task_not_executed,
         -- the telemetry window: the task's observed wall clock, open-ended while in flight
         CASE WHEN task_result_state IS NULL THEN current_timestamp() ELSE task_last_seen END AS task_end
  FROM task_runs
),
job_task_agg AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT CASE WHEN task_not_executed THEN NULL ELSE job_run_id END) AS runs_executed,
         COUNT(DISTINCT CASE WHEN task_not_executed THEN NULL
                             WHEN cluster_id IS NULL THEN NULL
                             ELSE job_run_id END)                                  AS runs_with_cluster
  FROM task_obs
  GROUP BY workspace_id, job_id
),
task_slices AS (
  -- every node-minute of each executed task run's cluster that overlaps that task run's window
  SELECT t.workspace_id, t.job_id, t.job_run_id, t.cluster_id,
         n.instance_id, n.start_time, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.mem_used_percent, n.mem_swap_percent
  FROM task_obs t
  JOIN system.compute.node_timeline n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST(:period_days, 90), current_date())
    AND NOT t.task_not_executed
),
job_slices AS (
  -- the dedupe: parallel tasks of one run on one shared cluster count each node-minute once
  SELECT DISTINCT workspace_id, job_id, job_run_id, cluster_id, instance_id, start_time, driver,
         cpu_pct, mem_used_percent, mem_swap_percent
  FROM task_slices
),
profile AS (
  -- the job's pooled hardware profile over its deduplicated node-minutes
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)                                AS runs_with_telemetry,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9)          AS worker_cpu_p90_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE cpu_pct END)                      AS worker_cpu_peak_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END)             AS worker_mem_peak_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END)             AS worker_swap_peak_pct
  FROM job_slices
  GROUP BY workspace_id, job_id
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_source, worker_node_type, worker_count, max_autoscale_workers
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
node_mem AS (
  -- memory and cores per node type (account-level reference table)
  SELECT node_type, MAX(memory_mb) AS memory_mb, MAX(core_count) AS core_count
  FROM system.compute.node_types
  GROUP BY node_type
),
latest_cluster AS (
  -- the classic cluster (one this query actually has node telemetry for) of the
  -- latest-starting task run (ties: cluster_id)
  SELECT t.workspace_id, t.job_id, t.cluster_id AS latest_cluster_id
  FROM task_obs t
  JOIN (SELECT DISTINCT workspace_id, job_id, cluster_id FROM job_slices) s
    ON  s.workspace_id = t.workspace_id
    AND s.job_id       = t.job_id
    AND s.cluster_id   = t.cluster_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY t.workspace_id, t.job_id ORDER BY t.task_start DESC, t.cluster_id
  ) = 1
),
run_starts AS (
  SELECT workspace_id, job_id, job_run_id, MIN(task_start) AS run_start
  FROM task_obs
  GROUP BY workspace_id, job_id, job_run_id
),
run_minutes AS (
  -- each deduplicated node-minute on its minute boundary, to count the workers running at once
  SELECT workspace_id, job_id, job_run_id, cluster_id, instance_id, driver,
         date_trunc('MINUTE', start_time) AS minute_start
  FROM job_slices
),
run_minute_workers AS (
  SELECT workspace_id, job_id, job_run_id, cluster_id, minute_start,
         COUNT(DISTINCT CASE WHEN driver THEN NULL ELSE instance_id END) AS workers_now
  FROM run_minutes
  GROUP BY workspace_id, job_id, job_run_id, cluster_id, minute_start
),
run_peak AS (
  -- the most workers running at once per run+cluster: churned or replaced instances never double-count
  SELECT workspace_id, job_id, job_run_id, cluster_id, MAX(workers_now) AS worker_nodes_peak
  FROM run_minute_workers
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
worker_ref AS (
  -- the job's BUSIEST run+cluster (most workers running at once; ties: latest run, then
  -- cluster_id): workers configured vs seen for the suggested_action lever
  SELECT k.workspace_id, k.job_id,
         k.worker_nodes_peak                                AS worker_nodes_seen_max,
         COALESCE(c.max_autoscale_workers, c.worker_count)  AS workers_configured_max
  FROM run_peak k
  LEFT JOIN run_starts s
    ON  s.workspace_id = k.workspace_id AND s.job_id = k.job_id AND s.job_run_id = k.job_run_id
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = k.workspace_id AND c.cluster_id = k.cluster_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY k.workspace_id, k.job_id
    ORDER BY k.worker_nodes_peak DESC, s.run_start DESC, k.cluster_id
  ) = 1
),
judged AS (
  SELECT a.workspace_id, a.job_id, a.runs_executed, a.runs_with_cluster,
         p.runs_with_telemetry, p.worker_cpu_p90_pct, p.worker_mem_p90_pct,
         p.worker_cpu_peak_pct, p.worker_mem_peak_pct, p.worker_swap_peak_pct,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         lc.latest_cluster_id,
         w.worker_nodes_seen_max, w.workers_configured_max
  FROM job_task_agg a
  LEFT JOIN profile p
    ON  p.workspace_id = a.workspace_id AND p.job_id = a.job_id
  LEFT JOIN latest_cluster lc
    ON  lc.workspace_id = a.workspace_id AND lc.job_id = a.job_id
  LEFT JOIN worker_ref w
    ON  w.workspace_id = a.workspace_id AND w.job_id = a.job_id
),
sized AS (
  SELECT j.*,
         CASE
           WHEN j.runs_executed = 0                                                THEN NULL
           WHEN j.runs_with_cluster = 0                                            THEN NULL
           WHEN j.runs_with_telemetry IS NULL OR j.runs_with_telemetry < :min_runs THEN NULL
           WHEN j.single_node                                                      THEN FALSE
           -- memory on its peak and no swap: a smaller node must still hold the busiest minute
           ELSE (j.worker_cpu_p90_pct < :oversized_cpu_pct AND j.worker_mem_peak_pct < :oversized_mem_pct
                 AND COALESCE(j.worker_swap_peak_pct, 0) < :oversized_swap_pct)
         END AS oversized
  FROM judged j
),
cost_usage AS (
  -- the exact per-run attribution and effective-list price join lakeflow_job_run_cost /
  -- lakeflow_job_cost_summary use (usage_metadata.job_id/job_run_id, DEC-66.1)
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity * lp.list_rate)                          AS net_run_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM system.billing.list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
job_cost AS (
  SELECT workspace_id, job_id,
         SUM(net_run_list_cost) AS job_list_cost_usd,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced' THEN 1 ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM cost_usage
  GROUP BY workspace_id, job_id
),
priced AS (
  SELECT s.*, c.job_list_cost_usd, c.price_basis
  FROM sized s
  LEFT JOIN job_cost c
    ON  c.workspace_id = s.workspace_id AND c.job_id = s.job_id
),
savings AS (
  SELECT p.*,
         CASE
           WHEN p.oversized IS NULL THEN NULL
           WHEN NOT p.oversized THEN 0
           WHEN p.job_list_cost_usd IS NULL THEN NULL
           WHEN p.worker_cpu_p90_pct < :oversized_cpu_pct / 2.0
            AND p.worker_mem_peak_pct < :oversized_mem_pct / 2.0 THEN p.job_list_cost_usd * 0.5
           ELSE p.job_list_cost_usd * 0.25
         END AS est_saving_usd
  FROM priced p
)
SELECT sv.workspace_id,
       sv.job_id,
       jb.job_name,                                              -- NULL for submit/workflow runs
       sv.runs_with_telemetry,
       sv.single_node,
       sv.latest_cluster_id,
       lc2.cluster_source,
       lc2.worker_node_type,
       ROUND(nm.memory_mb / 1024.0, 1)                           AS worker_memory_gb,
       nm.core_count                                              AS worker_cores,
       sv.workers_configured_max,
       sv.worker_nodes_seen_max,
       ROUND(sv.worker_cpu_p90_pct, 1)                           AS worker_cpu_p90_pct,
       ROUND(sv.worker_mem_p90_pct, 1)                           AS worker_mem_p90_pct,
       ROUND(sv.worker_cpu_peak_pct, 1)                          AS worker_cpu_peak_pct,
       ROUND(sv.worker_mem_peak_pct, 1)                          AS worker_mem_peak_pct,
       ROUND(sv.worker_swap_peak_pct, 1)                         AS worker_swap_peak_pct,
       sv.oversized,
       ROUND(sv.job_list_cost_usd, 2)                            AS job_list_cost_usd,
       sv.price_basis,
       ROUND(sv.est_saving_usd, 2)                               AS est_saving_usd_list,
       CASE
         WHEN sv.oversized IS NOT TRUE THEN NULL
         WHEN sv.worker_nodes_seen_max IS NOT NULL AND sv.workers_configured_max IS NOT NULL
          AND sv.worker_nodes_seen_max < sv.workers_configured_max THEN 'fewer workers / lower autoscale max'
         ELSE 'one node size down'
       END                                                        AS suggested_action,
       CASE
         WHEN sv.oversized IS NOT NULL             THEN NULL
         WHEN sv.runs_executed = 0                 THEN 'task_not_executed'
         WHEN sv.runs_with_cluster = 0              THEN 'no_cluster_recorded'
         WHEN sv.runs_with_telemetry IS NULL
           OR sv.runs_with_telemetry = 0            THEN 'no_node_timeline_rows'
         ELSE 'too_few_runs'
       END                                                        AS not_assessed_reason,
       CASE
         WHEN sv.oversized IS NULL                       THEN 'NOT_ASSESSED'
         WHEN COALESCE(sv.est_saving_usd, 0) >= :crit_saving_usd THEN 'CRITICAL'
         WHEN COALESCE(sv.est_saving_usd, 0) >= :warn_saving_usd THEN 'WARN'
         ELSE 'OK'
       END                                                        AS status
FROM savings sv
LEFT JOIN latest_jobs jb
  ON  jb.workspace_id = sv.workspace_id AND jb.job_id = sv.job_id
LEFT JOIN latest_clusters lc2
  ON  lc2.workspace_id = sv.workspace_id AND lc2.cluster_id = sv.latest_cluster_id
LEFT JOIN node_mem nm
  ON  nm.node_type = lc2.worker_node_type
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_saving_usd_list DESC NULLS LAST,
         workspace_id, job_id
LIMIT :top_n
