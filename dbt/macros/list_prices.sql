{#
    list_prices(): system.billing.list_prices, de-duplicated (docs/REVIEW.md C3). Two overlapping
    price rows for the same (sku_name, cloud, usage_unit) fan out every join that reads the table
    raw -- one usage row matches both, doubling its dollars. Keeps USD rows only (the app prints $
    everywhere) and caps each row's price_end_time at the next row's price_start_time for that
    (sku_name, cloud, usage_unit) when that is earlier, so exactly one price row covers any instant
    and the latest start wins; rows left with no open interval (effective end at or before their
    own start) are dropped.
#}
{% macro list_prices() %}
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM {{ source('system_billing', 'list_prices') }}
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
{% endmacro %}
