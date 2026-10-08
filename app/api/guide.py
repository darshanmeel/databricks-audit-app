"""app/api/guide.py -- build_guide(), the whole body of GET /api/guide: one entry per executable
registry query (header fields, first step, docs links, columns) plus the "How this app works"
sections' own numbers."""
from __future__ import annotations

import re

import duckdb
import yaml

from app.api import service
from app.core import config as app_config
from app.core import data as app_core_data
from app.core import databricks_docs
from app.core import finding_columns
from app.core import materiality as app_materiality
from app.core import registry
from app.core import tag_compliance as app_tag_compliance
from app.core import tag_spend as app_tag_spend
from app.core import tags as app_core_tags

# the "How this app works" section ids; guide.tsx's GUIDE_SECTIONS mirrors this list. The last
# three are the Coverage & Gaps tabs' own topics (one per sub-tab), alongside the general
# "coverage" topic that config/databricks_docs.yml's own guide_sections entries already point at.
GUIDE_SECTION_SLUGS = (
    "flow", "window", "status", "floors", "money", "steps", "tags", "names", "coverage",
    "coverage-data-sources", "coverage-couldnt-check", "coverage-known-limitations",
)

# text for each header `empty_if` vocabulary word: why an empty result can still be a clean pass.
EMPTY_IF_WORDS: dict[str, str] = {
    "schema_not_enabled": "This system schema is opt-in and may not be enabled on the metastore.",
    "usage_tracking_off": "Serving / AI-Gateway usage tracking has not been turned on for the endpoint.",
    "preview_unavailable": "This table is Public Preview / Beta and may not exist in this region yet.",
    "po_not_enabled": "Predictive Optimization is not enabled, or there are no Unity Catalog managed tables.",
    "compute_scope_gap": "This table covers only some compute (classic-only, or SQL-warehouse/serverless-only).",
    "no_serverless": "This feature needs serverless compute (scans / query capture), which is not in use here.",
    "abac_only": "Only manually applied masks/filters are shown; ABAC policy-derived ones are not captured.",
    "submit_run_skipped": "One-time SUBMIT_RUN / WORKFLOW_RUN runs skip the jobs dimension tables.",
    "verbose_audit_required": "Fine-grained audit events need verbose audit logging turned on for the workspace.",
    "account_admin_only": "Reading this table requires account-admin privileges (e.g. system.ai_gateway.usage).",
    "privilege_scoped": "Reads are privilege-scoped; a non-admin principal sees fewer rows, or zero.",
    "retention_window": "Activity older than this table's retention window has already been purged.",
    "no_activity": "There is genuinely no matching activity in the selected window.",
    "lineage_inference_only": "Lineage is inferred and misses unsupported paths (spark-submit, RDD, JDBC, UDF, path-only references).",
    "ingestion_lag": "Recent activity has not been materialized into the system table yet.",
}


# ---------------------------------------------------------------------------------------------
# Header :param substitution: the ONE switch point, so a future change to the substitution rules
# means changing this one function's body, not every call site.
# ---------------------------------------------------------------------------------------------


_DIRECT_SQL_DIR = app_config.ROOT / "app" / "direct_sql"


_NORM = "regexp_replace(lower(k), '[ _-]+', '')"


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _as_of() -> tuple[str, str]:
    """(SQL date, words) for the end of the window: the loaded export's own cutoff, so a pasted
    query covers the same days as the app; today when no export says otherwise."""
    try:
        info = app_core_data.direct_export_info() or {}
    except Exception:  # noqa: BLE001 -- no export loaded yet: fall back to today
        info = {}
    cutoff = info.get("as_of_date")
    if cutoff and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(cutoff)):
        return f"DATE '{cutoff}'", f"the days before {cutoff}, the same window as this export"
    if app_config.load_settings()["include_today"]:
        return "date_add(current_date(), 1)", "up to now, today included"
    return "current_date()", "up to today, complete days only"


