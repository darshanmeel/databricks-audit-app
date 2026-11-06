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
    r2 = random.Random(11)
    for i, h in enumerate(history):
        if h[3] == "OPTIMIZE":
            removed = r2.randint(200, 4000)
            size = r2.randint(5, 400) * 1_000_000_000
            history[i] = h[:5] + (f"numAddedFiles={max(1, removed // 60)}, numRemovedFiles={removed}, "
                                  f"numAddedBytes={size}, numRemovedBytes={int(size * 1.04)}",)
        elif h[3] == "VACUUM END":
            history[i] = h[:5] + (f"numDeletedFiles={r2.randint(300, 9000)}, sizeOfDataToDelete={r2.randint(2, 300) * 1_000_000_000}",)
    # acme.sales.orders: compacted 10 days ago, its reads got faster
    history.append(("acme.sales.orders", 1, ts(10, 2), "OPTIMIZE", "ana@acme.example",
                    "numAddedFiles=40, numRemovedFiles=3100, numAddedBytes=212000000000, numRemovedBytes=220000000000"))
    # acme.sales.returns: written every day (lineage), but DESCRIBE HISTORY gave nothing for it
    history = [h for h in history if h[0] != "acme.sales.returns"]
    put(con, "storage.table_history", "table_full_name VARCHAR, version BIGINT, timestamp VARCHAR, operation VARCHAR, "
        "user_name VARCHAR, metrics VARCHAR", history)
    described = sum(1 for d in detail if d[10] == "described")
    con.execute("CREATE OR REPLACE TABLE storage.table_detail_info AS SELECT ? AS info", [json.dumps(
        {"exported": True, "error": None, "tables": len(detail), "described": described,
         "view": sum(1 for d in detail if d[10] == "view"), "failed": 1, "history_rows": len(history),
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
        if written_only:  # its writer is a job's service principal
            for who in writers:
                for w in WINDOWS:
                    user_tables.append((w, ws, who, full, 0, max(1, writes * w // 90), last_write, "JOB"))
            continue
        if last < 30:
            for d in range(30):
                r = rnd.randint(0, reads // 20 + 1)
                daily.append((full, ws, (END - timedelta(days=d)).isoformat(), r, 0 if view else rnd.randint(0, 3), min(r, 4), 0 if view else 1))
        for who in rnd.sample(PEOPLE[ws], k=min(2, len(PEOPLE[ws]))):
            r, wr, via = rnd.randint(1, reads), rnd.randint(0, writes), rnd.choice(["DBSQL_QUERY", "NOTEBOOK", "DASHBOARD", "JOB, NOTEBOOK"])
            for w in WINDOWS:  # in a window only when last used inside it
                if last < w:
                    user_tables.append((w, ws, who, full, max(1, r * w // 90), wr * w // 90, ts(last), via))
        if not view and rnd.random() < 0.4:
            maint.append((full, ts(rnd.randint(1, 20), 3), rnd.choice(["COMPACTION", "VACUUM", "ANALYZE"]), "SUCCESSFUL",
                          round(rnd.uniform(0.1, 4.0), 2), "ESTIMATED_DBU"))
    if "finance.reporting.monthly_pnl" in {a[2] for a in access}:  # also read from acme-prod
        for w in WINDOWS:
            access.append((w, "2001", "finance.reporting.monthly_pnl", ts(2), None, 40 * w // 90, 0, 2, 0,
                           "ana@acme.example, ben@acme.example", None))
    # the export's own reads of a system table: in lineage, never shown as a table of the account
    for w in WINDOWS:
        access.append((w, "2001", "system.query.history", ts(0), None, 5000 * w // 30, 0, 1, 0, "svc-audit", None))
    daily += [("system.query.history", "2001", (END - timedelta(days=d)).isoformat(), 5000, 0, 1, 0) for d in range(30)]
    put(con, "usage.table_access", "window_days BIGINT, workspace_id VARCHAR, table_full_name VARCHAR, last_read VARCHAR, "
        "last_write VARCHAR, reads BIGINT, writes BIGINT, readers BIGINT, writers BIGINT, reader_names VARCHAR, "
        "writer_names VARCHAR", access)
    put(con, "usage.table_daily", "table_full_name VARCHAR, workspace_id VARCHAR, day VARCHAR, reads BIGINT, writes BIGINT, readers BIGINT, writers BIGINT", daily)
    put(con, "usage.user_tables", "window_days BIGINT, workspace_id VARCHAR, identity VARCHAR, table_full_name VARCHAR, reads BIGINT, writes BIGINT, "
        "last_access VARCHAR, via VARCHAR", user_tables)
    r3 = random.Random(12)
    po = {"COMPACTION": lambda: {"number_of_compacted_files": str(r3.randint(50, 900)), "number_of_output_files": str(r3.randint(1, 20)),
                                 "amount_of_data_compacted_bytes": str(r3.randint(1, 90) * 1_000_000_000)},
          "VACUUM": lambda: {"number_of_deleted_files": str(r3.randint(100, 5000)), "amount_of_data_deleted_bytes": str(r3.randint(1, 60) * 1_000_000_000)},
          "ANALYZE": lambda: {}}
    maint = [m + (json.dumps(po[m[2]]()) if m[2] in po else None,) for m in maint]
    put(con, "usage.table_maintenance", "table_full_name VARCHAR, start_time VARCHAR, operation_type VARCHAR, "
        "operation_status VARCHAR, usage_quantity DOUBLE, usage_unit VARCHAR, metrics VARCHAR", maint)
    _table_queries(con, [d for d in daily if not d[0].startswith("system.")])
    con.executemany("INSERT INTO usage.table_query_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [("system.query.history", (END - timedelta(days=d)).isoformat(), 500, 90_000_000, 10**12, 9000, 0, 400,
                      10**11, 0, 0, 0) for d in range(30)])

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
            r, wr = rnd.randint(1, 400), rnd.randint(0, 80)
            for w in WINDOWS:  # in a window only when last used inside it
                if last < w:
                    va.append((w, ws, f"{cat}.{sch}.{name}", who, max(1, r * w // 90), wr * w // 90, ts(last), ts(last + 2)))
    for w in WINDOWS:
        va.append((w, "2005", "analytics.ml.unlisted_models", "hana@acme.example", max(1, 12 * w // 90), 3 * w // 90, ts(4), ts(4)))
    put(con, "usage.volume_access", "window_days BIGINT, workspace_id VARCHAR, volume_full_name VARCHAR, identity VARCHAR, reads BIGINT, "
        "writes BIGINT, last_read VARCHAR, last_write VARCHAR", va)
    # usage.table_edges: what each table is built from; usage.table_grants: direct SELECT grants
    put(con, "usage.table_edges", "source VARCHAR, target VARCHAR, events BIGINT, last_event VARCHAR", [
        ("acme.raw.events", "acme.sales.orders", 49, ts(1)), ("acme.sales.orders", "acme.sales.daily", 30, ts(1)),
        ("acme.sales.customers", "acme.sales.daily", 30, ts(1)), ("acme.sales.orders", "finance.reporting.monthly_pnl", 4, ts(5)),
        ("acme.raw.clicks_raw", "acme.sales.returns", 20, ts(0)), ("acme.sales.customers", "sandbox.scratch.copy_customers", 1, ts(2)),
        ("finance.ledger.gl_entries", "finance.reporting.monthly_pnl", 22, ts(2)), ("finance.ledger.fx_rates", "finance.ledger.gl_entries", 30, ts(1)),
        ("analytics.web.pageviews", "analytics.web.sessions", 28, ts(1)), ("analytics.web.sessions", "analytics.ml.features", 12, ts(3)),
        ("analytics.ml.features", "analytics.ml.scores", 12, ts(3)),
    ] + [("lab.sales.clicks", f"lab.mart.t{i}", rnd.randint(2, 30), ts(rnd.randint(0, 20))) for i in range(12)])
    put(con, "usage.table_grants", "table_full_name VARCHAR, grantee VARCHAR, privilege VARCHAR", [
        ("acme.sales.customers", "ana@acme.example", "SELECT"), ("acme.sales.customers", "dora@acme.example", "SELECT"),
        ("acme.sales.customers", "bi_readers", "SELECT"), ("acme.sales.orders", "dora@acme.example", "SELECT"),
        ("acme.sales.orders", "chen@acme.example", "SELECT"), ("archive.y2024.orders_2024", "kim@acme.example", "SELECT"),
        ("sandbox.scratch.copy_customers", "jo@acme.example", "ALL PRIVILEGES"), ("lab.sales.users", "eli@acme.example", "SELECT"),
        ("finance.ledger.accounts", "gus@acme.example", "SELECT"), ("finance.ledger.accounts", "fay@acme.example", "SELECT"),
    ])
    _warehouses(con, rnd)
    con.close()
    return {"tables": len(detail), "volumes": len(vols), "table_access": len(access), "users": len(users), "seen": len(seen)}


def _table_queries(con, daily: list[tuple]) -> None:
    """usage.table_queries and table_query_daily: queries on the tables read in the last 30 days.
    acme.sales.orders reads whole files before its OPTIMIZE 10 days ago and prunes after it."""
    rnd = random.Random(13)
    tables = sorted({d[0] for d in daily if d[3]})
    per_day, top = [], []
    for t in tables:
        ws = CATALOGS[t.split(".")[0]][0]
        scans = t in ("acme.sales.orders", "analytics.web.pageviews")
        for d in range(30):
            n = rnd.randint(0, 12)
            if not n:
                continue
            before = t == "acme.sales.orders" and d > 10
            files = rnd.randint(800, 3000) if before or (scans and t != "acme.sales.orders") else rnd.randint(5, 60)
            pruned = 0 if files >= 800 else files * rnd.randint(5, 40)
            ms = (rnd.randint(20000, 90000) if files >= 800 else rnd.randint(800, 6000)) * n
            spill = files * n * 400_000 if files >= 800 else 0
            per_day.append((t, (END - timedelta(days=d)).isoformat(), n, ms, files * n * 30_000_000, files * n, pruned * n,
                            n if pruned == 0 and files >= 50 else 0, spill, 1 if d % 7 == 1 else 0,
                            rnd.randint(30000, 400000) if d % 7 == 1 else 0, 2_000_000_000 if d % 7 == 1 else 0))
            for k in range(min(n, 2)):
                who = rnd.choice(PEOPLE[ws])
                text = f"SELECT * FROM {t}" if files >= 800 else f"SELECT region, SUM(amount) FROM {t} WHERE order_date >= '2026-09-01' GROUP BY region"
                top.append((t, "read", ws, f"ac_tq_{len(top)}", who, ts(d, 8 + k), "SELECT", "FINISHED", ms // n,
                            files * 30_000_000, files, pruned, files * 400_000, rnd.randint(1, 5000), "ac_wh_bi", text,
                            files * 400_000 if files >= 800 else 0, files * 5_000_000, 0, 400, ms // n - 2000, 1200, 400,
                            ms // n * 6, 35.0, 0, d))
        top.append((t, "write", ws, f"ac_tq_{len(top)}", SPS.get(ws, PEOPLE[ws])[0], ts(1, 2), "MERGE", "FINISHED",
                    rnd.randint(30000, 400000), 10_000_000_000, 400, 0, 9_000_000, 120_000, "ac_wh_etl",
                    f"MERGE INTO {t} t USING updates u ON t.id = u.id WHEN MATCHED THEN UPDATE SET *",
                    3_000_000_000, 6_000_000_000, 2_000_000_000, 900, 100000, 8000, 0, 900000, 10.0, 0, 1))
    rows = []
    for w in WINDOWS:
        for t in tables:
            mine = sorted((r for r in top if r[0] == t and r[-1] < w), key=lambda r: (-r[8], r[3]))[:10]
            rows += [(w,) + r[:-1] for r in mine]
    put(con, "usage.table_queries", "window_days BIGINT, table_full_name VARCHAR, access VARCHAR, workspace_id VARCHAR, "
        "statement_id VARCHAR, identity VARCHAR, start_time VARCHAR, statement_type VARCHAR, status VARCHAR, total_ms BIGINT, "
        "read_bytes BIGINT, read_files BIGINT, pruned_files BIGINT, read_rows BIGINT, produced_rows BIGINT, warehouse_id VARCHAR, "
        "text VARCHAR, spilled_bytes BIGINT, shuffle_bytes BIGINT, written_bytes BIGINT, compile_ms BIGINT, exec_ms BIGINT, "
        "wait_compute_ms BIGINT, queue_ms BIGINT, task_ms BIGINT, cache_pct DOUBLE, result_cache BIGINT", rows)
    # jobs and notebooks on classic compute: in lineage, not in query history
    other = []
    for t in tables:
        ws = CATALOGS[t.split(".")[0]][0]
        picks = [("write", "JOB", SPS.get(ws, PEOPLE[ws])[0], rnd.randint(20, 90), 1), ("read", "NOTEBOOK", rnd.choice(PEOPLE[ws]), rnd.randint(1, 40), 3)]
        for w in WINDOWS:
            mine = [(w, t, a, via, who, max(1, n * w // 90), ts(last, 4)) for a, via, who, n, last in picks]
            other += [m + (sum(x[5] for x in mine),) for m in mine]
    put(con, "usage.table_other_access", "window_days BIGINT, table_full_name VARCHAR, access VARCHAR, via VARCHAR, identity VARCHAR, "
        "events BIGINT, last_event VARCHAR, table_events BIGINT", other)
    put(con, "usage.table_query_daily", "table_full_name VARCHAR, day VARCHAR, queries BIGINT, total_ms BIGINT, read_bytes BIGINT, "
        "read_files BIGINT, pruned_files BIGINT, full_scans BIGINT, spilled_bytes BIGINT, write_queries BIGINT, write_ms BIGINT, "
        "written_bytes BIGINT", per_day)


# warehouse -> (workspace, $ per up hour, typical up hours a day, share of up time busy)
WAREHOUSES = {"ac_wh_bi": ("2001", 12, 10, 0.35), "ac_wh_etl": ("2001", 24, 3, 0.85), "ac_wh_alwayson": ("2002", 6, 24, 0.02),
              "ac_wh_analytics": ("2005", 18, 9, 0.6), "ac_wh_fin_scd": ("2006", 10, 8, 0.5), "ac_wh_huge": ("2014", 60, 5, 0.3)}


def _warehouses(con, rnd) -> None:
    """warehouse.* (one warehouse in detail) and the cost_daily_by_resource rows its spend comes from."""
    con.execute("CREATE SCHEMA IF NOT EXISTS warehouse")
    daily, users, tables, slow, cost = [], [], [], [], []
    every = [t for cat, (_ws, _e, schemas) in CATALOGS.items() for sch, ts_ in schemas.items() for t in
             (f"{cat}.{sch}.{x}" for x in ts_)]
    for wh, (ws, rate, hours, busy_share) in WAREHOUSES.items():
        people = PEOPLE[ws] + SPS.get(ws, [])
        for n in range(90, 0, -1):
            d = (END - timedelta(days=n)).isoformat()
            weekend = (END - timedelta(days=n)).weekday() >= 5
            up = 0.0 if weekend and hours < 24 else max(0.0, rnd.gauss(hours, hours * 0.2)) * 60
            busy = round(up * min(1.0, max(0.0, rnd.gauss(busy_share, 0.08))))
            q = int(busy * rnd.uniform(0.5, 3)) if busy else 0
            daily.append((ws, wh, d, round(up, 1), float(busy), q, int(q * rnd.uniform(0, 0.04)),
                          min(len(PEOPLE[ws]), max(1, q // 40)) if q else 0, 1 if q and ws in SPS else 0,
                          int(q * rnd.uniform(50, 3000)), int(busy * 60_000 * rnd.uniform(0.5, 1.5))))
            for window in (7, 30, 90):
                if n <= window:
                    usd = round(up / 60 * rate, 2)
                    cost.append((window, d, "warehouse", f"{ws}:{wh}", ws, wh, wh, usd, round(usd / 0.7, 2)))
        for who in people:
            q = rnd.randint(5, 4000)
            users.append((ws, wh, who, q, int(q * rnd.uniform(0, 0.05)), q * rnd.randint(500, 20000), ts(rnd.randint(0, 6))))
        for t in rnd.sample(every, k=8):
            tables.append((ws, wh, t, rnd.randint(1, 3000), rnd.randint(0, 200), rnd.randint(1, len(people)), ts(rnd.randint(0, 10))))
        for i in range(10):
            ms = rnd.randint(60_000, 3_600_000)
            slow.append((ws, wh, f"stmt-{wh}-{i}", rnd.choice(people), ts(rnd.randint(0, 29), rnd.randint(0, 23)), ms,
                         rnd.randint(0, ms // 4), rnd.choice(["FINISHED", "FINISHED", "FAILED"]), "SELECT",
                         f"SELECT * FROM {rnd.choice(every)} WHERE day >= current_date() - 30"))
    put(con, "warehouse.warehouse_daily", "workspace_id VARCHAR, warehouse_id VARCHAR, day VARCHAR, up_minutes DOUBLE, "
        "busy_minutes DOUBLE, queries BIGINT, failed BIGINT, people BIGINT, service_principals BIGINT, queued_ms BIGINT, "
        "run_ms BIGINT", daily)
    put(con, "warehouse.warehouse_users", "workspace_id VARCHAR, warehouse_id VARCHAR, identity VARCHAR, queries BIGINT, "
        "failed BIGINT, run_ms BIGINT, last_query VARCHAR", users)
    put(con, "warehouse.warehouse_tables", "workspace_id VARCHAR, warehouse_id VARCHAR, table_full_name VARCHAR, "
        "reads BIGINT, writes BIGINT, people BIGINT, last_access VARCHAR", tables)
    put(con, "warehouse.warehouse_slow", "workspace_id VARCHAR, warehouse_id VARCHAR, statement_id VARCHAR, identity VARCHAR, "
        "start_time VARCHAR, total_ms BIGINT, queued_ms BIGINT, status VARCHAR, statement_type VARCHAR, text VARCHAR", slow)
    con.execute("CREATE OR REPLACE TABLE warehouse.warehouse_activity_info AS SELECT ? AS info",
                [json.dumps({"exported": True, "days": 90, "end": END.isoformat(), "tables": {"rows": len(tables), "error": None}})])
    # the snapshot build has no cost_daily_by_resource; a direct export does
    if not con.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = 'findings' "
                       "AND table_name = 'f_cost_daily_by_resource'").fetchone():
        put(con, "findings.f_cost_daily_by_resource", "window_days BIGINT, usage_date VARCHAR, resource_type VARCHAR, "
            "resource_key VARCHAR, workspace_id VARCHAR, resource_id VARCHAR, warehouse_id VARCHAR, usd_list DOUBLE, "
            "dbus DOUBLE", cost)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HERE / "acme.duckdb"))
    print(add(Path(ap.parse_args().db)))
