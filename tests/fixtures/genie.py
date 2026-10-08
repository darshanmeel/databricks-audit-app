"""tests/fixtures/genie.py -- system.billing.usage rows for Genie (billing_origin_product 'GENIE'),
read by genie_usage.

Every id this file writes carries the `gn_` prefix, and it writes only into its own two
workspaces (gn_ws_prod "genie-prod", gn_ws_dev "genie-dev"), so no other builder's per-workspace
figures move. Prices: gn_SKU_GENIE $0.10 per DBU (effective list); gn_GENIE_FREE_USAGE and
gn_SKU_NOPRICE have no list price (free by name, and unpriced).

Scenario map (DBU unless stated; W=7: current D(1)..D(7), previous D(8)..D(14)):

gn_ws_prod
  gn_u_code_a1   D(2)  GENIE_CODE  UI   alice@example.com   10 DBU priced
  gn_u_code_a2   D(2)  same key as gn_u_code_a1               5 DBU -> one grain row, 15 before corrections
  gn_u_code_ret  D(2)  same key, record_type RETRACTION      -2 DBU -> nets to 13 DBU, $1.30
  gn_u_code_free D(2)  GENIE_CODE  UI   alice, free SKU      20 DBU -> is_free, $0, price_basis free
  gn_u_agent_sp  D(3)  GENIE_AGENTS API agent gn_agent_sales, service principal (GUID)  40 DBU, $4
  gn_u_code_np   D(3)  GENIE_CODE  UI   dave@example.com, unpriced SKU  7 DBU -> price_basis unpriced
  gn_u_nostruct  D(6)  no genie struct, run_as NULL          3 DBU -> surface/channel/agent NULL, unknown
  gn_u_prev      D(10) GENIE_CODE  UI   alice               12 DBU -> period previous at W=7
  gn_u_d20       D(20) GENIE_CODE  UI   alice                4 DBU -> outside W=7 (both periods), current at W=30
  gn_u_d0        D0    GENIE_CODE  UI   alice              999 DBU -> today, never counted
  gn_u_serving   D(2)  MODEL_SERVING (not Genie)             9 DBU -> excluded by the product filter
  gn_u_tokens    D(2)  GENIE, usage_unit TOKEN              50     -> excluded by the DBU filter
gn_ws_dev
  gn_u_agent_bob D(4)  GENIE_AGENTS UI  agent gn_agent_sales, bob@example.com   8 DBU
  gn_u_one_carol D(5)  GENIE_ONE   UI   carol@example.com    6 DBU
"""
from __future__ import annotations

from datetime import datetime, timedelta

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py callers)

D0 = AS_OF.date()
ACCOUNT_ID = "gn_acct"
CLOUD = "aws"
WS_PROD = "gn_ws_prod"
WS_DEV = "gn_ws_dev"
SKU = "gn_SKU_GENIE"
SKU_FREE = "gn_GENIE_FREE_USAGE"
SKU_NOPRICE = "gn_SKU_NOPRICE"
SP = "aaaaaaaa-1111-2222-3333-444444444444"


def D(n: int):
    return D0 - timedelta(days=n)


_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_WS_SQL = (
    "INSERT INTO access__workspaces_latest (account_id, workspace_id, workspace_name, "
    "workspace_url, create_time, status) VALUES (?,?,?,?,?,?)"
)
_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def usage(con, record_id, *, ws, day, qty, sku=SKU, surface=None, channel=None, agent_id=None,
          run_as=None, product="GENIE", unit="DBU", record_type="ORIGINAL", genie=True):
    start = datetime.combine(D(day), datetime.min.time()) + timedelta(hours=9)
    # An older row carries no genie struct at all.
    meta = {"genie": {"surface": surface, "channel": channel, "agent_id": agent_id}} if genie else {"genie": None}
    con.execute(_USAGE_SQL, [
        ACCOUNT_ID, ws, record_id, sku, CLOUD, start, start + timedelta(hours=1), D(day),
        {}, unit, qty, meta,
        {"run_as": run_as, "created_by": None, "owned_by": None, "run_by": None},
        record_type, D(day), product,
        {"is_serverless": True, "is_photon": False}, "COMPUTE_TIME",
    ])


def build(con) -> None:
    for ws, name in ((WS_PROD, "genie-prod"), (WS_DEV, "genie-dev")):
        con.execute(_WS_SQL, [ACCOUNT_ID, ws, name, None, AS_OF - timedelta(days=500), "ACTIVE"])
    con.execute(_LP_SQL, [
        ACCOUNT_ID, AS_OF - timedelta(days=100), None, SKU, CLOUD, "USD", "DBU",
        {"default": 0.08, "promotional": {"default": None}, "effective_list": {"default": 0.10}},
    ])

    alice = dict(surface="GENIE_CODE", channel="UI", run_as="alice@example.com")
    usage(con, "gn_u_code_a1", ws=WS_PROD, day=2, qty=10.0, **alice)
    usage(con, "gn_u_code_a2", ws=WS_PROD, day=2, qty=5.0, **alice)
    usage(con, "gn_u_code_ret", ws=WS_PROD, day=2, qty=-2.0, record_type="RETRACTION", **alice)
    usage(con, "gn_u_code_free", ws=WS_PROD, day=2, qty=20.0, sku=SKU_FREE, **alice)
    usage(con, "gn_u_agent_sp", ws=WS_PROD, day=3, qty=40.0, surface="GENIE_AGENTS", channel="API",
          agent_id="gn_agent_sales", run_as=SP)
    usage(con, "gn_u_code_np", ws=WS_PROD, day=3, qty=7.0, sku=SKU_NOPRICE, surface="GENIE_CODE",
          channel="UI", run_as="dave@example.com")
    usage(con, "gn_u_nostruct", ws=WS_PROD, day=6, qty=3.0, genie=False)
    usage(con, "gn_u_prev", ws=WS_PROD, day=10, qty=12.0, **alice)
    usage(con, "gn_u_d20", ws=WS_PROD, day=20, qty=4.0, **alice)
    usage(con, "gn_u_d0", ws=WS_PROD, day=0, qty=999.0, **alice)
    usage(con, "gn_u_serving", ws=WS_PROD, day=2, qty=9.0, product="MODEL_SERVING", **alice)
    usage(con, "gn_u_tokens", ws=WS_PROD, day=2, qty=50.0, unit="TOKEN", **alice)
    usage(con, "gn_u_agent_bob", ws=WS_DEV, day=4, qty=8.0, surface="GENIE_AGENTS", channel="UI",
          agent_id="gn_agent_sales", run_as="bob@example.com")
    usage(con, "gn_u_one_carol", ws=WS_DEV, day=5, qty=6.0, surface="GENIE_ONE", channel="UI",
          run_as="carol@example.com")
