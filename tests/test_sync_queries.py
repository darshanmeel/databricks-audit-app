"""Tests for tools/sync_queries.py (DEC-04).

Builds a tiny synthetic source clone under tmp_path (its own git repo, committed inline via
subprocess) so this test never depends on the real crosshire-audit-databricks-admin clone's
state. Every scenario runs the tool as a real subprocess (matching how it is actually invoked)
against an isolated output root, selected via the AUDIT_APP_ROOT env var that sync_queries.py
reads for test isolation (see its module docstring) so no test ever writes this repo's own
app/queries/vendored/ or config/vendored.lock.

Covers: the dirty-tree refusal without --allow-dirty, --allow-dirty proceeding and vendoring only
*.sql (never a domain README.md) plus the three top-level files and lineage/sources.yml, the
DEC-03 lock shape (now DEC-66.2-extended with a `diverged` map), --check passing on a matching
tree and failing on a tampered one, the --source / AUDIT_QUERY_SOURCE default-fallback, the
newline-normalised sha256 hashing rule, and (T-75L, DEC-66.2) --relock refreshing `files`/
`diverged` from the current tree via the app root's OWN git log, plus --commit refusing (then,
with --force, allowing) to clobber a file this repo has fixed in place.

Also covers the T-75L review-fix round: --commit refusing to let an upstream file *removal* erase
an in-place fix (both from live git-log divergence and from the lock's already-recorded
`diverged`, e.g. when this repo's own .git is unavailable); --relock requiring --item to fold an
uncommitted in-place fix into `diverged` before that fix has a commit of its own for `git log` to
read; and `diverged`'s item ordering being oldest-first even when the same file is touched more
than once under different (or repeated) item ids.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = ROOT / "tools" / "sync_queries.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_queries_under_test", SYNC_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


SYNC = _load_module()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )


def make_source_repo(tmp_path: Path) -> Path:
    """A tiny fake source clone shaped like the real one: two domains' worth of *.sql (plus a
    README.md that must never be vendored), queries/manifest.json, lineage/sources.yml,
    databricks_system_catalog_schema.txt and LICENSE, committed to a fresh local git repo."""
    src = tmp_path / "source_repo"
    (src / "queries" / "compute").mkdir(parents=True)
    (src / "queries" / "cost").mkdir(parents=True)
    (src / "lineage").mkdir(parents=True)

    (src / "queries" / "compute" / "foo_query.sql").write_text(
        "-- query_id: foo_query\nSELECT 1 AS x\n", encoding="utf-8", newline="\n"
    )
    (src / "queries" / "compute" / "README.md").write_text(
        "not a query, must never be vendored\n", encoding="utf-8"
    )
    (src / "queries" / "cost" / "bar_query.sql").write_text(
        "-- query_id: bar_query\r\nSELECT 2 AS y\r\n", encoding="utf-8", newline=""
    )
    (src / "queries" / "manifest.json").write_text('{"ok": true}\n', encoding="utf-8")
    (src / "lineage" / "sources.yml").write_text("sources: []\n", encoding="utf-8")
    (src / "databricks_system_catalog_schema.txt").write_text(
        "CATALOGS available:\n", encoding="utf-8"
    )
    (src / "LICENSE").write_text("MIT License\n", encoding="utf-8")

    _git(src, "init", "-q")
    _git(src, "config", "user.email", "test@example.com")
    _git(src, "config", "user.name", "Test")
    _git(src, "remote", "add", "origin", "https://example.invalid/test/source.git")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "initial")
    return src


def _head_sha(src: Path) -> str:
    return _git(src, "log", "-1", "--format=%H").stdout.strip()


def _rmtree_git_dir(path: Path) -> None:
    """shutil.rmtree(path) alone can raise PermissionError on Windows: git leaves its object
    files read-only, and Windows honors that bit for delete too. Clear it on the way through."""

    def _onerror(func, p, exc_info):
        os.chmod(p, 0o666)
        func(p)

    shutil.rmtree(path, onerror=_onerror)


def _init_app_root_git(app_root: Path) -> None:
    """DEC-66.2 (T-75L): give the already-vendored app_root its own git history, standing in for
    the real audit-app repo's own commits, so compute_diverged()'s `git -C <app_root> log` has
    something to read."""
    _git(app_root, "init", "-q")
    _git(app_root, "config", "user.email", "test@example.com")
    _git(app_root, "config", "user.name", "Test")
    _git(app_root, "add", "-A")
    _git(app_root, "commit", "-q", "-m", "T-02: vendor the library")


def _run_sync(app_root: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AUDIT_APP_ROOT"] = str(app_root)
    env.pop("AUDIT_QUERY_SOURCE", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


def test_dirty_tree_refused_without_allow_dirty(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    (src / "queries" / "compute" / "foo_query.sql").write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x\n", encoding="utf-8", newline="\n"
    )

    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))

    assert r.returncode != 0, r.stdout + r.stderr
    combined = (r.stdout + r.stderr).lower()
    assert "dirty" in combined or "uncommitted" in combined
    assert not (app_root / "config" / "vendored.lock").exists()


def test_allow_dirty_proceeds_and_vendors_expected_files(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    (src / "queries" / "compute" / "foo_query.sql").write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x\n", encoding="utf-8", newline="\n"
    )
    (src / "queries" / "extra_untracked.txt").write_text("untracked\n", encoding="utf-8")

    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src), "--allow-dirty")
    assert r.returncode == 0, r.stdout + r.stderr

    vendored = app_root / "app" / "queries" / "vendored"
    assert (vendored / "compute" / "foo_query.sql").exists()
    assert (vendored / "cost" / "bar_query.sql").exists()
    assert not (vendored / "compute" / "README.md").exists()
    assert (vendored / "manifest.json").exists()
    assert (vendored / "lineage" / "sources.yml").exists()
    assert (vendored / "databricks_system_catalog_schema.txt").exists()
    assert (vendored / "LICENSE").exists()

    lock = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock["dirty"] is True
    assert lock["commit"] == sha
    assert lock["counts"]["queries"] == 2
    assert lock["counts"]["by_domain"]["compute"] == 1
    assert lock["counts"]["by_domain"]["cost"] == 1
    assert any("foo_query.sql" in f for f in lock["dirty_files"])


def test_allow_dirty_requires_commit_to_match_head(tmp_path):
    src = make_source_repo(tmp_path)
    (src / "queries" / "compute" / "foo_query.sql").write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x\n", encoding="utf-8", newline="\n"
    )

    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", "0" * 40, "--source", str(src), "--allow-dirty")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "HEAD" in (r.stdout + r.stderr)


def test_lock_shape_matches_dec03(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    lock_path = app_root / "config" / "vendored.lock"
    raw = lock_path.read_text(encoding="utf-8")
    lock = json.loads(raw)

    assert set(lock.keys()) == {
        "url", "branch", "commit", "dirty", "dirty_files",
        "synced_at", "counts", "files", "diverged",
    }
    assert isinstance(lock["url"], str) and lock["url"]
    assert isinstance(lock["branch"], str) and lock["branch"]
    assert lock["commit"] == sha
    assert lock["dirty"] is False
    assert lock["dirty_files"] == []
    assert set(lock["counts"].keys()) == {"queries", "by_domain"}
    assert lock["counts"]["queries"] == 2
    assert set(lock["counts"]["by_domain"].keys()) == set(SYNC.DOMAINS)
    for digest in lock["files"].values():
        assert isinstance(digest, str) and len(digest) == 64
    # app_root is not (yet) a git checkout in this test, so compute_diverged() finds no history
    # to read and this is DEC-66.2's documented {} (never an error).
    assert lock["diverged"] == {}

    # json.dumps(..., sort_keys=True) => the serialized top-level keys are lexicographically
    # sorted (DEC-03 "JSON, sorted keys").
    assert list(lock.keys()) == sorted(lock.keys())


def test_check_passes_on_matching_tree_then_fails_when_tampered(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    ok = _run_sync(app_root, "--check")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout.strip() == (
        f"vendored tree matches config/vendored.lock (2 queries, commit {sha})"
    )

    tampered = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    tampered.write_text("-- query_id: foo_query\nSELECT 999 AS x\n", encoding="utf-8", newline="\n")

    bad = _run_sync(app_root, "--check")
    assert bad.returncode != 0, bad.stdout + bad.stderr
    assert "foo_query.sql" in (bad.stdout + bad.stderr)


def test_source_and_env_var_fallback(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"

    r = _run_sync(app_root, "--commit", sha, extra_env={"AUDIT_QUERY_SOURCE": str(src)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql").exists()


def test_sha256_normalized_ignores_crlf_vs_lf():
    lf = b"line1\nline2\n"
    crlf = b"line1\r\nline2\r\n"
    assert SYNC.sha256_normalized(lf) == SYNC.sha256_normalized(crlf)


def test_relock_refreshes_hashes_and_records_divergence_from_git_log(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    # An in-place fix, committed under an item id -- exactly how a real T-NN fix round commits
    # app/queries/vendored/**.
    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- fixed in place\n", encoding="utf-8", newline="\n"
    )
    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-99: fix foo_query's off-by-one")

    # --check now fails: config/vendored.lock still records the pre-fix hash.
    stale = _run_sync(app_root, "--check")
    assert stale.returncode != 0, stale.stdout + stale.stderr
    assert "foo_query.sql" in (stale.stdout + stale.stderr)

    relocked = _run_sync(app_root, "--relock")
    assert relocked.returncode == 0, relocked.stdout + relocked.stderr
    # --relock never writes app/queries/vendored/ itself -- the fix (and only the fix) is
    # exactly what's still there.
    assert fixed_path.read_text(encoding="utf-8").endswith("-- fixed in place\n")
    assert not (app_root / "app" / "queries" / "vendored" / "compute" / "README.md").exists()

    lock = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    # upstream provenance is carried over unchanged by --relock.
    assert lock["commit"] == sha
    assert lock["dirty"] is False
    assert lock["url"] and lock["branch"]
    assert lock["files"]["compute/foo_query.sql"] == SYNC.sha256_normalized(fixed_path.read_bytes())
    assert lock["diverged"] == {"compute/foo_query.sql": ["T-99"]}
    assert "cost/bar_query.sql" not in lock["diverged"]

    ok = _run_sync(app_root, "--check")
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_relock_requires_existing_lock(tmp_path):
    app_root = tmp_path / "app_root"
    app_root.mkdir()
    r = _run_sync(app_root, "--relock")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "vendored.lock" in (r.stdout + r.stderr)


def test_check_and_relock_and_commit_are_mutually_exclusive(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--check", "--relock")
    assert r.returncode == 2, r.stdout + r.stderr
    r2 = _run_sync(app_root, "--commit", sha, "--relock", "--source", str(src))
    assert r2.returncode == 2, r2.stdout + r2.stderr


def test_commit_refuses_to_overwrite_diverged_file_without_force(tmp_path):
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- fixed in place\n", encoding="utf-8", newline="\n"
    )
    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-99: fix foo_query's off-by-one")

    blocked = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    combined = blocked.stdout + blocked.stderr
    assert "foo_query.sql" in combined and "T-99" in combined
    # the fix survives the refused write untouched.
    assert "fixed in place" in fixed_path.read_text(encoding="utf-8")
    # bar_query.sql (never fixed) is not part of the refusal and is not itself reported.
    assert "bar_query.sql  (changed by" not in combined

    forced = _run_sync(app_root, "--commit", sha, "--source", str(src), "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert "fixed in place" not in fixed_path.read_text(encoding="utf-8")


def test_commit_refuses_when_diverged_file_removed_upstream(tmp_path):
    """Review fix (a): a file fixed in place here that the incoming upstream commit no longer
    ships at all must block the write just like a changed-upstream file does -- not fall through
    the `rel not in new_content: continue` hole and get silently deleted by the rmtree()."""
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- fixed in place\n", encoding="utf-8", newline="\n"
    )
    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-99: fix foo_query's off-by-one")

    # Upstream drops the file entirely in a later commit.
    _git(src, "rm", "-q", "queries/compute/foo_query.sql")
    _git(src, "commit", "-q", "-m", "drop foo_query")
    sha2 = _head_sha(src)

    blocked = _run_sync(app_root, "--commit", sha2, "--source", str(src))
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    combined = blocked.stdout + blocked.stderr
    assert "foo_query.sql" in combined and "T-99" in combined
    assert fixed_path.is_file()

    forced = _run_sync(app_root, "--commit", sha2, "--source", str(src), "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert not fixed_path.is_file()


def test_commit_refuses_using_recorded_divergence_when_git_history_unavailable(tmp_path):
    """Review fix (b): the guard must not depend solely on this repo's live `git log` -- with no
    git history to read (deleted .git, non-git export, shallow clone), the lock's own recorded
    `diverged` map (from the last --relock) must still block the overwrite."""
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- fixed in place\n", encoding="utf-8", newline="\n"
    )
    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-99: fix foo_query's off-by-one")

    relocked = _run_sync(app_root, "--relock")
    assert relocked.returncode == 0, relocked.stdout + relocked.stderr

    _rmtree_git_dir(app_root / ".git")

    blocked = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    combined = blocked.stdout + blocked.stderr
    assert "foo_query.sql" in combined and "T-99" in combined


def test_relock_requires_item_for_uncommitted_change_then_reconciles_after_commit(tmp_path):
    """Review fix: the one-commit-per-item workflow relocks BEFORE committing the fix, so the
    fix isn't in `git log` yet. Plain --relock must refuse and name the file; --relock --item
    <T-id> must fold that id into `diverged`; and once the fix is actually committed, a later
    plain --relock must read the same id back from git log and leave the map unchanged."""
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- fixed in place\n", encoding="utf-8", newline="\n"
    )
    # Not committed yet -- this is the point of the test.

    bare = _run_sync(app_root, "--relock")
    assert bare.returncode != 0, bare.stdout + bare.stderr
    assert "foo_query.sql" in (bare.stdout + bare.stderr)

    tagged = _run_sync(app_root, "--relock", "--item", "T-99")
    assert tagged.returncode == 0, tagged.stdout + tagged.stderr
    lock = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock["diverged"] == {"compute/foo_query.sql": ["T-99"]}

    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-99: fix foo_query's off-by-one")

    relocked_again = _run_sync(app_root, "--relock")
    assert relocked_again.returncode == 0, relocked_again.stdout + relocked_again.stderr
    lock2 = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock2["diverged"] == {"compute/foo_query.sql": ["T-99"]}


def test_relock_item_accepts_priority_lane_id(tmp_path):
    """P3-WASTEUSD review note: the 2026-09-24 P2/P3 workflow commits under a `P<number>-<NAME>`
    id (e.g. "P3-WASTEUSD"), not the original `T-<number><letters>` task-file scheme -- --item
    must accept both, and a commit subject starting with either is picked up by a later plain
    --relock's own git-log read."""
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- fixed in place\n", encoding="utf-8", newline="\n"
    )

    tagged = _run_sync(app_root, "--relock", "--item", "P3-WASTEUSD")
    assert tagged.returncode == 0, tagged.stdout + tagged.stderr
    lock = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock["diverged"] == {"compute/foo_query.sql": ["P3-WASTEUSD"]}

    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "P3-WASTEUSD: fix foo_query's off-by-one")

    relocked_again = _run_sync(app_root, "--relock")
    assert relocked_again.returncode == 0, relocked_again.stdout + relocked_again.stderr
    lock2 = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock2["diverged"] == {"compute/foo_query.sql": ["P3-WASTEUSD"]}


