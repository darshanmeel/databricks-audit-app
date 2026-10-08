"""app/api/service.py

The four-outcome honesty rule (PLAN.md 6.2), reimplemented against app/core alone -- a standalone
twin of app/ui/data.load(), not a wrapper around it. app/ui/data.py is Streamlit-coupled
(st.cache_data, and it lives under a path T-57 must not touch/depend on while another agent edits
it concurrently), so this module re-derives the same outcome logic straight from app/core/data.py
and app/core/registry.py:

    OUTCOME_OK_ROWS / OUTCOME_OK_EMPTY_WINDOW / OUTCOME_OK_EMPTY_FILTERS / OUTCOME_NOT_ASSESSED /
    OUTCOME_ERROR      the five honesty-rule rows (PLAN.md 6.2's table; OUTCOME_OK_* is the "OK"
                       row split three ways).
    QueryNotFoundError  raised by get_spec() for an id app/core/registry does not know -- the
                        FastAPI layer turns this into a 404, never a 500.
    window_for(spec, window_days)
                        DEC-51: a snapshot-type query (windowed=False) is only ever built at
                        window_days=0; forwarding the caller's own 7/30/90 filter straight through
                        would read it as falsely empty.
    Outcome             the typed result load_outcome()/list_findings() produce.
    load_outcome(query_id, window_days, workspace_ids=None, env=None, limit=None, offset=None,
                 statuses=None)
                        one query_id's full outcome, mirroring app/ui/data.load()'s five-step
                        derivation order exactly (see that module's docstring, copied into this
                        function's own docstring below). `statuses` (T-68) is threaded straight to
                        app/core/data.read_finding/count_finding.
    load_aggregate(query_id, window_days, group, agg, value=None, *, workspace_ids=None,
                    env=None, attributes=None, statuses=None, top=None)
                        T-68: the same four-outcome derivation as load_outcome (reruns it at
                        limit=1, cheap, so the two endpoints can never disagree about NOT_ASSESSED/
                        ERROR/empty-window/empty-filters for the same query_id+filters), but an
                        OK_ROWS result carries app/core/data.aggregate_finding's groups instead of
                        a row page -- the server-side SUM/COUNT this task exists to add.
    list_findings(window_days, workspace_ids=None, env=None, attributes=None, tag=None)
                        one row per executable registry query -- outcome + (for a finding with a
                        status column) a verified CRITICAL/WARN/NOT_ASSESSED/OK breakdown via
                        app/core/data.count_by_status -- the findings table app/web renders.
                        `attributes` (T-63/DEC-60, wired in for P2-FILTERS) is the same
                        {canonical_key: [value, ...]} contract load_outcome already honours --
                        narrows the row/status counts and the outside-region check the same way
                        workspace_ids does, on any query that carries workspace_id. `tag`
                        (P4-T-IDX section 5.5) narrows the same way through
                        app/core/data.finding_window_counts, and each row also carries
                        tag_applied/tag_chain/tag_chain_label.
    classify_columns(columns, discount_pct)
                        {column: {"kind", "label"}} -- "money" (via app.core.pricing's own
                        list-price regex plus its NON_DISCOUNT_MONEY_LABELS own-basis columns, so
                        the two modules can never disagree about which columns are dollars or how
                        they are labelled), "id", or one of the non-money magnitude categories
                        (dbu/pct/hours/gb/gb_bytes) PLAN.md 6.1 documents. A column matching none
                        of those is left out of the dict, exactly like app/ui/style.column_config_for.
    records(df, kinds)  df -> a list of JSON-safe dicts: NaN/NaT -> None, numpy scalars -> native
                        Python, an "id"-kind column always -> str (PLAN.md 6.2 "IDs are strings"),
                        a datetime -> ISO text.
    resolve_params(spec)  P4-03: {param name: (value in effect, source)} for every :param a
                        header declares -- the same thresholds.yml[query_id]/[_all]/header-default
                        precedence dbt/macros/param.sql resolves at build time.
    row_cap(query_id)   T-70 review round 2: the BUILT :top_n row cap for a query_id, via
                        resolve_params() -- or None when it has no :top_n param at all -- mirrors
                        app/ui/drilldown._resolve_param without importing that Streamlit-coupled
                        module. get_finding uses it so the job focus panel (app/web) can tell a
                        real "not flagged"/"OK" from one a build-time row cap might be hiding.
    substitute_params(text, resolved) / substitute_header_params(spec, window_days)
                        P4-03: replace every `:name` token in a header field with the value
                        resolve_params() says is actually built for this account plus a plain
                        source word (e.g. ":warn_coverage_pct" -> "80 (default)"), so a header
                        never shows a raw token the reader cannot resolve. Applied to read_this,
                        healthy, investigate_if, actions and not_assessed_reasons -- the fields a
                        tile or the finding panel actually renders. `window_days` (review fix)
                        overrides `:period_days` with the effective 7/30/90 window in effect,
                        never a stale header default -- see substitute_header_params's own doc.
    T-71 (what the data covers): region_reason(billed), workspace_region(in_snapshot_region,
                        billed_in_snapshot), gap_entries(outside), region_split_or_none(...),
                        outside_only(split), billed_gap(split), region_gap(...) -- a regional
                        finding read for workspaces outside the snapshot's region alone is
                        NOT_ASSESSED, never a clean empty read; Outcome/FindingRow carry the
                        `scope` / `has_workspace_id`/`regional`/`not_assessed_reason` fields a
                        finding with no workspace_id (the filter does not reach it) also needs.

Stdlib + duckdb (for the two store exception classes) + pandas, then app/core/data,
app/core/registry, app/core/pricing, app/core/config.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date as _dt_date
from datetime import datetime as _dt_datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core import config as app_config  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import library_corrections as app_library_corrections  # noqa: E402
from app.core import materiality as app_materiality  # noqa: E402
from app.core import pricing as app_pricing  # noqa: E402
from app.core import registry  # noqa: E402
from app.core import tags as app_core_tags  # noqa: E402  -- P4-T-IDX: TagFilter/tag_models_not_built

OUTCOME_OK_ROWS = "ok_rows"
OUTCOME_OK_EMPTY_WINDOW = "ok_empty_window"
OUTCOME_OK_EMPTY_FILTERS = "ok_empty_filters"
OUTCOME_NOT_ASSESSED = "not_assessed"
OUTCOME_ERROR = "error"

STATUS_ORDER = ["CRITICAL", "WARN", "NOT_ASSESSED", "OK"]
WINDOW_CHOICES = (7, 30, 90)


class QueryNotFoundError(KeyError):
    """query_id does not resolve in app.core.registry -- the API layer's own signal to 404
    rather than let registry.by_id's bare KeyError become an unhandled 500."""


def get_spec(query_id: str) -> registry.QuerySpec:
    try:
        return registry.by_id(query_id)
    except KeyError as exc:
        raise QueryNotFoundError(query_id) from exc


