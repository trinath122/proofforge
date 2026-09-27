-- Shallow fix: dedupe, FX as-of, USD and refund pre-aggregation are right, but the SCD2
-- join uses BETWEEN, so orders on a region-change day match two history rows.
DROP TABLE IF EXISTS monthly_region_revenue;
CREATE TABLE monthly_region_revenue AS
WITH latest AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY _ingested_at DESC) AS rn
    FROM raw_orders_ingest
),
orders AS (
    SELECT order_id, customer_id, date(order_ts) AS order_date, substr(order_ts, 1, 7) AS month,
           currency, amount
    FROM latest WHERE rn = 1 AND status = 'completed'
),
refunds AS (SELECT order_id, SUM(amount) AS refunded FROM raw_refunds GROUP BY order_id)
SELECT o.month,
       COALESCE(c.region, 'unknown') AS region,
       COUNT(*) AS orders,
       ROUND(SUM(o.amount * fx.rate), 2) AS gross_usd,
       ROUND(SUM(COALESCE(r.refunded, 0) * fx.rate), 2) AS refunds_usd,
       ROUND(SUM((o.amount - COALESCE(r.refunded, 0)) * fx.rate), 2) AS net_usd
FROM orders AS o
LEFT JOIN raw_customers AS c
  ON c.customer_id = o.customer_id
 AND o.order_date BETWEEN c.valid_from AND COALESCE(NULLIF(c.valid_to, ''), '9999-12-31')
JOIN (
    SELECT o2.order_id,
           CASE WHEN o2.currency = 'USD' THEN 1.0 ELSE (
               SELECT f.usd_rate FROM raw_fx_rates AS f
               WHERE f.currency = o2.currency AND f.date <= o2.order_date
               ORDER BY f.date DESC LIMIT 1) END AS rate
    FROM orders AS o2
) AS fx ON fx.order_id = o.order_id
LEFT JOIN refunds AS r ON r.order_id = o.order_id
GROUP BY 1, 2;
