-- generated from dbt/models/databricks_direct/serving_ai/d_compute_serving_dormant_endpoints.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/serving_ai/compute_serving_dormant_endpoints.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH entities AS (
  -- served_entities is change-history; collapse to the latest config row per entity.
  SELECT
    workspace_id,
    endpoint_id,
    endpoint_name,
    served_entity_id,
    served_entity_name,
    entity_type,
    entity_name,
    entity_version,
    latest_change_time,
    endpoint_delete_time
  FROM (
    SELECT
      se.workspace_id,
      se.endpoint_id,
      se.endpoint_name,
      se.served_entity_id,
      se.served_entity_name,
      se.entity_type,
      se.entity_name,
      se.entity_version,
      MAX(se.change_time) OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
      ) AS latest_change_time,
      MAX(se.endpoint_delete_time) OVER (
        PARTITION BY se.workspace_id, se.endpoint_id
      ) AS endpoint_delete_time,
      ROW_NUMBER() OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
        ORDER BY se.change_time DESC
      ) AS _rn
    FROM `system`.`serving`.`served_entities` se
  )
  WHERE _rn = 1
),
usage_rollup AS (
  -- Per-entity traffic inside the window. Entities with no traffic never appear here,
  -- so the LEFT JOIN below leaves their total_requests NULL (the dormant signal).
  SELECT
    eu.workspace_id,
    eu.served_entity_id,
    COUNT(*) AS total_requests,
    MAX(CAST(eu.request_time AS DATE)) AS last_request_date
  FROM `system`.`serving`.`endpoint_usage` eu
  WHERE eu.request_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND eu.request_time < __AS_OF_DATE__
  GROUP BY eu.workspace_id, eu.served_entity_id
),
per_endpoint AS (
  -- one row per endpoint: its served entities (model versions) are summed, not listed
  SELECT
    ent.workspace_id,
    ent.endpoint_id,
    MAX(ent.endpoint_name) AS endpoint_name,
    COUNT(*) AS served_entity_count,
    MAX(ent.latest_change_time) AS latest_change_time,
    SUM(COALESCE(ur.total_requests, 0)) AS total_requests,
    MAX(ur.last_request_date) AS last_request_date,
    MAX(ent.endpoint_delete_time) AS endpoint_delete_time,
    MIN(CASE WHEN ent.entity_type = 'FOUNDATION_MODEL' THEN 1 ELSE 0 END) AS all_foundation
  FROM entities ent
  LEFT JOIN usage_rollup ur
    ON ent.workspace_id = ur.workspace_id
    AND ent.served_entity_id = ur.served_entity_id
  GROUP BY ent.workspace_id, ent.endpoint_id
)
SELECT
  pe.workspace_id AS workspace_id,
  pe.endpoint_id AS endpoint_id,
  pe.endpoint_name AS endpoint_name,
  pe.served_entity_count AS served_entity_count,
  pe.latest_change_time AS latest_change_time,
  CAST(pe.latest_change_time AS DATE) AS latest_change_date,
  pe.total_requests AS total_requests,
  pe.last_request_date AS last_request_date,
  CASE
    WHEN pe.latest_change_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__) THEN 'NOT_ASSESSED'
    WHEN pe.total_requests = 0 THEN 'CRITICAL'
    WHEN pe.total_requests < 10 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM per_endpoint pe
WHERE pe.endpoint_delete_time IS NULL
  AND NOT (pe.endpoint_name LIKE 'databricks-%' AND pe.all_foundation = 1)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  total_requests ASC
) q
