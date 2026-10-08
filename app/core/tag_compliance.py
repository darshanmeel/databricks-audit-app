"""Mandatory tags (settings `mandatory_tag_keys`): how many queries, jobs, pipelines, serverless
notebooks, clusters, warehouses and workspaces miss each one, and at which level a tag is found.

A tag is looked for level by level, the most specific first:
  queries     own query tag -> the job or pipeline that ran it -> its compute (the warehouse's or
              cluster's own tag or bill; on serverless, the notebook's, job's or pipeline's bill,
              where the usage policy's tags land) -> its workspace's tag
  jobs, pipelines  own tag -> the tags on its bill -> its workspace's tag
  notebooks   serverless notebooks, which carry no tags: the bill (its usage policy) -> its workspace's tag
  clusters    own tag (or its pool's) -> the tags on its bill -> its workspace's tag
  warehouses  own tag -> the tags on its bill (usage policies included) -> its workspace's tag
  workspaces  its own tag (on Azure the workspace resource's tag), read from its bill (tags.tag_workspace)
A value on a bill equal to the workspace's own value is the workspace's tag, which Azure copies onto
every bill row: it counts at the workspace level, not the compute's. Missing at every level is truly
missing. Queries are counted by statement over the window, with where they came from (SQL editor,
dashboard, job, ...); the others are objects as of the export (serverless notebooks: those that ran
queries in the window). An object tagged one way and billed another is listed as a conflict, the
bill's value being what the cost carries.
"""
from __future__ import annotations

import re

import duckdb

# Cluster sources that are jobs, pipelines or Databricks-run compute; the rest are all-purpose.
_NOT_ALL_PURPOSE = ("JOB", "PIPELINE", "PIPELINE_MAINTENANCE", "SQL", "MODELS")
_OFFENDERS_MAX = 500
_SERVERLESS_NAME = "Serverless (no warehouse or cluster)"


def norm_key(raw: str) -> str:
    """The same normalisation the tag tables use: lower case, no spaces, _ or -."""
    return re.sub(r"[ _-]+", "", str(raw).lower())


# Keys that mean the same tag: a resource tagged env has an environment tag, and the reverse.
_SAME_TAG = [("environment", "env")]


def key_variants(keys: list[str]) -> dict[str, str]:
    """Normalised tag key -> the mandatory key it counts as: the key itself, or a same-meaning key
    that is not mandatory in its own right."""
    out = {k: k for k in keys}
    for group in _SAME_TAG:
        for k in keys:
            if k in group:
                for other in group:
                    out.setdefault(other, k)
    return out


def _has_delete_time(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """Older exports carry no delete_time: every row counts, and the page says so."""
    return bool(con.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'dims' AND table_name = ? "
        "AND column_name = 'delete_time'", [table],
    ).fetchone())


def _has_column(con: duckdb.DuckDBPyConnection, schema: str, table: str, column: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = ? AND table_name = ? AND column_name = ?",
        [schema, table, column],
    ).fetchone())


def _has_table(con: duckdb.DuckDBPyConnection, schema: str, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?", [schema, name]
    ).fetchone())


def _in_scope(ws, scope: set[str] | None) -> bool:
    return scope is None or str(ws) in scope


def _entity_keys(con, entity_type: str, variants: dict[str, str], present_sql: str) -> dict[tuple[str, str], set[str]]:
    """(workspace_id, entity_id) -> the mandatory keys tags.tag_entity holds for that object."""
    out: dict[tuple[str, str], set[str]] = {}
    rows = con.execute(
        f"SELECT workspace_id, entity_id, tag_key FROM tags.tag_entity "
        f"WHERE entity_type = ? AND tag_key IN ({', '.join('?' * len(variants))}) AND {present_sql}",
        [entity_type, *variants],
    ).fetchall()
    for ws, eid, key in rows:
        out.setdefault((str(ws), str(eid)), set()).add(variants[key])
    return out