def window_for(spec: registry.QuerySpec, window_days: int) -> int:
    """DEC-51: the window a query must actually be read at -- 0 for a snapshot-type query
    (registry windowed=False), else the caller's own filter."""
    return window_days if spec.windowed else 0


# ---------------------------------------------------------------------------------------------
# resolve_params / row_cap -- T-70 review round 2 gave row_cap this precedence for :top_n alone;
# P4-03 generalises it to every :param a header declares, exactly the way dbt/macros/param.sql
# resolves a param at build time (config/thresholds.yml[query_id][name], then [_all][name], then
# the query header's own default), the same order app/ui/drilldown._resolve_param already uses for
# warn_run_hours -- reimplemented here rather than imported, because drilldown.py is
# Streamlit-coupled and outside app/api's own dependency set.
# ---------------------------------------------------------------------------------------------


def resolve_params(spec: registry.QuerySpec) -> dict[str, tuple[Any, str]]:
    """{param name: (value in effect, source)} for every `:param` this query's header declares.
    `source` is "your setting" when config/thresholds.yml overrides the header default for this
    query_id (checked first) or `_all` (checked second), else "default" -- never a literal guessed
    here. Reruns the thresholds.yml load per call (cheap: a handful of params, one small YAML file
    already cached by app_config)."""
    try:
        thresholds = app_config.load_thresholds()
    except Exception:
        thresholds = {}
    out: dict[str, tuple[Any, str]] = {}
    for param in spec.params or []:
        name = param.get("name")
        if not name:
            continue
        value, source = param.get("default"), "default"
        for scope in (spec.query_id, "_all"):
            override = (thresholds.get(scope) or {}).get(name)
            if isinstance(override, (int, float)) and not isinstance(override, bool):
                value, source = override, "your setting"
                break
        out[name] = (value, source)
    return out


def row_cap(query_id: str) -> int | None:
    """The BUILT :top_n row cap for one query_id, or None when the query has no `top_n` param at
    all (its build carries no row cap -- e.g. lakeflow_job_queue_time) or the id is unknown."""
    try:
        spec = get_spec(query_id)
    except QueryNotFoundError:
        return None
    value, _source = resolve_params(spec).get("top_n", (None, None))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


# ---------------------------------------------------------------------------------------------
# substitute_header_params -- P4-03: a header's read_this/healthy/investigate_if/actions/
# not_assessed_reasons text carries `:name` tokens written once, at query-authoring time, for a
# param whose BUILT value a reader can override in config/thresholds.yml (the Settings page).
# Serving the raw token left every account reading e.g. "covered_pct below :warn_coverage_pct"
# whatever their own thresholds.yml said -- this replaces every token with the value actually
# built for this account, plus a plain word for where that value came from, so the header always
# states the real number in effect.
# ---------------------------------------------------------------------------------------------

_PARAM_TOKEN_RE = re.compile(r":([a-z][a-z0-9_]*)")


def _fmt_param_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def substitute_params(text: str | None, resolved: dict[str, tuple[Any, str]]) -> str | None:
    """Replace every `:name` token this query declares as a param with `"<value> (<source>)"`
    (resolve_params' own value/source pair). A `:name` this query's header does NOT declare as a
    param is left untouched rather than silently eaten -- it is either plain prose that happens to
    start with a colon-letter, or a lint_headers.py bug, never this function's call to guess."""
    if not text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name not in resolved:
            return m.group(0)
        value, source = resolved[name]
        shown = _fmt_param_value(value)
        # A byte threshold reads as MiB/GiB, never a ten-digit number.
        if name.endswith("_bytes") and isinstance(value, (int, float)) and value >= 1024 ** 2:
            shown = "1 GiB" if value == 1024 ** 3 else f"{value / 1024 ** 2:g} MiB"
        return f"{shown} ({source})"

    return _PARAM_TOKEN_RE.sub(_sub, text)


def substitute_header_params(spec: registry.QuerySpec, window_days: int | None = None) -> dict[str, Any]:
    """The header fields a reader actually sees -- read_this, healthy, investigate_if, actions and
    not_assessed_reasons -- with every `:param` token replaced by resolve_params()'s value/source
    (P4-03). caveats and confidence_note are deliberately left alone: they are reference text, not
    part of a tile or the panel's headline guidance.

    Review fix: `:period_days` is not a thresholds.yml setting like the others -- at build time
    tools/generate_models.py maps it straight to the window the reader picked (7/30/90, `__W__`),
    never to a header default or a thresholds.yml override (dbt/macros/param.sql never resolves
    it either). resolve_params() alone therefore always reads "30 (default)" regardless of which
    window is on screen. When the caller passes the effective `window_days` for a windowed spec,
    it overrides resolve_params()'s own period_days entry with the value actually in effect on
    screen, sourced as "the window you picked" -- never the stale header-default/thresholds value.

    Review fix: when a direct export never ran the full `window_days` (exported_window_coverage()
    covers it only partially -- a 10-day export read at 30d), the token instead carries the days
    actually covered, sourced as "the export covered N of the M days you picked", so the header
    text never claims a fuller query than what actually ran.
    """
    resolved = resolve_params(spec)
    if window_days is not None and spec.windowed and "period_days" in resolved:
        coverage = app_core_data.exported_window_coverage()
        covered = coverage.get(window_days) if coverage else None
        if covered is not None and covered < window_days:
            resolved["period_days"] = (
                covered, f"the export covered {covered} of the {window_days} days you picked",
            )
        else:
            resolved["period_days"] = (window_days, "the window you picked")
    return {
        "read_this": substitute_params(spec.read_this, resolved),
        "healthy": substitute_params(spec.healthy, resolved),
        "investigate_if": substitute_params(spec.investigate_if, resolved),
        "actions": [substitute_params(a, resolved) for a in (spec.actions or [])],
        "not_assessed_reasons": {
            code: substitute_params(words, resolved)
            for code, words in (spec.not_assessed_reasons or {}).items()
        },
        # the values in effect, so a page draws a check's own line instead of a hard-coded default
        "params": {name: {"value": value, "source": source} for name, (value, source) in resolved.items()},
    }


def _not_built_reason(query_id: str) -> str:
    # Plain words on screen: a direct export only lacks a check that is newer than the export.
    if app_core_data.direct_export_info() is not None:
        return "Not in this export yet: this check is newer than the export. The next export includes it."
    return "Not built yet: this check is newer than the local build. Run the local build to add it."


def _not_exported_reason(eff_window: int, coverage: dict[int, int] | None) -> str | None:
    """None unless this is a direct export whose own window_coverage never ran eff_window at all
    -- a plain reason ("the export covered N days; the <w>-day window was not exported"), never
    read as ok_empty_window's "nothing in this window". An ordinary dbt/snapshot build, or a
    direct export whose manifest predates window_coverage, has no opinion here. `coverage` is
    load_outcome's own exported_window_coverage() read, passed in rather than re-fetched."""
    if coverage is None or eff_window in coverage:
        return None
    covered = max(coverage.values())
    return f"the export covered {covered} days; the {eff_window}-day window was not exported"


