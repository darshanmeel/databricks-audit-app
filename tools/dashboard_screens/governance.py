"""Governance tab (web/src/tabs/tab_governance.tsx): Access & grants, Admin activity, Sensitive
data, Lineage, Sharing. Whole metastore -- most checks here carry no workspace_id, so {ws} is
used only where the underlying check does (the audit-log and PO checks)."""
from tools.dashboard_kit import INT, bar, kpi


def build(d):
    access(d)
    admin(d)
    sensitive(d)
    lineage(d)
    sharing(d)


def access(d):
    p = d.page("access")
    p.kpis(
        kpi("Broad grants", """
WITH r AS (SELECT * FROM {S}.access_broad_grants)
SELECT (SELECT count(*) FROM r) AS value,
       concat(CAST((SELECT count(*) FROM r WHERE status = 'CRITICAL') AS STRING), ' critical; worst: ',
              COALESCE((SELECT concat(grantee, ', ', privilege_type, ' on ', securable) FROM r
                        ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END LIMIT 1), 'none')) AS target
""", target="target", tone="crit",
            how="Every row in the broad-grants check (ALL_PRIVILEGES or account-wide grants); critical count and worst grant below."),
        kpi("Grants inventory", """
WITH r AS (SELECT * FROM {S}.access_grants_inventory)
SELECT (SELECT COALESCE(SUM(grant_count), 0) FROM r) AS value,
       concat('Top: ', COALESCE((SELECT privilege_type FROM r GROUP BY privilege_type ORDER BY SUM(grant_count) DESC LIMIT 1), '-'),
              '; ', CAST((SELECT count(*) FROM r) AS STRING), ' grantee x privilege rows') AS target
""", target="target", how="All grants from SHOW GRANTS across the metastore, by grantee x privilege; top privilege type below."),
        kpi("Run-as escalation", """
WITH r AS (SELECT * FROM {named:access_runas_escalation} WHERE {ws})
SELECT (SELECT count(*) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS value,
       concat(CAST((SELECT COALESCE(SUM(event_count), 0) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS STRING), ' events; worst: ',
              COALESCE((SELECT concat(run_by, ' running as ', run_as) FROM r WHERE status IN ('CRITICAL', 'WARN')
                        ORDER BY event_count DESC LIMIT 1), 'none')) AS target
""", target="target", tone="warn",
            how="Audit events where a job or query ran as a different identity than the caller, flagged critical or warn."),
        kpi("Network denials", """
WITH i AS (SELECT * FROM {named:access_network_inbound_denials} WHERE {ws}),
     o AS (SELECT * FROM {named:access_network_outbound_denials} WHERE {ws})
SELECT (SELECT COALESCE(SUM(denial_count), 0) FROM i) + (SELECT COALESCE(SUM(denial_count), 0) FROM o) AS value,
       'Inbound + outbound, blocked by IP access list or network policy' AS target
""", target="target", how="Requests blocked by an IP access list or network policy, inbound plus outbound, summed over the window."),
    )
    p.charts(
        bar("Grants by privilege type", """
SELECT privilege_type, SUM(grant_count) AS grants
FROM {S}.access_grants_inventory
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "privilege_type", "grants", fmt=INT, category_title="Privilege", value_title="Grants",
            how="Grant count from SHOW GRANTS, summed by privilege type, top 10."),
        bar("Sign-in events by principal", """
SELECT principal, SUM(event_count) AS events
FROM {named:access_login_concentration}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "principal", "events", fmt=INT, category_title="Principal", value_title="Events",
            how="Audit-log sign-in events per principal, from the login-concentration check, top 8."),
    )
    p.checks()


