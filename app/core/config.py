"""app/core/config.py

YAML load/save for config/settings.yml, config/thresholds.yml, config/tag_aliases.yml and
config/materiality.yml, plus CSV read/write helpers over dbt/seeds/workspace_env_overrides.csv
(DEC-30 -- there is no config/workspace_env.yml; D7's naming is superseded). Every load/save
re-reads AUDIT_CONFIG_DIR from os.environ on every call (DEC-27), never caching it at import time,
so tests/conftest.py (DEC-26) can point a whole pytest session at a temporary config/ copy without
this module ever touching the real one. Writes are atomic: a temp file in the same directory, then
os.replace().

Two shapes this file owns and documents here (T-28's Settings page author should not have to
reverse-engineer either):

  config/settings.yml (DEC-29):
    discount_pct: float in [0, 1)          # presentation-only; never rebuilds anything
    dbt_target: str                        # "dev" | "test" | "databricks"
    default_window: int                    # one of 7, 30, 90 (D5)
    ui:
      max_rows: int                        # LIMIT applied by app/core/data.read_finding (DEC-47)
      max_chart_categories: int            # charts group everything past this into "Other"
    tag_share_floor: float in [0, 1]       # T-63/DEC-60 rule 3: below this share a workspace's
                                            #   dominant tag-attribute value reads 'mixed', fed to
                                            #   dbt as the `share_floor` var (tools/dbt_run.py)
    tag_coverage_floor: float in [0, 1]    # P4-T/DEC-63: below this coverage a workspace's tag
                                            #   inference (tags.tag_workspace) reads 'untagged'
                                            #   even at full dominance, fed to dbt as the
                                            #   `coverage_floor` var (tools/dbt_run.py)
    brand:                                 # visible wordmark + footer; a missing key falls
      name / url / contact_name / contact_email: str  #   back to DEFAULT_SETTINGS["brand"]
    privacy:                               # mask_user_identities off by default; on
      mask_user_identities: bool           #   masks user email/name columns, never a resource
                                            #   name. Fed to dbt as the `mask_user_identities` var.
    Seeded on first use (no file present anywhere on the board before this task) with
    discount_pct: 0.0, dbt_target: "dev", default_window: 30,
    ui: {max_rows: 500, max_chart_categories: 8}, tag_share_floor: 0.6, tag_coverage_floor: 0.5,
    brand: Crosshire defaults, privacy: {mask_user_identities: false}.

    config/settings.local.yml (under AUDIT_CONFIG_DIR, gitignored): whichever top-level settings
    keys the app itself has saved (save_settings_local) -- merged over settings.yml by
    load_settings, only the keys it actually sets. The one place the running app writes a setting;
    settings.yml, the tracked file, is never touched by it.
    Unknown top-level or `ui` keys, and wrong types, are rejected (ConfigError, a SettingsError
    with `.key`/`.line` set when they can be determined -- P2-NOBUILD: GET /api/status reads
    those two fields so app/web's shell can point at the exact broken line rather than just
    saying "invalid"). db_path/snapshot_dir keys existed briefly (T-25/DEC-29) but were never
    read by anything -- app/core/data.py's own _db_path()/_snapshot_dir() only ever look at the
    AUDIT_DB/AUDIT_SNAPSHOT_DIR environment variables, so the settings.yml keys were silently
    ignored the whole time (P2-NOBUILD, DEC-66.6: honour or remove, whichever is simpler and
    honest -- honouring them would mean teaching dbt/profiles.yml and tools/dbt_run.py the same
    settings.yml lookup too, well outside this module; removed instead).

  config/tag_aliases.yml:
    top_tags:
      cost_center: Cost center             # tag key (matched case/space/hyphen/underscore-
      domain: Domain                       #   insensitive) -> the name shown; empty shows the key.
    The top-bar filters and Money's "Spend by" tags, in this order; at most TOP_TAGS_SHOWN with
    values are shown.

config/thresholds.yml (already seeded by T-05 with task_cluster_utilization: {top_n: 100000};
this module never removes or overwrites an existing file's contents on load, only on an explicit
save_thresholds() call) shape: {<query_id>: {<param>: number}, _all: {<param>: number}} --
overrides only, every threshold not listed here falls through to the query's own header default
(dbt/macros/param.sql).

config/materiality.yml (the per-check materiality-floor register): {<query_id>: {column, min, unit,
label}} -- a row whose named column is under `min` is not judged (app/core/materiality.py applies
this at read time; the row's own exported status is never rewritten). query_id must resolve in
app.core.registry (an unknown id fails loudly at load, the same discipline
app/core/library_corrections.py's register already uses); `column` must look like a real SQL
identifier -- the built table may or may not actually carry it (that is checked at read time,
never here, since this module never opens the database). Seeded with DEFAULT_MATERIALITY (this
repo's own config/materiality.yml) on first use.

dbt/seeds/workspace_env_overrides.csv (T-23; columns workspace_id, env, name, url, note; written
by the Settings page, T-28) is NOT under AUDIT_CONFIG_DIR -- it is a fixed dbt seed path. Both
read_env_overrides() and write_env_overrides() accept an optional `path` override so a test can
round-trip it through a tmp_path copy (DEC-26) instead of the real seed file.

Stdlib + pyyaml, plus a local import of app.core.registry inside materiality validation only (to
check a query_id is real) -- every other function here stays free of that dependency.
"""
from __future__ import annotations

