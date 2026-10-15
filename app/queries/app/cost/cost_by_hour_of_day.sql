-- query_id: cost_by_hour_of_day
-- title: When you spend - list-priced dollars by weekday x hour-of-day x product, with job runs
--   and queries running in the same slot
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.lakeflow.job_run_timeline,
--   system.query.history
-- requires: SELECT on system.billing, system.lakeflow AND system.query; GA (system.billing.usage
--   / list_prices, system.lakeflow.job_run_timeline and system.query.history are all generally
--   available)
-- empty_if: schema_not_enabled, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :warn_hour_share_pct (default 15)
--   percent of the window's whole list-priced spend one hour-of-day holds, summed across every
--   weekday and product, at/above which that hour-of-day flags WARN; :crit_hour_share_pct
--   (default 25) the same at/above which it flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: Reuses the exact effective-list price join (DEC-66.1) cost_monthly_actuals
--   already carries, and the same hour(start_time) / date(start_time) extraction
--   query_workload_mix_hours already carries against a live workspace, but this query itself has
--   not been run live. The weekday number is computed from a fixed Monday anchor date
--   (DATE '2023-01-02') via datediff() MOD 7, deliberately avoiding dayofweek()/dayname(), whose
--   day-1 convention differs between Databricks SQL and DuckDB - confirm on your account that a
--   day you know was a Monday reads weekday_num = 0 here.
-- read_this: One row = a workspace + weekday + hour-of-day + billing product slot's list-priced
--   spend in the window (UTC - see caveats). job_runs_active and queries_started count activity
--   in that same weekday + hour-of-day slot ONCE and are repeated on every product row of that
--   slot (they have no product dimension of their own - see caveats). share_of_window_spend and
--   status describe the HOUR-OF-DAY alone, collapsed across every weekday and product: what
--   share of the workspace's whole window spend lands in that one hour, whichever day it falls
--   on - the peak-hour signal a heat map is built from.
-- healthy: status = OK - this hour-of-day holds under :warn_hour_share_pct of the workspace's
--   window spend - field heuristic, both thresholds are header params.
-- investigate_if: status = WARN or CRITICAL - this hour-of-day holds at/above
--   :warn_hour_share_pct or :crit_hour_share_pct of the workspace's window spend, concentrated in
--   one hour of the day across the whole window.
-- actions: 1) Databricks' own prices do not change by hour of day - the saving here comes from
--   avoiding PEAKS, not from a cheaper hour: fewer warehouse scale-outs and queued queries during
--   the spike, smaller shared clusters sized for a flatter load, jobs not all starting at the
--   same hour, 2am for example. Every figure on this row is measured from what already happened,
--   not a forecast (free); 2) if job_runs_active is high in the flagged hour, stagger job schedules
--   (cron offsets) across the day instead of a single top-of-hour trigger (config); 3) if
--   queries_started is high in the flagged hour on a warehouse-backed workload, check
--   query_warehouse_pressure and compute_warehouse_autoscale_churn for that same hour - a spend
--   peak that is also a queuing/scale-out peak is worth smoothing first (config).
-- next: query_warehouse_pressure (if the peak hour also queues or spills),
--   compute_warehouse_autoscale_churn (if a warehouse scales out every day around the peak hour),
--   cost_daily_spikes (for a day-level, not hour-level, spend jump), cost_monthly_actuals (the
--   calendar-month total this window's spend rolls into)
-- caveats: UTC - weekday, hour_of_day, job_runs_active and queries_started are all bucketed on
--   the source timestamp's own UTC calendar hour (usage_start_time, period_start_time, start_time
--   respectively); this query does not read or apply any workspace/browser timezone, so "02:00"
--   here is 02:00 UTC. WEEKDAY - weekday_num is 0 (Monday) .. 6 (Sunday), computed as
--   datediff(<date>, DATE '2023-01-02') MOD 7 (2023-01-02 was a Monday) rather than
--   dayofweek()/dayname(): Databricks SQL's dayofweek() numbers Sunday=1..Saturday=7 while
--   DuckDB's numbers Sunday=0..Saturday=6, and Databricks SQL has no dayname() - either would
--   silently disagree between this query's two run targets, so neither is used. GRAIN - the
--   PRIMARY grain is (workspace_id, weekday, hour_of_day, billing_origin_product): usd_list and
--   price_basis are this slot's own list-priced spend. job_runs_active (system.lakeflow.
--   job_run_timeline, COUNT of distinct job_id+run_id whose period_start_time falls in that
--   weekday+hour) and queries_started (system.query.history, COUNT of statements whose
--   start_time falls in it) have no product dimension of their own: each is counted ONCE per
--   (workspace_id, weekday, hour_of_day) and then repeated (broadcast) on every product row of
--   that same slot - read them once per weekday+hour, not summed across a workspace's product
--   rows. A workspace+weekday+hour with job or query activity but literally NO billed usage row
--   in that exact same UTC hour (rare - compute usually bills in the same or an adjacent hour)
--   has no row here at all, since usd_list drives the grain; pair with lakeflow_workload_mix_hours
--   or query_workload_mix_hours for activity with no matching spend. PEAK FLAG -
--   share_of_window_spend and status are computed on hour_usd_list (SUM of usd_list for this
--   workspace + hour_of_day, across every weekday and product) over window_usd_list (SUM of
--   usd_list for the whole workspace and window) - a single value per (workspace_id,
--   hour_of_day) that is then repeated on every weekday and product row sharing that hour, so a
--   heat map built from this table will show the same status running down every column for a
--   given hour. PRICE - the same effective-list join (DEC-66.1) cost_monthly_actuals uses
--   (sku_name, cloud AND usage_unit, matched to usage_end_time), an estimate at list price, never
--   a negotiated or billed dollar; price_basis is 'unpriced' when any non-free-usage SKU in the
--   slot had no matching list_prices row (usd_list then understates the slot), 'free' when every
--   matched SKU was FREE_USAGE (a real $0), 'priced' otherwise. Account-level usage
--   (workspace_id NULL) is its own group like any workspace's, and is matched to job/query
--   activity (which always carries a real workspace_id) null-safely, so it simply never picks up
--   a job_runs_active/queries_started figure. The current, still-in-progress UTC day is excluded
--   from all three sources, since billing.usage lands with ingestion lag and a trailing partial
--   day would understate whichever hour it stops in. No identities are emitted.
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM system.billing.list_prices
),
usage_priced AS (
  SELECT u.workspace_id, u.billing_origin_product,
         (datediff(DATE(u.usage_start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(u.usage_start_time)             AS hour_of_day,
         u.usage_quantity * lp.list_rate      AS usd,
         CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_quantity,
         CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_quantity
  FROM system.billing.usage u
  LEFT JOIN price lp
    ON  u.sku_name   = lp.sku_name
    AND u.cloud      = lp.cloud
    AND u.usage_unit = lp.usage_unit
    AND u.usage_end_time >= lp.price_start_time
    AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_start_time >= dateadd(day, -:period_days, current_date())
    AND u.usage_start_time < current_date()
),
spend_grid AS (
  SELECT workspace_id, weekday_num, hour_of_day, billing_origin_product,
         SUM(usd)                AS usd_list,
         SUM(unpriced_quantity)  AS unpriced_quantity,
         SUM(priced_quantity)    AS priced_quantity
  FROM usage_priced
  GROUP BY workspace_id, weekday_num, hour_of_day, billing_origin_product
),
hour_totals AS (
  -- this workspace's whole spend in this hour-of-day, across every weekday and product
  SELECT workspace_id, hour_of_day, SUM(usd_list) AS hour_usd_list
  FROM spend_grid
  GROUP BY workspace_id, hour_of_day
),
workspace_totals AS (
  -- this workspace's whole window spend, every weekday, hour and product
  SELECT workspace_id, SUM(usd_list) AS window_usd_list
  FROM spend_grid
  GROUP BY workspace_id
),
job_runs AS (
  SELECT workspace_id,
         (datediff(DATE(period_start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(period_start_time)                                     AS hour_of_day,
         COUNT(DISTINCT CONCAT(job_id, '::', run_id))                AS job_runs_active
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_start_time < current_date()
  GROUP BY workspace_id, weekday_num, hour_of_day
),
query_counts AS (
  SELECT workspace_id,
         (datediff(DATE(start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(start_time)                                     AS hour_of_day,
         COUNT(*)                                              AS queries_started
  FROM system.query.history
  WHERE start_time >= dateadd(day, -:period_days, current_date())
    AND start_time < current_date()
  GROUP BY workspace_id, weekday_num, hour_of_day
)
SELECT g.workspace_id,
       CASE g.weekday_num
         WHEN 0 THEN 'Monday'    WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday'
         WHEN 3 THEN 'Thursday'  WHEN 4 THEN 'Friday'  WHEN 5 THEN 'Saturday'
         ELSE 'Sunday'
       END                                                  AS weekday,
       g.weekday_num,
       g.hour_of_day,
       g.billing_origin_product,
       ROUND(g.usd_list, 2)                                 AS usd_list,
       CASE
         WHEN g.unpriced_quantity > 0 THEN 'unpriced'
         WHEN g.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END                                                  AS price_basis,
       COALESCE(jr.job_runs_active, 0)                      AS job_runs_active,
       COALESCE(qc.queries_started, 0)                      AS queries_started,
       ROUND(ht.hour_usd_list, 2)                           AS hour_usd_list,
       ROUND(wt.window_usd_list, 2)                         AS window_usd_list,
       ROUND(100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0), 1) AS share_of_window_spend,
       CASE
         WHEN 100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0) >= :crit_hour_share_pct THEN 'CRITICAL'
         WHEN 100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0) >= :warn_hour_share_pct THEN 'WARN'
         ELSE 'OK'
       END                                                  AS status
FROM spend_grid g
LEFT JOIN hour_totals ht
  ON  ht.workspace_id IS NOT DISTINCT FROM g.workspace_id AND ht.hour_of_day = g.hour_of_day
LEFT JOIN workspace_totals wt
  ON  wt.workspace_id IS NOT DISTINCT FROM g.workspace_id
LEFT JOIN job_runs jr
  ON  jr.workspace_id IS NOT DISTINCT FROM g.workspace_id
  AND jr.weekday_num = g.weekday_num AND jr.hour_of_day = g.hour_of_day
LEFT JOIN query_counts qc
  ON  qc.workspace_id IS NOT DISTINCT FROM g.workspace_id
  AND qc.weekday_num = g.weekday_num AND qc.hour_of_day = g.hour_of_day
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         g.workspace_id, g.weekday_num, g.hour_of_day, g.billing_origin_product
