"""Row order: the committed model yml stands in for a missing dbt manifest, and equal sort keys
break the same way on every read."""
from __future__ import annotations

import json

import pytest

from app.core import data as app_core_data


def test_order_by_falls_back_to_the_committed_model_yml(monkeypatch, tmp_path):
    monkeypatch.setattr(app_core_data, "_dbt_manifest_path", lambda: tmp_path / "manifest.json")
    order_by = app_core_data._model_order_by("lakeflow_failed_jobs_wasted_dbus")
    assert order_by and order_by.startswith("CASE status")


def test_committed_order_by_matches_the_built_manifest():
    path = app_core_data._dbt_manifest_path()
    if not path.exists():
        pytest.skip("dbt/target is not built")
    nodes = json.loads(path.read_text(encoding="utf-8"))["nodes"]
    committed = app_core_data._committed_order_bys()
    assert len(committed) > 100
    for query_id, order_by in committed.items():
        node = nodes.get(f"model.databricks_audit.f_{query_id}")
        if node:
            assert node["config"]["meta"]["order_by"] == order_by, query_id


def test_every_column_breaks_ties_after_the_model_order():
    clause = app_core_data._order_by_clause("lakeflow_failed_jobs_wasted_dbus", ["status", "workspace_id", "job_id"])
    assert clause.endswith('"status", "workspace_id", "job_id"')
