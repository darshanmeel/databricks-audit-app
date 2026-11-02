"""Adds what only a direct export writes to a built Acme database: table and volume sizes
(storage.*) and who works where (usage.*), so Workspace table usage, User activity, Volume access
and Volumes have rows.

    python tests/fake_account/usage.py [--db tests/fake_account/acme.duckdb]

Catalogs and their home workspace: acme (acme-prod), lab (acme-lab), finance (acme-finance),
analytics (acme-analytics), sandbox (acme-dev), archive (acme-old, nobody reads it). Seeded, so
every run gives the same rows.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
END = date(2026, 9, 21)
WINDOWS = (7, 30, 90)

# catalog -> (home workspace, lineage events, schemas -> tables)
CATALOGS = {
    "acme": ("2001", 900, {"sales": ["orders", "customers", "daily", "returns", "v_orders"], "raw": ["events", "events_old", "clicks_raw"]}),
    "lab": ("2014", 400, {"sales": ["clicks", "no_po", "archive", "archive_xl", "growing", "users"],
                         "mart": [f"t{i}" for i in range(12)]}),
    "finance": ("2006", 300, {"ledger": ["gl_entries", "accounts", "fx_rates", "v_trial_balance"], "reporting": ["monthly_pnl", "budget"]}),
    "analytics": ("2005", 500, {"web": ["sessions", "pageviews", "v_funnel"], "ml": ["features", "scores", "training_set"]}),
    "sandbox": ("2002", 120, {"scratch": ["tmp_join", "test_orders", "copy_customers", "old_extract"]}),
    "archive": ("2003", 5, {"y2024": ["orders_2024", "events_2024"]}),
}
VOLUMES = [("acme", "raw", "landing"), ("lab", "raw", "exports"), ("finance", "files", "invoices"),
           ("analytics", "ml", "models"), ("sandbox", "scratch", "uploads"), ("archive", "y2024", "dumps")]
PEOPLE = {
    "2001": ["ana@acme.example", "ben@acme.example", "chen@acme.example", "dora@acme.example"],
    "2014": ["lab.owner@acme.example", "eli@acme.example"],
    "2006": ["fay@acme.example", "gus@acme.example", "ana@acme.example"],
    "2005": ["hana@acme.example", "ivan@acme.example", "ben@acme.example"],
    "2002": ["ben@acme.example", "chen@acme.example", "jo@acme.example"],
    "2003": ["kim@acme.example"],
}
SPS = {"2001": ["0b7c2f1e-5a4d-4e8f-9a1b-2c3d4e5f6a01"], "2005": ["0b7c2f1e-5a4d-4e8f-9a1b-2c3d4e5f6a02"],
       "2006": ["0b7c2f1e-5a4d-4e8f-9a1b-2c3d4e5f6a03"]}
# days since last seen, for the inactive bands
AWAY = {"kim@acme.example": 200, "jo@acme.example": 120, "dora@acme.example": 95, "gus@acme.example": 45, "eli@acme.example": 33}


def ts(days: int, hour: int = 9) -> str:
    return (datetime.combine(END, datetime.min.time()) - timedelta(days=days) + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M:%S")


def put(con, name: str, cols: str, rows: list[tuple]) -> None:
    con.execute(f"CREATE OR REPLACE TABLE {name} ({cols})")
    if rows:
        con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(rows[0]))})", rows)


def add(db: Path) -> dict:
    rnd = random.Random(7)
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA IF NOT EXISTS storage")
    con.execute("CREATE SCHEMA IF NOT EXISTS usage")

    # storage.table_detail: every table and view, sizes from DESCRIBE DETAIL
    detail, history, names = [], [], []
    for cat, (_ws, _ev, schemas) in CATALOGS.items():
        for sch, tables in schemas.items():
            for t in tables:
                full = f"{cat}.{sch}.{t}"
                view = t.startswith("v_")
                altered = ts(rnd.choice([1, 2, 5, 12, 40, 160, 300]) if cat != "archive" else 400)
                cols = rnd.randint(4, 60)
                born = ts(2 if full == "sandbox.scratch.copy_customers" else 500 if cat == "archive" else rnd.choice([3, 30, 200, 500]))
                owner = PEOPLE[CATALOGS[cat][0]][0]
                if view:
                    detail.append((cat, sch, t, "VIEW", None, altered, cols, None, None, None, "view", born, owner))
                    names.append((full, True))
                    continue
                if full == "finance.reporting.budget":
                    detail.append((cat, sch, t, "MANAGED", "DELTA", altered, cols, None, None, None,
                                   "failed: PERMISSION_DENIED: User does not have SELECT on Table 'finance.reporting.budget'",
                                   born, owner))
                    names.append((full, False))
                    continue
                size = int(10 ** rnd.uniform(6, 11.5))
                files = max(1, size // rnd.choice([2_000_000, 30_000_000, 120_000_000]))
                detail.append((cat, sch, t, "MANAGED", "DELTA", altered, cols, size, files, altered, "described", born, owner))
                names.append((full, False))
                for v in range(rnd.randint(2, 6)):
                    op = rnd.choice(["WRITE", "MERGE", "OPTIMIZE", "DELETE", "WRITE", "VACUUM END"])
                    history.append((full, 20 - v, ts(v * 3 + 1, 2), op, rnd.choice(PEOPLE[CATALOGS[cat][0]]),
                                    f"numOutputRows={rnd.randint(100, 900000)}" if op in ("WRITE", "MERGE") else None))
    put(con, "storage.table_detail", "table_catalog VARCHAR, table_schema VARCHAR, table_name VARCHAR, table_type VARCHAR, "
        "data_source_format VARCHAR, last_altered VARCHAR, column_count BIGINT, size_in_bytes BIGINT, num_files BIGINT, "
        "last_modified VARCHAR, detail VARCHAR, created VARCHAR, owner VARCHAR", detail)
    put(con, "storage.table_history", "table_full_name VARCHAR, version BIGINT, timestamp VARCHAR, operation VARCHAR, "
        "user_name VARCHAR, metrics VARCHAR", history)
    described = sum(1 for d in detail if d[-1] == "described")
    con.execute("CREATE OR REPLACE TABLE storage.table_detail_info AS SELECT ? AS info", [json.dumps(
        {"exported": True, "error": None, "tables": len(detail), "described": described,
         "view": sum(1 for d in detail if d[-1] == "view"), "failed": 1, "history_rows": len(history),
         "threads": 16, "max_tables": 20000, "seconds": 41.0})])

    # storage.volume_detail: files and bytes from listing each volume
    vols = []
    for cat, sch, name in VOLUMES:
        if name == "dumps":
            vols.append((cat, sch, name, "MANAGED", None, None, False, "failed: PERMISSION_DENIED: no READ VOLUME"))
        else:
            files = rnd.randint(40, 60000 if name != "models" else 50000)
            vols.append((cat, sch, name, "MANAGED", files, files * rnd.randint(50_000, 4_000_000), name == "models", "listed"))
    put(con, "storage.volume_detail", "volume_catalog VARCHAR, volume_schema VARCHAR, volume_name VARCHAR, volume_type VARCHAR, "
        "files BIGINT, size_in_bytes BIGINT, truncated BOOLEAN, detail VARCHAR", vols)
    con.execute("CREATE OR REPLACE TABLE storage.volume_detail_info AS SELECT ? AS info", [json.dumps(
        {"exported": True, "volumes": len(vols), "listed": len(vols) - 1, "failed": 1, "truncated": 1, "max_entries": 50000})])

    # usage.catalog_workspace: lineage events per catalog and workspace (decides the home workspace)
    cw = [(cat, ws, ev) for cat, (ws, ev, _s) in CATALOGS.items()] + [("acme", "2005", 200), ("finance", "2001", 60)]
    put(con, "usage.catalog_workspace", "catalog VARCHAR, workspace_id VARCHAR, events BIGINT", cw)

    # usage.table_access: last read and write per table in each workspace that used it
    access, daily, user_tables, maint = [], [], [], []
    for full, view in names:
        cat = full.split(".")[0]
        ws = CATALOGS[cat][0]
        if cat == "archive" or full.endswith(("old_extract", "archive_xl", "t11", "t10", "copy_customers")):
            continue  # never read in the lookback
        written_only = full.endswith(("returns", "clicks_raw", "t9"))  # written every day, read by nobody
        last = rnd.choice([0, 1, 2, 3, 8, 20, 45, 70, 110, 200])
        reads, writes = (0 if written_only else rnd.randint(1, 900)), 0 if view else rnd.randint(0 if not written_only else 20, 60)
        last_write = ts(rnd.randint(0, 2)) if written_only else (None if view or not writes else ts(last + rnd.randint(0, 5)))
        readers = rnd.sample(PEOPLE[ws], k=rnd.randint(1, len(PEOPLE[ws]))) if reads else []
        writers = [] if view or not writes else (SPS.get(ws, PEOPLE[ws])[:1] + rnd.sample(PEOPLE[ws], k=1))
        for w in WINDOWS:
            share = 0 if (not written_only and last >= w) else w / 90
            r_w, w_w = int(reads * share), int(writes * (w / 90 if written_only else share))
            access.append((w, ws, full, None if written_only else ts(last), last_write, r_w, w_w,
                           len(readers) if r_w else 0, len(set(writers)) if w_w else 0,
                           ", ".join(sorted(readers)[:3]) if r_w else None,
                           ", ".join(sorted(set(writers))[:3]) if w_w else None))
        if written_only:
            continue
        if last < 30:
            for d in range(30):
                r = rnd.randint(0, reads // 20 + 1)
                daily.append((full, (END - timedelta(days=d)).isoformat(), r, 0 if view else rnd.randint(0, 3), min(r, 4), 0 if view else 1))
        for who in rnd.sample(PEOPLE[ws], k=min(2, len(PEOPLE[ws]))):
            user_tables.append((ws, who, full, rnd.randint(1, reads), rnd.randint(0, writes), ts(last),
                                rnd.choice(["DBSQL_QUERY", "NOTEBOOK", "DASHBOARD", "JOB, NOTEBOOK"])))
        if not view and rnd.random() < 0.4:
            maint.append((full, ts(rnd.randint(1, 20), 3), rnd.choice(["COMPACTION", "VACUUM", "ANALYZE"]), "SUCCESSFUL",
                          round(rnd.uniform(0.1, 4.0), 2), "ESTIMATED_DBU"))
    if "finance.reporting.monthly_pnl" in {a[2] for a in access}:  # also read from acme-prod
        for w in WINDOWS:
            access.append((w, "2001", "finance.reporting.monthly_pnl", ts(2), None, 40 * w // 90, 0, 2, 0,
                           "ana@acme.example, ben@acme.example", None))
    put(con, "usage.table_access", "window_days BIGINT, workspace_id VARCHAR, table_full_name VARCHAR, last_read VARCHAR, "
        "last_write VARCHAR, reads BIGINT, writes BIGINT, readers BIGINT, writers BIGINT, reader_names VARCHAR, "
        "writer_names VARCHAR", access)
    put(con, "usage.table_daily", "table_full_name VARCHAR, day VARCHAR, reads BIGINT, writes BIGINT, readers BIGINT, writers BIGINT", daily)
    put(con, "usage.user_tables", "workspace_id VARCHAR, identity VARCHAR, table_full_name VARCHAR, reads BIGINT, writes BIGINT, "
        "last_access VARCHAR, via VARCHAR", user_tables)
    put(con, "usage.table_maintenance", "table_full_name VARCHAR, start_time VARCHAR, operation_type VARCHAR, "
        "operation_status VARCHAR, usage_quantity DOUBLE, usage_unit VARCHAR", maint)

    # usage.user_access / user_last_seen: who works where, and when they were last seen
    users, seen = [], []
    for ws, people in PEOPLE.items():
        for who in people + SPS.get(ws, []):
            away = AWAY.get(who, rnd.choice([0, 1, 2, 4, 9]))
            seen.append((ws, who, ts(away), None if "@" not in who else ts(away + rnd.randint(0, 6))))
            if away < 90:
                q = rnd.randint(0, 2500) if "@" in who else rnd.randint(500, 9000)
                users.append((ws, who, ts(away), rnd.randint(0, 40) if "@" in who else 0, q, ts(away) if q else None,
                              rnd.randint(0, 30), rnd.randint(0, 8)))
    seen.append(("2013", "left.company@acme.example", ts(330), ts(331)))
    put(con, "usage.user_access", "workspace_id VARCHAR, identity VARCHAR, last_seen VARCHAR, logins BIGINT, queries BIGINT, "
        "last_query VARCHAR, tables_read BIGINT, tables_written BIGINT", users)
    put(con, "usage.user_last_seen", "workspace_id VARCHAR, identity VARCHAR, last_seen VARCHAR, last_login VARCHAR", seen)

    # usage.workspace_activity: per window, who was active and how many queries
    act = []
    for w in WINDOWS:
        for ws, people in PEOPLE.items():
            row = [u for u in users if u[0] == ws and (END - date.fromisoformat(u[2][:10])).days < w]
            act.append((w, ws, sum(1 for u in row if "@" in u[1]), sum(1 for u in row if "@" not in u[1]),
                        sum(u[4] for u in row) * w // 90, sum(1 for u in row if u[4]), max((u[2] for u in row), default=None),
                        min(w, int(w * (0.9 if len(row) > 3 else 0.5 if row else 0)))))
    act.append((90, "2004", 1, 0, 12, 1, ts(60), 2))
    put(con, "usage.workspace_activity", "window_days BIGINT, workspace_id VARCHAR, active_people BIGINT, "
        "active_service_principals BIGINT, queries BIGINT, query_identities BIGINT, last_activity VARCHAR, "
        "active_days BIGINT", act)
    # usage.sensitive_columns: from a tag, data classification or the column name; masked or not
    put(con, "usage.sensitive_columns", "table_full_name VARCHAR, column_name VARCHAR, pii_type VARCHAR, method VARCHAR, "
        "masked BOOLEAN", [
            ("acme.sales.customers", "email", "Email", "classification", True),
            ("acme.sales.customers", "phone", "Phone number", "name", False),
            ("acme.sales.customers", "street", "Postal address", "name", False),
            ("acme.raw.events_old", "ip_address", "IP address", "name", False),
            ("lab.sales.users", "email", "Email", "tag", False),
            ("lab.sales.archive_xl", "birth_date", "Date of birth", "name", False),
            ("finance.ledger.accounts", "iban", "IBAN / bank account", "tag", True),
            ("sandbox.scratch.copy_customers", "email", "Email", "name", False),
            ("archive.y2024.orders_2024", "customer_email", "Email", "name", False),
        ])
    con.execute("CREATE OR REPLACE TABLE usage.workspace_activity_info AS SELECT ? AS info", [json.dumps(
        {"exported": True, "error": None, "end": END.isoformat(), "windows": list(WINDOWS), "read_lookback_days": 365})])

    # usage.volume_access: who read and wrote files in each volume
    va = []
    for cat, sch, name in VOLUMES[:-1]:
        ws = CATALOGS[cat][0]
        for who in rnd.sample(PEOPLE[ws], k=min(2, len(PEOPLE[ws]))):
            last = rnd.choice([0, 3, 15, 50])
            va.append((ws, f"{cat}.{sch}.{name}", who, rnd.randint(1, 400), rnd.randint(0, 80), ts(last), ts(last + 2)))
    va.append(("2005", "analytics.ml.unlisted_models", "hana@acme.example", 12, 3, ts(4), ts(4)))
    put(con, "usage.volume_access", "workspace_id VARCHAR, volume_full_name VARCHAR, identity VARCHAR, reads BIGINT, "
        "writes BIGINT, last_read VARCHAR, last_write VARCHAR", va)
    con.close()
    return {"tables": len(detail), "volumes": len(vols), "table_access": len(access), "users": len(users), "seen": len(seen)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HERE / "acme.duckdb"))
    print(add(Path(ap.parse_args().db)))
