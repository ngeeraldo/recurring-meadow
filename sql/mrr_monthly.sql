WITH
  month_series AS (
    SELECT m AS month
    FROM UNNEST(GENERATE_DATE_ARRAY(
      DATE '2025-11-01', DATE '2026-04-01', INTERVAL 1 MONTH
    )) AS m
  ),
  line_items AS (
    SELECT
      customer_id,
      CAST(amount AS NUMERIC) / 100
        / GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)
        AS monthly_contribution,
      DATE_TRUNC(DATE(period_start), MONTH) AS first_covered_month,
      GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1) AS months_covered
    FROM `stripe_raw.invoice_line_items`
    WHERE invoice_status = 'paid' AND currency = 'usd'
  ),
  per_customer_monthly AS (
    SELECT
      ms.month,
      li.customer_id,
      SUM(li.monthly_contribution) AS mrr_amount
    FROM month_series ms
    LEFT JOIN line_items li
      ON ms.month >= li.first_covered_month
     AND ms.month <  DATE_ADD(li.first_covered_month, INTERVAL li.months_covered MONTH)
    WHERE li.customer_id IS NOT NULL
    GROUP BY ms.month, li.customer_id
  ),
  per_customer_current AS (
    SELECT
      customer_id,
      SUM(amount / 100.0
        / GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)
      ) AS mrr_amount
    FROM `stripe_raw.invoice_line_items`
    WHERE invoice_status = 'paid'
      AND currency = 'usd'
      AND period_start <= CURRENT_TIMESTAMP()
      AND period_end > CURRENT_TIMESTAMP()
    GROUP BY customer_id
  ),
  historical AS (
    SELECT
      ms.month,
      ROUND(COALESCE(SUM(pcm.mrr_amount), 0), 2) AS mrr_amount,
      FALSE AS is_current
    FROM month_series ms
    LEFT JOIN per_customer_monthly pcm USING (month)
    GROUP BY ms.month
  ),
  current_mrr AS (
    SELECT
      DATE_TRUNC(CURRENT_DATE(), MONTH) AS month,
      ROUND(SUM(mrr_amount), 2) AS mrr_amount,
      TRUE AS is_current
    FROM per_customer_current
  )

-- ROLLUP -- (validate_mrr.py splits on this marker to reuse the CTEs above)
SELECT * FROM historical
UNION ALL
SELECT * FROM current_mrr
ORDER BY month;
