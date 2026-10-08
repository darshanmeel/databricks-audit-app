"""app/api/app.py

The FastAPI application (tasks/T-57-web-vertical-slice.md). Every route returns JSON, never HTML;
the store is read exactly once per request through app/core/data.py's existing functions (plus
app/core/data.count_by_status, T-57's own read-only addition) -- this module never builds SQL
itself. app/web's static files are served from the same process so "one command to run it" is
literal: `python -m app.api` (or the uvicorn line in its own docstring) serves both the API and
the vendored-React front end.

Routes:
    GET /api/status                     P2-NOBUILD: the one route app/web's shell calls before
                                         anything else -- {state, settings_error, database}. state
                                         is "settings_invalid" | "no_database" | "build_incomplete"
                                         | "ready" (settings_invalid wins: with config/settings.yml
                                         broken nothing else can be trusted). Never raises, never
                                         touches the database when settings.yml is already broken.
                                         web/src/components/first_run.tsx renders the friendly
                                         screen for anything but "ready", naming the exact next
                                         command from README.md's own "First run" section. brand (settings' brand
                                         block, null when settings_invalid) for the setup screens.
    GET /api/meta                       as_of, window options, model counts ok/failed, snapshot
                                         availability, plus (T-63/DEC-60) attribute_keys_populated
                                         -- which config/tag_aliases.yml canonical keys at least
                                         one workspace actually carries. metastore ({cloud, region,
                                         id_fingerprint} or null, T-71). brand ({name, url,
                                         contact_name, contact_email}) for the wordmark and footer.
                                         direct_export: {as_of, window_coverage, windows,
                                         window_aliases, tags_sources_not_exported} when
                                         tools/load_direct_results.py built the db, else null.
    GET /api/workspaces                 dims.dim_workspace: workspace_id, name, env, env_reason
                                         (plus url/env_source), plus (T-63/DEC-60) one <key>/
                                         <key>_share/<key>_reason column set per canonical key.
                                         region_status (in_region | outside_region | unknown) and
                                         region_reason (T-71).
    GET /api/dims                       T-58: dims.dim_job/cluster/warehouse/pipeline (names +
                                         owner columns), for "names, not ids" in the tab charts.
                                         T-66B: every owner/run-as identity column is
                                         masked to DEC-66.3's format before this returns, only when
                                         privacy.mask_user_identities is on (app/core/data.py's
                                         read_dim_job and friends do the masking; this route
                                         serves whatever they return, unmodified).
    GET /api/coverage                   T-58: source states x dependent query_ids, plus the
                                         run_results.json per-model detail -- the Coverage & Gaps
                                         tab's own feed. export_mode ("snapshot" | "direct" |
                                         "none") says which; a direct export (no snapshot manifest)
                                         derives table state from run_results instead of reading
                                         every source as uncaptured. checks_not_ok groups every
                                         failed/never-run check by plain reason, with the fix.
                                         truncated_query_ids: a direct export's own row-cap hits.
                                         region (T-71).
    GET /api/guide                      app/api/guide.py's build_guide() -- the Guide tab's whole
                                         feed. One entry per executable registry query (header
                                         fields, params, first step, known issues, docs links, the
                                         built table's columns when available) plus the "How this
                                         app works" sections' own numbers and the Databricks docs
                                         map, resolved for every cloud.
    GET /api/rollup                     P4-T-ROLL: the nested cost or performance rollup for one
                                         tag key (tasks/P4-T-SPEC.md section 4.5) -- account, then
                                         workspace tag, then compute tag, then query/job tag, every
                                         dollar or statement counted once. area=cost (default) or
                                         performance. tag_value (repeatable): one value marks
                                         `selected` as before; several also OR-mark every match
                                         into `selected_values`. 422 for a bad area, a missing/
                                         empty tag_key, or an unknown window.
    GET /api/tag_origins                the dollars the active tag filter keeps, split by where
                                         each dollar's value was set: query, job or pipeline tag,
                                         the warehouse/cluster/policy tag on the bill, or the
                                         workspace tag. keys [] with no tag filter.
    GET /api/rollup/keys                P4-T-ROLL: the tag keys seen at compute or work level (or
                                         allocating on any workspace) in the window, for the Cost
                                         "By tag" view's own key chooser.
    GET /api/rollup/top                 the by-tag view: top `top` (default 10, max 100) tag
                                         VALUES for one key by $ (area=cost) or by total query time
                                         (area=performance), each row labelled by the level it sits
                                         on (query/warehouse/cluster/job/pipeline/workspace/
                                         "billing line") and never merged across levels -- the same
                                         value on a warehouse and on a job are two rows. Always
                                         carries one "untagged" row and `more_count` (rows past
                                         `top`, not themselves returned).
    GET /api/tags                       P4-T-IDX: every tag key/value seen anywhere in the
                                         snapshot, no configuration -- ?search=, optional
                                         ?tag_key= (all values of one key), limit_keys/
                                         limit_values. 503 with a plain detail when the tags
                                         models were not built.
    GET /api/findings                   one row per executable registry query: query_id, title,
                                         domain, tier, stars, outcome, status_counts, row_count.
                                         region_gap, and per row has_workspace_id, regional,
                                         not_assessed_reason (T-71). Accepts the same T-63/DEC-60
                                         workspace-attribute filters (cost_center/team/
                                         business_unit/domain, whatever config/tag_aliases.yml
                                         declares) as GET /api/finding -- P2-FILTERS wired this
                                         list endpoint's own bulk counts and region_gap to them,
                                         so a cost-centre filter narrows the findings table the
                                         same way a workspace/env filter already does. P4-T-IDX:
                                         optional tag_key/tag_value, same contract as GET
                                         /api/finding -- echoed as "tag", with each row's own
                                         tag_applied/tag_chain/tag_chain_label (None/[]/None when
                                         no tag filter was given); a query_id whose chain needs
                                         tags.tag_entity when that table was never built reads
                                         not_assessed with reason "tag_models_not_built". Several
                                         tag names: repeat `tag=<key>` for All or `tag=<key>:
                                         <value>` for one value (split on the FIRST ':'; a key
                                         containing ':' must be percent-encoded) -- the same key
                                         ORs its values, a different key ANDs across names;
                                         tag_key/tag_value keep meaning exactly what they do today
                                         and merge into this.
    GET /api/finding/{query_id}         columns, rows, the four-outcome outcome, the header
                                         fields a panel shows -- read_this/healthy/investigate_if/
                                         actions/not_assessed_reasons carry every :param token
                                         replaced by the value built for this account plus its
                                         source (P4-03, service.substitute_header_params) -- plus
                                         order_by, scope (T-71). Materiality floor
                                         (config/materiality.yml): each ok_rows row's `status` is
                                         the EFFECTIVE (floored) status -- a suppressed row's raw
                                         value survives as status_raw -- plus below_floor, and the
                                         body carries "floor"
                                         ({"column","min","unit","label","rows_below"} or None).
                                         Optional job_id (T-70)
                                         scopes to one job server-side, on a finding that carries
                                         job_id -- the job focus panel's own filter. Optional
                                         `status` (repeated, T-68) scopes to those status values
                                         server-side, on a finding that carries a status column --
                                         so a tile that needs only flagged rows fetches only those,
                                         instead of a capped, unfiltered page. row_cap
                                         (T-70 review round 2, service.row_cap) reports the
                                         query's BUILT :top_n cap on the ok_rows/ok_empty_filters
                                         bodies, None when it has no such param. P4-T-IDX: optional
                                         tag_key/tag_value (any spelling; tag_value without
                                         tag_key is a 422) scope to one tag value server-side, on
                                         whatever level the query's own chain can see (section 5.4)
                                         -- echoed as "tag", with "scope.tag" naming the chain and
                                         whether it applied. GET /api/findings' bulk list carries
                                         the same filter, per row (above). Several tag names: same
                                         repeatable `tag=<key>[:<value>]` format as GET /api/findings.
    GET /api/finding/{query_id}/aggregate
                                         T-68: a server-side SUM/COUNT/COUNT DISTINCT over EVERY
                                         row matching the filters (no `ui.max_rows` cap before the
                                         aggregation), grouped by 0-2 real columns, with a top-N +
                                         "other" fold when `top` is given. The fix for "a Cost/
                                         ML & AI/Overview tile or chart summed only the 5,000-row
                                         slice the browser fetched". Same tag_key/tag_value/`tag`
                                         contract as GET /api/finding.
    GET /                               the built page, app/web/dist/index.html (built from web/);
                                         any other non-/api path returns it too.

A query_id app/core/registry does not know 404s (service.QueryNotFoundError); any other
unexpected failure surfaces as FastAPI's own 500 with the exception's own message -- the honesty
rule's ERROR outcome only covers a STORE-level read failure (a locked/corrupt db file), which
load_outcome/list_findings already catch and report as outcome="error" with a 200, not an
exception. A broken config/settings.yml (app_config.ConfigError, DEC-57/58: a typo used to turn
EVERY route into an opaque 500) is the one exception besides HTTPException this module gives its
own handler below -- every route calls app_config.load_settings() sooner or later, so one handler
covers all of them: a structured 503 naming the offending key/line (P2-NOBUILD) instead of
FastAPI's default plain-text 500.
"""
from __future__ import annotations

