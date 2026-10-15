-- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.1). Rule 3's proof as a singular dbt test: every row
-- of tags.cost_reconciliation where the independently-computed billing total and tags.cost_unit's
-- own total disagree by 0.005 (dollars) or 0.0001 (unpriced quantity) or more. A real account's
-- build then flags a broken split on its own, the moment tags.cost_unit stops accounting for
-- every dollar of system.billing.usage.
SELECT *
FROM {{ ref('cost_reconciliation') }}
WHERE abs(billing_usd - unit_usd) >= 0.005
   OR abs(billing_unpriced_quantity - unit_unpriced_quantity) >= 0.0001
