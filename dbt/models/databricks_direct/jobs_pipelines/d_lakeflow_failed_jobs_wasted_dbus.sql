{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:deep', 'stars', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_failed_jobs_wasted_dbus.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
),
usage_rows AS (
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         u.usage_quantity            AS dbus,
         u.usage_quantity * p.list_rate AS usd,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND u.usage_date < {{ audit_today() }}
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
),
job_usage AS (
  SELECT workspace_id, job_id,
         SUM(dbus) AS net_dbus, SUM(usd) AS usd_priced_part,
         SUM(unpriced_q) AS unpriced_q, SUM(priced_q) AS priced_q,
         SUM(CASE WHEN job_run_id IS NULL THEN dbus ELSE 0 END)       AS unattributed_dbus,
         SUM(CASE WHEN job_run_id IS NULL THEN usd END)               AS unattributed_usd,
         SUM(CASE WHEN job_run_id IS NULL THEN unpriced_q ELSE 0 END) AS unattributed_unpriced_q,
         SUM(CASE WHEN job_run_id IS NOT NULL THEN 1 ELSE 0 END)      AS run_rows
  FROM usage_rows
  GROUP BY workspace_id, job_id
),
run_usage AS (
  SELECT workspace_id, job_id, job_run_id,
         SUM(dbus) AS run_dbus, SUM(usd) AS run_usd, SUM(unpriced_q) AS run_unpriced_q
  FROM usage_rows
  WHERE job_run_id IS NOT NULL
  GROUP BY workspace_id, job_id, job_run_id
),
cand AS (
  SELECT DISTINCT workspace_id, job_id, run_id
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
    AND result_state IS NOT NULL
),
run_rows AS (
  SELECT t.workspace_id, t.job_id, t.run_id, t.period_start_time, t.period_end_time,
         t.result_state, t.termination_code,
         unix_timestamp(t.period_end_time) - unix_timestamp(t.period_start_time) AS slice_s
  FROM {{ source('system_lakeflow', 'job_run_timeline') }} t
  JOIN cand c ON c.workspace_id = t.workspace_id AND c.job_id = t.job_id AND c.run_id = t.run_id
),
slices AS (
  SELECT r.*,
         1 + COALESCE(SUM(CASE WHEN r.result_state IS NOT NULL THEN 1 ELSE 0 END) OVER (
               PARTITION BY r.workspace_id, r.job_id, r.run_id
               ORDER BY r.period_end_time, r.period_start_time
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS attempt_no
  FROM run_rows r
),
attempts AS (
  SELECT workspace_id, job_id, run_id, attempt_no,
         SUM(slice_s)          AS attempt_s,
         MAX(result_state)     AS attempt_state,   -- one end row per attempt, or none (in flight)
         MAX(termination_code) AS attempt_code,
         MAX(period_end_time)  AS attempt_end
  FROM slices
  GROUP BY workspace_id, job_id, run_id, attempt_no
),
att2 AS (
  SELECT a.*, MAX(a.attempt_no) OVER (PARTITION BY a.workspace_id, a.job_id, a.run_id) AS last_attempt
  FROM attempts a
),
runs AS (
  SELECT workspace_id, job_id, run_id,
         MAX(CASE WHEN attempt_no = last_attempt THEN attempt_state END) AS final_state,
         MAX(CASE WHEN attempt_no = last_attempt THEN attempt_end END)   AS final_end,
         MAX(CASE WHEN attempt_no = last_attempt AND attempt_state IS NULL THEN 1 ELSE 0 END) AS in_flight,
         SUM(attempt_s) AS run_s,
         SUM(CASE WHEN attempt_no < last_attempt AND attempt_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
                  THEN attempt_s ELSE 0 END) AS failed_attempt_s
  FROM att2
  GROUP BY workspace_id, job_id, run_id
),
classed AS (
  SELECT r.*, ru.run_dbus, ru.run_usd, ru.run_unpriced_q,
         CASE WHEN r.final_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 'failed'
              WHEN r.final_state = 'CANCELLED' THEN 'cancelled'
              WHEN r.failed_attempt_s > 0 THEN 'repaired'
              ELSE 'ok' END AS run_class
  FROM runs r
  LEFT JOIN run_usage ru
    ON ru.workspace_id = r.workspace_id AND ru.job_id = r.job_id AND ru.job_run_id = r.run_id
  WHERE r.in_flight = 0
    AND r.final_end < date_trunc('DAY', {{ audit_now() }})
),
last_fail AS (
  -- the most recent failed attempt (a final failure or a repaired one) of these runs
  SELECT a.workspace_id, a.job_id, a.run_id AS last_failed_run_id,
         a.attempt_code AS last_failed_termination_code
  FROM att2 a
  JOIN classed c ON c.workspace_id = a.workspace_id AND c.job_id = a.job_id AND c.run_id = a.run_id
  WHERE a.attempt_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.workspace_id, a.job_id
                             ORDER BY a.attempt_end DESC, a.run_id DESC) = 1
),
per_job AS (
  SELECT workspace_id, job_id,
         COUNT(*) AS distinct_runs,
         SUM(CASE WHEN run_class = 'failed'    THEN 1 ELSE 0 END) AS failed_runs,
         SUM(CASE WHEN run_class = 'repaired'  THEN 1 ELSE 0 END) AS repaired_runs,
         SUM(CASE WHEN run_class = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_runs,
         SUM(CASE WHEN run_class = 'failed' AND run_dbus IS NULL THEN 1 ELSE 0 END) AS failed_runs_unbilled,
         SUM(CASE WHEN run_class = 'failed' THEN run_dbus
                  WHEN run_class = 'repaired' AND run_s > 0 THEN run_dbus * failed_attempt_s / run_s END) AS wasted_dbus_raw,
         SUM(CASE WHEN run_class = 'failed' THEN run_usd END) AS failed_usd,
         SUM(CASE WHEN run_class = 'repaired' AND run_s > 0 THEN run_usd * failed_attempt_s / run_s END) AS repair_usd,
         SUM(CASE WHEN run_class = 'cancelled' THEN run_usd END) AS cancelled_usd,
         SUM(CASE WHEN run_class IN ('failed', 'repaired') THEN COALESCE(run_unpriced_q, 0) ELSE 0 END) AS waste_unpriced_q
  FROM classed
  GROUP BY workspace_id, job_id
),
judged AS (
  SELECT p.*, j.net_dbus, j.usd_priced_part, j.unpriced_q, j.priced_q, j.unattributed_dbus,
         j.unattributed_usd, j.unattributed_unpriced_q,
         lf.last_failed_run_id, lf.last_failed_termination_code,
         (j.workspace_id IS NOT NULL) AS has_usage,
         p.failed_runs * 100.0 / p.distinct_runs AS rate_raw,
         COALESCE(p.failed_usd, 0) + COALESCE(p.repair_usd, 0) AS waste_raw,
         CASE WHEN j.workspace_id IS NULL THEN 'no_billing_rows'
              WHEN j.run_rows = 0          THEN 'no_run_id_in_billing'
              WHEN p.waste_unpriced_q > 0  THEN 'unpriced'
         END AS na_reason
  FROM per_job p
  LEFT JOIN job_usage j  ON j.workspace_id = p.workspace_id  AND j.job_id = p.job_id
  LEFT JOIN last_fail lf ON lf.workspace_id = p.workspace_id AND lf.job_id = p.job_id
  WHERE p.failed_runs > 0 OR p.repaired_runs > 0
)
SELECT workspace_id, job_id,
       distinct_runs, failed_runs, repaired_runs, cancelled_runs, failed_runs_unbilled,
       ROUND(rate_raw, 1)                                                     AS failure_rate_pct,
       last_failed_termination_code, last_failed_run_id,
       CASE WHEN na_reason IN ('no_billing_rows', 'no_run_id_in_billing') THEN NULL
            ELSE ROUND(COALESCE(wasted_dbus_raw, 0), 2) END                   AS wasted_dbus,
       CASE WHEN na_reason IS NULL THEN ROUND(COALESCE(failed_usd, 0), 2) END    AS est_failed_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(COALESCE(repair_usd, 0), 2) END    AS est_repair_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(waste_raw, 2) END                   AS est_wasted_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(COALESCE(cancelled_usd, 0), 2) END AS est_cancelled_usd_list,
       ROUND(net_dbus, 2)                                                     AS net_dbus,
       CASE WHEN NOT has_usage OR unpriced_q > 0 THEN NULL
            ELSE ROUND(COALESCE(usd_priced_part, 0), 2) END                   AS est_usd_list,
       CASE WHEN NOT has_usage THEN NULL
            WHEN unattributed_dbus = 0 THEN 0
            WHEN unattributed_unpriced_q > 0 THEN NULL
            ELSE ROUND(unattributed_usd, 2) END                               AS est_unattributed_usd_list,
       CASE WHEN na_reason IS NULL AND NOT (unpriced_q > 0)
            THEN ROUND(waste_raw * 100.0 / NULLIF(usd_priced_part, 0), 1) END AS wasted_share_pct,
       CASE WHEN NOT has_usage THEN NULL
            WHEN unpriced_q > 0 THEN 'unpriced' WHEN priced_q = 0 THEN 'free' ELSE 'priced' END AS price_basis,
       CASE
         WHEN na_reason = 'no_billing_rows' THEN
           'failed runs or failed attempts found, but no billed usage for this job in the window - the runs may have failed before compute started, or billing has not landed yet'
         WHEN na_reason = 'no_run_id_in_billing' THEN
           'failed runs or failed attempts found, but this job''s billing rows carry no run id, so what they cost cannot be measured (the job''s whole spend is in est_usd_list, not spread over its runs)'
         WHEN na_reason = 'unpriced' THEN
           'the failed runs'' usage has no list price, so there is no dollar figure (wasted_dbus is shown)'
         ELSE
           CONCAT_WS('; ',
             CONCAT(failed_runs, ' of ', distinct_runs, ' runs failed (', CAST(ROUND(rate_raw, 0) AS BIGINT), '%)'),
             CASE WHEN repaired_runs = 1 THEN '1 run failed first and was repaired'
                  WHEN repaired_runs > 1 THEN CONCAT(repaired_runs, ' runs failed first and were repaired') END,
             CASE WHEN failed_runs_unbilled = 1 THEN '1 failed run had no billed usage'
                  WHEN failed_runs_unbilled > 1 THEN CONCAT(failed_runs_unbilled, ' failed runs had no billed usage') END,
             CASE WHEN last_failed_termination_code IS NOT NULL
                  THEN CONCAT('last failure ', last_failed_termination_code) END)
       END                                                                    AS waste_reason,
       na_reason                                                              AS not_assessed_reason,
       -- the $ floor ({{ param('lakeflow_failed_jobs_wasted_dbus', 'min_waste_usd', 1) }}) gates only the $ branches, never the failure-rate ones -- a
       -- job failing outright must not read OK just because it is cheap. The rate branches
       -- themselves need {{ param('lakeflow_failed_jobs_wasted_dbus', 'min_runs', 3) }} runs first -- 1 of 1 failed is not a rate, just one data point.
       CASE
         WHEN na_reason IS NOT NULL                                                        THEN 'NOT_ASSESSED'
         WHEN (distinct_runs >= {{ param('lakeflow_failed_jobs_wasted_dbus', 'min_runs', 3) }} AND rate_raw >= {{ param('lakeflow_failed_jobs_wasted_dbus', 'crit_failure_pct', 50) }})
           OR waste_raw >= GREATEST({{ param('lakeflow_failed_jobs_wasted_dbus', 'crit_waste_usd', 200) }}, {{ param('lakeflow_failed_jobs_wasted_dbus', 'min_waste_usd', 1) }})                       THEN 'CRITICAL'
         WHEN (distinct_runs >= {{ param('lakeflow_failed_jobs_wasted_dbus', 'min_runs', 3) }} AND rate_raw >= {{ param('lakeflow_failed_jobs_wasted_dbus', 'warn_failure_pct', 10) }})
           OR waste_raw >= GREATEST({{ param('lakeflow_failed_jobs_wasted_dbus', 'warn_waste_usd', 20) }}, {{ param('lakeflow_failed_jobs_wasted_dbus', 'min_waste_usd', 1) }})                       THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM judged
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_wasted_usd_list DESC NULLS LAST, failed_runs DESC, workspace_id, job_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
