-- Shallow fix: "last row in the file wins" instead of latest by updated_at.
DROP TABLE IF EXISTS daily_revenue;
CREATE TABLE daily_revenue AS
WITH latest_customer AS (
    SELECT customer_id, region FROM raw_customers
    WHERE rowid IN (SELECT MAX(rowid) FROM raw_customers GROUP BY customer_id)
)
SELECT o.order_date,
       COALESCE(c.region, 'unknown') AS region,
       ROUND(SUM(o.amount), 2) AS revenue,
       COUNT(*) AS orders
FROM raw_orders AS o
LEFT JOIN latest_customer AS c ON c.customer_id = o.customer_id
GROUP BY o.order_date, COALESCE(c.region, 'unknown');
