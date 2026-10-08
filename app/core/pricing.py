"""app/core/pricing.py

apply(df, discount_pct) -- adds a "<col>_disc" column for every column in `df` whose name
matches ^est_(?:[a-z]+_)?usd_list$ (a list-price dollar column), leaving every `*_share` column
(and every other column) untouched. DBUs are never dollars (D6): every dollar figure a finding
table carries is the effective list price (DEC-66.1) until this function multiplies it by
(1 - discount_pct) to produce a what-if estimate; the "list price (effective)" / "what-if: ..."
label text itself belongs to app/api/service.py (est_label), not here.

app/core/data.py returns raw, list-priced finding rows; pages call pricing.apply on the
DataFrame they get back -- data.py itself never calls this (PLAN.md 5.6 Inputs).

Stdlib + pandas only.
"""
from __future__ import annotations

import re

import pandas as pd

# ^est_(?:[a-z]+_)?usd_list$ -- matches est_usd_list, est_compute_usd_list, est_storage_usd_list,
# ... but never est_usd_list_share (the trailing $ anchors the match, so a _share suffix fails
# it) and never a plain usage/DBU column (must start with "est_" and end in "_usd_list").
# DEC-50 / DEC-66.1: the list-priced dollar family (this repo's one price basis: the effective
# list price from system.billing.list_prices, no discount assumed by default).
_LIST_COL_RE = re.compile(r"^(?:est_(?:[a-z]+_)?usd_list|net_list_cost(?:_usd)?)$")

# Money whose basis is NOT the list price this app standardises on: never discounted (app/core/
# pricing.apply() never adds a "_disc" twin for these -- they fail _LIST_COL_RE above on purpose),
# labelled by what they actually are instead, the same dict app/ui/catalog.py (Streamlit table
# headers) and app/api/service.py (the React finding-detail table) both read, so neither UI can
# disagree on the label for the same column.
#
# Empty as of DEC-66.1/T-69A (integrator pass): this dict used to carry net_default_cost
# ("at account rate") and net_billed_cost ("as billed"), both sourced from
# system.billing.list_prices.pricing.default (a different rate field than
# pricing.effective_list.default) on cost_actual_vs_list_by_sku.sql/cost_cloud_infra.sql. T-69A
# eliminated the last two non-list-basis dollar columns in this app: cost_cloud_infra's
# net_billed_cost was renamed net_list_cost (it now matches _LIST_COL_RE above and IS
# discounted); cost_actual_vs_list_by_sku's net_default_cost was dropped entirely -- that query
# now emits only net_list_cost and always reads NOT_ASSESSED (no negotiated-rate source exists
# anywhere in system.billing, so an "actual vs list" comparison, the reason a second, non-list
# column existed at all, is not assessable -- see config/library_corrections.yml). DEC-66.1's
# "one basis everywhere" is therefore now literally true: every dollar column in this app is the
# effective list price. This dict and the labelling path that reads it (app/api/service.py's
# _money_label, app/ui/style.column_config_for) are kept, unused, for a future dollar column
# whose basis genuinely is not the list price -- not deleted, so that path needs no new plumbing.
NON_DISCOUNT_MONEY_LABELS: dict[str, str] = {}


def apply(df: pd.DataFrame, discount_pct: float) -> pd.DataFrame:
    """Return a copy of df with a "<col>_disc" column added for every column matching
    ^est_(?:[a-z]+_)?usd_list$ (col * (1 - discount_pct); discount_pct is a fraction, e.g. 0.15
    for 15% off list). Every other column, including any `*_share` column, is copied through
    unchanged -- never duplicated, never scaled."""
    out = df.copy()
    for col in df.columns:
        if _LIST_COL_RE.match(col):
            out[f"{col}_disc"] = df[col] * (1 - discount_pct)
    return out
