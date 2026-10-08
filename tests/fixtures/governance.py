"""tests/fixtures/governance.py -- batch F (T-20), the ONE builder for every governance source
table: system.access.audit, system.access.table_lineage, system.access.column_lineage,
system.access.inbound_network, system.access.outbound_network, system.data_classification.results,
and every system.information_schema.* table an F1 or F2 governance_access query reads (PLAN.md 7.2
batch F / F2 rows). This module supplies rows for BOTH this task's 12 F1 ids (tested here) AND
T-21's 10 F2 inventory ids (tested by T-21, which may not edit this file) -- never split across two
files (DEC-17 auto-discovers tests/fixtures/<name>.py modules by file name, sorted).

Per DEC-15 this is NOT the drill-down builder: it uses the shared workspace pool (1111 acme-prod,
2222 acme-dev, 3333 acme-uat), plus the account-level workspace_id "0" for the access.audit rows
that are genuinely account-wide per the vendored caveats. Every other id this builder writes
(table/schema/share names, action names, statement/edge identifiers, rule labels, etc.) carries the
prefix `gv_`, disjoint from every other builder's ids.

None of the 12 F1 query bodies GROUP BY a date/day column -- every window-boundary row below
therefore carries its OWN unique grouping key (a distinct target table name, destination, rule
label, etc.) per time anchor, so presence/absence at a given window_days is unambiguous regardless
of whether the anchor's own row also happens to satisfy the WHERE clause at a wider window.

Five reusable time anchors, relative to AS_OF = 2026-09-21 12:00:00 (identical deltas to
tests/fixtures/query_history.py's, already proven against the same pinned audit_today() /
audit_now() literals):

    D7     AS_OF - 3d    inside the 7d window (and 30d, 90d)
    D30    AS_OF - 16d   inside the 30d window, outside the 7d window
    D90    AS_OF - 68d   inside the 90d window, outside the 30d window
    DTODAY 2026-09-21 08:00:00   on AS_OF's own calendar day
    DOLD   AS_OF - 143d  older than the 90d window -- excluded everywhere

access.audit / access.table_lineage / access.column_lineage filter on event_date with BOTH a lower
bound (>= current_date() - :period_days) AND an upper bound (< current_date()) -- DTODAY is
EXCLUDED at every window for these three tables (today itself never lands in any window). access
.inbound_network / access.outbound_network filter on event_time with a lower bound only (>=
current_timestamp() - :period_days, no upper bound) -- DTODAY IS INCLUDED at every window for
these two (matching query_history.py's audit_self_cost pattern); DOLD is excluded everywhere on
all five tables.

Predicate / CASE-branch / window-boundary -> row map (see each `_*_rows()` function for the
literal ids and values):

  access_admin_role_change_events (warn_admin_events=20, crit_admin_events=100; T-73 upstream fix
      -- allow-listed (service_name, action_name) pairs only, no longer service_name alone)
    OK       unityCatalog/updatePermissions      (5 events, workspace 1111 default)
    WARN     accountsAccessControl/updateRuleSet (20 events, workspace 1111)
    CRITICAL accounts/setAdmin                   (100 events, workspace_id="0" account-level,
                               actor identity NULL -> mask branch 1 passthrough)
    excluded-by-service  servingEndpoints/createServingEndpoint (service_name not in the allow-list
                               at all)
    excluded-by-action   unityCatalog/getTable (allow-listed SERVICE, but a READ action -- the
                               actual T-73 bug: the old query counted this), accounts/tokenLogin
                               (allow-listed SERVICE, but a token LOGIN, not a token change)
    OK (review fix)  accounts/setAccountAdmin (1 event, workspace_id="0" account-level,
                               audit_level=ACCOUNT_LEVEL -- proves the allow-list's account-admin-
                               role-grant gap is closed), workspace/updateRoleAssignment (1 event,
                               workspace 1111 -- proves the workspace role-assignment gap is closed)
    OK (review fix)  accounts/removeAdmin (2 events, response.status_code=403 on both -- proves
                               event_count still counts every attempt (2) while failed_event_count
                               also counts 2 denied/non-200 attempts; every other group above uses
                               the default status_code=200 response, so their own failed_event_count
                               stays 0)
    window   accounts/{addPrincipalToGroup=t7, removePrincipalFromGroup=t30, createGroup=t90,
                        removeGroup=ttoday, updateGroup=told90} (own action_name per anchor, all
                        allow-listed so only the event_date predicate excludes ttoday/told90)

  access_login_concentration (warn_failed_logins=5, crit_failed_logins=20; service_name='accounts')
    OK       gv_login_ok     (3 success events, non_success_count=0)
    WARN     gv_login_warn   (5 non-success events, status_code=403)
    CRITICAL gv_login_crit   (20 non-success events, principal='__REDACTED__' -> mask branch 1;
                               status_code 403 -> a real failure; a NULL status is counted apart)
    window   gv_login_win_{t7,t30,t90,ttoday,told90}

  access_runas_escalation (status bands on delegation_kind + newly_seen_pair, not event count)
    OK       gv_runas_ok     (2 events; pair also seen before the window -> known pair -> OK
                              regardless of shape)
    WARN     gv_runas_warn   (5 events; run_as is a 36-char hex GUID -> user_to_service_principal,
                              newly seen -> WARN; mask branch 2 passthrough on run_as)
    CRITICAL gv_runas_crit   (25 events; both sides human-shaped -> user_to_user, newly seen ->
                              CRITICAL)
    excluded-equal  gv_runas_equal (run_by = run_as -> excluded)
    excluded-null   gv_runas_nullby (run_by NULL -> excluded)
    window   gv_runas_win_{t7,t30,t90,ttoday,told90} (both sides human-shaped, newly seen ->
                              CRITICAL, same shape as gv_runas_crit)

  access_vector_search_traffic (no status -- inventory; service_name='vectorSearch',
      action_name IN the 5 query/scan actions; request_params['endpoint_name'])
    included group_a  gv_vst_ep1 (queryVectorIndex, 3 events)
    included group_b  gv_vst_ep2 (scanVectorIndex, 2 events)
    excluded-service   gv_vst_wrong_service (service_name != 'vectorSearch')
    excluded-action    gv_vst_wrong_action (action_name not in the 5-action list)
    window   gv_vst_win_{t7,t30,t90,ttoday,told90} (action_name='queryVectorIndexNextPage', own
              event_date per anchor -- action_name AND event_date both separate this group from
              gv_vst_ep1/ep2 above; see the DEC-48 note above _vector_search_traffic_rows() for
              why the window rows do not just reuse gv_vst_ep1's action_name)

  access_column_lineage_sensitive_reach (warn_reach_principals=10, crit_reach_principals=50;
      source_table_full_name IS NOT NULL; every *_src column below carries a column_tags
      sensitivity tag (pii=true) -- the query only shows edges whose source is tagged)
    OK       gv_reach_ok_src -> gv_reach_ok_tgt    (3 distinct principals)
    WARN     gv_reach_warn_src -> gv_reach_warn_tgt (10 distinct principals)
    CRITICAL gv_reach_crit_src -> gv_reach_crit_tgt (50 distinct principals)
    excluded-null-source  gv_reach_nullsrc (source_table_full_name NULL -> excluded)
    window   gv_reach_win_src -> gv_reach_win_{t7,t30,t90,ttoday,told90}_tgt (distinct target per
              anchor; 1 principal each, band not the point)

  access_pii_propagation_untagged (warn_pii_gap_events=5, crit_pii_gap_events=50;
      direct_access=true, source/target column_name NOT NULL, source_table_catalog<>'system')
    match-excluded  gv_pii_match_src -> gv_pii_match_tgt (source tagged sensitive via tag_name
              'pii', target ALSO tagged sensitive (tag_value 'confidential') -> anti-join fails
              -> never a gap row)
    WARN     gv_pii_warn_src -> gv_pii_warn_tgt   (source tagged via tag_value 'restricted',
              target untagged, 5 events)
    CRITICAL gv_pii_crit_src -> gv_pii_crit_tgt   (source tagged via tag_name 'pii', target
              untagged, 50 events)
    excluded-untagged-source  gv_pii_notag_src (no column_tags row at all -> never in
              sensitive_tags -> excluded, INNER JOIN)
    excluded-system-catalog   gv_pii_sys_src (source_table_catalog='system' -> excluded by WHERE)
    excluded-indirect         gv_pii_indirect_src (direct_access=false -> excluded by WHERE)
    window   gv_pii_win_src (tagged) -> gv_pii_win_{t7,t30,t90,ttoday,told90}_tgt (untagged,
              distinct target per anchor, 1 event each, band not the point)

  access_dead_table_candidates (warn_dead_days=90, crit_dead_days=365; MANAGED/EXTERNAL tables
      that never appear as a table_lineage SOURCE in the window)
    CRITICAL gv_dead_crit   (last_altered AS_OF-400d, table_owner='__REDACTED__' -> mask branch 1)
    WARN     gv_dead_warn   (last_altered AS_OF-120d, table_owner an email -> mask branch 3, DEC-66.3 hash-derived id)
    OK       gv_dead_ok     (last_altered AS_OF-10d, table_owner plain string -> mask branch 3, DEC-66.3 hash-derived id)
    NOT_ASSESSED gv_dead_notassessed (last_altered NULL, table_owner a GUID -> mask branch 2)
    excluded-alive   gv_alive_excluded (last_altered AS_OF-500d but DOES appear as a lineage
              source, direct_access=false, within the window -> excluded: proves an indirect read
              still counts as "appeared as a source")
    excluded-catalog gv_sys_excluded (table_catalog='system' -> excluded by inventory filter)
    excluded-schema  gv_infoschema_excluded (table_schema='information_schema' -> excluded)
    excluded-type    gv_view_excluded (table_type='VIEW' -> excluded; also feeds F2's
              table_inventory_type with a non-MANAGED/EXTERNAL row)
    window   gv_dead_win_boundary (last_altered AS_OF-400d, i.e. CRITICAL-eligible) with its own
              lineage SOURCE row at D30 -- at window_days=7 the D30 row is outside the window, so
              this table looks dead (CRITICAL); at window_days=30/90 the D30 row is inside the
              window, so this table is excluded (alive) -- the one id-specific window boundary
              test for this id (period_days here gates lineage-source visibility, not a date column
              on the inventory side)

  access_table_lineage_blast_radius (warn_blast_principals=10, crit_blast_principals=50)
    READ class        gv_blast_read_src -> gv_blast_read_tgt: OK (3), WARN (10), CRITICAL (50)
                       distinct-principal groups (source_type set, target_type NULL)
    WRITE class        gv_blast_write_src -> gv_blast_write_tgt (target_type set, source_type NULL)
    READ_WRITE class   gv_blast_rw_src -> gv_blast_rw_tgt (both set)
    UNKNOWN class      gv_blast_unknown_src -> gv_blast_unknown_tgt (both NULL)
    window   gv_blast_win_src -> gv_blast_win_{t7,t30,t90,ttoday,told90}_tgt (READ_WRITE class --
              both source_type and target_type set so target_table_full_name is populated and the
              window rows are identifiable by target name; 1 principal each, band/class not the
              point)

  access_classified_unmasked (windowless, params: none; confidence='HIGH' AND class_tag NOT NULL)
    OK        gv_dc_masked (HIGH, PII, matching column_masks row -> is_unmasked=false)
    CRITICAL  gv_dc_unmasked (HIGH, PII, no column_masks row -> is_unmasked=true)
    excluded-lowconf  gv_dc_lowconf (confidence='LOW' -> excluded here, included in inventory)
    excluded-notag    gv_dc_notag (class_tag NULL -> excluded from both ids)

  access_data_classification_inventory (windowless, params: none; class_tag IS NOT NULL;
      GROUP BY catalog/schema/table/column/class_tag/confidence/data_type)
    dedup group  gv_lowconf_tbl appears TWICE with the same group key but different
              frequency/first_detected_time/latest_detected_time (both VARCHAR since this table
              is MISSING_FROM_DUMP / all-VARCHAR) -> proves MAX(frequency)/MAX(latest)/MIN(first)
              collapse to one row. NOT gv_dc_masked -- access_classified_unmasked has no GROUP BY
              at all, so a raw duplicate there would break THAT id's grain
              (catalog/schema/table/column); gv_lowconf_tbl's confidence='LOW' keeps it out of
              access_classified_unmasked's WHERE entirely, so the duplicate is safe there.
    gv_dc_unmasked, gv_dc_lowconf also appear (class_tag set on both)
    gv_dc_notag excluded (class_tag NULL)

  access_network_inbound_denials (warn_denial_count=10, crit_denial_count=50; event_time only, no
      upper bound)
    OK       gv_in_ok_rule    (3 denials, policy_outcome='DENY_DRY_RUN')
    WARN     gv_in_warn_rule  (10 denials, authenticated_as plain string -> mask branch 3, DEC-66.3 hash-derived id)
    CRITICAL gv_in_crit_rule  (50 denials, authenticated_as NULL -> mask branch 1)
    window   gv_in_win_{t7,t30,t90,ttoday,told90}_rule (DTODAY included here, no upper bound;
              DOLD excluded)

  access_network_outbound_denials (warn_denial_count=10, crit_denial_count=50; event_time only, no
      upper bound)
    OK       gv-out-ok.example.com     (3 denials, destination_type='DNS')
    WARN     gv-out-warn-bucket        (10 denials, destination_type='STORAGE')
    CRITICAL gv-out-crit.example.com   (50 denials, destination_type='DNS')
    window   gv-out-win-{t7,t30,t90,ttoday,told90}.example.com

F2-only support rows (no F1 id reads these; written so T-21, which owns no fixture rows, can test
its 10 ids straight away): system.information_schema.{table_privileges, catalog_privileges,
schema_privileges, connection_privileges, credential_privileges, external_location_privileges,
row_filters, table_tags, schema_tags, volume_tags, views, volumes, shares,
share_recipient_privileges, table_share_usage, schema_share_usage} -- see `_f2_support_rows()`.
column_masks and column_tags rows are written by the F1 helpers above (access_classified_unmasked
/ access_pii_propagation_untagged) and reused as-is by F2's access_column_masks_inventory /
access_tags_inventory; information_schema.tables rows are written by
`_dead_table_candidates_rows()` and reused by F2's table_inventory_type (storage domain).

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for callers)

# DEC-15: shared workspace pool, plus the account-level "0" workspace for global access.audit
# events (account-level events are global per the vendored caveats).
WS_PROD = "1111"
WS_DEV = "2222"
WS_UAT = "3333"
WS_ACCOUNT = "0"

GV_CATALOG = "gv_catalog"
GV_SCHEMA = "gv_schema"

# ---------------------------------------------------------------------------------------------
# Time anchors (module docstring above explains each).
# ---------------------------------------------------------------------------------------------
D7 = AS_OF - timedelta(days=3)
D30 = AS_OF - timedelta(days=16)
D90 = AS_OF - timedelta(days=68)
DTODAY = datetime(2026, 9, 21, 8, 0, 0)
DOLD = AS_OF - timedelta(days=143)

WINDOW_ANCHORS = [("t7", D7), ("t30", D30), ("t90", D90), ("ttoday", DTODAY), ("told90", DOLD)]

_seq = {"n": 0}


def _next_id() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------------------------
# access__audit
# ---------------------------------------------------------------------------------------------
AUDIT_COLUMNS = [
    "account_id", "workspace_id", "version", "event_time", "event_date", "source_ip_address",
    "user_agent", "session_id", "user_identity", "service_name", "action_name", "request_id",
    "request_params", "response", "audit_level", "event_id", "identity_metadata",
]

_AUDIT_DEFAULTS = {c: None for c in AUDIT_COLUMNS}
_AUDIT_DEFAULTS.update({
    "account_id": "gv-account",
    "workspace_id": WS_PROD,
    "version": "2.0",
    "user_identity": {"email": None, "subject_name": None},
    "request_params": {},
    "response": {"status_code": 200, "error_message": None, "result": "success"},
    "audit_level": "WORKSPACE_LEVEL",
    "identity_metadata": {
        "run_by": None, "run_as": None, "acting_resource": None,
        "run_by_display_name": None, "run_as_display_name": None,
    },
})


def _audit_row(event_time: datetime, **overrides) -> dict:
    unknown = set(overrides) - set(AUDIT_COLUMNS)
    if unknown:
        raise ValueError(f"unknown access__audit column(s): {sorted(unknown)}")
    r = dict(_AUDIT_DEFAULTS)
    r["event_time"] = event_time
    r["event_date"] = event_time.date()
    r["event_id"] = f"gv_evt_{_next_id()}"
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------------------------
# access__table_lineage / access__column_lineage (shared column prefix)
# ---------------------------------------------------------------------------------------------
TABLE_LINEAGE_COLUMNS = [
    "account_id", "metastore_id", "workspace_id", "entity_type", "entity_id", "entity_run_id",
    "source_table_full_name", "source_table_catalog", "source_table_schema", "source_table_name",
    "source_path", "source_type", "target_table_full_name", "target_table_catalog",
    "target_table_schema", "target_table_name", "target_path", "target_type", "created_by",
    "event_time", "event_date", "record_id", "event_id", "statement_id", "entity_metadata",
    "direct_access",
]

COLUMN_LINEAGE_COLUMNS = [
    "account_id", "metastore_id", "workspace_id", "entity_type", "entity_id", "entity_run_id",
    "source_table_full_name", "source_table_catalog", "source_table_schema", "source_table_name",
    "source_path", "source_type", "source_column_name", "target_table_full_name",
    "target_table_catalog", "target_table_schema", "target_table_name", "target_path",
    "target_type", "target_column_name", "created_by", "event_time", "event_date", "record_id",
    "event_id", "statement_id", "entity_metadata", "direct_access",
]

_LINEAGE_SHARED_DEFAULTS = {
    "account_id": "gv-account", "metastore_id": "gv-metastore", "workspace_id": WS_PROD,
    "entity_type": "NOTEBOOK", "entity_id": None, "entity_run_id": None,
    "source_path": None, "target_path": None, "record_id": None, "statement_id": None,
    "entity_metadata": None, "direct_access": True,
}


def _table_lineage_row(event_time: datetime, **overrides) -> dict:
    unknown = set(overrides) - set(TABLE_LINEAGE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown access__table_lineage column(s): {sorted(unknown)}")
    r = {c: None for c in TABLE_LINEAGE_COLUMNS}
    r.update(_LINEAGE_SHARED_DEFAULTS)
    r["event_time"] = event_time
    r["event_date"] = event_time.date()
    r["event_id"] = f"gv_evt_{_next_id()}"
    r.update(overrides)
    return r


def _column_lineage_row(event_time: datetime, **overrides) -> dict:
    unknown = set(overrides) - set(COLUMN_LINEAGE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown access__column_lineage column(s): {sorted(unknown)}")
    r = {c: None for c in COLUMN_LINEAGE_COLUMNS}
    r.update(_LINEAGE_SHARED_DEFAULTS)
    r["event_time"] = event_time
    r["event_date"] = event_time.date()
    r["event_id"] = f"gv_evt_{_next_id()}"
    r.update(overrides)
    return r


def _full_name(catalog: str | None, schema: str | None, name: str | None) -> str | None:
    if name is None:
        return None
    return f"{catalog}.{schema}.{name}"


# ---------------------------------------------------------------------------------------------
# access__inbound_network / access__outbound_network
# ---------------------------------------------------------------------------------------------
INBOUND_COLUMNS = [
    "account_id", "workspace_id", "event_id", "request_path", "source", "authenticated_as",
    "event_time", "policy_outcome", "rule_label",
]

_INBOUND_DEFAULTS = {c: None for c in INBOUND_COLUMNS}
_INBOUND_DEFAULTS.update({
    "account_id": "gv-account", "workspace_id": WS_PROD, "policy_outcome": "DENY",
})


def _inbound_row(event_time: datetime, **overrides) -> dict:
    unknown = set(overrides) - set(INBOUND_COLUMNS)
    if unknown:
        raise ValueError(f"unknown access__inbound_network column(s): {sorted(unknown)}")
    r = dict(_INBOUND_DEFAULTS)
    r["event_time"] = event_time
    r["event_id"] = f"gv_evt_{_next_id()}"
    r.update(overrides)
    return r


OUTBOUND_COLUMNS = [
    "account_id", "workspace_id", "destination_type", "destination", "dns_event",
    "storage_event", "event_time", "access_type", "event_id", "network_source_type",
]

_OUTBOUND_DEFAULTS = {c: None for c in OUTBOUND_COLUMNS}
_OUTBOUND_DEFAULTS.update({
    "account_id": "gv-account", "workspace_id": WS_PROD, "access_type": "BLOCKED",
    "network_source_type": "SERVERLESS",
})


def _outbound_row(event_time: datetime, **overrides) -> dict:
    unknown = set(overrides) - set(OUTBOUND_COLUMNS)
    if unknown:
        raise ValueError(f"unknown access__outbound_network column(s): {sorted(unknown)}")
    r = dict(_OUTBOUND_DEFAULTS)
    r["event_time"] = event_time
    r["event_id"] = f"gv_evt_{_next_id()}"
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------------------------
# data_classification__results (MISSING_FROM_DUMP, all-VARCHAR -- frequency / *_detected_time
# are strings, not typed numbers/timestamps; formatted so lexical ordering == the real ordering).
# ---------------------------------------------------------------------------------------------
DC_COLUMNS = [
    "catalog_name", "class_tag", "column_name", "confidence", "data_type",
    "first_detected_time", "frequency", "latest_detected_time", "schema_name", "table_name",
]


def _dc_row(**overrides) -> dict:
    unknown = set(overrides) - set(DC_COLUMNS)
    if unknown:
        raise ValueError(f"unknown data_classification__results column(s): {sorted(unknown)}")
    r = {
        "catalog_name": GV_CATALOG, "schema_name": GV_SCHEMA, "data_type": "STRING",
        "confidence": "HIGH", "frequency": "0.90",
        "first_detected_time": "2026-08-01 00:00:00", "latest_detected_time": "2026-09-15 00:00:00",
    }
    r.update(overrides)
    return {c: r.get(c) for c in DC_COLUMNS}


# ---------------------------------------------------------------------------------------------
# information_schema.* tables (19). Per-table column lists match tests/fixtures/ddl.py's
# ARROW_SCHEMA exactly. Named-column INSERTs mean row dicts only need the keys a scenario cares
# about; every other column defaults to NULL via the per-table factory below.
# ---------------------------------------------------------------------------------------------
IS_COLUMN_MASKS_COLUMNS = [
    "column_name", "mask_name", "table_catalog", "table_name", "table_schema", "using_columns",
]
IS_ROW_FILTERS_COLUMNS = ["filter_name", "table_catalog", "table_name", "table_schema", "target_columns"]
IS_TABLE_PRIVILEGES_COLUMNS = [
    "grantor", "grantee", "table_catalog", "table_schema", "table_name", "privilege_type",
    "is_grantable", "inherited_from",
]
IS_CATALOG_PRIVILEGES_COLUMNS = ["grantor", "grantee", "catalog_name", "privilege_type", "is_grantable"]
IS_SCHEMA_PRIVILEGES_COLUMNS = ["grantee", "object_scope", "privilege_type"]
IS_CONNECTION_PRIVILEGES_COLUMNS = ["grantee", "object_scope", "privilege_type"]
IS_CREDENTIAL_PRIVILEGES_COLUMNS = ["grantee", "object_scope", "privilege_type"]
IS_EXTERNAL_LOCATION_PRIVILEGES_COLUMNS = ["grantee", "object_scope", "privilege_type"]
IS_COLUMN_TAGS_COLUMNS = [
    "catalog_name", "column_name", "created_by", "crit_pii_gap_events",
    "direct_access", "event_count", "event_date", "lineage_window", "lw", "object_scope",
    "period_days", "schema_name", "sensitive_tags", "source_column_name", "source_table_catalog",
    "source_table_name", "source_table_schema", "st", "table_name", "tag_name", "tag_value",
    "target_column_name", "target_table_catalog", "target_table_name", "target_table_schema",
    "tt", "warn_pii_gap_events",
]
IS_TABLE_TAGS_COLUMNS = ["catalog_name", "schema_name", "table_name", "tag_name", "tag_value"]
IS_SCHEMA_TAGS_COLUMNS = ["catalog_name", "schema_name", "tag_name", "tag_value"]
IS_VOLUME_TAGS_COLUMNS = ["catalog_name", "schema_name", "volume_name", "tag_name", "tag_value"]
IS_VIEWS_COLUMNS = [
    "table_catalog", "table_schema", "table_name", "view_definition", "check_option",
    "is_updatable", "is_insertable_into", "sql_path", "is_materialized",
]
IS_VOLUMES_COLUMNS = [
    "volume_catalog", "volume_schema", "volume_name", "volume_type", "volume_owner", "comment",
    "storage_location", "created", "created_by", "last_altered", "last_altered_by",
]
IS_SHARES_COLUMNS = ["share_name", "share_owner", "comment", "created", "created_by", "last_altered", "last_altered_by"]
IS_SHARE_RECIPIENT_PRIVILEGES_COLUMNS = ["grantor", "recipient_name", "share_name", "privilege_type"]
IS_TABLE_SHARE_USAGE_COLUMNS = [
    "catalog_name", "schema_name", "table_name", "share_name", "partition_spec", "cdf_enabled",
    "start_version", "shared_as_schema", "shared_as_table", "comment",
]
IS_SCHEMA_SHARE_USAGE_COLUMNS = ["catalog_name", "schema_name", "share_name", "shared_as_schema", "comment"]
IS_TABLES_COLUMNS = [
    "table_catalog", "table_schema", "table_name", "table_type", "is_insertable_into",
    "commit_action", "table_owner", "comment", "created", "created_by", "last_altered",
    "last_altered_by", "data_source_format", "storage_sub_directory", "storage_path",
]


def _mk_row(columns: list[str], **overrides) -> dict:
    unknown = set(overrides) - set(columns)
    if unknown:
        raise ValueError(f"unknown column(s) {sorted(unknown)} for column set {columns}")
    r = {c: None for c in columns}
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------------------------
# Identity-mask literal helpers -- these MIRROR the SQL CASE in the vendored bodies (they are not
# used to compute expected output; tests compute expectations independently by re-implementing
# the same 3-branch rule against the builder's own known-by-construction raw values). Kept here
# only as a readability aid for which branch a given raw value below is meant to exercise
# (DEC-66.3; a value that is already a service-principal GUID passes through unchanged, and NULL
# / '__REDACTED__' pass through too, so the "identity format" branch below is the only one that
# actually masks anything):
#   branch 1: NULL or '__REDACTED__'      -> passthrough
#   branch 2: 36-char hex GUID            -> passthrough
#   branch 3: else                        -> first 8 hex chars of sha256(lower(trim(v))) (or the
#                                             row's own real user id, where the source table
#                                             carries one) + ' ' + first 2 chars of v + '***'
# ---------------------------------------------------------------------------------------------
GUID_EXAMPLE_1 = "11112222-3333-4444-5555-666677778888"
GUID_EXAMPLE_2 = "99998888-7777-6666-5555-444433332222"


# ---------------------------------------------------------------------------------------------
# access_admin_role_change_events -- warn_admin_events=20, crit_admin_events=100. T-73 upstream fix:
# the vendored query now allow-lists specific (service_name, action_name) pairs (state-changing
# admin/permission/security-configuration actions only) instead of service_name alone, and adds
# workspace_id to its SELECT/GROUP BY. The rows below use REAL Databricks action names (per the
# vendored .sql's own caveats, sourced from Databricks' audit-log/diagnostic-log-reference docs) so
# the query's own allow-list is what scopes "this builder's own rows" -- system.access.audit is
# shared with access_login_concentration / access_runas_escalation (see the module docstring), but
# neither of those uses a real allow-listed action_name, so nothing here collides with them, and no
# "gv_arce_"-prefix filter is needed on the test side any more.
# ---------------------------------------------------------------------------------------------
def _admin_role_change_rows() -> list[dict]:
    rows = []
    for i in range(5):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="unityCatalog", action_name="updatePermissions",
            user_identity={"email": "gv_arce_ok@example.com", "subject_name": None},
            source_ip_address="10.1.0.1",
        ))
    for i in range(20):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="accountsAccessControl",
            action_name="updateRuleSet",
            user_identity={"email": "gv_arce_warn@example.com", "subject_name": None},
            source_ip_address=f"10.1.0.{2 + (i % 3)}",
        ))
    for i in range(100):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), workspace_id=WS_ACCOUNT, service_name="accounts",
            action_name="setAdmin",
            user_identity={"email": None, "subject_name": None},
            source_ip_address=f"10.1.1.{i % 7}",
            audit_level="ACCOUNT_LEVEL",
        ))
    for i in range(3):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="servingEndpoints",
            action_name="createServingEndpoint",
            user_identity={"email": "gv_arce_excl@example.com", "subject_name": None},
        ))
    # T-73's actual bug, proven directly: an allow-listed SERVICE with a non-allow-listed action --
    # a read (getTable) and a token login (tokenLogin) -- must still be excluded. The old query
    # filtered on service_name alone, so both of these used to count as "admin role changes".
    rows.append(_audit_row(
        D7, service_name="unityCatalog", action_name="getTable",
        user_identity={"email": "gv_arce_read@example.com", "subject_name": None},
    ))
    rows.append(_audit_row(
        D7, service_name="accounts", action_name="tokenLogin",
        user_identity={"email": "gv_arce_login@example.com", "subject_name": None},
    ))
    # Review fix round: the allow-list previously missed account-admin-role and workspace
    # role/permission-assignment actions that the published audit-log reference documents.
    # setAccountAdmin proves category (1)'s account-level admin-role grant is now included
    # (workspace_id stays WS_ACCOUNT/"0", audit_level ACCOUNT_LEVEL, same as the setAdmin CRITICAL
    # group above); updateRoleAssignment proves category (1)'s workspace-level role-assignment
    # action is included too (default workspace_id WS_PROD/"1111").
    rows.append(_audit_row(
        D7, workspace_id=WS_ACCOUNT, service_name="accounts", action_name="setAccountAdmin",
        audit_level="ACCOUNT_LEVEL",
        user_identity={"email": "gv_arce_acctadmin@example.com", "subject_name": None},
    ))
    rows.append(_audit_row(
        D7, service_name="workspace", action_name="updateRoleAssignment",
        user_identity={"email": "gv_arce_role@example.com", "subject_name": None},
    ))
    # A handful of action names read CRITICAL at ANY count, regardless of :warn_admin_events /
    # :crit_admin_events -- one changeAccountOwner event, far below either volume threshold, must
    # still read CRITICAL on its own.
    rows.append(_audit_row(
        D7, workspace_id=WS_ACCOUNT, service_name="accountsManager", action_name="changeAccountOwner",
        audit_level="ACCOUNT_LEVEL",
        user_identity={"email": "gv_arce_owner@example.com", "subject_name": None},
    ))
    # Review fix round: failed_event_count. 2 DENIED attempts (status_code=403) at an
    # allow-listed action (removeAdmin, category (1), distinct from the setAdmin CRITICAL group
    # above so it forms its own small OK-status group) -- proves event_count still counts every
    # attempt (2) while failed_event_count also counts 2, and that the every-row-succeeds default
    # response on every OTHER group above keeps their own failed_event_count at 0.
    for i in range(2):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="accounts", action_name="removeAdmin",
            response={"status_code": 403, "error_message": "denied", "result": None},
            user_identity={"email": "gv_arce_denied@example.com", "subject_name": None},
        ))
    for suffix, ts, action in (
        ("t7", D7, "addPrincipalToGroup"),
        ("t30", D30, "removePrincipalFromGroup"),
        ("t90", D90, "createGroup"),
        ("ttoday", DTODAY, "removeGroup"),
        ("told90", DOLD, "updateGroup"),
    ):
        rows.append(_audit_row(
            ts, service_name="accounts", action_name=action,
            user_identity={"email": "gv_arce_win@example.com", "subject_name": None},
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# access_login_concentration -- warn_failed_logins=5, crit_failed_logins=20; service_name='accounts'.
# ---------------------------------------------------------------------------------------------
def _login_concentration_rows() -> list[dict]:
    rows = []
    for i in range(3):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="accounts", action_name="gv_login_ok_action",
            user_identity={"email": "gv_login_ok@example.com", "subject_name": None},
            source_ip_address="198.51.100.10", response={"status_code": 200, "error_message": None, "result": "success"},
        ))
    for i in range(5):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="accounts", action_name="gv_login_warn_action",
            user_identity={"email": "gv_login_warn@example.com", "subject_name": None},
            source_ip_address="198.51.100.11",
            response={"status_code": 403, "error_message": "forbidden", "result": "failure"},
        ))
    for i in range(20):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="accounts", action_name="gv_login_crit_action",
            user_identity={"email": "__REDACTED__", "subject_name": None},
            source_ip_address="198.51.100.12",
            response={"status_code": 403, "error_message": "forbidden", "result": "failure"},
        ))
    # T-75B: one principal authenticating (failed) from TWO distinct source IPs, under the SAME
    # action_name -- two grain rows (principal+ip+service+action differs only by ip). Both rows'
    # distinct_source_ips must read 2 (the principal's true IP spread across the whole window),
    # never 1 (what COUNT(DISTINCT source_ip_address) reads when it sits inside a GROUP BY that
    # already fixes source_ip_address to a single value per row -- the bug this fix closes).
    for ip in ("198.51.100.30", "198.51.100.31"):
        rows.append(_audit_row(
            D7, service_name="accounts", action_name="gv_login_multiip_action",
            user_identity={"email": "gv_login_multiip@example.com", "subject_name": None},
            source_ip_address=ip,
            response={"status_code": 403, "error_message": "forbidden", "result": "failure"},
        ))
    for suffix, ts in WINDOW_ANCHORS:
        rows.append(_audit_row(
            ts, service_name="accounts", action_name=f"gv_login_win_{suffix}",
            user_identity={"email": "gv_login_win@example.com", "subject_name": None},
            source_ip_address="198.51.100.20",
            response={"status_code": 200, "error_message": None, "result": "success"},
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# access_runas_escalation -- warn_runas_events=5, crit_runas_events=25.
# ---------------------------------------------------------------------------------------------
def _runas_escalation_rows() -> list[dict]:
    rows = []
    # pair_history seed, outside every test window (7/30/90d): makes gv_runas_ok's own
    # (run_by, run_as) pair a KNOWN one, so newly_seen_pair=False -> OK regardless of shape.
    rows.append(_audit_row(
        DOLD, service_name="jobs", action_name="gv_runas_ok_history",
        identity_metadata={
            "run_by": "gv_runas_ok_by@example.com", "run_as": "gv_runas_ok_as_sp",
            "acting_resource": None, "run_by_display_name": None, "run_as_display_name": None,
        },
    ))
    for i in range(2):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="jobs", action_name="gv_runas_ok",
            identity_metadata={
                "run_by": "gv_runas_ok_by@example.com", "run_as": "gv_runas_ok_as_sp",
                "acting_resource": None, "run_by_display_name": None, "run_as_display_name": None,
            },
        ))
    for i in range(5):
        # run_by human-shaped, run_as a 36-char hex GUID -> user_to_service_principal; no history
        # seeded for this pair, so newly_seen_pair=True -> WARN.
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="jobs", action_name="gv_runas_warn",
            identity_metadata={
                "run_by": "gv_runas_warn_by", "run_as": GUID_EXAMPLE_1,
                "acting_resource": None, "run_by_display_name": None, "run_as_display_name": None,
            },
        ))
    for i in range(25):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="pipelines", action_name="gv_runas_crit",
            identity_metadata={
                "run_by": "gv_runas_crit_by", "run_as": "gv_runas_crit_as",
                "acting_resource": None, "run_by_display_name": None, "run_as_display_name": None,
            },
        ))
    rows.append(_audit_row(
        D7, service_name="jobs", action_name="gv_runas_equal",
        identity_metadata={
            "run_by": "gv_runas_equal_both", "run_as": "gv_runas_equal_both",
            "acting_resource": None, "run_by_display_name": None, "run_as_display_name": None,
        },
    ))
    rows.append(_audit_row(
        D7, service_name="jobs", action_name="gv_runas_nullby",
        identity_metadata={
            "run_by": None, "run_as": "gv_runas_nullby_as",
            "acting_resource": None, "run_by_display_name": None, "run_as_display_name": None,
        },
    ))
    for suffix, ts in WINDOW_ANCHORS:
        rows.append(_audit_row(
            ts, service_name="jobs", action_name=f"gv_runas_win_{suffix}",
            identity_metadata={
                "run_by": "gv_runas_win_by", "run_as": "gv_runas_win_as",
                "acting_resource": None, "run_by_display_name": None, "run_as_display_name": None,
            },
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# access_vector_search_traffic -- no status; service_name='vectorSearch', action_name IN the
# 5 query/scan actions; GROUP BY event_date, action_name, request_params['endpoint_name'].
# ---------------------------------------------------------------------------------------------
def _vector_search_traffic_rows() -> list[dict]:
    rows = []
    for i in range(3):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="vectorSearch", action_name="queryVectorIndex",
            request_params={"endpoint_name": "gv_vst_ep1"},
        ))
    for i in range(2):
        rows.append(_audit_row(
            D7 + timedelta(minutes=i), service_name="vectorSearch", action_name="scanVectorIndex",
            request_params={"endpoint_name": "gv_vst_ep2"},
        ))
    rows.append(_audit_row(
        D7, service_name="vectorSearchOther", action_name="queryVectorIndex",
        request_params={"endpoint_name": "gv_vst_wrong_service"},
    ))
    rows.append(_audit_row(
        D7, service_name="vectorSearch", action_name="queryVectorIndexOTHER",
        request_params={"endpoint_name": "gv_vst_wrong_action"},
    ))
    # NOTE (DEC-48): this id's masked `endpoint_name` output column is derived from the RAW
    # request_params['endpoint_name'] AFTER the query's own GROUP BY (which groups on the raw,
    # unmasked value) -- two distinct raw endpoints that happen to share their first 2 characters
    # collapse to the identical masked display value; this is a real property of the vendored
    # query's own design (uniqueness on this grain holds by construction on the fixture, not as a
    # guarantee on real data -- config/grains/governance_events.yml's own header explains this and
    # carries the `# masked:` comment DEC-48 requires). Per DEC-48 rule (3), a masked collision is
    # never worked around by leaving this builder's `gv_` id namespace (DEC-15) -- every id here,
    # including the window-anchor rows below, keeps the `gv_` prefix. Instead, the window rows use
    # `queryVectorIndexNextPage` (one of the 5 action_name values this id's own WHERE accepts, per
    # the header's `action_name IN (...)` list), distinct from `gv_vst_ep1`'s `queryVectorIndex`
    # and `gv_vst_ep2`'s `scanVectorIndex` -- action_name is itself a non-masked grain column, so
    # this separates the window group from both scenario groups cleanly without touching the
    # endpoint naming at all.
    for suffix, ts in WINDOW_ANCHORS:
        rows.append(_audit_row(
            ts, service_name="vectorSearch", action_name="queryVectorIndexNextPage",
            request_params={"endpoint_name": f"gv_vst_win_{suffix}"},
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# access_column_lineage_sensitive_reach -- warn_reach_principals=10, crit_reach_principals=50;
# source_table_full_name IS NOT NULL.
# ---------------------------------------------------------------------------------------------
def _column_lineage_sensitive_reach_rows() -> tuple[list[dict], list[dict]]:
    rows = []
    tag_rows = []

    def _tag(table_name, column_name, tag_name, tag_value):
        tag_rows.append(_mk_row(
            IS_COLUMN_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
            table_name=table_name, column_name=column_name, tag_name=tag_name, tag_value=tag_value,
        ))

    def _edge(src_name, tgt_name, n_principals, ts=D7):
        out = []
        for i in range(n_principals):
            out.append(_column_lineage_row(
                ts + timedelta(minutes=i),
                source_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, src_name),
                source_table_catalog=GV_CATALOG, source_table_schema=GV_SCHEMA,
                source_table_name=src_name, source_column_name="gv_col_a", source_type="TABLE",
                target_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, tgt_name),
                target_table_catalog=GV_CATALOG, target_table_schema=GV_SCHEMA,
                target_table_name=tgt_name, target_column_name="gv_col_b", target_type="TABLE",
                created_by=f"gv_reach_principal_{i}@example.com",
            ))
        return out

    # SOURCE column must carry a sensitivity tag to appear at all -- tag every scenario's source.
    _tag("gv_reach_ok_src", "gv_col_a", "pii", "true")
    _tag("gv_reach_warn_src", "gv_col_a", "pii", "true")
    _tag("gv_reach_crit_src", "gv_col_a", "pii", "true")
    _tag("gv_reach_win_src", "gv_col_a", "pii", "true")

    rows += _edge("gv_reach_ok_src", "gv_reach_ok_tgt", 3)
    rows += _edge("gv_reach_warn_src", "gv_reach_warn_tgt", 10)
    rows += _edge("gv_reach_crit_src", "gv_reach_crit_tgt", 50)
    rows.append(_column_lineage_row(
        D7, source_table_full_name=None, source_table_catalog=None, source_table_schema=None,
        source_table_name=None, source_column_name="gv_col_a",
        target_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, "gv_reach_nullsrc_tgt"),
        target_table_catalog=GV_CATALOG, target_table_schema=GV_SCHEMA,
        target_table_name="gv_reach_nullsrc_tgt", target_column_name="gv_col_b",
        created_by="gv_reach_nullsrc_by",
    ))
    for suffix, ts in WINDOW_ANCHORS:
        rows += _edge("gv_reach_win_src", f"gv_reach_win_{suffix}_tgt", 1, ts=ts)
    return rows, tag_rows


# ---------------------------------------------------------------------------------------------
# access_pii_propagation_untagged -- warn_pii_gap_events=5, crit_pii_gap_events=50;
# direct_access=true, source/target column_name NOT NULL, source_table_catalog<>'system'.
# Also writes the information_schema.column_tags rows this id's own sensitive_tags /
# any_tagged_column CTEs read (reused by F2's access_tags_inventory).
# ---------------------------------------------------------------------------------------------
def _pii_propagation_untagged_rows() -> tuple[list[dict], list[dict]]:
    lineage_rows: list[dict] = []
    tag_rows: list[dict] = []

    def _tag(table_name, column_name, tag_name, tag_value):
        tag_rows.append(_mk_row(
            IS_COLUMN_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
            table_name=table_name, column_name=column_name, tag_name=tag_name, tag_value=tag_value,
        ))

    def _edge(src_name, src_col, tgt_name, tgt_col, n, ts=D7, direct_access=True, catalog=GV_CATALOG):
        out = []
        for i in range(n):
            out.append(_column_lineage_row(
                ts + timedelta(minutes=i),
                source_table_full_name=_full_name(catalog, GV_SCHEMA, src_name),
                source_table_catalog=catalog, source_table_schema=GV_SCHEMA,
                source_table_name=src_name, source_column_name=src_col, source_type="TABLE",
                target_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, tgt_name),
                target_table_catalog=GV_CATALOG, target_table_schema=GV_SCHEMA,
                target_table_name=tgt_name, target_column_name=tgt_col, target_type="TABLE",
                created_by="gv_pii_creator@example.com", direct_access=direct_access,
            ))
        return out

    # match-excluded: source tagged (tag_name branch), target ALSO tagged -> anti-join fails.
    _tag("gv_pii_match_src", "gv_ssn_col", "pii", "true")
    _tag("gv_pii_match_tgt", "gv_ssn_copy", "classification", "confidential")
    lineage_rows += _edge("gv_pii_match_src", "gv_ssn_col", "gv_pii_match_tgt", "gv_ssn_copy", 1)

    # WARN: source tagged via tag_value branch, target untagged.
    _tag("gv_pii_warn_src", "gv_ssn_col2", "owner", "restricted")
    lineage_rows += _edge("gv_pii_warn_src", "gv_ssn_col2", "gv_pii_warn_tgt", "gv_ssn_leak2", 5)

    # CRITICAL: source tagged via tag_name branch, target untagged.
    _tag("gv_pii_crit_src", "gv_ssn_col3", "pii", "true")
    lineage_rows += _edge("gv_pii_crit_src", "gv_ssn_col3", "gv_pii_crit_tgt", "gv_ssn_leak3", 50)

    # excluded-untagged-source: no column_tags row at all for this source -> not in sensitive_tags.
    lineage_rows += _edge("gv_pii_notag_src", "gv_col_x", "gv_pii_notag_tgt", "gv_col_y", 1)

    # excluded-system-catalog.
    _tag("gv_pii_sys_src", "gv_col_z", "pii", "true")
    lineage_rows += _edge("gv_pii_sys_src", "gv_col_z", "gv_pii_sys_tgt", "gv_col_w", 1, catalog="system")

    # excluded-indirect (direct_access=false).
    _tag("gv_pii_indirect_src", "gv_col_v", "pii", "true")
    lineage_rows += _edge(
        "gv_pii_indirect_src", "gv_col_v", "gv_pii_indirect_tgt", "gv_col_u", 1, direct_access=False,
    )

    # window rows: same tagged source, distinct untagged target per anchor.
    _tag("gv_pii_win_src", "gv_win_col", "pii", "true")
    for suffix, ts in WINDOW_ANCHORS:
        lineage_rows += _edge(
            "gv_pii_win_src", "gv_win_col", f"gv_pii_win_{suffix}_tgt", "gv_win_tgt_col", 1, ts=ts,
        )

    return lineage_rows, tag_rows


# ---------------------------------------------------------------------------------------------
# access_dead_table_candidates -- warn_dead_days=90, crit_dead_days=365; joins
# information_schema.tables against access.table_lineage source appearances.
# ---------------------------------------------------------------------------------------------
def _dead_table_candidates_rows() -> tuple[list[dict], list[dict]]:
    tables_rows: list[dict] = []
    lineage_rows: list[dict] = []

    def _tbl(name, table_type, owner, last_altered, catalog=GV_CATALOG, schema=GV_SCHEMA, fmt="DELTA"):
        tables_rows.append(_mk_row(
            IS_TABLES_COLUMNS, table_catalog=catalog, table_schema=schema, table_name=name,
            table_type=table_type, is_insertable_into="YES", commit_action=None,
            table_owner=owner, comment=None,
            created=datetime(2026, 1, 1, 0, 0, 0),
            created_by=owner, last_altered=last_altered, last_altered_by=owner,
            data_source_format=fmt, storage_sub_directory=None, storage_path=None,
        ))

    _tbl("gv_dead_crit", "MANAGED", "__REDACTED__", AS_OF - timedelta(days=400))
    _tbl("gv_dead_warn", "EXTERNAL", "gv_dead_warn_owner@example.com", AS_OF - timedelta(days=120))
    _tbl("gv_dead_ok", "MANAGED", "gv_dead_ok_owner", AS_OF - timedelta(days=10))
    _tbl("gv_dead_notassessed", "MANAGED", GUID_EXAMPLE_2, None)
    _tbl("gv_alive_excluded", "MANAGED", "gv_alive_owner", AS_OF - timedelta(days=500))
    _tbl("gv_sys_excluded", "MANAGED", "gv_sys_owner", AS_OF - timedelta(days=400), catalog="system")
    _tbl("gv_infoschema_excluded", "MANAGED", "gv_is_owner", AS_OF - timedelta(days=400), schema="information_schema")
    _tbl("gv_view_excluded", "VIEW", "gv_view_owner", AS_OF - timedelta(days=400), fmt="DELTA")
    # Window-boundary table: only becomes a "dead" candidate when the lineage-source row below
    # falls OUTSIDE the query's own :period_days window (window_days=7); at window_days=30/90 the
    # D30 lineage row is inside the window, so this table is excluded (alive) at those windows.
    _tbl("gv_dead_win_boundary", "MANAGED", "gv_dead_winb_owner", AS_OF - timedelta(days=400))

    lineage_rows.append(_table_lineage_row(
        D7, source_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, "gv_alive_excluded"),
        source_table_catalog=GV_CATALOG, source_table_schema=GV_SCHEMA,
        source_table_name="gv_alive_excluded", source_type="TABLE",
        target_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, "gv_alive_reader"),
        target_table_catalog=GV_CATALOG, target_table_schema=GV_SCHEMA,
        target_table_name="gv_alive_reader", target_type="TABLE",
        created_by="gv_lineage_writer", direct_access=False,
    ))
    lineage_rows.append(_table_lineage_row(
        D30, source_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, "gv_dead_win_boundary"),
        source_table_catalog=GV_CATALOG, source_table_schema=GV_SCHEMA,
        source_table_name="gv_dead_win_boundary", source_type="TABLE",
        target_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, "gv_dead_winb_reader"),
        target_table_catalog=GV_CATALOG, target_table_schema=GV_SCHEMA,
        target_table_name="gv_dead_winb_reader", target_type="TABLE",
        created_by="gv_lineage_writer", direct_access=True,
    ))
    return tables_rows, lineage_rows


# ---------------------------------------------------------------------------------------------
# access_table_lineage_blast_radius -- warn_blast_principals=10, crit_blast_principals=50.
# ---------------------------------------------------------------------------------------------
def _table_lineage_blast_radius_rows() -> list[dict]:
    rows = []

    def _edge(src_name, tgt_name, n_principals, source_type, target_type, ts=D7):
        out = []
        for i in range(max(n_principals, 1)):
            out.append(_table_lineage_row(
                ts + timedelta(minutes=i),
                source_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, src_name) if source_type else None,
                source_table_catalog=GV_CATALOG if source_type else None,
                source_table_schema=GV_SCHEMA if source_type else None,
                source_table_name=src_name if source_type else None, source_type=source_type,
                target_table_full_name=_full_name(GV_CATALOG, GV_SCHEMA, tgt_name) if target_type else None,
                target_table_catalog=GV_CATALOG if target_type else None,
                target_table_schema=GV_SCHEMA if target_type else None,
                target_table_name=tgt_name if target_type else None, target_type=target_type,
                created_by=f"gv_blast_principal_{i}@example.com",
            ))
        return out

    rows += _edge("gv_blast_read_ok_src", "gv_blast_read_ok_tgt", 3, "TABLE", None)
    rows += _edge("gv_blast_read_warn_src", "gv_blast_read_warn_tgt", 10, "TABLE", None)
    rows += _edge("gv_blast_read_crit_src", "gv_blast_read_crit_tgt", 50, "TABLE", None)
    rows += _edge("gv_blast_write_src", "gv_blast_write_tgt", 1, None, "TABLE")
    rows += _edge("gv_blast_rw_src", "gv_blast_rw_tgt", 1, "TABLE", "TABLE")
    rows += _edge("gv_blast_unknown_src", "gv_blast_unknown_tgt", 1, None, None)
    for suffix, ts in WINDOW_ANCHORS:
        # target_type must be set (not None) here: _edge only populates target_table_full_name
        # when target_type is truthy, and the window test identifies each anchor's row by its own
        # distinct target_table_full_name -- a NULL target_type/target_table_full_name would make
        # every window row structurally unidentifiable (READ_WRITE class, not READ, but the class
        # itself is not the point of these rows).
        rows += _edge("gv_blast_win_src", f"gv_blast_win_{suffix}_tgt", 1, "TABLE", "TABLE", ts=ts)
    return rows


# ---------------------------------------------------------------------------------------------
# access_classified_unmasked (windowless) / access_data_classification_inventory (windowless) --
# both read data_classification.results; access_classified_unmasked also LEFT JOINs
# information_schema.column_masks.
# ---------------------------------------------------------------------------------------------
def _classification_rows() -> tuple[list[dict], list[dict]]:
    dc_rows: list[dict] = []
    mask_rows: list[dict] = []

    # Exactly ONE raw row for gv_masked_tbl.gv_ssn -- access_classified_unmasked has NO GROUP BY
    # (one output row per data_classification.results row, LEFT JOIN never fans out), so its own
    # grain is (catalog_name, schema_name, table_name, column_name) alone; a second raw row
    # sharing that same identifier tuple would duplicate THAT id's grain even though it would
    # correctly dedup under access_data_classification_inventory's own GROUP BY. The dedup /
    # MAX(frequency) / MAX(latest_detected_time) / MIN(first_detected_time) scenario therefore
    # lives on gv_lowconf_tbl below instead (confidence='LOW' keeps it out of
    # access_classified_unmasked's WHERE dc.confidence = 'HIGH' entirely, so a raw duplicate there
    # can never surface as a duplicate grain row on this id).
    dc_rows.append(_dc_row(
        table_name="gv_masked_tbl", column_name="gv_ssn", class_tag="PII", confidence="HIGH",
        frequency="0.90", first_detected_time="2026-08-01 00:00:00",
        latest_detected_time="2026-09-10 00:00:00",
    ))
    mask_rows.append(_mk_row(
        IS_COLUMN_MASKS_COLUMNS,
        table_catalog=GV_CATALOG, table_schema=GV_SCHEMA, table_name="gv_masked_tbl",
        column_name="gv_ssn", mask_name="gv_mask_fn", using_columns="gv_ssn",
    ))

    dc_rows.append(_dc_row(
        table_name="gv_unmasked_tbl", column_name="gv_email", class_tag="PII", confidence="HIGH",
        frequency="0.80", first_detected_time="2026-08-05 00:00:00",
        latest_detected_time="2026-09-12 00:00:00",
    ))

    dc_rows.append(_dc_row(
        table_name="gv_lowconf_tbl", column_name="gv_note", class_tag="OTHER", confidence="LOW",
        frequency="0.30", first_detected_time="2026-08-10 00:00:00",
        latest_detected_time="2026-09-01 00:00:00",
    ))
    # Duplicate group-key row (dedup test for access_data_classification_inventory's GROUP BY /
    # MAX(frequency) / MAX(latest_detected_time) / MIN(first_detected_time)); safe here (see
    # comment above gv_masked_tbl) because confidence='LOW' excludes this table from
    # access_classified_unmasked entirely.
    dc_rows.append(_dc_row(
        table_name="gv_lowconf_tbl", column_name="gv_note", class_tag="OTHER", confidence="LOW",
        frequency="0.45", first_detected_time="2026-07-15 00:00:00",
        latest_detected_time="2026-09-05 00:00:00",
    ))

    dc_rows.append(_dc_row(
        table_name="gv_notag_tbl", column_name="gv_col", class_tag=None, confidence="HIGH",
        frequency="0.10", first_detected_time="2026-08-15 00:00:00",
        latest_detected_time="2026-09-01 00:00:00",
    ))
    return dc_rows, mask_rows


# ---------------------------------------------------------------------------------------------
# access_network_inbound_denials -- warn_denial_count=10, crit_denial_count=50; event_time only,
# no upper bound (DTODAY included, DOLD excluded).
# ---------------------------------------------------------------------------------------------
def _network_inbound_denials_rows() -> list[dict]:
    rows = []
    for i in range(3):
        rows.append(_inbound_row(
            D7 + timedelta(minutes=i), policy_outcome="DENY_DRY_RUN", rule_label="gv_in_ok_rule",
            request_path="/gv/ok", authenticated_as="gv_in_ok@example.com",
            source={"ip": "203.0.113.10"},
        ))
    for i in range(10):
        rows.append(_inbound_row(
            D7 + timedelta(minutes=i), policy_outcome="DENY", rule_label="gv_in_warn_rule",
            request_path="/gv/warn", authenticated_as="gv_in_warn_operator",
            source={"ip": "203.0.113.11"},
        ))
    for i in range(50):
        rows.append(_inbound_row(
            D7 + timedelta(minutes=i), policy_outcome="DENY", rule_label="gv_in_crit_rule",
            request_path="/gv/crit", authenticated_as=None,
            source={"ip": "203.0.113.12"},
        ))
    for suffix, ts in WINDOW_ANCHORS:
        rows.append(_inbound_row(
            ts, policy_outcome="DENY", rule_label=f"gv_in_win_{suffix}_rule",
            request_path="/gv/win", authenticated_as="gv_in_win@example.com",
            source={"ip": "203.0.113.20"},
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# access_network_outbound_denials -- warn_denial_count=10, crit_denial_count=50; event_time only,
# no upper bound.
# ---------------------------------------------------------------------------------------------
def _network_outbound_denials_rows() -> list[dict]:
    rows = []
    for i in range(3):
        rows.append(_outbound_row(
            D7 + timedelta(minutes=i), destination_type="DNS", destination="gv-out-ok.example.com",
            dns_event={"domain_name": "gv-out-ok.example.com", "rcode": 3, "rdata": None},
            storage_event=None,
        ))
    for i in range(10):
        rows.append(_outbound_row(
            D7 + timedelta(minutes=i), destination_type="STORAGE", destination="gv-out-warn-bucket",
            dns_event=None,
            storage_event={"hostname": "gv-out-warn-bucket", "path": "/x", "rejection_reason": "UNAUTHORIZED", "rdata": None},
        ))
    for i in range(50):
        rows.append(_outbound_row(
            D7 + timedelta(minutes=i), destination_type="DNS", destination="gv-out-crit.example.com",
            dns_event={"domain_name": "gv-out-crit.example.com", "rcode": 5, "rdata": None},
            storage_event=None,
        ))
    for suffix, ts in WINDOW_ANCHORS:
        rows.append(_outbound_row(
            ts, destination_type="DNS", destination=f"gv-out-win-{suffix}.example.com",
            dns_event={"domain_name": f"gv-out-win-{suffix}.example.com", "rcode": 0, "rdata": None},
            storage_event=None,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# F2-only support rows: no F1 id reads these tables, but T-21 (which owns no fixture rows) needs
# them populated now. table_privileges / catalog_privileges -> access_grants_inventory;
# schema/connection/credential/external_location_privileges -> access_grants_inventory_extended;
# row_filters -> access_row_filters_inventory; table_tags/schema_tags/volume_tags (+ column_tags
# above) -> access_tags_inventory; views -> access_views_inventory; volumes/volume_tags/
# schema_tags -> access_volumes_inventory + access_pii_outside_tables; shares/
# share_recipient_privileges/table_share_usage/schema_share_usage -> access_delta_sharing_exposure.
# ---------------------------------------------------------------------------------------------
def _f2_support_rows() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {
        "table_privileges": [], "catalog_privileges": [], "schema_privileges": [],
        "connection_privileges": [], "credential_privileges": [],
        "external_location_privileges": [], "row_filters": [], "table_tags": [],
        "schema_tags": [], "volume_tags": [], "views": [], "volumes": [], "shares": [],
        "share_recipient_privileges": [], "table_share_usage": [], "schema_share_usage": [],
    }

    out["table_privileges"] += [
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gv_admin@example.com",
                grantee="gv_grantee1@example.com", table_catalog=GV_CATALOG, table_schema=GV_SCHEMA,
                table_name="gv_tbl1", privilege_type="SELECT", is_grantable="NO", inherited_from=None),
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gv_admin@example.com",
                grantee="gv_grantee1@example.com", table_catalog=GV_CATALOG, table_schema=GV_SCHEMA,
                table_name="gv_tbl2", privilege_type="SELECT", is_grantable="NO", inherited_from=None),
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gv_admin@example.com", grantee=GUID_EXAMPLE_1,
                table_catalog=GV_CATALOG, table_schema=GV_SCHEMA, table_name="gv_tbl1",
                privilege_type="MODIFY", is_grantable="NO", inherited_from=None),
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gv_admin@example.com", grantee="gv_operator",
                table_catalog=GV_CATALOG, table_schema=GV_SCHEMA, table_name="gv_tbl1",
                privilege_type="ALL_PRIVILEGES", is_grantable="NO", inherited_from=None),
    ]
    out["catalog_privileges"].append(_mk_row(
        IS_CATALOG_PRIVILEGES_COLUMNS, grantor="gv_admin@example.com",
        grantee="gv_grantee1@example.com", catalog_name=GV_CATALOG, privilege_type="USE_CATALOG",
        is_grantable="NO",
    ))
    for tname, grantee, priv in [
        ("schema_privileges", "gv_grantee1@example.com", "USE_SCHEMA"),
        ("connection_privileges", GUID_EXAMPLE_2, "USE_CONNECTION"),
        ("credential_privileges", "gv_operator", "READ_FILES"),
        ("external_location_privileges", "gv_grantee2@example.com", "WRITE_FILES"),
    ]:
        cols = {"schema_privileges": IS_SCHEMA_PRIVILEGES_COLUMNS,
                "connection_privileges": IS_CONNECTION_PRIVILEGES_COLUMNS,
                "credential_privileges": IS_CREDENTIAL_PRIVILEGES_COLUMNS,
                "external_location_privileges": IS_EXTERNAL_LOCATION_PRIVILEGES_COLUMNS}[tname]
        out[tname].append(_mk_row(cols, grantee=grantee, object_scope="gv_scope_1", privilege_type=priv))

    out["row_filters"].append(_mk_row(
        IS_ROW_FILTERS_COLUMNS, filter_name="gv_row_filter_fn", table_catalog=GV_CATALOG,
        table_schema=GV_SCHEMA, table_name="gv_filtered_tbl", target_columns="gv_region",
    ))

    out["table_tags"].append(_mk_row(
        IS_TABLE_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
        table_name="gv_tagged_tbl", tag_name="team", tag_value="data-eng",
    ))
    out["schema_tags"] += [
        _mk_row(IS_SCHEMA_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name="gv_pii_schema",
                tag_name="pii", tag_value="true"),
        _mk_row(IS_SCHEMA_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name="gv_plain_schema",
                tag_name="owner", tag_value="team-x"),
    ]

    out["volumes"] += [
        _mk_row(IS_VOLUMES_COLUMNS, volume_catalog=GV_CATALOG, volume_schema=GV_SCHEMA,
                volume_name="gv_vol_ext_untagged", volume_type="EXTERNAL",
                volume_owner="gv_vol_owner@example.com", comment=None,
                storage_location="s3://gv-bucket/ext-untagged", created=AS_OF - timedelta(days=30),
                created_by="gv_vol_owner@example.com", last_altered=AS_OF - timedelta(days=5),
                last_altered_by="gv_vol_owner@example.com"),
        _mk_row(IS_VOLUMES_COLUMNS, volume_catalog=GV_CATALOG, volume_schema=GV_SCHEMA,
                volume_name="gv_vol_mgd_untagged", volume_type="MANAGED",
                volume_owner="gv_vol_owner2", comment=None,
                storage_location="/gv/mgd-untagged", created=AS_OF - timedelta(days=30),
                created_by="gv_vol_owner2", last_altered=AS_OF - timedelta(days=5),
                last_altered_by="gv_vol_owner2"),
        _mk_row(IS_VOLUMES_COLUMNS, volume_catalog=GV_CATALOG, volume_schema=GV_SCHEMA,
                volume_name="gv_vol_ext_sensitive", volume_type="EXTERNAL",
                volume_owner=GUID_EXAMPLE_1, comment=None,
                storage_location="s3://gv-bucket/ext-sensitive", created=AS_OF - timedelta(days=30),
                created_by=GUID_EXAMPLE_1, last_altered=AS_OF - timedelta(days=5),
                last_altered_by=GUID_EXAMPLE_1),
        _mk_row(IS_VOLUMES_COLUMNS, volume_catalog=GV_CATALOG, volume_schema=GV_SCHEMA,
                volume_name="gv_vol_mgd_sensitive", volume_type="MANAGED",
                volume_owner="gv_vol_owner3@example.com", comment=None,
                storage_location="/gv/mgd-sensitive", created=AS_OF - timedelta(days=30),
                created_by="gv_vol_owner3@example.com", last_altered=AS_OF - timedelta(days=5),
                last_altered_by="gv_vol_owner3@example.com"),
        _mk_row(IS_VOLUMES_COLUMNS, volume_catalog=GV_CATALOG, volume_schema=GV_SCHEMA,
                volume_name="gv_vol_mgd_tagged_nonsensitive", volume_type="MANAGED",
                volume_owner="gv_vol_owner4", comment=None,
                storage_location="/gv/mgd-nonsensitive", created=AS_OF - timedelta(days=30),
                created_by="gv_vol_owner4", last_altered=AS_OF - timedelta(days=5),
                last_altered_by="gv_vol_owner4"),
    ]
    out["volume_tags"] += [
        _mk_row(IS_VOLUME_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
                volume_name="gv_vol_ext_sensitive", tag_name="pii", tag_value="true"),
        _mk_row(IS_VOLUME_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
                volume_name="gv_vol_mgd_sensitive", tag_name="sensitivity", tag_value="high"),
        _mk_row(IS_VOLUME_TAGS_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
                volume_name="gv_vol_mgd_tagged_nonsensitive", tag_name="team", tag_value="data-eng"),
    ]

    out["views"] += [
        _mk_row(IS_VIEWS_COLUMNS, table_catalog=GV_CATALOG, table_schema=GV_SCHEMA,
                table_name="gv_view_plain",
                view_definition="SELECT gv_ssn FROM gv_catalog.gv_schema.gv_masked_tbl",
                check_option="NONE", is_updatable="NO", is_insertable_into="NO",
                sql_path=None, is_materialized="NO"),
        _mk_row(IS_VIEWS_COLUMNS, table_catalog=GV_CATALOG, table_schema=GV_SCHEMA,
                table_name="gv_view_masked",
                view_definition="SELECT MASK(gv_ssn) FROM gv_catalog.gv_schema.gv_masked_tbl",
                check_option="NONE", is_updatable="NO", is_insertable_into="NO",
                sql_path=None, is_materialized="NO"),
        _mk_row(IS_VIEWS_COLUMNS, table_catalog=GV_CATALOG, table_schema=GV_SCHEMA,
                table_name="gv_view_matview", view_definition="SELECT * FROM gv_catalog.gv_schema.gv_tbl1",
                check_option="NONE", is_updatable="NO", is_insertable_into="NO",
                sql_path=None, is_materialized="YES"),
    ]

    out["shares"] += [
        _mk_row(IS_SHARES_COLUMNS, share_name="gv_share_warn", share_owner="gv_share_owner@example.com",
                comment=None, created=AS_OF - timedelta(days=60), created_by="gv_share_owner@example.com",
                last_altered=AS_OF - timedelta(days=1), last_altered_by="gv_share_owner@example.com"),
        _mk_row(IS_SHARES_COLUMNS, share_name="gv_share_ok", share_owner="gv_share_owner2",
                comment=None, created=AS_OF - timedelta(days=60), created_by="gv_share_owner2",
                last_altered=AS_OF - timedelta(days=1), last_altered_by="gv_share_owner2"),
        _mk_row(IS_SHARES_COLUMNS, share_name="gv_share_crit", share_owner=GUID_EXAMPLE_1,
                comment=None, created=AS_OF - timedelta(days=60), created_by=GUID_EXAMPLE_1,
                last_altered=AS_OF - timedelta(days=1), last_altered_by=GUID_EXAMPLE_1),
    ]
    for i in range(2):
        out["share_recipient_privileges"].append(_mk_row(
            IS_SHARE_RECIPIENT_PRIVILEGES_COLUMNS, grantor="gv_share_owner@example.com",
            recipient_name=f"gv_recipient_warn_{i}@partner.example.com", share_name="gv_share_warn",
            privilege_type="SELECT",
        ))
    # Raw duplicate of gv_recipient_warn_0's own grant row (same grantor, recipient, share,
    # privilege_type) -- under DEC-66.3 every OTHER recipient here is now genuinely distinct, so
    # this is the suite's only remaining proof that array_join(collect_set(...)) still
    # deduplicates: 3 raw rows collapse to 2 masked entries.
    out["share_recipient_privileges"].append(_mk_row(
        IS_SHARE_RECIPIENT_PRIVILEGES_COLUMNS, grantor="gv_share_owner@example.com",
        recipient_name="gv_recipient_warn_0@partner.example.com", share_name="gv_share_warn",
        privilege_type="SELECT",
    ))
    for i in range(10):
        out["share_recipient_privileges"].append(_mk_row(
            IS_SHARE_RECIPIENT_PRIVILEGES_COLUMNS, grantor=GUID_EXAMPLE_1,
            recipient_name=f"gv_recipient_crit_{i}", share_name="gv_share_crit",
            privilege_type="SELECT",
        ))
    out["table_share_usage"] += [
        _mk_row(IS_TABLE_SHARE_USAGE_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
                table_name="gv_tbl1", share_name="gv_share_warn", partition_spec=None,
                cdf_enabled="false", start_version=1, shared_as_schema=GV_SCHEMA,
                shared_as_table="gv_tbl1", comment=None),
        _mk_row(IS_TABLE_SHARE_USAGE_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
                table_name="gv_tbl2", share_name="gv_share_warn", partition_spec=None,
                cdf_enabled="false", start_version=1, shared_as_schema=GV_SCHEMA,
                shared_as_table="gv_tbl2", comment=None),
    ]
    out["schema_share_usage"].append(_mk_row(
        IS_SCHEMA_SHARE_USAGE_COLUMNS, catalog_name=GV_CATALOG, schema_name=GV_SCHEMA,
        share_name="gv_share_warn", shared_as_schema=GV_SCHEMA, comment=None,
    ))
    return out


# ---------------------------------------------------------------------------------------------
# Generic named-column insert helper.
# ---------------------------------------------------------------------------------------------
def _insert(con, table: str, columns: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f'INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})',
        [[r[c] for c in columns] for r in rows],
    )


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17).
# ---------------------------------------------------------------------------------------------
def build(con) -> None:
    audit_rows = (
        _admin_role_change_rows()
        + _login_concentration_rows()
        + _runas_escalation_rows()
        + _vector_search_traffic_rows()
    )
    seen = set()
    for r in audit_rows:
        eid = r["event_id"]
        if eid in seen:
            raise ValueError(f"duplicate event_id in governance.py access__audit rows: {eid!r}")
        seen.add(eid)
    _insert(con, "access__audit", AUDIT_COLUMNS, audit_rows)

    reach_lineage, reach_tags = _column_lineage_sensitive_reach_rows()
    pii_lineage, pii_tags = _pii_propagation_untagged_rows()
    _insert(con, "access__column_lineage", COLUMN_LINEAGE_COLUMNS, reach_lineage + pii_lineage)

    dead_tables, dead_lineage = _dead_table_candidates_rows()
    blast_lineage = _table_lineage_blast_radius_rows()
    _insert(con, "access__table_lineage", TABLE_LINEAGE_COLUMNS, dead_lineage + blast_lineage)
    _insert(con, "information_schema__tables", IS_TABLES_COLUMNS, dead_tables)

    _insert(con, "access__inbound_network", INBOUND_COLUMNS, _network_inbound_denials_rows())
    _insert(con, "access__outbound_network", OUTBOUND_COLUMNS, _network_outbound_denials_rows())

    dc_rows, mask_rows = _classification_rows()
    _insert(con, "data_classification__results", DC_COLUMNS, dc_rows)
    _insert(con, "information_schema__column_masks", IS_COLUMN_MASKS_COLUMNS, mask_rows)
    _insert(con, "information_schema__column_tags", IS_COLUMN_TAGS_COLUMNS, pii_tags + reach_tags)

    f2 = _f2_support_rows()
    _insert(con, "information_schema__table_privileges", IS_TABLE_PRIVILEGES_COLUMNS, f2["table_privileges"])
    _insert(con, "information_schema__catalog_privileges", IS_CATALOG_PRIVILEGES_COLUMNS, f2["catalog_privileges"])
    _insert(con, "information_schema__schema_privileges", IS_SCHEMA_PRIVILEGES_COLUMNS, f2["schema_privileges"])
    _insert(con, "information_schema__connection_privileges", IS_CONNECTION_PRIVILEGES_COLUMNS, f2["connection_privileges"])
    _insert(con, "information_schema__credential_privileges", IS_CREDENTIAL_PRIVILEGES_COLUMNS, f2["credential_privileges"])
    _insert(con, "information_schema__external_location_privileges", IS_EXTERNAL_LOCATION_PRIVILEGES_COLUMNS, f2["external_location_privileges"])
    _insert(con, "information_schema__row_filters", IS_ROW_FILTERS_COLUMNS, f2["row_filters"])
    _insert(con, "information_schema__table_tags", IS_TABLE_TAGS_COLUMNS, f2["table_tags"])
    _insert(con, "information_schema__schema_tags", IS_SCHEMA_TAGS_COLUMNS, f2["schema_tags"])
    _insert(con, "information_schema__volume_tags", IS_VOLUME_TAGS_COLUMNS, f2["volume_tags"])
    _insert(con, "information_schema__views", IS_VIEWS_COLUMNS, f2["views"])
    _insert(con, "information_schema__volumes", IS_VOLUMES_COLUMNS, f2["volumes"])
    _insert(con, "information_schema__shares", IS_SHARES_COLUMNS, f2["shares"])
    _insert(con, "information_schema__share_recipient_privileges", IS_SHARE_RECIPIENT_PRIVILEGES_COLUMNS, f2["share_recipient_privileges"])
    _insert(con, "information_schema__table_share_usage", IS_TABLE_SHARE_USAGE_COLUMNS, f2["table_share_usage"])
    _insert(con, "information_schema__schema_share_usage", IS_SCHEMA_SHARE_USAGE_COLUMNS, f2["schema_share_usage"])
