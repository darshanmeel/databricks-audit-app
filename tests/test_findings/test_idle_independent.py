"""compute_warehouse_idle_minutes against a separate Python count of the same idle time, on random
fake warehouses: running periods from start/stop events, overlapping queries (some starting while
the warehouse is still starting), quiet periods and the auto-stop tail."""
from __future__ import annotations

import random
import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent
ROOT = TESTS_DIR.parent
for p in (TESTS_DIR, TESTS_DIR / "fixtures", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import ddl  # noqa: E402
import idle_waste as iw  # noqa: E402
import jinja_stub  # noqa: E402
from app.core.registry import by_id  # noqa: E402
from tools.generate_models import render_model  # noqa: E402

GAP_S = 60          # the check's default idle_gap_seconds
AUTO_STOP_S = 600


def _fake_warehouse(con, wh: str, rng: random.Random) -> dict:
    """Writes 3-6 running periods for `wh` and returns the expected minutes, counted by hand."""
    t = iw.DT(6, 8, 0, 0) + timedelta(seconds=rng.randint(0, 3600))
    exp = {"running": 0, "idle": 0, "counted": 0, "start": 0, "between": 0, "tail": 0, "no_query": 0, "busy": 0}
    for p in range(rng.randint(3, 6)):
        run0 = t + timedelta(seconds=rng.randint(5, 60))
        iw.we(con, wh, "STARTING", t, 0)
        iw.we(con, wh, "RUNNING", run0, 1)
        queries = []
        cursor = run0 - timedelta(seconds=30)
        for q in range(rng.choice([0, 1, 3, 6, 10])):
            cursor += timedelta(seconds=rng.choice([1, 20, 59, 61, 300, 1500]))
            dur = timedelta(seconds=rng.choice([1, 5, 45, 200, 900]))
            queries.append((cursor, cursor + dur))
        last_end = max([e for _, e in queries], default=run0)
        run1 = max(last_end, run0) + timedelta(seconds=AUTO_STOP_S)
        iw.we(con, wh, "STOPPING", run1, 0)
        iw.we(con, wh, "STOPPED", run1 + timedelta(seconds=30), 0)
        for i, (s, e) in enumerate(queries):
            iw.stmt(con, f"{wh}_p{p}_q{i}", wh, s, e)

        # Busy islands (queries touching or overlapping merge), clipped to the running period.
        islands: list[list] = []
        for s, e in sorted(queries):
            if islands and s <= islands[-1][1]:
                islands[-1][1] = max(islands[-1][1], e)
            else:
                islands.append([s, e])
        clipped = [(max(s, run0), min(e, run1)) for s, e in islands if s <= run1 and e >= run0]
        exp["running"] += (run1 - run0).total_seconds()
        exp["busy"] += sum((e - s).total_seconds() for s, e in clipped)
        marks = [(run0, run0, 0)] + [(s, e, 1) for s, e in clipped] + [(run1, run1, 2)]
        for (_, a_end, a_kind), (b_start, _, b_kind) in zip(marks, marks[1:]):
            g = (b_start - a_end).total_seconds()
            if g <= 0:
                continue
            kind = ("no_query" if a_kind == 0 and b_kind == 2 else "start" if a_kind == 0
                    else "tail" if b_kind == 2 else "between")
            exp["idle"] += g
            if g > GAP_S:
                exp["counted"] += g
                exp[kind] += g
        t = run1 + timedelta(seconds=30 + rng.randint(300, 6 * 3600))
    return {k: v / 60.0 for k, v in exp.items()}


@pytest.mark.parametrize("seed", range(8))
def test_idle_minutes_match_a_separate_count(seed):
    rng = random.Random(seed)
    con = duckdb.connect()
    try:
        for create_sql in ddl.DDL.values():
            con.execute(create_sql)
        expected = {f"fk_wh_{seed}_{n}": _fake_warehouse(con, f"fk_wh_{seed}_{n}", rng) for n in range(3)}
        sql = jinja_stub.render(render_model(by_id("compute_warehouse_idle_minutes"), target="duckdb"),
                                windows=(7,), source=jinja_stub.memory_source)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        got = {r["warehouse_id"]: r for r in (dict(zip(cols, row)) for row in cur.fetchall())}
        pairs = {"running": "running_minutes", "busy": "busy_minutes", "idle": "idle_minutes",
                 "counted": "counted_idle_minutes", "start": "start_gap_minutes",
                 "between": "between_queries_minutes", "tail": "stop_tail_minutes", "no_query": "no_query_minutes"}
        for wh, exp in expected.items():
            row = got[wh]
            for k, col in pairs.items():
                assert row[col] == pytest.approx(exp[k], abs=0.06), (wh, col, row[col], exp[k])
            assert row["running_minutes"] == pytest.approx(row["busy_minutes"] + row["idle_minutes"], abs=0.15)
    finally:
        con.close()