def _tag_sql(query_id: str, window_days: int) -> str | None:
    """The Tags page's own queries, for the keys in settings mandatory_tag_keys."""
    labels = app_config.load_settings()["mandatory_tag_keys"]
    as_of, _ = _as_of()
    keys = [(re.sub(r"[ _-]+", "", k.lower()), k) for k in labels]
    variants = app_tag_compliance.key_variants([n for n, _ in keys])
    # Each mandatory key with the keys that count as it (env for environment).
    alts = {n: ", ".join(_sql_str(v) for v, k in variants.items() if k == n) for n, _ in keys}
    key_list = ", ".join(_sql_str(v) for v in variants)
    names = ", ".join(labels)
    if query_id == "tags_missing_objects":
        wanted = "\n  UNION ALL ".join(f"SELECT array({alts[n]}) AS tag_keys, {_sql_str(label)} AS tag_name" for n, label in keys)
        return f"""-- Jobs, SQL warehouses and all-purpose clusters missing a mandatory tag ({names}) on their
-- own tags. One row per object that is not deleted; `missing` lists the tags it lacks.
WITH wanted AS (
  {wanted}
),
latest AS (
  SELECT 'job' AS kind, workspace_id, job_id AS id, name, tags, delete_time,
         ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) AS rn
  FROM system.lakeflow.jobs
  UNION ALL
  SELECT 'warehouse', workspace_id, warehouse_id, warehouse_name, tags, delete_time,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC)
  FROM system.compute.warehouses
  UNION ALL
  SELECT 'cluster', workspace_id, cluster_id, cluster_name, tags, delete_time,
         ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC)
  FROM system.compute.clusters
  WHERE COALESCE(cluster_source, '') NOT IN ('JOB', 'PIPELINE', 'PIPELINE_MAINTENANCE', 'SQL', 'MODELS')
),
own AS (
  SELECT kind, workspace_id, id, name, transform(map_keys(tags), k -> {_NORM}) AS tag_keys
  FROM latest
  WHERE rn = 1 AND delete_time IS NULL
)
SELECT COALESCE(w.workspace_name, own.workspace_id) AS workspace,
       own.kind, own.name, own.id,
       array_join(array_sort(collect_list(wanted.tag_name)), ', ') AS missing
FROM own
CROSS JOIN wanted
LEFT JOIN system.access.workspaces_latest w ON CAST(w.workspace_id AS STRING) = own.workspace_id
WHERE NOT COALESCE(arrays_overlap(own.tag_keys, wanted.tag_keys), false)
GROUP BY ALL
ORDER BY workspace, own.kind, own.name
"""
    if query_id == "tags_missing_queries":
        counts = ",\n       ".join(
            f"count_if(NOT COALESCE(arrays_overlap(q.tag_keys, array({alts[n]})), false)) AS missing_{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')}"
            for n, label in keys
        )
        return f"""-- Queries in the last {int(window_days)} days by warehouse or cluster: how many lack each mandatory
-- tag ({names}) as their own query tag, and how many lack them all.
WITH q AS (
  SELECT workspace_id,
         CASE WHEN compute.warehouse_id IS NOT NULL THEN 'warehouse'
              WHEN compute.cluster_id IS NOT NULL THEN 'cluster'
              ELSE 'serverless' END AS compute_kind,
         COALESCE(compute.warehouse_id, compute.cluster_id) AS compute_id,
         transform(map_keys(query_tags), k -> {_NORM}) AS tag_keys
  FROM system.query.history
  WHERE start_time >= {as_of} - INTERVAL {int(window_days)} DAYS AND start_time < {as_of}
),
wh AS (
  SELECT warehouse_id, warehouse_name FROM (
    SELECT warehouse_id, warehouse_name,
           ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
    FROM system.compute.warehouses
  ) x WHERE rn = 1
)
SELECT COALESCE(w.workspace_name, q.workspace_id) AS workspace,
       q.compute_kind,
       COALESCE(wh.warehouse_name, q.compute_id, 'serverless') AS warehouse_or_cluster,
       count(*) AS queries,
       {counts},
       count_if(NOT COALESCE(arrays_overlap(q.tag_keys, array({key_list})), false)) AS missing_all
FROM q
LEFT JOIN wh ON wh.warehouse_id = q.compute_id
LEFT JOIN system.access.workspaces_latest w ON CAST(w.workspace_id AS STRING) = q.workspace_id
GROUP BY ALL
ORDER BY queries DESC
"""
    if query_id == "tags_on_workspaces":
        return f"""-- Every tag on each workspace's billed usage in the last {int(window_days)} days.
WITH t AS (
  SELECT workspace_id, explode(custom_tags) AS (tag_key, tag_value)
  FROM system.billing.usage
  WHERE usage_date >= {as_of} - INTERVAL {int(window_days)} DAYS AND usage_date < {as_of}
)
SELECT DISTINCT
  COALESCE(w.workspace_name, t.workspace_id) AS workspace,
  t.tag_key,
  t.tag_value
FROM t
LEFT JOIN system.access.workspaces_latest w
  ON CAST(w.workspace_id AS STRING) = t.workspace_id
ORDER BY workspace, tag_key, tag_value
"""
    return None


