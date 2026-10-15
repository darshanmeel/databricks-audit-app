-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_config_posture.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_config_posture.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`warehouses`
),
current_warehouses AS (
  SELECT * FROM latest WHERE rn = 1 AND delete_time IS NULL
),
flagged AS (
  SELECT
    warehouse_id, workspace_id, account_id, warehouse_name, created_by, warehouse_type,
    warehouse_channel, warehouse_size, min_clusters, max_clusters, auto_stop_minutes, change_time,
    (auto_stop_minutes IS NULL OR auto_stop_minutes = 0
     OR auto_stop_minutes > 60)          AS flag_autostop_risk,
    (warehouse_type = 'CLASSIC')                             AS flag_classic_type,
    (warehouse_channel IN ('CHANNEL_NAME_PREVIEW', 'PREVIEW')) AS flag_preview_channel
  FROM current_warehouses
)
SELECT
  warehouse_id,
  workspace_id,
  warehouse_name,
  created_by                                                         AS created_by,
  warehouse_type,
  warehouse_channel,
  warehouse_size,
  min_clusters, max_clusters, auto_stop_minutes,
  flag_autostop_risk,
  flag_classic_type,
  flag_preview_channel,
  CONCAT_WS('; ',
    CASE WHEN flag_autostop_risk THEN
      CASE
        WHEN auto_stop_minutes IS NULL OR auto_stop_minutes = 0 THEN 'auto-stop is off'
        ELSE CONCAT('auto-stop ', CAST(auto_stop_minutes AS STRING), ' min, above the ',
                     CAST(60 AS STRING), ' min limit')
      END
    END,
    CASE WHEN flag_classic_type THEN
      'CLASSIC type; Pro/Serverless offers faster start-up and Unity Catalog-native governance'
    END,
    CASE WHEN flag_preview_channel THEN
      'on the Preview channel (pre-release features, confirm this is intentional)'
    END
  )                                                           AS reasons,
  CASE
    WHEN flag_autostop_risk OR flag_classic_type OR flag_preview_channel THEN 'WARN'
    ELSE 'OK'
  END                                                         AS status,
  change_time
FROM flagged
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END,
         warehouse_id
) q