import csv
import os
import re
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent


class ConfigError(ValueError):
    """A settings/thresholds/tag_aliases file (or an in-memory dict about to be saved) failed
    validation: an unknown key, a wrong type, or a value outside its allowed set."""


class SettingsError(ConfigError):
    """config/settings.yml specifically failed to load -- either it is not valid YAML at all, or
    it parsed but failed _validate_settings. A subclass of ConfigError (every existing `except
    app_config.ConfigError` elsewhere in the app still catches this unchanged) that additionally
    carries, when known:
      key   the offending top-level key ("discount_pct"), or a dotted "ui.max_rows" path for one
            nested under `ui:`. None for a whole-document problem (e.g. settings.yml is a list,
            not a mapping).
      line  the 1-indexed line it appears on in the raw file. Set directly by load_settings() for
            a YAML syntax error (PyYAML's own parser already knows the position); derived from
            `key` via _find_key_line() for a semantic error (_validate_settings works off the
            already-parsed dict and has no text position of its own).
    P2-NOBUILD: GET /api/status reads both so app/web's shell can say exactly what to fix instead
    of just "config/settings.yml is invalid"."""

    def __init__(self, message: str, key: str | None = None, line: int | None = None):
        super().__init__(message)
        self.key = key
        self.line = line


# ---------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------


def _config_dir() -> Path:
    env = os.environ.get("AUDIT_CONFIG_DIR")
    return Path(env) if env else ROOT / "config"


def materiality_path() -> Path:
    """config/materiality.yml's own path (under AUDIT_CONFIG_DIR) -- exposed so app/core/materiality
    can cache floors against this file's (mtime, size) without duplicating _config_dir()'s env
    lookup."""
    return _config_dir() / "materiality.yml"


def _atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------------------------
# settings.yml
# ---------------------------------------------------------------------------------------------

SETTINGS_KEYS = {
    "discount_pct", "dbt_target", "default_window", "ui",
    "tag_share_floor",  # T-63/DEC-60 rule 3: the workspace-attribute share floor (see below).
    "tag_coverage_floor",  # P4-T/DEC-63: the workspace-tag coverage floor (see below).
    "brand",
    "privacy",  # mask_user_identities (see below).
    "query_source",  # the "Source" tag on this app's queries in query history.
    "export_tag_keys",  # tag names the export keeps per object; None = all.
    "include_today",  # export today's partial day too.
    "mandatory_tag_keys",  # up to 5 tags every query, job, pipeline, cluster, warehouse and workspace must carry.
    "admin_groups",  # groups expected to hold broad grants; their grants read OK.
    "load",  # the local database load: DuckDB memory and threads, which tag keys tags.tag_entity keeps.
}
UI_KEYS = {"max_rows", "max_chart_categories"}
LOAD_KEYS = {"memory_limit", "threads", "tag_keys", "top_tag_keys"}
BRAND_KEYS = {"name", "url", "contact_name", "contact_email"}
PRIVACY_KEYS = {"mask_user_identities"}
WINDOW_CHOICES = (7, 30, 90)

