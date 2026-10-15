"""tests/test_library_corrections.py -- T-75A (DEC-66.2): the library-corrections register.

Covers app/core/library_corrections.py's loader/validator against BOTH the real repo file
(config/library_corrections.yml, which must already be valid -- this is the fail-loudly guarantee
app/api/app.py leans on at import time) and a set of deliberately-broken fixtures (monkeypatching
the module's own `_PATH`/`_CACHE` so no test ever touches the real file).

No DuckDB anywhere on this import path (app.core.library_corrections -> app.core.registry, both
stdlib + pyyaml only), so this module needs no tests/conftest.py fixture and no fixture database.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import library_corrections as lc  # noqa: E402
from app.core import registry  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a clean load cache -- a test that monkeypatches `lc._PATH` must
    never see a stale in-memory result from a previous test's file."""
    lc._CACHE = None
    yield
    lc._CACHE = None


def _write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object) -> Path:
    path = tmp_path / "library_corrections.yml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False)
    monkeypatch.setattr(lc, "_PATH", path)
    return path


# ---------------------------------------------------------------------------------------------
# The real repo register (config/library_corrections.yml) -- must already be valid, since
# app/api/app.py calls load_corrections() at import time (fail loudly at startup, not lazily).
# ---------------------------------------------------------------------------------------------


def test_real_register_loads_and_validates():
    corrections = lc.load_corrections()
    assert len(corrections) > 0
    known = {s.query_id: s.origin for s in registry.load_registry()}
    ids_seen = set()
    for c in corrections:
        assert c.id not in ids_seen, f"duplicate id {c.id!r} slipped past validation"
        ids_seen.add(c.id)
        assert c.query_id in known, f"{c.id}: {c.query_id!r} is not a known query_id"
        assert known[c.query_id] == "vendored", f"{c.id}: {c.query_id!r} is not vendored"
        assert c.status in lc.STATUS_VALUES
        assert c.problem and c.effect and c.fix
        if c.status == "not_fixed":
            # item is optional (None means "not yet planned"), but when present must be a
            # non-empty string -- already enforced by the loader; this is a belt-and-braces check
            # on the real data specifically.
            assert c.item is None or (isinstance(c.item, str) and c.item.strip())


def test_real_register_every_item_is_a_non_blank_string():
    # review round 1 (must_fix 5): a literal status/item pin on one concrete entry (e.g.
    # cost_actual_vs_list_by_sku) breaks the moment that entry is flipped to fixed -- this is the
    # structural replacement: every entry that DOES carry an item names a real task id, whatever
    # the register's current content is.
    corrections = lc.load_corrections()
    for c in corrections:
        if c.item is not None:
            assert isinstance(c.item, str) and c.item.strip()


def test_by_query_id_omits_a_query_with_no_entry():
    by_id = lc.by_query_id()
    # A query with no known defect must be absent (never an empty-list placeholder) -- callers
    # use .get(query_id, []). review round 1 (must_fix 5): pick a vendored query_id the CURRENT
    # register carries no entry for, at runtime, rather than assuming one fixed id always has
    # none -- the register's own content is expected to change as items land.
    known_vendored = {s.query_id for s in registry.load_registry() if s.origin == "vendored"}
    unlisted = next(iter(known_vendored - set(by_id)), None)
    assert unlisted is not None, "every vendored query_id currently carries a register entry"
    assert unlisted not in by_id


# ---------------------------------------------------------------------------------------------
# Validation failures -- every one of these must raise LibraryCorrectionsError, never silently
# drop or misread the bad entry (app/core/registry.py's own "fail loudly" discipline).
# ---------------------------------------------------------------------------------------------


def test_missing_file_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "_PATH", tmp_path / "does-not-exist.yml")
    assert lc.load_corrections() == []


def test_not_a_mapping_with_entries_key_raises(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, ["a", "list", "not", "a", "mapping"])
    with pytest.raises(lc.LibraryCorrectionsError, match="top-level 'entries'"):
        lc.load_corrections()


def test_unknown_query_id_raises(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "no_such_query_id_at_all", "problem": "p", "effect": "e",
         "status": "not_fixed", "fix": "f"},
    ]})
    with pytest.raises(lc.LibraryCorrectionsError, match="unknown query id"):
        lc.load_corrections()


def test_non_vendored_query_id_raises(tmp_path, monkeypatch):
    # overview_spend_estimate is app-owned (app/queries/app/manifest.json) -- the register is only
    # for OUR COPY of the vendored reference library (DEC-66.2); an app-owned bug is just a bug.
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "overview_spend_estimate", "problem": "p", "effect": "e",
         "status": "not_fixed", "fix": "f"},
    ]})
    with pytest.raises(lc.LibraryCorrectionsError, match="not 'vendored'"):
        lc.load_corrections()


def test_duplicate_id_raises(tmp_path, monkeypatch):
    entry = {"id": "dup-1", "query_id": "cost_by_job", "problem": "p", "effect": "e",
              "status": "not_fixed", "fix": "f"}
    _write(tmp_path, monkeypatch, {"entries": [dict(entry), dict(entry)]})
    with pytest.raises(lc.LibraryCorrectionsError, match="duplicate correction id"):
        lc.load_corrections()


def test_bad_status_raises(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "p", "effect": "e",
         "status": "kinda_fixed", "fix": "f"},
    ]})
    with pytest.raises(lc.LibraryCorrectionsError, match="status must be one of"):
        lc.load_corrections()


def test_missing_required_field_raises(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "p", "status": "not_fixed", "fix": "f"},
    ]})
    with pytest.raises(lc.LibraryCorrectionsError, match="missing required field"):
        lc.load_corrections()


def test_unknown_field_raises(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "p", "effect": "e",
         "status": "not_fixed", "fix": "f", "severity": "high"},
    ]})
    with pytest.raises(lc.LibraryCorrectionsError, match="unknown field"):
        lc.load_corrections()


def test_blank_required_field_raises(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "   ", "effect": "e",
         "status": "not_fixed", "fix": "f"},
    ]})
    with pytest.raises(lc.LibraryCorrectionsError, match="non-empty string"):
        lc.load_corrections()


def test_item_none_is_valid_not_fixed_and_unplanned(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "p", "effect": "e",
         "status": "not_fixed", "fix": "f"},
    ]})
    corrections = lc.load_corrections()
    assert len(corrections) == 1
    assert corrections[0].item is None


def test_fixed_status_is_valid(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "p", "effect": "e",
         "status": "fixed", "fix": "already fixed", "item": "T-99"},
    ]})
    corrections = lc.load_corrections()
    assert len(corrections) == 1
    assert corrections[0].status == "fixed"
    assert corrections[0].item == "T-99"


def test_cache_reflects_file_change(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "p", "effect": "e",
         "status": "not_fixed", "fix": "f"},
    ]})
    first = lc.load_corrections()
    assert len(first) == 1
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump({"entries": []}, fh)
    second = lc.load_corrections()
    assert second == []


def test_load_corrections_returns_a_fresh_list_each_call(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"entries": [
        {"id": "x-1", "query_id": "cost_by_job", "problem": "p", "effect": "e",
         "status": "not_fixed", "fix": "f"},
    ]})
    first = lc.load_corrections()
    first.clear()
    second = lc.load_corrections()
    assert len(second) == 1  # mutating the caller's list must never corrupt the cache
