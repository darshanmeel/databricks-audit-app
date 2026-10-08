{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/cost/cost_chargeback_by_allocation_tag.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH priced_usage AS (
  -- Every usage row in the window, priced at the effective list price (DEC-66.1, the exact join
  -- T-69A settled on), carrying its own custom_tags map and sku_name for the price_basis check
  -- below. One row per system.billing.usage row (record_id is unique per row).
  SELECT u.record_id, u.workspace_id, u.usage_unit, u.usage_quantity, u.sku_name, u.custom_tags,
         lp.list_rate,
         u.usage_quantity * lp.list_rate AS list_cost
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
  WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND u.usage_date < {{ audit_today() }}
),
tag_pairs AS (
  -- one row per (usage row, custom_tags key/value pair); OUTER so a usage row with no tags at
  -- all still appears once with tag_key/tag_value NULL, same explode cost_chargeback_by_tag uses.
  SELECT p.record_id, p.workspace_id, p.usage_unit, p.usage_quantity, p.sku_name, p.list_rate,
         p.list_cost, t.tag_key, t.tag_value
  FROM priced_usage p
       LATERAL VIEW OUTER explode(p.custom_tags) t AS tag_key, tag_value
),
ranked AS (
  -- DEC-60 rule 4 normalization. A key earlier in the allocation_keys list outranks a later one when a
  -- row carries both; a row whose tag_key matches none (or has no tags at all) ranks NULL.
  SELECT record_id, workspace_id, usage_unit, usage_quantity, sku_name, list_rate, list_cost,
         tag_key, tag_value, norm_key,
         NULLIF(instr(concat(',', {{ param('cost_chargeback_by_allocation_tag', 'allocation_keys', 'costcenter,team') }}, ','), concat(',', norm_key, ',')), 0) AS key_rank
  FROM (
    SELECT tag_pairs.*, lower(replace(replace(replace(tag_key, ' ', ''), '-', ''), '_', '')) AS norm_key
    FROM tag_pairs
  ) n
),
picked AS (
  -- ONE allocation identity per usage row: the highest-priority matching key's own value, or
  -- NULL (allocation_key NULL too) when the row matches neither key at all -- even when it
  -- carries OTHER tags (env, owner, ...), which is deliberately different from
  -- cost_chargeback_by_tag's "no tags at all" reading of untagged.
  SELECT record_id, workspace_id, usage_unit, usage_quantity, sku_name, list_rate, list_cost,
         CASE WHEN key_rank IS NULL THEN NULL WHEN norm_key = 'costcenter' THEN 'cost_center' ELSE norm_key END AS allocation_key,
         CASE WHEN key_rank IS NOT NULL THEN tag_value END              AS allocation_value
  FROM ranked
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY record_id
    ORDER BY CASE WHEN key_rank IS NULL THEN 1 ELSE 0 END, key_rank
  ) = 1
),
agg AS (
  SELECT workspace_id, usage_unit, allocation_key, allocation_value,
         SUM(usage_quantity)                                                 AS net_usage_quantity,
         SUM(list_cost)                                                      AS net_list_cost_usd,
         SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS unpriced_quantity,
         SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity ELSE 0 END) AS priced_quantity
  FROM picked
  GROUP BY workspace_id, usage_unit, allocation_key, allocation_value
),
unit_totals AS (
  -- this workspace + usage_unit's own total priced dollars across every allocation group
  -- (matched and missing) -- never summed across usage_unit (DEC-24 / this query's own contract).
  SELECT workspace_id, usage_unit, SUM(net_list_cost_usd) AS unit_total_cost_usd
  FROM agg
  GROUP BY workspace_id, usage_unit
)
SELECT a.workspace_id,
       a.usage_unit,
       a.allocation_key,
       a.allocation_value,
       (a.allocation_value IS NULL)                                             AS is_missing_allocation_key,
       ROUND(a.net_usage_quantity, 4)                                           AS net_usage_quantity,
       ROUND(a.net_list_cost_usd, 2)                                            AS net_list_cost_usd,
       ROUND(
         CASE WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0 THEN 0 ELSE a.net_list_cost_usd END
         * 100.0 / NULLIF(t.unit_total_cost_usd, 0), 1
       )                                                                         AS share_of_unit_pct,
       CASE
         WHEN a.unpriced_quantity > 0 THEN 'unpriced'
         WHEN a.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN a.allocation_value IS NOT NULL THEN 'OK'
         WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0           THEN 'OK'
         WHEN a.net_list_cost_usd IS NULL
              OR t.unit_total_cost_usd IS NULL
              OR t.unit_total_cost_usd = 0                                THEN 'NOT_ASSESSED'
         WHEN (a.net_list_cost_usd * 100.0 / t.unit_total_cost_usd) >= {{ param('cost_chargeback_by_allocation_tag', 'crit_missing_pct', 50) }}
                                                                           THEN 'CRITICAL'
         WHEN (a.net_list_cost_usd * 100.0 / t.unit_total_cost_usd) >= {{ param('cost_chargeback_by_allocation_tag', 'warn_missing_pct', 20) }}
                                                                           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.allocation_value IS NOT NULL THEN NULL
         WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0           THEN NULL
         WHEN a.net_list_cost_usd IS NULL THEN 'missing_allocation_spend_unpriced'
         WHEN t.unit_total_cost_usd IS NULL OR t.unit_total_cost_usd = 0
                                              THEN 'no_priced_spend_in_window'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
JOIN unit_totals t
  ON t.workspace_id IS NOT DISTINCT FROM a.workspace_id AND t.usage_unit = a.usage_unit
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         a.workspace_id, a.usage_unit, is_missing_allocation_key DESC, a.net_list_cost_usd DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
