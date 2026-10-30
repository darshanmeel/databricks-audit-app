"""tests/test_links.py -- app/core/links.py: every URL builder returns None on a missing
workspace_url, never a broken/bare link, and each builder's shape matches its own docstring."""
from __future__ import annotations

from app.core import links


def test_job_url_no_host():
    assert links.job_url(None, "123", "45") == {"url": None, "verified": False}


def test_job_url_shape():
    link = links.job_url("https://foo.cloud.databricks.com/", "123", "45")
    assert link == {"url": "https://foo.cloud.databricks.com/jobs/45?o=123", "verified": False}


def test_run_url_shape():
    link = links.run_url("https://foo.cloud.databricks.com", "123", "45", "678")
    assert link == {"url": "https://foo.cloud.databricks.com/jobs/45/runs/678?o=123", "verified": False}


def test_cluster_url_shape():
    link = links.cluster_url("https://foo.cloud.databricks.com", "123", "abc-def")
    assert link == {"url": "https://foo.cloud.databricks.com/compute/clusters/abc-def?o=123", "verified": False}


def test_warehouse_url_shape():
    link = links.warehouse_url("https://foo.cloud.databricks.com", "123", "wh1")
    assert link == {"url": "https://foo.cloud.databricks.com/sql/warehouses/wh1?o=123", "verified": False}


def test_query_url_no_host():
    assert links.query_url(None, "123", "stmt-1") == {"url": None, "verified": False}


def test_query_url_shape():
    link = links.query_url("https://foo.cloud.databricks.com", "123", "stmt-1")
    assert link == {"url": "https://foo.cloud.databricks.com/sql/history?queryId=stmt-1&o=123", "verified": False}
