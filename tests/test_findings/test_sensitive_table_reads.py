"""tests/test_findings/test_sensitive_table_reads.py -- P3-SENSITIVE.

Proves, against tests/fixtures/sensitive.py's `sn_` scenario rows, that
findings.f_access_sensitive_table_reads (grain [workspace_id, table_catalog, table_schema,
table_name, reader] -- the fixture writes every read at the same single workspace, "1111", so
adding workspace_id to the grain never splits an existing scenario's row count here)
has: the right grain, worst-first ordering, the status enum, window behaviour at 7 / 30 / 90 days
(and the empty window-0 case), a table with no tag/classification row never appearing (the JOIN
itself gates rows, not just a WHERE), a read on the 'system' catalog never appearing even when
tagged, the DEC-66.3 identity mask on both `reader` and `table_owner` (NULL / GUID / hash-derived
branches), the owner-exempt rule (a table's own owner is never flagged regardless of volume), the
:warn_reads boundary (exactly at the threshold is OK, one more is WARN), and sensitivity_basis
carrying every source that matched a table (table_tag / column_tag / classification).

Every expectation below is computed by hand from tests/fixtures/sensitive.py's own scenario
comment block, or re-derived independently (the identity mask via `_mask()`, an independent
Python re-implementation of the SQL CASE, the same pattern tests/test_findings/
test_governance_inventories.py's own `_mask()` uses) -- never read back from the model under test.

NOTE on the query's own account-wide NOT_ASSESSED sentinel row (status='NOT_ASSESSED',
not_assessed_reason='no_sensitivity_signal', fired only when NO builder anywhere in the shared
fixture DB has tagged or classified anything sensitive): tests/fixtures/governance.py's own PII
scenario rows (tag_name/tag_value 'pii' / 'confidential' / 'restricted' on its `gv_pii_*` tables)
guarantee that condition is never true in this shared suite, so it cannot be exercised through
dbutil.rows() here -- proven instead, once, by a standalone DuckDB smoke test against an EMPTY
fixture DB (see this task's write-up); test_sentinel_never_fires_in_shared_fixture below asserts
the shared-suite side of that same fact (a live regression guard, not a copy of the smoke test).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "access_sensitive_table_reads.yml"

QID = "access_sensitive_table_reads"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
WARN_READS = 20  # header default; no config/thresholds.yml override for this id
SN_CATALOG = "sn_catalog"

GUID_EXAMPLE = "aaaa1111-bbbb-2222-cccc-3333dddd4444"


def _mask(v):
    """`reader`/`table_owner` go through the mask_user() dbt macro, off by default, so every
    value here -- including NULL/'__REDACTED__'/a GUID -- passes through raw."""
    return v


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _own(window_days: int) -> list[dict]:
    """This file's own rows: table_catalog == 'sn_catalog' (the sentinel row and every other
    builder's rows are excluded by this filter -- the sentinel carries table_catalog=NULL)."""
    return [r for r in dbutil.rows(QID, window_days) if r["table_catalog"] == SN_CATALOG]


def _by_table(window_days: int) -> dict:
    return {r["table_name"]: r for r in _own(window_days)}


# =====================================================================================================
# Structural checks (whole-DB, every builder pooled -- matches the pattern every sibling
# tests/test_findings/test_*.py file uses for these)
# =====================================================================================================
def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        assert out, w
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"w={w}: duplicate grain rows"


def test_window_days_zero_is_empty():
    assert dbutil.rows(QID, 0) == []


def test_status_enum_and_not_assessed_reason():
    for w in WINDOWS:
        for r in dbutil.rows(QID, w):
            assert r["status"] in STATUS_VALUES, (w, r["table_name"], r["status"])
            if r["status"] == "NOT_ASSESSED":
                assert r["not_assessed_reason"], (w, r["table_name"], "NOT_ASSESSED must carry a reason")
            else:
                assert r["not_assessed_reason"] is None, (w, r["table_name"], r["not_assessed_reason"])


