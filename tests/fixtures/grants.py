"""tests/fixtures/grants.py -- P3-GRANTS. The ONE builder for access_broad_grants (domain
governance_access): rows in system.information_schema.table_privileges, .catalog_privileges and
.schema_privileges, the same sources tests/fixtures/governance.py's own `_f2_support_rows()`
already writes rows to for access_grants_inventory/access_grants_inventory_extended (T-20/T-21).
Per DEC-17 both builders' parquet slices for these sources are unioned by name at read time,
so access_broad_grants sees governance.py's rows too (and access_grants_inventory sees these) --
by design, not a conflict.

Every id this module writes (catalog/schema/table/grantee names) carries the prefix `gr_`,
disjoint from governance.py's own `gv_` prefix and from every other builder's ids (DEC-15), so
adding these rows changes no existing test's expected values: access_grants_inventory /
access_grants_inventory_extended (tests/test_findings/test_governance_inventories.py) compute
their own `expected` dict straight from the raw parquet (inclusive of these rows automatically),
and their hard-coded by-construction spot checks are keyed to governance.py's own exact raw
grantee strings ('gv_grantee1@example.com', GUID_EXAMPLE_1, 'gv_operator', 'gv_grantee2@example.com')
-- none of which any row below reuses, so those keys are untouched. The one existing assertion
that scans EVERY raw grantee regardless of prefix (`assert not any(g is None or g ==
'__REDACTED__' ...)` in that same test module) is why no row below ever sets grantee to NULL or
'__REDACTED__' -- deliberately, not an oversight; see this module's own docstring note below for
the resulting, already-precedented branch gap.

Eleven ids below are BROAD (appear in access_broad_grants), five are ordinary grants that must
NOT appear (negative controls, proving the query's filter, not just its shape), and three repeat a
grant their catalog or schema already gives (dropped):

  CRITICAL (ALL PRIVILEGES to a person, or write + all-users at ANY scope):
    gr_tbl1  TABLE  gr_catalog1.gr_schema1.gr_tbl1  'ALL PRIVILEGES' to a USER (masked), not inherited
    gr_tbl8  TABLE  gr_catalog3.gr_schema3.gr_tbl8  'MODIFY' to 'users' (built-in spelling #3) --
                    all-users + write, at TABLE scope: CRITICAL and the "write privilege granted to
                    every user..." reason apply at ANY scope, not catalog only
    gr_cat2  CATALOG gr_catalog5  'MODIFY' to 'all account users' -- CRITICAL via the all-users +
                    write combination, NOT via the ALL-PRIVILEGES literal
    gr_sch1  SCHEMA  gr_catalog8.gr_schema8  'ALL PRIVILEGES' to 'account users' -- the first
                    schema-scope row this query has ever judged
  WARN (ALL PRIVILEGES to a group or service principal, or one other broad rule):
    gr_tbl2  TABLE  gr_catalog1.gr_schema1.gr_tbl2  'ALL_PRIVILEGES' (underscored spelling) to a
                    GROUP, inherited_from set -- proves BOTH spellings are matched and is_inherited=True
    gr_tbl9  TABLE  gr_catalog3.gr_schema3.gr_tbl9  'ALL PRIVILEGES' to GR_GUID (a service
                    principal) -- proves the unmasked SERVICE_PRINCIPAL passthrough branch reaches
                    the query's own output (gr_tbl4 below exercises the GUID on a NOT-BROAD row;
                    this one exercises it on a BROAD row)
    gr_cat1  CATALOG gr_catalog4  'ALL PRIVILEGES' to a GROUP -- proves is_inherited is NULL
                    (not tracked) at CATALOG scope, not False
    gr_tbl3  TABLE  gr_catalog1.gr_schema1.gr_tbl3  'SELECT' to 'account users' (built-in spelling
                    #1) -- all-users but not a write privilege
    gr_tbl5  TABLE  gr_catalog2.gr_schema2.gr_tbl5  'MANAGE' to a GROUP
    gr_cat3  CATALOG gr_catalog6  'CREATE_SCHEMA' to a USER (masked) -- the CREATE% write pattern,
                    to a NAMED principal, still requires CATALOG scope (unlike the all-users rule)
    gr_sch2  SCHEMA  gr_catalog9.gr_schema9  'CREATE_TABLE' to a USER (masked) -- a write grant at
                    SCHEMA scope to a named principal counts like CATALOG scope
  NOT BROAD (excluded; proves the filter, not just the flags):
    gr_tbl4  TABLE  gr_catalog2.gr_schema2.gr_tbl4  'MANAGE' to a 36-char GUID (SERVICE_PRINCIPAL,
                    not GROUP) -- MANAGE only counts "to a group"
    gr_tbl6  TABLE  gr_catalog2.gr_schema2.gr_tbl6  'MODIFY' to a USER -- a named principal's write
                    is judged at CATALOG scope only; one table's MODIFY alone is ordinary
    gr_tbl7  TABLE  gr_catalog2.gr_schema2.gr_tbl7  'SELECT' to a USER -- plain, ordinary grant
    gr_cat4  CATALOG gr_catalog7  'USE_CATALOG' to a GROUP -- ordinary, non-write, non-all-users
    gr_sch3  SCHEMA  gr_catalog10.gr_schema10  'SELECT' to a GROUP -- ordinary schema grant
  COVERED BY A PARENT GRANT (dropped; the parent row above is listed instead):
    gr_tbl10 TABLE  gr_catalog4.gr_schema4.gr_tbl10  same as gr_cat1, inherited from the catalog
    gr_sch4  SCHEMA  gr_catalog4.gr_schema4  same as gr_cat1, at schema scope
    gr_tbl11 TABLE  gr_catalog8.gr_schema8.gr_tbl11  same as gr_sch1, inherited from the schema

Branch gap (recorded, not silently skipped, same as test_governance_inventories.py's own identical
note for access_grants_inventory's GRANTEE column): the NULL/'__REDACTED__' mask passthrough branch
is not exercised by any row here, because a NULL or '__REDACTED__' grantee on table_privileges /
catalog_privileges would also flow into governance.py's shared-source `raw_grantees` set and break
its `assert not any(g is None or g == '__REDACTED__' ...)` check -- adding that coverage belongs to
whichever task next touches that shared assertion, not to a builder that must not shift it.
"""
from __future__ import annotations

