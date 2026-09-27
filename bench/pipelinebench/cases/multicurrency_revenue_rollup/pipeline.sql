-- Monthly revenue by customer region, in USD.
DROP TABLE IF EXISTS monthly_region_revenue;
CREATE TABLE monthly_region_revenue AS
SELECT substr(o.order_ts, 1, 7) AS month,
       c.region,
       COUNT(*) AS orders,
       ROUND(SUM(o.amount * f.usd_rate), 2) AS gross_usd,
       ROUND(SUM(COALESCE(r.amount, 0) * f.usd_rate), 2) AS refunds_usd,
       ROUND(SUM((o.amount - COALESCE(r.amount, 0)) * f.usd_rate), 2) AS net_usd
FROM raw_orders_ingest AS o
JOIN raw_customers AS c
  ON c.customer_id = o.customer_id AND c.valid_to IS NULL
LEFT JOIN raw_fx_rates AS f
  ON f.currency = o.currency AND f.date = date(o.order_ts)
LEFT JOIN raw_refunds AS r
  ON r.order_id = o.order_id
WHERE o.status = 'completed'
GROUP BY 1, 2;