# ---------------------------------------------------------------------------------------------
# T-71: what the data covers. tools/snapshot.py connects through ONE workspace, so every
# regional system table holds rows only for the workspaces of that metastore's region, while
# billing and workspaces_latest are account-wide (app/core/data.GLOBAL_SOURCES).
# dims.dim_workspace.in_snapshot_region is FALSE for a workspace no regional table has a row for.
#   - A regional finding that carries workspace_id, read with a filter that selects ONLY such
#     workspaces, is NOT_ASSESSED (reason OUTSIDE_REGION) whenever it has no row for them --
#     never ok_empty_window / ok_empty_filters, which read as "nothing wrong". Rows it does
#     return are kept.
#   - Otherwise the outcome stands, and the selected outside workspaces WITH spend are the
#     finding's `region_gap`: spend on screen whose regional checks the snapshot cannot see.
# ---------------------------------------------------------------------------------------------

OUTSIDE_REGION = "outside_region"
OUTSIDE_REGION_LABEL = "outside the snapshot's region"
REGION_IN = "in_region"
REGION_UNKNOWN = "unknown"
NA_SOURCE_NOT_OK = "source_not_ok"
NA_MODEL_FAILED = "model_failed"
NA_NOT_BUILT = "not_built"
# A direct export's own window_coverage never ran this window at all (as opposed to running it
# partially) -- e.g. a 10-day export asked for the 90-day window. Never "nothing in this window".
NA_NOT_EXPORTED = "not_exported"
# P4-T-IDX: list_findings' own short code for a row whose chain needs tags.tag_entity when this
# batch already knows the tag models were never built -- the bulk-list twin of load_outcome's
# TagModelsNotBuiltError -> NOT_ASSESSED handling (never a silently-unfiltered count).
NA_TAG_MODELS_NOT_BUILT = "tag_models_not_built"
_REGIONAL_TABLES_TEXT = "compute, lakeflow, query, serving, storage, access audit"
REGION_UNKNOWN_REASON = (
    "the snapshot holds no row in any regional system table (not exported, not granted or "
    "empty), so no workspace can be placed inside or outside its region"
)


def region_reason(billed: bool) -> str:
    """Why a workspace is outside the snapshot's region -- shown verbatim by the UI."""
    if billed:
        return (
            "billed in system.billing.usage, but none of the snapshot's regional system tables "
            f"({_REGIONAL_TABLES_TEXT}) has a row for it: most likely it belongs to another "
            "region's metastore, which this snapshot did not connect through, or the regional "
            "schemas it uses were not exported or not enabled (see Coverage)"
        )
    return (
        f"none of the snapshot's regional system tables ({_REGIONAL_TABLES_TEXT}) has a row for "
        "it and it has no billed usage in the snapshot, so nothing places it in the snapshot's "
        "region"
    )


def workspace_region(in_snapshot_region, billed_in_snapshot) -> tuple[str, str | None]:
    """(region_status, region_reason) for one dim_workspace row -- /api/workspaces."""
    if in_snapshot_region is None:
        return REGION_UNKNOWN, REGION_UNKNOWN_REASON
    if in_snapshot_region:
        return REGION_IN, None
    return OUTSIDE_REGION, region_reason(bool(billed_in_snapshot))


def gap_entries(outside: list[dict]) -> list[dict]:
    return [
        {"workspace_id": w["workspace_id"], "name": w["name"], "billed": w["billed"],
         "reason": region_reason(w["billed"])}
        for w in outside
    ]


def region_split_or_none(
    workspace_ids: list[str] | None = None,
    env: list[str] | None = None,
    attributes: dict[str, list[str]] | None = None,
) -> dict | None:
    """app/core/data.region_split, or None when dims.dim_workspace cannot be read (a locked or
    corrupt db, a failed dims build): region facts are then unknown, and nothing is forced."""
    try:
        return app_core_data.region_split(workspace_ids, env, attributes)
    except (duckdb.Error, OSError):
        return None


def outside_only(split: dict | None) -> bool:
    """True when a workspace-level filter is active, matches at least one workspace, and every
    workspace it matches is outside the snapshot's region."""
    return bool(
        split is not None
        and split["filtered"]
        and split["selected"]
        and len(split["outside"]) == len(split["selected"])
    )


def billed_gap(split: dict | None) -> list[dict] | None:
    if split is None:
        return None
    return gap_entries([w for w in split["outside"] if w["billed"]])


def region_gap(
    workspace_ids: list[str] | None = None,
    env: list[str] | None = None,
    attributes: dict[str, list[str]] | None = None,
) -> list[dict] | None:
    """The selected workspaces WITH spend that no regional table holds a row for (all
    workspaces when unfiltered). None when dims.dim_workspace cannot be read."""
    return billed_gap(region_split_or_none(workspace_ids, env, attributes))


def _outside_region_info(status_info: dict, split: dict) -> dict:
    info = dict(status_info)
    info["outside_region"] = {"label": OUTSIDE_REGION_LABEL, "workspaces": gap_entries(split["outside"])}
    return info


@dataclass
class Outcome:
    """One query_id's fully-derived outcome -- the FastAPI layer switches on `outcome` and
    serialises exactly the fields that outcome names, never a generic 'no data' response."""

    outcome: str
    spec: registry.QuerySpec
    window_days: int
    status_info: dict | None = None
    window_coverage: dict | None = None
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    rows_total: int = 0          # rows matching window + every filter (read_finding's own count)
    rows_in_window: int = 0      # rows matching window alone, filters ignored
    error: str | None = None
    scope: dict | None = None    # T-71: {"has_workspace_id", "regional", "region_gap"}
    rows_below_floor: int = 0    # materiality floor: how many of rows_total (app_core_data's own
                                  # read_finding.rows_below_floor) -- 0 when this query has none
    excluded_no_workspace: dict | None = None  # contract C: app_core_data.FindingResult's own field


def _scope_tag(
    tag: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None", applied: bool | None, chain_words: list | None,
    chain_label: str | None, reason: str | None,
) -> dict | None:
    """The `scope.tag` object of section 4.5, or None when no tag filter was requested at all."""
    if tag is None:
        return None
    return {"applied": bool(applied), "chain": list(chain_words or []), "chain_label": chain_label, "reason": reason}


