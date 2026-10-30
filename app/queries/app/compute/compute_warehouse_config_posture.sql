-- query_id: compute_warehouse_config_posture
-- title: SQL warehouse setup risk - auto-stop left off or too high, CLASSIC type where
--   Pro/Serverless is available, running on the Preview channel
-- domain: compute   tier: standard
-- reads: system.compute.warehouses
-- requires: SELECT on system.compute; GA
-- params: :max_autostop_minutes (default 60) minutes above which a warehouse's auto-stop is
--   flagged as too high (NULL or 0 is flagged as off, at any value)
-- confidence: needs_confirmation
-- confidence_note: Columns and the SCD2 latest-row-per-warehouse_id shape are the ones
--   sql_warehouse_config_current already confirms against a live system-catalog schema dump.
--   Two things are NOT confirmed on a real account: 1) warehouse_channel's exact stored spelling
--   - the Databricks REST API's channel enum is CHANNEL_NAME_CURRENT / CHANNEL_NAME_PREVIEW, but
--   this system table's own value has only been checked against this app's own fixtures, never a
--   live dump, so flag_preview_channel matches both CHANNEL_NAME_PREVIEW and the bare 'PREVIEW'
--   spelling the API also uses elsewhere, to fail safe either way rather than silently missing a
--   real account that stores the shorter form; 2) that warehouse_type = 'CLASSIC' is the only
--   non-Pro/Serverless value on that column today (Databricks' own warehouse-type docs describe
--   CLASSIC vs PRO, with Serverless controlled separately) - verify against your account's actual
--   warehouse_type values before trusting the classic_type flag.
-- read_this: One row = the latest known configuration for one SQL warehouse that has not been
--   deleted - the same grain and dedupe as sql_warehouse_config_current, with a posture verdict
--   added. The columns that matter are status (worst flag wins) and reasons (every flag that
--   fired, in one line).
-- healthy: status = OK - no flag fired. Field heuristic; :max_autostop_minutes is an
--   approximation, tune it for your account's typical query cadence.
-- investigate_if: status = WARN - auto-stop off or above :max_autostop_minutes, warehouse_type
--   is CLASSIC, or warehouse_channel is the Preview channel. Read reasons for the exact
--   combination; more than one flag can fire on the same row. This query has no CRITICAL band -
--   none of its flags is a governance bypass the way an unmasked cluster access mode is
--   (see compute_cluster_config_posture), they are cost/support-tier risks.
-- actions: 1) lower or set auto_stop_minutes on the flagged warehouse so it suspends when idle
--   (free); 2) move the warehouse off the Preview channel onto Current unless the preview
--   feature is deliberately being evaluated (config); 3) move a CLASSIC warehouse to Pro or
--   Serverless SQL for faster start-up and Unity-Catalog-native governance (spend).
-- next: sql_warehouse_config_current (full configuration), compute_warehouse_idle_gaps (this
--   warehouse's actual idle tail against its auto_stop_minutes), compute_warehouse_autoscale_churn
--   (if it is also thrashing clusters), query_warehouse_pressure (if it is also under memory or
--   capacity pressure), compute_cluster_config_posture (the classic-cluster half of this same
--   posture check)
-- caveats: SCD snapshot: this returns the latest row per warehouse_id, so a deleted warehouse
--   (delete_time NOT NULL) is excluded - the same filter sql_warehouse_config_current uses.
--   warehouse_size enum includes 5X_LARGE (Beta, PRO/SERVERLESS channel only -
--   sql_warehouse_config_current's own confirmed caveat) but warehouse_size itself is not judged
--   here, only warehouse_type/warehouse_channel/auto_stop_minutes. The Preview-channel flag is a
--   support-tier signal, not a defect: a workspace running a warehouse on Preview on purpose (to
--   try a new feature early) should expect this WARN and can ignore it once confirmed
--   intentional - there is no "deliberately on preview" column to suppress it automatically.
--   auto_stop_minutes NULL or 0 is treated as "auto-stop is off" (a warehouse that never
--   suspends keeps billing DBUs indefinitely); this is the same off-or-above-a-cap shape
--   compute_cluster_config_posture uses for cluster auto-termination, kept consistent across
--   both posture queries on purpose. warehouse_name is masked to the existing 2-char-prefix mask
--   and created_by to the DEC-66.3 identity format (`<id> <first 2 chars>***`, a hash-derived id
--   when the source row carries no real user id, service-principal GUIDs passed through
--   unchanged) - both copied verbatim from sql_warehouse_config_current's / classic_clusters_
--   config_current's own existing masks, not reinvented. tags is a map, not read here. Regional -
--   run per metastore region. No dollars here: this is a configuration posture check, not a cost
--   query; price the flagged warehouse with compute_warehouse_idle_gaps or the relevant cost_by_*
--   query.
WITH latest AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM system.compute.warehouses
),
current_warehouses AS (
  SELECT * FROM latest WHERE rn = 1 AND delete_time IS NULL
),
flagged AS (
  SELECT
    warehouse_id, workspace_id, account_id, warehouse_name, created_by, warehouse_type,
    warehouse_channel, warehouse_size, min_clusters, max_clusters, auto_stop_minutes, change_time,
    (auto_stop_minutes IS NULL OR auto_stop_minutes = 0
     OR auto_stop_minutes > :max_autostop_minutes)          AS flag_autostop_risk,
    (warehouse_type = 'CLASSIC')                             AS flag_classic_type,
    (warehouse_channel IN ('CHANNEL_NAME_PREVIEW', 'PREVIEW')) AS flag_preview_channel
  FROM current_warehouses
)
SELECT
  warehouse_id,
  workspace_id,
  warehouse_name,
  CASE
    WHEN created_by IS NULL OR created_by = '__REDACTED__' THEN created_by
    WHEN created_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
      THEN created_by
    ELSE concat(substr(sha2(lower(trim(created_by)), 256), 1, 8), ' ', substr(created_by, 1, 2), '***')
  END                                                         AS created_by,
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
                     CAST(:max_autostop_minutes AS STRING), ' min limit')
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
