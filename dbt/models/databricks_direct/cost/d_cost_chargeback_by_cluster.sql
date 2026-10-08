{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/cost/cost_chargeback_by_cluster.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM {{ source('system_billing', 'usage') }}
),
priced AS (
  SELECT u.workspace_id,
         CASE
           WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'pipeline'
           WHEN u.usage_metadata.job_id IS NULL THEN 'all_purpose'
           ELSE 'job_cluster'
         END AS cluster_kind,
         CASE
           WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN u.usage_metadata.dlt_pipeline_id
           WHEN u.usage_metadata.job_id IS NULL THEN u.usage_metadata.cluster_id
           ELSE u.usage_metadata.job_id
         END AS entity_id,
         u.usage_date, u.sku_name,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM {{ list_prices() }} list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.cluster_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -({{ w }} * 2), {{ audit_today() }})
    AND u.usage_date <  {{ audit_today() }}
),
raw_agg AS (
  SELECT workspace_id, cluster_kind, entity_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
                  THEN list_cost END)                                         AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -{{ w }}, {{ audit_today() }})
                  THEN list_cost END)                                         AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -{{ w }}, {{ audit_today() }})
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -{{ w }}, {{ audit_today() }})
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, cluster_kind, entity_id
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
clusters AS (
  -- clusters/jobs/pipelines/ws below join with IS NOT DISTINCT FROM on workspace_id (a real
  -- account never has a NULL workspace_id here; this only matters for an account-level billing
  -- row, where a plain "=" join to workspace_id would never match a NULL to a NULL).
  SELECT workspace_id, cluster_id, cluster_name, owned_by
  FROM {{ source('system_compute', 'clusters') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
),
jobs AS (
  SELECT workspace_id, job_id, name AS job_name, run_as
  FROM {{ source('system_lakeflow', 'jobs') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
pipelines AS (
  SELECT workspace_id, pipeline_id, name AS pipeline_name, run_as
  FROM {{ source('system_lakeflow', 'pipelines') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
),
ws AS (
  SELECT workspace_id, workspace_name FROM {{ source('system_access', 'workspaces_latest') }}
),
total AS (
  SELECT SUM(eff_current_cost) AS total_current_usd_list FROM agg
),
scored AS (
  SELECT a.workspace_id,
         ws.workspace_name,
         a.cluster_kind,
         a.entity_id,
         CASE a.cluster_kind
           WHEN 'all_purpose' THEN cl.cluster_name
           WHEN 'pipeline'    THEN pl.pipeline_name
           ELSE j.job_name
         END                                                                          AS name,
         CASE a.cluster_kind
           WHEN 'all_purpose' THEN cl.owned_by
           WHEN 'pipeline'    THEN pl.run_as
           ELSE j.run_as
         END                                                                          AS owner,
         a.current_unpriced_quantity, a.previous_unpriced_quantity,
         a.current_priced_quantity, a.previous_priced_quantity,
         a.eff_current_cost, a.eff_previous_cost,
         ROUND(a.eff_current_cost, 2)                                                 AS est_current_usd_list,
         ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_current_usd_list, 0), 1)   AS share_of_total_pct,
         ROUND(a.eff_previous_cost, 2)                                                AS est_previous_usd_list,
         ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                           AS est_change_usd_list,
         ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
         CASE
           WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
           WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
           ELSE 'priced'
         END                                                                           AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -({{ w }} * 2), {{ audit_today() }})
             THEN 'NOT_ASSESSED'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN COALESCE(a.eff_current_cost, 0) < {{ param('cost_chargeback_by_cluster', 'min_spend_usd', 20) }} THEN 'OK'
           WHEN a.eff_previous_cost = 0 THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= {{ param('cost_chargeback_by_cluster', 'crit_increase_pct', 50) }}
             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= {{ param('cost_chargeback_by_cluster', 'warn_increase_pct', 25) }}
             THEN 'WARN'
           ELSE 'OK'
         END                                                                           AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -({{ w }} * 2), {{ audit_today() }})
             THEN 'previous_window_not_covered'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'current_period_unpriced'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'previous_period_unpriced'
           ELSE NULL
         END                                                                           AS not_assessed_reason
  FROM agg a
  LEFT JOIN clusters cl  ON a.cluster_kind = 'all_purpose' AND cl.workspace_id IS NOT DISTINCT FROM a.workspace_id AND cl.cluster_id   = a.entity_id
  LEFT JOIN jobs j       ON a.cluster_kind = 'job_cluster' AND j.workspace_id  IS NOT DISTINCT FROM a.workspace_id AND j.job_id       = a.entity_id
  LEFT JOIN pipelines pl ON a.cluster_kind = 'pipeline'    AND pl.workspace_id IS NOT DISTINCT FROM a.workspace_id AND pl.pipeline_id = a.entity_id
  LEFT JOIN ws           ON ws.workspace_id IS NOT DISTINCT FROM a.workspace_id
  CROSS JOIN snapshot s
  CROSS JOIN total t
),
flagged AS (
  -- Every non-OK row (WARN/CRITICAL/NOT_ASSESSED) always keeps its own row - never pooled.
  SELECT workspace_id, workspace_name, cluster_kind, entity_id, name, owner,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM scored
  WHERE status != 'OK'
),
ok_ranked AS (
  SELECT scored.*,
         ROW_NUMBER() OVER (ORDER BY est_current_usd_list DESC NULLS LAST, cluster_kind, entity_id) AS rn
  FROM scored
  WHERE status = 'OK'
),
ok_kept AS (
  -- The top {{ param('cost_chargeback_by_cluster', 'top_n', 20) }} OK rows by current spend, kept as their own row.
  SELECT workspace_id, workspace_name, cluster_kind, entity_id, name, owner,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM ok_ranked
  WHERE rn <= {{ param('cost_chargeback_by_cluster', 'top_n', 20) }}
),
ok_pooled_raw AS (
  -- Every remaining OK row beyond {{ param('cost_chargeback_by_cluster', 'top_n', 20) }} - re-aggregated into one is_other row below.
  SELECT * FROM ok_ranked WHERE rn > {{ param('cost_chargeback_by_cluster', 'top_n', 20) }}
),
other_row AS (
  SELECT
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS workspace_name,
    CAST(NULL AS STRING) AS cluster_kind,
    CAST(NULL AS STRING) AS entity_id,
    CAST(NULL AS STRING) AS name,
    CAST(NULL AS STRING) AS owner,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(eff_current_cost), 2)                                              AS est_current_usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_current_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                             AS est_previous_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)                     AS est_change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy rows is never
    -- itself a finding, so this row always reads OK regardless of the combined dollars.
    'OK' AS status,
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM ok_pooled_raw
  CROSS JOIN total t
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM ok_kept
  UNION ALL
  SELECT * FROM other_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         est_change_usd_list DESC NULLS LAST,
         workspace_id, cluster_kind, entity_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