def _scope_tag_from_applicability(tag: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None", applicability: dict) -> dict | None:
    return _scope_tag(
        tag, applicability.get("tag_applied"), applicability.get("tag_chain"),
        applicability.get("tag_chain_label"), applicability.get("tag_not_applicable_reason"),
    )


_TAG_MODELS_NOT_BUILT_REASON = "tag_models_not_built: the tag index was not built; rebuild to filter by tag"


def load_outcome(
    query_id: str,
    window_days: int,
    workspace_ids: list[str] | None = None,
    env: list[str] | None = None,
    limit: int | None = None,
    attributes: dict[str, list[str]] | None = None,
    job_id: str | None = None,
    statuses: list[str] | None = None,
    tag: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> Outcome:
    """Derive the honesty-rule outcome for one query_id, in the same order as app/ui/data.load()
    (PLAN.md 6.2):

    1. NOT_ASSESSED first: the model errored/was skipped/was never built (run_results.json), one
       of its own sources is not "ok" in the snapshot manifest (finding_status's own
       blocking_sources), or (review fix: same order list_findings already used) a direct export
       never ran this window at all.
    2. A local read failure while counting/reading rows (findings.f_<qid> missing at the
       filesystem/DuckDB level despite run_results claiming success) also lands on NOT_ASSESSED
       via FindingNotBuiltError; any other DuckDB/OS-level failure (a locked file, a corrupt db)
       is ERROR instead.
    3. OK, empty in window: rows_in_window (window alone, no other filter) is 0.
    4. OK, empty by filters: rows_in_window > 0 but the caller's own filters exclude every row.
    5. OK, rows: otherwise.

    `attributes` ({canonical_key: [value, ...]}, T-63/DEC-60) applies the same way env does --
    only on a finding that carries workspace_id (DEC-60 rule 6, app/core/data._build_filters'
    own contract); it never reduces a finding with none.

    `job_id` (T-70, the job focus panel) scopes to one job server-side, the same way -- applies
    only on a finding that carries job_id, never reduces one that doesn't. A finding whose window
    has rows but none for this job_id (and every other filter) reads OUTCOME_OK_EMPTY_FILTERS,
    same as any other filter that excludes every row.

    `statuses` (T-68) scopes to those status values server-side, same contract again -- applies
    only on a finding that carries a status column. Lets a tile that needs only CRITICAL/WARN rows
    (e.g. "sum this column across flagged rows") ask the store for exactly those instead of
    fetching an unfiltered, capped page and filtering by status in the browser, which can silently
    drop flagged rows sitting past the cap on a large finding.

    Raises QueryNotFoundError for an unknown query_id (the FastAPI layer's own job to 404).

    T-71: every Outcome carries `scope` = {"has_workspace_id": bool | None (None when the table
    was not read), "regional": bool (app/core/data.spec_is_regional), "region_gap": [entry, ...] |
    None (the selected workspaces WITH spend outside the snapshot's region; None unless the
    finding is regional AND carries workspace_id, or when dims.dim_workspace could not be read)}.
    Steps 3 and 4 become NOT_ASSESSED, with status_info["outside_region"], when the filter selects
    only workspaces outside the snapshot's region and the finding is regional with a workspace_id
    column: an empty read for those workspaces means "could not look", never "nothing there"."""
    spec = get_spec(query_id)
    eff_window = window_for(spec, window_days)
    status_info = app_core_data.finding_status(query_id)
    # Row-cap info from a direct export/import load (app.core.data.truncation_info); {} on an
    # ordinary dbt build, which never caps a finding's rows.
    status_info.update(app_core_data.truncation_info(query_id, eff_window))
    # One read for the whole call -- _not_exported_reason and window_coverage() below both used
    # to re-fetch this themselves (review fix: a second DB connection for the same row).
    exported_coverage = app_core_data.exported_window_coverage()
    scope: dict = {
        "has_workspace_id": None,
        "regional": app_core_data.spec_is_regional(spec),
        "region_gap": None,
        "tag": None,  # P4-T-IDX: filled in once read_finding's applicability is known (below)
    }

    # "not_built" is deliberately NOT treated as bad here. It only means run_results.json has no
    # entry for this model -- which is also true after a selective `dbt build --select ...`, and
    # after any zip extraction that left dbt/target/ behind. It is not evidence the table is
    # missing. If the table really is absent, the count_finding call below raises
    # FindingNotBuiltError and we report NOT_ASSESSED from that, which is evidence. Only an
    # explicit error/skipped verdict, or a source the manifest positively says is not ok, counts.
    model_or_source_bad = status_info["status"] in ("error", "skipped") or bool(
        status_info["blocking_sources"]
    )
    if model_or_source_bad:
        return Outcome(OUTCOME_NOT_ASSESSED, spec, eff_window, status_info=status_info, scope=scope)

    # Review fix: checked after model/source-bad, same order list_findings already used -- a
    # check that failed on Databricks must read model_failed everywhere, never not_exported here
    # and model_failed there for the same query_id.
    if spec.windowed:
        not_exported = _not_exported_reason(eff_window, exported_coverage)
        if not_exported is not None:
            info = dict(status_info)
            info["not_exported_reason"] = not_exported
            # NotAssessedCard (app/web) renders not_built_reason, not not_exported_reason yet --
            # same sentence in both keys so the card shows the real reason, not a rebuild prompt.
            info["not_built_reason"] = not_exported
            return Outcome(OUTCOME_NOT_ASSESSED, spec, eff_window, status_info=info, scope=scope)

    try:
        has_workspace_id = app_core_data.workspace_filterable(app_core_data.finding_columns(query_id))
        rows_in_window = app_core_data.count_finding(query_id, eff_window)
    except app_core_data.FindingNotBuiltError:
        info = dict(status_info)
        info["not_built_reason"] = _not_built_reason(query_id)
        return Outcome(OUTCOME_NOT_ASSESSED, spec, eff_window, status_info=info, scope=scope)
    except (duckdb.Error, OSError) as exc:
        return Outcome(
            OUTCOME_ERROR, spec, eff_window, status_info=status_info, error=str(exc), scope=scope
        )

    scope["has_workspace_id"] = has_workspace_id
    split = None
    if scope["regional"] and has_workspace_id:
        split = region_split_or_none(workspace_ids, env, attributes)
        scope["region_gap"] = billed_gap(split)
    blocked = outside_only(split)

    if rows_in_window == 0:
        if blocked:
            return Outcome(
                OUTCOME_NOT_ASSESSED, spec, eff_window,
                status_info=_outside_region_info(status_info, split), scope=scope,
            )
        try:
            coverage = app_core_data.window_coverage(query_id, eff_window, exported_coverage)
        except Exception:
            coverage = None
        return Outcome(
            OUTCOME_OK_EMPTY_WINDOW, spec, eff_window, status_info=status_info,
            window_coverage=coverage, scope=scope,
        )

    try:
        result = app_core_data.read_finding(
            query_id, eff_window, workspace_ids, env, None, limit, attributes=attributes,
            job_id=job_id, statuses=statuses, tag_filter=tag,
        )
    except app_core_tags.TagModelsNotBuiltError:
        # Section 5.5: never return unfiltered rows under an active filter -- NOT_ASSESSED with a
        # plain reason instead (DEC-57/58: never read "we could not check" as "untagged").
        info = dict(status_info)
        info["not_built_reason"] = _TAG_MODELS_NOT_BUILT_REASON
        return Outcome(OUTCOME_NOT_ASSESSED, spec, eff_window, status_info=info, scope=scope)
    except app_core_data.FindingNotBuiltError:
        info = dict(status_info)
        info["not_built_reason"] = _not_built_reason(query_id)
        return Outcome(OUTCOME_NOT_ASSESSED, spec, eff_window, status_info=info, scope=scope)
    except (duckdb.Error, OSError) as exc:
        return Outcome(
            OUTCOME_ERROR, spec, eff_window, status_info=status_info, error=str(exc), scope=scope
        )

    scope["tag"] = _scope_tag_from_applicability(tag, result.applicability)

    if result.rows_total == 0:
        if blocked:
            return Outcome(
                OUTCOME_NOT_ASSESSED, spec, eff_window,
                status_info=_outside_region_info(status_info, split),
                rows_in_window=rows_in_window, scope=scope,
            )
        return Outcome(
            OUTCOME_OK_EMPTY_FILTERS,
            spec,
            eff_window,
            status_info=status_info,
            rows_in_window=rows_in_window,
            scope=scope,
            excluded_no_workspace=result.excluded_no_workspace,
        )

    # DEC-62/DEC-64: coverage is attached to a finding WITH rows too, not only to an empty one.
    # A 7-day snapshot read at a 30-day window returns plenty of rows -- it just returns 7 days of
    # them, under a "30d" label. That is the case where a partial window is most dangerous and
    # was exactly the case that carried no coverage at all.
    try:
        coverage = app_core_data.window_coverage(query_id, eff_window, exported_coverage)
    except Exception:
        coverage = None

    return Outcome(
        OUTCOME_OK_ROWS,
        spec,
        eff_window,
        status_info=status_info,
        df=result.df,
        rows_total=result.rows_total,
        rows_in_window=rows_in_window,
        window_coverage=coverage,
        scope=scope,
        rows_below_floor=result.rows_below_floor,
        excluded_no_workspace=result.excluded_no_workspace,
    )


# ---------------------------------------------------------------------------------------------
# load_aggregate -- T-68: the server-side SUM/COUNT this task exists to add. A tile/chart that
# used to derive its number by summing the (capped) row page a plain /api/finding fetch returned
# now asks for the real total instead -- app/core/data.aggregate_finding's GROUP BY scans every
# matching row, not a `ui.max_rows` slice.
# ---------------------------------------------------------------------------------------------


@dataclass
class AggregateOutcome:
    """Same outcome vocabulary as Outcome above (the FastAPI layer switches on `outcome` the same
    way for both endpoints) -- an OK_ROWS result additionally carries `groups`/`other`/
    `total_value`/`matched_rows` instead of a row page."""

    outcome: str
    spec: registry.QuerySpec
    window_days: int
    status_info: dict | None = None
    window_coverage: dict | None = None
    rows_in_window: int = 0
    rows_total: int = 0
    matched_rows: int = 0
    group_count: int = 0
    groups: list = field(default_factory=list)   # list[app_core_data.AggregateGroup]
    other: Any = None                              # app_core_data.AggregateGroup | None
    total_value: Any = None
    null_value_rows: int = 0   # T-69B review round 1: app_core_data.AggregateResult.null_value_rows
    error: str | None = None
    scope: dict | None = None    # P4-T-IDX: scope.tag, mirroring Outcome.scope
    floor: "app_materiality.Floor | None" = None   # this check's configured floor, or None
    excluded_no_workspace: dict | None = None  # contract C: app_core_data.AggregateResult's own field


def load_aggregate(
    query_id: str,
    window_days: int,
    group: list[str],
    agg: str,
    value: str | None = None,
    *,
    workspace_ids: list[str] | None = None,
    env: list[str] | None = None,
    attributes: dict[str, list[str]] | None = None,
    statuses: list[str] | None = None,
    top: int | None = None,
    tag: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
    below_floor: bool = False,
) -> AggregateOutcome:
    """Derive the honesty-rule outcome exactly as load_outcome does (reruns it at limit=1 -- cheap,
    and it is the same tested branch order, so an aggregate and a plain finding read for the same
    query_id+filters can never disagree about NOT_ASSESSED/ERROR/empty-window/empty-filters), then
    for OUTCOME_OK_ROWS runs app_core_data.aggregate_finding for the real numbers.

    A bad `group`/`agg`/`value` raises ValueError (app_core_data.aggregate_finding's own
    validation) -- the FastAPI layer turns that into a 422, same as an unknown query_id (via
    QueryNotFoundError, raised by the load_outcome() call below) turns into a 404.

    `tag` (P4-T-IDX) follows load_outcome's own `tag` contract exactly, including
    TagModelsNotBuiltError -> NOT_ASSESSED (via the `base` call below) -- aggregate_finding must
    follow the filter (section 5.5's "aggregate_finding currently passes None, and that is
    fixed")."""
    base = load_outcome(
        query_id, window_days, workspace_ids, env, limit=1, attributes=attributes, statuses=statuses,
        tag=tag,
    )
    if base.outcome != OUTCOME_OK_ROWS:
        return AggregateOutcome(
            base.outcome, base.spec, base.window_days,
            status_info=base.status_info, window_coverage=base.window_coverage,
            rows_in_window=base.rows_in_window, rows_total=base.rows_total, error=base.error,
            scope=base.scope, excluded_no_workspace=base.excluded_no_workspace,
        )

    try:
        agg_result = app_core_data.aggregate_finding(
            query_id, base.window_days, group, agg, value,
            workspace_ids=workspace_ids, env=env, attributes=attributes,
            statuses=statuses, top=top, tag_filter=tag, below_floor=below_floor,
        )
    except app_core_tags.TagModelsNotBuiltError:
        info = dict(base.status_info or {})
        info["not_built_reason"] = _TAG_MODELS_NOT_BUILT_REASON
        return AggregateOutcome(OUTCOME_NOT_ASSESSED, base.spec, base.window_days, status_info=info)
    except (duckdb.Error, OSError) as exc:
        return AggregateOutcome(
            OUTCOME_ERROR, base.spec, base.window_days, status_info=base.status_info, error=str(exc)
        )

    return AggregateOutcome(
        OUTCOME_OK_ROWS, base.spec, base.window_days,
        status_info=base.status_info, window_coverage=base.window_coverage,
        rows_in_window=base.rows_in_window, rows_total=agg_result.rows_total,
        matched_rows=agg_result.matched_rows, group_count=agg_result.group_count,
        groups=agg_result.groups, other=agg_result.other, total_value=agg_result.total_value,
        null_value_rows=agg_result.null_value_rows, scope=base.scope, floor=agg_result.floor,
        excluded_no_workspace=agg_result.excluded_no_workspace,
    )


def aggregate_group_json(group_cols: list[str], g) -> dict[str, Any]:
    """One app_core_data.AggregateGroup -> a JSON-safe dict. A group column matching the "id"
    heuristic (_ID_RE, the same regex classify_columns() uses) renders its key value as a string,
    same "IDs are strings" rule records() applies to a row page (PLAN.md 6.2) -- so a bigint
    workspace_id/job_id group key never loses precision to float/scientific-notation JSON
    encoding. `g.key` is None for the folded "other" bucket (no single group it stands for).
    `null_rows` (T-69B review round 1) is 0 for every agg but "sum" -- see AggregateGroup's own
    docstring for what it counts."""
    key = None
    if g.key is not None:
        key = []
        for col, val in zip(group_cols, g.key):
            if _ID_RE.search(col):
                key.append(None if _json_scalar(val) is None else str(val))
            else:
                key.append(_json_scalar(val))
    return {"key": key, "value": _json_scalar(g.value), "row_count": g.row_count, "null_rows": g.null_rows}


# ---------------------------------------------------------------------------------------------
# list_findings -- the findings table's one row per query_id.
# ---------------------------------------------------------------------------------------------


@dataclass
class FindingRow:
    query_id: str
    title: str
    domain: str
    tier: str
    stars: bool
    origin: str
    is_finding: bool
    windowed: bool
    window_days: int
    outcome: str
    row_count: int
    status_counts: dict[str, int] | None
    has_workspace_id: bool | None = None     # T-71: None when the table was not read
    regional: bool = False                   # T-71: reads at least one regional system table
    not_assessed_reason: str | None = None   # T-71: set only when outcome is not_assessed
    partial: bool = False                    # P2-PARTIAL: a degraded/partial source, never set
                                              # together with not_assessed_reason -- see list_findings
    # P4-T-IDX (section 5.5/5.8 item 7): None (never set) unless `tag` was given to list_findings.
    # Mirrors app/core/data.TagApplication -- app/web's TagNotApplicableChip (findings_table.tsx)
    # already reads these three fields off each row.
    tag_applied: bool | None = None
    tag_chain: list = field(default_factory=list)
    tag_chain_label: str | None = None
    # null | {code, label, partial} -- app_core_data.not_assessed_status()'s own vocabulary, the
    # same one /api/coverage's checks_not_ok builds from, so the two screens name one gap one way.
    not_assessed: dict | None = None
    # null | {column, kind, usd} -- discounted SUM of the check's headline money column over
    # CRITICAL/WARN rows (contract B). null | {column, noun, flagged, total} for `affected`.
    money: dict | None = None
    affected: dict | None = None


def list_findings(
    window_days: int,
    workspace_ids: list[str] | None = None,
    env: list[str] | None = None,
    attributes: dict[str, list[str]] | None = None,
    tag: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> list[FindingRow]:
    """One row per EXECUTABLE registry query (both vendored + app-owned, every domain -- no
    curated subset, mirroring PLAN.md 6.3's Domains page: "every query in the registry"), each
    carrying its own honesty-rule outcome and, for a query with a status band, a verified
    CRITICAL/WARN/NOT_ASSESSED/OK breakdown.

    NOT_ASSESSED is still decided per-query from finding_status() (run_results.json + the
    snapshot manifest -- no DuckDB connection needed, DEC-27's JSON-only reads), but every OK-path
    count/breakdown for every remaining query is read through ONE
    app.core.data.finding_window_counts() batch call -- a single DuckDB connection for the whole
    page instead of one per query (T-57's own hand-off note: a per-query loop over every finding table
    measured over 90s wall-clock on this checkout; the batched form is the fix).

    T-71: each row also carries has_workspace_id, regional and not_assessed_reason
    (NA_SOURCE_NOT_OK, NA_MODEL_FAILED, NA_NOT_BUILT or OUTSIDE_REGION; None unless the outcome is
    not_assessed). The outside-only rule is load_outcome's, applied to the bulk counts.

    P2-PARTIAL: `partial` is True (never together with not_assessed_reason) when
    finding_status() reported a degraded name-lookup source or a partially-exported source for
    this query -- neither one blocks the finding any more (app_core_data.finding_status's own
    contract), but the row still says so rather than looking indistinguishable from a clean one.

    `attributes` (P2-FILTERS) reaches both the bulk row/status counts (finding_window_counts) and
    the outside-region check (region_split_or_none) -- the same two places workspace_ids/env
    already reach -- so selecting a cost centre narrows this table exactly the way selecting a
    workspace does, for every query that carries workspace_id.

    `tag` (P4-T-IDX section 5.5) reaches finding_window_counts the same way, so a tag filter
    narrows this table's own row/status counts exactly the way it already narrows a single
    finding's read/count/aggregate. Every row also carries tag_applied/tag_chain/tag_chain_label
    (None/[]/None when `tag` is None) -- the bulk twin of scope.tag on a single finding. A
    query_id whose chain needs tags.tag_entity when that table was never built is NOT_ASSESSED
    with reason NA_TAG_MODELS_NOT_BUILT, never a silently-unfiltered count (the same rule
    load_outcome's own TagModelsNotBuiltError handling already applies to a single finding)."""
    specs = [s for s in registry.load_registry() if s.executable]
    eff_windows = {s.query_id: window_for(s, window_days) for s in specs}
    exported_coverage = app_core_data.exported_window_coverage()  # None off a dbt/snapshot build

    bad_reasons: dict[str, str] = {}
    partial_flags: dict[str, bool] = {}
    # T-89 (F6): read run_results.json + the snapshot manifest ONCE for this whole request and
    # pass them into every finding_status() call below, instead of each of the ~122 calls
    # re-reading and re-parsing both files itself (5s of a 6.45s profile; 8-18s per /api/findings
    # over HTTP). No caching across requests -- DEC-27's read-fresh-on-every-request rule still
    # holds, this is only sharing one read across the many query_ids ONE request already needs.
    rr = app_core_data.run_results()
    mf = app_core_data.snapshot_manifest()
    for s in specs:
        info = app_core_data.finding_status(s.query_id, rr, mf)
        # Same rule as load_outcome: "not_built" only means run_results.json has no entry for
        # this model, which is equally true after a selective build or a zip extraction that left
        # dbt/target/ behind. It is not evidence the table is missing -- the bulk count below
        # decides that, and a genuinely absent table falls through to the `info is None` branch.
        if info["blocking_sources"]:
            bad_reasons[s.query_id] = NA_SOURCE_NOT_OK
        elif info["status"] in ("error", "skipped"):
            bad_reasons[s.query_id] = NA_MODEL_FAILED
        elif s.windowed and exported_coverage is not None and eff_windows[s.query_id] not in exported_coverage:
            bad_reasons[s.query_id] = NA_NOT_EXPORTED
        elif info["degraded_sources"] or info["partial_sources"]:
            partial_flags[s.query_id] = True
        # Review fix: a window the export ran but not in full (e.g. 10 of 30 days) is `partial`,
        # never `not_exported` (the branch above, for a window never run at all) and never
        # indistinguishable from a clean full-window read.
        eff = eff_windows[s.query_id]
        if (
            s.query_id not in bad_reasons and s.windowed and exported_coverage is not None
            and exported_coverage.get(eff, eff) < eff
        ):
            partial_flags[s.query_id] = True

    pairs = [(s.query_id, eff_windows[s.query_id]) for s in specs if s.query_id not in bad_reasons]

    bulk: dict[str, dict] = {}
    bulk_error: str | None = None
    if pairs:
        try:
            bulk = app_core_data.finding_window_counts(
                pairs, workspace_ids, env, attributes, tag_filter=tag,
            )
        except (duckdb.Error, OSError) as exc:
            bulk_error = str(exc)

    # not_assessed_status()'s own verdict per spec (contract B's `not_assessed`) -- the SAME
    # function /api/coverage's checks_not_ok names a gap with. table_exists comes from the bulk
    # count above, not run_results alone -- a query_id findings.f_<id> genuinely carries (built
    # outside dbt, e.g. a direct export) must never read not_built just because run_results.json
    # has no entry for it. The row-building loop below layers outside_region/all_rows_not_assessed
    # on top where those need live data this pass does not have.
    na_status: dict[str, dict | None] = {
        s.query_id: app_core_data.not_assessed_status(
            s, rr, mf, window_days=eff_windows[s.query_id], exported_coverage=exported_coverage,
            table_exists=(s.query_id in bulk) if bulk_error is None else None,
        )
        for s in specs
    }

    blocked_selection = outside_only(region_split_or_none(workspace_ids, env, attributes))

    rows: list[FindingRow] = []
    for s in specs:
        eff_window = eff_windows[s.query_id]
        regional = app_core_data.spec_is_regional(s)
        base = (s.query_id, s.title, s.domain, s.tier, s.stars, s.origin, s.is_finding, s.windowed, eff_window)

        if s.query_id in bad_reasons:
            rows.append(
                FindingRow(
                    *base, OUTCOME_NOT_ASSESSED, 0, None, None, regional, bad_reasons[s.query_id],
                    not_assessed=na_status.get(s.query_id),
                )
            )
            continue
        if bulk_error is not None:
            rows.append(FindingRow(*base, OUTCOME_ERROR, 0, None, None, regional, None))
            continue
        info = bulk.get(s.query_id)
        if info is None:
            # findings.f_<id> does not exist despite a clean run_results/manifest read (a stale
            # run_results.json, or a build in progress) -- FindingNotBuiltError's own contract.
            rows.append(
                FindingRow(
                    *base, OUTCOME_NOT_ASSESSED, 0, None, None, regional, NA_NOT_BUILT,
                    not_assessed=na_status.get(s.query_id),
                )
            )
            continue
        has_ws = info["has_workspace_id"]
        if tag is not None and info.get("tag_models_not_built"):
            rows.append(
                FindingRow(*base, OUTCOME_NOT_ASSESSED, 0, None, has_ws, regional, NA_TAG_MODELS_NOT_BUILT)
            )
            continue
        tag_kwargs = (
            {
                "tag_applied": info.get("tag_applied"),
                "tag_chain": info.get("tag_chain") or [],
                "tag_chain_label": info.get("tag_chain_label"),
            }
            if tag is not None
            else {}
        )
        if info["rows_total"] == 0 and blocked_selection and regional and has_ws:
            rows.append(
                FindingRow(
                    *base, OUTCOME_NOT_ASSESSED, 0, None, has_ws, regional, OUTSIDE_REGION,
                    not_assessed=app_core_data.not_assessed_status(
                        s, rr, mf, window_days=eff_window, exported_coverage=exported_coverage,
                        outside_region=True,
                    ),
                )
            )
            continue
        partial = partial_flags.get(s.query_id, False)
        if info["rows_in_window"] == 0:
            rows.append(
                FindingRow(
                    *base, OUTCOME_OK_EMPTY_WINDOW, 0, None, has_ws, regional, None, partial,
                    **tag_kwargs, not_assessed=na_status.get(s.query_id),
                )
            )
            continue
        if info["rows_total"] == 0:
            rows.append(
                FindingRow(
                    *base, OUTCOME_OK_EMPTY_FILTERS, 0, None, has_ws, regional, None, partial,
                    **tag_kwargs, not_assessed=na_status.get(s.query_id),
                )
            )
            continue
        status_counts = info["status_counts"] if s.is_finding else None
        all_na = bool(status_counts) and set(status_counts) == {"NOT_ASSESSED"}
        na = (
            app_core_data.not_assessed_status(
                s, rr, mf, window_days=eff_window, exported_coverage=exported_coverage,
                all_rows_not_assessed=True, table_exists=True,
            )
            if all_na else na_status.get(s.query_id)
        )
        rows.append(
            FindingRow(
                *base, OUTCOME_OK_ROWS, info["rows_total"], status_counts, has_ws, regional, None, partial,
                **tag_kwargs, not_assessed=na, money=info.get("money"), affected=info.get("affected"),
            )
        )
    return rows


# ---------------------------------------------------------------------------------------------
# Column classification + JSON-safe row serialisation.
# ---------------------------------------------------------------------------------------------

# PLAN.md 6.1's own regex (app/ui/catalog.MONEY_COLS' base case) plus app_pricing.
# NON_DISCOUNT_MONEY_LABELS -- a column app.core.pricing never discounts (T-69: it fails
# _LIST_COL_RE on purpose) carries its own honest label text instead of "at list", the same way
# the Streamlit table (app/ui/style.column_config_for) always has. Empty as of DEC-66.1/T-69A
# (integrator pass): every dollar column in this app is now the one list-price basis -- see
# app/core/pricing.py's own comment on NON_DISCOUNT_MONEY_LABELS. Kept for a future dollar
# column whose basis genuinely is not the list price.
# `_disc` is pricing.apply()'s own suffix for the discounted twin of a raw list-price column.
_ID_RE = re.compile(r"(^id$)|(_id$)")
_MAGNITUDE: dict[str, re.Pattern] = {
    "dbu": re.compile(r"dbu", re.IGNORECASE),
    "pct": re.compile(r"(pct|percent)", re.IGNORECASE),
    "hours": re.compile(r"hours?$", re.IGNORECASE),
    "gb": re.compile(r"_gb$", re.IGNORECASE),
    "gb_bytes": re.compile(r"bytes", re.IGNORECASE),
}
_DISC_SUFFIX = "_disc"


def est_label(discount_pct: float | None) -> str:
    """"list price (effective)" at the 0% default, or "what-if: list price (effective), -X%" once
    discount_pct is set away from 0 (DEC-66.1/T-69) -- the same wording web/src/format.tsx's own
    client-side estLabel uses, so a price basis never reads two ways depending which side rendered
    it. X = round(discount_pct * 100, 1), trailing zeros dropped -- NOT plain round(x*100)
    (Python's banker's rounding gave -12% for a 12.5% discount, wrong in the wrong direction over
    real money); None/0/0.0 alone returns the base label."""
    if not discount_pct:
        return "list price (effective)"
    pct_off = round(discount_pct * 100, 1)
    return f"what-if: list price (effective), -{pct_off:g}%"


def _money_label(col: str, discount_pct: float) -> str | None:
    is_disc = col.endswith(_DISC_SUFFIX)
    base = col[: -len(_DISC_SUFFIX)] if is_disc else col
    # T-69: an own-basis column (one whose money is not the list price) is labelled by what it
    # actually is, the same dict app/ui/style.column_config_for already reads from, so both UIs
    # agree -- currently empty (DEC-66.1/T-69A ended this app's last two own-basis columns; see
    # app/core/pricing.py's own comment on NON_DISCOUNT_MONEY_LABELS), so this always falls
    # through to the list-price branch below today.
    own_label = app_pricing.NON_DISCOUNT_MONEY_LABELS.get(base)
    if own_label is not None:
        return own_label
    # Reuses app.core.pricing's own list-price column regex (the exact set pricing.apply() adds a
    # "_disc" column for) -- so app/api and app/core.pricing can never disagree about what counts
    # as a discountable dollar column (T-57's "reuse app/core/pricing.py" instruction).
    if not app_pricing._LIST_COL_RE.match(base):
        return None
    return est_label(discount_pct if is_disc else 0.0)


def classify_columns(columns: list[str], discount_pct: float) -> dict[str, dict[str, str | None]]:
    """{column: {"kind": "money"|"id"|"dbu"|"pct"|"hours"|"gb"|"gb_bytes"|None, "label": str|None}}
    -- money's label is either its own-basis text (net_default_cost/net_billed_cost, never
    discounted) or the "est. at effective list price" / "what-if: ..." suffix (raw column always
    at 0%, matching app/ui/style.column_config_for's own rule: only the "_disc" twin ever carries
    the caller's real discount_pct); every other kind's label is None (the client formats it,
    PLAN.md 6.1's fmt.py equivalents live in web/src/format.tsx)."""
    out: dict[str, dict[str, str | None]] = {}
    for col in columns:
        label = _money_label(col, discount_pct)
        if label is not None:
            out[col] = {"kind": "money", "label": label}
            continue
        if _ID_RE.search(col):
            out[col] = {"kind": "id", "label": None}
            continue
        kind = None
        for name, pattern in _MAGNITUDE.items():
            if pattern.search(col):
                kind = name
                break
        out[col] = {"kind": kind, "label": None}
    return out


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    # F5: pd.NaT is not a pd.Timestamp (the isinstance check below misses it) but IS an instance
    # of datetime.datetime, so without this it fell through to the (correct-for-a-real-timestamp)
    # isinstance(value, (_dt_date, _dt_datetime)) branch further down and rendered as the literal
    # text "NaT" (NaT.isoformat() == "NaT") in every cell an empty timestamp column produced.
    if value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):  # numpy scalar (int64/float64/bool_/...)
        value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
        return value
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    # T-68: aggregate_group_json's own values come from duckdb.fetchall() directly (never through
    # a pandas DataFrame, unlike every other reader in this module), so a DATE/TIMESTAMP group key
    # arrives as a plain stdlib date/datetime, not a pd.Timestamp -- isoformat() it explicitly the
    # same way, rather than relying on FastAPI's own jsonable_encoder to catch it implicitly.
    if isinstance(value, (_dt_date, _dt_datetime)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def records(df: pd.DataFrame, kinds: dict[str, dict[str, str | None]]) -> list[dict[str, Any]]:
    """df -> JSON-safe records. Every "id"-kind column renders as a string (PLAN.md 6.2: "IDs are
    strings", and it avoids float/scientific-notation precision loss on a large bigint id);
    everything else is converted through _json_scalar (NaN/NaT -> None, numpy -> native Python,
    a Timestamp -> ISO text)."""
    out: list[dict[str, Any]] = []
    id_cols = {c for c, info in kinds.items() if info.get("kind") == "id"}
    for _, row in df.iterrows():
        rec: dict[str, Any] = {}
        for col in df.columns:
            val = row[col]
            if col in id_cols:
                rec[col] = None if _json_scalar(val) is None else str(val)
            else:
                rec[col] = _json_scalar(val)
        out.append(rec)
    return out


def discount_pct() -> float:
    return float(app_config.load_settings()["discount_pct"])


# ---------------------------------------------------------------------------------------------
# Materiality floor (config/materiality.yml, app/core/materiality.py): app/core/data.py already
# applies the floor everywhere a row is read (status, below_floor, status_counts, an aggregate
# sum) -- this is the one remaining Python-side spot, the "floor" summary object GET /api/finding
# carries alongside those already-floored rows.
# ---------------------------------------------------------------------------------------------


def floor_json(query_id: str, columns, rows_below: int) -> dict[str, Any] | None:
    """{"column", "min", "unit", "label", "rows_below"} for GET /api/finding's body, or None when
    this query has no floor applicable to this table (app_materiality.applicable_floor -- no
    configured floor, no "status" column, or the built table does not carry the floor's own
    column) -- so the page can say e.g. "412 rows under 100 GB not judged" without recomputing
    anything itself."""
    floor = app_materiality.applicable_floor(query_id, columns)
    if floor is None:
        return None
    return {
        "column": floor.column, "min": floor.min, "unit": floor.unit, "label": floor.label,
        "rows_below": rows_below,
    }


# ---------------------------------------------------------------------------------------------
# T-75A: the library-corrections register (DEC-66.2, app/core/library_corrections.py). Attached
# to GET /api/findings (per row), GET /api/finding/{id} (once), and summarised whole on
# GET /api/coverage -- the same JSON shape everywhere, so app/web has exactly one entry shape to
# render whichever endpoint it came from.
# ---------------------------------------------------------------------------------------------


def _correction_json(c: app_library_corrections.Correction) -> dict[str, Any]:
    from app.api.guide import _clean_prose  # guide imports this module, so import it late
    return {
        "id": c.id, "query_id": c.query_id, "problem": _clean_prose(c.problem),
        "effect": _clean_prose(c.effect), "status": c.status, "fix": _clean_prose(c.fix), "item": c.item,
    }


def library_corrections_for(query_id: str) -> list[dict[str, Any]]:
    """Every register entry for one query_id, [] when it has none -- what a finding's own API
    response (list row or detail body) carries as its "library_corrections" field."""
    return [_correction_json(c) for c in app_library_corrections.by_query_id().get(query_id, [])]


def all_library_corrections() -> list[dict[str, Any]]:
    """The WHOLE register, each entry carrying its query's own title too (for the Coverage tab's
    browsable list, which has no other route to a query's display name) -- query_title is None
    for a query_id the registry cannot resolve (should not happen once load_corrections() has
    validated every id, but a stale cache mid-rebuild is not this function's job to guard)."""
    specs_by_id = {s.query_id: s for s in registry.load_registry()}
    out: list[dict[str, Any]] = []
    for c in app_library_corrections.load_corrections():
        spec = specs_by_id.get(c.query_id)
        rec = _correction_json(c)
        rec["query_title"] = spec.title if spec else None
        out.append(rec)
    return out