def test_worst_first_ordering():
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        order = [rank[r["status"]] for r in out]
        assert order == sorted(order), (w, "status must be worst-first")


def test_sentinel_never_fires_in_shared_fixture():
    """See the module docstring's NOTE: governance.py's own PII tag rows keep the account-wide
    sensitivity signal non-zero across this whole shared suite, so the sentinel is provably absent
    here (a live guard against that fact silently changing), never proof of the sentinel's own
    logic -- that is proven separately by an empty-fixture smoke test."""
    for w in WINDOWS:
        assert not any(r["status"] == "NOT_ASSESSED" for r in dbutil.rows(QID, w)), w


# =====================================================================================================
# sn_owner_tbl -- OK: the table's own owner reads it 25x (above :warn_reads=20), still OK.
# =====================================================================================================
def test_owner_reading_own_table_is_ok_regardless_of_volume():
    r = _by_table(7)["sn_owner_tbl"]
    assert r["status"] == "OK"
    assert r["read_count"] == 25
    assert r["reader"] == _mask("sn_owner_person@example.com")
    assert r["table_owner"] == _mask("sn_owner_person@example.com")
    assert r["reader"] == r["table_owner"]  # same raw identity -> identical mask
    assert r["sensitivity_basis"] == "table_tag"


# =====================================================================================================
# sn_warn_tbl -- WARN: a non-owner reads it 21x (> :warn_reads=20), spread 7 reads each over 3
# distinct calendar days (D7=AS_OF-3d, AS_OF-2d, AS_OF-1d, i minutes apart -- hand-derived from
# AS_OF=2026-09-21 12:00:00 per tests/fixtures/sensitive.py's own docstring, never read back from
# the model): proves first_read_time/last_read_time/distinct_read_days independently of read_count.
# =====================================================================================================
def test_non_owner_over_threshold_is_warn():
    r = _by_table(7)["sn_warn_tbl"]
    assert r["status"] == "WARN"
    assert r["read_count"] == 21
    assert r["reader"] == _mask("sn_warn_reader@example.com")
    assert r["table_owner"] == _mask("sn_warn_owner@example.com")
    assert r["reader"] != r["table_owner"]
    assert r["sensitivity_basis"] == "column_tag"
    # 3 days x 7 reads, i minutes apart: earliest is day D7=AS_OF-3d at +0min, latest is
    # AS_OF-1d at +6min.
    assert r["distinct_read_days"] == 3
    assert r["first_read_time"] == datetime(2026, 9, 18, 12, 0, 0)
    assert r["last_read_time"] == datetime(2026, 9, 20, 12, 6, 0)


# =====================================================================================================
# sn_viaview_tbl -- excluded at every window: 21 reads by a non-owner, but every lineage row is
# direct_access=False (view-expansion) -- table_reads' own filter drops all of them.
# =====================================================================================================
def test_view_expansion_reads_never_count_against_the_base_table():
    for w in WINDOWS:
        assert "sn_viaview_tbl" not in _by_table(w)


# =====================================================================================================
# sn_okunder_tbl -- OK: a non-owner reads it EXACTLY 20x, the :warn_reads boundary itself
# (read_count > :warn_reads is false at 20 -- proves '>' not '>=').
# =====================================================================================================
def test_exactly_at_warn_reads_threshold_is_ok():
    r = _by_table(7)["sn_okunder_tbl"]
    assert r["status"] == "OK"
    assert r["read_count"] == WARN_READS == 20
    assert r["reader"] == _mask("sn_okunder_reader@example.com")
    assert r["sensitivity_basis"] == "classification"


# =====================================================================================================
# sn_guid_tbl -- WARN: reader is a service-principal GUID (DEC-66.3 branch 2, passthrough).
# =====================================================================================================
def test_guid_reader_passes_through_unmasked():
    r = _by_table(7)["sn_guid_tbl"]
    assert r["status"] == "WARN"
    assert r["read_count"] == 21
    assert r["reader"] == GUID_EXAMPLE  # unmasked, verbatim
    assert r["table_owner"] == _mask("sn_guid_owner@example.com")