def _entity_tags(con: duckdb.DuckDBPyConnection, entity_type: str) -> dict[tuple[str, str], dict[str, str]]:
    """(workspace_id, entity_id) -> every tag the object carries itself, as {key: value}."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for ws, eid, key, value in con.execute(
        "SELECT workspace_id, entity_id, coalesce(raw_key, tag_key), tag_value FROM tags.tag_entity "
        "WHERE entity_type = ? AND tag_value NOT IN ('__untagged__', '__mixed__') ORDER BY 3, 1, 2, 4",
        [entity_type],
    ).fetchall():
        out.setdefault((str(ws), str(eid)), {})[key] = value
    return out


def _bill(con, entity_type: str, variants: dict[str, str], ws_val: dict) -> tuple[dict, dict, dict]:
    """Tags on an object's bill: (workspace_id, id) -> the mandatory keys on it today; -> {key:
    {value, others, since, until, now, workspace, policy}} for the page; and -> its last billed day when that
    is over a week before the data's last day. A key is on the bill today when its last day is the
    bill's last day; an export without tags.bill_tag_dates falls back to the dominant billed value.
    A value equal to the workspace's own (`ws_val`) is the workspace's tag: shown, not counted, unless
    a usage policy put it there on the key's last billed day."""
    keys: dict[tuple[str, str], set[str]] = {}
    detail: dict[tuple[str, str], dict[str, dict]] = {}
    stale: dict[tuple[str, str], str] = {}

    def from_ws(k: tuple[str, str], tkey: str, value) -> bool:
        mk = variants.get(tkey)
        return mk is not None and value is not None and ws_val.get((k[0], mk)) == str(value).strip()

    if _has_table(con, "tags", "bill_tag_dates"):
        data_end = con.execute("SELECT max(entity_last_date) FROM tags.bill_tag_dates").fetchone()[0]
        for ws, eid, tkey, raw, value, values, first, last, ent_last, policy in con.execute(
            "SELECT workspace_id, entity_id, tag_key, raw_key, last_value, all_values, first_date, last_date, "
            "entity_last_date, from_policy FROM tags.bill_tag_dates WHERE entity_type = ? ORDER BY raw_key", [entity_type],
        ).fetchall():
            k = (str(ws), str(eid))
            now = last is not None and ent_last is not None and last >= ent_last
            ws_tag = not policy and from_ws(k, tkey, value)
            detail.setdefault(k, {})[raw or tkey] = {
                "value": value, "others": [v for v in (values or []) if v != value],
                "since": str(first) if first else None,
                "until": None if now else (str(last) if last else None), "now": now, "workspace": ws_tag,
                "policy": bool(policy)}
            if now and tkey in variants and not ws_tag:
                keys.setdefault(k, set()).add(variants[tkey])
            # An idle object keeps its last bill's tags; say how old that bill is.
            if ent_last is not None and data_end is not None and (data_end - ent_last).days > _STALE_BILL_DAYS:
                stale[k] = str(ent_last)
        return keys, detail, stale
    for ws, eid, tkey, raw, value in con.execute(
        "SELECT workspace_id, entity_id, tag_key, coalesce(raw_key, tag_key), tag_value FROM tags.tag_entity "
        "WHERE entity_type = ? AND tag_value <> '__untagged__' ORDER BY 4", [entity_type],
    ).fetchall():
        k = (str(ws), str(eid))
        ws_tag = value != "__mixed__" and from_ws(k, tkey, value)
        detail.setdefault(k, {})[raw] = {"value": None if value == "__mixed__" else value, "others": [],
                                         "since": None, "until": None, "now": True, "workspace": ws_tag}
        if tkey in variants and not ws_tag:
            keys.setdefault(k, set()).add(variants[tkey])
    return keys, detail, stale


# A bill older than this, before the data's last day, is flagged with its date.
_STALE_BILL_DAYS = 7

# The query tag this app puts on its own queries (tools/snapshot.py AUDIT_TAG_KEY), normalised.
_APP_QUERY_KEY = "auditapp"


