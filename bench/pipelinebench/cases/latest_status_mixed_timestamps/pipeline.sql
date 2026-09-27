DROP TABLE IF EXISTS order_status;
CREATE TABLE order_status AS
SELECT order_id,
       status,
       MAX(event_ts) AS updated_at
FROM raw_order_events
GROUP BY order_id;
