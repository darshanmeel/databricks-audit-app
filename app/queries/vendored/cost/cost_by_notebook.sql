-- query_id: cost_by_notebook
-- title: DBU cost by notebook (ad-hoc usage)
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days; :warn_notebook_dbus_per_day (default 10) DBUs/day on a single notebook that flags WARN; :crit_notebook_dbus_per_day (default 50) DBUs/day that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: usage_metadata.notebook_id is documented but populates only for notebook-attached all-purpose/interactive usage - confirm it is non-empty in your account before relying on a per-notebook view.
-- read_this: One row = a day + workspace + notebook's ad-hoc DBU cost. The column that matters is net_usage_quantity - ad-hoc/interactive spend is normally a small slice of total DBUs, so a notebook that clears the WARN/CRITICAL band is the runaway exception, not the norm.
-- healthy: net_usage_quantity below :warn_notebook_dbus_per_day DBUs/day per notebook (field heuristic - tune :warn_notebook_dbus_per_day for your account; ad-hoc spend is expected to be a small share of the total).
-- investigate_if: net_usage_quantity at/above :warn_notebook_dbus_per_day (WARN) or :crit_notebook_dbus_per_day (CRITICAL) DBUs/day - field heuristic for a runaway ad-hoc notebook left running on an expensive all-purpose cluster.
-- actions: 1) ask the notebook's owner whether it is meant to run unattended at this size (free); 2) attach the notebook to a smaller/auto-terminating all-purpose cluster, or move the workload into a scheduled job (config); 3) if the workload is legitimately heavy interactive analysis, size the cluster deliberately rather than leaving it on defaults (spend).
-- next: cost_by_compute_resource (to see which cluster is absorbing the notebook's cost), cost_by_job (to compare against scheduled-job DBU cost in the same window)
-- caveats: usage_metadata.notebook_id populates for notebook-attached interactive/all-purpose usage; it is NULL for jobs/serverless-editor/SQL and those are excluded here by construction. There is no notebook name in billing data (usage_metadata.notebook_path may exist - verify per account). Ad-hoc notebook cost is expected to be a small share of total spend, so this both confirms/quantifies that baseline and flags any single runaway notebook. usage_quantity is DBU, not dollars.
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.notebook_id     AS notebook_id,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       -- status: magnitude band on daily DBU cost per notebook (field heuristic; :warn_notebook_dbus_per_day / :crit_notebook_dbus_per_day).
       CASE
         WHEN SUM(usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(usage_quantity) >= :crit_notebook_dbus_per_day THEN 'CRITICAL'
         WHEN SUM(usage_quantity) >= :warn_notebook_dbus_per_day THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
  AND usage_metadata.notebook_id IS NOT NULL
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.notebook_id, product_features.is_serverless
ORDER BY net_usage_quantity DESC