def runnable_sql(query_id: str, window_days: int) -> str | None:
    """app/direct_sql/<id>.sql with the export's placeholders filled in so it runs as-is in a
    Databricks SQL editor: the window, "today" as the as-of date, and this app's source tag."""
    if not re.fullmatch(r"[a-z0-9_]+", query_id or ""):
        return None
    if query_id.startswith("tags_"):
        return _tag_sql(query_id, window_days)
    path = _DIRECT_SQL_DIR / f"{query_id}.sql"
    if not path.is_file():
        return None
    as_of_date, words = _as_of()
    # The build tool's own "generated from ..." lines mean nothing to someone running the query.
    body = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("-- generated "))
    header = f"-- {query_id}: {int(window_days)} days, {words}. Reads system tables only.\n"
    return header + (
        body
        .replace("__WINDOW_DAYS__", str(int(window_days)))
        .replace("__AS_OF_DATE__", as_of_date)
        .replace("__AS_OF_TS__", "current_timestamp()")
        .replace("__QUERY_SOURCE__", app_config.query_source())
    )


def _substituted(spec: registry.QuerySpec) -> dict:
    """read_this/healthy/investigate_if/actions/not_assessed_reasons, every :param token replaced
    by its value in effect (service.substitute_header_params)."""
    return service.substitute_header_params(spec)


# ---------------------------------------------------------------------------------------------
# summary_line -- line 1 of a finding's 2-line panel summary.
# ---------------------------------------------------------------------------------------------

_SNAKE_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_ONE_ROW_RE = re.compile(r"^One row\s*(?:=|is)\s*", re.IGNORECASE)
_COMBO_RE = re.compile(
    r"\b(?:a|one)\s+\(\s*([a-z][a-z0-9_]*(?:\s*,\s*[a-z][a-z0-9_]*)+)\s*\)\s+(?:combination|group|pair)\b",
    re.IGNORECASE,
)
_OTHER_BRACKET_RE = re.compile(r"\s*\(\s*[a-z][a-z0-9_]*(?:\s*,\s*[a-z][a-z0-9_]*)+\s*\)")
_ID_TOKEN_RE = re.compile(r"\b(DEC-\d+|T-\d+|P4-\d+)\b")
_PARAM_LEFTOVER_RE = re.compile(r":[A-Za-z_]\w*")

# ---------------------------------------------------------------------------------------------
# _clean_prose -- the Guide's own reference-documentation pass over a header's free text. Never
# touches the .sql header itself, and never runs on summary_line()'s output (that has its own,
# separately tested, allow-listed rules) -- only on what _finding_json sends out below.
# ---------------------------------------------------------------------------------------------

# A decision, task or plan id: DEC-66.3, T-75B, P4-01, P3-WASTEUSD.
_ID = r"(?:DEC-\d+(?:\.\d+)*|T-\d+[A-Z]?|P\d-(?:\d+|[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*))"
_COMMIT_RE = re.compile(r",?\s*\bcommits?\s+[0-9a-f]{7,40}(?:\s*/\s*[0-9a-f]{7,40})*")
_ID_PER_RE = re.compile(rf"\s+(?:per|see)\s+{_ID}(?![\w-]|\.\d)")
_ID_OWNER_RE = re.compile(rf"\b{_ID}'s\b")
_PAREN_ID_ONLY_RE = re.compile(rf"\(\s*{_ID}\s*\)")
_ID_INLINE_RE = re.compile(rf"\b{_ID}(?![\w-]|\.\d)\s*[-:]?\s*")
_PAREN_LEFTOVER_RE = re.compile(r"\s*\(\s*[,;]?\s*\)|\s*,\s*(?=\))")
_DOUBLE_DASH_RE = re.compile(r"\s+--\s+")
_SHOUTED_BOOL_RE = re.compile(r"\b(TRUE|FALSE)\b")
_SENTENCE_START_RE = re.compile(r"(^|[.!?]\s+)([a-z])")


