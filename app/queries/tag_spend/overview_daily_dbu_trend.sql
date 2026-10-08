-- overview_daily_dbu_trend over the dollars a tag filter keeps (app/core/tag_spend.py).
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day)
SELECT __W__ AS window_days, t.workspace_id, t.usage_date, SUM(t.quantity) AS net_dbus
FROM tagged t, snap a
WHERE t.usage_unit = 'DBU' AND t.usage_date >= a.d - __W__ AND t.usage_date < a.d
GROUP BY t.workspace_id, t.usage_date
