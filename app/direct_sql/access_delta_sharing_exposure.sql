-- generated from dbt/models/databricks_direct/governance_access/d_access_delta_sharing_exposure.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_delta_sharing_exposure.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH recips AS (
  SELECT SHARE_NAME,
         COUNT(DISTINCT RECIPIENT_NAME) AS recipient_count,
         array_join(collect_set(
           RECIPIENT_NAME), ', ') AS recipients
  FROM `system`.`information_schema`.`share_recipient_privileges`
  GROUP BY SHARE_NAME
),
tbls AS (
  SELECT SHARE_NAME, COUNT(*) AS shared_table_count
  FROM `system`.`information_schema`.`table_share_usage` GROUP BY SHARE_NAME
),
schs AS (
  SELECT SHARE_NAME, COUNT(*) AS shared_schema_count
  FROM `system`.`information_schema`.`schema_share_usage` GROUP BY SHARE_NAME
)
SELECT
  s.SHARE_NAME                                              AS share_name,
  s.SHARE_OWNER AS share_owner,
  COALESCE(r.recipient_count, 0)                            AS recipient_count,
  r.recipients                                             AS recipients,
  COALESCE(t.shared_table_count, 0)                        AS shared_table_count,
  COALESCE(sc.shared_schema_count, 0)                      AS shared_schema_count,
  s.CREATED                                                AS created,
  CASE
    WHEN COALESCE(r.recipient_count, 0) >= 10
      OR COALESCE(t.shared_table_count, 0) >= 100       THEN 'CRITICAL'
    WHEN COALESCE(r.recipient_count, 0) >= 1       THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM `system`.`information_schema`.`shares` s
LEFT JOIN recips r  ON r.SHARE_NAME  = s.SHARE_NAME
LEFT JOIN tbls   t  ON t.SHARE_NAME  = s.SHARE_NAME
LEFT JOIN schs   sc ON sc.SHARE_NAME = s.SHARE_NAME
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  recipient_count DESC, shared_table_count DESC
LIMIT 200
) q
