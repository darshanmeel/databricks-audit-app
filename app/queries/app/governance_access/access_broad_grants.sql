-- query_id: access_broad_grants
-- title: Broad grants: ALL PRIVILEGES, everyone-in-the-account, near-ownership to a group, or
--   write access at catalog level
-- domain: governance_access   tier: standard
-- reads: system.information_schema.table_privileges, system.information_schema.catalog_privileges, system.information_schema.schema_privileges
-- requires: SELECT on system.information_schema; Unity Catalog required
-- empty_if: privilege_scoped
-- params: :top_n (default 100000) row cap - far above any account's real grant count, because
--   this is already a findings list (broad grants only) and a cut row would read as "not flagged"
-- confidence: needs_confirmation
-- confidence_note: table_privileges and catalog_privileges are the two privilege views
--   access_grants_inventory already transcribed column-by-column - reused verbatim here, not
--   re-verified. What IS new and unconfirmed: the built-in all-users principal name(s)
--   ('account users', 'all account users', 'users' - matched case-insensitively, all three kept
--   because different Databricks releases/docs use different spellings and this has not been
--   checked against a live account); the group-vs-user grantee heuristic (no '@' and not a
--   36-char GUID -> group); and the literal spelling of the ALL-PRIVILEGES privilege_type (both
--   'ALL PRIVILEGES', Databricks' own GRANT keyword phrase, and 'ALL_PRIVILEGES', the underscored
--   spelling already seen in this repo's own fixture data, are matched - confirm which your
--   account actually emits). Confirm all of the above with a real SHOW GRANTS / DESCRIBE before
--   trusting grantee_type or the ALL-PRIVILEGES band on a live account.
-- read_this: One row = one (securable, grantee, privilege) grant that is BROAD - this is a
--   filtered findings list, not the full grant rollup (that is access_grants_inventory). The
--   columns that matter are securable (the full catalog/schema/table name), grantee (masked per
--   DEC-66.3 for a person, shown in full for a group or the all-users principal - group names are
--   not personal data), reason (plain words for why this row is here) and status. Grants to the
--   groups listed under admin_groups in config/settings.yml read OK (applied when the app reads).
-- healthy: not a row here - a grant not matched by any broad rule (e.g. SELECT or USE_CATALOG to
--   a named user or group, or MODIFY/CREATE on one table alone to a named, non all-users
--   principal) is ordinary access and does not appear; see access_grants_inventory for the
--   complete, unfiltered rollup every grant (broad or not) belongs to.
-- investigate_if: CRITICAL - ALL PRIVILEGES granted straight to a person (not through a group),
--   or a write privilege (MODIFY, any CREATE_* privilege, or ALL PRIVILEGES) granted to every
--   user in the account (any scope - a write grant to everyone is CRITICAL whether it sits on one
--   table or a whole catalog): read reason and securable first, this is the highest-leverage row
--   on the list. WARN - any other broad grant: ALL PRIVILEGES to a group or service principal
--   (usually an admin group - list it under admin_groups once confirmed), a non-write privilege
--   granted to every user in the account, MANAGE (near-ownership) granted to a group, or a write
--   privilege granted to a named (non all-users) principal at catalog or schema scope.
-- actions: 1) open the securable in Databricks (Catalog Explorer -> Permissions) and confirm who
--   actually needs this access today; revoke or narrow the grant to the smallest scope and
--   privilege that does the job (free); 2) for a MANAGE grant to a group, confirm the group's real
--   membership is "people who should be able to grant/revoke access here", not a broader group
--   reused from somewhere else (free/config); 3) for a grant to every user in the account, replace
--   it with grants to the specific named groups that need it (config); 4) if the access is
--   genuinely meant to be this broad, record why on the object's own comment or tag so a future
--   audit does not re-flag it for nothing (free).
-- next: access_grants_inventory (the full rollup this filters down from - every grant, broad or
--   not), access_grants_inventory_extended (schema/connection/credential/external-location
--   grants, out of scope here - see caveats), access_runas_escalation (cross-check a
--   broadly-granted identity against run-as activity)
-- caveats: PRIVILEGE-AWARE, SAME AS access_grants_inventory - system.information_schema is
--   privilege-aware, so a principal with MANAGE sees only its own grants and only the objects it
--   can see; even run as a high-privilege audit principal this is "partial - privilege-aware;
--   incomplete vs SHOW GRANTS", never a complete grant graph. SCOPE - TABLE, CATALOG and SCHEMA
--   securables are judged (schema-level: CATALOG_NAME/SCHEMA_NAME from schema_privileges, used
--   here for the first time in this library - access_grants_inventory_extended's own caveat still
--   treats them as unconfirmed for its own rollup, but a wrong catalog/schema name here would only
--   misname a securable, never change which grants are judged broad); connection_privileges/
--   credential_privileges/external_location_privileges are still out of scope -
--   access_grants_inventory_extended gives a rough per-privilege COUNT for those (no object name)
--   if you need a signal today. A grant made at the schema level is judged on its own here; it is
--   NOT expanded down to every table the schema holds, so this still misses a broad EFFECTIVE
--   grant on a table that inherits from a schema-level grant the table_privileges view itself does
--   not surface as inherited_from (unlike a catalog-level inheritance, which this source does
--   track - see INHERITANCE below). INHERITANCE - a table or schema grant whose catalog (or, for a
--   table, whose schema) gives the same grantee the same privilege is dropped: it adds no access,
--   and keeping it turned one catalog grant into a row per table. Only the grant at the highest
--   scope is listed; a table row whose parent grant this principal cannot see is kept.
--   catalog_privileges carries NO inherited_from column
--   in this source at all (access_grants_inventory's own caveat, verbatim) - is_inherited and
--   inherited_from are therefore always NULL on every CATALOG-scope row, meaning "not tracked",
--   never "not inherited". WRITE PRIVILEGE - "write" here means MODIFY, any privilege_type
--   starting with CREATE (Databricks' catalog-level create privileges - CREATE_TABLE,
--   CREATE_SCHEMA, CREATE_VOLUME, CREATE_FUNCTION, CREATE_MODEL and others - all share this
--   prefix), or ALL PRIVILEGES. Two rules use it, at two different scopes: (1) a write privilege
--   granted to every user in the account is CRITICAL at ANY scope (one table or a whole catalog -
--   a write grant to everyone is the same real risk either way); (2) a write privilege granted to
--   a named (non all-users) principal is WARN, but ONLY when granted at CATALOG or SCHEMA scope -
--   a MODIFY or CREATE_* grant to a named user or group on ONE table alone is ordinary, expected
--   access and is never broad by itself here.
--   MANAGE / OWNERSHIP-LIKE - MANAGE is the real Unity Catalog privilege_type closest to
--   "ownership-like" (it lets its holder grant/revoke other privileges on the object); a literal
--   OWNERSHIP privilege_type is also matched defensively, though these information_schema views do
--   not expose true object OWNERSHIP as a granted privilege at all - ownership is a separate,
--   ungranted attribute (access_grants_inventory's own caveat: "privileges derived from object
--   OWNERSHIP... still never appear as rows in either branch"), so this query can flag a MANAGE
--   grant but can never see who actually owns an object. GRANTEE_TYPE HEURISTIC - no dedicated
--   principal-type column exists in either source table, so grantee_type is inferred: the three
--   literal built-in all-users spellings above -> ALL_USERS; a 36-char hex GUID -> SERVICE_
--   PRINCIPAL (already an id, shown unmasked, same passthrough rule as every other identity mask
--   in this library); a value containing '@' -> USER (masked per DEC-66.3: <8-hex-char hash of the
--   lowercased, trimmed value> + first 2 raw characters + '***' - none of these tables carries a
--   real per-row user id to prefer, unlike system.query.history); anything else -> GROUP, shown in
--   full (group names are not personal data). NULL / '__REDACTED__' grantees pass through
--   unmasked and unclassified (grantee_type UNKNOWN) rather than being dropped, matching every
--   other identity mask in this library; this fixture does not happen to exercise that branch (see
--   the test file's own note, matching test_governance_inventories.py's identical, already-
--   recorded gap for access_grants_inventory's own GRANTEE column). No dollars, no window -
--   current-state grant rollup, like access_grants_inventory.
WITH table_grants AS (
  SELECT
    'TABLE'                                                    AS securable_type,
    CONCAT(TABLE_CATALOG, '.', TABLE_SCHEMA, '.', TABLE_NAME)  AS securable,
    GRANTEE                                                    AS raw_grantee,
    PRIVILEGE_TYPE                                             AS privilege_type,
    (INHERITED_FROM IS NOT NULL)                               AS is_inherited,
    INHERITED_FROM                                             AS inherited_from,
    TABLE_CATALOG                                              AS cat,
    TABLE_SCHEMA                                               AS sch
  FROM system.information_schema.table_privileges
),
catalog_grants AS (
  SELECT
    'CATALOG'                       AS securable_type,
    CATALOG_NAME                    AS securable,
    GRANTEE                         AS raw_grantee,
    PRIVILEGE_TYPE                  AS privilege_type,
    CAST(NULL AS BOOLEAN)           AS is_inherited,   -- not tracked by this source; see caveats
    CAST(NULL AS STRING)            AS inherited_from,
    CATALOG_NAME                    AS cat,
    CAST(NULL AS STRING)            AS sch
  FROM system.information_schema.catalog_privileges
),
schema_grants AS (
  SELECT
    'SCHEMA'                                AS securable_type,
    CONCAT(CATALOG_NAME, '.', SCHEMA_NAME)  AS securable,
    GRANTEE                                 AS raw_grantee,
    PRIVILEGE_TYPE                          AS privilege_type,
    CAST(NULL AS BOOLEAN)                   AS is_inherited,   -- not tracked by this source; see caveats
    CAST(NULL AS STRING)                    AS inherited_from,
    CATALOG_NAME                            AS cat,
    SCHEMA_NAME                             AS sch
  FROM system.information_schema.schema_privileges
),
grants AS (
  SELECT * FROM table_grants
  UNION ALL
  SELECT * FROM catalog_grants
  UNION ALL
  SELECT * FROM schema_grants
),
-- Drop a grant its catalog or schema already gives the same grantee; it adds no access (caveats).
own_grants AS (
  SELECT g.*
  FROM grants g
  LEFT JOIN (SELECT DISTINCT cat, raw_grantee, privilege_type FROM catalog_grants) pc
    ON g.securable_type <> 'CATALOG' AND pc.cat = g.cat
   AND pc.raw_grantee = g.raw_grantee AND pc.privilege_type = g.privilege_type
  LEFT JOIN (SELECT DISTINCT cat, sch, raw_grantee, privilege_type FROM schema_grants) ps
    ON g.securable_type = 'TABLE' AND ps.cat = g.cat AND ps.sch = g.sch
   AND ps.raw_grantee = g.raw_grantee AND ps.privilege_type = g.privilege_type
  WHERE pc.cat IS NULL AND ps.cat IS NULL
),
classified AS (
  SELECT g.*,
         -- field heuristic, no dedicated principal-type column exists in either source (caveats)
         CASE
           WHEN g.raw_grantee IS NULL OR g.raw_grantee = '__REDACTED__'                    THEN 'UNKNOWN'
           WHEN LOWER(TRIM(g.raw_grantee)) IN ('account users', 'all account users', 'users') THEN 'ALL_USERS'
           WHEN g.raw_grantee RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN 'SERVICE_PRINCIPAL'
           WHEN g.raw_grantee LIKE '%@%'                                                   THEN 'USER'
           ELSE 'GROUP'
         END AS grantee_type
  FROM own_grants g
),
flagged AS (
  SELECT c.*,
         (c.privilege_type IN ('ALL PRIVILEGES', 'ALL_PRIVILEGES'))                 AS is_all_privileges,
         (c.grantee_type = 'ALL_USERS')                                             AS is_all_users,
         (c.grantee_type = 'GROUP' AND c.privilege_type IN ('MANAGE', 'OWNERSHIP')) AS is_manage_to_group,
         -- write, ANY scope: MODIFY, any CREATE_* privilege, or ALL PRIVILEGES. Judged on its own
         -- (no scope limit) so a write grant to every user in the account is CRITICAL no matter
         -- where it is granted; see is_write_at_catalog below for the scope-limited rule that
         -- still applies to a named (non all-users) principal.
         (c.privilege_type = 'MODIFY'
           OR c.privilege_type LIKE 'CREATE%'
           OR c.privilege_type IN ('ALL PRIVILEGES', 'ALL_PRIVILEGES'))             AS is_write,
         -- write, catalog or schema scope (caveats): same privilege set, but only counts as broad
         -- on its own (for a named, non all-users principal) when granted at CATALOG or SCHEMA
         -- scope -- a schema-level write grant reaches every table the schema holds now and
         -- later, the same leverage as a catalog-level one.
         (c.securable_type IN ('CATALOG', 'SCHEMA')
           AND (c.privilege_type = 'MODIFY'
             OR c.privilege_type LIKE 'CREATE%'
             OR c.privilege_type IN ('ALL PRIVILEGES', 'ALL_PRIVILEGES')))          AS is_write_at_catalog_or_schema
  FROM classified c
)
SELECT
  f.securable_type,
  f.securable,
  f.grantee_type,
  CASE
    WHEN f.raw_grantee IS NULL OR f.raw_grantee = '__REDACTED__' THEN f.raw_grantee
    WHEN f.grantee_type = 'SERVICE_PRINCIPAL'                    THEN f.raw_grantee
    WHEN f.grantee_type = 'USER'
      THEN (
        CASE
          WHEN f.raw_grantee IS NULL OR f.raw_grantee = '__REDACTED__' THEN f.raw_grantee
          WHEN f.raw_grantee RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN f.raw_grantee
          ELSE concat(substr(sha2(lower(trim(f.raw_grantee)), 256), 1, 8), ' ', substr(f.raw_grantee, 1, 2), '***')
        END
      )
    ELSE f.raw_grantee   -- GROUP or ALL_USERS: not personal data, shown in full
  END AS grantee,
  f.privilege_type,
  f.is_inherited,
  f.inherited_from,
  CASE
    WHEN f.is_all_privileges AND f.grantee_type = 'USER'      THEN 'ALL PRIVILEGES granted to a person, not a group'
    WHEN f.is_all_users AND f.is_write                       THEN 'write privilege granted to every user in the account'
    WHEN f.is_all_privileges                                THEN 'ALL PRIVILEGES granted to a group or service principal'
    WHEN f.is_all_users                                      THEN 'granted to every user in the account'
    WHEN f.is_manage_to_group                                THEN 'MANAGE (near-ownership) privilege granted to a group'
    WHEN f.is_write_at_catalog_or_schema                      THEN 'write privilege granted at catalog or schema level'
    ELSE NULL
  END AS reason,
  -- status: a WARN must have a lever, same discipline as every other verdict in this library.
  -- ALL PRIVILEGES to a person, or a write privilege granted to everyone (any scope), is the
  -- highest-leverage case; ALL PRIVILEGES to a group is usually an admin group.
  CASE
    WHEN f.is_all_privileges AND f.grantee_type = 'USER' THEN 'CRITICAL'
    WHEN f.is_all_users AND f.is_write            THEN 'CRITICAL'
    WHEN f.is_all_privileges OR f.is_all_users OR f.is_manage_to_group OR f.is_write_at_catalog_or_schema THEN 'WARN'
    ELSE 'OK'   -- unreachable: the WHERE clause below only ever admits a broad row
  END AS status
FROM flagged f
WHERE f.is_all_privileges OR f.is_all_users OR f.is_manage_to_group OR f.is_write_at_catalog_or_schema
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         securable_type, securable, privilege_type, grantee
LIMIT :top_n
