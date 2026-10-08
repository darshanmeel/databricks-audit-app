#!/usr/bin/env python3
"""tools/generate_direct_models.py -- every executable query -> a Databricks-direct dbt model
(item DBX-DIRECT).

    python tools/generate_direct_models.py                       every executable query
    python tools/generate_direct_models.py --only <id> [<id>..]  just those query ids
    python tools/generate_direct_models.py --domain <name>       just that domain
    python tools/generate_direct_models.py --check [scope]       exit 1 listing stale / missing /
                                                                  extra files
    python tools/generate_direct_models.py --force               overwrite a model / yml file that
                                                                  lacks the marker (a hand-written
                                                                  file in the way is otherwise
                                                                  refused)

This is a second, narrower generator that sits next to tools/generate_models.py (imported here,
never re-implemented): for every executable query in the registry -- the exact same set
generate_models.py builds, skipped for the exact same reason -- it writes ONE more dbt model,
dbt/models/databricks_direct/<domain>/d_<query_id>.sql, whose body is byte-for-byte the Databricks
branch tools/generate_models.py already renders (`render_model(spec, target="databricks")`): same
translation (app.core.translate.pin_time), same source() substitution, same param()/window-loop
handling, same output columns as f_<query_id> -- so a `d_<query_id>` table (direct_mode="table")
is a drop-in replacement for the matching `f_<query_id>` finding table.

Only the model's own config differs (the point of this generator): instead of the findings
models' `materialized='table'`, every generated file here carries

    {{ config(enabled=(target.type == "databricks"),
              materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"),
              schema="audit_direct",
              tags=[<same tags as the f_ model>, "databricks_direct"]) }}

`enabled=(target.type == "databricks")` keeps every file here a no-op on the `dev`/`test` (DuckDB)
targets this repo builds day to day -- `dbt parse`/`dbt build --target test` disables the whole
node, never touches the databricks-only Jinja branch app.core.translate.pin_time produced.
`direct_mode` (a --vars flag) picks the materialization: `dbt/macros/direct_check.sql`'s custom
"direct_check" materialization for "run"/"explain" (creates nothing; that macro's own header
explains why a materialization, not a run-operation), or dbt's own built-in `table` materialization
for "table" (the drop-in-replacement path, results land in
`<AUDIT_DBX_CATALOG>.audit_direct.d_<query_id>`).

Also writes ONE combined dbt/models/databricks_direct/_databricks_direct.yml (every model, across
every domain, unlike generate_models.py's one-yml-per-domain) so `dbt docs generate` describes
every direct model, and dbt/models/databricks_direct/generated_manifest.json (mirrors
dbt/models/findings/generated_manifest.json's shape), mapping a dbt unique_id back to its
query_id/domain.

Outputs (LF, byte-identical on rerun, no timestamps):
  dbt/models/databricks_direct/<domain>/d_<query_id>.sql
  dbt/models/databricks_direct/_databricks_direct.yml
  dbt/models/databricks_direct/generated_manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import generate_models as gm  # noqa: E402 -- reused, never re-implemented (see below)
from app.core.registry import QuerySpec, load_registry  # noqa: E402

DIRECT_DIR = ROOT / "dbt" / "models" / "databricks_direct"
YML_NAME = "_databricks_direct.yml"
MANIFEST_NAME = "generated_manifest.json"
SOURCES_YML = gm.SOURCES_YML

# Every generated .sql file's second line carries this marker (see _origin_comment below); _write
# below refuses to overwrite an existing file that lacks it, the same hand-written-file guard
# tools/generate_models.py applies to dbt/models/findings/**.
GENERATED_MARK = "fix the source query and regenerate, never edit this file"

GenerationError = gm.GenerationError
skip_reason = gm.skip_reason


# ------------------------------------------------------------------------------------------------
# naming / tags / rendering
# ------------------------------------------------------------------------------------------------


def direct_model_name(spec: QuerySpec) -> str:
    return f"d_{spec.query_id}"


def direct_tags(spec: QuerySpec) -> list[str]:
    return gm.model_tags(spec) + ["databricks_direct"]


def _origin_comment(spec: QuerySpec) -> str:
    return f"-- generated from {spec.path}; {GENERATED_MARK}."


def render_direct_model(spec: QuerySpec, contract: dict[str, set[str]] | None = None) -> str:
    """The dbt model text for dbt/models/databricks_direct/<domain>/d_<query_id>.sql.

    Reuses tools.generate_models.render_model(spec, target="databricks") for everything that is
    actually query translation (render_branches -> app.core.translate.pin_time, source()
    substitution, the window loop, param()/sentinel expansion) -- that call's return value is
    `<config line>\\n<GENERATED comment line>\\n<body>`; only the first two lines are replaced
    here with this generator's own config/comment, the body (third line on) is passed through
    completely unchanged.
    """
    if not spec.executable:
        raise GenerationError(f"{spec.query_id}: not executable ({skip_reason(spec)})")
    contract = contract if contract is not None else gm.load_source_contract()
    full = gm.render_model(spec, target="databricks", contract=contract)
    _, _, body = full.split("\n", 2)
    tags = ", ".join(f"'{t}'" for t in direct_tags(spec))
    config_line = (
        '{{ config(enabled=(target.type == "databricks"), '
        'materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), '
        f'schema="audit_direct", tags=[{tags}]) }}}}'
    )
    return config_line + "\n" + _origin_comment(spec) + "\n" + body


def direct_model_entry(spec: QuerySpec) -> dict:
    return {"name": direct_model_name(spec), "description": spec.title,
            "meta": {"query_id": spec.query_id, "domain": spec.domain,
                     "finding_model": gm.model_name(spec)}}


def render_yml(specs: list[QuerySpec]) -> str:
    ordered = sorted(specs, key=lambda s: s.query_id)
    doc = {"version": 2, "models": [direct_model_entry(s) for s in ordered]}
    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=100, allow_unicode=False)
    return (
        f"# GENERATED by tools/generate_direct_models.py -- {GENERATED_MARK}.\n" + text
    )


# ------------------------------------------------------------------------------------------------
# files, manifest, check (mirrors tools/generate_models.py's shape, minus lock-commit/grains --
# scope here is always exactly "every executable query in the registry", there is no vendored-lock
# origin note or grain-driven data test to track)
# ------------------------------------------------------------------------------------------------


def model_path(spec: QuerySpec, out_dir: Path) -> Path:
    return out_dir / spec.domain / f"{direct_model_name(spec)}.sql"


def yml_path(out_dir: Path) -> Path:
    return out_dir / YML_NAME


def _rel(path: Path, out_dir: Path = DIRECT_DIR) -> str:
    for base in (ROOT, out_dir):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def _manifest_model(spec: QuerySpec, out_dir: Path) -> dict:
    return {
        "query_id": spec.query_id,
        "domain": spec.domain,
        "model": direct_model_name(spec),
        "path": _rel(model_path(spec, out_dir), out_dir),
        "finding_model": gm.model_name(spec),
        "tags": direct_tags(spec),
    }


def load_manifest(out_dir: Path) -> dict:
    path = out_dir / MANIFEST_NAME
    if not path.exists():
        return {"models": [], "skipped": []}
    return json.loads(path.read_text(encoding="utf-8"))


def build_manifest(existing: dict, scope: list[QuerySpec], registry: dict[str, QuerySpec]) -> dict:
    """The manifest after this run -- same merge rule as generate_models.build_manifest: entries
    for ids outside `scope` are kept (re-described from the registry when the id still exists),
    entries for ids in scope are recomputed. Lets --only/--domain regenerate a subset without
    dropping every other id's file from the recorded set."""
    scope_ids = {s.query_id for s in scope}
    models: dict[str, dict] = {}
    skipped: dict[str, dict] = {}
    for m in existing.get("models", []):
        qid = m["query_id"]
        if qid in scope_ids or qid not in registry:
            continue
        spec = registry[qid]
        if spec.executable:
            models[qid] = _manifest_model(spec, DIRECT_DIR)
    for s in existing.get("skipped", []):
        qid = s["query_id"]
        if qid in scope_ids or qid not in registry or qid in models:
            continue
        reason = skip_reason(registry[qid])
        if reason:
            skipped[qid] = {"query_id": qid, "domain": registry[qid].domain, "reason": reason}
    for spec in scope:
        reason = skip_reason(spec)
        if reason:
            skipped[spec.query_id] = {"query_id": spec.query_id, "domain": spec.domain, "reason": reason}
        else:
            models[spec.query_id] = _manifest_model(spec, DIRECT_DIR)
    return {
        "models": [models[k] for k in sorted(models)],
        "skipped": [skipped[k] for k in sorted(skipped)],
    }


