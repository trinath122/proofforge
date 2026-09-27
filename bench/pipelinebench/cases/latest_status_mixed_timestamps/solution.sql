DROP TABLE IF EXISTS order_status;
CREATE TABLE order_status AS
SELECT order_id, status, updated_at
FROM (
    SELECT order_id,
           status,
           datetime(event_ts) AS updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY order_id
               ORDER BY datetime(event_ts) DESC, event_id DESC
           ) AS rn
    FROM raw_order_events
)
WHERE rn = 1;