def admin(d):
    p = d.page("admin")
    p.kpis(
        kpi("Admin and permission changes", """
WITH r AS (SELECT * FROM {named:access_admin_role_change_events} WHERE {ws})
SELECT (SELECT count(*) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS value,
       concat(CAST((SELECT COALESCE(SUM(event_count), 0) FROM r) AS STRING), ' events total; worst: ',
              COALESCE((SELECT concat(actor, ' x ', action_name) FROM r WHERE status IN ('CRITICAL', 'WARN')
                        ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END, event_count DESC LIMIT 1), 'none')) AS target
""", target="target", tone="warn",
            how="Admin or permission-change audit events flagged critical or warn; worst actor and action named below."),
    )
    p.charts(
        bar("Admin and permission change events by actor", """
SELECT COALESCE(actor, '(no actor recorded)') AS actor, SUM(event_count) AS events
FROM {named:access_admin_role_change_events}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "actor", "events", fmt=INT, category_title="Actor", value_title="Events",
            how="Admin-role change events from the audit log, summed by actor, top 8."),
    )
    p.checks()


def sensitive(d):
    p = d.page("sensitive")
    p.kpis(
        kpi("Classified, no mask", """
WITH r AS (SELECT * FROM {S}.access_classified_unmasked)
SELECT (SELECT count_if(status IN ('CRITICAL', 'WARN')) FROM r) AS value,
       concat('of ', CAST((SELECT count(*) FROM r) AS STRING), ' classified columns; worst: ',
              COALESCE((SELECT concat(table_name, '.', column_name) FROM r WHERE status IN ('CRITICAL', 'WARN')
                        ORDER BY status LIMIT 1), 'none')) AS target
""", target="target", tone="crit",
            how="Columns tagged sensitive (PII, PCI, ...) with no column mask applied; worst flagged column below."),
        kpi("Outside tagged tables", """
WITH r AS (SELECT * FROM {S}.access_pii_outside_tables)
SELECT (SELECT count(*) FROM r) AS value,
       concat('Top: ', COALESCE((SELECT object_type FROM r GROUP BY object_type ORDER BY count(*) DESC LIMIT 1), 'none')) AS target
""", target="target", tone="warn",
            how="Objects (views, volumes, ...) carrying PII outside a classified table, so a mask or filter can't reach them."),
        kpi("Column masks in place", """
WITH r AS (SELECT * FROM {S}.access_column_masks_inventory)
SELECT (SELECT count(*) FROM r) AS value,
       concat('Top: ', COALESCE((SELECT mask_name FROM r GROUP BY mask_name ORDER BY count(*) DESC LIMIT 1), 'none')) AS target
""", target="target", how="Column masks defined in the metastore (SET MASK), counted once per column; top mask name below."),
        kpi("Row filters in place", """
SELECT count(*) AS value, 'Set on the table only' AS target FROM {S}.access_row_filters_inventory
""", target="target", how="Row filters set directly on a table (SET ROW FILTER); ABAC policies read live aren't counted here."),
        kpi("Tagged objects", """
WITH r AS (SELECT * FROM {S}.access_tags_inventory)
SELECT (SELECT COALESCE(SUM(tagged_object_count), 0) FROM r) AS value,
       concat('Top tag: ', COALESCE((SELECT tag_name FROM r ORDER BY tagged_object_count DESC LIMIT 1), 'none')) AS target
""", target="target", how="Objects carrying any Unity Catalog tag, from SHOW TAGS; top tag name below."),
    )
    p.charts(
        bar("Outside tagged tables, by object type", """
SELECT object_type, count(*) AS objects
FROM {S}.access_pii_outside_tables
GROUP BY 1 ORDER BY 2 DESC
""", "object_type", "objects", fmt=INT, category_title="Object type", value_title="Objects",
            how="PII found outside a classified table, counted by object type (view, volume, ...)."),
        bar("Column masks by name", """
SELECT mask_name, count(*) AS columns
FROM {S}.access_column_masks_inventory
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "mask_name", "columns", fmt=INT, category_title="Mask", value_title="Columns",
            how="Column masks in the metastore, counted by mask name, top 8."),
    )
    p.text(["ABAC row filters and column masks (SHOW POLICIES) are read live by the app, not from the "
            "snapshot, so they can't be reproduced here."], height=1)
    p.checks()


