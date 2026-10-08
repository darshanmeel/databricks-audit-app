-- query_id: query_top_by_cost
-- title: Top queries by cost
-- domain: performance   tier: standard
-- reads: system.query.history, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.query and system.billing; GA. SCOPE: SQL warehouses only - a
--   statement with no compute.warehouse_id (serverless, or a classic Lakeflow pipeline) cannot be
--   priced from warehouse billing and is excluded
-- empty_if: schema_not_enabled, compute_scope_gap, privilege_scoped, no_activity
-- params: :period_days (default 30) rolling window in days; :warn_cost_usd (default 50) dollars a
--   group must reach in the window to flag WARN; :crit_cost_usd (default 200) the same for
--   CRITICAL; :warn_share_frac (default 0.2) the group's share of its warehouse's own window cost,
--   0-1, that flags WARN at any dollar amount; :crit_share_frac (default 0.4) the same for
--   CRITICAL; :top_n (default 5000) row cap - keep only the top groups by cost
-- confidence: needs_confirmation
-- confidence_note: The billing join (system.billing.usage x system.billing.list_prices at the
--   effective list rate) is the same shape compute_warehouse_idle_minutes and
--   query_per_query_estimate_lane already read on a live workspace; the query_source struct paths
--   are query_provenance_by_source's own (itself needs_confirmation there); the de-valued-text
--   sha2 hash is query_costly_statements_grouped's. Splitting each billed hour's dollars across
--   the statements that overlap it by total_task_duration_ms (a statement spanning several hours
--   gets a share of each), then rolling that up by text hash or origin, is new and has NOT run on
--   a live workspace. Confirm on your account: 1) for one warehouse and window, the SUM of every
--   group's est_cost_usd_list is at or below that warehouse's own est_usd_list in
--   compute_warehouse_idle_minutes for the same window; 2) a dashboard or job group_id/source_id
--   matches the real dashboard/job id shown in the workspace UI; 3) the "ad-hoc, text hidden"
--   group appears only when statement_text reads '<REDACTED>' for that account (no PII-access role
--   on the snapshot principal).
-- read_this: One row is one group of SQL-warehouse statements that cost about the same thing.
--   group_kind says how they were grouped: the same statement text by hash when it is readable,
--   otherwise everything from the same saved query, dashboard, Genie space, alert, job, notebook
--   or pipeline (a job group is keyed by job_id alone, every task folded in), or - failing that -
--   one ad-hoc bucket, text hidden, per warehouse and user. source_kind/source_id name the group's
--   real origin (dashboard, Genie space, alert, job, notebook, saved query or pipeline id from
--   query_source) even when group_kind is "text hash" - NULL only when the group is genuinely
--   ad-hoc with no captured source. runs and top_user say who ran it and how often;
--   est_cost_usd_list is this group's share of its warehouse's billed dollars in the window, each
--   statement's share split across every billed hour it overlaps (not just its start hour) by
--   total_task_duration_ms (falling back to execution_duration_ms where the first is not
--   captured); share_of_warehouse_cost is the same figure as a fraction of the warehouse's own
--   total. sample_statement_id is the group's latest statement; p50_duration_ms/
--   p95_duration_ms are that group's execution-time spread. from_result_cache_share close to 1
--   means most of the group's runs were already free (served from the result cache). read_files
--   and pruned_files are the files its runs read and the files they skipped by pruning;
--   read_files / (read_files + pruned_files) is the share of candidate files it had to read.
-- healthy: status = OK - below :warn_cost_usd and below :warn_share_frac of its warehouse's window
--   cost (field heuristic).
-- investigate_if: status = WARN or CRITICAL - the group's est_cost_usd_list is at/above
--   :warn_cost_usd (WARN) or :crit_cost_usd (CRITICAL), or its share_of_warehouse_cost is at/above
--   :warn_share_frac (WARN) or :crit_share_frac (CRITICAL) of its warehouse's own window cost,
--   whichever band is worse.
-- actions: 1) open the source by name first - the dashboard, job, notebook or saved query
--   group_id - and check whether it needs to run this often (free); 2) when group_kind is
--   "text hash" and from_result_cache_share is low, look for a cache or materialization it could
--   reuse, and whether it is pruning the files it reads (config); 3) schedule it less often, or
--   move it off a shared warehouse onto one sized for it (spend).
-- next: query_costly_statements (the individual runs behind a hot text hash),
--   query_provenance_by_source (the fuller source breakdown this group_kind is drawn from),
--   query_per_query_estimate_lane (the row-level duration and bytes this cost split is built
--   from), compute_warehouse_idle_minutes (the warehouse's own total spend for the same window)
-- caveats: DOLLARS ARE AN ESTIMATE - system tables carry no per-query dollar column. Each SQL
--   warehouse's billed usage is priced by the hour at the effective list rate (DEC-66.1, the same
--   join compute_warehouse_idle_minutes uses). A statement that spans more than one billed hour
--   has its weight (total_task_duration_ms, or execution_duration_ms where the first is not
--   captured) SPLIT ACROSS EVERY HOUR IT OVERLAPS, in proportion to how many of that hour's
--   milliseconds the statement's own [start_time, end_time) covers (end_time falls back to
--   update_time, then start_time) - a statement that starts and ends in the same billed hour gets
--   that hour's full weight outright; a 3-hour query is priced in every one of those hours, not
--   billed whole to its start hour while the rest go unpriced. Each hour's dollars are then split across every
--   statement's share of that hour's total weight; an hour with billed usage but no statement, or
--   a statement-hour with no billed usage, contributes no dollars either way, so the sum of every
--   group's est_cost_usd_list for a warehouse can sit below that warehouse's own total spend.
--   SCOPE: only statements with compute.warehouse_id set are priced here - serverless SQL and
--   classic Lakeflow pipeline statements carry no warehouse_id and are excluded; their cost has to
--   be attributed a different way, from job or pipeline usage. GROUPING: statement_text reads
--   '<REDACTED>' for every row unless the snapshot principal is an account admin or a member of
--   the databricks_pii_access group; on such an account every group falls back to its query_source
--   origin, or, absent one, one "ad-hoc, text hidden" bucket per warehouse and user (executed_by,
--   masked the same way as query_provenance_by_source). Text hashing de-values the same way as
--   query_costly_statements_grouped (emails and single-quoted literals stripped before hashing),
--   so two statements that differ only in a literal collapse into one group; a job group is keyed
--   by job_id ALONE - every task run of that job folds into one group, even across separate job
--   runs. Origin precedence when more than one query_source field is populated at once
--   (undocumented by Databricks) is: saved query, dashboard, legacy dashboard, Genie space, alert,
--   job, notebook, pipeline - the same order query_provenance_by_source uses. SOURCE_KIND/
--   SOURCE_ID follow that same query_source precedence, independent of group_kind - a "text hash"
--   group still names its real dashboard/job/notebook/etc. origin here when one is captured, NULL
--   only for a genuinely ad-hoc statement with no query_source at all; PRIVACY: sample_statement_id
--   and a saved-query/dashboard/Genie/alert/job/notebook/pipeline group_id or source_id name a
--   Databricks entity, never a person; the only person-identifying fields here are top_user and an
--   ad-hoc group's group_id, masked to DEC-66.3's format when masking is turned on. distinct_users
--   counts a group's runs with no captured identity at all (executed_by NULL) as one user, not
--   zero. sample_statement_id is the group's latest run, so it still opens in Databricks' query
--   history; p50_duration_ms/p95_duration_ms are percentiles of execution_duration_ms
--   across the group's own runs. status is judged on est_cost_usd_list and share_of_warehouse_cost
--   only; a group on an unpriced or unbilled warehouse gets a NULL dollar figure, a NULL share,
--   and reads OK. Rows are capped to the top :top_n by est_cost_usd_list.
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
bill_hour AS (
  -- one row per SQL warehouse per billed hour: its list-price dollars in that hour
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         date_trunc('HOUR', u.usage_start_time) AS usage_hour,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS hour_usd
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL :period_days DAYS
    AND u.usage_date < current_date()
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
  FROM system.query.history q
  WHERE q.start_time >= current_date() - INTERVAL :period_days DAYS
    AND q.start_time < current_date()
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
             CASE WHEN h.executed_by IS NULL OR h.executed_by = '__REDACTED__' THEN h.executed_by
                  WHEN h.executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN h.executed_by
                  ELSE concat(substr(sha2(lower(trim(h.executed_by)), 256), 1, 8), ' ', substr(h.executed_by, 1, 2), '***')
             END
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
         CASE WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
              WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
              ELSE concat(substr(sha2(lower(trim(executed_by)), 256), 1, 8), ' ', substr(executed_by, 1, 2), '***')
         END AS top_user
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
         WHEN a.est_cost_usd_list >= :crit_cost_usd
           OR (wt.warehouse_usd_list > 0 AND a.est_cost_usd_list / wt.warehouse_usd_list >= :crit_share_frac)
           THEN 'CRITICAL'
         WHEN a.est_cost_usd_list >= :warn_cost_usd
           OR (wt.warehouse_usd_list > 0 AND a.est_cost_usd_list / wt.warehouse_usd_list >= :warn_share_frac)
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
LIMIT :top_n
