-- query_id: lakeflow_pipeline_idle_tail_duration
-- title: Pipeline update activity and idle-tail DBU exposure
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.pipeline_update_timeline, system.lakeflow.pipelines, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.lakeflow, system.billing; Public Preview (system.lakeflow.pipelines and pipeline_update_timeline); GA (system.billing.usage/list_prices)
-- empty_if: schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_idle_dbus (default 100) net DBUs in the window on a non-continuous pipeline that flags WARN; :crit_idle_dbus (default 500) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: the settings.continuous / settings.development dot-access assumes settings is a STRUCT on system.lakeflow.pipelines; whether it is a STRUCT (dot-access) or a MAP (settings['key']) on your account is unverified.
-- read_this: One row = a pipeline. The columns that matter are updates/active_seconds_total (how much the pipeline actually ran) next to net_dbus (what it was billed) - a large gap between billed DBUs and active update time on a non-continuous pipeline suggests the cluster is lingering after the run (idle tail) rather than shutting down promptly.
-- healthy: net_dbus roughly tracks active_seconds_total for non-continuous pipelines, and stays below :warn_idle_dbus for the window - field heuristic; tune :warn_idle_dbus for your account.
-- investigate_if: net_dbus at/above :warn_idle_dbus (WARN) or :crit_idle_dbus (CRITICAL) on a pipeline where setting_continuous is false and active_seconds_total looks small relative to net_dbus - field heuristic.
-- actions: 1) check the pipeline's cluster auto-termination / shutdown-delay setting (free); 2) shorten the idle shutdown delay or move the pipeline to serverless DLT compute (config); 3) if the workload genuinely needs a warm cluster between runs, that is a deliberate spend - just confirm it is intentional (spend).
-- next: lakeflow_pipeline_cost (for the full per-pipeline DBU picture), lakeflow_pipelines_inventory_tier (for the pipeline's serverless/continuous/development settings)
-- caveats: The active update window (period_start_time/period_end_time) is confirmed. The idle tail itself (post-run cluster lingering) is NOT a lakeflow column - this corroborates it via serverless/DLT DBU in system.billing.usage joined by usage_metadata.dlt_pipeline_id, so treat net_dbus vs active_seconds_total as a proxy, not a direct idle-time measurement. A cluster-shutdown-delay-style setting is not in the documented schema and is deliberately not emitted here. net_dbus is the exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, and it excludes cloud infra/egress cost - treat it as directional. Cost is attributed by (workspace_id, dlt_pipeline_id) over the window (per-pipeline), not per update/event; the rollup is pre-aggregated then LEFT JOINed, so result rows are never multiplied. price_basis is 'unpriced' when any non-free-usage SKU billed to this pipeline had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
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
)
-- NEEDS CONFIRMATION: settings.<key> dot-access is UNVERIFIED (struct vs map).
SELECT u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development,
       COUNT(DISTINCT u.update_id) AS updates,
       SUM(unix_timestamp(u.period_end_time) - unix_timestamp(u.period_start_time)) AS active_seconds_total,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       -- status: worst-first band on net DBUs for a non-continuous pipeline (field heuristic; :warn_idle_dbus / :crit_idle_dbus).
       CASE
         WHEN p.setting_continuous = true THEN 'NOT_ASSESSED'
         WHEN COALESCE(MAX(cr.net_dbus), 0) >= :crit_idle_dbus THEN 'CRITICAL'
         WHEN COALESCE(MAX(cr.net_dbus), 0) >= :warn_idle_dbus THEN 'WARN'
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
WHERE u.period_start_time >= dateadd(day, -:period_days, current_date())
  AND u.period_end_time < date_trunc('DAY', current_timestamp())
  AND u.result_state IS NOT NULL
GROUP BY u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development
ORDER BY net_dbus DESC
