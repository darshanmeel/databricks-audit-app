"""app/core/rollup.py -- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.2): the request-time nested cost
and performance rollup. Reads tags.cost_unit / tags.cost_unit_tag / tags.cost_reconciliation (or
their performance twins) plus the shared tags.tag_workspace (TAG-IDX-owned, never edited here)
through its own read-only DuckDB connection -- this module never imports app/api/service.py and
never edits app/core/data.py (section 4.7: app/core/rollup.py is TAG-ROLL's own new file). The one
function it calls on app/core/data.py is its already-public read_dim_workspace() (for the `env`/
attribute-key filters GET /api/rollup accepts, same as every other route), never a private one.

Everything here works from ONE tag key at a time (the nested rollup's own contract, section 3.1):
walk each cost/performance "unit" from the work level outward to the workspace level, take the
first REAL value (never `__mixed__`/`__untagged__`), and that is the unit's effective value and
attributed level (section 2). A unit's own compute-level value applies to its whole total (one
map, one set of values per unit); its work-level value(s) partition that same total exactly (one
or more tagged slices plus, when they do not cover it all, one untagged/none remainder) -- so the
tree is built by walking, per unit: one workspace bucket, one compute bucket, N work buckets that
sum to the unit's own total. No cross product between levels is needed or correct.

Stdlib + duckdb only.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from app.core import config as app_config
from app.core import data as app_core_data
from app.core import tag_keys
from app.core import tags as app_core_tags


WINDOW_CHOICES = (7, 30, 90)
AREAS = ("cost", "performance")

COST_METRIC_KEYS = ("usd",)
PERF_METRIC_KEYS = ("statements", "failed_statements", "duration_ms", "queue_ms", "spill_bytes")

LEVELS = [
    {"level": "account", "label": "Account"},
    {"level": "workspace", "label": "Workspace tag"},
    {"level": "compute", "label": "Compute tag"},
    {"level": "work", "label": "Query / job tag"},
]

# by_tag_top's own, more granular levels -- never the nested rollup's account/workspace/compute/
# work above. "billing line" is a compute-level tag billed against anything that is not a named
# warehouse/cluster (an endpoint, app or serverless budget policy) -- a line on the bill, not a
# resource with its own identity.
TOP_LEVELS = ("query", "warehouse", "cluster", "job", "pipeline", "workspace", "billing line")

_REQUIRED_SOURCES = {
    "cost": [("billing", "usage"), ("billing", "list_prices")],
    "performance": [("query", "history")],
}
# section 6.5: a source that is not usable degrades the label at the level it feeds, but never
# turns the value into "untagged" -- the effect text is attached to source_status, read by the UI.
_DEGRADING_SOURCES = {
    "cost": [
        ("billing", "attributed_usage",
         "query tags not assessed: billing.attributed_usage was not exported"),
        ("lakeflow", "jobs", "job tags not assessed (lakeflow.jobs was not exported)"),
        ("lakeflow", "pipelines", "pipeline tags not assessed (lakeflow.pipelines was not exported)"),
    ],
    "performance": [
        ("compute", "warehouses", "warehouse tags not assessed (compute.warehouses was not exported)"),
        ("compute", "clusters", "cluster tags not assessed (compute.clusters was not exported)"),
    ],
}


class RollupError(ValueError):
    """A bad area/window/tag_key -- app/api/app.py turns this into a 422."""


def _db_path() -> Path:
    # The same lookup as every other read (AUDIT_DB, the last load or refresh, the data folder).
    return app_core_data._db_path()  # noqa: SLF001


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(_db_path()), read_only=True)


def _tags_models_exist(con: duckdb.DuckDBPyConnection) -> bool:
    tables = {"cost_unit", "cost_unit_tag", "cost_reconciliation", "perf_unit", "perf_unit_tag", "tag_workspace"}
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'tags'"
    ).fetchall()
    have = {r[0] for r in rows}
    return tables.issubset(have)


def _validate(area: str, tag_key: str | None, window_days: int) -> str:
    if area not in AREAS:
        raise RollupError(f"area must be one of {AREAS}, got {area!r}")
    if window_days not in WINDOW_CHOICES:
        raise RollupError(f"window must be one of {list(WINDOW_CHOICES)}, got {window_days}")
    norm = tag_keys.normalize_tag_key(tag_key)
    if not norm:
        raise RollupError("tag_key is required and must not be empty after normalising")
    return norm


def _source_state(manifest: dict, schema: str, table: str) -> str | None:
    if not manifest.get("available"):
        return None
    info = manifest.get("tables", {}).get(f"system.{schema}.{table}")
    return (info or {}).get("state", "not_assessed")


def _usable(state: str | None) -> bool:
    return state is None or state in ("ok", "partial")


def _direct_missing_sources() -> set[str]:
    """"system.schema.table" names a direct export could not read (empty set on a dbt/snapshot
    build, or an older direct db with no such record)."""
    direct = app_core_data.direct_export_info()
    return set((direct or {}).get("tags_sources_not_exported") or [])


def _source_status(area: str) -> tuple[list[dict], bool, str | None]:
    """Returns (source_status rows, required_sources_ok, not_assessed_reason). Every source this
    area reads gets exactly one row -- required or degrading -- with effect=None when it is usable
    (section 4.5's own example: an "ok" source still gets a row, effect null), matching what
    section 6.5's table says about each one. A direct export's own missing sources count the same
    as a snapshot's "not_assessed" state."""
    manifest = app_core_data.snapshot_manifest()
    direct_missing = _direct_missing_sources()
    rows: list[dict] = []
    required_ok = True
    reason = None
    for schema, table in _REQUIRED_SOURCES[area]:
        fqn = f"system.{schema}.{table}"
        state = "not_assessed" if fqn in direct_missing else _source_state(manifest, schema, table)
        usable = _usable(state)
        effect = None
        if not usable:
            required_ok = False
            effect = f"system.{schema}.{table} was not exported"
            reason = effect
        rows.append({"source": fqn, "state": state or "ok", "effect": effect})
    for schema, table, degraded_effect in _DEGRADING_SOURCES.get(area, []):
        fqn = f"system.{schema}.{table}"
        state = "not_assessed" if fqn in direct_missing else _source_state(manifest, schema, table)
        effect = None if _usable(state) else degraded_effect
        rows.append({"source": fqn, "state": state or "ok", "effect": effect})
    return rows, required_ok, reason


def _degraded_effect(area: str) -> dict[tuple[str, str], str]:
    manifest = app_core_data.snapshot_manifest()
    direct_missing = _direct_missing_sources()
    out: dict[tuple[str, str], str] = {}
    for schema, table, effect in _DEGRADING_SOURCES.get(area, []):
        fqn = f"system.{schema}.{table}"
        state = "not_assessed" if fqn in direct_missing else _source_state(manifest, schema, table)
        if not _usable(state):
            out[(schema, table)] = effect
    return out


# ---------------------------------------------------------------------------------------------
# Labels (section 6.3) -- plain words, rendered as given.
# ---------------------------------------------------------------------------------------------

def _workspace_label(value: str | None, reason: str | None) -> str:
    if value == tag_keys.NONE:
        return "no workspace (account-level usage)"
    if value == tag_keys.MIXED:
        return "workspace tag: mixed"
    if value == tag_keys.UNTAGGED:
        return "untagged workspace"
    if value == "":
        return "workspace tag: (no value)"
    return f"workspace tag: {value}"


def _compute_label(compute_kind: str, compute_id: str | None, value: str | None, degraded: str | None,
                    area: str = "cost") -> str:
    kind_word = {
        "warehouse": "warehouse", "cluster": "cluster", "endpoint": "serving endpoint",
        "app": "app", "other": "other usage",
    }.get(compute_kind)
    if compute_kind == "serverless" and area == "performance":
        # section 6.3: performance has no budget-policy concept (query.history carries no billing
        # ids) -- a serverless statement with neither a warehouse nor a cluster reads plainly.
        return degraded or "serverless compute (no tags)"
    if compute_kind == "serverless":
        has_policy = compute_id is not None
        if degraded:
            return degraded
        if value is None:
            return "untagged budget policy" if has_policy else "serverless, no budget policy"
        if value == "":
            return "budget policy tag: (no value)" if has_policy else "serverless tag: (no value)"
        return f"budget policy tag: {value}" if has_policy else f"serverless tag: {value}"
    if degraded:
        return degraded
    if value is None:
        return f"untagged {kind_word}" if kind_word else "untagged"
    if value == "":
        return f"{kind_word} tag: (no value)" if kind_word else "(no value)"
    return f"{kind_word} tag: {value}" if kind_word else f"tag: {value}"


def _work_label(work_kind: str, value: str | None, degraded: str | None, compute_kind: str | None = None) -> str:
    if work_kind == "warehouse_idle":
        return "idle or not attributed to a query"
    if work_kind == "warehouse_unsplit":
        return "not split to queries (no attributed usage for that day)"
    if work_kind == "none":
        if degraded is not None:
            return degraded
        # section 6.3: "none" on a cluster reads as all-purpose/interactive; "none" elsewhere
        # (serving endpoints, apps, serverless notebooks, "other" usage, non-DBU warehouse rows)
        # is not a cluster at all, so it reads as no query or job.
        return "no per-query cost (all-purpose or interactive)" if compute_kind == "cluster" else "no query or job"
    kind_word = {"query": "query", "job": "job", "pipeline": "pipeline"}.get(work_kind, work_kind)
    if value is None or value == tag_keys.UNTAGGED:
        if degraded:
            return degraded
        return "untagged queries" if kind_word == "query" else f"untagged {kind_word}"
    if value == "":
        return f"{kind_word} tag: (no value)"
    return f"{kind_word} tag: {value}"


# ---------------------------------------------------------------------------------------------
# The core walk: one path per (unit, work-bucket).
# ---------------------------------------------------------------------------------------------

class _Path:
    __slots__ = (
        "unit_id",
        "workspace_id", "workspace_value", "workspace_source", "workspace_reason",
        "compute_kind", "compute_id", "compute_value", "compute_source",
        "work_kind", "work_id", "work_value", "work_source",
        "metrics",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _effective(p: _Path) -> tuple[str, str, str | None]:
    """(effective_value, attributed_level, attributed_source) -- section 2's walk, innermost
    (work) to outermost (workspace); `__mixed__`/`__untagged__`/`__none__` never allocate."""
    for value, level, source in (
        (p.work_value, "work", p.work_source),
        (p.compute_value, "compute", p.compute_source),
        (p.workspace_value, "workspace", p.workspace_source),
    ):
        if tag_keys.is_real_value(value):
            return value, level, source
    return tag_keys.UNTAGGED, "account", None


def _overrides_outer(p: _Path, effective_value: str, attributed_level: str) -> bool:
    """True when an OUTER level (relative to attributed_level) carries a DIFFERENT real value."""
    order = ("work", "compute", "workspace")
    idx = order.index(attributed_level) if attributed_level in order else len(order)
    outer = order[idx + 1:]
    for level in outer:
        value = {"work": p.work_value, "compute": p.compute_value, "workspace": p.workspace_value}[level]
        if tag_keys.is_real_value(value) and value != effective_value:
            return True
    return False


def _fetch_cost_paths(con, window_days: int, norm_key: str, unit_ids: set[str] | None = None) -> list[_Path]:
    units_filter, units_params = _unit_filter("u.unit_id", unit_ids)
    work_filter, work_params = _unit_filter("unit_id", unit_ids)
    units = con.execute(
        f"""
        SELECT u.unit_id, u.workspace_id, u.compute_kind, u.compute_id, u.work_kind, u.work_id,
               u.usd, ct.tag_value, ct.source
        FROM tags.cost_unit u
        LEFT JOIN tags.cost_unit_tag ct
          ON ct.window_days = u.window_days AND ct.unit_id = u.unit_id AND ct.level = 'compute'
         AND ct.tag_key = ?
        WHERE u.window_days = ?{units_filter}
        """,
        [norm_key, window_days, *units_params],
    ).fetchall()
    work_rows = con.execute(
        f"""
        SELECT unit_id, tag_value, source, usd
        FROM tags.cost_unit_tag
        WHERE window_days = ? AND level = 'work' AND tag_key = ?{work_filter}
        """,
        [window_days, norm_key, *work_params],
    ).fetchall()
    ws_rows = con.execute(
        "SELECT workspace_id, tag_value, reason FROM tags.tag_workspace WHERE tag_key = ?", [norm_key]
    ).fetchall()
    ws_map = {r[0]: (r[1], r[2]) for r in ws_rows}
    work_by_unit: dict[str, list[tuple]] = {}
    for unit_id, value, source, usd in work_rows:
        work_by_unit.setdefault(unit_id, []).append((value, source, usd))

    paths: list[_Path] = []
    for unit_id, workspace_id, compute_kind, compute_id, work_kind, work_id, usd, cvalue, csource in units:
        if workspace_id is None:
            wvalue, wsource, wreason = tag_keys.NONE, None, None
        elif workspace_id in ws_map:
            wvalue, wreason = ws_map[workspace_id]
            wsource = "workspace_inferred"
        else:
            wvalue, wsource, wreason = tag_keys.UNTAGGED, None, "no billed usage in this workspace carries the tag"
        rows = work_by_unit.get(unit_id, [])
        tagged_sum = sum(r[2] for r in rows)
        remainder = usd - tagged_sum
        entries = list(rows)
        untagged_value = tag_keys.UNTAGGED if work_kind in ("query", "job", "pipeline") else tag_keys.NONE
        if abs(remainder) > 1e-9:
            entries.append((untagged_value, None, remainder))
        elif not entries:
            entries.append((untagged_value, None, 0.0))
        for wvalue2, wsource2, wusd in entries:
            paths.append(_Path(
                unit_id=unit_id,
                workspace_id=workspace_id, workspace_value=wvalue, workspace_source=wsource, workspace_reason=wreason,
                compute_kind=compute_kind, compute_id=compute_id, compute_value=cvalue, compute_source=csource,
                work_kind=work_kind, work_id=work_id, work_value=wvalue2, work_source=wsource2,
                metrics={"usd": wusd},
            ))
    return paths


def _fetch_perf_paths(con, window_days: int, norm_key: str, unit_ids: set[str] | None = None) -> list[_Path]:
    units_filter, units_params = _unit_filter("u.unit_id", unit_ids)
    work_filter, work_params = _unit_filter("unit_id", unit_ids)
    units = con.execute(
        f"""
        SELECT u.unit_id, u.workspace_id, u.compute_kind, u.compute_id,
               u.statements, u.failed_statements, u.duration_ms, u.queue_ms, u.spill_bytes,
               ct.tag_value, ct.source
        FROM tags.perf_unit u
        LEFT JOIN tags.perf_unit_tag ct
          ON ct.window_days = u.window_days AND ct.unit_id = u.unit_id AND ct.level = 'compute'
         AND ct.tag_key = ?
        WHERE u.window_days = ?{units_filter}
        """,
        [norm_key, window_days, *units_params],
    ).fetchall()
    work_rows = con.execute(
        f"""
        SELECT unit_id, tag_value, source, statements, failed_statements, duration_ms, queue_ms, spill_bytes
        FROM tags.perf_unit_tag
        WHERE window_days = ? AND level = 'work' AND tag_key = ?{work_filter}
        """,
        [window_days, norm_key, *work_params],
    ).fetchall()
    ws_rows = con.execute(
        "SELECT workspace_id, tag_value, reason FROM tags.tag_workspace WHERE tag_key = ?", [norm_key]
    ).fetchall()
    ws_map = {r[0]: (r[1], r[2]) for r in ws_rows}
    work_by_unit: dict[str, list[tuple]] = {}
    for unit_id, value, source, *m in work_rows:
        work_by_unit.setdefault(unit_id, []).append((value, source, m))

    paths: list[_Path] = []
    for unit_id, workspace_id, compute_kind, compute_id, stmts, failed, dur, queue, spill, cvalue, csource in units:
        unit_metrics = {
            "statements": stmts, "failed_statements": failed, "duration_ms": dur,
            "queue_ms": queue, "spill_bytes": spill,
        }
        if workspace_id is None:
            wvalue, wsource, wreason = tag_keys.NONE, None, None
        elif workspace_id in ws_map:
            wvalue, wreason = ws_map[workspace_id]
            wsource = "workspace_inferred"
        else:
            wvalue, wsource, wreason = tag_keys.UNTAGGED, None, "no billed usage in this workspace carries the tag"
        rows = work_by_unit.get(unit_id, [])
        tagged = {k: 0 for k in PERF_METRIC_KEYS}
        for _v, _s, m in rows:
            for k, mv in zip(PERF_METRIC_KEYS, m):
                tagged[k] += mv or 0
        remainder = {k: (unit_metrics[k] or 0) - tagged[k] for k in PERF_METRIC_KEYS}
        entries = [(v, s, dict(zip(PERF_METRIC_KEYS, m))) for v, s, m in rows]
        untagged_value = tag_keys.UNTAGGED
        if any(abs(remainder[k]) > 1e-9 for k in PERF_METRIC_KEYS) or remainder["statements"] > 0:
            entries.append((untagged_value, None, remainder))
        elif not entries:
            entries.append((untagged_value, None, {k: 0 for k in PERF_METRIC_KEYS}))
        for wvalue2, wsource2, wmetrics in entries:
            paths.append(_Path(
                unit_id=unit_id,
                workspace_id=workspace_id, workspace_value=wvalue, workspace_source=wsource, workspace_reason=wreason,
                compute_kind=compute_kind, compute_id=compute_id, compute_value=cvalue, compute_source=csource,
                work_kind="query", work_id=None, work_value=wvalue2, work_source=wsource2,
                metrics=wmetrics,
            ))
    return paths


def _zero(metric_keys) -> dict:
    return {k: 0 for k in metric_keys}


def _add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def _primary(metrics: dict, primary_key: str) -> float:
    return metrics.get(primary_key, 0) or 0


def _round_metrics(metrics: dict, discount_pct: float, area: str) -> dict:
    if area != "cost":
        return dict(metrics)
    usd = metrics.get("usd", 0) or 0
    return {"usd": round(usd, 2), "usd_disc": round(usd * (1 - discount_pct), 2)}


def _resolve_workspace_scope(
    workspace_ids: list[str] | None, env: list[str] | None, attributes: dict[str, list[str]] | None
) -> set[str] | None:
    """The workspace id set workspace_ids/env/attributes resolve to, via read_dim_workspace() (the
    same public dim every other route reads) -- None means "no scope filter at all" (every path
    kept); an empty df read failure degrades to an empty set (nothing kept) rather than raising, the
    same defensive shape every other route's guard uses. Shared by rollup() and by_tag_top()."""
    if not (workspace_ids or env or attributes):
        return None
    try:
        df = app_core_data.read_dim_workspace()
    except Exception:  # pragma: no cover - defensive, matches other routes' guards
        return set()
    mask = None
    if workspace_ids:
        m = df["workspace_id"].astype(str).isin(workspace_ids)
        mask = m if mask is None else (mask & m)
    if env and "env" in df.columns:
        m = df["env"].isin(env)
        mask = m if mask is None else (mask & m)
    if attributes:
        for key, values in attributes.items():
            if key in df.columns:
                m = df[key].isin(values)
                mask = m if mask is None else (mask & m)
    if mask is not None:
        return set(df.loc[mask, "workspace_id"].astype(str))
    return set(df["workspace_id"].astype(str))


def _unit_filter(col: str, unit_ids: set[str] | None) -> tuple[str, list]:
    """SQL fragment (and its params) narrowing `col` to `unit_ids` -- "" when there is no filter
    at all, "AND FALSE" when the filter matched no unit (never an unfiltered read)."""
    if unit_ids is None:
        return "", []
    if not unit_ids:
        return " AND FALSE", []
    placeholders = ", ".join("?" for _ in unit_ids)
    return f" AND {col} IN ({placeholders})", list(unit_ids)


def _matching_unit_ids(
    con, area: str, window_days: int,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None",
    exclude_key: str | None = None,
) -> set[str] | None:
    """unit_ids whose EFFECTIVE value for each group's key -- the SAME work > compute >
    workspace precedence _effective() applies, not just any matching row at any level -- matches
    `tag_filter` (several tag NAMES ANDed, each name's own values ORed, same TagFilterSet contract
    app/core/data._build_filters compiles). None when no filter was given, or when every group's
    key is `exclude_key` (nothing left to narrow BY UNIT), else the matching set (possibly empty).
    An empty `values` group means "All": any real value, __untagged__ excluded, the same meaning
    TagValueGroup documents. A wanted __untagged__ matches a unit with no real value at any level --
    there is never a literal stored row for it.

    `exclude_key` (rollup()'s own norm_key) skips that one group here: a single unit can carry
    SEVERAL effective values under the SAME key across its own work items (one warehouse's queries
    split real per-query tags from an untagged remainder that falls back to the warehouse's own
    tag), so matching by whole unit would also pull in a sibling path with a different value under
    that key. rollup() matches that one group PER PATH instead, at the exact grain by_value/
    selected already use -- see _own_key_wanted below."""
    if tag_filter is None:
        return None
    filter_set = (
        tag_filter if isinstance(tag_filter, app_core_tags.TagFilterSet)
        else app_core_tags.TagFilterSet.single(tag_filter)
    )
    fetch = _fetch_cost_paths if area == "cost" else _fetch_perf_paths
    result: set[str] | None = None
    matched_group = False
    for group in filter_set.groups:
        norm_key = tag_keys.normalize_tag_key(group.key)
        if exclude_key is not None and norm_key == exclude_key:
            continue
        matched_group = True
        wanted = set(group.values) if group.values else None
        ids: set[str] = set()
        if norm_key:
            for p in fetch(con, window_days, norm_key):
                eff_value, _level, _source = _effective(p)
                match = eff_value != tag_keys.UNTAGGED if wanted is None else eff_value in wanted
                if match:
                    ids.add(p.unit_id)
        result = ids if result is None else (result & ids)
    if not matched_group:
        return None
    return result if result is not None else set()


def _own_key_wanted(
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None", norm_key: str,
) -> tuple[bool, set[str] | None]:
    """(has_group, wanted) for the tag_filter group (if any) whose key IS this rollup's own
    norm_key -- matched PER PATH in rollup(), never through _matching_unit_ids' per-unit narrowing
    (see its docstring). wanted=None means "All" (any real value); has_group=False means tag_filter
    carries no group on this key at all (nothing to narrow this way)."""
    if tag_filter is None:
        return False, None
    filter_set = (
        tag_filter if isinstance(tag_filter, app_core_tags.TagFilterSet)
        else app_core_tags.TagFilterSet.single(tag_filter)
    )
    for group in filter_set.groups:
        if tag_keys.normalize_tag_key(group.key) == norm_key:
            return True, (set(group.values) if group.values else None)
    return False, None


def rollup(
    area: str,
    tag_key: str,
    window_days: int,
    tag_value: str | None = None,
    workspace_ids: list[str] | None = None,
    env: list[str] | None = None,
    attributes: dict[str, list[str]] | None = None,
    tag_values: list[str] | None = None,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> dict:
    """`tag_value` marks one `by_value` entry as `selected` (unchanged); `tag_values` (plural, OR
    within this one key -- rollup always builds the tree for a single `tag_key`) additionally marks
    every matching entry in the new `selected_values` list. `tag_filter` (C26) is the SAME active
    tag filter /api/finding's `aggregate` applies -- narrows the units this rollup sums to those
    matching it, the same way workspace_ids/env/attributes already do."""
    norm_key = _validate(area, tag_key, window_days)
    primary_key = "usd" if area == "cost" else "statements"
    metric_keys = COST_METRIC_KEYS if area == "cost" else PERF_METRIC_KEYS

    con = _connect()
    try:
        if not _tags_models_exist(con):
            return _not_assessed_body(area, norm_key, window_days, "tag_models_not_built: the tag rollup was not built; rebuild to see it", display_key=tag_key)

        source_status, required_ok, na_reason = _source_status(area)
        if not required_ok:
            return _not_assessed_body(area, norm_key, window_days, na_reason, source_status=source_status, display_key=tag_key)

        resolved_ws = _resolve_workspace_scope(workspace_ids, env, attributes)
        unit_ids = _matching_unit_ids(con, area, window_days, tag_filter, exclude_key=norm_key)

        paths = _fetch_cost_paths(con, window_days, norm_key, unit_ids) if area == "cost" \
            else _fetch_perf_paths(con, window_days, norm_key, unit_ids)

        # C26's tag filter on THIS SAME key (norm_key) narrows per PATH, not per unit -- a unit can
        # carry several effective values under one key across its own work items, so
        # _matching_unit_ids above deliberately skips this group (see its docstring).
        has_own_group, own_wanted = _own_key_wanted(tag_filter, norm_key)
        if has_own_group:
            def _own_match(p: _Path) -> bool:
                eff_value, _level, _source = _effective(p)
                return eff_value != tag_keys.UNTAGGED if own_wanted is None else eff_value in own_wanted
            paths = [p for p in paths if _own_match(p)]

        excluded_account = _zero(metric_keys)
        workspace_filtered = resolved_ws is not None
        kept: list[_Path] = []
        for p in paths:
            if resolved_ws is not None:
                if p.workspace_id is None:
                    excluded_account = _add(excluded_account, p.metrics)
                    continue
                if p.workspace_id not in resolved_ws:
                    continue
            kept.append(p)
        paths = [p for p in kept if any(abs(v) > 1e-9 for v in p.metrics.values())]

        discount_pct = 0.0
        if area == "cost":
            try:
                discount_pct = float(app_config.load_settings().get("discount_pct", 0.0))
            except Exception:  # pragma: no cover
                discount_pct = 0.0

        degraded = _degraded_effect(area)

        tree = _build_tree(paths, area, metric_keys, primary_key, degraded)
        by_value = _build_by_value(paths, metric_keys, primary_key)
        total_metrics = tree["metrics"]

        reconciliation = _reconciliation(con, area, window_days, total_metrics)

        # ok_empty_filters when a filter is active and matches nothing (window has data, this
        # scope does not) -- ok_empty_window when the window itself has nothing at all (C26).
        filter_active = workspace_filtered or tag_filter is not None
        outcome = "ok_rows" if paths else ("ok_empty_filters" if filter_active else "ok_empty_window")

        selected = None
        if tag_value is not None:
            # by_value's own "value" field is already API-spelled (model_value_to_api), so compare
            # directly against the raw query param -- never re-convert it back to model spelling.
            for entry in by_value:
                if entry["value"] == tag_value:
                    selected = entry
                    break

        # tag_values (plural): OR-selects several by_value entries at once -- additive to `selected`
        # above, which stays exactly the single-value shape it always was (backward compatible).
        selected_values = [e for e in by_value if e["value"] in tag_values] if tag_values else None

        display_key = _display_key(con, norm_key)

        unpriced = []
        if area == "cost":
            unpriced = _unpriced(con, window_days)

        metrics_meta = [{"key": "usd", "label": "list price (effective)", "unit": "usd"}] if area == "cost" else [
            {"key": "statements", "label": "statements", "unit": "count"},
            {"key": "failed_statements", "label": "failed statements", "unit": "count"},
            {"key": "duration_ms", "label": "total duration", "unit": "ms"},
            {"key": "queue_ms", "label": "queue time", "unit": "ms"},
            {"key": "spill_bytes", "label": "spill", "unit": "bytes"},
        ]

        body = {
            "area": area,
            "tag_key": norm_key,
            "display_key": display_key,
            "window_days": window_days,
            "outcome": outcome,
            "not_assessed_reason": None,
            "metrics": metrics_meta,
            "primary_metric": primary_key,
            "discount_pct": discount_pct,
            "total": _round_metrics(total_metrics, discount_pct, area),
            "unpriced": unpriced,
            "reconciliation": reconciliation,
            "scope": {
                "workspace_filtered": workspace_filtered,
                "excluded_account_level": _round_metrics(excluded_account, discount_pct, area) if area == "cost" else excluded_account,
            },
            "source_status": source_status,
            "levels": LEVELS,
            "tree": tree,
            "by_value": by_value,
            "selected": selected,
            "selected_values": selected_values,
            "paths": _paths_out(paths, area, discount_pct),
        }
        return body
    finally:
        con.close()


def _display_key(con, norm_key: str) -> str:
    try:
        row = con.execute(
            "SELECT display_key FROM tags.tag_workspace WHERE tag_key = ? LIMIT 1", [norm_key]
        ).fetchone()
        if row and row[0]:
            return row[0]
        row = con.execute(
            "SELECT raw_key FROM tags.cost_unit_tag WHERE tag_key = ? ORDER BY raw_key LIMIT 1", [norm_key]
        ).fetchone()
        if row and row[0]:
            return row[0]
        row = con.execute(
            "SELECT raw_key FROM tags.perf_unit_tag WHERE tag_key = ? ORDER BY raw_key LIMIT 1", [norm_key]
        ).fetchone()
        if row and row[0]:
            return row[0]
    except duckdb.Error:
        pass
    return norm_key


def _unpriced(con, window_days: int) -> list[dict]:
    try:
        rows = con.execute(
            "SELECT usage_unit, SUM(unit_unpriced_quantity) FROM tags.cost_reconciliation "
            "WHERE window_days = ? AND unit_unpriced_quantity > 0 GROUP BY usage_unit",
            [window_days],
        ).fetchall()
    except duckdb.Error:
        return []
    return [{"usage_unit": u, "quantity": round(q, 4)} for u, q in rows]


def _reconciliation(con, area: str, window_days: int, total_metrics: dict) -> dict:
    if area == "cost":
        try:
            row = con.execute(
                "SELECT SUM(billing_usd), SUM(unit_usd) FROM tags.cost_reconciliation WHERE window_days = ?",
                [window_days],
            ).fetchone()
        except duckdb.Error:
            row = (None, None)
        billing_usd = round(row[0], 2) if row and row[0] is not None else 0.0
        rollup_usd = round(row[1], 2) if row and row[1] is not None else 0.0
        dbu_row = con.execute(
            "SELECT attributed_dbus, attributed_unmatched_dbus, scaled_warehouse_days, "
            "warehouse_idle_usd, warehouse_unsplit_usd FROM tags.cost_reconciliation "
            "WHERE window_days = ? AND usage_unit = 'DBU'",
            [window_days],
        ).fetchone()
        dbu_row = dbu_row or (0.0, 0.0, 0, 0.0, 0.0)
        return {
            "scope": "account",
            "billing_usd": billing_usd,
            "rollup_usd": rollup_usd,
            "difference_usd": round(billing_usd - rollup_usd, 2),
            "reconciled": abs(billing_usd - rollup_usd) < 0.005,
            "attributed_dbus": dbu_row[0] or 0.0,
            "attributed_unmatched_dbus": dbu_row[1] or 0.0,
            "scaled_warehouse_days": dbu_row[2] or 0,
            "warehouse_idle_usd": dbu_row[3] or 0.0,
            "warehouse_unsplit_usd": dbu_row[4] or 0.0,
        }
    history_total = con.execute(
        "SELECT SUM(statements) FROM tags.perf_unit WHERE window_days = ?", [window_days]
    ).fetchone()
    history_statements = int(history_total[0]) if history_total and history_total[0] is not None else 0
    return {
        "scope": "account",
        "history_statements": history_statements,
        "rollup_statements": history_statements,
        "reconciled": True,
    }


def _not_assessed_body(area, norm_key, window_days, reason, source_status=None, display_key=None):
    metrics_meta = [{"key": "usd", "label": "list price (effective)", "unit": "usd"}] if area == "cost" else [
        {"key": "statements", "label": "statements", "unit": "count"},
        {"key": "failed_statements", "label": "failed statements", "unit": "count"},
        {"key": "duration_ms", "label": "total duration", "unit": "ms"},
        {"key": "queue_ms", "label": "queue time", "unit": "ms"},
        {"key": "spill_bytes", "label": "spill", "unit": "bytes"},
    ]
    return {
        "area": area, "tag_key": norm_key, "display_key": display_key or norm_key, "window_days": window_days,
        "outcome": "not_assessed", "not_assessed_reason": reason,
        "metrics": metrics_meta, "primary_metric": "usd" if area == "cost" else "statements",
        "discount_pct": 0.0, "total": None, "unpriced": [], "reconciliation": None,
        "scope": {"workspace_filtered": False, "excluded_account_level": None},
        "source_status": source_status or [], "levels": LEVELS,
        "tree": None, "by_value": [], "selected": None, "selected_values": None, "paths": [],
    }


# ---------------------------------------------------------------------------------------------
# Tree / by_value / paths construction
# ---------------------------------------------------------------------------------------------

def _node_id(*parts: str) -> str:
    return "/".join(parts)


def _new_node(node_id, level, kind, value, label, source, reason, metric_keys) -> dict:
    return {
        "id": node_id, "level": level, "kind": kind, "value": value, "label": label,
        "source": source, "source_label": tag_keys.SOURCE_LABELS.get(source), "reason": reason,
        "metrics": _zero(metric_keys), "untagged": _zero(metric_keys), "attributed_here": _zero(metric_keys),
        "share_of_parent": 0.0, "share_of_total": 0.0,
        "_children": {}, "_workspace_ids": {}, "_resources": {},
    }


def _finalize_node(node: dict, area: str, discount_pct: float, parent_total: float, grand_total: float,
                    primary_key: str, workspaces_map=None, resources_map=None) -> dict:
    # Equal totals fall back to the key, so the tree reads the same on every request.
    children = [c for _, c in sorted(node["_children"].items(), key=lambda kv: (-_primary(kv[1]["metrics"], primary_key), str(kv[0])))]
    # sentinel (untagged/mixed/none) nodes last, per spec ("Children are sorted by the primary
    # metric, descending, with sentinel nodes last").
    def _is_sentinel(c):
        return isinstance(c.get("value"), str) and c["value"].startswith("__")
    ordered = [c for c in children if not _is_sentinel(c)] + [c for c in children if _is_sentinel(c)]
    finalized = []
    my_total = _primary(node["metrics"], primary_key)
    for c in ordered:
        finalized.append(_finalize_node(c, area, discount_pct, my_total, grand_total, primary_key, workspaces_map, resources_map))
    node["children"] = finalized
    node.pop("_children", None)

    if node["level"] == "workspace" and node["_workspace_ids"]:
        ws_list = sorted(node["_workspace_ids"].items(), key=lambda kv: (-_primary(kv[1], primary_key), str(kv[0])))[:20]
        node["workspaces"] = [
            {"workspace_id": wid, "metrics": _round_metrics(m, discount_pct, area)} for wid, m in ws_list
        ]
    node.pop("_workspace_ids", None)
    if node["level"] == "compute" and node["_resources"]:
        res_list = sorted(node["_resources"].items(), key=lambda kv: (-_primary(kv[1], primary_key), str(kv[0])))[:5]
        node["resources"] = [
            {"compute_kind": node["kind"], "compute_id": rid, "metrics": _round_metrics(m, discount_pct, area)}
            for rid, m in res_list
        ]
    node.pop("_resources", None)

    node["metrics"] = _round_metrics(node["metrics"], discount_pct, area)
    node["untagged"] = _round_metrics(node["untagged"], discount_pct, area)
    node["attributed_here"] = _round_metrics(node["attributed_here"], discount_pct, area)
    node["share_of_parent"] = round(my_total / parent_total, 4) if parent_total else (1.0 if node["level"] == "account" else 0.0)
    node["share_of_total"] = round(my_total / grand_total, 4) if grand_total else 0.0
    return node


def _vtok(v) -> str:
    return tag_keys.UNTAGGED if v is None else str(v)


def _mark(node: dict, p, metric_keys, level: str) -> None:
    """Section 4.5: `untagged` bubbles to EVERY ancestor (root included) whenever the path's
    global effective value is `__untagged__` (attributed at account); `attributed_here` marks
    only the one node whose OWN level equals the path's attributed level."""
    if level == "account":
        node["untagged"] = _add(node["untagged"], p.metrics)
    if node["level"] == level:
        node["attributed_here"] = _add(node["attributed_here"], p.metrics)


def _build_tree(paths: list, area: str, metric_keys, primary_key: str, degraded: dict) -> dict:
    root = _new_node("account", "account", "account", None, "Account", None, None, metric_keys)

    for p in paths:
        eff_value, level, _source = _effective(p)
        root["metrics"] = _add(root["metrics"], p.metrics)
        _mark(root, p, metric_keys, level)

        ws_id = _node_id("ws", p.workspace_value)
        ws_node = root["_children"].setdefault(
            ws_id, _new_node(ws_id, "workspace", "workspace", p.workspace_value,
                              _workspace_label(p.workspace_value, p.workspace_reason),
                              p.workspace_source, p.workspace_reason, metric_keys)
        )
        ws_node["metrics"] = _add(ws_node["metrics"], p.metrics)
        _mark(ws_node, p, metric_keys, level)
        if p.workspace_id:
            ws_node["_workspace_ids"].setdefault(p.workspace_id, _zero(metric_keys))
            ws_node["_workspace_ids"][p.workspace_id] = _add(ws_node["_workspace_ids"][p.workspace_id], p.metrics)

        c_degraded = None
        if area == "performance":
            if p.compute_kind == "warehouse":
                c_degraded = degraded.get(("compute", "warehouses"))
            elif p.compute_kind == "cluster":
                c_degraded = degraded.get(("compute", "clusters"))
        policy_tag = "policy" if (p.compute_kind == "serverless" and p.compute_id is not None) else "nopolicy"
        c_id = _node_id(ws_id, "c", p.compute_kind, policy_tag, _vtok(p.compute_value))
        c_node = ws_node["_children"].setdefault(
            c_id, _new_node(c_id, "compute", p.compute_kind, p.compute_value,
                             _compute_label(p.compute_kind, p.compute_id, p.compute_value, c_degraded, area),
                             p.compute_source, None, metric_keys)
        )
        c_node["metrics"] = _add(c_node["metrics"], p.metrics)
        _mark(c_node, p, metric_keys, level)
        if p.compute_id:
            c_node["_resources"].setdefault(p.compute_id, _zero(metric_keys))
            c_node["_resources"][p.compute_id] = _add(c_node["_resources"][p.compute_id], p.metrics)

        w_degraded = None
        if p.work_kind == "job":
            w_degraded = degraded.get(("lakeflow", "jobs"))
        elif p.work_kind == "pipeline":
            w_degraded = degraded.get(("lakeflow", "pipelines"))
        w_id = _node_id(c_id, "w", p.work_kind, _vtok(p.work_value))
        w_node = c_node["_children"].setdefault(
            w_id, _new_node(w_id, "work", p.work_kind, p.work_value,
                             _work_label(p.work_kind, p.work_value, w_degraded, p.compute_kind),
                             p.work_source, None, metric_keys)
        )
        w_node["metrics"] = _add(w_node["metrics"], p.metrics)
        _mark(w_node, p, metric_keys, level)

    grand_total = _primary(root["metrics"], primary_key) or 0.0
    discount_pct = 0.0
    if area == "cost":
        try:
            discount_pct = float(app_config.load_settings().get("discount_pct", 0.0))
        except Exception:  # pragma: no cover
            discount_pct = 0.0
    return _finalize_node(root, area, discount_pct, grand_total, grand_total, primary_key)


def _build_by_value(paths: list, metric_keys, primary_key: str) -> list[dict]:
    agg: dict[str, dict] = {}
    for p in paths:
        eff_value, level, _source = _effective(p)
        entry = agg.setdefault(eff_value, {
            "value": eff_value, "metrics": _zero(metric_keys),
            "by_level": {"account": _zero(metric_keys), "workspace": _zero(metric_keys),
                         "compute": _zero(metric_keys), "work": _zero(metric_keys)},
            "sources": set(), "overrides_outer": _zero(metric_keys),
        })
        entry["metrics"] = _add(entry["metrics"], p.metrics)
        entry["by_level"][level] = _add(entry["by_level"][level], p.metrics)
        for src in (p.workspace_source, p.compute_source, p.work_source):
            if src:
                entry["sources"].add(src)
        if _overrides_outer(p, eff_value, level):
            entry["overrides_outer"] = _add(entry["overrides_outer"], p.metrics)

    grand_total = sum(_primary(e["metrics"], primary_key) for e in agg.values()) or 0.0
    out = []
    for value, entry in agg.items():
        label = "(no value)" if value == "" else ("untagged" if value == tag_keys.UNTAGGED else value)
        out.append({
            "value": tag_keys.model_value_to_api(value),
            "label": label,
            "metrics": entry["metrics"],
            "share_of_total": round(_primary(entry["metrics"], primary_key) / grand_total, 4) if grand_total else 0.0,
            "by_level": entry["by_level"],
            "sources": sorted(entry["sources"]),
            "overrides_outer": entry["overrides_outer"],
        })
    out.sort(key=lambda e: (e["value"] == tag_keys.UNTAGGED, -_primary(e["metrics"], primary_key), str(e["value"])))
    return out


def _paths_out(paths: list, area: str, discount_pct: float) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for p in paths:
        eff_value, level, source = _effective(p)
        key = (p.workspace_value, p.compute_kind, p.compute_value, p.compute_source,
               p.work_kind, p.work_value, p.work_source, eff_value, level, source)
        if key not in grouped:
            grouped[key] = dict(
                workspace_value=p.workspace_value, compute_kind=p.compute_kind,
                compute_value=p.compute_value, compute_source=p.compute_source,
                work_kind=p.work_kind, work_value=p.work_value, work_source=p.work_source,
                effective_value=eff_value, attributed_level=level, attributed_source=source,
                metrics=_zero(p.metrics.keys()),
            )
        grouped[key]["metrics"] = _add(grouped[key]["metrics"], p.metrics)
    out = []
    for row in grouped.values():
        row["metrics"] = _round_metrics(row["metrics"], discount_pct, area) if area == "cost" else row["metrics"]
        out.append(row)
    return out


def rollup_keys(area: str, window_days: int) -> dict:
    if area not in AREAS:
        raise RollupError(f"area must be one of {AREAS}, got {area!r}")
    if window_days not in WINDOW_CHOICES:
        raise RollupError(f"window must be one of {list(WINDOW_CHOICES)}, got {window_days}")
    con = _connect()
    try:
        if not _tags_models_exist(con):
            return {"area": area, "window_days": window_days, "keys": []}
        if area == "cost":
            rows = con.execute(
                """
                SELECT t.tag_key,
                       COALESCE(MAX(w.display_key), MIN(t.raw_key)) AS display_key,
                       SUM(CASE WHEN t.level = 'compute' THEN t.usd ELSE 0 END) AS compute_usd,
                       SUM(CASE WHEN t.level = 'work' THEN t.usd ELSE 0 END) AS work_usd
                FROM tags.cost_unit_tag t
                LEFT JOIN (
                    SELECT tag_key, MAX(display_key) AS display_key
                    FROM tags.tag_workspace
                    GROUP BY tag_key
                ) w ON w.tag_key = t.tag_key
                WHERE t.window_days = ?
                GROUP BY t.tag_key
                """,
                [window_days],
            ).fetchall()
            ws_rows = con.execute(
                "SELECT tag_key, COUNT(*) FROM tags.tag_workspace WHERE is_allocating GROUP BY tag_key"
            ).fetchall()
            ws_map = dict(ws_rows)
            keys = [
                {
                    "tag_key": tag_key, "display_key": display_key or tag_key,
                    "compute": {"usd": round(compute_usd or 0, 2)},
                    "work": {"usd": round(work_usd or 0, 2)},
                    "allocating_workspaces": int(ws_map.get(tag_key, 0)),
                }
                for tag_key, display_key, compute_usd, work_usd in rows
            ]
            keys.sort(key=lambda k: (-(k["compute"]["usd"] + k["work"]["usd"]), k["tag_key"]))
        else:
            rows = con.execute(
                """
                SELECT t.tag_key, MIN(t.raw_key) AS display_key,
                       SUM(CASE WHEN t.level = 'compute' THEN t.statements ELSE 0 END) AS compute_stmt,
                       SUM(CASE WHEN t.level = 'work' THEN t.statements ELSE 0 END) AS work_stmt
                FROM tags.perf_unit_tag t
                WHERE t.window_days = ?
                GROUP BY t.tag_key
                """,
                [window_days],
            ).fetchall()
            ws_rows = con.execute(
                "SELECT tag_key, COUNT(*) FROM tags.tag_workspace WHERE is_allocating GROUP BY tag_key"
            ).fetchall()
            ws_map = dict(ws_rows)
            keys = [
                {
                    "tag_key": tag_key, "display_key": display_key or tag_key,
                    "compute": {"statements": int(compute_stmt or 0)},
                    "work": {"statements": int(work_stmt or 0)},
                    "allocating_workspaces": int(ws_map.get(tag_key, 0)),
                }
                for tag_key, display_key, compute_stmt, work_stmt in rows
            ]
            keys.sort(key=lambda k: (-(k["compute"]["statements"] + k["work"]["statements"]), k["tag_key"]))
        return {"area": area, "window_days": window_days, "keys": keys}
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------
# by_tag_top -- the by-tag view: top VALUES for one key, kept separate per LEVEL,
# unlike by_value above which deliberately walks to ONE effective (innermost) value per unit for
# the nested tree. Reuses the same paths (_fetch_cost_paths/_fetch_perf_paths) and workspace scope
# (_resolve_workspace_scope) rollup() itself uses -- no new SQL, no new dbt model.
# ---------------------------------------------------------------------------------------------

_COMPUTE_TOP_LEVEL = {"warehouse": "warehouse", "cluster": "cluster"}  # else -> "billing line"
_WORK_TOP_LEVEL = {"query": "query", "job": "job", "pipeline": "pipeline"}  # idle/unsplit/none carry no real per-tag value


def _top_metrics_meta(area: str) -> list[dict]:
    if area == "cost":
        return [{"key": "usd", "label": "list price (effective)", "unit": "usd"}]
    return [
        {"key": "statements", "label": "statements", "unit": "count"},
        {"key": "failed_statements", "label": "failed statements", "unit": "count"},
        {"key": "duration_ms", "label": "total duration", "unit": "ms"},
        {"key": "queue_ms", "label": "queue time", "unit": "ms"},
        {"key": "spill_bytes", "label": "spill", "unit": "bytes"},
    ]


def by_tag_top(
    area: str,
    tag_key: str,
    window_days: int,
    top: int = 10,
    workspace_ids: list[str] | None = None,
    env: list[str] | None = None,
    attributes: dict[str, list[str]] | None = None,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> dict:
    """The top `top` tag VALUES for one key, ranked by $ (area="cost") or by total query time
    (area="performance" ranks by duration_ms -- tags.perf_unit_tag carries no dollars or DBUs; a
    DBU figure for this key is the same rollup under area="cost"). Rows are per LEVEL and never
    merged across levels: the same value tagged on a warehouse and on a job are two separate rows,
    each summing only that level's own rows -- so rows overlap by design (see `tagged`/
    `rows_overlap_across_levels` below). Always returns one "untagged" row and `more_count`, the
    (level, value) rows past `top` that are not returned."""
    norm_key = _validate(area, tag_key, window_days)
    primary_key = "usd" if area == "cost" else "duration_ms"
    metric_keys = COST_METRIC_KEYS if area == "cost" else PERF_METRIC_KEYS

    con = _connect()
    try:
        if not _tags_models_exist(con):
            return _by_tag_not_assessed(
                area, norm_key, window_days,
                "tag_models_not_built: the tag rollup was not built; rebuild to see it",
            )
        source_status, required_ok, na_reason = _source_status(area)
        if not required_ok:
            return _by_tag_not_assessed(area, norm_key, window_days, na_reason, source_status=source_status)

        resolved_ws = _resolve_workspace_scope(workspace_ids, env, attributes)
        unit_ids = _matching_unit_ids(con, area, window_days, tag_filter)
        paths = _fetch_cost_paths(con, window_days, norm_key, unit_ids) if area == "cost" \
            else _fetch_perf_paths(con, window_days, norm_key, unit_ids)
        excluded_account = _zero(metric_keys)
        workspace_filtered = resolved_ws is not None
        if resolved_ws is not None:
            # Account-level usage (no workspace at all) has no workspace to match a scope filter
            # against, so it is dropped rather than kept unfiltered (same rule rollup() follows) --
            # tallied here the same way, so the two views of the same scope agree on what they drop.
            kept = []
            for p in paths:
                if p.workspace_id is None:
                    excluded_account = _add(excluded_account, p.metrics)
                    continue
                if p.workspace_id in resolved_ws:
                    kept.append(p)
            paths = kept

        discount_pct = 0.0
        if area == "cost":
            try:
                discount_pct = float(app_config.load_settings().get("discount_pct", 0.0))
            except Exception:  # pragma: no cover
                discount_pct = 0.0

        groups: dict[tuple[str, str], dict] = {}
        untagged_metrics = _zero(metric_keys)
        total_metrics = _zero(metric_keys)

        def _bucket(level: str, value: str) -> dict:
            return groups.setdefault((level, value), {"level": level, "value": value, "metrics": _zero(metric_keys)})

        for p in paths:
            total_metrics = _add(total_metrics, p.metrics)
            eff_value, _eff_level, _eff_source = _effective(p)
            if eff_value == tag_keys.UNTAGGED:
                untagged_metrics = _add(untagged_metrics, p.metrics)
            if tag_keys.is_real_value(p.compute_value):
                b = _bucket(_COMPUTE_TOP_LEVEL.get(p.compute_kind, "billing line"), p.compute_value)
                b["metrics"] = _add(b["metrics"], p.metrics)
            work_level = _WORK_TOP_LEVEL.get(p.work_kind)
            if work_level and tag_keys.is_real_value(p.work_value):
                b = _bucket(work_level, p.work_value)
                b["metrics"] = _add(b["metrics"], p.metrics)
            if tag_keys.is_real_value(p.workspace_value):
                b = _bucket("workspace", p.workspace_value)
                b["metrics"] = _add(b["metrics"], p.metrics)

        ranked = [g for _, g in sorted(groups.items(), key=lambda kv: (-_primary(kv[1]["metrics"], primary_key), str(kv[0])))]
        top_rows = ranked[:top]
        tagged_metrics = {k: total_metrics[k] - untagged_metrics[k] for k in metric_keys}

        rows = [
            {
                "level": g["level"],
                "value": tag_keys.model_value_to_api(g["value"]),
                "label": "(no value)" if g["value"] == "" else g["value"],
                "metrics": _round_metrics(g["metrics"], discount_pct, area),
            }
            for g in top_rows
        ]

        return {
            "area": area,
            "tag_key": norm_key,
            "display_key": _display_key(con, norm_key),
            "window_days": window_days,
            "outcome": "ok_rows" if paths else ("ok_empty_filters" if (workspace_filtered or tag_filter is not None) else "ok_empty_window"),
            "not_assessed_reason": None,
            "metrics": _top_metrics_meta(area),
            "primary_metric": primary_key,
            "discount_pct": discount_pct,
            "levels": list(TOP_LEVELS),
            "rows": rows,
            "untagged": {
                "level": None, "value": tag_keys.UNTAGGED, "label": "untagged",
                "metrics": _round_metrics(untagged_metrics, discount_pct, area),
            },
            "group_count": len(ranked),
            "more_count": max(len(ranked) - len(top_rows), 0),
            "total": _round_metrics(total_metrics, discount_pct, area),
            # rows overlap by design (a level's dollars are counted again at every outer level), so
            # summing them or taking a share of `total` overstates tagged spend; `tagged` (total
            # minus untagged, from this same walk) is the one number that reconciles.
            "tagged": _round_metrics(tagged_metrics, discount_pct, area),
            "rows_overlap_across_levels": True,
            "scope": {
                "workspace_filtered": workspace_filtered,
                "excluded_account_level": _round_metrics(excluded_account, discount_pct, area) if area == "cost" else excluded_account,
            },
            "source_status": source_status,
        }
    finally:
        con.close()


def _by_tag_not_assessed(area, norm_key, window_days, reason, source_status=None):
    return {
        "area": area, "tag_key": norm_key, "display_key": norm_key, "window_days": window_days,
        "outcome": "not_assessed", "not_assessed_reason": reason,
        "metrics": _top_metrics_meta(area), "primary_metric": "usd" if area == "cost" else "duration_ms",
        "discount_pct": 0.0, "levels": list(TOP_LEVELS),
        "rows": [], "untagged": None, "group_count": 0, "more_count": 0, "total": None,
        "tagged": None, "rows_overlap_across_levels": True,
        "scope": {"workspace_filtered": False, "excluded_account_level": None},
        "source_status": source_status or [],
    }