import html
import json
import mimetypes
import sys
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api import guide as guide_api  # noqa: E402
from app.api import refresh as app_refresh  # noqa: E402
from app.api import service  # noqa: E402
from app.core import config as app_config  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import identity as app_identity  # noqa: E402
from app.core import library_corrections  # noqa: E402
from app.core import materiality as app_materiality  # noqa: E402
from app.core import pricing as app_pricing  # noqa: E402
from app.core import registry  # noqa: E402
from app.core import rollup as app_rollup  # noqa: E402
from app.core import tags as app_core_tags  # noqa: E402  -- P4-T-IDX: GET /api/tags, tag_key/tag_value

WEB_DIR = _ROOT / "app" / "web" / "dist"

app = FastAPI(title="Crosshire API", version="0.1.0")

# T-75A: fail loudly at import time, not lazily on the first request that happens to touch a
# finding with a correction -- a broken config/library_corrections.yml (an unknown query_id, a
# duplicate id, ...) must stop the app from serving at all, the same "fail loudly, never a silent
# skip" discipline app/core/registry.py's own _validate_next enforces at import.
library_corrections.load_corrections()


# P2-NOBUILD: one handler for the whole app -- every route calls app_config.load_settings() sooner
# or later (directly, or through service.load_outcome/list_findings), so a typo in
# config/settings.yml used to turn EVERY route into FastAPI's default plain-text 500 ("Could not
# load 500 Internal Server Error" on the React side, no detail at all). A structured 503 naming
# the offending key/line (SettingsError.key/.line, app/core/config.py) instead, read by every
# GET /api/finding*/meta/workspaces caller the same way, and by GET /api/status below to build the
# friendly "fix this line" screen without waiting for some OTHER route to happen to fail first.
@app.exception_handler(app_config.ConfigError)
def _config_error_handler(request: Request, exc: app_config.ConfigError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "state": "settings_invalid",
            "key": getattr(exc, "key", None),
            "line": getattr(exc, "line", None),
        },
    )


def _validate_window(window: int) -> None:
    if window not in service.WINDOW_CHOICES:
        raise HTTPException(
            422, f"window must be one of {list(service.WINDOW_CHOICES)}, got {window}"
        )


def _resolve_window(window: int | None, settings: dict) -> int:
    resolved = window if window is not None else settings["default_window"]
    _validate_window(resolved)
    return resolved


def _open_window(default_window: int, windows: list[dict]) -> int:
    """The window GET /api/meta opens on: default_window if this db has it, else the largest one
    it does (never a picker stuck on a window the db doesn't have)."""
    available = [w["days"] for w in windows if w["available"]]
    if default_window in available:
        return default_window
    return max(available) if available else default_window


# T-68: the status-value vocabulary GET /api/finding and its /aggregate twin accept -- the same
# closed set service.STATUS_ORDER already names (never an open string, which would let an
# unbound-cardinality value reach the `status IN (...)` clause -- still bound-parameter-safe, but
# an unknown value is a caller bug worth a 422 rather than a silent no-match).
def _validate_statuses(statuses: list[str]) -> None:
    bad = [s for s in statuses if s not in service.STATUS_ORDER]
    if bad:
        raise HTTPException(422, f"status must be one of {service.STATUS_ORDER}, got {bad!r}")


def _parse_tag_filter(
    tag_key: str | None, tag_value: str | None, tag: list[str] | None = None
) -> "app_core_tags.TagFilterSet | None":
    """P4-T-IDX (section 4.4) + the several-tag-names filter: `tag_value` without `tag_key`, or a
    `tag_key`/`tag` entry that normalises to empty, is a 422 --
    app_core_tags.TagFilterSet.from_params' own ValueError, converted here the same way
    service.load_outcome's ValueError already becomes one on the aggregate route. `tag` (repeated):
    "<key>" for All (the key present, any value) or "<key>:<value>" for one value -- repeat the
    same key to OR its values, a different key to AND across names; the legacy `tag_key`/
    `tag_value` pair keeps meaning exactly what it does today and merges into this the same way."""
    try:
        return app_core_tags.TagFilterSet.from_params(tag_key, tag_value, tag)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _tag_json(tag: "app_core_tags.TagFilterSet | None") -> dict | None:
    """The `"tag"` object every tag-aware route echoes, or None with no filter. A plain
    single-name request (0 or 1 value) returns the exact {tag_key, display_key, tag_value} shape
    it always has. Several names, or several values on one name, add `groups` and "multi": true;
    tag_key/tag_value (and every per-row tag_match_* column) still describe the FIRST name only, so
    tag_value is that name's first value rather than null -- an older client reading null as "All
    values" would otherwise be wrong."""
    if tag is None:
        return None
    first = tag.groups[0]
    multi = len(tag.groups) > 1 or len(first.values) > 1
    body = {
        "tag_key": first.key,
        "display_key": first.key,  # the exact display spelling lives in tags.tag_index only
        "tag_value": app_core_tags.tag_keys.model_value_to_api(first.values[0]) if first.values else None,
    }
    if multi:
        body["multi"] = True
        body["groups"] = [
            {
                "tag_key": g.key,
                "display_key": g.key,
                "values": [app_core_tags.tag_keys.model_value_to_api(v) for v in g.values],
            }
            for g in tag.groups
        ]
    return body


# -------------------------------------------------------------------------------------------
# Top-tag filters: one per config/tag_aliases.yml `top_tags` key, read as ordinary query params
# by NAME (request.query_params.getlist), since the key set is config-driven.
# -------------------------------------------------------------------------------------------


def _canonical_keys() -> list[str]:
    return list(app_config.load_top_tags())