DEFAULT_SETTINGS: dict = {
    "discount_pct": 0.0,
    "dbt_target": "dev",
    "default_window": 30,
    "ui": {"max_rows": 500, "max_chart_categories": 8},
    "tag_share_floor": 0.6,
    "tag_coverage_floor": 0.5,
    "brand": {
        "name": "Crosshire",
        "url": "https://crosshire.ch",
        "contact_name": "Darshan Singh",
        "contact_email": "",
    },
    "privacy": {"mask_user_identities": False},
    "query_source": None,  # None = the brand name
    "export_tag_keys": None,  # None = every tag
    "include_today": True,
    "mandatory_tag_keys": ["cost_center", "domain", "env"],
    "admin_groups": [],
    "load": {"memory_limit": "1GB", "threads": 4, "tag_keys": None, "top_tag_keys": 10},
}


def _find_key_line(text: str, key: str) -> int | None:
    """Best-effort 1-indexed line number of `key` in raw settings.yml `text`. config/settings.yml
    is flat -- `ui:`/`brand:` nest one level, no deeper -- so this is a plain per-line scan, not
    a real YAML-position lookup: a dotted "ui.max_rows" key is searched for only inside the
    `ui:` block; anything else is searched for at the top level. Returns None when the key's own
    line cannot be found -- display-only, never worth raising over."""
    if not isinstance(key, str):
        # _validate_settings always str()s `.key` before raising (see the sites above), so this
        # only guards a future/other SettingsError raiser that does not -- never worth raising
        # over here either, same as the "not found" case.
        return None
    top_key, _, sub_key = key.partition(".")
    lines = text.splitlines()
    if not sub_key:
        pattern = re.compile(rf"^{re.escape(top_key)}\s*:")
        for i, line in enumerate(lines):
            if pattern.match(line):
                return i + 1
        return None
    block_start = re.compile(rf"^{re.escape(top_key)}\s*:")
    sub_pattern = re.compile(rf"^\s+{re.escape(sub_key)}\s*:")
    in_block = False
    for i, line in enumerate(lines):
        if block_start.match(line):
            in_block = True
            continue
        if in_block:
            if line.strip() and not line[0].isspace():
                break  # left the block
            if sub_pattern.match(line):
                return i + 1
    return None