def test_relock_item_accepts_hyphenated_priority_lane_id(tmp_path):
    """P3-WASTEUSD review fix: the widened _ITEM_ID_RE from test_relock_item_accepts_priority_lane_id
    above only matched a single `-<NAME>` segment (`P\\d+-[A-Za-z0-9]+`), so a lane id with more
    than one hyphenated word -- this workflow's own "P3-UI-JOBS" / "P3-UI-COST" /
    "P3-UI-POSTURE-GOV" -- failed --item's fullmatch outright and was silently cut short to
    "P3-UI" by the git-log subject parser (three different lanes would merge under one wrong id
    in the diverged map). Proves both: --item P3-UI-JOBS is accepted, and a commit subject
    "P3-UI-JOBS: ..." relocks as ["P3-UI-JOBS"], never truncated to ["P3-UI"]."""
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"
    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- fixed in place\n", encoding="utf-8", newline="\n"
    )

    tagged = _run_sync(app_root, "--relock", "--item", "P3-UI-JOBS")
    assert tagged.returncode == 0, tagged.stdout + tagged.stderr
    lock = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock["diverged"] == {"compute/foo_query.sql": ["P3-UI-JOBS"]}

    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "P3-UI-JOBS: fix foo_query's off-by-one")

    relocked_again = _run_sync(app_root, "--relock")
    assert relocked_again.returncode == 0, relocked_again.stdout + relocked_again.stderr
    lock2 = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock2["diverged"] == {"compute/foo_query.sql": ["P3-UI-JOBS"]}, (
        "commit subject 'P3-UI-JOBS: ...' must relock as the full lane id, not truncated to "
        "['P3-UI']"
    )