def _read_attribute_filters(request: Request, canonical_keys: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key in canonical_keys:
        values = request.query_params.getlist(key)
        if values:
            out[key] = values
    return out


_UNPOPULATED_ATTRIBUTE_VALUES = {"not_tagged", "no_usage"}


def _populated_attribute_keys(canonical_keys: list[str]) -> list[str]:
    """The first TOP_TAGS_SHOWN top tags, in file order, that at least one workspace carries a real
    value or 'mixed' for -- an empty dropdown would read as "there are no cost centres"."""
    if not canonical_keys:
        return []
    try:
        df = app_core_data.read_dim_workspace()
    except (duckdb.Error, OSError):
        return []
    populated = []
    for key in canonical_keys:
        if key not in df.columns:
            continue
        if (~df[key].isin(_UNPOPULATED_ATTRIBUTE_VALUES)).any():
            populated.append(key)
    return populated[:app_config.TOP_TAGS_SHOWN]


# -------------------------------------------------------------------------------------------
# GET /api/status -- P2-NOBUILD: app/web's shell calls this FIRST, before /api/meta or
# /api/workspaces, and renders a friendly screen (first_run.tsx) for anything but "ready" instead
# of ever reaching a route that would 500 or hang the shell waiting on data that cannot arrive.
# -------------------------------------------------------------------------------------------


@app.get("/api/status")
def get_status() -> dict:
    # settings.yml is checked first and reported on its own, deliberately not through the
    # ConfigError exception handler above -- that handler is for every OTHER route, which has no
    # honest answer once settings.yml is broken; this one route's whole job is to have an answer
    # regardless, so it catches the same exception itself instead of letting it propagate.
    settings_error = None
    settings = None
    try:
        settings = app_config.load_settings()
    except app_config.ConfigError as exc:
        settings_error = {
            "message": str(exc),
            "key": getattr(exc, "key", None),
            "line": getattr(exc, "line", None),
        }
    database = app_core_data.db_state()
    if settings_error is not None:
        # With settings.yml broken, every other route 503s (the handler above) whether or not the
        # database itself is fine -- so this is the one state worth reporting, not "also broken".
        state = "settings_invalid"
    else:
        state = database["state"]
    return {
        "state": state,
        "settings_error": settings_error,
        "database": database,
        "brand": settings["brand"] if settings is not None else None,
    }


# -------------------------------------------------------------------------------------------
# GET /api/meta
# -------------------------------------------------------------------------------------------


@app.get("/api/refresh")
def get_refresh() -> dict:
    return app_refresh.status()


@app.post("/api/refresh")
def post_refresh(days: int = Query(default=30, ge=1, le=90)) -> dict:
    """Export the last `days` days from Databricks and load them; the page reloads when done."""
    return app_refresh.start(days)


# -------------------------------------------------------------------------------------------


@app.get("/api/meta")
def get_meta() -> dict:
    manifest = app_core_data.snapshot_manifest()
    results = app_core_data.run_results()
    settings = app_config.load_settings()

    models = results.get("models", {})
    total = len(models)
    ok = sum(1 for m in models.values() if m.get("status") in ("success", "pass"))
    failed = sum(1 for m in models.values() if m.get("status") == "error")
    skipped = sum(1 for m in models.values() if m.get("status") == "skipped")
    registry_total = sum(1 for s in registry.load_registry() if s.executable)
    canonical_keys = _canonical_keys()
    direct = app_core_data.direct_export_info()
    windows = app_core_data.window_status(service.WINDOW_CHOICES, direct)
    exported_coverage = app_core_data.exported_window_coverage(direct)
    if manifest.get("available"):
        as_of_date = manifest.get("as_of_date")
    else:
        # the window range labels need an end date on a direct-export database too
        as_of_date = (direct.get("as_of_date") or str(direct["as_of"] or "")[:10] or None) if direct else None

    return {
        "as_of": manifest.get("as_of") if manifest.get("available") else None,
        "as_of_date": as_of_date,
        "snapshot_available": bool(manifest.get("available")),
        # DEC-64: how many days the exporter actually captured, so the UI can mark a window the
        # snapshot cannot fill. Windows are always BUILT for 7/30/90 whatever the snapshot span,
        # so a --days 7 export read at 30d returns 7 days of money under a "30d" label unless
        # something says otherwise. `billing_days` is separate because the exporter takes a
        # longer window for billing than for everything else.
        "snapshot_days": (
            manifest.get("days")
            if manifest.get("available")
            else (max(exported_coverage.values()) if exported_coverage else None)
        ),
        "snapshot_billing_days": manifest.get("billing_days") if manifest.get("available") else None,
        # T-71: the metastore the snapshot connected through -- regional system tables cover
        # this region only. Null when the manifest does not record it.
        "metastore": manifest.get("metastore") if manifest.get("available") else None,
        "window_options": list(service.WINDOW_CHOICES),
        "default_window": settings["default_window"],
        # windows: each WINDOW_CHOICES label's real availability under THIS db (a direct export
        # may not have run every one); open_window is the window the app should open on.
        "windows": windows,
        "open_window": _open_window(settings["default_window"], windows),
        "discount_pct": settings["discount_pct"],
        "brand": settings["brand"],
        "mask_user_identities": settings["privacy"]["mask_user_identities"],
        "mandatory_tag_keys": settings["mandatory_tag_keys"],
        # T-63 (DEC-60 rule 5): the canonical tag-attribute keys at least one workspace actually
        # carries -- web/src/components/filters.tsx renders one picker per entry here, never for
        # a key with zero real data (an always-empty dropdown reads as "nothing has cost
        # centres" rather than "nothing is tagged").
        "attribute_keys_populated": _populated_attribute_keys(canonical_keys),
        # every configured top tag as {key, label}, in file order: names the pickers and Money's tabs
        "top_tags": [{"key": k, "label": v} for k, v in app_config.load_top_tags().items()],
        # T-58: the top-N-plus-Other cap every chart's aggregation helper (hooks.ts
        # topNWithOther) respects, so app/web never invents its own category cap.
        "max_chart_categories": settings["ui"]["max_chart_categories"],
        "run_results_available": bool(results.get("available")),
        "generated_at": results.get("generated_at"),
        # 2026-09-25 fix: the one cut-off every money figure on screen now shares (complete days
        # only, through yesterday) -- web/src/App.tsx's AsOfBanner shows it once so two cost cards
        # never look like they disagree just because one silently counted today's in-flight hours.
        "cost_cutoff": app_core_data.cost_cutoff(),
        "direct_export": direct,
        "db_path": str(app_core_data._db_path()),
        "models": {
            "registry_total": registry_total,
            "run_results_total": total,
            "ok": ok,
            "failed": failed,
            "skipped": skipped,
            "not_built": max(registry_total - total, 0),
        },
    }


# -------------------------------------------------------------------------------------------
# GET /api/workspaces
# -------------------------------------------------------------------------------------------


_WORKSPACE_FIXED_COLUMNS = (
    "workspace_id", "name", "url", "env", "env_source", "env_reason", "in_snapshot_region",
    "billed_in_snapshot",
)


@app.get("/api/workspaces")
def get_workspaces() -> list[dict]:
    # P2-NOBUILD: unlike GET /api/finding*, read_dim_workspace() had no guard of its own -- a
    # missing/corrupt db or a build that never reached dims.dim_workspace raised straight through
    # to FastAPI's default 500 (app/web's shell calls this on every load, so this was the actual
    # crash behind "Could not load 500 Internal Server Error"). app/web's shell now checks
    # GET /api/status first and never reaches this route in that state, but this guard stays as
    # the honest fallback for a build that fails mid-session (dbt_run.py's atomic swap) or for any
    # other direct caller: a clear 503, never an opaque 500.
    try:
        df = app_core_data.read_dim_workspace()
    except (duckdb.Error, OSError) as exc:
        raise HTTPException(503, f"dims.dim_workspace could not be read: {exc}")
    # T-63 (DEC-60): the <key>/<key>_share/<key>_reason columns are config-driven (whatever
    # config/tag_aliases.yml declares at the last dbt build), so they are carried through
    # generically rather than named here -- same "no hard-coded key name" discipline as the SQL.
    extra_columns = [c for c in df.columns if c not in _WORKSPACE_FIXED_COLUMNS]
    out: list[dict] = []
    for _, row in df.iterrows():
        rec = {
            "workspace_id": str(row["workspace_id"]),
            "name": service._json_scalar(row.get("name")),
            "url": service._json_scalar(row.get("url")),
            "env": service._json_scalar(row.get("env")),
            "env_source": service._json_scalar(row.get("env_source")),
            "env_reason": service._json_scalar(row.get("env_reason")),
        }
        # T-71: which workspaces the snapshot's regional tables cannot see, in words.
        rec["region_status"], rec["region_reason"] = service.workspace_region(
            service._json_scalar(row.get("in_snapshot_region")),
            service._json_scalar(row.get("billed_in_snapshot")),
        )
        for col in extra_columns:
            rec[col] = service._json_scalar(row.get(col))
        out.append(rec)
    return out


# -------------------------------------------------------------------------------------------
# GET /api/dims -- T-58: dims.dim_job/cluster/warehouse/pipeline, for "names, not ids" in the
# tab charts (app/core/data.read_dim_job and friends already exist, T-40). Each dim is queried
# independently so one missing/unbuilt dim (a duckdb CatalogException, app/core/data.py's own
# documented contract for these four functions) never blanks out the other three; the caller
# sees an empty list for that dim instead.
#
# T-66B / DEC-66.3: app/core/data.read_dim_job/read_dim_cluster/read_dim_warehouse/
# read_dim_pipeline now mask every owner/run-as identity column (run_as, run_as_user_name,
# creator_user_name, owned_by, created_by) to DEC-66.3's format before returning -- this route
# does no masking of its own and needs none; it serves those readers' frames exactly as
# _dim_records below already serialises every other column.
#
# _dim_records is column-vectorised (no per-cell iterrows() loop over ~17k combined dim rows), the
# four read_dim_* calls share one connection, and _DIMS_CACHE below skips recomputing entirely once
# the db file (path + mtime) and the masking setting both stay the same -- a config-only masking
# flip still needs the response recomputed even though the db itself did not change, so that flag
# is part of the cache key too.
# -------------------------------------------------------------------------------------------


def _dim_records(df, id_cols: tuple[str, ...]) -> list[dict]:
    """df -> JSON-safe records, column at a time (NaN -> None, id_cols -> str) instead of
    df.iterrows()'s per-cell loop -- the same values _json_scalar produced, since every dim
    column here is plain text or null, never a Timestamp/numpy scalar."""
    if df.empty:
        return []
    columns = list(df.columns)
    arrays: list[list] = []
    for col in columns:
        values = df[col].tolist()
        if col in id_cols:
            values = [None if (v is None or v != v) else str(v) for v in values]
        else:
            values = [None if (v is None or (isinstance(v, float) and v != v)) else v for v in values]
        arrays.append(values)
    return [dict(zip(columns, row)) for row in zip(*arrays)]


def _dim_or_empty(loader, con: duckdb.DuckDBPyConnection | None, id_cols: tuple[str, ...]) -> list[dict]:
    try:
        return _dim_records(loader(con), id_cols)
    except (duckdb.Error, OSError):
        return []


# db path (str) + mtime + masking flag -> the last get_dims() response computed for that
# combination. A single entry (not one per key ever seen) -- an old db/setting is never coming
# back, so nothing is gained by keeping more than the most recent.
_DIMS_CACHE: dict[tuple[str, float | None, bool], dict] = {}


@app.get("/api/dims")
def get_dims() -> dict:
    db_path = app_core_data._db_path()
    try:
        mtime = db_path.stat().st_mtime
    except OSError:
        mtime = None
    key = (str(db_path), mtime, app_identity.masking_enabled())
    cached = _DIMS_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        con = app_core_data._connect()
    except duckdb.Error:
        con = None  # each read_dim_* below opens (and fails on) its own connection instead
    try:
        result = {
            "jobs": _dim_or_empty(app_core_data.read_dim_job, con, ("workspace_id", "job_id")),
            "clusters": _dim_or_empty(app_core_data.read_dim_cluster, con, ("workspace_id", "cluster_id")),
            "warehouses": _dim_or_empty(
                app_core_data.read_dim_warehouse, con, ("workspace_id", "warehouse_id")
            ),
            "pipelines": _dim_or_empty(
                app_core_data.read_dim_pipeline, con, ("workspace_id", "pipeline_id")
            ),
            "notebooks": _dim_or_empty(
                app_core_data.read_dim_notebook, con, ("workspace_id", "notebook_id")
            ),
        }
    finally:
        if con is not None:
            con.close()

    if con is not None:
        # Only a working connection is worth remembering -- a total connect failure (db mid-build,
        # briefly locked) should retry on the very next call, not get stuck returning empty dims
        # until the file's mtime happens to change.
        _DIMS_CACHE.clear()
        _DIMS_CACHE[key] = result
    return result


# -------------------------------------------------------------------------------------------
# GET /api/coverage -- T-58's Coverage & Gaps tab: source states x dependent query_ids, plus the
# run_results.json per-model detail /api/meta only summarises as counts. Read-only, JSON-only
# (DEC-27) -- no DuckDB connection of its own except direct_source_states'/truncated_query_ids'
# own short-lived ones.
#
# Two source states, never mixed: a real snapshot_manifest.json (an ordinary dbt/snapshot build)
# reports it verbatim, unchanged; a loader-built direct export has no manifest at all, so its
# state instead comes from direct_source_states (run_results.json: which checks reading a source
# actually succeeded) -- the old code read a missing manifest as "every source uncaptured" even
# when the export read almost all of them fine.
# -------------------------------------------------------------------------------------------


@app.get("/api/coverage")
def get_coverage() -> dict:
    manifest = app_core_data.snapshot_manifest()
    results = app_core_data.run_results()
    direct = app_core_data.direct_export_info()
    specs = [s for s in registry.load_registry() if s.executable]

    dependents: dict[str, list[str]] = {}
    for s in specs:
        for schema, table in s.sources:
            dependents.setdefault(f"system.{schema}.{table}", []).append(s.query_id)

    if manifest.get("available"):
        export_mode = "snapshot"
        tables_out: dict[str, dict] = {}
        for key, info in manifest.get("tables", {}).items():
            tables_out[key] = {**info, "dependent_query_ids": dependents.get(key, [])}
        for key, ids in dependents.items():
            if key not in tables_out:
                tables_out[key] = {
                    "state": "not_assessed", "rows": None, "reason": "not captured in this snapshot",
                    "dependent_query_ids": ids,
                }
    elif direct is not None:
        export_mode = "direct"
        tables_out = app_core_data.direct_source_states(dependents, results)
    else:
        export_mode = "none"
        tables_out = {
            key: {"state": "not_assessed", "rows": None, "reason": "no export or snapshot yet", "dependent_query_ids": ids}
            for key, ids in dependents.items()
        }

    split = service.region_split_or_none()

    return {
        "export_mode": export_mode,
        "manifest_available": manifest.get("available"),
        "as_of": manifest.get("as_of") if manifest.get("available") else (direct or {}).get("as_of"),
        "as_of_date": manifest.get("as_of_date"),
        "tables": tables_out,
        "run_results_available": results.get("available"),
        "generated_at": results.get("generated_at"),
        "models": results.get("models", {}),
        # Every check not built cleanly this run, grouped by its own plain reason with the fix --
        # the Couldn't-check panel's feed, in both a direct export and an ordinary build.
        "checks_not_ok": app_core_data.checks_not_ok(specs, results, manifest),
        # A direct export's own row-cap hits ([] on an ordinary build, which has no such cap).
        "truncated_query_ids": app_core_data.truncated_query_ids() if export_mode == "direct" else [],
        # T-75A (DEC-66.2): the whole library-corrections register, for the Coverage tab's
        # browsable list -- every known defect in a vendored query, not just the ones a finding
        # currently on screen happens to carry.
        "library_corrections": service.all_library_corrections(),
        # T-71: every workspace the regional tables hold no row for (spend or not), and the
        # metastore the snapshot connected through.
        "region": {
            "metastore": manifest.get("metastore"),
            # F7: a `first_run.py --workspace <id ...>` snapshot filters EVERY table, billing
            # included (tools/snapshot.py), to those workspace ids -- web/src/components/scope.tsx
            # must not then claim billing covers the whole account.
            "workspace_ids": manifest.get("workspace_ids") or [],
            "outside": None if split is None else service.gap_entries(split["outside"]),
            "unknown": None if split is None else bool(split["unknown"]),
            "unknown_reason": service.REGION_UNKNOWN_REASON,
        },
    }


# -------------------------------------------------------------------------------------------
# GET /api/guide -- the Guide tab's one feed, entirely built by app/api/guide.build_guide().
# -------------------------------------------------------------------------------------------


@app.get("/api/guide")
def get_guide() -> dict:
    return guide_api.build_guide()


@app.get("/api/guide/sql/{query_id}")
def get_guide_sql(query_id: str, window: int | None = Query(default=None)) -> dict:
    """The SQL the export runs for this check, ready to paste into a Databricks SQL editor."""
    eff_window = _resolve_window(window, app_config.load_settings())
    sql = guide_api.runnable_sql(query_id, eff_window)
    if sql is None:
        raise HTTPException(404, f"no Databricks SQL for {query_id}")
    return {"query_id": query_id, "window_days": eff_window, "sql": sql}


# -------------------------------------------------------------------------------------------
# GET /api/rollup / GET /api/rollup/keys -- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.4). Thin
# wrappers over app/core/rollup.py: the window is resolved the same way as every other route
# (_resolve_window), and a bad area/tag_key/window from app/core/rollup.RollupError becomes a 422
# here, never an opaque 500.
# -------------------------------------------------------------------------------------------


@app.get("/api/rollup")
def get_rollup(
    request: Request,
    area: str = Query(default="cost"),
    tag_key: str = Query(...),
    window: int | None = Query(default=None),
    # tag_value (repeatable): a single value keeps marking one `selected` entry exactly as before;
    # several OR-mark every matching entry into the new `selected_values` list too (rollup always
    # builds the tree for this ONE tag_key -- there is no second name to AND against here).
    tag_value: list[str] = Query(default=[]),
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
    # C26: the SAME active tag filter (Api.tagParams()) /api/finding's aggregate already applies --
    # distinct from tag_key/tag_value above, which pick which value this rollup marks `selected`.
    tag: list[str] = Query(default=[]),
) -> dict:
    settings = app_config.load_settings()
    eff_window = _resolve_window(window, settings)
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    tag_filter = _parse_tag_filter(None, None, tag)
    try:
        return app_rollup.rollup(
            area, tag_key, eff_window,
            tag_value=tag_value[0] if tag_value else None, tag_values=tag_value or None,
            workspace_ids=workspace_ids or None, env=env or None, attributes=attributes,
            tag_filter=tag_filter,
        )
    except app_rollup.RollupError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/rollup/top")
def get_rollup_top(
    request: Request,
    area: str = Query(default="cost"),
    tag_key: str = Query(...),
    window: int | None = Query(default=None),
    top: int = Query(default=10, ge=1, le=100),
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
    tag: list[str] = Query(default=[]),
) -> dict:
    settings = app_config.load_settings()
    eff_window = _resolve_window(window, settings)
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    tag_filter = _parse_tag_filter(None, None, tag)
    try:
        return app_rollup.by_tag_top(
            area, tag_key, eff_window, top=top,
            workspace_ids=workspace_ids or None, env=env or None, attributes=attributes,
            tag_filter=tag_filter,
        )
    except app_rollup.RollupError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/tag_origins")
def get_tag_origins(
    request: Request,
    window: int | None = Query(default=None),
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
    tag: list[str] = Query(default=[]),
) -> dict:
    eff_window = _resolve_window(window, app_config.load_settings())
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    tag_filter = _parse_tag_filter(None, None, tag)
    scope = app_rollup._resolve_workspace_scope(workspace_ids or None, env or None, attributes)
    found = app_core_data.read_tag_origins(tag_filter, eff_window, scope)
    return found or {"window_days": eff_window, "usd": 0.0, "keys": []}


@app.get("/api/workspace_tags")
def get_workspace_tags(
    request: Request,
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
) -> dict:
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    scope = app_rollup._resolve_workspace_scope(workspace_ids or None, env or None, attributes)
    rows = app_core_data.read_workspace_tags(scope)
    settings = app_config.load_settings()
    floors = {"coverage_floor": settings["tag_coverage_floor"], "share_floor": settings["tag_share_floor"]}
    if rows is None:
        return {"outcome": "not_assessed", "rows": [], **floors}
    return {"outcome": "ok_rows" if rows else "ok_empty", "rows": rows, **floors}


@app.get("/api/abac_policies")
def get_abac_policies() -> dict:
    return app_core_data.read_abac_policies()


@app.get("/api/tag_compliance")
def get_tag_compliance(
    request: Request,
    window: int | None = Query(default=None),
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
) -> dict:
    eff_window = _resolve_window(window, app_config.load_settings())
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    scope = app_rollup._resolve_workspace_scope(workspace_ids or None, env or None, attributes)
    return _cached(("tag_compliance", eff_window, tuple(sorted(scope)) if scope is not None else None),
                   lambda: app_core_data.read_tag_compliance(eff_window, scope))


@app.get("/api/rollup/keys")
def get_rollup_keys(
    area: str = Query(default="cost"),
    window: int | None = Query(default=None),
) -> dict:
    settings = app_config.load_settings()
    eff_window = _resolve_window(window, settings)
    try:
        return app_rollup.rollup_keys(area, eff_window)
    except app_rollup.RollupError as exc:
        raise HTTPException(422, str(exc))


# -------------------------------------------------------------------------------------------
# GET /api/tags -- P4-T-IDX (tasks/P4-T-SPEC.md section 5.6). The tag search picker's own feed:
# every tag key and value seen anywhere in the snapshot, no configuration.
# -------------------------------------------------------------------------------------------


@app.get("/api/tags")
def get_tags(
    search: str = Query(default=""),
    tag_key: str | None = Query(default=None),
    limit_keys: int = Query(default=20, ge=1, le=200),
    limit_values: int = Query(default=50, ge=1, le=500),
) -> dict:
    try:
        return _cached(("tags", search, tag_key, limit_keys, limit_values),
                       lambda: app_core_tags.read_tag_index(search or None, tag_key, limit_keys, limit_values))
    except app_core_tags.TagModelsNotBuiltError:
        raise HTTPException(503, "the tag index was not built; rebuild to search tags")


# -------------------------------------------------------------------------------------------
# GET /api/findings
# -------------------------------------------------------------------------------------------


# GET /api/findings counts every check. The database only changes on a load or Refresh and the
# config on an edit, so each answer is kept until one of them changes.
_FINDINGS_CACHE: dict = {}
_FINDINGS_CACHE_MAX = 32
_CONFIG_FILES = ("settings.yml", "settings.local.yml", "thresholds.yml", "materiality.yml",
                 "tag_aliases.yml", "library_corrections.yml")


def _data_version() -> tuple:
    cfg = app_config._config_dir()
    mtimes = tuple((cfg / n).stat().st_mtime_ns if (cfg / n).exists() else None for n in _CONFIG_FILES)
    # The summary also reads the export manifest and run results, which can change without the db.
    sides = tuple(app_core_data._identity_key(str(f())) for f in (app_core_data._snapshot_manifest_path, app_core_data._run_results_path))
    return (app_core_data._identity_key(str(app_core_data._db_path())), mtimes, sides)


@app.get("/api/findings")
def get_findings(
    request: Request,
    window: int | None = Query(default=None),
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
    # P4-T-IDX: any spelling; the server normalises it. tag_value without tag_key is a 422
    # (_parse_tag_filter). Same contract as GET /api/finding's own tag_key/tag_value.
    tag_key: str | None = Query(default=None),
    tag_value: str | None = Query(default=None),
    # Several tag names (repeatable): "<key>" for All, "<key>:<value>" for one value -- same key
    # repeated ORs its values, a different key ANDs across names; merges with tag_key/tag_value.
    tag: list[str] = Query(default=[]),
) -> dict:
    settings = app_config.load_settings()
    eff_window = _resolve_window(window, settings)
    # T-63 (DEC-60) / P2-FILTERS: one query param per canonical key, same _read_attribute_filters
    # convention GET /api/finding and its /aggregate twin already use -- this list endpoint used
    # to ignore them entirely, which left chargeback-by-cost-centre unreachable from the findings
    # table (only a single finding's own detail/aggregate could be scoped that way).
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    tag_filter = _parse_tag_filter(tag_key, tag_value, tag)
    return _findings_answer(eff_window, workspace_ids, env, attributes, tag_filter)


def _findings_answer(eff_window: int, workspace_ids: list[str], env: list[str], attributes, tag_filter) -> dict:
    cache_key = (_data_version(), eff_window, tuple(workspace_ids), tuple(env),
                 json.dumps(attributes, sort_keys=True, default=str), repr(tag_filter))
    if cache_key in _FINDINGS_CACHE:
        return _FINDINGS_CACHE[cache_key]
    rows = service.list_findings(
        eff_window, workspace_ids or None, env or None, attributes, tag=tag_filter,
    )
    out = {
        "window_days": eff_window,
        "workspace_ids": workspace_ids,
        "env": env,
        "attributes": attributes or {},
        "tag": _tag_json(tag_filter),
        "count": len(rows),
        # T-71: the selected workspaces WITH spend no regional table holds a row for (null when
        # dims.dim_workspace could not be read).
        "region_gap": service.region_gap(workspace_ids or None, env or None, attributes),
        "findings": [
            {
                "query_id": r.query_id,
                "title": r.title,
                "domain": r.domain,
                "tier": r.tier,
                "stars": r.stars,
                "origin": r.origin,
                "is_finding": r.is_finding,
                "windowed": r.windowed,
                "window_days": r.window_days,
                "outcome": r.outcome,
                "row_count": r.row_count,
                "status_counts": r.status_counts,
                "has_workspace_id": r.has_workspace_id,
                "regional": r.regional,
                "not_assessed_reason": r.not_assessed_reason,
                # P2-PARTIAL: True when this finding computed despite a degraded name-lookup or
                # a partially-exported source -- never true together with not_assessed_reason.
                "partial": r.partial,
                # P4-T-IDX: None/[]/None (never true/false) unless `tag` was given above --
                # app/web's TagNotApplicableChip (findings_table.tsx) reads tag_applied/
                # tag_chain_label directly off each row.
                "tag_applied": r.tag_applied,
                "tag_chain": r.tag_chain,
                "tag_chain_label": r.tag_chain_label,
                # T-75A (DEC-66.2): [] on every row with no known vendored-library defect --
                # never omitted, so app/web never has to special-case a missing key.
                "library_corrections": service.library_corrections_for(r.query_id),
                # A trend/chargeback report never reads worse than WARN (app_materiality); null
                # for every other check. status_counts above already reflects the cap.
                "severity_cap": app_materiality.severity_cap(r.query_id),
                # null | {code, label, partial} -- the same vocabulary /api/coverage's
                # checks_not_ok names this same gap with (app_core_data.not_assessed_status).
                "not_assessed": (
                    {"code": r.not_assessed["code"], "label": r.not_assessed["label"],
                     "partial": r.not_assessed["partial"]}
                    if r.not_assessed is not None else None
                ),
                # null | {column, kind, usd} and null | {column, noun, flagged, total} -- the
                # findings table's own Possible waste/Other $/Affected columns, no per-row fetch.
                "money": r.money,
                "affected": r.affected,
            }
            for r in rows
        ],
    }
    if len(_FINDINGS_CACHE) >= _FINDINGS_CACHE_MAX:
        _FINDINGS_CACHE.pop(next(iter(_FINDINGS_CACHE)))
    _FINDINGS_CACHE[cache_key] = out
    return out


def warm_findings() -> None:
    """Works out the unfiltered summary for the default window once at start-up, so the first
    page does not wait for it."""
    try:
        _findings_answer(_resolve_window(None, app_config.load_settings()), [], [], None, None)
    except Exception:  # noqa: BLE001 -- the first request computes it again and reports the error
        pass


# Answers that change only with the data or the config, kept until one of them changes.
_ANSWER_CACHE: dict = {}
_ANSWER_CACHE_MAX = 64


def _cached(key: tuple, compute):
    full = (_data_version(), *key)
    if full in _ANSWER_CACHE:
        return _ANSWER_CACHE[full]
    out = compute()
    if len(_ANSWER_CACHE) >= _ANSWER_CACHE_MAX:
        _ANSWER_CACHE.pop(next(iter(_ANSWER_CACHE)))
    _ANSWER_CACHE[full] = out
    return out


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id}
# -------------------------------------------------------------------------------------------


