-- query_id: cost_chargeback_by_identity
-- title: DBU usage by identity (chargeback)
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: identity_metadata fields are documented system.billing.usage columns.
-- read_this: One row = a day + workspace + product + masked identity's DBU usage. The columns that matter are identity_type (user vs service_principal vs unknown) and identity_run_as (the masked principal) - use this to see who is spending, and how much of the spend has no attributable identity at all.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_chargeback_by_tag (for a tag-based cut of the same window), cost_workspace_names (to resolve workspace_id to a human name)
-- caveats: identity_metadata is sparse/conditional and is replaced with '__REDACTED__' in FedRamp workspaces - treat '__REDACTED__' and NULL as "identity unavailable," not a real principal. owned_by populates for SQL-warehouse usage only. identity_type is 'user' when run_as is an email, else 'service_principal' (SP app-id/name). run_as/owned_by/created_by are masked in-SQL to DEC-66.3's identity format (a GUID kept as-is; otherwise the first 8 hex chars of sha2(lower(trim(x)), 256) + a space + the first 2 chars of the raw identity + '***') - drop that masking only if you are building an internal, access-controlled user leaderboard. Restricted to usage_unit = 'DBU', so net_usage_quantity is comparable across billing_origin_product rows.
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       CASE
         WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
         WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
         ELSE 'service_principal'
       END                          AS identity_type,
       CASE
         WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN identity_metadata.run_as
         WHEN identity_metadata.run_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN identity_metadata.run_as
         ELSE concat(substr(sha2(lower(trim(identity_metadata.run_as)), 256), 1, 8), ' ', substr(identity_metadata.run_as, 1, 2), '***')
       END                          AS identity_run_as,
       CASE
         WHEN identity_metadata.owned_by IS NULL OR identity_metadata.owned_by = '__REDACTED__' THEN identity_metadata.owned_by
         WHEN identity_metadata.owned_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN identity_metadata.owned_by
         ELSE concat(substr(sha2(lower(trim(identity_metadata.owned_by)), 256), 1, 8), ' ', substr(identity_metadata.owned_by, 1, 2), '***')
       END                          AS identity_owned_by,
       CASE
         WHEN identity_metadata.created_by IS NULL OR identity_metadata.created_by = '__REDACTED__' THEN identity_metadata.created_by
         WHEN identity_metadata.created_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN identity_metadata.created_by
         ELSE concat(substr(sha2(lower(trim(identity_metadata.created_by)), 256), 1, 8), ' ', substr(identity_metadata.created_by, 1, 2), '***')
       END                          AS identity_created_by,
       SUM(usage_quantity) AS net_usage_quantity
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         CASE
           WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
           WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
           ELSE 'service_principal'
         END,
         identity_metadata.run_as, identity_metadata.owned_by, identity_metadata.created_by
ORDER BY usage_date DESC, workspace_id, identity_type
