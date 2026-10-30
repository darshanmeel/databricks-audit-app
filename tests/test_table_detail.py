"""tools/table_detail.py: DESCRIBE DETAIL of every table, many at once; unchanged tables keep last
export's figures; views and other formats aren't described; a cap leaves the oldest out."""
import duckdb
import pyarrow.parquet as pq
import pytest

from tools import table_detail

LISTED = [
    {"table_catalog": "main", "table_schema": "sales", "table_name": "orders", "table_type": "MANAGED",
     "data_source_format": "DELTA", "last_altered": "2026-10-20 10:00:00", "column_count": 12},
    {"table_catalog": "main", "table_schema": "sales", "table_name": "lines", "table_type": "MANAGED",
     "data_source_format": "DELTA", "last_altered": "2026-10-01 10:00:00", "column_count": 8},
    {"table_catalog": "main", "table_schema": "sales", "table_name": "orders_v", "table_type": "VIEW",
     "data_source_format": None, "last_altered": "2026-10-01 10:00:00", "column_count": 12},
    {"table_catalog": "main", "table_schema": "raw", "table_name": "drop_csv", "table_type": "EXTERNAL",
     "data_source_format": "CSV", "last_altered": "2026-10-01 10:00:00", "column_count": 3},
    {"table_catalog": "system", "table_schema": "billing", "table_name": "usage", "table_type": "MANAGED",
     "data_source_format": "DELTA", "last_altered": "2026-10-01 10:00:00", "column_count": 30},
    {"table_catalog": "main", "table_schema": "sales", "table_name": "broken", "table_type": "MANAGED",
     "data_source_format": "DELTA", "last_altered": "2026-10-19 10:00:00", "column_count": 2},
]
DETAIL = {"orders": (5_000_000, 4), "lines": (2_000_000, 2000)}


class Cursor:
    def __init__(self, log):
        self.log, self.description, self._rows = log, None, []

    def execute(self, sql):
        self.log.append(sql)
        if sql.startswith("DESCRIBE HISTORY"):
            self.description = [("version",), ("timestamp",), ("userName",), ("operation",), ("operationMetrics",)]
            self._rows = [(2, "2026-10-20 10:00:00", "alice@example.com", "VACUUM END", {"numDeletedFiles": "7"}),
                          (1, "2026-10-19 09:00:00", "etl-sp", "WRITE", {"numOutputRows": "100", "numFiles": "0"})]
            return
        if sql.startswith("DESCRIBE DETAIL"):
            name = sql.rsplit("`.`", 1)[1].strip("`")
            if name not in DETAIL:
                raise RuntimeError(f"[TABLE_OR_VIEW_NOT_FOUND] {name}\nmore")
            size, files = DETAIL[name]
            self.description = [("format",), ("sizeInBytes",), ("numFiles",), ("lastModified",)]
            self._rows = [("delta", size, files, "2026-10-20 10:00:00")]
        else:
            cols = list(LISTED[0])
            self.description = [(c,) for c in cols]
            self._rows = [tuple(r[c] for c in cols) for r in LISTED]

    def fetchall(self):
        return self._rows


class Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return Cursor(self.log)

    def close(self):
        pass


class Connected:
    def __init__(self, log):
        self.conn = Conn(log)


def _connect(log):
    return lambda: Connected(log)


def _rows(out):
    return {r["table_name"]: r for r in pq.read_table(str(out / "storage" / table_detail.FILE)).to_pylist()}


def test_describes_delta_tables_in_parallel_and_skips_views_other_formats_and_system(tmp_path):
    log: list[str] = []
    info = table_detail.export(_connect(log), tmp_path, threads=4, log=lambda m: None)
    rows = _rows(tmp_path)
    assert "usage" not in rows  # the system catalog is left out
    assert rows["orders"]["size_in_bytes"] == 5_000_000 and rows["orders"]["num_files"] == 4
    assert rows["orders"]["detail"] == "described" and rows["orders"]["column_count"] == 12
    assert rows["orders_v"]["detail"] == "view" and rows["orders_v"]["size_in_bytes"] is None
    assert rows["drop_csv"]["detail"] == "not_delta"
    assert rows["broken"]["detail"].startswith("failed: [TABLE_OR_VIEW_NOT_FOUND]")
    assert "\n" not in rows["broken"]["detail"]
    assert sum(s.startswith("DESCRIBE DETAIL") for s in log) == 3
    assert info["exported"] and info["described"] == 2 and info["failed"] == 1 and info["view"] == 1


