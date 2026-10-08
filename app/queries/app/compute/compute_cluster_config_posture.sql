-- query_id: compute_cluster_config_posture
-- title: Cluster setup risk - end-of-support runtime, no Unity Catalog isolation, no governing
--   policy, auto-termination left off or too high
-- domain: compute   tier: standard
-- reads: system.compute.clusters
-- requires: SELECT on system.compute; GA
-- empty_if: compute_scope_gap
-- params: :max_autoterm_minutes (default 120) minutes above which an all-purpose cluster's
--   auto-termination is flagged as too high (NULL or 0 is flagged as off, at any value)
-- confidence: needs_confirmation
-- confidence_note: Columns and the SCD2 latest-row-per-cluster_id shape are the same ones
--   classic_clusters_config_current already confirms against a live system-catalog schema dump.
--   Three things here are new and unverified on a real account: 1) the end-of-support Databricks
--   Runtime rule below (derived from Databricks' published Long Term Support lifecycle
--   documentation, not observed on an account); 2) that cluster_source NOT IN ('UI','API')
--   reliably means "this cluster is ephemeral, so auto-termination does not apply" - confirmed
--   for JOB (job clusters are torn down when the run ends) and PIPELINE/PIPELINE_MAINTENANCE
--   (Lakeflow-managed) by the same caveats lakeflow_job_compute_pressure confirms, but not
--   exercised against a live account's actual policy/DBR/access-mode mix; 3) the full
--   data_security_mode enum below, including LEGACY_SINGLE_USER_STANDARD and the newer
--   DATA_SECURITY_MODE_STANDARD/DEDICATED/AUTO values, is taken from the installed
--   databricks-sdk's DataSecurityMode enum, not observed on a live account.
-- read_this: One row = the latest known configuration for one classic cluster (all-purpose, job,
--   Lakeflow SDP, or pipeline-maintenance) that has not been deleted - the same grain and dedupe
--   as classic_clusters_config_current, with a posture verdict added. The columns that matter are
--   status (worst flag wins), reasons (every flag that fired, in one line) and is_all_purpose
--   (whether auto-termination was even judged for this row).
-- healthy: status = OK - no flag fired. Field heuristic; the end-of-support list and
--   :max_autoterm_minutes are both approximations, tune/refresh them for your account.
-- investigate_if: status = CRITICAL - data_security_mode bypasses Unity Catalog governance
--   entirely (NONE or any LEGACY_* mode); status = WARN - end-of-support runtime, no cluster
--   policy attached, or (all-purpose only) auto-termination off/too high; status = NOT_ASSESSED -
--   data_security_mode was not recorded on this cluster, so whether Unity Catalog isolation
--   applies could not be checked at all - this is not a pass, treat it as needing a look the same
--   as WARN/CRITICAL. Read reasons for the exact combination; more than one flag can fire on the
--   same row.
-- actions: 1) turn on or lower auto-termination on the flagged all-purpose cluster so it cannot
--   run unattended (free); 2) attach an existing cluster policy to the cluster, or move it to
--   USER_ISOLATION/SINGLE_USER access mode so Unity Catalog governance actually applies (config);
--   3) upgrade the cluster's Databricks Runtime off an end-of-support version, which may need a
--   compatibility check against its libraries/jobs first (spend/eng time).
-- next: classic_clusters_config_current (full configuration, incl. node types and cloud
--   attributes), task_cluster_utilization (if the same cluster is also under CPU/memory
--   pressure), compute_idle_node_ratio (if it is also mostly idle), compute_warehouse_config_posture
--   (the SQL-warehouse half of this same posture check)
-- caveats: SCOPE - same as classic_clusters_config_current: classic compute ONLY (all-purpose,
--   jobs, Lakeflow SDP, pipeline-maintenance) - no serverless, no SQL warehouses (see
--   compute_warehouse_config_posture for those). is_all_purpose is cluster_source IN ('UI',
--   'API'); the auto-termination flag is judged ONLY on those rows - a JOB or PIPELINE/
--   PIPELINE_MAINTENANCE cluster is ephemeral (torn down when its run/update ends), so
--   auto_termination_minutes on it is not a risk signal and is left out of both the flag and the
--   status for that row, though the column itself is still returned for reference. The other
--   three flags (end-of-support runtime, no isolation/legacy access mode, no cluster policy)
--   apply to every cluster source, ephemeral or not - a policy still governs an ephemeral
--   cluster's shape, and a legacy access mode still bypasses Unity Catalog for whatever runs on
--   it, however briefly. END-OF-SUPPORT RUNTIME RULE (compiled 2026-09-24 from Databricks'
--   published Long Term Support lifecycle: an LTS release gets three years of support, a non-LTS
--   release roughly six months until the next LTS supersedes it; NOT observed on a live account -
--   refresh this rule before trusting a build run more than a few months after that date): every
--   runtime below the 14.3 LTS line (all of 0.x through 13.x) is flagged, plus the non-LTS lines
--   that sit between LTS releases - 14.0-14.2, 15.0-15.3 and 16.0-16.3; 14.3 LTS, 15.4 LTS (once
--   released) and any newer LTS or non-LTS line are treated as still supported. Matched with a
--   regex on the dbr_version string's own major.minor prefix (e.g. '13.3.x-scala2.12' and
--   '7.3.x-scala2.12' both match the below-14.3 branch, '15.2.x-scala2.12' matches the non-LTS
--   15.0-15.3 branch, '14.3.x-scala2.12' and '15.4.x-scala2.12' match neither) - a custom or
--   not-yet-released major.minor this rule does not cover is never flagged by it.
--   data_security_mode enum is USER_ISOLATION / SINGLE_USER / LEGACY_PASSTHROUGH /
--   LEGACY_SINGLE_USER / LEGACY_SINGLE_USER_STANDARD / LEGACY_TABLE_ACL / NONE / the newer
--   DATA_SECURITY_MODE_STANDARD / DATA_SECURITY_MODE_DEDICATED / DATA_SECURITY_MODE_AUTO / null
--   (classic_clusters_config_current's own confirmed caveat for the older values, the installed
--   databricks-sdk's DataSecurityMode enum for LEGACY_SINGLE_USER_STANDARD and the
--   DATA_SECURITY_MODE_* values). flag_no_isolation matches data_security_mode = 'NONE' OR LIKE
--   'LEGACY_%', so every LEGACY_* value is caught by the wildcard rather than an enumerated list
--   that can miss one; the newer DATA_SECURITY_MODE_* values are treated as safe (Unity
--   Catalog-native), same as USER_ISOLATION/SINGLE_USER. NULL is never read as OK: it is left out
--   of flag_no_isolation (unknown, not assumed safe or assumed unsafe), but status reads
--   NOT_ASSESSED for it instead of falling through to OK, and reasons always says the access mode
--   was not recorded, so this is never silently dropped. worker_count is NULL for
--   autoscaling clusters; min_autoscale_workers/max_autoscale_workers are NULL for fixed-size
--   clusters (unchanged from classic_clusters_config_current). owned_by is masked to the
--   DEC-66.3 identity format (`<id> <first 2 chars>***`, a hash-derived id when the source row
--   carries no real user id, service-principal GUIDs passed through unchanged) and cluster_name
--   to the existing 2-char-prefix mask, exactly as classic_clusters_config_current already does -
--   copied verbatim, not reinvented. Regional - run per metastore region. No dollars here: this
--   is a configuration posture check, not a cost query; price the flagged cluster with
--   compute_idle_node_ratio or the relevant cost_by_* query.
WITH latest AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
  FROM system.compute.clusters
),
current_clusters AS (
  SELECT * FROM latest WHERE rn = 1 AND delete_time IS NULL
),
flagged AS (
  SELECT
    cluster_id, workspace_id, account_id, cluster_name, owned_by, cluster_source, dbr_version,
    data_security_mode, policy_id, worker_count, min_autoscale_workers, max_autoscale_workers,
    auto_termination_minutes, create_time, change_time,
    (cluster_source IN ('UI', 'API')) AS is_all_purpose,
    (data_security_mode = 'NONE' OR data_security_mode LIKE 'LEGACY_%')  AS flag_no_isolation,
    (dbr_version RLIKE '^([0-9]|1[0-3])[.]' OR dbr_version RLIKE '^14[.][0-2][.]'
     OR dbr_version RLIKE '^1[56][.][0-3][.]')                                AS flag_eol_runtime,
    (policy_id IS NULL)                                                       AS flag_no_policy,
    ((cluster_source IN ('UI', 'API'))
     AND (auto_termination_minutes IS NULL OR auto_termination_minutes = 0
          OR auto_termination_minutes > :max_autoterm_minutes))               AS flag_auto_term_risk
  FROM current_clusters
)
SELECT
  cluster_id,
  workspace_id,
  cluster_name,
  CASE
    WHEN owned_by IS NULL OR owned_by = '__REDACTED__' THEN owned_by
    WHEN owned_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
      THEN owned_by
    ELSE concat(substr(sha2(lower(trim(owned_by)), 256), 1, 8), ' ', substr(owned_by, 1, 2), '***')
  END                                                                         AS owned_by,
  cluster_source,
  is_all_purpose,
  dbr_version,
  data_security_mode,
  policy_id,
  worker_count, min_autoscale_workers, max_autoscale_workers, auto_termination_minutes,
  flag_eol_runtime,
  flag_no_isolation,
  flag_no_policy,
  flag_auto_term_risk,
  CONCAT_WS('; ',
    CASE WHEN flag_no_isolation THEN
      CONCAT('access mode ', data_security_mode, ' bypasses Unity Catalog (no user isolation)')
    END,
    CASE WHEN data_security_mode IS NULL THEN
      'access mode not recorded, so Unity Catalog isolation could not be checked'
    END,
    CASE WHEN flag_eol_runtime THEN
      CONCAT('runtime ', dbr_version, ' is past Databricks end-of-support')
    END,
    CASE WHEN flag_no_policy THEN 'no cluster policy attached' END,
    CASE WHEN flag_auto_term_risk THEN
      CASE
        WHEN auto_termination_minutes IS NULL OR auto_termination_minutes = 0
          THEN 'auto-termination is off'
        ELSE CONCAT('auto-termination ', CAST(auto_termination_minutes AS STRING),
                     ' min, above the ', CAST(:max_autoterm_minutes AS STRING), ' min limit')
      END
    END
  )                                                                           AS reasons,
  CASE
    WHEN flag_no_isolation THEN 'CRITICAL'
    WHEN flag_eol_runtime OR flag_no_policy OR flag_auto_term_risk THEN 'WARN'
    WHEN data_security_mode IS NULL THEN 'NOT_ASSESSED'
    ELSE 'OK'
  END                                                                         AS status,
  create_time, change_time
FROM flagged
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         cluster_id
