"""app/core/links.py

Run/job/cluster/warehouse URLs built from a workspace's `dim_workspace.url` (PLAN.md 6.1). PLAN.md
gives no exact URL template -- it explicitly expects these to be unverified against a live
workspace (this task never touches Databricks). DEC-25/DEC-28 (tasks/DECISIONS.md) settle the
DECIDE this task file raised: the best-known Databricks workspace UI URL conventions below, each
returned with `verified: False` -- callers (T-26's cards/panel, T-32's drill-down) must render
that flag, never present a bare link as confirmed.

    job_url(workspace_url, workspace_id, job_id)                 -> Link
    run_url(workspace_url, workspace_id, job_id, run_id)         -> Link
    cluster_url(workspace_url, workspace_id, cluster_id)         -> Link
    warehouse_url(workspace_url, workspace_id, warehouse_id)     -> Link
    query_url(workspace_url, workspace_id, statement_id)         -> Link

Every builder returns {"url": str | None, "verified": False}: url is None when workspace_url is
falsy (most fixture/real workspaces have no `system.access` grant and so no known
`workspace_url` -- dims.dim_workspace.url is NULL for them), never a broken/bare link built from a
missing host.

URL shapes (DEC-28, all `?o=<workspace_id>` suffixed the way a Databricks account-console link
disambiguates the workspace):
    job:       {workspace_url}/jobs/{job_id}?o={workspace_id}
    run:       {workspace_url}/jobs/{job_id}/runs/{run_id}?o={workspace_id}
    cluster:   {workspace_url}/compute/clusters/{cluster_id}?o={workspace_id}
    warehouse: {workspace_url}/sql/warehouses/{warehouse_id}?o={workspace_id}
    query:     {workspace_url}/sql/history?queryId={statement_id}&o={workspace_id}

Stdlib only.
"""
from __future__ import annotations

from typing import TypedDict


class Link(TypedDict):
    url: str | None
    verified: bool


def _build(workspace_url: str | None, path: str, workspace_id: str) -> Link:
    if not workspace_url:
        return Link(url=None, verified=False)
    base = workspace_url.rstrip("/")
    return Link(url=f"{base}{path}?o={workspace_id}", verified=False)


def job_url(workspace_url: str | None, workspace_id: str, job_id: str) -> Link:
    """{workspace_url}/jobs/{job_id}?o={workspace_id}"""
    return _build(workspace_url, f"/jobs/{job_id}", workspace_id)


def run_url(workspace_url: str | None, workspace_id: str, job_id: str, run_id: str) -> Link:
    """{workspace_url}/jobs/{job_id}/runs/{run_id}?o={workspace_id}"""
    return _build(workspace_url, f"/jobs/{job_id}/runs/{run_id}", workspace_id)


def cluster_url(workspace_url: str | None, workspace_id: str, cluster_id: str) -> Link:
    """{workspace_url}/compute/clusters/{cluster_id}?o={workspace_id}"""
    return _build(workspace_url, f"/compute/clusters/{cluster_id}", workspace_id)


def warehouse_url(workspace_url: str | None, workspace_id: str, warehouse_id: str) -> Link:
    """{workspace_url}/sql/warehouses/{warehouse_id}?o={workspace_id}"""
    return _build(workspace_url, f"/sql/warehouses/{warehouse_id}", workspace_id)


def query_url(workspace_url: str | None, workspace_id: str, statement_id: str) -> Link:
    """{workspace_url}/sql/history?queryId={statement_id}&o={workspace_id}

    Its path already carries a query string, so it cannot reuse _build's own "?o=" suffix -- the
    same URL built by hand here, with "&o=" instead.
    """
    if not workspace_url:
        return Link(url=None, verified=False)
    base = workspace_url.rstrip("/")
    return Link(url=f"{base}/sql/history?queryId={statement_id}&o={workspace_id}", verified=False)
