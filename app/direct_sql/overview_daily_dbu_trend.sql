-- generated from dbt/models/databricks_direct/cost/d_overview_daily_dbu_trend.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_daily_dbu_trend.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT
    workspace_id,
    usage_date,
    SUM(usage_quantity) AS net_dbus
FROM `system`.`billing`.`usage`
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
GROUP BY workspace_id, usage_date
ORDER BY usage_date
) q
