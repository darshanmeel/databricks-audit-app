"""tests/test_waste_contract.py -- tasks/P4-WASTE-SPEC.md 9.3 item 7 (P4-01-UI): a static contract
test for the Waste UI's own money rule (DEC-68 -- "stop showing spend as waste"), run without a
browser or a build. It parses WASTE_ITEMS straight out of web/src/components/tab_registry.ts (a
regex, so it runs without Node) and checks it against the registry's own generated SQL, so a future WASTE_ITEMS edit that reintroduces a
resource's own spend as a "waste" dollar column, or names a column that does not exist in the
query's own SELECT list, fails here instead of silently rendering a wrong number.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import registry  # noqa: E402

WEB = ROOT / "web" / "src"

# One WASTE_ITEMS entry, written as a single line in tab_registry.ts: { id: "...", area: "...",
# dollarCol: "..."|null, dbuCol: "..."|null, why: "..." },
WASTE_ITEM_RE = re.compile(
    r'\{\s*id:\s*"(?P<id>[a-z0-9_]+)"\s*,\s*area:\s*"(?P<area>[a-z_]+)"\s*,\s*'
    r'dollarCol:\s*(?P<dollarCol>"[a-z0-9_]+"|null)\s*,\s*'
    r'dbuCol:\s*(?P<dbuCol>"[a-z0-9_]+"|null)\s*,'
)


def _js_str_or_none(raw: str) -> str | None:
    return None if raw == "null" else raw.strip('"')


def _load_waste_items() -> list[dict]:
    text = (WEB / "components" / "tab_registry.ts").read_text(encoding="utf-8")
    m = re.search(r"const WASTE_ITEMS(?::[^=]+)? = \[(?P<body>.*?)\n\];", text, re.DOTALL)
    assert m, "WASTE_ITEMS array not found (or its shape changed) in tab_registry.ts"
    items = [
        {
            "id": mm.group("id"),
            "area": mm.group("area"),
            "dollarCol": _js_str_or_none(mm.group("dollarCol")),
            "dbuCol": _js_str_or_none(mm.group("dbuCol")),
        }
        for mm in WASTE_ITEM_RE.finditer(m.group("body"))
    ]
    # A parse that silently matched nothing (a formatting change the regex no longer recognises)
    # must fail loudly here, not pass every assertion below over an empty list.
    assert len(items) >= 5, f"WASTE_ITEMS parse found only {len(items)} entries -- check the regex"
    return items


def test_waste_items_dollar_column_is_only_the_one_waste_basis_column():
    # DEC-68 / D-W1: `dollarCol` is only "est_wasted_usd_list" (waste_total.ts's own
    # isWasteDollarColumn) or null -- never a resource's own spend column.
    for item in _load_waste_items():
        assert item["dollarCol"] in (None, "est_wasted_usd_list"), item


def test_waste_items_columns_exist_on_their_own_query():
    for item in _load_waste_items():
        spec = registry.by_id(item["id"])
        for col in (item["dollarCol"], item["dbuCol"]):
            if col is None:
                continue
            assert re.search(rf"AS\s+{re.escape(col)}\b", spec.body, re.IGNORECASE), (
                f"{item['id']}: {col!r} is not an output column of its own SQL (registry.by_id().body)"
            )


def test_waste_items_carry_the_three_measured_possible_waste_checks():
    ids = {item["id"] for item in _load_waste_items()}
    assert {
        "compute_warehouse_idle_minutes", "lakeflow_failed_jobs_wasted_dbus", "cost_failed_statement_waste",
    } <= ids
    # D-W4: compute_warehouse_idle_gaps' own dollar figure is the warehouse's WHOLE spend, not the
    # idle stretch that flags it -- it left WASTE_ITEMS and must never come back with a dollarCol.
    assert "compute_warehouse_idle_gaps" not in ids


def test_overview_no_longer_projects_a_monthly_waste_figure():
    text = (WEB / "tabs" / "tab_overview.tsx").read_text(encoding="utf-8")
    assert "monthlyWaste" not in text
    assert '"/mo"' not in text


def test_is_waste_dollar_column_is_defined_only_in_waste_total():
    hits = [
        p.name for p in WEB.rglob("*.ts*")
        if "function isWasteDollarColumn" in p.read_text(encoding="utf-8")
    ]
    assert hits == ["waste_total.ts"]