def _validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise SettingsError("settings.yml must be a mapping")
    unknown = set(settings) - SETTINGS_KEYS
    if unknown:
        # YAML 1.1 reads some unquoted scalars (2, on, yes, ...) as int/bool, not str, so an
        # unknown key is not guaranteed to be a string -- str() every key before sorting (a mixed
        # int/str set raises TypeError from sorted() itself) and before using it as `.key`, which
        # _find_key_line() and every caller downstream (GET /api/status) assumes is a str.
        bad = sorted(str(k) for k in unknown)
        raise SettingsError(f"settings.yml has unknown key(s): {bad}", key=bad[0])
    out = dict(DEFAULT_SETTINGS)
    out.update(settings)
    if not _is_number(out["discount_pct"]) or not (0 <= out["discount_pct"] < 1):
        raise SettingsError("discount_pct must be a number in [0, 1)", key="discount_pct")
    if not isinstance(out["dbt_target"], str) or not out["dbt_target"]:
        raise SettingsError("dbt_target must be a non-empty string", key="dbt_target")
    # It is spliced into a SQL string literal and a connector user agent, so keep it plain.
    if out["query_source"] is not None and (
        not isinstance(out["query_source"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", out["query_source"])
    ):
        raise SettingsError("query_source must be 1-40 letters, digits, _ or -", key="query_source")
    keys = out["export_tag_keys"]
    if keys is not None and (
        not isinstance(keys, list) or not keys
        or not all(isinstance(k, str) and k.strip() and len(k) <= 128 for k in keys)
    ):
        raise SettingsError("export_tag_keys must be a list of tag names", key="export_tag_keys")
    mandatory = out["mandatory_tag_keys"]
    if not isinstance(mandatory, list) or not (1 <= len(mandatory) <= 5) or not all(
        isinstance(k, str) and k.strip() and len(k) <= 128 for k in mandatory
    ):
        raise SettingsError("mandatory_tag_keys must be a list of 1-5 tag names", key="mandatory_tag_keys")
    admins = out["admin_groups"]
    if not isinstance(admins, list) or len(admins) > 50 or not all(
        isinstance(g, str) and g.strip() and len(g) <= 256 for g in admins
    ):
        raise SettingsError("admin_groups must be a list of up to 50 group names", key="admin_groups")
    if not isinstance(out["include_today"], bool):
        raise SettingsError("include_today must be true or false", key="include_today")
    if out["default_window"] not in WINDOW_CHOICES:
        raise SettingsError(f"default_window must be one of {WINDOW_CHOICES}", key="default_window")
    if not _is_number(out["tag_share_floor"]) or not (0 <= out["tag_share_floor"] <= 1):
        raise SettingsError("tag_share_floor must be a number in [0, 1]", key="tag_share_floor")
    if not _is_number(out["tag_coverage_floor"]) or not (0 <= out["tag_coverage_floor"] <= 1):
        raise SettingsError("tag_coverage_floor must be a number in [0, 1]", key="tag_coverage_floor")
    ui = out["ui"]
    if not isinstance(ui, dict):
        raise SettingsError("ui must be a mapping", key="ui")
    unknown_ui = set(ui) - UI_KEYS
    if unknown_ui:
        bad_ui = sorted(str(k) for k in unknown_ui)
        raise SettingsError(f"ui has unknown key(s): {bad_ui}", key=f"ui.{bad_ui[0]}")
    merged_ui = dict(DEFAULT_SETTINGS["ui"])
    merged_ui.update(ui)
    for key in UI_KEYS:
        if not isinstance(merged_ui[key], int) or isinstance(merged_ui[key], bool) or merged_ui[key] <= 0:
            raise SettingsError(f"ui.{key} must be a positive integer", key=f"ui.{key}")
    out["ui"] = merged_ui
    brand = out["brand"]
    if brand is None:  # a bare `brand:` (all sub-keys commented out) means the defaults
        brand = {}
    if not isinstance(brand, dict):
        raise SettingsError("brand must be a mapping", key="brand")
    unknown_brand = set(brand) - BRAND_KEYS
    if unknown_brand:
        bad_brand = sorted(str(k) for k in unknown_brand)
        raise SettingsError(f"brand has unknown key(s): {bad_brand}", key=f"brand.{bad_brand[0]}")
    merged_brand = dict(DEFAULT_SETTINGS["brand"])
    merged_brand.update(brand)
    for key in BRAND_KEYS:
        if merged_brand[key] is None:  # a bare `contact_email:` in YAML means "none"
            merged_brand[key] = ""
        elif not isinstance(merged_brand[key], str):
            raise SettingsError(f"brand.{key} must be a string", key=f"brand.{key}")
    out["brand"] = merged_brand
    privacy = out["privacy"]
    if privacy is None:  # a bare `privacy:` means the defaults
        privacy = {}
    if not isinstance(privacy, dict):
        raise SettingsError("privacy must be a mapping", key="privacy")
    unknown_privacy = set(privacy) - PRIVACY_KEYS
    if unknown_privacy:
        bad_privacy = sorted(str(k) for k in unknown_privacy)
        raise SettingsError(f"privacy has unknown key(s): {bad_privacy}", key=f"privacy.{bad_privacy[0]}")
    merged_privacy = dict(DEFAULT_SETTINGS["privacy"])
    merged_privacy.update(privacy)
    if not isinstance(merged_privacy["mask_user_identities"], bool):
        raise SettingsError(
            "privacy.mask_user_identities must be true or false", key="privacy.mask_user_identities"
        )
    out["privacy"] = merged_privacy
    load = out["load"]
    if load is None:  # a bare `load:` means the defaults
        load = {}
    if not isinstance(load, dict):
        raise SettingsError("load must be a mapping", key="load")
    unknown_load = set(load) - LOAD_KEYS
    if unknown_load:
        bad_load = sorted(str(k) for k in unknown_load)
        raise SettingsError(f"load has unknown key(s): {bad_load}", key=f"load.{bad_load[0]}")
    merged_load = dict(DEFAULT_SETTINGS["load"])
    merged_load.update(load)
    if not isinstance(merged_load["memory_limit"], str) or not re.fullmatch(r"\d+(\.\d+)?\s*(MB|GB)", merged_load["memory_limit"].strip(), re.I):
        raise SettingsError("load.memory_limit must be a size such as 2GB or 1500MB", key="load.memory_limit")
    for key, top in (("threads", 64), ("top_tag_keys", 1000)):
        v = merged_load[key]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= top):
            raise SettingsError(f"load.{key} must be a whole number from 1 to {top}", key=f"load.{key}")
    tag_keys = merged_load["tag_keys"]
    if tag_keys is not None and (
        not isinstance(tag_keys, list) or not tag_keys
        or not all(isinstance(k, str) and k.strip() and len(k) <= 128 for k in tag_keys)
    ):
        raise SettingsError("load.tag_keys must be a list of tag names", key="load.tag_keys")
    out["load"] = merged_load
    return out


LOCAL_SETTINGS_FILENAME = "settings.local.yml"


def _local_settings_path() -> Path:
    return _config_dir() / LOCAL_SETTINGS_FILENAME


def _load_local_settings() -> dict:
    """config/settings.local.yml (under AUDIT_CONFIG_DIR, gitignored): whichever top-level keys
    the app itself has saved there (save_settings_local) -- never seeded, {} when absent. Merged
    over settings.yml by load_settings so a setting the UI changes never touches the tracked
    settings.yml file."""
    path = _local_settings_path()
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SettingsError(f"settings.local.yml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError("settings.local.yml must be a mapping")
    raw.pop("cost_center_tag_keys", None)  # written by the removed cost-center gear
    return raw


def load_settings() -> dict:
    """Load config/settings.yml (under AUDIT_CONFIG_DIR), then merge config/settings.local.yml's
    own keys on top (only the keys it sets -- everything else still comes from settings.yml).
    Seeds settings.yml with DEFAULT_SETTINGS the first time it is called with no file present --
    this is the one place on the whole board that creates settings.yml (no earlier task does).

    P2-NOBUILD: an invalid file raises SettingsError with `.key`/`.line` filled in wherever they
    can be determined -- a YAML syntax error carries PyYAML's own line (`problem_mark`); a
    semantic error (unknown key, wrong type) comes back from _validate_settings with `.key` set
    but `.line` still None until this function looks it up in the raw text via _find_key_line().
    GET /api/status is the one caller that reads `.key`/`.line`; every existing
    `except app_config.ConfigError` elsewhere still catches this unchanged (SettingsError is a
    ConfigError)."""
    path = _config_dir() / "settings.yml"
    if not path.exists():
        seeded = {
            **DEFAULT_SETTINGS,
            "ui": dict(DEFAULT_SETTINGS["ui"]),
            "brand": dict(DEFAULT_SETTINGS["brand"]),
            "privacy": dict(DEFAULT_SETTINGS["privacy"]),
        }
        save_settings(seeded)
        text = ""
        raw = dict(seeded)
    else:
        text = path.read_text(encoding="utf-8")
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = (mark.line + 1) if mark is not None else None
            raise SettingsError(f"settings.yml is not valid YAML: {exc}", line=line) from exc

    merged = {**raw, **_load_local_settings()}
    try:
        return _validate_settings(merged)
    except SettingsError as exc:
        if exc.line is None and exc.key is not None:
            exc.line = _find_key_line(text, exc.key) if text else None
        raise


def save_settings(settings: dict) -> None:
    """Validate and atomically write settings to config/settings.yml (under AUDIT_CONFIG_DIR)."""
    _atomic_write_yaml(_config_dir() / "settings.yml", _validate_settings(settings))


def save_settings_local(overrides: dict) -> dict:
    """Merges `overrides` over the current local overrides, validates the RESULT against
    settings.yml's own values (so a bad override is rejected before anything is written), then
    atomically writes only the merged overrides to config/settings.local.yml -- settings.yml
    itself is never touched. Returns the new effective (merged) settings."""
    base_path = _config_dir() / "settings.yml"
    base_raw = yaml.safe_load(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    new_local = {**_load_local_settings(), **overrides}
    effective = _validate_settings({**(base_raw or {}), **new_local})
    _atomic_write_yaml(_local_settings_path(), new_local)
    return effective


# ---------------------------------------------------------------------------------------------
# thresholds.yml
# ---------------------------------------------------------------------------------------------

DEFAULT_THRESHOLDS: dict = {"task_cluster_utilization": {"top_n": 100000}}


def _validate_thresholds(thresholds: dict) -> dict:
    if not isinstance(thresholds, dict):
        raise ConfigError("thresholds.yml must be a mapping")
    for key, params in thresholds.items():
        if not isinstance(key, str):
            raise ConfigError("thresholds.yml top-level keys must be strings (query_id or '_all')")
        if not isinstance(params, dict):
            raise ConfigError(f"thresholds.yml[{key!r}] must be a mapping of param -> number")
        for pname, pval in params.items():
            if not isinstance(pname, str):
                raise ConfigError(f"thresholds.yml[{key!r}] has a non-string param name")
            if not _is_number(pval):
                raise ConfigError(f"thresholds.yml[{key!r}][{pname!r}] must be a number")
    return thresholds


def query_source() -> str:
    """The "Source" tag on this app's queries in query history: settings query_source, else the
    brand name with anything but letters, digits, _ and - dropped."""
    try:
        settings = load_settings()
    except ConfigError:
        settings = DEFAULT_SETTINGS
    if settings.get("query_source"):
        return settings["query_source"]
    brand = (settings.get("brand") or {}).get("name") or DEFAULT_SETTINGS["brand"]["name"]
    return re.sub(r"[^A-Za-z0-9_-]", "", brand)[:40] or "DatabricksAudit"


def load_thresholds() -> dict:
    """Load config/thresholds.yml (under AUDIT_CONFIG_DIR). Seeds with DEFAULT_THRESHOLDS (the
    task_cluster_utilization: {top_n: 100000} seed, DEC-29) only when no file is present at all --
    the real repo file (T-05-seeded) is never touched by this branch since it already exists."""
    path = _config_dir() / "thresholds.yml"
    if not path.exists():
        seeded = {k: dict(v) for k, v in DEFAULT_THRESHOLDS.items()}
        save_thresholds(seeded)
        return seeded
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _validate_thresholds(raw)


def save_thresholds(thresholds: dict) -> None:
    """Validate and atomically write thresholds to config/thresholds.yml (under
    AUDIT_CONFIG_DIR). Callers (T-28's Settings page) are responsible for including the existing
    task_cluster_utilization seed in `thresholds` if they load-modify-save; this function does
    not silently re-inject it -- it writes exactly what it is given, once validated."""
    _atomic_write_yaml(_config_dir() / "thresholds.yml", _validate_thresholds(thresholds))


# ---------------------------------------------------------------------------------------------
# materiality.yml -- the per-check materiality-floor register (app/core/materiality.py applies
# these at read time). See config/materiality.yml's own header for which checks were left out and
# why (already gated in the query's own SQL, or a governance/existence signal that matters at any
# size).
# ---------------------------------------------------------------------------------------------

_MATERIALITY_FIELDS = {"column", "min", "unit", "label"}
_COLUMN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

def _load_default_materiality() -> dict:
    """DEFAULT_MATERIALITY, read straight from the repo's OWN config/materiality.yml (ROOT/config,
    never AUDIT_CONFIG_DIR) instead of a second hand-copied dict -- so the seed used for a fresh
    AUDIT_CONFIG_DIR can never drift from the file that already ships in the repo."""
    path = ROOT / "config" / "materiality.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


DEFAULT_MATERIALITY: dict = _load_default_materiality()


def _validate_materiality(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ConfigError("materiality.yml must be a mapping of query_id -> floor")
    from app.core import registry  # local: keeps every OTHER function here free of this import

    known_ids = {s.query_id for s in registry.load_registry()}
    out: dict = {}
    for query_id, entry in data.items():
        if not isinstance(query_id, str) or not query_id:
            raise ConfigError(f"materiality.yml has a non-string or empty query_id key: {query_id!r}")
        if query_id not in known_ids:
            raise ConfigError(
                f"materiality.yml: query_id {query_id!r} is not in app.core.registry (unknown query id)"
            )
        if not isinstance(entry, dict):
            raise ConfigError(f"materiality.yml[{query_id!r}] must be a mapping")
        missing = _MATERIALITY_FIELDS - set(entry)
        if missing:
            raise ConfigError(
                f"materiality.yml[{query_id!r}] is missing field(s): {sorted(missing)}"
            )
        unknown = set(entry) - _MATERIALITY_FIELDS
        if unknown:
            raise ConfigError(
                f"materiality.yml[{query_id!r}] has unknown field(s): {sorted(unknown)}"
            )
        column = entry["column"]
        if not isinstance(column, str) or not _COLUMN_NAME_RE.match(column):
            raise ConfigError(
                f"materiality.yml[{query_id!r}]['column'] must be a lowercase_snake_case "
                f"column name, got {column!r}"
            )
        min_value = entry["min"]
        if not _is_number(min_value) or min_value <= 0:
            raise ConfigError(f"materiality.yml[{query_id!r}]['min'] must be a positive number")
        unit = entry["unit"]
        if not isinstance(unit, str) or not unit.strip():
            raise ConfigError(f"materiality.yml[{query_id!r}]['unit'] must be a non-empty string")
        label = entry["label"]
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"materiality.yml[{query_id!r}]['label'] must be a non-empty string")
        out[query_id] = {"column": column, "min": min_value, "unit": unit, "label": label}
    return out


def load_materiality() -> dict:
    """Load config/materiality.yml (under AUDIT_CONFIG_DIR). Seeds the file with DEFAULT_MATERIALITY
    the first time it is called with no file present -- this module never removes or overwrites an
    existing file's contents on load, only on an explicit save_materiality() call."""
    path = materiality_path()
    if not path.exists():
        seeded = {k: dict(v) for k, v in DEFAULT_MATERIALITY.items()}
        save_materiality(seeded)
        return seeded
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _validate_materiality(raw)


def save_materiality(data: dict) -> None:
    """Validate and atomically write floors to config/materiality.yml (under AUDIT_CONFIG_DIR)."""
    _atomic_write_yaml(materiality_path(), _validate_materiality(data))


# ---------------------------------------------------------------------------------------------
# tag_aliases.yml
# ---------------------------------------------------------------------------------------------

TOP_TAGS_SHOWN = 4
# a top tag becomes a dims.dim_workspace column and a query parameter, so it must not shadow one
_TOP_TAG_RESERVED = {
    "workspace_id", "name", "url", "env", "env_source", "env_reason", "in_snapshot_region",
    "billed_in_snapshot", "window", "workspace_ids", "tag", "tag_key", "tag_value", "status",
    "limit", "offset", "job_id", "group",
}
DEFAULT_TOP_TAGS = {"cost_center": "Cost center", "domain": "Domain", "team": "Team", "business_unit": "Business unit"}


def _validate_tag_aliases(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != {"top_tags"}:
        raise ConfigError("tag_aliases.yml must have exactly one top-level key: 'top_tags'")
    top = data["top_tags"]
    if not isinstance(top, dict):
        raise ConfigError("tag_aliases.yml['top_tags'] must be a mapping of tag key to name")
    out: dict = {}
    for key, label in top.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", key) or key in _TOP_TAG_RESERVED:
            raise ConfigError(f"tag_aliases.yml['top_tags'] key {key!r} must be lower case with underscores and not a reserved name")
        if label is not None and not isinstance(label, str):
            raise ConfigError(f"tag_aliases.yml['top_tags'][{key!r}] must be a name or empty")
        out[key] = label.strip() if label and label.strip() else key
    return {"top_tags": out}


def load_tag_aliases() -> dict:
    """Load config/tag_aliases.yml (under AUDIT_CONFIG_DIR), seeding DEFAULT_TOP_TAGS when absent."""
    path = _config_dir() / "tag_aliases.yml"
    if not path.exists():
        seeded = {"top_tags": dict(DEFAULT_TOP_TAGS)}
        save_tag_aliases(seeded)
        return seeded
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _validate_tag_aliases(raw)


def load_top_tags() -> dict[str, str]:
    """{tag key: name shown}, in file order; {} on a broken file so callers never crash."""
    try:
        return load_tag_aliases()["top_tags"]
    except ConfigError:
        return {}


def save_tag_aliases(data: dict) -> None:
    """Validate and atomically write tag aliases to config/tag_aliases.yml (under
    AUDIT_CONFIG_DIR)."""
    _atomic_write_yaml(_config_dir() / "tag_aliases.yml", _validate_tag_aliases(data))


# ---------------------------------------------------------------------------------------------
# dbt/seeds/workspace_env_overrides.csv (DEC-30) -- NOT under AUDIT_CONFIG_DIR
# ---------------------------------------------------------------------------------------------

ENV_OVERRIDE_COLUMNS = ["workspace_id", "env", "name", "url", "note"]
ENV_OVERRIDE_VALID_ENVS = {"prod", "uat", "dev", "unknown"}


def _env_overrides_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return ROOT / "dbt" / "seeds" / "workspace_env_overrides.csv"


def read_env_overrides(path: Path | str | None = None) -> list[dict]:
    """Every row of dbt/seeds/workspace_env_overrides.csv (or `path`, for a tmp_path round-trip
    test, DEC-26) as a list of dicts with exactly the ENV_OVERRIDE_COLUMNS keys. [] when the file
    does not exist (a fresh checkout before T-23's header-only seed has been written)."""
    csv_path = _env_overrides_path(path)
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def write_env_overrides(rows: list[dict], path: Path | str | None = None) -> None:
    """Atomically write `rows` to dbt/seeds/workspace_env_overrides.csv (or `path`). Every row's
    keys must be a subset of ENV_OVERRIDE_COLUMNS; a non-empty `env` must be one of the seed's
    known values (prod | uat | dev | unknown, DEC-22). Missing columns are written empty."""
    csv_path = _env_overrides_path(path)
    for row in rows:
        unknown = set(row) - set(ENV_OVERRIDE_COLUMNS)
        if unknown:
            raise ConfigError(f"workspace_env_overrides row has unknown column(s): {sorted(unknown)}")
        env_value = row.get("env")
        if env_value and env_value not in ENV_OVERRIDE_VALID_ENVS:
            raise ConfigError(
                f"workspace_env_overrides env must be one of {sorted(ENV_OVERRIDE_VALID_ENVS)}, "
                f"got {env_value!r}"
            )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(csv_path.parent), prefix=f".{csv_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            # csv.DictWriter defaults to "\r\n" (RFC 4180) regardless of the file's own newline
            # mode -- the repo's seed and .gitattributes are LF-only, so this must be explicit.
            writer = csv.DictWriter(fh, fieldnames=ENV_OVERRIDE_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in ENV_OVERRIDE_COLUMNS})
        os.replace(tmp_name, csv_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
