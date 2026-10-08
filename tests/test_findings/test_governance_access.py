"""tests/test_findings/test_governance_access.py -- T-20's 12 F1 governance_access ids (the
queries that read system.access.audit directly, plus the lineage and network deny-event queries).

Reads findings.f_<query_id> through tests/dbutil.py, per tests/test_findings/README.md's
checklist: grain uniqueness, status enum + worst-first ordering, at least one WARN and one
CRITICAL by id (where the query's own CASE can produce one), window inclusion/exclusion at 7 vs
30 vs 90 days for the 10 windowed ids, window_days=0 only for the 2 snapshot ids
(access_classified_unmasked, access_data_classification_inventory), the account-level
workspace_id="0" access.audit row surviving into access_admin_role_change_events, and all three
identity-mask branches (NULL/'__REDACTED__' passthrough, GUID passthrough, else DEC-66.3's
"<id> <first-2-chars>***" -- hash-derived id here, since none of these tables carries a real
per-row user id) exercised somewhere across this file.

Ordering is checked with `_assert_ordered_by(rows, field)`, NOT by assuming a fixed
CRITICAL/WARN/NOT_ASSESSED/OK status rank: `field` is each id's own `meta.order_by` primary sort
column, read from the regenerated
dbt/models/findings/governance_access/_findings__governance_access.yml (e.g. `event_count DESC`
for access_admin_role_change_events, `days_since_altered DESC NULLS LAST` for
access_dead_table_candidates -- see that helper's own docstring for why the naive status-rank
check is wrong specifically for that last id).

The fixture (tests/fixtures/governance.py) shares its source tables -- access.audit,
table_lineage, column_lineage, inbound_network, outbound_network -- with no other builder in this
repo today, so no cross-builder collision is possible; every test below still filters the raw rows
dbutil.rows() returns down to this builder's own gv_-prefixed ids before asserting, per DEC-15
("assertions filter on the builder's own ids, never on workspace alone") -- access_admin_role_change_
events is the one exception (T-73 upstream fix): its rows use real Databricks action_name values,
not a "gv_arce_"-prefixed one, so that test scopes to its own rows through the query's own
allow-list instead (see that test's comment). Of these 12 query bodies,
access_admin_role_change_events (T-73), access_table_lineage_blast_radius,
access_column_lineage_sensitive_reach and access_network_outbound_denials select workspace_id in
their own output; the fixture writes every row at the same single workspace (WS_PROD), so adding
workspace_id to a GROUP BY never splits an existing scenario's row count here -- dbutil.rows() is
still never called with workspace_ids (there is no such filter param on these ids).

Expected values below are computed BY CONSTRUCTION from tests/fixtures/governance.py's own row
counts / literal field values (e.g. "the WARN group has exactly 20 rows because
access_admin_role_change_events.sql's :warn_admin_events default is 20 and the fixture's docstring
predicate map says so"), never a hard-coded aggregate derived by guessing.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbutil

AS_OF = datetime(2026, 9, 21, 12, 0, 0)
D7 = AS_OF - timedelta(days=3)
D30 = AS_OF - timedelta(days=16)
D90 = AS_OF - timedelta(days=68)
DTODAY = datetime(2026, 9, 21, 8, 0, 0)
DOLD = AS_OF - timedelta(days=143)

# The 10 windowed F1 ids (:period_days present) -- everything in this batch except the 2
# windowless snapshot ids (access_classified_unmasked, access_data_classification_inventory,
# whose own rows live AT window_days=0, never empty there -- see those two tests below).
WINDOWED_IDS = [
    "access_admin_role_change_events",
    "access_login_concentration",
    "access_runas_escalation",
    "access_vector_search_traffic",
    "access_column_lineage_sensitive_reach",
    "access_pii_propagation_untagged",
    "access_dead_table_candidates",
    "access_table_lineage_blast_radius",
    "access_network_inbound_denials",
    "access_network_outbound_denials",
]

STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}


def _assert_status_enum(rows: list[dict]) -> None:
    bad = {r["status"] for r in rows} - STATUS_VALUES
    assert not bad, f"status values outside the enum: {bad}"


def _assert_ordered_by(rows: list[dict], field: str) -> None:
    """`rows`, as returned by dbutil.rows() (a plain `SELECT * ... WHERE window_days = ?`, no
    ORDER BY of its own), must already be in the model's own `order_by` (read from the regenerated
    dbt/models/findings/governance_access/_findings__governance_access.yml meta for each id, not
    guessed): sorted by `field` DESC, NULLs last. This recomputes the expected order independently
    (a stable sort on a COPY of the raw field values) rather than assuming a fixed
    CRITICAL/WARN/NOT_ASSESSED/OK status rank -- several of this batch's own order_by clauses do
    NOT coincide with that rank. access_dead_table_candidates in particular orders by
    `days_since_altered DESC NULLS LAST`, which places NOT_ASSESSED (NULL days_since_altered)
    AFTER OK (a small positive days_since_altered), not between WARN and OK as the
    CRITICAL/WARN/NOT_ASSESSED/OK status-rank convention would assume -- asserting against that
    convention here would be wrong for that one id. Booleans sort fine under this same DESC/NULLs-
    last rule (True=1 > False=0), used for access_classified_unmasked's `is_unmasked DESC`.
    """
    actual = [r[field] for r in rows]
    expected = sorted(actual, key=lambda v: (v is None, -v if v is not None else 0))
    assert actual == expected, f"not ordered by {field!r} DESC NULLS LAST: {actual}"


def _assert_grain_unique(rows: list[dict], key_fields: list[str]) -> None:
    keys = [tuple(r[f] for f in key_fields) for r in rows]
    assert len(keys) == len(set(keys)), f"duplicate grain keys: {[k for k in keys if keys.count(k) > 1]}"


# ---------------------------------------------------------------------------------------------
# access_admin_role_change_events -- warn_admin_events=20, crit_admin_events=100. T-73 upstream
# fix: the query now allow-lists specific (service_name, action_name) pairs (state-changing
# admin/permission/security-configuration actions only, never a read/getX/login) and carries
# workspace_id. Since only this builder's rows use a real allow-listed action_name (see
# tests/fixtures/governance.py's comment above _admin_role_change_rows), every row this query
# returns at window=30 IS this fixture's own -- no "gv_arce_"-prefix filter needed any more.
# ---------------------------------------------------------------------------------------------
def test_access_admin_role_change_events():
    out = dbutil.rows("access_admin_role_change_events", 30)
    _assert_grain_unique(out, ["workspace_id", "service_name", "action_name", "actor"])
    _assert_status_enum(out)
    _assert_ordered_by(out, "event_count")

    by_action = {r["action_name"]: r for r in out}
    for excluded in ("createServingEndpoint", "getTable", "tokenLogin"):
        assert excluded not in by_action, f"{excluded}: must never appear (wrong service, or a read/login, not a change)"

    ok = by_action["updatePermissions"]
    assert ok["event_count"] == 5 and ok["status"] == "OK" and ok["distinct_source_ips"] == 1
    assert ok["workspace_id"] == "1111", "workspace-level event keeps its real workspace_id (WS_PROD in tests/fixtures/governance.py)"
    assert ok["failed_event_count"] == 0, "default fixture response is status 200 -- no denied attempts here"

    warn = by_action["updateRuleSet"]
    assert warn["event_count"] == 20 and warn["status"] == "WARN" and warn["distinct_source_ips"] == 3
    assert warn["workspace_id"] == "1111"
    assert warn["failed_event_count"] == 0

    crit = by_action["setAdmin"]
    assert crit["event_count"] == 100 and crit["status"] == "CRITICAL"
    assert crit["distinct_source_ips"] == 7
    assert crit["actor"] is None, "NULL identity -> mask branch 1 passthrough"
    assert crit["workspace_id"] == "0", "account-level event keeps workspace_id='0'"
    assert crit["failed_event_count"] == 0

    # Review fix round: the allow-list gained account-admin-role (accounts/setAccountAdmin) and
    # workspace role-assignment (workspace/updateRoleAssignment) actions. setAccountAdmin is also
    # one of the always-CRITICAL action names (below), so it reads CRITICAL despite its low count.
    acctadmin = by_action["setAccountAdmin"]
    assert acctadmin["event_count"] == 1 and acctadmin["status"] == "CRITICAL"
    assert acctadmin["workspace_id"] == "0", "account-level admin-role grant keeps workspace_id='0'"

    role_assign = by_action["updateRoleAssignment"]
    assert role_assign["event_count"] == 1 and role_assign["status"] == "OK"
    assert role_assign["workspace_id"] == "1111"

    # Review fix round: failed_event_count. event_count still counts every attempt at an
    # allow-listed action regardless of outcome; failed_event_count is how many of those were
    # denied/non-200 (2 status_code=403 removeAdmin attempts here). removeAdmin is also one of the
    # always-CRITICAL action names, so status is CRITICAL despite the low count -- proving the
    # action-type override, not the volume band, decided it.
    denied = by_action["removeAdmin"]
    assert denied["event_count"] == 2 and denied["failed_event_count"] == 2 and denied["status"] == "CRITICAL"

    # changeAccountOwner: one event, far below either volume threshold, still reads CRITICAL --
    # the action-type override applies at any count.
    owner = by_action["changeAccountOwner"]
    assert owner["event_count"] == 1 and owner["status"] == "CRITICAL"
    assert owner["workspace_id"] == "0"

    # window: presence/absence at 7 / 30 / 90, restricted to this scenario's 5 candidate action
    # names (ttoday/told90 are allow-listed actions too -- only the event_date predicate excludes
    # them, proving the window boundary is independent of the new action_name allow-list).
    window_actions = {"addPrincipalToGroup", "removePrincipalFromGroup", "createGroup", "removeGroup", "updateGroup"}
    expect = {
        7: {"addPrincipalToGroup"},
        30: {"addPrincipalToGroup", "removePrincipalFromGroup"},
        90: {"addPrincipalToGroup", "removePrincipalFromGroup", "createGroup"},
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_admin_role_change_events", wd)
        seen = {r["action_name"] for r in raw_w} & window_actions
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


# ---------------------------------------------------------------------------------------------
# access_login_concentration -- warn_failed_logins=5, crit_failed_logins=20.
# ---------------------------------------------------------------------------------------------
def test_access_login_concentration():
    raw = dbutil.rows("access_login_concentration", 30)
    out = [r for r in raw if r["action_name"] and r["action_name"].startswith("gv_login_")]
    _assert_grain_unique(out, ["principal", "source_ip_address", "service_name", "action_name"])
    _assert_status_enum(out)
    _assert_ordered_by(out, "non_success_count")

    by_action = {r["action_name"]: r for r in out}
    ok = by_action["gv_login_ok_action"]
    assert ok["event_count"] == 3 and ok["success_count"] == 3 and ok["non_success_count"] == 0
    assert ok["status"] == "OK"

    warn = by_action["gv_login_warn_action"]
    assert warn["non_success_count"] == 5 and warn["status"] == "WARN"

    crit = by_action["gv_login_crit_action"]
    assert crit["non_success_count"] == 20 and crit["status"] == "CRITICAL"
    assert crit["principal"] == "__REDACTED__", "'__REDACTED__' -> mask branch 1 passthrough"

    # T-75B: distinct_source_ips is now the principal's TRUE ip spread across the whole window,
    # not COUNT(DISTINCT source_ip_address) scoped to a group already fixed on one ip (which could
    # only ever read 0 or 1). Both of this principal's rows (one per ip, same action) must read 2.
    multiip = [r for r in out if r["action_name"] == "gv_login_multiip_action"]
    assert len(multiip) == 2, multiip
    assert {r["source_ip_address"] for r in multiip} == {"198.51.100.30", "198.51.100.31"}
    assert all(r["distinct_source_ips"] == 2 for r in multiip), multiip
    # sanity: a single-ip principal (e.g. gv_login_warn_action) still reads 1, not the fleet total.
    assert warn["distinct_source_ips"] == 1

    expect = {
        7: {"gv_login_win_t7"},
        30: {"gv_login_win_t7", "gv_login_win_t30"},
        90: {"gv_login_win_t7", "gv_login_win_t30", "gv_login_win_t90"},
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_login_concentration", wd)
        seen = {r["action_name"] for r in raw_w if r["action_name"] and r["action_name"].startswith("gv_login_win_")}
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


# ---------------------------------------------------------------------------------------------
# access_runas_escalation -- warn_runas_events=5, crit_runas_events=25.
# ---------------------------------------------------------------------------------------------
def test_access_runas_escalation():
    raw = dbutil.rows("access_runas_escalation", 30)
    out = [r for r in raw if r["action_name"] and r["action_name"].startswith("gv_runas_")]
    _assert_grain_unique(out, ["service_name", "action_name", "run_by", "run_as"])
    _assert_status_enum(out)
    # order_by is status rank first, event_count DESC second (not event_count alone).
    rank = {"CRITICAL": 0, "WARN": 1, "OK": 2}
    actual = [(r["status"], r["event_count"]) for r in out]
    expected = sorted(actual, key=lambda sv: (rank[sv[0]], -sv[1]))
    assert actual == expected, actual

    by_action = {r["action_name"]: r for r in out}
    assert "gv_runas_equal" not in by_action, "run_by == run_as must be excluded"
    assert "gv_runas_nullby" not in by_action, "run_by NULL must be excluded"

    # known pair (seen before the window) -> OK regardless of shape.
    ok = by_action["gv_runas_ok"]
    assert ok["event_count"] == 2 and ok["status"] == "OK"

    # newly seen, user_to_service_principal (run_as is the GUID) -> WARN.
    warn = by_action["gv_runas_warn"]
    assert warn["event_count"] == 5 and warn["status"] == "WARN"
    assert warn["run_as"] == "11112222-3333-4444-5555-666677778888", "36-char hex GUID -> mask branch 3 passthrough"

    # newly seen, user_to_user (both sides human-shaped) -> CRITICAL.
    crit = by_action["gv_runas_crit"]
    assert crit["event_count"] == 25 and crit["status"] == "CRITICAL"

    expect = {
        7: {"gv_runas_win_t7"},
        30: {"gv_runas_win_t7", "gv_runas_win_t30"},
        90: {"gv_runas_win_t7", "gv_runas_win_t30", "gv_runas_win_t90"},
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_runas_escalation", wd)
        seen = {r["action_name"] for r in raw_w if r["action_name"] and r["action_name"].startswith("gv_runas_win_")}
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


# ---------------------------------------------------------------------------------------------
# access_vector_search_traffic -- no status (inventory); period_days only.
# ---------------------------------------------------------------------------------------------
def test_access_vector_search_traffic():
    raw = dbutil.rows("access_vector_search_traffic", 30)
    out = [
        r for r in raw
        if r["action_name"] in ("queryVectorIndex", "scanVectorIndex") and r["event_date"] == D7.date()
    ]
    _assert_grain_unique(out, ["event_date", "action_name", "endpoint_name"])
    assert "status" not in out[0], "access_vector_search_traffic has no status column"

    # gv_vst_ep1/ep2 (action_name queryVectorIndex/scanVectorIndex) and the window-anchor rows
    # (action_name queryVectorIndexNextPage, see tests/fixtures/governance.py's DEC-48 note) never
    # share an action_name, so `out`'s own WHERE (action_name IN (queryVectorIndex,
    # scanVectorIndex)) already excludes every window row -- no separate scoping needed here.
    by_action = {r["action_name"]: r for r in out}
    assert by_action["queryVectorIndex"]["event_count"] == 3
    assert by_action["scanVectorIndex"]["event_count"] == 2

    expect = {
        7: {D7.date()},
        30: {D7.date(), D30.date()},
        90: {D7.date(), D30.date(), D90.date()},
    }
    for wd, expected_dates in expect.items():
        raw_w = dbutil.rows("access_vector_search_traffic", wd)
        seen = {
            r["event_date"] for r in raw_w
            if r["action_name"] == "queryVectorIndexNextPage"
            and r["event_date"] in {D7.date(), D30.date(), D90.date(), DTODAY.date(), DOLD.date()}
        }
        assert seen == expected_dates, f"window={wd}: {seen} != {expected_dates}"


# ---------------------------------------------------------------------------------------------
# access_column_lineage_sensitive_reach -- warn_reach_principals=10, crit_reach_principals=50.
# ---------------------------------------------------------------------------------------------
def test_access_column_lineage_sensitive_reach():
    raw = dbutil.rows("access_column_lineage_sensitive_reach", 30)
    out = [
        r for r in raw
        if r["source_table_full_name"] and r["source_table_full_name"].startswith("gv_catalog.gv_schema.gv_reach_")
    ]
    _assert_grain_unique(
        out,
        ["workspace_id", "source_table_full_name", "source_column_name", "target_table_full_name", "target_column_name", "entity_type", "direct_access"],
    )
    _assert_status_enum(out)
    _assert_ordered_by(out, "distinct_principals")

    by_tgt = {r["target_table_full_name"].rsplit(".", 1)[-1]: r for r in out}
    ok = by_tgt["gv_reach_ok_tgt"]
    assert ok["distinct_principals"] == 3 and ok["status"] == "OK"
    warn = by_tgt["gv_reach_warn_tgt"]
    assert warn["distinct_principals"] == 10 and warn["status"] == "WARN"
    crit = by_tgt["gv_reach_crit_tgt"]
    assert crit["distinct_principals"] == 50 and crit["status"] == "CRITICAL"
    assert "gv_reach_nullsrc_tgt" not in by_tgt, "NULL source_table_full_name must be excluded"

    expect = {
        7: {"gv_reach_win_t7_tgt"},
        30: {"gv_reach_win_t7_tgt", "gv_reach_win_t30_tgt"},
        90: {"gv_reach_win_t7_tgt", "gv_reach_win_t30_tgt", "gv_reach_win_t90_tgt"},
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_column_lineage_sensitive_reach", wd)
        seen = {
            r["target_table_full_name"].rsplit(".", 1)[-1] for r in raw_w
            if r["target_table_full_name"] and "gv_reach_win_" in r["target_table_full_name"]
        }
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


# ---------------------------------------------------------------------------------------------
# access_pii_propagation_untagged -- warn_pii_gap_events=5, crit_pii_gap_events=50.
# ---------------------------------------------------------------------------------------------
def test_access_pii_propagation_untagged():
    raw = dbutil.rows("access_pii_propagation_untagged", 30)
    out = [r for r in raw if r["source_table_name"] and r["source_table_name"].startswith("gv_pii_")]
    _assert_grain_unique(
        out,
        [
            "workspace_id", "source_table_catalog", "source_table_schema", "source_table_name",
            "source_column_name", "target_table_catalog", "target_table_schema",
            "target_table_name", "target_column_name",
        ],
    )
    _assert_status_enum(out)
    _assert_ordered_by(out, "event_count")

    by_src = {r["source_table_name"]: r for r in out}
    assert "gv_pii_match_src" not in by_src, "target already tagged -> not a gap, must be excluded"
    assert "gv_pii_notag_src" not in by_src, "source never tagged -> excluded (INNER JOIN)"
    assert "gv_pii_sys_src" not in by_src, "source_table_catalog='system' -> excluded"
    assert "gv_pii_indirect_src" not in by_src, "direct_access=false -> excluded"

    warn = by_src["gv_pii_warn_src"]
    assert warn["event_count"] == 5 and warn["status"] == "WARN"
    assert warn["source_tags"] == "owner=restricted", "tag matched via the tag_value branch, not tag_name"
    assert warn["distinct_creators"] == 1

    crit = by_src["gv_pii_crit_src"]
    assert crit["event_count"] == 50 and crit["status"] == "CRITICAL"
    assert crit["source_tags"] == "pii=true"

    expect = {
        7: {"gv_pii_win_t7_tgt"},
        30: {"gv_pii_win_t7_tgt", "gv_pii_win_t30_tgt"},
        90: {"gv_pii_win_t7_tgt", "gv_pii_win_t30_tgt", "gv_pii_win_t90_tgt"},
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_pii_propagation_untagged", wd)
        seen = {
            r["target_table_name"] for r in raw_w
            if r["target_table_name"] and "gv_pii_win_" in r["target_table_name"]
        }
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


def test_access_pii_propagation_untagged_does_not_fan_out_on_a_multi_tag_source():
    """A source column carrying TWO matching sensitivity tags (tests/fixtures/cases_governance.py)
    must still produce exactly one row per edge -- source_tags collapses both tags into one
    string, and event_count/distinct_creators read the real lineage-event/identity counts, never
    doubled by the tag count."""
    raw = dbutil.rows("access_pii_propagation_untagged", 30)
    b1 = [r for r in raw if r["source_table_name"] == "b1_pii_multitag_src"]
    assert len(b1) == 1, f"expected exactly one row for a 2-tag source, got {len(b1)}"
    row = b1[0]
    assert row["source_tags"] == "phi=true, sensitive=true"
    assert row["event_count"] == 3
    assert row["distinct_creators"] == 2
    assert row["workspace_id"] == "7701"


# ---------------------------------------------------------------------------------------------
# access_dead_table_candidates -- warn_dead_days=90, crit_dead_days=365.
# ---------------------------------------------------------------------------------------------
def test_access_dead_table_candidates():
    raw = dbutil.rows("access_dead_table_candidates", 30)
    out = [
        r for r in raw
        if r["table_name"] and (r["table_name"].startswith("gv_dead_") or r["table_name"] == "gv_alive_excluded")
    ]
    _assert_grain_unique(out, ["table_catalog", "table_schema", "table_name"])
    _assert_status_enum(out)
    _assert_ordered_by(out, "days_since_altered")

    by_name = {r["table_name"]: r for r in out}
    assert "gv_alive_excluded" not in by_name, "appears as a lineage source (even indirect) -> excluded"
    assert "gv_sys_excluded" not in by_name
    assert "gv_infoschema_excluded" not in by_name
    assert "gv_view_excluded" not in by_name

    crit = by_name["gv_dead_crit"]
    assert crit["days_since_altered"] == 400 and crit["status"] == "CRITICAL"
    assert crit["table_owner"] == "__REDACTED__", "'__REDACTED__' -> mask branch 1 passthrough"

    warn = by_name["gv_dead_warn"]
    assert warn["days_since_altered"] == 120 and warn["status"] == "WARN"
    assert warn["table_owner"] == "gv_dead_warn_owner@example.com", "mask_user() is off by default"

    ok = by_name["gv_dead_ok"]
    assert ok["days_since_altered"] == 10 and ok["status"] == "OK"
    assert ok["table_owner"] == "gv_dead_ok_owner", "mask_user() is off by default"

    notassessed = by_name["gv_dead_notassessed"]
    assert notassessed["days_since_altered"] is None and notassessed["status"] == "NOT_ASSESSED"
    assert notassessed["table_owner"] == "99998888-7777-6666-5555-444433332222", (
        "36-char hex GUID -> mask branch 3 passthrough"
    )

    # window: gv_dead_win_boundary's own lineage-source row sits at D30 -- outside the 7d window
    # (looks dead -> CRITICAL), inside the 30d/90d windows (excluded -> alive).
    raw7 = dbutil.rows("access_dead_table_candidates", 7)
    by_name7 = {r["table_name"]: r for r in raw7 if r["table_name"] == "gv_dead_win_boundary"}
    assert by_name7["gv_dead_win_boundary"]["status"] == "CRITICAL"
    for wd in (30, 90):
        raw_w = dbutil.rows("access_dead_table_candidates", wd)
        assert not any(r["table_name"] == "gv_dead_win_boundary" for r in raw_w), (
            f"window={wd}: gv_dead_win_boundary must be excluded (its lineage source is in-window)"
        )


# ---------------------------------------------------------------------------------------------
# access_table_lineage_blast_radius -- warn_blast_principals=10, crit_blast_principals=50.
# ---------------------------------------------------------------------------------------------
def test_access_table_lineage_blast_radius():
    raw = dbutil.rows("access_table_lineage_blast_radius", 30)
    out = [
        r for r in raw
        if (r["source_table_full_name"] and "gv_blast_" in r["source_table_full_name"])
        or (r["target_table_full_name"] and "gv_blast_" in r["target_table_full_name"])
    ]
    _assert_grain_unique(
        out,
        ["workspace_id", "source_table_full_name", "target_table_full_name", "source_type", "target_type", "entity_type", "direct_access", "access_class"],
    )
    _assert_status_enum(out)
    _assert_ordered_by(out, "distinct_principals")

    by_src = {r["source_table_full_name"]: r for r in out if r["source_table_full_name"] and "gv_blast_read_" in r["source_table_full_name"]}
    ok = by_src["gv_catalog.gv_schema.gv_blast_read_ok_src"]
    assert ok["distinct_principals"] == 3 and ok["status"] == "OK" and ok["access_class"] == "READ"
    warn = by_src["gv_catalog.gv_schema.gv_blast_read_warn_src"]
    assert warn["distinct_principals"] == 10 and warn["status"] == "WARN"
    crit = by_src["gv_catalog.gv_schema.gv_blast_read_crit_src"]
    assert crit["distinct_principals"] == 50 and crit["status"] == "CRITICAL"

    write_row = next(r for r in out if r["target_table_full_name"] == "gv_catalog.gv_schema.gv_blast_write_tgt")
    assert write_row["access_class"] == "WRITE" and write_row["source_table_full_name"] is None
    rw_row = next(r for r in out if r["source_table_full_name"] == "gv_catalog.gv_schema.gv_blast_rw_src")
    assert rw_row["access_class"] == "READ_WRITE"
    # gv_blast_unknown_src/tgt has BOTH source_type and target_type NULL, so neither identifier
    # column is set on that row -- it is exactly the "cannot tie to a catalog table on EITHER
    # side" case the T-75B WHERE filter excludes (source_table_full_name IS NOT NULL OR
    # target_table_full_name IS NOT NULL). Before that fix it surfaced as a pooled 'UNKNOWN' edge;
    # now it must be absent entirely, never folded into any other edge's principal count.
    # access.table_lineage is written by no other builder in this repo today, so an unfiltered scan
    # for access_class='UNKNOWN' is still scoped to this builder's own fixture rows.
    unknown_rows = [r for r in raw if r["access_class"] == "UNKNOWN"]
    assert unknown_rows == []

    expect = {
        7: {"gv_catalog.gv_schema.gv_blast_win_t7_tgt"},
        30: {"gv_catalog.gv_schema.gv_blast_win_t7_tgt", "gv_catalog.gv_schema.gv_blast_win_t30_tgt"},
        90: {
            "gv_catalog.gv_schema.gv_blast_win_t7_tgt", "gv_catalog.gv_schema.gv_blast_win_t30_tgt",
            "gv_catalog.gv_schema.gv_blast_win_t90_tgt",
        },
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_table_lineage_blast_radius", wd)
        seen = {
            r["target_table_full_name"] for r in raw_w
            if r["target_table_full_name"] and "gv_blast_win_" in r["target_table_full_name"]
        }
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


def test_access_table_lineage_blast_radius_reach_is_per_source_table_not_per_edge():
    """A source read through two different targets (tests/fixtures/cases_governance.py) by 6
    distinct principals on each edge alone must read the table's own total reach (12, WARN) on
    both rows; each edge keeps its own count (6) in its own column."""
    raw = dbutil.rows("access_table_lineage_blast_radius", 30)
    rows = {
        r["target_table_full_name"]: r for r in raw
        if r["source_table_full_name"] == "b1_catalog.b1_schema.b1_blast_fanout_src"
    }
    assert set(rows) == {
        "b1_catalog.b1_schema.b1_blast_fanout_tgt_a", "b1_catalog.b1_schema.b1_blast_fanout_tgt_b",
    }
    for row in rows.values():
        assert row["event_count"] == 6
        assert row["distinct_principals"] == 6
        assert row["source_distinct_principals"] == 12
        assert row["status"] == "WARN"


# ---------------------------------------------------------------------------------------------
# access_classified_unmasked -- windowless (params: none); only OK/CRITICAL are reachable (the
# query's own CASE has no WARN branch).
# ---------------------------------------------------------------------------------------------
def test_access_classified_unmasked():
    raw = dbutil.rows("access_classified_unmasked", 0)
    out = [r for r in raw if r["table_name"] in ("gv_masked_tbl", "gv_unmasked_tbl")]
    assert all(r["window_days"] == 0 for r in raw), "windowless id: window_days must always be 0"
    _assert_grain_unique(out, ["catalog_name", "schema_name", "table_name", "column_name"])
    _assert_status_enum(out)
    _assert_ordered_by(out, "is_unmasked")  # order_by: is_unmasked DESC, catalog/schema/table/column

    by_name = {r["table_name"]: r for r in out}
    ok = by_name["gv_masked_tbl"]
    assert ok["mask_name"] == "gv_mask_fn" and ok["is_unmasked"] is False and ok["status"] == "OK"
    crit = by_name["gv_unmasked_tbl"]
    assert crit["mask_name"] is None and crit["is_unmasked"] is True and crit["status"] == "CRITICAL"

    excluded = {r["table_name"] for r in raw if r["table_name"] in ("gv_lowconf_tbl", "gv_notag_tbl")}
    assert excluded == set(), "confidence<>HIGH or class_tag NULL must be excluded"


# ---------------------------------------------------------------------------------------------
# access_data_classification_inventory -- windowless (params: none); no status column.
# ---------------------------------------------------------------------------------------------
def test_access_data_classification_inventory():
    raw = dbutil.rows("access_data_classification_inventory", 0)
    assert all(r["window_days"] == 0 for r in raw), "windowless id: window_days must always be 0"
    out = [r for r in raw if r["table_name"] in ("gv_masked_tbl", "gv_unmasked_tbl", "gv_lowconf_tbl")]
    _assert_grain_unique(
        out, ["catalog_name", "schema_name", "table_name", "column_name", "class_tag", "confidence"],
    )

    by_name = {r["table_name"]: r for r in out}

    # gv_masked_tbl: exactly one raw row (see tests/fixtures/governance.py's comment on why the
    # dedup-group scenario lives on gv_lowconf_tbl, not here -- a raw duplicate here would break
    # access_classified_unmasked's own grain, which has no GROUP BY at all).
    masked = [r for r in raw if r["table_name"] == "gv_masked_tbl"]
    assert len(masked) == 1
    assert masked[0]["max_frequency"] == "0.90"

    # gv_lowconf_tbl: TWO raw rows with the same group key dedup to one, with
    # MAX(frequency)/MAX(latest_detected_time)/MIN(first_detected_time) computed correctly (all
    # VARCHAR since this table is MISSING_FROM_DUMP -- lexical ordering matches the intended
    # ordering for these same-format "0.XX" / "YYYY-MM-DD HH:MM:SS" strings).
    lowconf = [r for r in raw if r["table_name"] == "gv_lowconf_tbl"]
    assert len(lowconf) == 1, "two raw rows with the same group key must dedup to one"
    lc = lowconf[0]
    assert lc["max_frequency"] == "0.45"
    assert lc["latest_detected_time"] == "2026-09-05 00:00:00"
    assert lc["first_detected_time"] == "2026-07-15 00:00:00"

    assert by_name["gv_unmasked_tbl"]["max_frequency"] == "0.80"
    assert "gv_lowconf_tbl" in by_name, "LOW confidence is still included here (only classified_unmasked filters HIGH)"
    assert not any(r["table_name"] == "gv_notag_tbl" for r in raw), "class_tag NULL must be excluded"


# ---------------------------------------------------------------------------------------------
# access_network_inbound_denials -- warn_denial_count=10, crit_denial_count=50; event_time only,
# no upper bound (DTODAY included, DOLD excluded).
# ---------------------------------------------------------------------------------------------
def test_access_network_inbound_denials():
    raw = dbutil.rows("access_network_inbound_denials", 30)
    out = [r for r in raw if r["rule_label"] and r["rule_label"].startswith("gv_in_") and not r["rule_label"].startswith("gv_in_win_")]
    _assert_grain_unique(out, ["policy_outcome", "rule_label", "request_path", "authenticated_as", "source_ip"])
    _assert_status_enum(out)
    _assert_ordered_by(out, "denial_count")

    by_rule = {r["rule_label"]: r for r in out}
    ok = by_rule["gv_in_ok_rule"]
    assert ok["denial_count"] == 3 and ok["status"] == "OK" and ok["policy_outcome"] == "DENY_DRY_RUN"

    warn = by_rule["gv_in_warn_rule"]
    assert warn["denial_count"] == 10 and warn["status"] == "WARN"
    assert warn["authenticated_as"] == "gv_in_warn_operator", "mask_user() is off by default"

    crit = by_rule["gv_in_crit_rule"]
    assert crit["denial_count"] == 50 and crit["status"] == "CRITICAL"
    assert crit["authenticated_as"] is None, "NULL -> mask branch 1 passthrough"

    expect = {
        7: {"gv_in_win_t7_rule", "gv_in_win_ttoday_rule"},
        30: {"gv_in_win_t7_rule", "gv_in_win_t30_rule", "gv_in_win_ttoday_rule"},
        90: {"gv_in_win_t7_rule", "gv_in_win_t30_rule", "gv_in_win_t90_rule", "gv_in_win_ttoday_rule"},
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_network_inbound_denials", wd)
        seen = {r["rule_label"] for r in raw_w if r["rule_label"] and r["rule_label"].startswith("gv_in_win_")}
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


# ---------------------------------------------------------------------------------------------
# access_network_outbound_denials -- warn_denial_count=10, crit_denial_count=50; event_time only,
# no upper bound.
# ---------------------------------------------------------------------------------------------
def test_access_network_outbound_denials():
    raw = dbutil.rows("access_network_outbound_denials", 30)
    out = [
        r for r in raw
        if r["destination"] and "gv-out-" in r["destination"] and "gv-out-win-" not in r["destination"]
    ]
    _assert_grain_unique(
        out, ["workspace_id", "network_source_type", "destination_type", "access_type", "destination", "dns_rcode", "storage_rejection_reason"],
    )
    _assert_status_enum(out)
    _assert_ordered_by(out, "denial_count")

    by_dest = {r["destination"]: r for r in out}
    ok = by_dest["gv-out-ok.example.com"]
    assert ok["denial_count"] == 3 and ok["status"] == "OK" and ok["destination_type"] == "DNS" and ok["dns_rcode"] == 3

    warn = by_dest["gv-out-warn-bucket"]
    assert warn["denial_count"] == 10 and warn["status"] == "WARN"
    assert warn["destination_type"] == "STORAGE" and warn["storage_rejection_reason"] == "UNAUTHORIZED"

    crit = by_dest["gv-out-crit.example.com"]
    assert crit["denial_count"] == 50 and crit["status"] == "CRITICAL" and crit["dns_rcode"] == 5

    expect = {
        7: {"gv-out-win-t7.example.com", "gv-out-win-ttoday.example.com"},
        30: {"gv-out-win-t7.example.com", "gv-out-win-t30.example.com", "gv-out-win-ttoday.example.com"},
        90: {
            "gv-out-win-t7.example.com", "gv-out-win-t30.example.com", "gv-out-win-t90.example.com",
            "gv-out-win-ttoday.example.com",
        },
    }
    for wd, expected_present in expect.items():
        raw_w = dbutil.rows("access_network_outbound_denials", wd)
        seen = {r["destination"] for r in raw_w if r["destination"] and "gv-out-win-" in r["destination"]}
        assert seen == expected_present, f"window={wd}: {seen} != {expected_present}"


# ---------------------------------------------------------------------------------------------
# Generic check over all 10 windowed ids (mirrors tests/test_findings/test_performance.py's own
# test_window_days_zero_is_empty_for_all_ids): the generator only ever emits window_days IN
# (7, 30, 90) blocks for a :period_days query (PLAN.md 5.3's UNION ALL of per-window blocks) --
# window_days=0 is reserved for the 2 windowless snapshot ids, so every one of these 10 must come
# back empty at window_days=0.
# ---------------------------------------------------------------------------------------------
def test_window_days_zero_is_empty_for_windowed_ids():
    for qid in WINDOWED_IDS:
        assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty (windowed id)"