class _Tally:
    """Counts one object type: per level, how much misses any, all, or each key; where each key
    is first found; and the truly-missing objects."""

    def __init__(self, keys: list[str], levels: list[str]):
        self.keys, self.levels = keys, levels
        self.total = 0
        self.missing = {lv: {"any": 0, "all": 0, "by_key": {k: 0 for k in keys}} for lv in levels}
        self.found = {k: {**{lv: 0 for lv in levels}, "missing": 0} for k in keys}
        self.by_env: dict[str, dict] = {}
        self.offenders: list[dict] = []

    def add(self, weight: int, level_sets: list[set[str]], env: str) -> tuple[set[str], dict[str, str]]:
        """Adds one object (or `weight` statements) in workspace environment `env`; returns the keys
        missing at every level and the level each key was first found at ("missing" when none)."""
        self.total += weight
        have: set[str] = set()
        for lv, own in zip(self.levels, level_sets):
            have |= own
            gone = [k for k in self.keys if k not in have]
            m = self.missing[lv]
            if gone:
                m["any"] += weight
            if len(gone) == len(self.keys):
                m["all"] += weight
            for k in gone:
                m["by_key"][k] += weight
        source = {}
        by_level = dict(zip(self.levels, level_sets))
        for k in self.keys:
            source[k] = next((lv for lv in self.levels if k in by_level[lv]), "missing")  # first level wins
            self.found[k][source[k]] += weight
        gone = {k for k in self.keys if k not in have}
        e = self.by_env.setdefault(env, {"total": 0, "any": 0, "by_key": {k: 0 for k in self.keys}})
        e["total"] += weight
        e["any"] += weight if gone else 0
        for k in gone:
            e["by_key"][k] += weight
        return gone, source

    deleted: int | None = None  # deleted objects left out; None = the export doesn't record deletions
    conflicts: list[dict] | None = None  # own tag and bill disagree; None = no bill level

    def result(self, kind: str, noun: str) -> dict:
        self.offenders.sort(key=lambda o: (-o.get("count", 1), -len(o["missing"]), str(o["name"] or o["id"]).lower(),
                                           str(o["id"]), str(o.get("workspace_id") or "")))
        return {
            "kind": kind, "noun": noun, "outcome": "ok", "total": self.total, "levels": self.levels,
            "missing": self.missing, "found": self.found, "by_env": self.by_env,
            "offenders": self.offenders[:_OFFENDERS_MAX], "offenders_total": len(self.offenders),
            "deleted": self.deleted,
            **({"conflicts": self.conflicts[:_OFFENDERS_MAX], "conflicts_total": len(self.conflicts)}
               if self.conflicts is not None else {}),
        }


def _not_assessed(kind: str, noun: str, reason: str) -> dict:
    return {"kind": kind, "noun": noun, "outcome": "not_assessed", "reason": reason}


def compliance(con: duckdb.DuckDBPyConnection, raw_keys: list[str], window_days: int,
               scope: set[str] | None) -> dict:
    keys = list(dict.fromkeys(norm_key(k) for k in raw_keys))
    labels = {norm_key(k): k for k in raw_keys}
    variants = key_variants(keys)
    no_tags = "Not in this export: it has no tag tables. Re-run the export."
    if not _has_table(con, "tags", "tag_entity") or not _has_table(con, "tags", "tag_workspace"):
        return {"keys": keys, "labels": labels, "window_days": window_days,
                "types": [_not_assessed(k, k, no_tags) for k in _KINDS]}

    ws_keys: dict[str, set[str]] = {}
    ws_val: dict[tuple[str, str], str] = {}
    ws_mixed: dict[str, dict[str, list[str]]] = {}
    for ws, key, value, valid, top_values in con.execute(
        f"SELECT workspace_id, tag_key, tag_value, is_allocating, top_values FROM tags.tag_workspace "
        f"WHERE tag_key IN ({', '.join('?' * len(variants))})", list(variants),
    ).fetchall():
        if valid:
            ws_keys.setdefault(str(ws), set()).add(variants[key])
            ws_val[(str(ws), variants[key])] = str(value).strip()
        elif value == "__mixed__":
            ws_mixed.setdefault(str(ws), {})[variants[key]] = list(top_values or [])
    ws_of = lambda ws: ws_keys.get(str(ws), set())  # noqa: E731
    envs: dict[str, str] = {}
    if _has_column(con, "dims", "dim_workspace", "env"):
        envs = {str(ws): env for ws, env in con.execute(
            "SELECT workspace_id, max(env) FROM dims.dim_workspace GROUP BY 1").fetchall() if env}
    env_of = lambda ws: envs.get(str(ws), "unknown")  # noqa: E731

    ctx = (con, keys, variants, scope, ws_of, env_of, ws_val)
    types = [
        _queries(*ctx, window_days),
        _jobs(*ctx),
        _pipelines(*ctx),
        _notebooks(*ctx, window_days),
        _clusters(*ctx),
        _warehouses(*ctx),
        _workspaces(con, keys, scope, ws_of, ws_mixed, env_of),
    ]
    return {"keys": keys, "labels": labels, "window_days": window_days, "types": types}


_KINDS = ("queries", "jobs", "pipelines", "notebooks", "clusters", "warehouses", "workspaces")