# =====================================================================================================
# sn_null_tbl -- WARN: reader (created_by) is NULL (DEC-66.3 branch 1, passthrough); NULL never
# equals a non-NULL owner, so is_owner is false and this still flags.
# =====================================================================================================
def test_null_reader_passes_through_and_is_never_the_owner():
    r = _by_table(7)["sn_null_tbl"]
    assert r["status"] == "WARN"
    assert r["read_count"] == 21
    assert r["reader"] is None
    assert r["table_owner"] == _mask("sn_null_owner@example.com")


# =====================================================================================================
# sn_noowner_tbl -- WARN: no information_schema.tables row at all -> table_owner NULL (owner
# unknown) -> judged as a non-owner read (the safer direction), never OK by default.
# =====================================================================================================
def test_unknown_owner_table_is_judged_non_owner():
    r = _by_table(7)["sn_noowner_tbl"]
    assert r["status"] == "WARN"
    assert r["read_count"] == 21
    assert r["table_owner"] is None
    assert r["reader"] == _mask("sn_noowner_reader@example.com")


# =====================================================================================================
# sn_multi_tbl -- both a table_tags AND a column_tags row match -> sensitivity_basis carries both,
# comma-joined, order not guaranteed (array_join(collect_set(...)), same caveat as
# cost_daily_spikes' top_skus) -- compared here as a set.
# =====================================================================================================
def test_sensitivity_basis_combines_every_matching_source():
    r = _by_table(7)["sn_multi_tbl"]
    assert r["status"] == "OK"
    assert r["read_count"] == 3
    assert set(x.strip() for x in r["sensitivity_basis"].split(",")) == {"table_tag", "column_tag"}


# =====================================================================================================
# sn_notsensitive_tbl / sn_sys_tbl -- excluded at every window: no tag/classification row at all
# (the JOIN itself gates rows), and a read on the 'system' catalog even when tagged.
# =====================================================================================================
def test_untagged_table_never_appears():
    for w in WINDOWS:
        assert "sn_notsensitive_tbl" not in _by_table(w)


def test_system_catalog_read_never_appears_even_if_tagged():
    for w in WINDOWS:
        names = {r["table_name"] for r in dbutil.rows(QID, w) if r["table_catalog"] == "system"}
        assert "sn_sys_tbl" not in names


# =====================================================================================================
# sn_win_tbl -- window boundary: one read per anchor, own reader per anchor (D7/D30/D90 present
# only once their own window includes that anchor; DTODAY/DOLD never present at any window).
# =====================================================================================================
def test_window_boundaries():
    def win_rows(window_days: int) -> dict:
        return {
            r["reader"]: r for r in _own(window_days) if r["table_name"] == "sn_win_tbl"
        }

    w7 = win_rows(7)
    assert set(w7) == {_mask("sn_win_reader_t7@example.com")}

    w30 = win_rows(30)
    assert set(w30) == {_mask("sn_win_reader_t7@example.com"), _mask("sn_win_reader_t30@example.com")}

    w90 = win_rows(90)
    assert set(w90) == {
        _mask("sn_win_reader_t7@example.com"),
        _mask("sn_win_reader_t30@example.com"),
        _mask("sn_win_reader_t90@example.com"),
    }
    # DTODAY / DOLD anchors are never in ANY window's result (event_date < current_date() excludes
    # today; DOLD is older than the widest window tested).
    for w in WINDOWS:
        readers = set(win_rows(w))
        assert _mask("sn_win_reader_ttoday@example.com") not in readers
        assert _mask("sn_win_reader_told90@example.com") not in readers
    for r in w90.values():
        assert r["status"] == "OK"
        assert r["read_count"] == 1
