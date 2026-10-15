"""All findings tab (web/src/tabs/tab_findings.tsx): every check this audit runs, one row per
check, worst first -- the app's own single-table view, read from {S}.snapshot_log via
bdd._status_sql (the same helper every per-page "Checks on this page" table already uses). No per-
check row dump here: with ~150 checks that would blow the 100-dataset-per-dashboard budget for no
reason, since every check's own rows already live on its home tab's dashboard.

This page has nothing of its own to filter by workspace -- snapshot_log carries one row per check,
account-wide -- so the Workspace filter on this dashboard has no dataset to reach."""
from tools import build_databricks_dashboards as bdd
from tools.dashboard_kit import INT, kpi, table

TOTAL_CHECKS = len(bdd.all_checks())


def _home_labels():
    """query_id -> "Area › Sub-tab", the same precedence tab_registry.ts's buildQueryHome uses
    (first area to claim a check wins) -- a check absent here falls back to "All findings"."""
    areas = bdd.area_registry()
    home: dict[str, str] = {}
    for area_key in bdd._HOME_ORDER:
        area = areas.get(area_key)
        if not area:
            continue
        for _, page_label, ids, refs in area["pages"]:
            label = area["label"] if page_label == area["label"] else f"{area['label']} › {page_label}"
            for q in ids + refs:
                home.setdefault(q, label)
    return home


def _verdict_sql(d):
    home = _home_labels()
    entries = [(q, d.title_of(q), home.get(q, "All findings")) for q in bdd.all_checks()]
    return bdd._status_sql("{S}", entries)


def build(d):
    p = d.page("all")
    v_sql = _verdict_sql(d)

    def count_kpi(title, where, how, tone=None):
        return kpi(title, "WITH v AS (\n" + v_sql + f")\nSELECT count_if({where}) AS value, "
                   f"concat('of ', CAST({TOTAL_CHECKS} AS STRING), ' checks') AS target FROM v",
                   target="target", tone=tone, how=how)

    p.kpis(
        count_kpi("Critical", "verdict = 'Critical'", "Checks whose snapshot verdict is Critical, account-wide.", tone="crit"),
        count_kpi("Warn", "verdict = 'Warn'", "Checks whose snapshot verdict is Warn, account-wide.", tone="warn"),
        count_kpi("No data this window", "verdict IN ('No rows', 'Not run')",
                  "Checks that ran clean but found nothing, or never ran this window, account-wide."),
        count_kpi("Not assessed", "verdict = 'Not assessed'",
                  "Checks the snapshot could not run at all -- a missing grant or a failed read, account-wide.", tone="warn"),
    )
    p.text(["Ordered critical first, then warn, then everything else. \"Home tab\" is where each "
            "check's own rows live; \"Not assessed\" means the snapshot could not run it.",
            "The snapshot's run log carries no workspace_id, so every count on this page is account-wide; "
            "the Workspace filter above does not reach it."], height=2)
    p.table(table("Every check, flagged first", v_sql,
                  [("check_title", "Check"), ("page", "Home tab"), ("verdict", "Verdict"),
                   ("critical_rows", "Critical rows", INT), ("warn_rows", "Warn rows", INT),
                   ("row_count", "Rows", INT), ("error", "Why not assessed")], height=12,
                  how="Every check's own verdict from the snapshot's run log, critical first then warn; account-wide."))
