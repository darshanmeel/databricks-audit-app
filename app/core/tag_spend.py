"""Spend checks under a tag filter, answered from tags.cost_day instead of their own table.

A billed dollar belongs to a tag value when its most specific tag carries it: the query's or the
job's/pipeline's own tag, else the tag on the bill, else the workspace's tag on that day
(tags.tag_workspace_history), the same order app/core/rollup.py uses. Each check with a file in app/queries/tag_spend/ is
re-computed there over the matching dollars, so totals, changes and statuses follow the filter.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.core import config as app_config
from app.core import registry
from app.core import tag_keys
from app.core import tags as app_core_tags

VARIANT_DIR = Path(__file__).resolve().parent.parent / "queries" / "tag_spend"
CHAIN_WORDS = ["query", "job", "pipeline", "compute", "workspace"]
CHAIN_LABEL = "the most specific tag on each billed dollar: query, job or pipeline, compute, then workspace"

_SIG_ID = re.compile(r"^[0-9a-f]{32}$")
_WS_ID = re.compile(r"^[0-9A-Za-z_.:-]+$")
_PARAM = re.compile(r"__P__([a-z0-9_]+)__")
_NO_WORKSPACE = "~"

# Where a dollar's winning tag was set, from cost_day's per-tag source; order is most specific first.
ORIGINS = {
    "query": "query", "job": "job", "pipeline": "pipeline",
    "warehouse": "warehouse", "cluster": "cluster", "budget_policy": "budget policy",
    "endpoint": "serving endpoint", "app": "app", "serverless": "serverless usage",
    "bill": "other usage", "workspace": "workspace", "untagged": "no tag at any level",
}
_SOURCE_ORIGIN = {
    "attributed_query_tags": "query", "job_tags": "job", "pipeline_tags": "pipeline",
    "billing:warehouse": "warehouse", "billing:cluster": "cluster", "billing:budget_policy": "budget_policy",
    "billing:endpoint": "endpoint", "billing:app": "app", "billing:serverless": "serverless",
}


def has_variant(query_id: str) -> bool:
    return (VARIANT_DIR / f"{query_id}.sql").is_file()


def available(con) -> bool:
    n = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'tags' AND table_name IN ('cost_day', 'tag_workspace_history')"
    ).fetchone()[0]
    return n == 2


def _filter_groups(tag_filter) -> list:
    filter_set = (
        tag_filter if isinstance(tag_filter, app_core_tags.TagFilterSet)
        else app_core_tags.TagFilterSet.single(tag_filter)
    )
    return list(filter_set.groups)


def _groups(tag_filter) -> list[tuple[str, set[str] | None]]:
    return [
        (tag_keys.normalize_tag_key(g.key), set(g.values) if g.values else None)
        for g in _filter_groups(tag_filter)
    ]


_OPEN = (None, None, {})


def _workspace_periods(con, groups) -> dict[str, list[tuple]]:
    """Per workspace, (from, to, {key: value}) periods over which its tags for the filtered keys
    hold -- from the first day of each version to the next one, None for open ends; the days
    before a tag first showed carry none."""
    keys = sorted({k for k, _ in groups})
    versions: dict[str, list[tuple]] = {}
    for ws, key, value, start, end in con.execute(
        "SELECT workspace_id, tag_key, tag_value, valid_from, valid_to FROM tags.tag_workspace_history "
        f"WHERE is_allocating AND tag_key IN ({', '.join('?' * len(keys))})", keys,
    ).fetchall():
        versions.setdefault(ws, []).append((key, value, start, end))
    periods: dict[str, list[tuple]] = {}
    for ws, rows in versions.items():
        cuts = sorted({d for _, _, start, end in rows for d in (start, end) if d is not None})
        bounds = [None, *cuts, None]
        periods[ws] = [
            (lo, hi, {key: value for key, value, start, end in rows
                      if (start is None or (lo is not None and start <= lo))
                      and (end is None or (hi is not None and hi <= end))})
            for lo, hi in zip(bounds, bounds[1:])
        ]
    return periods


def _resolve(sig: str, inherited: dict[str, str], groups) -> list[str] | None:
    """The origin of each group's value when every group matches, else None -- several keys
    ANDed, one key's values ORed, no values meaning any real value, __untagged__ matching a dollar
    with no value for that key at any level."""
    own = {e["k"]: e for e in json.loads(sig)}
    origins = []
    for key, wanted in groups:
        entry = own.get(key)
        if entry and entry["v"]:
            value, origin = entry["v"], _SOURCE_ORIGIN.get(entry["s"], "bill")
        elif inherited.get(key):
            value, origin = inherited[key], "workspace"
        else:
            value, origin = tag_keys.UNTAGGED, "untagged"
        if not (value != tag_keys.UNTAGGED if wanted is None else value in wanted):
            return None
        origins.append(origin)
    return origins


def matching_cells(con, tag_filter) -> list[tuple]:
    """(workspace_id, sig_id, from, to) cells of tags.cost_day whose effective value matches
    `tag_filter` on the days from..to (exclusive; None is open)."""
    groups = _groups(tag_filter)
    periods = _workspace_periods(con, groups)
    cells = []
    for ws, sig_id, sig in con.execute("SELECT DISTINCT workspace_id, sig_id, sig FROM tags.cost_day").fetchall():
        spans = periods.get(ws, [_OPEN])
        hits = [(lo, hi) for lo, hi, held in spans if _resolve(sig, held, groups) is not None]
        if len(hits) == len(spans):
            hits = [(None, None)]
        ws_key = ws if ws is not None else _NO_WORKSPACE
        cells += [(ws_key, sig_id, lo, hi) for lo, hi in hits]
    return cells


def origins(con, tag_filter, window_days: int, workspace_scope: set[str] | None = None) -> dict | None:
    """The dollars `tag_filter` keeps in the window, split per filtered key by where their value
    was set (ORIGINS); None without a filter or without tags.cost_day."""
    if tag_filter is None or not available(con):
        return None
    groups = _groups(tag_filter)
    periods = _workspace_periods(con, groups)
    spans = [(ws, i, lo, hi) for ws, rows in periods.items() for i, (lo, hi, _) in enumerate(rows)]
    period_join = ""
    if spans:
        values = ", ".join(
            f"('{ws}', {i}, {_date_sql(lo)}, {_date_sql(hi)})" for ws, i, lo, hi in spans if _WS_ID.match(ws)
        )
        period_join = (
            f" LEFT JOIN (VALUES {values}) p(ws, i, lo, hi) ON p.ws = c.workspace_id"
            " AND (p.lo IS NULL OR c.usage_date >= p.lo) AND (p.hi IS NULL OR c.usage_date < p.hi)"
        )
    total = 0.0
    split = [dict.fromkeys(ORIGINS, 0.0) for _ in groups]
    for ws, i, sig, usd in con.execute(
        f"SELECT c.workspace_id, {'p.i' if spans else 'NULL'}, c.sig, COALESCE(SUM(c.usd), 0) FROM tags.cost_day c"
        f"{period_join}, (SELECT max(as_of_date) AS d FROM tags.cost_day) a "
        "WHERE c.usage_date >= a.d - CAST(? AS INTEGER) AND c.usage_date < a.d "
        "GROUP BY ALL",
        [int(window_days)],
    ).fetchall():
        if workspace_scope is not None and ws not in workspace_scope:
            continue
        held = periods[ws][i][2] if i is not None and ws in periods else {}
        found = _resolve(sig, held, groups)
        if found is None:
            continue
        total += usd
        for i, origin in enumerate(found):
            split[i][origin] += usd
    return {
        "window_days": int(window_days),
        "usd": round(total, 2),
        "keys": [
            {
                "tag_key": g.key,
                "values": [tag_keys.model_value_to_api(v) for v in g.values],
                "origins": [
                    {"origin": o, "label": ORIGINS[o], "usd": round(v, 2)}
                    for o, v in by_origin.items() if round(v, 2) > 0
                ],
            }
            for g, by_origin in zip(_filter_groups(tag_filter), split)
        ],
    }


def _date_sql(d) -> str:
    return "CAST(NULL AS DATE)" if d is None else f"DATE '{d.isoformat()}'"


def _param(query_id: str, name: str) -> str:
    """The check's own threshold: thresholds.yml's per-check value, then its `_all` value, then
    the header default -- the order dbt/macros/param.sql uses."""
    thresholds = app_config.load_thresholds()
    values = [scope[name] for scope in (thresholds.get(query_id) or {}, thresholds.get("_all") or {}) if name in scope]
    values += [p["default"] for p in registry.by_id(query_id).params if p["name"] == name and p.get("default") is not None]
    if not values:
        raise ValueError(f"{query_id}: no value for :{name}")
    v = float(values[0])
    return str(int(v)) if v.is_integer() else repr(v)


def table_sql(con, query_id: str, window_days: int, tag_filter) -> str | None:
    """`(<the check re-computed over the matching dollars>) t` for a FROM clause, or None when
    this check has no tag-spend variant, no tag filter is set, or tags.cost_day is not built."""
    if tag_filter is None or not has_variant(query_id) or not available(con):
        return None
    cells = [
        (ws, sig, lo, hi) for ws, sig, lo, hi in matching_cells(con, tag_filter)
        if _SIG_ID.match(sig) and _WS_ID.match(ws)
    ]
    if cells:
        values = ", ".join(f"('{ws}', '{sig}', {_date_sql(lo)}, {_date_sql(hi)})" for ws, sig, lo, hi in cells)
        tagged = (
            "SELECT c.* FROM tags.cost_day c JOIN (VALUES " + values + ") v(ws, sig, lo, hi) "
            f"ON COALESCE(c.workspace_id, '{_NO_WORKSPACE}') = v.ws AND c.sig_id = v.sig "
            "AND (v.lo IS NULL OR c.usage_date >= v.lo) AND (v.hi IS NULL OR c.usage_date < v.hi)"
        )
    else:
        tagged = "SELECT * FROM tags.cost_day WHERE FALSE"
    return variant_sql(query_id, window_days, tagged)


def variant_sql(query_id: str, window_days: int, tagged_sql: str) -> str:
    """The check's variant over the tags.cost_day rows `tagged_sql` selects, as `(...) t`."""
    body = (VARIANT_DIR / f"{query_id}.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in body.splitlines() if not line.startswith("--"))
    body = body.replace("__TAGGED__", tagged_sql).replace("__W__", str(int(window_days)))
    body = _PARAM.sub(lambda m: _param(query_id, m.group(1)), body)
    return f"({body}) t"
