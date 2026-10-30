{{ config(materialized='table', tags=['dims']) }}
-- T-23. Current row per (cloud, sku_name, currency_code) from billing.list_prices
-- (PLAN.md 5.5 / tasks/T-23): the fixture (tests/fixtures/billing.py) marks the current row with
-- price_end_time IS NULL; ROW_NUMBER() over price_start_time DESC guards against more than one
-- row surviving that filter for the same (cloud, sku_name, currency_code) triple. list_price is
-- the EFFECTIVE list price (pricing.effective_list.default) -- the same basis every est_usd_list
-- column in this app uses, from the same de-duplicated list_prices macro every other price join
-- reads (a raw read of system.billing.list_prices can double-count an instant two rows both cover).
WITH ranked AS (
    SELECT
        cloud,
        sku_name,
        currency_code,
        usage_unit,
        CAST(pricing.effective_list.default AS DOUBLE) AS list_price,
        price_start_time,
        price_end_time,
        ROW_NUMBER() OVER (
            PARTITION BY cloud, sku_name, currency_code
            ORDER BY price_start_time DESC
        ) AS rn
    FROM {{ list_prices() }}
    WHERE price_end_time IS NULL
)
SELECT
    cloud,
    sku_name,
    currency_code,
    usage_unit,
    list_price,
    price_start_time
FROM ranked
WHERE rn = 1