def lineage(d):
    p = d.page("lineage")
    p.kpis(
        kpi("Table blast radius", """
WITH r AS (SELECT * FROM {named:access_table_lineage_blast_radius} WHERE {ws})
SELECT (SELECT count(DISTINCT source_table_full_name) FROM r) AS value,
       COALESCE((SELECT concat('Widest: ', source_table_full_name, ' -> ', target_table_full_name) FROM r
                 ORDER BY COALESCE(source_distinct_principals, distinct_principals) DESC LIMIT 1), 'None') AS target
""", target="target", how="Distinct source tables with a downstream lineage edge; widest reach by distinct principals below."),
        kpi("Sensitive column reach", """
WITH r AS (SELECT * FROM {named:access_column_lineage_sensitive_reach} WHERE {ws})
SELECT (SELECT count(*) FROM r) AS value,
       COALESCE((SELECT concat('Widest: ', source_column_name, ' -> ', target_column_name) FROM r
                 ORDER BY distinct_principals DESC LIMIT 1), 'None') AS target
""", target="target", how="Column-level lineage edges out of a sensitive source column; widest reach by distinct principals below."),
        kpi("PII propagating untagged", """
WITH r AS (SELECT * FROM {named:access_pii_propagation_untagged} WHERE {ws})
SELECT (SELECT count(DISTINCT concat_ws('.', source_table_catalog, source_table_schema, source_table_name, source_column_name,
                                        '>', target_table_catalog, target_table_schema, target_table_name, target_column_name))
        FROM r) AS value,
       COALESCE((SELECT concat('Worst: ', source_table_name, '.', source_column_name, ' -> ',
                                target_table_name, '.', target_column_name) FROM r
                 ORDER BY event_count DESC LIMIT 1), 'None') AS target
""", target="target", tone="warn",
            how="Distinct source-to-target column edges where PII reaches an untagged column; worst by event count below."),
    )
    p.charts(
        bar("Downstream access events by class", """
SELECT COALESCE(access_class, '(unknown)') AS access_class, SUM(event_count) AS events
FROM {named:access_table_lineage_blast_radius}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC
""", "access_class", "events", fmt=INT, category_title="Access class", value_title="Events",
            how="Lineage access events, summed by access class (direct read, downstream, ...)."),
        bar("PII propagating into untagged columns", """
SELECT MAX(concat(source_table_name, '.', source_column_name, ' -> ', target_table_name, '.', target_column_name)) AS edge,
       SUM(event_count) AS events
FROM {named:access_pii_propagation_untagged}
WHERE {ws}
GROUP BY concat_ws('.', source_table_catalog, source_table_schema, source_table_name, source_column_name,
                    '>', target_table_catalog, target_table_schema, target_table_name, target_column_name)
ORDER BY 2 DESC LIMIT 8
""", "edge", "events", fmt=INT, category_title="Edge", value_title="Events",
            how="Distinct PII source-to-target edges by full catalog.schema.table.column, labeled short, top 8 by events."),
    )
    p.checks()


def sharing(d):
    p = d.page("sharing")
    p.kpis(
        kpi("Delta Sharing exposure", """
WITH r AS (SELECT * FROM {S}.access_delta_sharing_exposure)
SELECT (SELECT COALESCE(SUM(recipient_count), 0) FROM r) AS value,
       COALESCE((SELECT concat('Widest: ', COALESCE(share_name, '(unnamed share)'), ', ',
                                CAST(recipient_count AS STRING), ' recipients') FROM r
                 ORDER BY recipient_count DESC LIMIT 1), 'None') AS target
""", target="target", how="Delta Sharing recipients, summed across all shares; widest share by recipient count below."),
    )
    p.checks()
