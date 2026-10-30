-- query_id: lakeflow_pipeline_idle_tail_duration
-- title: Pipeline update activity and idle-tail DBU exposure
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.pipeline_update_timeline, system.lakeflow.pipelines, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.lakeflow, system.billing; Public Preview (system.lakeflow.pipelines and pipeline_update_timeline); GA (system.billing.usage/list_prices)
-- empty_if: schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_idle_dbus (default 100) DBUs billed outside the pipeline's update time in the window that flag WARN; :crit_idle_dbus (default 500) that flag CRITICAL
-- confidence: needs_confirmation
-- confidence_note: the settings.continuous / settings.development dot-access assumes settings is a STRUCT on system.lakeflow.pipelines; whether it is a STRUCT (dot-access) or a MAP (settings['key']) on your account is unverified.
-- read_this: One row = a pipeline. idle_dbus is the DBUs billed in hours no update ran in at all - the cluster lingering after a run. idle_share is that part of net_dbus; updates/active_seconds_total show how much the pipeline actually ran.
-- healthy: idle_dbus below :warn_idle_dbus for the window - field heuristic; tune :warn_idle_dbus for your account.
-- investigate_if: idle_dbus at/above :warn_idle_dbus (WARN) or :crit_idle_dbus (CRITICAL) on a pipeline that is not continuous - field heuristic.
-- actions: 1) check the pipeline's cluster auto-termination / shutdown-delay setting (free); 2) shorten the idle shutdown delay or move the pipeline to serverless DLT compute (config); 3) if the workload genuinely needs a warm cluster between runs, that is a deliberate spend - just confirm it is intentional (spend).
-- next: lakeflow_pipeline_cost (for the full per-pipeline DBU picture), lakeflow_pipelines_inventory_tier (for the pipeline's serverless/continuous/development settings)
-- caveats: The active update window (period_start_time/period_end_time) is confirmed. The idle tail itself (post-run cluster lingering) is NOT a lakeflow column - it is measured as the DBUs of billed hours (system.billing.usage by usage_metadata.dlt_pipeline_id) that no update slice overlaps. The bill comes in hourly rows, so the idle part of an hour an update ran in cannot be told apart and counts as active: idle_dbus is a lower bound. A serverless pipeline does not linger and reads OK. A cluster-shutdown-delay-style setting is not in the documented schema and is deliberately not emitted here. net_dbus is the exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, and it excludes cloud infra/egress cost - treat it as directional. Cost is attributed by (workspace_id, dlt_pipeline_id) over the window (per-pipeline), not per update/event; the rollup is pre-aggregated then LEFT JOINed, so result rows are never multiplied. price_basis is 'unpriced' when any non-free-usage SKU billed to this pipeline had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
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
  FROM system.billing.usage u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.dlt_pipeline_id
),
billed_hours AS (
  SELECT u.workspace_id, u.usage_metadata.dlt_pipeline_id AS pipeline_id, u.record_id,
         u.usage_start_time, u.usage_end_time, u.usage_quantity,
         COALESCE(u.product_features.is_serverless, FALSE) AS serverless
  FROM system.billing.usage u
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
),
billed_active AS (
  -- whether any update slice overlaps the billed row's hour; serverless rows never linger
  SELECT b.workspace_id, b.pipeline_id, b.record_id, b.usage_quantity, b.serverless,
         MAX(CASE WHEN s.pipeline_id IS NOT NULL THEN 1 ELSE 0 END) AS overlapped
  FROM billed_hours b
  LEFT JOIN system.lakeflow.pipeline_update_timeline s
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
-- NEEDS CONFIRMATION: settings.<key> dot-access is UNVERIFIED (struct vs map).
SELECT u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development,
       COUNT(DISTINCT CASE WHEN u.result_state IS NOT NULL THEN u.update_id END) AS updates,
       -- every hourly slice counts: an update over an hour writes one row per hour, the result on the last
       SUM(unix_timestamp(u.period_end_time) - unix_timestamp(u.period_start_time)) AS active_seconds_total,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       COALESCE(MAX(i.idle_dbus), 0) AS idle_dbus,
       MAX(i.idle_dbus) / NULLIF(MAX(cr.net_dbus), 0) AS idle_share,
       -- status: worst-first band on DBUs billed outside update time (field heuristic; :warn_idle_dbus / :crit_idle_dbus).
       CASE
         WHEN p.setting_continuous = true THEN 'NOT_ASSESSED'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= :crit_idle_dbus THEN 'CRITICAL'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= :warn_idle_dbus THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.lakeflow.pipeline_update_timeline u
LEFT JOIN (
  SELECT workspace_id, pipeline_id, pipeline_type,
         settings.continuous  AS setting_continuous,
         settings.development AS setting_development
  FROM system.lakeflow.pipelines
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
) p
  ON u.workspace_id = p.workspace_id AND u.pipeline_id = p.pipeline_id
LEFT JOIN cost_rollup cr
  ON u.workspace_id = cr.workspace_id AND u.pipeline_id = cr.dlt_pipeline_id
LEFT JOIN idle i
  ON u.workspace_id = i.workspace_id AND u.pipeline_id = i.pipeline_id
WHERE u.period_start_time >= dateadd(day, -:period_days, current_date())
  AND u.period_end_time < date_trunc('DAY', current_timestamp())
GROUP BY u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development
ORDER BY idle_dbus DESC, net_dbus DESC
