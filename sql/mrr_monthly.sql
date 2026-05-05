WITH historical AS (
  WITH
    month_series AS (
      SELECT m AS month
      FROM UNNEST(GENERATE_DATE_ARRAY(
        DATE '2025-11-01', DATE '2026-04-01', INTERVAL 1 MONTH
      )) AS m
    ),
    line_items AS (
      SELECT
        CAST(amount AS NUMERIC) / 100
          / GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)
          AS monthly_contribution,
        DATE_TRUNC(DATE(period_start), MONTH) AS first_covered_month,
        GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1) AS months_covered
      FROM `stripe_raw.invoice_line_items`
      WHERE invoice_status = 'paid' AND currency = 'usd'
    )
  SELECT
    ms.month,
    ROUND(COALESCE(SUM(li.monthly_contribution), 0), 2) AS mrr_amount,
    FALSE AS is_current
  FROM month_series ms
  LEFT JOIN line_items li
    ON ms.month >= li.first_covered_month
   AND ms.month <  DATE_ADD(li.first_covered_month, INTERVAL li.months_covered MONTH)
  GROUP BY ms.month
),
current_mrr AS (
  SELECT 
    DATE_TRUNC(CURRENT_DATE(), MONTH) AS month,
    ROUND(SUM(amount / 100.0 / GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)), 2) AS mrr_amount,
    TRUE AS is_current
  FROM `stripe_raw.invoice_line_items`
  WHERE invoice_status = 'paid'
    AND currency = 'usd'
    AND period_start <= CURRENT_TIMESTAMP()
    AND period_end > CURRENT_TIMESTAMP()
)

SELECT * FROM historical
UNION ALL
SELECT * FROM current_mrr
ORDER BY month;