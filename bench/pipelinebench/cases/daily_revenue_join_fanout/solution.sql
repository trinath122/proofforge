DROP TABLE IF EXISTS daily_revenue;
CREATE TABLE daily_revenue AS
WITH latest_customer AS (
    SELECT customer_id, region
    FROM (
        SELECT customer_id,
               region,
               ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY updated_at DESC) AS rn
        FROM raw_customers
    )
    WHERE rn = 1
)
SELECT o.order_date,
       COALESCE(c.region, 'unknown') AS region,
       ROUND(SUM(o.amount), 2) AS revenue,
       COUNT(*) AS orders
FROM raw_orders AS o
LEFT JOIN latest_customer AS c ON c.customer_id = o.customer_id
GROUP BY o.order_date, COALESCE(c.region, 'unknown');