def test_unchanged_tables_keep_last_exports_figures(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    table_detail.export(_connect([]), first, log=lambda m: None)
    log: list[str] = []
    info = table_detail.export(_connect(log), second, previous=first / "storage" / table_detail.FILE,
                               log=lambda m: None)
    rows = _rows(second)
    assert rows["orders"]["detail"] == "reused" and rows["orders"]["size_in_bytes"] == 5_000_000
    # only the one that failed last time is asked again
    assert [s for s in log if s.startswith("DESCRIBE DETAIL")] == ["DESCRIBE DETAIL `main`.`sales`.`broken`"]
    assert info["reused"] == 2


def test_a_cap_describes_the_most_recently_changed_first(tmp_path):
    table_detail.export(_connect([]), tmp_path, max_tables=1, log=lambda m: None)
    rows = _rows(tmp_path)
    assert rows["orders"]["detail"] == "described"
    assert rows["lines"]["detail"] == "not_asked" and rows["broken"]["detail"] == "not_asked"


def test_a_failed_listing_is_reported_not_raised(tmp_path):
    def bad():
        raise RuntimeError("no warehouse")
    assert table_detail.export(bad, tmp_path, log=lambda m: None) == {"exported": False, "error": "no warehouse"}


def test_table_sizes_setting_is_checked():
    from app.core import config

    assert config._validate_table_sizes(None) == {"enabled": True, "threads": 16, "max_tables": 20000}
    assert config._validate_table_sizes({"threads": 32})["threads"] == 32
    for bad in ({"threads": 0}, {"threads": 65}, {"max_tables": -1}, {"enabled": "yes"}, {"other": 1}):
        with pytest.raises(config.SettingsError):
            config._validate_table_sizes(bad)


def test_read_table_sizes_sums_per_schema_and_flags_small_files(tmp_path, monkeypatch):
    table_detail.export(_connect([]), tmp_path, log=lambda m: None)
    db = tmp_path / "audit.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA storage")
    con.execute(f"CREATE TABLE storage.table_detail AS SELECT * FROM read_parquet('{(tmp_path / 'storage' / table_detail.FILE).as_posix()}')")
    con.execute("""CREATE TABLE storage.table_detail_info AS SELECT '{"exported": true, "described": 2}' AS info""")
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db))
    from app.core import data

    got = data.read_table_sizes()
    assert got["outcome"] == "ok_rows" and got["info"]["described"] == 2
    assert got["totals"]["tables"] == 4 and got["totals"]["views"] == 1 and got["totals"]["sized"] == 2
    assert got["totals"]["size_in_bytes"] == 7_000_000
    sales = next(r for r in got["by_schema"] if r["table_schema"] == "sales")
    assert sales["size_in_bytes"] == 7_000_000 and sales["views"] == 1
    assert [r["full_name"] for r in got["biggest"]] == ["main.sales.orders", "main.sales.lines"]
    # 2,000 files averaging 1 KB: critical
    assert [(r["full_name"], r["status"]) for r in got["small_files"]] == [("main.sales.lines", "CRITICAL")]


class VolCursor:
    TREE = {"/Volumes/main/work/raw/": [("/Volumes/main/work/raw/a.csv", "a.csv", 100), ("/Volumes/main/work/raw/sub/", "sub/", 0)],
            "/Volumes/main/work/raw/sub/": [("/Volumes/main/work/raw/sub/b.csv", "b.csv", 50)]}

    def __init__(self):
        self.description, self._rows = None, []

    def execute(self, sql):
        if sql.startswith("LIST"):
            path = sql[6:-1]
            if "locked" in path:
                raise RuntimeError("[INSUFFICIENT_PERMISSIONS] no READ VOLUME\nmore")
            self.description = [("path",), ("name",), ("size",)]
            self._rows = self.TREE.get(path, [])
        else:
            self.description = [("volume_catalog",), ("volume_schema",), ("volume_name",), ("volume_type",)]
            self._rows = [("main", "work", "raw", "MANAGED"), ("main", "work", "locked", "EXTERNAL"),
                          ("system", "x", "y", "MANAGED")]

    def fetchall(self):
        return self._rows


def _vol_connect():
    cur = VolCursor()
    conn = type("C", (), {"cursor": lambda self: cur, "close": lambda self: None})()
    return lambda: type("K", (), {"conn": conn})()


def test_volumes_are_listed_through_their_folders_and_a_failure_is_kept(tmp_path):
    info = table_detail.export_volumes(_vol_connect(), tmp_path, threads=2, log=lambda m: None)
    rows = {r["volume_name"]: r for r in pq.read_table(str(tmp_path / "storage" / table_detail.VOLUME_FILE)).to_pylist()}
    assert set(rows) == {"raw", "locked"}  # the system catalog left out
    assert rows["raw"]["files"] == 2 and rows["raw"]["size_in_bytes"] == 150 and not rows["raw"]["truncated"]
    assert rows["locked"]["detail"] == "failed: [INSUFFICIENT_PERMISSIONS] no READ VOLUME"
    assert info["listed"] == 1 and info["failed"] == 1


def test_a_volume_over_the_cap_reads_truncated(tmp_path):
    table_detail.export_volumes(_vol_connect(), tmp_path, max_entries=1, log=lambda m: None)
    raw = next(r for r in pq.read_table(str(tmp_path / "storage" / table_detail.VOLUME_FILE)).to_pylist() if r["volume_name"] == "raw")
    assert raw["truncated"] and raw["files"] == 1


def test_history_is_kept_per_described_table_and_carried_over_when_unchanged(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    table_detail.export(_connect([]), first, log=lambda m: None)
    hist = pq.read_table(str(first / "storage" / table_detail.HISTORY_FILE)).to_pylist()
    orders = [h for h in hist if h["table_full_name"] == "main.sales.orders"]
    assert [(h["version"], h["operation"], h["user_name"]) for h in orders] == [(2, "VACUUM END", "alice@example.com"), (1, "WRITE", "etl-sp")]
    assert orders[0]["metrics"] == "numDeletedFiles=7" and orders[1]["metrics"] == "numOutputRows=100"
    table_detail.export(_connect([]), second, previous=first / "storage" / table_detail.FILE, log=lambda m: None)
    again = pq.read_table(str(second / "storage" / table_detail.HISTORY_FILE)).to_pylist()
    assert len([h for h in again if h["table_full_name"] == "main.sales.orders"]) == 2  # reused, not asked again