def _clean_header_prose(fields: dict) -> dict:
    """Internal ticket and commit ids never reach the screen; the header text is otherwise as written."""
    out = dict(fields)
    for key in ("read_this", "healthy", "investigate_if"):
        if out.get(key):
            out[key] = guide_api._clean_prose(out[key])  # noqa: SLF001
    out["actions"] = [guide_api._clean_prose(a) for a in out.get("actions") or []]  # noqa: SLF001
    return out


@app.get("/api/finding/{query_id}")
def get_finding(
    request: Request,
    query_id: str,
    window: int | None = Query(default=None),
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    # T-70: the job focus panel's own filter -- scopes a finding to one job server-side, on a
    # finding that carries job_id (app/core/data._build_filters' own contract; ignored, never an
    # error, on one that doesn't). Before this, the panel fetched an unfiltered page and matched
    # job_id in the browser, so a job past that page's own row cap silently read "not flagged".
    job_id: str | None = Query(default=None),
    # T-68: scopes a finding to only these status values server-side, on a finding that carries a
    # status column (same unaffected-if-absent contract). A tile that only needs CRITICAL/WARN
    # rows (e.g. summing a column across flagged rows) asks for exactly those instead of fetching
    # an unfiltered, capped page and filtering by status in the browser -- which can silently drop
    # flagged rows sitting past the cap on a large finding.
    status: list[str] = Query(default=[]),
    # P4-T-IDX: any spelling; the server normalises it. tag_value without tag_key is a 422
    # (_parse_tag_filter); absent tag_value means "any value of this key".
    tag_key: str | None = Query(default=None),
    tag_value: str | None = Query(default=None),
    # Several tag names (repeatable): "<key>" for All, "<key>:<value>" for one value -- same key
    # repeated ORs its values, a different key ANDs across names; merges with tag_key/tag_value.
    tag: list[str] = Query(default=[]),
    # Only these columns in `rows` and `columns` (status, below_floor and workspace_id always
    # kept), so a page that reads a few columns of a wide check gets a small answer.
    columns: list[str] = Query(default=[]),
) -> dict:
    settings = app_config.load_settings()
    requested_window = _resolve_window(window, settings)
    _validate_statuses(status)
    # T-63 (DEC-60): one query param per canonical key (e.g. ?cost_center=engineering&
    # cost_center=sales), read by name since the key set is config-driven -- see
    # _read_attribute_filters above. DEC-60 rule 6: applies only on a finding that carries
    # workspace_id (app/core/data._build_filters' own contract) -- never reduces one that doesn't.
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    tag_filter = _parse_tag_filter(tag_key, tag_value, tag)

    try:
        outcome = service.load_outcome(
            query_id, requested_window, workspace_ids or None, env or None,
            limit=offset + limit, attributes=attributes, job_id=job_id, statuses=status or None,
            tag=tag_filter,
        )
    except service.QueryNotFoundError:
        raise HTTPException(404, f"unknown query_id: {query_id!r}")

    spec = outcome.spec
    body: dict = {
        "query_id": query_id,
        "outcome": outcome.outcome,
        "window_days": outcome.window_days,
        "requested_window_days": requested_window,
        "is_finding": spec.is_finding,
        "windowed": spec.windowed,
        "attributes": attributes or {},
        "tag": _tag_json(tag_filter),
        "scope": outcome.scope,
        "status": status,
        # T-75A (DEC-66.2): present on EVERY outcome (not just ok_rows) -- a NOT_ASSESSED or
        # ERROR finding can still be one this register already knows a reason for.
        "library_corrections": service.library_corrections_for(query_id),
        # P2-PARTIAL: present on EVERY outcome, same reasoning as library_corrections above --
        # app_core_data.finding_status() already ran for this query_id (service.load_outcome
        # calls it unconditionally), these two lists are just never blocking any more. [] is the
        # common case (nothing degraded/partial), never omitted, so app/web never special-cases
        # a missing key.
        "degraded_sources": (outcome.status_info or {}).get("degraded_sources", []),
        "partial_sources": (outcome.status_info or {}).get("partial_sources", []),
        "header": {
            "title": spec.title,
            "domain": spec.domain,
            "tier": spec.tier,
            "stars": spec.stars,
            "origin": spec.origin,
            "confidence": spec.confidence,
            "confidence_note": spec.confidence_note,
            # P4-03: read_this/healthy/investigate_if/actions/not_assessed_reasons carry every
            # :param token replaced by the value actually built for this account, plus its source
            # -- see service.substitute_header_params. outcome.window_days (review fix) is the
            # EFFECTIVE window (7/30/90, 0 for a snapshot-type spec) so a windowed spec's own
            # :period_days token reads the window actually on screen, never a stale default.
            **_clean_header_prose(service.substitute_header_params(spec, outcome.window_days)),
            "next": spec.next,
            "caveats": guide_api._clean_prose(spec.caveats) or spec.caveats,
            "empty_if": spec.empty_if,
        },
    }

    if outcome.outcome == service.OUTCOME_NOT_ASSESSED:
        body["status_info"] = outcome.status_info
        return body

    if outcome.outcome == service.OUTCOME_ERROR:
        body["error"] = outcome.error
        return body

    if outcome.outcome == service.OUTCOME_OK_EMPTY_WINDOW:
        body["window_coverage"] = outcome.window_coverage
        return body

    if outcome.outcome == service.OUTCOME_OK_EMPTY_FILTERS:
        body["rows_in_window"] = outcome.rows_in_window
        # T-70 review round 2: the job focus panel (app/web) needs this to tell a real "not
        # flagged" from a job this query's own build-time :top_n row cap may have cut before its
        # job_id filter ever saw it.
        body["row_cap"] = service.row_cap(query_id)
        # C1: a filter can read as "nothing matches" purely because every candidate row had no
        # resolvable workspace -- this is what tells the two cases apart.
        body["excluded_no_workspace"] = (
            {"rows": outcome.excluded_no_workspace["rows"], "value": None}
            if outcome.excluded_no_workspace is not None else None
        )
        return body

    # OUTCOME_OK_ROWS -- apply the same list-price discount app/ui/panel.py applies before a
    # table is rendered or downloaded (T-57's "reuse app/core/pricing.py" instruction), then page
    # the already-capped frame in Python (never a second SQL OFFSET -- the API layer builds no
    # SQL of its own).
    df = app_pricing.apply(outcome.df, settings["discount_pct"])
    if columns:
        wanted = set(columns) | {"status", "status_raw", "below_floor", "workspace_id"}
        df = df[[c for c in df.columns if c in wanted]]
    page_df = df.iloc[offset : offset + limit]
    kinds = service.classify_columns(list(df.columns), settings["discount_pct"])
    # Materiality floor (config/materiality.yml): app/core/data.read_finding already selected the
    # EFFECTIVE status as "status" (raw kept alongside as status_raw) and a real "below_floor"
    # column -- both land in `rows` below like any other column, no extra Python pass needed.
    rows = service.records(page_df, kinds)
    body.update(
        {
            "rows_total": outcome.rows_total,
            "rows_in_window": outcome.rows_in_window,
            "limit": limit,
            "offset": offset,
            "returned": len(page_df),
            "columns": [{"name": c, **kinds[c]} for c in df.columns],
            "rows": rows,
            # {"column","min","unit","label","rows_below"} or None -- "412 rows under 100 GB not
            # judged" is rows_below out of rows_total above, not just this page.
            "floor": service.floor_json(query_id, list(df.columns), outcome.rows_below_floor),
            # null when no workspace/env/attribute filter applies (or the check is not workspace-
            # filterable), else {rows, value} -- rows the filter left out for having no resolvable
            # workspace (contract C); value is always null here (no aggregation on this endpoint).
            "excluded_no_workspace": (
                {"rows": outcome.excluded_no_workspace["rows"], "value": None}
                if outcome.excluded_no_workspace is not None else None
            ),
            "order_by": app_core_data._model_order_by(query_id),
            "discount_pct": settings["discount_pct"],
            # T-70 review round 2: same row_cap the ok_empty_filters branch above carries, so a
            # job whose OWN row here is present but not flagged still shows a "partial" verdict
            # when other placements/runs of it could be sitting behind this query's own cap.
            "row_cap": service.row_cap(query_id),
            # DEC-64: a finding WITH rows still needs to say whether the snapshot reaches
            # back far enough to fill the window it is labelled with. 7 days of rows under
            # a "30d" heading is the quiet failure this field exists to prevent.
            "window_coverage": outcome.window_coverage,
            # A direct export/import load's own row cap (app.core.data.truncation_info) -- False/
            # None on an ordinary dbt build, which never caps a finding's rows.
            "truncated": (outcome.status_info or {}).get("truncated", False),
            "truncated_max_rows": (outcome.status_info or {}).get("max_rows"),
        }
    )
    return body


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id}/aggregate -- T-68. A server-side GROUP BY/SUM over EVERY row
# matching the filters, so a Cost/ML & AI/Overview tile or chart no longer derives its number from
# summing the (capped) page a plain /api/finding fetch returned. Same four-outcome shape as
# GET /api/finding above (a caller that already knows that shape reads this one for free), an
# OK_ROWS body carrying `groups`/`other`/`total_value` instead of `rows`.
# -------------------------------------------------------------------------------------------


