-- query_id: access_runas_escalation
-- title: Run-as escalation: initiator vs executed-as identity
-- domain: governance_access   tier: deep
-- reads: system.access.audit
-- requires: SELECT on system.access; Public Preview
-- empty_if: schema_not_enabled, preview_unavailable, verbose_audit_required, privilege_scoped
-- params: :period_days (default 30) rolling window in days.
-- confidence: needs_confirmation
-- confidence_note: identity_metadata.run_by and run_as are read directly off system.access.audit; no inferred column names there. The service-principal-vs-human classification below is a heuristic (the same UUID-shape test this query's own identity mask already used) that has not been cross-checked against a live account's actual service-principal id format.
-- read_this: One row = a workspace x masked run_by/run_as identity pair x service x action, where the principal that initiated an action (run_by) differs from the identity it executed as (run_as). The columns that matter are delegation_kind (whether run_by/run_as look like a service principal or a human) and newly_seen_pair (whether this exact pair has ever run before, across this table's full retention); event_count is supporting context only, never the driver of status.
-- healthy: status = OK; a known pair, of any delegation_kind - field heuristic: this table has seen the pair run before, so it is that workspace's established pattern. A low, steady event_count is often a legitimate job running as a service principal.
-- investigate_if: status = WARN - a newly seen pair that is still machine-involved (user_to_service_principal or service_principal_to_service_principal); status = CRITICAL - a newly seen pair where a human assumes another identity, either direction (service_principal_to_user or user_to_user) - field heuristic; also treat a ZERO-ROW result with suspicion rather than relief - see caveats.
-- actions: 1) confirm whether the run_as identity is a known service principal configured for that job/pipeline, and whether this pair is genuinely new automation (free); 2) if unexpected, revoke the run_by principal's ability to act as that identity and review its grants via access_grants_inventory (config); 3) if run-as patterns are hard to reason about at scale, invest in a naming/tagging convention for service principals so legitimate run-as pairs are self-documenting (spend/eng time).
-- next: access_admin_role_change_events (check if the same run_by identity also touched admin roles), access_grants_inventory (see what the run_as identity can do)
-- caveats: identity_metadata is commonly NULL for ordinary single-user actions, so expect it to be sparse or entirely empty on many accounts - an empty result here means "not assessed", not "no escalation happened". Before trusting a clean (zero-row) result, verify system.access.audit is actually populated for your account and window. This reads system.access.audit, which is Public Preview. WORKSPACE_ID: added to the SELECT/GROUP BY (confirmed present on system.access.audit); it is frequently NULL here because run-as delegation is commonly logged as an account-scoped event, not because the column itself is missing - the app's workspace filter may not narrow this finding even with the column present. STATUS (fixes access_runas_escalation-volume-not-escalation and access_runas_escalation-every-delegation-kind-warns): the query used to band CRITICAL/WARN purely on event COUNT per pair, so frequent-but-harmless automation and a rare real escalation got the same verdict; a first rewrite then banded WARN on every user_to_service_principal/user_to_user pair regardless of history, which made "Run now" on a job running as a service principal - the normal, recommended pattern - as noisy as a real escalation. It now bands on the pair's own SHAPE and NOVELTY instead: delegation_kind classifies run_by/run_as as service-principal-shaped (UUID-shaped, the same regex the identity mask above uses) or human; newly_seen_pair is true when pair_history - scanned over this table's FULL retention, not just :period_days - has never seen this exact (run_by, run_as) pair before this window. A pair this table has seen before, of ANY delegation_kind, is OK (that workspace's established pattern); only a NEWLY seen pair flags at all, and only a newly seen service_principal_to_user or user_to_user pair (a human assuming another identity) is CRITICAL - a newly seen user_to_service_principal or service_principal_to_service_principal pair (new automation, still machine-to-machine or a person delegating outward) is WARN. A service principal whose display id is not UUID-shaped on your account misclassifies as human, and a human whose identity happens to be UUID-shaped misclassifies as a service principal - see confidence_note. pair_history's full-retention scan is heavier than the rest of this query; narrow it to a fixed lookback if that cost matters more than the novelty signal on a long-retention account.
WITH pair_history AS (
  -- Every (run_by, run_as) pair this table has EVER seen, over its FULL retention (not just
  -- :period_days) - used to tell a genuinely new pair from one that simply didn't run earlier in a
  -- short window. Heavier than the rest of this query; see caveats.
  SELECT identity_metadata.run_by AS run_by, identity_metadata.run_as AS run_as,
         MIN(event_time) AS ever_first_event_time
  FROM system.access.audit
  WHERE identity_metadata.run_by IS NOT NULL
    AND identity_metadata.run_as IS NOT NULL
    AND identity_metadata.run_by <> identity_metadata.run_as
  GROUP BY identity_metadata.run_by, identity_metadata.run_as
),
window_pairs AS (
  SELECT workspace_id, service_name, action_name,
         identity_metadata.run_by AS run_by,
         identity_metadata.run_as AS run_as,
         COUNT(*) AS event_count,
         MIN(event_time) AS first_event_time,
         MAX(event_time) AS last_event_time
  FROM system.access.audit
  WHERE identity_metadata.run_by IS NOT NULL
    AND identity_metadata.run_as IS NOT NULL
    AND identity_metadata.run_by <> identity_metadata.run_as
    AND event_date >= current_date() - INTERVAL :period_days DAYS
    AND event_date < current_date()
  GROUP BY workspace_id, service_name, action_name, identity_metadata.run_by, identity_metadata.run_as
),
classified AS (
  -- Shape (service-principal vs human) and novelty, not raw event volume, drive status below.
  SELECT w.*,
         w.run_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_by_is_service_principal,
         w.run_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_as_is_service_principal,
         (h.ever_first_event_time IS NULL
          OR h.ever_first_event_time >= current_date() - INTERVAL :period_days DAYS) AS newly_seen_pair
  FROM window_pairs w
  LEFT JOIN pair_history h ON h.run_by = w.run_by AND h.run_as = w.run_as
)
SELECT workspace_id, service_name, action_name,
       CASE
         WHEN run_by IS NULL OR run_by = '__REDACTED__' THEN run_by
         WHEN run_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN run_by
         ELSE concat(substr(sha2(lower(trim(run_by)), 256), 1, 8), ' ', substr(run_by, 1, 2), '***')
       END AS run_by,
       CASE
         WHEN run_as IS NULL OR run_as = '__REDACTED__' THEN run_as
         WHEN run_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN run_as
         ELSE concat(substr(sha2(lower(trim(run_as)), 256), 1, 8), ' ', substr(run_as, 1, 2), '***')
       END AS run_as,
       CASE WHEN run_by_is_service_principal AND NOT run_as_is_service_principal THEN 'service_principal_to_user'
            WHEN NOT run_by_is_service_principal AND run_as_is_service_principal THEN 'user_to_service_principal'
            WHEN NOT run_by_is_service_principal AND NOT run_as_is_service_principal THEN 'user_to_user'
            ELSE 'service_principal_to_service_principal' END AS delegation_kind,
       newly_seen_pair,
       event_count, first_event_time, last_event_time,
       -- status: band on NOVELTY and SHAPE, not raw volume or delegation kind alone. A known pair
       -- (any kind) is OK; a newly seen pair is WARN, or CRITICAL when it is a human assuming
       -- another identity either direction. Zero rows does not mean OK - see caveats.
       CASE
         WHEN newly_seen_pair
              AND ((run_by_is_service_principal AND NOT run_as_is_service_principal)
                   OR (NOT run_by_is_service_principal AND NOT run_as_is_service_principal))
              THEN 'CRITICAL'
         WHEN newly_seen_pair THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM classified
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, event_count DESC
