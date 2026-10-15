"""tests/fixtures/posture.py -- P3-POSTURE, the fixture builder for the two app-owned
config-posture findings (app/queries/app/compute/): `compute_cluster_config_posture` and
`compute_warehouse_config_posture`. This builder writes its own rows directly into
system.compute.clusters / system.compute.warehouses -- the same "a builder that needs rows in
another builder's source table writes its own parquet file into that source folder" pattern
tests/fixtures/compute.py's own module docstring documents (DEC-15) -- WITHOUT importing or
extending compute.py, per this task's own instruction that a new builder owns its rows alone.

Per DEC-15 this builder reuses the shared fixture workspace `1111` (acme-prod, written by
billing.py) and prefixes every id it writes with `ps_` (cluster ids `ps_cl_*`, warehouse ids
`ps_wh_*`) -- disjoint from every other builder's own prefix (cp_ compute, dd_ drilldown, bl_
billing, qh_ query_history, gv_ governance, pt_ pr_ pressure). Both queries are plain SCD2
config-posture snapshots with no time window (`params: none` shape other than the header's own
:max_autoterm_minutes / :max_autostop_minutes thresholds), so there is no AS_OF-relative window
to prove here -- only the latest-row-per-key dedupe (delete_time IS NULL, ORDER BY change_time
DESC) and the flag/status/reasons logic itself. change_time values below are relative to
base.AS_OF purely to give the SCD2 ORDER BY something real to sort on, not because either query
filters by time.

compute_cluster_config_posture (max_autoterm_minutes=120; grain [cluster_id]):
  CRITICAL  ps_cl_critical      data_security_mode='NONE' (bypasses UC); otherwise clean: DBR
                                14.3.x (supported), policy_id set, auto_termination=30 (<=120),
                                cluster_source UI. owned_by is a plain email -> proves the
                                hash-derived DEC-66.3 mask branch. reasons: isolation only.
  WARN      ps_cl_warn_multi    three WARN flags at once on one all-purpose cluster: DBR
                                11.3.x (end-of-support), policy_id NULL (no policy),
                                auto_termination_minutes NULL (off). data_security_mode
                                USER_ISOLATION (clean -- not CRITICAL). owned_by is a
                                service-principal GUID -> proves the GUID passthrough mask
                                branch. reasons: all three, in header column order.
  WARN      ps_cl_warn_autoterm auto_termination_minutes=500 (>120) is the ONLY flag; DBR
                                14.3.x, policy_id set, USER_ISOLATION, cluster_source UI.
                                reasons: auto-termination only (states the minutes and the
                                limit).
  OK        ps_cl_ok            DBR 14.3.x, policy_id set, USER_ISOLATION, auto_termination=45
                                (<=120), cluster_source UI. owned_by IS NULL -> proves the
                                NULL-passthrough mask branch. reasons: '' (no flag fired).
  WARN      ps_cl_job_nopolicy  cluster_source JOB (ephemeral): policy_id NULL fires (applies
                                to every cluster source), but auto_termination_minutes is ALSO
                                NULL and must NOT appear in reasons/status -- proves the
                                all-purpose-only scope of the auto-termination flag. DBR
                                14.3.x, USER_ISOLATION. reasons: no-policy only.
  WARN      ps_cl_pipeline_eol  cluster_source PIPELINE: DBR 9.1.x (end-of-support) fires;
                                policy_id set (no no-policy flag), USER_ISOLATION,
                                auto_termination_minutes NULL but cluster_source is not
                                UI/API so the auto-termination flag must not fire either.
                                reasons: end-of-support runtime only.
  NOT_ASSESSED ps_cl_mode_unknown  data_security_mode NULL: flag_no_isolation reads NULL/None, not
                                False (`data_security_mode = 'NONE' OR ... LIKE 'LEGACY_%'` is
                                itself NULL under SQL three-valued logic when data_security_mode
                                is NULL -- "unknown", never silently "not flagged"), and status
                                must not fall through to OK either -- otherwise a cluster whose
                                isolation could not even be checked would read as safe. Otherwise
                                clean: DBR 14.3.x, policy_id set, auto_termination=30 (<=120),
                                cluster_source UI. reasons: the not-recorded note only.
  CRITICAL  ps_cl_legacy_std    data_security_mode='LEGACY_SINGLE_USER_STANDARD' -- a real
                                Clusters API value not in the header's old enumerated LEGACY_*
                                list, proving the flag now matches on the LEGACY_ prefix instead
                                of naming each value. Otherwise clean: DBR 14.3.x, policy_id set,
                                auto_termination=30 (<=120), cluster_source UI. reasons: isolation
                                only.
  WARN      ps_cl_eol_old       DBR 7.3.x -- well below the 14.3 LTS line, proving the
                                end-of-support rule catches old runtimes the old five-value list
                                missed, not just the five named LTS releases. Otherwise clean:
                                USER_ISOLATION, policy_id set, auto_termination=30 (<=120),
                                cluster_source UI. reasons: end-of-support runtime only.
  WARN      ps_cl_eol_nonlts    DBR 15.2.x -- a non-LTS line between the (not-yet-released) 15.4
                                LTS and its predecessor, proving the rule also catches non-LTS
                                lines, not only named LTS releases. Otherwise clean:
                                USER_ISOLATION, policy_id set, auto_termination=30 (<=120),
                                cluster_source UI. reasons: end-of-support runtime only.
  (excl.)   ps_cl_deleted       2 raw rows; the LATEST carries delete_time NOT NULL -> the
                                whole cluster is absent from the built table (not deduped to 1
                                row, dropped to 0), same as classic_clusters_config_current's
                                own cp_cl_deleted proof.
  SCD2      ps_cl_scd2          2 raw rows: OLD (change_time AS_OF-30d) is clean (DBR 14.3.x,
                                auto_termination=20, policy_id set, USER_ISOLATION) so if the
                                dedupe ever read the wrong row this would wrongly read OK;
                                NEW/latest (change_time AS_OF-1d) sets auto_termination_minutes
                                to NULL (off) with everything else the same -> the built row
                                must read the LATEST configuration: WARN, auto-termination
                                only.

compute_warehouse_config_posture (max_autostop_minutes=60; grain [warehouse_id]):
  OK        ps_wh_ok            warehouse_type PRO, channel CHANNEL_NAME_CURRENT,
                                 auto_stop_minutes=10 (<=60). created_by IS NULL -> proves the
                                 NULL-passthrough mask branch.
  WARN      ps_wh_warn_multi    all three WARN flags at once: warehouse_type CLASSIC, channel
                                 CHANNEL_NAME_PREVIEW, auto_stop_minutes NULL (off).
                                 created_by is a service-principal GUID -> proves the GUID
                                 passthrough mask branch. reasons: all three.
  WARN      ps_wh_warn_autostop warehouse_type PRO, channel CURRENT, auto_stop_minutes=120
                                 (>60) is the only flag. created_by is a plain email -> proves
                                 the hash-derived mask branch. reasons: auto-stop only.
  WARN      ps_wh_warn_classic  warehouse_type CLASSIC is the only flag; channel CURRENT,
                                 auto_stop_minutes=15 (<=60). reasons: classic-type only.
  WARN      ps_wh_warn_preview  warehouse_channel CHANNEL_NAME_PREVIEW is the only flag;
                                 warehouse_type PRO, auto_stop_minutes=5. reasons: preview
                                 channel only.
  WARN      ps_wh_warn_preview_short  warehouse_channel is the bare 'PREVIEW' spelling (not
                                 'CHANNEL_NAME_PREVIEW') -- proves flag_preview_channel matches
                                 both spellings since the stored value is unconfirmed on a live
                                 account. warehouse_type PRO, auto_stop_minutes=5. reasons:
                                 preview channel only.
  (excl.)   ps_wh_deleted       2 raw rows; the LATEST carries delete_time NOT NULL -> the
                                 whole warehouse is absent from the built table, same shape as
                                 sql_warehouse_config_current's own cp_wh_deleted proof.
  SCD2      ps_wh_scd2          2 raw rows: OLD (change_time AS_OF-30d) is clean (PRO, CURRENT,
                                 auto_stop=10); NEW/latest (change_time AS_OF-1d) sets
                                 auto_stop_minutes to NULL (off), everything else unchanged ->
                                 the built row must read the LATEST configuration: WARN,
                                 auto-stop only.

Every scenario id and every value above is read back by tests/test_findings/test_posture.py,
which derives its expected values directly from this docstring/build() rather than duplicating
literals independently, so the two files cannot silently drift apart.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

ACCOUNT_ID = "ps_acct"
WS_PROD = "1111"  # shared fixture workspace (acme-prod), written by billing.py -- DEC-15

# DBR versions used by the scenarios above.
DBR_SUPPORTED = "14.3.x-scala2.12"   # not on the end-of-support list (the 14.3 LTS line itself)
DBR_EOL_11_3 = "11.3.x-scala2.12"    # on the end-of-support list (an old LTS line, below 14.3)
DBR_EOL_9_1 = "9.1.x-scala2.12"      # on the end-of-support list (an old LTS line, below 14.3)
DBR_EOL_OLD = "7.3.x-scala2.12"      # on the end-of-support list (well below 14.3, non-LTS-named)
DBR_EOL_NONLTS = "15.2.x-scala2.12"  # on the end-of-support list (a non-LTS line, 15.0-15.3)

OLD_CHANGE = AS_OF - timedelta(days=30)
NEW_CHANGE = AS_OF - timedelta(days=1)

# A service-principal GUID (RLIKE-matched, passed through unchanged by the DEC-66.3 mask) and a
# plain email (hashed to the `<8-hex> <2 chars>***` form) used to exercise both masked-identity
# branches plus the NULL-passthrough branch, spread across scenarios per the module docstring.
SP_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OWNER_EMAIL = "ps.owner@example.com"
CREATOR_EMAIL = "ps.creator@example.com"


# ---------------------------------------------------------------------------------------------
# compute.clusters (SCD2) -- own copy of compute.py's INSERT shape (DEC-15: independent parquet).
# ---------------------------------------------------------------------------------------------
_CL_SQL = (
    "INSERT INTO compute__clusters "
    "(account_id, workspace_id, cluster_id, cluster_name, owned_by, create_time, delete_time, "
    "driver_node_type, worker_node_type, worker_count, min_autoscale_workers, "
    "max_autoscale_workers, auto_termination_minutes, enable_elastic_disk, tags, cluster_source, "
    "init_scripts, aws_attributes, azure_attributes, gcp_attributes, driver_instance_pool_id, "
    "worker_instance_pool_id, dbr_version, change_time, change_date, data_security_mode, "
    "policy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _cluster(con, cluster_id, *, change_time, dbr_version, data_security_mode, policy_id,
             auto_term, cluster_source, owned_by=OWNER_EMAIL, delete_time=None,
             workspace_id=WS_PROD):
    con.execute(_CL_SQL, [
        ACCOUNT_ID, workspace_id, cluster_id, "ps-cluster", owned_by,
        change_time - timedelta(days=1), delete_time, "ps.driver", "ps.worker", 2, None, None,
        auto_term, True, {}, cluster_source, [], None, None, None, None, None, dbr_version,
        change_time, change_time.date(), data_security_mode, policy_id,
    ])


# ---------------------------------------------------------------------------------------------
# compute.warehouses (SCD2) -- own copy of compute.py's INSERT shape (DEC-15).
# ---------------------------------------------------------------------------------------------
_WH_SQL = (
    "INSERT INTO compute__warehouses "
    "(warehouse_id, workspace_id, account_id, warehouse_name, warehouse_type, "
    "warehouse_channel, warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, "
    "change_time, delete_time, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _warehouse(con, warehouse_id, *, change_time, warehouse_type, warehouse_channel, auto_stop,
               created_by=CREATOR_EMAIL, delete_time=None, workspace_id=WS_PROD):
    con.execute(_WH_SQL, [
        warehouse_id, workspace_id, ACCOUNT_ID, "ps-warehouse", warehouse_type, warehouse_channel,
        "SMALL", 1, 1, auto_stop, {}, change_time, delete_time, created_by,
    ])


def build(con: duckdb.DuckDBPyConnection) -> None:
    # ---- compute_cluster_config_posture scenarios -------------------------------------------
    _cluster(con, "ps_cl_critical", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="NONE", policy_id="ps_policy_1", auto_term=30,
              cluster_source="UI", owned_by=OWNER_EMAIL)

    _cluster(con, "ps_cl_warn_multi", change_time=NEW_CHANGE, dbr_version=DBR_EOL_11_3,
              data_security_mode="USER_ISOLATION", policy_id=None, auto_term=None,
              cluster_source="UI", owned_by=SP_GUID)

    _cluster(con, "ps_cl_warn_autoterm", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=500,
              cluster_source="API", owned_by=OWNER_EMAIL)

    _cluster(con, "ps_cl_ok", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=45,
              cluster_source="UI", owned_by=None)

    _cluster(con, "ps_cl_job_nopolicy", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="USER_ISOLATION", policy_id=None, auto_term=None,
              cluster_source="JOB", owned_by=OWNER_EMAIL)

    _cluster(con, "ps_cl_pipeline_eol", change_time=NEW_CHANGE, dbr_version=DBR_EOL_9_1,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=None,
              cluster_source="PIPELINE", owned_by=OWNER_EMAIL)

    # data_security_mode NULL -> flag_no_isolation reads NULL/None (SQL three-valued logic, never
    # silently False), and status must read NOT_ASSESSED, never OK: whether UC isolation applies
    # could not be checked at all.
    _cluster(con, "ps_cl_mode_unknown", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode=None, policy_id="ps_policy_1", auto_term=30,
              cluster_source="UI", owned_by=OWNER_EMAIL)

    # LEGACY_SINGLE_USER_STANDARD: a real access-mode value not in the old enumerated LEGACY_*
    # list -- proves flag_no_isolation now matches the LEGACY_ prefix, not a fixed value list.
    _cluster(con, "ps_cl_legacy_std", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="LEGACY_SINGLE_USER_STANDARD", policy_id="ps_policy_1",
              auto_term=30, cluster_source="UI", owned_by=OWNER_EMAIL)

    # Well below the 14.3 LTS line (older than any of the five old named LTS releases).
    _cluster(con, "ps_cl_eol_old", change_time=NEW_CHANGE, dbr_version=DBR_EOL_OLD,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=30,
              cluster_source="UI", owned_by=OWNER_EMAIL)

    # A non-LTS line (15.0-15.3), not one of the named LTS releases.
    _cluster(con, "ps_cl_eol_nonlts", change_time=NEW_CHANGE, dbr_version=DBR_EOL_NONLTS,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=30,
              cluster_source="UI", owned_by=OWNER_EMAIL)

    # deleted: 2 raw rows, latest carries delete_time -> whole cluster dropped, not deduped.
    _cluster(con, "ps_cl_deleted", change_time=OLD_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=30,
              cluster_source="UI")
    _cluster(con, "ps_cl_deleted", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=30,
              cluster_source="UI", delete_time=NEW_CHANGE)

    # SCD2: old row is clean, latest row turns auto-termination off -> must read the latest.
    _cluster(con, "ps_cl_scd2", change_time=OLD_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=20,
              cluster_source="UI")
    _cluster(con, "ps_cl_scd2", change_time=NEW_CHANGE, dbr_version=DBR_SUPPORTED,
              data_security_mode="USER_ISOLATION", policy_id="ps_policy_1", auto_term=None,
              cluster_source="UI")

    # ---- compute_warehouse_config_posture scenarios ------------------------------------------
    _warehouse(con, "ps_wh_ok", change_time=NEW_CHANGE, warehouse_type="PRO",
               warehouse_channel="CHANNEL_NAME_CURRENT", auto_stop=10, created_by=None)

    _warehouse(con, "ps_wh_warn_multi", change_time=NEW_CHANGE, warehouse_type="CLASSIC",
               warehouse_channel="CHANNEL_NAME_PREVIEW", auto_stop=None, created_by=SP_GUID)

    _warehouse(con, "ps_wh_warn_autostop", change_time=NEW_CHANGE, warehouse_type="PRO",
               warehouse_channel="CHANNEL_NAME_CURRENT", auto_stop=120, created_by=CREATOR_EMAIL)

    _warehouse(con, "ps_wh_warn_classic", change_time=NEW_CHANGE, warehouse_type="CLASSIC",
               warehouse_channel="CHANNEL_NAME_CURRENT", auto_stop=15)

    _warehouse(con, "ps_wh_warn_preview", change_time=NEW_CHANGE, warehouse_type="PRO",
               warehouse_channel="CHANNEL_NAME_PREVIEW", auto_stop=5)

    # bare 'PREVIEW' spelling (not 'CHANNEL_NAME_PREVIEW') -- proves flag_preview_channel matches
    # both spellings, since the stored value is unconfirmed on a live account.
    _warehouse(con, "ps_wh_warn_preview_short", change_time=NEW_CHANGE, warehouse_type="PRO",
               warehouse_channel="PREVIEW", auto_stop=5)

    # deleted: 2 raw rows, latest carries delete_time -> whole warehouse dropped.
    _warehouse(con, "ps_wh_deleted", change_time=OLD_CHANGE, warehouse_type="PRO",
               warehouse_channel="CHANNEL_NAME_CURRENT", auto_stop=10)
    _warehouse(con, "ps_wh_deleted", change_time=NEW_CHANGE, warehouse_type="PRO",
               warehouse_channel="CHANNEL_NAME_CURRENT", auto_stop=10, delete_time=NEW_CHANGE)

    # SCD2: old row is clean, latest row turns auto-stop off -> must read the latest.
    _warehouse(con, "ps_wh_scd2", change_time=OLD_CHANGE, warehouse_type="PRO",
               warehouse_channel="CHANNEL_NAME_CURRENT", auto_stop=10)
    _warehouse(con, "ps_wh_scd2", change_time=NEW_CHANGE, warehouse_type="PRO",
               warehouse_channel="CHANNEL_NAME_CURRENT", auto_stop=None)
