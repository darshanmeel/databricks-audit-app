"""A check without workspace_id still follows the workspace filters through its warehouse or cluster."""
from __future__ import annotations

from app.core import data


def test_filterable_through_a_warehouse_or_cluster_id():
    assert data.workspace_filterable(["window_days", "warehouse_id"])
    assert data.workspace_filterable(["cluster_id"])
    assert not data.workspace_filterable(["sku_name", "endpoint_name"])


def test_clause_goes_through_the_resource_dims():
    assert data._in_workspaces(["workspace_id", "warehouse_id"], "?") == "workspace_id IN (?)"
    assert data._in_workspaces(["warehouse_id"], "?") == (
        "warehouse_id IN (SELECT warehouse_id FROM dims.dim_warehouse WHERE workspace_id IN (?))")
    assert data._in_workspaces(["cluster_id"], "?") == (
        "cluster_id IN (SELECT cluster_id FROM dims.dim_cluster WHERE workspace_id IN (?))")