def _clean_prose(text: str | None) -> str:
    """A DEC-nn/T-nn/P4-nn id dropped (a lone "(id)" dropped whole), " -- " read as a real em
    dash, and shouted TRUE/FALSE read as plain words. Column names are left exactly as the header
    wrote them -- guide.tsx puts those in code font instead of rewording them."""
    if not text:
        return text or ""
    cleaned = _COMMIT_RE.sub("", text)
    cleaned = _ID_PER_RE.sub("", cleaned)
    cleaned = _ID_OWNER_RE.sub("the app's", cleaned)
    cleaned = _PAREN_ID_ONLY_RE.sub("", cleaned)
    cleaned = _ID_INLINE_RE.sub("", cleaned)
    cleaned = re.sub(r"\(\s*,\s*", "(", cleaned)
    cleaned = _PAREN_LEFTOVER_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+(?=[:,]|\.(?:\s|$))", "", cleaned)
    cleaned = _DOUBLE_DASH_RE.sub(" — ", cleaned)
    cleaned = _SHOUTED_BOOL_RE.sub(lambda m: m.group(1).lower(), cleaned)
    # "carries no workspace_id" is a column talking; a reader wants "has no workspace".
    cleaned = re.sub(r"\bcarries no workspace_id\b", "has no workspace", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    def _cap(m: "re.Match[str]") -> str:
        # never capitalize into a column name (a snake_case identifier stays lower -- guide.tsx
        # renders it in code font, verbatim) -- only a stripped id can leave a sentence lowercase.
        if _SNAKE_TOKEN_RE.match(cleaned[m.start(2):m.start(2) + 40]):
            return m.group(0)
        return m.group(1) + m.group(2).upper()

    return _SENTENCE_START_RE.sub(_cap, cleaned)


def _desnake_word(word: str) -> str:
    """Drops a trailing _id and turns _ into a space (e.g. "termination_code" -> "termination code")."""
    word = re.sub(r"_id$", "", word)
    return word.replace("_", " ")


def _join_words(names: list) -> str:
    words = [_desnake_word(n.strip()) for n in names]
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _combo_sub(m: "re.Match[str]") -> str:
    return "one " + _join_words(m.group(1).split(","))


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    m = re.match(r"^(.*?[.!?])(\s|$)", text.strip())
    return (m.group(1) if m else text.strip()).strip()


def _auto_summary(read_this: str) -> str:
    """The first sentence of read_this: "One row is" -> "Each row is", a (x, y, z) combination ->
    "one x, y and z", any other bracketed snake_case list dropped, everything else desnaked."""
    line = _first_sentence(read_this)
    line = _ONE_ROW_RE.sub("Each row is ", line)
    line = _COMBO_RE.sub(_combo_sub, line)
    line = _OTHER_BRACKET_RE.sub("", line)
    line = _SNAKE_TOKEN_RE.sub(lambda m: _desnake_word(m.group(0)), line)
    return re.sub(r"\s{2,}", " ", line).strip()


def _summary_checks(line: str) -> list:
    """[] means `line` is a fine automatic summary; otherwise names why it needs a summary: override."""
    problems = []
    if not line.startswith("Each row is"):
        problems.append("does not start with 'Each row is'")
    if len(line) > 140:
        problems.append(f"is {len(line)} characters, over the 140 limit")
    if line.count("(") != line.count(")"):
        problems.append("brackets are not balanced")
    if _ID_TOKEN_RE.search(line):
        problems.append("still carries a DEC-/T-/P4- id")
    if _PARAM_LEFTOVER_RE.search(line):
        problems.append("still carries a :param token")
    if _SNAKE_TOKEN_RE.search(line):
        problems.append("still carries snake_case")
    return problems


def summary_line(spec: registry.QuerySpec, header_summary: str | None = None) -> dict:
    """`header_summary` (a header's own optional override) verbatim when given, else the
    auto-derived first sentence of read_this. `problems` is [] when the text needs no override."""
    if header_summary and header_summary.strip():
        return {"text": header_summary.strip(), "source": "header", "problems": []}
    # runs on the same substituted read_this the panel and the Guide both show
    text = _auto_summary(_substituted(spec)["read_this"])
    return {"text": text, "source": "auto", "problems": _summary_checks(text)}


# ---------------------------------------------------------------------------------------------
# first_step -- line 2's "first step".
# ---------------------------------------------------------------------------------------------

_TIER_WORDS = ("free", "config", "spend")
_ANYWHERE_TIER_BRACKET_RE = re.compile(r"\(([^()]*(?:free|config|spend)[^()]*)\)", re.IGNORECASE)
_TRAILING_BRACKET_RE = re.compile(r"\s*\(([^()]*)\)\s*[;.]?\s*$")
_CUT_SEP_RE = re.compile(r": |; | - ")


def _action_tier(raw: str) -> str | None:
    t = (raw or "").lower()
    for word in _TIER_WORDS:
        if word in t:
            return word
    return None


def _cap_words(text: str, limit: int = 100) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    head = cut[:last_space] if last_space > 40 else cut
    return re.sub(r"[\s,;:.–-]+$", "", head) + "..."


def first_step(actions: list) -> dict | None:
    """Rung 1 of an actions ladder (ported from overview_tile.tsx's firstAction()): tier bracket
    stripped when trailing, cut at the first ": "/"; "/" - ", snake_case -> words, 100-char cap.
    None when there is no ladder, or rung 1 is blank/"n/a"."""
    if not actions:
        return None
    full = str(actions[0]).strip()
    if not full or full.lower().startswith("n/a"):
        return None

    tier = None
    anywhere = _ANYWHERE_TIER_BRACKET_RE.search(full)
    if anywhere:
        tier = _action_tier(anywhere.group(1))
    trailing = _TRAILING_BRACKET_RE.search(full)
    if trailing and _action_tier(trailing.group(1)):
        full = full[: trailing.start()].strip()
    full = re.sub(r"[;.]+$", "", full).strip()
    if not full:
        return None

    text = full
    cut = _CUT_SEP_RE.search(text)
    if cut and cut.start() >= 25:
        text = text[: cut.start()]
    text = _SNAKE_TOKEN_RE.sub(lambda m: _desnake_word(m.group(0)), text)
    text = _cap_words(text)
    return {"text": text, "full": full, "tier": tier}


# ---------------------------------------------------------------------------------------------
# build_guide -- GET /api/guide's whole body.
# ---------------------------------------------------------------------------------------------


def _safe_thresholds() -> dict:
    try:
        return app_config.load_thresholds()
    except app_config.ConfigError:
        return {}


def _safe_materiality_count() -> int:
    try:
        return len(app_config.load_materiality())
    except app_config.ConfigError:
        return 0


def _param_json(query_id: str, param: dict, thresholds: dict) -> dict:
    name = param["name"]
    per_query = thresholds.get(query_id) or {}
    all_ = thresholds.get("_all") or {}
    if name in per_query:
        value, source = per_query[name], "your setting"
    elif name in all_:
        value, source = all_[name], "your setting"
    else:
        value, source = param.get("default"), "default"
    return {"name": name, "meaning": param.get("meaning"), "value": value, "source": source}


# `windowed` (registry.py) only says the app's period_days selector drives the query -- two
# checks still have a period (a calendar-month grain) without period_days, so windowed=False alone
# would misread as "no period at all, a snapshot" (the article's window chip).
FIXED_PERIOD_QUERY_IDS = frozenset({"cost_monthly_actuals", "cost_sku_trend_12m"})


def period_kind(spec: registry.QuerySpec) -> str:
    """"days" (the app's 7/30/90-day window), "fixed" (its own period the window selector never
    touches, e.g. calendar months) or "snapshot" (current state, no period at all)."""
    if spec.windowed:
        return "days"
    if spec.query_id in FIXED_PERIOD_QUERY_IDS:
        return "fixed"
    return "snapshot"


# A direct export/import build never runs tools/snapshot.py's own `SELECT current_metastore()`
# probe, so it has no manifest.metastore to resolve a cloud from -- dims.dim_workspace.url's own
# host still names it.
_AZURE_HOST_RE = re.compile(r"\.azuredatabricks\.net$", re.IGNORECASE)
_GCP_HOST_RE = re.compile(r"\.gcp\.databricks\.com$", re.IGNORECASE)
_AWS_HOST_RE = re.compile(r"\.cloud\.databricks\.com$", re.IGNORECASE)


def _cloud_from_workspace_urls() -> str | None:
    """The first dims.dim_workspace.url whose host names a cloud; None on no table, no rows, or
    no recognised host (never raises -- this is a best-effort fallback, not a required source)."""
    try:
        df = app_core_data.read_dim_workspace()
    except (duckdb.Error, OSError):
        return None
    if df is None or "url" not in df.columns:
        return None
    for raw in df["url"].dropna().tolist():
        host = re.sub(r"^https?://", "", str(raw).strip(), flags=re.IGNORECASE).split("/")[0]
        if _AZURE_HOST_RE.search(host):
            return "azure"
        if _GCP_HOST_RE.search(host):
            return "gcp"
        if _AWS_HOST_RE.search(host):
            return "aws"
    return None


def _money_column(names: list | None) -> dict | None:
    """The one column a Guide article treats as this check's own dollar figure -- the SAME rule
    /api/findings' bulk money field uses (finding_columns.money_column), so the Guide and All
    findings never name a different column for the same check. None when the check has none."""
    col = finding_columns.money_column(names or [])
    return {"column": col[0]} if col else None


def _idle_gap_seconds(specs: list, thresholds: dict):
    """The idle-gap threshold, read off whichever query's header names a param called
    idle_gap_seconds; None if no query has one."""
    for spec in specs:
        for param in spec.params:
            if param["name"] == "idle_gap_seconds":
                return _param_json(spec.query_id, param, thresholds)["value"]
    return None


def _tag_reach(spec: registry.QuerySpec, names: list | None, spend_ready: bool) -> dict | None:
    """Which tag levels a tag filter follows for this check, as /api/finding would apply them;
    None when the check is not built, so its columns are unknown."""
    if spend_ready and app_tag_spend.has_variant(spec.query_id):
        return {"kind": "spend", "words": list(app_tag_spend.CHAIN_WORDS), "reason": None}
    if names is None:
        return None
    chain = app_core_tags.tag_chain(spec.query_id, names, spec.domain)
    return {"kind": chain.kind, "words": chain.chain_words, "reason": chain.reason}


def _finding_json(spec: registry.QuerySpec, by_id: dict, docs_map, thresholds: dict,
                   columns_by_id: dict, discount_pct: float, spend_ready: bool = False) -> dict:
    sub = _substituted(spec)
    summary = summary_line(spec, getattr(spec, "summary", None))
    step = first_step(sub["actions"])
    reads = [f"system.{schema}.{table}" for schema, table in spec.sources]
    concept_keys = (
        [c.key for c in docs_map.concepts() if spec.query_id in c.queries] if docs_map else []
    )

    names = columns_by_id.get(spec.query_id)
    columns = None
    if names is not None:
        kinds = service.classify_columns(names, discount_pct)
        columns = [{"name": n, "kind": kinds[n]["kind"]} for n in names]

    next_list = []
    for n in spec.next:
        target = by_id.get(n["query_id"])
        # Only checks with a Guide page of their own; a link to any other falls back to the home.
        if target is None or not target.executable:
            continue
        next_list.append({
            "query_id": n["query_id"],
            "title": target.title if target else None,
            "if": n.get("if", ""),
        })

    return {
        "query_id": spec.query_id,
        "title": spec.title,
        "domain": spec.domain,
        "tier": spec.tier,
        "stars": spec.stars,
        "origin": spec.origin,
        "is_finding": spec.is_finding,
        "windowed": spec.windowed,
        "period_kind": period_kind(spec),
        # _clean_prose here too: an auto-derived summary (unlike header_summary) can still carry
        # an inline id or shouted bool from read_this, same as every other prose field below.
        "summary": _clean_prose(summary["text"]),
        "summary_source": summary["source"],
        "first_step": step,
        "read_this": _clean_prose(sub["read_this"]),
        "healthy": _clean_prose(sub["healthy"]),
        "investigate_if": _clean_prose(sub["investigate_if"]),
        "severity_cap": app_materiality.severity_cap(spec.query_id),
        "actions": [_clean_prose(a) for a in sub["actions"]],
        "not_assessed_reasons": [_clean_prose(r) for r in sub["not_assessed_reasons"].values()],
        "empty_if": list(spec.empty_if),
        "empty_if_words": [EMPTY_IF_WORDS.get(token, token) for token in spec.empty_if],
        "caveats": _clean_prose(spec.caveats),
        "confidence": spec.confidence,
        "confidence_note": _clean_prose(spec.confidence_note),
        "requires": _clean_prose(spec.requires),
        "params": [_param_json(spec.query_id, p, thresholds) for p in spec.params],
        "next": next_list,
        "reads": reads,
        "docs": reads + concept_keys,
        "columns": columns,
        "money": _money_column(names),
        "library_corrections": service.library_corrections_for(spec.query_id),
        "tag_reach": _tag_reach(spec, names, spend_ready),
    }


def build_guide() -> dict:
    """GET /api/guide's whole body: one executable-registry entry per finding, the docs map keyed
    for every cloud, and the "How this app works" sections' own numbers."""
    try:
        settings = app_config.load_settings()
    except app_config.ConfigError:
        settings = app_config.DEFAULT_SETTINGS
    manifest = app_core_data.snapshot_manifest()
    all_specs = registry.load_registry()
    by_id = {s.query_id: s for s in all_specs}
    specs = [s for s in all_specs if s.executable]
    thresholds = _safe_thresholds()

    docs_error = None
    try:
        docs_map = databricks_docs.load_docs(
            valid_slugs=frozenset(GUIDE_SECTION_SLUGS),
            valid_query_ids=frozenset(by_id),
        )
    except (databricks_docs.DocsError, OSError, yaml.YAMLError) as exc:
        docs_map = None
        docs_error = str(exc)

    cloud, cloud_source = databricks_docs.resolve_cloud(
        manifest if manifest.get("available") else None,
        docs_map.default_cloud if docs_map else "aws",
    )
    if cloud_source == "default":
        workspace_cloud = _cloud_from_workspace_urls()
        if workspace_cloud:
            cloud, cloud_source = workspace_cloud, "workspace"
    discount_pct = float(settings["discount_pct"])

    columns_available = True
    try:
        columns_by_id = app_core_data.finding_columns_batch([s.query_id for s in specs])
    except (duckdb.Error, OSError):
        columns_by_id = {}
        columns_available = False

    if docs_map is not None:
        docs_json = {
            key: {
                "kind": entry.kind, "title": entry.title, "what": entry.what,
                "enable": entry.enable, "note": entry.note,
                "urls": databricks_docs.entry_urls(entry),
            }
            for key, entry in docs_map.entries.items()
        }
        all_entries = list(docs_map.tables()) + list(docs_map.concepts())
        sections = {
            slug: [e.key for e in all_entries if slug in e.guide_sections]
            for slug in GUIDE_SECTION_SLUGS
        }
        area_keys = sorted({area for c in docs_map.concepts() for area in c.areas})
        areas = {area: [c.key for c in docs_map.concepts() if area in c.areas] for area in area_keys}
        default_enable = docs_map.default_enable
    else:
        docs_json = {}
        sections = {slug: [] for slug in GUIDE_SECTION_SLUGS}
        areas = {}
        default_enable = ""

    try:
        spend_ready = app_core_data.tag_spend_ready()
    except (duckdb.Error, OSError):
        spend_ready = False
    findings = [
        _finding_json(s, by_id, docs_map, thresholds, columns_by_id, discount_pct, spend_ready)
        for s in specs
    ]

    direct = app_core_data.direct_export_info()
    if manifest.get("available"):
        as_of = manifest.get("as_of")
    else:
        as_of = direct["as_of"] if direct else None
    return {
        "app": {
            "as_of": as_of,
            "direct_export": direct,
            "window_options": list(service.WINDOW_CHOICES),
            "default_window": settings["default_window"],
            "discount_pct": discount_pct,
            "idle_gap_seconds": _idle_gap_seconds(specs, thresholds),
            "cloud": cloud,
            "cloud_source": cloud_source,
            "columns_available": columns_available,
            "default_enable": default_enable,
            "docs_error": docs_error,
            "materiality_floors_configured": _safe_materiality_count(),
            "mandatory_tag_keys": settings["mandatory_tag_keys"],
            "tag_share_floor": settings["tag_share_floor"],
            "tag_coverage_floor": settings["tag_coverage_floor"],
        },
        "docs": docs_json,
        "sections": sections,
        "areas": areas,
        "findings": findings,
    }
