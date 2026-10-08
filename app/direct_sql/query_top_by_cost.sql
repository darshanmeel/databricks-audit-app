-- generated from dbt/models/databricks_direct/performance/d_query_top_by_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/query_top_by_cost.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
bill_hour AS (
  -- one row per SQL warehouse per billed hour: its list-price dollars in that hour
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         date_trunc('HOUR', u.usage_start_time) AS usage_hour,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS hour_usd
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND u.usage_date < __AS_OF_DATE__
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, date_trunc('HOUR', u.usage_start_time)
),
wh_total AS (
  -- each warehouse's own total window spend - the denominator for share_of_warehouse_cost
  SELECT workspace_id, warehouse_id, SUM(hour_usd) AS warehouse_usd_list
  FROM bill_hour
  GROUP BY workspace_id, warehouse_id
),
stmts AS (
  SELECT q.workspace_id,
         q.compute.warehouse_id                          AS warehouse_id,
         q.statement_id,
         q.statement_type,
         q.statement_text,
         q.executed_by,
         q.query_source,
         q.start_time,
         COALESCE(q.end_time, q.update_time, q.start_time) AS end_time,
         COALESCE(q.total_task_duration_ms, q.execution_duration_ms, 0) AS weight_ms,
         COALESCE(q.execution_duration_ms, 0)             AS execution_ms,
         COALESCE(q.read_bytes, 0)                        AS read_bytes,
         COALESCE(q.spilled_local_bytes, 0)               AS spilled_local_bytes,
         COALESCE(q.read_files, 0)                        AS read_files,
         COALESCE(q.pruned_files, 0)                      AS pruned_files,
         CASE WHEN q.from_result_cache THEN 1 ELSE 0 END  AS cached
  FROM `system`.`query`.`history` q
  WHERE q.start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND q.start_time < __AS_OF_DATE__
    AND q.compute.warehouse_id IS NOT NULL
),
stmt_hours AS (
  -- every billed hour a statement's own [start_time, end_time) overlaps, in milliseconds so a
  -- sub-second statement still gets weight (unix_timestamp truncates to whole seconds and would
  -- floor a sub-second statement to zero overlap) - full weight when it fits in one hour, otherwise
  -- its share of that hour by overlap_ms over duration_ms - a 3-hour query is priced across all 3
  -- hours, not billed whole to its start hour
  SELECT s.workspace_id, s.warehouse_id, s.statement_id, bh.usage_hour,
         GREATEST(unix_millis(s.end_time) - unix_millis(s.start_time), 1) AS duration_ms,
         CASE WHEN date_trunc('HOUR', s.start_time) = date_trunc('HOUR', s.end_time)
                THEN GREATEST(unix_millis(s.end_time) - unix_millis(s.start_time), 1)
              ELSE LEAST(unix_millis(s.end_time), unix_millis(bh.usage_hour) + 3600000)
                     - GREATEST(unix_millis(s.start_time), unix_millis(bh.usage_hour))
         END AS overlap_ms
  FROM stmts s
  JOIN bill_hour bh
    ON  bh.workspace_id = s.workspace_id AND bh.warehouse_id = s.warehouse_id
    AND bh.usage_hour BETWEEN date_trunc('HOUR', s.start_time) AND date_trunc('HOUR', s.end_time)
),
stmt_hour_weight AS (
  SELECT sh.workspace_id, sh.warehouse_id, sh.statement_id, sh.usage_hour,
         s.weight_ms * sh.overlap_ms / sh.duration_ms AS hour_weight_ms
  FROM stmt_hours sh
  JOIN stmts s
    ON  s.workspace_id = sh.workspace_id AND s.warehouse_id = sh.warehouse_id
    AND s.statement_id = sh.statement_id
),
hour_totals AS (
  -- total statement weight per warehouse per hour - the denominator for each statement-hour's share
  SELECT workspace_id, warehouse_id, usage_hour, SUM(hour_weight_ms) AS total_weight_ms
  FROM stmt_hour_weight
  GROUP BY workspace_id, warehouse_id, usage_hour
),
stmt_cost AS (
  -- each statement's total cost: its share of every billed hour it overlaps, summed
  SELECT w.workspace_id, w.warehouse_id, w.statement_id,
         SUM(CASE WHEN ht.total_weight_ms > 0 AND bh.hour_usd IS NOT NULL
                  THEN bh.hour_usd * w.hour_weight_ms / ht.total_weight_ms END) AS stmt_cost_usd
  FROM stmt_hour_weight w
  JOIN hour_totals ht
    ON  ht.workspace_id = w.workspace_id AND ht.warehouse_id = w.warehouse_id AND ht.usage_hour = w.usage_hour
  LEFT JOIN bill_hour bh
    ON  bh.workspace_id = w.workspace_id AND bh.warehouse_id = w.warehouse_id AND bh.usage_hour = w.usage_hour
  GROUP BY w.workspace_id, w.warehouse_id, w.statement_id
),
costed AS (
  SELECT s.*, sc.stmt_cost_usd
  FROM stmts s
  LEFT JOIN stmt_cost sc
    ON  sc.workspace_id = s.workspace_id AND sc.warehouse_id = s.warehouse_id
    AND sc.statement_id = s.statement_id
),
hashed AS (
  -- the de-valued-text sha2 hash (query_costly_statements_grouped's own recipe), NULL when the
  -- text is not real (never NULL, never '<REDACTED>')
  SELECT c.*,
         CASE WHEN c.statement_text IS NULL OR c.statement_text = '<REDACTED>' THEN NULL
              ELSE sha2(
                regexp_replace(
                  regexp_replace(c.statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
                  concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
                ), 256)
         END AS text_hash
  FROM costed c
),
origin AS (
  SELECT h.*,
         CASE
           WHEN h.text_hash IS NOT NULL                            THEN 'text hash'
           WHEN h.query_source.sql_query_id IS NOT NULL            THEN 'saved query'
           WHEN h.query_source.dashboard_id IS NOT NULL            THEN 'dashboard'
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL     THEN 'dashboard'
           WHEN h.query_source.genie_space_id IS NOT NULL          THEN 'Genie space'
           WHEN h.query_source.alert_id IS NOT NULL                THEN 'alert'
           WHEN h.query_source.job_info.job_id IS NOT NULL         THEN 'job'
           WHEN h.query_source.notebook_id IS NOT NULL             THEN 'notebook'
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
           ELSE 'ad-hoc, text hidden'
         END AS group_kind,
         CASE
           WHEN h.text_hash IS NOT NULL                        THEN h.text_hash
           WHEN h.query_source.sql_query_id IS NOT NULL        THEN h.query_source.sql_query_id
           WHEN h.query_source.dashboard_id IS NOT NULL        THEN h.query_source.dashboard_id
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL THEN h.query_source.legacy_dashboard_id
           WHEN h.query_source.genie_space_id IS NOT NULL      THEN h.query_source.genie_space_id
           WHEN h.query_source.alert_id IS NOT NULL            THEN h.query_source.alert_id
           WHEN h.query_source.job_info.job_id IS NOT NULL        THEN h.query_source.job_info.job_id
           WHEN h.query_source.notebook_id IS NOT NULL              THEN h.query_source.notebook_id
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN h.query_source.pipeline_info.pipeline_id
           ELSE
             h.executed_by
         END AS group_id,
         -- the group's real origin, independent of group_kind - a "text hash" group still names
         -- its dashboard/job/notebook/etc. source here when one is captured; NULL only when the
         -- statement is genuinely ad-hoc with no query_source at all
         CASE
           WHEN h.query_source.sql_query_id IS NOT NULL              THEN 'saved query'
           WHEN h.query_source.dashboard_id IS NOT NULL              THEN 'dashboard'
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL       THEN 'dashboard'
           WHEN h.query_source.genie_space_id IS NOT NULL            THEN 'Genie space'
           WHEN h.query_source.alert_id IS NOT NULL                  THEN 'alert'
           WHEN h.query_source.job_info.job_id IS NOT NULL           THEN 'job'
           WHEN h.query_source.notebook_id IS NOT NULL               THEN 'notebook'
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
         END AS source_kind,
         CASE
           WHEN h.query_source.sql_query_id IS NOT NULL              THEN h.query_source.sql_query_id
           WHEN h.query_source.dashboard_id IS NOT NULL              THEN h.query_source.dashboard_id
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL       THEN h.query_source.legacy_dashboard_id
           WHEN h.query_source.genie_space_id IS NOT NULL            THEN h.query_source.genie_space_id
           WHEN h.query_source.alert_id IS NOT NULL                  THEN h.query_source.alert_id
           WHEN h.query_source.job_info.job_id IS NOT NULL           THEN h.query_source.job_info.job_id
           WHEN h.query_source.notebook_id IS NOT NULL               THEN h.query_source.notebook_id
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN h.query_source.pipeline_info.pipeline_id
         END AS source_id
  FROM hashed h
),
user_agg AS (
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type, executed_by,
         COUNT(*) AS user_runs
  FROM origin
  GROUP BY workspace_id, warehouse_id, group_kind, group_id, statement_type, executed_by
),
top_user_pick AS (
  -- the group's most frequent user, masked to DEC-66.3's format
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type,
         executed_by AS top_user
  FROM user_agg
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, group_kind, group_id, statement_type
                             ORDER BY user_runs DESC, executed_by) = 1
),
sample_pick AS (
  -- one representative statement per group: its latest run, still in Databricks' query history -
  -- its own source names the group's
  -- real origin too, even for a "text hash" group grouped across statements from more than one
  -- source
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type,
         statement_id AS sample_statement_id, source_kind, source_id
  FROM origin
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, group_kind, group_id, statement_type
                             ORDER BY start_time DESC, statement_id) = 1
),
agg AS (
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type,
         COUNT(*)                                    AS runs,
         -- COALESCE so a group whose runs all carry a NULL identity still counts as 1 user, not 0
         -- (COUNT(DISTINCT executed_by) alone drops every NULL from the count)
         COUNT(DISTINCT COALESCE(executed_by, '__no_identity__')) AS distinct_users,
         SUM(stmt_cost_usd)                           AS est_cost_usd_list,
         SUM(execution_ms)                            AS total_exec_ms,
         CAST(AVG(execution_ms) AS BIGINT)            AS avg_exec_ms,
         MAX(execution_ms)                            AS max_exec_ms,
         CAST(percentile(execution_ms, 0.5) AS BIGINT)  AS p50_duration_ms,
         CAST(percentile(execution_ms, 0.95) AS BIGINT) AS p95_duration_ms,
         SUM(read_bytes)                              AS read_bytes,
         SUM(spilled_local_bytes)                     AS spilled_local_bytes,
         SUM(read_files)                              AS read_files,
         SUM(pruned_files)                            AS pruned_files,
         ROUND(SUM(cached) * 1.0 / COUNT(*), 3)       AS from_result_cache_share,
         MIN(start_time)                              AS first_seen,
         MAX(start_time)                              AS last_seen
  FROM origin
  GROUP BY workspace_id, warehouse_id, group_kind, group_id, statement_type
)
SELECT a.workspace_id,
       a.warehouse_id,
       a.group_kind,
       a.group_id,
       sp.source_kind,
       sp.source_id,
       a.statement_type,
       a.runs,
       a.distinct_users,
       tu.top_user,
       ROUND(a.est_cost_usd_list, 2) AS est_cost_usd_list,
       CASE WHEN wt.warehouse_usd_list > 0
            THEN ROUND(a.est_cost_usd_list / wt.warehouse_usd_list, 3) END AS share_of_warehouse_cost,
       a.total_exec_ms,
       a.avg_exec_ms,
       a.max_exec_ms,
       a.p50_duration_ms,
       a.p95_duration_ms,
       a.read_bytes,
       a.spilled_local_bytes,
       a.read_files,
       a.pruned_files,
       a.from_result_cache_share,
       sp.sample_statement_id,
       a.first_seen,
       a.last_seen,
       CASE
         WHEN a.est_cost_usd_list >= 200
           OR (wt.warehouse_usd_list > 0 AND a.est_cost_usd_list / wt.warehouse_usd_list >= 0.4)
           THEN 'CRITICAL'
         WHEN a.est_cost_usd_list >= 50
           OR (wt.warehouse_usd_list > 0 AND a.est_cost_usd_list / wt.warehouse_usd_list >= 0.2)
           THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM agg a
JOIN top_user_pick tu
  ON  tu.workspace_id = a.workspace_id AND tu.warehouse_id = a.warehouse_id
  AND tu.group_kind = a.group_kind AND tu.group_id IS NOT DISTINCT FROM a.group_id
  AND tu.statement_type IS NOT DISTINCT FROM a.statement_type
JOIN sample_pick sp
  ON  sp.workspace_id = a.workspace_id AND sp.warehouse_id = a.warehouse_id
  AND sp.group_kind = a.group_kind AND sp.group_id IS NOT DISTINCT FROM a.group_id
  AND sp.statement_type IS NOT DISTINCT FROM a.statement_type
LEFT JOIN wh_total wt
  ON  wt.workspace_id = a.workspace_id AND wt.warehouse_id = a.warehouse_id
ORDER BY est_cost_usd_list DESC NULLS LAST, a.runs DESC, a.workspace_id, a.warehouse_id,
         a.group_kind, a.group_id
LIMIT 5000
) q