BUILDER_NAME = "grants"

IS_TABLE_PRIVILEGES_COLUMNS = [
    "grantor", "grantee", "table_catalog", "table_schema", "table_name", "privilege_type",
    "is_grantable", "inherited_from",
]
IS_CATALOG_PRIVILEGES_COLUMNS = ["grantor", "grantee", "catalog_name", "privilege_type", "is_grantable"]
IS_SCHEMA_PRIVILEGES_COLUMNS = ["catalog_name", "schema_name", "grantee", "privilege_type"]

# A service-principal-shaped grantee, disjoint from governance.py's GUID_EXAMPLE_1/2.
GR_GUID = "77778888-9999-0000-1111-222233334444"


def _mk_row(columns: list[str], **overrides) -> dict:
    unknown = set(overrides) - set(columns)
    if unknown:
        raise ValueError(f"unknown column(s) {sorted(unknown)} for column set {columns}")
    r = {c: None for c in columns}
    r.update(overrides)
    return r


def _table_privilege_rows() -> list[dict]:
    return [
        # CRITICAL -- ALL PRIVILEGES (space spelling), to a USER, not inherited
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_user_broad@example.com", table_catalog="gr_catalog1",
                table_schema="gr_schema1", table_name="gr_tbl1", privilege_type="ALL PRIVILEGES",
                is_grantable="NO", inherited_from=None),
        # WARN -- ALL_PRIVILEGES (underscored spelling), to a GROUP, INHERITED
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_group_data_engineers", table_catalog="gr_catalog1",
                table_schema="gr_schema1", table_name="gr_tbl2", privilege_type="ALL_PRIVILEGES",
                is_grantable="NO", inherited_from="CATALOG gr_catalog1"),
        # WARN -- SELECT to the built-in all-users principal ("account users"): all-users, not write
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="account users", table_catalog="gr_catalog1", table_schema="gr_schema1",
                table_name="gr_tbl3", privilege_type="SELECT", is_grantable="NO",
                inherited_from=None),
        # NOT BROAD -- MANAGE to a service principal (GUID): MANAGE only counts "to a group"
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com", grantee=GR_GUID,
                table_catalog="gr_catalog2", table_schema="gr_schema2", table_name="gr_tbl4",
                privilege_type="MANAGE", is_grantable="NO", inherited_from=None),
        # WARN -- MANAGE to a GROUP: near-ownership
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_group_admins", table_catalog="gr_catalog2", table_schema="gr_schema2",
                table_name="gr_tbl5", privilege_type="MANAGE", is_grantable="NO",
                inherited_from=None),
        # NOT BROAD -- MODIFY (write) on ONE table alone: judged at CATALOG scope only
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_user_plain@example.com", table_catalog="gr_catalog2",
                table_schema="gr_schema2", table_name="gr_tbl6", privilege_type="MODIFY",
                is_grantable="NO", inherited_from=None),
        # NOT BROAD -- plain SELECT to a named user: ordinary access
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_user_plain2@example.com", table_catalog="gr_catalog2",
                table_schema="gr_schema2", table_name="gr_tbl7", privilege_type="SELECT",
                is_grantable="NO", inherited_from=None),
        # CRITICAL -- MODIFY to the built-in all-users principal ("users") at TABLE scope:
        # all-users + write is CRITICAL at ANY scope, not catalog only
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com", grantee="users",
                table_catalog="gr_catalog3", table_schema="gr_schema3", table_name="gr_tbl8",
                privilege_type="MODIFY", is_grantable="NO", inherited_from=None),
        # WARN -- ALL PRIVILEGES to GR_GUID (service principal): proves the SERVICE_PRINCIPAL
        # unmasked-passthrough branch reaches the query's own broad output (gr_tbl4 above exercises
        # the same GUID on a NOT-BROAD row instead)
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com", grantee=GR_GUID,
                table_catalog="gr_catalog3", table_schema="gr_schema3", table_name="gr_tbl9",
                privilege_type="ALL PRIVILEGES", is_grantable="NO", inherited_from=None),
        # DROPPED -- the same grant gr_cat1 gives on the whole catalog
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_group_platform", table_catalog="gr_catalog4", table_schema="gr_schema4",
                table_name="gr_tbl10", privilege_type="ALL PRIVILEGES", is_grantable="NO",
                inherited_from="CATALOG"),
        # DROPPED -- the same grant gr_sch1 gives on the whole schema
        _mk_row(IS_TABLE_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com", grantee="account users",
                table_catalog="gr_catalog8", table_schema="gr_schema8", table_name="gr_tbl11",
                privilege_type="ALL PRIVILEGES", is_grantable="NO", inherited_from="SCHEMA"),
    ]


