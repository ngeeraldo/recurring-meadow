-- ============================================================================
-- mrr_monthly.sql
--
-- Computes Monthly Recurring Revenue (MRR) per calendar month from the paid
-- USD invoice line items in `stripe_raw.invoice_line_items`.
--
-- Output: one row per month from Nov 2025 through Apr 2026 (6 rows, ASC).
-- May 2026 is excluded — it's still in progress.
--
-- ----------------------------------------------------------------------------
-- Period-spreading logic
--
-- Each line item carries the total `amount` Stripe charged for the billing
-- period [period_start, period_end]. To get a *monthly* figure we spread
-- that amount evenly across every calendar month the period covers:
--
--     months_covered      = max(DATE_DIFF(period_end, period_start, MONTH), 1)
--     monthly_contribution = amount / 100 / months_covered
--
--   * Annual line ($120, 12 months) → $10 contribution to each of 12 months.
--   * Monthly line ($10, 1 month)   → $10 to one month.
--   * Sub-month proration line      → max(.., 1) guards div-by-zero and
--                                      bills the full amount to one month.
--
-- ----------------------------------------------------------------------------
-- Why half-open attribution
--
-- Stripe's `period_end` is the *start* of the next billing period, not the
-- last day of this one. A monthly sub with period [Nov 5, Dec 5] should be
-- attributed entirely to November — Dec 5 belongs to the next period.
--
-- Naive `BETWEEN DATE_TRUNC(period_start) AND DATE_TRUNC(period_end)` would
-- pick up both Nov 1 and Dec 1, double-counting the line item. Instead we
-- join with a half-open range:
--
--     month >= first_covered_month
--     month <  first_covered_month + months_covered MONTH
--
-- which produces exactly `months_covered` matching months — consistent with
-- the math above.
--
-- ----------------------------------------------------------------------------
-- Notes
--   * `amount` is in Stripe's minor units (cents); divide by 100 for dollars.
--   * Math runs in NUMERIC throughout to avoid floating-point rounding.
--   * `interval` is a BigQuery reserved word — escape as `interval` if ever
--     referenced. This query doesn't need it because DATE_DIFF on the period
--     bounds gives the same information (1 for monthly, 12 for annual).
-- ============================================================================

WITH
  -- (1) The six reporting months we want a row for.
  month_series AS (
    SELECT m AS month
    FROM UNNEST(GENERATE_DATE_ARRAY(
      DATE '2025-11-01', DATE '2026-04-01', INTERVAL 1 MONTH
    )) AS m
  ),

  -- (2) Per-line monthly contribution + the inclusive lower bound and the
  --     length (in months) of the attribution range. Filtered to paid USD.
  line_items AS (
    SELECT
      CAST(amount AS NUMERIC) / 100
        / GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)
        AS monthly_contribution,
      DATE_TRUNC(DATE(period_start), MONTH) AS first_covered_month,
      GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)
        AS months_covered
    FROM `stripe_raw.invoice_line_items`
    WHERE invoice_status = 'paid'
      AND currency = 'usd'
  )

-- (3) For each month, sum the contribution of every line whose half-open
--     attribution range contains it. LEFT JOIN so an empty month still
--     reports 0.00 instead of dropping out.
SELECT
  ms.month,
  ROUND(COALESCE(SUM(li.monthly_contribution), 0), 2) AS mrr_amount
FROM month_series AS ms
LEFT JOIN line_items AS li
  ON ms.month >= li.first_covered_month
 AND ms.month <  DATE_ADD(li.first_covered_month, INTERVAL li.months_covered MONTH)
GROUP BY ms.month
ORDER BY ms.month;
