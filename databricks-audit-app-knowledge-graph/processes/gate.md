# Process: the gate (tools/gate.py)

`tools/gate.py` runs the repo's checks in two modes: `--quick` (lint, generated-file sync, web, pytest) and full (fixtures, dbt test build, pytest, fixture coverage); it prints a summary and exits 0 only when every step passed or was skipped.

```mermaid
flowchart TB
  M{python tools/gate.py}
  M -->|--quick| Q1[lint_headers<br/>tools/lint_headers.py]
  Q1 --> Q2[sync_queries<br/>sync_queries.py --check]
  Q2 --> Q3[generate_models<br/>generate_models.py --check]
  Q3 --> Q4[generate_direct_models<br/>generate_direct_models.py --check]
  Q4 --> Q5[build_direct_sql<br/>build_direct_sql.py --check]
  Q5 --> Q6[web<br/>npm typecheck + test, if web/node_modules]
  Q6 --> Q7[pytest -q]
  M -->|no flag| F1[build_fixtures<br/>tests/fixtures/build_fixtures.py]
  F1 --> F2[dbt_build_test<br/>dbt_run.py build --target test]
  F2 --> F3[pytest -q]
  F3 --> F4[fixture_coverage --check]
  Q7 --> S[summary line, GATE OK or exit 1]
  F4 --> S
```

## Steps

| Step | Mode | Command | Skips when | Evidence |
|---|---|---|---|---|
| `lint_headers` | quick | `python tools/lint_headers.py` | never | `tools/gate.py:56-60` |
| `sync_queries` | quick | `python tools/sync_queries.py --check` | `tools/sync_queries.py` missing | `tools/gate.py:63-72` |
| `generate_models` | quick | `python tools/generate_models.py --check` | script missing | `tools/gate.py:75-84` |
| `generate_direct_models` | quick | `python tools/generate_direct_models.py --check` | script missing | `tools/gate.py:87-96` |
| `build_direct_sql` | quick | `python tools/build_direct_sql.py --check` | never | `tools/gate.py:99-102` |
| `web` | quick | `npm --prefix web run typecheck` then `npm --prefix web test` | no `npm` or no `web/node_modules` | `tools/gate.py:105-115` |
| `pytest` | both | `python -m pytest -q`; exit 5 (no tests) counts as ok | never | `tools/gate.py:118-127` |
| `build_fixtures` | full | `python tests/fixtures/build_fixtures.py` | builder script missing | `tools/gate.py:130-139` |
| `dbt_build_test` | full | `python tools/dbt_run.py build --target test` | `dbt/dbt_project.yml` missing | `tools/gate.py:142-156` |
| `fixture_coverage` | full | `python tools/fixture_coverage.py --check` | script missing | `tools/gate.py:159-170` |

- Order and modes: `quick()` `tools/gate.py:182-192`, `full()` `tools/gate.py:195-202`, arg parsing `tools/gate.py:205-213`.
- Every step runs even after a failure (`ok &= ...`); result printed as `gate summary: name=status ...` then `GATE OK` or exit 1 (`tools/gate.py:173-179`).
- Commands stream live from the repo root (`tools/gate.py:49-53`).

## What each check proves

- Headers valid and app manifest in sync (`tools/lint_headers.py:4-11`).
- Vendored tree matches `config/vendored.lock` (`tools/sync_queries.py:58-63`).
- dbt `f_`/`d_` models and `app/direct_sql` match their sources (`tools/generate_models.py:7`, `tools/generate_direct_models.py:7-8`, `tools/build_direct_sql.py:3`).
- Built test DB matches fixtures and every executable check has a grain and a test (`tools/fixture_coverage.py:1-30`).
- pytest covers dashboards, web dist, API, tags, etc. (see `processes/tests.md`).

## Notes

- `--quick` needs `tests/db_audit_test.duckdb` from a full run (`README.md:747`); full takes about 30 min (`README.md:743`).
- Full mode does not run lint, sync or the generator `--check` steps; run both modes before a commit (inferred from `tools/gate.py:195-202`).
- The existence probes (`skip: ... not present yet`) date from the build-up phase; all probed files exist now, so they never skip in this checkout (inferred; the 2026-11-10 review calls them dead, `reviews/review-2026-11-10-repo.html:114`).