def _queries(con, keys, variants, scope, ws_of, env_of, ws_val, window_days) -> dict:
    if not _has_table(con, "tags", "query_tag_keys") or not _has_table(con, "tags", "perf_unit"):
        return _not_assessed("queries", "queries", "Not in this export: re-run the export to count query tags.")
    has_work = _has_column(con, "tags", "query_tag_keys", "origin")
    # The performance rollup's compute tags may be the bill's (tag_bill), so the workspace's value is left out.
    perf_keys: dict[str, set[str]] = {}
    if _has_table(con, "tags", "perf_unit_tag"):
        for unit, ws, key, value in con.execute(
            f"SELECT t.unit_id, p.workspace_id, t.tag_key, t.tag_value FROM tags.perf_unit_tag t "
            f"JOIN tags.perf_unit p USING (window_days, unit_id) WHERE t.window_days = ? AND t.level = 'compute' "
            f"AND t.tag_key IN ({', '.join('?' * len(variants))})", [window_days, *variants],
        ).fetchall():
            if ws_val.get((str(ws), variants[key])) != str(value).strip():
                perf_keys.setdefault(unit, set()).add(variants[key])
    own = {kind: _entity_keys(con, kind, variants, "is_allocating") for kind in ("warehouse", "cluster", "job", "pipeline")}
    wh_names = dict(con.execute("SELECT warehouse_id, max(warehouse_name) FROM dims.dim_warehouse GROUP BY 1").fetchall()) \
        if _has_table(con, "dims", "dim_warehouse") else {}
    cl_names = dict(con.execute("SELECT cluster_id, max(cluster_name) FROM dims.dim_cluster GROUP BY 1").fetchall()) \
        if _has_table(con, "dims", "dim_cluster") else {}
    bills = {kind: _bill(con, f"billed_{kind}", variants, ws_val)
             for kind in ("warehouse", "cluster", "job", "pipeline", "notebook")}
    work_cols = "q.origin, q.job_id, q.pipeline_id, q.notebook_id" if has_work else "NULL, NULL, NULL, NULL"
    rows = con.execute(
        f"SELECT q.unit_id, p.workspace_id, p.compute_kind, p.compute_id, q.own_keys, q.statements, {work_cols} "
        "FROM tags.query_tag_keys q JOIN tags.perf_unit p USING (window_days, unit_id) "
        "WHERE q.window_days = ?", [window_days],
    ).fetchall()
    tally = _Tally(keys, ["own", "job", "compute", "workspace"] if has_work else ["own", "compute", "workspace"])
    origins: dict[str, dict] = {}
    by_unit: dict[str, dict] = {}
    for unit, ws, kind, cid, own_keys, n, origin, job_id, pipeline_id, notebook_id in rows:
        if not _in_scope(ws, scope):
            continue
        n = int(n)
        mine = {k for k in (own_keys or "").split(",") if k}
        if _APP_QUERY_KEY in mine:
            continue  # this app's own export queries, not the account's work
        own_mandatory = {variants[k] for k in mine if k in variants}
        w = str(ws)
        work: set[str] = set()
        compute = set(perf_keys.get(unit, set()))
        for kind_of, oid in (("job", job_id), ("pipeline", pipeline_id)):
            if oid is not None:
                work |= own[kind_of].get((w, str(oid)), set())
                compute |= bills[kind_of][0].get((w, str(oid)), set())
        if kind in ("warehouse", "cluster"):
            compute |= own[kind].get((w, str(cid)), set()) | bills[kind][0].get((w, str(cid)), set())
        elif notebook_id is not None:
            compute |= bills["notebook"][0].get((w, str(notebook_id)), set())
        sets = [own_mandatory] + ([work] if has_work else []) + [compute, ws_of(ws)]
        gone, source = tally.add(n, sets, env_of(ws))
        origin = origin or "unknown"
        og = origins.setdefault(origin, {"total": 0, "no_own": {k: 0 for k in keys}, "missing": {k: 0 for k in keys}})
        og["total"] += n
        for k in keys:
            if k not in own_mandatory:
                og["no_own"][k] += n
            if k in gone:
                og["missing"][k] += n
        if gone:
            bill_keys, bill_detail, bill_stale = bills.get(kind, ({}, {}, {}))
            u = by_unit.setdefault(unit, {
                "name": _SERVERLESS_NAME if kind == "serverless"
                else (wh_names.get(cid) if kind == "warehouse" else cl_names.get(cid)),
                "kind": kind, "id": cid, "workspace_id": ws, "count": 0, "by_key": {k: 0 for k in keys},
                "source": {k: {} for k in keys}, "query_keys": {}, "origins": {},
                "bill": bill_detail.get((w, str(cid)), {}),
                "last_billed": bill_stale.get((w, str(cid))),
            })
            u["count"] += n
            u["origins"][origin] = u["origins"].get(origin, 0) + n
            for k in gone:
                u["by_key"][k] += n
            for k, lv in source.items():
                u["source"][k][lv] = u["source"][k].get(lv, 0) + n
            for k in sorted(mine):
                u["query_keys"][k] = u["query_keys"].get(k, 0) + n
    for u in by_unit.values():
        u["missing"] = [k for k in keys if u["by_key"][k]]
        tally.offenders.append(u)
    result = tally.result("queries", "queries")
    result["origins"] = origins if has_work else None
    return result


