-- query_id: query_queuing_waits
-- title: Queries queued for capacity or compute provisioning
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, compute_scope_gap
-- params: :period_days (default 30) rolling window in days; :warn_queue_secs (default 60) a day+warehouse's average combined queued-for-capacity + waiting-for-compute seconds, across its own statements that waited at all, that flags WARN; :crit_queue_secs (default 600) the same average that flags CRITICAL
-- confidence: confirmed
-- confidence_note: Columns verified against system.query.history in a live workspace.
-- read_this: One row = a day + warehouse whose queries waited before or during execution. The columns that matter are avg_wait_ms (the average combined wait of statements that waited at all - CRITICAL/WARN is judged on this, not the day's raw total, so one busy warehouse with many small waits does not outrank a warehouse with one genuinely long wait) and, for scale, waiting_at_capacity_ms_sum (queued because the warehouse was already at max concurrency/scale) and waiting_for_compute_ms_sum (compute was still provisioning - cold start). A warehouse that shows up here repeatedly on the same day-of-week/hour pattern is under-sized or under-scheduled for its load, not just unlucky once.
-- healthy: avg_wait_ms below :warn_queue_secs seconds (field heuristic - tune :warn_queue_secs for your account).
-- investigate_if: avg_wait_ms at/above :warn_queue_secs seconds (WARN) or :crit_queue_secs seconds (CRITICAL) - field heuristic; waits that recur on the same warehouse are the real signal, not a single spike.
-- actions: 1) check whether the wait clusters at specific hours before changing anything, e.g. against query_workload_mix_hours (free); 2) raise the warehouse's max clusters/scaling limit, or keep it warm through the busy window with a longer auto-stop (config); 3) split the workload onto a second warehouse, or size up the existing one, if concurrency is structurally too high for one warehouse (spend).
-- next: query_workload_mix_hours (to see if waits line up with a load spike by hour), query_local_spillage (if the same warehouse also spills once queries do get compute)
-- caveats: There are only two queue buckets in system tables: waiting_at_capacity_duration_ms (queued because the warehouse was at capacity) and waiting_for_compute_duration_ms (compute was still being provisioned / cold start) - there is no separate "repair" or "retry" bucket, so a query that failed and retried shows up as ordinary duration, not as a distinct wait category. AVG_WAIT_MS is judged PER STATEMENT, not per day: a "statement that waited" is one whose COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0) is greater than 0; avg_wait_ms is the SUM of that combined wait across those statements divided by how many of them there are. A busy warehouse with many statements each waiting a second or two now reads on that typical wait, not on the day's summed total - the old day-total sum flagged CRITICAL on 1,000 one-second waits (1,000 seconds summed) even though no single statement waited long. SUM() ignores NULL rows but returns NULL itself when EVERY row in a (day, warehouse) group is NULL for that column. NOT_ASSESSED is checked only when avg_wait_ms does not already clear CRITICAL/WARN, and triggers when EITHER bucket's raw SUM is NULL across the whole group (SERVERLESS_COMPUTE rows, for example, can report both buckets NULL for the whole day) - so a group where only one bucket is ever reported (the other genuinely never captured) still reads NOT_ASSESSED here rather than a silent false OK on the reported bucket alone, unless the reported bucket's own average already clears a status band, which is judged first and is never masked by this. A 0 (a real, observed zero-second wait) is NOT the same as this NULL case: a statement that never waited at all is simply not counted in avg_wait_ms's numerator or denominator, and a group where every statement's wait was genuinely 0 has no "statement that waited" (avg_wait_ms is NULL, reading OK), never NOT_ASSESSED. This query does NOT assume NULL means 0.
-- system.query.history only captures queries run on SQL warehouses or serverless compute; queries on classic all-purpose or job clusters are never recorded, so a workload that runs largely on classic clusters can look queue-free here even when it isn't.
WITH per_query AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         waiting_at_capacity_duration_ms, waiting_for_compute_duration_ms, total_duration_ms,
         COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0) AS wait_ms
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL :period_days DAYS
    AND start_time < current_date()
)
SELECT day, workspace_id, compute_type, warehouse_id,
       COUNT(*) AS query_count,
       SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN 1 ELSE 0 END) AS queued_at_capacity_count,
       SUM(CASE WHEN waiting_for_compute_duration_ms > 0 THEN 1 ELSE 0 END) AS waited_for_compute_count,
       SUM(waiting_at_capacity_duration_ms)  AS waiting_at_capacity_ms_sum,
       SUM(waiting_for_compute_duration_ms)  AS waiting_for_compute_ms_sum,
       SUM(total_duration_ms)                AS total_duration_ms_sum,
       -- avg_wait_ms: the average combined wait of statements that waited at all (a real, all-zero
       -- day has no such statement -> NULL, never 0 and never NOT_ASSESSED on its own).
       SUM(CASE WHEN wait_ms > 0 THEN wait_ms ELSE 0 END)
         / NULLIF(SUM(CASE WHEN wait_ms > 0 THEN 1 ELSE 0 END), 0) AS avg_wait_ms,
       -- status: worst-first band on the PER-STATEMENT average wait (field heuristic;
       -- :warn_queue_secs / :crit_queue_secs), not the day's raw total - so many tiny waits on a
       -- busy warehouse no longer outrank one genuinely long wait. NOT_ASSESSED is checked only
       -- when avg_wait_ms does not already clear a band, and triggers when EITHER bucket's raw SUM
       -- is NULL across the whole group (e.g. a SERVERLESS_COMPUTE day that never reports
       -- waiting_at_capacity at all) - the same rule as before, unaffected by the average.
       CASE
         WHEN SUM(CASE WHEN wait_ms > 0 THEN wait_ms ELSE 0 END)
                / NULLIF(SUM(CASE WHEN wait_ms > 0 THEN 1 ELSE 0 END), 0) >= :crit_queue_secs * 1000 THEN 'CRITICAL'
         WHEN SUM(CASE WHEN wait_ms > 0 THEN wait_ms ELSE 0 END)
                / NULLIF(SUM(CASE WHEN wait_ms > 0 THEN 1 ELSE 0 END), 0) >= :warn_queue_secs * 1000 THEN 'WARN'
         WHEN SUM(waiting_at_capacity_duration_ms) IS NULL OR SUM(waiting_for_compute_duration_ms) IS NULL THEN 'NOT_ASSESSED'
         ELSE 'OK'
       END AS status
FROM per_query
GROUP BY day, workspace_id, compute_type, warehouse_id
ORDER BY (waiting_at_capacity_ms_sum + waiting_for_compute_ms_sum) DESC
