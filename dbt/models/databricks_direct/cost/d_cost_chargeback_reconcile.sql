{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/cost/cost_chargeback_reconcile.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH window_usage AS (
  SELECT u.record_id, u.workspace_id, u.usage_date, u.usage_end_time, u.sku_name, u.cloud,
         u.usage_unit, u.usage_quantity, u.billing_origin_product, u.custom_tags,
         u.usage_metadata.warehouse_id AS warehouse_id,
         u.usage_metadata.job_id       AS job_id,
         u.usage_metadata.cluster_id   AS cluster_id
  FROM {{ source('system_billing', 'usage') }} u
  WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND u.usage_date <  {{ audit_today() }}
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM {{ source('system_billing', 'list_prices') }}
),
raw_priced AS (
  -- the plain, non-deduped join every chargeback query in this set uses.
  SELECT w.*, p.list_rate, w.usage_quantity * p.list_rate AS usd_list
  FROM window_usage w
  LEFT JOIN price p
    ON  w.sku_name = p.sku_name AND w.cloud = p.cloud AND w.usage_unit = p.usage_unit
    AND w.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR w.usage_end_time < p.price_end_time)
),
raw_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced GROUP BY workspace_id
),
deduped_priced AS (
  -- exactly one matching price row per usage row (most recently started wins), to check whether
  -- the plain join above ever fans out on an overlapping list_prices validity window.
  SELECT w.record_id, w.workspace_id, w.usage_quantity * p.list_rate AS usd_list
  FROM window_usage w
  LEFT JOIN price p
    ON  w.sku_name = p.sku_name AND w.cloud = p.cloud AND w.usage_unit = p.usage_unit
    AND w.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR w.usage_end_time < p.price_end_time)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY w.record_id ORDER BY p.price_start_time DESC) = 1
),
deduped_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM deduped_priced GROUP BY workspace_id
),
by_tag_value_total AS (
  -- deliberately NOT expected to equal the billing total: cost_chargeback_by_tag_value explodes
  -- EVERY key a usage row carries, so a resource tagged with N keys contributes its dollars N
  -- times here - the 'overlapping' coverage_type below labels this as expected, not a bug.
  SELECT rp.workspace_id, SUM(rp.usd_list) AS total_usd_list
  FROM raw_priced rp
  LATERAL VIEW OUTER explode(rp.custom_tags) t AS tag_key, tag_value
  GROUP BY rp.workspace_id
),
by_user_buckets_total AS (
  -- the same three mutually exclusive conditions a per-identity chargeback view splits usage
  -- across: warehouse DBU, job DBU, or neither (serverless/model-serving/storage/etc.).
  SELECT workspace_id,
    SUM(CASE WHEN upper(usage_unit) = 'DBU' AND warehouse_id IS NOT NULL THEN usd_list ELSE 0 END)
    + SUM(CASE WHEN upper(usage_unit) = 'DBU' AND job_id IS NOT NULL THEN usd_list ELSE 0 END)
    + SUM(CASE WHEN warehouse_id IS NULL AND job_id IS NULL THEN usd_list ELSE 0 END) AS total_usd_list
  FROM raw_priced
  GROUP BY workspace_id
),
by_warehouse_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND warehouse_id IS NOT NULL
  GROUP BY workspace_id
),
by_job_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND job_id IS NOT NULL
  GROUP BY workspace_id
),
by_cluster_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND cluster_id IS NOT NULL   -- same scope cost_chargeback_by_cluster's own WHERE uses, both all_purpose and job_cluster kinds
  GROUP BY workspace_id
),
checks AS (
  -- LEFT JOIN off raw_total (every workspace_id present in the window) so a workspace with $0 on
  -- a partial check's own scope (by_warehouse/by_job/by_cluster) still gets a row here, not a
  -- silently dropped one; IS NOT DISTINCT FROM so the NULL (account-level) workspace_id matches
  -- itself instead of failing a plain `=` join.
  SELECT 'price_join_fanout' AS check_name, 'full' AS coverage_type, r.workspace_id,
         d.total_usd_list AS billing_total_usd_list, r.total_usd_list AS view_total_usd_list
  FROM raw_total r LEFT JOIN deduped_total d ON d.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_tag_value', 'overlapping', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_tag_value_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_user', 'full', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_user_buckets_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_warehouse', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_warehouse_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_job', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_job_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_cluster', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_cluster_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
)
SELECT c.check_name,
       c.workspace_id,
       c.coverage_type,
       ROUND(c.billing_total_usd_list, 2)                                        AS billing_total_usd_list,
       ROUND(c.view_total_usd_list, 2)                                           AS view_total_usd_list,
       ROUND(c.view_total_usd_list - c.billing_total_usd_list, 2)                AS gap_usd_list,
       ROUND((c.view_total_usd_list - c.billing_total_usd_list) * 100.0
             / NULLIF(c.billing_total_usd_list, 0), 2)                           AS gap_pct,
       CASE WHEN c.coverage_type IN ('partial', 'overlapping')
            THEN ROUND(c.view_total_usd_list * 100.0 / NULLIF(c.billing_total_usd_list, 0), 1)
       END                                                                        AS coverage_pct,
       CASE
         WHEN c.coverage_type IN ('partial', 'overlapping') THEN 'OK'
         WHEN c.billing_total_usd_list IS NULL OR c.billing_total_usd_list = 0 THEN 'NOT_ASSESSED'
         WHEN ABS(c.view_total_usd_list - c.billing_total_usd_list) * 100.0 / c.billing_total_usd_list
              >= {{ param('cost_chargeback_reconcile', 'gap_crit_pct', 5) }} THEN 'CRITICAL'
         WHEN ABS(c.view_total_usd_list - c.billing_total_usd_list) * 100.0 / c.billing_total_usd_list
              >= {{ param('cost_chargeback_reconcile', 'gap_warn_pct', 1) }} THEN 'WARN'
         ELSE 'OK'
       END                                                                        AS status
FROM checks c
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         c.coverage_type, c.check_name, c.workspace_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