def test_relock_diverged_items_are_oldest_first_even_when_reordered_in_git_log(tmp_path):
    """Review fix: dedupe must keep each item's OLDEST commit position, not its most recent one.
    Committing T-98, T-99, T-98 (in that order) on the same file must relock to ["T-98", "T-99"],
    never ["T-99", "T-98"]."""
    src = make_source_repo(tmp_path)
    sha = _head_sha(src)
    app_root = tmp_path / "app_root"
    r = _run_sync(app_root, "--commit", sha, "--source", str(src))
    assert r.returncode == 0, r.stdout + r.stderr

    _init_app_root_git(app_root)

    fixed_path = app_root / "app" / "queries" / "vendored" / "compute" / "foo_query.sql"

    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2 AS x  -- a\n", encoding="utf-8", newline="\n"
    )
    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-98: a")

    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2, 3 AS x  -- b\n", encoding="utf-8", newline="\n"
    )
    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-99: b")

    fixed_path.write_text(
        "-- query_id: foo_query\nSELECT 1, 2, 3, 4 AS x  -- c\n", encoding="utf-8", newline="\n"
    )
    _git(app_root, "add", "--", "app/queries/vendored/compute/foo_query.sql")
    _git(app_root, "commit", "-q", "-m", "T-98: c")

    relocked = _run_sync(app_root, "--relock")
    assert relocked.returncode == 0, relocked.stdout + relocked.stderr
    lock = json.loads((app_root / "config" / "vendored.lock").read_text(encoding="utf-8"))
    assert lock["diverged"]["compute/foo_query.sql"] == ["T-98", "T-99"]
