"""tests/test_materiality.py

config/materiality.yml (app/core/materiality.py, app/core/config.py) -- a CRITICAL/WARN row whose
named column sits under the check's configured floor reads OK instead and carries below_floor=True,
so status_counts/worst-status/the sort order/a WARN+CRITICAL money sum all ignore it. NOT_ASSESSED
and OK rows are never touched by a floor. GET /api/finding exports the EFFECTIVE status as `status`
(the row's raw value survives as `status_raw`) plus a real `below_floor` column, both computed in
SQL (app/core/data.py's read_finding), never in per-row Python.

    TestFloorHelpers          pure app/core/materiality.py functions -- no database.
    TestFloorAppliedInSql     status_expr/below_floor_expr run against a tiny in-memory table --
                              proves a CRITICAL/WARN row under the floor reads OK while a
                              NOT_ASSESSED row under the same column never does, and that
                              app/core/data.py's ORDER BY ranks on the floored status.
    TestConfigValidation      app/core/config._validate_materiality -- no database.
    TestApiFloor              a fixture check that HAS a floor (compute_warehouse_idle_minutes,
                              $50 est_wasted_usd_list) against tests/db_audit_test.duckdb: counts
                              drop, rows carry status/status_raw/below_floor, the aggregate sum
                              excludes them. A second check with NO floor entry (cost_by_job) is
                              asserted unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.app import app  # noqa: E402
from app.core import config as app_config  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import materiality  # noqa: E402

client = TestClient(app)

FLOOR_QUERY_ID = "compute_warehouse_idle_minutes"
FLOOR_COLUMN = "est_wasted_usd_list"
NO_FLOOR_QUERY_ID = "cost_by_job"
WINDOW = 30


def _require_table(query_id: str) -> None:
    con = duckdb.connect(str(ROOT / "tests" / "db_audit_test.duckdb"), read_only=True)
    try:
        n = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'findings' AND table_name = ?",
            [f"f_{query_id}"],
        ).fetchone()[0]
    finally:
        con.close()
    if not n:
        pytest.skip(f"findings.f_{query_id} not built in this fixture db")


# -------------------------------------------------------------------------------------------
# Pure helpers -- no database.
# -------------------------------------------------------------------------------------------


class TestFloorHelpers:
    def test_floor_for_known_and_unknown(self):
        floor = materiality.floor_for(FLOOR_QUERY_ID)
        assert floor is not None
        assert floor.column == FLOOR_COLUMN
        assert floor.min == 50
        assert materiality.floor_for("cost_by_job") is None

    def test_applicable_floor_requires_status_and_floor_column(self):
        # Both the floor's own column AND a "status" column must be present -- a stale config
        # entry, or a table that dropped its status band, is unaffected rather than erroring.
        assert materiality.applicable_floor(FLOOR_QUERY_ID, ["status", FLOOR_COLUMN]) is not None
        assert materiality.applicable_floor(FLOOR_QUERY_ID, [FLOOR_COLUMN]) is None  # no "status"
        assert materiality.applicable_floor(FLOOR_QUERY_ID, ["status"]) is None  # no floor column
        assert materiality.applicable_floor("cost_by_job", ["status", "net_usage_quantity"]) is None

    def test_status_expr_no_floor_is_bare_column(self):
        assert materiality.status_expr("cost_by_job", ["status", "net_usage_quantity"]) == 't."status"'

    def test_status_expr_missing_column_is_bare_column(self):
        # The table has `status` but not the floor's own column -- unaffected, never an error.
        assert materiality.status_expr(FLOOR_QUERY_ID, ["status"]) == 't."status"'

    def test_status_expr_applies_floor_only_to_critical_or_warn(self):
        expr = materiality.status_expr(FLOOR_QUERY_ID, ["status", FLOOR_COLUMN])
        assert expr == (
            f"CASE WHEN t.\"status\" IN ('CRITICAL', 'WARN') AND t.\"{FLOOR_COLUMN}\" < 50 "
            'THEN \'OK\' ELSE t."status" END'
        )

    def test_below_floor_expr(self):
        assert materiality.below_floor_expr("cost_by_job", ["status"]) is None
        expr = materiality.below_floor_expr(FLOOR_QUERY_ID, ["status", FLOOR_COLUMN])
        assert expr == f"t.\"status\" IN ('CRITICAL', 'WARN') AND t.\"{FLOOR_COLUMN}\" < 50"

    def test_floors_are_cached_until_the_file_changes(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        monkeypatch.setenv("AUDIT_CONFIG_DIR", str(cfg_dir))
        app_config.save_materiality({FLOOR_QUERY_ID: {
            "column": FLOOR_COLUMN, "min": 10, "unit": "USD", "label": "x",
        }})
        first = materiality._floors()
        assert materiality._floors() is first  # same object -- no re-read/re-validate
        app_config.save_materiality({FLOOR_QUERY_ID: {
            "column": FLOOR_COLUMN, "min": 12345, "unit": "USD", "label": "x",
        }})
        second = materiality._floors()
        assert second is not first
        assert second[FLOOR_QUERY_ID].min == 12345


# -------------------------------------------------------------------------------------------
# status_expr/below_floor_expr run as real SQL, plus app/core/data's ORDER BY -- proves the
# CRITICAL/WARN-only guard (Must #2) and the floored sort rank (Must #1) against rows a fixture
# database cannot conveniently provide (a NOT_ASSESSED row under the floor).
# -------------------------------------------------------------------------------------------


class TestFloorScalesWithWindow:
    """A USD floor is per 30 days (config/materiality.yml's header) -- config/materiality.yml is
    unchanged for this, only how status_expr reads the table's own window_days column."""

    def _query_id(self, monkeypatch):
        query_id = "test_floor_scaled_query"
        floor = materiality.Floor(query_id=query_id, column="est_usd_list", min=50, unit="USD", label="x")
        monkeypatch.setattr(materiality, "_floors", lambda: {query_id: floor})
        return query_id

    def test_usd_floor_scales_down_at_a_shorter_window(self, monkeypatch):
        query_id = self._query_id(monkeypatch)
        con = duckdb.connect()
        con.execute(
            'CREATE TABLE f_test_floor_scaled_query '
            "(window_days INTEGER, status VARCHAR, est_usd_list DOUBLE, id VARCHAR)"
        )
        con.execute(
            "INSERT INTO f_test_floor_scaled_query VALUES "
            "(30, 'CRITICAL', 30.0, 'thirty_day'), "
            "(7, 'CRITICAL', 30.0, 'seven_day')"
        )
        cols = [("window_days", "INTEGER"), ("status", "VARCHAR"), ("est_usd_list", "DOUBLE"), ("id", "VARCHAR")]
        select_list = app_core_data._select_list(cols, query_id)
        rows = con.execute(
            f"SELECT {select_list} FROM f_test_floor_scaled_query t ORDER BY id"
        ).df().set_index("id")
        # $50/30d is $11.67 at 7d -- the same $30 row is immaterial at 30d but still flagged at 7d.
        assert rows.loc["thirty_day", "status"] == "OK"
        assert rows.loc["seven_day", "status"] == "CRITICAL"

    def test_non_usd_floor_is_never_scaled(self, monkeypatch):
        query_id = "test_floor_count_query"
        floor = materiality.Floor(query_id=query_id, column="scaling_events", min=20, unit="count", label="x")
        monkeypatch.setattr(materiality, "_floors", lambda: {query_id: floor})
        con = duckdb.connect()
        con.execute(
            'CREATE TABLE f_test_floor_count_query '
            "(window_days INTEGER, status VARCHAR, scaling_events INTEGER, id VARCHAR)"
        )
        con.execute("INSERT INTO f_test_floor_count_query VALUES (7, 'CRITICAL', 15, 'seven_day')")
        cols = [("window_days", "INTEGER"), ("status", "VARCHAR"), ("scaling_events", "INTEGER"), ("id", "VARCHAR")]
        select_list = app_core_data._select_list(cols, query_id)
        rows = con.execute(
            f"SELECT {select_list} FROM f_test_floor_count_query t ORDER BY id"
        ).df().set_index("id")
        # 15 < 20 at ANY window -- a count floor never scales with window_days.
        assert rows.loc["seven_day", "status"] == "OK"


class TestFloorAppliedInSql:
    @staticmethod
    def _table(con):
        con.execute(
            'CREATE TABLE f_test_floor_query '
            "(window_days INTEGER, status VARCHAR, est_wasted_usd_list DOUBLE, id VARCHAR)"
        )
        con.execute(
            "INSERT INTO f_test_floor_query VALUES "
            "(30, 'CRITICAL', 264.0, 'crit_above'), "
            "(30, 'CRITICAL', 5.0, 'crit_below'), "
            "(30, 'WARN', 2.0, 'warn_below'), "
            "(30, 'NOT_ASSESSED', 1.0, 'na_below'), "
            "(30, 'OK', 3.0, 'ok_below')"
        )

    def _query_id(self, monkeypatch):
        # A floor keyed on a query_id that will never collide with a real one, applied via a
        # monkeypatched materiality._floors() -- no config/materiality.yml round-trip needed.
        query_id = "test_floor_query"
        floor = materiality.Floor(
            query_id=query_id, column="est_wasted_usd_list", min=10, unit="USD", label="x",
        )
        monkeypatch.setattr(materiality, "_floors", lambda: {query_id: floor})
        return query_id

    def test_not_assessed_row_under_the_floor_stays_not_assessed(self, monkeypatch):
        query_id = self._query_id(monkeypatch)
        con = duckdb.connect()
        self._table(con)
        cols = [("window_days", "INTEGER"), ("status", "VARCHAR"),
                ("est_wasted_usd_list", "DOUBLE"), ("id", "VARCHAR")]
        select_list = app_core_data._select_list(cols, query_id)
        rows = con.execute(
            f"SELECT {select_list} FROM f_test_floor_query t ORDER BY id"
        ).df().set_index("id")
        # CRITICAL/WARN under the floor read OK and are flagged; NOT_ASSESSED/OK under the SAME
        # floor are untouched -- a floor can only ever move a row TOWARDS a healthier verdict it
        # already earned via a CRITICAL/WARN status, never manufacture one for an unjudged row.
        assert rows.loc["crit_above", "status"] == "CRITICAL"
        assert not rows.loc["crit_above", "below_floor"]
        assert rows.loc["crit_below", ["status", "status_raw", "below_floor"]].tolist() == ["OK", "CRITICAL", True]
        assert rows.loc["warn_below", ["status", "status_raw", "below_floor"]].tolist() == ["OK", "WARN", True]
        assert rows.loc["na_below", "status"] == "NOT_ASSESSED"
        assert not rows.loc["na_below", "below_floor"]
        assert rows.loc["ok_below", "status"] == "OK"
        assert not rows.loc["ok_below", "below_floor"]  # already OK -- the floor suppressed nothing

    def test_order_by_ranks_on_the_floored_status(self, monkeypatch):
        query_id = self._query_id(monkeypatch)
        con = duckdb.connect()
        self._table(con)
        col_names = ["window_days", "status", "est_wasted_usd_list", "id"]
        cols = [(c, "VARCHAR") for c in col_names]
        select_list = app_core_data._select_list(cols, query_id)
        order_sql = app_core_data._order_by_clause(query_id, col_names)
        ids = con.execute(
            f"SELECT {select_list} FROM f_test_floor_query t{order_sql}"
        ).df()["id"].tolist()
        # crit_below/warn_below now read OK (rank 3), so they sort AFTER na_below (NOT_ASSESSED,
        # rank 2) -- the bug this fixes let their raw CRITICAL/WARN rank keep them ahead of it.
        assert ids.index("crit_above") < ids.index("na_below") < ids.index("crit_below")
        assert ids.index("na_below") < ids.index("warn_below")


# -------------------------------------------------------------------------------------------
# config/materiality.yml validation -- no database.
# -------------------------------------------------------------------------------------------


class TestConfigValidation:
    def test_repo_file_loads_clean(self):
        floors = app_config.load_materiality()
        assert FLOOR_QUERY_ID in floors
        assert floors[FLOOR_QUERY_ID] == {
            "column": FLOOR_COLUMN, "min": 50, "unit": "USD",
            "label": "$50 possible waste per 30 days",
        }

    def test_unknown_query_id_rejected(self):
        with pytest.raises(app_config.ConfigError, match="not in app.core.registry"):
            app_config._validate_materiality(
                {"this_query_id_does_not_exist": {
                    "column": "x", "min": 1, "unit": "USD", "label": "y",
                }}
            )

    def test_missing_field_rejected(self):
        with pytest.raises(app_config.ConfigError, match="missing field"):
            app_config._validate_materiality({FLOOR_QUERY_ID: {"column": FLOOR_COLUMN, "min": 50}})

    def test_unknown_field_rejected(self):
        with pytest.raises(app_config.ConfigError, match="unknown field"):
            app_config._validate_materiality(
                {FLOOR_QUERY_ID: {
                    "column": FLOOR_COLUMN, "min": 50, "unit": "USD", "label": "y", "extra": 1,
                }}
            )

    def test_non_positive_min_rejected(self):
        with pytest.raises(app_config.ConfigError, match="positive number"):
            app_config._validate_materiality(
                {FLOOR_QUERY_ID: {"column": FLOOR_COLUMN, "min": 0, "unit": "USD", "label": "y"}}
            )

    def test_bad_column_shape_rejected(self):
        with pytest.raises(app_config.ConfigError, match="column name"):
            app_config._validate_materiality(
                {FLOOR_QUERY_ID: {"column": "Not Valid", "min": 50, "unit": "USD", "label": "y"}}
            )


# -------------------------------------------------------------------------------------------
# API -- a fixture check WITH a floor, and a control check with none.
# -------------------------------------------------------------------------------------------


class TestApiFloor:
    def test_findings_status_counts_reflect_floor(self):
        _require_table(FLOOR_QUERY_ID)
        resp = client.get("/api/findings", params={"window": WINDOW})
        assert resp.status_code == 200
        rows = {r["query_id"]: r for r in resp.json()["findings"]}
        row = rows[FLOOR_QUERY_ID]
        # Raw counts on this fixture are CRITICAL=24/NOT_ASSESSED=47/OK=2/WARN=2; 5 CRITICAL + both
        # WARN rows sit under the $50 floor and read OK instead -- WARN disappears entirely. The
        # 47 NOT_ASSESSED rows carry no est_wasted_usd_list at all (NULL), so they are untouched
        # either way -- see TestFloorAppliedInSql above for a NOT_ASSESSED row that DOES sit under
        # a floor's column. 32 of the 47 are tests/fixtures/chargeback_a.py's own cba_wh_* billing-
        # only warehouses (no compute.warehouses/warehouse_events row, so idle time is unmeasurable).
        assert row["status_counts"] == {"CRITICAL": 18, "NOT_ASSESSED": 47, "OK": 9}

    def test_findings_no_floor_check_unchanged(self):
        _require_table(NO_FLOOR_QUERY_ID)
        assert materiality.floor_for(NO_FLOOR_QUERY_ID) is None
        resp = client.get("/api/findings", params={"window": WINDOW})
        rows = {r["query_id"]: r for r in resp.json()["findings"]}
        # No floor on this check, so these are the raw per-job-per-day counts across every
        # builder's own job rows on the fixture (chargeback.py's cb2_* rows, oversized_jobs.py's
        # 9101, idle_waste.py's iw_ws8, chargeback_a.py's cba_ws_id, and 1111's own jobs).
        assert rows[NO_FLOOR_QUERY_ID]["status_counts"] == {"CRITICAL": 12, "OK": 100, "WARN": 25}

    def test_finding_rows_carry_floored_status_and_floor_object(self):
        _require_table(FLOOR_QUERY_ID)
        resp = client.get(f"/api/finding/{FLOOR_QUERY_ID}", params={"window": WINDOW, "limit": 100})
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "ok_rows"
        # rows_below counts only the 5 CRITICAL + 2 WARN rows the floor actually suppressed -- not
        # the one already-OK row (0.32) that also happens to sit under $50 (Must #2's fixed side
        # effect: a floor never counts a row it had nothing to suppress).
        assert body["floor"] == {
            "column": FLOOR_COLUMN, "min": 50, "unit": "USD",
            "label": "$50 possible waste per 30 days", "rows_below": 7,
        }
        below = [r for r in body["rows"] if r["below_floor"]]
        assert len(below) == 7
        # A suppressed row reads OK -- exactly what the findings list/waste totals/sort now agree
        # on -- while its raw verdict survives as status_raw for a reader who wants it.
        assert all(r["status"] == "OK" for r in below)
        assert all(r["status_raw"] in ("CRITICAL", "WARN") for r in below)
        assert all(r[FLOOR_COLUMN] is not None and r[FLOOR_COLUMN] < 50 for r in below)
        # The already-OK row under $50 is present but NOT flagged -- it had nothing to suppress.
        already_ok_small = [
            r for r in body["rows"]
            if r["status_raw"] == "OK" and r[FLOOR_COLUMN] is not None and r[FLOOR_COLUMN] < 50
        ]
        assert already_ok_small and all(not r["below_floor"] for r in already_ok_small)

    def test_finding_no_floor_check_has_no_floor_object_or_flag(self):
        _require_table(NO_FLOOR_QUERY_ID)
        resp = client.get(f"/api/finding/{NO_FLOOR_QUERY_ID}", params={"window": WINDOW, "limit": 5})
        body = resp.json()
        assert body["floor"] is None
        assert body["rows"]
        assert all(r["below_floor"] is False for r in body["rows"])
        assert all("status_raw" not in r for r in body["rows"])

    def test_aggregate_sum_excludes_below_floor_rows(self):
        _require_table(FLOOR_QUERY_ID)
        resp = client.get(
            f"/api/finding/{FLOOR_QUERY_ID}/aggregate",
            params={
                "window": WINDOW, "agg": "sum", "value": FLOOR_COLUMN,
                "status": ["WARN", "CRITICAL"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # The floor drops both WARN rows and the CRITICAL rows valued 1.5-7.5, leaving 18 rows
        # that sum to only the one still-material CRITICAL row (264.0).
        assert body["matched_rows"] == 18
        assert body["total_value"] == pytest.approx(264.0)

    def test_below_floor_aggregate_sums_only_the_suppressed_rows(self):
        _require_table(FLOOR_QUERY_ID)
        resp = client.get(
            f"/api/finding/{FLOOR_QUERY_ID}/aggregate",
            params={"window": WINDOW, "agg": "sum", "value": FLOOR_COLUMN, "below_floor": "true"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Same 7 rows test_aggregate_sum_excludes_below_floor_rows excludes (26 WARN/CRITICAL rows
        # summing to 329.72, minus the still-material 264.0 CRITICAL row left after the floor).
        assert body["matched_rows"] == 7
        assert body["total_value"] == pytest.approx(65.72)
        assert body["floor"] == {
            "min": 50, "unit": "USD", "label": "$50 possible waste per 30 days", "per_days": 30,
        }

    def test_below_floor_aggregate_matches_nothing_without_a_floor(self):
        _require_table(NO_FLOOR_QUERY_ID)
        resp = client.get(
            f"/api/finding/{NO_FLOOR_QUERY_ID}/aggregate",
            params={"window": WINDOW, "agg": "count", "below_floor": "true"},
        )
        body = resp.json()
        assert body["matched_rows"] == 0
        assert body["floor"] is None

    def test_aggregate_count_no_floor_check_unchanged(self):
        _require_table(NO_FLOOR_QUERY_ID)
        resp = client.get(
            f"/api/finding/{NO_FLOOR_QUERY_ID}/aggregate",
            params={"window": WINDOW, "agg": "count", "status": ["WARN", "CRITICAL"]},
        )
        # 9 CRITICAL + 25 WARN (test_findings_no_floor_check_unchanged above).
        assert resp.json()["matched_rows"] == 37


# -------------------------------------------------------------------------------------------
# Severity cap -- a trend/chargeback report never reads worse than WARN.
# -------------------------------------------------------------------------------------------

CAPPED_QUERY_ID = "cost_period_over_period"


class TestSeverityCap:
    def test_capped_report_has_no_critical_in_status_counts(self):
        _require_table(CAPPED_QUERY_ID)
        assert materiality.severity_cap(CAPPED_QUERY_ID) == "WARN"
        assert materiality.severity_cap(NO_FLOOR_QUERY_ID) is None
        resp = client.get("/api/findings", params={"window": WINDOW})
        rows = {r["query_id"]: r for r in resp.json()["findings"]}
        row = rows[CAPPED_QUERY_ID]
        assert row["severity_cap"] == "WARN"
        assert rows[NO_FLOOR_QUERY_ID]["severity_cap"] is None
        if row["status_counts"]:
            assert "CRITICAL" not in row["status_counts"]

    def test_capped_report_rows_keep_the_raw_status(self):
        _require_table(CAPPED_QUERY_ID)
        resp = client.get(f"/api/finding/{CAPPED_QUERY_ID}", params={"window": WINDOW, "limit": 500})
        body = resp.json()
        if body["outcome"] != "ok_rows":
            pytest.skip("no rows in this fixture window")
        assert not any(r["status"] == "CRITICAL" for r in body["rows"])
        raw_critical = [r for r in body["rows"] if r.get("status_raw") == "CRITICAL"]
        assert all(r["status"] == "WARN" for r in raw_critical)


# -------------------------------------------------------------------------------------------
# admin_groups -- a grant to a listed admin group reads OK in access_broad_grants.
# -------------------------------------------------------------------------------------------

class TestAdminGroups:
    def _settings(self, monkeypatch, groups):
        settings = dict(app_config.DEFAULT_SETTINGS, admin_groups=groups)
        monkeypatch.setattr(app_config, "load_settings", lambda: settings)

    def test_grant_to_an_admin_group_reads_ok(self, monkeypatch):
        self._settings(monkeypatch, ["Platform-Admins", "o'brien team"])
        con = duckdb.connect()
        con.execute("CREATE TABLE f_grants (status VARCHAR, grantee VARCHAR, id VARCHAR)")
        con.execute(
            "INSERT INTO f_grants VALUES ('WARN', 'platform-admins', 'admin'), "
            "('CRITICAL', 'someone@example.com', 'person'), ('WARN', 'o''brien team', 'quoted')"
        )
        cols = [("status", "VARCHAR"), ("grantee", "VARCHAR"), ("id", "VARCHAR")]
        select_list = app_core_data._select_list(cols, "access_broad_grants")
        rows = con.execute(f"SELECT {select_list} FROM f_grants t ORDER BY id").df().set_index("id")
        assert rows.loc["admin", ["status", "status_raw"]].tolist() == ["OK", "WARN"]
        assert rows.loc["quoted", "status"] == "OK"
        assert rows.loc["person", "status"] == "CRITICAL"

    def test_only_broad_grants_and_only_when_set(self, monkeypatch):
        self._settings(monkeypatch, [])
        assert materiality.status_expr("access_broad_grants", ["status", "grantee"]) == 't."status"'
        self._settings(monkeypatch, ["platform-admins"])
        assert materiality.status_expr("cost_by_job", ["status", "grantee"]) == 't."status"'

    def test_admin_groups_must_be_a_list_of_names(self):
        with pytest.raises(app_config.SettingsError):
            app_config._validate_settings({"admin_groups": "platform-admins"})
        assert app_config._validate_settings({"admin_groups": ["a"]})["admin_groups"] == ["a"]
