-- query_id: compute_idle_node_ratio
-- title: Idle classic clusters by idle-slice ratio
-- domain: compute   tier: standard
-- reads: system.compute.node_timeline, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.compute, system.billing; GA
-- empty_if: compute_scope_gap
-- params: :period_days (default 30) rolling window in days, capped at 90 in-SQL by node_timeline retention; :idle_cpu_pct (default 5) CPU busy percent below which a minute-slice counts as idle; :min_slices (default 60) minimum node-minutes before a cluster is judged (fewer -> NOT_ASSESSED); :warn_idle_ratio (default 0.5) idle-slice fraction that flags WARN; :crit_idle_ratio (default 0.8) idle-slice fraction that flags CRITICAL; :top_n (default 200) row cap
-- confidence: confirmed
-- confidence_note: node_timeline columns verified in a live workspace; the est_wasted_usd_list overlay is a directional estimate at the effective list price (DEC-66.1), not a verified invoice figure.
-- read_this: One row = one classic cluster over the window. idle_ratio (idle minute-slices / judged minute-slices, WORKER slices only - a single-node cluster with no workers is judged on its driver instead) is the column that matters; est_wasted_usd_list scales the cluster's effective-list-price DBU cost by its idle worker minutes over all node minutes (driver included in the denominator, so the driver's share is never counted as waste) so you can rank the biggest likely savings. High idle_ratio + meaningful est_wasted_usd_list = an auto-stop / downsizing candidate. price_basis (free/priced/unpriced) discloses whether that cost is a real $0 (free-usage SKU) or a pricing-coverage gap.
-- healthy: idle_ratio below :warn_idle_ratio (field heuristic - tune :warn_idle_ratio for your account).
-- investigate_if: idle_ratio at/above :warn_idle_ratio (WARN) or :crit_idle_ratio (CRITICAL) with real est_wasted_usd_list - field heuristic. BEFORE downsizing, check avg_mem_pct_all: high memory + low CPU means the cluster is memory-bound, NOT idle, so shrinking it will spill.
-- actions: 1) set/shorten auto-termination on the cluster so idle time stops billing (free); 2) enable autoscaling or lower the min workers if the idle is steady-state over-provisioning (config); 3) move the workload to a smaller or memory-optimized node type once you have confirmed it is CPU-idle, not memory-bound (spend).
-- next: compute_warehouse_idle_gaps (if the idle compute is a SQL warehouse, not a classic cluster), cost_by_compute_resource (to see the same cluster's full billed DBUs by day)
-- caveats: node_timeline retention is 90 DAYS ONLY, so :period_days is capped at LEAST(:period_days, 90) in SQL - a longer window silently truncates (read it as "assessed over the last 90 days at most"). Nodes that ran under ~10 minutes MAY NOT APPEAR (short-job blind spot), so a very short cluster is invisible here, not "idle". Classic compute ONLY - there are NO node_timeline rows for SQL warehouses or serverless, so this measure does not cover them. Each row of node_timeline is ONE node-minute (driver AND every worker), so a raw count is NOT hours - idle is reported as a RATIO of slices, never converted to hours. WORKER SLICES ONLY: idle_ratio and est_wasted_usd_list are judged on worker minute-slices alone (the `driver` column excludes the driver), because the driver is doing coordination work even on a fully idle cluster and including it understates idle on a small cluster (a 1 driver + 1 worker cluster with a busy driver and an idle worker used to read a diluted, misleadingly low idle share). A cluster with no worker slices at all in the window (a genuine single-node cluster) falls back to its driver slices instead, so it is still judged, not silently dropped. total_slices stays every node-minute (driver and worker); judged_slices is the worker-or-driver-fallback count :min_slices is checked against. CPU/mem are percents 0-100; mem_used_percent includes background processes. A slice is "idle" by a low-CPU threshold (:idle_cpu_pct) evaluated over every slice in the judged set, never only over already-idle rows. node_timeline carries no DBU/$ column: net_dbus is the exact billed DBUs (usage_unit='DBU') for the cluster and est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), NOT the negotiated invoice rate and excluding cloud infra/egress $. est_wasted_usd_list = est_usd_list x idle worker slices / all node slices is DIRECTIONAL - it assumes every node minute costs the same (a driver on a bigger node type makes it slightly off) and that waste is proportional to idle slices, which over-counts when idle time is cheap warm-standby. Cost is attributed by billing cluster_id over the same window (per-cluster, not per node/slice); cluster_id is a globally-unique GUID so cost keys on cluster_id alone. price_basis is 'unpriced' when any non-free-usage SKU billed to this cluster had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
WITH price AS (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM system.billing.list_prices
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
    FROM system.billing.usage u
    LEFT JOIN price p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
     AND u.usage_end_time >= p.price_start_time
     AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
    WHERE upper(u.usage_unit) = 'DBU'
      AND u.usage_metadata.cluster_id IS NOT NULL
      AND u.usage_date >= dateadd(DAY, -LEAST(:period_days, 90), current_date())
      AND u.usage_date <  current_date()
    GROUP BY u.usage_metadata.cluster_id
),
finding AS (
    SELECT
        cluster_id,
        -- lexicographically-largest node_type label (representative only, not load-weighted)
        MAX(node_type)                                                       AS node_type,
        COUNT(*)                                                             AS total_slices,
        SUM(CASE WHEN NOT driver THEN 1 ELSE 0 END)                          AS worker_slices,
        -- idle = CPU busy (user+system) below :idle_cpu_pct on that minute-slice, worker-only.
        SUM(CASE WHEN NOT driver AND (cpu_user_percent + cpu_system_percent) < :idle_cpu_pct
                 THEN 1 ELSE 0 END)                                          AS worker_idle_slices,
        SUM(CASE WHEN driver THEN 1 ELSE 0 END)                              AS driver_slices,
        -- driver fallback for a genuine single-node cluster (no worker slices at all)
        SUM(CASE WHEN driver AND (cpu_user_percent + cpu_system_percent) < :idle_cpu_pct
                 THEN 1 ELSE 0 END)                                          AS driver_idle_slices,
        COUNT(DISTINCT CASE WHEN driver THEN NULL ELSE node_type END)        AS worker_node_type_variants,
        -- utilization averaged over ALL slices (NOT only idle rows) so the mean is honest.
        AVG(cpu_user_percent + cpu_system_percent)                          AS avg_cpu_pct_all,
        MAX(cpu_user_percent + cpu_system_percent)                          AS peak_cpu_pct_all,
        AVG(mem_used_percent)                                               AS avg_mem_pct_all,
        MAX(mem_used_percent)                                               AS peak_mem_pct_all,
        MIN(start_time)                                                     AS first_slice,
        MAX(end_time)                                                       AS last_slice
    FROM system.compute.node_timeline
    WHERE start_time >= dateadd(DAY, -LEAST(:period_days, 90), current_date())
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
      WHEN f.judged_slices < :min_slices                                                THEN 'NOT_ASSESSED'
      WHEN f.judged_idle_slices / NULLIF(f.judged_slices, 0) >= :crit_idle_ratio       THEN 'CRITICAL'
      WHEN f.judged_idle_slices / NULLIF(f.judged_slices, 0) >= :warn_idle_ratio       THEN 'WARN'
      ELSE 'OK'
    END AS status
FROM judged f
LEFT JOIN cost_rollup cr
  ON f.cluster_id = cr.cluster_id
ORDER BY est_wasted_usd_list DESC
LIMIT :top_n
