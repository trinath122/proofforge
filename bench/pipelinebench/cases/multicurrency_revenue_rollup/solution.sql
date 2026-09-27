DROP TABLE IF EXISTS monthly_region_revenue;
CREATE TABLE monthly_region_revenue AS
WITH latest AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY _ingested_at DESC) AS rn
    FROM raw_orders_ingest
),
orders AS (
    SELECT order_id, customer_id, date(order_ts) AS order_date,
           substr(order_ts, 1, 7) AS month, currency, amount
    FROM latest
    WHERE rn = 1 AND status = 'completed'
),
enriched AS (
    SELECT o.order_id, o.month, o.amount,
           COALESCE((
               SELECT c.region FROM raw_customers AS c
               WHERE c.customer_id = o.customer_id
                 AND c.valid_from <= o.order_date
                 AND (c.valid_to IS NULL OR o.order_date < c.valid_to)
               ORDER BY c.valid_from DESC LIMIT 1
           ), 'unknown') AS region,
           CASE WHEN o.currency = 'USD' THEN 1.0 ELSE (
               SELECT f.usd_rate FROM raw_fx_rates AS f
               WHERE f.currency = o.currency AND f.date <= o.order_date
               ORDER BY f.date DESC LIMIT 1
           ) END AS fx
    FROM orders AS o
),
refunds AS (
    SELECT order_id, SUM(amount) AS refunded FROM raw_refunds GROUP BY order_id
)
SELECT e.month,
       e.region,
       COUNT(*) AS orders,
       ROUND(SUM(e.amount * e.fx), 2) AS gross_usd,
       ROUND(SUM(COALESCE(r.refunded, 0) * e.fx), 2) AS refunds_usd,
       ROUND(SUM((e.amount - COALESCE(r.refunded, 0)) * e.fx), 2) AS net_usd
FROM enriched AS e
LEFT JOIN refunds AS r ON r.order_id = e.order_id
GROUP BY e.month, e.region;
