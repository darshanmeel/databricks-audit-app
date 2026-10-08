-- query_id: access_delta_sharing_exposure
-- title: Delta Sharing external exposure - recipients and shared objects per share
-- domain: governance_access   tier: standard
-- reads: system.information_schema.shares, system.information_schema.share_recipient_privileges, system.information_schema.table_share_usage, system.information_schema.schema_share_usage
-- requires: SELECT on system.information_schema; Unity Catalog required; Delta Sharing (provider side) in use
-- empty_if: privilege_scoped, no_activity
-- params: :warn_share_recipients (default 1) recipients on a share at/above which it flags WARN (any external recipient is worth a review); :crit_share_recipients (default 10) recipients at/above which it flags CRITICAL; :crit_shared_tables (default 100) shared tables at/above which a share flags CRITICAL regardless of recipient count; :top_n (default 200) row cap
-- confidence: confirmed
-- confidence_note: shares, share_recipient_privileges, schema_share_usage, and table_share_usage columns are transcribed verbatim from the information_schema reference.
-- read_this: One row = one OUTBOUND Delta Share, with how many recipients it is granted to and how many schemas/tables it exposes. The columns that matter are recipient_count and shared_table_count - together they size external data exposure. A share with recipients IS live external exposure.
-- healthy: recipient_count = 0 - a share defined but not yet granted to any recipient (field heuristic).
-- investigate_if: recipient_count at/above :warn_share_recipients (WARN - external exposure exists, confirm it is intended); recipient_count at/above :crit_share_recipients or shared_table_count at/above :crit_shared_tables (CRITICAL - broad external exposure) - field heuristic.
-- actions: 1) confirm each recipient is an intended partner and revoke any that are not (free); 2) scope shares to the minimum tables/schemas needed and prefer schema shares with row/column controls (config); 3) rotate recipient tokens and set recipient IP allowlists / expirations (config).
-- next: access_grants_inventory_extended (the internal grant side of the same objects), access_tags_inventory (whether the shared tables carry sensitivity tags)
-- caveats: PROVIDER side only - this is what YOU share OUT (outbound), not data shared TO you. system.information_schema is privilege-aware, so a principal that cannot see a share or its recipients undercounts exposure - label "partial - privilege-aware", never a complete share graph. recipient_count is DISTINCT recipients across the share's privilege rows; recipients are partial-masked. shared_table_count / shared_schema_count come from table_share_usage / schema_share_usage (what is actually exposed) and can lag share edits. recipient_count = 0 means defined-but-not-yet-granted-out - not necessarily safe, just not yet live. Token / expiration / IP-allowlist details are not in these views (Delta Sharing API only).
WITH recips AS (
  SELECT SHARE_NAME,
         COUNT(DISTINCT RECIPIENT_NAME) AS recipient_count,
         array_join(collect_set(
           CASE WHEN RECIPIENT_NAME IS NULL OR RECIPIENT_NAME = '__REDACTED__' THEN RECIPIENT_NAME
                WHEN RECIPIENT_NAME RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN RECIPIENT_NAME
                ELSE concat(substr(sha2(lower(trim(RECIPIENT_NAME)), 256), 1, 8), ' ', substr(RECIPIENT_NAME, 1, 2), '***') END), ', ') AS recipients
  FROM system.information_schema.share_recipient_privileges
  GROUP BY SHARE_NAME
),
tbls AS (
  SELECT SHARE_NAME, COUNT(*) AS shared_table_count
  FROM system.information_schema.table_share_usage GROUP BY SHARE_NAME
),
schs AS (
  SELECT SHARE_NAME, COUNT(*) AS shared_schema_count
  FROM system.information_schema.schema_share_usage GROUP BY SHARE_NAME
)
SELECT
  s.SHARE_NAME                                              AS share_name,
  CASE WHEN s.SHARE_OWNER IS NULL OR s.SHARE_OWNER = '__REDACTED__' THEN s.SHARE_OWNER
       WHEN s.SHARE_OWNER RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN s.SHARE_OWNER
       ELSE concat(substr(sha2(lower(trim(s.SHARE_OWNER)), 256), 1, 8), ' ', substr(s.SHARE_OWNER, 1, 2), '***') END AS share_owner,
  COALESCE(r.recipient_count, 0)                            AS recipient_count,
  r.recipients                                             AS recipients,
  COALESCE(t.shared_table_count, 0)                        AS shared_table_count,
  COALESCE(sc.shared_schema_count, 0)                      AS shared_schema_count,
  s.CREATED                                                AS created,
  CASE
    WHEN COALESCE(r.recipient_count, 0) >= :crit_share_recipients
      OR COALESCE(t.shared_table_count, 0) >= :crit_shared_tables       THEN 'CRITICAL'
    WHEN COALESCE(r.recipient_count, 0) >= :warn_share_recipients       THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM system.information_schema.shares s
LEFT JOIN recips r  ON r.SHARE_NAME  = s.SHARE_NAME
LEFT JOIN tbls   t  ON t.SHARE_NAME  = s.SHARE_NAME
LEFT JOIN schs   sc ON sc.SHARE_NAME = s.SHARE_NAME
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  recipient_count DESC, shared_table_count DESC
LIMIT :top_n