def _catalog_privilege_rows() -> list[dict]:
    return [
        # WARN -- ALL PRIVILEGES to a GROUP; is_inherited must read NULL (not tracked), not False
        _mk_row(IS_CATALOG_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_group_platform", catalog_name="gr_catalog4",
                privilege_type="ALL PRIVILEGES", is_grantable="NO"),
        # CRITICAL -- MODIFY (write) to 'all account users' (built-in spelling #2): all-users + write
        _mk_row(IS_CATALOG_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="all account users", catalog_name="gr_catalog5", privilege_type="MODIFY",
                is_grantable="NO"),
        # WARN -- CREATE_SCHEMA (write, CREATE% pattern) to a named USER, at catalog scope
        _mk_row(IS_CATALOG_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_user_catalog_write@example.com", catalog_name="gr_catalog6",
                privilege_type="CREATE_SCHEMA", is_grantable="NO"),
        # NOT BROAD -- USE_CATALOG to a GROUP: ordinary, non-write, non-all-users
        _mk_row(IS_CATALOG_PRIVILEGES_COLUMNS, grantor="gr_admin@example.com",
                grantee="gr_group_readonly", catalog_name="gr_catalog7",
                privilege_type="USE_CATALOG", is_grantable="NO"),
    ]


def _schema_privilege_rows() -> list[dict]:
    return [
        # CRITICAL -- ALL PRIVILEGES to the built-in all-users principal ("account users"), at
        # SCHEMA scope: the first schema_grants row this query has ever judged.
        _mk_row(IS_SCHEMA_PRIVILEGES_COLUMNS, grantee="account users",
                catalog_name="gr_catalog8", schema_name="gr_schema8",
                privilege_type="ALL PRIVILEGES"),
        # WARN -- CREATE_TABLE (write, CREATE% pattern) to a named USER, at SCHEMA scope: a write
        # grant at schema scope to a named principal counts like catalog scope.
        _mk_row(IS_SCHEMA_PRIVILEGES_COLUMNS, grantee="gr_user_schema_write@example.com",
                catalog_name="gr_catalog9", schema_name="gr_schema9",
                privilege_type="CREATE_TABLE"),
        # NOT BROAD -- SELECT to a GROUP at schema scope: ordinary, non-write, non-all-users.
        _mk_row(IS_SCHEMA_PRIVILEGES_COLUMNS, grantee="gr_group_schema_readers",
                catalog_name="gr_catalog10", schema_name="gr_schema10", privilege_type="SELECT"),
        # DROPPED -- the same grant gr_cat1 gives on the whole catalog
        _mk_row(IS_SCHEMA_PRIVILEGES_COLUMNS, grantee="gr_group_platform",
                catalog_name="gr_catalog4", schema_name="gr_schema4", privilege_type="ALL PRIVILEGES"),
    ]


def _insert(con, table: str, columns: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f'INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})',
        [[r[c] for c in columns] for r in rows],
    )


def build(con) -> None:
    """Auto-discovered by tests/fixtures/build_fixtures.py (DEC-17): a module-level build(con).
    build_fixtures.py itself writes this builder's parquet slice for every source it touches --
    no write_parquet() call needed here."""
    _insert(con, "information_schema__table_privileges", IS_TABLE_PRIVILEGES_COLUMNS,
            _table_privilege_rows())
    _insert(con, "information_schema__catalog_privileges", IS_CATALOG_PRIVILEGES_COLUMNS,
            _catalog_privilege_rows())
    _insert(con, "information_schema__schema_privileges", IS_SCHEMA_PRIVILEGES_COLUMNS,
            _schema_privilege_rows())