@app.get("/api/finding/{query_id}/aggregate")
def get_finding_aggregate(
    request: Request,
    query_id: str,
    window: int | None = Query(default=None),
    workspace_ids: list[str] = Query(default=[]),
    env: list[str] = Query(default=[]),
    # 0-3 real column names on the finding table (app/core/data.aggregate_finding's own
    # _MAX_GROUP_COLS whitelist-before-splice check -- a bad name raises ValueError -> 422 below).
    group: list[str] = Query(default=[]),
    agg: str = Query(default="sum"),
    value: str | None = Query(default=None),
    status: list[str] = Query(default=[]),
    top: int | None = Query(default=None, ge=1, le=10_000),
    tag_key: str | None = Query(default=None),
    tag_value: str | None = Query(default=None),
    # Several tag names (repeatable): "<key>" for All, "<key>:<value>" for one value -- same key
    # repeated ORs its values, a different key ANDs across names; merges with tag_key/tag_value.
    tag: list[str] = Query(default=[]),
    # Contract D: sums only the rows this check's materiality floor moved from CRITICAL/WARN to
    # OK -- "how much did the floor hide". A check with no floor then matches nothing.
    below_floor: bool = Query(default=False),
) -> dict:
    settings = app_config.load_settings()
    requested_window = _resolve_window(window, settings)
    _validate_statuses(status)
    attributes = _read_attribute_filters(request, _canonical_keys()) or None
    tag_filter = _parse_tag_filter(tag_key, tag_value, tag)

    try:
        outcome = service.load_aggregate(
            query_id, requested_window, group, agg, value,
            workspace_ids=workspace_ids or None, env=env or None, attributes=attributes,
            statuses=status or None, top=top, tag=tag_filter, below_floor=below_floor,
        )
    except service.QueryNotFoundError:
        raise HTTPException(404, f"unknown query_id: {query_id!r}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    body: dict = {
        "query_id": query_id,
        "outcome": outcome.outcome,
        "window_days": outcome.window_days,
        "requested_window_days": requested_window,
        "group": group,
        "agg": agg,
        "value": value,
        "status": status,
        "tag": _tag_json(tag_filter),
        "scope": outcome.scope,
        # T-75A review round 1 (DEC-66.2): present on EVERY outcome, same as GET /api/finding --
        # a tile fed by useFindingAgg (overview_tile.tsx's aggregateGroupTileProps) needs this
        # whether or not the aggregate resolved to rows, so it can show "Library issue"/"Corrected"
        # the same as a tile fed by a plain useFindingData fetch.
        "library_corrections": service.library_corrections_for(query_id),
        # P2-PARTIAL: present on EVERY outcome, same reasoning as GET /api/finding above.
        "degraded_sources": (outcome.status_info or {}).get("degraded_sources", []),
        "partial_sources": (outcome.status_info or {}).get("partial_sources", []),
    }

    if outcome.outcome == service.OUTCOME_NOT_ASSESSED:
        body["status_info"] = outcome.status_info
        return body

    if outcome.outcome == service.OUTCOME_ERROR:
        body["error"] = outcome.error
        return body

    if outcome.outcome == service.OUTCOME_OK_EMPTY_WINDOW:
        body["window_coverage"] = outcome.window_coverage
        return body

    if outcome.outcome == service.OUTCOME_OK_EMPTY_FILTERS:
        body["rows_in_window"] = outcome.rows_in_window
        body["excluded_no_workspace"] = (
            {"rows": outcome.excluded_no_workspace["rows"], "value": None}
            if outcome.excluded_no_workspace is not None else None
        )
        return body

    # OUTCOME_OK_ROWS -- groups/other/total_value are RAW (list-price, undiscounted), same as a
    # plain /api/finding row; `discount_pct` is returned alongside so a money-column caller applies
    # (1 - discount_pct) itself, exactly as it already does for a row page (T-57's "reuse
    # app/core/pricing.py" discipline -- this endpoint does not duplicate that math).
    body.update(
        {
            "groups": [service.aggregate_group_json(group, g) for g in outcome.groups],
            "other": service.aggregate_group_json(group, outcome.other) if outcome.other else None,
            "total_value": service._json_scalar(outcome.total_value),
            "group_count": outcome.group_count,
            "rows_total": outcome.rows_total,
            "matched_rows": outcome.matched_rows,
            "floor": (
                {
                    "min": outcome.floor.min, "unit": outcome.floor.unit,
                    "label": outcome.floor.label, "per_days": 30,
                }
                if outcome.floor is not None else None
            ),
            # null when no workspace/env/attribute filter applies (or the check is not workspace-
            # filterable), else {rows, value} -- rows this filter left out for having no resolvable
            # workspace, and the same agg over them (contract C).
            "excluded_no_workspace": (
                {
                    "rows": outcome.excluded_no_workspace["rows"],
                    "value": service._json_scalar(outcome.excluded_no_workspace["value"]),
                }
                if outcome.excluded_no_workspace is not None else None
            ),
            # T-69B review round 1: how many of matched_rows never entered total_value at all
            # (agg="sum" only, 0 otherwise) -- see app_core_data.AggregateResult's own docstring.
            "null_value_rows": outcome.null_value_rows,
            "rows_in_window": outcome.rows_in_window,
            "discount_pct": settings["discount_pct"],
            "window_coverage": outcome.window_coverage,
            # A direct export/import load's own row cap -- the aggregate only ever summed the
            # rows that made it into the finding table, so a tile must label this a lower bound.
            "truncated": (outcome.status_info or {}).get("truncated", False),
            "truncated_max_rows": (outcome.status_info or {}).get("max_rows"),
        }
    )
    return body


# -------------------------------------------------------------------------------------------
# Static front end (app/web/dist, built from web/) -- registered LAST so no /api/* route is
# shadowed.
# -------------------------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page() -> HTMLResponse:
    # The brand goes into the page itself, so the built-in name never shows before /api/meta.
    try:
        name = app_config.load_settings()["brand"].get("name") or app_config.DEFAULT_SETTINGS["brand"]["name"]
    except app_config.ConfigError:
        name = app_config.DEFAULT_SETTINGS["brand"]["name"]
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="The page is not built. Run: cd web && npm ci && npm run build")
    page = index.read_text(encoding="utf-8")
    script = json.dumps(name).replace("<", "\\u003c")
    page = page.replace(
        "<title>Crosshire</title>",
        f"<title>{html.escape(name)}</title>\n  <script>window.APP_BRAND = {script};</script>", 1,
    )
    # Revalidate the page on every load; the files it names carry their own hash.
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


# Windows can map .js to text/plain in its registry, and browsers refuse a module script sent so.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/woff2", ".woff2")

if (WEB_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str) -> HTMLResponse:
    # Any other page path opens the app; an unknown /api path stays a 404.
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    return index_page()
