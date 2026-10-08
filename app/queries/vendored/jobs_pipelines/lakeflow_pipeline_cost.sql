-- query_id: lakeflow_pipeline_cost
-- title: Per-pipeline DBU cost and update/refresh volume
-- domain: jobs_pipelines   tier: deep
-- reads: system.billing.usage, system.billing.list_prices, system.lakeflow.pipelines, system.lakeflow.pipeline_update_timeline
-- requires: SELECT on system.billing, system.lakeflow; GA (system.billing.usage/list_prices); Public Preview (system.lakeflow.pipelines and pipeline_update_timeline - a missing/disabled table degrades names/updates to NULL, never the DBU attribution)
-- empty_if: schema_not_enabled, preview_unavailable, no_activity
-- params: :period_days (default 30) rolling window in days; :warn_pipeline_dbus (default 500) net pipeline DBUs in the window that flags WARN; :crit_pipeline_dbus (default 2000) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: pricing.effective_list.default (the effective, post-promotion list price - DEC-66.1) is the one dollar basis used across this app; net_list_cost is a directional estimate at that price, never a billed dollar figure. is_continuous (settings.continuous on system.lakeflow.pipelines) and the in-flight / nothing-delivered split below are new, not yet confirmed against a live workspace.
-- read_this: One row = a pipeline. The column that matters is net_pipeline_dbus - net DBUs billed to that pipeline in the window; net_maintenance_dbus is the housekeeping share of that total, broken out separately; updates/active_seconds_total are the over-refresh signal (many short updates can mean the pipeline is triggered more often than the data actually changes). price_basis (free/priced/unpriced) discloses whether net_list_cost is a real $0 (free-usage SKU) or understated by a pricing-coverage gap. est_wasted_usd_list is the possible waste: net_list_cost (minus its maintenance share) when the pipeline delivered NOTHING in the window (see caveats), 0 when it delivered at least one completed update, and NULL when there is no discrete completed-update signal to judge (a continuous pipeline, or one still mid-update at the end of the window).
-- healthy: net_pipeline_dbus below :warn_pipeline_dbus for the window, with maintenance a small share of the total, and est_wasted_usd_list at 0 - field heuristic; tune :warn_pipeline_dbus for your account.
-- investigate_if: est_wasted_usd_list > 0 (CRITICAL at/above :crit_pipeline_dbus, WARN at/above :warn_pipeline_dbus - the pipeline billed real DBUs and delivered nothing), or net_pipeline_dbus at/above :crit_pipeline_dbus with something delivered (WARN - large but not necessarily wasteful; this is a spend-magnitude signal, not a waste one) - field heuristic; also look at updates vs active_seconds_total for over-refresh. NOT_ASSESSED is not a pass: read not_assessed_reason.
-- actions: 1) check the pipeline's trigger interval against how often the source data actually changes and space it out if it is over-refreshing (free); 2) switch a continuously-triggered pipeline to a scheduled/triggered pipeline if continuous freshness is not required (config); 3) if the pipeline is correctly sized and still expensive, revisit serverless/Photon settings or move it to a cheaper SKU tier (spend).
-- next: lakeflow_pipeline_idle_tail_duration (for cluster lingering after the pipeline's active window), lakeflow_pipeline_update_failures_retries (for how much of that DBU spend is retries)
-- not_assessed_reasons: continuous_pipeline: a continuous pipeline has no discrete "completed update" to judge, so nothing-delivered can never be assessed; update_in_flight: the pipeline had update activity in the window but no update has ended yet, so "nothing delivered" cannot yet be told apart from "still working"
-- caveats: PER-PIPELINE DBUs = net DBUs billed on system.billing.usage rows carrying usage_metadata.dlt_pipeline_id, attributed on (workspace_id, dlt_pipeline_id). usage_metadata also carries dlt_update_id and dlt_maintenance_id; maintenance DBUs (dlt_maintenance_id IS NOT NULL) are summed into a SEPARATE net_maintenance_dbus column so housekeeping is not blamed on pipeline logic, and net_maintenance_list_cost is excluded from est_wasted_usd_list so maintenance-only spend is never counted as pipeline waste (net_list_cost, the pipeline's whole spend, is unchanged). usage_quantity is summed across ALL record_types (ORIGINAL/RETRACTION/RESTATEMENT already net) and filtered to upper(usage_unit)='DBU', so bytes/hours/tokens never blend into the DBU total. list_rate (pricing.effective_list.default) is the effective, post-promotion list price - DEC-66.1's one dollar basis; treat net_list_cost as an estimate at that price, never a negotiated or billed dollar (no negotiated-rate source exists, see cost_actual_vs_list_by_sku); the price window is open-interval (price_end_time NULL = currently effective). dlt_pipeline_id is unique only WITHIN a workspace, so every join/group here is on (workspace_id, pipeline_id), never pipeline_id alone. pipelines is SCD2, so this takes the latest row via QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1 for names/type/is_continuous, not ids. pipeline_update_timeline gives the update count and active-second proxy for the over-refresh signal; result_state IS NOT NULL marks an update's end row (update_end_rows / updates / updates_completed / updates_failed_or_canceled are all end-row-only); update_rows_any counts every update row seen in the window, ended or not, so a still-running update is not confused with no activity at all. NOTHING DELIVERED means either zero update activity all window, or every ended update read FAILED or CANCELED - a bare updates_completed = 0 is NOT enough on its own, since an update still in flight also shows 0 completed (checked first, via in_flight_only, and reads NOT_ASSESSED, never CRITICAL). A continuous pipeline (is_continuous) runs one update for the whole window that never produces an end row, so it has no discrete completed-update signal at all and reads NOT_ASSESSED, never CRITICAL, regardless of spend. system.lakeflow.pipelines and pipeline_update_timeline are Public Preview - a missing table degrades the names/update/is_continuous columns to NULL via LEFT JOIN (is_continuous defaults FALSE, i.e. judged as non-continuous), never dropping the DBU attribution itself. price_basis is 'unpriced' when any non-free-usage SKU billed to this pipeline had no matching list_prices row (net_list_cost then understates cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise. is_high_spend flags net_pipeline_dbus at/above :warn_pipeline_dbus as a plain size fact, independent of status - a pipeline between :warn_pipeline_dbus and :crit_pipeline_dbus that delivered something is_high_spend = true but status = OK (spend alone, below the CRITICAL floor, with nothing wasted, is not itself a finding); at/above :crit_pipeline_dbus it is WARN even when delivering, since that much spend is worth a second look regardless.
WITH pipe_dbus AS (
  -- DBUs billed to each pipeline over the window. Net across ALL record_types; DBU only.
  -- Split maintenance DBUs out via dlt_maintenance_id so they are reported separately.
  SELECT workspace_id,
         usage_metadata.dlt_pipeline_id AS pipeline_id,
         SUM(usage_quantity) AS net_pipeline_dbus,
         SUM(CASE WHEN usage_metadata.dlt_maintenance_id IS NOT NULL
                  THEN usage_quantity ELSE 0 END) AS net_maintenance_dbus
  FROM system.billing.usage
  WHERE usage_date >= dateadd(day, -:period_days, current_date())
    AND usage_date < current_date()
    AND upper(usage_unit) = 'DBU'
    AND usage_metadata.dlt_pipeline_id IS NOT NULL
  GROUP BY workspace_id, usage_metadata.dlt_pipeline_id
),
pipe_list AS (
  -- Effective-list-price estimate (DEC-66.1): SUM(usage_quantity * list_rate) per pipeline.
  -- Never a negotiated or billed dollar - no negotiated-rate source exists in system.billing.
  SELECT u.workspace_id,
         u.usage_metadata.dlt_pipeline_id AS pipeline_id,
         MAX(lp.currency_code) AS currency_code,
         SUM(u.usage_quantity * lp.list_rate) AS net_list_cost,
         SUM(CASE WHEN u.usage_metadata.dlt_maintenance_id IS NOT NULL
                  THEN u.usage_quantity * lp.list_rate ELSE 0 END) AS net_maintenance_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM system.billing.list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.dlt_pipeline_id
),
pipe_meta AS (
  -- SCD2 latest row per (workspace_id, pipeline_id): names-not-ids + type + continuous flag.
  SELECT workspace_id, pipeline_id, name AS pipeline_name, pipeline_type,
         settings.continuous AS is_continuous
  FROM system.lakeflow.pipelines
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC
  ) = 1
),
pipe_updates_any AS (
  -- every update row seen in the window, ended or not -- tells "no update activity at all" apart
  -- from "an update is running but has not ended yet" before pipe_updates (end rows only) is read.
  SELECT workspace_id, pipeline_id, COUNT(*) AS update_rows_any
  FROM system.lakeflow.pipeline_update_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
  GROUP BY workspace_id, pipeline_id
),
pipe_updates AS (
  -- Over-refresh signal (distinct updates + total active seconds) and the delivered-vs-not split,
  -- both end-row-only (result_state IS NOT NULL).
  SELECT workspace_id, pipeline_id,
         COUNT(*)                                                             AS update_end_rows,
         COUNT(DISTINCT update_id)                                            AS updates,
         SUM(CASE WHEN result_state = 'COMPLETED' THEN 1 ELSE 0 END)          AS updates_completed,
         SUM(CASE WHEN result_state IN ('FAILED', 'CANCELED') THEN 1 ELSE 0 END) AS updates_failed_or_canceled,
         SUM(unix_timestamp(period_end_time) - unix_timestamp(period_start_time)) AS active_seconds_total
  FROM system.lakeflow.pipeline_update_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL   -- update end row only
  GROUP BY workspace_id, pipeline_id
),
combined AS (
  SELECT d.workspace_id, d.pipeline_id,
         d.net_pipeline_dbus, d.net_maintenance_dbus,
         l.currency_code, l.net_list_cost, l.net_maintenance_list_cost,
         COALESCE(l.price_basis, 'priced')          AS price_basis,
         m.pipeline_name, m.pipeline_type, COALESCE(m.is_continuous, FALSE) AS is_continuous,
         COALESCE(up.updates, 0)                    AS updates,
         COALESCE(up.update_end_rows, 0)            AS update_end_rows,
         COALESCE(ua.update_rows_any, 0)            AS update_rows_any,
         COALESCE(up.updates_completed, 0)          AS updates_completed,
         COALESCE(up.updates_failed_or_canceled, 0) AS updates_failed_or_canceled,
         COALESCE(up.active_seconds_total, 0)       AS active_seconds_total,
         -- in_flight_only: the window saw update activity but no update has ended yet -- cannot yet
         -- tell "delivering" from "nothing delivered".
         (COALESCE(ua.update_rows_any, 0) > 0 AND COALESCE(up.update_end_rows, 0) = 0) AS in_flight_only,
         -- nothing_delivered: either no update activity in the window at all, or every ended update
         -- failed or was canceled (COMPLETED count of 0 alone is not enough -- an update still in
         -- flight also has 0 completed, which is why in_flight_only is checked first).
         (
           (COALESCE(up.update_end_rows, 0) = 0 AND COALESCE(ua.update_rows_any, 0) = 0)
           OR (COALESCE(up.update_end_rows, 0) > 0
               AND COALESCE(up.updates_failed_or_canceled, 0) = up.update_end_rows)
         ) AS nothing_delivered
  FROM pipe_dbus d
  LEFT JOIN pipe_list l         ON d.workspace_id = l.workspace_id AND d.pipeline_id = l.pipeline_id
  LEFT JOIN pipe_meta m         ON d.workspace_id = m.workspace_id AND d.pipeline_id = m.pipeline_id
  LEFT JOIN pipe_updates up     ON d.workspace_id = up.workspace_id AND d.pipeline_id = up.pipeline_id
  LEFT JOIN pipe_updates_any ua ON d.workspace_id = ua.workspace_id AND d.pipeline_id = ua.pipeline_id
)
SELECT c.workspace_id,
       c.pipeline_id,
       c.pipeline_name AS pipeline_name,
       c.pipeline_type, c.is_continuous,
       c.net_pipeline_dbus, c.net_maintenance_dbus,
       c.currency_code, c.net_list_cost, c.net_maintenance_list_cost,
       c.price_basis,
       c.updates, c.update_end_rows, c.update_rows_any,
       c.updates_completed, c.updates_failed_or_canceled,
       c.active_seconds_total,
       c.in_flight_only, c.nothing_delivered,
       (c.net_pipeline_dbus >= :warn_pipeline_dbus) AS is_high_spend,
       CASE
         WHEN c.is_continuous THEN NULL
         WHEN c.in_flight_only THEN NULL
         WHEN c.nothing_delivered THEN ROUND(c.net_list_cost - COALESCE(c.net_maintenance_list_cost, 0), 2)
         ELSE 0
       END AS est_wasted_usd_list,
       -- status: CRITICAL/WARN now require the nothing-delivered signal at :crit_pipeline_dbus, not
       -- size alone (field heuristic; :warn_pipeline_dbus / :crit_pipeline_dbus).
       CASE
         WHEN c.is_continuous THEN 'NOT_ASSESSED'    -- continuous pipeline: no discrete completed-update signal
         WHEN c.in_flight_only THEN 'NOT_ASSESSED'   -- update still running at window end: too early to call
         WHEN c.net_pipeline_dbus >= :crit_pipeline_dbus AND c.nothing_delivered THEN 'CRITICAL'
         WHEN c.net_pipeline_dbus >= :crit_pipeline_dbus THEN 'WARN'
         WHEN c.net_pipeline_dbus >= :warn_pipeline_dbus AND c.nothing_delivered THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN c.is_continuous THEN 'continuous_pipeline'
         WHEN c.in_flight_only THEN 'update_in_flight'
       END AS not_assessed_reason
FROM combined c
WHERE c.net_pipeline_dbus > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         c.net_pipeline_dbus DESC