def _objects(con, keys, variants, scope, ws_of, env_of, ws_val, *, kind, sql, own_type, compute_type, table=None) -> dict:
    """`sql` selects (workspace_id, id, name, is_deleted) for each object; `own_type` None for an
    object that carries no tags of its own (a notebook)."""
    own = _entity_keys(con, own_type, variants, "is_allocating") if own_type else {}
    own_tags = _entity_tags(con, own_type) if own_type else {}
    billed, bill_detail, bill_stale = _bill(con, compute_type, variants, ws_val)
    tally = _Tally(keys, (["own"] if own_type else []) + ["compute", "workspace"])
    tally.conflicts = [] if own_type else None
    deletes_known = table is not None and _has_delete_time(con, table)
    deleted = 0
    for ws, oid, name, is_deleted in con.execute(sql.format(deleted="delete_time IS NOT NULL" if deletes_known else "FALSE")).fetchall():
        if not _in_scope(ws, scope):
            continue
        if is_deleted:
            deleted += 1
            continue
        k = (str(ws), str(oid))
        sets = ([own.get(k, set())] if own_type else []) + [billed.get(k, set()), ws_of(ws)]
        gone, source = tally.add(1, sets, env_of(ws))
        if own_type:
            tally.conflicts.extend(_conflicts(name, oid, ws, own_tags.get(k, {}), bill_detail.get(k, {}), variants))
        if gone:
            tally.offenders.append({"name": name, "id": oid, "workspace_id": ws,
                                    "missing": [x for x in keys if x in gone],
                                    "source": {x: {lv: 1} for x, lv in source.items()},
                                    "tags": own_tags.get(k, {}),
                                    "bill": bill_detail.get(k, {}), "last_billed": bill_stale.get(k)})
    tally.deleted = deleted if deletes_known else None
    return tally.result(kind, kind)


def _conflicts(name, oid, ws, own_tags: dict, bill: dict, variants: dict[str, str]) -> list[dict]:
    """Mandatory keys whose own value differs from the value on the bill today."""
    own = {variants[norm_key(k)]: v for k, v in own_tags.items() if norm_key(k) in variants}
    out = []
    for raw, b in bill.items():
        key = variants.get(norm_key(raw))
        if key and b.get("now") and b.get("value") is not None and key in own and str(own[key]).strip() != str(b["value"]).strip():
            out.append({"name": name, "id": oid, "workspace_id": ws, "key": key, "own": own[key], "bill": b["value"]})
    return out


def _jobs(con, keys, variants, scope, ws_of, env_of, ws_val) -> dict:
    if not _has_table(con, "dims", "dim_job"):
        return _not_assessed("jobs", "jobs", "Not in this export: job names were not exported.")
    return _objects(con, keys, variants, scope, ws_of, env_of, ws_val, kind="jobs", table="dim_job", own_type="job",
                    compute_type="billed_job",
                    sql="SELECT workspace_id, job_id, max(name), bool_or({deleted}) FROM dims.dim_job GROUP BY 1, 2")


def _pipelines(con, keys, variants, scope, ws_of, env_of, ws_val) -> dict:
    if not _has_table(con, "dims", "dim_pipeline"):
        return _not_assessed("pipelines", "pipelines", "Not in this export: pipeline names were not exported.")
    return _objects(con, keys, variants, scope, ws_of, env_of, ws_val, kind="pipelines", own_type="pipeline",
                    compute_type="billed_pipeline",
                    sql="SELECT workspace_id, pipeline_id, max(pipeline_name), FALSE FROM dims.dim_pipeline GROUP BY 1, 2")