def manifest_text(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def _write(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        # The marker lives on line 2 (the origin comment) of every generated .sql file, and on
        # line 1 of the yml. A byte-offset slice (the earlier [:400]) can split the marker in half
        # depending on how long the config line/tag list happens to be for a given query, which
        # would make this guard refuse a perfectly-generated file. Checking whole lines instead
        # is immune to that.
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:3])
        if GENERATED_MARK not in head:
            raise GenerationError(
                f"{_rel(path)} exists and lacks the generated-file marker; refusing to overwrite "
                "a hand-written file (use --force)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data and not force:
        return
    path.write_bytes(data)


def select_scope(registry: dict[str, QuerySpec], only: list[str] | None, domain: str | None) -> list[QuerySpec]:
    if only:
        unknown = [q for q in only if q not in registry]
        if unknown:
            raise GenerationError(f"unknown query id(s): {unknown}")
        return [registry[q] for q in only]
    if domain:
        specs = [s for s in registry.values() if s.domain == domain]
        if not specs:
            raise GenerationError(f"no query has domain {domain!r}")
        return specs
    return list(registry.values())


def generate(
    only: list[str] | None = None,
    domain: str | None = None,
    *,
    force: bool = False,
    out_dir: Path = DIRECT_DIR,
    sources_yml: Path = SOURCES_YML,
    registry: list[QuerySpec] | None = None,
) -> dict:
    specs = registry if registry is not None else load_registry()
    reg = {s.query_id: s for s in specs}
    scope = select_scope(reg, only, domain)
    contract = gm.load_source_contract(sources_yml)
    manifest = build_manifest(load_manifest(out_dir), scope, reg)
    written: list[str] = []
    for spec in scope:
        if skip_reason(spec):
            continue
        _write(model_path(spec, out_dir), render_direct_model(spec, contract), force)
        written.append(spec.query_id)
    all_specs = [reg[m["query_id"]] for m in manifest["models"]]
    _write(yml_path(out_dir), render_yml(all_specs), force)
    _write(out_dir / MANIFEST_NAME, manifest_text(manifest), True)
    skipped_in_scope = [
        {"query_id": s.query_id, "reason": skip_reason(s)} for s in scope if skip_reason(s)
    ]
    return {"models": written, "skipped": skipped_in_scope, "yml": _rel(yml_path(out_dir), out_dir)}


def check(
    only: list[str] | None = None,
    domain: str | None = None,
    *,
    out_dir: Path = DIRECT_DIR,
    sources_yml: Path = SOURCES_YML,
    registry: list[QuerySpec] | None = None,
) -> dict:
    """Compare what is on disk with what the generator would write. Returns
    {'ok', 'stale', 'missing', 'extra', 'models', 'skipped'} -- same shape as
    tools.generate_models.check(). Unscoped: every model the manifest records (plus the shared yml
    and the manifest itself) must be present and byte-identical, and no unrecorded .sql/.yml may
    sit under the direct-models dir. Scoped (--only/--domain): the same for the ids in scope; the
    yml is still checked against the FULL manifest, since it always describes every model."""
    specs = registry if registry is not None else load_registry()
    reg = {s.query_id: s for s in specs}
    contract = gm.load_source_contract(sources_yml)
    manifest = build_manifest(load_manifest(out_dir), [], reg)
    manifest_ids = {m["query_id"] for m in manifest["models"]}

    stale: list[str] = []
    missing: list[str] = []
    extra: list[str] = []
    skipped: list[dict] = []
    expected: dict[Path, str] = {}

    if only or domain:
        scope = select_scope(reg, only, domain)
        scope_models = [s for s in scope if not skip_reason(s)]
        skipped = [{"query_id": s.query_id, "reason": skip_reason(s)} for s in scope if skip_reason(s)]
        for spec in scope_models:
            if spec.query_id not in manifest_ids:
                missing.append(_rel(model_path(spec, out_dir), out_dir) + " (never generated)")
            else:
                expected[model_path(spec, out_dir)] = render_direct_model(spec, contract)
        n_models = len(scope_models)
    else:
        for m in manifest["models"]:
            expected[model_path(reg[m["query_id"]], out_dir)] = render_direct_model(reg[m["query_id"]], contract)
        skipped = [{"query_id": s["query_id"], "reason": s["reason"]} for s in manifest["skipped"]]
        n_models = len(manifest["models"])

    if manifest["models"]:
        expected[yml_path(out_dir)] = render_yml([reg[m["query_id"]] for m in manifest["models"]])
    expected[out_dir / MANIFEST_NAME] = manifest_text(manifest)

    for path, text in expected.items():
        if not path.exists():
            missing.append(_rel(path, out_dir))
        elif path.read_bytes() != text.encode("utf-8"):
            stale.append(_rel(path, out_dir))

    known_paths = {model_path(reg[m["query_id"]], out_dir) for m in manifest["models"]}
    known_paths.add(yml_path(out_dir))
    known_paths.add(out_dir / MANIFEST_NAME)
    if out_dir.exists():
        for path in sorted(out_dir.rglob("*")):
            if path.is_file() and path.suffix in (".sql", ".yml", ".yaml", ".json") and path not in known_paths:
                extra.append(_rel(path, out_dir))

    return {
        "ok": not (stale or missing or extra),
        "stale": stale,
        "missing": missing,
        "extra": extra,
        "models": n_models,
        "skipped": skipped,
    }


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--only", nargs="+", metavar="ID", help="only these query ids")
    scope.add_argument("--domain", metavar="NAME", help="only this domain")
    ap.add_argument("--check", action="store_true", help="verify, do not write; exit 1 on any drift")
    ap.add_argument("--force", action="store_true", help="overwrite files lacking the generated-file marker")
    args = ap.parse_args(argv)
    try:
        if args.check:
            r = check(args.only, args.domain)
            for s in r["skipped"]:
                print(f"skipped: {s['query_id']} - {s['reason']}")
            for p in r["stale"]:
                print(f"stale: {p}")
            for p in r["missing"]:
                print(f"missing: {p}")
            for p in r["extra"]:
                print(f"extra: {p}")
            if r["ok"]:
                print(f"direct models in sync ({r['models']} models, {len(r['skipped'])} skipped)")
                return 0
            print(
                f"direct models out of sync: {len(r['stale'])} stale, {len(r['missing'])} missing, "
                f"{len(r['extra'])} extra -- rerun python tools/generate_direct_models.py",
                file=sys.stderr,
            )
            return 1
        r = generate(args.only, args.domain, force=args.force)
        for s in r["skipped"]:
            print(f"skipped: {s['query_id']} - {s['reason']}")
        print(f"wrote {len(r['models'])} direct models, {r['yml']}, {MANIFEST_NAME}")
        return 0
    except GenerationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
