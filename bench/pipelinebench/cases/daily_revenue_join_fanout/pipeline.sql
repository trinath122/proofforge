DROP TABLE IF EXISTS daily_revenue;
CREATE TABLE daily_revenue AS
SELECT o.order_date,
       c.region,
       ROUND(SUM(o.amount), 2) AS revenue,
       COUNT(*) AS orders
FROM raw_orders AS o
JOIN raw_customers AS c ON c.customer_id = o.customer_id
GROUP BY o.order_date, c.region;
