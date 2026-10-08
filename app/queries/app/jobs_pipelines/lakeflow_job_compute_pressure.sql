-- query_id: lakeflow_job_compute_pressure
-- title: Per job, whether its classic compute was memory-bound, CPU-bound at its ceiling, skewed,
--   driver-bound, I/O-bound or idle - and the lever that goes with it: scale up, scale out, scale
--   down, or not a scaling problem
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_task_run_timeline, system.compute.node_timeline,
--   system.compute.clusters, system.compute.node_types, system.lakeflow.jobs
-- requires: SELECT on system.lakeflow and system.compute; GA. SCOPE LIMIT: node_timeline holds
--   classic all-purpose, job, Lakeflow pipeline and pipeline-maintenance compute ONLY, so a job
--   whose tasks ran on serverless or a SQL warehouse has no hardware profile and comes back
--   NOT_ASSESSED with a named reason, never as "no pressure"
-- empty_if: schema_not_enabled, compute_scope_gap, retention_window, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days, capped at 90 in-SQL by node_timeline
--   retention; :min_slices (default 60) minimum judged node-minutes (the workers', or the driver's
--   on a single-node cluster) pooled over the whole job before it is judged (fewer ->
--   NOT_ASSESSED); :waiting_cpu_pct (default 20) average worker CPU percent below which the
--   workers were mostly waiting, not working; :busy_cpu_pct (default 70) median worker CPU percent
--   at/above which the job is genuinely CPU-bound; :crit_mem_pct (default 85) p90 worker memory
--   percent at/above which the job is memory-bound (CRITICAL); :crit_swap_pct (default 10) p90
--   worker swap percent at/above which the job is memory-bound (CRITICAL) - a sustained figure,
--   because a small non-zero swap is normal on these nodes; :skew_gap_pct (default 40) gap in
--   average CPU between the hottest and the coldest worker of one run's cluster that flags skew;
--   :warn_io_wait_pct (default 20) average worker CPU-wait percent at/above which the job is
--   I/O-bound; :top_n (default 100000) row cap - far above any account's job count, because this
--   is a verdict per job and a cut row would read as "not flagged"
-- confidence: needs_confirmation
-- confidence_note: Built on the columns, scopes and heuristic of task_cluster_utilization (checked
--   against a live system-catalog schema dump and the Databricks doc pages) and calibrated once
--   against a redacted diagnostics export of a real account, but this query has NOT been executed
--   on a workspace. Confirm on your account: 1) for a job you know ran on a job cluster,
--   node_timeline.cluster_id matches the first entry of its task runs' compute_ids - node_minutes
--   should be roughly nodes x run minutes, and far below that means the ids do not match or the
--   run fell in node_timeline's ~10-minute blind spot; 2) max_autoscale_workers on
--   system.compute.clusters reads as the ceiling autoscale can reach - at_ceiling TRUE on a job
--   you know scaled to its maximum, FALSE on one that did not; 3) a serverless-only job shows
--   NOT_ASSESSED with no_cluster_recorded rather than an error.
-- read_this: One row = one job with at least one task run starting in the window: every run and
--   every task of the job pooled, with the per-minute telemetry of every classic cluster its task
--   runs used, restricted to each task run's own window, node-minutes shared by parallel tasks
--   counted once. The columns that matter are pressure (MEMORY, SKEW, CPU, IO_WAIT, DRIVER, IDLE
--   or NONE), scaling_hint (the lever: SCALE_UP_MEMORY, SCALE_OUT, FIX_SKEW, DISTRIBUTE_WORK,
--   FIX_IO, SCALE_DOWN or NONE), pressure_reason (the numbers that decided it, in words),
--   hottest_task_key (the task to open first) and on_all_purpose (TRUE = the profile is shared
--   with other work on an all-purpose cluster). at_ceiling says whether any run's CPU-bound cluster
--   reached its configured maximum workers, which decides whether "more workers" is even a lever. The
--   query cannot see the Spark stage; it names the shape of the problem so the Spark UI is opened
--   knowing what to look for.
-- healthy: pressure = NONE, or pressure = CPU with no scale-out lever: at_ceiling FALSE - the
--   workers were doing the work and could still grow; NULL - the cluster config was not found, or
--   the cluster is single-node and cannot grow by workers (its lever, a larger node, is not named
--   here); field heuristic, every threshold is a header param.
-- investigate_if: MEMORY (CRITICAL) - p90 worker memory at/above :crit_mem_pct or p90 swap
--   at/above :crit_swap_pct: the job's input per run is larger than what its workers hold in
--   memory, so spill and OOM are the likely cost; CPU at the ceiling (at_ceiling TRUE), SKEW,
--   DRIVER, IO_WAIT or IDLE (WARN). NOT_ASSESSED is not a pass: read not_assessed_reason.
-- actions: 1) read scaling_hint with pressure_reason, hottest_task_key and on_all_purpose in hand:
--   MEMORY (SCALE_UP_MEMORY) - the job's input per run is larger than what its workers hold in
--   memory, so first cut the input per run (narrow the reprocessed window, filter and prune
--   earlier, process incrementally); SKEW (FIX_SKEW) - salt or repartition the hot key, adding
--   nodes will not help; DRIVER (DISTRIBUTE_WORK) - move pandas / collect() / driver-side loops
--   onto Spark; IO_WAIT (FIX_IO) - compact small files or cache the input; IDLE (SCALE_DOWN) -
--   find the pause in the driver log, an external call or a serial loop (free); 2) MEMORY - raise
--   spark.sql.shuffle.partitions so each Spark task holds less, or split the widest stage; IDLE -
--   fewer or smaller workers, or autoscale down to the minimum; CPU with headroom - nothing,
--   autoscale can still grow (config); 3) MEMORY - scale up: a worker node type with more memory
--   per worker (Databricks: increase the amount of memory available on your instances); CPU at
--   the ceiling (SCALE_OUT) - scale out: raise max_autoscale_workers or the fixed worker count, or
--   a faster family / Photon (spend).
-- next: task_cluster_utilization (per task run, for runs over two hours),
--   lakeflow_long_running_runs, node_timeline_utilization, classic_clusters_config_current,
--   lakeflow_jobs_on_all_purpose (if on_all_purpose)
-- not_assessed_reasons: task_not_executed: the job's tasks were skipped or blocked and never ran;
--   no_cluster_recorded: the job's tasks ran on compute this check cannot see (serverless, or a
--   run from before cluster ids were recorded); no_node_timeline_rows: a cluster was recorded but
--   no matching node telemetry exists for it; too_few_slices: too few overlapping minutes of node
--   telemetry were recorded to judge
-- caveats: SCOPE - system.compute.node_timeline records classic all-purpose, job, Lakeflow pipeline
--   and pipeline-maintenance compute only (cluster_source UI / API / JOB / PIPELINE /
--   PIPELINE_MAINTENANCE), never serverless or SQL warehouses, so a job with no classic telemetry
--   is kept as a NOT_ASSESSED row with not_assessed_reason = no_cluster_recorded (no executed task
--   run recorded a compute id - serverless, or rows from before compute_ids was populated in early
--   Dec 2025), no_node_timeline_rows (an id was recorded but nothing overlaps the task windows - a
--   SQL warehouse id, which the doc says compute_ids also carries, a node that ran under ~10
--   minutes, which the doc says may not appear in node_timeline at all, or activity past its 90-DAY
--   default retention, which is why :period_days is capped at LEAST(:period_days, 90)),
--   too_few_slices (fewer than :min_slices judged node-minutes pooled over the whole job - tune it
--   down for a job made only of very short runs) or task_not_executed (every task run of every run
--   was SKIPPED or BLOCKED; the doc writes such a task as a zero-length row). The cluster of a task
--   run is the FIRST entry of its compute_ids, as in task_cluster_utilization. POOLING - every run
--   and every task of the job in the window is pooled before the percentiles are taken, so a job
--   that is fine on weekdays and spills on Monday reads as its pooled p90; the per-task-run view of
--   runs over two hours is task_cluster_utilization. Parallel tasks of one run on one shared job
--   cluster are deduplicated per node-minute (run, cluster, instance, minute), but two overlapping
--   runs of the same job on one all-purpose cluster count that cluster twice. hottest_task_key is
--   the task whose own windows saw the highest p90 worker memory (ties: more task hours, then
--   task_key) - on a shared cluster that is where the memory was high, not proof that task used it
--   - and NULL when no task's windows had worker telemetry (a single-node job, whose memory is the
--   driver's, pooled across its tasks, so no task can be named from it). The profile is the CPU /
--   memory of the machines during the task windows, NOT the job's own consumption: on an
--   all-purpose cluster every concurrent workload is in the numbers, which is why on_all_purpose is
--   carried (NULL when none of the job's clusters has a configuration row). There is no Spark
--   stage, job or statement in any system table; the verdict is a machine-level inference (field
--   heuristics in the :params, in this precedence: MEMORY, SKEW, CPU, IO_WAIT, DRIVER, IDLE, else
--   NONE) meant to tell you what to look for when you open the Spark UI. worker_cpu_spread_pct is
--   the largest hottest-minus-coldest gap in average worker CPU within any one run's cluster.
--   Single-node clusters have no worker rows: single_node = true and the verdict is judged on the
--   driver's numbers instead; on a single-node job SCALE_UP_MEMORY means a node type with more
--   memory for the driver, which does all the work, and SCALE_DOWN a smaller node. single_node is
--   TRUE only when the job had no worker minute at all, so in a job that mixes a single-node
--   cluster with a multi-worker one the verdict reads the workers' numbers, the single-node
--   cluster's own memory and CPU are not judged, and the DRIVER rule reads only the drivers of run
--   clusters that had workers (a single-node cluster's only node is doing its own work, not waiting
--   on workers - task_cluster_utilization makes the same exclusion per task run);
--   driver_cpu_avg_pct is still every driver minute of the job. mem_used_percent includes OS and
--   background processes, so a memory floor of 30-40 percent on an idle node is normal. SWAP IS
--   THRESHOLDED - this query and task_cluster_utilization both judge the p90 of worker (or driver,
--   on a single-node cluster) swap against a :crit_swap_pct param (default 10 on both), because a
--   small non-zero swap figure is normal on these nodes and flagging any swap above zero used to
--   flag nearly every task/job assessed on a real account; the two queries pool over different
--   grains (this one over the whole job, task_cluster_utilization over one task run of one run),
--   so their p90s can still differ on a mixed job, but the rule itself is now the same. CEILING -
--   per run and cluster, the most worker
--   instances running in the same minute (worker_nodes_seen_max is that concurrent peak, not a
--   count of distinct instance ids, so autoscale churn and spot replacements that issue new ids
--   never read as growth) is compared against that cluster's LATEST configuration
--   (max_autoscale_workers, else worker_count; system.compute.clusters is SCD2, so a cluster
--   resized between runs shows its last shape), and a run's cluster counts toward at_ceiling only
--   when that cluster was itself CPU-bound (its own worker CPU p50 at/above :busy_cpu_pct), so a
--   light fixed-size side cluster never makes a job read as out of room: at_ceiling is TRUE when
--   any such run reached its ceiling, FALSE when none did, and NULL when no cluster of the job has
--   a known worker ceiling - no configuration row, or a single-node cluster (worker_count 0), which
--   has no workers to scale out. A CPU-bound single-node job therefore reads CPU / NONE / OK: its
--   lever would be a larger node, which this query does not name. workers_configured_max and
--   worker_nodes_seen_max describe ONE reference run and cluster: the one that set at_ceiling, else
--   a CPU-bound one, else any - the most workers running at once first (ties: latest run, then
--   cluster_id) - and the ending of a CPU pressure_reason (at ceiling, headroom, cluster config not
--   found, no worker ceiling configured) describes that same cluster, so a light cluster that sat
--   at its fixed size never stands in for the CPU-bound one; only when the job's pooled worker CPU
--   p50 crossed :busy_cpu_pct although no single run's cluster did is the reference simply the
--   largest cluster. A node that lived under ~10 minutes may be missing from that count. On a
--   single-node job pressure_reason names the driver's numbers as the driver's (driver memory p90,
--   driver CPU avg), and a figure the row lacks reads n/a there. CPU-bound with headroom is OK, not
--   WARN: a WARN must have a lever. clusters_seen and max_clusters_per_run count every compute id
--   the job's task runs recorded - a SQL warehouse id of a SQL task included - while
--   latest_cluster_id prefers a classic cluster (one with a configuration row or node_timeline
--   rows) and falls back to any recorded id only when the job has none. cluster_source,
--   worker_node_type, driver_node_type, worker_memory_gb and worker_cores describe
--   latest_cluster_id, the classic cluster of the latest-starting task run that recorded one;
--   worker_memory_gb and worker_cores come from system.compute.node_types and are NULL when the
--   node type is not listed there. network_received_gb is network traffic received by the job's
--   nodes, the only input-volume proxy a classic job has in the system tables - not bytes read.
--   Task seconds are execution_duration_seconds where populated (end row only, 0 treated as not
--   reported), else wall clock between the task's first and last observed rows, to
--   current_timestamp() while in flight; the telemetry window is that same wall clock, so cluster
--   start-up minutes before a task's first row are excluded (see lakeflow_phase_cold_start). Like
--   task_cluster_utilization this query keeps the current day, so a run in flight is examined and
--   very recent minutes may still be materializing. job_name comes from system.lakeflow.jobs (SCD2,
--   latest row) and is NULL for one-time SUBMIT_RUN / WORKFLOW_RUN executions; workspace names are
--   resolved outside this query, so it needs no system.access. No identities are emitted, and there
--   are no dollars here - node-minutes are not a billing unit; price a job with cost_by_job. :top_n
--   caps the rows across ALL workspaces, worst first; its default is set far above any account's
--   job count, and an override lowered in config/thresholds.yml cuts the OK rows, then the
--   NOT_ASSESSED ones, first. Scale up here means a larger warehouse_size / more
--   memory per worker and scale out means more clusters / more workers; Databricks' own
--   warehouse_events logs SCALED_UP when a warehouse ADDS A CLUSTER, so do not read that event name
--   as this query's scale-up. For a classic job cluster, scale up is a worker node type with more
--   memory per worker and scale out is more workers (a higher max_autoscale_workers or fixed worker
--   count).
WITH task_runs AS (
  -- one row per TASK RUN starting in the window, any duration (run_id here is the task run id)
  SELECT workspace_id, job_id, job_run_id,
         run_id                              AS task_run_id,
         task_key,
         MIN(period_start_time)              AS task_start,
         MAX(period_end_time)                AS task_last_seen,
         MAX(result_state)                   AS task_result_state,
         NULLIF(MAX(execution_duration_seconds), 0) AS task_exec_s,
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
         CASE WHEN task_result_state IS NULL THEN current_timestamp() ELSE task_last_seen END AS task_end,
         COALESCE(task_exec_s,
                  timestampdiff(SECOND, task_start,
                                CASE WHEN task_result_state IS NULL THEN current_timestamp()
                                     ELSE task_last_seen END)) AS task_s
  FROM task_runs
),
task_slices AS (
  -- every node-minute of each executed task run's cluster that overlaps that task run's window
  SELECT t.workspace_id, t.job_id, t.job_run_id, t.task_key, t.cluster_id,
         n.instance_id, n.start_time, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.cpu_wait_percent, n.mem_used_percent, n.mem_swap_percent,
         n.network_received_bytes
  FROM task_obs t
  JOIN system.compute.node_timeline n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST(:period_days, 90), current_date())
    AND CASE WHEN t.task_not_executed THEN 0 ELSE 1 END = 1
),
job_slices AS (
  -- the dedupe: parallel tasks of one run on one shared cluster count each node-minute once
  SELECT DISTINCT workspace_id, job_id, job_run_id, cluster_id, instance_id, start_time, driver,
         cpu_pct, cpu_wait_percent, mem_used_percent, mem_swap_percent, network_received_bytes
  FROM task_slices
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_source, driver_node_type, worker_node_type,
         worker_count, max_autoscale_workers
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
run_starts AS (
  -- per run: its first task start and how many distinct clusters its task runs recorded
  SELECT workspace_id, job_id, job_run_id,
         MIN(task_start)            AS run_start,
         COUNT(DISTINCT cluster_id) AS run_clusters
  FROM task_obs
  GROUP BY workspace_id, job_id, job_run_id
),
run_nodes AS (
  -- one row per node of each run's cluster, to count workers and measure skew
  SELECT workspace_id, job_id, job_run_id, cluster_id, instance_id, driver,
         AVG(cpu_pct) AS node_cpu_avg
  FROM job_slices
  GROUP BY workspace_id, job_id, job_run_id, cluster_id, instance_id, driver
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
  -- the most workers running at once: churned or replaced instances are never counted twice
  SELECT workspace_id, job_id, job_run_id, cluster_id, MAX(workers_now) AS worker_nodes_peak
  FROM run_minute_workers
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
run_cpu AS (
  -- the (run, cluster)'s own worker CPU p50: only a CPU-bound cluster can make the job at_ceiling
  SELECT workspace_id, job_id, job_run_id, cluster_id,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS pair_cpu_p50
  FROM job_slices
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
run_cluster_agg AS (
  SELECT n.workspace_id, n.job_id, n.job_run_id, n.cluster_id,
         MAX(s.run_start)                                       AS run_start,
         MAX(k.worker_nodes_peak)                               AS worker_nodes_seen,
         MAX(u.pair_cpu_p50)                                    AS pair_cpu_p50,
         MAX(CASE WHEN n.driver THEN NULL ELSE n.node_cpu_avg END)
           - MIN(CASE WHEN n.driver THEN NULL ELSE n.node_cpu_avg END) AS worker_cpu_spread_pct,
         -- the ceiling autoscale can reach, else the fixed size (latest SCD2 shape)
         MAX(COALESCE(c.max_autoscale_workers, c.worker_count)) AS workers_configured
  FROM run_nodes n
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = n.workspace_id
    AND c.cluster_id   = n.cluster_id
  LEFT JOIN run_starts s
    ON  s.workspace_id = n.workspace_id
    AND s.job_id       = n.job_id
    AND s.job_run_id   = n.job_run_id
  LEFT JOIN run_peak k
    ON  k.workspace_id = n.workspace_id
    AND k.job_id       = n.job_id
    AND k.job_run_id   = n.job_run_id
    AND k.cluster_id   = n.cluster_id
  LEFT JOIN run_cpu u
    ON  u.workspace_id = n.workspace_id
    AND u.job_id       = n.job_id
    AND u.job_run_id   = n.job_run_id
    AND u.cluster_id   = n.cluster_id
  GROUP BY n.workspace_id, n.job_id, n.job_run_id, n.cluster_id
),
run_cluster AS (
  -- a single-node cluster (0 workers configured) has no worker ceiling to reach: NULL, not TRUE
  SELECT workspace_id, job_id, job_run_id, cluster_id, run_start, worker_nodes_seen,
         worker_cpu_spread_pct, workers_configured,
         CASE WHEN workers_configured IS NULL OR workers_configured = 0 THEN NULL
              ELSE worker_nodes_seen >= workers_configured END AS hit_ceiling,
         -- 1 = this run's cluster was itself CPU-bound (its own worker CPU p50 at/above :busy_cpu_pct)
         CASE WHEN pair_cpu_p50 >= :busy_cpu_pct THEN 1 ELSE 0 END AS cpu_bound,
         -- 1 = this run's cluster was CPU-bound AND at its ceiling: the case with a scale-out lever
         CASE WHEN workers_configured IS NULL OR workers_configured = 0 THEN 0
              WHEN worker_nodes_seen >= workers_configured
               AND pair_cpu_p50 >= :busy_cpu_pct THEN 1
              ELSE 0 END                                AS sets_ceiling
  FROM run_cluster_agg
),
ceiling AS (
  -- per job: did ANY CPU-bound run's cluster reach its configured maximum (NULL = no ceiling known)
  SELECT workspace_id, job_id,
         CASE WHEN MAX(CASE WHEN hit_ceiling IS NULL THEN 0 ELSE 1 END) = 0 THEN NULL
              ELSE MAX(sets_ceiling) = 1
         END                        AS at_ceiling,
         MAX(worker_cpu_spread_pct) AS worker_cpu_spread_pct
  FROM run_cluster
  GROUP BY workspace_id, job_id
),
ceiling_ref AS (
  -- the reference (run, cluster) for the reason text: the one that set at_ceiling, else a
  -- CPU-bound one, else any - the most workers running at once first. A light cluster at its fixed
  -- size must never stand in for the CPU-bound cluster the verdict is about.
  SELECT workspace_id, job_id,
         worker_nodes_seen  AS worker_nodes_seen_max,
         workers_configured AS workers_configured_max
  FROM run_cluster
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id
    ORDER BY sets_ceiling DESC, cpu_bound DESC, worker_nodes_seen DESC, run_start DESC, cluster_id
  ) = 1
),
profile AS (
  -- the job's pooled hardware profile over its deduplicated node-minutes
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)                                AS runs_with_telemetry,
         COUNT(*)                                                  AS node_minutes,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_pct END)          AS worker_cpu_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS worker_cpu_p50_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9) AS worker_cpu_p90_pct,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_wait_percent END) AS worker_cpu_wait_avg_pct,
         AVG(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_peak_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_swap_percent END, 0.9) AS worker_swap_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END) AS worker_swap_peak_pct,
         AVG(CASE WHEN driver THEN cpu_pct END)                    AS driver_cpu_avg_pct,
         percentile(CASE WHEN driver THEN mem_used_percent END, 0.9) AS driver_mem_p90_pct,
         percentile(CASE WHEN driver THEN mem_swap_percent END, 0.9) AS driver_swap_p90_pct,
         SUM(network_received_bytes)                               AS network_received_bytes
  FROM job_slices
  GROUP BY workspace_id, job_id
),
driver_load AS (
  -- the drivers the DRIVER rule may judge: those of run clusters that had workers. A single-node
  -- cluster's only node is doing its own work (task_cluster_utilization's NOT single_node guard,
  -- which it applies per task run); pooled per job it would read as a busy driver over idle workers.
  SELECT s.workspace_id, s.job_id,
         AVG(CASE WHEN s.driver AND k.worker_nodes_peak > 0 THEN s.cpu_pct END) AS driver_cpu_avg_with_workers_pct
  FROM job_slices s
  LEFT JOIN run_peak k
    ON  k.workspace_id = s.workspace_id
    AND k.job_id       = s.job_id
    AND k.job_run_id   = s.job_run_id
    AND k.cluster_id   = s.cluster_id
  GROUP BY s.workspace_id, s.job_id
),
task_hours AS (
  SELECT workspace_id, job_id, task_key, SUM(task_s) AS task_key_s
  FROM task_obs
  GROUP BY workspace_id, job_id, task_key
),
task_heat AS (
  -- per task_key and NOT deduplicated: where the worker memory was high
  SELECT s.workspace_id, s.job_id, s.task_key,
         percentile(CASE WHEN s.driver THEN NULL ELSE s.mem_used_percent END, 0.9) AS task_mem_p90,
         MAX(h.task_key_s) AS task_key_s
  FROM task_slices s
  LEFT JOIN task_hours h
    ON  h.workspace_id = s.workspace_id
    AND h.job_id       = s.job_id
    AND h.task_key     = s.task_key
  GROUP BY s.workspace_id, s.job_id, s.task_key
),
hottest AS (
  -- NULL when no task's windows had worker memory (a single-node job): nothing to rank tasks by
  SELECT workspace_id, job_id,
         CASE WHEN task_mem_p90 IS NULL THEN NULL ELSE task_key END AS hottest_task_key,
         task_mem_p90 AS hottest_task_mem_p90_pct
  FROM task_heat
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY task_mem_p90 DESC, task_key_s DESC, task_key
  ) = 1
),
run_roll AS (
  SELECT t.workspace_id, t.job_id,
         COUNT(DISTINCT t.job_run_id)                                   AS runs_seen,
         COUNT(DISTINCT CASE WHEN t.task_not_executed THEN NULL
                             ELSE t.job_run_id END)                     AS runs_executed,
         COUNT(DISTINCT CASE WHEN t.task_not_executed THEN NULL
                             WHEN t.cluster_id IS NULL THEN NULL
                             ELSE t.job_run_id END)                     AS runs_with_cluster,
         COUNT(DISTINCT t.task_key)                                     AS task_keys_seen,
         SUM(t.task_s)                                                  AS task_s_total,
         COUNT(DISTINCT t.cluster_id)                                   AS clusters_seen,
         -- over the clusters used; NULL when none of them has a configuration row
         CASE WHEN MAX(CASE WHEN c.cluster_id IS NULL THEN 0 ELSE 1 END) = 0 THEN NULL
              ELSE MAX(CASE WHEN c.cluster_source IN ('UI', 'API') THEN 1 ELSE 0 END) = 1
         END                                                            AS on_all_purpose
  FROM task_obs t
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = t.workspace_id
    AND c.cluster_id   = t.cluster_id
  GROUP BY t.workspace_id, t.job_id
),
run_last AS (
  SELECT workspace_id, job_id,
         MAX(run_start)    AS last_run_start,
         MAX(run_clusters) AS max_clusters_per_run
  FROM run_starts
  GROUP BY workspace_id, job_id
),
job_telemetry_clusters AS (
  SELECT DISTINCT workspace_id, job_id, cluster_id
  FROM job_slices
),
job_classic AS (
  -- the compute ids of the job that are classic clusters: a configuration row or node telemetry
  -- (a SQL task records its warehouse id in compute_ids, which has neither)
  SELECT DISTINCT t.workspace_id, t.job_id, t.cluster_id
  FROM task_obs t
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = t.workspace_id
    AND c.cluster_id   = t.cluster_id
  LEFT JOIN job_telemetry_clusters s
    ON  s.workspace_id = t.workspace_id
    AND s.job_id       = t.job_id
    AND s.cluster_id   = t.cluster_id
  WHERE t.cluster_id IS NOT NULL
    AND (c.cluster_id IS NOT NULL OR s.cluster_id IS NOT NULL)
),
latest_cluster AS (
  -- the classic cluster of the latest-starting task run that recorded one, else any recorded id
  -- (ties: cluster_id)
  SELECT t.workspace_id, t.job_id, t.cluster_id AS latest_cluster_id
  FROM task_obs t
  LEFT JOIN job_classic k
    ON  k.workspace_id = t.workspace_id
    AND k.job_id       = t.job_id
    AND k.cluster_id   = t.cluster_id
  WHERE t.cluster_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY t.workspace_id, t.job_id
    ORDER BY CASE WHEN k.cluster_id IS NULL THEN 1 ELSE 0 END, t.task_start DESC, t.cluster_id
  ) = 1
),
judged AS (
  SELECT r.*,
         l.last_run_start, l.max_clusters_per_run,
         lc.latest_cluster_id,
         p.runs_with_telemetry, p.node_minutes, p.worker_minutes, p.driver_minutes,
         p.worker_cpu_avg_pct, p.worker_cpu_p50_pct, p.worker_cpu_p90_pct,
         p.worker_cpu_wait_avg_pct, p.worker_mem_avg_pct, p.worker_mem_p90_pct,
         p.worker_mem_peak_pct, p.worker_swap_p90_pct, p.worker_swap_peak_pct,
         p.driver_cpu_avg_pct, p.driver_mem_p90_pct, p.driver_swap_p90_pct,
         p.network_received_bytes,
         g.at_ceiling, g.worker_cpu_spread_pct,
         f.worker_nodes_seen_max, f.workers_configured_max,
         h.hottest_task_key, h.hottest_task_mem_p90_pct,
         d.driver_cpu_avg_with_workers_pct,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         -- the numbers the verdict is judged on: the workers', or the driver's on a single-node cluster
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_minutes ELSE p.driver_minutes END AS judged_minutes,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_avg_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_avg,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_p50_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_p50,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_mem_p90_pct ELSE p.driver_mem_p90_pct END AS eff_mem_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_swap_p90_pct ELSE p.driver_swap_p90_pct END AS eff_swap_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_wait_avg_pct ELSE NULL END AS eff_cpu_wait
  FROM run_roll r
  LEFT JOIN run_last l
    ON  l.workspace_id = r.workspace_id
    AND l.job_id       = r.job_id
  LEFT JOIN latest_cluster lc
    ON  lc.workspace_id = r.workspace_id
    AND lc.job_id       = r.job_id
  LEFT JOIN profile p
    ON  p.workspace_id = r.workspace_id
    AND p.job_id       = r.job_id
  LEFT JOIN ceiling g
    ON  g.workspace_id = r.workspace_id
    AND g.job_id       = r.job_id
  LEFT JOIN ceiling_ref f
    ON  f.workspace_id = r.workspace_id
    AND f.job_id       = r.job_id
  LEFT JOIN hottest h
    ON  h.workspace_id = r.workspace_id
    AND h.job_id       = r.job_id
  LEFT JOIN driver_load d
    ON  d.workspace_id = r.workspace_id
    AND d.job_id       = r.job_id
),
hinted AS (
  SELECT j.*,
         -- machine-level inference, in this precedence (field heuristics; see caveats)
         CASE
           WHEN j.runs_executed = 0                                                   THEN NULL
           WHEN j.judged_minutes IS NULL OR j.judged_minutes < :min_slices            THEN NULL
           WHEN j.eff_mem_p90 >= :crit_mem_pct
             OR COALESCE(j.eff_swap_p90, 0) >= :crit_swap_pct                        THEN 'MEMORY'
           WHEN COALESCE(j.worker_cpu_spread_pct, 0) >= :skew_gap_pct                 THEN 'SKEW'
           WHEN j.eff_cpu_p50 >= :busy_cpu_pct                                         THEN 'CPU'
           WHEN COALESCE(j.eff_cpu_wait, 0) >= :warn_io_wait_pct                       THEN 'IO_WAIT'
           WHEN j.eff_cpu_avg < :waiting_cpu_pct
            AND NOT j.single_node
            AND COALESCE(j.driver_cpu_avg_with_workers_pct, 0) >= :busy_cpu_pct        THEN 'DRIVER'
           WHEN j.eff_cpu_avg < :waiting_cpu_pct                                       THEN 'IDLE'
           ELSE 'NONE'
         END AS pressure
  FROM judged j
)
SELECT h.workspace_id,
       h.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       h.runs_seen,
       COALESCE(h.runs_with_telemetry, 0)                         AS runs_with_telemetry,
       h.task_keys_seen,
       ROUND(h.task_s_total / 3600.0, 2)                          AS task_hours_total,
       h.last_run_start,
       h.clusters_seen,
       h.max_clusters_per_run,
       h.on_all_purpose,
       h.latest_cluster_id,
       c.cluster_source,
       c.worker_node_type,
       c.driver_node_type,
       ROUND(m.memory_mb / 1024.0, 1)                             AS worker_memory_gb,
       m.core_count                                               AS worker_cores,
       h.workers_configured_max,
       h.worker_nodes_seen_max,
       h.at_ceiling,
       h.single_node,
       h.node_minutes,
       h.worker_minutes,
       ROUND(h.worker_cpu_avg_pct, 1)                             AS worker_cpu_avg_pct,
       ROUND(h.worker_cpu_p50_pct, 1)                             AS worker_cpu_p50_pct,
       ROUND(h.worker_cpu_p90_pct, 1)                             AS worker_cpu_p90_pct,
       ROUND(h.worker_cpu_spread_pct, 1)                          AS worker_cpu_spread_pct,
       ROUND(h.worker_cpu_wait_avg_pct, 1)                        AS worker_cpu_wait_avg_pct,
       ROUND(h.worker_mem_avg_pct, 1)                             AS worker_mem_avg_pct,
       ROUND(h.worker_mem_p90_pct, 1)                             AS worker_mem_p90_pct,
       ROUND(h.worker_mem_peak_pct, 1)                            AS worker_mem_peak_pct,
       ROUND(h.worker_swap_p90_pct, 1)                            AS worker_swap_p90_pct,
       ROUND(h.worker_swap_peak_pct, 1)                           AS worker_swap_peak_pct,
       ROUND(h.driver_cpu_avg_pct, 1)                             AS driver_cpu_avg_pct,
       ROUND(h.driver_mem_p90_pct, 1)                             AS driver_mem_p90_pct,
       ROUND(h.network_received_bytes / 1e9, 2)                   AS network_received_gb,
       h.hottest_task_key,
       ROUND(h.hottest_task_mem_p90_pct, 1)                       AS hottest_task_mem_p90_pct,
       h.pressure,
       -- the lever: one enum, its words written once in the header's actions ladder
       CASE
         WHEN h.pressure IS NULL                  THEN NULL
         WHEN h.pressure = 'MEMORY'               THEN 'SCALE_UP_MEMORY'
         WHEN h.pressure = 'CPU' AND h.at_ceiling THEN 'SCALE_OUT'
         WHEN h.pressure = 'SKEW'                 THEN 'FIX_SKEW'
         WHEN h.pressure = 'DRIVER'               THEN 'DISTRIBUTE_WORK'
         WHEN h.pressure = 'IO_WAIT'              THEN 'FIX_IO'
         WHEN h.pressure = 'IDLE'                 THEN 'SCALE_DOWN'
         ELSE 'NONE'
       END AS scaling_hint,
       -- the WHY, built from the row's own numbers (whole-number percentages). Every figure is
       -- COALESCEd to 'n/a' as a string: Databricks' CONCAT returns NULL when any argument is NULL
       -- (DuckDB's skips it), and a judged row must always carry its reason. A single-node job is
       -- judged on the driver's numbers, so its reason names them as the driver's.
       CASE
         WHEN h.pressure IS NULL THEN NULL
         ELSE CONCAT(
           CASE h.pressure
             WHEN 'MEMORY' THEN CONCAT(CASE WHEN h.single_node THEN 'driver memory p90 '
                                            ELSE 'worker memory p90 ' END,
                                       COALESCE(CAST(CAST(ROUND(h.eff_mem_p90, 0) AS BIGINT) AS STRING), 'n/a'),
                                       '% (threshold ', :crit_mem_pct, '%), swap p90 ',
                                       CAST(CAST(ROUND(COALESCE(h.eff_swap_p90, 0), 0) AS BIGINT) AS STRING),
                                       '% (threshold ', :crit_swap_pct, '%)',
                                       CASE WHEN h.hottest_task_mem_p90_pct IS NULL THEN ''
                                            ELSE CONCAT('; hottest task ', COALESCE(h.hottest_task_key, 'n/a'))
                                       END)
             WHEN 'CPU' THEN CASE
               WHEN h.single_node THEN CONCAT('driver CPU avg ',
                                              COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                                              '% (threshold ', :busy_cpu_pct, '%) on a single-node cluster')
               ELSE CONCAT('worker CPU p50 ',
                           COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                           '% (threshold ', :busy_cpu_pct, '%), up to ',
                           COALESCE(CAST(h.worker_nodes_seen_max AS STRING), 'n/a'),
                           CASE WHEN h.workers_configured_max IS NULL OR h.workers_configured_max = 0 THEN ''
                                ELSE CONCAT(' of ', CAST(h.workers_configured_max AS STRING)) END,
                           ' workers seen',
                           -- keyed on the quoted cluster's own configuration first, so the ending
                           -- never calls a cluster with an unknown or zero ceiling "headroom"
                           CASE WHEN h.workers_configured_max IS NULL THEN ' -- cluster config not found'
                                WHEN h.workers_configured_max = 0 THEN ' -- no worker ceiling configured'
                                WHEN h.at_ceiling THEN ' -- at ceiling'
                                ELSE ' -- headroom' END)
             END
             WHEN 'SKEW' THEN CONCAT('hottest-coldest worker CPU gap ',
                                     COALESCE(CAST(CAST(ROUND(h.worker_cpu_spread_pct, 0) AS BIGINT) AS STRING), 'n/a'),
                                     '% in the worst run (threshold ', :skew_gap_pct, '%)')
             WHEN 'DRIVER' THEN CONCAT('driver CPU ',
                                       COALESCE(CAST(CAST(ROUND(h.driver_cpu_avg_with_workers_pct, 0) AS BIGINT) AS STRING), 'n/a'),
                                       '% while workers averaged ',
                                       COALESCE(CAST(CAST(ROUND(h.eff_cpu_avg, 0) AS BIGINT) AS STRING), 'n/a'), '%')
             WHEN 'IO_WAIT' THEN CONCAT('worker CPU wait ',
                                        COALESCE(CAST(CAST(ROUND(h.eff_cpu_wait, 0) AS BIGINT) AS STRING), 'n/a'),
                                        '% (threshold ', :warn_io_wait_pct, '%)')
             WHEN 'IDLE' THEN CONCAT(CASE WHEN h.single_node THEN 'the driver averaged '
                                          ELSE 'workers averaged ' END,
                                     COALESCE(CAST(CAST(ROUND(h.eff_cpu_avg, 0) AS BIGINT) AS STRING), 'n/a'),
                                     '% CPU (threshold ', :waiting_cpu_pct, '%)')
             ELSE CONCAT('no threshold crossed: ',
                         CASE WHEN h.single_node THEN 'driver CPU avg ' ELSE 'CPU p50 ' END,
                         COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                         CASE WHEN h.single_node THEN '%, driver memory p90 ' ELSE '%, memory p90 ' END,
                         COALESCE(CAST(CAST(ROUND(h.eff_mem_p90, 0) AS BIGINT) AS STRING), 'n/a'),
                         '%, swap p90 ',
                         CAST(CAST(ROUND(COALESCE(h.eff_swap_p90, 0), 0) AS BIGINT) AS STRING), '%')
           END,
           CASE WHEN h.on_all_purpose THEN ' -- on an all-purpose cluster shared with other work'
                ELSE '' END)
       END AS pressure_reason,
       -- why a job could not be judged (NULL when it could)
       CASE
         WHEN h.pressure IS NOT NULL                        THEN NULL
         WHEN h.runs_executed = 0                           THEN 'task_not_executed'
         WHEN h.runs_with_cluster = 0                       THEN 'no_cluster_recorded'
         WHEN h.node_minutes IS NULL OR h.node_minutes = 0  THEN 'no_node_timeline_rows'
         ELSE 'too_few_slices'
       END AS not_assessed_reason,
       -- status: a WARN must have a lever, so CPU-bound with headroom is OK.
       -- A job with no telemetry is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN h.pressure IS NULL                                  THEN 'NOT_ASSESSED'
         WHEN h.pressure = 'MEMORY'                               THEN 'CRITICAL'
         WHEN h.pressure IN ('SKEW', 'DRIVER', 'IO_WAIT', 'IDLE') THEN 'WARN'
         WHEN h.pressure = 'CPU' AND h.at_ceiling                 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM hinted h
LEFT JOIN latest_clusters c
  ON  h.workspace_id      = c.workspace_id
  AND h.latest_cluster_id = c.cluster_id
LEFT JOIN node_mem m
  ON  c.worker_node_type = m.node_type
LEFT JOIN latest_jobs j
  ON  h.workspace_id = j.workspace_id
  AND h.job_id       = j.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         task_hours_total DESC,
         workspace_id, job_id
LIMIT :top_n
