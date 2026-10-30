#!/usr/bin/env python3
"""Vendor the public Databricks query library into app/queries/vendored/, pinned by
config/vendored.lock (DEC-01..DEC-03 in tasks/DECISIONS.md, superseded where noted by DEC-66.2).

DEC-66.2: every file under app/queries/vendored/ is OUR OWN copy, and a known defect is fixed
IN PLACE, in this repo, never upstream. That makes the original `--check` (which compared the
current tree against the hashes recorded at the one-time vendor from upstream) fail on every file
we have since fixed -- not a real problem, just a lock nobody had refreshed. This version keeps
`--check`'s job the same (verify the tree against the lock) but adds a way to refresh the lock
from our own edited tree without re-pulling from upstream, and tracks which files are ours-not-
upstream's and why, so the divergence is disclosed rather than silently passed over:

    python tools/sync_queries.py --commit <sha> [--allow-dirty] [--source <path>] [--force]
        Write mode: copies queries/<domain>/*.sql (7 domains, README.md files never vendored),
        queries/manifest.json, lineage/sources.yml, databricks_system_catalog_schema.txt and
        LICENSE from the local source clone into app/queries/vendored/, and writes
        config/vendored.lock. This is upstream re-sync, not an in-place fix -- DEC-66.2 says never
        run it to publish a fix, and by default it refuses to clobber a file this repo has
        diverged from upstream on (see `--relock` below): any such file whose incoming bytes
        differ from what's on disk blocks the write and lists the item ids that touched it, so a
        `--commit` never silently erases a fix. --force overwrites anyway, discarding the fix (the
        item ids stay in this repo's own git history regardless; only the vendored copy reverts).
          - Clean source tree (`git -C <source> status --porcelain` empty): extracts via
            `git -C <source> archive <sha> -- queries lineage/sources.yml
            databricks_system_catalog_schema.txt LICENSE` (read as a tar stream with the stdlib
            tarfile module -- no external tar binary required). <sha> is passed to `git archive`
            as given; it does not have to be the source's current HEAD.
          - Dirty source tree: refuses to run (exit 1) unless --allow-dirty is given, since a
            commit sha alone would not capture the uncommitted changes. With --allow-dirty, files
            are copied directly from the working tree instead of `git archive`; <sha> must match
            (be a prefix of) the source's current HEAD (`git -C <source> log -1 --format=%H`),
            since the working tree reflects HEAD plus its uncommitted changes, not an arbitrary
            other commit. The lock records `dirty: true` and the `git status --porcelain` file
            list under `dirty_files`.

    python tools/sync_queries.py --relock [--item <T-id>]
        Refresh mode (DEC-66.2, T-75L). Touches only config/vendored.lock -- never
        app/queries/vendored/ itself and never the source clone. Recomputes `files` (sha256 of
        every file currently vendored, i.e. OUR current copies, in-place fixes included) and
        `diverged` (see below) from the tree and from this repo's own `git log`; `url`, `branch`,
        `commit`, `dirty`, `dirty_files` and `synced_at` -- the record of the one upstream sync
        this tree started from -- are carried over unchanged, since a relock is not a re-sync. Run
        this after committing an in-place library fix, so `--check` verifies against the fixed
        copy instead of the stale pre-fix one.

        This repo's usual workflow commits the fix and the relocked lock TOGETHER in one commit
        (edit the vendored file, run --relock, `git add` both, commit under the item id) --  so at
        --relock time the fix is not yet in `git log` for `diverged` to pick up. --relock therefore
        also runs `git status` on app/queries/vendored/: if any vendored file is changed but not
        yet committed, --item <T-id> is required (plain --relock exits 1 and names the file(s)) and
        that id is folded into `diverged` for each such file, in addition to whatever `git log`
        already shows. Once the fix is actually committed, a later plain --relock reads it from
        `git log` as usual and the map is unchanged.

    python tools/sync_queries.py --check
        Verify mode. Touches only app/queries/vendored/ and config/vendored.lock -- no source-repo
        access, regardless of the lock's `dirty` flag (see note below). Recomputes sha256 of every
        file currently under app/queries/vendored/ and compares against config/vendored.lock's
        `files` map -- i.e. against OUR current copies, kept fresh by `--relock` above, not
        against the original upstream bytes. On an exact match prints
            vendored tree matches config/vendored.lock (<N> queries, commit <sha>)
        and exits 0; on any mismatch (missing file, extra file, or a changed hash) lists every
        offending path to stderr and exits 1.

    --source <path>    Local source clone path. Default: env AUDIT_QUERY_SOURCE if set, else the
                        pinned default below. Never recorded in config/vendored.lock (the lock's
                        `url` field is the git remote, not a local filesystem path).

`diverged` (config/vendored.lock, DEC-66.2): `{"<path under app/queries/vendored/>": [<item id>,
...]}` for every file this repo's own `git log` shows was modified (never the one-time vendor
commit that first added it) since it was vendored -- i.e. an in-place fix, oldest item first,
deduped. The item id is the leading `T-<number><letters>` (original task-file scheme) or
`P<number>-<NAME>[-<NAME>...]` (current priority-lane scheme, e.g. "P3-WASTEUSD", "P3-UI-JOBS")
token of each modifying commit's subject line (the same id the commit message and
config/library_corrections.yml use); a commit whose subject carries no such token is recorded by
its full subject instead of being
dropped silently. A file absent from `diverged` has never been touched since it was vendored. This
is mechanically derived (DEC-08's discipline: derived, not hand-maintained) from `git -C <the audit
app repo> log`, never from the upstream source clone, and is empty (never an error) when that repo
has no such history to read -- e.g. a synthetic root a test points AUDIT_APP_ROOT at.

Hashing (orchestrator decision): every sha256 in config/vendored.lock is computed over
newline-normalised bytes (b"\\r\\n" replaced with b"\\n" before hashing), so the lock matches on
any checkout regardless of platform line-ending translation. This repo's own .gitattributes sets
`* text=auto eol=lf`, but files this tool copies come from git-archive tar bytes or directly off
a Windows working tree, either of which may still carry CRLF; normalising only the bytes fed to
sha256 (never the bytes written to app/queries/vendored/, which are stored verbatim) keeps the
lock reproducible without silently rewriting vendored file content.

Resolution of an ambiguity in the task's CLI contract: it says --check "verifies only the
per-file hashes when dirty is true" but "verifies hashes and the commit together ... when dirty is
false", while also saying --check needs "no source-repo access required" (period, not conditioned
on dirty). Re-deriving the commit's content would need `git archive <commit>` against the source
clone, which contradicts the no-source-access sentence. This implementation therefore never
touches the source repo in --check, in either case: the "commit" is always read back from the
lock file itself (never re-verified against git) and included in the success message; only the
per-file hash comparison is a real check, in both the dirty and the clean case.

For test isolation only (DEC-04's synthetic-repo test): the *output* root (where
app/queries/vendored/ and config/vendored.lock are written) is env AUDIT_APP_ROOT if set, else
this file's own repo root -- mirrors the --source / AUDIT_QUERY_SOURCE pattern. Real invocations
of this tool never set AUDIT_APP_ROOT. `--relock` and write mode's divergence check read that same
root's `git log` (never the source clone's), so a test that wants divergence behaviour makes its
own AUDIT_APP_ROOT a git checkout with the history it needs; one that doesn't just gets `{}`.

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import header_schema as hs  # noqa: E402

DOMAINS = sorted(hs.DOMAINS)

# The query library checked out next to this repo; --source or AUDIT_QUERY_SOURCE point elsewhere.
DEFAULT_SOURCE = Path(__file__).resolve().parent.parent.parent / "crosshire-audit-databricks-admin"

VENDOR_TOP_LEVEL_FILES = (
    "manifest.json",
    "databricks_system_catalog_schema.txt",
    "LICENSE",
)

# Leading item-id token of a commit subject: either the original "T-<number><letters>" task-file
# scheme (e.g. "T-66A", "T-75B", "T-75L") or the current priority-lane scheme
# "P<number>-<NAME>[-<NAME>...]" (e.g. "P2-DOCS", "P3-WASTEUSD", and the hyphenated
# "P3-UI-JOBS"/"P3-UI-COST"/"P3-UI-POSTURE-GOV") the 2026-09-24 P2/P3 workflow commits under
# (DEC-66.4) -- both are accepted so --item and the git-log-derived `diverged` map keep working
# across the switch. The P<number> branch repeats "-<NAME>" one or more times (not just once) so
# a multi-word lane id is captured whole instead of being cut at its first hyphen (P3-WASTEUSD
# review fix: "P3-UI-JOBS" used to match only as far as "P3-UI").
_ITEM_ID_RE = re.compile(r"^(T-\d+[A-Za-z]*|P\d+(?:-[A-Za-z0-9]+)+)")


def dest_root() -> Path:
    override = os.environ.get("AUDIT_APP_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent


def source_path(args: argparse.Namespace) -> Path:
    if args.source:
        return Path(args.source)
    env = os.environ.get("AUDIT_QUERY_SOURCE")
    if env:
        return Path(env)
    return Path(DEFAULT_SOURCE)


def sha256_normalized(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def hash_tree(vendored_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not vendored_dir.exists():
        return out
    for p in sorted(vendored_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(vendored_dir).as_posix()
            out[rel] = sha256_normalized(p.read_bytes())
    return out


def compute_counts(vendored_dir: Path) -> dict:
    by_domain = {}
    total = 0
    for domain in DOMAINS:
        d = vendored_dir / domain
        n = len(list(d.glob("*.sql"))) if d.is_dir() else 0
        by_domain[domain] = n
        total += n
    return {"queries": total, "by_domain": by_domain}


def git(source: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True, text=True, check=check
    )


def git_status_porcelain(source: Path) -> list[str]:
    r = git(source, "status", "--porcelain")
    files = []
    for line in r.stdout.splitlines():
        if not line:
            continue
        # Porcelain v1 short format: 2 status chars + 1 space + path (rename: "old -> new").
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        files.append(rest.strip().strip('"'))
    return sorted(files)


def git_head_sha(source: Path) -> str:
    return git(source, "log", "-1", "--format=%H").stdout.strip()


def git_remote_url(source: Path) -> str:
    return git(source, "remote", "get-url", "origin").stdout.strip()


def git_branch(source: Path) -> str:
    return git(source, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def compute_diverged(root: Path) -> dict[str, list[str]]:
    """DEC-66.2 (T-75L): item ids that modified each file under app/queries/vendored/ since it
    was vendored, oldest first, deduped -- read from `root`'s OWN `git log` (the audit-app repo,
    never the upstream source clone). Never raises: `root` may not be a git checkout at all (a
    test's synthetic AUDIT_APP_ROOT), in which case this returns {} -- see the module docstring.
    """
    r = subprocess.run(
        [
            "git", "-C", str(root), "log", "--diff-filter=M", "--name-only",
            "--format=\x02%H\x1f%s", "--", "app/queries/vendored",
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {}

    out: dict[str, list[str]] = {}
    subject: str | None = None
    prefix = "app/queries/vendored/"
    for line in r.stdout.splitlines():
        if line.startswith("\x02"):
            _commit, subject = line[1:].split("\x1f", 1)
            continue
        line = line.strip()
        if not line or subject is None or not line.startswith(prefix):
            continue
        rel = line[len(prefix):]
        m = _ITEM_ID_RE.match(subject)
        item = m.group(1) if m else subject
        out.setdefault(rel, []).append(item)

    # `git log` prints newest-first, so `out[rel]` is built newest-first too, with every
    # occurrence kept (not deduped yet) so a file fixed more than once under the same item id
    # (e.g. an item and its own review-fix round) still dedupes correctly below. Reversing then
    # deduping-by-first-occurrence yields oldest-first, keeping each item's OLDEST commit position
    # -- not its most recent one, which a naive "dedupe while scanning newest-first" would keep.
    for rel, items in out.items():
        out[rel] = list(dict.fromkeys(reversed(items)))
    return out


def _classify_archive_member(name: str) -> str | None:
    """Map a `git archive` tar member path to its destination under app/queries/vendored/, or
    None to skip it (e.g. a per-domain README.md, also included by the `-- queries` pathspec)."""
    parts = name.split("/")
    if len(parts) == 3 and parts[0] == "queries" and parts[1] in DOMAINS and parts[2].endswith(".sql"):
        return f"{parts[1]}/{parts[2]}"
    if name == "queries/manifest.json":
        return "manifest.json"
    if name == "lineage/sources.yml":
        return "lineage/sources.yml"
    if name == "databricks_system_catalog_schema.txt":
        return "databricks_system_catalog_schema.txt"
    if name == "LICENSE":
        return "LICENSE"
    return None


def extract_via_git_archive(source: Path, commit: str) -> dict[str, bytes]:
    """Read the commit's vendored files into memory (rel path -> bytes); writes nothing. Kept in
    memory rather than written straight to app/queries/vendored/ so do_write() can compare
    incoming bytes against what's already on disk -- and refuse to clobber a diverged file --
    before touching anything (DEC-66.2)."""
    cmd = [
        "git", "-C", str(source), "archive", commit, "--",
        "queries", "lineage/sources.yml", "databricks_system_catalog_schema.txt", "LICENSE",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git archive failed: " + r.stderr.decode("utf-8", "replace"))
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            dest_rel = _classify_archive_member(member.name)
            if dest_rel is None:
                continue
            fh = tf.extractfile(member)
            assert fh is not None
            out[dest_rel] = fh.read()
    return out


def copy_from_working_tree(source: Path) -> dict[str, bytes]:
    """Same contract as extract_via_git_archive, sourced from the --allow-dirty working tree."""
    out: dict[str, bytes] = {}
    for domain in DOMAINS:
        ddir = source / "queries" / domain
        if not ddir.is_dir():
            continue
        for f in sorted(ddir.glob("*.sql")):
            out[f"{domain}/{f.name}"] = f.read_bytes()
    out["manifest.json"] = (source / "queries" / "manifest.json").read_bytes()
    out["lineage/sources.yml"] = (source / "lineage" / "sources.yml").read_bytes()
    out["databricks_system_catalog_schema.txt"] = (
        source / "databricks_system_catalog_schema.txt"
    ).read_bytes()
    out["LICENSE"] = (source / "LICENSE").read_bytes()
    return out


def write_tree(vendored_dir: Path, content: dict[str, bytes]) -> None:
    for rel, data in content.items():
        out_path = vendored_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)


def write_lock(lock: dict, root: Path) -> Path:
    path = root / "config" / "vendored.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def do_write(args: argparse.Namespace) -> int:
    source = source_path(args)
    if not source.is_dir():
        print(f"error: source clone not found at {source}", file=sys.stderr)
        return 1

    dirty_files = git_status_porcelain(source)
    is_dirty = bool(dirty_files)

    if is_dirty and not args.allow_dirty:
        print(
            f"error: source clone at {source} has {len(dirty_files)} uncommitted change(s); "
            "pass --allow-dirty to vendor the working tree as-is, or commit/stash first",
            file=sys.stderr,
        )
        for f in dirty_files:
            print(f"  {f}", file=sys.stderr)
        return 1

    commit = args.commit
    if is_dirty:
        head_sha = git_head_sha(source)
        if not head_sha.lower().startswith(commit.lower()):
            print(
                f"error: --commit {commit} does not match source HEAD {head_sha}; "
                "a dirty-tree sync records the nearest (HEAD) commit",
                file=sys.stderr,
            )
            return 1

    root = dest_root()
    vendored_dir = root / "app" / "queries" / "vendored"
    lock_path_for_guard = root / "config" / "vendored.lock"

    # Read the incoming content into memory first -- nothing on disk is touched yet -- so a
    # diverged file (DEC-66.2: fixed in place, not upstream) can be compared and, by default,
    # protected before do_write does anything destructive.
    if is_dirty:
        new_content = copy_from_working_tree(source)
    else:
        new_content = extract_via_git_archive(source, commit)

    # The guard against clobbering an in-place fix must not depend solely on this repo's live
    # `git log`: that read returns {} whenever there is no history to read (a non-git export, a
    # shallow clone, or git missing), which would silently turn the guard off. So it is the UNION
    # of the lock's already-recorded `diverged` map (survives even with no git available) and the
    # live git-log read (catches a fix committed since the lock was last relocked), item ids
    # deduped per file with the recorded ones kept first.
    recorded: dict[str, list[str]] = {}
    if lock_path_for_guard.exists():
        recorded = json.loads(lock_path_for_guard.read_text(encoding="utf-8")).get("diverged", {})
    live = compute_diverged(root)
    guard = {
        rel: list(dict.fromkeys(recorded.get(rel, []) + live.get(rel, [])))
        for rel in set(recorded) | set(live)
    }
    if guard and not args.force:
        blocked: list[tuple[str, list[str], str]] = []
        for rel in sorted(guard):
            cur_path = vendored_dir / rel
            if not cur_path.is_file():
                continue
            if rel not in new_content:
                # Fixed here, and this incoming commit no longer ships the file at all -- the
                # unguarded rmtree()+write below would otherwise delete a fix silently.
                blocked.append((rel, guard[rel], "removed upstream"))
                continue
            cur_hash = sha256_normalized(cur_path.read_bytes())
            new_hash = sha256_normalized(new_content[rel])
            if cur_hash != new_hash:
                blocked.append((rel, guard[rel], "changed upstream"))
        if blocked:
            print(
                f"error: refusing to overwrite {len(blocked)} file(s) fixed in place here "
                "(DEC-66.2); pass --force to overwrite anyway and discard the fix(es):",
                file=sys.stderr,
            )
            for rel, items, reason in blocked:
                print(f"  {rel}  (changed by {', '.join(items)}; {reason})", file=sys.stderr)
            return 1

    if vendored_dir.exists():
        shutil.rmtree(vendored_dir)
    vendored_dir.mkdir(parents=True)
    write_tree(vendored_dir, new_content)

    counts = compute_counts(vendored_dir)
    files_hashes = hash_tree(vendored_dir)
    diverged_after = {rel: items for rel, items in compute_diverged(root).items() if rel in files_hashes}

    lock = {
        "url": git_remote_url(source),
        "branch": git_branch(source),
        "commit": commit,
        "dirty": is_dirty,
        "dirty_files": dirty_files,
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": counts,
        "files": files_hashes,
        "diverged": diverged_after,
    }
    lock_path = write_lock(lock, root)

    print(
        f"synced {counts['queries']} queries from {source} @ {commit} "
        f"(dirty={is_dirty}) -> {vendored_dir}"
    )
    print(f"wrote {lock_path}")
    return 0


def do_relock(args: argparse.Namespace) -> int:
    """DEC-66.2 (T-75L): refresh config/vendored.lock's `files` and `diverged` from the CURRENT
    app/queries/vendored/ tree in place -- no source-repo access, and app/queries/vendored/ itself
    is never written. Run this after committing an in-place library fix."""
    root = dest_root()
    vendored_dir = root / "app" / "queries" / "vendored"
    lock_path = root / "config" / "vendored.lock"

    if not lock_path.exists():
        print(f"error: {lock_path} does not exist -- run --commit first", file=sys.stderr)
        return 1
    if not vendored_dir.exists():
        print(f"error: {vendored_dir} does not exist -- run --commit first", file=sys.stderr)
        return 1

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    counts = compute_counts(vendored_dir)
    files_hashes = hash_tree(vendored_dir)
    diverged = {rel: items for rel, items in compute_diverged(root).items() if rel in files_hashes}

    # The one-commit-per-item workflow (edit the vendored file, --relock, commit both together)
    # means `git log` above cannot see the fix that is ABOUT to be committed -- it isn't in history
    # yet. Left alone, that fix's file would be missing from `diverged` (or the current item id
    # missing from an existing entry) until some *later* commit happened to touch the same file,
    # and --check would pass despite the divergence being understated. So: any vendored file this
    # repo's own working tree currently shows as changed but not yet committed must be named via
    # --item, and that id is folded into `diverged` right here.
    r = subprocess.run(
        [
            "git", "-C", str(root), "status", "--porcelain", "--untracked-files=all",
            "--", "app/queries/vendored",
        ],
        capture_output=True, text=True,
    )
    uncommitted: list[str] = []
    if r.returncode == 0:
        prefix = "app/queries/vendored/"
        for line in r.stdout.splitlines():
            if not line:
                continue
            rel = line[3:].split(" -> ")[-1].strip().strip('"')
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                if rel in files_hashes:
                    uncommitted.append(rel)

    if uncommitted:
        if not args.item:
            print(
                "error: uncommitted change(s) under app/queries/vendored/: "
                + ", ".join(sorted(uncommitted))
                + "; pass --item <T-id> naming the item whose commit will carry them "
                  "(or commit first, then --relock)",
                file=sys.stderr,
            )
            return 1
        if not _ITEM_ID_RE.fullmatch(args.item):
            print(
                f"error: --item {args.item!r} is not a valid item id "
                "(expected T-<number><letters> or P<number>-<NAME>[-<NAME>...])",
                file=sys.stderr,
            )
            return 2
        for rel in uncommitted:
            items = diverged.setdefault(rel, [])
            if args.item not in items:
                items.append(args.item)
        diverged = dict(sorted(diverged.items()))

    lock["counts"] = counts
    lock["files"] = files_hashes
    lock["diverged"] = diverged
    lock_path_out = write_lock(lock, root)

    print(
        f"relocked {counts['queries']} queries under {vendored_dir} "
        f"({len(diverged)} file(s) diverged from upstream commit {lock.get('commit', '')})"
    )
    print(f"wrote {lock_path_out}")
    return 0


def do_check(args: argparse.Namespace) -> int:
    root = dest_root()
    vendored_dir = root / "app" / "queries" / "vendored"
    lock_path = root / "config" / "vendored.lock"

    if not lock_path.exists():
        print(f"error: {lock_path} does not exist -- run --commit first", file=sys.stderr)
        return 1

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    expected_files: dict[str, str] = lock.get("files", {})
    actual_files = hash_tree(vendored_dir)

    problems: list[str] = []
    for rel in sorted(expected_files):
        expected_hash = expected_files[rel]
        actual_hash = actual_files.get(rel)
        if actual_hash is None:
            problems.append(f"missing: {rel}")
        elif actual_hash != expected_hash:
            problems.append(f"hash mismatch: {rel}")
    extra = sorted(set(actual_files) - set(expected_files))
    for rel in extra:
        problems.append(f"unexpected extra file: {rel}")

    if problems:
        print(
            f"vendored tree does NOT match config/vendored.lock ({len(problems)} problem(s)):",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    counts = lock.get("counts", {})
    n = counts.get("queries", len(expected_files))
    commit = lock.get("commit", "")
    print(f"vendored tree matches config/vendored.lock ({n} queries, commit {commit})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--commit", help="source commit sha to vendor (write mode)")
    ap.add_argument(
        "--check", action="store_true", help="verify app/queries/vendored/ against config/vendored.lock"
    )
    ap.add_argument(
        "--relock",
        action="store_true",
        help=(
            "refresh config/vendored.lock's files/diverged from the current "
            "app/queries/vendored/ tree, without re-vendoring from --source (DEC-66.2)"
        ),
    )
    ap.add_argument(
        "--item",
        help=(
            "with --relock: the item id (T-NN...) whose commit will carry the uncommitted "
            "in-place change(s) under app/queries/vendored/ -- required whenever --relock finds "
            "such an uncommitted change, since this repo's own git log cannot see it yet"
        ),
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="with --commit: overwrite a file fixed in place here even though it discards the fix (DEC-66.2)",
    )
    ap.add_argument(
        "--allow-dirty",
        action="store_true",
        help="vendor the working tree even if the source clone has uncommitted changes",
    )
    ap.add_argument(
        "--source",
        help="path to the local source clone (default: env AUDIT_QUERY_SOURCE, else the pinned default)",
    )
    args = ap.parse_args()

    modes_given = sum(1 for m in (args.check, args.relock, bool(args.commit)) if m)
    if modes_given > 1:
        print("error: --check, --relock and --commit are mutually exclusive", file=sys.stderr)
        return 2
    if args.check:
        return do_check(args)
    if args.relock:
        return do_relock(args)
    if not args.commit:
        print("error: --commit <sha> is required (or use --check / --relock)", file=sys.stderr)
        return 2
    return do_write(args)


if __name__ == "__main__":
    raise SystemExit(main())
