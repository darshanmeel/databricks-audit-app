"""app/core/materiality.py

Per-check materiality floors (config/materiality.yml, loaded via app_config.load_materiality()):
a row whose size/impact column sits under the check's own floor, AND whose own status is CRITICAL
or WARN, reads OK instead -- so status_counts, the worst status, the sort order, and a WARN/
CRITICAL money sum all ignore it. NOT_ASSESSED/OK rows are never touched by a floor (they have
nothing to suppress). The row's raw status survives as status_raw wherever the floored status
replaces `status` (app/core/data.py's read_finding); a query_id with no floor entry, or whose floor
column the built table does not carry, behaves exactly as today -- the same unaffected-if-absent
contract app/core/data.py already uses for job_id/tag/statuses.

    Floor                            one config/materiality.yml entry.
    floor_for(query_id)              the query's Floor, or None.
    applicable_floor(query_id, cols) the Floor to apply here, or None (also checks "status" is a
                                      real column and the table carries the floor's own column).
    status_expr(query_id, columns)   SQL text for the EFFECTIVE status (floor, then the report cap).
    below_floor_expr(query_id, columns)  SQL boolean text for below_floor, or None.
    severity_cap(query_id)           'WARN' for a report-type check capped there, else None.

A grant to a group listed under admin_groups in settings.yml reads OK in access_broad_grants:
admin groups are expected to hold broad grants.

A report id (REPORT_IDS_CAPPED_AT_WARN) never reads CRITICAL: Critical means broken, wasting
money or exposing data, and these are trend/chargeback reports -- a spend swing is a WARN at most.
status_expr applies the cap after the floor, on the SAME expression every other status read
already uses, so it reaches rows, counts, sort order and a WARN/CRITICAL money sum for free. The
row's raw status still survives as status_raw (app/core/data.py's read_finding).

Stdlib only, then app/core/config. Floors are cached against config/materiality.yml's own
(mtime, size) -- see _floors() -- so a request touching many query_ids reloads/revalidates the
file (and the registry it checks query_ids against) once, not once per query_id.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core import config as app_config


@dataclass(frozen=True)
class Floor:
    query_id: str
    column: str
    min: float
    unit: str
    label: str


# Trend/chargeback reports: a rise in spend or duration is a WARN at most, never CRITICAL (that
# reads as broken, wasting money or exposing data). See status_expr.
REPORT_IDS_CAPPED_AT_WARN = frozenset({
    "cost_period_over_period",
    "cost_chargeback_by_workspace",
    "cost_chargeback_by_service",
    "cost_chargeback_by_sku",
    "cost_chargeback_by_warehouse",
    "cost_chargeback_by_job",
    "cost_chargeback_by_cluster",
    "cost_chargeback_identity_by_source",
    "cost_chargeback_by_tag_value",
    "cost_chargeback_by_allocation_tag",
    "cost_unnamed_workspaces",
    "cost_sku_trend_12m",
    "cost_daily_spikes",
    "cost_by_hour_of_day",
    "query_top_by_cost",
    "storage_growth",
    "lakeflow_job_duration_regression",
})


def severity_cap(query_id: str) -> str | None:
    return "WARN" if query_id in REPORT_IDS_CAPPED_AT_WARN else None


def admin_group_expr(query_id: str, columns) -> str | None:
    """SQL boolean text for "granted to an admin group", or None when it does not apply."""
    if query_id != "access_broad_grants" or "grantee" not in columns:
        return None
    try:
        groups = app_config.load_settings()["admin_groups"]
    except app_config.ConfigError:
        return None  # the settings error is already shown on every page
    if not groups:
        return None
    names = ", ".join("'" + g.strip().lower().replace("'", "''") + "'" for g in groups)
    return f'LOWER(TRIM(t."grantee")) IN ({names})'


_CACHE: tuple[tuple, dict[str, Floor]] | None = None


def _cache_key() -> tuple:
    path = app_config.materiality_path()
    try:
        st = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), st.st_mtime_ns, st.st_size)


def _floors() -> dict[str, Floor]:
    global _CACHE
    key = _cache_key()
    if _CACHE is not None and _CACHE[0] == key:
        return _CACHE[1]
    floors = {
        query_id: Floor(query_id=query_id, **entry)
        for query_id, entry in app_config.load_materiality().items()
    }
    _CACHE = (key, floors)
    return floors


def floor_for(query_id: str) -> Floor | None:
    """This query's configured floor, or None. Cached on config/materiality.yml's own (mtime,
    size) -- never at import time (DEC-27): a changed file is picked up on the next call."""
    return _floors().get(query_id)


def applicable_floor(query_id: str, columns) -> Floor | None:
    """The floor to apply, or None when there is none configured, the table carries no `status`
    column at all, or the table does not carry the floor's own column (a stale config entry, or
    an account whose build never populates it -- never an error at read time)."""
    floor = floor_for(query_id)
    if floor is None or "status" not in columns or floor.column not in columns:
        return None
    return floor


def _min_sql(floor: Floor, columns) -> str:
    """The floor's own `min`, scaled for a USD floor to the table's own window_days (a USD floor
    is configured per 30 days -- config/materiality.yml's header -- so a 7d table must flag at
    1/30th the 30d amount, not the flat 30d figure). Non-USD floors (a count, DBU, minutes,
    bytes...) are absolute thresholds and are never scaled. Falls back to the flat `min` when the
    table carries no window_days column at all (a snapshot-type table)."""
    if floor.unit != "USD" or "window_days" not in columns:
        return f"{floor.min}"
    return f'(CASE WHEN t."window_days" > 0 THEN {floor.min} * t."window_days" / 30.0 ELSE {floor.min} END)'


def status_expr(query_id: str, columns) -> str:
    """SQL text for the EFFECTIVE status: the bare `t."status"` column when no floor applies, else
    a CASE that only ever moves a CRITICAL/WARN row to OK -- NOT_ASSESSED and OK rows pass through
    unchanged, so a floor can never manufacture a healthier verdict than the check itself gave the
    row (e.g. a genuinely unjudged row must never start reading OK). Always qualified with `t.`
    (every caller's FROM table -- app/core/data.py always aliases it that way, see its own
    _build_filters docstring) so this is safe to splice anywhere -- a WHERE/GROUP BY, or a SELECT
    list that also aliases its own output column "status" to this same expression, or an ORDER BY
    referencing it again -- without ever binding to that output alias by accident. `columns` is
    the table's own information_schema column list; the floor's `column` was validated against
    app.core.registry at config-load time (app/core/config.py)."""
    floor = applicable_floor(query_id, columns)
    cap = severity_cap(query_id)
    admin = admin_group_expr(query_id, columns)
    if floor is None and cap is None and admin is None:
        return 't."status"'
    whens = []
    if admin is not None:
        whens.append(f"WHEN t.\"status\" IN ('CRITICAL', 'WARN') AND {admin} THEN 'OK'")
    if floor is not None:
        whens.append(
            f"WHEN t.\"status\" IN ('CRITICAL', 'WARN') AND t.\"{floor.column}\" < {_min_sql(floor, columns)} "
            "THEN 'OK'"
        )
    if cap is not None:
        whens.append(f"WHEN t.\"status\" = 'CRITICAL' THEN '{cap}'")
    return "CASE " + " ".join(whens) + ' ELSE t."status" END'


def below_floor_expr(query_id: str, columns) -> str | None:
    """SQL boolean text for below_floor, or None when no floor applies (see status_expr) -- True
    only for a row the floor actually suppressed (CRITICAL/WARN under min), never an already-OK
    or NOT_ASSESSED row that merely happens to sit under the column's min."""
    floor = applicable_floor(query_id, columns)
    if floor is None:
        return None
    return f"t.\"status\" IN ('CRITICAL', 'WARN') AND t.\"{floor.column}\" < {_min_sql(floor, columns)}"
