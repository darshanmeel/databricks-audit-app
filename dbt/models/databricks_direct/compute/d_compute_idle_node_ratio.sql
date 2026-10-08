{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:compute', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/compute/compute_idle_node_ratio.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH price AS (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM {{ list_prices() }} list_prices
),
cost_rollup AS (
    -- Pre-aggregated per cluster_id (globally-unique GUID -> workspace_id not needed for the join).
    SELECT u.usage_metadata.cluster_id                         AS cluster_id,
           SUM(u.usage_quantity)                               AS net_dbus,
           SUM(u.usage_quantity * COALESCE(p.list_rate, 0))    AS est_usd_list,
           -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (understates cost).
           CASE
             WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                           THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
             WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
             ELSE 'priced'
           END                                                  AS price_basis
    FROM {{ source('system_billing', 'usage') }} u
    LEFT JOIN price p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
     AND u.usage_end_time >= p.price_start_time
     AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
    WHERE upper(u.usage_unit) = 'DBU'
      AND u.usage_metadata.cluster_id IS NOT NULL
      AND u.usage_date >= dateadd(DAY, -LEAST({{ w }}, 90), {{ audit_today() }})
      AND u.usage_date <  {{ audit_today() }}
    GROUP BY u.usage_metadata.cluster_id
),
finding AS (
    SELECT
        cluster_id,
        -- lexicographically-largest node_type label (representative only, not load-weighted)
        MAX(node_type)                                                       AS node_type,
        COUNT(*)                                                             AS total_slices,
        SUM(CASE WHEN NOT driver THEN 1 ELSE 0 END)                          AS worker_slices,
        -- idle = CPU busy (user+system) below {{ param('compute_idle_node_ratio', 'idle_cpu_pct', 5) }} on that minute-slice, worker-only.
        SUM(CASE WHEN NOT driver AND (cpu_user_percent + cpu_system_percent) < {{ param('compute_idle_node_ratio', 'idle_cpu_pct', 5) }}
                 THEN 1 ELSE 0 END)                                          AS worker_idle_slices,
        SUM(CASE WHEN driver THEN 1 ELSE 0 END)                              AS driver_slices,
        -- driver fallback for a genuine single-node cluster (no worker slices at all)
        SUM(CASE WHEN driver AND (cpu_user_percent + cpu_system_percent) < {{ param('compute_idle_node_ratio', 'idle_cpu_pct', 5) }}
                 THEN 1 ELSE 0 END)                                          AS driver_idle_slices,
        COUNT(DISTINCT CASE WHEN driver THEN NULL ELSE node_type END)        AS worker_node_type_variants,
        -- utilization averaged over ALL slices (NOT only idle rows) so the mean is honest.
        AVG(cpu_user_percent + cpu_system_percent)                          AS avg_cpu_pct_all,
        MAX(cpu_user_percent + cpu_system_percent)                          AS peak_cpu_pct_all,
        AVG(mem_used_percent)                                               AS avg_mem_pct_all,
        MAX(mem_used_percent)                                               AS peak_mem_pct_all,
        MIN(start_time)                                                     AS first_slice,
        MAX(end_time)                                                       AS last_slice
    FROM {{ source('system_compute', 'node_timeline') }}
    WHERE start_time >= dateadd(DAY, -LEAST({{ w }}, 90), {{ audit_today() }})
    GROUP BY cluster_id
),
judged AS (
    -- worker slices judge idle when there are any; a single-node cluster (no workers at all)
    -- falls back to its driver instead of being silently dropped.
    SELECT f.*,
           CASE WHEN f.worker_slices > 0 THEN f.worker_slices ELSE f.driver_slices END AS judged_slices,
           CASE WHEN f.worker_slices > 0 THEN f.worker_idle_slices ELSE f.driver_idle_slices END AS judged_idle_slices
    FROM finding f
)
SELECT
    f.cluster_id,
    f.node_type,
    f.total_slices,
    f.judged_idle_slices                                                  AS idle_slices,
    -- idle_ratio = idle minute-slices / judged minute-slices (worker-only, or driver on a
    -- single-node cluster) - a fraction 0-1, never "hours".
    f.judged_idle_slices / NULLIF(f.judged_slices, 0)                     AS idle_ratio,
    f.worker_node_type_variants,
    f.avg_cpu_pct_all,
    f.peak_cpu_pct_all,
    f.avg_mem_pct_all,
    f.peak_mem_pct_all,
    f.first_slice,
    f.last_slice,
    COALESCE(cr.net_dbus, 0)     AS net_dbus,
    COALESCE(cr.est_usd_list, 0) AS est_usd_list,
    COALESCE(cr.price_basis, 'priced') AS price_basis,
    -- directional wasted-$ overlay: idle worker minutes over ALL node minutes, so the driver's
    -- share of the bill is never counted as idle worker waste.
    COALESCE(cr.est_usd_list, 0) * (f.judged_idle_slices / NULLIF(
      CASE WHEN f.worker_slices > 0 THEN f.worker_slices + f.driver_slices ELSE f.driver_slices END, 0)) AS est_wasted_usd_list,
    -- status: worst-first band on idle_ratio (field heuristic). Too few judged slices -> NOT_ASSESSED.
    CASE
      WHEN f.judged_slices < {{ param('compute_idle_node_ratio', 'min_slices', 60) }}                                                THEN 'NOT_ASSESSED'
      WHEN f.judged_idle_slices / NULLIF(f.judged_slices, 0) >= {{ param('compute_idle_node_ratio', 'crit_idle_ratio', 0.8) }}       THEN 'CRITICAL'
      WHEN f.judged_idle_slices / NULLIF(f.judged_slices, 0) >= {{ param('compute_idle_node_ratio', 'warn_idle_ratio', 0.5) }}       THEN 'WARN'
      ELSE 'OK'
    END AS status
FROM judged f
LEFT JOIN cost_rollup cr
  ON f.cluster_id = cr.cluster_id
ORDER BY est_wasted_usd_list DESC
LIMIT {{ param('compute_idle_node_ratio', 'top_n', 200) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