def _notebooks(con, keys, variants, scope, ws_of, env_of, ws_val, window_days) -> dict:
    if not _has_column(con, "tags", "query_tag_keys", "notebook_id") or not _has_table(con, "tags", "perf_unit"):
        return _not_assessed("notebooks", "notebooks", "Not in this export: re-run the export to count serverless notebooks.")
    if _has_table(con, "dims", "dim_notebook"):
        name, join = ("max(n.notebook_path)", "LEFT JOIN dims.dim_notebook n "
                      "ON n.workspace_id = p.workspace_id AND n.notebook_id = q.notebook_id ")
    else:
        name, join = "NULL", ""
    return _objects(con, keys, variants, scope, ws_of, env_of, ws_val, kind="notebooks", own_type=None,
                    compute_type="billed_notebook",
                    sql=f"SELECT p.workspace_id, q.notebook_id, {name}, FALSE FROM tags.query_tag_keys q "
                        f"JOIN tags.perf_unit p USING (window_days, unit_id) {join}"
                        f"WHERE q.window_days = {int(window_days)} AND q.notebook_id IS NOT NULL GROUP BY 1, 2")


def _clusters(con, keys, variants, scope, ws_of, env_of, ws_val) -> dict:
    if not _has_table(con, "dims", "dim_cluster"):
        return _not_assessed("clusters", "clusters", "Not in this export: cluster names were not exported.")
    excluded = ", ".join(f"'{s}'" for s in _NOT_ALL_PURPOSE)
    return _objects(con, keys, variants, scope, ws_of, env_of, ws_val, kind="clusters", table="dim_cluster", own_type="cluster",
                    compute_type="billed_cluster",
                    sql="SELECT workspace_id, cluster_id, max(cluster_name), bool_or({deleted}) FROM dims.dim_cluster "
                        f"WHERE cluster_source IS NULL OR cluster_source NOT IN ({excluded}) GROUP BY 1, 2")


def _warehouses(con, keys, variants, scope, ws_of, env_of, ws_val) -> dict:
    if not _has_table(con, "dims", "dim_warehouse"):
        return _not_assessed("warehouses", "warehouses", "Not in this export: warehouse names were not exported.")
    return _objects(con, keys, variants, scope, ws_of, env_of, ws_val, kind="warehouses", table="dim_warehouse", own_type="warehouse",
                    compute_type="billed_warehouse",
                    sql="SELECT workspace_id, warehouse_id, max(warehouse_name), bool_or({deleted}) FROM dims.dim_warehouse GROUP BY 1, 2")


def _workspaces(con, keys, scope, ws_of, ws_mixed, env_of) -> dict:
    if not _has_table(con, "dims", "dim_workspace"):
        return _not_assessed("workspaces", "workspaces", "Not in this export: workspace names were not exported.")
    # Only workspaces with usage: a tag is read from the bill, so one with no usage can't carry any.
    tally = _Tally(keys, ["own"])
    ws_tags: dict[str, dict[str, str]] = {}
    for ws, key, value in con.execute(
        "SELECT workspace_id, display_key, tag_value FROM tags.tag_workspace WHERE is_allocating ORDER BY 2, 1, 3"
    ).fetchall():
        ws_tags.setdefault(str(ws), {})[key] = value
    for ws, name in con.execute(
        "SELECT workspace_id, max(name) FROM dims.dim_workspace WHERE billed_in_snapshot GROUP BY 1"
    ).fetchall():
        if not _in_scope(ws, scope):
            continue
        gone, source = tally.add(1, [ws_of(ws)], env_of(ws))
        if gone:
            tally.offenders.append({"name": name, "id": ws, "workspace_id": ws,
                                    "missing": [x for x in keys if x in gone],
                                    "source": {x: {lv: 1} for x, lv in source.items()},
                                    "tags": ws_tags.get(str(ws), {}),
                                    "mixed": {x: v for x, v in ws_mixed.get(str(ws), {}).items() if x in keys}})
    result = tally.result("workspaces", "workspaces")
    result["unbilled"] = [
        {"id": ws, "name": name}
        for ws, name in con.execute(
            "SELECT workspace_id, max(name) FROM dims.dim_workspace GROUP BY 1 HAVING NOT bool_or(billed_in_snapshot) ORDER BY 2, 1"
        ).fetchall()
        if _in_scope(ws, scope)
    ]
    return result
