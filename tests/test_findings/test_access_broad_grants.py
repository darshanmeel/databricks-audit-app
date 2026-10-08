"""tests/test_findings/test_access_broad_grants.py -- P3-GRANTS.

Proves, against tests/fixtures/grants.py's own `gr_`-prefixed rows (DEC-15) in
system.information_schema.table_privileges / .catalog_privileges / .schema_privileges -- the same
sources tests/fixtures/governance.py's F2 support rows also write to, unioned by name at read time
(DEC-17) -- that findings.f_access_broad_grants (grain [securable_type, securable, grantee,
privilege_type], windowless per config/grains/access_broad_grants.yml) has: the right grain,
worst-first ordering, a status enum restricted to CRITICAL/WARN only (this is a pre-filtered
broad-grants list; OK and NOT_ASSESSED must never appear), and an output that matches an
independent Python re-implementation of the SQL body's own flagging + status rule computed
straight from the raw parquet -- never a hard-coded total (this also, by construction, covers
whatever governance.py's own F2 rows contribute: its one ALL_PRIVILEGES-to-a-group row is a real
broad grant and is expected to appear here too). Then by-construction spot checks prove each of
the 11 `gr_`-prefixed BROAD rows' exact status/reason/grantee_type/masking/is_inherited, and that
the 5 `gr_`-prefixed NOT-BROAD rows and the 3 grants covered by a parent grant are absent -- the
filter, not just the flags. Per
tests/test_findings/README.md's checklist.

Every expectation below is computed by hand from tests/fixtures/grants.py's own docstring/comment
block (and cross-checked against it), or independently re-derived from the raw fixture parquet --
never read back from the model under test.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import grants as gr  # noqa: E402 (imported for its GR_GUID constant only)

GRAINS_PATH = ROOT / "config" / "grains" / "access_broad_grants.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
QID = "access_broad_grants"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
STATUS_RANK = {"CRITICAL": 0, "WARN": 1, "OK": 2, "NOT_ASSESSED": 3}
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ALL_USERS_NAMES = {"account users", "all account users", "users"}


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _raw(schema_table: str) -> list[dict]:
    """Every row from tests/fixtures/parquet/<schema_table>/*.parquet, across every builder that
    writes to it (grants.py's own slice unioned by name with governance.py's, the same raw source
    dbt itself reads via read_parquet) -- the same pattern
    tests/test_findings/test_governance_inventories.py's own `_raw()` uses."""
    pattern = (PARQUET_DIR / schema_table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def _grantee_type(v) -> str:
    """Independent re-implementation of the SQL body's `classified` CTE CASE (not imported)."""
    if v is None or v == "__REDACTED__":
        return "UNKNOWN"
    if v.strip().lower() in _ALL_USERS_NAMES:
        return "ALL_USERS"
    if _GUID_RE.match(v):
        return "SERVICE_PRINCIPAL"
    if "@" in v:
        return "USER"
    return "GROUP"


def _mask(v, gtype: str):
    """Independent re-implementation of the SQL body's outer `grantee` CASE (not imported).
    mask_user() is off by default, so every grantee_type passes through raw here too."""
    return v


def _is_write(priv: str) -> bool:
    return priv == "MODIFY" or priv.startswith("CREATE") or priv in ("ALL PRIVILEGES", "ALL_PRIVILEGES")


def _classify(securable_type: str, priv: str, gtype: str):
    """Independent re-implementation of the SQL body's `flagged` CTE + status/reason CASE (not
    imported): returns (status, reason) for a broad grant, or (None, None) if it is not broad and
    must not appear in the query's output at all.

    `is_write` (any scope) drives the all-users CRITICAL rule; `is_write_catalog_or_schema`
    (CATALOG or SCHEMA scope only) drives the separate, scope-limited named-principal WARN rule --
    these are deliberately different flags, matching the SQL's own `is_write` /
    `is_write_at_catalog_or_schema` split."""
    is_all_priv = priv in ("ALL PRIVILEGES", "ALL_PRIVILEGES")
    is_all_users = gtype == "ALL_USERS"
    is_manage_group = gtype == "GROUP" and priv in ("MANAGE", "OWNERSHIP")
    is_write = _is_write(priv)
    is_write_catalog_or_schema = securable_type in ("CATALOG", "SCHEMA") and is_write
    if not (is_all_priv or is_all_users or is_manage_group or is_write_catalog_or_schema):
        return None, None
    if is_all_priv and gtype == "USER":
        return "CRITICAL", "ALL PRIVILEGES granted to a person, not a group"
    if is_all_users and is_write:
        return "CRITICAL", "write privilege granted to every user in the account"
    if is_all_priv:
        return "WARN", "ALL PRIVILEGES granted to a group or service principal"
    if is_all_users:
        return "WARN", "granted to every user in the account"
    if is_manage_group:
        return "WARN", "MANAGE (near-ownership) privilege granted to a group"
    return "WARN", "write privilege granted at catalog or schema level"


def _expected() -> dict:
    exp: dict[tuple, dict] = {}
    # A grant its catalog (or, for a table, its schema) already gives the same grantee is dropped.
    cat_grants = {(r["catalog_name"], r["grantee"], r["privilege_type"])
                  for r in _raw("information_schema__catalog_privileges")}
    sch_grants = {(r["catalog_name"], r["schema_name"], r["grantee"], r["privilege_type"])
                  for r in _raw("information_schema__schema_privileges")}
    for r in _raw("information_schema__table_privileges"):
        if ((r["table_catalog"], r["grantee"], r["privilege_type"]) in cat_grants
                or (r["table_catalog"], r["table_schema"], r["grantee"], r["privilege_type"]) in sch_grants):
            continue
        securable = f'{r["table_catalog"]}.{r["table_schema"]}.{r["table_name"]}'
        gtype = _grantee_type(r["grantee"])
        status, reason = _classify("TABLE", r["privilege_type"], gtype)
        if status is None:
            continue
        grantee = _mask(r["grantee"], gtype)
        exp[("TABLE", securable, grantee, r["privilege_type"])] = {
            "securable_type": "TABLE", "securable": securable, "grantee_type": gtype,
            "grantee": grantee, "privilege_type": r["privilege_type"],
            "is_inherited": r["inherited_from"] is not None, "inherited_from": r["inherited_from"],
            "reason": reason, "status": status,
        }
    for r in _raw("information_schema__catalog_privileges"):
        securable = r["catalog_name"]
        gtype = _grantee_type(r["grantee"])
        status, reason = _classify("CATALOG", r["privilege_type"], gtype)
        if status is None:
            continue
        grantee = _mask(r["grantee"], gtype)
        exp[("CATALOG", securable, grantee, r["privilege_type"])] = {
            "securable_type": "CATALOG", "securable": securable, "grantee_type": gtype,
            "grantee": grantee, "privilege_type": r["privilege_type"],
            "is_inherited": None, "inherited_from": None,
            "reason": reason, "status": status,
        }
    for r in _raw("information_schema__schema_privileges"):
        if (r["catalog_name"], r["grantee"], r["privilege_type"]) in cat_grants:
            continue
        securable = f'{r["catalog_name"]}.{r["schema_name"]}'
        gtype = _grantee_type(r["grantee"])
        status, reason = _classify("SCHEMA", r["privilege_type"], gtype)
        if status is None:
            continue
        grantee = _mask(r["grantee"], gtype)
        exp[("SCHEMA", securable, grantee, r["privilege_type"])] = {
            "securable_type": "SCHEMA", "securable": securable, "grantee_type": gtype,
            "grantee": grantee, "privilege_type": r["privilege_type"],
            "is_inherited": None, "inherited_from": None,
            "reason": reason, "status": status,
        }
    return exp


def _rows_by_securable() -> dict:
    return {r["securable"]: r for r in dbutil.rows(QID, 0)}


# ---------------------------------------------------------------------------------------------
# Generic shape: grain, windowless, status enum restricted to CRITICAL/WARN, worst-first order.
# ---------------------------------------------------------------------------------------------
def test_windowless_shape():
    at_zero = dbutil.rows(QID, 0)
    assert at_zero, "window_days=0 must be non-empty (grants.py + governance.py both seed rows)"
    assert all(r["window_days"] == 0 for r in at_zero)
    for w in WINDOWS:
        assert dbutil.rows(QID, w) == [], f"window_days={w} must be empty (snapshot id, not windowed)"


def test_grain_uniqueness():
    rows = dbutil.rows(QID, 0)
    grain = _grains()[QID]
    keys = [tuple(r[c] for c in grain) for r in rows]
    assert len(keys) == len(set(keys)), f"duplicate grain keys: {[k for k in keys if keys.count(k) > 1]}"


def test_status_is_broad_only():
    rows = dbutil.rows(QID, 0)
    statuses = {r["status"] for r in rows}
    assert statuses, "vacuous check"
    assert statuses <= STATUS_VALUES
    assert statuses <= {"CRITICAL", "WARN"}, (
        "this is a pre-filtered broad-grants list (WHERE-filtered in the SQL body): "
        f"OK/NOT_ASSESSED must never appear, got {statuses}"
    )


def test_worst_first_order():
    rows = dbutil.rows(QID, 0)
    keys = [
        (STATUS_RANK[r["status"]], r["securable_type"], r["securable"], r["privilege_type"], r["grantee"])
        for r in rows
    ]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------------------------
# Independent re-implementation match: proves the query's real output against every raw row in
# both source tables (grants.py's AND governance.py's), not just the ids this task added.
# ---------------------------------------------------------------------------------------------
def test_matches_independent_reimplementation():
    rows = dbutil.rows(QID, 0)
    actual = {
        (r["securable_type"], r["securable"], r["grantee"], r["privilege_type"]): {
            k: r[k] for k in (
                "securable_type", "securable", "grantee_type", "grantee", "privilege_type",
                "is_inherited", "inherited_from", "reason", "status",
            )
        }
        for r in rows
    }
    assert actual == _expected()

    # governance.py's own ALL_PRIVILEGES-to-'gv_operator' row (_f2_support_rows) is a genuine
    # broad grant on the shared source and must surface here too -- proves this query is not
    # scoped to gr_-prefixed rows only.
    assert "gv_catalog.gv_schema.gv_tbl1" in {r["securable"] for r in rows}


# ---------------------------------------------------------------------------------------------
# By-construction spot checks (tests/fixtures/grants.py's own docstring table).
# ---------------------------------------------------------------------------------------------
def test_all_privileges_critical_for_a_person_warn_for_a_group():
    by_sec = _rows_by_securable()

    r = by_sec["gr_catalog1.gr_schema1.gr_tbl1"]
    assert r["privilege_type"] == "ALL PRIVILEGES"
    assert r["status"] == "CRITICAL"
    assert r["reason"] == "ALL PRIVILEGES granted to a person, not a group"
    assert r["grantee_type"] == "USER"
    assert r["grantee"] == _mask("gr_user_broad@example.com", "USER")
    assert r["is_inherited"] is False
    assert r["inherited_from"] is None

    r2 = by_sec["gr_catalog1.gr_schema1.gr_tbl2"]
    assert r2["privilege_type"] == "ALL_PRIVILEGES", "underscored spelling must also be matched"
    assert r2["status"] == "WARN"
    assert r2["reason"] == "ALL PRIVILEGES granted to a group or service principal"
    assert r2["grantee_type"] == "GROUP"
    assert r2["grantee"] == "gr_group_data_engineers", "group grantee shown in full, unmasked"
    assert r2["is_inherited"] is True
    assert r2["inherited_from"] == "CATALOG gr_catalog1"


def test_all_users_variants():
    by_sec = _rows_by_securable()

    # built-in spelling #1, at TABLE scope, non-write privilege -> WARN, plain all-users reason
    r = by_sec["gr_catalog1.gr_schema1.gr_tbl3"]
    assert r["privilege_type"] == "SELECT"
    assert r["grantee_type"] == "ALL_USERS"
    assert r["grantee"] == "account users"
    assert r["status"] == "WARN"
    assert r["reason"] == "granted to every user in the account"

    # built-in spelling #3, at TABLE scope, a write-shaped privilege (MODIFY) -- all-users + write
    # is CRITICAL at ANY scope (not catalog only), so this must read the write reason, same as the
    # catalog-scope case below
    r2 = by_sec["gr_catalog3.gr_schema3.gr_tbl8"]
    assert r2["privilege_type"] == "MODIFY"
    assert r2["grantee_type"] == "ALL_USERS"
    assert r2["grantee"] == "users"
    assert r2["status"] == "CRITICAL"
    assert r2["reason"] == "write privilege granted to every user in the account"

    # built-in spelling #2, at CATALOG scope, MODIFY -- all-users + write -> CRITICAL, same reason,
    # via the all-users/write combination rather than the ALL-PRIVILEGES literal
    r3 = by_sec["gr_catalog5"]
    assert r3["securable_type"] == "CATALOG"
    assert r3["privilege_type"] == "MODIFY"
    assert r3["grantee_type"] == "ALL_USERS"
    assert r3["grantee"] == "all account users"
    assert r3["status"] == "CRITICAL"
    assert r3["reason"] == "write privilege granted to every user in the account"


def test_manage_to_group_warn():
    r = _rows_by_securable()["gr_catalog2.gr_schema2.gr_tbl5"]
    assert r["privilege_type"] == "MANAGE"
    assert r["grantee_type"] == "GROUP"
    assert r["grantee"] == "gr_group_admins"
    assert r["status"] == "WARN"
    assert r["reason"] == "MANAGE (near-ownership) privilege granted to a group"


def test_write_at_catalog_scope_to_named_user_warn():
    r = _rows_by_securable()["gr_catalog6"]
    assert r["securable_type"] == "CATALOG"
    assert r["privilege_type"] == "CREATE_SCHEMA"
    assert r["grantee_type"] == "USER"
    assert r["grantee"] == _mask("gr_user_catalog_write@example.com", "USER")
    assert r["status"] == "WARN"
    assert r["reason"] == "write privilege granted at catalog or schema level"


def test_schema_scope_all_privileges_to_all_users_critical():
    """The first schema_grants row this query has ever judged: ALL PRIVILEGES to the built-in
    all-users principal at SCHEMA scope."""
    r = _rows_by_securable()["gr_catalog8.gr_schema8"]
    assert r["securable_type"] == "SCHEMA"
    assert r["privilege_type"] == "ALL PRIVILEGES"
    assert r["grantee_type"] == "ALL_USERS"
    assert r["status"] == "CRITICAL"
    assert r["reason"] == "write privilege granted to every user in the account"
    assert r["is_inherited"] is None
    assert r["inherited_from"] is None


def test_write_at_schema_scope_to_named_user_warn():
    """A write grant at SCHEMA scope to a named principal counts like CATALOG scope."""
    r = _rows_by_securable()["gr_catalog9.gr_schema9"]
    assert r["securable_type"] == "SCHEMA"
    assert r["privilege_type"] == "CREATE_TABLE"
    assert r["grantee_type"] == "USER"
    assert r["grantee"] == _mask("gr_user_schema_write@example.com", "USER")
    assert r["status"] == "WARN"
    assert r["reason"] == "write privilege granted at catalog or schema level"


def test_ordinary_schema_grant_excluded():
    securables = {r["securable"] for r in dbutil.rows(QID, 0)}
    assert "gr_catalog10.gr_schema10" not in securables, "plain SELECT to a group at schema scope"


def test_catalog_and_schema_scope_is_inherited_always_null_not_false():
    by_sec = _rows_by_securable()
    for securable, securable_type in (
        ("gr_catalog4", "CATALOG"), ("gr_catalog5", "CATALOG"), ("gr_catalog6", "CATALOG"),
        ("gr_catalog8.gr_schema8", "SCHEMA"), ("gr_catalog9.gr_schema9", "SCHEMA"),
    ):
        r = by_sec[securable]
        assert r["securable_type"] == securable_type
        assert r["is_inherited"] is None, (
            f"{securable}: this source carries no inherited_from column -- "
            "is_inherited must be NULL (not tracked), never False"
        )
        assert r["inherited_from"] is None


def test_not_broad_rows_excluded():
    """The 4 gr_-prefixed ordinary grants in grants.py must not appear at all -- proves the
    query's WHERE filter, not just its status/reason flags."""
    securables = {r["securable"] for r in dbutil.rows(QID, 0)}
    assert "gr_catalog2.gr_schema2.gr_tbl4" not in securables, "MANAGE to a service principal, not a group"
    assert "gr_catalog2.gr_schema2.gr_tbl6" not in securables, "MODIFY on one table alone (write is catalog-scope only)"
    assert "gr_catalog2.gr_schema2.gr_tbl7" not in securables, "plain SELECT to a named user"
    assert "gr_catalog7" not in securables, "USE_CATALOG to a group at catalog scope"
    # GR_GUID itself (gr_tbl4, NOT BROAD -- MANAGE to a service principal) contributes no row here,
    # even though the SAME GUID does legitimately appear as a grantee on the separate BROAD gr_tbl9
    # row below (test_service_principal_guid_passthrough_when_broad) -- the filter is per-grant,
    # not per-grantee.
    assert "gr_catalog2.gr_schema2.gr_tbl4" not in {r["securable"] for r in dbutil.rows(QID, 0) if r["grantee"] == gr.GR_GUID}


def test_service_principal_guid_passthrough_when_broad():
    """GR_GUID is used on two rows in this fixture: the NOT-BROAD gr_tbl4 (MANAGE, excluded above)
    and the BROAD gr_tbl9 (ALL PRIVILEGES). This proves the unmasked SERVICE_PRINCIPAL passthrough
    branch against the query's own real output, not just the test's own re-implementation."""
    assert _mask(gr.GR_GUID, _grantee_type(gr.GR_GUID)) == gr.GR_GUID
    assert _grantee_type(gr.GR_GUID) == "SERVICE_PRINCIPAL"

    r = _rows_by_securable()["gr_catalog3.gr_schema3.gr_tbl9"]
    assert r["privilege_type"] == "ALL PRIVILEGES"
    assert r["grantee_type"] == "SERVICE_PRINCIPAL"
    assert r["grantee"] == gr.GR_GUID, "service principal GUID must pass through unmasked"
    assert r["status"] == "WARN"
    assert r["reason"] == "ALL PRIVILEGES granted to a group or service principal"


def test_grant_covered_by_a_parent_grant_dropped():
    securables = {r["securable"] for r in dbutil.rows(QID, 0)}
    assert "gr_catalog4" in securables, "the catalog grant itself is listed"
    assert "gr_catalog4.gr_schema4" not in securables, "same grant at schema scope"
    assert "gr_catalog4.gr_schema4.gr_tbl10" not in securables, "same grant inherited by a table"
    assert "gr_catalog8.gr_schema8.gr_tbl11" not in securables, "same grant as its schema"
