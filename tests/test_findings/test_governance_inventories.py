"""tests/test_findings/test_governance_inventories.py -- T-21's 10 F2 "current-state inventory"
ids (9 in domain governance_access, plus table_inventory_type in domain storage): the
information_schema snapshot queries, as opposed to T-20's 12 F1 ids that read system.access.audit
/ table_lineage / column_lineage / inbound_network / outbound_network directly
(tests/test_findings/test_governance_access.py). This task adds no fixture rows of its own -- every
row read here was written by tests/fixtures/governance.py's `_f2_support_rows()` (plus
information_schema.column_masks/column_tags from the F1 helpers, and information_schema.tables from
`_dead_table_candidates_rows()`, both reused as-is per that module's own docstring) -- and owns only
config/grains/governance_inventories.yml (new) and this file (new).

Reads findings.f_<query_id> through tests/dbutil.py, per tests/test_findings/README.md's checklist,
matching tests/test_findings/test_governance_access.py's (F1) and
tests/test_findings/test_compute_activity.py's style: grain uniqueness (proven against
config/grains/governance_inventories.yml, not re-derived), status enum + worst-first ordering where
a status column exists (and its EXPLICIT ABSENCE asserted where it does not -- 7 of these 10 ids are
pure inventories with no status column at all), every object_scope/source-table branch present at
least once for the UNION-based ids, all reachable status bands hit by id, and expectations computed
from the raw fixture parquet (`_raw()` below, the same read_parquet(..., union_by_name=true) pattern
as tests/dbutil.py's own usage_sum / test_compute_activity.py's _raw_row_exists) or from
tests/fixtures/governance.py's own literal ids (imported directly as `gv`) -- never a hard-coded
total.

Snapshot vs windowed (task file Inputs, DEC-24, PLAN.md 5.3): all 10 of these ids are windowless --
none carries a `:period_days` param, even the 4 that carry `:top_n` / threshold params
(access_views_inventory, access_volumes_inventory, access_pii_outside_tables,
access_delta_sharing_exposure). The generator therefore emits a single `0 AS window_days` block for
every one of them: `dbutil.rows(id, 0)` is the only non-empty read, and `dbutil.rows(id, w)` for
w in (7, 30, 90) MUST be `[]` for all 10 -- the reverse of a windowed id, and (per the task file,
verbatim) "the most common error in these batches." Every test below checks this explicitly, not
just once generically, so a per-id regression (e.g. someone later adding a real :period_days to one
of these bodies without updating its grain/tests) cannot slip past a single shared assertion.

GRANTEE (access_grants_inventory, access_grants_inventory_extended) is the only column in this
batch's grain that is a lossy, masked projection of the raw GROUP BY key (DEC-48; see
config/grains/governance_inventories.yml's header for exactly why). The masked *display* columns on
other ids (volume_owner, share_owner, RECIPIENT_NAME via the aggregated `recipients` string) are
NOT part of any grain here -- each of those ids' natural key is a plain information_schema
identifier column (volume/share name), so no DEC-48 note applies to them.

Two fixture gaps this task found but may not fix (tests/fixtures/governance.py is T-20's, out of
scope -- named here and in Hand-off notes, per this task's own instructions, rather than skipped
silently or worked around by editing the fixture):
  1. Identity-mask branch 1 (NULL or '__REDACTED__' passthrough) is never exercised on GRANTEE or
     RECIPIENT_NAME anywhere in the F2 support rows -- every GRANTEE/RECIPIENT_NAME/VOLUME_OWNER/
     SHARE_OWNER raw value governance.py writes is either an email, a 36-char GUID, or a plain
     string. Branches 2/3/4 are proven below (test_access_grants_inventory /
     test_access_grants_inventory_extended / test_access_delta_sharing_exposure); branch 1 is not,
     for these two columns specifically (it IS exercised elsewhere in governance.py, e.g.
     access_login_concentration's principal / access_dead_table_candidates' table_owner -- F1
     columns, not GRANTEE/RECIPIENT_NAME).
  2. access_delta_sharing_exposure's `shared_table_count >= :crit_shared_tables` (100) CRITICAL
     branch, independent of recipient_count, is never exercised: table_share_usage only ever
     attaches 2 rows to one share (gv_share_warn), so no share in this fixture crosses 100 shared
     tables while staying under the 10-recipient CRITICAL threshold. test_access_delta_sharing_exposure
     asserts this gap directly against the raw parquet rather than asserting a CRITICAL-via-table-count
     row that does not exist.
Both are also true, by the same argument, for `access_volumes_inventory` / `access_pii_outside_tables`'s
`array_join(collect_set(...))` -> `array_to_string(list_distinct(list(...)), ', ')` translation
(PLAN.md 5.4): governance.py never attaches two DIFFERENT raw tag rows to the same volume/schema, so
those two ids' own `tag_names` / `tags` columns are only ever proven non-null and correctly
populated with their one true value here, not a genuine multi-value collapse. A real multi-value
list IS proven directly, on the same translated construct, in test_access_delta_sharing_exposure
(`recipients`: gv_share_warn has 2 distinct raw recipients plus one raw-duplicate row of the
first, gv_share_crit has 10 distinct raw recipients -- under DEC-48's 2-char-prefix mask the
distinct ones also collapsed onto one shared masked string; under DEC-66.3's hash-derived id
(this module's `_mask()`) each distinct recipient stays distinct while the raw duplicate still
collapses, so the proof is that every distinct masked value appears exactly once in the built
list, duplicates removed but different people never merged) -- see that test for the actual proof.
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import governance as gv  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "governance_inventories.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
STATUS_RANK = {"CRITICAL": 0, "WARN": 1, "OK": 2, "NOT_ASSESSED": 3}

# The 10 F2 ids this file covers (9 governance_access + table_inventory_type, storage).
ALL_IDS = [
    "access_column_masks_inventory",
    "access_row_filters_inventory",
    "access_grants_inventory",
    "access_grants_inventory_extended",
    "access_tags_inventory",
    "access_views_inventory",
    "access_volumes_inventory",
    "access_pii_outside_tables",
    "access_delta_sharing_exposure",
    "table_inventory_type",
]

# Of the 10, only these 3 bodies emit a `status` column at all (DEC-08: is_finding requires a real
# `healthy:` header line AND an `AS status` in the body). The other 7 are pure inventories -- no
# `status` key exists on their rows, asserted as an explicit absence below, not skipped.
STATUS_IDS = {"access_volumes_inventory", "access_pii_outside_tables", "access_delta_sharing_exposure"}

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _mask(v):
    """Independent re-implementation of the identity-mask CASE every vendored body in this library
    uses, now routed through the mask_user() dbt macro: off by default, so every branch
    passes `v` through raw. Kept as a named function so an `on` build's expectation would only
    need this one place changed."""
    return v


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _raw(schema_table: str) -> list[dict]:
    """Every row from tests/fixtures/parquet/<schema_table>/*.parquet (governance.py's own slice,
    unioned by name with any other builder's -- the same raw source dbt itself reads via
    read_parquet), used to compute every count/grouping expectation below independently of the
    built model SQL -- the same pattern as tests/dbutil.py's usage_sum and
    test_compute_activity.py's _raw_row_exists / _cost_rollup."""
    pattern = (PARQUET_DIR / schema_table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def _assert_grain_unique(rows: list[dict], key_fields: list[str]) -> None:
    assert rows, "a vacuously-true uniqueness check on an empty result proves nothing"
    keys = [tuple(r[f] for f in key_fields) for r in rows]
    assert len(keys) == len(set(keys)), f"duplicate grain keys: {[k for k in keys if keys.count(k) > 1]}"


def _assert_sorted(rows: list[dict], key_fn) -> None:
    """`rows`, as returned by dbutil.rows() (a plain SELECT * ... WHERE window_days = ?, no ORDER
    BY of its own), must already be in the model's own meta.order_by (confirmed against
    dbt/models/findings/governance_access/_findings__governance_access.yml /
    dbt/models/findings/storage/_findings__storage.yml above, per id) -- `key_fn` encodes that
    order_by as a tuple per row (status rank first + DESC numeric fields negated, where the
    order_by uses `CASE status ...`); the returned sequence of keys must already be non-decreasing."""
    keys = [key_fn(r) for r in rows]
    assert keys == sorted(keys), f"not ordered by the model's own order_by: {keys}"


def _assert_no_status_column(rows: list[dict]) -> None:
    assert rows, "a vacuously-true absence check on an empty result proves nothing"
    assert "status" not in rows[0], "this id is a pure inventory (DEC-08); it must carry no status column"


# ---------------------------------------------------------------------------------------------
# Generic checks over all 10 ids: windowless shape (README / task file's "most common error"),
# grain uniqueness against config/grains/governance_inventories.yml, and status column presence
# vs. absence split by STATUS_IDS. Deliberately NOT scoped to gv_-prefixed rows -- governance.py is
# the only builder writing to 18 of these 19 underlying information_schema tables (all except
# information_schema.tables, which is also written by ports.py and is filtered explicitly in
# test_table_inventory_type below).
# ---------------------------------------------------------------------------------------------
def test_windowless_shape_for_all_ids():
    for qid in ALL_IDS:
        at_zero = dbutil.rows(qid, 0)
        assert at_zero, f"{qid}: window_days=0 must be non-empty (snapshot id)"
        assert all(r["window_days"] == 0 for r in at_zero), f"{qid}: window_days must always be 0"
        for w in WINDOWS:
            assert dbutil.rows(qid, w) == [], f"{qid}: window_days={w} must be empty (snapshot id, not windowed)"


def test_grain_uniqueness_all_ids():
    grains = _grains()
    for qid in ALL_IDS:
        _assert_grain_unique(dbutil.rows(qid, 0), grains[qid])


def test_status_column_presence_split():
    for qid in ALL_IDS:
        rows = dbutil.rows(qid, 0)
        assert rows, f"{qid}: window_days=0 must be non-empty"  # review fix item 5: guard against a vacuous check below
        if qid in STATUS_IDS:
            bad = {r["status"] for r in rows} - STATUS_VALUES
            assert not bad, f"{qid}: status values outside the enum: {bad}"
        else:
            _assert_no_status_column(rows)


# ---------------------------------------------------------------------------------------------
# access_column_masks_inventory -- no status (pure inventory); reads
# information_schema.column_masks verbatim, no GROUP BY.
# ---------------------------------------------------------------------------------------------
def test_access_column_masks_inventory():
    raw = dbutil.rows("access_column_masks_inventory", 0)
    grain = _grains()["access_column_masks_inventory"]
    _assert_grain_unique(raw, grain)
    _assert_no_status_column(raw)

    expected = _raw("information_schema__column_masks")
    assert len(raw) == len(expected), "one output row per information_schema.column_masks row (no fan-out)"
    by_key = {(r["table_catalog"], r["table_schema"], r["table_name"], r["column_name"]): r for r in raw}
    for e in expected:
        key = (e["table_catalog"], e["table_schema"], e["table_name"], e["column_name"])
        assert key in by_key
        assert by_key[key]["mask_name"] == e["mask_name"]
        assert by_key[key]["using_columns"] == e["using_columns"]

    only = by_key[(gv.GV_CATALOG, gv.GV_SCHEMA, "gv_masked_tbl", "gv_ssn")]
    assert only["mask_name"] == "gv_mask_fn" and only["using_columns"] == "gv_ssn"

    _assert_sorted(raw, lambda r: (r["table_catalog"], r["table_schema"], r["table_name"], r["column_name"]))


# ---------------------------------------------------------------------------------------------
# access_row_filters_inventory -- no status (pure inventory); reads
# information_schema.row_filters verbatim, no GROUP BY.
# ---------------------------------------------------------------------------------------------
def test_access_row_filters_inventory():
    raw = dbutil.rows("access_row_filters_inventory", 0)
    grain = _grains()["access_row_filters_inventory"]
    _assert_grain_unique(raw, grain)
    _assert_no_status_column(raw)

    expected = _raw("information_schema__row_filters")
    assert len(raw) == len(expected)
    by_key = {(r["table_catalog"], r["table_schema"], r["table_name"]): r for r in raw}
    for e in expected:
        key = (e["table_catalog"], e["table_schema"], e["table_name"])
        assert key in by_key
        assert by_key[key]["filter_name"] == e["filter_name"]
        assert by_key[key]["target_columns"] == e["target_columns"]

    only = by_key[(gv.GV_CATALOG, gv.GV_SCHEMA, "gv_filtered_tbl")]
    assert only["filter_name"] == "gv_row_filter_fn" and only["target_columns"] == "gv_region"

    _assert_sorted(raw, lambda r: (r["table_catalog"], r["table_schema"], r["table_name"]))


# ---------------------------------------------------------------------------------------------
# access_grants_inventory -- no status (pure inventory); table_privileges UNION ALL
# catalog_privileges, grouped by (object_scope, PRIVILEGE_TYPE, raw GRANTEE), GRANTEE masked in the
# outer SELECT (DEC-48 masked grain).
# ---------------------------------------------------------------------------------------------
def test_access_grants_inventory():
    raw = dbutil.rows("access_grants_inventory", 0)
    grain = _grains()["access_grants_inventory"]
    _assert_grain_unique(raw, grain)
    _assert_no_status_column(raw)

    groups: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "objects": set()})
    for r in _raw("information_schema__table_privileges"):
        key = ("TABLE", r["privilege_type"], r["grantee"])
        g = groups[key]
        g["count"] += 1
        g["objects"].add((r["table_catalog"], r["table_schema"], r["table_name"]))
    for r in _raw("information_schema__catalog_privileges"):
        key = ("CATALOG", r["privilege_type"], r["grantee"])
        g = groups[key]
        g["count"] += 1
        g["objects"].add(r["catalog_name"])
    expected = {
        (scope, priv, _mask(raw_grantee)): (g["count"], len(g["objects"]))
        for (scope, priv, raw_grantee), g in groups.items()
    }
    actual = {
        (r["object_scope"], r["privilege_type"], r["GRANTEE"]): (r["grant_count"], r["distinct_objects"])
        for r in raw
    }
    assert actual == expected

    # every object_scope branch present (task file Steps: "every object_scope/source-table branch
    # present at least once for the UNION-based ids").
    assert {r["object_scope"] for r in raw} == {"TABLE", "CATALOG"}

    # by-construction spot checks (tests/fixtures/governance.py _f2_support_rows()).
    assert actual[("TABLE", "SELECT", "gv_grantee1@example.com")] == (2, 2), "x2 rows, 2 distinct tables"
    assert actual[("TABLE", "MODIFY", gv.GUID_EXAMPLE_1)] == (1, 1), "GUID grantee passes through"
    assert actual[("TABLE", "ALL_PRIVILEGES", "gv_operator")] == (1, 1)
    assert actual[("CATALOG", "USE_CATALOG", "gv_grantee1@example.com")] == (1, 1)

    # mask_user() is off by default, so branch coverage is checked on the RAW source
    # values, then every raw shape is proven to reach the built output unchanged.
    raw_grantees = {k[2] for k in groups}
    assert any("@" in g for g in raw_grantees), "an email grantee must appear somewhere in the raw source"
    assert any(_GUID_RE.match(g) for g in raw_grantees), "a GUID grantee must appear somewhere in the raw source"
    assert any("@" not in g and not _GUID_RE.match(g) for g in raw_grantees), "a plain-string grantee must appear somewhere in the raw source"
    # branch (NULL/'__REDACTED__' passthrough) is NOT exercised on GRANTEE anywhere in
    # governance.py's F2 rows -- see this module's docstring "fixture gaps" note 1; recorded, not
    # silently skipped.
    assert not any(g is None or g == "__REDACTED__" for g in raw_grantees)

    grantees = {r["GRANTEE"] for r in raw}
    assert grantees == {_mask(g) for g in raw_grantees}
    assert any(_GUID_RE.match(g) for g in grantees), "GUID grantee still passes through unmasked in the output"

    _assert_sorted(raw, lambda r: (r["object_scope"], r["privilege_type"], r["GRANTEE"]))


# ---------------------------------------------------------------------------------------------
# access_grants_inventory_extended -- no status (pure inventory); schema/connection/credential/
# external_location_privileges, each UNION ALL branch grouped by (object_scope literal,
# PRIVILEGE_TYPE, raw GRANTEE), GRANTEE masked (DEC-48 masked grain).
# ---------------------------------------------------------------------------------------------
def test_access_grants_inventory_extended():
    raw = dbutil.rows("access_grants_inventory_extended", 0)
    grain = _grains()["access_grants_inventory_extended"]
    _assert_grain_unique(raw, grain)
    _assert_no_status_column(raw)

    branch_tables = {
        "SCHEMA": "information_schema__schema_privileges",
        "CONNECTION": "information_schema__connection_privileges",
        "CREDENTIAL": "information_schema__credential_privileges",
        "EXTERNAL_LOCATION": "information_schema__external_location_privileges",
    }
    groups: dict[tuple, int] = defaultdict(int)
    for scope, table in branch_tables.items():
        for r in _raw(table):
            groups[(scope, r["privilege_type"], r["grantee"])] += 1
    expected = {(scope, priv, _mask(raw_grantee)): count for (scope, priv, raw_grantee), count in groups.items()}
    actual = {(r["object_scope"], r["privilege_type"], r["GRANTEE"]): r["grant_count"] for r in raw}
    assert actual == expected

    # every one of the 4 object_scope literals (one per UNION ALL source table) present.
    assert {r["object_scope"] for r in raw} == {"SCHEMA", "CONNECTION", "CREDENTIAL", "EXTERNAL_LOCATION"}

    assert actual[("SCHEMA", "USE_SCHEMA", "gv_grantee1@example.com")] == 1
    assert actual[("CONNECTION", "USE_CONNECTION", gv.GUID_EXAMPLE_2)] == 1, "GUID grantee passes through"
    assert actual[("CREDENTIAL", "READ_FILES", "gv_operator")] == 1
    assert actual[("EXTERNAL_LOCATION", "WRITE_FILES", "gv_grantee2@example.com")] == 1

    # See test_access_grants_inventory above for why branch coverage is checked on the raw source.
    raw_grantees = {k[2] for k in groups}
    assert any("@" in g for g in raw_grantees)
    assert any(_GUID_RE.match(g) for g in raw_grantees)
    assert any("@" not in g and not _GUID_RE.match(g) for g in raw_grantees)
    assert not any(g is None or g == "__REDACTED__" for g in raw_grantees)  # branch gap, see docstring

    grantees = {r["GRANTEE"] for r in raw}
    assert grantees == {_mask(g) for g in raw_grantees}

    _assert_sorted(raw, lambda r: (r["object_scope"], r["privilege_type"], r["GRANTEE"]))


# ---------------------------------------------------------------------------------------------
# access_tags_inventory -- no status (pure inventory); column_tags UNION ALL table_tags UNION ALL
# schema_tags UNION ALL volume_tags, each explicitly GROUP BY (object_scope literal, TAG_NAME,
# TAG_VALUE) -- no masked column at all here.
# ---------------------------------------------------------------------------------------------
def test_access_tags_inventory():
    raw = dbutil.rows("access_tags_inventory", 0)
    grain = _grains()["access_tags_inventory"]
    _assert_grain_unique(raw, grain)
    _assert_no_status_column(raw)

    branch_tables = {
        "COLUMN": "information_schema__column_tags",
        "TABLE": "information_schema__table_tags",
        "SCHEMA": "information_schema__schema_tags",
        "VOLUME": "information_schema__volume_tags",
    }
    counter: Counter = Counter()
    for scope, table in branch_tables.items():
        for r in _raw(table):
            counter[(scope, r["tag_name"], r["tag_value"])] += 1
    actual = {(r["object_scope"], r["tag_name"], r["tag_value"]): r["tagged_object_count"] for r in raw}
    assert actual == dict(counter)

    assert {r["object_scope"] for r in raw} == {"COLUMN", "TABLE", "SCHEMA", "VOLUME"}

    # by-construction spot checks: 9 column_tags rows share (pii, true) -- access_pii_propagation_
    # untagged's 5 tagged sources (match_src/crit_src/sys_src/indirect_src/win_src) plus
    # access_column_lineage_sensitive_reach's 4 tagged sources (ok_src/warn_src/crit_src/win_src;
    # P4-FIXES28 -- see tests/fixtures/governance.py's two _*_rows() builders).
    assert actual[("COLUMN", "pii", "true")] == 9
    assert actual[("COLUMN", "classification", "confidential")] == 1
    assert actual[("COLUMN", "owner", "restricted")] == 1
    assert actual[("TABLE", "team", "data-eng")] == 1
    assert actual[("SCHEMA", "pii", "true")] == 1
    assert actual[("SCHEMA", "owner", "team-x")] == 1
    assert actual[("VOLUME", "pii", "true")] == 1
    assert actual[("VOLUME", "sensitivity", "high")] == 1
    assert actual[("VOLUME", "team", "data-eng")] == 1

    _assert_sorted(raw, lambda r: (r["object_scope"], r["tag_name"], r["tag_value"]))


# ---------------------------------------------------------------------------------------------
# access_views_inventory -- no status (pure inventory); reads information_schema.views verbatim,
# no GROUP BY. references_mask_or_filter is a TEXT heuristic over upper(VIEW_DEFINITION).
# ---------------------------------------------------------------------------------------------
def test_access_views_inventory():
    raw = dbutil.rows("access_views_inventory", 0)
    grain = _grains()["access_views_inventory"]
    _assert_grain_unique(raw, grain)
    _assert_no_status_column(raw)

    expected = _raw("information_schema__views")
    assert len(raw) == len(expected)
    by_name = {r["view_name"]: r for r in raw}
    for e in expected:
        row = by_name[e["table_name"]]
        assert row["view_catalog"] == e["table_catalog"] and row["view_schema"] == e["table_schema"]
        assert row["is_materialized"] == e["is_materialized"]
        assert row["is_updatable"] == e["is_updatable"]
        assert row["is_insertable_into"] == e["is_insertable_into"]
        assert row["definition_chars"] == len(e["view_definition"]), "definition_chars = LENGTH(VIEW_DEFINITION)"
        text = e["view_definition"].upper()
        expect_mask = ("MASK" in text) or ("FILTER" in text)
        assert row["references_mask_or_filter"] == expect_mask, e["table_name"]

    # gv_view_masked ("SELECT MASK(gv_ssn) FROM ...") -> True by an explicit MASK( call.
    assert by_name["gv_view_masked"]["references_mask_or_filter"] is True
    # gv_view_plain's definition references table gv_masked_tbl -- "MASKED_TBL" itself contains
    # the substring "MASK", so this TEXT heuristic flags it True too, for an unrelated reason --
    # exactly the caveat the vendored header calls out ("a definition can mention the words for
    # unrelated reasons"). Computed from the raw definition text above, not assumed from naming.
    assert by_name["gv_view_plain"]["references_mask_or_filter"] is True
    # gv_view_matview ("SELECT * FROM gv_catalog.gv_schema.gv_tbl1") is the one true negative in
    # this fixture, and also the only materialized view (is_materialized = 'YES').
    assert by_name["gv_view_matview"]["references_mask_or_filter"] is False
    assert by_name["gv_view_matview"]["is_materialized"] == "YES"
    assert by_name["gv_view_masked"]["is_materialized"] == "NO"

    _assert_sorted(raw, lambda r: (r["view_catalog"], r["view_schema"], r["view_name"]))


# ---------------------------------------------------------------------------------------------
# access_volumes_inventory -- HAS a status column (is_finding: true); CASE has only CRITICAL/WARN/
# ELSE-OK branches, no NOT_ASSESSED branch at all (structurally unreachable). One row per
# information_schema.volumes row, LEFT JOINed to a volume_tags-derived, pre-grouped CTE.
# ---------------------------------------------------------------------------------------------
def test_access_volumes_inventory():
    raw = dbutil.rows("access_volumes_inventory", 0)
    grain = _grains()["access_volumes_inventory"]
    _assert_grain_unique(raw, grain)

    statuses = {r["status"] for r in raw}
    bad = statuses - STATUS_VALUES
    assert not bad, f"status values outside the enum: {bad}"
    assert "NOT_ASSESSED" not in statuses, "no NOT_ASSESSED branch in this id's CASE -- structurally unreachable"
    assert statuses == {"CRITICAL", "WARN", "OK"}, "all 3 reachable bands must appear on this fixture"

    vols = _raw("information_schema__volumes")
    tag_counts: Counter = Counter()
    tag_names: dict[tuple, set] = defaultdict(set)
    for t in _raw("information_schema__volume_tags"):
        key = (t["catalog_name"], t["schema_name"], t["volume_name"])
        tag_counts[key] += 1
        tag_names[key].add(t["tag_name"])

    by_name = {r["volume_name"]: r for r in raw}
    for v in vols:
        key = (v["volume_catalog"], v["volume_schema"], v["volume_name"])
        tc = tag_counts.get(key, 0)
        expect_status = "CRITICAL" if (tc == 0 and v["volume_type"] == "EXTERNAL") else ("WARN" if tc == 0 else "OK")
        row = by_name[v["volume_name"]]
        assert row["tag_count"] == tc, v["volume_name"]
        assert row["status"] == expect_status, v["volume_name"]
        if tc == 0:
            assert row["tag_names"] is None
        else:
            # governance.py never attaches 2 raw tag rows to the same volume, so list_distinct's
            # dedup is only ever proven in its single-element form here -- see this module's
            # docstring "fixture gaps" note (real multi-value collapse is proven in
            # test_access_delta_sharing_exposure on the same array_join(collect_set(...)) construct).
            assert row["tag_names"] == ", ".join(sorted(tag_names[key])), v["volume_name"]

    crit = by_name["gv_vol_ext_untagged"]
    assert crit["status"] == "CRITICAL" and crit["tag_count"] == 0 and crit["volume_type"] == "EXTERNAL"
    warn = by_name["gv_vol_mgd_untagged"]
    assert warn["status"] == "WARN" and warn["tag_count"] == 0 and warn["volume_type"] == "MANAGED"
    ok = by_name["gv_vol_ext_sensitive"]
    assert ok["status"] == "OK" and ok["tag_count"] == 1 and ok["tag_names"] == "pii"

    # volume_owner mask branches: GUID passthrough (gv_vol_ext_sensitive), email (gv_vol_ext_untagged,
    # gv_vol_mgd_sensitive), plain (gv_vol_mgd_untagged, gv_vol_mgd_tagged_nonsensitive).
    assert ok["volume_owner"] == gv.GUID_EXAMPLE_1
    assert crit["volume_owner"] == "gv_vol_owner@example.com"
    assert warn["volume_owner"] == "gv_vol_owner2"

    _assert_sorted(raw, lambda r: (STATUS_RANK[r["status"]], r["volume_catalog"], r["volume_schema"], r["volume_name"]))


# ---------------------------------------------------------------------------------------------
# access_pii_outside_tables -- HAS a status column (is_finding: true); 2-way UNION ALL (VOLUME from
# volumes+volume_tags, SCHEMA from schema_tags), outer WHERE (is_sensitive=1 OR untagged=1) makes
# OK structurally unreachable, and the CASE has no NOT_ASSESSED branch either.
# ---------------------------------------------------------------------------------------------
def test_access_pii_outside_tables():
    raw = dbutil.rows("access_pii_outside_tables", 0)
    grain = _grains()["access_pii_outside_tables"]
    _assert_grain_unique(raw, grain)

    statuses = {r["status"] for r in raw}
    bad = statuses - STATUS_VALUES
    assert not bad, f"status values outside the enum: {bad}"
    assert "OK" not in statuses, "outer WHERE (is_sensitive=1 OR untagged=1) drops every OK-eligible row"
    assert "NOT_ASSESSED" not in statuses, "no NOT_ASSESSED branch in this id's CASE"
    assert statuses == {"CRITICAL", "WARN"}, "both reachable bands must appear on this fixture"

    assert {r["object_type"] for r in raw} == {"VOLUME", "SCHEMA"}, "both UNION ALL branches must appear"

    by_key = {(r["object_type"], r["object_name"]): r for r in raw}
    # Anchor (review fix item 1): prove both excluded rows genuinely exist in the raw parquet
    # BEFORE asserting their absence from the output -- an absence check with no anchor passes
    # just as well if the underlying fixture row is deleted, which proves nothing (the exact
    # pattern T-16 hit four times).
    assert any(
        v["volume_name"] == "gv_vol_mgd_tagged_nonsensitive" and v["volume_type"] == "MANAGED"
        for v in _raw("information_schema__volumes")
    ), "gv_vol_mgd_tagged_nonsensitive must genuinely exist in the raw volumes parquet"
    assert any(
        s["schema_name"] == "gv_plain_schema" for s in _raw("information_schema__schema_tags")
    ), "gv_plain_schema must genuinely exist in the raw schema_tags parquet"

    assert ("VOLUME", "gv_vol_mgd_tagged_nonsensitive") not in by_key, "tagged-but-non-sensitive volume dropped by outer WHERE"
    assert ("SCHEMA", "gv_plain_schema") not in by_key, "non-sensitive schema never enters the SCHEMA branch's own WHERE"

    crit_sensitive_ext = by_key[("VOLUME", "gv_vol_ext_sensitive")]
    assert crit_sensitive_ext["status"] == "CRITICAL" and crit_sensitive_ext["finding"].startswith("sensitive-tagged")
    assert crit_sensitive_ext["tags"] == "pii", "array_join(collect_set(...)) populated with the one true tag"

    crit_untagged_ext = by_key[("VOLUME", "gv_vol_ext_untagged")]
    assert crit_untagged_ext["status"] == "CRITICAL" and crit_untagged_ext["finding"].startswith("untagged")
    assert crit_untagged_ext["tags"] is None

    warn_schema = by_key[("SCHEMA", "gv_pii_schema")]
    assert warn_schema["status"] == "WARN"
    assert warn_schema["tags"] == "pii=true", "schema branch's concat(TAG_NAME,'=',TAG_VALUE) aggregation, populated"

    warn_sensitive_mgd = by_key[("VOLUME", "gv_vol_mgd_sensitive")]
    assert warn_sensitive_mgd["status"] == "WARN" and warn_sensitive_mgd["tags"] == "sensitivity"

    warn_untagged_mgd = by_key[("VOLUME", "gv_vol_mgd_untagged")]
    assert warn_untagged_mgd["status"] == "WARN" and warn_untagged_mgd["tags"] is None

    _assert_sorted(
        raw,
        lambda r: (STATUS_RANK[r["status"]], r["object_type"], r["catalog_name"], r["schema_name"], r["object_name"]),
    )


# ---------------------------------------------------------------------------------------------
# access_delta_sharing_exposure -- HAS a status column (is_finding: true); one row per share
# (recips/tbls/schs are all pre-grouped by SHARE_NAME before the LEFT JOINs). CASE has only
# CRITICAL/WARN/ELSE-OK branches, no NOT_ASSESSED. recipients is
# array_join(collect_set(<masked RECIPIENT_NAME>)), the genuine multi-value dedup proof for this
# batch's PLAN.md 5.4 translation rule.
# ---------------------------------------------------------------------------------------------
def test_access_delta_sharing_exposure():
    raw = dbutil.rows("access_delta_sharing_exposure", 0)
    grain = _grains()["access_delta_sharing_exposure"]
    _assert_grain_unique(raw, grain)

    statuses = {r["status"] for r in raw}
    bad = statuses - STATUS_VALUES
    assert not bad, f"status values outside the enum: {bad}"
    assert "NOT_ASSESSED" not in statuses, "no NOT_ASSESSED branch in this id's CASE"
    assert statuses == {"CRITICAL", "WARN", "OK"}, "all 3 reachable bands must appear on this fixture"

    by_name = {r["share_name"]: r for r in raw}

    crit = by_name["gv_share_crit"]
    assert crit["status"] == "CRITICAL" and crit["recipient_count"] == 10 and crit["shared_table_count"] == 0
    warn = by_name["gv_share_warn"]
    assert warn["status"] == "WARN" and warn["recipient_count"] == 2 and warn["shared_table_count"] == 2
    ok = by_name["gv_share_ok"]
    assert ok["status"] == "OK" and ok["recipient_count"] == 0 and ok["recipients"] is None

    # Multi-value list proof (this module's docstring "fixture gaps" note): gv_share_warn has TWO
    # distinct raw recipients PLUS one raw-duplicate row of gv_recipient_warn_0 (same grantor,
    # recipient, share, privilege_type -- 3 raw rows, 2 distinct people). Under DEC-66.3 every
    # OTHER recipient here is genuinely distinct (unlike the old DEC-48 2-char-prefix mask, which
    # collapsed unrelated people onto one shared string), so this raw duplicate is the suite's
    # only remaining proof that array_join(collect_set(...)) (-> array_to_string(list_distinct(
    # list(...))) on DuckDB, PLAN.md 5.4) still deduplicates: 3 raw rows collapse to 2 masked
    # entries, without merging the 2 different people together.
    warn_raw_recipients = [r["recipient_name"] for r in _raw("information_schema__share_recipient_privileges")
                            if r["share_name"] == "gv_share_warn"]
    assert len(warn_raw_recipients) == 3 and len(set(warn_raw_recipients)) == 2, \
        "3 raw rows, one a duplicate, 2 DISTINCT raw recipients"
    expected_warn_masked = {_mask(x) for x in warn_raw_recipients}
    assert len(expected_warn_masked) == 2, "DEC-66.3's hash-derived id must keep 2 distinct people distinct"
    assert set(warn["recipients"].split(", ")) == expected_warn_masked
    assert len(warn["recipients"].split(", ")) == 2, "collect_set must dedup the raw-duplicate row"

    crit_raw_recipients = [r["recipient_name"] for r in _raw("information_schema__share_recipient_privileges")
                            if r["share_name"] == "gv_share_crit"]
    assert len(crit_raw_recipients) == 10 and len(set(crit_raw_recipients)) == 10, "10 DISTINCT raw recipients"
    expected_crit_masked = {_mask(x) for x in crit_raw_recipients}
    assert len(expected_crit_masked) == 10, "DEC-66.3's hash-derived id must keep all 10 people distinct"
    assert set(crit["recipients"].split(", ")) == expected_crit_masked
    assert len(crit["recipients"].split(", ")) == 10, "collect_set must not duplicate an entry"

    # share_owner mask branches: GUID passthrough (gv_share_crit), email (gv_share_warn), plain (gv_share_ok).
    assert crit["share_owner"] == gv.GUID_EXAMPLE_1
    assert warn["share_owner"] == "gv_share_owner@example.com"
    assert ok["share_owner"] == "gv_share_owner2"

    # Named fixture gap (this module's docstring note 2): the shared_table_count >= 100 CRITICAL
    # branch, independent of recipient_count, is never exercised -- confirmed directly against the
    # raw parquet rather than merely asserted in prose.
    table_usage_counts = Counter(r["share_name"] for r in _raw("information_schema__table_share_usage"))
    assert max(table_usage_counts.values(), default=0) < 100, "no share crosses :crit_shared_tables independently"

    _assert_sorted(raw, lambda r: (STATUS_RANK[r["status"]], -r["recipient_count"], -r["shared_table_count"]))


# ---------------------------------------------------------------------------------------------
# table_inventory_type -- domain storage, not governance_access; no status (pure inventory); reads
# information_schema.tables, GROUP BY (table_catalog, table_schema, table_type,
# data_source_format). Shared source table: ports.py also writes information_schema__tables rows
# (pt_ prefixed catalogs; lakeflow.py writes no information_schema rows at all) -- filtered out
# below by (table_catalog, table_schema) PAIR, per DEC-15 ("assertions filter on the builder's own
# ids, never on workspace alone").
# ---------------------------------------------------------------------------------------------
def test_table_inventory_type():
    raw_all = dbutil.rows("table_inventory_type", 0)
    grain = _grains()["table_inventory_type"]
    _assert_grain_unique(raw_all, grain)  # grain must hold across the WHOLE built table, not just gv_ rows
    _assert_no_status_column(raw_all)

    # Scope by (table_catalog, table_schema) PAIR, not catalog alone (review fix item 3): "system"
    # is the real Databricks catalog name and information_schema__tables is a multi-builder source
    # (governance.py + ports.py today) -- scoping on catalog alone would silently fold in any
    # future builder's own table_catalog='system' rows under a different schema. The 3 pairs below
    # are exactly what _dead_table_candidates_rows() wrote.
    gv_schema_pairs = {
        (gv.GV_CATALOG, gv.GV_SCHEMA),
        (gv.GV_CATALOG, "information_schema"),
        ("system", gv.GV_SCHEMA),
    }
    raw = [r for r in raw_all if (r["table_catalog"], r["table_schema"]) in gv_schema_pairs]
    assert raw, "governance.py's own information_schema.tables rows must survive into this id"

    info_tables = [r for r in _raw("information_schema__tables") if (r["table_catalog"], r["table_schema"]) in gv_schema_pairs]
    counter = Counter((r["table_catalog"], r["table_schema"], r["table_type"], r["data_source_format"]) for r in info_tables)
    actual = {
        (r["table_catalog"], r["table_schema"], r["table_type"], r["data_source_format"]): r["table_count"]
        for r in raw
    }
    # No hard-coded `== 9` row-count pin (review fix item 3: it added nothing over this raw-derived
    # comparison, and was itself fragile to the same multi-builder source growing over time).
    assert actual == dict(counter)

    assert actual[(gv.GV_CATALOG, gv.GV_SCHEMA, "MANAGED", "DELTA")] == 5, (
        "gv_dead_crit, gv_dead_ok, gv_dead_notassessed, gv_alive_excluded, gv_dead_win_boundary "
        "(all MANAGED); gv_dead_warn is EXTERNAL, counted separately below"
    )
    assert actual[(gv.GV_CATALOG, gv.GV_SCHEMA, "EXTERNAL", "DELTA")] == 1, "gv_dead_warn (EXTERNAL)"
    assert actual[(gv.GV_CATALOG, gv.GV_SCHEMA, "VIEW", "DELTA")] == 1, "gv_view_excluded"
    assert actual[(gv.GV_CATALOG, "information_schema", "MANAGED", "DELTA")] == 1, "gv_infoschema_excluded"
    assert actual[("system", gv.GV_SCHEMA, "MANAGED", "DELTA")] == 1, "gv_sys_excluded"

    # Review fix item 2: order the FULL returned sequence (raw_all), not the gv_-filtered
    # subsequence -- a filtered subsequence can look sorted by accident under DuckDB's
    # hash-aggregate bucket order even with no ORDER BY at all (T-16 proved this empirically on
    # the same class of bug). The full 9-row table_inventory_type output happens to already be
    # sorted by (table_catalog, table_schema, table_type) today.
    _assert_sorted(raw_all, lambda r: (r["table_catalog"], r["table_schema"], r["table_type"]))
